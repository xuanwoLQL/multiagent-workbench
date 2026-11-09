"""大型文件型项目的默认 DAG 拆解。

目标不是「猜出所有业务细节」，而是把一份 PDF/需求文档/长 brief 变成
**可审阅、可调整、可执行**的九阶段计划：

``requirements-freeze → sim-core → web-3d / diagrams → ppt → docs → review → fix → final-qa``

计划只负责生成结构；真正创建前 API/UI 会先返回预览，用户可以改 JSON 再应用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from ..config import Config
from ..util import rel_posix, to_posix
from .profiles import build_acceptance_conditions

PROJECT_STAGE_KEYS = (
    "requirements-freeze",
    "sim-core",
    "web-3d",
    "diagrams",
    "ppt",
    "docs",
    "review",
    "fix",
    "final-qa",
)

_TEXT_SUFFIXES = {
    ".md",
    ".txt",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".csv",
}


def _inside(root: Path, path: Path) -> bool:
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


def _source_item(root: Path, path: Path) -> dict[str, Any]:
    rel = rel_posix(path, root)
    item: dict[str, Any] = {
        "path": rel,
        "kind": "pdf" if path.suffix.lower() == ".pdf" else "text",
        "size": path.stat().st_size if path.exists() else 0,
    }
    excerpt = ""
    if path.suffix.lower() not in {".pdf"} and path.is_file():
        try:
            excerpt = path.read_text(encoding="utf-8", errors="replace")[:2400]
        except OSError:
            excerpt = ""
    elif path.suffix.lower() == ".pdf":
        for candidate in (root / "pdf_text.txt", path.with_suffix(".txt"), path.with_name(path.stem + "_text.txt")):
            if candidate.is_file() and _inside(root, candidate):
                try:
                    excerpt = candidate.read_text(encoding="utf-8", errors="replace")[:2400]
                except OSError:
                    excerpt = ""
                if excerpt:
                    item["text_path"] = rel_posix(candidate, root)
                    break
    if excerpt:
        item["excerpt"] = excerpt
    return item


def discover_sources(
    root: str | Path,
    *,
    source_files: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """发现 PDF/需求文件/文本说明；返回带有限摘录的来源列表。"""
    base = Path(root)
    if not base.is_dir():
        return []
    requested = list(source_files or [])
    candidates: list[Path] = []
    if requested:
        for raw in requested:
            text = str(raw).strip()
            if not text:
                continue
            candidate = (base / text).resolve()
            if candidate.is_file() and _inside(base, candidate):
                candidates.append(candidate)
    else:
        patterns = (
            "pdf_text.txt",
            "*.pdf",
            "requirements*.txt",
            "requirement*.md",
            "需求*.txt",
            "需求*.md",
            "docs/requirements*.md",
            "docs/*需求*.md",
        )
        for pattern in patterns:
            try:
                candidates.extend(sorted(base.glob(pattern)))
            except (OSError, ValueError):
                continue
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for candidate in candidates:
        if not candidate.is_file() or candidate.is_symlink():
            continue
        rel = to_posix(rel_posix(candidate, base))
        if rel in seen:
            continue
        seen.add(rel)
        result.append(_source_item(base, candidate))
        if len(result) >= 8:
            break
    return result


def _pick_agent(config: Config, kind: str, stage: str) -> str | None:
    configured = config.policy_get("decomposition.agent_by_stage", {}) or {}
    explicit = configured.get(stage) if isinstance(configured, dict) else None
    if explicit and config.agent(str(explicit)) and config.agent(str(explicit)).get("enabled", True):
        return str(explicit)
    preferences = {
        "code": ("codex", "opencode", "dsh", "claude"),
        "doc": ("claude", "codex", "dsh", "opencode"),
        "ppt": ("claude", "codex", "dsh", "opencode"),
        "review": ("claude", "codex", "dsh", "opencode"),
    }
    for agent_id in preferences.get(kind, preferences["code"]):
        agent = config.agent(agent_id)
        if agent and agent.get("enabled", True):
            return agent_id
    available = config.agent_ids()
    return available[0] if available else None


def _python_root(root: Path) -> str:
    if (root / "src" / "python").is_dir():
        return "src/python"
    if (root / "src").is_dir():
        return "src"
    return "."


def _verify_command(root: Path, *, final: bool = False) -> str:
    py = _python_root(root)
    if final and (root / "tests").is_dir():
        return "python -m pytest -q"
    return f"python -m compileall -q {py}"


def _base_brief(
    goal: str,
    sources: list[dict[str, Any]],
    *,
    extra: str,
) -> str:
    source_lines = "\n".join(
        f"- {item['path']}" + (f"（文本：{item['text_path']}）" if item.get("text_path") else "")
        for item in sources
    ) or "- （未发现独立需求文件，按下面的目标执行）"
    excerpts = "\n\n".join(
        f"### {item['path']}\n{item['excerpt']}" for item in sources if item.get("excerpt")
    )
    return (
        f"项目目标：\n{goal}\n\n"
        f"需求来源：\n{source_lines}\n\n"
        f"本阶段要求：\n{extra}\n\n"
        f"来源摘录（只作上下文，若与源文件冲突以源文件为准）：\n{excerpts or '（无摘录；请直接读取源文件）'}"
    )


def _stage_specs(
    config: Config,
    root: Path,
    goal: str,
    sources: list[dict[str, Any]],
    *,
    agent_override: str | None = None,
) -> list[dict[str, Any]]:
    src_outputs = ["src/**/*.py"]
    web_outputs = ["web/index.html", "web/**/*.js", "web/**/*.css"]
    diagram_outputs = ["diagrams/**/*.svg", "diagrams/**/*.png"]
    ppt_outputs = ["ppt/**/*.pptx", "ppt/**/*.md"]
    doc_outputs = ["docs/**/*.md"]
    verify_code = _verify_command(root)
    verify_final = _verify_command(root, final=True)

    stages: list[dict[str, Any]] = [
        {
            "key": "requirements-freeze",
            "title": "需求冻结：提炼验收标准",
            "kind": "doc",
            "role": "researcher",
            "outputs": ["docs/requirements.md"],
            "write_scope": ["docs/**"],
            "timeout_sec": 1800,
            "max_attempts": 3,
            "depends_on": [],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "只做需求冻结，不写业务代码。阅读 PDF/需求文档，产出 docs/requirements.md，"
                    "必须包含：功能要求、非功能要求、验收标准、交付物清单、风险与假设。"
                ),
            ),
            "acceptance": [
                {"type": "file_exists", "path": "docs/requirements.md"},
                {"type": "file_nonempty", "path": "docs/requirements.md", "min_bytes": 200},
                {"type": "file_modified", "path": "docs/requirements.md"},
            ],
            "tags": ["decompose", "requirements"],
        },
        {
            "key": "sim-core",
            "title": "仿真核心：实现并验证 Python 调度/并发逻辑",
            "kind": "code",
            "role": "implementer",
            "outputs": src_outputs,
            "write_scope": ["src/**", "tests/**", "data/**"],
            "timeout_sec": 3600,
            "max_attempts": 3,
            "depends_on": ["requirements-freeze"],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "实现或修复仿真核心：调度器、队列、资源限制、指标计算、实验入口。"
                    "保留已有可运行代码，做最小必要改动；补充/运行可执行验证。"
                ),
            ),
            "verify_command": verify_code,
            "tags": ["decompose", "simulation", "code"],
        },
        {
            "key": "web-3d",
            "title": "Web 3D：可视化页面与交互",
            "kind": "web",
            "role": "implementer",
            "outputs": web_outputs,
            "write_scope": ["web/**", "assets/**"],
            "timeout_sec": 3600,
            "max_attempts": 3,
            "depends_on": ["sim-core"],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "实现 Web 3D 可视化：确保 web/index.html 可直接打开，核心仿真数据与图表联动，"
                    "three.js 资源本地可加载，页面无控制台致命错误。"
                ),
            ),
            "acceptance": [
                {"type": "file_exists", "path": "web/index.html"},
                {"type": "file_nonempty", "path": "web/index.html", "min_bytes": 500},
                {"type": "glob_min_files", "pattern": "web/**/*.js", "min_count": 1, "min_bytes": 50},
                {"type": "file_modified"},
            ],
            "tags": ["decompose", "web", "3d"],
        },
        {
            "key": "diagrams",
            "title": "图表：吞吐/等待/利用率/公平性",
            "kind": "diagrams",
            "role": "worker",
            "outputs": diagram_outputs,
            "write_scope": ["diagrams/**", "data/**", "tools/**"],
            "timeout_sec": 1800,
            "max_attempts": 3,
            "depends_on": ["sim-core"],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "基于仿真输出生成可交付图表与索引页；图表必须有标题、坐标轴/图例，"
                    "数据来源可追溯，至少覆盖吞吐、等待时间、资源利用率与公平性。"
                ),
            ),
            "acceptance": [
                {"type": "glob_min_files", "pattern": "diagrams/**/*", "min_count": 1, "min_bytes": 100},
                {"type": "file_modified"},
            ],
            "tags": ["decompose", "diagrams"],
        },
        {
            "key": "ppt",
            "title": "PPT：案例汇报材料",
            "kind": "ppt",
            "role": "writer",
            "outputs": ppt_outputs,
            "write_scope": ["ppt/**"],
            "timeout_sec": 1800,
            "max_attempts": 3,
            "depends_on": ["web-3d", "diagrams"],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "产出可打开的 PPT（.pptx）和演讲备注；结构覆盖背景、OS 原理、算法设计、"
                    "实验与指标、结论与改进。引用 diagrams 中的图表。"
                ),
            ),
            "acceptance": [
                {"type": "glob_min_files", "pattern": "ppt/**/*.pptx", "min_count": 1, "min_bytes": 1024},
                {"type": "file_modified"},
            ],
            "tags": ["decompose", "ppt"],
        },
        {
            "key": "docs",
            "title": "文档：案例说明书与实验指导",
            "kind": "doc",
            "role": "writer",
            "outputs": doc_outputs,
            "write_scope": ["docs/**", "README*"],
            "timeout_sec": 1800,
            "max_attempts": 3,
            "depends_on": ["ppt", "web-3d", "diagrams"],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "完善项目文档：案例说明、系统设计、实验步骤、指标解释、复现指南。"
                    "文档中的命令与文件名必须和实际产物一致。"
                ),
            ),
            "acceptance": [
                {"type": "glob_min_files", "pattern": "docs/**/*.md", "min_count": 1, "min_bytes": 100},
                {"type": "file_modified"},
            ],
            "tags": ["decompose", "docs"],
        },
        {
            "key": "review",
            "title": "只读审阅：需求/代码/文档/PPT 一致性",
            "kind": "review",
            "role": "reviewer",
            "outputs": [],
            "write_scope": [],
            "read_only": True,
            "isolation": "shared",
            "timeout_sec": 1800,
            "max_attempts": 2,
            "depends_on": ["docs"],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "只读审阅全部交付物，不修改文件。列出阻塞级问题、与需求不符项、"
                    "可复现性和验收证据缺口；给出按优先级排序的修复清单。"
                ),
            ),
            "acceptance": [{"type": "no_writes"}, {"type": "handoff_present"}],
            "tags": ["decompose", "review"],
        },
        {
            "key": "fix",
            "title": "修复：处理只读审阅发现的问题",
            "kind": "code",
            "role": "reviser",
            "outputs": [],
            "write_scope": ["src/**", "web/**", "docs/**", "diagrams/**", "ppt/**", "tests/**"],
            "timeout_sec": 2400,
            "max_attempts": 3,
            "depends_on": ["review"],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "只修复 review 交接中指出的问题，避免无关重构。修复后运行可执行验证，"
                    "在交接中逐条说明已修复/未修复及原因。"
                ),
            ),
            "verify_command": verify_code,
            "acceptance": [
                {"type": "file_modified"},
                {"type": "command_exit_zero", "command": verify_code, "timeout": 1200},
                {"type": "handoff_present"},
            ],
            "tags": ["decompose", "fix"],
        },
        {
            "key": "final-qa",
            "title": "最终 QA：只读验收与卫生报告",
            "kind": "review",
            "role": "verifier",
            "outputs": [],
            "write_scope": [],
            "read_only": True,
            "isolation": "shared",
            "timeout_sec": 2400,
            "max_attempts": 2,
            "depends_on": ["fix"],
            "brief": _base_brief(
                goal,
                sources,
                extra=(
                    "最终只读验收：核对全部交付物、运行验证命令、检查临时目录/缓存是否进入交付范围。"
                    "不要修改工作区；在交接中列出通过的验收项、缺失项、风险与人工下一步。"
                ),
            ),
            "verify_command": verify_final,
            "acceptance": [
                {"type": "no_writes"},
                {"type": "handoff_present"},
                {"type": "command_exit_zero", "command": verify_final, "timeout": 1200},
            ],
            "tags": ["decompose", "final-qa"],
        },
    ]

    for stage in stages:
        kind = str(stage["kind"])
        outputs = stage.get("outputs") or []
        if stage.get("acceptance") is None:
            acceptance, _warnings = build_acceptance_conditions(
                config,
                kind,
                outputs,
                verify_command=stage.get("verify_command"),
            )
            stage["acceptance"] = acceptance
        if stage.get("verify_command"):
            stage["context"] = {"verify_command": stage["verify_command"]}
        stage.setdefault("read_only", False)
        stage.setdefault("isolation", "copy")
        stage["agent"] = agent_override or _pick_agent(config, kind, str(stage["key"]))
        stage["priority"] = 0
    return stages


def build_project_plan(
    config: Config,
    root: str | Path,
    brief: str,
    *,
    title: str = "",
    source_files: Iterable[str] | None = None,
    agent_override: str | None = None,
) -> dict[str, Any]:
    base = Path(root)
    sources = discover_sources(base, source_files=source_files)
    warnings: list[str] = []
    if len(brief) > 4000:
        warnings.append("brief 超过 4000 字，已拆成九阶段 DAG；建议在预览中继续调整范围。")
    if any(item.get("kind") == "pdf" for item in sources):
        warnings.append("检测到 PDF 需求文件：已将源文件/文本摘录注入各阶段上下文。")
    if not sources:
        warnings.append("未发现 PDF/需求文件，计划只依据 brief 生成。")
    tasks = _stage_specs(
        config,
        base,
        brief.strip(),
        sources,
        agent_override=agent_override,
    )
    return {
        "mode": "project",
        "title": title or "大型文件型项目",
        "root_path": str(base),
        "source_files": sources,
        "warnings": warnings,
        "tasks": tasks,
        "stage_order": list(PROJECT_STAGE_KEYS),
    }
