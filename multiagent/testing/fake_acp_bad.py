"""「坏行为」假 ACP 服务端：`python -m multiagent.testing.fake_acp_bad`。

用于对 ACP 适配器做协议健壮性回归：超长行、非 JSON stdout、stderr 洪水、
进程提前退出、JSON-RPC 错误、snake_case/camelCase 两种 update 变体、
usage 重复计数、params 非法、未知方法、取消语义、超时后残留孙进程。

行为由 ``FAKE_ACP_MODE``（逗号分隔）驱动，全部是确定性行为，不消耗任何模型额度：

    pidfile           启动时把自身 pid 写入 FAKE_ACP_PIDFILE
    child_tree        再拉起一个 python 孙进程，pid 写入 FAKE_ACP_CHILD_PIDFILE
    hang              不回复任何请求（用于验证超时整树回收）
    delay=<秒>        回复 session/prompt 前先等待
    noise             stdout 里混入非 JSON 行、空行
    hugeline=<字节>   发一条超长 JSON 行
    stderr_flood=<行> 往 stderr 写大量内容（验证管道不会死锁）
    snake             用 snake_case 发送 session/update
    usage_cumulative  连发 3 次累计型 usage（验证不重复计数）
    usage_used        发 ACP 规范的 used/size 形式 usage
    bad_params        params 为数组（非对象）
    cancel            以 stopReason=cancelled 结束
    rpc_error         以 JSON-RPC error 响应 session/prompt
    die_early         initialize 之后立即退出
    unknown_method    session/new 之后发一个客户端未实现的方法请求
    early_cmd_exit    让父 cmd.exe 立刻退出（把孙进程变孤儿）——由 shim 负责，见测试
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
SESSION_ID = "bad-session-1"


def _force_utf8_stdio() -> None:
    """ACP 是 UTF-8 协议；Windows 上 Python 默认 stdout 走 ANSI 代码页，必须显式改。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _modes() -> list[str]:
    raw = os.environ.get("FAKE_ACP_MODE", "") or ""
    return [m.strip() for m in raw.split(",") if m.strip()]


def _has(prefix: str) -> bool:
    return any(m == prefix or m.startswith(prefix + "=") for m in _modes())


def _opt(name: str, default: str) -> str:
    for mode in _modes():
        if mode.startswith(name + "="):
            return mode.split("=", 1)[1]
    return default


def send(message: dict[str, Any]) -> None:
    data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def send_raw(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def read_message() -> dict[str, Any] | None:
    """按字节读一行再按 UTF-8 解码（ACP/JSON-RPC 是 UTF-8 协议）。"""
    try:
        raw = sys.stdin.buffer.readline()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    line = raw.decode("utf-8", "replace").strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def notify_update(update: dict[str, Any], *, snake: bool = False) -> None:
    if snake:
        notify_update_snake(update)
        return
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": SESSION_ID, "update": update}})


def notify_update_snake(update: dict[str, Any]) -> None:
    """把 update 的顶层键整体换成 snake_case 变体，复刻部分实现的写法。"""
    converted = {_to_snake(k): v for k, v in update.items()}
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"session_id": SESSION_ID, "update": converted}})


def _to_snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def _write_pid(path: str | None) -> None:
    if not path:
        return
    try:
        Path(path).write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass


def _spawn_child_tree() -> None:
    """拉起一个长睡孙进程，用它来断言「整树回收」。"""
    pidfile = os.environ.get("FAKE_ACP_CHILD_PIDFILE")
    if not pidfile:
        return
    code = (
        "import os,sys,time\n"
        "open(sys.argv[1],'w').write(str(os.getpid()))\n"
        "time.sleep(600)\n"
    )
    try:
        subprocess.Popen([sys.executable, "-c", code, pidfile])
    except OSError:
        pass


def _reply_prompt(msg_id: Any, modes: list[str]) -> bool:
    """处理一次 session/prompt；返回 False 表示要退出。"""
    delay = float(_opt("delay", "0") or 0)
    if delay > 0:
        time.sleep(delay)
    if _has("hang"):
        while True:
            time.sleep(1)

    if _has("noise"):
        send_raw("this is not json at all\n")
        send_raw("\n")
        send_raw("[1,2,3]\n")

    snake = _has("snake")
    notify_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "开始"}},
        snake=snake,
    )

    if _has("hugeline"):
        size = int(_opt("hugeline", "2000000") or 2000000)
        notify_update({"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "H" * size}})

    if _has("usage_cumulative"):
        for _ in range(3):
            notify_update({"sessionUpdate": "usage_update",
                           "usage": {"inputTokens": 40, "outputTokens": 60, "totalTokens": 100}},
                          snake=snake)

    if _has("usage_used"):
        notify_update({"sessionUpdate": "usage_update", "used": 1234, "size": 200000,
                       "cost": {"amount": 0.0123, "currency": "USD"}}, snake=snake)

    if _has("usage_cost"):
        notify_update({"sessionUpdate": "usage_update", "used": 4321, "size": 200000,
                       "cost": {"amount": 0.0123, "currency": "USD"}}, snake=snake)

    if _has("stderr_flood"):
        count = int(_opt("stderr_flood", "20000") or 20000)
        blob = ("x" * 200 + "\n") * count
        sys.stderr.write(blob)
        sys.stderr.flush()

    if _has("bad_params"):
        send({"jsonrpc": "2.0", "method": "session/update", "params": [1, 2, 3]})

    notify_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "完成"}},
        snake=snake,
    )

    if _has("rpc_error"):
        send({"jsonrpc": "2.0", "id": msg_id,
              "error": {"code": -32000, "message": "上游模型不可用"}})
        return True
    if _has("cancel"):
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "cancelled"}})
        return True
    send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
    return True


def _log_method(method: str) -> None:
    path = os.environ.get("FAKE_ACP_METHOD_LOG")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(method + "\n")
    except OSError:
        pass


def main() -> int:
    _force_utf8_stdio()
    modes = _modes()
    _write_pid(os.environ.get("FAKE_ACP_PIDFILE"))
    if _has("child_tree"):
        _spawn_child_tree()
    if _has("method_log"):
        log_path = os.environ.get("FAKE_ACP_METHOD_LOG")
        if log_path:
            try:
                Path(log_path).write_text("", encoding="utf-8")
            except OSError:
                pass

    probe_done = not _has("unknown_method")
    probe_file = os.environ.get("FAKE_ACP_PROBE_FILE")
    # 等待 probe 回复期间收到的其它消息先排队，保证「客户端必须回未知请求」可观测
    pending: list[dict[str, Any]] = []

    def next_msg() -> dict[str, Any] | None:
        if pending:
            return pending.pop(0)
        return read_message()

    def await_probe_reply() -> None:
        nonlocal probe_done
        while not probe_done:
            reply = read_message()
            if reply is None:
                return
            if reply.get("method") is None and reply.get("id") == "probe-1":
                if probe_file:
                    try:
                        Path(probe_file).write_text(
                            json.dumps(reply, ensure_ascii=False), encoding="utf-8"
                        )
                    except OSError:
                        pass
                probe_done = True
            else:
                pending.append(reply)

    while True:
        message = next_msg()
        if message is None:
            return 0
        method = message.get("method")
        msg_id = message.get("id")
        if method:
            _log_method(str(method))

        if method is None and msg_id == "probe-1":
            if probe_file:
                try:
                    Path(probe_file).write_text(
                        json.dumps(message, ensure_ascii=False), encoding="utf-8"
                    )
                except OSError:
                    pass
            probe_done = True
            continue

        if method == "initialize":
            if _has("die_early"):
                return 0
            send({"jsonrpc": "2.0", "id": msg_id, "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "agentInfo": {"name": "fake-acp-bad", "version": "0.0.1"},
            }})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": SESSION_ID}})
            if not probe_done:
                send({"jsonrpc": "2.0", "id": "probe-1", "method": "x/unknown", "params": {}})
                await_probe_reply()
        elif method == "session/prompt":
            _reply_prompt(msg_id, modes)
        elif method == "session/cancel":
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "session/close":
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id,
                      "error": {"code": -32601, "message": f"未知方法 {method}"}})


if __name__ == "__main__":
    raise SystemExit(main())
