"""用 headless Edge 给控制台截图（开发/验收用，不进入运行时依赖）。

示例：
    .\\.venv\\Scripts\\python.exe scripts\\ui_snapshot.py --out .e2e/home-1440.png
    .\\.venv\\Scripts\\python.exe scripts\\ui_snapshot.py --home .e2e\\demo-home --port 8905 \
        --out .e2e/demo-1440.png --size 1440x900 --wait 6000

它做三件事：起一个临时后端（若端口未占用）→ 等 /api/health → 用 Edge
--headless=new --screenshot 截图 → 关掉自己起的后端。已有服务可直接 --port 复用。
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_health(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - 还没起来
            time.sleep(0.3)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=str(REPO))
    parser.add_argument("--port", type=int, default=8903)
    parser.add_argument("--out", required=True)
    parser.add_argument("--size", default="1440x900")
    parser.add_argument("--wait", type=int, default=6000, help="Edge 虚拟时间预算 ms")
    parser.add_argument("--path", default="/")
    args = parser.parse_args()

    edge = next((p for p in EDGE_CANDIDATES if Path(p).exists()), None)
    if not edge:
        print("找不到 Edge，可改用其它浏览器截图", file=sys.stderr)
        return 2

    proc = None
    if not port_open(args.port):
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-m", "multiagent", "serve", "--host", "127.0.0.1", "--port", str(args.port)],
            cwd=str(REPO),
            env=dict(env, MULTIAGENT_HOME=str(Path(args.home).resolve())),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    try:
        if not wait_health(args.port):
            print(f"后端 {args.port} 未就绪", file=sys.stderr)
            return 3
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        width, height = args.size.lower().split("x")
        cmd = [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--window-size={width},{height}",
            f"--virtual-time-budget={args.wait}",
            f"--screenshot={out.resolve()}",
            f"http://127.0.0.1:{args.port}{args.path}",
        ]
        subprocess.run(cmd, check=False, timeout=120)
        print(f"{out.resolve()} {out.stat().st_size if out.exists() else 'MISSING'}")
        return 0 if out.exists() else 4
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
