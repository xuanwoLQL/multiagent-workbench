"""假 CLI Agent（探针版）：`python -m multiagent.testing.fake_cli_probe`。

用于回归 ``CliAdapter`` 的提示词投递模式、退出码、编码解码与超时整树回收。
行为由 ``FAKE_CLI_MODE`` 驱动：

    echo            把收到的 stdin / argv 以 JSON 打到 stdout（默认）
    encoding_gbk    stdout 输出 GBK 字节的「中文」，验证解码回退
    sleep=<秒>      等一段时间再退出
    tree            先拉起一个长睡孙进程（pid 写到 FAKE_CLI_CHILD_PIDFILE），再 sleep
    exit=<码>       指定退出码
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _modes() -> list[str]:
    raw = os.environ.get("FAKE_CLI_MODE", "") or ""
    return [m.strip() for m in raw.split(",") if m.strip()]


def _opt(name: str, default: str) -> str:
    for mode in _modes():
        if mode.startswith(name + "="):
            return mode.split("=", 1)[1]
    return default


def _has(name: str) -> bool:
    return any(m == name or m.startswith(name + "=") for m in _modes())


def _write(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    try:
        stdin_text = sys.stdin.buffer.read().decode("utf-8", "replace")
    except (OSError, UnicodeDecodeError):
        stdin_text = ""

    if _has("encoding_gbk"):
        sys.stdout.buffer.write("中文输出测试：GBK 编码\n".encode("gbk"))
        sys.stdout.buffer.flush()
    else:
        _write(json.dumps({
            "stdin": stdin_text,
            "argv": sys.argv[1:],
            "cwd": os.getcwd(),
        }, ensure_ascii=False) + "\n")

    if _has("tree"):
        pidfile = os.environ.get("FAKE_CLI_CHILD_PIDFILE")
        if pidfile:
            code = "import os,sys,time\nopen(sys.argv[1],'w').write(str(os.getpid()))\ntime.sleep(600)\n"
            subprocess.Popen([sys.executable, "-c", code, pidfile])
        time.sleep(float(_opt("tree", "300") or 300))

    if _has("sleep"):
        time.sleep(float(_opt("sleep", "300") or 300))

    return int(_opt("exit", "0") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
