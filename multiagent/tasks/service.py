"""任务服务：拆解、依赖、模板、状态机、产物（需求 13-18、38-41）。"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from ..config import Config
from ..db import Database
from ..util import dumps, is_binary_path, loads, new_id, now_iso, rel_posix, sha256_file
from .profiles import effective_acceptance, kind_defaults as task_kind_defaults
from .runtime import runtime_info


class TaskStatus(str, Enum):
    PENDING = "pending"        # 待执行
    RUNNING = "running"        # 执行中
    SUCCESS = "success"        # 成功
    FAILED = "failed"          # 失败
    BLOCKED = "blocked"        # 被阻塞（依赖未满足 / 等待人工 / 冲突）
    CANCELLED = "cancelled"    # 已取消
    WAITING = "waiting"        # 等待人工执行
    SKIPPED = "skipped"        # 已跳过
    INTERRUPTED = "interrupted"  # 中断（可续跑）


FINAL_STATUSES = {TaskStatus.SUCCESS.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value, TaskStatus.SKIPPED.value}
ACTIVE_STATUSES = {TaskStatus.RUNNING.value, TaskStatus.WAITING.value, TaskStatus.INTERRUPTED.value}
RESUMABLE_STATUSES = {TaskStatus.INTERRUPTED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}

# 可以「认领执行」的状态（原子 claim 的起点）。running 不在其中：已被别人认领。
CLAIMABLE_STATUSES = {
    TaskStatus.PENDING.value,
    TaskStatus.INTERRUPTED.value,
    TaskStatus.FAILED.value,
    TaskStatus.BLOCKED.value,
    TaskStatus.WAITING.value,
}

# 合法状态跃迁表（护栏）。设计约束：
# - 终态 success / cancelled 不能被「重新执行」：success 只能由显式的人工回滚改回 pending；
# - interrupted 不能直接置 success（必须经过 running，否则等于凭空成功）；
# - running → pending 被拒绝（否则在跑的任务会被重试派发，造成重复执行）；
# - skipped 保留人工 retry 复活路径（既有 CLI/API 契约）；
# - 未在表中的旧状态（历史数据）不阻断，避免升级后老库不可操作。
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    TaskStatus.PENDING.value: {
        TaskStatus.PENDING.value, TaskStatus.RUNNING.value, TaskStatus.BLOCKED.value,
        TaskStatus.CANCELLED.value, TaskStatus.SKIPPED.value, TaskStatus.WAITING.value,
        TaskStatus.SUCCESS.value, TaskStatus.FAILED.value,
    },
    TaskStatus.RUNNING.value: {
        TaskStatus.RUNNING.value, TaskStatus.SUCCESS.value, TaskStatus.FAILED.value,
        TaskStatus.BLOCKED.value, TaskStatus.WAITING.value, TaskStatus.INTERRUPTED.value,
        TaskStatus.CANCELLED.value,
    },
    TaskStatus.SUCCESS.value: {
        TaskStatus.SUCCESS.value, TaskStatus.PENDING.value,  # pending：人工回滚后重跑
        TaskStatus.CANCELLED.value, TaskStatus.BLOCKED.value,  # blocked：合并冲突/分支丢失
    },
    TaskStatus.FAILED.value: {
        TaskStatus.FAILED.value, TaskStatus.PENDING.value, TaskStatus.RUNNING.value,
        TaskStatus.SUCCESS.value, TaskStatus.BLOCKED.value, TaskStatus.CANCELLED.value,
        TaskStatus.SKIPPED.value, TaskStatus.WAITING.value,
    },
    TaskStatus.BLOCKED.value: {
        TaskStatus.BLOCKED.value, TaskStatus.PENDING.value, TaskStatus.RUNNING.value,
        TaskStatus.SUCCESS.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
        TaskStatus.SKIPPED.value, TaskStatus.WAITING.value,
    },
    TaskStatus.WAITING.value: {
        TaskStatus.WAITING.value, TaskStatus.PENDING.value, TaskStatus.RUNNING.value,
        TaskStatus.SUCCESS.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
        TaskStatus.SKIPPED.value, TaskStatus.BLOCKED.value,
    },
    TaskStatus.INTERRUPTED.value: {
        TaskStatus.INTERRUPTED.value, TaskStatus.PENDING.value, TaskStatus.RUNNING.value,
        TaskStatus.BLOCKED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value,
        TaskStatus.SKIPPED.value,
    },
    TaskStatus.CANCELLED.value: {TaskStatus.CANCELLED.value},   # 终态
    TaskStatus.SKIPPED.value: {
        TaskStatus.SKIPPED.value, TaskStatus.PENDING.value, TaskStatus.CANCELLED.value,
    },
}


class InvalidTransition(RuntimeError):
    """非法状态跃迁（例如把 success 重新置为 running、cancelled 又被 retry）。"""

    def __init__(self, task_id: str, previous: str, target: str):
        super().__init__(f"任务 {task_id} 不允许从 {previous} 跃迁到 {target}")
        self.task_id = task_id
        self.previous = previous
        self.target = target


def can_transition(previous: str, target: str) -> bool:
    if previous == target:
        return True
    allowed = ALLOWED_TRANSITIONS.get(previous)
    if allowed is None:  # 未知历史状态：不阻断，避免老库升级后不可操作
        return True
    return target in allowed



class TaskService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        project_id: str,
        title: str,
        *,
        kind: str | None = None,
        brief: str = "",
        template_id: str | None = None,
        agent_id: str | None = None,
        role: str | None = None,
        isolation: str | None = None,
        read_only: bool | None = None,
        write_scope: Iterable[str] | None = None,
        acceptance: list[dict[str, Any]] | None = None,
        acceptance_ref: str | None = None,
        inputs: list[Any] | None = None,
        outputs: list[Any] | None = None,
        depends_on: Iterable[str] | None = None,
        priority: int = 0,
        max_attempts: int | None = None,
        timeout_sec: int | None = None,
        tags: Iterable[str] | None = None,
        parent_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        template = self.config.template(template_id) if template_id else None
        resolved_kind = kind or (template or {}).get("kind") or "generic"
        kind_defaults = self.config.kind_defaults(resolved_kind)
        resolved_isolation = (
            isolation
            or (template or {}).get("isolation")
            or kind_defaults.get("isolation")
            or self.config.policy_get("isolation.default", "copy")
        )
        resolved_read_only = (
            read_only
            if read_only is not None
            else (template or {}).get("read_only", kind_defaults.get("read_only", False))
        )
        resolved_role = role or (template or {}).get("role") or "worker"
        resolved_ref = acceptance_ref or (template or {}).get("acceptance_ref") or kind_defaults.get("acceptance_ref")
        resolved_acceptance = acceptance
        if resolved_acceptance is None:
            resolved_acceptance = self.config.acceptance_rule(resolved_ref) if resolved_ref else []
        resolved_timeout = (
            timeout_sec if timeout_sec is not None else (template or {}).get("timeout_sec")
        )
        # 注意：这里**不能**回退到 policy.default_timeout_sec。
        # 需求 3 要求「每个 Agent 可独立配置超时」，而 agent 层的值只有在任务未显式指定时
        # 才生效；若创建任务时就把 policy 默认值写死进 task.timeout_sec，agent 配置永远轮不到。
        # 运行期由 runner.resolve_timeout(task → agent → policy) 统一解析，None = 继承。
        brief_text = brief or (template or {}).get("brief") or ""
        if max_attempts is None:
            max_attempts = int(task_kind_defaults(self.config, resolved_kind).get("max_attempts") or 3)
        else:
            max_attempts = max(1, int(max_attempts))

        task_id = new_id("task")
        now = now_iso()
        ordering = int(
            (self.db.query_one("SELECT COALESCE(MAX(ordering), 0) AS m FROM tasks WHERE project_id = ?", (project_id,)) or {"m": 0})["m"]
        ) + 1
        self.db.insert(
            "tasks",
            {
                "id": task_id,
                "project_id": project_id,
                "parent_id": parent_id,
                "title": title,
                "kind": resolved_kind,
                "brief": brief_text,
                "tags": dumps(list(tags or [])),
                "role": resolved_role,
                "template_id": template_id,
                "agent_id": agent_id,
                "status": TaskStatus.PENDING.value,
                "priority": priority,
                "attempt": 0,
                "max_attempts": max_attempts,
                "timeout_sec": resolved_timeout,
                "isolation": resolved_isolation,
                "read_only": 1 if resolved_read_only else 0,
                "write_scope": dumps(list(write_scope or [])),
                "acceptance": dumps(resolved_acceptance or []),
                "acceptance_ref": resolved_ref,
                "inputs": dumps(list(inputs or [])),
                "outputs": dumps(list(outputs or [])),
                "workdir": None,
                "blocked_reason": None,
                "last_error": None,
                "result": dumps(context or {}),
                "ordering": ordering,
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
            },
        )
        for dep in depends_on or []:
            self.add_edge(task_id, dep)
        return self.get(task_id)

    # ------------------------------------------------------------------ 依赖
    def add_edge(self, task_id: str, depends_on: str, mode: str = "success") -> None:
        """新增依赖边（task → depends_on），并在写入前做完整合法性校验。

        旧实现只写表、不校验：自依赖、跨项目依赖、环都会静默写进去，调度器随后
        永远等不到 ready 任务。这里把三类错误都挡在入口，并保持错误信息可操作。
        """
        if mode not in {"success", "soft", "any", "always"}:
            raise ValueError(f"不支持的依赖模式：{mode}")
        if task_id == depends_on:
            raise ValueError("任务不能依赖自身")
        task = self.get(task_id)
        dep = self.get(depends_on)
        if task["project_id"] != dep["project_id"]:
            raise ValueError("依赖任务必须属于同一项目")
        with self.db.transaction():
            # dep 是否已经（间接）依赖 task？是则 task→dep 会形成环。
            row = self.db.query_one(
                """
                WITH RECURSIVE reach(id) AS (
                    SELECT ?
                    UNION
                    SELECT e.depends_on FROM task_edges e JOIN reach r ON e.task_id = r.id
                )
                SELECT 1 AS hit FROM reach WHERE id = ? LIMIT 1
                """,
                (depends_on, task_id),
            )
            if row:
                raise ValueError(f"依赖会形成环：{task_id} → {depends_on}")
            self.db.execute(
                "INSERT OR REPLACE INTO task_edges (task_id, depends_on, mode) VALUES (?, ?, ?)",
                (task_id, depends_on, mode),
            )
            self.append_state(task_id, None, task["status"], f"新增依赖 {depends_on}")

    def remove_edge(self, task_id: str, depends_on: str) -> None:
        self.db.execute("DELETE FROM task_edges WHERE task_id = ? AND depends_on = ?", (task_id, depends_on))

    def delete(self, task_id: str) -> dict[str, Any]:
        """删除任务及其全部协作数据（人工清理用）。

        运行证据目录由调用方（API/CLI）通过 ``IsolationService.purge_task`` 删除；
        这里负责数据库内的关联行，避免留下孤儿产物/审批/黑板条目。
        """
        task = self.get(task_id)
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM task_edges WHERE task_id = ? OR depends_on = ?", (task_id, task_id)
            )
            self.db.execute("DELETE FROM task_states WHERE task_id = ?", (task_id,))
            self.db.execute(
                "DELETE FROM board_versions WHERE entry_id IN (SELECT id FROM board_entries WHERE task_id = ?)",
                (task_id,),
            )
            self.db.execute("DELETE FROM board_entries WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM artifacts WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM file_changes WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM handoffs WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM approvals WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM scheduler_slots WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM run_events WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM runs WHERE task_id = ?", (task_id,))
            self.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        return task

    def dependencies(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT e.depends_on AS id, e.mode, t.title, t.status
            FROM task_edges e LEFT JOIN tasks t ON t.id = e.depends_on
            WHERE e.task_id = ?
            """,
            (task_id,),
        )
        return [dict(r) for r in rows]

    def dependents(self, task_id: str) -> list[str]:
        rows = self.db.query("SELECT task_id FROM task_edges WHERE depends_on = ?", (task_id,))
        return [r["task_id"] for r in rows]

    def has_cycle(self, project_id: str) -> list[str] | None:
        tasks = {t["id"] for t in self.list(project_id)}
        edges = self.db.query("SELECT task_id, depends_on FROM task_edges")
        graph: dict[str, list[str]] = {tid: [] for tid in tasks}
        for edge in edges:
            if edge["task_id"] in graph and edge["depends_on"] in graph:
                graph[edge["task_id"]].append(edge["depends_on"])
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {tid: WHITE for tid in tasks}
        stack: list[str] = []

        def visit(node: str) -> list[str] | None:
            color[node] = GRAY
            stack.append(node)
            for nxt in graph.get(node, []):
                if color[nxt] == GRAY:
                    idx = stack.index(nxt)
                    return stack[idx:] + [nxt]
                if color[nxt] == WHITE:
                    found = visit(nxt)
                    if found:
                        return found
            stack.pop()
            color[node] = BLACK
            return None

        for tid in tasks:
            if color[tid] == WHITE:
                found = visit(tid)
                if found:
                    return found
        return None

    # ------------------------------------------------------------------ 查询
    def get(self, task_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not row:
            raise KeyError(f"任务不存在：{task_id}")
        return self._decorate(_task(row))

    def find(self, identifier: str, project_id: str | None = None) -> dict[str, Any] | None:
        if project_id:
            row = self.db.query_one(
                "SELECT * FROM tasks WHERE id = ? AND project_id = ?", (identifier, project_id)
            )
        else:
            row = self.db.query_one("SELECT * FROM tasks WHERE id = ?", (identifier,))
        return self._decorate(_task(row)) if row else None

    def list(
        self,
        project_id: str,
        *,
        status: str | None = None,
        status_in: Iterable[str] | None = None,
        kind: str | None = None,
        agent_id: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if status:
            clauses.append("status = ?")
            params.append(status)
        if status_in:
            values = list(status_in)
            clauses.append(f"status IN ({','.join('?' for _ in values)})")
            params.extend(values)
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if agent_id:
            clauses.append("agent_id = ?")
            params.append(agent_id)
        sql = f"SELECT * FROM tasks WHERE {' AND '.join(clauses)} ORDER BY priority DESC, ordering ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return [self._decorate(_task(r)) for r in self.db.query(sql, params)]

    def _decorate(self, task: dict[str, Any]) -> dict[str, Any]:
        """附加只读运行期视图（超时/已运行/剩余/告警），不写库。"""
        task["runtime"] = runtime_info(task, self.config)
        effective, warnings = effective_acceptance(task, self.config)
        task["acceptance_effective"] = effective
        task["acceptance_effective_warnings"] = warnings
        return task

    # ------------------------------------------------------------------ 更新
    def update(self, task_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "title", "kind", "brief", "tags", "role", "template_id", "agent_id", "priority",
            "max_attempts", "timeout_sec", "isolation", "read_only", "write_scope", "acceptance",
            "acceptance_ref", "inputs", "outputs", "workdir", "blocked_reason", "last_error",
            "result", "attempt", "started_at", "finished_at", "parent_id", "ordering",
        }
        values: dict[str, Any] = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in {"tags", "write_scope", "acceptance", "inputs", "outputs", "result"} and not isinstance(value, str):
                value = dumps(value)
            if key == "read_only":
                value = 1 if value else 0
            values[key] = value
        if not values:
            return self.get(task_id)
        values["updated_at"] = now_iso()
        self.db.update("tasks", {"id": task_id}, values)
        return self.get(task_id)

    def set_result_key(
        self,
        task_id: str,
        key: str,
        value: Any,
        *,
        run_id: str | None = None,
    ) -> bool:
        """原子更新 task.result 的单个键（保留其它并发写入）。

        审计线程、人工合并、Runner 收尾可能同时改 result 的不同键；整份
        「读→改→写」会互相覆盖。这里用 SQLite JSON1 的条件 UPDATE：
        - 只改 ``$.key``，不动 ``git``/``cost`` 等其它字段；
        - 传 ``run_id`` 时只在 result.run_id 仍匹配时更新（旧 run 不覆盖新 run）。
        返回是否真正更新了一行。
        """
        import re

        if not re.fullmatch(r"[A-Za-z0-9_]+", key):
            raise ValueError(f"非法 result 键：{key}")
        payload = dumps(value)
        sql = (
            "UPDATE tasks SET result = json_set("
            "CASE WHEN result IS NULL OR result = '' THEN '{}' ELSE result END,"
            " '$.' || ?, json(?)), updated_at = ? WHERE id = ?"
        )
        params: list[Any] = [key, payload, now_iso(), task_id]
        if run_id is not None:
            sql += " AND json_extract(result, '$.run_id') = ?"
            params.append(run_id)
        with self.db.transaction():
            cursor = self.db.execute(sql, params)
        return bool(cursor.rowcount)

    def set_status(self, task_id: str, status: str, reason: str | None = None, *, force: bool = False) -> dict[str, Any]:
        """更新任务状态。非法跃迁默认抛 :class:`InvalidTransition`（``force=True`` 可越过护栏）。"""
        task = self.get(task_id)
        previous = task["status"]
        if not force and not can_transition(previous, status):
            raise InvalidTransition(task_id, previous, status)
        patch: dict[str, Any] = {"status": status, "blocked_reason": reason if status == TaskStatus.BLOCKED.value else None}
        if status == TaskStatus.RUNNING.value and not task.get("started_at"):
            patch["started_at"] = now_iso()
        if status in FINAL_STATUSES:
            patch["finished_at"] = now_iso()
        else:
            # 重试/恢复/阻塞后任务不再处于终态；旧 finished_at 会让退避算法
            # 误以为「已经等了很久」，也会让 UI 显示「已结束但仍在排队」。
            patch["finished_at"] = None
        if status != TaskStatus.RUNNING.value:
            # 离开 running 就作废认领凭证：谁还想按旧凭证回写状态，谁就被拒绝
            patch["claim_id"] = None
            patch["claim_owner"] = None
            patch["claimed_at"] = None
        self.db.update("tasks", {"id": task_id}, {**patch, "updated_at": now_iso()})
        self.append_state(task_id, previous, status, reason)
        return self.get(task_id)

    def append_state(self, task_id: str, from_status: str | None, to_status: str, reason: str | None = None) -> None:
        self.db.insert(
            "task_states",
            {"task_id": task_id, "from_status": from_status, "to_status": to_status, "reason": reason, "at": now_iso()},
        )

    def history(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM task_states WHERE task_id = ? ORDER BY id", (task_id,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 认领（claim / lease）
    def claim(self, task_id: str, *, owner: str | None = None, allow: Iterable[str] | None = None) -> str | None:
        """原子认领一个任务用于执行。

        只有把状态从「可认领态」真正改成 ``running`` 的那一次调用会拿到 token；
        并发下同一任务最多一个赢家（条件 UPDATE + 事务，跨进程由 BEGIN IMMEDIATE 保证）。
        返回 ``None`` = 没抢到（已被别人认领，或状态不可执行）。
        """
        allowed = {str(s) for s in (allow if allow is not None else CLAIMABLE_STATUSES)}
        owner_id = owner or getattr(self.db, "session_id", "local")
        token = new_id("claim")
        now = now_iso()
        with self.db.transaction():
            row = self.db.query_one("SELECT status FROM tasks WHERE id = ?", (task_id,))
            if not row or row["status"] not in allowed:
                return None
            cursor = self.db.execute(
                """
                UPDATE tasks
                   SET status = ?, claim_id = ?, claim_owner = ?, claimed_at = ?, updated_at = ?,
                       started_at = COALESCE(started_at, ?)
                 WHERE id = ? AND status = ?
                """,
                (TaskStatus.RUNNING.value, token, owner_id, now, now, now, task_id, row["status"]),
            )
            if cursor.rowcount != 1:  # 状态在事务外被改动过（另一进程/线程）
                return None
            self.append_state(task_id, row["status"], TaskStatus.RUNNING.value, "认领执行")
        return token

    def holds_claim(self, task_id: str, token: str | None) -> bool:
        """凭证是否仍然有效（任务还在 running 且 claim_id 未被取消/接管清掉）。"""
        if not token:
            return False
        row = self.db.query_one("SELECT claim_id, status FROM tasks WHERE id = ?", (task_id,))
        return bool(row and row["claim_id"] == token and row["status"] == TaskStatus.RUNNING.value)

    def finish_claim(self, task_id: str, token: str | None, status: str, reason: str | None = None) -> bool:
        """持有凭证时落状态；凭证已失效（被 cancel/takeover/retry 抢走）则不回写，返回 False。"""
        if not token:
            return False
        with self.db.transaction():
            if not self.holds_claim(task_id, token):
                return False
            self.set_status(task_id, status, reason)
        return True

    # ------------------------------------------------------------------ 就绪
    def deps_satisfied(self, task: dict[str, Any]) -> tuple[bool, list[str]]:
        """依赖是否满足。

        边模式（``task_edges.mode``）：
        - ``success``（默认）：前置必须 success；
        - ``always`` / ``any``：不看前置状态，直接放行（前置失败也要跑，如「清理/收尾」任务）；
        - ``soft``：前置进入终态（success/failed/cancelled/skipped）即放行。
        未知模式按 ``success`` 处理（保守）。
        """
        pending: list[str] = []
        for dep in self.dependencies(task["id"]):
            status = dep.get("status")
            mode = dep.get("mode") or "success"
            if mode in ("any", "always"):
                continue
            if status != TaskStatus.SUCCESS.value:
                if mode == "soft" and status in FINAL_STATUSES:
                    continue
                pending.append(dep["id"])
                continue
            # 默认策略「先审后合」下，success 只代表任务分支已提交，并不代表
            # 产出已进入基线。下游若此时启动，会基于旧树执行并产出错误补丁。
            # 因此默认要求前置任务已合并（merge.merged=true）后下游才可派发；
            # 需要 DAG 连续交付的项目设置 git.auto_merge=true，或显式关闭本检查。
            if mode == "success" and self._dependency_unmerged(dep):
                pending.append(dep["id"])
        return (not pending), pending

    def _dependency_unmerged(self, dep: dict[str, Any]) -> bool:
        if not self.config.policy_bool("git.require_merged_for_dependents", True):
            return False
        row = self.db.query_one("SELECT result FROM tasks WHERE id = ?", (dep["id"],))
        if row is None:
            return False
        result = row["result"]
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except (TypeError, ValueError):
                return False
        if not isinstance(result, dict):
            return False
        git_meta = result.get("git")
        if not isinstance(git_meta, dict) or not git_meta:
            return False
        merge = git_meta.get("merge")
        if not isinstance(merge, dict):
            return True
        return not bool(merge.get("merged"))

    def ready_tasks(self, project_id: str) -> list[dict[str, Any]]:
        ready: list[dict[str, Any]] = []
        for task in self.list(project_id, status=TaskStatus.PENDING.value):
            ok, _ = self.deps_satisfied(task)
            if ok:
                ready.append(task)
        return ready

    def blocked_tasks(self, project_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for task in self.list(project_id, status=TaskStatus.PENDING.value):
            ok, pending = self.deps_satisfied(task)
            if not ok:
                task = dict(task)
                task["pending_deps"] = pending
                result.append(task)
        return result

    # ------------------------------------------------------------------ 计划
    def plan(self, project_id: str) -> dict[str, Any]:
        """返回可执行的层次编排（串行 / 并行）与依赖关系。"""
        tasks = self.list(project_id)
        by_id = {t["id"]: t for t in tasks}
        edges = [
            dict(r)
            for r in self.db.query(
                "SELECT e.task_id, e.depends_on, e.mode FROM task_edges e JOIN tasks t ON t.id = e.task_id WHERE t.project_id = ?",
                (project_id,),
            )
        ]
        remaining = set(by_id)
        levels: list[list[str]] = []
        while remaining:
            current = [
                tid
                for tid in remaining
                if all(e["depends_on"] not in remaining for e in edges if e["task_id"] == tid)
            ]
            if not current:
                levels.append(sorted(remaining))
                break
            levels.append(sorted(current))
            remaining -= set(current)
        return {
            "project_id": project_id,
            "levels": levels,
            "edges": edges,
            "nodes": [
                {
                    "id": t["id"],
                    "title": t["title"],
                    "status": t["status"],
                    "kind": t["kind"],
                    "agent_id": t["agent_id"],
                    "priority": t["priority"],
                }
                for t in tasks
            ],
        }

    def progress(self, project_id: str) -> dict[str, Any]:
        tasks = self.list(project_id)
        counters: dict[str, int] = {}
        for task in tasks:
            counters[task["status"]] = counters.get(task["status"], 0) + 1
        running = [t for t in tasks if t["status"] == TaskStatus.RUNNING.value]
        blocked = self.blocked_tasks(project_id)
        return {
            "project_id": project_id,
            "total": len(tasks),
            "counters": counters,
            "running": [
                {"id": t["id"], "title": t["title"], "agent_id": t["agent_id"]} for t in running
            ],
            "blocked": [
                {"id": t["id"], "title": t["title"], "pending_deps": t["pending_deps"]} for t in blocked
            ],
        }

    # ------------------------------------------------------------------ 产物
    def record_artifact(
        self,
        *,
        project_id: str,
        task_id: str,
        run_id: str | None,
        role: str,
        path: Path,
        root: Path,
        fragment: dict[str, Any] | None = None,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_id = new_id("art")
        rel = rel_posix(path, root)
        binary = is_binary_path(path)
        digest = None if binary else sha256_file(path)
        size = path.stat().st_size if path.exists() else 0
        self.db.insert(
            "artifacts",
            {
                "id": artifact_id,
                "project_id": project_id,
                "task_id": task_id,
                "run_id": run_id,
                "role": role,
                "rel_path": rel,
                "abs_path": str(path),
                "sha256": digest,
                "size": size,
                "is_binary": 1 if binary else 0,
                "fragment": dumps(fragment) if fragment else None,
                "record": dumps(record or {}),
                "created_at": now_iso(),
            },
        )
        return self.get_artifact(artifact_id)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        if not row:
            raise KeyError(f"产物不存在：{artifact_id}")
        return _artifact(row)

    def artifacts(self, *, task_id: str | None = None, project_id: str | None = None, role: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if role:
            clauses.append("role = ?")
            params.append(role)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.db.query(f"SELECT * FROM artifacts {where} ORDER BY created_at", params)
        return [_artifact(r) for r in rows]


def _task(row) -> dict[str, Any]:
    data = dict(row)
    for key in ("tags", "write_scope", "acceptance", "inputs", "outputs"):
        data[key] = loads(data.get(key), []) or []
    data["result"] = loads(data.get("result"), {}) or {}
    data["read_only"] = bool(data.get("read_only"))
    return data


def _artifact(row) -> dict[str, Any]:
    data = dict(row)
    data["record"] = loads(data.get("record"), {}) or {}
    data["fragment"] = loads(data.get("fragment"), None)
    data["is_binary"] = bool(data.get("is_binary"))
    return data
