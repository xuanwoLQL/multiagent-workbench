"""HTTP / WebSocket 接口层。

``create_api(app, *, host=..., token=..., strict=...)`` 构造应用；
``SecurityPolicy`` / ``build_policy`` 是鉴权与跨站防护的策略对象（见 :mod:`.security`）。
"""

from .app import create_api, serve
from .security import SecurityPolicy, build_policy

__all__ = ["create_api", "serve", "SecurityPolicy", "build_policy"]
