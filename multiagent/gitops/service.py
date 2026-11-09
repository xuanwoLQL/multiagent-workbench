"""Git worktree / diff helpers.

Every function is best-effort and never raises for "not a repository": the
workbench must keep working on plain folders. Only explicit git operations
(worktree add/remove) raise :class:`GitError`.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any

from ..util import run_command

__all__ = [
    "GitError",
    "branch_for_task",
    "commit_worktree",
    "create_worktree",
    "current_branch",
    "delete_worktree_branch",
    "diff_summary",
    "dirty_paths",
    "is_repo",
    "merge_worktree_branch",
    "remove_worktree",
    "repo_status",
    "worktree_branch_exists",
    "worktree_info",
]

#: 工作台自己的控制面目录。它可能在项目没有任何 .gitignore 的情况下也无条件
#: 出现在工作树里，因此提交与「干净树」判定都必须显式排除它。
CONTROL_PLANE = ".multiagent"


def _exclude_control_plane(path: str) -> bool:
    normalized = path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized == CONTROL_PLANE or normalized.startswith(CONTROL_PLANE + "/")


def dirty_paths(root: str | Path) -> list[str]:
    """工作树里「用户可见」的未提交路径（排除忽略项与 ``.multiagent`` 控制面）。

    ``git status --porcelain`` 默认不列 ignored 文件；这里再把工作台自己的
    运行目录过滤掉——否则每个导入项目都会因为 ``.multiagent/project.json``
    而被误判为脏树。
    """
    code, out, _ = _git(root, ["status", "--porcelain"], timeout=30)
    if code != 0:
        return []
    paths: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # 重命名格式：``R  old -> new``；两侧都要检查。
        candidates = [part.strip() for part in path.split("->")] if "->" in path else [path]
        if any(_exclude_control_plane(item) for item in candidates):
            continue
        paths.append(path)
    return sorted(set(paths))


class GitError(RuntimeError):
    """An explicit git operation failed."""


def _git(root: str | Path, args: list[str], *, timeout: int = 60) -> tuple[int, str, str]:
    code, out, err, _ = run_command(["git", "-C", str(root), *args], timeout=timeout)
    return code, out or "", err or ""


def _rev(root: str | Path, ref: str = "HEAD") -> str | None:
    code, out, _ = _git(root, ["rev-parse", ref], timeout=10)
    value = out.strip()
    return value if code == 0 and value else None


def is_repo(root: str | Path) -> bool:
    if not root or not Path(root).is_dir():
        return False
    code, out, _ = _git(root, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    return code == 0 and out.strip().lower() == "true"


def _slug(value: str, limit: int = 32) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value or "").strip("-._")
    return (cleaned or "task")[:limit].lower()


def branch_for_task(task_id: str, title: str = "") -> str:
    tail = _slug(task_id.split("-")[-1] if "-" in task_id else task_id, 12)
    name = _slug(title, 32)
    return f"ma/{tail}-{name}" if name else f"ma/{tail}"


def current_branch(root: str | Path) -> str | None:
    """Current branch name, or ``None`` when detached / not a repository."""
    if not is_repo(root):
        return None
    code, out, _ = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    value = out.strip()
    if code != 0 or not value or value == "HEAD":
        return None
    return value


def worktree_branch_exists(root: str | Path, branch: str) -> bool:
    """True when ``branch`` is a local branch in the repository."""
    if not branch or not is_repo(root):
        return False
    code, _, _ = _git(
        root, ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], timeout=10
    )
    return code == 0


def commit_exists(root: str | Path, ref: str) -> bool:
    """Return whether ``ref`` resolves to a commit object in ``root``."""
    if not ref or not is_repo(root):
        return False
    code, _, _ = _git(root, ["cat-file", "-e", f"{ref}^{{commit}}"], timeout=10)
    return code == 0


def resolve_commit(root: str | Path, ref: str) -> str | None:
    """Resolve ``ref`` to a full commit SHA, or ``None`` if it is unavailable."""
    if not ref or not is_repo(root):
        return None
    code, out, _ = _git(root, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], timeout=10)
    if code != 0:
        return None
    value = out.strip()
    return value or None


def head_commit(root: str | Path) -> str | None:
    """Return the current ``HEAD`` commit for a repository, if any."""
    if not is_repo(root):
        return None
    return _rev(root, "HEAD")


def create_branch_ref(root: str | Path, branch: str, ref: str) -> str:
    """Create ``refs/heads/<branch>`` at ``ref`` without touching the worktree.

    Used to repair a task branch that was deleted outside the workbench. The
    caller chooses a commit that still exists (normally the task's recorded
    commit, falling back to its base).
    """
    if not is_repo(root):
        raise GitError("not a git repository")
    if not branch:
        raise GitError("branch is required")
    if not ref:
        raise GitError("ref is required")
    if worktree_branch_exists(root, branch):
        raise GitError(f"branch already exists: {branch}")
    if not commit_exists(root, ref):
        raise GitError(f"commit not found: {ref}")
    code, out, err = _git(root, ["update-ref", f"refs/heads/{branch}", ref], timeout=30)
    if code != 0:
        raise GitError(err.strip() or out.strip() or "git update-ref failed")
    return ref


def delete_worktree_branch(root: str | Path, branch: str) -> bool:
    """Force-delete a local branch. Raises :class:`GitError` on failure."""
    if not is_repo(root):
        raise GitError("not a git repository")
    if not worktree_branch_exists(root, branch):
        return False
    code, out, err = _git(root, ["branch", "-D", branch], timeout=30)
    if code != 0:
        raise GitError(err.strip() or out.strip() or f"git branch -D {branch} failed")
    return True


def repo_status(root: str | Path) -> dict[str, Any]:
    if not is_repo(root):
        return {"is_repo": False}
    code, branch, _ = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"], timeout=10)
    code2, head, _ = _git(root, ["rev-parse", "--short", "HEAD"], timeout=10)
    code3, status, _ = _git(root, ["status", "--porcelain"], timeout=20)
    remote_code, remote, _ = _git(root, ["remote", "get-url", "origin"], timeout=10)
    ahead = behind = 0
    if branch.strip() and branch.strip() != "HEAD":
        code4, counts, _ = _git(
            root, ["rev-list", "--left-right", "--count", f"origin/{branch.strip()}...HEAD"], timeout=15
        )
        if code4 == 0:
            parts = counts.split()
            if len(parts) == 2:
                behind, ahead = int(parts[0]), int(parts[1])
    return {
        "is_repo": True,
        "root": str(Path(root)),
        "branch": branch.strip() if code == 0 else None,
        "head": head.strip() if code2 == 0 else None,
        "dirty": bool(status.strip()) if code3 == 0 else False,
        "detached": branch.strip() == "HEAD",
        "ahead": ahead,
        "behind": behind,
        "remote": remote.strip() if remote_code == 0 else None,
    }


def create_worktree(
    root: str | Path,
    workdir: str | Path,
    *,
    task_id: str,
    title: str = "",
    base_ref: str = "HEAD",
    branch: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Create a branch-backed worktree for one task.

    ``branch`` overrides the usual ``branch_for_task(task_id, title)``
    derivation so a rebuild keeps the ref recorded on the task even if its
    title changed. ``replace`` must be explicit: deleting an existing directory
    is destructive and must never happen merely because a path exists.
    """
    if not is_repo(root):
        raise GitError("not a git repository")
    work = Path(workdir)
    branch = branch or branch_for_task(task_id, title)
    # A crashed run can leave a stale registration pointing at a deleted folder.
    _git(root, ["worktree", "prune"], timeout=30)
    if work.exists():
        if not replace:
            raise GitError(f"workdir already exists: {work}")
        shutil.rmtree(work, ignore_errors=True)
    work.parent.mkdir(parents=True, exist_ok=True)
    code, out, err = _git(
        root, ["worktree", "add", "-B", branch, str(work), base_ref], timeout=180
    )
    if code != 0:
        raise GitError(err.strip() or out.strip() or "git worktree add failed")
    head_code, head, _ = _git(work, ["rev-parse", "--short", "HEAD"], timeout=10)
    return {
        "mode": "worktree",
        "path": str(work),
        "branch": branch,
        "base_ref": base_ref,
        "head": head.strip() if head_code == 0 else None,
    }


def _last_git_error(text: str) -> str:
    """取一条最有信息量的 git 错误，跳过 CRLF / advice 之类的 warning。"""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    meaningful = [
        line
        for line in lines
        if not line.lower().startswith(("warning:", "hint:"))
        and "will be replaced by crlf" not in line.lower()
    ]
    return (meaningful or lines or [""])[-1]


def _git_paths(raw: str) -> list[str]:
    """解析 ``git diff --name-only -z`` 输出（NUL 分隔，避免路径转义）。"""
    return [item for item in (raw or "").split("\x00") if item]


def _unstage_control_plane(workdir: Path) -> None:
    """把索引里任意层级的 ``.multiagent`` 目录/文件移出暂存区。

    只改 index，不碰工作区，也不影响用户自己的 .gitignore；即使控制面已被
    提交进 HEAD，本轮的运行时改动也不会进入任务 commit。
    """
    code, out, _ = _git(workdir, ["diff", "--cached", "--name-only", "-z"], timeout=60)
    if code != 0:
        return
    control_dirs: set[str] = set()
    for path in _git_paths(out):
        parts = path.replace("\\", "/").split("/")
        if CONTROL_PLANE in parts:
            control_dirs.add("/".join(parts[: parts.index(CONTROL_PLANE) + 1]))
    for target in sorted(control_dirs):
        # ``git reset -- <path>`` 对新增文件 = 移出索引，对已跟踪文件 = 恢复 HEAD 版本，
        # 两种情况都不会把控制面写进本次 commit。
        _git(workdir, ["reset", "-q", "--", target], timeout=30)


def commit_worktree(
    workdir: str | Path,
    message: str,
    author_name: str = "MultiAgent",
    author_email: str = "multiagent@local",
) -> dict[str, Any]:
    """Stage everything in ``workdir`` and create a real commit.

    Returns ``{"sha": <full sha or None>, "files": [<changed paths>]}``.
    ``sha`` is ``None`` when there is nothing to commit (idempotent, no error).
    """
    work = Path(workdir)
    if not is_repo(work):
        raise GitError("not a git repository")

    # The control plane lives in ``.multiagent/`` (handoff, context). It is runtime
    # metadata, never part of the deliverable, so keep it out of the commit — even
    # when the project has no ``.gitignore`` at all.
    #
    # 注意：不能把 ``:(exclude).multiagent`` 直接写进 ``git add`` 的 pathspec。
    # 如果项目自己的 ``.gitignore`` 忽略了 ``.multiagent/``（真实项目 Atlas 就是
    # 这样），git 会把 exclude pathspec 里的这个名字当成“显式添加被忽略路径”，
    # 以退出码 1 报 “The following paths are ignored…”，导致整个任务提交失败。
    # 正确做法：先正常 ``add -A``（尊重用户 .gitignore），再把索引里的任意层级
    # ``.multiagent`` 目录/文件显式 unstage。这样无论项目有没有 .gitignore 都不
    # 会进入 commit，也不会误报 add 失败。
    code, out, err = _git(work, ["add", "-A", "--", "."], timeout=120)
    if code != 0:
        detail = _last_git_error(err or out)
        raise GitError(detail or "git add failed")

    _unstage_control_plane(work)

    scode, status_text, _ = _git(work, ["status", "--porcelain"], timeout=30)
    # ``--quiet`` exits 1 when the index differs from HEAD, 0 when nothing is staged.
    code, _, _ = _git(work, ["diff", "--cached", "--quiet"], timeout=30)
    if code == 0:
        return {"sha": None, "files": []}
    if code not in (0, 1):
        # Unborn branch / unusual index: fall back to porcelain status.
        if scode != 0 or not any(
            line.strip() and not line.startswith("??") for line in status_text.splitlines()
        ):
            return {"sha": None, "files": []}

    code, names, _ = _git(work, ["diff", "--cached", "--name-only"], timeout=60)
    if code != 0:
        names = (
            "\n".join(line[3:] for line in status_text.splitlines() if len(line) > 3)
            if scode == 0
            else ""
        )
    files = sorted({line.strip() for line in names.splitlines() if line.strip()})

    code, out, err = _git(
        work,
        [
            "-c",
            f"user.name={author_name or 'MultiAgent'}",
            "-c",
            f"user.email={author_email or 'multiagent@local'}",
            "commit",
            "-m",
            message,
        ],
        timeout=120,
    )
    if code != 0:
        combined = f"{out}\n{err}".lower()
        if (
            "nothing to commit" in combined
            or "no changes added to commit" in combined
            or "nothing added to commit" in combined
        ):
            return {"sha": None, "files": []}
        raise GitError(_last_git_error(err or out) or "git commit failed")

    return {"sha": _rev(work, "HEAD"), "files": files}


def merge_worktree_branch(
    root: str | Path,
    branch: str,
    message: str,
    *,
    require_clean: bool = True,
) -> dict[str, Any]:
    """Merge ``branch`` into the repository's current branch with ``--no-ff``.

    Before touching the working tree the baseline is checked for uncommitted
    changes. A dirty baseline is **blocked** (``merged=False, blocked=True``)
    rather than merged opportunistically: the audit trail must be able to say
    exactly which tree the merge landed on, and a concurrent human edit must not
    be silently mixed into an agent commit. The ``.multiagent`` control plane is
    ignored by this check. Set ``require_clean=False`` (policy
    ``git.require_clean_tree=false``) to opt out.

    On conflict the merge is aborted so the repository is left exactly as it was,
    and the conflicting paths are returned. Never raises for a normal conflict
    or a dirty baseline; raises :class:`GitError` for invalid inputs (not a repo
    / missing branch).
    """
    repo = Path(root)
    if not is_repo(repo):
        raise GitError("not a git repository")
    merge_ref = resolve_commit(repo, branch)
    if not merge_ref:
        raise GitError(f"branch not found: {branch}")

    base = _rev(repo, "HEAD")
    if require_clean:
        dirty = dirty_paths(repo)
        if dirty:
            return {
                "merged": False,
                "blocked": True,
                "conflict": False,
                "reason": "dirty_worktree",
                "dirty_files": dirty[:100],
                "files": [],
                "branch": branch,
                "base": base,
                "sha": None,
                "error": (
                    "基线工作树有未提交改动，已阻止自动合并："
                    + ", ".join(dirty[:8])
                    + ("…" if len(dirty) > 8 else "")
                ),
            }

    # Merge an immutable SHA, not the mutable branch name: deleting the ref
    # between the existence check and the merge command must not turn into
    # Git's opaque "not something we can merge" failure.
    code, out, err = _git(repo, ["merge", "--no-ff", "-m", message, merge_ref], timeout=300)
    if code != 0:
        ucode, unmerged, _ = _git(repo, ["diff", "--name-only", "--diff-filter=U"], timeout=30)
        files = (
            sorted({line.strip() for line in unmerged.splitlines() if line.strip()})
            if ucode == 0
            else []
        )
        mcode, _, _ = _git(repo, ["rev-parse", "-q", "--verify", "MERGE_HEAD"], timeout=10)
        in_merge = mcode == 0
        if in_merge:
            _git(repo, ["merge", "--abort"], timeout=60)
        missing = not worktree_branch_exists(repo, branch)
        return {
            "merged": False,
            "blocked": False,
            "conflict": bool(files) or in_merge,
            "reason": "conflict" if (files or in_merge) else ("missing_branch" if missing else "merge_failed"),
            "sha": None,
            "files": files,
            "branch": branch,
            "base": base,
            "error": (err or out).strip(),
        }

    head = _rev(repo, "HEAD")
    files: list[str] = []
    if base and head and base != head:
        fcode, names, _ = _git(repo, ["diff", "--name-only", base, head], timeout=60)
        if fcode == 0:
            files = sorted({line.strip() for line in names.splitlines() if line.strip()})
    return {
        "merged": True,
        "blocked": False,
        "conflict": False,
        "reason": None,
        "sha": head,
        "files": files,
        "branch": branch,
        "base": base,
    }


def remove_worktree(root: str | Path, workdir: str | Path, *, branch: str | None = None, delete_branch: bool = False) -> None:
    if is_repo(root):
        _git(root, ["worktree", "remove", "--force", str(workdir)], timeout=120)
        _git(root, ["worktree", "prune"], timeout=30)
        if delete_branch and branch:
            _git(root, ["branch", "-D", branch], timeout=30)
    shutil.rmtree(workdir, ignore_errors=True)


def worktree_info(root: str | Path, workdir: str | Path, *, task_id: str | None = None, title: str = "") -> dict[str, Any]:
    work = Path(workdir)
    if not work.is_dir():
        return {"exists": False, "path": str(work)}
    branch = branch_for_task(task_id or "", title) if task_id else None
    if is_repo(work):
        status = repo_status(work)
        code, files, _ = _git(work, ["status", "--porcelain"], timeout=30)
        return {
            "exists": True,
            "mode": "worktree",
            "path": str(work),
            "branch": status.get("branch") or branch,
            "head": status.get("head"),
            "dirty": status.get("dirty", False),
            "pending_files": len([line for line in files.splitlines() if line.strip()]) if code == 0 else 0,
        }
    return {"exists": True, "mode": "copy", "path": str(work), "branch": None, "pending_files": 0}


def merge_status(git_meta: dict[str, Any] | None) -> str | None:
    """把 ``task.result.git.merge`` 归纳为 UI 可直接消费的单一状态。

    与 ``routes_git._merge_status`` 保持同一份语义，避免两个接口说法不一致：
    - ``None``：任务没有 git 交付信息（copy/shared 或非 git 项目）
    - ``not_merged``：有提交但从未尝试合并
    - ``merged`` / ``conflict`` / ``blocked`` / ``auto_merge_disabled``
    """
    if not isinstance(git_meta, dict) or not git_meta:
        return None
    merge = git_meta.get("merge")
    if not isinstance(merge, dict):
        return "not_merged"
    if merge.get("merged"):
        return "merged"
    if merge.get("discarded"):
        return "discarded"
    if merge.get("reason") == "missing_branch" or merge.get("code") == "missing_branch":
        return "missing_branch"
    if merge.get("conflict"):
        return "conflict"
    if merge.get("blocked"):
        return "blocked"
    if merge.get("skipped") or merge.get("reason") == "auto_merge_disabled":
        return "auto_merge_disabled"
    return "not_merged"


def diff_summary(root: str | Path, workdir: str | Path) -> dict[str, Any]:
    """Structured `git diff --numstat` between the repo HEAD and the worktree."""
    if not is_repo(workdir):
        return {"files": [], "additions": 0, "deletions": 0, "binary": 0}
    code, out, _ = _git(workdir, ["diff", "--numstat", "HEAD"], timeout=60)
    files: list[dict[str, Any]] = []
    additions = deletions = binary = 0
    if code == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            add, delete, path = parts
            if add == "-" or delete == "-":
                binary += 1
                files.append({"path": path, "binary": True})
                continue
            additions += int(add)
            deletions += int(delete)
            files.append({"path": path, "additions": int(add), "deletions": int(delete)})
    # Untracked files are part of the deliverable too.
    code2, untracked, _ = _git(workdir, ["ls-files", "--others", "--exclude-standard"], timeout=60)
    if code2 == 0:
        for path in untracked.splitlines():
            if path.strip():
                files.append({"path": path.strip(), "untracked": True, "additions": 0, "deletions": 0})
    return {"files": files, "additions": additions, "deletions": deletions, "binary": binary}
