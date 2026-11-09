"""桌面控制台新增接口的测试：Agent 详情 / 任务编辑删除与依赖 / 配置读写 / 项目与黑板删除。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def test_agents_detail(wb):
    client = _client(wb)
    items = client.get("/api/agents/detail").json()
    ids = {a["id"] for a in items}
    assert {"worker", "coder", "human"} <= ids
    worker = next(a for a in items if a["id"] == "worker")
    assert worker["enabled"] is True
    assert worker["protocol"] == "cli"
    assert "usage" in worker and "quota" in worker


def test_task_edit_delete_and_deps(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "ctrl"}).json()["id"]
    a = client.post(f"/api/projects/{pid}/tasks", json={"title": "A"}).json()
    b = client.post(f"/api/projects/{pid}/tasks", json={"title": "B"}).json()

    patched = client.patch(f"/api/tasks/{b['id']}", json={"title": "B2", "priority": 5, "tags": ["x", "y"]}).json()
    assert patched["title"] == "B2"
    assert patched["priority"] == 5
    assert patched["tags"] == ["x", "y"]

    deps = client.post(f"/api/tasks/{b['id']}/deps", json={"depends_on": a["id"]}).json()
    assert [d["id"] for d in deps] == [a["id"]]
    assert client.post(f"/api/tasks/{b['id']}/deps", json={"depends_on": b["id"]}).status_code == 400
    assert client.post(f"/api/tasks/{b['id']}/deps", json={"depends_on": "nope"}).status_code == 400

    left = client.delete(f"/api/tasks/{b['id']}/deps/{a['id']}").json()
    assert left == []

    client.delete(f"/api/tasks/{a['id']}")
    assert client.get(f"/api/tasks/{a['id']}").status_code == 404


def test_config_files_roundtrip(wb):
    client = _client(wb)
    files = client.get("/api/config/files").json()
    assert {"agents.json", "policy.json", "routing.json", "acceptance.json", "templates.json"} <= set(files["files"])

    agents = files["files"]["agents.json"]
    agents["agents"].append({"id": "extra", "label": "Extra", "protocol": "cli", "enabled": False})
    saved = client.put("/api/config/files/agents.json", json={"content": agents}).json()
    assert saved["saved"] == "agents.json"

    detail = client.get("/api/agents/detail").json()
    extra = next(a for a in detail if a["id"] == "extra")
    assert extra["enabled"] is False

    assert client.put("/api/config/files/agents.json", json={"content": "{bad json"}).status_code == 400
    assert client.put("/api/config/files/nope.json", json={"content": {}}).status_code == 400
    assert client.post("/api/config/reload", json={}).status_code == 200


def test_board_and_project_delete(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "del"}).json()["id"]
    entry = client.post(f"/api/projects/{pid}/board", json={"type": "note", "title": "临时"}).json()
    client.delete(f"/api/board/{entry['id']}")
    assert client.get(f"/api/board/{entry['id']}").status_code == 404

    assert client.delete(f"/api/projects/{pid}").json()["id"] == pid
    assert client.get(f"/api/projects/{pid}").status_code == 404


def test_fs_list_and_stop(wb, home):
    client = _client(wb)
    listing = client.get(f"/api/fs/list?path={home}").json()
    assert listing["path"]
    assert "config" in [d["name"] for d in listing["dirs"]]
    assert client.get("/api/fs/list?path=Z:\\nope\\nope").status_code in (400, 403)

    pid = client.post("/api/projects", json={"name": "stop"}).json()["id"]
    assert client.post(f"/api/projects/{pid}/stop", json={}).json()["stopped"] is True


def test_project_runs_endpoint(wb, set_plan):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "runs"}).json()["id"]
    set_plan("worker", {"files": {"r.txt": "1"}, "exit": 0})
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "R", "agent_id": "worker"}).json()
    client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60})
    runs = client.get(f"/api/projects/{pid}/runs").json()
    assert runs and runs[0]["task_id"] == task["id"]


def test_task_move_and_route(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "move"}).json()["id"]
    a = client.post(f"/api/projects/{pid}/tasks", json={"title": "A"}).json()
    b = client.post(f"/api/projects/{pid}/tasks", json={"title": "B"}).json()
    c = client.post(f"/api/projects/{pid}/tasks", json={"title": "C"}).json()

    def order():
        return [t["title"] for t in sorted(client.get(f"/api/projects/{pid}/tasks").json(), key=lambda x: x["ordering"])]

    assert order() == ["A", "B", "C"]
    client.post(f"/api/tasks/{c['id']}/move", json={"direction": "up"})
    assert order() == ["A", "C", "B"]
    client.post(f"/api/tasks/{a['id']}/move", json={"direction": "down"})
    assert order() == ["C", "A", "B"]
    client.post(f"/api/tasks/{a['id']}/move", json={"direction": "up"})
    assert order() == ["A", "C", "B"]

    route = client.get(f"/api/tasks/{a['id']}/route").json()
    assert "reason" in route and route["agent_id"] != ""


def test_project_artifacts_and_recover(wb, set_plan):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "art"}).json()["id"]
    set_plan("worker", {"files": {"out.txt": "hi"}, "exit": 0})
    task = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "有产物", "agent_id": "worker", "outputs": ["out.txt"]},
    ).json()
    client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60})
    arts = client.get(f"/api/projects/{pid}/artifacts").json()
    assert any(x["rel_path"] == "out.txt" and x["role"] == "output" for x in arts)
    outputs = client.get(f"/api/projects/{pid}/artifacts?role=output").json()
    assert outputs and all(x["role"] == "output" for x in outputs)

    rec = client.post(f"/api/projects/{pid}/recover", json={}).json()
    assert "recovered" in rec


def test_snapshot_two_way_diff(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "snapdiff"}).json()["id"]
    project = client.get(f"/api/projects/{pid}").json()["project"]
    from pathlib import Path

    root = Path(project["root_path"])
    (root / "a.txt").write_text("v1", encoding="utf-8")
    s1 = client.post(f"/api/projects/{pid}/snapshots", json={"label": "v1"}).json()
    (root / "a.txt").write_text("v2", encoding="utf-8")
    (root / "b.txt").write_text("new", encoding="utf-8")
    s2 = client.post(f"/api/projects/{pid}/snapshots", json={"label": "v2"}).json()
    diff = client.get(f"/api/snapshots/{s1['id']}/diff/{s2['id']}").json()
    assert isinstance(diff, dict)
    assert diff  # 两个快照间确有差异


def test_run_output_and_config_kinds(wb, set_plan):
    client = _client(wb)
    assert "kinds" in client.get("/api/config").json()

    pid = client.post("/api/projects", json={"name": "out"}).json()["id"]
    set_plan("worker", {"files": {"o.txt": "hello"}, "exit": 0})
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "O", "agent_id": "worker"}).json()
    client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60})

    run = client.get(f"/api/projects/{pid}/runs").json()[0]
    out = client.get(f"/api/runs/{run['id']}/output").json()
    assert set(out) >= {"run", "prompt", "stdout", "stderr"}
    assert out["run"]["agent_id"] == "worker"
    assert client.get("/api/runs/nope/output").status_code == 404


def test_event_hub_buffer_and_snapshot():
    from multiagent.api.app import EventHub

    hub = EventHub()
    hub.publish({"type": "agent.update", "project_id": "p1", "kind": "agent_message_chunk", "text": "hi"})
    hub.publish({"type": "agent.update", "project_id": "p2", "kind": "agent_message_chunk", "text": "yo"})
    assert [e["text"] for e in hub.snapshot("p1")] == ["hi"]
    merged = hub.snapshot(None)
    assert len(merged) == 2
    assert merged[0]["seq"] < merged[1]["seq"]
    assert all("at" in e for e in merged)


def test_runner_emits_agent_stream_events(wb, set_plan):
    events: list[dict] = []
    wb.runner.event_sink = events.append
    pid = wb.projects.create("stream")["id"]
    set_plan("coder", {"files": {"s.txt": "x"}, "exit": 0})
    task = wb.tasks.create(pid, "流式任务", agent_id="coder")
    wb.engine.run_task(task["id"])

    updates = [e for e in events if e.get("type") == "agent.update"]
    assert updates, "应至少收到一个 agent.update 事件"
    assert all(e.get("project_id") == pid and e.get("task_id") == task["id"] for e in updates)
    assert any(e.get("kind") == "agent_message_chunk" and e.get("text") for e in updates)


def test_task_file_diff(wb, set_plan):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "diff"}).json()["id"]
    project = client.get(f"/api/projects/{pid}").json()["project"]
    from pathlib import Path

    root = Path(project["root_path"])
    (root / "d.txt").write_text("line1\n", encoding="utf-8")
    set_plan("worker", {"files": {"d.txt": "line1\nline2\n"}, "exit": 0})
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "改文件", "agent_id": "worker"}).json()
    client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60})

    diff = client.get(f"/api/tasks/{task['id']}/diff?path=d.txt").json()
    assert diff["action"] == "modify"
    assert "+line2" in diff["diff"]
    assert client.get(f"/api/tasks/{task['id']}/diff?path=none.txt").status_code == 404


def test_project_delete_prunes_managed_workspace(wb):
    client = _client(wb)
    project = client.post("/api/projects", json={"name": "prune-me"}).json()
    from pathlib import Path

    root = Path(project["root_path"])
    (root / "junk.txt").write_text("x", encoding="utf-8")
    assert root.exists()
    client.delete(f"/api/projects/{project['id']}")
    assert not root.exists()


def test_task_approve_and_sync_endpoint(wb, project, set_plan):
    """API 等价路径：失败但验收通过 → 人工 approve + 手动同步。"""
    client = _client(wb)
    set_plan("worker", {"files": {"approved.txt": "ok"}, "exit": 3, "handoff": {"did": "已写出产物"}})
    task = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={
            "title": "失败但可人工验收",
            "agent_id": "worker",
            "acceptance": [{"type": "file_exists", "path": "approved.txt"}],
            "outputs": ["approved.txt"],
        },
    ).json()
    outcome = client.post(f"/api/tasks/{task['id']}/run", json={"wait": 30}).json()
    assert outcome["status"] == "failed"
    root = Path(project["root_path"])
    assert not (root / "approved.txt").exists()

    approved = client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"sync": True, "note": "人工复核通过"},
    ).json()
    assert approved["task"]["status"] == "success"
    assert approved["task"]["last_error"] is None
    assert approved["task"]["result"]["human_approved"] is True
    assert approved["task"]["result"]["acceptance"]["human_override"] is True
    assert "approved.txt" in approved["sync"]["committed"]
    assert (root / "approved.txt").read_text(encoding="utf-8") == "ok"

    again = client.post(f"/api/tasks/{task['id']}/sync", json={}).json()
    assert again["committed"] == [], "重复同步不应重复写盘"
    assert again["already_synced"] >= 1

    # 未执行 / 正在执行的任务不允许人工验收
    pending = client.post(f"/api/projects/{project['id']}/tasks", json={"title": "还没跑"}).json()
    assert client.post(f"/api/tasks/{pending['id']}/approve", json={"sync": False}).status_code == 409
