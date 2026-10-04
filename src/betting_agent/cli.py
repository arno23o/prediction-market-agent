"""``betting-agent`` — the operator CLI and scheduler (spec §15; Jul29 spec L10, L17).

Owns the deterministic tick loop (settle → reap → live genesis → reconcile → invariants →
backup → gc → board → cell plan → director → slots → digest), manual launches, migrations,
safety toggles, and status. ``tick`` never runs an attempt or a board pull inline: it
spawns the subcommand DETACHED (``python -m betting_agent.cli attempt --slot … --cell …``)
and the child owns its own lock for the duration.

The HALT gates betting, not the machine (docs/22 section 7.1). A tick under HALT still
settles, reconciles, checks its invariants, backs up and writes its digest; the three
places that actually stop are ``_may_spawn`` here, ``_real_order`` in ``execute.py`` and
``real_orders_allowed`` in ``safety.py``. The old early exit at the top of ``tick`` meant
a halted system stopped writing down what the exchange did to the money it already had,
which is the opposite of what a person wants from a stop button.

Locks (spec §9.1): ``tick.lock`` serializes ticks; ``attempt.lock`` serializes attempts;
``board.lock`` serializes board pulls. All use non-blocking ``flock``: a busy lock is not
an error, it is a no-op.

``tick.lock`` also fences the manual twins of the tick's own steps (``settle``,
``reconcile``) against the tick that is running them right now
(ST-8), and ``migrate`` HOLDS both ``attempt.lock`` and ``tick.lock`` for its duration
rather than probing and letting go (OR-3/ST-9).

Cell assignment (docs/22 section 8.7) lives in ``harness/cells.py``, not here: the tick
draws one permutation of the configured cell counts a day, stores it, and each slot runs
the cell at its own index. An explicit ``--cell`` with no slot is an operator-forced run
that bypasses the plan and says so on the row. The same stored plan gives each slot its
arm, a model and an effort, which the tick passes as ``--model`` and ``--effort``.
"""

from __future__ import annotations

import fcntl
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from betting_agent.board import refresh_board_cache
from betting_agent.config import (
    EFFORTS,
    config_warnings,
    config_warnings_message,
    load_settings,
    unknown_keys,
    unknown_keys_message,
)
from betting_agent.harness import cells
from betting_agent.harness.activity import activity_row, format_activity, stream_paths
from betting_agent.harness.attempt import run_attempt
from betting_agent.harness.digest import status_digest, status_log_line
from betting_agent.harness.director import (
    cohort_date_for,
    cohort_still_running,
    run_director,
)
from betting_agent.harness.invariants import run_invariants
from betting_agent.harness.notify import record_failure, record_success, streak_state
from betting_agent.harness.reconcile import (
    reconcile_once,
    record_balance_adjustment,
)
from betting_agent.harness.safety import (
    clear_halt,
    halt_reason,
    is_halted,
    real_orders_allowed,
    set_halt,
)
from betting_agent.harness.settle import genesis_snapshot, settle_once
from betting_agent.ledger.db import Ledger, LedgerError, safe_migrate
from betting_agent.moneymath import D, q4
from betting_agent.timeutil import ET, et_day, iso, parse_iso, slot_datetimes, utc_now

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="betting-agent — operator CLI and scheduler.",
)

# How many times one ET night may defer its reconciliation before it runs regardless
# (WP1 item 1c). At a 15-minute tick that is an hour of grace for a late settlement,
# after which a genuine problem still HALTs before morning.
_MAX_RECONCILE_DEFERRALS = 4

# Slot lifecycle markers (OR-2/ST-5, docs/14 D8). ``meta[slot:<day>/<hh:mm>]`` holds
# exactly one of:
#   ``pending:<iso ts>``  — consumed for a spawn; no child has confirmed it is running
#   ``running:<id>``      — a child holds ``attempt.lock`` (``pid-<n>`` until the row exists)
#   ``failed:<id>``       — an attempt ran and FAILED inside the window; re-offerable (D8)
#   ``skipped``           — the slot's grace window closed before it was ever consumed
#   ``lost``              — consumed, never ran, and the window closed (``slot_spawn_lost``)
# A pre-WP3 ledger holds flat ``pending``/``skipped`` values with no clock. They match
# none of the prefixes below, so they are inert: never re-offered, never audited lost.
# That is the right answer for a slot whose day is long over.
_SLOT_PENDING = "pending:"
_SLOT_RUNNING = "running:"
_SLOT_FAILED = "failed:"
_SLOT_LOST = "lost"

# How long a ``pending`` slot waits for its child to take ``attempt.lock`` before the
# slot may be offered again. Generous next to a Python start-up (seconds) and short next
# to the configured grace window (``schedule.slot_grace_min`` — 90 minutes as of docs/14
# A1), so a slot lost to a failed spawn is recovered inside the window rather than
# silently burned.
_SPAWN_GRACE = timedelta(minutes=3)

# Stale-``running`` reaper (ST-7). The multiplier and the flat half-hour come from the
# spec; the budget they apply to is derived from settings at call time.
_STALE_RUNNING_FACTOR = 1.5
_STALE_RUNNING_SLACK = timedelta(minutes=30)

# docs/14 D9 (docs/12 §9.10/§8.13's sleep labeling). launchd fires the tick every 15
# minutes (ops/com.betting-agent.tick.plist StartInterval=900); a gap this much larger
# between two consecutive ticks means ticks themselves stopped landing on schedule, not
# merely that one slot missed for some other reason. Twice the interval is comfortably
# past ordinary jitter (a slow step, launchd's own scheduling slop) while staying well
# under the grace window, so it cleanly separates the Aug-1 shape (a ~12.4h sleep burned
# three slots in one catch-up tick) from an awake host that missed a slot some other way.
_SLEEP_GAP_MIN = 30

# docs/22 section 5.1: how long the director step waits for a still-running attempt of the
# previous day before it goes ahead without it.
_DIRECTOR_WAIT_MIN = 60

# How often the shared-account scan re-reads the WHOLE post-genesis order history instead
# of its incremental window (EF-2). Belt to the incremental scan's braces: the 24-hour
# overlap covers late-arriving orders, this covers everything else.
_FULL_SCAN_EVERY_DAYS = 7

# Retention (ST-11/EF-6, decision D4: COMPRESS, NEVER DELETE). Runs monthly.
_GC_EVERY_DAYS = 30
_GC_STREAM_AGE = timedelta(days=7)     # session NDJSON streams
_GC_SPAWN_LOG_AGE = timedelta(days=90) # detached-spawn console logs

# The gc allowlist, as patterns rather than judgement. Only files whose NAME matches one
# of these, inside the configured attempts/logs directories, are ever touched. Everything
# else on disk — the ledger, its backups, HALT, every dated report — is unreachable from
# here by construction, which is the property that matters when the operation is
# "rewrite this file".
_GC_STREAM_SUFFIX = ".stream.jsonl"
_GC_SPAWN_LOG_RE = re.compile(r"^(?:attempt-spawn-.+|deep-review-spawn)\.log$")


# --------------------------------------------------------------------------- wiring
# One stderr warning per process per channel, however many times ``_settings()`` is called.
_CONFIG_WARNED = False
_SCHEDULE_WARNED = False


def _audit_once_per_day(settings, event: str, meta_key: str, detail: dict) -> None:
    """Write a config diagnostic at most once per ET day; never raises.

    The tick fires 96×/day and an audit row per tick would bury the signal it is meant to
    raise. Every failure in here is swallowed — a locked, absent or read-only ledger must
    not take down the command being diagnosed, and the stderr warning still stands.
    """
    try:
        if not settings.ledger_path.exists():
            return
        today = et_day(utc_now())
        ledger = Ledger.open(settings.ledger_path)
        try:
            if ledger.meta_get(meta_key) != today:
                ledger.audit(event, detail={**detail, "et_day": today})
                ledger.meta_set(meta_key, today)
        finally:
            ledger.close()
    except Exception:  # noqa: BLE001 - locked/absent/read-only ledger: warning still stands
        pass


def _config_health(settings) -> list[str]:
    """Surface config problems without ever stopping the run (CI-1, decisions D5 / docs/14 A3).

    Two channels, each with its own stderr line (once per process) and its own
    once-per-ET-day audit event. The third surfacing used to be the daily report's health
    line; since docs/22 phase one it is the status digest, which carries the same audit
    rows:

    * unknown keys → ``config_unknown_keys``: a typo can no longer silently revert a knob
      to its default.
    * :func:`config_warnings` → ``config_health_warning``: keys that are all recognized
      but not doing what they say (arm/slot aliasing, ignored phase overrides). Kept
      separate because "unknown config keys ignored (running on defaults)" would be the
      wrong sentence about them.

    The ledger is opened *only* when something is actually wrong, so the overwhelmingly
    common clean-config path costs two dict walks and touches no files. Every failure in
    here is swallowed: this is a diagnostic, and a diagnostic must not take down the
    command it is diagnosing.
    """
    global _CONFIG_WARNED, _SCHEDULE_WARNED
    try:
        unknown = unknown_keys(settings)
    except Exception:  # noqa: BLE001 - a broken sweep must not break every command
        unknown = []
    if unknown:
        if not _CONFIG_WARNED:
            print(f"warning: {unknown_keys_message(unknown)}", file=sys.stderr)
            _CONFIG_WARNED = True
        _audit_once_per_day(settings, "config_unknown_keys",
                            "last_config_unknown_date", {"keys": unknown})
    try:
        warnings = config_warnings(settings)
    except Exception:  # noqa: BLE001 - same rule: a diagnostic never breaks the command
        warnings = []
    if warnings:
        if not _SCHEDULE_WARNED:
            print(f"warning: {config_warnings_message(warnings)}", file=sys.stderr)
            _SCHEDULE_WARNED = True
        _audit_once_per_day(settings, "config_health_warning",
                            "last_config_warning_date", {"warnings": warnings})
    return unknown


def _settings():
    root = os.environ.get("BT_ROOT")
    settings = load_settings(root=Path(root).expanduser() if root else Path.cwd())
    _config_health(settings)
    return settings


def _client(settings):
    """Build a :class:`KalshiClient`. Monkeypatched in tests."""
    from betting_agent.kalshi.client import KalshiClient

    return KalshiClient(
        base_url=settings.kalshi.base_url,
        key_id=settings.kalshi.key_id or "",
        private_key_path=settings.kalshi.private_key_path,
    )


def _maybe_client(settings):
    """A client, or ``None`` when creds are absent (steps that need it are guarded)."""
    try:
        return _client(settings)
    except Exception:  # noqa: BLE001 - offline/no-creds is tolerated by the tick loop
        return None


# --------------------------------------------------------------------------- locks
def _acquire_lock(path: Path):
    """Non-blocking ``flock``; returns the held file object, or ``None`` when busy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")  # noqa: SIM115 - the fd must outlive this call (the lock is held)
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def _release_lock(f) -> None:
    try:
        fcntl.flock(f, fcntl.LOCK_UN)
    finally:
        f.close()


def _lock_free(path: Path) -> bool:
    """Probe ``path`` with a non-blocking exclusive lock, then release: ``True`` if free."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        f.close()


@contextmanager
def _tick_step_lock(settings, command: str):
    """Hold ``tick.lock`` for a manual run of one of the tick's own steps (ST-8).

    ``settle`` and ``reconcile`` are the same code the 15-minute tick
    runs. Racing it is not theoretical: the loser of a settle race tracebacked out
    mid-pass, and what it skipped on the way out was the impostor scan — the check that
    HALTs the system when someone else is trading on the account. Refusing loudly beats
    half a pass. The strict state machine is untouched: this is a lock, not a
    tolerant ``transition``.
    """
    lock = _acquire_lock(settings.locks_dir / "tick.lock")
    if lock is None:
        print(f"{command}: tick.lock busy; a tick is running this step right now — "
              "try again in a moment")
        raise typer.Exit(1)
    try:
        yield
    finally:
        _release_lock(lock)


# --------------------------------------------------------------------------- tick steps
# docs/14 D1. Streak names are namespaced so the generic core in ``harness/notify.py``
# never has to know what a "step" is; WP-B's insufficient-funds streak joins here as its
# own name with no change to any of this.
_SPAWN_STREAK = "attempt_spawn"
# The board pull's own streak (docs/22 section 6 item 4). It left the tick for a detached
# child, so it left the tick's per-step streak with it, and a child that dies into its own
# log file needs a way to reach a human.
_BOARD_STREAK = "board_refresh"


def _step_streak(name: str) -> str:
    return f"step:{name}"


def _settle_halt_if_unreachable(ledger, settings, streak: int) -> bool:
    """The D1(d) hard line: too many consecutive settle failures stops placement.

    ~5 hours of a settle step that cannot reach the exchange means every number the money
    path depends on — positions, fills, the balance walk — is stale, and betting on a
    stale picture is the failure mode "never fall back silently" exists to prevent. The
    streak resets as the HALT is written, so ``resume`` gives the system a fresh window
    instead of re-halting on the very next tick.
    """
    limit = int(getattr(settings.alerts, "settle_halt_streak", 20))
    if limit <= 0 or streak < limit or is_halted(settings):
        return False
    set_halt(settings, "settle_unreachable")
    try:
        ledger.audit("halt_set", detail={
            "reason": "settle_unreachable", "consecutive_settle_failures": streak,
        })
    except Exception:  # noqa: BLE001 - the HALT is what matters; its audit row is not
        pass
    record_success(ledger, _step_streak("settle"))  # re-arm for the post-resume window
    print(f"tick: HALT set — settle failed {streak} consecutive ticks (settle_unreachable)")
    return True


def _note_step_failure(ledger, settings, name: str, error: str) -> None:
    """One failed step: count it toward its streak, alert once, HALT on the settle line."""
    streak = _step_streak(name)
    n = streak_state(ledger, streak)["n"] + 1
    record_failure(
        ledger, settings, streak,
        threshold=max(1, int(getattr(settings.alerts, "step_failure_streak", 3))),
        title="betting-agent: tick step failing",
        message=f"step '{name}' has failed {n} consecutive ticks — {error}",
        detail={"step": name, "error": error},
    )
    if name == "settle":
        _settle_halt_if_unreachable(ledger, settings, n)


def _step(ledger, name: str, fn, outcomes: dict | None = None, settings=None,
          success_when=None):
    """Run one tick step; a failure logs + audits but never kills the rest of the tick.

    ``outcomes`` (when supplied) records ``"ok"``/``"errored"`` per step name, in process:
    later steps in the SAME tick read it, which is how the reconcile step learns that this
    tick's settle pass failed (MP-1 item 1b). It is deliberately not persisted — the
    signal is about this pass, and the next tick gets a fresh one.

    ``settings`` (when supplied) switches on the D1 escalation: the *persisted* streak,
    which is the part that has to survive across ticks, because one audit row per tick
    forever is exactly what nobody read for three days (docs/12 §8.1). Streak bookkeeping
    is best-effort by construction — a step's isolation is not allowed to depend on it.
    """
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - steps are independent; isolate failures
        print(f"tick: step '{name}' failed: {type(exc).__name__}: {exc}")
        try:
            ledger.audit("tick_step_error",
                         detail={"step": name, "error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001 - never let audit failure mask the step failure
            pass
        if settings is not None:
            try:
                _note_step_failure(ledger, settings, name, f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001 - escalation must not become the failure
                pass
        if outcomes is not None:
            outcomes[name] = "errored"
        return None
    # ``success_when`` narrows what re-arms the streak (docs/14 D1 follow-through): a step
    # that returned but reports in-band partial failure is NEUTRAL — it neither advances
    # the streak (a delisted market is not an unreachable exchange, so it must never feed
    # the settle HALT) nor resets it (a wedge that surfaces in-band every tick must not
    # hold the streak at zero while nothing works).
    step_ok = True
    if success_when is not None:
        try:
            step_ok = bool(success_when(result))
        except Exception:  # noqa: BLE001 - a broken predicate must not fail the step
            step_ok = True
    if settings is not None and step_ok:
        try:
            record_success(ledger, _step_streak(name))
        except Exception:  # noqa: BLE001 - a step that worked stays worked
            pass
    if outcomes is not None:
        outcomes[name] = "ok"
    return result


def _stale_running_after(settings) -> timedelta:
    """How long an attempt may sit in ``running`` before the reaper calls it dead (ST-7).

    Derived from settings at call time, never hardcoded: raising
    ``attempt.wall_time_min`` must move this bound with it, or the reaper starts eating
    live attempts. The budget it applies to is the largest an *attempt row* can honestly
    consume, which is now one session's wall clock: the two-loop attempt, whose three
    phases used to make the honest maximum 6.3 h, is gone (docs/22 section 2.1).
    """
    budget = timedelta(minutes=settings.attempt.wall_time_min)
    return budget * _STALE_RUNNING_FACTOR + _STALE_RUNNING_SLACK


def _reap_stale_running(ledger, settings, now: datetime) -> dict:
    """Fail attempts stranded in ``running`` past any honest duration (ST-7).

    The in-process guard (AE-2) covers every crash the harness lives to see. This covers
    the two it cannot: SIGKILL, and this machine going to sleep mid-attempt — after
    which the row stays ``running`` forever. Forever is not an exaggeration: nothing
    anywhere queried that status, so stale rows accumulate silently and are missing from
    every population keyed on a terminal status (settlement, and every `bt past` lens).

    Deliberately conservative. It only ever moves ``running`` -> ``failed``, which is a
    legal transition; the bound is hours past the worst legitimate case; and a row it
    cannot read a timestamp for is left alone rather than guessed at.
    """
    bound = _stale_running_after(settings)
    cutoff = now - bound
    reaped, errors = [], 0
    for row in ledger.attempts_by_status("running"):
        attempt_id = row["attempt_id"]
        try:
            started = parse_iso(row["created_at"])
        except (ValueError, TypeError):
            continue  # unreadable clock: leave it for a human, never guess
        if started > cutoff:
            continue
        try:  # per-row isolation (MP-2's lesson): one bad row is not a failed pass
            ledger.update_attempt_fields(attempt_id, error="stale_running_reaped")
            ledger.transition(attempt_id, "failed")
            ledger.audit("stale_running_reaped", attempt_id=attempt_id, detail={
                "created_at": row["created_at"],
                "age_hours": round((now - started).total_seconds() / 3600, 2),
                "bound_hours": round(bound.total_seconds() / 3600, 2),
            })
        except Exception as exc:  # noqa: BLE001 - report it, keep reaping
            errors += 1
            print(f"tick: could not reap {attempt_id}: {type(exc).__name__}: {exc}")
            continue
        reaped.append(attempt_id)
    if reaped:
        print(f"tick: reaped {len(reaped)} stale running attempt(s): {', '.join(reaped)}")
    return {"reaped": reaped, "errors": errors}


def _days_since(ledger, key: str, now: datetime) -> int | None:
    """Whole ET days since ``meta[key]`` was stamped; ``None`` when never/unreadable."""
    last = ledger.meta_get(key)
    if not last:
        return None
    try:
        return (date.fromisoformat(et_day(now)) - date.fromisoformat(str(last))).days
    except (ValueError, TypeError):
        return None


def _full_scan_due(ledger, now: datetime) -> bool:
    """True when the shared-account scan should re-read the whole history (EF-2)."""
    elapsed = _days_since(ledger, "last_full_scan_date", now)
    return elapsed is None or elapsed >= _FULL_SCAN_EVERY_DAYS


def _gzip_in_place(path: Path) -> bool:
    """Compress ``path`` to ``path.gz``. True iff this call did the work.

    Idempotent in both directions: an existing ``.gz`` is left alone (never overwritten,
    so a re-run cannot clobber an earlier compression), and the original is only removed
    once its compressed twin is completely written and renamed into place. A crash
    mid-write leaves the original plus a ``.tmp`` and loses nothing.
    """
    target = path.with_name(path.name + ".gz")
    if target.exists():
        return False
    tmp = path.with_name(path.name + ".gz.tmp")
    try:
        with open(path, "rb") as src, gzip.open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        return False
    path.unlink(missing_ok=True)
    return True


def _gc_candidates(settings, now: datetime) -> list[Path]:
    """Every file the retention pass is allowed to compress, and nothing else.

    Two populations, both name-matched and both confined to configured directories:
    ``*.stream.jsonl`` under ``attempts_dir`` past a week, and the detached-spawn console
    logs directly under ``logs_dir`` past a quarter. A file is a candidate only if it
    matches a pattern AND lives under the right root AND is old enough — three
    independent conditions, so a stray path cannot satisfy the set by accident.
    """
    out: list[Path] = []

    def _old_enough(path: Path, age: timedelta) -> bool:
        try:
            return (now.timestamp() - path.stat().st_mtime) >= age.total_seconds()
        except OSError:
            return False

    attempts_root = settings.attempts_dir
    if attempts_root.is_dir():
        for path in sorted(attempts_root.rglob(f"*{_GC_STREAM_SUFFIX}")):
            if not path.is_file() or path.name.endswith(".gz"):
                continue
            if _old_enough(path, _GC_STREAM_AGE):
                out.append(path)

    logs_root = settings.logs_dir
    if logs_root.is_dir():
        for path in sorted(logs_root.glob("*.log")):
            if not path.is_file() or not _GC_SPAWN_LOG_RE.match(path.name):
                continue
            if _old_enough(path, _GC_SPAWN_LOG_AGE):
                out.append(path)
    return out


def _gc_if_due(ledger, settings, now: datetime) -> dict | None:
    """Monthly retention pass: COMPRESS old logs, delete nothing (ST-11/EF-6, D4).

    Disk was the one resource nothing pruned: multi-megabyte session streams per attempt,
    a console log per spawn, forever. Arno's ruling was explicit — gzip is fine, deletion
    is not — so this only ever rewrites a file as its own ``.gz``. Nothing is removed,
    nothing outside :func:`_gc_candidates`' allowlist is opened, and the whole pass is
    idempotent, so a half-finished run simply finishes next month.
    """
    elapsed = _days_since(ledger, "last_gc_date", now)
    if elapsed is not None and elapsed < _GC_EVERY_DAYS:
        return None
    compressed = 0
    freed = 0
    for path in _gc_candidates(settings, now):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if _gzip_in_place(path):
            compressed += 1
            freed += size
    ledger.meta_set("last_gc_date", et_day(now))
    result = {"compressed": compressed, "bytes_before": freed}
    if compressed:
        ledger.audit("gc_compressed", detail=result)
        print(f"tick: gc compressed {compressed} log file(s)")
    return result


def _backup_if_stale(ledger, settings, now: datetime) -> None:
    last = ledger.meta_get("last_backup_ts")
    stale = True
    if last:
        try:
            stale = (now - parse_iso(last)) > timedelta(hours=24)
        except (ValueError, TypeError):
            stale = True
    if stale:
        ledger.backup(settings.backups_dir)
        ledger.meta_set("last_backup_ts", iso(now))
        ledger.audit("backup_done", detail={"ts": iso(now)})


def _slot_key(slot_dt: datetime) -> str:
    return f"slot:{slot_dt.strftime('%Y-%m-%d')}/{slot_dt.strftime('%H:%M')}"


def _note_spawn_failure(ledger, settings, slot_key: str, why: str) -> None:
    """Count one attempt-spawn failure toward the D1(b) streak; never raises.

    The counterpart success is stamped by the child itself (in :func:`attempt`, the moment
    it holds ``attempt.lock``), because that — not ``Popen`` returning — is what "the spawn
    worked" actually means.
    """
    try:
        n = streak_state(ledger, _SPAWN_STREAK)["n"] + 1
        record_failure(
            ledger, settings, _SPAWN_STREAK,
            threshold=max(1, int(getattr(settings.alerts, "spawn_failure_streak", 3))),
            title="betting-agent: attempts are not starting",
            message=f"{n} consecutive attempt spawns failed ({slot_key}): {why}",
            detail={"slot": slot_key, "why": why},
        )
    except Exception:  # noqa: BLE001 - escalation must never break slot bookkeeping
        pass


def _spawn(settings, args: list[str], log_stem: str) -> None:
    """Spawn ``python -m betting_agent.cli <args>`` DETACHED, logging to ``logs/``."""
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    log = open(settings.logs_dir / f"{log_stem}.log", "ab")
    argv = [sys.executable, "-m", "betting_agent.cli", *args]
    try:
        subprocess.Popen(
            argv,
            start_new_session=True,
            stdout=log,
            stderr=subprocess.STDOUT,
            cwd=str(settings.root),
            env={**os.environ, "BT_ROOT": str(settings.root)},
        )
    finally:
        log.close()  # the child keeps its own dup of the fd


def _spawn_attempt(settings, slot_key: str, cell: str, model: str, effort: str) -> None:
    """Spawn the attempt subcommand DETACHED; the child owns ``attempt.lock``.

    The cell and the arm come from the day's stored plan and are passed explicitly rather
    than left to the child to look up, so the spawn log says what was launched even if the
    plan is read again later. The model is the configured name: the child resolves it
    through the substitution switch like every other session.
    """
    args = ["attempt", "--slot", slot_key, "--cell", cell, "--model", model,
            "--effort", effort]
    sanitized = slot_key.replace(":", "-").replace("/", "-")
    _spawn(settings, args, f"attempt-spawn-{sanitized}")


def _slot_start(slot_key: str | None) -> datetime | None:
    """The ET launch time a ``slot:<day>/<hh:mm>`` key names; ``None`` if unreadable.

    The exact inverse of :func:`_slot_key`. Unreadable keys (a manual run's ``None``, a
    hand-edited marker) return ``None`` and every caller treats that as "no window".
    """
    if not slot_key or not slot_key.startswith("slot:"):
        return None
    try:
        day, hhmm = slot_key[len("slot:"):].split("/")
        year, month, dom = (int(p) for p in day.split("-"))
        hour, minute = (int(p) for p in hhmm.split(":"))
        return datetime(year, month, dom, hour, minute, tzinfo=ET)
    except (ValueError, TypeError):
        return None


def _slot_window_open(settings, slot_key: str | None, now: datetime) -> bool:
    """True while ``slot_key``'s grace window is still open (D8)."""
    start = _slot_start(slot_key)
    if start is None:
        return False
    return now <= start + timedelta(minutes=settings.schedule.slot_grace_min)


def _spawn_stale(value: str, now: datetime) -> bool:
    """True when a ``pending:<ts>`` marker is old enough to be re-offered (OR-2/ST-5).

    An unreadable timestamp counts as stale: a corrupt marker must not strand a slot,
    and the lock probe plus the CAS are what actually prevent a double spawn.
    """
    try:
        return (now - parse_iso(value[len(_SLOT_PENDING):])) >= _SPAWN_GRACE
    except (ValueError, TypeError):
        return True


def _past_grace_reason(ledger, now: datetime) -> str:
    """Which of D9's two ``slot_skipped`` reasons this tick's misses carry.

    ``meta.last_tick_ts`` still holds the PRIOR tick's stamp here: ``_run_slots`` runs
    before ``_tick_run`` writes this tick's own stamp (the final line of that function).
    A catch-up tick arriving long after the last one means the host, not the schedule,
    caused the miss (``past_grace_sleep``); a tick landing on the normal cadence means
    something else did -- a HALT, lock contention -- and calling that 'sleep' would
    misdiagnose it (``past_grace_other``).
    """
    last = ledger.meta_get("last_tick_ts")
    if last:
        try:
            if now - parse_iso(last) >= timedelta(minutes=_SLEEP_GAP_MIN):
                return "past_grace_sleep"
        except (ValueError, TypeError):
            pass
    return "past_grace_other"


def _run_slots(ledger, settings, now: datetime) -> None:
    """Consume today's ET slots: spawn at most one attempt; skip past-grace slots.

    The slot used to be consumed as a flat ``"pending"`` *before* the spawn could
    succeed, so every way the spawn failed to become a running attempt burned the slot
    silently and permanently: a ``Popen`` that raised, a child that lost the
    ``attempt.lock`` race to a manual run, and — since the HALT gate landed — a child
    that started and politely refused. All of them look identical from here, and all of
    them are now recoverable: the marker carries the consume time, the child overwrites
    it the moment it holds ``attempt.lock``, and a marker still reading ``pending``
    after the spawn grace is a spawn that never became an attempt.

    Three conditions gate a re-offer, and together they make a double spawn impossible:
    the marker must still read ``pending`` (compare-and-set — a child that has claimed
    the slot has already overwritten it), ``attempt.lock`` must be free (no attempt is
    running, whatever the marker says), and the slot's own grace window must still be
    open (a slot is never resurrected after its window closes). Past the window an
    unconsumed slot is ``slot_skipped`` as before, and a consumed-but-never-run slot is
    ``slot_spawn_lost`` — the event that used to be invisible.

    D8 adds one more re-offerable marker: ``failed:<attempt id>``, stamped by a child
    whose attempt ran and failed while the window was still open (an auth-dead spawn, a
    crash in the first seconds). It goes through the same CAS but skips the spawn grace —
    that grace exists to give a starting child time to take the lock, and this child has
    already come and gone. Each re-offer is a fresh attempt id and each failure is
    audited, so a slot that keeps failing keeps being re-offered until its grace expires:
    bounded by the window, and visible in the marker history rather than silent. Past the
    window the marker is simply left as it is — the attempt that failed is a fact worth
    keeping, and it is not ``lost`` (something did run).
    """
    today = now.astimezone(ET).date()
    grace = timedelta(minutes=settings.schedule.slot_grace_min)
    attempt_lock = settings.locks_dir / "attempt.lock"
    # One answer per tick (D9): whatever caused ticks to stop landing on schedule is the
    # same fact for every slot this pass discovers past grace, so it is read once rather
    # than re-derived per slot.
    skip_reason = _past_grace_reason(ledger, now)
    for slot_dt in slot_datetimes(today, settings.schedule.slots):
        slot_key = _slot_key(slot_dt)
        if now < slot_dt:
            continue  # not yet due
        marker = ledger.meta_get(slot_key)
        pending = isinstance(marker, str) and marker.startswith(_SLOT_PENDING)
        failed = isinstance(marker, str) and marker.startswith(_SLOT_FAILED)

        if now <= slot_dt + grace:
            if marker is not None and not (pending or failed):
                continue  # already ran (or was already given up on)
            if not _lock_free(attempt_lock):
                continue  # an attempt is running; the slot keeps its place in the window
            claim = f"{_SLOT_PENDING}{iso(now)}"
            if pending or failed:
                # The spawn grace applies to ``pending`` only: a ``failed`` marker is
                # written by a child that has already finished (D8).
                if pending and not _spawn_stale(marker, now):
                    continue
                if not ledger.reclaim_slot(slot_key, marker, claim):
                    continue
                ledger.audit("slot_respawned", detail={"slot": slot_key, "was": marker})
            elif not ledger.consume_slot(slot_key, claim):
                continue
            try:
                # Inside the wrapper, because the slot is already consumed by here: a
                # ledger error reading the day's plan is as much a spawn that never
                # happened as a ``Popen`` that raised, and it used to escape uncounted.
                cell = cells.cell_for_slot(ledger, settings, slot_key)
                model, effort = cells.arm_for_slot(ledger, settings, slot_key)
                _spawn_attempt(settings, slot_key, cell, model, effort)
            except Exception as exc:  # noqa: BLE001 - re-raised; counted on the way past
                _note_spawn_failure(ledger, settings, slot_key,
                                    f"spawn raised: {type(exc).__name__}: {exc}")
                raise
            break  # only one spawn per tick
        elif marker is None:
            if ledger.consume_slot(slot_key, "skipped"):
                ledger.audit("slot_skipped", detail={"slot": slot_key, "reason": skip_reason})
        elif pending and ledger.reclaim_slot(slot_key, marker, _SLOT_LOST):
            # Consumed, never ran, window closed. Includes the slot whose child refused
            # because a HALT was up: a loss is a loss, and it is recorded as one. A
            # ``failed:`` marker is deliberately NOT swept here (D8): an attempt did run,
            # it is already audited, and overwriting the id would erase which one.
            ledger.audit("slot_spawn_lost", detail={"slot": slot_key, "was": marker})
            # D1(b): a spawn that never became an attempt is the OAuth-death shape from
            # docs/12 §6 — indistinguishable from normal operation unless someone reads
            # the ledger. Streaked and escalated like any other repeated failure.
            _note_spawn_failure(ledger, settings, slot_key, "spawn never became an attempt")


def _live_genesis_if_due(ledger, client, settings, now: datetime) -> None:
    """Stamp the live era's genesis, once (spec L7).

    The first tick that finds the live gate open and no ``meta.live_genesis_ts`` records
    the timestamp and the account balance, the anchor every reconciliation walks from and
    the base of the drawdown floor. The tick itself runs under a halt since docs/22
    section 7.1, but this step does not: ``real_orders_allowed`` reports the halt as its
    first reason and closes the gate below. In the live ledger the first stamp landed
    after the go-live canary, so the genesis balance is the post-canary balance and that
    canary's payout is tracked separately from ``meta.canary`` (spec §7/§8).
    """
    if client is None or ledger.meta_get("live_genesis_ts") is not None:
        return
    allowed, _why = real_orders_allowed(settings, ledger)
    if not allowed:
        return
    balance = q4(D(str(client.get_balance().dollars)))
    # OR-5: one transaction for both keys. As two, a crash in between left the timestamp
    # without the balance — a half-stamp that fails every subsequent reconciliation (the
    # walk has an anchor date and no anchor balance) and never retries, because the
    # timestamp's presence is exactly what says "already stamped".
    ledger.meta_set_many({
        "live_genesis_ts": iso(now),
        "live_genesis_balance": str(balance),
    })
    ledger.audit("live_genesis", detail={"ts": iso(now), "balance": str(balance)})


def _reconcile_deferrals(ledger, today: str) -> int:
    """How many times reconciliation has already been deferred tonight (0 on a new day)."""
    raw = ledger.meta_get("reconcile_deferrals")
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return 0
    if not isinstance(data, dict) or data.get("date") != today:
        return 0
    try:
        return int(data.get("n", 0))
    except (TypeError, ValueError):
        return 0


def _defer_reconcile(ledger, today: str, reason: str, n: int, detail: dict | None = None):
    """Record one deferral (no date stamp — the night stays open) and audit it."""
    ledger.meta_set("reconcile_deferrals", json.dumps({"date": today, "n": n + 1}))
    ledger.audit("reconcile_deferred", detail={
        "reason": reason, "deferrals": n + 1, "max": _MAX_RECONCILE_DEFERRALS,
        **(detail or {}),
    })
    return {"deferred": reason, "deferrals": n + 1}


def _reconcile_if_due(ledger, client, settings, now: datetime, *,
                      settle_errored: bool = False):
    """Nightly balance reconciliation (spec L8): after ``reconcile.hour_et``, once a day.

    ``reconcile_once`` does not stamp the meta key — this gate owns it. **Stamping rule
    (WP1 item 1c):** only a run that COMPLETED stamps ``last_reconcile_date`` — a pass or
    a HALT. A skip, a deferral, or a raised error leaves the day unstamped, so the same
    night can retry; before this, a failed run stamped the day and killed its own retry.

    Three reasons to defer instead of run, each a false-HALT source found in review:

    * this tick's settle pass errored (MP-1 1b) — it may not have written down a
      settlement the exchange has already paid;
    * ``attempt.lock`` is held (ST-4) — a detached attempt may be mid-placement, and a
      balance read that straddles an order landing is drift that is not drift;
    * the run came back ``provisional`` (D1's attribution gate in ``reconcile.py``).

    Deferrals are bounded to ``_MAX_RECONCILE_DEFERRALS`` per ET day. Past the bound the
    reconciliation runs anyway with the provisional gate OFF: a real problem must still
    HALT before morning, and "defer" must never become "never".
    """
    if client is None:
        return None
    if now.astimezone(ET).hour < settings.reconcile.hour_et:
        return None
    today = et_day(now)
    if ledger.meta_get("last_reconcile_date") == today:
        return None

    deferrals = _reconcile_deferrals(ledger, today)
    forced = deferrals >= _MAX_RECONCILE_DEFERRALS
    if not forced:
        if settle_errored:
            return _defer_reconcile(ledger, today, "settle_errored", deferrals)
        if not _lock_free(settings.locks_dir / "attempt.lock"):
            return _defer_reconcile(ledger, today, "attempt_in_progress", deferrals)

    result = reconcile_once(ledger, client, settings, now=now,
                            allow_provisional=not forced)
    if not isinstance(result, dict) or result.get("skipped"):
        return result
    if result.get("provisional"):
        _defer_reconcile(ledger, today, "provisional", deferrals)
        return result
    ledger.meta_set("last_reconcile_date", today)
    return result


def _invariants_step(ledger, settings) -> list[dict]:
    """Run the six ledger invariants and alert on each failure (docs/22 section 10).

    Every tick, not on a cadence: the checks are cheap SQL over indexed columns, and the
    thing they catch, a bet with no attempt, a money column that is not 4dp, a settled
    row with no outcome, is a corruption that gets harder to reason about the longer it
    stands. One alert key per check, so a check that keeps failing keeps its own streak
    rather than being folded in with the others.
    """
    rows = run_invariants(ledger)
    for row in rows:
        # Spec 10 says "raises an alert", which read literally is ``raise_alert`` and one
        # banner every fifteen minutes for as long as the defect stands. One orphan row
        # would be ninety-six notifications a day, which is the noise docs/12 §8.1 exists
        # to stop. The streak core is the same escalation at threshold one: the first
        # failing tick alerts, the rest are counted, and the first passing tick re-arms.
        if row["ok"]:
            record_success(ledger, f"invariant:{row['name']}")
            continue
        record_failure(
            ledger, settings, f"invariant:{row['name']}", threshold=1,
            title="betting-agent: a ledger invariant is failing",
            message=f"{row['name']}: {row['detail']}",
            detail={"invariant": row["name"], "failing": row["detail"]},
        )
    return rows


def _board_if_due(ledger, client, settings, now: datetime) -> dict:
    """Spawn ``board-refresh`` DETACHED when the cache is stale (docs/22 section 6 item 4).

    The pull is minutes of wall clock against a 15-minute tick, and it used to run inline,
    so a slow board pull delayed settlement, reconciliation and every slot behind it. The
    tick now only decides whether a pull is due and hands it to a child that owns
    ``board.lock`` for its duration. It never waits: the next tick sees the newer
    generation, or sees the lock still held and does nothing.

    No client, no child. Without credentials the pull is a no-op the child announces and
    exits on, so spawning one every fifteen minutes would be a process and a log line an
    hour saying nothing. This is the same gate every other client-dependent step applies.

    No child under a HALT either. Nothing reads a fresh board while the system is stopped:
    no attempt runs, and the digest works off whatever snapshot is on disk. The 42-hour
    halt of 2026-09-20 spent 15 refreshes, several thousand requests, on snapshots nobody
    opened. The refresh is derived from the newest generation's capture time, not from tick
    bookkeeping, so the first tick after ``resume`` pulls a fresh one straight away.
    """
    from betting_agent.board import board_interval, cache_age

    if is_halted(settings):
        return {"status": "halted"}
    age = cache_age(settings.board_dir, now)
    if age is not None and age < board_interval(settings):
        return {"status": "fresh", "age_seconds": int(age.total_seconds())}
    if client is None:
        return {"status": "no_client"}
    if not _lock_free(settings.locks_dir / "board.lock"):
        return {"status": "busy"}
    _spawn(settings, ["board-refresh"], "board-refresh-spawn")
    return {"status": "spawned"}


def _plan_lines(settings, day: str, plan: list[dict] | None) -> str:
    """The day's cell plan, slot by slot with each slot's arm, for ``plan`` and ``status``."""
    if not plan:
        return f"cell plan {day}: not drawn yet"
    rows = [f"cell plan {day}:"]
    rows += [
        f"  {hhmm}  {entry['cell']:<8}  {entry['model']} {entry['effort']}"
        for hhmm, entry in zip(settings.schedule.slots, plan, strict=False)
    ]
    return "\n".join(rows)


def _director_line(ledger) -> str:
    """The latest director run, for ``betting-agent status`` (docs/22 section 8.2)."""
    row = ledger.latest_director_run()
    if row is None:
        return "director: no run yet"
    page = f"  page {row['page_hash']}" if row["page_hash"] else ""
    return f"director {row['run_date']}: {row['status']}{page}"


def _cell_plan_if_due(ledger, settings, now: datetime) -> dict:
    """Draw the day's cell plan once, before any slot reads it (docs/22 section 8.7).

    The first tick of an Eastern day stores the permutation, so ``betting-agent status``
    and ``betting-agent plan`` can print the whole day in advance and every slot reads the
    same draw. ``_run_slots`` would draw it on demand anyway; doing it here is what makes
    the plan knowable before the first attempt of the day runs.
    """
    day = et_day(now)
    drawn = ledger.cell_plan(day) is None
    return {"day": day, "drawn": drawn, "plan": cells.plan(ledger, settings, day)}


def _director_due_at(settings, now: datetime) -> datetime:
    """The Eastern instant today's director run becomes due (docs/22 section 8.2)."""
    hour, minute = settings.director.hour_minute()
    today = now.astimezone(ET)
    return today.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _director_if_due(ledger, settings, now: datetime) -> dict:
    """Spawn the day's director run once, DETACHED (docs/22 sections 5.1 and 8.2).

    Two clocks, one rule. From ``director.hour_et`` the run is due as soon as no attempt
    of the previous day is still running, because the review wants the whole cohort; an
    hour later it goes ahead regardless, because a wedged attempt must not cost the day
    its direction. The straggler is left out of the prospective review and is reviewed
    retrospectively with its cohort.

    The ``director_runs`` row is what makes this once a day: the child opens it before its
    session starts, and an invalid or failed run counts, because nothing is retried the
    same day. The lock probe covers the seconds between the spawn and that row.
    """
    settings.director_dir.mkdir(parents=True, exist_ok=True)
    run_date = et_day(now)
    due_at = _director_due_at(settings, now)
    if now < due_at:
        return {"status": "not_due", "run_date": run_date}
    existing = ledger.director_run_for_date(run_date)
    if existing is not None:
        return {"status": "done", "run_date": run_date, "was": existing["status"]}
    cohort_date = cohort_date_for(run_date)
    # The cohort is asked, not the slot markers: membership is the slot's day, and an
    # attempt an operator ran by hand has no slot key at all while still being a member.
    running = cohort_still_running(ledger, cohort_date)
    if running and now < due_at + timedelta(minutes=_DIRECTOR_WAIT_MIN):
        return {"status": "waiting", "run_date": run_date, "on": running}
    if not _lock_free(settings.locks_dir / "director.lock"):
        return {"status": "busy", "run_date": run_date}
    _spawn(settings, ["director", "--date", run_date], "director-spawn")
    return {"status": "spawned", "run_date": run_date, "left_running": running}


def _digest_if_due(ledger, client, settings, now: datetime) -> dict | None:
    """Write the previous Eastern day's page, once (docs/22 section 10).

    The first tick on or after 00:00 Eastern renders the day that has just **finished**,
    not the one that has just started: the archived page is the record of a completed day,
    and a page written at 00:05 about the day it names would describe five minutes. The
    account and board sections are still as of now, because a balance and a snapshot age
    are facts about this instant whatever day the page is filed under.
    ``betting-agent status`` renders today, which is the other question a person asks.

    The page goes to ``data/status/<that day>.md``, one line to ``data/logs/status.log``,
    and both ``meta.last_digest_date`` (the day written) and ``meta.last_digest_ts`` (the
    instant, which is what the page's "since the previous digest" windows read next time)
    are stamped after the render, never before.
    """
    day = et_day(now - timedelta(days=1))
    if ledger.meta_get("last_digest_date") == day:
        return None
    text = status_digest(ledger, settings, client, day=day, now=now)
    line = status_log_line(ledger, settings, day, client=client, now=now)
    settings.status_dir.mkdir(parents=True, exist_ok=True)
    (settings.status_dir / f"{day}.md").write_text(text, encoding="utf-8")
    settings.logs_dir.mkdir(parents=True, exist_ok=True)
    with open(settings.logs_dir / "status.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    ledger.meta_set_many({"last_digest_date": day, "last_digest_ts": iso(now)})
    return {"day": day, "lines": len(text.splitlines())}


def _tick_run(ledger, client, settings, now: datetime) -> None:
    outcomes: dict[str, str] = {}

    def step(name: str, fn, track: dict | None = None, success_when=None):
        """Every tick step goes through here so D1's streak escalation covers all of them."""
        return _step(ledger, name, fn, track, settings=settings, success_when=success_when)

    # EF-2: the scan is incremental on every ordinary tick and exhaustive once a week.
    # The cadence is date-keyed in meta rather than counted in process, so a restart
    # neither skips the full scan nor runs one every tick.
    full_scan = _full_scan_due(ledger, now)
    counts = step("settle",
                  lambda: settle_once(ledger, client, settings, now=now,
                                      full_scan=full_scan), outcomes,
                  success_when=lambda r: not (isinstance(r, dict) and r.get("errors")))
    # MP-2's per-bet isolation reports partial failure in-band rather than by raising, so
    # read it: a pass that swallowed a bet error (or gave up on a market) has not seen the
    # whole world, and tonight's reconciliation must defer instead of HALTing on the
    # settlement it just missed (MP-1 item 1b).
    if isinstance(counts, dict) and counts.get("errors"):
        outcomes["settle"] = "errored"
    if full_scan and isinstance(counts, dict):
        # Stamped only when the settle step actually returned: a step that raised did not
        # finish its scan, and next tick should try the full one again.
        step("full_scan_stamp",
             lambda: ledger.meta_set("last_full_scan_date", et_day(now)))

    # ST-7: right after settle, before anything reads the attempt population. A row the
    # host's sleep stranded in ``running`` is dead; nothing else in the system will ever
    # say so.
    step("reap", lambda: _reap_stale_running(ledger, settings, now))
    step("live_genesis", lambda: _live_genesis_if_due(ledger, client, settings, now))
    step("reconcile",
         lambda: _reconcile_if_due(ledger, client, settings, now,
                                   settle_errored=outcomes.get("settle") == "errored"),
         outcomes)
    # docs/22 section 10: right after reconcile, so the night's money picture is written
    # down before anything checks whether the shape of what was written is legal.
    step("invariants", lambda: _invariants_step(ledger, settings))

    # OR-1: HALT can arrive DURING a tick — the settle step's impostor scan and the
    # reconcile step both set it, and so can the operator. Every step that can spawn a
    # paid session re-checks first; a system that has just decided to stop must not start
    # an attempt in the same pass. Spec 7.1 names what a halt lets through (settle, reap,
    # genesis, reconcile, invariants, backup, gc, board, digest); the slot step is the one
    # step left that buys sessions, and it is not on that list.
    halt_audited = False

    def _may_spawn(step: str) -> bool:
        nonlocal halt_audited
        if not is_halted(settings):
            return True
        reason = halt_reason(settings)
        print(f"tick: HALT set mid-tick ({reason}); skipping '{step}'")
        if not halt_audited:
            halt_audited = True
            try:
                ledger.audit("halt_mid_tick", detail={"reason": reason, "step": step})
            except Exception:  # noqa: BLE001 - the skip matters more than its audit
                pass
        return False

    step("backup", lambda: _backup_if_stale(ledger, settings, now))
    step("gc", lambda: _gc_if_due(ledger, settings, now))
    # docs/14 C1, amended by docs/22 section 6 item 4: still before the slot step, so the
    # sessions this tick spawns read a snapshot no older than the configured bound instead
    # of each pulling the board themselves (docs/12 §3's five-to-nine-minute survey tax).
    # The pull itself is now a detached child; this step only decides whether one is due.
    step("board", lambda: _board_if_due(ledger, client, settings, now))

    # docs/22 section 8.7: before the slots, and not behind the halt gate. The plan is a
    # row, not a session, and a halted day still wants a readable one.
    step("cell_plan", lambda: _cell_plan_if_due(ledger, settings, now))
    # docs/22 section 8.2: after the plan, before the slots. It buys a model session, so
    # it is behind the same halt gate the slots are.
    if _may_spawn("director"):
        step("director", lambda: _director_if_due(ledger, settings, now))
    if _may_spawn("slots"):
        step("slots", lambda: _run_slots(ledger, settings, now))
    # Last, and under a HALT too: the digest is how a stopped system says what it stopped
    # with, and the day it is most worth reading is the day nothing ran. It reuses the
    # client this tick already holds rather than building a second one.
    step("digest", lambda: _digest_if_due(ledger, client, settings, now))
    try:
        ledger.meta_set("last_tick_ts", iso(now))
    except Exception:  # noqa: BLE001 - best effort; never crash the tick on the final stamp
        pass


# --------------------------------------------------------------------------- commands
@app.command()
def init() -> None:
    """Create data dirs, apply migrations, and (on prod with creds) snapshot genesis."""
    settings = _settings()
    for d in (settings.data_dir, settings.attempts_dir, settings.reports_dir,
              settings.logs_dir, settings.locks_dir, settings.backups_dir,
              settings.board_dir, settings.status_dir, settings.director_dir):
        d.mkdir(parents=True, exist_ok=True)
    # ``check_version=False``: init's job is to bring a behind-the-times file forward, so
    # it is one of the two callers the version guard must not refuse.
    ledger = Ledger.open(settings.ledger_path, check_version=False)
    ledger.migrate()
    k = settings.kalshi
    if k.env == "prod" and k.key_id and k.private_key_path:
        try:
            genesis_snapshot(ledger, _client(settings))
            print("genesis snapshot recorded")
        except Exception as exc:  # noqa: BLE001 - init must succeed offline
            print(f"warning: genesis snapshot skipped: {exc}")
    ledger.close()
    print(f"initialized {settings.data_dir}")


@app.command()
def tick() -> None:
    """The idempotent scheduler pass (spec §15). Safe to run every 15 minutes.

    Runs under a HALT (docs/22 section 7.1). The halt stops betting, and betting is
    stopped by ``_may_spawn`` here, by ``_real_order`` in ``execute.py`` and by
    ``real_orders_allowed`` in ``safety.py``; settle, reap, genesis, reconcile, the
    invariants, backup, gc and the digest all still run, because a halted account still has
    open positions the exchange is settling and money that has to keep adding up. The board
    child is the exception: nothing reads a fresh board while no attempt is running, so
    :func:`_board_if_due` stops at the HALT file.
    """
    settings = _settings()
    # (1) tick.lock — a busy lock means another tick is running; not an error.
    lock = _acquire_lock(settings.locks_dir / "tick.lock")
    if lock is None:
        # Since ST-8 the manual step commands hold this lock too, so a busy lock is no
        # longer proof that it is another *tick* — it is proof that these steps are
        # already running somewhere, which is all this gate ever needed to know.
        print("tick: tick.lock busy; another tick or a manual step command is running")
        raise typer.Exit(0)
    try:
        # The tick fires every fifteen minutes from an editable install, so it is the
        # first thing to run after a merge and before ``betting-agent migrate``. The
        # version guard's message is the instruction; one clean line in the launchd log
        # beats a traceback from whichever statement first touched a missing column.
        try:
            ledger = Ledger.open(settings.ledger_path)
        except LedgerError as exc:
            print(f"tick: {exc}")
            raise typer.Exit(1) from None
        settings.status_dir.mkdir(parents=True, exist_ok=True)
        _tick_run(ledger, _maybe_client(settings), settings, utc_now())
        ledger.close()
    finally:
        _release_lock(lock)


def _mark_slot(ledger, slot: str | None, value: str) -> None:
    """Stamp this child's slot marker; never raises (OR-2/ST-5).

    Bookkeeping for the scheduler, not for the attempt: a marker that fails to write
    costs at worst a re-offer that the ``attempt.lock`` probe will refuse anyway, and it
    must never be the reason an attempt does not run. Manual runs pass ``slot=None``.
    """
    if not slot:
        return
    try:
        ledger.meta_set(slot, value)
    except Exception:  # noqa: BLE001 - the attempt matters more than its slot marker
        pass


def _audit_quiet(ledger, event: str, detail: dict) -> None:
    """Audit without ever raising — bookkeeping must not sink the command around it."""
    try:
        ledger.audit(event, detail=detail)
    except Exception:  # noqa: BLE001 - a failed audit must not mask what it describes
        pass


def _mark_slot_finished(ledger, settings, slot: str | None, attempt_id: str,
                        now: datetime | None = None) -> None:
    """Stamp a finished attempt's slot marker (D8); never raises.

    An attempt that FAILED while its own window is still open leaves the slot
    re-offerable — ``failed:<id>`` rather than ``running:<id>`` — so the tick spawns a
    fresh attempt into the remaining grace instead of writing the slot off. That is the
    docs/12 §8.12 hole: during the Aug-5 wedge every slot died in seconds and not one of
    them was ever retried, because a consumed marker looks identical whether the attempt
    ran for three hours or three seconds.

    Anything else — placed, no_bets, ticket_invalid, or a failure that landed past grace —
    is terminal for the slot and keeps the ``running:<id>`` stamp.
    """
    now = now or utc_now()
    try:
        row = ledger.get_attempt(attempt_id)
        status = row["status"] if row is not None else None
    except Exception:  # noqa: BLE001 - an unreadable row must not lose the marker
        status = None
    if status == "failed" and _slot_window_open(settings, slot, now):
        _mark_slot(ledger, slot, f"{_SLOT_FAILED}{attempt_id}")
        _audit_quiet(ledger, "slot_attempt_failed", {
            "slot": slot, "attempt_id": attempt_id,
            "marker": f"{_SLOT_FAILED}{attempt_id}",
            "action": "slot left re-offerable for the rest of its grace (docs/14 D8)",
        })
        return
    _mark_slot(ledger, slot, f"{_SLOT_RUNNING}{attempt_id}")


def _auth_preflight(settings) -> tuple[bool, str]:
    """``(ok, detail)`` for the Claude-CLI auth check run before every attempt (D8).

    A seam, so the check is stubbed in tests rather than shelling out to the operator's
    own CLI; :func:`betting_agent.sessions.check_claude_auth` is the real thing.
    """
    from betting_agent.sessions import check_claude_auth

    return check_claude_auth(settings.attempt.runner)


def _fail_stranded_attempts(ledger, exc: BaseException) -> list[str]:
    """Drive any attempt still in ``running`` to ``failed`` after a crash (AE-2).

    Called only from the ``attempt`` command's failure path, only while it holds
    ``attempt.lock``. Returns the ids it transitioned — empty when ``run_attempt``
    already did its own, which is the normal case.
    """
    from betting_agent.harness.attempt import _fail_running

    stranded = []
    try:
        rows = ledger.attempts_by_status("running")
    except Exception:  # noqa: BLE001 - an unreadable ledger is the reaper's problem now
        return stranded
    for row in rows:
        if _fail_running(ledger, row["attempt_id"], exc, source="cli"):
            stranded.append(row["attempt_id"])
    return stranded


def _cell_is_forced(ledger, settings, cell: str | None, slot: str | None) -> bool:
    """Did ``--cell`` bypass the day's plan (docs/22 sections 4.1 and 8.7)?

    Only an explicit ``--cell`` can force anything. It bypassed the plan when there is no
    slot to look up, or when the cell it names is not the one the plan assigned to that
    slot. A tick spawn passes the plan's own cell alongside the slot it came from, so it is
    not a forced run and the row says so.
    """
    if cell is None:
        return False
    if slot is None:
        return True
    return cell != cells.cell_for_slot(ledger, settings, slot)


@app.command()
def attempt(
    now_flag: Annotated[bool, typer.Option("--now", help="launch immediately (manual)")] = False,
    model: Annotated[str | None, typer.Option(
        help="model override (default: the slot's arm, else attempt.model)")] = None,
    effort: Annotated[str | None, typer.Option(
        help="low|medium|high|xhigh|max (default: the slot's arm, else attempt.effort)")] = None,
    variant: Annotated[str | None, typer.Option(help="free-form JSON experiment label")] = None,
    slot: Annotated[str | None, typer.Option(help="slot key when launched by tick")] = None,
    cell: Annotated[str | None, typer.Option(
        help="baseline|static|director|focused (default: the day's plan)")] = None,
    force: Annotated[bool, typer.Option("--force", help="run even under HALT")] = False,
) -> None:
    """Run one attempt, holding ``attempt.lock`` for its full duration.

    Refuses under HALT (OR-1) — the tick spawns this command detached, so a HALT set
    between the spawn and the child's start would otherwise be honored only by the
    per-order gate, after the session's money was already spent. ``--force`` is the
    deliberate post-mortem escape.

    Both refusals — HALT, and a busy ``attempt.lock`` — happen *before* the slot marker
    is stamped, so a refused child looks exactly like a spawn that never happened: the
    slot stays ``pending`` and the tick re-offers it while its window is open (OR-2/ST-5).
    A slot burned by a refusal is a slot the day never gets back. The third refusal, the
    D8 auth preflight, comes after the marker is stamped and so restores it by hand.

    On the way out (D8) a failed attempt inside its own window downgrades the marker to
    ``failed:<id>`` instead of leaving the slot consumed — see :func:`_mark_slot_finished`.
    """
    settings = _settings()
    if cell is not None and cell not in cells.CELLS:
        print(f"error: --cell must be one of {', '.join(cells.CELLS)}")
        raise typer.Exit(2)
    if effort is not None and effort not in EFFORTS:
        print(f"error: --effort must be one of {', '.join(EFFORTS)}")
        raise typer.Exit(2)
    if is_halted(settings) and not force:
        print(f"attempt: HALT present: {halt_reason(settings)}; refusing (use --force)")
        raise typer.Exit(0)
    lock = _acquire_lock(settings.locks_dir / "attempt.lock")
    if lock is None:
        print("attempt: attempt.lock busy; another attempt is running")
        raise typer.Exit(1)
    try:
        ledger = Ledger.open(settings.ledger_path)
        attempt_id = None
        try:
            # We hold the lock: this slot is ours, and the marker says so before the
            # first (possibly hours-long) session starts. The interim value is the pid
            # because the attempt id does not exist until ``create_attempt`` runs; it is
            # upgraded below. All the re-offer logic reads is "no longer pending".
            _mark_slot(ledger, slot, f"{_SLOT_RUNNING}pid-{os.getpid()}")
            # OR-4: build the client BEFORE anything is written down. A
            # missing-credentials failure here used to happen after the attempt had
            # already been recorded as launched.
            client = _client(settings)
            # D8: the same argument for Claude's own credentials, which OR-4 never
            # covered. Every wedged slot from Aug 5 to Aug 8 created a row and paid for a
            # session that died on OAuth in 54 ms. This check is local, spends nothing,
            # and runs before the attempt row exists, so an outage costs no slot: the
            # marker goes back to ``pending`` and the next tick offers the slot again.
            auth_ok, auth_detail = _auth_preflight(settings)
            if not auth_ok:
                ledger.audit("auth_preflight_failed",
                             detail={"slot": slot, "check": auth_detail,
                                     "action": "no attempt row; slot re-offered while "
                                               "its window is open"})
                _mark_slot(ledger, slot, f"{_SLOT_PENDING}{iso(utc_now())}")
                print(f"attempt: claude auth preflight failed ({auth_detail}); refusing")
            else:
                if slot:
                    # D1(b) re-arm, and it belongs HERE rather than beside the slot
                    # marker above: a child that stamps its marker and then dies on the
                    # preflight has not become an attempt. Re-arming before the checks
                    # let the child clear the streak the ``slot_spawn_lost`` sweep was
                    # building, so a multi-day auth outage — the exact docs/12 §6 shape
                    # D1(b) exists for — held the counter at 1 and never alerted.
                    record_success(ledger, _SPAWN_STREAK)
                attempt_id = run_attempt(
                    ledger, client, settings,
                    model=model, effort=effort, variant=variant, slot=slot, cell=cell,
                    cell_forced=_cell_is_forced(ledger, settings, cell, slot),
                )
                _mark_slot_finished(ledger, settings, slot, attempt_id)
        except (Exception, KeyboardInterrupt) as exc:
            # AE-2's backstop. ``run_attempt`` transitions its own attempt on the way
            # out; this catches the case where that bookkeeping ALSO failed, plus a
            # crash before any attempt row existed. While we hold ``attempt.lock`` no
            # other process can legitimately have an attempt in ``running``, so anything
            # still there is this crash's. Nothing to do when run_attempt already
            # transitioned — the sweep simply finds no rows.
            stranded = _fail_stranded_attempts(ledger, exc)
            if stranded:
                print(f"attempt: crashed; marked failed: {', '.join(stranded)}")
            raise
        finally:
            ledger.close()
        if attempt_id is None:  # the preflight refused; nothing ran
            raise typer.Exit(1)
        print(attempt_id)
    finally:
        _release_lock(lock)


@app.command("plan")
def plan_cmd(
    date_str: Annotated[str | None, typer.Option(
        "--date", help="Eastern day, YYYY-MM-DD (default: today)")] = None,
) -> None:
    """Print an Eastern day's cell plan, drawing it if it has not been drawn yet."""
    settings = _settings()
    day = date_str or et_day(utc_now())
    try:
        date.fromisoformat(day)
    except ValueError:
        print("error: --date must be YYYY-MM-DD")
        raise typer.Exit(2) from None
    ledger = Ledger.open(settings.ledger_path)
    try:
        drawn = cells.plan(ledger, settings, day)
    finally:
        ledger.close()
    print(_plan_lines(settings, day, drawn))


@app.command()
def director(
    date_str: Annotated[str | None, typer.Option(
        "--date", help="Eastern run date, YYYY-MM-DD (default: today)")] = None,
    force: Annotated[bool, typer.Option("--force", help="run even under HALT")] = False,
) -> None:
    """Run the day's director session, holding ``director.lock`` for its duration.

    Refuses under HALT for the same reason ``attempt`` does (OR-1): the tick spawns this
    detached, so a HALT set between the spawn and the child's start would otherwise be
    honored only after a paid session had already run. ``--force`` is the deliberate
    escape.

    A run date that already has a ``director_runs`` row is refused as well, whatever that
    row says: nothing is retried the same day (docs/22 section 8.2), and a second run
    would overwrite the day's page with one written from the same workspace.
    """
    settings = _settings()
    run_date = date_str or et_day(utc_now())
    try:
        date.fromisoformat(run_date)
    except ValueError:
        print("error: --date must be YYYY-MM-DD")
        raise typer.Exit(2) from None
    if is_halted(settings) and not force:
        print(f"director: HALT present: {halt_reason(settings)}; refusing (use --force)")
        raise typer.Exit(0)
    lock = _acquire_lock(settings.locks_dir / "director.lock")
    if lock is None:
        print("director: director.lock busy; another director run is in progress")
        raise typer.Exit(1)
    try:
        ledger = Ledger.open(settings.ledger_path)
        try:
            already = ledger.director_run_for_date(run_date)
            if already is not None:
                print(f"director: {already['run_id']} already ran ({already['status']}); "
                      "nothing is retried the same day")
                raise typer.Exit(0)
            result = run_director(ledger, settings, run_date=run_date,
                                  client=_maybe_client(settings))
        finally:
            ledger.close()
    finally:
        _release_lock(lock)
    detail = f": {result['error']}" if result.get("error") else ""
    print(f"{result['run_id']} {result['status']}{detail}")


@app.command()
def migrate() -> None:
    """Back up, migrate, and verify the ledger schema (spec L1).

    **Acquires and HOLDS** ``attempt.lock`` and ``tick.lock`` for the whole migration
    (OR-3/ST-9). Probing them and letting go — which is what this did — proves only that
    the system was idle a moment ago; a 15-minute tick could and would begin mid-DDL. A
    held lock means live activity, so we refuse rather than wait.

    HALT does NOT block a migration (deliberate amendment to L1's "refuse under HALT": a
    paused system is exactly when you want to migrate, and the halt gate exists to stop
    *betting*, not maintenance).
    """
    settings = _settings()
    held = []
    for name in ("attempt.lock", "tick.lock"):
        lock = _acquire_lock(settings.locks_dir / name)
        if lock is None:
            for f in held:
                _release_lock(f)
            print(f"migrate: {name} is held; refusing while the system is active")
            raise typer.Exit(1)
        held.append(lock)
    try:
        try:
            report = safe_migrate(settings.ledger_path, settings.backups_dir)
        except LedgerError as exc:
            print(f"migrate FAILED: {exc}")
            raise typer.Exit(1) from exc
        print(f"backup: {report['backup_path']}")
        print(json.dumps(report, default=str, indent=2))
        ledger = Ledger.open(settings.ledger_path)
        ledger.audit("migration_applied", detail=report)
        ledger.close()
    finally:
        for f in reversed(held):
            _release_lock(f)
    print(
        f"migrate: schema v{report['before']['user_version']} → "
        f"v{report['after']['user_version']}; every check passed"
    )


@app.command()
def reconcile(
    full: Annotated[
        bool,
        typer.Option("--full", help="re-verify every post-genesis bet, ignoring the watermark"),
    ] = False,
) -> None:
    """Reconcile the live-era balance walk against the exchange (spec L8).

    Holds ``tick.lock`` (ST-8): the nightly tick runs this exact step.

    Ordinary runs skip bets a previous CLEAN reconciliation already verified against the
    exchange (``meta.fills_verified_through``, EF-1) — settled positions do not change,
    and re-proving all of them nightly is the cost that grows forever. ``--full`` throws
    that away and checks every post-genesis bet from scratch: the right move when the
    watermark itself is what you doubt, and the answer to "but has anything old drifted".

    Exits 0 on an ``exact``, ``absorbed`` or ``reversed`` night and in the paper era, and
    1 on ``noted``, ``large`` or ``provisional``: zero means nothing here needs a person.
    """
    settings = _settings()
    with _tick_step_lock(settings, "reconcile"):
        ledger = Ledger.open(settings.ledger_path)
        try:
            result = reconcile_once(ledger, _client(settings), settings, full=full)
        except sqlite3.IntegrityError:
            # reconciliations.run_at is the primary key at second precision: a manual
            # double-tap inside the same second collides. Nothing is wrong; wait a second.
            print("reconcile: a reconciliation was already recorded this instant; "
                  "retry in a second")
            raise typer.Exit(0) from None
        finally:
            ledger.close()
    print(json.dumps(result, default=str, indent=2))
    raise typer.Exit(0 if _reconcile_passed(result) else 1)


# The verdicts a manual reconcile exits 0 on (Arno, 2026-09-27): the ones that need
# nobody. ``absorbed`` and ``reversed`` are carried by the walk on their own; ``noted``
# and ``large`` want a person, and so does a ``provisional`` run, which concluded nothing.
_RECONCILE_QUIET = frozenset({"exact", "absorbed", "reversed"})


def _reconcile_passed(result: dict) -> bool:
    return bool(result.get("skipped") or result.get("ok")
                or result.get("verdict") in _RECONCILE_QUIET)


# Command name -> the direction ``record_balance_adjustment`` speaks (and the event it
# writes): the verb is what an operator types, the noun is what the audit log records.
_ADJUST_DIRECTION = {"deposit": "deposit", "withdraw": "withdrawal"}


def _adjust_balance(command: str, amount: str) -> None:
    """Shared body of ``deposit``/``withdraw`` (docs/14 B2). Owner-run, never the tick's.

    Holds ``tick.lock`` like every other command that touches the money anchor: the
    nightly reconciliation reads ``meta.live_genesis_balance``, walks, and then compares
    against a balance it fetches afterwards. An anchor that moves between those two reads
    is a HALT on a drift that never existed — so the deposit waits for the tick instead of
    racing it.

    Deliberately NOT wired into ``_tick_run``: a transfer is an owner action with a stated
    amount, and nothing automated has an amount to state. The tick has no path here.
    """
    settings = _settings()
    try:
        stated = D(amount)
    except (ArithmeticError, ValueError):
        print(f"{command}: --amount must be a dollar figure, got {amount!r}")
        raise typer.Exit(1) from None

    with _tick_step_lock(settings, command):
        try:
            client = _client(settings)
        except Exception as exc:  # noqa: BLE001 - a credential problem, reported not raised
            print(f"{command}: cannot reach the exchange to verify the amount: "
                  f"{type(exc).__name__}: {exc}")
            raise typer.Exit(1) from None
        ledger = Ledger.open(settings.ledger_path)
        try:
            result = record_balance_adjustment(
                ledger, client, settings,
                direction=_ADJUST_DIRECTION[command], amount=stated,
            )
        except Exception as exc:  # noqa: BLE001 - a credential/transport problem, reported
            # Deliberately does NOT claim "nothing written": every refusal this command is
            # for returns ``ok: False`` and says so itself (reconcile.py's messages). An
            # exception reaching here is something else — and one of the shapes it can be
            # is the ``deposit_recorded`` audit failing AFTER the anchor moved, which is
            # the one state a "nothing written" line would send the operator past.
            print(f"{command}: failed: {type(exc).__name__}: {exc}\n"
                  f"  the anchor may or may not have moved — check "
                  f"`betting-agent status` and the audit trail for a "
                  f"`{_ADJUST_DIRECTION[command]}_recorded` event before re-running")
            raise typer.Exit(1) from None
        finally:
            ledger.close()

    print(f"{command}: {result['message']}")
    if result["ok"]:
        print("\nPaste into docs/decisions.md:\n")
        print(result["decisions_line"])
    raise typer.Exit(0 if result["ok"] else 1)


@app.command()
def deposit(
    amount: Annotated[str, typer.Option(help="dollars added to the account, e.g. 20.00")],
) -> None:
    """Record an owner deposit against the balance anchor (docs/14 B2). Owner-run only.

    Verifies ``--amount`` against the live exchange balance versus the ledger's own
    expectation, leaving out any absorbed residual the nightly walk carries, and
    **refuses, writing nothing, if they disagree by more than a cent**.
    That is the case where the account holds money this ledger cannot explain, which must be
    investigated rather than absorbed into the anchor. On success it moves
    ``meta.live_genesis_balance``, audits ``deposit_recorded`` with the full derivation, and
    prints the decisions.md entry to paste. ``live_genesis_ts`` never moves: the era
    boundary is a timestamp, not a balance.
    """
    _adjust_balance("deposit", amount)


@app.command()
def withdraw(
    amount: Annotated[str, typer.Option(help="dollars removed from the account, e.g. 20.00")],
) -> None:
    """Record an owner withdrawal against the balance anchor (docs/14 B2). Owner-run only.

    The mirror of ``deposit``: same verification against the live exchange balance, same
    refusal on a disagreement over a cent, and the anchor moves down instead of up. Audits
    ``withdrawal_recorded``; ``live_genesis_ts`` is untouched.
    """
    _adjust_balance("withdraw", amount)


@app.command()
def credit(
    amount: Annotated[str, typer.Option(help="dollars the exchange credited, e.g. 0.01")],
    date: Annotated[str, typer.Option(
        "--date", help="when the exchange credited it, e.g. 2026-09-20T04:45:00Z")],
    kind: Annotated[str, typer.Option(
        help="what the exchange calls it")] = "incentive",
    reason: Annotated[str | None, typer.Option(
        help="the exchange's own words, verbatim")] = None,
) -> None:
    """Record money the exchange gave the account (schema 010). Owner-run only.

    Kalshi credited $0.01 on 2026-09-20 as "Incentive+: Volume Incentive For Event
    KXRAINDNYC-260919", and no API reports it: it is in the app under Account, Activity,
    Credits and nowhere else. Until it is written down it is a cent the nightly balance
    walk cannot explain, and these credits recur for any market in an incentive programme.

    Unlike ``deposit`` this moves no anchor and calls no exchange. A credit is an event
    with a date, so the walk adds it as a flow at that date, which means the next
    reconciliation either explains the drift or does not and nothing has been absorbed
    either way. It holds ``tick.lock`` for the reason ``deposit`` does: the nightly
    reconciliation walks and then compares against a balance it fetches afterwards, and a
    term that appears between those two reads is a HALT on a drift that never existed.
    """
    settings = _settings()
    try:
        stated = D(amount)
    except (ArithmeticError, ValueError):
        print(f"credit: --amount must be a dollar figure, got {amount!r}")
        raise typer.Exit(1) from None
    if stated <= 0:
        print(f"credit: --amount must be positive, got {amount!r}; a credit only ever adds")
        raise typer.Exit(1) from None
    try:
        credited_at = iso(parse_iso(date))
    except (ValueError, TypeError):
        print(f"credit: --date must be a timestamp, e.g. 2026-09-20T04:45:00Z, got {date!r}")
        raise typer.Exit(1) from None

    with _tick_step_lock(settings, "credit"):
        ledger = Ledger.open(settings.ledger_path)
        try:
            now = utc_now()
            credit_id = ledger.insert_credit(
                credited_at=credited_at, amount=stated, kind=kind, reason=reason,
                recorded_at=iso(now),
            )
            ledger.audit("credit_recorded", detail={
                "credit_id": credit_id, "credited_at": credited_at,
                "amount": str(q4(stated)), "kind": kind, "reason": reason,
            })
        finally:
            ledger.close()

    print(f"credit: recorded ${q4(stated)} {kind} credited {credited_at} "
          f"(credit #{credit_id}); the balance walk adds it from now on")
    print("\nPaste into docs/decisions.md:\n")
    print(_credit_decisions_line(credit_id, stated, credited_at, kind, reason,
                                 et_day(now)))


def _credit_decisions_line(credit_id: int, amount, credited_at: str, kind: str,
                           reason: str | None, day: str) -> str:
    """The decisions.md entry to paste, in ``_decisions_line``'s shape.

    Prose money is 2dp and the stored amount is 4dp, the same split the deposit entry
    makes: the entry is a narrative whose evidence has to be exact.
    """
    stated = D(amount).quantize(D("0.01"))
    return "\n".join([
        f"## {day} — ${stated} exchange credit recorded",
        "",
        f"The exchange credited ${stated} on {credited_at} ({kind}"
        + (f": {reason}" if reason else "") + ").",
        f"Mechanics: row {credit_id} in `credits` plus a `credit_recorded` audit_log event.",
        "The balance walk adds it as a flow at that date; no anchor moved, because a",
        "credit is an event with a date and a deposit is not.",
    ])


@app.command()
def settle(
    full: Annotated[bool, typer.Option(
        "--full", help="re-read the whole shared-account history from genesis")] = False,
) -> None:
    """Run one settlement + reconciliation pass (spec §10).

    Holds ``tick.lock`` (ST-8): the tick runs this exact step every 15 minutes, and the
    loser of that race used to traceback out mid-pass, skipping the impostor scan.

    ``--full`` runs the shared-account scan from genesis instead of from its 24-hour
    overlap window, which the tick otherwise does once a week. It is the operator's way to
    backfill ``personal_orders`` on demand: outside orders from before that table
    existed are only written down by a pass that reaches back to them, and the balance
    walk cannot explain a deposit until they are (docs/22 section 7.7).
    """
    settings = _settings()
    with _tick_step_lock(settings, "settle"):
        ledger = Ledger.open(settings.ledger_path)
        try:
            counts = settle_once(ledger, _client(settings), settings, full_scan=full)
        finally:
            ledger.close()
    print(json.dumps(counts, default=str))


@app.command()
def activity(
    attempt: Annotated[str | None, typer.Option(help="record this attempt only")] = None,
    backfill: Annotated[
        bool, typer.Option("--backfill", help="record every attempt that has streams")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="re-extract attempts already recorded")
    ] = False,
) -> None:
    """Record what each attempt did, from its own session streams (schema 008).

    Reads ``data/attempts/<id>/logs/session*.stream.jsonl`` and the attempt's ticket, and
    writes one ``attempt_activity`` row per attempt. Read-only against everything else: no
    market is fetched, no money column is touched, and no attempt status moves. The tick
    records each attempt as it finishes; this command is for the backlog and for
    re-extraction after the extractor learns something new.

    ``--backfill`` walks every attempt that has a stream and **skips the ones already
    recorded** unless ``--force`` is given. A backfilled row's code stamp is today's tree,
    not the tree the attempt ran under, so every backfilled row is marked
    ``stamped_at_backfill = 1`` (see ``008_attempt_activity.sql``).

    Safe under HALT, and deliberately not gated on it: a halted system is exactly when one
    sits down to read the record.
    """
    if not attempt and not backfill:
        print("activity: pass --attempt A-0NNN or --backfill")
        raise typer.Exit(2)
    settings = _settings()
    ledger = Ledger.open(settings.ledger_path)
    try:
        if attempt:
            targets = [ledger.get_attempt(attempt)]
            if targets[0] is None:
                print(f"activity: no such attempt: {attempt}")
                raise typer.Exit(1)
        else:
            targets = [
                row for row in ledger.conn.execute("SELECT * FROM attempts ORDER BY seq")
                if stream_paths(settings.attempts_dir / row["attempt_id"])
            ]
        done = 0
        skipped = 0
        for row in targets:
            aid = row["attempt_id"]
            if not force and ledger.activity(aid) is not None:
                skipped += 1
                continue
            record = activity_row(
                settings.attempts_dir / aid, aid, row["wall_seconds"],
                prompt_version=row["prompt_version"], backfill=True,
            )
            ledger.upsert_activity(record)
            print(format_activity(record))
            done += 1
        # Schema 009 added the two `bt past` counters. Every row written before them ran
        # before `bt past` existed, so the honest backfill value is zero, not NULL.
        filled = ledger.backfill_past_counts() if backfill else 0
    finally:
        ledger.close()
    print(f"activity: recorded {done} attempt(s), skipped {skipped} already recorded")
    if filled:
        print(f"activity: filled zero `bt past` counts on {filled} pre-009 row(s)")


def _board_log(message: str) -> None:
    """One stamped line from the board child.

    The child is detached and its stdout is a log file nobody is watching, so an unstamped
    line cannot be placed against a tick, a halt or a refresh interval afterwards. This is
    the same reason every other durable record here carries a UTC timestamp.
    """
    print(f"{iso(utc_now())} board-refresh: {message}")


@app.command("board-refresh")
def board_refresh(
    set_refusal_text: Annotated[str | None, typer.Option(
        "--set-refusal-text",
        help="store the exchange's refusal wording in the ledger and exit",
    )] = None,
) -> None:
    """Pull the board into ``data/board`` (docs/22 section 6 item 4).

    The tick spawns this DETACHED when the newest generation is past
    ``board.refresh_min_interval_min``, so the pull's minutes no longer sit on the tick's
    critical path. It is also the manual command for forcing a fresh snapshot by hand.

    ``board.lock`` is held for the whole pull. A busy lock means another refresh is already
    running, which is a no-op rather than an error, on the pattern of every other lock here.

    A failing pull is counted and escalated here rather than by the tick. Leaving the tick
    meant leaving the tick's per-step streak, and a detached child that dies into its own
    log file is exactly the silent wedge docs/12 §8.1 is about, so the pull keeps the same
    escalation under its own streak name: three consecutive failures reach a human.

    ``--set-refusal-text`` writes one meta value and refreshes nothing. It is the operator
    quoting the exchange's own words for why it will not take this account's orders in the
    excluded categories, so that every later generation carries the reason in its header
    rather than leaving the exclusion looking like our own unexplained choice.
    """
    settings = _settings()
    if set_refusal_text is not None:
        ledger = Ledger.open(settings.ledger_path)
        try:
            ledger.meta_set("category_refusal_text", set_refusal_text)
        finally:
            ledger.close()
        _board_log(f"category_refusal_text = {set_refusal_text}")
        return
    lock = _acquire_lock(settings.locks_dir / "board.lock")
    if lock is None:
        _board_log("board.lock busy; a refresh is already running")
        raise typer.Exit(0)
    failed = None
    try:
        ledger = Ledger.open(settings.ledger_path)
        try:
            try:
                result = refresh_board_cache(ledger, _maybe_client(settings), settings,
                                             now=utc_now())
            except Exception as exc:  # noqa: BLE001 - reported, counted, then exit 1
                failed = f"{type(exc).__name__}: {exc}"
                n = streak_state(ledger, _BOARD_STREAK)["n"] + 1
                record_failure(
                    ledger, settings, _BOARD_STREAK,
                    threshold=max(1, int(getattr(settings.alerts,
                                                 "step_failure_streak", 3))),
                    title="betting-agent: the board is not refreshing",
                    message=f"board-refresh has failed {n} times running: {failed}",
                    detail={"error": failed},
                )
            else:
                record_success(ledger, _BOARD_STREAK)
        finally:
            ledger.close()
    finally:
        _release_lock(lock)
    if failed is not None:
        _board_log(f"failed: {failed}")
        raise typer.Exit(1)
    _board_log(f"{result.get('generation') or result.get('status')}")


@app.command()
def backup() -> None:
    """Back up the ledger and prune to the newest 30 (spec §6.3)."""
    settings = _settings()
    ledger = Ledger.open(settings.ledger_path)
    path = ledger.backup(settings.backups_dir)
    ledger.meta_set("last_backup_ts", iso(utc_now()))
    ledger.audit("backup_done", detail={"path": str(path)})
    ledger.close()
    print(f"backup: {path}")


@app.command()
def halt(reason: Annotated[str, typer.Argument(help="why the system is halted")]) -> None:
    """Create the HALT kill switch (spec §14)."""
    settings = _settings()
    set_halt(settings, reason)
    if settings.ledger_path.exists():
        ledger = Ledger.open(settings.ledger_path)
        ledger.audit("halt_set", detail={"reason": reason})
        ledger.close()
    print(f"HALT set: {reason}")


@app.command()
def resume() -> None:
    """Remove the HALT kill switch (spec §14)."""
    settings = _settings()
    was_halted = is_halted(settings)
    clear_halt(settings)
    if settings.ledger_path.exists():
        ledger = Ledger.open(settings.ledger_path)
        ledger.audit("halt_cleared", detail={"was_halted": was_halted})
        ledger.close()
    print("HALT cleared")


@app.command()
def status(
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Print today's status digest (docs/22 section 10); ``--json`` adds the meta stamps.

    The digest is the page the tick archives once a day to ``data/status/``; this is the
    same renderer on demand. The two differ in one thing only: the tick files the day that
    has just finished, and this renders today, because "what has the system done so far
    today" is the question a person at a keyboard is asking. A live balance is used when
    credentials are present and a client can be built, and the account line says which
    number it is reading either way.
    """
    settings = _settings()
    halted = is_halted(settings)
    data: dict = {"halted": halted, "halt_reason": halt_reason(settings) if halted else None}
    if settings.ledger_path.exists():
        lg = Ledger.open(settings.ledger_path, readonly=True)
        by_status = {
            r["status"]: r["n"]
            for r in lg.conn.execute(
                "SELECT status, COUNT(*) AS n FROM attempts GROUP BY status"
            ).fetchall()
        }
        data.update({
            "attempts_by_status": by_status,
            "today_real_spend": lg.daily_real_spend(et_day(utc_now())),
            "last_tick_ts": lg.meta_get("last_tick_ts"),
            "last_backup_ts": lg.meta_get("last_backup_ts"),
            "last_reconcile_date": lg.meta_get("last_reconcile_date"),
            "live_genesis_ts": lg.meta_get("live_genesis_ts"),
            "live_genesis_balance": lg.meta_get("live_genesis_balance"),
        })
        today = et_day(utc_now())
        print(status_digest(lg, settings, _maybe_client(settings), day=today))
        # The day's cell plan, read-only: a read-only handle cannot draw one, so a day
        # whose plan has not been stored yet simply says so.
        stored = lg.cell_plan(today)
        data["cell_plan"] = cells.entries(settings, stored["plan"]) if stored else None
        print()
        print(_plan_lines(settings, today, data["cell_plan"]))
        print(_director_line(lg))
        lg.close()
    else:
        print(f"HALT: {'yes (' + str(data['halt_reason']) + ')' if halted else 'no'}")
        print("no ledger yet: run `betting-agent init`")
    if json_out:
        print(json.dumps(data, default=str, indent=2))


if __name__ == "__main__":
    app()
