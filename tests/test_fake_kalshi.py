"""Tests for ``FakeKalshi``'s money world (spec §8's test double).

The point of these tests is one property: a reconciliation walk over the fake balances
to the cent. Every fill debits stake + fee, every settlement credits the winner, every
void refunds, and ``balance_ledger()`` is the audit trail that proves it. The net effect
per settled bet is asserted against ``moneymath.bet_pnl`` — the same function the ledger
uses — so the fake and the real money math cannot silently disagree.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from betting_agent.kalshi import Settlement
from betting_agent.kalshi.client import KalshiAPIError, OrderAmbiguous
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.kalshi.types import reported_cost, reported_fee
from betting_agent.moneymath import bet_pnl, q4
from betting_agent.moneymath import fee as money_fee

D = Decimal
COEF = D("0.07")
CLOSE = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)


@pytest.fixture
def fake():
    f = FakeKalshi(balance="10.0000", fee_coef=COEF)
    f.add_market("T", title="Test market", close_time=CLOSE,
                 yes_ask=D("0.42"), yes_ask_size=10,
                 no_ask=D("0.55"), no_ask_size=10)
    return f


def deltas(f, reason):
    return [e["delta"] for e in f.balance_ledger() if e["reason"] == reason]


# --------------------------------------------------------------------- fills debit
def test_seeded_balance_is_the_first_trail_entry(fake):
    trail = fake.balance_ledger()
    assert len(trail) == 1
    assert trail[0]["reason"] == "seed"
    assert trail[0]["delta"] == D("10.0000") and trail[0]["running"] == D("10.0000")
    assert fake.get_balance().dollars == D("10.0000")


def test_fill_debits_stake_plus_fee_to_the_cent(fake):
    r = fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")

    assert r.filled_count == 2 and r.avg_fill_price == D("0.42")
    assert r.fee == money_fee(2, D("0.42"), COEF) == D("0.0342")
    # stake 2 x 0.42 = 0.84, fee ceil_4dp(0.07*2*0.42*0.58) = ceil(0.034104) = 0.0342,
    # so 10.0000 - 0.8742. No cent floor and no cent rounding (docs/14 D5).
    assert fake.balance == D("9.1258")
    assert isinstance(fake.balance, Decimal)
    assert str(fake.balance) == "9.1258"  # 4dp, the ledger's TEXT convention
    assert deltas(fake, "fill") == [D("-0.8742")]


def test_no_side_fill_debits_its_own_ask(fake):
    fake.create_order("T", "no", D("0.55"), 3, "A-0001-B01")
    # stake 3 x 0.55 = 1.65, fee = ceil_4dp(0.07*3*0.55*0.45) = ceil(0.051975) = 0.0520
    assert money_fee(3, D("0.55"), COEF) == D("0.0520")
    assert fake.balance == D("10.0000") - D("1.65") - D("0.0520") == D("8.2980")


def test_partial_fill_debits_only_the_filled_portion(fake):
    fake.set_order_behavior("T", "partial:1")
    r = fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")

    # FK-3: the status was 'partially_filled', which production can never observe — the
    # real client synthesizes only 'executed'/'canceled', because for an IOC the unfilled
    # remainder is canceled by construction. The partial-ness is in filled_count.
    assert r.filled_count == 1 and r.status == "executed"
    assert r.fee == D("0.0171")  # ceil_4dp(0.07 * 1 * 0.42 * 0.58) = ceil(0.017052)
    assert fake.balance == D("10.0000") - D("0.42") - D("0.0171") == D("9.5629")


def test_no_fill_debits_nothing(fake):
    fake.set_order_behavior("T", "no_fill")
    r = fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")

    # FK-3: fee/order_id/avg_fill_price were 0 and a synthetic id here, where the real
    # client returns None for all three — a test could pass on values production never
    # sees. Nothing filled, so the receipt claims nothing.
    assert r.filled_count == 0 and r.status == "canceled"
    assert r.fee is None and r.order_id is None and r.avg_fill_price is None
    assert fake.balance == D("10.0000")
    assert deltas(fake, "fill") == []
    assert fake.get_fills()[0] == []


def test_unfillable_ask_debits_nothing(fake):
    """Default behavior: no fill when the resting ask is above our limit."""
    r = fake.create_order("T", "yes", D("0.30"), 1, "A-0001-B01")
    assert r.filled_count == 0
    assert fake.balance == D("10.0000")


def test_ambiguous_order_debits_nothing(fake):
    fake.set_order_behavior("T", "ambiguous")
    with pytest.raises(OrderAmbiguous):
        fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")

    assert fake.balance == D("10.0000")
    assert len(fake.balance_ledger()) == 1  # seed only
    assert fake.orders_placed[-1]["client_order_id"] == "A-0001-B01"  # attempt recorded
    assert fake.get_orders()[0] == []                # nothing landed on the exchange
    assert fake.find_fills_by_client_order_id("A-0001-B01") == []


def test_ambiguous_after_fill_books_the_trade_and_only_loses_the_receipt(fake):
    """The expensive ambiguity: the money moved and the answer did not come back.

    ``ambiguous`` above models a request the matching engine never saw. This models the
    other world — the one the executor's bounded re-scan exists for — and the difference
    between them is the whole point of the resolution logic. Scripting it used to require
    ``add_personal_fill(client_order_id=...)``, which hangs a fill on no order at all; the
    coid then lives only in the fake's private fill log and the join resolves through its
    documented fallback rather than the orders->fills walk production runs. Here the coid
    lives on the ORDER, where the live API puts it (FK-4), and the join is the real one.
    """
    fake.set_order_behavior("T", "ambiguous_after_fill")

    with pytest.raises(OrderAmbiguous) as exc:
        fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")

    assert exc.value.client_order_id == "A-0001-B01"
    # The trade is real: order logged, fills booked, balance debited exactly once.
    orders, _ = fake.get_orders()
    assert [o["client_order_id"] for o in orders] == ["A-0001-B01"]
    assert orders[0]["status"] == "executed"
    fee = money_fee(2, D("0.42"), COEF)
    assert fake.balance == D("10.0000") - (D("2") * D("0.42") + fee)
    assert deltas(fake, "fill") == [-(D("2") * D("0.42") + fee)]
    # And it is discoverable exactly the way the resolver discovers it.
    fills = fake.find_fills_by_client_order_id("A-0001-B01")
    assert [(f.count, f.price, f.side) for f in fills] == [(D("2"), D("0.42"), "yes")]
    # The public fills feed still carries no coid — the join is what attributes it.
    served, _ = fake.get_fills()
    assert [f.client_order_id for f in served] == [None]


def test_ambiguous_after_fill_on_an_uncrossable_book_lands_an_empty_order(fake):
    """The order still LANDED — it just filled nothing, and the receipt was lost anyway.
    The resolver has to conclude no-fill here, and it must do so from a real (canceled)
    order rather than from the absence of one."""
    fake.set_order_behavior("T", "ambiguous_after_fill")

    with pytest.raises(OrderAmbiguous):
        fake.create_order("T", "yes", D("0.10"), 2, "A-0001-B01")  # below the 0.42 ask

    orders, _ = fake.get_orders()
    assert [o["status"] for o in orders] == ["canceled"]
    assert fake.balance == D("10.0000")
    assert fake.find_fills_by_client_order_id("A-0001-B01") == []


def test_scripted_full_fill_overrides_the_size_not_the_limit(fake):
    """FK-6: this used to fill 2 contracts at 0.42 against a 0.10 limit — a trade the
    exchange has no way to make. ``fill`` overrides the resting SIZE; the limit still
    binds, so an uncrossable book yields nothing however loudly the test scripts it."""
    fake.set_order_behavior("T", "fill")
    fake.set_book("T", "yes", D("0.42"), 1)  # only 1 resting, but 'fill' wants both

    above_the_limit = fake.create_order("T", "yes", D("0.10"), 2, "A-0001-B01")
    assert above_the_limit.filled_count == 0 and above_the_limit.status == "canceled"
    assert fake.balance == D("10.0000")

    crossable = fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B02")
    assert crossable.filled_count == 2  # the size, and only the size, is overridden
    assert fake.balance == D("9.1258")


# ---------------------------------------------------------------- settlement credits
def test_resolve_credits_the_winning_side_only(fake):
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.resolve("T", "yes")

    assert deltas(fake, "settlement_win") == [D("2.0000")]  # 2 contracts x $1.00
    assert fake.balance == D("11.1258")  # 10.0000 - 0.8742 + 2.0000
    # net of the whole round trip == the ledger's own P/L function
    assert fake.balance - D("10.0000") == bet_pnl("win", 2, D("0.42"), D("0.0342"))


def test_resolve_credits_nothing_to_the_losing_side(fake):
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.resolve("T", "no")

    assert deltas(fake, "settlement_win") == []
    assert deltas(fake, "settlement_loss") == [D("0.0000")]  # seen, paid nothing
    assert fake.balance == D("9.1258")
    assert fake.balance - D("10.0000") == bet_pnl("loss", 2, D("0.42"), D("0.0342"))


def test_no_side_win_is_credited(fake):
    fake.create_order("T", "no", D("0.55"), 3, "A-0001-B01")
    fake.resolve("T", "no")

    assert deltas(fake, "settlement_win") == [D("3.0000")]
    assert fake.balance - D("10.0000") == bet_pnl("win", 3, D("0.55"), D("0.0520"))


def test_void_refunds_stake_and_fee_and_nets_zero(fake):
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.resolve("T", "void")

    assert deltas(fake, "settlement_void") == [D("0.8742")]  # stake + fee back
    assert fake.balance == D("10.0000")
    assert fake.balance - D("10.0000") == bet_pnl("void", 2, D("0.42"), D("0.0342"))


@pytest.mark.parametrize("result", ["", "cancelled"])
def test_non_decisive_results_are_voids(fake, result):
    fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")
    fake.resolve("T", result)
    assert fake.balance == D("10.0000")


def test_resolve_keeps_market_status_behavior(fake):
    fake.resolve("T", "yes")
    m = fake.get_market("T")
    assert m.status == "finalized" and m.raw["result"] == "yes"
    assert fake.get_markets(status="open")[0] == []  # no longer open


def test_re_resolving_never_pays_twice(fake):
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.resolve("T", "yes")
    after_first = fake.balance
    fake.resolve("T", "yes")

    assert fake.balance == after_first
    assert deltas(fake, "settlement_win") == [D("2.0000")]
    assert len(fake.get_settlements()[0]) == 1


def test_two_markets_settle_independently(fake):
    fake.add_market("U", title="Other", close_time=CLOSE, yes_ask=D("0.20"), yes_ask_size=5)
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.create_order("U", "yes", D("0.20"), 1, "A-0001-B02")
    fake.resolve("T", "yes")

    assert deltas(fake, "settlement_win") == [D("2.0000")]  # U untouched
    fake.resolve("U", "no")
    assert deltas(fake, "settlement_loss") == [D("0.0000")]
    expected = (D("10.0000")
                + bet_pnl("win", 2, D("0.42"), D("0.0342"))
                + bet_pnl("loss", 1, D("0.20"), D("0.0112")))
    assert fake.balance == expected


# ----------------------------------------------------------------- settlements list
def test_settlement_appears_with_live_payload_shape(fake):
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.resolve("T", "yes")

    settlements, cursor = fake.get_settlements()
    assert cursor is None and len(settlements) == 1
    s = settlements[0]
    assert s.ticker == "T" and s.market_result == "yes"
    assert s.ts is not None and s.ts.tzinfo is not None
    # the raw payload mirrors the recorded fixture, so re-parsing round-trips
    again = Settlement.from_api(s.raw)
    assert again.ticker == s.ticker and again.market_result == s.market_result
    assert again.ts == s.ts
    assert s.raw["yes_count_fp"] == "2.00" and s.raw["no_count_fp"] == "0.00"
    assert s.raw["yes_total_cost_dollars"] == "0.8400"
    assert s.raw["fee_cost"] == "0.0342"
    assert s.raw["revenue"] == 200 and s.raw["value"] == 200  # cents, per the fixture


def test_no_settlement_without_a_position(fake):
    """The live endpoint reports settlements for markets you held, not every market
    that resolved — otherwise §8's ``settlements ⊆ our settled bets`` check misfires."""
    fake.resolve("T", "yes")
    assert fake.get_settlements()[0] == []


def test_add_settlement_injects_a_bare_settlement_without_moving_money(fake):
    fake.add_settlement("FOREIGN", "yes", contracts=2, price="0.30")

    settlements, _ = fake.get_settlements()
    assert [s.ticker for s in settlements] == ["FOREIGN"]
    assert settlements[0].raw["revenue"] == 200
    assert fake.balance == D("10.0000")
    assert len(fake.balance_ledger()) == 1


def test_settlements_min_ts_filters_on_settled_time(fake):
    early, late = CLOSE - timedelta(hours=2), CLOSE + timedelta(hours=2)
    fake.add_settlement("OLD", "yes", ts=early)
    fake.add_settlement("NEW", "yes", ts=late)

    assert [s.ticker for s in fake.get_settlements()[0]] == ["OLD", "NEW"]
    assert [s.ticker for s in fake.get_settlements(min_ts=CLOSE)[0]] == ["NEW"]
    assert [s.ticker for s in fake.get_settlements(min_ts=late)[0]] == ["NEW"]  # inclusive
    assert fake.get_settlements(min_ts=late + timedelta(seconds=1))[0] == []


def test_resolve_honors_an_explicit_settled_time(fake):
    early = CLOSE - timedelta(days=2)
    fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")
    fake.resolve("T", "yes", ts=early)

    assert fake.get_settlements(min_ts=CLOSE)[0] == []
    assert [s.ticker for s in fake.get_settlements(min_ts=early)[0]] == ["T"]
    assert fake.balance_ledger()[-1]["ts"] == early


def test_settlement_without_a_timestamp_is_retained(fake):
    """A tripwire must never silently drop account activity (the rule get_orders states)."""
    fake._settlements.append(Settlement(ticker="NO-TS", market_result="yes", ts=None))
    assert [s.ticker for s in fake.get_settlements(min_ts=CLOSE)[0]] == ["NO-TS"]


# ------------------------------------------------------------------ fills read surface
def test_fills_min_ts_filters_on_created_time(fake):
    early, late = CLOSE - timedelta(hours=2), CLOSE + timedelta(hours=2)
    fake.add_personal_fill("T", "yes", 1, D("0.40"), ts=early)
    fake.add_personal_fill("T", "yes", 1, D("0.40"), ts=late)

    assert len(fake.get_fills()[0]) == 2
    got, cursor = fake.get_fills(min_ts=CLOSE)
    assert cursor is None and [f.ts for f in got] == [late]
    assert len(fake.get_fills(min_ts=late)[0]) == 1  # inclusive


def test_fill_without_a_timestamp_is_retained(fake):
    fake.add_personal_fill("T", "yes", 1, D("0.40"), ts=None)
    fake._fill_log[-1].fill.ts = None
    assert len(fake.get_fills(min_ts=CLOSE)[0]) == 1


def test_find_fills_by_client_order_id_still_joins(fake):
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.add_personal_fill("T", "yes", 1, D("0.40"), client_order_id="SOMEONE-ELSE")

    ours = fake.find_fills_by_client_order_id("A-0001-B01")
    assert len(ours) == 1 and ours[0].count == 2 and ours[0].price == D("0.42")
    assert fake.find_fills_by_client_order_id("A-9999-B99") == []


def test_orders_min_ts_retains_orders_without_created_time(fake):
    """Mirrors the real client: an order missing created_time survives a min_ts filter."""
    fake.add_personal_order("T", ts=CLOSE - timedelta(hours=2))
    fake.add_personal_order("T", ts=CLOSE + timedelta(hours=2))
    fake._orders[0].pop("created_time")

    orders, cursor = fake.get_orders(min_ts=CLOSE)
    assert cursor is None and len(orders) == 2
    assert len(list(fake.iter_orders(min_ts=CLOSE))) == 2


# ---------------------------------------------------------------- personal activity
def test_personal_order_moves_the_balance(fake):
    """§8 drift: money left the account with nothing in our ledger to explain it."""
    fake.add_personal_order("T", side="yes", count=2, price="0.30")

    # stake 0.60 + fee 0.07*2*0.30*0.70 = 0.0294, already on the 4dp grid so the
    # ceiling leaves it alone
    assert fake.balance == D("10.0000") - D("0.60") - D("0.0294") == D("9.3706")
    entry = fake.balance_ledger()[-1]
    assert entry["reason"] == "personal_order" and entry["delta"] == D("-0.6294")
    assert entry["ticker"] == "T" and entry["contracts"] == 2
    # and it is invisible to a ledger-only walk: no fill, no coid
    assert fake.get_fills()[0] == []
    assert "client_order_id" not in fake.get_orders()[0][-1]


def test_personal_order_drifts_against_a_ledger_only_walk(fake):
    """The walk §8 performs over OUR bets alone cannot explain the foreign debit."""
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")  # ours
    fake.add_personal_order("T", side="yes", count=1, price="0.50")  # theirs

    ours_only = sum(
        (e["delta"] for e in fake.balance_ledger()
         if e["reason"] == "fill" or e["reason"].startswith("settlement_")),
        D("0"),
    )
    expected = q4(D("10.0000") + ours_only)
    drift = q4(fake.get_balance().dollars - expected)
    assert drift == D("-0.5175")  # 1 x 0.50 stake + 0.0175 fee, unexplained
    assert drift != 0


def test_personal_order_can_opt_out_of_moving_money(fake):
    fake.add_personal_order("T", count=1, price="0.50", move_balance=False)
    assert fake.balance == D("10.0000")
    assert len(fake.get_orders()[0]) == 1


def test_partially_filled_personal_order_debits_only_the_fill(fake):
    fake.add_personal_order("T", count=4, filled=1, price="0.30", status="partially_filled")
    # fee on the FILLED 1, not the ordered 4: 0.07 * 1 * 0.30 * 0.70 = 0.0147
    assert fake.balance == D("10.0000") - D("0.30") - D("0.0147") == D("9.6853")
    assert fake.get_orders()[0][0]["fill_count_fp"] == "1.00"
    assert fake.get_orders()[0][0]["initial_count_fp"] == "4.00"


def test_impostor_order_still_injects_and_moves_money(fake):
    oid = fake.add_impostor_order("A-9999-B01", price="0.50")
    orders, _ = fake.get_orders()
    assert [o["client_order_id"] for o in orders] == ["A-9999-B01"]
    assert orders[0]["order_id"] == oid and orders[0]["ticker"] == "KXIMPOSTOR"
    assert fake.balance == D("10.0000") - D("0.50") - D("0.0175")


def test_personal_fill_debits_and_settles_like_ours(fake):
    fake.add_personal_fill("T", "yes", 2, D("0.40"))
    # fee = 0.07 * 2 * 0.40 * 0.60 = 0.0336, already on the 4dp grid
    assert fake.balance == D("10.0000") - D("0.80") - D("0.0336") == D("9.1664")
    assert fake.balance_ledger()[-1]["reason"] == "personal_fill"

    fake.resolve("T", "yes")
    assert fake.balance == D("11.1664")  # 9.1664 + 2 contracts x $1.00
    assert fake.get_settlements()[0][0].raw["yes_count_fp"] == "2.00"


def test_personal_fill_can_opt_out_of_moving_money(fake):
    """Scripting an order and its fill as one money event."""
    fake.add_personal_order("T", count=1, price="0.50")
    fake.add_personal_fill("T", "yes", 1, D("0.50"), move_balance=False)

    assert fake.balance == D("10.0000") - D("0.50") - D("0.0175")
    assert len(fake.get_fills()[0]) == 1  # fill visible, money counted once


def test_maker_personal_fill_pays_no_fee(fake):
    fake.add_personal_fill("T", "yes", 1, D("0.40"), is_taker=False)
    assert fake.balance == D("9.6000")  # stake only
    assert fake.balance_ledger()[-1]["fee"] == D("0")


# ------------------------------------------------------------------- balance trail
def test_balance_ledger_reconstructs_the_running_balance(fake):
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.add_personal_order("T", count=1, price="0.50")
    fake.resolve("T", "yes")

    trail = fake.balance_ledger()
    assert [e["reason"] for e in trail] == [
        "seed", "fill", "personal_order", "settlement_win",
    ]
    running = D("0")
    for entry in trail:
        running = q4(running + entry["delta"])
        assert entry["running"] == running
    assert running == fake.balance == fake.get_balance().dollars
    assert all(isinstance(e["delta"], Decimal) for e in trail)
    assert all(e["ts"].tzinfo is not None for e in trail)


def test_balance_ledger_is_a_copy(fake):
    fake.balance_ledger().append({"reason": "bogus"})
    fake.balance_ledger()[0]["delta"] = D("999")
    assert [e["reason"] for e in fake.balance_ledger()] == ["seed"]
    assert fake.balance_ledger()[0]["delta"] == D("10.0000")


def test_set_balance_reseeds_and_keeps_the_trail_consistent(fake):
    fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    fake.set_balance("25.0000")

    assert fake.balance == D("25.0000")
    assert str(fake.balance) == "25.0000"  # existing behavior: str round-trips input
    seed = fake.balance_ledger()[-1]
    assert seed["reason"] == "seed" and seed["previous"] == D("9.1258")
    assert seed["delta"] == D("15.8742") and seed["running"] == D("25.0000")
    assert sum((e["delta"] for e in fake.balance_ledger()), D("0")) == D("25.0000")


def test_reconciliation_walk_balances_to_the_cent(fake):
    """The §8 walk: genesis − Σ(stake+fee) + Σ payout == the exchange balance."""
    fake.add_market("U", title="Loser", close_time=CLOSE, yes_ask=D("0.55"), yes_ask_size=5)
    fake.add_market("V", title="Void", close_time=CLOSE, yes_ask=D("0.33"), yes_ask_size=5)
    genesis = fake.get_balance().dollars

    placed = [
        ("T", D("0.42"), 2, "A-0001-B01", "yes"),
        ("U", D("0.55"), 1, "A-0001-B02", "no"),
        ("V", D("0.33"), 3, "A-0001-B03", "void"),
    ]
    spend = D("0")
    payout = D("0")
    for ticker, price, count, coid, result in placed:
        r = fake.create_order(ticker, "yes", price, count, coid)
        assert r.filled_count == count
        spend += q4(D(count) * price) + r.fee
        fake.resolve(ticker, result)
        if result == "yes":
            payout += D(count) * D("1.00")
        elif result == "void":
            payout += q4(D(count) * price) + r.fee  # refund

    expected = q4(genesis - spend + payout)
    assert q4(fake.get_balance().dollars) - expected == D("0")
    # and the same number falls out of the trail alone
    assert q4(sum((e["delta"] for e in fake.balance_ledger()), D("0"))) == expected
    assert len(fake.get_settlements()[0]) == 3
    assert len(fake.get_fills()[0]) == 3


# ------------------------------------------------------------------ fault injection
def test_reject_behavior_raises_a_definite_api_error(fake):
    """FK-2 slice: a 4xx rejection with a body — the shape 'insufficient balance on bet
    three of four' actually takes."""
    fake.set_order_behavior("T", "reject:400:insufficient balance")

    with pytest.raises(KalshiAPIError) as ei:
        fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")
    assert ei.value.status == 400
    assert ei.value.body == "insufficient balance"
    assert "insufficient balance" in str(ei.value)


def test_rejected_order_lands_nowhere_and_costs_nothing(fake):
    fake.set_order_behavior("T", "reject:429")

    with pytest.raises(KalshiAPIError) as ei:
        fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")

    assert ei.value.status == 429 and ei.value.body is None
    assert fake.get_orders()[0] == []       # never entered the order book
    assert fake.get_fills()[0] == []        # no fill
    assert fake.balance == D("10.0000")     # no money moved
    assert len(fake.balance_ledger()) == 1  # only the seed entry
    # the transmission itself is still observable
    assert [o["client_order_id"] for o in fake.orders_placed] == ["A-0001-B01"]


def test_reject_is_per_ticker_so_other_markets_still_fill(fake):
    fake.add_market("U", title="Other", close_time=CLOSE, yes_ask=D("0.30"), yes_ask_size=10)
    fake.set_order_behavior("T", "reject:400:market closed")

    ok = fake.create_order("U", "yes", D("0.30"), 1, "A-0001-B01")
    with pytest.raises(KalshiAPIError):
        fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B02")

    assert ok.filled_count == 1
    assert [o["client_order_id"] for o in fake.get_orders()[0]] == ["A-0001-B01"]


# ------------------------------------------------------------------ fractional counts
def test_fractional_fills_sum_exactly_and_move_the_balance(fake):
    """The live A-0054-B01 shape: three fractional pieces of one 1-contract position."""
    for c in (D("0.28"), D("0.34"), D("0.38")):
        fake.add_personal_fill("T", "yes", c, D("0.15"), client_order_id="A-0054-B01")

    fills = fake.find_fills_by_client_order_id("A-0054-B01")
    assert sum(f.count for f in fills) == D("1.00")
    assert [f.count for f in fills] == [D("0.28"), D("0.34"), D("0.38")]
    # stake is the exact fraction of the position, not a truncated zero, and the fee on a
    # 0.28-contract piece is ceil_4dp(0.07 * 0.28 * 0.15 * 0.85) = ceil(0.002499) = 0.0025
    assert deltas(fake, "personal_fill")[0] == -q4(D("0.28") * D("0.15") + D("0.0025"))


def test_order_fill_count_fp_keeps_fractional_precision(fake):
    fake.add_personal_order("T", count=D("0.90"), filled=D("0.28"), price="0.15")
    order = fake.get_orders()[0][0]
    assert order["initial_count_fp"] == "0.90"
    assert order["fill_count_fp"] == "0.28"


# --------------------------------------------------------------- EF-7: request accounting
def test_every_read_is_counted_by_name(fake):
    fake.reset_calls()
    fake.get_balance()
    fake.get_market("T")
    fake.get_orderbook("T")
    fake.get_orders()
    fake.get_fills()
    fake.get_settlements()
    fake.get_markets()

    assert fake.calls == {
        "get_balance": 1, "get_market": 1, "get_orderbook": 1, "get_orders": 1,
        "get_fills": 1, "get_settlements": 1, "get_markets": 1,
    }
    assert fake.calls_total() == 7
    fake.reset_calls()
    assert fake.calls_total() == 0


def test_paged_reads_count_one_per_page(fake):
    fake.page_size = 3
    for i in range(7):
        fake.add_personal_order(f"P{i}", move_balance=False)
    fake.reset_calls()

    assert len(list(fake.iter_orders())) == 7
    assert fake.calls["get_orders"] == 3  # 3 + 3 + 1


def test_without_page_size_everything_still_arrives_on_page_one(fake):
    for i in range(7):
        fake.add_personal_order(f"P{i}", move_balance=False)
    orders, cursor = fake.get_orders()
    assert len(orders) == 7 and cursor is None


@pytest.mark.parametrize("reader", ["get_orders", "get_fills", "get_settlements"])
def test_portfolio_reads_emit_real_cursors_under_page_size(fake, reader):
    for i in range(5):
        fake.add_market(f"M{i}", title="m", close_time=CLOSE, yes_ask=D("0.10"),
                        yes_ask_size=10)
        fake.create_order(f"M{i}", "yes", D("0.10"), 1, f"A-0001-B{i:02d}")
        fake.resolve(f"M{i}", "yes")
    fake.page_size = 2

    seen, cursor, pages = [], None, 0
    while True:
        page, cursor = getattr(fake, reader)(cursor=cursor)
        seen.extend(page)
        pages += 1
        if not cursor:
            break

    assert len(seen) == 5 and pages == 3


def test_get_markets_pages_and_iter_markets_follows_the_cursor(fake):
    for i in range(5):
        fake.add_market(f"M{i}", title="m", close_time=CLOSE, yes_ask=D("0.10"),
                        yes_ask_size=1)
    fake.page_size = 2

    page, cursor = fake.get_markets()
    assert len(page) == 2 and cursor is not None
    assert len(list(fake.iter_markets())) == 6  # T plus the five added here


# ------------------------------------------------- EF-7: the real orders->fills join
def test_the_coid_join_goes_through_the_order_pages(fake):
    """It used to be a free dict scan, which is exactly why a caller doing it once per bet
    — each time re-paging the whole order history — looked free in the suite."""
    fake.page_size = 2
    for i in range(5):
        fake.add_personal_order(f"P{i}", move_balance=False)
    fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")
    fake.reset_calls()

    fills = fake.find_fills_by_client_order_id("A-0001-B01")

    assert [f.count for f in fills] == [D("1")]
    assert fake.calls["get_orders"] == 3          # paged until the coid matched
    assert fake.calls["get_fills_by_order"] == 1  # then one fills read for that order


def test_the_join_returns_only_that_orders_fills(fake):
    first = fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")
    fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B02")

    fills = fake.find_fills_by_client_order_id("A-0001-B01")

    assert len(fills) == 1
    assert fills[0].raw["order_id"] == first.order_id


def test_a_missing_coid_pages_to_exhaustion_and_returns_nothing(fake):
    fake.page_size = 2
    for i in range(4):
        fake.add_personal_order(f"P{i}", move_balance=False)
    fake.reset_calls()

    assert fake.find_fills_by_client_order_id("A-9999-B01") == []
    assert fake.calls["get_orders"] == 2
    assert "get_fills_by_order" not in fake.calls  # nothing matched, nothing fetched


def test_directly_injected_fills_still_resolve(fake):
    """The documented fake-only fallback: many tests script a join with no order behind
    it. FK-4 (WP5) is where fills stop carrying a coid and this branch goes away."""
    for c in (D("0.28"), D("0.34"), D("0.38")):
        fake.add_personal_fill("T", "yes", c, D("0.15"), client_order_id="A-0054-B01")

    fills = fake.find_fills_by_client_order_id("A-0054-B01")

    assert sum(f.count for f in fills) == D("1.00")


# --------------------------------------------------- FK-1: the refusals the client makes
@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"action": "sell"}, "only buys"),
        ({"side": "bid"}, "side must be"),   # V2's wire vocabulary, not this surface
        ({"side": "ask"}, "side must be"),
        ({"time_in_force": "gtc"}, "unsupported time_in_force"),
        ({"price": D("0.42005")}, "4dp grid"),
        ({"count": D("1.5")}, "whole contracts"),
    ],
)
def test_create_order_refuses_exactly_what_the_client_refuses(fake, kwargs, match):
    """FK-1: the fake validated nothing, so ``side='bid'`` was silently routed to the NO
    ask and ``action='sell'`` debited like a buy — requests the real client refuses before
    it ever opens a socket. The refusal is local, so NOTHING is recorded: no request
    counted, no entry in the transmission log, no order, no money."""
    args = {"ticker": "T", "side": "yes", "price": D("0.42"), "count": 1,
            "client_order_id": "A-0001-B01", **kwargs}
    fake.reset_calls()

    with pytest.raises(ValueError, match=match):
        fake.create_order(**args)

    assert fake.orders_placed == []
    assert fake.calls == {}
    assert fake.get_orders()[0] == []
    assert fake.balance == D("10.0000") and len(fake.balance_ledger()) == 1


def test_unknown_ticker_is_a_404_the_way_the_other_reads_are(fake):
    """FK-1: an unknown ticker used to come back as a graceful no-fill cancel — the one
    answer the exchange cannot give. It is a definite rejection, same shape as
    ``get_market``'s, and it leaves the request in the transmission log: a 404 means the
    request DID reach the exchange (the same split ``reject:`` draws)."""
    with pytest.raises(KalshiAPIError) as ei:
        fake.create_order("NOSUCH", "yes", D("0.42"), 1, "A-0001-B01")

    assert ei.value.status == 404
    assert [o["ticker"] for o in fake.orders_placed] == ["NOSUCH"]  # transmitted
    assert fake.get_orders()[0] == []       # but nothing landed
    assert fake.balance == D("10.0000")


def test_a_scripted_outcome_beats_the_unknown_ticker_404(fake):
    """Scripting an outcome for a ticker declares what the exchange did with the request,
    so ``reject:`` still wins on a ticker with no market behind it."""
    fake.set_order_behavior("GHOST", "reject:400:market closed")
    with pytest.raises(KalshiAPIError) as ei:
        fake.create_order("GHOST", "yes", D("0.42"), 1, "A-0001-B01")
    assert ei.value.status == 400


# ------------------------------------------------ FK-5/FK-6: fills respect the real book
def test_the_default_fill_is_capped_by_the_resting_size(fake):
    """FK-5: an IOC for more than the displayed size used to fill in full — a trade the
    exchange cannot make. It fills what is there, and the rest is canceled."""
    r = fake.create_order("T", "yes", D("0.42"), 15, "A-0001-B01")  # book holds 10

    assert r.filled_count == 10 and r.status == "executed"
    # 10 x 0.42 stake, fee ceil_4dp(0.07 * 10 * 0.42 * 0.58) = ceil(0.17052) = 0.1706
    assert fake.balance == D("10.0000") - D("4.20") - D("0.1706") == D("5.6294")


def test_a_fractional_resting_size_produces_an_organic_partial_fill(fake):
    """The A-0054-B01 shape, arising from the book instead of being scripted: 0.9
    contracts resting against a 1-contract order. The FILL is exact regardless of what
    V11 made of the same size (docs/14 D3: ``Orderbook.best_ask_size``/``ask_depth``
    read it exactly too, but that is a separate concern from this fake's fill rule)."""
    fake.set_book("T", "yes", D("0.15"), D("0.9"))

    r = fake.create_order("T", "yes", D("0.15"), 1, "A-0001-B01")

    assert r.filled_count == D("0.9")
    assert r.fee == D("0.0081")  # ceil_4dp(0.07 * 0.9 * 0.15 * 0.85) = ceil(0.0080325)
    assert fake.balance == D("10.0000") - q4(D("0.9") * D("0.15")) - D("0.0081")
    assert fake.get_fills()[0][0].count == D("0.9")


def test_an_empty_book_side_fills_nothing(fake):
    fake.set_book("T", "yes", D("0.42"), 0)
    r = fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")
    assert r.filled_count == 0 and r.status == "canceled"
    assert fake.balance == D("10.0000")


# -------------------------------------------------------- FK-4: fills carry no coid
def test_served_fills_carry_no_client_order_id(fake):
    """FK-4: the live fills endpoint serves ``order_id`` and no ``client_order_id`` — the
    entire reason attribution is an orders->fills join. Serving one here let code that
    would read ``None`` in production pass in the suite."""
    fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")
    fake.add_personal_fill("T", "yes", 1, D("0.40"), client_order_id="SOMEONE-ELSE")

    fills, _ = fake.get_fills()

    assert [f.client_order_id for f in fills] == [None, None]
    assert all(f.raw.get("client_order_id") is None for f in fills)
    # order_id is what the join has to work from, and it IS served
    assert fills[0].raw["order_id"]


def test_the_join_stamps_the_coid_onto_the_fills_it_returns(fake):
    """...exactly as the real client stamps it after matching order_id — the only place a
    coid and a fill ever meet."""
    fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")

    joined = fake.find_fills_by_client_order_id("A-0001-B01")

    assert [f.client_order_id for f in joined] == ["A-0001-B01"]
    assert fake.get_fills()[0][0].client_order_id is None  # and the read is unchanged


def test_a_served_fill_is_a_copy_not_the_log_entry(fake):
    fake.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")
    served = fake.get_fills()[0][0]
    served.count = D("999")
    assert fake.get_fills()[0][0].count == D("1")


# ------------------------------------------------------------ FK-2: read-side faults
def test_fail_next_makes_the_next_read_raise_exactly_once(fake):
    fake.fail_next("get_balance")

    with pytest.raises(KalshiAPIError) as ei:
        fake.get_balance()
    assert ei.value.status is None  # retries-exhausted shape: no HTTP status

    assert fake.get_balance().dollars == D("10.0000")  # one-shot: the next call is fine


def test_fail_next_takes_the_exception_the_test_wants(fake):
    fake.fail_next("get_market", KalshiAPIError(500, "Internal Server Error", "boom"))
    with pytest.raises(KalshiAPIError) as ei:
        fake.get_market("T")
    assert ei.value.status == 500 and ei.value.body == "boom"


def test_an_injected_failure_still_costs_a_request(fake):
    """It models a request that reached the server and came back wrong, so the request
    counters — the EF-7 guards on settle/reconcile — must still see it."""
    fake.reset_calls()
    fake.fail_next("get_orders")
    with pytest.raises(KalshiAPIError):
        fake.get_orders()
    assert fake.calls["get_orders"] == 1


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_markets", ()), ("get_market", ("T",)), ("get_orderbook", ("T",)),
        ("get_balance", ()), ("get_orders", ()), ("get_fills", ()),
        ("get_settlements", ()), ("get_candlesticks", ("T",)),
    ],
)
def test_every_read_can_be_made_to_fail(fake, method, args):
    """The whole read surface, so no caller's failure branch is untestable by accident."""
    fake.fail_next(method)
    with pytest.raises(KalshiAPIError):
        getattr(fake, method)(*args)


def test_fail_next_refuses_a_method_it_cannot_arm(fake):
    """A fault injector that silently never fires is worse than none at all, so a typo —
    or an order-path method, which has its own scripting — is a ValueError now, not a
    surprise at assertion time."""
    with pytest.raises(ValueError, match="cannot inject"):
        fake.fail_next("get_fils")
    with pytest.raises(ValueError, match="set_order_behavior"):
        fake.fail_next("create_order")


# ---------------------------------------------------------- FK-2: balance enforcement
def test_an_unfundable_order_is_rejected_when_enforcement_is_on(fake):
    fake.enforce_balance = True
    fake.set_balance("0.50")

    with pytest.raises(KalshiAPIError) as ei:
        fake.create_order("T", "yes", D("0.42"), 5, "A-0001-B01")  # 2.10 + fee

    assert ei.value.status == 400 and "insufficient_balance" in (ei.value.body or "")
    assert fake.get_orders()[0] == []    # a 400 is definite: nothing landed
    assert fake.get_fills()[0] == []
    assert fake.balance == D("0.50")     # and no money moved


def test_an_affordable_order_still_fills_under_enforcement(fake):
    fake.enforce_balance = True
    fake.set_balance("1.00")
    r = fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")  # 0.84 + 0.0342
    assert r.filled_count == 2 and fake.balance == D("0.1258")


def test_balance_enforcement_is_off_by_default(fake):
    """Default off, so the deliberate-overspend tests — which need an order the balance
    cannot cover to go through — keep working unchanged."""
    fake.set_balance("0.10")
    r = fake.create_order("T", "yes", D("0.42"), 2, "A-0001-B01")
    assert r.filled_count == 2 and fake.balance < 0


def test_enforce_balance_can_be_set_at_construction():
    f = FakeKalshi(balance="0.10", enforce_balance=True)
    f.add_market("T", title="m", close_time=CLOSE, yes_ask=D("0.42"), yes_ask_size=10)
    with pytest.raises(KalshiAPIError):
        f.create_order("T", "yes", D("0.42"), 1, "A-0001-B01")


# ----------------------------------------------------------- FK-7: the candle window
def _candle(ts: datetime, close="0.42"):
    return {"end_period_ts": int(ts.timestamp()),
            "price": {"close_dollars": close, "open_dollars": close,
                      "high_dollars": close, "low_dollars": close},
            "volume_fp": "10.00", "open_interest_fp": "5.00"}


def test_candles_outside_the_requested_window_are_not_served(fake):
    """FK-7: the whole scripted series came back regardless of the window asked for, so
    any caller that computes a window and trusts the server to honor it was untested."""
    now = datetime.now(UTC)
    fake.set_candles("T", [_candle(now - timedelta(days=10)), _candle(now - timedelta(hours=1))])

    served = fake.get_candlesticks("T", period_minutes=60,
                                   start=now - timedelta(hours=6), end=now)

    assert len(served) == 1
    assert served[0].ts > now - timedelta(hours=6)


def test_the_default_candle_window_is_the_clients_last_72_hours(fake):
    now = datetime.now(UTC)
    fake.set_candles("T", [_candle(now - timedelta(hours=100)), _candle(now - timedelta(hours=2))])

    assert len(fake.get_candlesticks("T")) == 1  # end=now, start=now-72h, as the client


def test_a_window_wider_than_the_server_cap_is_a_400(fake):
    """5000 candles per call, verified live 2026-07-13/14: (end-start)/period > 5000 -> 400."""
    now = datetime.now(UTC)
    fake.set_candles("T", [_candle(now - timedelta(hours=1))])

    with pytest.raises(KalshiAPIError) as ei:
        fake.get_candlesticks("T", period_minutes=1, start=now - timedelta(days=100), end=now)
    assert ei.value.status == 400
    # the same span at a coarser period is inside the cap
    assert fake.get_candlesticks("T", period_minutes=60,
                                 start=now - timedelta(days=100), end=now)


def test_a_missing_required_query_argument_is_a_400(fake):
    """The client drops ``None`` params, so a period that never arrives is the endpoint's
    required-argument 400 (its ``start_ts`` twin is unreachable here — the client always
    derives a start)."""
    fake.set_candles("T", [_candle(datetime.now(UTC))])
    with pytest.raises(KalshiAPIError) as ei:
        fake.get_candlesticks("T", period_minutes=None)
    assert ei.value.status == 400


def test_an_unknown_ticker_404s_before_any_window_check(fake):
    """Mirrors the client, where series derivation fails before the candlesticks request
    is ever sent."""
    with pytest.raises(KalshiAPIError) as ei:
        fake.get_candlesticks("NOPE", period_minutes=None)
    assert ei.value.status == 404


# ------------------------------------------------------ FK-8: nothing vanishes off-page
def test_no_market_vanishes_beyond_the_first_page(fake):
    """FK-8's failure mode, pinned: ``get_markets`` truncated to ``limit`` with no cursor,
    so markets past it disappeared even through ``iter_markets``. With paging on, the
    cursor accounts for every one of them."""
    for i in range(9):
        fake.add_market(f"M{i}", title="m", close_time=CLOSE, yes_ask=D("0.10"), yes_ask_size=1)
    fake.page_size = 2

    seen = [m.ticker for m in fake.iter_markets()]

    assert len(seen) == 10 and len(set(seen)) == 10  # the nine plus T, each exactly once


def test_a_limit_is_a_page_size_not_a_truncation(fake):
    """The other half of FK-8, and the default mode: on the wire ``limit`` IS the page
    size, so asking for two rows must hand back a cursor rather than quietly dropping the
    rest of the board."""
    for i in range(9):
        fake.add_market(f"M{i}", title="m", close_time=CLOSE, yes_ask=D("0.10"), yes_ask_size=1)

    page, cursor = fake.get_markets(limit=2)

    assert len(page) == 2 and cursor is not None
    assert len(list(fake.iter_markets(limit=2))) == 10  # every market, two per request
    assert fake.calls["get_markets"] == 6  # 5 pages of 2, plus the single page above


# ------------------------------------------------------------- BT-3: event_category
def test_event_category_answers_from_the_markets_under_that_event(fake):
    fake.add_market("KXEVT-1-B85", title="m", category="Weather", close_time=CLOSE,
                    yes_ask=D("0.10"), yes_ask_size=1)
    assert fake.event_category("KXEVT-1") == "Weather"


def test_market_payloads_carry_their_event_ticker(fake):
    fake.add_market("KXEVT-1-B85", title="m", category="Weather", close_time=CLOSE,
                    yes_ask=D("0.10"), yes_ask_size=1)
    assert fake.get_market("KXEVT-1-B85").event_ticker == "KXEVT-1"


def test_event_category_404s_for_an_unknown_event_or_returns_none(fake):
    with pytest.raises(KalshiAPIError):
        fake.event_category("KXNOSUCH")
    assert fake.event_category("KXNOSUCH", raise_on_error=False) is None


# ------------------------------------------- fills across several book levels (2026-09-27)
def test_a_levels_fill_charges_each_piece_as_the_live_exchange_did():
    """A-0316-B01 at 2026-09-27T00:25:51Z: 3 YES contracts at a $0.45 limit, filled in four
    pieces as the book was walked. Every figure is the live one: each piece's fee is the
    step it adds to the exact running total rounded up ($0.0081, $0.0057, $0.0032,
    $0.0073), the order reports $0.4043 and $0.0243, the create response's average is the
    truncated $0.1347, and the balance moved by $0.4286."""
    f = FakeKalshi(balance="10.0000", fee_coef=COEF)
    f.add_market("KXLOW", title="t", close_time=CLOSE, yes_ask=D("0.11"), yes_ask_size=50)
    f.set_order_behavior("KXLOW", "levels:1.18@0.11,0.76@0.12,0.41@0.13,0.65@0.20")

    r = f.create_order("KXLOW", "yes", D("0.45"), 3, "A-0316-B01")

    assert r.status == "executed" and r.filled_count == D("3.00")
    assert r.avg_fill_price == D("0.1347") and r.fee == D("0.0243")
    order = next(iter(f.iter_orders()))
    assert reported_cost(order) == D("0.4043") and reported_fee(order) == D("0.0243")
    fills = f.find_fills_by_client_order_id("A-0316-B01")
    assert [(x.count, x.price) for x in fills] == [
        (D("1.18"), D("0.11")), (D("0.76"), D("0.12")),
        (D("0.41"), D("0.13")), (D("0.65"), D("0.20")),
    ]
    assert deltas(f, "fill") == [
        -(D("0.1298") + D("0.0081")), -(D("0.0912") + D("0.0057")),
        -(D("0.0533") + D("0.0032")), -(D("0.1300") + D("0.0073")),
    ]
    assert f.balance == D("10.0000") - D("0.4286")


def test_a_levels_fill_on_the_no_side_reports_the_average_the_live_response_did():
    """A-0320-B01: 3 NO contracts, 2 at $0.83 and 1 at $0.84, for $2.5000 and a $0.0292
    fee. The response quotes the YES average truncated, $0.1666, so the NO average the
    client derives is $0.8334, and 3 x $0.8334 is $2.5002."""
    f = FakeKalshi(balance="10.0000", fee_coef=COEF)
    f.add_market("KXRAIN", title="t", close_time=CLOSE, no_ask=D("0.83"), no_ask_size=50)
    f.set_order_behavior("KXRAIN", "levels:2.00@0.83,1.00@0.84")

    r = f.create_order("KXRAIN", "no", D("0.90"), 3, "A-0320-B01")

    assert r.avg_fill_price == D("0.8334") and r.fee == D("0.0292")
    order = next(iter(f.iter_orders()))
    assert reported_cost(order) == D("2.5000") and reported_fee(order) == D("0.0292")
    assert f.balance == D("10.0000") - D("2.5292")


def test_a_levels_script_cannot_fill_above_the_limit_or_past_the_count():
    f = FakeKalshi(balance="10.0000", fee_coef=COEF)
    f.add_market("KXLOW", title="t", close_time=CLOSE, yes_ask=D("0.11"), yes_ask_size=50)
    f.set_order_behavior("KXLOW", "levels:1.00@0.11,2.00@0.50")
    with pytest.raises(ValueError):
        f.create_order("KXLOW", "yes", D("0.45"), 3, "A-0001-B01")
    f.set_order_behavior("KXLOW", "levels:4.00@0.11")
    with pytest.raises(ValueError):
        f.create_order("KXLOW", "yes", D("0.45"), 3, "A-0001-B02")
