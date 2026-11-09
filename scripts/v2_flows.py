"""Real-browser interaction E2E for the MultiAgent v2 web UI (7 flows).

This script is deliberately separate from ``scripts/v2_smoke.py`` (the
navigation smoke test): it drives the real UI through a throw-away backend, a
throw-away ``MULTIAGENT_HOME`` and a throw-away git repository, then asserts the
delivery / merge / review / timeout / duplicate-project flows end to end.

It reuses the CDP client, recorder and low-level helpers from ``v2_smoke``
instead of copying them, so transport fixes land in both scripts.

Flows
-----
1. ``task-merged``        任务详情「已合并」徽章 + 无「同步到工作区」入口
2. ``merge-conflict``     合并冲突面板 + 重试合并返回 409 后页面不崩溃
3. ``extend-timeout``     更多菜单 → 延长超时：非法值被拒、合法值落库
4. ``settings-delivery``  设置「交付与合并」：切换、保存、刷新保持、切回
5. ``review-states``      审阅页同时展示已合并/待合并/冲突/已放弃四类状态
6. ``new-task-advanced``  新建任务高级选项：timeout/max_attempts 落库 + 非法值内联校验
7. ``duplicate-projects`` 历史重复项目横幅、筛选、打开成员与恢复列表

The seventh flow needs historical data that the current API refuses to create
(``POST /api/projects`` now answers 409 for an already-registered directory).
Before the backend boots, :func:`seed_duplicate_projects` opens the throw-away
``MULTIAGENT_HOME`` with ``multiagent.app.Workbench`` and inserts two project
rows pointing at the same directory directly through ``db.insert``.  The rows
get deliberately late ``created_at`` values so the imported project stays first
in ``GET /api/projects`` (the UI selects the first project on boot), which keeps
the six existing flows on exactly the same seed data and assertions.

All screenshots are written to ``.e2e/ui-v2/flows/``.  The script exits
non-zero when any flow fails or when the browser reports an uncaught exception
or a console error (the same hard-failure rule as ``v2_smoke``).

Usage
-----
    .venv/Scripts/python.exe scripts/v2_flows.py
    .venv/Scripts/python.exe scripts/v2_flows.py --timeout 30 --keep-temp
    .venv/Scripts/python.exe scripts/v2_flows.py --help

Only the standard library plus ``websockets`` is imported at module import
time; the duplicate-project seed step lazily imports ``multiagent.app`` to
write the historical rows before the backend process starts (the fake Agent is
spawned by the backend).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import v2_smoke  # noqa: E402
from v2_smoke import (  # noqa: E402
    CDP,
    VIEWPORT,
    Failure,
    Recorder,
    configure_edge,
    free_port,
    http_json,
    js_click_nav,
    log,
    wait_cdp,
    wait_health,
    wait_network_quiet,
    wait_ready,
)

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / ".e2e" / "ui-v2" / "flows"

FLOW_NAMES = (
    "task-merged",
    "merge-conflict",
    "extend-timeout",
    "settings-delivery",
    "review-states",
    "new-task-advanced",
    "duplicate-projects",
    "missing-branch",
)

FLOW_TITLES = {
    "task-merged": "任务详情：已合并",
    "merge-conflict": "合并冲突面板",
    "extend-timeout": "延长超时",
    "settings-delivery": "设置：交付与合并",
    "review-states": "审阅四状态",
    "new-task-advanced": "新建任务高级选项",
    "duplicate-projects": "历史重复项目",
    "missing-branch": "分支缺失与重建",
}

TITLES = {
    "merged": "已合并任务 Alpha",
    "unmerged": "未合并任务 Beta",
    "conflict": "冲突任务 Gamma",
    "discarded": "已放弃任务 Delta",
    "missing": "缺失分支任务 Epsilon",
}
NEW_TASK_TITLE = "E2E 高级选项任务"
NEW_TASK_TEXT = NEW_TASK_TITLE + "\n验证高级选项字段是否落库。"

#: 历史重复项目的种子名称（直接写 projects 表，不经过会返回 409 的 API）。
DUPLICATE_PROJECT_NAMES = ("历史重复项目甲", "历史重复项目乙")


# --------------------------------------------------------------------------- #
# temporary home / policy / agents
# --------------------------------------------------------------------------- #


def default_policy(auto_merge: bool = True) -> dict[str, Any]:
    return {
        "version": 1,
        "concurrency": {"global": 2, "per_project": 1},
        "default_timeout_sec": 1800,
        "task_defaults": {"timeout_sec": 1800, "max_attempts": 3},
        "retry": {"backoff_seconds": 0, "timeout_multiplier": 1.5, "max_timeout_sec": 3600},
        "timeout": {"warning_seconds": 300},
        "workspace": {"ignore_file": ".multiagentignore", "excludes": []},
        "context_budget_chars": 8000,
        "isolation": {
            "default": "worktree",
            "sync_back_on_accept": True,
            "block_conflict": True,
            "revert_out_of_scope": True,
            "resume_partial_on_retry": True,
        },
        "git": {
            "auto_merge": auto_merge,
            "require_clean_tree": True,
            "keep_worktree": True,
            "worktree_retention_days": 14,
            "author_name": "MultiAgent",
            "author_email": "multiagent@local",
        },
        "processes": {
            "audit_enabled": True,
            "reap_orphans": True,
            "min_timeout_sec": 5,
        },
        "dangerous_ops": {
            "require_confirmation": [
                "delete",
                "overwrite_existing_artifact",
                "modify_project_config",
            ],
            "auto_approve": False,
            "protect_paths": [".multiagent", "config", ".git"],
        },
        "review_gate": {"memory_requires_approval": True, "default_entry_status": "approved"},
        "snapshot": {"on_task_start": False, "keep": 20},
        "kinds": {
            "generic": {
                "isolation": "worktree",
                "read_only": False,
                "acceptance_ref": "file-exists",
                "timeout_sec": 1800,
                "max_attempts": 3,
            },
            "code": {
                "isolation": "worktree",
                "read_only": False,
                "acceptance_ref": "file-changed",
                "timeout_sec": 1800,
                "max_attempts": 3,
            },
            "doc": {
                "isolation": "worktree",
                "read_only": False,
                "acceptance_ref": "file-exists",
                "timeout_sec": 1800,
                "max_attempts": 3,
            },
        },
    }


def fake_cli_agent(plan_file: Path) -> dict[str, Any]:
    """与 tests/conftest.py 的 _fake_cli 保持一致（protocol=cli）。"""
    return {
        "id": "worker",
        "label": "Worker",
        "protocol": "cli",
        "cli": {
            "command": sys.executable,
            "args": ["-m", "multiagent.testing.fake_agent"],
            "prompt_mode": "stdin",
        },
        "env": {"PYTHONPATH": str(REPO), "FAKE_PLAN_FILE": str(plan_file)},
        "roles": ["worker", "implementer", "writer"],
        "strengths": ["code", "doc"],
        "can_write": True,
        "enabled": True,
    }


def write_config(home: Path, plan_file: Path, *, auto_merge: bool = True) -> None:
    config_dir = home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    agents = {
        "version": 1,
        "defaults": {
            "timeout_sec": 1800,
            "quota": {"max_invocations": 1000, "max_seconds": 100000, "max_cost": 1000},
        },
        "agents": [
            fake_cli_agent(plan_file),
            {"id": "human", "label": "人工", "protocol": "human", "enabled": True},
        ],
    }
    (config_dir / "agents.json").write_text(
        json.dumps(agents, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (config_dir / "policy.json").write_text(
        json.dumps(default_policy(auto_merge), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    plan_file.write_text("{}", encoding="utf-8")


def write_plan(plan_file: Path, files: dict[str, str]) -> None:
    plan_file.write_text(
        json.dumps({"files": files, "handoff": {"did": "E2E fake agent"}}, ensure_ascii=False),
        encoding="utf-8",
    )


def start_backend(home: Path, port: int, log_path: Path) -> tuple[subprocess.Popen[bytes], Any]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MULTIAGENT_")}
    env["MULTIAGENT_HOME"] = str(home)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    log_file = log_path.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-m", "multiagent", "serve", "--port", str(port)],
        cwd=str(REPO),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return process, log_file


def stop_process(process: subprocess.Popen[Any], name: str) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            log("  !! " + name + " 未能终止")
    except OSError:
        pass


def force_remove(function: Callable[..., Any], path: str, exc_info: Any) -> None:
    """git 对象文件在 Windows 上是只读的；rmtree 前先补写权限。"""
    try:
        os.chmod(path, 0o700)
        function(path)
    except OSError:
        pass


def remove_tree(root: Path) -> bool:
    for _ in range(8):
        if not root.exists():
            return True
        shutil.rmtree(root, onerror=force_remove)
        if not root.exists():
            return True
        time.sleep(0.4)
    return not root.exists()


def log_tail(path: Path, lines: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(没有后端日志)"
    return "\n".join(text.splitlines()[-lines:])


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #


def api_call(
    base: str,
    method: str,
    path: str,
    body: Any | None = None,
    *,
    timeout: float = 60.0,
) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8")) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", errors="replace")
        return exc.code, payload


def api_ok(
    base: str,
    method: str,
    path: str,
    body: Any | None = None,
    *,
    timeout: float = 60.0,
    label: str = "",
) -> Any:
    status, payload = api_call(base, method, path, body, timeout=timeout)
    if status != 200:
        raise RuntimeError(
            (label or path) + " 失败：HTTP " + str(status) + " " + str(payload)[:800]
        )
    return payload


def get_task(base: str, tid: str) -> dict[str, Any]:
    payload = api_ok(base, "GET", "/api/tasks/" + tid, label="读取任务 " + tid)
    return payload["task"]


def read_policy(base: str) -> dict[str, Any]:
    try:
        payload = api_ok(base, "GET", "/api/config/files/policy", label="读取 policy.json")
    except RuntimeError:
        return {}
    content = payload.get("content") if isinstance(payload, dict) else None
    return content if isinstance(content, dict) else {}


def set_auto_merge(base: str, value: bool) -> None:
    policy = read_policy(base)
    if not policy:
        raise RuntimeError("无法读取 policy.json，不能切换 auto_merge")
    git = policy.get("git") if isinstance(policy.get("git"), dict) else {}
    git["auto_merge"] = bool(value)
    policy["git"] = git
    api_ok(
        base,
        "PUT",
        "/api/config/files/policy",
        {"content": policy},
        label="写入 auto_merge=" + str(value),
    )


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# base\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "init")


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #


def release_home_logging(home: Path) -> None:
    """释放 Workbench 打开的临时 home 日志句柄。

    ``Workbench`` 构造时会调用 ``configure_logging``，把 ``multiagent`` logger
    的文件 handler 指向 ``<home>/.multiagent/logs/multiagent.log``。Windows 上
    只要脚本进程还持有该句柄，退出时 ``shutil.rmtree`` 就删不掉临时目录。
    后端是独立进程，移除脚本进程内的 handler 不影响它。
    """
    import logging

    home_marker = str((home / ".multiagent").resolve()).lower()
    logger = logging.getLogger("multiagent")
    for handler in list(logger.handlers):
        path = str(getattr(handler, "baseFilename", "") or "")
        try:
            inside = bool(path) and str(Path(path).resolve()).lower().startswith(home_marker)
        except OSError:
            inside = False
        if not inside:
            continue
        logger.removeHandler(handler)
        try:
            handler.flush()
            handler.close()
        except (OSError, ValueError):
            pass


def seed_duplicate_projects(home: Path, shared_dir: Path) -> None:
    """在启动后端**之前**直接写库，模拟历史遗留的「同一目录多个项目」。

    后端对 ``POST /api/projects`` / ``/api/projects/import`` 做了
    ``normalize_root`` 去重，同一目录会返回 409，因此这里不能走 API：
    用 ``multiagent.app.Workbench`` 打开临时 home，直接向 ``projects`` 表
    ``db.insert`` 两条 root_path 完全相同的记录，然后关闭连接。后端随后以
    同一个 ``MULTIAGENT_HOME`` 启动，列表接口会原样返回这两条历史数据。

    两条记录的 ``created_at`` 故意晚于 ``seed_environment`` 导入的主项目：
    ``GET /api/projects`` 按 created_at 排序，前端启动时默认选中列表第一项，
    这样前 6 条流程仍然作用在主项目上，种子数据与断言完全不变。
    """
    log("\n== seed: historical duplicate projects (direct DB, before backend) ==")
    sys.path.insert(0, str(REPO))
    from multiagent.app import Workbench  # noqa: PLC0415 - 延迟导入，--help 不加载应用

    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "README.md").write_text("# legacy shared workspace\n", encoding="utf-8")
    root_path = str(shared_dir.resolve())
    workbench = None
    rows: list[Any] = []
    try:
        workbench = Workbench.open(home, init=False)
        for index, name in enumerate(DUPLICATE_PROJECT_NAMES):
            # 未来时间戳 = 列表排在主项目之后；不影响任何既有流程的默认选中。
            stamp = "2999-01-01T00:00:0" + str(index) + ".000Z"
            workbench.db.insert(
                "projects",
                {
                    "id": "proj-e2e-dup-" + str(index + 1),
                    "name": name,
                    "label": name,
                    "root_path": root_path,
                    "origin_path": root_path,
                    "kind": "files",
                    "readonly": 0,
                    "config": "{}",
                    "created_at": stamp,
                    "updated_at": stamp,
                },
            )
        rows = workbench.db.query(
            "SELECT id, name, root_path FROM projects WHERE root_path = ?",
            (root_path,),
        )
    finally:
        if workbench is not None:
            workbench.close()
        # Workbench 会给脚本进程挂上临时 home 的日志 handler；Windows 上必须
        # 在 rmtree 之前关掉，否则 home/.multiagent/logs 永远删不掉。
        release_home_logging(home)
    if len(rows) != len(DUPLICATE_PROJECT_NAMES):
        raise RuntimeError(
            "历史重复项目播种失败：期望 "
            + str(len(DUPLICATE_PROJECT_NAMES))
            + " 条，实际 "
            + str(len(rows))
        )
    log("  " + str(len(rows)) + " duplicate projects -> " + root_path)


@dataclass
class SeedData:
    home: Path
    repo: Path
    plan_file: Path
    project_id: str
    project_name: str = ""
    tasks: dict[str, str] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)
    merge_shas: dict[str, str] = field(default_factory=dict)
    base_shas: dict[str, str] = field(default_factory=dict)


def create_task(base: str, pid: str, title: str, output: str) -> dict[str, Any]:
    payload = {
        "title": title,
        "kind": "generic",
        "agent_id": "worker",
        "isolation": "worktree",
        "outputs": [output],
        "acceptance": [{"type": "file_exists", "path": output}],
    }
    task = api_ok(base, "POST", "/api/projects/" + pid + "/tasks", payload, label="创建任务")
    if not task.get("id"):
        raise RuntimeError("创建任务没有返回 id：" + str(task))
    return task


def run_task(base: str, tid: str) -> dict[str, Any]:
    status, outcome = api_call(
        base, "POST", "/api/tasks/" + tid + "/run", {"wait": 300}, timeout=330
    )
    if status != 200:
        raise RuntimeError("运行任务失败：HTTP " + str(status) + " " + str(outcome)[:800])
    if not isinstance(outcome, dict) or outcome.get("status") != "success":
        raise RuntimeError("任务未成功：" + str(outcome)[:1200])
    return outcome


def task_git(task: dict[str, Any]) -> dict[str, Any]:
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    git_meta = result.get("git") if isinstance(result.get("git"), dict) else {}
    return git_meta


def task_merge(task: dict[str, Any]) -> dict[str, Any]:
    merge = task_git(task).get("merge")
    return merge if isinstance(merge, dict) else {}


def seed_environment(base: str, home: Path, repo: Path, plan_file: Path) -> SeedData:
    log("\n== seed: temporary git repository + worktree tasks ==")
    init_repo(repo)
    project = api_ok(base, "POST", "/api/projects/import", {"path": str(repo)}, label="导入项目")
    pid = str(project.get("id") or "")
    if not pid:
        raise RuntimeError("导入项目没有返回 id：" + str(project))
    data = SeedData(
        home=home,
        repo=repo,
        plan_file=plan_file,
        project_id=pid,
        project_name=str(project.get("label") or project.get("name") or ""),
    )
    log("  project id: " + pid)

    # 1) merged task: auto_merge=true (policy default)
    write_plan(plan_file, {"feature.txt": "feature one content"})
    task = create_task(base, pid, TITLES["merged"], "feature.txt")
    data.tasks["merged"] = str(task["id"])
    run_task(base, str(task["id"]))
    merged_task = get_task(base, str(task["id"]))
    merged = task_merge(merged_task)
    if merged.get("merged") is not True:
        raise RuntimeError("任务 1 未自动合并：" + json.dumps(merged, ensure_ascii=False)[:1200])
    if not (repo / "feature.txt").is_file():
        raise RuntimeError("任务 1 合并后基线缺少 feature.txt")
    data.branches["merged"] = str(task_git(merged_task).get("branch") or merged.get("branch") or "")
    data.merge_shas["merged"] = str(merged.get("sha") or task_git(merged_task).get("commit") or "")
    data.base_shas["merged"] = str(task_git(merged_task).get("base") or "")
    log("  1 merged: " + data.branches["merged"] + " @" + data.merge_shas["merged"][:8])

    # 2) unmerged task: auto_merge=false
    set_auto_merge(base, False)
    write_plan(plan_file, {"feature2.txt": "feature two content"})
    task = create_task(base, pid, TITLES["unmerged"], "feature2.txt")
    data.tasks["unmerged"] = str(task["id"])
    run_task(base, str(task["id"]))
    unmerged_task = get_task(base, str(task["id"]))
    skipped = task_merge(unmerged_task)
    if skipped.get("skipped") is not True:
        raise RuntimeError(
            "任务 2 未记录 skipped：" + json.dumps(skipped, ensure_ascii=False)[:1200]
        )
    data.branches["unmerged"] = str(
        task_git(unmerged_task).get("branch") or skipped.get("branch") or ""
    )
    data.base_shas["unmerged"] = str(task_git(unmerged_task).get("base") or "")
    log("  2 unmerged: " + data.branches["unmerged"])

    # 3) conflict task: branch committed, baseline then edits the same file.
    write_plan(plan_file, {"conflict.txt": "task branch conflict version"})
    task = create_task(base, pid, TITLES["conflict"], "conflict.txt")
    data.tasks["conflict"] = str(task["id"])
    run_task(base, str(task["id"]))
    conflict_task = get_task(base, str(task["id"]))
    data.branches["conflict"] = str(
        task_git(conflict_task).get("branch") or task_merge(conflict_task).get("branch") or ""
    )
    data.base_shas["conflict"] = str(task_git(conflict_task).get("base") or "")
    (repo / "conflict.txt").write_text("baseline conflict version", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baseline edits conflict.txt")
    status, body = api_call(base, "POST", "/api/tasks/" + str(task["id"]) + "/merge", {})
    if status != 409:
        raise RuntimeError("期望合并冲突 409，实际 HTTP " + str(status) + " " + str(body)[:800])
    if not isinstance(body, dict) or body.get("conflict") is not True:
        raise RuntimeError("409 响应缺少 conflict=true：" + str(body)[:800])
    if "conflict.txt" not in (body.get("files") or []):
        raise RuntimeError("409 响应缺少冲突文件 conflict.txt：" + str(body)[:800])
    # 冲突面板展示的是「人工合并时的基线 HEAD」，不是任务创建时的 base。
    merge_base = str(body.get("base") or "")
    if not merge_base:
        raise RuntimeError("409 响应缺少合并基线 base：" + str(body)[:800])
    data.base_shas["conflict"] = merge_base
    conflict_task = get_task(base, str(task["id"]))
    merge = task_merge(conflict_task)
    if merge.get("conflict") is not True:
        raise RuntimeError(
            "任务 3 的 result.git.merge.conflict 不是 true："
            + json.dumps(merge, ensure_ascii=False)[:1200]
        )
    log("  3 conflict: " + data.branches["conflict"])

    # 4) discarded task
    write_plan(plan_file, {"discard.txt": "discard me"})
    task = create_task(base, pid, TITLES["discarded"], "discard.txt")
    data.tasks["discarded"] = str(task["id"])
    run_task(base, str(task["id"]))
    discard_task = get_task(base, str(task["id"]))
    data.branches["discarded"] = str(
        task_git(discard_task).get("branch") or task_merge(discard_task).get("branch") or ""
    )
    data.base_shas["discarded"] = str(task_git(discard_task).get("base") or "")
    status, body = api_call(
        base,
        "POST",
        "/api/tasks/" + str(task["id"]) + "/discard-worktree",
        {"delete_branch": True},
    )
    if status != 200:
        raise RuntimeError("放弃分支失败：HTTP " + str(status) + " " + str(body)[:800])
    discard_task = get_task(base, str(task["id"]))
    merge = task_merge(discard_task)
    if merge.get("discarded") is not True:
        raise RuntimeError(
            "任务 4 的 result.git.merge.discarded 不是 true："
            + json.dumps(merge, ensure_ascii=False)[:1200]
        )
    log("  4 discarded: " + data.branches["discarded"])

    # 5) missing branch task: keep a successful worktree task, then delete only
    # the branch ref so /merge must report missing_branch instead of merging.
    write_plan(plan_file, {"missing.txt": "rebuilt from recorded commit"})
    task = create_task(base, pid, TITLES["missing"], "missing.txt")
    data.tasks["missing"] = str(task["id"])
    run_task(base, str(task["id"]))
    missing_task = get_task(base, str(task["id"]))
    missing_git = task_git(missing_task)
    data.branches["missing"] = str(
        missing_git.get("branch") or task_merge(missing_task).get("branch") or ""
    )
    data.base_shas["missing"] = str(
        missing_git.get("base") or task_merge(missing_task).get("base") or ""
    )
    if not data.branches["missing"]:
        raise RuntimeError("任务 5 未记录任务分支：" + json.dumps(missing_git, ensure_ascii=False))
    if not data.base_shas["missing"]:
        raise RuntimeError("任务 5 未记录基线：" + json.dumps(missing_git, ensure_ascii=False))
    workdir_raw = str(missing_task.get("workdir") or missing_git.get("worktree_path") or "")
    workdir = Path(workdir_raw)
    if not workdir_raw or not workdir.is_dir():
        raise RuntimeError("任务 5 的 worktree 目录不存在：" + workdir_raw)
    git(repo, "update-ref", "-d", "refs/heads/" + data.branches["missing"])
    remaining = git(repo, "branch", "--list", data.branches["missing"]).stdout.strip()
    if remaining:
        raise RuntimeError("任务 5 的分支 ref 未删除：" + remaining)
    if not workdir.is_dir():
        raise RuntimeError("删除 ref 后 worktree 目录不应被移除：" + str(workdir))
    log("  5 missing-branch: " + data.branches["missing"] + " (ref deleted, worktree kept)")

    log("  seed complete: 5 tasks")
    return data


# --------------------------------------------------------------------------- #
# CDP extension: capture request bodies + response statuses
# --------------------------------------------------------------------------- #


class FlowCDP(CDP):
    def __init__(self, ws: Any, recorder: Recorder) -> None:
        super().__init__(ws, recorder)
        self.requests: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []
        self._methods: dict[str, str] = {}
        self._urls: dict[str, str] = {}

    def _on_event(self, method: str, params: dict[str, Any]) -> None:
        if method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            request_id = str(params.get("requestId") or "")
            self._methods[request_id] = str(request.get("method") or "")
            self._urls[request_id] = str(request.get("url") or "")
            post_data = request.get("postData")
            if post_data:
                self.requests.append(
                    {
                        "method": request.get("method"),
                        "url": request.get("url"),
                        "postData": post_data,
                    }
                )
                if len(self.requests) > 300:
                    del self.requests[:150]
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            request_id = str(params.get("requestId") or "")
            self.responses.append(
                {
                    "method": self._methods.pop(request_id, ""),
                    "url": response.get("url") or self._urls.pop(request_id, ""),
                    "status": int(response.get("status") or 0),
                }
            )
            if len(self.responses) > 300:
                del self.responses[:150]
        super()._on_event(method, params)


# --------------------------------------------------------------------------- #
# JS snippets (single line, no regex escaping is required)
# --------------------------------------------------------------------------- #


def js_text(selector: str) -> str:
    return (
        "(() => { const el = document.querySelector(%s); "
        "return el ? (el.textContent || '').trim() : ''; })()"
    ) % json.dumps(selector)


def js_value(selector: str) -> str:
    return "(() => { const el = document.querySelector(%s); return el ? el.value : null; })()" % json.dumps(
        selector
    )


def js_click(selector: str, text: str, exact: bool = False, root: str | None = None) -> str:
    root_expr = "document" if root is None else "document.querySelector(%s)" % json.dumps(root)
    return (
        "(() => { const root = %(root)s; if (!root) return 'missing-root'; "
        "const want = %(want)s; const nodes = [...root.querySelectorAll(%(selector)s)]; "
        "const el = nodes.find((node) => { const value = (node.textContent || '').trim(); "
        "return %(compare)s; }); if (!el) return 'missing'; if (el.disabled) return 'disabled'; "
        "el.scrollIntoView({block: 'center', inline: 'nearest'}); el.click(); return 'ok'; })()"
    ) % {
        "root": root_expr,
        "want": json.dumps(text, ensure_ascii=False),
        "selector": json.dumps(selector),
        "compare": "value === want" if exact else "value.includes(want)",
    }


def js_click_selector(selector: str) -> str:
    return (
        "(() => { const el = document.querySelector(%s); if (!el) return 'missing'; "
        "if (el.disabled) return 'disabled'; el.scrollIntoView({block: 'center'}); "
        "el.click(); return 'ok'; })()"
    ) % json.dumps(selector)


def js_text_list(selector: str) -> str:
    return (
        "(() => { return [...document.querySelectorAll(%s)].map((el) => "
        "(el.textContent || '').trim()); })()"
    ) % json.dumps(selector)


def js_project_card_names() -> str:
    """.pj-card 同时用于骨架屏；排除骨架屏后才是真实项目卡片。"""
    return js_text_list(".pj-card:not(.pj-card-skeleton) .pj-name")


def js_duplicate_member_names() -> str:
    return js_text_list(".pj-dup-member-name")


def js_open_duplicate_member(name: str) -> str:
    return (
        "(() => { const want = %(name)s; "
        "const members = [...document.querySelectorAll('.pj-dup-member')]; "
        "const member = members.find((node) => { "
        "const el = node.querySelector('.pj-dup-member-name'); "
        "return el ? (el.textContent || '').trim() === want : false; }); "
        "if (!member) return 'missing'; "
        "const btn = [...member.querySelectorAll('button')].find((el) => "
        "(el.textContent || '').trim() === '打开'); "
        "if (!btn) return 'missing-button'; btn.click(); return 'ok'; })()"
    ) % {"name": json.dumps(name, ensure_ascii=False)}


def js_checked(selector: str) -> str:
    return (
        "(() => { const el = document.querySelector(%s); "
        "return el ? Boolean(el.checked) : null; })()"
    ) % json.dumps(selector)


def js_click_task_card(title: str) -> str:
    return (
        "(() => { const want = %(title)s; "
        "const titles = [...document.querySelectorAll('.tk-card-title')]; "
        "const el = titles.find((node) => (node.textContent || '').trim() === want); "
        "if (!el) return 'missing'; const card = el.closest('.tk-card'); "
        "if (!card) return 'missing-card'; card.scrollIntoView({block: 'center'}); "
        "card.click(); return 'ok'; })()"
    ) % {"title": json.dumps(title, ensure_ascii=False)}


def js_review_tile(title: str) -> str:
    return (
        "(() => { const want = %(title)s; "
        "const tiles = [...document.querySelectorAll('.review-task')]; "
        "const tile = tiles.find((node) => { const t = node.querySelector('.review-task-title'); "
        "return t ? (t.textContent || '').trim() === want : false; }); "
        "return tile ? (tile.textContent || '').trim() : ''; })()"
    ) % {"title": json.dumps(title, ensure_ascii=False)}


def js_review_click(title: str) -> str:
    return (
        "(() => { const want = %(title)s; "
        "const tiles = [...document.querySelectorAll('.review-task')]; "
        "const tile = tiles.find((node) => { const t = node.querySelector('.review-task-title'); "
        "return t ? (t.textContent || '').trim() === want : false; }); "
        "if (!tile) return 'missing'; tile.click(); return 'ok'; })()"
    ) % {"title": json.dumps(title, ensure_ascii=False)}


def js_set_value(selector: str, value: str) -> str:
    return (
        "(() => { const el = document.querySelector(%(selector)s); if (!el) return false; "
        "const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype; "
        "const setter = Object.getOwnPropertyDescriptor(proto, 'value').set; "
        "setter.call(el, %(value)s); el.dispatchEvent(new Event('input', {bubbles: true})); "
        "el.dispatchEvent(new Event('change', {bubbles: true})); return el.value; })()"
    ) % {"selector": json.dumps(selector), "value": json.dumps(value, ensure_ascii=False)}


def js_visible_button_count(text: str) -> str:
    return (
        "(() => { return [...document.querySelectorAll('button')].filter((el) => "
        "el.offsetParent !== null && (el.textContent || '').includes(%s)).length; })()"
    ) % json.dumps(text, ensure_ascii=False)


def js_button_states(selector: str) -> str:
    return (
        "(() => { const root = document.querySelector(%s); if (!root) return []; "
        "return [...root.querySelectorAll('button')].filter((el) => el.offsetParent !== null).map((el) => "
        "({ text: (el.textContent || '').trim(), disabled: Boolean(el.disabled) })); })()"
    ) % json.dumps(selector)


JS_SETTINGS_STATE = (
    "(() => { const cards = [...document.querySelectorAll('.settings-subcard')]; "
    "const card = cards.find((c) => { const h = c.querySelector('h3'); "
    "return h ? (h.textContent || '').trim() === '交付与合并' : false; }); "
    "if (!card) return null; "
    "const rows = [...card.querySelectorAll('.settings-merge-row')]; "
    "const row = rows.find((r) => { const l = r.querySelector('.settings-merge-label'); "
    "return l ? (l.textContent || '').trim().startsWith('自动合并') : false; }); "
    "if (!row) return null; "
    "const active = row.querySelector('.segmented-item.is-active'); "
    "const hint = row.querySelector('.settings-merge-hint'); "
    "const save = card.querySelector('.settings-actions button.btn-primary'); "
    "return { auto: active ? (active.textContent || '').trim() : '', "
    "hint: hint ? (hint.textContent || '').trim() : '', "
    "text: (card.textContent || '').trim(), "
    "saveDisabled: save ? Boolean(save.disabled) : null }; })()"
)


def js_settings_toggle(label: str) -> str:
    return (
        "(() => { const cards = [...document.querySelectorAll('.settings-subcard')]; "
        "const card = cards.find((c) => { const h = c.querySelector('h3'); "
        "return h ? (h.textContent || '').trim() === '交付与合并' : false; }); "
        "if (!card) return 'missing-card'; "
        "const rows = [...card.querySelectorAll('.settings-merge-row')]; "
        "const row = rows.find((r) => { const l = r.querySelector('.settings-merge-label'); "
        "return l ? (l.textContent || '').trim().startsWith('自动合并') : false; }); "
        "if (!row) return 'missing-row'; "
        "const btn = [...row.querySelectorAll('.segmented-item')].find((b) => "
        "(b.textContent || '').trim() === %(label)s); "
        "if (!btn) return 'missing-button'; btn.click(); return 'ok'; })()"
    ) % {"label": json.dumps(label, ensure_ascii=False)}


def js_scroll_settings() -> str:
    return (
        "(() => { const cards = [...document.querySelectorAll('.settings-subcard')]; "
        "const card = cards.find((c) => { const h = c.querySelector('h3'); "
        "return h ? (h.textContent || '').trim() === '交付与合并' : false; }); "
        "if (!card) return false; card.scrollIntoView({block: 'center'}); return true; })()"
    )


JS_NEWTASK_EXPAND = (
    "(() => { if (document.querySelector('#newtask-timeout')) return 'already'; "
    "const btn = document.querySelector('[role=\"dialog\"] button.disclosure'); "
    "if (!btn) return 'missing'; btn.click(); return 'ok'; })()"
)


def js_clear_toasts() -> str:
    return (
        "(() => { const nodes = [...document.querySelectorAll('.toast-close')]; "
        "nodes.forEach((el) => el.click()); return nodes.length; })()"
    )


# --------------------------------------------------------------------------- #
# flow harness
# --------------------------------------------------------------------------- #


@dataclass
class FlowResult:
    name: str
    title: str
    passed: bool
    failures: list[str] = field(default_factory=list)


def norm(value: Any) -> str:
    return " ".join(str(value or "").split())


class FlowHarness:
    def __init__(
        self,
        cdp: FlowCDP,
        recorder: Recorder,
        out_dir: Path,
        data: SeedData,
        base_url: str,
        timeout: float,
    ) -> None:
        self.cdp = cdp
        self.rec = recorder
        self.out_dir = out_dir
        self.data = data
        self.base_url = base_url
        self.timeout = timeout
        self.results: list[FlowResult] = []
        self.current = ""

    # ------------------------------------------------------------------ util

    def check(self, condition: Any, message: str) -> bool:
        return self.rec.check(self.current, bool(condition), message)

    async def eval(self, expression: str) -> Any:
        return await self.cdp.eval(expression)

    async def wait(
        self,
        expression: str,
        timeout: float | None = None,
        predicate: Callable[[Any], bool] | None = None,
    ) -> Any:
        return await self.cdp.poll(
            expression, timeout if timeout is not None else self.timeout, predicate=predicate
        )

    async def wait_text(self, selector: str, contains: str, timeout: float | None = None) -> str:
        value = await self.wait(
            js_text(selector),
            timeout,
            predicate=lambda item: contains in norm(item),
        )
        return norm(value)

    async def wait_toast(self, text: str, timeout: float = 8.0) -> str:
        value = await self.wait(
            js_text(".toast-stack"), timeout, predicate=lambda item: text in norm(item)
        )
        return norm(value)

    async def clear_toasts(self) -> None:
        await self.eval(js_clear_toasts())

    async def shot(self, name: str) -> None:
        path = self.out_dir / name
        try:
            size = await self.cdp.screenshot(path)
            self.check(size > 1000, "截图 " + name + "（" + str(size) + " B）")
        except Exception as exc:  # noqa: BLE001
            self.check(False, "截图 " + name + " 失败：" + str(exc))

    async def click_nav(self, label: str) -> bool:
        ok = bool(await self.eval(js_click_nav(label)))
        self.check(ok, "点击侧栏「" + label + "」")
        await asyncio.sleep(0.35)
        return ok

    async def wait_cards(self, minimum: int = 4, timeout: float | None = None) -> int:
        value = await self.wait(
            "document.querySelectorAll('.tk-card').length",
            timeout,
            predicate=lambda item: isinstance(item, (int, float)) and int(item) >= minimum,
        )
        return int(value or 0)

    async def open_task(self, key: str) -> bool:
        title = TITLES[key]
        current = norm(await self.eval(js_text(".td-title")))
        if current == title:
            return True
        await self.click_nav("任务")
        cards = await self.wait_cards()
        if not self.check(cards >= 4, "任务看板加载（" + str(cards) + " 张卡片）"):
            return False
        result = await self.eval(js_click_task_card(title))
        if not self.check(result == "ok", "打开任务「" + title + "」（" + str(result) + "）"):
            return False
        opened = await self.wait(
            js_text(".td-title"), predicate=lambda item: norm(item) == title
        )
        self.check(norm(opened) == title, "任务详情标题为「" + title + "」")
        return norm(opened) == title

    async def wait_dialog_closed(self, timeout: float = 10.0) -> bool:
        value = await self.wait(
            "Boolean(document.querySelector('.dialog'))", timeout, predicate=lambda item: not item
        )
        return not bool(value)

    async def wait_response(
        self, url_contains: str, status: int, start_index: int, timeout: float = 15.0
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for entry in self.cdp.responses[start_index:]:
                if url_contains in str(entry.get("url") or "") and entry.get("status") == status:
                    return entry
            await asyncio.sleep(0.15)
        return None

    async def wait_api(
        self,
        fetch: Callable[[], Any],
        predicate: Callable[[Any], bool],
        timeout: float = 12.0,
    ) -> Any:
        deadline = time.monotonic() + timeout
        last: Any = None
        while time.monotonic() < deadline:
            try:
                last = fetch()
            except Exception:  # noqa: BLE001
                last = None
            if last is not None and predicate(last):
                return last
            await asyncio.sleep(0.35)
        return last

    # ------------------------------------------------------------------ flows

    async def flow_task_merged(self) -> None:
        if not await self.open_task("merged"):
            return
        badge = norm(await self.wait(js_text(".td-merge-badge")))
        self.check("已合并到" in badge, "徽章包含「已合并到」（" + badge + "）")
        branch = self.data.branches["merged"]
        self.check(branch.startswith("ma/"), "种子任务分支以 ma/ 开头（" + branch + "）")
        self.check(branch in badge, "徽章包含任务分支 " + branch)
        sha8 = self.data.merge_shas["merged"][:8]
        self.check(len(sha8) == 8, "合并提交 sha 可用（" + sha8 + "）")
        self.check(sha8 in badge, "徽章包含 8 位 sha " + sha8)
        sync_buttons = int(await self.eval(js_visible_button_count("同步到工作区")) or 0)
        self.check(sync_buttons == 0, "页面上不存在可见的「同步到工作区」按钮")
        await self.shot("task-merged.png")

    async def flow_merge_conflict(self) -> None:
        if not await self.open_task("conflict"):
            return
        panel = await self.wait_text(".td-merge-issue", "合并冲突")
        self.check("合并冲突" in panel, "「合并问题」面板标题包含「合并冲突」")
        self.check("conflict.txt" in panel, "面板列出冲突文件 conflict.txt")
        branch = self.data.branches["conflict"]
        self.check(branch in panel, "面板显示任务分支 " + branch)
        base8 = self.data.base_shas["conflict"][:8]
        self.check(len(base8) == 8 and base8 in panel, "面板显示基线 " + base8)
        self.check("conflict" in panel, "面板显示原因 conflict")
        buttons = await self.eval(
            "(() => { const root = document.querySelector('.td-merge-issue-actions'); "
            "if (!root) return []; return [...root.querySelectorAll('button')].map((b) => "
            "({ text: (b.textContent || '').trim(), disabled: Boolean(b.disabled) })); })()"
        ) or []
        labels = [norm(item.get("text")) for item in buttons]
        for label in ("重试合并", "放弃任务分支", "打开工作目录"):
            self.check(label in labels, "冲突面板包含按钮「" + label + "」")
        self.check(
            all(not item.get("disabled") for item in buttons if norm(item.get("text")) in ("重试合并", "放弃任务分支", "打开工作目录")),
            "冲突面板三个动作均可用",
        )
        await self.shot("task-conflict-before.png")

        before = len(self.cdp.responses)
        result = await self.eval(js_click(".td-merge-issue-actions button", "重试合并", exact=True))
        self.check(result == "ok", "点击冲突面板「重试合并」（" + str(result) + "）")
        response = await self.wait_response(
            "/api/tasks/" + self.data.tasks["conflict"] + "/merge", 409, before, 15.0
        )
        self.check(response is not None, "重试合并返回 HTTP 409")
        toast = await self.wait_toast("合并冲突", 8.0)
        self.check("合并冲突" in toast, "409 后出现冲突 toast（" + toast + "）")
        await asyncio.sleep(0.8)
        still_panel = await self.eval(js_text(".td-merge-issue"))
        title = await self.eval(js_text(".td-title"))
        self.check("合并冲突" in norm(still_panel), "409 后合并冲突面板仍在")
        self.check(norm(title) == TITLES["conflict"], "409 后任务详情未崩溃（标题仍在）")
        await self.shot("task-conflict.png")

    async def flow_extend_timeout(self) -> None:
        if not await self.open_task("conflict"):
            return
        await self.clear_toasts()
        result = await self.eval(js_click_selector(".td-more-button"))
        self.check(result == "ok", "打开任务「更多」菜单（" + str(result) + "）")
        menu = await self.wait_text(".td-menu", "延长超时")
        self.check("延长超时" in menu, "更多菜单包含「延长超时」")
        result = await self.eval(js_click(".td-menu .menu-item", "延长超时"))
        self.check(result == "ok", "点击「延长超时」（" + str(result) + "）")
        dialog_title = norm(await self.wait(js_text(".dialog .dialog-title")))
        self.check(dialog_title == "延长超时", "弹出「延长超时」对话框")
        default_value = await self.eval(js_value('.dialog input[type="number"]'))
        try:
            default_int = int(str(default_value))
        except (TypeError, ValueError):
            default_int = 0
        self.check(default_int >= 30, "对话框默认超时值有效（" + str(default_value) + "）")
        dialog_body = norm(await self.eval(js_text(".dialog .dialog-body")))
        self.check("下一次" in dialog_body, "对话框说明包含「下一次」生效语义")

        original = int(get_task(self.base_url, self.data.tasks["conflict"]).get("timeout_sec") or 0)
        self.check(original >= 30, "任务当前 timeout_sec 已记录（" + str(original) + "）")

        await self.eval(js_set_value('.dialog input[type="number"]', "1"))
        result = await self.eval(js_click(".dialog .dialog-foot button", "保存", exact=True))
        self.check(result == "ok", "非法值 1 提交（" + str(result) + "）")
        toast = await self.wait_toast("超时时间无效", 8.0)
        self.check("超时时间无效" in toast, "非法超时值 1 被拒绝（" + toast + "）")
        open_dialog = bool(await self.eval("Boolean(document.querySelector('.dialog'))"))
        self.check(open_dialog, "非法值提交后对话框仍打开")
        after_invalid = int(get_task(self.base_url, self.data.tasks["conflict"]).get("timeout_sec") or 0)
        self.check(after_invalid == original, "非法值没有写入数据库（仍为 " + str(after_invalid) + "）")

        await self.eval(js_set_value('.dialog input[type="number"]', "120"))
        await self.shot("timeout-dialog.png")
        await self.clear_toasts()
        result = await self.eval(js_click(".dialog .dialog-foot button", "保存", exact=True))
        self.check(result == "ok", "合法值 120 提交（" + str(result) + "）")
        closed = await self.wait_dialog_closed(10.0)
        self.check(closed, "合法值提交后对话框关闭")
        toast = await self.wait_toast("超时预算已更新", 8.0)
        self.check("超时预算已更新" in toast, "出现「超时预算已更新」toast（" + toast + "）")
        self.check("下一次" in toast, "toast 提示「下一次」生效（实际：" + toast + "）")
        self.check("下一次尝试" in toast, "toast 明确「下一次尝试」语义（实际：" + toast + "）")
        await self.shot("timeout-saved.png")
        task = await self.wait_api(
            lambda: get_task(self.base_url, self.data.tasks["conflict"]),
            lambda item: int(item.get("timeout_sec") or 0) == 120,
            10.0,
        )
        self.check(int(task.get("timeout_sec") or 0) == 120, "API 确认 timeout_sec == 120")

    async def flow_settings_delivery(self) -> None:
        await self.clear_toasts()
        await self.click_nav("设置")
        page = await self.wait(
            "Boolean(document.querySelector('.settings-page'))",
            predicate=lambda item: bool(item),
        )
        self.check(bool(page), "进入设置页")
        state = await self.wait(JS_SETTINGS_STATE, predicate=lambda item: bool(item), timeout=15.0)
        if not self.check(isinstance(state, dict), "「交付与合并」设置卡片出现"):
            return
        self.check(norm(state.get("auto")) == "关闭", "初始 auto_merge 开关为「关闭」")
        self.check("手动合并" in norm(state.get("hint")), "关闭态提示包含「手动合并」")
        self.check("阻止合并" in norm(state.get("text")), "卡片包含「阻止合并」风险提示")

        result = await self.eval(js_settings_toggle("开启"))
        self.check(result == "ok", "切换 auto_merge 为「开启」（" + str(result) + "）")
        state = await self.wait(JS_SETTINGS_STATE, predicate=lambda item: bool(item))
        self.check(norm(state.get("auto")) == "开启", "开关显示「开启」")
        self.check("验收通过即" in norm(state.get("hint")), "开启态提示包含「验收通过即」")

        result = await self.eval(js_click(".settings-subcard button", "保存交付策略", exact=True))
        self.check(result == "ok", "点击「保存交付策略」（" + str(result) + "）")
        toast = await self.wait_toast("交付策略已保存", 10.0)
        self.check("交付策略已保存" in toast, "保存后出现成功 toast")
        policy = await self.wait_api(
            lambda: read_policy(self.base_url),
            lambda item: bool((item.get("git") or {}).get("auto_merge") is True),
            12.0,
        )
        self.check(
            (policy.get("git") or {}).get("auto_merge") is True,
            "API policy.json 的 git.auto_merge == true",
        )
        bodies = [
            entry
            for entry in self.cdp.requests
            if "config/files/policy" in str(entry.get("url") or "")
            and str(entry.get("method") or "").upper() == "PUT"
        ]
        if bodies:
            compact = "".join(str(bodies[-1].get("postData") or "").split())
            self.check(
                '"auto_merge":true' in compact,
                "捕获到 PUT policy 请求体包含 auto_merge=true",
            )
        else:
            log("  (info) 未捕获 PUT policy 请求体；已用 API 轮询确认落盘")

        await self.cdp.send("Page.reload", {"ignoreCache": True})
        await asyncio.sleep(0.6)
        state = await self.wait(JS_SETTINGS_STATE, predicate=lambda item: bool(item), timeout=20.0)
        self.check(isinstance(state, dict), "刷新后仍停留在设置页并加载交付卡片")
        if isinstance(state, dict):
            self.check(norm(state.get("auto")) == "开启", "刷新后开关状态保持「开启」")
            self.check("验收通过即" in norm(state.get("hint")), "刷新后风险提示保持")
        await self.eval(js_scroll_settings())
        await asyncio.sleep(0.4)
        await self.shot("settings-merge.png")

        result = await self.eval(js_settings_toggle("关闭"))
        self.check(result == "ok", "切回 auto_merge「关闭」（" + str(result) + "）")
        result = await self.eval(js_click(".settings-subcard button", "保存交付策略", exact=True))
        self.check(result == "ok", "再次点击「保存交付策略」（" + str(result) + "）")
        await self.wait_toast("交付策略已保存", 10.0)
        policy = await self.wait_api(
            lambda: read_policy(self.base_url),
            lambda item: (item.get("git") or {}).get("auto_merge") is False,
            12.0,
        )
        self.check(
            (policy.get("git") or {}).get("auto_merge") is False,
            "切回后 API policy.json 的 git.auto_merge == false",
        )
        state = await self.wait(JS_SETTINGS_STATE, predicate=lambda item: bool(item))
        self.check("手动合并" in norm(state.get("hint")), "切回后提示恢复「手动合并」")

    async def flow_review_states(self) -> None:
        await self.clear_toasts()
        await self.click_nav("审阅")
        page = await self.wait(
            "Boolean(document.querySelector('.review-page'))", predicate=lambda item: bool(item)
        )
        self.check(bool(page), "进入审阅页")
        sidebar = await self.wait_text(".review-sidebar", "已合并")
        self.check("待审阅" in sidebar, "审阅页分组包含「待审阅」")
        self.check("已合并 / 已同步" in sidebar, "审阅页分组包含「已合并 / 已同步」")
        self.check("失败" in sidebar, "审阅页分组包含「失败」")

        merged_tile = await self.wait(
            js_review_tile(TITLES["merged"]),
            predicate=lambda item: "已合并" in norm(item),
            timeout=20.0,
        )
        self.check("已合并" in norm(merged_tile), "已合并任务显示 Badge「已合并」")
        unmerged_tile = await self.wait(
            js_review_tile(TITLES["unmerged"]),
            predicate=lambda item: "待同步" in norm(item) or "未合并" in norm(item),
            timeout=20.0,
        )
        self.check(
            "待同步" in norm(unmerged_tile) or "未合并" in norm(unmerged_tile),
            "未合并任务显示「待同步/未合并」（" + norm(unmerged_tile) + "）",
        )
        conflict_tile = await self.wait(
            js_review_tile(TITLES["conflict"]),
            predicate=lambda item: "合并冲突" in norm(item),
            timeout=20.0,
        )
        self.check("合并冲突" in norm(conflict_tile), "冲突任务显示 Badge「合并冲突」")
        discarded_tile = await self.wait(
            js_review_tile(TITLES["discarded"]),
            predicate=lambda item: "已放弃" in norm(item),
            timeout=20.0,
        )
        self.check("已放弃分支" in norm(discarded_tile), "已放弃任务显示 Badge「已放弃分支」")

        result = await self.eval(js_review_click(TITLES["conflict"]))
        self.check(result == "ok", "在审阅页点击冲突任务（" + str(result) + "）")
        panel = await self.wait_text(".review-merge-panel", "合并冲突", timeout=15.0)
        self.check("合并冲突" in panel, "审阅详情显示「合并冲突」面板")
        self.check("conflict.txt" in panel, "审阅详情列出 conflict.txt")
        buttons = await self.eval(
            "(() => { const root = document.querySelector('.review-merge-actions'); "
            "if (!root) return []; return [...root.querySelectorAll('button')].map((b) => "
            "({ text: (b.textContent || '').trim(), disabled: Boolean(b.disabled) })); })()"
        ) or []
        labels = [norm(item.get("text")) for item in buttons]
        for label in ("重试合并", "放弃任务分支"):
            self.check(label in labels, "审阅合并面板包含「" + label + "」")
        self.check(
            all(not item.get("disabled") for item in buttons),
            "审阅合并面板动作可用",
        )
        await self.shot("review-states.png")

    async def flow_new_task_advanced(self) -> None:
        await self.clear_toasts()
        await self.click_nav("任务")
        await self.wait_cards()
        result = await self.eval(js_click(".topbar button", "新任务", exact=True))
        self.check(result == "ok", "点击顶栏「新任务」（" + str(result) + "）")
        opened = await self.wait(
            "Boolean(document.querySelector('[role=\"dialog\"] textarea.newtask-input'))",
            predicate=lambda item: bool(item),
        )
        if not self.check(bool(opened), "新建任务对话框打开"):
            return
        await self.eval(js_set_value('[role="dialog"] textarea.newtask-input', NEW_TASK_TEXT))
        expanded = await self.eval(JS_NEWTASK_EXPAND)
        self.check(expanded in ("ok", "already"), "展开「高级选项」（" + str(expanded) + "）")
        await self.eval(js_set_value("#newtask-timeout", "1234"))
        await self.eval(js_set_value("#newtask-max-attempts", "2"))
        value = await self.eval(js_value("#newtask-timeout"))
        self.check(str(value) == "1234", "高级选项 timeout 输入 1234（" + str(value) + "）")
        await self.shot("newtask-advanced.png")

        result = await self.eval(js_click(".dialog .dialog-foot button", "仅创建", exact=True))
        self.check(result == "ok", "点击「仅创建」（" + str(result) + "）")
        closed = await self.wait_dialog_closed(12.0)
        self.check(closed, "创建成功后对话框关闭")

        def list_tasks() -> list[dict[str, Any]]:
            payload = api_ok(
                self.base_url,
                "GET",
                "/api/projects/" + self.data.project_id + "/tasks",
                label="列出任务",
            )
            return payload if isinstance(payload, list) else []

        created = await self.wait_api(
            list_tasks,
            lambda rows: any(str(row.get("title")) == NEW_TASK_TITLE for row in rows or []),
            15.0,
        )
        rows = created if isinstance(created, list) else []
        match = next((row for row in rows if str(row.get("title")) == NEW_TASK_TITLE), None)
        if not self.check(match is not None, "API 能按标题找到新建任务"):
            return
        self.check(int(match.get("timeout_sec") or 0) == 1234, "新任务 timeout_sec 落库为 1234")
        self.check(int(match.get("max_attempts") or 0) == 2, "新任务 max_attempts 落库为 2")
        count_before = len(rows)

        result = await self.eval(js_click(".topbar button", "新任务", exact=True))
        self.check(result == "ok", "再次打开「新任务」")
        await self.wait(
            "Boolean(document.querySelector('[role=\"dialog\"] textarea.newtask-input'))",
            predicate=lambda item: bool(item),
        )
        await self.eval(js_set_value('[role="dialog"] textarea.newtask-input', NEW_TASK_TEXT + " 非法值"))
        expanded = await self.eval(JS_NEWTASK_EXPAND)
        self.check(expanded in ("ok", "already"), "再次展开「高级选项」（" + str(expanded) + "）")
        await self.eval(js_set_value("#newtask-timeout", "1"))
        result = await self.eval(js_click(".dialog .dialog-foot button", "仅创建", exact=True))
        self.check(result == "ok", "提交非法 timeout=1（" + str(result) + "）")
        hint = await self.wait_text("#newtask-timeout-hint", "超时预算需在", timeout=8.0)
        self.check("超时预算需在" in hint, "内联校验提示非法范围（" + hint + "）")
        invalid = await self.eval(
            "(() => { const el = document.querySelector('#newtask-timeout'); "
            "return el ? el.getAttribute('aria-invalid') : null; })()"
        )
        self.check(str(invalid) == "true", "输入框 aria-invalid=true")
        still_open = bool(
            await self.eval("Boolean(document.querySelector('[role=\"dialog\"] textarea.newtask-input'))")
        )
        self.check(still_open, "非法值阻止提交，对话框保持打开")
        rows_after = list_tasks()
        self.check(len(rows_after) == count_before, "非法值没有创建新任务")
        await self.shot("newtask-invalid.png")
        await self.cdp.key("Escape", "Escape", 27)
        await self.wait_dialog_closed(6.0)

    async def flow_missing_branch(self) -> None:
        """第 8 条：分支 ref 缺失 → 诚实 409 → 确认重建 → 重新合并成功。"""
        tid = self.data.tasks["missing"]
        branch = self.data.branches["missing"]
        base = self.data.base_shas["missing"]
        status, body = api_call(self.base_url, "POST", "/api/tasks/" + tid + "/merge", {})
        payload = body if isinstance(body, dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        details = error.get("details") if isinstance(error.get("details"), dict) else {}
        self.check(status == 409, "直接合并缺失分支返回 HTTP 409（实际 " + str(status) + "）")
        self.check(payload.get("reason") == "missing_branch", "409 顶层 reason=missing_branch")
        self.check(payload.get("code") == "missing_branch", "409 顶层 code=missing_branch")
        self.check(payload.get("branch") == branch, "409 顶层 branch 为 " + branch)
        self.check(payload.get("task_id") == tid, "409 顶层 task_id 正确")
        self.check(details.get("branch") == branch, "error.details.branch 正确")
        self.check(details.get("task_id") == tid, "error.details.task_id 正确")
        self.check(details.get("base") == base, "error.details.base 正确")

        if not await self.open_task("missing"):
            return
        panel = await self.wait_text(".td-merge-issue", "分支已不存在，无法合并", 20.0)
        self.check("分支已不存在，无法合并" in panel, "任务详情显示「分支已不存在，无法合并」")
        self.check(branch in panel, "缺失分支面板显示任务分支 " + branch)
        self.check(base[:8] in panel, "缺失分支面板显示基线 " + base[:8])
        self.check("missing_branch" in panel, "缺失分支面板显示原因 missing_branch")

        head_buttons = await self.eval(js_button_states(".td-head-actions")) or []
        merge_buttons = [
            item
            for item in head_buttons
            if "合并到基线" in norm(item.get("text")) or "重试合并" in norm(item.get("text"))
        ]
        self.check(
            not any(not item.get("disabled") for item in merge_buttons),
            "缺失分支时头部合并按钮不存在或已禁用",
        )
        actions = await self.eval(js_button_states(".td-merge-issue-actions")) or []
        labels = [norm(item.get("text")) for item in actions]
        for label in ("重建分支", "放弃任务", "打开工作目录"):
            self.check(label in labels, "缺失分支面板包含「" + label + "」")
        self.check("重试合并" not in labels, "缺失分支面板不显示「重试合并」")
        self.check(
            all(not item.get("disabled") for item in actions if norm(item.get("text")) in ("重建分支", "放弃任务")),
            "缺失分支面板的重建/放弃动作可用",
        )
        await self.shot("missing-branch-before.png")

        await self.clear_toasts()
        result = await self.eval(
            js_click(".td-merge-issue-actions button", "重建分支", exact=True)
        )
        self.check(result == "ok", "点击「重建分支」（" + str(result) + "）")
        dialog_title = norm(await self.wait(js_text(".dialog .dialog-title"), 10.0))
        self.check("重建任务分支" in dialog_title, "出现重建确认对话框（" + dialog_title + "）")
        dialog_body = norm(await self.eval(js_text(".dialog .dialog-body")))
        self.check("commit/base" in dialog_body, "确认框说明按 commit/base 重建")
        self.check("未提交" in dialog_body, "确认框提示未提交内容可能丢失")

        before = len(self.cdp.responses)
        result = await self.eval(
            js_click(".dialog .dialog-foot button", "确认重建", exact=True)
        )
        self.check(result == "ok", "确认重建（" + str(result) + "）")
        response = await self.wait_response(
            "/api/tasks/" + tid + "/rebuild-branch", 200, before, 20.0
        )
        self.check(response is not None, "重建接口返回 HTTP 200")
        closed = await self.wait_dialog_closed(10.0)
        self.check(closed, "重建确认对话框关闭")
        toast = await self.wait_toast("任务分支已重建", 15.0)
        self.check("任务分支已重建" in toast, "重建后出现成功 toast（" + toast + "）")
        gone = await self.wait(
            "Boolean(document.querySelector('.td-merge-issue'))",
            20.0,
            predicate=lambda item: not item,
        )
        self.check(not bool(gone), "重建后面板消失")
        head_buttons = await self.wait(
            js_button_states(".td-head-actions"),
            20.0,
            predicate=lambda items: any(
                norm(item.get("text")) == "合并到基线" and not item.get("disabled")
                for item in (items or [])
            ),
        )
        head_labels = [norm(item.get("text")) for item in (head_buttons or [])]
        self.check("合并到基线" in head_labels, "重建后「合并到基线」入口恢复")
        self.check(
            any(
                norm(item.get("text")) == "合并到基线" and not item.get("disabled")
                for item in (head_buttons or [])
            ),
            "重建后「合并到基线」按钮可用",
        )

        await self.clear_toasts()
        result = await self.eval(
            js_click(".td-head-actions button", "合并到基线", exact=True)
        )
        self.check(result == "ok", "点击「合并到基线」（" + str(result) + "）")
        dialog_title = norm(await self.wait(js_text(".dialog .dialog-title"), 10.0))
        self.check("合并" in dialog_title, "出现合并确认对话框（" + dialog_title + "）")
        before = len(self.cdp.responses)
        result = await self.eval(
            js_click(".dialog .dialog-foot button", "合并到基线", exact=True)
        )
        self.check(result == "ok", "确认合并（" + str(result) + "）")
        response = await self.wait_response("/api/tasks/" + tid + "/merge", 200, before, 20.0)
        self.check(response is not None, "重建后的合并返回 HTTP 200")
        toast = await self.wait_toast("已合并到", 15.0)
        self.check("已合并到" in toast, "合并成功 toast（" + toast + "）")
        merged = await self.wait_api(
            lambda: get_task(self.base_url, tid),
            lambda item: task_merge(item).get("merged") is True,
            15.0,
        )
        self.check(
            task_merge(merged).get("merged") is True,
            "API 确认重建后的任务已合并",
        )
        merged_file = self.data.repo / "missing.txt"
        self.check(merged_file.is_file(), "missing.txt 已进入基线")
        if merged_file.is_file():
            self.check(
                merged_file.read_text(encoding="utf-8") == "rebuilt from recorded commit",
                "missing.txt 内容与任务提交一致",
            )
        await self.shot("missing-branch-after.png")

    async def flow_duplicate_projects(self) -> None:
        """第 7 条：历史重复项目横幅 / 筛选 / 打开成员 / 恢复列表。

        种子来自 :func:`seed_duplicate_projects`（后端启动前直接写库）。
        本流程最后执行，即使打开历史项目改变了当前项目，也不会影响前 6 条。
        """
        await self.clear_toasts()
        await self.click_nav("项目")
        page = await self.wait(
            "Boolean(document.querySelector('.projects-page'))",
            predicate=lambda item: bool(item),
        )
        self.check(bool(page), "进入项目页")

        banner = await self.wait_text(".pj-dup-banner", "组重复项目", timeout=20.0)
        self.check("发现" in banner, "横幅包含「发现」（" + banner[:120] + "）")
        self.check("组重复项目" in banner, "横幅包含「组重复项目」")
        self.check("历史数据未删除" in banner, "横幅包含「历史数据未删除」")
        self.check("仅显示重复项目" in banner, "横幅包含「仅显示重复项目」")
        self.check("legacy-shared" in banner, "横幅展示重复目录 legacy-shared")

        members = await self.wait(
            js_duplicate_member_names(),
            predicate=lambda item: isinstance(item, list)
            and all(name in [norm(entry) for entry in item] for name in DUPLICATE_PROJECT_NAMES),
            timeout=15.0,
        )
        member_names = [norm(entry) for entry in (members or [])]
        for name in DUPLICATE_PROJECT_NAMES:
            self.check(name in member_names, "重复组内可见项目「" + name + "」")
        self.check(
            len(member_names) == len(DUPLICATE_PROJECT_NAMES),
            "重复组恰好包含两个项目（" + "、".join(member_names) + "）",
        )

        cards = await self.wait(
            js_project_card_names(),
            predicate=lambda item: isinstance(item, list)
            and len(item) == len(DUPLICATE_PROJECT_NAMES) + 1,
            timeout=20.0,
        )
        card_names = [norm(entry) for entry in (cards or [])]
        self.check(
            len(card_names) == 3,
            "项目页共 3 张卡片（" + "、".join(card_names) + "）",
        )
        self.check(
            self.data.project_name in card_names,
            "主项目「" + self.data.project_name + "」仍在列表中",
        )

        selector = '.pj-dup-filter input[type="checkbox"]'
        result = await self.eval(js_click_selector(selector))
        self.check(result == "ok", "勾选「仅显示重复项目」（" + str(result) + "）")
        filtered = await self.wait(
            js_project_card_names(),
            predicate=lambda item: isinstance(item, list)
            and len(item) == len(DUPLICATE_PROJECT_NAMES),
            timeout=15.0,
        )
        filtered_names = [norm(entry) for entry in (filtered or [])]
        for name in DUPLICATE_PROJECT_NAMES:
            self.check(name in filtered_names, "筛选后保留卡片「" + name + "」")
        self.check(
            self.data.project_name not in filtered_names,
            "非重复项目卡片「" + self.data.project_name + "」被隐藏",
        )
        self.check(
            bool(await self.eval(js_checked(selector))),
            "「仅显示重复项目」复选框为已勾选",
        )
        await self.shot("duplicate-projects.png")

        target = DUPLICATE_PROJECT_NAMES[0]
        result = await self.eval(js_open_duplicate_member(target))
        self.check(result == "ok", "点击重复组内「打开」（" + str(result) + "）")
        opened = await self.wait(
            js_text(".project-name"), predicate=lambda item: norm(item) == target, timeout=15.0
        )
        self.check(norm(opened) == target, "打开后顶栏项目名切换为「" + target + "」")
        heading = await self.wait(
            js_text("main h1"), predicate=lambda item: norm(item) == target, timeout=15.0
        )
        self.check(norm(heading) == target, "打开后进入概览页（主标题为项目名）")
        self.check(
            not bool(await self.eval("Boolean(document.querySelector('.projects-page'))")),
            "打开项目后已离开项目页",
        )

        await self.click_nav("项目")
        await self.wait(
            "Boolean(document.querySelector('.projects-page'))",
            predicate=lambda item: bool(item),
        )
        restored_banner = await self.wait_text(".pj-dup-banner", "历史数据未删除", timeout=15.0)
        self.check("组重复项目" in restored_banner, "返回项目页后重复横幅仍在")

        # 切换视图会重新挂载 ProjectsView，筛选状态可能已经复位；两种路径都要
        # 落到「未勾选」再断言列表恢复，避免把「复位」误判成失败。
        checked = await self.eval(js_checked(selector))
        if checked is True:
            result = await self.eval(js_click_selector(selector))
            self.check(result == "ok", "取消「仅显示重复项目」（" + str(result) + "）")
            checked = await self.eval(js_checked(selector))
            self.check(checked is False, "「仅显示重复项目」复选框已取消")
        else:
            log("  (info) 切换视图后 ProjectsView 重新挂载，筛选状态已复位")
            self.check(checked is False, "返回项目页后筛选已取消")

        restored = await self.wait(
            js_project_card_names(),
            predicate=lambda item: isinstance(item, list)
            and len(item) == len(DUPLICATE_PROJECT_NAMES) + 1,
            timeout=15.0,
        )
        restored_names = [norm(entry) for entry in (restored or [])]
        self.check(
            len(restored_names) == 3,
            "取消筛选后恢复全部 3 张卡片（" + "、".join(restored_names) + "）",
        )
        self.check(
            self.data.project_name in restored_names,
            "取消筛选后主项目卡片恢复",
        )
        for name in DUPLICATE_PROJECT_NAMES:
            self.check(
                name in restored_names,
                "取消筛选后重复项目「" + name + "」仍在",
            )

    # ------------------------------------------------------------------ driver

    async def run_flow(self, name: str, fn: Callable[[], Any]) -> None:
        self.current = name
        title = FLOW_TITLES[name]
        before_failures = len(self.rec.failures)
        before_exceptions = len(self.rec.exceptions)
        before_console = len(self.rec.console_errors)
        log("\n== flow: " + name + " — " + title + " ==")
        try:
            await fn()
        except Exception as exc:  # noqa: BLE001 - one flow must not kill the rest
            message = type(exc).__name__ + ": " + str(exc)
            self.rec.failures.append(Failure(name, message))
            log("  !! flow exception: " + message[:800])
        new_failures = self.rec.failures[before_failures:]
        new_exceptions = self.rec.exceptions[before_exceptions:]
        new_console = self.rec.console_errors[before_console:]
        details = [failure.message for failure in new_failures]
        details.extend("uncaught: " + item for item in new_exceptions)
        details.extend("console: " + item for item in new_console)
        passed = not details
        self.results.append(FlowResult(name=name, title=title, passed=passed, failures=details))
        log("FLOW " + name + ": " + ("PASS" if passed else "FAIL"))

    async def run_all(self) -> list[FlowResult]:
        await self.run_flow("task-merged", self.flow_task_merged)
        await self.run_flow("merge-conflict", self.flow_merge_conflict)
        await self.run_flow("extend-timeout", self.flow_extend_timeout)
        await self.run_flow("settings-delivery", self.flow_settings_delivery)
        await self.run_flow("review-states", self.flow_review_states)
        await self.run_flow("new-task-advanced", self.flow_new_task_advanced)
        await self.run_flow("missing-branch", self.flow_missing_branch)
        await self.run_flow("duplicate-projects", self.flow_duplicate_projects)
        return self.results


# --------------------------------------------------------------------------- #
# browser driver
# --------------------------------------------------------------------------- #


async def run_browser(
    base_url: str,
    data: SeedData,
    recorder: Recorder,
    out_dir: Path,
    timeout: float,
    edge_override: str | None,
    profile: Path,
) -> list[FlowResult]:
    if edge_override:
        v2_smoke.EDGE_CANDIDATES.insert(0, edge_override)
    edge = v2_smoke.find_edge()
    debug_port = free_port()
    process = subprocess.Popen(
        configure_edge(edge, debug_port, profile),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    cdp: FlowCDP | None = None
    ws = None
    results: list[FlowResult] = []
    try:
        if not wait_cdp(debug_port, timeout):
            recorder.check("browser", False, "Edge CDP " + str(debug_port) + " 未就绪")
            return results
        target = http_json(
            "http://127.0.0.1:" + str(debug_port) + "/json/new?"
            + urllib.parse.quote("about:blank", safe=""),
            method="PUT",
            timeout=10,
        )
        ws = await websockets.connect(
            target["webSocketDebuggerUrl"],
            max_size=64 << 20,
            open_timeout=10,
            ping_interval=None,
        )
        cdp = FlowCDP(ws, recorder)
        await cdp.start()
        await cdp.send("Page.enable")
        await cdp.send("Runtime.enable")
        await cdp.send("Log.enable")
        await cdp.send("Network.enable")
        await cdp.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": VIEWPORT[0],
                "height": VIEWPORT[1],
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )
        log("\n== navigate " + base_url + " ==")
        await cdp.send("Page.navigate", {"url": base_url})
        state = await wait_ready(cdp, timeout)
        recorder.check("boot", state == "app", "应用外壳出现（" + (state or "timeout") + "）")
        if state != "app":
            return results
        await wait_network_quiet(cdp, timeout=min(15.0, timeout))
        project_name = await cdp.poll(
            "(document.querySelector('.project-name') ? document.querySelector('.project-name').textContent : '').trim()",
            timeout,
            predicate=lambda value: bool(value),
        )
        recorder.check("boot", bool(project_name), "项目自动选中（" + str(project_name or "空") + "）")
        harness = FlowHarness(cdp, recorder, out_dir, data, base_url, timeout)
        results = await harness.run_all()
        return results
    finally:
        if cdp is not None:
            await cdp.close()
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        stop_process(process, "Edge")


# --------------------------------------------------------------------------- #
# summary / main
# --------------------------------------------------------------------------- #


def summary(recorder: Recorder, results: list[FlowResult], out_dir: Path) -> int:
    recorder.check("global", not recorder.exceptions, "无未捕获异常（" + str(len(recorder.exceptions)) + "）")
    recorder.check("global", not recorder.console_errors, "无 console error（" + str(len(recorder.console_errors)) + "）")
    by_name = {result.name: result for result in results}
    for name in FLOW_NAMES:
        if name not in by_name:
            results.append(FlowResult(name, FLOW_TITLES[name], False, ["未执行（浏览器未就绪或流程中断）"]))

    log("\n" + "=" * 72)
    log("V2 FLOWS SUMMARY")
    log("=" * 72)
    for result in results:
        log(("PASS  " if result.passed else "FAIL  ") + result.name + "  " + result.title)
        for failure in result.failures:
            log("        - " + str(failure)[:600])
    log("\nchecks: " + str(recorder.passed) + "/" + str(recorder.checks) + " passed")
    screenshots = sorted(out_dir.glob("*.png")) if out_dir.exists() else []
    log("screenshots: " + str(out_dir))
    for shot in screenshots:
        log("  - " + shot.name)
    if recorder.failures:
        log("\nFAILURES (" + str(len(recorder.failures)) + "):")
        for failure in recorder.failures:
            log("  - [" + failure.where + "] " + failure.message[:800])
    if recorder.exceptions:
        log("\nUNCAUGHT EXCEPTIONS (" + str(len(recorder.exceptions)) + "):")
        for item in recorder.exceptions:
            log("  - " + item[:800])
    if recorder.console_errors:
        log("\nCONSOLE ERRORS (" + str(len(recorder.console_errors)) + "):")
        for item in recorder.console_errors:
            log("  - " + item[:800])
    if recorder.network_warnings:
        unexpected_network = [
            item
            for item in recorder.network_warnings
            if not ("409" in item and "/merge" in item)
        ]
        for item in unexpected_network:
            recorder.failures.append(
                Failure("network", "非预期网络错误（仅允许冲突流程的 409 merge）：" + item)
            )
        log("\nNETWORK WARNINGS (" + str(len(recorder.network_warnings)) + ", non-fatal):")
        seen: set[str] = set()
        for item in recorder.network_warnings:
            if item in seen:
                continue
            seen.add(item)
            log("  - " + item[:500])

    failed_flows = [result for result in results if not result.passed]
    hard = len(recorder.failures) + len(recorder.exceptions) + len(recorder.console_errors)
    ok = not failed_flows and hard == 0
    log("\nRESULT: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MultiAgent v2 real-browser interaction E2E (8 flows: merged / conflict / timeout / settings / review / new-task / duplicate-projects / missing-branch)"
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="screenshot directory")
    parser.add_argument("--timeout", type=float, default=25.0, help="per-step timeout seconds")
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=90.0,
        help="backend startup timeout seconds",
    )
    parser.add_argument("--edge", default=None, help="path to msedge.exe")
    parser.add_argument("--port", type=int, default=None, help="backend port (default: free port)")
    parser.add_argument("--keep-temp", action="store_true", help="keep the temporary home for debugging")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    temp_root = Path(tempfile.mkdtemp(prefix="multiagent-v2-flows-"))
    home = temp_root / "home"
    repo = temp_root / "project"
    duplicate_root = temp_root / "legacy-shared"
    profile = temp_root / "edge-profile"
    home.mkdir(parents=True, exist_ok=True)
    repo.mkdir(parents=True, exist_ok=True)
    plan_file = home / "plan-worker.json"
    write_config(home, plan_file, auto_merge=True)
    # 必须早于 start_backend：历史重复项目直接写进临时 home 的 SQLite，
    # 后端启动后 /api/projects 会照常返回两条同目录记录（API 会 409）。
    seed_duplicate_projects(home, duplicate_root)

    port = args.port or free_port()
    base_url = "http://127.0.0.1:" + str(port)
    log_path = temp_root / "backend.log"
    backend: subprocess.Popen[bytes] | None = None
    log_file = None
    recorder = Recorder()
    results: list[FlowResult] = []
    exit_code = 1
    try:
        log("temporary home: " + str(home))
        log("temporary repo: " + str(repo))
        log("temporary duplicate root: " + str(duplicate_root))
        backend, log_file = start_backend(home, port, log_path)
        if not wait_health(port, args.startup_timeout):
            recorder.failures.append(
                Failure("backend", "后端 /api/health 在 " + str(args.startup_timeout) + "s 内未就绪")
            )
            log("后端启动日志：")
            log(log_tail(log_path))
            return 2
        data = seed_environment(base_url, home, repo, plan_file)
        results = asyncio.run(
            run_browser(base_url, data, recorder, out_dir, args.timeout, args.edge, profile)
        )
        exit_code = summary(recorder, results, out_dir)
    except KeyboardInterrupt:
        log("interrupted")
        exit_code = 130
    except Exception as exc:  # noqa: BLE001
        recorder.failures.append(Failure("harness", type(exc).__name__ + ": " + str(exc)))
        log("\nHARNESS ERROR: " + type(exc).__name__ + ": " + str(exc))
        log("后端日志：")
        log(log_tail(log_path))
        exit_code = summary(recorder, results, out_dir)
    finally:
        if backend is not None:
            stop_process(backend, "backend")
        if log_file is not None:
            try:
                log_file.close()
            except OSError:
                pass
        if args.keep_temp:
            log("临时目录已保留：" + str(temp_root))
        else:
            if remove_tree(temp_root):
                log("临时 home / 后端 / Edge 已清理")
            else:
                log("  !! 临时目录未能完全清理：" + str(temp_root))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
