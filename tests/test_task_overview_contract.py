"""任务详情「概览」的诚实展示契约。

守住三件事：
1. 概览组件必须继续使用 merge / cost / isolation / process 四个诚实数据源，
   并提供验收路径修正入口（只能改条件，不能伪造通过）；
2. PATCH /api/tasks/{tid} 接受新的验收条件并真正落库；
3. task.result.cost 的 source 字段在 GET /api/tasks/{tid} 中保持可读，
   避免后端/前端类型漂移后把「未上报」又显示成 0 元免费。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api

REPO = Path(__file__).resolve().parents[1]
TASK_FEATURE = REPO / "webapp" / "src" / "features" / "task"
OVERVIEW = TASK_FEATURE / "OverviewTab.tsx"
OVERVIEW_CSS = TASK_FEATURE / "overview.css"


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def test_overview_uses_honest_delivery_sources_and_correction_entry():
    text = OVERVIEW.read_text(encoding="utf-8", errors="replace")

    required = [
        "taskMergeState",
        "taskCostInfo",
        "taskIsolationInfo",
        "taskProcessInfo",
        "未上报",
        "修正验收路径",
        "/api/tasks/",
        "acceptance",
    ]
    missing = [token for token in required if token not in text]
    assert not missing, f"OverviewTab.tsx 缺少诚实展示契约标记：{missing}"

    assert "./overview.css" in text, "OverviewTab.tsx 没有引入本地的 overview.css"
    assert OVERVIEW_CSS.exists(), "缺少 webapp/src/features/task/overview.css"


def test_patch_acceptance_roundtrip(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "overview-contract"}).json()["id"]
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "概览验收路径"}).json()

    acceptance = [
        {"type": "glob_min_files", "pattern": "**/AppShell.tsx", "min_count": 1},
    ]
    response = client.patch(f"/api/tasks/{task['id']}", json={"acceptance": acceptance})
    assert response.status_code == 200, response.text
    assert response.json()["acceptance"] == acceptance

    detail = client.get(f"/api/tasks/{task['id']}").json()
    assert detail["task"]["acceptance"] == acceptance


def test_task_detail_cost_source_contract(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "overview-cost"}).json()["id"]
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "成本来源"}).json()

    cases = [
        ("unreported", {"tokens": 1200, "amount": 0, "source": "unreported"}),
        ("reported", {"tokens": 900, "amount": 0.42, "source": "reported"}),
    ]
    for source, payload in cases:
        wb.tasks.update(task["id"], result={"cost": payload})
        detail = client.get(f"/api/tasks/{task['id']}").json()
        cost = detail["task"]["result"]["cost"]
        assert cost["source"] == source, f"成本来源漂移：{cost!r}"
        assert "tokens" in cost and "amount" in cost, f"成本字段缺失：{cost!r}"
        assert cost["tokens"] == payload["tokens"]
        assert cost["amount"] == payload["amount"]


def test_overview_never_claims_merged_without_merge_state():
    """waiting / 未合并的 git 任务展示不能误导成「已合并」。

    概览的 Git 交付面板只允许在 ``taskMergeState() === "merged"`` 时显示
    「已合并」；默认（not_merged / pending）必须是「尚未合并」。
    """
    text = OVERVIEW.read_text(encoding="utf-8", errors="replace")

    assert "taskMergeState" in text, "概览必须继续用统一状态机推导合并状态"
    assert 'mergeState === "merged"' in text, "「已合并」图标/文件数必须受 merged 状态保护"

    merged_at = text.index('case "merged":')
    conflict_at = text.index('case "conflict":', merged_at)
    merged_case = text[merged_at:conflict_at]
    assert "已合并" in merged_case, "只有 merged 分支允许出现「已合并」文案"

    not_merged_at = text.index('case "not_merged":')
    label_at = text.index("function mergeStateLabel", not_merged_at)
    default_case = text[not_merged_at:label_at]
    assert "尚未合并" in default_case, "not_merged/pending 必须显式显示「尚未合并」"
    assert "已合并" not in default_case, "not_merged/pending 不得出现「已合并」"


# --------------------------------------------------------------------------- P0：未合并改动对下游不可见


def test_overview_warns_unmerged_changes_are_invisible_to_downstream():
    """默认 auto_merge=false 时，任务验收通过但分支未合并。

    概览必须明确告知：下游任务看的是基线，看不到这些未合并产出，并给出
    合并入口 / 开启 auto_merge 的操作指引。警告只能在三个「等待合并」状态
    出现，merged / conflict / blocked / discarded 保持原有文案。
    """
    text = OVERVIEW.read_text(encoding="utf-8", errors="replace")
    css = OVERVIEW_CSS.read_text(encoding="utf-8", errors="replace")

    required = [
        "尚未合并到基线",
        "下游任务看不到",
        "先审后合",
        "auto_merge === false",
        "td-merge-warning",
    ]
    missing = [token for token in required if token not in text]
    assert not missing, f"OverviewTab.tsx 缺少未合并警告标记：{missing}"

    # 警告样式必须落在本地 overview.css，且只用 tokens.css 变量（不允许裸 hex）。
    assert ".td-merge-warning" in css, "缺少 .td-merge-warning 样式"
    assert "var(--warning" in css, "未合并警告必须使用 warning token"
    warning_css_at = css.index(".td-merge-warning")
    warning_css = css[warning_css_at:]
    assert "#" not in warning_css.split(".td-git-note")[0], "未合并警告样式不得使用裸 hex 颜色"

    # 触发条件只允许三个等待合并状态；merged 等终态不得进入警告分支。
    decl_at = text.index("const unmergedGitState")
    decl_end = text.index(";", text.index('mergeState === "pending"', decl_at))
    declaration = text[decl_at:decl_end]
    for state in ("auto_merge_disabled", "not_merged", "pending"):
        assert f'mergeState === "{state}"' in declaration, f"警告缺少状态：{state}"

    # 警告必须位于 Git 交付面板的保护条件之后：没有 git 信息时整个面板不渲染。
    panel_guard = text.index("git && mergeState && gitMerge")
    warning_at = text.index("unmergedGitState ?")
    assert panel_guard < warning_at, "警告必须由 Git 交付面板守卫，copy/shared 不应渲染"


def test_task_detail_preserves_auto_merge_disabled_metadata(wb):
    """GET /api/tasks/{tid} 原样返回未合并元数据，前端状态机输入不漂移。

    ``auto_merge=false`` 时 Runner 记录 ``merge.skipped=true`` /
    ``reason=auto_merge_disabled``；前端据此推导 ``auto_merge_disabled`` 并
    显示未合并警告。后端任何字段丢失都会让警告静默消失。
    """
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "overview-unmerged"}).json()["id"]
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "未合并交付"}).json()

    git = {
        "branch": "ma/overview-unmerged",
        "commit": "abc12345deadbeef",
        "base": "0123456789abcdef",
        "files": ["src/app.ts"],
        "auto_merge": False,
        "merge": {
            "merged": False,
            "skipped": True,
            "reason": "auto_merge_disabled",
            "conflict": False,
            "blocked": False,
            "files": [],
        },
    }
    wb.tasks.update(task["id"], result={"git": git})

    detail = client.get(f"/api/tasks/{task['id']}").json()
    returned = detail["task"]["result"]["git"]

    assert returned["auto_merge"] is False
    assert returned["branch"] == "ma/overview-unmerged"
    assert returned["commit"] == "abc12345deadbeef"
    assert returned["base"] == "0123456789abcdef"
    assert returned["files"] == ["src/app.ts"]

    merge = returned["merge"]
    assert merge["merged"] is False
    assert merge["skipped"] is True
    assert merge["reason"] == "auto_merge_disabled"
    assert merge["conflict"] is False
    assert merge["blocked"] is False
    assert merge["files"] == []
