"""指挥台新接口测试（契约 ``docs/UI-OPS-REDESIGN.md`` §3）。

覆盖：

* ``GET /api/projects/{pid}/dashboard``：形状、聚合正确性、100 任务规模 ≤150ms、
  **不触发任何子进程探测**（这是首屏卡顿的历史根因）；
* ``POST /api/projects/{pid}/quick-task``：single / plan 两种模式、kind=auto 推断、
  标题首行截 60 字、验收默认（outputs→file_exists，否则 exit_zero）、start 后台调度；
* ``PATCH /api/projects/{pid}``：改名 / 改显示名、唯一性、中文保真；
* ``/api/agents/detail`` 永不 500、``/api/agents/{id}/check`` 超时 ≤10s；
* 静态资源 ``no-cache`` / API ``no-store``。
"""

from __future__ import annotations

import json
import statistics
import sys
import threading
import time

import pytest
from fastapi.testclient import TestClient

import multiagent.api.app as api_mod
from multiagent.agents.acp_adapter import AcpAdapter
from multiagent.api.app import create_api

#: 契约 §3.1 的单请求上限（100 任务规模）。
DASHBOARD_BUDGET_MS = 150.0


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _new_project(client: TestClient, name: str = "指挥台项目") -> str:
    return client.post("/api/projects", json={"name": name}).json()["id"]


def _seed_run(wb, project_id: str, task_id: str, *, duration_ms: int, tokens: int, cost: float) -> None:
    """插一条 run 行（统计口径用；不真跑 Agent，保持测试快且确定）。"""
    wb.db.insert(
        "runs",
        {
            "id": f"run-test-{task_id}",
            "project_id": project_id,
            "task_id": task_id,
            "agent_id": "worker",
            "status": "success",
            "started_at": "2026-11-06T10:00:00.000Z",
            "finished_at": "2026-11-06T10:00:01.000Z",
            "duration_ms": duration_ms,
            "cost_tokens": tokens,
            "cost_amount": cost,
        },
    )


# ===========================================================================
# 1) dashboard
# ===========================================================================


def test_dashboard_returns_full_first_paint_payload(wb):
    client = _client(wb)
    pid = _new_project(client, "指挥台-形状")
    first = client.post(
        f"/api/projects/{pid}/tasks",
        json={"title": "整理数据", "kind": "data", "agent_id": "worker", "priority": 3, "outputs": ["out.csv"]},
    ).json()
    done = client.post(f"/api/projects/{pid}/tasks", json={"title": "已完成", "kind": "doc"}).json()
    wb.tasks.set_status(done["id"], "success")
    wb.guard.request(project_id=pid, kind="danger", title="危险操作待确认", risk="high")
    wb.board.append_event(project_id=pid, kind="task.success", actor="worker", summary="写完了")
    _seed_run(wb, pid, first["id"], duration_ms=1200, tokens=42, cost=0.25)

    body = client.get(f"/api/projects/{pid}/dashboard").json()

    # 顶层形状（契约 §3.1）
    assert set(body) >= {
        "project", "progress", "tasks", "agents", "approvals",
        "stats", "activity", "running", "workspace", "server_time",
    }
    assert body["project"]["id"] == pid
    assert body["project"]["name"] == "指挥台-形状"
    assert body["project"]["readonly"] is False

    progress = body["progress"]
    assert progress["total"] == 2
    assert progress["counters"]["pending"] == 1
    assert progress["counters"]["success"] == 1
    assert progress["percent"] == 50
    # 全部状态都有键（前端直接索引 counters.xxx 也不会拿到 undefined）
    for status in ("pending", "running", "success", "failed", "blocked", "waiting", "skipped", "cancelled", "interrupted"):
        assert status in progress["counters"]

    tasks = {task["id"]: task for task in body["tasks"]}
    assert tasks[first["id"]]["title"] == "整理数据"
    assert tasks[first["id"]]["kind"] == "data"
    assert tasks[first["id"]]["priority"] == 3
    assert tasks[first["id"]]["read_only"] is False
    for key in ("id", "title", "status", "kind", "agent_id", "priority", "read_only", "updated_at", "blocked_reason"):
        assert key in tasks[first["id"]]

    agent_ids = {agent["id"] for agent in body["agents"]}
    assert {"worker", "coder", "human"} <= agent_ids
    worker = next(agent for agent in body["agents"] if agent["id"] == "worker")
    assert worker["enabled"] is True
    assert worker["protocol"] == "cli"
    assert set(worker["usage"]) == {"invocations", "seconds", "cost", "tokens"}
    assert "max_invocations" in worker["quota"]

    assert body["approvals"]["pending"] == 1
    assert body["approvals"]["items"][0]["title"] == "危险操作待确认"
    assert body["approvals"]["items"][0]["risk"] == "high"

    assert body["stats"]["runs"] == 1
    assert body["stats"]["duration_ms"] == 1200
    assert body["stats"]["tokens"] == 42

    assert body["activity"][0]["summary"] == "写完了"
    assert body["activity"][0]["kind"] == "task.success"
    assert len(body["activity"]) <= 8

    assert body["running"] is False
    assert isinstance(body["workspace"]["files"], int)
    assert isinstance(body["workspace"]["bytes"], int)
    assert isinstance(body["server_time"], str)


def test_dashboard_running_true_when_task_running(wb):
    client = _client(wb)
    pid = _new_project(client, "指挥台-运行中")
    task = client.post(f"/api/projects/{pid}/tasks", json={"title": "跑着的任务"}).json()
    wb.tasks.set_status(task["id"], "running")
    assert client.get(f"/api/projects/{pid}/dashboard").json()["running"] is True


def test_dashboard_100_tasks_under_budget(wb):
    """契约 §3.1：100 任务规模单请求 ≤150ms。"""
    client = _client(wb)
    pid = _new_project(client, "指挥台-100")
    for index in range(100):
        wb.tasks.create(pid, f"任务 {index}", kind="generic", agent_id="worker")

    client.get(f"/api/projects/{pid}/dashboard")  # 预热（路由/连接/缓存）
    samples: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        res = client.get(f"/api/projects/{pid}/dashboard")
        samples.append((time.perf_counter() - started) * 1000)
        assert res.status_code == 200

    body = res.json()
    assert body["progress"]["total"] == 100
    assert len(body["tasks"]) == 100
    median = statistics.median(samples)
    assert median <= DASHBOARD_BUDGET_MS, f"dashboard 中位耗时 {median:.1f}ms 超过 {DASHBOARD_BUDGET_MS}ms（样本 {samples}）"


def test_dashboard_never_spawns_probe_subprocesses(wb, monkeypatch):
    """dashboard 不得走 ``resolve_agent_argv`` / ``reg.exe`` / 任何 spawn 探测。"""
    import multiagent.agents.proc as procmod
    import multiagent.agents.resolve as resolve_mod

    def boom(*_args, **_kwargs):
        raise AssertionError("dashboard 路径不允许启动子进程探测")

    monkeypatch.setattr(procmod, "spawn", boom)
    monkeypatch.setattr(resolve_mod.subprocess, "run", boom)
    monkeypatch.setattr(resolve_mod, "_find_dsh", boom)

    client = _client(wb)
    pid = _new_project(client, "指挥台-无探测")
    wb.tasks.create(pid, "任务", agent_id="worker")

    res = client.get(f"/api/projects/{pid}/dashboard")
    assert res.status_code == 200
    assert {agent["id"] for agent in res.json()["agents"]} >= {"worker", "coder", "human"}


# ===========================================================================
# 2) quick-task
# ===========================================================================


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("修复代码里的空指针报错并补单元测试", "code"),
        ("撰写一份项目总结报告", "doc"),
        ("调研三个竞品并输出竞品分析.md", "research"),
        ("审阅文档并给出修改建议", "review"),
        ("整理 Excel 表格数据", "data"),
        ("你好，帮我看看这个", "generic"),
        # 平手时「先出现者」胜：动作词在前的审阅意图优先于后面的「文档」
        ("审阅这份文档", "review"),
    ],
)
def test_quicktask_kind_auto_inference(wb, text, expected):
    client = _client(wb)
    pid = _new_project(client, f"推断-{expected}-{abs(hash(text)) % 1000}")
    res = client.post(f"/api/projects/{pid}/quick-task", json={"text": text})
    assert res.status_code == 200, res.text
    task_id = res.json()["tasks"][0]["id"]
    assert client.get(f"/api/tasks/{task_id}").json()["task"]["kind"] == expected


def test_quicktask_single_mode_contract(wb):
    client = _client(wb)
    pid = _new_project(client, "一句话-single")
    text = "调研三个竞品并输出竞品分析.md\n要求：对比功能与定价"
    res = client.post(f"/api/projects/{pid}/quick-task", json={"text": text})

    assert res.status_code == 200
    body = res.json()
    assert body["project_id"] == pid
    assert body["mode"] == "single"
    assert body["started"] is False
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["status"] == "pending"
    assert "已创建 1 个任务" in body["message"]

    task = client.get(f"/api/tasks/{body['tasks'][0]['id']}").json()["task"]
    # text 是用户原话：完整进 brief（不截断），标题只取首行
    assert task["brief"] == text
    assert task["title"] == "调研三个竞品并输出竞品分析.md"
    assert task["kind"] == "research"
    # 无 outputs → 验收默认 exit_zero
    assert task["acceptance"] == [{"type": "exit_zero"}]
    assert task["outputs"] == []


def test_quicktask_title_from_first_line_truncated_to_60(wb):
    client = _client(wb)
    pid = _new_project(client, "一句话-标题")
    long_line = "标题" * 40  # 80 字
    text = f"{long_line}\n第二行不该进标题"
    task = client.post(f"/api/projects/{pid}/quick-task", json={"text": text}).json()["tasks"][0]
    assert task["title"] == long_line[:60]
    assert len(task["title"]) == 60

    # 显式 title 优先
    explicit = client.post(
        f"/api/projects/{pid}/quick-task", json={"text": "随便写点什么", "title": "我自己的标题"}
    ).json()["tasks"][0]
    assert explicit["title"] == "我自己的标题"


def test_quicktask_outputs_become_file_exists_acceptance(wb):
    client = _client(wb)
    pid = _new_project(client, "一句话-产物")
    body = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": "写两个文件", "outputs": ["a.md", "b.md"]},
    ).json()
    task = client.get(f"/api/tasks/{body['tasks'][0]['id']}").json()["task"]
    assert task["outputs"] == ["a.md", "b.md"]
    assert task["acceptance"] == [
        {"type": "file_exists", "path": "a.md"},
        {"type": "file_exists", "path": "b.md"},
    ]

    # 也接受「逗号分隔的字符串」（前端输入框原样提交时不会 500）
    body = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": "再写一个", "outputs": "c.md， d.md"},
    ).json()
    task = client.get(f"/api/tasks/{body['tasks'][0]['id']}").json()["task"]
    assert task["outputs"] == ["c.md", "d.md"]


def test_quicktask_plan_mode_builds_three_step_dag(wb):
    client = _client(wb)
    pid = _new_project(client, "一句话-plan")
    res = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": "调研竞品并写一份报告", "mode": "plan", "outputs": ["报告.md"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["mode"] == "plan"
    assert body["started"] is False
    assert len(body["tasks"]) == 3
    assert "调研整理" in body["message"]

    ids = [item["id"] for item in body["tasks"]]
    details = [client.get(f"/api/tasks/{tid}").json() for tid in ids]
    kinds = [detail["task"]["kind"] for detail in details]
    assert kinds == ["research", "doc", "review"]

    # 依赖：②→①、③→②，模式 success
    assert details[0]["dependencies"] == []
    assert [dep["id"] for dep in details[1]["dependencies"]] == [ids[0]]
    assert details[1]["dependencies"][0]["mode"] == "success"
    assert [dep["id"] for dep in details[2]["dependencies"]] == [ids[1]]

    # ①调研：无产物 → exit_zero；②初稿：产物 → file_exists；③审阅：只读 + review 模板验收
    assert details[0]["task"]["acceptance"] == [{"type": "exit_zero"}]
    assert details[1]["task"]["outputs"] == ["报告.md"]
    assert details[1]["task"]["acceptance"] == [{"type": "file_exists", "path": "报告.md"}]
    assert details[2]["task"]["read_only"] is True
    assert {check["type"] for check in details[2]["task"]["acceptance"]} == {"no_writes", "handoff_present"}

    # 看板/计划视图也能看到这条 DAG
    plan = client.get(f"/api/projects/{pid}/kanban").json()
    assert len(plan["edges"]) == 2


def test_quicktask_start_runs_engine_in_background(wb, monkeypatch):
    calls: list[tuple[str, dict]] = []
    entered = threading.Event()
    release = threading.Event()

    def fake_run_project(project_id, **kwargs):
        calls.append((project_id, kwargs))
        entered.set()
        release.wait(15)
        return {"project_id": project_id, "dispatched": 0}

    monkeypatch.setattr(wb.engine, "run_project", fake_run_project)
    client = _client(wb)
    pid = _new_project(client, "一句话-start")

    first = client.post(f"/api/projects/{pid}/quick-task", json={"text": "写一份报告", "start": True})
    assert first.status_code == 200
    assert first.json()["started"] is True
    assert entered.wait(10), "start=true 没有启动后台 engine.run_project"
    assert client.get(f"/api/projects/{pid}/run-status").json()["running"] is True
    assert client.get(f"/api/projects/{pid}/dashboard").json()["running"] is True

    # 同一项目已有调度在跑：不重复启动（与 /api/projects/{pid}/run 同一把闸门）
    second = client.post(f"/api/projects/{pid}/quick-task", json={"text": "再来一个", "start": True})
    assert second.json()["started"] is False
    assert "已有调度在运行" in second.json()["message"]
    # 任务仍然建出来了（start 只是调度开关）
    assert len(second.json()["tasks"]) == 1

    release.set()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if client.get(f"/api/projects/{pid}/run-status").json()["running"] is False:
            break
        time.sleep(0.05)
    assert client.get(f"/api/projects/{pid}/run-status").json()["running"] is False
    assert len(calls) == 1
    assert calls[0][0] == pid


def test_quicktask_start_false_does_not_schedule(wb, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("start=false 不应启动调度")

    monkeypatch.setattr(wb.engine, "run_project", boom)
    client = _client(wb)
    pid = _new_project(client, "一句话-不启动")
    res = client.post(f"/api/projects/{pid}/quick-task", json={"text": "只是建个任务"})
    assert res.status_code == 200
    assert res.json()["started"] is False
    assert client.get(f"/api/projects/{pid}/run-status").json()["running"] is False


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ({}, 400),                                   # 缺 text
        ({"text": "   "}, 400),                      # 空白 text
        ({"text": "x" * 20001}, 400),                # 超长 text
        ({"text": "任务", "mode": "batch"}, 400),     # 非法 mode
        ({"text": "任务", "kind": "nope"}, 400),      # 非法 kind
        ({"text": "任务", "agent_id": "ghost"}, 400),  # 未配置的 Agent
        ({"text": "任务", "outputs": 123}, 400),      # outputs 类型错误
        ({"text": "任务", "outputs": ["x" * 513]}, 400),                      # 单个产物路径过长
        ({"text": "任务", "outputs": [f"f{i}.md" for i in range(51)]}, 400),   # 产物数量过多
    ],
)
def test_quicktask_rejects_bad_requests(wb, payload, status):
    client = _client(wb)
    pid = _new_project(client, f"一句话-校验-{status}-{abs(hash(str(payload))) % 10000}")
    res = client.post(f"/api/projects/{pid}/quick-task", json=payload)
    assert res.status_code == status, res.text
    assert res.json()["error_code"] == "invalid_request"


def test_quicktask_unknown_project_is_404(wb):
    client = _client(wb)
    res = client.post("/api/projects/nope/quick-task", json={"text": "任务"})
    assert res.status_code == 404
    assert res.json()["error_code"] == "not_found"


def test_quicktask_accepts_explicit_agent_and_kind(wb):
    client = _client(wb)
    pid = _new_project(client, "一句话-显式")
    body = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": "帮我看看这个", "kind": "doc", "agent_id": "coder", "read_only": True},
    ).json()
    task = client.get(f"/api/tasks/{body['tasks'][0]['id']}").json()["task"]
    assert task["kind"] == "doc"
    assert task["agent_id"] == "coder"
    assert task["read_only"] is True

    auto = client.post(
        f"/api/projects/{pid}/quick-task", json={"text": "帮我看看这个", "agent_id": "auto"}
    ).json()
    assert client.get(f"/api/tasks/{auto['tasks'][0]['id']}").json()["task"]["agent_id"] is None


# ===========================================================================
# 3) PATCH /api/projects/{pid}
# ===========================================================================


def test_patch_project_rename_and_label(wb):
    client = _client(wb)
    pid = _new_project(client, "旧名字-乱码修复")
    res = client.patch(f"/api/projects/{pid}", json={"name": "新名字·竞品调研", "label": "竞品调研"})
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == pid
    assert body["name"] == "新名字·竞品调研"
    assert body["label"] == "竞品调研"

    assert client.get(f"/api/projects/{pid}").json()["project"]["name"] == "新名字·竞品调研"
    listed = client.get("/api/projects").json()
    assert any(item["id"] == pid and item["name"] == "新名字·竞品调研" for item in listed)

    # 只改 label：空 label 回退成项目名（避免界面出现空标题）
    only_label = client.patch(f"/api/projects/{pid}", json={"label": ""}).json()
    assert only_label["label"] == "新名字·竞品调研"
    assert only_label["name"] == "新名字·竞品调研"


def test_patch_project_rejects_duplicate_and_empty(wb):
    client = _client(wb)
    first = _new_project(client, "项目甲")
    second = _new_project(client, "项目乙")

    clash = client.patch(f"/api/projects/{second}", json={"name": "项目甲"})
    assert clash.status_code == 409
    assert clash.json()["error_code"] == "conflict"

    empty = client.patch(f"/api/projects/{first}", json={"name": "   "})
    assert empty.status_code == 400

    too_long = client.patch(f"/api/projects/{first}", json={"label": "x" * 201})
    assert too_long.status_code == 400

    missing = client.patch("/api/projects/nope", json={"name": "x"})
    assert missing.status_code == 404

    # 不改任何字段：原样返回，不报错
    same = client.patch(f"/api/projects/{first}", json={})
    assert same.status_code == 200 and same.json()["name"] == "项目甲"


# ===========================================================================
# 4) Agent 探测健壮性（契约 §5.4）
# ===========================================================================


def test_agents_detail_never_500_when_resolve_fails(wb, monkeypatch):
    def boom(_agent):
        raise RuntimeError("reg.exe 解码炸了")

    monkeypatch.setattr(api_mod, "resolve_agent_argv", boom)
    client = TestClient(create_api(wb), raise_server_exceptions=False)
    res = client.get("/api/agents/detail")

    assert res.status_code == 200, res.text
    items = {item["id"]: item for item in res.json()}
    assert {"worker", "coder", "human"} <= set(items)
    # 解析失败的那一项降级成带 error 的记录，其余字段仍然完整
    assert items["worker"]["error"]
    assert items["worker"]["command"] is None
    assert "usage" in items["worker"] and "quota" in items["worker"]
    # human 是人工执行者，不依赖命令解析
    assert items["human"]["command"] == "(human)"


def test_agents_detail_never_500_when_quota_fails(wb, monkeypatch):
    def boom():
        raise RuntimeError("额度表坏了")

    monkeypatch.setattr(wb.quota, "summary", boom)
    client = TestClient(create_api(wb), raise_server_exceptions=False)
    res = client.get("/api/agents/detail")
    assert res.status_code == 200
    assert {item["id"] for item in res.json()} >= {"worker", "coder", "human"}


def test_agent_check_uses_10s_timeout_and_never_500(wb, monkeypatch):
    captured: list[float] = []

    class _FakeAdapter:
        def check(self, timeout: float = 20.0):
            captured.append(timeout)
            return {"ok": True, "installed": True, "command": "x"}

    monkeypatch.setattr(api_mod, "create_adapter", lambda agent, resolver: _FakeAdapter())
    client = _client(wb)
    res = client.post("/api/agents/worker/check")
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert captured and captured[0] <= 10.0, captured

    def boom(*_args, **_kwargs):
        raise RuntimeError("适配器构造失败")

    monkeypatch.setattr(api_mod, "create_adapter", boom)
    degraded = client.post("/api/agents/worker/check")
    assert degraded.status_code == 200
    assert degraded.json()["ok"] is False
    assert "探测失败" in degraded.json()["detail"]

    assert client.post("/api/agents/ghost/check").status_code == 404


def test_acp_check_timeout_is_a_hard_limit(tmp_path):
    """握手卡死时不能白等 GRACE_SECONDS —— 探测本身必须是有硬上限的路径。"""
    script = tmp_path / "hang_acp.py"
    script.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    agent = {
        "id": "hang",
        "label": "Hang",
        "protocol": "acp",
        "command": sys.executable,
        "args": [str(script)],
        "enabled": True,
    }
    started = time.monotonic()
    info = AcpAdapter(agent).check(timeout=1.0)
    elapsed = time.monotonic() - started

    assert info["ok"] is False
    assert info["handshake"] == "failed"
    assert elapsed < 2.5, f"探测超时后仍在温柔等待：{elapsed:.2f}s"


# ===========================================================================
# 5) 缓存头（契约 §5.5）
# ===========================================================================


def test_static_no_cache_and_api_no_store(wb):
    client = _client(wb)
    page = client.get("/")
    assert page.headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert client.get("/api/health").headers["cache-control"] == "no-store"
    pid = _new_project(client, "缓存头")
    assert client.get(f"/api/projects/{pid}/dashboard").headers["cache-control"] == "no-store"


def test_quicktask_event_shows_up_in_activity(wb):
    """一句话建任务会写一条事件，首屏「最近活动」立刻能看到。"""
    client = _client(wb)
    pid = _new_project(client, "活动流")
    client.post(f"/api/projects/{pid}/quick-task", json={"text": "写一份报告"})
    activity = client.get(f"/api/projects/{pid}/dashboard").json()["activity"]
    assert any(item["kind"] == "quicktask.created" for item in activity)
    assert json.dumps(activity, ensure_ascii=False)  # 可序列化
