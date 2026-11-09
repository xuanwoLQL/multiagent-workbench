"""编排 / 持久化 / 配额的并发正确性与崩溃恢复回归（task-3）。

纪律：本文件中的每条用例都对应一个「先复现失败 → 再修复 → 回归通过」的真实缺陷。

覆盖：
- SQLite：事务原子性、写锁、跨连接并发写、busy_timeout + 锁重试
- 重复派发：同一任务被并发执行（缺 claim/lease）
- 状态机：非法跃迁护栏
- 崩溃恢复：recover 的存活判定（本进程 / 活着的对端进程 / 已死进程 / 孤儿 running）
- 现场保留：续跑前把上次中断的工作副本归档，run 目录不丢
- 配额：check-then-act 超发 vs 原子 reserve
- 并发上限：global / per_project 在多调用方并发下真生效
- 重试：max_attempts、last_error 留存、退避
- 依赖模式：success 严格、always/any 不阻塞
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from multiagent.agents.base import AgentResult
from multiagent.db import Database
from multiagent.tasks.service import InvalidTransition, TaskStatus
from multiagent.util import now_iso


# --------------------------------------------------------------------------- 测试替身


class _ConcurrencyTracker:
    """观测「同时在执行」的任务数（并发上限用例用）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.entered = 0
        self.started_at: list[float] = []
        self.finished_at: list[float] = []

    def enter(self) -> None:
        with self._lock:
            self.active += 1
            self.entered += 1
            self.peak = max(self.peak, self.active)
            self.started_at.append(time.monotonic())

    def exit(self) -> None:
        with self._lock:
            self.active -= 1
            self.finished_at.append(time.monotonic())


class _FakeAdapter:
    """进程内假适配器：不拉真实子进程，便于精确观测并发与耗时。"""

    def __init__(
        self,
        agent_id: str = "worker",
        *,
        delay: float = 0.05,
        files: dict[str, str] | None = None,
        exit_code: int = 0,
        error: str | None = None,
        tracker: _ConcurrencyTracker | None = None,
        on_run=None,
        reported_duration_ms: int | None = None,
        exit_sequence: list[int] | None = None,
        await_cancel: bool = False,
        partial_file: str | None = None,
    ) -> None:
        self.id = agent_id
        self.label = agent_id
        self.protocol = "fake"
        self.delay = delay
        self.files = files or {}
        self.exit_code = exit_code
        self.error = error
        self.reported_duration_ms = reported_duration_ms
        self.exit_sequence = exit_sequence
        self.await_cancel = await_cancel
        self.partial_file = partial_file
        self.tracker = tracker
        self.on_run = on_run
        self.run_count = 0
        self.requests: list = []
        self._lock = threading.Lock()

    def describe(self) -> dict:
        return {"id": self.id, "protocol": "fake"}

    def check(self, timeout: float = 20.0) -> dict:
        return {"ok": True}

    def run(self, request):
        with self._lock:
            self.run_count += 1
            index = self.run_count - 1
            self.requests.append(request)
        code = self.exit_code
        if self.exit_sequence:
            code = self.exit_sequence[min(index, len(self.exit_sequence) - 1)]
        started = time.monotonic()
        if self.tracker:
            self.tracker.enter()
        try:
            if self.on_run:
                self.on_run(request)
            if self.partial_file:
                # 模拟「已经写了一部分东西」的现场
                partial = Path(request.workspace) / self.partial_file
                partial.parent.mkdir(parents=True, exist_ok=True)
                partial.write_text("半成品", encoding="utf-8")
            if self.await_cancel:
                event = getattr(request, "cancel_event", None)
                if event is None:
                    raise AssertionError("AgentRequest 缺少 cancel_event 字段（task-6 契约）")
                cancel_path = getattr(request, "cancel_path", None)
                # 模拟「Agent 已经干了一小会儿活」——真实场景里取消总是发生在执行中段
                time.sleep(0.05)
                deadline = time.monotonic() + (self.delay or 20.0)
                while time.monotonic() < deadline:
                    if event.is_set() or (cancel_path and Path(cancel_path).exists()):
                        # 真实适配器在此整树回收子进程并返回 CANCELLED_EXIT(-15)
                        return AgentResult(
                            ok=False,
                            exit_code=-15,
                            duration_ms=int((time.monotonic() - started) * 1000),
                            stdout="",
                            error="人工取消（fake adapter 已终止）",
                        )
                    time.sleep(0.02)
            elif self.delay:
                time.sleep(self.delay)
            for rel, content in self.files.items():
                target = Path(request.workspace) / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            return AgentResult(
                ok=code == 0,
                exit_code=code,
                duration_ms=(
                    self.reported_duration_ms
                    if self.reported_duration_ms is not None
                    else int(self.delay * 1000)
                ),
                stdout="fake-agent done",
                error=None if code == 0 else (self.error or "fake-agent failed"),
            )
        finally:
            if self.tracker:
                self.tracker.exit()


def _install(wb, agent_id: str, **kwargs) -> _FakeAdapter:
    adapter = _FakeAdapter(agent_id=agent_id, **kwargs)
    wb.runner._adapters[agent_id] = adapter
    return adapter


def _add_run(wb, *, run_id: str, project_id: str, task_id: str, agent_id: str = "worker",
             status: str = "running", owner_session: str | None = None, cwd: str | None = None) -> None:
    wb.db.insert(
        "runs",
        {
            "id": run_id,
            "project_id": project_id,
            "task_id": task_id,
            "agent_id": agent_id,
            "status": status,
            "started_at": now_iso(),
            "cwd": cwd,
            "owner_session": owner_session,
        },
    )


def _runs_for(wb, task_id: str) -> list[dict]:
    return [dict(r) for r in wb.db.query("SELECT * FROM runs WHERE task_id = ? ORDER BY started_at", (task_id,))]


def _make_project(wb, name: str) -> dict:
    return wb.projects.create(name)


# --------------------------------------------------------------------------- 1. SQLite 并发


def test_transaction_rollback_is_atomic(tmp_path: Path) -> None:
    """`with db.transaction():` 中途抛错必须回滚块内所有语句。"""
    db = Database(tmp_path / "tx.db")
    try:
        db.insert("meta", {"key": "before", "value": "1"})
        with pytest.raises(RuntimeError):
            with db.transaction():
                db.insert("meta", {"key": "in-tx-1", "value": "2"})
                db.insert("meta", {"key": "in-tx-2", "value": "3"})
                raise RuntimeError("boom")
        keys = {r["key"] for r in db.query("SELECT key FROM meta")}
        assert keys == {"before"}, f"事务未回滚，残留：{keys}"
    finally:
        db.close()


def test_transaction_commit_persists(tmp_path: Path) -> None:
    db = Database(tmp_path / "tx2.db")
    try:
        with db.transaction():
            db.insert("meta", {"key": "a", "value": "1"})
            db.insert("meta", {"key": "b", "value": "2"})
        assert {r["key"] for r in db.query("SELECT key FROM meta")} == {"a", "b"}
    finally:
        db.close()


def test_transaction_holds_write_lock_for_whole_block(tmp_path: Path) -> None:
    """事务期间必须独占写锁（BEGIN IMMEDIATE），否则提交语义无意义。"""
    path = tmp_path / "lock.db"
    db = Database(path)
    try:
        db.insert("meta", {"key": "seed", "value": "1"})
        with db.transaction():
            db.insert("meta", {"key": "in-tx", "value": "2"})
            peer = sqlite3.connect(str(path), timeout=0.2)
            try:
                with pytest.raises(sqlite3.OperationalError):
                    peer.execute("INSERT INTO meta (key, value) VALUES ('peer', '3')")
            finally:
                peer.close()
        assert {r["key"] for r in db.query("SELECT key FROM meta")} == {"seed", "in-tx"}
    finally:
        db.close()


def test_write_survives_transient_peer_write_lock(tmp_path: Path) -> None:
    """对端短暂持锁（> busy_timeout）时，写操作必须重试成功，而不是把锁冲突抛成任务失败。"""
    path = tmp_path / "busy.db"
    db = Database(path)
    db.conn.execute("PRAGMA busy_timeout=50")  # 故意压短，模拟极端争抢
    try:
        db.insert("meta", {"key": "seed", "value": "1"})
        peer = sqlite3.connect(str(path), check_same_thread=False)
        peer.execute("PRAGMA journal_mode=WAL")
        peer.execute("BEGIN IMMEDIATE")
        peer.execute("INSERT INTO meta (key, value) VALUES ('peer', '1')")

        def release() -> None:
            time.sleep(0.4)
            peer.commit()
            peer.close()

        worker = threading.Thread(target=release)
        worker.start()
        try:
            db.insert("meta", {"key": "after", "value": "2"})
        finally:
            worker.join()
        keys = {r["key"] for r in db.query("SELECT key FROM meta")}
        assert keys == {"seed", "after", "peer"}, keys
    finally:
        db.close()


def test_concurrent_writers_two_connections_no_lost_writes(tmp_path: Path) -> None:
    """两个连接（模拟两个进程）并发写同一库：不丢写、不报错。"""
    path = tmp_path / "concurrent.db"
    a = Database(path)
    b = Database(path)
    try:
        n = 40

        def spam(db: Database, tag: str) -> None:
            for i in range(n):
                db.insert("meta", {"key": f"{tag}-{i}", "value": "x"})

        threads = [
            threading.Thread(target=spam, args=(a, "a")),
            threading.Thread(target=spam, args=(b, "b")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        total = a.query_one("SELECT COUNT(*) AS c FROM meta")["c"]
        assert total == 2 * n, f"丢写：{total} != {2 * n}"
    finally:
        a.close()
        b.close()


# --------------------------------------------------------------------------- 2. 重复派发


def test_run_task_concurrently_executes_once(wb, project) -> None:
    """同一任务被两个线程同时 run_task：只能真正执行一次。"""
    adapter = _install(wb, "worker", delay=0.3, files={"out.txt": "1"})
    task = wb.tasks.create(
        project["id"], "并发单任务", kind="generic", agent_id="worker", acceptance=[{"type": "always"}]
    )
    barrier = threading.Barrier(2)
    results: list = []

    def go() -> None:
        barrier.wait()
        results.append(wb.engine.run_task(task["id"]))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    runs = _runs_for(wb, task["id"])
    assert len(runs) == 1, f"任务被重复派发，产生了 {len(runs)} 条 run"
    assert adapter.run_count == 1, f"适配器被调用 {adapter.run_count} 次"
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.SUCCESS.value


def test_concurrent_run_project_no_duplicate_dispatch(wb, project) -> None:
    """两个调用方并发 run_project：每个任务恰好执行一次，且不会把在跑的任务误判中断。"""
    adapter = _install(wb, "worker", delay=0.15, files=None)
    tasks = [
        wb.tasks.create(
            project["id"], f"并发{i}", kind="generic", agent_id="worker", acceptance=[{"type": "always"}]
        )
        for i in range(10)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(wb.engine.run_project, project["id"], ) for _ in range(2)]
        for f in futures:
            f.result()

    runs = [dict(r) for r in wb.db.query("SELECT task_id, COUNT(*) AS c FROM runs GROUP BY task_id")]
    assert len(runs) == len(tasks), f"只有 {len(runs)} 个任务产生了 run"
    assert all(r["c"] == 1 for r in runs), f"存在重复派发：{[(r['task_id'], r['c']) for r in runs if r['c'] > 1]}"
    assert adapter.run_count == len(tasks), f"适配器被调用 {adapter.run_count} 次 != {len(tasks)}"
    statuses = {wb.tasks.get(t["id"])["status"] for t in tasks}
    assert statuses == {TaskStatus.SUCCESS.value}, statuses


def test_task_claim_is_exclusive_under_threads(wb, project) -> None:
    """claim 必须原子：32 个线程抢同一个任务，只有一个拿到 token。"""
    task = wb.tasks.create(project["id"], "抢占", kind="generic", agent_id="worker")
    workers = 32
    barrier = threading.Barrier(workers)
    tokens: list[str | None] = []
    lock = threading.Lock()

    def go() -> None:
        barrier.wait()
        token = wb.tasks.claim(task["id"])
        with lock:
            tokens.append(token)

    threads = [threading.Thread(target=go) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [t for t in tokens if t]
    assert len(winners) == 1, f"{len(winners)} 个线程同时认领成功"
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.RUNNING.value
    assert tokens.count(None) == workers - 1


def test_success_task_is_not_reexecuted(wb, project) -> None:
    """已完成（success）的任务不能被再次执行。"""
    adapter = _install(wb, "worker", delay=0.0, files={"a.txt": "1"})
    task = wb.tasks.create(
        project["id"], "只跑一次", kind="generic", agent_id="worker", acceptance=[{"type": "always"}]
    )
    assert wb.engine.run_task(task["id"]).status == TaskStatus.SUCCESS.value
    before = len(_runs_for(wb, task["id"]))
    outcome = wb.engine.run_task(task["id"])
    assert len(_runs_for(wb, task["id"])) == before, "成功态任务被重复执行"
    assert adapter.run_count == 1
    assert outcome.status == TaskStatus.SUCCESS.value
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.SUCCESS.value


def test_cancel_during_run_is_not_overwritten(wb, project) -> None:
    """执行中被取消：最终状态保持 cancelled，且改动不回收到工作区。"""
    started = threading.Event()
    adapter = _install(
        wb, "worker", delay=0.6, files={"cancel.txt": "1"}, on_run=lambda request: started.set()
    )
    root = Path(project["root_path"])
    task = wb.tasks.create(
        project["id"], "取消在跑任务", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["cancel.txt"],
    )
    outcome: dict = {}
    thread = threading.Thread(target=lambda: outcome.update(result=wb.engine.run_task(task["id"])))
    thread.start()
    assert started.wait(5), "假适配器未启动"
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.RUNNING.value
    wb.engine.cancel(task["id"])
    thread.join(10)

    assert wb.tasks.get(task["id"])["status"] == TaskStatus.CANCELLED.value, "取消被运行结果覆盖"
    assert not (root / "cancel.txt").exists(), "已取消的任务仍把改动回收到工作区"
    assert adapter.run_count == 1


def test_cross_process_cancel_uses_sentinel_file(wb, project) -> None:
    """另一个进程（CLI/API）取消时无法 set 本进程 Event，必须靠哨兵文件生效。"""
    from multiagent.app import Workbench

    started = threading.Event()
    _install(
        wb, "worker", delay=20, await_cancel=True, files={"cross.txt": "1"},
        on_run=lambda request: started.set(),
    )
    root = Path(project["root_path"])
    task = wb.tasks.create(
        project["id"], "跨进程取消", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["cross.txt"],
    )
    thread = threading.Thread(target=lambda: wb.engine.run_task(task["id"]))
    thread.start()
    assert started.wait(5), "假适配器未启动"

    other = Workbench.open(wb.config.home)
    try:
        other.engine.cancel(task["id"])
        run = wb.db.query_one(
            "SELECT prompt_path FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
            (task["id"],),
        )
        assert run and run["prompt_path"]
        sentinel = Path(run["prompt_path"]).parent / "cancel.requested"
        assert sentinel.exists(), "跨进程取消没有写入哨兵文件"
    finally:
        other.close()

    thread.join(10)
    assert not thread.is_alive(), "跨进程取消后 Agent 仍在运行"
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.CANCELLED.value
    assert not (root / "cross.txt").exists(), "已取消的任务仍把改动回收到工作区"


def test_closing_another_workbench_does_not_cancel_running_task(wb, project) -> None:
    """回归：短命 CLI 命令打开同一 home 后 close()，不能误杀别的进程在跑的任务。

    `Engine.shutdown()` 曾经按 DB 的 ``status='running'`` 发跨进程哨兵；
    任何一个 `handoff list` / `task status` 退出都会把正在执行的 Agent 杀掉。
    """
    from multiagent.app import Workbench

    started = threading.Event()
    _install(wb, "worker", delay=20, await_cancel=True, on_run=lambda request: started.set())
    task = wb.tasks.create(
        project["id"], "不应被旁观者误杀", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}],
    )
    thread = threading.Thread(target=lambda: wb.engine.run_task(task["id"]))
    thread.start()
    assert started.wait(5), "假适配器未启动"
    run = wb.db.query_one(
        "SELECT prompt_path FROM runs WHERE task_id = ? ORDER BY started_at DESC LIMIT 1",
        (task["id"],),
    )
    sentinel = Path(run["prompt_path"]).parent / "cancel.requested"

    other = Workbench.open(wb.config.home)
    other.close()
    time.sleep(0.3)

    assert wb.tasks.get(task["id"])["status"] == TaskStatus.RUNNING.value, "旁观者 close() 误杀了在跑任务"
    assert not sentinel.exists(), "旁观者 close() 写入了取消哨兵"

    wb.engine.cancel(task["id"])
    thread.join(10)
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.CANCELLED.value


# --------------------------------------------------------------------------- 3. 状态机


def test_illegal_transitions_rejected(wb, project) -> None:
    done = wb.tasks.create(project["id"], "已完成", kind="generic", agent_id="worker")
    wb.tasks.set_status(done["id"], TaskStatus.SUCCESS.value)
    with pytest.raises(InvalidTransition):
        wb.tasks.set_status(done["id"], TaskStatus.RUNNING.value)

    interrupted = wb.tasks.create(project["id"], "中断", kind="generic", agent_id="worker")
    wb.tasks.set_status(interrupted["id"], TaskStatus.RUNNING.value)
    wb.tasks.set_status(interrupted["id"], TaskStatus.INTERRUPTED.value)
    with pytest.raises(InvalidTransition):
        wb.tasks.set_status(interrupted["id"], TaskStatus.SUCCESS.value)

    running = wb.tasks.create(project["id"], "在跑", kind="generic", agent_id="worker")
    wb.tasks.set_status(running["id"], TaskStatus.RUNNING.value)
    with pytest.raises(InvalidTransition):
        wb.tasks.set_status(running["id"], TaskStatus.PENDING.value)


def test_cancelled_is_terminal_for_retry_and_skip(wb, project) -> None:
    task = wb.tasks.create(project["id"], "已取消", kind="generic", agent_id="worker")
    wb.engine.cancel(task["id"])
    with pytest.raises(InvalidTransition):
        wb.engine.retry(task["id"])
    with pytest.raises(InvalidTransition):
        wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.CANCELLED.value


def test_legal_human_flows_still_work(wb, project) -> None:
    """护栏不能误伤既有合法路径：success→pending（人工回滚后重跑）、skipped→pending（retry）。"""
    task = wb.tasks.create(project["id"], "T", kind="generic", agent_id="worker")
    wb.tasks.set_status(task["id"], TaskStatus.SUCCESS.value)
    wb.tasks.set_status(task["id"], TaskStatus.PENDING.value, "人工回滚改动")
    wb.engine.skip(task["id"])
    wb.engine.retry(task["id"])
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.PENDING.value


# --------------------------------------------------------------------------- 4. 崩溃恢复


def _dead_pid() -> int:
    """确定性的「已死进程」PID：起一个立刻退出的子进程并 wait。"""
    import subprocess
    import sys as _sys

    proc = subprocess.Popen([_sys.executable, "-c", "pass"])
    proc.wait(timeout=30)
    return int(proc.pid)


def _add_session(wb, session_id: str, *, pid: int, host: str, heartbeat_at: str) -> None:
    wb.db.insert(
        "runtime_sessions",
        {"session_id": session_id, "pid": pid, "host": host, "started_at": heartbeat_at, "heartbeat_at": heartbeat_at},
    )


@pytest.mark.skipif(os.name != "nt", reason="本机 PID 存活语义是 Windows 专属")
def test_recover_interrupts_dead_local_pid_immediately(wb, project) -> None:
    """同机会话 PID 已死：心跳再新鲜也必须**立刻**回收，且任务可 resume。"""
    adapter = _install(wb, "worker", delay=0.0, files={"resume.txt": "ok"})
    task = wb.tasks.create(
        project["id"], "崩溃后重启", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["resume.txt"],
    )
    wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)
    _add_session(wb, "session-crashed", pid=_dead_pid(), host=wb.db.host, heartbeat_at=now_iso())
    _add_run(wb, run_id="run-crashed", project_id=project["id"], task_id=task["id"], owner_session="session-crashed")

    result = wb.engine.recover(project["id"])
    assert result["interrupted"] == 1, result
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.INTERRUPTED.value
    assert _runs_for(wb, task["id"])[0]["status"] == "interrupted"

    summary = wb.engine.run_project(project["id"])  # 立刻可续跑
    assert summary["success"] == 1, summary
    assert adapter.run_count == 1


@pytest.mark.skipif(os.name != "nt", reason="本机 PID 存活语义是 Windows 专属")
def test_recover_keeps_running_task_of_live_local_pid(wb, project) -> None:
    """同机会话 PID 仍活：即使心跳早已过期也不能误杀（本机另一个调度方）。"""
    task = wb.tasks.create(project["id"], "对端在跑", kind="generic", agent_id="worker")
    wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)
    _add_session(wb, "peer-live", pid=os.getpid(), host=wb.db.host, heartbeat_at="2020-01-01T00:00:00Z")
    _add_run(wb, run_id="run-peer", project_id=project["id"], task_id=task["id"], owner_session="peer-live")

    result = wb.engine.recover(project["id"])
    assert result["interrupted"] == 0, result
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.RUNNING.value
    assert _runs_for(wb, task["id"])[0]["status"] == "running"


def test_recover_force_ignores_liveness(wb, project) -> None:
    """人工强制恢复：无视存活判定，把 running 任务与 run 行一并回收。"""
    task = wb.tasks.create(project["id"], "人工判定已死", kind="generic", agent_id="worker")
    wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)
    _add_session(wb, "peer-live", pid=os.getpid(), host=wb.db.host, heartbeat_at=now_iso())
    _add_run(wb, run_id="run-peer", project_id=project["id"], task_id=task["id"], owner_session="peer-live")

    assert wb.engine.recover(project["id"])["interrupted"] == 0  # 保守路径：不回收
    result = wb.engine.recover(project["id"], force=True)
    assert result["interrupted"] == 1, result
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.INTERRUPTED.value
    assert _runs_for(wb, task["id"])[0]["status"] == "interrupted"


def test_recover_cross_host_still_uses_heartbeat(wb, project) -> None:
    """跨 host 拿不到对端 PID：仍按心跳 staleness 判定（不能拿本机 PID 顶替）。"""
    remote_fresh = wb.tasks.create(project["id"], "远端心跳新鲜", kind="generic", agent_id="worker")
    wb.tasks.set_status(remote_fresh["id"], TaskStatus.RUNNING.value)
    _add_session(wb, "remote-fresh", pid=os.getpid(), host="another-host", heartbeat_at=now_iso())
    _add_run(wb, run_id="run-remote-fresh", project_id=project["id"], task_id=remote_fresh["id"], owner_session="remote-fresh")

    remote_stale = wb.tasks.create(project["id"], "远端心跳过期", kind="generic", agent_id="worker")
    wb.tasks.set_status(remote_stale["id"], TaskStatus.RUNNING.value)
    _add_session(wb, "remote-stale", pid=os.getpid(), host="another-host", heartbeat_at="2020-01-01T00:00:00Z")
    _add_run(wb, run_id="run-remote-stale", project_id=project["id"], task_id=remote_stale["id"], owner_session="remote-stale")

    result = wb.engine.recover(project["id"])
    assert result["interrupted"] == 1, result
    assert wb.tasks.get(remote_fresh["id"])["status"] == TaskStatus.RUNNING.value
    assert wb.tasks.get(remote_stale["id"])["status"] == TaskStatus.INTERRUPTED.value


def test_recover_keeps_running_task_of_live_peer_session(wb, project) -> None:
    """对端进程还活着（心跳新鲜）时的 running 任务不能被误标中断。"""
    task = wb.tasks.create(project["id"], "对端在跑", kind="generic", agent_id="worker")
    wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)
    _add_session(wb, "peer-live", pid=os.getpid(), host=wb.db.host, heartbeat_at=now_iso())
    _add_run(wb, run_id="run-peer", project_id=project["id"], task_id=task["id"], owner_session="peer-live")

    result = wb.engine.recover(project["id"])
    assert result["interrupted"] == 0, result
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.RUNNING.value
    assert _runs_for(wb, task["id"])[0]["status"] == "running"


def test_recover_keeps_live_run_in_own_session(wb, project) -> None:
    """本进程正在执行的任务，recover 绝不能标记中断（否则并发 run_project 会重复派发）。"""
    task = wb.tasks.create(project["id"], "本进程在跑", kind="generic", agent_id="worker")
    wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)
    _add_run(wb, run_id="run-mine", project_id=project["id"], task_id=task["id"], owner_session=wb.db.session_id)
    wb.db.runtime.mark_active("run-mine")

    result = wb.engine.recover(project["id"])
    assert result["interrupted"] == 0, result
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.RUNNING.value


def test_recover_interrupts_orphan_dead_and_stale(wb, project) -> None:
    orphan = wb.tasks.create(project["id"], "无 run 行的孤儿", kind="generic", agent_id="worker")
    wb.tasks.set_status(orphan["id"], TaskStatus.RUNNING.value)

    dead = wb.tasks.create(project["id"], "持有者已死", kind="generic", agent_id="worker")
    wb.tasks.set_status(dead["id"], TaskStatus.RUNNING.value)
    _add_run(wb, run_id="run-dead", project_id=project["id"], task_id=dead["id"], owner_session="session-gone")

    stale = wb.tasks.create(project["id"], "对端心跳过期", kind="generic", agent_id="worker")
    wb.tasks.set_status(stale["id"], TaskStatus.RUNNING.value)
    wb.db.insert(
        "runtime_sessions",
        {"session_id": "session-stale", "pid": 999, "started_at": "2020-01-01T00:00:00Z", "heartbeat_at": "2020-01-01T00:00:00Z"},
    )
    _add_run(wb, run_id="run-stale", project_id=project["id"], task_id=stale["id"], owner_session="session-stale")

    live = wb.tasks.create(project["id"], "本进程在跑", kind="generic", agent_id="worker")
    wb.tasks.set_status(live["id"], TaskStatus.RUNNING.value)
    _add_run(wb, run_id="run-live", project_id=project["id"], task_id=live["id"], owner_session=wb.db.session_id)
    wb.db.runtime.mark_active("run-live")

    result = wb.engine.recover(project["id"])
    assert result["interrupted"] == 3, result
    for tid in (orphan["id"], dead["id"], stale["id"]):
        assert wb.tasks.get(tid)["status"] == TaskStatus.INTERRUPTED.value
    assert wb.tasks.get(live["id"])["status"] == TaskStatus.RUNNING.value
    assert _runs_for(wb, dead["id"])[0]["status"] == "interrupted"
    assert _runs_for(wb, live["id"])[0]["status"] == "running"


def test_recover_keeps_inflight_claim_without_run_row(wb, project) -> None:
    """引擎先 claim、工作线程还没插 run 行的窗口期，不能被并发 recover 误判中断。"""
    mine = wb.tasks.create(project["id"], "已认领未开跑", kind="generic", agent_id="worker")
    assert wb.tasks.claim(mine["id"]) is not None  # claim_owner = 本进程会话，尚无 run 行

    dead = wb.tasks.create(project["id"], "死会话认领", kind="generic", agent_id="worker")
    assert wb.tasks.claim(dead["id"], owner="session-gone") is not None

    result = wb.engine.recover(project["id"])
    assert wb.tasks.get(mine["id"])["status"] == TaskStatus.RUNNING.value, result
    assert wb.tasks.get(dead["id"])["status"] == TaskStatus.INTERRUPTED.value, result
    assert result["interrupted"] == 1, result


def test_resume_preserves_interrupted_workspace_and_previous_run_dir(wb, project) -> None:
    """续跑：上次中断的现场（run 目录 + 工作副本半成品）必须保留，不能被静默清空。"""
    adapter = _install(wb, "worker", delay=0.0, files={"done.txt": "ok"})
    task = wb.tasks.create(
        project["id"], "续跑", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["done.txt"],
    )
    # 模拟上个进程崩在任务执行中：run 行 running + 工作副本里有半成品
    work = wb.isolation.workdir(project["id"], task["id"])
    work.mkdir(parents=True, exist_ok=True)
    (work / "partial.txt").write_text("半成品", encoding="utf-8")
    _add_run(
        wb, run_id="run-crashed", project_id=project["id"], task_id=task["id"],
        owner_session="session-dead", cwd=str(work),
    )
    wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)

    assert wb.engine.recover(project["id"])["interrupted"] == 1
    summary = wb.engine.run_project(project["id"])
    assert summary["success"] == 1, summary

    archived = wb.isolation.task_dir(project["id"], task["id"]) / "runs" / "run-crashed" / "workspace" / "partial.txt"
    assert archived.exists(), "上次中断的工作副本被静默清空，现场丢失"
    assert archived.read_text(encoding="utf-8") == "半成品"
    assert adapter.run_count == 1


# --------------------------------------------------------------------------- 5. 配额原子性


def test_quota_check_then_record_overissues_but_reserve_is_atomic(make_wb, cli_agent) -> None:
    """check-then-act 会超发；reserve 原子校验不会。"""
    wb = make_wb([cli_agent("worker", quota={"max_invocations": 5})])
    workers = 24

    # 旧路径：check 与 record 之间加栅栏，全部通过 → 超发（说明它不能用于准入）
    barrier = threading.Barrier(workers)

    def naive() -> None:
        allowed, _ = wb.quota.check("worker")
        barrier.wait()
        if allowed:
            wb.quota.record("worker")

    threads = [threading.Thread(target=naive) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wb.quota.usage("worker")["invocations"] == workers, "未能复现 check-then-act 超发"

    # 新路径：reserve 原子校验，最多 5 个成功
    wb.db.execute("DELETE FROM agent_usage")
    barrier2 = threading.Barrier(workers)
    outcomes: list[bool] = []
    lock = threading.Lock()

    def atomic() -> None:
        barrier2.wait()
        ok, _ = wb.quota.reserve("worker")
        with lock:
            outcomes.append(ok)

    threads = [threading.Thread(target=atomic) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for ok in outcomes if ok) == 5, f"原子配额被击穿：{sum(1 for ok in outcomes if ok)} 次放行"
    assert wb.quota.usage("worker")["invocations"] == 5


def test_engine_enforces_max_invocations_under_concurrency(make_wb, cli_agent, plan_writer) -> None:
    """并发调度下 max_invocations 不能被击穿。"""
    wb = make_wb([cli_agent("worker", quota={"max_invocations": 3})])
    project = _make_project(wb, "quota-conc")
    adapter = _install(wb, "worker", delay=0.2, files=None)
    for i in range(12):
        wb.tasks.create(project["id"], f"Q{i}", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])

    summary = wb.engine.run_project(project["id"], max_parallel=6)
    assert wb.quota.usage("worker")["invocations"] == 3, wb.quota.usage("worker")
    assert adapter.run_count == 3, f"实际调用 {adapter.run_count} 次，超过 max_invocations"
    assert summary["success"] == 3


# --------------------------------------------------------------------------- 6. 并发上限


def test_per_project_limit_across_concurrent_run_project(make_wb, cli_agent) -> None:
    wb = make_wb([cli_agent("worker")], policy_extra={"concurrency": {"global": 4, "per_project": 2}})
    project = _make_project(wb, "cap")
    tracker = _ConcurrencyTracker()
    _install(wb, "worker", delay=0.2, tracker=tracker)
    for i in range(8):
        wb.tasks.create(project["id"], f"C{i}", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(wb.engine.run_project, project["id"], ) for _ in range(2)]
        for f in futures:
            f.result()

    assert tracker.peak <= 2, f"per_project=2 被击穿，峰值并发 {tracker.peak}"
    assert wb.tasks.progress(project["id"])["counters"].get("success") == 8


def test_global_limit_across_projects(make_wb, cli_agent) -> None:
    wb = make_wb([cli_agent("worker")], policy_extra={"concurrency": {"global": 3, "per_project": 3}})
    tracker = _ConcurrencyTracker()
    _install(wb, "worker", delay=0.2, tracker=tracker)
    projects = [_make_project(wb, f"g{i}") for i in range(2)]
    for project in projects:
        for j in range(6):
            wb.tasks.create(project["id"], f"G{j}", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(wb.engine.run_project, p["id"], ) for p in projects]
        for f in futures:
            f.result()

    assert tracker.peak <= 3, f"global=3 被击穿，峰值并发 {tracker.peak}"


# --------------------------------------------------------------------------- 7. 重试 / 退避

def test_retry_records_last_error_and_respects_max_attempts(wb, project) -> None:
    adapter = _install(wb, "worker", delay=0.0, exit_code=1, error="假失败")
    task = wb.tasks.create(
        project["id"], "总是失败", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], max_attempts=2,
    )
    summary = wb.engine.run_project(project["id"])
    runs = _runs_for(wb, task["id"])
    assert len(runs) == 2, f"重试次数不符（max_attempts=2）：{len(runs)}"
    assert adapter.run_count == 2
    stored = wb.tasks.get(task["id"])
    assert stored["status"] == TaskStatus.FAILED.value
    assert stored["last_error"], "失败原因未留存（last_error 为空）"
    assert stored["attempt"] == 2
    assert summary["progress"]["counters"].get("failed") == 1


def test_retry_backoff_delays_next_attempt(make_wb, cli_agent) -> None:
    wb = make_wb([cli_agent("worker")], policy_extra={"retry": {"backoff_seconds": 0.3}})
    project = _make_project(wb, "backoff")
    tracker = _ConcurrencyTracker()
    _install(wb, "worker", delay=0.05, exit_code=1, tracker=tracker)
    wb.tasks.create(
        project["id"], "退避", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], max_attempts=2,
    )
    wb.engine.run_project(project["id"])
    assert len(tracker.started_at) == 2, tracker.started_at
    gap = tracker.started_at[1] - tracker.finished_at[0]
    assert gap >= 0.2, f"重试未退避（间隔 {gap:.3f}s）"


def test_run_records_wallclock_duration_and_quota_seconds(wb, project) -> None:
    """耗时必须以 runner 的墙钟实测为准，且配额 seconds 收到真实值。"""
    _install(wb, "worker", delay=0.25)
    task = wb.tasks.create(
        project["id"], "计时", kind="generic", agent_id="worker", acceptance=[{"type": "always"}]
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == TaskStatus.SUCCESS.value
    run = _runs_for(wb, task["id"])[0]
    assert run["duration_ms"] >= 200, f"runs.duration_ms 用了不可信的适配器上报值：{run['duration_ms']}"
    assert outcome.duration_ms >= 200
    assert wb.quota.usage("worker")["seconds"] >= 0.2, wb.quota.usage("worker")


def test_adapter_lying_about_duration_does_not_break_quota(wb, project) -> None:
    """适配器谎报 duration_ms=0（真实 ACP 就发生过）时，runner 仍必须记录真实耗时。"""
    _install(wb, "worker", delay=0.3, reported_duration_ms=0)
    task = wb.tasks.create(
        project["id"], "谎报耗时", kind="generic", agent_id="worker", acceptance=[{"type": "always"}]
    )
    assert wb.engine.run_task(task["id"]).status == TaskStatus.SUCCESS.value
    run = _runs_for(wb, task["id"])[0]
    assert run["duration_ms"] >= 250, run["duration_ms"]
    assert wb.quota.usage("worker")["seconds"] >= 0.25, wb.quota.usage("worker")


def test_quota_max_seconds_actually_triggers(make_wb, cli_agent) -> None:
    """max_seconds 必须真能触发（此前 seconds 恒为 0，上限形同虚设）。"""
    wb2 = make_wb([cli_agent("worker", quota={"max_seconds": 0.25})])
    project2 = _make_project(wb2, "maxsec")
    _install(wb2, "worker", delay=0.3)
    first = wb2.tasks.create(
        project2["id"], "第一次", kind="generic", agent_id="worker", acceptance=[{"type": "always"}]
    )
    second = wb2.tasks.create(
        project2["id"], "第二次", kind="generic", agent_id="worker", acceptance=[{"type": "always"}]
    )
    assert wb2.engine.run_task(first["id"], agent_id="worker").status == TaskStatus.SUCCESS.value
    assert wb2.quota.usage("worker")["seconds"] >= 0.25, wb2.quota.usage("worker")
    blocked = wb2.engine.run_task(second["id"], agent_id="worker")
    assert blocked.status == TaskStatus.BLOCKED.value, blocked
    assert "时长" in (blocked.error or ""), blocked.error


# --------------------------------------------------------------------------- 9. 证据留存（需求 43 / 46 / 50）


def test_fanout_preserves_every_attempt_evidence(make_wb, cli_agent) -> None:
    """扇出（同题多 Agent）后，每一次尝试的 prompt/stdout 都必须还在（需求 46 / 50）。"""
    wb = make_wb([cli_agent("good"), cli_agent("other")])
    project = _make_project(wb, "fanout")
    _install(wb, "good", delay=0.0, files={"out.txt": "来自 good"})
    _install(wb, "other", delay=0.0, files={"out.txt": "来自 other"})
    task = wb.tasks.create(
        project["id"], "扇出对比", kind="generic", acceptance=[{"type": "always"}], outputs=["out.txt"]
    )
    result = wb.engine.fanout(task["id"], ["good", "other"])
    assert len(result["results"]) == 2, result

    runs = _runs_for(wb, task["id"])
    assert len(runs) == 2
    for run in runs:
        assert run["status"] == "success", run
        for key in ("prompt_path", "stdout_path"):
            path = Path(run[key] or "")
            assert path.exists(), f"{run['agent_id']} 的 {key} 被扇出清掉了：{path}"
            assert path.read_text(encoding="utf-8").strip(), f"{run['agent_id']} 的 {key} 是空的"

    comparison = wb.audit.compare_runs(task["id"])
    assert len(comparison["runs"]) == 2
    files = {f["rel_path"]: f for f in comparison["files"]}
    assert "out.txt" in files, comparison["files"]
    per_run = files["out.txt"]["per_run"]
    assert len(per_run) == 2, per_run
    contents = set()
    for info in per_run.values():
        artifact_path = Path(info["abs_path"])
        assert artifact_path.exists(), f"产物证据文件不存在：{artifact_path}"
        contents.add(artifact_path.read_text(encoding="utf-8"))
    assert contents == {"来自 good", "来自 other"}, contents


def test_retry_keeps_previous_attempt_evidence(wb, project) -> None:
    """重试后，上一次尝试的 run 目录与工作副本现场仍然可读（需求 43）。"""
    adapter = _install(wb, "worker", delay=0.0, files={"out.txt": "第二次"}, exit_sequence=[1, 0])
    task = wb.tasks.create(
        project["id"], "失败后重试", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["out.txt"], max_attempts=2,
    )
    summary = wb.engine.run_project(project["id"])
    assert summary["success"] == 1, summary
    assert adapter.run_count == 2

    runs = _runs_for(wb, task["id"])
    assert len(runs) == 2 and [r["status"] for r in runs] == ["failed", "success"]
    first = runs[0]
    prompt = Path(first["prompt_path"])
    stdout = Path(first["stdout_path"])
    assert prompt.exists() and prompt.read_text(encoding="utf-8").strip(), "上一次尝试的 prompt 丢了"
    assert stdout.exists() and stdout.read_text(encoding="utf-8").strip(), "上一次尝试的 stdout 丢了"
    archived = Path(first["cwd"]) if first["cwd"] else None
    assert archived is not None
    # 第一次尝试的工作副本被归档到 runs/<run_id>/workspace
    archived_root = prompt.parent / "workspace"
    assert archived_root.is_dir(), f"未归档上一次尝试的工作副本：{archived_root}"
    artifact = [a for a in wb.tasks.artifacts(task_id=task["id"]) if a["run_id"] == first["id"]]
    if artifact:
        assert Path(artifact[0]["abs_path"]).exists(), "归档后产物指针没改指，证据读不到"


# --------------------------------------------------------------------------- 11. 每 Agent 超时（需求 3）


def test_agent_timeout_used_when_task_has_none(make_wb, cli_agent) -> None:
    """任务未显式指定超时时，必须用 agent 自己配置的超时（此前 agent 层完全没被读取）。"""
    wb = make_wb([cli_agent("worker", timeout_sec=7)])
    project = _make_project(wb, "t-agent")
    adapter = _install(wb, "worker", delay=0.0)
    task = wb.tasks.create(project["id"], "继承 agent 超时", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    assert task["timeout_sec"] is None, "创建任务时不应把 policy 默认值写死进任务"
    assert wb.runner.resolve_timeout(task, "worker") == 7
    assert wb.engine.run_task(task["id"], agent_id="worker").status == TaskStatus.SUCCESS.value
    assert adapter.requests[0].timeout_sec == 7


def test_task_timeout_overrides_agent_timeout(make_wb, cli_agent) -> None:
    wb = make_wb([cli_agent("worker", timeout_sec=7)])
    project = _make_project(wb, "t-task")
    adapter = _install(wb, "worker", delay=0.0)
    task = wb.tasks.create(
        project["id"], "任务显式超时", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], timeout_sec=3,
    )
    assert wb.runner.resolve_timeout(task, "worker") == 3
    wb.engine.run_task(task["id"], agent_id="worker")
    assert adapter.requests[0].timeout_sec == 3


def test_timeout_falls_back_to_policy_default(make_wb, cli_agent) -> None:
    wb = make_wb([cli_agent("worker")])  # agent 未配 timeout_sec
    project = _make_project(wb, "t-policy")
    task = wb.tasks.create(project["id"], "回退 policy", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    assert wb.runner.resolve_timeout(task, "worker") == wb.config.policy_get("default_timeout_sec")


def test_human_timeout_zero_means_unlimited(wb, project) -> None:
    task = wb.tasks.create(project["id"], "人工任务", template_id="human-step")
    assert wb.runner.resolve_timeout(task, "human") == 0, "human 的 timeout_sec=0 必须解释成不限时"
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == TaskStatus.WAITING.value  # 不会被「0 秒超时」打断


def test_agent_timeout_enforced_end_to_end(make_wb, cli_agent, plan_writer) -> None:
    """真实超时行为：agent 配 1s，假 Agent sleep 12s，超时必须是**硬上限**。

    收尾（回收进程树/关句柄）的成本是有界常量，不是倍率：断言 `elapsed < timeout + 1.5`
    （Lead 定的界，实测硬杀后 timeout=1 约 1.3s 停）。绝不允许「超时 10 分钟的任务再拖
    10 分钟」这种倍率语义。
    """
    timeout_sec = 1
    plan_writer("worker", {"sleep": 12})
    wb = make_wb([cli_agent("worker", timeout_sec=timeout_sec)])
    project = _make_project(wb, "t-e2e")
    task = wb.tasks.create(project["id"], "慢任务", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    started = time.monotonic()
    outcome = wb.engine.run_task(task["id"], agent_id="worker")
    elapsed = time.monotonic() - started
    assert outcome.exit_code == -9, f"未被超时终止：exit={outcome.exit_code}"
    assert outcome.status == TaskStatus.FAILED.value, outcome
    assert elapsed < timeout_sec + 1.5, (
        f"超时不是硬上限：timeout={timeout_sec}s，实际等了 {elapsed:.2f}s"
        f"（超出 timeout+1.5s 的收尾余量）"
    )
    run = _runs_for(wb, task["id"])[0]
    assert run["duration_ms"] > 0, run
    stderr = Path(run["stderr_path"]).read_text(encoding="utf-8", errors="replace")
    assert "timeout" in stderr.lower() or "超时" in stderr, stderr[-200:]



# --------------------------------------------------------------------------- 13. 终止执行（需求 54）


def test_cancel_stops_running_agent_and_keeps_workspace_clean(wb, project) -> None:
    """`cancel(task)` 必须真正终止在跑的 Agent：~1.5s 内停、终态 cancelled、不回收半成品。"""
    root = Path(project["root_path"])
    started = threading.Event()
    adapter = _install(
        wb, "worker", await_cancel=True, delay=20.0,
        partial_file="half.txt", files={"done.txt": "成品"},
    )
    adapter.on_run = lambda request: started.set()
    task = wb.tasks.create(
        project["id"], "长跑任务", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["done.txt"],
    )

    outcome: dict = {}
    thread = threading.Thread(target=lambda: outcome.update(result=wb.engine.run_task(task["id"], agent_id="worker")))
    clock = time.monotonic()
    thread.start()
    assert started.wait(5), "假 Agent 未启动"
    run_id = _runs_for(wb, task["id"])[0]["id"]
    assert wb.engine.run_cancels.active_run_ids() == {run_id}, "在跑 run 未登记进取消注册表"

    cancelled = wb.engine.cancel(task["id"])
    thread.join(5)
    elapsed = time.monotonic() - clock

    assert not thread.is_alive(), "取消后 Agent 调用没有被终止"
    assert elapsed < 1.5, f"取消没有在 ~1.5s 内生效：{elapsed:.2f}s"
    assert cancelled["status"] == TaskStatus.CANCELLED.value
    stored = wb.tasks.get(task["id"])
    assert stored["status"] == TaskStatus.CANCELLED.value, stored
    assert not (root / "done.txt").exists(), "被取消的任务不得把改动 commit_back"
    assert not (root / "half.txt").exists(), "半成品不得进工作区"
    # 现场保留在隔离副本 + run 目录（需求 43）
    assert (Path(stored["workdir"]) / "half.txt").exists(), "半成品现场应保留在隔离副本"
    run = _runs_for(wb, task["id"])[0]
    assert run["status"] == "cancelled", run
    assert run["duration_ms"] > 0
    assert wb.engine.run_cancels.active_run_ids() == set(), "run 结束后取消注册表应清空"


def test_stop_project_cancels_all_running_runs(wb, project) -> None:
    """`stop_project` 同时终止该项目多个在跑 run，并保持「不再派发新任务」。"""
    tracker = _ConcurrencyTracker()
    adapter = _install(wb, "worker", await_cancel=True, delay=20.0, tracker=tracker)
    # 夹具 policy: per_project=2；这里验证「停止会终止全部在跑 run」，
    # 因此只启动 2 个（第 3 个会被并发闸门挡住，属于另一条测试的职责）。
    tasks = [
        wb.tasks.create(
            project["id"], f"长跑{i}", kind="generic", agent_id="worker", acceptance=[{"type": "always"}]
        )
        for i in range(2)
    ]
    threads = [
        threading.Thread(target=lambda tid=t["id"]: wb.engine.run_task(tid, agent_id="worker"))
        for t in tasks
    ]
    clock = time.monotonic()
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while tracker.entered < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert tracker.entered == 2, f"只有 {tracker.entered} 个 run 起来"

    result = wb.engine.stop_project(project["id"])
    for thread in threads:
        thread.join(5)
    elapsed = time.monotonic() - clock

    assert result["runs_cancelled"] == 2, result
    assert result["tasks_cancelled"] == 2, result
    assert all(not t.is_alive() for t in threads), "stop_project 后仍有 Agent 在跑"
    assert elapsed < 1.5, f"stop_project 没有在 ~1.5s 内终止全部在跑 run：{elapsed:.2f}s"
    statuses = {wb.tasks.get(t["id"])["status"] for t in tasks}
    assert statuses == {TaskStatus.CANCELLED.value}, statuses
    assert all(r["status"] == "cancelled" for t in tasks for r in _runs_for(wb, t["id"]))
    assert wb.engine.run_cancels.active_run_ids() == set()


def test_cancel_does_not_affect_other_running_tasks(wb, project) -> None:
    """取消一个任务不能波及另一个在跑的任务。"""
    adapter = _install(wb, "worker", await_cancel=True, delay=1.0, files={"ok.txt": "1"})
    keep = wb.tasks.create(
        project["id"], "不要动我", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["ok.txt"],
    )
    doomed = wb.tasks.create(project["id"], "取消我", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])

    keep_outcome: dict = {}
    keep_thread = threading.Thread(target=lambda: keep_outcome.update(result=wb.engine.run_task(keep["id"], agent_id="worker")))
    doomed_outcome: dict = {}
    doomed_thread = threading.Thread(target=lambda: doomed_outcome.update(result=wb.engine.run_task(doomed["id"], agent_id="worker")))
    keep_thread.start()
    doomed_thread.start()

    deadline = time.monotonic() + 5
    while len(wb.engine.run_cancels.active_run_ids()) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(wb.engine.run_cancels.active_run_ids()) == 2

    wb.engine.cancel(doomed["id"])
    doomed_thread.join(5)
    keep_thread.join(10)

    assert wb.tasks.get(doomed["id"])["status"] == TaskStatus.CANCELLED.value
    assert wb.tasks.get(keep["id"])["status"] == TaskStatus.SUCCESS.value, keep_outcome
    assert keep_outcome["result"].status == TaskStatus.SUCCESS.value
    assert (Path(project["root_path"]) / "ok.txt").exists(), "未被取消的任务仍应正常回收产物"
    assert wb.engine.run_cancels.active_run_ids() == set()


def test_shutdown_terminates_inflight_runs(wb, project) -> None:
    """`POST /api/projects/{pid}/stop` 当前调用的就是 `engine.shutdown()`：也必须终止在跑 Agent。"""
    started = threading.Event()
    adapter = _install(wb, "worker", await_cancel=True, delay=20.0, partial_file="half.txt")
    adapter.on_run = lambda request: started.set()
    task = wb.tasks.create(project["id"], "急停", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    thread = threading.Thread(target=lambda: wb.engine.run_task(task["id"], agent_id="worker"))
    thread.start()
    assert started.wait(5)

    wb.engine.shutdown()
    thread.join(5)
    assert not thread.is_alive(), "shutdown 后 Agent 仍在跑（需求 54 缺口）"
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.CANCELLED.value
    assert _runs_for(wb, task["id"])[0]["status"] == "cancelled"
    assert not (Path(project["root_path"]) / "half.txt").exists()


def test_stop_project_does_not_kill_other_projects(wb, project) -> None:
    """需求 10 项目隔离：停项目 A 不能杀掉项目 B 在跑的 Agent（断言 event 本体，不只看 DB）。"""
    tracker = _ConcurrencyTracker()
    adapter = _install(wb, "worker", await_cancel=True, delay=1.0, tracker=tracker, files={"ok.txt": "1"})
    other = wb.projects.create("另一个项目")
    task_a = wb.tasks.create(
        project["id"], "A 长跑", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["ok.txt"],
    )
    task_b = wb.tasks.create(
        other["id"], "B 长跑", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], outputs=["ok.txt"],
    )
    outcome: dict = {}
    thread_a = threading.Thread(target=lambda: outcome.update(a=wb.engine.run_task(task_a["id"], agent_id="worker")))
    thread_b = threading.Thread(target=lambda: outcome.update(b=wb.engine.run_task(task_b["id"], agent_id="worker")))
    thread_a.start()
    thread_b.start()
    deadline = time.monotonic() + 5
    while tracker.entered < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert tracker.entered == 2, f"只有 {tracker.entered} 个 run 起来"

    run_a = _runs_for(wb, task_a["id"])[0]["id"]
    run_b = _runs_for(wb, task_b["id"])[0]["id"]
    # 先抓住两个 run 的 event 本体（run 结束后会从注册表注销，但对象仍然可断言）
    event_a = wb.engine.run_cancels.event_for(run_a)
    event_b = wb.engine.run_cancels.event_for(run_b)
    assert event_a is not None and event_b is not None

    result = wb.engine.stop_project(project["id"])
    thread_a.join(5)

    # 关键断言：A 的信号置位、B 的信号（event 本体）**没有**被置位
    assert event_a.is_set() is True, "A 自己的在跑 run 未被终止"
    assert wb.engine.run_cancels.event_for(run_b) is event_b
    assert event_b.is_set() is False, "停项目 A 把项目 B 的在跑 Agent 也杀了（跨项目误伤，需求 10）"

    thread_b.join(10)
    assert not thread_b.is_alive(), "B 的 Agent 被 A 的停止动作打断"
    assert result["runs_cancelled"] == 1, result
    assert result["tasks_cancelled"] == 1, result
    assert wb.tasks.get(task_a["id"])["status"] == TaskStatus.CANCELLED.value
    assert wb.tasks.get(task_b["id"])["status"] == TaskStatus.SUCCESS.value, outcome
    assert outcome["b"].status == TaskStatus.SUCCESS.value
    assert (Path(other["root_path"]) / "ok.txt").exists(), "B 的产物应正常回收"
    assert not (Path(project["root_path"]) / "ok.txt").exists(), "A 被取消，产物不得回收"

    # 「不再派发」标志也必须是项目级的：A 停、B 不停
    assert wb.engine._is_stopped(project["id"]) is True
    assert wb.engine._is_stopped(other["id"]) is False
    wb.tasks.create(other["id"], "B 的新任务", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    summary = wb.engine.run_project(other["id"])
    assert summary["success"] == 1, f"B 的调度循环被 A 的停止误伤：{summary}"


def test_shutdown_still_cancels_all_projects(wb, project) -> None:
    """进程退出路径（Workbench.close → shutdown）必须仍然全局终止，不能退化。"""
    tracker = _ConcurrencyTracker()
    _install(wb, "worker", await_cancel=True, delay=20.0, tracker=tracker, partial_file="half.txt")
    other = wb.projects.create("另一个项目")
    task_a = wb.tasks.create(project["id"], "A 长跑", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    task_b = wb.tasks.create(other["id"], "B 长跑", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    threads = [
        threading.Thread(target=lambda tid=task_a["id"]: wb.engine.run_task(tid, agent_id="worker")),
        threading.Thread(target=lambda tid=task_b["id"]: wb.engine.run_task(tid, agent_id="worker")),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 5
    while tracker.entered < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert tracker.entered == 2

    wb.engine.shutdown()
    for thread in threads:
        thread.join(5)

    assert all(not t.is_alive() for t in threads), "shutdown 后仍有 Agent 在跑"
    for task in (task_a, task_b):
        assert wb.tasks.get(task["id"])["status"] == TaskStatus.CANCELLED.value
        assert _runs_for(wb, task["id"])[0]["status"] == "cancelled"
    assert wb.engine.run_cancels.active_run_ids() == set()


def test_run_cancel_registry_has_no_leak(wb, project) -> None:
    """多轮 run（成功/失败/异常）之后，取消注册表必须为空。"""
    adapter = _install(wb, "worker", delay=0.0, exit_sequence=[0, 1, 0])
    for i in range(3):
        wb.tasks.create(project["id"], f"R{i}", kind="generic", agent_id="worker", acceptance=[{"type": "always"}], max_attempts=1)
    wb.engine.run_project(project["id"])
    assert adapter.run_count == 3
    assert wb.engine.run_cancels.active_run_ids() == set()
    assert len(wb.engine.run_cancels) == 0

    class _BoomAdapter(_FakeAdapter):
        def run(self, request):
            with self._lock:
                self.run_count += 1
            raise RuntimeError("适配器炸了")

    boom = _BoomAdapter("worker")
    wb.runner._adapters["worker"] = boom
    task = wb.tasks.create(project["id"], "异常路径", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    outcome = wb.engine.run_task(task["id"], agent_id="worker")
    assert outcome.status == TaskStatus.FAILED.value
    assert "适配器异常" in (outcome.error or "")
    assert boom.run_count == 1
    assert wb.engine.run_cancels.active_run_ids() == set(), "异常路径也必须注销取消信号"
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.FAILED.value


# --------------------------------------------------------------------------- 12. 事后审计配对（需求 34 / 44）


def _seed_committed_change(wb, project, task_id: str, rel_path: str) -> None:
    """模拟「另一个正在跑的任务已经改过这个文件」——冲突源。"""
    wb.db.insert(
        "file_changes",
        {
            "project_id": project["id"],
            "task_id": task_id,
            "rel_path": rel_path,
            "action": "modify",
            "in_scope": 1,
            "mode": "committed",
            "conflict": 0,
            "is_binary": 0,
            "at": "2026-01-01T00:00:00.000Z",
        },
    )


def test_audit_changes_pairs_by_rel_path_not_position(wb, project) -> None:
    """changes 被剪枝后，audit 仍必须按 rel_path 配对，冲突不能被静默丢弃。"""
    root = Path(project["root_path"])
    (root / "zz.txt").write_text("v0", encoding="utf-8")
    other = wb.tasks.create(project["id"], "另一个在跑的任务", kind="generic", agent_id="worker")
    wb.tasks.set_status(other["id"], TaskStatus.RUNNING.value)
    _seed_committed_change(wb, project, other["id"], "zz.txt")

    changes = [
        {"action": "create", "rel_path": "aaa.txt", "before_hash": None, "after_hash": "h1", "is_binary": False},
        {"action": "modify", "rel_path": "zz.txt", "before_hash": "b0", "after_hash": "h2", "is_binary": False},
    ]
    recorded = wb.runner.journal.record(
        project_id=project["id"],
        task_id="t-mine",
        run_id="r",
        agent_id="a",
        changes=changes,
        scope_patterns=["zz.txt"],  # aaa.txt 越权，zz.txt 命中冲突
        read_only=False,
        workspace_root=root,
    )
    assert [r.in_scope for r in recorded] == [False, True]
    assert [r.conflict for r in recorded] == [False, True]

    task_ref = {"id": "t-mine", "title": "t", "write_scope": ["zz.txt"], "read_only": 0}
    project_ref = {"id": project["id"], "root_path": str(root)}
    pruned = [c for c in changes if c["rel_path"] == "zz.txt"]  # runner 剪掉越权项后的形态
    audit = wb.guard.audit_changes(project=project_ref, task=task_ref, changes=pruned, recorded=recorded)
    assert audit["conflicts"] == ["zz.txt"], f"剪枝后冲突被静默丢弃：{audit}"
    assert audit["out_of_scope"] == [], f"配对错位把在范围内的文件当成越权：{audit}"


def test_out_of_scope_plus_conflict_still_blocks_and_asks_human(wb, project) -> None:
    """同一次运行里「越权写 + 冲突写」并存时：冲突仍被识别、仍进 approvals（kind=conflict）。"""
    root = Path(project["root_path"])
    (root / "zz.txt").write_text("v0", encoding="utf-8")
    other = wb.tasks.create(project["id"], "另一个在跑的任务", kind="generic", agent_id="worker")
    wb.tasks.set_status(other["id"], TaskStatus.RUNNING.value)
    _seed_committed_change(wb, project, other["id"], "zz.txt")

    _install(wb, "worker", delay=0.0, files={"aaa.txt": "越权内容", "zz.txt": "冲突内容"})
    task = wb.tasks.create(
        project["id"], "越权+冲突", kind="generic", agent_id="worker",
        acceptance=[{"type": "always"}], write_scope=["zz.txt"],
    )
    outcome = wb.engine.run_task(task["id"], agent_id="worker")
    assert "zz.txt" in outcome.conflicts, outcome.conflicts
    assert outcome.status == TaskStatus.BLOCKED.value, outcome
    assert (root / "zz.txt").read_text(encoding="utf-8") == "v0", "冲突文件被自动合并覆盖"
    assert not (root / "aaa.txt").exists(), "越权写入未被还原"
    approvals = wb.guard.list(project["id"], status="pending")
    assert any(a["kind"] == "conflict" for a in approvals), [a["kind"] for a in approvals]
    assert any(a["kind"] == "dangerous_op" for a in approvals), [a["kind"] for a in approvals]


# --------------------------------------------------------------------------- 10. 依赖模式


def test_dependency_mode_success_and_always(wb, project) -> None:
    a = wb.tasks.create(project["id"], "A", kind="generic", agent_id="worker")
    strict = wb.tasks.create(project["id"], "严格依赖", kind="generic", agent_id="worker", depends_on=[a["id"]])
    loose = wb.tasks.create(project["id"], "always 依赖", kind="generic", agent_id="worker")
    wb.tasks.add_edge(loose["id"], a["id"], mode="always")

    ready = {t["id"] for t in wb.tasks.ready_tasks(project["id"])}
    assert a["id"] in ready
    assert strict["id"] not in ready, "success 模式必须等前置成功"
    assert loose["id"] in ready, "always 模式不应被前置状态阻塞"

    wb.tasks.set_status(a["id"], TaskStatus.FAILED.value)
    ready = {t["id"] for t in wb.tasks.ready_tasks(project["id"])}
    assert strict["id"] not in ready
    assert loose["id"] in ready


def test_dependency_mode_soft_allows_final_dep(wb, project) -> None:
    a = wb.tasks.create(project["id"], "A", kind="generic", agent_id="worker")
    soft = wb.tasks.create(project["id"], "soft 依赖", kind="generic", agent_id="worker")
    wb.tasks.add_edge(soft["id"], a["id"], mode="soft")
    assert soft["id"] not in {t["id"] for t in wb.tasks.ready_tasks(project["id"])}
    wb.tasks.set_status(a["id"], TaskStatus.FAILED.value)
    assert soft["id"] in {t["id"] for t in wb.tasks.ready_tasks(project["id"])}
