"""企业级生产运维回归：配置校验、备份恢复、事件链、槽位、漂移、清理、指标、日志。"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from multiagent.agents.proc import cap_text_bytes
from multiagent.api.app import create_api
from multiagent.config import Config, validate_config
from multiagent.db import Database
from multiagent.logging_setup import log_path
from multiagent.orchestrator.slots import SlotService
from multiagent.tasks.service import TaskStatus
from multiagent.util import now_iso
from multiagent.workspace.isolation import WorkspaceDriftError


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


# --------------------------------------------------------------------------- 配置


def test_config_json_corruption_is_reported_not_silently_defaulted(home: Path):
    (home / "config").mkdir(parents=True, exist_ok=True)
    (home / "config" / "agents.json").write_text("{not json", encoding="utf-8")
    cfg = Config.load(home)
    assert cfg.errors
    assert "agents.json" in cfg.errors[0]
    # 仍然能启动（回退默认值），但错误必须可见
    assert cfg.agent("codex")


def test_config_validation_rejects_duplicate_and_broken_agents():
    errors = validate_config(
        "agents.json",
        {
            "agents": [
                {"id": "a", "protocol": "cli", "command": "x"},
                {"id": "a", "protocol": "cli", "command": "y"},
                {"id": "b", "protocol": "nope", "command": "z"},
                {"id": "c", "protocol": "cli"},
            ]
        },
    )
    joined = "\n".join(errors)
    assert "重复" in joined
    assert "protocol" in joined
    assert "缺少 command" in joined


def test_config_api_redacts_secrets_and_preserves_them_on_save(wb):
    client = _client(wb)
    files = client.get("/api/config/files").json()
    assert files["secrets_redacted"] is True
    agents = files["files"]["agents.json"]
    worker = next(item for item in agents["agents"] if item["id"] == "worker")
    worker.setdefault("env", {})["OPENAI_API_KEY"] = "sk-real-secret"
    assert client.put("/api/config/files/agents.json", json={"content": agents}).status_code == 200

    masked = client.get("/api/config/files").json()["files"]["agents.json"]
    masked_worker = next(item for item in masked["agents"] if item["id"] == "worker")
    assert masked_worker["env"]["OPENAI_API_KEY"] == "••••••"

    # 把带掩码的内容原样保存，磁盘上的真实密钥不能被覆盖成掩码
    assert client.put("/api/config/files/agents.json", json={"content": masked}).status_code == 200
    disk = json.loads((wb.config.config_dir / "agents.json").read_text(encoding="utf-8"))
    disk_worker = next(item for item in disk["agents"] if item["id"] == "worker")
    assert disk_worker["env"]["OPENAI_API_KEY"] == "sk-real-secret"


# --------------------------------------------------------------------------- 数据


def test_database_backup_and_hot_restore(wb, tmp_path: Path):
    root = tmp_path / "external-ws"
    root.mkdir()
    project = wb.projects.create("backup-demo", root_path=root)
    task = wb.tasks.create(project["id"], "会被恢复回来的任务")
    backup = wb.ops.create_backup("unit-test")
    assert Path(backup["path"]).is_file()
    assert backup["sha256"]

    wb.tasks.delete(task["id"])
    wb.projects.delete(project["id"])
    assert wb.projects.find("backup-demo") is None

    result = wb.ops.restore(backup["path"], backup_current=True)
    assert result["schema_version"] == wb.db.schema_version
    assert wb.projects.find("backup-demo")["id"] == project["id"]
    assert wb.tasks.find(task["id"])["title"] == "会被恢复回来的任务"
    assert wb.db.integrity_check()["ok"] is True


def test_schema_migration_creates_pre_migration_backup(tmp_path: Path):
    path = tmp_path / "migrate.sqlite3"
    db = Database(path)
    db.execute("PRAGMA user_version = 1")
    db.close()
    reopened = Database(path)
    try:
        assert reopened.schema_version >= 3
        versions = {row["version"] for row in reopened.query("SELECT version FROM schema_migrations")}
        assert {2, 3} <= versions
        assert list((tmp_path / "backups").glob("pre-migration-v1-*.sqlite3"))
    finally:
        reopened.close()


def test_event_hash_chain_supports_multiple_project_chains(wb):
    project_a = wb.projects.create("chain-a")
    project_b = wb.projects.create("chain-b")
    wb.board.append_event(project_id=project_a["id"], kind="x", summary="a1")
    wb.board.append_event(project_id=project_b["id"], kind="x", summary="b1")
    wb.board.append_event(project_id=None, kind="system", summary="global")
    wb.board.append_event(project_id=project_a["id"], kind="x", summary="a2")
    assert wb.board.verify_chain() is True
    assert wb.board.verify_chain(project_a["id"]) is True
    assert wb.board.verify_chain(project_b["id"]) is True

    row = wb.db.query_one("SELECT id FROM events WHERE project_id = ? ORDER BY id LIMIT 1", (project_a["id"],))
    wb.db.execute("UPDATE events SET hash = 'tampered' WHERE id = ?", (row["id"],))
    assert wb.board.verify_chain(project_a["id"]) is False


def test_stop_project_does_not_rewrite_completed_history(wb, project):
    done = wb.tasks.create(project["id"], "已经完成")
    wb.tasks.set_status(done["id"], TaskStatus.SUCCESS.value)
    running = wb.tasks.create(project["id"], "正在执行")
    wb.tasks.set_status(running["id"], TaskStatus.RUNNING.value)
    wb.db.insert(
        "runs",
        {
            "id": "run-stop-test",
            "project_id": project["id"],
            "task_id": running["id"],
            "agent_id": "worker",
            "status": "running",
            "started_at": now_iso(),
        },
    )
    result = wb.engine.stop_project(project["id"])
    assert result["tasks_cancelled"] == 1
    assert wb.tasks.get(done["id"])["status"] == TaskStatus.SUCCESS.value
    assert wb.tasks.get(running["id"])["status"] == TaskStatus.CANCELLED.value


def test_cross_process_scheduler_slots(wb, project):
    first = wb.engine.slots.acquire(
        project_id=project["id"], task_id="t1", global_limit=1, per_project_limit=1
    )
    assert first
    second_db = Database(wb.config.db_path)
    try:
        second = SlotService(second_db, wb.config)
        assert second.acquire(
            project_id=project["id"], task_id="t2", global_limit=1, per_project_limit=1
        ) is None
        wb.engine.slots.release(first)
        slot = second.acquire(
            project_id=project["id"], task_id="t2", global_limit=1, per_project_limit=1
        )
        assert slot
        second.release(slot)
    finally:
        second_db.close()


def test_run_task_respects_global_concurrency(wb, project, set_plan):
    set_plan("worker", {"exit": 0})
    task = wb.tasks.create(project["id"], "并发受限", agent_id="worker")
    held = [
        wb.engine.slots.acquire(
            project_id=project["id"], task_id=f"held{i}", global_limit=4, per_project_limit=2
        )
        for i in range(2)
    ]
    assert all(held)
    blocked = wb.engine.run_task(task["id"], agent_id="worker")
    assert blocked.status == TaskStatus.BLOCKED.value
    assert wb.tasks.get(task["id"])["status"] == TaskStatus.PENDING.value
    for slot in held:
        wb.engine.slots.release(slot)
    outcome = wb.engine.run_task(task["id"], agent_id="worker")
    assert outcome.status == TaskStatus.SUCCESS.value


def test_workspace_drift_blocks_auto_commit(wb, project, tmp_path: Path):
    root = Path(project["root_path"])
    (root / "note.txt").write_text("base", encoding="utf-8")
    task = wb.tasks.create(project["id"], "写 note")
    row = wb.tasks.get(task["id"])
    workdir = wb.isolation.prepare(project, row)
    baseline_dir = wb.isolation.baseline_dir(project["id"], task["id"])
    (Path(workdir) / "note.txt").write_text("agent", encoding="utf-8")
    changes = wb.isolation.changes(
        wb.isolation.baseline(baseline_dir), wb.isolation.current(Path(workdir))
    )
    # 用户在 Agent 跑的同时手动改了同一个文件
    (root / "note.txt").write_text("user-edit", encoding="utf-8")
    with pytest.raises(WorkspaceDriftError):
        wb.isolation.commit_back(project, Path(workdir), changes, scope=["**"])
    assert (root / "note.txt").read_text(encoding="utf-8") == "user-edit"


def test_task_delete_cascades_related_rows(wb, project):
    task = wb.tasks.create(project["id"], "待删除")
    wb.board.post(project["id"], type="note", title="task note", task_id=task["id"])
    wb.guard.request(
        project_id=project["id"], kind="general", title="approval", task_id=task["id"]
    )
    wb.db.insert(
        "file_changes",
        {
            "project_id": project["id"],
            "task_id": task["id"],
            "run_id": "r",
            "agent_id": "worker",
            "rel_path": "x.txt",
            "action": "create",
            "mode": "committed",
            "at": now_iso(),
        },
    )
    wb.db.insert(
        "runs",
        {
            "id": "run-delete-test",
            "project_id": project["id"],
            "task_id": task["id"],
            "agent_id": "worker",
            "status": "success",
            "started_at": now_iso(),
        },
    )
    wb.tasks.delete(task["id"])
    for table in ("board_entries", "approvals", "file_changes", "runs"):
        row = wb.db.query_one(f"SELECT COUNT(*) AS c FROM {table} WHERE task_id = ?", (task["id"],))
        assert int(row["c"]) == 0, table
    assert wb.tasks.find(task["id"]) is None


def test_config_reload_clears_adapter_cache(wb):
    client = _client(wb)
    _ = wb.runner.adapter("worker")
    assert "worker" in wb.runner._adapters
    files = client.get("/api/config/files").json()["files"]
    assert client.put("/api/config/files/agents.json", json={"content": files["agents.json"]}).status_code == 200
    assert "worker" not in wb.runner._adapters


def test_plan_cycle_is_rejected_and_rolled_back(wb, project):
    from multiagent.cli import _apply_plan

    before = len(wb.tasks.list(project["id"]))
    with pytest.raises(ValueError, match="循环依赖"):
        _apply_plan(
            wb,
            project["id"],
            {
                "tasks": [
                    {"key": "a", "title": "A", "depends_on": ["b"]},
                    {"key": "b", "title": "B", "depends_on": ["a"]},
                ]
            },
        )
    assert len(wb.tasks.list(project["id"])) == before


# --------------------------------------------------------------------------- 观测


def test_metrics_health_and_request_id(wb):
    client = _client(wb)
    response = client.get("/api/metrics")
    assert response.status_code == 200
    assert "multiagent_up 1" in response.text
    assert "multiagent_tasks" in response.text
    assert response.headers.get("X-Request-ID")

    health = client.get("/api/health?deep=1").json()
    assert health["ok"] is True
    assert health["database"]["ok"] is True
    assert health["schema_version"] == wb.db.schema_version

    storage = client.get("/api/system/storage").json()
    assert storage["db"]["schema_version"] == wb.db.schema_version
    backup = client.post("/api/system/backup", json={"label": "api-test"}).json()
    assert Path(backup["path"]).is_file()
    assert client.get("/api/system/backups").json()
    verify = client.get("/api/system/verify").json()
    assert verify["ok"] is True
    preview = client.post("/api/system/maintenance", json={"apply": False}).json()
    assert preview["dry_run"] is True
    assert client.post("/api/system/maintenance", json={"apply": True}).status_code == 400


def test_structured_log_written_as_json(wb):
    logger = logging.getLogger("multiagent")
    logger.info("unit-log-marker", extra={"status": "ok"})
    path = log_path()
    assert path is not None and path.is_file()
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if "unit-log-marker" in line]
    assert lines
    payload = json.loads(lines[-1])
    assert payload["message"] == "unit-log-marker"
    assert payload["level"] == "INFO"


def test_output_cap_keeps_valid_utf8():
    text = "中文" * 10000
    capped = cap_text_bytes(text, 1000)
    assert len(capped.encode("utf-8")) <= 1000
    assert "输出已截断" in capped


def test_maintenance_prunes_old_run_evidence(wb, project):
    task = wb.tasks.create(project["id"], "旧任务")
    wb.tasks.set_status(task["id"], TaskStatus.SUCCESS.value)
    wb.db.execute(
        "UPDATE tasks SET finished_at = ?, updated_at = ? WHERE id = ?",
        ("2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00.000Z", task["id"]),
    )
    run_dir = Path(wb.config.runtime_dir) / project["id"] / "tasks" / task["id"] / "runs" / "run-old"
    (run_dir / "workspace").mkdir(parents=True)
    (run_dir / "workspace" / "big.bin").write_bytes(b"x" * 2048)
    (run_dir / "prompt.txt").write_text("p", encoding="utf-8")
    wb.db.insert(
        "runs",
        {
            "id": "run-old",
            "project_id": project["id"],
            "task_id": task["id"],
            "agent_id": "worker",
            "status": "success",
            "started_at": "2020-01-01T00:00:00.000Z",
            "finished_at": "2020-01-01T00:00:00.000Z",
            "prompt_path": str(run_dir / "prompt.txt"),
        },
    )
    preview = wb.ops.prune(
        dry_run=True,
        keep_runs_per_task=0,
        keep_days=0,
        keep_snapshots=20,
        keep_backups=20,
        prune_workspaces_days=0,
        prune_blobs=False,
    )
    assert preview["runs"]["pruned_workspaces"]
    assert (run_dir / "workspace").exists()
    wb.ops.prune(
        dry_run=False,
        keep_runs_per_task=0,
        keep_days=0,
        keep_snapshots=20,
        keep_backups=20,
        prune_workspaces_days=0,
        prune_blobs=False,
    )
    assert not (run_dir / "workspace").exists()
    row = wb.db.query_one("SELECT evidence_pruned FROM runs WHERE id = 'run-old'")
    assert int(row["evidence_pruned"]) == 1
