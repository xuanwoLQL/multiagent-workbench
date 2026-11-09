"""假 Agent 的行为计划：用 JSON 描述「写哪些文件 / 删哪些文件 / 交接说明 / 退出码」。

通过环境变量传入：
    FAKE_PLAN=<json>          直接传 JSON
    FAKE_PLAN_FILE=<path>     从文件读取 JSON
支持字段：
    files: {相对路径: 内容}
    append: {相对路径: 追加内容}
    delete: [相对路径...]
    handoff: {did, not_done, open_issues, next_steps, artifacts, notes}
    exit: int
    stdout: str
    stderr: str
    sleep: float
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def read_plan() -> dict[str, Any]:
    raw = os.environ.get("FAKE_PLAN")
    if not raw:
        path = os.environ.get("FAKE_PLAN_FILE")
        if path and Path(path).exists():
            raw = Path(path).read_text(encoding="utf-8")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def apply_plan(plan: dict[str, Any], workspace: str | Path = ".") -> dict[str, Any]:
    root = Path(workspace)
    written: list[str] = []
    deleted: list[str] = []
    for rel, content in (plan.get("files") or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
        written.append(rel)
    for rel, content in (plan.get("append") or {}).items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        target.write_text(existing + str(content), encoding="utf-8")
        written.append(rel)
    for rel in plan.get("delete") or []:
        target = root / rel
        if target.exists():
            target.unlink()
            deleted.append(rel)
    handoff = plan.get("handoff")
    if handoff:
        path = root / ".multiagent" / "handoff.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(handoff, ensure_ascii=False), encoding="utf-8")
    if plan.get("sleep"):
        time.sleep(float(plan["sleep"]))
    return {"written": written, "deleted": deleted}
