"""上下文装配与预算控制（需求 27-31）。

派发新任务时，自动把「相关交接说明 + 任务简报 + 黑板条目」注入给执行的 Agent；
并按字符预算截断，避免历史挤占上下文窗口（需求 29）。
"""

from __future__ import annotations

from typing import Any

from ..board.handoff import render_handoff
from ..config import Config
from ..util import truncate


def build_context(
    *,
    config: Config,
    project: dict[str, Any],
    task: dict[str, Any],
    board,
    handoffs: list[dict[str, Any]] | None = None,
    predecessor_handoffs: list[dict[str, Any]] | None = None,
    variables: dict[str, Any] | None = None,
    budget_chars: int | None = None,
    resume_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    injection = config.policy_get("injection", {}) or {}
    budget = int(budget_chars or config.policy_get("context_budget_chars", 12000))

    packet: dict[str, Any] = {
        "task": {
            "id": task["id"],
            "title": task["title"],
            "kind": task["kind"],
            "role": task.get("role"),
            "read_only": bool(task.get("read_only")),
            "brief": _fill(task.get("brief", ""), variables or {}),
            "write_scope": task.get("write_scope") or [],
        },
        # 注意：这里给 Agent 看的「工作区」必须是**本次任务的工作目录**（隔离副本），
        # 不是项目原始根目录。否则 Agent 会绕过隔离直接写原目录（真实故障：Claude
        # 看到 project.root 后跑去读编排器源码、在错误目录里空转）。
        "project": {
            "id": project["id"],
            "name": project["name"],
            "root": (variables or {}).get("workspace") or project["root_path"],
        },
        "handoffs": [],
        "decisions": [],
        "issues": [],
        "todos": [],
        "memory": [],
        "do_not": [],
        "resume": resume_info or None,
        "budget_chars": budget,
    }

    if injection.get("include_handoffs", True):
        pool = list(predecessor_handoffs or []) + list(handoffs or [])
        seen = set()
        for item in pool:
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            packet["handoffs"].append(
                {
                    "task_id": item["task_id"],
                    "agent_id": item.get("agent_id"),
                    "did": item.get("did", ""),
                    "not_done": item.get("not_done", ""),
                    "open_issues": item.get("open_issues", []),
                    "next_steps": item.get("next_steps", []),
                    "artifacts": item.get("artifacts", []),
                    "source": item.get("source", "agent"),
                }
            )
            if len(packet["handoffs"]) >= int(injection.get("max_handoffs", 5)):
                break

    max_per_type = int(injection.get("max_entries_per_type", 8))
    if injection.get("include_decisions", True):
        packet["decisions"] = _entries(board, project["id"], "decision", max_per_type)
    if injection.get("include_open_issues", True):
        packet["issues"] = _entries(board, project["id"], "issue", max_per_type)
    if injection.get("include_todos", True):
        packet["todos"] = _entries(board, project["id"], "todo", max_per_type)
    if injection.get("include_project_memory", True):
        packet["memory"] = [
            {"title": e["title"], "body": e["body"]} for e in board.long_term_memory(project["id"], limit=max_per_type)
        ]

    # 已知问题 / 死路 → do_not 清单，避免重复劳动
    for issue in packet["issues"]:
        packet["do_not"].append(issue["title"] or issue["body"][:80])

    text = render_context(packet, budget)
    packet["text"] = text
    packet["chars"] = len(text)
    return packet


def _entries(board, project_id: str, type_: str, limit: int) -> list[dict[str, Any]]:
    return [
        {"id": e["id"], "title": e["title"], "body": e["body"], "updated_at": e["updated_at"]}
        for e in board.list(project_id, type=type_, status="approved", limit=limit)
    ]


def _fill(text: str, variables: dict[str, Any]) -> str:
    from ..tasks.acceptance import substitute

    return substitute(text or "", variables)


def render_context(packet: dict[str, Any], budget: int) -> str:
    lines: list[str] = []
    task = packet["task"]
    lines.append("# 任务上下文")
    lines.append("")
    lines.append(f"- 工作区：{packet['project']['name']}（{packet['project']['root']}）")
    lines.append(f"- 任务：{task['title']}（类型 {task['kind']}／角色 {task.get('role')}）")
    if task.get("read_only"):
        lines.append("- 权限：**只读**，不要修改任何文件")
    if task.get("write_scope"):
        lines.append(f"- 可写范围：{', '.join(task['write_scope'])}")
    lines.append("")
    lines.append("## 任务说明")
    lines.append("")
    lines.append(task.get("brief") or "（无）")
    lines.append("")

    if packet["handoffs"]:
        lines.append("## 前置交接说明（来自已完成任务）")
        lines.append("")
        for item in packet["handoffs"]:
            lines.append(f"### 来自任务 {item['task_id']}")
            lines.append(render_handoff(item))
            lines.append("")

    for key, title in (("decisions", "已定决策"), ("issues", "已知问题 / 死路"), ("todos", "待办"), ("memory", "项目长期记忆")):
        items = packet.get(key) or []
        if not items:
            continue
        lines.append(f"## {title}")
        lines.append("")
        for item in items:
            if "updated_at" in item:
                lines.append(f"- [{item['id']}] {item['title']}: {item['body']}")
            else:
                lines.append(f"- {item['title']}: {item['body']}")
        lines.append("")

    if packet["do_not"]:
        lines.append("## 不要重复尝试（已知无效）")
        lines.append("")
        for item in packet["do_not"]:
            lines.append(f"- {item}")
        lines.append("")

    resume = packet.get("resume") or {}
    if resume:
        lines.append("## 上一次尝试的部分产物已恢复")
        lines.append("")
        lines.append(
            f"系统已把上一次尝试中 **{resume.get('restored', 0)}** 个仍在写范围内的文件"
            f"恢复到当前工作副本（来源 run：`{resume.get('source_run_id') or '未知'}`）。"
        )
        if resume.get("skipped"):
            lines.append(
                f"另有 {resume['skipped']} 个越权/保护路径/越界改动**没有**恢复，"
                "不要尝试从工作目录之外取回它们。"
            )
        if resume.get("files"):
            lines.append("")
            lines.append("已恢复文件（最多列出 40 个）：")
            lines.append("")
            for rel in resume["files"][:40]:
                lines.append(f"- `{rel}`")
        lines.append("")
        lines.append(
            "请先核对已恢复的产物与当前任务说明，**在现有基础上继续完成**，"
            "不要推倒重来；如果发现半成品与目标冲突，先修正再继续。"
        )
        lines.append("")

    body = "\n".join(lines).strip()
    return truncate(body, budget, marker="\n\n…[上下文已按预算截断]")


def render_task_prompt(packet: dict[str, Any], handoff_path: str | None = None) -> str:
    """把上下文 + 明确的产出要求拼成给 Agent 的 prompt。"""
    parts = [packet["text"], "", "---", "# 执行要求", ""]
    parts.append("1. 只在当前工作目录内读写文件；不要越出工作区，不要 `cd` 到工作目录之外。")
    parts.append(
        "2. 不要读取或分析工作目录之外的路径（包括编排器源码、其他任务目录、用户主目录）；"
        "当前工作目录就是你的全部输入。"
    )
    if packet["task"].get("read_only"):
        parts.append("3. 这是一个**只读**任务：只做分析/评审，不要修改任何文件。")
    parts.append(
        "4. 任务结束时，必须产出一份结构化交接说明。优先把它写成 JSON 文件："
        f"`{handoff_path}`，字段为 "
        '`did`（做了什么）、`not_done`（没做什么）、`open_issues`（遗留问题，数组）、'
        '`next_steps`（下一步建议，数组）、`artifacts`（产物路径，数组）、`notes`（备注）。'
    )
    parts.append("5. 完成即结束进程；不要询问，直接执行。")
    return "\n".join(parts)
