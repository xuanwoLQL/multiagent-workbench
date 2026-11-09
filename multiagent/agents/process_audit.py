"""Agent 启动的本地监听服务审计（P1-7：残留进程占端口）。

背景
----
Agent 在隔离工作副本里跑 ``npm run dev`` / ``python -m http.server`` /
``vite preview`` 时，命令本身可能很快退出，但它拉起的监听进程会活下来。
下一次任务（甚至用户自己的开发服务）就会撞端口。

策略：**宁可漏杀，不可误杀**
---------------------------
1. Agent 调用前拍一次「监听 TCP 的进程」快照；
2. 调用后再拍一次，只关心 ``after - before`` 的新增 pid；
3. 新增进程必须能证明「与本次工作目录有关」才算 owned：

   - command / exe_path 里出现规范化后的 workdir（Windows 大小写不敏感），或
   - prober 给出了 ``cwd`` 且该 cwd 位于 workdir 之下；

4. 只有 ``owned=True`` 且 ``reap=True`` 才终止；判断不了归属的只写 warning；
5. 永不终止 pid<=4、当前进程、父进程、调用前就存在的 pid。

审计是旁路能力：任何异常都被吞掉并折算成 ``supported=False`` + warning，
``finish()`` 绝不向调用方抛异常，不能让它拖垮 Agent 任务本身。

API 契约
--------
::

    audit = ProcessAudit.begin()            # 可注入 prober / killer（测试用）
    result = audit.finish(workdir, reap=True)

    result = {
        "supported": bool,
        "started": [{"pid": int, "ports": [int], "command": str, "owned": bool}],
        "reaped":  [{"pid": int, "ports": [int], "command": str}],
        "warnings": [str],
        "before_count": int,
        "after_count": int,
    }

``command`` 在返回前截断到 500 字符。``killer`` 默认是模块级
:func:`_terminate_pid`（Windows ``taskkill /F /T /PID``；POSIX SIGTERM → SIGKILL），
测试可传 ``ProcessAudit.begin(prober, killer=fake)`` 或 monkeypatch
:func:`_terminate_pid`。

Windows netstat 漏报兜底（CIM）
-----------------------------
本机实测 ``python -m http.server`` 的监听 socket 在 ``netstat -ano`` 里可能被
报成 ``FIN_WAIT``/``CLOSED``（进程内 socket 状态），只靠 netstat 会漏掉这个
监听者。审计因此允许 ``finish`` 额外执行一次
``Get-NetTCPConnection -State Listen``（CIM cmdlet），把结果与 netstat 端口
按 pid 取并集。该查询**只在 finish** 使用：runner 已把 finish 放到异步线程，
额外耗时不会拖慢任务返回；``begin`` 仍然只走 netstat，避免每个任务开始都付
一次全机 CIM 查询的成本。

CIM 查询失败/超时/输出无法解析时**不影响 netstat 的回收能力**：只在 prober 上
记录独立的降级标记（``cim_supported=False`` + ``cim_error`` 原因），由
``ProcessAudit.finish`` 折算成 warning；``supported`` 保持 True，仍按 netstat
结果做归属判定与回收。只有 netstat 本身失败时才会 ``supported=False``
（此时审计只报告新增监听、不执行回收：宁可漏杀，不可误杀）。

性能契约（避免审计拖慢任务）
----------------------------
- ``begin`` 只走轻量快照：Windows 仅 ``netstat``，POSIX 仅 ``ss``/``lsof``，
  不查任何进程元数据、不跑 CIM；
- ``finish`` 在 Windows 上额外跑一次 ``Get-NetTCPConnection -State Listen``
  （CIM），与 netstat 结果并集；POSIX 无此步骤。finish 由 runner 异步执行，
  CIM 通常 <1s（硬超时 5s），不阻塞 ``run_task`` 返回；
- ``finish`` diff 出**新增 pid** 之后，才对这些 pid 调一次 ``describe()``
  （Windows 才启动进程元数据 PowerShell）；没有新增监听时绝不查元数据；
- prober 只实现旧 ``snapshot()`` 或旧 ``listening()``（不接受 ``include_cim``）
  时照常兼容，自动回退到原调用；快照值已经带 ``command`` / ``exe_path`` 时
  跳过 ``describe()``。
"""

from __future__ import annotations

import inspect
import json
import os
import re
import signal
import subprocess
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .proc import decode_output, process_alive

IS_WIN = os.name == "nt"

#: 返回给调用方的命令/路径字符串上限（字符）。
MAX_COMMAND_CHARS = 500

#: 枚举/元数据命令的超时（秒）。PowerShell 查询按需求固定 5s。
DEFAULT_TIMEOUT_SEC = 5.0

#: 单次 PowerShell 批量查询的 pid 数，避免命令行过长。
POWERSHELL_BATCH = 50

_CIM_DATE_RE = re.compile(r"Date\((-?\d+)")
_SS_PID_RE = re.compile(r"pid=(\d+)")


# --------------------------------------------------------------------- 小工具


def _clip(value: Any, limit: int = MAX_COMMAND_CHARS) -> str:
    """把字符串截断到 ``limit`` 字符（含省略号），空值归一为 ``""``。"""
    text = "" if value is None else str(value)
    if limit <= 0 or len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _accepts_kwarg(func: Callable[..., Any], name: str) -> bool:
    """判断可调用对象是否接受某个关键字参数（用于兼容旧版 fake prober）。"""
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return False
    if name in parameters:
        return True
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _normalize_workdir(workdir: str | os.PathLike[str]) -> str:
    """resolve + normcase + 正斜杠（Windows 下大小写不敏感）。"""
    text = os.fspath(workdir)
    try:
        resolved = Path(text).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        resolved = Path(os.path.abspath(os.path.expanduser(text)))
    return os.path.normcase(str(resolved)).replace("\\", "/")


def _normalize_text(value: Any) -> str:
    """把 command / exe 字符串规范成可比较形态。

    Windows 下 ``normcase`` 会同时做大小写折叠与分隔符统一；POSIX 保持大小写敏感
    （只把反斜杠换成斜杠，便于跨平台测试）。
    """
    text = "" if value is None else str(value)
    if IS_WIN:
        text = os.path.normcase(text)
    return text.replace("\\", "/")


def _normalize_cwd(value: Any) -> str:
    """进程 cwd 的规范化（尽量解析成真实路径；不存在时退化为 abspath）。"""
    text = "" if value is None else str(value).strip()
    if not text:
        return ""
    try:
        text = os.path.realpath(os.path.abspath(os.path.expanduser(text)))
    except (OSError, ValueError):
        pass
    return os.path.normcase(text).replace("\\", "/")


def _is_owned(info: Mapping[str, Any], workdir_norm: str) -> bool:
    """新增监听进程是否属于本次 Agent 调用（保守判定，失败即 False）。"""
    if not workdir_norm:
        return False
    command = _normalize_text(info.get("command"))
    exe_path = _normalize_text(info.get("exe_path"))
    if workdir_norm in command or workdir_norm in exe_path:
        return True
    cwd = _normalize_cwd(info.get("cwd"))
    if cwd:
        prefix = workdir_norm if workdir_norm.endswith("/") else workdir_norm + "/"
        if cwd == workdir_norm or cwd.startswith(prefix):
            return True
    return False


def _normalize_ports(value: Any) -> list[int]:
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, (int, float, str)):
        items: list[Any] = [value]
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        return []
    ports = set()
    for item in items:
        port = _to_int(item)
        if port is not None and 0 < port <= 65535:
            ports.add(port)
    return sorted(ports)


def _port_from_local_address(value: Any) -> int | None:
    """从 ``0.0.0.0:443`` / ``[::]:443`` / ``127.0.0.1%lo:53`` 里取端口。"""
    text = ("" if value is None else str(value)).strip()
    if not text or ":" not in text:
        return None
    tail = text.rsplit(":", 1)[1]
    port = _to_int(tail)
    return port if port is not None and 0 < port <= 65535 else None


def _parse_created_at(value: Any) -> float | None:
    """解析进程创建时间：CIM ``/Date(ms)/``、ISO 字符串或 epoch 数字。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # /Date(...)/ 是毫秒；正常 epoch 秒远小于 1e11
        return number / 1000.0 if number > 1e11 else number
    text = str(value).strip()
    match = _CIM_DATE_RE.search(text)
    if match:
        try:
            return int(match.group(1)) / 1000.0
        except (TypeError, ValueError):
            return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError):
        return None


# --------------------------------------------------------------------- 探测器


class SystemProber:
    """跨平台「监听 TCP 的进程」探测器。

    ``supported`` 为 False 表示本机/本平台无法可靠枚举（命令缺失、权限不足、
    超时等）。调用方不应把 ``supported=False`` 当成「没有监听进程」。

    三个入口：

    - :meth:`listening`：轻量快照，只返回 pid → ports（audit 的默认路径）；
      ``include_cim=True`` 时在 Windows 额外用 CIM 补齐 netstat 漏报；
    - :meth:`describe`：按需批量取指定 pid 的 command/exe_path/created_at；
    - :meth:`snapshot`：兼容旧调用，``with_metadata=False`` 等价于 ``listening()``。

    快照返回结构::

        {pid: {"ports": [int], "command": str, "exe_path": str,
               "created_at": float | None, "cwd": str (可选)}}
    """

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SEC) -> None:
        self.timeout = max(0.5, float(timeout or DEFAULT_TIMEOUT_SEC))
        self.supported = True
        #: CIM 兜底（Get-NetTCPConnection）最近一次结果：
        #: ``None`` = 未尝试；``True`` = 成功；``False`` = 失败（已降级）。
        #: CIM 失败**不**修改 :attr:`supported`：netstat 正常时回收能力必须保留，
        #: 由 ``ProcessAudit.finish`` 读取本标记并写入 warning。
        self.cim_supported: bool | None = None
        self.cim_error = ""

    # ------------------------------------------------------------------ 公共
    def listening(self, *, include_cim: bool = False) -> dict[int, dict[str, Any]]:
        """轻量快照：只列 pid/ports，不查进程元数据。

        ``include_cim=True``（仅 Windows 生效）会额外执行一次
        ``Get-NetTCPConnection -State Listen``，把 CIM 看到的监听端口与 netstat
        结果按 pid 取并集，修复 netstat 把进程内监听 socket 报成
        FIN_WAIT/CLOSED 导致的漏报。CIM 失败/超时/输出无法解析时**不**降级
        ``supported``，只记录 ``cim_supported=False`` + ``cim_error`` 供
        ``ProcessAudit.finish`` 告警；netstat 结果照常返回、照常参与回收。

        调用时机：``ProcessAudit.begin`` 保持默认 ``False``（要快）；
        ``ProcessAudit.finish`` 传 ``True``，而 finish 由 runner 异步执行，
        因此这里的额外耗时不会阻塞任务返回。
        """
        try:
            if IS_WIN:
                ports_by_pid = self._listening_windows()
                if include_cim:
                    try:
                        cim_ports, cim_error = self._listening_windows_cim()
                    except Exception as exc:  # noqa: BLE001 - CIM 绝不能拖垮 netstat 结果
                        cim_ports, cim_error = None, f"{type(exc).__name__}: {exc}"
                    if cim_ports is None:
                        # 独立降级标记：supported 保持 True，netstat 结果继续用于回收。
                        self.cim_supported = False
                        self.cim_error = cim_error or "Get-NetTCPConnection 查询失败"
                    else:
                        self.cim_supported = True
                        self.cim_error = ""
                        for pid, ports in cim_ports.items():
                            ports_by_pid.setdefault(pid, set()).update(ports)
            else:
                # POSIX 的 ss/lsof 已覆盖监听态；include_cim 是无害的兼容参数。
                ports_by_pid = self._listening_posix()
            return {
                pid: {
                    "ports": sorted(ports),
                    "command": "",
                    "exe_path": "",
                    "created_at": None,
                }
                for pid, ports in sorted(ports_by_pid.items())
            }
        except Exception:  # noqa: BLE001 - 审计失败绝不能向上抛
            self.supported = False
            return {}

    def snapshot(
        self,
        *,
        with_metadata: bool = True,
        include_cim: bool = False,
    ) -> dict[int, dict[str, Any]]:
        """兼容旧调用：默认带元数据；``with_metadata=False`` 等价于 :meth:`listening`。

        ``include_cim`` 原样传给轻量快照（仅 Windows 生效，POSIX 忽略），默认
        ``False`` 保持原有行为不变。
        """
        base = self.listening(include_cim=include_cim)
        if not with_metadata or not base:
            return base
        metadata = self.describe(list(base))
        for pid, info in base.items():
            meta = metadata.get(pid) or {}
            for key in ("command", "exe_path", "cwd"):
                if meta.get(key):
                    info[key] = meta[key]
            if info.get("created_at") is None and meta.get("created_at") is not None:
                info["created_at"] = meta["created_at"]
        return base

    def describe(self, pids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """批量查询指定 pid 的 command/exe_path/created_at（失败返回空，不影响 supported）。"""
        targets: list[int] = []
        for value in pids:
            pid = _to_int(value)
            if pid is not None and pid > 0:
                targets.append(pid)
        if not targets:
            return {}
        targets = sorted(set(targets))
        try:
            if IS_WIN:
                return self._windows_metadata(targets)
            return self._posix_metadata(targets)
        except Exception:  # noqa: BLE001 - 元数据失败只降级为空字符串
            return {}

    # ------------------------------------------------------------------ 执行
    def _run(self, argv: list[str], *, timeout: float | None = None) -> tuple[int | None, str]:
        """执行外部命令，返回 ``(returncode, stdout)``；异常时 returncode 为 None。"""
        kwargs: dict[str, Any] = {}
        if IS_WIN:
            # 桌面壳里不要弹黑框；capture_output 已重定向，不会影响解析。
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                timeout=timeout if timeout is not None else self.timeout,
                check=False,
                **kwargs,
            )
        except (OSError, subprocess.SubprocessError):
            return None, ""
        return completed.returncode, decode_output(completed.stdout or b"")

    # ------------------------------------------------------------------ Windows
    def _listening_windows(self) -> dict[int, set[int]]:
        """``netstat -ano -p tcp`` → pid/ports（轻量路径，不启动 PowerShell）。"""
        rc, text = self._run(["netstat", "-ano", "-p", "tcp"])
        if rc != 0:
            self.supported = False
            return {}

        ports_by_pid: dict[int, set[int]] = {}
        for line in text.splitlines():
            fields = line.split()
            # 数据行固定为：Proto Local Foreign State PID
            if len(fields) < 5 or fields[0].upper() != "TCP":
                continue
            if fields[3].upper() != "LISTENING":
                continue
            pid = _to_int(fields[4])
            port = _port_from_local_address(fields[1])
            if not pid or pid <= 0 or port is None:
                continue
            ports_by_pid.setdefault(pid, set()).add(port)
        return ports_by_pid

    def _listening_windows_cim(self) -> tuple[dict[int, set[int]] | None, str]:
        """``Get-NetTCPConnection -State Listen`` → ``(pid/ports, 错误原因)``。

        第一个元素为 ``None`` 表示 CIM 查询失败（PowerShell 缺失、超时、
        权限、输出无法解析等）；调用方只记录 ``cim_supported=False`` 降级标记，
        保留 netstat 结果并继续回收。返回空 dict 表示查询成功但没有 LISTEN
        连接；成功时错误原因为空字符串。
        """
        script = (
            "Get-NetTCPConnection -State Listen | "
            "Select-Object LocalPort,OwningProcess | ConvertTo-Json -Compress"
        )
        rc, text = self._run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=self.timeout,
        )
        if rc != 0:
            return None, f"powershell 退出码={rc!r}"
        if not text.strip():
            # rc=0 且输出为空 = 当前确实没有 LISTEN 连接（PowerShell 对空管线
            # 不输出 JSON），这是成功结果，不是降级。
            return {}, ""
        try:
            payload = json.loads(text.lstrip(chr(0xFEFF)).strip())
        except (json.JSONDecodeError, ValueError) as exc:
            return None, f"CIM JSON 解析失败：{exc}"
        if isinstance(payload, dict):
            items: list[Any] = [payload]
        elif isinstance(payload, list):
            items = payload
        else:
            return None, f"CIM JSON 顶层类型异常：{type(payload).__name__}"

        ports_by_pid: dict[int, set[int]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = _to_int(item.get("OwningProcess"))
            port = _to_int(item.get("LocalPort"))
            if pid is None or pid <= 0:
                continue
            if port is None or not 0 < port <= 65535:
                continue
            ports_by_pid.setdefault(pid, set()).add(port)
        return ports_by_pid, ""

    def _windows_metadata(self, pids: list[int]) -> dict[int, dict[str, Any]]:
        """批量取 CommandLine / ExecutablePath / CreationDate（失败返回空）。"""
        result: dict[int, dict[str, Any]] = {}
        for index in range(0, len(pids), POWERSHELL_BATCH):
            result.update(self._query_process_batch(pids[index : index + POWERSHELL_BATCH]))
        return result

    def _query_process_batch(self, pids: list[int]) -> dict[int, dict[str, Any]]:
        if not pids:
            return {}
        filter_expr = " or ".join(f"ProcessId={int(pid)}" for pid in pids)
        script = (
            "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
            "$ErrorActionPreference = 'SilentlyContinue'; "
            f'Get-CimInstance Win32_Process -Filter "{filter_expr}" | '
            "Select-Object ProcessId,CommandLine,ExecutablePath,CreationDate | "
            "ConvertTo-Json -Compress"
        )
        rc, text = self._run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            timeout=self.timeout,
        )
        if rc != 0 or not text.strip():
            return {}
        try:
            payload = json.loads(text.lstrip("\ufeff").strip())
        except (json.JSONDecodeError, ValueError):
            return {}
        items = payload if isinstance(payload, list) else [payload]
        result: dict[int, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = _to_int(item.get("ProcessId"))
            if not pid or pid <= 0:
                continue
            result[pid] = {
                "command": str(item.get("CommandLine") or ""),
                "exe_path": str(item.get("ExecutablePath") or ""),
                "created_at": _parse_created_at(item.get("CreationDate")),
            }
        return result

    # ------------------------------------------------------------------ POSIX
    def _listening_posix(self) -> dict[int, set[int]]:
        """``ss -ltnp`` 优先，失败降级 ``lsof``（轻量路径，不读 /proc）。"""
        ports_by_pid: dict[int, set[int]] = {}
        if self._snapshot_ss(ports_by_pid):
            return ports_by_pid

        ports_by_pid = {}
        if self._snapshot_lsof(ports_by_pid):
            return ports_by_pid

        self.supported = False
        return {}

    def _snapshot_ss(self, ports_by_pid: dict[int, set[int]]) -> bool:
        """``ss -ltnp`` 解析；命令不可用/失败返回 False 以触发 lsof 降级。"""
        rc, text = self._run(["ss", "-ltnp"])
        if rc != 0:
            return False
        for line in text.splitlines():
            fields = line.split()
            if len(fields) < 5:
                continue
            if not fields[0].upper().startswith("LISTEN"):
                continue
            port = _port_from_local_address(fields[3])
            match = _SS_PID_RE.search(line)
            pid = _to_int(match.group(1)) if match else None
            if pid is None:
                # 老版本 ss 的进程列位置可能不同：兜底扫一遍所有 token。
                for token in fields[4:]:
                    match = _SS_PID_RE.search(token)
                    if match:
                        pid = _to_int(match.group(1))
                        if pid:
                            break
            if pid and port:
                ports_by_pid.setdefault(pid, set()).add(port)
        return True

    def _snapshot_lsof(self, ports_by_pid: dict[int, set[int]]) -> bool:
        """``lsof -F pcn`` 解析（``lsof`` 无匹配时返回码为 1，属正常空结果）。"""
        rc, text = self._run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"])
        if rc not in (0, 1):
            return False
        current_pid: int | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            tag, payload = line[0], line[1:]
            if tag == "p":
                current_pid = _to_int(payload)
            elif tag == "n" and current_pid:
                port = _port_from_local_address(payload)
                if port:
                    ports_by_pid.setdefault(current_pid, set()).add(port)
        return True

    def _posix_metadata(self, pids: list[int]) -> dict[int, dict[str, Any]]:
        """按需读 ``/proc``（只对新增 pid 调用，不在每次快照里全量扫）。"""
        metadata: dict[int, dict[str, Any]] = {}
        for pid in pids:
            command, exe_path, cwd = self._proc_metadata(pid)
            entry: dict[str, Any] = {
                "command": command,
                "exe_path": exe_path,
                "created_at": None,
            }
            if cwd:
                entry["cwd"] = cwd
            metadata[pid] = entry
        return metadata

    @staticmethod
    def _proc_metadata(pid: int) -> tuple[str, str, str]:
        """读 ``/proc`` 拿 command / exe / cwd；失败一律降级为空字符串。"""
        command = ""
        exe_path = ""
        cwd = ""
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            command = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            pass
        try:
            exe_path = os.readlink(f"/proc/{pid}/exe")
            if exe_path.endswith(" (deleted)"):
                exe_path = exe_path[: -len(" (deleted)")]
        except OSError:
            pass
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            pass
        return command, exe_path, cwd


# --------------------------------------------------------------------- 终止


def _terminate_pid(pid: int) -> bool:
    """整树终止一个进程（不抛异常）。

    - Windows：``taskkill /F /T /PID``（覆盖 ``cmd.exe → node.exe`` 这类进程树）；
    - POSIX：SIGTERM → 宽限 0.5s → SIGKILL；进程组安全时优先对进程组发信号，
      覆盖「监听进程是某个 Agent 子进程」的常见形态。

    返回 True 表示已执行终止且进程不在（或原本就不存在）。
    """
    target = _to_int(pid)
    if target is None or target <= 4:
        return False
    if target in {os.getpid(), os.getppid()}:
        return False

    if IS_WIN:
        kwargs: dict[str, Any] = {
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)
        }
        try:
            completed = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(target)],
                capture_output=True,
                timeout=15,
                check=False,
                **kwargs,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if completed.returncode == 0:
            return True
        return not process_alive(target)

    try:
        pgid: int | None = os.getpgid(target)
    except ProcessLookupError:
        return True
    except OSError:
        pgid = None

    try:
        own_pgid: int | None = os.getpgid(0)
    except OSError:
        own_pgid = None
    try:
        parent_pgid: int | None = os.getpgid(os.getppid())
    except OSError:
        parent_pgid = None

    # 绝不把自己的进程组/父进程所在组当成回收目标。
    use_group = pgid is not None and pgid not in (own_pgid, parent_pgid)

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if use_group:
                os.killpg(pgid, sig)  # type: ignore[arg-type]
            else:
                os.kill(target, sig)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            if not process_alive(target):
                return True
            time.sleep(0.05)
    return not process_alive(target)


def _kill_block_reason(pid: int, protected: set[int], before: Mapping[int, Any]) -> str:
    """返回拒绝终止的原因（空字符串 = 允许终止）。"""
    if pid <= 4:
        return "pid<=4"
    if pid in protected:
        return "本进程或父进程"
    if pid in before:
        return "调用前已存在"
    return ""


# --------------------------------------------------------------------- 审计


class ProcessAudit:
    """一次 Agent 调用前后的监听进程审计（用法见模块文档）。"""

    def __init__(
        self,
        before: Mapping[int, Mapping[str, Any]],
        prober: Any,
        *,
        killer: Callable[[int], Any] | None = None,
        supported: bool = True,
        warnings: list[str] | None = None,
        before_ok: bool = True,
    ) -> None:
        self._before: dict[int, dict[str, Any]] = {}
        for key, value in (before or {}).items():
            pid = _to_int(key)
            if pid is not None and pid > 0:
                self._before[pid] = dict(value or {})
        self._prober = prober
        # 本次审计开始时间：用于识别 PID 复用（新快照里的 pid 创建时间早于开始）。
        self._started_at = time.time()
        # None 表示使用模块级 _terminate_pid（调用时再解析，方便 monkeypatch）。
        self._killer = killer
        self._supported = bool(supported)
        self._warnings = list(warnings or [])
        self._before_ok = bool(before_ok)

    # ------------------------------------------------------------------ 生命周期
    @classmethod
    def begin(
        cls,
        prober: Any = None,
        *,
        killer: Callable[[int], Any] | None = None,
    ) -> "ProcessAudit":
        """记录 Agent 调用前的监听进程快照（永不抛异常）。

        这里**不启用** CIM 兜底：begin 在 Agent 启动前同步执行，必须保持轻量
        （Windows 只跑 netstat）。CIM 只在 ``finish``（异步收尾）使用。
        """
        active = prober if prober is not None else SystemProber()
        warnings: list[str] = []
        before: dict[int, dict[str, Any]] = {}
        ok = True
        prober_supported = True
        try:
            before = cls._snapshot(active, with_metadata=False)
            prober_supported = bool(getattr(active, "supported", True))
            if not prober_supported:
                warnings.append("监听进程枚举不可用（supported=False），本次审计只报告不回收")
        except Exception as exc:  # noqa: BLE001 - begin 同样不能拖垮任务
            ok = False
            prober_supported = False
            warnings.append(f"进程审计启动快照失败（已忽略）：{type(exc).__name__}: {exc}")
        return cls(
            before,
            active,
            killer=killer,
            supported=ok and prober_supported,
            warnings=warnings,
            before_ok=ok,
        )

    def finish(
        self,
        workdir: str | os.PathLike[str],
        *,
        reap: bool = True,
        include_cim: bool = True,
    ) -> dict[str, Any]:
        """对比前后快照并（可选）回收 owned 的新增监听进程；**永不抛异常**。"""
        warnings = list(self._warnings)
        started: list[dict[str, Any]] = []
        reaped: list[dict[str, Any]] = []
        before_count = len(self._before)
        after_count = 0
        supported = self._supported

        try:
            # finish 才启用 CIM 兜底（Windows 上补 netstat 漏报）；runner 已把
            # finish 放到异步线程，额外耗时不会阻塞任务返回。begin 保持轻量。
            after: dict[int, dict[str, Any]] = {}
            after_ok = False
            try:
                after = self._snapshot(self._prober, include_cim=include_cim)
                after_ok = True
            except Exception as exc:  # noqa: BLE001 - CIM 兜底失败必须降级而非失败
                warnings.append(
                    "监听进程 CIM 兜底查询失败（已降级为轻量快照）："
                    f"{type(exc).__name__}: {exc}"
                )

            # 注入的 prober 可能用「空快照」表达 CIM 失败；此时再取一次纯
            # 轻量快照，保住 netstat 结果。对 SystemProber 而言，只有确实没有
            # 任何监听时才会走到这里（正常 Windows 至少存在系统监听）。
            if not after_ok or not after:
                try:
                    lightweight = self._snapshot(self._prober)
                except Exception as exc:  # noqa: BLE001 - 快照异常降级
                    if not after_ok:
                        warnings.append(
                            f"进程审计快照失败（已忽略）：{type(exc).__name__}: {exc}"
                        )
                        return {
                            "supported": False,
                            "started": started,
                            "reaped": reaped,
                            "warnings": warnings,
                            "before_count": before_count,
                            "after_count": after_count,
                        }
                    warnings.append(
                        "CIM 空快照后的轻量快照失败（已忽略）："
                        f"{type(exc).__name__}: {exc}"
                    )
                else:
                    if after_ok and not after and lightweight:
                        warnings.append("CIM 兜底快照为空，已退回轻量快照")
                    after = lightweight
                    after_ok = True

            if not after_ok:
                # 两条路径都失败（异常已记录 warning）：保持原有不可用语义。
                return {
                    "supported": False,
                    "started": started,
                    "reaped": reaped,
                    "warnings": warnings,
                    "before_count": before_count,
                    "after_count": after_count,
                }

            supported = supported and bool(getattr(self._prober, "supported", True))
            after_count = len(after)

            # CIM 只是 finish 的补充枚举：失败时 netstat 仍可靠，supported 保持
            # True，归属判定与回收照常进行；这里只把降级原因暴露到 warnings。
            if getattr(self._prober, "cim_supported", None) is False:
                detail = str(getattr(self._prober, "cim_error", "") or "").strip()
                warnings.append(
                    "CIM 兜底枚举降级（已保留 netstat 结果，继续判定与回收）："
                    + (detail or "Get-NetTCPConnection 查询失败")
                )

            if not self._before_ok:
                warnings.append("启动前快照不可用，无法可靠判定新增监听进程，本次不做任何回收")
                return {
                    "supported": False,
                    "started": started,
                    "reaped": reaped,
                    "warnings": warnings,
                    "before_count": before_count,
                    "after_count": after_count,
                }

            new_pids = [pid for pid in sorted(after) if pid not in self._before]
            if not new_pids:
                # 没有新增监听 → 不起 PowerShell、不读 /proc、不做归属判断。
                return {
                    "supported": supported,
                    "started": started,
                    "reaped": reaped,
                    "warnings": warnings,
                    "before_count": before_count,
                    "after_count": after_count,
                }

            # 轻量快照只有 pid/ports；只对「没有 command/exe_path 的新增 pid」
            # 做一次批量 describe，避免每个任务都全机查进程元数据（CIM）。
            missing = [
                pid
                for pid in new_pids
                if not (after[pid].get("command") or after[pid].get("exe_path"))
            ]
            if missing:
                try:
                    metadata = self._describe(self._prober, missing)
                except Exception as exc:  # noqa: BLE001 - 元数据失败只降级为空
                    metadata = {}
                    warnings.append(
                        f"进程元数据查询失败（已忽略）：{type(exc).__name__}: {exc}"
                    )
                for pid in missing:
                    meta = metadata.get(pid) or {}
                    info = after[pid]
                    for key in ("command", "exe_path", "cwd"):
                        if not info.get(key) and meta.get(key):
                            info[key] = meta[key]
                    if info.get("created_at") is None and meta.get("created_at") is not None:
                        info["created_at"] = meta["created_at"]

            workdir_norm = _normalize_workdir(workdir)
            protected = {os.getpid(), os.getppid()}
            killer = self._killer if self._killer is not None else _terminate_pid

            for pid in new_pids:
                info = after[pid]
                owned = _is_owned(info, workdir_norm)
                created_at = info.get("created_at")
                if owned and created_at is not None and float(created_at) < self._started_at - 2.0:
                    # PID 在两次快照之间被系统复用：命令行可能恰好又指向 workdir，
                    # 但进程创建时间早于本次调用，不能当作任务残留杀掉。
                    owned = False
                    warnings.append(
                        "监听进程创建时间早于本次调用，疑似 PID 复用，未回收："
                        f"pid={pid} command={_clip(info.get('command'))}"
                    )
                entry = {
                    "pid": pid,
                    "ports": list(info.get("ports") or []),
                    "command": _clip(info.get("command")),
                    "owned": owned,
                }
                started.append(entry)

                if not owned:
                    warnings.append(
                        "新增监听进程无法确认归属，未回收："
                        f"pid={pid} ports={entry['ports']} command={entry['command']}"
                    )
                    continue
                if not reap or not supported:
                    # reap=False：只报告；supported=False：枚举不可靠，保守跳过。
                    continue

                reason = _kill_block_reason(pid, protected, self._before)
                if reason:
                    warnings.append(f"拒绝终止受保护进程：pid={pid}（{reason}）")
                    continue

                try:
                    outcome = killer(pid)
                except Exception as exc:  # noqa: BLE001 - 单个进程回收失败不影响其余
                    warnings.append(f"终止进程失败：pid={pid} {type(exc).__name__}: {exc}")
                    continue
                if outcome is False:
                    warnings.append(f"终止进程未成功：pid={pid}")
                    continue
                reaped.append(
                    {
                        "pid": pid,
                        "ports": entry["ports"],
                        "command": entry["command"],
                    }
                )

            if not supported and started:
                warnings.append("监听进程枚举不可靠（supported=False），本次未执行回收")
        except Exception as exc:  # noqa: BLE001 - finish 永不向调用方抛异常
            warnings.append(f"进程审计失败（已忽略）：{type(exc).__name__}: {exc}")
            return {
                "supported": False,
                "started": started,
                "reaped": reaped,
                "warnings": warnings,
                "before_count": before_count,
                "after_count": after_count,
            }

        return {
            "supported": supported,
            "started": started,
            "reaped": reaped,
            "warnings": warnings,
            "before_count": before_count,
            "after_count": after_count,
        }

    # ------------------------------------------------------------------ 内部
    @classmethod
    def _snapshot(
        cls,
        prober: Any,
        *,
        with_metadata: bool = False,
        include_cim: bool = False,
    ) -> dict[int, dict[str, Any]]:
        """调用 prober 的轻量快照并归一化（类型非法时抛异常，由调用方降级）。

        ``include_cim`` 只对声明支持该参数的 ``listening()`` 生效；旧 prober
        会自动回退到原调用，行为与之前一致。
        """
        raw = cls._call_snapshot(
            prober,
            with_metadata=with_metadata,
            include_cim=include_cim,
        )
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise TypeError(f"snapshot 返回类型非法：{type(raw).__name__}")
        normalized: dict[int, dict[str, Any]] = {}
        for key, value in raw.items():
            pid = _to_int(key)
            if pid is None or pid <= 0:
                continue
            item = value if isinstance(value, Mapping) else {}
            cwd = item.get("cwd")
            normalized[pid] = {
                "ports": _normalize_ports(item.get("ports")),
                "command": str(item.get("command") or ""),
                "exe_path": str(item.get("exe_path") or ""),
                "created_at": _parse_created_at(item.get("created_at")),
                "cwd": str(cwd or "") if cwd else "",
            }
        return normalized

    @staticmethod
    def _call_snapshot(
        prober: Any,
        *,
        with_metadata: bool,
        include_cim: bool = False,
    ) -> Any:
        """兼容三类 prober：``listening()``、``snapshot(with_metadata=)``、旧 ``snapshot()``。

        ``include_cim`` 通过能力探测传递：只有 ``listening()`` 的签名接受该
        关键字时才传；旧 fake/注入 prober（或只实现 ``snapshot()`` 的 prober）
        自动回退到原调用，不会因 ``TypeError`` 影响审计。
        """
        if not with_metadata:
            listening = getattr(prober, "listening", None)
            if callable(listening):
                if _accepts_kwarg(listening, "include_cim"):
                    return listening(include_cim=bool(include_cim))
                # 旧 prober 不认识 include_cim：保持原有调用形态。
                return listening()
        snapshot = getattr(prober, "snapshot", None)
        if not callable(snapshot):
            raise TypeError("prober 缺少 listening()/snapshot()")
        if _accepts_kwarg(snapshot, "with_metadata"):
            return snapshot(with_metadata=with_metadata)
        return snapshot()

    @classmethod
    def _describe(cls, prober: Any, pids: list[int]) -> dict[int, dict[str, Any]]:
        """只对新增 pid 批量取元数据；prober 不支持 describe 时返回空。"""
        if not pids:
            return {}
        describe = getattr(prober, "describe", None)
        if not callable(describe):
            return {}
        raw = describe(sorted(pids))
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise TypeError(f"describe 返回类型非法：{type(raw).__name__}")
        normalized: dict[int, dict[str, Any]] = {}
        for key, value in raw.items():
            pid = _to_int(key)
            if pid is None or pid <= 0:
                continue
            item = value if isinstance(value, Mapping) else {}
            cwd = item.get("cwd")
            normalized[pid] = {
                "command": str(item.get("command") or ""),
                "exe_path": str(item.get("exe_path") or ""),
                "created_at": _parse_created_at(item.get("created_at")),
                "cwd": str(cwd or "") if cwd else "",
            }
        return normalized


__all__ = [
    "MAX_COMMAND_CHARS",
    "ProcessAudit",
    "SystemProber",
]
