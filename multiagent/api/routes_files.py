"""Read-only project file browsing API (used by the v2 workspace UI)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Query

from ..workspace.changes import (
    PathEscapeError,
    effective_excludes,
    is_excluded,
    resolve_workspace_path,
    walk_files,
)

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TREE_ENTRIES = 2000
MAX_SEARCH_MATCHES = 200
MAX_SEARCH_FILES = 1500
MAX_SEARCH_SECONDS = 8.0


def _root(ctx, pid: str) -> Path:
    project = ctx.project(pid)
    root = Path(project["root_path"])
    if not root.is_dir():
        raise HTTPException(404, "工作区目录不存在")
    return root


def _entry(root: Path, path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        stat = None
    rel = path.relative_to(root).as_posix()
    return {
        "path": rel,
        "name": path.name,
        "dir": path.is_dir(),
        "size": int(stat.st_size) if stat and path.is_file() else 0,
        "mtime": float(stat.st_mtime) if stat else 0.0,
    }


def register_files_routes(api, ctx) -> None:
    @api.get("/api/projects/{pid}/files")
    def list_files(pid: str, path: str = Query("")) -> dict[str, Any]:
        root = _root(ctx, pid)
        excludes = effective_excludes(root, ctx.app.config)
        rel = (path or "").replace("\\", "/").strip("/")
        try:
            target = resolve_workspace_path(root, rel) if rel else root
        except PathEscapeError as exc:
            raise HTTPException(400, "路径越界") from exc
        if not target.is_dir():
            raise HTTPException(404, "目录不存在")
        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        except OSError as exc:
            raise HTTPException(400, f"无法读取目录：{exc}") from exc
        for child in children:
            child_rel = child.relative_to(root).as_posix()
            if is_excluded(child_rel, excludes) or (
                child.is_dir() and is_excluded(child_rel + "/", excludes)
            ):
                continue
            if len(entries) >= MAX_TREE_ENTRIES:
                truncated = True
                break
            entries.append(_entry(root, child))
        return {"root": rel, "entries": entries, "truncated": truncated}

    @api.get("/api/projects/{pid}/file")
    def read_file(pid: str, path: str = Query(...)) -> dict[str, Any]:
        root = _root(ctx, pid)
        rel = path.replace("\\", "/").strip("/")
        try:
            target = resolve_workspace_path(root, rel)
        except PathEscapeError as exc:
            raise HTTPException(400, "路径越界") from exc
        if not target.is_file():
            raise HTTPException(404, "文件不存在")
        size = target.stat().st_size
        with target.open("rb") as handle:
            head = handle.read(min(size, 8192))
        if b"\x00" in head:
            return {"path": rel, "binary": True, "size": size}
        with target.open("rb") as handle:
            data = handle.read(min(size, MAX_FILE_BYTES))
        return {
            "path": rel,
            "binary": False,
            "size": size,
            "truncated": size > MAX_FILE_BYTES,
            "content": data.decode("utf-8", "replace"),
        }

    @api.get("/api/projects/{pid}/search")
    def search_files(pid: str, q: str = Query(..., min_length=1), limit: int = 100) -> dict[str, Any]:
        root = _root(ctx, pid)
        needle = q.strip()
        if not needle:
            return {"matches": [], "truncated": False}
        limit = max(1, min(int(limit or 100), MAX_SEARCH_MATCHES))
        excludes = effective_excludes(root, ctx.app.config)
        matches: list[dict[str, Any]] = []
        truncated = False
        started = time.monotonic()
        scanned = 0
        for rel in walk_files(root, excludes, config=ctx.app.config):
            if time.monotonic() - started > MAX_SEARCH_SECONDS or scanned >= MAX_SEARCH_FILES:
                truncated = True
                break
            path = root / rel
            try:
                if path.stat().st_size > 512 * 1024:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            scanned += 1
            if needle not in text:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line:
                    matches.append({"path": rel, "line": number, "text": line[:400]})
                    if len(matches) >= limit:
                        truncated = True
                        break
            if len(matches) >= limit:
                break
        return {"matches": matches, "truncated": truncated, "scanned": scanned}
