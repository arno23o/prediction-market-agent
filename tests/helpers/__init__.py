"""Test helpers for the session runner and attempt lifecycle."""

from __future__ import annotations

import os
from pathlib import Path

STUB_RUNNER = Path(__file__).parent / "stub_runner.py"


def stub_runner_cmd() -> str:
    """Path to the stub CLI, made executable so it can be argv[0] via its shebang."""
    os.chmod(STUB_RUNNER, 0o755)
    return str(STUB_RUNNER)
