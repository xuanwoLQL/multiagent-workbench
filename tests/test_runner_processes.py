"""P1-7 集成：Runner 必须把进程审计结果写进任务结果与事件流。

真实端口探测由 ``tests/test_process_audit.py`` 用注入 prober 覆盖；这里用假
``ProcessAudit`` 验证接线：开始/结束时机、策略开关、结果字段与事件。
"""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api


class _FakeAudit:
    calls: list[tuple[str, object]] = []

    @classmethod
    def begin(cls):
        instance = cls()
        cls.calls.append(("begin", None))
        return instance

    def finish(self, workdir, *, reap: bool = True, **_kwargs):
        self.calls.append(("finish", (str(workdir), reap)))
        return {
            "supported": True,
            "started": [
                {"pid": 4242, "ports": [5173], "command": "tsx server.ts", "owned": True}
            ],
            "reaped": [{"pid": 4242, "ports": [5173], "command": "tsx server.ts"}],
            "warnings": [],
            "before_count": 0,
            "after_count": 1,
        }


def _task(wb, project, *, title="Process task"):
    return wb.tasks.create(
        project["id"],
        title,
        kind="generic",
        agent_id="worker",
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )


def _wait_processes(wb, task_id: str, timeout: float = 10.0) -> dict:
    """异步审计在线程里完成；测试显式等待结果回填。"""
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = wb.tasks.get(task_id).get("result") or {}
        if isinstance(latest.get("processes"), dict):
            return latest["processes"]
        time.sleep(0.05)
    raise AssertionError(f"进程审计结果未在 {timeout}s 内回填：{latest}")


def test_runner_records_process_audit_result_and_event(
    wb, tmp_path: Path, set_plan, monkeypatch
):
    from multiagent.orchestrator import runner as runner_mod

    _FakeAudit.calls = []
    monkeypatch.setattr(runner_mod, "ProcessAudit", _FakeAudit)
    root = tmp_path / "plain"
    root.mkdir()
    project = wb.projects.create("process-demo", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = _task(wb, project)

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    processes = _wait_processes(wb, task["id"])
    assert [call[0] for call in _FakeAudit.calls] == ["begin", "finish"]
    workdir, reap = _FakeAudit.calls[1][1]
    assert workdir.endswith("work")
    assert reap is True

    assert processes["reaped"][0]["pid"] == 4242
    assert processes["started"][0]["ports"] == [5173]

    events = []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        events = [
            event
            for event in wb.board.read_events(project["id"])
            if event.get("kind") == "task.processes"
        ]
        if events:
            break
        time.sleep(0.05)
    assert events, "有新增监听进程时必须写事件"
    assert events[0]["data"]["reaped"][0]["pid"] == 4242

    # 任务详情 API 能读到同一份结果。
    client = TestClient(create_api(wb))
    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["result"]["processes"]["reaped"][0]["pid"] == 4242


def test_process_audit_can_be_disabled_by_policy(
    wb, tmp_path: Path, set_plan, monkeypatch
):
    from multiagent.orchestrator import runner as runner_mod

    class _MustNotRun:
        @classmethod
        def begin(cls):  # pragma: no cover - 断言不会被调用
            raise AssertionError("processes.audit_enabled=false 时不应启动审计")

    monkeypatch.setattr(runner_mod, "ProcessAudit", _MustNotRun)
    wb.config.policy_raw.setdefault("processes", {})["audit_enabled"] = False
    root = tmp_path / "plain-off"
    root.mkdir()
    project = wb.projects.create("process-off", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = _task(wb, project)

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    assert wb.tasks.get(task["id"])["result"]["processes"] is None


def test_short_timeout_can_still_opt_out_of_audit(
    wb, tmp_path: Path, set_plan, monkeypatch
):
    """min_timeout_sec 仍是显式逃生阀；默认 0 表示短任务也审计。"""
    from multiagent.orchestrator import runner as runner_mod

    class _MustNotRun:
        @classmethod
        def begin(cls):  # pragma: no cover - 断言不会被调用
            raise AssertionError("显式 min_timeout_sec=5 时短任务不应启动审计")

    monkeypatch.setattr(runner_mod, "ProcessAudit", _MustNotRun)
    wb.config.policy_raw.setdefault("processes", {})["min_timeout_sec"] = 5
    root = tmp_path / "plain-short"
    root.mkdir()
    project = wb.projects.create("process-short", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = wb.tasks.create(
        project["id"],
        "Short task",
        kind="generic",
        agent_id="worker",
        timeout_sec=1,
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    assert wb.tasks.get(task["id"])["result"]["processes"] is None


def test_slow_audit_does_not_block_run_return(wb, tmp_path: Path, set_plan, monkeypatch):
    """审计 finish 再慢也不能拖住 run_task；取消/硬超时的 1.5s 上限必须保住。"""
    from multiagent.orchestrator import runner as runner_mod

    class _SlowAudit:
        @classmethod
        def begin(cls):
            return cls()

        def finish(self, workdir, *, reap: bool = True, **_kwargs):
            time.sleep(2.0)
            return {
                "supported": True,
                "started": [],
                "reaped": [],
                "warnings": [],
                "before_count": 0,
                "after_count": 0,
            }

    monkeypatch.setattr(runner_mod, "ProcessAudit", _SlowAudit)
    root = tmp_path / "plain-slow-audit"
    root.mkdir()
    project = wb.projects.create("process-slow-audit", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = _task(wb, project)

    started = time.monotonic()
    outcome = wb.engine.run_task(task["id"])
    elapsed = time.monotonic() - started
    assert outcome.status == "success", outcome.error
    assert elapsed < 1.5, f"异步审计仍阻塞了 run_task：{elapsed:.2f}s"
    # Workbench/CLI 退出路径可等待审计落盘（有界），不会杀死 daemon 线程。
    assert wb.runner.wait_for_process_audits(5.0) is True
    # 审计线程最终仍会回填（没有新增进程时 started/reaped 为空）。
    processes = _wait_processes(wb, task["id"])
    assert processes["supported"] is True



def test_short_timeout_is_audited_by_default(wb, tmp_path: Path, set_plan, monkeypatch):
    """异步化后默认 min_timeout_sec=0：1 秒任务也应留下进程审计结果。"""
    from multiagent.orchestrator import runner as runner_mod

    started_calls: list[str] = []

    class _Audit:
        @classmethod
        def begin(cls):
            started_calls.append("begin")
            return cls()

        def finish(self, workdir, *, reap: bool = True, **_kwargs):
            return {
                "supported": True,
                "started": [],
                "reaped": [],
                "warnings": [],
                "before_count": 0,
                "after_count": 0,
            }

    monkeypatch.setattr(runner_mod, "ProcessAudit", _Audit)
    root = tmp_path / "plain-short-default"
    root.mkdir()
    project = wb.projects.create("process-short-default", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = wb.tasks.create(
        project["id"],
        "Short task default audit",
        kind="generic",
        agent_id="worker",
        timeout_sec=1,
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    assert started_calls == ["begin"]
    processes = _wait_processes(wb, task["id"])
    assert processes["supported"] is True


def test_set_result_key_preserves_concurrent_keys(wb):
    """审计线程与人工合并不能互相覆盖 result 的其它键。"""
    project = wb.projects.create("result-keys")
    task = wb.tasks.create(project["id"], "Result keys", kind="generic")
    wb.tasks.update(
        task["id"],
        result={"run_id": "run-a", "git": {"merge": {"merged": True}}, "cost": {"source": "reported"}},
    )
    assert wb.tasks.set_result_key(
        task["id"], "processes", {"supported": True}, run_id="run-a"
    ) is True
    stored = wb.tasks.get(task["id"])["result"]
    assert stored["git"]["merge"]["merged"] is True
    assert stored["cost"]["source"] == "reported"
    assert stored["processes"]["supported"] is True
    # 旧 run 不能覆盖新 run。
    assert wb.tasks.set_result_key(
        task["id"], "processes", {"supported": False}, run_id="run-old"
    ) is False
    assert wb.tasks.get(task["id"])["result"]["processes"]["supported"] is True
