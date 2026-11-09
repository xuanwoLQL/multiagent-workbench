"""把 Atlas 测试项目的任务契约、超时与重试策略固化到工作台。

这个脚本是「人工作为调度者参与编排」的示例（需求 54/57）：
- 人工纠正设计契约后，把决策写进项目黑板（后续任务会自动注入）；
- 人工修正设计任务的交接说明，使它成为后续任务的权威上下文；
- 按任务风险调整 max_attempts / timeout / write_scope；
- 强制回收上一轮崩溃留下的 running 现场。

运行：
    .\.venv\Scripts\python.exe examples\prepare_atlas.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from multiagent.app import Workbench  # noqa: E402

HOME = ROOT
PROJECT_ID = "proj-08c3642a"

CONTRACT_LINE = (
    "先读 docs/CONTRACT.md（人工冻结的单一契约），再读 docs/DESIGN.md 与 docs/API.md；"
    "三者冲突时以 CONTRACT.md 为准。禁止修改 docs/CONTRACT.md、docs/DESIGN.md、docs/API.md。\n\n"
)


def main() -> int:
    wb = Workbench.open(HOME)
    project = wb.projects.get(PROJECT_ID)
    print("project:", project["name"], project["root_path"])

    recovered = wb.engine.recover(PROJECT_ID, force=True)
    print("recover:", recovered)

    tasks = sorted(wb.tasks.list(PROJECT_ID), key=lambda t: int(t["ordering"]))
    for index, task in enumerate(tasks, 1):
        print(f"{index:>2}. {task['status']:<11} {task['title'][:60]}")

    by_order = {index: task for index, task in enumerate(tasks, 1)}
    policies = {
        2: {"max_attempts": 3, "timeout_sec": 3600},
        3: {"max_attempts": 3, "timeout_sec": 3000},
        4: {"max_attempts": 3, "timeout_sec": 3000},
        5: {"max_attempts": 3, "timeout_sec": 2400},
        6: {"max_attempts": 3, "timeout_sec": 3600},
        7: {"max_attempts": 2, "timeout_sec": 1800},
        8: {"max_attempts": 2, "timeout_sec": 1800},
        9: {"max_attempts": 3, "timeout_sec": 3600},
        10: {"max_attempts": 2, "timeout_sec": 2400},
    }
    for order, patch in policies.items():
        task = by_order.get(order)
        if not task:
            continue
        brief = task["brief"] or ""
        if "docs/CONTRACT.md" not in brief:
            brief = CONTRACT_LINE + brief
        wb.tasks.update(task["id"], brief=brief, **patch)
        print(f"updated #{order}: {task['title'][:40]} -> {patch}")

    # 文档任务只写交付文档，不得改冻结契约。
    if 7 in by_order:
        wb.tasks.update(
            by_order[7]["id"],
            write_scope=[
                "README.md",
                "docs/QA.md",
                "docs/OPERATIONS.md",
                "run.ps1",
                "run.sh",
                "Dockerfile",
                ".dockerignore",
                ".gitignore",
            ],
        )
    # 修复任务可以改实现与测试，但不得改契约文档。
    if 9 in by_order:
        wb.tasks.update(
            by_order[9]["id"],
            write_scope=[
                "server.js",
                "package.json",
                "src/**",
                "web/**",
                "tests/**",
                "scripts/**",
                "README.md",
                "docs/QA.md",
                "docs/OPERATIONS.md",
                "Dockerfile",
                "run.ps1",
                "run.sh",
                ".gitignore",
            ],
        )

    # 人工修正设计任务的交接：指出已冻结的契约与文档漂移。
    design = by_order.get(1)
    if design:
        handoff = wb.handoffs.latest_for_task(design["id"])
        if handoff:
            wb.handoffs.edit(
                handoff["id"],
                {
                    "did": handoff.get("did", ""),
                    "notes": (
                        "人工复核（2026-11-08）：原 docs/API.md 与 docs/DESIGN.md 存在契约漂移"
                        "（出现过 /api/v1、spaces/articles、If-Match 等与项目无关的约定）。"
                        "已由人重写 docs/API.md 并新增 docs/CONTRACT.md，作为后续所有任务的"
                        "唯一契约。实现任务不要再参考旧版 API 描述。"
                    ),
                },
                editor="human",
            )
            print("handoff edited:", handoff["id"])

    # 项目级决策：所有任务都会读到。
    existing = [
        entry
        for entry in wb.board.search(PROJECT_ID, "契约冻结", type="decision", limit=5)
        if entry.get("title") == "契约冻结：Atlas 使用 /api + notes/tickets"
    ]
    if not existing:
        entry = wb.board.post(
            PROJECT_ID,
            type="decision",
            scope="project",
            title="契约冻结：Atlas 使用 /api + notes/tickets",
            body=(
                "2026-11-08 人工定稿：\n"
                "- 基础路径 `/api`，不使用 `/api/v1`；资源只有 notes/tickets/comments/audit，"
                "没有 spaces/articles/attachments/If-Match。\n"
                "- 运行时零第三方依赖，前端零构建、零 CDN。\n"
                "- 唯一契约文件：docs/CONTRACT.md；docs/API.md 与 docs/DESIGN.md 必须与之一致。\n"
                "- 后续实现/测试/审阅任务一律以 docs/CONTRACT.md 为最高优先级。"
            ),
            status="approved",
            pinned=True,
            actor="human",
        )
        print("board decision:", entry["id"])

    # 给最终审阅一个明确的验收命令集合（也写进黑板，避免审阅任务漏跑）。
    final = by_order.get(10)
    if final:
        wb.tasks.update(
            final["id"],
            brief=(final["brief"] or "")
            + "\n\n补充（人工）：必须实际运行并记录 `node --test`、`node scripts/smoke.mjs`、"
            "`node --check server.js web/app.js` 的退出码；核对 package.json 无 dependencies；"
            "核对 docs/CONTRACT.md 的 8 条边界。",
        )

    print("\nactive tasks:")
    print(json.dumps(wb.tasks.progress(PROJECT_ID), ensure_ascii=False, indent=2))
    wb.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
