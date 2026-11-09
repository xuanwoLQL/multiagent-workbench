"""假 ACP 服务端（**流式版**）：连续发大量 token 级 chunk，用于验收实时流分块渲染。

与 ``multiagent/testing/fake_acp.py`` 的区别：那个只发 2 条消息，用来验协议；
这个专门制造**线上真实形态**的事件量（几十~上百条 ``agent_thought_chunk`` /
``agent_message_chunk`` + 工具调用进度 + 用量），让 ``StreamCoalescer`` 与前端
「实时块时间线」在**真实 WebSocket 链路**上被压测，而不是只在单测里。

行为（一次 ``session/prompt``）：

1. 发 ``thought_chunks`` 条思考 chunk（默认 120 条，每条 6~10 个中文字，间隔 15ms）；
2. 发一次 ``tool_call``（带 toolCallId / title / rawInput / status=in_progress）；
3. 发 ``message_chunks`` 条消息 chunk（默认 40 条，间隔 15ms）；
4. 发 ``tool_call_update``（status=completed + rawOutput + content 文本块）；
5. 发 ``usage_update``；
6. 在工作目录写 ``实时流产物.md``（让 file_exists 验收通过）；
7. 回 ``end_turn``。

可选环境变量（都带默认值，正常验收不用改）：

* ``FAKE_STREAM_THOUGHTS`` / ``FAKE_STREAM_MESSAGES``：chunk 条数；
* ``FAKE_STREAM_DELAY_MS``：每条 chunk 的间隔毫秒（默认 15）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

PROTOCOL_VERSION = 1
SESSION_ID = "fake-stream-session-1"


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read_message() -> dict | None:
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


def notify(update: dict) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION_ID, "update": update},
        }
    )


def chunk(kind: str, text: str) -> None:
    notify({"sessionUpdate": kind, "content": {"type": "text", "text": text}})


def main() -> int:
    thoughts = int(os.environ.get("FAKE_STREAM_THOUGHTS", "120"))
    messages = int(os.environ.get("FAKE_STREAM_MESSAGES", "40"))
    delay = max(0.0, float(os.environ.get("FAKE_STREAM_DELAY_MS", "15")) / 1000.0)

    while True:
        message = read_message()
        if message is None:
            break
        method = message.get("method")
        msg_id = message.get("id")

        if method == "initialize":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "agentInfo": {"name": "fake-acp-stream", "version": "0.0.1"},
                        "agentCapabilities": {},
                    },
                }
            )
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": SESSION_ID}})
        elif method == "session/prompt":
            # 1) 大量思考 chunk：线上实测平均 4 字/条，这里 6~10 字，形态一致
            for index in range(thoughts):
                chunk(
                    "agent_thought_chunk",
                    f"思考片段{index:03d}：检查工作区并决定下一步，" if index % 3 == 0 else f"继续分析第{index:03d}步的实现细节，",
                )
                if delay:
                    time.sleep(delay)

            # 2) 工具调用开始
            notify(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call_stream_1",
                    "title": "运行 PowerShell：Get-ChildItem -Recurse",
                    "kind": "execute",
                    "status": "in_progress",
                    "rawInput": {"command": "Get-ChildItem -Recurse"},
                    "locations": [{"path": "src"}],
                }
            )
            time.sleep(delay)

            # 3) 大量消息 chunk
            for index in range(messages):
                chunk("agent_message_chunk", f"消息片段{index:03d}：" if index % 4 == 0 else f"正在生成第{index:03d}段输出，")
                if delay:
                    time.sleep(delay)

            # 4) 工具完成（带输出与 content 文本块）
            notify(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "call_stream_1",
                    "status": "completed",
                    "rawOutput": {"exit": 0, "stdout": "共 12 个文件"},
                    "content": [{"type": "text", "text": "共 12 个文件"}],
                }
            )

            # 5) 用量
            notify(
                {
                    "sessionUpdate": "usage_update",
                    "usage": {"inputTokens": 120, "outputTokens": 340, "totalTokens": 460},
                }
            )

            # 6) 产物（验收用）
            try:
                Path.cwd().joinpath("实时流产物.md").write_text(
                    "# 实时流产物\n\n由 fake-acp-stream 生成，用于验收实时流分块渲染。\n",
                    encoding="utf-8",
                )
            except OSError:
                pass

            # 7) 结束
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
        elif method == "session/cancel":
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "session/close":
            if msg_id is not None:
                send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        else:
            if msg_id is not None:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"未知方法 {method}"},
                    }
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
