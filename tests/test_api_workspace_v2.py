"""v2 工作台新增接口：项目文件浏览、Git 状态、任务 worktree。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _project(wb, root: Path):
    return wb.projects.create("files-demo", root_path=str(root))


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_list_read_and_search_files(wb, tmp_path: Path):
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("def hello():\n    return 'atlas needle'\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "noise.js").write_text("ignored", encoding="utf-8")
    project = _project(wb, root)
    client = _client(wb)

    listing = client.get(f"/api/projects/{project['id']}/files").json()
    names = {entry["name"] for entry in listing["entries"]}
    assert names == {"src", "README.md"}
    assert all(not entry["path"].startswith("node_modules") for entry in listing["entries"])

    sub = client.get(f"/api/projects/{project['id']}/files", params={"path": "src"}).json()
    assert [entry["name"] for entry in sub["entries"]] == ["main.py"]

    content = client.get(f"/api/projects/{project['id']}/file", params={"path": "src/main.py"}).json()
    assert content["binary"] is False
    assert "atlas needle" in content["content"]

    found = client.get(f"/api/projects/{project['id']}/search", params={"q": "needle"}).json()
    assert found["matches"][0]["path"] == "src/main.py"
    assert found["matches"][0]["line"] == 2


def test_file_endpoints_reject_traversal(wb, tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "ok.txt").write_text("ok", encoding="utf-8")
    project = _project(wb, root)
    client = _client(wb)
    for bad in ("../secret.txt", "..\\secret.txt", "src/../../etc/passwd"):
        res = client.get(f"/api/projects/{project['id']}/file", params={"path": bad})
        assert res.status_code in (400, 404)


def test_git_status_and_worktree_info(wb, tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "a.txt").write_text("one", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init")

    project = _project(wb, root)
    client = _client(wb)
    status = client.get(f"/api/projects/{project['id']}/git").json()
    assert status["is_repo"] is True
    assert status["branch"] in {"master", "main"}
    assert status["dirty"] is False

    task = client.post(
        f"/api/projects/{project['id']}/tasks",
        json={"title": "worktree probe", "kind": "code", "isolation": "worktree"},
    ).json()
    info = client.get(f"/api/tasks/{task['id']}/worktree").json()
    assert info["task_id"] == task["id"]
    assert info["mode"] in {"worktree", "copy"}


def test_non_git_project_reports_not_repo(wb, tmp_path: Path):
    root = tmp_path / "plain"
    root.mkdir()
    project = _project(wb, root)
    client = _client(wb)
    status = client.get(f"/api/projects/{project['id']}/git").json()
    assert status == {"is_repo": False}
