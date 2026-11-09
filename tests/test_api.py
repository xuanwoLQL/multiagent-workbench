"""HTTP / WebSocket 接口测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from multiagent.api.app import create_api


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def test_health_and_config(wb):
    client = _client(wb)
    res = client.get("/api/health")
    assert res.json()["ok"] is True
    assert res.headers["content-type"] == "application/json; charset=utf-8"
    cfg = client.get("/api/config").json()
    assert "worker" in [a["id"] for a in cfg["agents"]]
    agents = client.get("/api/agents").json()
    assert any(a["agent_id"] == "worker" for a in agents)


def test_project_and_task_flow(wb, set_plan):
    client = _client(wb)
    project = client.post("/api/projects", json={"name": "api-demo"}).json()
    pid = project["id"]
    assert client.get(f"/api/projects/{pid}").json()["project"]["name"] == "api-demo"

    set_plan("worker", {"files": {"api.txt": "hello"}, "handoff": {"did": "API 完成"}, "exit": 0})
    task = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "API 任务", "kind": "generic", "agent_id": "worker", "acceptance": [{"type": "file_exists", "path": "api.txt"}]},
    ).json()
    run = client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60}).json()
    assert run["status"] == "success"

    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["status"] == "success"
    assert detail["runs"][0]["status"] == "success"
    assert detail["handoff"]["did"] == "API 完成"

    kanban = client.get(f"/api/projects/{pid}/kanban").json()
    assert kanban["nodes"][0]["status"] == "success"
    assert client.get(f"/api/projects/{pid}/progress").json()["counters"]["success"] == 1


def test_board_endpoints(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "board-api"}).json()["id"]
    entry = client.post(
        f"/api/projects/{pid}/board",
        json={"type": "decision", "title": "选定方案 A", "body": "理由", "scope": "project"},
    ).json()
    assert entry["status"] == "pending"
    assert client.get(f"/api/projects/{pid}/memory").json()["long_term"] == []
    client.post(f"/api/board/{entry['id']}/review", json={"approved": True})
    assert client.get(f"/api/projects/{pid}/memory").json()["long_term"]
    assert client.get(f"/api/projects/{pid}/board/search?q=方案").json()
    versions = client.get(f"/api/board/{entry['id']}").json()["versions"]
    assert len(versions) == 1


def test_approvals_and_stats(wb, set_plan):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "apr-api"}).json()["id"]
    set_plan("worker", {"exit": 0})
    task = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "必然验收失败", "kind": "code", "agent_id": "worker", "acceptance": [{"type": "file_modified"}]},
    ).json()
    client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60})
    approvals = client.get(f"/api/projects/{pid}/approvals?status=pending").json()
    assert any(a["kind"] == "acceptance_failed" for a in approvals)
    resolved = client.post(f"/api/approvals/{approvals[0]['id']}/resolve", json={"approved": True}).json()
    assert resolved["status"] == "approved"

    stats = client.get(f"/api/projects/{pid}/stats").json()
    assert stats["totals"]["runs"] >= 1
    timeline = client.get(f"/api/projects/{pid}/timeline").json()
    assert timeline
    events = client.get(f"/api/projects/{pid}/events").json()
    assert events


def test_task_ops_and_changes(wb, set_plan):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "ops-api"}).json()["id"]
    set_plan("worker", {"files": {"ops.txt": "1"}, "exit": 0})
    task = client.post(
        f"/api/projects/{pid}/tasks", json={"title": "操作", "kind": "generic", "agent_id": "worker"}
    ).json()
    client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60})
    changes = client.get(f"/api/tasks/{task['id']}/changes").json()
    assert changes and changes[0]["rel_path"] == "ops.txt"
    assert client.post(f"/api/tasks/{task['id']}/revert", json={}).json()["reverted"] == 1
    assert client.post(f"/api/tasks/{task['id']}/skip", json={}).json()["status"] == "skipped"
    assert client.post(f"/api/tasks/{task['id']}/retry", json={}).json()["status"] == "pending"
    assert client.post(f"/api/tasks/{task['id']}/cancel", json={}).json()["status"] == "cancelled"


def test_plan_endpoint_and_websocket(wb, set_plan):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "plan-api"}).json()["id"]
    created = client.post(
        f"/api/projects/{pid}/tasks/plan",
        json={"tasks": [{"key": "a", "title": "A"}, {"key": "b", "title": "B", "depends_on": ["a"]}]},
    ).json()
    assert len(created) == 2
    kanban = client.get(f"/api/projects/{pid}/kanban").json()
    assert len(kanban["edges"]) == 1

    with client.websocket_connect(f"/ws?project={pid}") as ws:
        message = ws.receive_json()
        assert message["type"] in ("events", "progress", "run_status", "stream.snapshot")
