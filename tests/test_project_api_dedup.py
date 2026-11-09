"""P1-11 API 层：同一目录重复导入必须 409，并返回已存在项目 ID。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def test_import_same_directory_twice_returns_409_with_existing_id(wb, tmp_path: Path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    client = _client(wb)

    first = client.post("/api/projects/import", json={"path": str(root), "name": "first"})
    assert first.status_code == 200, first.text
    first_id = first.json()["id"]

    # 换一个名字导入同一目录：必须拒绝，并指出已有项目。
    second = client.post("/api/projects/import", json={"path": str(root), "name": "second"})
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["error"] == "project_root_exists"
    assert body["existing_project_id"] == first_id
    assert body["existing_project"]["id"] == first_id
    assert "first" in body["message"]

    # 尾斜杠 / 反斜杠 / 大小写（Windows）视为同一目录。
    variant = str(root) + "\\"
    third = client.post("/api/projects/import", json={"path": variant, "name": "third"})
    assert third.status_code == 409, third.text

    # 项目数量没有变化。
    projects = client.get("/api/projects").json()
    assert len(projects) == 1
    assert projects[0]["id"] == first_id


def test_create_project_with_duplicate_root_returns_409(wb, tmp_path: Path):
    root = tmp_path / "managed"
    client = _client(wb)
    first = client.post("/api/projects", json={"name": "managed-one", "root_path": str(root)})
    assert first.status_code == 200, first.text
    second = client.post("/api/projects", json={"name": "managed-two", "root_path": str(root)})
    assert second.status_code == 409, second.text
    assert second.json()["existing_project_id"] == first.json()["id"]


def test_different_directories_still_allowed(wb, tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    client = _client(wb)
    assert client.post("/api/projects/import", json={"path": str(a), "name": "a"}).status_code == 200
    assert client.post("/api/projects/import", json={"path": str(b), "name": "b"}).status_code == 200
    assert len(client.get("/api/projects").json()) == 2
