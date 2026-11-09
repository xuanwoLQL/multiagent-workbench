"""任务工作副本的查看与手动同步（用户真实场景：产物都在隔离副本里，以为丢了）。

背景
----
``isolation=copy`` 时 Agent 写在 ``<runtime>/<project>/tasks/<task>/work/`` 下；验收通过后
runner 会自动 ``commit_back`` 到项目根目录。但如果任务被取消 / 阻塞 / 验收失败，或者用户
中途就想看产物，项目根目录看起来「只有一个 .multiagent」，产物像是丢了
（真实现场：``~/myworld`` 根目录只有 ``.multiagent``，52 个产物都在隔离副本里）。

本模块只做**查看**与**手动同步**，不重写任何规则：

* ``pending`` = ``IsolationService.baseline(baseline_dir)`` 与 ``current(workdir)`` 的差异
  （与 runner 收尾判定完全同一套 diff 规则，也是 ``commit_back`` 的输入）；
* ``unsynced`` = 把上面的改动集合再与**项目根目录**逐个比对（大小 / sha256）——
  同步之后 ``pending`` 仍然非 0（文件还在工作副本里），UI 用 ``unsynced`` 显示
  「待同步 N」才不会一直以为没同步；
* 回收 = ``IsolationService.commit_back(..., only_in_scope=True, scope=write_scope)``
  （目录边界 glob、符号链接/越界拒绝、read_only 保护都在那里）；
* 冲突 = ``ChangeJournal.conflicts()``（与「其他仍在进行中的任务」改同一文件）；
* 保护路径 = ``GuardService.protected_paths()``（.multiagent / config / .git 等）。

语义边界：

* **不改变任务状态**：running 也允许同步（UI 自行提示「任务仍在运行，结果可能不完整」）；
* ``read_only`` 任务拒绝同步（400）；
* ``workdir == root``（shared 模式）返回 ``already_shared``，不做任何写入；
* 只对比「任务自己的副本 vs 它的运行前基线」，**不扫整个工作区**。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from ..gitops import merge_status
from ..util import matches_any, sha256_file
from ..workspace.changes import _matches_scope  # 与 commit_back 完全同一套目录边界规则

LOGGER = logging.getLogger("multiagent.api.taskworkspace")

#: ``pending.files`` 最多返回多少条（避免一次响应把大任务的全部改动都搬出来）。
MAX_PENDING_FILES = 200
#: ``artifacts`` 最多返回多少条。
MAX_ARTIFACTS = 200
#: 事件里最多记录多少条已同步路径。
MAX_EVENT_FILES = 50


class SyncBlocked(RuntimeError):
    """同步被拒绝（API 层转成对应 HTTP 状态码，而不是 500）。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = int(status)


# --------------------------------------------------------------------------- 查询


def describe_task_workspace(app, task: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
    """``GET /api/tasks/{tid}/workspace`` 的响应体。"""
    project_id = project["id"]
    task_id = task["id"]
    root = Path(project["root_path"])
    isolation_mode = _isolation_mode(app, task)
    workdir = _task_workdir(app, task, project, isolation_mode)
    baseline_dir = app.isolation.baseline_dir(project_id, task_id)
    workdir_exists = workdir.is_dir()
    is_original = _same_path(workdir, root)
    running = _task_running(app, task)
    pending, changes, after_snapshot, pending_error = _pending_changes(
        app, workdir, baseline_dir, is_original=is_original
    )
    unsynced = _unsynced_changes(
        root, changes, after_snapshot, is_original=is_original
    )
    # worktree 任务验收后默认已 `git merge` 回基线；此时工作副本与根目录的
    # 字节差异可能只是 CRLF/构建产物，不应继续显示「待同步」。
    raw_result = task.get("result")
    if isinstance(raw_result, str):
        try:
            import json

            raw_result = json.loads(raw_result)
        except (TypeError, ValueError):
            raw_result = None
    git_meta = (raw_result or {}).get("git") if isinstance(raw_result, dict) else None
    git_status = merge_status(git_meta if isinstance(git_meta, dict) else None)
    if git_status == "merged":
        unsynced = {"count": 0, "files": [], "truncated": False}
    can_sync, reason = _eligibility(
        task,
        workdir_exists=workdir_exists,
        is_original=is_original,
        baseline_exists=baseline_dir.is_dir(),
        pending_error=pending_error,
    )
    if git_status == "merged":
        can_sync, reason = False, "该任务分支已合并到基线，无需再次同步"
    elif git_status == "discarded":
        can_sync, reason = False, "任务分支已放弃，无需同步"
        unsynced = {"count": 0, "files": [], "truncated": False}
    return {
        "task_id": task_id,
        "project_id": project_id,
        "isolation": isolation_mode,
        "root_path": str(root),
        "workdir": str(workdir),
        "workdir_exists": workdir_exists,
        "is_original": is_original,
        "running": running,
        "can_sync": can_sync,
        "sync_blocked_reason": reason,
        # git 交付信息与合并状态：审阅页据此显示「已合并/冲突」而不是「待同步」。
        # 只增字段，旧客户端忽略即可。
        "git": git_meta if isinstance(git_meta, dict) else None,
        "merge_status": git_status,
        # pending = baseline vs workdir（commit_back 的输入，同步后仍会保留）
        "pending": pending,
        # unsynced = workdir vs 项目根目录（「还没落到根目录」的改动，UI 用它显示「待同步 N」）
        "unsynced": unsynced,
        "artifacts": _artifacts(app, task_id),
    }


# --------------------------------------------------------------------------- 同步


def sync_task_workspace(
    app, task: dict[str, Any], project: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any]:
    """``POST /api/tasks/{tid}/sync``：把工作副本的改动回收进项目根目录。

    ``dry_run=True`` 只算「会同步什么」不落盘。返回值形状见契约；额外字段
    （``task_id`` / ``project_id`` / ``dry_run`` / ``already_shared`` / ``planned``）只增不改。
    """
    project_id = project["id"]
    root = Path(project["root_path"])
    isolation_mode = _isolation_mode(app, task)
    workdir = _task_workdir(app, task, project, isolation_mode)

    if task.get("read_only"):
        raise SyncBlocked(400, "只读任务不允许同步（它不会改动工作区）")
    if _same_path(workdir, root):
        return _sync_response(
            task,
            project_id,
            dry_run=dry_run,
            committed=[],
            skipped=[],
            skipped_details=[],
            conflicts=[],
            out_of_scope=[],
            already_shared=True,
            message="shared 模式直接在工作区操作，改动已在原目录，无需同步",
        )

    baseline_dir = app.isolation.baseline_dir(project_id, task["id"])
    if not workdir.is_dir():
        raise SyncBlocked(409, "工作副本不存在（任务尚未执行或已被清理），无法同步")
    if not baseline_dir.is_dir():
        raise SyncBlocked(409, "缺少运行前基线（任务尚未执行或基线已被清理），无法计算要同步的改动")

    try:
        before = app.isolation.baseline(baseline_dir)
        after = app.isolation.current(workdir)
        changes = app.isolation.changes(before, after)
    except OSError as exc:
        raise SyncBlocked(409, f"计算工作副本改动失败：{exc}") from exc

    scope = [str(item) for item in (task.get("write_scope") or [])]
    protected = app.guard.protected_paths()
    rel_paths = [str(change["rel_path"]) for change in changes]
    conflict_paths = _conflicts(app, project_id, task["id"], rel_paths)
    try:
        drift_paths = set(app.isolation.current_drift(root, changes))
    except Exception:  # noqa: BLE001 - 漂移检测失败不阻塞预览，提交时还有二次校验
        LOGGER.exception("检测工作区漂移失败")
        drift_paths = set()

    candidates: list[dict[str, Any]] = []
    skipped: list[str] = []
    skipped_details: list[dict[str, str]] = []
    out_of_scope: list[str] = []
    already_synced = 0
    for change in changes:
        rel = str(change["rel_path"])
        if _already_synced(root, rel, change, after.get(rel) or {}):
            # 重复点「同步」不应重复写盘；planned/committed 只列真正待同步的文件。
            already_synced += 1
            continue
        if rel in drift_paths:
            skipped.append(rel)
            skipped_details.append(
                {"rel_path": rel, "reason": "工作区文件在任务执行期间被外部改动，需人工处理"}
            )
            continue
        if rel in conflict_paths:
            skipped.append(rel)
            skipped_details.append(
                {"rel_path": rel, "reason": "与其他运行中的任务并发改动同一文件，需人工处理"}
            )
            continue
        if scope and not _matches_scope(rel, scope):
            out_of_scope.append(rel)
            skipped.append(rel)
            skipped_details.append({"rel_path": rel, "reason": "不在任务 write_scope 内"})
            continue
        if protected and matches_any(rel, protected):
            skipped.append(rel)
            skipped_details.append(
                {"rel_path": rel, "reason": "命中保护路径（.multiagent / config / .git 等）"}
            )
            continue
        candidates.append(change)

    planned = [str(change["rel_path"]) for change in candidates]
    if dry_run:
        return _sync_response(
            task,
            project_id,
            dry_run=True,
            committed=[],
            skipped=skipped,
            skipped_details=skipped_details,
            conflicts=sorted(conflict_paths),
            out_of_scope=out_of_scope,
            already_shared=False,
            already_synced=already_synced,
            planned=planned,
            message=f"试运行：将同步 {len(planned)} 个文件到工作目录"
            + (f"，跳过 {len(skipped)} 个" if skipped else ""),
        )

    if not changes:
        return _sync_response(
            task,
            project_id,
            dry_run=False,
            committed=[],
            skipped=skipped,
            skipped_details=skipped_details,
            conflicts=sorted(conflict_paths),
            out_of_scope=out_of_scope,
            already_shared=False,
            already_synced=already_synced,
            message="工作副本没有改动，无需同步",
        )

    try:
        committed = app.isolation.commit_back(
            project,
            workdir,
            candidates,
            only_in_scope=True,
            scope=scope,
            read_only=False,
        )
    except Exception as exc:  # noqa: BLE001 - WorkspaceDriftError 等要转成可读的跳过原因
        from ..workspace.isolation import WorkspaceDriftError

        if isinstance(exc, WorkspaceDriftError):
            skipped.extend(exc.paths)
            for rel in exc.paths:
                skipped_details.append(
                    {"rel_path": rel, "reason": "工作区文件在任务执行期间被外部改动，已阻止覆盖"}
                )
            committed = []
        else:
            raise
    committed_set = set(committed)
    for change in candidates:
        rel = str(change["rel_path"])
        if rel in skipped:
            continue
        if rel not in committed_set:
            # commit_back 会静默跳过越界/符号链接等不安全路径，这里显式说明
            skipped.append(rel)
            skipped_details.append({"rel_path": rel, "reason": "路径不安全或回收失败，未写入"})

    if committed:
        try:
            app.board.append_event(
                project_id=project_id,
                kind="task.synced",
                summary=f"手动同步 {len(committed)} 个文件到工作目录",
                actor="human",
                task_id=task["id"],
                data={
                    "committed": committed[:MAX_EVENT_FILES],
                    "skipped": len(skipped),
                    "out_of_scope": out_of_scope[:MAX_EVENT_FILES],
                },
            )
        except Exception:  # noqa: BLE001 - 事件写失败不影响已完成的同步
            LOGGER.exception("记录 task.synced 事件失败")

    message = f"已同步 {len(committed)} 个文件到工作目录"
    if skipped:
        message += f"，跳过 {len(skipped)} 个（不在写范围 / 保护路径 / 冲突）"
    return _sync_response(
        task,
        project_id,
        dry_run=False,
        committed=committed,
        skipped=skipped,
        skipped_details=skipped_details,
        conflicts=sorted(conflict_paths),
        out_of_scope=out_of_scope,
        already_shared=False,
        already_synced=already_synced,
        message=message,
    )


# --------------------------------------------------------------------------- 内部


def _sync_response(
    task: dict[str, Any],
    project_id: str,
    *,
    dry_run: bool,
    committed: list[str],
    skipped: list[str],
    skipped_details: list[dict[str, str]],
    conflicts: list[str],
    out_of_scope: list[str],
    already_shared: bool,
    message: str,
    planned: list[str] | None = None,
    already_synced: int = 0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": task["id"],
        "project_id": project_id,
        "dry_run": bool(dry_run),
        "already_shared": bool(already_shared),
        "already_synced": int(already_synced),
        "committed": list(committed),
        "skipped": list(skipped),
        "skipped_details": list(skipped_details),
        "conflicts": list(conflicts),
        "out_of_scope": list(out_of_scope),
        "message": message,
    }
    if planned is not None:
        payload["planned"] = list(planned)
    return payload


def _isolation_mode(app, task: dict[str, Any]) -> str:
    """任务实际生效的隔离模式（与 ``IsolationService.prepare`` 的判定一致）。"""
    mode = str(task.get("isolation") or app.config.policy_get("isolation.default", "copy")).lower()
    if task.get("read_only"):
        # 只读协作必须是真只读：prepare() 会强制用独立副本，绝不触碰原工作区
        mode = "copy"
    return mode


def _task_workdir(
    app, task: dict[str, Any], project: dict[str, Any], isolation_mode: str
) -> Path:
    """任务的工作目录：优先用 runner 落库的 ``workdir``，否则按隔离模式推导。"""
    recorded = task.get("workdir")
    if recorded:
        return Path(str(recorded))
    if isolation_mode == "shared":
        return Path(project["root_path"])
    return app.isolation.workdir(project["id"], task["id"])


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - 极端路径错误时退回字符串比较
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(
            os.path.abspath(str(right))
        )


def _task_running(app, task: dict[str, Any]) -> bool:
    """任务是否仍在执行（状态 running，或有 status=running 的 run 行）。"""
    if str(task.get("status") or "") == "running":
        return True
    row = app.db.query_one(
        "SELECT 1 FROM runs WHERE task_id = ? AND status = 'running' LIMIT 1", (task["id"],)
    )
    return bool(row)


def _pending_changes(
    app, workdir: Path, baseline_dir: Path, *, is_original: bool
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]], str | None]:
    """``baseline vs workdir`` 差异（**只对比任务自己的目录**，不扫整个工作区）。

    返回 ``(pending, changes, workdir_snapshot, error)``：``changes`` 与 ``workdir_snapshot``
    给 :func:`_unsynced_changes` 复用，避免为了算「待同步」再扫一遍工作副本。
    """
    empty: dict[str, Any] = {"count": 0, "files": [], "truncated": False}
    if is_original or not workdir.is_dir() or not baseline_dir.is_dir():
        # shared 模式或还没跑过：不扫整个工作区（那可能非常大）
        return empty, [], {}, None
    try:
        before = app.isolation.baseline(baseline_dir)
        after = app.isolation.current(workdir)
        changes = app.isolation.changes(before, after)
    except OSError as exc:  # pragma: no cover - 目录读失败
        return empty, [], {}, f"计算工作副本改动失败：{exc}"

    files: list[dict[str, Any]] = []
    for change in changes[:MAX_PENDING_FILES]:
        rel = str(change.get("rel_path"))
        size = 0
        if change.get("action") != "delete":
            try:
                size = int((workdir / rel).stat().st_size)
            except OSError:
                size = 0
        files.append({"rel_path": rel, "action": change.get("action"), "size": size})
    return {
        "count": len(changes),
        "files": files,
        "truncated": len(changes) > MAX_PENDING_FILES,
    }, changes, after, None


def _unsynced_changes(
    root: Path,
    changes: list[dict[str, Any]],
    workdir_snapshot: dict[str, dict[str, Any]],
    *,
    is_original: bool,
) -> dict[str, Any]:
    """``workdir vs 项目根目录`` 的差异 = 「还没落到根目录」的改动（UI 显示「待同步 N」）。

    为什么需要它：``pending`` 是 baseline vs workdir（commit_back 的输入），**同步之后
    pending 仍然非 0**（文件还在工作副本里），UI 会一直以为没同步。``unsynced`` 逐个
    比对工作副本与根目录：根目录没有 → ``create``；大小或 sha256 不同 → ``modify``；
    两者都相同 → 已同步、不列出。根目录里「多出来」的文件不算。

    只检查**任务自己的改动集合**（baseline vs workdir），不扫整个工作区；
    ``delete`` 改动只有根目录仍存在时才算待同步。
    """
    empty: dict[str, Any] = {"count": 0, "files": [], "truncated": False}
    if is_original or not changes:
        return empty

    items: list[dict[str, Any]] = []
    for change in changes:
        rel = str(change.get("rel_path"))
        action = change.get("action")
        target = root / rel
        if action == "delete":
            try:
                still_there = target.is_file()
            except OSError:  # pragma: no cover - 极端路径错误
                still_there = False
            if still_there:
                items.append({"rel_path": rel, "action": "delete", "size": 0})
            continue

        meta = workdir_snapshot.get(rel) or {}
        work_size = int(meta.get("size") or 0)
        try:
            root_stat = target.stat()
        except OSError:
            items.append({"rel_path": rel, "action": "create", "size": work_size})
            continue
        if not target.is_file():
            items.append({"rel_path": rel, "action": "create", "size": work_size})
            continue
        if int(root_stat.st_size) != work_size:
            items.append({"rel_path": rel, "action": "modify", "size": work_size})
            continue
        work_sha = meta.get("sha256")
        if work_sha:
            try:
                root_sha = sha256_file(target)
            except OSError:  # pragma: no cover - 读失败按「需要同步」保守处理
                root_sha = None
            if root_sha != work_sha:
                items.append({"rel_path": rel, "action": "modify", "size": work_size})
    return {
        "count": len(items),
        "files": items[:MAX_PENDING_FILES],
        "truncated": len(items) > MAX_PENDING_FILES,
    }


def _already_synced(
    root: Path, rel: str, change: dict[str, Any], work_meta: dict[str, Any]
) -> bool:
    """工作副本里的这个改动是否已经与项目根目录一致（避免重复同步写盘）。"""
    target = root / rel
    if change.get("action") == "delete":
        try:
            return not target.is_file()
        except OSError:
            return False
    try:
        if not target.is_file():
            return False
        root_stat = target.stat()
    except OSError:
        return False
    if int(root_stat.st_size) != int(work_meta.get("size") or 0):
        return False
    work_sha = work_meta.get("sha256")
    if not work_sha:
        return True
    try:
        return sha256_file(target) == work_sha
    except OSError:
        return False


def _eligibility(
    task: dict[str, Any],
    *,
    workdir_exists: bool,
    is_original: bool,
    baseline_exists: bool,
    pending_error: str | None,
) -> tuple[bool, str | None]:
    """能不能手动同步（结构性判断；与任务是否 running 无关 —— running 允许同步）。"""
    if task.get("read_only"):
        return False, "只读任务不允许同步（它不会改动工作区）"
    if is_original:
        return False, "shared 模式直接在工作区操作，改动已在原目录，无需同步"
    if not workdir_exists:
        return False, "工作副本不存在（任务尚未执行或已被清理）"
    if not baseline_exists:
        return False, "缺少运行前基线（任务尚未执行或基线已被清理），无法计算待同步改动"
    if pending_error:
        return False, pending_error
    return True, None


def _conflicts(app, project_id: str, task_id: str, rel_paths: list[str]) -> set[str]:
    """与其他仍在进行中的任务对同一文件的改动（复用 ChangeJournal 的判定）。"""
    if not rel_paths:
        return set()
    try:
        rows = app.runner.journal.conflicts(project_id, task_id, rel_paths)
    except Exception:  # noqa: BLE001 - 冲突检测失败按「无冲突」继续，不能挡住手动同步
        LOGGER.exception("检测并发冲突失败，按无冲突处理")
        return set()
    return {str(row["rel_path"]) for row in rows if row.get("rel_path")}


def _artifacts(app, task_id: str) -> list[dict[str, Any]]:
    try:
        items = app.tasks.artifacts(task_id=task_id)
    except Exception:  # noqa: BLE001 - 产物表异常不应让工作副本接口 500
        LOGGER.exception("读取任务产物失败")
        return []
    return [
        {
            "id": item.get("id"),
            "role": item.get("role"),
            "rel_path": item.get("rel_path"),
            "size": item.get("size"),
            "created_at": item.get("created_at"),
        }
        for item in items[-MAX_ARTIFACTS:]
    ]
