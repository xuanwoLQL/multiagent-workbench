"""Inspect a project root and return a stack profile with runnable checks.

The detector is deliberately read-only.  It never spawns a process or imports
project code; it only stats and reads text files.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

DEFAULT_TIMEOUT_SEC = 600
MAX_SCAN_FILES = 20000
MAX_MARKER_BYTES = 1024 * 1024

IGNORED_DIRS = (
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "bower_components",
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".cache",
    ".next",
    ".nuxt",
    ".svelte-kit",
    ".angular",
    ".turbo",
    ".parcel-cache",
    ".gradle",
    ".m2",
    "dist",
    "build",
    "target",
    "out",
    "bin",
    "obj",
    "coverage",
    ".idea",
    ".vscode",
    "vendor",
    "Pods",
    "DerivedData",
)

MARKER_FILES = (
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "npm-shrinkwrap.json",
    "tsconfig.json",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "pytest.ini",
    "tox.ini",
    "conftest.py",
    "ruff.toml",
    ".ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    "pyrightconfig.json",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Makefile",
    "makefile",
    "GNUmakefile",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "gradlew",
    "gradlew.bat",
    "composer.json",
    "Gemfile",
    "Gemfile.lock",
    "Dockerfile",
)

_EXT_LANGUAGE = {
    ".py": "python",
    ".pyi": "python",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
    ".jsx": "node",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".vue": "vue",
    ".svelte": "svelte",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".fs": "fsharp",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".dart": "dart",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".ps1": "powershell",
}

_CHECK_ORDER = ("lint", "typecheck", "test", "build")

_NODE_SCRIPTS = {
    "lint": ("lint", "eslint", "lint:ci"),
    "typecheck": ("typecheck", "type-check", "check-types", "tsc"),
    "test": ("test", "test:unit", "test:ci", "vitest", "jest"),
    "build": ("build", "compile"),
}

_MAKE_TARGETS = {
    "lint": ("lint",),
    "typecheck": ("typecheck", "check", "types"),
    "test": ("test", "tests"),
    "build": ("build", "all"),
}

_INSTALL_RE = re.compile(
    r"(?i)(?:^|[\s;&|])(?:"
    r"(?:npm|pnpm|yarn|bun)\s+(?:install|i|add|ci)\b"
    r"|(?:pip|pip3|python(?:\d+(?:\.\d+)?)?\s+-m\s+pip)\s+install\b"
    r"|(?:poetry|uv|pipenv)\s+(?:install|sync|add)\b"
    r"|cargo\s+install\b"
    r"|go\s+install\b"
    r"|composer\s+(?:install|require)\b"
    r"|gem\s+install\b"
    r"|(?:apt|apt-get|yum|dnf|apk|brew|choco|winget)\s+install\b"
    r")"
)

_COMPILE_EXCLUDE_RE = (
    r"(^|[\\/])(\.git|node_modules|\.venv|venv|env|__pycache__|\.pytest_cache|"
    r"\.mypy_cache|\.ruff_cache|\.tox|\.nox|dist|build|target|out|bin|obj)([\\/]|$)"
)


def _read_text(path: Path, limit: int = MAX_MARKER_BYTES) -> str:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit)
    except OSError:
        return ""
    for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def find_python(root: str | Path) -> str | None:
    """Return a project-local Python interpreter if one exists (never executes it)."""
    base = Path(root)
    candidates = (
        base / ".venv" / "Scripts" / "python.exe",
        base / ".venv" / "Scripts" / "python3.exe",
        base / ".venv" / "bin" / "python",
        base / ".venv" / "bin" / "python3",
        base / "venv" / "Scripts" / "python.exe",
        base / "venv" / "bin" / "python",
        base / "env" / "Scripts" / "python.exe",
        base / "env" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _scan_languages(root: Path) -> tuple[dict[str, int], int]:
    counts: dict[str, int] = {}
    scanned = 0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if name not in IGNORED_DIRS and not (Path(current) / name).is_symlink()
        ]
        for name in files:
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                break
            language = _EXT_LANGUAGE.get(Path(name).suffix.lower())
            if language:
                counts[language] = counts.get(language, 0) + 1
        if scanned > MAX_SCAN_FILES:
            break
    return counts, scanned


def _load_package_json(root: Path) -> dict[str, Any]:
    path = root / "package.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(_read_text(path))
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _node_package_manager(root: Path, has_package_json: bool) -> str | None:
    for name, manager in (
        ("pnpm-lock.yaml", "pnpm"),
        ("yarn.lock", "yarn"),
        ("bun.lock", "bun"),
        ("bun.lockb", "bun"),
        ("package-lock.json", "npm"),
        ("npm-shrinkwrap.json", "npm"),
    ):
        if (root / name).is_file():
            return manager
    return "npm" if has_package_json else None


def _python_package_manager(root: Path, pyproject: str) -> str | None:
    if (root / "uv.lock").is_file():
        return "uv"
    if (root / "poetry.lock").is_file() or "[tool.poetry]" in pyproject:
        return "poetry"
    if (root / "Pipfile").is_file() or (root / "Pipfile.lock").is_file():
        return "pipenv"
    if (
        (root / "pyproject.toml").is_file()
        or (root / "requirements.txt").is_file()
        or (root / "requirements-dev.txt").is_file()
        or (root / "setup.py").is_file()
        or (root / "setup.cfg").is_file()
    ):
        return "pip"
    return None


def _has_python_tests(root: Path) -> bool:
    if (root / "conftest.py").is_file() or (root / "pytest.ini").is_file():
        return True
    for directory in ("tests", "test"):
        folder = root / directory
        if folder.is_dir():
            try:
                if next(folder.rglob("*.py"), None) is not None:
                    return True
            except OSError:
                pass
    for pattern in ("test_*.py", "*_test.py"):
        try:
            if next(root.glob(pattern), None) is not None:
                return True
        except OSError:
            pass
    return False


def _python_source_dirs(root: Path) -> list[str]:
    if (root / "src").is_dir():
        return ["src"]
    packages = [
        child.name
        for child in sorted(root.iterdir())
        if child.is_dir()
        and child.name not in IGNORED_DIRS
        and (child / "__init__.py").is_file()
    ]
    return packages or ["."]


def _make_targets(text: str) -> dict[str, list[str]]:
    targets: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*)\s*:(?!=)", line)
        if match:
            current = match.group(1)
            targets.setdefault(current, [])
            recipe = line[match.end() :].strip()
            if recipe.startswith(";"):
                targets[current].append(recipe[1:].strip())
            continue
        if current and (line.startswith("\t") or line.startswith("    ")):
            targets[current].append(line.strip())
        elif line and not line.startswith((" ", "\t")):
            current = None
    return targets


def _unsafe_recipe(lines: list[str]) -> bool:
    return any(_INSTALL_RE.search(line) for line in lines)


def _display(argv: list[str]) -> str:
    if os.name == "nt":
        import subprocess

        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def _add_command(
    commands: dict[str, dict[str, Any]],
    check: str,
    argv: list[str],
    label: str,
    source: str,
) -> None:
    if check in commands:
        return
    commands[check] = {
        "argv": [str(item) for item in argv],
        "label": label,
        "source": source,
        "display": _display([str(item) for item in argv]),
    }


def _node_commands(
    root: Path,
    package: dict[str, Any],
    manager: str,
    commands: dict[str, dict[str, Any]],
    blocked: list[dict[str, Any]],
) -> None:
    scripts = package.get("scripts")
    if not isinstance(scripts, dict):
        scripts = {}
    for check, aliases in _NODE_SCRIPTS.items():
        script_name = next((name for name in aliases if isinstance(scripts.get(name), str)), None)
        if not script_name:
            continue
        body = str(scripts[script_name])
        if _INSTALL_RE.search(body):
            blocked.append(
                {
                    "check": check,
                    "source": "package.json",
                    "script": script_name,
                    "reason": "脚本包含安装命令；验证过程绝不安装依赖",
                    "command": body[:500],
                }
            )
            continue
        if manager == "yarn":
            argv = ["yarn", "run", script_name]
        elif manager == "bun":
            argv = ["bun", "run", script_name]
        elif manager == "pnpm":
            argv = ["pnpm", "run", script_name]
        else:
            argv = ["npm", "run", "--silent", script_name]
        _add_command(commands, check, argv, f"{manager} run {script_name}", f"package.json:{script_name}")


def _python_commands(
    root: Path,
    python: str | None,
    pyproject: str,
    commands: dict[str, dict[str, Any]],
) -> None:
    interpreter = python or find_python(root) or sys.executable
    has_pytest = (
        "[tool.pytest" in pyproject
        or (root / "pytest.ini").is_file()
        or (root / "tox.ini").is_file()
        or _has_python_tests(root)
    )
    if has_pytest:
        _add_command(commands, "test", [interpreter, "-m", "pytest", "-q"], "pytest", "pytest")
    if (
        (root / "ruff.toml").is_file()
        or (root / ".ruff.toml").is_file()
        or "[tool.ruff" in pyproject
    ):
        _add_command(commands, "lint", [interpreter, "-m", "ruff", "check", "."], "ruff check", "ruff")
    if (
        (root / "mypy.ini").is_file()
        or (root / ".mypy.ini").is_file()
        or "[tool.mypy" in pyproject
        or "[mypy]" in _read_text(root / "setup.cfg")
    ):
        _add_command(commands, "typecheck", [interpreter, "-m", "mypy", "."], "mypy", "mypy")
    elif (root / "pyrightconfig.json").is_file() or "[tool.pyright" in pyproject:
        _add_command(commands, "typecheck", ["pyright"], "pyright", "pyright")
    _add_command(
        commands,
        "build",
        [
            interpreter,
            "-m",
            "compileall",
            "-q",
            "-x",
            _COMPILE_EXCLUDE_RE,
            *_python_source_dirs(root),
        ],
        "python -m compileall",
        "python",
    )


def _stack_marker_commands(
    root: Path,
    commands: dict[str, dict[str, Any]],
) -> None:
    if (root / "Cargo.toml").is_file():
        _add_command(commands, "lint", ["cargo", "clippy", "--all-targets", "--", "-D", "warnings"], "cargo clippy", "Cargo.toml")
        _add_command(commands, "typecheck", ["cargo", "check", "--quiet"], "cargo check", "Cargo.toml")
        _add_command(commands, "test", ["cargo", "test", "--quiet"], "cargo test", "Cargo.toml")
        _add_command(commands, "build", ["cargo", "build", "--quiet"], "cargo build", "Cargo.toml")
    if (root / "go.mod").is_file():
        _add_command(commands, "lint", ["go", "vet", "./..."], "go vet", "go.mod")
        _add_command(commands, "test", ["go", "test", "./..."], "go test", "go.mod")
        _add_command(commands, "build", ["go", "build", "./..."], "go build", "go.mod")
    if (root / "pom.xml").is_file():
        _add_command(commands, "test", ["mvn", "-q", "test"], "mvn test", "pom.xml")
        _add_command(commands, "build", ["mvn", "-q", "-DskipTests", "package"], "mvn package", "pom.xml")
    gradlew = root / ("gradlew.bat" if os.name == "nt" else "gradlew")
    if gradlew.is_file():
        _add_command(commands, "test", [str(gradlew), "test", "--no-daemon"], "gradlew test", "gradle")
        _add_command(commands, "build", [str(gradlew), "build", "--no-daemon"], "gradlew build", "gradle")


def _make_commands(
    root: Path,
    commands: dict[str, dict[str, Any]],
    blocked: list[dict[str, Any]],
) -> list[str]:
    path = next(
        (root / name for name in ("Makefile", "makefile", "GNUmakefile") if (root / name).is_file()),
        None,
    )
    if path is None:
        return []
    targets = _make_targets(_read_text(path))
    for check, aliases in _MAKE_TARGETS.items():
        if check in commands:
            continue
        target = next((name for name in aliases if name in targets), None)
        if not target:
            continue
        if _unsafe_recipe(targets[target]):
            blocked.append(
                {
                    "check": check,
                    "source": path.name,
                    "target": target,
                    "reason": "Makefile 目标包含安装命令；验证过程绝不安装依赖",
                    "command": "\n".join(targets[target])[:500],
                }
            )
            continue
        _add_command(commands, check, ["make", target], f"make {target}", f"{path.name}:{target}")
    return sorted(targets)


def _primary_language(counts: dict[str, int], languages: list[str]) -> str | None:
    if counts:
        priority = {
            "typescript": 10,
            "python": 9,
            "rust": 8,
            "go": 8,
            "node": 7,
            "java": 7,
            "kotlin": 6,
            "csharp": 6,
            "cpp": 5,
            "c": 5,
            "php": 4,
            "ruby": 4,
            "swift": 4,
            "dart": 4,
        }
        return max(counts, key=lambda name: (counts[name], priority.get(name, 0)))
    return languages[0] if languages else None


def detect_project(
    root: str | Path,
    *,
    python: str | None = None,
) -> dict[str, Any]:
    """Return a JSON-serialisable stack profile for ``root``.

    ``python`` lets a caller point the profile at the original project's
    virtualenv when the validation target is an isolated work copy.
    """
    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise NotADirectoryError(f"项目目录不存在：{base}")

    markers = [name for name in MARKER_FILES if (base / name).is_file()]
    package = _load_package_json(base)
    pyproject = _read_text(base / "pyproject.toml")
    counts, scanned = _scan_languages(base)

    languages = set(counts)
    if (base / "package.json").is_file():
        languages.add("typescript" if (base / "tsconfig.json").is_file() else "node")
    if (base / "pyproject.toml").is_file() or (base / "requirements.txt").is_file() or (base / "setup.py").is_file():
        languages.add("python")
    if (base / "Cargo.toml").is_file():
        languages.add("rust")
    if (base / "go.mod").is_file():
        languages.add("go")
    if (base / "pom.xml").is_file():
        languages.add("java")
    if (base / "build.gradle").is_file() or (base / "build.gradle.kts").is_file():
        languages.add("java")
    if (base / "composer.json").is_file():
        languages.add("php")
    if (base / "Gemfile").is_file():
        languages.add("ruby")

    managers: list[str] = []
    node_manager = _node_package_manager(base, (base / "package.json").is_file())
    python_manager = _python_package_manager(base, pyproject)
    for value in (
        node_manager,
        python_manager,
        "cargo" if (base / "Cargo.toml").is_file() else None,
        "go" if (base / "go.mod").is_file() else None,
        "make"
        if any((base / name).is_file() for name in ("Makefile", "makefile", "GNUmakefile"))
        else None,
        "maven" if (base / "pom.xml").is_file() else None,
        "gradle" if (base / "build.gradle").is_file() or (base / "build.gradle.kts").is_file() else None,
        "composer" if (base / "composer.json").is_file() else None,
        "bundler" if (base / "Gemfile").is_file() else None,
    ):
        if value and value not in managers:
            managers.append(value)

    commands: dict[str, dict[str, Any]] = {}
    blocked: list[dict[str, Any]] = []
    if node_manager:
        _node_commands(base, package, node_manager, commands, blocked)
    if python_manager or (base / "pyproject.toml").is_file() or counts.get("python"):
        _python_commands(base, python, pyproject, commands)
    _stack_marker_commands(base, commands)
    make_targets = _make_commands(base, commands, blocked)

    recommended = [check for check in _CHECK_ORDER if check in commands]
    sorted_languages = sorted(languages)
    return {
        "root": str(base),
        "primary_language": _primary_language(counts, sorted_languages),
        "languages": sorted_languages,
        "language_counts": dict(sorted(counts.items())),
        "package_managers": managers,
        "markers": markers,
        "package_scripts": package.get("scripts") if isinstance(package.get("scripts"), dict) else {},
        "make_targets": make_targets,
        "commands": commands,
        "recommended": recommended,
        "blocked": blocked,
        "default_timeout_sec": DEFAULT_TIMEOUT_SEC,
        "ignored_dirs": list(IGNORED_DIRS),
        "files_scanned": scanned,
    }
