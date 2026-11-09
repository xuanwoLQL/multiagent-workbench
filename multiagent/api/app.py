"""HTTP / WebSocket API（需求 52）。

REST 覆盖项目、任务、黑板、交接、审批、观测；WebSocket 推送事件与进度，
供 Web 看板实时刷新。CLI 与 API 共用同一套服务对象。

桌面指挥台（``docs/UI-OPS-REDESIGN.md``）新增的聚合入口：

* ``GET  /api/projects/{pid}/dashboard`` —— 首屏一次拿全（不 spawn 任何探测子进程）；
* ``POST /api/projects/{pid}/quick-task`` —— 一句话建任务 / 拆三步计划；
* ``PATCH /api/projects/{pid}`` —— 改名 / 改显示名（修正历史乱码名称）。

安全边界（见 :mod:`multiagent.api.security`）：

* 默认 bind ``127.0.0.1``：零配置可用（桌面壳/浏览器看板同源直连），
  但仍校验 ``Host``（DNS rebinding）与 ``Origin``（跨站）；
* bind 非 loopback 或开启严格模式：``/api/**`` 强制 Bearer token；
* 文件类接口（fs/list、import、config 文件名、runs 输出）先 ``resolve``
  规范化再做目录边界校验；
* 统一错误信封 ``{"error", "detail", "error_code"}``，5xx 不回显堆栈与绝对路径。
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..agents.base import create_adapter
from ..agents.resolve import resolve_agent_argv
from ..app import Workbench
from ..config import CONFIG_FILES, Config, validate_config
from ..logging_setup import bind_context, new_request_id, reset_context
from ..metrics import collect as collect_metrics
from .. import __version__
from ..orchestrator.engine import MergeRetryBlocked
from ..util import loads, now_iso, read_json, write_json
from .dashboard import build_dashboard
from .quicktask import (
    QuickTaskError,
    create_quick_tasks,
    parse_quick_task,
    response_payload,
)
from ..tasks.decompose import build_project_plan
from ..tasks.planner import apply_plan as create_task_plan
from ..tasks.profiles import infer_verify_command, is_exit_zero_only, prepare_creation_fields
from ..workspace.projects import ProjectRootConflict
from .security import (
    CSP,
    SecurityError,
    SecurityPolicy,
    build_policy,
    extract_token,
    extract_ws_token,
    startup_banner,
    validate_stored_path,
)
from ..stream import StreamCoalescer
from .taskworkspace import SyncBlocked, describe_task_workspace, sync_task_workspace
from ..workspace.hygiene import scan_workspace

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
LOGGER = logging.getLogger("multiagent.api")

_CONFIG_NAME = re.compile(r"^[A-Za-z0-9_-]+\.json$")
_BLOB_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|access[_-]?key|private[_-]?key|authorization)$",
    re.I,
)
_SECRET_MASK = "••••••"
_STATUS_CODES = {
    400: "invalid_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "invalid_request",
    500: "internal_error",
}

#: 请求体上限（20MB）：看板/配置/任务 JSON 远小于此，超大 body 直接 413，
#: 避免一个畸形请求把进程内存吃穿。
_MAX_BODY_BYTES = 20 * 1024 * 1024


class EventHub:
    """进程内实时事件总线：把执行线程里的 Agent 事件推给所有 WebSocket 订阅者。

    执行器在工作线程里跑，WebSocket 在事件循环里跑，所以用 ``call_soon_threadsafe``
    把事件投递到订阅队列；同时保留每项目的环形缓冲，供新连接回放（需求 46 / 55）。

    缓冲容量默认 4000：进总线前已经过 :class:`~multiagent.api.stream.StreamCoalescer`
    合并（token 级 chunk → ~180ms 的块），4000 条等价于改动前几十倍的原始事件量，
    新连接回放能覆盖更长的历史。
    """

    def __init__(self, buffer: int = 4000) -> None:
        self._lock = threading.Lock()
        self._subs: set[asyncio.Queue] = set()
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._buffer = buffer
        self._seq = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            item = {"seq": self._seq, "at": now_iso(), **(event or {})}
            key = str(item.get("project_id") or "")
            buf = self._buffers.setdefault(key, [])
            buf.append(item)
            if len(buf) > self._buffer:
                del buf[: len(buf) - self._buffer]
            subs = list(self._subs)
            loop = self._loop
        if loop is None:
            return
        for queue in subs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, item)
            except RuntimeError:
                continue

    def snapshot(self, project_id: str | None) -> list[dict[str, Any]]:
        with self._lock:
            if project_id:
                return list(self._buffers.get(project_id, []))
            merged: list[dict[str, Any]] = []
            for buf in self._buffers.values():
                merged.extend(buf)
            merged.sort(key=lambda e: e.get("seq", 0))
            return merged[-self._buffer:]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=4000)
        with self._lock:
            self._subs.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs.discard(queue)


_TASK_EDITABLE = {
    "title", "kind", "brief", "tags", "role", "template_id", "agent_id", "priority",
    "max_attempts", "timeout_sec", "isolation", "read_only", "write_scope", "acceptance",
    "acceptance_ref", "inputs", "outputs", "workdir", "ordering",
}


def _reload_config(app: Workbench) -> None:
    """从磁盘重读配置并原地刷新（改配置无需重启进程）。"""
    fresh = Config.load(app.config.home)
    for attr in ("agents_raw", "templates_raw", "routing_raw", "acceptance_raw", "policy_raw", "warnings", "errors"):
        setattr(app.config, attr, getattr(fresh, attr))
    # 适配器缓存里保存的是旧命令/参数；配置保存后必须让下一次调用重新解析，
    # 否则「保存即生效」对 Agent 清单是假的。
    try:
        app.runner._adapters.clear()  # noqa: SLF001 - 配置热重载的内部协作
    except AttributeError:
        pass


# --------------------------------------------------------------------------- 通用


def _error_response(
    status: int,
    message: str,
    code: str | None = None,
    *,
    details: Any = None,
) -> JSONResponse:
    """统一错误信封；保留 ``detail`` 兼容既有前端与调用方。"""
    body: dict[str, Any] = {
        "error": message,
        "detail": message,
        "error_code": code or _STATUS_CODES.get(status, "error"),
    }
    if details is not None:
        body["details"] = details
    return JSONResponse(body, status_code=status, headers=dict(_SECURITY_HEADERS))


def _apply_security_headers(response: Any, path: str) -> None:
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    # API 响应绝不缓存（任务/审批/事件都是实时数据）；
    # 静态资源（index.html、/static/*）用 no-cache：可以存，但每次必须回源校验，
    # 这样开发期改前端文件刷新即生效，又不会像 no-store 那样完全禁止缓存。
    if path == "/api" or path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif path == "/" or path.startswith("/static"):
        response.headers.setdefault("Cache-Control", "no-cache")


_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": CSP,
}


def _safe_config_name(name: str) -> str:
    """配置文件名的白名单校验（即使路由参数被编码绕过也拒绝分隔符/``..``）。"""
    text = (name or "").strip()
    # 允许客户端省略扩展名（"policy" → "policy.json"）；仍然走同一套白名单。
    if text and "." not in text:
        text = f"{text}.json"
    if len(text) > 64 or not _CONFIG_NAME.fullmatch(text) or ".." in text:
        raise HTTPException(400, f"非法的配置文件名：{name}")
    return text


def _redact_secrets(value: Any) -> Any:
    """深拷贝并遮蔽配置里的凭据值（只影响 API 响应，不改磁盘文件）。"""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, str) and item and _SECRET_KEY.search(str(key)):
                result[key] = _SECRET_MASK
            else:
                result[key] = _redact_secrets(item)
        return result
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return value


def _merge_redacted(new: Any, old: Any) -> Any:
    """把响应里的 ``••••••`` 还原成磁盘上的真实值（保存时不会把密钥写成掩码）。"""
    if new == _SECRET_MASK:
        return old
    if isinstance(new, dict) and isinstance(old, dict):
        merged = {key: _merge_redacted(value, old.get(key)) for key, value in new.items()}
        for key, value in old.items():
            merged.setdefault(key, value)
        return merged
    if isinstance(new, list) and isinstance(old, list):
        result = []
        for index, item in enumerate(new):
            result.append(_merge_redacted(item, old[index]) if index < len(old) else item)
        return result
    return new


def _as_flag(value: Any) -> bool:
    """宽松布尔：只有真正的 ``true`` / ``1`` / ``yes`` / ``on`` 才算「显式要求」。

    用于 ``force`` 这类破坏性开关 —— JSON 里的字符串 ``"false"`` 绝不能因为非空而被当成真。
    """
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


#: ``/api/agents/{id}/check`` 的硬超时（契约 §5.4：整条链路 ≤10s）。
#: 留 1s 给「超时后的整树回收」，保证端到端不超过 10 秒。
_AGENT_CHECK_TIMEOUT_SEC = 9.0


def _agent_detail(agent: dict[str, Any], usage: dict[str, Any]) -> dict[str, Any]:
    """单个 Agent 的详情（命令解析失败也不抛：解析不到就是 ``command=None`` + ``error``）。"""
    # ``quota.summary()`` 返回的是 {usage:{...}, limits:{...}}；旧调用方也可能
    # 直接传扁平用量。两种形状都接受，否则 Agent 页会永远显示 0 次调用。
    if isinstance(usage, dict) and isinstance(usage.get("usage"), dict):
        usage = usage["usage"]
    error: str | None = None
    if agent.get("protocol") == "human":
        command, argv = "(human)", []
    else:
        try:
            resolved = resolve_agent_argv(agent)
        except Exception as exc:  # noqa: BLE001 - 解析失败不应让控制台整体报错
            LOGGER.exception("解析 Agent %s 的命令失败", agent.get("id"))
            resolved = None
            error = f"{type(exc).__name__}: {exc}"
        command, argv = (resolved[0], resolved[1]) if resolved else (None, [])
    item = {
        "id": agent.get("id"),
        "label": agent.get("label"),
        "protocol": agent.get("protocol"),
        "enabled": agent.get("enabled", True),
        "command": command,
        "args": list(argv or []),
        "cwd": agent.get("cwd"),
        "roles": list(agent.get("roles") or []),
        "strengths": list(agent.get("strengths") or []),
        "can_write": agent.get("can_write", True),
        "quota": dict(agent.get("quota") or {}),
        "usage": {
            "invocations": usage.get("invocations", 0),
            "seconds": usage.get("seconds", 0),
            "cost": usage.get("cost", 0),
            "tokens": usage.get("tokens", 0),
        },
    }
    if error:
        item["error"] = error
    return item


def _agent_detail_fallback(agent: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    """解析彻底失败时的降级记录：形状与正常记录一致，外加 ``error`` 说明原因。"""
    return {
        "id": agent.get("id"),
        "label": agent.get("label"),
        "protocol": agent.get("protocol"),
        "enabled": agent.get("enabled", True),
        "command": None,
        "args": [],
        "cwd": agent.get("cwd"),
        "roles": list(agent.get("roles") or []),
        "strengths": list(agent.get("strengths") or []),
        "can_write": agent.get("can_write", True),
        "quota": dict(agent.get("quota") or {}),
        "usage": {"invocations": 0, "seconds": 0, "cost": 0, "tokens": 0},
        "error": f"{type(exc).__name__}: {exc}",
    }


# --------------------------------------------------------------------------- 上下文


@dataclass
class ApiContext:
    """一次 ``create_api`` 期间共享的状态（事件总线 / 后台调度表 / 安全策略）。"""

    app: Workbench
    security: SecurityPolicy
    hub: EventHub = field(default_factory=EventHub)
    background: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: runner.event_sink → coalescer → hub.publish（token 级 chunk 合并成块）
    coalescer: StreamCoalescer | None = None

    def __post_init__(self) -> None:
        # 执行器的事件先由 RunEventStore 的 StreamCoalescer 合并（同一份合并器也负责落库），
        # 再把「已经合并好的块」直接发布到本进程 EventHub。**不要二次合并**：否则
        # chunk_count 会被覆盖成 1，实时页看起来像「一条块里只有一个 token」。
        self.coalescer = self.app.run_events.coalescer
        self.app.run_events.subscribe(self.hub.publish)

    # ------------------------------------------------------------- 查找
    def project(self, pid: str) -> dict[str, Any]:
        try:
            return self.app.projects.get(pid)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    def task(self, tid: str) -> dict[str, Any]:
        task = self.app.tasks.find(tid)
        if not task:
            raise HTTPException(404, f"任务不存在：{tid}")
        return task

    # ------------------------------------------------------------- 路径
    def resolved_dir(self, raw: str, *, label: str = "目录") -> Path:
        """``expanduser`` + ``resolve``（跟随符号链接、消 ``..``）后做边界校验。"""
        text = (raw or "").strip()
        if "\x00" in text:
            raise HTTPException(400, f"{label}路径无效")
        try:
            path = Path(text).expanduser().resolve() if text else Path.home().resolve()
        except (OSError, ValueError) as exc:
            raise HTTPException(400, f"路径无效：{raw}") from exc
        self.security.assert_path(path, kind=label)
        return path


# --------------------------------------------------------------------------- 中间件


def _ensure_json_charset(response) -> None:  # noqa: ANN001
    """给 ``application/json`` 补上 ``charset=utf-8``。

    浏览器 ``fetch().json()`` 默认按 UTF-8 解码，但 PowerShell ``Invoke-RestMethod``、
    部分企业 HTTP 客户端在没有 charset 时会按 ISO-8859-1 解码，中文全变乱码。
    这是跨客户端兼容性问题，不是数据库编码问题。
    """
    content_type = response.headers.get("content-type")
    if content_type and content_type.split(";", 1)[0].strip().lower() == "application/json":
        if "charset=" not in content_type.lower():
            response.headers["content-type"] = "application/json; charset=utf-8"


def _iso_ago(seconds: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=max(0.0, float(seconds)))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _run_health(app) -> dict[str, int]:
    """运行中调用的健康度：区分「进程没心跳」与「Agent 长时间没有产出」。"""
    heartbeat_cutoff = _iso_ago(60)
    activity_cutoff = _iso_ago(30 * 60)
    try:
        heartbeat = app.db.query_one(
            "SELECT COUNT(*) AS c FROM runs WHERE status = 'running' "
            "AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
            (heartbeat_cutoff,),
        )
        activity = app.db.query_one(
            "SELECT COUNT(*) AS c FROM runs WHERE status = 'running' "
            "AND (last_activity_at IS NULL OR last_activity_at < ?)",
            (activity_cutoff,),
        )
        running = app.db.query_one("SELECT COUNT(*) AS c FROM runs WHERE status = 'running'")
    except Exception:  # noqa: BLE001 - 老库可能还没有新列
        return {"running": 0, "stalled": 0, "silent": 0}
    return {
        "running": int(running["c"] if running else 0),
        "stalled": int(heartbeat["c"] if heartbeat else 0),
        "silent": int(activity["c"] if activity else 0),
    }


def _install_http_middleware(api: FastAPI, ctx: ApiContext) -> None:
    policy = ctx.security

    @api.middleware("http")
    async def _guard(request: Request, call_next):  # noqa: ANN001, ANN202
        path = request.url.path
        request_id = (request.headers.get("x-request-id") or "").strip()[:128] or new_request_id()
        request.state.request_id = request_id
        log_token = bind_context(request_id=request_id)
        started = time.monotonic()
        status_code = 500
        try:
            content_length = request.headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > _MAX_BODY_BYTES:
                response = _error_response(413, "请求体过大（上限 20MB）", "payload_too_large")
                response.headers.setdefault("X-Request-ID", request_id)
                _ensure_json_charset(response)
                status_code = 413
                return response
            response = await _guarded_call(request, call_next, path, policy)
            response.headers.setdefault("X-Request-ID", request_id)
            _ensure_json_charset(response)
            status_code = response.status_code
            return response
        finally:
            duration_ms = int((time.monotonic() - started) * 1000)
            LOGGER.info(
                "http.request",
                extra={
                    "method": request.method,
                    "path": path,
                    "duration_ms": duration_ms,
                    "status": status_code,
                },
            )
            reset_context(log_token)

    async def _guarded_call(request: Request, call_next, path: str, policy: SecurityPolicy):  # noqa: ANN001, ANN202
        guarded = path == "/api" or path.startswith("/api/")
        host = request.headers.get("host")
        # 1) Host 校验：DNS rebinding 的唯一可靠识别点（浏览器同源策略此时已失效）
        if not policy.host_ok(host):
            return _error_response(400, "非法的 Host 请求头（已拒绝 DNS rebinding 访问）", "bad_host")
        request.state.authenticated = True
        if guarded:
            origin = request.headers.get("origin")
            explicit = bool(origin) and origin.strip() in policy.allow_origins
            # 2) 跨站请求：浏览器对跨站 POST/fetch 必带 Origin
            if not explicit and not policy.origin_ok(origin, host):
                return _error_response(403, "已拒绝跨站请求（Origin 与本站不一致）", "cross_origin")
            # 2b) 没有 Origin 的跨站请求（img/script/表单 GET）用 Fetch Metadata 兜底
            if not explicit and (request.headers.get("sec-fetch-site") or "").strip().lower() == "cross-site":
                return _error_response(403, "已拒绝跨站请求（Sec-Fetch-Site: cross-site）", "cross_origin")
            # 3) 令牌校验（OPTIONS 预检与 /api/health 例外，后者降级为最小响应）
            token_ok = policy.token_ok(extract_token(request.headers, request.query_params))
            request.state.authenticated = token_ok
            if policy.require_token and not token_ok and request.method != "OPTIONS" and path != "/api/health":
                return _error_response(401, "缺少或无效的 Bearer token", "unauthorized")
        response = await call_next(request)
        _apply_security_headers(response, path)
        return response


def _install_error_handlers(api: FastAPI, ctx: ApiContext) -> None:
    @api.exception_handler(SecurityError)
    async def _security_error(_request: Request, exc: SecurityError) -> JSONResponse:  # noqa: ANN001
        return _error_response(exc.status, str(exc), exc.code)

    @api.exception_handler(StarletteHTTPException)
    async def _http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:  # noqa: ANN001
        detail = exc.detail
        message = detail if isinstance(detail, str) else "请求失败"
        return _error_response(exc.status_code, message, details=None if isinstance(detail, str) else detail)

    @api.exception_handler(KeyError)
    async def _key_error(_request: Request, exc: KeyError) -> JSONResponse:  # noqa: ANN001
        # 各服务用 KeyError(中文说明) 表达「对象不存在」
        message = str(exc.args[0]) if exc.args else "对象不存在"
        return _error_response(404, message, "not_found")

    @api.exception_handler(ValueError)
    async def _value_error(_request: Request, exc: ValueError) -> JSONResponse:  # noqa: ANN001
        # 领域校验（DAG 成环、跨项目依赖、非法配置等）统一 400，避免变成 500
        return _error_response(400, str(exc) or "请求不合法", "invalid_request")

    @api.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:  # noqa: ANN001
        details = [
            {"loc": list(err.get("loc") or []), "msg": err.get("msg"), "type": err.get("type")}
            for err in exc.errors()
        ]
        return _error_response(422, "请求参数不合法", "invalid_request", details=details)

    @api.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:  # noqa: ANN001
        # 堆栈只写服务端日志，不回显给客户端（避免泄漏绝对路径与内部结构）
        LOGGER.exception("未处理的服务端异常 %s %s", request.method, request.url.path)
        return _error_response(500, "内部服务错误（详情见服务端日志）", "internal_error")


# --------------------------------------------------------------------------- 路由


def _register_base(api: FastAPI, ctx: ApiContext) -> None:
    app = ctx.app

    @api.get("/api/health")
    def health(request: Request, deep: bool = False) -> dict[str, Any]:
        if ctx.security.require_token and not getattr(request.state, "authenticated", False):
            # 存活探针不需要凭据，但未鉴权时不暴露主目录与 Agent 清单
            return {"ok": True, "auth_required": True}
        payload: dict[str, Any] = {
            "ok": True,
            "version": __version__,
            "uptime_seconds": round(time.time() - float(getattr(app, "started_at", time.time())), 3),
            "schema_version": app.db.schema_version,
            "home": str(app.config.home),
            "agents": app.config.agent_ids(),
            "security": ctx.security.summary(),
        }
        if deep:
            try:
                payload["database"] = app.db.integrity_check(full=True)
            except Exception as exc:  # noqa: BLE001 - 健康检查失败要报告而不是 500
                payload["ok"] = False
                payload["database"] = {"ok": False, "error": str(exc)}
            try:
                import shutil as _shutil

                usage = _shutil.disk_usage(str(app.config.home))
                payload["disk"] = {
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "free_ratio": round(usage.free / usage.total, 4) if usage.total else 0,
                }
                if usage.free < 512 * 1024 * 1024:
                    payload["ok"] = False
                    payload["warning"] = "磁盘剩余空间低于 512MB"
            except OSError:
                pass
            try:
                payload["scheduler"] = app.slots.counts()
                payload["running"] = ctx.background
                runs = _run_health(app)
                payload["runs"] = runs
                if runs["stalled"]:
                    payload["ok"] = False
                    payload["warning"] = (
                        f"{runs['stalled']} 个运行中调用超过 60s 没有心跳（进程可能卡死）"
                    )
                elif runs["silent"]:
                    payload.setdefault("warnings", []).append(
                        f"{runs['silent']} 个运行中调用超过 30 分钟没有产出（模型可能长时间思考）"
                    )
            except Exception:  # noqa: BLE001
                pass
        return payload

    @api.get("/api/metrics", response_class=PlainTextResponse)
    def metrics() -> PlainTextResponse:
        """Prometheus 文本指标（需与其它 /api 接口相同的鉴权）。"""
        return PlainTextResponse(
            collect_metrics(app, api_state=api.state),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @api.get("/api/system/storage")
    def system_storage() -> dict[str, Any]:
        return app.ops.storage()

    @api.get("/api/system/backups")
    def system_backups() -> list[dict[str, Any]]:
        return app.ops.list_backups()

    @api.post("/api/system/backup")
    def system_backup(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        label = str(payload.get("label") or "manual")[:80]
        return app.ops.create_backup(label, actor="human")

    @api.post("/api/system/maintenance")
    def system_maintenance(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """保留清理。默认 dry-run；真正执行必须显式 ``{"apply": true, "confirm": "APPLY"}``。"""
        apply = _as_flag(payload.get("apply"))
        if apply and str(payload.get("confirm") or "") != "APPLY":
            raise HTTPException(400, "真正执行清理需要 confirm=APPLY（防止误删）")
        return app.ops.prune(
            dry_run=not apply,
            keep_runs_per_task=int(payload.get("keep_runs_per_task", 3)),
            keep_days=float(payload.get("keep_days", 30)),
            keep_snapshots=int(payload.get("keep_snapshots", 20)),
            keep_backups=int(payload.get("keep_backups", 20)),
            prune_workspaces_days=float(payload.get("prune_workspaces_days", 30)),
            prune_blobs=not bool(payload.get("no_blobs")),
        )

    @api.get("/api/system/verify")
    def system_verify(full: bool = False) -> dict[str, Any]:
        return app.ops.verify(full=full)

    @api.get("/api/system/logs")
    def system_logs(lines: int = 200) -> dict[str, Any]:
        """读取工作台结构化日志尾部（只允许读配置的日志文件）。"""
        from ..logging_setup import log_path

        path = log_path()
        if path is None or not path.is_file():
            return {"path": None, "lines": []}
        limit = max(1, min(int(lines), 2000))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(500, f"读取日志失败：{exc}") from exc
        tail = text.splitlines()[-limit:]
        return {"path": str(path), "lines": tail}

    @api.get("/api/fs/list")
    def fs_list(path: str = Query("")) -> dict[str, Any]:
        """列出目录（供导入工作区时选择路径）；只读，不动任何文件。

        安全：先 ``resolve`` 归一化（跟随符号链接、消 ``..``），再禁止访问工作台
        私有数据/配置目录；可用 ``MULTIAGENT_FS_ROOTS`` 进一步收窄范围。
        """
        base = ctx.resolved_dir(path, label="目录")
        if not base.is_dir():
            raise HTTPException(400, f"不是目录：{base}")
        dirs: list[dict[str, str]] = []
        try:
            entries = sorted(base.iterdir(), key=lambda p: p.name.lower())
        except PermissionError as exc:
            raise HTTPException(403, "无权限读取该目录") from exc
        for child in entries:
            if child.name.startswith("."):
                continue
            try:
                if child.is_dir():
                    dirs.append({"name": child.name, "path": str(child)})
            except OSError:
                continue
        parent = base.parent if base != base.parent else None
        return {"path": str(base), "parent": str(parent) if parent else None, "dirs": dirs}

    @api.get("/api/config")
    def config() -> dict[str, Any]:
        return app.config.summary()

    @api.get("/api/config/files")
    def config_files() -> dict[str, Any]:
        files: dict[str, Any] = {}
        for name in CONFIG_FILES:
            path = app.config.config_dir / name
            raw = read_json(path, {}) if path.exists() else None
            files[name] = _redact_secrets(raw) if raw is not None else None
        return {"dir": str(app.config.config_dir), "files": files, "secrets_redacted": True}

    @api.get("/api/config/files/{name}")
    def config_read(name: str) -> dict[str, Any]:
        safe = _safe_config_name(name)
        if safe not in CONFIG_FILES and f"{safe}.json" in CONFIG_FILES:
            safe = f"{safe}.json"
        if safe not in CONFIG_FILES:
            raise HTTPException(400, f"不支持的配置文件：{name}")
        path = app.config.config_dir / safe
        raw = read_json(path, None) if path.exists() else None
        return {
            "name": safe,
            "content": _redact_secrets(raw) if raw is not None else None,
            "secrets_redacted": True,
        }

    @api.put("/api/config/files/{name}")
    def config_write(name: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        safe = _safe_config_name(name)
        if safe not in CONFIG_FILES and f"{safe}.json" in CONFIG_FILES:
            safe = f"{safe}.json"
        if safe not in CONFIG_FILES:
            raise HTTPException(400, f"不支持的配置文件：{name}")
        content = payload.get("content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise HTTPException(400, f"JSON 解析失败：{exc}") from exc
        if not isinstance(content, dict):
            raise HTTPException(400, "配置内容必须是 JSON 对象")
        path = app.config.config_dir / safe
        old = read_json(path, {}) if path.exists() else {}
        content = _merge_redacted(content, old)
        errors = validate_config(safe, content)
        if errors:
            raise HTTPException(400, "配置校验失败：" + "；".join(errors[:8]))
        # 边界即「CONFIG_FILES 白名单 + 文件名正则」：只可能落在 config_dir 下的 5 个已知文件
        write_json(path, content)
        _reload_config(app)
        try:
            app.board.append_event(
                project_id=None,
                kind="config.write",
                summary=f"更新配置文件：{safe}",
                actor="human",
                data={"file": safe},
            )
        except Exception:  # noqa: BLE001 - 审计失败不能让保存失败
            LOGGER.exception("记录 config.write 事件失败")
        return {"saved": safe, "config": app.config.summary()}

    @api.post("/api/config/reload")
    def config_reload() -> dict[str, Any]:
        _reload_config(app)
        return app.config.summary()

    @api.get("/api/agents")
    def agents() -> list[dict[str, Any]]:
        return app.quota.summary()

    @api.get("/api/agents/detail")
    def agents_detail() -> list[dict[str, Any]]:
        """Agent 清单 + 命令解析 + 用量。

        契约 §5.4：**任何单个 Agent 的解析失败都不能让整个接口 500**。历史故障：
        ``reg.exe`` 的 GBK 输出让 ``text=True`` 的 reader 线程崩掉、``stdout`` 变成
        ``None``，上层 ``None.splitlines()`` 直接把控制台首屏打成 500。这里额度汇总与
        每个 Agent 的解析各自兜底：失败的那一项降级成带 ``error`` 的记录，其余照常返回。
        """
        try:
            quota = {
                row.get("agent_id"): row
                for row in app.quota.summary()
                if isinstance(row, dict)
            }
        except Exception:  # noqa: BLE001 - 额度表异常不应让控制台整体报错
            LOGGER.exception("Agent 额度汇总失败，已降级为空")
            quota = {}
        items: list[dict[str, Any]] = []
        for agent in app.config.agents:
            try:
                items.append(_agent_detail(agent, quota.get(agent.get("id"), {})))
            except Exception as exc:  # noqa: BLE001 - 单个 Agent 解析失败不影响整体
                LOGGER.exception("解析 Agent %s 失败", agent.get("id"))
                items.append(_agent_detail_fallback(agent, exc))
        return items

    @api.post("/api/agents/{agent_id}/check")
    def agent_check(agent_id: str) -> dict[str, Any]:
        """探测某个 Agent 是否可执行。

        契约 §5.4：超时 ≤10s；探测失败返回结构化结果而不是 500；``sync def`` 由
        FastAPI 放进线程池执行，不阻塞其它请求。
        """
        agent = app.config.agent(agent_id)
        if not agent:
            raise HTTPException(404, f"未配置的 Agent：{agent_id}")
        try:
            adapter = create_adapter(agent, resolve_agent_argv)
            return adapter.check(timeout=_AGENT_CHECK_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 - 探测异常降级为结果
            LOGGER.exception("探测 Agent %s 失败", agent_id)
            return {
                "ok": False,
                "installed": False,
                "detail": f"探测失败：{type(exc).__name__}: {exc}",
            }


# --------------------------------------------------------------------------- 后台调度


def _task_project(app: Workbench, task: dict[str, Any]) -> dict[str, Any]:
    """任务所属项目（项目已被删除但任务行残留时返回 404，而不是 500）。"""
    try:
        return app.projects.get(task["project_id"])
    except LookupError as exc:
        raise HTTPException(404, f"任务所属项目不存在：{task['project_id']}") from exc


def _start_project_run(
    ctx: ApiContext, project: dict[str, Any], payload: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """在后台线程里跑 ``engine.run_project``（``/run`` 与 ``quick-task?start=true`` 共用）。

    同一个项目同时只允许一个后台调度：``ctx.background`` 里的 ``running`` 既是幂等闸门，
    也是 ``/run-status`` 与 dashboard ``running`` 字段的数据源。线程是 daemon：
    进程退出不会因为调度线程而挂住。
    """
    key = project["id"]
    if (ctx.background.get(key) or {}).get("running"):
        return {"accepted": False, "message": "该项目已有调度在运行"}
    options = dict(payload or {})
    state: dict[str, Any] = {"running": True, "started_at": now_iso(), "summary": None}
    ctx.background[key] = state

    def _run() -> None:
        try:
            state["summary"] = ctx.app.engine.run_project(
                project["id"],
                only=options.get("only"),
                auto_retry=options.get("auto_retry", True),
                max_parallel=options.get("max_parallel"),
            )
        except Exception:  # noqa: BLE001 - 后台线程里的异常只记日志，不能静默吞掉
            LOGGER.exception("项目 %s 后台调度失败", key)
        finally:
            state["running"] = False

    threading.Thread(target=_run, daemon=True, name=f"run-project-{key}").start()
    return {"accepted": True, "project_id": project["id"], "message": "已在后台开始调度"}


def _project_root_conflict(exc: "ProjectRootConflict") -> JSONResponse:
    """409：同一目录已登记为项目。返回已存在项目 ID，前端可直接打开。"""
    project = dict(getattr(exc, "project", None) or {})
    summary = {
        key: project.get(key)
        for key in ("id", "name", "label", "root_path")
        if project.get(key) is not None
    }
    return JSONResponse(
        status_code=409,
        content={
            "error": "project_root_exists",
            "message": str(exc),
            "existing_project_id": project.get("id"),
            "existing_project": summary,
            "root_path": getattr(exc, "root_path", None),
        },
    )


def _register_projects(api: FastAPI, ctx: ApiContext) -> None:
    app = ctx.app

    @api.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        return app.projects.list()

    @api.post("/api/projects")
    def create_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "项目名必填")
        root_path = payload.get("root_path")
        if root_path:
            root_path = str(ctx.resolved_dir(str(root_path), label="工作目录"))
        try:
            project = app.projects.create(name, label=payload.get("label", ""), root_path=root_path)
        except ProjectRootConflict as exc:
            return _project_root_conflict(exc)
        app.projects.write_meta(project)
        return project

    @api.post("/api/projects/import")
    def import_project(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        raw = str(payload.get("path") or "").strip()
        if not raw:
            raise HTTPException(400, "path 必填")
        source = ctx.resolved_dir(raw, label="导入目录")
        if not source.is_dir():
            raise HTTPException(400, f"导入目录不存在或不是目录：{source}")
        try:
            project = app.projects.import_dir(str(source), name=payload.get("name"))
        except ProjectRootConflict as exc:
            return _project_root_conflict(exc)
        app.projects.write_meta(project)
        return project

    @api.delete("/api/projects/{pid}")
    def delete_project(pid: str, force: bool = False) -> dict[str, Any]:
        project = ctx.project(pid)
        running = app.engine._running_run_ids(project_id=project["id"])  # noqa: SLF001 - 同进程内一致性检查
        if running and not force:
            raise HTTPException(
                409,
                f"项目仍有 {len(running)} 个运行中的 Agent 调用；请先停止，或使用 ?force=true 强制删除",
            )
        if running and force:
            app.engine.stop_project(project["id"], "项目被强制删除")
        return app.projects.delete(project["id"])

    @api.get("/api/projects/{pid}")
    def get_project(pid: str) -> dict[str, Any]:
        project = ctx.project(pid)
        return {"project": project, "progress": app.tasks.progress(project["id"])}

    @api.patch("/api/projects/{pid}")
    def update_project(pid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """改名 / 改显示名（契约 §3.3）：用于修正历史乱码名称，不重建项目、不动工作区。

        ``name`` 是全库唯一标识（创建/导入时用它判重），``label`` 只是显示名。
        """
        project = ctx.project(pid)
        patch: dict[str, Any] = {}
        if "name" in payload:
            name = str(payload.get("name") or "").strip()
            if not name:
                raise HTTPException(400, "项目名不能为空")
            if len(name) > 200:
                raise HTTPException(400, "项目名过长（上限 200 字符）")
            if name != project["name"]:
                clash = app.db.query_one(
                    "SELECT id FROM projects WHERE name = ? AND id <> ?", (name, project["id"])
                )
                if clash:
                    raise HTTPException(409, f"项目名已被占用：{name}")
                patch["name"] = name
        if "label" in payload:
            label = str(payload.get("label") or "").strip()
            if len(label) > 200:
                raise HTTPException(400, "显示名过长（上限 200 字符）")
            # 空 label 回退成项目名（与 ProjectService.create 的默认一致，避免界面出现空标题）
            patch["label"] = label or patch.get("name") or project["name"]
        if not patch:
            return project
        patch["updated_at"] = now_iso()
        app.db.update("projects", {"id": project["id"]}, patch)
        updated = app.projects.get(project["id"])
        try:
            app.projects.write_meta(updated)
        except OSError:  # 工作区只读/被占用时元信息写不进去，但不该让改名失败
            LOGGER.warning("写入 .multiagent/project.json 失败（改名已生效）", exc_info=True)
        return updated

    @api.get("/api/projects/{pid}/dashboard")
    def dashboard(pid: str) -> dict[str, Any]:
        """指挥台首屏聚合（契约 §3.1）：一次请求拿到 project/progress/tasks/agents/
        approvals/stats/activity/running/workspace。

        约束：**不调用任何会 spawn 子进程的探测逻辑**（Agent 数据只来自配置 + usage 表），
        100 任务规模单请求 ≤150ms（见 ``api/dashboard.py`` 的聚合 SQL 与预算遍历）。
        """
        project = ctx.project(pid)
        state = ctx.background.get(project["id"]) or {}
        return build_dashboard(app, project, running=bool(state.get("running")))

    @api.post("/api/projects/{pid}/quick-task")
    def quick_task(pid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """一句话建任务（契约 §3.2）：single=1 个任务；plan=调研→初稿→审阅 三步 DAG。

        ``text`` 是用户原话（原样进 brief），标题取首行截 60 字；``kind=auto`` 按关键词推断；
        验收默认「有产物 → file_exists，无产物 → exit_zero」；``start=true`` 时复用
        ``/api/projects/{pid}/run`` 的后台调度路径（同一把「已在运行」闸门）。
        """
        project = ctx.project(pid)
        try:
            spec = parse_quick_task(payload, config=app.config)
            if spec["kind"] == "code" and not spec["verify_command"]:
                inferred = infer_verify_command(project["root_path"])
                if inferred:
                    spec["verify_command"] = inferred
                    spec["warnings"].append(f"未提供 verify_command，已自动使用：{inferred}")
            if spec["mode"] == "project":
                plan = build_project_plan(
                    app.config,
                    project["root_path"],
                    spec["text"],
                    title=spec["title"],
                    source_files=spec["source_files"],
                    agent_override=spec["agent_id"],
                )
                hygiene = scan_workspace(
                    project["root_path"],
                    config=app.config,
                    max_entries=int(app.config.policy_get("workspace.hygiene_max_entries", 20000) or 20000),
                )
                warnings = [*spec["warnings"], *plan.get("warnings", [])]
                if hygiene.get("temporary_files"):
                    warnings.append(
                        f"工作区发现 {hygiene['temporary_files']} 个临时文件；最终 QA 会要求清理。"
                    )
                plan["hygiene"] = hygiene
                plan["warnings"] = warnings
                if spec["preview"]:
                    return {
                        "project_id": project["id"],
                        "mode": "project",
                        "preview": True,
                        "started": False,
                        "warnings": warnings,
                        "tasks": [
                            {
                                "id": None,
                                "key": item.get("key"),
                                "title": item.get("title"),
                                "kind": item.get("kind"),
                                "agent_id": item.get("agent"),
                                "timeout_sec": item.get("timeout_sec"),
                                "max_attempts": item.get("max_attempts"),
                            }
                            for item in plan["tasks"]
                        ],
                        "plan": plan,
                        "message": "已生成九阶段 DAG 预览；请确认/调整后再应用，不会自动启动巨型任务。",
                    }
                created = create_task_plan(app.tasks, project["id"], plan)
            else:
                created = create_quick_tasks(app.tasks, project["id"], spec)
        except QuickTaskError as exc:
            raise HTTPException(400, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        note = ""
        started = False
        if spec["start"]:
            outcome = _start_project_run(ctx, project)
            started = bool(outcome.get("accepted"))
            if not started:
                note = outcome.get("message") or "已有调度在运行"
        if created:
            try:
                app.board.append_event(
                    project_id=project["id"],
                    kind="quicktask.created",
                    summary=f"一句话建任务：{created[0]['title']}（{spec['mode']}，{len(created)} 个）",
                    actor="human",
                    task_id=created[0]["id"],
                    data={
                        "mode": spec["mode"],
                        "kind": spec["kind"],
                        "started": started,
                        "warnings": spec["warnings"],
                    },
                )
            except Exception:  # noqa: BLE001 - 事件写失败不能让已创建的任务变成 500
                LOGGER.exception("记录 quick-task 事件失败")
        return response_payload(
            project["id"],
            spec["mode"],
            created,
            started=started,
            note=note,
            warnings=spec["warnings"],
        )

    @api.post("/api/projects/{pid}/decompose/preview")
    def decompose_preview(pid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """预览大型项目九阶段 DAG，不创建、不启动任何任务。"""
        project = ctx.project(pid)
        raw = dict(payload)
        raw.setdefault("text", payload.get("brief") or payload.get("goal"))
        raw["mode"] = "project"
        raw["preview"] = True
        raw["start"] = False
        try:
            spec = parse_quick_task(raw, config=app.config)
        except QuickTaskError as exc:
            raise HTTPException(400, str(exc)) from exc
        plan = build_project_plan(
            app.config,
            project["root_path"],
            spec["text"],
            title=spec["title"],
            source_files=spec["source_files"],
            agent_override=spec["agent_id"],
        )
        hygiene = scan_workspace(
            project["root_path"],
            config=app.config,
            max_entries=int(app.config.policy_get("workspace.hygiene_max_entries", 20000) or 20000),
        )
        warnings = [*spec["warnings"], *plan.get("warnings", [])]
        if hygiene.get("temporary_files"):
            warnings.append(
                f"工作区发现 {hygiene['temporary_files']} 个临时文件；最终 QA 会要求清理。"
            )
        plan["hygiene"] = hygiene
        plan["warnings"] = warnings
        return {
            "project_id": project["id"],
            "mode": "project",
            "preview": True,
            "warnings": warnings,
            "hygiene": hygiene,
            "plan": plan,
        }

    @api.post("/api/projects/{pid}/decompose/apply")
    def decompose_apply(pid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """应用预览出来的 DAG（允许用户先编辑 plan 再提交）。"""
        project = ctx.project(pid)
        candidate = payload.get("plan") if isinstance(payload.get("plan"), dict) else payload
        if not isinstance(candidate, dict) or not isinstance(candidate.get("tasks"), list):
            raise HTTPException(400, "decompose/apply 需要包含 tasks 数组的 plan")
        try:
            created = create_task_plan(app.tasks, project["id"], candidate)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            app.board.append_event(
                project_id=project["id"],
                kind="decompose.applied",
                summary=f"应用大型项目 DAG：{len(created)} 个任务",
                actor="human",
                task_id=created[0]["id"] if created else None,
                data={"tasks": len(created)},
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("记录 decompose.apply 事件失败")
        started = False
        if payload.get("start"):
            outcome = _start_project_run(ctx, project, payload.get("run") or {})
            started = bool(outcome.get("accepted"))
        return {
            "project_id": project["id"],
            "mode": "project",
            "started": started,
            "created": len(created),
            "tasks": [
                {
                    "id": task["id"],
                    "title": task["title"],
                    "status": task["status"],
                    "agent_id": task.get("agent_id"),
                    "timeout_sec": task.get("timeout_sec"),
                    "max_attempts": task.get("max_attempts"),
                }
                for task in created
            ],
            "message": f"已创建 {len(created)} 个任务"
            + ("，并已开始调度" if started else "；尚未启动，可在看板调整后启动"),
        }

    @api.get("/api/projects/{pid}/hygiene")
    def project_hygiene(pid: str) -> dict[str, Any]:
        project = ctx.project(pid)
        return scan_workspace(
            project["root_path"],
            config=app.config,
            max_entries=int(app.config.policy_get("workspace.hygiene_max_entries", 20000) or 20000),
        )

    @api.get("/api/projects/{pid}/progress")
    def progress(pid: str) -> dict[str, Any]:
        return app.tasks.progress(ctx.project(pid)["id"])

    @api.get("/api/projects/{pid}/kanban")
    def kanban(pid: str) -> dict[str, Any]:
        project = ctx.project(pid)
        plan = app.tasks.plan(project["id"])
        plan["progress"] = app.tasks.progress(project["id"])
        plan["pending_approvals"] = app.guard.pending_count(project["id"])
        return plan

    @api.get("/api/projects/{pid}/artifacts")
    def project_artifacts(pid: str, role: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        """项目级产物列表（需求 18：输入/输出产物）。"""
        return app.tasks.artifacts(project_id=ctx.project(pid)["id"], role=role, task_id=task_id)

    # ------------------------------------------------------------ 快照
    @api.post("/api/projects/{pid}/snapshots")
    def create_snapshot(pid: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        project = ctx.project(pid)
        snap = app.snapshots.create(project["id"], project["root_path"], label=payload.get("label", ""))
        try:
            app.board.append_event(
                project_id=project["id"],
                kind="snapshot.create",
                summary=f"创建快照 {snap['id']}",
                actor="human",
                data={"snapshot_id": snap["id"], "label": snap.get("label")},
            )
        except Exception:  # noqa: BLE001
            LOGGER.exception("记录 snapshot.create 事件失败")
        return snap

    @api.get("/api/projects/{pid}/snapshots")
    def list_snapshots(pid: str) -> list[dict[str, Any]]:
        return app.snapshots.list(ctx.project(pid)["id"])

    @api.post("/api/snapshots/{sid}/restore")
    def restore_snapshot(sid: str, force: bool = False) -> dict[str, Any]:
        snap = app.snapshots.get(sid)
        project = app.projects.get(snap["project_id"])
        running = app.engine._running_run_ids(project_id=project["id"])  # noqa: SLF001
        if running and not force:
            raise HTTPException(
                409,
                f"项目仍有 {len(running)} 个运行中的 Agent 调用；回退快照会覆盖它们的工作，请先停止或使用 ?force=true",
            )
        if running and force:
            app.engine.stop_project(project["id"], "回退快照前强制停止")
        return app.snapshots.restore(sid, project["root_path"], project_id=project["id"])

    @api.get("/api/snapshots/{sid}/diff")
    def diff_snapshot(sid: str) -> dict[str, Any]:
        snap = app.snapshots.get(sid)
        project = app.projects.get(snap["project_id"])
        return app.snapshots.diff_current(sid, project["root_path"])

    @api.get("/api/snapshots/{a}/diff/{b}")
    def diff_two_snapshots(a: str, b: str) -> dict[str, Any]:
        """两个快照之间的差异（需求 11：可对比）。"""
        return app.snapshots.diff(a, b)


def _register_tasks(api: FastAPI, ctx: ApiContext) -> None:
    app = ctx.app

    @api.get("/api/projects/{pid}/tasks")
    def list_tasks(pid: str, status: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        return app.tasks.list(ctx.project(pid)["id"], status=status, kind=kind)

    @api.post("/api/projects/{pid}/tasks")
    def create_task(pid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        project = ctx.project(pid)
        title = str(payload.get("title") or "").strip()
        if not title:
            raise HTTPException(400, "标题必填")
        kind = str(payload.get("kind") or "generic")
        outputs = payload.get("outputs")
        explicit_acceptance = payload.get("acceptance")
        if explicit_acceptance is not None:
            if not isinstance(explicit_acceptance, list):
                raise HTTPException(400, "acceptance 必须是对象数组")
            if not explicit_acceptance:
                raise HTTPException(400, "acceptance 不能为空；未指定时请省略该字段由类型模板生成")
        if (
            is_exit_zero_only(explicit_acceptance)
            and not (outputs or [])
            and not bool(payload.get("confirm_exit_zero_only"))
        ):
            raise HTTPException(
                400,
                "验收条件只有 exit_zero：请提供至少一个输出产物，"
                "或传 confirm_exit_zero_only=true 明确接受执行期补加文件改动检查",
            )
        fields = prepare_creation_fields(
            app.config,
            kind,
            outputs=outputs,
            isolation=payload.get("isolation"),
            read_only=bool(payload.get("read_only")),
            write_scope=payload.get("write_scope"),
            timeout_sec=payload.get("timeout_sec"),
            max_attempts=payload.get("max_attempts"),
            acceptance=explicit_acceptance,
            acceptance_ref=payload.get("acceptance_ref"),
            verify_command=payload.get("verify_command")
            or (infer_verify_command(project["root_path"]) if kind == "code" else None),
            allow_shared_write=bool(payload.get("allow_shared_write")),
            allow_unrestricted_write=bool(payload.get("allow_unrestricted_write")),
        )
        acceptance = fields["acceptance"]
        template_id = payload.get("template_id") or payload.get("template")
        if template_id and explicit_acceptance is None and payload.get("acceptance_ref") is None:
            # 模板自己的验收规则优先；这里只补类型默认超时/重试/隔离。
            acceptance = None
            fields["context"]["acceptance_guard"] = ""
        return app.tasks.create(
            project["id"],
            title,
            kind=kind,
            brief=payload.get("brief", ""),
            template_id=template_id,
            agent_id=payload.get("agent_id"),
            role=payload.get("role"),
            isolation=fields["isolation"],
            read_only=payload.get("read_only"),
            write_scope=fields["write_scope"],
            acceptance=acceptance,
            acceptance_ref=payload.get("acceptance_ref"),
            inputs=payload.get("inputs"),
            outputs=outputs,
            depends_on=payload.get("depends_on"),
            priority=payload.get("priority", 0),
            max_attempts=fields["max_attempts"],
            timeout_sec=fields["timeout_sec"],
            tags=payload.get("tags"),
            context=fields["context"],
        )

    @api.patch("/api/tasks/{tid}")
    def edit_task(tid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        task = ctx.task(tid)
        fields = {k: v for k, v in payload.items() if k in _TASK_EDITABLE}
        if "acceptance" in fields and not fields["acceptance"]:
            raise HTTPException(400, "acceptance 不能为空；如需删除请改为类型默认验收")
        if not fields:
            return task
        return app.tasks.update(task["id"], **fields)

    @api.delete("/api/tasks/{tid}")
    def delete_task(tid: str) -> dict[str, Any]:
        task = ctx.task(tid)
        if task["status"] == "running":
            raise HTTPException(409, "任务仍在执行；请先取消，再删除")
        result = app.tasks.delete(task["id"])
        try:
            app.isolation.purge_task(task["project_id"], task["id"])
        except OSError:
            LOGGER.warning("删除任务运行目录失败（DB 行已删除）", exc_info=True)
        return result

    @api.post("/api/projects/{pid}/tasks/plan")
    def apply_plan(pid: str, payload: dict[str, Any] = Body(...)) -> list[dict[str, Any]]:
        try:
            return create_task_plan(app.tasks, ctx.project(pid)["id"], payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/tasks/{tid}")
    def get_task(tid: str) -> dict[str, Any]:
        task = ctx.task(tid)
        return {
            "task": task,
            "dependencies": app.tasks.dependencies(task["id"]),
            "history": app.tasks.history(task["id"]),
            "runs": app.audit.runs(task_id=task["id"]),
            "artifacts": app.tasks.artifacts(task_id=task["id"]),
            "handoff": app.handoffs.latest_for_task(task["id"]),
            "changes": [dict(r) for r in app.db.query("SELECT * FROM file_changes WHERE task_id = ? ORDER BY id", (task["id"],))],
        }

    @api.post("/api/tasks/{tid}/timeout")
    def extend_task_timeout(tid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """人工调整超时预算。

        正在运行的 Agent 已经把当前硬超时交给适配器；该调用对下一次尝试生效。
        这比在运行中伪造一个「已延长」的 UI 状态诚实。
        """
        task = ctx.task(tid)
        raw = payload.get("timeout_sec")
        try:
            timeout_sec = int(raw)
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, "timeout_sec 必须是整数") from exc
        if timeout_sec <= 0:
            raise HTTPException(400, "timeout_sec 必须大于 0")
        updated = app.tasks.update(task["id"], timeout_sec=timeout_sec)
        return {
            "task": updated,
            "applies_to": "next_attempt" if task["status"] == "running" else "next_run",
            "message": (
                "任务正在运行；新的超时预算已保存，将从下一次尝试生效。"
                if task["status"] == "running"
                else "超时预算已更新，将从下一次尝试生效。"
            ),
        }

    @api.post("/api/tasks/{tid}/run")
    def run_task(tid: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        task = ctx.task(tid)
        result = {}

        def _run() -> None:
            outcome = app.engine.run_task(task["id"], agent_id=payload.get("agent_id"))
            result.update(outcome.__dict__)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=float(payload.get("wait", 300)))
        if result:
            return result
        return {"accepted": True, "task_id": task["id"], "message": "任务已在后台执行"}

    @api.post("/api/projects/{pid}/run")
    def run_project(pid: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        return _start_project_run(ctx, ctx.project(pid), payload)

    @api.get("/api/projects/{pid}/run-status")
    def run_status(pid: str) -> dict[str, Any]:
        project = ctx.project(pid)
        return ctx.background.get(project["id"], {"running": False})

    @api.post("/api/projects/{pid}/stop")
    def stop_project(pid: str) -> dict[str, Any]:
        """停止一个项目：不再派发新任务，并**真正终止**该项目在跑的 Agent（需求 10 / 54）。

        必须是**项目级**停止：早期实现直接调 ``engine.shutdown()``（全局急停，会连带终止
        其他项目正在跑的 Agent），违反需求 10「项目之间文件/任务/配置相互隔离」。
        ``engine.shutdown()`` 只保留给进程退出（``Workbench.close()``），HTTP 路由不得调用。
        """
        project = ctx.project(pid)
        result = app.engine.stop_project(project["id"])
        runs = int(result.get("runs_cancelled", 0) or 0)
        tasks = int(result.get("tasks_cancelled", 0) or 0)
        return {
            **result,
            "stopped": True,
            "message": f"已停止：终止 {runs} 个运行中的 Agent 调用，{tasks} 个任务标记为已取消",
        }

    @api.post("/api/projects/{pid}/recover")
    def recover_project(
        pid: str,
        payload: dict[str, Any] = Body(default={}),
        force: bool | None = Query(None),
    ) -> dict[str, Any]:
        """把遗留的「执行中」标记为中断，可续跑（需求 40）。

        ``force`` 默认 **False（保守）**：同机按 PID 判存活 —— 本机若还有另一个活着的调度方
        （例如用户同时在 CLI 里跑 ``run_project``），不会把它正在执行的任务误判为中断；
        跨机场景退回心跳 staleness。

        只有调用方**显式**要求强制恢复才会无视存活判定：请求体 ``{"force": true}``
        或 ``?force=true``（请求体里出现 ``force`` 键时以请求体为准，其次是 query，都没有＝False）。
        强制恢复会把仍在跑的任务与 run 行一并标成 interrupted 并清租约，**是破坏性操作**，
        因此控制台用独立的「强制恢复」按钮 + 二次确认，绝不默认开启。自动调度内部那次
        recover 也仍然保持保守（见 ``Engine.recover``）。
        """
        project = ctx.project(pid)
        raw = payload.get("force") if "force" in payload else force
        result = app.engine.recover(project["id"], force=_as_flag(raw))
        return {"recovered": result.get("interrupted", 0), "message": f"已标记 {result.get('interrupted', 0)} 个中断任务，可再次启动调度续跑"}

    @api.post("/api/tasks/{tid}/fanout")
    def fanout(tid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        task = ctx.task(tid)
        return app.engine.fanout(task["id"], payload.get("agents", []))

    @api.post("/api/tasks/{tid}/deps")
    def add_dep(tid: str, payload: dict[str, Any] = Body(...)) -> list[dict[str, Any]]:
        task = ctx.task(tid)
        dep = app.tasks.find(payload.get("depends_on", ""))
        if not dep or dep["project_id"] != task["project_id"]:
            raise HTTPException(400, "依赖任务不存在或不属于同一项目")
        if dep["id"] == task["id"]:
            raise HTTPException(400, "任务不能依赖自身")
        app.tasks.add_edge(task["id"], dep["id"], payload.get("mode", "success"))
        return app.tasks.dependencies(task["id"])

    @api.delete("/api/tasks/{tid}/deps/{dep}")
    def remove_dep(tid: str, dep: str) -> list[dict[str, Any]]:
        task = ctx.task(tid)
        app.tasks.remove_edge(task["id"], dep)
        return app.tasks.dependencies(task["id"])

    @api.post("/api/tasks/{tid}/move")
    def move_task(tid: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """动态调整执行顺序（需求 16）：把任务在同项目内上移/下移一位。"""
        task = ctx.task(tid)
        rows = sorted(app.tasks.list(task["project_id"]), key=lambda t: int(t.get("ordering") or 0))
        ids = [t["id"] for t in rows]
        index = ids.index(task["id"])
        target = index - 1 if payload.get("direction", "down") == "up" else index + 1
        if 0 <= target < len(ids):
            ids[index], ids[target] = ids[target], ids[index]
            for position, task_id in enumerate(ids):
                app.tasks.update(task_id, ordering=position + 1)
        return app.tasks.get(task["id"])

    @api.get("/api/tasks/{tid}/route")
    def route_preview(tid: str) -> dict[str, Any]:
        """预览自动路由结果（需求 5）：会选哪个 Agent、为什么。"""
        task = ctx.task(tid)
        try:
            agent_id, reason = app.runner.choose_agent(task)
        except Exception as exc:  # noqa: BLE001 - 预览失败不应报错
            return {"agent_id": None, "reason": f"路由预览失败：{exc}"}
        return {"agent_id": agent_id, "reason": reason, "explicit": task.get("agent_id")}

    # ------------------------------------------------------------ 工作副本（task-11）
    @api.get("/api/tasks/{tid}/workspace")
    def task_workspace(tid: str) -> dict[str, Any]:
        """查看任务工作副本：产物在哪、有哪些待同步改动、能不能同步。

        现场场景：``isolation=copy`` 时产物在 runtime 副本里，项目根目录看起来「只有一个
        ``.multiagent``」，用户以为丢了。这里只对比「任务副本 vs 它的运行前基线」，
        **不扫整个工作区**。

        两个计数别混用：``pending`` 是 baseline vs workdir（commit_back 的输入，同步后
        仍非 0）；``unsynced`` 是 workdir vs 项目根目录（「还没落到根目录」的改动，
        UI 的「待同步 N」应该读它）。
        """
        task = ctx.task(tid)
        project = _task_project(app, task)
        return describe_task_workspace(app, task, project)

    @api.post("/api/tasks/{tid}/sync")
    def task_sync(tid: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """把工作副本里的改动手动同步回项目根目录（``dry_run`` 可预览）。

        语义：running 也允许（UI 自行警告）；**不改变任务状态**；只回收 write_scope 内、
        非保护路径、非 read_only 的改动；``workdir == root``（shared）返回 ``already_shared``。
        """
        task = ctx.task(tid)
        project = _task_project(app, task)
        try:
            return sync_task_workspace(
                app, task, project, dry_run=_as_flag(payload.get("dry_run"))
            )
        except SyncBlocked as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @api.post("/api/tasks/{tid}/{op}")
    def task_op(tid: str, op: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        task = ctx.task(tid)
        if op == "retry":
            try:
                return app.engine.retry(task["id"])
            except MergeRetryBlocked as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": {
                            "code": "merge_retry_blocked",
                            "message": exc.message,
                            "details": {
                                "task_id": exc.task_id,
                                "blocked_reason": exc.blocked_reason,
                            },
                        }
                    },
                ) from exc
        if op == "skip":
            return app.engine.skip(task["id"], payload.get("reason", "人工跳过"))
        if op == "cancel":
            return app.engine.cancel(task["id"], payload.get("reason", "人工取消"))
        if op == "takeover":
            return app.engine.takeover(task["id"], payload.get("agent_id", "human"))
        if op == "provide":
            return app.engine.provide(
                task["id"], did=payload.get("did", ""), artifacts=payload.get("artifacts"), notes=payload.get("notes", "")
            )
        if op == "revert":
            if task["status"] == "running":
                raise HTTPException(409, "任务仍在执行；请先取消，再回滚改动")
            project = app.projects.get(task["project_id"])
            stats = app.runner.journal.revert_task(task["id"], workspace_root=Path(project["root_path"]))
            app.tasks.set_status(task["id"], "pending", "人工回滚改动")
            return stats
        if op == "approve":
            note = payload.get("note") or "人工验收通过（保留 run 现场）"
            try:
                return app.engine.approve(task["id"], note=note, sync=_as_flag(payload.get("sync")))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            except SyncBlocked as exc:
                raise HTTPException(exc.status, str(exc)) from exc
        raise HTTPException(400, f"未知操作：{op}")

    @api.get("/api/tasks/{tid}/compare")
    def compare(tid: str) -> dict[str, Any]:
        return app.audit.compare_runs(ctx.task(tid)["id"])


def _register_board(api: FastAPI, ctx: ApiContext) -> None:
    app = ctx.app

    @api.get("/api/projects/{pid}/board")
    def board_list(
        pid: str,
        type: str | None = None,
        scope: str | None = None,
        status: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return app.board.list(ctx.project(pid)["id"], type=type, scope=scope, status=status, task_id=task_id, agent_id=agent_id)

    @api.post("/api/projects/{pid}/board")
    def board_post(pid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        project = ctx.project(pid)
        return app.board.post(
            project["id"],
            type=payload.get("type", "note"),
            title=payload.get("title", ""),
            body=payload.get("body", ""),
            scope=payload.get("scope", "task"),
            task_id=payload.get("task_id"),
            agent_id=payload.get("agent_id"),
            tags=payload.get("tags"),
            status=payload.get("status"),
            pinned=bool(payload.get("pinned")),
            actor=payload.get("actor", "human"),
        )

    @api.get("/api/projects/{pid}/board/search")
    def board_search(
        pid: str,
        q: str = "",
        task_id: str | None = None,
        agent_id: str | None = None,
        type: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        return app.board.search(ctx.project(pid)["id"], q, task_id=task_id, agent_id=agent_id, type=type, since=since, until=until)

    @api.get("/api/board/{eid}")
    def board_get(eid: str) -> dict[str, Any]:
        return {"entry": app.board.get(eid), "versions": app.board.versions(eid)}

    @api.delete("/api/board/{eid}")
    def board_delete(eid: str) -> dict[str, Any]:
        return app.board.delete(eid)

    @api.patch("/api/board/{eid}")
    def board_edit(eid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return app.board.edit(
            eid,
            title=payload.get("title"),
            body=payload.get("body"),
            tags=payload.get("tags"),
            author=payload.get("author", "human"),
            note=payload.get("note"),
        )

    @api.post("/api/board/{eid}/review")
    def board_review(eid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return app.board.review(eid, approved=bool(payload.get("approved")), reviewer=payload.get("reviewer", "human"), note=payload.get("note"))

    @api.get("/api/projects/{pid}/memory")
    def memory(pid: str) -> dict[str, Any]:
        project = ctx.project(pid)
        return {
            "long_term": app.board.long_term_memory(project["id"]),
            "pending": app.board.pending_reviews(project["id"]),
        }

    @api.get("/api/projects/{pid}/events")
    def events(pid: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return app.board.read_events(ctx.project(pid)["id"], after_id=after, limit=limit)

    # ------------------------------------------------------------ 交接 / 审批
    @api.get("/api/projects/{pid}/handoffs")
    def handoffs(pid: str) -> list[dict[str, Any]]:
        return app.handoffs.list_for_project(ctx.project(pid)["id"])

    @api.get("/api/tasks/{tid}/handoff")
    def task_handoff(tid: str) -> dict[str, Any]:
        return app.handoffs.latest_for_task(ctx.task(tid)["id"]) or {}

    @api.patch("/api/handoffs/{hid}")
    def edit_handoff(hid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return app.handoffs.edit(hid, payload, editor=payload.get("editor", "human"))

    @api.get("/api/projects/{pid}/approvals")
    def approvals(pid: str, status: str | None = None) -> list[dict[str, Any]]:
        return app.guard.list(ctx.project(pid)["id"], status=status)

    @api.post("/api/approvals/{aid}/resolve")
    def resolve_approval(aid: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return app.guard.resolve(
            aid, approved=bool(payload.get("approved")), by=payload.get("by", "human"), resolution=payload.get("resolution"), board=app.board
        )


def _register_observe(api: FastAPI, ctx: ApiContext) -> None:
    app = ctx.app

    @api.get("/api/projects/{pid}/stats")
    def stats(pid: str) -> dict[str, Any]:
        return app.audit.stats(ctx.project(pid)["id"])

    @api.get("/api/projects/{pid}/runs")
    def project_runs(pid: str, agent_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return app.audit.runs(project_id=ctx.project(pid)["id"], agent_id=agent_id, limit=limit)

    @api.get("/api/projects/{pid}/run-events")
    def project_run_events(
        pid: str,
        after: int = 0,
        limit: int = 200,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """跨进程实时事件回放（CLI/桌面进程跑的任务也能在看板里看到）。"""
        project = ctx.project(pid)
        items = app.run_events.list(project["id"], after_id=after, limit=limit, run_id=run_id)
        return {
            "items": items,
            "last_id": items[-1]["event_id"] if items else int(after or 0),
            "count": len(items),
        }

    @api.get("/api/projects/{pid}/timeline")
    def timeline(pid: str, fmt: str = Query("json")) -> Any:
        project = ctx.project(pid)
        items = app.audit.timeline(project["id"])
        if fmt == "markdown":
            out = app.audit.export_timeline(project["id"], app.config.runtime_dir / f"timeline-{project['id']}.md")
            return FileResponse(out)
        return items

    @api.get("/api/tasks/{tid}/changes")
    def changes(tid: str) -> list[dict[str, Any]]:
        task = ctx.task(tid)
        return [dict(r) for r in app.db.query("SELECT * FROM file_changes WHERE task_id = ? ORDER BY id", (task["id"],))]

    @api.get("/api/tasks/{tid}/diff")
    def change_diff(tid: str, path: str = Query(...)) -> dict[str, Any]:
        """某次文件改动的统一 diff（文本）；二进制只回大小（需求 34 / 50）。"""
        task = ctx.task(tid)
        row = app.db.query_one(
            "SELECT * FROM file_changes WHERE task_id = ? AND rel_path = ? ORDER BY id DESC LIMIT 1",
            (task["id"], path),
        )
        if not row:
            raise HTTPException(404, f"没有该文件的改动记录：{path}")
        rec = dict(row)
        blobs = app.runner.journal.blobs
        before = _read_blob(blobs, rec.get("before_hash"))
        after = _read_blob(blobs, rec.get("after_hash"))
        head_b = (before or b"")[:8000]
        head_a = (after or b"")[:8000]
        if rec.get("is_binary") or b"\x00" in head_b or b"\x00" in head_a:
            return {
                "path": path, "binary": True, "action": rec.get("action"),
                "before_size": rec.get("before_size"), "after_size": rec.get("after_size"),
                "note": "二进制文件，仅记录改动（不自动合并，交人工处理）",
            }
        old = (before or b"").decode("utf-8", "replace").splitlines()
        new = (after or b"").decode("utf-8", "replace").splitlines()
        diff_lines = list(difflib.unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
        return {
            "path": path, "binary": False, "action": rec.get("action"),
            "before_size": rec.get("before_size"), "after_size": rec.get("after_size"),
            "diff": "\n".join(diff_lines),
        }

    @api.get("/api/runs/{rid}/output")
    def run_output(rid: str, limit: int = 200000) -> dict[str, Any]:
        """读回某次 Agent 调用的输入/输出现场（需求 43 / 46）。

        安全：只读工作台数据目录内的文件，数据库里被污染/越界的路径一律拒绝。
        """
        row = app.db.query_one("SELECT * FROM runs WHERE id = ?", (rid,))
        if not row:
            raise HTTPException(404, f"运行记录不存在：{rid}")
        run = dict(row)
        root = app.config.data_dir

        def _read(path_value: str | None) -> str:
            if not path_value:
                return ""
            resolved = validate_stored_path(path_value, root)
            if resolved is None:
                return "[已拒绝] 记录中的路径越界，未读取"
            if not resolved.exists():
                return ""
            try:
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return "[读取失败，请查看服务端日志]"
            if len(text) > limit:
                return text[:limit] + f"\n…（已截断，共 {len(text)} 字符）"
            return text

        return {
            "run": {
                "id": run.get("id"), "agent_id": run.get("agent_id"), "status": run.get("status"),
                "exit_code": run.get("exit_code"), "duration_ms": run.get("duration_ms"),
                "cost_tokens": run.get("cost_tokens"), "started_at": run.get("started_at"),
                "error": run.get("error"),
            },
            "prompt": _read(run.get("prompt_path")),
            "stdout": _read(run.get("stdout_path")),
            "stderr": _read(run.get("stderr_path")),
        }


def _read_blob(blobs: Any, digest: Any) -> bytes | None:
    """只接受 sha256 十六进制摘要，避免被污染的 DB 行把路径拼出 blob 根目录。"""
    if not digest or not _BLOB_DIGEST.fullmatch(str(digest)):
        return None
    return blobs.read_bytes(str(digest))


def _register_stream(api: FastAPI, ctx: ApiContext) -> None:
    app = ctx.app

    @api.websocket("/ws")
    async def ws(websocket: WebSocket, project: str | None = None) -> None:
        # 握手先做 Host / Origin / token 校验，再 accept（跨站页面必带 Origin）
        headers = websocket.headers
        host = headers.get("host")
        if not ctx.security.host_ok(host):
            await websocket.close(code=1008, reason="bad host")
            return
        if not ctx.security.origin_ok(headers.get("origin"), host):
            await websocket.close(code=1008, reason="cross origin")
            return
        offered = [
            item.strip()
            for item in (headers.get("sec-websocket-protocol") or "").split(",")
            if item.strip()
        ]
        token, accepted_protocol = extract_ws_token(
            headers, websocket.query_params, offered
        )
        if ctx.security.require_token and not ctx.security.token_ok(token):
            await websocket.close(code=1008, reason="unauthorized")
            return
        # 回显选中的子协议，浏览器才会保留连接；token 本身不再出现在 URL。
        await websocket.accept(subprotocol=accepted_protocol)
        project_id = None
        if project:
            try:
                project_id = app.projects.get(project)["id"]
            except LookupError:
                await websocket.send_json({"type": "error", "message": f"项目不存在：{project}"})
        ctx.hub.bind_loop(asyncio.get_running_loop())
        queue = ctx.hub.subscribe()
        last_event = 0
        # 实时事件从持久流回放最近 200 条，再持续 tail；这样 CLI/桌面进程跑的任务
        # 在浏览器看板里也能看到，而不仅限于「在同一个 API 进程里启动」的任务。
        last_run_event = max(0, app.run_events.latest_id(project_id) - 200)
        seen_run_events: set[int] = set()
        try:
            backlog = ctx.hub.snapshot(project_id)
            if backlog:
                await websocket.send_json({"type": "stream.snapshot", "items": backlog})
            recent_runs = app.run_events.list(project_id, after_id=last_run_event, limit=200)
            if recent_runs:
                for item in recent_runs:
                    if item.get("event_id"):
                        seen_run_events.add(int(item["event_id"]))
                        last_run_event = max(last_run_event, int(item["event_id"]))
                await websocket.send_json({"type": "run.snapshot", "items": recent_runs})
            while True:
                drained = 0
                while drained < 300:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    drained += 1
                    if project_id is None or item.get("project_id") == project_id:
                        event_id = item.get("event_id")
                        if event_id and int(event_id) in seen_run_events:
                            continue
                        if event_id:
                            seen_run_events.add(int(event_id))
                            last_run_event = max(last_run_event, int(event_id))
                        await websocket.send_json({"type": "stream.event", "event": item})
                run_items = app.run_events.list(project_id, after_id=last_run_event, limit=300)
                for item in run_items:
                    event_id = int(item.get("event_id") or 0)
                    if event_id:
                        seen_run_events.add(event_id)
                        last_run_event = max(last_run_event, event_id)
                    await websocket.send_json({"type": "stream.event", "event": item})
                if len(seen_run_events) > 20000:
                    # 只保留最近的一段，避免长时间连接内存无限增长
                    seen_run_events = {value for value in seen_run_events if value > last_run_event - 5000}
                payload = app.board.read_events(project_id, after_id=last_event, limit=200)
                if payload:
                    last_event = payload[-1]["id"]
                    await websocket.send_json({"type": "events", "items": payload})
                if project_id:
                    await websocket.send_json({"type": "progress", "progress": app.tasks.progress(project_id)})
                    await websocket.send_json({"type": "run_status", "status": ctx.background.get(project_id, {"running": False})})
                await asyncio.sleep(0.5)
        except WebSocketDisconnect:
            return
        except Exception:  # noqa: BLE001 - 连接中断即退出
            return
        finally:
            ctx.hub.unsubscribe(queue)


def _register_static(api: FastAPI, ctx: ApiContext) -> None:
    if not WEB_DIR.exists():
        return

    # 新版前端（webapp/，Vite 构建到 web/dist）。构建产物存在时只提供它；
    # 旧零构建控制台是明确的 legacy 回退，必须显式设置 MULTIAGENT_UI=legacy
    # 才能通过 /legacy 入口访问，默认不在任何入口暴露。
    dist_dir = WEB_DIR / "dist"
    index_path = dist_dir / "index.html"
    prefer_legacy = (os.environ.get("MULTIAGENT_UI") or "").strip().lower() == "legacy"

    @api.get("/")
    def index() -> FileResponse:
        if index_path.exists():
            return FileResponse(index_path)
        # 没有构建产物时仍然可用（开发/最小安装），但这是降级路径而非并列入口。
        return FileResponse(WEB_DIR / "index.html")

    @api.get("/legacy")
    def legacy_index() -> FileResponse:
        if not prefer_legacy:
            raise HTTPException(
                404,
                "旧版控制台已退役：设置环境变量 MULTIAGENT_UI=legacy 后重启，"
                "才可通过 /legacy 访问。",
            )
        return FileResponse(WEB_DIR / "index.html")

    @api.get("/static/index.html")
    def legacy_static_index() -> FileResponse:
        # StaticFiles 挂在 /static 下，这里先注册精确路由，阻止旧入口被直接访问。
        if not prefer_legacy:
            raise HTTPException(
                404,
                "旧版控制台入口默认关闭；设置 MULTIAGENT_UI=legacy 后使用 /legacy。",
            )
        return FileResponse(WEB_DIR / "index.html")

    api.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


# --------------------------------------------------------------------------- 装配


def create_api(
    app: Workbench,
    *,
    host: str | None = None,
    token: str | None = None,
    strict: bool | None = None,
    policy: SecurityPolicy | None = None,
    env: Mapping[str, str] | None = None,
) -> FastAPI:
    """构造 API 应用。

    ``host`` 是**监听**地址（决定是否强制 token），不是请求里的 Host 头：
    默认 ``127.0.0.1`` 走零配置豁免，桌面端与浏览器看板无需改动。
    """
    ctx = ApiContext(app, policy or build_policy(app, host=host, token=token, strict=strict, env=env))
    api = FastAPI(title="多 Agent 协作工作台", version="0.1.0")
    api.state.security = ctx.security
    # 供运维/测试观察实时流（coalescer 的 pending、hub 的回放缓冲）
    api.state.hub = ctx.hub
    api.state.coalescer = ctx.coalescer
    cors = ctx.security.cors_kwargs()
    if cors:
        from fastapi.middleware.cors import CORSMiddleware

        api.add_middleware(CORSMiddleware, **cors)
    _install_http_middleware(api, ctx)
    _install_error_handlers(api, ctx)
    _register_base(api, ctx)
    _register_projects(api, ctx)
    from .routes_validation import register_validation_routes

    register_validation_routes(api, ctx)
    # 具体的 /api/tasks/{tid}/<verb> 必须先于兜底的 /api/tasks/{tid}/{op} 注册，
    # 否则 POST /merge、POST /discard-worktree 会被当成未知任务操作。
    from .routes_files import register_files_routes
    from .routes_git import register_git_routes

    register_files_routes(api, ctx)
    register_git_routes(api, ctx)
    _register_tasks(api, ctx)
    _register_board(api, ctx)
    _register_observe(api, ctx)
    _register_stream(api, ctx)
    _register_static(api, ctx)
    return api


def serve(
    app: Workbench,
    *,
    host: str = "127.0.0.1",
    port: int = 8770,
    open_browser: bool = False,
    token: str | None = None,
    strict: bool | None = None,
) -> None:
    import uvicorn

    policy = build_policy(app, host=host, token=token, strict=strict)
    for line in startup_banner(policy, host=host, port=port):
        print(line, file=sys.stderr)
    if open_browser:
        import webbrowser

        display_host = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
        suffix = f"?token={policy.token}" if policy.require_token and policy.token else ""
        threading.Timer(1.2, lambda: webbrowser.open(f"http://{display_host}:{port}/{suffix}")).start()
    try:
        uvicorn.run(create_api(app, policy=policy), host=host, port=port, log_level="info")
    finally:
        # 干净退出：删会话行、释放本进程槽位、停心跳；异常退出时 recover 会按 PID 回收。
        try:
            app.close()
        except Exception:  # noqa: BLE001 - 退出路径不能再抛
            LOGGER.exception("关闭工作台失败")
