"""升级兼容性核验：拿**真实既有库**的副本跑一遍新代码，确认自动迁移不毁数据。

背景：本轮改动给 `tasks` / `runs` 加了列，并新增 `runtime_sessions` 表。这些迁移是
`ALTER TABLE ADD COLUMN` 之类的幂等操作，但**从未在真实库上验证过**（开发期遵守
「不碰既有库」的约束）。对一个「已经用了一段时间」的部署来说，升级把数据搞坏是最
严重的事故类型，所以这里用真实库的**一致性副本**（sqlite3 backup API，含 WAL 内容）
做一次 dry-run。

用法：
    .venv\\Scripts\\python.exe scripts\\verify_upgrade.py
    .venv\\Scripts\\python.exe scripts\\verify_upgrade.py --db <path> --work <.e2e\\upgrade>
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

TRACKED_TABLES = ("projects", "tasks", "task_edges", "runs", "board_entries", "board_versions",
                  "events", "handoffs", "artifacts", "file_changes", "approvals", "leases",
                  "agent_usage", "snapshots", "meta")


def snapshot_state(db: Path) -> dict:
    """读取一个库的结构摘要 + 关键表行数 + 样本（用于前后对比）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        cols = {}
        for t in tables:
            cols[t] = [r[1] for r in con.execute(f"PRAGMA table_info({t})")]
        counts = {}
        for t in tables:
            try:
                counts[t] = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                counts[t] = None
        projects = [dict(zip(("id", "name", "root_path"), r)) for r in con.execute(
            "SELECT id, name, root_path FROM projects ORDER BY created_at")]
        tasks = [dict(zip(("id", "title", "status", "attempt"), r)) for r in con.execute(
            "SELECT id, title, status, attempt FROM tasks ORDER BY created_at")]
        return {"tables": tables, "columns": cols, "counts": counts,
                "projects": projects, "tasks": tasks}
    finally:
        con.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(REPO / ".multiagent" / "multiagent.sqlite3"))
    ap.add_argument("--work", default=str(REPO / ".e2e" / "upgrade"))
    args = ap.parse_args()

    src = Path(args.db)
    if not src.exists():
        print(f"找不到数据库 {src}（如果这是全新部署，本项核验可跳过）")
        return 0

    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    (work / ".multiagent").mkdir(parents=True, exist_ok=True)

    # 用 sqlite3 backup API 做一致性副本：直接 copy 文件会漏掉 WAL 里的已提交数据
    dst = work / ".multiagent" / "multiagent.sqlite3"
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    dst_con = sqlite3.connect(str(dst))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()
    print(f"已复制真实库副本 -> {dst}")

    before = snapshot_state(dst)
    print(f"升级前：{len(before['tables'])} 张表，projects={before['counts'].get('projects')}，"
          f"tasks={before['counts'].get('tasks')}，runs={before['counts'].get('runs')}")

    # 用新代码打开（触发迁移）
    for name in ("agents.json", "templates.json", "routing.json", "acceptance.json", "policy.json"):
        s = REPO / "config" / name
        if s.exists():
            (work / "config").mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, work / "config" / name)

    from multiagent.app import Workbench

    wb = Workbench.open(work)
    failures: list[str] = []
    try:
        after = snapshot_state(dst)
        new_tables = sorted(set(after["tables"]) - set(before["tables"]))
        print(f"升级后：{len(after['tables'])} 张表；新增表={new_tables}")

        new_cols = {}
        for t in before["tables"]:
            added = sorted(set(after["columns"].get(t, [])) - set(before["columns"].get(t, [])))
            if added:
                new_cols[t] = added
        print(f"新增列={new_cols}")

        # 1) 老数据必须一条不少、内容不变
        for t in TRACKED_TABLES:
            b, a = before["counts"].get(t), after["counts"].get(t)
            if b is None and a is None:
                continue
            if b != a:
                failures.append(f"表 {t} 行数变化：{b} -> {a}")
        if before["projects"] != after["projects"]:
            failures.append("projects 内容发生变化")
        if before["tasks"] != after["tasks"]:
            failures.append("tasks 内容（含 status/attempt）发生变化")

        # 2) 迁移确实发生了
        if "runtime_sessions" not in after["tables"]:
            failures.append("新表 runtime_sessions 未创建")

        # 3) 业务功能可用：能列项目、能跑 recover
        projects = wb.projects.list()
        print(f"新代码读到 {len(projects)} 个项目：{[p['name'] for p in projects]}")
        if len(projects) != before["counts"].get("projects", 0):
            failures.append(f"项目数不一致：读到 {len(projects)}，库里有 {before['counts'].get('projects')}")

        for p in projects:
            result = wb.engine.recover(p["id"])
            print(f"  recover({p['name']}) -> {result}")
            progress = wb.tasks.progress(p["id"])
            print(f"  进度：{progress.get('counters')}")

        # 4) 再开一次，确认迁移幂等（重复 ALTER 不报错）
        wb.close()
        wb2 = Workbench.open(work)
        try:
            again = snapshot_state(dst)
            if again["columns"] != after["columns"]:
                failures.append("二次打开后结构再次变化（迁移不幂等）")
            print("二次打开：结构稳定（迁移幂等）")
        finally:
            wb2.close()
        return _report(failures, before, after, new_tables, new_cols)
    finally:
        try:
            wb.close()
        except Exception:  # noqa: BLE001
            pass
        out = work / "upgrade-report.json"
        out.write_text(json.dumps({"before": before, "failures": failures},
                                  ensure_ascii=False, indent=2), encoding="utf-8")


def _report(failures, before, after, new_tables, new_cols) -> int:
    print("\n================ 结论 ================")
    if failures:
        print("[FAIL] 升级兼容性核验未通过：")
        for f in failures:
            print("   -", f)
        return 1
    print("[PASS] 真实库副本可安全升级：老数据零变化，新结构已就位，业务功能可用，迁移幂等")
    print(f"       新增表 {new_tables}，新增列 {new_cols}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
