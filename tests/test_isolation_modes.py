"""隔离模式决策可见性（P0-5）与 worktree 基线单次复制（P1-6）。

覆盖 ``IsolationService.prepare_ex`` 的契约：
- 干净 git 仓库 + worktree：正常使用 worktree，不降级；
- 脏仓库 / 非 git 目录 / worktree 创建失败：显式降级为 copy 并给出原因；
- read_only 强制 copy 属于策略行为，不算降级；
- shared + 修改型任务 + 空 write_scope：安全兜底降级；
- worktree 成功路径 baseline 只从 worktree 复制一次；
- ``prepare()`` 保持返回 ``Path`` 的旧签名。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from multiagent.workspace import isolation as isolation_module


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
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


def _project(wb, tmp_path: Path, name: str, *, git: bool) -> dict:
    root = tmp_path / name
    if git:
        _init_repo(root)
    else:
        root.mkdir(parents=True, exist_ok=True)
        (root / "seed.txt").write_text("seed", encoding="utf-8")
    return wb.projects.create(name, root_path=str(root))


def _task(wb, project: dict, *, isolation: str, **extra) -> dict:
    created = wb.tasks.create(
        project["id"],
        f"{isolation} 任务",
        kind=extra.pop("kind", "generic"),
        agent_id="worker",
        isolation=isolation,
        **extra,
    )
    return wb.tasks.get(created["id"])


# --------------------------------------------------------------------------- P0-5


def test_clean_git_repo_uses_worktree_and_baseline_matches(wb, tmp_path: Path):
    project = _project(wb, tmp_path, "clean-repo", git=True)
    task = _task(wb, project, isolation="worktree")

    workdir, info = wb.isolation.prepare_ex(project, task)

    assert isinstance(workdir, Path)
    assert info["requested_mode"] == "worktree"
    assert info["mode"] == "worktree"
    assert info["degraded"] is False
    assert info["degrade_code"] is None
    assert info["degrade_reason"] is None
    assert Path(info["workdir"]).is_absolute()
    assert Path(info["workdir"]).resolve() == workdir.resolve()

    baseline = wb.isolation.baseline_dir(project["id"], task["id"])
    assert (workdir / "README.md").is_file()
    assert (baseline / "README.md").read_bytes() == (workdir / "README.md").read_bytes()
    assert (baseline / "README.md").read_bytes().strip(), "baseline 不能为空或半成品"


def test_dirty_repo_degrades_to_copy_with_previous_change_visible(wb, tmp_path: Path):
    project = _project(wb, tmp_path, "dirty-repo", git=True)
    root = Path(project["root_path"])
    (root / "README.md").write_text("agent previous change\n", encoding="utf-8")
    task = _task(wb, project, isolation="worktree")

    workdir, info = wb.isolation.prepare_ex(project, task)

    assert info["requested_mode"] == "worktree"
    assert info["mode"] == "copy"
    assert info["degraded"] is True
    assert info["degrade_code"] == "dirty_workspace"
    assert info["degrade_reason"]
    assert "工作区有未提交改动" in info["degrade_reason"]
    assert (workdir / "README.md").read_text(encoding="utf-8") == "agent previous change\n"


def test_non_git_directory_degrades_to_copy(wb, tmp_path: Path):
    project = _project(wb, tmp_path, "plain-dir", git=False)
    task = _task(wb, project, isolation="worktree")

    workdir, info = wb.isolation.prepare_ex(project, task)

    assert info["requested_mode"] == "worktree"
    assert info["mode"] == "copy"
    assert info["degraded"] is True
    assert info["degrade_code"] == "not_git_repo"
    assert info["degrade_reason"] == "工作区不是 Git 仓库"
    assert (workdir / "seed.txt").read_text(encoding="utf-8") == "seed"


def test_worktree_creation_failure_degrades_to_copy(wb, tmp_path: Path, monkeypatch):
    project = _project(wb, tmp_path, "failed-worktree", git=True)
    root = Path(project["root_path"])
    task = _task(wb, project, isolation="worktree")

    def boom(*_args, **_kwargs):
        raise RuntimeError("模拟 worktree 创建失败")

    monkeypatch.setattr("multiagent.gitops.create_worktree", boom)

    workdir, info = wb.isolation.prepare_ex(project, task)

    assert info["mode"] == "copy"
    assert info["degraded"] is True
    assert info["degrade_code"] == "worktree_failed"
    assert "RuntimeError" in info["degrade_reason"]
    assert "模拟 worktree 创建失败" in info["degrade_reason"]
    assert (workdir / "README.md").read_bytes() == (root / "README.md").read_bytes()
    baseline = wb.isolation.baseline_dir(project["id"], task["id"])
    assert (baseline / "README.md").is_file(), "降级后 baseline 不能为空"


def test_read_only_forces_copy_without_degradation(wb, tmp_path: Path):
    project = _project(wb, tmp_path, "readonly-repo", git=True)
    task = _task(wb, project, isolation="worktree", read_only=True)

    workdir, info = wb.isolation.prepare_ex(project, task)

    assert info["requested_mode"] == "worktree"
    assert info["mode"] == "copy"
    assert info["degraded"] is False
    assert info["degrade_code"] is None
    assert workdir.resolve() != Path(project["root_path"]).resolve()
    assert (workdir / "README.md").is_file()


def test_unscoped_shared_modifying_task_degrades_to_copy(wb, tmp_path: Path):
    project = _project(wb, tmp_path, "unscoped-shared", git=False)
    task = _task(wb, project, isolation="shared", kind="code")

    workdir, info = wb.isolation.prepare_ex(project, task)

    assert info["requested_mode"] == "shared"
    assert info["mode"] == "copy"
    assert info["degraded"] is True
    assert info["degrade_code"] == "unscoped_shared"
    assert info["degrade_reason"]
    assert workdir.resolve() != Path(project["root_path"]).resolve()


# --------------------------------------------------------------------------- P1-6


def test_worktree_baseline_is_copied_once_from_worktree(wb, tmp_path: Path, monkeypatch):
    project = _project(wb, tmp_path, "single-copy", git=True)
    root = Path(project["root_path"])
    task = _task(wb, project, isolation="worktree")

    calls: list[tuple[Path, Path]] = []
    real_copy_tree = isolation_module.copy_tree

    def spy(src, dst, *args, **kwargs):
        calls.append((Path(src).resolve(), Path(dst).resolve()))
        return real_copy_tree(src, dst, *args, **kwargs)

    monkeypatch.setattr(isolation_module, "copy_tree", spy)

    workdir, info = wb.isolation.prepare_ex(project, task)
    baseline = wb.isolation.baseline_dir(project["id"], task["id"]).resolve()

    assert info["mode"] == "worktree"
    assert len(calls) == 1, f"worktree 成功路径只应复制一次，实际：{calls}"
    assert calls[0][0] == workdir.resolve()
    assert calls[0][1] == baseline
    assert calls[0][0] != root.resolve(), "不应先做 root→baseline 的复制"


# --------------------------------------------------------------------------- 兼容


def test_prepare_still_returns_path(wb, tmp_path: Path):
    project = _project(wb, tmp_path, "legacy-prepare", git=False)
    task = _task(wb, project, isolation="copy")

    result = wb.isolation.prepare(project, task)

    assert isinstance(result, Path)
    assert result.is_dir()
