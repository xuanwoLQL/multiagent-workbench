"""任务工作副本查看 + 手动同步 API 测试（task-11）。

真实现场：``~/myworld`` 里任务以 ``isolation=copy`` 跑，项目根目录只有
``.multiagent``，52 个产物都在 runtime 隔离副本里，用户以为丢了。这里锁定：

* ``GET /api/tasks/{tid}/workspace`` 能把「产物在哪 / 待同步什么 / 能不能同步」说清楚；
* ``POST /api/tasks/{tid}/sync`` 能把工作副本里的改动按 write_scope / 保护路径规则
  手动回收进项目根目录，且**不改变任务状态**（running 也允许）；
* shared 模式返回 ``already_shared``；baseline 缺失时 ``can_sync=false`` 且同步 409；
  read_only 任务同步 400。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from multiagent.api.app import create_api

REPO_ROOT = Path(__file__).resolve().parents[1]


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _fake_cli(agent_id: str, plan_file: Path) -> dict:
    return {
        "id": agent_id,
        "label": agent_id.title(),
        "protocol": "cli",
        "cli": {
            "command": sys.executable,
            "args": ["-m", "multiagent.testing.fake_agent"],
            "prompt_mode": "stdin",
        },
        "env": {"PYTHONPATH": str(REPO_ROOT), "FAKE_PLAN_FILE": str(plan_file)},
        "roles": ["worker"],
        "strengths": ["code"],
        "can_write": True,
        "enabled": True,
    }


@pytest.fixture
def wb_manual(make_wb, plan_writer, home):
    """``sync_back_on_accept=False``：产物留在工作副本里（复刻用户现场）。"""
    plan_writer("worker", {})
    return make_wb(
        [_fake_cli("worker", home / "plan-worker.json")],
        policy_extra={
            "isolation": {
                "default": "copy",
                "sync_back_on_accept": False,
                "block_conflict": True,
                "revert_out_of_scope": True,
            }
        },
    )


def _project(client: TestClient, name: str = "工作副本") -> dict:
    return client.post("/api/projects", json={"name": name}).json()


def _prepare(wb, project: dict, **kwargs):
    """手工准备「任务工作副本 + 运行前基线」，模拟任务已执行但结果还没回收。"""
    task = wb.tasks.create(
        project["id"], kwargs.pop("title", "任务"), agent_id="worker", **kwargs
    )
    workdir = Path(wb.isolation.prepare(project, task))
    return wb.tasks.get(task["id"]), workdir


# ===========================================================================
# 1) 端到端：Agent 产物留在副本里 → workspace 能看到 → 手动 sync 回根目录
# ===========================================================================


def test_agent_output_visible_in_workspace_and_synced_back(wb_manual, set_plan):
    client = _client(wb_manual)
    project = _project(client, "myworld")
    set_plan(
        "worker",
        {"files": {"out/report.md": "# 竞品分析\n"}, "handoff": {"did": "写完报告"}, "exit": 0},
    )
    task = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={
            "title": "写竞品分析",
            "kind": "generic",
            "agent_id": "worker",
            "outputs": ["out/report.md"],
        },
    ).json()
    run = client.post(f"/api/tasks/{task['id']}/run", json={"wait": 60}).json()
    assert run["status"] == "success", run

    root = Path(project["root_path"])
    assert not (root / "out/report.md").exists(), "sync_back_on_accept=False 时不应自动回收"

    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["task_id"] == task["id"]
    assert workspace["project_id"] == project["id"]
    assert workspace["isolation"] == "copy"
    assert workspace["root_path"] == str(root)
    assert workspace["workdir_exists"] is True
    assert workspace["is_original"] is False
    assert workspace["running"] is False
    assert workspace["can_sync"] is True
    assert workspace["sync_blocked_reason"] is None

    pending_files = {item["rel_path"]: item for item in workspace["pending"]["files"]}
    assert "out/report.md" in pending_files
    assert pending_files["out/report.md"]["action"] == "create"
    assert pending_files["out/report.md"]["size"] > 0
    assert workspace["pending"]["count"] >= 1
    assert len(workspace["pending"]["files"]) <= 200
    # unsynced = workdir vs 项目根目录：产物还没落到根目录 → 待同步 1
    assert workspace["unsynced"]["count"] == 1
    assert workspace["unsynced"]["files"][0]["rel_path"] == "out/report.md"
    assert workspace["unsynced"]["files"][0]["action"] == "create"

    # 产物确实在隔离副本里（用户以为丢了）
    workdir = Path(workspace["workdir"])
    assert workdir.is_dir() and (workdir / "out/report.md").exists()
    assert any(item["rel_path"] == "out/report.md" for item in workspace["artifacts"])

    synced = client.post(f"/api/tasks/{task['id']}/sync", json={}).json()
    assert synced["committed"] == ["out/report.md"]
    assert synced["skipped"] == []
    assert synced["already_shared"] is False
    assert "已同步 1 个文件" in synced["message"]
    assert (root / "out/report.md").read_text(encoding="utf-8") == "# 竞品分析\n"

    # 同步之后：pending 仍是 1（baseline vs workdir 不变），unsynced 归零（根目录已一致）
    after = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert after["pending"]["count"] == 1
    assert after["unsynced"]["count"] == 0

    # 事件流有 task.synced；任务状态不变
    events = client.get(f"/api/projects/{project['id']}/events").json()
    assert any(event["kind"] == "task.synced" for event in events)
    assert client.get(f"/api/tasks/{task['id']}").json()["task"]["status"] == "success"


# ===========================================================================
# 2) write_scope 外不回收 + dry_run 不落盘
# ===========================================================================


def test_sync_only_commits_files_inside_write_scope(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "范围")
    task, workdir = _prepare(
        wb_manual, project, title="只许写 out/", write_scope=["out/**"]
    )
    (workdir / "out").mkdir(parents=True, exist_ok=True)
    (workdir / "out" / "ok.txt").write_text("ok", encoding="utf-8")
    (workdir / "other.txt").write_text("no", encoding="utf-8")
    root = Path(project["root_path"])

    dry = client.post(f"/api/tasks/{task['id']}/sync", json={"dry_run": True}).json()
    assert dry["dry_run"] is True
    assert dry["committed"] == []
    assert "out/ok.txt" in dry["planned"]
    assert "other.txt" in dry["out_of_scope"]
    assert "other.txt" not in dry["planned"]
    assert not (root / "out/ok.txt").exists(), "dry_run 不能落盘"

    result = client.post(f"/api/tasks/{task['id']}/sync", json={}).json()
    assert result["committed"] == ["out/ok.txt"]
    assert "other.txt" in result["out_of_scope"]
    assert "other.txt" in result["skipped"]
    details = {item["rel_path"]: item["reason"] for item in result["skipped_details"]}
    assert "write_scope" in details["other.txt"]
    assert (root / "out/ok.txt").read_text(encoding="utf-8") == "ok"
    assert not (root / "other.txt").exists()


def test_sync_without_changes_reports_clean_message(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "无改动")
    task, _workdir = _prepare(wb_manual, project, title="什么都没写")
    result = client.post(f"/api/tasks/{task['id']}/sync", json={}).json()
    assert result["committed"] == []
    assert result["skipped"] == []
    assert "没有改动" in result["message"]


# ===========================================================================
# 2b) unsynced = workdir vs 项目根目录（UI 的「待同步 N」）
# ===========================================================================


def test_unsynced_tracks_files_not_yet_in_root(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "待同步计数")
    task, workdir = _prepare(wb_manual, project, title="产物待同步")
    (workdir / "out").mkdir(parents=True, exist_ok=True)
    (workdir / "out" / "a.txt").write_text("hello", encoding="utf-8")
    root = Path(project["root_path"])

    before = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert before["pending"]["count"] == 1
    assert before["unsynced"]["count"] == 1
    assert before["unsynced"]["files"] == [
        {"rel_path": "out/a.txt", "action": "create", "size": 5}
    ]

    assert client.post(f"/api/tasks/{task['id']}/sync", json={}).json()["committed"] == [
        "out/a.txt"
    ]

    after = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert after["pending"]["count"] == 1, "pending 是 baseline vs workdir，同步后仍保留"
    assert after["unsynced"]["count"] == 0
    assert after["unsynced"]["files"] == []

    # 根目录里「多出来」的文件不算待同步
    (root / "user-extra.txt").write_text("mine", encoding="utf-8")
    assert client.get(f"/api/tasks/{task['id']}/workspace").json()["unsynced"]["count"] == 0


def test_unsynced_detects_root_modified_after_sync(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "根目录被改")
    task, workdir = _prepare(wb_manual, project, title="同步后再改根目录")
    (workdir / "x.txt").write_text("v1", encoding="utf-8")
    client.post(f"/api/tasks/{task['id']}/sync", json={})
    assert client.get(f"/api/tasks/{task['id']}/workspace").json()["unsynced"]["count"] == 0

    # 用户手工改了根目录里的同名文件 → 又变成「待同步」
    (Path(project["root_path"]) / "x.txt").write_text("v2-different", encoding="utf-8")
    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["unsynced"]["count"] == 1
    assert workspace["unsynced"]["files"][0]["rel_path"] == "x.txt"
    assert workspace["unsynced"]["files"][0]["action"] == "modify"


def test_unsynced_reports_pending_delete(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "删除待同步")
    root = Path(project["root_path"])
    (root / "old.txt").write_text("old", encoding="utf-8")
    task, workdir = _prepare(wb_manual, project, title="删掉旧文件")
    (workdir / "old.txt").unlink()

    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["pending"]["count"] == 1
    assert workspace["unsynced"]["count"] == 1
    assert workspace["unsynced"]["files"][0] == {
        "rel_path": "old.txt",
        "action": "delete",
        "size": 0,
    }

    assert client.post(f"/api/tasks/{task['id']}/sync", json={}).json()["committed"] == [
        "old.txt"
    ]
    assert not (root / "old.txt").exists()
    assert client.get(f"/api/tasks/{task['id']}/workspace").json()["unsynced"]["count"] == 0


# ===========================================================================
# 3) shared 模式：already_shared
# ===========================================================================


def test_shared_mode_returns_already_shared(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "共享")
    task = wb_manual.tasks.create(
        project["id"], "共享任务", isolation="shared", agent_id="worker"
    )
    wb_manual.isolation.prepare(project, task)  # shared 返回项目根目录

    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["isolation"] == "shared"
    assert workspace["is_original"] is True
    assert workspace["can_sync"] is False
    assert "shared" in workspace["sync_blocked_reason"]
    assert workspace["pending"]["count"] == 0, "shared 模式不应扫描整个工作区"

    result = client.post(f"/api/tasks/{task['id']}/sync", json={}).json()
    assert result["already_shared"] is True
    assert result["committed"] == []
    assert "无需同步" in result["message"]


# ===========================================================================
# 4) baseline 缺失：can_sync=false + 同步 409
# ===========================================================================


def test_missing_baseline_blocks_sync(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "无基线")
    task, workdir = _prepare(wb_manual, project, title="基线被清理")
    (workdir / "x.txt").write_text("x", encoding="utf-8")
    shutil.rmtree(wb_manual.isolation.baseline_dir(project["id"], task["id"]))

    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["workdir_exists"] is True
    assert workspace["can_sync"] is False
    assert "基线" in workspace["sync_blocked_reason"]

    res = client.post(f"/api/tasks/{task['id']}/sync", json={})
    assert res.status_code == 409
    assert res.json()["error_code"] == "conflict"
    assert "基线" in res.json()["error"]


def test_never_run_task_has_no_workdir(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "没跑过")
    task = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={"title": "还没执行", "isolation": "copy", "agent_id": "worker"},
    ).json()
    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["workdir_exists"] is False
    assert workspace["can_sync"] is False
    assert "工作副本不存在" in workspace["sync_blocked_reason"]
    assert client.post(f"/api/tasks/{task['id']}/sync", json={}).status_code == 409


# ===========================================================================
# 5) running 允许同步且不改任务状态；read_only 拒绝
# ===========================================================================


def test_running_task_can_sync_without_status_change(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "运行中")
    task, workdir = _prepare(wb_manual, project, title="边跑边看")
    (workdir / "live.txt").write_text("live", encoding="utf-8")
    wb_manual.tasks.set_status(task["id"], "running")

    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["running"] is True
    assert workspace["can_sync"] is True

    result = client.post(f"/api/tasks/{task['id']}/sync", json={}).json()
    assert result["committed"] == ["live.txt"]
    assert (Path(project["root_path"]) / "live.txt").read_text(encoding="utf-8") == "live"
    # 同步不改变任务状态
    assert client.get(f"/api/tasks/{task['id']}").json()["task"]["status"] == "running"


def test_read_only_task_sync_is_rejected(wb_manual):
    client = _client(wb_manual)
    project = _project(client, "只读")
    task, _workdir = _prepare(wb_manual, project, title="只读审阅", read_only=True)

    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["can_sync"] is False
    assert "只读" in workspace["sync_blocked_reason"]

    res = client.post(f"/api/tasks/{task['id']}/sync", json={})
    assert res.status_code == 400
    assert res.json()["error_code"] == "invalid_request"
    assert "只读" in res.json()["error"]


# ===========================================================================
# 6) 边界：未知任务 404
# ===========================================================================


def test_unknown_task_is_404(wb_manual):
    client = _client(wb_manual)
    assert client.get("/api/tasks/nope/workspace").status_code == 404
    assert client.post("/api/tasks/nope/sync", json={}).status_code == 404
