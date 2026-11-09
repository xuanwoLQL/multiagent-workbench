"""Read-only stack detection and bounded runtime validation."""

from .detect import DEFAULT_TIMEOUT_SEC, IGNORED_DIRS, detect_project, find_python
from .runner import MAX_OUTPUT_BYTES, MAX_TIMEOUT_SEC, run_checks

__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "IGNORED_DIRS",
    "MAX_OUTPUT_BYTES",
    "MAX_TIMEOUT_SEC",
    "detect_project",
    "find_python",
    "run_checks",
]
