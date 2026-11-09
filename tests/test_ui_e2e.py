"""端到端验收（不经浏览器）：中文全链路 + 一句话派活 + dashboard 首屏。

这一层刻意**走前端真正会走的路径**，而不是后端单测的路径：

* 中文链路：``POST /api/projects``（中文名）→ DB 读回 → ``GET /api/projects/{pid}/dashboard``
  （指挥台首屏唯一数据源）→ 断言原样、无 ``?``/``�``；再用 ``PATCH`` 把历史乱码名改回来。
* 一句话派活：只通过 ``quick-task`` 建任务，然后**从 dashboard 看**任务是否出现在看板里、
  kind / read_only / 验收是否符合契约 §3.2——因为前端只看 dashboard。
* 真跑一次：``start=true`` + 假 Agent，最终以 dashboard 的 ``running``/任务状态收口
  （前端就是靠这两个字段把「执行中」变回「成功」的）。
* 100 任务首屏预算：实测 p50，写进 ``.e2e/qa-dashboard-perf.json`` 供 docs 引用。

与 ``tests/test_api_quicktask.py`` 的分工：那边验「接口自身的字段/校验/错误码」，
这边验「用户从输入一句话到看见结果的整条链路」，断言对象是 dashboard 与页面级行为。
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from multiagent.api.app import create_api

REPO = Path(__file__).resolve().parents[1]
PERF_RECORD = REPO / ".e2e" / "qa-dashboard-perf.json"

#: 契约 §3.1：100 任务规模单请求 ≤150ms。
DASHBOARD_BUDGET_MS = 150.0

#: 判定「乱码」的字符：ASCII 问号替换、Unicode 替换字符。
MOJIBAKE = ("???", "\ufffd")

CHINESE_PROJECT = "竞品调研·甲项目"
CHINESE_TASK = "调研三个竞品并输出竞品分析.md"
CHINESE_OUTPUT = "竞品分析.md"


def _client(wb) -> TestClient:  # noqa: ANN001
    return TestClient(create_api(wb))


def _assert_clean(text: str, where: str) -> None:
    """任何出现在用户可见字段里的文本都不得含替换字符/问号串。"""
    for token in MOJIBAKE:
        assert token not in text, f"{where} 出现乱码标记 {token!r}：{text!r}"


def _new_project(client: TestClient, name: str) -> dict:
    res = client.post("/api/projects", json={"name": name})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["name"] == name
    _assert_clean(body["name"], "POST /api/projects 响应")
    return body


def _dashboard(client: TestClient, pid: str) -> dict:
    res = client.get(f"/api/projects/{pid}/dashboard")
    assert res.status_code == 200, res.text
    # 原始字节必须能按 UTF-8 严格解码（HTTP 层没被二次转码）
    raw = res.content.decode("utf-8", errors="strict")
    _assert_clean(raw, f"GET /api/projects/{pid}/dashboard 原始响应")
    return res.json()


# ===========================================================================
# 1) 中文全链路：创建 → 存储 → API → 首屏数据源
# ===========================================================================


def test_chinese_project_and_task_survive_full_chain(wb):
    """中文项目名 / 任务名从创建到 dashboard 原样保真（契约 §0.1 / §5.3）。"""
    client = _client(wb)
    project = _new_project(client, CHINESE_PROJECT)
    pid = project["id"]

    created = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": CHINESE_TASK, "outputs": [CHINESE_OUTPUT]},
    )
    assert created.status_code == 200, created.text
    tid = created.json()["tasks"][0]["id"]

    # --- DB 层：存进去的就是中文（不是问号）
    stored_project = wb.projects.get(pid)
    assert stored_project["name"] == CHINESE_PROJECT
    _assert_clean(stored_project["name"], "DB 项目名")
    stored_task = wb.tasks.get(tid)
    assert stored_task["title"] == "调研三个竞品并输出竞品分析.md"
    assert stored_task["brief"] == CHINESE_TASK
    _assert_clean(stored_task["title"], "DB 任务标题")
    _assert_clean(stored_task["brief"], "DB 任务 brief")

    # --- API 层：指挥台首屏（前端唯一数据源）原样显示
    dash = _dashboard(client, pid)
    assert dash["project"]["name"] == CHINESE_PROJECT
    _assert_clean(dash["project"]["name"], "dashboard 项目名")
    assert dash["progress"]["total"] == 1
    titles = [task["title"] for task in dash["tasks"]]
    assert "调研三个竞品并输出竞品分析.md" in titles
    for title in titles:
        _assert_clean(title, "dashboard 任务标题")

    # 看板视图（另一个数据源）同样保真
    kanban = client.get(f"/api/projects/{pid}/kanban").json()
    _assert_clean(json.dumps(kanban, ensure_ascii=False), "kanban 响应")


def test_chinese_rename_fixes_legacy_garbled_name(wb):
    """契约 §3.3：历史乱码项目名可以 PATCH 改回中文，dashboard 立刻正确。"""
    client = _client(wb)
    # 模拟历史脏数据：名字本身就是问号串（老版本 GBK 解码失败的典型残留）。
    # 这里刻意绕过 _new_project 的「干净」断言——脏数据正是要被修的对象。
    legacy = client.post("/api/projects", json={"name": "????????"}).json()
    pid = legacy["id"]

    renamed = client.patch(
        f"/api/projects/{pid}", json={"name": "竞品调研·乙项目", "label": "竞品调研"}
    )
    assert renamed.status_code == 200, renamed.text
    body = renamed.json()
    assert body["name"] == "竞品调研·乙项目"
    assert body["label"] == "竞品调研"
    _assert_clean(json.dumps(body, ensure_ascii=False), "PATCH 响应")

    dash = _dashboard(client, pid)
    assert dash["project"]["name"] == "竞品调研·乙项目"
    assert dash["project"]["label"] == "竞品调研"
    _assert_clean(json.dumps(dash["project"], ensure_ascii=False), "dashboard.project")

    # 列表接口（顶栏项目选择器数据源）也同步
    listed = client.get("/api/projects").json()
    row = next(item for item in listed if item["id"] == pid)
    assert row["name"] == "竞品调研·乙项目"
    _assert_clean(row["name"], "/api/projects 列表")


def test_page_and_static_assets_are_utf8(wb):
    """页面声明与静态资源都是 UTF-8（否则浏览器按 GBK 猜就是满屏乱码）。"""
    client = _client(wb)
    page = client.get("/")
    assert page.status_code == 200
    html = page.content.decode("utf-8", errors="strict")
    assert "charset=utf-8" in html.lower() or "charset=\"utf-8\"" in html.lower()
    _assert_clean(html, "index.html")

    js = client.get("/static/app.js")
    assert js.status_code == 200
    source = js.content.decode("utf-8", errors="strict")
    assert "esc(" in source  # 仍是转义渲染（XSS 护栏在 test_api_security 里细查）


# ===========================================================================
# 2) 一句话派活：single 模式（契约 §3.2）
# ===========================================================================


def test_quicktask_single_defaults_to_exit_zero_acceptance(wb):
    """无 outputs → 验收默认 exit_zero；任务出现在 dashboard 看板的「待执行」列。"""
    client = _client(wb)
    pid = _new_project(client, "一句话-默认验收")["id"]
    body = client.post(f"/api/projects/{pid}/quick-task", json={"text": "看看这个仓库有什么问题"}).json()
    assert body["mode"] == "single" and len(body["tasks"]) == 1

    task = client.get(f"/api/tasks/{body['tasks'][0]['id']}").json()["task"]
    assert task["acceptance"] == [{"type": "exit_zero"}]
    assert task["outputs"] == []

    dash = _dashboard(client, pid)
    assert dash["progress"]["counters"]["pending"] == 1
    assert dash["tasks"][0]["status"] == "pending"


def test_quicktask_single_with_outputs_requires_files(wb):
    """有 outputs → 每个产物一条 file_exists 验收（前端「产出文件」输入框的语义）。"""
    client = _client(wb)
    pid = _new_project(client, "一句话-产物验收")["id"]
    body = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": "写两份文档", "outputs": ["方案.md", "复盘.md"]},
    ).json()
    task = client.get(f"/api/tasks/{body['tasks'][0]['id']}").json()["task"]
    assert task["acceptance"] == [
        {"type": "file_exists", "path": "方案.md"},
        {"type": "file_exists", "path": "复盘.md"},
    ]
    assert task["outputs"] == ["方案.md", "复盘.md"]


@pytest.mark.parametrize(
    ("text", "expected_kind", "why"),
    [
        ("撰写一份 2000 字竞品对比报告", "doc", "报告 → doc"),
        ("调研三个竞品并整理资料", "research", "调研 → research"),
        ("审阅这份方案并给出修改建议", "review", "审阅 → review"),
        ("修复登录接口的代码报错并补测试", "code", "代码 → code"),
        ("整理 Excel 表格数据", "data", "数据 → data"),
    ],
)
def test_quicktask_kind_auto_inference_reaches_dashboard(wb, text, expected_kind, why):
    """kind=auto 的推断结果必须体现在 dashboard（前端看板按 kind 显示类型）。"""
    client = _client(wb)
    pid = _new_project(client, f"一句话-推断-{expected_kind}")["id"]
    body = client.post(f"/api/projects/{pid}/quick-task", json={"text": text}).json()
    dash = _dashboard(client, pid)
    task = next(item for item in dash["tasks"] if item["id"] == body["tasks"][0]["id"])
    assert task["kind"] == expected_kind, f"{why}：期望 {expected_kind}，实际 {task['kind']}"
    _assert_clean(task["title"], "推断任务标题")


def test_quicktask_start_false_never_runs(wb, monkeypatch):
    """start=false（「创建任务」按钮）：只建任务，绝不调度。"""
    def boom(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("start=false 不应启动调度（用户点的是「创建任务」）")

    monkeypatch.setattr(wb.engine, "run_project", boom)
    client = _client(wb)
    pid = _new_project(client, "一句话-不启动")["id"]
    res = client.post(f"/api/projects/{pid}/quick-task", json={"text": "只是先记下来"})
    assert res.status_code == 200
    assert res.json()["started"] is False

    dash = _dashboard(client, pid)
    assert dash["running"] is False
    assert dash["progress"]["counters"]["pending"] == 1


# ===========================================================================
# 3) 一句话拆计划：plan 模式（契约 §3.2）
# ===========================================================================


def test_quicktask_plan_creates_three_step_dag(wb):
    """plan：调研 → 初稿 → 只读审阅，依赖用 success，dashboard 能看到 3 个任务。"""
    client = _client(wb)
    pid = _new_project(client, "一句话-三步计划")["id"]
    body = client.post(
        f"/api/projects/{pid}/quick-task",
        json={"text": "调研竞品并写一份对比报告", "mode": "plan", "outputs": ["对比报告.md"]},
    ).json()
    assert body["mode"] == "plan" and len(body["tasks"]) == 3
    assert body["started"] is False

    ids = [item["id"] for item in body["tasks"]]
    details = [client.get(f"/api/tasks/{tid}").json() for tid in ids]

    # 三步的类型与顺序
    assert [detail["task"]["kind"] for detail in details] == ["research", "doc", "review"]
    # 依赖链：②→①、③→②，且都是 success 门
    assert details[0]["dependencies"] == []
    assert [dep["id"] for dep in details[1]["dependencies"]] == [ids[0]]
    assert details[1]["dependencies"][0]["mode"] == "success"
    assert [dep["id"] for dep in details[2]["dependencies"]] == [ids[1]]
    assert details[2]["dependencies"][0]["mode"] == "success"
    # 审阅步骤必须只读（不允许改文件）
    assert details[2]["task"]["read_only"] is True
    # 初稿步骤承接产物，调研/审阅各自的验收口径
    assert details[1]["task"]["outputs"] == ["对比报告.md"]
    assert details[1]["task"]["acceptance"] == [{"type": "file_exists", "path": "对比报告.md"}]
    assert details[0]["task"]["acceptance"] == [{"type": "exit_zero"}]
    assert {check["type"] for check in details[2]["task"]["acceptance"]} == {"no_writes", "handoff_present"}

    # dashboard：三个任务都在「待执行」，总数为 3
    dash = _dashboard(client, pid)
    assert dash["progress"]["total"] == 3
    assert dash["progress"]["counters"]["pending"] == 3
    assert {task["id"] for task in dash["tasks"]} == set(ids)
    assert next(task for task in dash["tasks"] if task["id"] == ids[2])["read_only"] is True

    # 依赖图数据源（kanban 的 edges）也是两步
    assert len(client.get(f"/api/projects/{pid}/kanban").json()["edges"]) == 2


# ===========================================================================
# 4) 「创建并启动」真跑一次（假 Agent，不消耗任何模型额度）
# ===========================================================================


def test_quicktask_start_true_runs_fake_agent_to_success(wb, set_plan):
    """start=true → 后台调度 → 假 Agent 写产物 → dashboard 从 running 回到 false 且任务 success。

    这条是「用户点一下就能跑起来」的端到端证据：不是 mock 掉引擎，而是真的 spawn
    ``python -m multiagent.testing.fake_agent`` 子进程、真的同步产物、真的判验收。
    """
    set_plan("worker", {"files": {CHINESE_OUTPUT: "中文产物：竞品对比表"}, "exit": 0})
    client = _client(wb)
    pid = _new_project(client, "一句话-真跑一次")["id"]

    res = client.post(
        f"/api/projects/{pid}/quick-task",
        json={
            "text": "调研三个竞品并输出竞品分析.md",
            "outputs": [CHINESE_OUTPUT],
            "agent_id": "worker",
            "start": True,
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["started"] is True, body
    tid = body["tasks"][0]["id"]

    deadline = time.monotonic() + 90
    dash: dict = {}
    while time.monotonic() < deadline:
        dash = _dashboard(client, pid)
        if dash["running"] is False and dash["tasks"] and dash["tasks"][0]["status"] != "running":
            break
        time.sleep(0.1)

    dash = _dashboard(client, pid)
    assert dash["running"] is False, "调度结束后 dashboard.running 仍为 True（前端会一直显示执行中）"
    task = next(item for item in dash["tasks"] if item["id"] == tid)
    assert task["status"] == "success", f"任务没有成功：{task}"
    assert dash["progress"]["counters"]["success"] == 1
    assert dash["progress"]["percent"] == 100

    # 产物真的落到了项目工作区，且内容中文保真
    root = Path(wb.projects.get(pid)["root_path"])
    artifact = root / CHINESE_OUTPUT
    assert artifact.is_file(), f"产物没有同步回工作区：{root}"
    assert artifact.read_text(encoding="utf-8").strip() == "中文产物：竞品对比表"

    # 最近活动（首屏「最近活动」数据源）能解释发生了什么
    kinds = {item["kind"] for item in dash["activity"]}
    assert any(kind.startswith("task.") for kind in kinds), f"活动流没有任务事件：{kinds}"


# ===========================================================================
# 5) 首屏性能：100 任务 ≤150ms（契约 §3.1），实测值落到 .e2e 供文档引用
# ===========================================================================


def test_dashboard_100_tasks_p50_under_budget(wb):
    """100 任务规模下 dashboard 单请求 p50 ≤150ms，并把实测值记录成证据。"""
    client = _client(wb)
    pid = _new_project(client, "首屏性能-100")["id"]
    for index in range(100):
        wb.tasks.create(pid, f"任务 {index}", kind="generic", agent_id="worker")

    client.get(f"/api/projects/{pid}/dashboard")  # 预热
    samples: list[float] = []
    for _ in range(7):
        started = time.perf_counter()
        res = client.get(f"/api/projects/{pid}/dashboard")
        samples.append((time.perf_counter() - started) * 1000)
        assert res.status_code == 200

    body = res.json()
    assert body["progress"]["total"] == 100
    assert len(body["tasks"]) == 100

    ordered = sorted(samples)
    p50 = statistics.median(samples)
    p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
    record = {
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "tasks": 100,
        "samples_ms": [round(value, 2) for value in samples],
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "min_ms": round(min(samples), 2),
        "max_ms": round(max(samples), 2),
        "budget_ms": DASHBOARD_BUDGET_MS,
        "passed": p50 <= DASHBOARD_BUDGET_MS,
    }
    PERF_RECORD.parent.mkdir(parents=True, exist_ok=True)
    PERF_RECORD.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    assert p50 <= DASHBOARD_BUDGET_MS, (
        f"dashboard 100 任务 p50={p50:.1f}ms 超过契约上限 {DASHBOARD_BUDGET_MS}ms；"
        f"样本={[round(v, 1) for v in samples]}"
    )
