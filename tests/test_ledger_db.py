"""Ledger DAO — migrations, state machine, unique index, slots, caps, backup (spec §6)."""

import json
import sqlite3
from decimal import Decimal as D

import pytest

from betting_agent.ledger.db import (
    _SCHEMA_DIR,
    Ledger,
    LedgerError,
    _schema_expectations,
)

# Derived, never literal (LG-6, the same lesson ``safe_migrate`` learned): every schema
# file added after this test was written used to break it for saying "2".
_SCHEMA_VERSION = _schema_expectations()[0]

# Canonical legal path to reach each attempt status from 'created'.
_PATH_TO = {
    "created": [],
    "running": ["running"],
    "placed": ["running", "placed"],
    "no_bets": ["running", "no_bets"],
    "ticket_invalid": ["running", "ticket_invalid"],
    "failed": ["running", "failed"],
    "settled": ["running", "placed", "settled"],
    "reviewed": ["running", "placed", "settled", "reviewed"],
}

_LEGAL_EDGES = [
    ("created", "running"),
    ("running", "placed"),
    ("running", "no_bets"),
    ("running", "ticket_invalid"),
    ("running", "failed"),
    ("placed", "settled"),
    ("settled", "reviewed"),
    ("no_bets", "reviewed"),
    ("ticket_invalid", "reviewed"),
    ("failed", "reviewed"),
]

_ILLEGAL_EDGES = [
    ("created", "placed"),
    ("created", "settled"),
    ("created", "reviewed"),
    ("running", "reviewed"),
    ("running", "settled"),
    ("placed", "reviewed"),
    ("placed", "running"),
    ("settled", "placed"),
    ("reviewed", "running"),
    ("no_bets", "placed"),
]


@pytest.fixture
def lg(tmp_path):
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    yield ledger
    ledger.close()


def _mk(lg, **over):
    kw = dict(
        env="prod", model="claude-sonnet-5", effort="high", memory_mode="on",
        prompt_version="p1", toolkit_version="0.1.0", workspace_path="/ws",
    )
    kw.update(over)
    return lg.create_attempt(**kw)


def test_migrate_fresh_and_idempotent(tmp_path):
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == _SCHEMA_VERSION  # 000_init -> 1, then one bump per file
    assert lg.migrate() == _SCHEMA_VERSION  # no-op second time
    rows = lg.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r["name"] for r in rows}
    for t in ["meta", "attempts", "bet_groups", "bets", "retrospectives", "tags",
              "playbook", "playbook_proposals", "audit_log"]:
        assert t in names, t
    # FTS virtual table exists.
    assert lg.conn.execute("SELECT count(*) AS n FROM ledger_fts").fetchone()["n"] == 0
    lg.close()


def test_readonly_cannot_write(tmp_path):
    path = tmp_path / "ledger.db"
    Ledger.open(path).migrate()
    ro = Ledger.open(path, readonly=True)
    with pytest.raises(LedgerError):
        _mk(ro)
    ro.close()


def test_create_and_get_attempt(lg):
    seq, aid = _mk(lg, slot="2026-07-07/10:00", variant='{"study":"x"}')
    assert seq == 1 and aid == "A-0001"
    a = lg.get_attempt(aid)
    assert a["status"] == "created"
    assert a["env"] == "prod"
    assert a["edge_class"] == "probability"
    assert a["slot"] == "2026-07-07/10:00"
    seq2, aid2 = _mk(lg)
    assert (seq2, aid2) == (2, "A-0002")
    assert lg.get_attempt("A-9999") is None


def test_attempts_by_status(lg):
    _mk(lg)
    _, a2 = _mk(lg)
    lg.transition(a2, "running")
    assert [a["attempt_id"] for a in lg.attempts_by_status("created")] == ["A-0001"]
    both = lg.attempts_by_status("created", "running")
    assert [a["attempt_id"] for a in both] == ["A-0001", "A-0002"]


@pytest.mark.parametrize("frm,to", _LEGAL_EDGES)
def test_legal_transitions(lg, frm, to):
    _, aid = _mk(lg)
    for step in _PATH_TO[frm]:
        lg.transition(aid, step)
    lg.transition(aid, to)
    assert lg.get_attempt(aid)["status"] == to


@pytest.mark.parametrize("frm,to", _ILLEGAL_EDGES)
def test_illegal_transitions_raise(lg, frm, to):
    _, aid = _mk(lg)
    for step in _PATH_TO[frm]:
        lg.transition(aid, step)
    with pytest.raises(LedgerError):
        lg.transition(aid, to)


def test_transition_unknown_attempt(lg):
    with pytest.raises(LedgerError):
        lg.transition("A-0404", "running")


def test_update_attempt_fields_whitelist(lg):
    _, aid = _mk(lg)
    lg.update_attempt_fields(aid, context_pack_hash="abc", cost_usd=D("0.1234"))
    a = lg.get_attempt(aid)
    assert a["context_pack_hash"] == "abc"
    assert a["cost_usd"] == "0.1234"  # money stored as 4dp TEXT
    with pytest.raises(LedgerError):
        lg.update_attempt_fields(aid, seq=5)  # not writable


def test_partial_unique_index(lg):
    _, aid = _mk(lg)
    # Two rejected bets on the same ticker are allowed (index excludes rejected).
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"),
                  rationale="r", status="rejected", reject_code="V09")
    lg.insert_bet(bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"),
                  rationale="r", status="rejected", reject_code="V03")
    # First non-rejected on ticker T is fine.
    lg.insert_bet(bet_id=f"{aid}-B03", attempt_id=aid, ticket_index=3, ticker="T",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"),
                  rationale="r", status="filled")
    # Second non-rejected on ticker T is blocked by the partial unique index.
    with pytest.raises(sqlite3.IntegrityError):
        lg.insert_bet(bet_id=f"{aid}-B04", attempt_id=aid, ticket_index=4, ticker="T",
                      side="yes", limit_price=D("0.40"), model_prob=D("0.50"),
                      rationale="r", status="no_fill")


def test_client_order_id_unique(lg):
    _, aid = _mk(lg)
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"),
                  rationale="r", status="filled", client_order_id=f"{aid}-B01")
    with pytest.raises(sqlite3.IntegrityError):
        lg.insert_bet(bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T2",
                      side="yes", limit_price=D("0.40"), model_prob=D("0.50"),
                      rationale="r", status="filled", client_order_id=f"{aid}-B01")


def test_consume_slot_first_writer_wins(lg):
    _, aid = _mk(lg)
    assert lg.consume_slot("slot:2026-07-07/10:00", aid) is True
    assert lg.consume_slot("slot:2026-07-07/10:00", "A-0002") is False
    assert lg.meta_get("slot:2026-07-07/10:00") == aid  # original winner preserved


def test_daily_real_spend_utc_to_et_boundary(lg):
    _, aid = _mk(lg)
    # ET midnight = 04:00 UTC in July. These two share a UTC date but different ET days.
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
                  status="filled", is_real=1, stake=D("0.84"),
                  placed_at="2026-07-07T03:30:00Z")  # 23:30 ET on 07-06
    lg.insert_bet(bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T2",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
                  status="filled", is_real=1, stake=D("0.90"),
                  placed_at="2026-07-07T04:30:00Z")  # 00:30 ET on 07-07
    # A paper bet in-window must be ignored by the real cap.
    lg.insert_bet(bet_id=f"{aid}-B03", attempt_id=aid, ticket_index=3, ticker="T3",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
                  status="filled", is_real=0, stake=D("5.00"),
                  placed_at="2026-07-07T04:30:00Z")
    assert lg.daily_real_spend("2026-07-06") == D("0.8400")
    assert lg.daily_real_spend("2026-07-07") == D("0.9000")
    assert lg.per_market_real_stake("T2", "2026-07-07") == D("0.9000")
    assert lg.per_market_real_stake("T3", "2026-07-07") == D("0.0000")  # paper excluded


def test_the_per_market_cap_is_scoped_to_the_charge_day(lg):
    """docs/22 section 7.6. The per-market cap used to sum a ticker's real stake for ALL
    time while the daily cap beside it was day-scoped, so a ticker that ever reached the
    cap was closed to real orders for good. Both caps now describe the same charge day."""
    # One attempt per leg: the partial unique index forbids two non-rejected rows on the
    # same ticker inside one attempt, and this is about two days, not one ticket.
    for idx, placed in ((1, "2026-07-07T03:30:00Z"), (2, "2026-07-07T04:30:00Z")):
        _, aid = _mk(lg)
        lg.insert_bet(bet_id=f"{aid}-B{idx:02d}", attempt_id=aid, ticket_index=idx,
                      ticker="T1", side="yes", limit_price=D("0.40"),
                      model_prob=D("0.50"), rationale="r", status="filled", is_real=1,
                      stake=D("1.50"), placed_at=placed)
    # Same ticker, same UTC date, two different ET days: each day sees only its own stake.
    assert lg.per_market_real_stake("T1", "2026-07-06") == D("1.5000")
    assert lg.per_market_real_stake("T1", "2026-07-07") == D("1.5000")
    assert lg.per_market_real_stake("T1", "2026-07-08") == D("0.0000")


def test_backup_creates_and_prunes(lg, tmp_path):
    dest = tmp_path / "backups"
    last = None
    for _ in range(5):
        last = lg.backup(dest, keep=3)
    files = list(dest.glob("ledger-*.db"))
    assert len(files) == 3  # pruned to keep
    assert last.exists()
    # Backup is a valid ledger at the current schema version.
    restored = Ledger.open(last, readonly=True)
    assert restored._user_version() == _SCHEMA_VERSION
    restored.close()


def test_groups_and_bets_roundtrip(lg):
    _, aid = _mk(lg, edge_class="structural")
    gid = f"{aid}-G1"
    scenarios = json.dumps([{"name": "a", "outcomes": {"T1": "win", "T2": "loss"}}])
    lg.insert_group(gid, aid, scenarios, D("0.01"))
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.42"), rationale="r",
                  status="filled", group_id=gid)
    groups = lg.groups_for_attempt(aid)
    assert groups[0]["declared_worst_case"] == "0.0100"
    assert groups[0]["status"] == "pending"
    lg.set_group(gid, status="settled", realized_pnl=D("0.01"))
    assert lg.groups_for_attempt(aid)[0]["realized_pnl"] == "0.0100"
    bets = lg.bets_for_attempt(aid)
    assert bets[0]["group_id"] == gid


def test_filled_unsettled_bets(lg):
    _, aid = _mk(lg)
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"),
                  rationale="r", status="filled")
    lg.insert_bet(bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T2",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"),
                  rationale="r", status="settled", outcome="win", pnl=D("0.5"))
    ids = [b["bet_id"] for b in lg.filled_unsettled_bets()]
    assert ids == [f"{aid}-B01"]


def test_audit_and_meta(lg):
    _, aid = _mk(lg)
    lg.audit("attempt_launched", attempt_id=aid, detail={"slot": "10:00"})
    lg.audit("backup_done")
    assert [e["event"] for e in lg.audit_events(limit=10)] == ["backup_done", "attempt_launched"]
    assert lg.audit_events(event="backup_done")[0]["event"] == "backup_done"
    lg.meta_set("k", "v1")
    lg.meta_set("k", "v2")  # upsert
    assert lg.meta_get("k") == "v2"
    assert lg.meta_get("missing", "dflt") == "dflt"


# ---------------------------------------------------------------- Jul29 revision (L1)


def test_new_attempt_columns_default_and_writable(lg):
    _seq, aid = _mk(lg)
    row = lg.get_attempt(aid)
    assert row["loop_mode"] == "one"
    assert row["recipe_id"] is None
    assert row["priors_mode"] is None
    assert row["grader_blind"] is None
    assert row["playbook_version"] is None
    lg.update_attempt_fields(
        aid, recipe_id="abc123", loop_mode="two", priors_mode="on",
        grader_blind=True, playbook_version=31,
    )
    row = lg.get_attempt(aid)
    assert row["loop_mode"] == "two"
    assert row["grader_blind"] == 1
    assert row["playbook_version"] == 31


def test_sessions_roundtrip(lg):
    _seq, aid = _mk(lg)
    lg.insert_session("sess-1", "ideation", "claude-opus-5", "2026-07-30T07:00:00Z", aid)
    lg.finish_session(
        "sess-1", ended_at="2026-07-30T07:10:00Z", exit="ok", num_turns=12,
        cost_usd=D("1.5"), input_tokens=10, output_tokens=20, wall_seconds=600,
    )
    rows = lg.sessions_for_attempt(aid)
    assert len(rows) == 1
    assert rows[0]["cost_usd"] == "1.5000"
    assert rows[0]["exit"] == "ok"
    with pytest.raises(LedgerError):
        lg.finish_session("nope", ended_at="x", exit="ok")


def test_compute_health_columns_round_trip(lg):
    """docs/14 D11 §7: captured once at session close, audited forever from SQL."""
    _seq, aid = _mk(lg)
    lg.insert_session("sess-h", "attempt", "claude-fable-5", "2026-07-30T07:00:00Z", aid)
    lg.finish_session(
        "sess-h", ended_at="2026-07-30T07:10:00Z", exit="ok", num_turns=12,
        api_retries=4, throttle_errors=2,
        error_kinds={"result:api_error:429": 2, "api_retry:throttle": 2},
    )
    row = lg.sessions_for_attempt(aid)[0]
    assert row["api_retries"] == 4
    assert row["throttle_errors"] == 2
    # Sorted keys, so two processes writing the same counts write the same bytes.
    assert row["error_kinds"] == ('{"api_retry:throttle": 2, "result:api_error:429": 2}')


def test_an_uninstrumented_close_leaves_null_not_zero(lg):
    """NULL is 'nobody looked'; 0 is 'looked, saw nothing'. The audit needs both."""
    _seq, aid = _mk(lg)
    lg.insert_session("sess-n", "attempt", "m", "2026-07-30T07:00:00Z", aid)
    lg.finish_session("sess-n", ended_at="2026-07-30T07:10:00Z", exit="ok")
    row = lg.sessions_for_attempt(aid)[0]
    assert row["api_retries"] is None
    assert row["throttle_errors"] is None
    assert row["error_kinds"] is None


def test_an_empty_error_kinds_map_is_stored_as_null(lg):
    _seq, aid = _mk(lg)
    lg.insert_session("sess-e", "attempt", "m", "2026-07-30T07:00:00Z", aid)
    lg.finish_session("sess-e", ended_at="2026-07-30T07:10:00Z", exit="ok",
                      api_retries=0, throttle_errors=0, error_kinds={})
    row = lg.sessions_for_attempt(aid)[0]
    assert row["api_retries"] == 0 and row["error_kinds"] is None


def test_reconciliation_roundtrip_latest(lg):
    lg.insert_reconciliation(
        "2026-07-30T23:00:00Z", expected_balance=D("30.16"),
        actual_balance=D("30.16"), drift=D("0"), ok=True, detail="{}",
    )
    lg.insert_reconciliation(
        "2026-07-31T23:00:00Z", expected_balance=D("29.05"),
        actual_balance=D("29.00"), drift=D("-0.05"), ok=False,
    )
    latest = lg.latest_reconciliation()
    assert latest["run_at"] == "2026-07-31T23:00:00Z"
    assert latest["drift"] == "-0.0500"
    assert latest["ok"] == 0


# --------------------------------------------------------------------------- LG-2 float guard
def test_float_stake_raises_instead_of_being_laundered(lg):
    """LG-2: ``_ser`` quantized a float into a plausible-looking 4dp string.

    ``D(0.1)`` is ``0.1000000000000000055511…``; ``q4`` rendered that as ``"0.1000"`` and
    the floats-forbidden invariant was defeated at the one choke point that could enforce
    it. The DAO is the boundary, so the DAO is where a float has to stop.
    """
    _, aid = _mk(lg)
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
                  status="filled")
    with pytest.raises(LedgerError, match="float"):
        lg.update_bet(f"{aid}-B01", stake=0.1)
    # …and the row is unchanged: the write never happened.
    assert lg.bets_for_attempt(aid)[0]["stake"] is None


@pytest.mark.parametrize("col,value", [
    ("limit_price", 0.42), ("fill_price", 0.42), ("fee", 0.01), ("pnl", 1.5),
    # Counts share the exact-4dp path, so they share the rule: WP0 made counts Decimal
    # end-to-end precisely so a fractional fill is never guessed at.
    ("contracts", 1.0),
])
def test_every_exact_column_refuses_a_float(lg, col, value):
    _, aid = _mk(lg)
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
                  status="no_fill")
    with pytest.raises(LedgerError, match="float"):
        lg.update_bet(f"{aid}-B01", **{col: value})


def test_exact_columns_accept_decimal_int_and_str(lg):
    """The narrowing that matters: the guard rejects floats, not everything."""
    _, aid = _mk(lg)
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
                  status="no_fill")
    lg.update_bet(f"{aid}-B01", stake=D("0.28"), contracts=1, fee="0.0100")
    row = lg.bets_for_attempt(aid)[0]
    assert row["stake"] == "0.2800"
    assert row["fee"] == "0.0100"


def test_non_money_columns_still_accept_floats(lg):
    """``wall_seconds`` is a duration, not money — the guard is deliberately narrow."""
    _, aid = _mk(lg)
    lg.update_attempt_fields(aid, wall_seconds=1.5)
    assert lg.get_attempt(aid)["wall_seconds"] == 1.5


# --------------------------------------------------------------------------- LG-5 whitelist
@pytest.mark.parametrize("col", ["edge_claim_md", "hypothesis_md", "manifest_md"])
def test_ticket_text_columns_are_not_generically_writable(lg, col):
    """LG-5: these are FTS-synced. A generic UPDATE would change the text and leave the
    search index pointing at the old content; ``set_ticket_texts`` is the only writer."""
    _, aid = _mk(lg)
    with pytest.raises(LedgerError, match="non-writable attempt columns"):
        lg.update_attempt_fields(aid, **{col: "smuggled"})


def test_set_ticket_texts_still_writes_and_syncs_fts(lg):
    _, aid = _mk(lg)
    lg.set_ticket_texts(aid, "## Markets\nweather claim", "## If we're right\nx", "m")
    assert lg.get_attempt(aid)["edge_claim_md"] == "## Markets\nweather claim"
    hits = lg.conn.execute(
        "SELECT attempt_id FROM ledger_fts WHERE ledger_fts MATCH 'weather'"
    ).fetchall()
    assert [h["attempt_id"] for h in hits] == [aid]


def test_the_index_takes_three_kinds_and_refuses_the_retired_ones(lg):
    """docs/22 section 13: `retro`, `summary` and `tags` lost their writers, so the ledger
    stops accepting them. The rows already in the index are untouched by this."""
    _, aid = _mk(lg)
    lg.fts_upsert(aid, "closing", "the closing paragraph")
    for kind in ("retro", "summary", "tags"):
        with pytest.raises(LedgerError, match="unknown FTS kind"):
            lg.fts_upsert(aid, kind, "no longer written")
    kinds = {
        r["kind"] for r in lg.conn.execute("SELECT kind FROM ledger_fts").fetchall()
    }
    assert kinds == {"closing"}


# --------------------------------------------------------------------------- LG-3 rowcount
def test_set_group_on_a_missing_group_raises(lg):
    """LG-3: every other targeted updater checks rowcount; this one silently succeeded."""
    with pytest.raises(LedgerError, match="no such group: A-9999-G1"):
        lg.set_group("A-9999-G1", status="settled")


def test_set_group_with_no_fields_is_still_a_no_op(lg):
    """The early return precedes the rowcount check — an empty update writes nothing and
    must not manufacture an error for a group that does not exist."""
    lg.set_group("A-9999-G1")


# --------------------------------------------------------------------------- LG-8/ST-10 pragmas
def test_open_sets_busy_timeout_and_synchronous(tmp_path):
    """Three writer processes plus a whole-DB backup, on Python's 5 s default, is how a
    tick starts failing with 'database is locked' (LG-8/ST-10)."""
    path = tmp_path / "ledger.db"
    rw = Ledger.open(path)
    rw.migrate()
    assert rw.conn.execute("PRAGMA busy_timeout").fetchone()["timeout"] == 30000
    assert rw.conn.execute("PRAGMA synchronous").fetchone()["synchronous"] == 1  # NORMAL
    assert rw.conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"] == "wal"
    rw.close()
    ro = Ledger.open(path, readonly=True)
    assert ro.conn.execute("PRAGMA busy_timeout").fetchone()["timeout"] == 30000
    ro.close()


# --------------------------------------------------------------------------- LG-9 contention
def test_consume_slot_across_two_connections_has_exactly_one_winner(lg, tmp_path):
    """LG-9: the slot claim is what stops two ticks spawning two attempts, and until now
    it was only ever exercised on a single connection."""
    other = Ledger.open(lg.path)
    try:
        assert lg.consume_slot("slot:2026-07-07/10:00", "pending:a") is True
        assert other.consume_slot("slot:2026-07-07/10:00", "pending:b") is False
        assert other.meta_get("slot:2026-07-07/10:00") == "pending:a"
    finally:
        other.close()


def test_reclaim_slot_across_two_connections_has_exactly_one_winner(lg):
    """Same argument for the re-offer: two ticks must not both revive one slot."""
    other = Ledger.open(lg.path)
    try:
        lg.consume_slot("slot:2026-07-07/10:00", "pending:t0")
        assert lg.reclaim_slot("slot:2026-07-07/10:00", "pending:t0", "pending:t1") is True
        assert other.reclaim_slot("slot:2026-07-07/10:00", "pending:t0", "pending:t2") is False
        assert other.meta_get("slot:2026-07-07/10:00") == "pending:t1"
    finally:
        other.close()


def test_reclaim_slot_never_creates_a_slot_that_was_never_consumed(lg):
    assert lg.reclaim_slot("slot:2026-07-07/10:00", "pending:t0", "lost") is False
    assert lg.meta_get("slot:2026-07-07/10:00") is None


def test_a_second_connection_waits_out_a_writer_instead_of_erroring(lg):
    """The busy_timeout, demonstrated: a held BEGIN IMMEDIATE makes the second writer
    WAIT (and here, time out at a deliberately tiny timeout) rather than fail instantly."""
    other = Ledger.open(lg.path)
    other.conn.execute("PRAGMA busy_timeout=50")   # keep the test quick; 30 s in production
    cur = lg.conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.meta_set("k", "v")
    finally:
        lg.conn.rollback()
        other.close()
    # Once the writer lets go, the same call succeeds — the lock was contention, not damage.
    again = Ledger.open(lg.path)
    again.meta_set("k", "v")
    assert again.meta_get("k") == "v"
    again.close()


def test_reads_see_committed_rows_from_another_connection(lg):
    """WAL's other half: a reader is never blocked by (and never sees) a live writer."""
    reader = Ledger.open(lg.path, readonly=True)
    try:
        cur = lg.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("INSERT INTO meta (key, value) VALUES ('pending-key', '1')")
        assert reader.meta_get("pending-key") is None     # uncommitted: invisible
        lg.conn.commit()
        assert reader.meta_get("pending-key") == "1"      # committed: visible
    finally:
        reader.close()


# --------------------------------------------------------------------------- OR-5 meta_set_many
def test_meta_set_many_writes_every_key(lg):
    lg.meta_set_many({"live_genesis_ts": "2026-08-01T00:00:00Z", "live_genesis_balance": "30.16"})
    assert lg.meta_get("live_genesis_ts") == "2026-08-01T00:00:00Z"
    assert lg.meta_get("live_genesis_balance") == "30.16"


def test_meta_set_many_is_all_or_nothing(lg):
    """OR-5: the live-genesis pair written as two transactions could be interrupted
    between them, leaving a timestamp with no balance — a half-stamp that fails every
    later reconciliation and never retries. Failing on the SECOND key is the whole
    point, so the failure is injected there."""
    class _Explodes:
        def __str__(self):
            raise RuntimeError("interrupted between the two stamps")

    with pytest.raises(RuntimeError, match="interrupted"):
        lg.meta_set_many({"live_genesis_ts": "2026-08-01T00:00:00Z",
                          "live_genesis_balance": _Explodes()})
    assert lg.meta_get("live_genesis_ts") is None      # neither key landed
    assert lg.meta_get("live_genesis_balance") is None


def test_meta_set_many_with_nothing_to_write_is_a_no_op(lg):
    lg.meta_set_many({})


# ------------------------------------------------------------------ OR-9/EF-4: audit_count
def test_audit_count_counts_in_sql_and_honors_since(lg):
    """The report used to materialize up to a million rows in Python, three times a
    render, to produce these numbers. ``audit_log`` only ever grows."""
    _, aid = _mk(lg)
    for _ in range(3):
        lg.audit("slot_skipped", attempt_id=aid)
    lg.audit("backup_done")
    # backdate one row so `since` has something to exclude
    lg.conn.execute(
        "UPDATE audit_log SET ts='2019-12-31T00:00:00Z' WHERE id=(SELECT MIN(id) FROM "
        "audit_log WHERE event='slot_skipped')"
    )
    lg.conn.commit()

    assert lg.audit_count("slot_skipped") == 3
    assert lg.audit_count("slot_skipped", since="2020-01-01T00:00:00Z") == 2
    assert lg.audit_count("backup_done") == 1
    assert lg.audit_count("never_happened") == 0


def test_audit_count_is_not_capped_the_way_audit_events_is(lg):
    """``audit_events`` defaults to 100 rows; a count that silently stopped at a limit is
    exactly the bug this replaces."""
    for _ in range(150):
        lg.audit("personal_fill_observed")

    assert lg.audit_count("personal_fill_observed") == 150
    assert len(lg.audit_events(event="personal_fill_observed")) == 100


# ------------------------------------------------------- the version guard (coordinator)
def _behind_ledger(path):
    """A real ledger at schema 3: the shape a production file has when the code moved on
    but ``betting-agent migrate`` has not run yet."""
    lg = Ledger.open(path, check_version=False)
    for name in ("000_init.sql", "001_live.sql", "002_compute_health.sql"):
        lg._apply_schema_file(_SCHEMA_DIR / name)
    assert lg._user_version() == 3
    lg.close()
    return path


def test_a_ledger_behind_the_code_refuses_to_open(tmp_path):
    """The live tick runs from an editable install every fifteen minutes, so code that
    expects a new column can land before ``betting-agent migrate`` has run. Without a
    guard that gap is a crash in the middle of a tick, at whichever statement first names
    the missing column. With it, the file refuses to open and says what to run."""
    db = _behind_ledger(tmp_path / "ledger.db")

    with pytest.raises(LedgerError) as ei:
        Ledger.open(db)
    assert "v3" in str(ei.value) and f"v{_SCHEMA_VERSION}" in str(ei.value)
    assert "betting-agent migrate" in str(ei.value)

    # …and the escape the migration path itself needs still opens it.
    behind = Ledger.open(db, check_version=False)
    assert behind.migrate() == _SCHEMA_VERSION
    behind.close()
    Ledger.open(db).close()                         # now current, so it opens normally


def test_a_brand_new_file_is_not_refused(tmp_path):
    """``user_version`` 0 is an empty file about to be created, not one left behind."""
    lg = Ledger.open(tmp_path / "fresh.db")
    assert lg._user_version() == 0
    assert lg.migrate() == _SCHEMA_VERSION
    lg.close()


def test_the_guard_applies_to_read_only_opens_too(tmp_path):
    """A stale file is stale for readers as well: ``bt`` opens read-only and would
    otherwise fail on a missing column deep inside a query."""
    db = _behind_ledger(tmp_path / "ledger.db")
    with pytest.raises(LedgerError):
        Ledger.open(db, readonly=True)


# ------------------------------------------- personal orders (docs/22 sections 4.4, 7.3)
def _owner_row(lg, oid, *, created, cost="0.98", fee="0.0014", ticker="KXBIKE"):
    lg.upsert_personal_order(
        oid, ticker=ticker, side="yes", created_time=created, contracts=D("1"),
        cost=D(cost), fee=D(fee), fee_source="exchange", first_seen_at=created,
    )


def test_the_upsert_keeps_first_seen_and_refreshes_the_money(lg):
    _owner_row(lg, "OID-1", created="2026-08-30T12:00:00Z")
    lg.upsert_personal_order(
        "OID-1", ticker="KXBIKE", side="yes", created_time="2026-08-30T12:00:00Z",
        contracts=D("2"), cost=D("1.96"), fee=D("0.0028"), fee_source="exchange",
        first_seen_at="2026-09-06T00:00:00Z",          # a later sighting
    )
    rows = lg.personal_orders()
    assert len(rows) == 1
    assert rows[0]["first_seen_at"] == "2026-08-30T12:00:00Z"   # never moves
    assert rows[0]["cost"] == "1.9600"                          # the money does


def test_settling_a_personal_order_twice_is_refused(lg):
    _owner_row(lg, "OID-1", created="2026-08-30T12:00:00Z")
    lg.settle_personal_order("OID-1", settled_at="2026-09-01T00:00:00Z", payout=D("1"))
    with pytest.raises(LedgerError):
        lg.settle_personal_order("OID-1", settled_at="2026-09-02T00:00:00Z", payout=D("0"))
    assert lg.personal_orders()[0]["payout"] == "1.0000"
    assert lg.unsettled_personal_orders() == []


def test_the_summary_counts_and_nets_since_genesis(lg):
    """What ``betting-agent status`` prints: how many outside orders since the
    live era began, and what they have taken out of the account net of payouts."""
    _owner_row(lg, "OID-OLD", created="2026-07-01T00:00:00Z")       # pre-genesis
    _owner_row(lg, "OID-1", created="2026-08-30T12:00:00Z")
    _owner_row(lg, "OID-2", created="2026-09-02T12:00:00Z", ticker="KXOTHER")
    lg.settle_personal_order("OID-2", settled_at="2026-09-03T00:00:00Z", payout=D("1.00"))

    summary = lg.personal_orders_summary("2026-07-31T05:44:29Z")
    assert summary["n"] == 2
    # OID-1 is still open (0.98 + 0.0014 out); OID-2 cost 0.9814 and paid 1.00 back
    assert summary["net_cost"] == D("0.9628")
    assert lg.personal_orders_summary(None)["n"] == 3        # no window, every row
    assert lg.personal_order_tickers() == {"KXBIKE", "KXOTHER"}
