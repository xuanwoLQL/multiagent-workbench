"""Agent 适配层：把任意 CLI 型 Agent 包装成统一的可调度执行单元（需求 1-7）。

不修改 Agent 本体；只通过进程边界（CLI 参数/stdin）或 ACP 协议驱动。
"""

from .base import AgentAdapter, AgentRequest, AgentResult, PermissionRequest, create_adapter

__all__ = [
    "AgentAdapter",
    "AgentRequest",
    "AgentResult",
    "PermissionRequest",
    "create_adapter",
]
