"""Reconciliation — the master balance audit (Jul29 spec L8).

Every case drives the money-consistent ``FakeKalshi``: orders are placed THROUGH
``create_order`` (which debits stake + fee) and settled through ``resolve`` (which
credits winners), so a clean walk drifting to exactly zero is a real proof rather than
a rehearsal of the same arithmetic on both sides. The ledger rows are then written by
hand through the DAO to mirror what execute/settle would have recorded.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from betting_agent.config import load_settings
from betting_agent.harness import reconcile, settle
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.kalshi.types import Fill
from betting_agent.ledger.db import Ledger
from betting_agent.moneymath import bet_pnl, q4
from betting_agent.moneymath import fee as calc_fee

GENESIS = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 7, 29, 23, 5, tzinfo=UTC)
CLOSE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
COEF = D("0.07")


@pytest.fixture
def env(tmp_path):
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    settings = load_settings(root=tmp_path)
    fake = FakeKalshi(balance=D("30.16"))
    yield ledger, fake, settings
    ledger.close()


# --------------------------------------------------------------------------- seeding
def _attempt(lg):
    _, aid = lg.create_attempt(
        env="prod", model="claude-sonnet-5", effort="high", memory_mode="on",
        prompt_version="p1", toolkit_version="0.1.0", workspace_path="/ws",
    )
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    return aid


def _place_real(lg, fake, aid, idx, ticker, *, side="yes", price="0.40", contracts=1,
                placed_at=None, stored_fee=None):
    """Place a real order on the fake (debiting the balance) and record the bets row.

    ``stored_fee`` overrides what the ROW says the fee was, leaving what the exchange
    charged where it is: the shape of the docs/25 defect, where the client built a total
    by multiplying an already-rounded per-contract average.
    """
    fake.add_market(ticker, title=ticker, close_time=CLOSE,
                    yes_ask=D(price) if side == "yes" else None,
                    yes_ask_size=50 if side == "yes" else 0,
                    no_ask=D(price) if side == "no" else None,
                    no_ask_size=50 if side == "no" else 0)
    coid = f"{aid}-B{idx:02d}"
    r = fake.create_order(ticker, side, D(price), contracts, coid)
    assert r.filled_count == contracts, "fixture expects a full fill"
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=idx, ticker=ticker, side=side,
        limit_price=D(price), model_prob=D("0.60"), rationale="r", is_real=1,
        status="filled", contracts=contracts, fill_price=r.avg_fill_price,
        stake=q4(D(contracts) * r.avg_fill_price),
        fee=D(stored_fee) if stored_fee is not None else D(r.fee),
        order_id=r.order_id, client_order_id=coid,
        placed_at=(placed_at or GENESIS + timedelta(hours=1)).isoformat().replace(
            "+00:00", "Z"),
    )
    return coid


def _settle_real(lg, fake, coid, result, *, ts=None):
    """Resolve the market on the fake and mirror settle.py's ledger update."""
    bet = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    when = ts or GENESIS + timedelta(hours=5)
    fake.resolve(bet["ticker"], result, ts=when)
    settled_at = when.isoformat().replace("+00:00", "Z")
    if result not in ("yes", "no"):  # void: fee refunded, pnl zeroed (settle.py)
        lg.update_bet(coid, status="voided", outcome="void", pnl=D("0"), fee=D("0"),
                      settled_at=settled_at)
        return
    outcome = "win" if bet["side"] == result else "loss"
    pnl = bet_pnl(outcome, int(bet["contracts"]), D(bet["fill_price"]), D(bet["fee"]))
    lg.update_bet(coid, status="settled", outcome=outcome, pnl=pnl, settled_at=settled_at)


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


def _go_live(lg, fake):
    lg.meta_set("live_genesis_ts", GENESIS.isoformat().replace("+00:00", "Z"))
    lg.meta_set("live_genesis_balance", str(fake.balance))


def _clean_world(lg, fake):
    """Genesis at the seeded balance, then a win, a loss and a void — all through the
    fake, so the exchange's own money trail is the reference for the walk."""
    _go_live(lg, fake)
    aid = _attempt(lg)
    win = _place_real(lg, fake, aid, 1, "KXWIN", price="0.40")
    loss = _place_real(lg, fake, aid, 2, "KXLOSS", price="0.30")
    void = _place_real(lg, fake, aid, 3, "KXVOID", price="0.20")
    _settle_real(lg, fake, win, "yes")
    _settle_real(lg, fake, loss, "no")
    _settle_real(lg, fake, void, "")
    return aid


# --------------------------------------------------------------------------- paper era
def test_paper_era_is_a_noop(env):
    lg, fake, s = env
    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out == {"skipped": "paper_era"}
    assert lg.latest_reconciliation() is None
    assert lg.audit_events(event="reconcile_ok") == []
    assert lg.audit_events(event="reconcile_drift") == []
    assert not s.halt_path.exists()


# --------------------------------------------------------------------------- clean walk
def test_clean_walk_drifts_zero(env):
    lg, fake, s = env
    _clean_world(lg, fake)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.0000")
    assert out["ok"] is True
    assert out["failed_checks"] == []
    assert out["report"] is None
    assert not s.halt_path.exists()

    row = lg.latest_reconciliation()
    assert row["ok"] == 1
    assert row["drift"] == "0.0000"
    assert row["actual_balance"] == str(q4(fake.balance))
    detail = json.loads(row["detail"])
    assert detail["debits"]["n"] == 3
    assert detail["credits"]["n"] == 3
    for name in ("fills_match", "settlements_covered"):
        assert detail["checks"][name]["ok"] is True
    assert detail["verdict"] == "exact"

    events = lg.audit_events(event="reconcile_ok")
    assert len(events) == 1
    assert json.loads(events[0]["detail"]) == {"drift": "0.0000"}


def test_clean_walk_matches_bet_pnl_sum(env):
    """The walk's own cross-proof: expected − genesis must equal Σ net P/L (after fees)."""
    lg, fake, s = env
    _clean_world(lg, fake)
    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    pnl_sum = q4(sum(
        (D(r["pnl"]) for r in lg.conn.execute(
            "SELECT pnl FROM bets WHERE pnl IS NOT NULL").fetchall()), D("0")
    ))
    assert out["expected"] - D("30.16") == pnl_sum


def test_open_real_bet_is_debited_but_not_credited(env):
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    _place_real(lg, fake, aid, 1, "KXOPEN", price="0.40")

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is True
    assert out["drift"] == D("0.0000")
    detail = out["detail"]
    assert detail["debits"]["n"] == 1
    assert detail["credits"]["n"] == 0
    expected_debit = q4(D("0.40") + calc_fee(1, D("0.40"), COEF))
    assert detail["debits"]["total"] == str(expected_debit)


def test_pre_genesis_bets_are_excluded(env):
    """A bet placed before genesis already sits inside the genesis balance."""
    lg, fake, s = env
    aid = _attempt(lg)
    _place_real(lg, fake, aid, 1, "KXOLD", price="0.40",
                placed_at=GENESIS - timedelta(hours=3))
    _go_live(lg, fake)  # genesis snapshot taken AFTER the old bet's debit

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["detail"]["debits"]["n"] == 0
    assert out["drift"] == D("0.0000")
    assert out["ok"] is True


# --------------------------------------------------------------------------- drift
def test_foreign_debit_drifts_and_halts(env):
    """Six contracts, so the debit clears ``reconcile.halt_drift_usd`` and the night is
    ``large``. The same debit at one contract is a ``noted`` night; that is its own test
    below."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.add_personal_fill("KXFOREIGN", "yes", 6, D("0.50"),
                           ts=GENESIS + timedelta(hours=2))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    stolen = q4(D("3.00") + calc_fee(6, D("0.50"), COEF))
    assert out["drift"] == q4(-stolen)
    assert out["ok"] is False
    assert out["verdict"] == "large"
    assert out["halted"] is True
    assert s.halt_path.exists()
    assert s.halt_path.read_text().splitlines()[0] == "reconcile_drift"

    assert out["report"] == s.reports_dir / "reconcile-2026-07-29.md"
    text = out["report"].read_text(encoding="utf-8")
    assert "# Reconciliation — 2026-07-29" in text
    assert "**FAIL**" in text
    assert "**drift** (actual − expected)" in text
    assert f"${q4(-stolen)}" in text

    row = lg.latest_reconciliation()
    assert row["ok"] == 0
    assert row["drift"] == str(q4(-stolen))
    events = lg.audit_events(event="reconcile_drift")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["drift"] == str(q4(-stolen))
    assert detail["failed_checks"] == []  # money-only failure: the checks still pass


def test_bare_settlement_fails_settlements_covered_at_zero_drift(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    # add_settlement injects a settlement with no fill and no balance move: drift stays 0.
    fake.add_settlement("KXORPHAN", "yes", ts=GENESIS + timedelta(hours=6))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.0000")
    assert out["ok"] is False
    assert out["failed_checks"] == ["settlements_covered"]
    uncovered = out["checks"]["settlements_covered"]["uncovered"]
    assert [u["ticker"] for u in uncovered] == ["KXORPHAN"]
    assert lg.latest_reconciliation()["ok"] == 0
    assert s.halt_path.exists()
    text = out["report"].read_text(encoding="utf-8")
    assert "`settlements_covered`: **FAIL**" in text
    assert "KXORPHAN" in text


def test_fee_mismatch_trips_fills_match(env):
    lg, fake, s = env
    aid = _clean_world(lg, fake)
    # Overwrite a recorded fee far from what the fills imply (> $0.01 tolerance).
    lg.update_bet(f"{aid}-B01", fee=D("0.99"))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is False
    assert "fills_match" in out["failed_checks"]
    problems = out["checks"]["fills_match"]["problems"]
    assert [p["problem"] for p in problems] == ["fee_mismatch"]


def test_a_sub_cent_fee_error_is_still_a_mismatch(env):
    """docs/14 D5 follow-through: fees live on the $0.0001 grid, so the tolerance must
    out-resolve them. Under the old $0.01 band this 50-times-the-grid error was invisible
    (the whole fee on a low-priced bet is smaller than a cent)."""
    lg, fake, s = env
    aid = _clean_world(lg, fake)
    row = lg.conn.execute("SELECT fee FROM bets WHERE bet_id = ?", (f"{aid}-B01",)).fetchone()
    lg.update_bet(f"{aid}-B01", fee=D(row["fee"]) + D("0.0050"))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is False
    problems = out["checks"]["fills_match"]["problems"]
    assert [p["problem"] for p in problems] == ["fee_mismatch"]


def test_voided_bet_skips_the_fee_check_and_nets_zero(env):
    """settle.py zeroes a void's fee on purpose; that must not read as a fee mismatch."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    void = _place_real(lg, fake, aid, 1, "KXVOIDONLY", price="0.25")
    _settle_real(lg, fake, void, "")

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is True
    assert out["checks"]["fills_match"]["problems"] == []
    assert out["drift"] == D("0.0000")


# --------------------------------------------------------------------------- canary
def test_post_genesis_canary_cost_is_in_the_walk(env):
    lg, fake, s = env
    _go_live(lg, fake)
    # The canary is placed after genesis: the fake debits it, and meta.canary explains it.
    fake.add_market("KXCANARY", title="canary", close_time=CLOSE,
                    yes_ask=D("0.05"), yes_ask_size=10)
    r = fake.create_order("KXCANARY", "yes", D("0.05"), 1, "CANARY-1")
    lg.meta_set("canary", json.dumps({
        "ts": (GENESIS + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "ticker": "KXCANARY", "side": "yes", "contracts": 1,
        "fill_price": str(q4(r.avg_fill_price)), "fee": str(q4(D(r.fee))),
        "coid": "CANARY-1", "settled": False, "payout": None,
    }))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is True
    assert out["drift"] == D("0.0000")
    can = out["detail"]["canary"]
    assert can["cost_included"] is True
    assert can["payout_included"] is False
    assert can["cost"] == str(q4(D("0.05") + D(r.fee)))


def test_settled_canary_payout_and_its_settlement_are_covered(env):
    lg, fake, s = env
    _go_live(lg, fake)
    fake.add_market("KXCANARY", title="canary", close_time=CLOSE,
                    yes_ask=D("0.05"), yes_ask_size=10)
    r = fake.create_order("KXCANARY", "yes", D("0.05"), 1, "CANARY-1")
    settled_at = GENESIS + timedelta(hours=8)
    fake.resolve("KXCANARY", "yes", ts=settled_at)
    lg.meta_set("canary", json.dumps({
        "ts": (GENESIS + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "ticker": "KXCANARY", "side": "yes", "contracts": 1,
        "fill_price": str(q4(r.avg_fill_price)), "fee": str(q4(D(r.fee))),
        "coid": "CANARY-1", "settled": True, "outcome": "win", "payout": "1.0000",
        "settled_at": settled_at.isoformat().replace("+00:00", "Z"),
    }))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is True, out["failed_checks"]
    assert out["drift"] == D("0.0000")
    assert out["detail"]["canary"]["payout_included"] is True
    # the canary's settlement has no bets row, but the canary ticker is ours
    assert out["checks"]["settlements_covered"]["uncovered"] == []


def test_pre_genesis_canary_terms_are_excluded(env):
    """A canary placed before the snapshot already sits inside the genesis balance."""
    lg, fake, s = env
    fake.add_market("KXCANARY", title="canary", close_time=CLOSE,
                    yes_ask=D("0.05"), yes_ask_size=10)
    r = fake.create_order("KXCANARY", "yes", D("0.05"), 1, "CANARY-1")
    _go_live(lg, fake)  # genesis AFTER the canary debit
    lg.meta_set("canary", json.dumps({
        "ts": (GENESIS - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "ticker": "KXCANARY", "side": "yes", "contracts": 1,
        "fill_price": str(q4(r.avg_fill_price)), "fee": str(q4(D(r.fee))),
        "coid": "CANARY-1", "settled": False, "payout": None,
    }))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["detail"]["canary"]["cost_included"] is False
    assert out["detail"]["canary"]["cost"] == "0.0000"
    assert out["ok"] is True
    assert out["drift"] == D("0.0000")


# --------------------------------------------------------------------------- row shape
def test_row_money_is_4dp_text(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    reconcile.reconcile_once(lg, fake, s, now=NOW)
    row = lg.latest_reconciliation()
    for col in ("expected_balance", "actual_balance", "drift"):
        assert row[col].count(".") == 1
        assert len(row[col].split(".")[1]) == 4


# --------------------------------------------------------------------------- exact counts
def test_fractional_fills_summing_to_one_pass_the_fills_check(env):
    """Bet A-0054-B01, live 2026-08-02 — the false HALT this package exists to prevent.

    One real 1-contract bet at $0.1500, filled by the exchange as three fractional
    pieces: "0.28" + "0.34" + "0.38". Int-truncating each fill summed the join to 0, so
    ``fills_match`` reported count_mismatch (recorded 1, actual 0) and the loop HALTed
    overnight — while this very walk balanced to the cent. The join is fed the raw live
    payload shape, because the truncation happened while parsing it.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    coid = f"{aid}-B01"
    when = GENESIS + timedelta(hours=1)
    fake.add_market("KXFRAC", title="KXFRAC", close_time=CLOSE,
                    yes_ask=D("0.15"), yes_ask_size=50)
    # the order carries our coid — that IS the attribution path — and moves the money
    # once; its fills come back through the join below
    fake.add_personal_order("KXFRAC", "yes", count=1, price=D("0.15"),
                            client_order_id=coid, ts=when)
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=1, ticker="KXFRAC", side="yes",
        limit_price=D("0.15"), model_prob=D("0.60"), rationale="r", is_real=1,
        status="filled", contracts=1, fill_price=D("0.15"), stake=D("0.15"),
        fee=D("0.0090"), client_order_id=coid,  # the receipt: ceil4(0.07*1*0.15*0.85)
        placed_at=when.isoformat().replace("+00:00", "Z"),
    )
    client = _LiveFillsClient(fake, coid, _fractional_fill_payloads("KXFRAC", "0.1500"))

    out = reconcile.reconcile_once(lg, client, s, now=NOW)

    assert out["checks"]["fills_match"]["ok"] is True
    assert out["checks"]["fills_match"]["problems"] == []
    assert out["drift"] == D("0.0000")
    assert out["ok"] is True and out["failed_checks"] == []
    assert not s.halt_path.exists()  # no HALT: nothing was ever wrong


def test_priceless_fill_is_excluded_from_the_weighted_average_denominator(env):
    """MP-6: the count-weighted average price must divide by the fills that carry a
    price, not by every fill. One priced fill (1 @ 0.40) plus one priceless fill (1,
    price unknown) must average to 0.40 (the one priced fill), not 0.20 (notional over
    every fill) — and it audits once per affected bet instead of silently mis-pricing."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    coid = f"{aid}-B01"
    when = GENESIS + timedelta(hours=1)
    fake.add_market("KXPRICELESS", title="KXPRICELESS", close_time=CLOSE,
                    yes_ask=D("0.40"), yes_ask_size=50)
    fake.add_personal_order("KXPRICELESS", "yes", count=2, price=D("0.40"),
                            client_order_id=coid, ts=when)
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=1, ticker="KXPRICELESS", side="yes",
        limit_price=D("0.40"), model_prob=D("0.60"), rationale="r", is_real=1,
        status="filled", contracts=2, fill_price=D("0.40"), stake=D("0.80"),
        fee=D("0.0336"), client_order_id=coid,  # the receipt: ceil4(0.07*2*0.40*0.60)
        placed_at=when.isoformat().replace("+00:00", "Z"),
    )
    payloads = [
        {"ticker": "KXPRICELESS", "side": "yes", "count_fp": "1",
         "yes_price_dollars": "0.4000", "order_id": "OID-PRICELESS", "is_taker": True,
         "created_time": "2026-08-02T20:15:00Z"},
        {"ticker": "KXPRICELESS", "side": "yes", "count_fp": "1",
         "order_id": "OID-PRICELESS", "is_taker": True,
         "created_time": "2026-08-02T20:15:01Z"},   # no *_price_dollars -> priceless
    ]
    client = _LiveFillsClient(fake, coid, payloads)

    out = reconcile.reconcile_once(lg, client, s, now=NOW)

    fm = out["checks"]["fills_match"]
    assert fm["problems"] == []            # 0.40 recorded == 0.40 averaged (priced fill only)
    assert fm["priceless"] == [{"bet_id": coid, "total_count": "2", "priced_count": "1"}]
    assert out["drift"] == D("0.0000") and out["ok"] is True
    detail = json.loads(lg.audit_events(event="priceless_fills")[0]["detail"])
    assert detail == {"total_count": "2", "priced_count": "1"}


def test_a_real_count_mismatch_is_still_caught(env):
    """The tolerance is Decimal equality, not a fudge factor: a genuinely short fill
    still fails the check and still HALTs."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    coid = f"{aid}-B01"
    when = GENESIS + timedelta(hours=1)
    fake.add_market("KXSHORT", title="KXSHORT", close_time=CLOSE,
                    yes_ask=D("0.20"), yes_ask_size=50)
    fake.add_personal_order("KXSHORT", "yes", count=1, price=D("0.20"),
                            client_order_id=coid, ts=when)
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=1, ticker="KXSHORT", side="yes",
        limit_price=D("0.20"), model_prob=D("0.60"), rationale="r", is_real=1,
        status="filled", contracts=1, fill_price=D("0.20"), stake=D("0.20"),
        fee=D("0.02"), client_order_id=coid,
        placed_at=when.isoformat().replace("+00:00", "Z"),
    )
    client = _LiveFillsClient(fake, coid, _fractional_fill_payloads(
        "KXSHORT", "0.2000", pieces=("0.60",)
    ))

    out = reconcile.reconcile_once(lg, client, s, now=NOW)

    problems = out["checks"]["fills_match"]["problems"]
    assert [p["problem"] for p in problems] == ["count_mismatch"]
    assert problems[0]["recorded"] == "1" and problems[0]["actual"] == "0.60"
    assert out["ok"] is False and s.halt_path.exists()


class _RepricedFillsClient:
    """The fake, except ``/portfolio/fills`` reports a different price for one ticker.

    The reconciliation reads fills ONCE per run into a shared ``order_id -> fills`` index
    (EF-1), so re-pricing has to happen where that index is built — overriding
    ``find_fills_by_client_order_id`` would never be consulted for an order the index
    already resolves. The ledger row and the fake's balance are both left alone, so the
    balance walk stays clean and the fills check is the only thing under test.
    """

    def __init__(self, inner, ticker, price):
        self._inner, self._ticker, self._price = inner, ticker, D(price)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_fills(self, min_ts=None, cursor=None):
        fills, nxt = self._inner.get_fills(min_ts=min_ts, cursor=cursor)
        return [
            f.model_copy(update={"price": self._price}) if f.ticker == self._ticker else f
            for f in fills
        ], nxt


def test_a_price_mismatch_beyond_a_cent_is_caught(env):
    """MP-8: ``_check_fills_match``'s price branch, which nothing reached.

    The recorded fill price is what the P/L is computed from, so a row claiming 0.40 for
    a position the exchange filled at 0.20 reports the wrong number on every downstream
    metric — silently, and forever, because settlement has already run. The tolerance is
    a cent (rounding on a count-weighted average); 20 cents is not rounding.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    # Placed and debited at 0.40 through the fake, so the BALANCE walk stays clean.
    coid = _place_real(lg, fake, aid, 1, "KXPRICE", price="0.40")
    client = _RepricedFillsClient(fake, "KXPRICE", "0.20")

    out = reconcile.reconcile_once(lg, client, s, now=NOW)

    problems = out["checks"]["fills_match"]["problems"]
    # A 20-cent reprice mismatches the fee too (the recorded fee was computed at 0.40,
    # the actual price implies 0.0112) — under the D5-tightened tolerance both surface.
    assert [p["bet_id"] for p in problems] == [coid, coid]
    assert [p["problem"] for p in problems] == ["price_mismatch", "fee_mismatch"]
    assert problems[0]["recorded"] == "0.4000" and problems[0]["actual"] == "0.2000"
    assert out["drift"] == D("0.0000")       # the money is where it should be...
    assert out["ok"] is False                # ...and the position still is not
    assert out["failed_checks"] == ["fills_match"]
    assert s.halt_path.exists()


def test_a_cent_of_price_movement_is_within_tolerance(env):
    """The other side of that line: a count-weighted average can legitimately land a cent
    off a single recorded price, and one cent must not HALT the system overnight."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    _place_real(lg, fake, aid, 1, "KXNEAR", price="0.40")
    client = _RepricedFillsClient(fake, "KXNEAR", "0.41")

    out = reconcile.reconcile_once(lg, client, s, now=NOW)

    assert out["checks"]["fills_match"]["problems"] == []
    assert out["ok"] is True and not s.halt_path.exists()


def test_a_filled_real_bet_with_no_client_order_id_is_a_problem(env):
    """MP-8: the ``missing_client_order_id`` branch.

    The coid is the ONLY way a fill is attributed to us — ``get_fills`` serves none, so
    the join runs orders→fills through it (FK-4), and the impostor discriminator is
    'carries one of our coids'. A real filled row without one is therefore unverifiable,
    not merely inconvenient: it can never be proved ours, so the check refuses to skip it
    quietly. It is also NOT counted as verified, so it can never advance the watermark.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    coid = _place_real(lg, fake, aid, 1, "KXNOCOID", price="0.40")
    # Whatever loses the coid (a legacy row, a partial write), the row still holds a
    # real, debited position — so the balance walk is clean and only this check fires.
    lg.conn.execute("UPDATE bets SET client_order_id=NULL WHERE bet_id=?", (coid,))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    fm = out["checks"]["fills_match"]
    assert fm["problems"] == [{"bet_id": coid, "problem": "missing_client_order_id"}]
    assert fm["n_bets"] == 0                 # never counted as checked...
    assert fm["verified_through"] is None    # ...so it cannot be watermarked either
    assert out["drift"] == D("0.0000")
    assert out["ok"] is False and s.halt_path.exists()
    assert lg.meta_get("fills_verified_through") is None


# --------------------------------------------------------------------------- NO side
def test_real_no_side_bet_walks_and_joins_clean(env):
    """MP-7: a real NO buy — quoted on the single YES book as an ask at 1-q and converted
    back on the way in — through the fills join and the balance walk. The sharpest edge
    of the V2 migration had no test at all."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    coid = _place_real(lg, fake, aid, 1, "KXNO", side="no", price="0.30")
    bet = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    assert bet["side"] == "no" and bet["fill_price"] == "0.3000"  # NO terms, not 0.70
    _settle_real(lg, fake, coid, "no")  # the NO side wins

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.0000")
    assert out["ok"] is True and out["failed_checks"] == []
    fills = fake.find_fills_by_client_order_id(coid)
    assert [f.side for f in fills] == ["no"]
    assert sum(f.count for f in fills) == 1 and fills[0].price == D("0.30")
    assert lg.bets_for_attempt(aid)[0]["pnl"] == str(
        bet_pnl("win", 1, D("0.30"), D(bet["fee"]))
    )


# ------------------------------------------------- late-but-attributable settlements (D1)
def _late_settlement(lg, fake, *, ticker="KXLATE", side="yes", price="0.40", result="yes"):
    """A real bet the exchange has already settled while our settle pass has NOT: the row
    is still ``filled`` and the payout is already in the balance. This is MP-1's world."""
    aid = _attempt(lg)
    coid = _place_real(lg, fake, aid, 1, ticker, side=side, price=price)
    fake.resolve(ticker, result, ts=GENESIS + timedelta(hours=5))
    return aid, coid


def test_late_settlement_is_provisional_not_a_halt(env):
    """The MP-1 reproduction. Before D1 this was a nonzero drift plus a failing
    ``settlements_covered`` — an overnight HALT over money that is not missing, merely not
    written down yet."""
    lg, fake, s = env
    _go_live(lg, fake)
    _aid, coid = _late_settlement(lg, fake)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["ok"] is False          # it is NOT clean — the drift is real, just explained
    assert out["halted"] is False
    assert not s.halt_path.exists()
    assert out["drift"] == D("1.0000")            # the exchange's $1 payout on a win
    assert out["failed_checks"] == ["settlements_covered"]
    prov = out["provisional"]
    assert prov["attributed"] == "1.0000"
    assert prov["uncovered"] == ["KXLATE"]
    assert [p["bet_id"] for p in prov["pending"]] == [coid]
    assert prov["pending"][0]["implied_outcome"] == "win"
    assert lg.latest_reconciliation() is None     # a deferral is not a completed run
    assert len(lg.audit_events(event="reconcile_provisional")) == 1
    assert lg.audit_events(event="reconcile_drift") == []
    assert out["report"] is None


def test_the_next_settle_pass_closes_the_gap_and_reconcile_is_clean(env):
    """The other half of the reproduction: deferring is only right because the next tick
    fixes it. Settle for real (not a hand-mirrored row) and walk again."""
    lg, fake, s = env
    _go_live(lg, fake)
    _late_settlement(lg, fake)
    assert reconcile.reconcile_once(lg, fake, s, now=NOW).get("provisional")

    settle.settle_once(lg, fake, s)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is True and out["drift"] == D("0.0000")
    assert out.get("provisional") is None
    assert not s.halt_path.exists()
    assert lg.latest_reconciliation()["ok"] == 1


def test_a_late_loss_defers_at_zero_drift(env):
    """A losing settlement pays nothing, so only ``settlements_covered`` fails. Attribution
    still has to account for it — a zero-drift residual is a residual."""
    lg, fake, s = env
    _go_live(lg, fake)
    _late_settlement(lg, fake, result="no")       # our YES bet lost

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.0000")
    assert out["provisional"]["attributed"] == "0.0000"
    assert out["provisional"]["pending"][0]["implied_outcome"] == "loss"
    assert not s.halt_path.exists()


def test_a_late_void_defers_on_the_refunded_stake_and_fee(env):
    """A void refunds stake AND fee, which is exactly what ``settle.py`` will record."""
    lg, fake, s = env
    _go_live(lg, fake)
    _aid, coid = _late_settlement(lg, fake, ticker="KXVOIDLATE", price="0.25", result="")
    bet = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    refund = q4(D(bet["fill_price"]) * D("1") + D(bet["fee"]))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == refund
    assert out["provisional"]["attributed"] == str(refund)
    assert out["provisional"]["pending"][0]["implied_outcome"] == "void"
    assert not s.halt_path.exists()


def test_an_extra_uncovered_settlement_still_halts(env):
    """One residual ticker and the whole run is a HALT again — the gate is total."""
    lg, fake, s = env
    _go_live(lg, fake)
    _late_settlement(lg, fake)
    fake.add_settlement("KXORPHAN", "yes", ts=GENESIS + timedelta(hours=6))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out.get("provisional") is None
    assert out["ok"] is False and out["halted"] is True
    assert s.halt_path.read_text().splitlines()[0] == "reconcile_drift"
    assert len(lg.audit_events(event="reconcile_drift")) == 1
    assert lg.audit_events(event="reconcile_provisional") == []


def test_an_unattributed_cent_still_halts(env):
    """The drift must equal the implied payouts EXACTLY. A foreign debit riding along
    inside an otherwise explainable night is precisely what must not be waved through."""
    lg, fake, s = env
    _go_live(lg, fake)
    _late_settlement(lg, fake)
    fake.add_personal_fill("KXFOREIGN", "yes", 8, D("0.50"),
                           ts=GENESIS + timedelta(hours=2))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    stolen = q4(D("4.00") + calc_fee(8, D("0.50"), COEF))
    assert out["drift"] == q4(D("1.0000") - stolen)
    assert out.get("provisional") is None
    assert out["halted"] is True and s.halt_path.exists()


def _coidless_bet(lg, fake, aid, idx, ticker, *, price="0.40"):
    """A real bet recorded with NO ``client_order_id`` whose order carries a foreign one.

    That is a ``fills_match`` ``missing_client_order_id`` problem which (a) leaves the
    balance walk untouched, so the drift stays exactly attributable, and (b) is not an
    impostor, because a foreign coid does not match our pattern. It isolates one residual.
    """
    fake.add_market(ticker, title=ticker, close_time=CLOSE, yes_ask=D(price),
                    yes_ask_size=50)
    r = fake.create_order(ticker, "yes", D(price), 1, f"manual-app-{idx}")
    bet_id = f"{aid}-B{idx:02d}"
    lg.insert_bet(
        bet_id=bet_id, attempt_id=aid, ticket_index=idx, ticker=ticker, side="yes",
        limit_price=D(price), model_prob=D("0.60"), rationale="r", is_real=1,
        status="filled", contracts=1, fill_price=r.avg_fill_price,
        stake=q4(D("1") * r.avg_fill_price), fee=D(r.fee), order_id=r.order_id,
        client_order_id=None,
        placed_at=(GENESIS + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    )
    return bet_id


def test_a_fills_problem_on_an_unrelated_bet_still_halts(env):
    """A ``fills_match`` failure the pending settlements do not explain is a residual."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    stray = _coidless_bet(lg, fake, aid, 1, "KXSTRAY")
    _settle_real(lg, fake, stray, "yes")          # terminal, covered — and NOT pending
    _place_real(lg, fake, aid, 2, "KXLATE", price="0.40")
    fake.resolve("KXLATE", "yes", ts=GENESIS + timedelta(hours=5))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    problems = out["checks"]["fills_match"]["problems"]
    assert [(p["bet_id"], p["problem"]) for p in problems] == [
        (stray, "missing_client_order_id")
    ]
    assert out["drift"] == D("1.0000")            # the drift alone WAS attributable
    assert out.get("provisional") is None
    assert out["halted"] is True and s.halt_path.exists()


def test_a_fills_problem_on_the_pending_bet_is_still_deferred(env):
    """The same defect on the bet whose settlement we are waiting for stays inside the
    attributable population. It is not tolerated forever: the caller's four-deferral bound
    runs the night with the gate off, and this then HALTs."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    late = _coidless_bet(lg, fake, aid, 1, "KXLATE")
    fake.resolve("KXLATE", "yes", ts=GENESIS + timedelta(hours=5))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert [p["bet_id"] for p in out["checks"]["fills_match"]["problems"]] == [late]
    assert out["provisional"]["attributed"] == "1.0000"
    assert not s.halt_path.exists()

    forced = reconcile.reconcile_once(lg, fake, s, now=NOW, allow_provisional=False)
    assert forced.get("provisional") is None
    assert forced["halted"] is True and s.halt_path.exists()


def test_allow_provisional_false_halts_the_very_same_run(env):
    """The bound-exhausted path: the identical world, judged with the gate off."""
    lg, fake, s = env
    _go_live(lg, fake)
    _late_settlement(lg, fake)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW, allow_provisional=False)

    assert out.get("provisional") is None
    assert out["ok"] is False and out["halted"] is True
    assert lg.latest_reconciliation()["ok"] == 0     # a completed run, recorded
    assert s.halt_path.exists()


def test_a_clean_night_never_consults_the_attribution_gate(env):
    """Belt and braces: the gate is only reachable from a dirty run."""
    lg, fake, s = env
    _clean_world(lg, fake)
    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is True and out.get("provisional") is None
    assert lg.audit_events(event="reconcile_provisional") == []


def _unsettled_canary(lg, fake, *, ticker="KXCANARY", side="yes", price="0.05"):
    """A filled, unsettled ``meta.canary`` placed after genesis — the one position the walk
    owns that lives outside ``bets`` (D1's "still-filled real bets" under-enumerated it)."""
    fake.add_market(ticker, title="canary", close_time=CLOSE,
                    yes_ask=D(price) if side == "yes" else None,
                    yes_ask_size=10 if side == "yes" else 0,
                    no_ask=D(price) if side == "no" else None,
                    no_ask_size=10 if side == "no" else 0)
    r = fake.create_order(ticker, side, D(price), 1, "CANARY-1")
    lg.meta_set("canary", json.dumps({
        "ts": (GENESIS + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "ticker": ticker, "side": side, "contracts": 1,
        "fill_price": str(q4(r.avg_fill_price)), "fee": str(q4(D(r.fee))),
        "coid": "CANARY-1", "settled": False, "payout": None,
    }))
    return r


def test_a_late_canary_win_defers_provisionally(env):
    """The canary's market finalized between the settle pass and the balance read. Its
    payout is real money the walk has not been told about yet — a deferral, not a HALT."""
    lg, fake, s = env
    _go_live(lg, fake)
    _unsettled_canary(lg, fake)
    fake.resolve("KXCANARY", "yes", ts=GENESIS + timedelta(hours=8))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("1.0000")            # 1 contract x $1
    assert out["halted"] is False and not s.halt_path.exists()
    prov = out["provisional"]
    assert prov["attributed"] == "1.0000"
    assert prov["pending"] == []                  # no bets involved at all
    assert prov["canary"] == {
        "ticker": "KXCANARY", "side": "yes", "market_result": "yes",
        "implied_outcome": "win", "implied_payout": "1.0000",
    }
    assert lg.latest_reconciliation() is None
    assert len(lg.audit_events(event="reconcile_provisional")) == 1


def test_a_late_canary_settlement_plus_a_stray_cent_still_halts(env):
    """Composition is all-or-nothing: the canary explains its dollar, and the foreign fill
    beside it explains nothing, so the night HALTs exactly as L8 demands."""
    lg, fake, s = env
    _go_live(lg, fake)
    _unsettled_canary(lg, fake)
    fake.resolve("KXCANARY", "yes", ts=GENESIS + timedelta(hours=8))
    fake.add_personal_fill("KXFOREIGN", "yes", 8, D("0.50"),
                           ts=GENESIS + timedelta(hours=2))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    stolen = q4(D("4.00") + calc_fee(8, D("0.50"), COEF))
    assert out["drift"] == q4(D("1.0000") - stolen)
    assert out.get("provisional") is None
    assert out["halted"] is True and s.halt_path.exists()
    assert lg.audit_events(event="reconcile_provisional") == []


def test_a_late_canary_and_a_late_bet_compose_into_one_attribution(env):
    """Both populations sum into one total that must match the drift to the cent."""
    lg, fake, s = env
    _go_live(lg, fake)
    _unsettled_canary(lg, fake)
    fake.resolve("KXCANARY", "yes", ts=GENESIS + timedelta(hours=8))
    _aid, coid = _late_settlement(lg, fake)       # a bet payout on top

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("2.0000")
    prov = out["provisional"]
    assert prov["attributed"] == "2.0000" and prov["n_pending"] == 2
    assert [p["bet_id"] for p in prov["pending"]] == [coid]
    assert prov["canary"]["implied_payout"] == "1.0000"
    assert not s.halt_path.exists()


def test_a_settled_canary_is_not_a_pending_position(env):
    """Only an UNSETTLED canary is attributable; once settle stamps it the walk owns the
    payout outright, so any residual after that is real drift."""
    lg, fake, s = env
    _go_live(lg, fake)
    r = _unsettled_canary(lg, fake)
    settled_at = GENESIS + timedelta(hours=8)
    fake.resolve("KXCANARY", "yes", ts=settled_at)
    lg.meta_set("canary", json.dumps({
        "ts": (GENESIS + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        "ticker": "KXCANARY", "side": "yes", "contracts": 1,
        "fill_price": str(q4(r.avg_fill_price)), "fee": str(q4(D(r.fee))),
        "coid": "CANARY-1", "settled": True, "outcome": "win", "payout": "1.0000",
        "settled_at": settled_at.isoformat().replace("+00:00", "Z"),
    }))
    fake.add_personal_fill("KXFOREIGN", "yes", 8, D("0.50"),
                           ts=GENESIS + timedelta(hours=2))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out.get("provisional") is None
    assert out["halted"] is True and s.halt_path.exists()


# ------------------------------------------------- EF-1: the verification watermark
def _fills_check(out):
    return out["checks"]["fills_match"]


def test_a_clean_run_stamps_the_fills_watermark(env):
    lg, fake, s = env
    _clean_world(lg, fake)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["ok"] is True
    assert _fills_check(out)["n_bets"] == 3 and _fills_check(out)["n_skipped"] == 0
    # the newest placed_at the run stood behind, stored verbatim (not reformatted)
    placed = [b["placed_at"] for b in lg.conn.execute("SELECT placed_at FROM bets").fetchall()]
    assert lg.meta_get("fills_verified_through") == max(placed)


def test_a_previously_verified_bet_is_skipped_on_the_next_run(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    reconcile.reconcile_once(lg, fake, s, now=NOW)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))

    assert out["ok"] is True
    # MP-6 added "priceless" (bets with an unpriced fill) alongside "problems".
    assert _fills_check(out) == {
        "ok": True, "n_bets": 0, "n_skipped": 3,
        "watermark": _fills_check(out)["watermark"],
        "verified_through": _fills_check(out)["verified_through"],
        "problems": [],
        "priceless": [],
    }
    assert _fills_check(out)["n_skipped"] == 3  # every bet carried over, none re-fetched


def test_a_new_bet_is_verified_while_the_old_ones_are_skipped(env):
    lg, fake, s = env
    aid = _clean_world(lg, fake)
    reconcile.reconcile_once(lg, fake, s, now=NOW)
    _place_real(lg, fake, aid, 4, "KXNEW", price="0.25",
                placed_at=GENESIS + timedelta(hours=9))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))

    assert out["ok"] is True
    assert (_fills_check(out)["n_bets"], _fills_check(out)["n_skipped"]) == (1, 3)


def test_full_ignores_the_watermark_and_re_verifies_everything(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    reconcile.reconcile_once(lg, fake, s, now=NOW)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1), full=True)

    assert (_fills_check(out)["n_bets"], _fills_check(out)["n_skipped"]) == (3, 0)
    assert _fills_check(out)["watermark"] is None


def test_the_report_states_the_skip_population(env):
    """A silent skip would be the whole objection to a watermark; it is never silent."""
    lg, fake, s = env
    _clean_world(lg, fake)
    reconcile.reconcile_once(lg, fake, s, now=NOW)
    fake.set_balance(fake.balance + D("5.00"))  # past the halt bound, so a report is written

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))

    assert out["verdict"] == "large"
    text = out["report"].read_text()
    assert "Fills coverage: verified 0 new, skipped 3 previously verified" in text


def test_a_drifting_run_still_advances_the_watermark_when_the_fills_matched(env):
    """docs/22 section 7.2. The watermark means "a passing fills join stood behind every
    bet at or before this placed_at", and a balance drift does not unmake that. Tying it
    to the night's verdict meant one drift re-verified the whole book every night after."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(fake.balance + D("5.00"))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["verdict"] == "large" and s.halt_path.exists()
    assert out["checks"]["fills_match"]["ok"] is True
    placed = [b["placed_at"] for b in lg.conn.execute("SELECT placed_at FROM bets").fetchall()]
    assert lg.meta_get("fills_verified_through") == max(placed)


def test_a_failing_fills_check_never_advances_the_watermark(env):
    """The one thing that does stop it: the claim itself failing."""
    lg, fake, s = env
    aid = _clean_world(lg, fake)
    _coidless_bet(lg, fake, aid, 4, "KXSTRAY")      # missing_client_order_id

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["checks"]["fills_match"]["ok"] is False
    assert lg.meta_get("fills_verified_through") is None


def test_a_provisional_run_never_advances_the_watermark(env):
    """WP1's deferral semantics rule the watermark too: a run that did not conclude
    cannot claim to have verified anything."""
    lg, fake, s = env
    _go_live(lg, fake)
    _late_settlement(lg, fake)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["provisional"] is not None
    assert lg.meta_get("fills_verified_through") is None


def test_a_skipped_bet_is_still_re_proved_by_the_nightly_balance_walk(env):
    """The watermark skips the FILLS JOIN, never the money. Every post-genesis bet stays
    in the balance walk forever, which is what makes skipping the join safe."""
    lg, fake, s = env
    _clean_world(lg, fake)
    reconcile.reconcile_once(lg, fake, s, now=NOW)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))

    assert _fills_check(out)["n_skipped"] == 3
    assert out["detail"]["debits"]["n"] == 3  # all three still walked
    assert out["drift"] == D("0.0000")


def test_a_bet_with_no_readable_placed_at_is_never_skipped(env):
    lg, fake, s = env
    aid = _clean_world(lg, fake)
    reconcile.reconcile_once(lg, fake, s, now=NOW)
    coid = _place_real(lg, fake, aid, 4, "KXODD", price="0.25")
    lg.update_bet(coid, placed_at="not-a-timestamp")

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))

    assert _fills_check(out)["n_bets"] == 1  # undatable rows are re-verified, always


# ------------------------------------------------- EF-1: request cost
def _pages(n, size):
    return -(-n // size)


def test_the_orders_feed_is_paged_once_per_run(env):
    lg, fake, s = env
    fake.page_size = 2
    _clean_world(lg, fake)
    fake.reset_calls()

    reconcile.reconcile_once(lg, fake, s, now=NOW)

    # 3 orders at 2/page = 2 pages, shared by the coid map AND the impostor scan.
    assert fake.calls["get_orders"] == 2
    assert "get_fills_by_order" not in fake.calls  # the index answered every bet


def _twelve_month_world(lg, fake, *, n_bets, n_personal, page_size):
    """An account a year old: months of somebody else's orders, plus our own bets."""
    fake.page_size = page_size
    _go_live(lg, fake)
    for i in range(n_personal):
        fake.add_personal_order(
            f"KXPERSONAL{i}", ts=GENESIS + timedelta(hours=i), move_balance=False,
        )
    aid = _attempt(lg)
    for i in range(n_bets):
        _place_real(lg, fake, aid, i + 1, f"KXBET{i}", price="0.40",
                    placed_at=GENESIS + timedelta(days=300, minutes=i))
    return aid


def test_reconcile_once_request_count_at_twelve_month_account_age(env):
    """The EF-1/MP-4 regression guard — the one the canary-hang class never had.

    Before the shared pagination, every real bet ever placed triggered its own
    ``find_fills_by_client_order_id``, and each of those re-paged ``/portfolio/orders``
    from page one: 20 bets over a 500-order history is ~420 requests, nightly, growing
    forever. The bound below is deliberately generous — it is not pinning an exact count,
    it is refusing anything that scales with bets x pages.
    """
    lg, fake, s = env
    n_bets, n_personal, page_size = 20, 500, 25
    _twelve_month_world(lg, fake, n_bets=n_bets, n_personal=n_personal, page_size=page_size)
    fake.reset_calls()

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["ok"] is True and out["drift"] == D("0.0000")
    order_pages = _pages(n_personal + n_bets, page_size)     # 21
    fill_pages = _pages(n_bets, page_size)                   # 1
    assert fake.calls["get_orders"] == order_pages           # ONE pass, not one per bet
    assert fake.calls_total() <= order_pages + fill_pages + 5
    # and the quadratic shape is nowhere near: it would have been >= 400 here
    assert fake.calls_total() < n_bets * order_pages / 2


def test_the_second_night_at_twelve_month_age_verifies_nothing_new(env):
    """The watermark is what keeps the population from growing with the account."""
    lg, fake, s = env
    _twelve_month_world(lg, fake, n_bets=20, n_personal=500, page_size=25)
    reconcile.reconcile_once(lg, fake, s, now=NOW)
    fake.reset_calls()

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))

    assert (_fills_check(out)["n_bets"], _fills_check(out)["n_skipped"]) == (0, 20)
    assert "get_fills_by_order" not in fake.calls  # no per-bet join at all


# ------------------------------------------------- fold-in A: exact canary counts
def test_a_fractional_canary_is_costed_on_its_exact_size(env):
    """WP2 made ``canary.py`` write the exact count string; ``int()`` here turned a
    0.9-contract canary into a zero-cost one and produced drift out of thin air."""
    lg, fake, s = env
    _go_live(lg, fake)
    lg.meta_set("canary", json.dumps({
        "ts": (GENESIS + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "ticker": "KXCANARY", "side": "yes",
        "contracts": "0.90", "fill_price": "0.1500", "fee": "0.0100",
        "settled": False, "payout": None,
    }))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    canary = out["detail"]["canary"]
    assert canary["contracts"] == "0.90"
    assert canary["cost"] == "0.1450"  # 0.90 * 0.15 + 0.01, not 0.0100
    assert out["expected"] == q4(D(str(fake.balance)) - D("0.1450"))


# --------------------------------------------- WP-B B2: owner deposits and withdrawals
def _deposit(lg, fake, s, amount, *, direction="deposit", now=None):
    return reconcile.record_balance_adjustment(
        lg, fake, s, direction=direction, amount=D(amount), now=now or NOW
    )


def test_expected_balance_is_the_walk_with_no_exchange_call(env):
    """The verification anchor is the reconciliation's own walk, not a second opinion."""
    lg, fake, s = env
    _clean_world(lg, fake)

    walk = reconcile.expected_balance(lg, s)
    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert walk["expected"] == out["expected"]
    assert walk["genesis_balance"] == D("30.1600")
    assert walk["breakdown"]["debits"]["n"] == out["detail"]["debits"]["n"]


def test_expected_balance_is_none_in_the_paper_era(env):
    lg, fake, s = env
    assert reconcile.expected_balance(lg, s) is None


def test_a_verified_deposit_moves_the_anchor_and_leaves_the_era_boundary(env):
    """docs/14 B2, the mechanism: the anchor moves by the amount, the timestamp never does.

    Formalizes the 2026-08-10 hand edit. The deposit arrives at the exchange first (the fake
    is credited), and the command's job is to prove the credit is the stated transfer and
    nothing else before it touches the anchor.
    """
    lg, fake, s = env
    _clean_world(lg, fake)
    before = reconcile.expected_balance(lg, s)["expected"]
    fake.set_balance(str(q4(D(str(fake.balance)) + D("20.00"))))   # the money lands

    out = _deposit(lg, fake, s, "20.00")

    assert out["ok"] and out["reason"] is None
    assert out["implied"] == D("20.0000") and out["stated"] == D("20.0000")
    assert out["old_genesis_balance"] == D("30.1600")
    assert out["new_genesis_balance"] == D("50.1600")
    assert lg.meta_get("live_genesis_balance") == "50.1600"
    # the era boundary is a timestamp, not a balance (docs/14 B2)
    assert lg.meta_get("live_genesis_ts") == GENESIS.isoformat().replace("+00:00", "Z")
    # and the expectation moved by exactly the deposit
    assert reconcile.expected_balance(lg, s)["expected"] == q4(before + D("20.00"))


def test_the_deposit_event_carries_the_whole_derivation(env):
    """Anchor, flows, implied and stated — the shape the hand-recorded event set."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) + D("20.00"))))

    out = _deposit(lg, fake, s, "20.00")

    ev = lg.audit_events(event="deposit_recorded")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    # production-compatible keys (the 2026-08-10 row's own field names)
    assert detail["amount"] == "20.0000"
    assert detail["old_genesis_balance"] == "30.1600"
    assert detail["new_genesis_balance"] == "50.1600"
    assert detail["exchange_balance_verified"] == str(q4(D(str(fake.balance))))
    assert "derivation" in detail
    # and the structured superset B2 asks for: anchor, flows, implied, stated
    assert detail["anchor"] == {
        "ts": GENESIS.isoformat().replace("+00:00", "Z"), "balance": "30.1600",
    }
    assert detail["implied"] == "20.0000" and detail["stated"] == "20.0000"
    assert detail["flows"]["debits_n"] == 3 and detail["flows"]["credits_n"] == 3
    assert detail["direction"] == "deposit" and detail["tolerance"] == "0.0100"
    assert detail["authorized_by"] == "owner (betting-agent deposit)"
    # the paste-able entry: 2dp prose, 4dp evidence
    assert out["decisions_line"].startswith("## ")
    assert "$20.00 deposit recorded" in out["decisions_line"]
    assert detail["derivation"] in out["decisions_line"]
    assert "live_genesis_ts` unchanged" in out["decisions_line"]


def test_reconcile_passes_across_a_mid_stream_recorded_deposit(env):
    """The B2 acceptance criterion: flows before AND after a deposit walk to the cent.

    Pre-deposit bets settle, $20 arrives and is recorded, then more bets are placed and
    settled. Because the deposit moves the walk's anchor rather than inserting a
    pseudo-flow, every term on both sides of the transfer keeps its meaning and the nightly
    reconciliation is clean — which is the whole reason this lives next to the walk.
    """
    lg, fake, s = env
    aid = _clean_world(lg, fake)                        # pre-deposit flows, all settled
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["drift"] == D("0")

    fake.set_balance(str(q4(D(str(fake.balance)) + D("20.00"))))
    assert _deposit(lg, fake, s, "20.00")["ok"]

    # post-deposit flows: one winner, one loser, placed and settled after the transfer
    later = GENESIS + timedelta(days=1)
    win = _place_real(lg, fake, aid, 4, "KXPOSTWIN", price="0.35", placed_at=later)
    loss = _place_real(lg, fake, aid, 5, "KXPOSTLOSS", price="0.25", placed_at=later)
    _settle_real(lg, fake, win, "yes", ts=later + timedelta(hours=2))
    _settle_real(lg, fake, loss, "no", ts=later + timedelta(hours=2))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))

    assert out["ok"] and out["drift"] == D("0")
    assert out["actual"] == out["expected"]
    assert not out["halted"]
    assert out["detail"]["debits"]["n"] == 5 and out["detail"]["credits"]["n"] == 5


def test_a_deposit_the_exchange_disagrees_with_is_refused_and_writes_nothing(env):
    """The guard that matters: a wrong amount must never be absorbed into the anchor.

    The exchange is credited $20 and $25 is claimed. A hand edit would have folded the $5
    difference into the genesis balance and permanently erased the evidence that anything
    was ever unexplained; the command refuses and leaves the ledger untouched.
    """
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) + D("20.00"))))
    audit_before = len(lg.audit_events(limit=1000))

    out = _deposit(lg, fake, s, "25.00")

    assert not out["ok"] and out["reason"] == "amount_disagrees"
    assert out["implied"] == D("20.0000") and out["stated"] == D("25.0000")
    assert out["disagreement"] == D("5.0000")
    assert "NOTHING was written" in out["message"]
    assert "reconcile --full" in out["message"]
    # zero writes: the anchor stands and not one audit row was added
    assert D(lg.meta_get("live_genesis_balance")) == D("30.16")  # untouched
    assert len(lg.audit_events(limit=1000)) == audit_before
    assert lg.audit_events(event="deposit_recorded") == []


def test_a_withdrawal_mirrors_the_deposit(env):
    """Same verification, opposite sign, its own event name."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) - D("5.00"))))   # money leaves

    out = _deposit(lg, fake, s, "5.00", direction="withdrawal")

    assert out["ok"] and out["event"] == "withdrawal_recorded"
    assert out["implied"] == D("-5.0000") and out["stated"] == D("-5.0000")
    assert out["new_genesis_balance"] == D("25.1600")
    assert lg.meta_get("live_genesis_balance") == "25.1600"
    assert lg.meta_get("live_genesis_ts") == GENESIS.isoformat().replace("+00:00", "Z")
    assert len(lg.audit_events(event="withdrawal_recorded")) == 1
    assert lg.audit_events(event="deposit_recorded") == []
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["drift"] == D("0")


def test_a_withdrawal_stated_as_a_deposit_is_refused(env):
    """The sign is the command's, not the amount's: a debit claimed as a credit disagrees."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) - D("5.00"))))

    out = _deposit(lg, fake, s, "5.00")   # $5 left the account; a deposit is claimed

    assert not out["ok"] and out["reason"] == "amount_disagrees"
    assert out["disagreement"] == D("10.0000")
    assert D(lg.meta_get("live_genesis_balance")) == D("30.16")  # untouched


@pytest.mark.parametrize(
    ("off_by", "accepted"),
    [("0.0000", True), ("0.0100", True), ("-0.0100", True), ("0.0101", False),
     ("-0.0101", False)],
)
def test_the_tolerance_boundary_is_a_cent_inclusive(env, off_by, accepted):
    """A cent of disagreement is rounding; a hundredth over it is a question to answer.

    The boundary is inclusive on purpose: the walk and the exchange agree to the cent on
    every run this system has had, and refusing at *exactly* a cent would make the guard
    fire on noise it was never meant to catch.
    """
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) + D("20.00") + D(off_by))))

    out = _deposit(lg, fake, s, "20.00")

    assert out["ok"] is accepted
    assert out["tolerance"] == D("0.01")
    if accepted:
        # the anchor moves by the STATED amount, never by the implied one: the tolerance
        # forgives a cent of noise, it does not silently bank it
        assert lg.meta_get("live_genesis_balance") == "50.1600"
    else:
        assert D(lg.meta_get("live_genesis_balance")) == D("30.16")  # untouched


def test_a_deposit_is_refused_in_the_paper_era(env):
    """No live genesis means no anchor to move."""
    lg, fake, s = env

    out = _deposit(lg, fake, s, "20.00")

    assert not out["ok"] and out["reason"] == "paper_era"
    assert lg.meta_get("live_genesis_balance") is None


@pytest.mark.parametrize("amount", ["0", "-5.00"])
def test_a_non_positive_amount_is_refused(env, amount):
    """Direction is the command; the amount is always positive."""
    lg, fake, s = env
    _clean_world(lg, fake)

    out = _deposit(lg, fake, s, amount)

    assert not out["ok"] and out["reason"] == "non_positive_amount"
    assert D(lg.meta_get("live_genesis_balance")) == D("30.16")  # untouched
    assert lg.audit_events(event="deposit_recorded") == []


def test_an_unknown_direction_is_a_programming_error(env):
    lg, fake, s = env
    with pytest.raises(ValueError, match="direction must be"):
        _deposit(lg, fake, s, "20.00", direction="transfer")


def test_a_deposit_over_unexplained_drift_is_refused(env):
    """The case the guard exists for: real drift plus a real deposit.

    $20 goes in and $3 has gone missing for reasons nobody has explained yet. The implied
    transfer is $17, the stated one $20, and recording it would have moved the anchor by $20
    — leaving a ledger that reconciles perfectly and a $3 hole nobody would ever find.
    """
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) + D("20.00") - D("3.00"))))

    out = _deposit(lg, fake, s, "20.00")

    assert not out["ok"] and out["disagreement"] == D("3.0000")
    assert D(lg.meta_get("live_genesis_balance")) == D("30.16")  # untouched
    # Having written nothing, the reconciliation still sees the WHOLE $17 (the $20 that
    # arrived less the $3 that vanished) and halts on it. That is the outcome worth having:
    # recording the deposit would have left a ledger that balances and a $3 hole nobody
    # would ever look for again.
    after = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert after["drift"] == D("17.0000") and after["halted"]


# ------------------------------------------------- docs/16 §5: netting + scalar (the Aug-16 night)
def _aug16_pairs(lg, fake):
    """The three opposite-side pairs of 2026-08-15, at their real tickers and prices.

    KXTRUTHSOCIAL-B189 is the one with extra same-side legs: 1 NO against 3 YES, so ONE
    matched pair was pre-paid and a live 2-YES position remained. Each pair is placed
    through the fake (which debits stake + fee) and then netted through the fake (which
    credits the matched dollar), so the balance the walk is measured against is the
    exchange's own arithmetic rather than the test's.
    """
    pairs = {
        "KXTRUTHSOCIAL-B189": [("no", "0.72"), ("yes", "0.2717"), ("yes", "0.19"),
                               ("yes", "0.07")],
        "KXHIGHMIA-B92.5": [("no", "0.49"), ("yes", "0.47")],
        "KXTRUTHSOCIAL-B230": [("yes", "0.12"), ("no", "0.83")],
    }
    idx = 0
    legs: dict[str, list[str]] = {}
    for ticker, sides in pairs.items():
        legs[ticker] = []
        for side, price in sides:
            idx += 1
            # One bet per (attempt, ticker) is a ledger invariant, so same-ticker siblings
            # get their own attempts — which is how they arose live, too: sibling attempts
            # colliding on one market with no shared view of each other's positions.
            legs[ticker].append(
                _place_real(lg, fake, _attempt(lg), idx, ticker, side=side, price=price)
            )
        fake.net_matched_pair(ticker, 1, ts=GENESIS + timedelta(hours=4))
    return legs


def _aug16_scalar(lg, fake, *, ticker="KXNPBTOTAL-SHORTENED"):
    """A-0097-B01's world: 1 NO at $0.75 on a market that settled ``scalar`` at $0.82,
    with the settle pass not yet run — the row is still ``filled``."""
    aid = _attempt(lg)
    coid = _place_real(lg, fake, aid, 97, ticker, side="no", price="0.75")
    fake.resolve(ticker, "scalar", scalar_value=D("0.18"),
                 ts=GENESIS + timedelta(hours=5))
    return coid


def test_the_aug16_shape_is_the_walks_own_arithmetic_now(env):
    """The night the live system stopped, replayed. Three netted pairs the exchange had
    already paid $1.00 each for, plus a scalar settlement.

    The answer in August was the attribution gate: the $3.00 was money the walk could not
    name, so the night was DEFERRED rather than explained. Since docs/26 the walk adds the
    matched dollars up itself, so only the scalar is left to defer, and the three pairs are
    a line in the breakdown rather than a reason to wait.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    legs = _aug16_pairs(lg, fake)
    scalar_leg = _aug16_scalar(lg, fake)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.8200")     # the scalar's payout, and nothing else
    assert out["halted"] is False and not s.halt_path.exists()

    # the $3.00 is a walk term, readable: which pairs, which legs, how many contracts
    netting = out["detail"]["netting"]
    assert netting["n"] == 3 and netting["total"] == "3.0000"
    by_ticker = {p["ticker"]: p for p in netting["pairs"]}
    assert set(by_ticker) == set(legs)
    b189 = by_ticker["KXTRUTHSOCIAL-B189"]
    assert b189["matched_contracts"] == "1.0000" and b189["prepaid"] == "1.0000"
    assert b189["yes_contracts"] == "3.0000" and b189["no_contracts"] == "1.0000"
    assert sorted(b189["yes_bets"] + b189["no_bets"]) == sorted(legs[b189["ticker"]])

    # …and the scalar, which settle really has not written down yet, still defers
    prov = out["provisional"]
    assert prov["attributed"] == "0.8200" == prov["drift"]
    assert [p["bet_id"] for p in prov["pending"]] == [scalar_leg]
    assert prov["pending"][0]["implied_outcome"] == "scalar"
    assert prov["pending"][0]["implied_payout"] == "0.8200"
    assert len(lg.audit_events(event="reconcile_provisional")) == 1


def test_the_aug16_shape_minus_real_money_still_halts(env):
    """The gate stays TOTAL. Money the exchange did not in fact pay and the equality
    fails: no partial credit, no 'close enough'. Three dollars rather than one, so the
    residual also clears the halt bound and the night ends where it should."""
    lg, fake, s = env
    _go_live(lg, fake)
    _aug16_pairs(lg, fake)
    _aug16_scalar(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) - D("3.00"))))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("-2.1800")
    assert out.get("provisional") is None
    assert out["ok"] is False and out["halted"] is True
    assert s.halt_path.read_text().splitlines()[0] == "reconcile_drift"
    assert len(lg.audit_events(event="reconcile_drift")) == 1
    assert lg.audit_events(event="reconcile_provisional") == []


def test_a_netted_ticker_that_also_settled_is_not_counted_twice(env):
    """The double-count guard, now split between the two halves. The walk carries the
    matched dollar; the gate carries the exchange's own residual ``revenue`` and nothing
    else. All three Aug-15 markets settled at revenue 0 because the pair WAS the whole
    position, so the total is the matched dollar once, from the walk."""
    lg, fake, s = env
    _go_live(lg, fake)
    a1, a2 = _attempt(lg), _attempt(lg)
    _place_real(lg, fake, a1, 1, "KXPAIR", side="no", price="0.49")
    _place_real(lg, fake, a2, 2, "KXPAIR", side="yes", price="0.47")
    fake.net_matched_pair("KXPAIR", 1, ts=GENESIS + timedelta(hours=4))
    fake.resolve("KXPAIR", "yes", ts=GENESIS + timedelta(hours=5))
    record = fake.get_settlements()[0][0]
    assert record.revenue == D("0")        # the whole position was netted away

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.0000")              # the walk already has the dollar
    assert out["detail"]["netting"]["total"] == "1.0000"
    # the settlement itself is still unwritten, so the night defers on that and nothing more
    prov = out["provisional"]
    assert prov["attributed"] == "0.0000"           # NOT 1.0000, and never 2.0000
    assert prov["pending"] == []                    # netted legs never double-attributed
    assert prov["n_netted"] == 1
    pair = prov["netted"][0]
    assert pair["settled"] is True and pair["settlement_revenue"] == "0.0000"
    assert pair["prepaid"] == "1.0000"
    assert out["halted"] is False and not s.halt_path.exists()


def test_settling_the_netted_pairs_and_the_scalar_leaves_the_walk_clean(env):
    """Deferring is only right because the next tick closes it. Settle for real and walk
    again: every one of those dollars lands in the ledger and the drift is zero."""
    lg, fake, s = env
    _go_live(lg, fake)
    _aug16_pairs(lg, fake)
    scalar_leg = _aug16_scalar(lg, fake)
    for ticker in ("KXTRUTHSOCIAL-B189", "KXHIGHMIA-B92.5", "KXTRUTHSOCIAL-B230"):
        fake.resolve(ticker, "yes", ts=GENESIS + timedelta(hours=6))
    assert reconcile.reconcile_once(lg, fake, s, now=NOW).get("provisional")

    settle.settle_once(lg, fake, s)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["ok"] is True and out["drift"] == D("0.0000")
    assert out.get("provisional") is None
    assert not s.halt_path.exists()
    assert lg.latest_reconciliation()["ok"] == 1
    scalar = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (scalar_leg,)).fetchone()
    assert scalar["outcome"] == "scalar" and scalar["pnl"] == "0.0568"


def test_the_live_shape_with_the_scalar_still_booked_void_is_absorbed_then_reversed(env):
    """Why the one-row correction (D4) exists, pinned.

    The live ledger holds A-0097-B01 as a VOID (stake refunded, fee zeroed), so the walk
    nets it to zero while the exchange is +$0.0568 up on it. A terminal row is not a
    settle-pending position, so nothing attributes that residual. Since 2026-09-27 five
    cents is absorbed: carried into the walk, not announced, not halted. The correction
    then explains the same five cents, the next night reads exactly the opposite, and
    that night is recorded as the reversal of the first: the term drops back to zero and
    the watch counts neither night.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    coid = _aug16_scalar(lg, fake)
    # what the pre-docs/16 settle pass wrote for it
    lg.update_bet(coid, status="voided", outcome="void", pnl=D("0"), fee=D("0"),
                  settled_at=(GENESIS + timedelta(hours=5)).isoformat().replace(
                      "+00:00", "Z"))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.0568")
    assert out.get("provisional") is None
    assert out["verdict"] == "absorbed" and out["halted"] is False
    assert not s.halt_path.exists()

    # the correction, with no other change to the world
    result = settle.correct_scalar_settlement(
        lg, bet_id=coid, revenue=D("0.82"), fee=D("0.0132")
    )
    assert result["ok"] is True
    after = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=1))
    assert after["drift"] == D("-0.0568") and after["verdict"] == "reversed"
    assert after["detail"]["reversed_run_at"] == "2026-07-29T23:05:00Z"
    assert after["detail"]["reversed_drift"] == "0.0568"
    ev = lg.audit_events(event="reconcile_absorption_reversed")
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["reversed_drift"] == "0.0568"
    assert after["residual"]["window_nights"] == 0
    assert not s.halt_path.exists()
    final = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=2))
    assert final["verdict"] == "exact"
    assert final["detail"]["absorbed_residual"] == {"n": 0, "total": "0.0000", "runs": []}


# ----------------------------------------------------- docs/16 §5: the drift HALT is announced
def test_a_drift_halt_audits_halt_set_and_posts_exactly_one_banner(env, notify_calls):
    """docs/12 §8.1 is the standing proof that a condition written only to a file reaches
    nobody: this HALT stopped live trading at 03:01Z on 2026-08-16 and was found by a human
    happening to look. Every other HALT in the system audits ``halt_set``; this one did
    not, so 'why is the system stopped' was unanswerable from SQL for the money reason."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) + D("2.50"))))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["halted"] is True and s.halt_path.exists()
    halts = lg.audit_events(event="halt_set")
    assert len(halts) == 1
    detail = json.loads(halts[0]["detail"])
    assert detail["reason"] == "reconcile_drift" and detail["drift"] == "2.5000"
    assert detail["report"].endswith(".md")
    assert len(notify_calls) == 1
    assert "HALT (reconcile drift)" in notify_calls[0]
    assert "2.5000" in notify_calls[0]
    alerts = lg.audit_events(event="alert_raised")
    assert len(alerts) == 1
    assert json.loads(alerts[0]["detail"])["key"] == "reconcile_drift"


def test_a_broken_notifier_never_unmakes_the_halt(env, monkeypatch):
    """The HALT is the protection; announcing it is extra. A notifier that raises must not
    be able to undo the file, the report, or the audit trail (docs/14 D1)."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) + D("2.50"))))

    def _boom(*a, **kw):
        raise RuntimeError("notification subsystem is on fire")

    monkeypatch.setattr("betting_agent.harness.notify.raise_alert", _boom)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["halted"] is True
    assert s.halt_path.read_text().splitlines()[0] == "reconcile_drift"
    assert out["report"] is not None and out["report"].exists()
    assert len(lg.audit_events(event="reconcile_drift")) == 1
    assert len(lg.audit_events(event="halt_set")) == 1


def test_a_clean_run_announces_nothing(env, notify_calls):
    lg, fake, s = env
    _clean_world(lg, fake)

    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["ok"] is True

    assert notify_calls == []
    assert lg.audit_events(event="halt_set") == []
    assert lg.audit_events(event="alert_raised") == []


# ------------------------------------- docs/22 section 7.3: outside orders
PERSONAL = "KXBIKECYCLE"


def _personal(lg, fake, s, *, ticker=PERSONAL, side="yes", price="0.98", contracts=1,
              with_fill=False, ts=None, needs_market=True):
    """An outside order on the account, then the settle pass that records it.

    Modelled on the 2026-08-30 order that produced the $0.9991 gap the live system halted
    on: no ``client_order_id`` of ours, post-genesis, real money out of the account.
    ``with_fill`` hangs the matching fill on it (money already moved) so the market can
    later settle the position, which is what the exchange does and what the fake needs in
    order to publish a settlement record.
    """
    when = ts or GENESIS + timedelta(hours=2)
    if needs_market:
        fake.add_market(ticker, title=ticker, close_time=CLOSE)
    fake.add_personal_order(ticker, side=side, count=contracts, price=D(price), ts=when)
    if with_fill:
        fake.add_personal_fill(ticker, side, contracts, D(price), ts=when,
                               move_balance=False)
    settle.settle_once(lg, fake, s, now=NOW)
    return ticker


def _cost_and_fee(price="0.98", contracts=1):
    return q4(D(price) * contracts + calc_fee(contracts, D(price), COEF))


def test_a_personal_order_in_the_walk_reconciles_to_the_cent(env):
    """The carve-out, end to end: the outside order debits the account, settle writes it
    down, and the walk explains it. Before this the same order was unexplained drift."""
    lg, fake, s = env
    _go_live(lg, fake)
    _personal(lg, fake, s)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.0000")
    assert out["verdict"] == "exact"
    per = out["detail"]["personal"]
    assert per["n"] == 1 and per["n_on_harness_ticker"] == 0
    assert D(per["cost"]) == D("0.9800")
    assert D(per["fee"]) == calc_fee(1, D("0.98"), COEF)
    assert D(per["payout"]) == D("0.0000")          # still open
    assert per["orders"][0]["fee_source"] == "exchange"
    # …and the walk agrees with the exchange's own money trail, not just with itself: the
    # personal term equals, to the cent, what the fake actually took out of the account.
    moved = [e for e in fake.balance_ledger() if e["reason"] == "personal_order"]
    assert len(moved) == 1
    assert q4(-moved[0]["delta"]) == q4(D(per["cost"]) + D(per["fee"]))
    assert out["expected"] == out["actual"] == q4(fake.balance)


def test_the_same_order_without_its_row_is_unexplained_drift(env):
    """The control: delete the carve-out row and the drift comes straight back."""
    lg, fake, s = env
    _go_live(lg, fake)
    _personal(lg, fake, s)
    lg.conn.execute("DELETE FROM personal_orders")
    lg.conn.commit()

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == q4(-_cost_and_fee())
    assert out["verdict"] == "noted"                 # under $1: announced, not halted


def test_a_personal_order_pays_out_and_its_settlement_is_covered(env):
    """When the market settles, settle stamps the payout and the walk credits it. The
    settlement itself must also be accounted for: an owner's ticker is covered, not an
    orphan."""
    lg, fake, s = env
    _go_live(lg, fake)
    _personal(lg, fake, s, with_fill=True)
    fake.resolve(PERSONAL, "yes", ts=GENESIS + timedelta(hours=6))
    counts = settle.settle_once(lg, fake, s, now=NOW)

    assert counts["personal_settled"] == 1
    row = lg.personal_orders()[0]
    assert row["payout"] == "1.0000" and row["settled_at"] is not None

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"
    assert D(out["detail"]["personal"]["payout"]) == D("1.0000")
    assert out["checks"]["settlements_covered"]["ok"] is True


def test_a_losing_personal_order_pays_nothing(env):
    lg, fake, s = env
    _go_live(lg, fake)
    _personal(lg, fake, s, side="no", with_fill=True)
    fake.resolve(PERSONAL, "yes", ts=GENESIS + timedelta(hours=6))
    settle.settle_once(lg, fake, s, now=NOW)

    assert lg.personal_orders()[0]["payout"] == "0.0000"
    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"


def test_a_voided_personal_order_is_refunded_cost_and_fee(env):
    lg, fake, s = env
    _go_live(lg, fake)
    _personal(lg, fake, s, with_fill=True)
    fake.resolve(PERSONAL, "", ts=GENESIS + timedelta(hours=6))
    settle.settle_once(lg, fake, s, now=NOW)

    assert D(lg.personal_orders()[0]["payout"]) == _cost_and_fee()
    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"


def test_a_personal_order_on_a_ticker_we_trade_is_excluded_and_named(env):
    """Position netting pays a matched pair against the account at the later fill, so the
    owner's payout on a ticker we also hold is inseparable from ours. It is reported, not
    guessed at, and the drift it leaves is exactly what the verdict bands are for."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    _place_real(lg, fake, aid, 1, "KXSHARED", price="0.40")
    _personal(lg, fake, s, ticker="KXSHARED", side="no", price="0.55",
              needs_market=False)      # _place_real already put it on the exchange

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    per = out["detail"]["personal"]
    assert per["n"] == 0 and per["n_on_harness_ticker"] == 1
    assert per["on_harness_ticker"][0]["ticker"] == "KXSHARED"
    assert D(per["cost"]) == D("0.0000")                 # not in the arithmetic
    assert out["drift"] == q4(-_cost_and_fee(price="0.55"))
    assert out["verdict"] == "noted"


def test_an_order_the_exchange_does_not_price_falls_back_to_the_fee_model(env):
    lg, fake, s = env
    _go_live(lg, fake)
    fake.add_personal_order(PERSONAL, side="yes", count=1, price=D("0.98"),
                            ts=GENESIS + timedelta(hours=2), report_costs=False)
    settle.settle_once(lg, fake, s, now=NOW)

    row = lg.personal_orders()[0]
    assert row["fee_source"] == "computed"
    assert D(row["fee"]) == calc_fee(1, D("0.98"), COEF)
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["drift"] == D("0.0000")


def test_a_pre_genesis_personal_order_is_not_in_the_walk(env):
    """A row from before the live era stays in the table and out of the arithmetic, the
    same rule every other term in this walk follows. Written straight through the DAO: the
    scan's own window starts at genesis, so only a hand-recorded row can be older."""
    lg, fake, s = env
    _go_live(lg, fake)
    lg.upsert_personal_order(
        "OID-OLD", ticker=PERSONAL, side="yes",
        created_time=(GENESIS - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        contracts=D("1"), cost=D("0.98"), fee=D("0.0014"), fee_source="exchange",
        first_seen_at=str(NOW),
    )

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert lg.personal_orders() != []              # recorded…
    assert out["detail"]["personal"]["n"] == 0     # …and outside the window
    assert out["drift"] == D("0.0000")


def test_the_deposit_is_accepted_once_the_personal_order_is_in_the_walk(env):
    """docs/22 section 7.7 step 2, which is the reason the carve-out exists. The deposit
    command refuses when the implied transfer and the stated one disagree by more than a
    cent, and the owner's unexplained order was exactly such a disagreement."""
    lg, fake, s = env
    _go_live(lg, fake)
    _personal(lg, fake, s)
    fake.set_balance(q4(fake.balance + D("100.00")))

    out = reconcile.record_balance_adjustment(
        lg, fake, s, direction="deposit", amount=D("100.00"), now=NOW
    )

    assert out["ok"] is True
    assert lg.meta_get("live_genesis_balance") == str(q4(D("30.16") + D("100.00")))
    detail = json.loads(lg.audit_events(event="deposit_recorded")[0]["detail"])
    assert detail["flows"]["personal_n"] == 1
    assert D(detail["flows"]["personal_cost_and_fees"]) == _cost_and_fee()
    assert "personal orders" in detail["derivation"]
    # and the night after the deposit reconciles exactly, which is the acceptance test
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["verdict"] == "exact"


def test_the_deposit_still_refuses_when_the_order_is_not_carved_out(env):
    """The guard the carve-out must not weaken: an amount the walk cannot explain is
    refused, and nothing is written."""
    lg, fake, s = env
    _go_live(lg, fake)
    _personal(lg, fake, s)
    lg.conn.execute("DELETE FROM personal_orders")
    lg.conn.commit()
    fake.set_balance(q4(fake.balance + D("100.00")))

    out = reconcile.record_balance_adjustment(
        lg, fake, s, direction="deposit", amount=D("100.00"), now=NOW
    )

    assert out["ok"] is False and out["reason"] == "amount_disagrees"
    assert lg.meta_get("live_genesis_balance") == "30.16"
    assert lg.audit_events(event="deposit_recorded") == []


# ------------------------------------- 2026-09-27: absorbed, noted and large
def _no_absorption(s):
    """Turn absorption off, for a test about a walk term rather than about the bands.

    Such a test reconciles once to show a drift, fixes the term, and reconciles again to
    show the walk exact. With absorption on, the first run would carry the drift into the
    walk and the fix would then read as the same drift reversed, which is right in
    production and beside the point of the test. At zero, every nonzero drift is noted.
    """
    s.reconcile.absorb_usd = D("0")


def _drift_by(lg, fake, s, drift):
    """Set the exchange balance so the walk, absorbed residual included, is off by
    exactly ``drift`` tonight."""
    fake.set_balance(q4(reconcile.expected_balance(lg, s)["expected"] + D(drift)))


def _alert_keys(lg):
    return [json.loads(e["detail"])["key"] for e in lg.audit_events(event="alert_raised")]


def test_a_noted_drift_records_and_alerts_without_halting(env, notify_calls):
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(fake.balance + D("0.75"))      # past $0.50, inside $1.00

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["verdict"] == "noted"
    assert out["ok"] is False and out["halted"] is False
    assert not s.halt_path.exists()
    assert out["report"] is None
    row = lg.latest_reconciliation()
    assert row["ok"] == 0 and row["drift"] == "0.7500"
    assert json.loads(row["detail"])["verdict"] == "noted"
    ev = lg.audit_events(event="reconcile_drift")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["verdict"] == "noted"
    assert detail["drift"] == "0.7500" and detail["expected"] == str(out["expected"])
    assert detail["actual"] == str(out["actual"])
    assert detail["walk"]["debits"]["n"] == 3        # the arithmetic rides along
    assert _alert_keys(lg) == ["reconcile_drift_noted"]
    assert len(notify_calls) == 1
    assert "still running" in notify_calls[0]
    assert lg.audit_events(event="halt_set") == []
    # nothing absorbed, so the same drift is noted again tomorrow
    again = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))
    assert again["verdict"] == "noted" and again["drift"] == D("0.7500")
    assert again["detail"]["absorbed_residual"]["n"] == 0


def test_an_absorbed_drift_is_silent_and_the_next_night_is_exact(env, notify_calls):
    """The cent of 2026-09-21 to 2026-09-25. Under the repeated-drift rule it halted the
    system on its third night; absorbed, it is one quiet row, and the walk carries it."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(fake.balance + D("0.0101"))

    first = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert first["verdict"] == "absorbed"
    assert first["ok"] is False and first["halted"] is False
    assert first["report"] is None and not s.halt_path.exists()
    assert json.loads(lg.latest_reconciliation()["detail"])["verdict"] == "absorbed"
    ev = lg.audit_events(event="reconcile_absorbed")
    assert len(ev) == 1 and json.loads(ev[0]["detail"])["drift"] == "0.0101"
    assert lg.audit_events(event="reconcile_drift") == []
    assert lg.audit_events(event="alert_raised") == [] and notify_calls == []
    assert first["detail"]["absorbed_residual"] == {"n": 0, "total": "0.0000", "runs": []}

    # Nothing else moves: the next night, and a second look the same night, are exact.
    for later in (NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1)):
        out = reconcile.reconcile_once(lg, fake, s, now=later)
        assert out["verdict"] == "exact" and out["ok"] is True
        assert out["drift"] == D("0.0000")
        assert out["expected"] == out["actual"] == q4(fake.balance)
        term = out["detail"]["absorbed_residual"]
        assert term["n"] == 1 and term["total"] == "0.0101"
        assert term["runs"] == [{"run_at": "2026-07-29T23:05:00Z", "drift": "0.0101"}]
    assert not s.halt_path.exists()


def test_a_deposit_with_no_absorbed_residual_passes_as_before(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(fake.balance + D("20.00"))

    out = _deposit(lg, fake, s, "20.00")

    assert out["ok"] is True and out["implied"] == D("20.0000")
    assert out["absorbed_residual"] == D("0.0000")
    assert "absorbed residual in the walk: 0.0000 over 0 night(s)" in out["message"]
    assert "absorbed residual" not in out["decisions_line"]


def _absorb_the_rebate(lg, fake, s):
    """The cent of 2026-09-22 to 2026-09-25, absorbed: an exchange rebate nobody had
    recorded yet, standing in the walk as +0.0103."""
    _clean_world(lg, fake)
    fake.set_balance(fake.balance + D("0.0103"))
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["verdict"] == "absorbed"


def test_a_deposit_is_never_verified_against_an_absorbed_amount(env):
    """An absorbed drift is carried on trust, and a deposit is where an unexplained amount
    would become permanent. With +0.0103 absorbed and a stated amount that is otherwise
    exact, the deposit is refused, and the refusal names the absorbed residual and the
    way out."""
    lg, fake, s = env
    _absorb_the_rebate(lg, fake, s)
    fake.set_balance(fake.balance + D("20.00"))

    out = _deposit(lg, fake, s, "20.00", now=NOW + timedelta(hours=1))

    assert out["ok"] is False and out["reason"] == "absorbed_residual"
    assert out["implied"] == D("20.0103") and out["disagreement"] == D("0.0103")
    assert "absorbed residual of 0.0103 over 1 night(s)" in out["message"]
    assert "betting-agent credit" in out["message"]
    assert "NOTHING was written" in out["message"]
    assert "absorbed residual in the walk: 0.0103 over 1 night(s)" in out["message"]
    assert lg.meta_get("live_genesis_balance") == "30.16"
    assert lg.audit_events(event="deposit_recorded") == []


def test_a_deposit_passes_once_the_absorbed_item_is_recorded_as_a_credit(env):
    """Recording the rebate is the fix: the deposit then implies exactly what arrived,
    and the next night reverses the absorption, so the walk is exact with a zero term."""
    lg, fake, s = env
    _absorb_the_rebate(lg, fake, s)
    fake.set_balance(fake.balance + D("20.00"))
    lg.insert_credit(credited_at=(GENESIS + timedelta(hours=6)).isoformat().replace(
        "+00:00", "Z"), amount=D("0.0103"), kind="rebate",
        recorded_at="2026-09-27T18:00:00Z")

    out = _deposit(lg, fake, s, "20.00", now=NOW + timedelta(hours=1))

    assert out["ok"] is True and out["implied"] == D("20.0000")
    assert "absorbed residual 0.0103 over 1 night(s) not counted" in out["decisions_line"]
    night = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))
    assert night["verdict"] == "reversed" and night["drift"] == D("-0.0103")
    after = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=2))
    assert after["verdict"] == "exact"
    assert after["detail"]["absorbed_residual"]["n"] == 0


# The live nights under the docs/22 regime, 2026-09-17 to 2026-09-27, as the walk read
# them. The two six-dollar nights are the netting halts of 2026-09-20 and 2026-09-21.
LIVE_DRIFTS = ("-0.0003", "-0.0017", "5.9968", "6.0068", "0.0101", "0.0101",
               "0.0103", "0.0103", "0.0103", "0.0103", "0.0000", "-0.0002")


def test_the_live_history_halts_only_on_the_two_six_dollar_nights(env, notify_calls):
    """The decision's own acceptance test. Each night the walk, absorbed residual
    included, is off by that night's live drift. Under the three-way verdict and the
    repeated-drift rule, five of these nights halted; under the bands only the two
    six-dollar nights do, and the residual watch stays quiet."""
    lg, fake, s = env
    _clean_world(lg, fake)

    outs = []
    for night, drift in enumerate(LIVE_DRIFTS):
        _drift_by(lg, fake, s, drift)
        outs.append(reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=night)))

    assert [o["drift"] for o in outs] == [D(d) for d in LIVE_DRIFTS]
    assert [o["verdict"] for o in outs] == [
        "absorbed", "absorbed", "large", "large", "absorbed", "absorbed", "absorbed",
        "absorbed", "absorbed", "absorbed", "exact", "absorbed",
    ]
    assert [n for n, o in enumerate(outs) if o["halted"]] == [2, 3]
    assert _alert_keys(lg) == ["reconcile_drift", "reconcile_drift"]
    # the walk now carries every absorbed night, and nothing else
    absorbed = [D(d) for d, o in zip(LIVE_DRIFTS, outs, strict=True)
                if o["verdict"] == "absorbed"]
    walk = reconcile.expected_balance(lg, s)["breakdown"]["absorbed_residual"]
    assert walk["n"] == 9 and D(walk["total"]) == sum(absorbed) == D("0.0592")
    last = outs[-1]["residual"]
    assert last["over"] is False
    assert last["nights"] == last["window_nights"] == 9
    assert last["window_abs"] == D("0.0636")


def test_after_an_absorption_the_walk_is_exact_while_nothing_moves(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    _drift_by(lg, fake, s, "-0.0017")
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["verdict"] == "absorbed"

    outs = [reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=n))
            for n in (1, 2, 3)]

    assert [o["verdict"] for o in outs] == ["exact"] * 3
    assert all(o["detail"]["absorbed_residual"]["total"] == "-0.0017" for o in outs)


@pytest.mark.parametrize(
    ("drift", "verdict"),
    [
        ("0.50", "absorbed"), ("-0.50", "absorbed"),
        ("0.5001", "noted"), ("-0.5001", "noted"),
        ("1.00", "noted"), ("-1.00", "noted"),
        ("1.0001", "large"), ("-1.0001", "large"),
    ],
)
def test_the_band_edges(env, drift, verdict):
    lg, fake, s = env
    _clean_world(lg, fake)
    _drift_by(lg, fake, s, drift)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D(drift)
    assert out["verdict"] == verdict
    assert out["halted"] is (verdict == "large")
    assert s.halt_path.exists() is (verdict == "large")
    expected_keys = {"absorbed": [], "noted": ["reconcile_drift_noted"],
                     "large": ["reconcile_drift"]}[verdict]
    assert _alert_keys(lg) == expected_keys


@pytest.mark.parametrize("drift", ["0", "0.0001", "0.0101", "0.75"])
def test_a_failed_check_is_large_at_any_drift(env, drift):
    """The checks are the part of the reconciliation that is not a model. A settlement
    for a market we never held is not rounding at any size of drift."""
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.add_settlement("KXORPHAN", "yes", ts=GENESIS + timedelta(hours=6))
    _drift_by(lg, fake, s, drift)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["failed_checks"] == ["settlements_covered"]
    assert out["verdict"] == "large" and out["halted"] is True
    assert s.halt_path.exists()
    # and nothing was absorbed: a failed night is never carried into the walk
    assert reconcile.expected_balance(lg, s)["breakdown"]["absorbed_residual"]["n"] == 0


def test_a_drift_past_the_bound_halts(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    fake.set_balance(fake.balance + D("1.01"))      # one cent past it

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["verdict"] == "large" and out["halted"] is True
    assert s.halt_path.exists() and out["report"] is not None
    assert json.loads(lg.audit_events(event="reconcile_drift")[0]["detail"])["verdict"] \
        == "large"


def test_the_bands_are_configurable(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    s.reconcile.absorb_usd = D("0.10")
    s.reconcile.halt_drift_usd = D("0.50")
    _drift_by(lg, fake, s, "0.20")
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["verdict"] == "noted"
    _drift_by(lg, fake, s, "0.60")
    assert reconcile.reconcile_once(
        lg, fake, s, now=NOW + timedelta(days=1))["verdict"] == "large"


def test_the_residual_watch_alerts_once_on_the_sum(env, notify_calls):
    """Many absorbed drifts adding up is what a slow leak looks like. Six nights of
    $0.45 is $2.70 over thirty days, past $2.50: one alert, no halt, and silence on the
    nights after it while the sum stays over."""
    lg, fake, s = env
    _clean_world(lg, fake)

    outs = []
    for night in range(7):
        _drift_by(lg, fake, s, "0.45")
        outs.append(reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=night)))

    assert [o["verdict"] for o in outs] == ["absorbed"] * 7
    assert [o["residual"]["over"] for o in outs] == [False] * 5 + [True] * 2
    assert outs[5]["residual"]["window_abs"] == D("2.7000")
    assert _alert_keys(lg) == ["reconcile_residual"]          # once, not twice
    assert len(notify_calls) == 1 and "still running" in notify_calls[0]
    assert not s.halt_path.exists()


def test_the_residual_watch_alerts_on_the_count_of_nights(env):
    """Eleven absorbed nights inside thirty days is past ten, however small each was."""
    lg, fake, s = env
    _clean_world(lg, fake)

    outs = []
    for night in range(11):
        _drift_by(lg, fake, s, "0.0001")
        outs.append(reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=night)))

    assert [o["residual"]["over"] for o in outs] == [False] * 10 + [True]
    assert outs[-1]["residual"]["window_nights"] == 11
    assert outs[-1]["residual"]["total"] == D("0.0011")        # a hundredth of a dollar…
    assert _alert_keys(lg) == ["reconcile_residual"]           # …and still counted
    assert not s.halt_path.exists()


def test_an_opposite_drift_that_matches_no_single_run_absorbs_as_usual(env):
    """A reversal is one fix explaining one night. A drift the other way that is not the
    exact negative of any single run is a new absorption, and both nights count."""
    lg, fake, s = env
    _clean_world(lg, fake)
    _drift_by(lg, fake, s, "0.0101")
    reconcile.reconcile_once(lg, fake, s, now=NOW)
    _drift_by(lg, fake, s, "0.0002")
    reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=1))
    _drift_by(lg, fake, s, "-0.0103")        # the sum of both runs, but neither one

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=2))

    assert out["verdict"] == "absorbed" and "reversed_run_at" not in out["detail"]
    assert lg.audit_events(event="reconcile_absorption_reversed") == []
    term = reconcile.expected_balance(lg, s)["breakdown"]["absorbed_residual"]
    assert term["n"] == 3 and term["total"] == "0.0000"
    assert out["residual"]["window_nights"] == 3
    assert out["residual"]["window_abs"] == D("0.0206")


def test_a_reversal_takes_one_run_and_leaves_the_rest_in_force(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    for night, drift in enumerate(("0.0101", "0.0002", "-0.0101")):
        _drift_by(lg, fake, s, drift)
        out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=night))

    assert out["verdict"] == "reversed"
    assert out["detail"]["reversed_run_at"] == "2026-07-29T23:05:00Z"
    term = reconcile.expected_balance(lg, s)["breakdown"]["absorbed_residual"]
    assert term == {"n": 1, "total": "0.0002",
                    "runs": [{"run_at": "2026-07-30T23:05:00Z", "drift": "0.0002"}]}
    assert out["residual"]["window_nights"] == 1
    later = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=3))
    assert later["verdict"] == "exact"


def test_the_residual_watch_forgets_nights_older_than_its_window(env):
    """Absorbed nights past the window still sit in the walk, and stop counting against
    the watch; the first night back under re-arms the alert."""
    lg, fake, s = env
    _clean_world(lg, fake)
    s.reconcile.residual_alert_nights = 2
    for night in range(3):
        _drift_by(lg, fake, s, "0.01")
        reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=night))
    assert _alert_keys(lg) == ["reconcile_residual"]

    later = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=40))

    assert later["verdict"] == "exact"
    assert later["residual"]["over"] is False and later["residual"]["window_nights"] == 0
    assert later["residual"]["nights"] == 3 and later["residual"]["total"] == D("0.0300")
    # re-armed: the next time it goes over, it alerts again
    for night in range(41, 44):
        _drift_by(lg, fake, s, "0.01")
        reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(days=night))
    assert _alert_keys(lg) == ["reconcile_residual", "reconcile_residual"]


def test_a_row_from_before_genesis_is_never_absorbed_into_this_era(env):
    lg, fake, s = env
    _clean_world(lg, fake)
    lg.insert_reconciliation(
        (GENESIS - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        expected_balance=D("30.0000"), actual_balance=D("30.0100"), drift=D("0.0100"),
        ok=False, detail=json.dumps({"verdict": "absorbed"}),
    )

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["verdict"] == "exact"
    assert out["detail"]["absorbed_residual"]["n"] == 0


def test_a_pre_genesis_order_that_settles_after_it_credits_the_walk(env):
    """The canary's rule, applied here: an order placed before the live era spent money
    this walk does not own, but a settlement inside the era is a credit it must see."""
    lg, fake, s = env
    _go_live(lg, fake)
    lg.upsert_personal_order(
        "OID-OLD", ticker=PERSONAL, side="yes",
        created_time=(GENESIS - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        contracts=D("1"), cost=D("0.98"), fee=D("0.0014"), fee_source="exchange",
        first_seen_at=str(NOW),
    )
    lg.settle_personal_order(
        "OID-OLD",
        settled_at=(GENESIS + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
        payout=D("1.00"),
    )
    fake.set_balance(q4(fake.balance + D("1.00")))      # the exchange paid it out

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    per = out["detail"]["personal"]
    assert per["n"] == 1
    assert D(per["cost"]) == D("0.0000")            # the spend is pre-era…
    assert D(per["payout"]) == D("1.0000")          # …the credit is not
    assert per["orders"][0]["cost_included"] is False
    assert per["orders"][0]["payout_included"] is True
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"


# --------------------- docs/24: the canary counted twice, and the deposit it refused
def test_a_stale_canary_personal_row_double_counts_until_the_scan_clears_it(env):
    """The go-live reproduction, end to end.

    The canary is placed with a client order id that deliberately does not match ours, so
    the shared-account scan once read it as an outside order and wrote a
    ``personal_orders`` row for it. The walk then subtracted the same cost twice, once in
    its canary term and once in its personal term. On 2026-09-16 that was $0.0661, and it
    is what refused ``betting-agent deposit --amount 100.00``: implied $100.0661 against a
    stated $100.0000, a disagreement larger than the cent of tolerance, so the command
    wrote nothing and said so.

    Nothing about the walk changes here. The scan stops writing the row, and clears the one
    it already wrote, which is all the arithmetic needed.
    """
    lg, fake, s = env
    _no_absorption(s)
    _go_live(lg, fake)
    canary = _unsettled_canary(lg, fake, ticker="KXALIENS-27", price="0.06")
    canary_cost = q4(D("1") * D(canary.avg_fill_price) + D(canary.fee))
    _personal(lg, fake, s)                      # the owner's own cycling order
    assert [r["ticker"] for r in lg.personal_orders()] == [PERSONAL]

    # the row the scan left behind before docs/24, written the way it wrote it
    lg.upsert_personal_order(
        str(canary.order_id), ticker="KXALIENS-27", side="yes",
        created_time=(GENESIS + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        contracts=D("1"), cost=q4(D(canary.avg_fill_price)), fee=q4(D(canary.fee)),
        fee_source="exchange", first_seen_at=str(NOW),
    )

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    # The walk is short by exactly the canary, because it paid for it twice.
    assert out["drift"] == canary_cost
    assert out["verdict"] == "noted" and not s.halt_path.exists()
    assert out["detail"]["personal"]["n"] == 2

    # `betting-agent deposit` refuses on the difference, and writes nothing.
    fake.set_balance(q4(fake.balance + D("100.00")))
    refused = reconcile.record_balance_adjustment(
        lg, fake, s, direction="deposit", amount=D("100.00"), now=NOW
    )
    assert refused["ok"] is False and refused["reason"] == "amount_disagrees"
    assert refused["implied"] == q4(D("100.00") + canary_cost)
    assert lg.audit_events(event="deposit_recorded") == []

    # The scan recognizes its own canary and takes the row back out.
    settle.settle_once(lg, fake, s, now=NOW, full_scan=True)
    assert [r["ticker"] for r in lg.personal_orders()] == [PERSONAL]
    assert len(lg.audit_events(event="personal_order_withdrawn")) == 1

    # The walk is now short by the deposit and by nothing else, which is what the deposit
    # command exists to record.
    after = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=1))
    assert after["drift"] == D("100.0000")

    accepted = reconcile.record_balance_adjustment(
        lg, fake, s, direction="deposit", amount=D("100.00"),
        now=NOW + timedelta(minutes=2),
    )
    assert accepted["ok"] is True
    detail = json.loads(lg.audit_events(event="deposit_recorded")[0]["detail"])
    assert detail["implied"] == "100.0000" and detail["stated"] == "100.0000"
    assert detail["disagreement"] == "0.0000"
    assert lg.meta_get("live_genesis_balance") == str(q4(D("30.16") + D("100.00")))

    # And the first reconcile after the deposit comes back exact, which is the acceptance
    # test docs/22 section 7.7 step 4 sets for the whole carve-out.
    final = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=3))
    assert final["drift"] == D("0.0000") and final["verdict"] == "exact"
    assert final["expected"] == final["actual"] == q4(fake.balance)


def test_the_canary_term_still_carries_the_canary_on_its_own(env):
    """The carve-out takes the canary out of the PERSONAL term and leaves it in its own,
    which is the term that was always right."""
    lg, fake, s = env
    _go_live(lg, fake)
    _unsettled_canary(lg, fake, ticker="KXALIENS-27", price="0.06")

    settle.settle_once(lg, fake, s, now=NOW, full_scan=True)
    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert lg.personal_orders() == []
    assert out["detail"]["canary"]["cost_included"] is True
    assert out["detail"]["personal"]["n"] == 0
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"


# --------------------- docs/25: the three cent-and-a-bit fees of 2026-09-17
def test_the_multi_contract_fee_rounding_drifts_three_hundredths_until_settle_heals_it(env):
    """The first nightly walk under the new regime, reproduced.

    It came back with a drift of -0.0003: expected 159.0350 against an actual
    159.0347, every cross-check passing. The three legs that had filled that day carried
    fees of 0.0099, 0.0309 and 0.0099, where the exchange had charged 0.0100, 0.0310 and
    0.0100. The client was multiplying the exchange's per-contract fee average, which the
    exchange has already rounded, by the fill count; the exchange rounds once, on the leg
    total. At one contract a leg the two agree, which is why 352 earlier legs reconciled
    to the cent and sizing is what exposed it.

    Nothing about the walk changes. The scan reads the fee off the order the exchange
    reports and corrects the row. Since 2026-09-27 the first night's -0.0003 is absorbed,
    so the heal reads as the same three hundredths the other way. That night is recorded
    as the reversal of the first, and the night after it is exact with a zero term.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    first, second = _attempt(lg), _attempt(lg)
    legs = [
        _place_real(lg, fake, first, 1, "KXFEE-A", price="0.05", contracts=3,
                    stored_fee="0.0099"),
        _place_real(lg, fake, second, 1, "KXFEE-B", price="0.18", contracts=3,
                    stored_fee="0.0309"),
        _place_real(lg, fake, second, 2, "KXFEE-C", price="0.05", contracts=3,
                    stored_fee="0.0099"),
    ]
    settle.genesis_snapshot(lg, fake)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("-0.0003")
    assert out["verdict"] == "absorbed"
    assert out["failed_checks"] == []
    assert not s.halt_path.exists()

    settle.settle_once(lg, fake, s, now=NOW, full_scan=True)

    corrected = lg.audit_events(event="fee_corrected")
    assert len(corrected) == 3
    assert sorted(json.loads(e["detail"])["charged_fee"] for e in corrected) == [
        "0.0100", "0.0100", "0.0310",
    ]
    fees = [
        lg.conn.execute("SELECT fee FROM bets WHERE bet_id=?", (b,)).fetchone()["fee"]
        for b in legs
    ]
    assert sorted(fees) == ["0.0100", "0.0100", "0.0310"]

    after = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=1))
    assert after["drift"] == D("0.0003")
    assert after["verdict"] == "reversed"
    assert after["detail"]["reversed_run_at"] == "2026-07-29T23:05:00Z"
    final = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=2))
    assert final["verdict"] == "exact"
    assert final["detail"]["absorbed_residual"] == {"n": 0, "total": "0.0000", "runs": []}
    assert final["residual"]["window_nights"] == 0
    assert final["expected"] == final["actual"] == q4(fake.balance)


def test_a_one_contract_leg_never_needed_correcting(env):
    """The control, and the reason this went unseen for 352 legs."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    _place_real(lg, fake, aid, 1, "KXONE", price="0.05", contracts=1)
    settle.genesis_snapshot(lg, fake)

    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["verdict"] == "exact"
    settle.settle_once(lg, fake, s, now=NOW, full_scan=True)
    assert lg.audit_events(event="fee_corrected") == []


def test_settled_legs_heal_too_and_the_walk_stops_drifting(env):
    """The second half of the docs/25 defect, and the reason settled rows cannot be left
    alone. By 2026-09-19 three of the mis-feed legs had settled, the live walk was at
    -0.0017, and correcting only the open ones would have left a CONSTANT sub-cent drift
    once fills stopped adding to it. A constant drift is the one thing the repeated-drift
    rule was built to halt on: the same figure on the third night stopped the system.

    The three shapes here are the live ones: a settled leg 0.0001 short, another 0.0001
    short at an odd fill price, and one that was already right.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    legs = [
        # (ticket index, ticker, price, stored fee, outcome)
        (1, "KXHEAL-A", "0.05", "0.0099", "loss"),      # A-0241-B01
        (2, "KXHEAL-B", "0.2467", "0.0390", "loss"),    # A-0253-B01
        (3, "KXHEAL-C", "0.18", None, "win"),           # A-0253-B02, already right
    ]
    _no_absorption(s)
    for idx, ticker, price, stored_fee, outcome in legs:
        coid = _place_real(lg, fake, aid, idx, ticker, price=price, contracts=3,
                           stored_fee=stored_fee)
        fake.resolve(ticker, "yes" if outcome == "win" else "no",
                     ts=GENESIS + timedelta(hours=4))
        row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
        lg.update_bet(coid, status="settled", outcome=outcome,
                      pnl=bet_pnl(outcome, D("3"), D(price), D(row["fee"])),
                      settled_at=(GENESIS + timedelta(hours=4)).isoformat().replace(
                          "+00:00", "Z"))
    settle.genesis_snapshot(lg, fake)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("-0.0002")          # two legs, a hundredth of a cent each
    assert out["verdict"] == "noted" and out["failed_checks"] == []

    settle.settle_once(lg, fake, s, now=NOW, full_scan=True)

    corrected = lg.audit_events(event="fee_corrected")
    assert len(corrected) == 2                   # the third was already right
    assert all(json.loads(e["detail"])["status"] == "settled" for e in corrected)
    rows = {r["ticker"]: r for r in lg.conn.execute("SELECT * FROM bets")}
    assert rows["KXHEAL-A"]["fee"] == "0.0100" and rows["KXHEAL-A"]["pnl"] == "-0.1600"
    assert rows["KXHEAL-B"]["fee"] == "0.0391" and rows["KXHEAL-B"]["pnl"] == "-0.7792"
    # every settled leg's P/L is now what the formula gives for the fee that was charged
    for ticker, price, outcome in (("KXHEAL-A", "0.05", "loss"),
                                   ("KXHEAL-B", "0.2467", "loss"),
                                   ("KXHEAL-C", "0.18", "win")):
        r = rows[ticker]
        assert D(r["pnl"]) == bet_pnl(outcome, D("3"), D(price), D(r["fee"]))

    after = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=1))
    assert after["drift"] == D("0.0000")
    assert after["verdict"] == "exact"
    assert after["expected"] == after["actual"] == q4(fake.balance)

    # and a second pass corrects nothing, so the walk stays exact rather than oscillating
    settle.settle_once(lg, fake, s, now=NOW + timedelta(minutes=2), full_scan=True)
    assert len(lg.audit_events(event="fee_corrected")) == 2
    assert reconcile.reconcile_once(
        lg, fake, s, now=NOW + timedelta(minutes=3)
    )["verdict"] == "exact"


# ---------------- 2026-09-27: a leg that filled at several prices (A-0316-B01)
def test_a_leg_filled_at_several_prices_walks_exact_once_its_stake_is_what_was_charged(env):
    """The night of 2026-09-26, reproduced, and the fix.

    A-0316-B01 bought 3 YES contracts in four pieces between $0.11 and $0.20: $0.4043 for
    the contracts and $0.0243 in fees, $0.4286 off the balance. Placement booked the
    create response's truncated average, $0.1347, so a $0.4041 stake, and the fee model
    priced at that average, $0.0245. Both were $0.0002 off in opposite directions, so the
    walk was exact by accident. The scan then corrected the fee alone, and the nightly
    reconciliation came back at -$0.0002. Settlement later re-priced the leg at the
    rounded average, $0.1348, which as a stake would have left it $0.0001 over for good.
    """
    lg, fake, s = env
    _no_absorption(s)
    _go_live(lg, fake)
    aid = _attempt(lg)
    fake.add_market("KXLOWTDC", title="t", close_time=CLOSE, yes_ask=D("0.11"),
                    yes_ask_size=50)
    fake.set_order_behavior("KXLOWTDC", "levels:1.18@0.11,0.76@0.12,0.41@0.13,0.65@0.20")
    coid = f"{aid}-B01"
    r = fake.create_order("KXLOWTDC", "yes", D("0.45"), 3, coid)
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=1, ticker="KXLOWTDC", side="yes",
        limit_price=D("0.45"), model_prob=D("0.60"), rationale="r", is_real=1,
        status="filled", contracts=3, fill_price=r.avg_fill_price,
        stake=q4(3 * r.avg_fill_price), fee=D("0.0245"), order_id=r.order_id,
        client_order_id=coid,
        placed_at=(GENESIS + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    )
    settle.genesis_snapshot(lg, fake)

    # at placement the two errors cancel
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["drift"] == D("0.0000")

    # the fee alone corrected, as the scan did at 00:31Z: the night of 2026-09-26
    lg.update_bet(coid, fee=D("0.0243"))
    night = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=1))
    assert night["drift"] == D("-0.0002")
    assert night["verdict"] == "noted" and night["failed_checks"] == []

    # the scan corrects the stake too now, and the walk is exact again
    settle.settle_once(lg, fake, s, now=NOW + timedelta(minutes=2), full_scan=True)
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    assert (row["stake"], row["fee"]) == ("0.4043", "0.0243")
    assert reconcile.reconcile_once(
        lg, fake, s, now=NOW + timedelta(minutes=3)
    )["verdict"] == "exact"

    # settlement re-prices at the rounded average and still books the fills' own cost
    fake.resolve("KXLOWTDC", "yes", ts=NOW + timedelta(minutes=4))
    settle.settle_once(lg, fake, s, now=NOW + timedelta(minutes=5), full_scan=True)
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    assert (row["status"], row["fill_price"], row["stake"]) == ("settled", "0.1348", "0.4043")
    assert row["pnl"] == "2.5714"                        # 3 - 0.4043 - 0.0243
    after = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=6))
    assert after["drift"] == D("0.0000") and after["verdict"] == "exact"
    assert after["expected"] == after["actual"] == q4(fake.balance)


def test_the_walk_reads_the_stored_stake_before_the_price():
    """The stake column holds what the exchange charged; the product is only a fallback
    for a row that has none."""
    assert reconcile._stake_of(
        {"stake": "0.4043", "contracts": 3, "fill_price": "0.1348"}) == D("0.4043")
    assert reconcile._stake_of(
        {"stake": None, "contracts": 3, "fill_price": "0.1348"}) == D("0.4044")
    assert reconcile._stake_of(
        {"stake": None, "contracts": None, "fill_price": None}) == D("0")


# ------------------- docs/26: the two markets held on both sides, 2026-09-20
def _both_sides(lg, fake, ticker, legs, *, matched):
    """One market several attempts took positions on, opposite sides, three contracts a
    leg: the 2026-09-20 shape. The pair is netted through the fake, so the balance the
    walk is measured against is the exchange's own arithmetic."""
    placed = []
    for idx, (side, price) in enumerate(legs, start=1):
        # One bet per (attempt, ticker) is a ledger invariant, and live these arose from
        # sibling attempts with no shared view of each other's positions.
        placed.append(_place_real(lg, fake, _attempt(lg), idx, ticker, side=side,
                                  price=price, contracts=3))
    fake.net_matched_pair(ticker, matched, ts=GENESIS + timedelta(hours=4))
    return placed


def test_both_sides_of_a_market_are_six_dollars_the_walk_used_to_miss(env):
    """The live halt of 2026-09-20T03:14Z, replayed: +5.9968 with every cross-check
    passing, and again the next night at +6.0068.

    Two markets ended up held on both sides by different attempts. KXTSAW-26SEP20-A2.40
    took two NO legs and then a YES leg, KXOPENSHARE-26SEP21-18 a NO and then a YES, and
    the exchange paid $3.00 on each at the fill that crossed. The walk credited nothing
    until settlement, so the account held $6.00 the walk could not name.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    _both_sides(lg, fake, "KXTSAW-26SEP20-A2.40",
                [("no", "0.40"), ("no", "0.35"), ("yes", "0.55")], matched=3)
    _both_sides(lg, fake, "KXOPENSHARE-26SEP21-18",
                [("no", "0.30"), ("yes", "0.65")], matched=3)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["drift"] == D("0.0000")
    assert out["verdict"] == "exact"
    assert out["expected"] == out["actual"] == q4(fake.balance)
    netting = out["detail"]["netting"]
    assert netting["n"] == 2 and netting["total"] == "6.0000"
    pairs = {p["ticker"]: p for p in netting["pairs"]}
    tsaw = pairs["KXTSAW-26SEP20-A2.40"]
    assert tsaw["matched_contracts"] == "3.0000" and tsaw["prepaid"] == "3.0000"
    assert tsaw["no_contracts"] == "6.0000" and tsaw["yes_contracts"] == "3.0000"
    assert len(tsaw["no_bets"]) == 2 and len(tsaw["yes_bets"]) == 1


def test_without_the_term_the_same_world_is_the_six_dollar_halt(env):
    """The control, so the number above is not a rehearsal: take the term away and the
    drift is the +6.00 that stopped the system, with every cross-check passing."""
    lg, fake, s = env
    _go_live(lg, fake)
    _both_sides(lg, fake, "KXTSAW-26SEP20-A2.40",
                [("no", "0.40"), ("no", "0.35"), ("yes", "0.55")], matched=3)
    _both_sides(lg, fake, "KXOPENSHARE-26SEP21-18",
                [("no", "0.30"), ("yes", "0.65")], matched=3)

    walk = reconcile._walk(
        lg, s, reconcile._real_bets(lg), GENESIS,
        D(lg.meta_get("live_genesis_balance")),
    )[0]
    without = q4(walk - D(reconcile._netting_term(reconcile._real_bets(lg))[0]))
    assert q4(D(str(fake.balance)) - without) == D("6.0000")


def test_the_term_hands_over_to_the_payouts_when_the_legs_settle(env):
    """The algebra that makes the term safe: it disappears in the same pass that books the
    payouts it stood in for, and the totals agree from either side.

    KXTSAW resolved NO, so its two NO legs win $6.00 against an exchange settlement of
    $3.00 (the exchange had already paid the other $3.00 at the fill). KXOPENSHARE
    resolved NO too, so its one NO leg wins $3.00 against a settlement of $0.00, the whole
    position having been netted away.
    """
    lg, fake, s = env
    _go_live(lg, fake)
    _both_sides(lg, fake, "KXTSAW-26SEP20-A2.40",
                [("no", "0.40"), ("no", "0.35"), ("yes", "0.55")], matched=3)
    _both_sides(lg, fake, "KXOPENSHARE-26SEP21-18",
                [("no", "0.30"), ("yes", "0.65")], matched=3)
    for ticker in ("KXTSAW-26SEP20-A2.40", "KXOPENSHARE-26SEP21-18"):
        fake.resolve(ticker, "no", ts=GENESIS + timedelta(hours=6))

    settle.settle_once(lg, fake, s, now=NOW)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["detail"]["netting"]["n"] == 0        # the legs left the filled population
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"
    paid: dict[str, D] = {}
    for c in out["detail"]["credits"]["bets"]:
        paid[c["ticker"]] = paid.get(c["ticker"], D("0")) + D(c["payout"])
    # 6.00 where the exchange settled 3.00, and 3.00 where it settled 0: the other half of
    # each was the matched dollars, paid at the fill and carried by the term until now.
    assert paid == {"KXTSAW-26SEP20-A2.40": D("6.0000"),
                    "KXOPENSHARE-26SEP21-18": D("3.0000")}


def test_one_side_only_is_not_a_netted_pair(env):
    """An ordinary open position, however many legs it has, was never pre-paid."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    _place_real(lg, fake, aid, 1, "KXONESIDE", side="no", price="0.40", contracts=3)
    _place_real(lg, fake, _attempt(lg), 2, "KXONESIDE", side="no", price="0.35",
                contracts=3)

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["detail"]["netting"] == {"n": 0, "total": "0.0000", "pairs": []}
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"


def test_only_the_matched_contracts_are_counted(env):
    """The Aug-15 KXTRUTHSOCIAL-B189 shape: 1 NO against 3 YES is one matched pair plus a
    live 2-YES position, and only the pair was pre-paid."""
    lg, fake, s = env
    _go_live(lg, fake)
    _place_real(lg, fake, _attempt(lg), 1, "KXPART", side="no", price="0.72", contracts=1)
    # three one-contract YES legs, so the fake can consume one whole fill against the NO
    for idx, price in ((2, "0.2717"), (3, "0.19"), (4, "0.07")):
        _place_real(lg, fake, _attempt(lg), idx, "KXPART", side="yes", price=price,
                    contracts=1)
    fake.net_matched_pair("KXPART", 1, ts=GENESIS + timedelta(hours=4))

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)

    assert out["detail"]["netting"]["total"] == "1.0000"
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"


def test_a_fractional_pair_is_counted_exactly(env):
    """Counts are exact Decimals on this path, as everywhere else: fractional trading is
    live and a truncated count is a truncated dollar."""
    lg, fake, s = env
    _go_live(lg, fake)
    _place_real(lg, fake, _attempt(lg), 1, "KXFRAC", side="no", price="0.40", contracts=3)
    coid = _place_real(lg, fake, _attempt(lg), 2, "KXFRAC", side="yes", price="0.55",
                       contracts=3)
    lg.update_bet(coid, contracts=D("0.28"))      # the A-0054 shape, a fractional fill

    netting = reconcile._walk(
        lg, s, reconcile._real_bets(lg), GENESIS,
        D(lg.meta_get("live_genesis_balance")),
    )[1]["netting"]

    assert netting["total"] == "0.2800"
    assert netting["pairs"][0]["matched_contracts"] == "0.2800"


def test_a_voided_leg_is_not_half_a_pair(env):
    """Only ``filled`` legs are a position. A void refunded its stake and holds nothing."""
    lg, fake, s = env
    _go_live(lg, fake)
    _place_real(lg, fake, _attempt(lg), 1, "KXVOIDPAIR", side="no", price="0.40",
                contracts=3)
    coid = _place_real(lg, fake, _attempt(lg), 2, "KXVOIDPAIR", side="yes", price="0.55",
                       contracts=3)
    lg.update_bet(coid, status="voided", outcome="void", pnl=D("0"), fee=D("0"),
                  settled_at=(GENESIS + timedelta(hours=5)).isoformat().replace(
                      "+00:00", "Z"))

    netting = reconcile._walk(
        lg, s, reconcile._real_bets(lg), GENESIS,
        D(lg.meta_get("live_genesis_balance")),
    )[1]["netting"]

    assert netting == {"n": 0, "total": "0.0000", "pairs": []}


# ------------------------------------------- schema 010: what the exchange gives back
def test_a_recorded_credit_explains_the_cent_the_walk_could_not(env):
    """2026-09-20 04:45: Kalshi credited $0.01 as "Incentive+: Volume Incentive For Event
    KXRAINDNYC-260919". No API reports it, so the walk had no term for it and the night
    read as a cent of unexplained drift. Recording it makes the same night walk to zero."""
    lg, fake, s = env
    _no_absorption(s)
    _clean_world(lg, fake)
    fake.set_balance(str(q4(D(str(fake.balance)) + D("0.01"))))    # the credit lands

    unexplained = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert unexplained["drift"] == D("0.0100")
    assert unexplained["detail"]["exchange_credits"] == {
        "n": 0, "total": "0.0000", "credits": []
    }

    lg.insert_credit(
        credited_at=(GENESIS + timedelta(hours=6)).isoformat().replace("+00:00", "Z"),
        amount=D("0.01"), kind="incentive",
        reason="Volume Incentive For Event KXRAINDNYC-260919",
        recorded_at="2026-09-21T18:00:00Z",
    )

    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=15))
    assert out["drift"] == D("0.0000")
    assert out["ok"] is True
    given = out["detail"]["exchange_credits"]
    assert given["n"] == 1 and given["total"] == "0.0100"
    assert given["credits"][0]["kind"] == "incentive"
    assert given["credits"][0]["reason"].startswith("Volume Incentive")
    # …and the bet-payouts term keeps its own key, which is what "credits" has meant here
    # since the first walk.
    assert out["detail"]["credits"]["n"] == 3


def test_a_credit_before_genesis_is_not_spent_twice(env):
    """It is already inside ``live_genesis_balance``; adding it again invents a cent."""
    lg, fake, s = env
    _clean_world(lg, fake)
    lg.insert_credit(credited_at=(GENESIS - timedelta(days=2)).isoformat().replace(
        "+00:00", "Z"), amount=D("0.01"), kind="incentive",
        recorded_at="2026-09-21T18:00:00Z")

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["drift"] == D("0.0000")
    assert out["detail"]["exchange_credits"]["n"] == 0


def test_the_report_names_the_credit_it_spent(env):
    """A HALT report is the document somebody reads at 3am to find the term that is wrong,
    so every term the walk spent has to be a line in it."""
    lg, fake, s = env
    _clean_world(lg, fake)
    lg.insert_credit(credited_at=(GENESIS + timedelta(hours=6)).isoformat().replace(
        "+00:00", "Z"), amount=D("0.01"), kind="incentive",
        reason="Volume Incentive For Event KXRAINDNYC-260919",
        recorded_at="2026-09-21T18:00:00Z")
    fake.set_balance(str(q4(D(str(fake.balance)) - D("5.00"))))   # a HALT-sized hole

    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    assert out["halted"] is True
    assert "| + exchange credits, n=1 (incentive) | $0.0100 |" in (
        Path(out["report"]).read_text()
    )


def test_a_deposit_is_unaffected_by_a_recorded_credit(env):
    """The credit is a flow and the deposit moves the anchor, so the two do not interact:
    the implied transfer is still exactly the $20 that arrived, and the anchor moves by
    $20 rather than by $20.01."""
    lg, fake, s = env
    _clean_world(lg, fake)
    lg.insert_credit(credited_at=(GENESIS + timedelta(hours=6)).isoformat().replace(
        "+00:00", "Z"), amount=D("0.01"), kind="incentive",
        recorded_at="2026-09-21T18:00:00Z")
    fake.set_balance(str(q4(D(str(fake.balance)) + D("0.01") + D("20.00"))))

    out = _deposit(lg, fake, s, "20.00")

    assert out["ok"] and out["reason"] is None
    assert out["implied"] == D("20.0000") and out["stated"] == D("20.0000")
    assert out["old_genesis_balance"] == D("30.1600")
    assert out["new_genesis_balance"] == D("50.1600")
    # The derivation is arithmetic somebody re-does by hand, so a term the walk spent has
    # to be in it or the line will not add up for whoever checks it.
    assert "+ 0.0100 exchange credits (1)" in out["decisions_line"]
    assert reconcile.reconcile_once(lg, fake, s, now=NOW)["drift"] == D("0.0000")


def test_the_fee_check_reads_what_the_fills_charged_not_the_model_at_the_average(env):
    """The HALT of 2026-10-02 (A-0391-B04). Three NO contracts filled at 0.75, 0.79, 0.55
    and 0.55; the exchange charged $0.0475 in fees, fill by fill, and the scan healed the
    row to that. The check priced the fee from the model at the 0.6194 average, $0.0496,
    called the $0.0021 a fee mismatch on a walk that was exact, and HALTed. It now sums
    the fills' own fees, and only falls back to the model when a fill carries none."""
    lg, fake, s = env
    _go_live(lg, fake)
    aid = _attempt(lg)
    fake.add_market("KXDIESELW", title="t", close_time=CLOSE, no_ask=D("0.55"),
                    no_ask_size=50)
    fake.set_order_behavior("KXDIESELW", "levels:0.80@0.75,0.20@0.79,1.00@0.55,1.00@0.55")
    coid = f"{aid}-B01"
    r = fake.create_order("KXDIESELW", "no", D("0.79"), 3, coid)
    lg.insert_bet(
        bet_id=coid, attempt_id=aid, ticket_index=1, ticker="KXDIESELW", side="no",
        limit_price=D("0.79"), model_prob=D("0.60"), rationale="r", is_real=1,
        status="filled", contracts=3, fill_price=r.avg_fill_price,
        stake=D("1.8580"), fee=D("0.0475"), order_id=r.order_id, client_order_id=coid,
        placed_at=(GENESIS + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    )
    settle.genesis_snapshot(lg, fake)
    # the model at the average is past the tolerance: the old check would have failed
    model = calc_fee(D(3), q4(D("1.8580") / 3), COEF)
    assert model - D("0.0475") > reconcile._FEE_TOLERANCE

    # a fee that really is wrong still fails, and says the fills were the reference
    lg.update_bet(coid, fee=D("0.0496"))
    out = reconcile.reconcile_once(lg, fake, s, now=NOW)
    problems = out["checks"]["fills_match"]["problems"]
    assert [(p["problem"], p["actual"], p["source"]) for p in problems] == [
        ("fee_mismatch", "0.0475", "fills")]

    # the row healed to what was charged: the check passes and the walk is exact
    lg.update_bet(coid, fee=D("0.0475"))
    out = reconcile.reconcile_once(lg, fake, s, now=NOW + timedelta(minutes=1))
    assert out["checks"]["fills_match"]["ok"] is True, out["checks"]["fills_match"]
    assert out["drift"] == D("0.0000") and out["verdict"] == "exact"
    assert out["failed_checks"] == []


def test_a_live_fill_carries_its_fee_in_dollars():
    f = Fill.from_api({"order_id": "o", "side": "no", "count_fp": "0.80",
                       "no_price_dollars": "0.7500", "fee_cost": "0.010500"})
    assert f.fee == D("0.010500") and f.count == D("0.80") and f.price == D("0.7500")
    assert Fill.from_api({"side": "yes", "count_fp": "1.00", "yes_price_dollars": "0.40",
                          "fee_cost_dollars": "0.0168"}).fee == D("0.0168")
    assert Fill.from_api({"side": "yes", "count_fp": "1.00"}).fee is None
