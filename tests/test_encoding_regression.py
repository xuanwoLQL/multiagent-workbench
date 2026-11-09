"""编码回归测试（契约 ``docs/UI-OPS-REDESIGN.md`` §5.2/§5.3）。

覆盖三层：

1. **解码原语**：``decode_output`` 对 GBK 字节的解码策略（UTF-8 → 本地代码页 → GBK → replace）；
2. **子进程层**：假 CLI 用 cp936 写 stdout/stderr → ``run_command_tree`` 必须返回中文，
   不抛异常、不出现 reader 线程崩溃（``text=True`` 未显式 encoding 的历史故障）；
3. **ACP 层**：假 ACP 服务端往 stderr 写 GBK 字节 → 适配器按字节读取 + ``decode_output``
   解码，``result.stderr`` 里是中文而不是 ``\\ufffd``，且 reader 线程不崩；
4. **HTTP 全链路**：中文项目名 / 任务标题 / 用户原话 从创建 → SQLite → API 全程 Unicode，
   响应字节是合法 UTF-8，不出现 ``?`` / ``\\ufffd`` 替换字符。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from multiagent.agents import proc as procmod
from multiagent.agents.acp_adapter import AcpAdapter, _Session
from multiagent.agents.base import AgentRequest
from multiagent.api.app import create_api

PY = sys.executable

#: 任何一处出现它都说明解码退化成「替换字符」了。
REPLACEMENT = "\ufffd"


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _write_script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


# ===========================================================================
# 1) 解码原语
# ===========================================================================


def test_decode_output_prefers_utf8_then_local_codepage():
    text = "中文诊断：编码正确"
    assert procmod.decode_output(text.encode("utf-8")) == text
    assert procmod.decode_output(text.encode("cp936")) == text
    assert procmod.decode_output(text) == text
    assert procmod.decode_output(None) == ""
    assert procmod.decode_output(b"") == ""
    # 纯 ASCII 不变
    assert procmod.decode_output(b"hello\n") == "hello\n"


def test_decode_output_never_raises_on_garbage():
    # 非法字节序列也不能抛异常（宁可出替换字符，也不能让 reader 线程崩掉）
    assert isinstance(procmod.decode_output(b"\xff\xfe\x00\x81"), str)


# ===========================================================================
# 2) run_command_tree：假 CLI 用 cp936 输出
# ===========================================================================


def test_run_command_tree_decodes_gbk_stdout(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "gbk_stdout.py",
        "import sys\n"
        "sys.stdout.buffer.write('中文输出：竞品分析完成\\n'.encode('cp936'))\n"
        "sys.stdout.buffer.flush()\n",
    )
    code, out, err, _ms = procmod.run_command_tree([PY, str(script)], timeout=60)

    assert code == 0, err
    assert "中文输出" in out, f"GBK stdout 没有被正确解码：{out!r}"
    assert REPLACEMENT not in out


def test_run_command_tree_decodes_gbk_stderr(tmp_path: Path):
    script = _write_script(
        tmp_path,
        "gbk_stderr.py",
        "import sys\n"
        "sys.stderr.buffer.write('错误：找不到配置文件\\n'.encode('cp936'))\n"
        "sys.stderr.buffer.flush()\n",
    )
    code, out, err, _ms = procmod.run_command_tree([PY, str(script)], timeout=60)

    assert code == 0
    assert "错误：找不到配置文件" in err, f"GBK stderr 没有被正确解码：{err!r}"
    assert REPLACEMENT not in err


def test_run_command_tree_mixed_utf8_and_gbk_does_not_crash_reader(tmp_path: Path):
    """同一行里既有 UTF-8 又有 GBK 字节：只能降级，不能崩。"""
    script = _write_script(
        tmp_path,
        "mixed_bytes.py",
        "import sys\n"
        "sys.stdout.buffer.write(b'ascii-prefix ' + '中文'.encode('cp936') + b' tail\\n')\n"
        "sys.stdout.buffer.flush()\n",
    )
    captured: list[BaseException] = []
    previous = threading.excepthook
    threading.excepthook = lambda args: captured.append(args.exc_value)
    try:
        code, out, err, _ms = procmod.run_command_tree([PY, str(script)], timeout=60)
    finally:
        threading.excepthook = previous

    assert code == 0, err
    assert out.startswith("ascii-prefix")
    assert not any(isinstance(e, UnicodeDecodeError) for e in captured), captured


# ===========================================================================
# 3) ACP：stderr 按字节读取 + decode_output
# ===========================================================================

_ACP_GBK_SERVER = r'''
"""最小 ACP 服务端：session/prompt 时往 stderr 写 GBK 字节（模拟中文 Windows 上的 CLI）。"""
import json
import sys

SESSION = "gbk-session-1"


def send(message):
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


while True:
    line = sys.stdin.buffer.readline()
    if not line:
        break
    try:
        message = json.loads(line.decode("utf-8"))
    except Exception:
        continue
    method = message.get("method")
    msg_id = message.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": 1}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": SESSION}})
    elif method == "session/prompt":
        sys.stderr.buffer.write("中文诊断：GBK 字节写入 stderr\n".encode("cp936"))
        sys.stderr.buffer.flush()
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": SESSION,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "任务完成"},
                },
            },
        })
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
    elif method in ("session/cancel", "session/close"):
        if msg_id is not None:
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
'''


def test_acp_session_decodes_gbk_stderr_bytes(tmp_path: Path):
    """直接验证 ``_Session``：stderr 是 GBK 字节时，stderr_lines 里必须是中文。"""
    script = _write_script(
        tmp_path,
        "gbk_stderr_sleeper.py",
        "import sys, time\n"
        "sys.stderr.buffer.write('中文错误：模型不可用\\n'.encode('cp936'))\n"
        "sys.stderr.buffer.flush()\n"
        "time.sleep(3)\n",
    )
    session = _Session([PY, str(script)], tmp_path, dict(os.environ), [])
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not session.stderr_lines:
            time.sleep(0.05)
        joined = "\n".join(session.stderr_lines)
        assert "中文错误" in joined, f"GBK stderr 没有被解码：{joined!r}"
        assert REPLACEMENT not in joined
    finally:
        session.close()


def test_acp_adapter_gbk_stderr_keeps_threads_alive(tmp_path: Path):
    """完整 ACP 运行：GBK stderr 不解码崩溃，结果里是中文，且没有 reader 线程异常。"""
    script = _write_script(tmp_path, "gbk_acp_server.py", _ACP_GBK_SERVER)
    agent = {
        "id": "gbkacp",
        "label": "GBK ACP",
        "protocol": "acp",
        "command": PY,
        "args": [str(script)],
        "roles": ["worker"],
        "strengths": ["code"],
        "can_write": True,
        "enabled": True,
    }
    captured: list[BaseException] = []
    previous = threading.excepthook
    threading.excepthook = lambda args: captured.append(args.exc_value)
    try:
        result = AcpAdapter(agent).run(
            AgentRequest(
                prompt="跑一次任务",
                workspace=tmp_path,
                run_dir=tmp_path / "run",
                timeout_sec=60,
            )
        )
    finally:
        threading.excepthook = previous

    assert result.ok is True, result.error
    assert "中文诊断" in result.stderr, f"stderr 解码失败：{result.stderr!r}"
    assert REPLACEMENT not in result.stderr
    assert any("任务完成" in m for m in result.messages), result.messages
    assert not any(isinstance(e, UnicodeDecodeError) for e in captured), captured


# ===========================================================================
# 4) HTTP 全链路：中文创建 → DB → API
# ===========================================================================


def test_chinese_project_and_task_survive_db_and_api(wb):
    client = _client(wb)
    project_name = "中文项目·竞品调研"
    project_label = "竞品调研（中文标签）"
    created = client.post("/api/projects", json={"name": project_name, "label": project_label}).json()
    pid = created["id"]

    # 1) DB 层：原样落库（不是问号、不是替换字符）
    row = wb.db.query_one("SELECT name, label FROM projects WHERE id = ?", (pid,))
    assert row["name"] == project_name
    assert row["label"] == project_label

    # 2) API 层：读回来还是同一串 Unicode
    detail = client.get(f"/api/projects/{pid}").json()
    assert detail["project"]["name"] == project_name
    assert detail["project"]["label"] == project_label

    # 3) 一句话任务：用户原话进 brief，标题取首行
    text = "调研三个竞品并输出竞品分析.md\n要求：对比功能与定价，2000 字"
    quick = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": text, "outputs": ["竞品分析.md"]},
    )
    assert quick.status_code == 200, quick.text
    task_id = quick.json()["tasks"][0]["id"]

    task_row = wb.db.query_one("SELECT title, brief, outputs FROM tasks WHERE id = ?", (task_id,))
    assert task_row["title"] == "调研三个竞品并输出竞品分析.md"
    assert task_row["brief"] == text
    assert json.loads(task_row["outputs"]) == ["竞品分析.md"]

    task = client.get(f"/api/tasks/{task_id}").json()["task"]
    assert task["title"] == "调研三个竞品并输出竞品分析.md"
    assert task["brief"] == text

    # 4) 原始响应字节必须是合法 UTF-8，且没有替换字符 / 问号
    raw = quick.content
    assert raw.decode("utf-8")  # 不抛 UnicodeDecodeError
    body = json.dumps(quick.json(), ensure_ascii=False)
    assert REPLACEMENT not in body
    assert "?" not in body, body
    assert project_name in client.get(f"/api/projects/{pid}").content.decode("utf-8")


def test_chinese_dashboard_payload_is_clean_utf8(wb):
    """指挥台聚合响应同样不能出现替换字符（任务标题/活动摘要都是中文）。"""
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "指挥台·中文", "label": "指挥台"}).json()["id"]
    client.post(f"/api/projects/{pid}/quick-task", json={"text": "写一份项目总结报告"})
    wb.board.append_event(
        project_id=pid, kind="task.success", actor="worker", summary="中文活动摘要"
    )

    res = client.get(f"/api/projects/{pid}/dashboard")
    assert res.status_code == 200
    payload = res.json()
    raw = res.content.decode("utf-8")
    assert REPLACEMENT not in raw
    assert "指挥台·中文" in raw
    assert any(task["title"] == "写一份项目总结报告" for task in payload["tasks"])
    assert any(item["summary"] == "中文活动摘要" for item in payload["activity"])


@pytest.mark.parametrize("field", ["title", "brief"])
def test_quicktask_chinese_text_not_mangled(wb, field):
    """quick-task 的中文原话/标题逐字保留（不是首字节被截断的乱码）。"""
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": f"编码-{field}"}).json()["id"]
    text = "帮我把『产品需求文档』整理成表格，输出 需求表.xlsx"
    res = client.post(f"/api/projects/{pid}/quick-task", json={"text": text})
    assert res.status_code == 200
    task = client.get(f"/api/tasks/{res.json()['tasks'][0]['id']}").json()["task"]
    expected = "帮我把『产品需求文档』整理成表格，输出 需求表.xlsx"
    assert task[field] == expected, task[field]
