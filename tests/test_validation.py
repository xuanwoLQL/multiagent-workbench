"""Runtime validation: detection, bounded runner and API persistence."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from multiagent.api.app import create_api
from multiagent.validation.detect import detect_project
from multiagent.validation.runner import MAX_OUTPUT_BYTES, run_checks


def _client(wb) -> TestClient:
    return TestClient(create_api(wb))


def _spec(argv: list[str], label: str = "check") -> dict:
    return {"argv": argv, "label": label, "source": "test"}


def test_detect_python_project(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\n\n"
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n\n"
        "[tool.ruff]\nline-length = 100\n",
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    package = tmp_path / "src" / "demo"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    profile = detect_project(tmp_path, python=sys.executable)

    assert "pyproject.toml" in profile["markers"]
    assert "python" in profile["languages"]
    assert "pip" in profile["package_managers"]
    assert profile["commands"]["test"]["argv"] == [sys.executable, "-m", "pytest", "-q"]
    assert "lint" in profile["commands"]
    assert "build" in profile["commands"]
    assert "compileall" in profile["commands"]["build"]["argv"]
    assert profile["recommended"]
    assert profile["default_timeout_sec"] >= 1
    assert "node_modules" in profile["ignored_dirs"]


def test_detect_node_project_and_blocks_install_scripts(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "node test.js",
                    "lint": "eslint .",
                    "build": "tsc -p .",
                    "typecheck": "tsc --noEmit",
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "index.js").write_text("", encoding="utf-8")
    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")

    profile = detect_project(tmp_path)

    assert "node" in profile["languages"] or "typescript" in profile["languages"]
    assert "npm" in profile["package_managers"]
    assert {"test", "lint", "build", "typecheck"} <= set(profile["commands"])
    assert profile["commands"]["test"]["argv"][:3] == ["npm", "run", "--silent"]

    blocked_dir = tmp_path / "blocked"
    blocked_dir.mkdir()
    (blocked_dir / "package.json").write_text(
        json.dumps({"scripts": {"test": "npm install && node test.js"}}),
        encoding="utf-8",
    )
    blocked_profile = detect_project(blocked_dir)
    assert "test" not in blocked_profile["commands"]
    assert any(item["check"] == "test" for item in blocked_profile["blocked"])


def test_detect_never_executes(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": 'node -e "require(\'fs\').writeFileSync(\'ran.txt\', \'x\')"'}}),
        encoding="utf-8",
    )
    detect_project(tmp_path)
    assert not (tmp_path / "ran.txt").exists()


def test_detect_makefile_and_rust(tmp_path: Path):
    (tmp_path / "Makefile").write_text(
        "test:\n\tpython -m pytest -q\n\nbuild:\n\tpython -m compileall -q .\n",
        encoding="utf-8",
    )
    profile = detect_project(tmp_path)
    assert "make" in profile["package_managers"]
    assert profile["commands"]["test"]["argv"] == ["make", "test"]
    assert profile["commands"]["build"]["argv"] == ["make", "build"]

    rust_dir = tmp_path / "rust"
    rust_dir.mkdir()
    (rust_dir / "Cargo.toml").write_text("[package]\nname = 'demo'\n", encoding="utf-8")
    rust_profile = detect_project(rust_dir)
    assert "rust" in rust_profile["languages"]
    assert "cargo" in rust_profile["package_managers"]
    assert {"lint", "typecheck", "test", "build"} <= set(rust_profile["commands"])


def test_runner_pass_fail_and_output_cap(tmp_path: Path):
    commands = {
        "pass": _spec([sys.executable, "-c", "print('ok')"]),
        "fail": _spec([sys.executable, "-c", "import sys; sys.exit(3)"]),
        "big": _spec([sys.executable, "-c", "import sys; sys.stdout.write('x' * 300000)"]),
    }
    results = run_checks(
        tmp_path,
        {"commands": commands, "recommended": ["pass", "fail", "big"]},
        timeout_sec=30,
    )
    by_id = {item["id"]: item for item in results}
    assert by_id["pass"]["ok"] is True
    assert by_id["pass"]["exit_code"] == 0
    assert by_id["fail"]["ok"] is False
    assert by_id["fail"]["exit_code"] == 3
    assert by_id["big"]["truncated"] is True
    assert len(by_id["big"]["output"]) <= MAX_OUTPUT_BYTES


def test_runner_timeout(tmp_path: Path):
    commands = {"slow": _spec([sys.executable, "-c", "import time; time.sleep(10)"])}
    started = time.monotonic()
    results = run_checks(
        tmp_path,
        {"commands": commands, "recommended": ["slow"]},
        timeout_sec=1,
    )
    elapsed = time.monotonic() - started
    assert results[0]["timeout"] is True
    assert results[0]["ok"] is False
    assert elapsed < 8


def test_runner_cancellation(tmp_path: Path):
    commands = {"slow": _spec([sys.executable, "-c", "import time; time.sleep(10)"])}
    cancel = threading.Event()
    timer = threading.Timer(0.5, cancel.set)
    timer.start()
    started = time.monotonic()
    try:
        results = run_checks(
            tmp_path,
            {"commands": commands, "recommended": ["slow"]},
            timeout_sec=30,
            cancel=cancel,
        )
    finally:
        timer.cancel()
    assert results[0]["cancelled"] is True
    assert results[0]["ok"] is False
    assert time.monotonic() - started < 8


def test_runner_does_not_interpret_shell_metacharacters(tmp_path: Path):
    marker = tmp_path / "pwned.txt"
    payload = f"hello & echo pwned > {marker}"
    commands = {
        "echo": _spec(
            [sys.executable, "-c", "import sys; print(sys.argv[1])", payload]
        )
    }
    results = run_checks(
        tmp_path,
        {"commands": commands, "recommended": ["echo"]},
        timeout_sec=30,
    )
    assert results[0]["ok"] is True
    assert payload in results[0]["output"]
    assert not marker.exists()

    injected = run_checks(
        tmp_path,
        {"commands": commands, "recommended": ["echo"]},
        checks=["echo; echo pwned"],
        timeout_sec=30,
    )
    assert injected[0]["skipped"] is True
    assert not marker.exists()


def test_runner_missing_tool_is_skipped(tmp_path: Path):
    commands = {"missing": _spec(["definitely-not-a-real-tool-xyz"])}
    results = run_checks(
        tmp_path,
        {"commands": commands, "recommended": ["missing"]},
        timeout_sec=30,
    )
    assert results[0]["skipped"] is True
    assert results[0]["ok"] is False
    assert "找不到可执行文件" in results[0]["output"]


def test_runner_build_does_not_write_pycache(tmp_path: Path):
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    profile = detect_project(tmp_path, python=sys.executable)
    assert "build" in profile["commands"]

    result = run_checks(tmp_path, profile, checks=["build"], timeout_sec=30)[0]

    assert result["ok"] is True
    assert not list(tmp_path.rglob("__pycache__"))
    assert not list(tmp_path.rglob("*.pyc"))


def test_validation_api_persists_and_reads_latest(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "val-api"}).json()["id"]
    project = wb.projects.get(pid)
    root = Path(project["root_path"])
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")

    task = client.post(
        f"/api/projects/{pid}/tasks",
        json={
            "title": "validate this task",
            "kind": "manual",
            "isolation": "shared",
            "allow_shared_write": True,
            "acceptance": [{"type": "manual_confirm"}],
        },
    ).json()

    inspect = client.get(f"/api/projects/{pid}/inspect")
    assert inspect.status_code == 200
    profile = inspect.json()
    assert "languages" in profile
    assert "commands" in profile
    assert "recommended" in profile

    assert client.get(f"/api/tasks/{task['id']}/validation").status_code == 404

    response = client.post(
        f"/api/tasks/{task['id']}/validate",
        json={"checks": ["test"], "timeout_sec": 60},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["ok"] is True, report
    assert report["checks"][0]["ok"] is True
    assert report["summary"]["passed"] == 1

    out_dir = Path(wb.config.runtime_dir) / pid / "tasks" / task["id"] / "validation"
    stored = list(out_dir.glob("*.json"))
    assert len(stored) == 1
    saved = json.loads(stored[0].read_text(encoding="utf-8"))
    assert saved["report_id"] == report["report_id"]

    latest = client.get(f"/api/tasks/{task['id']}/validation")
    assert latest.status_code == 200
    assert latest.json()["report_id"] == report["report_id"]
    assert not (root / ".pytest_cache").exists()
    assert not list(root.rglob("__pycache__"))


def test_validation_copy_task_uses_workdir(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "val-copy"}).json()["id"]
    project = wb.projects.get(pid)
    root = Path(project["root_path"])
    (root / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    task = client.post(
        f"/api/projects/{pid}/tasks",
        json={
            "title": "copy validation",
            "kind": "manual",
            "isolation": "copy",
            "acceptance": [{"type": "manual_confirm"}],
        },
    ).json()

    missing = client.post(f"/api/tasks/{task['id']}/validate", json={"checks": ["test"]})
    assert missing.status_code == 409

    wb.isolation.prepare(project, task)
    response = client.post(
        f"/api/tasks/{task['id']}/validate",
        json={"checks": ["test"], "timeout_sec": 60},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["ok"] is True, report
    assert report["mode"] == "copy"
    assert Path(report["root"]) != Path(report["project_root"])


def test_validation_api_rejects_bad_timeout(wb):
    client = _client(wb)
    pid = client.post("/api/projects", json={"name": "val-bad"}).json()["id"]
    task = client.post(
        f"/api/projects/{pid}/tasks",
        json={
            "title": "bad timeout",
            "kind": "manual",
            "isolation": "shared",
            "allow_shared_write": True,
            "acceptance": [{"type": "manual_confirm"}],
        },
    ).json()
    response = client.post(
        f"/api/tasks/{task['id']}/validate",
        json={"timeout_sec": 0},
    )
    assert response.status_code == 400
