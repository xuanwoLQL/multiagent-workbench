"""兼容层：``StreamCoalescer`` 已迁到 :mod:`multiagent.stream`。

保留本模块是为了不破坏既有 import 契约（测试与外部脚本大量使用
``from multiagent.api.stream import StreamCoalescer``）。实现只有一份。
"""

from __future__ import annotations

from ..stream import (
    CHUNK_KINDS,
    DEFAULT_FLUSH_INTERVAL_SECONDS,
    DEFAULT_MAX_TEXT_CHARS,
    DEFAULT_MERGE_WINDOW_SECONDS,
    StreamCoalescer,
)

__all__ = [
    "CHUNK_KINDS",
    "DEFAULT_FLUSH_INTERVAL_SECONDS",
    "DEFAULT_MAX_TEXT_CHARS",
    "DEFAULT_MERGE_WINDOW_SECONDS",
    "StreamCoalescer",
]
