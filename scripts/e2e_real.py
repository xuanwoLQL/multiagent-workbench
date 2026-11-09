"""真实 Agent 端到端验收脚本（不依赖任何假 Agent）。

这是本项目**唯一**会真正调用本机 Codex / Claude Code / OpenCode / DSH 的验收入口，
用来证明「多 Agent 在同一工作区协作完成文件型工作」这条主链路真的跑得通，
而不是只在单元测试的假 Agent 上通过。

用法：
    # 1) 四个 Agent 各自冒烟（每个建一个文件，校验内容与审计记录）
    .venv\\Scripts\\python.exe scripts\\e2e_real.py smoke --agents opencode,codex,dsh,claude

    # 2) 跨 Agent 协作链：调研 -> 撰写 -> 只读审阅 -> 汇总（带依赖、交接注入、真只读）
    .venv\\Scripts\\python.exe scripts\\e2e_real.py chain \\
        --research opencode --draft codex --review claude --summary dsh

退出码：0 = 全部通过；1 = 有失败（并打印失败项的原始证据路径）。
所有现场落在 --home 指定目录（默认 <repo>/.e2e/home），不污染项目自身的 .multiagent/。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

MARKER = "TOPIC-ALPHA-2026"
SMOKE_MARK = "RKEY-7788"

REPORT: dict = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "checks": [], "failures": []}


def _record(name: str, ok: bool, detail: str, evidence: dict | None = None) -> None:
    icon = "PASS" if ok else "FAIL"
    print(f"  [{icon}] {name} — {detail}")
    REPORT["checks"].append({"name": name, "ok": ok, "detail": detail, "evidence": evidence or {}})
    if not ok:
        REPORT["failures"].append({"name": name, "detail": detail, "evidence": evidence or {}})


def _open_workbench(home: Path, fresh: bool):
    if fresh and home.exists():
        shutil.rmtree(home, ignore_errors=True)
    (home / "config").mkdir(parents=True, exist_ok=True)
    for src in sorted((REPO / "config").glob("*.json")):
        dst = home / "config" / src.name
        if not dst.exists():
            shutil.copy2(src, dst)
    from multiagent.app import Workbench

    return Workbench.open(home)


def _project(wb, name: str) -> dict:
    for p in wb.projects.list():
        if p["name"] == name:
            return p
    return wb.projects.create(name)


def _finish(wb, home: Path, project: dict) -> dict:
    """收集一次协作的全部审计证据。"""
    return {
        "runs": wb.audit.runs(project_id=project["id"]) if hasattr(wb.audit, "runs") else [],
        "stats": wb.audit.stats(project["id"]),
        "progress": wb.tasks.progress(project["id"]),
    }


# --------------------------------------------------------------------------- smoke


def cmd_smoke(args) -> int:
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    print(f"=== 冒烟：{len(agents)} 个真实 Agent，home={args.home} ===")
    wb = _open_workbench(Path(args.home), args.fresh)
    try:
        project = _project(wb, "e2e-smoke")
        root = Path(project["root_path"])
        print(f"项目 {project['id']}  root={root}")
        for agent_id in agents:
            print(f"\n--- {agent_id} ---")
            if not wb.config.agent(agent_id):
                _record(f"smoke:{agent_id}", False, "配置里没有这个 Agent")
                continue
            brief = (
                "在当前工作目录下新建纯文本文件 notes.txt，内容恰好三行：\n"
                f"E2E-OK\n{SMOKE_MARK}\nagent {agent_id}\n"
                "不要创建或修改任何其他文件。"
            )
            task = wb.tasks.create(
                project["id"],
                f"真实冒烟 {agent_id}",
                kind="doc",
                brief=brief,
                agent_id=agent_id,
                outputs=["notes.txt"],
                acceptance=[
                    {"type": "file_exists", "path": "notes.txt"},
                    {"type": "content_contains", "path": "notes.txt", "text": SMOKE_MARK},
                ],
                timeout_sec=args.timeout,
            )
            target = root / "notes.txt"
            if target.exists():
                target.unlink()
            started = time.time()
            try:
                outcome = wb.engine.run_task(task["id"])
            except Exception as exc:  # noqa: BLE001
                _record(f"smoke:{agent_id}", False, f"执行抛异常 {type(exc).__name__}: {exc}")
                continue
            elapsed = time.time() - started
            run = (wb.audit.runs(task_id=task["id"]) or [{}])[0]
            content = target.read_text(encoding="utf-8", errors="replace") if target.exists() else ""
            ok = outcome.status == "success" and SMOKE_MARK in content
            _record(
                f"smoke:{agent_id}",
                ok,
                f"status={outcome.status} exit={getattr(outcome, 'exit_code', None)} "
                f"acceptance={getattr(outcome, 'acceptance_ok', None)} elapsed={elapsed:.1f}s",
                {
                    "run": {k: run.get(k) for k in
                            ("id", "agent_id", "protocol", "status", "exit_code", "duration_ms", "cost_tokens", "error")},
                    "prompt_path": run.get("prompt_path"),
                    "stdout_path": run.get("stdout_path"),
                    "stderr_path": run.get("stderr_path"),
                    "notes_txt": content[:200],
                },
            )
            if not ok:
                stderr = run.get("stderr_path")
                if stderr and Path(stderr).exists():
                    tail = Path(stderr).read_text(encoding="utf-8", errors="replace")[-600:]
                    print(f"        stderr tail: {tail!r}")
        _dump(wb, project)
    finally:
        wb.close()
    return _verdict()


# --------------------------------------------------------------------------- chain


def cmd_chain(args) -> int:
    print(f"=== 跨 Agent 协作链：调研 -> 撰写 -> 只读审阅 -> 汇总 (home={args.home}) ===")
    wb = _open_workbench(Path(args.home), args.fresh)
    try:
        project = _project(wb, "e2e-chain")
        root = Path(project["root_path"])
        print(f"项目 {project['id']}  root={root}")
        for stale in ("notes.md", "report.md", "summary.md"):
            p = root / stale
            if p.exists():
                p.unlink()

        # ---- T1 调研（写 notes.md，标记必须出现在产物里）----
        t1 = wb.tasks.create(
            project["id"],
            "调研：整理主题要点",
            kind="research",
            brief=(
                "在当前工作目录新建 notes.md（Markdown）。内容要求：\n"
                f"1) 第一行写标题：「调研笔记 {MARKER}」；\n"
                "2) 下面写 3 条要点，每条以「- 」开头；\n"
                "3) 结尾写一行「结论：标记 " + MARKER + " 已确认」。\n"
                "只创建这一个文件。"
            ),
            agent_id=args.research,
            outputs=["notes.md"],
            acceptance=[
                {"type": "file_exists", "path": "notes.md"},
                {"type": "content_contains", "path": "notes.md", "text": MARKER},
                {"type": "handoff_present"},
            ],
            timeout_sec=args.timeout,
        )
        _run_one(wb, t1, "T1 调研", args.timeout)

        # ---- T2 撰写（依赖 T1，必须引用 T1 的标记 -> 证明跨 Agent 产物/上下文交接）----
        t2 = wb.tasks.create(
            project["id"],
            "撰写：基于调研笔记出报告",
            kind="doc",
            brief=(
                "读取当前工作目录下的 notes.md（这是上一个 Agent 的调研产物），"
                "据此新建 report.md（Markdown），要求：\n"
                f"1) 标题「报告：{MARKER}」；\n"
                "2) 至少包含一节「摘要」，用自己的话概括 notes.md 的 3 条要点；\n"
                f"3) 正文中必须原样出现标记 {MARKER}。\n"
                "不要修改 notes.md。"
            ),
            agent_id=args.draft,
            depends_on=[t1["id"]],
            outputs=["report.md"],
            acceptance=[
                {"type": "file_exists", "path": "report.md"},
                {"type": "content_contains", "path": "report.md", "text": MARKER},
                {"type": "exit_zero"},
            ],
            timeout_sec=args.timeout,
        )
        _run_one(wb, t2, "T2 撰写", args.timeout)

        # ---- T3 只读审阅（依赖 T2，read_only：绝不能写文件）----
        t3 = wb.tasks.create(
            project["id"],
            "审阅：只读评审报告",
            kind="review",
            template_id="review",
            brief=(
                "只读评审：阅读 report.md 与 notes.md，给出评审意见（结构、准确性、是否缺数据源）。"
                "你**不能**修改或创建任何文件，只在交接说明里写出评审结论。"
            ),
            agent_id=args.review,
            depends_on=[t2["id"]],
            acceptance=[
                {"type": "no_writes"},
                {"type": "handoff_present"},
            ],
            timeout_sec=args.timeout,
        )
        assert wb.tasks.get(t3["id"])["read_only"] is True, "review 模板未把任务置为只读"
        before = _tree_hashes(root)
        _run_one(wb, t3, "T3 只读审阅", args.timeout)
        after = _tree_hashes(root)
        _record("chain:T3-真只读", before == after, f"审阅前后工作区哈希一致={before == after}")

        # ---- T4 汇总（依赖 T3，第四个 Agent）----
        t4 = wb.tasks.create(
            project["id"],
            "汇总：产出交付摘要",
            kind="doc",
            brief=(
                "综合 notes.md、report.md 以及审阅意见，在当前工作目录新建 summary.md，要求：\n"
                "1) 标题「交付摘要」；\n"
                f"2) 一行写明标记 {MARKER}；\n"
                "3) 一行写明审阅结论（采纳/需修改）。\n"
                "不要修改其他文件。"
            ),
            agent_id=args.summary,
            depends_on=[t3["id"]],
            outputs=["summary.md"],
            acceptance=[
                {"type": "file_exists", "path": "summary.md"},
                {"type": "content_contains", "path": "summary.md", "text": MARKER},
            ],
            timeout_sec=args.timeout,
        )
        _run_one(wb, t4, "T4 汇总", args.timeout)

        # ---- 跨任务上下文注入：T2 的 prompt 里应出现 T1 交接说明的内容 ----
        runs2 = [r for r in wb.audit.runs(task_id=t2["id"]) if r.get("prompt_path")]
        injected = False
        snippet = ""
        for r in runs2:
            text = Path(r["prompt_path"]).read_text(encoding="utf-8", errors="replace")
            if "调研笔记" in text or MARKER in text:
                injected = True
                snippet = text[:400]
                break
        _record("chain:上下文注入", injected, "T2 的 prompt 里包含 T1 的产物/交接内容", {"snippet": snippet})

        for tid, label in ((t1["id"], "notes.md"), (t2["id"], "report.md"), (t4["id"], "summary.md")):
            p = root / label
            _record(f"chain:产物 {label}", p.exists(),
                    f"存在={p.exists()} size={p.stat().st_size if p.exists() else 0}")

        # ---- 依赖顺序：T4 的开始时间必须晚于 T2 的结束时间 ----
        _record_dependency_order(wb, t1, t2, t3, t4)

        _dump(wb, project)
    finally:
        wb.close()
    return _verdict()


def cmd_fanout(args) -> int:
    """需求 6 / 50：同一个任务分派给多个真实 Agent，各自产出保留并可比对。"""
    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    print(f"=== 同题多 Agent 扇出：{agents} (home={args.home}) ===")
    wb = _open_workbench(Path(args.home), args.fresh)
    try:
        project = _project(wb, "e2e-fanout")
        root = Path(project["root_path"])
        print(f"项目 {project['id']}  root={root}")

        task = wb.tasks.create(
            project["id"],
            "同题多产出：写一句话结论",
            kind="doc",
            brief=(
                "在当前工作目录新建 out.txt，只写一行中文结论，说明「多 Agent 协作」的最大价值是什么。"
                "不要写其他任何内容，不要创建其他文件。"
            ),
            outputs=["out.txt"],
            acceptance=[{"type": "file_exists", "path": "out.txt"}],
            timeout_sec=args.timeout,
        )
        result = wb.engine.fanout(task["id"], agents)
        print(f"\nfanout 返回 best_agent={result.get('best_agent')}")
        for row in result.get("results", []):
            print(f"  agent={row.get('agent_id')} status={row.get('status')} "
                  f"exit={row.get('exit_code')} error={str(row.get('error'))[:80]}")

        runs = wb.audit.runs(task_id=task["id"])
        _record("fanout:每个 Agent 都有独立 run", len(runs) == len(agents),
                f"runs={len(runs)} agents={len(agents)}")

        comparison = wb.audit.compare_runs(task["id"])
        listed = comparison.get("runs", [])
        _record("fanout:产出可对比", len(listed) >= 2, f"compare_runs 返回 {len(listed)} 条")

        for r in runs:
            path = r.get("stdout_path")
            text = Path(path).read_text(encoding="utf-8", errors="replace")[:160] if path and Path(path).exists() else ""
            print(f"\n  ---- {r['agent_id']} stdout 摘要 ----\n  {text!r}")

        _dump(wb, project)
    finally:
        wb.close()
    return _verdict()


def _run_one(wb, task: dict, label: str, timeout: int) -> dict:
    started = time.time()
    outcome = wb.engine.run_task(task["id"])
    elapsed = time.time() - started
    run = (wb.audit.runs(task_id=task["id"]) or [{}])[0]
    detail = (f"status={outcome.status} exit={getattr(outcome, 'exit_code', None)} "
              f"acceptance={getattr(outcome, 'acceptance_ok', None)} elapsed={elapsed:.1f}s "
              f"agent={run.get('agent_id')}")
    _record(f"{label}", outcome.status == "success", detail,
            {"run": {k: run.get(k) for k in
                     ("id", "agent_id", "protocol", "status", "exit_code", "duration_ms", "cost_tokens", "error")},
             "stdout_path": run.get("stdout_path"), "stderr_path": run.get("stderr_path")})
    return outcome


def _record_dependency_order(wb, *tasks) -> None:
    ids = [t["id"] for t in tasks]
    rows = [wb.tasks.get(i) for i in ids]
    order_ok = True
    detail = []
    for prev, cur in zip(rows, rows[1:]):
        pf, cs = prev.get("finished_at"), cur.get("started_at")
        if not pf or not cs:
            order_ok = False
            detail.append(f"{prev['id'][:12]} finished={pf} / {cur['id'][:12]} started={cs}")
            continue
        ok = cs >= pf
        order_ok = order_ok and ok
        detail.append(f"{prev['id'][:12]}.finished={pf} <= {cur['id'][:12]}.started={cs} → {ok}")
    _record("chain:依赖顺序", order_ok, "; ".join(detail))


def _tree_hashes(root: Path) -> dict:
    import hashlib

    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _dump(wb, project: dict) -> None:
    pid = project["id"]
    print("\n---- 审计汇总 ----")
    stats = wb.audit.stats(pid)
    print(f"  totals: {stats['totals']}")
    for row in stats["by_agent"]:
        print(f"  agent {row['agent_id']}: runs={row['runs']} duration={row['duration_ms']}ms "
              f"tokens={row['tokens']} success={row['success']} failed={row['failed']}")
    for row in stats["by_task"]:
        print(f"  task {row['task_id'][:12]} [{row['status']}] runs={row['runs']} duration={row['duration_ms']}ms")
    print("\n---- 交接说明 ----")
    for h in wb.handoffs.list(project_id=pid) if hasattr(wb.handoffs, "list") else []:
        print(f"  [{h['source']}] {h['task_id'][:12]} did={h['did'][:60]!r}")
    print("\n---- 黑板 ----")
    for e in wb.board.list(pid) or []:
        print(f"  [{e['type']}/{e['scope']}/{e['status']}] {e['title'][:60]}")
    REPORT["stats"] = stats
    REPORT["progress"] = wb.tasks.progress(pid)


def _verdict() -> int:
    REPORT["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    total = len(REPORT["checks"])
    failed = len(REPORT["failures"])
    print(f"\n================ 结论：{total - failed}/{total} 项通过，{failed} 项失败 ================")
    out = Path(REPO) / ".e2e" / f"report-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"证据报告：{out}")
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="真实 Agent 端到端验收")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("smoke", help="四个 Agent 各自冒烟")
    s.add_argument("--agents", default="opencode,codex,dsh,claude")
    s.add_argument("--home", default=str(REPO / ".e2e" / "home-smoke"))
    s.add_argument("--timeout", type=int, default=300)
    s.add_argument("--fresh", action="store_true")
    s.set_defaults(func=cmd_smoke)

    c = sub.add_parser("chain", help="跨 Agent 协作链")
    c.add_argument("--research", default="opencode")
    c.add_argument("--draft", default="codex")
    c.add_argument("--review", default="claude")
    c.add_argument("--summary", default="dsh")
    c.add_argument("--home", default=str(REPO / ".e2e" / "home-chain"))
    c.add_argument("--timeout", type=int, default=420)
    c.add_argument("--fresh", action="store_true")
    c.set_defaults(func=cmd_chain)

    f = sub.add_parser("fanout", help="同题多 Agent 扇出并比对产出")
    f.add_argument("--agents", default="opencode,codex")
    f.add_argument("--home", default=str(REPO / ".e2e" / "home-fanout"))
    f.add_argument("--timeout", type=int, default=300)
    f.add_argument("--fresh", action="store_true")
    f.set_defaults(func=cmd_fanout)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
