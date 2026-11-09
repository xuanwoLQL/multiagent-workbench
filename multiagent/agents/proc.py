"""子进程生命周期工具：**整树回收** + 字节精确捕获 + 编码回退。

为什么需要单独一层：
- Windows 上 ``Popen(["cmd.exe", "/c", r"C:\\...\\x.CMD", ...])`` 的真实进程树是
  ``cmd.exe → node.exe``。``Popen.terminate()/kill()`` 只作用于直接子进程，
  杀掉 cmd.exe 会让 node.exe 变成孤儿继续跑（占端口、占额度、写文件）。
  必须用进程组：Windows 用 Job Object（best-effort，覆盖「父已先退出」的形态）
  + ``taskkill /F /T /PID``；POSIX 用 ``start_new_session`` + ``killpg``。
- ``subprocess`` 的 ``text=True`` 在没有显式 ``encoding``/``errors`` 时按
  ``locale.getpreferredencoding()`` 严格解码。桌面壳/DSH 会带 ``PYTHONUTF8=1``，
  此时解码器变成严格 UTF-8，中文 Windows 上 ``reg.exe``/CLI 的 GBK 字节会让
  reader 线程抛 ``UnicodeDecodeError``，``.stdout`` 静默变成 ``None``。
  这里统一走「先按 UTF-8 严格试，失败再退回本地代码页」的解码策略。
"""

from __future__ import annotations

import locale
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Any, Sequence

IS_WIN = os.name == "nt"

#: 单行输出上限（字符）。超过则截断并丢弃该行剩余部分，避免内存被单行吃穿。
MAX_LINE_CHARS = 16 << 20

#: 协作式取消时返回的退出码（与超时的 -9、找不到的 -2 区分开）。
CANCELLED_EXIT = -15


def cancel_requested(cancel_event: Any = None, cancel_path: str | os.PathLike[str] | None = None) -> bool:
    """取消判定：进程内 Event 或跨进程哨兵文件任一命中。"""
    if cancel_event is not None and cancel_event.is_set():
        return True
    if cancel_path:
        try:
            return os.path.exists(cancel_path)
        except OSError:
            return False
    return False

#: 轮询取消信号的粒度（秒）。保证 200ms 级响应，又不至于忙等。
CANCEL_POLL_SECONDS = 0.2

#: 正常收尾时，等待「温柔退出」的秒数（调用方已关闭 stdin，给进程一点时间自己退干净）。
GRACE_SECONDS = 3.0

#: 强制回收后，确认进程真的消失的秒数。
CONFIRM_SECONDS = 0.5


# --------------------------------------------------------------------- 进程存活

def process_alive(pid: int) -> bool:
    """进程是否仍在运行（跨平台；已退出的僵尸不算活着）。"""
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        try:
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        except OSError:  # pragma: no cover - 非 Windows 不会走到
            return False
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x00100000 | 0x1000, False, int(pid))  # SYNCHRONIZE|QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return stat.split(") ", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


# --------------------------------------------------------------------- Job Object

if IS_WIN:  # pragma: no cover - 平台分支
    import ctypes
    from ctypes import wintypes

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(wintypes.ULONG)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001


def _create_kill_on_close_job():  # pragma: no cover - 平台分支
    """创建一个「句柄关闭即杀死所有成员」的 Job Object；失败返回 None。"""
    if not IS_WIN:
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        if not k32.SetInformationJobObject(
            job, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            k32.CloseHandle(job)
            return None
        return job
    except (OSError, AttributeError):
        return None


def _assign_to_job(job, pid: int) -> bool:  # pragma: no cover - 平台分支
    if not job or not IS_WIN:
        return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        handle = k32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, int(pid))
        if not handle:
            return False
        try:
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            return bool(k32.AssignProcessToJobObject(job, handle))
        finally:
            k32.CloseHandle(handle)
    except (OSError, AttributeError):
        return False


def _close_job(job) -> None:  # pragma: no cover - 平台分支
    if not job or not IS_WIN:
        return
    try:
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
    except (OSError, AttributeError):
        pass


# --------------------------------------------------------------------- 命令行拼装

def _windows_command_line(argv: Sequence[str]) -> str | None:
    """把 ``cmd.exe /d /s /c "<inner>"`` 形式还原成原始命令行字符串。

    ``cmd.exe`` 对引号的解析很挑：``cmd /c "C:\\a b\\x.cmd" "arg with space"``
    会被拆成 ``C:\\a`` + 一堆参数（实测 rc=1）；只有 ``/d /s /c "…"``
    这种「一层外引号」形式才稳。Python 的 ``list2cmdline`` 会自作主张地重新加引号，
    所以这种形式必须以**字符串**交给 ``Popen``，绕开它的转义。
    """
    if not IS_WIN or len(argv) < 5:
        return None
    if Path(str(argv[0])).name.lower() not in ("cmd.exe", "cmd"):
        return None
    if [str(a).lower() for a in argv[1:4]] != ["/d", "/s", "/c"]:
        return None
    inner = str(argv[4])
    if not (inner.startswith('"') and inner.endswith('"') and len(inner) >= 2):
        return None
    inner = inner[1:-1]
    trailing = [str(a) for a in argv[5:]]
    if trailing:
        inner = inner + " " + subprocess.list2cmdline(trailing)
    return f'{argv[0]} /d /s /c "{inner}"'


def spawn_command(argv: Sequence[str] | str) -> Sequence[str] | str:
    """返回可直接交给 ``Popen`` 的目标（可能是字符串）。"""
    if isinstance(argv, str):
        return argv
    line = _windows_command_line(argv)
    return line if line else list(argv)


# --------------------------------------------------------------------- spawn / kill

def spawn(
    argv: Sequence[str] | str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    stdin: Any = None,
    stdout: Any = None,
    stderr: Any = None,
    text: bool = False,
    bufsize: int = -1,
) -> subprocess.Popen:
    """启动子进程并把它放进独立进程组 / Job Object（便于整树回收）。"""
    kwargs: dict[str, Any] = {"cwd": str(cwd) if cwd else None, "env": env}
    if IS_WIN:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        spawn_command(argv),
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=text,
        encoding="utf-8" if text else None,
        errors="replace" if text else None,
        bufsize=bufsize,
        **kwargs,
    )
    job = _create_kill_on_close_job() if IS_WIN else None
    if job is not None:
        if not _assign_to_job(job, proc.pid):
            _close_job(job)
            job = None
    proc.job_handle = job  # type: ignore[attr-defined]
    return proc


def terminate_tree(
    proc: subprocess.Popen,
    *,
    wait_natural: float = GRACE_SECONDS,
    confirm: float = CONFIRM_SECONDS,
    close_streams: bool = True,
) -> int | None:
    """强制回收整棵进程树，返回直接子进程的退出码（拿不到则 None）。

    ``wait_natural`` 区分两种语义，不能共用一个常量：

    * **正常收尾路径**（调用方刚关闭 stdin）→ 给 ``GRACE_SECONDS``，让进程有机会
      自己退干净，避免无谓地强杀；
    * **超时 / 取消路径**（调用方**已经确认**它卡住了）→ 传 ``0``。此时再白等
      几秒毫无意义，只会让「1s 超时」实际变成「1s + grace」，让超时不再是硬上限，
      并且白占并发槽位（需求 41/45）。

    在这之后才是真正的整树终止（Windows：Job Object + ``taskkill /F /T``；
    POSIX：进程组 SIGTERM → SIGKILL）。
    """
    if wait_natural > 0 and proc.poll() is None:
        try:
            proc.wait(timeout=wait_natural)
        except subprocess.TimeoutExpired:
            pass

    if IS_WIN:
        if proc.poll() is None:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        job = getattr(proc, "job_handle", None)
        if job is not None:
            # 关掉最后一个 Job 句柄 = 杀死组内所有进程（含父已退出后的孤儿）
            _close_job(job)
            proc.job_handle = None  # type: ignore[attr-defined]
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
    else:
        _kill_process_group(proc, grace=wait_natural if wait_natural > 0 else confirm)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    try:
        proc.wait(timeout=max(confirm, 0.1))
    except subprocess.TimeoutExpired:
        pass
    # 释放管道，避免读线程一直挂在已断开的 pipe 上。
    # close_streams=False 时由调用方先读完残留输出再关（超时/取消也想拿到已有输出）。
    if close_streams:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
    return proc.returncode


def _kill_process_group(proc: subprocess.Popen, *, grace: float = GRACE_SECONDS) -> None:
    """POSIX：SIGTERM → 宽限 → SIGKILL，作用于整个进程组。"""
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        return
    if pgid == os.getpgid(0):
        return  # 安全兜底：绝不杀自己所在的组
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            return
        deadline = time.monotonic() + max(grace, 0.1)
        while time.monotonic() < deadline:
            if not process_alive(proc.pid):
                return
            time.sleep(0.05)


# --------------------------------------------------------------------- 运行 / 解码

def decode_output(data: bytes | str | None) -> str:
    """把子进程输出解码成文本：先严格 UTF-8，再退回本地代码页，最后替换。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    if not data:
        return ""
    candidates: list[str] = ["utf-8"]
    try:
        preferred = locale.getpreferredencoding(False)
    except (LookupError, ValueError):  # pragma: no cover
        preferred = ""
    normalized = preferred.lower().replace("-", "").replace("_", "") if preferred else ""
    if normalized and normalized not in ("utf8", "utf8mb4"):
        # English Windows uses cp1252, which decodes almost any byte sequence
        # without error. Try GBK first there so Chinese CLI output is not
        # silently turned into cp1252 mojibake.
        if normalized in ("cp1252", "latin1", "iso88591"):
            candidates.extend(["gbk", preferred])
        else:
            candidates.extend([preferred, "gbk"])
    else:
        candidates.append("gbk")
    for encoding in candidates:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def cap_text_bytes(text: str, limit: int, *, marker: str = "\n…[输出已截断]") -> str:
    """按 UTF-8 字节上限截断文本（保留头部；用于 stdout/stderr 持久化）。"""
    if not text or limit <= 0:
        return text
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text
    keep = max(0, limit - len(marker.encode("utf-8")))
    # 避免把多字节字符切一半：逐次回退到可解码边界
    while keep > 0:
        try:
            head = raw[:keep].decode("utf-8")
            return head + marker
        except UnicodeDecodeError:
            keep -= 1
    return marker


def run_command_tree(
    argv: Sequence[str] | str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = 60,
    stdin_text: str | None = None,
    cancel_event: Any = None,
    cancel_path: str | os.PathLike[str] | None = None,
    max_output_bytes: int = 32 * 1024 * 1024,
) -> tuple[int, str, str, int]:
    """执行命令并返回 ``(exit_code, stdout, stderr, duration_ms)``。

    与 ``util.run_command`` 的区别：

    * 超时 / 取消 / 异常时**回收整棵进程树**（超时 ``-9``，取消 ``CANCELLED_EXIT``）；
    * 按「UTF-8 → 本地代码页」解码，避免中文 Windows 下的乱码与 reader 线程崩溃；
    * ``cancel_event``（``threading.Event``）：每 ``CANCEL_POLL_SECONDS`` 轮询一次，
      一旦被 set 立即整树强杀并以 ``CANCELLED_EXIT`` 收尾。传 ``None`` 时行为与
      历史版本完全一致（一次阻塞式 ``communicate``）。
    * ``cancel_path``：跨进程取消哨兵文件；存在即视为取消。用于 CLI/API/桌面
      各自是独立进程、无法共享 ``threading.Event`` 的场景。
    """
    started = time.monotonic()
    payload = stdin_text.encode("utf-8") if stdin_text is not None else None
    try:
        proc = spawn(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return -2, "", f"[not-found] {exc}", int((time.monotonic() - started) * 1000)
    except OSError as exc:
        return -3, "", f"[os-error] {exc}", int((time.monotonic() - started) * 1000)

    effective_timeout = timeout if (timeout is None or timeout > 0) else None
    deadline = (time.monotonic() + effective_timeout) if effective_timeout is not None else None
    timed_out = False
    cancelled = False
    failed = False
    out_b: bytes = b""
    err_b: bytes = b""
    first = True

    while True:
        if cancel_requested(cancel_event, cancel_path):
            cancelled = True
            break
        chunk: float | None = None
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                timed_out = True
                break
            chunk = left
        if (cancel_event is not None or cancel_path) and not timed_out:
            # 轮询粒度：既能 200ms 级响应取消，又不会忙等
            chunk = CANCEL_POLL_SECONDS if chunk is None else min(CANCEL_POLL_SECONDS, chunk)
        try:
            out_b, err_b = proc.communicate(payload if first else None, timeout=chunk)
            break
        except subprocess.TimeoutExpired:
            first = False
            continue
        except (OSError, ValueError):
            failed = True
            break

    if cancelled or timed_out or failed:
        # 已知卡住（超时）或被人工终止：不再温柔等待，直接整树强杀。
        # close_streams=False：先读完残留输出再关，用户终止后仍能看到已有进展。
        terminate_tree(proc, wait_natural=0.0, close_streams=False)
        try:
            tail_out, tail_err = proc.communicate(timeout=5)
            out_b = out_b or tail_out or b""
            err_b = err_b or tail_err or b""
        except (subprocess.TimeoutExpired, ValueError, OSError):
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass

    if cancelled:
        code = CANCELLED_EXIT
    elif timed_out:
        code = -9
    elif failed:
        code = -3
    else:
        code = proc.returncode if proc.returncode is not None else -1

    duration = int((time.monotonic() - started) * 1000)
    stdout = decode_output(out_b)
    stderr = decode_output(err_b)
    if max_output_bytes and max_output_bytes > 0:
        stdout = cap_text_bytes(stdout, int(max_output_bytes))
        stderr = cap_text_bytes(stderr, int(max_output_bytes))
    if cancelled:
        stderr = (stderr + "\n[cancelled] 收到人工终止信号，已回收整棵进程树").strip()
    elif timed_out:
        stderr = (stderr + f"\n[timeout] 超过 {effective_timeout}s 未返回（已回收整棵进程树）").strip()
    return code, stdout, stderr, duration


def read_lines(stream: IO[str], limit_chars: int = MAX_LINE_CHARS):
    """按行读取文本流；单行超过 ``limit_chars`` 时截断并丢弃该行剩余部分。"""
    while True:
        try:
            chunk = stream.readline(limit_chars)
        except (ValueError, OSError):
            return
        if not chunk:
            return
        if chunk.endswith("\n") or len(chunk) < limit_chars:
            yield chunk
            continue
        # 行太长：把这一行的剩余部分读完丢掉，避免阻塞后续消息
        while True:
            try:
                rest = stream.readline(limit_chars)
            except (ValueError, OSError):
                return
            if not rest or rest.endswith("\n"):
                break
        yield chunk


def read_byte_lines(stream: IO[bytes], limit_bytes: int = MAX_LINE_CHARS):
    """按行读取**字节**流，逐行原样 yield ``bytes``（不做解码）。

    为什么要字节读取：``text=True`` 的管道在 ``PYTHONUTF8=1`` 下是**严格 UTF-8**，
    中文 Windows 上 CLI / ACP 服务端的 GBK 输出会让 reader 线程抛
    ``UnicodeDecodeError``，管道里剩下的字节全丢、``.stdout`` 变成 ``None``。
    改成「字节读取 + 调用方逐行 :func:`decode_output`」后，GBK 输出能被正确解码，
    reader 线程也不会崩。

    语义与 :func:`read_lines` 对齐：单行超过 ``limit_bytes`` 时截断该行并丢弃剩余部分
    （避免一个畸形超长行把内存吃穿或阻塞后续消息）。
    """
    while True:
        try:
            chunk = stream.readline(limit_bytes)
        except (ValueError, OSError):
            return
        if not chunk:
            return
        if chunk.endswith(b"\n") or len(chunk) < limit_bytes:
            yield chunk
            continue
        # 行太长：把这一行的剩余部分读完丢掉，避免阻塞后续消息
        while True:
            try:
                rest = stream.readline(limit_bytes)
            except (ValueError, OSError):
                return
            if not rest or rest.endswith(b"\n"):
                break
        yield chunk


def force_utf8_stdio() -> None:
    """（可选）把当前进程 stdio 调成 UTF-8 —— 需要时由调用方显式开启。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
