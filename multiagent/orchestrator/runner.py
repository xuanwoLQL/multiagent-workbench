"""单任务执行器：隔离 → 注入上下文 → 调用 Agent → 审计改动 → 验收 → 交接。

关键纪律（需求 42）：**不采信 Agent 的自我声明**。任务成功的唯一依据是
进程退出码 + 产物存在性 + 验收条件。
"""

from __future__ import annotations

import shutil
import time
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..agents import AgentRequest, AgentResult, create_adapter
from ..agents.human_adapter import AWAITING_HUMAN
from ..agents.proc import CANCELLED_EXIT, cap_text_bytes
from ..agents.process_audit import ProcessAudit
from ..agents.resolve import resolve_agent_argv
from ..config import Config
from ..db import Database
from ..gitops import (
    branch_for_task,
    commit_worktree,
    current_branch,
    is_repo,
    merge_worktree_branch,
    remove_worktree,
)
from ..gitops.service import _git as _git_cmd
from ..logging_setup import bind_context, reset_context
from ..tasks.acceptance import evaluate_acceptance
from ..tasks.profiles import effective_acceptance, output_status
from ..tasks.runtime import resolve_timeout as resolve_task_timeout
from ..tasks.service import TaskStatus
from ..util import dumps, ensure_dir, new_id, now_iso, safe_join, truncate, write_json, write_text
from ..workspace.blobs import BlobStore
from ..workspace.changes import (
    ChangeJournal,
    PathEscapeError,
    copy_tree,
    diff_trees,
    resolve_workspace_path,
    snapshot_tree,
)
from ..workspace.isolation import IsolationService, WorkspaceDriftError
from ..workspace.hygiene import scan_workspace
from . import context as context_mod
from . import router as router_mod
from .cancellation import RunCancelRegistry

LOGGER = logging.getLogger("multiagent.runner")


class _RunHeartbeat(threading.Thread):
    """在 Agent 调用期间持续刷新 ``runs.heartbeat_at``。

    它与 ``last_activity_at`` 分工明确：
    - ``heartbeat_at``：执行进程还活着（本线程每 10s 刷新）；
    - ``last_activity_at``：Agent 真的产出了事件（runner 在事件回调里刷新）。

    这样 UI / 运维既能区分「进程卡死」和「模型长时间思考没有输出」，
    也不会因为一次 40 分钟的正常长任务被误判为失联。
    """

    def __init__(self, db: Database, run_id: str, interval: float = 10.0):
        super().__init__(name=f"run-heartbeat-{run_id}", daemon=True)
        self.db = db
        self.run_id = run_id
        self.interval = max(2.0, float(interval))
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.db.execute(
                    "UPDATE runs SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
                    (now_iso(), self.run_id),
                )
            except Exception:  # noqa: BLE001 - 心跳失败不能影响 Agent 调用
                pass

    def stop(self) -> None:
        self._stop.set()


@dataclass
class RunOutcome:
    task_id: str
    run_id: str
    agent_id: str
    status: str
    exit_code: int
    duration_ms: int
    changes: list[dict[str, Any]] = field(default_factory=list)
    acceptance_ok: bool = False
    acceptance: dict[str, Any] = field(default_factory=dict)
    handoff_id: str | None = None
    error: str | None = None
    awaiting: str | None = None
    conflicts: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    committed: list[str] = field(default_factory=list)
    timeout_sec: int = 0
    missing_outputs: list[str] = field(default_factory=list)
    hygiene: dict[str, Any] = field(default_factory=dict)


class TaskRunner:
    # 「不限时」在适配器边界上的表达值（适配器只认秒数，0 会被误解成立刻超时）
    UNLIMITED_TIMEOUT_SEC = 10 ** 9

    def __init__(
        self,
        db: Database,
        config: Config,
        *,
        tasks,
        projects,
        board,
        handoffs,
        guard,
        quota,
        isolation: IsolationService | None = None,
        event_sink=None,
    ):
        self.db = db
        self.config = config
        self.tasks = tasks
        self.projects = projects
        self.board = board
        self.handoffs = handoffs
        self.guard = guard
        self.quota = quota
        self.isolation = isolation or IsolationService(db, config)
        self.journal = ChangeJournal(db, BlobStore(config.data_dir))
        self.event_sink = event_sink
        self._adapters: dict[str, Any] = {}
        # 在跑 run 的取消信号注册表（需求 54）。Engine 装配时会用自己那份替换它，
        # 保证「取消/停止」能 set 到正在执行的这次调用的 event。
        self.run_cancels = RunCancelRegistry()
        # 异步进程审计线程登记表：CLI/桌面退出前用有界 join 等它们落盘，
        # 否则 daemon 线程会随进程退出被杀死，孤儿进程和审计结果都丢失。
        self._audit_threads: set[threading.Thread] = set()
        self._audit_lock = threading.Lock()

    # ------------------------------------------------------------------ Agent
    def adapter(self, agent_id: str):
        if agent_id not in self._adapters:
            agent_cfg = self.config.agent(agent_id)
            if not agent_cfg:
                raise KeyError(f"未配置的 Agent：{agent_id}")
            self._adapters[agent_id] = create_adapter(agent_cfg, resolve_agent_argv)
        return self._adapters[agent_id]

    def choose_agent(self, task: dict[str, Any]) -> tuple[str | None, str]:
        load = self._active_load(task["project_id"])
        remaining = {a["agent_id"]: (a["limits"].get("max_invocations") or 1e9) - a["usage"]["invocations"] for a in self.quota.summary()}

        def is_available(agent_id: str) -> bool:
            agent = self.config.agent(agent_id)
            if agent_id == "human":
                return True
            return bool(agent and agent.get("enabled", True))

        def is_allowed(agent_id: str) -> bool:
            if agent_id == "human":
                return True
            return self.quota.check(agent_id)[0]

        return router_mod.choose_agent(
            config=self.config, task=task, is_available=is_available, is_allowed=is_allowed, load=load, remaining=remaining
        )

    def _active_load(self, project_id: str) -> dict[str, int]:
        rows = self.db.query(
            "SELECT agent_id, COUNT(*) AS c FROM runs WHERE status = 'running' GROUP BY agent_id"
        )
        return {r["agent_id"]: int(r["c"]) for r in rows}

    # ------------------------------------------------------------------ 主流程
    def resolve_timeout(
        self,
        task: dict[str, Any],
        agent_id: str | None,
        attempt: int | None = None,
    ) -> int:
        """超时解析：``task``（显式指定）→ ``agent``（需求 3 的每 Agent 配置）→ ``policy`` 默认。

        ``<= 0`` 表示**不限时**（例如 human Agent 必须等人，需求 57）。
        """
        timeout = resolve_task_timeout(task, self.config, agent_id, attempt=attempt)
        # 需求 45：max_seconds 是**硬上限**，不能只在任务结束后才记账。
        # 如果某个 Agent 已用时长接近上限，本次调用的超时不得超过「剩余额度」，
        # 否则一次长任务就能把额度从 10% 直接跑穿到 300%。
        if agent_id and agent_id != "human":
            limits = self.quota.limits(agent_id)
            max_seconds = limits.get("max_seconds")
            if max_seconds:
                used = float(self.quota.usage(agent_id).get("seconds") or 0.0)
                remaining = max(0, int(float(max_seconds) - used))
                if remaining <= 0:
                    return 1  # 已无剩余额度：极短超时让适配器立即失败并走额度阻塞
                if timeout <= 0 or timeout > remaining:
                    timeout = remaining
        return timeout

    def run(
        self,
        task_id: str,
        *,
        agent_id: str | None = None,
        attempt: int | None = None,
        claim_token: str | None = None,
    ) -> RunOutcome:
        task = self.tasks.get(task_id)
        project = self.projects.get(task["project_id"])

        if task["status"] in (TaskStatus.CANCELLED.value, TaskStatus.SKIPPED.value):
            return RunOutcome(task_id, "", task.get("agent_id") or "", task["status"], -1, 0, error="任务已取消/跳过")

        resolved_agent, reason = (agent_id, "调用方指定") if agent_id else self.choose_agent(task)
        if not resolved_agent:
            self.tasks.set_status(task_id, TaskStatus.BLOCKED.value, reason)
            self._event(task, "task.blocked", f"无法派发：{reason}")
            explicit = task.get("agent_id")
            if explicit and self.config.agent(explicit):
                allowed, why = self.quota.check(explicit)
                if not allowed:
                    self.guard.request(
                        project_id=project["id"],
                        kind="quota",
                        title=f"Agent {explicit} 额度超限",
                        detail={"reason": why},
                        task_id=task_id,
                        agent_id=explicit,
                        risk="medium",
                        board=self.board,
                    )
            return RunOutcome(task_id, "", "", TaskStatus.BLOCKED.value, -1, 0, error=reason)

        self.tasks.update(task_id, agent_id=resolved_agent)
        task = self.tasks.get(task_id)

        if self.config.agent(resolved_agent) and (self.config.agent(resolved_agent) or {}).get("protocol") == "human":
            return self._await_human(task, project)

        allowed, why = (True, "") if resolved_agent == "human" else self.quota.check(resolved_agent)
        if not allowed:
            self.tasks.set_status(task_id, TaskStatus.BLOCKED.value, f"额度超限：{why}")
            self._event(task, "quota.blocked", f"{resolved_agent} 超限：{why}")
            self.guard.request(
                project_id=project["id"],
                kind="quota",
                title=f"Agent {resolved_agent} 额度超限",
                detail={"reason": why},
                task_id=task_id,
                agent_id=resolved_agent,
                risk="medium",
                board=self.board,
            )
            return RunOutcome(task_id, "", resolved_agent, TaskStatus.BLOCKED.value, -1, 0, error=why)

        # ---- 原子认领（claim）：并发下同一任务只有一个执行者 ----
        token = claim_token
        if token is None:
            token = self.tasks.claim(task_id)
            if token is None:
                current = self.tasks.get(task_id)
                return RunOutcome(
                    task_id, "", current.get("agent_id") or "", current["status"], -1, 0,
                    error=f"任务已被其他执行者认领或状态不可执行（当前 {current['status']}）",
                )
        elif not self.tasks.holds_claim(task_id, token):
            current = self.tasks.get(task_id)
            return RunOutcome(
                task_id, "", current.get("agent_id") or "", current["status"], -1, 0,
                error="认领凭证已失效（任务被取消/接管或已完成）",
            )
        task = self.tasks.get(task_id)

        attempt = attempt if attempt is not None else int(task["attempt"]) + 1
        self.tasks.update(task_id, attempt=attempt)
        planned_timeout = self.resolve_timeout(task, resolved_agent, attempt=attempt)
        # 新一次尝试开始：旧 run 的越权/验收失败/冲突审批自动作废，避免队列越积越多。
        try:
            self.guard.resolve_stale(task_id, board=self.board)
        except Exception:  # noqa: BLE001 - 审批清理失败不能阻断真正执行
            LOGGER.exception("清理历史审批失败 task=%s", task_id)
        run_id = new_id("run")
        task_dir = self.isolation.task_dir(project["id"], task_id)
        run_dir = ensure_dir(task_dir / "runs" / run_id)

        # 续跑/重试前先把上次尝试的工作副本归档，避免现场被静默清空
        archived = self._archive_previous_attempt(task, project)

        # 隔离工作副本：prepare_ex 同时给出「请求模式 / 实际模式 / 降级原因」。
        # 降级（dirty workspace、非 git、worktree 创建失败）必须可见，不能只写日志。
        workdir, isolation_info = self.isolation.prepare_ex(project, task)
        workdir = Path(workdir)
        if isolation_info.get("degraded"):
            self._event(
                task,
                "task.isolation.degraded",
                f"隔离模式降级为 {isolation_info.get('mode')}："
                f"{isolation_info.get('degrade_reason') or '未知原因'}",
                run_id=run_id,
                data=isolation_info,
            )
        baseline_dir = self.isolation.baseline_dir(project["id"], task_id)
        # 重试/续跑时，把上一次还在写范围内的半成品恢复进新副本。
        # 只恢复 write_scope 内、非保护路径的文件；越权内容一律不带过来。
        resume_info = (
            self._restore_partial_attempt(task, project, Path(archived), workdir, baseline_dir)
            if archived
            else None
        )
        handoff_path = workdir / ".multiagent" / "handoff.json"
        try:
            if handoff_path.exists():
                handoff_path.unlink()
        except OSError:
            pass
        ensure_dir(handoff_path.parent)

        # 上下文注入
        variables = self._variables(task, project, workdir)
        packet = context_mod.build_context(
            config=self.config,
            project=project,
            task=task,
            board=self.board,
            predecessor_handoffs=self._predecessor_handoffs(task),
            variables=variables,
            resume_info=resume_info,
        )
        prompt = context_mod.render_task_prompt(packet, handoff_path=str(handoff_path))
        write_text(run_dir / "context.json", dumps(packet))
        write_text(run_dir / "prompt.txt", prompt)

        baseline = self.isolation.baseline(baseline_dir)

        now = now_iso()
        self.db.insert(
            "runs",
            {
                "id": run_id,
                "project_id": project["id"],
                "task_id": task_id,
                "agent_id": resolved_agent,
                "agent_label": (self.config.agent(resolved_agent) or {}).get("label", resolved_agent),
                "protocol": (self.config.agent(resolved_agent) or {}).get("protocol", "cli"),
                "role": task.get("role"),
                "attempt": attempt,
                "timeout_sec": planned_timeout,
                "status": "running",
                "exit_code": None,
                "started_at": now,
                "finished_at": None,
                "duration_ms": 0,
                "cost_tokens": 0,
                "cost_amount": 0.0,
                "command": None,
                "cwd": str(workdir),
                "prompt_path": str(run_dir / "prompt.txt"),
                "stdout_path": str(run_dir / "stdout.txt"),
                "stderr_path": str(run_dir / "stderr.txt"),
                "transcript_path": str(run_dir / "transcript.json"),
                "handoff_path": str(handoff_path),
                "error": None,
                "owner_session": self.db.session_id,
                "heartbeat_at": now,
                "last_activity_at": now,
            },
        )
        self.db.runtime.mark_active(run_id)
        run_heartbeat = _RunHeartbeat(
            self.db,
            run_id,
            interval=float(self.config.policy_get("recover.run_heartbeat_seconds", 10) or 10),
        )
        run_heartbeat.start()
        # 需求 54：本次调用的取消信号。注册后 Engine.cancel/stop_project 才能 set 到它，
        # 适配器据此整树回收子进程；务必在 finally 里注销，避免注册表泄漏。
        cancel_path = run_dir / "cancel.requested"
        try:
            if cancel_path.exists():
                cancel_path.unlink()
        except OSError:
            pass
        cancel_event = self.run_cancels.register(run_id)
        log_token = bind_context(
            project_id=project["id"],
            task_id=task_id,
            run_id=run_id,
            agent_id=resolved_agent,
        )
        LOGGER.info("run.start", extra={"status": "running"})
        # 在 finally 里统一放行异步进程审计（即使 _finalize 抛异常也不能让它干等）。
        audit_done: threading.Event | None = None
        try:
            self.tasks.set_status(task_id, TaskStatus.RUNNING.value)
            self.tasks.update(task_id, workdir=str(workdir))
            self._event(task, "task.running", f"派发给 {resolved_agent}（第 {attempt} 次）", run_id=run_id)
            if resume_info:
                self._event(
                    task,
                    "task.resume.partial",
                    f"已恢复上一次尝试的 {resume_info['restored']} 个文件"
                    + (f"，跳过 {resume_info['skipped']} 个越权/保护路径" if resume_info.get("skipped") else ""),
                    run_id=run_id,
                    data=resume_info,
                )

            # 原子额度准入：check-then-act 在并发下会超发（已有回归测试证明）
            reserved = False
            if resolved_agent != "human":
                reserved, reserve_reason = self.quota.reserve(resolved_agent)
                if not reserved:
                    self.db.update(
                        "runs", {"id": run_id},
                        {"status": "blocked", "finished_at": now_iso(), "duration_ms": 0, "error": reserve_reason},
                    )
                    self.tasks.finish_claim(task_id, token, TaskStatus.BLOCKED.value, f"额度超限：{reserve_reason}")
                    self._event(task, "quota.blocked", f"{resolved_agent} 超限：{reserve_reason}", run_id=run_id)
                    self.guard.request(
                        project_id=project["id"],
                        kind="quota",
                        title=f"Agent {resolved_agent} 额度超限",
                        detail={"reason": reserve_reason},
                        task_id=task_id,
                        agent_id=resolved_agent,
                        risk="medium",
                        board=self.board,
                    )
                    return RunOutcome(task_id, run_id, resolved_agent, TaskStatus.BLOCKED.value, -1, 0, error=reserve_reason)

            try:
                adapter = self.adapter(resolved_agent)
            except Exception:
                if reserved:
                    self.quota.release(resolved_agent, invocations=1)
                raise

            seen_dangerous: set[str] = set()
            critical_hit = {"value": False}
            last_activity = {"at": 0.0}

            def _guarded_event(event: dict[str, Any]) -> None:
                """工具调用事件 → 危险命令检测 → 实时事件总线。"""
                now_monotonic = time.monotonic()
                if now_monotonic - last_activity["at"] >= 2.0:
                    last_activity["at"] = now_monotonic
                    try:
                        self.db.execute(
                            "UPDATE runs SET last_activity_at = ? WHERE id = ?",
                            (now_iso(), run_id),
                        )
                    except Exception:  # noqa: BLE001 - 活跃时间写失败不能拖垮事件流
                        pass
                try:
                    verdict = self.guard.inspect_agent_event(event, workspace=workdir)
                except Exception:  # noqa: BLE001 - 检测失败不能拖垮事件流
                    verdict = {"dangerous": False}
                if verdict.get("dangerous"):
                    command = str(verdict.get("command") or "")
                    severity = str(verdict.get("severity") or "destructive")
                    key = f"{severity}:{verdict.get('reason')}:{command[:200]}"
                    if severity == "critical":
                        if not critical_hit["value"]:
                            critical_hit["value"] = True
                            cancel_event.set()
                            self.guard.request(
                                project_id=project["id"],
                                kind="dangerous_op",
                                title="高危系统命令被立即终止",
                                detail={
                                    "reason": verdict.get("reason"),
                                    "command": command[:4000],
                                    "agent_id": resolved_agent,
                                    "run_id": run_id,
                                },
                                task_id=task_id,
                                run_id=run_id,
                                agent_id=resolved_agent,
                                risk="high",
                                board=self.board,
                            )
                            self._event(
                                task,
                                "task.dangerous_command",
                                f"检测到高危系统命令，已终止本次调用：{verdict.get('reason')}",
                                run_id=run_id,
                                data={"command": command[:2000], "reason": verdict.get("reason")},
                            )
                    elif key not in seen_dangerous:
                        seen_dangerous.add(key)
                        self.guard.request(
                            project_id=project["id"],
                            kind="dangerous_op",
                            title="检测到破坏性命令，需人工确认",
                            detail={
                                "reason": verdict.get("reason"),
                                "command": command[:4000],
                                "agent_id": resolved_agent,
                                "run_id": run_id,
                            },
                            task_id=task_id,
                            run_id=run_id,
                            agent_id=resolved_agent,
                            risk="medium",
                            board=self.board,
                        )
                self._emit_agent_event(task, resolved_agent, event, run_id=run_id)

            timeout_sec = planned_timeout
            max_output_bytes = int(self.config.policy_get("limits.max_output_bytes", 32 * 1024 * 1024) or 0)
            request = AgentRequest(
                prompt=prompt,
                workspace=workdir,
                run_dir=run_dir,
                # <=0 = 不限时：用一个足够大的值表达（适配器不能把 0 当成「立刻超时」）
                timeout_sec=timeout_sec if timeout_sec > 0 else self.UNLIMITED_TIMEOUT_SEC,
                model=(self.config.agent(resolved_agent) or {}).get("model"),
                permission="readonly" if task.get("read_only") else "guarded",
                allow_write=not task.get("read_only"),
                handoff_path=handoff_path,
                task_id=task_id,
                run_id=run_id,
                cancel_event=cancel_event,
                cancel_path=cancel_path,
                max_output_bytes=max_output_bytes,
                approve=self.guard.permission_policy(task),
                on_event=_guarded_event,
            )
            # 进程/端口审计（P1-7）：Agent 可能在 workdir 里拉起 dev server 并留下
            # 孤儿监听进程。审计只认「新增 + 能证明归属 workdir」的进程，其余只告警。
            #
            # finish() 已异步化，元数据查询不会阻塞任务返回；begin() 只跑 netstat
            # （约 20–50ms）。默认 min_timeout_sec=0 表示短任务也审计；把它设为
            # 正数可作为显式逃生阀（例如极端硬超时场景）。
            process_audit = None
            audit_enabled = self.config.policy_bool("processes.audit_enabled", True)
            min_audit_timeout = int(self.config.policy_get("processes.min_timeout_sec", 0) or 0)
            audit_applicable = (
                planned_timeout <= 0 or planned_timeout >= min_audit_timeout
            )
            if audit_enabled and audit_applicable:
                try:
                    process_audit = ProcessAudit.begin()
                except Exception:  # noqa: BLE001 - 审计不能拖垮任务
                    LOGGER.warning("启动进程审计失败（已忽略）", exc_info=True)
            invocation_started = time.monotonic()
            try:
                result = adapter.run(request)
            except Exception as exc:  # noqa: BLE001 - 适配器异常必须转成结构化失败，不能让任务卡在 running
                LOGGER.exception("Agent 适配器异常：%s", resolved_agent)
                result = AgentResult(
                    ok=False,
                    exit_code=-3,
                    duration_ms=int((time.monotonic() - invocation_started) * 1000),
                    stdout="",
                    stderr=f"{type(exc).__name__}: {exc}",
                    error=f"Agent 适配器异常：{type(exc).__name__}: {exc}",
                    transcript=[{"type": "adapter_error", "error": f"{type(exc).__name__}: {exc}"}],
                )
            wall_ms = int((time.monotonic() - invocation_started) * 1000)
            result = _trusted_duration(result, wall_ms)
            if max_output_bytes > 0:
                result.stdout = cap_text_bytes(result.stdout or "", max_output_bytes)
                result.stderr = cap_text_bytes(result.stderr or "", max_output_bytes)

            # 进程审计改为**异步收尾**：finish() 在新增监听进程时需要 ~2s 查询
            # Windows 进程元数据；同步等待会把取消/超时的硬上限拖长（实测取消
            # 1.99s > 1.5s 断言）。审计线程在任务收尾落库后再把结果合并进
            # task.result.processes，因此不阻塞 run_task 返回。
            if process_audit is not None:
                audit_done = threading.Event()
                # shared 模式下 workdir 就是项目根目录：用户自己的 dev server
                # 也满足「command 含 workdir」，默认不自动回收，只记录。
                shared_workspace = str((isolation_info or {}).get("mode") or "").lower() == "shared"
                audit_reap = self.config.policy_bool(
                    "processes.reap_orphans", True
                ) and not shared_workspace
                audit_cim = self.config.policy_bool("processes.cim_fallback", True)

                def _audit_processes(
                    audit=process_audit,
                    target_workdir=Path(workdir),
                    target_task_id=task_id,
                    target_run_id=run_id,
                    done=audit_done,
                    reap=audit_reap,
                    include_cim=audit_cim,
                ) -> None:
                    try:
                        try:
                            report = audit.finish(
                                target_workdir, reap=reap, include_cim=include_cim
                            )
                        except Exception:  # noqa: BLE001 - finish 本身承诺不抛，这里再兜底
                            LOGGER.warning("进程审计收尾失败（已忽略）", exc_info=True)
                            return
                        # 等 _finalize 写完 result 再合并，避免被后续 update 覆盖。
                        done.wait(timeout=120)
                        # 只写 processes 键：与人工 merge 的 result.git 并发安全。
                        applied = self.tasks.set_result_key(
                            target_task_id,
                            "processes",
                            report,
                            run_id=target_run_id,
                        )
                        if not applied:
                            return
                        try:
                            current = self.tasks.get(target_task_id)
                        except Exception:  # noqa: BLE001 - 任务可能已被删除
                            return
                        started = list(report.get("started") or [])
                        reaped = list(report.get("reaped") or [])
                        if started or reaped:
                            self._event(
                                current,
                                "task.processes",
                                f"进程审计：新增 {len(started)} 个监听进程，回收 {len(reaped)} 个",
                                run_id=target_run_id,
                                data=report,
                            )
                    except Exception:  # noqa: BLE001 - 审计线程绝不能把异常抛到宿主
                        LOGGER.warning("进程审计异步收尾异常（已忽略）", exc_info=True)
                    finally:
                        with self._audit_lock:
                            self._audit_threads.discard(threading.current_thread())

                worker = threading.Thread(
                    target=_audit_processes,
                    name=f"process-audit-{run_id}",
                    daemon=True,
                )
                try:
                    with self._audit_lock:
                        self._audit_threads.add(worker)
                    worker.start()
                except Exception:  # noqa: BLE001 - 审计是旁路，不能把成功任务拖成失败
                    with self._audit_lock:
                        self._audit_threads.discard(worker)
                    LOGGER.warning("启动进程审计线程失败（已忽略）", exc_info=True)
                    audit_done.set()

            self.db.update("runs", {"id": run_id}, {"duration_ms": result.duration_ms})
            write_text(run_dir / "stdout.txt", result.stdout or "")
            write_text(run_dir / "stderr.txt", result.stderr or "")
            write_json(run_dir / "transcript.json", result.transcript or [])

            if result.exit_code == AWAITING_HUMAN and not cancel_event.is_set():
                self.db.update(
                    "runs", {"id": run_id},
                    {"status": "waiting", "finished_at": now_iso(), "duration_ms": result.duration_ms},
                )
                if reserved:
                    self.quota.record(
                        resolved_agent, seconds=result.duration_ms / 1000.0,
                        cost=result.cost_amount, tokens=result.cost_tokens, invocations=0,
                    )
                return self._await_human(task, project, run_id=run_id)

            return self._finalize(
                task=task,
                project=project,
                run_id=run_id,
                agent_id=resolved_agent,
                result=result,
                workdir=workdir,
                baseline=baseline,
                baseline_dir=baseline_dir,
                variables=variables,
                run_dir=run_dir,
                claim_token=token,
                quota_reserved=reserved,
                cancel_event=cancel_event,
                timeout_sec=planned_timeout,
                isolation_info=isolation_info,
            )
        except Exception as exc:  # noqa: BLE001 - 兜底：任何未预期异常都要把任务从 running 收尾
            LOGGER.exception("run.crashed task=%s", task_id)
            try:
                self.tasks.finish_claim(task_id, token, TaskStatus.FAILED.value, f"执行器异常：{exc}")
            except Exception:  # noqa: BLE001
                pass
            try:
                self.db.update(
                    "runs",
                    {"id": run_id},
                    {
                        "status": "failed",
                        "exit_code": -3,
                        "finished_at": now_iso(),
                        "error": f"执行器异常：{type(exc).__name__}: {exc}",
                    },
                )
            except Exception:  # noqa: BLE001
                pass
            return RunOutcome(
                task_id,
                run_id,
                resolved_agent,
                TaskStatus.FAILED.value,
                -3,
                0,
                error=f"执行器异常：{type(exc).__name__}: {exc}",
            )
        finally:
            # 放行异步进程审计：此时本次 run 的 result 已落库（或已失败收尾），
            # 审计线程可以安全地把 processes 合并进去。
            if audit_done is not None:
                audit_done.set()
            LOGGER.info("run.finish")
            reset_context(log_token)
            run_heartbeat.stop()
            self.db.runtime.mark_done(run_id)
            self.run_cancels.unregister(run_id)

    def _archive_previous_attempt(self, task: dict[str, Any], project: dict[str, Any]) -> str | None:
        """把上一次尝试的工作副本复制到 ``runs/<run_id>/workspace``（现场留档）。

        没有它，续跑/重试/扇出时 ``isolation.prepare()`` 会 rmtree 掉工作副本，
        Agent 写下的半成品会被静默清空，产物行也会指向已经不存在的路径，
        事后既无法审计「做到哪一步」，也无法做需求 50 的产出对比。
        """
        row = self.db.query_one(
            """
            SELECT id, cwd FROM runs
             WHERE task_id = ? AND cwd IS NOT NULL
             ORDER BY started_at DESC, rowid DESC LIMIT 1
            """,
            (task["id"],),
        )
        if not row or not row["cwd"]:
            return None
        source = Path(row["cwd"])
        if not source.is_dir():
            return None
        task_dir = self.isolation.task_dir(project["id"], task["id"])
        # 只归档「任务自己的隔离副本」：shared 模式下 cwd 就是工作区原目录，
        # 把整个项目再拷一份既荒唐又危险。
        try:
            source.resolve().relative_to(task_dir.resolve())
        except ValueError:
            return None
        target = task_dir / "runs" / row["id"] / "workspace"
        if target.exists():
            # 旧版本已经归档过：直接复用，不再重复拷贝整个工作区。
            return str(target)
        try:
            ensure_dir(target)
            copy_tree(source, target, config=self.config)
        except (OSError, shutil.Error):
            return None
        # 产物行原本指向会被下一次 prepare() 清掉的 work/，改指向归档副本，证据才读得到
        for artifact in self.db.query("SELECT id, abs_path FROM artifacts WHERE run_id = ?", (row["id"],)):
            try:
                rel = Path(artifact["abs_path"]).relative_to(source)
            except (ValueError, TypeError):
                continue
            archived = target / rel
            if archived.exists():
                self.db.update("artifacts", {"id": artifact["id"]}, {"abs_path": str(archived)})
        self._event(task, "task.workspace_archived", f"上次尝试的工作副本已归档：{row['id']}", run_id=row["id"])
        return str(target)

    def _restore_partial_attempt(
        self,
        task: dict[str, Any],
        project: dict[str, Any],
        archive: Path,
        workdir: Path,
        baseline_dir: Path,
    ) -> dict[str, Any] | None:
        """把上一次失败/中断尝试的半成品恢复到新工作副本。

        为什么需要它：长任务（30–40 分钟）在超时/进程崩溃后若从零重来，既浪费额度，
        也永远无法在有限重试次数内收敛。这里以「上次工作副本 vs 原始基线」的差异为输入，
        **只恢复 write_scope 内、非保护路径、非路径逃逸**的改动；越权内容和保护路径
        （``.multiagent/config/.git``）一律丢弃，避免把上一轮的安全问题带进下一轮。

        恢复后 Agent 看到的仍然是「当前任务工作目录」，但里面已经有上一轮的半成品；
        上下文里会明确提示「先核对、在此基础上继续，不要推倒重来」。
        """
        if not bool(self.config.policy_get("isolation.resume_partial_on_retry", True)):
            return None
        if int(task.get("attempt") or 0) <= 0 or bool(task.get("read_only")):
            return None
        if not archive.is_dir() or not baseline_dir.is_dir():
            return None
        try:
            before = snapshot_tree(baseline_dir, config=self.config)
            after = snapshot_tree(archive, config=self.config)
            changes = diff_trees(before, after)
        except OSError:
            return None
        if not changes:
            return None

        scope = task.get("write_scope") or []
        deny = self.guard.protected_paths()
        restored: list[str] = []
        skipped = 0
        for change in changes:
            rel = str(change["rel_path"])
            if not self.journal.scope_check(
                rel, scope, False, deny, workspace_root=workdir
            ):
                skipped += 1
                continue
            try:
                target = resolve_workspace_path(workdir, rel)
            except (PathEscapeError, OSError):
                skipped += 1
                continue
            if change["action"] == "delete":
                try:
                    if target.is_file():
                        target.unlink()
                    restored.append(rel)
                except OSError:
                    skipped += 1
                continue
            source = archive / rel
            if not source.is_file() or source.is_symlink():
                skipped += 1
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                restored.append(rel)
            except OSError:
                skipped += 1

        if not restored and not skipped:
            return None
        LOGGER.info(
            "resume.partial task=%s restored=%s skipped=%s source=%s",
            task["id"], len(restored), skipped, archive.parent.name,
        )
        return {
            "restored": len(restored),
            "skipped": skipped,
            "files": sorted(set(restored)),
            "source_run_id": archive.parent.name,
        }

    # ------------------------------------------------------------------ 收尾
    def wait_for_process_audits(self, timeout: float = 10.0) -> bool:
        """有界等待异步进程审计完成（CLI/桌面退出前调用）。

        daemon 线程在进程退出时会被直接杀死；给它们一个有限窗口把 processes
        写回 DB 并回收孤儿监听进程，超时则放弃，绝不阻塞正常退出。
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._audit_lock:
            pending = list(self._audit_threads)
        for thread in pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                thread.join(remaining)
            except RuntimeError:
                break
        with self._audit_lock:
            alive = [thread for thread in self._audit_threads if thread.is_alive()]
        return not alive

    def _finalize(self, *, task, project, run_id, agent_id, result, workdir, baseline, baseline_dir, variables, run_dir,
                  claim_token: str | None = None, quota_reserved: bool = False, cancel_event=None,
                  timeout_sec: int = 0, isolation_info: dict[str, Any] | None = None) -> RunOutcome:
        after = self.isolation.current(workdir)
        changes = self.isolation.changes(baseline, after)
        outputs = output_status(Path(workdir), task.get("outputs") or [])
        generated_outputs = [item["path"] for item in outputs if item.get("exists")]
        missing_outputs = [item["path"] for item in outputs if not item.get("exists")]
        hygiene = scan_workspace(
            project["root_path"],
            config=self.config,
            max_entries=int(self.config.policy_get("workspace.hygiene_max_entries", 20000) or 20000),
        )
        if hygiene.get("temporary_files"):
            self._event(
                task,
                "workspace.hygiene",
                f"发现 {hygiene.get('temporary_files')} 个临时产物："
                + ", ".join(item["path"] for item in hygiene.get("temporary", [])[:5]),
                run_id=run_id,
                data=hygiene,
            )
        # 认领凭证在收尾前被抢走（cancel / takeover / retry）→ 不再写状态、不回收改动
        claim_ok = self.tasks.holds_claim(task["id"], claim_token)
        # 需求 54：被取消的这次调用，无论 Agent 报什么结果，终态都是 cancelled；
        # 半成品留在隔离副本/run 目录里（需求 43），绝不 commit_back 进工作区。
        cancelled = bool(cancel_event is not None and cancel_event.is_set()) or result.exit_code == CANCELLED_EXIT

        scope = task.get("write_scope") or []
        read_only = bool(task.get("read_only"))
        recorded = self.journal.record(
            project_id=project["id"],
            task_id=task["id"],
            run_id=run_id,
            agent_id=agent_id,
            changes=changes,
            scope_patterns=scope,
            read_only=read_only,
            workspace_root=Path(project["root_path"]),
            work_root=Path(baseline_dir),
            after_root=Path(workdir),
            deny_patterns=self.guard.protected_paths(),
        )
        audit = self.guard.audit_changes(project=project, task=task, changes=changes, recorded=recorded)

        # 越权改动：还原成运行前内容 + 记待人工确认
        if audit["out_of_scope"] and not read_only and bool(self.config.policy_get("isolation.revert_out_of_scope", True)):
            # 需求 43：还原前先留证——越权产物可能是 Agent 的真实劳动成果，
            # 人工复核/调整写范围后仍可取回；不能因为「越权」就把现场物理删除。
            self._archive_paths(workdir, run_dir, audit["out_of_scope"], "out-of-scope")
            self.isolation.revert_out_of_scope(project, workdir, audit["out_of_scope"], baseline_dir)
            self.journal.mark_reverted(task["id"], audit["out_of_scope"], run_id=run_id)
            changes = [c for c in changes if c["rel_path"] not in set(audit["out_of_scope"])]

        for rel in audit["out_of_scope"]:
            self.guard.request(
                project_id=project["id"],
                kind="dangerous_op",
                title=f"越权写入被拦截：{rel}",
                detail={"rel_path": rel, "run_id": run_id, "reason": "不在任务可写范围内"},
                task_id=task["id"],
                run_id=run_id,
                agent_id=agent_id,
                risk="high",
                board=self.board,
            )
        for rel in audit["protected"]:
            self.guard.request(
                project_id=project["id"],
                kind="dangerous_op",
                title=f"改动命中保护路径：{rel}",
                detail={"rel_path": rel, "run_id": run_id},
                task_id=task["id"],
                run_id=run_id,
                agent_id=agent_id,
                risk="high",
                board=self.board,
            )
        for rel in audit["destructive"]:
            self.guard.request(
                project_id=project["id"],
                kind="dangerous_op",
                title=f"删除文件：{rel}",
                detail={"rel_path": rel, "run_id": run_id},
                task_id=task["id"],
                run_id=run_id,
                agent_id=agent_id,
                risk="high",
                board=self.board,
            )
        conflicts = audit["conflicts"]
        if conflicts:
            self.guard.request(
                project_id=project["id"],
                kind="conflict",
                title=f"检测到并发改动冲突：{', '.join(conflicts[:5])}",
                detail={"files": conflicts, "run_id": run_id},
                task_id=task["id"],
                run_id=run_id,
                agent_id=agent_id,
                risk="medium",
                board=self.board,
            )

        # 交接说明
        handoff_data = self.handoffs.read_handoff_file(workdir)
        if not handoff_data:
            handoff_data = _handoff_from_result(result)
        if hygiene.get("temporary_files"):
            issues = list(handoff_data.get("open_issues") or [])
            issues.append(
                "工作区卫生检查发现临时产物，交付前需要清理："
                + ", ".join(item["path"] for item in hygiene.get("temporary", [])[:10])
            )
            handoff_data["open_issues"] = issues
        if audit["out_of_scope"]:
            # 交接必须反映**最终工作区状态**：Agent 可能声称修了某个文件，
            # 但该文件不在 write_scope 内、已在收尾时被还原。下游任务如果照单全收，
            # 会以为修复已生效（真实事故：tests 任务修了 web/markdown.js，被还原后
            # node --test 仍然失败，但 handoff 仍写着「已修复」）。
            note = (
                f"系统提示：本次有 {len(audit['out_of_scope'])} 个改动因超出任务 write_scope "
                f"被还原，未进入工作区：{', '.join(audit['out_of_scope'][:10])}。"
                "如果这些修复是必要的，请由具备相应写范围的任务重做。"
            )
            handoff_data["notes"] = (
                (str(handoff_data.get("notes") or "").rstrip() + "\n\n" + note).strip()
            )
            issues = list(handoff_data.get("open_issues") or [])
            issues.append(
                "以下改动超出 write_scope 已被系统还原，需要在后续任务中重新处理："
                + ", ".join(audit["out_of_scope"][:10])
            )
            handoff_data["open_issues"] = issues
        handoff = self.handoffs.create(
            project_id=project["id"],
            task_id=task["id"],
            run_id=run_id,
            agent_id=agent_id,
            data=handoff_data,
            source="agent",
        )

        # 回收改动（隔离副本 → 原工作区）
        block_conflict = bool(self.config.policy_get("isolation.block_conflict", True))
        sync_back = not read_only and bool(self.config.policy_get("isolation.sync_back_on_accept", True))
        committed: list[str] = []
        blocked_reason: str | None = None
        external_drift: list[str] = []
        # 需求 44：删除/覆盖既有产物默认要「二次确认」。审批项在这里已经生成；
        # 未获人工批准前不自动回写工作区，保留完整工作副本供复核（approve --sync 可取回）。
        destructive_blocked = (
            bool(audit["destructive"])
            and not read_only
            and bool(self.config.policy_get("dangerous_ops.block_commit_on_destructive", True))
            and not self.guard.is_approved(project["id"], task["id"], "dangerous_op")
        )
        if destructive_blocked:
            sync_back = False
            blocked_reason = (
                f"检测到 {len(audit['destructive'])} 个删除操作，已保留工作副本，等待人工确认"
            )
            self._event(
                task,
                "task.blocked",
                blocked_reason or "",
                run_id=run_id,
                data={"destructive": audit["destructive"]},
            )
        if not claim_ok:
            # 任务在执行期间被取消/接管：结果只留档，绝不回写工作区
            sync_back = False
            self._event(task, "task.claim_lost", "执行期间任务状态被外部改变，结果不再回写", run_id=run_id)
        if cancelled:
            sync_back = False
            self._event(task, "task.cancel.finished", "已终止在跑的 Agent，半成品保留在 run 目录", run_id=run_id)
        if conflicts and block_conflict:
            sync_back = False
            blocked_reason = f"并发冲突：{', '.join(conflicts[:3])}"
            self._archive_paths(workdir, run_dir, conflicts, "conflicts")
            self.isolation.revert_out_of_scope(project, workdir, conflicts, baseline_dir)
            self.journal.mark_reverted(task["id"], conflicts, run_id=run_id)
            changes = [c for c in changes if c["rel_path"] not in set(conflicts)]
            self._event(task, "task.blocked", "并发冲突，阻止自动合并，交人工处理", run_id=run_id)

        # 验收（先在隔离副本上判定）
        manual_approved = self.guard.is_approved(project["id"], task["id"], "acceptance_failed")
        acceptance_conditions, acceptance_warnings = effective_acceptance(task, self.config)
        acceptance = evaluate_acceptance(
            acceptance_conditions,
            workspace=workdir,
            changes=changes,
            exit_code=result.exit_code,
            handoff=handoff,
            variables=variables,
            manual_approved=manual_approved,
        )

        # worktree 交付：把验收通过的改动在任务分支上落成一个真实 commit，
        # 由 ``POST /api/tasks/{tid}/merge`` 做真正的 git merge。
        # copy 模式 / 非 git 项目仍然走下面的文件回收（commit_back）。
        isolation_mode = (
            task.get("isolation") or self.config.policy_get("isolation.default", "copy")
        ).lower()
        project_root = Path(project["root_path"])
        worktree_active = (
            isolation_mode == "worktree"
            and not read_only
            and is_repo(project_root)
            and is_repo(Path(workdir))
        )
        git_info: dict[str, Any] | None = None
        if worktree_active:
            # worktree 的交付物是分支提交，不再把文件平铺回工作区（否则 merge 会撞上脏工作树）
            sync_back = False
            if result.ok and acceptance.ok and not cancelled and claim_ok and not conflicts:
                try:
                    branch = current_branch(Path(workdir)) or branch_for_task(
                        str(task["id"]), str(task.get("title") or "")
                    )
                    base_sha = _git_rev(Path(workdir), "HEAD")
                    author_name = str(self.config.policy_get("git.author_name", "MultiAgent") or "MultiAgent")
                    author_email = str(
                        self.config.policy_get("git.author_email", "multiagent@local")
                        or "multiagent@local"
                    )
                    outcome = commit_worktree(
                        Path(workdir),
                        f"task({_short_task_id(str(task['id']))}): {task.get('title') or ''}".strip(),
                        author_name=author_name,
                        author_email=author_email,
                    )
                    git_info = {
                        "branch": branch,
                        "commit": outcome.get("sha"),
                        "base": base_sha,
                        "files": outcome.get("files") or [],
                    }
                    if outcome.get("sha"):
                        self._event(
                            task,
                            "task.git.committed",
                            f"已提交 {str(outcome['sha'])[:8]} 到 {branch}",
                            run_id=run_id,
                            data={**git_info, "files": outcome.get("files") or []},
                        )
                        # 默认「先审后合」（policy.git.auto_merge=false）：
                        # 验收通过只在任务分支提交，等待人工合并；需要 DAG 连续
                        # 交付的项目显式把 git.auto_merge 设为 true。
                        # 注意这里用 False 作为缺省值，避免配置缺失时静默绕过审阅。
                        auto_merge = self.config.policy_bool("git.auto_merge", False)
                        git_info["auto_merge"] = auto_merge
                        if auto_merge:
                            merge = merge_worktree_branch(
                                project_root,
                                str(branch),
                                f"merge task({_short_task_id(str(task['id']))}): {task.get('title') or ''}".strip(),
                                require_clean=self.config.policy_bool("git.require_clean_tree", True),
                            )
                            git_info["merge"] = merge
                            if merge.get("blocked"):
                                blocked_reason = str(
                                    merge.get("error")
                                    or "基线工作树有未提交改动，已阻止自动合并"
                                )
                                self._event(
                                    task,
                                    "task.git.merge.blocked",
                                    blocked_reason,
                                    run_id=run_id,
                                    data=merge,
                                )
                            elif merge.get("conflict"):
                                blocked_reason = (
                                    "任务分支与基线冲突，已中止合并："
                                    + ", ".join(str(item) for item in (merge.get("files") or [])[:8])
                                )
                                self._event(
                                    task,
                                    "task.git.merge.conflict",
                                    blocked_reason,
                                    run_id=run_id,
                                    data=merge,
                                )
                            elif merge.get("merged"):
                                self._event(
                                    task,
                                    "task.git.merged",
                                    f"已合并 {branch} 到基线（{str(merge.get('sha') or '')[:8]}）",
                                    run_id=run_id,
                                    data=merge,
                                )
                            else:
                                # 既不是脏树阻断、也不是冲突：Git 命令本身失败。
                                # 绝不能记成 task.git.merged，否则事件流与真实交付不一致。
                                blocked_reason = (
                                    "任务分支合并失败："
                                    + str(merge.get("error") or merge.get("reason") or "未知 Git 错误")
                                )
                                self._event(
                                    task,
                                    "task.git.merge.failed",
                                    blocked_reason,
                                    run_id=run_id,
                                    data=merge,
                                )
                        else:
                            # 诚实记录「未自动合并」：UI 依据它显示人工合并入口，
                            # 而不是把「同步到工作区」当成已合并。
                            git_info["merge"] = {
                                "merged": False,
                                "skipped": True,
                                "reason": "auto_merge_disabled",
                                "branch": branch,
                                "base": base_sha,
                                "sha": None,
                                "files": [],
                            }
                            self._event(
                                task,
                                "task.git.merge.skipped",
                                "策略已关闭自动合并，改动保留在任务分支等待人工合并",
                                run_id=run_id,
                                data=git_info["merge"],
                            )
                except Exception as exc:  # noqa: BLE001 - git 失败必须阻断「成功」结论
                    LOGGER.exception("worktree 提交失败 task=%s", task["id"])
                    blocked_reason = f"worktree 提交失败，改动保留在分支工作副本：{type(exc).__name__}: {exc}"
                    git_info = {
                        "branch": current_branch(Path(workdir)),
                        "commit": None,
                        "base": _git_rev(Path(workdir), "HEAD"),
                        "error": str(exc),
                    }
                    self._event(task, "task.git.commit.failed", blocked_reason, run_id=run_id)

        # 只在「Agent 自身成功 + 验收通过」时回写工作区。
        # 退出码非 0（崩溃/超时/被中断）时即使产物看起来齐全，也只保留现场，
        # 由人工确认后再同步；否则半成品会静默覆盖用户文件（需求 42/43）。
        if sync_back and result.ok and acceptance.ok and not conflicts:
            try:
                committed = self.isolation.commit_back(
                    project, workdir, changes, only_in_scope=True, scope=scope, read_only=read_only
                )
            except WorkspaceDriftError as exc:
                external_drift = list(exc.paths)
                blocked_reason = f"工作区在任务执行期间被外部改动，已阻止自动覆盖：{', '.join(exc.paths[:5])}"
                self.guard.request(
                    project_id=project["id"],
                    kind="conflict",
                    title=f"检测到工作区外部改动：{', '.join(exc.paths[:3])}",
                    detail={"files": exc.paths, "run_id": run_id, "reason": "自动回收会覆盖人工改动"},
                    task_id=task["id"],
                    run_id=run_id,
                    agent_id=agent_id,
                    risk="high",
                    board=self.board,
                )
                self._event(task, "task.drift.blocked", blocked_reason, run_id=run_id)
            except Exception as exc:  # noqa: BLE001 - 回收失败（磁盘/权限/路径）不能把任务留在 running
                LOGGER.exception("回收工作副本失败 task=%s", task["id"])
                blocked_reason = f"回收工作副本失败，已保留现场：{type(exc).__name__}: {exc}"
                self.guard.request(
                    project_id=project["id"],
                    kind="conflict",
                    title=f"回收工作副本失败：{task['title']}",
                    detail={"run_id": run_id, "error": str(exc)},
                    task_id=task["id"],
                    run_id=run_id,
                    agent_id=agent_id,
                    risk="high",
                    board=self.board,
                )
                self._event(task, "task.commit.failed", blocked_reason, run_id=run_id)

        # 记录产物
        self._record_artifacts(task, project, run_id, workdir, changes)

        # 需求 42 的延伸：任务“成功”必须以**工作区里的真实产物**为准。
        # 历史事故：验收在隔离副本上通过、任务标记成功，但回收阶段因冲突/漂移
        # 没有真正写回，用户在项目目录里看不到任何文件。这里做最后一道证据校验：
        # 声明了 outputs 的任务，成功前必须能在工作区根目录找到这些文件。
        missing_in_workspace: list[str] = []
        # worktree 的产物在任务分支上，工作区根目录在 merge 之前本来就没有它们。
        if not read_only and not worktree_active and sync_back and task.get("outputs"):
            root = Path(project["root_path"])
            for rel in task.get("outputs") or []:
                try:
                    target = resolve_workspace_path(root, rel)
                except Exception:  # noqa: BLE001 - 非法产物路径按缺失处理
                    missing_in_workspace.append(str(rel))
                    continue
                if not target.exists():
                    missing_in_workspace.append(str(rel))
        # 只有「本来就应该自动回收」的任务才需要校验工作区产物；
        # 明确关闭 sync_back 的策略（产物留在副本里等待人工同步）不应因此被判阻塞。
        if missing_in_workspace and result.ok and acceptance.ok and not cancelled and sync_back:
            blocked_reason = (
                "验收在副本内通过，但工作区缺少声明的产物："
                + ", ".join(missing_in_workspace[:10])
            )

        # 合并成功后可选的 worktree 回收：只删工作目录，任务分支与提交永远保留，
        # 之后仍可从分支追溯/重新检出。默认 keep_worktree=true（留下现场）。
        if (
            worktree_active
            and git_info
            and bool((git_info.get("merge") or {}).get("merged"))
            and not self.config.policy_bool("git.keep_worktree", True)
        ):
            worktree_path = str(workdir)
            try:
                remove_worktree(project_root, Path(workdir), branch=None, delete_branch=False)
                git_info["worktree_removed"] = True
                git_info["worktree_path"] = worktree_path
                self._event(
                    task,
                    "task.git.worktree.removed",
                    f"已回收 worktree（任务分支 {git_info.get('branch')} 保留）",
                    run_id=run_id,
                    data={"path": worktree_path, "branch": git_info.get("branch")},
                )
            except Exception as exc:  # noqa: BLE001 - 回收失败不影响已完成的合并
                git_info["worktree_removed"] = False
                git_info["worktree_remove_error"] = f"{type(exc).__name__}: {exc}"
                LOGGER.warning("回收 worktree 失败 task=%s: %s", task["id"], exc)

        # 状态判定
        if cancelled:
            # 取消优先于一切：即使 Agent 恰好也跑成功/验收失败，用户要的是「已取消」
            status = TaskStatus.CANCELLED.value
            reason = "执行被取消，已终止在跑的 Agent（现场保留在 run 目录）"
        elif blocked_reason:
            status = TaskStatus.BLOCKED.value
            reason = blocked_reason
        elif acceptance.need_manual:
            status = TaskStatus.BLOCKED.value
            reason = "等待人工确认"
            self.guard.request(
                project_id=project["id"],
                kind="manual_confirm",
                title=f"任务需要人工确认：{task['title']}",
                detail={"run_id": run_id, "checks": acceptance.checks},
                task_id=task["id"],
                run_id=run_id,
                agent_id=agent_id,
                risk="low",
                board=self.board,
            )
        elif result.ok and acceptance.ok and not missing_in_workspace:
            status = TaskStatus.SUCCESS.value
            reason = None
        else:
            status = TaskStatus.FAILED.value
            reason = (result.error or "") + ("；" + acceptance.summary() if not acceptance.ok else "")
            if missing_outputs:
                reason = (reason + "；" if reason else "") + "缺失产物：" + ", ".join(missing_outputs[:10])
            self.guard.request(
                project_id=project["id"],
                kind="acceptance_failed",
                title=f"验收失败：{task['title']}",
                detail={"run_id": run_id, "checks": acceptance.checks, "exit_code": result.exit_code},
                task_id=task["id"],
                run_id=run_id,
                agent_id=agent_id,
                risk="medium",
                board=self.board,
            )

        finished = now_iso()
        # 状态落库走 finish_claim：凭证失效则保持外部（人工）设定的状态，不回写
        applied = self.tasks.finish_claim(task["id"], claim_token, status, reason)
        final_status = status if applied else self.tasks.get(task["id"])["status"]
        run_status = {
            TaskStatus.SUCCESS.value: "success",
            TaskStatus.BLOCKED.value: "blocked",
            TaskStatus.WAITING.value: "waiting",
            TaskStatus.CANCELLED.value: "cancelled",
            TaskStatus.INTERRUPTED.value: "interrupted",
        }.get(final_status, "failed")
        self.db.update(
            "runs",
            {"id": run_id},
            {
                "status": run_status,
                "exit_code": result.exit_code,
                "finished_at": finished,
                "duration_ms": result.duration_ms,
                "cost_tokens": result.cost_tokens,
                "cost_amount": result.cost_amount,
                "error": result.error,
            },
        )
        self.quota.record(
            agent_id,
            # 调用次数已在派发前原子预占（reserve），这里只补记真实耗时/费用，避免重复计数
            seconds=result.duration_ms / 1000.0,
            cost=result.cost_amount,
            tokens=result.cost_tokens,
            invocations=0 if quota_reserved else 1,
        )

        previous_result = dict(task.get("result") or {})
        cost_source = str(getattr(result, "cost_source", "") or "").strip() or (
            "reported" if (result.cost_tokens or result.cost_amount) else "unreported"
        )
        result_payload: dict[str, Any] = {
            **previous_result,
            "run_id": run_id,
            "agent_id": agent_id,
            "exit_code": result.exit_code,
            # 0 成本不等于免费：source 明确区分「真实上报」与「协议未提供」。
            "cost": {
                "tokens": result.cost_tokens,
                "amount": result.cost_amount,
                "source": cost_source,
            },
            "acceptance": {
                "ok": acceptance.ok,
                "summary": acceptance.summary(),
                "checks": acceptance.checks,
                "warnings": acceptance_warnings,
            },
            "generated_outputs": generated_outputs,
            "missing_outputs": missing_outputs,
            "output_status": outputs,
            "hygiene": hygiene,
            # 实际隔离模式与降级原因：UI 用它显示「copy（worktree 不可用：dirty）」，
            # 而不是把任务声明的 worktree 当成真正执行的模式。
            **({"isolation": isolation_info} if isolation_info is not None else {}),
            # 每次 run 开始时清空上一次的进程审计结果；异步审计完成后回填。
            # 这样 UI 不会把上一轮遗留的端口/进程当成这一轮的。
            "processes": None,
            "committed": committed,
            "external_drift": external_drift,
            "conflicts": conflicts,
            "out_of_scope": audit["out_of_scope"],
            "handoff_id": handoff["id"],
        }
        if git_info is not None:
            # 不覆盖 result 里其它字段：只补 git 交付信息，供 /merge、/commit 和 UI 读取。
            result_payload["git"] = git_info
        else:
            # 本次尝试没有 git 交付（copy/shared/降级/失败）时必须清掉上一次尝试的
            # git 元数据；否则 UI 会把旧分支显示成“待合并”，点击后合并的是旧分支。
            result_payload.pop("git", None)
        self.tasks.update(
            task["id"],
            result=result_payload,
        )
        if applied and final_status == TaskStatus.FAILED.value:
            # 失败原因留存：重试/复盘不用再去翻 run 行
            self.tasks.update(task["id"], last_error=reason or result.error or acceptance.summary())
        elif applied:
            self.tasks.update(task["id"], last_error=None)
        self._event(
            task,
            f"task.{final_status}",
            f"{agent_id} 执行完成：{acceptance.summary()}",
            run_id=run_id,
            data={
                "exit_code": result.exit_code,
                "changes": len(changes),
                "generated_outputs": generated_outputs,
                "missing_outputs": missing_outputs,
                "temporary_files": hygiene.get("temporary_files", 0),
                "applied": applied,
            },
        )

        return RunOutcome(
            task_id=task["id"],
            run_id=run_id,
            agent_id=agent_id,
            status=final_status,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            changes=changes,
            acceptance_ok=acceptance.ok,
            acceptance={"ok": acceptance.ok, "summary": acceptance.summary(), "checks": acceptance.checks},
            handoff_id=handoff["id"],
            error=reason,
            conflicts=conflicts,
            out_of_scope=audit["out_of_scope"],
            committed=committed,
            timeout_sec=timeout_sec,
            missing_outputs=missing_outputs,
            hygiene=hygiene,
        )

    # ------------------------------------------------------------------ 人工
    def _await_human(self, task, project, run_id: str | None = None) -> RunOutcome:
        self.tasks.set_status(task["id"], TaskStatus.WAITING.value, "等待人工执行")
        if run_id:
            # ACP/CLI 返回 AWAITING_HUMAN 时本次 run 仍有异步进程审计在跑；
            # 写入当前 run_id 才能让审计线程在收尾后把 processes 回填到本任务，
            # 否则会因旧 run_id 不匹配而静默丢弃。
            current = dict(task.get("result") or {})
            current["run_id"] = run_id
            current["processes"] = None
            self.tasks.update(task["id"], result=current)
        self.guard.request(
            project_id=project["id"],
            kind="manual_confirm",
            title=f"等待人工完成：{task['title']}",
            detail={"task_id": task["id"], "brief": task.get("brief")},
            task_id=task["id"],
            run_id=run_id,
            agent_id="human",
            risk="low",
            board=self.board,
        )
        self._event(task, "task.waiting", "任务等待人工执行", run_id=run_id)
        return RunOutcome(task["id"], run_id or "", "human", TaskStatus.WAITING.value, AWAITING_HUMAN, 0, awaiting="human")

    # ------------------------------------------------------------------ 工具
    def _predecessor_handoffs(self, task) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for dep in self.tasks.dependencies(task["id"]):
            handoff = self.handoffs.latest_for_task(dep["id"])
            if handoff:
                result.append(handoff)
        return result

    def _archive_paths(self, workdir: Path, run_dir: Path, rel_paths: list[str], label: str) -> list[str]:
        """把即将被还原/删除的文件复制到 run 目录留证（需求 43）。

        越权产物与冲突版本都不该被物理销毁：人工复核后可能正是需要的交付物。
        只复制单个文件；目录/符号链接跳过，避免把整个工作区搬进 run 目录。
        """
        archived: list[str] = []
        base = Path(run_dir) / label
        for rel in rel_paths:
            try:
                source = safe_join(Path(workdir), rel)
                target = safe_join(base, rel)
            except (ValueError, OSError):
                continue
            if not source.is_file():
                continue
            try:
                ensure_dir(target.parent)
                shutil.copy2(source, target)
                archived.append(rel)
            except OSError:
                continue
        return archived

    def _variables(self, task, project, workdir) -> dict[str, Any]:
        variables: dict[str, Any] = {
            "task_id": task["id"],
            "title": task["title"],
            "kind": task["kind"],
            "goal": task.get("brief", ""),
            "workspace": str(workdir),
            "project": project["name"],
            "project_root": project["root_path"],
        }
        variables.update(task.get("result") or {})
        outputs = task.get("outputs") or []
        if outputs and isinstance(outputs[0], str):
            variables.setdefault("target", outputs[0])
        variables.setdefault("target", "")
        variables.setdefault("contains", "")
        variables.setdefault("command", "")
        return variables

    def _record_artifacts(self, task, project, run_id, workdir, changes) -> None:
        root = Path(workdir)
        seen: set[str] = set()
        for change in changes:
            if change["action"] == "delete":
                continue
            rel = change["rel_path"]
            path = root / rel
            if not path.exists():
                continue
            seen.add(rel)
            self.tasks.record_artifact(
                project_id=project["id"],
                task_id=task["id"],
                run_id=run_id,
                role="output",
                path=path,
                root=root,
                record={"action": change["action"]},
            )
        for pattern in task.get("outputs") or []:
            if not isinstance(pattern, str) or pattern in seen:
                continue
            candidate = root / pattern
            if candidate.exists() and candidate.is_file():
                self.tasks.record_artifact(
                    project_id=project["id"], task_id=task["id"], run_id=run_id, role="output", path=candidate, root=root
                )
        for item in task.get("inputs") or []:
            rel = item if isinstance(item, str) else (item or {}).get("path")
            if not rel:
                continue
            candidate = root / rel
            if candidate.exists() and candidate.is_file():
                self.tasks.record_artifact(
                    project_id=project["id"],
                    task_id=task["id"],
                    run_id=run_id,
                    role="input",
                    path=candidate,
                    root=root,
                    record=item if isinstance(item, dict) else {},
                )

    def _on_agent_event(self, event: dict[str, Any]) -> None:
        if self.event_sink:
            self.event_sink(event)

    def _emit_agent_event(
        self,
        task: dict[str, Any],
        agent_id: str,
        event: dict[str, Any],
        *,
        run_id: str | None = None,
    ) -> None:
        """给适配器事件补上项目/任务/Agent 上下文，推给事件总线（实时流）。"""
        if not self.event_sink:
            return
        inner = dict(event or {})
        payload: dict[str, Any] = {
            "type": "agent.update",
            "source_type": inner.pop("type", None),
            "project_id": task.get("project_id"),
            "task_id": task.get("id"),
            "agent_id": agent_id,
        }
        if run_id:
            payload["run_id"] = run_id
        payload.update(inner)
        self.event_sink(payload)

    def _event(self, task, kind: str, summary: str, run_id: str | None = None, data: dict[str, Any] | None = None) -> None:
        self.board.append_event(
            project_id=task.get("project_id"),
            kind=kind,
            summary=summary,
            actor=task.get("agent_id"),
            task_id=task.get("id"),
            run_id=run_id,
            data=data or {},
        )
        if self.event_sink:
            self.event_sink({"type": "task.event", "kind": kind, "task_id": task.get("id"), "summary": summary})


def _trusted_duration(result, wall_ms: int):
    """需求 42：不采信执行单元的自述。

    适配器可能少报/不报耗时（真实 ACP 就出现过 ``duration_ms=0``，导致 ``max_seconds``
    额度永远不触发）。这里以 runner 的墙钟实测为下界，取 ``max(上报值, 墙钟)``。
    """
    reported = int(getattr(result, "duration_ms", 0) or 0)
    trusted = max(reported, int(wall_ms or 0))
    if trusted != reported:
        result.duration_ms = trusted
    return result


def _git_rev(root: Path, ref: str = "HEAD") -> str | None:
    code, out, _ = _git_cmd(root, ["rev-parse", ref], timeout=10)
    value = (out or "").strip()
    return value if code == 0 and value else None


def _short_task_id(task_id: str) -> str:
    text = str(task_id or "")
    tail = text.split("-")[-1] if "-" in text else text
    return (tail or text)[:8]


def _handoff_from_result(result) -> dict[str, Any]:
    """Agent 没写交接文件时，从它的输出里尽力解析；再不行就合成一条。"""
    from ..board.handoff import parse_handoff

    for text in (result.stdout, "\n".join(result.messages or [])):
        if not text:
            continue
        parsed = parse_handoff(text)
        if parsed:
            return parsed
    summary = truncate((result.stdout or "").strip(), 1500)
    return {
        "did": summary or "（Agent 未提供交接说明）",
        "not_done": "",
        "open_issues": [] if result.ok else ["Agent 未能完成或未通过验收"],
        "next_steps": [],
        "artifacts": [],
        "notes": "此交接由系统依据 Agent 输出合成，建议人工复核。",
    }
