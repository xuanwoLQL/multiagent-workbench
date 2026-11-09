"""P2：历史重复项目可识别（不删数据）的前端契约。

背景：后端已禁止同一目录重复登记（409），但历史库中仍可能有多个项目
指向同一目录（例如 myworld / atlas / atlas-v2 都指向 ~/myworld）。
前端必须让用户看见这些组、直接打开成员项目，并支持「仅显示重复项目」，
而后端路由与历史数据不做任何改动。

做法：
* 静态契约：检查 ``duplicates.ts`` 的纯函数导出与归一化规则，以及
  ``ProjectsView.tsx`` 的横幅/筛选文案与 ``openProject`` 接线；
* 语义契约：pytest 不能直接执行 TS，在 Python 中复刻同一套文本归一化
  规则，用 3 个历史项目 dict 断言预期分组（不伪造前端运行时）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DUPLICATES_TS = REPO / "webapp" / "src" / "features" / "projects" / "duplicates.ts"
PROJECTS_TSX = REPO / "webapp" / "src" / "features" / "projects" / "ProjectsView.tsx"
PROJECTS_CSS = REPO / "webapp" / "src" / "features" / "projects" / "projects.css"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- 静态契约

def test_duplicates_module_exports_pure_grouping_functions():
    source = _read(DUPLICATES_TS)

    assert re.search(r"export\s+function\s+normalizeRootKey\s*\(", source), (
        "duplicates.ts 必须导出 normalizeRootKey"
    )
    assert re.search(r"export\s+function\s+groupDuplicateProjects\b", source), (
        "duplicates.ts 必须导出 groupDuplicateProjects"
    )
    # 纯函数：不依赖 React / react-query / 网络。
    assert not re.search(r"from\s+['\"]react", source)
    assert "@tanstack" not in source
    assert "fetch(" not in source

    assert ">= 2" in source, "只返回成员数 >= 2 的重复组"


def test_duplicates_normalization_rules_match_backend():
    source = _read(DUPLICATES_TS)

    assert r'replace(/\\/g, "/")' in source, "必须把反斜杠统一为正斜杠"
    assert r'replace(/\/+/g, "/")' in source, "必须合并重复分隔符"
    assert r'replace(/\/+$/, "")' in source, "必须去掉尾部分隔符"
    assert ".toLowerCase()" in source, "Windows 路径必须统一小写"
    assert 'startsWith("//")' in source, "UNC 路径不能被合并成单斜杠"
    assert 'return "";' in source, "空路径必须返回空字符串"


def test_projects_view_shows_banner_filter_and_open_action():
    source = _read(PROJECTS_TSX)

    assert "groupDuplicateProjects" in source, "ProjectsView 必须引用重复分组函数"
    assert "duplicateGroups.length > 0" in source, "没有重复时不应渲染横幅"
    assert "aria-expanded={duplicatesOpen}" in source, "横幅必须可折叠且可键盘操作"

    for phrase in (
        "发现",
        "重复项目",
        "历史数据未删除",
        "仅显示重复项目",
        "409",
        "人工确认后删除",
        "打开",
    ):
        assert phrase in source, f"ProjectsView 缺少重复项目文案：{phrase}"

    # 每个成员项目一个「打开」按钮，复用现有 openProject。
    assert re.search(
        r'<Button size="sm" onClick=\{\(\) => openProject\(project\.id\)\}>',
        source,
    ), "重复项目成员必须有「打开」按钮并调用 openProject"

    # 「仅显示重复项目」筛选必须真正参与卡片过滤。
    assert "duplicateIds.has(card.project.id)" in source, (
        "仅显示重复项目必须按重复组 ID 过滤卡片"
    )
    assert "dangerouslySetInnerHTML" not in source


def test_duplicate_styles_use_tokens_only():
    css = _read(PROJECTS_CSS)
    assert ".pj-dup-banner" in css, "缺少重复项目横幅样式"
    assert ".pj-dup-member" in css, "缺少重复项目成员样式"

    start = css.index("/* -------------------------------------------------------------- duplicates */")
    end = css.index("/* ------------------------------------------------------------------ toolbar */")
    section = css[start:end]
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", section), (
        "重复项目样式不得使用裸十六进制颜色，只能使用 tokens.css 变量"
    )
    assert "var(--warning-soft)" in section
    assert "var(--warning-text)" in section


# --------------------------------------------------------------------------- 语义契约

def _normalize_root_key(path: str) -> str:
    """复刻 duplicates.ts 的 normalizeRootKey（与后端 normalize_root 文本语义一致）。"""
    raw = str(path or "").strip().replace("\\", "/")
    if not raw:
        return ""
    is_unc = raw.startswith("//")
    body = raw[2:] if is_unc else raw
    collapsed = re.sub(r"/+", "/", body)
    with_root = f"//{collapsed}" if is_unc else collapsed
    return with_root.rstrip("/").lower()


def _group_duplicate_projects(projects: list[dict]) -> list[dict]:
    """复刻 duplicates.ts 的分组语义：按输入顺序分组，只保留 >=2 成员的组。"""
    groups: dict[str, dict] = {}
    for project in projects:
        key = _normalize_root_key(project.get("root_path", ""))
        if not key:
            continue
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "key": key,
                "rootPath": str(project.get("root_path") or "").strip(),
                "projects": [project],
            }
        else:
            group["projects"].append(project)
    return [group for group in groups.values() if len(group["projects"]) >= 2]


def test_three_historical_projects_for_same_directory_group_together():
    projects = [
        {"id": "p1", "name": "myworld", "root_path": "~/myworld"},
        {"id": "p2", "name": "atlas", "root_path": "~/MyWorld/"},
        {"id": "p3", "name": "atlas-v2", "root_path": "~/myworld//"},
    ]

    groups = _group_duplicate_projects(projects)

    assert len(groups) == 1
    assert groups[0]["key"] == "~/myworld"
    assert groups[0]["rootPath"] == "~/myworld"
    assert [project["name"] for project in groups[0]["projects"]] == [
        "myworld",
        "atlas",
        "atlas-v2",
    ], "组内成员必须保持输入（创建）顺序"


def test_multiple_groups_and_singletons_are_separated():
    projects = [
        {"id": "a1", "name": "a-one", "root_path": "D:\\work\\a"},
        {"id": "b1", "name": "b-one", "root_path": "E:\\work\\b"},
        {"id": "a2", "name": "a-two", "root_path": "d:/WORK/a/"},
        {"id": "c1", "name": "single", "root_path": "D:\\work\\c"},
        {"id": "b2", "name": "b-two", "root_path": "e:\\work\\b\\\\"},
    ]

    groups = _group_duplicate_projects(projects)

    assert [group["key"] for group in groups] == ["d:/work/a", "e:/work/b"]
    assert [
        [project["id"] for project in group["projects"]] for group in groups
    ] == [["a1", "a2"], ["b1", "b2"]]
    # 单成员目录不产生重复组。
    assert all(len(group["projects"]) >= 2 for group in groups)


def test_empty_paths_and_distinct_drives_are_not_duplicates():
    assert _group_duplicate_projects(
        [
            {"id": "x", "name": "x", "root_path": ""},
            {"id": "y", "name": "y", "root_path": "   "},
        ]
    ) == []

    assert _normalize_root_key("D:\\a") != _normalize_root_key("E:\\a")
    assert _normalize_root_key("\\\\localhost\\share\\a") == (
        "//localhost/share/a"
    )
    assert _normalize_root_key("//LOCALHOST/share/a/") == "//localhost/share/a"
    assert _normalize_root_key("\\\\localhost\\share\\a") != (
        _normalize_root_key("\\\\localhost\\other\\a")
    )
