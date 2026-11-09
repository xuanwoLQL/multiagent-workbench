"""Static contract tests for scripts/v2_flows.py (no browser required).

These tests protect the shape of the real-browser flow harness without
requiring Microsoft Edge in CI.  The browser script itself is exercised with
``--help``; the eight flows only run on a developer machine (or a Windows
runner with Edge installed).
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "v2_flows.py"
SMOKE = REPO / "scripts" / "v2_smoke.py"

EXPECTED_FLOWS = [
    "task-merged",
    "merge-conflict",
    "extend-timeout",
    "settings-delivery",
    "review-states",
    "new-task-advanced",
    "duplicate-projects",
    "missing-branch",
]


def _source() -> str:
    assert SCRIPT.is_file(), "scripts/v2_flows.py 不存在"
    return SCRIPT.read_text(encoding="utf-8")


def _flow_names() -> list[str]:
    text = _source()
    marker = "FLOW_NAMES"
    start = text.index(marker)
    open_paren = text.index("(", start)
    close_paren = text.index(")", open_paren)
    block = text[open_paren + 1 : close_paren]
    names = []
    for raw in block.split(","):
        value = raw.strip()
        if not value:
            continue
        names.append(value.strip("'\""))
    return names


def test_script_exists_and_declares_eight_flows() -> None:
    assert SCRIPT.is_file()
    assert _flow_names() == EXPECTED_FLOWS
    assert len(EXPECTED_FLOWS) == 8
    assert "duplicate-projects" in EXPECTED_FLOWS
    assert "missing-branch" in EXPECTED_FLOWS


def test_missing_branch_flow_covers_409_disable_and_rebuild() -> None:
    """missing_branch 必须是真实浏览器终态：409、禁用合并、确认重建、重建后合并。"""
    text = _source()
    for marker in (
        "flow_missing_branch",
        "missing-branch",
        "missing_branch",
        "rebuild-branch",
        "分支已不存在",
        "无法合并",
        "missing-branch-before.png",
        "missing-branch-after.png",
    ):
        assert marker in text, f"missing-branch 流程缺少断言标记：{marker}"
    assert text.count("flow_missing_branch") >= 2, "第 8 条流程必须注册到 run_all"


def test_duplicate_projects_are_seeded_directly_before_backend() -> None:
    """历史重复项目不能走 409 API，必须在后端启动前直接写临时 home 的 DB。"""
    text = _source()
    assert "seed_duplicate_projects" in text
    assert "from multiagent.app import Workbench" in text, (
        "应使用 Workbench 打开临时 home 直接写库"
    )
    assert "db.insert(" in text, "应直接调用 db.insert 写入 projects 表"
    assert '"projects"' in text
    for column in ('"root_path"', '"label"', '"created_at"', '"updated_at"'):
        assert column in text, f"播种行缺少字段：{column}"
    assert "release_home_logging" in text, "Workbench 的日志句柄必须在退出前释放，否则临时目录无法清理"

    # 调用点（main 中传 duplicate_root）必须早于同一临时 home 的后端启动。
    seed_call = text.index("seed_duplicate_projects(home, duplicate_root)")
    backend_call = text.index("start_backend(home, port")
    assert seed_call < backend_call, "历史重复项目必须在启动后端之前播种"


def test_duplicate_flow_asserts_banner_filter_and_open() -> None:
    text = _source()
    for marker in (
        ".pj-dup-banner",
        ".pj-dup-filter",
        "仅显示重复项目",
        "历史数据未删除",
        "组重复项目",
        ".pj-card:not(.pj-card-skeleton)",
        "js_open_duplicate_member",
        "duplicate-projects.png",
        "main h1",
    ):
        assert marker in text, f"duplicate-projects 流程缺少断言标记：{marker}"
    assert text.count("flow_duplicate_projects") >= 2, "第 7 条流程必须注册到 run_all"


def test_temp_home_isolation() -> None:
    text = _source()
    assert "MULTIAGENT_HOME" in text
    assert "tempfile.mkdtemp" in text
    assert "shutil.rmtree" in text
    assert "start_backend" in text
    assert "stop_process" in text


def test_flow_screenshot_directory() -> None:
    text = _source()
    assert ".e2e" in text
    assert "ui-v2" in text
    assert "flows" in text
    assert "DEFAULT_OUT_DIR" in text
    assert "screenshot" in text


def test_exception_and_console_checks() -> None:
    text = _source()
    assert "exceptions" in text
    assert "console_errors" in text
    assert "UNCAUGHT EXCEPTIONS" in text
    assert "CONSOLE ERRORS" in text
    assert "Recorder" in text


def test_does_not_rewrite_smoke_script() -> None:
    text = _source()
    assert "from v2_smoke import" in text, "应复用 v2_smoke 的 CDP/Recorder 辅助函数"
    for forbidden in (
        "v2_smoke.write_text",
        "v2_smoke.write_bytes",
        "v2_smoke.unlink",
        "open(V2_SMOKE",
    ):
        assert forbidden not in text


def test_help_runs_and_smoke_is_untouched() -> None:
    before = hashlib.sha256(SMOKE.read_bytes()).hexdigest()
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=str(REPO),
        capture_output=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
    output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
    assert "E2E" in output or "flows" in output
    after = hashlib.sha256(SMOKE.read_bytes()).hexdigest()
    assert before == after, "--help 不应修改 v2_smoke.py"
