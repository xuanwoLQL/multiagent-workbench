"""API 安全策略：鉴权 / 跨站与 DNS rebinding / CORS / 文件路径边界（需求 52 加固）。

设计原则
--------
1. **默认零配置可用**：bind 在 ``127.0.0.1`` / ``::1``（桌面壳与本地浏览器看板）
   时不要求 token，Electron 直接开窗即可用，不破坏既有桌面体验。
2. **越权即拒**：
   * ``Host`` 必须是 loopback 名字/IP 字面量或显式信任列表 —— 挡住 DNS rebinding
     （恶意网页把域名解析到 127.0.0.1，浏览器同源策略会失效，只有 Host 能识别）；
   * 带 ``Origin`` 的跨站请求（浏览器对跨站 POST / fetch / WebSocket 握手必带）
     与 ``Sec-Fetch-Site: cross-site`` 一律拒绝；
   * 文件类接口一律先规范化（``resolve`` 跟随符号链接）再校验边界。
3. **绑定非 loopback 时强制 Bearer token**（``--token`` 等价物：环境变量
   ``MULTIAGENT_API_TOKEN``，或自动生成写入 ``<home>/.multiagent/token``，
   权限 0600）。
4. **严格模式**（loopback 也要求 token）通过 ``--strict-auth`` 等价物开启：
   环境变量 ``MULTIAGENT_STRICT_AUTH=1`` 或 ``config/policy.json`` 的
   ``security.strict``。

环境变量一览
------------
``MULTIAGENT_API_TOKEN``      显式 token
``MULTIAGENT_STRICT_AUTH``    1/true/on → loopback 也要求 token
``MULTIAGENT_TRUSTED_HOSTS``  额外信任的 Host（逗号分隔）
``MULTIAGENT_ALLOW_ORIGINS``  允许的跨站来源（逗号分隔，``*`` 不能配 credentials）
``MULTIAGENT_FS_ROOTS``       允许浏览/导入的根目录（``os.pathsep`` 分隔，空=不限）
``MULTIAGENT_HOST``           兜底 bind host（供脚本用）

``config/policy.json`` 可选块::

    {"security": {"strict": false, "require_token": false, "token": "...",
                  "trusted_hosts": ["myhost"], "allow_origins": ["http://x:1"],
                  "fs_roots": ["D:/work"]}}
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

# 单标签主机名（不含 "."）在非严格模式下放行：Starlette TestClient 用
# ``http://testserver``，企业内网也常用单标签主机名；公网 DNS 无法注册单标签域名，
# 因此这条豁免不构成 DNS rebinding 通道。
_TEST_HOSTS = frozenset({"testserver", "testclient"})
_LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1"})
_DEFAULT_TRUSTED = _LOOPBACK_NAMES | _TEST_HOSTS
_WILDCARD = "*"
_TOKEN_FILE = "token"

#: 静态看板的安全响应头（无内联脚本；style 属性需要 unsafe-inline）
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self' ws: wss:; font-src 'self'; "
    "object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
)

_BAD_HOST_CHARS = re.compile(r"[\s\x00-\x1f/\\@]")


class SecurityError(ValueError):
    """安全边界被触发（供接口转成 4xx）。"""

    def __init__(self, message: str, *, status: int = 403, code: str = "forbidden") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# --------------------------------------------------------------------------- 解析


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y", "是"}


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        raw = [str(v) for v in value]
    else:
        raw = re.split(r"[,;\s]+", str(value))
    return [item.strip() for item in raw if item.strip()]


def _split_paths(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        raw = [str(v) for v in value]
    else:
        raw = str(value).split(os.pathsep)
    return [item.strip().strip('"').strip("'") for item in raw if item.strip()]


def normalize_host(value: str | None) -> str:
    """把 ``Host`` 头归一化成裸主机名（小写、去端口、去 IPv6 方括号）。"""
    if not value:
        return ""
    raw = value.strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):  # [::1]:8770
        end = raw.find("]")
        return raw[1:end] if end != -1 else raw
    if raw.count(":") == 1:  # host:port（IPv6 未加括号时有 2 个以上冒号）
        head, _, tail = raw.partition(":")
        if tail.isdigit():
            return head
    return raw


def split_host_port(value: str | None) -> tuple[str, int | None]:
    """拆出 (主机名, 端口)；用于同源比较时对齐默认端口。"""
    if not value:
        return "", None
    raw = value.strip().lower()
    if raw.startswith("["):
        end = raw.find("]")
        host = raw[1:end] if end != -1 else raw
        rest = raw[end + 1:] if end != -1 else ""
        port = int(rest[1:]) if rest.startswith(":") and rest[1:].isdigit() else None
        return host, port
    if raw.count(":") == 1:
        head, _, tail = raw.partition(":")
        if tail.isdigit():
            return head, int(tail)
    return raw, None


def is_ip_literal(host: str) -> bool:
    candidate = (host or "").split("%")[0]
    if not candidate:
        return False
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return False
    return True


def is_loopback_bind(host: str | None) -> bool:
    """bind 目标是否只监听本机回环（决定是否强制 token）。"""
    name = normalize_host(host)
    if not name:
        return False
    if name in _LOOPBACK_NAMES:
        return True
    candidate = name.split("%")[0]
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def is_within(child: str | os.PathLike[str], root: str | os.PathLike[str]) -> bool:
    """``child`` 是否等于 ``root`` 或位于其下（Windows 大小写不敏感，已 ``resolve``）。

    调用方负责先 ``Path.resolve()``，让符号链接/junction 指向的真实路径参与比较。
    """
    child_abs = os.path.normcase(os.path.abspath(str(child)))
    root_abs = os.path.normcase(os.path.abspath(str(root)))
    if child_abs == root_abs:
        return True
    return child_abs.startswith(root_abs.rstrip("\\/") + os.sep)


def extract_token(headers: Mapping[str, str], query: Mapping[str, str]) -> str:
    """从 ``Authorization: Bearer ...`` 或 ``?token=`` 里取 token。

    ``?token=`` 是历史兼容路径（下载链接、旧书签）。WebSocket 握手的新客户端
    改用 ``Sec-WebSocket-Protocol`` 传递 token，避免 token 出现在 URL / 访问日志里；
    见 :func:`extract_ws_token`。
    """
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    token = query.get("token") if hasattr(query, "get") else None
    return (token or "").strip()


#: WebSocket 子协议前缀：``multiagent.token.<base64url(token)>``。
#: 子协议由浏览器在握手头里发送，不会进入 URL、历史记录或反向代理日志。
WS_TOKEN_PROTOCOL_PREFIX = "multiagent.token."


def _decode_b64url(value: str) -> str:
    import base64

    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def extract_ws_token(
    headers: Mapping[str, str],
    query: Mapping[str, str],
    protocols: list[str] | tuple[str, ...] | None = None,
) -> tuple[str, str | None]:
    """取 WebSocket 握手 token，返回 ``(token, 应回显的子协议)``。

    优先 ``Authorization``（非浏览器客户端），其次 ``?token=``（旧客户端），
    最后 ``Sec-WebSocket-Protocol``（浏览器新客户端）。服务端 accept 时必须回显
    选中的子协议，否则浏览器会主动断开。
    """
    token = extract_token(headers, query)
    if token:
        return token, None
    for protocol in protocols or []:
        if not protocol.startswith(WS_TOKEN_PROTOCOL_PREFIX):
            continue
        encoded = protocol[len(WS_TOKEN_PROTOCOL_PREFIX):]
        try:
            return _decode_b64url(encoded), protocol
        except (ValueError, UnicodeDecodeError):
            continue
    return "", None


def validate_stored_path(value: str | None, root: Path) -> Path | None:
    """校验数据库里记录的文件路径确实位于 ``root`` 下（防被污染的行读任意文件）。"""
    if not value:
        return None
    try:
        candidate = Path(value)
        if "\x00" in str(candidate):
            return None
        resolved = candidate.resolve()
    except (OSError, ValueError):
        return None
    if not is_within(resolved, root):
        return None
    return resolved


# --------------------------------------------------------------------------- 策略


@dataclass(frozen=True)
class SecurityPolicy:
    """一次 API 进程生效的完整安全策略。"""

    bind_host: str = "127.0.0.1"
    token: str = ""
    require_token: bool = False
    token_source: str = "none"
    strict: bool = False
    trusted_hosts: frozenset[str] = field(default_factory=lambda: frozenset(_DEFAULT_TRUSTED))
    allow_origins: tuple[str, ...] = ()
    allow_credentials: bool = False
    fs_roots: tuple[str, ...] = ()
    blocked_roots: tuple[str, ...] = ()
    cors_wildcard: bool = False

    # ------------------------------------------------------------- 判定
    @property
    def loopback_only(self) -> bool:
        return is_loopback_bind(self.bind_host)

    def host_ok(self, host_header: str | None) -> bool:
        """Host 头白名单校验 —— DNS rebinding 的唯一可靠识别点。"""
        raw = (host_header or "").strip()
        if not raw or _BAD_HOST_CHARS.search(raw):
            return False
        name = normalize_host(raw)
        if not name:
            return False
        if name in self.trusted_hosts:
            return True
        if is_ip_literal(name):
            # IP 字面量无法被 DNS 重绑定利用；局域网直连 IP 也要放行。
            return True
        if "." not in name and not self.strict:
            return True
        return False

    def origin_ok(self, origin: str | None, host_header: str | None) -> bool:
        """跨站请求判定：无 Origin（curl/原生客户端）放行，同源放行，其余拒绝。"""
        if not origin or not origin.strip():
            return True
        value = origin.strip()
        if value.lower() == "null":
            return False  # sandbox iframe / data: / file: 一律视为跨站
        if value in self.allow_origins:
            return True
        try:
            parts = urlsplit(value)
        except ValueError:
            return False
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False
        origin_host, origin_port = split_host_port(parts.netloc)
        host_name, host_port = split_host_port(host_header)
        if not origin_host or not host_name:
            return False
        default_port = 443 if scheme == "https" else 80
        return origin_host == host_name and (origin_port or default_port) == (host_port or default_port)

    def token_ok(self, provided: str | None) -> bool:
        if not self.require_token:
            return True
        if not self.token or not provided:
            return False
        return hmac.compare_digest(self.token.encode("utf-8"), str(provided).encode("utf-8"))

    # ------------------------------------------------------------- 路径
    def path_error(self, path: Path, *, kind: str = "目录") -> str:
        """返回拒绝原因（空串=允许）。"""
        for root in self.blocked_roots:
            try:
                if is_within(path, root):
                    return "该路径属于工作台私有数据/配置目录，禁止通过 API 访问"
            except (OSError, ValueError):
                continue
        if self.fs_roots:
            allowed = False
            for root in self.fs_roots:
                try:
                    if is_within(path, root):
                        allowed = True
                        break
                except (OSError, ValueError):
                    continue
            if not allowed:
                return f"{kind}不在允许访问的根目录内（MULTIAGENT_FS_ROOTS / policy.security.fs_roots）"
        return ""

    def assert_path(self, path: Path, *, kind: str = "目录") -> Path:
        reason = self.path_error(path, kind=kind)
        if reason:
            raise SecurityError(reason, status=403, code="path_out_of_bounds")
        return path

    def within_data(self, path: Path) -> bool:
        for root in self.blocked_roots:
            if is_within(path, root):
                return True
        return False

    # ------------------------------------------------------------- CORS
    def cors_kwargs(self) -> dict[str, Any] | None:
        """CORS 中间件参数；未配置跨站来源时返回 None（纯同源，最严格）。

        禁止 ``allow_origins=["*"]`` 与 ``allow_credentials=True`` 组合：通配来源
        加凭据等价于允许任意网站带 cookie 调用本 API。
        """
        if not self.allow_origins:
            return None
        origins = list(self.allow_origins)
        wildcard = _WILDCARD in origins
        if wildcard and self.allow_credentials:
            raise ValueError(
                "CORS 配置非法：allow_origins=['*'] 不能与 allow_credentials=True 同时使用"
            )
        return {
            "allow_origins": [_WILDCARD] if wildcard else origins,
            "allow_credentials": bool(self.allow_credentials) and not wildcard,
            "allow_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            "allow_headers": ["Authorization", "Content-Type"],
            "max_age": 600,
        }

    def summary(self) -> dict[str, Any]:
        """给 /api/health 用的安全态概览（不含 token 本身）。"""
        return {
            "bind_host": self.bind_host,
            "loopback_only": self.loopback_only,
            "auth_required": self.require_token,
            "strict": self.strict,
            "token_source": self.token_source,
            "trusted_hosts": sorted(self.trusted_hosts),
            "allow_origins": list(self.allow_origins),
            "fs_roots": list(self.fs_roots),
        }


# --------------------------------------------------------------------------- token 文件


def token_file_path(data_dir: str | os.PathLike[str]) -> Path:
    """token 文件位置：``<data_dir>/token``（``data_dir`` 即 ``<home>/.multiagent``）。"""
    return Path(data_dir) / _TOKEN_FILE


def read_token_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.strip()


def write_token_file(path: Path, token: str) -> None:
    """原子写入并尽量收紧权限（POSIX 0600；Windows 依赖用户目录 ACL）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows/受限文件系统
        pass


def ensure_token(data_dir: str | os.PathLike[str]) -> tuple[str, str]:
    """确保 ``<data_dir>/token`` 存在，返回 (token, 来源)。"""
    path = token_file_path(data_dir)
    existing = read_token_file(path)
    if existing:
        return existing, "file"
    token = secrets.token_urlsafe(32)
    write_token_file(path, token)
    return token, "generated"


# --------------------------------------------------------------------------- 组装


def build_policy(
    app: Any,
    *,
    host: str | None = None,
    token: str | None = None,
    strict: bool | None = None,
    env: Mapping[str, str] | None = None,
) -> SecurityPolicy:
    """从 bind host + 环境变量 + ``policy.json:security`` 合成策略。"""
    environ: Mapping[str, str] = os.environ if env is None else env
    config = getattr(app, "config", None)
    raw: dict[str, Any] = {}
    if config is not None:
        try:
            value = config.policy_get("security", {})
        except Exception:  # noqa: BLE001 - 策略缺失不应阻断启动
            value = {}
        if isinstance(value, dict):
            raw = value

    bind_host = str(host or environ.get("MULTIAGENT_HOST") or raw.get("bind_host") or "127.0.0.1")
    loopback = is_loopback_bind(bind_host)
    strict_mode = _as_bool(strict) if strict is not None else _as_bool(
        environ.get("MULTIAGENT_STRICT_AUTH") or raw.get("strict")
    )
    require = bool(raw.get("require_token")) or strict_mode or not loopback

    explicit = (token or "").strip() or (environ.get("MULTIAGENT_API_TOKEN") or "").strip() or str(
        raw.get("token") or ""
    ).strip()
    source = "none"
    secret = ""
    if explicit:
        secret, source = explicit, "explicit"
    elif require:
        data_dir = getattr(config, "data_dir", None) or (Path(bind_host or ".").parent)
        secret, source = ensure_token(data_dir)

    trusted = set(_DEFAULT_TRUSTED)
    if not loopback and bind_host not in ("0.0.0.0", "::", ""):
        trusted.add(normalize_host(bind_host))
    trusted.update(h.lower() for h in _split_list(environ.get("MULTIAGENT_TRUSTED_HOSTS")))
    trusted.update(h.lower() for h in _split_list(raw.get("trusted_hosts")))
    trusted.discard("")

    origins = tuple(_split_list(environ.get("MULTIAGENT_ALLOW_ORIGINS")) or _split_list(raw.get("allow_origins")))
    fs_roots = tuple(_split_paths(environ.get("MULTIAGENT_FS_ROOTS")) or _split_paths(raw.get("fs_roots")))
    blocked: list[str] = []
    for attr in ("data_dir", "config_dir"):
        value = getattr(config, attr, None)
        if value:
            blocked.append(str(Path(value)))

    return SecurityPolicy(
        bind_host=bind_host,
        token=secret,
        require_token=require,
        token_source=source,
        strict=strict_mode,
        trusted_hosts=frozenset(trusted),
        allow_origins=origins,
        allow_credentials=bool(raw.get("allow_credentials")),
        fs_roots=fs_roots,
        blocked_roots=tuple(blocked),
        cors_wildcard=_WILDCARD in origins,
    )


def startup_banner(policy: SecurityPolicy, *, host: str, port: int) -> list[str]:
    """启动时打印的安全提示（不含 token 明文）。"""
    lines: list[str] = []
    if policy.require_token:
        lines.append(
            f"[安全] 已启用 Bearer token 鉴权（来源：{policy.token_source}）。"
            "客户端需带 Authorization: Bearer <token> 或 ?token=<token>。"
        )
        if policy.token_source == "generated":
            lines.append("[安全] token 已写入 <home>/.multiagent/token（权限 0600），请妥善保管。")
        elif policy.token_source == "file":
            lines.append("[安全] token 取自 <home>/.multiagent/token。")
    if not policy.loopback_only:
        lines.append(
            f"[安全] 正在监听 {host}:{port}（非 loopback）：已强制 token 鉴权，并校验 Host/Origin。"
        )
    if policy.strict:
        lines.append("[安全] 严格模式：即便监听 loopback 也要求 token。")
    if policy.allow_origins:
        lines.append(f"[安全] CORS 允许来源：{', '.join(policy.allow_origins)}（credentials={policy.allow_credentials}）")
    return lines
