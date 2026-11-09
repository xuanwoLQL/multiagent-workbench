"""Agent 接入层运行时回归测试（进程树回收 / 超时取消 / ACP 协议健壮性 / 解析）。

设计原则
--------
1. 每条断言都对应一个「可观测的外部事实」：某个 PID 是否还活着、返回码、
   写没写出文件、报错文本……不依赖内部实现细节。
2. 进程树测试用**独立实现**的 liveness 检查（ctypes WaitForSingleObject /
   os.kill），不复用被测代码，避免「自己验自己」。
3. 真实 Agent 端到端默认跳过，需 ``MULTIAGENT_REAL_AGENT=1`` 才跑（会消耗额度）。
"""

from __future__ import annotations

import ctypes
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from multiagent.agents import resolve as resolve_mod
from multiagent.agents.acp_adapter import AcpAdapter, _Session
from multiagent.agents.base import AgentRequest
from multiagent.agents.cli_adapter import CliAdapter
from multiagent.agents.resolve import wrap_executable

REPO_ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
IS_WIN = os.name == "nt"
REAL = os.environ.get("MULTIAGENT_REAL_AGENT") == "1"


# --------------------------------------------------------------------------- 工具


def _pid_alive(pid: int) -> bool:
    """独立实现的「进程是否存活」检查（故意不复用 multiagent.agents.proc）。"""
    if pid <= 0:
        return False
    if IS_WIN:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x00100000 | 0x1000, False, int(pid))  # SYNCHRONIZE|QUERY_LIMITED
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _wait_file(path: Path, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return True
        time.sleep(0.05)
    return path.exists()


def _read_pid(path: Path, timeout: float = 20.0) -> int:
    _wait_file(path, timeout)
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _shim(path: Path, argv: list[str], *, forward: bool = False) -> Path:
    """写一个启动 ``argv`` 的包装脚本（Windows: .cmd，POSIX: .sh）。"""
    if IS_WIN:
        target = path.with_suffix(".cmd")
        body = subprocess.list2cmdline(argv) + (" %*" if forward else "")
        target.write_text("@echo off\r\n" + body + "\r\n", encoding="utf-8")
    else:
        target = path.with_suffix(".sh")
        body = " ".join(shlex.quote(a) for a in argv) + (' "$@"' if forward else "")
        target.write_text("#!/bin/sh\nexec " + body + "\n", encoding="utf-8")
        target.chmod(0o755)
    return target


def _echo_args_shim(path: Path, out: Path) -> Path:
    """把收到的命令行参数写进 ``out`` 的包装脚本（用来证明参数没被引号吃掉）。"""
    if IS_WIN:
        target = path.with_suffix(".cmd")
        target.write_text(f'@echo off\r\necho %* > "{out}"\r\n', encoding="utf-8")
    else:
        target = path.with_suffix(".sh")
        target.write_text(f'#!/bin/sh\necho "$@" > "{out}"\n', encoding="utf-8")
        target.chmod(0o755)
    return target


def _acp_agent(tmp_path: Path, mode: str, **extra_env: str) -> dict:
    env = {"PYTHONPATH": str(REPO_ROOT), "FAKE_ACP_MODE": mode}
    env.update(extra_env)
    return {
        "id": "bad-acp",
        "label": "Bad ACP",
        "protocol": "acp",
        "command": PY,
        "args": ["-m", "multiagent.testing.fake_acp_bad"],
        "env": env,
        "roles": ["worker"],
        "can_write": True,
        "enabled": True,
    }


def _acp_request(tmp_path: Path, timeout_sec: int = 20, cancel_event=None) -> AgentRequest:
    return AgentRequest(
        prompt="ping",
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        timeout_sec=timeout_sec,
        run_id="run-test",
        task_id="task-test",
        cancel_event=cancel_event,
    )


def _run_bad_acp(
    tmp_path: Path, mode: str, timeout_sec: int = 20, *, shim: bool = False,
    cancel_event=None, **env,
):
    """跑一次「坏行为」假 ACP；``shim=True`` 时经过 cmd.exe 一层（复刻真实 .CMD）。"""
    agent = _acp_agent(tmp_path, mode, **env)
    if shim:
        wrapper = _shim(tmp_path / "codex-acp-shim", [PY, "-m", "multiagent.testing.fake_acp_bad"])
        command, args = wrap_executable(str(wrapper), [])
        adapter = AcpAdapter(agent, lambda a: (command, list(args)))
    else:
        adapter = AcpAdapter(agent)
    return adapter.run(_acp_request(tmp_path, timeout_sec, cancel_event=cancel_event))


def _cancel_after(event: threading.Event, seconds: float) -> threading.Timer:
    timer = threading.Timer(seconds, event.set)
    timer.daemon = True
    timer.start()
    return timer


# ======================================================= 1. 进程树回收（最重要）


def test_session_close_reaps_grandchildren(tmp_path):
    """复现：cmd.exe /c xxx.CMD 的孙进程在 close() 后必须消失。

    现状只 terminate 直接子进程（cmd.exe），孙进程（node/python）变孤儿。
    """
    pidfile = tmp_path / "grandchild.pid"
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
        "time.sleep(300)\n",
        encoding="utf-8",
    )
    wrapper = _shim(tmp_path / "spawn-tree", [PY, str(sleeper), str(pidfile)])
    command, args = wrap_executable(str(wrapper), [])
    session = _Session([command] + args, tmp_path, dict(os.environ), [])

    grandchild = _read_pid(pidfile)
    assert grandchild > 0, "孙进程未启动，测试夹具失效"
    assert _pid_alive(grandchild)

    session.close()

    assert _wait_dead(grandchild), f"孙进程 {grandchild} 在会话关闭后仍存活（孤儿）"


def test_orphan_survives_when_only_direct_child_is_killed(tmp_path):
    """对照实验：证明「只 kill 直接子进程」确实会留下孤儿（缺陷真实存在）。"""
    pidfile = tmp_path / "grandchild2.pid"
    sleeper = tmp_path / "sleeper2.py"
    sleeper.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    wrapper = _shim(tmp_path / "spawn-tree2", [PY, str(sleeper), str(pidfile)])
    command, args = wrap_executable(str(wrapper), [])
    # 用安全的命令行形式启动（旧实现也能起来），然后**只** kill 直接子进程 ——
    # 复刻旧 _Session.close() 的行为，证明孤儿确实会残留。
    from multiagent.agents.proc import spawn_command

    proc = subprocess.Popen(spawn_command([command] + args), cwd=str(tmp_path))
    try:
        grandchild = _read_pid(pidfile)
        assert grandchild > 0
        proc.terminate()  # 旧实现的做法：只杀直接子进程
        proc.wait(timeout=10)
        assert _pid_alive(grandchild), "对照实验失效：旧做法竟然回收了孙进程"
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(grandchild)],
                       capture_output=True) if IS_WIN else os.kill(grandchild, 9)


def test_acp_timeout_reaps_whole_tree(tmp_path):
    """复现：ACP 超时后，服务进程与它拉起的孙进程都必须被回收。"""
    server_pid = tmp_path / "server.pid"
    child_pid = tmp_path / "child.pid"
    result = _run_bad_acp(
        tmp_path,
        "pidfile,child_tree,hang",
        timeout_sec=6,
        shim=True,
        FAKE_ACP_PIDFILE=str(server_pid),
        FAKE_ACP_CHILD_PIDFILE=str(child_pid),
    )

    assert result.ok is False
    assert "超时" in (result.error or ""), result.error
    server = _read_pid(server_pid)
    child = _read_pid(child_pid)
    assert server > 0 and child > 0, "假 ACP 未按期启动"
    assert _wait_dead(server, 10), f"ACP 服务进程 {server} 超时后仍存活"
    assert _wait_dead(child, 10), f"ACP 孙进程 {child} 超时后成为孤儿"


def test_acp_duration_ms_measured_on_success(tmp_path):
    """复现：ACP 成功路径 duration_ms 硬编码 0（真实 opencode 跑 14.8s 记 0ms）。"""
    result = _run_bad_acp(tmp_path, "delay=1", timeout_sec=30)
    assert result.ok is True, result.error
    assert result.duration_ms >= 900, f"成功路径耗时失真：{result.duration_ms}ms"


def test_acp_duration_ms_measured_on_timeout(tmp_path):
    result = _run_bad_acp(tmp_path, "hang", timeout_sec=3)
    assert result.ok is False
    assert result.duration_ms >= 2500, f"超时路径耗时失真：{result.duration_ms}ms"


def test_acp_timeout_is_a_hard_limit(tmp_path):
    """复现：超时后再「温柔等待 3s」→ 真实耗时 = 设定超时 + grace，超时不再是硬上限。

    需求 41/45：卡住的 Agent 必须按时释放，不能白占并发槽位。
    """
    started = time.monotonic()
    result = _run_bad_acp(tmp_path, "hang", timeout_sec=1, shim=True)
    elapsed = time.monotonic() - started
    assert result.ok is False
    # 上限 2.6s：足以覆盖 CI/并发负载下的调度抖动，但仍能抓住「多等 3s grace」的回归
    assert elapsed < 2.6, f"1s 超时实际用了 {elapsed:.2f}s（疑似多等了 grace）"


def test_cli_timeout_is_a_hard_limit(tmp_path):
    """同上，CLI 路径（超时后必须立即整树强杀）。"""
    pidfile = tmp_path / "hard-limit.pid"
    wrapper = _shim(tmp_path / "cli-probe-hard", [PY], forward=True)
    command, args = wrap_executable(str(wrapper), [])
    agent = {
        "id": "cli-probe",
        "label": "CLI Probe",
        "protocol": "cli",
        "command": PY,
        "cli": {"command": PY, "args": ["-m", "multiagent.testing.fake_cli_probe"],
                "prompt_mode": "stdin"},
        "env": {
            "PYTHONPATH": str(REPO_ROOT),
            "FAKE_CLI_MODE": "tree=300",
            "FAKE_CLI_CHILD_PIDFILE": str(pidfile),
        },
        "can_write": True,
        "enabled": True,
    }
    adapter = CliAdapter(agent, lambda a: (command, list(args)))
    started = time.monotonic()
    result = adapter.run(_acp_request(tmp_path, timeout_sec=1))
    elapsed = time.monotonic() - started

    assert result.exit_code == -9, result.exit_code
    assert elapsed < 2.6, f"1s 超时实际用了 {elapsed:.2f}s（疑似多等了 grace）"
    child = _read_pid(pidfile)
    assert child > 0
    assert _wait_dead(child), f"硬超时后孙进程 {child} 仍存活"


def test_cli_timeout_reaps_process_tree(tmp_path):
    """复现：CLI 超时只杀直接子进程（util.run_command），孙进程成孤儿。"""
    pidfile = tmp_path / "cli-child.pid"
    wrapper = _shim(tmp_path / "cli-probe", [PY], forward=True)
    command, args = wrap_executable(str(wrapper), [])
    agent = {
        "id": "cli-probe",
        "label": "CLI Probe",
        "protocol": "cli",
        "command": PY,
        "cli": {
            "command": PY,
            "args": ["-m", "multiagent.testing.fake_cli_probe"],
            "prompt_mode": "stdin",
        },
        "env": {
            "PYTHONPATH": str(REPO_ROOT),
            "FAKE_CLI_MODE": "tree=300",
            "FAKE_CLI_CHILD_PIDFILE": str(pidfile),
        },
        "can_write": True,
        "enabled": True,
    }
    adapter = CliAdapter(agent, lambda a: (command, list(args)))
    result = adapter.run(_acp_request(tmp_path, timeout_sec=4))

    assert result.exit_code == -9, f"期望超时 -9，实际 {result.exit_code}"
    child = _read_pid(pidfile)
    assert child > 0, "CLI 孙进程未启动（说明 cmd 包装后的参数没有正确透传）"
    assert _wait_dead(child), f"CLI 超时后孙进程 {child} 成为孤儿"


def test_orphan_after_parent_exit_is_still_reaped(tmp_path):
    """复现：直接子进程先退出、孙进程仍活着 —— 光靠 taskkill /T 抓不到。

    必须靠 Windows Job Object（KILL_ON_JOB_CLOSE）或 POSIX 进程组兜底。
    """
    if not IS_WIN:
        pytest.skip("该场景针对 Windows cmd.exe 提前退出")
    pidfile = tmp_path / "detached-child.pid"
    orph = tmp_path / "orphan.py"
    orph.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    # 用 start 启动后父进程立即退出，孙进程被「甩开」
    cmd_file = tmp_path / "detach.cmd"
    cmd_file.write_text(
        f'@echo off\r\nstart "" /b "{PY}" "{orph}" "{pidfile}"\r\n',
        encoding="utf-8",
    )
    command, args = wrap_executable(str(cmd_file), [])
    session = _Session([command] + args, tmp_path, dict(os.environ), [])
    child = _read_pid(pidfile)
    assert child > 0, "脱离的孙进程未启动"
    time.sleep(1.0)
    session.close()
    assert _wait_dead(child), f"父进程已退出，孙进程 {child} 未能回收"


# ======================================================= 2. 协作式取消（需求 54）


def test_acp_cancel_event_terminates_promptly_and_reaps_tree(tmp_path):
    """复现：设置 AgentRequest.cancel_event 后，卡住的 ACP 会话必须尽快整树消失。

    计时包含「Python 解释器启动 + shim + ACP 握手」；取消轮询粒度是 200ms。
    整套测试并行/机器负载高时启动可能多花 1s+，因此上限取 3.5s：
    真正要锁的是「取消能穿透握手/提示词等待并回收整树」，不是冷启动耗时。
    """
    from multiagent.agents.proc import CANCELLED_EXIT

    server_pid = tmp_path / "cancel-server.pid"
    child_pid = tmp_path / "cancel-child.pid"
    event = threading.Event()
    _cancel_after(event, 0.7)

    started = time.monotonic()
    result = _run_bad_acp(
        tmp_path,
        "pidfile,child_tree,hang",
        timeout_sec=120,  # 超时设得很长：能停下来只可能是因为取消了
        shim=True,
        cancel_event=event,
        FAKE_ACP_PIDFILE=str(server_pid),
        FAKE_ACP_CHILD_PIDFILE=str(child_pid),
    )
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.exit_code == CANCELLED_EXIT, result.exit_code
    assert "取消" in (result.error or ""), result.error
    assert elapsed < 3.5, f"取消响应太慢：{elapsed:.2f}s"
    server = _read_pid(server_pid)
    child = _read_pid(child_pid)
    assert server > 0 and child > 0
    assert _wait_dead(server, 10), f"取消后 ACP 服务进程 {server} 仍存活"
    assert _wait_dead(child, 10), f"取消后 ACP 孙进程 {child} 成为孤儿"


def test_cli_cancel_event_terminates_within_1s_and_reaps_tree(tmp_path):
    from multiagent.agents.proc import CANCELLED_EXIT

    pidfile = tmp_path / "cli-cancel-child.pid"
    wrapper = _shim(tmp_path / "cli-probe-cancel", [PY], forward=True)
    command, args = wrap_executable(str(wrapper), [])
    agent = {
        "id": "cli-probe",
        "label": "CLI Probe",
        "protocol": "cli",
        "command": PY,
        "cli": {"command": PY, "args": ["-m", "multiagent.testing.fake_cli_probe"],
                "prompt_mode": "stdin"},
        "env": {
            "PYTHONPATH": str(REPO_ROOT),
            "FAKE_CLI_MODE": "tree=300",
            "FAKE_CLI_CHILD_PIDFILE": str(pidfile),
        },
        "can_write": True,
        "enabled": True,
    }
    event = threading.Event()
    _cancel_after(event, 0.7)
    adapter = CliAdapter(agent, lambda a: (command, list(args)))

    started = time.monotonic()
    result = adapter.run(_acp_request(tmp_path, timeout_sec=120, cancel_event=event))
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.exit_code == CANCELLED_EXIT, result.exit_code
    assert "取消" in (result.error or ""), result.error
    assert elapsed < 2.0, f"取消响应太慢：{elapsed:.2f}s"
    child = _read_pid(pidfile)
    assert child > 0
    assert _wait_dead(child, 10), f"取消后 CLI 孙进程 {child} 成为孤儿"


def test_cross_process_cancel_sentinel_stops_cli(tmp_path):
    """取消方与执行方不在同一进程时，哨兵文件必须同样能在 1s 级终止子进程。"""
    from multiagent.agents.proc import CANCELLED_EXIT, run_command_tree

    cancel_path = tmp_path / "cancel.requested"
    timer = threading.Timer(0.6, lambda: cancel_path.write_text("cancel", encoding="utf-8"))
    timer.start()
    started = time.monotonic()
    code, _out, err, _ms = run_command_tree(
        [PY, "-c", "import time; time.sleep(30)"],
        timeout=60,
        cancel_path=cancel_path,
    )
    elapsed = time.monotonic() - started
    timer.join(1)
    assert code == CANCELLED_EXIT, code
    assert elapsed < 3.0, f"跨进程取消响应太慢：{elapsed:.2f}s"
    assert "取消" in err or "cancelled" in err.lower(), err


def test_cancel_is_distinguishable_from_timeout_and_failure(tmp_path):
    """取消(-15) 必须与超时(-9/超时文案)、普通失败(真实退出码) 区分开。"""
    from multiagent.agents.proc import CANCELLED_EXIT

    # 超时（ACP 路径）：绝不能报成「取消」
    timeout_result = _run_bad_acp(tmp_path / "t", "hang", timeout_sec=2)
    assert timeout_result.exit_code != CANCELLED_EXIT
    assert "超时" in (timeout_result.error or "")
    assert "取消" not in (timeout_result.error or "")

    # 超时（CLI 路径）：固定 -9
    cli_timeout = CliAdapter(_cli_agent("sleep=30")).run(
        _acp_request(tmp_path / "ct", timeout_sec=2)
    )
    assert cli_timeout.exit_code == -9, cli_timeout.exit_code

    # 取消：-15 + 明确文案，且不能混成超时
    event = threading.Event()
    _cancel_after(event, 0.5)
    cancel_result = _run_bad_acp(tmp_path / "c", "hang", timeout_sec=120, cancel_event=event)
    assert cancel_result.exit_code == CANCELLED_EXIT
    assert "取消" in (cancel_result.error or "")
    assert "超时" not in (cancel_result.error or ""), cancel_result.error

    # 普通失败：透传真实退出码
    failed = CliAdapter(_cli_agent("exit=7")).run(_acp_request(tmp_path / "f"))
    assert failed.exit_code == 7


def test_unset_cancel_event_runs_to_completion(tmp_path):
    """向后兼容：传了但从不 set 的 event，不得改变任何行为。"""
    event = threading.Event()
    result = _run_bad_acp(tmp_path, "delay=1", timeout_sec=30, cancel_event=event)
    assert result.ok is True, result.error
    assert result.exit_code == 0

    cli_event = threading.Event()
    cli_result = CliAdapter(_cli_agent("echo")).run(
        _acp_request(tmp_path / "cli", cancel_event=cli_event)
    )
    assert cli_result.ok is True, cli_result.error


def test_no_cancel_event_is_backward_compatible(tmp_path):
    """默认 None：与历史版本完全一致。"""
    result = _run_bad_acp(tmp_path, "delay=1", timeout_sec=30)
    assert result.ok is True, result.error
    assert result.exit_code == 0


# ======================================================= 3. resolve.py


def test_resolve_cmd_wrapper_uses_safe_form():
    """含空格路径 + 含空格参数：必须走 /d /s /c "..." 的安全形式。"""
    command, args = wrap_executable(r"C:\Program Files\a b\codex-acp.CMD", ["--x"])
    assert command.lower() in ("cmd.exe", "cmd")
    assert [a.lower() for a in args[:3]] == ["/d", "/s", "/c"]
    assert args[3].startswith('"') and args[3].endswith('"')
    assert "codex-acp.CMD" in args[3] and "--x" in args[3]


def test_resolve_exe_is_not_wrapped():
    command, args = wrap_executable(r"C:\tools\opencode.EXE", ["acp"])
    assert command.endswith("opencode.EXE") and args == ["acp"]


@pytest.mark.skipif(not IS_WIN, reason="cmd.exe 引号规则仅 Windows")
def test_cmd_with_spaces_in_path_and_args_actually_runs(tmp_path):
    """复现：``cmd /c "含空格路径" "含空格参数"`` 会被 cmd 拆错（实测 rc=1）。"""
    spaced = tmp_path / "dir with spaces"
    spaced.mkdir()
    out = tmp_path / "args.txt"
    wrapper = _echo_args_shim(spaced / "echoer", out)
    command, args = wrap_executable(str(wrapper), ["arg with space"])

    session = _Session([command] + args, tmp_path, dict(os.environ), [])
    session.close()

    assert _wait_file(out), "含空格路径的 .cmd 没有被执行"
    assert "arg with space" in out.read_text(encoding="utf-8")


def test_resolve_finds_executable_on_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if IS_WIN:
        (bindir / "mytool.cmd").write_text("@echo off\r\necho hi\r\n", encoding="utf-8")
    else:
        tool = bindir / "mytool"
        tool.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    resolved = resolve_mod.resolve_agent_argv({"protocol": "acp", "command": "mytool", "args": ["acp"]})
    assert resolved is not None
    if IS_WIN:
        assert resolved[0].lower().startswith("cmd")
        assert "mytool.cmd" in resolved[1][3].lower()
    else:
        assert resolved[0] == str(bindir / "mytool")


def test_resolve_missing_executable_reports_clearly(tmp_path):
    agent = {"protocol": "acp", "command": "definitely-not-installed-xyz", "args": []}
    assert resolve_mod.resolve_agent_argv(agent) is None
    message = resolve_mod.explain_missing(agent)
    assert "definitely-not-installed-xyz" in message
    assert "PATH" in message

    result = AcpAdapter(agent).run(_acp_request(tmp_path, timeout_sec=5))
    assert result.ok is False
    assert result.exit_code == -2
    assert "definitely-not-installed-xyz" in (result.error or "")


def test_reg_fallback_tolerates_non_utf8_and_none_stdout(monkeypatch):
    """复现（生产日志）：reg.exe 的 GBK 输出让 text=True 的 reader 线程崩掉，
    .stdout 变成 None → ``None.splitlines()`` → /api/agents/detail 500。"""
    captured: dict = {}

    class _Result:
        stdout = None  # reader 线程崩掉后真实的形态

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(resolve_mod.subprocess, "run", fake_run)
    assert resolve_mod._find_dsh_registry() is None  # 不再抛 AttributeError
    assert captured.get("errors") == "replace", "reg.exe 调用必须显式 errors=replace"
    assert captured.get("encoding") == "utf-8"


def test_reg_fallback_swallows_missing_reg(monkeypatch):
    def boom(*_a, **_k):
        raise FileNotFoundError("reg.exe")

    monkeypatch.setattr(resolve_mod.subprocess, "run", boom)
    assert resolve_mod._find_dsh_registry() is None


def test_subprocess_text_decode_never_kills_reader_thread(tmp_path):
    """复现：子进程吐非 UTF-8 字节时，不能出现 _readerthread 异常、也不能返回 None。"""
    script = tmp_path / "gbk_out.py"
    script.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'\\xcb\\xf9\\xd3\\xd0 InstallLocation REG_SZ C:\\\\x\\r\\n')\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    captured: list[BaseException] = []
    previous = threading.excepthook
    threading.excepthook = lambda args: captured.append(args.exc_value)
    try:
        proc = resolve_mod._run_text([PY, str(script)])
    finally:
        threading.excepthook = previous

    assert proc.stdout is not None, "reader 线程崩掉后 stdout 变成了 None"
    assert "InstallLocation" in proc.stdout
    assert not any(isinstance(e, UnicodeDecodeError) for e in captured), captured


def test_find_dsh_never_raises(monkeypatch):
    """兜底链任何一环出错都必须干净返回 None，而不是把异常抛给 API。"""
    monkeypatch.delenv("MULTIAGENT_DSH", raising=False)
    monkeypatch.setattr(resolve_mod, "which", lambda name: None)
    monkeypatch.setattr(resolve_mod, "_find_dsh_registry", lambda: (_ for _ in ()).throw(OSError("boom")))
    try:
        resolve_mod._find_dsh()
    except OSError:
        # 目前允许冒泡，但必须能被上层兜住；这里显式记录该行为
        pytest.fail("_find_dsh 不应把兜底链异常抛给调用方")


# ======================================================= 3. ACP 协议健壮性


def test_acp_tolerates_non_json_and_blank_stdout(tmp_path):
    result = _run_bad_acp(tmp_path, "noise")
    assert result.ok is True, result.error
    assert any("完成" in m for m in result.messages)


def test_acp_parses_very_long_line(tmp_path):
    result = _run_bad_acp(tmp_path, "hugeline=2000000")
    assert result.ok is True, result.error
    assert any(len(m) >= 1_000_000 for m in result.messages), [len(m) for m in result.messages]


def test_acp_truncates_over_limit_line_without_hanging(tmp_path, monkeypatch):
    """超过行上限时截断并继续，不阻塞、不爆内存、不影响后续消息。"""
    import multiagent.agents.acp_adapter as acp

    monkeypatch.setattr(acp, "_MAX_LINE_CHARS", 64 * 1024)
    result = _run_bad_acp(tmp_path, "hugeline=3000000")
    assert result.ok is True, result.error
    assert max((len(m) for m in result.messages), default=0) <= 200_000
    assert any("完成" in m for m in result.messages)


def test_acp_stderr_flood_does_not_deadlock(tmp_path):
    """复现：stderr 持续大量输出时不能与客户端互锁。"""
    started = time.monotonic()
    result = _run_bad_acp(tmp_path, "stderr_flood=20000", timeout_sec=60)
    elapsed = time.monotonic() - started
    assert result.ok is True, result.error
    assert elapsed < 45, f"疑似管道死锁，耗时 {elapsed:.1f}s"
    assert len(result.stderr) < 500_000, "stderr 保留量没有上限"


def test_acp_process_died_early_reports_clear_error(tmp_path):
    result = _run_bad_acp(tmp_path, "die_early")
    assert result.ok is False
    assert "提前退出" in (result.error or ""), result.error


def test_acp_jsonrpc_error_response_is_reported(tmp_path):
    result = _run_bad_acp(tmp_path, "rpc_error")
    assert result.ok is False
    assert "上游模型不可用" in (result.error or ""), result.error


def test_acp_snake_case_update_variant(tmp_path):
    """复现：部分实现的 session/update 用 snake_case —— 必须等价识别。"""
    result = _run_bad_acp(tmp_path, "snake")
    assert result.ok is True, result.error
    assert any("完成" in m for m in result.messages), result.messages


def test_acp_bad_params_do_not_crash_run(tmp_path):
    """复现：params 不是对象时现状抛 AttributeError，整次运行失败。"""
    result = _run_bad_acp(tmp_path, "bad_params")
    assert result.ok is True, f"params 非法导致整次运行失败：{result.error}"


def test_acp_cumulative_usage_is_not_double_counted(tmp_path):
    """复现：连发 3 次累计型 usage(total=100) 现状会累加成 300。"""
    result = _run_bad_acp(tmp_path, "usage_cumulative")
    assert result.ok is True, result.error
    assert result.cost_tokens == 100, f"token 重复计数：{result.cost_tokens}"


def test_acp_usage_used_size_variant(tmp_path):
    """复现：真实 opencode 发的是 {used,size,cost}，现状 token 记 0。"""
    result = _run_bad_acp(tmp_path, "usage_used")
    assert result.ok is True, result.error
    assert result.cost_tokens == 1234, f"未识别 used/size：{result.cost_tokens}"
    assert result.cost_amount > 0, "未识别 cost.amount"


def test_acp_unknown_request_gets_method_not_found(tmp_path):
    reply_file = tmp_path / "probe.json"
    result = _run_bad_acp(tmp_path, "unknown_method", FAKE_ACP_PROBE_FILE=str(reply_file))
    assert result.ok is True, result.error
    _wait_file(reply_file)
    assert reply_file.exists(), "客户端对未知方法请求没有回复"
    payload = json.loads(reply_file.read_text(encoding="utf-8"))
    assert (payload.get("error") or {}).get("code") == -32601


def test_acp_cancel_stop_reason_is_surfaced(tmp_path):
    result = _run_bad_acp(tmp_path, "cancel")
    assert result.ok is False
    assert "取消" in (result.error or ""), result.error


def test_acp_zero_timeout_means_no_deadline(tmp_path):
    """复现：timeout_sec<=0（表示不限时）现状被 max(5,...) 压成 5s。"""
    result = _run_bad_acp(tmp_path, "delay=6", timeout_sec=0)
    assert result.ok is True, f"timeout_sec=0 被误当成短超时：{result.error}"


def test_acp_success_does_not_send_spurious_cancel(tmp_path):
    """正常 end_turn 结束后不应再发 session/cancel（避免污染真实 Agent 会话）。"""
    log = tmp_path / "methods.txt"
    result = _run_bad_acp(tmp_path, "method_log", FAKE_ACP_METHOD_LOG=str(log))
    assert result.ok is True, result.error
    _wait_file(log)
    methods = log.read_text(encoding="utf-8")
    assert "session/prompt" in methods
    assert "session/cancel" not in methods, methods


# ======================================================= 4. cli_adapter


def _cli_agent(mode: str = "echo", *, prompt_mode: str = "stdin", args=None, extra_cli=None, **env) -> dict:
    cli = {
        "command": PY,
        "args": ["-m", "multiagent.testing.fake_cli_probe"] if args is None else args,
        "prompt_mode": prompt_mode,
    }
    if extra_cli:
        cli.update(extra_cli)
    agent_env = {"PYTHONPATH": str(REPO_ROOT), "FAKE_CLI_MODE": mode}
    agent_env.update(env)
    return {
        "id": "cli-probe",
        "label": "CLI Probe",
        "protocol": "cli",
        "cli": cli,
        "env": agent_env,
        "can_write": True,
        "enabled": True,
    }


def _payload(result) -> dict:
    line = [ln for ln in result.stdout.splitlines() if ln.strip()][-1]
    return json.loads(line)


def test_cli_prompt_mode_stdin(tmp_path):
    result = CliAdapter(_cli_agent("echo")).run(_acp_request(tmp_path))
    assert result.ok is True, result.error
    assert _payload(result)["stdin"] == "ping"


def test_cli_prompt_mode_arg_with_placeholder(tmp_path):
    agent = _cli_agent("echo", prompt_mode="arg", args=["-m", "multiagent.testing.fake_cli_probe", "{prompt}"])
    result = CliAdapter(agent).run(_acp_request(tmp_path))
    assert result.ok is True, result.error
    assert _payload(result)["argv"][-1] == "ping"


def test_cli_prompt_mode_arg_without_placeholder_still_delivers(tmp_path):
    """复现：prompt_mode=arg 但参数里没有 {prompt} 也没开 append_prompt → 提示词被静默丢弃。"""
    result = CliAdapter(_cli_agent("echo", prompt_mode="arg")).run(_acp_request(tmp_path))
    assert result.ok is True, result.error
    assert "ping" in _payload(result)["argv"], _payload(result)["argv"]


def test_cli_prompt_mode_append_alias(tmp_path):
    agent = _cli_agent("echo", prompt_mode="append")
    result = CliAdapter(agent).run(_acp_request(tmp_path))
    assert result.ok is True, result.error
    assert _payload(result)["argv"][-1] == "ping"


def test_cli_prompt_mode_arg_append_flag(tmp_path):
    agent = _cli_agent("echo", prompt_mode="arg", extra_cli={"append_prompt": True})
    result = CliAdapter(agent).run(_acp_request(tmp_path))
    assert result.ok is True, result.error
    assert _payload(result)["argv"][-1] == "ping"


def test_cli_prompt_mode_file_writes_prompt_file(tmp_path):
    agent = _cli_agent("echo", prompt_mode="file", args=["-m", "multiagent.testing.fake_cli_probe", "{prompt_file}"])
    result = CliAdapter(agent).run(_acp_request(tmp_path))
    assert result.ok is True, result.error
    assert (tmp_path / "run" / "prompt.txt").read_text(encoding="utf-8") == "ping"


def test_cli_exit_code_is_passed_through(tmp_path):
    result = CliAdapter(_cli_agent("exit=7")).run(_acp_request(tmp_path))
    assert result.ok is False
    assert result.exit_code == 7


def test_cli_gbk_output_is_decoded(tmp_path):
    """复现：中文 Windows 下 CLI 输出 GBK，按 utf-8 解码得到乱码。"""
    result = CliAdapter(_cli_agent("encoding_gbk")).run(_acp_request(tmp_path))
    assert result.ok is True, result.error
    assert "中文输出测试" in result.stdout, repr(result.stdout)


def test_cli_missing_executable_duration_is_not_negative(tmp_path):
    agent = _cli_agent("echo")
    agent["cli"]["command"] = "definitely-missing-xyz"
    result = CliAdapter(agent, lambda a: None).run(_acp_request(tmp_path))
    assert result.ok is False
    assert result.exit_code == -2
    assert result.duration_ms >= 0


# ======================================================= 5. 真实 Agent（按需）


def _which(name: str) -> str | None:
    return shutil.which(name)


def _node_cmd_pids() -> set[int]:
    """当前 node.exe / cmd.exe 的 PID 集合（用 tasklist，避免依赖 psutil）。"""
    out = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    ).stdout or ""
    pids: set[int] = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.strip().split('","')]
        if len(parts) >= 2 and parts[0].strip('"').lower() in ("node.exe", "cmd.exe"):
            try:
                pids.add(int(parts[1]))
            except ValueError:
                continue
    return pids


REAL_AGENTS = [
    ("codex", "codex-acp", [], []),
    ("opencode", "opencode", ["acp"], []),
    ("dsh", "dsh", ["--profile", "acp"], ["resolve"]),
    ("claude", "claude-agent-acp", [], []),
]


@pytest.mark.skipif(not REAL, reason="设 MULTIAGENT_REAL_AGENT=1 才跑真实 Agent（消耗额度）")
@pytest.mark.parametrize("agent_id,command,args,flags", REAL_AGENTS)
def test_real_agent_tiny_task_writes_hello(agent_id, command, args, flags, tmp_path):
    """真实 Agent 跑极小任务：临时目录写 hello.txt。"""
    spec = {"id": agent_id, "protocol": "acp", "command": command, "args": args}
    if "resolve" in flags:
        spec["resolve"] = "dsh"
    from multiagent.agents.resolve import resolve_agent_argv

    if resolve_agent_argv(spec) is None:
        pytest.skip(f"{agent_id} 未安装")

    result = AcpAdapter(spec).run(
        AgentRequest(
            prompt="在工作目录下创建文件 hello.txt，内容恰好是 hello。做完立刻结束，不要做其它事。",
            workspace=tmp_path,
            run_dir=tmp_path / "run",
            timeout_sec=300,
            run_id="real-run",
            task_id="real-task",
            approve=lambda req: True,
        )
    )
    hello = tmp_path / "hello.txt"
    assert result.ok is True, f"{agent_id} 失败：{result.error} / {result.stderr[-400:]}"
    assert hello.exists(), f"{agent_id} 未产出 hello.txt"
    assert "hello" in hello.read_text(encoding="utf-8", errors="replace")
    assert result.duration_ms > 0, "真实运行 duration_ms 不能为 0"


@pytest.mark.skipif(not REAL, reason="设 MULTIAGENT_REAL_AGENT=1 才跑真实 Agent（消耗额度）")
@pytest.mark.parametrize("agent_id,command,args,flags", REAL_AGENTS[:2])
def test_real_agent_cancel_kills_process_tree(agent_id, command, args, flags, tmp_path):
    """需求 54：真实 Agent 跑长任务 → 触发 cancel_event → 进程消失、无孤儿。"""
    from multiagent.agents.proc import CANCELLED_EXIT
    from multiagent.agents.resolve import resolve_agent_argv

    spec = {"id": agent_id, "protocol": "acp", "command": command, "args": args}
    if resolve_agent_argv(spec) is None:
        pytest.skip(f"{agent_id} 未安装")

    event = threading.Event()
    before = _node_cmd_pids()
    during: set[int] = set()
    stop_watch = threading.Event()

    def watcher() -> None:
        # 整个运行期间持续累积「新出现的 node/cmd」，它们就是本次运行产生的进程树
        while not stop_watch.is_set():
            time.sleep(0.3)
            during.update(_node_cmd_pids() - before)

    watch = threading.Thread(target=watcher, daemon=True)
    watch.start()
    _cancel_after(event, 4.0)

    started = time.monotonic()
    result = AcpAdapter(spec).run(
        AgentRequest(
            prompt="请从 1 数到 3000，每个数字单独一行输出，不要使用任何工具。",
            workspace=tmp_path,
            run_dir=tmp_path / "run",
            timeout_sec=600,  # 超时设长：能停下来只可能是取消了
            run_id="real-cancel",
            task_id="real-cancel",
            approve=lambda req: True,
            cancel_event=event,
        )
    )
    elapsed = time.monotonic() - started
    stop_watch.set()

    assert result.exit_code == CANCELLED_EXIT, (
        f"{agent_id} 未按取消收尾：exit={result.exit_code} err={result.error} "
        f"（若为 0 说明任务在取消信号前就跑完了，属测试用例失效）"
    )
    assert "取消" in (result.error or ""), result.error
    assert elapsed < 30, f"{agent_id} 取消响应太慢：{elapsed:.1f}s"

    time.sleep(3)
    survivors = [pid for pid in during if _pid_alive(pid)]
    assert not survivors, f"{agent_id} 取消后仍残留 node/cmd 进程：{survivors}"


@pytest.mark.skipif(not REAL, reason="设 MULTIAGENT_REAL_AGENT=1 才跑真实 Agent（消耗额度）")
@pytest.mark.parametrize("agent_id,command,args,flags", REAL_AGENTS)
def test_real_agent_acp_handshake(agent_id, command, args, flags, tmp_path):
    """四家 Agent 的 ACP 握手（initialize）必须成功。"""
    from multiagent.agents.resolve import resolve_agent_argv

    spec = {"id": agent_id, "protocol": "acp", "command": command, "args": args}
    if "resolve" in flags:
        spec["resolve"] = "dsh"
    if resolve_agent_argv(spec) is None:
        pytest.skip(f"{agent_id} 未安装")

    info = AcpAdapter(spec).check(timeout=90)
    assert info["installed"] is True
    assert info["ok"] is True, f"{agent_id} 握手失败：{info.get('detail')} / {info.get('stderr', '')[-300:]}"
