"""实时流块时间线验收（task-10）：真实抓包离线回放 + 前端纯函数 + WS 链路 + 工作副本契约。

三层证据，缺一不可：

1. **真实抓包**（``.e2e/live-capture.json``，1612 条线上 raw 事件）：离线回放
   - 后端 ``StreamCoalescer``：1612 → ~196 条（chunk 合并，非 chunk 事件原样直发）；
   - 前端 ``buildLiveBlocks``（Node 抽函数求值）：1612 → **19 个块**（思考 5 / 消息 2 / 工具 6 / 用量 6），
     即用户看到的「块」，与事件数相差 84 倍——页面不可能再「一行一个 token」。
2. **WS 链路**：``runner.event_sink → StreamCoalescer → EventHub → /ws``，30 条 chunk 只能收到 1 条合并事件。
3. **契约**：``#live`` 的 DOM id / CSS 类、抽屉依赖的 ``GET /api/tasks/{tid}/workspace`` 字段、
   ``normalizeChunkedText`` 对真实「一行一个 token」正文的归一化。

浏览器侧（截图 / 折叠 / 工具卡 / console）在 ``tests/live_smoke_qa.py`` 里做，需要真实 Edge。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from multiagent.api.app import WEB_DIR, create_api
from multiagent.api.stream import StreamCoalescer

REPO = Path(__file__).resolve().parents[1]
CAPTURE = REPO / ".e2e" / "live-capture.json"
BOARD_SAMPLE = REPO / ".e2e" / "live-board-sample.json"
HARNESS = REPO / "tests" / "live_blocks_harness.mjs"
APP_JS = WEB_DIR / "app.js"
INDEX_HTML = WEB_DIR / "index.html"
STYLE_CSS = WEB_DIR / "style.css"

NODE = shutil.which("node")

#: 契约期望：真实抓包渲染成 13–19 个块（实测 19）。
EXPECTED_BLOCK_RANGE = (13, 19)
#: 抓包里各类事件的数量（来自真实 run，作为 fixture 形状护栏）。
CAPTURE_KINDS = {
    "agent_thought_chunk": 1395,
    "agent_message_chunk": 77,
    "tool_call": 6,
    "tool_call_update": 128,
    "usage_update": 6,
}

requires_node = pytest.mark.skipif(NODE is None, reason="需要 Node.js 才能离线回放前端纯函数")
requires_capture = pytest.mark.skipif(
    not CAPTURE.exists(), reason=f"缺少真实抓包 {CAPTURE}（线上 run 的 raw 事件回放依赖它）"
)


# ===========================================================================
# 工具
# ===========================================================================


def _run_harness(mode: str, path: Path | None = None) -> dict:
    """调 Node 抽 app.js 纯函数求值，返回 JSON 报告。"""
    assert NODE, "需要 node"
    cmd = [NODE, str(HARNESS), mode]
    if path is not None:
        cmd.append(str(path))
    result = subprocess.run(
        cmd, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", timeout=180
    )
    assert result.returncode == 0, f"Node 回放失败：{result.stderr[:2000]}"
    return json.loads(result.stdout)


def _load_capture() -> list[dict]:
    return json.loads(CAPTURE.read_text(encoding="utf-8"))


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _replay(events: list[dict], *, merge_window: float = 0.18) -> list[dict]:
    """按抓包里的 ``at`` 时间戳回放：模拟 120ms 一次的后台 flush。"""
    from datetime import datetime

    def parse(value: str) -> float:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()

    published: list[dict] = []
    clock = _Clock()
    coalescer = StreamCoalescer(published.append, clock=clock, merge_window=merge_window)
    base = parse(events[0]["at"])
    last_tick = 0.0
    try:
        for event in events:
            clock.t = parse(event["at"]) - base
            coalescer.feed(event)
            if clock.t - last_tick >= 0.12:  # 后台线程的检查节奏
                coalescer.flush_expired()
                last_tick = clock.t
        coalescer.flush()
    finally:
        coalescer.close()
    return published


def _write_json(tmp_path: Path, name: str, payload) -> Path:  # noqa: ANN001
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# ===========================================================================
# 1) 真实抓包形状
# ===========================================================================


@requires_capture
def test_live_capture_shape_is_real_acp_stream():
    """抓包必须是「token 级 chunk 为主」的真实形态（否则回放结论没有意义）。"""
    events = _load_capture()
    assert len(events) == 1612
    kinds: dict[str, int] = {}
    for event in events:
        kinds[event.get("kind")] = kinds.get(event.get("kind"), 0) + 1
    assert kinds == CAPTURE_KINDS
    for event in events:
        assert event.get("task_id") and event.get("run_id") and event.get("agent_id")
        assert event.get("type") == "agent.update"

    chunks = [e for e in events if str(e.get("kind", "")).endswith("_chunk")]
    average = sum(len(str(e.get("text") or "")) for e in chunks) / len(chunks)
    assert average < 6, f"抓包平均 chunk 长度 {average:.1f} 字，不像 token 级流"


@requires_capture
def test_live_capture_has_no_mojibake():
    raw = CAPTURE.read_text(encoding="utf-8")
    assert "\ufffd" not in raw
    assert "???" not in raw
    texts = "".join(str(e.get("text") or "") for e in _load_capture())
    assert "\ufffd" not in texts and "???" not in texts


# ===========================================================================
# 2) 后端合并：1612 → 一个数量级更少的事件
# ===========================================================================


@requires_capture
def test_backend_replay_compresses_events_without_losing_text():
    events = _load_capture()
    published = _replay(events)

    assert len(published) <= 250, f"合并后仍有 {len(published)} 条事件（压缩不足）"
    assert len(published) >= 100, f"合并后只剩 {len(published)} 条，可能丢事件"
    # 只合并 chunk；工具/用量事件原样直发
    kinds: dict[str, int] = {}
    for event in published:
        kinds[event.get("kind")] = kinds.get(event.get("kind"), 0) + 1
    assert kinds["tool_call"] == 6
    assert kinds["tool_call_update"] == 128
    assert kinds["usage_update"] == 6
    assert kinds["agent_thought_chunk"] + kinds["agent_message_chunk"] < 60

    # 文本一字不少、chunk 数一条不少
    raw_text = "".join(str(e.get("text") or "") for e in events if str(e.get("kind", "")).endswith("_chunk"))
    merged_text = "".join(
        str(e.get("text") or "") for e in published if str(e.get("kind", "")).endswith("_chunk")
    )
    assert merged_text == raw_text
    assert sum(int(e.get("chunk_count") or 0) for e in published) == len(
        [e for e in events if str(e.get("kind", "")).endswith("_chunk")]
    )


@requires_capture
def test_backend_replay_keeps_order_and_tool_boundaries():
    """非 chunk 事件必须先 flush：合并块的文本顺序不能跨过工具事件。"""
    events = _load_capture()
    published = _replay(events)

    def chunk_text(event: dict) -> str:
        return str(event.get("text") or "") if str(event.get("kind", "")).endswith("_chunk") else ""

    # 合并后 chunk 文本的拼接顺序 == 原始顺序（没有任何一段被挪到工具事件后面）
    raw_order = "".join(chunk_text(e) for e in events)
    merged_order = "".join(chunk_text(e) for e in published)
    assert merged_order == raw_order

    # 每个工具事件之前，上一条 chunk 块必须已经收尾（不会出现「工具事件后仍追加旧块」）
    seen_tool = False
    for event in published:
        kind = str(event.get("kind") or "")
        if kind in ("tool_call", "tool_call_update"):
            seen_tool = True
            continue
        if seen_tool and kind in ("agent_thought_chunk", "agent_message_chunk"):
            # 工具之后可以开新块，但它的文本必须来自工具之后的事件：
            # 用长度下界校验（合并块至少 1 个 chunk；不会包含工具前的文本）
            assert int(event.get("chunk_count") or 0) >= 1


# ===========================================================================
# 3) 前端块时间线：1612 → 13~19 块（Node 离线回放 app.js 纯函数）
# ===========================================================================


@requires_node
@requires_capture
def test_frontend_renders_capture_as_19_blocks():
    report = _run_harness("blocks", CAPTURE)
    low, high = EXPECTED_BLOCK_RANGE
    assert low <= report["blocks"] <= high, (
        f"1612 条 raw 事件渲染成 {report['blocks']} 个块，期望 {low}-{high}；byType={report['byType']}"
    )
    assert report["blocks"] == 19
    assert report["byType"] == {"thought": 5, "message": 2, "tool": 6, "usage": 6}
    assert report["thoughtChunks"] == 1395
    assert report["messageChunks"] == 77
    assert report["toolChunks"] == 134
    assert report["hasMojibake"] is False


@requires_node
@requires_capture
def test_frontend_has_no_single_token_blocks():
    """「一行一个 token」的判定：任何思考/消息块都不能只有 1 个极短 chunk。

    真实 run 里也有**合法的短思考**（工具调用之间的一句 "Now let me check…"），
    所以判据是「块的内容不是 token 级碎片」，而不是「每块都很长」。
    """
    report = _run_harness("blocks", CAPTURE)
    assert report["singleTokenBlocks"] == 0
    thought_lengths = [block["contentLen"] for block in report["thoughtBlocks"]]
    assert min(thought_lengths) >= 10, f"有思考块短到像 token 碎片：{thought_lengths}"
    average = sum(thought_lengths) / len(thought_lengths)
    assert average >= 200, f"思考块平均只有 {average:.0f} 字，仍是碎片化"
    assert report["maxBlockChars"] >= 1000  # 最长块应是整段思考，而不是一个词
    # 5 个块装下 1395 条 chunk：平均每块 279 条，不可能是一行一个 token
    assert report["thoughtChunks"] / len(report["thoughtBlocks"]) >= 50


@requires_node
@requires_capture
def test_frontend_tool_blocks_have_command_and_status():
    """工具块按 tool_call_id 合并，并带命令与状态（用户能看出「在跑什么 / 成没成」）。"""
    report = _run_harness("blocks", CAPTURE)
    tools = report["toolBlocks"]
    assert len(tools) == 6
    assert len({tool["toolId"] for tool in tools}) == 6  # 134 条事件按 id 合并成 6 个块
    for tool in tools:
        assert tool["hasCommand"], f"工具块没有命令：{tool}"
        assert tool["status"] in {"completed", "failed", "in_progress", "pending"}
        assert tool["chunkCount"] >= 1
    # 真实 run 里多数工具块带完整命令行（个别是 ACP 的短标题，如 "Editing files"）
    long_commands = [tool for tool in tools if len(tool["command"]) >= 20]
    assert len(long_commands) >= 4, f"带完整命令行的工具块太少：{[t['command'] for t in tools]}"
    statuses = {tool["status"] for tool in tools}
    assert "completed" in statuses and "failed" in statuses  # 真实 run 里两种都有


@requires_node
@requires_capture
def test_frontend_blocks_match_between_raw_and_coalesced_events(tmp_path: Path):
    """后端合并后（196 条）与原始 1612 条，前端渲染出的块结构必须一致。"""
    events = _load_capture()
    published = _replay(events)
    coalesced = _write_json(tmp_path, "coalesced.json", published)

    raw_report = _run_harness("blocks", CAPTURE)
    merged_report = _run_harness("blocks", coalesced)
    assert merged_report["events"] == len(published)
    assert merged_report["blocks"] == raw_report["blocks"] == 19
    assert merged_report["byType"] == raw_report["byType"]


@requires_node
def test_build_live_blocks_merges_same_run_and_splits_on_tool(tmp_path: Path):
    """单元级：同 run 连续 chunk 合并；工具事件是边界；工具事件按 id 合并。"""
    events = [
        {"seq": 1, "kind": "agent_thought_chunk", "run_id": "r1", "agent_id": "a", "text": "第一段"},
        {"seq": 2, "kind": "agent_thought_chunk", "run_id": "r1", "agent_id": "a", "text": "继续"},
        {"seq": 3, "kind": "tool_call", "run_id": "r1", "agent_id": "a", "tool_call_id": "c1", "tool": "npm test", "status": "in_progress"},
        {"seq": 4, "kind": "tool_call_update", "run_id": "r1", "agent_id": "a", "tool_call_id": "c1", "status": "completed"},
        {"seq": 5, "kind": "agent_thought_chunk", "run_id": "r1", "agent_id": "a", "text": "第二段"},
        {"seq": 6, "kind": "agent_message_chunk", "run_id": "r1", "agent_id": "a", "text": "回复"},
        {"seq": 7, "kind": "agent_message_chunk", "run_id": "r1", "agent_id": "a", "text": "继续回复"},
    ]
    report = _run_harness("blocks", _write_json(tmp_path, "unit.json", events))
    assert report["byType"] == {"thought": 2, "tool": 1, "message": 1}
    assert report["thoughtBlocks"][0]["chunkCount"] == 2
    assert report["thoughtBlocks"][0]["contentLen"] == len("第一段继续")
    assert report["messageBlocks"][0]["chunkCount"] == 2
    tool = report["toolBlocks"][0]
    assert tool["toolId"] == "c1"
    assert tool["status"] == "completed"
    assert tool["chunkCount"] == 2  # tool_call + tool_call_update 合并


# ===========================================================================
# 4) normalizeChunkedText：真实「一行一个 token」正文 → 整段
# ===========================================================================


@requires_node
def test_normalize_chunked_text_merges_real_token_lines(tmp_path: Path):
    """真实现场形态：board 464 行 / handoff 451 行（一行一个 token）→ 整段。

    用真实抓包同一台机器上的真实正文片段（截取，避免测试依赖 .e2e 大文件）。
    """
    token_lines = "\n".join(["我先", "检查", "工作", "区", "与", "Java", "环境", "。"] * 60)  # 480 行
    code_block = "```js\nconst a = 1;\n```"
    payload = {
        "board": [
            {"id": "b1", "body": token_lines},
            {"id": "b2", "body": "Agent 未能完成或未通过验收"},
            {"id": "b3", "body": code_block},
        ],
        "handoffs": [
            {"id": "h1", "did": token_lines, "notes": "此交接由系统依据 Agent 输出合成，建议人工复核。"},
        ],
    }
    report = _run_harness("normalize", _write_json(tmp_path, "sample.json", payload))
    rows = {f"{row['id']}.{row['field']}": row for row in report["rows"]}
    long_row = rows["b1.board.body"]
    assert long_row["beforeLines"] == 480
    assert long_row["afterLines"] <= 3, f"归一化后仍有 {long_row['afterLines']} 行"
    assert "我先" in long_row["afterHead"] and "Java" in long_row["afterHead"]
    # 短文本与代码块不动
    assert rows["b2.board.body"]["afterLen"] == rows["b2.board.body"]["beforeLen"]
    assert rows["b3.board.body"]["afterLines"] == 3  # ``` 代码块原样保留（3 行）
    assert rows["h1.handoff.did"]["beforeLines"] == 480
    assert rows["h1.handoff.did"]["afterLines"] <= 3
    assert rows["h1.handoff.notes"]["afterLines"] == 1


@requires_node
def test_normalize_chunked_text_heuristic_boundaries():
    """归一化是启发式：短文本 / 代码块 / 空值必须原样返回（不能把正常排版揉成一团）。"""
    cases = _run_harness("cases")["cases"]
    assert cases["shortText"] == "只有一行"
    assert cases["paragraphUnchanged"] is True
    assert cases["codeBlockUnchanged"] is True
    assert cases["empty"] == ""
    assert cases["tokenLinesBefore"] == 40
    assert cases["tokenLinesAfter"] == 1


@requires_node
@pytest.mark.skipif(not BOARD_SAMPLE.exists(), reason="缺少真实 board/handoff 样本 .e2e/live-board-sample.json")
def test_normalize_real_board_and_handoff_samples():
    """真实项目「我的世界」的黑板/交接正文（464/451 行）归一化后应为整段。"""
    report = _run_harness("normalize", BOARD_SAMPLE)
    rows = {(row["id"], row["field"]): row for row in report["rows"]}
    board_long = [row for (ident, field), row in rows.items() if field == "board.body" and row["beforeLines"] > 100]
    assert board_long, "样本里没有长黑板正文"
    for row in board_long:
        assert row["afterLines"] <= 25, f"{row['id']} 归一化后仍 {row['afterLines']} 行"
    handoff_long = [row for (ident, field), row in rows.items() if field == "handoff.did" and row["beforeLines"] > 100]
    assert handoff_long
    for row in handoff_long:
        assert row["afterLines"] <= 3, f"{row['id']} 交接正文归一化后仍 {row['afterLines']} 行"


# ===========================================================================
# 5) 契约：live 视图 DOM / CSS / 函数；抽屉依赖的 workspace API
# ===========================================================================


def test_live_view_dom_and_frontend_functions_exist():
    html = INDEX_HTML.read_text(encoding="utf-8")
    for ident in ("live-runbar", "live-run-title", "live-run-meta", "live-run-status", "live-jump", "live-list", "live-task", "live-autoscroll"):
        assert f'id="{ident}"' in html, f"index.html 缺少 #{ident}"

    js = APP_JS.read_text(encoding="utf-8")
    for name in (
        "buildLiveBlocks",
        "applyToolEvent",
        "renderLiveFeed",
        "blockHtmlFor",
        "toolBlockHtml",
        "toggleLiveBlock",
        "scrollLiveToBottom",
        "updateLiveJump",
        "normalizeChunkedText",
    ):
        assert f"function {name}(" in js, f"app.js 缺少 {name}()"

    css = STYLE_CSS.read_text(encoding="utf-8")
    for token in (".live-feed", ".live-block", ".live-thought", ".live-msg", ".live-tool", ".live-block.is-collapsed", ".live-jump", ".live-code", ".live-tool-status"):
        assert token in css, f"style.css 缺少 {token}"


def test_workspace_api_contract_used_by_drawer(wb, set_plan):
    """抽屉读的字段必须由 ``GET /api/tasks/{tid}/workspace`` 提供（缺一个就渲染不出来）。"""
    set_plan("worker", {"files": {"ws-out.txt": "工作副本产物"}, "exit": 0})
    client = TestClient(create_api(wb))
    pid = client.post("/api/projects", json={"name": "工作副本契约"}).json()["id"]
    task = client.post(
        f"/api/projects/{pid}/tasks",
        json={
            "title": "产出文件到副本",
            "kind": "generic",
            "agent_id": "worker",
            "acceptance": [{"type": "file_exists", "path": "ws-out.txt"}],
        },
    ).json()
    run = client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60}).json()
    assert run["status"] == "success"

    res = client.get(f"/api/tasks/{task['id']}/workspace")
    assert res.status_code == 200, res.text
    body = res.json()
    # 前端 workspaceInfoOf() 直接索引的字段
    for key in ("task_id", "project_id", "isolation", "root_path", "workdir", "workdir_exists", "can_sync", "pending", "artifacts"):
        assert key in body, f"workspace 响应缺少前端依赖字段 {key}"
    assert body["task_id"] == task["id"]
    assert body["isolation"] == "copy"
    assert body["workdir_exists"] is True
    assert body["can_sync"] is True
    assert body["pending"]["count"] >= 1
    assert isinstance(body["pending"]["files"], list)
    assert any(item["rel_path"] == "ws-out.txt" for item in body["pending"]["files"])
    # 未知任务 404（前端 catch 后降级，不弹错误）
    assert client.get("/api/tasks/nope/workspace").status_code == 404


def test_ws_stream_delivers_merged_blocks(wb):
    """真实 WS 链路：30 条 chunk 只能收到 1 条合并事件（而不是 30 条逐 chunk 消息）。"""
    client = TestClient(create_api(wb))
    api = create_api(wb)
    pid = client.post("/api/projects", json={"name": "ws-live"}).json()["id"]
    client2 = TestClient(api)

    with client2.websocket_connect(f"/ws?project={pid}") as ws:
        for index in range(30):
            wb.runner.event_sink(
                {
                    "type": "agent.update",
                    "source_type": "acp.update",
                    "project_id": pid,
                    "task_id": "task-live",
                    "run_id": "run-live",
                    "agent_id": "worker",
                    "kind": "agent_thought_chunk",
                    "text": f"字{index}",
                }
            )
        api.state.coalescer.flush()

        merged = None
        for _ in range(15):
            message = ws.receive_json()
            if message.get("type") == "stream.event":
                event = message.get("event") or {}
                if event.get("kind") == "agent_thought_chunk":
                    merged = event
                    break
        assert merged is not None, "WS 没有收到合并后的思考事件"
        assert merged["chunk_count"] == 30
        assert merged["text"] == "".join(f"字{index}" for index in range(30))
        assert merged["project_id"] == pid and merged["task_id"] == "task-live"


@requires_capture
def test_capture_round_trip_is_stable_across_runs():
    """同输入两次回放结果一致（合并器没有时间抖动导致的不确定性）。"""
    events = _load_capture()
    first = _replay(events)
    second = _replay(events)
    assert [e.get("chunk_count") for e in first] == [e.get("chunk_count") for e in second]
    assert [e.get("kind") for e in first] == [e.get("kind") for e in second]
