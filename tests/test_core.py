"""工具函数与配置加载的单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from multiagent.config import Config
from multiagent.util import (
    is_binary_path,
    matches_any,
    new_id,
    now_iso,
    parse_iso,
    safe_join,
    sha256_file,
    sha256_text,
    slugify,
    truncate,
)


def test_ids_and_slug():
    assert new_id("task").startswith("task-")
    assert new_id("task") != new_id("task")
    assert slugify("Hello World!") == "Hello-World"
    assert slugify("") == "item"
    assert slugify("我的项目") == "我的项目"


def test_time_helpers():
    stamp = now_iso()
    assert stamp.endswith("Z")
    assert parse_iso(stamp) is not None
    assert parse_iso("nonsense") is None


def test_matches_any_globs():
    assert matches_any("src/a.py", ["src/**"])
    assert matches_any("src/deep/b.py", ["src/**"])
    assert matches_any("notes.md", ["**/*.md"])
    assert not matches_any("docs/a.md", ["src/**"])
    assert matches_any("README.md", ["README.md"])


def test_safe_join_blocks_escape(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    assert safe_join(tmp_path, "a.txt").name == "a.txt"
    with pytest.raises(ValueError):
        safe_join(tmp_path, "../outside.txt")


def test_hashing_and_binary(tmp_path: Path):
    path = tmp_path / "f.txt"
    path.write_text("hello", encoding="utf-8")
    assert sha256_file(path) == sha256_text("hello")
    assert not is_binary_path(path)
    raw = tmp_path / "blob.bin"
    raw.write_bytes(b"\x00\x01\x02")
    assert is_binary_path(raw)


def test_truncate():
    assert truncate("abcdef", 4).startswith("a")
    assert truncate("abc", 100) == "abc"


def test_config_defaults_and_materialize(tmp_path: Path):
    cfg = Config.load(tmp_path)
    assert cfg.warnings == []
    assert "codex" in cfg.agent_ids()
    assert cfg.template("implement")["kind"] == "code"
    created = cfg.materialize()
    assert len(created) == 5
    for name in ("agents.json", "templates.json", "routing.json", "acceptance.json", "policy.json"):
        assert (cfg.config_dir / name).exists()
    assert cfg.acceptance_rule("file-exists")


def test_config_policy_paths(tmp_path: Path):
    cfg = Config.load(tmp_path)
    assert cfg.policy_get("concurrency.global") == 6
    assert cfg.policy_get("nope.missing", "fallback") == "fallback"
    assert cfg.kind_defaults("review")["read_only"] is True


def test_effective_cwd(tmp_path: Path):
    from multiagent.agents.base import effective_cwd

    workspace = tmp_path / "ws"
    workspace.mkdir()
    assert effective_cwd({"cwd": "."}, workspace) == workspace
    sub = effective_cwd({"cwd": "sub"}, workspace)
    assert sub == workspace / "sub" and sub.exists()
    absolute = tmp_path / "abs"
    absolute.mkdir()
    assert effective_cwd({"cwd": str(absolute)}, workspace) == absolute
