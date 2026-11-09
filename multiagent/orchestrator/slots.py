"""跨进程调度槽位（需求 41）。

``_ConcurrencyGate`` 只能约束单进程内的线程；CLI、桌面后端、另一个 ``serve.py``
各自是独立进程，旧实现下三个进程可以同时开满 ``global``，真实并发翻倍。

这里用 SQLite 表做「全局/每项目」槽位：

* 获取：``BEGIN IMMEDIATE`` 内清理过期/失联槽位 → 计数 → 未满则插入；
* 续租：引擎心跳线程定期把本会话槽位的 ``expires_at`` 往后推；
* 释放：任务结束/提交失败/取消时删除；
* 崩溃：会话心跳停止后，槽位在 ``ttl`` 内过期，其他进程可回收。

槽位只是**并发闸门**，不替代任务认领（claim）；两者一起保证「同一任务不重复执行」
与「同时执行数不超限」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import Config
from ..db import Database
from ..util import new_id, now_iso


def _future(seconds: float) -> str:
    moment = datetime.now(timezone.utc) + timedelta(seconds=max(1.0, float(seconds)))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SlotService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ 获取
    def acquire(
        self,
        *,
        project_id: str,
        task_id: str | None = None,
        global_limit: int,
        per_project_limit: int,
        ttl_seconds: float = 1800,
    ) -> str | None:
        """尝试占用一个槽位；返回 slot_id，满了返回 None。"""
        session_id = self.db.session_id
        now = now_iso()
        with self.db.transaction():
            self._cleanup_locked()
            total = self.db.query_one("SELECT COUNT(*) AS c FROM scheduler_slots")
            per = self.db.query_one(
                "SELECT COUNT(*) AS c FROM scheduler_slots WHERE project_id = ?", (project_id,)
            )
            if int(total["c"] if total else 0) >= max(1, int(global_limit)):
                return None
            if int(per["c"] if per else 0) >= max(1, int(per_project_limit)):
                return None
            slot_id = new_id("slot")
            self.db.insert(
                "scheduler_slots",
                {
                    "slot_id": slot_id,
                    "session_id": session_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "acquired_at": now,
                    "expires_at": _future(ttl_seconds),
                },
            )
            return slot_id

    def release(self, slot_id: str | None) -> None:
        if not slot_id:
            return
        self.db.execute("DELETE FROM scheduler_slots WHERE slot_id = ?", (slot_id,))

    def release_session(self) -> int:
        """释放本会话全部槽位（进程干净退出时调用）。"""
        cur = self.db.execute("DELETE FROM scheduler_slots WHERE session_id = ?", (self.db.session_id,))
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ 续租
    def renew(self, ttl_seconds: float = 1800) -> int:
        """续租本会话的全部槽位（心跳线程调用）。"""
        cur = self.db.execute(
            "UPDATE scheduler_slots SET expires_at = ? WHERE session_id = ?",
            (_future(ttl_seconds), self.db.session_id),
        )
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------ 清理
    def cleanup(self, *, stale_seconds: float = 90) -> int:
        with self.db.transaction():
            return self._cleanup_locked(stale_seconds=stale_seconds)

    def _cleanup_locked(self, *, stale_seconds: float = 90) -> int:
        """删除过期槽位与会话心跳已失联的槽位。"""
        now = now_iso()
        removed = self.db.execute("DELETE FROM scheduler_slots WHERE expires_at < ?", (now,)).rowcount or 0
        try:
            live = self.db.live_sessions(stale_seconds)
        except Exception:  # noqa: BLE001 - 心跳表异常时只按过期时间清理
            live = {self.db.session_id}
        rows = self.db.query("SELECT slot_id, session_id FROM scheduler_slots")
        for row in rows:
            if row["session_id"] not in live and row["session_id"] != self.db.session_id:
                self.db.execute("DELETE FROM scheduler_slots WHERE slot_id = ?", (row["slot_id"],))
                removed += 1
        return int(removed)

    # ------------------------------------------------------------------ 观测
    def counts(self) -> dict[str, Any]:
        total = self.db.query_one("SELECT COUNT(*) AS c FROM scheduler_slots")
        by_project = [
            dict(row)
            for row in self.db.query(
                "SELECT project_id, COUNT(*) AS c FROM scheduler_slots GROUP BY project_id"
            )
        ]
        return {
            "total": int(total["c"] if total else 0),
            "global_limit": int(self.config.policy_get("concurrency.global", 6)),
            "per_project_limit": int(self.config.policy_get("concurrency.per_project", 3)),
            "by_project": by_project,
        }
