"""V2 React 前端的静态契约测试。

与 ``tests/test_web_contract.py``（零构建 ``multiagent/web/app.js``）互补：
本文件守住 ``webapp/src`` 这份新前端，防止「界面能打开、按钮调用了不存在的接口」
这类只能在浏览器里才暴露的漂移。

覆盖：
1. ``webapp/src`` 里出现的每个 ``/api/...`` 字面量都必须能命中后端真实路由；
2. ``multiagent/web/dist`` 必须已构建，且 ``GET /`` 返回 HTML；
3. 静态安全护栏：禁止 ``dangerouslySetInnerHTML`` / ``innerHTML =`` / ``eval(``，
   ``localStorage`` 不得写入新的 token 键；
4. 设计令牌：13px 基准字号，``--text-muted`` 对 ``--surface`` 对比度 >= 3.0；
5. 每个顶层视图模块都导出组件，且 ``App.tsx`` 全部引入。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "webapp" / "src"
DIST_INDEX = REPO / "multiagent" / "web" / "dist" / "index.html"
APP_TSX = SRC / "app" / "App.tsx"
TOKENS_CSS = SRC / "styles" / "tokens.css"

#: 模板变量归一化后的占位符，与旧契约测试保持一致。
PLACEHOLDER = "probe-id"

#: 顶层视图：目录 -> (组件名, 文件相对路径)
TOP_LEVEL_VIEWS: dict[str, tuple[str, str]] = {
    "overview": ("OverviewView", "features/overview/OverviewView.tsx"),
    "tasks": ("TasksView", "features/tasks/TasksView.tsx"),
    "task": ("TaskDetailView", "features/task/TaskDetailView.tsx"),
    "runs": ("RunsView", "features/runs/RunsView.tsx"),
    "files": ("FilesView", "features/files/FilesView.tsx"),
    "review": ("ReviewView", "features/review/ReviewView.tsx"),
    "memory": ("MemoryView", "features/memory/MemoryView.tsx"),
    "agents": ("AgentsView", "features/agents/AgentsView.tsx"),
    "settings": ("SettingsView", "features/settings/SettingsView.tsx"),
    "onboarding": ("WelcomeView", "features/onboarding/WelcomeView.tsx"),
}

#: 兼容旧版桌面壳/浏览器书签的 token 键；除此之外不得写入 token。
COMPAT_TOKEN_KEYS = {"multiagent_token", "multiagent.token"}


# --------------------------------------------------------------------------- TS 扫描

_QUOTES = "\"'`"
_TERMINATORS = set(" \t\r\n,;<>|)]}")


def _source_files() -> list[Path]:
    return sorted(
        path
        for path in SRC.rglob("*")
        if path.is_file() and path.suffix in {".ts", ".tsx"}
    )


def _skip_quoted(text: str, start: int) -> int:
    """返回从 ``start`` 处引号开始、到对应闭引号之后的位置。"""
    quote = text[start]
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if quote == "`" and char == "$" and index + 1 < len(text) and text[index + 1] == "{":
            index = _find_expr_end(text, index + 1) + 1
            continue
        if char == quote:
            return index + 1
        index += 1
    return index


def _find_expr_end(text: str, open_brace: int) -> int:
    """找到 ``${`` 对应的 ``}``（跳过字符串与嵌套对象）。"""
    depth = 0
    index = open_brace
    while index < len(text):
        char = text[index]
        if char in _QUOTES:
            index = _skip_quoted(text, index)
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return len(text) - 1


def _string_literals(text: str) -> list[str]:
    """提取一段表达式里的字符串字面量内容（用于展开三元分支）。"""
    found: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char in _QUOTES:
            end = _skip_quoted(text, index)
            found.append(text[index + 1 : end - 1])
            index = end
        else:
            index += 1
    return found


def _has_top_level(text: str, needle: str) -> bool:
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in _QUOTES:
            index = _skip_quoted(text, index)
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and char == needle:
            return True
        index += 1
    return False


def _expr_replacements(expr: str) -> list[str] | None:
    """把 ``${...}`` 展开成路径片段。

    返回 ``None`` 表示这个表达式属于查询串（例如 ``${suffix ? `?${suffix}` : ""}``），
    路径到此为止；否则返回一个或多个可替换片段。
    """
    literals = _string_literals(expr)
    # 嵌套模板里出现以 ? / & 开头的字符串，说明从这里开始是 query。
    if any(item.lstrip().startswith(("?", "&")) for item in literals):
        return None
    # ``running ? "stop" : "run"`` 这类三元式同时代表两条真实路由。
    if _has_top_level(expr, "?") and len(literals) == 2:
        return literals
    return [PLACEHOLDER]


def _read_path_variants(text: str, start: int) -> list[str]:
    """从 ``/api/`` 起始位置扫描一个字符串/模板字面量中的所有路径变体。"""
    paths = [""]
    index = start
    while index < len(text):
        char = text[index]
        if char in _QUOTES or char in _TERMINATORS or char in "?#":
            break
        if char == "$" and index + 1 < len(text) and text[index + 1] == "{":
            end = _find_expr_end(text, index + 1)
            replacements = _expr_replacements(text[index + 2 : end])
            if replacements is None:
                break
            paths = [prefix + value for prefix in paths for value in replacements]
            index = end + 1
            continue
        paths = [prefix + char for prefix in paths]
        index += 1
    return paths


def _is_comment_line(text: str, position: int) -> bool:
    line_start = text.rfind("\n", 0, position) + 1
    return text[line_start:position].lstrip().startswith(("//", "/*", "*"))


def _normalize(path: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = re.sub(r"/{2,}", "/", path)
    return path.rstrip("/") or "/"


def _frontend_api_paths() -> set[str]:
    found: set[str] = set()
    for file in _source_files():
        text = file.read_text(encoding="utf-8", errors="replace")
        cursor = 0
        while True:
            index = text.find("/api/", cursor)
            if index < 0:
                break
            # 只接受真正以 /api/ 开头的字符串（排除 "../../api/client" 这类 import 路径）。
            if index == 0 or text[index - 1] not in _QUOTES:
                cursor = index + 5
                continue
            if _is_comment_line(text, index):
                cursor = index + 5
                continue
            for raw in _read_path_variants(text, index):
                path = _normalize(raw)
                if path == "/api" or not path.startswith("/api/"):
                    continue
                found.add(path)
            cursor = index + 5
    return found


# --------------------------------------------------------------------------- 路由

def _route_table(api) -> list[tuple[str, set[str]]]:
    routes: list[tuple[str, set[str]]] = []
    for route in api.routes:
        path = getattr(route, "path", "") or ""
        methods = set(getattr(route, "methods", None) or set())
        routes.append((path, methods))
    return routes


def _routable(routes: list[tuple[str, set[str]]], path: str) -> bool:
    for route_path, methods in routes:
        if not route_path.startswith("/api/") or not methods:
            continue
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", route_path) + "$"
        if re.match(pattern, path):
            return True
    return False


# --------------------------------------------------------------------------- 对比度

def _read_root_vars() -> dict[str, str]:
    css = TOKENS_CSS.read_text(encoding="utf-8", errors="replace")
    match = re.search(r":root\s*\{(.*?)\}", css, re.S)
    assert match, "tokens.css 缺少 :root 设计令牌块"
    return {
        key: value.strip()
        for key, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", match.group(1))
    }


def _hex_to_rgb(value: str) -> tuple[float, float, float]:
    text = value.strip().lstrip("#")
    assert re.fullmatch(r"[0-9a-fA-F]{6}", text), f"不是 6 位十六进制颜色：{value}"
    return tuple(int(text[i : i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def _luminance(rgb: tuple[float, float, float]) -> float:
    channels = []
    for value in rgb:
        channels.append(value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4)
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _contrast(foreground: str, background: str) -> float:
    light = _luminance(_hex_to_rgb(foreground))
    dark = _luminance(_hex_to_rgb(background))
    high, low = max(light, dark), min(light, dark)
    return (high + 0.05) / (low + 0.05)


# --------------------------------------------------------------------------- 用例

def test_frontend_uses_only_routable_api_paths(wb):
    from multiagent.api.app import create_api

    api = create_api(wb)
    routes = _route_table(api)
    paths = _frontend_api_paths()

    assert paths, "webapp/src 里没有扫描到任何 /api 路径，扫描器可能失效"
    missing = sorted(path for path in paths if not _routable(routes, path))
    assert not missing, (
        f"前端引用了 {len(missing)} 个后端不存在的接口（按钮会点了没反应）：\n  "
        + "\n  ".join(missing)
    )


def test_dist_index_is_built_and_root_serves_html(wb):
    from multiagent.api.app import create_api

    assert DIST_INDEX.exists(), f"缺少构建产物 {DIST_INDEX}，请先运行 webapp 构建"
    html = DIST_INDEX.read_text(encoding="utf-8", errors="replace")
    assert "/static/dist/assets/" in html, "dist/index.html 没有引用 /static/dist/assets/ 产物"

    client = TestClient(create_api(wb))
    response = client.get("/")
    assert response.status_code == 200, f"GET / 返回 {response.status_code}"
    content_type = response.headers.get("content-type", "")
    assert content_type.lower().startswith("text/html"), f"GET / Content-Type 不是 HTML：{content_type}"
    assert "<div id=\"root\"></div>" in response.text, "GET / 返回的不是构建后的 React 入口"


def test_frontend_has_no_dangerous_html_or_eval():
    offenders: list[str] = []
    for file in _source_files():
        text = file.read_text(encoding="utf-8", errors="replace")
        relative = file.relative_to(REPO)
        for line_number, line in enumerate(text.splitlines(), 1):
            if "dangerouslySetInnerHTML" in line:
                offenders.append(f"{relative}:{line_number}: dangerouslySetInnerHTML")
            if re.search(r"\.innerHTML\s*=", line):
                offenders.append(f"{relative}:{line_number}: innerHTML =")
            if re.search(r"\beval\s*\(", line):
                offenders.append(f"{relative}:{line_number}: eval(")
    assert not offenders, "发现不安全的 HTML/JS 执行用法：\n  " + "\n  ".join(offenders)


def test_no_new_localstorage_token_keys():
    writes: list[tuple[Path, str]] = []
    for file in _source_files():
        text = file.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"localStorage\.setItem\s*\(\s*", text):
            index = match.end()
            if index >= len(text) or text[index] not in _QUOTES:
                continue
            end = _skip_quoted(text, index)
            writes.append((file, text[index + 1 : end - 1]))

    bad = [
        (file.relative_to(REPO), key)
        for file, key in writes
        if re.search(r"(token|secret|password|api[_-]?key|auth)", key, re.I)
        and key not in COMPAT_TOKEN_KEYS
    ]
    assert not bad, "发现新的 localStorage token 写入：\n  " + "\n  ".join(
        f"{file}: {key}" for file, key in bad
    )


def test_design_tokens_are_accessible():
    variables = _read_root_vars()
    assert variables.get("--fs-base") == "13px", (
        f"--fs-base 应为 13px，实际为 {variables.get('--fs-base')!r}"
    )

    for name in ("--text-muted", "--surface"):
        assert name in variables, f"tokens.css 缺少 {name}"

    ratio = _contrast(variables["--text-muted"], variables["--surface"])
    assert ratio >= 3.0, (
        f"--text-muted ({variables['--text-muted']}) 对 --surface "
        f"({variables['--surface']}) 对比度仅 {ratio:.2f}:1，低于 3.0:1"
    )


def test_every_top_level_view_is_exported_and_imported():
    app_text = APP_TSX.read_text(encoding="utf-8", errors="replace")
    problems: list[str] = []

    for folder, (component, relative) in TOP_LEVEL_VIEWS.items():
        file = SRC / relative
        if not file.exists():
            problems.append(f"缺少视图文件 {relative}")
            continue
        text = file.read_text(encoding="utf-8", errors="replace")
        exported = re.search(
            rf"export\s+(?:default\s+)?(?:function|const|class)\s+{component}\b",
            text,
        ) or re.search(rf"export\s*\{{[^}}]*\b{component}\b", text)
        if not exported:
            problems.append(f"{relative} 没有导出组件 {component}")
        # 兼容静态 import 与 React.lazy 的动态 import(...) 两种写法。
        imported = component in app_text and re.search(
            rf"features/{re.escape(folder)}/", app_text
        )
        if not imported:
            problems.append(f"App.tsx 没有从 features/{folder}/ 引入 {component}")

    assert not problems, "顶层视图契约不完整：\n  " + "\n  ".join(problems)
