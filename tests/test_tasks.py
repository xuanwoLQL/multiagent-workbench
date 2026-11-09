"""任务：模板、依赖 DAG、状态机、产物。"""

from __future__ import annotations

from multiagent.tasks.service import TaskStatus


def test_template_applies_defaults(wb, project):
    task = wb.tasks.create(project["id"], "写个文档", template_id="write-doc")
    assert task["kind"] == "doc"
    assert task["isolation"] == "copy"
    assert task["role"] == "writer"
    assert task["acceptance"]  # report-written 展开
    assert task["acceptance"][0]["type"] == "file_exists"


def test_dependencies_and_ready(wb, project):
    a = wb.tasks.create(project["id"], "A")
    b = wb.tasks.create(project["id"], "B", depends_on=[a["id"]])
    assert [t["id"] for t in wb.tasks.ready_tasks(project["id"])] == [a["id"]]
    blocked = wb.tasks.blocked_tasks(project["id"])
    assert blocked[0]["id"] == b["id"]
    wb.tasks.set_status(a["id"], TaskStatus.SUCCESS.value)
    ready = [t["id"] for t in wb.tasks.ready_tasks(project["id"])]
    assert b["id"] in ready


def test_cycle_detection(wb, project):
    import pytest

    a = wb.tasks.create(project["id"], "A")
    b = wb.tasks.create(project["id"], "B", depends_on=[a["id"]])
    # 成环的边必须在入口被拒绝，而不是写进库让调度器永远卡住
    with pytest.raises(ValueError, match="环"):
        wb.tasks.add_edge(a["id"], b["id"])
    assert wb.tasks.has_cycle(project["id"]) is None


def test_plan_levels(wb, project):
    a = wb.tasks.create(project["id"], "A")
    b = wb.tasks.create(project["id"], "B", depends_on=[a["id"]])
    c = wb.tasks.create(project["id"], "C", depends_on=[a["id"]])
    d = wb.tasks.create(project["id"], "D", depends_on=[b["id"], c["id"]])
    plan = wb.tasks.plan(project["id"])
    assert plan["levels"][0] == [a["id"]]
    assert set(plan["levels"][1]) == {b["id"], c["id"]}
    assert plan["levels"][2] == [d["id"]]
    assert len(plan["edges"]) == 4


def test_progress_counters(wb, project):
    a = wb.tasks.create(project["id"], "A")
    b = wb.tasks.create(project["id"], "B")
    wb.tasks.set_status(a["id"], TaskStatus.SUCCESS.value)
    progress = wb.tasks.progress(project["id"])
    assert progress["total"] == 2
    assert progress["counters"].get("success") == 1
    assert progress["counters"].get("pending") == 1


def test_add_plan_file(wb, project):
    from multiagent.cli import _apply_plan

    created = _apply_plan(
        wb,
        project["id"],
        {
            "tasks": [
                {"key": "step1", "title": "第一步", "outputs": ["out.txt"]},
                {"key": "step2", "title": "第二步", "depends_on": ["step1"]},
            ]
        },
    )
    assert len(created) == 2
    deps = wb.tasks.dependencies(created[1]["id"])
    assert deps and deps[0]["id"] == created[0]["id"]


def test_artifact_recording(wb, project, tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "out.md").write_text("# hi", encoding="utf-8")
    artifact = wb.tasks.record_artifact(
        project_id=project["id"], task_id="t1", run_id=None, role="output", path=root / "out.md", root=root
    )
    assert artifact["rel_path"] == "out.md"
    assert artifact["sha256"]
    assert wb.tasks.artifacts(task_id="t1")[0]["id"] == artifact["id"]
