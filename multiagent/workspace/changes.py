"""文件树快照与改动日志（需求 32-37）。

- :func:`snapshot_tree` 为工作区建立「改动前基线」；
- :func:`diff_trees` 得出新增/修改/删除；
- :class:`ChangeJournal` 落库改动、判定越权写与并发冲突、支持按任务回滚。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from fnmatch import fnmatch

from ..db import Database
from ..util import (
    _strip_pattern_prefix,
    is_binary_path,
    matches_any,
    now_iso,
    rel_posix,
    sha256_file,
    to_posix,
)

#: 永远不纳入管理/快照的目录与文件。
#:
#: 同时写根级与 ``**/`` 形式：``walk_files`` 传入的是 POSIX 相对路径，
#: 仅写 ``node_modules/**`` 会漏掉 ``tools/node_modules/...`` 这类嵌套目录。
DEFAULT_EXCLUDES = (
    # A linked worktree's ``.git`` is a *file* (gitdir pointer), not a directory.
    # If it is not excluded here it looks like a deliverable, gets flagged as an
    # out-of-scope/protected change, and the finalize step deletes it — which
    # silently detaches the worktree from its repository.
    ".git",
    "**/.git",
    ".git/**",
    "**/.git/**",
    ".multiagent/**",
    "**/.multiagent/**",
    "node_modules/**",
    "**/node_modules/**",
    ".venv/**",
    "**/.venv/**",
    "venv/**",
    "**/venv/**",
    "__pycache__",
    "__pycache__/**",
    "**/__pycache__",
    "**/__pycache__/**",
    "*.pyc",
    "**/*.pyc",
    "*.tsbuildinfo",
    "**/*.tsbuildinfo",
    ".pytest_cache/**",
    "**/.pytest_cache/**",
    ".mypy_cache/**",
    "**/.mypy_cache/**",
    ".ruff_cache/**",
    "**/.ruff_cache/**",
    "dist/**",
    "**/dist/**",
    "build/**",
    "**/build/**",
    "*.egg-info/**",
    "**/*.egg-info/**",
    ".edge-tmp*",
    ".edge-tmp*/**",
    "**/.edge-tmp*",
    "**/.edge-tmp*/**",
    "edge-profile",
    "edge-profile/**",
    "**/edge-profile",
    "**/edge-profile/**",
    "edge-capture*",
    "edge-capture*/**",
    "**/edge-capture*",
    "**/edge-capture*/**",
    "edge-cdp*",
    "edge-cdp*/**",
    "**/edge-cdp*",
    "**/edge-cdp*/**",
    "edge-shot*",
    "edge-shot*/**",
    "**/edge-shot*",
    "**/edge-shot*/**",
    "build/edge-*/**",
    "**/build/edge-*/**",
    "~$*",
    "**/~$*",
    ".cache",
    ".cache/**",
    "**/.cache",
    "**/.cache/**",
    ".tmp*",
    ".tmp*/**",
    "**/.tmp*",
    "**/.tmp*/**",
    "*.tmp",
    "**/*.tmp",
    "*.log",
    "**/*.log",
)

#: Windows 大小写不敏感：同一路径可能以任意大小写出现（需求 35）
_CASE_INSENSITIVE_FS = os.name == "nt"


class PathEscapeError(ValueError):
    """相对路径越出工作区边界（``..`` / 绝对路径 / UNC / 符号链接）。"""


class BlobMissingError(RuntimeError):
    """回滚所需的 blob 不在内容寻址仓库里——必须显式失败，绝不静默成功。"""


def is_excluded(rel_path: str, excludes: Iterable[str] = DEFAULT_EXCLUDES) -> bool:
    return matches_any(rel_path, excludes)


def read_ignore_file(root: str | Path, filename: str = ".multiagentignore") -> list[str]:
    """读取项目级忽略规则（每行一个 glob；``#`` 开头为注释）。"""
    path = Path(root) / filename
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rules: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!"):
            continue
        rules.append(line.replace("\\", "/"))
    return rules


def effective_excludes(
    root: str | Path | None = None,
    config=None,
    extra: Iterable[str] | None = None,
) -> tuple[str, ...]:
    """默认排除 + policy.workspace.excludes + 项目 ``.multiagentignore`` + 调用方追加。"""
    values: list[str] = list(DEFAULT_EXCLUDES)
    ignore_file = ".multiagentignore"
    if config is not None:
        try:
            configured = config.policy_get("workspace.excludes", []) or []
            if isinstance(configured, str):
                configured = [configured]
            values.extend(str(item) for item in configured)
            ignore_file = str(
                config.policy_get("workspace.ignore_file", ".multiagentignore")
                or ".multiagentignore"
            )
        except Exception:  # noqa: BLE001 - 配置异常不能阻断工作区扫描
            pass
    if root is not None:
        values.extend(read_ignore_file(root, ignore_file))
    if extra:
        values.extend(str(item) for item in extra)
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        normalized = str(item).replace("\\", "/").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


# --------------------------------------------------------------------------- 路径安全

def normalize_rel_path(rel_path: str | Path) -> str:
    """把任意「相对路径」规范化成安全的 POSIX 相对路径。

    拒绝（抛 :class:`PathEscapeError`）：``..`` 逃逸、绝对路径、Windows 盘符、
    UNC/扩展长度前缀、空路径。只做词法归一（不碰文件系统），保证 ``record`` /
    ``scope_check`` / ``revert`` 对同一个输入得到同一个结论。
    """
    if rel_path is None:
        raise PathEscapeError("空路径")
    text = str(rel_path).strip()
    if not text or text in (".", "/", "\\"):
        raise PathEscapeError(f"非法路径：{rel_path!r}")
    text = text.replace("\\", "/")
    # UNC（//server/share）、扩展长度前缀（//?/C:/...）、盘符（C:/...）
    if text.startswith("//") or (len(text) >= 2 and text[1] == ":"):
        raise PathEscapeError(f"绝对路径 / UNC 不允许：{rel_path!r}")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise PathEscapeError(f"路径越界：{rel_path!r}")
            parts.pop()
            continue
        if ":" in part:
            raise PathEscapeError(f"路径含非法字符 ':'：{rel_path!r}")
        parts.append(part)
    if not parts:
        raise PathEscapeError(f"非法路径：{rel_path!r}")
    return "/".join(parts)


def _within_bounds(root: Path, candidate: Path) -> bool:
    """lexical 判断 candidate 是否落在 root 内（不解析符号链接）。"""
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_workspace_path(workspace_root: str | Path, rel_path: str | Path) -> Path:
    """把相对路径安全地拼到工作区根下。

    - 先词法归一，拒绝 ``..`` / 绝对路径 / UNC / 盘符；
    - 再逐级解析符号链接，确保**真实路径**仍在工作区内（需求 35/37）。
    """
    rel = normalize_rel_path(rel_path)
    root = Path(workspace_root).absolute()
    candidate = Path(os.path.normpath(os.path.join(str(root), *rel.split("/"))))
    if not _within_bounds(root, candidate):
        raise PathEscapeError(f"路径越界：{rel_path!r}")
    # 逐级解析符号链接/junction：任意一级链接到工作区外都算越界
    current = root
    for part in rel.split("/"):
        current = current / part
        try:
            real = Path(os.path.realpath(current))
        except OSError:  # pragma: no cover - 平台相关
            continue
        if not _within_bounds(root, real) and not _within_bounds(
            Path(os.path.realpath(root)), real
        ):
            raise PathEscapeError(f"路径经符号链接越界：{rel_path!r}")
    return candidate


def _glob_base(pattern: str) -> str:
    base = _strip_pattern_prefix(pattern)
    if base.endswith("/**"):
        base = base[:-3].rstrip("/")
    elif base.endswith("/**/*"):
        base = base[:-5].rstrip("/")
    return base


def _matches_scope(rel: str, patterns: Iterable[str]) -> bool:
    """带**目录边界**的 glob 匹配。

    与 :func:`multiagent.util.matches_any` 的差别：这里 ``src`` 只覆盖 ``src`` 与
    ``src/...``，不会把 ``srcevil/x`` 也算命中（需求 35 的目录前缀混淆）。
    """
    rel_norm = _strip_pattern_prefix(rel)
    rel_cmp = rel_norm.lower() if _CASE_INSENSITIVE_FS else rel_norm
    for pattern in patterns:
        pat = _strip_pattern_prefix(pattern)
        if not pat:
            continue
        pat_cmp = pat.lower() if _CASE_INSENSITIVE_FS else pat
        if fnmatch(rel_cmp, pat_cmp):
            return True
        base = _glob_base(pat)
        if not base:
            continue
        base_cmp = base.lower() if _CASE_INSENSITIVE_FS else base
        if rel_cmp == base_cmp:
            return True
        if rel_cmp.startswith(base_cmp.rstrip("/") + "/"):
            return True
    return False


def _is_within_workspace(workspace_root: str | Path, rel: str) -> bool:
    """rel（已归一）是否落在工作区内：逐级解析符号链接/junction。"""
    root = Path(workspace_root).absolute()
    current = root
    for part in rel.split("/"):
        current = current / part
        try:
            if not current.exists():
                continue
            real = Path(os.path.realpath(current))
        except OSError:  # pragma: no cover - 平台相关
            continue
        root_real = Path(os.path.realpath(root))
        if not _within_bounds(root, real) and not _within_bounds(root_real, real):
            return False
    return True


def _safe_child(root: str | Path | None, rel: str) -> Path | None:
    """在 root 下安全取子路径；越界返回 None（绝不落到工作区外）。"""
    if root is None:
        return None
    try:
        return resolve_workspace_path(root, rel)
    except PathEscapeError:
        return None


def _same_effect(change: dict[str, Any], peers: list[dict[str, Any]]) -> bool:
    """本次改动与并发方是否「效果相同」（相同 action + 相同 after_hash）。"""
    mine_after = change.get("after_hash")
    mine_action = change.get("action")
    if not mine_after:
        return False
    return any(p.get("action") == mine_action and p.get("after_hash") == mine_after for p in peers)


def prune_empty_dirs(root: str | Path, *, keep: Iterable[str] | None = None) -> list[str]:
    """自底向上删除工作区里的空目录（回滚/回退后清理新建的目录骨架）。

    ``keep`` 给出改动前就存在的目录相对路径集合，这些目录即使为空也保留。
    """
    root_path = Path(root)
    if not root_path.exists():
        return []
    keep_set = {to_posix(k).rstrip("/") for k in (keep or ())}
    removed: list[str] = []
    dirs = [
        p
        for p in root_path.rglob("*")
        if p.is_dir()
        and not p.is_symlink()
        and not is_excluded(rel_posix(p, root_path), DEFAULT_EXCLUDES)
    ]
    for path in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        rel = rel_posix(path, root_path)
        if rel in keep_set:
            continue
        try:
            next(path.iterdir())
            continue
        except StopIteration:
            pass
        except OSError:
            continue
        try:
            path.rmdir()
            removed.append(to_posix(rel))
        except OSError:
            continue
    return removed


def walk_files(
    root: str | Path,
    excludes: Iterable[str] | None = None,
    *,
    config=None,
) -> list[str]:
    root_path = Path(root)
    rules = effective_excludes(root_path, config) if excludes is None else tuple(excludes)
    result: list[str] = []
    # os.walk(followlinks=False) 不跟随目录符号链接；文件符号链接也单独跳过。
    # 需求 32-37：链接目标不属于工作区，不能进入快照/差异/回收链路。
    for current, dirs, files in os.walk(root_path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(current) / d).is_symlink()]
        for name in files:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                continue
            rel = rel_posix(path, root_path)
            if is_excluded(rel, rules):
                continue
            result.append(rel)
    return sorted(result)


def snapshot_tree(
    root: str | Path,
    excludes: Iterable[str] | None = None,
    *,
    max_bytes: int = 64 * 1024 * 1024,
    config=None,
) -> dict[str, dict[str, Any]]:
    root_path = Path(root)
    tree: dict[str, dict[str, Any]] = {}
    for rel in walk_files(root_path, excludes, config=config):
        path = root_path / rel
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            continue
        try:
            digest = sha256_file(path)
        except OSError:
            continue
        tree[to_posix(rel)] = {
            "sha256": digest,
            "size": size,
            "binary": is_binary_path(path),
        }
    return tree


def diff_trees(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for rel, info in after.items():
        if rel not in before:
            changes.append(
                {
                    "action": "create",
                    "rel_path": rel,
                    "before_hash": None,
                    "after_hash": info["sha256"],
                    "before_size": None,
                    "after_size": info["size"],
                    "is_binary": info.get("binary", False),
                }
            )
        elif before[rel]["sha256"] != info["sha256"]:
            changes.append(
                {
                    "action": "modify",
                    "rel_path": rel,
                    "before_hash": before[rel]["sha256"],
                    "after_hash": info["sha256"],
                    "before_size": before[rel].get("size"),
                    "after_size": info["size"],
                    "is_binary": info.get("binary", False) or before[rel].get("binary", False),
                }
            )
    for rel, info in before.items():
        if rel not in after:
            changes.append(
                {
                    "action": "delete",
                    "rel_path": rel,
                    "before_hash": info["sha256"],
                    "after_hash": None,
                    "before_size": info.get("size"),
                    "after_size": None,
                    "is_binary": info.get("binary", False),
                }
            )
    changes.sort(key=lambda c: (c["rel_path"], c["action"]))
    return changes


@dataclass
class RecordedChange:
    row_id: int
    action: str
    rel_path: str
    in_scope: bool
    conflict: bool
    is_binary: bool
    #: 非文本（Office/图片/PDF）并发改动：识别不了就只能转人工（需求 34）
    manual_review: bool = False
    #: 越权判定原因（越界路径 / 符号链接 / 大小写绕过…），便于人工定位
    reason: str | None = None
    #: 路径本身逃出工作区（``..``/绝对路径/UNC/符号链接）——比「不在写范围」更严重
    escaped_path: bool = False
    others: list[dict[str, Any]] = field(default_factory=list)


class ChangeJournal:
    """把一次运行的改动写入 ``file_changes``，并给出越权 / 冲突判定。"""

    def __init__(self, db: Database, blobs):
        self.db = db
        self.blobs = blobs

    # ------------------------------------------------------------------ 判定
    @staticmethod
    def scope_check(
        rel_path: str,
        scope_patterns: list[str],
        read_only: bool,
        deny_patterns: list[str] | None = None,
        *,
        workspace_root: str | Path | None = None,
    ) -> bool:
        """写范围校验（需求 35）。任何「路径本身的越界」都先一票否决。

        - 相对路径词法归一：拒绝 ``..``、绝对路径、Windows 盘符、UNC；
        - Windows 大小写不敏感：``SRC/main.py`` 不能借大小写差异绕过 ``src/**``；
        - ``scope_patterns`` 采用**目录边界**语义：``src`` 只覆盖 ``src`` 与
          ``src/...``，绝不含 ``srcevil/x``；
        - 传了 ``workspace_root`` 时再校验符号链接（junction）不指向工作区外。
        """
        if read_only:
            return False
        raw = str(rel_path or "")
        try:
            rel = normalize_rel_path(raw)
        except PathEscapeError:
            return False
        if workspace_root is not None and not _is_within_workspace(workspace_root, rel):
            return False
        if deny_patterns and _matches_scope(rel, deny_patterns):
            return False
        if not scope_patterns:
            return True
        return _matches_scope(rel, scope_patterns)

    def conflicts(self, project_id: str, task_id: str, rel_paths: list[str]) -> list[dict[str, Any]]:
        """与「其他仍在进行中的任务」对同一文件的改动 = 冲突（需求 32/34）。"""
        if not rel_paths:
            return []
        placeholders = ",".join("?" for _ in rel_paths)
        rows = self.db.query(
            f"""
            SELECT fc.rel_path, fc.task_id, fc.agent_id, fc.action, fc.before_hash, fc.after_hash,
                   fc.is_binary, t.status AS task_status, t.title
            FROM file_changes fc
            JOIN tasks t ON t.id = fc.task_id
            WHERE fc.project_id = ? AND fc.task_id IS NOT NULL AND fc.task_id <> ?
              AND fc.mode = 'committed' AND fc.conflict = 0 AND fc.in_scope = 1
              AND fc.rel_path IN ({placeholders})
              AND t.status IN ('running', 'pending', 'blocked', 'waiting')
            """,
            [project_id, task_id, *rel_paths],
        )
        return [dict(r) for r in rows]

    def mark_reverted(self, task_id: str, rel_paths: Iterable[str], *, run_id: str | None = None) -> int:
        """把已经还原到工作区的改动标记为 reverted。

        越权/冲突改动在 runner 里会先归档再还原；如果 DB 行仍停留在 ``committed``，
        后续任务的冲突检测会把「已经还原、工作区根本没变」的文件误判为并发冲突。
        因此还原动作必须同步更新日志状态。
        """
        paths = [str(item) for item in rel_paths if str(item)]
        if not paths:
            return 0
        placeholders = ",".join("?" for _ in paths)
        clauses = [f"task_id = ?", f"rel_path IN ({placeholders})", "mode = 'committed'"]
        params: list[Any] = [task_id, *paths]
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        cur = self.db.execute(f"UPDATE file_changes SET mode = 'reverted' WHERE {' AND '.join(clauses)}", params)
        return int(cur.rowcount or 0)

    def record(
        self,
        *,
        project_id: str,
        task_id: str,
        run_id: str | None,
        agent_id: str | None,
        changes: list[dict[str, Any]],
        scope_patterns: list[str],
        read_only: bool,
        workspace_root: Path,
        work_root: Path | None = None,
        after_root: Path | None = None,
        mark_conflicts: bool = True,
        deny_patterns: list[str] | None = None,
    ) -> list[RecordedChange]:
        recorded: list[RecordedChange] = []
        rel_paths = [c["rel_path"] for c in changes]
        others = self.conflicts(project_id, task_id, rel_paths) if mark_conflicts else []
        conflict_map: dict[str, list[dict[str, Any]]] = {}
        for item in others:
            conflict_map.setdefault(item["rel_path"], []).append(item)

        now = now_iso()
        for change in changes:
            rel = change["rel_path"]
            # 第一层：路径本身是否合法（越界 / UNC / 盘符 / 符号链接逃逸）。
            # 这一层与「是否在写范围内」是两回事：路径逃逸是更强的信号，
            # 必须让调用方（guard/runner）明确看到，不能被 scope 判定掩盖。
            escape_reason = None
            try:
                safe_rel = normalize_rel_path(rel)
                if safe_rel != to_posix(str(rel)).lstrip("./"):
                    escape_reason = "路径未归一（可能借 .. 或分隔符绕过范围校验）"
            except PathEscapeError as exc:
                safe_rel = None
                escape_reason = str(exc)
            if safe_rel is not None and not _is_within_workspace(workspace_root, safe_rel):
                escape_reason = "路径经符号链接越界，落点在工作区之外"
            escaped_path = escape_reason is not None
            # 第二层：写范围校验（需求 35）
            in_scope = self.scope_check(
                rel, scope_patterns, read_only, deny_patterns, workspace_root=workspace_root
            )
            if escaped_path or read_only:
                # 只读任务的一切改动都要还原；路径逃逸同理
                in_scope = False
            reason = escape_reason
            peers = conflict_map.get(rel, [])
            conflict = bool(peers) and not _same_effect(change, peers)
            manual_review = conflict and (bool(change.get("is_binary")) or any(p.get("is_binary") for p in peers))
            if peers and _same_effect(change, peers):
                conflict, manual_review = False, False
            # 保存改动前的内容（用于回滚）。
            # 优先级：调用方给的 before_hash 且**仓库里真有这个 blob** > 基线工作副本
            # > 原工作区现场文件。为什么必须校验 blob：快照算出来的哈希通常并不在
            # blob 仓库里，信了它会让回滚在事后因 blob 缺失而失败；而如果仓库里确实
            # 有这份内容，它就是最准确的「改动前」字节。
            before_hash = change.get("before_hash")
            if change["action"] in ("modify", "delete") and not self.blobs.exists(before_hash):
                before_source = None
                if work_root is not None:
                    candidate = _safe_child(work_root, rel)
                    if candidate is not None and candidate.is_file():
                        before_source = candidate
                if before_source is None:
                    candidate = _safe_child(workspace_root, rel)
                    if candidate is not None and candidate.is_file():
                        before_source = candidate
                if before_source is not None:
                    digest = self.blobs.put_file(before_source)
                    if digest:
                        before_hash = digest
            # 保存改动后的内容（用于差异查看与「本次任务内再次改动」的回滚基线）
            after_hash = change.get("after_hash")
            if change["action"] in ("create", "modify") and after_root is not None:
                after_source = _safe_child(after_root, rel)
                if after_source is not None and after_source.exists() and after_source.is_file():
                    digest = self.blobs.put_file(after_source)
                    if digest:
                        after_hash = digest
            fragment = None
            if peers:
                fragment = {
                    "others": [
                        {
                            "task_id": item["task_id"],
                            "title": item.get("title"),
                            "agent_id": item.get("agent_id"),
                            "status": item.get("task_status"),
                        }
                        for item in peers
                    ]
                }
                if manual_review:
                    fragment["manual_review"] = True
                    fragment["reason"] = "非文本文件并发改动无法自动合并"
            cur = self.db.execute(
                """
                INSERT INTO file_changes
                  (project_id, task_id, run_id, agent_id, rel_path, action, before_hash,
                   after_hash, before_size, after_size, in_scope, mode, conflict, is_binary, fragment, at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'committed', ?, ?, ?, ?)
                """,
                (
                    project_id,
                    task_id,
                    run_id,
                    agent_id,
                    rel,
                    change["action"],
                    before_hash,
                    after_hash,
                    change.get("before_size"),
                    change.get("after_size"),
                    1 if in_scope else 0,
                    1 if conflict else 0,
                    1 if change.get("is_binary") else 0,
                    None if fragment is None else json.dumps(fragment, ensure_ascii=False),
                    now,
                ),
            )
            recorded.append(
                RecordedChange(
                    row_id=int(cur.lastrowid),
                    action=change["action"],
                    rel_path=rel,
                    in_scope=in_scope,
                    conflict=conflict,
                    is_binary=bool(change.get("is_binary")),
                    manual_review=manual_review,
                    reason=reason,
                    escaped_path=escaped_path,
                    others=fragment["others"] if fragment else [],
                )
            )
        return recorded

    def revert(
        self,
        change_id: int,
        *,
        workspace_root: Path,
        mode: str = "reverted",
        expect_current_hash: str | None = None,
    ) -> bool:
        """按一条改动记录回滚：恢复内容或删除新建文件（需求 36）。

        - 目标路径必须落在工作区内，否则抛 :class:`PathEscapeError`
          （回滚不是「任意文件写」原语）；
        - 需要 blob 却取不到时抛 :class:`BlobMissingError`，绝不静默成功；
        - 传了 ``expect_current_hash``（该路径只有这一次改动时）而现场内容已经
          漂移，则拒绝盲写并显式报错。
        """
        row = self.db.query_one("SELECT * FROM file_changes WHERE id = ?", (change_id,))
        if not row:
            return False
        target = resolve_workspace_path(workspace_root, row["rel_path"])
        action = row["action"]
        if action == "create":
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            self.db.update("file_changes", {"id": change_id}, {"mode": mode})
            return True
        # delete / modify 都需要改动前内容
        before_hash = row["before_hash"]
        if not before_hash:
            raise BlobMissingError(
                f"回滚 {row['rel_path']} 失败：记录中没有改动前内容哈希（无法还原）"
            )
        if not self.blobs.exists(before_hash):
            raise BlobMissingError(
                f"回滚 {row['rel_path']} 失败：blob {before_hash[:12]}… 缺失，拒绝静默成功"
            )
        if expect_current_hash and target.exists() and target.is_file():
            current = sha256_file(target)
            if current != expect_current_hash:
                raise BlobMissingError(
                    f"回滚 {row['rel_path']} 失败：当前内容已被其他改动覆盖"
                    f"（当前 {current[:12]}… ≠ 记录 {expect_current_hash[:12]}…），拒绝盲写"
                )
        if not self.blobs.restore_to(before_hash, target):
            raise BlobMissingError(f"回滚 {row['rel_path']} 失败：blob 还原失败")
        self.db.update("file_changes", {"id": change_id}, {"mode": mode})
        return True

    def revert_task(self, task_id: str, *, workspace_root: Path) -> dict[str, int]:
        """按任务回滚全部改动，使工作区逐字节回到改动前状态（需求 36）。

        同一路径可能有多条改动（改名 = delete + create、同文件多次 modify），
        顺序因此很关键：

        1. 先处理 delete/modify，按 id **倒序**：后一条记录的 ``before_hash`` 才是
           更接近改动前的内容，倒序让最新的还原先写、更早的还原最后写，最终落在
           最早记录给出的「改动前」内容上；
        2. 最后撤销 create：否则「改名」场景下新建出来的文件会把刚还原的旧文件
           覆盖掉（兄弟路径并存），或把已还原内容再次删掉；
        3. 全部完成后清理改动期间留下的空目录骨架。
        """
        rows = self.db.query(
            """
            SELECT id, action, rel_path, after_hash FROM file_changes
            WHERE task_id = ? AND mode = 'committed' ORDER BY id ASC
            """,
            (task_id,),
        )
        stats: dict[str, Any] = {"reverted": 0, "failed": 0, "errors": []}
        if not rows:
            return stats
        first_seen: dict[str, None] = {}
        # 同一路径只回滚一次（取能代表「改动前」的那条记录）
        for row in rows:
            rel = row["rel_path"]
            if rel not in first_seen:
                first_seen[rel] = None
        rows_by_path: dict[str, list[dict[str, Any]]] = {rel: [] for rel in first_seen}
        for row in rows:
            rows_by_path[row["rel_path"]].append(dict(row))

        def pick(records: list[dict[str, Any]]) -> dict[str, Any]:
            """挑出代表「改动前状态」的那条记录。"""
            deletes = [r for r in records if r["action"] == "delete"]
            if deletes:  # 被删过：最早那次 delete 的 before_hash 就是原内容
                return min(deletes, key=lambda r: int(r["id"]))
            return min(records, key=lambda r: int(r["id"]))

        targets = [pick(records) for records in rows_by_path.values()]
        ordered = sorted(
            targets,
            key=lambda r: (1 if r["action"] == "create" else 0, -int(r["id"])),
        )
        for row in ordered:
            siblings = rows_by_path[row["rel_path"]]
            # 只有该路径在这次任务里仅此一次改动时，after_hash 才等于现场内容；
            # 多次改动时现场是新值，不能拿旧记录的 after_hash 当漂移判据。
            expect = row["after_hash"] if len(siblings) == 1 else None
            try:
                if self.revert(
                    int(row["id"]), workspace_root=workspace_root, expect_current_hash=expect
                ):
                    stats["reverted"] += 1
                else:
                    stats["failed"] += 1
            except (BlobMissingError, PathEscapeError, OSError) as exc:
                stats["failed"] += 1
                stats["errors"].append(str(exc))
        # 回滚后清掉本次新建、现在已空的目录骨架，保证整树状态一致。
        # 非空目录（改动前就有内容）不受影响。
        prune_empty_dirs(workspace_root)
        return stats


def copy_tree(
    source: str | Path,
    dest: str | Path,
    excludes: Iterable[str] | None = None,
    *,
    config=None,
) -> int:
    """整树复制（隔离工作副本），跳过排除项。返回复制文件数。"""
    src = Path(source)
    dst = Path(dest)
    dst.mkdir(parents=True, exist_ok=True)
    count = 0
    for rel in walk_files(src, excludes, config=config):
        source_path = src / rel
        if source_path.is_symlink():
            continue
        try:
            target = resolve_workspace_path(dst, rel)
        except PathEscapeError:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(source_path, target)
        except OSError:
            # 快照/隔离副本不应因 Office 锁文件、被杀进程留下的临时句柄而整体失败。
            continue
        count += 1
    return count
