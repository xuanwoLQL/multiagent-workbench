"""P1：默认合并策略必须是「先审后合」（git.auto_merge=false）。

2026-11-08 起，产品默认改为：
- 验收通过只在 ``ma/<task>`` 分支提交；
- 不自动 merge 回基线，等待人工在任务详情/审阅页合并；
- 需要 DAG 连续交付的项目显式设置 ``git.auto_merge=true``。

这些测试锁住默认值，防止后续配置/代码把默认悄悄改回自动合并。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from multiagent import presets
from multiagent.app import Workbench
from multiagent.config import Config


def test_preset_default_is_review_first():
    assert presets.DEFAULT_POLICY["git"]["auto_merge"] is False


def test_repo_policy_default_is_review_first():
    repo = Path(__file__).resolve().parents[1]
    policy = json.loads((repo / "config" / "policy.json").read_text(encoding="utf-8"))
    assert policy["git"]["auto_merge"] is False


def test_config_missing_git_policy_falls_back_to_review_first(tmp_path: Path):
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    # 不写 policy.json：Config 应使用内置默认值。
    cfg = Config.load(home)
    assert cfg.policy_get("git.auto_merge", "missing") is False


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def test_runner_default_keeps_branch_unmerged(wb, tmp_path: Path, set_plan):
    from multiagent.gitops import worktree_branch_exists

    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("review-first", root_path=str(root))
    set_plan(
        "worker",
        {"files": {"feature.txt": "hello\n"}, "handoff": {"did": "完成"}, "exit": 0},
    )
    task = wb.tasks.create(
        project["id"],
        "Review first",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "file_exists", "path": "feature.txt"}],
    )

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error

    stored = wb.tasks.get(task["id"])
    git_meta = stored["result"]["git"]
    assert git_meta["commit"], "任务分支仍必须真实提交"
    assert git_meta["auto_merge"] is False
    assert git_meta["merge"]["skipped"] is True
    assert git_meta["merge"]["reason"] == "auto_merge_disabled"
    assert worktree_branch_exists(root, git_meta["branch"]), "任务分支必须保留"
    assert not (root / "feature.txt").exists(), "默认策略不得自动合并到基线"


def test_explicit_auto_merge_true_still_merges(wb, tmp_path: Path, set_plan):
    """DAG 连续交付必须仍可通过显式配置开启。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("dag-fast", root_path=str(root))
    wb.config.policy_raw.setdefault("git", {})["auto_merge"] = True
    set_plan(
        "worker",
        {"files": {"feature.txt": "hello\n"}, "handoff": {"did": "完成"}, "exit": 0},
    )
    task = wb.tasks.create(
        project["id"],
        "DAG fast path",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "file_exists", "path": "feature.txt"}],
    )

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    stored = wb.tasks.get(task["id"])
    assert stored["result"]["git"]["merge"]["merged"] is True
    assert (root / "feature.txt").read_text(encoding="utf-8") == "hello\n"


def test_default_workbench_config_file_is_loaded(tmp_path: Path):
    """真实 Workbench 打开空 home 时也应使用先审后合默认值。"""
    home = tmp_path / "wb-home"
    (home / "config").mkdir(parents=True)
    wb = Workbench.open(home)
    try:
        assert wb.config.policy_get("git.auto_merge", "missing") is False
    finally:
        wb.close()


def test_string_false_is_not_treated_as_auto_merge(wb, tmp_path: Path, set_plan):
    """手工 JSON 写 "false" 时，Runner 不能 bool("false")=True 自动合并。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("string-false", root_path=str(root))
    wb.config.policy_raw.setdefault("git", {})["auto_merge"] = "false"
    set_plan(
        "worker",
        {"files": {"feature.txt": "hello\n"}, "handoff": {"did": "完成"}, "exit": 0},
    )
    task = wb.tasks.create(
        project["id"],
        "String false",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "file_exists", "path": "feature.txt"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    git_meta = wb.tasks.get(task["id"])["result"]["git"]
    assert git_meta["auto_merge"] is False
    assert git_meta["merge"]["skipped"] is True
    assert not (root / "feature.txt").exists()


def test_unmerged_predecessor_blocks_dependent_until_merge(
    wb, tmp_path: Path, set_plan
):
    """默认先审后合：A 未合并时 B 不得被派发；人工合并后 B 才 ready。"""
    from fastapi.testclient import TestClient

    from multiagent.api.app import create_api

    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("dag-review-first", root_path=str(root))
    set_plan(
        "worker",
        {"files": {"a.txt": "A\n"}, "handoff": {"did": "A"}, "exit": 0},
    )
    a = wb.tasks.create(
        project["id"],
        "A",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "file_exists", "path": "a.txt"}],
    )
    b = wb.tasks.create(
        project["id"],
        "B",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        depends_on=[a["id"]],
        acceptance=[{"type": "always"}],
    )
    outcome = wb.engine.run_task(a["id"])
    assert outcome.status == "success", outcome.error
    assert wb.tasks.get(a["id"])["result"]["git"]["merge"]["merged"] is False

    ready_ids = {task["id"] for task in wb.tasks.ready_tasks(project["id"])}
    assert b["id"] not in ready_ids, "前置未合并时下游绝不能 ready"
    blocked = {task["id"]: task for task in wb.tasks.blocked_tasks(project["id"])}
    assert b["id"] in blocked
    assert a["id"] in blocked[b["id"]]["pending_deps"]

    client = TestClient(create_api(wb))
    merged = client.post(f"/api/tasks/{a['id']}/merge")
    assert merged.status_code == 200, merged.text
    ready_ids = {task["id"] for task in wb.tasks.ready_tasks(project["id"])}
    assert b["id"] in ready_ids, "前置合并后下游必须恢复 ready"


def test_require_merged_for_dependents_can_be_disabled(wb, tmp_path: Path, set_plan):
    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("dag-fast-opt-in", root_path=str(root))
    wb.config.policy_raw.setdefault("git", {})["require_merged_for_dependents"] = False
    set_plan("worker", {"files": {"a.txt": "A\n"}, "handoff": {"did": "A"}, "exit": 0})
    a = wb.tasks.create(
        project["id"], "A", kind="generic", agent_id="worker",
        isolation="worktree", acceptance=[{"type": "file_exists", "path": "a.txt"}],
    )
    b = wb.tasks.create(
        project["id"], "B", kind="generic", agent_id="worker",
        isolation="worktree", depends_on=[a["id"]], acceptance=[{"type": "always"}],
    )
    assert wb.engine.run_task(a["id"]).status == "success"
    ready_ids = {task["id"] for task in wb.tasks.ready_tasks(project["id"])}
    assert b["id"] in ready_ids
