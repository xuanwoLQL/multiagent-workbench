"""SQLite + WAL 存储层（需求 25 / 39 / 46）。

单文件数据库，进程重启不丢状态；所有领域服务共享一个 :class:`Database`。

并发模型（重要）：
- 连接**不是 per-thread**，而是每进程一条共享连接（``check_same_thread=False``），
  所有访问都经过 :class:`Database` 的方法，并由一把可重入锁 ``_lock`` 串行化；
  禁止直接使用 ``db.conn`` 做并发读写。读操作同样走锁（WAL 允许读写并发，但
  sqlite3.Connection 对象本身不支持同一连接上的并发使用）。
- 跨进程（桌面壳后端 + CLI + serve.py 同时开同一个库）靠 ``busy_timeout`` +
  写事务 ``BEGIN IMMEDIATE`` + 锁冲突重试来协调，绝不假设「只有我一个写入者」。
- 进程身份记录在 ``runtime_sessions``：崩溃恢复（recover）据此判断「任务是不是
  真的还在别的活进程里跑」，而不是看见 ``status='running'`` 就一律判死。
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

_LOCK_ERRORS = ("database is locked", "database table is locked", "database schema is locked")

#: 当前数据库 schema 版本。升级时**只增不减**，并在 ``_MIGRATIONS`` 里登记迁移函数。
SCHEMA_VERSION = 6

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 项目 / 工作区（需求 8-12）
CREATE TABLE IF NOT EXISTS projects (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  label       TEXT NOT NULL DEFAULT '',
  root_path   TEXT NOT NULL,
  origin_path TEXT,
  kind        TEXT NOT NULL DEFAULT 'files',
  readonly    INTEGER NOT NULL DEFAULT 0,
  config      TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

-- 版本快照（需求 11 / 17）
CREATE TABLE IF NOT EXISTS snapshots (
  id         TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  label      TEXT NOT NULL DEFAULT '',
  kind       TEXT NOT NULL DEFAULT 'full',
  store_path TEXT NOT NULL,
  manifest   TEXT NOT NULL DEFAULT '{}',
  parent_id  TEXT,
  file_count INTEGER NOT NULL DEFAULT 0,
  bytes      INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);

-- 任务（需求 13-18 / 38）
CREATE TABLE IF NOT EXISTS tasks (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL,
  parent_id     TEXT,
  title         TEXT NOT NULL,
  kind          TEXT NOT NULL DEFAULT 'generic',
  brief         TEXT NOT NULL DEFAULT '',
  tags          TEXT NOT NULL DEFAULT '[]',
  role          TEXT NOT NULL DEFAULT 'worker',
  template_id   TEXT,
  agent_id      TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',
  priority      INTEGER NOT NULL DEFAULT 0,
  attempt       INTEGER NOT NULL DEFAULT 0,
  max_attempts  INTEGER NOT NULL DEFAULT 2,
  timeout_sec   INTEGER,
  isolation     TEXT NOT NULL DEFAULT 'copy',
  read_only     INTEGER NOT NULL DEFAULT 0,
  write_scope   TEXT NOT NULL DEFAULT '[]',
  acceptance    TEXT NOT NULL DEFAULT '[]',
  acceptance_ref TEXT,
  inputs        TEXT NOT NULL DEFAULT '[]',
  outputs       TEXT NOT NULL DEFAULT '[]',
  workdir       TEXT,
  blocked_reason TEXT,
  last_error    TEXT,
  result        TEXT NOT NULL DEFAULT '{}',
  ordering      INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  started_at    TEXT,
  finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS task_edges (
  task_id    TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  mode       TEXT NOT NULL DEFAULT 'success',
  PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS task_states (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id   TEXT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  reason    TEXT,
  at        TEXT NOT NULL
);

-- Agent 实时事件（跨进程可回放；与 runs 的最终 transcript 互补）
CREATE TABLE IF NOT EXISTS run_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT NOT NULL,
  task_id    TEXT,
  run_id     TEXT,
  agent_id   TEXT,
  kind       TEXT NOT NULL DEFAULT '',
  source_type TEXT,
  at         TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}'
);

-- 每次 Agent 调用（需求 46）
CREATE TABLE IF NOT EXISTS runs (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL,
  task_id      TEXT NOT NULL,
  agent_id     TEXT NOT NULL,
  agent_label  TEXT NOT NULL DEFAULT '',
  protocol     TEXT NOT NULL DEFAULT 'cli',
  role         TEXT,
  attempt      INTEGER NOT NULL DEFAULT 1,
  timeout_sec  INTEGER,
  status       TEXT NOT NULL,
  exit_code    INTEGER,
  started_at   TEXT NOT NULL,
  finished_at  TEXT,
  duration_ms  INTEGER NOT NULL DEFAULT 0,
  cost_tokens  INTEGER NOT NULL DEFAULT 0,
  cost_amount  REAL NOT NULL DEFAULT 0,
  command      TEXT,
  cwd          TEXT,
  prompt_path  TEXT,
  stdout_path  TEXT,
  stderr_path  TEXT,
  transcript_path TEXT,
  handoff_path TEXT,
  error        TEXT,
  last_activity_at TEXT
);

-- 共享黑板条目（需求 19-26）
CREATE TABLE IF NOT EXISTS board_entries (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  task_id     TEXT,
  agent_id    TEXT,
  run_id      TEXT,
  scope       TEXT NOT NULL DEFAULT 'task',   -- project | task | agent
  type        TEXT NOT NULL DEFAULT 'note',   -- brief|artifact|decision|handoff|todo|issue|reference|note
  title       TEXT NOT NULL DEFAULT '',
  body        TEXT NOT NULL DEFAULT '',
  tags        TEXT NOT NULL DEFAULT '[]',
  status      TEXT NOT NULL DEFAULT 'approved', -- pending | approved | rejected
  pinned      INTEGER NOT NULL DEFAULT 0,
  current_version INTEGER NOT NULL DEFAULT 1,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  reviewed_by TEXT,
  reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS board_versions (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id TEXT NOT NULL,
  version  INTEGER NOT NULL,
  title    TEXT NOT NULL DEFAULT '',
  body     TEXT NOT NULL DEFAULT '',
  author   TEXT,
  task_id  TEXT,
  run_id   TEXT,
  note     TEXT,
  at       TEXT NOT NULL
);

-- 追加事件 + 哈希链（需求 22 / 49）
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id TEXT,
  at         TEXT NOT NULL,
  kind       TEXT NOT NULL,
  actor      TEXT,
  task_id    TEXT,
  run_id     TEXT,
  entry_id   TEXT,
  summary    TEXT NOT NULL DEFAULT '',
  data       TEXT NOT NULL DEFAULT '{}',
  prev_hash  TEXT,
  hash       TEXT NOT NULL
);

-- 结构化交接说明（需求 27-31）
CREATE TABLE IF NOT EXISTS handoffs (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  task_id     TEXT NOT NULL,
  run_id      TEXT,
  agent_id    TEXT,
  did         TEXT NOT NULL DEFAULT '',
  not_done    TEXT NOT NULL DEFAULT '',
  open_issues TEXT NOT NULL DEFAULT '[]',
  next_steps  TEXT NOT NULL DEFAULT '[]',
  artifacts   TEXT NOT NULL DEFAULT '[]',
  notes       TEXT NOT NULL DEFAULT '',
  source      TEXT NOT NULL DEFAULT 'agent',   -- agent | human
  edited_by   TEXT,
  edited_at   TEXT,
  created_at  TEXT NOT NULL
);

-- 输入 / 输出产物（需求 18）
CREATE TABLE IF NOT EXISTS artifacts (
  id         TEXT PRIMARY KEY,
  project_id TEXT NOT NULL,
  task_id    TEXT,
  run_id     TEXT,
  role       TEXT NOT NULL DEFAULT 'output',   -- input | output
  rel_path   TEXT NOT NULL,
  abs_path   TEXT NOT NULL,
  sha256     TEXT,
  size       INTEGER NOT NULL DEFAULT 0,
  is_binary  INTEGER NOT NULL DEFAULT 0,
  fragment   TEXT,
  record     TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

-- 文件改动日志（需求 32-37：隔离 / 冲突 / 回滚 / 记录）
CREATE TABLE IF NOT EXISTS file_changes (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id  TEXT NOT NULL,
  task_id     TEXT,
  run_id      TEXT,
  agent_id    TEXT,
  rel_path    TEXT NOT NULL,
  action      TEXT NOT NULL,       -- create | modify | delete
  before_hash TEXT,
  after_hash  TEXT,
  before_size INTEGER,
  after_size  INTEGER,
  in_scope    INTEGER NOT NULL DEFAULT 1,
  mode        TEXT NOT NULL DEFAULT 'committed',  -- committed | reverted | pending
  conflict    INTEGER NOT NULL DEFAULT 0,
  is_binary   INTEGER NOT NULL DEFAULT 0,
  fragment    TEXT,
  at          TEXT NOT NULL
);

-- 人工决策 / 审批（需求 23 / 44 / 55）
CREATE TABLE IF NOT EXISTS approvals (
  id          TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  task_id     TEXT,
  run_id      TEXT,
  agent_id    TEXT,
  kind        TEXT NOT NULL,       -- dangerous_op | conflict | acceptance_failed | quota | manual_confirm | general
  title       TEXT NOT NULL,
  detail      TEXT NOT NULL DEFAULT '{}',
  risk        TEXT NOT NULL DEFAULT 'medium',
  status      TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
  created_at  TEXT NOT NULL,
  resolved_at TEXT,
  resolved_by TEXT,
  resolution  TEXT
);

-- 资源租约（需求 32：同一资源不被并发占用）
CREATE TABLE IF NOT EXISTS leases (
  resource    TEXT PRIMARY KEY,
  project_id  TEXT NOT NULL,
  holder      TEXT NOT NULL,
  task_id     TEXT,
  agent_id    TEXT,
  acquired_at TEXT NOT NULL,
  expires_at  TEXT,
  note        TEXT
);

-- 每 Agent 额度（需求 45 / 47）
CREATE TABLE IF NOT EXISTS agent_usage (
  agent_id   TEXT PRIMARY KEY,
  invocations INTEGER NOT NULL DEFAULT 0,
  seconds    REAL NOT NULL DEFAULT 0,
  cost       REAL NOT NULL DEFAULT 0,
  tokens     INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT
);

-- 运行时进程身份（崩溃恢复的存活判定依据；干净退出时会删除本行）
CREATE TABLE IF NOT EXISTS runtime_sessions (
  session_id   TEXT PRIMARY KEY,
  pid          INTEGER NOT NULL DEFAULT 0,
  host         TEXT NOT NULL DEFAULT '',
  started_at   TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL
);

-- 数据库迁移记录（每次升级留痕；迁移前自动备份）
CREATE TABLE IF NOT EXISTS schema_migrations (
  version     INTEGER PRIMARY KEY,
  applied_at  TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  backup_path TEXT
);

-- 跨进程调度槽位（需求 41：global / per_project 并发上限不能只在单进程内生效）
CREATE TABLE IF NOT EXISTS scheduler_slots (
  slot_id     TEXT PRIMARY KEY,
  session_id  TEXT NOT NULL,
  project_id  TEXT NOT NULL,
  task_id     TEXT,
  acquired_at TEXT NOT NULL,
  expires_at  TEXT NOT NULL
);

-- 通用事件流（时间线导出用；与 events 同表，见上）
CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_run_events_project ON run_events(project_id, id);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_board_project ON board_entries(project_id, scope, type);
CREATE INDEX IF NOT EXISTS idx_changes_task ON file_changes(task_id, rel_path);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at);
CREATE INDEX IF NOT EXISTS idx_slots_project ON scheduler_slots(project_id);
CREATE INDEX IF NOT EXISTS idx_slots_session ON scheduler_slots(session_id);
"""

# 增量迁移：老库补列（ALTER TABLE ADD COLUMN 幂等安全）
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "tasks": {
        "claim_id": "TEXT",
        "claim_owner": "TEXT",
        "claimed_at": "TEXT",
    },
    "runs": {
        "owner_session": "TEXT",
        "heartbeat_at": "TEXT",
        "last_activity_at": "TEXT",
        "evidence_pruned": "INTEGER NOT NULL DEFAULT 0",
        "timeout_sec": "INTEGER",
    },
    "projects": {
        "deleted_at": "TEXT",
    },
}


def _migrate_v2(db: "Database") -> None:
    """v2：跨进程调度槽位 + run 证据裁剪标记。"""
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_slots (
          slot_id     TEXT PRIMARY KEY,
          session_id  TEXT NOT NULL,
          project_id  TEXT NOT NULL,
          task_id     TEXT,
          acquired_at TEXT NOT NULL,
          expires_at  TEXT NOT NULL
        )
        """
    )
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_project ON scheduler_slots(project_id)")
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_slots_session ON scheduler_slots(session_id)")


def _migrate_v3(db: "Database") -> None:
    """v3：为恢复/维护查询补索引（项目删除时间、运行证据裁剪）。"""
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_evidence ON runs(evidence_pruned, finished_at)")
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_updated ON tasks(project_id, updated_at)")
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_kind ON events(project_id, kind, id)")


def _migrate_v4(db: "Database") -> None:
    """v4：运行活跃时间（区分「进程活着」与「Agent 有实际进展」）。"""
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_heartbeat ON runs(status, heartbeat_at)")
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_activity ON runs(status, last_activity_at)")


def _migrate_v5(db: "Database") -> None:
    """v5：Agent 实时事件持久化（跨进程看板 / 崩溃后回放）。"""
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_events (
          id         INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id TEXT NOT NULL,
          task_id    TEXT,
          run_id     TEXT,
          agent_id   TEXT,
          kind       TEXT NOT NULL DEFAULT '',
          source_type TEXT,
          at         TEXT NOT NULL,
          payload    TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_run_events_project ON run_events(project_id, id)")
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, id)")


def _migrate_v6(db: "Database") -> None:
    """v6：记录本次调用的有效超时预算（含重试递增）。"""
    db.conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_timeout ON runs(status, timeout_sec)")


_MIGRATIONS = {2: _migrate_v2, 3: _migrate_v3, 4: _migrate_v4, 5: _migrate_v5, 6: _migrate_v6}


class RuntimeRegistry:
    """进程内运行时身份：会话 id + 正在执行中的 run 集合。

    ``recover`` 的存活判定的权威来源：本进程的 run 是否还在跑，只看这个集合，
    不看数据库里的 ``status='running'``（后者可能是崩溃残留）。
    """

    def __init__(self, session_id: str, pid: int):
        self.session_id = session_id
        self.pid = pid
        self._lock = threading.RLock()
        self._active: set[str] = set()

    def mark_active(self, run_id: str) -> None:
        with self._lock:
            self._active.add(run_id)

    def mark_done(self, run_id: str) -> None:
        with self._lock:
            self._active.discard(run_id)

    def is_active(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._active

    def active_runs(self) -> set[str]:
        with self._lock:
            return set(self._active)


class Database:
    """SQLite 连接包装：WAL、行工厂、串行写、跨进程锁协调。"""

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        lock_retry_attempts: int = 20,
        lock_retry_delay: float = 0.05,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))
        self._retry_attempts = max(0, int(lock_retry_attempts))
        self._retry_delay = max(0.0, float(lock_retry_delay))
        self._conn: sqlite3.Connection | None = None
        self._open()
        # 进程身份：崩溃恢复据此区分「我自己的 run」与「别人的 run」
        self.session_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.pid = os.getpid()
        self.host = os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or ""
        self.runtime = RuntimeRegistry(self.session_id, self.pid)
        self._register_session()

    # ---------------------------------------------------------------- 连接
    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.path), check_same_thread=False, timeout=self._busy_timeout_ms / 1000.0
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(SCHEMA)
        conn.commit()
        self._conn = conn
        self._migrate()
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:  # pragma: no cover - 仅在 restore 窗口内发生
            raise RuntimeError("数据库连接尚未打开")
        return self._conn

    @property
    def schema_version(self) -> int:
        try:
            row = self.query_one("PRAGMA user_version")
            value = int(row[0]) if row else 0
        except (TypeError, ValueError, IndexError):
            value = 0
        if value:
            return value
        # 兼容早期把版本写在 meta 里的库
        row = self.query_one("SELECT value FROM meta WHERE key = 'schema_version'")
        try:
            return int(row["value"]) if row else 0
        except (TypeError, ValueError):
            return 0

    def _migrate(self) -> None:
        """列级增量迁移 + 版本化迁移 + 迁移前自动备份。

        老库升级最怕「改到一半失败」。因此：

        1. 先补齐所有幂等列（ALTER TABLE ADD COLUMN）；
        2. 若磁盘库版本落后，先在 ``<data_dir>/backups/`` 做一份 SQLite 在线备份；
        3. 逐版本执行迁移函数，全部成功后才把 ``schema_version`` 推进；
        4. 迁移后跑 ``quick_check``，失败立即抛错（绝不带着坏库继续服务）。
        """
        conn = self.conn
        for table, columns in _COLUMN_MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.commit()

        current = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
        if current <= 0:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
            if row:
                try:
                    current = int(row["value"])
                except (TypeError, ValueError):
                    current = 0
        # 新库：直接标记为当前版本（schema 已经是最新结构）
        has_data = bool(conn.execute("SELECT COUNT(*) AS c FROM projects").fetchone()["c"])
        if current <= 0 and not has_data:
            conn.execute(f"PRAGMA user_version = {int(SCHEMA_VERSION)}")
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (version, applied_at, description) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, _utc_now(), "initial schema"),
            )
            conn.commit()
            return
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema 版本 {current} 高于当前程序支持的 {SCHEMA_VERSION}；"
                "请升级程序或从备份恢复，禁止降级写入。"
            )
        if current <= 0:
            current = 1
        # 早期版本把版本号写在 meta 里：读到后同步到 PRAGMA，后续统一走 user_version。
        if current:
            conn.execute(f"PRAGMA user_version = {int(current)}")
            conn.commit()

        backup_path = None
        if current < SCHEMA_VERSION:
            backup_path = self._backup_before_migration(current)
            for version in range(current + 1, SCHEMA_VERSION + 1):
                migration = _MIGRATIONS.get(version)
                if migration is None:
                    continue
                migration(self)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations (version, applied_at, description, backup_path) "
                    "VALUES (?, ?, ?, ?)",
                    (version, _utc_now(), migration.__doc__ or f"migration {version}", backup_path),
                )
                conn.execute(
                    f"PRAGMA user_version = {int(version)}"
                )
                conn.commit()
        self._ensure_integrity()

    def _backup_before_migration(self, current_version: int) -> str | None:
        """迁移前在线备份（只保留最近 5 份，避免无限增长）。"""
        backups = self.path.parent / "backups"
        try:
            backups.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            target = backups / f"pre-migration-v{current_version}-{stamp}.sqlite3"
            dst = sqlite3.connect(str(target))
            try:
                self.conn.backup(dst)
                dst.commit()
            finally:
                dst.close()
            for old in sorted(backups.glob("pre-migration-*.sqlite3"), key=lambda p: p.stat().st_mtime, reverse=True)[5:]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return str(target)
        except (OSError, sqlite3.Error):
            return None

    def _ensure_integrity(self) -> None:
        row = self.conn.execute("PRAGMA quick_check").fetchone()
        result = str(row[0]) if row else ""
        if result.lower() != "ok":
            raise RuntimeError(f"数据库完整性校验失败：{result[:500]}")

    # ---------------------------------------------------------------- 备份 / 恢复
    def integrity_check(self, *, full: bool = False) -> dict[str, Any]:
        """``PRAGMA quick_check`` / ``integrity_check``（只读，不修改库）。"""
        pragma = "integrity_check" if full else "quick_check"
        with self._lock:
            rows = list(self.conn.execute(f"PRAGMA {pragma}"))
        values = [str(row[0]) for row in rows]
        return {"ok": bool(values) and all(v.lower() == "ok" for v in values), "result": values}

    def backup_to(self, target: str | os.PathLike[str]) -> dict[str, Any]:
        """用 SQLite 在线备份 API 生成一致性快照（WAL 下也安全）。"""
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with self._lock:
            dst = sqlite3.connect(str(tmp))
            try:
                self.conn.backup(dst)
                dst.commit()
                check = dst.execute("PRAGMA quick_check").fetchone()
                if not check or str(check[0]).lower() != "ok":
                    raise RuntimeError(f"备份完整性校验失败：{check[0] if check else 'empty'}")
            finally:
                dst.close()
        os.replace(tmp, path)
        digest = _sha256_file(path)
        return {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": digest,
            "schema_version": self.schema_version,
            "at": _utc_now(),
        }

    def restore_from(self, source: str | os.PathLike[str], *, backup_current: bool = True) -> dict[str, Any]:
        """从备份恢复数据库（进程内热恢复：服务对象无需重建）。

        安全顺序：校验备份完整性 → 备份当前库 → 关闭连接 → 用 SQLite backup
        写入当前路径 → 重开连接并跑迁移。任一步失败都会抛错，不会留下半截库。
        """
        src = Path(source)
        if not src.is_file():
            raise FileNotFoundError(f"备份文件不存在：{src}")
        probe = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            probe.row_factory = sqlite3.Row
            check = probe.execute("PRAGMA quick_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RuntimeError(f"备份文件完整性校验失败：{check[0] if check else 'empty'}")
            version = 0
            try:
                version = int(probe.execute("PRAGMA user_version").fetchone()[0] or 0)
                if not version:
                    row = probe.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                    version = int(row["value"]) if row else 0
            except (sqlite3.Error, TypeError, ValueError):
                version = 0
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"备份 schema 版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}，拒绝恢复。"
                )
        finally:
            probe.close()

        pre_backup = None
        if backup_current:
            backups = self.path.parent / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            pre_backup = self.backup_to(backups / f"pre-restore-{stamp}.sqlite3")["path"]

        with self._lock:
            if self._conn is not None:
                try:
                    self.conn.execute("DELETE FROM runtime_sessions WHERE session_id = ?", (self.session_id,))
                    self.conn.commit()
                except sqlite3.Error:
                    pass
                self.conn.close()
                self._conn = None
            for suffix in ("-wal", "-shm"):
                try:
                    Path(str(self.path) + suffix).unlink()
                except OSError:
                    pass
            dst = sqlite3.connect(str(self.path))
            try:
                src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
                try:
                    src_conn.backup(dst)
                    dst.commit()
                finally:
                    src_conn.close()
            finally:
                dst.close()
            self._open()
            self._register_session()
        return {
            "restored_from": str(src),
            "pre_restore_backup": pre_backup,
            "schema_version": self.schema_version,
            "at": _utc_now(),
        }

    def _register_session(self) -> None:
        now = _utc_now()
        self.execute(
            """
            INSERT INTO runtime_sessions (session_id, pid, host, started_at, heartbeat_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at, pid = excluded.pid
            """,
            (self.session_id, self.pid, self.host, now, now),
        )

    def touch_session(self) -> None:
        """刷新本进程心跳（调度循环 / 心跳线程调用，成本 = 一条 UPDATE）。"""
        try:
            self.execute(
                "UPDATE runtime_sessions SET heartbeat_at = ?, pid = ? WHERE session_id = ?",
                (_utc_now(), self.pid, self.session_id),
            )
        except Exception:  # noqa: BLE001 - 心跳失败不能拖垮调度
            pass

    def sessions(self) -> list[dict[str, Any]]:
        """所有会话行（含 pid/host，供存活判定使用）。"""
        return [
            dict(row)
            for row in self.query(
                "SELECT session_id, pid, host, started_at, heartbeat_at FROM runtime_sessions"
            )
        ]

    def live_sessions(self, stale_seconds: float) -> set[str]:
        """**按心跳**判定仍然新鲜的会话（含本进程）。

        注意：这只是「跨 host / 拿不到 PID」时的兜底信号。同机会话应当用 PID 存在性
        （见 ``Engine._live_sessions``）——PID 没了就是没了，不必等心跳过期。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0.0, float(stale_seconds)))
        live: set[str] = set()
        for row in self.sessions():
            moment = _parse_iso(row["heartbeat_at"])
            if moment is not None and moment >= cutoff:
                live.add(row["session_id"])
        return live

    def purge_stale_sessions(self, older_than_seconds: float = 86400.0) -> int:
        """清理很老的历史会话行（避免 runtime_sessions 无限增长）。"""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max(0.0, float(older_than_seconds)))
        removed = 0
        for row in self.query("SELECT session_id, heartbeat_at FROM runtime_sessions"):
            moment = _parse_iso(row["heartbeat_at"])
            if row["session_id"] != self.session_id and moment is not None and moment < cutoff:
                self.execute("DELETE FROM runtime_sessions WHERE session_id = ?", (row["session_id"],))
                removed += 1
        return removed

    def _run(self, sql: str, params: Any, *, many: bool = False) -> sqlite3.Cursor:
        if self._conn is None:
            raise RuntimeError("数据库连接已关闭")
        attempt = 0
        while True:
            try:
                if many:
                    cur = self._conn.executemany(sql, params)
                else:
                    cur = self._conn.execute(sql, params)
                if self._tx_depth == 0:
                    self._conn.commit()
                return cur
            except sqlite3.OperationalError as exc:
                locked = any(marker in str(exc).lower() for marker in _LOCK_ERRORS)
                if not locked or self._tx_depth > 0 or attempt >= self._retry_attempts:
                    raise
                attempt += 1
                # 另一进程正持写锁：让出时间片后重试（busy_timeout 之外的第二道保险）
                time.sleep(self._retry_delay)

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._run(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._lock:
            return self._run(sql, list(seq), many=True)

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def query_one(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def insert(self, table: str, values: dict[str, Any]) -> None:
        cols = ", ".join(values.keys())
        marks = ", ".join(["?"] * len(values))
        self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))

    def update(self, table: str, key: dict[str, Any], values: dict[str, Any]) -> None:
        if not values:
            return
        sets = ", ".join(f"{k}=?" for k in values)
        where = " AND ".join(f"{k}=?" for k in key)
        self.execute(
            f"UPDATE {table} SET {sets} WHERE {where}",
            list(values.values()) + list(key.values()),
        )

    def close(self) -> None:
        with self._lock:
            if self._conn is None:
                return
            try:
                # 干净退出：删掉自己的会话行，别的进程 recover 时能立刻判定本进程的 run 已死
                self._conn.execute("DELETE FROM runtime_sessions WHERE session_id = ?", (getattr(self, "session_id", ""),))
                self._conn.commit()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._conn.commit()
            finally:
                self._conn.close()
                self._conn = None

    # ---------------------------------------------------------------- 事务
    def transaction(self):
        """真事务：块内 ``execute`` 不提交，退出时统一 commit / rollback。

        顶层用 ``BEGIN IMMEDIATE``（一开始就拿写锁，避免「先读后写」升级死锁）；
        嵌套时退化为 SAVEPOINT。跨进程的争抢由 ``busy_timeout`` + 重试兜底。
        """
        return _Transaction(self)


class _Transaction:
    """with db.transaction(): —— 失败自动回滚（支持嵌套 SAVEPOINT）。"""

    def __init__(self, db: Database):
        self.db = db
        self._top = False

    def __enter__(self):
        self.db._lock.acquire()
        try:
            if self.db._tx_depth == 0:
                self.db._conn.execute("BEGIN IMMEDIATE")
                self._top = True
            else:
                self.db._conn.execute(f"SAVEPOINT multiagent_sp_{self.db._tx_depth}")
                self._top = False
            self.db._tx_depth += 1
        except BaseException:
            self.db._lock.release()
            raise
        return self.db

    def __exit__(self, exc_type, exc, tb):
        self.db._tx_depth -= 1
        try:
            if self._top:
                if exc_type is None:
                    self.db._conn.commit()
                else:
                    self.db._conn.rollback()
            else:
                name = f"multiagent_sp_{self.db._tx_depth}"
                if exc_type is None:
                    self.db._conn.execute(f"RELEASE {name}")
                else:
                    self.db._conn.execute(f"ROLLBACK TO {name}")
                    self.db._conn.execute(f"RELEASE {name}")
        finally:
            self.db._lock.release()
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def _sha256_file(path: str | os.PathLike[str], chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()

