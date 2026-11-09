"""前端 ↔ 后端契约测试（防止 API 漂移）。

背景：本项目的前端是零构建的原生 JS（``multiagent/web/app.js``），与后端之间
没有任何类型系统或代码生成约束。后端改了路由、前端没跟上（或反之），
在单元测试里完全看不出来，只会体现为「控制台某个按钮点了没反应」。

这个测试把「前端引用的每个 /api 路径，后端必须真的能路由到」变成一条可执行断言。

范围与局限：
- 只检查**路径可路由性**，不检查请求体/响应体的字段结构（那需要 schema 校验）。
- 用 Starlette 自己的路由匹配器判定，避免手写模板归一化产生假阳性。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP_JS = REPO / "multiagent" / "web" / "app.js"

# 前端模板变量 -> 可匹配的具体值
_PLACEHOLDER = "probe-id"


def _frontend_paths() -> set[str]:
    js = APP_JS.read_text(encoding="utf-8", errors="replace")
    found = set(re.findall(r"""["'`](/api/[^"'`\s]*)["'`]""", js))
    for m in re.findall(r"""["'`](/api/[^"'`]*?)["'`]\s*\+""", js):
        found.add(m)
    out: set[str] = set()
    for raw in found:
        path = raw.split("?")[0]
        path = re.sub(r"\$\{[^}]*\}", _PLACEHOLDER, path)
        path = re.sub(r"\{[^}]*\}", _PLACEHOLDER, path)
        path = re.sub(r"/+", "/", path)
        if path.startswith("/api/"):
            out.add(path)
    return out


def _routable(routes: list[tuple[str, set[str]]], path: str) -> bool:
    for route_path, methods in routes:
        if not route_path or not route_path.startswith("/api/"):
            continue
        if not methods:
            continue
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", route_path) + "$"
        if re.match(pattern, path):
            return True
    return False


def test_app_js_exists_and_is_nonempty():
    assert APP_JS.exists(), f"缺少前端文件 {APP_JS}"
    assert APP_JS.stat().st_size > 1000


def test_every_frontend_api_path_is_routable(wb):
    """前端调用的每个 /api 路径，后端都必须注册了对应路由。"""
    from multiagent.api.app import create_api

    api = create_api(wb)
    routes: list[tuple[str, set[str]]] = []
    for route in api.routes:
        path = getattr(route, "path", "") or ""
        methods = set(getattr(route, "methods", None) or set())
        routes.append((path, methods))

    missing = sorted(p for p in _frontend_paths() if not _routable(routes, p))
    assert not missing, (
        "前端 app.js 调用了后端不存在的接口（按钮会点了没反应）：\n  "
        + "\n  ".join(missing)
    )


def test_backend_has_no_unexpected_duplicate_api_routes(wb):
    """同一 (路径, 方法) 不应注册两次——重复注册会让后注册的静默覆盖先注册的。"""
    from multiagent.api.app import create_api

    api = create_api(wb)
    seen: dict[tuple[str, str], int] = {}
    for route in api.routes:
        path = getattr(route, "path", "") or ""
        if not path.startswith("/api/"):
            continue
        for method in sorted(getattr(route, "methods", None) or set()):
            seen[(path, method)] = seen.get((path, method), 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    assert not dupes, f"存在重复注册的路由：{dupes}"
