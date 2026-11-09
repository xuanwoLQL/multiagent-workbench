"""Git 交付硬化回归（本轮缺陷 P0-1 / P0-2 / P0-4 / P1-8 / P2-13 / P2-15 / P2-18）。

覆盖：
- ``commit_worktree`` 在项目**没有 .gitignore** 时也绝不提交 ``.multiagent``；
- ``merge_worktree_branch`` 在基线脏树时阻断（不 merge、不破坏用户改动），
  且 ``.multiagent`` 未跟踪目录不算脏；
- Runner：``git.auto_merge=false`` 时如实记录 skipped 元数据、不合并；
  ``git.keep_worktree=false`` 时合并后回收 worktree 但保留任务分支；
- 任务 result 的成本来源字段（0 不等于免费）；
- WebSocket token 走 ``Sec-WebSocket-Protocol``，不再出现在 URL；
- 旧版控制台默认入口关闭，仅在 MULTIAGENT_UI=legacy 时可访问 /legacy。
"""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from multiagent.api.app import create_api
from multiagent.gitops import (
    commit_worktree,
    create_worktree,
    current_branch,
    is_repo,
    merge_worktree_branch,
    worktree_branch_exists,
)


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path, *, gitignore: bool = False) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# base\n", encoding="utf-8")
    if gitignore:
        (root / ".gitignore").write_text(".multiagent/\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def _b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


# --------------------------------------------------------------------------- P0-2


def test_commit_excludes_control_plane_without_gitignore(tmp_path: Path):
    """没有 .gitignore 的仓库：.multiagent 也不能进入任务提交。"""
    root = tmp_path / "repo"
    _init_repo(root, gitignore=False)
    work = tmp_path / "wt"
    info = create_worktree(root, work, task_id="task-excl1", title="Exclude")

    (work / "feature.txt").write_text("hello\n", encoding="utf-8")
    (work / ".multiagent").mkdir(parents=True, exist_ok=True)
    (work / ".multiagent" / "handoff.json").write_text('{"did": "x"}', encoding="utf-8")
    (work / ".multiagent" / "runtime.sqlite3").write_bytes(b"db")

    committed = commit_worktree(work, "task(excl1): feature", "Bot", "bot@example.com")
    assert committed["sha"], "必须产生真实 commit"
    assert committed["files"] == ["feature.txt"]

    tree = _git(root, "ls-tree", "-r", "--name-only", info["branch"]).stdout.splitlines()
    assert "feature.txt" in tree
    assert not [name for name in tree if name.startswith(".multiagent")], tree

    # 用户自己的 .gitignore 行为不变：被 ignore 的文件仍然不进提交。
    (work / "ignored.tmp").write_text("x", encoding="utf-8")
    (work / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
    second = commit_worktree(work, "task(excl1): ignored", "Bot", "bot@example.com")
    assert second["sha"]
    assert "ignored.tmp" not in second["files"]
    assert ".gitignore" in second["files"]


# --------------------------------------------------------------------------- P0-4


def test_merge_blocks_on_dirty_tree_and_ignores_control_plane(tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    work = tmp_path / "wt"
    info = create_worktree(root, work, task_id="task-dirty1", title="Dirty")
    (work / "feature.txt").write_text("branch\n", encoding="utf-8")
    assert commit_worktree(work, "task(dirty1): feature", "Bot", "bot@example.com")["sha"]

    # 控制面未跟踪目录不算脏：导入项目每次都会写 .multiagent/project.json。
    (root / ".multiagent").mkdir()
    (root / ".multiagent" / "project.json").write_text("{}", encoding="utf-8")
    merged = merge_worktree_branch(root, info["branch"], "merge clean")
    assert merged["merged"] is True, merged
    assert (root / "feature.txt").read_text(encoding="utf-8") == "branch\n"

    # 第二个分支 + 用户改了基线已跟踪文件 → 阻断，不进入 merge。
    (root / "README.md").write_text("# user edit\n", encoding="utf-8")
    work2 = tmp_path / "wt2"
    info2 = create_worktree(root, work2, task_id="task-dirty2", title="Dirty2")
    (work2 / "second.txt").write_text("second\n", encoding="utf-8")
    assert commit_worktree(work2, "task(dirty2): second", "Bot", "bot@example.com")["sha"]

    blocked = merge_worktree_branch(root, info2["branch"], "merge dirty")
    assert blocked["merged"] is False
    assert blocked["blocked"] is True
    assert blocked["conflict"] is False
    assert blocked["reason"] == "dirty_worktree"
    assert "README.md" in blocked["dirty_files"]
    assert blocked["branch"] == info2["branch"]
    assert blocked["base"]
    # 用户改动与仓库状态保持原样，没有半合并。
    assert (root / "README.md").read_text(encoding="utf-8") == "# user edit\n"
    probe = _git(root, "rev-parse", "--verify", "MERGE_HEAD", check=False)
    assert probe.returncode != 0
    assert not (root / "second.txt").exists()


def test_merge_require_clean_false_allows_dirty_tree(tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    work = tmp_path / "wt"
    info = create_worktree(root, work, task_id="task-dirty3", title="Dirty3")
    (work / "feature.txt").write_text("branch\n", encoding="utf-8")
    assert commit_worktree(work, "task(dirty3): feature", "Bot", "bot@example.com")["sha"]
    (root / "README.md").write_text("# local\n", encoding="utf-8")

    merged = merge_worktree_branch(root, info["branch"], "merge opt-out", require_clean=False)
    assert merged["merged"] is True, merged
    assert (root / "feature.txt").exists()


# --------------------------------------------------------------------------- Runner 策略


def _worktree_task(wb, project, **kwargs):
    return wb.tasks.create(
        project["id"],
        kwargs.pop("title", "Feature task"),
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "file_exists", "path": "feature.txt"}],
        **kwargs,
    )


def test_runner_auto_merge_disabled_records_skipped_and_keeps_branch(
    wb, tmp_path: Path, set_plan
):
    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("git-no-merge", root_path=str(root))
    wb.config.policy_raw.setdefault("git", {})["auto_merge"] = False
    set_plan(
        "worker",
        {"files": {"feature.txt": "hello\n"}, "handoff": {"did": "完成"}, "exit": 0},
    )
    task = _worktree_task(wb, project)

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error

    stored = wb.tasks.get(task["id"])
    git_meta = stored["result"]["git"]
    assert git_meta["commit"]
    assert git_meta["auto_merge"] is False
    assert git_meta["merge"]["skipped"] is True
    assert git_meta["merge"]["reason"] == "auto_merge_disabled"
    assert worktree_branch_exists(root, git_meta["branch"])
    assert not (root / "feature.txt").exists(), "关闭自动合并后不应落到基线"
    assert any(
        event.get("kind") == "task.git.merge.skipped"
        for event in wb.board.read_events(project["id"])
    )


def test_runner_keep_worktree_false_removes_worktree_but_keeps_branch(
    wb, tmp_path: Path, set_plan
):
    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("git-prune-wt", root_path=str(root))
    wb.config.policy_raw.setdefault("git", {})["auto_merge"] = True
    wb.config.policy_raw.setdefault("git", {})["keep_worktree"] = False
    set_plan(
        "worker",
        {"files": {"feature.txt": "hello\n"}, "handoff": {"did": "完成"}, "exit": 0},
    )
    task = _worktree_task(wb, project)

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error

    stored = wb.tasks.get(task["id"])
    git_meta = stored["result"]["git"]
    work = wb.isolation.workdir(project["id"], task["id"])
    assert not work.exists(), "keep_worktree=false 时合并成功后应回收工作目录"
    assert git_meta["worktree_removed"] is True
    assert worktree_branch_exists(root, git_meta["branch"]), "任务分支必须保留"
    assert (root / "feature.txt").read_text(encoding="utf-8") == "hello\n"


# --------------------------------------------------------------------------- P2-13


def test_runner_records_cost_source_for_unreported_cli(wb, tmp_path: Path, set_plan):
    root = tmp_path / "plain"
    root.mkdir()
    project = wb.projects.create("cost-demo", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = wb.tasks.create(
        project["id"],
        "Cost task",
        kind="generic",
        agent_id="worker",
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    cost = wb.tasks.get(task["id"])["result"]["cost"]
    assert cost["tokens"] == 0
    assert cost["amount"] == 0
    assert cost["source"] == "unreported", "CLI 没上报成本时必须标记 unreported，不能把 0 当免费"


# --------------------------------------------------------------------------- P2-18


def test_websocket_token_via_subprotocol(wb):
    client = TestClient(create_api(wb, strict=True, token="tok-subprotocol"))
    project = client.post(
        "/api/projects", json={"name": "ws-sub"}, headers={"Authorization": "Bearer tok-subprotocol"}
    ).json()
    protocol = f"multiagent.token.{_b64url('tok-subprotocol')}"

    with client.websocket_connect(
        f"/ws?project={project['id']}",
        subprotocols=[protocol],
        headers={"Origin": "http://testserver"},
    ) as ws:
        assert ws.accepted_subprotocol == protocol
        message = ws.receive_json()
        assert message["type"] in ("events", "progress", "run_status", "stream.snapshot")

    # 错误 token 仍然被拒绝；旧 ?token= 客户端继续可用（兼容，不写进 URL 的是前端）。
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            f"/ws?project={project['id']}",
            subprotocols=[f"multiagent.token.{_b64url('wrong')}"],
            headers={"Origin": "http://testserver"},
        ):
            pass

    with client.websocket_connect(
        f"/ws?project={project['id']}&token=tok-subprotocol",
        headers={"Origin": "http://testserver"},
    ) as ws:
        assert ws.receive_json()["type"] in ("events", "progress", "run_status", "stream.snapshot")


# --------------------------------------------------------------------------- P2-15


def test_legacy_ui_entry_is_closed_by_default(wb, monkeypatch):
    monkeypatch.delenv("MULTIAGENT_UI", raising=False)
    client = TestClient(create_api(wb, env={}))
    assert client.get("/static/index.html").status_code == 404
    assert client.get("/legacy").status_code == 404
    # 根路径仍是 v2 构建产物
    root = client.get("/")
    assert root.status_code == 200
    assert 'id="root"' in root.text


def test_legacy_ui_entry_available_only_with_env(wb, monkeypatch):
    monkeypatch.setenv("MULTIAGENT_UI", "legacy")
    client = TestClient(create_api(wb, env={}))
    res = client.get("/legacy")
    assert res.status_code == 200
    assert "text/html" in res.headers.get("content-type", "")
    assert client.get("/static/index.html").status_code == 200
    # / 永远优先 v2 构建产物：legacy 是回退入口，不是默认入口。
    root = client.get("/")
    assert 'id="root"' in root.text


# --------------------------------------------------------------------------- P0-5 降级可见


def test_control_plane_changes_do_not_degrade_worktree(wb, tmp_path: Path):
    """即使项目把 .multiagent 提交进仓库，它的运行时改动也不算「脏工作树」。"""
    root = tmp_path / "repo"
    _init_repo(root)
    (root / ".multiagent").mkdir()
    (root / ".multiagent" / "project.json").write_text("{}", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "track control plane")
    (root / ".multiagent" / "project.json").write_text('{"touched": true}', encoding="utf-8")

    project = wb.projects.create("control-plane", root_path=str(root))
    task = wb.tasks.create(
        project["id"],
        "Worktree task",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "always"}],
    )
    work, info = wb.isolation.prepare_ex(project, wb.tasks.get(task["id"]))
    assert info["mode"] == "worktree", info
    assert info["degraded"] is False, info
    assert is_repo(work)


def test_runner_records_isolation_degradation(wb, tmp_path: Path, set_plan):
    """worktree 因脏树降级为 copy 时，原因必须进入 result 与事件流。"""
    root = tmp_path / "repo"
    _init_repo(root)
    (root / "README.md").write_text("# uncommitted user edit\n", encoding="utf-8")
    project = wb.projects.create("degrade-demo", root_path=str(root))
    set_plan(
        "worker",
        {"files": {"out.txt": "ok\n"}, "handoff": {"did": "完成"}, "exit": 0},
    )
    task = wb.tasks.create(
        project["id"],
        "Degraded task",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )

    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error

    stored = wb.tasks.get(task["id"])
    info = stored["result"]["isolation"]
    assert info["requested_mode"] == "worktree"
    assert info["mode"] == "copy"
    assert info["degraded"] is True
    assert info["degrade_code"] == "dirty_workspace"
    assert "未提交" in info["degrade_reason"]
    assert (root / "out.txt").read_text(encoding="utf-8") == "ok\n"
    events = [event for event in wb.board.read_events(project["id"]) if event.get("kind") == "task.isolation.degraded"]
    assert events, "降级必须写事件，不能只写日志"
    assert events[0]["data"]["degrade_code"] == "dirty_workspace"


# --------------------------------------------------------------------------- P0-3 人工合并回写


def test_discard_branch_persists_discarded_state(wb, tmp_path: Path):
    """放弃任务分支后，任务与审阅 API 都必须显示 discarded，而不是继续劝用户合并。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("discard-demo", root_path=str(root))
    task = wb.tasks.create(
        project["id"],
        "Discard task",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "always"}],
    )
    work = wb.isolation.prepare(project, wb.tasks.get(task["id"]))
    (work / "abandoned.txt").write_text("wip\n", encoding="utf-8")
    sha = commit_worktree(work, "task(discard): wip", "Bot", "bot@example.com")["sha"]
    assert sha
    branch = current_branch(work)
    wb.tasks.update(
        task["id"],
        result={"git": {"branch": branch, "commit": sha, "base": _git(root, "rev-parse", "HEAD").stdout.strip()}},
    )

    client = TestClient(create_api(wb))
    res = client.post(f"/api/tasks/{task['id']}/discard-worktree", json={"delete_branch": True})
    assert res.status_code == 200, res.text
    assert res.json()["branch_deleted"] is True

    stored = wb.tasks.get(task["id"])
    merge = stored["result"]["git"]["merge"]
    assert merge["discarded"] is True
    assert merge["merged"] is False

    info = client.get(f"/api/tasks/{task['id']}/worktree").json()
    assert info["merge_status"] == "discarded"
    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["merge_status"] == "discarded"
    assert workspace["can_sync"] is False
    assert "放弃" in (workspace["sync_blocked_reason"] or "")
    assert not worktree_branch_exists(root, branch)


def test_manual_merge_persists_result_for_ui(wb, tmp_path: Path):
    """人工 merge 成功后必须回写 task.result.git.merge，否则 UI 仍显示未合并。"""
    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("manual-merge", root_path=str(root))
    task = wb.tasks.create(
        project["id"],
        "Manual merge",
        kind="generic",
        agent_id="worker",
        isolation="worktree",
        acceptance=[{"type": "always"}],
    )
    work = wb.isolation.prepare(project, wb.tasks.get(task["id"]))
    (work / "manual.txt").write_text("manual\n", encoding="utf-8")
    assert commit_worktree(work, "task(manual): merge", "Bot", "bot@example.com")["sha"]

    client = TestClient(create_api(wb))
    res = client.post(f"/api/tasks/{task['id']}/merge")
    assert res.status_code == 200, res.text
    assert res.json()["merged"] is True

    stored = wb.tasks.get(task["id"])
    merge = stored["result"]["git"]["merge"]
    assert merge["merged"] is True
    assert merge["source"] == "manual"
    assert merge["branch"] == stored["result"]["git"]["branch"]
    assert merge["base"]

    info = client.get(f"/api/tasks/{task['id']}/worktree").json()
    assert info["merge_status"] == "merged"
    assert info["git"]["merge"]["merged"] is True
    # /workspace 与 /worktree 必须说同一种话：审阅页据此隐藏「同步」。
    workspace = client.get(f"/api/tasks/{task['id']}/workspace").json()
    assert workspace["merge_status"] == "merged"
    assert workspace["can_sync"] is False
    assert "已合并" in (workspace["sync_blocked_reason"] or "")
    # 人工合并成功后，工作区根目录确实拿到了文件。
    assert (root / "manual.txt").read_text(encoding="utf-8") == "manual\n"


def test_runner_never_reports_merged_event_on_failed_merge(
    wb, tmp_path: Path, set_plan, monkeypatch
):
    """Git 命令失败（非脏树/非冲突）时不得伪造 task.git.merged 事件。"""
    from multiagent.orchestrator import runner as runner_mod

    root = tmp_path / "repo"
    _init_repo(root)
    project = wb.projects.create("git-merge-fail", root_path=str(root))
    wb.config.policy_raw.setdefault("git", {})["auto_merge"] = True
    set_plan(
        "worker",
        {"files": {"feature.txt": "hello\n"}, "handoff": {"did": "完成"}, "exit": 0},
    )
    monkeypatch.setattr(
        runner_mod,
        "merge_worktree_branch",
        lambda *args, **kwargs: {
            "merged": False,
            "blocked": False,
            "conflict": False,
            "reason": "merge_failed",
            "sha": None,
            "files": [],
            "branch": "ma/x",
            "base": "deadbeef",
            "error": "simulated git failure",
        },
    )
    task = _worktree_task(wb, project)
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "blocked", outcome
    assert "合并失败" in (outcome.error or "")

    kinds = [event.get("kind") for event in wb.board.read_events(project["id"])]
    assert "task.git.merge.failed" in kinds
    assert "task.git.merged" not in kinds
    stored = wb.tasks.get(task["id"])
    assert stored["result"]["git"]["merge"]["merged"] is False
    assert not (root / "feature.txt").exists()


def test_v2_welcome_page_does_not_expose_legacy_entry():
    """默认 UI 不能放一个点了必然 404 的旧版入口链接。"""
    from tests.conftest import REPO_ROOT  # type: ignore

    source = (
        REPO_ROOT / "webapp" / "src" / "features" / "onboarding" / "WelcomeView.tsx"
    ).read_text(encoding="utf-8")
    assert "/static/index.html" not in source
    assert "旧版控制台" not in source


def test_commit_succeeds_when_project_gitignore_ignores_control_plane(tmp_path: Path):
    """真实 Atlas 场景：项目 .gitignore 忽略 .multiagent 时，git add 不能因 exclude
    pathspec 把忽略路径当成显式添加而失败（回归：GitError 只暴露一条 CRLF warning）。
    """
    root = tmp_path / "repo"
    _init_repo(root)
    (root / ".gitignore").write_text(".multiagent/\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "ignore control plane")

    work = tmp_path / "wt"
    info = create_worktree(root, work, task_id="task-atlas1", title="Atlas")
    (work / "docs").mkdir()
    (work / "docs" / "workbench-regression.md").write_text("hello\n", encoding="utf-8")
    (work / ".multiagent").mkdir()
    (work / ".multiagent" / "handoff.json").write_text('{"did":"x"}', encoding="utf-8")

    committed = commit_worktree(work, "task(atlas1): doc", "Bot", "bot@example.com")
    assert committed["sha"], "有 .gitignore 忽略控制面时也必须能提交"
    assert committed["files"] == ["docs/workbench-regression.md"], committed
    tree = _git(root, "ls-tree", "-r", "--name-only", info["branch"]).stdout.splitlines()
    assert "docs/workbench-regression.md" in tree
    assert not [name for name in tree if ".multiagent" in name.split("/")], tree


def test_commit_unstages_nested_control_plane_without_gitignore(tmp_path: Path):
    root = tmp_path / "repo"
    _init_repo(root)
    work = tmp_path / "wt"
    info = create_worktree(root, work, task_id="task-nested1", title="Nested")
    for rel in (".multiagent/db", "sub/.multiagent/state.json"):
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("secret", encoding="utf-8")
    (work / "docs").mkdir()
    (work / "docs" / "out.md").write_text("ok", encoding="utf-8")

    committed = commit_worktree(work, "task(nested1): doc", "Bot", "bot@example.com")
    assert committed["sha"]
    assert committed["files"] == ["docs/out.md"], committed
    tree = _git(root, "ls-tree", "-r", "--name-only", info["branch"]).stdout.splitlines()
    assert not [name for name in tree if ".multiagent" in name.split("/")], tree


def test_new_attempt_clears_stale_git_metadata(wb, tmp_path: Path, set_plan):
    """重试/降级到 copy 时，上一次尝试的 git 元数据必须清空，不能指向旧分支。"""
    root = tmp_path / "plain"
    root.mkdir()
    project = wb.projects.create("stale-git", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = wb.tasks.create(
        project["id"],
        "Stale git retry",
        kind="generic",
        agent_id="worker",
        isolation="copy",
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )
    wb.tasks.update(
        task["id"],
        result={"git": {"branch": "ma/old-branch", "merge": {"merged": True, "sha": "deadbeef"}}},
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    stored = wb.tasks.get(task["id"])
    assert "git" not in stored["result"], stored["result"]


def test_shared_mode_does_not_reap_processes(wb, tmp_path: Path, set_plan, monkeypatch):
    """shared 模式 workdir 就是项目根：默认不自动回收用户自己的 dev server。"""
    from multiagent.orchestrator import runner as runner_mod

    reaps: list[bool] = []

    class _Audit:
        @classmethod
        def begin(cls):
            return cls()

        def finish(self, workdir, *, reap: bool = True, **_kwargs):
            reaps.append(reap)
            return {
                "supported": True,
                "started": [],
                "reaped": [],
                "warnings": [],
                "before_count": 0,
                "after_count": 0,
            }

    monkeypatch.setattr(runner_mod, "ProcessAudit", _Audit)
    root = tmp_path / "shared"
    root.mkdir()
    project = wb.projects.create("shared-audit", root_path=str(root))
    set_plan("worker", {"files": {"out.txt": "ok"}, "handoff": {"did": "完成"}, "exit": 0})
    task = wb.tasks.create(
        project["id"],
        "Shared audit",
        kind="generic",
        agent_id="worker",
        isolation="shared",
        write_scope=["**"],
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome.error
    assert reaps == [False], "shared 模式不得自动回收进程"
