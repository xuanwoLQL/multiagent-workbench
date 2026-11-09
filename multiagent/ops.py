"""生产运维：存储盘点、备份/恢复、保留清理、完整性与事件链校验。

这一层解决的是「跑得久了会怎样」：

* 运行目录、stdout/transcript、快照、blob 会无限增长；
* 数据库升级/恢复需要可验证的备份；
* 事件哈希链、SQLite 完整性需要能一键体检。

设计原则：

* **默认 dry-run**：清理只报告不删除，必须显式 ``apply=True``；
* **只删可再生成/已留档的数据**：run 行、任务、事件、审批记录永不因清理而消失；
* **审计优先**：所有清理/备份动作都写 events（actor=system）；
* **失败不静默**：备份/恢复/完整性校验失败一律抛错。
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .util import (
    ensure_dir,
    loads,
    new_id,
    now_iso,
    read_json,
    run_command,
    sha256_file,
    slugify,
    write_json,
)

#: 单个 run 的 stdout/stderr/transcript 保留上限（超出后截断，保留头尾）。
DEFAULT_MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
#: blob 未被引用多久后才允许回收（给「刚写入但还没落库」留窗口）。
DEFAULT_BLOB_GRACE_HOURS = 24
#: 备份保留份数。
DEFAULT_KEEP_BACKUPS = 20
#: 已合并 worktree 的默认保留天数（policy ``git.worktree_retention_days``）。
DEFAULT_WORKTREE_RETENTION_DAYS = 14.0
#: 允许清理 worktree 的终态任务；running/waiting/pending 一律不处理。
WORKTREE_PRUNE_STATUSES = {
    "success", "failed", "skipped", "cancelled", "blocked", "interrupted",
}


def _dir_size(path: str | os.PathLike[str]) -> int:
    total = 0
    base = Path(path)
    if not base.exists():
        return 0
    stack = [str(base)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += int(entry.stat(follow_symlinks=False).st_size)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _count_files(path: str | os.PathLike[str]) -> int:
    total = 0
    base = Path(path)
    if not base.exists():
        return 0
    stack = [str(base)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _iso_cutoff(days: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(days=max(0.0, float(days)))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _registered_worktrees(root: str | os.PathLike[str]) -> set[str]:
    """``git worktree list --porcelain`` 里登记的 worktree 路径（规范化）。"""
    try:
        code, out, _err, _ = run_command(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"], timeout=30
        )
    except Exception:  # noqa: BLE001 - 盘点失败按「不是 worktree」处理
        return set()
    if code != 0:
        return set()
    paths: set[str] = set()
    for line in (out or "").splitlines():
        if not line.startswith("worktree "):
            continue
        value = line[len("worktree "):].strip()
        if value:
            paths.add(os.path.normcase(os.path.realpath(value)))
    return paths


def _looks_like_worktree(
    root: str | os.PathLike[str] | None,
    workdir: str | os.PathLike[str],
    *,
    check_registry: bool = True,
) -> bool:
    """判断 ``workdir`` 是否是 Git linked worktree（而不是普通 copy 目录）。

    linked worktree 的标志是 ``.git`` 为文件；``.git`` 缺失但仍在
    ``git worktree list`` 中登记（半损坏现场）也算。``check_registry=False``
    时只做零成本的 ``.git`` 文件检查，用于避免为每个 copy 任务起 git 进程。
    """
    path = Path(workdir)
    try:
        if (path / ".git").is_file():
            return True
    except OSError:
        return False
    if not check_registry or root is None:
        return False
    return os.path.normcase(os.path.realpath(path)) in _registered_worktrees(root)


def _truncate_file(path: Path, limit: int) -> tuple[int, int]:
    """把大文件截成「头 1/3 + 尾 2/3」，返回 (原大小, 新大小)。"""
    try:
        size = path.stat().st_size
    except OSError:
        return 0, 0
    if size <= limit or limit <= 0:
        return size, size
    head_len = max(1, limit // 3)
    tail_len = max(1, limit - head_len)
    try:
        with open(path, "rb") as handle:
            head = handle.read(head_len)
            handle.seek(max(0, size - tail_len))
            tail = handle.read(tail_len)
        marker = f"\n…[证据已裁剪：原 {size} 字节，保留头尾共 {limit} 字节]…\n".encode("utf-8")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(head + marker + tail)
        tmp.replace(path)
        return size, path.stat().st_size
    except OSError:
        return size, size


class OpsService:
    def __init__(self, db: Database, config: Config, board=None):
        self.db = db
        self.config = config
        self.board = board

    # ------------------------------------------------------------------ 存储
    def storage(self) -> dict[str, Any]:
        data = Path(self.config.data_dir)
        db_path = Path(self.config.db_path)
        projects = [
            {
                "id": row["id"],
                "name": row["name"],
                "root_path": row["root_path"],
                "runtime_bytes": _dir_size(data / "runtime" / row["id"]),
                "runtime_files": _count_files(data / "runtime" / row["id"]),
            }
            for row in self.db.query("SELECT id, name, root_path FROM projects ORDER BY created_at")
        ]
        counts = {}
        for table in (
            "projects", "tasks", "runs", "events", "board_entries", "board_versions",
            "handoffs", "artifacts", "file_changes", "approvals", "snapshots",
            "scheduler_slots", "runtime_sessions", "run_events",
        ):
            try:
                counts[table] = int(self.db.query_one(f"SELECT COUNT(*) AS c FROM {table}")["c"])
            except Exception:  # noqa: BLE001 - 旧库/表缺失时不让盘点 500
                counts[table] = -1
        wal = Path(str(db_path) + "-wal")
        shm = Path(str(db_path) + "-shm")
        return {
            "data_dir": str(data),
            "db": {
                "path": str(db_path),
                "bytes": db_path.stat().st_size if db_path.exists() else 0,
                "wal_bytes": wal.stat().st_size if wal.exists() else 0,
                "shm_bytes": shm.stat().st_size if shm.exists() else 0,
                "schema_version": self.db.schema_version,
            },
            "dirs": {
                "runtime": {"bytes": _dir_size(data / "runtime"), "files": _count_files(data / "runtime")},
                "blobs": {"bytes": _dir_size(data / "blobs"), "files": _count_files(data / "blobs")},
                "snapshots": {"bytes": _dir_size(data / "snapshots"), "files": _count_files(data / "snapshots")},
                "logs": {"bytes": _dir_size(data / "logs"), "files": _count_files(data / "logs")},
                "backups": {"bytes": _dir_size(data / "backups"), "files": _count_files(data / "backups")},
            },
            "counts": counts,
            "projects": projects,
            "server_time": now_iso(),
        }

    # ------------------------------------------------------------------ 备份
    @property
    def backup_dir(self) -> Path:
        return ensure_dir(Path(self.config.data_dir) / "backups")

    def list_backups(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.backup_dir.glob("backup-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True):
            manifest = read_json(path.with_suffix(".json"), {}) or {}
            items.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "bytes": path.stat().st_size,
                    "sha256": manifest.get("sha256") or sha256_file(path),
                    "label": manifest.get("label") or "",
                    "created_at": manifest.get("created_at") or "",
                    "schema_version": manifest.get("schema_version"),
                }
            )
        return items

    def create_backup(self, label: str = "manual", *, actor: str = "system") -> dict[str, Any]:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        name = f"backup-{stamp}-{slugify(label or 'manual', 'manual', 24)}.sqlite3"
        target = self.backup_dir / name
        info = self.db.backup_to(target)
        manifest = {
            "id": new_id("bak"),
            "label": label or "manual",
            "created_at": now_iso(),
            "app": "multiagent",
            "schema_version": info.get("schema_version"),
            "bytes": info.get("bytes"),
            "sha256": info.get("sha256"),
            "counts": self.storage().get("counts", {}),
        }
        write_json(target.with_suffix(".json"), manifest)
        self._event("system.backup", f"创建数据库备份：{name}", actor=actor, data={"path": str(target), "label": label})
        return {**info, "label": label, "manifest_path": str(target.with_suffix(".json"))}

    def prune_backups(self, keep: int = DEFAULT_KEEP_BACKUPS, *, dry_run: bool = True) -> dict[str, Any]:
        files = sorted(self.backup_dir.glob("backup-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)
        remove = files[max(0, int(keep)):]
        freed = sum(p.stat().st_size for p in remove if p.exists())
        if not dry_run:
            for path in remove:
                for target in (path, path.with_suffix(".json")):
                    try:
                        target.unlink()
                    except OSError:
                        pass
        return {"removed": [p.name for p in remove], "freed_bytes": freed, "dry_run": dry_run}

    def restore(self, path: str | os.PathLike[str], *, backup_current: bool = True) -> dict[str, Any]:
        result = self.db.restore_from(path, backup_current=backup_current)
        self._event(
            "system.restore",
            f"从备份恢复数据库：{Path(path).name}",
            actor="human",
            data={"path": str(path), "pre_restore_backup": result.get("pre_restore_backup")},
        )
        return result

    # ------------------------------------------------------------------ 清理
    def prune(
        self,
        *,
        dry_run: bool = True,
        keep_runs_per_task: int = 3,
        keep_days: float = 30,
        keep_snapshots: int = 20,
        keep_backups: int = DEFAULT_KEEP_BACKUPS,
        max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
        prune_workspaces_days: float = 30,
        prune_worktrees_days: float | None = None,
        prune_blobs: bool = True,
        blob_grace_hours: float = DEFAULT_BLOB_GRACE_HOURS,
    ) -> dict[str, Any]:
        """按保留策略清理可再生成数据。

        * 每个任务保留最近 ``keep_runs_per_task`` 次 run 的完整证据；
          更老的 run 删除 ``workspace/`` 归档、裁剪大输出文件，但 **run 行保留**；
        * 终态任务超过 ``prune_workspaces_days`` 的 ``work/``/``baseline/`` 可删除；
        * 已合并且超过 ``prune_worktrees_days`` 的 Git worktree 会先注销注册再删目录，
          任务分支与提交永远保留；未合并的 worktree 绝不清理。
          ``prune_worktrees_days=None`` 时读取 policy ``git.worktree_retention_days``
          （默认 14 天）；
        * 每个项目只保留最近 ``keep_snapshots`` 个快照；
        * 未被 file_changes / artifacts 引用的 blob 超过宽限期后回收；
        * 备份只保留最近 ``keep_backups`` 份；
        * 最后 ``VACUUM``（apply 时）。
        """
        if prune_worktrees_days is None:
            prune_worktrees_days = self.config.policy_get(
                "git.worktree_retention_days", DEFAULT_WORKTREE_RETENTION_DAYS
            )
        try:
            worktree_days = max(0.0, float(prune_worktrees_days))
        except (TypeError, ValueError):
            worktree_days = DEFAULT_WORKTREE_RETENTION_DAYS

        report: dict[str, Any] = {
            "dry_run": bool(dry_run),
            "runs": {"pruned_workspaces": [], "truncated": [], "kept": 0},
            "tasks": {"removed_workspaces": []},
            "worktrees": {"removed": [], "kept": 0, "skipped_unmerged": 0},
            "snapshots": {"removed": []},
            "blobs": {"removed": [], "freed_bytes": 0},
            "backups": {},
            "freed_bytes": 0,
            "vacuum": None,
        }
        cutoff = _iso_cutoff(keep_days)
        workspace_cutoff = _iso_cutoff(prune_workspaces_days)

        # 1) run 证据（附带 project.root_path / result，供 worktree 清理复用）
        tasks = self.db.query(
            "SELECT t.id, t.project_id, t.status, t.finished_at, t.updated_at, "
            "t.title, t.result, p.root_path "
            "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id"
        )
        for task in tasks:
            runs = self.db.query(
                "SELECT id, started_at, finished_at, status, prompt_path, stdout_path, stderr_path, transcript_path, evidence_pruned "
                "FROM runs WHERE task_id = ? ORDER BY started_at DESC",
                (task["id"],),
            )
            keep_ids = set()
            for index, run in enumerate(runs):
                recent = (run["started_at"] or "") >= cutoff
                active = run["status"] in ("running", "waiting")
                if active or recent or index < max(0, int(keep_runs_per_task)):
                    keep_ids.add(run["id"])
            for run in runs:
                if run["id"] in keep_ids:
                    report["runs"]["kept"] += 1
                    continue
                run_dir = self._run_dir(run)
                if run_dir is None:
                    continue
                workspace = run_dir / "workspace"
                if workspace.exists():
                    size = _dir_size(workspace)
                    report["runs"]["pruned_workspaces"].append(
                        {"run_id": run["id"], "task_id": task["id"], "path": str(workspace), "bytes": size}
                    )
                    report["freed_bytes"] += size
                    if not dry_run:
                        shutil.rmtree(workspace, ignore_errors=True)
                for key in ("stdout_path", "stderr_path", "transcript_path"):
                    path = Path(run[key]) if run[key] else None
                    if path is None or not path.is_file():
                        continue
                    before = path.stat().st_size
                    if before <= max_evidence_bytes:
                        continue
                    report["runs"]["truncated"].append(
                        {"run_id": run["id"], "path": str(path), "before": before, "limit": max_evidence_bytes}
                    )
                    if not dry_run:
                        _, after = _truncate_file(path, max_evidence_bytes)
                        report["freed_bytes"] += max(0, before - after)
                if not dry_run and not int(run["evidence_pruned"] or 0):
                    self.db.update("runs", {"id": run["id"]}, {"evidence_pruned": 1})
                    # 指向已删除 workspace 的产物行一并清理（保留其他位置的产物）
                    self.db.execute(
                        "DELETE FROM artifacts WHERE run_id = ? AND abs_path LIKE ?",
                        (run["id"], f"%{os.sep}workspace{os.sep}%"),
                    )

        # 2) 已合并的 Git worktree：先注销注册再删目录，分支与提交永远保留。
        #    未合并的 worktree 属于「未审阅成果」，即使任务很老也绝不清理。
        self._prune_worktrees(tasks, report, worktree_days, dry_run=dry_run)

        # 3) 终态任务的 work/baseline（保留最近 N 天，便于手动同步/回滚）
        terminal = {"success", "failed", "cancelled", "skipped"}
        for task in tasks:
            if task["status"] not in terminal:
                continue
            finished = task["finished_at"] or task["updated_at"] or ""
            if finished >= workspace_cutoff:
                continue
            task_dir = Path(self.config.runtime_dir) / task["project_id"] / "tasks" / task["id"]
            for name in ("work", "baseline"):
                target = task_dir / name
                if not target.exists():
                    continue
                if name == "work":
                    # Git worktree 由步骤 2 的专用策略处理；未合并的现场绝不能
                    # 被这里的 rmtree 直接删掉（那会留下陈旧注册）。
                    stored = loads(task["result"], {}) if task["result"] else {}
                    git_meta = stored.get("git") if isinstance(stored, dict) else None
                    if _looks_like_worktree(
                        task["root_path"], target, check_registry=bool(git_meta)
                    ):
                        continue
                size = _dir_size(target)
                report["tasks"]["removed_workspaces"].append(
                    {"task_id": task["id"], "path": str(target), "bytes": size}
                )
                report["freed_bytes"] += size
                if not dry_run:
                    shutil.rmtree(target, ignore_errors=True)

        # 4) 快照保留
        for project in self.db.query("SELECT id FROM projects"):
            rows = self.db.query(
                "SELECT id, store_path FROM snapshots WHERE project_id = ? ORDER BY created_at DESC",
                (project["id"],),
            )
            for row in rows[max(0, int(keep_snapshots)):]:
                size = _dir_size(row["store_path"])
                report["snapshots"]["removed"].append(
                    {"snapshot_id": row["id"], "path": row["store_path"], "bytes": size}
                )
                report["freed_bytes"] += size
                if not dry_run:
                    shutil.rmtree(row["store_path"], ignore_errors=True)
                    self.db.execute("DELETE FROM snapshots WHERE id = ?", (row["id"],))

        # 5) 孤儿 runtime 目录（任务已被删除）
        known: dict[str, set[str]] = {}
        for task in tasks:
            known.setdefault(task["project_id"], set()).add(task["id"])
        runtime = Path(self.config.runtime_dir)
        if runtime.exists():
            for project_dir in runtime.iterdir():
                if not project_dir.is_dir():
                    continue
                tasks_dir = project_dir / "tasks"
                if not tasks_dir.is_dir():
                    continue
                for task_dir in tasks_dir.iterdir():
                    if not task_dir.is_dir() or task_dir.name in known.get(project_dir.name, set()):
                        continue
                    size = _dir_size(task_dir)
                    report["tasks"]["removed_workspaces"].append(
                        {"task_id": task_dir.name, "path": str(task_dir), "bytes": size, "orphan": True}
                    )
                    report["freed_bytes"] += size
                    if not dry_run:
                        shutil.rmtree(task_dir, ignore_errors=True)

        # 6) blob GC
        if prune_blobs:
            referenced: set[str] = set()
            for row in self.db.query("SELECT before_hash, after_hash FROM file_changes"):
                for key in ("before_hash", "after_hash"):
                    if row[key]:
                        referenced.add(str(row[key]))
            for row in self.db.query("SELECT sha256 FROM artifacts WHERE sha256 IS NOT NULL"):
                if row["sha256"]:
                    referenced.add(str(row["sha256"]))
            blob_root = Path(self.config.data_dir) / "blobs"
            grace = time.time() - max(0.0, float(blob_grace_hours)) * 3600
            if blob_root.exists():
                for bucket in blob_root.iterdir():
                    if not bucket.is_dir():
                        continue
                    for blob in bucket.iterdir():
                        if not blob.is_file() or blob.name in referenced:
                            continue
                        try:
                            if blob.stat().st_mtime > grace:
                                continue
                        except OSError:
                            continue
                        size = blob.stat().st_size
                        report["blobs"]["removed"].append(str(blob))
                        report["blobs"]["freed_bytes"] += size
                        report["freed_bytes"] += size
                        if not dry_run:
                            try:
                                blob.unlink()
                            except OSError:
                                pass

        # 7) 实时事件保留（run_events）
        event_cutoff = _iso_cutoff(14)
        event_row = self.db.query_one(
            "SELECT COUNT(*) AS c FROM run_events WHERE at < ?", (event_cutoff,)
        )
        report["run_events"] = {"would_remove": int(event_row["c"] if event_row else 0)}
        if not dry_run:
            from .run_events import RunEventStore

            report["run_events"] = RunEventStore(self.db).prune(keep_days=14, keep_per_project=50000)

        # 8) 备份保留
        report["backups"] = self.prune_backups(keep_backups, dry_run=dry_run)
        report["freed_bytes"] += int(report["backups"].get("freed_bytes") or 0)

        # 9) 空间回收（apply 时）
        if not dry_run:
            report["vacuum"] = self.vacuum()
            self._event(
                "system.maintenance",
                f"保留清理完成：释放约 {report['freed_bytes']} 字节",
                actor="system",
                data={
                    "runs_pruned": len(report["runs"]["pruned_workspaces"]),
                    "snapshots_removed": len(report["snapshots"]["removed"]),
                    "blobs_removed": len(report["blobs"]["removed"]),
                    "worktrees_removed": len(report["worktrees"]["removed"]),
                },
            )
        return report

    def _prune_worktrees(
        self,
        tasks: list[Any],
        report: dict[str, Any],
        retention_days: float,
        *,
        dry_run: bool,
    ) -> None:
        """注销并删除「已合并且超过保留期」的任务 worktree。

        保守规则（任何一条不满足都保留）：

        * 只处理终态任务（success/failed/skipped/cancelled/blocked/interrupted），
          running/waiting/pending 永不处理；
        * ``finished_at``（缺失时 ``updated_at``）早于 cutoff；
        * ``task.result.git.merge.merged == true``——未合并的 worktree 是未审阅
          成果，绝不清理；
        * 目录必须真实存在，且是 Git linked worktree（``.git`` 文件或
          ``git worktree list`` 中有登记）；
        * 只删工作目录，**任务分支与提交永远保留**（``delete_branch=False``）；
        * ``dry_run=True`` 只报告，不动磁盘。
        """
        from .gitops import branch_for_task, merge_status, remove_worktree

        info: dict[str, Any] = report["worktrees"]
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(0.0, float(retention_days)))
        runtime_root = Path(self.config.runtime_dir)
        for task in tasks:
            task_id = str(task["id"])
            workdir = runtime_root / str(task["project_id"]) / "tasks" / task_id / "work"
            try:
                if not workdir.is_dir():
                    continue
            except OSError:
                continue
            result = loads(task["result"], {})
            git_meta = result.get("git") if isinstance(result, dict) else None
            if not isinstance(git_meta, dict):
                git_meta = {}
            root_value = task["root_path"]
            root = Path(root_value) if root_value else None
            if not _looks_like_worktree(root, workdir, check_registry=bool(git_meta)):
                # copy/shared 的 work/ 不是 worktree，交给 prune_workspaces_days。
                continue
            status = str(task["status"] or "")
            finished = _parse_iso(task["finished_at"] or task["updated_at"])
            if (
                status not in WORKTREE_PRUNE_STATUSES
                or finished is None
                or finished >= cutoff
            ):
                info["kept"] += 1
                continue
            if merge_status(git_meta) != "merged":
                info["skipped_unmerged"] += 1
                continue
            branch = str(git_meta.get("branch") or "") or branch_for_task(
                task_id, str(task["title"] or "")
            )
            entry: dict[str, Any] = {
                "task_id": task_id,
                "path": str(workdir),
                "branch": branch,
            }
            info["removed"].append(entry)
            size = _dir_size(workdir)
            if dry_run:
                report["freed_bytes"] += size
                continue
            if root is None:
                entry["error"] = "项目根目录缺失，无法注销 worktree"
                continue
            try:
                # branch=None / delete_branch=False：只注销并删除工作目录，
                # 任务分支和提交保持可追溯、可重新检出。
                remove_worktree(root, workdir, branch=None, delete_branch=False)
            except Exception as exc:  # noqa: BLE001 - 单个失败不能中断整轮清理
                entry["error"] = f"{type(exc).__name__}: {exc}"
                continue
            if workdir.exists():
                entry["error"] = "worktree 目录未能完全删除（可能被占用）"
                continue
            # remove_worktree 内部是先 `worktree prune` 再 rmtree；若目录在 prune
            # 之后才消失，可能留下陈旧注册，这里补一次注销（失败也不影响主流程）。
            try:
                run_command(["git", "-C", str(root), "worktree", "prune"], timeout=30)
            except Exception:  # noqa: BLE001
                pass
            report["freed_bytes"] += size

    def vacuum(self) -> dict[str, Any]:
        """WAL checkpoint + VACUUM（阻塞式，建议在维护窗口执行）。"""
        before = Path(self.config.db_path).stat().st_size if Path(self.config.db_path).exists() else 0
        with self.db._lock:  # noqa: SLF001 - 运维操作需要独占连接
            self.db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.db.conn.execute("VACUUM")
            self.db.conn.commit()
        after = Path(self.config.db_path).stat().st_size if Path(self.config.db_path).exists() else 0
        return {"before_bytes": before, "after_bytes": after, "freed_bytes": max(0, before - after)}

    # ------------------------------------------------------------------ 校验
    def verify(self, *, full: bool = False) -> dict[str, Any]:
        db_check = self.db.integrity_check(full=full)
        chains: list[dict[str, Any]] = []
        project_ids = [row["id"] for row in self.db.query("SELECT id FROM projects")]
        for project_id in [None, *project_ids]:
            try:
                ok = self.board.verify_chain(project_id) if self.board is not None else True
            except Exception as exc:  # noqa: BLE001 - 校验失败要报告，不要 500
                chains.append({"project_id": project_id, "ok": False, "error": str(exc)})
                continue
            chains.append({"project_id": project_id, "ok": bool(ok)})
        missing_artifacts = self.db.query_one(
            "SELECT COUNT(*) AS c FROM artifacts WHERE abs_path IS NOT NULL"
        )
        return {
            "ok": bool(db_check["ok"]) and all(item["ok"] for item in chains),
            "database": db_check,
            "event_chains": chains,
            "artifacts": int(missing_artifacts["c"]) if missing_artifacts else 0,
            "schema_version": self.db.schema_version,
            "server_time": now_iso(),
        }

    # ------------------------------------------------------------------ 工具
    @staticmethod
    def _run_dir(run) -> Path | None:
        for key in ("prompt_path", "stdout_path", "stderr_path", "transcript_path"):
            value = run[key]
            if value:
                return Path(value).parent
        return None

    def _event(self, kind: str, summary: str, *, actor: str = "system", data: dict[str, Any] | None = None) -> None:
        if self.board is None:
            return
        try:
            self.board.append_event(project_id=None, kind=kind, summary=summary, actor=actor, data=data or {})
        except Exception:  # noqa: BLE001 - 运维动作不应因审计写失败而失败
            pass
