"""通用工具：ID、时间、哈希、JSON 读写、路径安全、文件类型、子进程。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------- 时间

def now_iso() -> str:
    """UTC ISO8601（毫秒精度），全库统一使用。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def human_duration(ms: int | float | None) -> str:
    if not ms:
        return "0s"
    seconds = float(ms) / 1000.0
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# --------------------------------------------------------------------------- ID

_ID_SAFE = re.compile(r"[^\w\-]+", re.UNICODE)


def slugify(text: str, fallback: str = "item", max_len: int = 40) -> str:
    """把任意文本转成可用作目录名的 slug（保留中文等 Unicode 字母）。"""
    slug = _ID_SAFE.sub("-", (text or "").strip()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug or fallback)[:max_len].strip("-") or fallback


def new_id(prefix: str) -> str:
    """短 ID：<prefix>-<8位随机>。可读、可排序（配合 created_at）。"""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def new_token(n: int = 16) -> str:
    return uuid.uuid4().hex[:n]


# --------------------------------------------------------------------------- 哈希

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", "replace"))


def sha256_file(path: str | os.PathLike[str], chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- JSON

def read_json(path: str | os.PathLike[str], default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, OSError):
        return default


def write_json(path: str | os.PathLike[str], data: Any) -> None:
    """原子写：先写临时文件再替换，避免半截文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


# --------------------------------------------------------------------------- 路径

def to_posix(path: str | os.PathLike[str]) -> str:
    return str(path).replace("\\", "/")


def rel_posix(path: str | os.PathLike[str], root: str | os.PathLike[str]) -> str:
    return to_posix(os.path.relpath(str(path), str(root)))


def safe_join(root: str | os.PathLike[str], rel: str) -> Path:
    """把相对路径安全地拼到 root 下，阻止 ``..`` 越权。"""
    root_path = Path(root).resolve()
    candidate = (root_path / rel).resolve()
    if candidate != root_path and root_path not in candidate.parents:
        raise ValueError(f"路径越界：{rel}")
    return candidate


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def matches_any(rel_path: str, patterns: Iterable[str]) -> bool:
    """glob 匹配（POSIX 风格相对路径）。

    支持 ``*``（不跨目录）、``**``（跨任意层级，可为零层）、``?``。
    ``src/**`` 同时匹配 ``src`` 本身与 ``src`` 下的任意内容。
    """
    rel = _strip_pattern_prefix(rel_path)
    for pattern in patterns:
        pat = _strip_pattern_prefix(pattern)
        if not pat:
            continue
        if fnmatch(rel, pat):
            return True
        regex = _glob_to_regex(pat)
        if regex.match(rel):
            return True
        # 目录前缀语义：pattern 目录下的所有内容都算命中
        base = pat
        if pat.endswith("/**"):
            base = pat[:-3].rstrip("/")
        elif pat.endswith("/**/*"):
            base = pat[:-5].rstrip("/")
        if base and base != "/" and rel.startswith(base.rstrip("/") + "/"):
            return True
    return False


def _strip_pattern_prefix(value: str | os.PathLike[str]) -> str:
    """只去掉开头的 ``./`` 与绝对路径前缀，**绝不剥掉点文件名**。

    ``.lstrip("./")`` 会把 ``.gitignore`` 变成 ``gitignore``、``.multiagent``
    变成 ``multiagent``——真实事故：任务 write_scope 写了 ``.gitignore``，产出的
    文件却被判越权并还原。这里只做前缀处理，保留 ``.`` 本身。
    """
    text = to_posix(value)
    while text.startswith("./"):
        text = text[2:]
    if text.startswith("/") and not text.startswith("//"):
        text = text[1:]
    return text


_GLOB_CACHE: dict[str, "re.Pattern[str]"] = {}


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    out: list[str] = ["^"]
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
                    out.append("(?:.*/)?")
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
        i += 1
    out.append("$")
    compiled = re.compile("".join(out))
    _GLOB_CACHE[pattern] = compiled
    return compiled


# --------------------------------------------------------------------------- 文件类型

_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".html",
    ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx", ".py", ".rb", ".go",
    ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".sh",
    ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql", ".tex", ".srt", ".vtt",
    ".svg", ".gitignore", ".dockerfile",
}


#: 明确属于「非文本」的后缀（需求 9 / 34 点名：文档、表格、图片）。
#:
#: 只靠内容嗅探不够：纯 ASCII 的 PDF、把中文压掉的 Office 部件、只有少量
#: 高位字节的图片，都可能既没有 NUL 又能按 UTF-8 解码，于是被误判成文本，
#: 进而走行级 diff / 自动合并路径——这正是需求 34 要禁止的。
#: 因此对已知的二进制格式**按后缀直接判定**，不看内容。
_BINARY_SUFFIXES = {
    # 文档 / 表格 / 演示 / PDF
    ".doc", ".docx", ".docm", ".dot", ".dotx", ".xls", ".xlsx", ".xlsm", ".xlsb",
    ".ppt", ".pptx", ".pptm", ".pdf", ".odt", ".ods", ".odp", ".pages", ".numbers", ".key",
    # 压缩包 / 归档 / 包
    ".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".jar", ".war", ".whl",
    # 图片 / 音频 / 视频
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".tif", ".tiff",
    ".psd", ".ai", ".heic", ".raw",
    ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
    ".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm",
    # 可执行 / 库 / 字节码 / 目标文件
    ".exe", ".dll", ".so", ".dylib", ".bin", ".dat", ".class", ".pyc", ".pyd", ".o", ".a", ".lib",
    # 数据库 / 索引
    ".db", ".sqlite", ".sqlite3", ".mdb", ".accdb",
    # 字体
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    # 镜像 / 安装包
    ".iso", ".img", ".vmdk", ".dmg", ".msi", ".cab",
}


def is_binary_path(path: str | os.PathLike[str], sniff: int = 8192) -> bool:
    """判断是否为二进制文件（需求 9 / 34）。

    判定顺序（任一命中即判定，前面的优先）：
      1. 后缀在已知二进制格式表里 → 二进制（不读内容，避免纯 ASCII 的 PDF 漏网）；
      2. 后缀在文本白名单里 → 文本；
      3. 前 ``sniff`` 字节含 NUL → 二进制；
      4. 前 ``sniff`` 字节无法按 UTF-8 解码 → 二进制。

    第 4 条是刻意保守的：本项目只对 UTF-8 文本做行级 diff 与自动合并，
    GBK/Latin-1 等非 UTF-8 文本同样无法安全逐行比对，按需求 34 应「识别不了
    就阻止自动合并并转人工」，因此一律归为二进制而不是冒险当文本处理。
    """
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in _BINARY_SUFFIXES:
        return True
    if suffix in _TEXT_SUFFIXES:
        return False
    try:
        with open(p, "rb") as fh:
            chunk = fh.read(sniff)
    except OSError:
        return False
    if not chunk:
        return False
    if b"\x00" in chunk:
        return True
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def read_text(path: str | os.PathLike[str], limit: int | None = None) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read(limit) if limit else fh.read()
    except OSError:
        return ""


def write_text(path: str | os.PathLike[str], text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def file_size(path: str | os.PathLike[str]) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --------------------------------------------------------------------------- 子进程

def run_command(
    command: Sequence[str] | str,
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = 60,
    shell: bool = False,
    stdin_text: str | None = None,
) -> tuple[int, str, str, int]:
    """执行命令，返回 (exit_code, stdout, stderr, duration_ms)。超时返回 -9。"""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd) if cwd else None,
            env=env,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=shell,
        )
        duration = int((time.monotonic() - started) * 1000)
        return proc.returncode, proc.stdout or "", proc.stderr or "", duration
    except subprocess.TimeoutExpired as exc:
        duration = int((time.monotonic() - started) * 1000)
        out = exc.stdout or ""
        err = (exc.stderr or "") + f"\n[timeout] 超过 {timeout}s 未返回"
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return -9, out, err, duration
    except FileNotFoundError as exc:
        duration = int((time.monotonic() - started) * 1000)
        return -2, "", f"[not-found] {exc}", duration
    except OSError as exc:
        duration = int((time.monotonic() - started) * 1000)
        return -3, "", f"[os-error] {exc}", duration


def which(name: str, extra_paths: Sequence[str] | None = None) -> str | None:
    """跨平台 which（Windows 会补 .cmd/.exe）。"""
    import shutil

    found = shutil.which(name)
    if found:
        return found
    if os.name == "nt" and not name.lower().endswith((".cmd", ".exe", ".bat", ".ps1")):
        for ext in (".cmd", ".exe", ".bat", ".ps1"):
            found = shutil.which(name + ext)
            if found:
                return found
    if extra_paths:
        for raw in extra_paths:
            candidate = Path(raw)
            if candidate.is_file():
                return str(candidate)
    return None


def truncate(text: str, limit: int, marker: str = "\n…[已截断]") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    if len(marker) >= limit:
        return text[:limit]
    return text[: limit - len(marker)] + marker
