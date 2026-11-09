"""任务详情「诚实合并 UI」契约测试（P0-1 / P0-3 / P0-5 / P1-9 / P1-10）。

覆盖两层：

1. 静态契约：``TaskDetailView.tsx`` / ``ChangesTab.tsx`` 必须以
   ``taskMergeState()`` 决定合并入口，409 必须转成页面内的「合并问题」面板，
   隔离降级必须展示 ``degrade_reason``，超时与重试文案必须诚实。
2. 后端字段契约：``GET /api/tasks/{tid}`` 必须原样回传
   ``task.result.git.merge`` 的 branch / sha / base / files / dirty_files，
   前端状态机读取的字段不能悄悄漂移。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api

REPO = Path(__file__).resolve().parents[1]
TASK_DIR = REPO / "webapp" / "src" / "features" / "task"
TASK_DETAIL = TASK_DIR / "TaskDetailView.tsx"
CHANGES_TAB = TASK_DIR / "ChangesTab.tsx"


def _read(path: Path) -> str:
    assert path.exists(), f"缺少文件：{path}"
    return path.read_text(encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- 静态契约


def test_task_detail_uses_merge_state_instead_of_sync():
    """git 任务必须由 taskMergeState 决定合并 UI，绝不继续显示「同步到工作区」。"""
    text = _read(TASK_DETAIL)

    assert "taskMergeState" in text, "TaskDetailView 必须使用 taskMergeState 推导合并状态"
    assert "taskMergeInfo" in text, "TaskDetailView 必须读取 merge 元数据（branch/sha）"
    assert "taskGitInfo" in text, "TaskDetailView 必须回退读取 git.branch / git.commit"
    assert "taskIsolationInfo" in text, "TaskDetailView 必须读取隔离降级信息"

    for needle in [
        # 成功态：Badge 文案 + 回退取 branch/sha
        "已合并到",
        "mergeInfo?.branch || gitInfo?.branch",
        "mergeInfo?.sha || gitInfo?.commit",
        # 未合并 / 自动合并关闭 / 下一步待合并
        "合并到基线",
        # 冲突 / 脏基线 / 其它阻断
        "重试合并",
        "放弃任务分支",
        # 人工放弃分支后必须是终态：不再提供必然 409 的合并入口
        'mergeState === "discarded"',
        "任务分支已放弃",
        "合并问题",
        # 后端接口
        "/discard-worktree",
        "/timeout",
        "/merge",
        # ChangesTab 必须拿到同一个 task 才能复用同一套状态机
        "task={task}",
        # 诚实文案
        "系统不会自动解决冲突",
        "文件级续跑",
        "不会延长正在运行的上次调用",
        # 隔离降级
        "degrade_reason",
        "worktree 不可用",
    ]:
        assert needle in text, f"TaskDetailView 缺少契约文案/调用：{needle}"

    # 「已合并」状态下主按钮区域不能退化成同步按钮。
    merged_branch = text[text.index('mergeState === "merged"') : text.index('} else if (mergeState)')]
    assert "同步到工作区" not in merged_branch, "已合并状态不得显示同步按钮"
    assert "再次运行" in merged_branch, "已合并状态可保留「再次运行」"

    # 「任务分支已放弃」是终态：隐藏合并/同步/重试合并，避免点击必然 409。
    discarded_start = text.index('mergeState === "discarded"')
    discarded_end = text.index('} else if (task.status === "pending")', discarded_start)
    discarded_branch = text[discarded_start:discarded_end]
    assert "任务分支已放弃" in discarded_branch
    for forbidden in ("合并到基线", "重试合并", "同步到工作区", "request(\"merge\")"):
        assert forbidden not in discarded_branch, f"已放弃分支不得显示：{forbidden}"


def test_task_detail_turns_409_into_inline_merge_panel():
    """409 不能只弹 toast：必须读取 error.body 并转成面板状态。"""
    text = _read(TASK_DETAIL)

    assert "ApiError" in text
    assert "error instanceof ApiError" in text
    assert "error.status === 409" in text
    assert "error.body" in text
    assert "mergeIssueFromBody" in text
    assert "setMergeIssue" in text
    assert "record.conflict" in text
    assert "record.blocked" in text
    assert "record.files" in text
    assert "record.dirty_files" in text
    # 面板动作：重试合并 / 放弃任务分支 / 打开工作目录
    assert "execute(\"merge\")" in text
    assert 'request("discardBranch")' in text
    assert "openWorkdir()" in text
    # 文件列表最多 12 个，超出显示 …等 N 个
    assert ".slice(0, 12)" in text
    assert "…等 {mergeProblemFiles.length} 个" in text


def test_changes_tab_respects_merge_state():
    """改动页签也必须按合并状态切换按钮，不能对 git 任务显示「同步到工作区」。"""
    text = _read(CHANGES_TAB)

    assert "taskMergeState" in text
    assert "taskMergeInfo" in text
    assert "taskGitInfo" in text
    assert "task?: Task" in text, "ChangesTab 必须接受可选 task prop（由详情页传入）"
    assert "已合并" in text
    assert "无需同步" in text
    assert "合并到基线" in text
    assert "合并被阻止" in text
    assert "查看合并问题" in text
    assert 'mergeState === "discarded"' in text
    assert "任务分支已放弃，未合并任何改动" in text
    assert "ApiError" in text
    assert "error.body" in text
    assert "body?.message" in text
    assert 'api.post<MergeResponse>(`/api/tasks/${taskId}/merge`' in text

    # 同步按钮只在 taskMergeState === null（copy/shared）的分支出现。
    assert "mergeState === null && canSync" in text
    assert "mergeState === null && !canSync" in text


def test_merge_blocked_tasks_hide_plain_retry_entry():
    """merge_* 阻塞时普通重试必然 409：概览、头部、菜单与其他 tab 的 onRetry 都要拦住。"""
    overview = _read(TASK_DIR / "OverviewTab.tsx")
    detail = _read(TASK_DETAIL)

    # 概览页：按 blocked_reason 的 merge_ 前缀识别，重试按钮只存在于非 merge 分支。
    assert 'String(task.blocked_reason || "").startsWith("merge_")' in overview
    assert "请先处理合并冲突或重建分支" in overview
    guard_at = overview.index("mergeBlocked ? (")
    else_at = overview.index(") : (", guard_at)
    retry_at = overview.index("onRetry", guard_at)
    assert else_at < retry_at, "普通重试按钮必须位于 mergeBlocked 的 else 分支"
    assert "查看日志" in overview[max(0, guard_at - 500) : guard_at], "合并阻塞时仍保留查看日志"

    # 详情页：统一 canRetry 判断，merge_* blocked 时主按钮只给提示，不给重试。
    assert "canRetry" in detail
    assert "请先处理合并冲突或重建分支" in detail
    branch_start = detail.index(
        'task.status === "failed" || task.status === "blocked" || task.status === "interrupted"'
    )
    branch_end = detail.index("} else if (canSync)", branch_start)
    retry_branch = detail[branch_start:branch_end]
    assert "canRetry ? (" in retry_branch
    assert "请先处理合并冲突或重建分支" in retry_branch
    assert 'request("retry")' in retry_branch
    assert retry_branch.index("canRetry ? (") < retry_branch.index('request("retry")')

    # 更多菜单里的「重新排队」同样受 canRetry 约束。
    menu_retry_at = detail.index("> 重新排队")
    menu_window = detail[max(0, menu_retry_at - 700) : menu_retry_at]
    assert "canRetry &&" in menu_window, "菜单重试入口必须受 canRetry 约束"

    # request("retry") 的防御必须在 setConfirm / execute 之前，且只 toast 不发请求。
    req_start = detail.index("const request = (kind: ActionKind) => {")
    req_end = detail.index("const saveTitle", req_start)
    request_body = detail[req_start:req_end]
    assert 'kind === "retry"' in request_body
    assert '.startsWith("merge_")' in request_body
    assert "请先处理合并冲突或重建分支" in request_body
    assert "toast.info" in request_body
    guard_at = request_body.index('kind === "retry"')
    assert guard_at < request_body.index("setConfirm(")
    assert guard_at < request_body.index("execute(")


def test_isolation_degrade_reason_is_rendered():
    """隔离降级必须可见：文本包含 degrade_reason，title 提供完整原因。"""
    text = _read(TASK_DETAIL)

    assert "isolationInfo?.degraded" in text
    assert "isolationInfo?.degrade_reason" in text
    assert "isolationTitle" in text
    assert "（worktree 不可用" in text
    assert 'tone={isolationInfo?.degraded ? "warning" : "outline"}' in text


def test_missing_branch_rebuild_ui_contract():
    """分支 ref 缺失必须是独立状态：不提供必然失败的合并重试，只能重建或放弃。"""
    helpers = _read(TASK_DIR / "helpers.ts")
    detail = _read(TASK_DETAIL)
    changes = _read(CHANGES_TAB)
    review_detail = _read(REPO / "webapp" / "src" / "features" / "review" / "ReviewDetail.tsx")
    review_api = _read(REPO / "webapp" / "src" / "features" / "review" / "review-api.ts")

    assert "missing_branch" in helpers
    assert 'merge.reason === "missing_branch"' in helpers
    assert 'code === "missing_branch"' in helpers
    discarded_at = helpers.index('if (merge.discarded) return "discarded";')
    missing_at = helpers.index('if (merge.reason === "missing_branch"')
    assert discarded_at < missing_at, "discarded 必须仍是优先终态"

    assert "missing_branch" in detail
    assert "分支已不存在，无法合并" in detail
    assert "任务分支已被删除，不能直接重试合并" in detail
    assert "重建分支" in detail
    assert "放弃任务" in detail
    assert "useRebuildBranch" in detail
    assert "确认重建" in detail
    assert "commit/base" in detail
    assert 'request("rebuildBranch")' in detail
    assert 'request("discardBranch")' in detail
    assert 'setMergeIssue(null)' in detail

    # 删除分支面板：不出现「重试合并 / 直接 POST /merge」，只允许重建或放弃。
    missing_start = detail.index("mergeProblemMissing ? (")
    missing_end = detail.index(") : (", missing_start)
    missing_actions = detail[missing_start:missing_end]
    assert "重建分支" in missing_actions
    assert "放弃任务" in missing_actions
    for forbidden in ('execute("merge")', "重试合并"):
        assert forbidden not in missing_actions, f"缺失分支面板不得出现：{forbidden}"

    # 变更页：显示提示与禁用按钮，不能把用户引向必然 409 的 /merge。
    assert "mergeMissingBranch" in changes
    assert "分支已不存在，无法合并" in changes
    changes_start = changes.index("mergeMissingBranch ? (")
    changes_end = changes.index(") : null}", changes_start)
    changes_missing = changes[changes_start:changes_end]
    assert "disabled" in changes_missing
    assert "setConfirmMerge" not in changes_missing

    assert "missing_branch" in review_detail
    assert "分支已不存在，无法合并" in review_detail
    assert "重建分支" in review_detail
    assert "放弃任务" in review_detail
    assert "确认重建" in review_detail
    assert "useRebuildBranch" in review_detail
    assert "record.error" in review_detail and "details" in review_detail

    # 审阅页头部合并按钮必须禁用，而不是重新打开一个必然失败的合并确认框。
    header_start = review_detail.index("issueMissingBranch ? (")
    header_end = review_detail.index(") : issue ? null", header_start)
    missing_header = review_detail[header_start:header_end]
    assert "disabled" in missing_header
    assert "合并到基线" in missing_header
    assert "setMergeConfirmOpen(true)" not in missing_header

    assert "missing_branch" in review_api
    assert "useRebuildBranch" in review_api
    assert "/rebuild-branch" in review_api
    assert "confirm: true" in review_api

    rebuild_body = review_api[
        review_api.index("export function useRebuildBranch") :
        review_api.index("export function latestChanges")
    ]
    assert "invalidateReview(client, tid)" in rebuild_body


# --------------------------------------------------------------------------- 后端字段


def test_task_detail_api_exposes_merge_contract_fields(wb):
    """构造 task.result.git 后，GET 详情必须原样返回前端依赖的合并字段。"""
    client = TestClient(create_api(wb))
    project = client.post("/api/projects", json={"name": "merge-ui-contract"}).json()
    created = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={"title": "合并 UI 契约", "kind": "generic", "agent_id": "worker"},
    ).json()

    merge = {
        "merged": True,
        "conflict": False,
        "blocked": False,
        "skipped": False,
        "reason": None,
        "sha": "abc12345deadbeef",
        "branch": "ma/ui-contract",
        "base": "0123456789abcdef",
        "files": ["src/app.ts", "docs/readme.md"],
        "dirty_files": [],
        "error": None,
        "source": "manual",
    }
    git = {
        "branch": "ma/ui-contract",
        "commit": "abc12345deadbeef",
        "base": "0123456789abcdef",
        "files": ["src/app.ts", "docs/readme.md"],
        "auto_merge": False,
        "merge": merge,
    }
    isolation = {
        "requested_mode": "worktree",
        "mode": "copy",
        "degraded": True,
        "degrade_code": "dirty_workspace",
        "degrade_reason": "工作区有未提交改动",
    }
    wb.tasks.update(
        created["id"],
        result={"git": git, "isolation": isolation},
        timeout_sec=1800,
    )

    response = client.get(f"/api/tasks/{created['id']}")
    assert response.status_code == 200
    payload = response.json()
    task = payload["task"]

    assert task["result"]["git"]["branch"] == "ma/ui-contract"
    assert task["result"]["git"]["commit"] == "abc12345deadbeef"
    returned_merge = task["result"]["git"]["merge"]
    for key in (
        "merged",
        "conflict",
        "blocked",
        "reason",
        "sha",
        "branch",
        "base",
        "files",
        "dirty_files",
        "error",
    ):
        assert key in returned_merge, f"GET /api/tasks/{{tid}} 丢失 merge.{key}"
    assert returned_merge["merged"] is True
    assert returned_merge["sha"] == "abc12345deadbeef"
    assert returned_merge["branch"] == "ma/ui-contract"
    assert returned_merge["base"] == "0123456789abcdef"
    assert returned_merge["files"] == ["src/app.ts", "docs/readme.md"]
    assert returned_merge["dirty_files"] == []

    # 超时弹窗默认值 & 隔离徽章依赖的字段。
    assert task["timeout_sec"] == 1800
    assert task["isolation"] == "copy"
    assert task["result"]["isolation"]["degraded"] is True
    assert task["result"]["isolation"]["degrade_reason"] == "工作区有未提交改动"


def test_task_detail_api_exposes_missing_branch_merge_reason(wb):
    """GET /api/tasks/{tid} 必须原样回传 merge.reason/code=missing_branch。"""
    client = TestClient(create_api(wb))
    project = client.post("/api/projects", json={"name": "missing-branch-contract"}).json()
    created = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={"title": "缺失分支契约", "kind": "generic", "agent_id": "worker"},
    ).json()

    merge = {
        "merged": False,
        "conflict": False,
        "blocked": False,
        "skipped": False,
        "discarded": False,
        "reason": "missing_branch",
        "code": "missing_branch",
        "task_id": created["id"],
        "branch": "ma/missing-contract",
        "base": "fedcba9876543210",
        "sha": "1234567890abcdef",
        "files": ["missing.txt"],
        "dirty_files": [],
        "error": "task branch not found",
    }
    wb.tasks.update(
        created["id"],
        result={"git": {"branch": merge["branch"], "base": merge["base"], "merge": merge}},
    )

    response = client.get(f"/api/tasks/{created['id']}")
    assert response.status_code == 200
    returned = response.json()["task"]["result"]["git"]["merge"]

    assert returned["reason"] == "missing_branch"
    assert returned["code"] == "missing_branch"
    assert returned["branch"] == "ma/missing-contract"
    assert returned["base"] == "fedcba9876543210"
    assert returned["sha"] == "1234567890abcdef"
    assert returned["files"] == ["missing.txt"]
    assert returned["conflict"] is False
    assert returned["blocked"] is False


# --------------------------------------------------------------------------- P0：waiting 的 git 任务先验收再合并


def test_waiting_git_task_approves_then_merges_instead_of_file_sync():
    """waiting + git 交付时，主操作必须是「验收并合并」：/merge 成功后再 approve(sync:false)。

    waiting 且没有 git 元数据（copy/shared/非 git 项目）才允许保持文件级
    「验收并同步」。合并结果只认 /merge 响应，approve 不伪造已合并。
    """
    text = _read(TASK_DETAIL)

    assert "approveMerge" in text, "TaskDetailView 必须新增 approveMerge 动作"
    assert "验收并合并" in text, "必须提供「验收并合并」按钮文案"
    assert "/approve" in text
    assert "sync: false" in text

    # 确认框必须说明「先 git merge --no-ff，成功后再验收；脏树/冲突转为待处理」。
    assert "先通过 git merge --no-ff" in text
    assert "git merge --no-ff" in text
    assert "合并失败会中止并保持任务为 waiting" in text

    # waiting 分支：有 git 元数据走 approveMerge；没有 git 元数据保留 approve+sync。
    waiting_start = text.index('task.status === "waiting"')
    waiting_end = text.index('mergeState === "merged"', waiting_start)
    waiting = text[waiting_start:waiting_end]
    assert 'request("approveMerge")' in waiting, "waiting 的 git 任务必须走验收并合并"
    assert 'request("approve")' in waiting, "waiting 无 git 元数据时必须保留验收并同步"
    assert "验收并同步" in waiting
    assert "Boolean(gitInfo) || mergeState !== null" in waiting, "waiting 必须按 git 元数据分流"
    assert "验收并合并" in waiting

    # 执行序列：先 POST /merge，成功后才 approve(sync:false)。
    # 反序会在 merge 409 时把任务永久留在 success，下游误判依赖已满足。
    case_start = text.index('case "approveMerge":')
    case_end = text.index('case "sync":', case_start)
    approve_merge_case = text[case_start:case_end]
    assert "/approve" in approve_merge_case
    assert "sync: false" in approve_merge_case
    assert "/merge" in approve_merge_case
    merge_at = approve_merge_case.index("/merge")
    approve_at = approve_merge_case.index("/approve", merge_at)
    assert merge_at < approve_at, "approveMerge 必须先 /merge 成功后再 /approve"

    # 两步失败都必须有明确 toast；merge 失败时任务必须保持 waiting。
    assert "验收失败" in text
    assert "验收已完成，但合并失败" in text
    assert "合并已完成，但验收失败" in text
    assert "任务仍为 waiting" in text
    assert "已验收，但" in text
    assert '(kind === "merge" || kind === "approveMerge")' in text, (
        "approveMerge 的 409 必须复用 mergeIssueFromBody 面板"
    )
    assert "mergeIssueFromBody" in text
    assert "先合并，后验收" in approve_merge_case, (
        "approveMerge 必须先合并，避免 409 后留下 success+未合并"
    )
    assert "merged.merged === false" in text, "不能把 200 + merged=false 显示成已合并"


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def test_waiting_git_task_approve_sync_false_then_real_git_merge(wb, tmp_path: Path):
    """API 契约：waiting 任务 approve {sync:false} 只改状态，不回收文件也不合并；
    随后 POST /merge 才走真实 git 合并，且 result.git.merge 以 /merge 为准。
    """
    from multiagent.gitops import commit_worktree, current_branch, is_repo

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")

    project = wb.projects.create("waiting-merge", root_path=str(root))
    task = wb.tasks.create(
        project["id"],
        "等待人工验收的 git 任务",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "always"}],
    )
    work = Path(wb.runner.isolation.prepare(project, wb.tasks.get(task["id"])))
    assert is_repo(work), "测试前提：worktree 模式必须真正创建 git worktree"
    branch = current_branch(work)
    assert branch
    (work / "feature.txt").write_text("from branch\n", encoding="utf-8")
    committed = commit_worktree(work, "task change feature.txt", "Bot", "bot@example.com")
    assert committed["sha"], "测试前提：任务分支必须有真实提交"

    # 模拟 runner 在 waiting 前留下的 git 交付元数据：分支已提交、自动合并未执行。
    wb.tasks.update(
        task["id"],
        result={
            "git": {
                "branch": branch,
                "commit": committed["sha"],
                "base": _git(root, "rev-parse", "HEAD").stdout.strip(),
                "files": ["feature.txt"],
                "auto_merge": False,
                "merge": {
                    "merged": False,
                    "skipped": True,
                    "reason": "auto_merge_disabled",
                    "sha": None,
                    "branch": branch,
                    "base": _git(root, "rev-parse", "HEAD").stdout.strip(),
                    "files": [],
                    "dirty_files": [],
                },
            }
        },
    )
    wb.tasks.set_status(task["id"], "waiting", "等待人工验收")

    client = TestClient(create_api(wb))

    approve = client.post(
        f"/api/tasks/{task['id']}/approve",
        json={"sync": False, "note": "只验收，准备合并"},
    )
    assert approve.status_code == 200, approve.text
    approved = approve.json()
    assert approved["task"]["status"] == "success"
    assert approved["sync"] is None, "approve sync:false 不得执行文件回收"
    assert not (root / "feature.txt").exists(), "approve sync:false 不得把分支文件平铺回基线"
    assert _git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False).returncode != 0
    detail = client.get(f"/api/tasks/{task['id']}").json()["task"]
    stale_merge = detail["result"]["git"]["merge"]
    assert stale_merge.get("merged") is False, "approve 不得伪造已合并"
    assert stale_merge.get("skipped") is True, "approve 不得改写旧的合并元数据"

    merged = client.post(f"/api/tasks/{task['id']}/merge")
    assert merged.status_code == 200, merged.text
    merged_body = merged.json()
    assert merged_body["merged"] is True
    assert "feature.txt" in merged_body["files"]
    assert (root / "feature.txt").read_text(encoding="utf-8") == "from branch\n"

    detail = client.get(f"/api/tasks/{task['id']}").json()["task"]
    assert detail["status"] == "success"
    final_merge = detail["result"]["git"]["merge"]
    assert final_merge["merged"] is True
    assert final_merge["source"] == "manual"
    assert final_merge["files"] == merged_body["files"]
    assert final_merge["sha"] == merged_body["sha"]
