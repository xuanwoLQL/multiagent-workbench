"""Git 交付生命周期：worktree 分支提交 + 真实 ``git merge``。

覆盖：
- ``commit_worktree`` / ``merge_worktree_branch`` 的真实 git 行为；
- runner 在 worktree 任务成功后自动提交，并把 sha 写进 ``result["git"]``；
- ``POST /api/tasks/{tid}/merge`` 做 --no-ff 合并，冲突时 abort 且仓库保持干净；
- ``GET /api/tasks/{tid}/commit`` 读取任务提交信息；
- 非 git 项目仍然退化为 copy 模式，原有文件回收不受影响。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api
from multiagent.gitops import (
    commit_worktree,
    create_worktree,
    current_branch,
    delete_worktree_branch,
    is_repo,
    merge_worktree_branch,
    remove_worktree,
    worktree_branch_exists,
)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


# --------------------------------------------------------------------------- gitops 直接测试


def test_commit_creates_real_commit_and_merge_lands_files(tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    work = tmp_path / "wt"
    info = create_worktree(root, work, task_id="task-abc12345", title="Feature One")
    assert is_repo(work)

    (work / "feature.txt").write_text("hello\n", encoding="utf-8")
    committed = commit_worktree(work, "task(abc12345): Feature One", "Bot", "bot@example.com")
    assert committed["sha"], "必须产生真实 commit"
    assert "feature.txt" in committed["files"]
    assert worktree_branch_exists(root, info["branch"])
    subject = _git(root, "log", "-1", "--format=%s", info["branch"]).stdout.strip()
    assert subject == "task(abc12345): Feature One"

    # 没有改动时是幂等的：不报错、不产生空 commit。
    again = commit_worktree(work, "task(abc12345): Feature One", "Bot", "bot@example.com")
    assert again == {"sha": None, "files": []}

    merged = merge_worktree_branch(root, info["branch"], "merge feature")
    assert merged["merged"] is True
    assert merged["conflict"] is False
    assert merged["sha"]
    assert (root / "feature.txt").read_text(encoding="utf-8") == "hello\n"
    # --no-ff 必须留下真正的 merge commit（两个 parent）。
    parents = _git(root, "rev-list", "--parents", "-n", "1", "HEAD").stdout.split()
    assert len(parents) == 3

    remove_worktree(root, work, branch=info["branch"], delete_branch=False)
    assert delete_worktree_branch(root, info["branch"]) is True
    assert worktree_branch_exists(root, info["branch"]) is False


def test_merge_conflict_aborts_and_leaves_repo_clean(tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    work = tmp_path / "wt"
    info = create_worktree(root, work, task_id="task-conflict1", title="Conflict")

    (work / "README.md").write_text("branch side\n", encoding="utf-8")
    assert commit_worktree(work, "task(conflict1): branch", "Bot", "bot@example.com")["sha"]

    (root / "README.md").write_text("main side\n", encoding="utf-8")
    _git(root, "commit", "-qam", "main change")

    merged = merge_worktree_branch(root, info["branch"], "merge conflict")
    assert merged["merged"] is False
    assert merged["conflict"] is True
    assert "README.md" in merged["files"]

    # abort 之后不能残留 UU/AA，也不能还处在 merge 中。
    status = _git(root, "status", "--porcelain").stdout
    assert "UU " not in status and "AA " not in status and "DD " not in status
    probe = _git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False)
    assert probe.returncode != 0
    assert (root / "README.md").read_text(encoding="utf-8") == "main side\n"


# --------------------------------------------------------------------------- runner 端到端


def _git_project(wb, root: Path):
    _init_repo(root)
    return wb.projects.create("git-demo", root_path=str(root))


def test_runner_commits_worktree_and_merge_route_lands_branch(wb, tmp_path: Path, set_plan):
    root = tmp_path / "repo"
    project = _git_project(wb, root)
    # 默认策略已是「先审后合」；本用例验证自动合并路径，显式开启。
    wb.config.policy_raw.setdefault("git", {})["auto_merge"] = True
    set_plan(
        "worker",
        {"files": {"feature.txt": "hello from branch\n"}, "handoff": {"did": "完成功能"}, "exit": 0},
    )
    task = wb.tasks.create(
        project["id"],
        "Feature task",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "file_exists", "path": "feature.txt"}],
    )

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error

    stored = wb.tasks.get(task["id"])
    git_meta = stored["result"]["git"]
    assert git_meta["commit"], "worktree 任务成功后必须落一个真实 commit"
    assert git_meta["branch"].startswith("ma/")
    assert git_meta["base"]
    # 显式开启 policy.git.auto_merge=true：验收通过即合并回基线，保证依赖它的
    # 后续任务能在最新的树上工作；默认是关闭（先审后合）。
    assert (root / "feature.txt").exists(), "worktree 交付默认应在验收后合并回基线"
    assert any(
        event.get("kind") == "task.git.committed"
        for event in wb.board.read_events(project["id"])
    )

    client = _client(wb)
    commit = client.get(f"/api/tasks/{task['id']}/commit").json()
    assert commit["available"] is True
    assert commit["sha"] == git_meta["commit"]
    assert commit["branch"] == git_meta["branch"]
    assert "feature.txt" in commit["files"]

    merged = client.post(f"/api/tasks/{task['id']}/merge")
    assert merged.status_code == 200, merged.text
    body = merged.json()
    assert body["merged"] is True
    assert body["mode"] == "git"
    assert body["branch"] == git_meta["branch"]
    assert body["sha"]
    assert (root / "feature.txt").read_text(encoding="utf-8") == "hello from branch\n"


def test_merge_route_conflict_returns_409_and_aborts(wb, tmp_path: Path):
    root = tmp_path / "repo"
    project = _git_project(wb, root)
    task = wb.tasks.create(
        project["id"],
        "Conflict task",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "always"}],
    )
    work = Path(wb.runner.isolation.prepare(project, wb.tasks.get(task["id"])))
    assert is_repo(work)
    assert current_branch(work) == f"ma/{task['id'].split('-')[-1]}-conflict-task"

    (work / "README.md").write_text("branch side\n", encoding="utf-8")
    assert commit_worktree(work, "task(x): branch", "Bot", "bot@example.com")["sha"]
    (root / "README.md").write_text("main side\n", encoding="utf-8")
    _git(root, "commit", "-qam", "main change")

    res = _client(wb).post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["conflict"] is True
    assert "README.md" in body["files"]

    status = _git(root, "status", "--porcelain").stdout
    assert "UU " not in status and "AA " not in status
    assert (root / "README.md").read_text(encoding="utf-8") == "main side\n"


def test_non_git_project_keeps_copy_back_fallback(wb, tmp_path: Path, set_plan):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "seed.txt").write_text("seed", encoding="utf-8")
    project = wb.projects.create("plain-demo", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = wb.tasks.create(
        project["id"],
        "Plain task",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    assert (root / "out.txt").read_text(encoding="utf-8") == "ok"
    assert "git" not in wb.tasks.get(task["id"])["result"], "非 git 项目不应产生 git 交付元数据"
