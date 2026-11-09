"""任务与编排：子任务 DAG、模板、产物、验收、状态机。"""

from .service import TaskService, TaskStatus, FINAL_STATUSES, ACTIVE_STATUSES
from .acceptance import evaluate_acceptance, AcceptanceResult

__all__ = [
    "TaskService",
    "TaskStatus",
    "FINAL_STATUSES",
    "ACTIVE_STATUSES",
    "evaluate_acceptance",
    "AcceptanceResult",
]
