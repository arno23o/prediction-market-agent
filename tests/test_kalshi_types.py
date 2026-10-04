"""Type-parsing tests over the RECORDED live-production fixtures (real market/orderbook
shapes; scrubbed personal payloads). Proves the models parse Kalshi's real fixed-point
payloads and that price fields land as ``Decimal``."""
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from betting_agent.kalshi import (
    Balance,
    Candle,
    ExchangeStatus,
    Fill,
    Market,
    Orderbook,
    OrderResult,
    Settlement,
)
from betting_agent.kalshi.types import parse_count, parse_ts, reported_cost, reported_fee

FIX = Path(__file__).parent / "fixtures" / "recorded"


def load(name):
    return json.loads((FIX / name).read_text())


def test_markets_list_parses_real_shape():
    data = load("markets_list.json")
    markets = [Market.from_api(m) for m in data["markets"]]
    assert len(markets) == 6
    m = markets[0]
    assert isinstance(m.yes_ask, Decimal)
    assert isinstance(m.no_ask, Decimal)
    assert isinstance(m.tick_size, Decimal)
    assert m.tick_size == Decimal("0.01")  # tapered_deci_cent mid-band step
    assert isinstance(m.volume, int) and isinstance(m.open_interest, int)
    assert m.close_time is not None and m.close_time.tzinfo is not None
    assert m.expected_expiration is not None  # from expected_expiration_time
    assert m.category is None  # markets carry no category field
    assert m.event_ticker  # but the parent event ticker is present in raw


def test_market_detail_parses():
    m = Market.from_api(load("market_detail.json")["market"])
    assert m.ticker
    assert isinstance(m.tick_size, Decimal)
    assert isinstance(m.volume, int)
    assert m.status == "active"


def test_settled_market_parses():
    m = Market.from_api(load("market_settled.json")["market"])
    assert m.status == "finalized"
    assert m.raw["result"] == "no"
    assert m.tick_size == Decimal("0.0010")  # deci_cent single-range step


def test_orderbook_ask_derivation_from_real_book():
    ob = Orderbook.from_api(load("orderbook.json"), ticker="X")
    # yes_ask = 1 - best no bid (0.22) = 0.78; size = the exact size at that no bid
    # (198.76, docs/14 D3 — best_ask_size no longer truncates to int)
    assert ob.best_ask("yes") == Decimal("0.78")
    assert ob.best_ask_size("yes") == Decimal("198.76")
    assert isinstance(ob.best_ask_size("yes"), Decimal)
    assert isinstance(ob.best_ask("yes"), Decimal)
    # no_ask = 1 - best yes bid (0.77) = 0.23
    assert ob.best_ask("no") == Decimal("0.23")
    assert ob.best_bid("yes") == Decimal("0.77") and ob.best_bid("no") == Decimal("0.22")


def test_balance_parses_dollar_string():
    b = Balance.from_api(load("balance.json"))
    assert b.dollars == Decimal("10.0000") and isinstance(b.dollars, Decimal)


def test_fills_parse_and_have_no_client_order_id():
    fills = [Fill.from_api(f) for f in load("fills.json")["fills"]]
    assert fills[0].client_order_id is None  # fills endpoint carries no client_order_id
    assert fills[0].side == "yes" and fills[0].price == Decimal("0.4200")
    assert isinstance(fills[0].price, Decimal)
    assert fills[1].side == "no" and fills[1].price == Decimal("0.3300")  # no-side price
    assert fills[0].count == 2 and fills[1].count == 3
    assert fills[0].is_taker is True and fills[1].is_taker is False
    assert isinstance(fills[0].ts, datetime) and fills[0].ts.tzinfo is not None


def test_settlements_parse():
    setts = [Settlement.from_api(s) for s in load("settlements.json")["settlements"]]
    assert setts[0].market_result == "yes" and setts[1].market_result == "no"
    assert setts[0].ticker == "KXTEST-26JUL08-A"
    assert setts[0].ts is not None and setts[0].ts.tzinfo is not None


def test_settlement_revenue_is_cents_in_the_payload_and_dollars_on_the_model():
    """docs/16 §5. ``revenue: 200`` on a 2-contract YES position that won is $2.00 — the
    fixture is the evidence that the field is CENTS and a TOTAL, not a per-contract value.
    ``value`` duplicates it (200, not 100), which is why nothing reads ``value``."""
    setts = [Settlement.from_api(s) for s in load("settlements.json")["settlements"]]
    assert setts[0].revenue == Decimal("2")
    assert setts[0].yes_count == Decimal("2.00") and setts[0].no_count == Decimal("0.00")
    assert setts[0].raw["revenue"] == 200 and setts[0].raw["value"] == 200
    assert setts[1].revenue == Decimal("0")


def test_settlement_payout_for_prices_a_scalar_position_per_contract():
    """The live A-0097 record: ``revenue: 82`` on ``no_count_fp: "1.00"`` is $0.82 for one
    NO contract — and $0.82 a contract for however many of them we held."""
    s = Settlement.from_api({
        "ticker": "KXNPBTOTAL-26AUG130500HIRYAK-12", "market_result": "scalar",
        "revenue": 82, "value": 82, "yes_count_fp": "0.00", "no_count_fp": "1.00",
        "settled_time": "2026-08-15T14:48:49Z",
    })
    assert s.market_result == "scalar" and s.revenue == Decimal("0.82")
    assert s.payout_for("no", 1) == Decimal("0.82")
    assert s.payout_for("yes", 1) is None      # we held no YES: nothing to divide by

    bigger = Settlement.from_api({
        "ticker": "T", "market_result": "scalar", "revenue": 164, "no_count_fp": "2.00",
        "yes_count_fp": "0.00", "settled_time": "2026-08-15T14:48:49Z",
    })
    assert bigger.payout_for("no", 1) == Decimal("0.82")     # per contract, from the total
    assert bigger.payout_for("no", 2) == Decimal("1.64")


@pytest.mark.parametrize("raw, why", [
    ({"ticker": "T", "market_result": "scalar", "no_count_fp": "1.00"},
     "no revenue on the row"),
    ({"ticker": "T", "market_result": "scalar", "revenue": 82, "no_count_fp": "0.00"},
     "no position on our own side"),
    ({"ticker": "T", "market_result": "scalar", "revenue": 100, "no_count_fp": "1.00",
      "yes_count_fp": "1.00"},
     "a nonzero position on BOTH sides: the record netted them"),
])
def test_settlement_payout_for_refuses_rather_than_guesses(raw, why):
    """Each refusal is a case where a number could be invented and must not be — the
    caller's answer is 'leave it for a later pass / for a human', never a substitute."""
    assert Settlement.from_api(raw).payout_for("no", 1) is None, why


def test_legacy_cent_integer_fallback():
    # tolerate legacy cent integers where dollar strings are absent
    m = Market.from_api({"ticker": "X", "yes_ask": 42, "volume": 100, "open_interest": 7})
    assert m.yes_ask == Decimal("0.42") and m.volume == 100 and m.open_interest == 7
    assert Balance.from_api({"balance": 1000}).dollars == Decimal("10")


def test_fractional_size_stays_exact_decimal():
    """docs/14 D3: ``best_ask_size`` used to truncate a fractional top-of-book to int
    (0.90 -> 0), which is exactly the bug that flipped A-0020's sign — V11 read a real
    0.90-contract book as size-0 and rejected the bet outright. It stays an exact
    Decimal now, like a fill count (KC-2)."""
    ob = Orderbook.from_api(
        {"orderbook_fp": {"no_dollars": [["0.2200", "0.90"]], "yes_dollars": []}}
    )
    assert ob.best_ask("yes") == Decimal("0.78")
    assert ob.best_ask_size("yes") == Decimal("0.90")
    assert isinstance(ob.best_ask_size("yes"), Decimal)


def test_empty_book_side_yields_none():
    ob = Orderbook.from_api({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert ob.best_ask("yes") is None
    assert ob.best_ask_size("yes") is None
    assert ob.best_bid("no") is None


# --------------------------------------------------------------- ask_depth (docs/14 D3)
def test_ask_depth_sums_every_level_not_just_the_top():
    """The A-0020 shape: 0.96 contracts at the best price, 115 more one level behind.
    ``best_ask_size`` alone only sees the top (0.96, itself < 1 contract); V11 needs the
    full depth (115.96) to correctly call this book liquid enough for a 1-contract
    order."""
    ob = Orderbook.from_api({"orderbook_fp": {
        "no_dollars": [["0.6000", "0.96"], ["0.5900", "115"]], "yes_dollars": [],
    }})
    assert ob.best_ask_size("yes") == Decimal("0.96")  # top level alone, unchanged
    assert ob.ask_depth("yes") == Decimal("115.96")     # every level, summed


def test_ask_depth_below_one_contract():
    ob = Orderbook.from_api(
        {"orderbook_fp": {"no_dollars": [["0.6000", "0.50"]], "yes_dollars": []}}
    )
    assert ob.ask_depth("yes") == Decimal("0.50")


def test_ask_depth_boundary_exactly_one():
    ob = Orderbook.from_api(
        {"orderbook_fp": {"no_dollars": [["0.6000", "1.00"]], "yes_dollars": []}}
    )
    assert ob.ask_depth("yes") == Decimal("1.00")


def test_ask_depth_empty_book_is_zero_not_none():
    """Unlike ``best_ask_size``, ``ask_depth`` never returns ``None`` — V11 compares it
    against 1 directly, and "no levels" is just the depth-0 case of "not enough"."""
    ob = Orderbook.from_api({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert ob.ask_depth("yes") == Decimal(0)
    assert ob.ask_depth("no") == Decimal(0)


def test_ask_depth_reads_the_correct_opposite_side():
    ob = Orderbook.from_api({"orderbook_fp": {
        "no_dollars": [["0.6000", "10"]],   # prices a YES ask
        "yes_dollars": [["0.7000", "20"]],  # prices a NO ask
    }})
    assert ob.ask_depth("yes") == Decimal("10")
    assert ob.ask_depth("no") == Decimal("20")


def test_candlesticks_parse_real_shape():
    data = load("candlesticks.json")
    candles = [Candle.from_api(c) for c in data["candlesticks"]]
    assert candles, "recorded fixture should carry candles"
    c = candles[0]
    # YES-side OHLC land as Decimal (parsed from the price sub-object *_dollars strings)
    assert isinstance(c.open, Decimal) and isinstance(c.close, Decimal)
    assert isinstance(c.high, Decimal) and isinstance(c.low, Decimal)
    assert c.low <= c.high  # OHLC internal consistency
    # volume / open interest come from *_fp fixed-point strings, truncated to int
    assert isinstance(c.volume, int) and isinstance(c.open_interest, int)
    # ts derives from end_period_ts (epoch seconds) and is tz-aware UTC
    assert isinstance(c.ts, datetime) and c.ts.tzinfo is not None
    # ascending (oldest-first) server order is preserved
    ts = [c.ts for c in candles]
    assert ts == sorted(ts)
    # full payload retained (mean/previous + yes_bid/yes_ask OHLC live only in raw)
    assert "yes_ask" in c.raw and "yes_bid" in c.raw


def test_candle_no_trade_period_yields_none_ohlc():
    # a period with no trades: price sub-object absent -> OHLC None, counts still parse
    c = Candle.from_api({"end_period_ts": 1783753200, "volume_fp": "0.00",
                         "open_interest_fp": "12.00"})
    assert c.open is None and c.high is None and c.low is None and c.close is None
    assert c.volume == 0 and c.open_interest == 12
    assert c.ts is not None and c.ts.tzinfo is not None


# ------------------------------------------------------------------ exact counts (KC-2)
def test_fill_count_keeps_fractional_precision():
    """The live A-0054-B01 shape: one order, three fractional fills summing to exactly 1.

    ``int()`` on each of these gives 0, which is how a $0.15 position became a phantom
    no-fill and HALTed the system on 2026-08-02.
    """
    fills = [
        Fill.from_api({"ticker": "T", "side": "yes", "count_fp": c,
                       "yes_price_dollars": "0.1500"})
        for c in ("0.28", "0.34", "0.38")
    ]
    assert [f.count for f in fills] == [Decimal("0.28"), Decimal("0.34"), Decimal("0.38")]
    assert all(isinstance(f.count, Decimal) for f in fills)
    assert sum(f.count for f in fills) == Decimal("1.00") == 1


def test_fill_count_falls_back_to_the_legacy_plain_field():
    f = Fill.from_api({"ticker": "T", "side": "yes", "count": 3, "yes_price_dollars": "0.20"})
    assert f.count == Decimal("3") and isinstance(f.count, Decimal)
    assert Fill.from_api({"ticker": "T"}).count == Decimal(0)


def test_order_result_filled_count_is_exact():
    """KC-5 deleted ``OrderResult.from_api`` (production-dead, and its side fallback
    predated V2's single YES-quoted book), so the exact-count property is pinned on the
    model itself; the parse that matters is ``KalshiClient.create_order``'s, covered in
    test_kalshi_client.py."""
    r = OrderResult(order_id="OID", client_order_id="A-0001-B01", status="executed",
                    filled_count=parse_count({"fill_count_fp": "0.90"}, "fill_count"))
    assert r.filled_count == Decimal("0.90")
    assert isinstance(r.filled_count, Decimal)


def test_order_result_has_no_from_api_parser():
    """KC-5: the parser is gone for good. A test was its only caller, which is precisely
    how a stale parser survives a wire-format migration unnoticed."""
    assert not hasattr(OrderResult, "from_api")


def test_display_counts_still_truncate_to_int():
    """volume / open interest keep their int typing — only money-bearing counts changed."""
    m = Market.from_api({"ticker": "X", "volume_fp": "12.75", "open_interest_fp": "3.99"})
    assert m.volume == 12 and m.open_interest == 3
    assert isinstance(m.volume, int) and isinstance(m.open_interest, int)


# ------------------------------------------------------- KC-8: the exchange-status gate
# ``ExchangeStatus.active`` is a trading GATE: every attempt asks it before placing, so a
# flag combination that reads the wrong way either bets into a closed exchange or skips a
# night for nothing. Only the both-true case was covered; the rule is "every flag that is
# present must be true, and no flags at all means NOT active".
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"exchange_active": True, "trading_active": True}, True),
        ({"exchange_active": True, "trading_active": False}, False),
        ({"exchange_active": False, "trading_active": True}, False),
        ({"exchange_active": False, "trading_active": False}, False),
        ({"exchange_active": True}, True),               # partial payload, all present true
        ({"trading_active": True}, True),
        ({"exchange_active": False}, False),
        ({"exchange_active": True, "trading_active": None}, True),   # None = absent
        ({"exchange_active": None, "trading_active": None}, False),  # nothing to trust
        ({}, False),                                     # an unreadable status is closed
        ({"some_other_flag": True}, False),              # unknown fields grant nothing
    ],
)
def test_exchange_status_flag_matrix(payload, expected):
    assert ExchangeStatus.from_api(payload).active is expected


def test_exchange_status_keeps_the_raw_payload():
    s = ExchangeStatus.from_api({"exchange_active": True, "trading_active": True, "x": 1})
    assert s.raw["x"] == 1


# ------------------------------------------------------------------ KC-8: parse_ts edges
def test_parse_ts_reads_iso_z_offset_and_naive_as_utc():
    expected = datetime(2026, 7, 6, 15, 30, tzinfo=UTC)
    assert parse_ts("2026-07-06T15:30:00Z") == expected
    assert parse_ts("2026-07-06T17:30:00+02:00") == expected      # normalized to UTC
    assert parse_ts("2026-07-06T15:30:00") == expected            # naive is assumed UTC
    assert parse_ts("2026-07-06T15:30:00Z").tzinfo is not None


def test_parse_ts_distinguishes_epoch_seconds_from_milliseconds():
    """Kalshi serves both (``end_period_ts`` in seconds, ``ts_ms`` in milliseconds), and
    reading one as the other lands 50,000 years out — a settlement lag test would pass
    forever."""
    assert parse_ts(1783753200) == datetime(2026, 7, 11, 7, 0, tzinfo=UTC)
    assert parse_ts(1783753200000) == datetime(2026, 7, 11, 7, 0, tzinfo=UTC)
    assert parse_ts(1783753200.5).microsecond == 500000  # float seconds keep sub-second


@pytest.mark.parametrize("value", [None, "", "   "])
def test_parse_ts_absent_values_are_none(value):
    assert parse_ts(value) is None


def test_parse_ts_refuses_to_read_a_bool_as_an_epoch():
    """``bool`` is an ``int`` subclass, so ``True`` would otherwise parse as 1970-01-01 —
    a timestamp that silently passes every "is it in the window" comparison."""
    assert parse_ts(True) is None
    assert parse_ts(False) is None


def test_parse_ts_raises_on_garbage_rather_than_inventing_a_time():
    """Pinned deliberately: callers that must tolerate junk catch ``ValueError`` and
    RETAIN the row (get_orders/get_fills). A silent ``None`` here would look like "no
    timestamp" and let a filter drop real account activity."""
    with pytest.raises(ValueError):
        parse_ts("not a timestamp")


# ------------------------- 2026-09-27: the order listing's totals are the receipt
def test_reported_cost_and_fee_read_the_order_listings_totals():
    """The live A-0316-B01 order as ``GET /portfolio/orders`` served it on 2026-09-27: 3
    contracts bought in four pieces between $0.11 and $0.20. The cost and fee are the
    exchange's own totals, they equal the sums over the order's fills, and they are what
    the balance moved by. No four-place average multiplied back by 3 gives the cost."""
    order = {
        "fill_count_fp": "3.00", "yes_price_dollars": "0.4500",
        "taker_fill_cost_dollars": "0.404300", "maker_fill_cost_dollars": "0.000000",
        "taker_fees_dollars": "0.024300", "maker_fees_dollars": "0.000000",
    }
    assert reported_cost(order) == Decimal("0.4043")
    assert reported_fee(order) == Decimal("0.0243")
    assert Decimal("0.1347") * 3 != reported_cost(order)   # the create response's average
    assert Decimal("0.1348") * 3 != reported_cost(order)   # the fills' average, rounded


def test_reported_cost_is_none_when_the_payload_carries_no_totals():
    """The only live create response on record, the canary's of 2026-08-01, carries
    neither total. Nothing is guessed from its per-contract averages."""
    canary_response = {"average_fee_paid": "0.0041", "average_fill_price": "0.0620",
                       "fill_count": "1.00", "remaining_count": "0.00"}
    assert reported_cost(canary_response) is None
    assert reported_fee(canary_response) is None
    assert reported_cost({"maker_fill_cost_dollars": "0.1200"}) == Decimal("0.1200")
