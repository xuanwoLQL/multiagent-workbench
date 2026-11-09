"""ACP（Agent Client Protocol）适配器。

四家 Agent 都有标准 ACP 入口，因此用同一套协议驱动它们，无需修改 Agent 本体：
    opencode acp / codex-acp / claude-agent-acp / dsh --profile acp

协议：JSON-RPC 2.0 over stdio（换行分隔）。
实现范围：initialize / session/new / session/prompt / session/cancel，
以及服务端发来的 session/update、session/request_permission、fs/* 请求。

进程生命周期由 ``agents/proc.py`` 托管：本机 codex/claude 的 command 实际会解析成
``cmd.exe /c xxx.CMD``，真实进程树是 ``cmd.exe → node.exe``。只 terminate 直接子进程
会把 node.exe 变成孤儿（继续占端口、写文件、烧额度），所以关闭/超时/异常三条路径
统一走整树回收（Windows：Job Object + ``taskkill /F /T``；POSIX：进程组）。
"""

from __future__ import annotations

import io
import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import __version__
from ..util import ensure_dir, read_text, write_text
from . import proc as procmod
from .base import AgentAdapter, AgentRequest, AgentResult, PermissionRequest, build_env, effective_cwd
from .resolve import explain_missing, resolve_agent_argv

PROTOCOL_VERSION = 1
_SENTINEL = object()

#: 单行协议消息上限（字符）。超过则截断并丢弃该行剩余部分：
#: 单个畸形/超长行不应把内存吃穿，也不应阻塞后续消息。
_MAX_LINE_CHARS = 16 << 20

#: stderr 保留的最近行数，以及单行保留长度（诊断够用即可）。
_STDERR_KEEP = 500
_STDERR_LINE_CHARS = 1000

#: transcript 事件数上限（防止长时间运行的 Agent 把内存吃穿）。
_MAX_TRANSCRIPT_EVENTS = 20000

#: ``timeout_sec <= 0`` 视为「不限时」，用一个足够大的值代替 None。
_NO_TIMEOUT = 24 * 3600.0


class _AcpError(RuntimeError):
    pass


class _Cancelled(RuntimeError):
    """收到协作式取消信号（需求 54「终止执行」）。"""


@dataclass
class _Usage:
    """一次 session/update 里携带的用量信息。"""

    tokens: int = 0
    cumulative: bool = False  # True=总量快照（取 max），False=增量（累加）
    cost: float = 0.0
    #: 协议**确实**带回了 usage 字段（tokens/cost）时为 True。
    #: 数值为 0 的上报也算 reported；完全没带 usage 时必须为 False，
    #: 绝不把「协议没上报」写成 0 成本冒充真实数据。
    reported: bool = False


@dataclass
class _MessageBuffer:
    """把 token 级 chunk 聚合成「完整消息」。

    历史缺陷（现场故障）：每个 ``agent_message_chunk`` 都直接 append 进 ``messages``，
    最后 ``stdout = "\\n".join(messages)`` → **一个 token 一行**；而 handoff 解析把
    stdout 当正文，于是共享黑板/交接页的文字变成「一列一个字」
    （DB 里 ``handoffs.did = "我\\n先\\n检\\n查\\n..."``）。

    现在的语义：

    * ``agent_message_chunk`` → 直接拼接到当前消息缓冲（**不插换行**）；
    * ``agent_message``（完整消息）/ 工具调用 / 会话结束 → 把缓冲 flush 成一条完整消息；
    * ``stdout`` 用「完整消息之间 \\n」连接，而不是「每个 chunk 之间 \\n」；
    * thoughts 同理（``agent_thought_chunk`` 拼接、``agent_thought`` flush）。
    """

    messages: list[str] = field(default_factory=list)
    thoughts: list[str] = field(default_factory=list)
    message_parts: list[str] = field(default_factory=list)
    thought_parts: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 消息
    def add_message_chunk(self, text: str) -> None:
        if text:
            self.message_parts.append(text)

    def add_message(self, text: str) -> None:
        """完整消息：先 flush 未收尾的 chunk 缓冲，再把完整文本作为一条消息。"""
        self.flush_message()
        if text:
            self.messages.append(text)

    def flush_message(self) -> None:
        if self.message_parts:
            self.messages.append("".join(self.message_parts))
            self.message_parts.clear()

    # ------------------------------------------------------------------ 思考
    def add_thought_chunk(self, text: str) -> None:
        if text:
            self.thought_parts.append(text)

    def add_thought(self, text: str) -> None:
        self.flush_thought()
        if text:
            self.thoughts.append(text)

    def flush_thought(self) -> None:
        if self.thought_parts:
            self.thoughts.append("".join(self.thought_parts))
            self.thought_parts.clear()

    # ------------------------------------------------------------------ 收尾
    def flush_all(self) -> None:
        self.flush_message()
        self.flush_thought()


class _Session:
    """一次 ACP 子进程会话。

    管道**一律用二进制**（``text=False``），由本类自己做解码：

    * ``stdout``：逐行字节读取 → :func:`procmod.decode_output`（UTF-8 → 本地代码页 → replace）
      再 ``json.loads``；
    * ``stderr``：同样逐行字节读取 + ``decode_output``。``text=True`` 时管道解码器在
      ``PYTHONUTF8=1`` 下是**严格 UTF-8**，中文 Windows 上 ACP 服务端的 GBK 诊断输出会
      让 reader 线程抛 ``UnicodeDecodeError``（线程静默死掉，stderr 永久为空，还可能让
      管道缓冲区填满反过来卡死子进程）；
    * ``stdin``：套一层显式 UTF-8 的 ``TextIOWrapper``（ACP/JSON-RPC 是 UTF-8 协议，
      ``newline="\\n"`` 保证不把换行翻译成 CRLF，帧分隔才是干净的 LF）。
    """

    def __init__(
        self,
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        transcript: list[dict[str, Any]],
        cancel_event: "threading.Event | None" = None,
        cancel_path: Path | None = None,
    ):
        self.argv = argv
        self.transcript = transcript
        self.cancel_event = cancel_event
        self.cancel_path = cancel_path
        self.proc = procmod.spawn(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            bufsize=-1,
        )
        # 二进制 stdin → 显式 UTF-8 文本层（write_through：不做文本层缓冲，flush 即落管道）
        self._stdin: io.TextIOWrapper | None = (
            io.TextIOWrapper(
                self.proc.stdin,
                encoding="utf-8",
                errors="replace",
                newline="\n",
                write_through=True,
            )
            if self.proc.stdin is not None
            else None
        )
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._next_id = 0
        self._lock = threading.Lock()
        self._closed = False
        self.stderr_lines: list[str] = []
        self._threads = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    # ------------------------------------------------------------ IO 线程
    def _read_stdout(self) -> None:
        stream = self.proc.stdout
        try:
            if stream is None:
                return
            for raw in procmod.read_byte_lines(stream, _MAX_LINE_CHARS):
                line = procmod.decode_output(raw).strip()
                if not line:
                    continue
                try:
                    self._queue.put(json.loads(line))
                except json.JSONDecodeError:
                    self._queue.put({"__raw__": line})
        except (ValueError, OSError):
            pass
        finally:
            self._queue.put(_SENTINEL)

    def _read_stderr(self) -> None:
        """持续读取 stderr（**按字节**读取，逐行 ``decode_output``）。

        必须「一直读」：agent 往 stderr 灌大量内容时，管道缓冲区填满会让它阻塞在
        write 上，而我们又在等它写 stdout —— 典型的互相死锁。
        保留量有上限（最近 500 行 × 每行 1000 字符），避免长时间运行吃内存。
        解码走「UTF-8 严格 → 本地代码页 → GBK → replace」：GBK 诊断输出不乱码，
        reader 线程也绝不会因为解码失败而崩掉。
        """
        stream = self.proc.stderr
        try:
            if stream is None:
                return
            for raw in procmod.read_byte_lines(stream, _STDERR_LINE_CHARS):
                line = procmod.decode_output(raw).rstrip("\r\n")
                if len(line) > _STDERR_LINE_CHARS:
                    line = line[:_STDERR_LINE_CHARS] + "…[单行已截断]"
                self.stderr_lines.append(line)
                if len(self.stderr_lines) > _STDERR_KEEP:
                    del self.stderr_lines[: len(self.stderr_lines) - _STDERR_KEEP]
        except (ValueError, OSError):
            pass

    # ------------------------------------------------------------ 发送
    def send(self, obj: dict[str, Any]) -> None:
        stream = self._stdin
        if stream is None or self._closed:
            raise _AcpError("ACP 进程 stdin 已关闭")
        payload = json.dumps(obj, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                stream.write(payload)
                stream.flush()
            except (BrokenPipeError, ValueError) as exc:
                raise _AcpError(f"ACP 进程已退出，无法写入（{type(exc).__name__}）") from exc
            except OSError as exc:
                raise _AcpError(f"ACP 写入失败：{exc}") from exc

    def send_request(self, method: str, params: dict[str, Any] | None) -> int:
        self._next_id += 1
        request_id = self._next_id
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return request_id

    def notify(self, method: str, params: dict[str, Any] | None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def reply(self, request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        self.send(payload)

    def next_message(self, timeout: float) -> Any:
        """取一条服务端消息。

        带 ``cancel_event`` 时用 ``CANCEL_POLL_SECONDS`` 的小片轮询（保证 200ms 级的
        「终止执行」响应），但**不会**把整体 timeout 拉长；不带 event 时行为与历史
        版本一致（一次阻塞式 get）。
        """
        event = self.cancel_event
        deadline = time.monotonic() + timeout
        while True:
            if procmod.cancel_requested(event, self.cancel_path):
                raise _Cancelled("收到人工终止信号")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("ACP 等待超时")
            polling = event is not None or self.cancel_path is not None
            slice_ = min(procmod.CANCEL_POLL_SECONDS, left) if polling else left
            try:
                return self._queue.get(timeout=max(0.01, slice_))
            except queue.Empty:
                if not polling:
                    raise TimeoutError("ACP 等待超时")
                continue

    # ------------------------------------------------------------ 关闭
    def close(self, *, wait_natural: float | None = None) -> int:
        """回收整棵进程树并返回直接子进程的退出码（幂等）。

        ``wait_natural``：关闭 stdin 之后再等它自然退出多久。
        默认走正常收尾语义（``GRACE_SECONDS``）；**已经确认卡死**（超时/取消）的
        会话要传 ``0``，否则超时不再是硬上限（真实耗时 = 设定超时 + grace）。
        """
        if self._closed:
            return self.proc.returncode if self.proc.returncode is not None else -1
        self._closed = True
        # 先关文本层（= 关闭底层 stdin 管道，给子进程 EOF），再整树回收
        stream, self._stdin = self._stdin, None
        if stream is not None:
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        kwargs = {} if wait_natural is None else {"wait_natural": wait_natural}
        code = procmod.terminate_tree(self.proc, **kwargs)
        for thread in self._threads:
            thread.join(timeout=2.0)
        return code if code is not None else -1


class AcpAdapter(AgentAdapter):
    protocol = "acp"

    def __init__(
        self,
        agent: dict[str, Any],
        resolver: Callable[[dict[str, Any]], tuple[str, list[str]] | None] | None = None,
    ):
        self.id = agent.get("id", "unknown")
        self.label = agent.get("label") or self.id
        self.agent = agent
        self.resolver = resolver or (lambda a: resolve_agent_argv(a))

    # ------------------------------------------------------------------ 描述
    def describe(self) -> dict[str, Any]:
        resolved = self.resolver(self.agent)
        return {
            "id": self.id,
            "label": self.label,
            "protocol": self.protocol,
            "command": resolved[0] if resolved else self.agent.get("command"),
            "args": resolved[1] if resolved else self.agent.get("args"),
            "roles": self.agent.get("roles", []),
            "strengths": self.agent.get("strengths", []),
            "can_write": self.agent.get("can_write", True),
            "quota": self.agent.get("quota", {}),
            # ACP v1 没有经过验证的「请求级 model」字段：模型由 Agent 自身配置决定。
            # 这里只暴露语义，绝不在 initialize/session/new/session/prompt 里塞未验证字段。
            "model_control": "agent-config",
        }

    # ------------------------------------------------------------------ 检查
    def check(self, timeout: float = 20.0) -> dict[str, Any]:
        """握手探测（``/api/agents/{id}/check`` 用，契约要求整条链路 ≤10s）。

        探测是**有硬上限**的路径：握手失败/超时说明进程已经卡住，收尾时不能再温柔等
        ``GRACE_SECONDS``（否则「10s 超时」实际会变成 10s + 3s）。成功路径才给宽限。
        """
        started = time.monotonic()
        resolved = self.resolver(self.agent)
        if not resolved:
            return {"ok": False, "installed": False, "detail": explain_missing(self.agent)}
        command, args = resolved
        transcript: list[dict[str, Any]] = []
        session = _Session([command] + args, Path.cwd(), build_env(self.agent.get("env", {}), None), transcript)
        hung = False
        info: dict[str, Any] | None = None
        failure: str | None = None
        try:
            info = self._initialize(session, timeout)
        except Exception as exc:  # noqa: BLE001 - 探测不应抛出
            hung = True
            failure = str(exc)
        finally:
            session.close(wait_natural=0.0 if hung else None)

        # 同样要等 close()（join reader 线程）之后再读 stderr_lines，否则诊断可能还没读出来。
        if failure is not None:
            return {
                "ok": False,
                "installed": True,
                "command": command,
                "args": args,
                "handshake": "failed",
                "detail": failure,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "stderr": "\n".join(session.stderr_lines[-10:]),
            }
        agent_info = (info or {}).get("agentInfo") or (info or {}).get("agent_info") or {}
        return {
            "ok": True,
            "installed": True,
            "command": command,
            "args": args,
            "handshake": "ok",
            "agent_info": agent_info,
            "protocol_version": (info or {}).get("protocolVersion") or (info or {}).get("protocol_version"),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }

    # ------------------------------------------------------------------ 执行
    def run(self, request: AgentRequest) -> AgentResult:
        started = time.monotonic()
        ensure_dir(request.run_dir)
        prompt_file = request.run_dir / "prompt.txt"
        write_text(prompt_file, request.prompt)

        resolved = self.resolver(self.agent)
        if not resolved:
            return AgentResult(
                ok=False,
                exit_code=-2,
                duration_ms=int((time.monotonic() - started) * 1000),
                error=explain_missing(self.agent),
            )
        command, args = resolved

        transcript: list[dict[str, Any]] = []
        extra_env = dict(self.agent.get("env", {}) or {})
        permission_env = self.agent.get("permission_env", {}) or {}
        if isinstance(permission_env, dict):
            extra_env.update(permission_env.get(request.permission, {}) or {})
        extra_env.update(request.env)
        extra_env.setdefault("MULTIAGENT_AGENT_ID", self.id)
        extra_env.setdefault("MULTIAGENT_TASK_ID", request.task_id)
        extra_env.setdefault("MULTIAGENT_RUN_ID", request.run_id)
        env = build_env(extra_env, None)

        # timeout_sec <= 0 表示「不限时」，不能被 max(5, ...) 悄悄压成 5 秒。
        deadline = (
            time.monotonic() + float(request.timeout_sec)
            if request.timeout_sec and request.timeout_sec > 0
            else None
        )
        cwd = effective_cwd(self.agent, request.workspace)
        session = _Session(
            [command] + args,
            cwd,
            env,
            transcript,
            cancel_event=request.cancel_event,
            cancel_path=request.cancel_path,
        )
        messages: list[str] = []
        thoughts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        usage = _Usage()
        # token 级 chunk → 完整消息的聚合缓冲（stdout/handoff 用，见 _MessageBuffer）。
        # messages/thoughts 是「缓冲为 None 时」的兜底容器，实际写入都走 message_buffer。
        message_buffer = _MessageBuffer()
        session_id: str | None = None
        error: str | None = None
        stop_reason: str | None = None
        completed = False
        timed_out = False
        cancelled = False
        # 正常协议循环结束时的结果（None = 走了异常/失败收尾）。
        # **关键：try 里绝不 return** —— 结果对象只能在 ``session.close()`` 之后构造，
        # 因为 close() 才会 join stdout/stderr 的 reader 线程；提前构造会读到「还没读出来」
        # 的 stderr（全量/慢机器下诊断就丢了：GBK stderr 回归测试偶发 result.stderr 为空）。
        outcome_ok: bool | None = None
        outcome_exit_code = 0
        code: int | None = None

        def _result(ok: bool, exit_code: int, err: str | None) -> AgentResult:
            return AgentResult(
                ok=ok,
                exit_code=exit_code,
                duration_ms=int((time.monotonic() - started) * 1000),
                # 完整消息之间才用 \n：绝不能对每个 token 级 chunk 都 join 一次
                # （那会让 handoff / 黑板正文变成「一列一个字」）
                stdout="\n".join(message_buffer.messages),
                stderr="\n".join(session.stderr_lines[-200:]),
                error=err,
                cost_tokens=usage.tokens,
                cost_amount=usage.cost,
                # 只有协议真的带回 usage 才算 reported；没带回时保持 unreported，
                # 不把 0 冒充成「免费」或真实成本。
                cost_source="reported" if usage.reported else "unreported",
                transcript=transcript,
                session_ref=session_id,
                messages=list(message_buffer.messages),
            )

        try:
            self._initialize(session, self._remaining(deadline))
            session_id = self._new_session(session, cwd, self._remaining(deadline))
            if request.on_event:
                request.on_event({"type": "acp.session", "session_id": session_id, "run_id": request.run_id})

            prompt_id = session.send_request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": request.prompt}],
                },
            )
            while True:
                message = session.next_message(self._remaining(deadline))
                if message is _SENTINEL:
                    raise _AcpError("ACP 进程提前退出")
                self._absorb(usage, self._handle_message(
                    session, message, request, messages, thoughts, tool_calls, transcript,
                    buffer=message_buffer,
                ))
                # 是否为 session/prompt 的响应
                if isinstance(message, dict) and message.get("id") == prompt_id and "method" not in message:
                    if "error" in message:
                        error = self._format_rpc_error(message["error"])
                    else:
                        result = message.get("result")
                        stop_reason = result.get("stopReason") if isinstance(result, dict) else None
                    completed = True
                    break
            if stop_reason == "cancelled":
                error = error or "会话被取消"
            elif stop_reason == "refusal":
                error = error or "Agent 拒绝执行"

            outcome_exit_code = 0 if not error else 1
            outcome_ok = outcome_exit_code == 0
        except _Cancelled as exc:
            cancelled = True
            error = f"已取消：{exc}"
        except TimeoutError as exc:
            timed_out = True
            error = f"超时：{exc}"
        except _AcpError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 兜底，保证调用方拿到结构化失败
            error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                if session_id and not completed:
                    # 只在「没正常跑完」时打断 agent；正常 end_turn 之后再发
                    # session/cancel 会污染真实 Agent 的会话语义。
                    session.notify("session/cancel", {"sessionId": session_id})
            except Exception:  # noqa: BLE001
                pass
            try:
                if session_id:
                    session.notify("session/close", {"sessionId": session_id})
            except Exception:  # noqa: BLE001
                pass
            # 超时/取消路径已知卡死：跳过温柔等待，直接整树回收，保证「终止执行」立即生效。
            force = timed_out or cancelled
            code = session.close(wait_natural=0.0) if force else session.close()

        # 走到这里 session.close() 已执行完（含 join reader 线程），stderr_lines 才是完整的。
        # 同时把最后一段没等到「完整消息/工具调用」边界的 chunk 缓冲收尾，
        # 否则最后一句会丢（Agent 正常 end_turn 时通常没有后续 update 来触发 flush）。
        message_buffer.flush_all()
        if outcome_ok is not None:
            return _result(outcome_ok, outcome_exit_code, error)

        exit_code = procmod.CANCELLED_EXIT if cancelled else (code if code else 1)
        return _result(False, exit_code, error or "ACP 会话失败")

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _remaining(deadline: float | None) -> float:
        if deadline is None:
            return _NO_TIMEOUT
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("整体超时")
        return left

    @staticmethod
    def _format_rpc_error(error: Any) -> str:
        if isinstance(error, dict):
            message = error.get("message") or json.dumps(error, ensure_ascii=False)
            code = error.get("code")
            return f"{message}（code={code}）" if code is not None else str(message)
        return str(error)

    @staticmethod
    def _absorb(target: _Usage, incoming: _Usage | None) -> None:
        """合并用量：总量型取 max（避免累计上报被反复相加），增量型累加。"""
        if incoming is None:
            return
        target.reported = target.reported or incoming.reported
        if incoming.tokens:
            if incoming.cumulative:
                target.tokens = max(target.tokens, incoming.tokens)
            else:
                target.tokens += incoming.tokens
        if incoming.cost:
            target.cost = max(target.cost, incoming.cost)

    def _initialize(self, session: _Session, timeout: float) -> dict[str, Any]:
        request_id = session.send_request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "multiagent", "title": "多 Agent 协作工作台", "version": __version__},
            },
        )
        return self._await_response(session, request_id, timeout)

    def _new_session(self, session: _Session, cwd: Path, timeout: float) -> str:
        request_id = session.send_request(
            "session/new",
            {"cwd": str(cwd), "mcpServers": []},
        )
        result = self._await_response(session, request_id, timeout)
        session_id = result.get("sessionId") or result.get("session_id")
        if not session_id:
            raise _AcpError("session/new 未返回 sessionId")
        return str(session_id)

    def _await_response(self, session: _Session, request_id: int, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待 ACP 响应超时")
            message = session.next_message(remaining)
            if message is _SENTINEL:
                raise _AcpError("ACP 进程提前退出")
            if isinstance(message, dict) and message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise _AcpError(self._format_rpc_error(message["error"]))
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            self._handle_message(session, message, None, [], [], [], None)

    def _handle_message(
        self,
        session: _Session,
        message: Any,
        request: AgentRequest | None,
        messages: list[str],
        thoughts: list[str],
        tool_calls: list[dict[str, Any]],
        transcript: list[dict[str, Any]] | None = None,
        *,
        buffer: "_MessageBuffer | None" = None,
    ) -> _Usage:
        """处理服务端消息；返回本次携带的用量（无则全 0）。"""
        if not isinstance(message, dict) or "method" not in message:
            return _Usage()
        method = message.get("method")
        raw_params = message.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        msg_id = message.get("id")

        if method == "session/update":
            return self._handle_update(params, messages, thoughts, tool_calls, request, transcript, buffer=buffer)

        if method == "session/request_permission":
            allowed = False
            if request and request.approve:
                tool_call = params.get("toolCall")
                if not isinstance(tool_call, dict):
                    tool_call = {}
                kind = self._classify_tool(tool_call)
                allowed = bool(
                    request.approve(
                        PermissionRequest(
                            run_id=request.run_id,
                            agent_id=self.id,
                            kind=kind,
                            title=str(tool_call.get("title") or tool_call.get("kind") or "工具调用"),
                            detail=str(tool_call.get("rawInput") or tool_call.get("rawOutput") or ""),
                            raw=tool_call,
                        )
                    )
                )
            options = params.get("options")
            option_id = self._pick_option(options if isinstance(options, list) else [], allowed)
            try:
                if allowed and option_id:
                    session.reply(msg_id, {"outcome": {"outcome": "selected", "optionId": option_id}})
                else:
                    session.reply(msg_id, {"outcome": {"outcome": "cancelled"}})
            except _AcpError:
                pass
            tool_calls.append({"type": "permission", "allowed": allowed, "option": option_id})
            if transcript is not None:
                transcript.append({"type": "permission", "allowed": allowed, "toolCall": params.get("toolCall")})
            return _Usage()

        if method == "fs/read_text_file":
            path = str(params.get("path", ""))
            self._safe_reply(session, msg_id, {"content": read_text(path)})
            return _Usage()
        if method == "fs/write_text_file":
            path = str(params.get("path", ""))
            try:
                write_text(path, str(params.get("content", "")))
                self._safe_reply(session, msg_id, {})
            except OSError as exc:
                self._safe_reply(session, msg_id, None, {"code": -32000, "message": str(exc)})
            return _Usage()
        if method in ("terminal/create", "terminal/output", "terminal/wait_for_exit", "terminal/kill", "terminal/release"):
            self._safe_reply(session, msg_id, None, {"code": -32601, "message": f"未启用的能力：{method}"})
            return _Usage()

        # 未知请求：回 method-not-found；未知通知：忽略
        if msg_id is not None:
            self._safe_reply(session, msg_id, None, {"code": -32601, "message": f"未实现的方法：{method}"})
        return _Usage()

    @staticmethod
    def _safe_reply(session: _Session, msg_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
        """进程已退出时回复失败不应把整次运行带崩。"""
        try:
            session.reply(msg_id, result, error)
        except _AcpError:
            pass

    def _handle_update(
        self,
        params: dict[str, Any],
        messages: list[str],
        thoughts: list[str],
        tool_calls: list[dict[str, Any]],
        request: AgentRequest | None,
        transcript: list[dict[str, Any]] | None = None,
        *,
        buffer: "_MessageBuffer | None" = None,
    ) -> _Usage:
        update = params.get("update")
        if not isinstance(update, dict):
            return _Usage()
        kind = update.get("sessionUpdate") or update.get("session_update")
        usage = _Usage(
            tokens=self._extract_tokens(update),
            cumulative=self._tokens_are_cumulative(update),
            cost=self._extract_cost(update),
            reported=self._usage_reported(update),
        )
        if transcript is not None and len(transcript) < _MAX_TRANSCRIPT_EVENTS:
            transcript.append({"type": "update", "kind": kind, "update": update})
        elif transcript is not None:
            # 只记录一次「已截断」标记，避免每条 update 都重复追加
            if not transcript or transcript[-1].get("type") != "transcript_truncated":
                transcript.append(
                    {
                        "type": "transcript_truncated",
                        "limit": _MAX_TRANSCRIPT_EVENTS,
                        "at": time.time(),
                    }
                )
        text = self._content_text(update)
        if request and request.on_event:
            payload: dict[str, Any] = {"type": "acp.update", "kind": kind, "run_id": request.run_id}
            if text:
                payload["text"] = text
            if kind in ("tool_call", "tool_call_update", "tool_call_progress"):
                # 前端靠这些字段渲染工具卡：只给 tool_call_id 的话，进度/结果更新
                # 无法与标题、输入、输出、文件位置关联（线上实测 244 条 tool_call_update
                # 只有 tool_call_id，卡片只能显示一个空壳）。
                payload.update(self._tool_fields(update))
            try:
                request.on_event(payload)
            except Exception:  # noqa: BLE001 - 事件回调不应影响协议循环
                pass

        if kind in ("agent_message_chunk", "agent_message"):
            if buffer is not None:
                if kind == "agent_message_chunk":
                    buffer.add_message_chunk(text)
                else:
                    # 完整消息：把之前累积的 chunk 收尾成一条，再追加这条完整消息
                    buffer.add_message(text)
            elif text:
                messages.append(text)
        elif kind in ("agent_thought_chunk", "agent_thought"):
            if buffer is not None:
                if kind == "agent_thought_chunk":
                    buffer.add_thought_chunk(text)
                else:
                    buffer.add_thought(text)
            elif text:
                thoughts.append(text)
        elif kind in ("tool_call", "tool_call_update", "tool_call_progress"):
            # 工具调用是消息边界：先把手上的 chunk 收尾成一条完整消息，
            # 否则「调用前的半句 + 调用后的半句」会被拼成一条错位消息。
            if buffer is not None:
                buffer.flush_message()
            tool_calls.append(update)
        return usage

    @staticmethod
    def _content_text(update: dict[str, Any]) -> str:
        """尽力取出文本：兼容 ContentBlock / 纯字符串 / ContentBlock 数组。"""
        content = update.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, dict):
            return str(content.get("text") or "")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    nested = item.get("content")
                    if isinstance(nested, dict):
                        parts.append(str(nested.get("text") or ""))
                    elif isinstance(nested, str):
                        parts.append(nested)
                    else:
                        parts.append(str(item.get("text") or ""))
            return "".join(parts)
        return str(update.get("text") or "")

    # ------------------------------------------------------------------ 工具卡字段
    @classmethod
    def _tool_fields(cls, update: dict[str, Any]) -> dict[str, Any]:
        """把 ACP ``tool_call`` / ``tool_call_update`` 的完整信息带进事件。

        前端靠这些字段渲染工具卡；只给 ``tool_call_id`` 的话，进度/结果更新无法与
        标题、输入、输出、文件位置关联，卡片只能显示一个空壳。字段对齐 ACP 规范
        （camelCase；部分实现用 snake_case，两种都认）：

        ``tool_title`` (title) / ``tool_kind`` (kind) / ``status`` /
        ``tool_call_id`` (toolCallId) / ``tool_input`` (rawInput) /
        ``tool_output`` (rawOutput) / ``tool_content`` (content 文本块数组) /
        ``locations``（文件位置数组）。

        ``tool`` 保留为兼容字段（旧前端只读它），取值优先级 title → toolName → name → kind。
        """
        fields: dict[str, Any] = {}
        title = update.get("title")
        tool_kind = update.get("kind")
        call_id = update.get("toolCallId") or update.get("tool_call_id")
        status = update.get("status")
        tool = title or update.get("toolName") or update.get("name") or tool_kind
        if tool:
            fields["tool"] = str(tool)
        if title:
            fields["tool_title"] = str(title)
        if tool_kind:
            fields["tool_kind"] = str(tool_kind)
        if call_id:
            fields["tool_call_id"] = str(call_id)
        if status:
            fields["status"] = str(status)
        raw_input = update.get("rawInput", update.get("raw_input"))
        if raw_input is not None:
            fields["tool_input"] = raw_input
        raw_output = update.get("rawOutput", update.get("raw_output"))
        if raw_output is not None:
            fields["tool_output"] = raw_output
        content = cls._tool_content(update)
        if content:
            fields["tool_content"] = content
        locations = update.get("locations")
        if isinstance(locations, list) and locations:
            fields["locations"] = locations
        return fields

    @staticmethod
    def _tool_content(update: dict[str, Any]) -> list[str]:
        """提取 tool content 的可读文本块（数组；无内容返回 ``[]``）。

        ACP 的 content 可能是字符串、ContentBlock、或 ContentBlock 数组；逐个取出文本，
        非文本块（图片等）忽略。保持「总是数组」的形状，前端渲染不用分支。
        """
        content = update.get("content")
        if content is None:
            return []
        if isinstance(content, str):
            return [content] if content else []
        if isinstance(content, dict):
            return _block_texts([content])
        if isinstance(content, list):
            return _block_texts(content)
        return []

    @staticmethod
    def _usage_sources(update: dict[str, Any]) -> list[dict[str, Any]]:
        sources: list[dict[str, Any]] = []
        for key in ("usage", "usageUpdate", "usage_update"):
            value = update.get(key)
            if isinstance(value, dict):
                sources.append(value)
        sources.append(update)
        return sources

    @staticmethod
    def _as_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        return value if isinstance(value, int) else None

    @classmethod
    def _extract_tokens(cls, update: dict[str, Any]) -> int:
        """从 session/update 里尽力提取 token 用量。

        覆盖三种真实形态：
        - ``usage.totalTokens`` / ``total_tokens``（累计总量）
        - ``used`` / ``size``（ACP 规范 ``usage_update``，真实 opencode 用的就是这个）
        - ``inputTokens`` + ``outputTokens``（增量）
        """
        for source in cls._usage_sources(update):
            for key in ("totalTokens", "total_tokens", "used", "tokensUsed", "tokens_used"):
                value = cls._as_int(source.get(key))
                if value is not None:
                    return value
            subtotal = 0
            found = False
            for key in ("inputTokens", "outputTokens", "input_tokens", "output_tokens"):
                value = cls._as_int(source.get(key))
                if value is not None:
                    subtotal += value
                    found = True
            if found:
                return subtotal
        return 0

    @classmethod
    def _tokens_are_cumulative(cls, update: dict[str, Any]) -> bool:
        """``True`` 表示这是个「总量快照」，只能取 max，不能累加。"""
        for source in cls._usage_sources(update):
            for key in ("totalTokens", "total_tokens", "used", "tokensUsed", "tokens_used"):
                if cls._as_int(source.get(key)) is not None:
                    return True
        return False

    @classmethod
    def _extract_cost(cls, update: dict[str, Any]) -> float:
        for source in cls._usage_sources(update):
            cost = source.get("cost")
            if isinstance(cost, dict):
                amount = cost.get("amount")
                if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                    return float(amount)
            for key in ("costAmount", "cost_amount"):
                amount = source.get(key)
                if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                    return float(amount)
        return 0.0

    @classmethod
    def _usage_reported(cls, update: dict[str, Any]) -> bool:
        """协议是否**确实**带回了 usage（tokens/cost）。

        只认 :meth:`_usage_sources` 里的字段形状（与 ``_extract_tokens`` /
        ``_extract_cost`` 完全一致），避免把普通消息里的同名字段误当成用量。
        显式上报 0 也算 ``reported`` —— 那是「真实为 0」，不是「没上报」。
        """
        token_keys = (
            "totalTokens", "total_tokens", "used", "tokensUsed", "tokens_used",
            "inputTokens", "outputTokens", "input_tokens", "output_tokens",
        )
        for source in cls._usage_sources(update):
            for key in token_keys:
                if cls._as_int(source.get(key)) is not None:
                    return True
            cost = source.get("cost")
            if isinstance(cost, dict):
                amount = cost.get("amount")
                if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                    return True
            for key in ("costAmount", "cost_amount"):
                amount = source.get(key)
                if isinstance(amount, (int, float)) and not isinstance(amount, bool):
                    return True
        return False

    @staticmethod
    def _classify_tool(tool_call: dict[str, Any]) -> str:
        kind = str(tool_call.get("kind") or "").lower()
        if kind in ("edit", "write", "create", "delete", "move"):
            return "write"
        if kind in ("execute", "terminal", "run"):
            return "execute"
        if kind in ("read", "search", "fetch"):
            return "read"
        title = str(tool_call.get("title") or "").lower()
        for token, mapped in (("write", "write"), ("edit", "write"), ("delete", "write"),
                              ("exec", "execute"), ("command", "execute"), ("bash", "execute")):
            if token in title:
                return mapped
        return "other"

    @staticmethod
    def _pick_option(options: list[dict[str, Any]], allowed: bool) -> str | None:
        want = ("allow",) if allowed else ("reject", "deny")
        for option in options:
            kind = str(option.get("kind") or "").lower()
            if kind.startswith(want):
                return str(option.get("optionId") or option.get("id") or "")
        if allowed:
            for option in options:
                kind = str(option.get("kind") or "").lower()
                if "allow" in kind:
                    return str(option.get("optionId") or option.get("id") or "")
        return None


def _block_texts(items: list[Any]) -> list[str]:
    """从 ContentBlock 列表里提取文本块（兼容 ``{content: {text}}`` 与 ``{text}`` 两种形态）。"""
    texts: list[str] = []
    for item in items:
        if isinstance(item, str):
            if item:
                texts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        nested = item.get("content")
        if isinstance(nested, dict):
            text = nested.get("text")
            if isinstance(text, str) and text:
                texts.append(text)
            continue
        if isinstance(nested, str):
            if nested:
                texts.append(nested)
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
    return texts
