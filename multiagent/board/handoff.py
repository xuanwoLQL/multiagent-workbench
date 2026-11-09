"""结构化交接说明（需求 27-31）。

每个任务结束都要产出「做了什么 / 没做什么 / 遗留问题 / 下一步建议」。
Agent 被要求在约定路径 ``<workspace>/.multiagent/handoff.json`` 落盘，
系统解析后写库、写黑板，并在后续任务的上下文中自动注入（需求 28）。
人工可以编辑修正，修正后的版本成为权威上下文（需求 30）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import Config
from ..db import Database
from ..util import dumps, loads, new_id, now_iso, read_text

_HANDOFF_KEYS = ("did", "not_done", "open_issues", "next_steps", "artifacts", "notes", "summary")
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_handoff(text: str) -> dict[str, Any]:
    """从 Agent 输出里尽力解析结构化交接；解析不到返回空结构。"""
    if not text:
        return {}
    candidates: list[str] = _FENCE_RE.findall(text)
    # 直接整体 JSON
    stripped = text.strip()
    if stripped.startswith("{"):
        candidates.insert(0, stripped)
    # 平衡括号扫描
    start = text.find("{")
    while start != -1:
        depth = 0
        for idx in range(start, len(text)):
            ch = text[idx]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : idx + 1])
                    break
        start = text.find("{", start + 1)
        if len(candidates) > 12:
            break
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict) and any(key in data for key in _HANDOFF_KEYS):
            return _normalize(data)
    return {}


def _normalize(data: dict[str, Any]) -> dict[str, Any]:
    def _list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    return {
        "did": str(data.get("did") or data.get("summary") or ""),
        "not_done": str(data.get("not_done") or ""),
        "open_issues": _list(data.get("open_issues")),
        "next_steps": _list(data.get("next_steps")),
        "artifacts": _list(data.get("artifacts")),
        "notes": str(data.get("notes") or ""),
    }


class HandoffService:
    def __init__(self, db: Database, config: Config, board=None):
        self.db = db
        self.config = config
        self.board = board

    # ------------------------------------------------------------------ 写入
    def create(
        self,
        *,
        project_id: str,
        task_id: str,
        run_id: str | None,
        agent_id: str | None,
        data: dict[str, Any],
        source: str = "agent",
        post_to_board: bool = True,
    ) -> dict[str, Any]:
        normalized = _normalize(data) if data else _normalize({})
        handoff_id = new_id("ho")
        now = now_iso()
        self.db.insert(
            "handoffs",
            {
                "id": handoff_id,
                "project_id": project_id,
                "task_id": task_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "did": normalized["did"],
                "not_done": normalized["not_done"],
                "open_issues": dumps(normalized["open_issues"]),
                "next_steps": dumps(normalized["next_steps"]),
                "artifacts": dumps(normalized["artifacts"]),
                "notes": normalized["notes"],
                "source": source,
                "created_at": now,
            },
        )
        if post_to_board and self.board is not None:
            # 新交接代表该任务的最新事实：旧交接里的遗留问题先整体作废，
            # 本次交接的 issues 会在下面重新 post 为 approved。
            self.board.supersede_task_issues(task_id, f"被新交接 {handoff_id} 取代")
            self.board.post(
                project_id,
                type="handoff",
                title=f"交接：{task_id}",
                body=render_handoff(normalized),
                scope="task",
                task_id=task_id,
                agent_id=agent_id,
                run_id=run_id,
                status="approved",
                actor=agent_id,
                note="任务结束时自动生成的交接说明",
            )
            for issue in normalized["open_issues"]:
                self.board.post(
                    project_id,
                    type="issue",
                    title=f"遗留问题：{issue[:60]}",
                    body=issue,
                    scope="task",
                    task_id=task_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    status="approved",
                    actor=agent_id,
                )
            for step in normalized["next_steps"]:
                self.board.post(
                    project_id,
                    type="todo",
                    title=f"下一步：{step[:60]}",
                    body=step,
                    scope="task",
                    task_id=task_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    status="approved",
                    actor=agent_id,
                )
        return self.get(handoff_id)

    def create_from_text(
        self,
        *,
        project_id: str,
        task_id: str,
        run_id: str | None,
        agent_id: str | None,
        text: str,
        source: str = "agent",
    ) -> dict[str, Any]:
        parsed = parse_handoff(text)
        if not parsed:
            parsed = {"did": "", "not_done": "", "notes": (text or "")[:2000]}
        return self.create(
            project_id=project_id,
            task_id=task_id,
            run_id=run_id,
            agent_id=agent_id,
            data=parsed,
            source=source,
        )

    def read_handoff_file(self, workspace: Path) -> dict[str, Any]:
        path = Path(workspace) / ".multiagent" / "handoff.json"
        if not path.exists():
            return {}
        raw = read_text(path)
        data = loads(raw, None)
        if isinstance(data, dict):
            return _normalize(data)
        return parse_handoff(raw)

    # ------------------------------------------------------------------ 编辑
    def edit(self, handoff_id: str, patch: dict[str, Any], *, editor: str) -> dict[str, Any]:
        current = self.get(handoff_id)
        merged = {
            "did": patch.get("did", current["did"]),
            "not_done": patch.get("not_done", current["not_done"]),
            "open_issues": patch.get("open_issues", current["open_issues"]),
            "next_steps": patch.get("next_steps", current["next_steps"]),
            "artifacts": patch.get("artifacts", current["artifacts"]),
            "notes": patch.get("notes", current["notes"]),
        }
        self.db.update(
            "handoffs",
            {"id": handoff_id},
            {
                "did": merged["did"],
                "not_done": merged["not_done"],
                "open_issues": dumps(merged["open_issues"]),
                "next_steps": dumps(merged["next_steps"]),
                "artifacts": dumps(merged["artifacts"]),
                "notes": merged["notes"],
                "source": "human",
                "edited_by": editor,
                "edited_at": now_iso(),
            },
        )
        if self.board is not None:
            self.board.append_event(
                project_id=current["project_id"],
                kind="handoff.edit",
                summary=f"人工修正交接 {handoff_id}",
                actor=editor,
                task_id=current["task_id"],
                run_id=current["run_id"],
            )
        return self.get(handoff_id)

    # ------------------------------------------------------------------ 读取
    def get(self, handoff_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM handoffs WHERE id = ?", (handoff_id,))
        if not row:
            raise KeyError(f"交接不存在：{handoff_id}")
        return _handoff(row)

    def latest_for_task(self, task_id: str) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT * FROM handoffs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,)
        )
        return _handoff(row) if row else None

    def list_for_project(self, project_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM handoffs WHERE project_id = ? ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        return [_handoff(r) for r in rows]


def _handoff(row) -> dict[str, Any]:
    data = dict(row)
    for key in ("open_issues", "next_steps", "artifacts"):
        data[key] = loads(data.get(key), []) or []
    return data


def render_handoff(data: dict[str, Any]) -> str:
    lines = ["【做了什么】", data.get("did") or "（未填写）", "", "【没做什么】", data.get("not_done") or "（无）"]
    issues = data.get("open_issues") or []
    steps = data.get("next_steps") or []
    arts = data.get("artifacts") or []
    lines += ["", "【遗留问题】", *([f"- {i}" for i in issues] or ["（无）"])]
    lines += ["", "【下一步建议】", *([f"- {i}" for i in steps] or ["（无）"])]
    if arts:
        lines += ["", "【产物】", *[f"- {a}" for a in arts]]
    if data.get("notes"):
        lines += ["", "【备注】", data["notes"]]
    return "\n".join(lines)
