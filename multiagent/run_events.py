"""Agent 实时事件的持久化与回放（跨进程可观测性）。

背景
----
CLI 调度、API 后台调度、桌面壳各自是独立进程。原来的 ``runner.event_sink`` 只把
ACP 事件推给**本进程**的 WebSocket 总线：用 CLI 跑任务、另开浏览器看板时，「实时」
页永远没有内容；进程崩溃后也拿不到运行中的事件，只能等最终 transcript。

本模块把经过 ``StreamCoalescer`` 合并后的事件写入 ``run_events`` 表：

* 任何进程都能按 ``project_id`` / ``run_id`` / ``after_id`` 读取；
* 写入后同步通知本进程订阅者（API 的 EventHub），保留低延迟；
* 单条 payload 有上限，避免一条超大工具输出把库撑爆；
* 只存「事件流」，最终权威记录仍是 ``runs/*/transcript.json``。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

from .db import Database
from .stream import StreamCoalescer
from .util import now_iso

#: 单条事件 payload 上限（字符）。超过则截断大字段，保留事件骨架。
MAX_PAYLOAD_CHARS = 64 * 1024
#: 文本字段截断长度。
MAX_TEXT_CHARS = 8 * 1024
#: 大对象字段截断长度。
MAX_OBJECT_CHARS = 16 * 1024


class RunEventStore:
    def __init__(self, db: Database, *, merge_window: float = 0.18):
        self.db = db
        self.coalescer = StreamCoalescer(self._insert, merge_window=merge_window)
        self._subs: dict[int, Callable[[dict[str, Any]], None]] = {}
        self._sub_lock = threading.Lock()
        self._next_sub = 1

    # ------------------------------------------------------------------ 写入
    def feed(self, event: dict[str, Any]) -> None:
        self.coalescer.feed(event)

    def flush(self) -> int:
        return self.coalescer.flush()

    def close(self) -> None:
        self.coalescer.close()

    def _insert(self, event: dict[str, Any]) -> None:
        payload = _cap_payload(dict(event))
        try:
            cursor = self.db.execute(
                """
                INSERT INTO run_events
                  (project_id, task_id, run_id, agent_id, kind, source_type, at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(event.get("project_id") or ""),
                    event.get("task_id"),
                    event.get("run_id"),
                    event.get("agent_id"),
                    str(event.get("kind") or event.get("source_type") or ""),
                    event.get("source_type"),
                    str(event.get("at") or now_iso()),
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
        except Exception:  # noqa: BLE001 - 事件持久化失败不能拖垮 Agent 调用
            return
        event_id = int(cursor.lastrowid or 0)
        delivered = dict(payload)
        delivered["event_id"] = event_id
        with self._sub_lock:
            subscribers = list(self._subs.values())
        for callback in subscribers:
            try:
                callback(delivered)
            except Exception:  # noqa: BLE001 - 单个订阅者失败不影响其它订阅者
                continue

    # ------------------------------------------------------------------ 订阅
    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> int:
        with self._sub_lock:
            token = self._next_sub
            self._next_sub += 1
            self._subs[token] = callback
            return token

    def unsubscribe(self, token: int) -> None:
        with self._sub_lock:
            self._subs.pop(int(token), None)

    # ------------------------------------------------------------------ 读取
    def list(
        self,
        project_id: str | None = None,
        *,
        after_id: int = 0,
        limit: int = 200,
        run_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["id > ?"]
        params: list[Any] = [int(after_id or 0)]
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        params.append(max(1, min(int(limit or 200), 1000)))
        rows = self.db.query(
            f"SELECT * FROM run_events WHERE {' AND '.join(clauses)} ORDER BY id ASC LIMIT ?",
            params,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, ValueError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {"value": payload}
            payload["event_id"] = int(row["id"])
            payload.setdefault("project_id", row["project_id"])
            payload.setdefault("task_id", row["task_id"])
            payload.setdefault("run_id", row["run_id"])
            payload.setdefault("agent_id", row["agent_id"])
            payload.setdefault("kind", row["kind"])
            payload.setdefault("source_type", row["source_type"])
            payload.setdefault("at", row["at"])
            result.append(payload)
        return result

    def latest_id(self, project_id: str | None = None) -> int:
        if project_id:
            row = self.db.query_one(
                "SELECT COALESCE(MAX(id), 0) AS m FROM run_events WHERE project_id = ?", (project_id,)
            )
        else:
            row = self.db.query_one("SELECT COALESCE(MAX(id), 0) AS m FROM run_events")
        return int(row["m"]) if row else 0

    def count(self, project_id: str | None = None) -> int:
        if project_id:
            row = self.db.query_one(
                "SELECT COUNT(*) AS c FROM run_events WHERE project_id = ?", (project_id,)
            )
        else:
            row = self.db.query_one("SELECT COUNT(*) AS c FROM run_events")
        return int(row["c"]) if row else 0

    # ------------------------------------------------------------------ 清理
    def prune(self, *, keep_days: float = 14.0, keep_per_project: int = 50000) -> dict[str, int]:
        """保留策略：先按时间删，再按项目保留最近 N 条。"""
        from datetime import datetime, timedelta, timezone

        cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0.0, float(keep_days)))).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3] + "Z"
        old = self.db.execute("DELETE FROM run_events WHERE at < ?", (cutoff,))
        removed = int(old.rowcount or 0)
        projects = [row["project_id"] for row in self.db.query("SELECT DISTINCT project_id FROM run_events")]
        for project_id in projects:
            row = self.db.query_one(
                "SELECT id FROM run_events WHERE project_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
                (project_id, max(1, int(keep_per_project))),
            )
            if not row:
                continue
            cur = self.db.execute(
                "DELETE FROM run_events WHERE project_id = ? AND id <= ?", (project_id, row["id"])
            )
            removed += int(cur.rowcount or 0)
        return {"removed": removed}


def _cap_payload(event: dict[str, Any]) -> dict[str, Any]:
    text = event.get("text")
    if isinstance(text, str) and len(text) > MAX_TEXT_CHARS:
        event["text"] = text[:MAX_TEXT_CHARS] + "…[截断]"
        event["truncated"] = True
    for key in ("tool_input", "tool_output", "tool_content"):
        value = event.get(key)
        if value is None:
            continue
        try:
            rendered = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            rendered = str(value)
        if len(rendered) > MAX_OBJECT_CHARS:
            event[key] = {"truncated": True, "preview": rendered[:MAX_OBJECT_CHARS]}
            event["truncated"] = True
    try:
        rendered = json.dumps(event, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(event)
    if len(rendered) > MAX_PAYLOAD_CHARS:
        # 极端情况：整体截断，保证至少保留定位字段
        event = {
            "project_id": event.get("project_id"),
            "task_id": event.get("task_id"),
            "run_id": event.get("run_id"),
            "agent_id": event.get("agent_id"),
            "kind": event.get("kind"),
            "source_type": event.get("source_type"),
            "at": event.get("at"),
            "text": rendered[:MAX_TEXT_CHARS] + "…[整体截断]",
            "truncated": True,
        }
    return event
