"""历史文档标记的静态契约（P2）。

只校验文档头部的历史标记，不涉及历史正文：

- 旧控制台 / 旧测试数字文档必须带 ``v1 / 历史参考`` 标记，并指向 DEFECTS-V2；
- v2 第一轮交付报告必须标注为历史交付报告，且 558 等数字有历史说明；
- 当前文档（README / USAGE-V2 / DEFECTS-V2）不得被误加 v1 标记。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = REPO_ROOT / "docs"

V1_DOCS = (
    "ARCHITECTURE.md",
    "USAGE.md",
    "VERIFICATION.md",
    "UI-V2-QA.md",
)

CURRENT_DOCS = (
    REPO_ROOT / "README.md",
    DOCS_DIR / "USAGE-V2.md",
    DOCS_DIR / "DEFECTS-V2.md",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _head(text: str, lines: int = 12) -> str:
    return "\n".join(text.splitlines()[:lines])


def test_v1_docs_are_marked_historical() -> None:
    for name in V1_DOCS:
        path = DOCS_DIR / name
        text = _read(path)
        head = _head(text)
        assert "v1 / 历史参考" in head, f"{name} 头部缺少 v1 历史标记"
        assert "DEFECTS-V2" in text, f"{name} 缺少 DEFECTS-V2 指向"
        assert "仅作历史记录" in text, f"{name} 未说明旧数字仅作历史记录"


def test_v2_final_report_is_historical_delivery_report() -> None:
    path = DOCS_DIR / "V2-FINAL-REPORT.md"
    text = _read(path)
    head = _head(text)
    assert "历史交付报告" in head, "V2-FINAL-REPORT 头部缺少历史交付报告标记"
    assert "v1 / 历史参考" not in head, "V2-FINAL-REPORT 是 v2 第一轮报告，不应标成 v1"

    # 558 必须出现在“历史”语境中，避免被当成当前测试基线。
    historical_558 = [
        line for line in text.splitlines() if "558" in line and "历史记录" in line
    ]
    assert historical_558, "558 缺少“仅作历史记录”的说明"


def test_current_docs_are_not_marked_as_v1() -> None:
    for path in CURRENT_DOCS:
        head = _head(_read(path))
        assert "v1 / 历史参考" not in head, f"{path.name} 被误加 v1 历史标记"
