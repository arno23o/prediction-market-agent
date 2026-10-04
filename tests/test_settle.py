"""Settlement + shared-account reconciliation (spec §10)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from betting_agent.config import load_settings
from betting_agent.harness import settle
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.kalshi.types import Fill
from betting_agent.ledger.db import Ledger
from betting_agent.moneymath import bet_pnl, q4

CLOSE = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path):
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    settings = load_settings(root=tmp_path)
    fake = FakeKalshi()
    yield ledger, fake, settings
    ledger.close()


def _placed_attempt(lg, edge_class="probability"):
    _, aid = lg.create_attempt(
        env="prod", model="claude-sonnet-5", effort="high", memory_mode="on",
        prompt_version="p1", toolkit_version="0.1.0", workspace_path="/ws",
        edge_class=edge_class,
    )
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    return aid


def _filled_bet(lg, aid, idx, ticker, *, side="yes", limit="0.42", fill="0.42",
                contracts=2, fee="0.04", is_real=0, coid=None, group_id=None,
                category=None):
    bet_id = f"{aid}-B{idx:02d}"
    lg.insert_bet(
        bet_id=bet_id, attempt_id=aid, ticket_index=idx, ticker=ticker, side=side,
        limit_price=D(limit), model_prob=D("0.55"), rationale="r", status="filled",
        # ``fee=None`` is a real shape (a row whose receipt never carried one), so the
        # helper has to be able to write NULL rather than coerce it to zero.
        contracts=contracts, fill_price=D(fill),
        fee=D(fee) if fee is not None else None,
        # What ``execute._filled`` writes. This used to be a flat 1.00 that nothing read;
        # settle books P/L against the stored stake since 2026-09-27, so it has to be real.
        stake=q4(D(str(contracts)) * D(fill)),
        is_real=is_real, client_order_id=coid, group_id=group_id, category=category,
        placed_at="2026-07-07T12:00:00Z",
    )
    return bet_id


def _fractional_fill_payloads(ticker, price, pieces=("0.28", "0.34", "0.38")):
    """Raw ``/portfolio/fills`` rows in the live shape — fixed-point ``count_fp`` strings
    and ``*_dollars`` prices. The A-0054-B01 pieces by default."""
    return [
        {"ticker": ticker, "side": "yes", "count_fp": c, "yes_price_dollars": price,
         "order_id": "OID-A0054", "is_taker": True,
         "created_time": "2026-08-02T20:15:00Z"}
        for c in pieces
    ]


class _LiveFillsClient:
    """A client whose coid join answers from RAW payloads, parsed the way the real
    ``KalshiClient.find_fills_by_client_order_id`` parses them. Everything else delegates
    to the fake — this exists to drive the join with production's own bytes."""

    def __init__(self, inner, coid, payloads):
        self._inner, self._coid, self._payloads = inner, coid, payloads

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def find_fills_by_client_order_id(self, coid):
        if coid != self._coid:
            return self._inner.find_fills_by_client_order_id(coid)
        return [Fill.from_api(p, client_order_id=coid) for p in self._payloads]


class _FlakyMarketClient:
    """The fake, except ``get_market(ticker)`` raises its first ``fails`` calls (MP-1)."""

    def __init__(self, inner, ticker, fails):
        self._inner, self._ticker, self._fails = inner, ticker, fails
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_market(self, ticker):
        if ticker == self._ticker:
            self.calls += 1
            if self.calls <= self._fails:
                raise RuntimeError("429 slow down")
        return self._inner.get_market(ticker)


class _BrokenJoinClient:
    """The fake, except the coid join blows up for exactly one bet (MP-2)."""

    def __init__(self, inner, coid):
        self._inner, self._coid = inner, coid

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def find_fills_by_client_order_id(self, coid):
        if coid == self._coid:
            raise RuntimeError("fills endpoint 500")
        return self._inner.find_fills_by_client_order_id(coid)


# --------------------------------------------------------------------------- win/loss/void
def test_win_pnl_and_attempt_settles(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.42", contracts=2, fee="0.04")
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["status"] == "settled" and b["outcome"] == "win"
    assert b["pnl"] == "1.1200"  # 2*(1-0.42) - 0.04
    assert counts["bets_settled"] == 1
    assert counts["attempts_settled"] == 1
    assert lg.get_attempt(aid)["status"] == "settled"


def test_loss_pnl(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.42", contracts=2, fee="0.04")
    fake.resolve("T", "no")  # yes bet, resolves no -> loss

    settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["outcome"] == "loss"
    assert b["pnl"] == "-0.8800"  # -(2*0.42) - 0.04


def test_void_zeroes_pnl_and_fee(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.42", contracts=2, fee="0.04")
    fake.resolve("T", "void")

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["status"] == "voided" and b["outcome"] == "void"
    assert b["pnl"] == "0.0000" and b["fee"] == "0.0000"  # fee refunded on void
    assert counts["bets_voided"] == 1


def test_open_market_is_not_settled(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T")
    # market never resolved -> still 'active'
    counts = settle.settle_once(lg, fake, st)
    assert counts["bets_settled"] == 0
    assert lg.bets_for_attempt(aid)[0]["status"] == "filled"
    assert lg.get_attempt(aid)["status"] == "placed"


# --------------------------------------------------------------------------- reconcile
def test_real_bet_reconcile_keeps_exchange_fee_and_audits_mismatch(env):
    """The recorded fee came from the exchange at placement and is the truth — the §8
    model (which reproduces the live record exactly, but is a reconstruction rather than a
    receipt) is only the mismatch-audit baseline."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.50"), yes_ask_size=10)
    # A receipt of 0.02 against the §8 model's 0.0350 for 2@0.50: a 1.5¢ gap to audit.
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.50", contracts=2, fee="0.02",
                is_real=1, coid=coid)
    # The orders->fills join returns the actual fills for our client_order_id.
    fake.add_personal_fill("T", "yes", 2, D("0.50"), client_order_id=coid)
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["fee"] == "0.0200"  # exchange-charged fee kept, NOT the model estimate
    assert b["pnl"] == "0.9800"  # 2*(1-0.50) - 0.02
    assert counts["reconcile_mismatches"] == 1  # the divergence is still audited
    ev = lg.audit_events(event="reconcile_mismatch")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["recorded"]["fee"] == "0.0200"
    assert detail["actual"]["fee_model_estimate"] == "0.0350"
    assert detail["actual"]["fee_used"] == "0.0200"


def test_real_bet_reconcile_falls_back_to_model_fee_when_count_changed(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.50"), yes_ask_size=10)
    # Recorded 2 contracts, but the fills join finds only 1 — the recorded fee no longer
    # describes the position, so the model estimate for the ACTUAL fill is used.
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.50", contracts=2, fee="0.02",
                is_real=1, coid=coid)
    fake.add_personal_fill("T", "yes", 1, D("0.50"), client_order_id=coid)
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["contracts"] == 1
    assert b["fee"] == "0.0175"  # model fee for 1@0.50 = 0.0175, already on the 4dp grid
    assert b["pnl"] == "0.4825"  # 1*(1-0.50) - 0.0175
    assert counts["reconcile_mismatches"] == 1  # count mismatch audited


def test_real_bet_reconcile_uses_the_model_fee_when_none_was_ever_recorded(env):
    """The other arm of the same fallback: no count change, but no recorded fee either.

    A row can reach settlement with a NULL fee — a legacy paper-era row promoted to real,
    or a fill whose receipt never carried one. There is nothing to keep, so the §8 estimate
    stands in; the alternative is treating the trade as free, which understates every cost
    metric and drifts the balance walk by exactly the fee.
    """
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.50"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.50", contracts=2, fee=None,
                is_real=1, coid=coid)
    fake.add_personal_fill("T", "yes", 2, D("0.50"), client_order_id=coid)
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["fee"] == "0.0350"                    # §8 model for 2 @ 0.50
    assert b["pnl"] == "0.9650"                    # 2 x (1 - 0.50) - 0.035
    assert counts["reconcile_mismatches"] == 1     # a NULL fee is itself the divergence
    detail = json.loads(lg.audit_events(event="reconcile_mismatch")[0]["detail"])
    assert detail["recorded"]["fee"] == "0" and detail["actual"]["fee_used"] == "0.0350"


def test_real_bet_reconcile_price_override_changes_pnl(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.40", contracts=1, fee="0.02",
                is_real=1, coid=coid)
    fake.add_personal_fill("T", "yes", 1, D("0.60"), client_order_id=coid)  # true fill 0.60
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, fake, st)
    b = lg.bets_for_attempt(aid)[0]
    assert b["fill_price"] == "0.6000"
    assert b["pnl"] == "0.3800"  # 1*(1-0.60) - 0.02
    assert counts["reconcile_mismatches"] == 1


def test_real_bet_no_mismatch_when_fills_agree(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.50"), yes_ask_size=10)
    # "Fills agree" includes the fee: model-exact D5 value at 2 @ 0.50 is 0.0350.
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.50", contracts=2, fee="0.035",
                is_real=1, coid=coid)
    fake.add_personal_fill("T", "yes", 2, D("0.50"), client_order_id=coid)
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, fake, st)
    assert counts["reconcile_mismatches"] == 0
    assert lg.audit_events(event="reconcile_mismatch") == []
    assert lg.bets_for_attempt(aid)[0]["pnl"] == "0.9650"


def test_a_real_bet_whose_fills_join_finds_nothing_settles_on_its_recorded_values(env):
    """TQ-3, the phantom-fill case: a real ``filled`` bet the coid join answers EMPTY for.

    Settlement trusts the placement record and says nothing. That is the contract, and
    this test exists to pin it deliberately rather than by omission, because the silence
    is the surprising part: an empty join is indistinguishable here from a fills endpoint
    that is briefly lying, and the choice was to treat the order response — which the
    exchange gave us at placement, with an order id — as the better evidence.

    The alternative (zeroing the position, or auditing a mismatch) would be worse in the
    common case: the fills endpoint lags placement by seconds to minutes, and reconcile at
    23:00 is the check that actually has the standing to complain, with a whole day of
    lag budget behind it. Note what that means, though — if the join is empty because the
    position genuinely is not there, this pass records a P/L for a bet that does not
    exist, and only the nightly balance walk catches it.
    """
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    # No order and no fill are ever booked for this coid, so the join returns [].
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.42", contracts=2, fee="0.04",
                is_real=1, coid=coid)
    assert fake.find_fills_by_client_order_id(coid) == []
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["status"] == "settled" and b["outcome"] == "win"
    assert (b["contracts"], b["fill_price"], b["fee"]) == (2, "0.4200", "0.0400")
    assert b["pnl"] == "1.1200"                    # 2 x (1 - 0.42) - 0.04, on the record
    assert counts["bets_settled"] == 1
    assert counts["reconcile_mismatches"] == 0     # silence, not a count_mismatch of 2->0
    assert lg.audit_events(event="reconcile_mismatch") == []
    assert lg.audit_events(event="priceless_fills") == []
    assert lg.audit_events(event="settle_bet_error") == []


def test_a_settled_bet_is_never_settled_a_second_time(env):
    """MP-8: the bet-level double-settle no-op, stated explicitly.

    The tick runs this pass every 15 minutes, so a settled bet is walked past ~96 times a
    day. The guard is the ``status='filled'`` predicate in ``filled_unsettled_bets``, and
    it is load-bearing in two directions: re-settling would re-credit ``pnl`` (double-
    counting the payout in every report) and it would re-run ``_reconcile_real``, which
    on a bet whose fills the exchange has since aged out would rewrite a correct position
    into a phantom. The second pass here uses a LATER clock, so any re-write shows up as a
    moved ``settled_at`` rather than having to be inferred.
    """
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.create_order("T", "yes", D("0.40"), 1, coid)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.40", contracts=1, fee="0.0168",
                is_real=1, coid=coid)
    fake.resolve("T", "yes")

    first_at = datetime(2026, 7, 9, 13, 0, tzinfo=UTC)
    counts_1 = settle.settle_once(lg, fake, st, now=first_at)
    row_1 = dict(lg.bets_for_attempt(aid)[0])
    assert counts_1["bets_settled"] == 1
    assert row_1["settled_at"] == "2026-07-09T13:00:00Z"

    counts_2 = settle.settle_once(lg, fake, st, now=first_at + timedelta(hours=1))

    assert counts_2["bets_settled"] == 0 and counts_2["errors"] == 0
    assert dict(lg.bets_for_attempt(aid)[0]) == row_1   # byte-for-byte untouched
    assert lg.audit_events(event="reconcile_mismatch") == []
    assert lg.get_attempt(aid)["status"] == "settled"   # and not transitioned twice


# --------------------------------------------------------------------------- attempt gating
def test_attempt_settles_only_when_all_non_rejected_terminal(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T1", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    fake.add_market("T2", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T1")
    _filled_bet(lg, aid, 2, "T2")
    # a rejected bet must NOT block completion
    lg.insert_bet(bet_id=f"{aid}-B03", attempt_id=aid, ticket_index=3, ticker="T3",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
                  status="rejected", reject_code="V09")

    fake.resolve("T1", "yes")
    counts1 = settle.settle_once(lg, fake, st)  # only T1 resolved
    assert counts1["attempts_settled"] == 0
    assert lg.get_attempt(aid)["status"] == "placed"

    fake.resolve("T2", "yes")
    counts2 = settle.settle_once(lg, fake, st)  # now both terminal
    assert counts2["attempts_settled"] == 1
    assert lg.get_attempt(aid)["status"] == "settled"


def test_no_fill_leg_counts_as_terminal(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T1", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T1")
    lg.insert_bet(bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T2",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
                  status="no_fill")
    fake.resolve("T1", "yes")
    counts = settle.settle_once(lg, fake, st)
    assert counts["attempts_settled"] == 1
    assert lg.get_attempt(aid)["status"] == "settled"


# --------------------------------------------------------------------------- groups
def test_group_realized_pnl_and_settled(env):
    lg, fake, st = env
    aid = _placed_attempt(lg, edge_class="structural")
    gid = f"{aid}-G1"
    scenarios = json.dumps([
        {"name": "event happens", "outcomes": {"A": "win", "B": "loss"}},
        {"name": "event doesn't", "outcomes": {"A": "loss", "B": "win"}},
    ])
    lg.insert_group(gid, aid, scenarios, D("0.01"))
    # MP-3: settlement candidates are 'filled'/'broken' groups, never 'pending' — so the
    # fixture now records what the executor records once both legs land.
    lg.set_group(gid, status="filled")
    fake.add_market("A", title="m", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.add_market("B", title="m", close_time=CLOSE, no_ask=D("0.55"), no_ask_size=10)
    _filled_bet(lg, aid, 1, "A", side="yes", fill="0.40", contracts=1, fee="0.02",
                group_id=gid)
    _filled_bet(lg, aid, 2, "B", side="no", fill="0.55", contracts=1, fee="0.02",
                group_id=gid)
    # "event happens": A resolves yes (yes-leg wins), B resolves yes (no-leg loses)
    fake.resolve("A", "yes")
    fake.resolve("B", "yes")

    counts = settle.settle_once(lg, fake, st)

    g = lg.groups_for_attempt(aid)[0]
    assert g["status"] == "settled"
    assert g["realized_pnl"] == "0.0100"  # 0.58 + (-0.57)
    assert counts["groups_settled"] == 1


def test_broken_group_keeps_status_but_records_realized(env):
    lg, fake, st = env
    aid = _placed_attempt(lg, edge_class="structural")
    gid = f"{aid}-G1"
    lg.insert_group(gid, aid, json.dumps([]), D("0.01"))
    lg.set_group(gid, status="broken")
    fake.add_market("A", title="m", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "A", side="yes", fill="0.40", contracts=1, fee="0.02",
                group_id=gid)
    # second leg never filled (broken group), recorded as no_fill
    lg.insert_bet(bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="B",
                  side="no", limit_price=D("0.55"), model_prob=D("0.43"), rationale="r",
                  status="no_fill", group_id=gid)
    fake.resolve("A", "yes")

    counts = settle.settle_once(lg, fake, st)

    g = lg.groups_for_attempt(aid)[0]
    assert g["status"] == "broken"  # stays broken
    assert g["realized_pnl"] == "0.5800"  # only the filled leg contributes
    assert counts["groups_settled"] == 0


# --------------------------------------------------------------------------- shared account
def test_impostor_order_halts_and_audits(env, monkeypatch):
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    halts = []
    monkeypatch.setattr(settle, "_set_halt", lambda settings, reason: halts.append(reason))
    oid = fake.add_impostor_order("A-9999-B01")

    counts = settle.settle_once(lg, fake, st)

    assert counts["impostors"] == 1
    assert halts and "A-9999-B01" in halts[0]
    ev = lg.audit_events(event="unknown_fill")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["client_order_id"] == "A-9999-B01"
    assert detail["order_id"] == oid


def test_our_order_is_not_an_impostor(env, monkeypatch):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    _filled_bet(lg, aid, 1, "T", is_real=1, coid=coid)  # registers coid in ledger
    settle.genesis_snapshot(lg, fake)
    monkeypatch.setattr(settle, "_set_halt",
                        lambda *a: pytest.fail("must not halt on our own order"))
    # the system order lands in the account's order list via the normal placement path
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    fake.create_order("T", "yes", D("0.42"), 2, coid)

    counts = settle.settle_once(lg, fake, st)
    assert counts["impostors"] == 0
    assert counts["personal_seen"] == 0
    assert lg.audit_events(event="unknown_fill") == []


def test_personal_order_audited_once_across_two_runs(env, monkeypatch):
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    monkeypatch.setattr(settle, "_set_halt", lambda *a: pytest.fail("personal orders never halt"))
    fake.add_personal_order("T", side="no", count=3, price="0.30")  # no coid -> personal

    c1 = settle.settle_once(lg, fake, st)
    c2 = settle.settle_once(lg, fake, st)

    assert c1["personal_seen"] == 1
    assert c2["personal_seen"] == 0  # deduped by order_id in meta
    assert len(lg.audit_events(event="personal_fill_observed")) == 1


def test_foreign_coid_order_is_personal_not_impostor(env, monkeypatch):
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    monkeypatch.setattr(settle, "_set_halt", lambda *a: pytest.fail("foreign coid never halts"))
    fake.add_personal_order("T", client_order_id="manual-app-1a2b")  # non-matching coid

    counts = settle.settle_once(lg, fake, st)
    assert counts["impostors"] == 0 and counts["personal_seen"] == 1
    assert lg.audit_events(event="unknown_fill") == []
    assert len(lg.audit_events(event="personal_fill_observed")) == 1


def test_pre_genesis_order_is_never_scanned(env, monkeypatch):
    lg, fake, st = env
    fake.add_personal_order("T", ts=datetime.now(UTC) - timedelta(days=2))
    fake.add_impostor_order("A-9999-B01", ts=datetime.now(UTC) - timedelta(days=2))
    settle.genesis_snapshot(lg, fake)  # genesis AFTER the old activity
    monkeypatch.setattr(settle, "_set_halt", lambda *a: pytest.fail("pre-genesis must be ignored"))

    counts = settle.settle_once(lg, fake, st)
    assert counts["impostors"] == 0 and counts["personal_seen"] == 0
    assert lg.audit_events(event="unknown_fill") == []
    assert lg.audit_events(event="personal_fill_observed") == []


def test_scan_skipped_when_genesis_absent(env, monkeypatch):
    lg, fake, st = env
    # genesis_snapshot deliberately NOT called
    monkeypatch.setattr(settle, "_set_halt", lambda *a: pytest.fail("scan must be skipped"))
    fake.add_impostor_order("A-9999-B01")
    fake.add_personal_order("T")

    counts = settle.settle_once(lg, fake, st)
    assert counts["impostors"] == 0 and counts["personal_seen"] == 0
    assert lg.audit_events(event="unknown_fill") == []
    assert lg.audit_events(event="personal_fill_observed") == []


def test_genesis_snapshot_is_idempotent(env):
    lg, fake, st = env
    fake.set_balance("10.0000")
    settle.genesis_snapshot(lg, fake)
    first = lg.meta_get("genesis_ts")
    snap = json.loads(lg.meta_get("genesis_snapshot"))
    assert snap["balance"] == "10.0000" and snap["fill_count"] == 0
    settle.genesis_snapshot(lg, fake)  # second call must not overwrite
    assert lg.meta_get("genesis_ts") == first


# --------------------------------------------------------------------------- shadows (L16)
def _shadow(lg, aid, ticker, *, side="yes", limit="0.42", idx=1, candidate="C2"):
    """Seed one open shadow bet by hand.

    Raw SQL because nothing writes a new shadow bet since docs/22 section 4.6; what settle
    still does, and what these tests cover, is scoring the rows already in the ledger.
    """
    sid = f"{aid}-{candidate}-S{idx:02d}"
    lg.conn.execute(
        "INSERT INTO shadow_bets (shadow_bet_id, attempt_id, candidate_id, ticker, "
        "side, limit_price, model_prob) VALUES (?,?,?,?,?,?,?)",
        (sid, aid, candidate, ticker, side, str(q4(D(limit))), "0.6000"),
    )
    lg.conn.commit()
    return sid


def _shadow_row(lg, sid):
    return lg.conn.execute(
        "SELECT * FROM shadow_bets WHERE shadow_bet_id=?", (sid,)
    ).fetchone()


@pytest.mark.parametrize(
    "side,result,outcome,pnl",
    [
        # 1 contract assumed filled at the 0.42 limit; fee(1, 0.42, 0.07) = 0.017052 -> 0.0171
        ("yes", "yes", "win", "0.5629"),   # (1 - 0.42) - 0.0171
        ("yes", "no", "loss", "-0.4371"),  # -0.42 - 0.0171
        ("no", "no", "win", "0.5629"),
        ("no", "yes", "loss", "-0.4371"),
    ],
)
def test_shadow_scored_on_resolve_net_of_fee(env, side, result, outcome, pnl):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("S", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    sid = _shadow(lg, aid, "S", side=side, limit="0.42")
    fake.resolve("S", result)

    counts = settle.settle_once(lg, fake, st)

    row = _shadow_row(lg, sid)
    assert row["status"] == "scored"
    assert row["outcome"] == outcome
    assert row["hypothetical_pnl"] == pnl  # fill assumed at limit, net of the §8 fee
    assert row["scored_at"] is not None
    assert counts["shadows_scored"] == 1 and counts["shadows_voided"] == 0


def test_shadow_fee_uses_the_category_coefficient(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    # index coef 0.035: fee(1, 0.50, 0.035) = 0.00875 -> 0.0088 (vs 0.0175 at the default)
    fake.add_market("S", title="m", category="index", close_time=CLOSE,
                    yes_ask=D("0.50"), yes_ask_size=10)
    sid = _shadow(lg, aid, "S", side="yes", limit="0.50")
    fake.resolve("S", "yes")

    settle.settle_once(lg, fake, st)
    assert _shadow_row(lg, sid)["hypothetical_pnl"] == "0.4912"  # (1 - 0.50) - 0.0088


def test_shadow_voided_on_void_market(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("S", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    sid = _shadow(lg, aid, "S")
    fake.resolve("S", "void")

    counts = settle.settle_once(lg, fake, st)

    row = _shadow_row(lg, sid)
    assert row["status"] == "void" and row["outcome"] == "void"
    assert row["hypothetical_pnl"] is None
    assert counts["shadows_voided"] == 1 and counts["shadows_scored"] == 0


def test_shadow_on_open_market_stays_open_and_scores_once(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("S", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    sid = _shadow(lg, aid, "S")

    assert settle.settle_once(lg, fake, st)["shadows_scored"] == 0
    assert _shadow_row(lg, sid)["status"] == "open"

    fake.resolve("S", "yes")
    assert settle.settle_once(lg, fake, st)["shadows_scored"] == 1
    # a second pass finds no open shadows -> no rescoring
    assert settle.settle_once(lg, fake, st)["shadows_scored"] == 0
    assert _shadow_row(lg, sid)["status"] == "scored"


def test_shadow_on_unreachable_market_is_skipped(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    sid = _shadow(lg, aid, "GONE")  # never added to the fake exchange

    counts = settle.settle_once(lg, fake, st)
    assert counts["shadows_scored"] == 0 and counts["shadows_voided"] == 0
    assert _shadow_row(lg, sid)["status"] == "open"  # retried next tick


def test_shadows_never_touch_bets_groups_or_the_attempt(env):
    """A shadow on the same ticker as a real bet must leave every real number alone."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.42", contracts=2, fee="0.04")
    # a losing shadow on the same market, big enough to swamp the real P/L if it leaked
    sid = _shadow(lg, aid, "T", side="no", limit="0.90")
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["status"] == "settled" and b["outcome"] == "win"
    assert b["pnl"] == "1.1200"                      # identical to the no-shadow case
    assert counts["bets_settled"] == 1
    assert counts["attempts_settled"] == 1
    assert lg.get_attempt(aid)["status"] == "settled"
    assert len(lg.bets_for_attempt(aid)) == 1        # no bets row invented for the shadow
    assert _shadow_row(lg, sid)["status"] == "scored"
    assert _shadow_row(lg, sid)["outcome"] == "loss"


# --------------------------------------------------------------------------- canary (L6.6)
def _canary_meta(lg, **over):
    """meta.canary as the (separately built) ``canary`` command writes it."""
    payload = {
        "ts": "2026-07-29T14:00:00Z", "ticker": "CAN", "side": "yes", "contracts": 1,
        "fill_price": "0.0700", "fee": "0.0100", "coid": "CANARY-1753800000",
        "settled": False, "payout": None,
    }
    payload.update(over)
    lg.meta_set("canary", json.dumps(payload))
    return payload


def _canary(lg):
    return json.loads(lg.meta_get("canary"))


def test_canary_settles_on_a_win(env):
    lg, fake, st = env
    fake.add_market("CAN", title="c", close_time=CLOSE, yes_ask=D("0.07"), yes_ask_size=10)
    _canary_meta(lg)
    fake.resolve("CAN", "yes")

    counts = settle.settle_once(lg, fake, st)

    canary = _canary(lg)
    assert canary["settled"] is True
    assert canary["outcome"] == "win"
    assert canary["payout"] == "1.0000"  # contracts x $1
    assert counts["canary_settled"] == 1
    ev = lg.audit_events(event="canary_settled")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail == {"ticker": "CAN", "outcome": "win", "payout": "1.0000"}


def test_canary_settles_on_a_loss(env):
    lg, fake, st = env
    fake.add_market("CAN", title="c", close_time=CLOSE, yes_ask=D("0.07"), yes_ask_size=10)
    _canary_meta(lg)
    fake.resolve("CAN", "no")

    counts = settle.settle_once(lg, fake, st)

    canary = _canary(lg)
    assert canary["outcome"] == "loss" and canary["payout"] == "0.0000"
    assert canary["settled"] is True
    assert counts["canary_settled"] == 1


def test_canary_void_refunds_stake_and_fee(env):
    lg, fake, st = env
    fake.add_market("CAN", title="c", close_time=CLOSE, yes_ask=D("0.07"), yes_ask_size=10)
    _canary_meta(lg, contracts=1, fill_price="0.0700", fee="0.0100")
    fake.resolve("CAN", "void")

    settle.settle_once(lg, fake, st)

    canary = _canary(lg)
    assert canary["outcome"] == "void"
    assert canary["payout"] == "0.0800"  # 1 x 0.07 stake + 0.01 fee, mirroring bet voids


def test_canary_multi_contract_payout(env):
    lg, fake, st = env
    fake.add_market("CAN", title="c", close_time=CLOSE, yes_ask=D("0.07"), yes_ask_size=10)
    _canary_meta(lg, contracts=3)
    fake.resolve("CAN", "yes")

    settle.settle_once(lg, fake, st)
    assert _canary(lg)["payout"] == "3.0000"


def test_canary_untouched_while_market_is_open(env):
    lg, fake, st = env
    fake.add_market("CAN", title="c", close_time=CLOSE, yes_ask=D("0.07"), yes_ask_size=10)
    _canary_meta(lg)

    counts = settle.settle_once(lg, fake, st)

    assert counts["canary_settled"] == 0
    assert _canary(lg)["settled"] is False
    assert _canary(lg)["payout"] is None
    assert lg.audit_events(event="canary_settled") == []


def test_canary_already_settled_is_not_rescored(env):
    lg, fake, st = env
    fake.add_market("CAN", title="c", close_time=CLOSE, yes_ask=D("0.07"), yes_ask_size=10)
    _canary_meta(lg)
    fake.resolve("CAN", "yes")

    assert settle.settle_once(lg, fake, st)["canary_settled"] == 1
    assert settle.settle_once(lg, fake, st)["canary_settled"] == 0
    assert len(lg.audit_events(event="canary_settled")) == 1
    assert _canary(lg)["payout"] == "1.0000"  # not doubled


def test_no_canary_meta_is_a_no_op(env):
    lg, fake, st = env
    fake.add_market("CAN", title="c", close_time=CLOSE, yes_ask=D("0.07"), yes_ask_size=10)
    fake.resolve("CAN", "yes")

    counts = settle.settle_once(lg, fake, st)
    assert counts["canary_settled"] == 0
    assert lg.meta_get("canary") is None
    assert lg.audit_events(event="canary_settled") == []


def test_canary_on_unreachable_market_is_left_alone(env):
    lg, fake, st = env
    _canary_meta(lg, ticker="GONE")

    counts = settle.settle_once(lg, fake, st)
    assert counts["canary_settled"] == 0
    assert _canary(lg)["settled"] is False


def test_three_fractional_fills_summing_to_one_are_not_a_count_mismatch(env):
    """Bet A-0054-B01, live 2026-08-02: recorded 1 contract at $0.1500, filled by the
    exchange as "0.28" + "0.34" + "0.38" — exactly 1.00.

    The join is fed the raw payload shape the live endpoint served, because that is where
    it broke: ``int(Decimal("0.28"))`` is 0, so the three pieces summed to nothing. The
    join then read as a zero-fill and settled on recorded values without ever confirming
    the position — the same truncation that made reconcile HALT on a phantom mismatch.

    The bet is recorded at $0.1450 (the order response's rounded average) and the fills
    say $0.1500: only a join that actually sees the fills corrects it.
    """
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.15"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", limit="0.15", fill="0.1450", contracts=1,
                fee="0.01", is_real=1, coid=coid)
    client = _LiveFillsClient(fake, coid, _fractional_fill_payloads("T", "0.1500"))
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, client, st)

    b = lg.bets_for_attempt(aid)[0]
    assert counts["reconcile_mismatches"] == 0   # 0.28 + 0.34 + 0.38 == the recorded 1
    assert lg.audit_events(event="reconcile_mismatch") == []
    assert D(str(b["contracts"])) == D("1")
    assert b["fill_price"] == "0.1500"    # taken from the fills, which were really seen
    assert b["fee"] == "0.0100"           # exchange-charged fee kept: the count matched
    assert b["pnl"] == "0.8400"           # 1 x (1 - 0.15) - 0.01


def test_priceless_fill_is_excluded_from_the_weighted_average_denominator(env):
    """MP-6: the count-weighted average price must divide by the fills that carry a
    price, not by every fill. One priced fill (1 @ 0.40) plus one priceless fill (1,
    price unknown) must average to 0.40 (the one priced fill), not 0.20 (notional over
    every fill) — and it audits once instead of silently mis-pricing."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", limit="0.40", fill="0.4000", contracts=2,
                fee="0.04", is_real=1, coid=coid)
    payloads = [
        {"ticker": "T", "side": "yes", "count_fp": "1", "yes_price_dollars": "0.4000",
         "order_id": "OID-PRICELESS", "is_taker": True, "created_time": "2026-08-02T20:15:00Z"},
        {"ticker": "T", "side": "yes", "count_fp": "1", "order_id": "OID-PRICELESS",
         "is_taker": True, "created_time": "2026-08-02T20:15:01Z"},  # no price -> priceless
    ]
    client = _LiveFillsClient(fake, coid, payloads)
    fake.resolve("T", "yes")

    settle.settle_once(lg, client, st)

    b = lg.bets_for_attempt(aid)[0]
    assert D(str(b["contracts"])) == D("2")             # both fills counted
    assert b["fill_price"] == "0.4000"                  # averaged over the ONE priced fill
    assert lg.audit_events(event="priceless_fills")
    detail = json.loads(lg.audit_events(event="priceless_fills")[0]["detail"])
    assert detail == {"total_count": "2", "priced_count": "1"}


def test_a_genuinely_fractional_position_settles_on_its_exact_size(env):
    """The other half of KC-2: when the fills really do sum to a fraction, that fraction
    is the position — count, price and P/L all follow it."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.20"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", limit="0.20", fill="0.20", contracts=1,
                fee="0.02", is_real=1, coid=coid)
    client = _LiveFillsClient(fake, coid, _fractional_fill_payloads(
        "T", "0.2000", pieces=("0.60",)
    ))
    fake.resolve("T", "yes")

    counts = settle.settle_once(lg, client, st)

    b = lg.bets_for_attempt(aid)[0]
    assert D(str(b["contracts"])) == D("0.60")
    assert counts["reconcile_mismatches"] == 1  # 0.60 filled where 1 was recorded
    detail = json.loads(lg.audit_events(event="reconcile_mismatch")[0]["detail"])
    assert detail["recorded"]["contracts"] == "1" and detail["actual"]["contracts"] == "0.60"
    assert b["pnl"] == "0.4732"  # 0.60*(1-0.20) - model fee 0.0068 for the actual fill


# ------------------------------------------------------------------ robustness (MP-1/MP-2/MP-3)
def test_one_transient_market_failure_is_retried_and_settles(env):
    """MP-1a: a single ``get_market`` blip used to cache ``None`` and leave a finalized
    bet unsettled — the exact input to the spurious overnight HALT. One retry closes it."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.42", contracts=2, fee="0.04")
    fake.resolve("T", "yes")
    client = _FlakyMarketClient(fake, "T", fails=1)

    counts = settle.settle_once(lg, client, st)

    assert client.calls == 2                       # initial + one retry
    assert counts["bets_settled"] == 1
    assert counts["errors"] == 0                   # the retry cured it: nothing to report
    assert lg.bets_for_attempt(aid)[0]["status"] == "settled"


def test_a_persistently_unreachable_market_reports_an_error(env):
    """Two failures give up (as before) but now say so: ``errors`` is what makes the tick
    defer tonight's reconciliation instead of halting on the settlement it never saw."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.42", contracts=2, fee="0.04")
    fake.resolve("T", "yes")
    client = _FlakyMarketClient(fake, "T", fails=99)

    counts = settle.settle_once(lg, client, st)

    assert client.calls == 2                       # bounded: initial + one retry, no more
    assert counts["bets_settled"] == 0 and counts["errors"] == 1
    assert lg.bets_for_attempt(aid)[0]["status"] == "filled"


def test_one_bets_failure_does_not_abort_the_pass(env, monkeypatch):
    """MP-2: a fills-join 500 on one bet used to abort settlement, group settlement, the
    canary close-out AND the impostor scan for the whole tick."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    bad, good = f"{aid}-B01", f"{aid}-B02"
    for ticker in ("T1", "T2"):
        fake.add_market(ticker, title="m", close_time=CLOSE, yes_ask=D("0.40"),
                        yes_ask_size=10)
    _filled_bet(lg, aid, 1, "T1", side="yes", fill="0.40", contracts=1, fee="0.02",
                is_real=1, coid=bad)
    _filled_bet(lg, aid, 2, "T2", side="yes", fill="0.40", contracts=1, fee="0.02",
                is_real=1, coid=good)
    fake.resolve("T1", "yes")
    fake.resolve("T2", "yes")
    settle.genesis_snapshot(lg, fake)
    monkeypatch.setattr(settle, "_set_halt", lambda *a: pytest.fail("nothing to halt on"))
    fake.add_personal_order("T3")  # only the shared-account scan can see this
    client = _BrokenJoinClient(fake, bad)

    counts = settle.settle_once(lg, client, st)

    rows = {b["bet_id"]: b["status"] for b in lg.bets_for_attempt(aid)}
    assert rows[bad] == "filled" and rows[good] == "settled"
    assert counts["bets_settled"] == 1 and counts["errors"] == 1
    assert counts["personal_seen"] == 1            # the scan still ran
    events = lg.audit_events(event="settle_bet_error")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["ticker"] == "T1" and "fills endpoint 500" in detail["error"]
    assert events[0]["bet_id"] == bad
    # the attempt is NOT settled: one leg is still open, exactly as before the failure
    assert lg.get_attempt(aid)["status"] == "placed"


def test_an_injected_orders_page_failure_isolates_one_bet_and_the_pass_finishes(env):
    """MP-2 again, but through WP5's real fault injector instead of a hand-written stub.

    ``_BrokenJoinClient`` above proves the isolation by replacing
    ``find_fills_by_client_order_id`` wholesale — which quietly assumes the join is one
    call that either works or does not. It is not: it PAGES ``/portfolio/orders`` and then
    reads fills for the matched order, so the way this fails in production is a single GET
    coming back 5xx somewhere inside that walk. ``fail_next("get_orders")`` injects
    exactly that, from outside, and the isolation has to hold against the real call graph.
    """
    lg, fake, st = env
    aid = _placed_attempt(lg)
    first, second = f"{aid}-B01", f"{aid}-B02"
    for ticker in ("T1", "T2"):
        fake.add_market(ticker, title="m", close_time=CLOSE, yes_ask=D("0.40"),
                        yes_ask_size=10)
        fake.create_order(ticker, "yes", D("0.40"), 1,
                          first if ticker == "T1" else second)
    _filled_bet(lg, aid, 1, "T1", side="yes", fill="0.40", contracts=1, fee="0.02",
                is_real=1, coid=first)
    _filled_bet(lg, aid, 2, "T2", side="yes", fill="0.40", contracts=1, fee="0.02",
                is_real=1, coid=second)
    fake.resolve("T1", "yes")
    fake.resolve("T2", "yes")
    settle.genesis_snapshot(lg, fake)

    fake.fail_next("get_orders")   # one-shot: the FIRST bet's join walks into it
    counts = settle.settle_once(lg, fake, st)

    rows = {b["bet_id"]: b["status"] for b in lg.bets_for_attempt(aid)}
    assert rows[first] == "filled"                 # left for the next pass, untouched
    assert rows[second] == "settled"               # the pass carried on
    assert counts["bets_settled"] == 1 and counts["errors"] == 1
    events = lg.audit_events(event="settle_bet_error")
    assert [e["bet_id"] for e in events] == [first]
    assert "injected" in json.loads(events[0]["detail"])["error"]
    assert not st.halt_path.exists()
    # errors > 0 is the signal cli._reconcile_if_due reads to defer tonight's walk (D1).
    assert counts["errors"] > 0


def test_an_injected_market_blip_is_absorbed_by_the_retry_and_the_bet_still_settles(env):
    """The MP-1 half, through the injector: one ``get_market`` failure is a blip, and
    ``_cached_market`` retries once. The pass reports no error, so the night's
    reconciliation is NOT deferred over a single dropped packet."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = f"{aid}-B01"
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.create_order("T", "yes", D("0.40"), 1, coid)
    _filled_bet(lg, aid, 1, "T", side="yes", fill="0.40", contracts=1, fee="0.02",
                is_real=1, coid=coid)
    fake.resolve("T", "yes")

    fake.fail_next("get_market")
    counts = settle.settle_once(lg, fake, st)

    assert counts["bets_settled"] == 1 and counts["errors"] == 0
    assert lg.bets_for_attempt(aid)[0]["status"] == "settled"


def test_group_left_unstamped_by_a_crash_heals_on_the_next_pass(env):
    """MP-3: the legs settled, then the process died before the group row was written.
    The next pass touches no bets at all, so the old touched-attempts key never revisited
    the group and ``realized_pnl`` was stranded forever."""
    lg, fake, st = env
    aid = _placed_attempt(lg, edge_class="structural")
    gid = f"{aid}-G1"
    lg.insert_group(gid, aid, json.dumps([]), D("0.01"))
    lg.set_group(gid, status="filled")
    fake.add_market("A", title="m", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "A", side="yes", fill="0.40", contracts=1, fee="0.02",
                group_id=gid)
    fake.resolve("A", "yes")
    # settle the leg by hand and leave the group alone — the crash window
    lg.update_bet(f"{aid}-B01", status="settled", outcome="win", pnl=D("0.58"),
                  settled_at="2026-07-09T12:00:00Z")

    counts = settle.settle_once(lg, fake, st)

    g = lg.groups_for_attempt(aid)[0]
    assert counts["bets_settled"] == 0             # nothing new to settle this pass
    assert g["status"] == "settled" and g["realized_pnl"] == "0.5800"
    assert counts["groups_settled"] == 1
    # and it is idempotent: a third pass leaves it alone
    assert settle.settle_once(lg, fake, st)["groups_settled"] == 0


def test_a_pending_group_is_never_settled(env):
    """The verifier's correction to MP-3: a group still ``pending`` never completed
    placement. Its legs can all be terminal (rejected) while nothing was ever at risk."""
    lg, fake, st = env
    aid = _placed_attempt(lg, edge_class="structural")
    gid = f"{aid}-G1"
    lg.insert_group(gid, aid, json.dumps([]), D("0.01"))
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="A",
                  side="yes", limit_price=D("0.40"), model_prob=D("0.55"), rationale="r",
                  status="rejected", reject_code="V07", group_id=gid)

    settle.settle_once(lg, fake, st)

    g = lg.groups_for_attempt(aid)[0]
    assert g["status"] == "pending" and g["realized_pnl"] is None


def test_a_broken_group_is_stamped_only_once(env):
    """The broken branch stays in the candidate set only until it has a realized_pnl."""
    lg, fake, st = env
    aid = _placed_attempt(lg, edge_class="structural")
    gid = f"{aid}-G1"
    lg.insert_group(gid, aid, json.dumps([]), D("0.01"))
    lg.set_group(gid, status="broken")
    fake.add_market("A", title="m", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    _filled_bet(lg, aid, 1, "A", side="yes", fill="0.40", contracts=1, fee="0.02",
                group_id=gid)
    fake.resolve("A", "yes")

    settle.settle_once(lg, fake, st)
    lg.set_group(gid, realized_pnl=D("9.99"))      # a later hand-correction must survive
    settle.settle_once(lg, fake, st)

    g = lg.groups_for_attempt(aid)[0]
    assert g["status"] == "broken" and g["realized_pnl"] == "9.9900"


# ----------------------------------------------- EF-2: the incremental account scan
def _no_halt(monkeypatch):
    monkeypatch.setattr(settle, "_set_halt", lambda *a: pytest.fail("must not halt"))


def _watermarks(lg):
    return lg.meta_get("impostor_seen_through"), json.loads(
        lg.meta_get("personal_orders_seen") or "null"
    )


def test_the_first_pass_stamps_the_scan_watermark(env, monkeypatch):
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _no_halt(monkeypatch)
    when = datetime.now(UTC)
    fake.add_personal_order("T", ts=when)

    settle.settle_once(lg, fake, st)

    seen_through, personal = _watermarks(lg)
    # stored verbatim as the exchange reported it, so no rounding can un-cover the order
    assert seen_through == fake.get_orders()[0][0]["created_time"]
    assert personal["recent"] == [fake.get_orders()[0][0]["order_id"]]
    assert personal["undated"] == []


def test_the_next_pass_only_reads_the_overlap_window(env, monkeypatch):
    """EF-2: this ran ~96x/day over the entire post-genesis history. Now the second pass
    asks for a 24-hour window, and the pages it costs shrink with it."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _no_halt(monkeypatch)
    fake.page_size = 10
    base = datetime.now(UTC) - timedelta(days=200)
    lg.meta_set("genesis_ts", base.isoformat().replace("+00:00", "Z"))
    for i in range(200):  # ~200 days of somebody else's activity
        fake.add_personal_order(f"T{i}", ts=base + timedelta(days=i), move_balance=False)

    settle.settle_once(lg, fake, st)
    first = fake.calls["get_orders"]
    fake.reset_calls()
    settle.settle_once(lg, fake, st)

    assert first == 20                          # 200 orders at 10/page
    assert fake.calls["get_orders"] == 1         # only the last day is re-read
    assert lg.audit_count("personal_fill_observed") == 200  # none dropped


def test_an_order_inside_the_overlap_window_is_still_seen(env, monkeypatch):
    """The overlap is the guarantee that "incremental" is not "lossy" — and it has to
    audit the late order, not merely re-read it, which a bare watermark could not do."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _no_halt(monkeypatch)
    now = datetime.now(UTC)
    lg.meta_set("genesis_ts", (now - timedelta(days=30)).isoformat().replace("+00:00", "Z"))
    fake.add_personal_order("FIRST", ts=now)
    settle.settle_once(lg, fake, st)
    # an order that becomes visible late, dated an hour BEFORE the watermark
    fake.add_personal_order("LATE", ts=now - timedelta(hours=1))

    counts = settle.settle_once(lg, fake, st)

    assert counts["personal_seen"] == 1
    assert len(lg.audit_events(event="personal_fill_observed")) == 2


def test_the_window_base_switches_to_the_live_genesis(env, monkeypatch):
    """The paper genesis is months earlier; walking from it forever was half of EF-2."""
    lg, fake, st = env
    _no_halt(monkeypatch)
    paper = datetime.now(UTC) - timedelta(days=120)
    live = datetime.now(UTC) - timedelta(days=3)
    lg.meta_set("genesis_ts", paper.isoformat().replace("+00:00", "Z"))
    lg.meta_set("live_genesis_ts", live.isoformat().replace("+00:00", "Z"))
    fake.add_personal_order("PAPER_ERA", ts=paper + timedelta(days=1))
    fake.add_personal_order("LIVE_ERA", ts=live + timedelta(hours=1))

    counts = settle.settle_once(lg, fake, st)

    assert counts["personal_seen"] == 1
    detail = json.loads(lg.audit_events(event="personal_fill_observed")[0]["detail"])
    assert detail["ticker"] == "LIVE_ERA"


def test_an_impostor_does_not_advance_the_watermark_and_stays_loud(env, monkeypatch):
    """A halted system is stopped for as long as a human takes. If the watermark moved
    past the impostor, resuming a day later would find nothing wrong."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    halts = []
    monkeypatch.setattr(settle, "_set_halt", lambda settings, reason: halts.append(reason))
    fake.add_impostor_order("A-9999-B01")

    settle.settle_once(lg, fake, st)
    settle.settle_once(lg, fake, st)

    assert len(halts) == 2  # re-detected, re-halted
    assert lg.meta_get("impostor_seen_through") is None
    assert len(lg.audit_events(event="unknown_fill")) == 2


def test_a_weekly_full_scan_re_reads_the_whole_history(env, monkeypatch):
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _no_halt(monkeypatch)
    fake.page_size = 10
    base = datetime.now(UTC) - timedelta(days=60)
    lg.meta_set("genesis_ts", base.isoformat().replace("+00:00", "Z"))
    for i in range(60):
        fake.add_personal_order(f"T{i}", ts=base + timedelta(days=i), move_balance=False)
    settle.settle_once(lg, fake, st)
    fake.reset_calls()

    settle.settle_once(lg, fake, st, full_scan=True)

    assert fake.calls["get_orders"] == 6  # the whole 60-order history again, not one page


def test_a_full_scan_re_detects_an_impostor_the_window_has_moved_past(env, monkeypatch):
    lg, fake, st = env
    _no_halt(monkeypatch)
    base = datetime.now(UTC) - timedelta(days=30)
    lg.meta_set("genesis_ts", base.isoformat().replace("+00:00", "Z"))
    fake.add_personal_order("RECENT", ts=datetime.now(UTC))
    settle.settle_once(lg, fake, st)  # watermark jumps to today
    # an impostor dated a month back — outside the 24h overlap from here on
    fake.add_impostor_order("A-9999-B01", ts=base + timedelta(days=1))

    halts = []
    monkeypatch.setattr(settle, "_set_halt", lambda settings, reason: halts.append(reason))
    incremental = settle.settle_once(lg, fake, st)
    full = settle.settle_once(lg, fake, st, full_scan=True)

    assert incremental["impostors"] == 0   # genuinely out of the incremental window
    assert full["impostors"] == 1          # and this is why the weekly pass exists
    assert halts and "A-9999-B01" in halts[0]


def test_a_legacy_key_list_is_folded_into_the_watermark_once(env, monkeypatch):
    """ST-11: the key was an unbounded list of every order ever seen. The first pass after
    the upgrade covers the full window, so it can retire the list without re-auditing."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _no_halt(monkeypatch)
    when = datetime.now(UTC)
    oid = fake.add_personal_order("T", ts=when)
    lg.meta_set("personal_orders_seen", json.dumps([oid]))  # the pre-WP4 shape

    counts = settle.settle_once(lg, fake, st)

    assert counts["personal_seen"] == 0  # already audited under the old scheme
    assert lg.audit_events(event="personal_fill_observed") == []
    _, personal = _watermarks(lg)
    assert personal["recent"] == [oid] and personal["undated"] == []
    assert isinstance(personal["through"], str)  # the list is gone for good


def test_an_undated_order_is_audited_once_and_kept_out_of_the_watermark(env, monkeypatch):
    """§10 forbids dropping account activity, and a watermark cannot describe an order
    with no clock — so those few keep a bounded key list of their own."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _no_halt(monkeypatch)
    fake.add_personal_order("T")
    fake._orders[-1].pop("created_time")  # the pathological payload

    c1 = settle.settle_once(lg, fake, st)
    c2 = settle.settle_once(lg, fake, st)

    assert (c1["personal_seen"], c2["personal_seen"]) == (1, 0)
    _, personal = _watermarks(lg)
    assert personal["through"] is None and len(personal["undated"]) == 1


def test_settle_once_request_count_at_twelve_month_account_age(env, monkeypatch):
    """The EF-2 regression guard. A year of account history used to be re-paged on every
    one of ~96 daily ticks; the steady-state pass must not scale with that history."""
    lg, fake, st = env
    _no_halt(monkeypatch)
    fake.page_size = 25
    base = datetime.now(UTC) - timedelta(days=365)
    lg.meta_set("genesis_ts", base.isoformat().replace("+00:00", "Z"))
    for i in range(600):
        fake.add_personal_order(f"T{i}", ts=base + timedelta(hours=i * 14),
                                move_balance=False)

    settle.settle_once(lg, fake, st)          # the cold pass reads everything
    cold = fake.calls["get_orders"]
    fake.reset_calls()
    settle.settle_once(lg, fake, st)          # the steady-state pass

    assert cold == 24                          # 600 orders at 25/page
    assert fake.calls["get_orders"] <= 2       # bounded by the overlap, not by history
    assert fake.calls_total() <= 4


# ----------------------------------------------- fold-in A: exact canary counts
def test_a_fractional_canary_settles_on_its_exact_size(env):
    """``int(Decimal("0.90"))`` is 0: a canary that cost real money paying out nothing."""
    lg, fake, st = env
    fake.add_market("KXCANARY", title="c", close_time=CLOSE, yes_ask=D("0.15"),
                    yes_ask_size=10)
    lg.meta_set("canary", json.dumps({
        "ts": "2026-07-07T12:00:00Z", "ticker": "KXCANARY", "side": "yes",
        "contracts": "0.90", "fill_price": "0.1500", "fee": "0.0100",
        "settled": False, "payout": None,
    }))
    fake.resolve("KXCANARY", "yes")

    settle.settle_once(lg, fake, st)

    canary = json.loads(lg.meta_get("canary"))
    assert canary["settled"] is True and canary["outcome"] == "win"
    assert canary["payout"] == "0.9000"


def test_a_fractional_canary_void_refunds_its_exact_stake(env):
    lg, fake, st = env
    fake.add_market("KXCANARY", title="c", close_time=CLOSE, yes_ask=D("0.15"),
                    yes_ask_size=10)
    lg.meta_set("canary", json.dumps({
        "ts": "2026-07-07T12:00:00Z", "ticker": "KXCANARY", "side": "yes",
        "contracts": "0.90", "fill_price": "0.1500", "fee": "0.0100",
        "settled": False, "payout": None,
    }))
    fake.resolve("KXCANARY", "")

    settle.settle_once(lg, fake, st)

    assert json.loads(lg.meta_get("canary"))["payout"] == "0.1450"  # 0.9*0.15 + 0.01


# ----------------------------------------------- MP-5: genesis snapshot ordering
class _NoFillsClient:
    """Everything works except the (multi-page, fallible) fills read."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_fills(self, min_ts=None, cursor=None):
        raise RuntimeError("transient fills failure")


def test_a_failed_fills_page_leaves_genesis_unstamped_and_retryable(env):
    """MP-5: stamping the timestamp FIRST meant a transient failure here lost the
    personal-fill baseline permanently — the stamp is exactly what makes this a no-op."""
    lg, fake, st = env

    with pytest.raises(RuntimeError):
        settle.genesis_snapshot(lg, _NoFillsClient(fake))

    assert lg.meta_get("genesis_ts") is None
    assert lg.meta_get("genesis_snapshot") is None

    settle.genesis_snapshot(lg, fake)  # the retry that used to be impossible

    assert lg.meta_get("genesis_ts") is not None
    assert json.loads(lg.meta_get("genesis_snapshot"))["fill_count"] == 0


# ------------------------------------------------------- docs/16 §5: scalar settlements
def _place_real_leg(lg, fake, aid, idx, ticker, *, side, price, contracts=1,
                    model_prob="0.55"):
    """A real order on the fake (debiting stake + fee) mirrored into the ledger.

    Money-consistent on purpose: everything a settle assertion later reads — the fill
    price, the exchange-charged fee, the settlement record — is the fake's own
    bookkeeping rather than a payload hand-written to match the assertion.
    """
    coid = f"{aid}-B{idx:02d}"
    r = fake.create_order(ticker, side, D(price), contracts, coid)
    assert r.filled_count == contracts, "fixture expects a full fill"
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=idx, ticker=ticker, side=side,
        limit_price=D(price), model_prob=D(model_prob), rationale="r", is_real=1,
        status="filled", contracts=contracts, fill_price=r.avg_fill_price,
        stake=q4(D(contracts) * r.avg_fill_price), fee=D(r.fee),
        order_id=r.order_id, client_order_id=coid, placed_at="2026-07-07T12:00:00Z",
    )
    return coid


def test_a_scalar_settlement_pays_the_exchange_revenue_and_keeps_the_fee(env):
    """The A-0097-B01 shape, end to end: 1 NO at $0.75, scalar-settled at $0.82.

    Booked as a void (what the code did before docs/16 §5) this was pnl $0.00 with the fee
    refunded, against an exchange-true +$0.0568 — the residual that HALTed the live system.
    """
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("KXNPBTOTAL-SHORTENED", title="m", close_time=CLOSE,
                    no_ask=D("0.75"), no_ask_size=50)
    coid = _place_real_leg(lg, fake, aid, 1, "KXNPBTOTAL-SHORTENED", side="no",
                           price="0.75")
    fake.resolve("KXNPBTOTAL-SHORTENED", "scalar", scalar_value=D("0.18"))

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["status"] == "settled" and b["outcome"] == "scalar"
    assert b["fee"] == "0.0132"          # RETAINED — the exchange kept it
    assert b["pnl"] == "0.0568"          # 0.82 payout - 0.75 stake - 0.0132 fee
    assert counts["bets_settled"] == 1 and counts["scalars_settled"] == 1
    assert counts["bets_voided"] == 0 and counts["errors"] == 0
    # and the exchange agrees to the cent: its own credit for this position was $0.82.
    credits = [e for e in fake.balance_ledger() if e["reason"] == "settlement_scalar"]
    assert len(credits) == 1 and credits[0]["delta"] == D("0.8200")
    assert credits[0]["client_order_id"] == coid


def test_the_settlements_feed_is_read_only_when_a_scalar_needs_it(env):
    """Laziness, pinned. ``/portfolio/settlements`` is a per-pass cost the settle step never
    used to pay, and scalars are rare: a day with none must not start paying for it. When
    one does appear the feed is paged ONCE, however many scalar legs read it."""
    lg, fake, st = env
    a1, a2 = _placed_attempt(lg), _placed_attempt(lg)
    fake.add_market("ORDINARY", title="m", close_time=CLOSE, yes_ask=D("0.42"),
                    yes_ask_size=10)
    _filled_bet(lg, a1, 1, "ORDINARY", side="yes", fill="0.42", contracts=1, fee="0.02")
    fake.resolve("ORDINARY", "yes")

    fake.reset_calls()
    settle.settle_once(lg, fake, st)
    assert fake.calls.get("get_settlements", 0) == 0

    fake.add_market("SCALAR-A", title="a", close_time=CLOSE, no_ask=D("0.75"),
                    no_ask_size=10)
    fake.add_market("SCALAR-B", title="b", close_time=CLOSE, no_ask=D("0.75"),
                    no_ask_size=10)
    for i, ticker in enumerate(("SCALAR-A", "SCALAR-B"), start=1):
        _filled_bet(lg, a2, i + 1, ticker, side="no", fill="0.75", contracts=1,
                    fee="0.0132")
        fake.add_settlement(ticker, "scalar", side="no", contracts=1, revenue="0.82")
        fake.resolve(ticker, "scalar", scalar_value=D("0.18"))

    fake.reset_calls()
    counts = settle.settle_once(lg, fake, st)

    assert counts["scalars_settled"] == 2
    assert fake.calls["get_settlements"] == 1     # one pagination, shared by both legs


def test_a_scalar_market_with_no_settlement_record_is_left_for_a_later_pass(env):
    """No record, no number. The row stays a position and the pass reports an error, which
    is what makes the night's reconciliation DEFER rather than HALT on it."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, no_ask=D("0.75"), no_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="no", fill="0.75", contracts=1, fee="0.0132")
    fake.resolve("T", "scalar", scalar_value=D("0.18"))  # no fills -> no settlement record
    assert fake.get_settlements()[0] == []

    counts = settle.settle_once(lg, fake, st)

    b = lg.bets_for_attempt(aid)[0]
    assert b["status"] == "filled" and b["outcome"] is None and b["pnl"] is None
    assert counts["bets_settled"] == 0 and counts["bets_voided"] == 0
    assert counts["errors"] == 1
    ev = lg.audit_events(event="scalar_settlement_deferred")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["settlement_record"] is None
    assert "no settlement record" in detail["reason"]

    # ...and the later pass, once the exchange publishes it, settles it.
    fake.add_settlement("T", "scalar", side="no", contracts=1, revenue="0.82")
    counts = settle.settle_once(lg, fake, st)
    b = lg.bets_for_attempt(aid)[0]
    assert b["status"] == "settled" and b["outcome"] == "scalar" and b["pnl"] == "0.0568"
    assert counts["scalars_settled"] == 1 and counts["errors"] == 0


def test_a_scalar_record_that_nets_both_sides_is_refused_not_split(env):
    """A record carrying a position on BOTH sides folds them into one revenue figure.
    Splitting that total back between the sides is unrecoverable, so nothing is written."""
    lg, fake, st = env
    a1, a2 = _placed_attempt(lg), _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE,
                    yes_ask=D("0.30"), yes_ask_size=50, no_ask=D("0.75"), no_ask_size=50)
    _place_real_leg(lg, fake, a1, 1, "T", side="no", price="0.75")
    _place_real_leg(lg, fake, a2, 1, "T", side="yes", price="0.30")
    fake.resolve("T", "scalar", scalar_value=D("0.18"))
    record = fake.get_settlements()[0][0]
    assert record.yes_count == D("1.00") and record.no_count == D("1.00")

    counts = settle.settle_once(lg, fake, st)

    assert [b["status"] for b in lg.conn.execute(
        "SELECT status FROM bets ORDER BY bet_id").fetchall()] == ["filled", "filled"]
    assert counts["errors"] == 2 and counts["bets_settled"] == 0
    detail = json.loads(lg.audit_events(event="scalar_settlement_deferred")[0]["detail"])
    assert detail["settlement_record"] == {"revenue": "1", "yes_count": "1.00",
                                           "no_count": "1.00"}
    assert "cannot attribute a payout" in detail["reason"]


def test_two_settlement_records_that_disagree_on_revenue_are_refused(env):
    """The same refusal ``reconcile._settlement_results`` makes for an ambiguous ticker:
    guessing which figure the exchange actually paid is what a money path must not do."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, no_ask=D("0.75"), no_ask_size=10)
    _filled_bet(lg, aid, 1, "T", side="no", fill="0.75", contracts=1, fee="0.0132")
    fake.resolve("T", "scalar", scalar_value=D("0.18"))
    fake.add_settlement("T", "scalar", side="no", contracts=1, revenue="0.82")
    fake.add_settlement("T", "scalar", side="no", contracts=1, revenue="0.55")

    counts = settle.settle_once(lg, fake, st)

    assert lg.bets_for_attempt(aid)[0]["status"] == "filled"
    assert counts["errors"] == 1


def test_a_scalar_payout_divides_the_record_across_our_legs_per_contract(env):
    """One settlement record covers every contract the ACCOUNT held, however many ledger
    legs bought them, so the per-contract value is ``revenue / position count``."""
    lg, fake, st = env
    a1, a2 = _placed_attempt(lg), _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, no_ask=D("0.75"), no_ask_size=50)
    for aid in (a1, a2):
        _filled_bet(lg, aid, 1, "T", side="no", fill="0.75", contracts=1, fee="0.0132")
    # The exchange's record: 2 NO contracts, $1.64 total, i.e. $0.82 each.
    fake.add_settlement("T", "scalar", side="no", contracts=2, revenue="1.64")
    fake.resolve("T", "scalar", scalar_value=D("0.18"))

    counts = settle.settle_once(lg, fake, st)

    for aid in (a1, a2):
        b = lg.bets_for_attempt(aid)[0]
        assert b["outcome"] == "scalar" and b["pnl"] == "0.0568"
    assert counts["scalars_settled"] == 2


def test_a_scalar_market_voids_the_shadow_and_the_nofill_counterfactual(env):
    """Documented in ``_settle_shadows``/``_settle_nofills``: a counterfactual leg held no
    position, so the exchange published no revenue for it and there is nothing to derive a
    scalar value from. Void-at-zero, never a fabricated number."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    fake.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    fake.add_market("T2", title="m2", close_time=CLOSE, yes_ask=D("0.30"), yes_ask_size=10)
    _shadow(lg, aid, "T", limit="0.42")
    lg.insert_bet(bet_id=f"{aid}-B09", attempt_id=aid, ticket_index=9, ticker="T2",
                  side="yes", limit_price=D("0.30"), model_prob=D("0.55"), rationale="r",
                  status="no_fill", contracts=1)
    fake.resolve("T", "scalar", scalar_value=D("0.18"))
    fake.resolve("T2", "scalar", scalar_value=D("0.18"))

    counts = settle.settle_once(lg, fake, st)

    shadow = lg.conn.execute("SELECT * FROM shadow_bets").fetchone()
    assert shadow["outcome"] == "void" and shadow["hypothetical_pnl"] is None
    nofill = lg.conn.execute(
        "SELECT * FROM bets WHERE bet_id=?", (f"{aid}-B09",)
    ).fetchone()
    assert nofill["status"] == "no_fill"                      # never touched
    assert nofill["hypothetical_outcome"] == "void"
    assert nofill["hypothetical_pnl"] == "0.0000"
    assert counts["shadows_voided"] == 1 and counts["nofills_voided"] == 1


def test_a_scalar_canary_market_is_left_for_a_human(env):
    """The canary is real money in the balance walk and has no scalar payout path. Booking
    it a void would claim a stake-and-fee refund that never happened — the A-0097 bug."""
    lg, fake, st = env
    fake.add_market("CAN", title="c", close_time=CLOSE, yes_ask=D("0.07"), yes_ask_size=10)
    _canary_meta(lg)
    fake.resolve("CAN", "scalar", scalar_value=D("0.18"))

    counts = settle.settle_once(lg, fake, st)

    assert _canary(lg)["settled"] is False
    assert _canary(lg)["payout"] is None
    assert counts["canary_settled"] == 0 and counts["errors"] == 1
    detail = json.loads(lg.audit_events(event="scalar_settlement_deferred")[0]["detail"])
    assert detail["position"] == "canary"


# --------------------------------------------- docs/16 §5 D3: netted pairs at settle time
def test_a_netted_pair_settles_cleanly_on_both_legs(env):
    """Per-leg booking is UNCHANGED for a market we hold both sides of, and this pins that.

    The exchange nets the pair and pays $1.00 at the later fill; our ledger books one
    winning leg and one losing leg at settlement. The two agree in total to the cent (only
    the timing differs), so nothing here needed changing — but the fills join runs per
    ``client_order_id``, so a test has to prove the netted world does not make it report a
    spurious mismatch against either leg. The Aug-15 KXHIGHMIA-26AUG15-B92.5 numbers.
    """
    lg, fake, st = env
    a1, a2 = _placed_attempt(lg), _placed_attempt(lg)
    fake.add_market("KXHIGHMIA-B92.5", title="m", close_time=CLOSE,
                    yes_ask=D("0.47"), yes_ask_size=50, no_ask=D("0.49"), no_ask_size=50)
    no_leg = _place_real_leg(lg, fake, a1, 15, "KXHIGHMIA-B92.5", side="no", price="0.49")
    yes_leg = _place_real_leg(lg, fake, a2, 3, "KXHIGHMIA-B92.5", side="yes", price="0.47")
    fake.net_matched_pair("KXHIGHMIA-B92.5", 1)   # the exchange pays the matched pair now
    fake.resolve("KXHIGHMIA-B92.5", "yes")

    counts = settle.settle_once(lg, fake, st)

    won = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (yes_leg,)).fetchone()
    lost = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (no_leg,)).fetchone()
    assert won["outcome"] == "win" and won["pnl"] == "0.5125"      # 1*(1-0.47) - 0.0175
    assert lost["outcome"] == "loss" and lost["pnl"] == "-0.5075"  # -(1*0.49) - 0.0175
    # net across the pair: +$0.005, exactly what the exchange's own money did.
    assert q4(D(won["pnl"]) + D(lost["pnl"])) == D("0.0050")
    assert counts["bets_settled"] == 2 and counts["reconcile_mismatches"] == 0
    assert lg.audit_events(event="reconcile_mismatch") == []


# ------------------------------------------- docs/16 §5 D4: the owner's one-row correction
def _voided_scalar_row(lg):
    """A-0097-B01 as the live ledger actually holds it: a scalar settlement booked void."""
    aid = _placed_attempt(lg)
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1,
        ticker="KXNPBTOTAL-26AUG130500HIRYAK-12", side="no", limit_price=D("0.75"),
        model_prob=D("0.55"), rationale="r", is_real=1, status="filled", contracts=1,
        fill_price=D("0.75"), stake=D("0.75"), fee=D("0.0132"),
        client_order_id=f"{aid}-B01", placed_at="2026-08-12T13:05:53Z",
    )
    lg.update_bet(f"{aid}-B01", status="voided", outcome="void", pnl=D("0"), fee=D("0"),
                  settled_at="2026-08-15T14:48:49Z")
    return aid, f"{aid}-B01"


def test_the_correction_rewrites_exactly_the_four_fields_and_audits_once(env):
    lg, _fake, _st = env
    aid, bet_id = _voided_scalar_row(lg)

    result = settle.correct_scalar_settlement(
        lg, bet_id=bet_id, revenue=D("0.82"), fee=D("0.0132")
    )

    assert result["ok"] is True and result["pnl"] == D("0.0568")
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bet_id,)).fetchone()
    assert row["status"] == "settled" and row["outcome"] == "scalar"
    assert row["pnl"] == "0.0568" and row["fee"] == "0.0132"
    assert row["settled_at"] == "2026-08-15T14:48:49Z"   # never moved
    assert row["contracts"] == 1 and row["fill_price"] == "0.7500"  # untouched
    ev = lg.audit_events(event="scalar_settlement_corrected")
    assert len(ev) == 1 and ev[0]["bet_id"] == bet_id and ev[0]["attempt_id"] == aid
    detail = json.loads(ev[0]["detail"])
    assert detail["before"] == {"status": "voided", "outcome": "void", "pnl": "0.0000",
                                "fee": "0.0000", "settled_at": "2026-08-15T14:48:49Z"}
    assert detail["after"] == {"status": "settled", "outcome": "scalar", "pnl": "0.0568",
                               "fee": "0.0132", "settled_at": "2026-08-15T14:48:49Z"}
    assert detail["revenue"] == "0.8200" and detail["dry_run"] is False


def test_the_correction_dry_run_writes_absolutely_nothing(env):
    lg, _fake, _st = env
    _aid, bet_id = _voided_scalar_row(lg)

    result = settle.correct_scalar_settlement(
        lg, bet_id=bet_id, revenue=D("0.82"), fee=D("0.0132"), dry_run=True
    )

    assert result["ok"] is True and result["pnl"] == D("0.0568")
    assert "NOTHING was written" in result["message"]
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bet_id,)).fetchone()
    assert row["status"] == "voided" and row["outcome"] == "void"
    assert row["pnl"] == "0.0000" and row["fee"] == "0.0000"
    assert lg.audit_events(event="scalar_settlement_corrected") == []


def test_the_correction_refuses_a_bet_that_is_not_voided_which_makes_it_idempotent(env):
    lg, _fake, _st = env
    _aid, bet_id = _voided_scalar_row(lg)
    assert settle.correct_scalar_settlement(
        lg, bet_id=bet_id, revenue=D("0.82"), fee=D("0.0132"))["ok"] is True

    second = settle.correct_scalar_settlement(
        lg, bet_id=bet_id, revenue=D("0.82"), fee=D("0.0132")
    )

    assert second["ok"] is False and second["reason"] == "not_voided"
    assert "not 'voided'" in second["message"]
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bet_id,)).fetchone()
    assert row["pnl"] == "0.0568"                        # unchanged by the second run
    assert len(lg.audit_events(event="scalar_settlement_corrected")) == 1


def test_the_correction_refuses_a_still_filled_bet_and_an_unknown_one(env):
    lg, _fake, _st = env
    aid = _placed_attempt(lg)
    _filled_bet(lg, aid, 1, "T", side="no", fill="0.75", contracts=1, fee="0.0132")

    filled = settle.correct_scalar_settlement(
        lg, bet_id=f"{aid}-B01", revenue=D("0.82"), fee=D("0.0132")
    )
    missing = settle.correct_scalar_settlement(
        lg, bet_id="A-9999-B01", revenue=D("0.82"), fee=D("0.0132")
    )

    assert filled["ok"] is False and filled["reason"] == "not_voided"
    assert missing["ok"] is False and missing["reason"] == "no_such_bet"
    assert lg.conn.execute(
        "SELECT status FROM bets WHERE bet_id=?", (f"{aid}-B01",)
    ).fetchone()["status"] == "filled"
    assert lg.audit_events(event="scalar_settlement_corrected") == []


# ------------------------------- docs/22 section 7.3: outside orders, written down
SETTLE_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _owner_order(fake, ticker="KXBIKECYCLE", *, side="yes", price="0.98", count=1,
                 with_fill=False, **kw):
    """An outside order: no client_order_id of ours, real money out."""
    fake.add_market(ticker, title=ticker, close_time=CLOSE)
    # No explicit timestamps: the scan's window starts at the genesis these tests stamp a
    # moment earlier, so "now" is the only time that is inside it.
    oid = fake.add_personal_order(ticker, side=side, count=count, price=D(price), **kw)
    if with_fill:
        fake.add_personal_fill(ticker, side, count, D(price), move_balance=False)
    return oid


def test_the_scan_records_a_personal_order_with_the_exchange_s_own_money(env):
    """Before this table the scan audited the order and nothing read the audit, so the
    owner's 2026-08-30 trade sat as unexplained drift and halted the system."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    oid = _owner_order(fake)

    counts = settle.settle_once(lg, fake, st)

    assert counts["personal_seen"] == 1
    rows = lg.personal_orders()
    assert len(rows) == 1
    row = rows[0]
    assert row["order_id"] == oid and row["ticker"] == "KXBIKECYCLE"
    assert row["side"] == "yes" and row["contracts"] == "1.0000"
    assert row["cost"] == "0.9800"                 # contracts x price, fee NOT included
    assert row["fee_source"] == "exchange"         # the payload's own figure, not a model
    assert D(row["fee"]) > D("0")
    assert row["on_harness_ticker"] == 0
    assert row["settled_at"] is None and row["payout"] is None
    assert row["first_seen_at"] is not None
    # the audit the scan always wrote is still written
    assert len(lg.audit_events(event="personal_fill_observed")) == 1


def test_the_row_is_upserted_and_first_seen_never_moves(env):
    """The scan re-reads a 24-hour overlap on every pass and re-judges everything on the
    weekly full scan, so the same order arrives many times. The row must say when the
    harness FIRST saw it, not when it last looked."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake)
    settle.settle_once(lg, fake, st, now=SETTLE_NOW)
    first_seen = lg.personal_orders()[0]["first_seen_at"]

    settle.settle_once(lg, fake, st, now=SETTLE_NOW + timedelta(days=1), full_scan=True)

    rows = lg.personal_orders()
    assert len(rows) == 1                          # one order, one row
    assert rows[0]["first_seen_at"] == first_seen
    # the audit is still once per order, not once per sighting
    assert len(lg.audit_events(event="personal_fill_observed")) == 1


def test_a_personal_order_on_a_ticker_we_trade_is_flagged(env):
    lg, fake, st = env
    aid = _placed_attempt(lg)
    _filled_bet(lg, aid, 1, "KXSHARED", is_real=1, coid=f"{aid}-B01")
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, "KXSHARED", side="no", price="0.55")

    settle.settle_once(lg, fake, st)

    assert lg.personal_orders()[0]["on_harness_ticker"] == 1


def test_a_paper_row_on_the_ticker_does_not_flag_it(env):
    """Netting is about positions the exchange actually holds; a paper row is not one."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    _filled_bet(lg, aid, 1, "KXSHARED", is_real=0)
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, "KXSHARED", side="no", price="0.55")

    settle.settle_once(lg, fake, st)

    assert lg.personal_orders()[0]["on_harness_ticker"] == 0


def test_an_impostor_is_never_recorded_as_a_personal_order(env, monkeypatch):
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    monkeypatch.setattr(settle, "_set_halt", lambda *a: None)
    fake.add_impostor_order("A-9999-B01")

    settle.settle_once(lg, fake, st)

    assert lg.personal_orders() == []


def test_an_order_that_filled_nothing_is_not_recorded(env):
    """No fills, no money, no position to settle later."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, filled=0, status="canceled")

    settle.settle_once(lg, fake, st)

    assert lg.personal_orders() == []
    assert len(lg.audit_events(event="personal_fill_observed")) == 1   # still audited


@pytest.mark.parametrize(
    ("side", "result", "payout"),
    [("yes", "yes", "1.0000"), ("no", "yes", "0.0000"), ("yes", "", None)],
)
def test_a_personal_order_is_paid_out_when_its_market_settles(env, side, result, payout):
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, side=side, with_fill=True)
    settle.settle_once(lg, fake, st)
    fake.resolve("KXBIKECYCLE", result)

    counts = settle.settle_once(lg, fake, st, now=SETTLE_NOW)

    assert counts["personal_settled"] == 1
    row = lg.personal_orders()[0]
    assert row["settled_at"] == "2026-07-10T12:00:00Z"
    if payout is None:      # a void refunds cost and fee, as it does for our own legs
        assert D(row["payout"]) == q4(D(row["cost"]) + D(row["fee"]))
    else:
        assert row["payout"] == payout
    # …and a second pass leaves it alone.
    assert settle.settle_once(lg, fake, st, now=SETTLE_NOW)["personal_settled"] == 0


def test_a_scalar_market_pays_the_exchange_s_own_value(env):
    """docs/16 §5, applied to the owner's side of the account: the exchange pays a value
    of its own choosing per contract and keeps the fee."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, side="no", price="0.75", with_fill=True)
    settle.settle_once(lg, fake, st)
    fake.resolve("KXBIKECYCLE", "scalar", scalar_value=D("0.18"))

    settle.settle_once(lg, fake, st, now=SETTLE_NOW)

    # the NO side of a $0.18 YES value is $0.82 a contract
    assert lg.personal_orders()[0]["payout"] == "0.8200"


def test_an_unsettled_market_leaves_the_row_open(env):
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, with_fill=True)

    counts = settle.settle_once(lg, fake, st, now=SETTLE_NOW)

    assert counts["personal_settled"] == 0
    assert lg.personal_orders()[0]["settled_at"] is None


def test_settle_scores_a_drawdown_floor_reject(env):
    """docs/22 section 7.4 end to end: the floor writes refused rows rather than paper
    ones, and a refused row is a counterfactual the settle pass scores like any other."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    # the row exactly as ``execute._select_real`` writes one under the floor
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="FL-1", side="yes",
        limit_price=D("0.40"), model_prob=D("0.55"), rationale="r", status="rejected",
        reject_code="drawdown_floor",
        reject_reason="drawdown_floor: live balance 9.0000 is below the 10.0000 floor",
        contracts=None, declared_contracts=D("1"), is_real=0,
    )
    fake.add_market("FL-1", title="FL-1", close_time=CLOSE, yes_ask=D("0.40"),
                    yes_ask_size=10)
    fake.resolve("FL-1", "yes")

    counts = settle.settle_once(lg, fake, st, now=SETTLE_NOW)

    assert counts["rejects_scored"] == 1
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (f"{aid}-B01",)).fetchone()
    assert row["hypothetical_outcome"] == "win"
    assert D(row["hypothetical_pnl"]) > D("0")
    assert row["status"] == "rejected"                  # the row itself never moves
    assert row["reject_reason"].startswith("drawdown_floor:")


def test_a_personal_sell_is_never_booked_as_a_cost(env):
    """A sale credits the account. Booking it as a cost would move the expected balance by
    twice the trade, so the table refuses the shape it cannot express and says why."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    before = fake.balance
    oid = _owner_order(fake, action="sell", price="0.60", count=2)

    counts = settle.settle_once(lg, fake, st)

    assert fake.balance > before                   # the fake credited it, as a sale does
    assert lg.personal_orders() == []              # …and nothing was booked as a cost
    assert counts["personal_seen"] == 1            # still seen and still audited
    ev = lg.audit_events(event="personal_order_unrecorded")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["order_id"] == oid and detail["action"] == "sell"
    assert detail["reason"] == "action is sell, not buy"


def test_the_unrecorded_audit_names_every_reason_once_per_order(env):
    """Each skip leaves one row, and only one: the scan re-reads its overlap window on
    every pass, and a reason repeated every fifteen minutes is a reason nobody reads."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, action="sell")
    _owner_order(fake, "KXNOFILL", filled=0, status="canceled")

    settle.settle_once(lg, fake, st)
    settle.settle_once(lg, fake, st, full_scan=True)

    reasons = sorted(
        json.loads(e["detail"])["reason"]
        for e in lg.audit_events(event="personal_order_unrecorded")
    )
    assert reasons == ["action is sell, not buy", "the order filled nothing"]


def test_on_harness_ticker_is_stamped_once_and_never_re_derived(env):
    """It answers "was this order netted against a position of ours", which is a fact
    about the moment the order was placed. Re-deriving it on every sighting means a
    harness bet weeks later flips a settled-in order out of the walk, and the expected
    balance jumps by its cost and fee on a night nothing happened."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, "KXSHARED")
    settle.settle_once(lg, fake, st)
    assert lg.personal_orders()[0]["on_harness_ticker"] == 0

    # the harness trades that ticker afterwards, and the weekly full scan re-reads the order
    _filled_bet(lg, aid, 1, "KXSHARED", is_real=1, coid=f"{aid}-B01")
    settle.settle_once(lg, fake, st, full_scan=True)

    assert lg.personal_orders()[0]["on_harness_ticker"] == 0


def test_a_netted_personal_order_is_never_settled_by_this_pass(env):
    """Its payout belongs to a netted pair the exchange paid against the account, so there
    is nothing here to attribute. Left in the population it would take the deferred branch
    on every tick forever and keep the night's reconciliation deferring with it."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    _filled_bet(lg, aid, 1, "KXSHARED", is_real=1, coid=f"{aid}-B01")
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake, "KXSHARED", side="no", price="0.55", with_fill=True)
    settle.settle_once(lg, fake, st)
    assert lg.personal_orders()[0]["on_harness_ticker"] == 1
    fake.resolve("KXSHARED", "yes")

    counts = settle.settle_once(lg, fake, st, now=SETTLE_NOW)

    assert counts["personal_settled"] == 0
    row = lg.personal_orders()[0]
    assert row["settled_at"] is None and row["payout"] is None
    assert lg.audit_events(event="scalar_settlement_deferred") == []
    assert lg.audit_events(event="personal_settle_error") == []


# ------------------------- docs/24: the canary is not an outside order
# The live record's shape, from the ledger that refused the deposit on 2026-09-16, with
# synthetic identifiers. The client order id keeps the `CANARY-<epoch>` form, which must
# not match the harness's own `A-NNNN-BNN` pattern.
CANARY_COID = "CANARY-1700000000"
CANARY_OID = "00000000-0000-4000-8000-000000000001"
CANARY_TICKER = "KXALIENS-27"


def _live_canary(lg, *, with_response=True):
    """``meta.canary`` in the shape the live one holds, response object included."""
    record = {
        "ts": "2026-08-01T21:06:39Z", "ticker": CANARY_TICKER, "side": "yes",
        "contracts": "1", "fill_price": "0.0620", "fee": "0.0041",
        "coid": CANARY_COID, "settled": False, "payout": None,
    }
    if with_response:
        record["response"] = {"order_id": CANARY_OID, "status": "executed"}
    lg.meta_set("canary", json.dumps(record))


def _canary_order(fake, *, coid=CANARY_COID, order_id=CANARY_OID):
    """The canary as ``/portfolio/orders`` reports it: a real order on the account whose
    ``client_order_id`` deliberately does NOT match our pattern."""
    return _owner_order(fake, CANARY_TICKER, price="0.0620", count=1,
                        client_order_id=coid, order_id=order_id)


def test_the_canary_is_never_filed_as_one_of_the_owners_orders(env):
    """The go-live defect. The canary carries a deliberately non-matching client order id
    so the impostor tripwire cannot claim it, and the scan read that as "not ours, not an
    impostor, therefore the owner's". The walk then counted its cost twice, once in its
    canary term and once in its personal term, and refused a $100 deposit over $0.0661."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _live_canary(lg)
    _canary_order(fake)

    counts = settle.settle_once(lg, fake, st, full_scan=True)

    assert lg.personal_orders() == []
    assert counts["personal_seen"] == 0
    assert counts["impostors"] == 0
    assert lg.audit_events(event="personal_fill_observed") == []
    assert lg.audit_events(event="personal_order_unrecorded") == []
    assert lg.audit_events(event="unknown_fill") == []


def test_the_scan_removes_a_personal_row_an_earlier_pass_wrote_for_the_canary(env):
    """What Arno's ``settle --full`` has to undo: the row the first full scan wrote before
    this was fixed. Deleting it is what takes the double count out of the walk."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _live_canary(lg)
    _canary_order(fake)
    lg.upsert_personal_order(                       # the stale row, as the live ledger has it
        CANARY_OID, ticker=CANARY_TICKER, side="yes",
        created_time="2026-08-01T21:06:39Z", contracts=D("1"), cost=D("0.0620"),
        fee=D("0.0041"), fee_source="exchange", first_seen_at="2026-09-12T00:00:00Z",
    )

    settle.settle_once(lg, fake, st, full_scan=True)

    assert lg.personal_orders() == []
    ev = lg.audit_events(event="personal_order_withdrawn")
    assert len(ev) == 1
    assert json.loads(ev[0]["detail"])["order_id"] == CANARY_OID

    # …and a second pass is a no-op rather than a second audit row.
    settle.settle_once(lg, fake, st, full_scan=True)
    assert len(lg.audit_events(event="personal_order_withdrawn")) == 1


def test_the_canary_is_recognized_by_its_exchange_order_id_alone(env):
    """The orders feed shows the canary under either identifier, so both are checked. A
    canary whose coid the feed omits is still the harness's own order."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _live_canary(lg)
    _canary_order(fake, coid=None)

    settle.settle_once(lg, fake, st, full_scan=True)

    assert lg.personal_orders() == []
    assert lg.audit_events(event="personal_fill_observed") == []


def test_the_canary_is_recognized_by_its_coid_when_the_record_has_no_response(env):
    """The older ``meta.canary`` shape carries no ``response`` object."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _live_canary(lg, with_response=False)
    _canary_order(fake, order_id="some-other-order-id")

    settle.settle_once(lg, fake, st, full_scan=True)

    assert lg.personal_orders() == []
    assert lg.audit_events(event="personal_fill_observed") == []


def test_with_no_canary_recorded_the_scan_is_unchanged(env):
    """Outside orders stay outside orders; only the canary is carved out."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _owner_order(fake)

    counts = settle.settle_once(lg, fake, st, full_scan=True)

    assert len(lg.personal_orders()) == 1
    assert counts["personal_seen"] == 1
    assert lg.audit_events(event="personal_order_withdrawn") == []


def test_an_impostor_is_still_caught_beside_a_canary(env, monkeypatch):
    """The carve-out is one order wide. Everything else the scan judged, it still judges."""
    lg, fake, st = env
    settle.genesis_snapshot(lg, fake)
    _live_canary(lg)
    _canary_order(fake)
    monkeypatch.setattr(settle, "_set_halt", lambda *a: None)
    fake.add_impostor_order("A-9999-B01")

    counts = settle.settle_once(lg, fake, st, full_scan=True)

    assert counts["impostors"] == 1
    assert len(lg.audit_events(event="unknown_fill")) == 1
    assert lg.personal_orders() == []


# ------------------------------------ docs/25: the fee the exchange actually charged
def _our_order(fake, lg, aid, idx, ticker, *, price="0.0500", contracts=3, stored_fee):
    """One of our own real legs, placed through the fake so the exchange charges what it
    charges, with ``stored_fee`` written on the row: the figure the old client produced by
    multiplying the exchange's per-contract average by the fill count."""
    fake.add_market(ticker, title=ticker, close_time=CLOSE, yes_ask=D(price),
                    yes_ask_size=50)
    coid = f"{aid}-B{idx:02d}"
    r = fake.create_order(ticker, "yes", D(price), contracts, coid)
    assert r.filled_count == contracts, "fixture expects a full fill"
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=idx, ticker=ticker, side="yes",
        limit_price=D(price), rationale="r", is_real=1, status="filled",
        contracts=contracts, fill_price=r.avg_fill_price,
        stake=q4(D(contracts) * r.avg_fill_price), fee=D(stored_fee),
        order_id=r.order_id, client_order_id=coid, placed_at="2026-09-17T13:00:00Z",
    )
    return coid, q4(D(r.fee))


def test_the_scan_corrects_a_fee_the_exchange_charged_differently(env):
    """The live defect. Three legs on 2026-09-17 carried a fee built by multiplying the
    exchange's already-rounded per-contract average by the fill count, which is a
    hundredth of a cent short of what was charged on each. The client stores the reported
    total now; this is what heals the rows already written."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid, charged = _our_order(fake, lg, aid, 1, "KXFEE", stored_fee="0.0099")
    assert charged == D("0.0100")

    settle.settle_once(lg, fake, st, full_scan=True)

    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    assert D(row["fee"]) == D("0.0100")
    ev = lg.audit_events(event="fee_corrected")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["stored_fee"] == "0.0099" and detail["charged_fee"] == "0.0100"
    assert detail["contracts"] == "3.00" and detail["ticker"] == "KXFEE"
    assert ev[0]["bet_id"] == coid and ev[0]["attempt_id"] == aid

    # …and a second pass has nothing left to do.
    settle.settle_once(lg, fake, st, full_scan=True)
    assert len(lg.audit_events(event="fee_corrected")) == 1


def test_a_fee_that_already_agrees_is_left_alone(env):
    """A one-contract leg was always right, which is why 352 earlier legs reconciled to
    the cent and sizing is what exposed this."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    _our_order(fake, lg, aid, 1, "KXONE", contracts=1, stored_fee="0.0034")

    settle.settle_once(lg, fake, st, full_scan=True)

    assert lg.audit_events(event="fee_corrected") == []


@pytest.mark.parametrize(
    ("outcome", "stored_pnl", "corrected_pnl"),
    [("loss", "-0.1599", "-0.1600"), ("win", "2.8401", "2.8400")],
)
def test_a_settled_rows_pnl_moves_with_its_fee(env, outcome, stored_pnl, corrected_pnl):
    """``pnl`` is ``payout - stake - fee``, so a fee that rises by a hundredth of a cent
    lowers the P/L by exactly that and leaves every other term where it was. Both columns
    move in one update, because a row carrying one without the other does not add up.

    Skipping settled rows was this function's first shape and it was wrong: once fills
    stop adding to the error the ledger sits at a CONSTANT sub-cent drift, which is
    precisely what the repeated-drift rule halts the system for on its third night.
    """
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid, charged = _our_order(fake, lg, aid, 1, "KXFEE", stored_fee="0.0099")
    lg.update_bet(coid, status="settled", outcome=outcome, pnl=D(stored_pnl),
                  settled_at="2026-09-18T00:00:00Z")

    settle.settle_once(lg, fake, st, full_scan=True)

    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    assert D(row["fee"]) == charged == D("0.0100")
    assert row["pnl"] == corrected_pnl
    # …and the corrected P/L is what the formula gives for the fee that was charged
    assert D(row["pnl"]) == bet_pnl(outcome, D("3"), D("0.05"), charged)

    ev = lg.audit_events(event="fee_corrected")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["status"] == "settled"
    assert (detail["stored_fee"], detail["charged_fee"]) == ("0.0099", "0.0100")
    assert (detail["stored_pnl"], detail["corrected_pnl"]) == (stored_pnl, corrected_pnl)

    # a second pass has nothing left to do, and does not walk the P/L again
    settle.settle_once(lg, fake, st, full_scan=True)
    assert len(lg.audit_events(event="fee_corrected")) == 1
    assert lg.conn.execute(
        "SELECT pnl FROM bets WHERE bet_id=?", (coid,)
    ).fetchone()["pnl"] == corrected_pnl


def test_a_voided_rows_zeroed_fee_is_left_alone(env):
    """Settle zeroes a void's fee on purpose, and the walk's refund term is built on that:
    a void nets exactly zero by construction. Writing the charged fee here would break the
    construction to fix a figure that is not wrong."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid, _charged = _our_order(fake, lg, aid, 1, "KXFEE", stored_fee="0.0099")
    lg.update_bet(coid, status="voided", outcome="void", pnl=D("0"), fee=D("0"),
                  settled_at="2026-09-18T00:00:00Z")

    settle.settle_once(lg, fake, st, full_scan=True)

    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    assert D(row["fee"]) == D("0") and D(row["pnl"]) == D("0")
    assert lg.audit_events(event="fee_corrected") == []


def test_a_settled_row_with_no_pnl_to_move_is_left_alone(env):
    """There is nothing to keep consistent, so the fee stays where it is and the money
    shows up as drift, which is the outcome a half-written row would have hidden."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid, _charged = _our_order(fake, lg, aid, 1, "KXFEE", stored_fee="0.0099")
    lg.update_bet(coid, status="settled", outcome="loss",
                  settled_at="2026-09-18T00:00:00Z")

    settle.settle_once(lg, fake, st, full_scan=True)

    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    assert D(row["fee"]) == D("0.0099") and row["pnl"] is None
    assert lg.audit_events(event="fee_corrected") == []


def test_a_corrected_leg_moves_its_group_s_stored_realization(env):
    """The one sum this ledger stores rather than computes. ``_settle_groups`` stamps it
    once and never revisits a settled group, so a leg corrected afterwards would leave it
    stale. Nothing new can reach this path: groups left the execution path with the
    rebuild, and every group in the ledger belongs to an earlier era."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    lg.insert_group("G-1", aid, "[]", D("0"))
    coid, _charged = _our_order(fake, lg, aid, 1, "KXFEE", stored_fee="0.0099")
    lg.conn.execute("UPDATE bets SET group_id='G-1' WHERE bet_id=?", (coid,))
    lg.conn.commit()
    lg.update_bet(coid, status="settled", outcome="loss", pnl=D("-0.1599"),
                  settled_at="2026-09-18T00:00:00Z")
    lg.set_group("G-1", status="settled", realized_pnl=D("-0.1599"))

    settle.settle_once(lg, fake, st, full_scan=True)

    group = lg.groups_for_attempt(aid)[0]
    assert group["realized_pnl"] == "-0.1600"
    detail = json.loads(lg.audit_events(event="fee_corrected")[0]["detail"])
    assert detail["group_realized_pnl"] == "-0.1600"


def test_a_count_that_disagrees_is_left_to_the_fills_check(env):
    """A fee is only comparable against the same position. A count mismatch is a bigger
    problem than a hundredth of a cent, and it is ``fills_match``'s to raise."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid, _charged = _our_order(fake, lg, aid, 1, "KXFEE", stored_fee="0.0099")
    lg.update_bet(coid, contracts=D("2"))          # the row now claims a different size

    settle.settle_once(lg, fake, st, full_scan=True)

    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    assert D(row["fee"]) == D("0.0099")
    assert lg.audit_events(event="fee_corrected") == []


# ------------------------------- 2026-09-27: the stake the exchange actually charged
# A-0316-B01's four pieces, in the order the live book was walked.
_A0316_LEVELS = "levels:1.18@0.11,0.76@0.12,0.41@0.13,0.65@0.20"


def _multi_level_leg(fake, lg, aid, idx, ticker, *, fee="0.0245"):
    """A-0316-B01's shape: one real leg of 3 YES contracts that filled across four book
    levels for $0.4043 and a $0.0243 fee, with the row as placement wrote it. That is the
    create response's truncated average ($0.1347) as the price, that average times the
    count ($0.4041) as the stake, and ``fee``, by default the model priced at that average
    ($0.0245)."""
    fake.add_market(ticker, title=ticker, close_time=CLOSE, yes_ask=D("0.11"),
                    yes_ask_size=50)
    fake.set_order_behavior(ticker, _A0316_LEVELS)
    coid = f"{aid}-B{idx:02d}"
    r = fake.create_order(ticker, "yes", D("0.45"), 3, coid)
    assert r.avg_fill_price == D("0.1347")
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=idx, ticker=ticker, side="yes",
        limit_price=D("0.45"), rationale="r", is_real=1, status="filled",
        contracts=3, fill_price=r.avg_fill_price, stake=q4(3 * r.avg_fill_price),
        fee=D(fee), order_id=r.order_id, client_order_id=coid,
        placed_at="2026-09-27T00:25:50Z",
    )
    return coid


def _row(lg, bet_id):
    return lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bet_id,)).fetchone()


def test_the_scan_heals_the_stake_and_fee_of_a_leg_filled_at_several_prices(env):
    """What the scan should have done on 2026-09-27. It corrected the fee from $0.0245 to
    the $0.0243 the exchange charged, which was right, and left the stake at $0.4041,
    $0.0002 short of the $0.4043 the contracts cost. The two errors had been cancelling,
    so fixing one alone put that night's walk $0.0002 out. Both are healed now, from the
    same order payload, and the price column keeps its four-place average for display."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid = _multi_level_leg(fake, lg, aid, 1, "KXLOWTDC")

    settle.settle_once(lg, fake, st, full_scan=True)

    row = _row(lg, coid)
    assert (row["stake"], row["fee"], row["fill_price"]) == ("0.4043", "0.0243", "0.1347")
    fee_ev = json.loads(lg.audit_events(event="fee_corrected")[0]["detail"])
    assert (fee_ev["stored_fee"], fee_ev["charged_fee"]) == ("0.0245", "0.0243")
    stake_ev = lg.audit_events(event="stake_corrected")
    assert len(stake_ev) == 1 and stake_ev[0]["bet_id"] == coid
    detail = json.loads(stake_ev[0]["detail"])
    assert (detail["stored_stake"], detail["charged_stake"]) == ("0.4041", "0.4043")
    assert detail["status"] == "filled" and detail["contracts"] == "3.00"

    # a second pass has nothing left to do
    settle.settle_once(lg, fake, st, full_scan=True)
    assert len(lg.audit_events(event="stake_corrected")) == 1
    assert len(lg.audit_events(event="fee_corrected")) == 1


@pytest.mark.parametrize(
    ("outcome", "stored_pnl", "corrected_pnl"),
    [("win", "2.5713", "2.5714"), ("loss", "-0.4287", "-0.4286")],
)
def test_a_leg_settled_before_the_fix_has_its_pnl_rebuilt_from_the_charged_stake(
        env, outcome, stored_pnl, corrected_pnl):
    """A-0316-B01 as the live ledger holds it: settled, the price re-rounded to $0.1348 by
    settlement, the stake column still $0.4041 from placement, and a P/L taken from 3 x
    $0.1348, which is neither. The P/L is rebuilt from the charged stake and the fee
    rather than moved by the stake's difference, because the difference between two wrong
    stakes is not the error in the P/L."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid = _multi_level_leg(fake, lg, aid, 1, "KXLOWTDC", fee="0.0243")
    assert D(stored_pnl) == bet_pnl(outcome, D("3"), D("0.1348"), D("0.0243"))
    lg.update_bet(coid, status="settled", outcome=outcome, fill_price=D("0.1348"),
                  pnl=D(stored_pnl), settled_at="2026-09-27T11:31:31Z")

    settle.settle_once(lg, fake, st, full_scan=True)

    row = _row(lg, coid)
    assert (row["stake"], row["fee"], row["pnl"]) == ("0.4043", "0.0243", corrected_pnl)
    assert lg.audit_events(event="fee_corrected") == []
    detail = json.loads(lg.audit_events(event="stake_corrected")[0]["detail"])
    assert (detail["stored_stake"], detail["charged_stake"]) == ("0.4041", "0.4043")
    assert (detail["stored_pnl"], detail["corrected_pnl"]) == (stored_pnl, corrected_pnl)


def test_a_scalar_legs_stake_correction_keeps_the_payout_the_walk_recovers(env):
    """A scalar payout survives on the row only as ``pnl + stake + fee``, so the P/L moves
    by the stake's difference and that sum stays where it was."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid = _multi_level_leg(fake, lg, aid, 1, "KXLOWTDC", fee="0.0243")
    lg.update_bet(coid, status="settled", outcome="scalar", pnl=D("1.0716"),
                  settled_at="2026-09-27T11:31:31Z")      # a $1.50 payout on $0.4041

    settle.settle_once(lg, fake, st, full_scan=True)

    row = _row(lg, coid)
    assert (row["stake"], row["pnl"]) == ("0.4043", "1.0714")
    assert D(row["pnl"]) + D(row["stake"]) + D(row["fee"]) == D("1.5000")


def test_settlement_books_the_fills_own_cost_not_the_rounded_average(env):
    """The settle loop's half. It re-prices the leg at the fills' average, $0.1348 to four
    places, and used to book the P/L on 3 x $0.1348 = $0.4044. It books the fills' own
    sum now, the $0.4043 the exchange took. No genesis here, so the scan does not run and
    the stored fee stands: this pins the settle loop alone."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    coid = _multi_level_leg(fake, lg, aid, 1, "KXLOWTDC")
    fake.resolve("KXLOWTDC", "yes")

    settle.settle_once(lg, fake, st)

    row = _row(lg, coid)
    assert (row["status"], row["outcome"]) == ("settled", "win")
    assert (row["fill_price"], row["stake"], row["fee"]) == ("0.1348", "0.4043", "0.0245")
    assert row["pnl"] == "2.5712"                  # 3 - 0.4043 - 0.0245
    assert lg.audit_events(event="reconcile_mismatch") == []


def test_a_stake_that_already_agrees_is_left_alone(env):
    """One price, whole cents: ``contracts x fill_price`` is the cost exactly, which is why
    this went unseen until orders began filling across several levels."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    _our_order(fake, lg, aid, 1, "KXONE", contracts=3, stored_fee="0.0100")

    settle.settle_once(lg, fake, st, full_scan=True)

    assert lg.audit_events(event="stake_corrected") == []


@pytest.mark.parametrize("shape", ["voided", "count_differs", "settled_without_pnl"])
def test_the_stake_correction_leaves_alone_what_the_fee_correction_leaves_alone(env, shape):
    """A void nets to zero by construction, a count that disagrees is the fills check's to
    raise, and a settled row with no P/L has nothing to keep consistent."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    coid = _multi_level_leg(fake, lg, aid, 1, "KXLOWTDC", fee="0.0243")
    if shape == "voided":
        lg.update_bet(coid, status="voided", outcome="void", pnl=D("0"), fee=D("0"),
                      settled_at="2026-09-27T11:31:31Z")
    elif shape == "count_differs":
        lg.update_bet(coid, contracts=D("2"))
    else:
        lg.update_bet(coid, status="settled", outcome="win",
                      settled_at="2026-09-27T11:31:31Z")

    settle.settle_once(lg, fake, st, full_scan=True)

    assert _row(lg, coid)["stake"] == "0.4041"
    assert lg.audit_events(event="stake_corrected") == []


def test_a_stake_correction_moves_its_group_s_stored_realization(env):
    """The one stored sum, kept consistent the same way the fee correction keeps it."""
    lg, fake, st = env
    aid = _placed_attempt(lg)
    settle.genesis_snapshot(lg, fake)
    lg.insert_group("G-1", aid, "[]", D("0"))
    coid = _multi_level_leg(fake, lg, aid, 1, "KXLOWTDC", fee="0.0243")
    lg.conn.execute("UPDATE bets SET group_id='G-1' WHERE bet_id=?", (coid,))
    lg.conn.commit()
    lg.update_bet(coid, status="settled", outcome="win", fill_price=D("0.1348"),
                  pnl=D("2.5713"), settled_at="2026-09-27T11:31:31Z")
    lg.set_group("G-1", status="settled", realized_pnl=D("2.5713"))

    settle.settle_once(lg, fake, st, full_scan=True)

    assert lg.groups_for_attempt(aid)[0]["realized_pnl"] == "2.5714"
    detail = json.loads(lg.audit_events(event="stake_corrected")[0]["detail"])
    assert detail["group_realized_pnl"] == "2.5714"
