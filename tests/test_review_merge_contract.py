"""审阅页「已合并 / 待合并 / 冲突」契约（P0-1 + P0-3）。

后端契约：``GET /api/tasks/{tid}/workspace`` 必须把 ``task.result.git.merge``
归纳成 ``merge_status``，审阅页据此决定显示「已合并」「冲突」还是文件同步。

静态契约：``review-api.ts`` 必须暴露合并 mutation，``ReviewDetail.tsx`` 必须
有冲突面板文案，且 ``groupFor`` 对 ``merged`` 返回 ``synced``（不能继续留在
「待审阅」里）。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api

REPO = Path(__file__).resolve().parents[1]
REVIEW_API = REPO / "webapp" / "src" / "features" / "review" / "review-api.ts"
REVIEW_DETAIL = REPO / "webapp" / "src" / "features" / "review" / "ReviewDetail.tsx"


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _project(wb, tmp_path: Path) -> dict:
    root = tmp_path / "root"
    root.mkdir()
    return wb.projects.create("review-merge", root_path=str(root))


def _task_with_merge(wb, project: dict, merge: dict) -> dict:
    task = wb.tasks.create(
        project["id"],
        "merge contract probe",
        kind="generic",
        isolation="worktree",
    )
    wb.tasks.update(
        task["id"],
        result={
            "git": {
                "branch": "ma/x",
                "base": "def",
                "merge": merge,
            }
        },
    )
    return task


def _workspace(wb, project: dict, task: dict) -> dict:
    response = _client(wb).get(f"/api/tasks/{task['id']}/workspace")
    assert response.status_code == 200, response.text
    return response.json()


def test_workspace_reports_merged_task(wb, tmp_path: Path):
    project = _project(wb, tmp_path)
    task = _task_with_merge(
        wb,
        project,
        {"merged": True, "branch": "ma/x", "sha": "abc", "base": "def"},
    )

    body = _workspace(wb, project, task)

    assert body["merge_status"] == "merged"
    assert body["git"]["merge"]["merged"] is True
    assert body["git"]["merge"]["branch"] == "ma/x"
    assert body["git"]["merge"]["sha"] == "abc"
    # 已合并的任务不再提供文件级「同步」，否则页面会继续显示「待同步」。
    assert body["can_sync"] is False


def test_workspace_reports_conflict(wb, tmp_path: Path):
    project = _project(wb, tmp_path)
    task = _task_with_merge(
        wb,
        project,
        {"merged": False, "conflict": True, "files": ["a.py"]},
    )

    body = _workspace(wb, project, task)

    assert body["merge_status"] == "conflict"
    assert body["git"]["merge"]["conflict"] is True
    assert body["git"]["merge"]["files"] == ["a.py"]


def test_workspace_reports_auto_merge_disabled(wb, tmp_path: Path):
    project = _project(wb, tmp_path)
    task = _task_with_merge(
        wb,
        project,
        {"merged": False, "skipped": True, "reason": "auto_merge_disabled"},
    )

    body = _workspace(wb, project, task)

    assert body["merge_status"] == "auto_merge_disabled"
    assert body["git"]["merge"]["skipped"] is True
    assert body["git"]["merge"]["reason"] == "auto_merge_disabled"


def test_review_sources_cover_merge_states():
    api_source = REVIEW_API.read_text(encoding="utf-8", errors="replace")
    detail_source = REVIEW_DETAIL.read_text(encoding="utf-8", errors="replace")

    assert "merge_status" in api_source
    assert "export function useMergeTask" in api_source
    assert "export function useDiscardBranch" in api_source
    assert "/discard-worktree" in api_source
    assert "delete_branch: true" in api_source

    assert "已合并到" in detail_source
    assert "重试合并" in detail_source
    assert "放弃任务分支" in detail_source
    assert "taskMergeState" in detail_source
    assert "useMergeTask" in detail_source
    assert "useDiscardBranch" in detail_source
    assert 'from "../task/helpers"' in detail_source

    match = re.search(r"export function groupFor.*?\n\}", api_source, re.S)
    assert match, "review-api.ts 缺少 groupFor"
    body = match.group(0)
    assert 'merge_status === "merged"' in body
    assert 'return "synced"' in body
    # merged 必须先于 task.status 判断：失败但已合并的任务也应归入已同步。
    assert body.index('"merged"') < body.index('task.status !== "success"')
