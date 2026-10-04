"""Headless-agent session runner (spec §9.4).

Launches a Claude Code subprocess (an attempt or a director run), enforces a wall-clock
kill, parses the NDJSON stream for the init manifest and the final result, and checks
hermeticity. Verified against Claude Code v2.1.202.

**The stream IS the record.** This used to also copy Claude's own on-disk transcript into
the attempt's logs, which stored every session twice: the ``*.stream.jsonl`` written here
carries the same conversation, and is what ``activity.py`` reads and the retention pass
compresses. Duplicating multi-megabyte files to be read by nothing
was pure disk cost (EF-6). Copies made before this change are left exactly where they are
— retention compresses, it never deletes (decision D4).

Two design choices are made where the spec grants latitude, both for v1 simplicity:

* **Hermeticity is checked post-hoc**, after the process ends, rather than by streaming
  the first ``init`` line live and killing on the spot. A genuinely non-hermetic session
  therefore runs to its own end (or the wall clock) before being flagged
  ``env_not_hermetic``. The check only fires when the init line actually carries the
  ``memory_paths``/``mcp_servers`` keys, so stub runners (which omit them) are never
  spuriously failed — this is the simplest rule that distinguishes a real Claude init
  from a test double.
* **The child runs in its own session/process group** (``start_new_session=True``); the
  wall-clock enforcement signals the whole group (SIGTERM, then SIGKILL after a 30 s
  grace) so a session that spawned helpers cannot outlive its parent.
"""

from __future__ import annotations

import gzip
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

_GRACE_S = 30  # SIGTERM → this many seconds → SIGKILL (spec §9.4)

# Grace for the *abort* path (ST-6): the harness is going down or has already raised, so
# the child is not being given a chance to finish — only a chance to exit cleanly. Short
# by design; the wall-clock kill above keeps the spec's 30 s.
_ABORT_GRACE_S = 5

# Hardening env applied to every session (spec §9.4).
_HARDENING_ENV = {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS": "60000",
}


@dataclass
class SessionSpec:
    # "attempt" | "ideation" | "critic" | "implementation" | "grader" | "curator"
    # | "deep_review" | "director" (docs/22 sections 4.3 and 5.7). Only "attempt" and
    # "director" are written from the rebuild on; the rest stay legal so old rows read.
    kind: str
    cwd: Path
    prompt: str
    model: str
    effort: str
    max_turns: int
    wall_time_s: int
    session_id: str
    add_dirs: list[Path] = field(default_factory=list)
    max_budget_usd: Decimal | None = None
    tools: str | None = None
    json_schema: dict | None = None
    extra_env: dict[str, str] = field(default_factory=dict)
    disallowed_tools: str | None = None


@dataclass
class SessionResult:
    exit_kind: str  # "ok"|"error"|"timeout"|"killed"|"env_not_hermetic"
    is_error: bool
    result_text: str | None
    structured_output: Any | None
    session_id: str
    cost_usd: Decimal | None
    input_tokens: int | None
    output_tokens: int | None
    num_turns: int | None
    wall_seconds: int
    init_manifest: dict | None
    # Compute health (docs/14 D11 §7), parsed from the stream at close. See
    # :class:`ComputeHealth`; ``error_kinds`` is ``{kind: count}``.
    api_retries: int = 0
    throttle_errors: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)
    # The machine-readable failure signals the stream carries (docs/14 D2). Defaulted so
    # a hand-built double stays a valid SessionResult: absent signals read as "a session
    # that ran", which is what a test double represents.
    error_code: str | None = None       # terminal error CODE, e.g. "authentication_failed"
    terminal_reason: str | None = None  # the result record's own terminal_reason
    has_result: bool = True             # False when the stream carried no result record


def build_command(spec: SessionSpec, runner_cmd: str) -> list[str]:
    """The exact ``claude -p`` argv for ``spec`` (pure; unit-tested).

    Flag order follows spec §9.4. ``--max-budget-usd``, ``--tools``, ``--disallowedTools``,
    and ``--json-schema`` are emitted only when their fields are set; ``--add-dir`` is
    repeated per directory.
    """
    cmd = [
        runner_cmd, "-p", spec.prompt,
        "--safe-mode",
        "--permission-mode", "bypassPermissions",
        "--model", spec.model,
        "--effort", spec.effort,
        "--max-turns", str(spec.max_turns),
    ]
    if spec.max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(spec.max_budget_usd)]
    cmd += ["--session-id", spec.session_id]
    for d in spec.add_dirs:
        cmd += ["--add-dir", str(d)]
    cmd += ["--output-format", "stream-json", "--verbose"]
    if spec.tools:
        cmd += ["--tools", spec.tools]
    if spec.disallowed_tools:
        cmd += ["--disallowedTools", spec.disallowed_tools]
    if spec.json_schema is not None:
        # Defensively drop a top-level "$schema" key: the claude CLI's schema
        # validator rejects meta-schema references it doesn't bundle (live
        # failure 2026-07-13: 'no schema with key or ref ".../draft/2020-12/schema"').
        schema = {k: v for k, v in spec.json_schema.items() if k != "$schema"}
        cmd += ["--json-schema", json.dumps(schema)]
    return cmd


def _session_env(spec: SessionSpec) -> dict[str, str]:
    env = dict(os.environ)
    env.update(_HARDENING_ENV)
    # The session must find `bt` no matter how the harness was launched —
    # cron/launchd invoke .venv/bin/betting-agent without .venv/bin on PATH.
    venv_bin = str(Path(sys.executable).parent)
    path = env.get("PATH", "")
    if venv_bin not in path.split(os.pathsep):
        env["PATH"] = venv_bin + os.pathsep + path if path else venv_bin
    env.update(spec.extra_env)
    return env


def _cwd_slug(cwd: Path) -> str:
    """Absolute cwd with ``/`` replaced by ``-`` (the ``~/.claude/projects`` slug).

    Kept as the documented answer to "where does Claude keep its own transcript for this
    workspace" — the harness no longer copies that file (EF-6), but knowing the path is
    what makes the original recoverable by hand when someone wants it.
    """
    return str(Path(cwd).resolve()).replace("/", "-")


def _kill_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's whole process group; fall back to the child alone."""
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except (ProcessLookupError, OSError):
            pass


def _terminate_child(proc: subprocess.Popen) -> None:
    """Make sure the child's process group is dead; never raises (ST-6).

    Runs on the way out of :func:`run_session` however it exits. A no-op when the child
    already finished — which is the overwhelmingly common case — and the escalation
    (SIGTERM, brief grace, SIGKILL) for the child that ignores SIGTERM. Without this, a
    Ctrl-C'd or SIGTERM'd harness left a *detached* headless Claude session running: no
    parent, no wall clock, and up to three hours of API budget spending itself down.
    """
    if proc.poll() is not None:
        return
    _kill_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=_ABORT_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    _kill_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=_ABORT_GRACE_S)
    except subprocess.TimeoutExpired:  # unreapable: nothing further we can do
        pass


@contextmanager
def _forward_termination(proc: subprocess.Popen):
    """SIGTERM/SIGINT reach the child while this session runs, then hands back (ST-6).

    The child lives in its own process group precisely so it cannot be killed by a
    signal aimed at ours — which also means a signal aimed at ours never reached it.
    While the session is in flight, this bridges the gap: the handler forwards SIGTERM
    to the child's group, then lets the harness die (or raise ``KeyboardInterrupt``)
    exactly as it would have.

    Chaining matters. A previous handler that is *callable* — pytest's, or Python's own
    ``default_int_handler`` — is invoked, so Ctrl-C still raises ``KeyboardInterrupt``
    where the caller expects it. A previous ``SIG_DFL`` is restored and the signal
    re-raised at ourselves, so the process dies of the signal it was sent rather than
    swallowing it. Handlers can only be installed on the main thread; off it, this is a
    no-op and the try/finally in :func:`run_session` is the whole guarantee.
    """
    prior: dict[int, Any] = {}

    def handler(signum, frame):
        _kill_group(proc, signal.SIGTERM)
        previous = prior.get(signum, signal.SIG_DFL)
        if callable(previous):
            previous(signum, frame)
            return
        try:
            signal.signal(signum, previous)
            if previous is signal.SIG_DFL:
                os.kill(os.getpid(), signum)
        except (ValueError, OSError):  # noqa: S110 - best effort on the way out
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prior[sig] = signal.signal(sig, handler)
        except (ValueError, OSError):  # not the main thread, or no such signal
            pass
    try:
        yield
    finally:
        for sig, previous in prior.items():
            try:
                signal.signal(sig, previous)
            except (ValueError, OSError):
                pass


def _wait_with_timeout(proc: subprocess.Popen, wall_time_s: int) -> tuple[bool, bool]:
    """Wait for ``proc``; enforce the wall clock. Returns ``(timed_out, hard_killed)``."""
    timed_out = False
    hard_killed = False
    try:
        proc.wait(timeout=wall_time_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=_GRACE_S)
        except subprocess.TimeoutExpired:
            hard_killed = True
            _kill_group(proc, signal.SIGKILL)
            proc.wait()
    return timed_out, hard_killed


# --------------------------------------------------------------------------- compute health
# docs/14 D11 §7. What the stream is known to carry, from the corpus of real runs analysed
# for docs/05 §8.1 (verified there, not guessed here):
#   * ``{"type": "system", "subtype": "api_retry", …}`` records — "zero on the healthy
#     reference run, three to ten on every death". This is the retry signal.
#   * the final ``result`` line's ``terminal_reason`` (``completed``/``api_error``/
#     ``max_turns``) and ``api_error_status`` (429 = rate-limited, 5xx = transient,
#     ``null`` = a connection death). On ``subtype == "error_max_turns"`` those fields are
#     ABSENT, not null — every read below uses ``.get()``.
# Nothing here invents a field: a stream that carries none of these yields zeros, and the
# audit distinguishes "zero" from "not instrumented" by the column being NULL.
_THROTTLE_STATUSES = frozenset({429, 529})
_THROTTLE_WORDS = ("rate_limit", "rate limit", "ratelimit", "overloaded", "overload")


def _status_of(obj: dict) -> int | None:
    """The HTTP-ish status a record carries, under any of the names the stream uses."""
    for key in ("api_error_status", "status", "status_code"):
        value = obj.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _is_throttle(obj: dict) -> bool:
    """True when this record names a rate-limit / overload condition.

    Status first (429 and Anthropic's 529 ``overloaded``), then a word match over the
    record's own error text — the text arm is what catches an overload reported without a
    numeric status, and it can only ever over-count a record that already errored.
    """
    if _status_of(obj) in _THROTTLE_STATUSES:
        return True
    for key in ("terminal_reason", "error", "message", "result", "subtype"):
        value = obj.get(key)
        if isinstance(value, str):
            low = value.lower()
            if any(w in low for w in _THROTTLE_WORDS):
                return True
    return False


@dataclass
class ComputeHealth:
    """Throttling telemetry for one session (docs/14 D11 §7)."""

    api_retries: int = 0
    throttle_errors: int = 0
    error_kinds: dict[str, int] = field(default_factory=dict)

    def _count(self, kind: str) -> None:
        self.error_kinds[kind] = self.error_kinds.get(kind, 0) + 1


def _observe(health: ComputeHealth, obj: dict) -> None:
    """Fold one stream record into ``health``. Pure bookkeeping; never raises."""
    kind = obj.get("type")
    subtype = obj.get("subtype")
    if kind == "system" and subtype == "api_retry":
        health.api_retries += 1
        if _is_throttle(obj):
            health.throttle_errors += 1
            health._count("api_retry:throttle")
        else:
            health._count("api_retry")
        return
    if kind == "system" and isinstance(subtype, str) and "error" in subtype:
        health._count(f"system:{subtype}")
        if _is_throttle(obj):
            health.throttle_errors += 1
        return
    if kind == "result":
        reason = obj.get("terminal_reason")
        errored = bool(obj.get("is_error")) or (
            isinstance(reason, str) and reason not in ("completed", "")
        )
        if not errored:
            return
        status = _status_of(obj)
        # ``subtype`` is "success" even on a failed run (docs/05 §8.1), so it is only a
        # useful label when it says something else ("error_max_turns").
        label = reason or (subtype if subtype not in (None, "", "success") else "error")
        health._count(f"result:{label}" + (f":{status}" if status is not None else ""))
        if _is_throttle(obj):
            health.throttle_errors += 1


def iter_stream_records(stream_path: Path) -> Iterator[dict]:
    """Every JSON object in an NDJSON session stream, tolerant of junk.

    The one reader for these files. A stream is a log a subprocess was writing when it was
    killed, so it is read defensively at three levels: an unreadable file yields nothing, a
    line that is not JSON is skipped, and a JSON value that is not an object is skipped.
    Callers get objects or nothing and never an exception.

    CI-3: one invalid byte in a multi-megabyte stream used to raise ``UnicodeDecodeError``
    — not an ``OSError``, so it escaped the guard and killed the harness *after* the
    session's money was spent, leaving the attempt ``running``. "Tolerant of junk" includes
    junk bytes. The ``ValueError`` arm is belt and braces (``UnicodeDecodeError`` is a
    ``ValueError``) for a codec that raises anyway.

    A ``.gz`` path is read as gzip: the retention pass (``cli._gzip_in_place``) compresses
    old streams in place, and a reader that could not open one would silently report an
    empty session for every attempt old enough to have been swept.
    """
    stream_path = Path(stream_path)
    try:
        if stream_path.name.endswith(".gz"):
            with gzip.open(stream_path, "rt", encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        else:
            text = stream_path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError, EOFError):
        return
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            yield obj


def _parse_stream(
    stream_path: Path,
) -> tuple[dict | None, dict | None, ComputeHealth, str | None]:
    """``(init_line, result_line, compute_health, error_code)`` from the NDJSON stream.

    One junk-tolerant pass: the health scan (docs/14 D11 §7) and the failure-signal scan
    (docs/14 D2) both ride along with the init/result extraction rather than re-reading a
    file that can run to megabytes. ``error_code`` is the last record-level ``"error"``
    string that is not a *retry* record — retry records (the ones carrying
    ``retry_delay_ms``) report ``"error": "unknown"`` in the middle of sessions that go on
    to succeed, so counting them as a terminal signal would classify healthy sessions as
    infrastructure failures; they are counted by the health scan instead.
    """
    init = None
    result = None
    health = ComputeHealth()
    error_code = None
    for obj in iter_stream_records(stream_path):
        code = obj.get("error")
        if isinstance(code, str) and code and "retry_delay_ms" not in obj:
            error_code = code
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            if init is None:
                init = obj
        elif kind == "result":
            result = obj
        _observe(health, obj)
    return init, result, health, error_code


def _hermeticity_enforced(init: dict) -> bool:
    """True iff the init line looks like a real Claude init (carries the checked keys)."""
    return "memory_paths" in init or "mcp_servers" in init


def _hermeticity_ok(init: dict, requested_model: str | None) -> bool:
    """Spec §9.4: memory_paths empty, mcp_servers empty, model as configured."""
    if init.get("memory_paths"):
        return False
    if init.get("mcp_servers"):
        return False
    model = init.get("model")
    if model is not None and requested_model is not None and model != requested_model:
        return False
    return True


def run_session(
    spec: SessionSpec, runner_cmd: str, stream_path: Path, err_path: Path
) -> SessionResult:
    """Run one session to completion under the wall clock and return its parsed result."""
    cmd = build_command(spec, runner_cmd)
    env = _session_env(spec)
    stream_path = Path(stream_path)
    err_path = Path(err_path)
    stream_path.parent.mkdir(parents=True, exist_ok=True)
    err_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    with open(stream_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(
            cmd, cwd=str(spec.cwd), stdout=out, stderr=err, env=env,
            start_new_session=True,
        )
        # ST-6: however this scope is left — the wall clock, an interrupt, a bug in the
        # wait itself — the child does not outlive it. A paid headless session with no
        # parent is the one failure mode here that keeps costing money after the fact.
        try:
            with _forward_termination(proc):
                timed_out, hard_killed = _wait_with_timeout(proc, spec.wall_time_s)
        finally:
            _terminate_child(proc)
    wall_seconds = int(round(time.monotonic() - start))
    returncode = proc.returncode

    init, result, health, error_code = _parse_stream(stream_path)

    is_error = bool(result.get("is_error")) if result else returncode not in (0, None)
    result_text = result.get("result") if result else None
    structured_output = result.get("structured_output") if result else None
    num_turns = result.get("num_turns") if result else None
    cost_usd: Decimal | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    if result:
        raw_cost = result.get("total_cost_usd")
        if raw_cost is not None:
            cost_usd = Decimal(str(raw_cost))
        usage = result.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
    session_id = (init.get("session_id") if init else None) or spec.session_id

    if timed_out:
        exit_kind = "killed" if hard_killed else "timeout"
    elif init is not None and _hermeticity_enforced(init) and not _hermeticity_ok(
        init, spec.model
    ):
        exit_kind = "env_not_hermetic"
    elif is_error or returncode not in (0, None):
        exit_kind = "error"
    else:
        exit_kind = "ok"

    return SessionResult(
        exit_kind=exit_kind,
        is_error=is_error,
        result_text=result_text,
        structured_output=structured_output,
        session_id=session_id,
        cost_usd=cost_usd,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        num_turns=num_turns,
        wall_seconds=wall_seconds,
        init_manifest=init,
        api_retries=health.api_retries,
        throttle_errors=health.throttle_errors,
        error_kinds=dict(health.error_kinds),
        error_code=error_code,
        terminal_reason=result.get("terminal_reason") if result else None,
        has_result=result is not None,
    )


# --------------------------------------------------------------------------- D2: infra vs contract
# Live vocabulary, read off every stream under ``data/attempts/**`` on 2026-08-09:
# ``terminal_reason`` is one of ``completed``, ``api_error``,
# ``structured_output_retry_exhausted``, ``budget_exhausted``, ``max_turns``. Only
# ``api_error`` means the model never got to run — the other three are sessions that ran
# and ended badly, ``structured_output_retry_exhausted`` being a contract failure exactly
# as recorded today. The wedge's auth shape (2026-08-05..08, docs/12 §8.4) is an assistant
# record carrying ``{"error": "authentication_failed"}`` next to a result record whose
# ``subtype`` still reads ``success`` while ``is_error`` is true and ``terminal_reason`` is
# ``api_error``. The prose ("Failed to authenticate: OAuth session expired …") is never
# parsed: a message string is not a classification signal.
_INFRA_TERMINAL_REASONS = frozenset({"api_error"})
_AUTH_ERROR_CODES = frozenset({"authentication_failed"})

# A session that emitted no result record at all reports no cost either, so the cost guard
# below cannot see it. This is the second guard for that one case: CLI start-up plus the
# init line is seconds, and a model turn is tens of seconds, so a child that died this
# fast without a result record never ran a model. A long-running session killed at the end
# looks identical from the stream and is NOT reclassified — it may well have spent money.
_SPAWN_DEATH_S = 60


def infra_error_kind(result) -> str | None:
    """``"auth"`` / ``"api"`` / ``"spawn"`` when a session died before the model ran (D2).

    ``None`` for everything else, deliberately including: a clean exit, a wall-clock kill
    (``timeout``/``killed`` — the model ran, it just did not finish), a hermeticity
    failure (CI-2 owns that path), and **any session that billed**.

    Cost is the guard that makes this safe. Classifying a failure as infra strips it of
    the day-stamp and retry backoff that exist to stop 15-minute respawn loops (LL-1,
    LL-2) — cheap when the failed session cost $0.00, ruinous when it did not. So a
    session that spent money stays on the contract path whatever its stream says, and the
    one shape whose cost is unknowable (no result record) has to have died inside
    ``_SPAWN_DEATH_S`` to qualify.
    """
    if getattr(result, "exit_kind", None) != "error":
        return None
    cost = getattr(result, "cost_usd", None)
    if cost is not None and cost > 0:
        return None
    code = getattr(result, "error_code", None)
    if code in _AUTH_ERROR_CODES:
        return "auth"
    if getattr(result, "terminal_reason", None) in _INFRA_TERMINAL_REASONS or code:
        return "api"
    if not getattr(result, "has_result", True):
        wall = getattr(result, "wall_seconds", None)
        if wall is not None and wall <= _SPAWN_DEATH_S:
            return "spawn"  # died on arrival, without ever emitting a result record
    return None


# --------------------------------------------------------------------------- D8: auth preflight
_AUTH_PROBE_TIMEOUT_S = 20


def check_claude_auth(runner_cmd: str, timeout_s: int = _AUTH_PROBE_TIMEOUT_S) -> tuple[bool, str]:
    """``(ok, detail)`` from ``<runner> auth status --json`` — the D8 preflight.

    The cheapest deterministic auth check the Claude CLI offers: it answers from local
    credentials, starts no session, and spends nothing. **Only a definite "logged out"
    fails.** A probe that cannot run, times out, answers with something other than JSON,
    or answers without a ``loggedIn`` field returns ok — an unreadable preflight must
    never be the reason the loop stops betting. Nothing but ``loggedIn``/``authMethod`` is
    read out of the payload: the rest is account identity, and it does not belong in an
    audit row.
    """
    try:
        proc = subprocess.run(
            [runner_cmd, "auth", "status", "--json"],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return True, f"preflight unavailable ({type(exc).__name__})"
    try:
        payload = json.loads((proc.stdout or "").strip() or "{}")
    except ValueError:
        return True, "preflight inconclusive: answer was not JSON"
    if not isinstance(payload, dict) or "loggedIn" not in payload:
        return True, "preflight inconclusive: no loggedIn field"
    if payload.get("loggedIn"):
        return True, f"logged in via {payload.get('authMethod') or 'an unnamed method'}"
    return False, "the claude CLI reports logged out"
