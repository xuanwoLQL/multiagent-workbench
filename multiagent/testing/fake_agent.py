"""假 CLI Agent：`python -m multiagent.testing.fake_agent`。

从 stdin 读提示词（可配置），按 ``FAKE_PLAN`` 操作工作目录，最后按 ``exit`` 退出。
用来在没有真实模型的情况下做端到端验收。
"""

from __future__ import annotations

import sys
from pathlib import Path

from .fake_plan import apply_plan, read_plan


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    prompt = sys.stdin.read() if not sys.stdin.isatty() else ""
    plan = read_plan()
    result = apply_plan(plan, Path.cwd())
    if plan.get("stdout"):
        print(plan["stdout"])
    else:
        print(f"[fake-agent] 收到提示词 {len(prompt)} 字符；写入 {result['written']}，删除 {result['deleted']}")
    if plan.get("stderr"):
        print(plan["stderr"], file=sys.stderr)
    return int(plan.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
