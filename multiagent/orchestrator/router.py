"""Agent 路由：显式指定 + 规则自动选择（需求 5）。

规则来源 ``config/routing.json``：按 任务类型 / 角色 / 标签 匹配候选顺序，
再按策略（first_available / least_loaded / most_budget）在候选里挑一个。
"""

from __future__ import annotations

from typing import Any

from ..config import Config


def _rule_matches(rule: dict[str, Any], task: dict[str, Any]) -> bool:
    when = rule.get("when") or {}
    if "kind" in when and when["kind"] != task.get("kind"):
        return False
    if "role" in when and when["role"] != task.get("role"):
        return False
    if "tag" in when and when["tag"] not in (task.get("tags") or []):
        return False
    if "agent_id" in when and when["agent_id"] != task.get("agent_id"):
        return False
    return True


def candidates(config: Config, task: dict[str, Any]) -> list[str]:
    routing = config.routing
    order: list[str] = []
    for rule in routing.get("rules", []):
        if _rule_matches(rule, task):
            for agent_id in rule.get("order", []):
                if agent_id not in order:
                    order.append(agent_id)
    for agent_id in routing.get("fallbacks", []):
        if agent_id not in order:
            order.append(agent_id)
    # 偏好（模板里声明的擅长领域）优先
    prefer = (config.template(task.get("template_id") or "") or {}).get("prefer") or {}
    preferred = [a for a in prefer.get("agents", []) if a in order]
    if preferred:
        rest = [a for a in order if a not in preferred]
        order = preferred + rest
    return order


def choose_agent(
    *,
    config: Config,
    task: dict[str, Any],
    is_available,
    is_allowed,
    load: dict[str, int] | None = None,
    remaining: dict[str, float] | None = None,
) -> tuple[str | None, str]:
    """返回 (agent_id, 理由)。``is_available``/``is_allowed`` 为回调。"""
    explicit = task.get("agent_id")
    if explicit:
        if is_available(explicit) and is_allowed(explicit):
            return explicit, "任务显式指定"
        return None, f"显式指定的 Agent 不可用或超额：{explicit}"

    order = candidates(config, task)
    usable = [a for a in order if is_available(a) and is_allowed(a)]
    if not usable:
        return None, "没有可用且未超额的 Agent"

    strategy = str(config.routing.get("strategy", "first_available"))
    load = load or {}
    remaining = remaining or {}
    if strategy == "least_loaded":
        usable.sort(key=lambda a: (load.get(a, 0), order.index(a)))
    elif strategy == "most_budget":
        usable.sort(key=lambda a: (-remaining.get(a, 1e9), order.index(a)))
    return usable[0], f"按规则选择（策略 {strategy}）"
