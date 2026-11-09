"""配置加载：Agent 清单 / 任务模板 / 路由规则 / 验收规则 / 策略（需求 51、53）。

配置文件位于 ``<home>/config/``：
    agents.json templates.json routing.json acceptance.json policy.json

``home`` 由环境变量 ``MULTIAGENT_HOME`` 指定，未设置时使用当前工作目录。
数据（数据库、运行时、隔离工作副本、快照）统一放在 ``<home>/.multiagent/``。
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import presets
from .util import ensure_dir, write_json

CONFIG_FILES = ("agents.json", "templates.json", "routing.json", "acceptance.json", "policy.json")


def resolve_home(home: str | os.PathLike[str] | None = None) -> Path:
    if home:
        return Path(home).expanduser().resolve()
    env = os.environ.get("MULTIAGENT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


@dataclass
class Config:
    home: Path
    config_dir: Path
    data_dir: Path
    agents_raw: dict[str, Any]
    templates_raw: dict[str, Any]
    routing_raw: dict[str, Any]
    acceptance_raw: dict[str, Any]
    policy_raw: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 构造
    @classmethod
    def load(cls, home: str | os.PathLike[str] | None = None) -> "Config":
        home_path = resolve_home(home)
        config_dir = home_path / "config"
        data_dir = home_path / ".multiagent"
        warnings: list[str] = []
        errors: list[str] = []
        loaded: dict[str, Any] = {}
        for name in CONFIG_FILES:
            path = config_dir / name
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append(f"配置 {name} 读取/解析失败：{exc}；已回退内置默认值")
                    data = None
                if data is not None and not isinstance(data, dict):
                    errors.append(f"配置 {name} 不是 JSON 对象；已回退内置默认值")
                    data = None
                loaded[name] = data
            else:
                loaded[name] = None
        cfg = cls(
            home=home_path,
            config_dir=config_dir,
            data_dir=data_dir,
            agents_raw=loaded["agents.json"] or copy.deepcopy(presets.DEFAULT_AGENTS),
            templates_raw=loaded["templates.json"] or copy.deepcopy(presets.DEFAULT_TEMPLATES),
            routing_raw=loaded["routing.json"] or copy.deepcopy(presets.DEFAULT_ROUTING),
            acceptance_raw=loaded["acceptance.json"] or copy.deepcopy(presets.DEFAULT_ACCEPTANCE),
            policy_raw=loaded["policy.json"] or copy.deepcopy(presets.DEFAULT_POLICY),
            warnings=warnings,
            errors=errors,
        )
        cfg._upgrade_legacy_defaults()
        cfg.errors.extend(cfg.validate())
        return cfg

    def _upgrade_legacy_defaults(self) -> None:
        """把 v1 时代落盘的 900 秒配置在内存里升级到大型项目默认值。

        只升级**明确的旧默认值**（900 秒），不会覆盖用户自定义的 60/120 等预算。
        这样桌面版已有数据目录无需手工删配置也能获得新的超时/验收/模板。
        """
        def _int(value: Any, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        policy = self.policy_raw
        legacy_policy = (
            isinstance(policy, dict)
            and "retry" not in policy
            and _int(policy.get("default_timeout_sec")) == 900
        )
        if legacy_policy:
            policy["version"] = max(2, int(policy.get("version") or 1))
            policy["default_timeout_sec"] = presets.DEFAULT_POLICY["default_timeout_sec"]
            for key in ("task_defaults", "retry", "timeout", "workspace"):
                policy.setdefault(key, copy.deepcopy(presets.DEFAULT_POLICY[key]))
            kinds = policy.setdefault("kinds", {})
            for kind, defaults in presets.DEFAULT_POLICY["kinds"].items():
                target = kinds.setdefault(kind, {})
                for key, value in defaults.items():
                    target.setdefault(key, copy.deepcopy(value))
            policy.setdefault(
                "decomposition", copy.deepcopy(presets.DEFAULT_POLICY["decomposition"])
            )
            self.warnings.append("已将旧配置的 900 秒默认超时升级为大型项目默认值（内存生效）")

        agents = self.agents_raw
        defaults = agents.get("defaults") if isinstance(agents, dict) else None
        if (
            isinstance(defaults, dict)
            and _int(defaults.get("timeout_sec")) == 900
            and not any(isinstance(agent, dict) and "timeout_sec" in agent for agent in agents.get("agents", []))
        ):
            defaults["timeout_sec"] = 2400
            for agent in agents.get("agents", []):
                if isinstance(agent, dict) and agent.get("id") == "codex":
                    agent["timeout_sec"] = 3600
            self.warnings.append("已将旧 Agent 默认超时 900 秒升级为 2400 秒")

        acceptance = self.acceptance_raw
        if isinstance(acceptance, dict) and "kind_templates" not in acceptance:
            acceptance["kind_templates"] = copy.deepcopy(presets.DEFAULT_ACCEPTANCE["kind_templates"])
            self.warnings.append("已补齐 code/doc/ppt/diagrams/web/review 验收模板")

        templates = self.templates_raw
        if isinstance(templates, dict) and isinstance(templates.get("templates"), list):
            existing = {
                str(item.get("id")): item
                for item in templates["templates"]
                if isinstance(item, dict)
            }
            legacy_timeouts = {
                "implement": (1800, 2400),
                "write-doc": (1200, 2400),
                "research": (1500, 1800),
                "organize-data": (1200, 1800),
                "review": (900, 1800),
            }
            for template_id, (legacy, updated) in legacy_timeouts.items():
                item = existing.get(template_id)
                if item and _int(item.get("timeout_sec")) == legacy:
                    item["timeout_sec"] = updated
            for item in presets.DEFAULT_TEMPLATES["templates"]:
                if str(item.get("id")) not in existing:
                    templates["templates"].append(copy.deepcopy(item))

    def validate(self) -> list[str]:
        """校验五份配置的结构（不校验业务语义，只保证调度器能安全读取）。"""
        errors: list[str] = []
        for name, raw in (
            ("agents.json", self.agents_raw),
            ("templates.json", self.templates_raw),
            ("routing.json", self.routing_raw),
            ("acceptance.json", self.acceptance_raw),
            ("policy.json", self.policy_raw),
        ):
            errors.extend(validate_config(name, raw))
        return errors

    def materialize(self, *, overwrite: bool = False) -> list[str]:
        """把内置默认配置写到磁盘（缺失时），返回创建的文件列表。"""
        ensure_dir(self.config_dir)
        created: list[str] = []
        for name, default in presets.BUILTIN.items():
            path = self.config_dir / name
            if overwrite or not path.exists():
                write_json(path, default)
                created.append(str(path))
        return created

    # ------------------------------------------------------------------ 目录
    def ensure_dirs(self) -> None:
        ensure_dir(self.data_dir)
        ensure_dir(self.db_path.parent)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "multiagent.sqlite3"

    @property
    def runtime_dir(self) -> Path:
        return ensure_dir(self.data_dir / "runtime")

    @property
    def workspaces_dir(self) -> Path:
        return ensure_dir(self.data_dir / "workspaces")

    @property
    def snapshots_dir(self) -> Path:
        return ensure_dir(self.data_dir / "snapshots")

    # ------------------------------------------------------------------ Agent
    @property
    def agents(self) -> list[dict[str, Any]]:
        items = self.agents_raw.get("agents", [])
        merged: list[dict[str, Any]] = []
        defaults = self.agents_raw.get("defaults", {}) or {}
        for item in items:
            agent = copy.deepcopy(defaults)
            agent.update(copy.deepcopy(item))
            quota = copy.deepcopy(defaults.get("quota", {}))
            quota.update(item.get("quota", {}) or {})
            agent["quota"] = quota
            agent.setdefault("enabled", True)
            agent.setdefault("roles", ["worker"])
            agent.setdefault("strengths", [])
            agent.setdefault("can_write", True)
            merged.append(agent)
        return merged

    def agent_ids(self, *, enabled_only: bool = True) -> list[str]:
        return [
            a["id"]
            for a in self.agents
            if a.get("id") and (a.get("enabled", True) or not enabled_only)
        ]

    def agent(self, agent_id: str) -> dict[str, Any] | None:
        for agent in self.agents:
            if agent.get("id") == agent_id:
                return agent
        return None

    # ------------------------------------------------------------------ 模板
    @property
    def templates(self) -> list[dict[str, Any]]:
        return list(self.templates_raw.get("templates", []))

    def template(self, template_id: str) -> dict[str, Any] | None:
        for tpl in self.templates:
            if tpl.get("id") == template_id:
                return tpl
        return None

    # ------------------------------------------------------------------ 路由
    @property
    def routing(self) -> dict[str, Any]:
        return self.routing_raw

    # ------------------------------------------------------------------ 验收
    @property
    def acceptance_rules(self) -> dict[str, Any]:
        return self.acceptance_raw.get("rules", {})

    @property
    def acceptance_templates(self) -> dict[str, Any]:
        return self.acceptance_raw.get("kind_templates", {})

    def acceptance_rule(self, ref: str) -> list[dict[str, Any]]:
        rule = self.acceptance_rules.get(ref)
        return copy.deepcopy(rule) if rule else []

    def acceptance_template(self, kind: str) -> dict[str, Any]:
        value = self.acceptance_templates.get(kind)
        return copy.deepcopy(value) if isinstance(value, dict) else {}

    # ------------------------------------------------------------------ 策略
    @property
    def policy(self) -> dict[str, Any]:
        return self.policy_raw

    def policy_get(self, path: str, default: Any = None) -> Any:
        node: Any = self.policy_raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def policy_bool(self, path: str, default: bool = False) -> bool:
        """读取布尔策略，兼容手工 JSON 里的字符串/数字写法。

        直接 ``bool("false")`` 会得到 True，足以让 ``auto_merge="false"`` 悄悄
        变成自动合并；策略布尔值必须按人类语义解释（与前端 ``boolValue`` 一致）。
        """
        value = self.policy_get(path, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"", "false", "0", "no", "off", "disabled", "none"}:
                return False
            if normalized in {"true", "1", "yes", "on", "enabled"}:
                return True
            return bool(default)
        if isinstance(value, (int, float)):
            return bool(value)
        return bool(default)

    def kind_defaults(self, kind: str) -> dict[str, Any]:
        kinds = self.policy_get("kinds", {}) or {}
        return copy.deepcopy(kinds.get(kind) or kinds.get("generic") or {})

    # ------------------------------------------------------------------ 展示
    def summary(self) -> dict[str, Any]:
        return {
            "home": str(self.home),
            "config_dir": str(self.config_dir),
            "data_dir": str(self.data_dir),
            "agents": [
                {
                    "id": a.get("id"),
                    "label": a.get("label"),
                    "protocol": a.get("protocol"),
                    "enabled": a.get("enabled", True),
                }
                for a in self.agents
            ],
            "templates": [t.get("id") for t in self.templates],
            "acceptance_rules": sorted(self.acceptance_rules.keys()),
            "acceptance_templates": sorted(self.acceptance_templates.keys()),
            "kinds": sorted((self.policy_get("kinds", {}) or {}).keys()),
            "task_defaults": self.policy_get("task_defaults", {}),
            "routing_strategy": self.routing.get("strategy"),
            "concurrency": self.policy_get("concurrency", {}),
            "warnings": self.warnings,
            "errors": self.errors,
        }


def validate_config(name: str, data: Any) -> list[str]:
    """按文件名校验配置结构；返回错误列表（空=合法）。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        return [f"{name} 必须是 JSON 对象"]
    if name == "agents.json":
        agents = data.get("agents")
        if not isinstance(agents, list) or not agents:
            return ["agents.json 必须包含非空的 agents 数组"]
        seen: set[str] = set()
        for index, agent in enumerate(agents):
            if not isinstance(agent, dict):
                errors.append(f"agents[{index}] 必须是对象")
                continue
            agent_id = str(agent.get("id") or "").strip()
            if not agent_id:
                errors.append(f"agents[{index}].id 必填")
                continue
            if agent_id in seen:
                errors.append(f"agents[].id 重复：{agent_id}")
            seen.add(agent_id)
            protocol = str(agent.get("protocol") or "cli").lower()
            if protocol not in {"acp", "cli", "human"}:
                errors.append(f"agents[{agent_id}].protocol 不支持：{protocol}")
            if protocol != "human" and agent.get("enabled", True):
                command = agent.get("command") or (agent.get("cli") or {}).get("command")
                if not command:
                    errors.append(f"agents[{agent_id}] 缺少 command")
            quota = agent.get("quota") or {}
            if quota and not isinstance(quota, dict):
                errors.append(f"agents[{agent_id}].quota 必须是对象")
            for key in ("max_invocations", "max_seconds", "max_cost"):
                if key in quota:
                    try:
                        if float(quota[key]) < 0:
                            errors.append(f"agents[{agent_id}].quota.{key} 不能为负")
                    except (TypeError, ValueError):
                        errors.append(f"agents[{agent_id}].quota.{key} 必须是数字")
    elif name == "templates.json":
        templates = data.get("templates")
        if not isinstance(templates, list):
            return ["templates.json 必须包含 templates 数组"]
        seen = set()
        for index, template in enumerate(templates):
            if not isinstance(template, dict):
                errors.append(f"templates[{index}] 必须是对象")
                continue
            template_id = str(template.get("id") or "").strip()
            if not template_id:
                errors.append(f"templates[{index}].id 必填")
            elif template_id in seen:
                errors.append(f"templates[].id 重复：{template_id}")
            seen.add(template_id)
    elif name == "routing.json":
        rules = data.get("rules")
        if not isinstance(rules, list):
            errors.append("routing.json.rules 必须是数组")
        else:
            for index, rule in enumerate(rules):
                if not isinstance(rule, dict):
                    errors.append(f"routing.rules[{index}] 必须是对象")
                    continue
                order = rule.get("order")
                if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
                    errors.append(f"routing.rules[{index}].order 必须是字符串数组")
        fallbacks = data.get("fallbacks")
        if fallbacks is not None and (
            not isinstance(fallbacks, list) or not all(isinstance(item, str) for item in fallbacks)
        ):
            errors.append("routing.json.fallbacks 必须是字符串数组")
    elif name == "acceptance.json":
        rules = data.get("rules")
        if not isinstance(rules, dict):
            errors.append("acceptance.json.rules 必须是对象")
        else:
            for ref, conditions in rules.items():
                if not isinstance(conditions, list):
                    errors.append(f"acceptance.rules.{ref} 必须是数组")
        templates = data.get("kind_templates")
        if templates is not None and not isinstance(templates, dict):
            errors.append("acceptance.json.kind_templates 必须是对象")
        elif isinstance(templates, dict):
            for kind, template in templates.items():
                if not isinstance(template, dict):
                    errors.append(f"acceptance.kind_templates.{kind} 必须是对象")
                    continue
                checks = template.get("checks")
                if checks is not None and (
                    not isinstance(checks, list)
                    or not all(isinstance(item, str) for item in checks)
                ):
                    errors.append(f"acceptance.kind_templates.{kind}.checks 必须是字符串数组")
    elif name == "policy.json":
        concurrency = data.get("concurrency") or {}
        if not isinstance(concurrency, dict):
            errors.append("policy.concurrency 必须是对象")
        else:
            for key in ("global", "per_project"):
                if key in concurrency:
                    try:
                        if int(concurrency[key]) < 1:
                            errors.append(f"policy.concurrency.{key} 必须 >= 1")
                    except (TypeError, ValueError):
                        errors.append(f"policy.concurrency.{key} 必须是整数")
        for key in (
            "kinds", "injection", "isolation", "dangerous_ops", "review_gate",
            "retry", "timeout", "workspace", "task_defaults", "decomposition",
        ):
            if key in data and not isinstance(data[key], dict):
                errors.append(f"policy.{key} 必须是对象")
        workspace = data.get("workspace") or {}
        if isinstance(workspace, dict):
            excludes = workspace.get("excludes")
            if excludes is not None and (
                not isinstance(excludes, list)
                or not all(isinstance(item, str) for item in excludes)
            ):
                errors.append("policy.workspace.excludes 必须是字符串数组")
        retry = data.get("retry") or {}
        if isinstance(retry, dict):
            for key in ("timeout_multiplier", "max_timeout_sec", "backoff_seconds"):
                if key in retry:
                    try:
                        if float(retry[key]) < 0:
                            errors.append(f"policy.retry.{key} 不能为负")
                    except (TypeError, ValueError):
                        errors.append(f"policy.retry.{key} 必须是数字")
    return errors
