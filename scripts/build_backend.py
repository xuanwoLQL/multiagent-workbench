"""把 Python 后端冻结成自包含可执行文件（PyInstaller），供桌面端打包。

用法：
    .\.venv\Scripts\python.exe -m pip install pyinstaller
    .\.venv\Scripts\python.exe scripts\build_backend.py

产物：
    desktop\\backend-dist\\multiagent-backend.exe

打包桌面端时 electron-builder 会把它放进安装包的 resources\\backend\\，
桌面壳会优先使用它，从而不再依赖机器上是否装了 Python。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENTRY = REPO / "scripts" / "backend_entry.py"
DIST = REPO / "desktop" / "backend-dist"
WORK = REPO / "build" / "pyinstaller"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(
            "未找到 PyInstaller，请先安装：.\\.venv\\Scripts\\python.exe -m pip install pyinstaller",
            file=sys.stderr,
        )
        return 2
    sep = ";" if sys.platform == "win32" else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--noupx", "--name", "multiagent-backend",
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(WORK),
        "--paths", str(REPO),
        "--add-data", f"{REPO / 'multiagent' / 'web'}{sep}multiagent/web",
        "--collect-submodules", "multiagent",
        "--collect-data", "multiagent",
        "--hidden-import", "uvicorn.logging",
        "--hidden-import", "uvicorn.loops.auto",
        "--hidden-import", "uvicorn.protocols.http.auto",
        "--hidden-import", "uvicorn.protocols.websockets.auto",
        "--hidden-import", "uvicorn.lifespan.on",
        str(ENTRY),
    ]
    print(" ".join(cmd))
    code = subprocess.call(cmd)
    if code == 0:
        print(f"\n完成。后端产物在 {DIST}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
