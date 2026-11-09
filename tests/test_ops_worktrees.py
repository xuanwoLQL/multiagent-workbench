"""P1-8：已完成 worktree 的保留/清理策略。

覆盖：
- 已合并 + 过保留期的 worktree：dry-run 只报告，apply 注销注册并删目录，
  任务分支与提交永远保留；
- 未合并（含没有 result.git）的 worktree：再老也不清理；
- running/waiting/pending 任务永不处理；
- 非 git 项目 / copy 隔离不被 worktree 清理误删；
- ``policy.git.worktree_retention_days`` 作为默认保留期生效。
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from multiagent.gitops import worktree_branch_exists, worktree_info


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def _stamp(days_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _age_task(wb, task_id: str, days_ago: float) -> None:
    stamp = _stamp(days_ago)
    wb.db.update(
        "tasks", {"id": task_id}, {"finished_at": stamp, "updated_at": stamp}
    )


def _worktree_paths(root: Path) -> set[str]:
    out = _git(root, "worktree", "list", "--porcelain").stdout
    paths: set[str] = set()
    for line in out.splitlines():
        if line.startswith("worktree "):
            value = line[len("worktree "):].strip()
            if value:
                paths.add(os.path.normcase(os.path.realpath(value)))
    return paths


def _repo_and_project(wb, tmp_path: Path, name: str):
    root = tmp_path / f"repo-{name}"
    _init_repo(root)
    project = wb.projects.create(f"project-{name}", root_path=root)
    return root, project


def _new_worktree_task(wb, project, title: str):
    task = wb.tasks.create(project["id"], title, isolation="worktree")
    workdir = Path(wb.isolation.prepare(project, task))
    info = worktree_info(Path(project["root_path"]), workdir)
    assert info["mode"] == "worktree", info
    branch = info["branch"]
    assert branch and worktree_branch_exists(Path(project["root_path"]), branch)
    return task, workdir, branch


def _merged_result(branch: str) -> dict:
    return {"git": {"branch": branch, "merge": {"merged": True}}}


# --------------------------------------------------------------------------- 已合并


def test_merged_old_worktree_dry_run_then_remove_keeps_branch(wb, tmp_path: Path):
    root, project = _repo_and_project(wb, tmp_path, "merged")
    task, workdir, branch = _new_worktree_task(wb, project, "已合并任务")
    (workdir / "feature.txt").write_text("done\n", encoding="utf-8")

    wb.tasks.update(task["id"], result=_merged_result(branch))
    wb.tasks.set_status(task["id"], "success")
    _age_task(wb, task["id"], 30)

    preview = wb.ops.prune(dry_run=True, prune_worktrees_days=14)
    entries = [
        item for item in preview["worktrees"]["removed"] if item["task_id"] == task["id"]
    ]
    assert len(entries) == 1
    assert entries[0]["path"] == str(workdir)
    assert entries[0]["branch"] == branch
    # dry-run 只报告，不删目录、不动注册。
    assert workdir.is_dir()
    assert os.path.normcase(os.path.realpath(workdir)) in _worktree_paths(root)
    assert preview["freed_bytes"] >= len("done\n")

    applied = wb.ops.prune(dry_run=False, prune_worktrees_days=14)
    assert any(
        item["task_id"] == task["id"] for item in applied["worktrees"]["removed"]
    )
    assert not workdir.exists()
    assert os.path.normcase(os.path.realpath(workdir)) not in _worktree_paths(root)
    # 分支与提交永远保留，仍可重新检出。
    assert worktree_branch_exists(root, branch)


# --------------------------------------------------------------------------- 未合并


def test_unmerged_old_worktrees_are_never_removed(wb, tmp_path: Path):
    root, project = _repo_and_project(wb, tmp_path, "unmerged")

    first, work_a, branch_a = _new_worktree_task(wb, project, "合并失败的场景")
    wb.tasks.update(
        first["id"],
        result={"git": {"branch": branch_a, "merge": {"merged": False, "conflict": True}}},
    )
    wb.tasks.set_status(first["id"], "success")
    _age_task(wb, first["id"], 30)

    second, work_b, branch_b = _new_worktree_task(wb, project, "没有 git 交付信息")
    wb.tasks.update(second["id"], result={})
    wb.tasks.set_status(second["id"], "success")
    _age_task(wb, second["id"], 30)

    report = wb.ops.prune(
        dry_run=False,
        prune_worktrees_days=14,
        # 即使工作副本保留期已过，也不能用旧 rmtree 绕过 worktree 注销。
        prune_workspaces_days=14,
    )
    assert report["worktrees"]["skipped_unmerged"] == 2
    assert not report["worktrees"]["removed"]
    assert work_a.is_dir() and work_b.is_dir()
    assert worktree_branch_exists(root, branch_a)
    assert worktree_branch_exists(root, branch_b)
    for path in (work_a, work_b):
        assert os.path.normcase(os.path.realpath(path)) in _worktree_paths(root)


# --------------------------------------------------------------------------- 运行中


def test_running_task_worktree_is_never_pruned(wb, tmp_path: Path):
    root, project = _repo_and_project(wb, tmp_path, "running")
    task, workdir, branch = _new_worktree_task(wb, project, "还在执行")

    wb.tasks.update(task["id"], result=_merged_result(branch))
    wb.tasks.set_status(task["id"], "running")
    wb.db.update(
        "tasks", {"id": task["id"]}, {"finished_at": None, "updated_at": _stamp(30)}
    )

    report = wb.ops.prune(dry_run=False, prune_worktrees_days=14)
    assert workdir.is_dir()
    assert report["worktrees"]["kept"] >= 1
    assert all(
        item["task_id"] != task["id"] for item in report["worktrees"]["removed"]
    )
    assert os.path.normcase(os.path.realpath(workdir)) in _worktree_paths(root)


# --------------------------------------------------------------------------- copy


def test_copy_task_in_non_git_project_is_not_treated_as_worktree(wb, tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "note.txt").write_text("base\n", encoding="utf-8")
    project = wb.projects.create("project-copy", root_path=plain)

    task = wb.tasks.create(project["id"], "copy 任务", isolation="copy")
    workdir = Path(wb.isolation.prepare(project, task))
    assert (workdir / "note.txt").is_file()
    # 即使元数据被错误标成「已合并」，也不能按 worktree 逻辑误删普通副本。
    wb.tasks.update(task["id"], result=_merged_result("ma/whatever"))
    wb.tasks.set_status(task["id"], "success")
    _age_task(wb, task["id"], 30)

    report = wb.ops.prune(
        dry_run=False,
        prune_worktrees_days=14,
        prune_workspaces_days=3650,  # 只验证 worktree 步骤不会误删
    )
    assert workdir.is_dir()
    assert not report["worktrees"]["removed"]
    assert report["worktrees"]["skipped_unmerged"] == 0


# --------------------------------------------------------------------------- policy


def test_policy_worktree_retention_days_is_used_when_argument_omitted(wb, tmp_path: Path):
    wb.config.policy_raw.setdefault("git", {})["worktree_retention_days"] = 1

    root, project = _repo_and_project(wb, tmp_path, "policy")
    task, workdir, branch = _new_worktree_task(wb, project, "两天前的任务")
    wb.tasks.update(task["id"], result=_merged_result(branch))
    wb.tasks.set_status(task["id"], "success")
    _age_task(wb, task["id"], 2)

    report = wb.ops.prune(dry_run=False)
    assert any(
        item["task_id"] == task["id"] for item in report["worktrees"]["removed"]
    )
    assert not workdir.exists()
    assert worktree_branch_exists(root, branch)
