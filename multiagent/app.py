"""应用装配：把配置、存储与各领域服务组装成一个工作台实例。"""

from __future__ import annotations

import time
from pathlib import Path

from .audit import AuditService
from .board.handoff import HandoffService
from .board.service import BoardService
from .config import Config
from .db import Database
from .logging_setup import configure_logging
from .ops import OpsService
from .orchestrator.engine import Engine
from .orchestrator.runner import TaskRunner
from .run_events import RunEventStore
from .safety.guard import GuardService
from .safety.quota import QuotaService
from .tasks.service import TaskService
from .workspace.isolation import IsolationService
from .workspace.projects import ProjectService
from .workspace.snapshots import SnapshotService


class Workbench:
    def __init__(self, config: Config):
        self.config = config
        self.started_at = time.time()
        config.ensure_dirs()
        configure_logging(config.data_dir, level=config.policy_get("logging.level", "INFO"))
        self.db = Database(config.db_path)
        self.projects = ProjectService(self.db, config)
        self.board = BoardService(self.db, config)
        self.snapshots = SnapshotService(self.db, config, board=self.board)
        self.isolation = IsolationService(self.db, config)
        self.handoffs = HandoffService(self.db, config, board=self.board)
        self.tasks = TaskService(self.db, config)
        self.guard = GuardService(self.db, config)
        self.quota = QuotaService(self.db, config)
        self.audit = AuditService(self.db, config)
        self.ops = OpsService(self.db, config, board=self.board)
        self.run_events = RunEventStore(self.db)
        self.runner = TaskRunner(
            self.db,
            config,
            tasks=self.tasks,
            projects=self.projects,
            board=self.board,
            handoffs=self.handoffs,
            guard=self.guard,
            quota=self.quota,
            isolation=self.isolation,
        )
        # 所有进程（CLI / API / 桌面）都先把实时事件落库，再通知本进程订阅者。
        # 这样「CLI 跑任务、另开看板」也能看到实时流；API 只是额外订阅一份。
        self.runner.event_sink = self.run_events.feed
        self.engine = Engine(self)

    @classmethod
    def open(cls, home: str | Path | None = None, *, init: bool = True) -> "Workbench":
        config = Config.load(home)
        if init:
            config.materialize()
        return cls(config)

    def close(self, *, wait_audits: bool = True) -> None:
        self.engine.shutdown()
        # 给异步进程审计一个有界窗口写回结果/回收孤儿，随后再关 DB。
        # 测试 teardown 可传 wait_audits=False 以保持套件速度。
        if wait_audits:
            try:
                self.runner.wait_for_process_audits(timeout=10.0)
            except Exception:  # noqa: BLE001 - 关闭路径不能再抛
                pass
        self.run_events.close()
        self.db.close()
