"""版本快照与回退（需求 11 / 17）。

快照 = 工作区全量副本 + 哈希清单，可回退、可对比。落盘在
``<data>/snapshots/<snapshot_id>/``。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database
from ..util import dumps, ensure_dir, new_id, now_iso, write_json
from .changes import (
    DEFAULT_EXCLUDES,
    copy_tree,
    is_excluded,
    prune_empty_dirs as _prune_empty_dirs,
    snapshot_tree,
    walk_files,
)


class SnapshotService:
    def __init__(self, db: Database, config: Config, board=None):
        self.db = db
        self.config = config
        self.board = board

    # ------------------------------------------------------------------ 创建
    def create(self, project_id: str, root: str | Path, label: str = "", kind: str = "full") -> dict[str, Any]:
        snapshot_id = new_id("snap")
        store = ensure_dir(self.config.snapshots_dir / snapshot_id)
        files_dir = ensure_dir(store / "files")
        count = copy_tree(root, files_dir, config=self.config)
        manifest = snapshot_tree(root, config=self.config)
        total = sum(int(v.get("size") or 0) for v in manifest.values())
        write_json(store / "manifest.json", manifest)
        write_json(
            store / "snapshot.json",
            {
                "id": snapshot_id,
                "project_id": project_id,
                "label": label,
                "root": str(root),
                "file_count": count,
                "created_at": now_iso(),
            },
        )
        self.db.insert(
            "snapshots",
            {
                "id": snapshot_id,
                "project_id": project_id,
                "label": label,
                "kind": kind,
                "store_path": str(store),
                "manifest": dumps(manifest),
                "parent_id": None,
                "file_count": count,
                "bytes": total,
                "created_at": now_iso(),
            },
        )
        return self.get(snapshot_id)

    # ------------------------------------------------------------------ 查询
    def get(self, snapshot_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,))
        if not row:
            raise KeyError(f"快照不存在：{snapshot_id}")
        data = dict(row)
        data["manifest_obj"] = _manifest(data)
        return data

    def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id:
            rows = self.db.query("SELECT * FROM snapshots WHERE project_id = ? ORDER BY created_at DESC", (project_id,))
        else:
            rows = self.db.query("SELECT * FROM snapshots ORDER BY created_at DESC")
        return [dict(r) for r in rows]

    def latest(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM snapshots WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (project_id,)
        )
        return dict(row) if row else None

    def prune(self, project_id: str, keep: int = 20) -> list[str]:
        rows = self.db.query(
            "SELECT id, store_path FROM snapshots WHERE project_id = ? ORDER BY created_at DESC", (project_id,)
        )
        removed: list[str] = []
        for row in rows[keep:]:
            shutil.rmtree(row["store_path"], ignore_errors=True)
            self.db.execute("DELETE FROM snapshots WHERE id = ?", (row["id"],))
            removed.append(row["id"])
        return removed

    # ------------------------------------------------------------------ 回退
    def restore(
        self,
        snapshot_id: str,
        root: str | Path,
        *,
        clean: bool = True,
        auto_snapshot: bool = True,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        snap = self.get(snapshot_id)
        root_path = Path(root)
        pre_id = None
        if auto_snapshot and project_id:
            pre_id = self.create(project_id, root_path, label=f"回退前自动快照({snapshot_id})", kind="auto")["id"]
        files_dir = Path(snap["store_path"]) / "files"
        manifest = snap["manifest_obj"]
        if clean:
            for rel in walk_files(root_path, config=self.config):
                if rel not in manifest:
                    try:
                        (root_path / rel).unlink()
                    except OSError:
                        pass
            # 清掉改动期间新建的目录骨架（快照里没有这些目录）
            keep_dirs = {
                "/".join(rel.split("/")[: i + 1])
                for rel in manifest
                for i in range(len(rel.split("/")) - 1)
            }
            _prune_empty_dirs(root_path, keep=keep_dirs)
        copy_tree(files_dir, root_path, config=self.config)
        if self.board is not None:
            self.board.append_event(
                project_id=snap["project_id"],
                kind="snapshot.restore",
                summary=f"回退到快照 {snapshot_id}",
                actor="human",
                data={"snapshot_id": snapshot_id, "pre_snapshot": pre_id, "files": snap["file_count"]},
            )
        return {"snapshot_id": snapshot_id, "pre_snapshot": pre_id, "files": snap["file_count"]}

    # ------------------------------------------------------------------ 对比
    def diff(self, snapshot_a: str, snapshot_b: str) -> dict[str, Any]:
        a = self.get(snapshot_a)
        b = self.get(snapshot_b)
        return _diff_manifests(a["manifest_obj"], b["manifest_obj"], snapshot_a, snapshot_b)

    def diff_current(self, snapshot_id: str, root: str | Path) -> dict[str, Any]:
        snap = self.get(snapshot_id)
        current = snapshot_tree(root, config=self.config)
        return _diff_manifests(snap["manifest_obj"], current, snapshot_id, "<current>")


def _manifest(row: dict[str, Any]) -> dict[str, Any]:
    from ..util import loads

    value = loads(row.get("manifest"), {})
    return value if isinstance(value, dict) else {}


def _diff_manifests(a: dict[str, Any], b: dict[str, Any], name_a: str, name_b: str) -> dict[str, Any]:
    added = sorted(set(b) - set(a))
    removed = sorted(set(a) - set(b))
    modified = sorted(
        rel for rel in set(a) & set(b) if a[rel].get("sha256") != b[rel].get("sha256")
    )
    return {
        "from": name_a,
        "to": name_b,
        "added": added,
        "removed": removed,
        "modified": modified,
        "counts": {"added": len(added), "removed": len(removed), "modified": len(modified)},
    }
