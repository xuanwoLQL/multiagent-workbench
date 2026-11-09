"""可观测与审计：调用记录、汇总、时间线导出、同题多 Agent 产出对比（需求 46-50）。"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from .config import Config
from .db import Database
from .util import dumps, loads, read_text


class AuditService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ------------------------------------------------------------------ 调用
    def runs(
        self,
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("project_id", project_id), ("task_id", task_id), ("agent_id", agent_id)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM runs {where} ORDER BY started_at DESC LIMIT ?", params
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 汇总
    def stats(self, project_id: str | None = None) -> dict[str, Any]:
        """成本/耗时汇总（需求 46-47）。

        注意：过滤条件与绑定参数必须**一一对应**——``project_id`` 为空时既不能
        保留 WHERE 也不能再传参数，否则 sqlite 会因参数个数不匹配直接报错。
        """
        has_project = bool(project_id)
        where = "WHERE project_id = ?" if has_project else ""
        params: list[Any] = [project_id] if has_project else []
        by_agent = [
            dict(r)
            for r in self.db.query(
                f"""
                SELECT agent_id,
                       COUNT(*) AS runs,
                       SUM(duration_ms) AS duration_ms,
                       SUM(cost_tokens) AS tokens,
                       SUM(cost_amount) AS cost,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
                FROM runs {where} GROUP BY agent_id ORDER BY runs DESC
                """,
                params,
            )
        ]
        by_task = [
            dict(r)
            for r in self.db.query(
                f"""
                SELECT t.id AS task_id, t.title, t.status,
                       COUNT(r.id) AS runs,
                       COALESCE(SUM(r.duration_ms),0) AS duration_ms,
                       COALESCE(SUM(r.cost_tokens),0) AS tokens
                FROM tasks t LEFT JOIN runs r ON r.task_id = t.id
                {('WHERE t.project_id = ?' if has_project else '')}
                GROUP BY t.id ORDER BY duration_ms DESC LIMIT 100
                """,
                [project_id] if has_project else [],
            )
        ]
        totals = self.db.query_one(
            f"""
            SELECT COUNT(*) AS runs, COALESCE(SUM(duration_ms),0) AS duration_ms,
                   COALESCE(SUM(cost_tokens),0) AS tokens, COALESCE(SUM(cost_amount),0) AS cost
            FROM runs {where}
            """,
            params,
        )
        return {
            "project_id": project_id or None,
            "totals": dict(totals) if totals else {},
            "by_agent": by_agent,
            "by_task": by_task,
            "quota": self._quota_summary(),
        }

    def _quota_summary(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM agent_usage ORDER BY agent_id")
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ 时间线
    def timeline(self, project_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        events = self.db.query(
            "SELECT * FROM events WHERE project_id = ? ORDER BY id LIMIT ?", (project_id, limit)
        )
        items: list[dict[str, Any]] = []
        for row in events:
            items.append(
                {
                    "at": row["at"],
                    "type": "event",
                    "kind": row["kind"],
                    "actor": row["actor"],
                    "task_id": row["task_id"],
                    "run_id": row["run_id"],
                    "summary": row["summary"],
                    "data": loads(row["data"], {}),
                }
            )
        for row in self.db.query(
            "SELECT * FROM runs WHERE project_id = ? ORDER BY started_at LIMIT ?", (project_id, limit)
        ):
            items.append(
                {
                    "at": row["started_at"],
                    "type": "run",
                    "kind": f"run.{row['status']}",
                    "actor": row["agent_id"],
                    "task_id": row["task_id"],
                    "run_id": row["id"],
                    "summary": f"{row['agent_id']} 用时 {row['duration_ms']}ms，退出码 {row['exit_code']}",
                    "data": {"exit_code": row["exit_code"], "duration_ms": row["duration_ms"], "tokens": row["cost_tokens"]},
                }
            )
        items.sort(key=lambda x: x["at"] or "")
        return items

    def export_timeline(self, project_id: str, out_path: str | Path, fmt: str = "markdown") -> str:
        items = self.timeline(project_id)
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            path.write_text(dumps(items), encoding="utf-8")
        else:
            lines = [f"# 协作时间线 · {project_id}", "", f"共 {len(items)} 条记录", ""]
            for item in items:
                lines.append(
                    f"- `{item['at']}` **{item['kind']}** "
                    f"{('· ' + (item['actor'] or '')) if item['actor'] else ''} "
                    f"{('· ' + item['task_id']) if item['task_id'] else ''} — {item['summary']}"
                )
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(path)

    # ------------------------------------------------------------------ 产出对比
    def compare_runs(self, task_id: str) -> dict[str, Any]:
        """对比不同 Agent 对同一任务的产出差异（需求 50）。"""
        runs = self.runs(task_id=task_id, limit=50)
        runs.sort(key=lambda r: r["started_at"])
        artifacts: dict[str, dict[str, Any]] = {}
        for run in runs:
            rows = self.db.query(
                "SELECT rel_path, sha256, size, abs_path FROM artifacts WHERE run_id = ? AND role = 'output'",
                (run["id"],),
            )
            for row in rows:
                entry = artifacts.setdefault(row["rel_path"], {"per_run": {}})
                entry["per_run"][run["id"]] = {
                    "sha256": row["sha256"],
                    "size": row["size"],
                    "abs_path": row["abs_path"],
                }
        comparison = []
        for rel, info in sorted(artifacts.items()):
            hashes = {r: v["sha256"] for r, v in info["per_run"].items()}
            distinct = {h for h in hashes.values() if h}
            comparison.append(
                {
                    "rel_path": rel,
                    "identical": len(distinct) <= 1,
                    "per_run": info["per_run"],
                }
            )
        return {
            "task_id": task_id,
            "runs": [
                {"run_id": r["id"], "agent_id": r["agent_id"], "status": r["status"], "started_at": r["started_at"]}
                for r in runs
            ],
            "files": comparison,
        }

    def diff_runs_text(self, run_a: str, run_b: str, rel_path: str) -> str:
        rows = {
            r["run_id"]: r for r in self.db.query(
                "SELECT run_id, abs_path FROM artifacts WHERE run_id IN (?, ?) AND rel_path = ?",
                (run_a, run_b, rel_path),
            )
        }
        if run_a not in rows or run_b not in rows:
            return f"任一运行缺少该产物：{rel_path}"
        text_a = read_text(rows[run_a]["abs_path"]).splitlines()
        text_b = read_text(rows[run_b]["abs_path"]).splitlines()
        diff = difflib.unified_diff(
            text_a, text_b, fromfile=f"{run_a}:{rel_path}", tofile=f"{run_b}:{rel_path}", lineterm=""
        )
        return "\n".join(diff)
