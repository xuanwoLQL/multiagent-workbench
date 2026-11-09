"""假 ACP 服务端：`python -m multiagent.testing.fake_acp`。

实现最小可用的 ACP v1（initialize / session/new / session/prompt /
session/cancel / session/close），按 ``FAKE_ACP_PLAN`` 操作工作目录，
可选地发一次 ``session/request_permission`` 来验证客户端的权限决策链路。

用真实 ACP 协议驱动，因此可以完整验证适配器而不消耗任何模型额度。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .fake_plan import apply_plan, read_plan

PROTOCOL_VERSION = 1
SESSION_ID = "fake-session-1"


def _force_utf8_stdio() -> None:
    """ACP is a UTF-8 protocol; Windows CI may default stdout to cp1252."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read_message() -> dict[str, Any] | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def notify(method: str, params: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


def main() -> int:
    _force_utf8_stdio()
    plan = read_plan()
    while True:
        message = read_message()
        if message is None:
            break
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "agentInfo": {"name": "fake-acp", "version": "0.0.1"},
                        "agentCapabilities": {},
                    },
                }
            )
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": SESSION_ID}})
        elif method == "session/prompt":
            allowed = True
            if plan.get("permission"):
                allowed = _request_permission(plan)
            notify(
                "session/update",
                {
                    "sessionId": SESSION_ID,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "开始执行任务…"},
                    },
                },
            )
            if allowed:
                result = apply_plan(plan, Path.cwd())
                text = f"执行完成：写入 {result['written']}，删除 {result['deleted']}"
            else:
                text = "权限被拒绝，未做任何改动"
            notify(
                "session/update",
                {
                    "sessionId": SESSION_ID,
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": text},
                    },
                },
            )
            notify(
                "session/update",
                {
                    "sessionId": SESSION_ID,
                    "update": {
                        "sessionUpdate": "usage_update",
                        "usage": {"inputTokens": 12, "outputTokens": 34, "totalTokens": 46},
                    },
                },
            )
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "session/close":
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"未知方法 {method}"}})
    return 0


def _request_permission(plan: dict[str, Any]) -> bool:
    """发一次权限请求并阻塞等待客户端回复。"""
    token = "req-1"
    send(
        {
            "jsonrpc": "2.0",
            "id": token,
            "method": "session/request_permission",
            "params": {
                "sessionId": SESSION_ID,
                "toolCall": {
                    "toolCallId": "call-1",
                    "title": plan.get("permission_title", "写入文件"),
                    "kind": plan.get("permission_kind", "edit"),
                    "rawInput": plan.get("permission_detail", ""),
                },
                "options": [
                    {"optionId": "allow-once", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        }
    )
    while True:
        reply = read_message()
        if reply is None:
            return False
        if reply.get("id") == token and "method" not in reply:
            outcome = (reply.get("result") or {}).get("outcome") or {}
            return outcome.get("outcome") == "selected"


if __name__ == "__main__":
    raise SystemExit(main())
