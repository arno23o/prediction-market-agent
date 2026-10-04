"""Attempt orchestration (docs/22 section 5.2; spec §9.1–9.5, §9.7; Jul29 spec L18).

``run_attempt`` creates the ledger row, lays out the workspace, renders the cell's
``CONTEXT.md`` and ``TASK.md``, runs the headless attempt session, records the session
result, and drives ticket intake through the validator and executor owned by the rest
of the harness. The load-bearing rule is the contract rule (spec §9.4): a ticket whose
``bets.json`` exists and parses is processed regardless of how the session exited (a
timed-out session that finished its ticket still counts); only when no parseable ticket
exists does the exit status decide ``ticket_invalid`` vs ``failed``.

**One exit kind sits above the contract rule**: ``env_not_hermetic`` fails the attempt
outright, ticket or no ticket (spec §9.4; CI-2). A session that escaped its envelope did
not run the experiment we launched, so its ticket is not evidence and must not become real
orders.

One shape, one session. The two-loop attempt (ideation, critic, implementation) is
retired: it cost 2.6 times as much per attempt for no measurable quality gain, and its
prompts and result are archived in ``docs/archive/prompts/`` (docs/22 section 2.2).

What an attempt reads before it looks at a market is its cell, and ``harness/cells.py``
owns that: the day's plan says which cell a slot runs and on which arm (a model and an
effort, recorded on the row as what ran), and ``cells.render`` returns the ``CONTEXT.md``
body, the two prompt placeholders, the examples it holds and the hash of the direction it
carries.

Interface contract with the validator/executor (``parse_ticket``/``validate_ticket``/
``execute_attempt`` are imported lazily inside ``_intake``, which is the seam the tests
mock):

    parse_ticket(ticket_dir: Path) -> parsed
        parsed.ok: bool                 # False on whole-ticket V01/V02 failure
        parsed.edge_claim_md: str
        parsed.hypothesis_md: str
        parsed.manifest_md: str

    validate_ticket(parsed, *, market_fetch, book_fetch, settings, attempt_id, now)
        -> validated                    # opaque; consumed by execute_attempt

    execute_attempt(attempt_id, validated, ledger, client, settings, now=None)
        -> "placed" | "no_bets"         # writes bet rows + audits orders + ticket_truncated,
                                        # but does NOT transition attempt status

This module owns the attempt-status transitions and these audit events:
``attempt_launched``, ``session_end``, ``principles_missing``, ``session_row_error``,
``ticket_invalid``, ``attempt_crashed``, ``activity_record_error``; ``execute_attempt``
owns order/truncation audits.

**No attempt is left in ``running`` by a harness-side failure** (AE-2). Everything after
the ``running`` transition is wrapped: any exception records the error, audits
``attempt_crashed``, transitions the attempt to ``failed``, and then propagates so the
process still exits nonzero with its traceback. The stale-``running`` reaper in the tick
covers what no in-process guard can — SIGKILL and a sleeping host.
"""

from __future__ import annotations

import hashlib
import json
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from betting_agent.harness import cells
from betting_agent.harness.activity import activity_row
from betting_agent.sessions import SessionSpec, infra_error_kind, run_session
from betting_agent.timeutil import iso, utc_now

# Attempt bootstrap prompt (constant; spec §9.4).
_BOOTSTRAP = (
    "Read ../TASK.md and carry it out completely. "
    "If ../CONTEXT.md exists, study it before choosing a target."
)

_ERR_MAX = 500  # sessions.error is a diagnostic breadcrumb, not a transcript

# ``sessions.run_session`` sets this exit kind when the session's init line shows the
# hermetic envelope was violated (leaked memory paths, wrong model). Spec §9.4 makes it
# fatal to the attempt — see :func:`_fail_not_hermetic`.
_NOT_HERMETIC = "env_not_hermetic"

# Validation detail carried into the ``ticket_invalid`` audit (PC-2): enough to act on,
# bounded so an audit row never becomes a transcript.
_DETAIL_MAX = 20
_DETAIL_CHARS = 200

# docs/14 D9 (the A-0053 shape, docs/12 §3): how far a session's real wall-clock span may
# exceed its monotonic ``wall_seconds`` before the gap is recorded as a host sleep rather
# than measurement noise. A-0053 ran 25 monotonic minutes over a real 103 -- a ~78 minute
# gap -- so ten minutes is well clear of ordinary scheduling jitter (API waits, a slow
# tool call) while catching that shape easily.
_SLEEP_STRETCH_S = 600


def _template_bytes(name: str = "attempt.md") -> bytes:
    """Raw bytes of a packaged prompt template."""
    return (files("betting_agent") / "prompts" / name).read_bytes()


def _toolkit_version() -> str:
    try:
        return metadata.version("betting-agent")
    except Exception:  # noqa: BLE001 - fall back when the package metadata is absent
        return "0.1.0"


def _sha12(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:12]


def _prompt_version() -> str:
    """``prompt_version``: the hash of the attempt template's bytes."""
    return _sha12(_template_bytes())


def _render_task(template: str, subs: dict[str, str]) -> str:
    """Sequential ``str.replace`` of the known placeholders.

    The template embeds literal JSON braces (the ``bets.json`` example), so ``str.format``
    would raise/misfire — only the enumerated ``{placeholder}`` tokens are substituted.
    """
    out = template
    for key, val in subs.items():
        out = out.replace("{" + key + "}", val)
    return out


def _stamp_clock(text: str) -> str:
    """Prepend the render-time UTC clock to the TASK.md body (docs/14 D9; docs/12 §9.11,
    where both A-0055 Fable sessions fabricated timestamps in their artifacts for lack of
    any real clock in the rendered prompt).

    TASK.md only. CONTEXT.md is written exactly as ``cells.render`` produced it, because
    nothing but the cell's own sections goes into it (docs/22 section 8.5) and its hash is
    of the bytes on disk.

    A fresh ``utc_now()`` read at each call, never the attempt's launch ``now``.
    """
    return f"Current time: {iso(utc_now())}\n\n{text}"


def _merge_variant(variant: str | None, **extra) -> str | None:
    """Merge ``extra`` into the variant JSON label, creating the object when absent."""
    if not extra:
        return variant
    label: dict = {}
    if variant:
        try:
            loaded = json.loads(variant)
            if isinstance(loaded, dict):
                label = loaded
        except (ValueError, TypeError):
            label = {"variant": variant}
    label.update(extra)
    return json.dumps(label)


def _model_substitutions_for(settings, model: str, now) -> dict[str, str]:
    """``{configured: effective}`` for the model THIS attempt substitutes (2026-08-18).

    Restricted to the model the attempt actually launches, not the whole configured table:
    a switch that also names Sonnet has nothing to do with an Opus attempt, and recording
    it on that attempt's row would make the marker a copy of config.toml instead of a
    record of what happened here.

    Read at the attempt's launch clock, while :func:`_launch` reads the session's own. The
    two can only disagree for the single attempt that straddles the expiry instant, and for
    that one the ``sessions`` row is the finer record, which is why it, not this marker, is
    what the per-session model is read from.
    """
    subs = settings.model_substitutions(now)
    return {model: subs[model]} if model in subs else {}


# --------------------------------------------------------------------------- sessions rows (L18)
def _audit_quiet(ledger, event: str, attempt_id: str | None, detail: dict) -> None:
    """Audit without ever raising — used on the failure paths of bookkeeping itself."""
    try:
        ledger.audit(event, attempt_id=attempt_id, detail=detail)
    except Exception:  # noqa: BLE001 - a failed audit must not mask the original failure
        pass


def record_activity(ledger, settings, attempt_id: str, wall_seconds) -> None:
    """Record what this attempt did (schema 008); never raises.

    Runs once the session(s) have finished and the ticket is on disk, next to the
    ``session_end`` audit. Reading a stream is pure measurement — no market is fetched, no
    money column is touched, no status moves — but it happens at the end of a run that may
    have placed real orders, so the whole thing is wrapped: a bookkeeping row is never a
    reason to fail an attempt, and an attempt missing its activity row is a gap the
    ``betting-agent activity --backfill`` pass closes.

    The row's code stamp is taken here, in the tree the attempt actually ran in, which is
    the stamp worth having (``stamped_at_backfill`` is 0 for exactly these rows).
    """
    try:
        row = ledger.get_attempt(attempt_id) or {}
        ledger.upsert_activity(activity_row(
            settings.attempts_dir / attempt_id, attempt_id, wall_seconds,
            prompt_version=row.get("prompt_version"),
        ))
    except Exception as exc:  # noqa: BLE001 - measurement must never fail an attempt
        _audit_quiet(ledger, "activity_record_error", attempt_id,
                     {"error": f"{type(exc).__name__}: {exc}"})


def open_session_row(ledger, *, session_id: str, kind: str, model: str, started_at: str,
                     attempt_id: str | None = None) -> None:
    """Insert the ``sessions`` row for a launching session (L18); never raises.

    Lives here (rather than in a shared module) because this is the module that owns
    session orchestration.
    """
    try:
        ledger.insert_session(session_id, kind, model, started_at, attempt_id=attempt_id)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never kill the run
        _audit_quiet(ledger, "session_row_error", attempt_id,
                     {"session_id": session_id, "kind": kind, "step": "insert",
                      "error": f"{type(exc).__name__}: {exc}"})


def close_session_row(ledger, session_id: str, result, *, ended_at: str,
                      attempt_id: str | None = None) -> None:
    """Finish the ``sessions`` row from a :class:`SessionResult` (L18); never raises."""
    text = getattr(result, "result_text", None)
    error = text[:_ERR_MAX] if (getattr(result, "is_error", False) and text) else None
    try:
        ledger.finish_session(
            session_id,
            ended_at=ended_at,
            exit=getattr(result, "exit_kind", None),
            num_turns=getattr(result, "num_turns", None),
            cost_usd=getattr(result, "cost_usd", None),
            input_tokens=getattr(result, "input_tokens", None),
            output_tokens=getattr(result, "output_tokens", None),
            wall_seconds=getattr(result, "wall_seconds", None),
            error=error,
            # docs/14 D11 §7: capture once here, audit forever from SQL. ``getattr`` with
            # None keeps the doubles that return a bare namespace working unchanged, and
            # None is exactly the right value for "this session was never instrumented".
            api_retries=getattr(result, "api_retries", None),
            throttle_errors=getattr(result, "throttle_errors", None),
            error_kinds=getattr(result, "error_kinds", None),
        )
    except Exception as exc:  # noqa: BLE001 - bookkeeping must never kill the run
        _audit_quiet(ledger, "session_row_error", attempt_id,
                     {"session_id": session_id, "step": "finish",
                      "error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------- session launch
def _launch(ledger, settings, *, attempt_id: str, kind: str, session_id: str, cwd: Path,
            model: str, effort: str, max_turns: int, wall_time_s: int, budget,
            attempt_dir: Path, stream_stem: str, past_mode: str):
    """Run one attempt-side session with the standard hardening + its ``sessions`` row.

    ``started_at``/``ended_at`` are each a real ``utc_now()`` taken at the moment this
    session's row actually opens/closes (AE-5), never the attempt's launch clock.

    ``model`` arrives as the CONFIGURED model and is resolved through the substitution
    switch here (2026-08-18), one line above the ``sessions`` row it is written into, so
    that "the ledger records the model that actually ran" is true by construction.

    ``past_mode`` is the cell's ``bt past`` gate: ``off`` for the baseline cell, ``on`` for
    every other.
    """
    model = settings.effective_model(model)
    cwd.mkdir(parents=True, exist_ok=True)
    logs_dir = attempt_dir / "logs"
    started = utc_now()
    open_session_row(ledger, session_id=session_id, kind=kind, model=model,
                     started_at=iso(started), attempt_id=attempt_id)
    spec = SessionSpec(
        kind=kind,
        cwd=cwd,
        prompt=_BOOTSTRAP,
        model=model,
        effort=effort,
        max_turns=max_turns,
        wall_time_s=wall_time_s,
        session_id=session_id,
        add_dirs=[attempt_dir],
        max_budget_usd=budget,
        extra_env={
            "BT_ATTEMPT_ID": attempt_id,
            "BT_PAST": past_mode,
            "BT_ROOT": str(settings.root),
        },
        # Nested agents (Task) are legitimate, but user-global skills must not leak into
        # a hermetic experiment: an inherited `deep-research` skill drove a multi-level
        # research fan-out that burned most of the budget cap before any ticket was
        # written (pilot audit, docs/decisions.md). `--safe-mode` does not block skills.
        disallowed_tools="Skill",
    )
    result = run_session(
        spec,
        runner_cmd=settings.attempt.runner,
        stream_path=logs_dir / f"{stream_stem}.stream.jsonl",
        err_path=logs_dir / f"{stream_stem}.err",
    )
    ended = utc_now()
    close_session_row(ledger, session_id, result, ended_at=iso(ended),
                      attempt_id=attempt_id)
    _flag_if_sleep_stretched(ledger, attempt_id, started, ended, result)
    return result


def _flag_if_sleep_stretched(ledger, attempt_id: str, started, ended, result) -> None:
    """docs/14 D9 (the A-0053 shape): ``result.wall_seconds`` is a monotonic elapsed time,
    which does not advance while the host sleeps. ``started``/``ended`` are real clock
    reads bracketing the same :func:`run_session` call, so their difference is the
    session's true wall-clock span; when it outruns ``wall_seconds`` by more than
    :data:`_SLEEP_STRETCH_S`, the gap is almost certainly the host asleep mid-session, and
    the attempt carries that forward — never cleared once set, since a later phase running
    cleanly does not undo an earlier one's lost time.
    """
    wall_seconds = getattr(result, "wall_seconds", None)
    if wall_seconds is None:
        return
    real_seconds = (ended - started).total_seconds()
    if real_seconds - wall_seconds > _SLEEP_STRETCH_S:
        try:
            ledger.update_attempt_fields(attempt_id, sleep_stretched=True)
        except Exception:  # noqa: BLE001 - bookkeeping must never kill the run
            pass


# --------------------------------------------------------------------------- intake
def _audit_ticket_invalid(ledger, attempt_id: str, codes, detail) -> None:
    """The ``ticket_invalid`` audit: codes plus the reasons behind them (PC-2).

    The reasons are what makes the format-failure telemetry (D3) legible — "V01 x3" says
    nothing about whether strictness or sloppiness is burning the sessions.
    """
    _audit_quiet(
        ledger, "ticket_invalid", attempt_id,
        {"codes": list(codes),
         "detail": [str(d)[:_DETAIL_CHARS] for d in list(detail)[:_DETAIL_MAX]]},
    )


def _audit_phase_infra(ledger, attempt_id: str, kind: str, result=None, *,
                       exit_kind: str | None = None, **extra) -> None:
    """The D2 audit for a session that died before the model ran.

    Distinct from the contract event (``ticket_invalid``) on purpose: that describes a
    model that ran and produced something the harness could not use, and the D3 telemetry
    prices exactly that. An auth outage in the same slot is a different fact about a
    different system, and the Aug-5 wedge proved what happens when the two share a row.
    """
    detail = {
        "phase": "attempt",
        "kind": kind,
        "exit_kind": getattr(result, "exit_kind", exit_kind),
        "error_code": getattr(result, "error_code", None),
        "terminal_reason": getattr(result, "terminal_reason", None),
        "action": "infrastructure failure, not a model contract failure (docs/14 D2)",
    }
    detail.update(extra)
    _audit_quiet(ledger, "phase_infra_error", attempt_id, detail)


def _fail_not_hermetic(ledger, attempt_id: str) -> str:
    """Hermeticity is a *precondition*, not a tiebreaker: fail the attempt (CI-2).

    Spec §9.4 says a hermeticity mismatch fails the attempt. The contract rule — "a
    parseable ticket is processed however the session exited" — was being applied first,
    so a session that leaked memory paths or ran the wrong model still got its bets
    placed **with real money**, and its result still entered the experiment record as if
    the cells had held. The exit-kind was consulted only when the ticket was missing.

    Ordering matters and is deliberate: this check runs before the contract rule, because
    what is wrong here is not the ticket — it is the conditions the ticket was produced
    under. A ticket from a non-hermetic session is not a bad ticket to reject; it is an
    invalid experiment to discard.
    """
    ledger.audit("env_not_hermetic", attempt_id=attempt_id,
                 detail={"phase": "attempt", "exit_kind": _NOT_HERMETIC,
                         "action": "attempt failed before any order (spec §9.4)"})
    ledger.update_attempt_fields(attempt_id, error="env_not_hermetic (attempt)")
    ledger.transition(attempt_id, "failed")
    return "failed"


def _fail_running(ledger, attempt_id: str, exc: BaseException, *, source: str) -> bool:
    """Drive a still-``running`` attempt to ``failed``, recording ``exc`` (AE-2).

    The crash-recovery primitive. Returns True iff this call performed the transition,
    so a caller further out can tell "already handled" from "still stuck" and not
    double-transition. Never raises: it runs on the way out of a failure, and a
    bookkeeping error here must not replace the error the operator needs to see.

    An attempt that already left ``running`` is left exactly as it is — the strict state
    machine is not softened anywhere in this package, and a ``placed`` attempt whose
    *bookkeeping* then blew up is placed, not failed.
    """
    try:
        row = ledger.get_attempt(attempt_id)
    except Exception:  # noqa: BLE001 - an unreadable ledger is the reaper's problem now
        return False
    if row is None or row.get("status") != "running":
        return False
    error = f"{type(exc).__name__}: {exc}"[:_ERR_MAX]
    try:
        ledger.update_attempt_fields(attempt_id, error=error)
    except Exception:  # noqa: BLE001 - the transition matters more than the message
        pass
    _audit_quiet(ledger, "attempt_crashed", attempt_id, {"error": error, "source": source})
    try:
        ledger.transition(attempt_id, "failed")
        return True
    except Exception:  # noqa: BLE001 - the stale-running reaper is the next line of defense
        return False


def _intake(ledger, client, settings, *, attempt_id: str, ticket_dir: Path,
            exit_kind: str | None, now, session_result=None) -> str:
    """Contract rule + validate/execute; returns the final status (spec §9.4–9.5).

    ``session_result`` is the result of the session that was supposed to write this ticket,
    consulted for one thing only: telling "wrote no ticket because it never ran" from
    "wrote no ticket" (docs/14 D2).
    """
    # CI-2: BEFORE the contract rule, and regardless of whether the ticket parsed.
    if exit_kind == _NOT_HERMETIC:
        return _fail_not_hermetic(ledger, attempt_id)

    bets_path = ticket_dir / "bets.json"
    ticket_ok = False
    if bets_path.exists():
        try:
            json.loads(bets_path.read_text())
            ticket_ok = True
        except (json.JSONDecodeError, OSError):
            ticket_ok = False

    if not ticket_ok:
        # No parseable ticket: a clean exit means the agent produced nothing
        # (ticket_invalid); a crashed/timed-out/non-hermetic session is a hard failure.
        status = "ticket_invalid" if exit_kind == "ok" else "failed"
        if status == "ticket_invalid":
            _audit_ticket_invalid(
                ledger, attempt_id, ["V01"],
                [f"bets.json is missing or does not parse at {bets_path}"],
            )
        elif (infra := infra_error_kind(session_result)) is not None:
            # D2: the row said "failed" and nothing said why. It never wrote a ticket
            # because it never ran.
            _audit_phase_infra(ledger, attempt_id, infra, session_result,
                               exit_kind=exit_kind)
            try:
                ledger.update_attempt_fields(attempt_id,
                                             error=f"session_infra_{infra} (attempt)")
            except Exception:  # noqa: BLE001 - the transition matters more than the label
                pass
        ledger.transition(attempt_id, status)
        return status

    # Everything from here on is inside the try (AE-2). The parse and the three ledger
    # writes that follow it used to sit OUTSIDE it: a ledger error there — the exact
    # class of failure a locked or full database produces — propagated out of the
    # harness and left the attempt in ``running`` forever, after the session was paid
    # for. Nothing between the ``running`` transition and a terminal status may escape.
    try:
        # Lazily imported so this module loads before validate/execute exist.
        from betting_agent.harness.execute import execute_attempt
        from betting_agent.harness.validate import parse_ticket, validate_ticket

        parsed = parse_ticket(ticket_dir)
        ledger.set_ticket_texts(
            attempt_id, parsed.edge_claim_md, parsed.hypothesis_md, parsed.manifest_md
        )

        if not parsed.ok:
            # getattr because the codes and their reasons are additions to the documented
            # parse contract, not its mandated surface.
            _audit_ticket_invalid(ledger, attempt_id,
                                  getattr(parsed, "whole_ticket_errors", []) or [],
                                  getattr(parsed, "error_detail", []) or [])
            ledger.transition(attempt_id, "ticket_invalid")
            return "ticket_invalid"

        validated = validate_ticket(
            parsed,
            market_fetch=client.get_market,
            book_fetch=client.get_orderbook,
            settings=settings,
            attempt_id=attempt_id,
            now=now,
        )
        # No clock is passed: execute_attempt reads its own at placement time. `now` is
        # the attempt's START clock, up to an hour and a half stale by the time a session
        # ends, and using it for placed_at while the caps charge the current day is
        # exactly the AE-4 split.
        outcome = execute_attempt(attempt_id, validated, ledger, client, settings)
    except Exception as exc:  # noqa: BLE001 - record and fail; never propagate
        _fail_running(ledger, attempt_id, exc, source="intake")
        return "failed"

    status = "placed" if outcome == "placed" else "no_bets"
    ledger.transition(attempt_id, status)
    return status


# --------------------------------------------------------------------------- public
def run_attempt(
    ledger,
    client,
    settings,
    *,
    model: str | None = None,
    effort: str | None = None,
    variant: str | None = None,
    slot: str | None = None,
    now=None,
    cell: str | None = None,
    cell_forced: bool = False,
) -> str:
    """Run one attempt end-to-end; returns its ``attempt_id``. Never propagates
    validate/execute exceptions (they are recorded and the attempt is ``failed``).

    ``cell`` is what the attempt reads first (docs/22 section 5.2). ``None`` means "ask
    the day's plan", which is what the tick's spawns do through the slot key; an attempt
    with no slot and no cell runs static, the cell that needs nothing but the ledger.
    ``cell_forced`` marks an operator's ``--cell`` so the plan's own statistics can leave
    it out.

    ``model`` and ``effort`` are the arm (2026-09-26). Each one left ``None`` comes from
    the slot's entry in the day's plan, or from ``attempt.model`` and ``attempt.effort``
    when there is no slot.
    """
    now = now or utc_now()
    planned_model, planned_effort = cells.arm_for_slot(ledger, settings, slot)
    model = model or planned_model
    effort = effort or planned_effort
    env = settings.kalshi.env
    if cell is None:
        cell = cells.cell_for_slot(ledger, settings, slot) if slot else "static"
    if cell not in cells.CELLS:
        raise ValueError(f"cell must be one of {cells.CELLS}, got {cell!r}")

    prompt_version = _prompt_version()
    toolkit_version = _toolkit_version()

    # The substitution switch (2026-08-18). The row and the launch audit carry the model
    # the session runs; the marker keeps the model that was asked for beside it, and the
    # day's plan still names the arm the slot was drawn for. Absent entirely when nothing
    # was substituted, so every attempt outside the switch's window keeps a byte-identical
    # variant.
    model_subs = _model_substitutions_for(settings, model, now)
    if model_subs:
        variant = _merge_variant(variant, model_substitutions=model_subs)
    ran_model = settings.effective_model(model, now)

    # 1. Ledger row (created). attempt_id is derived from the autoincrement seq, so the
    #    workspace path is patched in once it is known. ``memory_mode`` and ``priors_mode``
    #    keep meaningful values for the old queries: off for the baseline cell, on for
    #    every other (docs/22 section 4.1).
    memory_mode = "off" if cell == "baseline" else "on"
    _seq, attempt_id = ledger.create_attempt(
        env=env,
        model=ran_model,
        effort=effort,
        memory_mode=memory_mode,
        prompt_version=prompt_version,
        toolkit_version=toolkit_version,
        workspace_path="",
        slot=slot,
        variant=variant,
        era=settings.history.current_era,
        cell=cell,
        cell_forced=int(bool(cell_forced)),
    )
    attempt_dir = settings.attempts_dir / attempt_id
    workspace_dir = attempt_dir / "workspace"
    ticket_dir = attempt_dir / "ticket"
    logs_dir = attempt_dir / "logs"
    for d in (workspace_dir, ticket_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)
    ledger.update_attempt_fields(
        attempt_id,
        workspace_path=str(attempt_dir),
        loop_mode="one",
        priors_mode=memory_mode,
    )

    session_id = str(uuid4())
    ledger.update_attempt_fields(attempt_id, claude_session_id=session_id)
    ledger.transition(attempt_id, "running")

    # 2. Everything past the ``running`` transition is wrapped (AE-2). Before this there
    #    was NO path out of ``running`` when the harness itself died — a Popen failure, a
    #    ledger error, a Ctrl-C, a host that went to sleep — and the row sat there
    #    forever: never settled, never counted, invisible in every report that keys off a
    #    terminal status. The exception still propagates (the child process must exit
    #    nonzero and leave its traceback in the spawn log); what changes is that the
    #    record is terminal before it does.
    try:
        # The cell's rendering: CONTEXT.md, the two placeholders, and the record of what
        # this attempt was actually shown. ``cell_effective`` is where a director or
        # focused slot lands when no director run has validated yet. It runs INSIDE the
        # wrapper and after the transition, because a render that raises (a malformed
        # stored page, an unreadable ledger) used to leave the row in ``created``, which
        # no reaper sweeps and no report counts.
        (context_md, memory_section, priors_section, example_ids, direction_hash,
         cell_effective) = cells.render(ledger, settings, cell)
        if context_md is not None:
            (attempt_dir / "CONTEXT.md").write_text(context_md, encoding="utf-8")
        ledger.set_context_record(
            attempt_id,
            context_pack_hash=_sha12(context_md.encode("utf-8")) if context_md else None,
            example_ids=json.dumps(example_ids),
            direction_hash=direction_hash,
            cell_effective=cell_effective,
        )
        if priors_section:
            # The hash covers the page text, not the separator the placeholder carries, so
            # two attempts that read the same page hash the same however the block is
            # spaced.
            variant = _merge_variant(
                variant,
                principles_hash=_sha12(priors_section.strip().encode("utf-8")),
            )
            ledger.update_attempt_fields(attempt_id, variant=variant)
        elif cell_effective != "baseline":
            ledger.audit("principles_missing", attempt_id=attempt_id,
                         detail={"path": str(Path(settings.root) / "docs" / "principles.md"),
                                 "cell": cell_effective})

        # Prompt substitutions (docs/22 section 5.2, appendix A). The wall clock and budget
        # are deliberately absent from every rendered prompt: they are silent safeguards
        # (decision 2026-07-29), not constraints the model should optimize around.
        subs = {
            "attempt_id": attempt_id,
            "env": env,
            "window_hours": str(settings.limits.max_resolve_hours),
            "memory_section": memory_section,
            "priors_section": priors_section,
        }
        ledger.audit(
            "attempt_launched",
            attempt_id=attempt_id,
            detail={"slot": slot, "model": ran_model, "effort": effort, "cell": cell,
                    "cell_effective": cell_effective, "session_id": session_id,
                    "example_ids": example_ids, "direction_hash": direction_hash,
                    # The one event an operator reads to answer "what did this attempt
                    # run?" (2026-08-18). ``model`` and ``effort`` are what runs; while the
                    # switch is on, this names the model that was asked for beside it.
                    "model_substitutions": model_subs or None},
        )
        (attempt_dir / "TASK.md").write_text(
            _stamp_clock(_render_task(_template_bytes().decode("utf-8"), subs)),
            encoding="utf-8",
        )
        result = _launch(
            ledger, settings, attempt_id=attempt_id, kind="attempt", session_id=session_id,
            cwd=workspace_dir, model=model, effort=effort,
            max_turns=settings.attempt.max_turns,
            wall_time_s=settings.attempt.wall_time_min * 60,
            budget=settings.attempt.max_budget_usd, attempt_dir=attempt_dir,
            stream_stem="session", past_mode=memory_mode,
        )
        ledger.set_session_result(
            attempt_id,
            claude_session_id=result.session_id,
            # docs/22 section 12: the exit kind is the exit kind. The kill-versus-timeout
            # ambiguity is recorded beside it in ``variant`` rather than folded into it,
            # which is what used to misclassify a killed session as a clean one.
            session_exit=result.exit_kind,
            num_turns=result.num_turns,
            cost_usd=result.cost_usd,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            wall_seconds=result.wall_seconds,
        )
        terminal_reason = getattr(result, "terminal_reason", None)
        if terminal_reason:
            variant = _merge_variant(variant, terminal_reason=terminal_reason)
            ledger.update_attempt_fields(attempt_id, variant=variant)
        # The session's closing paragraph, which the prompt asks for by name. It is the
        # attempt's own account of what it did, and it is indexed so a later search over
        # the history finds it beside the claim and the hypothesis.
        ledger.set_session_summary(attempt_id, result.result_text)
        if result.result_text:
            ledger.fts_upsert(attempt_id, "closing", result.result_text)
        ledger.audit(
            "session_end",
            attempt_id=attempt_id,
            detail={
                "exit_kind": result.exit_kind,
                "terminal_reason": terminal_reason,
                "cost_usd": str(result.cost_usd) if result.cost_usd is not None else None,
                "num_turns": result.num_turns,
            },
        )
        # Schema 008. Before ``_intake`` on purpose: the session has finished and its
        # ticket is on disk, and intake is the step that can end the run early.
        record_activity(ledger, settings, attempt_id, result.wall_seconds)
        _intake(ledger, client, settings, attempt_id=attempt_id, ticket_dir=ticket_dir,
                exit_kind=result.exit_kind, now=now, session_result=result)
    except (Exception, KeyboardInterrupt) as exc:
        # KeyboardInterrupt included on purpose: an operator's Ctrl-C is one of the two
        # ways this process dies mid-attempt, and it is the one we can still record.
        _fail_running(ledger, attempt_id, exc, source="run_attempt")
        raise
    return attempt_id
