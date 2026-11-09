"""大型文件型项目的编排回归：超时、验收、拆解、隔离与卫生。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api
from multiagent.config import Config
from multiagent.tasks.profiles import acceptance_template, kind_defaults
from multiagent.tasks.runtime import resolve_timeout
from multiagent.workspace.changes import effective_excludes, snapshot_tree
from multiagent.workspace.hygiene import scan_workspace

STAGES = [
    "requirements-freeze",
    "sim-core",
    "web-3d",
    "diagrams",
    "ppt",
    "docs",
    "review",
    "fix",
    "final-qa",
]


def _client(wb) -> TestClient:  # noqa: ANN001
    return TestClient(create_api(wb))


def _new_project(client: TestClient, name: str) -> str:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def test_config_has_large_project_defaults_and_templates(tmp_path: Path):
    cfg = Config.load(tmp_path)
    code = kind_defaults(cfg, "code")
    doc = kind_defaults(cfg, "doc")
    assert code["timeout_sec"] >= 1800
    assert doc["timeout_sec"] >= 1800
    assert code["max_attempts"] == 3
    assert doc["max_attempts"] == 3
    assert {"code", "doc", "ppt", "diagrams", "web", "review"} <= set(cfg.acceptance_templates)
    for kind in ("code", "doc", "ppt", "diagrams", "web", "review"):
        assert acceptance_template(cfg, kind)["checks"]
    assert cfg.agent("codex")["timeout_sec"] >= 2400


def test_legacy_data_dir_config_is_upgraded_in_memory(tmp_path: Path):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "defaults": {"timeout_sec": 900},
                "agents": [{"id": "codex", "protocol": "cli", "command": "codex"}],
            }
        ),
        encoding="utf-8",
    )
    (cfg_dir / "policy.json").write_text(
        json.dumps(
            {
                "version": 1,
                "default_timeout_sec": 900,
                "kinds": {
                    "code": {"isolation": "copy", "acceptance_ref": "file-changed"},
                    "doc": {"isolation": "copy", "acceptance_ref": "file-exists"},
                },
            }
        ),
        encoding="utf-8",
    )
    (cfg_dir / "acceptance.json").write_text(
        json.dumps({"version": 1, "rules": {"file-exists": [{"type": "file_exists", "path": "{{target}}"}]}}),
        encoding="utf-8",
    )
    (cfg_dir / "templates.json").write_text(
        json.dumps(
            {
                "version": 1,
                "templates": [
                    {
                        "id": "write-doc",
                        "kind": "doc",
                        "acceptance_ref": "file-exists",
                        "timeout_sec": 1200,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cfg = Config.load(tmp_path)
    assert cfg.policy_get("default_timeout_sec") == 1800
    assert cfg.policy_get("retry.timeout_multiplier") == 1.5
    assert kind_defaults(cfg, "doc")["timeout_sec"] == 2400
    assert cfg.agent("codex")["timeout_sec"] == 3600
    assert "kind_templates" in cfg.acceptance_raw
    assert cfg.template("write-doc")["timeout_sec"] == 2400
    assert cfg.template("make-ppt") is not None


def test_timeout_budget_escalates_on_retry(wb):
    task = wb.tasks.create(
        wb.projects.create("timeout-budget")["id"],
        "timeout",
        kind="code",
        agent_id="worker",
        timeout_sec=2000,
        acceptance=[{"type": "always"}],
    )
    assert wb.runner.resolve_timeout(task, "worker", attempt=1) == 2000
    assert wb.runner.resolve_timeout(task, "worker", attempt=2) == 3000
    assert wb.runner.resolve_timeout(task, "worker", attempt=3) == 3600


def test_run_records_effective_timeout_and_task_runtime(wb, project, set_plan):
    set_plan("worker", {"exit": 0})
    task = wb.tasks.create(
        project["id"],
        "记录超时",
        kind="code",
        agent_id="worker",
        timeout_sec=123,
        acceptance=[{"type": "always"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success"
    run = wb.audit.runs(task_id=task["id"])[0]
    assert run["timeout_sec"] == 123
    detail = wb.tasks.get(task["id"])
    assert detail["runtime"]["base_timeout_sec"] == 123
    assert detail["runtime"]["next_attempt_timeout_sec"] >= 123


def test_quicktask_sets_timeout_attempts_copy_and_effective_acceptance(wb):
    client = _client(wb)
    pid = _new_project(client, "quick-large")
    body = client.post(
        f"/api/projects/{pid}/quick-task",
        json={
            "text": "实现 Python 仿真核心并补充测试",
            "kind": "code",
            "agent_id": "worker",
            "outputs": ["src/app.py", "tests/test_app.py"],
            "verify_command": "python -m compileall -q src",
            "isolation": "shared",
        },
    ).json()
    task = client.get(f"/api/tasks/{body['tasks'][0]['id']}").json()["task"]
    assert task["timeout_sec"] >= 1800
    assert task["max_attempts"] == 3
    assert task["isolation"] == "copy"
    assert task["write_scope"] == ["src/**", "tests/**"]
    assert task["result"]["verify_command"] == "python -m compileall -q src"
    assert any("shared" in warning for warning in task["result"]["creation_warnings"])
    effective = {check["type"] for check in task["acceptance_effective"]}
    assert {"file_exists", "file_nonempty", "command_exit_zero"} <= effective


def test_quicktask_without_outputs_keeps_legacy_shape_but_runtime_is_strict(wb):
    client = _client(wb)
    pid = _new_project(client, "quick-unverified")
    body = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": "看看这个仓库有什么问题", "agent_id": "worker"},
    ).json()
    task = client.get(f"/api/tasks/{body['tasks'][0]['id']}").json()["task"]
    # 对外字段保持兼容（旧 UI/API 约定），执行期则由 acceptance_effective 补齐。
    assert task["acceptance"] == [{"type": "exit_zero"}]
    assert task["result"]["acceptance_guard"] == "artifact_or_handoff"
    assert body["warnings"]
    effective = {check["type"] for check in task["acceptance_effective"]}
    assert {"file_modified", "handoff_present"} <= effective


def test_api_rejects_exit_zero_only_without_explicit_confirmation(wb):
    client = _client(wb)
    pid = _new_project(client, "exit-zero")
    payload = {"title": "无证据任务", "acceptance": [{"type": "exit_zero"}]}
    assert client.post(f"/api/projects/{pid}/tasks", json=payload).status_code == 400
    assert client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "空验收", "acceptance": []},
    ).status_code == 400
    created = client.post(
        f"/api/projects/{pid}/tasks",
        json={**payload, "confirm_exit_zero_only": True},
    )
    assert created.status_code == 200
    task = created.json()
    assert {check["type"] for check in task["acceptance_effective"]} >= {
        "exit_zero",
        "file_modified",
        "handoff_present",
    }


def test_decompose_preview_and_apply_builds_nine_stage_dag(wb, project):
    root = Path(project["root_path"])
    (root / "pdf_text.txt").write_text("pages: 2\n需求：并发调度与三维可视化", encoding="utf-8")
    (root / "requirements.txt").write_text("必须实现调度器、图表、PPT 和文档", encoding="utf-8")
    client = _client(wb)

    preview = client.post(
        f"/api/projects/{project['id']}/decompose/preview",
        json={"text": "完成 PDF 中的无人机仿真项目全部要求"},
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()["plan"]
    assert [item["key"] for item in plan["tasks"]] == STAGES
    assert client.get(f"/api/projects/{project['id']}/tasks").json() == []
    assert {item["path"] for item in plan["source_files"]} >= {"pdf_text.txt", "requirements.txt"}

    by_key = {item["key"]: item for item in plan["tasks"]}
    assert by_key["sim-core"]["depends_on"] == ["requirements-freeze"]
    assert set(by_key["ppt"]["depends_on"]) == {"web-3d", "diagrams"}
    assert set(by_key["docs"]["depends_on"]) == {"ppt", "web-3d", "diagrams"}
    for key in ("requirements-freeze", "sim-core", "web-3d", "diagrams", "ppt", "docs", "fix"):
        assert by_key[key]["write_scope"]
        assert by_key[key]["timeout_sec"] >= 1800
        assert by_key[key]["max_attempts"] == 3
        assert by_key[key]["acceptance"]
    assert by_key["sim-core"]["verify_command"]
    assert by_key["review"]["read_only"] is True
    assert by_key["final-qa"]["read_only"] is True

    applied = client.post(
        f"/api/projects/{project['id']}/decompose/apply",
        json={"plan": plan},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["created"] == 9
    tasks = client.get(f"/api/projects/{project['id']}/tasks").json()
    assert len(tasks) == 9
    assert all(task["status"] == "pending" for task in tasks)
    assert sum(
        len(client.get(f"/api/tasks/{task['id']}").json()["dependencies"])
        for task in tasks
    ) == 11


def test_task_timeout_endpoint_updates_budget(wb):
    client = _client(wb)
    pid = _new_project(client, "timeout-api")
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "T"}).json()
    res = client.post(f"/api/tasks/{task['id']}/timeout", json={"timeout_sec": 3333})
    assert res.status_code == 200
    assert res.json()["task"]["timeout_sec"] == 3333
    assert res.json()["applies_to"] in {"next_attempt", "next_run"}
    # 验收要求：非运行态也必须明确说“下一次尝试”，不能只写“下次运行”。
    assert "下一次" in res.json()["message"]


def test_workspace_hygiene_excludes_temp_and_supports_project_ignore(tmp_path: Path):
    root = tmp_path / "ws"
    (root / ".edge-tmp").mkdir(parents=True)
    (root / ".edge-tmp" / "a.txt").write_text("junk", encoding="utf-8")
    (root / ".edge-tmp2").mkdir()
    (root / ".edge-tmp2" / "b.txt").write_text("junk", encoding="utf-8")
    (root / "build" / "edge-profile").mkdir(parents=True)
    (root / "build" / "edge-profile" / "c.txt").write_text("junk", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "x.pyc").write_bytes(b"bytecode")
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "a.js").write_text("dep", encoding="utf-8")
    (root / "keep.md").write_text("# deliverable", encoding="utf-8")
    (root / "custom.generated").write_text("ignore me", encoding="utf-8")
    (root / ".multiagentignore").write_text("*.generated\n", encoding="utf-8")

    excludes = effective_excludes(root)
    manifest = snapshot_tree(root, excludes=excludes)
    assert "keep.md" in manifest
    assert "custom.generated" not in manifest
    assert not any(path.startswith((".edge-tmp", "__pycache__", "node_modules")) for path in manifest)
    assert "build/edge-profile/c.txt" not in manifest

    report = scan_workspace(root)
    assert report["ok"] is False
    assert report["temporary_files"] >= 3
    assert report["temporary_dirs"] >= 3
    assert {item["path"] for item in report["temporary"]} >= {
        ".edge-tmp/a.txt",
        ".edge-tmp2/b.txt",
        "__pycache__/x.pyc",
    }
    assert report["deliverable_files"] == 2  # keep.md + .multiagentignore


def test_runner_reports_missing_outputs_and_keeps_workdir(wb, project, set_plan):
    set_plan("worker", {"exit": 0})
    task = wb.tasks.create(
        project["id"],
        "缺失产物",
        kind="doc",
        agent_id="worker",
        outputs=["docs/missing.md"],
        acceptance=[{"type": "file_exists", "path": "docs/missing.md"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "failed"
    saved = wb.tasks.get(task["id"])
    assert saved["result"]["missing_outputs"] == ["docs/missing.md"]
    assert "缺失产物" in (saved["last_error"] or "")
    assert Path(saved["workdir"]).is_dir()


def test_runner_doc_nonempty_and_exit_zero_only_are_enforced(wb, project, set_plan):
    root = Path(project["root_path"])
    set_plan("worker", {"files": {"report.md": ""}, "exit": 0})
    doc = wb.tasks.create(
        project["id"],
        "空文档",
        kind="doc",
        agent_id="worker",
        outputs=["report.md"],
        acceptance=[{"type": "file_exists", "path": "report.md"}],
    )
    assert wb.engine.run_task(doc["id"]).status == "failed"

    set_plan("worker", {"exit": 0})
    unverified = wb.tasks.create(
        project["id"],
        "只有退出码",
        kind="generic",
        agent_id="worker",
        acceptance=[{"type": "exit_zero"}],
    )
    assert wb.engine.run_task(unverified["id"]).status == "failed"
    assert not (root / "report.md").exists()
