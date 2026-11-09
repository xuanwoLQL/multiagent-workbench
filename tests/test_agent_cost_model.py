"""P1-12 / P2-13 回归：模型控制语义必须诚实，成本来源不得伪造。

约束（与实现保持一致）：

* ACP v1 没有经过验证的请求级 model 字段 —— 适配器不得发送 model，
  ``describe()`` 必须声明 ``model_control == "agent-config"``；
* CLI 只在参数骨架真的含 ``{model}`` 时才注入，``describe()`` 必须如实反映；
* 成本来源 ``AgentResult.cost_source``：协议带货 = ``reported``，
  完全没带 = ``unreported``（0 绝不能冒充真实成本 / 免费）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from multiagent.agents.acp_adapter import AcpAdapter, _Usage
from multiagent.agents.base import AgentRequest, AgentResult
from multiagent.agents.cli_adapter import CliAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]


def _request(workspace: Path, *, run_id: str = "cost-run") -> AgentRequest:
    return AgentRequest(
        prompt="ping",
        workspace=workspace,
        run_dir=workspace / "run",
        timeout_sec=60,
        run_id=run_id,
        task_id=run_id,
    )


def _bad_acp_agent(mode: str) -> dict:
    """``fake_acp_bad``：mode 为空 = 正常跑完且**不带**任何 usage。"""
    return {
        "id": "bad-acp-cost",
        "label": "Bad ACP",
        "protocol": "acp",
        "command": sys.executable,
        "args": ["-m", "multiagent.testing.fake_acp_bad"],
        "env": {"PYTHONPATH": str(REPO_ROOT), "FAKE_ACP_MODE": mode},
        "roles": ["worker"],
        "can_write": True,
        "enabled": True,
    }


# --------------------------------------------------------------- AgentResult 默认值


def test_agent_result_default_cost_source_is_unreported():
    result = AgentResult(ok=True, exit_code=0)
    assert result.cost_source == "unreported"
    assert result.cost_tokens == 0
    assert result.cost_amount == 0.0


# --------------------------------------------------------------- ACP：usage 上报


def test_acp_usage_sets_task_cost_source_reported(make_wb, acp_agent, plan_writer):
    """假 ACP 的 usage_update 带 46 tokens：任务结果必须标成 reported。"""
    plan_writer("cost-acp", {"files": {"out.txt": "done"}, "handoff": {"did": "ACP 完成"}, "exit": 0})
    wb = make_wb([acp_agent("cost-acp")])
    project = wb.projects.create("cost")
    task = wb.tasks.create(
        project["id"],
        "ACP 成本来源",
        kind="generic",
        agent_id="cost-acp",
        acceptance=[{"type": "file_exists", "path": "out.txt"}],
    )
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success", outcome

    cost = wb.tasks.get(task["id"])["result"]["cost"]
    assert cost["source"] == "reported"
    # fake_acp 只上报 token（totalTokens=46）、不上报费用：amount 保持 0，
    # 但来源必须是 reported —— UI 才能据此区分「真实为 0」与「未上报」。
    assert cost["tokens"] == 46
    assert cost["amount"] == 0.0

    run = wb.audit.runs(task_id=task["id"])[0]
    assert run["cost_tokens"] == 46


def test_acp_cost_amount_usage_is_reported(tmp_path):
    """只带 cost、同时带 used/size 的 ACP 实现也必须识别为 reported。"""
    result = AcpAdapter(_bad_acp_agent("usage_cost")).run(_request(tmp_path))
    assert result.ok is True, result.error
    assert result.cost_source == "reported"
    assert result.cost_tokens == 4321
    assert result.cost_amount == pytest.approx(0.0123)


def test_explicit_zero_usage_is_still_reported():
    """协议显式上报 0 token 是「真实为 0」，不是「没上报」。"""
    assert AcpAdapter._usage_reported(
        {"sessionUpdate": "usage_update", "usage": {"totalTokens": 0}}
    ) is True
    assert AcpAdapter._usage_reported(
        {"sessionUpdate": "usage_update", "usage": {}}
    ) is False
    assert AcpAdapter._usage_reported(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    ) is False


def test_absorb_keeps_reported_even_when_tokens_are_zero():
    usage = _Usage()
    AcpAdapter._absorb(usage, _Usage(tokens=0, reported=True))
    assert usage.reported is True
    assert usage.tokens == 0


# --------------------------------------------------------------- ACP：无 usage


def test_acp_without_usage_stays_unreported(tmp_path):
    """fake_acp_bad 默认模式不发送任何 usage：结果必须是 unreported + 0。"""
    result = AcpAdapter(_bad_acp_agent("")).run(_request(tmp_path))
    assert result.ok is True, result.error
    assert result.cost_source == "unreported"
    assert result.cost_tokens == 0
    assert result.cost_amount == 0.0


# --------------------------------------------------------------- CLI：永不解析成本


def test_cli_adapter_never_reports_cost(tmp_path, cli_agent):
    result = CliAdapter(cli_agent("cost-cli")).run(_request(tmp_path))
    assert result.ok is True, result.error
    assert result.cost_source == "unreported"
    assert result.cost_tokens == 0
    assert result.cost_amount == 0.0


# --------------------------------------------------------------- describe() 语义


def test_acp_describe_declares_model_control_agent_config():
    adapter = AcpAdapter(
        {
            "id": "acp-model",
            "label": "ACP",
            "protocol": "acp",
            "command": sys.executable,
            "args": [],
            "model": "agents-json-model",
        }
    )
    info = adapter.describe()
    assert info["model_control"] == "agent-config"


def test_cli_describe_model_control_matches_argv_behavior():
    with_placeholder = CliAdapter(
        {
            "id": "cli-model",
            "protocol": "cli",
            "cli": {"command": sys.executable, "args": ["--model", "{model}"], "prompt_mode": "stdin"},
        }
    )
    assert with_placeholder.describe()["model_control"] == "request-arg"

    without_placeholder = CliAdapter(
        {
            "id": "cli-model",
            "protocol": "cli",
            "cli": {"command": sys.executable, "args": ["-p"], "prompt_mode": "stdin"},
        }
    )
    assert without_placeholder.describe()["model_control"] == "agent-config"


# --------------------------------------------------------------- 前端静态契约


def test_agents_view_has_honest_model_control_copy():
    source = (
        REPO_ROOT / "webapp" / "src" / "features" / "agents" / "AgentsView.tsx"
    ).read_text(encoding="utf-8")
    assert "模型由 Agent 自身配置决定（ACP 不传递 model）" in source
    assert "CLI 参数注入 {model}" in source
    assert "未配置" in source
