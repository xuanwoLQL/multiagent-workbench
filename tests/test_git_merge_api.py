"""P0-3：合并冲突的结构化 API + discard-worktree 分支删除。

覆盖：
- 脏基线：409 + blocked/dirty_files，且仓库不会进入 merge 状态，用户改动不被破坏；
- 真实冲突：409 + conflict/files，``git merge --abort`` 后无 UU/AA；
- 成功合并：200 + sha/files，文件真实落到基线；
- discard-worktree：默认保留分支，``delete_branch=true`` 时删分支且基线分支仍在；
- worktree 路由的 ``merge_status`` 推导（merged / auto_merge_disabled / conflict / blocked）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from multiagent.api import routes_git
from multiagent.api.app import create_api
from multiagent.gitops import (
    GitError,
    commit_worktree,
    create_worktree,
    current_branch,
    is_repo,
    worktree_branch_exists,
)
from multiagent.gitops.service import head_commit


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


def _git_worktree_task(wb, root: Path, title: str):
    """建项目 + worktree 任务，并用 isolation.prepare 真正创建 worktree。"""
    project = wb.projects.create(f"git-merge-{title}", root_path=str(root))
    task = wb.tasks.create(
        project["id"],
        title,
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "always"}],
    )
    work = Path(wb.runner.isolation.prepare(project, wb.tasks.get(task["id"])))
    assert is_repo(work), "测试前提：worktree 模式必须真正创建 git worktree"
    return project, task, work


def _commit_branch_change(work: Path, rel: str, content: str) -> str:
    (work / rel).write_text(content, encoding="utf-8")
    committed = commit_worktree(work, f"task change {rel}", "Bot", "bot@example.com")
    assert committed["sha"], "测试前提：任务分支必须有真实提交"
    return committed["sha"]


def _record_git_meta(wb, task, branch: str, commit: str, base: str) -> None:
    wb.tasks.update(
        task["id"],
        result={"git": {"branch": branch, "commit": commit, "base": base, "files": []}},
    )


# --------------------------------------------------------------------------- 脏树阻断


def test_merge_blocks_dirty_baseline_without_touching_user_files(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Dirty baseline")
    branch = current_branch(work)
    assert branch
    _commit_branch_change(work, "feature.txt", "branch work\n")

    # 基线有一处已跟踪文件的未提交改动（模拟人类正在编辑）。
    (root / "README.md").write_text("user local edit\n", encoding="utf-8")

    res = _client(wb).post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["merged"] is False
    assert body["blocked"] is True
    assert body["conflict"] is False
    assert body["reason"] == "dirty_worktree"
    assert "README.md" in body["dirty_files"]
    assert body["files"] == []
    assert body["branch"] == branch
    assert body["base"]
    assert body["sha"] is None
    assert "未提交改动" in body["message"]
    assert "已阻止合并" in body["message"]

    # 没有进入 merge 状态，用户改动原样保留。
    probe = _git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False)
    assert probe.returncode != 0
    assert (root / "README.md").read_text(encoding="utf-8") == "user local edit\n"
    status = _git(root, "status", "--porcelain").stdout
    assert "UU " not in status and "AA " not in status
    assert not (root / "feature.txt").exists(), "脏树阻断时不允许把分支内容悄悄落盘"

    events = wb.board.read_events(project["id"])
    assert any(event.get("kind") == "task.merge.blocked" for event in events)


def test_merge_honours_require_clean_policy(make_wb, tmp_path: Path):
    """policy.git.require_clean_tree=false 时必须允许脏基线合并（旧语义保留）。"""
    wb2 = make_wb([], policy_extra={"git": {"require_clean_tree": False}})
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb2, root, "Policy task")
    _commit_branch_change(work, "feature.txt", "branch work\n")
    (root / "README.md").write_text("user local edit\n", encoding="utf-8")

    res = _client(wb2).post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["merged"] is True
    assert body["blocked"] is False
    assert (root / "feature.txt").read_text(encoding="utf-8") == "branch work\n"
    # 未提交的人类改动不会被合并动作吞掉。
    assert (root / "README.md").read_text(encoding="utf-8") == "user local edit\n"


# --------------------------------------------------------------------------- 冲突


def test_merge_conflict_returns_structured_409_and_aborts(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Conflict task")
    branch = current_branch(work)
    assert branch
    _commit_branch_change(work, "README.md", "branch side\n")

    (root / "README.md").write_text("main side\n", encoding="utf-8")
    _git(root, "commit", "-qam", "main change")
    wb.tasks.set_status(task["id"], "success")

    client = _client(wb)
    res = client.post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["merged"] is False
    assert body["blocked"] is False
    assert body["conflict"] is True
    assert body["reason"] == "conflict"
    assert "README.md" in body["files"]
    assert body["branch"] == branch
    assert body["base"]
    assert body["sha"] is None
    assert "git merge --abort" in body["message"]

    status = _git(root, "status", "--porcelain").stdout
    assert "UU " not in status and "AA " not in status and "DD " not in status
    probe = _git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False)
    assert probe.returncode != 0
    assert (root / "README.md").read_text(encoding="utf-8") == "main side\n"

    events = wb.board.read_events(project["id"])
    assert any(event.get("kind") == "task.merge.conflict" for event in events)

    # 冲突后任务不能继续显示 success（Atlas 场景 A）。
    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["status"] != "success"
    assert detail["task"]["status"] == "blocked"
    assert detail["task"]["blocked_reason"] == "merge_conflict"

    # 人工解决冲突后再次合并：状态恢复 success，并记录 merge_resolved。
    (root / "README.md").write_text("branch side\n", encoding="utf-8")
    _git(root, "commit", "-qam", "resolve conflict")
    resolved = client.post(f"/api/tasks/{task['id']}/merge")
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["merged"] is True
    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["status"] == "success"
    assert detail["task"]["blocked_reason"] is None
    assert detail["history"][-1]["reason"] == "merge_resolved"


# --------------------------------------------------------------------------- 成功合并


def test_merge_success_returns_structured_200_and_lands_files(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Success task")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "hello from branch\n")

    res = _client(wb).post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["merged"] is True
    assert body["blocked"] is False
    assert body["conflict"] is False
    assert body["mode"] == "git"
    assert body["branch"] == branch
    assert body["base"]
    assert body["sha"] and body["sha"] != sha  # --no-ff 产生新的 merge commit
    assert "feature.txt" in body["files"]
    assert body["count"] == len(body["files"]) >= 1
    assert (root / "feature.txt").read_text(encoding="utf-8") == "hello from branch\n"

    events = wb.board.read_events(project["id"])
    assert any(event.get("kind") == "task.merged" for event in events)


# --------------------------------------------------------------------------- discard-worktree


def test_discard_worktree_can_delete_task_branch(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    baseline = current_branch(root)
    assert baseline
    project, task, work = _git_worktree_task(wb, root, "Discard task")
    branch = current_branch(work)
    assert branch and branch != baseline

    # 让接口优先采用 result.git.branch 作为删除目标。
    wb.tasks.update(task["id"], result={"git": {"branch": branch}})

    res = _client(wb).post(
        f"/api/tasks/{task['id']}/discard-worktree", json={"delete_branch": True}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["discarded"] is True
    assert body["branch_deleted"] is True
    assert body["branch"] == branch
    assert body["message"]

    assert not work.exists(), "worktree 目录必须被删除"
    assert worktree_branch_exists(root, branch) is False, "任务分支必须被删除"
    assert worktree_branch_exists(root, baseline) is True, "基线分支必须保留"


def test_discard_worktree_keeps_branch_by_default(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Keep branch task")
    branch = current_branch(work)
    assert branch

    res = _client(wb).post(f"/api/tasks/{task['id']}/discard-worktree")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["discarded"] is True
    assert body["branch_deleted"] is False
    assert body["branch"] == branch
    assert not work.exists()
    assert worktree_branch_exists(root, branch) is True, "默认必须保留任务分支"


def test_discard_worktree_deletes_branch_even_without_worktree(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Orphan branch task")
    branch = current_branch(work)
    assert branch

    # 先手工删掉 worktree 目录并 prune，只留下分支（模拟中断现场）。
    _git(root, "worktree", "remove", "--force", str(work))
    _git(root, "worktree", "prune")
    assert not work.exists()
    assert worktree_branch_exists(root, branch) is True

    res = _client(wb).post(
        f"/api/tasks/{task['id']}/discard-worktree", json={"delete_branch": True}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["discarded"] is False
    assert body["branch_deleted"] is True
    assert body["branch"] == branch
    assert worktree_branch_exists(root, branch) is False


def test_discard_worktree_refuses_current_baseline_branch(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("git-baseline-guard", root_path=str(root))
    task = wb.tasks.create(
        project["id"], "Baseline guard", kind="generic", agent_id="worker", isolation="worktree"
    )
    baseline = current_branch(root)
    assert baseline
    wb.tasks.update(task["id"], result={"git": {"branch": baseline}})

    res = _client(wb).post(
        f"/api/tasks/{task['id']}/discard-worktree", json={"delete_branch": True}
    )
    assert res.status_code == 409, res.text
    assert worktree_branch_exists(root, baseline) is True


# --------------------------------------------------------------------------- merge_status 推导


def _plain_task(wb, tmp_path: Path, name: str):
    root = tmp_path / name
    root.mkdir()
    project = wb.projects.create(name, root_path=str(root))
    task = wb.tasks.create(project["id"], name, kind="generic")
    return task


def test_worktree_route_reports_merge_status(wb, tmp_path: Path):
    task = _plain_task(wb, tmp_path, "status-demo")
    client = _client(wb)

    def status_for(git_meta) -> dict:
        wb.tasks.update(task["id"], result={"git": git_meta} if git_meta is not None else {})
        res = client.get(f"/api/tasks/{task['id']}/worktree")
        assert res.status_code == 200, res.text
        return res.json()

    body = status_for({"branch": "ma/demo", "merge": {"merged": True}})
    assert body["merge_status"] == "merged"
    assert body["git"]["branch"] == "ma/demo"
    # 旧字段保持存在（只增不改）。
    assert "exists" in body and "path" in body and body["task_id"] == task["id"]

    assert status_for({"branch": "ma/demo", "merge": {"merged": False, "conflict": True}})["merge_status"] == "conflict"
    assert status_for({"branch": "ma/demo", "merge": {"merged": False, "blocked": True}})["merge_status"] == "blocked"
    assert (
        status_for(
            {"branch": "ma/demo", "merge": {"merged": False, "reason": "missing_branch"}}
        )["merge_status"]
        == "missing_branch"
    )
    assert (
        status_for(
            {
                "branch": "ma/demo",
                "merge": {"merged": False, "skipped": True, "reason": "auto_merge_disabled"},
            }
        )["merge_status"]
        == "auto_merge_disabled"
    )
    assert status_for({"branch": "ma/demo"})["merge_status"] == "not_merged"
    body = status_for(None)
    assert body["git"] is None
    assert body["merge_status"] is None


# --------------------------------------------------------------------------- missing branch + rebuild


def test_merge_missing_branch_returns_structured_409(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Missing branch")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)

    # 只删 ref，保留 worktree 目录（模拟外部 git update-ref -d）。
    _git(root, "update-ref", "-d", f"refs/heads/{branch}")
    assert work.is_dir()
    assert worktree_branch_exists(root, branch) is False
    wb.tasks.set_status(task["id"], "success")

    client = _client(wb)
    res = client.post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["merged"] is False
    assert body["blocked"] is False
    assert body["conflict"] is False
    assert body["reason"] == "missing_branch"
    assert body["code"] == "missing_branch"
    assert body["branch"] == branch
    assert body["task_id"] == task["id"]
    assert body["base"] == base
    assert body["files"] == []
    assert body["sha"] is None
    assert branch in body["message"]
    assert body["error"]["code"] == "missing_branch"
    assert body["error"]["details"] == {
        "branch": branch,
        "task_id": task["id"],
        "base": base,
    }

    persisted = wb.tasks.get(task["id"])["result"]["git"]
    assert persisted["branch"] == branch
    merge = persisted["merge"]
    assert merge["merged"] is False
    assert merge["blocked"] is False
    assert merge["conflict"] is False
    assert merge["reason"] == "missing_branch"
    assert merge["branch"] == branch
    assert merge["base"] == base
    assert merge["files"] == []
    assert merge["sha"] is None

    events = wb.board.read_events(project["id"])
    missing = [event for event in events if event.get("kind") == "task.merge.missing_branch"]
    assert missing, "分支缺失必须记录 task.merge.missing_branch 事件"
    assert missing[-1]["data"]["branch"] == branch
    assert missing[-1]["data"]["base"] == base
    assert missing[-1]["data"]["task_id"] == task["id"]

    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["status"] == "blocked"
    assert detail["task"]["blocked_reason"] == "merge_missing_branch"


def test_merge_missing_branch_race_maps_giterror_to_structured_409(
    wb, tmp_path: Path, monkeypatch
):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Race branch")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)

    def _race(*_args, **_kwargs):
        raise GitError(f"branch not found: {branch}")

    monkeypatch.setattr(routes_git, "merge_worktree_branch", _race)

    res = _client(wb).post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["reason"] == "missing_branch"
    assert body["code"] == "missing_branch"
    assert body["branch"] == branch
    assert body["task_id"] == task["id"]
    assert body["error"]["code"] == "missing_branch"
    assert wb.tasks.get(task["id"])["result"]["git"]["merge"]["reason"] == "missing_branch"
    assert any(
        event.get("kind") == "task.merge.missing_branch"
        for event in wb.board.read_events(project["id"])
    )


def test_rebuild_branch_requires_confirmation_then_restores_merge(
    wb, tmp_path: Path
):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Rebuild branch")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)
    _git(root, "update-ref", "-d", f"refs/heads/{branch}")
    assert worktree_branch_exists(root, branch) is False
    wb.tasks.set_status(task["id"], "blocked", "merge_missing_branch")

    client = _client(wb)
    unconfirmed = client.post(f"/api/tasks/{task['id']}/rebuild-branch", json={})
    assert unconfirmed.status_code == 400, unconfirmed.text
    assert unconfirmed.json()["error"]["code"] == "confirmation_required"
    assert worktree_branch_exists(root, branch) is False, "未确认时不得修改 git ref"

    rebuilt = client.post(
        f"/api/tasks/{task['id']}/rebuild-branch", json={"confirm": True}
    )
    assert rebuilt.status_code == 200, rebuilt.text
    body = rebuilt.json()
    assert body == {
        "rebuilt": True,
        "branch": branch,
        "base": sha,
        "sha": sha,
        "reused_commit": True,
    }
    assert worktree_branch_exists(root, branch) is True
    assert _git(root, "rev-parse", f"refs/heads/{branch}").stdout.strip() == sha

    persisted = wb.tasks.get(task["id"])["result"]["git"]
    assert persisted["branch"] == branch
    assert persisted["base"] == sha
    assert persisted["commit"] == sha
    assert persisted["merge"] == {
        "merged": False,
        "blocked": False,
        "conflict": False,
        "discarded": False,
        "reason": None,
        "branch": branch,
        "base": sha,
        "sha": None,
        "files": [],
    }
    assert any(
        event.get("kind") == "task.branch.rebuilt"
        for event in wb.board.read_events(project["id"])
    )
    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["status"] == "success"
    assert detail["task"]["blocked_reason"] is None
    assert detail["history"][-1]["reason"] == "branch_rebuilt"

    merged = client.post(f"/api/tasks/{task['id']}/merge")
    assert merged.status_code == 200, merged.text
    assert merged.json()["merged"] is True
    assert (root / "feature.txt").read_text(encoding="utf-8") == "branch work\n"
    assert wb.tasks.get(task["id"])["result"]["git"]["merge"]["merged"] is True


def test_rebuild_branch_rejects_existing_branch(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Existing branch")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)
    assert worktree_branch_exists(root, branch) is True

    res = _client(wb).post(
        f"/api/tasks/{task['id']}/rebuild-branch", json={"confirm": True}
    )
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["error"]["code"] == "branch_exists"
    assert body["error"]["details"]["branch"] == branch
    assert worktree_branch_exists(root, branch) is True


def test_rebuild_branch_recreates_missing_worktree(wb, tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Recreate worktree")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)

    _git(root, "worktree", "remove", "--force", str(work))
    _git(root, "worktree", "prune")
    _git(root, "update-ref", "-d", f"refs/heads/{branch}")
    assert not work.exists()
    assert worktree_branch_exists(root, branch) is False

    res = _client(wb).post(
        f"/api/tasks/{task['id']}/rebuild-branch", json={"confirm": True}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["rebuilt"] is True
    assert body["branch"] == branch
    assert body["base"] == sha
    assert body["reused_commit"] is True
    assert work.is_dir(), "workdir 不存在时必须重建 worktree"
    assert current_branch(work) == branch
    assert (work / "feature.txt").read_text(encoding="utf-8") == "branch work\n"

    persisted = wb.tasks.get(task["id"])["result"]["git"]
    assert persisted["merge"]["reason"] is None
    assert persisted["merge"]["merged"] is False
    assert any(
        event.get("kind") == "task.branch.rebuilt"
        for event in wb.board.read_events(project["id"])
    )


def test_merge_reports_missing_branch_when_workdir_is_gone(wb, tmp_path: Path):
    """workdir 与 branch 同时丢失时，仍要返回可恢复的 missing_branch，而不是 404。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Gone workdir")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)

    _git(root, "worktree", "remove", "--force", str(work))
    _git(root, "worktree", "prune")
    _git(root, "update-ref", "-d", f"refs/heads/{branch}")
    wb.tasks.set_status(task["id"], "success")

    res = _client(wb).post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["reason"] == "missing_branch"
    assert body["code"] == "missing_branch"
    assert body["branch"] == branch
    detail = _client(wb).get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["status"] == "blocked"
    assert detail["task"]["blocked_reason"] == "merge_missing_branch"


def test_merge_does_not_commit_when_recorded_branch_is_gone(wb, tmp_path: Path):
    """记录缺失时也先校验推导出的 ref，不能先 commit 复活一个分支。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "No branch record")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _git(root, "update-ref", "-d", f"refs/heads/{branch}")
    wb.tasks.update(task["id"], result={"git": {"commit": sha, "base": base}})
    wb.tasks.set_status(task["id"], "success")
    head_before = head_commit(root)

    res = _client(wb).post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 409, res.text
    assert res.json()["reason"] == "missing_branch"
    assert head_commit(root) == head_before, "分支缺失时不得先提交"
    assert worktree_branch_exists(root, branch) is False, "不得复活已删分支"


def test_merge_result_reason_missing_branch_is_structured(wb, tmp_path: Path, monkeypatch):
    """merge 命令阶段 ref 消失时，Git 的普通失败文本也要映射为 missing_branch。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Merge race")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)
    wb.tasks.set_status(task["id"], "success")

    def _missing_result(*_args, **_kwargs):
        return {
            "merged": False,
            "blocked": False,
            "conflict": False,
            "reason": "missing_branch",
            "branch": branch,
            "base": base,
            "files": [],
            "sha": None,
            "error": "not something we can merge",
        }

    monkeypatch.setattr(routes_git, "merge_worktree_branch", _missing_result)
    res = _client(wb).post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["reason"] == "missing_branch"
    assert body["code"] == "missing_branch"
    assert body["error"]["details"]["branch"] == branch
    assert wb.tasks.get(task["id"])["status"] == "blocked"


def test_rebuild_refuses_head_fallback_when_task_commit_is_lost(wb, tmp_path: Path):
    """commit 与 base 都不存在时拒绝用 HEAD 造分支，避免 0 文件假成功。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Lost commit")
    branch = current_branch(work)
    assert branch
    _git(root, "worktree", "remove", "--force", str(work))
    _git(root, "worktree", "prune")
    _git(root, "update-ref", "-d", f"refs/heads/{branch}")
    wb.tasks.update(
        task["id"],
        result={"git": {"branch": branch, "commit": "deadbeef" * 5, "base": "deadbeef" * 5}},
    )
    wb.tasks.set_status(task["id"], "blocked", "merge_missing_branch")

    res = _client(wb).post(
        f"/api/tasks/{task['id']}/rebuild-branch", json={"confirm": True}
    )
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "ref_unavailable"
    assert worktree_branch_exists(root, branch) is False


def test_rebuild_rolls_back_branch_when_worktree_creation_fails(
    wb, tmp_path: Path, monkeypatch
):
    """worktree 创建失败要回滚本次新建的 ref，重试不能被 branch_exists 卡死。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Rollback rebuild")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)
    _git(root, "worktree", "remove", "--force", str(work))
    _git(root, "worktree", "prune")
    _git(root, "update-ref", "-d", f"refs/heads/{branch}")

    def _boom(*_args, **_kwargs):
        raise GitError("worktree boom")

    monkeypatch.setattr(routes_git, "create_worktree", _boom)
    res = _client(wb).post(
        f"/api/tasks/{task['id']}/rebuild-branch", json={"confirm": True}
    )
    assert res.status_code == 409, res.text
    assert res.json()["error"]["code"] == "worktree_rebuild_failed"
    assert worktree_branch_exists(root, branch) is False, "失败后必须回滚新建 ref"


def test_rebuild_keeps_recorded_branch_after_title_change(wb, tmp_path: Path):
    """标题变更后重建仍必须使用记录的 branch，而不是按新标题重新推导。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project, task, work = _git_worktree_task(wb, root, "Original title")
    branch = current_branch(work)
    assert branch
    sha = _commit_branch_change(work, "feature.txt", "branch work\n")
    base = head_commit(root)
    assert base
    _record_git_meta(wb, task, branch, sha, base)
    _git(root, "worktree", "remove", "--force", str(work))
    _git(root, "worktree", "prune")
    _git(root, "update-ref", "-d", f"refs/heads/{branch}")
    wb.tasks.update(task["id"], title="Changed title")

    res = _client(wb).post(
        f"/api/tasks/{task['id']}/rebuild-branch", json={"confirm": True}
    )
    assert res.status_code == 200, res.text
    assert res.json()["branch"] == branch
    assert current_branch(work) == branch


def test_create_worktree_refuses_existing_directory_without_replace(tmp_path: Path):
    """目录已存在时必须显式 replace，默认绝不递归删除用户目录。"""
    root = tmp_path / "repo"
    _init_repo(root)
    work = tmp_path / "work"
    work.mkdir()
    marker = work / "user-untracked.txt"
    marker.write_text("keep me\n", encoding="utf-8")

    with pytest.raises(GitError, match="already exists"):
        create_worktree(root, work, task_id="task-existing", title="Existing")
    assert marker.read_text(encoding="utf-8") == "keep me\n"
