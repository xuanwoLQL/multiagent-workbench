"""每 Agent 的调用次数 / 时长 / 费用上限（需求 45 / 47）。

超限即停止派发，并在额度汇总里可见。

并发纪律：**准入必须走 :meth:`QuotaService.reserve`**（原子校验+占用）。
:meth:`check` 只是只读查询，``check() → 执行 → record()`` 是典型的 check-then-act，
并发下会超发（本仓库有回归测试证明这一点）。
"""

from __future__ import annotations

from typing import Any

from ..config import Config
from ..db import Database
from ..util import now_iso


class QuotaExceeded(RuntimeError):
    def __init__(self, agent_id: str, reason: str):
        super().__init__(reason)
        self.agent_id = agent_id
        self.reason = reason


class QuotaService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    def usage(self, agent_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM agent_usage WHERE agent_id = ?", (agent_id,))
        if not row:
            return {"agent_id": agent_id, "invocations": 0, "seconds": 0.0, "cost": 0.0, "tokens": 0}
        return dict(row)

    def limits(self, agent_id: str) -> dict[str, Any]:
        agent = self.config.agent(agent_id) or {}
        return dict(agent.get("quota") or {})

    def check(self, agent_id: str) -> tuple[bool, str]:
        """只读检查（展示 / 预览用）。**不要**用它做派发准入——见 :meth:`reserve`。"""
        limits = self.limits(agent_id)
        if not limits:
            return True, ""
        usage = self.usage(agent_id)
        if limits.get("max_invocations") and usage["invocations"] >= limits["max_invocations"]:
            return False, f"调用次数已达上限 {limits['max_invocations']}"
        if limits.get("max_seconds") and usage["seconds"] >= limits["max_seconds"]:
            return False, f"累计时长已达上限 {limits['max_seconds']}s"
        if limits.get("max_cost") and usage["cost"] >= limits["max_cost"]:
            return False, f"累计费用已达上限 {limits['max_cost']}"
        return True, ""

    def reserve(
        self,
        agent_id: str,
        *,
        invocations: int = 1,
        seconds: float = 0.0,
        cost: float = 0.0,
        tokens: int = 0,
    ) -> tuple[bool, str]:
        """原子「校验 + 占用」：在一条条件 UPDATE 里完成，事务保护。

        返回 ``(True, "")`` 表示额度已占用（调用方必须在完成时用 ``record`` 补记真实
        耗时/费用，且 ``invocations=0``，避免重复计数）；``(False, reason)`` 表示应拒绝派发。
        """
        limits = self.limits(agent_id)
        if not limits:
            self.record(agent_id, seconds=seconds, cost=cost, tokens=tokens, invocations=invocations)
            return True, ""

        max_inv = limits.get("max_invocations") or None
        max_sec = limits.get("max_seconds") or None
        max_cost = limits.get("max_cost") or None
        now = now_iso()
        with self.db.transaction():
            # 先保证行存在（幂等），再在单条 UPDATE 里做条件累加
            self.db.execute(
                """
                INSERT INTO agent_usage (agent_id, invocations, seconds, cost, tokens, updated_at)
                VALUES (?, 0, 0, 0, 0, ?)
                ON CONFLICT(agent_id) DO NOTHING
                """,
                (agent_id, now),
            )
            cursor = self.db.execute(
                """
                UPDATE agent_usage
                   SET invocations = invocations + ?, seconds = seconds + ?, cost = cost + ?,
                       tokens = tokens + ?, updated_at = ?
                 WHERE agent_id = ?
                   AND (? IS NULL OR invocations + ? <= ?)
                   AND (? IS NULL OR seconds + ? <= ?)
                   AND (? IS NULL OR cost + ? <= ?)
                """,
                (
                    invocations, seconds, cost, tokens, now, agent_id,
                    max_inv, invocations, max_inv,
                    max_sec, seconds, max_sec,
                    max_cost, cost, max_cost,
                ),
            )
            if cursor.rowcount == 1:
                return True, ""
            usage = self.usage(agent_id)
        return False, self._blocked_reason(usage, limits, invocations, seconds, cost)

    @staticmethod
    def _blocked_reason(
        usage: dict[str, Any], limits: dict[str, Any], invocations: int, seconds: float, cost: float
    ) -> str:
        max_inv = limits.get("max_invocations")
        max_sec = limits.get("max_seconds")
        max_cost = limits.get("max_cost")
        if max_inv and usage["invocations"] + invocations > max_inv:
            return f"调用次数已达上限 {max_inv}（已用 {usage['invocations']}）"
        if max_sec and usage["seconds"] + seconds > max_sec:
            return f"累计时长已达上限 {max_sec}s（已用 {round(usage['seconds'], 3)}s）"
        if max_cost and usage["cost"] + cost > max_cost:
            return f"累计费用已达上限 {max_cost}（已用 {usage['cost']}）"
        return "额度超限"

    def release(
        self,
        agent_id: str,
        *,
        invocations: int = 0,
        seconds: float = 0.0,
        cost: float = 0.0,
        tokens: int = 0,
    ) -> None:
        """归还一次已占用但最终没真正执行的额度（例如适配器构造失败）。"""
        self.db.execute(
            """
            UPDATE agent_usage
               SET invocations = MAX(0, invocations - ?), seconds = MAX(0, seconds - ?),
                   cost = MAX(0, cost - ?), tokens = MAX(0, tokens - ?), updated_at = ?
             WHERE agent_id = ?
            """,
            (invocations, seconds, cost, tokens, now_iso(), agent_id),
        )

    def record(self, agent_id: str, *, seconds: float = 0.0, cost: float = 0.0, tokens: int = 0, invocations: int = 1) -> None:
        """累加**实际**用量（单条 UPSERT，天然并发安全）。

        通过 :meth:`reserve` 预占过调用次数时，这里应传 ``invocations=0``，只补记耗时/费用。
        """
        self.db.execute(
            """
            INSERT INTO agent_usage (agent_id, invocations, seconds, cost, tokens, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
              invocations = invocations + excluded.invocations,
              seconds = seconds + excluded.seconds,
              cost = cost + excluded.cost,
              tokens = tokens + excluded.tokens,
              updated_at = excluded.updated_at
            """,
            (agent_id, invocations, seconds, cost, tokens, now_iso()),
        )


    def summary(self) -> list[dict[str, Any]]:
        agents = self.config.agents
        result: list[dict[str, Any]] = []
        for agent in agents:
            agent_id = agent.get("id")
            usage = self.usage(agent_id)
            limits = self.limits(agent_id)
            ok, reason = self.check(agent_id)
            result.append(
                {
                    "agent_id": agent_id,
                    "label": agent.get("label"),
                    "enabled": agent.get("enabled", True),
                    "usage": usage,
                    "limits": limits,
                    "allowed": ok,
                    "reason": reason,
                }
            )
        return result
