"""P2-14：计划创建前扫描仓库，并把实际产物路径回填到验收条件 / outputs。

背景：Atlas 实跑时计划把验收路径写死为 ``AppShell.tsx``，实际产物在
``layout/AppShell.tsx``。计划创建阶段就应按 basename 找到唯一候选并回填，
避免任务因「预期路径写错」直接失败；多候选 / 无候选只告警，不阻断创建，
也不放宽验收（改写后的路径仍由 Agent 的真实产出决定是否通过）。
"""

from __future__ import annotations

from pathlib import Path

from multiagent.tasks.planner import apply_plan


def _project(wb, name: str) -> tuple[dict, Path]:  # noqa: ANN001
    project = wb.projects.create(name)
    return project, Path(project["root_path"])


def _apply(wb, project: dict, tasks: list[dict]) -> list[dict]:  # noqa: ANN001
    return apply_plan(wb.tasks, project["id"], {"tasks": tasks})


def _scan(task: dict) -> dict:
    return task["result"]["acceptance_path_scan"]


def test_unique_candidate_backfills_acceptance_path(wb):
    project, root = _project(wb, "scan-unique")
    (root / "layout").mkdir()
    (root / "layout" / "AppShell.tsx").write_text(
        "export default function AppShell() {}", encoding="utf-8"
    )

    created = _apply(
        wb,
        project,
        [
            {
                "key": "shell",
                "title": "实现 AppShell",
                "acceptance": [{"type": "file_exists", "path": "AppShell.tsx"}],
            }
        ],
    )

    task = wb.tasks.get(created[0]["id"])
    assert task["acceptance"][0]["path"] == "layout/AppShell.tsx"
    scan = _scan(task)
    assert {
        "from": "AppShell.tsx",
        "to": "layout/AppShell.tsx",
        "where": "acceptance",
    } in scan["autofixes"]
    assert scan["warnings"] == []


def test_multiple_candidates_keep_path_and_warn(wb):
    project, root = _project(wb, "scan-multiple")
    for sub in ("a", "b"):
        (root / sub).mkdir()
        (root / sub / "AppShell.tsx").write_text("x", encoding="utf-8")

    created = _apply(
        wb,
        project,
        [
            {
                "key": "shell",
                "title": "实现 AppShell",
                "acceptance": [{"type": "file_exists", "path": "AppShell.tsx"}],
            }
        ],
    )

    task = wb.tasks.get(created[0]["id"])
    assert task["acceptance"][0]["path"] == "AppShell.tsx"
    scan = _scan(task)
    assert scan["autofixes"] == []
    assert len(scan["warnings"]) == 1
    warning = scan["warnings"][0]
    assert warning["path"] == "AppShell.tsx"
    assert len(warning["candidates"]) >= 2
    assert len(warning["candidates"]) <= 5
    assert "AppShell.tsx" in warning["message"]


def test_existing_exact_path_is_left_alone(wb):
    project, root = _project(wb, "scan-exists")
    (root / "AppShell.tsx").write_text("root-level", encoding="utf-8")
    (root / "layout").mkdir()
    (root / "layout" / "AppShell.tsx").write_text("nested", encoding="utf-8")

    created = _apply(
        wb,
        project,
        [
            {
                "key": "shell",
                "title": "实现 AppShell",
                "acceptance": [{"type": "file_exists", "path": "AppShell.tsx"}],
            }
        ],
    )

    task = wb.tasks.get(created[0]["id"])
    assert task["acceptance"][0]["path"] == "AppShell.tsx"
    assert _scan(task) == {"autofixes": [], "warnings": []}


def test_glob_paths_are_not_rewritten(wb):
    project, root = _project(wb, "scan-glob")
    (root / "src").mkdir()
    (root / "src" / "AppShell.tsx").write_text("x", encoding="utf-8")

    created = _apply(
        wb,
        project,
        [
            {
                "key": "shell",
                "title": "实现 AppShell",
                "outputs": ["src/**/*.tsx"],
                "acceptance": [
                    {"type": "file_exists", "path": "**/*.tsx"},
                    {"type": "file_nonempty", "path": "src/*.tsx", "min_bytes": 1},
                ],
            }
        ],
    )

    task = wb.tasks.get(created[0]["id"])
    assert task["outputs"] == ["src/**/*.tsx"]
    assert [item["path"] for item in task["acceptance"]] == [
        "**/*.tsx",
        "src/*.tsx",
    ]
    assert _scan(task) == {"autofixes": [], "warnings": []}


def test_output_backfill_rewrites_generated_acceptance(wb):
    project, root = _project(wb, "scan-output")
    (root / "pages").mkdir()
    (root / "pages" / "TicketBoard.tsx").write_text("board", encoding="utf-8")

    created = _apply(
        wb,
        project,
        [
            {
                "key": "board",
                "title": "实现 TicketBoard",
                "kind": "web",
                "outputs": ["TicketBoard.tsx"],
            }
        ],
    )

    task = wb.tasks.get(created[0]["id"])
    assert task["outputs"] == ["pages/TicketBoard.tsx"]
    assert task["write_scope"] == ["pages/**"]
    acceptance_paths = [
        item["path"] for item in task["acceptance"] if "path" in item
    ]
    assert acceptance_paths
    assert set(acceptance_paths) == {"pages/TicketBoard.tsx"}

    scan = _scan(task)
    assert {
        "from": "TicketBoard.tsx",
        "to": "pages/TicketBoard.tsx",
        "where": "outputs",
    } in scan["autofixes"]
    assert {
        "from": "TicketBoard.tsx",
        "to": "pages/TicketBoard.tsx",
        "where": "acceptance",
    } in scan["autofixes"]
    assert scan["warnings"] == []


def test_candidate_scan_never_escapes_project_root(wb, tmp_path: Path):
    project, root = _project(wb, "scan-escape")
    outside = tmp_path / "outside-scan"
    outside.mkdir()
    (outside / "AppShell.tsx").write_text("secret", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "AppShell.tsx").write_text("inside", encoding="utf-8")

    escape_rel = "../outside-scan/AppShell.tsx"
    escape_win = "..\\outside-scan\\AppShell.tsx"
    absolute = str((outside / "AppShell.tsx").resolve())

    created = _apply(
        wb,
        project,
        [
            {
                "key": "escape",
                "title": "越界路径",
                "outputs": [escape_rel, absolute],
                "acceptance": [
                    {"type": "file_exists", "path": escape_rel},
                    {"type": "file_exists", "path": escape_win},
                    {"type": "file_exists", "path": absolute},
                ],
            }
        ],
    )

    task = wb.tasks.get(created[0]["id"])
    assert task["outputs"] == [escape_rel, absolute]
    assert [item["path"] for item in task["acceptance"]] == [
        escape_rel,
        escape_win,
        absolute,
    ]

    scan = _scan(task)
    assert scan["autofixes"] == []
    assert len(scan["warnings"]) >= 2
    for warning in scan["warnings"]:
        assert warning["candidates"] == []
    # 保险：任何回填目标都必须是 root 内的相对路径。
    for fix in scan["autofixes"]:
        assert not fix["to"].startswith(("/", ".."))
        assert ":" not in fix["to"]
