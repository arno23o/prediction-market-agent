"""Fake-agent end-to-end pipeline suite (spec §18).

Proves every joint of the pipeline with zero LLM/API cost: ``run_attempt`` drives the
real ``tests/fake_agent.py`` subprocess (via ``attempt.runner``) which copies a fixture
ticket into ``../ticket/`` and emits a Claude-CLI stream; validation, execution,
settlement and the shared-account tripwires then run against a :class:`FakeKalshi`
recorded-response exchange. Nothing is mocked below ``run_attempt``, and this is the
first place the real attempt→validate→execute→settle seams meet.

Scenarios (task letters a–i) are labelled on each test. The suite is deterministic and
tmp-isolated so it passes twice in a row, including the crash-and-resume mid-pipeline
(scenario i).

The last section (l) is the loop, built around one cohort (docs/22 section 13, phase
three): nine attempts across the four cells on one day, a director run the next morning
that ranks them with no outcome known, settlement, a second run that ranks the completed
cohort with the outcomes in front of it, and ``bt past attempt`` printing all of it back.
Nothing there is mocked either: the same fake agent writes the director's three files.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from betting_agent.config import Settings, load_settings
from betting_agent.harness import director as director_mod
from betting_agent.harness.attempt import run_attempt
from betting_agent.harness.director import run_director
from betting_agent.harness.reconcile import reconcile_once
from betting_agent.harness.safety import clear_halt
from betting_agent.harness.settle import genesis_snapshot, settle_once
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.ledger import history
from betting_agent.ledger.db import Ledger
from betting_agent.timeutil import iso, parse_iso, utc_now

_NOW = parse_iso("2026-07-07T12:00:00Z")
_CLOSE = _NOW + timedelta(hours=1)  # inside the 72 h resolution window

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"

# --------------------------------------------------------------------------- world
@dataclass
class World:
    settings: Settings
    ledger: Ledger
    fake: FakeKalshi
    root: Path


def _fake_agent_cmd() -> str:
    """Path to the fake agent, made executable so it can be argv[0] via its shebang."""
    os.chmod(FAKE_AGENT, 0o755)
    return str(FAKE_AGENT)


def _seed_markets(fake: FakeKalshi) -> None:
    """Seed every market the nine fixtures reference (books chosen so valid_basic fills).

    Ask sizes are all comfortably above the one-contract order size, so every validated
    bet fills in full: since the 2026-07-29 all-real decision there is no liquidity-ranked
    real subset for depth to select (TQ-6 — the ranking these sizes once encoded is gone).
    """
    def m(ticker, **kw):
        fake.add_market(ticker, title=ticker, close_time=_CLOSE, **kw)

    # valid_basic — asks meet the limits on all three legs.
    m("VB-1", yes_ask=D("0.40"), yes_ask_size=500)
    m("VB-2", yes_ask=D("0.30"), yes_ask_size=300)
    m("VB-3", yes_ask=D("0.50"), yes_ask_size=100)
    # yes_and_no: one YES and one NO leg, so the chain proof covers both sides.
    m("TICKER-A", yes_ask=D("0.40"), yes_ask_size=50)
    m("TICKER-B", no_ask=D("0.55"), no_ask_size=70)
    # dup_ticker.
    m("DUP-1", yes_ask=D("0.40"), yes_ask_size=500)
    # thin_book: one contract resting against legs that ask for three (V11).
    m("ET-1", yes_ask=D("0.48"), yes_ask_size=1)
    m("ET-2", yes_ask=D("0.38"), yes_ask_size=1)
    # over_cap — 25 markets.
    for n in range(1, 26):
        m(f"T{n:02d}", yes_ask=D("0.40"), yes_ask_size=10)


@pytest.fixture
def world(tmp_path) -> World:
    """A tmp root with a config.toml (env=demo → live gate open; slots empty), a migrated
    Ledger, and a FakeKalshi seeded with every fixture's markets. The fake agent is wired
    in as ``attempt.runner``."""
    (tmp_path / "config.toml").write_text(
        '[kalshi]\nenv = "demo"\n\n[schedule]\nslots = []\n\n[attempt]\nwall_time_min = 5\n'
    )
    (tmp_path / "data").mkdir()
    settings = load_settings(root=tmp_path)
    settings.attempt.runner = _fake_agent_cmd()
    ledger = Ledger.open(settings.ledger_path)
    ledger.migrate()
    fake = FakeKalshi()
    _seed_markets(fake)
    w = World(settings=settings, ledger=ledger, fake=fake, root=tmp_path)
    yield w
    ledger.close()


# --------------------------------------------------------------------------- helpers
def _run(world: World, monkeypatch, fixture: str, *, cell: str = "static") -> str:
    monkeypatch.setenv("FAKE_FIXTURE", fixture)
    return run_attempt(world.ledger, world.fake, world.settings, cell=cell, now=_NOW)


def _rows(world: World, aid: str) -> dict:
    return {r["ticket_index"]: r for r in world.ledger.bets_for_attempt(aid)}


def _place_valid_basic(world: World, monkeypatch) -> str:
    """Run valid_basic to ``placed`` — all three legs real and filled (TQ-6)."""
    aid = _run(world, monkeypatch, "valid_basic")
    assert world.ledger.get_attempt(aid)["status"] == "placed"
    return aid


def _resolve_valid_basic(world: World) -> None:
    world.fake.resolve("VB-1", "yes")   # yes bet → win
    world.fake.resolve("VB-2", "no")    # yes bet, resolves no → loss
    world.fake.resolve("VB-3", "yes")   # yes bet → win


# --------------------------------------------------------------------------- (a) happy path
def test_valid_basic_placed_all_real_and_audits(world, monkeypatch):
    """(a) valid_basic → placed; 3 bet rows; every validated bet real in ticket order
    (Jul29 spec L4); one contract each (L3); fees/stakes §8."""
    aid = _run(world, monkeypatch, "valid_basic")
    assert aid == "A-0001"
    a = world.ledger.get_attempt(aid)
    assert a["status"] == "placed"
    assert a["session_exit"] == "ok"
    assert a["edge_class"] == "probability"

    rows = _rows(world, aid)
    assert set(rows) == {1, 2, 3}

    # No real subset any more: every validated bet is real, book depth irrelevant.
    assert (rows[1]["is_real"], rows[2]["is_real"], rows[3]["is_real"]) == (1, 1, 1)
    assert all(r["status"] == "filled" for r in rows.values())

    # One-contract sizing: stake == fill price, fee per §8 at 1 contract under the docs/14
    # D5 model — 0.07*P*(1-P) ceiled to 4dp, and all three products here are already exact:
    # 0.0168, 0.0147, 0.0175. (Under the pre-D5 cent ceiling every one of them read 0.0200.)
    assert (rows[1]["contracts"], rows[2]["contracts"], rows[3]["contracts"]) == (1, 1, 1)
    assert (rows[1]["fill_price"], rows[2]["fill_price"], rows[3]["fill_price"]) == \
        ("0.4000", "0.3000", "0.5000")
    assert (rows[1]["fee"], rows[2]["fee"], rows[3]["fee"]) == ("0.0168", "0.0147", "0.0175")
    assert (rows[1]["stake"], rows[2]["stake"], rows[3]["stake"]) == \
        ("0.4000", "0.3000", "0.5000")

    # Real orders placed for all three legs, in ticket order.
    assert [o["client_order_id"] for o in world.fake.orders_placed] == \
        [f"{aid}-B01", f"{aid}-B02", f"{aid}-B03"]
    assert all(o["time_in_force"] == "ioc" for o in world.fake.orders_placed)
    assert world.ledger.audit_events(event="cap_stop") == []  # 1.20 total, well under the caps

    # Attempt-owned audits + execution audits (one order_placed/order_result per real leg).
    events = {e["event"] for e in world.ledger.audit_events(limit=200)}
    assert {"attempt_launched", "session_end"} <= events
    assert len(world.ledger.audit_events(event="order_placed")) == 3
    assert len(world.ledger.audit_events(event="order_result")) == 3


# --------------------------------------------------------------------------- (c) rejections
def test_invalid_schema_is_ticket_invalid(world, monkeypatch):
    """(c) bets.json parses but violates Appendix D → whole-ticket V01 → ticket_invalid."""
    aid = _run(world, monkeypatch, "invalid_schema")
    a = world.ledger.get_attempt(aid)
    assert a["status"] == "ticket_invalid"
    assert world.ledger.bets_for_attempt(aid) == []   # no bet rows written


def test_dup_ticker_first_placed_second_rejected(world, monkeypatch):
    """(c) duplicate ticker → later index rejected V03; first still placed."""
    aid = _run(world, monkeypatch, "dup_ticker")
    assert world.ledger.get_attempt(aid)["status"] == "placed"
    rows = _rows(world, aid)
    assert rows[1]["status"] == "filled"
    assert rows[2]["status"] == "rejected" and rows[2]["reject_code"] == "V03"


def test_thin_book_all_rejected_no_bets(world, monkeypatch):
    """(c) every bet asks for three contracts against a one-deep book → V11 → no_bets."""
    aid = _run(world, monkeypatch, "thin_book")
    assert world.ledger.get_attempt(aid)["status"] == "no_bets"
    rows = _rows(world, aid)
    assert all(r["status"] == "rejected" and r["reject_code"] == "V11" for r in rows.values())
    assert world.fake.orders_placed == []


def test_over_cap_truncated_and_audited(world, monkeypatch):
    """(c) 25 bets → truncated to max_bets_per_attempt (20) + ticket_truncated audit."""
    aid = _run(world, monkeypatch, "over_cap")
    rows = _rows(world, aid)
    assert set(rows) == set(range(1, 21))        # indices 21–25 dropped, no rows
    assert world.ledger.get_attempt(aid)["status"] == "placed"
    ev = world.ledger.audit_events(event="ticket_truncated")
    assert ev and json.loads(ev[0]["detail"])["count"] == 5


# --------------------------------------------------------------------------- (d) crash / slow
def test_crash_without_ticket_is_failed(world, monkeypatch):
    """(d) session exits 1 with no ticket → failed."""
    aid = _run(world, monkeypatch, "crash")
    a = world.ledger.get_attempt(aid)
    assert a["status"] == "failed"
    assert a["session_exit"] == "error"
    assert not (world.settings.attempts_dir / aid / "ticket" / "bets.json").exists()


def test_an_auth_dead_session_is_infra_not_a_bad_ticket(world, monkeypatch):
    """(d) docs/14 D2 through the real subprocess: the fake agent emits the Aug-5 wedge's
    stream and the attempt is recorded as an infrastructure death, not a format failure."""
    aid = _run(world, monkeypatch, "auth_error")
    a = world.ledger.get_attempt(aid)
    assert a["status"] == "failed"
    assert a["session_exit"] == "error"
    assert a["error"] == "session_infra_auth (attempt)"
    assert a["cost_usd"] == "0.0000"
    assert world.ledger.audit_events(event="ticket_invalid") == []
    detail = json.loads(world.ledger.audit_events(event="phase_infra_error")[0]["detail"])
    assert detail["kind"] == "auth" and detail["error_code"] == "authentication_failed"


def test_slow_times_out_but_ticket_survives_contract_rule(world, monkeypatch):
    """(d) the session is killed by the wall clock, yet its finished ticket still counts
    (spec §9.4 contract rule): session_exit == timeout AND status == placed."""
    world.settings.attempt.wall_time_min = 2 / 60  # ~2 s wall clock; fixture sleeps far past it
    aid = _run(world, monkeypatch, "slow")
    a = world.ledger.get_attempt(aid)
    assert a["session_exit"] == "timeout"
    assert a["status"] == "placed"                 # contract rule honored despite the kill


# --------------------------------------------------------------------------- (f) tripwires
def test_impostor_order_halts_and_audits(world, monkeypatch):
    """(f) an order matching our id pattern but absent from the ledger → HALT + unknown_fill."""
    genesis_snapshot(world.ledger, world.fake)
    world.fake.add_impostor_order("A-9999-B01")
    counts = settle_once(world.ledger, world.fake, world.settings, now=_NOW)

    assert counts["impostors"] == 1
    assert world.settings.halt_path.exists()          # real HALT file created
    ev = world.ledger.audit_events(event="unknown_fill")
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["client_order_id"] == "A-9999-B01"

    clear_halt(world.settings)                         # as `resume` would
    assert not world.settings.halt_path.exists()


def test_personal_fill_observed_once_across_two_settles(world, monkeypatch):
    """(f) a personal (foreign-id) order → personal_fill_observed once, never a halt."""
    genesis_snapshot(world.ledger, world.fake)
    world.fake.add_personal_order("VB-1", side="no", count=3, price="0.30")

    c1 = settle_once(world.ledger, world.fake, world.settings, now=_NOW)
    c2 = settle_once(world.ledger, world.fake, world.settings, now=_NOW)

    assert c1["personal_seen"] == 1
    assert c2["personal_seen"] == 0                    # deduped by order_id in meta
    assert len(world.ledger.audit_events(event="personal_fill_observed")) == 1
    assert not world.settings.halt_path.exists()


# --------------------------------------------------------------------------- (i) idempotency
def test_settle_twice_is_a_no_op(world, monkeypatch):
    """(i) a second settle over fully-settled state does nothing (no double-settle)."""
    _place_valid_basic(world, monkeypatch)
    _resolve_valid_basic(world)
    first = settle_once(world.ledger, world.fake, world.settings, now=_NOW)
    assert first["bets_settled"] == 3 and first["attempts_settled"] == 1

    second = settle_once(world.ledger, world.fake, world.settings, now=_NOW)
    assert second == {
        # "errors" joined the counts with MP-2's per-bet isolation: a clean pass reports 0.
        "bets_settled": 0, "bets_voided": 0, "attempts_settled": 0, "groups_settled": 0,
        "reconcile_mismatches": 0, "personal_seen": 0, "personal_settled": 0,
        "impostors": 0,
        "shadows_scored": 0, "shadows_voided": 0, "canary_settled": 0,
        # docs/14 D12's hypothetical scoring: nothing to score here (all three legs filled,
        # so there is neither a no-fill nor a reject in the population).
        "nofills_scored": 0, "nofills_voided": 0,
        "rejects_scored": 0, "rejects_voided": 0,
        # docs/16 §5: a subset of bets_settled, and nothing here settled scalar.
        "scalars_settled": 0,
        "errors": 0,
    }


def test_crash_resume_settles_attempt_exactly_once(world, monkeypatch):
    """(i) crash mid-pipeline: settle with only 1/3 markets resolved leaves the attempt
    placed; resolving the rest and settling again completes it exactly once."""
    aid = _place_valid_basic(world, monkeypatch)

    world.fake.resolve("VB-1", "yes")                   # only one market resolved so far
    mid = settle_once(world.ledger, world.fake, world.settings, now=_NOW)
    assert mid["bets_settled"] == 1 and mid["attempts_settled"] == 0
    assert world.ledger.get_attempt(aid)["status"] == "placed"  # not yet terminal

    world.fake.resolve("VB-2", "no")
    world.fake.resolve("VB-3", "yes")
    resume = settle_once(world.ledger, world.fake, world.settings, now=_NOW)
    assert resume["bets_settled"] == 2 and resume["attempts_settled"] == 1
    assert world.ledger.get_attempt(aid)["status"] == "settled"

    # A third settle is a pure no-op — no double-settle on resume.
    again = settle_once(world.ledger, world.fake, world.settings, now=_NOW)
    assert again["bets_settled"] == 0 and again["attempts_settled"] == 0


# ===========================================================================
# (j) TQ-2 — the full chain: execute -> settle -> reconcile, against real writer output
#
# This is the proof the nightly balance audit never had. Every reconcile test in
# `test_reconcile.py` hand-writes the bets rows through the DAO to *mirror* what
# `execute.py` and `settle.py` would have written — which means the walk has only ever
# been checked against a hand-made copy of the writers' output, not the output. If the
# executor recorded a stake the exchange never charged, or settle wrote a payout in the
# wrong direction, the mirrored rows would agree with the mirror and the suite would stay
# green while the live loop HALTed at 23:00.
#
# Here nothing is mirrored: the fake agent emits a ticket, `run_attempt` validates it,
# `execute_attempt` places the orders (moving the fake's balance as the exchange would)
# and writes the rows, `settle_once` closes them from the resolved markets, and
# `reconcile_once` walks the balance and joins the fills. Both sides are in the same
# world, because a NO buy is quoted on the single YES-quoted book at 1-q and converted
# back on the way in — the one place a sign error would be invisible on the YES side.
# ===========================================================================
def _go_live(world: World) -> None:
    """Stamp the live era *before* any order is placed, at the balance the fake holds.

    `_live_genesis_if_due` does exactly this in the tick; the walk covers bets whose
    `placed_at` is at or after the stamp, so stamping late would silently exclude them
    and make any drift they caused invisible.
    """
    world.ledger.meta_set("live_genesis_ts", iso(utc_now() - timedelta(minutes=5)))
    world.ledger.meta_set("live_genesis_balance", str(world.fake.balance))


def test_execute_settle_reconcile_chain_drifts_zero_on_both_sides(world, monkeypatch):
    """(j) The chain proof. Five real bets, three YES singles and a YES/NO pair, placed,
    settled and reconciled to the cent, with no row written by hand."""
    _go_live(world)
    genesis_balance = world.fake.balance

    singles = _run(world, monkeypatch, "valid_basic")      # VB-1/2/3, all YES
    pair = _run(world, monkeypatch, "yes_and_no")          # TICKER-A yes + TICKER-B no
    assert world.ledger.get_attempt(singles)["status"] == "placed"
    assert world.ledger.get_attempt(pair)["status"] == "placed"

    rows = {**_rows(world, singles), **{10 + k: v for k, v in _rows(world, pair).items()}}
    assert all(r["is_real"] == 1 and r["status"] == "filled" for r in rows.values())
    # Both sides really are in this world, and each carries the coid the join needs.
    assert {r["side"] for r in rows.values()} == {"yes", "no"}
    assert all(r["client_order_id"] and r["order_id"] for r in rows.values())
    # The executor's rows already agree with the exchange's own money trail.
    spent = sum((D(r["stake"]) + D(r["fee"]) for r in rows.values()), D("0"))
    assert genesis_balance - world.fake.balance == spent

    # Wins and losses on both sides: VB-1 YES wins, VB-2 YES loses, VB-3 YES wins,
    # TICKER-A YES wins and (complementarily) TICKER-B's NO leg loses.
    _resolve_valid_basic(world)
    world.fake.resolve("TICKER-A", "yes")
    world.fake.resolve("TICKER-B", "yes")
    counts = settle_once(world.ledger, world.fake, world.settings, now=utc_now())
    assert counts["bets_settled"] == 5 and counts["reconcile_mismatches"] == 0
    assert counts["groups_settled"] == 0 and counts["attempts_settled"] == 2

    outcomes = {(r["ticker"], r["side"]): r["outcome"]
                for a in (singles, pair) for r in world.ledger.bets_for_attempt(a)}
    assert outcomes[("VB-1", "yes")] == "win" and outcomes[("VB-2", "yes")] == "loss"
    assert outcomes[("TICKER-A", "yes")] == "win"
    assert outcomes[("TICKER-B", "no")] == "loss"      # the NO leg, settled as a NO leg

    result = reconcile_once(world.ledger, world.fake, world.settings, now=utc_now())

    assert result["drift"] == D("0.0000")
    assert result["ok"] is True and result["failed_checks"] == []
    assert result["expected"] == result["actual"] == world.fake.balance
    # fills_match verified all five through the real orders->fills join, not by skipping.
    fm = result["checks"]["fills_match"]
    assert fm["ok"] is True and fm["n_bets"] == 5 and fm["n_skipped"] == 0
    assert fm["problems"] == [] and fm["priceless"] == []
    assert result["checks"]["settlements_covered"]["ok"] is True
    assert not world.settings.halt_path.exists()
    assert world.ledger.latest_reconciliation()["ok"] == 1

    # And the walk's own arithmetic is the ledger's: net P/L is the balance move.
    net = sum((D(r["pnl"]) for a in (singles, pair)
               for r in world.ledger.bets_for_attempt(a)), D("0"))
    assert world.fake.balance - genesis_balance == net


# ===========================================================================
# (k) docs/14 D12: the whole proposed book is scored, not just the legs that filled
#
# The A-0058 shape, built end to end: three validated legs, of which the exchange gives us
# one, refuses to fill a second, and the daily cap refuses to send a third. Before D12 the
# record held one filled leg and two status words, so the attempt was read against the one
# leg the market happened to give us, which is the adversely-selected subset, because a
# leg fills exactly when the market moved our way.
#
# The test's two halves are both load-bearing: every leg must carry its counterfactual, AND
# the money must be exactly as it was without any of it: same P/L, same balance, drift
# $0.0000. A counterfactual that can move the record is not a counterfactual.
# ===========================================================================
def _truncated_book(world: World, monkeypatch) -> str:
    """valid_basic against a world that fills one leg, refuses one, and caps out one.

    Cap arithmetic (projected at LIMIT price, charged in ticket order): the cap is $0.80, so
    VB-1 ($0.40) fits, VB-2 ($0.30) fits at $0.70, and VB-3 ($0.50) would reach $1.20 and is
    refused ``cap_daily``. VB-2's order is scripted to come back empty.
    """
    world.settings.stakes.daily_real_stake_cap = D("0.80")
    world.fake.set_order_behavior("VB-2", "no_fill")
    return _run(world, monkeypatch, "valid_basic")


def test_d12_the_unfilled_book_is_scored_and_shown_without_touching_the_money(
    world, monkeypatch
):
    """(k) one filled leg, one no-fill, one cap-rejected: settle scores the no-fill at its
    declared limit, every leg carries its own label, and the ledger's money is untouched
    — reconcile still walks to $0.0000."""
    _go_live(world)
    genesis_balance = world.fake.balance

    aid = _truncated_book(world, monkeypatch)

    rows = _rows(world, aid)
    assert rows[1]["status"] == "filled" and rows[1]["is_real"] == 1
    assert rows[2]["status"] == "no_fill" and rows[2]["is_real"] == 1
    assert rows[2]["contracts"] == 1 and rows[2]["fill_price"] is None
    assert rows[3]["status"] == "rejected" and rows[3]["reject_code"] == "cap_daily"
    # The cap-refused leg has no execution fields at all; its size lives in the D12 column.
    assert rows[3]["contracts"] is None and rows[3]["declared_contracts"] == 1
    assert [o["client_order_id"] for o in world.fake.orders_placed] == \
        [f"{aid}-B01", f"{aid}-B02"]              # VB-3 never reached the exchange
    cap = json.loads(world.ledger.audit_events(event="cap_stop")[0]["detail"])
    assert cap["code"] == "cap_daily" and cap["day_headroom"] == "0.1000"

    # VB-2 — the leg we did not get — resolves in our favour. This is the adverse selection
    # the record could not previously show: the fill we got won by 0.5832, the fill we were
    # refused would have won by more.
    _resolve_valid_basic(world)
    counts = settle_once(world.ledger, world.fake, world.settings, now=utc_now())

    assert counts["bets_settled"] == 1            # only the real position settled
    assert counts["nofills_scored"] == 1 and counts["nofills_voided"] == 0
    # The cap-refused leg is scored by the same pass, counted apart from the no-fills.
    assert counts["rejects_scored"] == 1 and counts["rejects_voided"] == 0
    assert counts["attempts_settled"] == 1
    rows = _rows(world, aid)
    assert (rows[1]["outcome"], rows[1]["pnl"]) == ("win", "0.5832")
    # The no-fill's counterfactual: 1 contract at its declared 0.30 limit, fee 0.0147.
    assert rows[2]["hypothetical_outcome"] == "loss"
    assert rows[2]["hypothetical_pnl"] == "-0.3147"
    assert (rows[2]["status"], rows[2]["outcome"], rows[2]["pnl"]) == ("no_fill", None, None)
    # The cap-rejected leg, scored the same way: 1 contract at its declared 0.50 limit, fee
    # 0.0175, and VB-3 resolved yes — the cap cost the attempt a winner, which is the whole
    # reason this row is now scored rather than left blank.
    assert rows[3]["hypothetical_outcome"] == "win"
    assert rows[3]["hypothetical_pnl"] == "0.4825"
    # Still a reject, still holding no position, still no money on the row.
    assert (rows[3]["status"], rows[3]["reject_code"]) == ("rejected", "cap_daily")
    assert (rows[3]["outcome"], rows[3]["pnl"], rows[3]["settled_at"]) == (None, None, None)
    assert (rows[3]["contracts"], rows[3]["stake"], rows[3]["fee"]) == (None, None, None)

    # MONEY TRUTH, unchanged. The exchange only ever charged us the one fill, the ledger
    # agrees to the cent, and the nightly walk still balances with the hypotheticals stored.
    spent = D(rows[1]["stake"]) + D(rows[1]["fee"])
    assert genesis_balance - world.fake.balance == spent - D("1.0000")   # the win paid out
    result = reconcile_once(world.ledger, world.fake, world.settings, now=utc_now())
    assert result["drift"] == D("0.0000")
    assert result["ok"] is True and result["failed_checks"] == []
    assert result["expected"] == result["actual"] == world.fake.balance
    fm = result["checks"]["fills_match"]
    assert fm["ok"] is True and fm["n_bets"] == 1 and fm["problems"] == []
    assert not world.settings.halt_path.exists()
    # And the era's net P/L is the filled leg alone — the counterfactual is nowhere in it.
    net = sum((D(r["pnl"]) for r in rows.values() if r["pnl"] is not None), D("0"))
    assert net == D("0.5832")
    assert world.fake.balance - genesis_balance == net


def test_d12_scoring_is_idempotent_across_ticks(world, monkeypatch):
    """(k/i) the tick settles every 15 minutes: a second pass must not re-score the
    no-fill, re-stamp its clock, or move the balance."""
    _go_live(world)
    aid = _truncated_book(world, monkeypatch)
    _resolve_valid_basic(world)

    first = settle_once(world.ledger, world.fake, world.settings, now=utc_now())
    scored_at = _rows(world, aid)[2]["hypothetical_scored_at"]
    balance_after_first = world.fake.balance

    later = utc_now() + timedelta(days=1)
    second = settle_once(world.ledger, world.fake, world.settings, now=later)

    assert first["nofills_scored"] == 1
    assert second["nofills_scored"] == 0 and second["nofills_voided"] == 0
    assert second["errors"] == 0
    rows = _rows(world, aid)
    assert rows[2]["hypothetical_scored_at"] == scored_at      # the original clock stands
    assert rows[2]["hypothetical_pnl"] == "-0.3147"
    assert world.fake.balance == balance_after_first
    assert reconcile_once(world.ledger, world.fake, world.settings, now=later)["ok"] is True


def test_the_chain_is_still_clean_after_a_second_settle_and_reconcile(world, monkeypatch):
    """The tick runs both steps repeatedly, so idempotence is part of the proof: a second
    pass over an already-settled world must move no money and find no drift."""
    _go_live(world)
    aid = _place_valid_basic(world, monkeypatch)
    _resolve_valid_basic(world)
    tonight = utc_now()
    settle_once(world.ledger, world.fake, world.settings, now=tonight)
    first = reconcile_once(world.ledger, world.fake, world.settings, now=tonight)
    assert first["ok"] is True

    # A distinct clock, because `reconciliations.run_at` is unique to the second and the
    # real second pass is a whole tick later.
    tomorrow = tonight + timedelta(days=1)
    balance_after_first = world.fake.balance
    counts = settle_once(world.ledger, world.fake, world.settings, now=tomorrow)
    second = reconcile_once(world.ledger, world.fake, world.settings, now=tomorrow)

    assert counts["bets_settled"] == 0 and counts["attempts_settled"] == 0
    assert world.fake.balance == balance_after_first
    assert second["ok"] is True and second["drift"] == D("0.0000")
    # EF-1's watermark: the second night re-proves nothing it already stands behind.
    assert second["checks"]["fills_match"]["n_skipped"] == 3
    assert second["checks"]["fills_match"]["n_bets"] == 0
    assert world.ledger.get_attempt(aid)["status"] == "settled"
    assert not world.settings.halt_path.exists()


# ------------------------------------------------- (k) market lenses over the board cache
def test_the_board_step_feeds_bt_series_which_then_answers_offline(world, monkeypatch):
    """(k) docs/14 C1/C2 end to end on the fake transport: the harness's refresh pulls the
    board through the same FakeKalshi the attempt trades against, and the toolkit a session
    runs then answers ``bt series`` from that file with the exchange unreachable.

    ``bt`` is exercised through its Typer app in-process, which is how every CLI behavior in
    this suite is verified; the env plumbing that hands a session ``BT_ROOT`` is pinned in
    tests/test_sessions.py.
    """
    from typer.testing import CliRunner

    from betting_agent import board, bt

    refresh = board.refresh_board_cache(world.ledger, world.fake, world.settings, now=_NOW)
    assert refresh["status"] == "refreshed"
    (gen,) = board.generations(world.settings.board_dir)

    monkeypatch.setenv("BT_ROOT", str(world.root))

    def unreachable(settings):
        raise AssertionError("bt series must answer from the snapshot, not the exchange")

    monkeypatch.setattr(bt, "_client", unreachable)

    res = CliRunner().invoke(bt.app, ["series", "--json"])
    assert res.exit_code == 0, res.stdout
    rows = json.loads(res.stdout)
    assert iso(_NOW) in res.stderr                              # the snapshot's own clock
    # Every seeded market is accounted for, and each single-market family is one row —
    # which is the flood fix at this scale (docs/12 §3).
    assert sum(r["n_markets"] for r in rows) == refresh["n_markets"]
    assert {r["series"] for r in rows} >= {"VB", "TICKER", "T01"}
    assert {r["series"]: r["n_markets"] for r in rows}["VB"] == 3

    # And the attempt pipeline still runs to `placed` in the same root, unaffected.
    aid = _run(world, monkeypatch, "valid_basic")
    assert world.ledger.get_attempt(aid)["status"] == "placed"
    assert board.generations(world.settings.board_dir) == [gen]  # no session wrote the cache


# ===========================================================================
# (l) docs/22 sections 8 and 13: one cohort, from nine attempts to two rankings
#
# This is the loop end to end, and nothing in it is mocked. Nine attempts run on one
# Eastern day across the four cells, through the real fake-agent subprocess: five place
# bets, one passes on a book too thin for its size, one writes an unusable ticket, one
# crashes before writing anything at all, and every one of them is a member of the day's
# cohort. The next morning the director reads that cohort, ranks it with no outcome known
# and writes the page the next day's cells will read. The markets then resolve, settle
# closes the legs and scores the refusals, and the morning after that the director reads
# the same cohort again with the money in front of it and ranks it a second time.
#
# What the two rankings are for is the comparison: the same nine attempts, ordered twice,
# against a third ordering the harness computes from the bet rows. That is why the
# retrospective row carries `realized` and why the ranking has to cover the cohort exactly.
# ===========================================================================
_COHORT_DAY = "2026-07-07"
_RUN_ONE = "2026-07-08"
_RUN_TWO = "2026-07-09"

# Nine slots, the four cells, and the ticket each attempt writes. The mix is deliberate:
# the cohort has to survive a pass, an unusable ticket and a session that never wrote one.
_COHORT = (
    ("01:00", "baseline", "valid_basic"),
    ("03:40", "static", "valid_basic"),
    ("06:20", "static", "yes_and_no"),
    ("09:00", "static", "dup_ticker"),
    ("11:40", "director", "valid_basic"),
    ("14:20", "director", "thin_book"),
    ("17:00", "director", "invalid_schema"),
    ("19:40", "director", "crash"),
    ("22:20", "focused", "valid_basic"),
)

# Every market the cohort touched, including the two it was refused on: a refused leg is
# scored hypothetically, and a cohort is not complete until it has been.
_RESOLUTIONS = (
    ("VB-1", "yes"), ("VB-2", "no"), ("VB-3", "yes"),
    ("TICKER-A", "yes"), ("TICKER-B", "yes"),
    ("DUP-1", "yes"), ("ET-1", "no"), ("ET-2", "no"),
)


def _run_cohort(world: World, monkeypatch) -> list[str]:
    ids = []
    for hhmm, cell, fixture in _COHORT:
        monkeypatch.setenv("FAKE_FIXTURE", fixture)
        ids.append(run_attempt(world.ledger, world.fake, world.settings,
                               slot=f"slot:{_COHORT_DAY}/{hhmm}", cell=cell, now=_NOW))
    return ids


def _director_run(world: World, monkeypatch, run_date: str) -> dict:
    monkeypatch.setenv("FAKE_FIXTURE", "director")
    return run_director(world.ledger, world.settings, run_date=run_date,
                        client=world.fake, now=parse_iso(f"{run_date}T04:00:00Z"))


def _reviews(world: World, kind: str) -> list[dict]:
    return world.ledger.conn.execute(
        "SELECT * FROM attempt_reviews WHERE kind=? ORDER BY rank", (kind,)
    ).fetchall()


def _bt_past_attempt(world: World, monkeypatch, attempt_id: str) -> str:
    from typer.testing import CliRunner

    from betting_agent import bt

    monkeypatch.setenv("BT_ROOT", str(world.root))
    monkeypatch.setenv("BT_PAST", "on")
    res = CliRunner().invoke(bt.app, ["past", "attempt", attempt_id])
    assert res.exit_code == 0, res.stdout
    return res.stdout


def test_one_cohort_is_run_directed_settled_and_directed_again(world, monkeypatch):
    """(l) The phase-three acceptance: two consecutive runs, both valid, the second's
    retrospective ranking covering exactly the first day's cohort, and two review rows
    per attempt."""
    cohort = _run_cohort(world, monkeypatch)

    statuses = [world.ledger.get_attempt(a)["status"] for a in cohort]
    assert statuses == ["placed", "placed", "placed", "placed", "placed", "no_bets",
                        "ticket_invalid", "failed", "placed"]
    assert history.cohort(world.ledger, _COHORT_DAY) == cohort
    # The cells the attempts ran: director and focused have no page to read on day one, so
    # they render as static and say so, which is what the first day of an era looks like.
    assert [world.ledger.get_attempt(a)["cell"] for a in cohort] == \
        [c for _h, c, _f in _COHORT]
    assert {world.ledger.get_attempt(a)["cell_effective"] for a in cohort} == \
        {"baseline", "static"}

    # ---------------------------------------------------------------- the first run
    first = _director_run(world, monkeypatch, _RUN_ONE)
    assert first == {"run_id": f"D-{_RUN_ONE}", "status": "valid", "error": None}

    run = world.ledger.director_run(f"D-{_RUN_ONE}")
    assert run["cohort_date"] == _COHORT_DAY
    assert run["page_hash"] and "## Standing direction" in run["page_md"]
    sets = json.loads(run["sets_json"])
    assert 6 <= len(sets["balanced"]) <= 12
    assert set(sets["balanced"]) <= set(cohort)
    # The session is on the record as a director session, and it is the run's own.
    session = world.ledger.conn.execute(
        "SELECT * FROM sessions WHERE session_id=?", (run["session_id"],)).fetchone()
    assert session["kind"] == "director" and session["attempt_id"] is None
    assert session["exit"] == "ok" and session["model"] == "claude-fable-5-1"

    prospective = _reviews(world, "prospective")
    assert [r["attempt_id"] for r in prospective] == sorted(cohort)
    assert [r["rank"] for r in prospective] == list(range(1, 10))
    assert {r["cohort_size"] for r in prospective} == {9}
    assert {r["cohort_date"] for r in prospective} == {_COHORT_DAY}
    assert json.loads(
        world.ledger.cohort_review(_COHORT_DAY, "prospective")["ranking"]) == sorted(cohort)
    assert world.ledger.cohort_review(_COHORT_DAY, "prospective")["realized"] is None

    # Nothing had settled yet, so there was nothing to review retrospectively.
    assert _reviews(world, "retrospective") == []

    # ------------------------------------------------- day two reads what the run wrote
    # The loop closes here: an attempt in the director cell renders the page and the
    # balanced set the run above stored, and its row records what it was shown.
    monkeypatch.setenv("FAKE_FIXTURE", "valid_basic")
    day_two = run_attempt(world.ledger, world.fake, world.settings,
                          slot=f"slot:{_RUN_ONE}/01:00", cell="director",
                          now=parse_iso(f"{_RUN_ONE}T05:00:00Z"))
    row = world.ledger.get_attempt(day_two)
    assert row["cell"] == "director" and row["cell_effective"] == "director"
    assert len(row["direction_hash"] or "") == 12
    assert json.loads(row["example_ids"]) == json.loads(run["sets_json"])["balanced"]
    context = (world.settings.attempts_dir / day_two / "CONTEXT.md").read_text()
    assert context.startswith("## Direction")
    assert "## Past attempts (chosen for today)" in context
    # The direction is the page's own words and the examples are the run's own set.
    assert "Keep sweeping the strait ladders" in context
    assert run["page_md"].splitlines()[1] in context
    assert cohort[0] in context

    # ---------------------------------------------------------------- the settlement
    for ticker, outcome in _RESOLUTIONS:
        world.fake.resolve(ticker, outcome)
    counts = settle_once(world.ledger, world.fake, world.settings, now=utc_now())
    assert counts["attempts_settled"] == 7        # six placers of day one, and day two's
    assert counts["rejects_scored"] == 3          # one V03 and the two V11 legs
    assert all(history.is_complete(world.ledger, a) for a in cohort)

    # ---------------------------------------------------------------- the second run
    second = _director_run(world, monkeypatch, _RUN_TWO)
    assert second["status"] == "valid"
    second_run = world.ledger.director_run(f"D-{_RUN_TWO}")
    assert "## Standing direction" in second_run["page_md"]
    assert second_run["page_hash"] == director_mod._sha12(second_run["page_md"])
    assert 6 <= len(json.loads(second_run["sets_json"])["focused"]) <= 12

    retrospective = _reviews(world, "retrospective")
    assert [r["attempt_id"] for r in retrospective] == sorted(cohort)
    assert {r["cohort_date"] for r in retrospective} == {_COHORT_DAY}
    assert {r["run_id"] for r in retrospective} == {f"D-{_RUN_TWO}"}
    # The second run ranks its own day prospectively, which is the one attempt day two ran.
    assert [r["attempt_id"] for r in _reviews(world, "prospective")
            if r["cohort_date"] == _RUN_ONE] == [day_two]

    # Two rows per attempt of the first day's cohort: the phase-three acceptance.
    counted = world.ledger.conn.execute(
        "SELECT attempt_id, COUNT(*) AS n FROM attempt_reviews GROUP BY attempt_id"
    ).fetchall()
    assert {r["attempt_id"] for r in counted} == {*cohort, day_two}
    assert {r["n"] for r in counted if r["attempt_id"] in cohort} == {2}
    assert [r["n"] for r in counted if r["attempt_id"] == day_two] == [1]

    # And the ordering the harness computed beside the one the director wrote.
    row = world.ledger.cohort_review(_COHORT_DAY, "retrospective")
    assert json.loads(row["ranking"]) == sorted(cohort)
    realized = json.loads(row["realized"])
    assert realized == director_mod.realized_order(world.ledger, cohort)
    assert sorted(realized) == sorted(cohort)
    # The attempts that made money come first. The three that made none come last, in the
    # order their ids fall, because net and net per contract are zero for all three: the
    # pass on a book too thin to fill, the unusable ticket, and the session that crashed.
    assert realized[-3:] == [cohort[5], cohort[6], cohort[7]]
    nets = {a: history.outcome_record(world.ledger, a)["totals"]["net"] for a in cohort}
    assert nets[realized[0]] == max(nets.values())


def test_the_second_run_reads_the_first_days_outcomes_and_the_first_days_page(
    world, monkeypatch
):
    """(l) What the second run was given: a settled cohort with its money, and the page
    its predecessor wrote."""
    cohort = _run_cohort(world, monkeypatch)
    _director_run(world, monkeypatch, _RUN_ONE)
    for ticker, outcome in _RESOLUTIONS:
        world.fake.resolve(ticker, outcome)
    settle_once(world.ledger, world.fake, world.settings, now=utc_now())

    workspace = director_mod.build_workspace(
        world.ledger, world.settings, run_date=_RUN_TWO, client=world.fake,
        now=parse_iso(f"{_RUN_TWO}T04:00:00Z"))

    assert workspace.settled == {_COHORT_DAY: cohort}
    assert workspace.cohort == []                 # nothing ran on the day between
    folder = workspace.work_dir / "settled" / _COHORT_DAY / cohort[0]
    outcomes = (folder / "outcomes.md").read_text()
    assert "VB-1 yes @0.4000 x1 · settled · win · profit 0.5832 · fee 0.0168" in outcomes
    assert "Net" in outcomes and "VB: net" in outcomes
    # The previous page is in front of it, named by the run date that wrote it.
    assert (workspace.work_dir / "pages" / f"{_RUN_ONE}.md").read_text() == \
        world.ledger.director_run(f"D-{_RUN_ONE}")["page_md"]
    # A refused leg is in the record too, with the reason it was refused.
    refused = (workspace.work_dir / "settled" / _COHORT_DAY / cohort[5] / "legs.md").read_text()
    assert "refused:" in refused and "ET-1" in refused


def test_bt_past_attempt_prints_the_ticket_the_legs_the_outcome_and_both_paragraphs(
    world, monkeypatch
):
    """(l) docs/22 section 8.6: the retro is a view assembled at read time from the ticket,
    the bet rows and the two director paragraphs."""
    cohort = _run_cohort(world, monkeypatch)
    _director_run(world, monkeypatch, _RUN_ONE)
    for ticker, outcome in _RESOLUTIONS:
        world.fake.resolve(ticker, outcome)
    settle_once(world.ledger, world.fake, world.settings, now=utc_now())
    _director_run(world, monkeypatch, _RUN_TWO)

    printed = _bt_past_attempt(world, monkeypatch, cohort[1])

    assert "## edge_claim.md" in printed and "## hypothesis.md" in printed
    assert "## Legs" in printed and "VB-1 yes @0.4000 ×1 → filled, won, +$0.58" in printed
    assert "## Outcome" in printed and "of 3 legs won" in printed
    assert "## Session summary" in printed
    ranks = {r["kind"]: r["rank"] for r in world.ledger.conn.execute(
        "SELECT kind, rank FROM attempt_reviews WHERE attempt_id=?", (cohort[1],))}
    assert f"prospective (rank {ranks['prospective']} of 9, cohort {_COHORT_DAY})" in printed
    assert f"retrospective (rank {ranks['retrospective']} of 9, cohort {_COHORT_DAY})" \
        in printed
