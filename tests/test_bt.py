"""The ``bt`` toolkit — neutral, read-only, memory-gated (spec §13)."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from typer.testing import CliRunner

from betting_agent import bt
from betting_agent.bt import _HISTORY_ROW_CAP as _HISTORY_ROWS
from betting_agent.kalshi.client import KalshiAPIError
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.kalshi.types import Market
from betting_agent.ledger.db import Ledger

runner = CliRunner()
CLOSE = datetime.now(UTC) + timedelta(hours=10)


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("BT_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake(monkeypatch):
    fk = FakeKalshi()
    monkeypatch.setattr(bt, "_client", lambda settings: fk)
    return fk


def _ledger(root):
    return Ledger.open(root / "data" / "ledger.db")


def _seed(root):
    """A settled probability attempt (A-0001) with one winning bet."""
    lg = _ledger(root)
    lg.migrate()
    _, aid = lg.create_attempt(env="prod", model="m", effort="high", memory_mode="on",
                               prompt_version="p", toolkit_version="0.1.0", workspace_path="/ws")
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="W1", side="yes",
                  limit_price=D("0.40"), model_prob=D("0.55"), rationale="r", is_real=0,
                  status="settled", fill_price=D("0.40"), outcome="win", pnl=D("1.16"),
                  fee=D("0.04"))
    lg.set_ticket_texts(aid, "## Markets\nNYC weather fair-value model", "## If we're right\nx",
                        "manifest text")
    lg.transition(aid, "settled")
    lg.close()
    return aid


def _mkt(fake, ticker="W1", *, category=None, yes_ask="0.42", no_ask="0.58",
         yes_ask_size=500):
    fake.add_market(ticker, title=f"title {ticker}", category=category, close_time=CLOSE,
                    yes_ask=D(yes_ask), yes_ask_size=yes_ask_size, no_ask=D(no_ask),
                    no_ask_size=300)


def invoke(*args):
    return runner.invoke(bt.app, list(args))


def _paged_market(ticker):
    return Market(
        ticker=ticker, title=f"title {ticker}", category=None, status="active",
        close_time=CLOSE, expected_expiration=CLOSE, yes_bid=D("0.40"), yes_ask=D("0.42"),
        no_bid=D("0.56"), no_ask=D("0.58"), volume=100, open_interest=10,
        tick_size=D("0.01"), raw={},
    )


class _PagedKalshi:
    """Kalshi double whose ``/markets`` paginates and enforces the 500 server page cap:
    a page larger than 500 raises HTTP 400, exactly as the live endpoint did in the pilot.
    ``iter_markets`` mirrors the real client's cursor loop."""

    def __init__(self, total: int):
        self._markets = [_paged_market(f"M{i:04d}") for i in range(total)]
        self.max_page_size_seen = 0
        self.pages_served = 0

    def get_markets(self, max_close_ts=None, status="open", category=None,
                    limit=200, cursor=None):
        self.max_page_size_seen = max(self.max_page_size_seen, limit)
        if limit > 500:
            raise KalshiAPIError(400, "limit exceeds server page cap")
        start = int(cursor) if cursor else 0
        page = self._markets[start:start + limit]
        self.pages_served += 1
        nxt = start + limit
        return page, (str(nxt) if nxt < len(self._markets) else None)

    def iter_markets(self, max_close_ts=None, status="open", category=None, limit=200):
        cursor = None
        while True:
            markets, cursor = self.get_markets(
                max_close_ts=max_close_ts, status=status, category=category,
                limit=limit, cursor=cursor,
            )
            yield from markets
            if not cursor:
                return


# --------------------------------------------------------------------------- markets / book
def test_markets_lists_against_fake(root, fake):
    _mkt(fake, "W1", yes_ask="0.42")
    _mkt(fake, "S1", yes_ask="0.55")
    res = invoke("markets", "--live")
    assert res.exit_code == 0
    assert "W1" in res.stdout and "S1" in res.stdout
    assert "0.42" in res.stdout


def test_markets_category_enrichment(root, fake):
    _mkt(fake, "W1", category="weather")
    _mkt(fake, "S1", category="sports")
    res = invoke("markets", "--live", "--category", "weather")
    assert res.exit_code == 0
    assert "W1" in res.stdout and "S1" not in res.stdout


def test_markets_min_volume_filters(root, fake):
    _mkt(fake, "W1")  # fake markets have volume None -> treated as 0
    res = invoke("markets", "--live", "--min-volume", "5")
    assert res.exit_code == 0
    assert "W1" not in res.stdout


def test_markets_json_decimals_are_strings(root, fake):
    _mkt(fake, "W1", yes_ask="0.42")
    res = invoke("markets", "--live", "--json")
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data[0]["yes_ask"] == "0.42"
    assert isinstance(data[0]["yes_ask"], str)


def test_markets_paginates_and_honors_total_limit(root, monkeypatch):
    paged = _PagedKalshi(total=1200)
    monkeypatch.setattr(bt, "_client", lambda settings: paged)
    res = invoke("markets", "--live", "--limit", "1000", "--json")
    assert res.exit_code == 0
    rows = json.loads(res.stdout)
    assert len(rows) == 1000                   # total honored across pages
    assert paged.max_page_size_seen <= 500     # every API page clamped to the server cap
    assert paged.pages_served >= 2             # actually fanned out across pages


def test_markets_large_limit_never_surfaces_400(root, monkeypatch):
    paged = _PagedKalshi(total=600)
    monkeypatch.setattr(bt, "_client", lambda settings: paged)
    res = invoke("markets", "--live", "--limit", "2000", "--json")
    assert res.exit_code == 0                   # a raw 400 would surface as exit 5
    rows = json.loads(res.stdout)
    assert len(rows) == 600                     # board exhausted below the requested limit
    assert paged.max_page_size_seen <= 500      # proves --limit is not passed as a page size


def test_markets_out_writes_full_json_and_prints_summary(root, fake, tmp_path, monkeypatch):
    _mkt(fake, "W1", yes_ask="0.42")
    _mkt(fake, "S1", yes_ask="0.55")
    # BT-1: --out now resolves strictly inside the working directory, so this writes from
    # the session's own cwd (which is what a real session does) instead of naming an
    # arbitrary absolute path.
    monkeypatch.chdir(tmp_path)
    out_path = tmp_path / "board.json"
    res = invoke("markets", "--live", "--out", "board.json")
    assert res.exit_code == 0
    assert f"wrote 2 markets to {out_path}" in res.stdout
    assert "yes_ask" not in res.stdout          # only the summary, not the table/JSON
    data = json.loads(out_path.read_text())     # the file holds the full JSON result
    assert {r["ticker"] for r in data} == {"W1", "S1"}
    assert data[0]["yes_ask"] == "0.42"         # Decimals serialized as strings


def test_book_shows_levels_and_derived_asks(root, fake):
    _mkt(fake, "W1", yes_ask="0.42", no_ask="0.58")
    res = invoke("book", "W1")
    assert res.exit_code == 0
    assert "[yes]" in res.stdout and "[no]" in res.stdout
    assert "best_ask=0.42" in res.stdout  # 1 - best no bid (0.58)


def test_book_json(root, fake):
    _mkt(fake, "W1", yes_ask="0.42", no_ask="0.58")
    res = invoke("book", "W1", "--json")
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["yes"]["best_ask"] == "0.42"  # 1 - best no bid (0.58)
    assert data["no"]["best_ask"] == "0.58"  # 1 - best yes bid (0.42)


def test_book_shows_fractional_size_not_truncated(root, fake):
    """docs/14 D3: ``bt book`` is a real consumer of ``best_ask_size``/
    ``best_bid_size`` too, not just V11 — the A-0020 shape (a fractional resting
    size) must display exactly, not silently round to a whole contract."""
    fake.add_market("W1", title="W1", close_time=CLOSE,
                    yes_ask=D("0.42"), yes_ask_size=D("0.96"),
                    no_ask=D("0.58"), no_ask_size=D("115"))

    res = invoke("book", "W1", "--json")
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["yes"]["best_ask_size"] == "0.96"
    assert data["no"]["best_ask_size"] == "115"

    text = invoke("book", "W1")
    assert res.exit_code == 0
    assert "best_ask=0.42 (0.96)" in text.stdout
    assert "best_ask=0.58 (115)" in text.stdout


def test_market_detail(root, fake):
    _mkt(fake, "W1", category="weather")
    res = invoke("market", "W1")
    assert res.exit_code == 0
    assert "tick_size:" in res.stdout
    assert "category: weather" in res.stdout


# --------------------------------------------------------------------------- history
# WP5/FK-7: FakeKalshi now honors the requested window, and ``bt history`` asks for the
# last ``--hours`` ending NOW. These candles used to sit at a fixed 2026-07-11 epoch, i.e.
# outside every window the command asks for — a series the real endpoint would never have
# returned. Anchored a few hours back instead, so the scripted world is one the exchange
# could actually serve. Ten hours of headroom covers the longest series scripted below
# (~9 hours of one-minute candles).
_TS0 = int((datetime.now(UTC) - timedelta(hours=10)).timestamp())


def _candle(ts_epoch, o, h, low, c, vol, oi):
    return {
        "end_period_ts": ts_epoch,
        "open_interest_fp": f"{oi}.00",
        "price": {"open_dollars": o, "high_dollars": h, "low_dollars": low,
                  "close_dollars": c, "mean_dollars": c, "previous_dollars": o},
        "volume_fp": f"{vol}.00",
        "yes_ask": {"close_dollars": h}, "yes_bid": {"close_dollars": low},
    }


def test_history_table_against_fake(root, fake):
    fake.set_candles("W1", [
        _candle(_TS0, "0.40", "0.43", "0.39", "0.42", 100, 500),
        _candle(_TS0 + 3600, "0.42", "0.45", "0.41", "0.44", 120, 510),
    ])
    res = invoke("history", "W1")
    assert res.exit_code == 0
    assert "ts" in res.stdout and "open" in res.stdout and "close" in res.stdout
    assert "oi" in res.stdout  # open interest column shown when present
    assert "0.42" in res.stdout and "0.44" in res.stdout


def test_history_json_is_raw_facts_with_string_decimals(root, fake):
    fake.set_candles("W1", [_candle(_TS0, "0.40", "0.43", "0.39", "0.42", 100, 500)])
    res = invoke("history", "W1", "--json")
    assert res.exit_code == 0
    rows = json.loads(res.stdout)
    assert len(rows) == 1
    row = rows[0]
    # neutral: exactly the raw candle facts, no derived signals
    assert set(row) == {"ts", "open", "high", "low", "close", "volume", "open_interest"}
    assert row["open"] == "0.40" and isinstance(row["open"], str)  # Decimal -> string
    assert row["close"] == "0.42"
    assert row["volume"] == 100 and row["open_interest"] == 500  # ints
    assert row["ts"].endswith("Z")  # tz-aware ISO


def test_history_accepts_all_period_labels(root, fake):
    fake.set_candles("W1", [_candle(_TS0, "0.40", "0.43", "0.39", "0.42", 100, 500)])
    for period in ("1m", "1h", "1d"):
        res = invoke("history", "W1", "--period", period)
        assert res.exit_code == 0, period


def test_history_invalid_period_is_usage_error(root, fake):
    fake.set_candles("W1", [_candle(_TS0, "0.40", "0.43", "0.39", "0.42", 100, 500)])
    res = invoke("history", "W1", "--period", "5m")
    assert res.exit_code == 2
    assert "invalid --period" in res.stdout


def test_history_empty_series_is_ok(root, fake):
    fake.add_market("W1", title="t", close_time=CLOSE)  # known market, no candles
    res = invoke("history", "W1")
    assert res.exit_code == 0
    assert "(none)" in res.stdout


def test_history_unknown_ticker_is_not_found(root, fake):
    res = invoke("history", "NOPE")  # no market, no candles -> KalshiAPIError(404)
    assert res.exit_code == 4


def test_history_upstream_error_is_exit_5(root, fake, monkeypatch):
    def boom(ticker, **kwargs):
        raise KalshiAPIError(500, "candles boom")

    monkeypatch.setattr(fake, "get_candlesticks", boom)
    res = invoke("history", "W1")
    assert res.exit_code == 5


# --------------------------------------------------------------------------- pure math
def test_fees_vector(root):
    # spec §8 as revised by docs/14 D5: 2 @ 0.50, coef 0.07 -> 0.035000, already on the
    # 4dp grid, so the ceiling is a no-op -> 0.0350
    res = invoke("fees", "--price", "0.50", "--contracts", "2")
    assert res.exit_code == 0
    assert "fee=0.0350" in res.stdout


def test_fees_index_coef(root):
    # same vector at the index coef 0.035 -> 0.0175000 -> 0.0175
    res = invoke("fees", "--price", "0.50", "--contracts", "2", "--category", "index")
    assert res.exit_code == 0
    assert "fee=0.0175" in res.stdout


def test_size_defaults_to_one_contract(root):
    res = invoke("size", "--price", "0.05")
    assert res.exit_code == 0
    assert "contracts=1" in res.stdout
    assert "stake=0.0500" in res.stdout
    assert "max=3" in res.stdout           # the bound a ticket is held to


def test_size_multiplies_the_declared_count_by_the_price(root):
    res = invoke("size", "--price", "0.70", "--contracts", "3")
    assert "contracts=3" in res.stdout
    assert "stake=2.1000" in res.stdout


def test_size_json_carries_the_stake_and_the_bound(root):
    res = invoke("size", "--price", "0.42", "--contracts", "2", "--json")
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert data["contracts"] == 2
    assert data["stake"] == "0.8400"
    assert data["max_contracts_per_bet"] == 3


# --------------------------------------------------------------------------- past (docs/22 §9)
_PAST_CLAIM = (
    "## Markets\nKXHORMUZWEEKLY-25SEP05-T3, the strait ladder.\n\n"
    "## Why this is profitable\nThe ladder prices the war headline, not the shipping "
    "data.\n\n## Why the opportunity exists and persists\nThe trackers are paywalled.\n"
)
_PAST_HYP = (
    "## If we're right\nThe ladder settles no.\n\n## If we're wrong\nA closure is "
    "announced.\n\n## Kill criteria\nAny Lloyd's list closure notice.\n"
)


@pytest.fixture
def past_on(monkeypatch):
    monkeypatch.setenv("BT_PAST", "on")


def _past_attempt(lg, *, slot, status, cell, era="live-v2", claim=_PAST_CLAIM):
    _seq, aid = lg.create_attempt(env="prod", model="claude-opus-5", effort="high",
                                  memory_mode="on", prompt_version="p",
                                  toolkit_version="0.1.0", workspace_path="/ws",
                                  slot=slot, cell=cell, cell_effective=cell, era=era)
    lg.transition(aid, "running")
    lg.set_ticket_texts(aid, claim, _PAST_HYP, "manifest text")
    lg.update_attempt_fields(aid, wall_seconds=1860, cost_usd=D("11.94"))
    lg.transition(aid, status)
    return aid


def _seed_past(root):
    """A settled winner with a refusal beside it, two passes, and one valid director page."""
    lg = _ledger(root)
    lg.migrate()
    won = _past_attempt(lg, slot="slot:2026-08-29/01:00", status="placed", cell="director")
    lg.insert_bet(bet_id=f"{won}-B01", attempt_id=won, ticket_index=1,
                  ticker="KXHORMUZWEEKLY-25SEP05-T3", category="Politics", side="no",
                  limit_price=D("0.62"), rationale="r", is_real=1, status="settled",
                  contracts=2, fill_price=D("0.62"), stake=D("1.24"), fee=D("0.04"),
                  outcome="win", pnl=D("0.72"), client_order_id=f"{won}-B01")
    lg.insert_bet(bet_id=f"{won}-B02", attempt_id=won, ticket_index=2,
                  ticker="KXHORMUZWEEKLY-25SEP05-T5", category="Politics", side="yes",
                  limit_price=D("0.21"), rationale="r", is_real=1, status="rejected",
                  declared_contracts=1, reject_code="cap_daily",
                  reject_reason="daily cap: $9.80 of $10.00 already committed today")
    lg.score_rejected_bet(f"{won}-B02", outcome="loss", hypothetical_pnl=D("-0.21"),
                          scored_at="2026-08-30T05:00:00Z")
    passed = _past_attempt(lg, slot="slot:2026-08-29/03:40", status="no_bets", cell="static")
    old = _past_attempt(lg, slot="slot:2026-07-08/10:00", status="no_bets", cell=None,
                        era="live-v1")
    lg.conn.execute(
        "INSERT INTO director_runs (run_id, run_date, cohort_date, started_at, model, "
        "status, page_md, page_hash) VALUES (?,?,?,?,?,?,?,?)",
        ("D-2026-08-30", "2026-08-30", "2026-08-29", "2026-08-30T04:00:00Z",
         "claude-fable-5", "valid", "## Standing direction\nKeep sweeping the strait.",
         "abc123abc123"),
    )
    lg.conn.execute(
        "INSERT INTO attempt_reviews (attempt_id, kind, run_id, cohort_date, rank, "
        "cohort_size, paragraph) VALUES (?,?,?,?,?,?,?)",
        (won, "retrospective", "D-2026-08-30", "2026-08-29", 1, 3,
         "It won for the reason it gave. " * 8),
    )
    lg.conn.commit()
    lg.close()
    return won, passed, old


def _blocked_leg(root):
    """An attempt the exchange refused: a `no_fill` row carrying the refusal it sent back.

    The text is the category refusal of 2026-09-18 to 09-20 with the state replaced by a
    generic phrase, stored the way `execute` stores it (the exception's string, its JSON
    body cut at 300 characters).
    """
    lg = _ledger(root)
    aid = _past_attempt(lg, slot="slot:2026-09-19/01:00", status="no_bets", cell="static")
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1,
        ticker="KXPRESPARTY-26NOV03-DEM", category="Politics", side="yes",
        limit_price=D("0.32"), rationale="r", is_real=1, status="no_fill", contracts=1,
        declared_contracts=1, client_order_id=f"{aid}-B01",
        reject_reason=(
            'KalshiAPIError: HTTP 403: Forbidden — {"error":{"code":"forbidden_location",'
            '"message":"Residents of this state are not currently allowed to open positions in '
            'Sports, Elections and Entertainment. Check your email for more details.","s'
        ),
    )
    lg.close()
    return aid


def _second_family(root):
    """One more settled attempt, in a family of its own, so a --limit of 1 has something
    to cut."""
    lg = _ledger(root)
    aid = _past_attempt(lg, slot="slot:2026-08-28/22:20", status="placed", cell="static")
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1,
                  ticker="KXHIGHNY-26AUG28-B90.5", category="Weather", side="yes",
                  limit_price=D("0.30"), rationale="r", is_real=1, status="settled",
                  contracts=1, fill_price=D("0.30"), stake=D("0.30"), fee=D("0.02"),
                  outcome="loss", pnl=D("-0.32"), client_order_id=f"{aid}-B01")
    lg.close()
    return aid


@pytest.mark.parametrize(
    "args",
    [
        ["past", "families"],
        ["past", "family", "KXHORMUZWEEKLY"],
        ["past", "search", "strait"],
        ["past", "attempt", "A-0001"],
        ["past", "page"],
        ["past", "recent"],
    ],
)
def test_past_is_refused_without_the_gate(root, args):
    """docs/22 section 8.7: the baseline cell has no history and is told so."""
    _seed_past(root)
    res = invoke(*args)
    assert res.exit_code == 3
    assert res.stdout.strip() == "history is not available to this attempt"


@pytest.mark.parametrize("value", ["off", "ON", "1", "true"])
def test_only_the_word_on_opens_the_history(root, monkeypatch, value):
    _seed_past(root)
    monkeypatch.setenv("BT_PAST", value)
    assert invoke("past", "recent").exit_code == 3


def test_past_help_lists_every_subcommand(root, past_on):
    res = invoke("past", "--help")
    assert res.exit_code == 0
    for name in ("families", "family", "search", "attempt", "page", "recent"):
        assert name in res.stdout


def test_past_with_no_subcommand_is_the_families_map(root, past_on):
    """A session that types `bt past --limit 60` means the map with more rows; it used to
    get "No such option" and lose the turn."""
    _seed_past(root)
    _second_family(root)
    bare = invoke("past")
    table = invoke("past", "families")
    assert bare.exit_code == 0
    assert bare.stdout == table.stdout

    capped = invoke("past", "--limit", "1")
    assert capped.exit_code == 0
    assert "notice: showing 1 of 2 families" in capped.stdout

    filtered = invoke("past", "--category", "Politics", "--json")
    assert filtered.exit_code == 0
    assert filtered.stdout == invoke("past", "families", "--category", "Politics",
                                     "--json").stdout


def test_past_families_counts_legs_wins_and_passes(root, past_on):
    _seed_past(root)
    res = invoke("past", "families")
    assert res.exit_code == 0
    header, *rows = [line for line in res.stdout.splitlines() if line.strip()][1:]
    assert header.split() == ["family", "attempts", "legs", "filled", "wins", "net",
                              "passes", "last_entry", "pass_reason"]
    row = next(r for r in rows if r.startswith("KXHORMUZWEEKLY"))
    assert row.split()[1:6] == ["1", "2", "1", "1", "0.7200"]
    assert "The ladder prices the war headline" in row      # the stated pass reason


def test_past_families_json_is_the_rows(root, past_on):
    _seed_past(root)
    res = invoke("past", "families", "--json")
    assert res.exit_code == 0
    rows = json.loads(res.stdout)
    assert [r["family"] for r in rows] == ["KXHORMUZWEEKLY"]
    assert rows[0]["net"] == "0.7200" and rows[0]["passes"] == 2
    assert "era:" in res.stderr                             # the header never mixes in


def test_past_families_json_is_whole_while_the_table_says_it_was_cut(root, past_on):
    """The shared convention (docs/22 section 9): the table is what gets read aloud and
    is capped with a notice; the JSON document is the answer and is never truncated."""
    _seed_past(root)
    _second_family(root)

    rows = json.loads(invoke("past", "families", "--json", "--limit", "1").stdout)
    assert [r["family"] for r in rows] == ["KXHORMUZWEEKLY", "KXHIGHNY"]

    res = invoke("past", "families", "--limit", "1")
    assert res.exit_code == 0
    assert "KXHIGHNY" not in res.stdout
    assert "notice: showing 1 of 2 families" in res.stdout


def test_past_family_prints_entries_then_totals(root, past_on):
    won, _passed, _old = _seed_past(root)
    res = invoke("past", "family", "KXHORMUZWEEKLY")
    assert res.exit_code == 0
    assert f"2026-08-29 · {won} · director · net 0.7200 on 1.2400 staked" in res.stdout
    assert "claim: The ladder prices the war headline" in res.stdout
    assert "KXHORMUZWEEKLY-25SEP05-T5 yes @0.2100 ×1 → refused: daily cap" in res.stdout
    assert ("totals: attempts 1 · legs 2 · filled 1 · wins 1 · staked 1.2400 · "
            "net 0.7200 · passes 2") in res.stdout


def test_past_family_json_shape(root, past_on):
    _seed_past(root)
    data = json.loads(invoke("past", "family", "KXHORMUZWEEKLY", "--json").stdout)
    assert data["family"] == "KXHORMUZWEEKLY"
    assert data["totals"]["legs"] == 2
    assert data["entries"][0]["legs"][0]["price"] == "0.6200"


def test_past_family_with_no_entries_is_not_found(root, past_on):
    _seed_past(root)
    res = invoke("past", "family", "KXNOSUCH")
    assert res.exit_code == 4
    assert "no attempt has entered KXNOSUCH" in res.stdout


def test_past_family_with_no_entries_is_not_found_under_json_too(root, past_on):
    """Exit 4 is a property of the question, not of the format the answer is printed in.

    The JSON branch used to return before the check, so a script asking about a family
    nobody has ever entered got a zero exit code and an empty document.
    """
    _seed_past(root)
    res = invoke("past", "family", "KXNOSUCH", "--json")
    assert res.exit_code == 4
    assert res.stdout.strip() == "error: no attempt has entered KXNOSUCH"


def test_past_search_prints_one_record_per_hit(root, past_on):
    won, _passed, _old = _seed_past(root)
    res = invoke("past", "search", "strait")
    assert res.exit_code == 0
    assert "match in claim:" in res.stdout
    assert (f"{won} · 2026-08-29 01:00 · director cell · claude-opus-5 high · Politics · "
            "KXHORMUZWEEKLY") in res.stdout
    rows = json.loads(invoke("past", "search", "strait", "--json").stdout)
    assert {r["kind"] for r in rows} == {"claim"}
    assert rows[0]["record"].startswith(rows[0]["attempt_id"])


def test_past_search_says_when_it_capped_the_matches(root, past_on):
    """It used to stop at --limit without a word, which reads as "that is all there is"."""
    _seed_past(root)
    _second_family(root)
    total = len(json.loads(invoke("past", "search", "ladder", "--json").stdout))
    assert total > 1

    res = invoke("past", "search", "ladder", "--limit", "1")

    assert res.exit_code == 0
    assert f"notice: showing 1 of {total} matches" in res.stdout


def test_past_attempt_prints_the_full_record(root, past_on):
    won, _passed, _old = _seed_past(root)
    res = invoke("past", "attempt", won)
    assert res.exit_code == 0
    assert "## edge_claim.md" in res.stdout and "## Legs" in res.stdout
    assert "retrospective (rank 1 of 3, cohort 2026-08-29):" in res.stdout
    data = json.loads(invoke("past", "attempt", won, "--json").stdout)
    assert data["attempt_id"] == won
    assert data["outcome"]["totals"]["net"] == "0.7200"
    assert data["outcome"]["legs"][0]["price"] == "0.6200"


def test_past_attempt_unknown_is_not_found(root, past_on):
    _seed_past(root)
    assert invoke("past", "attempt", "A-9999").exit_code == 4


def test_past_page_prints_the_latest_valid_page(root, past_on):
    _seed_past(root)
    res = invoke("past", "page")
    assert res.exit_code == 0
    assert ("page: D-2026-08-30 · run 2026-08-30 · cohort 2026-08-29 · claude-fable-5"
            in res.stdout)
    assert "Keep sweeping the strait." in res.stdout
    data = json.loads(invoke("past", "page", "--json").stdout)
    assert data["run_id"] == "D-2026-08-30" and data["page_hash"] == "abc123abc123"


def test_past_page_before_any_run_is_not_found(root, past_on):
    _seed_past(root)
    res = invoke("past", "page", "--date", "2026-08-01")
    assert res.exit_code == 4
    assert "no valid director page yet on or before 2026-08-01" in res.stdout


def test_past_recent_prints_short_records_newest_first(root, past_on):
    won, passed, old = _seed_past(root)
    res = invoke("past", "recent")
    assert res.exit_code == 0
    ids = [line.split(" ·")[0] for line in res.stdout.splitlines() if line.startswith("A-")]
    assert ids == [passed, won, old]
    rows = json.loads(invoke("past", "recent", "--limit", "1", "--json").stdout)
    assert [r["attempt_id"] for r in rows] == [passed]


def test_past_recent_says_when_it_capped_the_records(root, past_on):
    """Same notice as the tables: a set that was cut short says so in the same words."""
    _seed_past(root)

    res = invoke("past", "recent", "--limit", "1")

    assert res.exit_code == 0
    assert "notice: showing 1 of 3 records" in res.stdout


def test_past_shows_an_exchange_refusal_as_a_refusal(root, past_on):
    """What a session actually reads. These legs rendered as "no fill", and the attempts
    that studied them raised their limits against a door the exchange had shut."""
    _seed_past(root)
    blocked = _blocked_leg(root)

    record = invoke("past", "attempt", blocked)
    assert record.exit_code == 0
    assert (
        "refused by the exchange: Residents of this state are not currently allowed"
        in record.stdout
    )
    assert "no fill" not in record.stdout

    refused = invoke("past", "families", "--outcome", "refused")
    assert refused.exit_code == 0
    assert "KXPRESPARTY" in refused.stdout


def test_past_states_the_era_in_force_and_honors_it(root, past_on):
    won, _passed, old = _seed_past(root)
    res = invoke("past", "recent")
    assert res.stdout.splitlines()[0] == "era: all"
    current = invoke("past", "recent", "--era", "current")
    assert current.stdout.splitlines()[0] == "era: current (live-v2)"
    assert old not in current.stdout and won in current.stdout


@pytest.mark.parametrize(
    "args",
    [
        ["past", "recent", "--era", "pilot"],
        ["past", "families", "--outcome", "brilliant"],
        ["past", "families", "--since", "last tuesday"],
    ],
)
def test_past_bad_filters_are_usage_errors(root, past_on, args):
    _seed_past(root)
    assert invoke(*args).exit_code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["past", "recent", "--era", "pilot"],
        ["past", "families", "--outcome", "brilliant"],
        ["past", "search", "strait", "--since", "last tuesday"],
    ],
)
def test_past_bad_filters_are_usage_errors_before_the_ledger_is_opened(root, past_on, args):
    """A malformed flag is a usage error whether or not there is history to run it against.

    Checked after the ledger was opened, a bad --outcome on a box with no ledger exited 4
    and said the history was missing, which is the wrong code and the wrong sentence.
    """
    res = invoke(*args)
    assert res.exit_code == 2
    assert "no history yet" not in res.stdout


def test_past_pair_is_reserved_and_not_implemented(root, past_on):
    """docs/22 section 9: `bt past pair` is named in the spec and deliberately absent."""
    _seed_past(root)
    assert invoke("past", "pair", "A-0001", "A-0002").exit_code == 2


def test_past_without_a_ledger_is_not_found(root, past_on):
    res = invoke("past", "recent")
    assert res.exit_code == 4
    assert "no history yet" in res.stdout


# --------------------------------------------------------------------------- error mapping
def test_unknown_ticker_market_is_not_found(root, fake):
    res = invoke("market", "NOPE")  # fake has no markets -> KalshiAPIError(404)
    assert res.exit_code == 4


def test_unknown_ticker_book_is_not_found(root, fake):
    res = invoke("book", "NOPE")
    assert res.exit_code == 4


class _BoomClient:
    def iter_markets(self, **kwargs):
        raise KalshiAPIError(500, "boom")

    def get_market(self, ticker):
        raise KalshiAPIError(500, "boom")

    def get_orderbook(self, ticker, depth=5):
        raise KalshiAPIError(503, "boom")


def test_upstream_api_error_is_exit_5(root, monkeypatch):
    monkeypatch.setattr(bt, "_client", lambda settings: _BoomClient())
    assert invoke("markets", "--live").exit_code == 5
    assert invoke("market", "W1").exit_code == 5
    assert invoke("book", "W1").exit_code == 5


def test_markets_category_enrichment_error_is_exit_5(root, fake, monkeypatch):
    # BT-3: the failing call is now the cached EVENT lookup, not a per-row get_market —
    # the point of the test (an enrichment failure is exit 5, never a silent "no match")
    # is unchanged.
    _mkt(fake, "W1")

    def boom(event_ticker, *, raise_on_error=True):
        raise KalshiAPIError(500, "enrichment boom")

    monkeypatch.setattr(fake, "event_category", boom)
    assert invoke("markets", "--live", "--category", "weather").exit_code == 5


# --------------------------------------------------------------------------- ticket validate
def _write_ticket(directory, bets, *, headings=True):
    directory.mkdir(parents=True, exist_ok=True)
    if headings:
        (directory / "edge_claim.md").write_text(
            "## Markets\nm\n## Why this is profitable\ne\n"
            "## Why the opportunity exists and persists\nw\n"
        )
        (directory / "hypothesis.md").write_text(
            "## If we're right\na\n## If we're wrong\nb\n## Kill criteria\nc\n"
        )
    (directory / "MANIFEST.md").write_text("method")
    (directory / "bets.json").write_text(json.dumps({"attempt": "A-0001", "bets": bets}))


def test_ticket_validate_valid_and_reject(root, fake, tmp_path):
    _mkt(fake, "W1", yes_ask="0.42")
    _mkt(fake, "W2", yes_ask="0.42", yes_ask_size=1)
    tdir = tmp_path / "ticket"
    _write_ticket(tdir, [
        {"ticker": "W1", "side": "yes", "limit_price": "0.4200", "contracts": 1,
         "rationale": "a bet the book can fill"},
        {"ticker": "W2", "side": "yes", "limit_price": "0.4200", "contracts": 3,
         "rationale": "three contracts, one resting"},  # V11
    ])
    res = invoke("ticket", "validate", str(tdir))
    assert res.exit_code == 0
    assert "1 W1 validated" in res.stdout
    # the code AND the sentence the ledger row will carry
    assert "2 W2 rejected V11 (the order book could not be read, or holds less depth" \
        in res.stdout


def test_ticket_validate_whole_ticket_error(root, fake, tmp_path):
    _mkt(fake, "W1")
    tdir = tmp_path / "ticket"
    _write_ticket(tdir, [{"ticker": "W1", "side": "yes", "limit_price": "0.4200",
                          "contracts": 1, "rationale": "r"}], headings=False)
    res = invoke("ticket", "validate", str(tdir))
    assert res.exit_code == 0  # it ran
    assert "V02" in res.stdout  # missing required headings


def test_ticket_validate_missing_dir_is_usage_error(root, fake, tmp_path):
    res = invoke("ticket", "validate", str(tmp_path / "does-not-exist"))
    assert res.exit_code == 2


# --------------------------------------------------------------------------- PC-2 detail
def test_whole_ticket_error_prints_the_reason_not_just_the_code(root, fake, tmp_path):
    """The session repairing its ticket used to see a bare `V01` and nothing else."""
    _mkt(fake, "W1")
    tdir = tmp_path / "ticket"
    _write_ticket(tdir, [{"ticker": "W1", "side": "yes", "limit_price": "0.42",
                          "contracts": 1, "rationale": "r"}])
    res = invoke("ticket", "validate", str(tdir))
    assert res.exit_code == 0
    assert "whole-ticket errors: V01" in res.stdout
    assert "- bets[0] bad limit_price" in res.stdout


def test_v02_detail_names_the_missing_heading(root, fake, tmp_path):
    _mkt(fake, "W1")
    tdir = tmp_path / "ticket"
    _write_ticket(tdir, [{"ticker": "W1", "side": "yes", "limit_price": "0.4200",
                          "contracts": 1, "rationale": "r"}], headings=False)
    res = invoke("ticket", "validate", str(tdir))
    assert "## Kill criteria" in res.stdout


def test_json_output_carries_the_detail(root, fake, tmp_path):
    _mkt(fake, "W1")
    tdir = tmp_path / "ticket"
    _write_ticket(tdir, [{"ticker": "W1", "side": "yes", "limit_price": "0.42",
                          "contracts": 1, "rationale": "r"}])
    res = invoke("ticket", "validate", str(tdir), "--json")
    payload = json.loads(res.stdout)
    assert payload["whole_ticket_errors"] == ["V01"]          # the structure stays codes-only
    assert "bets[0] bad limit_price" in payload["detail"]


def test_clean_ticket_prints_no_detail_lines(root, fake, tmp_path):
    _mkt(fake, "W1", yes_ask="0.42")
    tdir = tmp_path / "ticket"
    _write_ticket(tdir, [{"ticker": "W1", "side": "yes", "limit_price": "0.4200",
                          "contracts": 1, "rationale": "r"}])
    res = invoke("ticket", "validate", str(tdir), "--json")
    assert json.loads(res.stdout)["detail"] == []


# --------------------------------------------------------------------------- ticket dir resolution
def test_ticket_validate_prints_resolved_dir(root, fake, tmp_path):
    tdir = tmp_path / "ticket"
    _write_ticket(tdir, [])
    res = invoke("ticket", "validate", str(tdir))
    assert res.exit_code == 0
    assert f"resolved ticket dir: {tdir}" in res.stdout


def test_ticket_validate_resolves_dot_dot_ticket(root, fake, tmp_path, monkeypatch):
    attempt = tmp_path / "attempts" / "A-0001"
    _write_ticket(attempt / "ticket", [])
    work = attempt / "workspace"
    work.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(work)
    res = invoke("ticket", "validate")           # no DIR -> ../ticket relative to cwd
    assert res.exit_code == 0
    assert "resolved ticket dir" in res.stdout


def test_ticket_validate_resolves_via_env(fake, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    tdir = proj / "data" / "attempts" / "A-0007" / "ticket"
    _write_ticket(tdir, [])
    elsewhere = tmp_path / "elsewhere" / "deep"  # no ../ticket here
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("BT_ROOT", str(proj))
    monkeypatch.setenv("BT_ATTEMPT_ID", "A-0007")
    res = invoke("ticket", "validate")           # no DIR, no ../ticket -> env fallback
    assert res.exit_code == 0
    assert str(tdir) in res.stdout


def test_ticket_validate_no_dir_no_env_is_usage_error(tmp_path, monkeypatch):
    elsewhere = tmp_path / "nowhere" / "deep"    # no ../ticket here
    elsewhere.mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("BT_ATTEMPT_ID", raising=False)
    monkeypatch.delenv("BT_ROOT", raising=False)
    res = invoke("ticket", "validate")
    assert res.exit_code == 2
    assert "DIR" in res.stdout and "BT_ATTEMPT_ID" in res.stdout


# --------------------------------------------------------------------------- read-only guarantee
def _row_counts(root):
    lg = Ledger.open(root / "data" / "ledger.db", readonly=True)
    tables = ("attempts", "bets", "retrospectives", "tags", "playbook", "audit_log",
              "meta", "ledger_fts", "bet_groups", "playbook_proposals")
    counts = {t: lg.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] for t in tables}
    lg.close()
    return counts


def test_bt_never_writes_the_ledger(root, fake, monkeypatch, past_on):
    aid = _seed(root)
    _mkt(fake, "W1", yes_ask="0.42", no_ask="0.58")
    before = _row_counts(root)
    for args in (
        ["markets", "--live"], ["market", "W1"], ["book", "W1"],
        ["fees", "--price", "0.42", "--contracts", "2"], ["size", "--price", "0.42"],
        ["past", "recent"], ["past", "families"], ["past", "attempt", aid],
    ):
        assert invoke(*args).exit_code == 0
    assert _row_counts(root) == before


# --------------------------------------------------------------------------- BT-7 hostile input
@pytest.mark.parametrize("bad", ["abc", "0,42", "", "0.4.2", "1e", "--", "0x1f", " "])
def test_fees_rejects_unparseable_price_with_a_usage_error(root, bad):
    """BT-2: a malformed --price handed the session a raw ``InvalidOperation`` traceback
    and exit 1 — indistinguishable from an upstream failure, when the honest answer is
    "you typed it wrong" (spec §13: 2 == usage error)."""
    res = invoke("fees", "--price", bad, "--contracts", "2")
    assert res.exit_code == 2, res.stdout
    assert "invalid --price" in res.stdout
    assert "Traceback" not in res.stdout


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "sNaN"])
def test_fees_rejects_non_finite_prices(root, bad):
    """These parse *successfully* as Decimals and then poison every number computed from
    them — ``fee=NaN`` printed as though it were an answer."""
    res = invoke("fees", "--price", bad, "--contracts", "2")
    assert res.exit_code == 2
    assert "finite" in res.stdout


@pytest.mark.parametrize("bad", ["abc", "2.5", "", "1e3"])
def test_fees_rejects_unparseable_contracts(root, bad):
    res = invoke("fees", "--price", "0.50", "--contracts", bad)
    assert res.exit_code == 2


def test_fees_rejects_negative_contracts(root):
    """A negative count parsed cleanly and produced a negative fee — a number that means
    nothing and reads as if it did."""
    res = invoke("fees", "--price", "0.50", "--contracts=-2")
    assert res.exit_code == 2
    assert "invalid --contracts" in res.stdout


def test_fees_accepts_zero_contracts(root):
    """Zero is a legitimate question ("what would nothing cost?"), unlike a negative."""
    res = invoke("fees", "--price", "0.50", "--contracts", "0")
    assert res.exit_code == 0


@pytest.mark.parametrize("bad", ["abc", "0,42", "NaN", ""])
def test_size_rejects_unparseable_price(root, bad):
    res = invoke("size", "--price", bad)
    assert res.exit_code == 2
    assert "invalid --price" in res.stdout
    assert "Traceback" not in res.stdout


def test_valid_prices_still_work_after_the_guard(root):
    assert invoke("fees", "--price", "0.50", "--contracts", "2").exit_code == 0
    assert invoke("size", "--price", "0.0500").exit_code == 0
    assert invoke("size", "--price", ".5").exit_code == 0


@pytest.mark.parametrize("limit", ["0", "-1", "-500"])
def test_markets_limit_zero_or_negative_returns_nothing(root, fake, limit):
    """BT-6: ``max(1, limit)`` turned "give me nothing" into a one-row page, and the
    ``len(listed) >= limit`` break fired after the first row."""
    _mkt(fake, "W1")
    _mkt(fake, "S1")
    res = invoke("markets", "--limit", limit, "--json")
    assert res.exit_code == 0
    assert json.loads(res.stdout) == []


def test_markets_limit_zero_sends_no_request(root, fake, monkeypatch):
    """Nothing wanted, nothing fetched — the toolkit runs inside a session that pays for
    the time."""
    _mkt(fake, "W1")

    def explode(*a, **kw):
        raise AssertionError("no listing request should be made for --limit 0")

    monkeypatch.setattr(fake, "iter_markets", explode)
    assert invoke("markets", "--live", "--limit", "0", "--json").exit_code == 0


def test_markets_limit_one_still_returns_one(root, fake):
    """The guard is exact: it changes 0 and below, not 1."""
    _mkt(fake, "W1")
    _mkt(fake, "S1")
    res = invoke("markets", "--live", "--limit", "1", "--json")
    assert res.exit_code == 0
    assert len(json.loads(res.stdout)) == 1


@pytest.mark.parametrize("bad", ["/etc/hosts", "../escaped.json", "../../escaped.json",
                                 "~/escaped.json", "sub/../../escaped.json"])
def test_markets_out_refuses_paths_outside_the_working_directory(root, fake, tmp_path,
                                                                 monkeypatch, bad):
    """BT-1 (reproduced: it silently truncated an existing file). ``bt`` is the *neutral,
    read-only* toolkit — it should not be the thing that overwrites the ledger, a prompt
    or a config file on a typo'd flag."""
    _mkt(fake, "W1")
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    res = invoke("markets", "--out", bad)

    assert res.exit_code == 2, res.stdout
    assert "must stay inside the working directory" in res.stdout


def test_markets_out_does_not_truncate_an_existing_outside_file(root, fake, tmp_path,
                                                                monkeypatch):
    """The reproduction, inverted: the victim file is byte-identical afterwards."""
    _mkt(fake, "W1")
    victim = tmp_path / "precious.json"
    victim.write_text("do not lose me")
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    assert invoke("markets", "--out", str(victim)).exit_code == 2
    assert victim.read_text() == "do not lose me"


def test_markets_out_allows_a_nested_path_inside_the_working_directory(root, fake, tmp_path,
                                                                      monkeypatch):
    _mkt(fake, "W1")
    workdir = tmp_path / "workspace"
    (workdir / "out").mkdir(parents=True)
    monkeypatch.chdir(workdir)

    res = invoke("markets", "--live", "--out", "out/board.json")

    assert res.exit_code == 0, res.stdout
    assert json.loads((workdir / "out" / "board.json").read_text())[0]["ticker"] == "W1"


def test_markets_out_unwritable_path_is_a_usage_error_not_a_traceback(root, fake, tmp_path,
                                                                     monkeypatch):
    """BT-1's OSError half: a directory where a file was expected."""
    _mkt(fake, "W1")
    workdir = tmp_path / "workspace"
    (workdir / "board.json").mkdir(parents=True)      # a directory, not a file
    monkeypatch.chdir(workdir)

    res = invoke("markets", "--live", "--out", "board.json")

    assert res.exit_code == 2
    assert "cannot write --out" in res.stdout
    assert "Traceback" not in res.stdout


# ------------------------------------------- WP4: category filtering before --limit
def test_markets_category_filter_finds_matches_deeper_than_the_limit(root, fake):
    """BT-4: the old order — take ``--limit`` rows, THEN filter — answered "none" while
    matches sat further down the board, and nothing said the search had been that shallow.
    """
    for i in range(10):
        _mkt(fake, f"S{i}", category="sports")
    _mkt(fake, "W9", category="weather")  # the only match, eleventh in listing order

    res = invoke("markets", "--live", "--category", "weather", "--limit", "3")

    assert res.exit_code == 0
    assert "W9" in res.stdout and "S0" not in res.stdout


def test_markets_limit_counts_matching_rows_not_scanned_rows(root, fake):
    for i in range(5):
        _mkt(fake, f"W{i}", category="weather")
    for i in range(5):
        _mkt(fake, f"S{i}", category="sports")

    res = invoke("markets", "--live", "--category", "weather", "--limit", "2", "--json")

    assert [r["ticker"] for r in json.loads(res.stdout)] == ["W0", "W1"]


def test_markets_category_uses_one_cached_event_lookup_not_a_market_get_per_row(root, fake):
    """BT-3/EF-3: the per-row ``get_market`` fan-out was up to a few hundred serial
    requests inside a session that pays for the wait."""
    for i in range(6):
        _mkt(fake, f"KXW-{i}", category="weather")
    fake.reset_calls()

    assert invoke("markets", "--live", "--category", "weather").exit_code == 0

    assert "get_market" not in fake.calls           # not one per listed row
    assert fake.calls["event_category"] == 6        # one per distinct event, cached


def test_markets_scan_cap_stops_the_walk_and_says_so(root, fake, monkeypatch):
    monkeypatch.setattr(bt, "_MARKETS_SCAN_CAP", 4)
    for i in range(10):
        _mkt(fake, f"S{i}", category="sports")

    res = invoke("markets", "--live", "--category", "weather")

    assert res.exit_code == 0
    assert "stopped after examining 4 markets" in res.stdout


def test_markets_without_a_filter_is_unchanged_by_the_scan_cap(root, fake):
    _mkt(fake, "W1")
    _mkt(fake, "S1")
    res = invoke("markets", "--live", "--limit", "1")
    assert res.exit_code == 0
    assert "notice:" not in res.stdout


# --------------------------------------------------------- WP4: bt history discipline
def _candles(n):
    return [_candle(_TS0 + i * 60, "0.40", "0.43", "0.39", "0.42", 1, 5)
            for i in range(n)]


def test_history_caps_printed_rows_and_names_what_it_dropped(root, fake):
    """BT-5: ``--period 1m`` over three days is ~4,300 rows straight into the context."""
    fake.set_candles("W1", _candles(_HISTORY_ROWS + 20))

    res = invoke("history", "W1")

    assert res.exit_code == 0
    assert f"showing the most recent {_HISTORY_ROWS} of {_HISTORY_ROWS + 20} candles" in res.stdout
    assert "20 rows omitted — use --out or narrow the window" in res.stdout
    assert len([ln for ln in res.stdout.splitlines() if ln.startswith("2026-")]) <= _HISTORY_ROWS


def test_history_under_the_cap_says_nothing(root, fake):
    fake.set_candles("W1", _candles(3))
    res = invoke("history", "W1")
    assert res.exit_code == 0 and "omitted" not in res.stdout


def test_history_json_is_capped_too_and_stays_parseable(root, fake):
    fake.set_candles("W1", _candles(_HISTORY_ROWS + 5))

    res = invoke("history", "W1", "--json")

    rows = json.loads(res.stdout)  # the notice must not land in the document
    assert len(rows) == _HISTORY_ROWS


def test_history_out_writes_the_whole_series_uncapped(root, fake, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake.set_candles("W1", _candles(_HISTORY_ROWS + 42))

    res = invoke("history", "W1", "--out", "candles.json")

    assert res.exit_code == 0
    assert f"wrote {_HISTORY_ROWS + 42} candles to" in res.stdout
    assert len(json.loads((tmp_path / "candles.json").read_text())) == _HISTORY_ROWS + 42


def test_history_out_refuses_to_escape_the_working_directory(root, fake, tmp_path, monkeypatch):
    """BT-1's path restriction applies to the new flag from the start, not eventually."""
    monkeypatch.chdir(tmp_path)
    fake.set_candles("W1", _candles(2))

    res = invoke("history", "W1", "--out", "../escape.json")

    assert res.exit_code == 2
    assert not (tmp_path.parent / "escape.json").exists()
