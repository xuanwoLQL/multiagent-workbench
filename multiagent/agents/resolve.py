"""把配置里的命令解析成真实可执行的 argv（Windows 下 .cmd/.bat 需要 cmd.exe 包一层）。

两个容易踩的坑，这里都显式处理：

1. **cmd.exe 的引号规则**：``cmd /c "C:\\a b\\x.cmd" "arg with space"`` 会被拆错
   （实测 rc=1，报 “'C:\\a' 不是内部或外部命令”）。正确形式是
   ``cmd.exe /d /s /c ""<path>" <args>"`` —— 一层外引号，``/s`` 负责去掉首尾引号。
   注意这种形式必须以**字符串**交给 ``Popen``（见 ``agents/proc.py``），
   因为 ``list2cmdline`` 会自作主张地重新转义。
2. **子进程输出的解码**：``text=True`` 不显式给 ``encoding``/``errors`` 时会按
   ``locale.getpreferredencoding()`` 严格解码。桌面壳/DSH 会带 ``PYTHONUTF8=1``，
   此时它变成严格 UTF-8；中文 Windows 上 ``reg.exe`` 的 GBK 输出会让 subprocess
   的 reader 线程抛 ``UnicodeDecodeError``，``.stdout`` 静默变成 ``None``，
   上层 ``None.splitlines()`` 直接把 ``/api/agents/detail`` 打成 500。
   本模块所有只读文本的子进程一律经 ``_run_text``。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Sequence

from ..util import which

DSH_CLI_REL = os.path.join("resources", "runtime", "cli", "bin", "dsh.cmd")
CMD_EXE = "cmd.exe"
_CMD_SWITCHES = ["/d", "/s", "/c"]

_NOT_FOUND_TEMPLATE = (
    "未找到可执行文件：{command}。已尝试：PATH 查找（含 PATHEXT 补全 .cmd/.exe/.bat）、"
    "resolve=dsh 的常见安装目录与注册表卸载项。请确认它已安装并加入 PATH，"
    "或在配置里写绝对路径，或设置环境变量 MULTIAGENT_DSH。"
)


def wrap_executable(path: str, args: Sequence[str]) -> tuple[str, list[str]]:
    """把可执行文件包装成可直接 spawn 的 ``(command, args)``。

    ``.cmd``/``.bat`` 返回 ``("cmd.exe", ["/d", "/s", "/c", '"<path>" <args>'])``：
    第 4 个元素本身就是**带外引号的整串**，真实 spawn 请走
    ``multiagent.agents.proc.spawn``，它会把这段还原成原始命令行字符串。
    """
    text = str(path)
    if os.name == "nt" and text.lower().endswith((".cmd", ".bat")):
        inner = subprocess.list2cmdline([text, *[str(a) for a in args]])
        return CMD_EXE, [*_CMD_SWITCHES, f'"{inner}"']
    return text, [str(a) for a in args]


def explain_missing(agent: dict[str, Any]) -> str:
    """「找不到可执行文件」的可读解释（给用户看，不是给日志看）。"""
    protocol = str(agent.get("protocol") or "cli").lower()
    command = agent.get("command")
    if protocol == "cli":
        cli = agent.get("cli") or {}
        command = cli.get("command") or command
    return _NOT_FOUND_TEMPLATE.format(command=command or agent.get("id") or "?")


def _run_text(argv: Sequence[str], timeout: float = 8.0) -> subprocess.CompletedProcess:
    """执行一个「只读文本」的短命令。

    显式指定 ``encoding``/``errors`` —— 这既避免了中文 Windows 上的乱码，
    也避免 reader 线程崩溃后 ``stdout`` 变成 ``None`` 把上层打崩。
    """
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _find_dsh() -> str | None:
    """按「环境变量 → PATH → 常见安装目录 → 注册表」的顺序找 dsh CLI。

    这是一条**兜底链**，任何一环出问题都只降级到下一环，绝不把异常抛给调用方
    （``/api/agents/detail`` 会直接 500）。
    """
    try:
        env_override = os.environ.get("MULTIAGENT_DSH")
        if env_override and Path(env_override).exists():
            return env_override
        on_path = which("dsh")
        if on_path:
            return on_path
    except OSError:
        pass
    try:
        roots: list[Path] = []
        for key in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
            value = os.environ.get(key)
            if value:
                roots.append(Path(value))
                roots.append(Path(value) / "Programs")
        if os.environ.get("SystemDrive"):
            roots.append(Path(os.environ["SystemDrive"] + "\\"))
        for root in roots:
            try:
                if not root.exists():
                    continue
            except OSError:
                continue
            candidates = [root / DSH_CLI_REL]
            for name in ("DeepSeek Harness", "DeepSeek-Harness", "deepseek harness"):
                candidates.append(root / name / DSH_CLI_REL)
            for candidate in candidates:
                try:
                    if candidate.exists():
                        return str(candidate)
                except OSError:
                    continue
            try:
                children = list(root.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if not child.is_dir():
                        continue
                    normalized = child.name.lower().replace(" ", "").replace("-", "")
                    if normalized != "deepseekharness":
                        continue
                    candidate = child / DSH_CLI_REL
                    if candidate.exists():
                        return str(candidate)
                except OSError:
                    continue
    except OSError:
        pass
    try:
        return _find_dsh_registry()
    except Exception:  # noqa: BLE001 - 兜底链绝不能把异常抛给 /api/agents/detail
        return None


def _find_dsh_registry() -> str | None:
    """注册表卸载项兜底。

    注意 ``stdout`` 可能是 ``None``（reader 线程解码失败），必须用 ``or ""`` 兜住；
    同时 ``errors="replace"`` 保证乱码不会影响 ASCII 关键行的解析。
    """
    if os.name != "nt":
        return None
    keys = [
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall",
        r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for key in keys:
        try:
            result = _run_text(["reg.exe", "query", key, "/s", "/f", "DeepSeek Harness", "/d"])
        except (OSError, subprocess.SubprocessError):
            continue
        out = getattr(result, "stdout", None) or ""
        if not isinstance(out, str):
            continue
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith(("InstallLocation", "DisplayIcon", "UninstallString")):
                continue
            _, _, value = line.partition("REG_")
            value = value.split(None, 1)[-1].strip().strip('"')
            if not value:
                continue
            value = value.split(",")[0]
            try:
                directory = Path(value)
                if directory.is_file():
                    directory = directory.parent
                candidate = directory / DSH_CLI_REL
                if candidate.exists():
                    return str(candidate)
            except OSError:
                continue
    return None


def resolve_agent_argv(agent: dict[str, Any]) -> tuple[str, list[str]] | None:
    """按 Agent 配置解析出可直接 spawn 的 (command, args)。

    优先按 ``protocol`` 取对应分支：``acp`` 用顶层 command/args，``cli`` 用 ``cli`` 子对象。
    """
    protocol = str(agent.get("protocol") or "cli").lower()
    if protocol == "cli":
        return resolve_cli_argv(agent)
    return _resolve_spec(
        agent,
        command=agent.get("command"),
        args=agent.get("args"),
        resolve=agent.get("resolve"),
    )


def resolve_cli_argv(agent: dict[str, Any]) -> tuple[str, list[str]] | None:
    """一次性 CLI 模式：解析 ``cli`` 子对象（没有则回退顶层 command/args）。"""
    cli = agent.get("cli") or {}
    return _resolve_spec(
        agent,
        command=cli.get("command") or agent.get("command"),
        args=cli.get("args") if cli.get("args") is not None else agent.get("args"),
        resolve=agent.get("resolve"),
    )


def _resolve_spec(agent: dict[str, Any], *, command: str | None, args, resolve: str | None):
    if resolve == "dsh":
        path = _find_dsh()
        if not path:
            return None
        return wrap_executable(path, list(args or ["--profile", "acp"]))
    if not command:
        return None
    text = str(command)
    if os.path.isabs(text) or os.sep in text or "/" in text:
        try:
            path = text if Path(text).exists() else None
        except OSError:
            path = None
    else:
        path = which(text)
    if not path:
        return None
    return wrap_executable(path, list(args or []))


def probe_agent(agent: dict[str, Any]) -> dict[str, Any]:
    """只探测，不安装（C3）。返回 command/args/可用性。"""
    resolved = resolve_agent_argv(agent)
    if not resolved:
        return {
            "id": agent.get("id"),
            "label": agent.get("label"),
            "available": False,
            "command": agent.get("command"),
            "args": agent.get("args"),
            "detail": explain_missing(agent),
        }
    command, args = resolved
    return {
        "id": agent.get("id"),
        "label": agent.get("label"),
        "available": True,
        "command": command,
        "args": args,
    }
