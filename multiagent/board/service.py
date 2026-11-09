"""共享黑板：类型化条目、版本化、审核闸门、检索、追加事件与哈希链。

作用域（需求 26）：
    project  项目级长期记忆（默认需审核后才作为权威上下文）
    task     任务级临时上下文
    agent    Agent 私有草稿

条目类型（需求 20）：
    brief      任务简报
    artifact   中间产物
    decision   决策记录
    handoff    交接说明
    todo       待办
    issue      已知问题
    reference  参考资料
    note       一般笔记
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Iterable

from ..config import Config
from ..db import Database
from ..util import dumps, loads, new_id, now_iso, sha256_text


class MemoryScope(str, Enum):
    PROJECT = "project"
    TASK = "task"
    AGENT = "agent"


class EntryType(str, Enum):
    BRIEF = "brief"
    ARTIFACT = "artifact"
    DECISION = "decision"
    HANDOFF = "handoff"
    TODO = "todo"
    ISSUE = "issue"
    REFERENCE = "reference"
    NOTE = "note"


#: 能进入「长期记忆」的条目类型：其余（note/todo/handoff/artifact）是临时上下文
LONG_TERM_TYPES = {EntryType.DECISION.value, EntryType.REFERENCE.value, EntryType.ISSUE.value, EntryType.BRIEF.value}


class BoardService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ 事件
    def append_event(
        self,
        *,
        project_id: str | None,
        kind: str,
        summary: str = "",
        actor: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        entry_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # 「读上一条 hash → 写本条」必须**跨进程**原子：进程内锁不够，
        # 必须用 BEGIN IMMEDIATE 拿写锁后再读。否则两个进程会写出同一个
        # prev_hash 的分叉，verify_chain 直接失守（需求 22/49）。
        with self.db.transaction():
            at = now_iso()
            payload = _event_payload(
                project_id=project_id,
                kind=kind,
                summary=summary,
                actor=actor,
                task_id=task_id,
                run_id=run_id,
                entry_id=entry_id,
                data=data or {},
                at=at,
            )
            prev = self.db.query_one(
                "SELECT hash FROM events WHERE project_id IS ? ORDER BY id DESC LIMIT 1",
                (project_id,),
            )
            prev_hash = prev["hash"] if prev else ""
            digest = sha256_text(prev_hash + payload)
            cur = self.db.execute(
                """
                INSERT INTO events (project_id, at, kind, actor, task_id, run_id, entry_id, summary, data, prev_hash, hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (project_id, at, kind, actor, task_id, run_id, entry_id, summary, dumps(data or {}), prev_hash, digest),
            )
        return {
            "id": int(cur.lastrowid),
            "project_id": project_id,
            "at": at,
            "kind": kind,
            "summary": summary,
            "prev_hash": prev_hash,
            "hash": digest,
        }


    def read_events(
        self, project_id: str | None = None, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        if project_id:
            rows = self.db.query(
                "SELECT * FROM events WHERE project_id = ? AND id > ? ORDER BY id LIMIT ?",
                (project_id, after_id, limit),
            )
        else:
            rows = self.db.query("SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit))
        return [_event(r) for r in rows]

    def verify_chain(self, project_id: str | None = None) -> bool:
        """校验事件哈希链。

        每条链按 ``project_id`` 独立（``None`` 是全局运维链）。``project_id=None``
        表示「校验所有链」，不是把所有项目的事件混成一条链——两者在数据库里
        本来就是交错写入的，混起来必然误报。
        """
        rows = self.read_events(project_id, 0, 5_000_000)
        chains: dict[Any, list[dict[str, Any]]] = {}
        for row in rows:
            chains.setdefault(row["project_id"], []).append(row)
        for _key, items in chains.items():
            prev = ""
            for row in items:
                payload = _event_payload(
                    project_id=row["project_id"],
                    kind=row["kind"],
                    summary=row["summary"],
                    actor=row["actor"],
                    task_id=row["task_id"],
                    run_id=row["run_id"],
                    entry_id=row["entry_id"],
                    data=row["data"],
                    at=row["at"],
                )
                if sha256_text(prev + payload) != row["hash"]:
                    return False
                prev = row["hash"]
        return True

    # ------------------------------------------------------------------ 写入
    def post(
        self,
        project_id: str,
        *,
        type: str = "note",
        title: str = "",
        body: str = "",
        scope: str = "task",
        task_id: str | None = None,
        agent_id: str | None = None,
        run_id: str | None = None,
        tags: Iterable[str] | None = None,
        status: str | None = None,
        pinned: bool = False,
        actor: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        entry_id = new_id("bb")
        now = now_iso()
        if status is None:
            status = self._default_status(scope, type)
        with self.db.transaction():
            self.db.insert(
                "board_entries",
                {
                    "id": entry_id,
                    "project_id": project_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "run_id": run_id,
                    "scope": scope,
                    "type": type,
                    "title": title,
                    "body": body,
                    "tags": dumps(list(tags or [])),
                    "status": status,
                    "pinned": 1 if pinned else 0,
                    "current_version": 1,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            self._add_version(entry_id, 1, title, body, author=actor or agent_id, task_id=task_id, run_id=run_id, note=note)
            self.append_event(
                project_id=project_id,
                kind=f"board.post.{type}",
                summary=(title or body[:60]).strip(),
                actor=actor or agent_id,
                task_id=task_id,
                run_id=run_id,
                entry_id=entry_id,
                data={"scope": scope, "status": status, "type": type},
            )
        return self.get(entry_id)

    def _default_status(self, scope: str, type: str) -> str:
        """审核闸门（需求 23）：项目级条目默认待审。

        scope=project 就是「长期记忆候选区」——不论类型，默认都先 pending；
        只有确实不需要人工过目的（policy 配成 memory_requires_approval=false）
        才直接 approved。
        """
        gate = self.config.policy_get("review_gate", {}) or {}
        requires = bool(gate.get("memory_requires_approval", True))
        if scope == MemoryScope.PROJECT.value and requires:
            return "pending"
        return str(gate.get("default_entry_status", "approved"))

    def edit(
        self,
        entry_id: str,
        *,
        title: str | None = None,
        body: str | None = None,
        tags: Iterable[str] | None = None,
        author: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        """追加一个新版本。

        「读 current_version → 写新版本」必须原子：并发编辑否则会丢版本
        （多个作者都算出同一个 version，后写覆盖先写），需求 19/21 的
        「谁/何时/哪一版」就无法追溯。
        """
        with self.db.transaction():
            entry = self.get(entry_id)
            version = int(entry["current_version"]) + 1
            new_title = entry["title"] if title is None else title
            new_body = entry["body"] if body is None else body
            values: dict[str, Any] = {
                "title": new_title,
                "body": new_body,
                "current_version": version,
                "updated_at": now_iso(),
            }
            if tags is not None:
                values["tags"] = dumps(list(tags))
            # 已审核进长期记忆的条目被改动后，新版本重新过闸门（需求 23）
            if (
                entry.get("scope") == MemoryScope.PROJECT.value
                and str(entry.get("status")) == "approved"
                and bool((self.config.policy_get("review_gate", {}) or {}).get("memory_requires_approval", True))
            ):
                values["status"] = "pending"
                values["reviewed_by"] = None
                values["reviewed_at"] = None
            self.db.update("board_entries", {"id": entry_id}, values)
            self._add_version(
                entry_id,
                version,
                new_title,
                new_body,
                author=author,
                task_id=entry["task_id"],
                run_id=entry["run_id"],
                note=note,
            )
            self.append_event(
                project_id=entry["project_id"],
                kind="board.edit",
                summary=f"编辑条目 {entry_id} → v{version}",
                actor=author,
                task_id=entry["task_id"],
                run_id=entry["run_id"],
                entry_id=entry_id,
                data={"version": version, "note": note},
            )
        return self.get(entry_id)


    def review(self, entry_id: str, *, approved: bool, reviewer: str, note: str | None = None) -> dict[str, Any]:
        entry = self.get(entry_id)
        status = "approved" if approved else "rejected"
        with self.db.transaction():
            self.db.update(
                "board_entries",
                {"id": entry_id},
                {"status": status, "reviewed_by": reviewer, "reviewed_at": now_iso(), "updated_at": now_iso()},
            )
            self.append_event(
                project_id=entry["project_id"],
                kind="board.review",
                summary=f"审核条目 {entry_id} → {status}",
                actor=reviewer,
                task_id=entry["task_id"],
                run_id=entry["run_id"],
                entry_id=entry_id,
                data={"status": status, "note": note},
            )
        return self.get(entry_id)

    def supersede_task_issues(self, task_id: str, reason: str) -> int:
        """把同一任务旧交接里的「遗留问题」标记为已过时（rejected）。

        任务重试成功后，旧 run 的 open_issues 已经不再成立；如果继续以 approved
        状态注入后续任务，会变成误导性的「不要重复尝试」清单（需求 23/25）。
        最新一次交接随后会把自己的 issues 重新 post 为 approved。
        """
        rows = self.db.query(
            "SELECT id FROM board_entries WHERE task_id = ? AND type = 'issue' AND status = 'approved'",
            (task_id,),
        )
        for row in rows:
            self.review(row["id"], approved=False, reviewer="system", note=reason)
        return len(rows)

    def _add_version(self, entry_id, version, title, body, *, author, task_id, run_id, note) -> None:
        self.db.insert(
            "board_versions",
            {
                "entry_id": entry_id,
                "version": version,
                "title": title,
                "body": body,
                "author": author,
                "task_id": task_id,
                "run_id": run_id,
                "note": note,
                "at": now_iso(),
            },
        )

    # ------------------------------------------------------------------ 读取
    def get(self, entry_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM board_entries WHERE id = ?", (entry_id,))
        if not row:
            raise KeyError(f"黑板条目不存在：{entry_id}")
        return _entry(row)

    def versions(self, entry_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM board_versions WHERE entry_id = ? ORDER BY version", (entry_id,)
        )
        return [dict(r) for r in rows]

    def delete(self, entry_id: str) -> dict[str, Any]:
        """删除一条黑板条目及其全部历史版本。"""
        entry = self.get(entry_id)
        with self.db.transaction():
            self.db.execute("DELETE FROM board_versions WHERE entry_id = ?", (entry_id,))
            self.db.execute("DELETE FROM board_entries WHERE id = ?", (entry_id,))
        return entry

    def list(
        self,
        project_id: str,
        *,
        type: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        for column, value in (("type", type), ("scope", scope), ("status", status), ("task_id", task_id), ("agent_id", agent_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM board_entries WHERE {' AND '.join(clauses)} ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            params,
        )
        return [_entry(r) for r in rows]

    def search(
        self,
        project_id: str,
        query: str = "",
        *,
        task_id: str | None = None,
        agent_id: str | None = None,
        type: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """按关键词 / 任务 / Agent / 时间 / 类型检索（需求 24）。"""
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if query:
            like = f"%{query}%"
            clauses.append("(title LIKE ? OR body LIKE ? OR tags LIKE ?)")
            params.extend([like, like, like])
        for column, value in (("task_id", task_id), ("agent_id", agent_id), ("type", type)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since:
            clauses.append("updated_at >= ?")
            params.append(since)
        if until:
            clauses.append("updated_at <= ?")
            params.append(until)
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM board_entries WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
            params,
        )
        return [_entry(r) for r in rows]

    def search_events(
        self, project_id: str | None = None, query: str = "", *, limit: int = 100
    ) -> list[dict[str, Any]]:
        like = f"%{query}%"
        if project_id:
            rows = self.db.query(
                "SELECT * FROM events WHERE project_id = ? AND (summary LIKE ? OR kind LIKE ?) ORDER BY id DESC LIMIT ?",
                (project_id, like, like, limit),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM events WHERE (summary LIKE ? OR kind LIKE ?) ORDER BY id DESC LIMIT ?",
                (like, like, limit),
            )
        return [_event(r) for r in rows]

    # ------------------------------------------------------------------ 记忆分层
    def long_term_memory(self, project_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """长期记忆 = 项目作用域 + 已审核 + 属于长期类型的条目（需求 23/26）。"""
        long_term = sorted(LONG_TERM_TYPES)
        placeholders = ",".join("?" for _ in long_term)
        rows = self.db.query(
            f"""
            SELECT * FROM board_entries
            WHERE project_id = ? AND scope = 'project' AND status = 'approved'
              AND type IN ({placeholders})
            ORDER BY pinned DESC, updated_at DESC LIMIT ?
            """,
            [project_id, *long_term, limit],
        )
        return [_entry(r) for r in rows]

    def pending_reviews(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM board_entries WHERE project_id = ? AND status = 'pending' ORDER BY created_at DESC",
            (project_id,),
        )
        return [_entry(r) for r in rows]

    def counts(self, project_id: str) -> dict[str, int]:
        rows = self.db.query(
            "SELECT type, COUNT(*) AS c FROM board_entries WHERE project_id = ? GROUP BY type", (project_id,)
        )
        result = {row["type"]: row["c"] for row in rows}
        result["total"] = sum(result.values())
        return result


def _entry(row) -> dict[str, Any]:
    data = dict(row)
    data["tags"] = loads(data.get("tags"), []) or []
    data["pinned"] = bool(data.get("pinned"))
    return data


def _event_payload(
    *,
    project_id: str | None,
    kind: str,
    summary: str,
    actor: str | None,
    task_id: str | None,
    run_id: str | None,
    entry_id: str | None,
    data: Any,
    at: str,
) -> str:
    """事件哈希链的规范化载荷（写入与校验必须共用同一函数，避免字段漂移）。"""
    if isinstance(data, str):
        data = loads(data, {})
    return dumps(
        {
            "project_id": project_id,
            "kind": kind,
            "summary": summary,
            "actor": actor,
            "task_id": task_id,
            "run_id": run_id,
            "entry_id": entry_id,
            "data": data if data is not None else {},
            "at": at,
        }
    )


def _event(row) -> dict[str, Any]:
    data = dict(row)
    data["data"] = loads(data.get("data"), {}) or {}
    return data
