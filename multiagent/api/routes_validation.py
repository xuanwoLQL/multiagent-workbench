"""Project inspection and task validation routes."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Body, HTTPException

from ..util import ensure_dir
from ..validation.detect import DEFAULT_TIMEOUT_SEC, detect_project, find_python
from ..validation.runner import MAX_TIMEOUT_SEC, run_checks


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _report_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _task_root(ctx, task: dict[str, Any]) -> tuple[Path, Path, str]:
    project = ctx.project(task["project_id"])
    project_root = Path(project["root_path"])
    mode = str(task.get("isolation") or "copy").lower()
    if mode == "shared":
        return project_root, project_root, mode
    work = ctx.app.isolation.workdir(task["project_id"], task["id"])
    if not work.is_dir():
        raise HTTPException(
            409,
            "任务工作副本尚未创建；请先运行任务，或改用 shared 隔离后再验证",
        )
    return work, project_root, mode


def _validation_dir(ctx, task: dict[str, Any]) -> Path:
    return (
        Path(ctx.app.config.runtime_dir)
        / task["project_id"]
        / "tasks"
        / task["id"]
        / "validation"
    )


def _env_extra(project_root: Path) -> dict[str, str]:
    """Let isolated copies resolve dependencies installed in the origin tree."""
    node_modules = project_root / "node_modules"
    if not node_modules.is_dir():
        return {}
    extra: dict[str, str] = {"NODE_PATH": str(node_modules)}
    bin_dir = node_modules / ".bin"
    if bin_dir.is_dir():
        base_path = os.environ.get("PATH") or os.defpath
        extra["PATH"] = str(bin_dir) + os.pathsep + base_path
    return extra


def _checks_payload(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple)):
        if any(not isinstance(item, str) for item in raw):
            raise HTTPException(400, "checks 必须是字符串数组")
        values = list(raw)
    else:
        raise HTTPException(400, "checks 必须是字符串数组")
    checks = [item.strip() for item in values if item and item.strip()]
    if len(checks) > 32:
        raise HTTPException(400, "checks 最多 32 项")
    return checks or None


def register_validation_routes(api, ctx) -> None:
    @api.get("/api/projects/{pid}/inspect")
    def inspect_project(pid: str) -> dict[str, Any]:
        project = ctx.project(pid)
        root = Path(project["root_path"])
        if not root.is_dir():
            raise HTTPException(404, "工作区目录不存在")
        try:
            return detect_project(root, python=find_python(root))
        except OSError as exc:
            raise HTTPException(400, f"无法扫描项目：{exc}") from exc

    @api.post("/api/tasks/{tid}/validate")
    def validate_task(
        tid: str,
        payload: dict[str, Any] | None = Body(default=None),
    ) -> dict[str, Any]:
        task = ctx.task(tid)
        data = payload or {}
        work, project_root, mode = _task_root(ctx, task)
        python = find_python(project_root) or find_python(work)
        try:
            profile = detect_project(work, python=python)
        except OSError as exc:
            raise HTTPException(400, f"无法扫描验证目录：{exc}") from exc

        checks = _checks_payload(data.get("checks"))
        raw_timeout = data.get("timeout_sec", profile.get("default_timeout_sec", DEFAULT_TIMEOUT_SEC))
        try:
            timeout_sec = int(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "timeout_sec 必须是整数") from exc
        if timeout_sec < 1 or timeout_sec > MAX_TIMEOUT_SEC:
            raise HTTPException(400, f"timeout_sec 必须在 1..{MAX_TIMEOUT_SEC} 之间")

        started_at = _now()
        results = run_checks(
            work,
            profile,
            checks=checks,
            timeout_sec=timeout_sec,
            cancel=threading.Event(),
            env_extra=_env_extra(project_root),
        )
        finished_at = _now()

        passed = sum(1 for item in results if item["ok"] and not item["skipped"])
        failed = sum(
            1
            for item in results
            if not item["ok"] and not item["skipped"] and not item["timeout"] and not item["cancelled"]
        )
        skipped = sum(1 for item in results if item["skipped"])
        timed_out = sum(1 for item in results if item["timeout"])
        cancelled = sum(1 for item in results if item["cancelled"])
        ok = passed > 0 and failed == 0 and timed_out == 0 and cancelled == 0
        if not results:
            message = "未检测到可运行的检查"
        elif ok:
            message = f"验证通过（{passed} 项）"
        elif timed_out:
            message = f"验证超时（{timed_out} 项）"
        elif failed:
            message = f"验证失败（{failed} 项）"
        elif passed == 0:
            message = "所有检查均被跳过（缺少工具或命令）"
        else:
            message = "验证未通过"

        report: dict[str, Any] = {
            "report_id": _report_id(),
            "task_id": task["id"],
            "project_id": task["project_id"],
            "mode": mode,
            "root": str(work),
            "project_root": str(project_root),
            "started_at": started_at,
            "finished_at": finished_at,
            "timeout_sec": timeout_sec,
            "requested": (
                checks
                if checks is not None
                else list(profile.get("recommended") or list(profile.get("commands") or {}))
            ),
            "profile": profile,
            "checks": results,
            "summary": {
                "total": len(results),
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "timeout": timed_out,
                "cancelled": cancelled,
                "duration_ms": sum(int(item.get("duration_ms") or 0) for item in results),
            },
            "ok": ok,
            "empty": not results,
            "message": message,
        }

        out_dir = ensure_dir(_validation_dir(ctx, task))
        target = out_dir / f"{report['report_id']}.json"
        temp = target.with_suffix(".json.tmp")
        report["stored_at"] = str(target)
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, target)
        return report

    @api.get("/api/tasks/{tid}/validation")
    def latest_validation(tid: str) -> dict[str, Any]:
        task = ctx.task(tid)
        out_dir = _validation_dir(ctx, task)
        if not out_dir.is_dir():
            raise HTTPException(404, "该任务还没有验证报告")
        reports = sorted(out_dir.glob("*.json"), key=lambda path: path.name, reverse=True)
        for path in reports:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return data
        raise HTTPException(404, "该任务还没有验证报告")
