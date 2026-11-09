"""工作区隔离 / 冲突 / 回滚 / 快照 / 黑板记忆的加固测试（需求 11、19-26、32-37）。

组织方式：每条用例对应一个真实缺陷或一条需求，全部走 tmp 目录，不碰真实 .multiagent 库，
不使用真实 Agent（只复用 conftest 的假 Agent 夹具）。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from multiagent.db import Database
from multiagent.util import sha256_bytes, sha256_file, sha256_text
from multiagent.workspace.blobs import BlobStore
from multiagent.workspace.changes import (
    ChangeJournal,
    diff_trees,
    is_excluded,
    prune_empty_dirs,
    snapshot_tree,
    walk_files,
)


# --------------------------------------------------------------------------- 工具

def tree_hashes(root: Path, excludes=()) -> dict[str, str]:
    """整棵树的 {相对路径: sha256}，用于断言「逐字节还原」。"""
    result: dict[str, str] = {}
    root = Path(root)
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if any(is_excluded(rel, (pat,)) for pat in excludes):
            continue
        result[rel] = sha256_file(path)
    return result


def tree_fingerprint(root: Path) -> str:
    """把整棵树的哈希折叠成一个指纹，便于比较「完全一致」。"""
    hashes = tree_hashes(root)
    return sha256_text("\n".join(f"{k}:{v}" for k, v in sorted(hashes.items())))


def make_journal(tmp_path: Path, name: str = "t") -> ChangeJournal:
    db = Database(tmp_path / f"{name}.sqlite3")
    blobs = BlobStore(tmp_path / f"{name}-data")
    return ChangeJournal(db, blobs)


def seed_conflicting_task(
    journal: ChangeJournal,
    *,
    project_id: str,
    task_id: str,
    rel_path: str,
    action: str,
    before_hash: str | None,
    after_hash: str | None,
    is_binary: bool,
    status: str = "pending",
) -> None:
    """写入一条「另一个仍在进行中的任务改了同一文件」的记录，用于冲突场景。"""
    journal.db.insert(
        "file_changes",
        {
            "project_id": project_id,
            "task_id": task_id,
            "run_id": "run-x",
            "agent_id": "other",
            "rel_path": rel_path,
            "action": action,
            "before_hash": before_hash,
            "after_hash": after_hash,
            "before_size": None,
            "after_size": None,
            "in_scope": 1,
            "mode": "committed",
            "conflict": 0,
            "is_binary": 1 if is_binary else 0,
            "fragment": None,
            "at": "2026-01-01T00:00:00.000Z",
        },
    )
    journal.db.insert(
        "tasks",
        {
            "id": task_id,
            "project_id": project_id,
            "title": f"并发任务 {task_id}",
            "kind": "generic",
            "status": status,
            "created_at": "2026-01-01T00:00:00.000Z",
            "updated_at": "2026-01-01T00:00:00.000Z",
        },
    )


def symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台相关
        pytest.skip(f"当前平台无法创建符号链接（{exc}）")


PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    b"2 0 obj\n<< /Filter /FlateDecode /Length 24 >>\nstream\n"
    b"\x78\x9c\xcb\x48\xcd\xc9\xc9\x07\x00\x0a\x2c\x02\x15\x00\x00\x00\x00\xff\xfe\x00"
    b"\nendstream\nendobj\ntrailer\n%%EOF\n"
)
GBK_BYTES = "中文文本".encode("gbk")  # 无 NUL，但非 UTF-8
FF_BYTES = b"\xff" * 64  # 无 NUL，但非 UTF-8


# =========================================================================== 1 越权

class TestScopeEscapes:
    """需求 35：写范围校验不能被绕过。"""

    def test_directory_prefix_confusion(self):
        """scope=src 时 srcevil/x 不能被误判为合法。"""
        assert ChangeJournal.scope_check("src/main.py", ["src"], False) is True
        assert ChangeJournal.scope_check("srcevil/x.py", ["src"], False) is False
        assert ChangeJournal.scope_check("src-evil/x.py", ["src"], False) is False

    def test_case_difference_on_windows(self):
        """Windows 大小写不敏感：src/** 必须覆盖 SRC/main.py，否则是绕过。"""
        assert ChangeJournal.scope_check("SRC/main.py", ["src/**"], False) is True
        assert ChangeJournal.scope_check("Src/Sub/M.PY", ["src/**"], False) is True
        assert ChangeJournal.scope_check("../SRC/main.py", ["src/**"], False) is False

    def test_parent_traversal_rejected(self):
        for rel in ("../evil.txt", "src/../../evil.txt", "..\\evil.txt", "a/../../b"):
            assert ChangeJournal.scope_check(rel, ["**"], False) is False, rel

    def test_windows_absolute_and_unc_rejected(self):
        for rel in (
            "C:/Windows/Temp/x.txt",
            "c:\\windows\\temp\\x.txt",
            "//attacker/share/x.txt",
            "\\\\attacker\\share\\x.txt",
            "\\\\?\\C:\\Windows\\x.txt",
        ):
            assert ChangeJournal.scope_check(rel, ["**"], False) is False, rel

    def test_scope_pattern_with_traversal_cannot_widen_rights(self):
        """scope 本身写成 ../** 也不能把范围扩到工作区外。"""
        assert ChangeJournal.scope_check("../evil.txt", ["../**"], False) is False

    def test_record_marks_escaping_and_case_bypass_as_out_of_scope(self, tmp_path: Path):
        """record() 是最终闸门：越界/大小写绕过的改动必须落成 in_scope=0。"""
        root = tmp_path / "root"
        root.mkdir()
        (root / "SRC").mkdir()
        (root / "SRC" / "main.py").write_text("x", encoding="utf-8")
        work = tmp_path / "work"
        work.mkdir()
        (work / "SRC").mkdir()
        (work / "SRC" / "main.py").write_text("hacked", encoding="utf-8")
        journal = make_journal(tmp_path)
        recorded = journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=[
                {"action": "modify", "rel_path": "SRC/main.py", "before_hash": None, "after_hash": None},
                {"action": "modify", "rel_path": "../escape.txt", "before_hash": None, "after_hash": None},
            ],
            scope_patterns=["src/**"],
            read_only=False,
            workspace_root=root,
            work_root=work,
        )
        assert [r.in_scope for r in recorded] == [True, False]
        assert recorded[0].escaped_path is False
        assert recorded[1].escaped_path is True and recorded[1].reason

    def test_symlink_inside_scope_pointing_outside_is_rejected(self, tmp_path: Path):
        """scope 内的符号链接指向工作区外：必须判定为越权。"""
        root = tmp_path / "root"
        (root / "src").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("s", encoding="utf-8")
        symlink_or_skip(root / "src" / "link.txt", outside / "secret.txt")
        journal = make_journal(tmp_path)
        recorded = journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=[{"action": "modify", "rel_path": "src/link.txt", "before_hash": None, "after_hash": None}],
            scope_patterns=["src/**"],
            read_only=False,
            workspace_root=root,
        )
        assert recorded[0].in_scope is False

    def test_record_does_not_accept_escaping_rel_path(self, tmp_path: Path):
        """即使调用方直接塞越界 rel_path，也不得被当成合法范围内改动。"""
        root = tmp_path / "root"
        root.mkdir()
        journal = make_journal(tmp_path)
        recorded = journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=[{"action": "create", "rel_path": "../outside.txt", "before_hash": None, "after_hash": None}],
            scope_patterns=["**"],
            read_only=False,
            workspace_root=root,
        )
        assert recorded[0].in_scope is False

    def test_revert_refuses_path_outside_workspace(self, tmp_path: Path):
        """回滚不能被越界 rel_path 当成任意文件写原语。"""
        from multiagent.workspace.changes import PathEscapeError

        root = tmp_path / "root"
        root.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("keep", encoding="utf-8")
        journal = make_journal(tmp_path)
        digest = journal.blobs.put_bytes(b"pwned")
        journal.db.insert(
            "file_changes",
            {
                "project_id": "p",
                "task_id": "t",
                "run_id": "r",
                "agent_id": "a",
                "rel_path": "../victim.txt",
                "action": "modify",
                "before_hash": digest,
                "after_hash": None,
                "before_size": None,
                "after_size": None,
                "in_scope": 1,
                "mode": "committed",
                "conflict": 0,
                "is_binary": 0,
                "fragment": None,
                "at": "2026-01-01T00:00:00.000Z",
            },
        )
        row_id = int(journal.db.query_one("SELECT MAX(id) AS m FROM file_changes")["m"])
        with pytest.raises(PathEscapeError):
            journal.revert(row_id, workspace_root=root)
        assert victim.read_text(encoding="utf-8") == "keep"


# =========================================================================== 2 二进制冲突

class TestBinaryConflict:
    """需求 34：非文本文件（Office/图片/PDF）并发改动必须识别冲突并阻止自动合并。"""

    @pytest.mark.parametrize(
        "name,payload",
        [
            ("report.pdf", PDF_BYTES),
            ("blob.bin", FF_BYTES),
            ("legacy.txt", GBK_BYTES),
            ("photo.png", b"\x89PNG\r\n\x1a\n" + b"\x00\x01\x02" * 10),
        ],
    )
    def test_binary_concurrent_edit_is_conflict_and_manual_review(self, tmp_path: Path, name, payload):
        journal = make_journal(tmp_path)
        before = sha256_bytes(payload)
        after = sha256_bytes(payload + b"changed")
        seed_conflicting_task(
            journal,
            project_id="p",
            task_id="t-other",
            rel_path=name,
            action="modify",
            before_hash=before,
            after_hash=after,
            is_binary=True,
        )
        recorded = journal.record(
            project_id="p",
            task_id="t-mine",
            run_id="r",
            agent_id="a",
            changes=[
                {
                    "action": "modify",
                    "rel_path": name,
                    "before_hash": before,
                    "after_hash": sha256_bytes(payload + b"other"),
                    "is_binary": True,
                }
            ],
            scope_patterns=[],
            read_only=False,
            workspace_root=tmp_path,
        )
        assert recorded[0].conflict is True
        assert recorded[0].manual_review is True

    def test_binary_conflict_falls_back_to_manual_review_when_action_unknown(self, tmp_path: Path):
        """识别不了（改动类型不一致）时不能静默合并：必须转人工。"""
        journal = make_journal(tmp_path)
        seed_conflicting_task(
            journal,
            project_id="p",
            task_id="t-other",
            rel_path="doc.docx",
            action="create",
            before_hash=None,
            after_hash="deadbeef",
            is_binary=True,
        )
        recorded = journal.record(
            project_id="p",
            task_id="t-mine",
            run_id="r",
            agent_id="a",
            changes=[{"action": "delete", "rel_path": "doc.docx", "before_hash": "deadbeef", "is_binary": True}],
            scope_patterns=[],
            read_only=False,
            workspace_root=tmp_path,
        )
        assert recorded[0].conflict is True
        assert recorded[0].manual_review is True

    def test_identical_binary_change_is_not_a_conflict(self, tmp_path: Path):
        """两个任务写出逐字节相同的结果不算冲突（无需打扰人工）。"""
        payload = PDF_BYTES
        digest = sha256_bytes(payload)
        journal = make_journal(tmp_path)
        seed_conflicting_task(
            journal,
            project_id="p",
            task_id="t-other",
            rel_path="report.pdf",
            action="modify",
            before_hash="same-before",
            after_hash=digest,
            is_binary=True,
        )
        recorded = journal.record(
            project_id="p",
            task_id="t-mine",
            run_id="r",
            agent_id="a",
            changes=[
                {"action": "modify", "rel_path": "report.pdf", "before_hash": "same-before", "after_hash": digest, "is_binary": True}
            ],
            scope_patterns=[],
            read_only=False,
            workspace_root=tmp_path,
        )
        assert recorded[0].conflict is False
        assert recorded[0].manual_review is False

    def test_ascii_only_pdf_goes_through_real_pipeline_as_binary(self, tmp_path: Path):
        """纯 ASCII 的 PDF（无 NUL、UTF-8 可解码）必须靠后缀表判成二进制。

        这里走**真实检测链路**：`snapshot_tree → diff_trees → record`，不手工塞
        `is_binary`。修复前这条会失败（ASCII PDF 被判文本，需求 34 的「二进制并发改动
        转人工」被绕开）。
        """
        ascii_pdf = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"
        before_dir = tmp_path / "before"
        after_dir = tmp_path / "after"
        before_dir.mkdir()
        after_dir.mkdir()
        (before_dir / "plain.pdf").write_bytes(ascii_pdf)
        (after_dir / "plain.pdf").write_bytes(ascii_pdf + b"% trailing comment\n")

        journal = make_journal(tmp_path)
        seed_conflicting_task(
            journal,
            project_id="p",
            task_id="t-other",
            rel_path="plain.pdf",
            action="modify",
            before_hash=sha256_bytes(ascii_pdf),
            after_hash="other",
            is_binary=False,  # 并发方历史记录里没标二进制也不影响本次判定
        )
        changes = diff_trees(snapshot_tree(before_dir), snapshot_tree(after_dir))
        assert changes[0]["is_binary"] is True, "known-suffix 文件必须由后缀表直接判为二进制"
        recorded = journal.record(
            project_id="p",
            task_id="t-mine",
            run_id="r",
            agent_id="a",
            changes=changes,
            scope_patterns=[],
            read_only=False,
            workspace_root=tmp_path,
        )
        assert recorded[0].conflict is True
        assert recorded[0].manual_review is True, "二进制并发改动必须转人工，不允许自动合并"
        row = journal.db.query_one("SELECT is_binary, conflict FROM file_changes WHERE task_id='t-mine'")
        assert (row["is_binary"], row["conflict"]) == (1, 1)

    def test_unknown_suffix_ascii_only_still_conflicts_but_is_not_manual_review(self, tmp_path: Path):
        """已知下界：**未知后缀** + 纯 ASCII + 无 NUL 的文件仍会被判成文本。

        后缀表和 NUL/UTF-8 嗅探都识别不了它，但它依然是路径级冲突 → 必须阻止
        自动合并；只是不会打 `manual_review`（不误杀）。这是记录在案的保守下界，
        文件名里已写明是「未知后缀」而非 PDF。
        """
        ascii_payload = b"just plain ascii bytes, no NUL, decodes as utf-8\n"
        journal = make_journal(tmp_path)
        seed_conflicting_task(
            journal,
            project_id="p",
            task_id="t-other",
            rel_path="mystery.xyz",
            action="modify",
            before_hash=sha256_bytes(ascii_payload),
            after_hash="other",
            is_binary=False,
        )
        recorded = journal.record(
            project_id="p",
            task_id="t-mine",
            run_id="r",
            agent_id="a",
            changes=[
                {
                    "action": "modify",
                    "rel_path": "mystery.xyz",
                    "before_hash": "b",
                    "after_hash": "mine",
                    "is_binary": False,
                }
            ],
            scope_patterns=[],
            read_only=False,
            workspace_root=tmp_path,
        )
        assert recorded[0].conflict is True  # 依然阻止自动合并
        assert recorded[0].manual_review is False

    def test_engine_blocks_auto_merge_on_binary_conflict(self, wb, project):
        """端到端（需求 34）：另一个进行中任务改过同一个二进制文件时，
        必须阻止自动合并、转人工，且原工作区文件不被覆盖。"""
        from multiagent.agents.base import AgentResult

        root = Path(project["root_path"])
        original = b"\x89PNG\r\n\x1a\n" + b"\x00\x11\x22\x33" * 16
        (root / "photo.png").write_bytes(original)
        digest = sha256_file(root / "photo.png")

        # 造出「另一个任务已经改过同一文件」的进行中状态
        other = wb.tasks.create(project["id"], "并发改图", kind="generic", agent_id="worker")
        wb.runner.journal.db.insert(
            "file_changes",
            {
                "project_id": project["id"],
                "task_id": other["id"],
                "run_id": "run-other",
                "agent_id": "worker",
                "rel_path": "photo.png",
                "action": "modify",
                "before_hash": digest,
                "after_hash": "0" * 64,
                "before_size": len(original),
                "after_size": len(original),
                "in_scope": 1,
                "mode": "committed",
                "conflict": 0,
                "is_binary": 1,
                "fragment": None,
                "at": "2026-01-01T00:00:00.000Z",
            },
        )

        class _StubAdapter:
            id = "worker"
            label = "Worker"
            protocol = "cli"

            def describe(self):
                return {"id": self.id}

            def check(self, timeout: float = 20.0):
                return {"ok": True}

            def run(self, request):
                (Path(request.workspace) / "photo.png").write_bytes(original + b"TAMPER")
                return AgentResult(ok=True, exit_code=0, duration_ms=1, stdout="stub", stderr="")

        wb.runner._adapters["worker"] = _StubAdapter()
        mine = wb.tasks.create(project["id"], "我改图", kind="generic", agent_id="worker")
        outcome = wb.runner.run(mine["id"])
        assert outcome.conflicts == ["photo.png"]
        assert outcome.status == "blocked"
        assert outcome.committed == []
        assert (root / "photo.png").read_bytes() == original, "冲突文件绝不能被自动覆盖"
        rows = wb.db.query(
            "SELECT conflict, is_binary, fragment FROM file_changes WHERE task_id = ?", (mine["id"],)
        )
        assert len(rows) == 1 and rows[0]["conflict"] == 1 and rows[0]["is_binary"] == 1
        approvals = wb.guard.list(project["id"], status="pending")
        assert any(a["kind"] == "conflict" for a in approvals)

    def test_audit_pairs_by_rel_path_not_position(self, wb, tmp_path: Path):
        """越权改动被 runner 剪枝后，冲突/保护判定仍必须按 rel_path 配对。

        曾经的真实缺陷：`guard.audit_changes` 用 `zip(changes, recorded)`。`runner._finalize()`
        会先把**越权项**从 `changes` 里剔除，`changes` 一短就整体错位，
        **列表尾部的冲突被静默丢弃**，需求 34 的「阻止自动合并」在特定排序下失效。
        现已改为按 `rel_path` 建索引（`guard._index_records`）。

        场景构造：`aaa.txt` 越权（排序在前，会被剪掉）、`zz.txt` 在范围内但与并发任务冲突
        （排序在后）。剪枝后只剩 `zz.txt`，冲突必须仍然被识别。
        """
        from multiagent.safety.guard import GuardService

        journal = wb.runner.journal
        seed_conflicting_task(
            journal,
            project_id="p",
            task_id="t-other",
            rel_path="zz.txt",
            action="modify",
            before_hash="b",
            after_hash="c",
            is_binary=False,
        )
        changes = [
            {"action": "create", "rel_path": "aaa.txt", "before_hash": None, "after_hash": "h1", "is_binary": False},
            {"action": "modify", "rel_path": "zz.txt", "before_hash": "b", "after_hash": "m", "is_binary": False},
        ]
        # 只有 aaa.txt 被排除在写范围外 → 会被 runner 剪掉
        recorded = journal.record(
            project_id="p",
            task_id="t-mine",
            run_id="r",
            agent_id="a",
            changes=changes,
            scope_patterns=["zz.txt"],
            deny_patterns=["aaa.txt"],
            read_only=False,
            workspace_root=tmp_path,
        )
        assert {r.rel_path: r.in_scope for r in recorded} == {"aaa.txt": False, "zz.txt": True}
        assert {r.rel_path: r.conflict for r in recorded} == {"aaa.txt": False, "zz.txt": True}

        guard = GuardService(wb.db, wb.config)
        project = {"id": "p", "root_path": str(tmp_path)}
        task = {"id": "t-mine", "title": "t", "write_scope": ["zz.txt"], "read_only": 0}
        # runner._finalize 的真实形态：越权项 aaa.txt 已从 changes 里剪掉
        pruned = [c for c in changes if c["rel_path"] == "zz.txt"]
        audit = guard.audit_changes(project=project, task=task, changes=pruned, recorded=recorded)
        assert audit["conflicts"] == ["zz.txt"], "剪枝后不得丢掉尾部冲突"
        # 未剪枝形态也必须一致（顺序无关）
        full = guard.audit_changes(project=project, task=task, changes=changes, recorded=recorded)
        assert full["conflicts"] == ["zz.txt"]
        assert full["out_of_scope"] == ["aaa.txt"]

    def test_engine_out_of_scope_pruning_preserves_binary_conflict(self, wb, project):
        """端到端：同一次运行里「越权写在前 + 二进制冲突在后」，冲突不能被剪枝吃掉。

        `forbidden/x.txt`（越权，排序在前）会被 `runner._finalize` 从 changes 里剔除；
        若 audit 按位置配对，紧随其后的 `photo.png` 冲突就丢了 → 任务被判成功并自动
        回收文件。这里断言：冲突仍被识别、任务 blocked、冲突产物没被覆盖。
        """
        from multiagent.agents.base import AgentResult

        root = Path(project["root_path"])
        original = b"\x89PNG\r\n\x1a\n" + b"\x00\x11\x22\x33" * 16
        (root / "photo.png").write_bytes(original)

        other = wb.tasks.create(project["id"], "并发改图", kind="generic", agent_id="worker")
        wb.runner.journal.db.insert(
            "file_changes",
            {
                "project_id": project["id"],
                "task_id": other["id"],
                "run_id": "run-other",
                "agent_id": "worker",
                "rel_path": "photo.png",
                "action": "modify",
                "before_hash": sha256_file(root / "photo.png"),
                "after_hash": "0" * 64,
                "before_size": len(original),
                "after_size": len(original),
                "in_scope": 1,
                "mode": "committed",
                "conflict": 0,
                "is_binary": 1,
                "fragment": None,
                "at": "2026-01-01T00:00:00.000Z",
            },
        )

        class _StubAdapter:
            id = "worker"
            label = "Worker"
            protocol = "cli"

            def describe(self):
                return {"id": self.id}

            def check(self, timeout: float = 20.0):
                return {"ok": True}

            def run(self, request):
                workspace = Path(request.workspace)
                (workspace / "photo.png").write_bytes(original + b"TAMPER")
                (workspace / "forbidden" / "x.txt").parent.mkdir(parents=True, exist_ok=True)
                (workspace / "forbidden" / "x.txt").write_text("out of scope", encoding="utf-8")
                return AgentResult(ok=True, exit_code=0, duration_ms=1, stdout="stub", stderr="")

        wb.runner._adapters["worker"] = _StubAdapter()
        task = wb.tasks.create(
            project["id"], "越权+冲突", kind="generic", agent_id="worker", write_scope=["photo.png"]
        )
        outcome = wb.runner.run(task["id"])
        assert outcome.out_of_scope == ["forbidden/x.txt"]
        assert "photo.png" in outcome.conflicts, "剪枝后的尾部冲突必须仍然被识别"
        assert outcome.status == "blocked"
        assert outcome.committed == []
        assert (root / "photo.png").read_bytes() == original
        assert not (root / "forbidden" / "x.txt").exists()
        assert any(a["kind"] == "conflict" for a in wb.guard.list(project["id"], status="pending"))

    def test_text_conflict_from_finished_task_is_not_conflict(self, tmp_path: Path):
        """已结束任务的改动不再构成冲突。"""
        journal = make_journal(tmp_path)
        seed_conflicting_task(
            journal,
            project_id="p",
            task_id="t-done",
            rel_path="a.txt",
            action="modify",
            before_hash="b",
            after_hash="c",
            is_binary=False,
            status="success",
        )
        recorded = journal.record(
            project_id="p",
            task_id="t-mine",
            run_id="r",
            agent_id="a",
            changes=[{"action": "modify", "rel_path": "a.txt", "before_hash": "b", "after_hash": "m"}],
            scope_patterns=[],
            read_only=False,
            workspace_root=tmp_path,
        )
        assert recorded[0].conflict is False

    def test_binary_file_is_flagged_binary_in_journal(self, tmp_path: Path):
        """二进制标记必须真正落库（配合需求 34 的判定）。"""
        root = tmp_path / "root"
        root.mkdir()
        (root / "report.pdf").write_bytes(PDF_BYTES)
        journal = make_journal(tmp_path)
        changes = diff_trees({}, snapshot_tree(root))
        journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=changes,
            scope_patterns=[],
            read_only=False,
            workspace_root=root,
        )
        row = journal.db.query_one("SELECT is_binary FROM file_changes WHERE rel_path='report.pdf'")
        assert row["is_binary"] == 1


# =========================================================================== 3 回滚精确性

class TestRevertPrecision:
    """需求 36：新建/删除/改名/二进制四类改动回滚后必须逐字节还原。"""

    def _record_and_revert(self, tmp_path: Path, before: dict[str, str], after: dict[str, str], name: str):
        """构造 root(=改动后) 与 baseline(=改动前) 两棵树，跑完整 record→revert_task。"""
        root = tmp_path / f"root-{name}"
        baseline = tmp_path / f"baseline-{name}"
        root.mkdir()
        baseline.mkdir()
        for rel, text in after.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        for rel, text in before.items():
            target = baseline / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        journal = make_journal(tmp_path, name)
        changes = diff_trees(snapshot_tree(baseline), snapshot_tree(root))
        journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=changes,
            scope_patterns=[],
            read_only=False,
            workspace_root=root,
            work_root=baseline,
            after_root=root,
        )
        stats = journal.revert_task("t", workspace_root=root)
        keep_dirs = {
            "/".join(rel.split("/")[: i + 1])
            for rel in before
            for i in range(len(rel.split("/")) - 1)
        }
        prune_empty_dirs(root, keep=keep_dirs)
        return root, stats

    def test_revert_create(self, tmp_path: Path):
        root, stats = self._record_and_revert(tmp_path, {}, {"new/a.txt": "brand new"}, "create")
        assert stats["reverted"] == 1 and stats["failed"] == 0
        assert tree_hashes(root) == {}

    def test_revert_delete(self, tmp_path: Path):
        root, stats = self._record_and_revert(tmp_path, {"gone.txt": "bye"}, {}, "delete")
        assert stats["reverted"] == 1 and stats["failed"] == 0
        assert (root / "gone.txt").read_text(encoding="utf-8") == "bye"

    def test_revert_modify(self, tmp_path: Path):
        root, stats = self._record_and_revert(tmp_path, {"a.txt": "v1"}, {"a.txt": "v2"}, "modify")
        assert stats["reverted"] == 1 and stats["failed"] == 0
        assert (root / "a.txt").read_text(encoding="utf-8") == "v1"

    def test_revert_rename_is_byte_exact(self, tmp_path: Path):
        """改名 = delete(old) + create(new)，回滚后必须同时还原两侧。"""
        root, stats = self._record_and_revert(
            tmp_path, {"old/name.txt": "content-x"}, {"new/name.txt": "content-x"}, "rename"
        )
        assert stats["reverted"] == 2 and stats["failed"] == 0
        assert (root / "old/name.txt").read_text(encoding="utf-8") == "content-x"
        assert not (root / "new").exists()

    def test_revert_binary_byte_exact(self, tmp_path: Path):
        root = tmp_path / "bin-root"
        baseline = tmp_path / "bin-baseline"
        root.mkdir()
        baseline.mkdir()
        (baseline / "report.pdf").write_bytes(PDF_BYTES)
        (root / "report.pdf").write_bytes(b"%PDF-1.4\nTAMPERED\n")
        journal = make_journal(tmp_path, "bin")
        changes = diff_trees(snapshot_tree(baseline), snapshot_tree(root))
        journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=changes,
            scope_patterns=[],
            read_only=False,
            workspace_root=root,
            work_root=baseline,
            after_root=root,
        )
        stats = journal.revert_task("t", workspace_root=root)
        assert stats["reverted"] == 1 and stats["failed"] == 0
        assert (root / "report.pdf").read_bytes() == PDF_BYTES

    def test_revert_same_file_twice_restores_original(self, tmp_path: Path):
        """同一文件在一次任务里被连续改两次：回滚必须回到最初内容，不是中间态。"""
        root = tmp_path / "twice-root"
        work = tmp_path / "twice-work"
        root.mkdir()
        work.mkdir()
        (work / "a.txt").write_text("v1", encoding="utf-8")
        (root / "a.txt").write_text("v3", encoding="utf-8")
        journal = make_journal(tmp_path, "twice")
        journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=[
                {"action": "modify", "rel_path": "a.txt", "before_hash": sha256_text("v2"), "after_hash": sha256_text("v3")}
            ],
            scope_patterns=[],
            read_only=False,
            workspace_root=root,
            work_root=work,
            after_root=root,
        )
        journal.record(
            project_id="p",
            task_id="t",
            run_id="r2",
            agent_id="a",
            changes=[
                {"action": "modify", "rel_path": "a.txt", "before_hash": sha256_text("v1"), "after_hash": sha256_text("v2")}
            ],
            scope_patterns=[],
            read_only=False,
            workspace_root=root,
            work_root=work,
            after_root=root,
        )
        stats = journal.revert_task("t", workspace_root=root)
        assert stats["failed"] == 0, stats
        assert (root / "a.txt").read_text(encoding="utf-8") == "v1"

    def test_missing_blob_raises_explicit_error(self, tmp_path: Path):
        """blob 缺失必须显式报错，绝不静默成功。"""
        from multiagent.workspace.changes import BlobMissingError

        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("v2", encoding="utf-8")
        journal = make_journal(tmp_path)
        journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=[
                {"action": "modify", "rel_path": "a.txt", "before_hash": sha256_text("v1"), "after_hash": sha256_text("v2")}
            ],
            scope_patterns=[],
            read_only=False,
            workspace_root=root,
        )
        row = journal.db.query_one("SELECT * FROM file_changes WHERE task_id='t'")
        # 模拟 blob 仓库被清理/损坏
        journal.blobs.path_for(row["before_hash"]).unlink()
        with pytest.raises(BlobMissingError, match="缺失"):
            journal.revert(int(row["id"]), workspace_root=root)
        assert (root / "a.txt").read_text(encoding="utf-8") == "v2"
        assert journal.db.query_one("SELECT mode FROM file_changes WHERE id=?", (row["id"],))["mode"] == "committed"

    def test_revert_task_reports_missing_blob_as_failure(self, tmp_path: Path):
        """批量回滚遇到 blob 缺失：计入 failed 并给出错误原因，记录保留可重试。"""
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("v2", encoding="utf-8")
        journal = make_journal(tmp_path)
        journal.record(
            project_id="p",
            task_id="t",
            run_id="r",
            agent_id="a",
            changes=[
                {"action": "modify", "rel_path": "a.txt", "before_hash": sha256_text("v1"), "after_hash": sha256_text("v2")}
            ],
            scope_patterns=[],
            read_only=False,
            workspace_root=root,
        )
        row = journal.db.query_one("SELECT * FROM file_changes WHERE task_id='t'")
        journal.blobs.path_for(row["before_hash"]).unlink()
        stats = journal.revert_task("t", workspace_root=root)
        assert stats["failed"] == 1
        assert stats["errors"] and "缺失" in stats["errors"][0]
        assert journal.db.query_one("SELECT mode FROM file_changes WHERE task_id='t'")["mode"] == "committed"

    def test_engine_revert_returns_tree_to_pre_change_state(self, wb, project, set_plan):
        """端到端：越权运行留下的改动必须被还原成运行前状态。

        注：同一任务里范围内的改动仍会回收，所以这里只断言「越权路径」回到原样。
        """
        root = Path(project["root_path"])
        (root / "keep.txt").write_text("original", encoding="utf-8")
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
        before_out_of_scope = (root / "keep.txt").read_bytes()
        set_plan(
            "worker",
            {"files": {"src/a.py": "print(2)\n", "forbidden/x.txt": "n"}, "exit": 0},
        )
        task = wb.tasks.create(
            project["id"], "范围外改动", kind="generic", agent_id="worker", write_scope=["src/**"]
        )
        outcome = wb.engine.run_task(task["id"])
        assert outcome.out_of_scope == ["forbidden/x.txt"]
        assert not (root / "forbidden").exists(), "越权写入必须被还原（含目录骨架）"
        assert (root / "keep.txt").read_bytes() == before_out_of_scope
        rows = wb.db.query(
            "SELECT rel_path, in_scope FROM file_changes WHERE task_id = ? ORDER BY rel_path", (task["id"],)
        )
        assert {r["rel_path"]: r["in_scope"] for r in rows} == {
            "forbidden/x.txt": 0,
            "src/a.py": 1,
        }

    def test_engine_revert_keeps_in_scope_changes(self, wb, project, set_plan):
        """范围外被还原的同时，范围内的改动必须保留（不能一刀切全回滚）。"""
        root = Path(project["root_path"])
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "a.py").write_text("print(1)\n", encoding="utf-8")
        set_plan("worker", {"files": {"src/a.py": "print(2)\n", "forbidden/x.txt": "n"}, "exit": 0})
        task = wb.tasks.create(
            project["id"], "部分越权", kind="generic", agent_id="worker", write_scope=["src/**"]
        )
        wb.engine.run_task(task["id"])
        assert (root / "src" / "a.py").read_text(encoding="utf-8") == "print(2)\n"
        assert not (root / "forbidden").exists()


# =========================================================================== 4 快照

class TestSnapshots:
    """需求 11：大量文件 / 二进制 / 两次快照对比的正确性与性能。"""

    def test_large_tree_snapshot_diff_restore(self, wb, project, tmp_path: Path):
        import time

        root = Path(project["root_path"])
        for i in range(1200):
            target = root / "bulk" / f"d{i % 40}" / f"f{i:05d}.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"content-{i}", encoding="utf-8")
        (root / "bulk" / "d0" / "blob.bin").write_bytes(b"\x00\xff" * 512)

        started = time.monotonic()
        snap_a = wb.snapshots.create(project["id"], root, label="大量文件A")
        create_ms = (time.monotonic() - started) * 1000
        assert snap_a["file_count"] >= 1200
        assert create_ms < 60_000, f"快照创建退化：{create_ms:.0f}ms"

        # 改动：改一个文本、删一个、加一个、改二进制
        (root / "bulk" / "d1" / "f00001.txt").write_text("changed", encoding="utf-8")
        (root / "bulk" / "d2" / "f00002.txt").unlink()
        (root / "bulk" / "d3" / "added.txt").write_text("added", encoding="utf-8")
        (root / "bulk" / "d0" / "blob.bin").write_bytes(b"\x01\x02" * 300)

        snap_b = wb.snapshots.create(project["id"], root, label="大量文件B")
        started = time.monotonic()
        diff = wb.snapshots.diff(snap_a["id"], snap_b["id"])
        diff_ms = (time.monotonic() - started) * 1000
        assert diff["added"] == ["bulk/d3/added.txt"]
        assert diff["removed"] == ["bulk/d2/f00002.txt"]
        assert set(diff["modified"]) == {"bulk/d0/blob.bin", "bulk/d1/f00001.txt"}
        assert diff["counts"] == {"added": 1, "removed": 1, "modified": 2}
        assert diff_ms < 60_000, f"快照对比退化：{diff_ms:.0f}ms"

        expected_after_restore = tree_hashes(root)
        started = time.monotonic()
        wb.snapshots.restore(snap_a["id"], root, project_id=project["id"])
        restore_ms = (time.monotonic() - started) * 1000
        assert restore_ms < 120_000, f"快照回退退化：{restore_ms:.0f}ms"
        assert (root / "bulk" / "d2" / "f00002.txt").read_text(encoding="utf-8") == "content-2"
        assert not (root / "bulk" / "d3" / "added.txt").exists()
        assert (root / "bulk" / "d1" / "f00001.txt").read_text(encoding="utf-8") == "content-1"
        assert (root / "bulk" / "d0" / "blob.bin").read_bytes() == b"\x00\xff" * 512
        assert len(tree_hashes(root)) == snap_a["file_count"]
        assert tree_hashes(root) != expected_after_restore

    def test_snapshot_restore_is_exact_for_deletions_and_binaries(self, wb, project):
        root = Path(project["root_path"])
        (root / "keep").mkdir()
        (root / "keep" / "a.txt").write_text("A", encoding="utf-8")
        (root / "keep" / "b.bin").write_bytes(bytes(range(256)) * 4)
        snap = wb.snapshots.create(project["id"], root, label="原始")
        original = tree_fingerprint(root)

        (root / "keep" / "a.txt").write_text("A2", encoding="utf-8")
        (root / "keep" / "b.bin").unlink()
        (root / "keep" / "c.txt").write_text("C", encoding="utf-8")
        wb.snapshots.restore(snap["id"], root, project_id=project["id"])
        assert tree_fingerprint(root) == original
        assert (root / "keep" / "b.bin").read_bytes() == bytes(range(256)) * 4

    def test_restore_prunes_leftover_empty_dirs(self, wb, project):
        """回退后不应留下改动期间新造的空目录（否则不是「回到快照状态」）。"""
        root = Path(project["root_path"])
        (root / "a.txt").write_text("A", encoding="utf-8")
        snap = wb.snapshots.create(project["id"], root, label="原始")
        (root / "leftover" / "deep").mkdir(parents=True)
        (root / "leftover" / "deep" / "x.txt").write_text("x", encoding="utf-8")
        wb.snapshots.restore(snap["id"], root, project_id=project["id"])
        assert not (root / "leftover").exists()


# =========================================================================== 5 只读任务

class TestReadOnlyIsolation:
    """需求 37：只读任务无论用什么路径都不能写回原工作区。"""

    def test_read_only_task_cannot_touch_origin_workspace(self, wb, project, set_plan):
        root = Path(project["root_path"])
        (root / "src").mkdir(exist_ok=True)
        (root / "src" / "real.txt").write_text("real", encoding="utf-8")
        before = tree_fingerprint(root)
        set_plan("worker", {"files": {"src/real.txt": "hacked", "src/new.txt": "new"}, "exit": 0})
        task = wb.tasks.create(project["id"], "只读评审", kind="review", agent_id="worker")
        outcome = wb.engine.run_task(task["id"])
        assert tree_fingerprint(root) == before
        assert outcome.committed == []

    def test_read_only_task_cannot_write_through_symlink(self, wb, project, set_plan, tmp_path: Path):
        root = Path(project["root_path"])
        secret = tmp_path / "secret.txt"
        secret.write_text("secret", encoding="utf-8")
        symlink_or_skip(root / "link.txt", secret)
        before_root = tree_fingerprint(root)
        before_secret = sha256_file(secret)
        set_plan("worker", {"files": {"link.txt": "overwritten"}, "exit": 0})
        task = wb.tasks.create(project["id"], "只读穿越", kind="review", agent_id="worker")
        wb.engine.run_task(task["id"])
        assert tree_fingerprint(root) == before_root
        assert sha256_file(secret) == before_secret

    def test_commit_back_refuses_symlink_smuggling(self, wb, project, tmp_path: Path):
        """工作副本里指向工作区外的符号链接，绝不能被当作文件内容回收。"""
        root = Path(project["root_path"])
        secret = tmp_path / "external-secret.txt"
        secret.write_text("EXTERNAL-SECRET", encoding="utf-8")
        work = tmp_path / "work-copy"
        work.mkdir()
        symlink_or_skip(work / "leak.txt", secret)
        committed = wb.runner.isolation.commit_back(
            project,
            work,
            [{"action": "create", "rel_path": "leak.txt"}],
            only_in_scope=True,
            scope=["**"],
            read_only=False,
        )
        assert committed == []
        assert not (root / "leak.txt").exists()

    def test_commit_back_refuses_traversal_paths(self, wb, project, tmp_path: Path):
        root = Path(project["root_path"])
        victim = tmp_path / "victim-commit.txt"
        victim.write_text("keep", encoding="utf-8")
        work = tmp_path / "work-copy2"
        work.mkdir()
        source_dir = tmp_path / "outside-src"
        source_dir.mkdir()
        (source_dir / "x.txt").write_text("pwned", encoding="utf-8")
        committed = wb.runner.isolation.commit_back(
            project,
            work,
            [{"action": "modify", "rel_path": "../victim-commit.txt"}],
            only_in_scope=True,
            scope=["**"],
            read_only=False,
        )
        assert committed == []
        assert victim.read_text(encoding="utf-8") == "keep"

    def test_read_only_task_always_runs_on_isolated_copy(self, wb, project):
        """只读任务绝不能直接在原工作区上跑：prepare 必须给独立工作副本。"""
        root = Path(project["root_path"])
        (root / "a.txt").write_text("A", encoding="utf-8")
        task = wb.tasks.create(project["id"], "只读", kind="review", agent_id="worker")
        row = wb.tasks.get(task["id"])
        assert row["read_only"] == 1
        workdir = Path(wb.runner.isolation.prepare(project, row))
        assert workdir.resolve() != root.resolve()
        assert workdir.is_dir()


# =========================================================================== 6 黑板与记忆

class TestBoardMemory:
    """需求 19-26：版本化、审核闸门、分层作用域、跨项目隔离、并发写。"""

    def test_version_history_is_complete_and_attributed(self, wb, project):
        entry = wb.board.post(project["id"], type="decision", title="v1", body="b1", scope="task", actor="worker")
        for i in range(2, 12):
            wb.board.edit(entry["id"], body=f"b{i}", author=f"agent-{i}", note=f"n{i}")
        versions = wb.board.versions(entry["id"])
        assert [v["version"] for v in versions] == list(range(1, 12))
        assert versions[0]["author"] == "worker"
        assert versions[-1]["author"] == "agent-11"
        assert versions[-1]["body"] == "b11"
        assert versions[-1]["at"]

    def test_review_gate_blocks_ephemeral_project_entries(self, wb, project):
        """长期记忆只收长记忆类型；note/todo/handoff 即使 scope=project 也不能直接进。"""
        wb.board.post(project["id"], type="note", title="随手记", scope="project")
        wb.board.post(project["id"], type="todo", title="待办", scope="project")
        wb.board.post(project["id"], type="handoff", title="交接", scope="project")
        assert wb.board.long_term_memory(project["id"]) == []

    def test_edited_entry_needs_rereview(self, wb, project):
        """已审核进长期记忆的条目被编辑后，新版本必须重新过闸门。"""
        entry = wb.board.post(project["id"], type="decision", title="决定A", body="原始", scope="project")
        wb.board.review(entry["id"], approved=True, reviewer="human")
        assert len(wb.board.long_term_memory(project["id"])) == 1
        wb.board.edit(entry["id"], body="被偷偷改过的内容", author="worker")
        memory = wb.board.long_term_memory(project["id"])
        assert memory == [], "编辑后的新版本不得未经复审就留在长期记忆"

    def test_rejected_entry_never_enters_memory(self, wb, project):
        entry = wb.board.post(project["id"], type="decision", title="决定B", body="x", scope="project")
        wb.board.review(entry["id"], approved=False, reviewer="human")
        assert wb.board.long_term_memory(project["id"]) == []

    def test_layering_and_cross_project_isolation(self, wb, project):
        other = wb.projects.create("other")
        wb.board.post(project["id"], type="decision", title="P项目", scope="project")
        wb.board.post(project["id"], type="note", title="P任务", scope="task")
        wb.board.post(other["id"], type="decision", title="O项目", scope="project")
        # 未审核的项目条目一条都不进长期记忆，且两项目互不可见
        assert wb.board.long_term_memory(project["id"]) == []
        assert wb.board.long_term_memory(other["id"]) == []
        p_pending = wb.board.pending_reviews(project["id"])
        o_pending = wb.board.pending_reviews(other["id"])
        assert [e["title"] for e in p_pending] == ["P项目"]
        assert [e["title"] for e in o_pending] == ["O项目"]
        wb.board.review(p_pending[0]["id"], approved=True, reviewer="human")
        assert [e["title"] for e in wb.board.long_term_memory(project["id"])] == ["P项目"]
        assert wb.board.long_term_memory(other["id"]) == []
        assert {e["title"] for e in wb.board.list(project["id"])} == {"P项目", "P任务"}
        assert [e["title"] for e in wb.board.search(project["id"], "O")] == []

    def test_concurrent_edits_do_not_lose_versions(self, wb, project):
        entry = wb.board.post(project["id"], type="note", title="并发", body="base", scope="task")
        workers = 12
        barrier = threading.Barrier(workers)
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=20)
                wb.board.edit(entry["id"], body=f"body-{i}", author=f"w{i}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors, errors
        versions = wb.board.versions(entry["id"])
        assert len(versions) == workers + 1
        assert len({v["version"] for v in versions}) == workers + 1
        assert wb.board.get(entry["id"])["current_version"] == workers + 1

    def test_concurrent_posts_keep_event_chain_valid(self, wb, project):
        workers = 12
        barrier = threading.Barrier(workers)
        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=20)
                wb.board.post(project["id"], type="note", title=f"c{i}", body="x")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert not errors, errors
        assert wb.board.verify_chain(project["id"]) is True
        assert len(wb.board.list(project["id"])) == workers

    def test_handoff_artifacts_default_cross_project_scope(self, wb, project):
        other = wb.projects.create("other2")
        wb.handoffs.create(
            project_id=project["id"], task_id="t1", run_id=None, agent_id="worker", data={"did": "本项目的活"}
        )
        assert [h["did"] for h in wb.handoffs.list_for_project(other["id"])] == []


# =========================================================================== 8 证据保留

class TestRunEvidenceRetention:
    """需求 43/46/50：调用证据（runs/）不能被常规收尾流程物理删除。"""

    def _make_task_dir(self, wb, project, task_id: str, run_ids: list[str]) -> Path:
        isolation = wb.runner.isolation
        task_dir = isolation.task_dir(project["id"], task_id)
        (task_dir / "work").mkdir(parents=True, exist_ok=True)
        (task_dir / "work" / "scratch.txt").write_text("wip", encoding="utf-8")
        (task_dir / "baseline").mkdir(parents=True, exist_ok=True)
        (task_dir / "baseline" / "orig.txt").write_text("orig", encoding="utf-8")
        for run_id in run_ids:
            run_dir = isolation.run_evidence_dir(project["id"], task_id, run_id)
            run_dir.mkdir(parents=True, exist_ok=True)
            for name in ("prompt.txt", "stdout.txt", "stderr.txt", "transcript.json"):
                (run_dir / name).write_text(f"{run_id}:{name}", encoding="utf-8")
        return task_dir

    def test_discard_keeps_run_evidence(self, wb, project):
        """discard() 只清 work/ 与 baseline/，runs/** 必须完整保留。"""
        task_id = "task-evidence"
        task_dir = self._make_task_dir(wb, project, task_id, ["run-a", "run-b"])
        assert (task_dir / "runs" / "run-a" / "prompt.txt").exists()

        wb.runner.isolation.discard(project["id"], task_id)

        for run_id in ("run-a", "run-b"):
            for name in ("prompt.txt", "stdout.txt", "stderr.txt", "transcript.json"):
                path = task_dir / "runs" / run_id / name
                assert path.exists(), f"调用证据被误删：{path}"
                assert path.read_text(encoding="utf-8") == f"{run_id}:{name}"
        assert not (task_dir / "work").exists()
        assert not (task_dir / "baseline").exists()

    def test_purge_task_removes_everything_explicitly(self, wb, project):
        """需要彻底删除痕迹时，必须显式调用语义明确的 purge_task()。"""
        task_id = "task-purge"
        task_dir = self._make_task_dir(wb, project, task_id, ["run-c"])
        wb.runner.isolation.purge_task(project["id"], task_id)
        assert not task_dir.exists()

    def test_engine_run_keeps_previous_run_evidence(self, wb, project):
        """端到端：同一任务跑两次（重试），第一次 run 的证据必须还在。

        用本地假 adapter 代替真实 CLI 适配器：本测试只关心「证据目录的保留语义」，
        不引入外部进程依赖（也避免被 agents/ 目录的并行改动牵连）。
        """
        from multiagent.agents.base import AgentResult

        root = Path(project["root_path"])
        (root / "a.txt").write_text("A", encoding="utf-8")

        class _StubAdapter:
            id = "worker"
            label = "Worker"
            protocol = "cli"

            def __init__(self):
                self.calls = 0

            def describe(self):
                return {"id": self.id}

            def check(self, timeout: float = 20.0):
                return {"ok": True}

            def run(self, request):
                self.calls += 1
                (Path(request.workspace) / "a.txt").write_text("B", encoding="utf-8")
                if self.calls == 1:  # 第一次失败 → 触发重试，形成两条 run 记录
                    return AgentResult(
                        ok=False, exit_code=1, duration_ms=1, stdout="", stderr="boom", error="第一次失败"
                    )
                return AgentResult(ok=True, exit_code=0, duration_ms=1, stdout="stub-out", stderr="stub-err")

        wb.runner._adapters["worker"] = _StubAdapter()
        task = wb.tasks.create(project["id"], "重试任务", kind="generic", agent_id="worker")
        first = wb.runner.run(task["id"])
        second = wb.runner.run(task["id"])
        isolation = wb.runner.isolation
        assert first.run_id and second.run_id and first.run_id != second.run_id
        for run_id, out_text in ((first.run_id, ""), (second.run_id, "stub-out")):
            run_dir = isolation.run_evidence_dir(project["id"], task["id"], run_id)
            assert (run_dir / "prompt.txt").exists(), f"{run_id} 的 prompt 证据丢失"
            assert (run_dir / "stdout.txt").read_text(encoding="utf-8") == out_text
            assert (run_dir / "transcript.json").exists()
        assert (isolation.run_evidence_dir(project["id"], task["id"], first.run_id) / "stderr.txt").read_text(
            encoding="utf-8"
        ) == "boom"
        runs = wb.db.query("SELECT id FROM runs WHERE task_id = ?", (task["id"],))
        assert len(runs) == 2


# =========================================================================== 7 审计

class TestAudit:
    """需求 46-50：并发 run 下成本/耗时汇总与时间线导出的准确性。"""

    def _insert_run(self, db, run_id: str, project_id: str, task_id: str, agent: str, ms: int, tokens: int, cost: float, at: str, status="success"):
        db.insert(
            "runs",
            {
                "id": run_id,
                "project_id": project_id,
                "task_id": task_id,
                "agent_id": agent,
                "agent_label": agent,
                "protocol": "cli",
                "status": status,
                "exit_code": 0,
                "started_at": at,
                "finished_at": at,
                "duration_ms": ms,
                "cost_tokens": tokens,
                "cost_amount": cost,
            },
        )

    def test_stats_without_project_id(self, wb, project):
        """不传 project_id 的汇总也要能用（原实现会因绑定参数错位报错）。"""
        self._insert_run(wb.db, "r1", project["id"], "t1", "worker", 100, 10, 0.1, "2026-01-01T00:00:01.000Z")
        stats = wb.audit.stats()
        assert stats["totals"]["runs"] == 1
        assert stats["totals"]["duration_ms"] == 100
        assert stats["totals"]["tokens"] == 10

    def test_stats_empty_project_id_behaves_like_none(self, wb, project):
        self._insert_run(wb.db, "r1", project["id"], "t1", "worker", 100, 10, 0.1, "2026-01-01T00:00:01.000Z")
        stats = wb.audit.stats("")
        assert stats["totals"]["runs"] == 1

    def test_stats_sums_are_exact_under_concurrency(self, wb, project):
        for i in range(20):
            self._insert_run(
                wb.db,
                f"r{i}",
                project["id"],
                "t1",
                "worker" if i % 2 else "coder",
                100 + i,
                10 + i,
                0.5,
                f"2026-01-01T00:00:{i:02d}.000Z",
            )
        stats = wb.audit.stats(project["id"])
        assert stats["totals"]["runs"] == 20
        assert stats["totals"]["duration_ms"] == sum(100 + i for i in range(20))
        assert stats["totals"]["tokens"] == sum(10 + i for i in range(20))
        assert abs(stats["totals"]["cost"] - 20 * 0.5) < 1e-9
        per_agent = {a["agent_id"]: a for a in stats["by_agent"]}
        assert per_agent["worker"]["runs"] == 10
        assert per_agent["coder"]["runs"] == 10
        assert per_agent["worker"]["duration_ms"] + per_agent["coder"]["duration_ms"] == stats["totals"]["duration_ms"]

    def test_timeline_merges_events_and_runs_in_order(self, wb, project, tmp_path: Path):
        self._insert_run(wb.db, "r1", project["id"], "t1", "worker", 100, 10, 0.1, "2026-01-01T00:00:05.000Z")
        wb.board.post(project["id"], type="note", title="事件1", body="x")
        items = wb.audit.timeline(project["id"])
        assert [i["at"] for i in items] == sorted(i["at"] for i in items)
        kinds = {i["type"] for i in items}
        assert kinds == {"event", "run"}

    def test_export_timeline_markdown_and_json(self, wb, project, tmp_path: Path):
        import json

        self._insert_run(wb.db, "r1", project["id"], "t1", "worker", 100, 10, 0.1, "2026-01-01T00:00:05.000Z")
        wb.board.post(project["id"], type="note", title="事件1", body="x")
        md = wb.audit.export_timeline(project["id"], tmp_path / "tl.md", fmt="markdown")
        text = Path(md).read_text(encoding="utf-8")
        assert "协作时间线" in text
        assert "事件1" in text
        js = wb.audit.export_timeline(project["id"], tmp_path / "tl.json", fmt="json")
        data = json.loads(Path(js).read_text(encoding="utf-8"))
        assert isinstance(data, list) and len(data) == len(wb.audit.timeline(project["id"]))
