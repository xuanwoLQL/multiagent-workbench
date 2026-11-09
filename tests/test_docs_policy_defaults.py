"""默认 Git 策略与文档表述的静态契约（先审后合）。

本轮把默认策略从 ``git.auto_merge=true`` 改为 ``false``：验收通过只在
``ma/<task>`` 分支提交，人工合并后才进入基线；需要 DAG 连续交付的项目显式设为
``true``。policy、presets 与文档必须保持一致，不能残留“默认自动合并”的旧表述。
"""

from __future__ import annotations

import json
from pathlib import Path

from multiagent.presets import DEFAULT_POLICY

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_JSON = REPO_ROOT / "config" / "policy.json"
POLICY_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs" / "USAGE-V2.md",
    REPO_ROOT / "docs" / "V2-ARCHITECTURE.md",
    REPO_ROOT / "docs" / "RELEASE.md",
)
DEFECTS = REPO_ROOT / "docs" / "DEFECTS-V2.md"

# auto_merge 的默认值只能是 false；这里不允许出现与之矛盾的旧表述。
CONTRADICTORY_PHRASES = (
    "默认自动合并",
    "默认 true",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """返回以 heading 开头、到下一个二级标题之前的段落。"""
    start = text.index(heading)
    rest = text[start + len(heading) :]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def test_policy_and_preset_default_to_review_first() -> None:
    policy = json.loads(_read(POLICY_JSON))
    assert policy["git"]["auto_merge"] is False
    assert policy["git"]["require_clean_tree"] is True
    assert policy["git"]["keep_worktree"] is True

    assert DEFAULT_POLICY["git"]["auto_merge"] is False
    assert DEFAULT_POLICY["git"]["require_clean_tree"] is True
    assert DEFAULT_POLICY["git"]["keep_worktree"] is True


def test_policy_docs_use_review_first_wording() -> None:
    for path in POLICY_DOCS:
        text = _read(path)
        assert "先审后合" in text, f"{path.name} 未说明默认先审后合"
        assert "auto_merge" in text, f"{path.name} 未提到 auto_merge"
        for phrase in CONTRADICTORY_PHRASES:
            assert phrase not in text, (
                f"{path.name} 仍有与 auto_merge 默认 false 矛盾的表述：{phrase}"
            )

        # DAG 连续交付必须给出显式开启方式与配套策略建议。
        assert "DAG" in text, f"{path.name} 未说明 DAG 连续交付场景"
        assert "require_clean_tree" in text, (
            f"{path.name} 缺少 require_clean_tree 建议"
        )
        assert "keep_worktree" in text, f"{path.name} 缺少 keep_worktree 建议"


def test_defects_p0_1_records_review_first_default() -> None:
    text = _read(DEFECTS)

    p0_1 = _section(text, "## P0-1")
    assert "已修复" in p0_1
    assert "默认" in p0_1
    assert "先审后合" in p0_1
    assert "auto_merge=false" in p0_1, "P0-1 未记录默认 auto_merge=false"

    summary = _section(text, "## 状态汇总")
    assert "| 已修复 | 18" in summary
    assert "| 部分修复 | 0" in summary
    assert "| 未修 | 0" in summary
    stale_rows = [
        line
        for line in summary.splitlines()
        if "部分修复" in line and ("P0-1" in line or "P2-16" in line)
    ]
    assert not stale_rows, f"状态汇总仍把 P0-1/P2-16 列为部分修复：{stale_rows}"

    p2_16 = _section(text, "## P2-16")
    assert "- **状态**：已修复" in p2_16
