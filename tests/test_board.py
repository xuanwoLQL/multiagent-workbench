"""共享黑板：类型、审核闸门、版本化、检索、事件哈希链。"""

from __future__ import annotations


def test_post_and_get(wb, project):
    entry = wb.board.post(project["id"], type="note", title="标题", body="正文", scope="task")
    assert entry["type"] == "note"
    assert entry["status"] == "approved"
    assert wb.board.get(entry["id"])["body"] == "正文"


def test_review_gate_blocks_project_memory(wb, project):
    entry = wb.board.post(project["id"], type="decision", title="用 SQLite", body="理由", scope="project")
    assert entry["status"] == "pending"
    assert wb.board.long_term_memory(project["id"]) == []
    wb.board.review(entry["id"], approved=True, reviewer="human")
    memory = wb.board.long_term_memory(project["id"])
    assert memory and memory[0]["id"] == entry["id"]


def test_scope_layering(wb, project):
    wb.board.post(project["id"], type="note", title="项目", scope="project", status="approved")
    wb.board.post(project["id"], type="note", title="任务", scope="task")
    wb.board.post(project["id"], type="note", title="草稿", scope="agent", agent_id="worker")
    assert len(wb.board.list(project["id"], scope="project")) == 1
    assert len(wb.board.list(project["id"], scope="agent")) == 1


def test_versioning_and_edit(wb, project):
    entry = wb.board.post(project["id"], type="note", title="v1", body="body1")
    wb.board.edit(entry["id"], body="body2", author="human", note="修正")
    updated = wb.board.get(entry["id"])
    assert updated["current_version"] == 2
    assert updated["body"] == "body2"
    versions = wb.board.versions(entry["id"])
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["body"] == "body1"


def test_search_by_keyword_task_agent(wb, project):
    wb.board.post(project["id"], type="issue", title="密码错误", body="登录失败", task_id="t1", agent_id="worker")
    wb.board.post(project["id"], type="issue", title="超时", body="接口慢", task_id="t2", agent_id="coder")
    assert len(wb.board.search(project["id"], "密码")) == 1
    assert len(wb.board.search(project["id"], "", task_id="t2")) == 1
    assert len(wb.board.search(project["id"], "", agent_id="worker")) == 1


def test_events_hash_chain(wb, project):
    for i in range(5):
        wb.board.post(project["id"], type="note", title=f"n{i}", body="x")
    assert wb.board.verify_chain(project["id"]) is True
    events = wb.board.read_events(project["id"], after_id=0)
    assert len(events) >= 5
    assert events[0]["prev_hash"] == ""


def test_handoff_posts_context(wb, project):
    handoff = wb.handoffs.create(
        project_id=project["id"],
        task_id="t1",
        run_id=None,
        agent_id="worker",
        data={"did": "完成了调研", "next_steps": ["写报告"], "open_issues": ["数据缺失"]},
    )
    assert handoff["did"] == "完成了调研"
    assert wb.handoffs.latest_for_task("t1")["id"] == handoff["id"]
    types = {e["type"] for e in wb.board.list(project["id"])}
    assert {"handoff", "todo", "issue"} <= types


def test_new_handoff_supersedes_stale_issues(wb, project):
    """任务重试成功后，旧 run 的遗留问题不能继续以 approved 注入后续任务。"""
    first = wb.handoffs.create(
        project_id=project["id"], task_id="t-retry", run_id="r1", agent_id="worker",
        data={"did": "第一次失败", "open_issues": ["旧问题"]},
    )
    stale = [e for e in wb.board.list(project["id"], type="issue") if e["task_id"] == "t-retry"]
    assert len(stale) == 1 and stale[0]["status"] == "approved"

    second = wb.handoffs.create(
        project_id=project["id"], task_id="t-retry", run_id="r2", agent_id="worker",
        data={"did": "重试成功"},
    )
    assert second["id"] != first["id"]
    assert wb.board.get(stale[0]["id"])["status"] == "rejected"
    assert not [e for e in wb.board.list(project["id"], type="issue", status="approved") if e["task_id"] == "t-retry"]


def test_handoff_edit_becomes_authoritative(wb, project):
    handoff = wb.handoffs.create(project_id=project["id"], task_id="t1", run_id=None, agent_id="worker", data={"did": "原版"})
    edited = wb.handoffs.edit(handoff["id"], {"did": "人工修正版", "notes": "审核通过"}, editor="human")
    assert edited["did"] == "人工修正版"
    assert edited["source"] == "human"
    assert edited["edited_by"] == "human"


def test_parse_handoff_from_text():
    from multiagent.board.handoff import parse_handoff, render_handoff

    text = '前言\n```json\n{"did": "A", "next_steps": ["B"], "open_issues": "C"}\n```\n尾声'
    parsed = parse_handoff(text)
    assert parsed["did"] == "A"
    assert parsed["next_steps"] == ["B"]
    assert parsed["open_issues"] == ["C"]
    rendered = render_handoff(parsed)
    assert "做了什么" in rendered and "A" in rendered
