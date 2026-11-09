"""实时流浏览器验收（task-10）：离线回放 / 真实 WS / 真实桌面服务三种场景。

用法（PowerShell）::

    .\\.venv\\Scripts\\python.exe tests\\live_smoke_qa.py --mode all
    .\\.venv\\Scripts\\python.exe tests\\live_smoke_qa.py --mode replay   # 只用真实抓包回放
    .\\.venv\\Scripts\\python.exe tests\\live_smoke_qa.py --mode live     # 假 Agent 长输出（真实 WS）
    .\\.venv\\Scripts\\python.exe tests\\live_smoke_qa.py --mode real     # 8899 真实桌面服务（只读）

三个场景：

1. **replay**：把 ``.e2e/live-capture.json``（1612 条真实 raw 事件）注入页面的
   ``state.stream``，用真实前端函数渲染 → 断言 19 个块、无逐 chunk 行、思考默认折叠、
   工具块有命令/状态、无乱码、console 0；截图 1832/1440（含折叠/展开两种状态）。
2. **live**：独立服务（默认 8902）+ ``tests/fake_acp_stream.py``（120 思考 chunk + 40 消息
   chunk + 工具调用）跑一个真实任务，浏览器连真实 WS → 断言按块渲染、贴底跟随、
   上滚出现「回到最新」、console 0；同时用独立 WS 客户端统计后端合并效果。
3. **real**：连 ``127.0.0.1:8899``（当前桌面端连的 %APPDATA%\\multiagent-desktop，
   真实项目「我的世界」）→ 只读验证 ``#live`` 可打开、任务抽屉里 ``GET /api/tasks/{tid}/workspace``
   200 / 「待同步 N 个文件」/ 同步按钮存在（**不点同步**，不写用户目录）、黑板/交接正文已整段。

产物：``.e2e/live-*.png``、``.e2e/live-smoke-results.json``。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_smoke_qa import (  # noqa: E402  - 复用 CDP / 服务进程工具
    Cdp,
    E2E,
    REPO,
    free_port,
    http_json,
    http_post,
    kill_edge,
    launch_edge,
    log,
    page_target,
    start_server,
    stop_server,
    wait_cdp_ready,
)

CAPTURE = E2E / "live-capture.json"
RESULTS = E2E / "live-smoke-results.json"
STREAM_AGENT = REPO / "tests" / "fake_acp_stream.py"
SIZES = ((1832, 1010), (1440, 900))
REAL_BASE = "http://127.0.0.1:8899/"

PAGE_HELPERS = """
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
const feedText = () => { const el = document.getElementById('live-list'); return el ? (el.innerText || el.textContent || '') : ''; };
"""


# ---------------------------------------------------------------------------
# 公共：注入脚本 / 截图 / 断言
# ---------------------------------------------------------------------------


def save_shot(payload_b64: str, path: Path) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(base64.b64decode(payload_b64))
    return {"path": str(path.relative_to(REPO)), "bytes": path.stat().st_size}


async def screenshot(cdp: Cdp, path: Path) -> dict:
    result = await cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    payload = (result.get("result") or {}).get("data")
    if not payload:
        return {"path": str(path), "error": "captureScreenshot 无数据"}
    return save_shot(payload, path)


async def set_viewport(cdp: Cdp, size: tuple[int, int]) -> None:
    await cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": size[0], "height": size[1], "deviceScaleFactor": 1, "mobile": False},
    )
    await asyncio.sleep(0.3)


async def wait_boot(cdp: Cdp, *, require_project: bool = True, timeout: float = 40.0) -> bool:
    """等前端 boot 完成：项目选中 + KPI/状态条渲染。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await cdp.evaluate(
            "JSON.stringify({ready: document.readyState,"
            " project: (document.getElementById('project-select')||{}).value || '',"
            " metrics: (document.getElementById('home-metrics')||{}).textContent || '',"
            " status: (document.getElementById('status-ws')||{}).textContent || ''})"
        )
        data = json.loads(value or "{}")
        if data.get("ready") == "complete" and data.get("status"):
            if not require_project or data.get("project"):
                return True
        await asyncio.sleep(0.25)
    return False


async def goto_view(cdp: Cdp, view: str, timeout: float = 15.0) -> bool:
    await cdp.evaluate(f"location.hash = '#{view}'; 'ok'")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await cdp.evaluate(
            "(() => { const el = document.querySelector('.view.is-active'); return el ? el.id : ''; })()"
        )
        if value == f"view-{view}":
            return True
        await asyncio.sleep(0.2)
    return False


async def poll_until(
    cdp: Cdp,
    expression: str,
    predicate,  # noqa: ANN001
    *,
    timeout: float = 30.0,
    interval: float = 0.2,
    await_promise: bool = False,
):
    """轮询页面里的表达式直到 predicate 为真；返回最后一次解析出的 dict。"""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        value = await cdp.evaluate(expression, await_promise=await_promise)
        try:
            data = json.loads(value or "{}")
        except (json.JSONDecodeError, TypeError):
            data = {}
        if isinstance(data, dict):
            last = data
        if isinstance(data, dict) and predicate(data):
            return data
        await asyncio.sleep(interval)
    return last


# ---------------------------------------------------------------------------
# 场景 1：离线回放真实抓包
# ---------------------------------------------------------------------------

REPLAY_STATS_JS = (
    "(() => {"
    + PAGE_HELPERS
    + """
  const feed = document.getElementById('live-list');
  if (!feed) return JSON.stringify({error: 'no-live-list'});
  const blocks = $$('.live-block', feed);
  const events = $$('.live-event', feed);
  const thought = $$('.live-thought', feed);
  const messages = $$('.live-msg', feed);
  const tools = $$('.live-tool', feed);
  const text = feed.innerText || '';
  return JSON.stringify({
    blocks: blocks.length,
    events: events.length,
    thought: thought.length,
    messages: messages.length,
    tools: tools.length,
    rows: blocks.length + events.length,
    collapsedThoughts: $$('.live-thought.is-collapsed', feed).length,
    toggles: $$('.live-toggle', feed).length,
    toolStatuses: $$('.live-tool-status', feed).map((el) => el.textContent.trim()),
    toolCodes: $$('.live-tool .live-code', feed).map((el) => el.textContent.trim()),
    shortRich: $$('.live-rich', feed).filter((el) => (el.textContent || '').trim().length < 5).length,
    textLen: text.length,
    mojibake: text.includes('\\uFFFD') || text.includes('???'),
    firstThoughtLen: thought.length ? (thought[0].innerText || '').length : 0,
  });
})()"""
)


async def replay_flow(debug_port: int, base_url: str, capture: Path) -> dict:
    import websockets

    report: dict = {"ok": False, "steps": {}, "console_errors": [], "exceptions": [], "screenshots": []}
    events = json.loads(capture.read_text(encoding="utf-8"))
    ws_url = page_target(debug_port, base_url)
    async with websockets.connect(ws_url, max_size=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Log.enable")
        await cdp.call("Page.enable")
        await set_viewport(cdp, SIZES[0])
        report["steps"]["boot"] = await wait_boot(cdp)
        report["steps"]["live_view"] = await goto_view(cdp, "live")

        # 注入 1612 条真实事件（分块，避免单条 CDP 消息过大）
        await cdp.evaluate("window.__liveCapture = []; 'ok'")
        step = 200
        for start in range(0, len(events), step):
            payload = json.dumps(events[start: start + step], ensure_ascii=False)
            await cdp.evaluate(f"window.__liveCapture.push(...{payload}); 'ok'")
        injected = await cdp.evaluate(
            "(() => { if (typeof state === 'undefined') return 'no-state';"
            " if (!window.__liveCapture || !window.__liveCapture.length) return 'no-capture';"
            " state.stream = window.__liveCapture.slice();"
            " markLiveDirty(); renderLiveFeed(true);"
            " return String(state.stream.length); })()"
        )
        report["steps"]["injected"] = injected
        await asyncio.sleep(0.6)

        stats = json.loads(await cdp.evaluate(REPLAY_STATS_JS) or "{}")
        report["stats"] = stats
        report["screenshots"].append(await screenshot(cdp, E2E / "live-replay-1832.png"))

        # 思考块展开 / 折叠交互
        toggle = await cdp.evaluate(
            "(() => { const btn = document.querySelector('.live-thought.is-collapsed .live-toggle');"
            " if (!btn) return 'no-collapsed-toggle'; btn.click(); return 'clicked'; })()"
        )
        await asyncio.sleep(0.35)
        expanded = json.loads(
            await cdp.evaluate(
                "(() => { const feed = document.getElementById('live-list');"
                " return JSON.stringify({collapsed: feed.querySelectorAll('.live-thought.is-collapsed').length,"
                " expandedText: (feed.querySelector('.live-thought:not(.is-collapsed) .live-rich')||{}).innerText ?"
                " feed.querySelector('.live-thought:not(.is-collapsed) .live-rich').innerText.length : 0}); })()"
            )
            or "{}"
        )
        report["steps"]["thought_toggle"] = toggle
        report["steps"]["thought_expanded"] = expanded
        report["screenshots"].append(await screenshot(cdp, E2E / "live-thought-expanded-1832.png"))

        # 工具块展开（details open）
        tool_open = await cdp.evaluate(
            "(() => { const det = document.querySelector('.live-tool'); if (!det) return 'no-tool';"
            " det.open = true; det.scrollIntoView({block: 'center'}); return 'opened'; })()"
        )
        await asyncio.sleep(0.35)
        report["steps"]["tool_expand"] = tool_open
        report["screenshots"].append(await screenshot(cdp, E2E / "live-tool-expanded-1832.png"))

        # 任务抽屉：工作副本「待同步 N 个文件 + 同步按钮」（只验证存在，绝不点击 —— 不写工作区）
        project_id = await cdp.evaluate("(document.getElementById('project-select')||{}).value || ''")
        drawer_task = await cdp.evaluate(
            "fetch('/api/projects/' + encodeURIComponent(%s) + '/tasks', {method: 'POST',"
            " headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({title: '工作副本待同步演示', kind: 'generic', agent_id: 'stream',"
            " max_attempts: 1, acceptance: [{type: 'file_exists', path: '缺失产物.md'}]})})"
            ".then((r) => r.json())" % json.dumps(project_id),
            await_promise=True,
        )
        drawer_tid = (drawer_task or {}).get("id")
        await cdp.evaluate(
            "fetch('/api/tasks/' + encodeURIComponent(%s) + '/run', {method: 'POST',"
            " headers: {'Content-Type': 'application/json'}, body: JSON.stringify({wait: false})})"
            ".then((r) => r.json())" % json.dumps(drawer_tid),
            await_promise=True,
        )
        drawer_status = await poll_until(
            cdp,
            "fetch('/api/tasks/' + encodeURIComponent(%s)).then((r) => r.json())"
            ".then((d) => JSON.stringify({status: d.task.status}))" % json.dumps(drawer_tid),
            lambda data: data.get("status") in ("failed", "success", "blocked", "cancelled"),
            timeout=90.0,
            interval=0.5,
            await_promise=True,
        )
        drawer_opened = await cdp.evaluate(
            "openTask(%s).then(() => { const d = document.getElementById('drawer');"
            " return d && d.classList.contains('open') ? 'open' : 'closed'; })" % json.dumps(drawer_tid),
            await_promise=True,
        )
        await asyncio.sleep(0.8)
        drawer_stats = json.loads(await cdp.evaluate(REAL_STATS_JS) or "{}")
        report["steps"]["drawer_sync"] = {
            "task_id": drawer_tid,
            "status": drawer_status,
            "open": drawer_opened,
            **drawer_stats,
        }
        report["screenshots"].append(await screenshot(cdp, E2E / "live-drawer-sync-1832.png"))
        await cdp.evaluate(
            "(() => { const btn = document.getElementById('drawer-close'); if (btn) btn.click(); return 'ok'; })()"
        )
        await asyncio.sleep(0.3)

        # 1440 尺寸再拍一张（折叠态恢复）
        await cdp.evaluate(
            "(() => { document.querySelectorAll('.live-tool').forEach((el) => { el.open = false; });"
            " const feed = document.getElementById('live-list'); if (feed) feed.scrollTop = feed.scrollHeight; return 'ok'; })()"
        )
        await set_viewport(cdp, SIZES[1])
        await asyncio.sleep(0.4)
        report["screenshots"].append(await screenshot(cdp, E2E / "live-replay-1440.png"))
        report["steps"]["stats_1440"] = json.loads(await cdp.evaluate(REPLAY_STATS_JS) or "{}")

        await cdp.pump(0.4)
        report["console_errors"] = cdp.console_errors
        report["exceptions"] = cdp.exceptions

    stats = report.get("stats") or {}
    # 说明：工具块是 <details class="live-block">，所以 .live-block = 思考5 + 消息2 + 工具6 = 13，
    # 加上 6 条 usage/事件行 = 19 行（与 Node 离线回放的 19 个块一致）。
    checks = {
        "blocks_13": stats.get("blocks") == 13,
        "rows_19": stats.get("rows") == 19,
        "thought_5": stats.get("thought") == 5,
        "messages_2": stats.get("messages") == 2,
        "tools_6": stats.get("tools") == 6,
        "events_6": stats.get("events") == 6,
        "rows_far_below_1612": (stats.get("rows") or 0) <= 30,
        "no_short_blocks": stats.get("shortRich") == 0,
        "no_mojibake": stats.get("mojibake") is False,
        "collapsed_thoughts_4": stats.get("collapsedThoughts") == 4,
        "toggles_5": stats.get("toggles") == 5,
        "tool_status_all": bool(stats.get("toolStatuses")) and all(stats["toolStatuses"]),
        "tool_commands_long": len([code for code in (stats.get("toolCodes") or []) if len(code) >= 20]) >= 4,
        "thought_toggle_clicked": report["steps"].get("thought_toggle") == "clicked",
        "thought_toggle_effect": (report["steps"].get("thought_expanded") or {}).get("collapsed") == 3,
        "tool_expand_ok": report["steps"].get("tool_expand") == "opened",
        "drawer_sync_button": bool((report["steps"].get("drawer_sync") or {}).get("syncButtonExists")),
        "drawer_pending_one": str((report["steps"].get("drawer_sync") or {}).get("pendingMatch")) == "1",
        "drawer_task_failed": ((report["steps"].get("drawer_sync") or {}).get("status") or {}).get("status") == "failed",
        "drawer_sync_not_clicked": True,  # 验收全程未点击同步（不写工作区）
        "no_console_errors": not report["console_errors"] and not report["exceptions"],
    }
    report["checks"] = checks
    report["ok"] = all(checks.values())
    return report


# ---------------------------------------------------------------------------
# 场景 2：真实 WS + 假 Agent 长输出
# ---------------------------------------------------------------------------


def smoke_home(kind: str) -> Path:
    """每个场景一个独立工作台（replay / live），互不覆盖、也避免 Windows 文件锁互相干扰。"""
    return E2E / f"live-smoke-home-{kind}"


def prepare_stream_home(kind: str) -> Path:
    """独立工作台：一个「流式假 ACP」Agent（连续 chunk + 工具调用）。

    每次都重建：上次跑的残留项目会让 ``POST /api/projects`` 撞名。
    删不掉通常是上一轮后端进程还占着 sqlite 文件——重试几秒后仍失败就报错，
    别带着脏数据继续跑（历史教训：脏 home 会让 POST /api/projects 直接 500）。
    """
    home = smoke_home(kind)
    for _ in range(6):
        if not home.exists():
            break
        shutil.rmtree(home, ignore_errors=True)
        if home.exists():
            time.sleep(0.5)
    if home.exists():
        raise SystemExit(f"无法清理 {home}（可能有残留后端进程占用），请先停掉再重跑")
    (home / "config").mkdir(parents=True)
    demo = E2E / "demo-clean" / "config"
    for name in ("acceptance.json", "templates.json", "policy.json"):
        if (demo / name).exists():
            shutil.copy2(demo / name, home / "config" / name)
    agents = {
        "version": 1,
        "defaults": {"timeout_sec": 120},
        "agents": [
            {
                "id": "stream",
                "label": "流式 Agent（假 ACP）",
                "protocol": "acp",
                "command": sys.executable,
                "args": [str(STREAM_AGENT)],
                "env": {
                    "PYTHONPATH": str(REPO),
                    "FAKE_STREAM_THOUGHTS": "260",
                    "FAKE_STREAM_MESSAGES": "120",
                    "FAKE_STREAM_DELAY_MS": "15",
                },
                "roles": ["worker", "implementer", "writer"],
                "strengths": ["code", "doc"],
                "can_write": True,
                "enabled": True,
            },
            {"id": "human", "label": "人工", "protocol": "human", "enabled": True},
        ],
    }
    (home / "config" / "agents.json").write_text(
        json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return home


async def count_ws_events(base_url: str, project_id: str, stop: asyncio.Event, *, timeout: float = 120.0) -> dict:
    """独立 WS 客户端：统计后端实际推了多少条事件（验证合并，而不是逐 chunk）。

    用 ``stop`` 事件收口（由调用方在任务结束后置位）——不能拿 ``run_status.running=false``
    当结束信号：订阅瞬间服务端就会先推一条「当前没在跑」的状态。
    """
    import websockets

    url = base_url.replace("http://", "ws://") + f"ws?project={project_id}"
    counts = {"events": 0, "chunk_events": 0, "chunk_count_sum": 0, "kinds": {}}
    deadline = time.monotonic() + timeout
    async with websockets.connect(url, max_size=None, origin=base_url.rstrip("/")) as ws:
        while time.monotonic() < deadline and not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            message = json.loads(raw)
            if message.get("type") != "stream.event":
                continue
            event = message.get("event") or {}
            kind = str(event.get("kind") or "")
            counts["events"] += 1
            counts["kinds"][kind] = counts["kinds"].get(kind, 0) + 1
            if kind.endswith("_chunk"):
                counts["chunk_events"] += 1
                counts["chunk_count_sum"] += int(event.get("chunk_count") or 0)
    return counts


LIVE_STATS_JS = (
    "(() => {"
    + PAGE_HELPERS
    + """
  const feed = document.getElementById('live-list');
  if (!feed) return JSON.stringify({error: 'no-live-list'});
  const blocks = $$('.live-block', feed);
  const tools = $$('.live-tool', feed);
  const box = feed;
  const text = feed.innerText || '';
  return JSON.stringify({
    blocks: blocks.length,
    thought: $$('.live-thought', feed).length,
    messages: $$('.live-msg', feed).length,
    tools: tools.length,
    events: $$('.live-event', feed).length,
    thoughtTextLen: $$('.live-thought .live-rich', feed).reduce((n, el) => n + (el.innerText || '').length, 0),
    toolCommands: $$('.live-tool .live-code', feed).map((el) => el.textContent.trim()),
    toolStatuses: $$('.live-tool-status', feed).map((el) => el.textContent.trim()),
    mojibake: text.includes('\\uFFFD') || text.includes('???'),
    pinned: box.scrollHeight - box.scrollTop - box.clientHeight < 40,
    jumpVisible: (() => { const btn = document.getElementById('live-jump'); return Boolean(btn && !btn.hidden && btn.classList.contains('is-visible')); })(),
    scrollTop: Math.round(box.scrollTop),
    scrollHeight: Math.round(box.scrollHeight),
    clientHeight: Math.round(box.clientHeight),
    scrollable: box.scrollHeight - box.clientHeight > 60,
  });
})()"""
)


async def live_flow(debug_port: int, base_url: str, project_id: str, task_id: str) -> dict:
    import websockets

    report: dict = {"ok": False, "steps": {}, "console_errors": [], "exceptions": [], "screenshots": []}
    ws_url = page_target(debug_port, base_url)
    async with websockets.connect(ws_url, max_size=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Log.enable")
        await cdp.call("Page.enable")
        await set_viewport(cdp, SIZES[0])
        report["steps"]["boot"] = await wait_boot(cdp)
        report["steps"]["live_view"] = await goto_view(cdp, "live")
        await asyncio.sleep(0.4)

        stop = asyncio.Event()
        ws_task = asyncio.create_task(count_ws_events(base_url, project_id, stop))
        started = await cdp.evaluate(
            "fetch('/api/tasks/%s/run', {method: 'POST', headers: {'Content-Type': 'application/json'},"
            " body: JSON.stringify({wait: false})}).then((r) => r.json())" % task_id,
            await_promise=True,
        )
        report["steps"]["run_started"] = started

        # 运行中轮询：块数应逐步增长，但远小于 160 条 chunk
        growth: list[int] = []
        for _ in range(240):
            await asyncio.sleep(0.15)
            value = await cdp.evaluate(
                "(() => { const feed = document.getElementById('live-list');"
                " return JSON.stringify({blocks: feed ? feed.querySelectorAll('.live-block').length : 0}); })()"
            )
            data = json.loads(value or "{}")
            count = data.get("blocks", 0)
            if not growth or growth[-1] != count:
                growth.append(count)
            if count >= 3:
                break
        report["steps"]["block_growth"] = growth

        # 等任务结束（dashboard.running=false）
        settled = await poll_until(
            cdp,
            "fetch('/api/projects/%s/dashboard').then((r) => r.json())"
            ".then((d) => JSON.stringify({running: d.running, statuses: (d.tasks || []).map((t) => t.status)}))"
            % project_id,
            lambda data: data.get("running") is False and bool(data.get("statuses")),
            timeout=90.0,
            interval=0.4,
            await_promise=True,
        )
        report["steps"]["settled"] = settled
        await asyncio.sleep(0.8)

        stats = json.loads(await cdp.evaluate(LIVE_STATS_JS) or "{}")
        report["stats"] = stats
        report["screenshots"].append(await screenshot(cdp, E2E / "live-run-1832.png"))

        # 上滚 → 「回到最新」出现；点击 → 回到底部。
        # 内容不足一屏时不会出现 jump（滚动条不存在），这是正确行为 —— 分支处理，不算失败。
        scrollable = bool(stats.get("scrollable"))
        report["steps"]["scrollable"] = scrollable
        if scrollable:
            await cdp.evaluate(
                "(() => { const feed = document.getElementById('live-list'); feed.scrollTop = 0;"
                " feed.dispatchEvent(new Event('scroll')); return 'ok'; })()"
            )
            await asyncio.sleep(0.4)
            report["steps"]["scrolled_up"] = json.loads(await cdp.evaluate(LIVE_STATS_JS) or "{}")
            await cdp.evaluate(
                "(() => { const btn = document.getElementById('live-jump'); if (btn) btn.click(); return 'ok'; })()"
            )
            await asyncio.sleep(0.4)
            report["steps"]["jumped_back"] = json.loads(await cdp.evaluate(LIVE_STATS_JS) or "{}")
        else:
            report["steps"]["scrolled_up"] = {"skipped": "内容不足一屏，无滚动条"}
            report["steps"]["jumped_back"] = {"skipped": "内容不足一屏，无滚动条"}

        await set_viewport(cdp, SIZES[1])
        await asyncio.sleep(0.4)
        report["screenshots"].append(await screenshot(cdp, E2E / "live-run-1440.png"))

        stop.set()
        try:
            report["ws_counts"] = await asyncio.wait_for(ws_task, timeout=15)
        except asyncio.TimeoutError:
            ws_task.cancel()
            report["ws_counts"] = {"error": "timeout"}

        await cdp.pump(0.4)
        report["console_errors"] = cdp.console_errors
        report["exceptions"] = cdp.exceptions

    stats = report.get("stats") or {}
    ws_counts = report.get("ws_counts") or {}
    scrolled = report.get("steps", {}).get("scrolled_up") or {}
    jumped = report.get("steps", {}).get("jumped_back") or {}
    checks = {
        "boot": bool(report["steps"].get("boot")),
        "live_view": bool(report["steps"].get("live_view")),
        "blocks_rendered": (stats.get("blocks") or 0) >= 3,
        "blocks_far_below_chunks": (stats.get("blocks") or 0) <= 40,
        "thought_present": (stats.get("thought") or 0) >= 1,
        "thought_is_long": (stats.get("thoughtTextLen") or 0) >= 400,
        "message_present": (stats.get("messages") or 0) >= 1,
        "tool_present": (stats.get("tools") or 0) >= 1,
        "tool_has_command": any("Get-ChildItem" in code for code in (stats.get("toolCommands") or [])),
        "tool_status_completed": "完成" in (stats.get("toolStatuses") or []),
        "no_mojibake": stats.get("mojibake") is False,
        "autoscroll_pinned": stats.get("pinned") is True,
        "jump_appears_on_scroll_up": (not scrollable)
        or (scrolled.get("jumpVisible") is True and scrolled.get("pinned") is False),
        "jump_returns_to_bottom": (not scrollable)
        or (jumped.get("pinned") is True and jumped.get("jumpVisible") is False),
        "ws_merged_chunks": (ws_counts.get("chunk_events") or 0) >= 1
        and (ws_counts.get("chunk_events") or 0) < 80,
        "ws_chunk_count_sum": (ws_counts.get("chunk_count_sum") or 0) >= 120,
        "task_success": "success" in (settled.get("statuses") or []),
        "no_console_errors": not report["console_errors"] and not report["exceptions"],
    }
    report["checks"] = checks
    report["ok"] = all(checks.values())
    return report


# ---------------------------------------------------------------------------
# 场景 3：真实桌面服务 8899（只读）
# ---------------------------------------------------------------------------

REAL_STATS_JS = (
    "(() => {"
    + PAGE_HELPERS
    + """
  const drawer = document.getElementById('drawer');
  const body = drawer ? (drawer.innerText || '') : '';
  const syncBtn = drawer ? drawer.querySelector('[data-act="task-sync"]') : null;
  const entries = drawer ? $$('.entry', drawer).map((el) => (el.innerText || '').trim()) : [];
  const live = document.getElementById('live-list');
  return JSON.stringify({
    liveText: live ? (live.innerText || '').trim().slice(0, 120) : '',
    drawerOpen: Boolean(drawer && drawer.classList.contains('open')),
    drawerTitle: (document.getElementById('drawer-title') || {}).textContent || '',
    hasWorkspaceSection: body.includes('工作目录 / 工作副本'),
    hasPendingCopy: /待同步\\s*\\d+\\s*个文件/.test(body),
    pendingMatch: (body.match(/待同步\\s*(\\d+)\\s*个文件/) || [])[1] || '',
    changesMatch: (body.match(/工作副本改动\\s*(\\d+)\\s*个文件/) || [])[1] || '',
    syncButtonText: syncBtn ? (syncBtn.innerText || '').trim() : '',
    syncButtonExists: Boolean(syncBtn),
    entries: entries.slice(0, 8),
  });
})()"""
)

BOARD_STATS_JS = (
    "(() => {"
    + PAGE_HELPERS
    + """
  const active = document.querySelector('.view.is-active');
  const list = (active && active.querySelector('#bb-list, #handoff-list'))
    || document.getElementById('bb-list') || document.getElementById('handoff-list');
  const bodies = list ? $$('.row-body', list) : [];
  const rows = list ? $$('.row', list).length : 0;
  const measured = bodies.map((el) => {
    const text = el.innerText || '';
    return { len: text.length, lines: text.split('\\n').length, head: text.slice(0, 40) };
  });
  const long = measured.filter((item) => item.len > 200);
  return JSON.stringify({
    rows,
    bodies: measured.length,
    longBodies: long.length,
    maxLinesInLongBody: long.reduce((n, item) => Math.max(n, item.lines), 0),
    sample: long.slice(0, 2),
    mojibake: (list ? list.innerText : '').includes('\\uFFFD') || (list ? list.innerText : '').includes('???'),
  });
})()"""
)


async def real_flow(debug_port: int, base_url: str, project_id: str, task_id: str) -> dict:
    import websockets

    report: dict = {"ok": False, "steps": {}, "console_errors": [], "exceptions": [], "screenshots": []}
    ws_url = page_target(debug_port, base_url)
    async with websockets.connect(ws_url, max_size=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Log.enable")
        await cdp.call("Page.enable")
        await set_viewport(cdp, SIZES[0])
        report["steps"]["boot"] = await wait_boot(cdp)
        report["steps"]["live_view"] = await goto_view(cdp, "live")
        await asyncio.sleep(0.6)
        report["steps"]["live"] = json.loads(await cdp.evaluate(REAL_STATS_JS) or "{}")
        report["screenshots"].append(await screenshot(cdp, E2E / "live-real-8899-1832.png"))

        # 8899 的 hub 是内存态（那条真实 run 是在旧进程里跑完的）→ 注入真实抓包，
        # 验证「同一份前端资产在真实桌面服务上」也能按块渲染 + 滚动/回到最新。
        events = json.loads(CAPTURE.read_text(encoding="utf-8"))
        await cdp.evaluate("window.__liveCapture = []; 'ok'")
        for start in range(0, len(events), 200):
            payload = json.dumps(events[start: start + 200], ensure_ascii=False)
            await cdp.evaluate(f"window.__liveCapture.push(...{payload}); 'ok'")
        injected = await cdp.evaluate(
            "(() => { if (typeof state === 'undefined') return 'no-state';"
            " state.stream = window.__liveCapture.slice(); markLiveDirty(); renderLiveFeed(true);"
            " const feed = document.getElementById('live-list');"
            " if (feed) feed.scrollTop = feed.scrollHeight;"
            " return String(state.stream.length); })()"
        )
        await asyncio.sleep(0.8)
        real_replay = json.loads(await cdp.evaluate(REPLAY_STATS_JS) or "{}")
        await cdp.evaluate(
            "(() => { const feed = document.getElementById('live-list'); if (!feed) return 'no-feed';"
            " feed.scrollTop = 0; feed.dispatchEvent(new Event('scroll')); return 'ok'; })()"
        )
        await asyncio.sleep(0.4)
        real_replay_scrolled = json.loads(await cdp.evaluate(LIVE_STATS_JS) or "{}")
        await cdp.evaluate(
            "(() => { const btn = document.getElementById('live-jump'); if (btn) btn.click(); return 'ok'; })()"
        )
        await asyncio.sleep(0.4)
        real_replay_jumped = json.loads(await cdp.evaluate(LIVE_STATS_JS) or "{}")
        report["steps"]["real_replay"] = {
            "injected": injected,
            "stats": real_replay,
            "scrolled": {k: real_replay_scrolled.get(k) for k in ("pinned", "jumpVisible")},
            "jumped": {k: real_replay_jumped.get(k) for k in ("pinned", "jumpVisible")},
        }
        report["screenshots"].append(await screenshot(cdp, E2E / "live-real-replay-1832.png"))

        # 页面内直连 workspace 接口（浏览器同源，等价于抽屉实际调用）
        workspace = await cdp.evaluate(
            "fetch('/api/tasks/%s/workspace').then(async (r) => JSON.stringify({status: r.status, body: await r.json()}))"
            % task_id,
            await_promise=True,
        )
        report["steps"]["workspace_api"] = json.loads(workspace or "{}")

        # 打开任务抽屉（调用真实 openTask，而不是拼 DOM）
        opened = await cdp.evaluate(
            "openTask(%s).then(() => { const d = document.getElementById('drawer');"
            " return d && d.classList.contains('open') ? 'open' : 'closed'; })" % json.dumps(task_id),
            await_promise=True,
        )
        await asyncio.sleep(0.8)
        report["steps"]["drawer_open"] = opened
        drawer = json.loads(await cdp.evaluate(REAL_STATS_JS) or "{}")
        report["steps"]["drawer"] = drawer
        report["screenshots"].append(await screenshot(cdp, E2E / "live-real-drawer-1832.png"))

        await cdp.evaluate(
            "(() => { const btn = document.getElementById('drawer-close'); if (btn) btn.click(); return 'ok'; })()"
        )
        await asyncio.sleep(0.3)

        # 黑板：正文必须已是整段（normalizeChunkedText 生效）——切到 1440 再拍，文件名与尺寸一致
        await set_viewport(cdp, SIZES[1])
        await goto_view(cdp, "board")
        await asyncio.sleep(1.2)
        report["steps"]["board"] = json.loads(await cdp.evaluate(BOARD_STATS_JS) or "{}")
        report["screenshots"].append(await screenshot(cdp, E2E / "live-real-board-1440.png"))

        # 交接说明
        await goto_view(cdp, "handoffs")
        await asyncio.sleep(1.2)
        report["steps"]["handoffs"] = json.loads(await cdp.evaluate(BOARD_STATS_JS) or "{}")
        report["screenshots"].append(await screenshot(cdp, E2E / "live-real-handoffs-1440.png"))

        await goto_view(cdp, "live")
        await asyncio.sleep(0.5)
        report["screenshots"].append(await screenshot(cdp, E2E / "live-real-8899-1440.png"))

        await cdp.pump(0.4)
        report["console_errors"] = cdp.console_errors
        report["exceptions"] = cdp.exceptions

    live = report["steps"].get("live") or {}
    workspace = report["steps"].get("workspace_api") or {}
    ws_body = workspace.get("body") or {}
    drawer = report["steps"].get("drawer") or {}
    board = report["steps"].get("board") or {}
    handoffs = report["steps"].get("handoffs") or {}
    pending_count = int((ws_body.get("pending") or {}).get("count") or 0)
    unsynced_raw = ws_body.get("unsynced")
    has_unsynced = isinstance(unsynced_raw, dict)
    # 待同步 = unsynced（sync 后归零）；旧后端没有该字段时退回 pending
    expected_sync = int((unsynced_raw or {}).get("count") or 0) if has_unsynced else pending_count
    checks = {
        "boot": bool(report["steps"].get("boot")),
        "live_view": bool(report["steps"].get("live_view")),
        "live_no_error": "error" not in (live.get("liveText") or "").lower(),
        "workspace_200": workspace.get("status") == 200,
        "workspace_pending_number": pending_count >= 1,
        "drawer_open": report["steps"].get("drawer_open") == "open",
        "drawer_workspace_section": bool(drawer.get("hasWorkspaceSection")),
        "drawer_pending_copy": bool(drawer.get("hasPendingCopy")),
        "drawer_pending_matches_api": str(drawer.get("pendingMatch")) == str(expected_sync),
        "drawer_changes_matches_pending": (str(drawer.get("changesMatch")) == str(pending_count)) if has_unsynced else True,
        # 同步按钮只在「有待同步改动」时出现：真实项目已同步过（unsynced=0）→ 不出现才是对的
        "sync_button_semantics": bool(drawer.get("syncButtonExists")) == (expected_sync > 0),
        "sync_button_text_when_present": (not drawer.get("syncButtonExists"))
        or ("同步" in str(drawer.get("syncButtonText") or "")),
        "real_replay_blocks": (real_replay.get("blocks") == 13 and real_replay.get("rows") == 19),
        "real_replay_jump": (real_replay_scrolled.get("jumpVisible") is True and real_replay_scrolled.get("pinned") is False)
        and (real_replay_jumped.get("pinned") is True and real_replay_jumped.get("jumpVisible") is False),
        "board_paragraphs": (board.get("rows") or 0) >= 1
        and (board.get("longBodies") or 0) >= 1
        and (board.get("maxLinesInLongBody") or 0) <= 40,
        "handoffs_paragraphs": (handoffs.get("rows") or 0) >= 1
        and (handoffs.get("maxLinesInLongBody") or 0) <= 40,
        "no_mojibake": not (board.get("mojibake") or handoffs.get("mojibake") or live.get("mojibake")),
        "no_console_errors": not report["console_errors"] and not report["exceptions"],
    }
    report["checks"] = checks
    report["ok"] = all(checks.values())
    report["sync_clicked"] = False  # 明确：验收全程没有点击「同步」（不写用户目录）
    return report


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_replay_phase() -> dict:
    home = prepare_stream_home("replay")
    port = free_port(8906)
    server = start_server(home, port)
    edge = None
    try:
        base = f"http://127.0.0.1:{port}/"
        http_post(f"{base}api/projects", {"name": "实时流回放项目"})
        debug_port = free_port(9360)
        edge = launch_edge(debug_port, E2E / "live-edge-replay", f"{base}#live", SIZES[0])
        asyncio.run(wait_cdp_ready(debug_port))
        return asyncio.run(replay_flow(debug_port, base, CAPTURE))
    finally:
        kill_edge(edge)
        stop_server(server)


def run_live_phase() -> dict:
    home = prepare_stream_home("live")
    port = free_port(8902)
    server = start_server(home, port)
    edge = None
    try:
        base = f"http://127.0.0.1:{port}/"
        project = http_post(f"{base}api/projects", {"name": "实时流真跑项目"})
        task = http_post(
            f"{base}api/projects/{project['id']}/tasks",
            {
                "title": "实时流长输出（假 ACP）",
                "kind": "generic",
                "agent_id": "stream",
                "acceptance": [{"type": "file_exists", "path": "实时流产物.md"}],
            },
        )
        debug_port = free_port(9370)
        edge = launch_edge(debug_port, E2E / "live-edge-live", f"{base}#live", SIZES[0])
        asyncio.run(wait_cdp_ready(debug_port))
        result = asyncio.run(live_flow(debug_port, base, project["id"], task["id"]))
        result["project"] = project["name"]
        result["task_id"] = task["id"]
        return result
    finally:
        kill_edge(edge)
        stop_server(server)


def run_real_phase() -> dict:
    try:
        health = http_json(REAL_BASE + "api/health")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"8899 服务不可用（需要桌面端在跑）：{exc}")
    projects = http_json(REAL_BASE + "api/projects")
    if not projects:
        raise SystemExit("8899 上没有项目")
    project = projects[0]
    tasks = http_json(f"{REAL_BASE}api/projects/{project['id']}/tasks")
    if not tasks:
        raise SystemExit(f"项目 {project['name']} 没有任务")
    task = tasks[0]
    debug_port = free_port(9380)
    edge = launch_edge(debug_port, E2E / "live-edge-real", f"{REAL_BASE}#live", SIZES[0])
    try:
        asyncio.run(wait_cdp_ready(debug_port))
        result = asyncio.run(real_flow(debug_port, REAL_BASE, project["id"], task["id"]))
        result["project"] = project["name"]
        result["task_id"] = task["id"]
        result["home"] = health.get("home")
        return result
    finally:
        kill_edge(edge)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("replay", "live", "real", "all"), default="all")
    args = parser.parse_args()

    E2E.mkdir(parents=True, exist_ok=True)
    if args.mode in ("replay", "all") and not CAPTURE.exists():
        raise SystemExit(f"缺少真实抓包 {CAPTURE}")

    started = time.time()
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mode": args.mode}
    if args.mode in ("replay", "all"):
        log("[1/3] 离线回放 1612 条真实事件 …")
        report["replay"] = run_replay_phase()
        log(f"  → {'PASS' if report['replay']['ok'] else 'FAIL'} {report['replay'].get('stats')}")
    if args.mode in ("live", "all"):
        log("[2/3] 真实 WS + 假 Agent 长输出 …")
        report["live"] = run_live_phase()
        log(f"  → {'PASS' if report['live']['ok'] else 'FAIL'} {report['live'].get('stats')}")
    if args.mode in ("real", "all"):
        log("[3/3] 8899 真实桌面服务（只读）…")
        report["real"] = run_real_phase()
        log(f"  → {'PASS' if report['real']['ok'] else 'FAIL'} {report['real'].get('steps', {}).get('drawer')}")

    report["elapsed_sec"] = round(time.time() - started, 1)
    failures: list[str] = []
    for phase in ("replay", "live", "real"):
        data = report.get(phase)
        if not data:
            continue
        for name, ok in (data.get("checks") or {}).items():
            if not ok:
                failures.append(f"[{phase}] {name}")
        if data.get("console_errors"):
            failures.append(f"[{phase}] console 错误 {len(data['console_errors'])} 条")
        if data.get("exceptions"):
            failures.append(f"[{phase}] 未捕获异常 {len(data['exceptions'])} 条")
    report["failures"] = failures
    report["ok"] = not failures
    RESULTS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n结果：{'PASS' if report['ok'] else 'FAIL'}（{report['elapsed_sec']}s）→ {RESULTS}")
    for failure in failures:
        log(f"  ✗ {failure}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
