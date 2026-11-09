"""「一句话任务」（quick-task）的纯逻辑：标题、类型推断、验收默认值、三步计划。

契约见 ``docs/UI-OPS-REDESIGN.md`` §3.2。本模块**不碰 HTTP、不管理数据库连接**：
只把用户原话翻译成 :meth:`multiagent.tasks.service.TaskService.create` 需要的参数，
方便单测与复用（CLI 以后也能直接用）。

设计要点：

* ``text`` 是用户原话，原样进 ``brief``（不截断、不改写）；标题才做截断；
* ``kind=auto`` 按关键词打分推断：命中数多者胜，平手时「先出现者」胜，
  再平手按契约给出的顺序（code → doc → research → review → data → generic）；
  这样 ``审阅文档`` → review（动作词在前）、``写一份报告`` → doc；
* 验收默认：有 ``outputs`` → 每个产物一条 ``file_exists``；没有 → ``exit_zero``，
  但执行期会额外要求真实文件改动（不再只凭退出码成功）；
* ``mode=plan`` 固定三步 DAG：①调研整理（research）→ ②产出初稿（doc）→ ③只读审阅（review 模板），
  依赖一律 ``success``。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..tasks.profiles import (
    infer_write_scope,
    kind_defaults,
    prepare_creation_fields,
)

#: 契约上限：``text`` 1..20000 字符。
MAX_TEXT_CHARS = 20000
#: 缺省标题从首行截断的字符数。
TITLE_LIMIT = 60
#: 显式传入的标题最多保留的字符数（防止 20k 字的标题把看板撑爆）。
TITLE_MAX_CHARS = 200
#: 单个产物路径 / 产物条数的上限（防止一次请求塞进海量 file_exists 验收条件）。
MAX_OUTPUT_ITEMS = 50
MAX_OUTPUT_CHARS = 512

MODES = ("single", "plan", "project")
BUILTIN_KINDS = ("code", "doc", "data", "research", "review", "ppt", "diagrams", "web", "generic")

#: ``kind=auto`` 关键词表。**顺序即契约顺序**（仅用于平手时的最终裁决）。
_KIND_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "code",
        (
            "代码", "报错", "重构", "函数", "编译", "脚本", "单元测试", "测试", "接口",
            "bug", "debug", "traceback", "refactor", "code", "pytest", "stack trace",
        ),
    ),
    (
        "doc",
        (
            "报告", "文档", "撰写", "写作", "总结", "方案", "说明", "readme",
            "report", "document", "writeup", "幻灯片", "演示文稿",
        ),
    ),
    (
        "ppt",
        (
            "ppt", "pptx", "幻灯片", "演示", "汇报材料", "slide", "deck",
        ),
    ),
    (
        "diagrams",
        (
            "图表", "架构图", "流程图", "甘特图", "柱状图", "折线图", "雷达图",
            "diagram", "chart", "visualization", "可视化",
        ),
    ),
    (
        "web",
        (
            "网页", "前端", "页面", "three.js", "threejs", "3d", "html", "css",
            "javascript", "web",
        ),
    ),
    (
        "research",
        (
            "调研", "竞品", "资料", "搜集", "搜索", "研究", "盘点", "梳理",
            "research", "survey",
        ),
    ),
    (
        "review",
        (
            "审阅", "评审", "复查", "代码审查", "review",
        ),
    ),
    (
        "data",
        (
            "表格", "数据", "统计", "清洗", "报表", "excel", "csv", "xlsx", "sql",
        ),
    ),
)

_ASCII_WORD = re.compile(r"^[A-Za-z0-9_ .\-]+$")


class QuickTaskError(ValueError):
    """请求不合法（API 层转 400，而不是 500）。"""


# --------------------------------------------------------------------------- 解析


def parse_quick_task(payload: dict[str, Any], *, config=None) -> dict[str, Any]:
    """把请求体规范化成内部 spec（所有校验都在这里，API 层只负责转 HTTP 错误码）。"""
    if not isinstance(payload, dict):
        raise QuickTaskError("请求体必须是 JSON 对象")

    raw_text = payload.get("text")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise QuickTaskError("text 必填（描述你要完成的任务）")
    if len(raw_text) > MAX_TEXT_CHARS:
        raise QuickTaskError(f"text 过长：上限 {MAX_TEXT_CHARS} 字符（当前 {len(raw_text)}）")

    outputs = _parse_outputs(payload.get("outputs"))
    title = derive_title(raw_text, payload.get("title"))

    kind_raw = payload.get("kind") or "auto"
    kind_raw = str(kind_raw).strip().lower()
    if kind_raw in ("", "auto"):
        kind = infer_kind(raw_text)
    else:
        kind = kind_raw
        allowed = _allowed_kinds(config)
        if kind not in allowed:
            raise QuickTaskError(f"未知任务类型：{kind_raw}（可选：auto/{'/'.join(sorted(allowed))}）")

    agent_id = payload.get("agent_id")
    if agent_id is not None:
        agent_id = str(agent_id).strip()
        if agent_id in ("", "auto"):
            agent_id = None
    if agent_id and config is not None and config.agent(agent_id) is None:
        raise QuickTaskError(f"未配置的 Agent：{agent_id}")

    mode = str(payload.get("mode") or "single").strip().lower()
    if mode not in MODES:
        raise QuickTaskError(f"mode 只支持 {'/'.join(MODES)}（收到 {mode}）")

    isolation = payload.get("isolation")
    if isolation is not None:
        isolation = str(isolation).strip() or None

    write_scope = _parse_list(payload.get("write_scope"), "write_scope")
    source_files = _parse_list(payload.get("source_files"), "source_files")
    content_checks = payload.get("content_checks")
    if content_checks is not None and not isinstance(content_checks, list):
        raise QuickTaskError("content_checks 必须是对象数组")
    timeout_sec = _optional_positive_int(payload.get("timeout_sec"), "timeout_sec")
    max_attempts = _optional_positive_int(payload.get("max_attempts"), "max_attempts")
    verify_command = payload.get("verify_command")
    if verify_command is not None:
        verify_command = str(verify_command).strip() or None
        if verify_command and len(verify_command) > 4000:
            raise QuickTaskError("verify_command 过长（上限 4000 字符）")

    defaults = kind_defaults(config, kind)
    resolved_isolation = isolation or ("copy" if not _as_flag(payload.get("read_only")) else None)
    prepared = prepare_creation_fields(
        config,
        kind,
        outputs=outputs,
        isolation=resolved_isolation,
        read_only=_as_flag(payload.get("read_only")),
        write_scope=write_scope,
        timeout_sec=timeout_sec,
        max_attempts=max_attempts,
        acceptance=None,
        acceptance_ref=None,
        verify_command=verify_command,
        allow_shared_write=_as_flag(payload.get("allow_shared_write")),
        allow_unrestricted_write=_as_flag(payload.get("allow_unrestricted_write")),
    )
    warnings = list(prepared["warnings"])
    if len(raw_text) > 4000:
        warnings.append("brief 较长（>4000 字），建议使用 project 拆解模式。")
    if re.search(r"\.pdf\b", raw_text, re.I):
        warnings.append("brief 提到 PDF：建议使用 project 拆解模式并传入 source_files。")
    if mode == "project" and not source_files:
        warnings.append("project 模式将自动扫描项目根目录下的 PDF/需求文件。")

    return {
        "text": raw_text,
        "title": title,
        "outputs": outputs,
        "kind": kind,
        "kind_explicit": kind_raw not in ("", "auto"),
        "agent_id": agent_id,
        "read_only": _as_flag(payload.get("read_only")),
        "isolation": prepared["isolation"],
        "write_scope": prepared["write_scope"],
        "timeout_sec": prepared["timeout_sec"],
        "max_attempts": prepared["max_attempts"],
        "verify_command": verify_command,
        "content_checks": content_checks or [],
        "source_files": source_files,
        "allow_shared_write": _as_flag(payload.get("allow_shared_write")),
        "allow_unrestricted_write": _as_flag(payload.get("allow_unrestricted_write")),
        "mode": mode,
        "start": _as_flag(payload.get("start")),
        "preview": _as_flag(payload.get("preview")) if "preview" in payload else mode == "project",
        "warnings": warnings,
    }


def _parse_outputs(value: Any) -> list[str]:
    """产物列表：接受数组，也接受「逗号 / 顿号 / 分号分隔」的字符串（前端输入框原样提交）。"""
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,，、;；\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = [str(item) for item in value]
    else:
        raise QuickTaskError("outputs 必须是字符串数组（或逗号分隔的字符串）")
    result: list[str] = []
    for part in parts:
        item = str(part).strip().strip('"').strip("'")
        if not item:
            continue
        if len(item) > MAX_OUTPUT_CHARS:
            raise QuickTaskError(f"产物路径过长（上限 {MAX_OUTPUT_CHARS} 字符）：{item[:40]}…")
        if item not in result:
            result.append(item)
    if len(result) > MAX_OUTPUT_ITEMS:
        raise QuickTaskError(f"产物文件过多（上限 {MAX_OUTPUT_ITEMS} 个）")
    return result


def _parse_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    items: list[str]
    if isinstance(value, str):
        items = re.split(r"[,，、;；\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        items = [str(item) for item in value]
    else:
        raise QuickTaskError(f"{label} 必须是字符串数组（或逗号分隔的字符串）")
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return result[:MAX_OUTPUT_ITEMS]


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise QuickTaskError(f"{name} 必须是整数") from exc
    if number <= 0:
        raise QuickTaskError(f"{name} 必须大于 0")
    return number


def _allowed_kinds(config) -> set[str]:
    allowed = set(BUILTIN_KINDS)
    if config is not None:
        try:
            allowed |= set((config.policy_get("kinds", {}) or {}).keys())
        except Exception:  # noqa: BLE001 - 配置异常不应让建任务失败
            pass
    return allowed


def _as_flag(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- 标题 / 类型


def derive_title(text: str, title: Any = None) -> str:
    """标题：显式给就用显式的；否则取 ``text`` 首行截 60 字。"""
    if isinstance(title, str) and title.strip():
        return title.strip()[:TITLE_MAX_CHARS]
    stripped = (text or "").strip()
    if not stripped:
        return ""
    first_line = stripped.splitlines()[0].strip()
    return first_line[:TITLE_LIMIT]


def infer_kind(text: str) -> str:
    """按关键词推断任务类型（契约 §3.2）。命中数多者胜；平手看谁先出现；再平手看契约顺序。"""
    lowered = (text or "").lower()
    if not lowered.strip():
        return "generic"
    best_kind = "generic"
    best_score = 0
    best_position = len(lowered) + 1
    for kind, keywords in _KIND_KEYWORDS:
        positions = [pos for pos in (_keyword_position(lowered, kw) for kw in keywords) if pos >= 0]
        if not positions:
            continue
        score = len(positions)
        position = min(positions)
        if score > best_score or (score == best_score and position < best_position):
            best_kind, best_score, best_position = kind, score, position
    return best_kind


def _keyword_position(text: str, keyword: str) -> int:
    """关键词在文本里的位置（-1 = 未命中）。

    纯 ASCII 关键词按词边界匹配，避免 ``doc`` 命中 ``docker``、``code`` 命中 ``codex``；
    中文关键词直接子串匹配（中文没有词边界）。
    """
    key = keyword.lower()
    if _ASCII_WORD.match(key):
        match = re.search(rf"(?<![a-z0-9_]){re.escape(key)}(?![a-z0-9_])", text)
        return match.start() if match else -1
    return text.find(key)


# --------------------------------------------------------------------------- 验收


def default_acceptance(outputs: Iterable[str] | None) -> list[dict[str, Any]]:
    """验收默认：有产物 → 每个都要 ``file_exists``；没有产物 → ``exit_zero``。"""
    items = [str(item) for item in (outputs or []) if str(item).strip()]
    if items:
        return [{"type": "file_exists", "path": item} for item in items]
    return [{"type": "exit_zero"}]


# --------------------------------------------------------------------------- 计划


def plan_specs(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """``mode=plan`` 的三步 DAG 规格（不含 id，依赖用步骤下标表达）。

    ①调研整理（research）→ ②产出初稿（doc）→ ③只读审阅（review 模板）。
    """
    base = spec["title"]
    text = spec["text"]
    outputs = spec["outputs"]
    scope = spec.get("write_scope") or infer_write_scope(outputs)
    timeout_sec = int(spec.get("timeout_sec") or 1800)
    max_attempts = int(spec.get("max_attempts") or 3)
    return [
        {
            "step": 1,
            "title": f"调研整理：{base}",
            "kind": "research",
            "brief": text,
            "outputs": [],
            "acceptance": [{"type": "exit_zero"}],
            "read_only": spec["read_only"],
            "isolation": spec["isolation"],
            "agent_id": spec["agent_id"],
            "timeout_sec": timeout_sec,
            "max_attempts": max_attempts,
            "write_scope": [],
            "context": {"acceptance_guard": "artifact_or_handoff"},
            "depends_on_step": None,
        },
        {
            "step": 2,
            "title": f"产出初稿：{base}",
            "kind": "doc",
            "brief": text,
            "outputs": outputs,
            "acceptance": default_acceptance(outputs),
            "read_only": spec["read_only"],
            "isolation": spec["isolation"],
            "agent_id": spec["agent_id"],
            "timeout_sec": timeout_sec,
            "max_attempts": max_attempts,
            "write_scope": scope,
            "context": {"verify_command": spec.get("verify_command") or ""},
            "depends_on_step": 1,
        },
        {
            "step": 3,
            "title": f"只读审阅：{base}",
            "kind": "review",
            # 审阅必须真只读：模板 review 自带 isolation=shared / read_only=True /
            # acceptance_ref=review-readonly，这里显式传 template_id 让配置说了算。
            "template_id": "review",
            "brief": text,
            "outputs": [],
            "acceptance": None,
            "read_only": True,
            "isolation": None,
            "agent_id": spec["agent_id"],
            "timeout_sec": min(timeout_sec, 1800),
            "max_attempts": 2,
            "write_scope": [],
            "context": {},
            "depends_on_step": 2,
        },
    ]


# --------------------------------------------------------------------------- 创建


def create_quick_tasks(tasks_service, project_id: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """按 spec 建任务（single=1 个 / plan=3 个 DAG），返回创建出的任务列表。"""
    if spec["mode"] == "project":
        raise QuickTaskError(
            "project 模式需要先预览 DAG：请调用 /decompose/preview，"
            "确认/调整后再调用 /decompose/apply。"
        )
    if spec["mode"] == "single":
        task = tasks_service.create(
            project_id,
            spec["title"],
            kind=spec["kind"],
            brief=spec["text"],
            agent_id=spec["agent_id"],
            isolation=spec["isolation"],
            read_only=spec["read_only"],
            write_scope=spec.get("write_scope"),
            outputs=spec["outputs"] or None,
            acceptance=default_acceptance(spec["outputs"]),
            max_attempts=spec.get("max_attempts") or 3,
            timeout_sec=spec.get("timeout_sec") or 1800,
            context={
                "acceptance_guard": "artifact_or_handoff" if not spec["outputs"] else "",
                "verify_command": spec.get("verify_command") or "",
                "creation_warnings": spec.get("warnings") or [],
            },
        )
        return [task]

    created: list[dict[str, Any]] = []
    for step in plan_specs(spec):
        depends_on: list[str] = []
        if step["depends_on_step"]:
            depends_on = [created[step["depends_on_step"] - 1]["id"]]
        task = tasks_service.create(
            project_id,
            step["title"],
            kind=step["kind"],
            brief=step["brief"],
            template_id=step.get("template_id"),
            agent_id=step["agent_id"],
            isolation=step["isolation"],
            read_only=step["read_only"],
            write_scope=step.get("write_scope"),
            outputs=step["outputs"] or None,
            acceptance=step["acceptance"],
            depends_on=depends_on,
            max_attempts=step.get("max_attempts") or 3,
            timeout_sec=step.get("timeout_sec") or 1800,
            context=step.get("context"),
        )
        created.append(task)
    return created


def response_payload(
    project_id: str,
    mode: str,
    created: list[dict[str, Any]],
    *,
    started: bool,
    note: str = "",
    warnings: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """契约 §3.2 的响应体。"""
    count = len(created)
    if mode == "plan":
        message = f"已创建 {count} 个任务（调研整理 → 产出初稿 → 只读审阅）"
    else:
        message = f"已创建 {count} 个任务"
    if started:
        message += "，已在后台开始调度"
    elif note:
        message += f"（{note}）"
    return {
        "project_id": project_id,
        "mode": mode,
        "started": bool(started),
        "warnings": list(warnings or []),
        "tasks": [
            {
                "id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "timeout_sec": task.get("timeout_sec"),
                "max_attempts": task.get("max_attempts"),
            }
            for task in created
        ],
        "message": message,
        **(extra or {}),
    }
