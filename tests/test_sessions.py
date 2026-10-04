"""Session runner: command composition + subprocess contract (spec §9.4)."""

import json
import os
import signal
import time
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from betting_agent import sessions
from betting_agent.sessions import (
    SessionResult,
    SessionSpec,
    _cwd_slug,
    _parse_stream,
    build_command,
    check_claude_auth,
    infra_error_kind,
    run_session,
)
from helpers import stub_runner_cmd

# ------------------------------------------------------------------ build_command


def _full_spec(**over):
    kw = dict(
        kind="grader", cwd=Path("/ws"), prompt="do it", model="claude-sonnet-5",
        effort="high", max_turns=40, wall_time_s=120, session_id="uuid-1",
        add_dirs=[Path("/a"), Path("/b")], max_budget_usd=Decimal("15.00"),
        tools="Read,Grep,Glob", json_schema={"type": "object"},
    )
    kw.update(over)
    return SessionSpec(**kw)


def test_build_command_full_composition():
    cmd = build_command(_full_spec(), "claude")
    assert cmd == [
        "claude", "-p", "do it",
        "--safe-mode",
        "--permission-mode", "bypassPermissions",
        "--model", "claude-sonnet-5",
        "--effort", "high",
        "--max-turns", "40",
        "--max-budget-usd", "15.00",
        "--session-id", "uuid-1",
        "--add-dir", "/a",
        "--add-dir", "/b",
        "--output-format", "stream-json", "--verbose",
        "--tools", "Read,Grep,Glob",
        "--json-schema", json.dumps({"type": "object"}),
    ]


def test_build_command_omits_optional_flags():
    cmd = build_command(
        _full_spec(max_budget_usd=None, tools=None, json_schema=None, add_dirs=[]),
        "claude",
    )
    assert "--max-budget-usd" not in cmd
    assert "--tools" not in cmd
    assert "--json-schema" not in cmd
    assert "--add-dir" not in cmd
    # The mandatory flags remain and stream-json is always present.
    assert cmd[:3] == ["claude", "-p", "do it"]
    assert "--safe-mode" in cmd
    assert cmd[cmd.index("--session-id") + 1] == "uuid-1"
    assert cmd[-3:] == ["--output-format", "stream-json", "--verbose"]


def test_build_command_repeats_add_dir_and_uses_runner_cmd():
    spec = _full_spec(add_dirs=[Path("/x"), Path("/y"), Path("/z")])
    cmd = build_command(spec, "/opt/runner")
    assert cmd[0] == "/opt/runner"
    assert cmd.count("--add-dir") == 3
    dirs = [cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-dir"]
    assert dirs == ["/x", "/y", "/z"]


def test_build_command_emits_disallowed_tools_when_set():
    cmd = build_command(_full_spec(disallowed_tools="Skill"), "claude")
    assert "--disallowedTools" in cmd
    assert cmd[cmd.index("--disallowedTools") + 1] == "Skill"


def test_build_command_omits_disallowed_tools_when_none():
    # default (unset) -> the flag is absent; nothing else about the argv changes.
    assert "--disallowedTools" not in build_command(_full_spec(), "claude")
    assert "--disallowedTools" not in build_command(
        _full_spec(disallowed_tools=None), "claude"
    )


def test_build_command_strips_meta_schema_key():
    # The claude CLI rejects schemas carrying a "$schema" meta-reference
    # (live grader failure 2026-07-13); build_command must drop the key.
    import json as _json

    spec = _full_spec(
        json_schema={"$schema": "https://json-schema.org/draft/2020-12/schema",
                     "type": "object", "required": ["x"]}
    )
    cmd = build_command(spec, "claude")
    emitted = _json.loads(cmd[cmd.index("--json-schema") + 1])
    assert "$schema" not in emitted
    assert emitted["type"] == "object" and emitted["required"] == ["x"]


# ------------------------------------------------------------------ run_session


def _run(tmp_path, cwd, *, behavior, wall_time_s=60, extra_env=None, model="stub-model"):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = {"STUB_BEHAVIOR": behavior}
    if extra_env:
        env.update(extra_env)
    spec = SessionSpec(
        kind="attempt", cwd=cwd, prompt="go", model=model, effort="high",
        max_turns=10, wall_time_s=wall_time_s, session_id="11111111-1111-1111-1111-111111111111",
        extra_env=env,
    )
    return run_session(
        spec, stub_runner_cmd(), logs / "session.stream.jsonl", logs / "session.err"
    )


def test_run_session_ok_path(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    res = _run(tmp_path, cwd, behavior="ok")
    assert res.exit_kind == "ok"
    assert res.is_error is False
    assert res.result_text == "stub ok done"
    assert res.cost_usd == Decimal("0.0123")
    assert res.input_tokens == 100
    assert res.output_tokens == 50
    assert res.num_turns == 3
    assert res.session_id == "11111111-1111-1111-1111-111111111111"
    assert res.init_manifest is not None and res.init_manifest["subtype"] == "init"


def test_run_session_structured_roundtrips(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    payload = {"verdict": "confirmed", "score": 7, "tags": ["a", "b"]}
    res = _run(
        tmp_path, cwd, behavior="structured",
        extra_env={"STUB_STRUCTURED_JSON": json.dumps(payload)},
    )
    assert res.exit_kind == "ok"
    assert res.structured_output == payload


def test_run_session_error_path(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    res = _run(tmp_path, cwd, behavior="error")
    assert res.exit_kind == "error"
    assert res.is_error is True


def test_run_session_timeout_kills_process(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    res = _run(tmp_path, cwd, behavior="hang", wall_time_s=2)
    assert res.exit_kind == "timeout"
    # Proves the 60 s sleep was interrupted (SIGTERM landed within the grace window),
    # i.e. the process is actually dead rather than still sleeping.
    assert res.wall_seconds < 30


def test_run_session_env_not_hermetic(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    res = _run(tmp_path, cwd, behavior="bad_init")
    assert res.exit_kind == "env_not_hermetic"
    assert res.init_manifest["memory_paths"]  # non-empty -> the violation


def test_run_session_does_not_duplicate_claudes_transcript(tmp_path):
    """EF-6/D4: the stream we write IS the record — Claude's own transcript is left where
    it is rather than copied into the attempt's logs. Replaces the test that pinned the
    copy; the source file still exists afterwards, so nothing was deleted, only not
    duplicated."""
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    config_dir = tmp_path / "claude-config"
    sid = "11111111-1111-1111-1111-111111111111"
    transcript = config_dir / "projects" / _cwd_slug(cwd) / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text('{"type":"user"}\n')

    _run(tmp_path, cwd, behavior="ok", extra_env={"CLAUDE_CONFIG_DIR": str(config_dir)})

    assert not (tmp_path / "logs" / "transcript.jsonl").exists()
    assert transcript.read_text() == '{"type":"user"}\n'  # untouched, not deleted
    assert (tmp_path / "logs" / "session.stream.jsonl").exists()  # the record itself


def test_run_session_with_no_transcript_on_disk_is_unaffected(tmp_path):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    res = _run(tmp_path, cwd, behavior="ok")
    assert res.exit_kind == "ok"
    assert not (tmp_path / "logs" / "transcript.jsonl").exists()


# ------------------------------------------------------------------ ST-6 child lifecycle
def _await_pid(pid_file: Path, timeout: float = 5.0) -> int:
    """Block until the stub child publishes its pid (it does so first thing)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(pid_file.read_text())
        except (OSError, ValueError):
            time.sleep(0.02)
    raise AssertionError(f"stub child never wrote {pid_file}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.02)
    return False


def test_an_interrupted_run_kills_the_child_it_would_have_orphaned(tmp_path, monkeypatch):
    """ST-6/CI-7: the child lives in its OWN process group, so a Ctrl-C or SIGTERM aimed
    at the harness never reached it — it kept running headless, parentless, unbounded by
    any wall clock, spending API budget for up to three hours. And it ignores SIGTERM
    here, so the escalation to SIGKILL is what has to do the work."""
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    pid_file = tmp_path / "child.pid"
    monkeypatch.setattr("betting_agent.sessions._ABORT_GRACE_S", 0.3)

    def interrupt_mid_wait(proc, wall_time_s):
        _await_pid(pid_file)
        raise KeyboardInterrupt("operator hit Ctrl-C")

    monkeypatch.setattr("betting_agent.sessions._wait_with_timeout", interrupt_mid_wait)

    with pytest.raises(KeyboardInterrupt):
        _run(tmp_path, cwd, behavior="ignore_term", wall_time_s=60,
             extra_env={"STUB_PID_FILE": str(pid_file)})

    pid = int(pid_file.read_text())
    assert _wait_gone(pid), "the interrupted harness left a paid session running headless"


def test_a_normal_run_leaves_the_finished_child_alone(tmp_path):
    """The terminate-on-exit guard must be a no-op on every ordinary session."""
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    pid_file = tmp_path / "child.pid"
    res = _run(tmp_path, cwd, behavior="ok", extra_env={"STUB_PID_FILE": str(pid_file)})
    assert res.exit_kind == "ok"                       # not "killed"
    assert not _alive(int(pid_file.read_text()))


def test_termination_forwarding_reaches_the_child_and_chains_the_prior_handler():
    """The handler forwards to the child's group, then lets the harness die (or raise)
    exactly as it would have. Exercised directly: sending ourselves a real signal inside
    a test suite is neither hermetic nor kind."""
    forwarded = []
    chained = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("betting_agent.sessions._kill_group",
                        lambda proc, sig: forwarded.append(sig))
    prior = signal.signal(signal.SIGTERM, lambda signum, frame: chained.append(signum))
    try:
        proc = SimpleNamespace(pid=-1)
        with sessions._forward_termination(proc):
            installed = signal.getsignal(signal.SIGTERM)
            assert installed is not prior
            installed(signal.SIGTERM, None)
        assert forwarded == [signal.SIGTERM]           # the child got it
        assert chained == [signal.SIGTERM]             # and so did the previous handler
        assert signal.getsignal(signal.SIGTERM) is not installed   # restored on the way out
    finally:
        monkeypatch.undo()
        signal.signal(signal.SIGTERM, prior)


def test_termination_forwarding_restores_handlers_even_when_the_body_raises():
    prior_term = signal.getsignal(signal.SIGTERM)
    prior_int = signal.getsignal(signal.SIGINT)
    with pytest.raises(RuntimeError):
        with sessions._forward_termination(SimpleNamespace(pid=-1)):
            raise RuntimeError("wait blew up")
    assert signal.getsignal(signal.SIGTERM) is prior_term
    assert signal.getsignal(signal.SIGINT) is prior_int


def test_terminate_child_is_a_no_op_for_a_finished_process():
    calls = []
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("betting_agent.sessions._kill_group",
                        lambda proc, sig: calls.append(sig))
    try:
        sessions._terminate_child(SimpleNamespace(pid=-1, poll=lambda: 0))
    finally:
        monkeypatch.undo()
    assert calls == []


# ------------------------------------------------------------------ CI-3 stream decoding
def test_stream_with_invalid_utf8_bytes_still_parses(tmp_path):
    """CI-3: one bad byte in a multi-megabyte stream raised UnicodeDecodeError — not an
    OSError, so it escaped the guard and killed the harness AFTER the session was paid
    for, leaving the attempt stuck in 'running'."""
    stream = tmp_path / "session.stream.jsonl"
    stream.write_bytes(
        b'{"type":"system","subtype":"init","session_id":"s1"}\n'
        b'\xff\xfe garbage that is not utf-8 at all \x80\n'
        b'{"type":"result","subtype":"success","is_error":false,"result":"done"}\n'
    )
    init, result, _health, error_code = _parse_stream(stream)
    assert init["session_id"] == "s1"
    assert result["result"] == "done"
    assert error_code is None


def test_stream_that_is_entirely_junk_bytes_is_tolerated(tmp_path):
    stream = tmp_path / "session.stream.jsonl"
    stream.write_bytes(b"\xff\xfe\x00\x80 not json, not utf-8\n")
    assert _parse_stream(stream)[:2] == (None, None)


def test_missing_stream_is_still_tolerated(tmp_path):
    assert _parse_stream(tmp_path / "nope.jsonl")[:2] == (None, None)


# ------------------------------------------------------------------ compute health (D11 §7)
def _health(tmp_path, *records):
    stream = tmp_path / "session.stream.jsonl"
    stream.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return _parse_stream(stream)[2]


_INIT = {"type": "system", "subtype": "init", "session_id": "s1"}
_OK_RESULT = {"type": "result", "subtype": "success", "is_error": False,
              "terminal_reason": "completed", "result": "done"}


def test_a_healthy_stream_reports_zero_of_everything(tmp_path):
    """docs/05 §8.1's reference run: zero api_retry records on the healthy session."""
    h = _health(tmp_path, _INIT, _OK_RESULT)
    assert (h.api_retries, h.throttle_errors, h.error_kinds) == (0, 0, {})


def test_api_retry_records_are_counted(tmp_path):
    """The earliest available signal — three to ten of these on every death (docs/05)."""
    h = _health(tmp_path, _INIT,
                {"type": "system", "subtype": "api_retry", "attempt": 1},
                {"type": "system", "subtype": "api_retry", "attempt": 2},
                _OK_RESULT)
    assert h.api_retries == 2
    assert h.throttle_errors == 0
    assert h.error_kinds == {"api_retry": 2}


def test_a_429_retry_is_also_a_throttle(tmp_path):
    h = _health(tmp_path, _INIT,
                {"type": "system", "subtype": "api_retry", "status": 429},
                {"type": "system", "subtype": "api_retry", "status": 529},
                {"type": "system", "subtype": "api_retry", "status": 500},
                _OK_RESULT)
    assert h.api_retries == 3
    assert h.throttle_errors == 2          # 500 is transient, not throttling
    assert h.error_kinds == {"api_retry:throttle": 2, "api_retry": 1}


def test_a_rate_limited_result_line_is_classified(tmp_path):
    """docs/05 §8.1: subtype stays "success" even on a failed run, so key on
    terminal_reason and api_error_status, never on subtype alone."""
    h = _health(tmp_path, _INIT, {
        "type": "result", "subtype": "success", "is_error": True,
        "terminal_reason": "api_error", "api_error_status": 429,
        "result": "rate limit exceeded",
    })
    assert h.throttle_errors == 1
    assert h.error_kinds == {"result:api_error:429": 1}


def test_a_connection_death_is_an_error_but_not_a_throttle(tmp_path):
    h = _health(tmp_path, _INIT, {
        "type": "result", "subtype": "success", "is_error": True,
        "terminal_reason": "api_error", "api_error_status": None,
        "result": "connection closed mid-response",
    })
    assert h.throttle_errors == 0
    assert h.error_kinds == {"result:api_error": 1}


def test_max_turns_reads_absent_fields_without_raising(tmp_path):
    """On ``error_max_turns`` the status and result fields are ABSENT, not null."""
    h = _health(tmp_path, _INIT,
                {"type": "result", "subtype": "error_max_turns", "is_error": True,
                 "terminal_reason": "max_turns"})
    assert h.throttle_errors == 0
    assert h.error_kinds == {"result:max_turns": 1}


def test_an_overloaded_message_without_a_status_still_counts(tmp_path):
    h = _health(tmp_path, _INIT,
                {"type": "system", "subtype": "api_error", "message": "Overloaded"},
                _OK_RESULT)
    assert h.throttle_errors == 1
    assert h.error_kinds == {"system:api_error": 1}


def test_junk_lines_between_records_are_skipped_not_counted(tmp_path):
    stream = tmp_path / "s.jsonl"
    stream.write_text(
        json.dumps(_INIT) + "\nnot json at all\n[]\n"
        + json.dumps({"type": "system", "subtype": "api_retry"}) + "\n",
        encoding="utf-8",
    )
    assert _parse_stream(stream)[2].api_retries == 1


def test_run_session_carries_compute_health_onto_the_result(tmp_path, monkeypatch):
    """The stub emits no retry records, so the honest answer is zero — and it is the
    session RESULT, not a second pass over the file, that carries it."""
    spec = _full_spec(cwd=tmp_path, wall_time_s=30, kind="attempt")
    result = run_session(spec, stub_runner_cmd(), tmp_path / "s.jsonl", tmp_path / "s.err")
    assert result.exit_kind == "ok"          # the session really ran
    assert result.api_retries == 0
    assert result.throttle_errors == 0
    assert result.error_kinds == {}


# ------------------------------------------------------------------ D2 infra classification
def _result(**over):
    kw = dict(
        exit_kind="error", is_error=True, result_text=None, structured_output=None,
        session_id="s1", cost_usd=None, input_tokens=None, output_tokens=None,
        num_turns=1, wall_seconds=1, init_manifest=None,
    )
    kw.update(over)
    return SessionResult(**kw)


def test_auth_error_stream_surfaces_the_machine_signals(tmp_path):
    """The Aug-5..08 wedge, end to end through run_session: the CODE and the terminal
    reason reach the SessionResult, and the prose is not what classifies it."""
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    res = _run(tmp_path, cwd, behavior="auth_error")
    assert res.exit_kind == "error" and res.is_error is True
    assert res.error_code == "authentication_failed"
    assert res.terminal_reason == "api_error"
    assert res.has_result is True
    assert res.cost_usd == Decimal("0")
    assert infra_error_kind(res) == "auth"


def test_retry_records_are_not_terminal_error_codes(tmp_path):
    """Mid-stream retry records carry {"error": "unknown"} in sessions that go on to
    succeed (28 of them live). Counting those would classify healthy sessions as infra."""
    stream = tmp_path / "session.stream.jsonl"
    stream.write_text(
        json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}) + "\n"
        + json.dumps({"type": "system", "subtype": "api_retry", "attempt": 1,
                      "retry_delay_ms": 577.03, "error_status": None,
                      "error": "unknown", "session_id": "s1"}) + "\n"
        + json.dumps({"type": "result", "subtype": "success", "is_error": False,
                      "terminal_reason": "completed", "result": "done"}) + "\n"
    )
    _init, _result_line, _health, error_code = _parse_stream(stream)
    assert error_code is None


@pytest.mark.parametrize("over,expected", [
    ({"exit_kind": "ok", "is_error": False}, None),                    # it ran and finished
    ({"exit_kind": "timeout"}, None),                                  # ran, wall clock killed it
    ({"exit_kind": "killed"}, None),
    ({"exit_kind": "env_not_hermetic"}, None),                         # CI-2 owns that path
    ({"error_code": "authentication_failed"}, "auth"),
    ({"terminal_reason": "api_error"}, "api"),
    ({"error_code": "server_error"}, "api"),
    ({"has_result": False}, "spawn"),                                  # died before any result
    ({"has_result": False, "wall_seconds": 1200}, None),               # ran 20 min, then died
    ({"terminal_reason": "structured_output_retry_exhausted"}, None),  # a CONTRACT failure
    ({"terminal_reason": "budget_exhausted"}, None),
    ({"terminal_reason": "max_turns"}, None),
])
def test_infra_error_kind_reads_the_live_vocabulary(over, expected):
    assert infra_error_kind(_result(**over)) == expected


def test_a_long_session_that_died_without_a_result_record_is_not_infra():
    """The one shape with no cost to guard on: a child killed after twenty minutes may
    well have spent money, so it keeps the contract path's backoff. Only a death on
    arrival — the actual spawn failure — is reclassified."""
    assert infra_error_kind(_result(has_result=False, wall_seconds=2)) == "spawn"
    assert infra_error_kind(_result(has_result=False, wall_seconds=1200)) is None


def test_a_session_that_billed_is_never_infra():
    """Cost is the guard: infra classification skips the day-stamp and retry backoff that
    stop 15-minute respawn loops, so a failure that spent money keeps them (LL-1/LL-2)."""
    paid = _result(error_code="authentication_failed", cost_usd=Decimal("2.5000"))
    assert infra_error_kind(paid) is None
    assert infra_error_kind(_result(error_code="authentication_failed",
                                    cost_usd=Decimal("0"))) == "auth"


def test_infra_error_kind_tolerates_a_foreign_object():
    assert infra_error_kind(SimpleNamespace()) is None
    assert infra_error_kind(None) is None


# ------------------------------------------------------------------ D8 auth preflight
def _fake_cli(tmp_path, name, body):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return str(path)


def test_check_claude_auth_passes_when_logged_in(tmp_path):
    cmd = _fake_cli(tmp_path, "claude-in",
                    'echo \'{"loggedIn": true, "authMethod": "claude.ai"}\'\n')
    ok, detail = check_claude_auth(cmd)
    assert ok is True and "claude.ai" in detail


def test_check_claude_auth_fails_only_on_a_definite_logout(tmp_path):
    cmd = _fake_cli(tmp_path, "claude-out", 'echo \'{"loggedIn": false}\'\nexit 1\n')
    ok, detail = check_claude_auth(cmd)
    assert ok is False and "logged out" in detail


@pytest.mark.parametrize("body", [
    "echo 'not json at all'\n",           # a CLI that answers in prose
    "echo '{\"other\": 1}'\n",            # a payload without loggedIn
    "exit 3\n",                           # a CLI that just fails
])
def test_check_claude_auth_is_ok_when_inconclusive(tmp_path, body):
    """An unreadable preflight must never be the reason the loop stops betting."""
    ok, _detail = check_claude_auth(_fake_cli(tmp_path, "claude-odd", body))
    assert ok is True


def test_check_claude_auth_survives_a_missing_binary(tmp_path):
    ok, detail = check_claude_auth(str(tmp_path / "no-such-claude"))
    assert ok is True and "unavailable" in detail


def test_check_claude_auth_probes_read_only_argv(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        return SimpleNamespace(stdout='{"loggedIn": true}', returncode=0)

    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    assert check_claude_auth("claude")[0] is True
    # No session, no prompt, no money: a status read and a timeout, nothing else.
    assert seen["argv"] == ["claude", "auth", "status", "--json"]
    assert seen["timeout"] == sessions._AUTH_PROBE_TIMEOUT_S
