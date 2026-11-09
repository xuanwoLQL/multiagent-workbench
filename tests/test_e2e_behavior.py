"""P2-17：CI 里的真实行为闭环（假 Agent，不消耗额度）。

不是 UI 导航冒烟，而是走真实 API + 真实 SQLite + 真实 git 仓库：

    导入项目 → 建任务 → 通过 API 执行（假 Agent 在 worktree 里写文件）
    → 验收 → 自动提交 + 自动合并 → 任务详情/工作副本/worktree/看板/仪表盘
    → 人工重复合并（幂等）→ 第二个任务在合并后的基线上继续。

Agent 行为全部由 ``set_plan`` 写入的假计划驱动；不启动后台项目调度线程，
不依赖网络、真实模型或任何外部服务。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from multiagent.api.app import create_api


# --------------------------------------------------------------------------- helpers


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    """真实 git 仓库：init + 身份配置 + README 首次提交。"""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _import_project(client: TestClient, root: Path) -> dict[str, Any]:
    res = client.post("/api/projects/import", json={"path": str(root)})
    assert res.status_code == 200, res.text
    project = res.json()
    assert project.get("id"), project
    assert Path(project["root_path"]).resolve() == root.resolve(), project
    return project


def _create_worktree_task(
    client: TestClient,
    pid: str,
    *,
    title: str,
    output: str,
    acceptance: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "title": title,
        "kind": "generic",
        "agent_id": "worker",
        "isolation": "worktree",
        "outputs": [output],
        "acceptance": acceptance,
    }
    res = client.post(f"/api/projects/{pid}/tasks", json=payload)
    assert res.status_code == 200, res.text
    task = res.json()
    assert task.get("id"), task
    return task


def _run_task(client: TestClient, tid: str) -> dict[str, Any]:
    """同步等待任务执行完（假 Agent，秒级完成）。"""
    res = client.post(f"/api/tasks/{tid}/run", json={"wait": 300})
    assert res.status_code == 200, res.text
    outcome = res.json()
    assert outcome.get("task_id") == tid, outcome
    assert outcome.get("status") == "success", outcome
    return outcome


def _task_detail(client: TestClient, tid: str) -> dict[str, Any]:
    res = client.get(f"/api/tasks/{tid}")
    assert res.status_code == 200, res.text
    return res.json()


# --------------------------------------------------------------------------- 主闭环


def test_full_task_lifecycle_via_api(wb, tmp_path: Path, set_plan):
    root = tmp_path / "repo"
    _init_repo(root)
    # 本用例验证自动合并闭环；默认策略是「先审后合」，这里显式开启。
    wb.config.policy_raw.setdefault("git", {})["auto_merge"] = True
    content = "hello from fake agent\n"
    set_plan(
        "worker",
        {"files": {"feature.txt": content}, "handoff": {"did": "写入 feature.txt"}, "exit": 0},
    )

    client = _client(wb)
    project = _import_project(client, root)
    pid = project["id"]

    # a/b. 通过真实 API 建 worktree 任务。
    task = _create_worktree_task(
        client,
        pid,
        title="Feature one",
        output="feature.txt",
        acceptance=[{"type": "file_exists", "path": "feature.txt"}],
    )
    tid = task["id"]
    assert task["isolation"] == "worktree", task
    assert task["agent_id"] == "worker", task

    # c. 执行：假 Agent 在 worktree 写文件，验收通过后自动提交并合并回基线。
    outcome = _run_task(client, tid)
    assert outcome.get("acceptance_ok") is True, outcome

    # d. 任务详情：真实 commit + 已合并 + 验收通过。
    detail = _task_detail(client, tid)
    stored = detail["task"]
    assert stored["status"] == "success", detail
    result = stored.get("result") or {}
    git_meta = result.get("git") or {}
    assert git_meta.get("commit"), detail
    assert (git_meta.get("merge") or {}).get("merged") is True, detail
    assert (result.get("acceptance") or {}).get("ok") is True, detail
    assert str(git_meta.get("branch") or "").startswith("ma/"), git_meta

    # e. 文件真实落到项目根目录，内容与假 Agent 写入一致。
    target = root / "feature.txt"
    assert target.is_file(), f"项目根目录缺少 feature.txt：{sorted(p.name for p in root.iterdir())}"
    assert target.read_text(encoding="utf-8") == content

    # f. 工作副本接口：已合并、无需再同步。
    ws_res = client.get(f"/api/tasks/{tid}/workspace")
    assert ws_res.status_code == 200, ws_res.text
    workspace = ws_res.json()
    assert workspace["merge_status"] == "merged", workspace
    assert workspace["can_sync"] is False, workspace
    assert ((workspace.get("git") or {}).get("merge") or {}).get("merged") is True, workspace

    # g. worktree 接口：同一份合并状态 + 任务分支。
    wt_res = client.get(f"/api/tasks/{tid}/worktree")
    assert wt_res.status_code == 200, wt_res.text
    worktree = wt_res.json()
    assert worktree["merge_status"] == "merged", worktree
    branch = (worktree.get("git") or {}).get("branch") or worktree.get("branch") or ""
    assert branch.startswith("ma/"), worktree
    workdir = Path(worktree["path"])
    assert (workdir / "feature.txt").read_text(encoding="utf-8") == content

    # h. 已自动合并后再次点「合并」：200、无冲突/阻断、文件不被破坏（幂等）。
    for attempt in (1, 2):
        merged_res = client.post(f"/api/tasks/{tid}/merge")
        assert merged_res.status_code == 200, merged_res.text
        merged = merged_res.json()
        assert merged.get("merged") is True, merged
        assert merged.get("conflict") is not True, merged
        assert merged.get("blocked") is not True, merged
        assert target.read_text(encoding="utf-8") == content, (
            f"第 {attempt} 次重复合并破坏了 feature.txt"
        )

    # i. 仪表盘：任务在列表中且状态 success。
    dash_res = client.get(f"/api/projects/{pid}/dashboard")
    assert dash_res.status_code == 200, dash_res.text
    dashboard = dash_res.json()
    row = next((item for item in dashboard.get("tasks") or [] if item.get("id") == tid), None)
    assert row is not None, dashboard
    assert row["status"] == "success", row
    assert dashboard["progress"]["counters"]["success"] >= 1, dashboard

    # j. 审阅状态。本仓库没有 GET /api/projects/{pid}/reviews；若未来新增则直接
    #    断言任务出现在响应里。当前用等价事实验证「已审阅/已合并侧」：
    #    任务的交接黑板条目已 approved，且没有任何待审条目挂在该任务上。
    reviews_res = client.get(f"/api/projects/{pid}/reviews")
    if reviews_res.status_code == 200:
        assert tid in json.dumps(reviews_res.json(), ensure_ascii=False), reviews_res.text
    else:
        assert reviews_res.status_code == 404, reviews_res.text
        memory_res = client.get(f"/api/projects/{pid}/memory")
        assert memory_res.status_code == 200, memory_res.text
        memory = memory_res.json()
        pending = [e for e in memory.get("pending") or [] if e.get("task_id") == tid]
        assert pending == [], memory
        board_res = client.get(f"/api/projects/{pid}/board", params={"task_id": tid})
        assert board_res.status_code == 200, board_res.text
        entries = board_res.json()
        assert entries, "任务成功后应有交接黑板条目"
        assert all(e.get("status") != "pending" for e in entries), entries
        assert any(
            e.get("type") == "handoff" and e.get("status") == "approved" for e in entries
        ), entries

    # k. 事件流（GET /events 是哈希链事件；/board 返回的是黑板条目）：
    #    必须包含真实 git 提交与合并事件。
    events_res = client.get(f"/api/projects/{pid}/events")
    assert events_res.status_code == 200, events_res.text
    events = events_res.json()
    task_events = [e for e in events if e.get("task_id") == tid]
    kinds = {e.get("kind") for e in task_events}
    assert "task.git.committed" in kinds, task_events
    assert "task.git.merged" in kinds, task_events


# --------------------------------------------------------------------------- 依赖基线


def test_second_task_sees_merged_baseline(wb, tmp_path: Path, set_plan):
    """第一个任务合并后，第二个任务的 worktree 必须基于最新基线。"""
    root = tmp_path / "repo"
    _init_repo(root)
    # DAG 连续交付场景：显式开启自动合并（生产默认关闭）。
    wb.config.policy_raw.setdefault("git", {})["auto_merge"] = True
    first_content = "first task baseline\n"
    set_plan(
        "worker",
        {"files": {"feature.txt": first_content}, "handoff": {"did": "第一个任务"}, "exit": 0},
    )

    client = _client(wb)
    project = _import_project(client, root)
    pid = project["id"]

    first = _create_worktree_task(
        client,
        pid,
        title="First feature",
        output="feature.txt",
        acceptance=[{"type": "file_exists", "path": "feature.txt"}],
    )
    _run_task(client, first["id"])
    first_result = _task_detail(client, first["id"])["task"]["result"]
    first_git = first_result["git"]
    first_merge_sha = (first_git.get("merge") or {}).get("sha")
    assert first_merge_sha, first_result
    assert (root / "feature.txt").read_text(encoding="utf-8") == first_content

    second_content = "second task on top of baseline\n"
    set_plan(
        "worker",
        {"files": {"feature2.txt": second_content}, "handoff": {"did": "第二个任务"}, "exit": 0},
    )
    second = _create_worktree_task(
        client,
        pid,
        title="Second feature",
        output="feature2.txt",
        # feature.txt 是验收条件而不是产物：只有 worktree 基于合并后的 HEAD 创建时
        # 这个检查才会通过（否则任务直接 failed，断言信息里带 acceptance.checks）。
        acceptance=[
            {"type": "file_exists", "path": "feature.txt"},
            {"type": "file_exists", "path": "feature2.txt"},
        ],
    )
    outcome = _run_task(client, second["id"])
    assert outcome.get("acceptance_ok") is True, outcome

    detail = _task_detail(client, second["id"])
    assert detail["task"]["status"] == "success", detail
    result = detail["task"]["result"]
    git_meta = result["git"]
    assert (git_meta.get("merge") or {}).get("merged") is True, detail
    assert git_meta.get("base") == first_merge_sha, (
        "第二个任务必须基于第一个任务合并后的 HEAD 创建 worktree"
    )

    # 第二个任务的 worktree 能读到第一个任务的文件（而不是空的基线）。
    wt_res = client.get(f"/api/tasks/{second['id']}/worktree")
    assert wt_res.status_code == 200, wt_res.text
    worktree = wt_res.json()
    assert worktree["merge_status"] == "merged", worktree
    workdir = Path(worktree["path"])
    assert (workdir / "feature.txt").read_text(encoding="utf-8") == first_content
    assert (workdir / "feature2.txt").read_text(encoding="utf-8") == second_content

    # 合并后根目录同时保留两个任务的产物。
    assert (root / "feature2.txt").read_text(encoding="utf-8") == second_content
    assert (root / "feature.txt").read_text(encoding="utf-8") == first_content
