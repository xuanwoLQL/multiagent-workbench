"""任务运行期参数：超时解析、重试预算与剩余时间。

时间预算有三层来源，优先级从高到低：

1. 任务显式 ``timeout_sec``；
2. 显式/路由选中的 Agent ``timeout_sec``；
3. 任务类型默认 ``timeout_sec``，最后回退 policy 全局默认。

首次尝试使用基础预算；超时/中断后的下一次尝试按 policy 的
``retry.timeout_multiplier`` 自动放大，并由 ``retry.max_timeout_sec`` 封顶。
这样不会出现「第二次重试仍然只有 900 秒」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..config import Config
from ..util import parse_iso

DEFAULT_RETRY_MULTIPLIER = 1.5
DEFAULT_MAX_RETRY_TIMEOUT = 3600


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def base_timeout(task: dict[str, Any], config: Config, agent_id: str | None = None) -> int:
    """基础超时（秒；``<=0`` 表示不限时）。"""
    value = task.get("timeout_sec")
    agent = (config.agent(agent_id) or {}) if agent_id else {}
    if value is None and agent:
        value = agent.get("timeout_sec")
    if value is None:
        kind_defaults = config.kind_defaults(str(task.get("kind") or "generic"))
        value = kind_defaults.get("timeout_sec")
    if value is None:
        value = config.policy_get("default_timeout_sec", 1800)
    return _as_int(value, 0)


def effective_timeout(
    base_seconds: int,
    attempt: int,
    config: Config,
) -> int:
    """按尝试次数放大超时预算；`attempt=1` 保持基础值。"""
    if base_seconds <= 0:
        return base_seconds
    attempt = max(1, _as_int(attempt, 1))
    multiplier = float(
        config.policy_get("retry.timeout_multiplier", DEFAULT_RETRY_MULTIPLIER)
        or DEFAULT_RETRY_MULTIPLIER
    )
    cap = _as_int(
        config.policy_get("retry.max_timeout_sec", DEFAULT_MAX_RETRY_TIMEOUT),
        DEFAULT_MAX_RETRY_TIMEOUT,
    )
    if cap <= 0:
        cap = DEFAULT_MAX_RETRY_TIMEOUT
    scaled = int(round(base_seconds * (multiplier ** (attempt - 1))))
    return max(1, min(cap, scaled))


def resolve_timeout(
    task: dict[str, Any],
    config: Config,
    agent_id: str | None = None,
    *,
    attempt: int | None = None,
) -> int:
    base = base_timeout(task, config, agent_id)
    current_attempt = attempt
    if current_attempt is None:
        current_attempt = _as_int(task.get("attempt"), 0) or 1
    return effective_timeout(base, current_attempt, config)


def _elapsed_seconds(task: dict[str, Any], now: datetime, running: bool) -> float | None:
    started = parse_iso(task.get("started_at"))
    if started is None:
        return None
    finished = parse_iso(task.get("finished_at"))
    if not running and finished is not None:
        now = finished
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0.0, (now - started).total_seconds())


def runtime_info(
    task: dict[str, Any],
    config: Config,
    *,
    now: datetime | None = None,
    agent_id: str | None = None,
) -> dict[str, Any]:
    """给任务详情/运行面板用的时间预算快照。

    该函数只读任务行，不查 DB；dashboard 的 100 任务预算不会被 N+1 拖垮。
    """
    current = now or datetime.now(timezone.utc)
    attempt = max(1, _as_int(task.get("attempt"), 0) or 1)
    base = base_timeout(task, config, agent_id or task.get("agent_id"))
    current_timeout = effective_timeout(base, attempt, config)
    next_timeout = effective_timeout(base, attempt + 1, config)
    running = str(task.get("status") or "") == "running"
    elapsed = _elapsed_seconds(task, current, running)
    remaining: float | None = None
    warning = False
    warning_text = ""
    if elapsed is not None and current_timeout > 0:
        remaining = max(0.0, round(current_timeout - elapsed, 1)) if running else None
        threshold = _as_int(
            config.policy_get("timeout.warning_seconds", min(600, max(60, current_timeout // 10))),
            min(600, max(60, current_timeout // 10)),
        )
        if running and remaining <= threshold:
            warning = True
            warning_text = (
                f"剩余 {max(0, int(remaining))} 秒（阈值 {threshold} 秒）；"
                "可延长任务 timeout 后由下一次尝试使用"
            )
    return {
        "base_timeout_sec": base,
        "effective_timeout_sec": current_timeout,
        "next_attempt_timeout_sec": next_timeout,
        "attempt": attempt,
        "max_attempts": _as_int(task.get("max_attempts"), 0),
        "elapsed_sec": None if elapsed is None else round(elapsed, 1),
        "remaining_sec": remaining,
        "warning": warning,
        "warning_text": warning_text,
    }
