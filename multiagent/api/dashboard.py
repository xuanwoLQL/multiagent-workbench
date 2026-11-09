"""指挥台聚合数据（契约 ``docs/UI-OPS-REDESIGN.md`` §3.1）。

一次请求返回首屏需要的全部数据，替代原先 10 个并发请求。两条硬约束：

1. **绝不调用会启动子进程的探测逻辑**。``/api/agents/detail`` 会走
   ``resolve_agent_argv``（Windows 上要查注册表、跑 ``reg.exe``），
   ``/api/agents/{id}/check`` 会真的跑 ``--version``。这些都不允许出现在首屏路径上：
   本模块的 agents 数据只来自**配置 + ``agent_usage`` 表**。
2. **单请求 ≤150ms（100 任务规模）**。全部走聚合 SQL（每个区块 1 条查询）；
   进度不调用 ``TaskService.progress()``（后者会对每个 pending 任务再查一次依赖，
   100 任务 = 100+ 次数据库往返）。工作区体积用**带时间预算**的遍历，
   绝不让一个超大目录把首屏拖死。

返回结构见契约；额外字段一律只增不改（前端按需读取）。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from ..tasks.service import TaskStatus
from ..tasks.runtime import runtime_info
from ..util import now_iso

#: 计数器预置全部状态，避免前端 ``counters.success`` 之类的索引拿到 undefined。
_STATUSES: tuple[str, ...] = tuple(status.value for status in TaskStatus)

#: 任务列表只取首屏需要的列（不 SELECT *，避免把 brief/result 这些大字段搬进响应）。
_TASK_COLUMNS = (
    "id, title, status, kind, agent_id, priority, read_only, updated_at, blocked_reason, "
    "timeout_sec, attempt, max_attempts, started_at"
)

_ACTIVITY_LIMIT = 8
_APPROVAL_LIMIT = 20

#: 工作区体积统计的时间预算（秒）。超时即返回已统计部分并标记 ``truncated``。
_WORKSPACE_BUDGET_SECONDS = 0.04
#: 工作区遍历的条目上限（防止海量小文件把预算内的循环拉长）。
_WORKSPACE_MAX_ENTRIES = 20000
#: 工作台自己的元数据目录不计入「工作区体积」。
_WORKSPACE_SKIP_DIRS = frozenset({".multiagent"})


def build_dashboard(app, project: dict[str, Any], *, running: bool = False) -> dict[str, Any]:
    """构造 ``GET /api/projects/{pid}/dashboard`` 的响应体。

    ``running`` 由调用方传入（``ApiContext.background`` 里的调度状态）；
    另外「有任务处于 running」也算正在跑，二者取或。
    """
    project_id = project["id"]
    tasks = _tasks(app, project_id)
    counters = _counters(tasks)
    return {
        "project": {
            "id": project_id,
            "name": project.get("name"),
            "label": project.get("label"),
            "root_path": project.get("root_path"),
            "created_at": project.get("created_at"),
            "readonly": bool(project.get("readonly")),
        },
        "progress": {
            "total": len(tasks),
            "counters": counters,
            "percent": _percent(counters, len(tasks)),
        },
        "tasks": tasks,
        "agents": _agents(app),
        "approvals": _approvals(app, project_id),
        "stats": _stats(app, project_id),
        "activity": _activity(app, project_id),
        "running": bool(running) or counters.get(TaskStatus.RUNNING.value, 0) > 0,
        "workspace": _workspace(project.get("root_path")),
        "server_time": now_iso(),
    }


# --------------------------------------------------------------------------- 任务


def _tasks(app, project_id: str) -> list[dict[str, Any]]:
    rows = app.db.query(
        f"SELECT {_TASK_COLUMNS} FROM tasks WHERE project_id = ? ORDER BY priority DESC, ordering ASC",
        (project_id,),
    )
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "kind": row["kind"],
            "agent_id": row["agent_id"],
            "priority": int(row["priority"] or 0),
            "read_only": bool(row["read_only"]),
            "updated_at": row["updated_at"],
            "blocked_reason": row["blocked_reason"],
            "runtime": runtime_info(dict(row), app.config),
        }
        for row in rows
    ]


def _counters(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counters = {status: 0 for status in _STATUSES}
    for task in tasks:
        status = str(task.get("status") or "")
        counters[status] = counters.get(status, 0) + 1
    return counters


def _percent(counters: dict[str, int], total: int) -> int:
    """完成度（%）：只按 ``success`` 计（失败/取消不算完成）。"""
    if total <= 0:
        return 0
    return int(round(counters.get(TaskStatus.SUCCESS.value, 0) * 100 / total))


# --------------------------------------------------------------------------- Agent


def _agents(app) -> list[dict[str, Any]]:
    """Agent 编队：**纯配置 + usage 表**，不做任何可执行文件探测（不 spawn 子进程）。"""
    usage = {row["agent_id"]: dict(row) for row in app.db.query("SELECT * FROM agent_usage")}
    items: list[dict[str, Any]] = []
    for agent in app.config.agents:
        agent_id = agent.get("id")
        row = usage.get(agent_id) or {}
        items.append(
            {
                "id": agent_id,
                "label": agent.get("label"),
                "protocol": agent.get("protocol"),
                "enabled": bool(agent.get("enabled", True)),
                "roles": list(agent.get("roles") or []),
                "strengths": list(agent.get("strengths") or []),
                "usage": {
                    "invocations": int(row.get("invocations") or 0),
                    "seconds": float(row.get("seconds") or 0),
                    "cost": float(row.get("cost") or 0),
                    "tokens": int(row.get("tokens") or 0),
                },
                "quota": dict(agent.get("quota") or {}),
            }
        )
    return items


# --------------------------------------------------------------------------- 审批


def _approvals(app, project_id: str) -> dict[str, Any]:
    row = app.db.query_one(
        "SELECT COUNT(*) AS c FROM approvals WHERE project_id = ? AND status = 'pending'",
        (project_id,),
    )
    pending = int(row["c"]) if row else 0
    rows = app.db.query(
        """
        SELECT id, kind, title, risk, created_at FROM approvals
         WHERE project_id = ? AND status = 'pending'
         ORDER BY created_at DESC LIMIT ?
        """,
        (project_id, _APPROVAL_LIMIT),
    )
    return {
        "pending": pending,
        "items": [
            {
                "id": r["id"],
                "kind": r["kind"],
                "title": r["title"],
                "risk": r["risk"],
                "created_at": r["created_at"],
            }
            for r in rows
        ],
    }


# --------------------------------------------------------------------------- 观测


def _stats(app, project_id: str) -> dict[str, Any]:
    row = app.db.query_one(
        """
        SELECT COUNT(*) AS runs,
               COALESCE(SUM(duration_ms), 0) AS duration_ms,
               COALESCE(SUM(cost_tokens), 0) AS tokens,
               COALESCE(SUM(cost_amount), 0) AS cost
          FROM runs WHERE project_id = ?
        """,
        (project_id,),
    )
    data = dict(row) if row else {}
    return {
        "runs": int(data.get("runs") or 0),
        "duration_ms": int(data.get("duration_ms") or 0),
        "tokens": int(data.get("tokens") or 0),
        "cost": round(float(data.get("cost") or 0), 6),
    }


def _activity(app, project_id: str) -> list[dict[str, Any]]:
    """最近活动（新→旧）。只取首屏需要的 4 个字段。"""
    rows = app.db.query(
        """
        SELECT at, kind, actor, summary FROM events
         WHERE project_id = ? ORDER BY id DESC LIMIT ?
        """,
        (project_id, _ACTIVITY_LIMIT),
    )
    return [
        {"at": r["at"], "kind": r["kind"], "actor": r["actor"], "summary": r["summary"]}
        for r in rows
    ]


# --------------------------------------------------------------------------- 工作区


def _workspace(root: str | None) -> dict[str, Any]:
    """工作区体积（文件数 / 字节数）。

    有**硬预算**：最多 40ms、最多 2 万个条目。超限返回已统计部分并标记
    ``truncated=True``，绝不因为工作区里有 node_modules / .git 这类巨型目录
    把首屏卡住。
    """
    result: dict[str, Any] = {"files": 0, "bytes": 0, "truncated": False}
    if not root:
        return result
    base = Path(root)
    if not base.is_dir():
        return result

    deadline = time.monotonic() + _WORKSPACE_BUDGET_SECONDS
    files = 0
    total = 0
    seen = 0
    stack: list[str] = [str(base)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    seen += 1
                    if seen > _WORKSPACE_MAX_ENTRIES or time.monotonic() > deadline:
                        result.update(files=files, bytes=total, truncated=True)
                        return result
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in _WORKSPACE_SKIP_DIRS:
                                stack.append(entry.path)
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    files += 1
                    total += int(getattr(stat, "st_size", 0) or 0)
        except OSError:
            continue
    result.update(files=files, bytes=total)
    return result
