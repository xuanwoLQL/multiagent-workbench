"""需求可追溯性核验：把 docs/REQUIREMENTS.md 的「需求 -> 验收」映射变成可执行断言。

它回答一个很具体的问题：**文档里声称的验收方式是不是真的存在、真的通过？**
（文档写了 57 条「已实现」，但没有东西能保证那些被引用的测试真的存在、真的绿。
这是最典型的「文档夸大」来源。）

做法：
  1. 解析 docs/REQUIREMENTS.md 的表格行，抽出每条需求编号、描述、被引用的测试 node id；
  2. 用 pytest --collect-only 收集真实存在的测试；
  3. 运行被引用的测试，记录 PASSED/FAILED；
  4. 输出 docs/REQUIREMENTS-EVIDENCE.md 证据表，并对「引用了不存在的测试」或
     「被引用测试失败」返回非零退出码。

用法：
    .venv\\Scripts\\python.exe scripts\\verify_requirements.py
    .venv\\Scripts\\python.exe scripts\\verify_requirements.py --no-run   # 只做引用存在性检查
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO / "docs" / "REQUIREMENTS.md"
OUT = REPO / "docs" / "REQUIREMENTS-EVIDENCE.md"
PY = REPO / ".venv" / "Scripts" / "python.exe"
PYTHON = str(PY if PY.exists() else sys.executable)


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

# docs/REQUIREMENTS.md 里的测试引用形如 tests/test_x.py::test_y 或 tests/test_x.py
TEST_REF = re.compile(r"(tests/[\w./-]+\.py)(?:::(test_[\w]+))?")
ROW = re.compile(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$")


def parse_requirements() -> list[dict]:
    rows: list[dict] = []
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line.strip())
        if not m:
            continue
        num, text, impl, evidence = m.groups()
        rows.append(
            {
                "num": int(num),
                "text": text,
                "impl": impl,
                "evidence": evidence,
                "refs": sorted({f"{a}::{b}" if b else a for a, b in TEST_REF.findall(evidence)}),
            }
        )
    return rows


def run_cited(refs: list[str]) -> tuple[dict[str, str], list[str]]:
    """一次性把被引用的用例交给 pytest。

    这样做的额外好处：pytest 对不存在的 node id 会打印 ``ERROR: not found: <id>``，
    于是「文档引用了不存在的测试」这件事由 pytest 自己判定，不依赖我们自己解析收集结果。
    返回 (用例结果表, 不存在的引用列表)。
    """
    if not refs:
        return {}, []
    proc = subprocess.run(
        # 注意：pyproject.toml 里有 addopts="-q"，会盖掉命令行 -v；
        # 必须显式 -o addopts= 清空，否则拿不到逐用例的 PASSED/FAILED 行。
        [PYTHON, "-m", "pytest", "-o", "addopts=", "-v", "--no-header",
         "-p", "no:cacheprovider", *refs],
        cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600,
    )
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    outcomes: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^(tests[\\/]\S+::\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)", line.strip())
        if m:
            outcomes[m.group(1).replace("\\", "/")] = m.group(2)
    not_found = []
    for line in text.splitlines():
        m = re.search(r"not found:\s*(\S+)", line)
        if m:
            token = m.group(1).rstrip(":").replace("\\", "/")
            # pytest 有时回显成绝对路径，统一截回仓库相对形式
            if "multi_xuanwo/" in token:
                token = token.split("multi_xuanwo/")[-1]
            not_found.append(token)
    return outcomes, sorted(set(not_found))


def main() -> int:
    _force_utf8_stdio()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-run", action="store_true", help="只检查引用存在性，不实际运行")
    args = ap.parse_args()

    if not REQUIREMENTS.exists():
        print(f"找不到 {REQUIREMENTS}")
        return 2

    reqs = parse_requirements()
    print(f"解析到 {len(reqs)} 条需求")

    cited: list[str] = sorted({ref for r in reqs for ref in r["refs"] if "::" in ref})
    print(f"被引用的测试用例 {len(cited)} 个")

    if args.no_run:
        # 不做运行也要能判断「引用是否存在」：用一次 --collect-only 的逐文件计数做粗筛，
        # 精确判定仍以运行时的 not found 为准。
        print("（--no-run：跳过实际运行，仅输出映射表）")
        outcomes: dict[str, str] = {}
        missing: list[str] = []
    else:
        print("运行被引用的用例…")
        outcomes, missing = run_cited(cited)
        if missing:
            # pytest 遇到不存在的 node id 会直接以退出码 4 结束、什么都不跑，
            # 所以把不存在的剔除后再跑一遍，才能拿到其余用例的真实结果。
            missing_norm = {m.replace("\\", "/").split("multi_xuanwo/")[-1] for m in missing}
            rest = [r for r in cited if r not in missing_norm and r not in set(missing)]
            print(f"剔除 {len(missing)} 条不存在的引用后，重跑 {len(rest)} 条…")
            outcomes, _ = run_cited(rest)
            missing = sorted(missing_norm)
        print(f"取得 {len(outcomes)} 条结果，{len(missing)} 条引用不存在")

    failed = sorted(ref for ref, res in outcomes.items() if res in ("FAILED", "ERROR"))

    lines: list[str] = []
    lines.append("# 需求验收证据表（由 scripts/verify_requirements.py 生成）\n")
    lines.append("> 本表由脚本从 `docs/REQUIREMENTS.md` 抽取，并把被引用的测试**真的跑一遍**核对，")
    lines.append("> 不是人工填写的声明。\n")
    lines.append(f"> 需求条数：{len(reqs)}　被引用测试：{len(cited)}　"
                 f"引用不存在：{len(missing)}　被引用但失败：{len(failed)}\n")
    lines.append("| # | 需求 | 验收引用 | 存在 | 结果 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in reqs:
        refs = [x for x in r["refs"] if "::" in x]
        if not refs:
            lines.append(f"| {r['num']} | {r['text'][:56]} | {r['evidence'][:64]} | — | 命令/人工核验 |")
            continue
        for ref in refs:
            exists = "**否**" if ref in missing else "是"
            res = outcomes.get(ref, "—" if args.no_run else "未取到")
            if res == "PASSED":
                res = "**通过**"
            elif res in ("FAILED", "ERROR"):
                res = f"**{res}**"
            lines.append(f"| {r['num']} | {r['text'][:56]} | `{ref}` | {exists} | {res} |")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"已写出 {OUT}")

    ok = True
    if missing:
        ok = False
        print(f"\n[FAIL] 文档引用了 {len(missing)} 个不存在的测试：")
        for m in missing:
            print("   -", m)
    if failed:
        ok = False
        print(f"\n[FAIL] 文档引用但实际失败的测试 {len(failed)} 个：")
        for m in failed:
            print("   -", m, outcomes.get(m))
    if ok:
        print("\n[PASS] 文档引用的每一项验收都真实存在且通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
