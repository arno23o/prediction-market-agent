#!/usr/bin/env python3
"""Test double for the Claude Code CLI stream contract (see tests/helpers).

Invoked exactly like ``claude -p`` (as argv[0]); it ignores every flag except that it
reads ``--session-id`` and ``--model`` from argv to echo them on the init line. The
behavior is selected by the ``STUB_BEHAVIOR`` env var:

  ok          init (hermetic-looking: no memory_paths/mcp_servers) + success result.
  error       init + result with is_error=true, exit code 1.
  auth_error  init + the Aug-5..08 OAuth wedge's exact shape: a synthetic assistant record
              carrying the error CODE, then a result record that still says
              subtype=success while is_error is true, terminal_reason is api_error, and
              total_cost_usd is 0. Exit code 1. The infra-vs-contract classifier (docs/14
              D2) reads those fields and never the prose.
  hang        init (+ optional ticket), then sleep 60s — for wall-clock timeout tests.
  bad_init    init carrying a non-empty ``memory_paths`` (hermeticity violation) + result.
  structured  init + success result carrying ``structured_output`` from STUB_STRUCTURED_JSON.
  ignore_term init, then IGNORE SIGTERM and sleep 30s — the kill-escalation case (CI-7):
              only SIGKILL ends it, which is what a wedged headless session looks like.

``STUB_PID_FILE``, when set, receives this process's pid as soon as it starts — the seam
a test uses to watch an orphan for real instead of trusting the harness's word for it.

If ``STUB_WRITE_TICKET`` names a directory, its files are copied into ``../ticket/``
(relative to cwd) right after the init line, *before* the behavior tail — so ``hang`` can
prove the contract rule (ticket finished, session later killed). Only stdlib is used, so
any ``python3`` on PATH runs it.
"""

import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path


def _argv_value(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def _emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    behavior = os.environ.get("STUB_BEHAVIOR", "ok")
    session_id = _argv_value("--session-id", "stub-session")
    model = _argv_value("--model", "stub-model")
    cwd = os.getcwd()

    pid_file = os.environ.get("STUB_PID_FILE")
    if pid_file:
        Path(pid_file).write_text(str(os.getpid()))

    init = {
        "type": "system", "subtype": "init", "session_id": session_id,
        "cwd": cwd, "model": model, "tools": ["Read", "Bash"],
        "permissionMode": "bypassPermissions",
    }
    if behavior == "bad_init":
        # Mimic a non-hermetic real Claude init: a user CLAUDE.md leaked in.
        init["memory_paths"] = [str(Path.home() / ".claude" / "CLAUDE.md")]
        init["mcp_servers"] = []
    _emit(init)

    ticket_src = os.environ.get("STUB_WRITE_TICKET")
    if ticket_src:
        dest = Path(cwd).parent / "ticket"
        dest.mkdir(parents=True, exist_ok=True)
        for p in Path(ticket_src).iterdir():
            if p.is_file():
                shutil.copy(p, dest / p.name)

    if behavior == "auth_error":
        prose = "Failed to authenticate: OAuth session expired and could not be refreshed"
        _emit({
            "type": "assistant", "session_id": session_id, "error": "authentication_failed",
            "message": {"model": "<synthetic>", "role": "assistant", "type": "message",
                        "content": [{"type": "text", "text": prose}],
                        "usage": {"input_tokens": 0, "output_tokens": 0}},
        })
        _emit({
            "type": "result", "subtype": "success", "is_error": True,
            "api_error_status": None, "duration_ms": 54, "duration_api_ms": 0,
            "num_turns": 1, "result": prose, "session_id": session_id,
            "total_cost_usd": 0, "usage": {"input_tokens": 0, "output_tokens": 0},
            "terminal_reason": "api_error",
        })
        sys.exit(1)

    if behavior == "hang":
        time.sleep(60)  # killed by the runner's wall clock before this returns

    if behavior == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        time.sleep(30)  # only SIGKILL ends this; the test asserts the escalation happens

    result = {
        "type": "result", "subtype": "success",
        "is_error": behavior == "error",
        "result": f"stub {behavior} done",
        "session_id": session_id,
        "total_cost_usd": 0.0123,
        "num_turns": 3,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    if behavior == "structured":
        result["structured_output"] = json.loads(
            os.environ.get("STUB_STRUCTURED_JSON", "null")
        )
    _emit(result)

    sys.exit(1 if behavior == "error" else 0)


if __name__ == "__main__":
    main()
