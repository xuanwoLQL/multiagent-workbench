"""pytest 夹具：一个隔离的临时工作台 + 假 Agent。

不依赖真实模型，全部走「假 CLI / 假 ACP」两个测试替身。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _fake_cli(agent_id: str, plan_file: Path, **extra) -> dict:
    agent = {
        "id": agent_id,
        "label": agent_id.title(),
        "protocol": "cli",
        "cli": {
            "command": sys.executable,
            "args": ["-m", "multiagent.testing.fake_agent"],
            "prompt_mode": "stdin",
        },
        "env": {"PYTHONPATH": str(REPO_ROOT), "FAKE_PLAN_FILE": str(plan_file)},
        "roles": ["worker", "implementer"],
        "strengths": ["code", "doc"],
        "can_write": True,
        "enabled": True,
    }
    agent.update(extra)
    return agent


def _fake_acp(agent_id: str, plan_file: Path, **extra) -> dict:
    agent = {
        "id": agent_id,
        "label": agent_id.title(),
        "protocol": "acp",
        "command": sys.executable,
        "args": ["-m", "multiagent.testing.fake_acp"],
        "env": {"PYTHONPATH": str(REPO_ROOT), "FAKE_PLAN_FILE": str(plan_file)},
        "roles": ["worker"],
        "strengths": ["code"],
        "can_write": True,
        "enabled": True,
    }
    agent.update(extra)
    return agent


@pytest.fixture
def cli_agent(home: Path):
    def _make(agent_id: str, **extra):
        return _fake_cli(agent_id, home / f"plan-{agent_id}.json", **extra)

    return _make


@pytest.fixture
def acp_agent(home: Path):
    def _make(agent_id: str, **extra):
        return _fake_acp(agent_id, home / f"plan-{agent_id}.json", **extra)

    return _make


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    (path / "config").mkdir(parents=True)
    return path


@pytest.fixture
def plan_writer(home: Path):
    """写某个假 Agent 的行为计划，返回写入函数。"""

    def _write(name: str, plan: dict) -> Path:
        path = home / f"plan-{name}.json"
        path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        return path

    return _write


@pytest.fixture
def make_wb(home: Path):
    """按需构造工作台；可先写配置再启动。"""
    created = []

    def _make(agents: list[dict], *, policy_extra: dict | None = None, routing: dict | None = None):
        config_dir = home / "config"
        (config_dir / "agents.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "defaults": {"timeout_sec": 60, "quota": {"max_invocations": 1000, "max_seconds": 100000, "max_cost": 1000}},
                    "agents": [
                        *agents,
                        {"id": "human", "label": "人工", "protocol": "human", "enabled": True},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        if routing:
            (config_dir / "routing.json").write_text(json.dumps(routing, ensure_ascii=False), encoding="utf-8")
        policy = {
            "version": 1,
            "concurrency": {"global": 4, "per_project": 2},
            "default_timeout_sec": 60,
            "context_budget_chars": 8000,
            "isolation": {"default": "copy", "sync_back_on_accept": True, "block_conflict": True, "revert_out_of_scope": True},
            "dangerous_ops": {
                "require_confirmation": ["delete", "overwrite_existing_artifact", "modify_project_config"],
                "auto_approve": False,
                "protect_paths": [".multiagent", "config", ".git"],
            },
            "review_gate": {"memory_requires_approval": True, "default_entry_status": "approved"},
            # 测试套件避免每个任务都起 PowerShell 查 CIM；CIM 语义由
            # tests/test_process_audit.py 的注入用例覆盖。
            "processes": {"cim_fallback": False},
            "snapshot": {"on_task_start": False, "keep": 20},
            "kinds": {
                "code": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-changed"},
                "doc": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-exists"},
                "review": {"isolation": "shared", "read_only": True, "acceptance_ref": "review-readonly"},
                "manual": {"isolation": "shared", "read_only": False, "acceptance_ref": "manual-confirm"},
                "generic": {"isolation": "copy", "read_only": False, "acceptance_ref": "file-exists"},
            },
        }
        if policy_extra:
            policy.update(policy_extra)
        (config_dir / "policy.json").write_text(json.dumps(policy, ensure_ascii=False), encoding="utf-8")

        from multiagent.app import Workbench

        wb = Workbench.open(home)
        created.append(wb)
        return wb

    yield _make
    for wb in created:
        try:
            wb.close(wait_audits=False)
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def wb(make_wb, plan_writer, home: Path):
    """默认工作台：worker（CLI 假 Agent）+ coder（ACP 假 Agent）。"""
    plan_writer("worker", {})
    plan_writer("coder", {})
    return make_wb(
        [
            _fake_cli("worker", home / "plan-worker.json", roles=["worker", "implementer", "writer"], strengths=["code", "doc"]),
            _fake_acp("coder", home / "plan-coder.json", roles=["worker", "implementer"], strengths=["code"]),
        ]
    )


@pytest.fixture
def set_plan(home: Path):
    """运行时修改某个假 Agent 的行为计划（下一次调用生效）。"""

    def _set(name: str, plan: dict) -> None:
        (home / f"plan-{name}.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    return _set


@pytest.fixture
def project(wb):
    return wb.projects.create("demo", root_path=None)
