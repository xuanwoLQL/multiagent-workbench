"""验收 P2-14：glob 产物路径 + 精确路径失败的候选诊断。

回归背景：任务把验收写死为 ``AppShell.tsx``，Agent 实际产出
``layout/AppShell.tsx``。验收必须继续判定失败，但失败信息要给出候选与
可执行的修正建议（而不是让用户面对一条没有诊断的红色记录）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from multiagent.config import Config
from multiagent.tasks.acceptance import evaluate_acceptance
from multiagent.tasks.profiles import effective_acceptance


def _run(
    workspace: Path,
    *conditions: dict,
    changes: list[dict] | None = None,
    exit_code: int = 0,
):
    return evaluate_acceptance(
        list(conditions),
        workspace=workspace,
        changes=changes or [],
        exit_code=exit_code,
        handoff=None,
    )


def test_exact_file_exists_failure_reports_candidates_without_passing(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "layout").mkdir(parents=True)
    (workspace / "layout" / "AppShell.tsx").write_text(
        "export default function AppShell() {}", encoding="utf-8"
    )

    result = _run(workspace, {"type": "file_exists", "path": "AppShell.tsx"})

    assert result.ok is False
    check = result.checks[0]
    assert check["type"] == "file_exists"
    assert check["ok"] is False
    assert check["candidates"] == ["layout/AppShell.tsx"]
    assert check["suggested"] == {
        "type": "glob_min_files",
        "pattern": "**/AppShell.tsx",
        "min_count": 1,
    }
    assert check["hint"]
    assert "layout/AppShell.tsx" in check["detail"]
    # 候选只是诊断信息，绝不能把失败伪造成通过。
    assert result.failing()[0]["type"] == "file_exists"


def test_file_exists_glob_passes_on_actual_layout(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "src" / "layout").mkdir(parents=True)
    (workspace / "src" / "layout" / "AppShell.tsx").write_text("x", encoding="utf-8")

    result = _run(workspace, {"type": "file_exists", "path": "**/AppShell.tsx"})

    assert result.ok is True
    assert result.checks[0]["ok"] is True
    assert result.checks[0]["detail"].count("匹配文件数=1") == 1


def test_file_nonempty_glob_requires_a_nonempty_match(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "Empty.tsx").write_text("", encoding="utf-8")

    failed = _run(workspace, {"type": "file_nonempty", "path": "**/*.tsx", "min_bytes": 1})
    assert failed.ok is False
    assert failed.checks[0]["ok"] is False

    (workspace / "src" / "Filled.tsx").write_text("export const x = 1;", encoding="utf-8")
    passed = _run(workspace, {"type": "file_nonempty", "path": "**/*.tsx", "min_bytes": 1})
    assert passed.ok is True
    assert passed.checks[0]["ok"] is True


def test_exact_file_nonempty_failure_reports_candidates(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "tickets").mkdir(parents=True)
    (workspace / "tickets" / "TicketBoard.tsx").write_text("board", encoding="utf-8")

    result = _run(
        workspace, {"type": "file_nonempty", "path": "TicketBoard.tsx", "min_bytes": 1}
    )

    assert result.ok is False
    check = result.checks[0]
    assert check["ok"] is False
    assert check["candidates"] == ["tickets/TicketBoard.tsx"]
    assert check["suggested"] == {
        "type": "glob_min_files",
        "pattern": "**/TicketBoard.tsx",
        "min_count": 1,
    }
    assert check["hint"]


def test_content_contains_glob_passes_when_any_match_contains_text(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.tsx").write_text("export const a = 1;", encoding="utf-8")
    (workspace / "src" / "b.tsx").write_text(
        "export const token = 'NEEDLE-42';", encoding="utf-8"
    )

    hit = _run(
        workspace, {"type": "content_contains", "path": "**/*.tsx", "text": "NEEDLE-42"}
    )
    assert hit.ok is True
    assert hit.checks[0]["ok"] is True

    miss = _run(
        workspace, {"type": "content_contains", "path": "**/*.tsx", "text": "ABSENT"}
    )
    assert miss.ok is False
    assert miss.checks[0]["ok"] is False


def test_content_not_contains_glob_is_the_inverse(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "clean.tsx").write_text("export const clean = true;", encoding="utf-8")

    clean = _run(
        workspace, {"type": "content_not_contains", "path": "**/*.tsx", "text": "FORBIDDEN"}
    )
    assert clean.ok is True
    assert clean.checks[0]["ok"] is True

    (workspace / "src" / "bad.tsx").write_text("const x = 'FORBIDDEN';", encoding="utf-8")
    dirty = _run(
        workspace, {"type": "content_not_contains", "path": "**/*.tsx", "text": "FORBIDDEN"}
    )
    assert dirty.ok is False
    assert dirty.checks[0]["ok"] is False
    assert "bad.tsx" in dirty.checks[0]["detail"]


def test_exact_content_failure_reports_candidates(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "components").mkdir(parents=True)
    (workspace / "components" / "AppShell.tsx").write_text(
        "export const KEY = 'here';", encoding="utf-8"
    )

    result = _run(
        workspace, {"type": "content_contains", "path": "AppShell.tsx", "text": "KEY"}
    )

    assert result.ok is False
    check = result.checks[0]
    assert check["ok"] is False
    assert check["candidates"] == ["components/AppShell.tsx"]
    assert check["suggested"]["pattern"] == "**/AppShell.tsx"
    assert check["hint"]


def test_out_of_bounds_globs_match_nothing_and_do_not_raise(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (tmp_path / "escape.txt").write_text("secret", encoding="utf-8")
    absolute_pattern = str(tmp_path / "*.txt")

    for pattern in ("../*.txt", "..\\*.txt", absolute_pattern, "/etc/*.txt"):
        result = _run(workspace, {"type": "file_exists", "path": pattern})
        assert result.ok is False, pattern
        assert result.checks[0]["ok"] is False, pattern
        assert "Traceback" not in result.checks[0]["detail"], pattern

    escaped = _run(workspace, {"type": "glob_min_files", "pattern": "../*.txt", "min_count": 1})
    assert escaped.ok is False

    # 精确路径越界同样只判失败，不向外抛异常。
    exact_escape = _run(workspace, {"type": "file_exists", "path": "../escape.txt"})
    assert exact_escape.ok is False
    assert exact_escape.checks[0]["ok"] is False


def test_candidate_search_skips_vendor_dirs(tmp_path: Path):
    workspace = tmp_path / "ws"
    (workspace / "node_modules" / "pkg").mkdir(parents=True)
    (workspace / "node_modules" / "pkg" / "AppShell.tsx").write_text("dep", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "AppShell.tsx").write_text("git", encoding="utf-8")
    (workspace / "dist").mkdir()
    (workspace / "dist" / "AppShell.tsx").write_text("dist", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "AppShell.tsx").write_text("real", encoding="utf-8")

    result = _run(workspace, {"type": "file_exists", "path": "AppShell.tsx"})

    check = result.checks[0]
    assert check["ok"] is False
    assert check["candidates"] == ["src/AppShell.tsx"]


def test_candidate_search_caps_at_twenty_and_shows_five(tmp_path: Path):
    workspace = tmp_path / "ws"
    for index in range(25):
        directory = workspace / "copies" / f"d{index:02d}"
        directory.mkdir(parents=True)
        (directory / "AppShell.tsx").write_text("x", encoding="utf-8")

    result = _run(workspace, {"type": "file_exists", "path": "AppShell.tsx"})

    check = result.checks[0]
    assert check["ok"] is False
    assert len(check["candidates"]) == 20
    for index in range(5):
        assert f"copies/d{index:02d}/AppShell.tsx" in check["detail"]
    assert "copies/d05/AppShell.tsx" not in check["detail"]
    assert "共 20 个" in check["detail"]


def test_candidate_search_skips_symlinks(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AppShell.tsx").write_text("outside", encoding="utf-8")
    try:
        (workspace / "AppShell.tsx").symlink_to(outside / "AppShell.tsx")
        (workspace / "linked").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")

    result = _run(workspace, {"type": "file_exists", "path": "AppShell.tsx"})

    check = result.checks[0]
    assert check["ok"] is False
    assert check["candidates"] == []
    assert check["suggested"] is None


def test_unknown_acceptance_type_still_fails(tmp_path: Path):
    workspace = tmp_path / "ws"
    workspace.mkdir()

    result = _run(workspace, {"type": "definitely_not_a_check"})

    assert result.ok is False
    assert result.checks[0]["ok"] is False
    assert "未知验收类型" in result.checks[0]["detail"]


def test_effective_acceptance_keeps_glob_outputs(tmp_path: Path):
    config = Config.load(tmp_path)
    conditions, _warnings = effective_acceptance(
        {"outputs": ["web/src/**/*.tsx"], "kind": "web", "acceptance": []},
        config,
    )

    globs = [item for item in conditions if item["type"] == "glob_min_files"]
    assert any(
        item["pattern"] == "web/src/**/*.tsx" and int(item["min_count"]) == 1 for item in globs
    )
    assert not any(item["type"] == "file_exists" for item in conditions)
