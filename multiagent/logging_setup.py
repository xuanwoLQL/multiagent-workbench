"""结构化日志：JSON Lines + 轮转 + 请求/任务上下文。

企业现场排障需要的不是「print 一堆」，而是能按 ``request_id`` / ``run_id``
把一次调用串起来。这里统一给 ``multiagent`` logger 配置：

* 文件：``<data_dir>/logs/multiagent.log``（10MB × 5，UTF-8，JSON Lines）；
* 控制台：默认只输出 WARNING 及以上（避免污染 CLI 的 JSON 输出）；
* 上下文：``request_id`` / ``project_id`` / ``task_id`` / ``run_id`` / ``agent_id``
  通过 ``contextvars`` 自动带进每条日志，线程/协程安全；
* 幂等：重复调用只替换 handler，不会重复写、不会泄漏文件句柄。
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_CONTEXT: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("multiagent_log_context", default={})
_CONFIGURED_PATH: Path | None = None


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def bind_context(**fields: Any) -> contextvars.Token:
    """绑定日志上下文，返回 token（配合 :func:`reset_context`）。"""
    current = dict(_CONTEXT.get())
    current.update({k: v for k, v in fields.items() if v is not None})
    return _CONTEXT.set(current)


def reset_context(token: contextvars.Token) -> None:
    try:
        _CONTEXT.reset(token)
    except (ValueError, RuntimeError):
        _CONTEXT.set({})


def current_context() -> dict[str, Any]:
    return dict(_CONTEXT.get())


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update(_CONTEXT.get())
        for key in ("request_id", "project_id", "task_id", "run_id", "agent_id", "duration_ms", "status", "method", "path"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(
    data_dir: str | os.PathLike[str],
    *,
    level: str | None = None,
    console: bool | None = None,
) -> Path | None:
    """配置 ``multiagent`` logger；返回日志文件路径（失败返回 None）。"""
    global _CONFIGURED_PATH
    root = logging.getLogger("multiagent")
    root.setLevel(getattr(logging, str(level or os.environ.get("MULTIAGENT_LOG_LEVEL") or "INFO").upper(), logging.INFO))
    root.propagate = False

    for handler in list(root.handlers):
        try:
            handler.close()
        except Exception:  # noqa: BLE001
            pass
        root.removeHandler(handler)

    log_dir = Path(data_dir) / "logs"
    log_path = log_dir / "multiagent.log"
    formatter = JsonFormatter()
    file_handler: logging.Handler | None = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        root.addHandler(file_handler)
        _CONFIGURED_PATH = log_path
    except OSError:
        _CONFIGURED_PATH = None

    show_console = console if console is not None else os.environ.get("MULTIAGENT_LOG_CONSOLE", "0") not in ("0", "false", "off")
    if show_console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(formatter)
        stream.setLevel(logging.WARNING)
        root.addHandler(stream)
    return _CONFIGURED_PATH


def log_path() -> Path | None:
    return _CONFIGURED_PATH
