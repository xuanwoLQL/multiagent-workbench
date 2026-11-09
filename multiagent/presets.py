"""内置默认配置（作为 config/*.json 的初始副本，也是配置缺失时的兜底）。

配置驱动是硬要求（需求 51）：以下内容全部可以在 <home>/config/*.json 里覆盖，
不改代码即可生效。
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- Agent

DEFAULT_AGENTS: dict[str, Any] = {
    "version": 1,
    "defaults": {
        "timeout_sec": 2400,
        "permission": "guarded",
        "quota": {"max_invocations": 200, "max_seconds": 36000, "max_cost": 60.0},
    },
    "agents": [
        {
            "id": "codex",
            "label": "Codex",
            "protocol": "acp",
            "command": "codex-acp",
            "args": [],
            "cli": {"command": "codex", "args": ["exec", "-"], "prompt_mode": "stdin"},
            "roles": ["lead", "implementer", "verifier", "reviser", "reviewer"],
            "strengths": ["code", "engineering", "refactor", "tests", "build"],
            "can_write": True,
            "enabled": True,
            "notes": "官方 ACP 适配器 @agentclientprotocol/codex-acp；CLI 兜底 codex exec -",
        },
        {
            "id": "claude",
            "label": "Claude Code",
            "protocol": "acp",
            "command": "claude-agent-acp",
            "args": [],
            "cli": {"command": "claude", "args": ["-p"], "prompt_mode": "stdin"},
            "roles": ["lead", "reviewer", "verifier", "writer", "reviser"],
            "strengths": ["doc", "prose", "review", "research", "reasoning"],
            "can_write": True,
            "enabled": True,
            "notes": "官方 ACP 适配器 @agentclientprotocol/claude-agent-acp；CLI 兜底 claude -p",
        },
        {
            "id": "opencode",
            "label": "OpenCode",
            "protocol": "acp",
            "command": "opencode",
            "args": ["acp"],
            "cli": {"command": "opencode", "args": ["run"], "prompt_mode": "arg", "append_prompt": True},
            "roles": ["lead", "implementer", "recon", "worker"],
            "strengths": ["code", "shell", "data", "explore", "general"],
            "can_write": True,
            "enabled": True,
            "notes": "自带 `opencode acp` 子命令；CLI 兜底 opencode run <message>",
        },
        {
            "id": "dsh",
            "label": "DeepSeek Harness",
            "protocol": "acp",
            "command": "dsh",
            "args": ["--profile", "acp"],
            "cli": None,
            "resolve": "dsh",
            "roles": ["implementer", "summarizer", "worker"],
            "strengths": ["code", "data", "summarize", "general"],
            "can_write": True,
            "enabled": True,
            "notes": "DeepSeek Harness 桌面版自带 CLI，`--profile acp` 即标准 ACP v1 stdio server",
        },
    ],
}

# --------------------------------------------------------------------------- 任务模板

DEFAULT_TEMPLATES: dict[str, Any] = {
    "version": 1,
    "templates": [
        {
            "id": "implement",
            "name": "实现代码 / 功能",
            "kind": "code",
            "role": "implementer",
            "isolation": "copy",
            "read_only": False,
            "brief": "在工作区中完成以下编码任务：\n{{goal}}",
            "acceptance_ref": "file-changed",
            "prefer": {"strengths": ["code"], "agents": ["codex", "opencode", "dsh", "claude"]},
            "timeout_sec": 2400,
        },
        {
            "id": "write-doc",
            "name": "撰写 / 改写文档",
            "kind": "doc",
            "role": "writer",
            "isolation": "copy",
            "read_only": False,
            "brief": "在工作区中完成以下文档任务：\n{{goal}}",
            "acceptance_ref": "report-written",
            "prefer": {"strengths": ["doc", "prose"], "agents": ["claude", "codex", "dsh", "opencode"]},
            "timeout_sec": 2400,
        },
        {
            "id": "research",
            "name": "调研并产出笔记",
            "kind": "research",
            "role": "researcher",
            "isolation": "copy",
            "read_only": False,
            "brief": "针对以下主题做调研，把结论写进工作区的调研笔记：\n{{goal}}",
            "acceptance_ref": "file-exists",
            "prefer": {"strengths": ["research", "reasoning"], "agents": ["claude", "dsh", "codex", "opencode"]},
            "timeout_sec": 1800,
        },
        {
            "id": "organize-data",
            "name": "整理 / 清洗数据",
            "kind": "data",
            "role": "worker",
            "isolation": "copy",
            "read_only": False,
            "brief": "整理工作区中的数据文件：\n{{goal}}",
            "acceptance_ref": "file-changed",
            "prefer": {"strengths": ["data"], "agents": ["opencode", "dsh", "codex", "claude"]},
            "timeout_sec": 1800,
        },
        {
            "id": "review",
            "name": "审阅他人产出（只读）",
            "kind": "review",
            "role": "reviewer",
            "isolation": "shared",
            "read_only": True,
            "brief": "以只读方式审阅工作区中已有的产出，给出评审意见（不要修改任何文件）：\n{{goal}}",
            "acceptance_ref": "review-readonly",
            "prefer": {"strengths": ["review", "reasoning"], "agents": ["claude", "codex", "dsh", "opencode"]},
            "timeout_sec": 1800,
        },
        {
            "id": "human-step",
            "name": "人工执行步骤",
            "kind": "manual",
            "role": "human",
            "isolation": "shared",
            "read_only": False,
            "brief": "需要人工完成：\n{{goal}}",
            "acceptance_ref": "manual-confirm",
            "prefer": {"agents": ["human"]},
            "timeout_sec": 0,
        },
        {
            "id": "make-ppt",
            "name": "制作 PPT / 演示材料",
            "kind": "ppt",
            "role": "writer",
            "isolation": "copy",
            "read_only": False,
            "brief": "根据已有材料制作可打开的 PPT：\n{{goal}}",
            "acceptance_ref": "file-exists",
            "prefer": {"strengths": ["doc", "prose"], "agents": ["claude", "codex", "dsh", "opencode"]},
            "timeout_sec": 2400,
        },
        {
            "id": "make-diagrams",
            "name": "制作图表 / 架构图",
            "kind": "diagrams",
            "role": "worker",
            "isolation": "copy",
            "read_only": False,
            "brief": "生成图表、架构图或数据可视化页面：\n{{goal}}",
            "acceptance_ref": "file-changed",
            "prefer": {"strengths": ["data", "code"], "agents": ["codex", "claude", "opencode", "dsh"]},
            "timeout_sec": 1800,
        },
        {
            "id": "build-web",
            "name": "实现 Web 页面 / 3D 可视化",
            "kind": "web",
            "role": "implementer",
            "isolation": "copy",
            "read_only": False,
            "brief": "实现可运行的 Web 页面与 3D/可视化交互：\n{{goal}}",
            "acceptance_ref": "file-changed",
            "prefer": {"strengths": ["code", "data"], "agents": ["codex", "opencode", "claude", "dsh"]},
            "timeout_sec": 2400,
        },
    ],
}

# --------------------------------------------------------------------------- 路由

DEFAULT_ROUTING: dict[str, Any] = {
    "version": 1,
    "strategy": "first_available",
    "rules": [
        {"when": {"kind": "code"}, "order": ["codex", "opencode", "dsh", "claude"]},
        {"when": {"kind": "doc"}, "order": ["claude", "codex", "dsh", "opencode"]},
        {"when": {"kind": "research"}, "order": ["claude", "dsh", "codex", "opencode"]},
        {"when": {"kind": "data"}, "order": ["opencode", "dsh", "codex", "claude"]},
        {"when": {"kind": "review"}, "order": ["claude", "codex", "dsh", "opencode"]},
        {"when": {"role": "reviewer"}, "order": ["claude", "codex", "dsh", "opencode"]},
        {"when": {"tag": "research"}, "order": ["claude", "dsh", "opencode", "codex"]},
        {"when": {"tag": "refactor"}, "order": ["codex", "opencode", "dsh", "claude"]},
    ],
    "fallbacks": ["opencode", "codex", "claude", "dsh", "human"],
}

# --------------------------------------------------------------------------- 验收规则

DEFAULT_ACCEPTANCE: dict[str, Any] = {
    "version": 1,
    "kind_templates": {
        "code": {"checks": ["outputs_exist", "file_modified", "outputs_nonempty", "verify_command"], "min_bytes": 1, "timeout_sec": 2400, "max_attempts": 3},
        "doc": {"checks": ["outputs_exist", "outputs_nonempty"], "min_bytes": 32, "timeout_sec": 2400, "max_attempts": 3},
        "ppt": {"checks": ["outputs_exist", "outputs_nonempty"], "min_bytes": 1024, "timeout_sec": 2400, "max_attempts": 3},
        "diagrams": {"checks": ["outputs_exist", "outputs_nonempty"], "min_bytes": 64, "timeout_sec": 1800, "max_attempts": 3},
        "web": {"checks": ["outputs_exist", "outputs_nonempty"], "min_bytes": 64, "timeout_sec": 2400, "max_attempts": 3},
        "review": {"checks": ["no_writes", "handoff_present"], "timeout_sec": 1800, "max_attempts": 2},
        "research": {"checks": ["outputs_exist", "outputs_nonempty"], "min_bytes": 1, "timeout_sec": 1800, "max_attempts": 3},
        "data": {"checks": ["outputs_exist", "file_modified", "outputs_nonempty"], "min_bytes": 1, "timeout_sec": 1800, "max_attempts": 3},
        "generic": {"checks": ["file_modified", "handoff_present"], "timeout_sec": 1800, "max_attempts": 3},
    },
    "rules": {
        "file-exists": [{"type": "file_exists", "path": "{{target}}"}],
        "file-changed": [{"type": "file_modified"}],
        "report-written": [
            {"type": "file_exists", "path": "{{target}}"},
            {"type": "content_contains", "path": "{{target}}", "text": "{{contains}}"},
        ],
        "no-writes": [{"type": "no_writes"}],
        "review-readonly": [{"type": "no_writes"}, {"type": "handoff_present"}],
        "manual-confirm": [{"type": "manual_confirm"}],
        "exit-zero": [{"type": "command_exit_zero", "command": "{{command}}"}],
    },
}

# --------------------------------------------------------------------------- 策略 / 安全

DEFAULT_POLICY: dict[str, Any] = {
    "version": 1,
    "concurrency": {"global": 6, "per_project": 3},
    "default_timeout_sec": 1800,
    "task_defaults": {"timeout_sec": 1800, "max_attempts": 3},
    "retry": {"backoff_seconds": 0, "timeout_multiplier": 1.5, "max_timeout_sec": 3600},
    "timeout": {"warning_seconds": 300},
    "workspace": {
        "ignore_file": ".multiagentignore",
        "excludes": [],
    },
    "context_budget_chars": 12000,
    "injection": {
        "include_handoffs": True,
        "include_decisions": True,
        "include_open_issues": True,
        "include_todos": True,
        "include_project_memory": True,
        "max_handoffs": 5,
        "max_entries_per_type": 8,
    },
    "isolation": {
        "default": "copy",
        "sync_back_on_accept": True,
        "block_conflict": True,
        "revert_out_of_scope": True,
    },
    "git": {
        # 默认「先审后合」：验收通过只提交任务分支，不自动合并回基线。
        # 需要 DAG 连续交付（下游任务立即看到上游产出）的项目显式设为 true，
        # 或使用 docs 中的「连续交付预设」。
        "auto_merge": False,
        # merge 前要求基线工作树干净（忽略 .multiagent 控制面）；脏树直接阻断。
        "require_clean_tree": True,
        # 下游任务必须等前置任务的 git 分支真正合并后才可派发（先审后合默认）。
        "require_merged_for_dependents": True,
        # merge 成功后是否保留 worktree 目录（任务分支永远保留）。
        "keep_worktree": True,
        # ops.prune 清理已合并 worktree 的保留天数。
        "worktree_retention_days": 14,
        "author_name": "MultiAgent",
        "author_email": "multiagent@local",
    },
    "processes": {
        # Agent 调用前后审计监听端口；关闭后不记录也不回收。
        "audit_enabled": True,
        # 只回收「能证明属于本任务 workdir」的新增监听进程；判断不了只告警。
        "reap_orphans": True,
        # finish 阶段用 Windows CIM 补齐 netstat 漏报；失败只降级 warning。
        "cim_fallback": True,
        # 低于该超时预算的极短任务不启动审计；默认 0 = 全部审计。
        "min_timeout_sec": 0,
    },
    "dangerous_ops": {
        "require_confirmation": ["delete", "overwrite_existing_artifact", "modify_project_config"],
        "auto_approve": False,
        "protect_paths": [".multiagent", "config", ".git"],
    },
    "review_gate": {"memory_requires_approval": True, "default_entry_status": "approved"},
    "snapshot": {"on_task_start": False, "keep": 20},
    "kinds": {
        "code": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-changed", "timeout_sec": 2400, "max_attempts": 3},
        "doc": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-exists", "timeout_sec": 2400, "max_attempts": 3},
        "ppt": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-exists", "timeout_sec": 2400, "max_attempts": 3},
        "diagrams": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-changed", "timeout_sec": 1800, "max_attempts": 3},
        "web": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-changed", "timeout_sec": 2400, "max_attempts": 3},
        "data": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-changed", "timeout_sec": 1800, "max_attempts": 3},
        "research": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-exists", "timeout_sec": 1800, "max_attempts": 3},
        "review": {"isolation": "shared", "read_only": True, "acceptance_ref": "review-readonly", "timeout_sec": 1800, "max_attempts": 2},
        "manual": {"isolation": "shared", "read_only": False, "acceptance_ref": "manual-confirm", "timeout_sec": 0, "max_attempts": 1},
        "generic": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-exists", "timeout_sec": 1800, "max_attempts": 3},
    },
    "decomposition": {
        "agent_by_stage": {
            "requirements-freeze": "claude",
            "sim-core": "codex",
            "web-3d": "codex",
            "diagrams": "claude",
            "ppt": "claude",
            "docs": "claude",
            "review": "claude",
            "fix": "codex",
            "final-qa": "codex",
        }
    },
}

BUILTIN: dict[str, dict[str, Any]] = {
    "agents.json": DEFAULT_AGENTS,
    "templates.json": DEFAULT_TEMPLATES,
    "routing.json": DEFAULT_ROUTING,
    "acceptance.json": DEFAULT_ACCEPTANCE,
    "policy.json": DEFAULT_POLICY,
}
