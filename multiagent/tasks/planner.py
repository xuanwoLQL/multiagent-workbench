"""批量计划 / DAG 创建（CLI 与 API 共用）。"""

from __future__ import annotations

import copy
from pathlib import Path, PurePosixPath
from typing import Any

from ..util import safe_join, to_posix
from .acceptance import suggest_path_candidates
from .profiles import infer_write_scope, prepare_creation_fields

#: 计划预扫描处理的验收类型（只有这些类型才带「目标产物路径」语义）。
_PATH_SCAN_TYPES = frozenset(
    {"file_exists", "file_nonempty", "content_contains", "content_not_contains"}
)
#: 与验收诊断保持一致的候选上限。
_PATH_SCAN_CANDIDATE_LIMIT = 20
#: 告警里最多展示的候选数。
_PATH_SCAN_WARNING_DETAIL = 5
#: 视为 glob 的字符；含任一字符的路径不参与自动回填。
_GLOB_META = frozenset("*?[]")


def validate_plan(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """校验计划结构与 DAG；返回 items 列表。"""
    if not isinstance(spec, dict):
        raise ValueError("计划文件必须是 JSON 对象")
    items = spec.get("tasks")
    if not isinstance(items, list) or not items:
        raise ValueError("计划文件必须包含非空的 tasks 数组")
    keys: dict[str, int] = {}
    all_keys = {
        str(item.get("key"))
        for item in items
        if isinstance(item, dict) and item.get("key")
    }
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"tasks[{index}] 必须是 JSON 对象")
        key = item.get("key")
        if not key or not isinstance(key, str):
            raise ValueError(f"tasks[{index}].key 必填且必须是字符串")
        if key in keys:
            raise ValueError(f"计划中的 key 重复：{key}")
        keys[key] = index
        if not str(item.get("title") or "").strip():
            raise ValueError(f"tasks[{index}].title 必填")
        deps = item.get("depends_on") or []
        if not isinstance(deps, list) or not all(isinstance(dep, str) for dep in deps):
            raise ValueError(f"tasks[{index}].depends_on 必须是字符串数组")
        for dep in deps:
            if dep == key:
                raise ValueError(f"任务不能依赖自身：{key}")
            if dep not in all_keys:
                raise ValueError(f"tasks[{index}] 依赖了不存在的 key：{dep}")

    graph = {
        str(item.get("key")): [str(dep) for dep in (item.get("depends_on") or [])]
        for item in items
    }
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> None:
        if node in done:
            return
        if node in visiting:
            raise ValueError(f"计划存在循环依赖：{node}")
        visiting.add(node)
        for dep in graph.get(node, []):
            visit(dep)
        visiting.discard(node)
        done.add(node)

    for key in graph:
        visit(key)
    return [dict(item) for item in items]


# ---------------------------------------------------------------- 仓库预扫描
def _project_root(tasks_service, project_id: str) -> Path | None:
    """查询项目根目录；项目不存在时返回 None（跳过扫描，不阻断计划）。"""
    row = tasks_service.db.query_one(
        "SELECT root_path FROM projects WHERE id = ?", (project_id,)
    )
    if not row:
        return None
    raw = str(row["root_path"] or "").strip()
    return Path(raw) if raw else None


def _exact_text(value: Any) -> str | None:
    """提取精确路径文本；空值或含 glob 元字符时返回 None。"""
    if value is None or isinstance(value, (int, float, bool)):
        return None
    text = to_posix(str(value)).strip()
    if not text or any(char in text for char in _GLOB_META):
        return None
    return text


def _safe_exact_rel(text: str) -> str | None:
    """规范化为安全的相对 POSIX 路径；绝对路径 / ``..`` / 空路径返回 None。"""
    normalized = PurePosixPath(text).as_posix()
    if normalized in {"", "."}:
        return None
    if normalized.startswith("/"):
        return None
    if len(normalized) >= 2 and normalized[1] == ":":
        return None
    if ".." in normalized.split("/"):
        return None
    return normalized


def _path_exists(root: Path, rel: str) -> bool:
    try:
        return safe_join(root, rel).exists()
    except (OSError, ValueError):
        return False


def _candidates(root: Path, rel: str, cache: dict[str, list[str]]) -> list[str]:
    """按 basename 复用同一次 apply_plan 内的扫描结果。"""
    basename = PurePosixPath(rel).name
    if basename not in cache:
        cache[basename] = suggest_path_candidates(
            root, rel, limit=_PATH_SCAN_CANDIDATE_LIMIT
        )
    return cache[basename]


def _add_warning(
    warnings: list[dict[str, Any]], path: str, message: str, candidates: list[str]
) -> None:
    warnings.append(
        {
            "path": path,
            "message": message,
            "candidates": candidates[:_PATH_SCAN_WARNING_DETAIL],
        }
    )


def _replacement_for(
    root: Path,
    rel: str,
    cache: dict[str, list[str]],
    warnings: list[dict[str, Any]],
) -> str | None:
    """目标不存在时按 basename 诊断：恰好一个候选才回填，否则记告警。"""
    candidates = _candidates(root, rel, cache)
    basename = PurePosixPath(rel).name
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        _add_warning(
            warnings,
            rel,
            f"找到 {len(candidates)} 个同名候选（{basename}），未自动回填；请确认实际产物路径",
            candidates,
        )
    else:
        _add_warning(
            warnings,
            rel,
            f"未找到同名候选（{basename}），未自动回填；请确认实际产物路径",
            [],
        )
    return None


def _scan_outputs(
    outputs: Any,
    root: Path,
    cache: dict[str, list[str]],
    autofixes: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, str]:
    """回填 outputs 中的精确路径，返回 ``原路径 -> 实际路径`` 映射。"""
    fixes: dict[str, str] = {}
    if not isinstance(outputs, list):
        return fixes
    for index, entry in enumerate(outputs):
        if isinstance(entry, str):
            raw: Any = entry
        elif isinstance(entry, dict):
            raw = entry.get("path")
        else:
            continue
        text = _exact_text(raw)
        if text is None:
            continue
        rel = _safe_exact_rel(text)
        if rel is None:
            _add_warning(
                warnings, text, "路径越界（绝对路径或含 ..），已跳过自动回填", []
            )
            continue
        if _path_exists(root, rel):
            continue
        replacement = fixes.get(rel)
        if replacement is None:
            replacement = _replacement_for(root, rel, cache, warnings)
            if replacement is None or replacement == rel:
                continue
            fixes[rel] = replacement
            autofixes.append({"from": rel, "to": replacement, "where": "outputs"})
        if isinstance(entry, dict):
            entry["path"] = replacement
        else:
            outputs[index] = replacement
    return fixes


def _scan_acceptance(
    acceptance: Any,
    root: Path,
    cache: dict[str, list[str]],
    output_fixes: dict[str, str],
    autofixes: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    """回填验收条件里的精确路径；glob 与已存在的路径保持原样。"""
    if not isinstance(acceptance, list):
        return
    for condition in acceptance:
        if not isinstance(condition, dict):
            continue
        if str(condition.get("type") or "") not in _PATH_SCAN_TYPES:
            continue
        text = _exact_text(condition.get("path"))
        if text is None:
            continue
        rel = _safe_exact_rel(text)
        if rel is None:
            _add_warning(
                warnings, text, "路径越界（绝对路径或含 ..），已跳过自动回填", []
            )
            continue
        replacement = output_fixes.get(rel)
        if replacement is None:
            if _path_exists(root, rel):
                continue
            replacement = _replacement_for(root, rel, cache, warnings)
            if replacement is None or replacement == rel:
                continue
        condition["path"] = replacement
        autofixes.append({"from": rel, "to": replacement, "where": "acceptance"})


def _dedupe_scan(
    autofixes: list[dict[str, Any]], warnings: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    unique_fixes: list[dict[str, Any]] = []
    seen_fixes: set[tuple[str, str, str]] = set()
    for fix in autofixes:
        key = (str(fix.get("from")), str(fix.get("to")), str(fix.get("where")))
        if key in seen_fixes:
            continue
        seen_fixes.add(key)
        unique_fixes.append(fix)

    unique_warnings: list[dict[str, Any]] = []
    seen_warnings: set[tuple[str, str, tuple[str, ...]]] = set()
    for warning in warnings:
        key = (
            str(warning.get("path")),
            str(warning.get("message")),
            tuple(str(item) for item in (warning.get("candidates") or [])),
        )
        if key in seen_warnings:
            continue
        seen_warnings.add(key)
        unique_warnings.append(warning)
    return unique_fixes, unique_warnings


def _scan_item_paths(
    root: Path,
    acceptance: Any,
    outputs: Any,
    cache: dict[str, list[str]],
) -> tuple[Any, Any, dict[str, Any], dict[str, str]]:
    """扫描单个计划项，返回 ``(acceptance, outputs, scan_record, output_fixes)``。

    只修正「预期路径写错」，不改变验收阈值；改写后的路径仍由 Agent 的真实
    产出决定是否通过，因此绝不放宽验收。
    """
    acceptance_copy = (
        copy.deepcopy(acceptance) if isinstance(acceptance, list) else acceptance
    )
    outputs_copy = (
        copy.deepcopy(list(outputs)) if isinstance(outputs, (list, tuple)) else outputs
    )
    autofixes: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    output_fixes = _scan_outputs(outputs_copy, root, cache, autofixes, warnings)
    _scan_acceptance(
        acceptance_copy, root, cache, output_fixes, autofixes, warnings
    )
    unique_fixes, unique_warnings = _dedupe_scan(autofixes, warnings)
    return (
        acceptance_copy,
        outputs_copy,
        {"autofixes": unique_fixes, "warnings": unique_warnings},
        output_fixes,
    )


def _prepare_item(tasks_service, item: dict[str, Any]) -> dict[str, Any]:
    """计算单个计划项的创建字段（含模板特例；不写库）。"""
    kind = str(item.get("kind") or "generic")
    defaults = prepare_creation_fields(
        tasks_service.config,
        kind,
        outputs=item.get("outputs"),
        isolation=item.get("isolation"),
        read_only=bool(item.get("read_only")),
        write_scope=item.get("write_scope"),
        timeout_sec=item.get("timeout_sec"),
        max_attempts=item.get("max_attempts"),
        acceptance=item.get("acceptance"),
        acceptance_ref=item.get("acceptance_ref"),
        verify_command=item.get("verify_command") or (item.get("context") or {}).get("verify_command"),
        allow_shared_write=bool(item.get("allow_shared_write")),
        allow_unrestricted_write=bool(item.get("allow_unrestricted_write")),
    )
    context = dict(item.get("context") or {})
    context.update(defaults.get("context") or {})
    acceptance = defaults["acceptance"]
    if item.get("template") and item.get("acceptance") is None and item.get("acceptance_ref") is None:
        acceptance = None
        context["acceptance_guard"] = ""
    return {
        "kind": kind,
        "defaults": defaults,
        "context": context,
        "acceptance": acceptance,
        "outputs": item.get("outputs"),
    }


def apply_plan(tasks_service, project_id: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """按计划创建任务；全量校验通过后才写库。

    创建前会扫描项目根目录：验收条件与 outputs 中的精确路径若不存在、但按
    basename 只匹配到唯一候选，则回填为实际产物的相对 POSIX 路径；多候选或
    无候选只记录告警，不阻断创建。扫描结果随任务持久化到
    ``context["acceptance_path_scan"]``（任务字段中表现为 ``result``）。
    """
    items = validate_plan(spec)
    root = _project_root(tasks_service, project_id)
    scan_cache: dict[str, list[str]] = {}
    prepared: list[dict[str, Any]] = []
    for item in items:
        entry = _prepare_item(tasks_service, item)
        if root is not None:
            acceptance, outputs, scan_record, output_fixes = _scan_item_paths(
                root, entry["acceptance"], entry["outputs"], scan_cache
            )
            entry["acceptance"] = acceptance
            entry["outputs"] = outputs
            entry["context"]["acceptance_path_scan"] = scan_record
            # outputs 被回填后，同步「由产物推导」的写范围，避免任务被旧范围卡住。
            if output_fixes and not item.get("write_scope"):
                entry["defaults"]["write_scope"] = infer_write_scope(outputs)
                write_policy = entry["context"].get("write_policy")
                if isinstance(write_policy, dict):
                    write_policy["scope"] = list(entry["defaults"]["write_scope"])
        prepared.append(entry)

    key_to_id: dict[str, str] = {}
    created: list[dict[str, Any]] = []
    with tasks_service.db.transaction():
        for item, entry in zip(items, prepared):
            defaults = entry["defaults"]
            deps = [key_to_id[key] for key in item.get("depends_on", []) if key in key_to_id]
            task = tasks_service.create(
                project_id,
                str(item["title"]).strip(),
                kind=entry["kind"],
                brief=item.get("brief", ""),
                template_id=item.get("template"),
                agent_id=item.get("agent"),
                role=item.get("role"),
                isolation=defaults["isolation"],
                read_only=defaults["read_only"],
                write_scope=defaults["write_scope"],
                acceptance=entry["acceptance"],
                acceptance_ref=defaults["acceptance_ref"],
                inputs=item.get("inputs"),
                outputs=entry["outputs"],
                depends_on=deps,
                priority=item.get("priority", 0),
                max_attempts=defaults["max_attempts"],
                timeout_sec=defaults["timeout_sec"],
                tags=item.get("tags"),
                context=entry["context"],
            )
            if item.get("key"):
                key_to_id[str(item["key"])] = task["id"]
            created.append(task)

        # 前置顺序未按拓扑排序时，补加剩余依赖边。
        for item, task in zip(items, created):
            for key in item.get("depends_on", []):
                dep_id = key_to_id.get(str(key))
                if dep_id and dep_id != task["id"]:
                    tasks_service.add_edge(task["id"], dep_id)
    return created
