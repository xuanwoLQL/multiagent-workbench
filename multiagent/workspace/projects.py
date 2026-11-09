"""项目 / 工作区管理（需求 8-12）。

一个工作区 = 一组文件 + 协作上下文（任务、记忆、配置）。支持多项目并存、
从外部目录导入、相互隔离。
"""

from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database
from ..util import ensure_dir, new_id, now_iso, read_json, slugify, write_json


class ProjectNotFound(LookupError):
    pass


class ProjectRootConflict(ValueError):
    """同一真实目录被重复登记为项目时抛出。

    属性契约（供 API 层接线 409 响应）：

    - ``project``: 已存在项目的完整 dict（与 ``ProjectService.get`` 返回同构）。
    - ``root_path``: 冲突目录的规范化绝对路径（见 :func:`normalize_root`）。
    """

    def __init__(self, project: dict[str, Any], root_path: str | Path):
        self.project = project
        self.root_path = normalize_root(root_path)
        name = project.get("name") or project.get("label") or ""
        super().__init__(f"该目录已登记为项目「{name}」")


def normalize_root(path: str | Path) -> str:
    """把项目根路径规范化成可比较的键。

    - ``~`` 展开后解析为绝对路径（不要求目录存在）；
    - Windows 下 ``os.path.normcase`` 统一大小写（``D:\\A`` 与 ``d:\\a`` 相同）；
    - 统一使用正斜杠并去掉尾部分隔符（``D:\\a`` 与 ``D:\\a\\`` 相同）；
    - 空路径返回空字符串，避免把历史脏数据误判为当前目录。
    """
    raw = str(path).strip() if path is not None else ""
    if not raw:
        return ""
    try:
        resolved = Path(raw).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        # 不可访问的网络路径（UNC）等场景退回到不解析的绝对化结果。
        resolved = Path(raw).expanduser()
    normalized = os.path.normcase(str(resolved)).replace("\\", "/")
    stripped = normalized.rstrip("/")
    return stripped or "/"


class ProjectService:
    # 同一进程内把「判重 + 插入」串行化：两个并发导入请求否则会同时查到
    # “不存在”，再各自插入，绕过 409 去重承诺。多进程部署仍建议加唯一索引。
    _create_lock = threading.Lock()

    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ 查询
    def list(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM projects ORDER BY created_at")
        return [dict(r) for r in rows]

    def get(self, identifier: str) -> dict[str, Any]:
        row = self.db.query_one(
            "SELECT * FROM projects WHERE id = ? OR name = ? OR label = ?",
            (identifier, identifier, identifier),
        )
        if not row:
            raise ProjectNotFound(f"项目不存在：{identifier}")
        return dict(row)

    def find(self, identifier: str | None) -> dict[str, Any] | None:
        if not identifier:
            return None
        try:
            return self.get(identifier)
        except ProjectNotFound:
            return None

    def find_by_root(
        self, root: str | Path, *, exclude_id: str | None = None
    ) -> dict[str, Any] | None:
        """按规范化后的真实目录查找已登记项目，忽略大小写与尾部分隔符差异。"""
        key = normalize_root(root)
        if not key:
            return None
        for row in self.db.query("SELECT * FROM projects ORDER BY created_at"):
            project = dict(row)
            if exclude_id and project.get("id") == exclude_id:
                continue
            candidate = normalize_root(project.get("root_path") or "")
            if candidate and candidate == key:
                return project
        return None

    def root(self, project: dict[str, Any] | str) -> Path:
        row = self.get(project) if isinstance(project, str) else project
        return Path(row["root_path"])

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        name: str,
        *,
        label: str = "",
        root_path: str | Path | None = None,
        kind: str = "files",
        project_config: dict[str, Any] | None = None,
        exists_ok: bool = False,
    ) -> dict[str, Any]:
        with self._create_lock:
            if root_path is not None:
                conflict = self.find_by_root(root_path)
                if conflict:
                    raise ProjectRootConflict(conflict, root_path)
            existing = self.find(name)
            if existing:
                if exists_ok:
                    return existing
                raise ValueError(f"项目名已存在：{name}")
            project_id = new_id("proj")
            root = Path(root_path) if root_path else (self.config.workspaces_dir / slugify(name))
            ensure_dir(root)
            now = now_iso()
            self.db.insert(
                "projects",
                {
                    "id": project_id,
                    "name": name,
                    "label": label or name,
                    "root_path": str(root.resolve()),
                    "origin_path": None,
                    "kind": kind,
                    "readonly": 0,
                    "config": _json(project_config or {}),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return self.get(project_id)

    def import_dir(
        self,
        path: str | Path,
        *,
        name: str | None = None,
        label: str = "",
        kind: str = "files",
        exists_ok: bool = False,
    ) -> dict[str, Any]:
        """把一个已有目录导入为工作区，不移动、不重组任何文件（需求 12）。"""
        source = Path(path).expanduser().resolve()
        if not source.exists() or not source.is_dir():
            raise ValueError(f"导入目录不存在或不是目录：{source}")
        project_name = name or source.name
        with self._create_lock:
            existing_name = self.find(project_name)
            if exists_ok and existing_name:
                return existing_name
            existing_root = self.find_by_root(source)
            if existing_root:
                if exists_ok:
                    return existing_root
                raise ProjectRootConflict(existing_root, source)
            if existing_name:
                raise ValueError(f"项目名已存在：{project_name}")
            project_id = new_id("proj")
            now = now_iso()
            self.db.insert(
                "projects",
                {
                    "id": project_id,
                    "name": project_name,
                    "label": label or project_name,
                    "root_path": str(source),
                    "origin_path": str(source),
                    "kind": kind,
                    "readonly": 0,
                    "config": "{}",
                    "created_at": now,
                    "updated_at": now,
                },
            )
            return self.get(project_id)

    # ------------------------------------------------------------------ 维护
    def touch(self, project_id: str) -> None:
        self.db.update("projects", {"id": project_id}, {"updated_at": now_iso()})

    def set_config(self, project_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        row = self.db.query_one("SELECT config FROM projects WHERE id = ?", (project_id,))
        if not row:
            raise ProjectNotFound(project_id)
        current = read_json(row["config"], {}) if isinstance(row["config"], str) else {}
        merged = {**(current or {}), **patch}
        self.db.update("projects", {"id": project_id}, {"config": _json(merged), "updated_at": now_iso()})
        return merged

    def write_meta(self, project: dict[str, Any]) -> None:
        """把项目元信息写到工作区 .multiagent/project.json，便于人工查看。"""
        root = self.root(project)
        meta_dir = ensure_dir(root / ".multiagent")
        write_json(meta_dir / "project.json", {
            "id": project["id"],
            "name": project["name"],
            "label": project.get("label"),
            "created_at": project.get("created_at"),
            "managed_by": "multiagent",
        })

    def delete(self, project_id: str) -> dict[str, Any]:
        """删除项目及其全部协作数据与工作副本（不可恢复）。

        只清理工作台自己管理的数据（运行副本、快照、任务、黑板...）；不删除
        导入来源目录里的原始文件，也不动工作区里的用户文件。
        """
        project = self.get(project_id)
        pid = project["id"]
        shutil.rmtree(self.config.runtime_dir / pid, ignore_errors=True)
        # 若工作区是工作台自己管理的（在 workspaces/ 下），一并清理，避免残留脏文件
        try:
            root = Path(project["root_path"]).resolve()
            managed = self.config.workspaces_dir.resolve()
            if root != managed and managed in root.parents:
                shutil.rmtree(root, ignore_errors=True)
        except OSError:
            pass
        for row in self.db.query("SELECT store_path FROM snapshots WHERE project_id = ?", (pid,)):
            shutil.rmtree(row["store_path"], ignore_errors=True)
        self.db.execute(
            "DELETE FROM board_versions WHERE entry_id IN (SELECT id FROM board_entries WHERE project_id = ?)",
            (pid,),
        )
        self.db.execute(
            "DELETE FROM task_states WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?)",
            (pid,),
        )
        self.db.execute(
            "DELETE FROM task_edges WHERE task_id IN (SELECT id FROM tasks WHERE project_id = ?) "
            "OR depends_on IN (SELECT id FROM tasks WHERE project_id = ?)",
            (pid, pid),
        )
        for table in (
            "tasks", "board_entries", "runs", "handoffs", "artifacts",
            "file_changes", "approvals", "leases", "snapshots", "events", "scheduler_slots",
            "run_events",
        ):
            self.db.execute(f"DELETE FROM {table} WHERE project_id = ?", (pid,))
        self.db.execute("DELETE FROM projects WHERE id = ?", (pid,))
        return project


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
