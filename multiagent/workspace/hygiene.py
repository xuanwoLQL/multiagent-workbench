"""工作区卫生检查：把临时产物与交付物分开。

默认忽略规则用于快照/diff/copy，不让临时目录污染任务改动；本模块则**专门
扫描**这些被忽略的目录，供任务收尾、最终 QA、API 与 CLI 报告。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from ..config import Config
from ..util import matches_any, rel_posix
from .changes import effective_excludes

#: 明确属于运行期垃圾的路径。node_modules/.venv 仅作为「被忽略的依赖」，不算垃圾。
TEMP_PATTERNS = (
    ".edge-tmp*",
    ".edge-tmp*/**",
    "**/.edge-tmp*",
    "**/.edge-tmp*/**",
    "edge-profile",
    "edge-profile/**",
    "**/edge-profile",
    "**/edge-profile/**",
    "edge-capture*",
    "edge-capture*/**",
    "**/edge-capture*",
    "**/edge-capture*/**",
    "edge-cdp*",
    "edge-cdp*/**",
    "**/edge-cdp*",
    "**/edge-cdp*/**",
    "edge-shot*",
    "edge-shot*/**",
    "**/edge-shot*",
    "**/edge-shot*/**",
    "build/edge-*/**",
    "**/build/edge-*/**",
    "~$*",
    "**/~$*",
    "__pycache__",
    "__pycache__/**",
    "**/__pycache__",
    "**/__pycache__/**",
    "*.pyc",
    "**/*.pyc",
    ".cache",
    ".cache/**",
    "**/.cache",
    "**/.cache/**",
    ".tmp*",
    ".tmp*/**",
    "**/.tmp*",
    "**/.tmp*/**",
    "*.tmp",
    "**/*.tmp",
    "*.log",
    "**/*.log",
)

LIMIT_MESSAGE = "工作区卫生扫描达到条目上限，结果可能不完整"

#: 这些目录内容多且与临时垃圾判定无关；扫描时整体跳过（仍会在顶层 ignored_dirs 报告）。
_PRUNE_DIRS = {
    ".git",
    ".multiagent",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def is_temporary_path(rel_path: str) -> bool:
    return matches_any(rel_path, TEMP_PATTERNS)


def scan_workspace(
    root: str | Path,
    *,
    config: Config | None = None,
    extra_excludes: Iterable[str] | None = None,
    max_entries: int = 50000,
) -> dict[str, Any]:
    """扫描工作区并返回可序列化报告。

    - ``temporary``：需要提示用户清理的临时文件/目录；
    - ``ignored``：node_modules/.venv/.multiagent 等依赖/平台目录；
    - ``files/bytes``：按有效排除规则统计的交付文件。
    """
    base = Path(root)
    report: dict[str, Any] = {
        "root": str(base),
        "deliverable_files": 0,
        "deliverable_bytes": 0,
        "temporary_files": 0,
        "temporary_bytes": 0,
        "temporary_dirs": 0,
        "temporary_dir_paths": [],
        "temporary": [],
        "ignored_dirs": [],
        "truncated": False,
        "ok": True,
        "summary": "工作区卫生良好",
    }
    if not base.is_dir():
        report.update(ok=False, summary="工作区目录不存在")
        return report

    excludes = effective_excludes(base, config, extra_excludes)
    seen = 0
    temp_roots: list[str] = []
    try:
        for current, dirs, files in os.walk(base, followlinks=False):
            kept_dirs: list[str] = []
            for name in dirs:
                path = Path(current) / name
                if path.is_symlink():
                    continue
                rel = rel_posix(path, base)
                under_temp = any(rel == root or rel.startswith(root + "/") for root in temp_roots)
                if is_temporary_path(rel) and not under_temp:
                    temp_roots.append(rel)
                    report["temporary_dirs"] += 1
                    if len(report["temporary_dir_paths"]) < 100:
                        report["temporary_dir_paths"].append(rel)
                    # 保留遍历：目录里的文件也要计入临时文件/字节预算。
                    kept_dirs.append(name)
                    continue
                if under_temp:
                    kept_dirs.append(name)
                    continue
                if name in _PRUNE_DIRS:
                    continue
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in files:
                seen += 1
                if seen > max_entries:
                    report["truncated"] = True
                    break
                path = Path(current) / name
                if path.is_symlink() or not path.is_file():
                    continue
                rel = rel_posix(path, base)
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                if is_temporary_path(rel):
                    report["temporary_files"] += 1
                    report["temporary_bytes"] += size
                    if len(report["temporary"]) < 100:
                        report["temporary"].append({"path": rel, "size": size})
                    continue
                if matches_any(rel, excludes):
                    continue
                report["deliverable_files"] += 1
                report["deliverable_bytes"] += size
            if report["truncated"]:
                break

        ignored_dirs: list[str] = []
        for name in ("node_modules", ".venv", "venv", ".multiagent", "build"):
            candidate = base / name
            if candidate.is_dir() and len(ignored_dirs) < 20:
                ignored_dirs.append(name)
        report["ignored_dirs"] = ignored_dirs
    except OSError as exc:
        report["ok"] = False
        report["summary"] = f"工作区卫生扫描失败：{type(exc).__name__}: {exc}"
        return report

    if report["temporary_files"] or report["temporary_dirs"]:
        report["ok"] = False
        report["summary"] = (
            f"发现 {report['temporary_files']} 个临时文件、{report['temporary_dirs']} 个临时目录"
            f"（{report['temporary_bytes']} B），"
            "不应计入交付物"
        )
    if report["truncated"]:
        report["summary"] += f"；{LIMIT_MESSAGE}"
    return report
