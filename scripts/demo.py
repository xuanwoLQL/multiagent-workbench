"""端到端演示：用「假 Agent」跑一个完整的多 Agent 协作项目（不消耗任何模型额度）。

    python scripts/demo.py                 # 跑一遍并打印报告
    python scripts/demo.py --serve         # 跑完后启动 Web 看板

演示内容：
- 项目/工作区创建与文件导入
- 任务 DAG：调研 → 撰写报告 → 只读审阅（串行 + 并行）
- 隔离工作副本、改动日志、验收条件、结构化交接
- 交接上下文自动注入下一个任务
- 共享黑板、长期记忆审核闸门
- 版本快照与回退
- 成本/耗时汇总与时间线导出
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def write_config(home: Path) -> None:
    cfg = home / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    env = {"PYTHONPATH": str(REPO_ROOT)}
    (cfg / "agents.json").write_text(
        json.dumps(
            {
                "version": 1,
                "defaults": {"timeout_sec": 60},
                "agents": [
                    {
                        "id": "researcher",
                        "label": "调研 Agent（假）",
                        "protocol": "cli",
                        "cli": {"command": sys.executable, "args": ["-m", "multiagent.testing.fake_agent"], "prompt_mode": "stdin"},
                        "env": {**env, "FAKE_PLAN_FILE": str(home / "plan-researcher.json")},
                        "roles": ["researcher", "writer"],
                        "strengths": ["research", "doc"],
                        "can_write": True,
                    },
                    {
                        "id": "writer",
                        "label": "写作 Agent（假 ACP）",
                        "protocol": "acp",
                        "command": sys.executable,
                        "args": ["-m", "multiagent.testing.fake_acp"],
                        "env": {**env, "FAKE_PLAN_FILE": str(home / "plan-writer.json")},
                        "roles": ["writer", "implementer"],
                        "strengths": ["doc", "code"],
                        "can_write": True,
                    },
                    {
                        "id": "reviewer",
                        "label": "审阅 Agent（假）",
                        "protocol": "cli",
                        "cli": {"command": sys.executable, "args": ["-m", "multiagent.testing.fake_agent"], "prompt_mode": "stdin"},
                        "env": {**env, "FAKE_PLAN_FILE": str(home / "plan-reviewer.json")},
                        "roles": ["reviewer"],
                        "strengths": ["review"],
                        "can_write": True,
                    },
                    {"id": "human", "label": "人工", "protocol": "human"},
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (cfg / "policy.json").write_text(
        json.dumps(
            {
                "version": 1,
                "concurrency": {"global": 4, "per_project": 3},
                "default_timeout_sec": 60,
                "context_budget_chars": 8000,
                "isolation": {"default": "copy", "sync_back_on_accept": True, "block_conflict": True, "revert_out_of_scope": True},
                "dangerous_ops": {"require_confirmation": ["delete"], "auto_approve": False, "protect_paths": [".multiagent", "config", ".git"]},
                "review_gate": {"memory_requires_approval": True, "default_entry_status": "approved"},
                "kinds": {
                    "code": {"isolation": "copy", "acceptance_ref": "file-changed"},
                    "doc": {"isolation": "copy", "acceptance_ref": "file-exists"},
                    "review": {"isolation": "shared", "read_only": True, "acceptance_ref": "review-readonly"},
                    "manual": {"isolation": "shared", "acceptance_ref": "manual-confirm"},
                    "generic": {"isolation": "copy", "acceptance_ref": "file-exists"},
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def set_plan(home: Path, name: str, plan: dict) -> None:
    (home / f"plan-{name}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(REPO_ROOT / "demo_home"))
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()

    # 相对路径必须转绝对：假 Agent 的 cwd 是隔离工作副本，
    # 相对 FAKE_PLAN_FILE 会在副本里解析，导致「收到提示词但什么都没写」。
    home = Path(args.home).expanduser().resolve()
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    write_config(home)

    from multiagent.app import Workbench

    wb = Workbench.open(home)

    # ---------------- 工作区 ----------------
    project = wb.projects.create("演示项目", label="多 Agent 协作演示")
    root = Path(project["root_path"])
    (root / "需求.md").write_text("# 需求\n\n请调研并撰写一份《产品方案》报告。\n", encoding="utf-8")
    print(f"[1] 工作区已创建：{root}")

    snap = wb.snapshots.create(project["id"], root, label="初始状态")
    print(f"[2] 版本快照：{snap['id']}")

    # ---------------- 任务 DAG ----------------
    set_plan(home, "researcher", {"files": {"调研笔记.md": "# 调研笔记\n\n- 结论：采用方案 A\n"}, "handoff": {"did": "完成调研，结论：采用方案 A", "open_issues": ["缺少竞品价格"], "next_steps": ["撰写报告"]}, "exit": 0})
    set_plan(home, "writer", {"files": {"产品方案.md": "# 产品方案\n\n依据调研笔记撰写。\n"}, "handoff": {"did": "撰写《产品方案》初稿"}, "exit": 0})
    set_plan(home, "reviewer", {"handoff": {"did": "审阅通过，建议补充价格对比"}, "exit": 0})

    research = wb.tasks.create(project["id"], "调研产品方案", kind="doc", agent_id="researcher", brief="调研同类产品，产出调研笔记。", outputs=["调研笔记.md"], acceptance=[{"type": "file_exists", "path": "调研笔记.md"}])
    draft = wb.tasks.create(project["id"], "撰写《产品方案》", kind="doc", agent_id="writer", brief="依据前置调研撰写报告。", outputs=["产品方案.md"], depends_on=[research["id"]], acceptance=[{"type": "file_exists", "path": "产品方案.md"}])
    review = wb.tasks.create(project["id"], "只读审阅报告", template_id="review", agent_id="reviewer", depends_on=[draft["id"]])
    print(f"[3] 任务 DAG：{research['id']} → {draft['id']} → {review['id']}")

    # ---------------- 调度 ----------------
    summary = wb.engine.run_project(project["id"])
    print(f"[4] 调度完成：成功 {summary['success']}，失败 {summary['failed']}，阻塞 {summary['blocked']}，等待人工 {summary['waiting']}")

    print("\n--- 任务结果 ---")
    for task in wb.tasks.list(project["id"]):
        handoff = wb.handoffs.latest_for_task(task["id"])
        print(f"  {task['id']} [{task['status']:<8}] {task['title']}  ← {task['agent_id']}")
        if handoff and handoff["did"]:
            print(f"      交接：{handoff['did']}")

    print("\n--- 验收（只信证据，不信自我声明）---")
    for task in wb.tasks.list(project["id"]):
        result = task["result"].get("acceptance")
        if result:
            print(f"  {task['title']}: {'✔' if result['ok'] else '✘'} {result['summary']}")

    print("\n--- 上下文注入检查 ---")
    runs = wb.audit.runs(task_id=draft["id"])
    if runs:
        prompt = Path(runs[0]["prompt_path"]).read_text(encoding="utf-8")
        print(f"  撰写任务的 prompt 是否包含调研结论：{'是' if '方案 A' in prompt else '否'}")

    # ---------------- 黑板 / 记忆 -----------------
    wb.board.post(project["id"], type="decision", title="采用方案 A", body="甲方确认", scope="project", actor="human")
    pending = wb.board.pending_reviews(project["id"])
    print(f"\n[5] 长期记忆待审核 {len(pending)} 条；批准后进入项目记忆")
    for entry in pending:
        wb.board.review(entry["id"], approved=True, reviewer="human")
    print(f"    项目长期记忆 {len(wb.board.long_term_memory(project['id']))} 条")

    # ---------------- 快照回退 -----------------
    diff = wb.snapshots.diff_current(snap["id"], root)
    print(f"[6] 与初始快照的差异：新增 {diff['counts']['added']}，修改 {diff['counts']['modified']}，删除 {diff['counts']['removed']}")

    # ---------------- 观测 -----------------
    stats = wb.audit.stats(project["id"])
    print(f"\n[7] 成本/耗时：调用 {stats['totals']['runs']} 次，耗时 {stats['totals']['duration_ms']}ms，token {stats['totals']['tokens']}")
    for row in stats["by_agent"]:
        print(f"    {row['agent_id']}: {row['runs']} 次，成功 {row['success']}，失败 {row['failed']}")
    out = wb.audit.export_timeline(project["id"], Path(home) / "timeline.md")
    print(f"[8] 时间线已导出：{out}")

    print(f"\n数据目录：{home}")
    wb.close()

    if args.serve:
        from multiagent.api.app import serve

        serve(Workbench.open(home), host="127.0.0.1", port=args.port, open_browser=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
