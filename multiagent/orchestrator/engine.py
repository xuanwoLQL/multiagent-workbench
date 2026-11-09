"""编排引擎：串行/并行调度、并发上限、重试/跳过/人工接管、中断恢复、同题多 Agent 扇出。"""

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..agents.proc import process_alive
from ..tasks.service import InvalidTransition, TaskStatus
from ..util import parse_iso
from .cancellation import RunCancelRegistry
from .runner import RunOutcome
from .slots import SlotService

# 没有可派发任务时的最大空转轮数（退避等待 / 全局额度等待共用，防呆上限）
_MAX_IDLE_POLLS = 300


class MergeRetryBlocked(ValueError):
    """合并失败导致的任务阻塞不允许通过普通 retry 重新执行。"""

    def __init__(self, task_id: str, blocked_reason: str, message: str):
        super().__init__(message)
        self.task_id = task_id
        self.blocked_reason = blocked_reason
        self.message = message


class _ConcurrencyGate:
    """进程内并发闸门：``global`` / ``per_project`` 在多调用方并发下也真正生效。

    之前每个 ``run_project`` 各自算 ``min(global, per_project, max_parallel)`` 并自建线程池，
    两个并发调用就会各自开满，实际并发翻倍（压测峰值 4 而 per_project=2）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total = 0
        self._per: dict[str, int] = {}

    def acquire(self, project_id: str, global_limit: int, per_project_limit: int) -> bool:
        with self._lock:
            if self._total >= global_limit:
                return False
            if self._per.get(project_id, 0) >= per_project_limit:
                return False
            self._total += 1
            self._per[project_id] = self._per.get(project_id, 0) + 1
            return True

    def release(self, project_id: str) -> None:
        with self._lock:
            if self._total > 0:
                self._total -= 1
            current = self._per.get(project_id, 0)
            if current <= 1:
                self._per.pop(project_id, None)
            else:
                self._per[project_id] = current - 1

    def active(self) -> int:
        with self._lock:
            return self._total


class _SessionHeartbeat(threading.Thread):
    """后台刷新本进程会话心跳，让别的进程知道「我还在跑」。"""

    def __init__(self, db, interval: float, slots: SlotService | None = None, ttl: float = 1800):
        super().__init__(name="multiagent-heartbeat", daemon=True)
        self._db = db
        self._interval = max(0.5, float(interval))
        self._slots = slots
        self._ttl = max(60.0, float(ttl))
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self._interval):
            self._db.touch_session()
            if self._slots is not None:
                try:
                    self._slots.renew(self._ttl)
                except Exception:  # noqa: BLE001 - 槽位续租失败不能拖垮心跳
                    pass

    def stop(self) -> None:
        self._stop.set()


class Engine:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.config = app.config
        self._lock = threading.RLock()
        # 「不再派发」标志按项目隔离（需求 10）：停项目 A 不能顺带停掉 B 的调度循环。
        # project_id 进入 _stop_projects；进程退出/全局急停走 _stop_all。
        self._stop_lock = threading.Lock()
        self._stop_all = False
        self._stop_projects: set[str] = set()
        self._running: dict[str, Future] = {}
        self.slots = SlotService(self.db, self.config)
        self._heartbeat: _SessionHeartbeat | None = None
        # 在跑的 run_id -> 取消信号；runner 每次调用登记/注销（需求 54）
        self.run_cancels = RunCancelRegistry()
        # Workbench 先建 runner 再建 engine，这里把注册表注入给 runner（runner 不反向依赖 engine）
        if getattr(app.runner, "run_cancels", None) is not None:
            app.runner.run_cancels = self.run_cancels

    # ------------------------------------------------------------------ 停止标志
    def _request_stop(self, project_id: str | None = None) -> None:
        """置「不再派发」标志；``project_id=None`` 表示全局（进程退出）。"""
        with self._stop_lock:
            if project_id is None:
                self._stop_all = True
                self._stop_projects.clear()
            else:
                self._stop_projects.add(project_id)

    def _clear_stop(self, project_id: str) -> None:
        """恢复某项目的派发（``run_project`` 开始时调用，等价于旧实现的 resume 语义）。"""
        with self._stop_lock:
            self._stop_all = False
            self._stop_projects.discard(project_id)

    def _is_stopped(self, project_id: str) -> bool:
        with self._stop_lock:
            return self._stop_all or project_id in self._stop_projects

    # ------------------------------------------------------------------ 心跳
    def _ensure_heartbeat(self) -> None:
        if self._heartbeat is not None and self._heartbeat.is_alive():
            return
        interval = float(self.config.policy_get("recover.heartbeat_seconds", 5))
        ttl = float(self.config.policy_get("concurrency.slot_ttl_seconds", 1800) or 1800)
        self._heartbeat = _SessionHeartbeat(self.db, interval, self.slots, ttl)
        self._heartbeat.start()

    # ------------------------------------------------------------------ 恢复
    def _live_sessions(self, stale_seconds: float) -> set[str]:
        """仍然活着的会话（含本进程）。

        两路信号：
        - **同机会话**（``host`` 与本机相同）：用 ``process_alive(pid)`` 做**确定性**判定。
          PID 已不存在 = 该会话（实例）已经死了，**无视心跳新鲜度**——进程崩溃后立刻重启
          也能马上回收现场，不必等 ``stale_seconds``。PID 仍存在 = 本机另一个活着的调度方，
          不能误杀。
        - **跨机会话**（host 不同或为空）：拿不到对端 PID，只能按心跳 staleness 判定。

        因此 ``stale_seconds`` 现在**只对跨 host 场景（以及没有 host 的老会话行）有意义**。
        """
        fresh = self.db.live_sessions(stale_seconds)
        local_host = self.db.host
        live: set[str] = set()
        for row in self.db.sessions():
            session_id = row["session_id"]
            host = row.get("host") or ""
            if host and local_host and host == local_host:
                if process_alive(int(row.get("pid") or 0)):
                    live.add(session_id)
                continue
            if session_id in fresh:
                live.add(session_id)
        return live

    def recover(
        self,
        project_id: str | None = None,
        *,
        stale_seconds: float | None = None,
        force: bool = False,
    ) -> dict[str, int]:
        """把「确实已经死掉」的执行中任务标记为中断，可续跑（需求 40）。

        判定依据（**不是**只看 ``status='running'``）：

        1. 任务名下有没有 ``status='running'`` 的 run 行；
        2. run 的 ``owner_session`` 是本进程 → 以进程内 active run 集合为准（权威，绝不错杀
           本进程正在执行的任务）；
        3. 是别的会话 → 该会话是否存活（同机看 PID，跨机看心跳，见 :meth:`_live_sessions`）；
        4. 没有 run 行 / 会话未知 / 会话已死 → 崩溃残留，标 ``interrupted``。

        这样重启后既不会把「其实还在别的活进程里跑」的任务误标中断，也不会把「已经死了的」
        一直留在 running。

        ``force=True``（**人工触发**的恢复路径：API ``/recover``、CLI 恢复命令）无视一切
        存活判定，把范围内所有 running 任务与 run 行强制回收——人明确说「那个实例已经死了」
        时不该被心跳或 PID 挡住。自动调度（``run_project`` 内部那次 recover）一律保持保守。
        """
        stale = float(
            stale_seconds if stale_seconds is not None else self.config.policy_get("recover.stale_seconds", 300)
        )
        live_sessions: set[str] = set() if force else self._live_sessions(stale)
        live_sessions.add(self.db.session_id)

        clauses = "status = 'running'"
        params: list[Any] = []
        if project_id:
            clauses += " AND project_id = ?"
            params.append(project_id)
        rows = self.db.query(
            f"SELECT id, project_id, claim_owner, claimed_at FROM tasks WHERE {clauses}", params
        )

        interrupted = 0
        kept: set[str] = set()
        for row in rows:
            runs = self.db.query(
                "SELECT id, owner_session FROM runs WHERE task_id = ? AND status = 'running'", (row["id"],)
            )
            if not force and (
                any(self._run_is_alive(run, live_sessions) for run in runs)
                or self._claim_is_live(row, live_sessions, stale)
            ):
                kept.add(row["id"])
                continue
            self.app.tasks.set_status(
                row["id"], TaskStatus.INTERRUPTED.value, "执行进程已退出或失联，任务中断，可续跑"
            )
            interrupted += 1

        # 孤儿 run 行（任务已经不在 running，但 run 还挂着 running）→ 一并收尾
        run_clauses = "status = 'running'"
        run_params: list[Any] = []
        if project_id:
            run_clauses += " AND project_id = ?"
            run_params.append(project_id)
        for run in self.db.query(f"SELECT id, task_id, owner_session FROM runs WHERE {run_clauses}", run_params):
            if not force:
                if run["task_id"] in kept:
                    continue
                if self._run_is_alive(run, live_sessions):
                    continue
            self.db.update("runs", {"id": run["id"]}, {"status": "interrupted", "finished_at": _now()})

        # 租约：只清死掉的持有者（以及孤儿租约），绝不抢活进程的租约
        lease_sql = "SELECT resource, task_id, holder FROM leases"
        lease_params: list[Any] = []
        if project_id:
            lease_sql += " WHERE project_id = ?"
            lease_params.append(project_id)
        for lease in self.db.query(lease_sql, lease_params):
            if not force:
                holder_alive = lease["holder"] in live_sessions
                task_kept = bool(lease["task_id"]) and lease["task_id"] in kept
                if holder_alive or task_kept:
                    continue
            self.db.execute("DELETE FROM leases WHERE resource = ?", (lease["resource"],))

        self.db.purge_stale_sessions(older_than_seconds=7 * 86400)
        try:
            self.slots.cleanup(stale_seconds=max(30.0, float(stale)))
        except Exception:  # noqa: BLE001 - 槽位清理失败不影响恢复结论
            pass
        return {"interrupted": interrupted, "kept": len(kept)}

    def _run_is_alive(self, run, live_sessions: set[str]) -> bool:
        owner = run["owner_session"]
        if not owner:
            return False
        if owner == self.db.session_id:
            return bool(self.db.runtime.is_active(run["id"]))
        return owner in live_sessions

    def _claim_is_live(self, task_row, live_sessions: set[str], stale_seconds: float) -> bool:
        """「已认领但 run 行还没落库」的窗口期（引擎先 claim，工作线程随后才插 run 行）。

        这个窗口里任务已经是 running 却没有 run 行，旧逻辑会把它当崩溃残留误标中断。
        判定：认领者会话还活着（见 :meth:`_live_sessions`：同机看 PID、跨机看心跳）
        且认领时间在 stale 窗口内。
        """
        owner = task_row["claim_owner"]
        if not owner or owner not in live_sessions:
            return False
        moment = parse_iso(task_row["claimed_at"])
        if moment is None:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - moment).total_seconds() <= float(stale_seconds)

    # ------------------------------------------------------------------ 调度
    def run_project(
        self,
        project_id: str,
        *,
        only: list[str] | None = None,
        auto_retry: bool = True,
        max_parallel: int | None = None,
        max_seconds: float | None = None,
        poll_seconds: float = 0.2,
    ) -> dict[str, Any]:
        self._ensure_heartbeat()
        self.recover(project_id)
        global_limit = int(self.config.policy_get("concurrency.global", 6))
        per_project = int(self.config.policy_get("concurrency.per_project", global_limit))
        limit = max(1, min(global_limit, per_project, int(max_parallel) if max_parallel else global_limit))
        deadline = time.monotonic() + max_seconds if max_seconds else None
        outcomes: list[RunOutcome] = []
        inflight: dict[Future, str] = {}
        slot_by_future: dict[Future, str] = {}
        running_ids: set[str] = set()
        self._clear_stop(project_id)
        idle_waits = 0
        deadline_hit = False
        stopped = False
        ttl = float(self.config.policy_get("concurrency.slot_ttl_seconds", 1800) or 1800)

        with ThreadPoolExecutor(max_workers=limit) as pool:
            while True:
                if self._is_stopped(project_id):
                    stopped = True
                    break
                if deadline and time.monotonic() > deadline:
                    deadline_hit = True
                    break
                dispatched_any = False
                dispatchable = self._dispatchable(project_id, only, auto_retry) if len(inflight) < limit else []
                for task in dispatchable:
                    if len(inflight) >= limit:
                        break
                    if task["id"] in running_ids:
                        continue
                    # 跨进程闸门：CLI / API / 桌面后端同时调度也不会突破 global/per_project
                    slot_id = self.slots.acquire(
                        project_id=project_id,
                        task_id=task["id"],
                        global_limit=global_limit,
                        per_project_limit=per_project,
                        ttl_seconds=ttl,
                    )
                    if not slot_id:
                        break
                    token = self.app.tasks.claim(task["id"])
                    if not token:
                        self.slots.release(slot_id)
                        continue
                    try:
                        future = pool.submit(
                            self.app.runner.run,
                            task["id"],
                            agent_id=task.get("agent_id"),
                            claim_token=token,
                        )
                    except Exception:  # noqa: BLE001 - 提交失败要归还额度并放掉认领
                        self.slots.release(slot_id)
                        self.app.tasks.set_status(
                            task["id"], TaskStatus.INTERRUPTED.value, "调度提交失败，可续跑"
                        )
                        continue
                    inflight[future] = task["id"]
                    slot_by_future[future] = slot_id
                    running_ids.add(task["id"])
                    dispatched_any = True

                if inflight:
                    done, _ = wait(list(inflight.keys()), timeout=poll_seconds, return_when=FIRST_COMPLETED)
                    for future in done:
                        task_id = inflight.pop(future)
                        running_ids.discard(task_id)
                        self.slots.release(slot_by_future.pop(future, None))
                        try:
                            outcomes.append(future.result())
                        except Exception as exc:  # noqa: BLE001
                            self.app.tasks.set_status(task_id, TaskStatus.FAILED.value, f"执行异常：{exc}")
                            outcomes.append(RunOutcome(task_id, "", "", TaskStatus.FAILED.value, -3, 0, error=str(exc)))
                    idle_waits = 0
                    continue

                if not dispatchable:
                    # 可能有任务正在退避等待重试：等它到点，而不是提前收工
                    retry_wait = self._next_retry_delay(project_id, only) if auto_retry else None
                    if retry_wait is None:
                        break
                    idle_waits += 1
                    if idle_waits > _MAX_IDLE_POLLS:
                        break
                    time.sleep(min(max(retry_wait, 0.01), poll_seconds))
                    continue
                # 有可派发任务但一个都没派出去：全局额度被其它调度方占满 → 等
                idle_waits += 1
                if idle_waits > _MAX_IDLE_POLLS:
                    break
                time.sleep(poll_seconds)

            # ``max_seconds`` / 项目停止必须是**硬上限**：只停止派发不够，
            # 线程池退出时会一直等在跑的 Future。这里向仍在执行的 run 发取消信号，
            # 让适配器整树回收子进程，再等待它们收尾。
            if inflight:
                reason = "调度停止" if stopped else "调度时间上限"
                self._cancel_inflight(set(inflight.values()), reason)
                for future in list(inflight.keys()):
                    try:
                        future.result(timeout=15)
                    except Exception:  # noqa: BLE001 - 超时由线程池退出兜底
                        pass

        # 兜底：循环提前退出时，已提交的 future 仍会被线程池等待完成；
        # 这里统一释放它们的槽位，避免崩溃/停止后槽位泄漏到 TTL 过期。
        for slot_id in slot_by_future.values():
            self.slots.release(slot_id)

        return {
            "project_id": project_id,
            "dispatched": len(outcomes),
            "success": sum(1 for o in outcomes if o.status == TaskStatus.SUCCESS.value),
            "failed": sum(1 for o in outcomes if o.status == TaskStatus.FAILED.value),
            "blocked": sum(1 for o in outcomes if o.status == TaskStatus.BLOCKED.value),
            "waiting": sum(1 for o in outcomes if o.status == TaskStatus.WAITING.value),
            "deadline_hit": deadline_hit,
            "progress": self.app.tasks.progress(project_id),
            "outcomes": [
                {
                    "task_id": o.task_id,
                    "run_id": o.run_id,
                    "agent_id": o.agent_id,
                    "status": o.status,
                    "acceptance": o.acceptance,
                    "changes": len(o.changes),
                    "error": o.error,
                }
                for o in outcomes
            ],
        }

    def _cancel_inflight(self, task_ids: set[str], reason: str, *, timeout: float = 3.0) -> None:
        """向仍在跑的调用发取消信号；容忍「已派发但 run 行尚未落库」的窗口期。

        ``run_project`` 在 deadline/stop 时调用。若立刻查 DB，可能还没看到 run 行，
        于是取消信号落空、线程池只能干等 Agent 自然结束（实测可多等 15s+）。
        这里给每个任务最多 ``timeout`` 秒去等 run 行出现。
        """
        pending = set(task_ids)
        deadline = time.monotonic() + max(0.0, float(timeout))
        while pending and time.monotonic() <= deadline:
            for task_id in list(pending):
                run_ids = self._running_run_ids(task_id=task_id)
                if not run_ids:
                    continue
                self._signal_cancel(run_ids, reason)
                pending.discard(task_id)
            if pending:
                time.sleep(0.1)
        # 仍没有 run 行：任务可能还在适配器构造阶段。置 cancelled 让收尾凭证失效，
        # 至少保证不会把结果回写工作区。
        for task_id in pending:
            try:
                self.app.tasks.set_status(task_id, TaskStatus.CANCELLED.value, reason)
            except InvalidTransition:
                pass

    def _dispatchable(self, project_id: str, only: list[str] | None, auto_retry: bool) -> list[dict[str, Any]]:
        ready = self.app.tasks.ready_tasks(project_id)
        if only is not None:
            ready = [t for t in ready if t["id"] in only]
        # 中断 / 失败可重试（受 max_attempts + 退避约束）
        if auto_retry:
            backoff = float(self.config.policy_get("retry.backoff_seconds", 0) or 0)
            for status in (TaskStatus.INTERRUPTED.value, TaskStatus.FAILED.value):
                for task in self.app.tasks.list(project_id, status=status):
                    if int(task["attempt"]) >= int(task["max_attempts"]):
                        continue
                    if only is not None and task["id"] not in only:
                        continue
                    if not self._retry_ready(task, backoff):
                        continue
                    self.app.tasks.set_status(task["id"], TaskStatus.PENDING.value, "自动重试")
                    ready.append(self.app.tasks.get(task["id"]))
        ready.sort(key=lambda t: (-int(t.get("priority") or 0), int(t.get("ordering") or 0)))
        return ready

    @staticmethod
    def retry_delay(attempt: int, backoff_seconds: float) -> float:
        """指数退避：``backoff * 2^(attempt-1)``（backoff<=0 表示不退避）。"""
        if backoff_seconds <= 0:
            return 0.0
        return float(backoff_seconds) * (2 ** max(0, int(attempt) - 1))

    def _remaining_retry_delay(self, task: dict[str, Any], backoff_seconds: float) -> float:
        """距离「可以重试」还差多少秒（0 = 现在就可以）。"""
        delay = self.retry_delay(int(task.get("attempt") or 0), backoff_seconds)
        if delay <= 0:
            return 0.0
        anchor = parse_iso(task.get("finished_at") or task.get("updated_at"))
        if anchor is None:
            return 0.0
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        return max(0.0, delay - (datetime.now(timezone.utc) - anchor).total_seconds())

    def _retry_ready(self, task: dict[str, Any], backoff_seconds: float) -> bool:
        return self._remaining_retry_delay(task, backoff_seconds) <= 0

    def _next_retry_delay(self, project_id: str, only: list[str] | None) -> float | None:
        """所有「还会重试但正在退避」的任务里，最早到点的剩余等待秒数。"""
        backoff = float(self.config.policy_get("retry.backoff_seconds", 0) or 0)
        if backoff <= 0:
            return None
        waits: list[float] = []
        for status in (TaskStatus.INTERRUPTED.value, TaskStatus.FAILED.value):
            for task in self.app.tasks.list(project_id, status=status):
                if int(task["attempt"]) >= int(task["max_attempts"]):
                    continue
                if only is not None and task["id"] not in only:
                    continue
                waits.append(self._remaining_retry_delay(task, backoff))
        return min(waits) if waits else None

    # ------------------------------------------------------------------ 单任务
    def run_task(self, task_id: str, *, agent_id: str | None = None) -> RunOutcome:
        self._ensure_heartbeat()
        task = self.app.tasks.get(task_id)
        global_limit = int(self.config.policy_get("concurrency.global", 6))
        per_project = int(self.config.policy_get("concurrency.per_project", global_limit))
        ttl = float(self.config.policy_get("concurrency.slot_ttl_seconds", 1800) or 1800)
        slot_id = self.slots.acquire(
            project_id=task["project_id"],
            task_id=task_id,
            global_limit=global_limit,
            per_project_limit=per_project,
            ttl_seconds=ttl,
        )
        if not slot_id:
            return RunOutcome(
                task_id,
                "",
                task.get("agent_id") or "",
                TaskStatus.BLOCKED.value,
                -1,
                0,
                error="并发上限已满（global/per_project），请稍后重试或等待在跑任务结束",
            )
        try:
            return self.app.runner.run(task_id, agent_id=agent_id)
        finally:
            self.slots.release(slot_id)

    def fanout(self, task_id: str, agent_ids: list[str]) -> dict[str, Any]:
        """同一任务分派给多个 Agent 分别尝试，保留各自结果供比较（需求 6 / 50）。

        注意：**不要**在这里 ``isolation.discard()``。task_dir 下就是 ``runs/``，
        删掉它等于把之前每一次尝试的 prompt/stdout/transcript 证据一起删掉（需求 43/46）。
        每次尝试的干净副本由 ``isolation.prepare()`` 自己负责（它本来就 rmtree 重建
        ``work/`` 与 ``baseline/``），不需要也不应该动 ``runs/``。
        """
        task = self.app.tasks.get(task_id)
        project = self.app.projects.get(task["project_id"])
        results: list[dict[str, Any]] = []
        for agent_id in agent_ids:
            self.app.tasks.set_status(task_id, TaskStatus.PENDING.value, f"扇出尝试：{agent_id}")
            global_limit = int(self.config.policy_get("concurrency.global", 6))
            per_project = int(self.config.policy_get("concurrency.per_project", global_limit))
            ttl = float(self.config.policy_get("concurrency.slot_ttl_seconds", 1800) or 1800)
            slot_id = self.slots.acquire(
                project_id=project["id"],
                task_id=task_id,
                global_limit=global_limit,
                per_project_limit=per_project,
                ttl_seconds=ttl,
            )
            if not slot_id:
                results.append(
                    {
                        "agent_id": agent_id,
                        "run_id": "",
                        "status": TaskStatus.BLOCKED.value,
                        "acceptance": {},
                        "changes": [],
                        "error": "并发上限已满，跳过该 Agent 尝试",
                    }
                )
                continue
            try:
                outcome = self.app.runner.run(task_id, agent_id=agent_id)
            finally:
                self.slots.release(slot_id)
            results.append(
                {
                    "agent_id": agent_id,
                    "run_id": outcome.run_id,
                    "status": outcome.status,
                    "acceptance": outcome.acceptance,
                    "changes": [c["rel_path"] for c in outcome.changes],
                    "error": outcome.error,
                }
            )
        best = next((r for r in results if r["status"] == TaskStatus.SUCCESS.value), None)
        self.app.tasks.update(
            task_id,
            result={"fanout": results, "best_agent": (best or {}).get("agent_id"), "history": task.get("result")},
        )
        self.app.tasks.set_status(
            task_id,
            TaskStatus.SUCCESS.value if best else TaskStatus.FAILED.value,
            None if best else "所有 Agent 尝试均未通过验收",
        )
        return {"task_id": task_id, "results": results, "best_agent": (best or {}).get("agent_id")}

    # ------------------------------------------------------------------ 人工
    def retry(self, task_id: str) -> dict[str, Any]:
        task = self.app.tasks.get(task_id)
        blocked_reason = str(task.get("blocked_reason") or "")
        if task["status"] == TaskStatus.BLOCKED.value and blocked_reason.startswith("merge_"):
            raise MergeRetryBlocked(
                task_id,
                blocked_reason,
                f"任务 {task_id} 因合并问题被阻塞（{blocked_reason}），不能直接重试",
            )
        self.app.tasks.set_status(task_id, TaskStatus.PENDING.value, "人工要求重试")
        return self.app.tasks.get(task_id)

    def skip(self, task_id: str, reason: str = "人工跳过") -> dict[str, Any]:
        return self.app.tasks.set_status(task_id, TaskStatus.SKIPPED.value, reason)

    def approve(self, task_id: str, *, note: str = "人工验收通过", sync: bool = False) -> dict[str, Any]:
        """人工验收：把失败/中断/取消的任务标记为成功，并可选同步工作副本。

        与「重试」不同，人工验收不伪造 Agent 成功：原始 run 的 exit_code、错误与
        验收明细全部保留，只在 result.acceptance 上打 ``human_override`` 标记。
        """
        task = self.app.tasks.get(task_id)
        if task["status"] == TaskStatus.RUNNING.value:
            raise ValueError("任务仍在执行，不能人工验收；请先取消或等待结束")
        if task["status"] == TaskStatus.PENDING.value:
            raise ValueError("任务尚未执行，没有可验收的现场")
        project = self.app.projects.get(task["project_id"])
        synced = None
        if sync:
            from ..api.taskworkspace import sync_task_workspace

            synced = sync_task_workspace(self.app, task, project, dry_run=False)
        now = _now()
        result = dict(task.get("result") or {})
        acceptance = dict(result.get("acceptance") or {})
        acceptance["original_ok"] = acceptance.get("ok")
        acceptance["original_summary"] = acceptance.get("summary")
        acceptance["ok"] = True
        original_summary = acceptance.get("original_summary")
        acceptance["summary"] = (
            f"人工验收通过（原：{original_summary}）" if original_summary else "人工验收通过"
        )
        acceptance["human_override"] = True
        acceptance["override_note"] = note
        acceptance["approved_at"] = now
        result["acceptance"] = acceptance
        result["human_approved"] = True
        result["approved_at"] = now
        result["approved_by"] = "human"
        if synced is not None:
            result["manual_sync"] = synced
        self.app.tasks.update(task_id, result=result, last_error=None)
        updated = self.app.tasks.set_status(task_id, TaskStatus.SUCCESS.value, note, force=True)
        self.app.board.append_event(
            project_id=project["id"],
            kind="task.approved",
            summary=f"人工验收通过：{task['title']}",
            actor="human",
            task_id=task_id,
            data={"note": note, "sync": synced},
        )
        return {"task": updated, "sync": synced}

    def _running_run_ids(self, project_id: str | None = None, task_id: str | None = None) -> list[str]:
        """范围内「在跑」的 run id：DB 里 status='running' 的，外加内存注册表里的窗口期。"""
        clauses = ["status = 'running'"]
        params: list[Any] = []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if task_id:
            clauses.append("task_id = ?")
            params.append(task_id)
        run_ids = {row["id"] for row in self.db.query(f"SELECT id FROM runs WHERE {' AND '.join(clauses)}", params)}

        active = self.run_cancels.active_run_ids()
        if active:
            scope = []
            scope_params: list[Any] = []
            if project_id:
                scope.append("project_id = ?")
                scope_params.append(project_id)
            if task_id:
                scope.append("task_id = ?")
                scope_params.append(task_id)
            if scope:
                for row in self.db.query(f"SELECT id FROM runs WHERE {' AND '.join(scope)}", scope_params):
                    if row["id"] in active:
                        run_ids.add(row["id"])
            else:
                run_ids |= active
        return sorted(run_ids)

    def _signal_cancel(self, run_ids: list[str], reason: str = "人工取消") -> int:
        """向在跑调用发终止信号，返回实际命中的 run 数。

        同进程：set 内存里的 ``threading.Event``；
        跨进程（CLI cancel / 另一个 API 实例 / 桌面壳）：在 run 目录写
        ``cancel.requested`` 哨兵，适配器每 200ms 轮询一次。
        只依赖 DB 里的 ``prompt_path``，不要求取消方与执行方共享内存。
        """
        signalled: set[str] = set()
        for run_id in run_ids:
            if self.run_cancels.cancel(run_id):
                signalled.add(run_id)
            row = self.db.query_one("SELECT prompt_path FROM runs WHERE id = ?", (run_id,))
            prompt_path = row["prompt_path"] if row else None
            if not prompt_path:
                continue
            try:
                run_dir = Path(prompt_path).parent
                run_dir.mkdir(parents=True, exist_ok=True)
                (run_dir / "cancel.requested").write_text(reason, encoding="utf-8")
                signalled.add(run_id)
            except OSError:
                continue
        return len(signalled)

    def cancel(self, task_id: str, reason: str = "人工取消") -> dict[str, Any]:
        """人工取消（需求 54：可终止执行）。

        先 set 该任务在跑 run 的取消信号（适配器据此整树回收子进程），再走状态机把任务置
        ``cancelled``。即便信号晚于状态写入也安全：runner 收尾时凭证已失效 → 不回写状态、
        不回收集改动，最终态保持 cancelled。
        """
        cancelled_runs = self._signal_cancel(self._running_run_ids(task_id=task_id), reason)
        result = self.app.tasks.set_status(task_id, TaskStatus.CANCELLED.value, reason)
        self.app.board.append_event(
            project_id=result.get("project_id"),
            kind="task.cancel.requested",
            summary=f"人工取消：已向 {cancelled_runs} 个在跑 run 发送终止信号",
            actor="human",
            task_id=task_id,
        )
        return result

    def stop_project(self, project_id: str, reason: str = "项目停止") -> dict[str, Any]:
        """项目级停止：**只影响该项目**（需求 10 项目隔离 / 需求 54 终止执行）。

        - 只置该项目的「不再派发」标志，不影响其他项目的调度循环；
        - 只向**该项目**在跑的 run 发终止信号（不碰其他项目的 Agent 进程）；
        - 把该项目在跑的任务置 cancelled；
        - **不停心跳**：本进程可能还在跑别的项目的任务，停心跳会让对端把本进程误判为
          已死（跨 host 走心跳 staleness）从而抢跑/中断别的项目的任务。
        """
        self._request_stop(project_id)
        run_ids = self._running_run_ids(project_id=project_id)
        cancelled_runs = self._signal_cancel(run_ids, reason)
        # 只取消「此刻确实在跑」的任务。历史成功/失败任务绝不能被一次停止操作改写：
        # 旧实现把该项目所有出现过 run 的任务都置 cancelled，等于把已完成的项目历史清空。
        task_ids = {
            t["id"] for t in self.app.tasks.list(project_id, status=TaskStatus.RUNNING.value)
        }
        task_ids.update(
            row["task_id"]
            for row in self.db.query(
                "SELECT DISTINCT task_id FROM runs WHERE project_id = ? AND status = 'running' AND task_id IS NOT NULL",
                (project_id,),
            )
        )
        cancelled_tasks = 0
        for task_id in sorted(task_ids):
            try:
                self.app.tasks.set_status(task_id, TaskStatus.CANCELLED.value, reason)
            except InvalidTransition:
                continue  # 已经是终态（例如刚被收尾）→ 跳过
            cancelled_tasks += 1
        return {
            "project_id": project_id,
            "stopped": True,
            "runs_cancelled": cancelled_runs,
            "tasks_cancelled": cancelled_tasks,
        }

    def takeover(self, task_id: str, agent_id: str = "human") -> dict[str, Any]:
        """人工接管：把任务改为等待人工执行（需求 16 / 54 / 57）。"""
        self.app.tasks.update(task_id, agent_id=agent_id)
        self.app.tasks.set_status(task_id, TaskStatus.WAITING.value, "人工接管")
        return self.app.tasks.get(task_id)

    def provide(self, task_id: str, *, did: str = "", artifacts: list[str] | None = None, notes: str = "") -> dict[str, Any]:
        """人工执行者回填结果并完成任务（需求 57）。"""
        task = self.app.tasks.get(task_id)
        project = self.app.projects.get(task["project_id"])
        root = self.app.isolation.prepare(project, task)
        handoff = self.app.handoffs.create(
            project_id=project["id"],
            task_id=task_id,
            run_id=None,
            agent_id="human",
            data={"did": did or "人工完成", "artifacts": artifacts or [], "notes": notes},
            source="human",
        )
        run_id = None
        self.db.insert(
            "runs",
            {
                "id": _new_run_id(),
                "project_id": project["id"],
                "task_id": task_id,
                "agent_id": "human",
                "agent_label": "人工",
                "protocol": "human",
                "role": task.get("role"),
                "attempt": int(task["attempt"]) + 1,
                "status": "success",
                "exit_code": 0,
                "started_at": _now(),
                "finished_at": _now(),
                "duration_ms": 0,
                "cost_tokens": 0,
                "cost_amount": 0.0,
                "cwd": str(root),
                "error": None,
            },
        )
        self.app.tasks.update(task_id, result={"handoff_id": handoff["id"], "source": "human"})
        self.app.tasks.set_status(task_id, TaskStatus.SUCCESS.value, "人工执行完成")
        approval = self.db.query_one(
            "SELECT id FROM approvals WHERE task_id = ? AND kind = 'manual_confirm' AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        if approval:
            self.app.guard.resolve(approval["id"], approved=True, by="human", resolution="人工完成", board=self.app.board)
        return self.app.tasks.get(task_id)

    def shutdown(self) -> None:
        """全局急停：**仅进程退出路径**（``Workbench.close()``）使用。

        语义边界（避免误用）：
        - 置**全局**「不再派发」标志，并只向**本进程持有**的在跑 run 发终止信号
          （跨项目，故意的：进程都要退了，不能让本进程拉起的 Agent 继续跑）；
        - **绝不**根据 DB 里 ``status='running'`` 去杀其他进程的 run：CLI/API/桌面
          各自是独立进程，任何一个短命命令退出都不该误杀另一个进程正在执行的任务；
        - 停心跳线程（本进程不再执行任何任务）；
        - **项目级**停止请用 :meth:`stop_project`（只影响该项目，需求 10）；
        - 之后再调用 ``run_project(pid)`` 会清掉该项目的停止标志（resume 语义）。
        """
        self._request_stop(None)
        self._signal_cancel(sorted(self.run_cancels.active_run_ids()), "进程退出")
        try:
            self.slots.release_session()
        except Exception:  # noqa: BLE001 - 退出路径不能因槽位清理失败而卡住
            pass
        if self._heartbeat is not None:
            self._heartbeat.stop()
            self._heartbeat = None


def _now() -> str:
    from ..util import now_iso

    return now_iso()


def _new_run_id() -> str:
    from ..util import new_id

    return new_id("run")
