"""Ticket parse and validation matrix (docs/22 sections 5.4 and 5.5).

Nine codes, one case each, plus the shape rules of the ticket the codes read.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from betting_agent.config import Settings
from betting_agent.harness.validate import (
    REJECT_REASONS,
    BetSpec,
    ParsedTicket,
    parse_ticket,
    reason_for_code,
    validate_ticket,
)
from betting_agent.kalshi.types import Market, Orderbook

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)

GOOD_EDGE = "\n".join(
    ["## Markets", "m",
     "## Why this is profitable", "e",
     "## Why the opportunity exists and persists", "w"]
)
GOOD_HYP = "\n".join(
    ["## If we're right", "r", "## If we're wrong", "x", "## Kill criteria", "k"]
)

_CODES = ("V01", "V02", "V03", "V04", "V05", "V07", "V10", "V11", "V16")


# --------------------------------------------------------------------------- helpers
def _settings(tmp_path, **over):
    s = Settings()
    s._root = tmp_path
    for key, val in over.items():
        obj = s
        parts = key.split(".")
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], val)
    return s


def _market(ticker, *, status="active", close_h=1, category=None, tick_size="0.01",
            price_ranges=None, title="T"):
    close = NOW + timedelta(hours=close_h)
    raw = {"ticker": ticker}
    if price_ranges is not None:
        raw["price_ranges"] = price_ranges
    return Market(
        ticker=ticker, title=title, category=category, status=status,
        close_time=close, expected_expiration=close, tick_size=D(tick_size), raw=raw,
    )


def _book(*, yes_ask=None, yes_size=0, no_ask=None, no_size=0):
    # asks encoded as opposite-side bids so best_ask(side) reproduces the configured ask
    yes_levels = [(D(1) - D(no_ask), D(no_size))] if no_ask is not None else []
    no_levels = [(D(1) - D(yes_ask), D(yes_size))] if yes_ask is not None else []
    return Orderbook(yes_levels=yes_levels, no_levels=no_levels)


def _multi_book(side, levels):
    """A book with explicit multi-level depth for buying ``side`` (docs/14 D3).

    ``levels`` is ``[(ask_price, size), ...]``, the derived ask ladder a real order
    would walk; encoded as opposite-side bids (same convention as :func:`_book`) so
    ``ask_depth(side)`` sums exactly what was asked for, across every level.
    """
    opp_levels = [(D(1) - D(p), D(s)) for p, s in levels]
    if side == "yes":
        return Orderbook(yes_levels=[], no_levels=opp_levels)
    return Orderbook(yes_levels=opp_levels, no_levels=[])


def _bet(idx, ticker, *, side="yes", limit="0.4000", contracts=1, resolution_event=None):
    return BetSpec(idx, ticker, side, D(limit), contracts, "why", resolution_event)


def _parsed(bets, *, attempt="A-0001"):
    return ParsedTicket(attempt, bets, GOOD_EDGE, GOOD_HYP, "m", [])


def _fetchers(markets, books):
    return (lambda t: markets.get(t)), (lambda t: books.get(t))


def _validate(parsed, markets, books, settings):
    mf, bf = _fetchers(markets, books)
    return validate_ticket(parsed, mf, bf, settings, NOW)


def _by_index(outcome):
    return {vb.spec.ticket_index: vb for vb in outcome.bets}


def _write_ticket(tmp_path, bets_json, *, edge=GOOD_EDGE, hyp=GOOD_HYP):
    d = tmp_path / "ticket"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bets.json").write_text(bets_json)
    (d / "edge_claim.md").write_text(edge)
    (d / "hypothesis.md").write_text(hyp)
    return d


def _one_bet(**over):
    bet = {"ticker": "A", "side": "yes", "limit_price": "0.4200",
           "contracts": 1, "rationale": "r"}
    bet.update(over)
    return json.dumps({"attempt": "A-0007", "bets": [bet]})


# --------------------------------------------------------------------------- the vocabulary
def test_there_are_exactly_nine_codes_and_each_has_a_reason():
    assert set(REJECT_REASONS) == set(_CODES)
    assert all(REJECT_REASONS[c].strip() for c in _CODES)
    assert reason_for_code("V04") == REJECT_REASONS["V04"]
    assert reason_for_code(None) is None
    assert reason_for_code("V99") is None


# --------------------------------------------------------------------------- parse
def test_parse_valid(tmp_path):
    parsed = parse_ticket(_write_ticket(tmp_path, _one_bet(contracts=3)))
    assert parsed.whole_ticket_errors == []
    assert parsed.error_detail == []
    assert parsed.ok is True
    assert parsed.attempt_id == "A-0007"
    assert len(parsed.bets) == 1
    assert parsed.bets[0].ticker == "A"
    assert parsed.bets[0].limit_price == D("0.4200")
    assert parsed.bets[0].contracts == 3
    assert parsed.bets[0].ticket_index == 1


def test_a_manifest_is_stored_when_present_and_never_required(tmp_path):
    """docs/22 section 5.4: MANIFEST.md is neither required nor mentioned."""
    d = _write_ticket(tmp_path, _one_bet())
    assert parse_ticket(d).manifest_md is None      # absent: still a clean ticket
    assert parse_ticket(d).ok is True
    (d / "MANIFEST.md").write_text("whatever")
    assert parse_ticket(d).manifest_md == "whatever"


# --------------------------------------------------------------------------- V01
@pytest.mark.parametrize(
    "bets_json,expect",
    [
        ("{not valid json", "not valid JSON"),
        (_one_bet(surprise=1), "bets[0] unexpected ['surprise']"),
        ('{"attempt": "A-0007", "surprise": 1, "bets": []}',
         "unexpected top-level keys ['surprise']"),
        (json.dumps({"attempt": "A-0007", "bets": [
            {"ticker": "A", "side": "yes", "limit_price": "0.4200", "contracts": 1}]}),
         "bets[0] missing ['rationale']"),
        (_one_bet(limit_price="0.42"), "bets[0] bad limit_price"),
        (_one_bet(limit_price=0.42), "bets[0] bad limit_price"),
        (_one_bet(side="maybe"), "bets[0] bad side"),
        (_one_bet(ticker=""), "bets[0] bad ticker"),
        (_one_bet(rationale="x" * 401), "bets[0] bad rationale"),
        (_one_bet(resolution_event=7), "bets[0] bad resolution_event"),
        (_one_bet(resolution_event=""), "bets[0] bad resolution_event"),
        (_one_bet(resolution_event="x" * 81), "bets[0] bad resolution_event"),
        ('{"bets": []}', "missing attempt"),
        ('{"attempt": "nope", "bets": []}', "bad attempt"),
        ('{"attempt": "A-0007"}', "missing bets"),
        ('{"attempt": "A-0007", "bets": {}}', "bets not an array"),
    ],
)
def test_v01_off_shape_tickets(tmp_path, bets_json, expect):
    parsed = parse_ticket(_write_ticket(tmp_path, bets_json))
    assert parsed.whole_ticket_errors == ["V01"]
    assert parsed.ok is False
    assert any(expect in line for line in parsed.error_detail)


def test_v01_group_and_candidate_keys_are_gone_from_the_ticket(tmp_path):
    """The structural schema and the two-loop declaration are not ticket shapes any more,
    so a ticket carrying either is off-shape like any other unknown key."""
    parsed = parse_ticket(_write_ticket(tmp_path, json.dumps({
        "attempt": "A-0007", "edge_class": "structural",
        "groups": [{"id": "g1", "legs": ["A", "B"], "scenarios": []}],
        "candidates": [{"id": "C1", "rank": 1}], "chosen": "C1",
        "bets": [{"ticker": "A", "side": "yes", "limit_price": "0.4200",
                  "contracts": 1, "rationale": "r", "group": "g1"}],
    })))
    assert parsed.whole_ticket_errors == ["V01"]
    assert any("unexpected top-level keys" in line for line in parsed.error_detail)
    assert any("bets[0] unexpected ['group']" in line for line in parsed.error_detail)


def test_v01_model_prob_is_not_a_ticket_field(tmp_path):
    parsed = parse_ticket(_write_ticket(tmp_path, _one_bet(model_prob="0.5500")))
    assert parsed.whole_ticket_errors == ["V01"]
    assert any("bets[0] unexpected ['model_prob']" in line for line in parsed.error_detail)


def test_v01_too_many_bets(tmp_path):
    bets = [{"ticker": f"T{i:02d}", "side": "yes", "limit_price": "0.4000",
             "contracts": 1, "rationale": "r"} for i in range(51)]
    parsed = parse_ticket(_write_ticket(tmp_path, json.dumps(
        {"attempt": "A-0007", "bets": bets}
    )))
    assert parsed.whole_ticket_errors == ["V01"]
    assert any("too many bets (>50)" in line for line in parsed.error_detail)


def test_v01_missing_bets_json(tmp_path):
    d = _write_ticket(tmp_path, _one_bet())
    (d / "bets.json").unlink()
    parsed = parse_ticket(d)
    assert parsed.whole_ticket_errors == ["V01"]
    assert "missing or unreadable" in parsed.error_detail[0]


def test_v01_short_circuits_validation(tmp_path):
    parsed = parse_ticket(_write_ticket(tmp_path, "{not valid json"))
    out = _validate(parsed, {}, {}, _settings(tmp_path))
    assert out.ticket_valid is False
    assert out.whole_ticket_error == "V01"
    assert out.bets == []


# --------------------------------------------------------------------------- V02
@pytest.mark.parametrize("heading", [
    "## Markets", "## Why this is profitable", "## Why the opportunity exists and persists",
])
def test_v02_missing_edge_heading(tmp_path, heading):
    edge = GOOD_EDGE.replace(heading, "## something else")
    parsed = parse_ticket(_write_ticket(tmp_path, _one_bet(), edge=edge))
    assert parsed.whole_ticket_errors == ["V02"]
    assert any(heading in line for line in parsed.error_detail)


@pytest.mark.parametrize("heading", ["## If we're right", "## If we're wrong", "## Kill criteria"])
def test_v02_missing_hypothesis_heading(tmp_path, heading):
    hyp = GOOD_HYP.replace(heading, "## something else")
    parsed = parse_ticket(_write_ticket(tmp_path, _one_bet(), hyp=hyp))
    assert parsed.whole_ticket_errors == ["V02"]
    assert any(heading in line for line in parsed.error_detail)


def test_v02_the_old_headings_no_longer_satisfy_the_rule(tmp_path):
    old = "\n".join(["## Markets", "m", "## The edge", "e",
                     "## Why it exists and persists", "w", "## Edge type tags", "t"])
    parsed = parse_ticket(_write_ticket(tmp_path, _one_bet(), edge=old))
    assert parsed.whole_ticket_errors == ["V02"]
    out = _validate(parsed, {}, {}, _settings(tmp_path))
    assert out.whole_ticket_error == "V02"


def test_v02_detail_names_only_what_is_missing(tmp_path):
    parsed = parse_ticket(_write_ticket(
        tmp_path, _one_bet(), edge=GOOD_EDGE.replace("## Markets", "### Markets")
    ))
    assert parsed.whole_ticket_errors == ["V02"]
    assert any("## Markets" in m for m in parsed.error_detail)
    assert not any("## Why this is profitable" in m for m in parsed.error_detail)


# --------------------------------------------------------------------------- the leg codes
def test_each_leg_code_fires_on_its_own_case_and_nothing_else(tmp_path):
    """One ticket, one leg per per-leg code, plus a clean leg that must survive."""
    markets = {
        "A": _market("A"),
        "FAR": _market("FAR", close_h=200),
        "TICK": _market("TICK", tick_size="0.05"),
        "SIZE": _market("SIZE"),
        "THIN": _market("THIN"),
    }
    deep = _book(yes_ask="0.40", yes_size=50)
    books = {t: deep for t in ("A", "FAR", "TICK", "SIZE")}
    books["THIN"] = _book(yes_ask="0.4000", yes_size="0.5")
    bets = [
        _bet(1, "A"),                          # clean
        _bet(2, "A"),                          # V03 duplicate ticker
        _bet(3, "NOPE"),                       # V04 no such market
        _bet(4, "FAR"),                        # V05 outside the window
        _bet(5, "TICK", limit="0.4200"),       # V07 off the tick band
        _bet(6, "SIZE", contracts=9),          # V10 out of range
        _bet(7, "THIN"),                       # V11 not enough depth
    ]
    out = _validate(_parsed(bets), markets, books, _settings(tmp_path))
    by = _by_index(out)
    assert by[1].status == "validated" and by[1].reject_code is None
    assert {i: by[i].reject_code for i in range(2, 8)} == {
        2: "V03", 3: "V04", 4: "V05", 5: "V07", 6: "V10", 7: "V11",
    }
    assert all(by[i].status == "rejected" for i in range(2, 8))


def test_v03_the_later_of_two_legs_on_one_ticker_loses(tmp_path):
    """The second leg is good in every other way: its ticker is the only thing wrong."""
    markets = {"A": _market("A")}
    books = {"A": _book(yes_ask="0.40", yes_size=50)}

    by = _by_index(_validate(
        _parsed([_bet(1, "A"), _bet(2, "A", limit="0.4200")]), markets, books,
        _settings(tmp_path),
    ))

    assert by[1].status == "validated" and by[1].reject_code is None
    assert by[2].status == "rejected" and by[2].reject_code == "V03"
    assert reason_for_code(by[2].reject_code) == REJECT_REASONS["V03"]


def test_v04_finalized_market(tmp_path):
    markets = {"A": _market("A", status="finalized")}
    books = {"A": _book(yes_ask="0.40", yes_size=50)}
    out = _validate(_parsed([_bet(1, "A")]), markets, books, _settings(tmp_path))
    assert out.bets[0].reject_code == "V04"


def test_v05_both_times_none(tmp_path):
    m = Market(ticker="A", status="active", close_time=None, expected_expiration=None)
    books = {"A": _book(yes_ask="0.40", yes_size=50)}
    out = _validate(_parsed([_bet(1, "A")]), {"A": m}, books, _settings(tmp_path))
    assert out.bets[0].reject_code == "V05"


def test_v05_uses_the_configured_window(tmp_path):
    """120 hours in the rebuilt config; the window is read, not hardcoded."""
    markets = {"A": _market("A", close_h=100)}
    books = {"A": _book(yes_ask="0.40", yes_size=50)}
    s = _settings(tmp_path, **{"limits.max_resolve_hours": 120})
    assert _validate(_parsed([_bet(1, "A")]), markets, books, s).bets[0].status == "validated"
    s = _settings(tmp_path, **{"limits.max_resolve_hours": 72})
    assert _validate(_parsed([_bet(1, "A")]), markets, books, s).bets[0].reject_code == "V05"


def test_v07_price_below_bound(tmp_path):
    markets = {"A": _market("A")}
    books = {"A": _book(yes_ask="0.40", yes_size=50)}
    out = _validate(_parsed([_bet(1, "A", limit="0.0050")]), markets, books, _settings(tmp_path))
    assert out.bets[0].reject_code == "V07"


def test_v07_fine_band_uses_price_ranges(tmp_path):
    # tick_size (0.50 band) is coarse 0.05; a fine band near 0 allows 0.01 steps.
    ranges = [
        {"start": "0.0000", "end": "0.1000", "step": "0.0100"},
        {"start": "0.1000", "end": "1.0000", "step": "0.0500"},
    ]
    markets = {
        "A": _market("A", tick_size="0.05", price_ranges=ranges),
        "B": _market("B", tick_size="0.05", price_ranges=ranges),
    }
    books = {"A": _book(yes_ask="0.03", yes_size=50), "B": _book(yes_ask="0.03", yes_size=50)}
    parsed = _parsed([
        _bet(1, "A", limit="0.0300"),  # aligned to fine 0.01 (misaligned to 0.05) -> passes
        _bet(2, "B", limit="0.0250"),  # misaligned to fine 0.01 -> V07
    ])
    out = _validate(parsed, markets, books, _settings(tmp_path))
    by = _by_index(out)
    assert by[1].status == "validated"  # proves the band schedule (not tick_size) was used
    assert by[2].reject_code == "V07"


# --------------------------------------------------------------------------- contracts (V10/V01)
def test_contracts_comes_from_the_ticket(tmp_path):
    markets = {"A": _market("A"), "B": _market("B")}
    books = {t: _book(yes_ask="0.40", yes_size=50) for t in ("A", "B")}
    out = _validate(
        _parsed([_bet(1, "A", contracts=1), _bet(2, "B", contracts=3)]),
        markets, books, _settings(tmp_path),
    )
    by = _by_index(out)
    assert by[1].contracts == D(1)
    assert by[2].contracts == D(3)


@pytest.mark.parametrize("contracts", [0, -1, 4, 99])
def test_v10_a_whole_number_outside_the_range(tmp_path, contracts):
    markets = {"A": _market("A")}
    books = {"A": _book(yes_ask="0.40", yes_size=50)}
    out = _validate(
        _parsed([_bet(1, "A", contracts=contracts)]), markets, books, _settings(tmp_path)
    )
    assert out.bets[0].reject_code == "V10"
    assert out.bets[0].contracts is None


def test_v10_follows_the_configured_maximum(tmp_path):
    markets = {"A": _market("A")}
    books = {"A": _book(yes_ask="0.40", yes_size=50)}
    bets = _parsed([_bet(1, "A", contracts=4)])
    s = _settings(tmp_path, **{"stakes.max_contracts_per_bet": 4})
    assert _validate(bets, markets, books, s).bets[0].status == "validated"
    s = _settings(tmp_path, **{"stakes.max_contracts_per_bet": 3})
    assert _validate(bets, markets, books, s).bets[0].reject_code == "V10"


@pytest.mark.parametrize("value", ["2", 2.0, True, None, [2]])
def test_v01_contracts_that_are_not_whole_numbers(tmp_path, value):
    """A wrong type is a malformed ticket (V01); a whole number out of range is the leg's
    own failure (V10). ``true`` is a bool, which Python calls an int and the ticket does
    not."""
    parsed = parse_ticket(_write_ticket(tmp_path, _one_bet(contracts=value)))
    assert parsed.whole_ticket_errors == ["V01"]
    assert any("bets[0] bad contracts" in line for line in parsed.error_detail)


def test_v01_contracts_missing_entirely(tmp_path):
    parsed = parse_ticket(_write_ticket(tmp_path, json.dumps({
        "attempt": "A-0007",
        "bets": [{"ticker": "A", "side": "yes", "limit_price": "0.4200", "rationale": "r"}],
    })))
    assert parsed.whole_ticket_errors == ["V01"]
    assert any("missing ['contracts']" in line for line in parsed.error_detail)


# --------------------------------------------------------------------------- V11 depth
def test_v11_book_fetch_none(tmp_path):
    markets = {"A": _market("A")}
    out = _validate(_parsed([_bet(1, "A")]), markets, {}, _settings(tmp_path))
    assert out.bets[0].reject_code == "V11"


def test_v11_empty_on_side(tmp_path):
    markets = {"A": _market("A")}
    books = {"A": _book(no_ask="0.40", no_size=50)}  # only the NO side has an ask
    out = _validate(_parsed([_bet(1, "A", side="yes")]), markets, books, _settings(tmp_path))
    assert out.bets[0].reject_code == "V11"


def test_v11_depth_is_measured_against_the_legs_own_size(tmp_path):
    """docs/22 section 5.5: a three-contract bet on a two-contract book is refused before
    it is sent, rather than partially filled."""
    markets = {"A": _market("A")}
    books = {"A": _book(yes_ask="0.4000", yes_size="2")}
    s = _settings(tmp_path)
    assert _validate(_parsed([_bet(1, "A", contracts=2)]), markets, books, s) \
        .bets[0].status == "validated"
    out = _validate(_parsed([_bet(1, "A", contracts=3)]), markets, books, s)
    assert out.bets[0].reject_code == "V11"


def test_v11_fractional_top_with_depth_behind_passes(tmp_path):
    """docs/14 D3, the A-0020 regression: a fractional top-of-book (0.96 contracts) used
    to int-truncate to size 0 and reject the bet outright, even with 115 more contracts
    resting one level behind it. ``ask_depth`` is an exact Decimal sum across levels, so
    the real (115.96-contract) depth passes even for a 3-contract bet."""
    markets = {"A": _market("A")}
    books = {"A": _multi_book("yes", [("0.4000", "0.96"), ("0.4100", "115")])}
    out = _validate(
        _parsed([_bet(1, "A", limit="0.4200", contracts=3)]), markets, books,
        _settings(tmp_path),
    )
    assert out.bets[0].status == "validated"


def test_v11_thin_total_depth_rejects(tmp_path):
    """0.5 total contracts is a real, non-empty, non-truncated read of the book, and still
    not enough to fill a 1-contract order."""
    markets = {"A": _market("A")}
    books = {"A": _book(yes_ask="0.4000", yes_size="0.5")}
    out = _validate(_parsed([_bet(1, "A", limit="0.4200")]), markets, books, _settings(tmp_path))
    assert out.bets[0].reject_code == "V11"


def test_v11_boundary_exactly_enough_depth_passes(tmp_path):
    markets = {"A": _market("A")}
    books = {"A": _book(yes_ask="0.4000", yes_size="1.0")}
    out = _validate(_parsed([_bet(1, "A", limit="0.4200")]), markets, books, _settings(tmp_path))
    assert out.bets[0].status == "validated"


def test_v11_does_not_gate_on_the_bets_own_limit_price(tmp_path):
    """Deliberate (docs/14 D3, see ``Orderbook.ask_depth``'s docstring): V11 checks that
    the book has the depth this bet wants, not whether THIS bet's limit crosses it. A book
    priced worse than the limit still validates here. Whether it fills is execute.py's
    job: a real IOC lets the exchange decide, and paper fills replay this exact snapshot
    (``best_ask(side) <= limit_price``). Folding a price filter into V11 too would make
    that paper no-fill branch unreachable and would reject a normal, informative no-fill
    (docs/14 D12) as a malformed ticket instead."""
    markets = {"A": _market("A")}
    books = {"A": _book(yes_ask="0.4500", yes_size="50")}  # worse than the 0.40 limit
    out = _validate(_parsed([_bet(1, "A", limit="0.4000")]), markets, books, _settings(tmp_path))
    assert out.bets[0].status == "validated"


# --------------------------------------------------------------------------- first failure wins
def test_first_failure_wins_per_leg(tmp_path):
    """A leg that breaks several rules keeps the first code in the order they run: V04
    before V05, V05 before V07, V07 before V10, V10 before V11.

    One leg, broken every way a leg can be, with the checks in front of the one under
    test removed one at a time. V03 is not in this ladder even though it runs first: it
    fires only against a ticker an earlier leg has already validated, so a leg that fails
    any of these codes can never also be a duplicate. It has its own test above.
    """
    settings = _settings(tmp_path)
    broken = dict(limit="0.4200", contracts=9)          # off the 0.05 band, size past 3
    books = {}                                          # unobtainable, which would be V11

    # nothing to fetch: the window, the price and the size are never read
    out = _validate(_parsed([_bet(1, "GONE", **broken)]), {}, books, settings)
    assert out.bets[0].reject_code == "V04"

    # the market exists and is open, so the window is the first thing wrong
    markets = {"A": _market("A", close_h=200, tick_size="0.05")}
    out = _validate(_parsed([_bet(1, "A", **broken)]), markets, books, settings)
    assert out.bets[0].reject_code == "V05"

    # inside the window: the price band is next
    markets = {"A": _market("A", tick_size="0.05")}
    out = _validate(_parsed([_bet(1, "A", **broken)]), markets, books, settings)
    assert out.bets[0].reject_code == "V07"

    # a price on the band: the declared size is next
    out = _validate(_parsed([_bet(1, "A", limit="0.4000", contracts=9)]), markets, books,
                    settings)
    assert out.bets[0].reject_code == "V10"

    # and with every other defect gone, the unreadable book is what is left
    out = _validate(_parsed([_bet(1, "A", limit="0.4000")]), markets, books, settings)
    assert out.bets[0].reject_code == "V11"


# --------------------------------------------------------------------------- truncation
def test_truncation_past_the_cap_is_silent_and_counted(tmp_path):
    settings = _settings(tmp_path, **{"limits.max_bets_per_attempt": 2})
    markets = {t: _market(t) for t in ("A", "B", "C")}
    books = {t: _book(yes_ask="0.40", yes_size=50) for t in ("A", "B", "C")}
    bets = [_bet(1, "A"), _bet(2, "B"), _bet(3, "C")]

    out = _validate(_parsed(bets), markets, books, settings)

    assert out.truncated_count == 1
    assert {vb.spec.ticket_index for vb in out.bets} == {1, 2}
    assert all(vb.status == "validated" for vb in out.bets)
    assert all(vb.reject_code is None for vb in out.bets)


# --------------------------------------------------------------------------- resolution_event
def test_resolution_event_round_trips(tmp_path):
    """MAY declare it; a leg that does not is unaffected, and the validator passes it
    through unexamined (docs/14 D7)."""
    parsed = parse_ticket(_write_ticket(tmp_path, json.dumps({
        "attempt": "A-0007",
        "bets": [
            {"ticker": "A", "side": "yes", "limit_price": "0.4200", "contracts": 1,
             "rationale": "r", "resolution_event": "OWGR-2026-08-03"},
            {"ticker": "B", "side": "yes", "limit_price": "0.3000", "contracts": 1,
             "rationale": "r2"},
        ],
    })))
    assert parsed.whole_ticket_errors == []
    markets = {"A": _market("A"), "B": _market("B")}
    books = {"A": _book(yes_ask="0.42", yes_size=50), "B": _book(yes_ask="0.30", yes_size=50)}
    by = _by_index(_validate(parsed, markets, books, _settings(tmp_path)))
    assert by[1].status == "validated"
    assert by[1].spec.resolution_event == "OWGR-2026-08-03"
    assert by[2].status == "validated"
    assert by[2].spec.resolution_event is None


# --------------------------------------------------------------------------- V16, the allowance
def _allowance_case(tmp_path, legs, cap="8.00"):
    """Validate one ticket of ``(ticker, side, limit, contracts)`` legs on deep books."""
    settings = _settings(tmp_path, **{"stakes.per_attempt_real_cap": D(cap)})
    markets = {t: _market(t) for t, *_ in legs}
    books = {t: _multi_book(side, [("0.01", 50)]) for t, side, *_ in legs}
    bets = [_bet(i + 1, t, side=side, limit=limit, contracts=n)
            for i, (t, side, limit, n) in enumerate(legs)]
    return _by_index(_validate(_parsed(bets), markets, books, settings))


def test_v16_a_ticket_at_exactly_the_allowance_passes(tmp_path):
    # 3 x 0.94 + 3 x 0.94 + 2 x 0.90 + 1 x 0.56 = 2.82 + 2.82 + 1.80 + 0.56 = 8.00
    by = _allowance_case(tmp_path, [
        ("A", "no", "0.9400", 3), ("B", "no", "0.9400", 3),
        ("C", "yes", "0.9000", 2), ("D", "yes", "0.5600", 1),
    ])
    assert all(vb.status == "validated" for vb in by.values())


def test_v16_a_cent_over_the_allowance_refuses_the_leg_that_goes_past(tmp_path):
    # the same ticket with the last leg one cent dearer: 8.01 in total
    by = _allowance_case(tmp_path, [
        ("A", "no", "0.9400", 3), ("B", "no", "0.9400", 3),
        ("C", "yes", "0.9000", 2), ("D", "yes", "0.5700", 1),
    ])
    assert [by[i].status for i in (1, 2, 3)] == ["validated"] * 3
    assert by[4].status == "rejected" and by[4].reject_code == "V16"
    reason = by[4].reject_reason
    assert "$8.01 in total" in reason
    assert "$8.00 allowance" in reason
    assert "drop or shrink legs" in reason
    # the code keeps a sentence of its own for anything that reads codes only
    assert reason_for_code("V16") == REJECT_REASONS["V16"]


def test_v16_a_no_leg_is_staked_at_its_no_price(tmp_path):
    """A NO at 0.94 for three contracts stakes 2.82, not 3 x 0.06."""
    by = _allowance_case(tmp_path, [("A", "no", "0.9400", 3)], cap="2.81")
    assert by[1].reject_code == "V16"
    assert "$2.82 in total" in by[1].reject_reason
    by = _allowance_case(tmp_path, [("A", "no", "0.9400", 3)], cap="2.82")
    assert by[1].status == "validated"


def test_v16_a_refused_leg_adds_nothing_so_a_smaller_later_leg_fits(tmp_path):
    by = _allowance_case(tmp_path, [
        ("A", "yes", "0.9000", 3), ("B", "yes", "0.9000", 3),   # 5.40
        ("C", "yes", "0.9000", 3),                               # 8.10: refused
        ("D", "yes", "0.5000", 2),                               # 6.40: fits
    ])
    assert {i: by[i].reject_code for i in by} == {1: None, 2: None, 3: "V16", 4: None}
    assert "$9.10 in total" in by[3].reject_reason


def test_v16_counts_only_legs_that_passed_the_other_checks(tmp_path):
    """A leg another code refused never reaches the exchange, so it costs nothing."""
    settings = _settings(tmp_path)
    markets = {"A": _market("A"), "B": _market("B")}
    books = {t: _book(yes_ask="0.90", yes_size=50) for t in ("A", "B")}
    bets = [_bet(1, "A", limit="0.9000", contracts=3),
            _bet(2, "NOPE", limit="0.9000", contracts=3),     # V04
            _bet(3, "B", limit="0.9000", contracts=3)]
    by = _by_index(_validate(_parsed(bets), markets, books, settings))
    assert {i: by[i].reject_code for i in by} == {1: None, 2: "V04", 3: None}
