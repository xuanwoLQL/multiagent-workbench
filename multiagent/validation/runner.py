"""Bounded, cancellable execution of project validation checks.

Guarantees:

* ``shell=False`` for every child process; ``.cmd``/``.bat`` shims get an
  explicit ``cmd.exe`` wrapper with shell-metacharacter validation.
* output is capped at :data:`MAX_OUTPUT_BYTES` while the pipe is still drained;
* a hard timeout is enforced with process-tree cleanup;
* cancellation is cooperative through ``threading.Event``;
* the environment is an allowlist, not a copy of the caller's environment.
"""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from ..agents.proc import spawn, terminate_tree
from ..agents.resolve import wrap_executable

MAX_OUTPUT_BYTES = 200 * 1024
MAX_TIMEOUT_SEC = 3600
POLL_SECONDS = 0.1
_READ_CHUNK = 65536

_KEEP_ENV = {
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "JAVA_HOME",
    "GOROOT",
    "GOPATH",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "RUSTUP_TOOLCHAIN",
    "DOTNET_ROOT",
    "NUGET_PACKAGES",
    "MAVEN_HOME",
    "M2_HOME",
    "GRADLE_USER_HOME",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "NVM_HOME",
    "NVM_SYMLINK",
    "PNPM_HOME",
    "BUN_INSTALL",
}

_CMD_UNSAFE = frozenset('&|<>^%!"\r\n\x00')


class _Capture(threading.Thread):
    """Drain a pipe and keep at most ``limit`` bytes in memory."""

    def __init__(self, stream, limit: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.total = 0

    def run(self) -> None:  # pragma: no cover - thread body
        try:
            while True:
                chunk = self.stream.read(_READ_CHUNK)
                if not chunk:
                    break
                self.total += len(chunk)
                room = self.limit - len(self.data)
                if room > 0:
                    self.data.extend(chunk[:room])
        except (OSError, ValueError):
            pass

    @property
    def truncated(self) -> bool:
        return self.total > self.limit


def _decode(data: bytes) -> str:
    candidates = ["utf-8"]
    try:
        candidates.append(locale.getpreferredencoding(False))
    except Exception:  # noqa: BLE001 - locale probing must never break validation
        pass
    candidates.extend(["gbk", "latin-1"])
    for encoding in candidates:
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _display(argv: Iterable[str]) -> str:
    values = [str(item) for item in argv]
    if os.name == "nt":
        return subprocess.list2cmdline(values)
    import shlex

    return shlex.join(values)


def _build_env(root: Path, env_extra: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _KEEP_ENV or upper.startswith("LC_"):
            env[key] = value
    # Windows exposes PATH as "Path"; normalise so lookups are predictable.
    path_key = next((key for key in env if key.upper() == "PATH"), None)
    if path_key is None:
        path_key = "PATH"
        env[path_key] = os.environ.get("PATH") or os.defpath
    elif path_key != "PATH":
        env["PATH"] = env.pop(path_key)
    python_paths = [str(root)]
    src = root / "src"
    if src.is_dir():
        python_paths.insert(0, str(src))
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "PYTHONUNBUFFERED": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(python_paths),
            "PYTEST_ADDOPTS": "-p no:cacheprovider",
            "PIP_NO_INPUT": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "NPM_CONFIG_AUDIT": "false",
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "NPM_CONFIG_YES": "false",
            "GIT_TERMINAL_PROMPT": "0",
            "CI": "1",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
            "TERM": "dumb",
        }
    )
    if env_extra:
        for key, value in env_extra.items():
            if value:
                env[str(key)] = str(value)
    return env


def _resolve(command: str, root: Path, env: dict[str, str]) -> str | None:
    text = str(command or "").strip()
    if not text:
        return None
    if os.path.isabs(text) or "/" in text or "\\" in text:
        candidate = Path(text)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            return str(candidate) if candidate.is_file() else None
        except OSError:
            return None
    return shutil.which(text, path=env.get("PATH"))


def _unsafe_arguments(resolved: str, args: list[str]) -> bool:
    text = str(resolved).lower()
    if os.name == "nt" and text.endswith((".cmd", ".bat")):
        if any(char in _CMD_UNSAFE for char in str(resolved)):
            return True
        return any(any(char in _CMD_UNSAFE for char in arg) for arg in args)
    return any("\x00" in arg for arg in args)


def _skipped(
    check_id: str,
    label: str,
    command: str,
    reason: str,
    *,
    cancelled: bool = False,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label or check_id,
        "command": command,
        "ok": False,
        "exit_code": None,
        "duration_ms": 0,
        "output": reason,
        "skipped": True,
        "timeout": False,
        "cancelled": cancelled,
        "truncated": False,
    }


def _normalise_checks(
    checks: Iterable[str] | str | None,
    profile: dict[str, Any],
    commands: dict[str, Any],
) -> list[str]:
    if checks is None:
        raw: Iterable[Any] = profile.get("recommended") or list(commands)
    elif isinstance(checks, str):
        raw = [checks]
    else:
        raw = checks
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name or name in seen or len(name) > 64:
            continue
        seen.add(name)
        result.append(name)
        if len(result) >= 32:
            break
    return result


def _run_one(
    root: Path,
    check_id: str,
    spec: dict[str, Any],
    timeout_sec: float,
    cancel: threading.Event | None,
    env_extra: dict[str, str] | None,
) -> dict[str, Any]:
    label = str(spec.get("label") or check_id)
    raw_argv = spec.get("argv")
    if not isinstance(raw_argv, (list, tuple)) or not raw_argv:
        return _skipped(check_id, label, "", "检查命令为空")
    argv = [str(item) for item in raw_argv]
    command_text = _display(argv)

    env = _build_env(root, env_extra)
    resolved = _resolve(argv[0], root, env)
    if not resolved:
        return _skipped(check_id, label, command_text, f"找不到可执行文件：{argv[0]}")
    args = argv[1:]
    if _unsafe_arguments(resolved, args):
        return _skipped(check_id, label, command_text, "参数包含 shell 元字符，已拒绝执行")

    command, wrapped_args = wrap_executable(resolved, args)
    started = time.monotonic()
    try:
        cache_dir: str | None = tempfile.mkdtemp(prefix="multiagent-pycache-")
    except OSError:
        cache_dir = None
    if cache_dir:
        env["PYTHONPYCACHEPREFIX"] = cache_dir
    try:
        try:
            proc = spawn(
                [command, *wrapped_args],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            return _skipped(check_id, label, command_text, f"启动失败：{exc}")

        capture = _Capture(proc.stdout, MAX_OUTPUT_BYTES)
        capture.start()
        timed_out = False
        cancelled = False
        deadline = started + max(0.1, float(timeout_sec))
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                cancelled = True
                terminate_tree(proc, wait_natural=0.0)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                terminate_tree(proc, wait_natural=0.0)
                break
            time.sleep(POLL_SECONDS)

        capture.join(timeout=2.0)
        if capture.is_alive():
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass
            capture.join(timeout=1.0)

        duration_ms = int((time.monotonic() - started) * 1000)
        exit_code = proc.poll()
        if exit_code is None:
            exit_code = -1
        output = _decode(bytes(capture.data))
        if timed_out:
            output = (output + f"\n[timeout after {timeout_sec:g}s]")[:MAX_OUTPUT_BYTES]
        elif cancelled:
            output = (output + "\n[cancelled]")[:MAX_OUTPUT_BYTES]
        ok = exit_code == 0 and not timed_out and not cancelled
        return {
            "id": check_id,
            "label": label,
            "command": command_text,
            "ok": ok,
            "exit_code": int(exit_code),
            "duration_ms": duration_ms,
            "output": output,
            "skipped": False,
            "timeout": timed_out,
            "cancelled": cancelled,
            "truncated": capture.truncated,
        }
    finally:
        if cache_dir:
            shutil.rmtree(cache_dir, ignore_errors=True)


def run_checks(
    root: str | Path,
    profile: dict[str, Any],
    *,
    checks: Iterable[str] | str | None = None,
    timeout_sec: float | None = None,
    cancel: threading.Event | None = None,
    env_extra: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Run requested checks under ``root`` and return one dict per check.

    Missing commands and unsafe arguments are returned as ``skipped`` rather
    than raised, so one unavailable tool cannot abort a whole validation run.
    """
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"验证目录不存在：{base}")
    commands = profile.get("commands")
    if not isinstance(commands, dict):
        commands = {}
    requested = _normalise_checks(checks, profile, commands)
    timeout = float(timeout_sec if timeout_sec is not None else profile.get("default_timeout_sec", 600))
    timeout = max(0.1, min(timeout, float(MAX_TIMEOUT_SEC)))

    results: list[dict[str, Any]] = []
    for check_id in requested:
        if cancel is not None and cancel.is_set():
            results.append(_skipped(check_id, check_id, "", "已取消", cancelled=True))
            continue
        spec = commands.get(check_id)
        if not isinstance(spec, dict):
            results.append(_skipped(check_id, check_id, "", f"未检测到检查：{check_id}"))
            continue
        results.append(_run_one(base, check_id, spec, timeout, cancel, env_extra))
    return results
