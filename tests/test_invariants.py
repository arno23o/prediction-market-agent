"""The six ledger invariants (docs/22 section 10), moved out of the audit's section 4.

Two questions per check: does it pass on a clean ledger, and does it name the offending
rows on a seeded defect. The defects are seeded with ``PRAGMA foreign_keys=OFF`` or by
raw SQL where the writers would refuse them, which is the point: these checks exist for
the shapes that should be impossible, and the only way to test them is to make one.
"""

from __future__ import annotations

from decimal import Decimal as D

import pytest

from betting_agent.harness.invariants import run_invariants
from betting_agent.ledger.db import Ledger

_NAMES = [
    "fts_integrity",
    "money_text_4dp",
    "no_orphan_bets",
    "settled_completeness",
    "reviewed_have_retros",
    "sessions_coverage",
]


@pytest.fixture
def lg(tmp_path):
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    yield ledger
    ledger.close()


def _attempt(ledger, **kw):
    _seq, aid = ledger.create_attempt(
        env="prod", model="m", effort="high", memory_mode="on", prompt_version="p",
        toolkit_version="0.1.0", workspace_path="/ws", **kw,
    )
    return aid


def _by_name(rows) -> dict:
    return {r["name"]: r for r in rows}


def test_every_check_passes_on_a_clean_ledger(lg):
    aid = _attempt(lg)
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="KXA",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.55"), rationale="r",
                  is_real=1, status="settled", contracts=1, fill_price=D("0.40"),
                  stake=D("0.40"), fee=D("0.02"), outcome="win", pnl=D("0.58"))

    rows = run_invariants(lg)

    assert [r["name"] for r in rows] == _NAMES
    assert all(r["ok"] for r in rows)
    assert all(r["detail"] for r in rows)


def test_the_report_is_one_row_per_check_in_a_fixed_order(lg):
    assert [r["name"] for r in run_invariants(lg)] == _NAMES


def test_sessions_coverage_is_skipped_while_the_table_is_empty(lg):
    _attempt(lg)
    row = _by_name(run_invariants(lg))["sessions_coverage"]
    assert row["ok"] is True and row["skipped"] is True
    assert "not yet meaningful" in row["detail"]


def test_sessions_coverage_names_an_uncovered_attempt(lg):
    covered = _attempt(lg)
    lg.insert_session(session_id="S1", attempt_id=covered, kind="attempt", model="m",
                      started_at="2020-01-01T00:00:00Z")
    naked = _attempt(lg)

    row = _by_name(run_invariants(lg))["sessions_coverage"]

    assert row["ok"] is False
    assert naked in row["detail"] and covered not in row["detail"]


def test_an_attempt_that_failed_before_its_session_is_not_an_uncovered_attempt(lg):
    """A render that raises fails the attempt before any session launches (docs/22 5.2).

    There is no session row to find and there never will be one, so counting it here
    would leave the check failing for good over a row the harness handled correctly. The
    tell is ``session_exit``: no session ever reported.
    """
    covered = _attempt(lg)
    lg.insert_session(session_id="S1", attempt_id=covered, kind="attempt", model="m",
                      started_at="2020-01-01T00:00:00Z")
    stillborn = _attempt(lg)
    lg.transition(stillborn, "running")
    lg.transition(stillborn, "failed")

    row = _by_name(run_invariants(lg))["sessions_coverage"]

    assert row["ok"] is True
    assert stillborn not in row["detail"]


def test_a_failed_attempt_that_did_run_a_session_still_needs_its_row(lg):
    """The exemption is for attempts that never ran a session, not for every failure.

    An attempt whose ``session_exit`` is set paid for a session, so a missing ``sessions``
    row for it is exactly the hole this check exists to name.
    """
    covered = _attempt(lg)
    lg.insert_session(session_id="S1", attempt_id=covered, kind="attempt", model="m",
                      started_at="2020-01-01T00:00:00Z")
    ran_and_failed = _attempt(lg)
    lg.transition(ran_and_failed, "running")
    lg.transition(ran_and_failed, "failed")
    lg.update_attempt_fields(ran_and_failed, session_exit="error")

    row = _by_name(run_invariants(lg))["sessions_coverage"]

    assert row["ok"] is False
    assert ran_and_failed in row["detail"]


def test_money_text_names_the_column_that_is_not_four_decimal_places(lg):
    aid = _attempt(lg)
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="KXA",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.55"), rationale="r",
                  is_real=0, status="no_fill")
    lg.conn.execute("UPDATE bets SET limit_price='0.40'")  # 2dp: the shape 4.2 forbids
    lg.conn.commit()

    row = _by_name(run_invariants(lg))["money_text_4dp"]

    assert row["ok"] is False
    assert f"{aid}-B01.limit_price" in row["detail"]


def test_an_orphan_bet_is_named(lg):
    lg.conn.execute("PRAGMA foreign_keys=OFF")
    lg.conn.execute(
        "INSERT INTO bets (bet_id, attempt_id, ticket_index, ticker, side, limit_price, "
        "rationale, status) VALUES ('X-B01','A-9999',1,'T','yes','0.4000','r','no_fill')"
    )
    lg.conn.commit()

    row = _by_name(run_invariants(lg))["no_orphan_bets"]

    assert row["ok"] is False and "X-B01" in row["detail"]


def test_a_settled_bet_without_pnl_or_outcome_is_named(lg):
    aid = _attempt(lg)
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="KXA",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.55"), rationale="r",
                  is_real=1, status="filled", contracts=1, fill_price=D("0.40"),
                  stake=D("0.40"), fee=D("0.02"))
    lg.conn.execute("UPDATE bets SET status='settled'")   # settled with no pnl, no outcome
    lg.conn.commit()

    row = _by_name(run_invariants(lg))["settled_completeness"]

    assert row["ok"] is False and f"{aid}-B01" in row["detail"]


def test_a_reviewed_attempt_without_a_retrospective_is_named(lg):
    aid = _attempt(lg)
    lg.transition(aid, "running")
    lg.transition(aid, "no_bets")
    lg.conn.execute("UPDATE attempts SET status='reviewed' WHERE attempt_id=?", (aid,))
    lg.conn.commit()

    row = _by_name(run_invariants(lg))["reviewed_have_retros"]

    assert row["ok"] is False and aid in row["detail"]


def test_the_fts_check_is_skipped_on_a_read_only_handle(tmp_path):
    """FTS5's ``integrity-check`` is issued as a write, so a read-only connection refuses
    it. That is a check that could not run, not a corrupt index, and ``betting-agent
    status`` opens exactly such a connection on every invocation."""
    writable = Ledger.open(tmp_path / "ledger.db")
    writable.migrate()
    writable.close()

    ro = Ledger.open(tmp_path / "ledger.db", readonly=True)
    try:
        row = _by_name(run_invariants(ro))["fts_integrity"]
    finally:
        ro.close()

    assert row["ok"] is True and row["skipped"] is True
    assert "read-only connection" in row["detail"]


def test_the_writable_pass_still_runs_the_fts_check_for_real(lg):
    row = _by_name(run_invariants(lg))["fts_integrity"]
    assert row["ok"] is True and "skipped" not in row
    assert row["detail"] == "FTS integrity-check passed"


def test_a_corrupt_fts_index_is_a_finding_and_not_a_crash(lg):
    lg.conn.execute("DROP TABLE ledger_fts")
    lg.conn.commit()

    row = _by_name(run_invariants(lg))["fts_integrity"]

    assert row["ok"] is False
    assert "ledger_fts" in row["detail"]


def test_one_broken_check_does_not_hide_the_other_five(lg):
    """A check that raises is reported as a failure; the pass continues past it."""
    lg.conn.execute("DROP TABLE ledger_fts")
    lg.conn.execute("DROP TABLE sessions")
    lg.conn.commit()

    rows = _by_name(run_invariants(lg))

    assert [r["name"] for r in run_invariants(lg)] == _NAMES
    assert rows["fts_integrity"]["ok"] is False
    assert rows["sessions_coverage"]["ok"] is False
    assert "check raised" in rows["sessions_coverage"]["detail"]
    assert rows["no_orphan_bets"]["ok"] is True
