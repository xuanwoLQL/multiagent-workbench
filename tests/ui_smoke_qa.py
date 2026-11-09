"""浏览器冒烟（headless Edge）：截图 + DOM 断言 + 横向溢出 + console 错误 + 真实点击。

用法（PowerShell）::

    .\\.venv\\Scripts\\python.exe tests\\ui_smoke_qa.py --mode all
    .\\.venv\\Scripts\\python.exe tests\\ui_smoke_qa.py --mode dom      # 截图 + dump-dom
    .\\.venv\\Scripts\\python.exe tests\\ui_smoke_qa.py --mode layout   # 三尺寸横向溢出 + 关键 id 有内容
    .\\.venv\\Scripts\\python.exe tests\\ui_smoke_qa.py --mode cdp      # 真实点击 + console 错误
    .\\.venv\\Scripts\\python.exe tests\\ui_smoke_qa.py --home .e2e\\demo-clean

为什么单独一个脚本而不是 pytest 用例：它需要真实浏览器（Edge）与真实后端进程，
在无头 CI 上会因环境缺失而失败；放进 pytest 会让「单元测试」变成「环境测试」。
所以它保持 `tests/` 下但**不叫 test_*.py**（pytest 不会收集），由验收人手动跑。

产物：
* 截图：``.e2e/qa-<view>-<width>.png``（1832×1010 / 1440×900 / 1280×800）
* DOM 快照：``.e2e/qa-dom-<name>.html``（出问题时可 diff）
* 结果：``.e2e/qa-smoke-results.json``（截图路径、断言明细、溢出量、console 错误、耗时）

三组断言：
1. ``dom``    —— dump-dom：``#mission-text`` 存在、``#home-metrics`` 有数字、看板有 6 个 ``.kcol``
                 + 卡片、``#inspector-agents`` 有 ``.agent-tile``、``#status-ws`` 含已连接/未连接、
                 整页文本无 ``�``/``???``；
2. ``layout`` —— 三个尺寸下逐视图切页：关键容器有内容 + 页面无横向溢出
                 （``documentElement.scrollWidth <= innerWidth + 1``）；
3. ``cdp``    —— 真实点击 ``#mission-start``：toast 出现、看板多一张卡、调度收口、
                 全程无 console error / 未捕获异常。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

REPO = Path(__file__).resolve().parents[1]
E2E = REPO / ".e2e"
RESULTS = E2E / "qa-smoke-results.json"
DEMO_HOME = E2E / "demo-clean"
EMPTY_HOME = E2E / "empty-home"
SMOKE_HOME = E2E / "qa-smoke-home"

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

#: 验收尺寸：1832×1010（高分辨率桌面）、1440×900 / 1280×800（常见笔记本）。
#: 每个尺寸都逐视图截图并做布局检查（契约要求「两个尺寸首屏无空白/无溢出」，这里多验一个 1832）。
HOME_SIZES = ((1832, 1010), (1440, 900), (1280, 800))

VIEWS = (
    ("home", "/#home"),
    ("tasks", "/#tasks"),
    ("live", "/#live"),
    ("board", "/#board"),
    ("agents", "/#agents"),
    ("observe", "/#observe"),
    ("config", "/#config"),
)

#: 每个视图「有内容」的判定（在页面里求值，返回 JSON 字符串）。
VIEW_PROBES: dict[str, tuple[str, str]] = {
    "home": (
        "JSON.stringify({view: activeId(), metrics: txt('home-metrics'), "
        "cards: cnt('#home-kanban .kcard'), tiles: cnt('#inspector-agents .agent-tile')})",
        "metrics 含数字 & 看板有卡片 & 编队有 tile",
    ),
    "tasks": ("JSON.stringify({view: activeId(), cards: cnt('#kanban .kcard')})", "看板有卡片"),
    "live": ("JSON.stringify({view: activeId(), text: txt('live-list').length})", "事件流容器有内容"),
    "board": (
        "JSON.stringify({view: activeId(), rows: cnt('#bb-list .row'), text: txt('bb-list').length})",
        "黑板有记录",
    ),
    "agents": (
        "JSON.stringify({view: activeId(), rows: cnt('#agents-list .row'), text: txt('agents-list').length})",
        "Agent 列表有行",
    ),
    "observe": (
        "JSON.stringify({view: activeId(), stats: txt('stats').length, runs: cnt('#runs .row'), "
        "timeline: cnt('#timeline .row'), artifacts: cnt('#artifacts .row')})",
        "统计/运行/时间线/产物都有内容",
    ),
    "config": (
        "JSON.stringify({view: activeId(), len: (document.getElementById('config-editor') || {}).value "
        "? document.getElementById('config-editor').value.length : 0})",
        "配置编辑器已载入",
    ),
    # 以下四个视图不截图（Lead 的截图清单没点），但布局阶段照样切过去验证「有内容」。
    "dag": (
        "JSON.stringify({view: activeId(), nodes: cnt('#dag svg *'), text: txt('dag').length})",
        "依赖图 SVG 有节点",
    ),
    "handoffs": (
        "JSON.stringify({view: activeId(), rows: cnt('#handoff-list .row'), text: txt('handoff-list').length})",
        "交接说明有行",
    ),
    "approvals": (
        "JSON.stringify({view: activeId(), text: txt('approval-list').length})",
        "审批列表有内容（空态也算）",
    ),
    "snapshots": (
        "JSON.stringify({view: activeId(), rows: cnt('#snap-list .row'), text: txt('snap-list').length})",
        "快照列表有行",
    ),
}

#: 布局/内容检查覆盖全部 11 个视图（截图只覆盖 VIEWS 里那 7 个）。
LAYOUT_VIEWS = VIEWS + (("dag", "/#dag"), ("handoffs", "/#handoffs"), ("approvals", "/#approvals"), ("snapshots", "/#snapshots"))

#: 注入到页面里的公共小工具（activeId/txt/cnt），只用于求值，不改页面。
PAGE_HELPERS = """
const activeId = () => { const el = document.querySelector('.view.is-active'); return el ? el.id : ''; };
const txt = (id) => { const el = document.getElementById(id); return el ? (el.textContent || '').trim() : ''; };
const cnt = (sel) => document.querySelectorAll(sel).length;
"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def log(message: str) -> None:
    print(message, flush=True)


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def free_port(start: int) -> int:
    port = start
    while port_open(port):
        port += 1
    return port


def wait_health(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(0.25)
    return False


def edge_path() -> str:
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise SystemExit("找不到 Edge（msedge.exe），无法做浏览器冒烟")


def start_server(home: Path, port: int) -> subprocess.Popen:
    """起一个独立后端（独立 MULTIAGENT_HOME），日志写 .e2e/qa-serve-<port>.*。"""
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", MULTIAGENT_HOME=str(home.resolve()))
    out = (E2E / f"qa-serve-{port}.out").open("wb")
    err = (E2E / f"qa-serve-{port}.err").open("wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "multiagent", "serve", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO),
        env=env,
        stdout=out,
        stderr=err,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if not wait_health(port):
        proc.terminate()
        raise SystemExit(f"后端 {port}（home={home}）未就绪，见 .e2e/qa-serve-{port}.err")
    return proc


def stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()


def http_json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url: str, payload: dict) -> object:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def png_size(path: Path) -> tuple[int, int] | None:
    """读 PNG 头拿宽高（不引入 Pillow 依赖）。"""
    try:
        raw = path.read_bytes()[:24]
    except OSError:
        return None
    if len(raw) < 24 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", raw[16:24])


def edge_common(profile: Path) -> list[str]:
    profile.mkdir(parents=True, exist_ok=True)
    return [
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-extensions",
        "--disable-background-networking",
        f"--user-data-dir={profile}",
    ]


def save_png(payload_b64: str, out: Path) -> dict:
    import base64

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(base64.b64decode(payload_b64))
    return {"path": str(out.relative_to(REPO)) if out.is_relative_to(REPO) else str(out)}


def dump_dom(url: str, out: Path, wait_ms: int = 8000) -> str:
    """Edge 是 GUI 子系统程序：stdout 必须重定向到文件才拿得到 DOM。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    profile = E2E / "qa-edge-profile-dom"
    cmd = [
        edge_path(),
        *edge_common(profile),
        "--dump-dom",
        f"--virtual-time-budget={wait_ms}",
        url,
    ]
    with out.open("wb") as handle:
        subprocess.run(cmd, check=False, timeout=180, stdout=handle, stderr=subprocess.DEVNULL)
    return out.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# DOM 解析：按 id 取文本、按 class 计数
# ---------------------------------------------------------------------------


class DomIndex(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.id_text: dict[str, str] = {}
        self.class_counts: dict[str, int] = {}
        self.text_parts: list[str] = []
        self._open: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        attr = {key: (value or "") for key, value in attrs}
        ident = attr.get("id", "")
        if ident:
            self.ids.add(ident)
        for token in attr.get("class", "").split():
            self.class_counts[token] = self.class_counts.get(token, 0) + 1
        if tag not in ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"):
            self._open.append((tag, ident))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                break

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        self.text_parts.append(data)
        for _tag, ident in self._open:
            if ident:
                self.id_text[ident] = (self.id_text.get(ident, "") + data).strip()

    def text_of(self, ident: str) -> str:
        return re.sub(r"\s+", " ", self.id_text.get(ident, "")).strip()

    def has_digit(self, ident: str) -> bool:
        return bool(re.search(r"\d", self.text_of(ident)))

    def body_text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.text_parts))


def dom_checks(dom: str, *, expect_agents: bool, expect_data: bool = True) -> dict[str, dict]:
    """对 dump-dom 结果做断言（返回 name -> {ok, detail}）。

    ``expect_data=False`` 用于「空工作台」：没有项目时 KPI 是占位符 ``—``、看板无卡片，
    这不是缺陷；此时只校验骨架与编码。
    """
    index = DomIndex()
    index.feed(dom)
    index.close()
    text = index.body_text()

    checks: dict[str, dict] = {}

    def add(name: str, ok: bool, detail: str) -> None:
        checks[name] = {"ok": bool(ok), "detail": detail}

    add("mission-text 存在", "mission-text" in index.ids, f"ids={len(index.ids)}")
    metrics_text = index.text_of("home-metrics")
    if expect_data:
        add("home-metrics 有数字", index.has_digit("home-metrics"), f"metrics={metrics_text[:80]!r}")
    else:
        add("home-metrics 显示占位", True, f"空工作台 metrics={metrics_text[:60]!r}")
    kcol = index.class_counts.get("kcol", 0)
    add("kanban 有 .kcol 列", kcol >= 6, f"kcol={kcol}")
    kcard = index.class_counts.get("kcard", 0)
    if expect_data:
        add("kanban 有任务卡片", kcard >= 1, f"kcard={kcard}")
    else:
        add("kanban 有任务卡片", True, f"跳过（空工作台）kcard={kcard}")
    tiles = index.class_counts.get("agent-tile", 0)
    if expect_agents:
        add("inspector-agents 有 .agent-tile", tiles >= 1, f"agent-tile={tiles}")
    else:
        add("inspector-agents 有 .agent-tile", True, f"跳过（空工作台）agent-tile={tiles}")
    ws_text = index.text_of("status-ws")
    add(
        "status-ws 含已连接/未连接",
        ("已连接" in ws_text) or ("未连接" in ws_text),
        f"status-ws={ws_text!r}",
    )
    add("页面文本无 U+FFFD", "\ufffd" not in text, "replacement-char=0" if "\ufffd" not in text else "发现替换字符")
    add("页面文本无 '???'", "???" not in text, "question-runs=0" if "???" not in text else "发现问号串")
    add("mission-templates 有模板", index.class_counts.get("chip", 0) >= 3, f"chip={index.class_counts.get('chip', 0)}")
    return checks


# ---------------------------------------------------------------------------
# CDP 客户端
# ---------------------------------------------------------------------------


class Cdp:
    def __init__(self, ws) -> None:  # noqa: ANN001
        self.ws = ws
        self.next_id = 0
        self.console_errors: list[dict] = []
        self.exceptions: list[dict] = []

    async def _drain(self, msg: dict) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Runtime.consoleAPICalled" and params.get("type") in ("error", "assert"):
            self.console_errors.append(
                {
                    "type": params.get("type"),
                    "args": [str(arg.get("value", arg.get("description", ""))) for arg in params.get("args", [])],
                }
            )
        elif method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            self.exceptions.append(
                {"text": details.get("text", ""), "url": details.get("url", ""), "line": details.get("lineNumber")}
            )
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error":
                self.console_errors.append({"type": "log", "args": [entry.get("text", "")]})

    async def call(self, method: str, params: dict | None = None, timeout: float = 25.0):
        self.next_id += 1
        ident = self.next_id
        await self.ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=timeout))
            if message.get("id") == ident:
                return message
            await self._drain(message)

    async def evaluate(self, expression: str, *, await_promise: bool = False):
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": await_promise},
        )
        payload = result.get("result", {}).get("result", {})
        if payload.get("subtype") == "error":
            raise RuntimeError(payload.get("description", "evaluate failed"))
        return payload.get("value")

    async def pump(self, seconds: float) -> None:
        """在等待期间持续收事件（console/exception 只在事件流里出现）。"""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=0.2))
            except asyncio.TimeoutError:
                continue
            if message.get("id") is None:
                await self._drain(message)


def page_target(port: int, base_url: str) -> str:
    targets = http_json(f"http://127.0.0.1:{port}/json/list")
    pages = [
        item
        for item in targets
        if item.get("type") == "page" and str(item.get("url", "")).startswith(base_url.rstrip("/"))
    ]
    if not pages:
        raise SystemExit(f"CDP 找不到页面目标（base={base_url}）：{json.dumps(targets, ensure_ascii=False)[:500]}")
    return pages[0]["webSocketDebuggerUrl"]


async def wait_cdp_ready(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            http_json(f"http://127.0.0.1:{port}/json/version")
            return
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.25)
    raise SystemExit("Edge 调试端口未就绪")


def launch_edge(debug_port: int, profile: Path, url: str, size: tuple[int, int] | None = None) -> subprocess.Popen:
    shutil.rmtree(profile, ignore_errors=True)
    cmd = [edge_path(), *edge_common(profile), f"--remote-debugging-port={debug_port}"]
    if size:
        cmd.append(f"--window-size={size[0]},{size[1]}")
    cmd.append(url)
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def kill_edge(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        proc.kill()


# ---------------------------------------------------------------------------
# 阶段 1：截图 + dump-dom
# ---------------------------------------------------------------------------


async def capture_views(
    debug_port: int,
    base_url: str,
    size: tuple[int, int],
    views,
    *,
    prefix: str = "qa",
    require_content: bool = True,
    wait_ws: bool = True,
) -> list[dict]:
    """CDP 截图：精确视口 + 真实时间等待（WS 能连上），每个视图等到有内容再拍。

    为什么不用 ``msedge --screenshot``：``--virtual-time-budget`` 会把时间快进，
    WebSocket 握手还没完成就截屏，状态条会拍到「WS 未连接」——那是截图工具的假象，
    不是产品缺陷。CDP 用真实时间等待，拍到的才是用户看到的画面。
    """
    import websockets

    width, height = size
    ws_url = page_target(debug_port, base_url)
    shots: list[dict] = []
    async with websockets.connect(ws_url, max_size=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Page.enable")
        await cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )
        # 等首屏骨架渲染（真实时间）
        for _ in range(120):
            value = await cdp.evaluate(
                "JSON.stringify({ready: document.readyState,"
                " metrics: (document.getElementById('home-metrics')||{}).textContent || '',"
                " ws: (document.getElementById('status-ws')||{}).textContent || ''})"
            )
            data = json.loads(value or "{}")
            if data.get("ready") == "complete" and data.get("metrics") and data.get("ws"):
                break
            await asyncio.sleep(0.25)
        if wait_ws:
            for _ in range(32):
                value = await cdp.evaluate("(document.getElementById('status-ws')||{}).textContent || ''")
                if "已连接" in (value or ""):
                    break
                await asyncio.sleep(0.25)
        for view, _path in views:
            await cdp.evaluate(f"location.hash = '#{view}'; 'ok'")
            if require_content:
                probe, _expectation = VIEW_PROBES[view]
                for _ in range(40):
                    await asyncio.sleep(0.2)
                    value = await cdp.evaluate("(() => {" + PAGE_HELPERS + f"return {probe};}})()")
                    data = json.loads(value or "{}")
                    if data.get("view") == f"view-{view}" and _view_ok(view, data):
                        break
            await asyncio.sleep(0.35)  # 让过渡动画/懒加载收尾
            result = await cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
            payload = (result.get("result") or {}).get("data")
            if not payload:
                shots.append({"view": view, "ok": False, "error": "captureScreenshot 无数据"})
                continue
            out = E2E / f"{prefix}-{view}-{width}.png"
            meta = save_png(payload, out)
            actual = png_size(out)
            shots.append(
                {
                    **meta,
                    "view": view,
                    "requested": [width, height],
                    "actual": list(actual) if actual else None,
                    "ok": actual == (width, height),
                }
            )
    return shots


def capture_size(port: int, size: tuple[int, int], views, *, prefix: str = "qa", **kwargs) -> list[dict]:
    """一个尺寸起一个 Edge 会话，逐视图截图。"""
    debug_port = free_port(9350)
    profile = E2E / f"qa-edge-shot-{port}-{size[0]}"
    edge = launch_edge(debug_port, profile, f"http://127.0.0.1:{port}/#home", size)
    try:
        asyncio.run(wait_cdp_ready(debug_port))
        return asyncio.run(
            capture_views(debug_port, f"http://127.0.0.1:{port}/", size, views, prefix=prefix, **kwargs)
        )
    finally:
        kill_edge(edge)


def run_dom_phase(demo_home: Path) -> dict:
    results: dict = {"screenshots": [], "dom": {}, "servers": {}}
    demo_port, empty_port = 8902, 8903
    demo = start_server(demo_home, demo_port)
    empty = start_server(EMPTY_HOME, empty_port)
    results["servers"] = {"demo": demo_port, "empty": empty_port, "demo_home": str(demo_home)}
    try:
        for width, height in HOME_SIZES:
            shots = capture_size(demo_port, (width, height), VIEWS)
            for shot in shots:
                results["screenshots"].append(shot)
                log(f"  截图 {shot['path'].split(chr(92))[-1]}: {shot.get('actual')} ok={shot['ok']}")
        for width, height in HOME_SIZES:
            shots = capture_size(
                empty_port, (width, height), (("home", "/#home"),), prefix="qa-empty", require_content=False, wait_ws=False
            )
            for shot in shots:
                results["screenshots"].append(shot)
                log(f"  截图 {shot['path'].split(chr(92))[-1]}: {shot.get('actual')} ok={shot['ok']}")

        log("  dump-dom: demo home …")
        demo_dom = dump_dom(f"http://127.0.0.1:{demo_port}/#home", E2E / "qa-dom-home.html")
        results["dom"]["demo-home"] = dom_checks(demo_dom, expect_agents=True)

        log("  dump-dom: empty home …")
        empty_dom = dump_dom(f"http://127.0.0.1:{empty_port}/#home", E2E / "qa-dom-empty.html")
        empty_checks = dom_checks(empty_dom, expect_agents=False, expect_data=False)
        empty_index = DomIndex()
        empty_index.feed(empty_dom)
        empty_index.close()
        empty_text = empty_index.body_text()
        empty_checks["空工作台提示新建/导入"] = {
            "ok": ("新建" in empty_text) or ("导入" in empty_text),
            "detail": empty_text[:120],
        }
        empty_checks["空工作台 KPI 显示占位"] = {
            "ok": "—" in empty_index.text_of("home-metrics") or empty_index.text_of("home-metrics") == "",
            "detail": empty_index.text_of("home-metrics")[:60],
        }
        results["dom"]["empty-home"] = empty_checks
    finally:
        stop_server(demo)
        stop_server(empty)
    return results


# ---------------------------------------------------------------------------
# 阶段 2：三尺寸逐视图 —— 关键 id 有内容 + 无横向溢出
# ---------------------------------------------------------------------------


def _view_ok(view: str, data: dict) -> bool:
    if view == "home":
        return bool(re.search(r"\d", data.get("metrics", ""))) and data.get("cards", 0) >= 1 and data.get("tiles", 0) >= 1
    if view == "tasks":
        return data.get("cards", 0) >= 1
    if view == "live":
        return data.get("text", 0) > 0
    if view in ("board", "agents"):
        return data.get("rows", 0) >= 1
    if view == "observe":
        return (
            data.get("stats", 0) > 10
            and data.get("runs", 0) >= 1
            and data.get("timeline", 0) >= 1
            and data.get("artifacts", 0) >= 1
        )
    if view == "config":
        return data.get("len", 0) > 20
    if view == "dag":
        return data.get("nodes", 0) > 0 or data.get("text", 0) > 0
    if view == "handoffs":
        return data.get("rows", 0) >= 1
    if view == "approvals":
        return data.get("text", 0) > 0
    if view == "snapshots":
        return data.get("rows", 0) >= 1
    return True


async def layout_views(debug_port: int, base_url: str, size: tuple[int, int]) -> dict:
    import websockets

    ws_url = page_target(debug_port, base_url)
    width, height = size
    report: dict = {"requested": [width, height], "views": {}, "inner": {}, "ok": True}
    async with websockets.connect(ws_url, max_size=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Page.enable")
        # 固定精确视口：--window-size 在 headless 下含窗口装饰（实测少 26px 宽 / 93px 高），
        # 用 Emulation 覆盖才能保证「1440×900」就是真的 1440×900 布局视口。
        await cdp.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )

        # 等首屏就绪
        booted = False
        for _ in range(80):
            value = await cdp.evaluate(
                "(() => {" + PAGE_HELPERS + "return JSON.stringify({project: (document.getElementById('project-select')||{}).value || '',"
                " metrics: txt('home-metrics'), tiles: cnt('#inspector-agents .agent-tile'),"
                " iw: window.innerWidth, ih: window.innerHeight});})()"
            )
            data = json.loads(value or "{}")
            if data.get("project") and re.search(r"\d", data.get("metrics", "")) and data.get("tiles", 0) > 0:
                booted = True
                report["inner"] = {"width": data.get("iw"), "height": data.get("ih")}
                break
            await asyncio.sleep(0.25)
        report["boot"] = booted
        report["viewport_exact"] = report.get("inner") == {"width": width, "height": height}

        for view, _path in LAYOUT_VIEWS:
            probe, expectation = VIEW_PROBES[view]
            await cdp.evaluate(f"location.hash = '#{view}'; 'ok'")
            data: dict = {}
            content_ok = False
            for _ in range(40):
                await asyncio.sleep(0.2)
                value = await cdp.evaluate("(() => {" + PAGE_HELPERS + f"return {probe};}})()")
                data = json.loads(value or "{}")
                if data.get("view") == f"view-{view}" and _view_ok(view, data):
                    content_ok = True
                    break
            overflow = await cdp.evaluate(
                "(() => {"
                + PAGE_HELPERS
                + "return JSON.stringify({doc: document.documentElement.scrollWidth,"
                " body: document.body.scrollWidth, iw: window.innerWidth, ih: window.innerHeight,"
                " view: activeId(),"
                " viewScroll: (() => { const el = document.querySelector('.view.is-active');"
                " return el ? el.scrollWidth : 0; })(),"
                " viewClient: (() => { const el = document.querySelector('.view.is-active');"
                " return el ? el.clientWidth : 0; })()});})()"
            )
            metrics = json.loads(overflow or "{}")
            doc_width = metrics.get("doc") or 0
            inner_width = metrics.get("iw") or width
            overflow_px = max(0, doc_width - inner_width)
            entry = {
                "content_ok": content_ok,
                "expectation": expectation,
                "data": data,
                "overflow_px": overflow_px,
                "doc_scroll_width": doc_width,
                "inner_width": inner_width,
                "view_scroll_width": metrics.get("viewScroll"),
                "view_client_width": metrics.get("viewClient"),
            }
            # 契约 §4.1：≥1560 宽六列一行；<1560 三列 × 2 行。量一下列的真实排布。
            if view == "home":
                layout = json.loads(
                    await cdp.evaluate(
                        "(() => { const cols = Array.from(document.querySelectorAll('#home-kanban .kcol'));"
                        " const tops = new Set(cols.map((c) => Math.round(c.getBoundingClientRect().top)));"
                        " const lefts = new Set(cols.map((c) => Math.round(c.getBoundingClientRect().left)));"
                        " return JSON.stringify({count: cols.length, rows: tops.size, cols: lefts.size, width: window.innerWidth}); })()"
                    )
                    or "{}"
                )
                expected = {"rows": 1, "cols": 6} if (layout.get("width") or 0) >= 1560 else {"rows": 2, "cols": 3}
                entry["kanban_layout"] = layout
                entry["kanban_layout_expected"] = expected
                entry["kanban_layout_ok"] = layout.get("rows") == expected["rows"] and layout.get("cols") == expected["cols"]
                if not entry["kanban_layout_ok"]:
                    report["ok"] = False
            report["views"][view] = entry
            if not content_ok or overflow_px > 1:
                report["ok"] = False
    return report


def run_layout_phase(demo_home: Path) -> dict:
    port = free_port(8905)
    server = start_server(demo_home, port)
    base_url = f"http://127.0.0.1:{port}/"
    results: dict = {"sizes": {}, "ok": True}
    try:
        for size in HOME_SIZES:
            width, height = size
            debug_port = free_port(9340)
            profile = E2E / f"qa-edge-layout-{width}"
            log(f"  布局检查 {width}x{height} …")
            edge = launch_edge(debug_port, profile, f"{base_url}#home", size)
            try:
                asyncio.run(wait_cdp_ready(debug_port))
                data = asyncio.run(layout_views(debug_port, base_url, size))
            finally:
                kill_edge(edge)
            results["sizes"][f"{width}x{height}"] = data
            if not data.get("ok"):
                results["ok"] = False
            for view, entry in data.get("views", {}).items():
                extra = ""
                if view == "home" and entry.get("kanban_layout"):
                    extra = f" kanban={entry['kanban_layout']} expect={entry['kanban_layout_expected']}"
                log(
                    f"    {view:<8} content={'OK' if entry['content_ok'] else 'EMPTY'} "
                    f"overflow={entry['overflow_px']}px{extra}"
                )
    finally:
        stop_server(server)
    return results


# ---------------------------------------------------------------------------
# 阶段 3：真实点击 + console 错误
# ---------------------------------------------------------------------------


async def cdp_flow(debug_port: int, base_url: str, project_name: str) -> dict:
    """点击「创建并启动」→ toast + 看板新卡片 → 等调度收口；全程收集 console 错误。"""
    import websockets

    ws_url = page_target(debug_port, base_url)
    report: dict = {"ok": False, "steps": {}, "console_errors": [], "exceptions": []}
    async with websockets.connect(ws_url, max_size=None) as ws:
        cdp = Cdp(ws)
        await cdp.call("Runtime.enable")
        await cdp.call("Log.enable")
        await cdp.call("Page.enable")

        # 1) 等首屏就绪：顶栏选中了项目 + KPI 渲染出数字（新项目可能 0 张卡片）
        booted = False
        for _ in range(80):
            value = await cdp.evaluate(
                "JSON.stringify({project: (document.getElementById('project-select')||{}).value || '',"
                " metrics: (document.getElementById('home-metrics')||{}).textContent || '',"
                " tiles: document.querySelectorAll('#inspector-agents .agent-tile').length,"
                " ws: (document.getElementById('status-ws')||{}).textContent || ''})"
            )
            data = json.loads(value or "{}")
            if data.get("project") and re.search(r"\d", data.get("metrics", "")) and data.get("tiles", 0) > 0:
                booted = True
                break
            await asyncio.sleep(0.25)
        report["steps"]["boot"] = booted

        before = json.loads(
            await cdp.evaluate(
                "JSON.stringify({cards: document.querySelectorAll('#kanban .kcard').length,"
                " home: document.querySelectorAll('#home-kanban .kcard').length,"
                " ws: (document.getElementById('status-ws')||{}).textContent || '',"
                " project: (document.getElementById('project-select')||{}).value || ''})"
            )
            or "{}"
        )
        report["before"] = before

        # 2) 填任务 + 选假 Agent（若有该选项）+ 点「创建并启动」
        clicked = await cdp.evaluate(
            """
            (() => {
              const text = document.getElementById('mission-text');
              const outputs = document.getElementById('mission-outputs');
              const agent = document.getElementById('mission-agent');
              if (!text) return 'no-mission-text';
              text.value = '浏览器冒烟：写一份报告并输出 冒烟报告.md';
              text.dispatchEvent(new Event('input', { bubbles: true }));
              if (outputs) {
                outputs.value = '冒烟报告.md';
                outputs.dispatchEvent(new Event('input', { bubbles: true }));
              }
              if (agent) {
                const option = Array.from(agent.options).find((o) => o.value === 'smoke');
                if (option) { agent.value = 'smoke'; agent.dispatchEvent(new Event('change', { bubbles: true })); }
              }
              const button = document.getElementById('mission-start');
              if (!button) return 'no-mission-start';
              button.click();
              return 'clicked';
            })()
            """
        )
        report["steps"]["click"] = clicked

        # 3) 等 toast + 看板新卡片
        toast_text = ""
        cards_after = before.get("cards", 0)
        for _ in range(80):
            await asyncio.sleep(0.25)
            data = json.loads(
                await cdp.evaluate(
                    "JSON.stringify({toast: ((document.getElementById('toast')||{}).textContent||'').trim(),"
                    " cards: document.querySelectorAll('#kanban .kcard').length,"
                    " home: document.querySelectorAll('#home-kanban .kcard').length,"
                    " view: (document.querySelector('.view.is-active')||{}).id || ''})"
                )
                or "{}"
            )
            toast_text = data.get("toast", "")
            cards_after = data.get("cards", 0)
            if toast_text and (cards_after > before.get("cards", 0) or data.get("home", 0) > before.get("home", 0)):
                report["after"] = data
                break
        report["steps"]["toast"] = toast_text
        report["steps"]["cards_after"] = cards_after
        report["steps"]["card_appeared"] = cards_after > before.get("cards", 0)

        # 4) 等调度收口：dashboard.running 变 false 且新任务不再是 running/pending
        pid = before.get("project", "")
        settled = None
        if pid:
            for _ in range(240):
                await asyncio.sleep(0.25)
                value = await cdp.evaluate(
                    "fetch('/api/projects/' + encodeURIComponent(%s) + '/dashboard')"
                    ".then(r => r.json())"
                    ".then(d => JSON.stringify({running: d.running,"
                    " statuses: d.tasks.map(t => t.status),"
                    " titles: d.tasks.map(t => t.title)}))" % json.dumps(pid),
                    await_promise=True,
                )
                data = json.loads(value or "{}")
                statuses = data.get("statuses") or []
                if data.get("running") is False and statuses and all(s not in ("running", "pending") for s in statuses):
                    settled = data
                    break
        report["steps"]["settled"] = settled

        # 5) 点开任务详情抽屉（任务详情是「看板 → 详情」的核心交互），确认能打开再关掉
        drawer = None
        await cdp.evaluate(
            "(() => { const card = document.querySelector('#kanban .kcard'); if (card) card.click(); return 'ok'; })()"
        )
        for _ in range(40):
            await asyncio.sleep(0.2)
            value = await cdp.evaluate(
                "JSON.stringify({cls: (document.getElementById('drawer')||{}).className || '',"
                " hidden: (document.getElementById('drawer')||{}).getAttribute"
                " ? document.getElementById('drawer').getAttribute('aria-hidden') : '',"
                " title: (document.getElementById('drawer-title')||{}).textContent || '',"
                " body: ((document.getElementById('drawer-body')||{}).textContent || '').length})"
            )
            data = json.loads(value or "{}")
            if "open" in (data.get("cls") or "") and data.get("title") and data.get("body", 0) > 10:
                drawer = data
                break
        report["steps"]["drawer"] = drawer
        await cdp.evaluate(
            "(() => { const btn = document.getElementById('drawer-close'); if (btn) btn.click(); return 'ok'; })()"
        )
        await asyncio.sleep(0.3)
        report["steps"]["drawer_closed"] = json.loads(
            await cdp.evaluate(
                "JSON.stringify({cls: (document.getElementById('drawer')||{}).className || ''})"
            )
            or "{}"
        )

        await cdp.pump(0.5)
        report["console_errors"] = cdp.console_errors
        report["exceptions"] = cdp.exceptions

    report["ok"] = bool(
        report["steps"].get("boot")
        and report["steps"].get("click") == "clicked"
        and report["steps"].get("card_appeared")
        and report["steps"].get("drawer")
        and "open" not in (report["steps"].get("drawer_closed") or {}).get("cls", "")
        and not report["console_errors"]
        and not report["exceptions"]
    )
    return report


def prepare_smoke_home() -> Path:
    """独立冒烟工作台：一个假 CLI Agent（不污染 demo-clean 的数据与截图）。"""
    if SMOKE_HOME.exists():
        shutil.rmtree(SMOKE_HOME, ignore_errors=True)
    (SMOKE_HOME / "config").mkdir(parents=True)
    for name in ("acceptance.json", "templates.json", "policy.json"):
        source = DEMO_HOME / "config" / name
        if source.exists():
            shutil.copy2(source, SMOKE_HOME / "config" / name)
    plan_file = SMOKE_HOME / "plan-smoke.json"
    plan_file.write_text(json.dumps({"files": {"冒烟报告.md": "冒烟产物：中文正常"}, "exit": 0}, ensure_ascii=False), encoding="utf-8")
    agents = {
        "version": 1,
        "defaults": {"timeout_sec": 60},
        "agents": [
            {
                "id": "smoke",
                "label": "冒烟 Agent（假）",
                "protocol": "cli",
                "cli": {"command": sys.executable, "args": ["-m", "multiagent.testing.fake_agent"], "prompt_mode": "stdin"},
                "env": {"PYTHONPATH": str(REPO), "FAKE_PLAN_FILE": str(plan_file)},
                "roles": ["worker", "implementer", "writer"],
                "strengths": ["doc", "code"],
                "can_write": True,
                "enabled": True,
            },
            {"id": "human", "label": "人工", "protocol": "human", "enabled": True},
        ],
    }
    (SMOKE_HOME / "config" / "agents.json").write_text(json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8")
    return SMOKE_HOME


def run_cdp_phase() -> dict:
    """真实点击：创建并启动 → toast + 看板卡片 → 调度收口；收集 console 错误。"""
    home = prepare_smoke_home()
    port = free_port(8904)
    server = start_server(home, port)
    base_url = f"http://127.0.0.1:{port}/"
    # 先建项目再开页面：否则首屏是「还没有项目」，点「创建并启动」只会提示先建项目。
    created = http_post(f"{base_url}api/projects", {"name": "浏览器冒烟项目"})
    log(f"  冒烟项目：{created['name']} ({created['id']})")
    debug_port = free_port(9333)
    profile = E2E / "qa-edge-cdp-profile"
    edge = launch_edge(debug_port, profile, f"{base_url}#home")
    try:
        asyncio.run(wait_cdp_ready(debug_port))
        result = asyncio.run(cdp_flow(debug_port, base_url, created["name"]))
        result["project"] = created["name"]
        return result
    finally:
        kill_edge(edge)
        stop_server(server)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("dom", "layout", "cdp", "all"), default="all")
    parser.add_argument("--home", default=str(DEMO_HOME), help="演示工作台目录（默认 .e2e/demo-clean）")
    args = parser.parse_args()
    demo_home = Path(args.home)
    if not demo_home.is_absolute():
        demo_home = (REPO / demo_home).resolve()

    E2E.mkdir(parents=True, exist_ok=True)
    if not demo_home.exists():
        raise SystemExit(
            f"缺少演示工作台 {demo_home}；先跑："
            f".\\.venv\\Scripts\\python.exe scripts\\demo.py --home {demo_home}"
        )
    if not EMPTY_HOME.exists():
        raise SystemExit(f"缺少空工作台 {EMPTY_HOME}")

    started = time.time()
    report: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "mode": args.mode, "home": str(demo_home)}

    if args.mode in ("dom", "all"):
        log("[1/3] 截图 + DOM 断言 …")
        report["dom_phase"] = run_dom_phase(demo_home)
    if args.mode in ("layout", "all"):
        log("[2/3] 三尺寸布局 / 横向溢出 / 关键 id 内容 …")
        report["layout_phase"] = run_layout_phase(demo_home)
    if args.mode in ("cdp", "all"):
        log("[3/3] CDP 真实点击 + console 错误 …")
        report["cdp_phase"] = run_cdp_phase()

    report["elapsed_sec"] = round(time.time() - started, 1)

    failures: list[str] = []
    dom_phase = report.get("dom_phase") or {}
    for shot in dom_phase.get("screenshots", []):
        if not shot["ok"]:
            failures.append(f"截图尺寸不符 {shot['path']}: {shot['actual']} != {shot['requested']}")
    for page, checks in (dom_phase.get("dom") or {}).items():
        for name, check in checks.items():
            if not check["ok"]:
                failures.append(f"DOM 断言失败 [{page}] {name}: {check['detail']}")

    layout_phase = report.get("layout_phase") or {}
    for size, data in (layout_phase.get("sizes") or {}).items():
        if not data.get("boot"):
            failures.append(f"布局检查 {size}: 首屏未就绪")
        if not data.get("viewport_exact"):
            failures.append(f"布局检查 {size}: 视口不是精确尺寸（inner={data.get('inner')}）")
        for view, entry in (data.get("views") or {}).items():
            if not entry.get("content_ok"):
                failures.append(f"布局检查 {size} / {view}: 关键容器无内容（{entry.get('expectation')}）{entry.get('data')}")
            if entry.get("overflow_px", 0) > 1:
                failures.append(
                    f"布局检查 {size} / {view}: 横向溢出 {entry['overflow_px']}px "
                    f"(doc={entry['doc_scroll_width']} > inner={entry['inner_width']})"
                )
            if entry.get("kanban_layout_ok") is False:
                failures.append(
                    f"布局检查 {size} / 看板列数不符契约 §4.1：实际 {entry.get('kanban_layout')} "
                    f"期望 {entry.get('kanban_layout_expected')}"
                )

    cdp = report.get("cdp_phase") or {}
    if cdp:
        if not cdp.get("ok"):
            failures.append(f"CDP 交互未通过：{json.dumps(cdp.get('steps'), ensure_ascii=False)[:400]}")
        if cdp.get("console_errors"):
            failures.append(f"console 错误 {len(cdp['console_errors'])} 条：{cdp['console_errors'][:3]}")
        if cdp.get("exceptions"):
            failures.append(f"未捕获异常 {len(cdp['exceptions'])} 条：{cdp['exceptions'][:3]}")
        settled = (cdp.get("steps") or {}).get("settled") or {}
        if "failed" in (settled.get("statuses") or []):
            failures.append(f"浏览器里真实派发的任务失败了：{settled}")

    report["failures"] = failures
    report["ok"] = not failures
    RESULTS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"\n结果：{'PASS' if report['ok'] else 'FAIL'}（{report['elapsed_sec']}s）→ {RESULTS}")
    for failure in failures:
        log(f"  ✗ {failure}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
