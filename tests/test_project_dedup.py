"""P1-11：同一真实目录不能被重复登记为多个项目。

覆盖 ProjectService 层契约（API 409 接线由 app.py 负责）：
- 同一绝对路径换名字导入 -> ProjectRootConflict；
- exists_ok=True 返回已有项目且不新增行；
- 尾斜杠 / 反斜杠 / Windows 大小写差异视为同一目录；
- create(root_path=...) 同样判重；
- 不同目录同名项目仍按名字判重（现有语义）；
- 冲突不删除、不改写历史项目数据。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from multiagent.workspace.projects import ProjectRootConflict, normalize_root


def _make_dir(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    (path / "README.md").write_text(name, encoding="utf-8")
    return path


def test_import_same_root_different_name_conflicts(wb, tmp_path: Path):
    root = _make_dir(tmp_path, "repo")
    first = wb.projects.import_dir(root, name="alpha")
    before = wb.projects.list()

    with pytest.raises(ProjectRootConflict) as info:
        wb.projects.import_dir(root, name="beta")

    conflict = info.value
    assert conflict.project["id"] == first["id"]
    assert conflict.project["name"] == "alpha"
    assert conflict.root_path == normalize_root(root)
    assert str(conflict) == "该目录已登记为项目「alpha」"

    after = wb.projects.list()
    assert len(after) == len(before)
    assert {p["id"] for p in after} == {p["id"] for p in before}
    # 历史项目记录保持不变
    stored = wb.projects.get(first["id"])
    assert stored["root_path"] == str(root.resolve())
    assert stored["name"] == "alpha"


def test_import_exists_ok_returns_same_project(wb, tmp_path: Path):
    root = _make_dir(tmp_path, "repo")
    first = wb.projects.import_dir(root)
    count = len(wb.projects.list())

    same_name = wb.projects.import_dir(root, exists_ok=True)
    renamed = wb.projects.import_dir(str(root) + os.sep, name="another", exists_ok=True)

    assert same_name["id"] == first["id"]
    assert renamed["id"] == first["id"]
    assert len(wb.projects.list()) == count


def test_root_variants_are_same_directory(wb, tmp_path: Path):
    root = _make_dir(tmp_path, "RepoCase")
    first = wb.projects.import_dir(root, name="first")

    variants = [
        str(root) + os.sep,  # 尾部分隔符
        str(root) + os.sep + os.sep,  # 多重尾部分隔符
        str(root).replace("\\", "/"),  # 正斜杠
        str(root) + os.sep + ".." + os.sep + root.name,  # 相对片段绕行
    ]
    if os.name == "nt":
        variants.append(str(root).upper())  # Windows 大小写不敏感

    for index, variant in enumerate(variants):
        with pytest.raises(ProjectRootConflict) as info:
            wb.projects.import_dir(variant, name=f"variant-{index}")
        assert info.value.project["id"] == first["id"]
        assert info.value.root_path == normalize_root(root)
        assert wb.projects.find_by_root(variant)["id"] == first["id"]


def test_create_with_registered_root_conflicts(wb, tmp_path: Path):
    root = _make_dir(tmp_path, "repo")
    first = wb.projects.import_dir(root, name="alpha")
    count = len(wb.projects.list())

    for index, variant in enumerate([str(root), str(root) + os.sep]):
        with pytest.raises(ProjectRootConflict) as info:
            wb.projects.create(f"created-{index}", root_path=variant)
        assert info.value.project["id"] == first["id"]
        assert info.value.root_path == normalize_root(root)

    assert len(wb.projects.list()) == count
    assert wb.projects.find_by_root(root)["id"] == first["id"]


def test_same_name_different_roots_still_name_conflict(wb, tmp_path: Path):
    root_a = _make_dir(tmp_path, "a")
    root_b = _make_dir(tmp_path, "b")
    wb.projects.import_dir(root_a, name="shared")

    with pytest.raises(ValueError) as info:
        wb.projects.import_dir(root_b, name="shared")

    assert not isinstance(info.value, ProjectRootConflict)
    assert "项目名已存在" in str(info.value)
    assert wb.projects.find_by_root(root_b) is None
    assert len(wb.projects.list()) == 1


def test_create_name_conflict_semantics_unchanged(wb, tmp_path: Path):
    root_a = _make_dir(tmp_path, "a")
    root_b = _make_dir(tmp_path, "b")
    wb.projects.create("shared", root_path=root_a)

    with pytest.raises(ValueError) as info:
        wb.projects.create("shared", root_path=root_b)

    assert not isinstance(info.value, ProjectRootConflict)
    assert "项目名已存在" in str(info.value)
    # exists_ok 仍返回已有的同名项目（旧语义），且不登记第二个目录
    same = wb.projects.create("shared", root_path=root_b, exists_ok=True)
    assert same["name"] == "shared"
    assert wb.projects.find_by_root(root_b) is None
    assert len(wb.projects.list()) == 1


def test_normalize_root_contract(tmp_path: Path):
    root = _make_dir(tmp_path, "Dir")
    key = normalize_root(root)
    assert key == normalize_root(str(root) + os.sep)
    assert key == normalize_root(str(root).replace("\\", "/"))
    if os.name == "nt":
        assert key == normalize_root(str(root).upper())
        assert normalize_root("D:\\a") == normalize_root("d:/A/")
        assert normalize_root("D:\\a") != normalize_root("D:\\b")
        assert normalize_root("D:\\a\\") == "d:/a"
        # 跨盘符 / UNC 不能被误判为同一目录
        assert normalize_root("D:\\a") != normalize_root("E:\\a")
        assert normalize_root("\\\\localhost\\share\\a") == normalize_root("//LOCALHOST/share/a/")
        assert normalize_root("\\\\localhost\\share\\a") != normalize_root("\\\\localhost\\other\\a")
    assert normalize_root("") == ""


def test_concurrent_import_same_root_only_one_wins(wb, tmp_path: Path):
    """并发导入同一目录：判重+插入必须串行，否则会绕过 409 去重。"""
    import threading

    root = tmp_path / "race-dir"
    root.mkdir()
    barrier = threading.Barrier(2)
    results: list[tuple[str, str]] = []

    def worker(name: str) -> None:
        barrier.wait()
        try:
            project = wb.projects.import_dir(str(root), name=name)
            results.append(("ok", project["id"]))
        except Exception as exc:  # noqa: BLE001 - 断言类型
            results.append((type(exc).__name__, str(exc)))

    threads = [threading.Thread(target=worker, args=(name,)) for name in ("race-a", "race-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2, results
    kinds = sorted(kind for kind, _ in results)
    assert kinds == ["ProjectRootConflict", "ok"], results
    assert len(wb.projects.list()) == 1
