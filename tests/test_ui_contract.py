"""前端 ↔ 契约静态护栏（``docs/UI-OPS-REDESIGN.md`` §1 / §2.1 / §4）。

与 ``tests/test_web_contract.py`` 的分工：

* ``test_web_contract.py``：只保证「app.js 里的每个 ``/api`` 路径后端能路由」。
* 本文件：保证**页面结构与交互契约**——DOM id 齐不齐、视图与导航是否配对、
  ``$("id")`` 引用的元素真的存在、``data-act`` 都有处理分支、表单控件可访问、
  静态资源路径正确、没有重复 id、设计令牌落地、轮询节流符合契约。

为什么值得单独写：这套前端是零构建的原生 JS，没有类型系统。元素 id 打错一个字母
（``home-metric`` vs ``home-metrics``）在单元测试里毫无症状，只表现为「某个区块永远空白」；
视图少了 ``#view-live`` 则表现为「点导航没反应」。这些都在浏览器里才暴露，成本很高。

这些断言全部是静态的（读文件 + Starlette 路由表），不启服务、不依赖浏览器，毫秒级。
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "multiagent" / "web"
INDEX = WEB / "index.html"
APP_JS = WEB / "app.js"
STYLE = WEB / "style.css"

#: 前端模板变量 -> 可匹配的具体值（与 test_web_contract 保持一致）
_PLACEHOLDER = "probe-id"

_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


# ===========================================================================
# 契约 §2.1：必须存在的 DOM id（按区域分组，失败信息直接指到区域）
# ===========================================================================

CONTRACT_IDS: dict[str, tuple[str, ...]] = {
    "topbar": (
        "topbar", "brand-name", "project-select", "btn-new-project", "btn-import-project",
        "btn-project-settings", "btn-refresh", "run-status", "desktop-badge",
    ),
    "sidebar": ("sidebar", "side-project-name", "side-project-path", "side-agents", "nav"),
    "view-home（指挥台）": (
        "view-home", "mission-text", "mission-outputs", "mission-agent", "mission-kind",
        "mission-submit", "mission-plan", "mission-templates",
        "home-metrics", "home-kanban", "home-activity", "home-onboarding",
    ),
    "view-tasks": ("view-tasks", "kanban"),
    "view-dag": ("view-dag", "dag"),
    "view-board": ("view-board", "bb-query", "bb-since", "bb-until", "bb-type", "bb-search", "bb-list"),
    "view-handoffs": ("view-handoffs", "handoff-list"),
    "view-approvals": ("view-approvals", "approval-list"),
    "view-snapshots": ("view-snapshots", "snap-label", "snap-a", "snap-b", "snap-list"),
    "view-agents": ("view-agents", "agents-list"),
    "view-live（实时事件流）": ("view-live", "live-list", "live-task", "live-autoscroll", "nav-live"),
    "view-observe": (
        "view-observe", "stats", "runs", "artifacts", "timeline", "artifact-role", "btn-export",
    ),
    "view-config": ("view-config", "config-file", "config-editor", "config-dir"),
    "inspector（右栏）": (
        "inspector", "inspector-agents", "btn-run", "btn-stop", "btn-recover", "btn-force-recover",
        "inspector-approvals", "inspector-running", "inspector-stream",
    ),
    "status strip（底部状态条）": (
        "status-ws", "status-agents", "status-tasks", "status-approvals", "status-autopilot",
    ),
    "保留的抽屉 / 弹窗 / toast": (
        "drawer", "drawer-title", "drawer-body", "drawer-close",
        "modal", "modal-title", "modal-body", "modal-foot", "toast",
    ),
    # 契约 §4.1：输入卡三个按钮；`mission-start`（创建并启动）由 app.js 直接绑定。
    "指挥台输入卡按钮（§4.1）": ("mission-start",),
}

ALL_CONTRACT_IDS: tuple[str, ...] = tuple(
    dict.fromkeys(ident for group in CONTRACT_IDS.values() for ident in group)
)

#: 所有视图 section 的 id（契约 §2.1 的 ``section.view#view-<name>``）。
VIEW_IDS = tuple(ident for ident in ALL_CONTRACT_IDS if ident.startswith("view-"))

#: 契约 §1 的设计令牌：必须作为 CSS 变量落地（否则「暖纸色 Ops 风格」无从谈起）。
OPS_TOKENS = (
    "--ops-paper", "--ops-paper-2", "--ops-card", "--ops-ink", "--ops-muted", "--ops-soft",
    "--ops-rule", "--ops-rule-strong", "--ops-accent", "--ops-accent-soft", "--ops-blue",
    "--ops-blue-soft", "--ops-green", "--ops-green-soft", "--ops-yellow", "--ops-red",
    "--ops-shadow", "--ops-mono", "--ops-display", "--ops-body",
)

#: 新前端必须真正调用的新接口（契约 §3.1/§3.2/§3.3）——只检查后端有路由是不够的，
#: 前端没调用等于没接上。
REQUIRED_NEW_CALLS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/projects/probe-id/dashboard"),
    ("POST", "/api/projects/probe-id/quick-task"),
    ("PATCH", "/api/projects/probe-id"),
    ("GET", "/api/agents/detail"),
)

#: 懒加载视图用到的既有接口（契约 §3.4 要求全部保留）。
LAZY_VIEW_CALLS: tuple[str, ...] = (
    "/api/projects/probe-id/kanban",
    "/api/projects/probe-id/board",
    "/api/projects/probe-id/handoffs",
    "/api/projects/probe-id/approvals",
    "/api/projects/probe-id/snapshots",
    "/api/projects/probe-id/stats",
    "/api/projects/probe-id/runs",
    "/api/projects/probe-id/timeline",
    "/api/projects/probe-id/artifacts",
)

#: ``$("id")`` / ``setText("id")`` 等直接按 id 取元素的辅助函数。
#: 这些 id 打错时没有任何报错，只会静默不渲染——所以必须逐一核对。
ID_HELPER_PATTERNS = (
    r"""\$\(\s*"([A-Za-z0-9_-]+)"\s*\)""",
    r"""setText\(\s*"([A-Za-z0-9_-]+)\"""",
    r"""setHtml\(\s*"([A-Za-z0-9_-]+)\"""",
    r"""setGridHtml\(\s*"([A-Za-z0-9_-]+)\"""",
    r"""setListHtml\(\s*"([A-Za-z0-9_-]+)\"""",
    r"""setStreamHtml\(\s*"([A-Za-z0-9_-]+)\"""",
    r"""bindClick\(\s*"([A-Za-z0-9_-]+)\"""",
    r"""bindChange\(\s*"([A-Za-z0-9_-]+)\"""",
)


# ===========================================================================
# 读取与解析
# ===========================================================================


def _read(path: Path) -> str:
    assert path.exists(), f"缺少前端文件 {path}"
    return path.read_text(encoding="utf-8", errors="strict")


class _HtmlIndex(HTMLParser):
    """够用的静态索引：id / 控件 / label / 资源 / data-* 属性。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.id_tags: dict[str, str] = {}
        self.id_classes: dict[str, str] = {}
        self.controls: list[dict] = []
        self.label_for: set[str] = set()
        self.scripts: list[dict] = []
        self.links: list[dict] = []
        self.data_acts: list[str] = []
        self.data_views: list[str] = []
        self._label_depth = 0
        self._stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        attr = {key: (value or "") for key, value in attrs}
        if tag == "label":
            self._label_depth += 1
            if attr.get("for"):
                self.label_for.add(attr["for"])
        if attr.get("id"):
            ident = attr["id"]
            self.ids.append(ident)
            self.id_tags[ident] = tag
            self.id_classes[ident] = attr.get("class", "")
        if tag in ("input", "select", "textarea"):
            self.controls.append({"tag": tag, "attrs": attr, "in_label": self._label_depth > 0})
        if tag == "script":
            self.scripts.append(attr)
        if tag == "link":
            self.links.append(attr)
        if attr.get("data-act"):
            self.data_acts.append(attr["data-act"])
        if attr.get("data-view"):
            self.data_views.append(attr["data-view"])
        if tag not in _VOID_TAGS:
            self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag == "label" and self._label_depth > 0:
            self._label_depth -= 1
        if self._stack and self._stack[-1] == tag:
            self._stack.pop()


@pytest.fixture(scope="module")
def html_index() -> _HtmlIndex:
    parser = _HtmlIndex()
    parser.feed(_read(INDEX))
    parser.close()
    return parser


@pytest.fixture(scope="module")
def js_source() -> str:
    return _read(APP_JS)


def _frontend_paths(js: str) -> set[str]:
    """从 app.js 里抽出所有 ``/api/...`` 字面量（含模板拼接的前缀与 ``${base}/x`` 相对拼接）。"""
    found: set[str] = set(re.findall(r"""["'`](/api/[^"'`\s]*)["'`]""", js))
    for match in re.findall(r"""["'`](/api/[^"'`]*?)["'`]\s*\+""", js):
        found.add(match)
    out: set[str] = set()
    for raw in found:
        path = raw.split("?")[0]
        path = re.sub(r"\$\{[^}]*\}", _PLACEHOLDER, path)
        path = re.sub(r"\{[^}]*\}", _PLACEHOLDER, path)
        path = re.sub(r"/+", "/", path)
        if path.startswith("/api/"):
            out.add(path)
    out |= _relative_api_paths(js)
    return out


#: 观测视图用 ``const base = `/api/projects/${pid}` `` 再 ``api(`${base}/stats`)`` 拼路径，
#: 字面量扫描看不到它们——这里按 base 的已知形状补回真实路径。
_BASE_DEF = re.compile(r"""base\s*=\s*`/api/projects/\$\{[^}]+\}`""")


def _relative_api_paths(js: str) -> set[str]:
    if not _BASE_DEF.search(js):
        return set()
    out = {
        f"/api/projects/{_PLACEHOLDER}/{suffix}"
        for suffix in re.findall(r"\$\{base\}/([A-Za-z0-9_\-/]+)", js)
    }
    if re.search(r"api\(\s*`\$\{base\}`", js):
        out.add(f"/api/projects/{_PLACEHOLDER}")
    return out


def _route_table(wb) -> list[tuple[str, set[str]]]:  # noqa: ANN001
    from multiagent.api.app import create_api

    api = create_api(wb)
    table: list[tuple[str, set[str]]] = []
    for route in api.routes:
        path = getattr(route, "path", "") or ""
        methods = set(getattr(route, "methods", None) or set())
        table.append((path, methods))
    return table


def _routable(table: list[tuple[str, set[str]]], path: str, method: str | None = None) -> bool:
    for route_path, methods in table:
        if not route_path or not route_path.startswith("/api/"):
            continue
        if method is not None and method not in methods:
            continue
        if not methods:
            continue
        pattern = "^" + re.sub(r"\{[^}]+\}", "[^/]+", route_path) + "$"
        if re.match(pattern, path):
            return True
    return False


def _dynamic_ids(js: str) -> set[str]:
    """app.js 自己渲染出来的元素 id（弹窗/抽屉/表单内容），不需要在 index.html 里。"""
    ids = set(re.findall(r"""id=\\?["']([A-Za-z0-9_-]+)\\?["']""", js))
    # 模板字符串里转义的引号（\`id="x"\`）已被上面覆盖；再兜一层反引号内联。
    return ids


def _data_act_values(js: str) -> set[str]:
    """app.js 生成的 data-act（含 ``data-act="${cond ? "a" : "b"}"`` 里的字面量）。

    注意不能用 ``data-act="([^"]*)"`` 这种朴素正则：模板里的三元表达式本身带双引号，
    会被截断成 ``${cond ? `` 而漏掉两个真实动作名——变异自检里专门钉了这条。
    """
    acts: set[str] = set()
    for match in re.finditer(r'data-act="', js):
        cursor = match.end()
        depth = 0
        start = cursor
        while cursor < len(js):
            char = js[cursor]
            if char == "$" and cursor + 1 < len(js) and js[cursor + 1] == "{":
                depth += 1
                cursor += 2
                continue
            if depth and char == "}":
                depth -= 1
                cursor += 1
                continue
            if depth == 0 and char == '"':
                break
            cursor += 1
        raw = js[start:cursor]
        if "${" in raw:
            acts.update(re.findall(r"""["']([a-z][a-z0-9-]+)["']""", raw))
        elif raw:
            acts.add(raw)
    return acts


def _case_labels(js: str) -> set[str]:
    return set(re.findall(r"""case\s+"([a-z][a-z0-9-]*)"\s*:""", js))


# --- 纯函数化的检查（测试与变异自检共用，保证断言本身不是空转） -----------------


def _parse_html(text: str) -> _HtmlIndex:
    parser = _HtmlIndex()
    parser.feed(text)
    parser.close()
    return parser


def _missing_ids(index: _HtmlIndex, group: str) -> list[str]:
    present = set(index.ids)
    return [ident for ident in CONTRACT_IDS[group] if ident not in present]


def _duplicate_ids(index: _HtmlIndex) -> dict[str, int]:
    seen: dict[str, int] = {}
    for ident in index.ids:
        seen[ident] = seen.get(ident, 0) + 1
    return {ident: count for ident, count in seen.items() if count > 1}


def _unlabelled_controls(index: _HtmlIndex) -> list[str]:
    unlabelled: list[str] = []
    for control in index.controls:
        attrs = control["attrs"]
        ident = attrs.get("id", "")
        if attrs.get("aria-label") or attrs.get("aria-labelledby"):
            continue
        if ident and ident in index.label_for:
            continue
        if control["in_label"]:
            continue
        if attrs.get("type") in {"hidden", "submit", "button"}:
            continue
        unlabelled.append(f"<{control['tag']} id={ident or '(无 id)'} name={attrs.get('name', '')}>")
    return unlabelled


# ===========================================================================
# 1) DOM id：契约 §2.1 全部存在、无重复、视图 section 结构正确
# ===========================================================================


@pytest.mark.parametrize("group", list(CONTRACT_IDS))
def test_contract_dom_ids_exist(group: str, html_index: _HtmlIndex):
    """契约 §2.1：每个区域要求的 DOM id 都必须在 index.html 里。"""
    missing = _missing_ids(html_index, group)
    assert not missing, (
        f"[{group}] index.html 缺少契约要求的 DOM id：{missing}\n"
        f"  这些 id 缺失时，app.js 里对应的 $('id') 会静默拿到 null，"
        f"表现为该区块永远空白或按钮点了没反应。"
    )


def test_index_html_has_no_duplicate_ids(html_index: _HtmlIndex):
    """重复 id 必须失败：getElementById 只会返回第一个，第二个永远渲染不到。"""
    dupes = _duplicate_ids(html_index)
    assert not dupes, f"index.html 存在重复 id（后者永远不会被 getElementById 命中）：{dupes}"


def test_view_sections_follow_contract_structure(html_index: _HtmlIndex):
    """契约 §2.1：视图必须是 ``section.view#view-<name>``。"""
    for ident in VIEW_IDS:
        assert ident in html_index.id_tags, f"缺少视图容器 #{ident}"
        tag = html_index.id_tags[ident]
        classes = html_index.id_classes.get(ident, "").split()
        assert tag == "section", f"#{ident} 应该是 <section>，实际是 <{tag}>"
        assert "view" in classes, f"#{ident} 缺少 .view 类（当前 class={classes}）"


def test_nav_buttons_and_views_are_paired(html_index: _HtmlIndex):
    """导航的 data-view 与 #view-<name> 必须一一对应（少了就是「点了没反应」）。"""
    nav_views = set(html_index.data_views)
    view_names = {ident[len("view-"):] for ident in VIEW_IDS}
    missing_views = sorted(name for name in nav_views if f"view-{name}" not in html_index.id_tags)
    missing_nav = sorted(name for name in view_names if name not in nav_views)
    assert not missing_views, f"导航 data-view 指向不存在的视图：{missing_views}"
    assert not missing_nav, f"存在视图没有导航入口：{missing_nav}"


# ===========================================================================
# 2) API 契约：app.js 调用的路径可路由，且新接口真的被调用
# ===========================================================================


def test_every_frontend_api_path_is_routable(wb, js_source: str):
    """app.js 里每个 ``/api`` 路径，后端都必须注册了对应路由。"""
    table = _route_table(wb)
    missing = sorted(path for path in _frontend_paths(js_source) if not _routable(table, path))
    assert not missing, (
        "前端调用了后端不存在的接口（按钮会点了没反应）：\n  " + "\n  ".join(missing)
    )


@pytest.mark.parametrize(("method", "path"), REQUIRED_NEW_CALLS)
def test_new_contract_endpoints_are_wired(wb, js_source: str, method: str, path: str):
    """契约 §3.1/§3.2/§3.3：新接口必须前端真的调用、后端真的注册（含方法）。"""
    called = _frontend_paths(js_source)
    assert path in called, f"app.js 没有调用 {method} {path}（新契约接口没接上）"
    assert _routable(_route_table(wb), path, method), f"后端没有注册 {method} {path}"


@pytest.mark.parametrize("path", LAZY_VIEW_CALLS)
def test_lazy_loaded_view_endpoints_still_exist(wb, js_source: str, path: str):
    """契约 §3.4：既有接口保持兼容——前端懒加载视图依赖它们。"""
    assert path in _frontend_paths(js_source), f"app.js 没有调用 {path}"
    assert _routable(_route_table(wb), path, "GET"), f"后端缺少 GET {path}"


def test_appjs_patches_project_for_rename(js_source: str):
    """契约 §3.3：项目改名必须走 PATCH /api/projects/{pid}（用户修正历史乱码名的入口）。"""
    pattern = re.compile(
        r"""api\(\s*`/api/projects/\$\{[^}]+\}`\s*,\s*\{[^}]*method:\s*["']PATCH["']""",
        re.S,
    )
    assert pattern.search(js_source), "app.js 没有用 PATCH 调用 /api/projects/{pid}（项目改名不可用）"


# ===========================================================================
# 3) DOM 引用完整性：$("id") 等必须指向真实存在的元素
# ===========================================================================


def test_static_id_references_resolve(html_index: _HtmlIndex, js_source: str):
    """app.js 用 id 取的所有元素，必须在 index.html 或它自己渲染的 HTML 里存在。

    动态弹窗内容（``id="tf-title"`` 之类）由 app.js 自己注入，允许不在 index.html；
    其余一律要求在页面里能找到，否则就是打错字/漏改。
    """
    known = set(html_index.ids) | _dynamic_ids(js_source)
    referenced: set[str] = set()
    for pattern in ID_HELPER_PATTERNS:
        referenced.update(re.findall(pattern, js_source))
    missing = sorted(ident for ident in referenced if ident not in known)
    assert not missing, (
        "app.js 按 id 取的元素在页面与动态模板里都找不到（会静默不渲染）：\n  "
        + "\n  ".join(missing)
    )


def test_key_ids_are_actually_used_by_js(html_index: _HtmlIndex, js_source: str):
    """反向检查：契约要求「有内容」的容器，JS 必须真的往里写数据。"""
    wired = {
        "mission-text": "指挥台任务输入",
        "mission-submit": "创建任务按钮",
        "mission-start": "创建并启动按钮",
        "home-metrics": "KPI 条",
        "home-kanban": "任务看板",
        "home-activity": "最近活动",
        "inspector-agents": "右侧 Agent 编队",
        "status-ws": "状态条 WS",
        "kanban": "任务视图看板",
        "live-list": "实时事件流",
    }
    for ident, purpose in wired.items():
        assert ident in html_index.ids, f"#{ident}（{purpose}）不在 index.html"
        assert f'"{ident}"' in js_source, f"app.js 从未引用 #{ident}（{purpose} 不会有内容）"


# ===========================================================================
# 4) 交互契约：data-act 都有处理分支
# ===========================================================================


def test_html_data_act_all_have_handlers(html_index: _HtmlIndex, js_source: str):
    """index.html 里写死的 data-act，必须在 handleAction 的 switch 里有 case。"""
    handlers = _case_labels(js_source)
    unhandled = sorted(set(html_index.data_acts) - handlers)
    assert not unhandled, f"这些按钮没有处理分支（点了没反应）：{unhandled}"


def test_generated_data_act_all_have_handlers(js_source: str):
    """app.js 动态生成的 data-act 同样要有 case（列表里的按钮也是按钮）。"""
    handlers = _case_labels(js_source)
    unhandled = sorted(_data_act_values(js_source) - handlers)
    assert not unhandled, f"app.js 生成了没有处理分支的 data-act：{unhandled}"


def test_contract_required_actions_exist(html_index: _HtmlIndex, js_source: str):
    """契约 §4.1/§2.1 点名的动作：新建任务、导入计划、一句话三种模式。"""
    required = {"new-task", "import-plan", "mission-create", "mission-start", "mission-plan"}
    present = set(html_index.data_acts) | _data_act_values(js_source)
    assert required <= present, f"缺少契约要求的动作按钮：{sorted(required - present)}"
    assert required <= _case_labels(js_source), f"动作缺少处理分支：{sorted(required - _case_labels(js_source))}"


# ===========================================================================
# 5) 静态资源路径 / 可访问性 / 编码
# ===========================================================================


def test_script_and_link_paths_are_local_and_exist(html_index: _HtmlIndex):
    """index.html 的 <script>/<link> 必须是本地 /static 且文件真的存在。"""
    assert html_index.scripts, "index.html 没有 <script>（页面不会有任何交互）"
    for attrs in html_index.scripts:
        src = attrs.get("src", "")
        assert src, f"发现内联 <script>（CSP 禁止）：{attrs}"
        assert src.startswith("/static/"), f"脚本必须来自本地 /static：{src}"
        target = WEB / src[len("/static/"):]
        assert target.is_file(), f"脚本文件不存在：{src} -> {target}"

    for attrs in html_index.links:
        href = attrs.get("href", "")
        if not href:
            continue
        assert not re.match(r"^(https?:)?//", href), f"样式表不得引用外部资源（离线可用性）：{href}"
        if href.startswith("/static/"):
            target = WEB / href[len("/static/"):]
            assert target.is_file(), f"样式文件不存在：{href} -> {target}"


def test_form_controls_are_labelled(html_index: _HtmlIndex):
    """契约 §4.4：所有表单控件都要有 label 或 aria-label。"""
    unlabelled = _unlabelled_controls(html_index)
    assert not unlabelled, "这些表单控件没有 label / aria-label：\n  " + "\n  ".join(unlabelled)


def _is_comment_or_detector(line: str) -> bool:
    """U+FFFD 出现在注释或「乱码检测器」里是合法的（例如 isGarbled 的注释与正则）。"""
    stripped = line.strip()
    if stripped.startswith(("//", "*", "/*", "<!--")):
        return True
    return any(marker in line for marker in ("isGarbled", "uFFFD", "ufffd", "\\uFFFD"))


def test_static_assets_are_clean_utf8():
    """契约 §0.1/§5：静态资源本身不能是乱码（GBK 存盘/二次转码会留下 ``�`` 或 ``???``）。

    例外：``app.js`` 的 ``isGarbled()`` 与它的注释**故意**包含替换字符（用来识别历史脏数据），
    所以只对「出现在数据/文案里」的替换字符报错——注释行与检测器代码放行。
    """
    for path in (INDEX, APP_JS, STYLE):
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except UnicodeDecodeError as exc:  # pragma: no cover - 只有真的坏文件才走到
            pytest.fail(f"{path.name} 不是合法 UTF-8（{exc}）——打包后必然乱码")
        bad_lines = [
            f"{number}: {line.strip()[:120]}"
            for number, line in enumerate(text.splitlines(), 1)
            if "\ufffd" in line and not _is_comment_or_detector(line)
        ]
        assert not bad_lines, (
            f"{path.name} 的文案/数据里出现替换字符 U+FFFD（编码链损坏）：\n  " + "\n  ".join(bad_lines)
        )
        assert "???" not in text, f"{path.name} 含连续 '???'（中文被替换成问号）"


# ===========================================================================
# 6) 设计系统 / 卡顿治理（契约 §1 / §4.2）
# ===========================================================================


@pytest.mark.parametrize("token", OPS_TOKENS)
def test_style_defines_ops_design_tokens(token: str):
    """契约 §1：``--ops-*`` 设计令牌必须作为 CSS 变量落地。"""
    css = _read(STYLE)
    assert token in css, f"style.css 缺少契约设计令牌 {token}"


def test_style_respects_reduced_motion():
    """契约 §4.4：``prefers-reduced-motion`` 时必须关掉动画。"""
    css = _read(STYLE)
    assert "prefers-reduced-motion" in css, "style.css 没有 prefers-reduced-motion 分支"


def test_polling_is_throttled_and_visibility_aware(js_source: str):
    """契约 §4.2：定时刷新间隔 ≥5000ms，且只在页面可见时刷新（这是「卡手」的根因）。"""
    intervals: list[int] = []
    # 逐个 setInterval 调用取它结尾的毫秒参数：``}, 7000)``
    for match in re.finditer(r"setInterval\(", js_source):
        tail = js_source[match.end(): match.end() + 4000]
        close = re.search(r"\}\s*,\s*(\d+)\s*\)", tail)
        assert close, "无法解析 setInterval 的间隔参数（请保持 `setInterval(() => {...}, 7000)` 写法）"
        intervals.append(int(close.group(1)))
        block = tail[: close.start()]
        assert "visibilityState" in block, "定时器没有检查 document.visibilityState（隐藏时仍在拉数据）"
    assert intervals, "app.js 没有 setInterval——定时刷新逻辑不见了？"
    for value in intervals:
        assert value >= 5000, f"定时刷新间隔 {value}ms < 5000ms（契约 §4.2 要求 ≥5000ms）"


def test_first_paint_uses_single_dashboard_request(js_source: str):
    """契约 §4.2：首屏在项目就绪后只发 dashboard 一个请求，不再 Promise.all 拉十个接口。"""
    assert "/dashboard" in js_source, "app.js 没有使用 dashboard 聚合接口"
    start = js_source.find("async function boot(")
    assert start >= 0, "找不到 boot()"
    boot_body = js_source[start: start + 2500]
    assert "loadDashboard" in boot_body, "boot() 首屏没有调用 loadDashboard（仍在一堆接口里拼首屏）"
    # 首屏允许并行的只有 agents/detail 与 projects（契约 §4.2 明确写了这两个）
    assert "loadProjects" in boot_body and "loadAgentsDetail" in boot_body, (
        "boot() 首屏应并行 loadProjects + loadAgentsDetail"
    )

# ===========================================================================
# 7) 变异自检：把契约要求的元素/绑定删掉，上面的断言必须立刻失败。
#    目的：证明这些检查不是「读文件但什么都没断言」的空转。
# ===========================================================================


def test_mutation_missing_dom_id_is_detected():
    index = _parse_html('<section class="view" id="view-home"><div id="home-metrics"></div></section>')
    missing = _missing_ids(index, "view-home（指挥台）")
    assert "mission-text" in missing and "home-metrics" not in missing


def test_mutation_duplicate_dom_id_is_detected():
    index = _parse_html('<div id="kanban"></div><div id="kanban"></div>')
    assert _duplicate_ids(index) == {"kanban": 2}


def test_mutation_unlabelled_control_is_detected():
    index = _parse_html('<input id="loose" /><input id="ok" aria-label="有标签" />')
    unlabelled = _unlabelled_controls(index)
    assert len(unlabelled) == 1 and "loose" in unlabelled[0]


def test_mutation_relative_base_path_is_extracted():
    js = 'const base = `/api/projects/${encodeURIComponent(state.projectId)}`; api(`${base}/stats`);'
    assert "/api/projects/probe-id/stats" in _frontend_paths(js)


def test_mutation_unhandled_data_act_is_detected():
    js = 'switch (act) { case "mission-create": return; }'
    assert {"mission-create"} <= _case_labels(js)
    assert "new-task" not in _case_labels(js)


def test_mutation_template_data_act_literals_are_extracted():
    js = '`<button data-act="${entry ? "do-board-edit" : "do-board-new"}">`'
    assert _data_act_values(js) == {"do-board-edit", "do-board-new"}


def test_mutation_comment_with_replacement_char_is_allowed():
    assert _is_comment_or_detector("/* 旧数据兼容：整体是 ? / \ufffd / ？ 的 label */")
    assert not _is_comment_or_detector('<span class="name">\ufffd\ufffd</span>')
