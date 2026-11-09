"""实时流分片合并（StreamCoalescer）与 ACP 工具事件字段测试。

背景（线上实测 ``.e2e/live-capture.json``，1500 条事件 / 约 6 秒）：
    1215 条 ``agent_thought_chunk`` 平均 4.2 字、35 条 ``agent_message_chunk`` 平均 1.7 字、
    244 条 ``tool_call_update`` 只有 ``tool_call_id``、3 条 ``tool_call`` 只有 tool/status。
直接推给前端 = 一行一个 token + 工具卡是空壳。这里锁定两件事：

1. chunk 按 ``(project_id, task_id, run_id, agent_id, kind)`` 合并成块（180ms 窗口 /
   120ms 超时 flush），事件量降一个数量级，且**不同 run / 不同 kind 绝不串**；
2. ``tool_call`` / ``tool_call_update`` 事件带齐 title/kind/status/rawInput/rawOutput/
   content/locations，前端才能渲染工具卡。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from multiagent.agents.acp_adapter import AcpAdapter, _MessageBuffer
from multiagent.agents.base import AgentRequest
from multiagent.api.app import EventHub, create_api
from multiagent.api.stream import CHUNK_KINDS, StreamCoalescer

# ===========================================================================
# 工具：假时钟 / 事件收集器 / 事件构造
# ===========================================================================


class _Clock:
    """可手动推进的单调时钟（避免测试依赖真实时间）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Sink:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    @property
    def texts(self) -> list[str]:
        return [str(event.get("text") or "") for event in self.events]

    @property
    def kinds(self) -> list[str]:
        return [str(event.get("kind") or "") for event in self.events]


def _chunk(
    text: str = "x",
    *,
    kind: str = "agent_thought_chunk",
    run_id: str = "run-1",
    **extra: Any,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "agent.update",
        "source_type": "acp.update",
        "project_id": "proj-1",
        "task_id": "task-1",
        "agent_id": "codex",
        "kind": kind,
        "run_id": run_id,
        "text": text,
    }
    event.update(extra)
    return event


def _acp_events(update: dict[str, Any], *, run_id: str = "run-1") -> list[dict[str, Any]]:
    """把一条 ACP ``session/update`` 喂给适配器，收集它发出的 on_event 载荷。"""
    captured: list[dict[str, Any]] = []
    request = AgentRequest(
        prompt="p",
        workspace=Path("."),
        run_dir=Path("."),
        run_id=run_id,
        on_event=captured.append,
    )
    adapter = AcpAdapter({"id": "codex", "protocol": "acp"})
    adapter._handle_update({"update": update}, [], [], [], request, [])
    return captured


# ===========================================================================
# 1) 合并 / 窗口 / 超时 / 顺序
# ===========================================================================


def test_chunk_kinds_are_the_two_acp_chunk_kinds():
    assert CHUNK_KINDS == {"agent_message_chunk", "agent_thought_chunk"}


def test_coalescer_merges_chunks_same_key():
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, clock=clock)
    try:
        for text in ("游", "戏", "开始"):
            coalescer.feed(_chunk(text))
            clock.advance(0.05)
        assert sink.events == [], "窗口内不应发布，否则又变成一行一个 token"
        assert coalescer.pending_count == 1

        assert coalescer.flush() == 1
        assert coalescer.pending_count == 0

        event = sink.events[0]
        assert event["text"] == "游戏开始"
        assert event["chunk_count"] == 3
        # 原字段全部保留
        assert event["type"] == "agent.update"
        assert event["source_type"] == "acp.update"
        assert event["kind"] == "agent_thought_chunk"
        assert event["project_id"] == "proj-1"
        assert event["task_id"] == "task-1"
        assert event["agent_id"] == "codex"
        assert event["run_id"] == "run-1"
        # 新增字段
        assert isinstance(event["started_at"], str) and event["started_at"]
        assert isinstance(event["at"], str) and event["at"]
        # 上游没有 seq → 不伪造，交给 EventHub 在 publish 时统一编号
        assert "seq" not in event
    finally:
        coalescer.close()


def test_coalescer_starts_new_block_after_merge_window():
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, clock=clock)
    try:
        coalescer.feed(_chunk("A"))
        clock.advance(0.10)
        coalescer.feed(_chunk("B"))  # 窗口内 → 合并
        assert coalescer.pending_count == 1

        clock.advance(0.20)  # 距块开始 300ms > 180ms
        coalescer.feed(_chunk("C"))  # 先 flush 旧块，再开新块
        assert len(sink.events) == 1
        assert sink.events[0]["text"] == "AB"
        assert sink.events[0]["chunk_count"] == 2

        coalescer.flush()
        assert sink.texts == ["AB", "C"]
        assert [event["chunk_count"] for event in sink.events] == [2, 1]
    finally:
        coalescer.close()


def test_background_timer_flushes_stale_pending():
    """后台线程每 120ms 检查，把「超时」的 pending 落地（流停止时也能及时显示）。"""
    sink = _Sink()
    coalescer = StreamCoalescer(sink, merge_window=0.05, flush_interval=0.02)
    try:
        coalescer.feed(_chunk("后台"))
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sink.events:
            time.sleep(0.01)
        assert sink.events, "超时 pending 没有被后台线程 flush"
        assert sink.events[0]["text"] == "后台"
        assert coalescer.pending_count == 0
    finally:
        coalescer.close()


def test_different_runs_do_not_mix_and_keep_arrival_order():
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, clock=clock)
    try:
        coalescer.feed(_chunk("A1", run_id="run-A"))
        coalescer.feed(_chunk("B1", run_id="run-B"))
        coalescer.feed(_chunk("A2", run_id="run-A"))
        coalescer.feed(_chunk("B2", run_id="run-B"))
        assert coalescer.pending_count == 2

        coalescer.flush()
        assert [(event["run_id"], event["text"]) for event in sink.events] == [
            ("run-A", "A1A2"),
            ("run-B", "B1B2"),
        ]
        assert [event["chunk_count"] for event in sink.events] == [2, 2]
    finally:
        coalescer.close()


def test_different_kinds_do_not_mix():
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, clock=clock)
    try:
        coalescer.feed(_chunk("思考", kind="agent_thought_chunk"))
        coalescer.feed(_chunk("回答", kind="agent_message_chunk"))
        coalescer.feed(_chunk("更多思考", kind="agent_thought_chunk"))
        assert coalescer.pending_count == 2
        coalescer.flush()
        assert [(event["kind"], event["text"]) for event in sink.events] == [
            ("agent_thought_chunk", "思考更多思考"),
            ("agent_message_chunk", "回答"),
        ]
    finally:
        coalescer.close()


def test_non_chunk_event_flushes_pending_first():
    """工具卡 / 任务事件必须排在它之前的思考块后面（否则时间线错乱）。"""
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, clock=clock)
    try:
        coalescer.feed(_chunk("思考中"))
        tool_event = {
            "type": "agent.update",
            "source_type": "acp.update",
            "kind": "tool_call",
            "project_id": "proj-1",
            "task_id": "task-1",
            "agent_id": "codex",
            "run_id": "run-1",
            "tool_call_id": "call-1",
            "tool_title": "执行命令",
        }
        coalescer.feed(tool_event)

        assert sink.kinds == ["agent_thought_chunk", "tool_call"]
        assert sink.events[0]["text"] == "思考中"
        assert sink.events[1]["tool_title"] == "执行命令"
        assert sink.events[1] is not tool_event, "发布出去的是副本，调用方后续改动不能污染总线"

        coalescer.feed(_chunk("继续"))
        coalescer.flush()
        assert sink.texts[2:] == ["继续"]
    finally:
        coalescer.close()


def test_max_text_chars_flushes_eagerly():
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, max_text_chars=10, clock=clock)
    try:
        coalescer.feed(_chunk("12345"))
        assert sink.events == []
        coalescer.feed(_chunk("67890"))  # 累计 10 字 → 立即 flush
        assert len(sink.events) == 1
        assert sink.events[0]["text"] == "1234567890"
        assert sink.events[0]["chunk_count"] == 2
        assert coalescer.pending_count == 0
    finally:
        coalescer.close()


def test_merged_event_uses_last_at_and_seq():
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, clock=clock)
    try:
        coalescer.feed(_chunk("a", at="2026-01-01T00:00:00.000Z", seq=11))
        clock.advance(0.01)
        coalescer.feed(_chunk("b", at="2026-01-01T00:00:00.150Z", seq=12))
        coalescer.flush()

        event = sink.events[0]
        assert event["at"] == "2026-01-01T00:00:00.150Z", "at 应取最后一条"
        assert event["seq"] == 12, "seq 应取最后一条"
        assert event["started_at"] == "2026-01-01T00:00:00.000Z", "started_at 应取第一条"
        assert event["text"] == "ab"
    finally:
        coalescer.close()


def test_feed_ignores_non_dict_and_keeps_empty_text_chunks():
    sink = _Sink()
    coalescer = StreamCoalescer(sink, clock=_Clock())
    try:
        coalescer.feed(None)  # type: ignore[arg-type]
        coalescer.feed("nope")  # type: ignore[arg-type]
        assert sink.events == []
        coalescer.feed(_chunk(""))
        coalescer.feed(_chunk(""))
        coalescer.flush()
        assert len(sink.events) == 1
        assert sink.events[0]["chunk_count"] == 2
        assert sink.events[0]["text"] == ""
    finally:
        coalescer.close()


def test_live_capture_volume_is_reduced_by_an_order_of_magnitude():
    """按线上真实分布（1215 条 4.2 字 chunk / 约 6 秒）验证事件量至少降 20 倍。"""
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, clock=clock)
    try:
        for index in range(1215):
            coalescer.feed(_chunk("字" * 4))
            clock.advance(0.005)  # 每 5ms 一条 ≈ 6 秒 1215 条
            if index % 24 == 0:  # 模拟后台线程每 120ms 检查一次
                coalescer.flush_expired()
        coalescer.flush()

        assert sum(event["chunk_count"] for event in sink.events) == 1215
        assert sum(len(event["text"]) for event in sink.events) == 1215 * 4
        assert len(sink.events) <= 60, f"合并后仍有 {len(sink.events)} 条事件，压缩不足"
    finally:
        coalescer.close()


# ===========================================================================
# 2) EventHub 缓冲 + API 装配
# ===========================================================================


def test_event_hub_buffer_is_4000():
    hub = EventHub()
    for index in range(4200):
        hub.publish({"type": "agent.update", "project_id": "p", "kind": "k", "text": str(index)})
    assert len(hub.snapshot("p")) == 4000


def test_api_wires_coalescer_between_runner_and_hub(wb):
    api = create_api(wb)
    coalescer = api.state.coalescer
    hub = api.state.hub
    assert isinstance(coalescer, StreamCoalescer)
    # 事件先由 RunEventStore 合并并落库，API 只订阅合并后的块（不再二次合并）。
    assert coalescer is wb.run_events.coalescer
    assert getattr(wb.runner.event_sink, "__self__", None) is wb.run_events

    try:
        for text in ("一", "二", "三"):
            wb.runner.event_sink(
                {
                    "type": "agent.update",
                    "source_type": "acp.update",
                    "project_id": "proj-x",
                    "task_id": "task-x",
                    "agent_id": "codex",
                    "kind": "agent_thought_chunk",
                    "run_id": "run-x",
                    "text": text,
                }
            )
        assert coalescer.pending_count == 1
        coalescer.flush()

        items = hub.snapshot("proj-x")
        assert len(items) == 1, "三条 chunk 应合并成一条事件"
        merged = items[0]
        assert merged["text"] == "一二三"
        assert merged["chunk_count"] == 3
        assert isinstance(merged["seq"], int)  # EventHub 在 publish 时分配
        assert merged["at"] and merged["started_at"]

        # 非 chunk 事件立即发布，并排在合并块之后
        wb.runner.event_sink(
            {
                "type": "task.event",
                "kind": "task.success",
                "project_id": "proj-x",
                "task_id": "task-x",
                "summary": "完成",
            }
        )
        items = hub.snapshot("proj-x")
        assert [item["kind"] for item in items] == ["agent_thought_chunk", "task.success"]
    finally:
        coalescer.close()


# ===========================================================================
# 3) ACP 工具事件字段补全
# ===========================================================================


def test_tool_call_event_carries_full_fields():
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call-1",
        "title": "执行命令：pytest -q",
        "kind": "execute",
        "status": "in_progress",
        "rawInput": {"command": "pytest -q"},
        "rawOutput": {"stdout": "ok"},
        "content": [
            {"type": "content", "content": {"type": "text", "text": "第一行"}},
            {"type": "content", "content": {"type": "text", "text": "第二行"}},
        ],
        "locations": [{"path": "src/app.py", "line": 12}],
    }
    events = _acp_events(update)
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "tool_call"
    # 兼容字段
    assert event["tool"] == "执行命令：pytest -q"
    # 完整字段（前端渲染工具卡用）
    assert event["tool_title"] == "执行命令：pytest -q"
    assert event["tool_kind"] == "execute"
    assert event["status"] == "in_progress"
    assert event["tool_call_id"] == "call-1"
    assert event["tool_input"] == {"command": "pytest -q"}
    assert event["tool_output"] == {"stdout": "ok"}
    assert event["tool_content"] == ["第一行", "第二行"]
    assert event["locations"] == [{"path": "src/app.py", "line": 12}]


def test_tool_call_update_with_only_id_keeps_id():
    """线上 244 条 tool_call_update 大多只有 tool_call_id —— 不能崩，也不能丢 id。"""
    events = _acp_events({"sessionUpdate": "tool_call_update", "toolCallId": "call-2"})
    assert len(events) == 1
    event = events[0]
    assert event["tool_call_id"] == "call-2"
    assert "tool_title" not in event
    assert "tool_input" not in event
    assert "tool" not in event


def test_tool_fields_accept_snake_case_variants():
    events = _acp_events(
        {
            "sessionUpdate": "tool_call_update",
            "tool_call_id": "call-3",
            "toolName": "read_file",
            "kind": "read",
            "raw_input": {"path": "a.md"},
            "raw_output": "content",
            "content": "纯文本输出",
            "locations": [{"path": "a.md"}],
        }
    )
    event = events[0]
    assert event["tool_call_id"] == "call-3"
    assert event["tool"] == "read_file"  # 没有 title 时回退到 toolName
    assert event["tool_kind"] == "read"
    assert event["tool_input"] == {"path": "a.md"}
    assert event["tool_output"] == "content"
    assert event["tool_content"] == ["纯文本输出"]
    assert event["locations"] == [{"path": "a.md"}]


def test_message_chunk_has_no_tool_fields():
    events = _acp_events(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    )
    event = events[0]
    assert event["kind"] == "agent_message_chunk"
    assert event["text"] == "hi"
    assert not [key for key in event if key.startswith("tool_")]
    assert "status" not in event
    assert "locations" not in event


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("abc", ["abc"]),
        ({"type": "text", "text": "xyz"}, ["xyz"]),
        (
            [
                {"type": "text", "text": "a"},
                "b",
                {"content": {"text": "c"}},
                {"content": "d"},
            ],
            ["a", "b", "c", "d"],
        ),
        ([{"type": "image", "data": "..."}], []),  # 非文本块忽略
        ([], []),
        (None, []),
    ],
)
def test_tool_content_extraction_shapes(content, expected):
    assert AcpAdapter._tool_content({"content": content}) == expected


def test_tool_event_passes_through_coalescer_unmerged():
    """工具事件不是 chunk：即使同 run 也在合并块之后立即发布。"""
    sink = _Sink()
    clock = _Clock()
    coalescer = StreamCoalescer(sink, clock=clock)
    try:
        coalescer.feed(_chunk("思考"))
        for event in _acp_events(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "call-9",
                "title": "写文件",
                "kind": "edit",
                "status": "completed",
            }
        ):
            # 复刻 runner._emit_agent_event 的补字段方式
            payload = dict(event)
            payload.update(
                {
                    "type": "agent.update",
                    "source_type": "acp.update",
                    "project_id": "proj-1",
                    "task_id": "task-1",
                    "agent_id": "codex",
                }
            )
            coalescer.feed(payload)

        assert sink.kinds == ["agent_thought_chunk", "tool_call"]
        assert sink.events[1]["tool_title"] == "写文件"
        assert sink.events[1]["tool_kind"] == "edit"
        assert sink.events[1]["status"] == "completed"
        assert sink.events[1]["tool_call_id"] == "call-9"
    finally:
        coalescer.close()


# ===========================================================================
# 4) ACP 消息聚合：token 级 chunk 不能变成「一列一个字」
# ===========================================================================

_ACP_CHUNK_SERVER = r'''
"""假 ACP 服务端：按 FAKE_CHUNK_MODE 发 token 级 chunk / 完整消息 / 工具边界。

用于回归现场故障：每个 agent_message_chunk 被当成一条独立消息、
stdout 用 "\n".join(messages) → handoff/黑板正文变成「一列一个字」。
"""
import json
import os
import sys

SESSION = "chunk-session-1"
MODE = os.environ.get("FAKE_CHUNK_MODE", "chunks")
SENTENCE = ["我", "们先", "检查", "工作区", "，", "然后", "修复", "空指针", "报错", "。"]


def send(message):
    sys.stdout.buffer.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def update(payload):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": SESSION, "update": payload}})


def chunk(text):
    update({"sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text}})


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
        if MODE == "chunks":
            for part in SENTENCE:
                chunk(part)
        elif MODE == "tool_boundary":
            for part in ["前半", "句。"]:
                chunk(part)
            update({"sessionUpdate": "tool_call", "toolCallId": "call-1",
                    "title": "执行命令", "kind": "execute", "status": "in_progress"})
            for part in ["后半", "句。"]:
                chunk(part)
        elif MODE == "complete":
            for part in ["A", "B"]:
                chunk(part)
            update({"sessionUpdate": "agent_message",
                    "content": {"type": "text", "text": "完整消息"}})
            for part in ["C", "D"]:
                chunk(part)
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
    elif method in ("session/cancel", "session/close"):
        if msg_id is not None:
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
'''

EXPECTED_SENTENCE = "我们先检查工作区，然后修复空指针报错。"


def _run_chunk_acp(tmp_path: Path, mode: str):
    script = tmp_path / f"chunk_acp_{mode}.py"
    script.write_text(_ACP_CHUNK_SERVER, encoding="utf-8")
    agent = {
        "id": "chunkacp",
        "label": "Chunk ACP",
        "protocol": "acp",
        "command": sys.executable,
        "args": [str(script)],
        "env": {"FAKE_CHUNK_MODE": mode},
        "enabled": True,
    }
    return AcpAdapter(agent).run(
        AgentRequest(
            prompt="跑一次任务",
            workspace=tmp_path,
            run_dir=tmp_path / f"run-{mode}",
            timeout_sec=60,
        )
    )


def test_message_buffer_concatenates_chunks_without_newlines():
    buffer = _MessageBuffer()
    for part in ["我", "们", "在", "检查"]:
        buffer.add_message_chunk(part)
    buffer.flush_all()
    assert buffer.messages == ["我们在检查"]
    assert buffer.message_parts == []


def test_message_buffer_flushes_on_boundaries():
    buffer = _MessageBuffer()
    buffer.add_message_chunk("前半")
    buffer.flush_message()  # 工具调用 / 会话结束的边界
    buffer.add_message_chunk("后半")
    buffer.add_message("完整")  # 完整消息：先收尾 chunk，再追加
    buffer.add_message_chunk("尾巴")
    buffer.flush_all()
    assert buffer.messages == ["前半", "后半", "完整", "尾巴"]


def test_message_buffer_aggregates_thoughts_too():
    buffer = _MessageBuffer()
    for part in ["先", "想一", "下"]:
        buffer.add_thought_chunk(part)
    buffer.add_thought("完整想法")
    buffer.flush_all()
    assert buffer.thoughts == ["先想一下", "完整想法"]


def test_acp_chunks_become_one_continuous_message(tmp_path):
    """现场回归：10 个 1-3 字 chunk → stdout/messages 必须是一句连续的话。"""
    result = _run_chunk_acp(tmp_path, "chunks")

    assert result.ok is True, result.error
    assert result.messages == [EXPECTED_SENTENCE]
    assert result.stdout == EXPECTED_SENTENCE
    assert "\n" not in result.stdout, f"stdout 出现逐字换行：{result.stdout!r}"

    # handoff 兜底合成（runner._handoff_from_result）也不能拿到「一列一个字」
    from multiagent.orchestrator.runner import _handoff_from_result

    handoff = _handoff_from_result(result)
    assert handoff["did"] == EXPECTED_SENTENCE
    assert "\n" not in handoff["did"]


def test_acp_tool_call_is_a_message_boundary(tmp_path):
    """工具调用前后的 chunk 不能被拼成一条错位消息。"""
    result = _run_chunk_acp(tmp_path, "tool_boundary")

    assert result.ok is True, result.error
    assert result.messages == ["前半句。", "后半句。"]
    assert result.stdout == "前半句。\n后半句。"


def test_acp_complete_message_flushes_pending_chunks(tmp_path):
    result = _run_chunk_acp(tmp_path, "complete")

    assert result.ok is True, result.error
    assert result.messages == ["AB", "完整消息", "CD"]
    assert result.stdout == "AB\n完整消息\nCD"
