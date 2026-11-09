"""启动 Web 看板 + API。

    python scripts/serve.py --port 8770 --open
等价于 `python -m multiagent --home <dir> serve`。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    from multiagent.api.app import serve
    from multiagent.app import Workbench

    serve(Workbench.open(args.home), host=args.host, port=args.port, open_browser=args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
