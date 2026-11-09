"""任务类型默认值与验收模板。

验收条件分两层：

- **持久化验收**：任务创建时写入 ``task.acceptance``，直接可审计/可编辑；
- **有效验收**：执行收尾时在持久化条件上补齐类型约束（例如文档必须非空、
  只填了 ``exit_zero`` 的任务必须至少有真实文件改动）。

第二层专门用于兼容历史任务：旧任务的 ``acceptance`` 形状不变，但不会因为
Agent 退出码为 0 就被当成成功。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Iterable

from ..config import Config
from ..util import matches_any, rel_posix, to_posix

TEXT_KINDS = {"doc", "ppt", "diagrams", "web"}
CODE_KINDS = {"code", "data"}
NONEMPTY_KINDS = TEXT_KINDS | CODE_KINDS

DEFAULT_KIND_TEMPLATES: dict[str, dict[str, Any]] = {
    "code": {
        "checks": ["outputs_exist", "file_modified", "outputs_nonempty", "verify_command"],
        "min_bytes": 1,
        "timeout_sec": 2400,
        "max_attempts": 3,
    },
    "doc": {
        "checks": ["outputs_exist", "outputs_nonempty"],
        "min_bytes": 32,
        "timeout_sec": 2400,
        "max_attempts": 3,
    },
    "ppt": {
        "checks": ["outputs_exist", "outputs_nonempty"],
        "min_bytes": 1024,
        "timeout_sec": 2400,
        "max_attempts": 3,
    },
    "diagrams": {
        "checks": ["outputs_exist", "outputs_nonempty"],
        "min_bytes": 64,
        "timeout_sec": 1800,
        "max_attempts": 3,
    },
    "web": {
        "checks": ["outputs_exist", "outputs_nonempty"],
        "min_bytes": 64,
        "timeout_sec": 2400,
        "max_attempts": 3,
    },
    "review": {
        "checks": ["no_writes", "handoff_present"],
        "timeout_sec": 1800,
        "max_attempts": 2,
    },
    "research": {
        "checks": ["outputs_exist", "outputs_nonempty"],
        "min_bytes": 1,
        "timeout_sec": 1800,
        "max_attempts": 3,
    },
    "data": {
        "checks": ["outputs_exist", "file_modified", "outputs_nonempty"],
        "min_bytes": 1,
        "timeout_sec": 1800,
        "max_attempts": 3,
    },
    "generic": {
        "checks": ["file_modified", "handoff_present"],
        "timeout_sec": 1800,
        "max_attempts": 3,
    },
}


def acceptance_template(config: Config | None, kind: str) -> dict[str, Any]:
    if config is not None:
        raw = config.acceptance_raw.get("kind_templates") or {}
        configured = raw.get(kind)
        if isinstance(configured, dict):
            return copy.deepcopy(configured)
    return copy.deepcopy(DEFAULT_KIND_TEMPLATES.get(kind) or DEFAULT_KIND_TEMPLATES["generic"])


def kind_defaults(config: Config | None, kind: str) -> dict[str, Any]:
    """类型默认值：policy.kinds 优先，补齐验收模板里的 timeout/max_attempts。"""
    template = acceptance_template(config, kind)
    defaults: dict[str, Any] = {
        "timeout_sec": template.get("timeout_sec"),
        "max_attempts": template.get("max_attempts"),
        "isolation": "copy",
    }
    if config is not None:
        configured = config.kind_defaults(kind) or {}
        defaults.update({k: v for k, v in configured.items() if v is not None})
        if defaults.get("timeout_sec") is None:
            defaults["timeout_sec"] = config.policy_get("task_defaults.timeout_sec")
        if defaults.get("max_attempts") is None:
            defaults["max_attempts"] = config.policy_get("task_defaults.max_attempts")
        if defaults.get("timeout_sec") is None:
            defaults["timeout_sec"] = config.policy_get("default_timeout_sec", 1800)
    try:
        defaults["timeout_sec"] = int(defaults.get("timeout_sec") or 0)
    except (TypeError, ValueError):
        defaults["timeout_sec"] = 1800
    try:
        defaults["max_attempts"] = max(1, int(defaults.get("max_attempts") or 3))
    except (TypeError, ValueError):
        defaults["max_attempts"] = 3
    return defaults


def output_paths(outputs: Iterable[Any] | None) -> list[str]:
    result: list[str] = []
    for item in outputs or []:
        path = item if isinstance(item, str) else (item or {}).get("path") if isinstance(item, dict) else None
        if path and str(path).strip():
            result.append(str(path).strip())
    return result


def is_glob(path: str) -> bool:
    return any(char in path for char in "*?[")


def _safe_pattern(path: str) -> str | None:
    text = to_posix(path).strip()
    if not text or text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
        return None
    if ".." in text.split("/"):
        return None
    return text


def glob_matches(workspace: Path, pattern: str) -> list[Path]:
    safe = _safe_pattern(pattern)
    if not safe:
        return []
    root = Path(workspace)
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root
    matches: list[Path] = []
    try:
        for path in root.glob(safe):
            try:
                # 符号链接本身不采信；解析后仍需落在 workspace 内，
                # 防止 ``link/*`` 这类路径借符号链接读到工作区之外。
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve()
            except OSError:
                continue
            if resolved != root_resolved and root_resolved not in resolved.parents:
                continue
            matches.append(path)
    except (OSError, ValueError):
        pass
    return sorted(matches)


def output_status(workspace: Path, outputs: Iterable[Any] | None) -> list[dict[str, Any]]:
    """产物是否存在（支持 glob），供验收与失败报告共用。"""
    root = Path(workspace)
    items: list[dict[str, Any]] = []
    for raw in output_paths(outputs):
        if is_glob(raw):
            matches = glob_matches(root, raw)
            size = sum(path.stat().st_size for path in matches if path.exists())
            items.append(
                {
                    "path": raw,
                    "exists": bool(matches),
                    "matches": [rel_posix(path, root) for path in matches[:50]],
                    "count": len(matches),
                    "size": size,
                    "glob": True,
                }
            )
            continue
        try:
            target = (root / raw).resolve()
            root_resolved = root.resolve()
            inside = target == root_resolved or root_resolved in target.parents
        except OSError:
            target, inside = root / raw, False
        exists = bool(inside and target.exists())
        size = int(target.stat().st_size) if exists and target.is_file() else 0
        items.append(
            {
                "path": raw,
                "exists": exists,
                "matches": [raw] if exists else [],
                "count": 1 if exists else 0,
                "size": size,
                "glob": False,
            }
        )
    return items


def infer_write_scope(outputs: Iterable[Any] | None) -> list[str]:
    """从产物反推最小写范围（目录级）。"""
    scopes: list[str] = []
    for raw in output_paths(outputs):
        text = to_posix(raw).strip()
        if not text or text.startswith("/") or (len(text) >= 2 and text[1] == ":"):
            continue
        if ".." in text.split("/"):
            continue
        glob_at = min((text.find(ch) for ch in "*?[" if ch in text), default=-1)
        prefix = text if glob_at < 0 else text[:glob_at]
        parts = [part for part in prefix.split("/") if part]
        if not parts:
            continue
        base = "/".join(parts[:-1] if glob_at < 0 else parts)
        if not base:
            base = parts[0]
        scope = f"{base}/**" if base else "**"
        if scope not in scopes:
            scopes.append(scope)
    return scopes


def infer_verify_command(root: str | Path) -> str:
    """为常见 Python 项目推断一个只读、低风险的编译验证命令。"""
    base = Path(root)
    py_dir = "src/python" if (base / "src" / "python").is_dir() else ("src" if (base / "src").is_dir() else "")
    has_python = bool(
        (base / "pyproject.toml").is_file()
        or (base / "requirements.txt").is_file()
        or any(base.glob("**/*.py"))
    )
    if not has_python:
        return ""
    target = py_dir or "."
    return f"python -m compileall -q {target}"


def build_acceptance_conditions(
    config: Config | None,
    kind: str,
    outputs: Iterable[Any] | None,
    *,
    verify_command: str | None = None,
    content_checks: Iterable[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """按类型模板展开验收条件；返回 ``(conditions, warnings)``。"""
    template = acceptance_template(config, kind)
    checks = [str(item) for item in (template.get("checks") or [])]
    min_bytes = int(template.get("min_bytes") or 1)
    paths = output_paths(outputs)
    warnings: list[str] = []
    conditions: list[dict[str, Any]] = []

    for check in checks:
        if check == "outputs_exist":
            for path in paths:
                if is_glob(path):
                    conditions.append({"type": "glob_min_files", "pattern": path, "min_count": 1})
                else:
                    conditions.append({"type": "file_exists", "path": path})
        elif check == "outputs_nonempty":
            for path in paths:
                if is_glob(path):
                    conditions.append(
                        {
                            "type": "glob_min_files",
                            "pattern": path,
                            "min_count": 1,
                            "min_bytes": min_bytes,
                        }
                    )
                else:
                    conditions.append(
                        {"type": "file_nonempty", "path": path, "min_bytes": min_bytes}
                    )
        elif check == "file_modified":
            conditions.append({"type": "file_modified"})
        elif check == "verify_command":
            if verify_command:
                conditions.append(
                    {
                        "type": "command_exit_zero",
                        "command": verify_command,
                        "timeout": int(template.get("command_timeout_sec") or 900),
                    }
                )
            else:
                warnings.append(
                    "未提供 verify_command：代码任务没有可执行验证命令，只能检查产物/改动。"
                )
        elif check == "no_writes":
            conditions.append({"type": "no_writes"})
        elif check == "handoff_present":
            conditions.append({"type": "handoff_present"})
        elif check == "no_temp_artifacts":
            conditions.append({"type": "no_temp_artifacts"})
        else:
            warnings.append(f"未知验收模板检查项：{check}")

    for item in content_checks or []:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or (paths[0] if paths else ""))
        text = str(item.get("text") or "")
        if path and text:
            conditions.append({"type": "content_contains", "path": path, "text": text})

    if not paths and not any(
        condition.get("type") in {"no_writes", "file_modified", "always"}
        for condition in conditions
    ):
        conditions.append({"type": "file_modified"})
    if not conditions:
        conditions.append({"type": "file_modified"})
        warnings.append("没有可用产物路径，验收回退为「至少改动一个文件」。")
    return _dedupe(conditions), warnings


def effective_acceptance(task: dict[str, Any], config: Config) -> tuple[list[dict[str, Any]], list[str]]:
    """执行期有效验收：不改变历史任务字段，但补齐类型硬约束。"""
    conditions = [copy.deepcopy(item) for item in (task.get("acceptance") or []) if isinstance(item, dict)]
    warnings: list[str] = []
    result = task.get("result") or {}
    guard = str(result.get("acceptance_guard") or "")
    paths = output_paths(task.get("outputs"))
    kind = str(task.get("kind") or "generic")
    verify_command = str(result.get("verify_command") or "")

    if guard == "artifact_or_handoff" or _only_exit_zero(conditions):
        conditions.append({"type": "file_modified"})
        conditions.append({"type": "handoff_present"})
        warnings.append("该任务未声明产物，执行期强制要求真实文件改动 + 交接说明。")

    for path in paths:
        if not _has_path_condition(conditions, path):
            conditions.append(
                {"type": "glob_min_files", "pattern": path, "min_count": 1}
                if is_glob(path)
                else {"type": "file_exists", "path": path}
            )

    if paths and kind in NONEMPTY_KINDS:
        min_bytes = int(acceptance_template(config, kind).get("min_bytes") or 1)
        for path in paths:
            if not _has_nonempty_condition(conditions, path):
                conditions.append(
                    {
                        "type": "glob_min_files",
                        "pattern": path,
                        "min_count": 1,
                        "min_bytes": min_bytes,
                    }
                    if is_glob(path)
                    else {"type": "file_nonempty", "path": path, "min_bytes": min_bytes}
                )

    if kind in CODE_KINDS:
        if verify_command and not _has_condition(conditions, "command_exit_zero"):
            conditions.append({"type": "command_exit_zero", "command": verify_command, "timeout": 900})
        elif not verify_command:
            warnings.append("代码任务未提供 verify_command，未执行构建/测试命令。")

    if kind == "review":
        if not _has_condition(conditions, "no_writes"):
            conditions.append({"type": "no_writes"})
        if not _has_condition(conditions, "handoff_present"):
            conditions.append({"type": "handoff_present"})

    return _dedupe(conditions), warnings


def _only_exit_zero(conditions: list[dict[str, Any]]) -> bool:
    return len(conditions) == 1 and str(conditions[0].get("type") or "") == "exit_zero"


def _has_condition(conditions: list[dict[str, Any]], ctype: str) -> bool:
    return any(str(item.get("type") or "") == ctype for item in conditions)


def _has_path_condition(conditions: list[dict[str, Any]], path: str) -> bool:
    for item in conditions:
        if str(item.get("type") or "") not in {"file_exists", "glob_min_files"}:
            continue
        if str(item.get("path") or item.get("pattern") or "") == path:
            return True
    return False


def _has_nonempty_condition(conditions: list[dict[str, Any]], path: str) -> bool:
    for item in conditions:
        if str(item.get("type") or "") not in {"file_nonempty", "glob_min_files"}:
            continue
        if str(item.get("path") or item.get("pattern") or "") == path:
            return True
    return False


def _dedupe(conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in conditions:
        key = (
            str(item.get("type") or ""),
            str(item.get("path") or item.get("pattern") or ""),
            str(item.get("command") or item.get("text") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def is_exit_zero_only(acceptance: Iterable[Any] | None) -> bool:
    items = [item for item in (acceptance or []) if isinstance(item, dict)]
    return len(items) == 1 and str(items[0].get("type") or "") == "exit_zero"


def prepare_creation_fields(
    config: Config,
    kind: str,
    *,
    outputs: Iterable[Any] | None = None,
    isolation: str | None = None,
    read_only: bool = False,
    write_scope: Iterable[str] | None = None,
    timeout_sec: int | None = None,
    max_attempts: int | None = None,
    acceptance: list[dict[str, Any]] | None = None,
    acceptance_ref: str | None = None,
    verify_command: str | None = None,
    allow_shared_write: bool = False,
    allow_unrestricted_write: bool = False,
) -> dict[str, Any]:
    """创建任务时的类型化默认值 + 风险提示。"""
    defaults = kind_defaults(config, kind)
    warnings: list[str] = []
    acceptance_was_unset = acceptance is None and acceptance_ref is None
    resolved_isolation = str(isolation or defaults.get("isolation") or "copy")
    if not read_only and resolved_isolation == "shared" and not allow_shared_write:
        resolved_isolation = "copy"
        warnings.append("修改型任务不能使用 shared 直写；已自动切换为 copy 隔离。")

    scope = [str(item).strip() for item in (write_scope or []) if str(item).strip()]
    paths = output_paths(outputs)
    if not scope and paths:
        scope = infer_write_scope(paths)
    if not read_only and not scope:
        if allow_unrestricted_write:
            warnings.append("已确认不受限写入：任何文件改动都可能进入工作区。")
        else:
            warnings.append(
                "write_scope 为空：任务可修改整个工作区。建议填写目录范围，"
                "或在界面显式勾选「不受限写入」。"
            )

    if acceptance is None and acceptance_ref is None:
        if paths:
            acceptance, acceptance_warnings = build_acceptance_conditions(
                config, kind, paths, verify_command=verify_command
            )
            warnings.extend(acceptance_warnings)
        else:
            acceptance, acceptance_warnings = build_acceptance_conditions(
                config, kind, [], verify_command=verify_command
            )
            warnings.extend(acceptance_warnings)
    elif is_exit_zero_only(acceptance) and not paths:
        warnings.append("验收只有 exit_zero；执行期会额外要求真实文件改动 + 交接说明。")

    resolved_timeout = timeout_sec
    if resolved_timeout is None:
        resolved_timeout = defaults.get("timeout_sec")
    try:
        resolved_timeout = int(resolved_timeout)
    except (TypeError, ValueError):
        resolved_timeout = defaults.get("timeout_sec")
    try:
        resolved_timeout = int(resolved_timeout)
    except (TypeError, ValueError):
        resolved_timeout = int(config.policy_get("default_timeout_sec", 1800))

    resolved_attempts = max_attempts
    if resolved_attempts is None:
        resolved_attempts = defaults.get("max_attempts")
    try:
        resolved_attempts = max(1, int(resolved_attempts))
    except (TypeError, ValueError):
        resolved_attempts = 3

    context = {
        "creation_warnings": warnings,
        "acceptance_guard": (
            "artifact_or_handoff"
            if not paths and (acceptance_was_unset or is_exit_zero_only(acceptance))
            else ""
        ),
        "verify_command": verify_command or "",
        "write_policy": {
            "scope": scope,
            "unrestricted": bool(not scope and not read_only),
            "confirmed": bool(allow_unrestricted_write),
        },
    }
    return {
        "isolation": resolved_isolation,
        "read_only": bool(read_only),
        "write_scope": scope,
        "timeout_sec": resolved_timeout,
        "max_attempts": resolved_attempts,
        "acceptance": acceptance,
        "acceptance_ref": acceptance_ref,
        "context": context,
        "warnings": warnings,
    }


def workspace_size(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def excluded_paths(workspace: Path, paths: Iterable[str], excludes: Iterable[str]) -> list[str]:
    return [path for path in paths if matches_any(path, excludes)]


def normalized_rel(workspace: Path, path: Path) -> str:
    try:
        return rel_posix(path, workspace)
    except ValueError:
        return to_posix(os.path.relpath(str(path), str(workspace)))
