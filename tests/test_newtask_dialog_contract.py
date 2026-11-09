"""P1-10：新建任务对话框暴露 timeout_sec / max_attempts。

两部分契约：

* 静态契约：读取 ``NewTaskDialog.tsx``，确认高级选项可折叠、可键盘操作、
  字段名正确、范围校验存在，且留空时不会把字段塞进请求体；
* 后端契约：真实调用 ``POST /api/projects/{pid}/tasks``，确认显式值与类型默认
  都会正确落库（前端只是把用户填写的值传给同一个创建链路）。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api

REPO_ROOT = Path(__file__).resolve().parents[1]
DIALOG_PATH = REPO_ROOT / "webapp" / "src" / "features" / "tasks" / "NewTaskDialog.tsx"


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _create_project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_dialog_static_contract() -> None:
    source = DIALOG_PATH.read_text(encoding="utf-8")

    # 高级选项默认折叠、按钮可键盘操作（原生 button）且暴露 aria-expanded。
    assert re.search(r"const \[advanced, setAdvanced\] = useState\(false\)", source)
    assert re.search(r"aria-expanded=\{advanced\}", source)
    assert 'aria-controls="newtask-advanced-options"' in source
    assert "高级选项" in source

    # 后端字段真正被告知给了创建请求。
    assert "timeout_sec" in source
    assert "max_attempts" in source
    assert re.search(
        r"if \(timeoutResult\.value !== null\) body\.timeout_sec = timeoutResult\.value;",
        source,
    )
    assert re.search(
        r"if \(attemptsResult\.value !== null\) body\.max_attempts = attemptsResult\.value;",
        source,
    )

    # 范围校验：30–86400 秒 / 1–10 次，并同时用于输入框 min/max。
    assert re.search(r"TIMEOUT_MIN_SEC\s*=\s*30\b", source)
    assert re.search(r"TIMEOUT_MAX_SEC\s*=\s*86400\b", source)
    assert re.search(r"MAX_ATTEMPTS_MIN\s*=\s*1\b", source)
    assert re.search(r"MAX_ATTEMPTS_MAX\s*=\s*10\b", source)
    for snippet in (
        "min={TIMEOUT_MIN_SEC}",
        "max={TIMEOUT_MAX_SEC}",
        "min={MAX_ATTEMPTS_MIN}",
        "max={MAX_ATTEMPTS_MAX}",
    ):
        assert snippet in source

    # 非法值有内联错误（role=alert）且提交前会阻止。
    assert 'role={timeoutError ? "alert" : undefined}' in source
    assert 'role={maxAttemptsError ? "alert" : undefined}' in source
    assert "if (timeoutResult.error || attemptsResult.error)" in source
    assert "return;" in source

    # 文案说明生效边界。
    assert "不会延长正在跑的 Agent" in source
    assert "只对下一次尝试/运行生效" in source

    # 冒烟脚本依赖的两个行为不能回退。
    assert "autoFocus" in source
    assert "data-autofocus" in source


def test_create_task_persists_explicit_advanced_fields(wb) -> None:
    client = _client(wb)
    pid = _create_project(client, "advanced-fields")

    response = client.post(
        f"/api/projects/{pid}/tasks",
        json={
            "title": "带高级字段的任务",
            "kind": "generic",
            "timeout_sec": 1234,
            "max_attempts": 2,
        },
    )
    assert response.status_code == 200, response.text
    created = response.json()
    assert created["timeout_sec"] == 1234
    assert created["max_attempts"] == 2

    detail = client.get(f"/api/tasks/{created['id']}")
    assert detail.status_code == 200, detail.text
    stored = detail.json()["task"]
    assert stored["timeout_sec"] == 1234
    assert stored["max_attempts"] == 2


def test_create_task_without_advanced_fields_uses_kind_defaults(wb) -> None:
    client = _client(wb)
    pid = _create_project(client, "advanced-defaults")

    response = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "未填高级字段的任务", "kind": "generic"},
    )
    assert response.status_code == 200, response.text
    created = response.json()

    # 留空走类型默认，而不是把对话框里的示例值写死。
    assert created["timeout_sec"] != 1234
    assert created["max_attempts"] != 2
    assert isinstance(created["timeout_sec"], int) and created["timeout_sec"] > 0
    assert isinstance(created["max_attempts"], int) and created["max_attempts"] >= 1

    stored = client.get(f"/api/tasks/{created['id']}").json()["task"]
    assert stored["timeout_sec"] == created["timeout_sec"]
    assert stored["max_attempts"] == created["max_attempts"]


def test_quick_task_persists_advanced_fields(wb) -> None:
    """对话框实际走 quick-task 接口；高级字段也必须在该链路生效。"""
    client = _client(wb)
    pid = _create_project(client, "advanced-quick")

    response = client.post(
        f"/api/projects/{pid}/quick-task",
        json={
            "text": "做一个页面组件",
            "timeout_sec": 1234,
            "max_attempts": 2,
            "start": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    task_id = body["tasks"][0]["id"]
    stored = client.get(f"/api/tasks/{task_id}").json()["task"]
    assert stored["timeout_sec"] == 1234
    assert stored["max_attempts"] == 2
