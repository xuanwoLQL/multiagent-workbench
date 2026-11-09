"""工作区：项目/工作区管理、版本快照、隔离工作副本、文件改动日志（需求 8-12、32-37）。"""

from .projects import ProjectService
from .snapshots import SnapshotService
from .isolation import IsolationService, snapshot_tree, diff_trees

__all__ = ["ProjectService", "SnapshotService", "IsolationService", "snapshot_tree", "diff_trees"]
