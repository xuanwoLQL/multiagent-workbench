"""合并失败阻塞的任务不得通过普通 retry 重跑。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from multiagent.api.app import create_api
from multiagent.orchestrator.engine import MergeRetryBlocked


def _blocked_task(wb, project, reason: str) -> dict:
    task = wb.tasks.create(project["id"], f"blocked-{reason}", kind="generic", agent_id="worker")
    return wb.tasks.set_status(task["id"], "blocked", reason)


def test_retry_rejects_merge_conflict_block(wb, project) -> None:
    task = _blocked_task(wb, project, "merge_conflict")
    history_before = wb.tasks.history(task["id"])

    with pytest.raises(MergeRetryBlocked) as excinfo:
        wb.engine.retry(task["id"])

    assert excinfo.value.task_id == task["id"]
    assert excinfo.value.blocked_reason == "merge_conflict"
    assert excinfo.value.message
    stored = wb.tasks.get(task["id"])
    assert stored["status"] == "blocked"
    assert stored["blocked_reason"] == "merge_conflict"
    assert wb.tasks.history(task["id"]) == history_before


def test_retry_rejects_merge_missing_branch_block(wb, project) -> None:
    task = _blocked_task(wb, project, "merge_missing_branch")

    with pytest.raises(MergeRetryBlocked) as excinfo:
        wb.engine.retry(task["id"])

    assert excinfo.value.task_id == task["id"]
    assert excinfo.value.blocked_reason == "merge_missing_branch"
    stored = wb.tasks.get(task["id"])
    assert stored["status"] == "blocked"
    assert stored["blocked_reason"] == "merge_missing_branch"


def test_retry_allows_non_merge_block(wb, project) -> None:
    task = _blocked_task(wb, project, "验收失败")

    updated = wb.engine.retry(task["id"])

    assert updated["status"] == "pending"
    assert updated["blocked_reason"] is None


def test_retry_allows_after_merge_success(wb, project) -> None:
    task = _blocked_task(wb, project, "merge_conflict")
    recovered = wb.tasks.set_status(task["id"], "success", "人工合并成功")
    assert recovered["status"] == "success"
    assert recovered["blocked_reason"] is None

    updated = wb.engine.retry(task["id"])

    assert updated["status"] == "pending"


def test_retry_endpoint_returns_409_for_merge_block(wb, project) -> None:
    """API 不能把 MergeRetryBlocked 落成 500；必须返回 409 且状态不变。"""
    task = _blocked_task(wb, project, "merge_conflict")
    res = TestClient(create_api(wb)).post(f"/api/tasks/{task['id']}/retry")

    assert res.status_code == 409, res.text
    stored = wb.tasks.get(task["id"])
    assert stored["status"] == "blocked"
    assert stored["blocked_reason"] == "merge_conflict"
