"""冻结后端的入口：等价于 ``python -m multiagent``（供 PyInstaller 打包）。

子命令与参数原样透传，因此打包出来的 exe 支持 ``multiagent-backend serve --port 8770``
等全部 CLI 能力。
"""

from __future__ import annotations

import multiprocessing
import sys

from multiagent.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
