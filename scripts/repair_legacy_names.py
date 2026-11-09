"""修复历史遗留的「问号名称」（早期 PowerShell/控制台编码事故留下的 `?????`）。

背景：2026-11 之前，部分项目/任务/黑板条目是通过 Windows 控制台以非 UTF-8 方式写入的，
标题被替换成了纯问号。这些数据本身没有可恢复信息，但可以在**不改结构、不删数据**的前提下
换成可读的占位名，避免界面继续显示 `?????`。

用法（默认 dry-run，只打印将要改什么）：

    .\\.venv\\Scripts\\python.exe scripts\\repair_legacy_names.py
    .\\.venv\\Scripts\\python.exe scripts\\repair_legacy_names.py --home ~/multi_xuanwo --apply

规则：
- 只处理「整体由 ? / ？ / U+FFFD / 空白组成」的字符串，正常中文/英文一律不动；
- project.label 坏 → 用 project.name（可读时），否则「演示项目」；
- task.title 坏 → 按 kind 生成「调研任务（历史数据）」等；
- board_entries.title/body 坏 → 可读占位；
- 事件表是 append-only 哈希链，**不修改**（避免破坏链完整性）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from multiagent.app import Workbench  # noqa: E402

GARBLED = re.compile(r"^[?\uFFFD？\s]+$")

TASK_FALLBACK = {
    "research": "调研任务（历史数据）",
    "code": "编码任务（历史数据）",
    "doc": "文档任务（历史数据）",
    "data": "数据任务（历史数据）",
    "review": "审阅任务（历史数据）",
    "generic": "未命名任务（历史数据）",
}


def bad(value) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    if GARBLED.match(text):
        return True
    # 「大部分是问号」的混排文本（例如 "???????? + SQLite"）：无法恢复原文，按乱码处理。
    compact = re.sub(r"\s+", "", text)
    if len(compact) >= 4:
        marks = sum(1 for ch in compact if ch in "?\uFFFD？")
        if marks >= 2 and marks / len(compact) >= 0.5:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(REPO))
    parser.add_argument("--apply", action="store_true", help="真正写库；默认只预览")
    args = parser.parse_args()

    wb = Workbench.open(Path(args.home))
    changes: list[tuple[str, str, str, str]] = []
    try:
        for project in wb.projects.list():
            label = str(project.get("label") or "")
            if bad(label):
                name = str(project.get("name") or "")
                new_label = name if not bad(name) else "演示项目"
                changes.append(("projects", project["id"], f"label={label!r}", f"label={new_label!r}"))
                if args.apply:
                    wb.db.update("projects", {"id": project["id"]}, {"label": new_label})

        rows = wb.db.query("SELECT id, project_id, title, kind FROM tasks")
        for row in rows:
            title = str(row["title"] or "")
            if bad(title):
                new_title = TASK_FALLBACK.get(str(row["kind"] or ""), TASK_FALLBACK["generic"])
                changes.append(("tasks", row["id"], f"title={title!r}", f"title={new_title!r}"))
                if args.apply:
                    wb.db.update("tasks", {"id": row["id"]}, {"title": new_title})

        rows = wb.db.query("SELECT id, title, body FROM board_entries")
        for row in rows:
            title = str(row["title"] or "")
            body = str(row["body"] or "")
            patch: dict[str, str] = {}
            if bad(title):
                patch["title"] = "历史黑板条目"
            if bad(body):
                patch["body"] = "（历史乱码内容已清理，原文不可恢复）"
            if patch:
                changes.append(("board_entries", row["id"], f"{title!r}/{body[:20]!r}", str(patch)))
                if args.apply:
                    wb.db.update("board_entries", {"id": row["id"]}, patch)
    finally:
        wb.close()

    if not changes:
        print("没有发现纯问号名称，无需修复。")
        return 0
    for table, row_id, old, new in changes:
        print(f"[{table}] {row_id}: {old} -> {new}")
    print(f"\n共 {len(changes)} 处；{'已写入' if args.apply else 'dry-run（加 --apply 才写入）'}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
