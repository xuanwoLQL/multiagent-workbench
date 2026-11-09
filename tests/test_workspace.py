"""工作区：项目、隔离、改动日志、快照。"""

from __future__ import annotations

from pathlib import Path

from multiagent.workspace.changes import ChangeJournal, diff_trees, snapshot_tree
from multiagent.workspace.blobs import BlobStore


def test_project_create_and_import(wb, tmp_path: Path):
    created = wb.projects.create("alpha")
    assert Path(created["root_path"]).exists()
    external = tmp_path / "external"
    external.mkdir()
    (external / "note.md").write_text("hello", encoding="utf-8")
    imported = wb.projects.import_dir(external, name="beta")
    assert imported["root_path"] == str(external.resolve())
    assert (external / "note.md").read_text(encoding="utf-8") == "hello"
    assert {p["name"] for p in wb.projects.list()} == {"alpha", "beta"}


def test_isolation_copy_and_commit(wb, project, set_plan):
    root = Path(project["root_path"])
    (root / "keep.txt").write_text("original", encoding="utf-8")
    set_plan("worker", {"files": {"keep.txt": "changed", "new.txt": "brand new"}, "exit": 0})
    task = wb.tasks.create(project["id"], "copy 隔离", kind="generic", agent_id="worker")
    outcome = wb.engine.run_task(task["id"])
    assert outcome.status == "success"
    assert (root / "keep.txt").read_text(encoding="utf-8") == "changed"
    assert (root / "new.txt").read_text(encoding="utf-8") == "brand new"


def test_write_scope_blocks_and_reverts(wb, project, set_plan):
    root = Path(project["root_path"])
    set_plan("worker", {"files": {"allowed/ok.txt": "y", "forbidden/secret.txt": "n"}, "exit": 0})
    task = wb.tasks.create(
        project["id"], "限定范围", kind="generic", agent_id="worker", write_scope=["allowed/**"]
    )
    outcome = wb.engine.run_task(task["id"])
    assert not (root / "forbidden/secret.txt").exists()
    assert (root / "allowed/ok.txt").exists()
    assert outcome.out_of_scope == ["forbidden/secret.txt"]
    approvals = wb.guard.list(project["id"], status="pending")
    assert any("越权写入" in a["title"] for a in approvals)


def test_snapshot_restore_and_diff(wb, project):
    root = Path(project["root_path"])
    (root / "a.txt").write_text("v1", encoding="utf-8")
    snap = wb.snapshots.create(project["id"], root, label="初始")
    (root / "a.txt").write_text("v2", encoding="utf-8")
    (root / "b.txt").write_text("new", encoding="utf-8")
    diff = wb.snapshots.diff_current(snap["id"], root)
    assert "a.txt" in diff["modified"]
    assert "b.txt" in diff["added"]
    wb.snapshots.restore(snap["id"], root, project_id=project["id"])
    assert (root / "a.txt").read_text(encoding="utf-8") == "v1"
    assert not (root / "b.txt").exists()
    assert wb.board.verify_chain(project["id"]) is True


def test_diff_trees_detects_all_actions(tmp_path: Path):
    before_dir = tmp_path / "before"
    after_dir = tmp_path / "after"
    before_dir.mkdir()
    after_dir.mkdir()
    (before_dir / "same.txt").write_text("x", encoding="utf-8")
    (after_dir / "same.txt").write_text("x", encoding="utf-8")
    (before_dir / "mod.txt").write_text("a", encoding="utf-8")
    (after_dir / "mod.txt").write_text("b", encoding="utf-8")
    (before_dir / "del.txt").write_text("bye", encoding="utf-8")
    (after_dir / "new.txt").write_text("hi", encoding="utf-8")
    changes = diff_trees(snapshot_tree(before_dir), snapshot_tree(after_dir))
    actions = {c["rel_path"]: c["action"] for c in changes}
    assert actions == {"mod.txt": "modify", "del.txt": "delete", "new.txt": "create"}


def test_scope_and_protected_patterns_keep_dotfiles():
    """``.gitignore`` / ``.env`` 这类点文件不能被归一化成 ``gitignore`` / ``env``。"""
    from multiagent.util import matches_any
    from multiagent.workspace.changes import ChangeJournal

    assert ChangeJournal.scope_check(".gitignore", [".gitignore"], False)
    assert ChangeJournal.scope_check(".env", [".env"], False)
    assert ChangeJournal.scope_check(".multiagent/config.json", [".multiagent"], False)
    assert not ChangeJournal.scope_check("gitignore", [".gitignore"], False)
    assert not ChangeJournal.scope_check("env", [".env"], False)
    assert matches_any(".gitignore", [".gitignore"])
    assert not matches_any("gitignore", [".gitignore"])


def test_change_journal_revert(tmp_path: Path):
    from multiagent.db import Database
    from multiagent.util import sha256_text

    db = Database(tmp_path / "t.sqlite3")
    blobs = BlobStore(tmp_path / "data")
    journal = ChangeJournal(db, blobs)
    root = tmp_path / "root"
    root.mkdir()
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "file.txt").write_text("orig", encoding="utf-8")
    (root / "file.txt").write_text("new", encoding="utf-8")
    journal.record(
        project_id="p",
        task_id="t",
        run_id="r",
        agent_id="a",
        changes=[
            {
                "action": "modify",
                "rel_path": "file.txt",
                "before_hash": sha256_text("orig"),
                "after_hash": sha256_text("new"),
                "before_size": 4,
                "after_size": 3,
            }
        ],
        scope_patterns=[],
        read_only=False,
        workspace_root=root,
        work_root=baseline,
    )
    stats = journal.revert_task("t", workspace_root=root)
    assert stats["reverted"] == 1
    assert (root / "file.txt").read_text(encoding="utf-8") == "orig"


def test_scope_check_rules():
    assert ChangeJournal.scope_check("a/b.txt", ["a/**"], False)
    assert not ChangeJournal.scope_check("a/b.txt", [], True)
    assert not ChangeJournal.scope_check("c.txt", ["a/**"], False)
