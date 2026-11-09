"""Headless-Edge smoke test for the MultiAgent v2 web UI.

What it does
------------
* launches Microsoft Edge in headless mode with a throw-away profile and a
  remote-debugging port (all ports are chosen dynamically);
* navigates to the running backend (default http://127.0.0.1:8771/);
* records Runtime.exceptionThrown, console.error/assert and Log entries;
* waits for the app shell and a network-idle-ish moment;
* clicks every sidebar entry and asserts the page heading changes, the entry
  becomes active and no uncaught exception is raised;
* opens the "new task" dialog, asserts the textarea is focused and Escape
  closes it;
* opens the command palette with Ctrl+K, types a query and closes with Escape;
* stores one 1440x900 screenshot per view in .e2e/ui-v2/;
* prints a PASS/FAIL summary and exits non-zero when a hard check fails.

Only the standard library plus ``websockets`` is used.  The script never
imports application code and never mutates application state on purpose.

Examples
--------
    .\\.venv\\Scripts\\python.exe scripts\\v2_smoke.py --port 8771
    .\\.venv\\Scripts\\python.exe scripts\\v2_smoke.py --url http://127.0.0.1:8771/ \
        --out-dir .e2e/ui-v2 --timeout 25
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import websockets

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / ".e2e" / "ui-v2"
VIEWPORT = (1440, 900)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# label shown in the sidebar -> expected substring of main h1 (None = dynamic)
NAV_ITEMS: list[tuple[str, str, str | None]] = [
    ("projects", "项目", "项目"),
    ("overview", "概览", None),
    ("tasks", "任务", "任务"),
    ("runs", "运行", "运行记录"),
    ("files", "文件", "文件"),
    ("review", "审阅", "审阅队列"),
    ("memory", "记忆", "记忆"),
    ("agents", "Agent", "Agent 编队"),
    ("settings", "设置", "设置"),
]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def log(message: str) -> None:
    print(message, flush=True)


def find_edge() -> str:
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise SystemExit("找不到 Microsoft Edge，可用 --edge 指定 msedge.exe 路径")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def http_json(url: str, method: str = "GET", timeout: float = 3.0) -> Any:
    request = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_health(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=1.5
            ) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - backend may still be booting
            pass
        time.sleep(0.25)
    return False


def wait_cdp(port: int, timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return http_json(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        except Exception:  # noqa: BLE001 - Edge may still be starting
            time.sleep(0.2)
    return None


# --------------------------------------------------------------------------- #
# recording / reporting
# --------------------------------------------------------------------------- #


@dataclass
class Failure:
    where: str
    message: str


@dataclass
class Recorder:
    failures: list[Failure] = field(default_factory=list)
    exceptions: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    network_warnings: list[str] = field(default_factory=list)
    other_log_warnings: list[str] = field(default_factory=list)
    checks: int = 0
    passed: int = 0

    def check(self, where: str, condition: bool, message: str) -> bool:
        self.checks += 1
        if condition:
            self.passed += 1
            log(f"  PASS  [{where}] {message}")
            return True
        self.failures.append(Failure(where, message))
        log(f"  FAIL  [{where}] {message}")
        return False

    def exception_count(self) -> int:
        return len(self.exceptions)


def _describe_exception(params: dict[str, Any]) -> str:
    details = params.get("exceptionDetails") or {}
    exception = details.get("exception") or {}
    text = (
        exception.get("description")
        or details.get("text")
        or exception.get("value")
        or "unknown exception"
    )
    url = details.get("url") or ""
    line = details.get("lineNumber")
    column = details.get("columnNumber")
    location = ""
    if url:
        location = f" ({url}:{line}:{column})"
    return f"{text}{location}"


def _describe_console(params: dict[str, Any]) -> str:
    kind = params.get("type") or "error"
    parts = []
    for arg in params.get("args") or []:
        value = arg.get("value")
        if value is None:
            value = arg.get("description") or arg.get("unserializableValue") or arg.get("type")
        parts.append(str(value))
    return f"console.{kind}: {' '.join(parts)[:500]}"


class CDP:
    """Minimal CDP client: request/response plus event callbacks."""

    def __init__(self, ws: Any, recorder: Recorder) -> None:
        self.ws = ws
        self.recorder = recorder
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._closed = False
        self._inflight: set[str] = set()

    async def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        self._closed = True
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _read_loop(self) -> None:
        while not self._closed:
            try:
                raw = await self.ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - socket closed
                break
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            message_id = message.get("id")
            if message_id is not None:
                future = self._pending.pop(int(message_id), None)
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            self._on_event(message.get("method", ""), message.get("params") or {})

    def _on_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Runtime.exceptionThrown":
            text = _describe_exception(params)
            self.recorder.exceptions.append(text)
            log(f"  !! uncaught exception: {text[:400]}")
        elif method == "Runtime.consoleAPICalled" and params.get("type") in {"error", "assert"}:
            text = _describe_console(params)
            self.recorder.console_errors.append(text)
            log(f"  !! {text[:400]}")
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") != "error":
                return
            text = f"{entry.get('source', 'log')}: {entry.get('text', '')}"
            url = entry.get("url")
            if url:
                text += f" ({url})"
            if "favicon.ico" in text:
                return
            if entry.get("source") == "network":
                self.recorder.network_warnings.append(text)
            else:
                self.recorder.other_log_warnings.append(text)
                log(f"  !! log error: {text[:400]}")
        elif method == "Network.requestWillBeSent":
            request_id = params.get("requestId")
            if request_id:
                self._inflight.add(str(request_id))
        elif method in {"Network.loadingFinished", "Network.loadingFailed"}:
            self._inflight.discard(str(params.get("requestId")))

    @property
    def pending_requests(self) -> int:
        return len(self._inflight)

    async def send(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 20.0
    ) -> dict[str, Any]:
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        await self.ws.send(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        try:
            message = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(message_id, None)
        if "error" in message:
            raise RuntimeError(f"{method} failed: {message['error']}")
        return message.get("result") or {}

    async def eval(self, expression: str, timeout: float = 20.0) -> Any:
        result = await self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(_describe_exception(result))
        return (result.get("result") or {}).get("value")

    async def poll(
        self,
        expression: str,
        timeout: float,
        interval: float = 0.12,
        predicate: Any | None = None,
    ) -> Any:
        """Poll ``expression`` until it is truthy (or predicate(value))."""
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = await self.eval(expression)
            except Exception:  # noqa: BLE001 - page may be navigating
                last = None
            if predicate is not None:
                if last is not None and predicate(last):
                    return last
            elif last:
                return last
            await asyncio.sleep(interval)
        return last

    async def key(self, key: str, code: str, vk: int, modifiers: int = 0) -> None:
        for kind in ("rawKeyDown", "keyUp"):
            await self.send(
                "Input.dispatchKeyEvent",
                {
                    "type": kind,
                    "key": key,
                    "code": code,
                    "windowsVirtualKeyCode": vk,
                    "nativeVirtualKeyCode": vk,
                    "modifiers": modifiers,
                },
            )
            await asyncio.sleep(0.03)

    async def screenshot(self, path: Path) -> int:
        result = await self.send(
            "Page.captureScreenshot",
            {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
            timeout=30,
        )
        data = result.get("data")
        if not data:
            raise RuntimeError("captureScreenshot returned no data")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))
        return path.stat().st_size


# --------------------------------------------------------------------------- #
# UI helpers (JS snippets)
# --------------------------------------------------------------------------- #


def js_click_nav(label: str) -> str:
    return f"""
    (() => {{
      const label = {json.dumps(label, ensure_ascii=False)};
      const items = [...document.querySelectorAll('.nav-item')];
      const target = items.find((el) => el.textContent.trim().startsWith(label));
      if (!target) return false;
      target.click();
      return true;
    }})()
    """


HEADING_JS = """
(() => {
  const el = document.querySelector('main h1');
  return el ? el.textContent.replace(/\\s+/g, ' ').trim() : '';
})()
"""

ACTIVE_NAV_JS = """
(() => {
  const el = document.querySelector('.nav-item.is-active');
  return el ? el.textContent.trim() : '';
})()
"""


async def wait_ready(cdp: CDP, timeout: float) -> str:
    """Wait for .app or .welcome; returns which one appeared ('' on timeout)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = await cdp.eval(
                "document.querySelector('.app') ? 'app' : "
                "(document.querySelector('.welcome') ? 'welcome' : '')"
            )
        except Exception:  # noqa: BLE001
            state = ""
        if state:
            return str(state)
        await asyncio.sleep(0.15)
    return ""


async def wait_network_quiet(cdp: CDP, timeout: float, quiet_for: float = 0.5) -> None:
    """Best-effort network idle: no in-flight requests for ``quiet_for`` seconds."""
    deadline = time.monotonic() + timeout
    quiet_since: float | None = None
    while time.monotonic() < deadline:
        if cdp.pending_requests == 0:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= quiet_for:
                return
        else:
            quiet_since = None
        await asyncio.sleep(0.1)


def configure_edge(edge: str, port: int, profile: Path) -> list[str]:
    width, height = VIEWPORT
    return [
        edge,
        "--headless=new",
        "--disable-gpu",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--remote-allow-origins=*",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "about:blank",
    ]


# --------------------------------------------------------------------------- #
# main flow
# --------------------------------------------------------------------------- #


async def exercise(url: str, recorder: Recorder, out_dir: Path, timeout: float) -> None:
    edge = find_edge()
    profile = Path(tempfile.mkdtemp(prefix="multiagent-v2-smoke-"))
    debug_port = free_port()
    process = subprocess.Popen(
        configure_edge(edge, debug_port, profile),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    cdp: CDP | None = None
    ws = None
    try:
        if not wait_cdp(debug_port, timeout):
            recorder.check("browser", False, f"Edge CDP {debug_port} 未就绪")
            return

        target = http_json(
            f"http://127.0.0.1:{debug_port}/json/new?"
            + urllib.parse.quote("about:blank", safe=""),
            method="PUT",
            timeout=10,
        )
        ws = await websockets.connect(
            target["webSocketDebuggerUrl"],
            max_size=64 << 20,
            open_timeout=10,
            ping_interval=None,
        )
        cdp = CDP(ws, recorder)
        await cdp.start()

        await cdp.send("Page.enable")
        await cdp.send("Runtime.enable")
        await cdp.send("Log.enable")
        await cdp.send("Network.enable")
        await cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": VIEWPORT[0],
                "height": VIEWPORT[1],
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )

        log(f"\n== navigate {url} ==")
        await cdp.send("Page.navigate", {"url": url})
        state = await wait_ready(cdp, timeout)
        recorder.check("boot", state in {"app", "welcome"}, f"应用外壳出现（{state or 'timeout'}）")
        if state != "app":
            if state == "welcome":
                recorder.failures.append(
                    Failure("boot", "当前后端没有项目，界面停留在欢迎页，侧边导航无法测试")
                )
            return

        await wait_network_quiet(cdp, timeout=min(10.0, timeout))
        heading = await cdp.poll(HEADING_JS, timeout=timeout, predicate=lambda v: bool(v))
        recorder.check("boot", bool(heading), f"主标题渲染（{heading or '未找到 main h1'}）")
        await asyncio.sleep(0.3)

        # ---- per-view navigation -------------------------------------------
        previous = ""
        for key, label, expected in NAV_ITEMS:
            where = key
            before = recorder.exception_count()
            log(f"\n== view: {label} ==")
            clicked = await cdp.eval(js_click_nav(label))
            if not recorder.check(where, bool(clicked), f"点击侧栏「{label}」"):
                continue

            new_heading = ""
            deadline = time.monotonic() + min(10.0, timeout)
            while time.monotonic() < deadline:
                candidate = await cdp.poll(
                    HEADING_JS, timeout=2.0, predicate=lambda v: bool(v)
                )
                if candidate and candidate != previous:
                    new_heading = str(candidate)
                    break
                await asyncio.sleep(0.12)
            if not new_heading:
                new_heading = str(await cdp.eval(HEADING_JS) or "")

            recorder.check(
                where,
                bool(new_heading) and new_heading != previous,
                f"主标题变化（{previous or '-'} -> {new_heading or '空'}）",
            )

            if expected is not None:
                recorder.check(
                    where,
                    expected in new_heading,
                    f"标题匹配「{expected}」（实际：{new_heading or '空'}）",
                )
            else:
                project_name = str(
                    await cdp.eval(
                        "document.querySelector('.project-name')?.textContent?.trim() || ''"
                    )
                    or ""
                )
                recorder.check(
                    where,
                    bool(project_name) and new_heading == project_name,
                    f"概览标题等于当前项目名（{new_heading or '空'} vs {project_name or '空'}）",
                )

            active = str(await cdp.eval(ACTIVE_NAV_JS) or "")
            recorder.check(
                where,
                active.startswith(label),
                f"侧栏高亮「{label}」（实际：{active or '无'}）",
            )

            await asyncio.sleep(0.35)
            shot = out_dir / f"{key}.png"
            try:
                size = await cdp.screenshot(shot)
                recorder.check(where, size > 1_000, f"截图 {shot.name}（{size} B）")
            except Exception as error:  # noqa: BLE001
                recorder.check(where, False, f"截图 {shot.name} 失败：{error}")

            new_exceptions = recorder.exception_count() - before
            recorder.check(where, new_exceptions == 0, f"无新增未捕获异常（新增 {new_exceptions}）")
            previous = new_heading

        # ---- new task dialog ------------------------------------------------
        log("\n== dialog: 新建任务 ==")
        before = recorder.exception_count()
        opened = await cdp.eval(
            """
            (() => {
              const buttons = [...document.querySelectorAll('.topbar button')];
              const target = buttons.find((el) => el.textContent.trim() === '新任务');
              if (!target || target.disabled) return false;
              target.click();
              return true;
            })()
            """
        )
        recorder.check("new-task", bool(opened), "点击顶栏「新任务」")
        dialog = await cdp.poll(
            "Boolean(document.querySelector('[role=\"dialog\"] textarea.newtask-input'))",
            timeout=min(8.0, timeout),
        )
        if recorder.check("new-task", bool(dialog), "新建任务对话框打开"):
            focused = await cdp.poll(
                "document.activeElement === document.querySelector("
                "'[role=\"dialog\"] textarea.newtask-input')",
                timeout=min(2.0, timeout),
            )
            active_info = await cdp.eval(
                """
                (() => {
                  const el = document.activeElement;
                  if (!el) return 'none';
                  const text = (el.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
                  return [el.tagName.toLowerCase(), el.className || '', el.getAttribute('aria-label') || '', text]
                    .filter(Boolean).join(' | ');
                })()
                """
            )
            recorder.check(
                "new-task",
                bool(focused),
                f"textarea 自动获得焦点（实际焦点：{active_info or 'unknown'}）",
            )
            try:
                size = await cdp.screenshot(out_dir / "new-task.png")
                recorder.check("new-task", size > 1_000, f"截图 new-task.png（{size} B）")
            except Exception as error:  # noqa: BLE001
                recorder.check("new-task", False, f"截图 new-task.png 失败：{error}")

            await cdp.key("Escape", "Escape", 27)
            closed = await cdp.poll(
                "!document.querySelector('[role=\"dialog\"] textarea.newtask-input')",
                timeout=min(6.0, timeout),
            )
            recorder.check("new-task", bool(closed), "Escape 关闭对话框")
        recorder.check(
            "new-task",
            recorder.exception_count() - before == 0,
            "对话框流程无新增未捕获异常",
        )

        # ---- command palette ------------------------------------------------
        log("\n== command palette (Ctrl+K) ==")
        before = recorder.exception_count()
        await cdp.key("k", "KeyK", 75, modifiers=2)
        palette = await cdp.poll(
            "Boolean(document.querySelector('[role=\"dialog\"] .command-dialog input, "
            ".command-dialog input'))",
            timeout=min(8.0, timeout),
        )
        if recorder.check("palette", bool(palette), "Ctrl+K 打开命令面板"):
            await cdp.send("Input.insertText", {"text": "task"})
            await asyncio.sleep(0.25)
            value = str(
                await cdp.eval(
                    "(document.querySelector('.command-dialog input')?.value) || ''"
                )
                or ""
            )
            if value != "task":
                # fall back to a native setter + input event for controlled inputs
                await cdp.eval(
                    """
                    (() => {
                      const input = document.querySelector('.command-dialog input');
                      if (!input) return false;
                      const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'value').set;
                      setter.call(input, 'task');
                      input.dispatchEvent(new Event('input', { bubbles: true }));
                      return true;
                    })()
                    """
                )
                await asyncio.sleep(0.25)
                value = str(
                    await cdp.eval(
                        "(document.querySelector('.command-dialog input')?.value) || ''"
                    )
                    or ""
                )
            recorder.check("palette", value == "task", f"输入查询（实际值：{value or '空'}）")
            try:
                size = await cdp.screenshot(out_dir / "command-palette.png")
                recorder.check("palette", size > 1_000, f"截图 command-palette.png（{size} B）")
            except Exception as error:  # noqa: BLE001
                recorder.check("palette", False, f"截图 command-palette.png 失败：{error}")

            await cdp.key("Escape", "Escape", 27)
            closed = await cdp.poll(
                "!document.querySelector('.command-dialog')",
                timeout=min(6.0, timeout),
            )
            recorder.check("palette", bool(closed), "Escape 关闭命令面板")
        recorder.check(
            "palette",
            recorder.exception_count() - before == 0,
            "命令面板流程无新增未捕获异常",
        )

        # ---- back to overview for a stable final state ----------------------
        await cdp.eval(js_click_nav("概览"))
        await asyncio.sleep(0.3)

    finally:
        if cdp is not None:
            await cdp.close()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            process.terminate()
            process.wait(timeout=8)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        shutil.rmtree(profile, ignore_errors=True)


def summary(recorder: Recorder, out_dir: Path) -> int:
    log("\n" + "=" * 68)
    log("SMOKE SUMMARY")
    log("=" * 68)
    log(f"checks: {recorder.passed}/{recorder.checks} passed")
    log(f"screenshots: {out_dir}")

    if recorder.failures:
        log(f"\nFAILURES ({len(recorder.failures)}):")
        for failure in recorder.failures:
            log(f"  - [{failure.where}] {failure.message}")
    if recorder.exceptions:
        log(f"\nUNCAUGHT EXCEPTIONS ({len(recorder.exceptions)}):")
        for item in recorder.exceptions:
            log(f"  - {item[:800]}")
    if recorder.console_errors:
        log(f"\nCONSOLE ERRORS ({len(recorder.console_errors)}):")
        for item in recorder.console_errors:
            log(f"  - {item[:800]}")
    if recorder.network_warnings:
        log(f"\nNETWORK WARNINGS ({len(recorder.network_warnings)}, non-fatal):")
        seen: set[str] = set()
        for item in recorder.network_warnings:
            if item in seen:
                continue
            seen.add(item)
            log(f"  - {item[:500]}")
    if recorder.other_log_warnings:
        log(f"\nLOG ERRORS ({len(recorder.other_log_warnings)}):")
        for item in recorder.other_log_warnings:
            log(f"  - {item[:500]}")

    hard_failures = (
        len(recorder.failures) + len(recorder.exceptions) + len(recorder.console_errors)
    )
    status = "PASS" if hard_failures == 0 else "FAIL"
    log(f"\nRESULT: {status}")
    return 0 if hard_failures == 0 else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MultiAgent v2 UI smoke test (CDP)")
    parser.add_argument("--port", type=int, default=8771, help="backend port (default 8771)")
    parser.add_argument("--url", default=None, help="full URL; overrides --port")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="screenshot directory")
    parser.add_argument("--timeout", type=float, default=25.0, help="per-step timeout seconds")
    parser.add_argument("--edge", default=None, help="path to msedge.exe")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    if args.edge:
        EDGE_CANDIDATES.insert(0, args.edge)
    url = args.url or f"http://127.0.0.1:{args.port}/"
    host_port = urllib.parse.urlparse(url).port or 80
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not wait_health(host_port, timeout=min(15.0, args.timeout)):
        log(f"FAIL: 后端 {host_port} /api/health 不可用")
        return 2

    recorder = Recorder()
    log(f"MultiAgent v2 smoke @ {url}  viewport={VIEWPORT[0]}x{VIEWPORT[1]}")
    try:
        asyncio.run(exercise(url, recorder, out_dir, args.timeout))
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except Exception as error:  # noqa: BLE001 - report and fail cleanly
        recorder.failures.append(Failure("harness", f"{type(error).__name__}: {error}"))
        log(f"\nHARNESS ERROR: {type(error).__name__}: {error}")
    return summary(recorder, out_dir)


if __name__ == "__main__":
    raise SystemExit(main())
