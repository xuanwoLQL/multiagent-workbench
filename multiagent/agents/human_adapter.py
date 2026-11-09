"""人工执行者适配器（需求 57）。

人可以作为「一个执行者」参与编排：任务派给 ``human`` 时不拉起任何进程，
而是把任务挂起为等待人工，人工完成后用 ``task provide`` / API 回填产物。
"""

from __future__ import annotations

from typing import Any, Callable

from .base import AgentAdapter, AgentRequest, AgentResult

AWAITING_HUMAN = -100


class HumanAdapter(AgentAdapter):
    protocol = "human"

    def __init__(self, agent: dict[str, Any], resolver: Callable[[dict[str, Any]], tuple[str, list[str]] | None] | None = None):
        self.id = agent.get("id", "human")
        self.label = agent.get("label") or "人工"
        self.agent = agent
        self.resolver = resolver

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "protocol": self.protocol,
            "roles": ["human"],
            "strengths": ["judgement", "approval", "taste"],
            "can_write": True,
            "quota": {},
            "notes": "人工作为执行者",
        }

    def check(self, timeout: float = 20.0) -> dict[str, Any]:
        return {"ok": True, "installed": True, "detail": "人工执行者，无需可执行文件"}

    def run(self, request: AgentRequest) -> AgentResult:
        return AgentResult(
            ok=False,
            exit_code=AWAITING_HUMAN,
            error="等待人工完成",
            transcript=[{"type": "human", "awaiting": True}],
        )
