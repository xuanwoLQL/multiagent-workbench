"""Git-aware routes: repo status, task worktree, diff and merge."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, HTTPException
from fastapi.responses import JSONResponse

from ..gitops import (
    GitError,
    branch_for_task,
    commit_worktree,
    create_worktree,
    current_branch,
    delete_worktree_branch,
    diff_summary,
    is_repo,
    merge_status,
    merge_worktree_branch,
    remove_worktree,
    repo_status,
    worktree_branch_exists,
    worktree_info,
)
from ..gitops.service import (
    _git,
    commit_exists,
    create_branch_ref,
    head_commit,
    resolve_commit,
)
from ..workspace.isolation import WorkspaceDriftError


def _project(ctx, pid: str) -> dict[str, Any]:
    return ctx.project(pid)


def _workdir(ctx, task: dict[str, Any]) -> Path:
    return ctx.app.isolation.task_dir(task["project_id"], task["id"]) / "work"


def _short_task_id(task_id: str) -> str:
    text = str(task_id or "")
    tail = text.split("-")[-1] if "-" in text else text
    return (tail or text)[:8]


def _merge_message(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "任务")
    return f"merge({_short_task_id(str(task['id']))}): {title}".strip()


def _persist_merge_result(
    ctx,
    task: dict[str, Any],
    git_meta: dict[str, Any],
    merge: dict[str, Any],
    *,
    branch: str,
) -> None:
    """把人工合并结果写回 ``task.result.git.merge``。

    没有这一步，UI 和 ``GET /worktree`` 只能看到 Runner 上次自动合并的旧状态，
    人工 merge 成功后仍显示「未合并」。只改 result，不动任务状态。
    """
    result = dict(task.get("result") or {})
    meta = dict(git_meta or {})
    meta["branch"] = branch
    meta["merge"] = {
        "merged": bool(merge.get("merged")),
        "conflict": bool(merge.get("conflict")),
        "blocked": bool(merge.get("blocked")),
        "skipped": False,
        "reason": merge.get("reason"),
        "sha": merge.get("sha"),
        "branch": branch,
        "base": merge.get("base"),
        "files": list(merge.get("files") or []),
        "dirty_files": list(merge.get("dirty_files") or []),
        "error": merge.get("error"),
        "source": "manual",
    }
    result["git"] = meta
    # 只改 git 键：异步进程审计可能正在写 processes，整份 result 回写会丢更新。
    ctx.app.tasks.set_result_key(task["id"], "git", meta)


def _merge_status(git_meta: dict[str, Any] | None) -> str | None:
    """兼容旧调用名；状态语义统一在 :func:`gitops.merge_status`。"""
    return merge_status(git_meta)


def _block_after_merge_failure(ctx, task: dict[str, Any], reason: str) -> None:
    """成功任务在合并失败后不能被继续当作 success（Atlas 场景 A）。"""
    status = str(task.get("status") or "")
    if status == "success":
        ctx.app.tasks.set_status(task["id"], "blocked", reason)
    elif status == "blocked" and str(task.get("blocked_reason") or "").startswith("merge_"):
        if task.get("blocked_reason") != reason:
            ctx.app.tasks.set_status(task["id"], "blocked", reason)


def _restore_after_merge_resolution(
    ctx,
    task: dict[str, Any],
    *,
    blocked_reasons: set[str],
    reason: str,
) -> None:
    """合并冲突/分支缺失被人工修复后，把 blocked 任务恢复为 success。"""
    if (
        str(task.get("status") or "") == "blocked"
        and str(task.get("blocked_reason") or "") in blocked_reasons
    ):
        ctx.app.tasks.set_status(task["id"], "success", reason)


def _is_missing_branch_error(exc: BaseException | str) -> bool:
    text = str(exc or "").lower()
    return "branch not found" in text or "missing branch" in text


def _structured_error(
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": code,
                "message": message,
                "details": details or {},
            }
        },
    )


def register_git_routes(api, ctx) -> None:
    @api.get("/api/projects/{pid}/git")
    def project_git(pid: str) -> dict[str, Any]:
        project = _project(ctx, pid)
        return repo_status(project["root_path"])

    @api.get("/api/tasks/{tid}/worktree")
    def task_worktree(tid: str) -> dict[str, Any]:
        task = ctx.task(tid)
        work = _workdir(ctx, task)
        info = worktree_info(
            _project(ctx, task["project_id"])["root_path"],
            work,
            task_id=task["id"],
            title=task.get("title") or "",
        )
        mode = (task.get("isolation") or "copy").lower()
        info["mode"] = info.get("mode") or mode
        info["task_id"] = task["id"]
        # 只增不改：旧字段保持原样，新增结构化 git 交付 + 合并状态。
        raw_git = (task.get("result") or {}).get("git")
        git_meta = raw_git if isinstance(raw_git, dict) else None
        info["git"] = git_meta
        info["merge_status"] = _merge_status(git_meta)
        return info

    @api.get("/api/tasks/{tid}/git-diff")
    def task_git_diff(tid: str) -> dict[str, Any]:
        task = ctx.task(tid)
        project = _project(ctx, task["project_id"])
        work = _workdir(ctx, task)
        if not work.is_dir():
            return {"files": [], "additions": 0, "deletions": 0, "binary": 0, "available": False}
        summary = diff_summary(project["root_path"], work)
        summary["available"] = True
        summary["path"] = str(work)
        return summary

    @api.get("/api/tasks/{tid}/git-diff/file")
    def task_git_diff_file(tid: str, path: str) -> dict[str, Any]:
        task = ctx.task(tid)
        work = _workdir(ctx, task)
        if not work.is_dir():
            raise HTTPException(404, "任务还没有工作副本")
        code, out, err = _git(work, ["diff", "HEAD", "--", path], timeout=60)
        if code != 0:
            raise HTTPException(400, err.strip() or "git diff 失败")
        if not out.strip():
            target = (work / path)
            if target.is_file():
                try:
                    text = target.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                lines = text.splitlines()
                out = "\n".join(
                    [f"--- /dev/null", f"+++ b/{path}", f"@@ -0,0 +1,{len(lines)} @@", *[f"+{line}" for line in lines]]
                )
        return {"path": path, "diff": out, "binary": False}

    @api.post("/api/tasks/{tid}/merge")
    def merge_task(tid: str) -> dict[str, Any]:
        """Merge a worktree task branch, or copy changes back for non-git tasks.

        worktree + git 仓库：把任务分支 ``--no-ff`` 合并进当前分支；冲突时
        ``git merge --abort`` 保证仓库干净并返回 409 + 冲突文件。
        其它情况（copy / 非 git）保持原来的文件回收语义。
        """
        task = ctx.task(tid)
        project = _project(ctx, task["project_id"])
        work = _workdir(ctx, task)
        root = Path(project["root_path"])
        mode = (task.get("isolation") or "copy").lower()
        git_meta = dict((task.get("result") or {}).get("git") or {})
        branch = str(git_meta.get("branch") or "") or None
        if mode == "worktree" and is_repo(root):
            if work.is_dir() and is_repo(work):
                branch = branch or current_branch(work)
            branch = branch or branch_for_task(str(task["id"]), str(task.get("title") or ""))
        worktree_mode = mode == "worktree" and is_repo(root) and (
            is_repo(work) or bool(branch)
        )

        if worktree_mode:
            # 先按任务记录的 branch 检查 ref：分支若已被外部删除，绝不能先
            # commit_worktree（可能把已删分支复活），也不能降级成普通 400。
            # 这个检查必须先于 workdir 存在性：分支和目录同时丢失时，仍然
            # 要给出可恢复的 missing_branch，而不是一个不可操作的 404。
            if branch and not worktree_branch_exists(root, branch):
                base = str(git_meta.get("base") or "") or head_commit(root) or None
                message = f"任务分支已不存在，无法合并：{branch}"
                merge = {
                    "merged": False,
                    "blocked": False,
                    "conflict": False,
                    "reason": "missing_branch",
                    "branch": branch,
                    "base": base,
                    "files": [],
                    "sha": None,
                    "error": message,
                }
                ctx.app.board.append_event(
                    project_id=project["id"],
                    kind="task.merge.missing_branch",
                    summary=message,
                    actor="human",
                    task_id=task["id"],
                    data={"branch": branch, "base": base, "task_id": task["id"]},
                )
                _persist_merge_result(ctx, task, git_meta, merge, branch=branch)
                _block_after_merge_failure(ctx, task, "merge_missing_branch")
                return JSONResponse(
                    status_code=409,
                    content={
                        "merged": False,
                        "blocked": False,
                        "conflict": False,
                        "reason": "missing_branch",
                        "code": "missing_branch",
                        "branch": branch,
                        "task_id": task["id"],
                        "base": base,
                        "files": [],
                        "sha": None,
                        "message": message,
                        "error": {
                            "code": "missing_branch",
                            "message": message,
                            "details": {
                                "branch": branch,
                                "task_id": task["id"],
                                "base": base,
                            },
                        },
                    },
                )

        if not work.is_dir():
            return _structured_error(
                409,
                "workdir_missing",
                "任务工作副本已不存在，无法合并",
                {"task_id": task["id"], "branch": branch},
            )

        if worktree_mode:
            # 旧任务/中断的 worktree 可能还留着未提交改动：显式人工合并时先落盘。
            if is_repo(work):
                try:
                    pending = commit_worktree(
                        work,
                        _merge_message(task),
                        author_name=str(ctx.app.config.policy_get("git.author_name", "MultiAgent") or "MultiAgent"),
                        author_email=str(
                            ctx.app.config.policy_get("git.author_email", "multiagent@local")
                            or "multiagent@local"
                        ),
                    )
                except GitError as exc:
                    if branch and not worktree_branch_exists(root, branch):
                        base = str(git_meta.get("base") or "") or head_commit(root) or None
                        message = f"任务分支已不存在，无法合并：{branch}"
                        merge = {
                            "merged": False,
                            "blocked": False,
                            "conflict": False,
                            "reason": "missing_branch",
                            "branch": branch,
                            "base": base,
                            "files": [],
                            "sha": None,
                            "error": message,
                        }
                        ctx.app.board.append_event(
                            project_id=project["id"],
                            kind="task.merge.missing_branch",
                            summary=message,
                            actor="human",
                            task_id=task["id"],
                            data={"branch": branch, "base": base, "task_id": task["id"]},
                        )
                        _persist_merge_result(ctx, task, git_meta, merge, branch=branch)
                        _block_after_merge_failure(ctx, task, "merge_missing_branch")
                        return _structured_error(
                            409,
                            "missing_branch",
                            message,
                            {"branch": branch, "task_id": task["id"], "base": base},
                        )
                    raise HTTPException(400, f"工作副本提交失败：{exc}") from exc
                if pending.get("sha"):
                    git_meta["commit"] = pending["sha"]
                    git_meta["files"] = pending.get("files") or []
                    task = dict(task)
                    result = dict(task.get("result") or {})
                    result["git"] = git_meta
                    ctx.app.tasks.update(task["id"], result=result)
                branch = branch or current_branch(work)

            branch = branch or branch_for_task(str(task["id"]), str(task.get("title") or ""))
            if not worktree_branch_exists(root, branch):
                base = str(git_meta.get("base") or "") or head_commit(root) or None
                message = f"任务分支已不存在，无法合并：{branch}"
                merge = {
                    "merged": False,
                    "blocked": False,
                    "conflict": False,
                    "reason": "missing_branch",
                    "branch": branch,
                    "base": base,
                    "files": [],
                    "sha": None,
                    "error": message,
                }
                ctx.app.board.append_event(
                    project_id=project["id"],
                    kind="task.merge.missing_branch",
                    summary=message,
                    actor="human",
                    task_id=task["id"],
                    data={"branch": branch, "base": base, "task_id": task["id"]},
                )
                _persist_merge_result(ctx, task, git_meta, merge, branch=branch)
                _block_after_merge_failure(ctx, task, "merge_missing_branch")
                return JSONResponse(
                    status_code=409,
                    content={
                        "merged": False,
                        "blocked": False,
                        "conflict": False,
                        "reason": "missing_branch",
                        "code": "missing_branch",
                        "branch": branch,
                        "task_id": task["id"],
                        "base": base,
                        "files": [],
                        "sha": None,
                        "message": message,
                        "error": {
                            "code": "missing_branch",
                            "message": message,
                            "details": {
                                "branch": branch,
                                "task_id": task["id"],
                                "base": base,
                            },
                        },
                    },
                )
            require_clean = ctx.app.config.policy_bool("git.require_clean_tree", True)
            try:
                merged = merge_worktree_branch(
                    root, branch, _merge_message(task), require_clean=require_clean
                )
            except GitError as exc:
                if _is_missing_branch_error(exc):
                    base = str(git_meta.get("base") or "") or head_commit(root) or None
                    message = f"任务分支已不存在，无法合并：{branch}"
                    merge = {
                        "merged": False,
                        "blocked": False,
                        "conflict": False,
                        "reason": "missing_branch",
                        "branch": branch,
                        "base": base,
                        "files": [],
                        "sha": None,
                        "error": message,
                    }
                    ctx.app.board.append_event(
                        project_id=project["id"],
                        kind="task.merge.missing_branch",
                        summary=message,
                        actor="human",
                        task_id=task["id"],
                        data={"branch": branch, "base": base, "task_id": task["id"]},
                    )
                    _persist_merge_result(ctx, task, git_meta, merge, branch=branch)
                    _block_after_merge_failure(ctx, task, "merge_missing_branch")
                    return JSONResponse(
                        status_code=409,
                        content={
                            "merged": False,
                            "blocked": False,
                            "conflict": False,
                            "reason": "missing_branch",
                            "code": "missing_branch",
                            "branch": branch,
                            "task_id": task["id"],
                            "base": base,
                            "files": [],
                            "sha": None,
                            "message": message,
                            "error": {
                                "code": "missing_branch",
                                "message": message,
                                "details": {
                                    "branch": branch,
                                    "task_id": task["id"],
                                    "base": base,
                                },
                            },
                        },
                    )
                raise HTTPException(400, f"合并失败：{exc}") from exc

            files = list(merged.get("files") or [])
            base = merged.get("base")
            reason = merged.get("reason")

            if reason == "missing_branch":
                message = f"任务分支已不存在，无法合并：{branch}"
                ctx.app.board.append_event(
                    project_id=project["id"],
                    kind="task.merge.missing_branch",
                    summary=message,
                    actor="human",
                    task_id=task["id"],
                    data={"branch": branch, "base": base, "task_id": task["id"]},
                )
                _persist_merge_result(ctx, task, git_meta, merged, branch=branch)
                _block_after_merge_failure(ctx, task, "merge_missing_branch")
                return JSONResponse(
                    status_code=409,
                    content={
                        "merged": False,
                        "blocked": False,
                        "conflict": False,
                        "reason": "missing_branch",
                        "code": "missing_branch",
                        "branch": branch,
                        "task_id": task["id"],
                        "base": base,
                        "files": [],
                        "sha": None,
                        "message": message,
                        "error": {
                            "code": "missing_branch",
                            "message": message,
                            "details": {
                                "branch": branch,
                                "task_id": task["id"],
                                "base": base,
                            },
                        },
                    },
                )

            if merged.get("blocked"):
                dirty_files = list(merged.get("dirty_files") or [])
                message = "基线工作树有未提交改动，已阻止合并"
                if dirty_files:
                    message += "：" + "、".join(str(item) for item in dirty_files[:8])
                    if len(dirty_files) > 8:
                        message += "…"
                ctx.app.board.append_event(
                    project_id=project["id"],
                    kind="task.merge.blocked",
                    summary=f"基线工作树有未提交改动，已阻止合并：{task['title']}",
                    actor="human",
                    task_id=task["id"],
                    data={
                        "merged": False,
                        "blocked": True,
                        "conflict": False,
                        "reason": "dirty_worktree",
                        "dirty_files": dirty_files,
                        "branch": branch,
                        "base": base,
                        "files": [],
                    },
                )
                _persist_merge_result(ctx, task, git_meta, merged, branch=branch)
                return JSONResponse(
                    status_code=409,
                    content={
                        "merged": False,
                        "blocked": True,
                        "conflict": False,
                        "reason": "dirty_worktree",
                        "dirty_files": dirty_files,
                        "branch": branch,
                        "base": base,
                        "sha": None,
                        "files": [],
                        "message": message,
                        "error": merged.get("error"),
                    },
                )

            if merged.get("conflict"):
                ctx.app.board.append_event(
                    project_id=project["id"],
                    kind="task.merge.conflict",
                    summary=f"合并冲突，已自动回滚：{task['title']}",
                    actor="human",
                    task_id=task["id"],
                    data={
                        "merged": False,
                        "blocked": False,
                        "conflict": True,
                        "reason": "conflict",
                        "branch": branch,
                        "base": base,
                        "files": files,
                    },
                )
                _persist_merge_result(ctx, task, git_meta, merged, branch=branch)
                _block_after_merge_failure(ctx, task, "merge_conflict")
                return JSONResponse(
                    status_code=409,
                    content={
                        "merged": False,
                        "blocked": False,
                        "conflict": True,
                        "reason": "conflict",
                        "branch": branch,
                        "base": base,
                        "sha": None,
                        "files": files,
                        "message": "合并冲突，已执行 git merge --abort，仓库保持干净",
                        "error": merged.get("error"),
                    },
                )

            if not merged.get("merged"):
                error_text = str(merged.get("error") or "")
                ctx.app.board.append_event(
                    project_id=project["id"],
                    kind="task.merge.failed",
                    summary=f"合并失败：{task['title']}",
                    actor="human",
                    task_id=task["id"],
                    data={
                        "merged": False,
                        "blocked": False,
                        "conflict": False,
                        "reason": reason or "merge_failed",
                        "branch": branch,
                        "base": base,
                        "files": files,
                        "error": error_text,
                    },
                )
                _persist_merge_result(ctx, task, git_meta, merged, branch=branch)
                return JSONResponse(
                    status_code=409,
                    content={
                        "merged": False,
                        "blocked": False,
                        "conflict": False,
                        "reason": reason or "merge_failed",
                        "branch": branch,
                        "base": base,
                        "sha": None,
                        "files": files,
                        "message": error_text or "合并失败",
                        "error": error_text,
                    },
                )

            ctx.app.board.append_event(
                project_id=project["id"],
                kind="task.merged",
                summary=f"合并分支 {branch}（{len(files)} 个文件）：{task['title']}",
                actor="human",
                task_id=task["id"],
                data={
                    "merged": True,
                    "blocked": False,
                    "conflict": False,
                    "reason": None,
                    "branch": branch,
                    "base": base,
                    "sha": merged.get("sha"),
                    "files": files,
                    "count": len(files),
                },
            )
            _persist_merge_result(ctx, task, git_meta, merged, branch=branch)
            _restore_after_merge_resolution(
                ctx,
                task,
                blocked_reasons={"merge_conflict", "merge_missing_branch"},
                reason="merge_resolved",
            )
            return {
                "merged": True,
                "blocked": False,
                "conflict": False,
                "reason": None,
                "mode": "git",
                "branch": branch,
                "base": base,
                "sha": merged.get("sha"),
                "files": files,
                "count": len(files),
            }

        # 非 worktree / 非 git：保留原有的文件回收（copy-back）语义。
        rows = ctx.app.db.query(
            "SELECT * FROM file_changes WHERE task_id = ? ORDER BY id", (task["id"],)
        )
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest[str(row["rel_path"])] = dict(row)
        changes = [
            {
                "rel_path": rel,
                "action": item.get("action") or "update",
                "before_hash": item.get("before_hash"),
                "after_hash": item.get("after_hash"),
            }
            for rel, item in latest.items()
        ]
        if not changes:
            return {"committed": [], "count": 0, "message": "没有可合并的改动"}
        try:
            committed = ctx.app.isolation.commit_back(
                project, work, changes, only_in_scope=False, scope=None, read_only=False
            )
        except WorkspaceDriftError as exc:
            raise HTTPException(409, f"工作区已被外部改动，阻止覆盖：{', '.join(exc.paths[:5])}") from exc
        except OSError as exc:
            raise HTTPException(500, f"合并失败：{exc}") from exc
        ctx.app.board.append_event(
            project_id=project["id"],
            kind="task.merged",
            summary=f"人工合并 {len(committed)} 个文件：{task['title']}",
            actor="human",
            task_id=task["id"],
            data={"files": committed},
        )
        return {"committed": committed, "count": len(committed)}

    @api.post("/api/tasks/{tid}/rebuild-branch")
    def rebuild_branch(
        tid: str,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        """Recreate a task branch that was deleted outside the workbench.

        Requires ``{"confirm": true}``. The ref is rebuilt from the task's
        recorded commit when it still exists, otherwise from its base. If both
        objects are gone this refuses with ``ref_unavailable`` instead of
        silently pointing a task branch at HEAD (which could later "merge"
        zero files and look successful). An existing worktree directory is
        never removed.
        """
        task = ctx.task(tid)
        project = _project(ctx, task["project_id"])
        data = payload or {}
        if data.get("confirm") is not True:
            return _structured_error(
                400,
                "confirmation_required",
                "重建任务分支会修改 git ref，需要显式确认",
                {"task_id": task["id"]},
            )

        root = Path(project["root_path"])
        work = _workdir(ctx, task)
        mode = (task.get("isolation") or "copy").lower()
        if mode != "worktree" or not is_repo(root):
            return _structured_error(
                409,
                "not_worktree_task",
                "只有 git worktree 任务可以重建分支",
                {"task_id": task["id"], "isolation": mode},
            )

        git_meta = dict((task.get("result") or {}).get("git") or {})
        branch = str(git_meta.get("branch") or "").strip()
        if not branch and work.is_dir() and is_repo(work):
            branch = str(current_branch(work) or "").strip()
        if not branch:
            return _structured_error(
                409,
                "missing_branch",
                "任务没有可重建的分支",
                {"task_id": task["id"]},
            )

        recorded_commit = str(git_meta.get("commit") or "").strip()
        recorded_base = str(git_meta.get("base") or "").strip()
        reused_commit = False
        ref: str | None = None
        if recorded_commit and commit_exists(root, recorded_commit):
            ref = recorded_commit
            reused_commit = True
        elif recorded_base and commit_exists(root, recorded_base):
            ref = recorded_base
        if not ref:
            return _structured_error(
                409,
                "ref_unavailable",
                "任务提交与基线提交都已不存在，不能安全重建分支",
                {
                    "task_id": task["id"],
                    "branch": branch,
                    "commit": recorded_commit or None,
                    "base": recorded_base or None,
                },
            )

        branch_commit = resolve_commit(root, branch)
        created_ref = False
        if branch_commit:
            if work.is_dir():
                return _structured_error(
                    409,
                    "branch_exists",
                    f"任务分支已存在，无需重建：{branch}",
                    {"branch": branch, "task_id": task["id"]},
                )
            if branch_commit != ref:
                return _structured_error(
                    409,
                    "branch_exists",
                    f"任务分支已存在但指向其他提交：{branch}",
                    {
                        "branch": branch,
                        "task_id": task["id"],
                        "branch_commit": branch_commit,
                        "expected_ref": ref,
                    },
                )
            # Idempotent retry: the branch was created by a previous attempt
            # whose worktree creation failed, and it already points at the
            # expected commit. Continue by recreating only the worktree.
        else:
            try:
                create_branch_ref(root, branch, ref)
                created_ref = True
                branch_commit = ref
            except GitError as exc:
                code = "branch_exists" if "already exists" in str(exc).lower() else "branch_rebuild_failed"
                return _structured_error(
                    409,
                    code,
                    f"重建任务分支失败：{exc}",
                    {"branch": branch, "task_id": task["id"], "ref": ref},
                )

        worktree_rebuilt = False
        if not work.is_dir():
            try:
                create_worktree(
                    root,
                    work,
                    task_id=str(task["id"]),
                    title=str(task.get("title") or ""),
                    base_ref=str(branch_commit or ref),
                    branch=branch,
                    replace=False,
                )
            except GitError as exc:
                if created_ref:
                    try:
                        delete_worktree_branch(root, branch)
                    except GitError:
                        pass
                return _structured_error(
                    409,
                    "worktree_rebuild_failed",
                    f"工作副本创建失败，已回滚新建分支：{exc}",
                    {"branch": branch, "task_id": task["id"], "ref": ref},
                )
            worktree_rebuilt = True

        meta = dict(git_meta)
        meta["branch"] = branch
        meta["base"] = ref
        if not (recorded_commit and commit_exists(root, recorded_commit)):
            meta.pop("commit", None)
        meta["merge"] = {
            "merged": False,
            "blocked": False,
            "conflict": False,
            "discarded": False,
            "reason": None,
            "branch": branch,
            "base": ref,
            "sha": None,
            "files": [],
        }
        ctx.app.tasks.set_result_key(task["id"], "git", meta)
        _restore_after_merge_resolution(
            ctx,
            task,
            blocked_reasons={"merge_missing_branch"},
            reason="branch_rebuilt",
        )
        ctx.app.board.append_event(
            project_id=project["id"],
            kind="task.branch.rebuilt",
            summary=f"已重建任务分支 {branch}（{str(ref)[:8]}）：{task['title']}",
            actor="human",
            task_id=task["id"],
            data={
                "branch": branch,
                "base": ref,
                "task_id": task["id"],
                "reused_commit": reused_commit,
                "worktree_rebuilt": worktree_rebuilt,
            },
        )
        return {
            "rebuilt": True,
            "branch": branch,
            "base": ref,
            "sha": ref,
            "reused_commit": reused_commit,
        }

    @api.get("/api/tasks/{tid}/commit")
    def task_commit(tid: str) -> dict[str, Any]:
        """Return the git commit produced by this task (from ``result["git"]``)."""
        task = ctx.task(tid)
        git_meta = dict((task.get("result") or {}).get("git") or {})
        sha = git_meta.get("commit")
        return {
            "task_id": task["id"],
            "available": bool(git_meta),
            "sha": sha,
            "commit": sha,
            "branch": git_meta.get("branch"),
            "base": git_meta.get("base"),
            "files": git_meta.get("files") or [],
            "error": git_meta.get("error"),
        }

    @api.post("/api/tasks/{tid}/discard-worktree")
    def discard_worktree(
        tid: str,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        """删除任务工作副本；可选同时删除任务分支。

        默认（``delete_branch=false``）只删 ``work/`` 目录并保留分支，保持旧语义。
        ``delete_branch=true`` 时先移除 worktree，再用
        :func:`delete_worktree_branch` 强制删除任务分支；worktree 不存在但分支
        存在时同样可以删除。当前基线分支永远不允许删除（409）。
        """
        task = ctx.task(tid)
        project = _project(ctx, task["project_id"])
        root = Path(project["root_path"])
        work = _workdir(ctx, task)
        data = payload or {}
        delete_branch = bool(data.get("delete_branch", False))

        git_meta = dict((task.get("result") or {}).get("git") or {})
        branch = str(git_meta.get("branch") or "") or branch_for_task(
            str(task["id"]), str(task.get("title") or "")
        )

        # 删除前先确认目标不是当前基线分支，避免把仓库 checkout 的分支删掉。
        if delete_branch and branch and current_branch(root) == branch:
            raise HTTPException(409, f"拒绝删除当前基线分支：{branch}")

        existed = work.is_dir()
        if existed:
            remove_worktree(root, work, branch=None, delete_branch=False)

        branch_deleted = False
        if delete_branch:
            try:
                branch_deleted = delete_worktree_branch(root, branch)
            except GitError as exc:
                raise HTTPException(400, f"删除任务分支失败：{exc}") from exc

        if existed and branch_deleted:
            message = f"已删除工作副本与任务分支 {branch}"
        elif existed and delete_branch:
            message = f"已删除工作副本；任务分支 {branch} 不存在，未删除"
        elif existed:
            message = "已删除工作副本（任务分支保留）"
        elif branch_deleted:
            message = f"没有工作副本，已删除任务分支 {branch}"
        elif delete_branch:
            message = f"没有工作副本；任务分支 {branch} 不存在，未删除"
        else:
            message = "没有工作副本"

        branch_exists = worktree_branch_exists(root, branch)
        if delete_branch and git_meta and not branch_exists:
            # 放弃任务分支后必须回写状态：否则刷新页面仍显示冲突/待合并，
            # 而分支其实已经不存在（点击合并只会得到 409）。
            result = dict(task.get("result") or {})
            meta = dict(git_meta)
            meta["branch"] = branch or meta.get("branch")
            meta["merge"] = {
                "merged": False,
                "conflict": False,
                "blocked": False,
                "skipped": False,
                "discarded": True,
                "reason": "discarded",
                "sha": None,
                "branch": branch,
                "base": meta.get("base"),
                "files": [],
                "dirty_files": [],
                "error": None,
                "source": "manual",
            }
            result["git"] = meta
            ctx.app.tasks.update(task["id"], result=result)
            ctx.app.board.append_event(
                project_id=project["id"],
                kind="task.branch.discarded",
                summary=f"已放弃任务分支 {branch}：{task['title']}",
                actor="human",
                task_id=task["id"],
                data={"branch": branch, "branch_deleted": branch_deleted},
            )

        return {
            "discarded": existed,
            "branch_deleted": branch_deleted,
            "branch": branch
            if (existed or branch_deleted or branch_exists or git_meta.get("branch"))
            else None,
            "message": message,
        }
