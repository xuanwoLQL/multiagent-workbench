"""Git 感知的工作区能力：worktree、分支、diff、合并建议。"""

from .service import (
    GitError,
    branch_for_task,
    commit_worktree,
    create_worktree,
    current_branch,
    delete_worktree_branch,
    diff_summary,
    dirty_paths,
    is_repo,
    merge_status,
    merge_worktree_branch,
    remove_worktree,
    repo_status,
    worktree_branch_exists,
    worktree_info,
)

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
    "merge_status",
    "merge_worktree_branch",
    "remove_worktree",
    "repo_status",
    "worktree_branch_exists",
    "worktree_info",
]
