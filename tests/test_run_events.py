"""跨进程实时事件持久化（CLI 跑任务、另开看板也能看到）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from multiagent.api.app import create_api
from multiagent.app import Workbench


def test_run_events_persist_and_are_readable_by_another_workbench(
    make_wb, plan_writer, acp_agent, home
):
    plan_writer(
        "coder",
        {
            "files": {"event.txt": "ok"},
            "handoff": {"did": "ACP 事件测试"},
            "exit": 0,
        },
    )
    wb = make_wb([acp_agent("coder")])
    project = wb.projects.create("events")
    task = wb.tasks.create(
        project["id"],
        "事件任务",
        kind="generic",
        agent_id="coder",
        acceptance=[{"type": "file_exists", "path": "event.txt"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success"
    wb.run_events.flush()

    items = wb.run_events.list(project["id"])
    assert items, "实时事件没有落库"
    assert any("agent_message" in str(item.get("kind")) for item in items)
    assert any("执行完成" in str(item.get("text")) for item in items)

    # 模拟另一个进程（看板后端）打开同一个 home：它必须能读到同样的流。
    other = Workbench.open(home)
    try:
        read_back = other.run_events.list(project["id"])
        assert len(read_back) >= len(items)
        assert read_back[0]["event_id"] == items[0]["event_id"]
    finally:
        other.close()


def test_run_events_api_replay(make_wb, plan_writer, acp_agent, home):
    plan_writer("coder", {"files": {"api-event.txt": "ok"}, "handoff": {"did": "ok"}, "exit": 0})
    wb = make_wb([acp_agent("coder")])
    project = wb.projects.create("events-api")
    task = wb.tasks.create(
        project["id"], "事件 API", kind="generic", agent_id="coder", acceptance=[{"type": "always"}]
    )
    assert wb.engine.run_task(task["id"]).status == "success"
    wb.run_events.flush()

    client = TestClient(create_api(wb))
    res = client.get(f"/api/projects/{project['id']}/run-events?limit=50")
    assert res.status_code == 200
    body = res.json()
    assert body["count"] > 0
    assert body["last_id"] >= body["items"][0]["event_id"]
