"""命令行接口测试：确认对外接口稳定可用（需求 52）。"""

from __future__ import annotations

import json
from pathlib import Path

from multiagent.cli import main


def run_cli(home: Path, *argv: str) -> int:
    return main(["--home", str(home), *argv])


def _json(capsys) -> object:
    out = capsys.readouterr().out
    return json.loads(out)


def test_init_and_config(home, capsys):
    assert run_cli(home, "init") == 0
    payload = _json(capsys)
    assert Path(payload["db"]).exists()
    assert run_cli(home, "config") == 0
    summary = _json(capsys)
    assert "agents" in summary


def test_project_and_task_lifecycle(wb, home, capsys, set_plan):
    set_plan("worker", {"files": {"cli.txt": "ok"}, "handoff": {"did": "CLI 完成"}, "exit": 0})
    assert run_cli(home, "project", "create", "cli项目") == 0
    project = _json(capsys)
    assert project["name"] == "cli项目"

    assert run_cli(home, "task", "add", "cli项目", "写点东西", "--agent", "worker", "--acceptance-ref", "file-exists") == 0
    task = _json(capsys)
    assert task["status"] == "pending"

    assert run_cli(home, "task", "run-project", "cli项目") == 0
    summary = _json(capsys)
    assert summary["success"] == 1

    assert run_cli(home, "task", "status", "cli项目") == 0
    assert _json(capsys)["counters"]["success"] == 1

    assert run_cli(home, "task", "show", task["id"]) == 0
    detail = _json(capsys)
    assert detail["handoff"]["did"] == "CLI 完成"


def test_task_plan_file(wb, home, capsys, tmp_path: Path):
    assert run_cli(home, "project", "create", "plan项目") == 0
    _json(capsys)
    spec = tmp_path / "plan.json"
    spec.write_text(
        json.dumps({"tasks": [{"key": "a", "title": "A"}, {"key": "b", "title": "B", "depends_on": ["a"]}]}),
        encoding="utf-8",
    )
    assert run_cli(home, "task", "add-plan", "plan项目", "--file", str(spec)) == 0
    created = _json(capsys)
    assert len(created) == 2
    assert run_cli(home, "task", "plan", "plan项目") == 0
    plan = _json(capsys)
    assert len(plan["edges"]) == 1


def test_board_and_handoff_and_stats(wb, home, capsys, set_plan):
    set_plan("worker", {"files": {"x.txt": "1"}, "handoff": {"did": "结束"}, "exit": 0})
    assert run_cli(home, "project", "create", "obs项目") == 0
    _json(capsys)
    assert run_cli(home, "board", "post", "obs项目", "--type", "note", "--title", "记一笔") == 0
    entry = _json(capsys)
    assert entry["title"] == "记一笔"
    assert run_cli(home, "board", "list", "obs项目") == 0
    assert len(_json(capsys)) >= 1

    assert run_cli(home, "task", "add", "obs项目", "跑一下", "--agent", "worker", "--acceptance-ref", "file-exists") == 0
    _json(capsys)
    assert run_cli(home, "task", "run-project", "obs项目") == 0
    _json(capsys)

    assert run_cli(home, "handoff", "list", "obs项目") == 0
    handoffs = _json(capsys)
    assert handoffs[0]["did"] == "结束"

    assert run_cli(home, "stats", "--project", "obs项目") == 0
    stats = _json(capsys)
    assert stats["totals"]["runs"] >= 1

    assert run_cli(home, "runs", "--project", "obs项目") == 0
    assert len(_json(capsys)) >= 1

    out = home / "timeline.md"
    assert run_cli(home, "timeline", "--project", "obs项目", "--out", str(out)) == 0
    _json(capsys)
    assert out.exists() and "协作时间线" in out.read_text(encoding="utf-8")


def test_agent_list_and_approvals(wb, home, capsys):
    assert run_cli(home, "agent", "list") == 0
    agents = _json(capsys)
    assert any(a["id"] == "worker" for a in agents)
    assert run_cli(home, "approval", "list") == 0
    assert isinstance(_json(capsys), list)


def test_global_json_flag_can_appear_anywhere(home, capsys):
    """回归：--json 曾被「remove 后再 insert 回原位」写成死循环。"""
    assert run_cli(home, "project", "list", "--json") == 0
    assert isinstance(_json(capsys), list)
    assert run_cli(home, "--json", "project", "list") == 0
    assert isinstance(_json(capsys), list)
    assert run_cli(home, "project", "list", "--json", "--json") == 0
    assert isinstance(_json(capsys), list)


def test_task_approve_syncs_failed_but_acceptable_run(wb, home, capsys, set_plan):
    """人工验收路径：Agent 退出码非 0 但产物/验收通过时，可人工批准并手动同步。"""
    set_plan("worker", {"files": {"approved.txt": "ok"}, "exit": 3, "handoff": {"did": "已写出产物"}})
    assert run_cli(home, "project", "create", "approve项目") == 0
    project = _json(capsys)
    assert (
        run_cli(
            home, "task", "add", "approve项目", "失败但可人工验收",
            "--agent", "worker", "--acceptance-ref", "file-exists", "--outputs", "approved.txt",
        )
        == 0
    )
    task = _json(capsys)
    # 失败任务的 CLI 退出码是 1（可脚本化判断），stdout 仍是完整 outcome
    assert run_cli(home, "task", "run", task["id"]) == 1
    outcome = _json(capsys)
    assert outcome["status"] == "failed"
    root = Path(project["root_path"])
    assert not (root / "approved.txt").exists(), "失败任务不应自动回写"

    assert run_cli(home, "task", "approve", task["id"], "--sync", "--note", "人工复核通过") == 0
    approved = _json(capsys)
    assert approved["task"]["status"] == "success"
    assert approved["task"]["last_error"] is None
    assert approved["task"]["result"]["human_approved"] is True
    assert approved["task"]["result"]["acceptance"]["human_override"] is True
    assert "人工验收" in approved["task"]["result"]["acceptance"]["summary"]
    assert approved["sync"]["committed"] == ["approved.txt"]
    assert (root / "approved.txt").read_text(encoding="utf-8") == "ok"


def test_task_sync_read_only_reports_clean_error(wb, home, capsys):
    project = wb.projects.create("sync-guard")
    task = wb.tasks.create(
        project["id"], "只读任务", kind="review", agent_id="worker", read_only=True
    )
    assert run_cli(home, "task", "sync", task["id"]) == 2
    captured = capsys.readouterr()
    assert "只读" in captured.err
    assert "Traceback" not in captured.err
