"""通用验收条件（需求 15 / 42）。

系统不采信 Agent 的自我声明：只以退出码、产物存在性、内容匹配、命令退出码、
人工确认等**可观测条件**判定任务成败。条件本身由配置描述，可复用。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ..util import matches_any, read_text, rel_posix, run_command, safe_join, to_posix
from .profiles import _safe_pattern, glob_matches

_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*\}\}")

#: 产物路径里出现任一字符即按 glob 处理（``**`` 已被 ``*`` 覆盖）。
_GLOB_META = frozenset("*?[]")

#: 候选文件搜索时跳过的目录：版本库、运行时数据、依赖与构建产物。
_CANDIDATE_SKIP_DIRS = frozenset(
    {
        ".git",
        ".multiagent",
        "node_modules",
        "dist",
        "build",
        ".venv",
        "venv",
        "target",
        "__pycache__",
    }
)
_CANDIDATE_LIMIT = 20
_CANDIDATE_DETAIL_LIMIT = 5
_PATH_MISMATCH_HINT = "实际产物路径与验收条件不一致；可在任务详情修正验收路径。"


def _is_glob_path(value: str) -> bool:
    return any(char in value for char in _GLOB_META)


def _file_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def suggest_path_candidates(
    workspace: Path,
    rel: str,
    *,
    exclude: Path | None = None,
    limit: int = _CANDIDATE_LIMIT,
) -> list[str]:
    """按 basename 在 workspace 内查找候选产物路径（可复用的公开函数）。

    只返回 workspace 内的 POSIX 相对路径，最多 ``limit`` 条；绝对路径、含
    ``..`` 的越界路径、符号链接以及跳过目录中的文件一律不返回。该函数只做
    路径建议（验收诊断 / 计划预扫描），**不参与**验收结论判定。
    """
    try:
        bounded = int(limit)
    except (TypeError, ValueError):
        bounded = _CANDIDATE_LIMIT
    if bounded < 1:
        return []
    safe = _safe_pattern(rel)
    if not safe:
        return []
    basename = PurePosixPath(safe).name
    if not basename:
        return []
    root = Path(workspace)
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    excluded: Path | None = None
    if exclude is not None:
        try:
            excluded = exclude.resolve()
        except OSError:
            excluded = None

    def inside(path: Path) -> bool:
        return path == root_resolved or root_resolved in path.parents

    found: list[str] = []
    try:
        for current, dirnames, filenames in os.walk(root):
            kept: list[str] = []
            for name in sorted(dirnames):
                if name in _CANDIDATE_SKIP_DIRS:
                    continue
                full = Path(current) / name
                try:
                    # 符号链接（含指向 root 外的目录联接）一律不进入。
                    if full.is_symlink() or not inside(full.resolve()):
                        continue
                except OSError:
                    continue
                kept.append(name)
            dirnames[:] = kept
            for name in sorted(filenames):
                if name != basename:
                    continue
                full = Path(current) / name
                try:
                    if full.is_symlink():
                        continue
                    resolved = full.resolve()
                    if excluded is not None and resolved == excluded:
                        continue
                    if not inside(resolved):
                        continue
                except OSError:
                    continue
                found.append(rel_posix(full, root))
                if len(found) >= bounded:
                    return sorted(found)
    except OSError:
        pass
    return sorted(found)


def _candidate_files(workspace: Path, rel: str, *, exclude: Path | None = None) -> list[str]:
    """验收诊断专用包装：保持原有默认候选上限。"""
    return suggest_path_candidates(
        workspace, rel, exclude=exclude, limit=_CANDIDATE_LIMIT
    )


def _apply_candidate_diagnostics(
    check: dict[str, Any], workspace: Path, rel: str, target: Path | None
) -> None:
    """给失败的精确路径 check 追加候选与修正建议；``ok`` 保持不变。"""
    candidates = _candidate_files(workspace, rel, exclude=target)
    check["candidates"] = candidates
    if not candidates:
        check["suggested"] = None
        check["hint"] = None
        return
    safe = _safe_pattern(rel) or to_posix(rel)
    basename = PurePosixPath(safe).name
    check["suggested"] = {
        "type": "glob_min_files",
        "pattern": f"**/{basename}",
        "min_count": 1,
    }
    check["hint"] = _PATH_MISMATCH_HINT
    shown = candidates[:_CANDIDATE_DETAIL_LIMIT]
    suffix = "；候选：" + ", ".join(shown)
    if len(candidates) > len(shown):
        suffix += f"（共 {len(candidates)} 个）"
    check["detail"] = f"{check.get('detail') or ''}{suffix}"


def substitute(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        def repl(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(variables.get(key, ""))
        return _PLACEHOLDER.sub(repl, value)
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    return value


@dataclass
class AcceptanceResult:
    ok: bool
    checks: list[dict[str, Any]] = field(default_factory=list)
    need_manual: bool = False
    skipped: bool = False

    def failing(self) -> list[dict[str, Any]]:
        return [c for c in self.checks if not c["ok"]]

    def summary(self) -> str:
        if self.skipped:
            return "未定义验收条件（视为通过）"
        if self.ok:
            return f"验收通过（{len(self.checks)} 项）"
        if self.need_manual:
            return "等待人工确认"
        failed = ", ".join(c["type"] for c in self.failing())
        return f"验收失败：{failed}"


def evaluate_acceptance(
    conditions: list[dict[str, Any]],
    *,
    workspace: Path,
    changes: list[dict[str, Any]],
    exit_code: int,
    handoff: dict[str, Any] | None,
    variables: dict[str, Any] | None = None,
    manual_approved: bool = False,
) -> AcceptanceResult:
    if not conditions:
        return AcceptanceResult(ok=True, checks=[], skipped=True)

    variables = variables or {}
    changed_paths = {c["rel_path"]: c["action"] for c in changes}
    checks: list[dict[str, Any]] = []
    need_manual = False

    for raw in conditions:
        condition = substitute(raw, variables)
        ctype = condition.get("type")
        ok = False
        detail = ""
        diagnose_rel: str | None = None
        diagnose_target: Path | None = None
        try:
            if ctype == "file_exists":
                rel = str(condition.get("path", "") or "")
                if _is_glob_path(rel):
                    matches = glob_matches(workspace, rel)
                    ok = bool(matches)
                    detail = f"{rel} 匹配文件数={len(matches)}"
                else:
                    diagnose_rel = rel
                    target = safe_join(workspace, rel)
                    diagnose_target = target
                    ok = target.exists()
                    detail = f"{rel} 存在={ok}"
            elif ctype == "file_absent":
                rel = condition.get("path", "")
                ok = not safe_join(workspace, rel).exists()
                detail = f"{rel} 不存在={ok}"
            elif ctype == "file_nonempty":
                rel = str(condition.get("path", "") or "")
                min_bytes = max(1, int(condition.get("min_bytes", 1)))
                if _is_glob_path(rel):
                    matches = glob_matches(workspace, rel)
                    qualified = [path for path in matches if _file_size(path) >= min_bytes]
                    ok = bool(qualified)
                    detail = (
                        f"{rel} 匹配文件数={len(matches)}，"
                        f"其中 >= {min_bytes}B 的 {len(qualified)} 个"
                    )
                else:
                    diagnose_rel = rel
                    target = safe_join(workspace, rel)
                    diagnose_target = target
                    size = _file_size(target) if target.is_file() else 0
                    ok = size >= min_bytes
                    detail = f"{rel} 大小={size}（要求 >= {min_bytes}）"
            elif ctype == "glob_min_files":
                pattern = str(condition.get("pattern") or "")
                min_count = max(1, int(condition.get("min_count", 1)))
                min_bytes = max(0, int(condition.get("min_bytes", 0)))

                matches = [
                    path
                    for path in glob_matches(workspace, pattern)
                    if not min_bytes or path.stat().st_size >= min_bytes
                ]
                ok = len(matches) >= min_count
                detail = f"{pattern} 命中={len(matches)}（要求 >= {min_count}，单文件 >= {min_bytes}B）"
            elif ctype == "file_modified":
                rel = condition.get("path")
                if rel:
                    ok = rel in changed_paths and changed_paths[rel] in ("create", "modify")
                    detail = f"{rel} 被修改={ok}"
                else:
                    ok = any(a in ("create", "modify") for a in changed_paths.values())
                    detail = f"改动文件数={len(changed_paths)}"
            elif ctype == "file_deleted":
                rel = condition.get("path")
                ok = changed_paths.get(rel) == "delete"
                detail = f"{rel} 被删除={ok}"
            elif ctype in ("content_contains", "content_not_contains"):
                rel = str(condition.get("path", "") or "")
                text = str(condition.get("text", ""))
                if _is_glob_path(rel):
                    matches = glob_matches(workspace, rel)
                    hit_path: Path | None = None
                    for path in matches:
                        if text in read_text(path):
                            hit_path = path
                            break
                    hit = hit_path is not None
                    ok = hit if ctype == "content_contains" else not hit
                    hit_note = f"；命中 {rel_posix(hit_path, workspace)}" if hit_path else ""
                    detail = (
                        f"{rel} 匹配文件数={len(matches)}，"
                        f"{'包含' if hit else '不含'} '{text[:40]}'{hit_note}"
                    )
                else:
                    diagnose_rel = rel
                    target = safe_join(workspace, rel)
                    diagnose_target = target
                    content = read_text(target) if target.exists() else ""
                    hit = text in content
                    ok = hit if ctype == "content_contains" else not hit
                    detail = f"{rel} {'包含' if hit else '不含'} '{text[:40]}'"
            elif ctype == "no_writes":
                ok = not changed_paths
                detail = f"改动文件数={len(changed_paths)}"
            elif ctype == "writes_within_scope":
                scope = condition.get("scope", [])
                offenders = [p for p in changed_paths if scope and not matches_any(p, scope)]
                ok = not offenders
                detail = f"越权文件={offenders[:5]}"
            elif ctype == "handoff_present":
                ok = bool(handoff and (handoff.get("did") or handoff.get("notes")))
                detail = "交接说明存在" if ok else "缺少交接说明"
            elif ctype == "exit_zero":
                ok = exit_code == 0
                detail = f"exit_code={exit_code}"
            elif ctype == "command_exit_zero":
                command = condition.get("command") or ""
                code, _out, err, _ms = run_command(
                    command, cwd=workspace, shell=True, timeout=int(condition.get("timeout", 60))
                )
                ok = code == 0
                detail = f"`{command}` → exit {code} {err.strip()[:120]}"
            elif ctype == "command_output_contains":
                command = condition.get("command") or ""
                text = str(condition.get("text", ""))
                code, out, err, _ms = run_command(
                    command, cwd=workspace, shell=True, timeout=int(condition.get("timeout", 60))
                )
                ok = text in (out + err)
                detail = f"`{command}` 输出{'包含' if ok else '不含'} '{text[:40]}'"
            elif ctype == "manual_confirm":
                need_manual = need_manual or not manual_approved
                ok = bool(manual_approved)
                detail = "人工已确认" if ok else "等待人工确认"
            elif ctype == "no_temp_artifacts":
                from ..workspace.hygiene import scan_workspace

                report = scan_workspace(workspace)
                ok = int(report.get("temporary_files") or 0) == 0
                detail = str(report.get("summary") or "")
            elif ctype == "always":
                ok = True
                detail = "恒真"
            else:
                detail = f"未知验收类型：{ctype}"
                ok = False
        except Exception as exc:  # noqa: BLE001 - 验收失败不应抛出
            ok = False
            detail = f"验收执行异常：{type(exc).__name__}: {exc}"
        check: dict[str, Any] = {"type": ctype, "ok": ok, "detail": detail, "condition": condition}
        if not ok and diagnose_rel is not None:
            # 精确路径失败时附带候选与修正建议；绝不因为候选存在而放行。
            _apply_candidate_diagnostics(check, workspace, diagnose_rel, diagnose_target)
        checks.append(check)

    hard_fail = any(not c["ok"] and c["type"] != "manual_confirm" for c in checks)
    if need_manual and not hard_fail:
        return AcceptanceResult(ok=False, checks=checks, need_manual=True)
    return AcceptanceResult(ok=not hard_fail, checks=checks)
