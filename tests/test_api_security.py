"""API 安全加固的复现 / 回归测试（任务书 task-2）。

覆盖六类真实缺陷，每条测试都对应一个「修复前失败、修复后通过」的断言：

1. 无鉴权：``/api/**`` 约 90 个端点开放 → 非 loopback / 严格模式强制 Bearer token；
2. 跨站与 DNS rebinding：跨站 Origin、``Sec-Fetch-Site: cross-site``、伪造 Host 被拒；
3. CORS：默认同源；显式来源可用；禁止 ``allow_origins=["*"]`` + credentials；
4. 路径穿越：fs/list、import、config 文件名、runs 输出、blob 摘要；
5. 前端 XSS：静态回归护栏（不可信字段必须走 ``esc()``）；
6. 错误处理：统一信封、5xx 不回显堆栈与绝对路径、安全响应头。

注意：这些测试只使用 ``create_api(wb)`` 的既有接口（外加新增的关键字参数），
以便在修复前后都能直接跑出差异。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from multiagent.api.app import WEB_DIR, create_api
from multiagent.api.security import (
    SecurityPolicy,
    build_policy,
    extract_token,
    is_within,
    normalize_host,
    split_host_port,
    token_file_path,
    validate_stored_path,
)

APP_JS = WEB_DIR / "app.js"
INDEX_HTML = WEB_DIR / "index.html"

_ENV_KEYS = (
    "MULTIAGENT_API_TOKEN",
    "MULTIAGENT_STRICT_AUTH",
    "MULTIAGENT_TRUSTED_HOSTS",
    "MULTIAGENT_ALLOW_ORIGINS",
    "MULTIAGENT_FS_ROOTS",
    "MULTIAGENT_HOST",
)


@pytest.fixture(autouse=True)
def _clean_security_env(monkeypatch):
    """测试环境与宿主机环境隔离，避免宿主 Environment 影响策略。"""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _client(wb, **kwargs) -> TestClient:
    kwargs.setdefault("env", {})
    return TestClient(create_api(wb, **kwargs))


def _new_project(client: TestClient, name: str = "sec") -> str:
    res = client.post("/api/projects", json={"name": name})
    assert res.status_code == 200, res.text
    return res.json()["id"]


# ===========================================================================
# 1) 跨站 / DNS rebinding
# ===========================================================================


def test_cross_origin_post_rejected(wb):
    """恶意网页跨站 POST 驱动本机 Agent —— 本项目最大的真实风险。"""
    client = _client(wb)
    res = client.post(
        "/api/projects",
        json={"name": "evil"},
        headers={"Origin": "http://evil.example"},
    )
    assert res.status_code == 403
    assert res.json()["error_code"] == "cross_origin"
    assert client.get("/api/projects").json() == []  # 确实没落库


def test_cross_origin_get_rejected(wb):
    """跨站 GET 同样拒绝（fs/list、runs output 都是信息泄漏面）。"""
    client = _client(wb)
    res = client.get("/api/fs/list", headers={"Origin": "http://evil.example"})
    assert res.status_code == 403
    assert res.json()["error_code"] == "cross_origin"


def test_cross_site_fetch_metadata_rejected(wb):
    """浏览器没带 Origin 的跨站请求（img/script/表单 GET）用 Fetch Metadata 兜底。"""
    client = _client(wb)
    res = client.get("/api/config/files", headers={"Sec-Fetch-Site": "cross-site"})
    assert res.status_code == 403


def test_null_origin_rejected(wb):
    """sandbox iframe / data: 页面的 Origin: null 视为跨站。"""
    client = _client(wb)
    res = client.post("/api/config/reload", json={}, headers={"Origin": "null"})
    assert res.status_code == 403


def test_dns_rebinding_host_rejected(wb):
    """Host 指向攻击者域名 → 拒绝（浏览器同源策略此时已经失效）。"""
    client = TestClient(create_api(wb, env={}), base_url="http://evil.example")
    res = client.get("/api/health")
    assert res.status_code == 400
    assert res.json()["error_code"] == "bad_host"


@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:8811", "http://localhost:8811", "http://[::1]:8811", "http://192.168.1.9:8811", "http://testserver"],
)
def test_legitimate_hosts_allowed(wb, base_url):
    """桌面端（127.0.0.1:动态端口）、浏览器看板、局域网 IP 都必须放行。"""
    client = TestClient(create_api(wb, env={}), base_url=base_url)
    assert client.get("/api/health").status_code == 200


def test_same_origin_with_dynamic_port_allowed(wb):
    """同源 Origin（含动态端口）必须放行，否则桌面端整体不可用。"""
    client = _client(wb)
    res = client.post(
        "/api/projects",
        json={"name": "desktop-ok"},
        headers={"Origin": "http://testserver"},
    )
    assert res.status_code == 200, res.text


def test_websocket_cross_origin_rejected(wb):
    client = _client(wb)
    pid = _new_project(client, "ws-evil")
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/ws?project={pid}", headers={"Origin": "http://evil.example"}):
            pass
    assert excinfo.value.code == 1008


def test_websocket_same_origin_still_works(wb):
    client = _client(wb)
    pid = _new_project(client, "ws-ok")
    with client.websocket_connect(f"/ws?project={pid}", headers={"Origin": "http://testserver"}) as ws:
        message = ws.receive_json()
        assert message["type"] in ("events", "progress", "run_status", "stream.snapshot")


# ===========================================================================
# 2) 鉴权：默认 loopback 零配置，越权即拒
# ===========================================================================


def test_loopback_default_needs_no_token(wb):
    """默认行为：桌面端/浏览器同源直连不需要任何 token（不能破坏可用性）。"""
    client = _client(wb)
    health = client.get("/api/health").json()
    assert health["ok"] is True
    assert health["security"]["auth_required"] is False
    assert _new_project(client, "zero-config")
    assert client.put("/api/config/files/policy.json", json={"content": {}}).status_code == 200


def test_strict_auth_enforced_via_env(wb, monkeypatch):
    """严格模式（环境变量开启）后，未带 token 一律 401，带 token 放行。"""
    monkeypatch.setenv("MULTIAGENT_STRICT_AUTH", "1")
    client = TestClient(create_api(wb, env=dict(os.environ)))
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/projects", json={"name": "nope"}).status_code == 401
    assert client.put("/api/config/files/policy.json", json={"content": {}}).status_code == 401

    token = token_file_path(wb.config.data_dir).read_text(encoding="utf-8").strip()
    assert token
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/projects", headers=auth).status_code == 200
    assert client.post("/api/projects", json={"name": "with-token"}, headers=auth).status_code == 200


def test_remote_bind_requires_token(wb):
    """绑定 0.0.0.0 时强制 token；token 可从文件读取，也可走 ?token=。"""
    client = _client(wb, host="0.0.0.0")
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/agents/worker/check").status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer "}).status_code == 401

    token = token_file_path(wb.config.data_dir).read_text(encoding="utf-8").strip()
    assert len(token) >= 32
    assert client.get("/api/projects", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get(f"/api/projects?token={token}").status_code == 200
    assert client.get("/api/projects?token=bad").status_code == 401


def test_health_stays_anonymous_but_minimal(wb):
    """桌面端 waitForHealth() 在开窗前轮询：必须匿名 200，但不泄漏主目录/Agent 清单。"""
    client = _client(wb, host="0.0.0.0")
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "home" not in body and "agents" not in body

    token = token_file_path(wb.config.data_dir).read_text(encoding="utf-8").strip()
    full = client.get("/api/health", headers={"Authorization": f"Bearer {token}"}).json()
    assert full["home"] == str(wb.config.home)
    assert full["security"]["auth_required"] is True


def test_explicit_token_from_env_and_argument(wb, monkeypatch):
    monkeypatch.setenv("MULTIAGENT_API_TOKEN", "env-token-123")
    client = TestClient(create_api(wb, strict=True, env=dict(os.environ)))
    assert client.get("/api/projects").status_code == 401
    assert client.get("/api/projects", headers={"Authorization": "Bearer env-token-123"}).status_code == 200

    client2 = _client(wb, strict=True, token="arg-token-456")
    assert client2.get("/api/projects", headers={"Authorization": "Bearer arg-token-456"}).status_code == 200
    assert client2.get("/api/projects", headers={"Authorization": "Bearer env-token-123"}).status_code == 401


def test_recover_endpoint_passes_force_only_when_explicit(wb, project, monkeypatch):
    """恢复默认保守（force=False）；只有显式 {"force": true} / ?force=true 才强制回收。

    背景：本机若还有另一个活着的调度方（例如 CLI 另开了一次 run_project），强制回收会把
    **正在跑**的任务判成中断，属于破坏性操作，所以绝不能被默认打开、也不能被字符串
    ``"false"`` 之类的非空值意外打开。
    """
    calls: list[bool] = []

    def fake_recover(project_id=None, *, stale_seconds=None, force=False):  # noqa: ANN001, ANN003
        calls.append(bool(force))
        return {"interrupted": 0}

    monkeypatch.setattr(wb.engine, "recover", fake_recover)
    client = _client(wb)
    url = f"/api/projects/{project['id']}/recover"

    assert client.post(url, json={}).status_code == 200
    assert calls[-1] is False  # 默认：保守

    assert client.post(url).status_code == 200  # 完全不带 body
    assert calls[-1] is False

    assert client.post(url, json={"force": False}).status_code == 200
    assert calls[-1] is False

    assert client.post(url, json={"force": "false"}).status_code == 200  # 非空字符串不算真
    assert calls[-1] is False

    assert client.post(url, json={"force": True}).status_code == 200
    assert calls[-1] is True

    assert client.post(f"{url}?force=true", json={}).status_code == 200
    assert calls[-1] is True

    assert client.post(f"{url}?force=1").status_code == 200
    assert calls[-1] is True

    # 请求体里显式写了 force 就以请求体为准（可用来覆盖 query 默认）
    assert client.post(f"{url}?force=true", json={"force": False}).status_code == 200
    assert calls[-1] is False


def test_frontend_force_recover_is_a_separate_confirmed_action():
    """控制台的强制恢复必须是独立动作 + 二次确认，且默认「恢复」按钮不带 force。"""
    js = APP_JS.read_text(encoding="utf-8")
    assert "do-force-recover" in js and "btn-force-recover" in js
    assert "openForceRecoverModal" in js and "forceRecoverProject" in js
    # 默认恢复路径必须发空 body（即 force 缺省 False），强制路径才带 force: true
    plain = re.search(r"/recover`,\s*\{\s*method:\s*\"POST\",\s*body:\s*\{\s*\}\s*\}", js)
    forced = re.search(r"/recover`,\s*\{\s*method:\s*\"POST\",\s*body:\s*\{\s*force:\s*true\s*\}\s*\}", js)
    assert plain, "默认「恢复」按钮不得携带 force"
    assert forced, "强制恢复必须显式带 force: true"
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="btn-force-recover"' in html
    assert 'id="btn-recover"' in html


def test_cli_serve_exposes_token_flags_and_passes_them_through(wb, monkeypatch):
    """CLI 严格模式入口钉死：``serve`` 支持 ``--token`` / ``--strict-auth`` 并透传给 ``api.serve()``。

    重构 CLI 参数或漏传 kwargs 时这条会红（不需要真的起服务）。
    """
    from multiagent import cli

    parser = cli.build_parser()
    args = parser.parse_args(["serve", "--host", "127.0.0.1", "--port", "9999", "--token", "tk-123", "--strict-auth"])
    assert args.func is cli.cmd_serve
    assert args.token == "tk-123"
    assert args.strict_auth is True

    plain = parser.parse_args(["serve"])
    assert plain.token is None
    assert plain.strict_auth is False

    captured: dict[str, object] = {}

    def fake_serve(app, **kwargs):  # noqa: ANN001, ANN003
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr("multiagent.api.app.serve", fake_serve)
    assert cli.cmd_serve(wb, args) == 0
    assert captured["app"] is wb
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9999
    assert captured["open_browser"] is False
    assert captured["token"] == "tk-123"
    assert captured["strict"] is True

    captured.clear()
    assert cli.cmd_serve(wb, plain) == 0
    # 未显式指定时必须是 None（交给 env / policy 决定），不能被压成 False
    assert captured["strict"] is None
    assert captured["token"] is None


def test_token_file_generated_then_reused(wb):
    """自动生成的 token 落在 <home>/.multiagent/token，第二次读取复用同一值。"""
    first = build_policy(wb, host="0.0.0.0", env={})
    path = token_file_path(wb.config.data_dir)
    assert path.exists()
    assert first.token_source == "generated"
    assert path.read_text(encoding="utf-8").strip() == first.token

    second = build_policy(wb, host="0.0.0.0", env={})
    assert second.token_source == "file"
    assert second.token == first.token

    if os.name != "nt":
        assert (path.stat().st_mode & 0o077) == 0  # 0600


def test_websocket_token_required_in_remote_mode(wb):
    client = _client(wb, host="0.0.0.0")
    token = token_file_path(wb.config.data_dir).read_text(encoding="utf-8").strip()
    auth = {"Authorization": f"Bearer {token}"}
    pid = client.post("/api/projects", json={"name": "ws-token"}, headers=auth).json()["id"]

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(f"/ws?project={pid}"):
            pass
    assert excinfo.value.code == 1008

    with client.websocket_connect(f"/ws?project={pid}&token={token}") as ws:
        message = ws.receive_json()
        assert "type" in message


# ===========================================================================
# 3) CORS
# ===========================================================================


def test_no_cors_headers_by_default(wb):
    client = _client(wb)
    res = client.get("/api/health", headers={"Origin": "http://testserver"})
    assert "access-control-allow-origin" not in {k.lower() for k in res.headers}
    assert "access-control-allow-credentials" not in {k.lower() for k in res.headers}


def test_wildcard_with_credentials_is_rejected():
    policy = SecurityPolicy(allow_origins=("*",), allow_credentials=True)
    with pytest.raises(ValueError, match="allow_credentials"):
        policy.cors_kwargs()


def test_wildcard_alone_drops_credentials():
    policy = SecurityPolicy(allow_origins=("*",), allow_credentials=True)
    with pytest.raises(ValueError):
        policy.cors_kwargs()
    plain = SecurityPolicy(allow_origins=("*",))
    kwargs = plain.cors_kwargs()
    assert kwargs["allow_credentials"] is False


def test_create_api_refuses_unsafe_cors(wb):
    policy = SecurityPolicy(allow_origins=("*",), allow_credentials=True)
    with pytest.raises(ValueError):
        create_api(wb, policy=policy)


def test_explicit_origin_allowed(wb, monkeypatch):
    monkeypatch.setenv("MULTIAGENT_ALLOW_ORIGINS", "http://app.example")
    client = TestClient(create_api(wb, env=dict(os.environ)))
    res = client.get("/api/health", headers={"Origin": "http://app.example"})
    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") == "http://app.example"
    assert client.post("/api/projects", json={"name": "cors-ok"}, headers={"Origin": "http://app.example"}).status_code == 200


# ===========================================================================
# 4) 路径穿越 / 边界
# ===========================================================================


def test_fs_list_blocks_private_data_dirs(wb, home):
    """私有数据目录（token、SQLite、运行现场）不可被枚举。"""
    client = _client(wb)
    assert client.get(f"/api/fs/list?path={home}").status_code == 200  # 正常浏览不受影响
    for blocked in (home / ".multiagent", home / "config", home / ".multiagent" / "runtime"):
        res = client.get(f"/api/fs/list?path={blocked}")
        assert res.status_code == 403, blocked
        assert res.json()["error_code"] == "path_out_of_bounds"


def test_fs_list_respects_configured_roots(wb, tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    monkeypatch.setenv("MULTIAGENT_FS_ROOTS", str(allowed))
    client = TestClient(create_api(wb, env=dict(os.environ)))
    assert client.get(f"/api/fs/list?path={allowed}").status_code == 200
    assert client.get(f"/api/fs/list?path={outside}").status_code == 403
    # 符号链接/`..` 规范化后仍在允许根内 → 放行；弹出根外 → 拒绝
    assert client.get(f"/api/fs/list?path={allowed / '..' / 'allowed'}").status_code == 200
    assert client.get(f"/api/fs/list?path={allowed / '..'}").status_code == 403


def test_import_private_dir_rejected(wb, home, tmp_path):
    client = _client(wb)
    res = client.post("/api/projects/import", json={"path": str(home / ".multiagent")})
    assert res.status_code == 403

    normal = tmp_path / "src-project"
    normal.mkdir()
    (normal / "a.txt").write_text("x", encoding="utf-8")
    ok = client.post("/api/projects/import", json={"path": str(normal)})
    assert ok.status_code == 200, ok.text
    assert ok.json()["root_path"] == str(normal.resolve())

    missing = client.post("/api/projects/import", json={"path": str(normal / "nope")})
    assert missing.status_code == 400


@pytest.mark.parametrize(
    "name",
    ["..%2Fevil.json", "..%5Cevil.json", "%2e%2e%2fpolicy.json", "..\\policy.json", "/etc/passwd", "C:%5Cevil.json"],
)
def test_config_write_traversal_rejected(wb, home, name):
    client = _client(wb)
    sentinel = home / "evil.json"
    sentinel.write_text('{"pwned": true}', encoding="utf-8")
    res = client.put(f"/api/config/files/{name}", json={"content": {"x": 1}})
    assert res.status_code in (400, 404), res.text
    assert sentinel.read_text(encoding="utf-8") == '{"pwned": true}'
    policy_text = (home / "config" / "policy.json").read_text(encoding="utf-8")
    assert '"x": 1' not in policy_text


def test_run_output_refuses_path_outside_data_dir(wb, project):
    """数据库行被污染指向工作台外的文件时，不得读回内容。"""
    client = _client(wb)
    secret = wb.config.home / "leak-run.txt"
    secret.write_text("LEAK-SECRET-RUN", encoding="utf-8")
    task = wb.tasks.create(project["id"], "污染行")
    wb.db.insert(
        "runs",
        {
            "id": "run-poisoned",
            "project_id": project["id"],
            "task_id": task["id"],
            "agent_id": "worker",
            "status": "success",
            "started_at": "2026-01-01T00:00:00.000Z",
            "prompt_path": str(secret),
            "stdout_path": str(secret),
            "stderr_path": str(secret / ".." / "leak-run.txt"),
        },
    )
    body = client.get("/api/runs/run-poisoned/output")
    assert body.status_code == 200
    payload = body.json()
    assert "LEAK-SECRET-RUN" not in json.dumps(payload)
    assert "[已拒绝]" in payload["prompt"]


def _poison_hash_pair(wb) -> tuple[str, str, str]:
    """构造一对「相对路径式摘要」，指向 blob 根目录之外的两个文件。

    ``BlobStore.path_for(digest)`` 会做 ``root/digest[:2]/digest``，摘要含 ``../``
    时就能拼出 blob 根之外的路径 —— 若摘要来自被污染的 DB 行即可读任意文件。
    """
    blobs_root = wb.runner.journal.blobs.root
    pair = []
    for name, text in (("leak-api-a.txt", "LEAK-SECRET-DIFF-A"), ("leak-api-b.txt", "LEAK-SECRET-DIFF-B")):
        digest = f"../../{name}"
        victim = Path(os.path.normpath(str(blobs_root / ".." / digest)))
        victim.parent.mkdir(parents=True, exist_ok=True)
        victim.write_text(text, encoding="utf-8")
        pair.append(digest)
    return pair[0], pair[1], "LEAK-SECRET-DIFF"


def test_change_diff_refuses_poisoned_blob_hash(wb, project):
    """file_changes 的 hash 被污染成相对路径时，不得借 blob 根目录穿越读文件。"""
    client = _client(wb)
    before_hash, after_hash, secret = _poison_hash_pair(wb)
    task = wb.tasks.create(project["id"], "毒摘要")
    wb.db.insert(
        "file_changes",
        {
            "project_id": project["id"],
            "task_id": task["id"],
            "rel_path": "poison.txt",
            "action": "modify",
            "before_hash": before_hash,
            "after_hash": after_hash,
            "before_size": 18,
            "after_size": 18,
            "mode": "committed",
            "at": "2026-01-01T00:00:00.000Z",
        },
    )
    res = client.get(f"/api/tasks/{task['id']}/diff?path=poison.txt")
    assert res.status_code == 200
    assert secret not in json.dumps(res.json())


def test_stored_blob_path_helper_rejects_escapes(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    assert validate_stored_path(str(root / "runtime" / "a.txt"), root) is not None
    assert validate_stored_path(str(root / ".." / "outside.txt"), root) is None
    assert validate_stored_path(str(tmp_path / "outside.txt"), root) is None
    assert validate_stored_path("", root) is None
    assert validate_stored_path(None, root) is None


def test_path_boundary_semantics(tmp_path):
    """Windows 语义：大小写、`..`、目录前缀混淆、UNC。"""
    root = tmp_path / "src"
    root.mkdir()
    assert is_within(root / "a.txt", root)
    assert is_within(root, root)
    assert is_within(root / ".." / root.name / "a.txt", root)
    assert not is_within(tmp_path / "srcevil" / "x", root)  # 前缀混淆
    assert not is_within(tmp_path / "other", root)
    if os.name == "nt":
        assert is_within(Path("C:/A/B"), Path("c:/a"))
        assert not is_within(Path("C:/ab"), Path("C:/a"))
    assert is_within(Path(r"\\server\share\x"), Path(r"\\server\share"))
    assert not is_within(Path(r"\\server\other"), Path(r"\\server\share"))


def test_host_and_origin_helpers():
    assert normalize_host("127.0.0.1:8770") == "127.0.0.1"
    assert normalize_host("[::1]:8770") == "::1"
    assert normalize_host("EVIL.example:80") == "evil.example"
    assert split_host_port("[::1]:8770") == ("::1", 8770)
    assert split_host_port("127.0.0.1") == ("127.0.0.1", None)

    policy = SecurityPolicy()
    assert policy.origin_ok(None, "127.0.0.1:8770")  # curl 等无 Origin 客户端
    assert policy.origin_ok("http://127.0.0.1:8770", "127.0.0.1:8770")
    assert policy.origin_ok("http://127.0.0.1", "127.0.0.1:80")  # 默认端口对齐
    assert not policy.origin_ok("http://127.0.0.1:9999", "127.0.0.1:8770")
    assert not policy.origin_ok("https://evil.example", "127.0.0.1:8770")
    assert not policy.origin_ok("null", "127.0.0.1:8770")
    assert not policy.origin_ok("file:///x", "127.0.0.1:8770")

    assert extract_token({"authorization": "bearer abc"}, {}) == "abc"
    assert extract_token({}, {"token": "xyz"}) == "xyz"
    assert extract_token({}, {}) == ""


# ===========================================================================
# 5) 错误处理与安全响应头
# ===========================================================================


def test_error_envelope_shape(wb):
    client = _client(wb)
    missing = client.get("/api/tasks/nope")
    assert missing.status_code == 404
    body = missing.json()
    assert body["error_code"] == "not_found"
    assert body["error"] and body["detail"] == body["error"]

    bad = client.post("/api/projects", json={})
    assert bad.status_code == 400
    assert bad.json()["error_code"] == "invalid_request"

    unknown = client.put("/api/config/files/nope.json", json={"content": {}})
    assert unknown.status_code == 400
    assert unknown.json()["error_code"] == "invalid_request"


def test_internal_error_hides_stack_and_paths(wb, monkeypatch):
    def boom():
        raise RuntimeError(f"内部炸了：{wb.config.home}\\secret.db")

    monkeypatch.setattr(wb.projects, "list", boom)
    client = TestClient(create_api(wb, env={}), raise_server_exceptions=False)
    res = client.get("/api/projects")
    assert res.status_code == 500
    body = res.json()
    assert body["error_code"] == "internal_error"
    text = json.dumps(body)
    assert str(wb.config.home) not in text
    assert "RuntimeError" not in text
    assert "Traceback" not in text


def test_security_response_headers(wb):
    client = _client(wb)
    page = client.get("/")
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_health_reports_security_summary(wb):
    info = _client(wb).get("/api/health").json()["security"]
    assert info["loopback_only"] is True
    assert info["auth_required"] is False
    assert info["bind_host"] == "127.0.0.1"


# ===========================================================================
# 6) 前端 XSS 静态回归护栏
# ===========================================================================

_TAINTED = re.compile(
    r"(?<![\w$.])(?:e|h|a|c|r|s|n|t|v|it|d)\.(?:body|title|did|not_done|rel_path|agent_id|summary|name|path|prompt|stdout|stderr|note|actor|blocked_reason|last_error)"
    r"|\b(?:text|line)\b"
)


def _interpolations(src: str) -> list[tuple[int, str]]:
    """扫描模板字符串里的 ``${...}``（按大括号配平），返回 (行号, 表达式)。"""
    found: list[tuple[int, str]] = []
    index = 0
    while True:
        index = src.find("${", index)
        if index < 0:
            break
        depth, cursor = 1, index + 2
        while cursor < len(src) and depth > 0:
            char = src[cursor]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
            cursor += 1
        found.append((src[:index].count("\n") + 1, src[index + 2: cursor - 1].strip()))
        index = cursor
    return found


def unescaped_interpolations(src: str) -> list[tuple[int, str]]:
    """找出「引用了不可信字段但没走 esc()/encodeURIComponent()」的插值。

    三元表达式里只出现在**条件**中的字段不构成注入（例如
    ``${t.agent_id === a.id ? "selected" : ""}`` 只输出静态字符串）。
    """
    bad: list[tuple[int, str]] = []
    for line, expr in _interpolations(src):
        if not _TAINTED.search(expr):
            continue
        if "esc(" in expr or "encodeURIComponent(" in expr:
            continue
        if not _reaches_output(expr):
            continue
        bad.append((line, expr))
    return bad


def _reaches_output(expr: str) -> bool:
    depth = 0
    question = -1
    for index, char in enumerate(expr):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "?" and depth == 0:
            question = index
            break
    if question == -1:
        return True
    return bool(_TAINTED.search(expr[question + 1:]))


def test_appjs_guard_detects_unescaped_output():
    """护栏自检：合成一段未转义插值，必须被判定为不安全。"""
    sample = 'box.innerHTML = `<div>${esc(e.title)}</div><div>${e.body}</div>`;'
    bad = unescaped_interpolations(sample)
    assert bad and bad[0][1] == "e.body"


def test_appjs_tainted_interpolations_are_escaped():
    """Agent 输出 / 任务标题 / 黑板正文 / 交接 / 事件流等不可信字段必须转义。"""
    assert unescaped_interpolations(APP_JS.read_text(encoding="utf-8")) == []


def test_appjs_attributes_and_urls_are_safe():
    src = APP_JS.read_text(encoding="utf-8")
    # data-* 属性拼接必须走 esc()（属性上下文注入）
    assert not re.search(r'data-(?:id|path|task|dep|file|dir|ok)="\$\{(?!esc\()', src)
    # 不允许内联事件处理器或 javascript: URL
    assert not re.search(r"\.setAttribute\(\s*['\"]on", src)
    assert "javascript:" not in src
    assert "insertAdjacentHTML" not in src
    assert "document.write" not in src
    # API 路径参数必须编码
    assert re.search(r"encodeURIComponent\(", src)


def test_index_html_has_no_inline_script_or_handlers():
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


def test_frontend_sends_token_when_present():
    """前端必须支持严格模式：注入 Authorization / WS query token。"""
    src = APP_JS.read_text(encoding="utf-8")
    assert "Authorization" in src
    assert "Bearer" in src
    assert "token=" in src


# ===========================================================================
# 7) 项目级停止：只终止本项目，不误伤别的项目（需求 10 隔离 / 需求 54 终止执行）
# ===========================================================================


def _running_task(wb, project: dict, title: str, run_id: str) -> tuple[dict, object]:
    """造一个「正在跑」的任务 + 一条 running 的 run 行，并登记取消信号（模拟进程内在跑）。

    返回 ``(task, cancel_event)``：``cancel_event`` 就是 runner 塞进
    ``AgentRequest.cancel_event`` 的那个 ``threading.Event``，适配器轮询它来决定是否整树
    回收子进程 —— 因此断言它是否被 set，等价于「Agent 会不会被杀」。
    """
    task = wb.tasks.create(project["id"], title)
    wb.tasks.set_status(task["id"], "running")
    wb.db.insert(
        "runs",
        {
            "id": run_id,
            "project_id": project["id"],
            "task_id": task["id"],
            "agent_id": "worker",
            "status": "running",
            "started_at": "2026-01-01T00:00:00.000Z",
        },
    )
    event = wb.engine.run_cancels.register(run_id)
    return task, event


def test_stop_endpoint_calls_project_scoped_stop(wb, project, monkeypatch):
    """路由必须走 ``engine.stop_project``，绝不能再调全局急停 ``engine.shutdown()``。"""
    calls: list[tuple] = []
    shutdown_calls: list[int] = []

    def fake_stop_project(project_id, **kwargs):  # noqa: ANN001, ANN003
        calls.append((project_id, kwargs))
        return {"project_id": project_id, "stopped": True, "runs_cancelled": 2, "tasks_cancelled": 3}

    monkeypatch.setattr(wb.engine, "stop_project", fake_stop_project)
    monkeypatch.setattr(wb.engine, "shutdown", lambda *a, **k: shutdown_calls.append(1))

    body = _client(wb).post(f"/api/projects/{project['id']}/stop", json={}).json()
    assert calls and calls[0][0] == project["id"]  # 项目级、且带的是本项目 id
    assert shutdown_calls == []  # 路由不再触发全局急停
    assert body["stopped"] is True  # 既有契约（test_api_control 依赖）
    assert body["runs_cancelled"] == 2 and body["tasks_cancelled"] == 3  # 引擎结果透传
    assert "2" in body["message"] and "3" in body["message"]  # 前端可直接提示


def test_stop_project_does_not_touch_other_projects(wb, set_plan):
    """只停 A：A 被真终止；**B 的取消信号未被置位、B 的 Agent 还能跑完并回收产物**。

    断言分三层（缺一不可）：

    1. **取消信号层** —— 直接断言 runner 塞进 ``AgentRequest.cancel_event`` 的那个
       ``threading.Event`` 本体。旧实现 ``stop_project`` 内部调 ``shutdown()``，后者不带
       project 过滤地 ``cancel_many(_running_run_ids())``，会把 B 的信号一起 set（B 的 Agent
       被整树杀掉），而 B 的 task 状态恰好仍是 ``running`` —— **只看状态会假绿**。
    2. **DB 状态层** —— A 的任务 ``cancelled``，B 的任务仍是 ``running``。
    3. **进程行为层** —— 停 A 之后 B 的 Agent 仍然能正常跑完（假 CLI Agent 真跑一次），
       产物同步回工作区并被登记为 output 产物。
    """
    client = _client(wb)
    project_a = wb.projects.create("stop-A")
    project_b = wb.projects.create("stop-B")
    task_a, event_a = _running_task(wb, project_a, "A 在跑", "run-stop-A")
    task_b, event_b = _running_task(wb, project_b, "B 在跑", "run-stop-B")

    body = client.post(f"/api/projects/{project_a['id']}/stop", json={}).json()

    # --- 1/2. A：被真终止
    assert body["stopped"] is True
    assert body["runs_cancelled"] == 1  # 只算 A 的在跑 run
    assert body["tasks_cancelled"] == 1
    assert event_a.is_set() is True
    assert wb.tasks.get(task_a["id"])["status"] == "cancelled"

    # --- 1. B：取消信号本体必须保持未置位（这条才是真正的隔离护栏）
    assert event_b.is_set() is False, "停 A 不得 set B 的 run 取消信号（B 的 Agent 会被误杀）"
    assert wb.engine.run_cancels.is_set("run-stop-B") is False
    # --- 2. B：DB 状态不受影响
    assert wb.tasks.get(task_b["id"])["status"] == "running"

    # --- 3. B：Agent 仍能跑完 + 产物正常回收
    set_plan("worker", {"files": {"b-out.txt": "B 的产物"}, "handoff": {"did": "B 完成"}, "exit": 0})
    follow_up = client.post(
        f"/api/projects/{project_b['id']}/tasks",
        json={
            "title": "B 的后续任务",
            "kind": "generic",
            "agent_id": "worker",
            "acceptance": [{"type": "file_exists", "path": "b-out.txt"}],
        },
    ).json()
    run = client.post(f"/api/tasks/{follow_up['id']}/run", json={"wait": 60}).json()
    assert run["status"] == "success"
    assert wb.tasks.get(follow_up["id"])["status"] == "success"
    root = Path(wb.projects.get(project_b["id"])["root_path"])
    assert (root / "b-out.txt").read_text(encoding="utf-8").strip() == "B 的产物"
    artifacts = client.get(f"/api/projects/{project_b['id']}/artifacts").json()
    assert any(a["rel_path"] == "b-out.txt" and a["role"] == "output" for a in artifacts)


def test_frontend_stop_copy_matches_real_termination():
    """控制台文案不能再说「跑完后停下」——现在是真终止。"""
    js = APP_JS.read_text(encoding="utf-8")
    api_py = (Path(__file__).resolve().parents[1] / "multiagent" / "api" / "app.py").read_text(encoding="utf-8")
    assert "跑完后停下" not in js
    assert "跑完后停下" not in api_py
    assert "已请求停止" not in js
    assert "res.message" in js  # 停止结果用后端消息（含终止数量）提示
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'title="停止当前项目' in html


# ===========================================================================
# 8) 变异自检：把对应防护关掉，上面每条断言立刻复现「修复前」的漏洞。
#    作用有两个：证明断言不是空转；把「修复前行为」固化成可重跑的回归证据。
# ===========================================================================


def test_mutation_without_origin_guard(wb, monkeypatch):
    monkeypatch.setattr(SecurityPolicy, "origin_ok", lambda self, origin, host: True)
    client = _client(wb)
    res = client.post("/api/projects", json={"name": "legacy"}, headers={"Origin": "http://evil.example"})
    assert res.status_code == 200  # 修复前：跨站网页可以直接驱动本机 Agent


def test_mutation_without_host_guard(wb, monkeypatch):
    monkeypatch.setattr(SecurityPolicy, "host_ok", lambda self, host: True)
    client = TestClient(create_api(wb, env={}), base_url="http://evil.example")
    assert client.get("/api/health").status_code == 200  # 修复前：DNS rebinding 可用


def test_mutation_without_token_guard(wb, monkeypatch):
    monkeypatch.setattr(SecurityPolicy, "token_ok", lambda self, provided: True)
    client = _client(wb, host="0.0.0.0")
    assert client.post("/api/projects", json={"name": "legacy"}).status_code == 200


def test_mutation_without_path_boundary(wb, home, monkeypatch):
    monkeypatch.setattr(SecurityPolicy, "assert_path", lambda self, path, kind="目录": path)
    client = _client(wb)
    assert client.get(f"/api/fs/list?path={home / '.multiagent'}").status_code == 200


def test_mutation_without_run_output_boundary(wb, project, monkeypatch):
    monkeypatch.setattr(
        "multiagent.api.app.validate_stored_path",
        lambda value, root: Path(value) if value else None,
    )
    client = _client(wb)
    secret = wb.config.home / "leak-run.txt"
    secret.write_text("LEAK-SECRET-RUN", encoding="utf-8")
    task = wb.tasks.create(project["id"], "污染行")
    wb.db.insert(
        "runs",
        {
            "id": "run-legacy",
            "project_id": project["id"],
            "task_id": task["id"],
            "agent_id": "worker",
            "status": "success",
            "started_at": "2026-01-01T00:00:00.000Z",
            "prompt_path": str(secret),
        },
    )
    body = client.get("/api/runs/run-legacy/output").json()
    assert "LEAK-SECRET-RUN" in json.dumps(body)


def test_mutation_without_blob_digest_check(wb, project, monkeypatch):
    monkeypatch.setattr(
        "multiagent.api.app._read_blob",
        lambda blobs, digest: blobs.read_bytes(str(digest)) if digest else None,
    )
    client = _client(wb)
    before_hash, after_hash, secret = _poison_hash_pair(wb)
    task = wb.tasks.create(project["id"], "毒摘要")
    wb.db.insert(
        "file_changes",
        {
            "project_id": project["id"],
            "task_id": task["id"],
            "rel_path": "poison.txt",
            "action": "modify",
            "before_hash": before_hash,
            "after_hash": after_hash,
            "mode": "committed",
            "at": "2026-01-01T00:00:00.000Z",
        },
    )
    res = client.get(f"/api/tasks/{task['id']}/diff?path=poison.txt")
    assert secret in json.dumps(res.json())


def test_mutation_without_websocket_origin_guard(wb, monkeypatch):
    monkeypatch.setattr(SecurityPolicy, "origin_ok", lambda self, origin, host: True)
    client = _client(wb)
    pid = _new_project(client, "ws-legacy")
    with client.websocket_connect(f"/ws?project={pid}", headers={"Origin": "http://evil.example"}) as ws:
        assert "type" in ws.receive_json()


def test_mutation_legacy_stop_project_globally_kills_other_projects(wb, monkeypatch):
    """变异自检：把 ``stop_project`` 退回「内部调 shutdown()」的旧实现，B 的取消信号立刻被置位。

    这就是 task-8 修掉的那条跨项目误伤：旧实现停 A 时会全局 ``cancel_many``，
    B 正在跑的 Agent 被整树杀掉，而 B 的 task 状态仍然显示 ``running``（所以只看状态会假绿）。
    """
    project_a = wb.projects.create("legacy-A")
    project_b = wb.projects.create("legacy-B")
    _task_a, event_a = _running_task(wb, project_a, "A 在跑", "run-legacy-A")
    _task_b, event_b = _running_task(wb, project_b, "B 在跑", "run-legacy-B")

    engine = wb.engine
    real_stop_project = engine.stop_project

    def legacy_stop_project(project_id, reason="项目停止"):  # 旧实现：先全局急停再按项目收尾
        engine.shutdown()
        return real_stop_project(project_id, reason)

    monkeypatch.setattr(engine, "stop_project", legacy_stop_project)
    _client(wb).post(f"/api/projects/{project_a['id']}/stop", json={}).json()

    assert event_a.is_set() is True
    assert event_b.is_set() is True  # 旧实现下 B 的 Agent 会被误杀 —— 正是被修复的缺陷
