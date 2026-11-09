"""适配器公共契约。"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol


@dataclass
class PermissionRequest:
    """Agent 请求执行一个受控动作（写文件 / 执行命令 / 越界访问……）。"""

    run_id: str
    agent_id: str
    kind: str  # write | execute | read | network | other
    title: str
    detail: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


# 返回 True = 放行；False = 拒绝。None 表示拒绝（安全侧默认）。
PermissionHandler = Callable[[PermissionRequest], bool | None]


@dataclass
class AgentRequest:
    prompt: str
    workspace: Path
    run_dir: Path
    timeout_sec: int = 900
    model: str | None = None
    permission: str = "guarded"  # guarded | autonomous | readonly
    allow_write: bool = True
    env: dict[str, str] = field(default_factory=dict)
    handoff_path: Path | None = None
    task_id: str = ""
    run_id: str = ""
    approve: PermissionHandler | None = None
    on_event: Callable[[dict[str, Any]], None] | None = None
    #: 协作式取消信号（需求 54「终止执行」）。由调用方（engine/runner）持有并 set；
    #: 适配器轮询它，一旦被 set 就整树回收子进程并以「已取消」语义收尾
    #: （exit_code = proc.CANCELLED_EXIT = -15，error 明确写「人工取消」）。
    #: 默认 None = 不启用，行为与历史版本完全一致。
    cancel_event: "threading.Event | None" = None
    #: 跨进程取消哨兵：另一个进程（CLI/API/桌面）无法 set 本进程的 Event，
    #: 因此取消方在 run 目录写入该文件；适配器轮询到文件存在即整树回收子进程。
    #: 与 ``cancel_event`` 等价，二者任一命中都算取消。
    cancel_path: Path | None = None
    #: 单次调用允许保留的 stdout/stderr 上限（字节）。超出部分截断并标记，
    #: 防止失控 Agent 把内存/磁盘/前端事件流吃穿。
    max_output_bytes: int = 32 * 1024 * 1024


@dataclass
class AgentResult:
    ok: bool
    exit_code: int
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    cost_tokens: int = 0
    cost_amount: float = 0.0
    #: 成本数据来源：reported（Agent/协议真实上报）/ estimated（本地估算）/
    #: unreported（协议没有提供）。0 不等于免费，UI 必须据此区分。
    cost_source: str = "unreported"
    transcript: list[dict[str, Any]] = field(default_factory=list)
    session_ref: str | None = None
    messages: list[str] = field(default_factory=list)


class AgentAdapter(Protocol):
    id: str
    label: str
    protocol: str

    def describe(self) -> dict[str, Any]: ...

    def check(self, timeout: float = 20.0) -> dict[str, Any]: ...

    def run(self, request: AgentRequest) -> AgentResult: ...


def build_env(base: dict[str, str], extra: dict[str, str] | None) -> dict[str, str]:
    env = dict(os.environ)
    env.update(base or {})
    env.update(extra or {})
    return env


def effective_cwd(agent: dict[str, Any], workspace: Path) -> Path:
    """Agent 的默认工作目录（需求 3）：

    ``cwd`` 为 ``.`` 时即任务工作区；为相对路径时表示工作区下的子目录；
    为绝对路径时若存在则直接使用。
    """
    configured = str(agent.get("cwd") or ".").strip()
    if configured in ("", ".", "./"):
        return Path(workspace)
    candidate = Path(configured)
    if candidate.is_absolute():
        return candidate if candidate.exists() else Path(workspace)
    target = Path(workspace) / candidate
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return Path(workspace)
    return target


def create_adapter(agent: dict[str, Any], resolver: Callable[[dict[str, Any]], tuple[str, list[str]] | None] | None = None) -> AgentAdapter:
    """工厂：按 ``protocol`` 创建适配器。

    ``resolver`` 仅在 ACP 模式下可覆盖；CLI 模式一律用 ``cli`` 子对象解析，
    避免把 ACP 的启动命令误用到一次性 CLI 上。
    """
    from .acp_adapter import AcpAdapter
    from .cli_adapter import CliAdapter
    from .resolve import resolve_agent_argv, resolve_cli_argv

    protocol = (agent.get("protocol") or "cli").lower()
    if protocol == "human":
        from .human_adapter import HumanAdapter

        return HumanAdapter(agent)
    if protocol == "acp":
        return AcpAdapter(agent, resolver or resolve_agent_argv)
    return CliAdapter(agent, resolve_cli_argv)
