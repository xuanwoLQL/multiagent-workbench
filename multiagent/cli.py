"""命令行入口（需求 52：稳定的对外接口）。

    multiagent [--home DIR] [--json] <命令> ...

设计上 CLI 与 HTTP API 共享同一套服务，二者可互换。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .app import Workbench
from .agents.base import create_adapter
from .agents.resolve import resolve_agent_argv
from .util import loads, now_iso, read_json, to_posix
from .tasks.decompose import build_project_plan
from .tasks.planner import apply_plan as create_task_plan, validate_plan as validate_task_plan
from .tasks.profiles import infer_verify_command, is_exit_zero_only, prepare_creation_fields
from .workspace.hygiene import scan_workspace


# --------------------------------------------------------------------------- 辅助

def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(value, str):
        print(value)
        return
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _read_text_arg(text: str | None, path: str | None, default: str = "") -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    if text is not None:
        return text
    return default


def _resolve_task(app: Workbench, value: str):
    task = app.tasks.find(value)
    if not task:
        raise SystemExit(f"任务不存在：{value}")
    return task


def _resolve_project(app: Workbench, value: str):
    return app.projects.get(value)


# --------------------------------------------------------------------------- 命令

def cmd_init(app: Workbench, args) -> int:
    created = app.config.materialize(overwrite=args.force)
    app.config.ensure_dirs()
    _emit(
        {
            "home": str(app.config.home),
            "config_dir": str(app.config.config_dir),
            "data_dir": str(app.config.data_dir),
            "created": created,
            "db": str(app.config.db_path),
        },
        args.json,
    )
    return 0


def cmd_config(app: Workbench, args) -> int:
    _emit(app.config.summary(), True)
    return 0


def cmd_doctor(app: Workbench, args) -> int:
    """环境 / 配置 / Agent 自检（需求 3、51、52）：一条命令看清能不能开工。"""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    home = app.config.home
    add("home", home.exists(), str(home))
    add("config_dir", app.config.config_dir.exists(), str(app.config.config_dir))
    add("data_dir", app.config.data_dir.exists(), str(app.config.data_dir))
    try:
        add("database", bool(app.db.query_one("SELECT 1 AS x")), str(app.config.db_path))
    except Exception as exc:  # noqa: BLE001
        add("database", False, str(exc))
    try:
        check = app.db.integrity_check(full=False)
        add("db_integrity", bool(check.get("ok")), "; ".join(check.get("result") or [])[:200])
        add("schema_version", app.db.schema_version > 0, str(app.db.schema_version))
    except Exception as exc:  # noqa: BLE001
        add("db_integrity", False, str(exc))
    add("config_warnings", not app.config.warnings, "; ".join(app.config.warnings) or "无")
    errors = list(getattr(app.config, "errors", []) or [])
    add("config_errors", not errors, "; ".join(errors) or "无")

    for agent in ([] if getattr(args, "skip_agents", False) else app.config.agents):
        aid = str(agent.get("id"))
        enabled = agent.get("enabled", True)
        if agent.get("protocol") == "human":
            add(f"agent:{aid}", True, "人工执行者")
            continue
        try:
            resolved = resolve_agent_argv(agent)
        except Exception as exc:  # noqa: BLE001
            add(f"agent:{aid}", False, f"解析失败：{exc}")
            continue
        if not resolved:
            add(f"agent:{aid}", False, "未找到可执行文件（未安装 / 路径不对）")
            continue
        command, argv = resolved
        detail = f"{command} {' '.join(argv)}".strip() + ("" if enabled else "（已停用）")
        add(f"agent:{aid}", True, detail)

    ports = app.config.policy_get("concurrency", {}) or {}
    add("concurrency", True, f"global={ports.get('global', 6)} per_project={ports.get('per_project', 6)}")
    add("projects", True, f"{len(app.projects.list())} 个项目")

    ok = all(c["ok"] for c in checks)
    if args.json:
        _emit({"ok": ok, "checks": checks}, True)
    else:
        for c in checks:
            mark = "OK  " if c["ok"] else "FAIL"
            print(f"[{mark}] {c['check']:<20} {c['detail']}")
        print(f"\n总体：{'通过' if ok else '存在问题'}")
    return 0 if ok else 1


def cmd_storage(app: Workbench, args) -> int:
    _emit(app.ops.storage(), True)
    return 0


def cmd_backup(app: Workbench, args) -> int:
    _emit(app.ops.create_backup(args.label or "manual", actor="human"), True)
    return 0


def cmd_restore(app: Workbench, args) -> int:
    # 先保守恢复，确认没有「活进程仍在跑」的调用；有则必须显式 --force。
    app.engine.recover()
    running = app.db.query_one("SELECT COUNT(*) AS c FROM runs WHERE status = 'running'")
    if running and int(running["c"]) > 0 and not args.force:
        raise ValueError(
            f"仍有 {int(running['c'])} 个运行中的调用（可能来自其他进程）；"
            "确认它们已停止后使用 --force 恢复"
        )
    _emit(app.ops.restore(args.path, backup_current=not args.no_backup), True)
    return 0


def cmd_maintenance(app: Workbench, args) -> int:
    result = app.ops.prune(
        dry_run=not args.apply,
        keep_runs_per_task=args.keep_runs,
        keep_days=args.keep_days,
        keep_snapshots=args.keep_snapshots,
        keep_backups=args.keep_backups,
        prune_workspaces_days=args.keep_workspaces_days,
        prune_blobs=not args.no_blobs,
    )
    _emit(result, True)
    return 0


def cmd_verify(app: Workbench, args) -> int:
    result = app.ops.verify(full=bool(args.full))
    _emit(result, True)
    return 0 if result.get("ok") else 1


# ------------------------------------------------------------------ agent

def cmd_agent(app: Workbench, args) -> int:
    if args.action == "list":
        items = []
        for agent in app.config.agents:
            resolved = resolve_agent_argv(agent) if agent.get("protocol") != "human" else ("(human)", [])
            items.append(
                {
                    "id": agent.get("id"),
                    "label": agent.get("label"),
                    "protocol": agent.get("protocol"),
                    "enabled": agent.get("enabled", True),
                    "command": resolved[0] if resolved else None,
                    "args": resolved[1] if resolved else [],
                    "roles": agent.get("roles", []),
                    "strengths": agent.get("strengths", []),
                }
            )
        _emit(items, args.json)
        return 0
    if args.action == "check":
        targets = [args.id] if args.id else [a.get("id") for a in app.config.agents]
        results = []
        for agent_id in targets:
            agent = app.config.agent(agent_id)
            if not agent:
                results.append({"id": agent_id, "ok": False, "detail": "未配置"})
                continue
            adapter = create_adapter(agent, resolve_agent_argv)
            results.append({"id": agent_id, **adapter.check()})
        _emit(results, args.json)
        return 0 if all(r.get("ok") for r in results) else 1
    if args.action == "quota":
        _emit(app.quota.summary(), args.json)
        return 0
    raise SystemExit("未知 agent 子命令")


# ------------------------------------------------------------------ project

def cmd_project(app: Workbench, args) -> int:
    if args.action == "create":
        project = app.projects.create(args.name, label=args.label or args.name, root_path=args.root)
        app.projects.write_meta(project)
        _emit(project, args.json)
        return 0
    if args.action == "import":
        project = app.projects.import_dir(args.path, name=args.name, label=args.label or "")
        app.projects.write_meta(project)
        _emit(project, args.json)
        return 0
    if args.action == "list":
        _emit(app.projects.list(), args.json)
        return 0
    if args.action == "delete":
        project = _resolve_project(app, args.project or args.target)
        running = app.engine._running_run_ids(project_id=project["id"])  # noqa: SLF001
        if running and not getattr(args, "force", False):
            raise ValueError(
                f"项目仍有 {len(running)} 个运行中的 Agent 调用；请先停止，或加 --force 强制删除"
            )
        if running:
            app.engine.stop_project(project["id"], "项目被强制删除")
        _emit(app.projects.delete(project["id"]), args.json)
        return 0
    if args.action == "show":
        project = _resolve_project(app, args.project)
        _emit({"project": project, "progress": app.tasks.progress(project["id"])}, args.json)
        return 0
    if args.action == "hygiene":
        project = _resolve_project(app, args.project or args.target)
        report = scan_workspace(
            project["root_path"],
            config=app.config,
            max_entries=int(app.config.policy_get("workspace.hygiene_max_entries", 20000) or 20000),
        )
        _emit(report, args.json)
        return 0 if report.get("ok") else 1
    if args.action == "snapshot":
        project = _resolve_project(app, args.project)
        snap = app.snapshots.create(project["id"], project["root_path"], label=args.label or now_iso())
        app.board.append_event(project_id=project["id"], kind="snapshot.create", summary=f"创建快照 {snap['id']}", actor="human")
        _emit(snap, args.json)
        return 0
    if args.action == "snapshots":
        project = _resolve_project(app, args.project)
        _emit(app.snapshots.list(project["id"]), args.json)
        return 0
    if args.action == "restore":
        snap = app.snapshots.get(args.snapshot)
        project = app.projects.get(snap["project_id"])
        running = app.engine._running_run_ids(project_id=project["id"])  # noqa: SLF001
        if running and not getattr(args, "force", False):
            raise ValueError(
                f"项目仍有 {len(running)} 个运行中的 Agent 调用；请先停止，或加 --force 强制回退"
            )
        if running:
            app.engine.stop_project(project["id"], "回退快照前强制停止")
        result = app.snapshots.restore(args.snapshot, project["root_path"], project_id=project["id"])
        _emit(result, args.json)
        return 0
    if args.action == "diff":
        if args.current:
            snap = app.snapshots.get(args.a)
            project = app.projects.get(snap["project_id"])
            _emit(app.snapshots.diff_current(args.a, project["root_path"]), args.json)
        else:
            _emit(app.snapshots.diff(args.a, args.b), args.json)
        return 0
    raise SystemExit("未知 project 子命令")


# ------------------------------------------------------------------ task

def cmd_task(app: Workbench, args) -> int:
    action = args.action
    if action == "add":
        project = _resolve_project(app, args.project)
        brief = _read_text_arg(args.brief, args.brief_file)
        acceptance = loads(args.acceptance_json, None) if args.acceptance_json else None
        if acceptance is not None and not isinstance(acceptance, list):
            raise ValueError("--acceptance-json 必须是对象数组")
        if acceptance == []:
            raise ValueError("--acceptance-json 不能为空；未指定时省略该参数")
        outputs = args.outputs or []
        if is_exit_zero_only(acceptance) and not outputs and not getattr(args, "confirm_exit_zero_only", False):
            raise ValueError(
                "验收条件只有 exit_zero：请提供 --outputs，或加 --confirm-exit-zero-only 明确接受执行期文件改动检查"
            )
        kind = args.kind or "generic"
        fields = prepare_creation_fields(
            app.config,
            kind,
            outputs=outputs,
            isolation=args.isolation,
            read_only=bool(args.read_only),
            write_scope=args.scope,
            timeout_sec=args.timeout,
            max_attempts=args.max_attempts,
            acceptance=acceptance,
            acceptance_ref=args.acceptance_ref,
            verify_command=getattr(args, "verify_command", None)
            or (infer_verify_command(project["root_path"]) if kind == "code" else None),
            allow_shared_write=bool(getattr(args, "allow_shared_write", False)),
            allow_unrestricted_write=bool(getattr(args, "allow_unrestricted_write", False)),
        )
        resolved_acceptance = fields["acceptance"]
        if args.template and acceptance is None and args.acceptance_ref is None:
            resolved_acceptance = None
            fields["context"]["acceptance_guard"] = ""
        task = app.tasks.create(
            project["id"],
            args.title,
            kind=kind,
            brief=brief,
            template_id=args.template,
            agent_id=args.agent,
            role=args.role,
            isolation=fields["isolation"],
            read_only=True if args.read_only else None,
            write_scope=fields["write_scope"],
            acceptance=resolved_acceptance,
            acceptance_ref=args.acceptance_ref,
            inputs=args.inputs,
            outputs=outputs,
            depends_on=args.after or [],
            priority=args.priority,
            max_attempts=fields["max_attempts"],
            timeout_sec=fields["timeout_sec"],
            tags=args.tag,
            context=fields["context"],
        )
        _emit(task, args.json)
        return 0
    if action == "add-plan":
        project = _resolve_project(app, args.project)
        spec = read_json(args.file, {})
        created = _apply_plan(app, project["id"], spec)
        _emit(created, args.json)
        return 0
    if action == "import-plan":
        project = _resolve_project(app, args.project)
        spec = read_json(args.file, {})
        created = _apply_plan(app, project["id"], spec)
        _emit(created, args.json)
        return 0
    if action == "decompose":
        project = _resolve_project(app, args.project)
        brief = _read_text_arg(args.brief, args.brief_file)
        if not brief.strip():
            raise ValueError("decompose 需要 --brief 或 --brief-file")
        plan = build_project_plan(
            app.config,
            project["root_path"],
            brief,
            title=args.title or f"大型项目：{project.get('label') or project['name']}",
            source_files=args.source_files,
            agent_override=args.agent,
        )
        if not getattr(args, "apply", False):
            _emit(plan, args.json)
            return 0
        created = create_task_plan(app.tasks, project["id"], plan)
        _emit(created, args.json)
        return 0
    if action == "list":
        project = _resolve_project(app, args.project)
        tasks = app.tasks.list(project["id"], status=args.status, kind=args.kind, agent_id=args.agent)
        if args.json:
            _emit(tasks, True)
        else:
            for task in tasks:
                print(f"{task['id']}  {task['status']:<11} {task['kind']:<8} {task['agent_id'] or '-':<10} {task['title']}")
        return 0
    if action == "show":
        task = _resolve_task(app, args.task)
        detail = {
            "task": task,
            "dependencies": app.tasks.dependencies(task["id"]),
            "history": app.tasks.history(task["id"]),
            "runs": app.audit.runs(task_id=task["id"]),
            "artifacts": app.tasks.artifacts(task_id=task["id"]),
            "handoff": app.handoffs.latest_for_task(task["id"]),
        }
        _emit(detail, args.json)
        return 0
    if action == "plan":
        project = _resolve_project(app, args.project)
        _emit(app.tasks.plan(project["id"]), args.json)
        return 0
    if action == "status":
        project = _resolve_project(app, args.project)
        _emit(app.tasks.progress(project["id"]), args.json)
        return 0
    if action == "run":
        task = _resolve_task(app, args.task)
        outcome = app.engine.run_task(task["id"], agent_id=args.agent)
        _emit(outcome.__dict__, args.json)
        return 0 if outcome.status == "success" else 1
    if action == "run-project":
        project = _resolve_project(app, args.project)
        result = app.engine.run_project(
            project["id"], only=args.only, auto_retry=not args.no_retry, max_parallel=args.parallel, max_seconds=args.seconds
        )
        _emit(result, args.json)
        return 0
    if action == "fanout":
        task = _resolve_task(app, args.task)
        agents = [a for a in (args.agents or "").split(",") if a]
        result = app.engine.fanout(task["id"], agents)
        _emit(result, args.json)
        return 0
    if action == "retry":
        _emit(app.engine.retry(_resolve_task(app, args.task)["id"]), args.json)
        return 0
    if action == "timeout":
        task = _resolve_task(app, args.task or args.target)
        if not args.timeout or args.timeout <= 0:
            raise ValueError("timeout 需要 --timeout <秒>（大于 0）")
        updated = app.tasks.update(task["id"], timeout_sec=args.timeout)
        _emit(
            {
                "task": updated,
                "applies_to": "next_attempt" if task["status"] == "running" else "next_run",
            },
            args.json,
        )
        return 0
    if action == "skip":
        _emit(app.engine.skip(_resolve_task(app, args.task)["id"], args.reason or "人工跳过"), args.json)
        return 0
    if action == "cancel":
        _emit(app.engine.cancel(_resolve_task(app, args.task)["id"], args.reason or "人工取消"), args.json)
        return 0
    if action == "takeover":
        _emit(app.engine.takeover(_resolve_task(app, args.task)["id"], args.agent or "human"), args.json)
        return 0
    if action == "provide":
        _emit(
            app.engine.provide(
                _resolve_task(app, args.task)["id"],
                did=args.did or "",
                artifacts=args.artifact or [],
                notes=args.notes or "",
            ),
            args.json,
        )
        return 0
    if action == "resume":
        project = _resolve_project(app, args.project)
        app.engine.recover(project["id"], force=bool(getattr(args, "force", False)))
        result = app.engine.run_project(project["id"], max_parallel=args.parallel)
        _emit(result, args.json)
        return 0
    if action == "changes":
        task = _resolve_task(app, args.task)
        rows = app.db.query("SELECT * FROM file_changes WHERE task_id = ? ORDER BY id", (task["id"],))
        _emit([dict(r) for r in rows], args.json)
        return 0
    if action == "revert":
        task = _resolve_task(app, args.task)
        if task["status"] == "running":
            raise ValueError("任务仍在执行；请先取消，再回滚改动")
        project = app.projects.get(task["project_id"])
        stats = app.runner.journal.revert_task(task["id"], workspace_root=Path(project["root_path"]))
        app.tasks.set_status(task["id"], "pending", "人工回滚改动")
        _emit(stats, args.json)
        return 0
    if action == "sync":
        task = _resolve_task(app, args.task)
        project = app.projects.get(task["project_id"])
        from .api.taskworkspace import SyncBlocked, sync_task_workspace

        try:
            result = sync_task_workspace(app, task, project, dry_run=bool(args.dry_run))
        except SyncBlocked as exc:
            raise ValueError(str(exc)) from exc
        _emit(result, args.json)
        return 0
    if action == "approve":
        task = _resolve_task(app, args.task)
        note = args.note or "人工验收通过（保留 run 现场）"
        from .api.taskworkspace import SyncBlocked

        try:
            result = app.engine.approve(task["id"], note=note, sync=bool(args.sync))
        except SyncBlocked as exc:
            raise ValueError(str(exc)) from exc
        _emit(result, args.json)
        return 0
    if action == "delete":
        task = _resolve_task(app, args.task)
        if task["status"] == "running":
            raise ValueError("任务仍在执行；请先取消，再删除")
        result = app.tasks.delete(task["id"])
        app.isolation.purge_task(task["project_id"], task["id"])
        _emit(result, args.json)
        return 0
    raise SystemExit("未知 task 子命令")


def _apply_plan(app: Workbench, project_id: str, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """按计划文件批量建任务，key 映射到真实任务 ID 以支持依赖。

    先做**全量预校验**（key 唯一、依赖存在、无自依赖、无环），再在一个事务里创建；
    任何一步失败都整体回滚，绝不留下「建了一半」的计划。
    """
    return create_task_plan(app.tasks, project_id, spec)


def _validate_plan(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """计划文件的结构与 DAG 合法性校验（API 与 CLI 共用）。"""
    return validate_task_plan(spec)


# ------------------------------------------------------------------ board

def cmd_board(app: Workbench, args) -> int:
    action = args.action
    if action == "post":
        project = _resolve_project(app, args.project)
        entry = app.board.post(
            project["id"],
            type=args.type,
            title=args.title or "",
            body=_read_text_arg(args.body, args.body_file),
            scope=args.scope,
            task_id=args.task,
            agent_id=args.agent,
            tags=args.tags,
            status=args.status,
            pinned=bool(args.pin),
            actor=args.actor or "human",
        )
        _emit(entry, args.json)
        return 0
    if action == "list":
        project = _resolve_project(app, args.project)
        _emit(
            app.board.list(project["id"], type=args.type, scope=args.scope, status=args.status, task_id=args.task, agent_id=args.agent),
            args.json,
        )
        return 0
    if action == "search":
        project = _resolve_project(app, args.project)
        _emit(
            app.board.search(
                project["id"], args.query, task_id=args.task, agent_id=args.agent, type=args.type, since=args.since, until=args.until
            ),
            args.json,
        )
        return 0
    if action == "show":
        _emit({"entry": app.board.get(args.entry), "versions": app.board.versions(args.entry)}, args.json)
        return 0
    if action == "edit":
        entry = app.board.edit(
            args.entry,
            title=args.title,
            body=_read_text_arg(args.body, args.body_file, default=None) if (args.body or args.body_file) else None,
            tags=args.tags,
            author=args.actor or "human",
            note=args.note,
        )
        _emit(entry, args.json)
        return 0
    if action == "review":
        entry = app.board.review(args.entry, approved=bool(args.approve), reviewer=args.actor or "human", note=args.note)
        _emit(entry, args.json)
        return 0
    if action == "versions":
        _emit(app.board.versions(args.entry), args.json)
        return 0
    if action == "memory":
        project = _resolve_project(app, args.project)
        _emit(app.board.long_term_memory(project["id"]), args.json)
        return 0
    if action == "pending":
        project = _resolve_project(app, args.project)
        _emit(app.board.pending_reviews(project["id"]), args.json)
        return 0
    if action == "events":
        project = _resolve_project(app, args.project)
        _emit(app.board.read_events(project["id"], after_id=args.after or 0, limit=args.limit or 200), args.json)
        return 0
    raise SystemExit("未知 board 子命令")


# ------------------------------------------------------------------ handoff

def cmd_handoff(app: Workbench, args) -> int:
    if args.action == "list":
        project = _resolve_project(app, args.project)
        _emit(app.handoffs.list_for_project(project["id"]), args.json)
        return 0
    if args.action == "show":
        task = _resolve_task(app, args.task)
        _emit(app.handoffs.latest_for_task(task["id"]), args.json)
        return 0
    if args.action == "edit":
        patch: dict[str, Any] = {}
        if args.did is not None:
            patch["did"] = args.did
        if args.not_done is not None:
            patch["not_done"] = args.not_done
        if args.next:
            patch["next_steps"] = args.next
        if args.issue:
            patch["open_issues"] = args.issue
        if args.notes is not None:
            patch["notes"] = args.notes
        if args.file:
            patch.update(read_json(args.file, {}) or {})
        _emit(app.handoffs.edit(args.handoff, patch, editor=args.actor or "human"), args.json)
        return 0
    raise SystemExit("未知 handoff 子命令")


# ------------------------------------------------------------------ approval

def cmd_approval(app: Workbench, args) -> int:
    if args.action == "list":
        project = _resolve_project(app, args.project) if args.project else None
        _emit(app.guard.list(project["id"] if project else None, status=args.status), args.json)
        return 0
    if args.action == "resolve":
        _emit(
            app.guard.resolve(
                args.approval, approved=bool(args.approve), by=args.actor or "human", resolution=args.note, board=app.board
            ),
            args.json,
        )
        return 0
    raise SystemExit("未知 approval 子命令")


# ------------------------------------------------------------------ 观测

def cmd_stats(app: Workbench, args) -> int:
    project = _resolve_project(app, args.project) if args.project else None
    _emit(app.audit.stats(project["id"] if project else None), args.json)
    return 0


def cmd_runs(app: Workbench, args) -> int:
    project_id = None
    if args.project:
        project_id = _resolve_project(app, args.project)["id"]
    _emit(
        app.audit.runs(project_id=project_id, task_id=args.task, agent_id=args.agent, limit=args.limit or 200),
        args.json,
    )
    return 0


def cmd_timeline(app: Workbench, args) -> int:
    project = _resolve_project(app, args.project)
    if args.out:
        path = app.audit.export_timeline(project["id"], args.out, fmt=args.format)
        _emit({"path": path, "count": len(app.audit.timeline(project["id"]))}, args.json)
    else:
        _emit(app.audit.timeline(project["id"]), args.json)
    return 0


def cmd_compare(app: Workbench, args) -> int:
    task = _resolve_task(app, args.task)
    _emit(app.audit.compare_runs(task["id"]), args.json)
    return 0


def cmd_serve(app: Workbench, args) -> int:
    from .api.app import serve

    serve(
        app,
        host=args.host,
        port=args.port,
        open_browser=args.open,
        token=args.token,
        strict=args.strict_auth or None,
    )
    return 0


# --------------------------------------------------------------------------- 解析器

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="multiagent", description="多 Agent 协作完成任意文件型工作台")
    parser.add_argument("--home", help="工作台主目录（默认 MULTIAGENT_HOME 或当前目录）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="初始化配置与数据目录")
    p.add_argument("--force", action="store_true", help="覆盖已有配置文件")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("config", help="打印配置摘要")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="环境 / 配置 / Agent 自检")
    p.add_argument("--skip-agents", action="store_true", help="跳过 Agent 可执行文件检查（CI 无 Agent CLI 时使用）")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("storage", help="存储盘点（数据库 / 运行证据 / 快照 / 日志 / 备份）")
    p.set_defaults(func=cmd_storage)

    p = sub.add_parser("backup", help="创建数据库一致性备份")
    p.add_argument("--label", default="manual")
    p.set_defaults(func=cmd_backup)

    p = sub.add_parser("restore", help="从备份恢复数据库（进程内热恢复）")
    p.add_argument("path")
    p.add_argument("--force", action="store_true", help="确认其他进程的运行中调用已停止")
    p.add_argument("--no-backup", action="store_true", help="恢复前不备份当前库（不推荐）")
    p.set_defaults(func=cmd_restore)

    p = sub.add_parser("maintenance", help="保留清理（默认 dry-run）")
    p.add_argument("--apply", action="store_true", help="真正执行删除（默认只报告）")
    p.add_argument("--keep-runs", type=int, default=3)
    p.add_argument("--keep-days", type=float, default=30)
    p.add_argument("--keep-snapshots", type=int, default=20)
    p.add_argument("--keep-backups", type=int, default=20)
    p.add_argument("--keep-workspaces-days", type=float, default=30)
    p.add_argument("--no-blobs", action="store_true", help="不回收未引用的 blob")
    p.set_defaults(func=cmd_maintenance)

    p = sub.add_parser("verify", help="数据库完整性 + 事件哈希链校验")
    p.add_argument("--full", action="store_true", help="执行完整 integrity_check（较慢）")
    p.set_defaults(func=cmd_verify)

    # agent
    p = sub.add_parser("agent", help="Agent 管理")
    p.add_argument("action", choices=["list", "check", "quota"])
    p.add_argument("id", nargs="?")
    p.set_defaults(func=cmd_agent)

    # project
    p = sub.add_parser("project", help="项目 / 工作区管理")
    p.add_argument("action", choices=["create", "import", "list", "show", "delete", "snapshot", "snapshots", "restore", "diff", "hygiene"])
    p.add_argument("target", nargs="?", help="项目名或 ID")
    p.add_argument("path", nargs="?", help="import 的源目录")
    p.add_argument("--name")
    p.add_argument("--label")
    p.add_argument("--root")
    p.add_argument("--project")
    p.add_argument("--snapshot")
    p.add_argument("a", nargs="?")
    p.add_argument("b", nargs="?")
    p.add_argument("--current", action="store_true")
    p.add_argument("--force", action="store_true", help="delete：仍有运行中调用时强制删除")
    p.set_defaults(func=cmd_project)

    # task
    p = sub.add_parser("task", help="任务与编排")
    p.add_argument("action",
        choices=["add", "add-plan", "import-plan", "decompose", "list", "show", "plan", "status", "run", "run-project", "fanout", "retry", "timeout", "skip", "cancel", "takeover", "provide", "resume", "changes", "revert", "sync", "approve", "delete"],
    )
    p.add_argument("target", nargs="?")
    p.add_argument("extra", nargs="?", help="task add 的标题等第二位置参数")
    p.add_argument("--project")
    p.add_argument("--title")
    p.add_argument("--kind", default=None)
    p.add_argument("--template")
    p.add_argument("--brief")
    p.add_argument("--brief-file")
    p.add_argument("--agent")
    p.add_argument("--role")
    p.add_argument("--isolation", choices=["shared", "copy", "worktree"])
    p.add_argument("--read-only", action="store_true")
    p.add_argument("--scope", action="append")
    p.add_argument("--inputs", action="append")
    p.add_argument("--outputs", action="append")
    p.add_argument("--after", action="append")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--max-attempts", type=int, default=None)
    p.add_argument("--timeout", type=int)
    p.add_argument("--tag", action="append")
    p.add_argument("--acceptance-json")
    p.add_argument("--acceptance-ref")
    p.add_argument("--verify-command")
    p.add_argument("--allow-shared-write", action="store_true")
    p.add_argument("--allow-unrestricted-write", action="store_true")
    p.add_argument("--confirm-exit-zero-only", action="store_true")
    p.add_argument("--source", action="append", dest="source_files", help="decompose：需求 PDF/文本路径（相对项目根）")
    p.add_argument("--apply", action="store_true", help="decompose：确认后直接创建 DAG")
    p.add_argument("--status")
    p.add_argument("--task")
    p.add_argument("--file")
    p.add_argument("--agents")
    p.add_argument("--only", action="append")
    p.add_argument("--parallel", type=int)
    p.add_argument(
        "--force",
        action="store_true",
        help="resume 时强制回收现场：无视「对端会话是否仍存活」的判定。"
             "仅在你确认上次的调度进程已经死了（例如被强杀、PID 已被复用）时使用；"
             "若本机确实还有活着的调度方，强制回收会把它正在跑的任务判为中断。",
    )
    p.add_argument("--seconds", type=float)
    p.add_argument("--no-retry", action="store_true")
    p.add_argument("--reason")
    p.add_argument("--did")
    p.add_argument("--artifact", action="append")
    p.add_argument("--notes")
    p.add_argument("--dry-run", action="store_true", help="sync：只预览将同步哪些文件")
    p.add_argument("--sync", action="store_true", help="approve：先同步工作副本再标记成功")
    p.add_argument("--note", help="approve：人工验收备注")
    p.set_defaults(func=cmd_task)

    # board
    p = sub.add_parser("board", help="共享黑板与记忆")
    p.add_argument("action", choices=["post", "list", "search", "show", "edit", "review", "versions", "memory", "pending", "events"])
    p.add_argument("target", nargs="?")
    p.add_argument("query", nargs="?")
    p.add_argument("--project")
    p.add_argument("--entry")
    p.add_argument("--type")
    p.add_argument("--title")
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--scope", default="task")
    p.add_argument("--status")
    p.add_argument("--task")
    p.add_argument("--agent")
    p.add_argument("--actor")
    p.add_argument("--tags", action="append")
    p.add_argument("--pin", action="store_true")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--note")
    p.add_argument("--since")
    p.add_argument("--until")
    p.add_argument("--after", type=int)
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_board)

    # handoff
    p = sub.add_parser("handoff", help="交接说明")
    p.add_argument("action", choices=["list", "show", "edit"])
    p.add_argument("target", nargs="?")
    p.add_argument("--project")
    p.add_argument("--task")
    p.add_argument("--handoff")
    p.add_argument("--did")
    p.add_argument("--not-done")
    p.add_argument("--next", action="append")
    p.add_argument("--issue", action="append")
    p.add_argument("--notes")
    p.add_argument("--file")
    p.add_argument("--actor")
    p.set_defaults(func=cmd_handoff)

    # approval
    p = sub.add_parser("approval", help="人工确认队列")
    p.add_argument("action", choices=["list", "resolve"])
    p.add_argument("target", nargs="?")
    p.add_argument("--project")
    p.add_argument("--status")
    p.add_argument("--approval")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--note")
    p.add_argument("--actor")
    p.set_defaults(func=cmd_approval)

    # observability
    p = sub.add_parser("stats", help="成本 / 耗时汇总")
    p.add_argument("--project")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("runs", help="Agent 调用记录")
    p.add_argument("--project")
    p.add_argument("--task")
    p.add_argument("--agent")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("timeline", help="协作时间线")
    p.add_argument("--project")
    p.add_argument("--out")
    p.add_argument("--format", default="markdown", choices=["markdown", "json"])
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("compare", help="对比不同 Agent 对同一任务的产出")
    p.add_argument("--task")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("serve", help="启动 Web 看板与 API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--open", action="store_true")
    p.add_argument("--token", default=None, help="API Bearer token（默认取 MULTIAGENT_API_TOKEN 或 .multiagent/token）")
    p.add_argument(
        "--strict-auth",
        action="store_true",
        help="loopback 绑定也强制校验 token（默认仅非 loopback 强制）",
    )
    p.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    raw = list(argv if argv is not None else sys.argv[1:])
    # 允许把全局开关写在子命令之后：统一前移到最前（只保留一份，避免重复解析）
    if "--json" in raw:
        raw = ["--json"] + [item for item in raw if item != "--json"]
    args = parser.parse_args(raw)
    # project/task 等子命令里 target 的语义按动作补齐
    args = _normalize(args)
    app = Workbench.open(args.home)
    try:
        return args.func(app, args)
    except (KeyError, LookupError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    finally:
        app.close()


def _normalize(args) -> Any:
    """把位置参数映射到各动作真正需要的字段。"""
    cmd = getattr(args, "command", None)
    if cmd == "project":
        action = args.action
        if action == "create":
            args.name = args.name or args.target
        elif action == "import":
            args.path = args.path or args.target
        elif action in ("show", "delete", "snapshot", "snapshots"):
            args.project = getattr(args, "project", None) or args.target
        elif action == "restore":
            args.snapshot = args.snapshot or args.target
        elif action == "diff":
            args.a = args.a or args.target
            args.b = args.b or args.path
    if cmd == "task":
        if args.action in ("add", "add-plan", "import-plan", "list", "plan", "status", "run-project", "resume"):
            args.project = args.project or args.target
        if args.action == "add":
            args.title = args.title or args.extra or args.target
        if args.action in ("show", "run", "fanout", "retry", "skip", "cancel", "takeover", "provide", "changes", "revert", "sync", "approve", "delete"):
            args.task = args.task or args.target
    if cmd == "board":
        if args.action == "post":
            args.project = args.project or args.target
            if args.query:  # 允许 board post <project> <title> 的简写
                args.title = args.title or args.query
        elif args.action == "search":
            args.project = args.project or args.target
        elif args.action in ("show", "edit", "review", "versions"):
            args.entry = args.entry or args.target
        else:
            args.project = args.project or args.target
    if cmd == "handoff":
        if args.action == "list":
            args.project = args.project or args.target
        else:
            args.task = args.task or args.target
            args.handoff = args.handoff or args.target
    if cmd == "approval":
        if args.action == "list":
            args.project = args.project or args.target
        else:
            args.approval = args.approval or args.target
    return args


if __name__ == "__main__":
    sys.exit(main())
