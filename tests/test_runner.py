"""端到端：跑假 Agent 的完整执行链路（隔离 / 审计 / 验收 / 交接 / 冲突 / 额度 / 恢复）。"""

from __future__ import annotations

import time
from pathlib import Path

from multiagent.tasks.service import TaskStatus


def _files(task):
    return task["result"].get("committed", [])


def test_run_success_cli(wb, project, set_plan):
    root = Path(project["root_path"])
    set_plan(
        "worker",
        {
            "files": {"report.md": "# 报告\n\n内容"},
            "handoff": {
                "did": "写好了报告",
                "not_done": "未配图",
                "open_issues": ["缺少数据源"],
                "next_steps": ["补充图表"],
            },
            "exit": 0,
        },
    )
    task = wb.tasks.create(
        project["id"], "写报告", kind="doc", agent_id="worker", acceptance=[{"type": "file_exists", "path": "report.md"}]
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == TaskStatus.SUCCESS.value
    assert (root / "report.md").exists()
    stored = wb.tasks.get(task["id"])
    assert stored["status"] == "success"
    assert stored["result"]["acceptance"]["ok"] is True
    handoff = wb.handoffs.latest_for_task(task["id"])
    assert handoff["did"] == "写好了报告"
    assert handoff["open_issues"] == ["缺少数据源"]
    assert wb.tasks.artifacts(task_id=task["id"])
    assert wb.audit.runs(task_id=task["id"])[0]["status"] == "success"
    assert wb.quota.usage("worker")["invocations"] == 1


def test_run_success_acp_with_tokens(wb, project, set_plan):
    set_plan("coder", {"files": {"out.txt": "done"}, "handoff": {"did": "ACP 完成"}, "exit": 0})
    task = wb.tasks.create(
        project["id"], "ACP 任务", kind="generic", agent_id="coder", acceptance=[{"type": "file_exists", "path": "out.txt"}]
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success"
    run = wb.audit.runs(task_id=task["id"])[0]
    assert run["agent_id"] == "coder"
    assert run["cost_tokens"] == 46  # 假 ACP 服务端上报的 usage


def test_exit_code_failure_even_with_artifacts(wb, project, set_plan):
    set_plan("worker", {"files": {"x.txt": "y"}, "exit": 3})
    task = wb.tasks.create(project["id"], "必然失败", kind="generic", agent_id="worker", acceptance=[{"type": "file_exists", "path": "x.txt"}])
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "failed"
    assert outcome.exit_code == 3
    # 退出码非 0 时即使产物存在且验收通过，也不能自动回写工作区（需求 42/43）。
    assert outcome.acceptance_ok is True
    assert outcome.committed == []
    assert not (Path(project["root_path"]) / "x.txt").exists()


def test_acceptance_failure_when_no_change(wb, project, set_plan):
    set_plan("worker", {"exit": 0})
    task = wb.tasks.create(
        project["id"], "什么都没做", kind="code", agent_id="worker", acceptance=[{"type": "file_modified"}]
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "failed"
    assert not outcome.acceptance_ok
    assert any(a["kind"] == "acceptance_failed" for a in wb.guard.list(project["id"]))


def test_read_only_review_blocks_writes(wb, project, set_plan):
    root = Path(project["root_path"])
    (root / "draft.md").write_text("原稿", encoding="utf-8")
    set_plan("worker", {"files": {"draft.md": "被改坏了"}, "handoff": {"did": "评审"}, "exit": 0})
    task = wb.tasks.create(project["id"], "只读评审", template_id="review", agent_id="worker")
    assert task["read_only"] is True
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "failed"  # 违反 no_writes
    assert (root / "draft.md").read_text(encoding="utf-8") == "原稿"


def test_protected_path_blocked(wb, project, set_plan):
    root = Path(project["root_path"])
    (root / "config").mkdir(exist_ok=True)
    set_plan("worker", {"files": {"config/hack.json": "{}"}, "handoff": {"did": "尝试改配置"}, "exit": 0})
    task = wb.tasks.create(project["id"], "改配置", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    outcome = wb.engine.run_task(task["id"])
    assert not (root / "config/hack.json").exists()
    assert "config/hack.json" in outcome.out_of_scope
    assert any("越权写入" in a["title"] for a in wb.guard.list(project["id"]))
    # 需求 43：越权产物在还原前必须留证，不能物理销毁
    run = wb.db.query_one("SELECT prompt_path FROM runs WHERE id = ?", (outcome.run_id,))
    archived = Path(run["prompt_path"]).parent / "out-of-scope" / "config" / "hack.json"
    assert archived.exists(), "越权文件被还原前没有归档到 run 目录"


def test_conflict_blocks_auto_merge(wb, project, set_plan):
    root = Path(project["root_path"])
    (root / "shared.txt").write_text("v0", encoding="utf-8")
    other = wb.tasks.create(project["id"], "另一个任务", kind="generic", agent_id="coder")
    wb.tasks.set_status(other["id"], TaskStatus.RUNNING.value)
    wb.db.insert(
        "file_changes",
        {
            "project_id": project["id"],
            "task_id": other["id"],
            "rel_path": "shared.txt",
            "action": "modify",
            "in_scope": 1,
            "mode": "committed",
            "conflict": 0,
            "is_binary": 0,
            "at": "2026-01-01T00:00:00.000Z",
        },
    )
    set_plan("worker", {"files": {"shared.txt": "v1"}, "handoff": {"did": "改了"}, "exit": 0})
    task = wb.tasks.create(project["id"], "并发写同一文件", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "blocked"
    assert "shared.txt" in outcome.conflicts
    assert (root / "shared.txt").read_text(encoding="utf-8") == "v0"
    assert any(a["kind"] == "conflict" for a in wb.guard.list(project["id"]))


def test_quota_exceeded(make_wb, plan_writer, cli_agent, home):
    plan_writer("worker", {"files": {"a.txt": "1"}, "exit": 0})
    wb = make_wb([cli_agent("worker", quota={"max_invocations": 1})])
    project = wb.projects.create("q")
    t1 = wb.tasks.create(project["id"], "第一次", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    t2 = wb.tasks.create(project["id"], "第二次", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    assert wb.engine.run_task(t1["id"]).status == "success"
    second = wb.engine.run_task(t2["id"])
    assert second.status == "blocked"
    assert any(a["kind"] == "quota" for a in wb.guard.list(project["id"]))


def test_retry_skip_cancel(wb, project):
    task = wb.tasks.create(project["id"], "T", kind="generic", agent_id="worker")
    wb.engine.skip(task["id"])
    assert wb.tasks.get(task["id"])["status"] == "skipped"
    wb.engine.retry(task["id"])
    assert wb.tasks.get(task["id"])["status"] == "pending"
    wb.engine.cancel(task["id"])
    assert wb.tasks.get(task["id"])["status"] == "cancelled"


def test_recover_marks_interrupted(wb, project):
    task = wb.tasks.create(project["id"], "中断的任务", kind="generic", agent_id="worker")
    wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)
    result = wb.engine.recover(project["id"])
    assert result["interrupted"] >= 1
    assert wb.tasks.get(task["id"])["status"] == "interrupted"


def test_resume_reruns_interrupted(wb, project, set_plan):
    set_plan("worker", {"files": {"resume.txt": "ok"}, "exit": 0})
    task = wb.tasks.create(project["id"], "续跑", kind="generic", agent_id="worker", acceptance=[{"type": "file_exists", "path": "resume.txt"}])
    wb.tasks.set_status(task["id"], TaskStatus.RUNNING.value)
    summary = wb.engine.run_project(project["id"])
    assert summary["success"] >= 1
    assert wb.tasks.get(task["id"])["status"] == "success"


def test_partial_workspace_resumed_on_retry(wb, project, set_plan):
    """长任务超时/失败后重试，应恢复上次在写范围内的半成品而不是从零重来。"""
    root = Path(project["root_path"])
    set_plan(
        "worker",
        {
            "files": {"partial.txt": "first\n"},
            "handoff": {"did": "做了一半"},
            "exit": 3,
        },
    )
    task = wb.tasks.create(
        project["id"],
        "长任务续跑",
        kind="code",
        agent_id="worker",
        acceptance=[
            {"type": "file_exists", "path": "partial.txt"},
            {"type": "content_contains", "path": "partial.txt", "text": "second"},
        ],
    )
    first = wb.engine.run_task(task["id"])
    assert first.status == "failed"
    assert not (root / "partial.txt").exists()

    set_plan(
        "worker",
        {
            "append": {"partial.txt": "second\n"},
            "handoff": {"did": "接着上次完成"},
            "exit": 0,
        },
    )
    wb.engine.retry(task["id"])
    second = wb.engine.run_task(task["id"])
    assert second.status == "success", second.error
    assert (root / "partial.txt").read_text(encoding="utf-8") == "first\nsecond\n"
    assert any("恢复上一次尝试" in str(e.get("summary") or "") for e in wb.board.read_events(project["id"]))


def test_quota_max_seconds_caps_single_run(make_wb, plan_writer, cli_agent):
    """max_seconds 必须约束单次调用，而不是只在事后拒绝后续派发。"""
    plan_writer("slow", {"sleep": 10, "exit": 0})
    wb = make_wb([cli_agent("slow", quota={"max_seconds": 1, "max_invocations": 10})])
    project = wb.projects.create("quota-seconds")
    task = wb.tasks.create(
        project["id"], "慢任务", kind="generic", agent_id="slow", acceptance=[{"type": "always"}]
    )
    started = time.monotonic()
    outcome = wb.engine.run_task(task["id"])
    elapsed = time.monotonic() - started
    assert outcome.status == "failed"
    assert elapsed < 6, f"max_seconds 没有硬约束单次调用：{elapsed:.1f}s"


def test_run_project_deadline_is_hard_bound(make_wb, plan_writer, cli_agent):
    """``run_project(max_seconds=...)`` 必须终止在跑的 Agent，而不是等它自然结束。"""
    plan_writer("slow", {"sleep": 30, "exit": 0})
    wb = make_wb([cli_agent("slow")], policy_extra={"concurrency": {"global": 2, "per_project": 1}})
    project = wb.projects.create("deadline")
    task = wb.tasks.create(
        project["id"], "慢任务", kind="generic", agent_id="slow", acceptance=[{"type": "always"}]
    )
    started = time.monotonic()
    summary = wb.engine.run_project(project["id"], max_seconds=0.5, poll_seconds=0.05)
    elapsed = time.monotonic() - started
    assert summary["deadline_hit"] is True
    assert elapsed < 10, f"max_seconds 没有终止在跑任务：{elapsed:.1f}s"
    assert wb.tasks.get(task["id"])["status"] in ("cancelled", "failed", "interrupted")


def test_guard_detects_dangerous_commands(wb):
    critical = wb.guard.inspect_agent_event(
        {"source_type": "tool_call", "tool_input": {"command": "format C: /q"}}
    )
    assert critical["dangerous"] and critical["severity"] == "critical"
    destructive = wb.guard.inspect_agent_event(
        {"source_type": "tool_call", "tool_input": {"command": "rm -rf src/old"}}
    )
    assert destructive["dangerous"] and destructive["severity"] == "destructive"
    safe = wb.guard.inspect_agent_event(
        {"source_type": "tool_call", "tool_input": {"command": "node --test"}}
    )
    assert safe["dangerous"] is False
    # 真实误报回归：方法名 `.shutdown()` 不是系统关机命令。
    method_call = wb.guard.inspect_agent_event(
        {
            "source_type": "tool_call",
            "tool_input": {"command": "await atlas.shutdown('TEST'); console.log('PASS');"},
        }
    )
    assert method_call["dangerous"] is False
    real_shutdown = wb.guard.inspect_agent_event(
        {"source_type": "tool_call", "tool_input": {"command": "shutdown /s /t 0"}}
    )
    assert real_shutdown["dangerous"] and real_shutdown["severity"] == "critical"
    write_tool = wb.guard.inspect_agent_event(
        {
            "source_type": "tool_call",
            "tool": "Write",
            "tool_kind": "write",
            "tool_input": {"content": "shutdown /s /t 0 is just documentation"},
        }
    )
    assert write_tool["dangerous"] is False
    # 普通 HTML 里的 ">" 不是 shell 重定向，不能被当成覆盖既有产物。
    html_write = wb.guard.classify_permission(
        "write", "Write index.html", '<div class="hero">Hello</div>'
    )
    assert html_write.allowed is True


def test_retry_supersedes_stale_approvals(wb, project, set_plan):
    """重试后，上一次尝试的验收失败审批不应继续挂在待办里。"""
    set_plan("worker", {"exit": 0})
    task = wb.tasks.create(
        project["id"],
        "第一次失败",
        kind="code",
        agent_id="worker",
        acceptance=[{"type": "file_modified"}],
    )
    first = wb.engine.run_task(task["id"])
    assert first.status == "failed"
    assert any(
        a["kind"] == "acceptance_failed" and a["status"] == "pending"
        for a in wb.guard.list(project["id"])
    )

    set_plan("worker", {"files": {"retry.txt": "ok"}, "exit": 0})
    wb.engine.retry(task["id"])
    second = wb.engine.run_task(task["id"])
    assert second.status == "success"
    assert not any(
        a["kind"] == "acceptance_failed" and a["status"] == "pending" and a["task_id"] == task["id"]
        for a in wb.guard.list(project["id"])
    )


def test_destructive_delete_requires_confirmation(wb, project, set_plan):
    """需求 44：删除既有文件必须二次确认，未确认前不自动回写工作区。"""
    root = Path(project["root_path"])
    (root / "gone.txt").write_text("要删的内容", encoding="utf-8")
    set_plan("worker", {"delete": ["gone.txt"], "handoff": {"did": "删除旧文件"}, "exit": 0})
    task = wb.tasks.create(
        project["id"],
        "删除文件",
        kind="code",
        agent_id="worker",
        acceptance=[{"type": "file_absent", "path": "gone.txt"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "blocked"
    assert (root / "gone.txt").exists(), "未确认前删除不应落到工作区"
    assert any(
        a["kind"] == "dangerous_op" and a["status"] == "pending" and a["task_id"] == task["id"]
        for a in wb.guard.list(project["id"])
    )

    # 人工确认后 sync：删除才真正生效。
    wb.engine.approve(task["id"], note="确认删除", sync=True)
    assert wb.tasks.get(task["id"])["status"] == "success"
    assert not (root / "gone.txt").exists()


def test_handoff_notes_out_of_scope_reverts(wb, project, set_plan):
    """被还原的越权改动必须在交接里明确写出，不能继续误导下游任务。"""
    set_plan(
        "worker",
        {
            "files": {"outside.txt": "not allowed"},
            "handoff": {"did": "修好了 outside.txt", "notes": "已完成"},
            "exit": 0,
        },
    )
    task = wb.tasks.create(
        project["id"],
        "越权修复",
        kind="code",
        agent_id="worker",
        write_scope=["allowed/**"],
        acceptance=[{"type": "always"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success"
    assert not (Path(project["root_path"]) / "outside.txt").exists()
    handoff = wb.handoffs.latest_for_task(task["id"])
    assert "write_scope" in handoff["notes"]
    assert any("outside.txt" in item for item in handoff["open_issues"])


def test_context_injection_between_tasks(wb, project, set_plan):
    a = wb.tasks.create(project["id"], "调研", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    b = wb.tasks.create(
        project["id"], "写报告", kind="generic", agent_id="worker", depends_on=[a["id"]], acceptance=[{"type": "always"}]
    )
    set_plan("worker", {"handoff": {"did": "关键结论：XYZ-123 是答案"}, "exit": 0})
    wb.engine.run_task(a["id"])
    set_plan("worker", {"files": {"report.md": "done"}, "exit": 0})
    wb.engine.run_task(b["id"])
    run_b = [r for r in wb.audit.runs(task_id=b["id"]) if r["status"] == "success"][0]
    prompt = Path(run_b["prompt_path"]).read_text(encoding="utf-8")
    assert "XYZ-123" in prompt


def test_fanout_keeps_all_results(make_wb, plan_writer, cli_agent, home):
    plan_writer("good", {"files": {"ok.txt": "1"}, "handoff": {"did": "good"}, "exit": 0})
    plan_writer("bad", {"exit": 1})
    wb = make_wb([cli_agent("good"), cli_agent("bad")])
    project = wb.projects.create("fan")
    task = wb.tasks.create(
        project["id"], "扇出", kind="generic", acceptance=[{"type": "file_exists", "path": "ok.txt"}]
    )
    result = wb.engine.fanout(task["id"], ["good", "bad"])
    assert result["best_agent"] == "good"
    assert len(result["results"]) == 2
    assert len(wb.audit.runs(task_id=task["id"])) == 2
    comparison = wb.audit.compare_runs(task["id"])
    assert len(comparison["runs"]) == 2


def test_parallel_run_project(wb, project, set_plan):
    set_plan("worker", {"exit": 0})
    set_plan("coder", {"exit": 0})
    for i in range(3):
        wb.tasks.create(project["id"], f"并行{i}", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    for i in range(2):
        wb.tasks.create(project["id"], f"并行ACP{i}", kind="generic", agent_id="coder", acceptance=[{"type": "always"}])
    summary = wb.engine.run_project(project["id"])
    assert summary["success"] == 5
    assert summary["progress"]["counters"].get("success") == 5


def test_human_task_flow(wb, project):
    task = wb.tasks.create(project["id"], "人工审批一下", template_id="human-step")
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == TaskStatus.WAITING.value
    assert wb.tasks.get(task["id"])["status"] == "waiting"
    assert any(a["kind"] == "manual_confirm" for a in wb.guard.list(project["id"], status="pending"))
    done = wb.engine.provide(task["id"], did="人做完了", notes="手动")
    assert done["status"] == "success"
    assert wb.handoffs.latest_for_task(task["id"])["source"] == "human"


def test_router_prefers_kind_order(wb, project):
    task = wb.tasks.create(project["id"], "文档类", kind="doc")
    agent, reason = wb.runner.choose_agent(wb.tasks.get(task["id"]))
    assert agent is not None
    assert "选择" in reason or "指定" in reason


def test_default_router_can_pick_explicit(wb, project):
    task = wb.tasks.create(project["id"], "指定 coder", kind="generic", agent_id="coder")
    agent, _ = wb.runner.choose_agent(task)
    assert agent == "coder"


def test_stats_timeline_export(wb, project, set_plan, tmp_path):
    set_plan("worker", {"files": {"s.txt": "1"}, "exit": 0})
    task = wb.tasks.create(project["id"], "统计", kind="generic", agent_id="worker", acceptance=[{"type": "always"}])
    wb.engine.run_task(task["id"])
    stats = wb.audit.stats(project["id"])
    assert stats["totals"]["runs"] >= 1
    assert stats["by_agent"][0]["agent_id"] == "worker"
    out = tmp_path / "timeline.md"
    wb.audit.export_timeline(project["id"], out)
    text = out.read_text(encoding="utf-8")
    assert "协作时间线" in text
    assert len(wb.audit.timeline(project["id"])) >= 1
