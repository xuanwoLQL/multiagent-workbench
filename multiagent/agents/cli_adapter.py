"""一次性 CLI 适配器（通用 AgentShell 模式，需求 4）。

任何「接受提示词 → 在工作目录里干活 → 退出」的 CLI 都能接入，只要在配置里
描述好命令、参数骨架与提示词投递方式。不依赖 Agent 本体的任何改动。

提示词投递三种模式：
    stdin   提示词走标准输入（默认；``claude -p``、``codex exec -`` 这类）
    arg     提示词作为命令行参数（可用 ``{prompt}`` 占位符精确落位；
            没写占位符时**自动追加到末尾**，绝不静默丢弃）
    append  ``arg`` + 追加的简写（``opencode run <message>`` 这类）
    file    提示词落盘为 ``<run_dir>/prompt.txt``，用 ``{prompt_file}`` 引用

超时通过 ``agents/proc.py`` 回收**整棵进程树**；输出按「UTF-8 → 本地代码页」
解码，避免中文 Windows 下 CLI（尤其 .cmd shim）的乱码。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from ..util import ensure_dir, sha256_text, truncate, which, write_text
from . import proc as procmod
from .base import AgentAdapter, AgentRequest, AgentResult, build_env, effective_cwd
from .resolve import explain_missing, wrap_executable

PROMPT_MODES = ("stdin", "arg", "append", "file")


class CliAdapter(AgentAdapter):
    protocol = "cli"

    def __init__(
        self,
        agent: dict[str, Any],
        resolver: Callable[[dict[str, Any]], tuple[str, list[str]] | None] | None = None,
    ):
        self.id = agent.get("id", "unknown")
        self.label = agent.get("label") or self.id
        self.agent = agent
        self.cli = dict(agent.get("cli") or {})
        if not self.cli:
            self.cli = {
                "command": agent.get("command") or self.id,
                "args": list(agent.get("args") or []),
                "prompt_mode": "stdin",
            }
        self.cli.setdefault("prompt_mode", "stdin")
        self.cli.setdefault("args", [])
        self.resolver = resolver

    # ------------------------------------------------------------------ 描述
    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "protocol": self.protocol,
            "command": self.cli.get("command"),
            "args": self.cli.get("args"),
            "prompt_mode": self.cli.get("prompt_mode"),
            "roles": self.agent.get("roles", []),
            "strengths": self.agent.get("strengths", []),
            "can_write": self.agent.get("can_write", True),
            "quota": self.agent.get("quota", {}),
            # 与 _build_argv 的真实行为一致：只有参数骨架里写了 {model}，
            # 才会把 AgentRequest.model 替换进去；否则模型只能由 CLI 自身配置决定。
            "model_control": "request-arg" if self._injects_model() else "agent-config",
        }

    def _injects_model(self) -> bool:
        """参数骨架里是否真的包含 ``{model}`` 占位符（见 :meth:`_build_argv`）。"""
        return any("{model}" in str(arg) for arg in (self.cli.get("args") or []))

    # ------------------------------------------------------------------ 解析
    def _resolve_command(self) -> tuple[str, list[str]] | None:
        if self.resolver:
            resolved = self.resolver(self.agent)
            if resolved:
                return resolved
        command = self.cli.get("command") or self.id
        text = str(command)
        if Path(text).is_absolute() or "/" in text or "\\" in text:
            target = Path(text)
            if target.is_file():
                return wrap_executable(str(target), [])
            return None
        found = which(text)
        if not found:
            return None
        return wrap_executable(found, [])

    def _build_argv(
        self,
        request: AgentRequest,
        *,
        command: str,
        prefix: list[str],
        prompt_file: Path,
    ) -> tuple[list[str], str | None, str]:
        """返回 (argv, stdin_text, prompt_mode)。"""
        mapping = {
            "{prompt}": request.prompt,
            "{prompt_file}": str(prompt_file),
            "{workspace}": str(request.workspace),
            "{model}": request.model or "",
        }
        skeleton = list(self.cli.get("args") or [])
        args: list[str] = []
        for raw in list(prefix) + skeleton:
            item = str(raw)
            for key, value in mapping.items():
                item = item.replace(key, value)
            args.append(item)

        raw_mode = str(self.cli.get("prompt_mode", "stdin")).lower()
        mode = "arg" if raw_mode == "append" else raw_mode
        if mode not in ("stdin", "arg", "file"):
            mode = "stdin"

        if mode == "file" and not any("{prompt_file}" in str(a) for a in skeleton):
            args.append(str(prompt_file))

        if mode == "arg" and request.prompt:
            has_placeholder = any("{prompt}" in str(a) for a in skeleton)
            append = self.cli.get("append_prompt")
            if append is None:
                # 既没写占位符、也没显式配置：默认追加，避免提示词被静默丢弃
                append = not has_placeholder
            if append and not has_placeholder:
                args.append(request.prompt)

        stdin_text = request.prompt if mode == "stdin" else None
        return [command] + args, stdin_text, mode

    def _env_for(self, request: AgentRequest) -> dict[str, str]:
        extra: dict[str, str] = {}
        extra.update(self.agent.get("env", {}) or {})
        perm_env = self.agent.get("permission_env", {}) or {}
        if isinstance(perm_env, dict):
            extra.update(perm_env.get(request.permission, {}) or {})
        extra.update(request.env)
        extra.setdefault("MULTIAGENT_AGENT_ID", self.id)
        extra.setdefault("MULTIAGENT_TASK_ID", request.task_id)
        extra.setdefault("MULTIAGENT_RUN_ID", request.run_id)
        extra.setdefault("MULTIAGENT_WORKSPACE", str(request.workspace))
        return extra

    # ------------------------------------------------------------------ 检查
    def check(self, timeout: float = 20.0) -> dict[str, Any]:
        started = time.monotonic()
        resolved = self._resolve_command()
        if not resolved:
            return {"ok": False, "installed": False, "detail": explain_missing(self.agent)}
        command, prefix = resolved
        code, out, err, ms = procmod.run_command_tree(
            [command] + prefix + ["--version"], timeout=timeout
        )
        version = (out or err).strip().splitlines()
        return {
            "ok": code == 0,
            "installed": True,
            "command": command,
            "version": version[0] if version else "",
            "duration_ms": ms or int((time.monotonic() - started) * 1000),
            "detail": "" if code == 0 else f"退出码 {code}",
        }

    # ------------------------------------------------------------------ 执行
    def run(self, request: AgentRequest) -> AgentResult:
        started = time.monotonic()
        ensure_dir(request.run_dir)
        resolved = self._resolve_command()
        prompt_file = request.run_dir / "prompt.txt"
        write_text(prompt_file, request.prompt)

        if not resolved:
            return AgentResult(
                ok=False,
                exit_code=-2,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=explain_missing(self.agent),
            )
        command, prefix = resolved
        argv, stdin_text, mode = self._build_argv(
            request, command=command, prefix=prefix, prompt_file=prompt_file
        )
        env = build_env(self._env_for(request), None)
        cwd = effective_cwd(self.agent, request.workspace)
        code, out, err, ms = procmod.run_command_tree(
            argv,
            cwd=cwd,
            env=env,
            timeout=request.timeout_sec,
            stdin_text=stdin_text,
            cancel_event=request.cancel_event,
            cancel_path=request.cancel_path,
            max_output_bytes=request.max_output_bytes,
        )
        cancelled = code == procmod.CANCELLED_EXIT
        if request.on_event:
            try:
                request.on_event(
                    {"type": "cli.exit", "run_id": request.run_id, "exit_code": code, "mode": mode}
                )
            except Exception:  # noqa: BLE001 - 事件回调不应影响结果
                pass
        if cancelled:
            error = "已取消：收到人工终止信号（已回收整棵进程树）"
        elif code == 0:
            error = None
        else:
            error = err or out or f"退出码 {code}"
        return AgentResult(
            ok=code == 0,
            exit_code=code,
            duration_ms=ms or int((time.monotonic() - started) * 1000),
            stdout=out,
            stderr=err,
            error=error,
            # CLI 输出没有标准化的 token/cost 字段，不解析成本：
            # cost_source 保持 AgentResult 默认的 "unreported"（0 不等于免费）。
            transcript=[
                {
                    "type": "cli",
                    "argv": argv,
                    "mode": mode,
                    "prompt_sha256": sha256_text(request.prompt),
                    "stdout_preview": truncate(out, 4000),
                    "stderr_preview": truncate(err, 4000),
                }
            ],
        )
