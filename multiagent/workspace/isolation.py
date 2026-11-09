"""隔离工作副本与改动回收（需求 32-37）。

- ``shared``  ：直接在工作区原目录操作，适合只读 / 审阅类任务；
- ``copy``    ：为任务拷贝一份独立工作副本，产出的改动在验收通过后回收；
- ``worktree``：工作区是 git 仓库时用 ``git worktree``，否则退化为 ``copy``。

同时提供文件树快照与差异计算（从 :mod:`.changes` 再导出）。
"""

from __future__ import annotations

import os
import logging
import shutil
from pathlib import Path
from typing import Any

from ..config import Config
from ..util import ensure_dir, run_command, sha256_file as _sha256
from .changes import (
    DEFAULT_EXCLUDES,
    PathEscapeError,
    _matches_scope,
    _within_bounds,
    copy_tree,
    diff_trees,
    effective_excludes,
    is_excluded,
    normalize_rel_path,
    resolve_workspace_path,
    snapshot_tree,
    walk_files,
)

__all__ = ["IsolationService", "snapshot_tree", "diff_trees", "copy_tree"]

LOGGER = logging.getLogger(__name__)


class WorkspaceDriftError(RuntimeError):
    """工作区在任务执行期间被人工/外部进程改动，自动回收会覆盖这些改动。"""

    def __init__(self, paths: list[str], message: str | None = None):
        self.paths = sorted(set(paths))
        super().__init__(message or f"工作区在任务执行期间已被改动：{', '.join(self.paths[:5])}")


def _within_dir(root: Path, rel: str) -> Path | None:
    """在 root 下拼出合法落点；rel 越界返回 None。"""
    try:
        normalized = normalize_rel_path(rel)
    except PathEscapeError:
        return None
    candidate = Path(os.path.normpath(os.path.join(str(Path(root).absolute()), *normalized.split("/"))))
    if not _within_bounds(Path(root).absolute(), candidate):
        return None
    return candidate


class IsolationService:
    def __init__(self, db, config: Config):
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ 工作副本
    def task_dir(self, project_id: str, task_id: str) -> Path:
        return ensure_dir(self.config.runtime_dir / project_id / "tasks" / task_id)

    def workdir(self, project_id: str, task_id: str) -> Path:
        return self.task_dir(project_id, task_id) / "work"

    def baseline_dir(self, project_id: str, task_id: str) -> Path:
        return self.task_dir(project_id, task_id) / "baseline"

    def prepare(self, project: dict[str, Any], task: dict[str, Any]) -> Path:
        """准备任务工作副本，并保留一份「改动前基线」用于差异与回滚。

        行为与 :meth:`prepare_ex` 完全一致，只是不返回隔离决策信息。
        """
        return self.prepare_ex(project, task)[0]

    def prepare_ex(
        self, project: dict[str, Any], task: dict[str, Any]
    ) -> tuple[Path, dict[str, Any]]:
        """准备任务工作副本，并返回实际隔离模式与降级原因。

        无论哪种隔离模式，都会保留一份「改动前基线」用于差异与回滚：
        - 差异 = diff(baseline, 工作副本)
        - 回滚 = 从 baseline 还原（即使 ``shared`` 模式也有原始内容）

        返回的 info 至少包含 ``requested_mode`` / ``mode`` / ``workdir`` /
        ``degraded`` / ``degrade_code`` / ``degrade_reason``。调用方据此把
        「静默降级」变成可见状态，而不是只能翻日志。
        """
        requested_mode = str(
            task.get("isolation") or self.config.policy_get("isolation.default", "copy")
        ).lower()
        mode = requested_mode
        degraded = False
        degrade_code: str | None = None
        degrade_reason: str | None = None

        if task.get("read_only"):
            # 只读协作必须是真只读：强制用独立副本，绝不触碰原工作区。
            # 这是策略要求，不算非预期降级（requested_mode 仍记录原请求）。
            mode = "copy"
        elif (
            mode == "shared"
            and str(task.get("kind") or "generic") in {"code", "doc", "data", "ppt", "diagrams", "web"}
            and not (task.get("write_scope") or [])
        ):
            # 修改型任务在 shared + 空写范围下等于「Agent 可直写整个工作区」。
            # 这里做最后一道运行时兜底；创建入口会先给出确认/警告。
            mode = "copy"
            degraded = True
            degrade_code = "unscoped_shared"
            degrade_reason = (
                "shared 模式下修改型任务未声明 write_scope，"
                "为避免 Agent 直接改写整个工作区，已改用独立工作副本"
            )
            LOGGER.warning("shared 模式缺少 write_scope，安全兜底退化为 copy")

        root = Path(project["root_path"])

        if mode == "worktree":
            if not self._is_git_repo(root):
                mode = "copy"
                degraded = True
                degrade_code = "not_git_repo"
                degrade_reason = "工作区不是 Git 仓库"
                LOGGER.warning("worktree 不可用：工作区不是 Git 仓库，退化为 copy")
            elif not self._git_is_clean(root):
                # 工作区有未提交改动时，`git worktree add HEAD` 拿到的是旧树，
                # 而基线快照是当前工作树——两者 diff 会把上一个任务刚交付的文件
                # 误判成“本任务删除”。宁可退化为 copy，也不用不一致的树执行任务。
                mode = "copy"
                degraded = True
                degrade_code = "dirty_workspace"
                degrade_reason = "工作区有未提交改动，worktree 不可用"
                LOGGER.warning("worktree 不可用：工作区有未提交改动，退化为 copy")

        baseline = self.baseline_dir(project["id"], task["id"])
        work = self.workdir(project["id"], task["id"])
        shutil.rmtree(baseline, ignore_errors=True)
        if mode != "shared":
            shutil.rmtree(work, ignore_errors=True)

        if mode == "shared":
            ensure_dir(baseline)
            copy_tree(root, baseline, config=self.config)
            return root, self._isolation_info(
                requested_mode, mode, root, degraded, degrade_code, degrade_reason
            )

        if mode == "worktree":
            try:
                from ..gitops import create_worktree

                create_worktree(
                    root,
                    work,
                    task_id=str(task["id"]),
                    title=str(task.get("title") or ""),
                    replace=True,
                )
                # 基线必须来自**同一棵 worktree**，而不是工作区原目录：
                # Windows 上 git checkout 会做 CRLF 转换、忽略文件也不会进 worktree，
                # 用原目录做基线会把每个文本文件都判成“被修改”，把构建产物判成“删除”。
                # worktree 刚创建、Agent 还没跑，此刻复制一份就是与执行环境完全一致的基线。
                # 注意：这里不再先复制 root→baseline，worktree 成功路径只复制一次。
                ensure_dir(baseline)
                copy_tree(work, baseline, config=self.config)
                return work, self._isolation_info(
                    requested_mode, mode, work, degraded, degrade_code, degrade_reason
                )
            except Exception as exc:  # noqa: BLE001 - worktree 不可用时退化为 copy
                # 失败/降级路径先清掉可能半成品的 work/baseline，再走完整 copy 流程。
                shutil.rmtree(work, ignore_errors=True)
                shutil.rmtree(baseline, ignore_errors=True)
                mode = "copy"
                degraded = True
                degrade_code = "worktree_failed"
                degrade_reason = f"创建 git worktree 失败：{type(exc).__name__}: {exc}"
                LOGGER.warning(
                    "worktree 创建失败（%s: %s），退化为 copy", type(exc).__name__, exc
                )

        # copy（默认 / worktree 降级时）：先 root→baseline，再 baseline→work
        ensure_dir(baseline)
        copy_tree(root, baseline, config=self.config)
        ensure_dir(work)
        copy_tree(baseline, work, config=self.config)
        return work, self._isolation_info(
            requested_mode, mode, work, degraded, degrade_code, degrade_reason
        )

    @staticmethod
    def _isolation_info(
        requested_mode: str,
        mode: str,
        workdir: Path,
        degraded: bool,
        degrade_code: str | None,
        degrade_reason: str | None,
    ) -> dict[str, Any]:
        return {
            "requested_mode": requested_mode,
            "mode": mode,
            "workdir": str(Path(workdir).absolute()),
            "degraded": degraded,
            "degrade_code": degrade_code,
            "degrade_reason": degrade_reason,
        }

    def discard(self, project_id: str, task_id: str) -> None:
        """清理任务的工作副本与基线副本。

        **只清 ``work/`` 与 ``baseline/``**——``task_dir`` 下面还有 ``runs/``，
        里面是每次调用的 ``prompt.txt / stdout.txt / stderr.txt / transcript.json``
        （需求 43 失败保留现场、46 完整记录每次调用、50 同题多产出对比）。
        删掉 ``runs/`` 等于把历史尝试的证据物理销毁，扇出/重试场景下直接让
        这三条需求变成假的。需要彻底清除任务全部痕迹时用 :meth:`purge_task`。
        """
        shutil.rmtree(self.workdir(project_id, task_id), ignore_errors=True)
        shutil.rmtree(self.baseline_dir(project_id, task_id), ignore_errors=True)

    def purge_task(self, project_id: str, task_id: str) -> None:
        """彻底删除任务的全部运行痕迹（含 ``runs/`` 调用证据）。

        语义明确、必须显式调用：只在「任务被永久删除」这类场景使用，
        绝不能被常规的收尾流程顺手调用。
        """
        shutil.rmtree(self.task_dir(project_id, task_id), ignore_errors=True)

    def run_evidence_dir(self, project_id: str, task_id: str, run_id: str | None = None) -> Path:
        """某次调用（或整个任务）的证据目录 ``<task_dir>/runs[/<run_id>]``。"""
        base = self.task_dir(project_id, task_id) / "runs"
        return base / run_id if run_id else base

    @staticmethod
    def _is_git_repo(root: Path) -> bool:
        return (root / ".git").exists()

    @staticmethod
    def _git_is_clean(root: Path) -> bool:
        """已跟踪文件是否有未提交改动（忽略 ``.multiagent`` 控制面）。

        ``.multiagent`` 是工作台自己的运行目录：即使项目把它提交进了仓库，
        它的变动也不影响 worktree 的任务基线，不应因此把 worktree 降级成 copy。
        与 ``gitops.merge_worktree_branch`` 的脏树判定保持同一口径。
        """
        try:
            code, out, _err, _ = run_command(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=root,
                timeout=20,
            )
            if code != 0:
                return False
            for line in out.splitlines():
                if not line.strip():
                    continue
                path = line[3:].strip().replace("\\", "/")
                candidates = [part.strip() for part in path.split("->")] if "->" in path else [path]
                if all(
                    item == ".multiagent" or item.startswith(".multiagent/")
                    for item in candidates
                ):
                    continue
                return False
            return True
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------ 差异
    def baseline(self, workdir: Path) -> dict[str, dict[str, Any]]:
        return snapshot_tree(workdir, config=self.config)

    def current(self, workdir: Path) -> dict[str, dict[str, Any]]:
        return snapshot_tree(workdir, config=self.config)

    def changes(self, before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
        return diff_trees(before, after)

    # ------------------------------------------------------------------ 回收
    @staticmethod
    def _safe_rel(rel: str) -> str | None:
        """归一 rel 并拒绝一切越界形式（``..``/绝对路径/UNC/盘符）。"""
        try:
            return normalize_rel_path(rel)
        except PathEscapeError:
            return None

    @staticmethod
    def _safe_copy_source(workdir: Path, rel: str) -> Path | None:
        """取工作副本里可安全回收的源文件。

        拒绝三类：路径越界、源是符号链接、源的真实路径逃出工作副本——
        否则「agent 在工作副本里放一个指向工作区外文件的链接」就能把外部内容
        夹带进原工作区（需求 35/37）。
        """
        candidate = _within_dir(workdir, rel)
        if candidate is None or not candidate.is_file():
            return None
        if candidate.is_symlink():
            return None
        work_real = Path(os.path.realpath(workdir))
        if not _within_bounds(work_real, Path(os.path.realpath(candidate))):
            return None
        return candidate

    def commit_back(
        self,
        project: dict[str, Any],
        workdir: Path,
        changes: list[dict[str, Any]],
        *,
        only_in_scope: bool = True,
        scope: list[str] | None = None,
        read_only: bool = False,
        verify_current: bool = True,
    ) -> list[str]:
        """把工作副本里的改动写回工作区原目录。返回已回收的相对路径。

        ``verify_current`` 默认开启：写回前逐个比对目标文件的当前哈希与任务基线。
        如果用户在 Agent 跑的同时手动改了同一个文件，自动回收会**拒绝覆盖**并抛
        :class:`WorkspaceDriftError`，由上层转人工确认（需求 32 / 42）。
        """
        root = Path(project["root_path"])
        if Path(workdir).resolve() == root.resolve():
            return [c["rel_path"] for c in changes]
        if read_only:
            return []
        if verify_current:
            drift = self.current_drift(root, changes)
            if drift:
                raise WorkspaceDriftError(drift)
        committed: list[str] = []
        excludes = effective_excludes(root, self.config)

        for change in changes:
            rel = change["rel_path"]
            safe = self._safe_rel(rel)
            if safe is None:
                continue  # 越界路径一律不回收
            if is_excluded(safe, excludes):
                continue  # 临时目录/缓存永远不进入交付物
            if only_in_scope and scope and not _matches_scope(safe, scope):
                continue
            try:
                target = resolve_workspace_path(root, safe)
            except PathEscapeError:
                continue
            if change["action"] == "delete":
                if target.exists() and target.is_file():
                    target.unlink()
                committed.append(rel)
                continue
            source = self._safe_copy_source(Path(workdir), safe)
            if source is None:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            committed.append(rel)
        return sorted(set(committed))

    def current_drift(self, root: Path, changes: list[dict[str, Any]]) -> list[str]:
        """检测「任务基线之后，目标文件是否又被外部改动」。

        只对能从 ``before_hash`` 判断的改动生效；没有基线哈希（例如旧数据）时
        保持保守——不阻断，交给冲突日志/人工复核。
        """
        drift: list[str] = []
        for change in changes:
            rel = str(change.get("rel_path") or "")
            try:
                safe = self._safe_rel(rel)
                if safe is None:
                    continue
                target = resolve_workspace_path(root, safe)
            except (PathEscapeError, OSError):
                drift.append(rel)
                continue
            action = change.get("action")
            exists = target.exists()
            if action == "create":
                if exists:
                    # 同内容重复写回是安全的；不同内容必须人工处理
                    after = change.get("after_hash")
                    try:
                        if not after or not target.is_file() or _sha256(target) != after:
                            drift.append(rel)
                    except OSError:
                        drift.append(rel)
                continue
            before = change.get("before_hash")
            if not before:
                continue
            if action == "delete":
                if exists and target.is_file() and _sha256(target) != before:
                    drift.append(rel)
                continue
            if not exists or not target.is_file():
                drift.append(rel)
                continue
            try:
                if _sha256(target) != before:
                    drift.append(rel)
            except OSError:
                drift.append(rel)
        return sorted(set(drift))

    def revert_out_of_scope(
        self,
        project: dict[str, Any],
        workdir: Path,
        out_of_scope: list[str],
        baseline_dir: Path,
    ) -> None:
        """把越权（不在写范围内）的改动还原成运行前的内容（从基线副本取）。"""
        baseline = Path(baseline_dir)
        for rel in out_of_scope:
            safe = self._safe_rel(rel)
            try:
                target = resolve_workspace_path(workdir, safe) if safe else None
            except PathEscapeError:
                target = None
            if target is None:
                continue
            original = _within_dir(baseline, safe)
            if original is not None and original.is_file():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, target)
            elif target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
