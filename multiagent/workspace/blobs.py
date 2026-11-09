"""内容寻址的 blob 仓库：保存改动前后的文件内容，用于回滚与对比（需求 36 / 50）。"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..util import ensure_dir, sha256_bytes, sha256_file

MAX_BLOB_BYTES = 5 * 1024 * 1024  # 超过 5MB 不做快照内容保存（只记哈希）


class BlobStore:
    def __init__(self, root: str | Path):
        self.root = ensure_dir(Path(root) / "blobs")

    def path_for(self, digest: str) -> Path:
        return self.root / digest[:2] / digest

    def put_bytes(self, data: bytes) -> str | None:
        if len(data) > MAX_BLOB_BYTES:
            return None
        digest = sha256_bytes(data)
        target = self.path_for(digest)
        if not target.exists():
            ensure_dir(target.parent)
            tmp = target.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(target)
        return digest

    def put_file(self, path: str | Path) -> str | None:
        p = Path(path)
        try:
            size = p.stat().st_size
        except OSError:
            return None
        if size > MAX_BLOB_BYTES:
            return None
        digest = sha256_file(p)
        target = self.path_for(digest)
        if not target.exists():
            ensure_dir(target.parent)
            shutil.copy2(p, target)
        return digest

    def exists(self, digest: str | None) -> bool:
        return bool(digest) and self.path_for(digest).exists()

    def read_bytes(self, digest: str) -> bytes | None:
        target = self.path_for(digest)
        if target.exists():
            return target.read_bytes()
        return None

    def restore_to(self, digest: str, dest: str | Path) -> bool:
        source = self.path_for(digest)
        if not source.exists():
            return False
        dest_path = Path(dest)
        ensure_dir(dest_path.parent)
        shutil.copy2(source, dest_path)
        return True
