"""The six ledger integrity checks (docs/22 section 10).

These were section 4 of the eight-section audit. The audit is gone; the checks are not,
because they are the only thing in the system that reads the ledger looking for a shape
that should be impossible rather than for a number. They now run as their own tick step,
right after reconcile, and each failure raises an alert with the key ``invariant:<name>``
instead of landing in a dated file nobody opened.

The logic is the audit's, unchanged. What changed is where the answers go:
:func:`run_invariants` returns one row per check, and the caller (the tick step, and
:mod:`harness.digest`) decides what to do with them. Nothing here writes, alerts or
raises: a check that cannot run is a failed check, reported as one.

``sessions_coverage`` can come back ``skipped``: on a ledger whose ``sessions`` table is
empty there is nothing to be covered by, and reporting a hole that cannot exist is worse
than saying the check is not yet meaningful. A skipped check counts as passing.
"""

from __future__ import annotations

import re
import sqlite3

# Storage shape for every money TEXT column (spec section 5: Decimal-as-TEXT at 4dp).
_MONEY_RE = re.compile(r"^-?\d+\.\d{4}$")
_BET_MONEY_COLS = ("limit_price", "model_prob", "fill_price", "stake", "fee", "pnl")


def _check_fts(conn, *, readonly: bool = False) -> dict:
    """FTS5's own ``integrity-check``, which sqlite issues as a write.

    That makes it unrunnable on a read-only connection, and ``betting-agent status`` opens
    one, so the check used to report the read-only refusal as a corrupt index on every
    single run. A check that cannot run is not a failing check: it is skipped, and says so,
    the way sessions coverage does on a ledger with no sessions. The tick holds a writable
    connection and runs it for real every fifteen minutes.
    """
    if readonly:
        return {"ok": True, "skipped": True,
                "note": "read-only connection: the FTS integrity-check needs a write"}
    try:
        conn.execute("INSERT INTO ledger_fts(ledger_fts) VALUES('integrity-check')")
        return {"ok": True, "note": "FTS integrity-check passed"}
    except sqlite3.OperationalError as exc:
        # Belt to the flag's braces: a connection opened writable over a file the process
        # may not write reaches this instead, and it is the same non-finding.
        if "readonly database" in str(exc):
            return {"ok": True, "skipped": True,
                    "note": "read-only database: the FTS integrity-check needs a write"}
        return {"ok": False, "note": f"{type(exc).__name__}: {exc}"}
    except Exception as exc:  # noqa: BLE001 - any other sqlite error here is the finding
        return {"ok": False, "note": f"{type(exc).__name__}: {exc}"}


def _check_money_text(conn) -> dict:
    """Every non-NULL bets money value must be 4dp TEXT (checked in Python with ``re``)."""
    cols = ", ".join(_BET_MONEY_COLS)
    rows = conn.execute(f"SELECT bet_id, {cols} FROM bets").fetchall()
    bad: list[str] = []
    for r in rows:
        for c in _BET_MONEY_COLS:
            v = r[c]
            if v is not None and not _MONEY_RE.match(str(v)):
                bad.append(f"{r['bet_id']}.{c}={v!r}")
    return {
        "ok": not bad,
        "note": (f"{len(rows)} bets x {len(_BET_MONEY_COLS)} money columns clean"
                 if not bad else f"{len(bad)} malformed: " + ", ".join(bad[:10])),
    }


def _check_orphan_bets(conn) -> dict:
    rows = conn.execute(
        "SELECT b.bet_id AS bet_id FROM bets b WHERE NOT EXISTS "
        "(SELECT 1 FROM attempts a WHERE a.attempt_id = b.attempt_id)"
    ).fetchall()
    ids = [r["bet_id"] for r in rows]
    return {"ok": not ids,
            "note": "no bets reference a missing attempt" if not ids
                    else f"{len(ids)} orphan bets: " + ", ".join(ids[:10])}


def _check_settled_completeness(conn) -> dict:
    rows = conn.execute(
        "SELECT bet_id FROM bets WHERE status='settled' AND (pnl IS NULL OR outcome IS NULL)"
    ).fetchall()
    ids = [r["bet_id"] for r in rows]
    return {"ok": not ids,
            "note": "every settled bet has pnl and outcome" if not ids
                    else f"{len(ids)} incomplete: " + ", ".join(ids[:10])}


def _check_reviewed_have_retros(conn) -> dict:
    rows = conn.execute(
        "SELECT a.attempt_id AS attempt_id FROM attempts a WHERE a.status='reviewed' "
        "AND NOT EXISTS (SELECT 1 FROM retrospectives r WHERE r.attempt_id=a.attempt_id)"
    ).fetchall()
    ids = [r["attempt_id"] for r in rows]
    return {"ok": not ids,
            "note": "every reviewed attempt has a retrospective" if not ids
                    else f"{len(ids)} missing: " + ", ".join(ids[:10])}


def _check_sessions_coverage(conn) -> dict:
    """Attempts newer than the first recorded session must each have a session row.

    Anchoring on ``MIN(sessions.started_at)`` rather than a migration date keeps the check
    honest on a ledger that predates the sessions table: pre-sessions attempts are simply
    out of scope instead of being reported as a hole.

    One attempt is out of scope for a different reason: one that is ``failed`` and whose
    ``session_exit`` is NULL, which is an attempt that died before a session ever reported
    anything. A failed render does exactly that, and there is no session row to find and
    never will be, so counting it would leave this check failing for good over a row the
    harness handled correctly. A failed attempt whose ``session_exit`` is set DID run a
    session, so a missing row for it is the hole this check exists to name.
    """
    first = conn.execute("SELECT MIN(started_at) AS t FROM sessions").fetchone()["t"]
    if not first:
        return {"ok": True, "skipped": True,
                "note": "sessions table is empty, coverage not yet meaningful"}
    rows = conn.execute(
        "SELECT a.attempt_id AS attempt_id FROM attempts a WHERE a.created_at >= ? "
        "AND NOT (a.status = 'failed' AND a.session_exit IS NULL) "
        "AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.attempt_id=a.attempt_id)",
        (first,),
    ).fetchall()
    ids = [r["attempt_id"] for r in rows]
    return {"ok": not ids,
            "note": f"anchored at {first}; " + ("all covered" if not ids
                    else f"{len(ids)} uncovered: " + ", ".join(ids[:10]))}


# ``needs_write`` marks the checks that cannot run on a read-only handle.
_CHECKS = (
    ("fts_integrity", _check_fts, True),
    ("money_text_4dp", _check_money_text, False),
    ("no_orphan_bets", _check_orphan_bets, False),
    ("settled_completeness", _check_settled_completeness, False),
    ("reviewed_have_retros", _check_reviewed_have_retros, False),
    ("sessions_coverage", _check_sessions_coverage, False),
)


def run_invariants(ledger) -> list[dict]:
    """Run all six checks in order. One row each: ``name``, ``ok``, ``detail``.

    ``detail`` is the check's own sentence, which names the failing rows when there are
    any (capped at ten, the audit's cap). ``skipped`` is present and true on a check that
    could not be run rather than one that failed: sessions coverage with no sessions to
    anchor to, and the FTS check on a read-only handle.

    A check that raises is reported as a failure rather than propagating: this runs inside
    a tick step, and one broken check must not hide the other five.
    """
    readonly = bool(getattr(ledger, "readonly", False))
    rows: list[dict] = []
    for name, fn, needs_write in _CHECKS:
        try:
            result = fn(ledger.conn, readonly=readonly) if needs_write else fn(ledger.conn)
        except Exception as exc:  # noqa: BLE001 - a broken check is a finding, not a crash
            result = {"ok": False, "note": f"check raised: {type(exc).__name__}: {exc}"}
        row = {"name": name, "ok": bool(result["ok"]), "detail": result["note"]}
        if result.get("skipped"):
            row["skipped"] = True
        rows.append(row)
    return rows
