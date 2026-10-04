"""The shared board cache and the market lenses over it (docs/14 C1/C2).

Three populations, and the boundaries between them are the point:

* the cache module itself — publication atomicity, generation rotation, the
  ``first_seen`` carry-forward — asserted directly against files in ``tmp_path``;
* the tick's refresh step, driven by ``FakeKalshi`` and by the real ``betting-agent tick``
  so its step isolation is proven where it actually lives;
* every ``bt`` lens, run OFFLINE against a fixture cache with the exchange client wired to
  explode — a lens that quietly reached the network would fail here rather than pass
  slowly in production.

Nothing here touches ``data/``: the cache directory comes from ``settings.board_dir``,
which is derived from the root ``Settings`` was built with.
"""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from datetime import timedelta
from decimal import Decimal as D

import pytest
from typer.testing import CliRunner

from betting_agent import board, bt, cli
from betting_agent.config import load_settings
from betting_agent.kalshi.client import KalshiAPIError
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.kalshi.types import Market
from betting_agent.ledger.db import Ledger
from betting_agent.timeutil import iso, parse_iso, utc_now

runner = CliRunner()

# Second precision on purpose: ``iso()`` (and therefore every stored timestamp) truncates
# microseconds, so an anchor that carries them would never compare equal to what came back.
NOW = parse_iso(iso(utc_now()))
# The older generation sits outside ``bt new``'s 24 h default so "new" means something.
T_OLD = NOW - timedelta(hours=30)
T_NEW = NOW - timedelta(minutes=5)
CLOSE_SOON = NOW + timedelta(hours=6)
CLOSE_LATE = NOW + timedelta(hours=100)


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setenv("BT_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def settings(root):
    return load_settings(root=root)


@pytest.fixture
def ledger(root):
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.migrate()
    yield lg
    lg.close()


@pytest.fixture
def no_network(monkeypatch):
    """Any attempt to build an exchange client is a test failure, not a slow success."""
    def explode(settings):
        raise AssertionError("a cache-backed lens must not build a Kalshi client")

    monkeypatch.setattr(bt, "_client", explode)


def mkt(ticker, title, *, yes_bid=None, yes_ask=None, volume=100, oi=10, status="active",
        close_time=CLOSE_SOON, category=None, rules=None, last=None, event=None) -> Market:
    raw = {"ticker": ticker, "event_ticker": event or ticker.rsplit("-", 1)[0]}
    if rules:
        raw["rules_primary"] = rules
    if last is not None:
        raw["last_price_dollars"] = str(last)
    return Market(
        ticker=ticker, title=title, category=category, status=status, close_time=close_time,
        yes_bid=None if yes_bid is None else D(yes_bid),
        yes_ask=None if yes_ask is None else D(yes_ask),
        volume=volume, open_interest=oi, raw=raw,
    )


def _gen_old() -> list[Market]:
    return [
        mkt("KXHIGHNY-26AUG12-B85.5", "NY high above 85.5", yes_bid="0.40", yes_ask="0.42",
            rules="Resolves to the NWS official daily high for Central Park", last="0.41"),
        mkt("KXHIGHNY-26AUG12-B90.5", "NY high above 90.5", yes_bid="0.10", yes_ask="0.12"),
        mkt("KXOLD-26AUG12-GONE", "A market about to be delisted", yes_bid="0.30",
            yes_ask="0.32"),
    ]


def _gen_new() -> list[Market]:
    return [
        # moved 0.41 -> 0.61 mid
        mkt("KXHIGHNY-26AUG12-B85.5", "NY high above 85.5", yes_bid="0.60", yes_ask="0.62",
            rules="Resolves to the NWS official daily high for Central Park", last="0.61"),
        mkt("KXHIGHNY-26AUG12-B90.5", "NY high above 90.5", yes_bid="0.10", yes_ask="0.12"),
        mkt("KXVALORANT-26AUG12-TEAMA", "Valorant: Team A to win", yes_bid="0.55",
            yes_ask="0.57", volume=500, oi=80),
        mkt("KXUNQUOTED-26AUG12-NOQUOTE", "Never quoted", volume=0, oi=0,
            close_time=CLOSE_LATE),
    ]


@pytest.fixture
def two_generations(settings):
    """Two published generations, ``first_seen`` carried forward, ~30 h apart."""
    first = board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=T_OLD)
    second = board.write_generation(
        settings.board_dir, iter(_gen_new()), captured_at=T_NEW, prior=first["path"]
    )
    return first, second


def invoke(*args):
    return runner.invoke(bt.app, list(args))


# --------------------------------------------------------------------------- publication
def test_a_generation_is_published_by_a_single_rename(settings, monkeypatch):
    """The temp name cannot match the reader's glob, and one ``os.replace`` publishes it."""
    seen: list[tuple[str, str]] = []
    real_replace = board.os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(board.os, "replace", spy)
    stats = board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=T_OLD)

    (src, dst) = seen[0]
    assert len(seen) == 1
    assert src.rsplit("/", 1)[1].startswith(".board-")     # unmatchable by the glob
    assert dst == str(stats["path"])
    assert board.generations(settings.board_dir) == [stats["path"]]


def test_a_reader_never_sees_a_partial_build(settings, two_generations):
    """Mid-build, ``generations()`` still lists exactly the snapshots that are complete."""
    first, second = two_generations
    observed: list[list[str]] = []

    def streaming():
        for i, market in enumerate(_gen_new()):
            if i == 2:  # halfway through writing generation three
                observed.append([p.name for p in board.generations(settings.board_dir)])
            yield market

    third = board.write_generation(
        settings.board_dir, streaming(), captured_at=NOW, prior=second["path"]
    )
    assert observed == [[second["path"].name, first["path"].name]]
    assert board.generations(settings.board_dir)[0] == third["path"]


def test_a_crash_mid_build_leaves_the_previous_generation_serving(settings, two_generations):
    _first, second = two_generations

    def exploding():
        yield mkt("KXA-26AUG12-B1", "one", yes_bid="0.10", yes_ask="0.12")
        raise KalshiAPIError(500, "the board pull died halfway")

    with pytest.raises(KalshiAPIError):
        board.write_generation(settings.board_dir, exploding(), captured_at=NOW,
                               prior=second["path"])

    assert board.generations(settings.board_dir)[0] == second["path"]
    assert list(settings.board_dir.glob(".board-*")) == []      # temp file cleaned up
    with board.Board.open(second["path"]) as b:
        assert b.n_markets == len(_gen_new())


def test_rotation_keeps_only_the_two_newest_generations(settings):
    paths = [
        board.write_generation(settings.board_dir, iter(_gen_old()),
                               captured_at=NOW - timedelta(hours=h))["path"]
        for h in (3, 2, 1)
    ]
    removed = board.rotate(settings.board_dir, keep=2)

    assert [p.name for p in removed] == [paths[0].name]
    assert board.generations(settings.board_dir) == [paths[2], paths[1]]


def test_rotation_sweeps_an_abandoned_build_but_never_a_running_one(settings):
    """A build that dies before its ``os.replace`` leaves a temp file no reader can glob
    and nothing ever deleted. A pull is minutes, so anything older than a day is abandoned;
    anything younger may be a live build and is left alone."""
    kept = board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=NOW)
    stale = settings.board_dir / ".board-20260101T000000Z.sqlite3.tmp"
    running = settings.board_dir / ".board-20260102T000000Z.sqlite3.tmp"
    for path in (stale, running):
        path.write_bytes(b"half a generation")
    old = (NOW - timedelta(days=3)).timestamp()
    os.utime(stale, (old, old))

    removed = board.rotate(settings.board_dir, keep=2, now=NOW)

    assert [p.name for p in removed] == [stale.name]
    assert not stale.exists() and running.exists()
    assert board.generations(settings.board_dir) == [kept["path"]]


def test_the_cache_is_opened_read_only(settings, two_generations):
    _first, second = two_generations
    with board.Board.open(second["path"]) as b, pytest.raises(sqlite3.OperationalError):
        b._conn.execute("DELETE FROM markets")


def test_captured_at_comes_from_the_generation_not_its_mtime(settings, two_generations):
    _first, second = two_generations
    import os as _os

    _os.utime(second["path"], (0, 0))                 # a restore/backup rewrites mtime
    assert board.read_captured_at(second["path"]) == T_NEW


# --------------------------------------------------------------------------- first_seen
def test_first_seen_carries_forward_from_the_prior_generation(settings, two_generations):
    _first, second = two_generations
    with board.Board.open(second["path"]) as b:
        seen = {r["ticker"]: r["first_seen"] for r in b.markets(limit=None)}
        assert b.carried_forward is True
    assert seen["KXHIGHNY-26AUG12-B85.5"] == iso(T_OLD)     # survived, keeps its first sight
    assert seen["KXVALORANT-26AUG12-TEAMA"] == iso(T_NEW)   # genuinely new


def test_a_delisted_market_never_reappears_as_new(settings, two_generations):
    """The diff, reversed: a market in the PRIOR generation and absent from the latest is
    simply not in the latest, so no window can report it as newly listed."""
    _first, second = two_generations
    with board.Board.open(second["path"]) as b:
        rows = b.new_since(T_OLD - timedelta(days=1), limit=None)
    assert "KXOLD-26AUG12-GONE" not in {r["ticker"] for r in rows}


def test_the_first_generation_admits_it_cannot_tell_new_from_first_seen(settings):
    board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=T_NEW)
    res = invoke("new")
    assert res.exit_code == 0
    assert "first generation in the cache" in res.stdout


# --------------------------------------------------------------------------- refresh step
def _fake_board(*, settled: bool = False) -> FakeKalshi:
    fake = FakeKalshi()
    fake.add_market("KXHIGHNY-26AUG12-B85.5", title="NY high above 85.5", category="weather",
                    close_time=CLOSE_SOON, yes_ask=D("0.42"), yes_ask_size=500,
                    no_ask=D("0.58"), no_ask_size=300, volume=120, open_interest=40,
                    last_price=D("0.41"),
                    rules_primary="Resolves to the NWS official high for Central Park")
    fake.add_market("KXHIGHNY-26AUG12-B90.5", title="NY high above 90.5", category="weather",
                    close_time=CLOSE_SOON, yes_ask=D("0.12"), yes_ask_size=100, volume=5)
    fake.add_market("KXVALORANT-26AUG12-TEAMA", title="Valorant: Team A to win",
                    category="esports", close_time=CLOSE_LATE, yes_ask=D("0.57"),
                    yes_ask_size=200, volume=500)
    if settled:
        fake.add_market("KXDONE-26AUG01-B1", title="Already resolved", category="weather",
                        close_time=NOW - timedelta(days=1), status="finalized", volume=900)
    return fake


def test_refresh_pulls_the_board_enriches_categories_and_audits(ledger, settings):
    fake = _fake_board(settled=True)
    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)

    assert result["status"] == "refreshed"
    assert result["n_markets"] == 4                      # three open + one settled
    assert result["n_series"] == 3
    (audit,) = ledger.audit_events(event="board_cache_refreshed")
    detail = json.loads(audit["detail"])
    assert detail["n_markets"] == 4 and detail["n_series"] == 3

    with board.Board.open(result["path"]) as b:
        rows = {r["ticker"]: r for r in b.markets(limit=None, closing_within_hours=None)}
        assert rows["KXHIGHNY-26AUG12-B85.5"]["category"] == "weather"
        assert rows["KXHIGHNY-26AUG12-B85.5"]["last_price"] == D("0.41")
        assert rows["KXHIGHNY-26AUG12-B85.5"]["open_interest"] == 40
        settled = [s for s in b.series_rollup() if s["series"] == "KXDONE"]
        assert settled and settled[0]["n_settled"] == 1


def test_refresh_covers_both_configured_statuses(ledger, settings):
    """A settled market reaches the cache through the ``status=settled`` listing, which is
    the half of the fake's status vocabulary the board cache needed."""
    assert settings.board.statuses == ["open", "settled"]
    fake = _fake_board(settled=True)
    board.refresh_board_cache(ledger, fake, settings, now=NOW)
    assert fake.calls["get_markets"] >= 2               # one pull per status


def test_refresh_respects_the_configured_bound(ledger, settings):
    fake = _fake_board()
    board.refresh_board_cache(ledger, fake, settings, now=NOW - timedelta(minutes=40))
    fake.reset_calls()

    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)

    assert result["status"] == "fresh"
    assert fake.calls_total() == 0                      # not one request inside the 60 min
    assert len(board.generations(settings.board_dir)) == 1


def test_refresh_past_the_bound_keeps_the_configured_generations(ledger, settings):
    fake = _fake_board()
    board.refresh_board_cache(ledger, fake, settings, now=NOW - timedelta(hours=9))
    board.refresh_board_cache(ledger, fake, settings, now=NOW - timedelta(hours=5))
    board.refresh_board_cache(ledger, fake, settings, now=NOW - timedelta(hours=1))

    gens = board.generations(settings.board_dir)
    assert len(gens) == 2                               # generations_keep = 2
    # Newest first: `bt movers` compares the pair, which is why two are kept.
    assert board.read_captured_at(gens[0]) == NOW - timedelta(hours=1)
    assert board.read_captured_at(gens[1]) == NOW - timedelta(hours=5)


def test_a_forced_refresh_in_the_same_second_does_not_eat_a_generation(ledger, settings):
    """A generation's name IS its capture second, so republishing inside one would
    overwrite the very file the carry-forward reads."""
    fake = _fake_board()
    board.refresh_board_cache(ledger, fake, settings, now=NOW - timedelta(hours=6))
    board.refresh_board_cache(ledger, fake, settings, now=NOW)
    before = board.generations(settings.board_dir)

    again = board.refresh_board_cache(ledger, fake, settings, now=NOW, force=True)

    assert again["status"] == "fresh" and again["same_second"] is True
    assert board.generations(settings.board_dir) == before


def test_refresh_without_a_client_is_a_quiet_no_op(ledger, settings):
    assert board.refresh_board_cache(ledger, None, settings, now=NOW) == {"status": "no_client"}
    assert board.generations(settings.board_dir) == []
    assert ledger.audit_events(event="board_cache_refreshed") == []


def _uncategorized_fake(n_series: int, per_series: int = 1) -> FakeKalshi:
    """A fake whose LISTING carries no category, which is the live shape.

    ``/markets`` payloads have no category field at all (kalshi/types.py, verified live) —
    it lives on the parent event — so the enrichment path only exists because of this. The
    fake stores a category per market and serves it in listings, so it is left unset here
    and ``event_category`` answers instead, exactly as the exchange does.
    """
    fake = FakeKalshi()
    for s in range(n_series):
        for i in range(per_series):
            fake.add_market(f"KXS{s}-26AUG12-B{i}", title=f"series {s} strike {i}",
                            close_time=CLOSE_SOON, yes_ask=D("0.42"), yes_ask_size=10)
    def event_category(event_ticker, *, raise_on_error=True):
        fake.calls["event_category"] = fake.calls.get("event_category", 0) + 1
        return "weather"

    fake.event_category = event_category
    return fake


def test_category_lookup_is_one_per_series_not_one_per_market(ledger, settings):
    """Categories live on the parent event and bulk listings carry none, so ONE lookup per
    family is what makes having the column affordable at board scale."""
    fake = _uncategorized_fake(n_series=1, per_series=40)
    fake.reset_calls()

    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)

    assert fake.calls["event_category"] == 1            # one series, one lookup
    assert "get_market" not in fake.calls               # never one full GET per row
    assert result["categories_resolved"] == 1
    with board.Board.open(result["path"]) as b:
        rows = b.markets(limit=None)
        assert len(rows) == 40
        assert {r["category"] for r in rows} == {"weather"}   # all 40 carry it


def test_the_category_lookup_cap_is_recorded(ledger, settings):
    fake = _uncategorized_fake(n_series=4)
    settings.board.category_lookup_cap = 2

    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)

    assert result["categories_resolved"] == 2
    assert result["category_lookup_capped"] is True
    with board.Board.open(result["path"]) as b:
        assert sum(1 for r in b.markets(limit=None) if r["category"] is None) == 2


def test_a_failing_category_lookup_leaves_the_column_unknown(ledger, settings):
    """An enrichment failure must not become a wrong category or a failed refresh."""
    fake = _uncategorized_fake(n_series=2)

    def boom(event_ticker, *, raise_on_error=True):
        raise KalshiAPIError(500, "events endpoint down")

    fake.event_category = boom

    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)

    assert result["status"] == "refreshed" and result["categories_resolved"] == 0
    with board.Board.open(result["path"]) as b:
        assert all(r["category"] is None for r in b.markets(limit=None))


# ----------------------------------------- categories are paid for once, then carried
LATER = NOW + timedelta(hours=2)


def test_a_known_series_costs_no_lookup_in_the_next_generation(ledger, settings):
    """The enrichment was the refresh's largest cost (about 500 of about 650 requests) and
    it bought almost nothing: the board's families barely change between generations. Each
    one is now looked up once and carried in the generation's ``series_category`` table."""
    fake = _uncategorized_fake(n_series=3)
    first = board.refresh_board_cache(ledger, fake, settings, now=NOW)
    assert first["categories_resolved"] == 3 and first["categories_carried"] == 0

    fake.add_market("KXNEW-26AUG12-B0", title="a family that listed since",
                    close_time=CLOSE_SOON, yes_ask=D("0.42"), yes_ask_size=10)
    fake.reset_calls()
    second = board.refresh_board_cache(ledger, fake, settings, now=LATER)

    assert fake.calls["event_category"] == 1        # the new family only
    assert second["categories_resolved"] == 1
    assert second["categories_carried"] == 3
    with board.Board.open(second["path"]) as b:
        assert all(r["category"] == "weather" for r in b.markets(limit=None))
        assert b.meta("categories_carried") == "3"


def _excluding_fake() -> FakeKalshi:
    """Two families, one in a category the exchange refuses this account's orders in."""
    fake = FakeKalshi()
    for ticker, title in (("KXWX-26AUG12-B1", "a weather market"),
                          ("KXGAME-26AUG12-B1", "a game")):
        fake.add_market(ticker, title=title, close_time=CLOSE_SOON, yes_ask=D("0.42"),
                        yes_ask_size=10)

    def event_category(event_ticker, *, raise_on_error=True):
        fake.calls["event_category"] = fake.calls.get("event_category", 0) + 1
        return "Sports" if event_ticker.startswith("KXGAME") else "Weather"

    fake.event_category = event_category
    return fake


def test_an_excluded_familys_category_is_kept_although_its_rows_are_dropped(ledger,
                                                                           settings):
    """The 340 wasted lookups: Sports and Entertainment families were resolved and then
    deleted seconds later, every single refresh, because nothing remembered why they went.
    The map is written before the drop, so the exclusion still bites and costs nothing."""
    fake = _excluding_fake()
    first = board.refresh_board_cache(ledger, fake, settings, now=NOW)
    assert first["excluded_category_rows"] == 1 and first["categories_resolved"] == 2

    fake.reset_calls()
    second = board.refresh_board_cache(ledger, fake, settings, now=LATER)

    assert "event_category" not in fake.calls           # not one request for either family
    assert second["categories_carried"] == 2
    assert second["excluded_category_rows"] == 1        # and the game is still not listed
    with board.Board.open(second["path"]) as b:
        assert [r["series"] for r in b.markets(limit=None)] == ["KXWX"]


def test_a_series_whose_category_never_resolved_is_looked_up_again(ledger, settings):
    """Carrying forward must not turn one failed lookup into a permanently blank column."""
    fake = _uncategorized_fake(n_series=1)
    answers = [None, "weather"]

    def event_category(event_ticker, *, raise_on_error=True):
        fake.calls["event_category"] = fake.calls.get("event_category", 0) + 1
        return answers.pop(0)

    fake.event_category = event_category
    first = board.refresh_board_cache(ledger, fake, settings, now=NOW)
    assert first["categories_resolved"] == 0

    fake.reset_calls()
    second = board.refresh_board_cache(ledger, fake, settings, now=LATER)

    assert fake.calls["event_category"] == 1
    assert second["categories_resolved"] == 1 and second["categories_carried"] == 0


def test_the_lookup_cap_bounds_the_new_lookups_and_the_rest_arrive_next_time(ledger,
                                                                            settings):
    """The cap is a bound on requests, not on how much the cache may ever know: what it
    cuts off this generation is resolved by the next one, which no longer pays for the
    families this one already answered."""
    fake = _uncategorized_fake(n_series=4)
    settings.board.category_lookup_cap = 2
    first = board.refresh_board_cache(ledger, fake, settings, now=NOW)
    assert first["categories_resolved"] == 2 and first["category_lookup_capped"] is True

    fake.reset_calls()
    second = board.refresh_board_cache(ledger, fake, settings, now=LATER)

    assert fake.calls["event_category"] == 2           # the two the cap cut off
    assert second["categories_resolved"] == 2 and second["categories_carried"] == 2
    assert second["category_lookup_capped"] is False
    with board.Board.open(second["path"]) as b:
        assert all(r["category"] == "weather" for r in b.markets(limit=None))


def test_a_prior_generation_without_the_table_carries_nothing_and_still_builds(ledger,
                                                                              settings):
    """Every generation on disk when this landed predates ``series_category``. Reading one
    must cost requests, never a failed build."""
    old = board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=NOW)
    with sqlite3.connect(old["path"]) as conn:
        conn.execute("DROP TABLE series_category")

    fake = _uncategorized_fake(n_series=2)
    result = board.refresh_board_cache(ledger, fake, settings, now=LATER)

    assert result["status"] == "refreshed"
    assert result["categories_carried"] == 0 and result["categories_resolved"] == 2


# ------------------------------------------- what the pull is allowed to retrieve (docs/22 §6)
def _bounded_fake() -> FakeKalshi:
    """A board with a market on the far side of every bound the pull now carries: the close
    window, the excluded family, and the two categories the exchange refuses orders in."""
    fake = FakeKalshi()
    fake.add_market("KXHIGHNY-26AUG12-B85.5", title="NY high above 85.5", category="Weather",
                    close_time=CLOSE_SOON, yes_ask=D("0.42"), yes_ask_size=100, volume=120)
    fake.add_market("KXFAR-26DEC31-B1", title="closes in 200 hours", category="Weather",
                    close_time=NOW + timedelta(hours=200), yes_ask=D("0.30"), yes_ask_size=10)
    fake.add_market("KXMVECROSS-26AUG12-A", title="a multi-game parlay leg",
                    category="Weather", close_time=CLOSE_SOON, yes_ask=D("0.01"),
                    yes_ask_size=10)
    fake.add_market("KXNFL-26AUG12-TEAMA", title="Team A to win", category="Sports",
                    close_time=CLOSE_SOON, yes_ask=D("0.55"), yes_ask_size=50)
    fake.add_market("KXOSCARS-26AUG12-BESTPIC", title="Best picture", category="Entertainment",
                    close_time=CLOSE_SOON, yes_ask=D("0.20"), yes_ask_size=50)
    return fake


def test_the_open_pull_carries_the_close_bound_and_the_settled_pull_does_not(ledger, settings):
    """The one bound the exchange applies for us, in the seconds-since-the-epoch unit its
    ``max_close_ts`` parameter takes: those rows never leave the server, which is the whole
    difference between a 1.5 GB generation and a small one. The settled slice cannot carry
    it, since those markets closed in the past and a future bound would take every one of
    them, so it keeps ``settled_max`` instead."""
    fake = _bounded_fake()
    seen: list[tuple[str | None, int | None]] = []
    real = fake.iter_markets

    def spy(**kwargs):
        seen.append((kwargs.get("status"), kwargs.get("max_close_ts")))
        return real(**kwargs)

    fake.iter_markets = spy

    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)

    assert settings.board.close_bound_hours == 120
    assert seen == [("open", int((NOW + timedelta(hours=120)).timestamp())), ("settled", None)]
    with board.Board.open(result["path"]) as b:
        assert "KXFAR-26DEC31-B1" not in {r["ticker"] for r in b.markets(limit=None)}


def test_an_excluded_series_entry_is_a_prefix(ledger, settings):
    """The live parlay families are KXMVECROSSCATEGORY and KXMVECROSSCATEGORY0, with
    millions of markets closing inside the bound; the config names KXMVECROSS. An entry
    matches every series that starts with it, so the generation stays small."""
    assert board._series_excluded("KXMVECROSSCATEGORY", ["KXMVECROSS"])
    assert board._series_excluded("KXMVECROSSCATEGORY0", ["KXMVECROSS"])
    assert board._series_excluded("KXMVECROSS", ["KXMVECROSS"])
    assert not board._series_excluded("KXHIGHNY", ["KXMVECROSS"])
    assert not board._series_excluded("KXHIGHNY", [])


def test_an_excluded_series_never_enters_the_generation(ledger, settings):
    """Not filtered by each lens in turn: a family the account will not bet is absent from
    the file, so it cannot be counted, rolled up or paged through."""
    assert settings.board.excluded_series == ["KXMVECROSS"]

    result = board.refresh_board_cache(ledger, _bounded_fake(), settings, now=NOW)

    with board.Board.open(result["path"]) as b:
        rows = b.markets(limit=None, include_settled=True)
        assert "KXMVECROSS" not in {r["series"] for r in rows}
        assert "KXMVECROSS" not in {r["series"] for r in b.series_rollup()}


def test_the_excluded_categories_are_deleted_and_counted(ledger, settings):
    assert settings.board.excluded_categories == ["Sports", "Entertainment"]

    result = board.refresh_board_cache(ledger, _bounded_fake(), settings, now=NOW)

    assert result["excluded_category_rows"] == 2
    assert result["n_markets"] == 1                 # the one market past all three bounds
    with board.Board.open(result["path"]) as b:
        assert {r["category"] for r in b.markets(limit=None)} == {"Weather"}


def test_a_category_that_arrives_only_from_the_enrichment_is_still_excluded(ledger, settings):
    """Bulk listings carry no category at all, so the deletion has to run AFTER the
    per-series enrichment or it would never match a live row."""
    fake = FakeKalshi()
    fake.add_market("KXNFL-26AUG12-TEAMA", title="Team A to win", close_time=CLOSE_SOON,
                    yes_ask=D("0.55"), yes_ask_size=50)
    fake.add_market("KXHIGHNY-26AUG12-B85.5", title="NY high above 85.5",
                    close_time=CLOSE_SOON, yes_ask=D("0.42"), yes_ask_size=50)
    fake.event_category = lambda ev, *, raise_on_error=True: (
        "Sports" if ev.startswith("KXNFL") else "Weather"
    )

    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)

    assert result["excluded_category_rows"] == 1
    with board.Board.open(result["path"]) as b:
        assert [r["ticker"] for r in b.markets(limit=None)] == ["KXHIGHNY-26AUG12-B85.5"]


def test_every_listing_says_what_the_pull_left_out(ledger, settings, no_network):
    """The line exists because an empty answer and an excluded market are the same thing on
    screen: the category block cost two weeks of attempts researching markets whose orders
    were silently refused (docs/18). ``bt`` prints it through the banner it already had."""
    board.refresh_board_cache(ledger, _bounded_fake(), settings, now=NOW)

    res = invoke("series")

    assert res.exit_code == 0, res.stdout
    assert res.stdout.splitlines()[1] == (
        "bound: markets closing within 120 hours; excluded series: KXMVECROSS; "
        "excluded categories: Sports, Entertainment (2 removed)"
    )


def test_a_generation_built_before_the_header_renders_the_old_label(two_generations):
    """A snapshot must not be made to claim a bound it was not built with."""
    _first, second = two_generations
    with board.Board.open(second["path"]) as b:
        label = b.label(now=NOW)
    assert label == f"snapshot: {iso(T_NEW)} (5 min old, cached board listing)"


def test_the_refusal_text_is_set_once_and_then_travels_with_every_generation(
    tmp_path, monkeypatch, no_network
):
    """The operator quotes the exchange, the snapshot carries the quote: an exclusion with
    a reason attached cannot be mistaken for one of our own unexplained choices."""
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))
    settings = load_settings(root=root)
    fake = _bounded_fake()
    monkeypatch.setattr(cli, "_maybe_client", lambda settings: fake)
    text = "Kalshi: this account may not trade Sports or Entertainment markets."

    stored = runner.invoke(cli.app, ["board-refresh", "--set-refusal-text", text])

    assert stored.exit_code == 0, stored.stdout
    assert board.generations(settings.board_dir) == []       # it refreshed nothing
    assert fake.calls_total() == 0

    assert runner.invoke(cli.app, ["board-refresh"]).exit_code == 0
    with board.Board.open(board.generations(settings.board_dir)[0]) as b:
        assert b.meta("category_refusal_text") == text


# --------------------------------------------------------- step isolation (through the tick)
def _tick_root(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[kalshi]\nenv = "demo"\n\n[schedule]\nslots = []\n'
    )
    (tmp_path / "data").mkdir()
    lg = Ledger.open(tmp_path / "data" / "ledger.db")
    lg.migrate()
    lg.close()
    return tmp_path


def _fake_popen(monkeypatch) -> list[list[str]]:
    """Record every detached spawn instead of starting a process."""
    argvs: list[list[str]] = []

    def popen(argv, **kwargs):
        argvs.append(list(argv))
        return object()

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    return argvs


class _DeadTransport:
    """Every listing read fails, the way a dead network or a 403 fails one."""

    def __init__(self):
        self.calls = 0

    def iter_markets(self, **kwargs):
        self.calls += 1
        raise KalshiAPIError(403, "GET iter_markets blocked")


def test_a_dead_transport_costs_the_detached_child_and_not_the_tick(
    tmp_path, monkeypatch, no_network
):
    """docs/22 section 6 item 4: the pull left the tick's critical path.

    The tick now only decides that a refresh is due and hands it to a child, so it never
    touches the transport at all, and a dead one costs the child alone. The stale
    generation keeps serving with its own honest timestamp either way.
    """
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))
    settings = load_settings(root=root)
    stale = board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=T_OLD)

    dead = _DeadTransport()
    monkeypatch.setattr(cli, "_maybe_client", lambda settings: dead)
    monkeypatch.setattr(cli, "_client", lambda settings: dead)
    monkeypatch.setattr(cli, "settle_once", lambda *a, **k: {"errors": 0})
    monkeypatch.setattr(cli, "reconcile_once", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(cli, "real_orders_allowed",
                        lambda settings, ledger=None: (False, "test"))
    # The director step spawns a child of its own from midnight on; this test is about the
    # board child, and one spawn is what it counts.
    monkeypatch.setattr(cli, "_director_if_due", lambda *a, **k: {"status": "not_due"})
    spawned = _fake_popen(monkeypatch)

    res = runner.invoke(cli.app, ["tick"])
    assert res.exit_code == 0
    assert dead.calls == 0                          # the tick never reached the exchange
    assert [a[-1] for a in spawned] == ["board-refresh"]

    lg = Ledger.open(root / "data" / "ledger.db", readonly=True)
    errors = [json.loads(r["detail"]) for r in lg.audit_events(event="tick_step_error")]
    lg.close()
    assert errors == []                             # no step failed

    # The child is where the transport dies, and the stale generation survives it.
    child = runner.invoke(cli.app, ["board-refresh"])
    assert child.exit_code != 0
    assert dead.calls == 1

    # No new generation, and the lens still answers, from the stale snapshot, saying so.
    assert board.generations(settings.board_dir) == [stale["path"]]
    lens = invoke("series")
    assert lens.exit_code == 0
    assert f"snapshot: {iso(T_OLD)}" in lens.stdout
    assert "KXHIGHNY" in lens.stdout


def test_the_board_step_spawns_nothing_without_a_client_a_fresh_generation_or_a_free_lock(
    tmp_path, monkeypatch, no_network
):
    """Three gates, all cheap, all checked before a process is started."""
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))
    settings = load_settings(root=root)
    client = object()
    lg = Ledger.open(root / "data" / "ledger.db")
    try:
        board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=NOW)
        spawned = _fake_popen(monkeypatch)
        assert cli._board_if_due(lg, client, settings, NOW)["status"] == "fresh"
        assert spawned == []

        later = NOW + timedelta(hours=4)

        # Past the bound, but no credentials. The child would announce ``no_client`` and
        # exit, so spawning one every fifteen minutes is a process an hour saying nothing.
        assert cli._board_if_due(lg, None, settings, later)["status"] == "no_client"
        assert spawned == []

        # Past the bound with a client, but another refresh already holds the lock.
        settings.locks_dir.mkdir(parents=True, exist_ok=True)
        held = open(settings.locks_dir / "board.lock", "a")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            assert cli._board_if_due(lg, client, settings, later)["status"] == "busy"
            assert spawned == []
            fcntl.flock(held, fcntl.LOCK_UN)
        finally:
            held.close()

        # Client, stale generation, free lock: one child, and the tick does not wait.
        assert cli._board_if_due(lg, client, settings, later)["status"] == "spawned"
        assert [a[-1] for a in spawned] == ["board-refresh"]
    finally:
        lg.close()


def test_a_halted_system_refreshes_nothing(tmp_path, monkeypatch, no_network):
    """Nothing reads a fresh board under a HALT: no attempt runs, and the digest works off
    whatever snapshot is on disk. The 42-hour halt of 2026-09-20 spent 15 refreshes on
    snapshots nobody opened. The gate is the HALT file, checked before every other gate."""
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))
    settings = load_settings(root=root)
    lg = Ledger.open(root / "data" / "ledger.db")
    try:
        board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=T_OLD)
        spawned = _fake_popen(monkeypatch)
        settings.halt_path.parent.mkdir(parents=True, exist_ok=True)
        settings.halt_path.write_text("reconcile_drift\n")

        assert cli._board_if_due(lg, object(), settings, NOW)["status"] == "halted"
        assert spawned == []

        # And the first tick after `resume` pulls straight away: the interval is derived
        # from the snapshot's own capture time, so the halt cannot leave it waiting.
        settings.halt_path.unlink()
        assert cli._board_if_due(lg, object(), settings, NOW)["status"] == "spawned"
        assert [a[-1] for a in spawned] == ["board-refresh"]
    finally:
        lg.close()


def test_a_refresh_still_running_when_the_interval_expires_is_not_spawned_twice(
    tmp_path, monkeypatch, no_network
):
    """At a 60-minute interval and a 15-minute tick, four ticks pass over every refresh, and
    a slow pull is still running when the next one comes due. The child holds ``board.lock``
    for its whole run and the tick probes it, so the overlap is a no-op, not a second pull
    of the same board."""
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))
    settings = load_settings(root=root)
    lg = Ledger.open(root / "data" / "ledger.db")
    try:
        board.write_generation(settings.board_dir, iter(_gen_old()), captured_at=NOW)
        spawned = _fake_popen(monkeypatch)
        # Due: the generation is past the 60-minute interval.
        due = NOW + timedelta(minutes=61)
        assert cli._board_if_due(lg, object(), settings, due)["status"] == "spawned"

        # That child is now running and holds the lock the way ``board-refresh`` does.
        child_lock = cli._acquire_lock(settings.locks_dir / "board.lock")
        assert child_lock is not None
        try:
            for minutes in (15, 30, 45):
                later = due + timedelta(minutes=minutes)
                assert cli._board_if_due(lg, object(), settings, later)["status"] == "busy"
        finally:
            cli._release_lock(child_lock)

        assert [a[-1] for a in spawned] == ["board-refresh"]     # one child, not four
    finally:
        lg.close()


def test_board_refresh_exits_zero_and_pulls_nothing_when_the_lock_is_busy(
    tmp_path, monkeypatch, no_network
):
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))
    settings = load_settings(root=root)
    dead = _DeadTransport()
    monkeypatch.setattr(cli, "_maybe_client", lambda settings: dead)
    settings.locks_dir.mkdir(parents=True, exist_ok=True)
    held = open(settings.locks_dir / "board.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = runner.invoke(cli.app, ["board-refresh"])
        assert res.exit_code == 0
        assert "board.lock busy" in res.stdout
        assert dead.calls == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_board_refresh_pulls_prints_the_generation_and_clears_its_streak(
    tmp_path, monkeypatch, no_network
):
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))
    settings = load_settings(root=root)
    fake = _fake_board(settled=True)
    monkeypatch.setattr(cli, "_maybe_client", lambda settings: fake)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("alert_streak:board_refresh", json.dumps({"n": 5, "notified": True}))
    lg.close()

    res = runner.invoke(cli.app, ["board-refresh"])

    assert res.exit_code == 0, res.stdout
    gens = board.generations(settings.board_dir)
    assert len(gens) == 1
    assert gens[0].name in res.stdout
    # Stamped: the child is detached and its stdout is a log file nobody is watching, so an
    # unstamped line cannot be placed against a tick, a halt or the refresh interval.
    (line,) = [ln for ln in res.stdout.splitlines() if "board-refresh:" in ln]
    when, _, rest = line.partition(" board-refresh: ")
    assert parse_iso(when) <= utc_now()
    assert rest == gens[0].name
    lg = Ledger.open(root / "data" / "ledger.db", readonly=True)
    assert json.loads(lg.meta_get("alert_streak:board_refresh")) == {
        "n": 0, "notified": False,
    }
    lg.close()


def test_a_failing_board_refresh_is_counted_and_escalates_on_the_third(
    tmp_path, monkeypatch, no_network, notify_calls
):
    """The pull left the tick, so it left the tick's per-step streak with it.

    A detached child that dies into its own log file is the silent wedge docs/12 §8.1 is
    about, so it keeps the same escalation under its own streak name.
    """
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))
    dead = _DeadTransport()
    monkeypatch.setattr(cli, "_maybe_client", lambda settings: dead)

    for _ in range(4):
        res = runner.invoke(cli.app, ["board-refresh"])
        assert res.exit_code == 1
        assert "board-refresh: failed:" in res.stdout

    lg = Ledger.open(root / "data" / "ledger.db", readonly=True)
    assert json.loads(lg.meta_get("alert_streak:board_refresh"))["n"] == 4
    alerts = [json.loads(r["detail"]) for r in lg.audit_events(event="alert_raised")]
    lg.close()

    assert [a["key"] for a in alerts] == ["board_refresh"]       # once, not four times
    assert alerts[0]["streak"] == 3
    assert len(notify_calls) == 1
    assert "board-refresh has failed 3 times running" in notify_calls[0]


def test_the_tick_wires_the_board_step_before_slots(monkeypatch, tmp_path):
    """Order matters: the sessions a tick spawns must read a snapshot this tick asked for."""
    order: list[str] = []
    real_step = cli._step

    def spy(ledger, name, fn, outcomes=None, settings=None, **kwargs):
        order.append(name)
        return real_step(ledger, name, fn, outcomes, settings=settings)

    monkeypatch.setattr(cli, "_step", spy)
    monkeypatch.setattr(cli, "_board_if_due", lambda *a, **k: {"status": "no_client"})
    monkeypatch.setattr(cli, "settle_once", lambda *a, **k: {"errors": 0})
    monkeypatch.setattr(cli, "reconcile_once", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(cli, "_director_if_due", lambda *a, **k: {"status": "not_due"})
    root = _tick_root(tmp_path)
    monkeypatch.setenv("BT_ROOT", str(root))

    assert runner.invoke(cli.app, ["tick"]).exit_code == 0

    assert "board" in order
    assert order.index("board") < order.index("slots")
    assert order.index("settle") < order.index("board")


# --------------------------------------------------------------------------- lenses, offline
_OFFLINE_LENSES = [
    ["series"], ["markets"], ["search", "valorant"], ["new"], ["movers"],
    ["board", "KXHIGHNY"], ["calendar"],
]


@pytest.mark.parametrize("args", _OFFLINE_LENSES, ids=[a[0] for a in _OFFLINE_LENSES])
def test_every_lens_runs_offline_and_prints_the_snapshot_timestamp(
    two_generations, no_network, args
):
    res = invoke(*args)
    assert res.exit_code == 0, res.stdout
    assert res.stdout.startswith(f"snapshot: {iso(T_NEW)}")


@pytest.mark.parametrize("args", _OFFLINE_LENSES, ids=[a[0] for a in _OFFLINE_LENSES])
def test_json_output_is_only_json_with_the_source_line_on_stderr(
    two_generations, no_network, args
):
    res = invoke(*args, "--json")
    assert res.exit_code == 0, res.stdout
    json.loads(res.stdout)                          # nothing but the document on stdout
    assert iso(T_NEW) in res.stderr


def test_series_collapses_a_parlay_family_to_one_row(settings, no_network):
    """docs/12 §3's flood, structurally: hundreds of dead rows become one row that says
    how many there are — no denylist anywhere."""
    flood = [
        mkt(f"KXPARLAY-26AUG12-G{i}", f"parlay leg {i}", yes_bid="0.01", yes_ask="0.99",
            volume=0, oi=0)
        for i in range(300)
    ]
    live = [mkt("KXHIGHNY-26AUG12-B85.5", "NY high above 85.5", yes_bid="0.40",
                yes_ask="0.42", volume=900)]
    board.write_generation(settings.board_dir, iter(flood + live), captured_at=T_NEW)

    rows = json.loads(invoke("series", "--json").stdout)

    assert [r["series"] for r in rows] == ["KXHIGHNY", "KXPARLAY"]   # volume order
    (parlay,) = [r for r in rows if r["series"] == "KXPARLAY"]
    assert parlay["n_markets"] == 300 and parlay["volume"] == 0
    # 301 markets, and the survey reads three lines: a banner, a header, two rows.
    assert len(invoke("series").stdout.splitlines()) == 4


def test_series_reports_counts_volume_oi_close_and_settled(settings, no_network):
    rows_in = [
        mkt("KXS-26AUG12-B1", "one", yes_bid="0.40", yes_ask="0.42", volume=10, oi=3,
            close_time=CLOSE_SOON, category="weather"),
        mkt("KXS-26AUG12-B2", "two", yes_bid="0.50", yes_ask="0.52", volume=90, oi=7,
            close_time=CLOSE_LATE, category="weather"),
        mkt("KXS-26AUG01-B3", "three", volume=1, oi=0, status="finalized",
            close_time=NOW - timedelta(days=2), category="weather"),
    ]
    board.write_generation(settings.board_dir, iter(rows_in), captured_at=T_NEW)

    (row,) = json.loads(invoke("series", "--json").stdout)

    assert row["series"] == "KXS"
    assert row["n_markets"] == 3 and row["n_settled"] == 1
    assert row["volume"] == 101 and row["open_interest"] == 10
    assert row["soonest_close"] == iso(NOW - timedelta(days=2))
    assert row["title"] == "two"                    # the busiest market represents the family
    assert row["category"] == "weather"


# ------------------------------------- the settled slice stays out of the tradeable lenses
def _settled_flood(n: int = 300):
    """A cache shaped like the live one: resolved history dwarfing the open board.

    ``board.settled_max`` is 20000 against a few thousand open markets, and the resolved
    rows carry their whole lifetime's volume and the oldest close times — so they win the
    default ``--sort close`` (ascending) AND ``--sort volume`` unless a lens excludes them.
    """
    settled = [
        mkt(f"KXDONE-26JUL{i:02d}-B1", f"resolved {i}", status="finalized", volume=900000 + i,
            oi=5, close_time=NOW - timedelta(days=30 + i))
        for i in range(n)
    ]
    open_rows = [
        mkt(f"KXLIVE-26AUG12-B{i}", f"open {i}", yes_bid="0.40", yes_ask="0.42",
            volume=1000, oi=40, close_time=NOW + timedelta(hours=6 + i))
        for i in range(3)
    ]
    return settled + open_rows


def test_the_listing_lenses_never_serve_resolved_history(settings, no_network):
    """The regression that would have shipped: with the settled slice unfiltered, the
    primary survey lens answered 200 rows of resolved markets and zero tradeable ones.
    ``bt series`` still counts them — that count is why the slice is cached at all."""
    board.write_generation(settings.board_dir, iter(_settled_flood()), captured_at=T_NEW)

    # Asserted on the series, not on ``status``: a resolved market's payload status is
    # "finalized" as often as "settled" (``_SETTLED_STATUSES``), and a check against one
    # spelling passes while the other floods the listing.
    for args in (("markets", "--json"),
                 ("markets", "--sort", "volume", "--json"),
                 ("markets", "--closing-within", "24", "--json"),
                 ("search", "open", "--json"),
                 ("calendar", "--json"),
                 ("new", "--json")):
        rows = json.loads(invoke(*args).stdout)
        assert rows, f"{args[0]} returned nothing at all"
        assert {r["series"] for r in rows} == {"KXLIVE"}, f"{args} served resolved markets"

    (row,) = json.loads(invoke("series", "--json", "--sort", "markets").stdout)[:1]
    assert row["series"] == "KXDONE" and row["n_settled"] == 300


def test_a_ladder_is_the_live_rungs_not_the_familys_history(settings, no_network):
    """A daily family accumulates one resolved rung per strike per day; on 'one screen'
    they would push today's tradeable rungs off it."""
    rows_in = [
        mkt("KXHIGHNY-26AUG01-B85.5", "NY high 85.5, resolved", status="finalized",
            volume=5000, close_time=NOW - timedelta(days=11)),
        mkt("KXHIGHNY-26AUG12-B85.5", "NY high 85.5", yes_bid="0.40", yes_ask="0.42",
            close_time=CLOSE_SOON),
    ]
    board.write_generation(settings.board_dir, iter(rows_in), captured_at=T_NEW)

    rows = json.loads(invoke("board", "kxhighny", "--json").stdout)

    assert [r["ticker"] for r in rows] == ["KXHIGHNY-26AUG12-B85.5"]


def test_the_settled_rows_stay_reachable_through_the_board_api(settings, no_network):
    """Excluded by default, not deleted: the count `bt series` prints comes from these rows
    and a future lens can ask for them without a schema change."""
    board.write_generation(settings.board_dir, iter(_settled_flood(n=2)), captured_at=T_NEW)

    with board.Board.open(board.generations(settings.board_dir)[-1]) as b:
        assert len(b.markets(include_settled=True)) == 5
        assert len(b.markets()) == 3
        assert len(b.search("resolved", include_settled=True)) == 2
        assert b.search("resolved") == []


def test_markets_filters_by_series_category_volume_and_close(two_generations, no_network):
    assert [r["ticker"] for r in json.loads(
        invoke("markets", "--series", "kxhighny", "--json").stdout
    )] == ["KXHIGHNY-26AUG12-B85.5", "KXHIGHNY-26AUG12-B90.5"]

    assert [r["ticker"] for r in json.loads(
        invoke("markets", "--min-volume", "200", "--json").stdout
    )] == ["KXVALORANT-26AUG12-TEAMA"]

    within = {r["ticker"] for r in json.loads(
        invoke("markets", "--closing-within", "12", "--json").stdout
    )}
    assert "KXUNQUOTED-26AUG12-NOQUOTE" not in within      # closes in 100 h


def test_markets_sorts_by_the_named_column(two_generations, no_network):
    by_volume = [r["ticker"] for r in json.loads(
        invoke("markets", "--sort", "volume", "--json").stdout
    )]
    assert by_volume[0] == "KXVALORANT-26AUG12-TEAMA"

    by_close = [r["close_time"] for r in json.loads(
        invoke("markets", "--sort", "close", "--json").stdout
    )]
    assert by_close == sorted(by_close)


def test_an_unquoted_market_sorts_where_a_zero_would(settings, no_network):
    """The sorts use bare columns so SQLite can use an index; that is only safe because
    ``DESC`` puts NULL exactly where ``COALESCE(x, 0)`` would put it — last."""
    rows_in = [
        mkt("KXA-26AUG12-B1", "has volume", yes_bid="0.10", yes_ask="0.12", volume=7, oi=3),
        mkt("KXB-26AUG12-B1", "no volume", yes_bid="0.10", yes_ask="0.12", volume=None,
            oi=None),
        mkt("KXC-26AUG12-B1", "zero volume", yes_bid="0.10", yes_ask="0.12", volume=0, oi=0),
    ]
    board.write_generation(settings.board_dir, iter(rows_in), captured_at=T_NEW)

    by_volume = [r["ticker"] for r in json.loads(
        invoke("markets", "--sort", "volume", "--json").stdout)]
    by_oi = [r["ticker"] for r in json.loads(
        invoke("markets", "--sort", "oi", "--json").stdout)]

    assert by_volume[0] == "KXA-26AUG12-B1"
    assert set(by_volume[1:]) == {"KXB-26AUG12-B1", "KXC-26AUG12-B1"}
    assert by_oi[0] == "KXA-26AUG12-B1"
    # And an explicit --min-volume still counts an unquoted market as zero, as the live
    # path's ``(market.volume or 0)`` always has.
    assert [r["ticker"] for r in json.loads(
        invoke("markets", "--min-volume", "1", "--json").stdout)] == ["KXA-26AUG12-B1"]
    assert len(json.loads(invoke("markets", "--min-volume", "0", "--json").stdout)) == 3


def test_markets_rejects_an_unknown_sort(two_generations, no_network):
    res = invoke("markets", "--sort", "attractiveness")
    assert res.exit_code == 2
    assert "invalid --sort" in res.stdout


def test_markets_limit_counts_cache_rows(two_generations, no_network):
    assert len(json.loads(invoke("markets", "--limit", "2", "--json").stdout)) == 2


def test_search_matches_titles_and_rules_text(two_generations, no_network):
    by_title = [r["ticker"] for r in json.loads(invoke("search", "valorant", "--json").stdout)]
    assert by_title == ["KXVALORANT-26AUG12-TEAMA"]

    by_rules = [r["ticker"] for r in json.loads(invoke("search", "nws", "--json").stdout)]
    assert by_rules == ["KXHIGHNY-26AUG12-B85.5"]

    both_terms = json.loads(invoke("search", "central park", "--json").stdout)
    assert [r["ticker"] for r in both_terms] == ["KXHIGHNY-26AUG12-B85.5"]
    assert json.loads(invoke("search", "central saturn", "--json").stdout) == []


def test_new_lists_only_what_first_appeared(two_generations, no_network):
    rows = json.loads(invoke("new", "--json").stdout)
    assert {r["ticker"] for r in rows} == {
        "KXVALORANT-26AUG12-TEAMA", "KXUNQUOTED-26AUG12-NOQUOTE",
    }
    assert all(r["first_seen"] == iso(T_NEW) for r in rows)


def test_new_honors_an_explicit_since(two_generations, no_network):
    rows = json.loads(invoke("new", "--since", iso(T_OLD), "--json").stdout)
    assert len(rows) == 4                       # everything in the latest generation


def test_new_names_the_prior_generation_for_the_strict_diff(two_generations, no_network):
    res = invoke("new")
    assert f"prior generation: {iso(T_OLD)}" in res.stdout


def test_new_rejects_a_malformed_since(two_generations, no_network):
    res = invoke("new", "--since", "yesterday-ish")
    assert res.exit_code == 2
    assert "invalid --since" in res.stdout


def test_movers_ranks_by_absolute_move_and_skips_the_unquoted(two_generations, no_network):
    rows = json.loads(invoke("movers", "--json").stdout)

    assert [r["ticker"] for r in rows] == ["KXHIGHNY-26AUG12-B85.5"]
    assert (rows[0]["prior_yes_mid"], rows[0]["yes_mid"], rows[0]["move"]) == \
        ("0.41", "0.61", "0.20")
    # B90.5 never moved; VALORANT/UNQUOTED are not in both generations at all.
    assert "KXVALORANT-26AUG12-TEAMA" not in {r["ticker"] for r in rows}


def test_movers_honors_top(settings, no_network):
    old = [mkt(f"KX{i}-26AUG12-B1", f"m{i}", yes_bid="0.10", yes_ask="0.12") for i in range(5)]
    new = [mkt(f"KX{i}-26AUG12-B1", f"m{i}", yes_bid=f"0.{20 + i * 10}",
               yes_ask=f"0.{22 + i * 10}") for i in range(5)]
    first = board.write_generation(settings.board_dir, iter(old), captured_at=T_OLD)
    board.write_generation(settings.board_dir, iter(new), captured_at=T_NEW,
                           prior=first["path"])

    rows = json.loads(invoke("movers", "--top", "2", "--json").stdout)
    assert [r["ticker"] for r in rows] == ["KX4-26AUG12-B1", "KX3-26AUG12-B1"]


def test_movers_needs_two_generations(settings, no_network):
    board.write_generation(settings.board_dir, iter(_gen_new()), captured_at=T_NEW)
    res = invoke("movers")
    assert res.exit_code == 4
    assert "needs two" in res.stdout


def test_a_game_id_shaped_ticker_tail_is_a_label_not_a_strike():
    """Live-board regression (2026-08-12, first refresh): a digits-E-digits tail parses
    as scientific notation whose exponent survives ``is_finite()`` and overflows the
    e4 scaling — the whole board step died on one ticker. Ids keep their label and get
    no sort key; real strikes are untouched."""
    label, strike = board._strike("KXSERIES-1E999999999")
    assert label == "1E999999999" and strike is None
    label, strike = board._strike("KXSERIES-9E999999999999")
    assert label == "9E999999999999" and strike is None
    # The bound does not bite anything price-shaped, including the biggest real ladders.
    assert board._strike("KXBTC-B125000")[1] == 1250000000
    assert board._strike("KXHIGHNY-B85.5")[1] == 855000


def test_board_shows_the_ladder_in_numeric_strike_order(settings, no_network):
    ladder = [
        mkt("KXHIGHNY-26AUG12-B100.5", "NY high above 100.5", yes_bid="0.01", yes_ask="0.03"),
        mkt("KXHIGHNY-26AUG12-B85.5", "NY high above 85.5", yes_bid="0.40", yes_ask="0.42",
            last="0.41"),
        mkt("KXHIGHNY-26AUG12-B90.5", "NY high above 90.5", yes_bid="0.10", yes_ask="0.12"),
    ]
    board.write_generation(settings.board_dir, iter(ladder), captured_at=T_NEW)

    rows = json.loads(invoke("board", "kxhighny", "--json").stdout)

    # Lexicographic order would put B100.5 first; the numeric strike key does not.
    assert [r["strike_label"] for r in rows] == ["B85.5", "B90.5", "B100.5"]
    assert rows[0]["yes_bid"] == "0.40" and rows[0]["yes_ask"] == "0.42"
    assert rows[0]["last_price"] == "0.41" and rows[0]["open_interest"] == 10


def test_board_for_an_unknown_series_is_not_found(two_generations, no_network):
    res = invoke("board", "KXNOSUCHSERIES")
    assert res.exit_code == 4
    assert "no markets for series KXNOSUCHSERIES" in res.stdout


def test_calendar_orders_by_close_time_inside_the_window(two_generations, no_network):
    rows = json.loads(invoke("calendar", "--hours", "12", "--json").stdout)
    assert [r["ticker"] for r in rows] == [
        "KXHIGHNY-26AUG12-B85.5", "KXHIGHNY-26AUG12-B90.5", "KXVALORANT-26AUG12-TEAMA",
    ]

    wide = json.loads(invoke("calendar", "--hours", "200", "--json").stdout)
    assert "KXUNQUOTED-26AUG12-NOQUOTE" in {r["ticker"] for r in wide}
    assert [r["close_time"] for r in wide] == sorted(r["close_time"] for r in wide)


def test_calendar_excludes_what_has_already_closed(settings, no_network):
    rows_in = [
        mkt("KXPAST-26AUG01-B1", "already closed", yes_bid="0.10", yes_ask="0.12",
            close_time=NOW - timedelta(hours=2)),
        mkt("KXSOON-26AUG12-B1", "closes soon", yes_bid="0.10", yes_ask="0.12"),
    ]
    board.write_generation(settings.board_dir, iter(rows_in), captured_at=T_NEW)

    rows = json.loads(invoke("calendar", "--json").stdout)
    assert [r["ticker"] for r in rows] == ["KXSOON-26AUG12-B1"]


# --------------------------------------------------------------------------- cold cache
_COLD = [
    (["series"], "--live"), (["markets"], "--live"), (["search", "x"], "--live"),
    (["board", "KXHIGHNY"], "--live"), (["calendar"], "--live"),
    (["new"], None), (["movers"], None),
]


@pytest.mark.parametrize("args,hint", _COLD, ids=[a[0][0] for a in _COLD])
def test_a_cold_cache_says_so_instead_of_answering_empty(root, no_network, args, hint):
    """"The board is empty" and "nobody has snapshotted the board" are opposite facts, and
    a session that cannot tell them apart concludes the exchange has nothing on it."""
    res = invoke(*args)
    assert res.exit_code == 4, res.stdout
    assert "no board snapshot yet" in res.stdout
    if hint:
        assert hint in res.stdout


# --------------------------------------------------------------------------- live path
@pytest.fixture
def fake_live(monkeypatch):
    fake = _fake_board(settled=True)
    monkeypatch.setattr(bt, "_client", lambda settings: fake)
    return fake


@pytest.mark.parametrize("args", [
    ["series"], ["search", "valorant"], ["board", "KXHIGHNY"], ["calendar", "--hours", "200"],
], ids=["series", "search", "board", "calendar"])
def test_the_live_path_answers_from_the_exchange_with_no_cache_at_all(root, fake_live, args):
    res = invoke(*args, "--live")
    assert res.exit_code == 0, res.stdout
    assert res.stdout.startswith("source: LIVE pull at ")
    assert fake_live.calls_total() > 0
    assert board.generations(load_settings(root=root).board_dir) == []   # nothing written


def test_live_series_rolls_up_the_direct_pull(root, fake_live):
    rows = json.loads(invoke("series", "--live", "--json").stdout)
    assert {r["series"] for r in rows} == {"KXHIGHNY", "KXVALORANT", "KXDONE"}
    assert [r for r in rows if r["series"] == "KXDONE"][0]["n_settled"] == 1


def test_a_live_listing_renders_the_same_columns_as_the_cached_one(root, fake_live):
    """One table shape from both sources: a session comparing a fresh pull against the
    snapshot must not have to reconcile two column sets (docs/14 C2)."""
    live_row = json.loads(invoke("markets", "--live", "--json").stdout)[0]
    assert {"series", "last_price"} <= set(live_row)
    assert live_row["series"] == board.series_of(live_row["ticker"])

    from betting_agent.board import _ROW_SELECT

    cached_keys = {c.strip() for c in _ROW_SELECT.split(",")}
    assert set(live_row) <= cached_keys | {"open_interest"}
    assert "series" in invoke("markets", "--live").stdout.splitlines()[1]


def test_live_beats_a_stale_cache_when_asked(two_generations, monkeypatch):
    fake = _fake_board()
    monkeypatch.setattr(bt, "_client", lambda settings: fake)

    cached = invoke("markets", "--json")
    live = invoke("markets", "--live", "--json")

    assert iso(T_NEW) in cached.stderr
    assert "LIVE pull" in live.stderr
    assert fake.calls_total() > 0


def test_the_live_scan_cap_bites_and_says_so(root, monkeypatch):
    fake = FakeKalshi()
    for i in range(12):
        fake.add_market(f"KXM-26AUG12-B{i}", title=f"m{i}", close_time=CLOSE_SOON,
                        yes_ask=D("0.42"), yes_ask_size=10)
    monkeypatch.setattr(bt, "_client", lambda settings: fake)
    monkeypatch.setattr(bt, "_MARKETS_SCAN_CAP", 4)

    res = invoke("series", "--live")

    assert res.exit_code == 0
    assert "--live stopped after examining 4 markets" in res.stdout


def test_a_live_upstream_failure_is_exit_5(root, monkeypatch):
    class _Boom:
        def iter_markets(self, **kwargs):
            raise KalshiAPIError(503, "boom")

    monkeypatch.setattr(bt, "_client", lambda settings: _Boom())
    assert invoke("series", "--live").exit_code == 5


# --------------------------------------------------------------------------- bt book
def test_bt_book_is_always_live_and_never_reads_the_cache(two_generations, monkeypatch):
    """An order book is what a bet is about to be placed against; a cached one is a lie
    with money on it. ``bt book`` therefore has no cache path at all — with the transport
    gone it fails rather than answering from the snapshot that holds the same ticker."""
    class _NoTransport:
        def get_orderbook(self, ticker, depth=5):
            raise KalshiAPIError(503, "exchange unreachable")

    monkeypatch.setattr(bt, "_client", lambda settings: _NoTransport())

    res = invoke("book", "KXHIGHNY-26AUG12-B85.5")

    assert res.exit_code == 5
    assert "snapshot:" not in res.stdout

    # And with a transport it answers from the exchange, not the cache.
    fake = FakeKalshi()
    fake.add_market("KXHIGHNY-26AUG12-B85.5", title="live book", close_time=CLOSE_SOON,
                    yes_ask=D("0.42"), yes_ask_size=500, no_ask=D("0.58"), no_ask_size=300)
    monkeypatch.setattr(bt, "_client", lambda settings: fake)
    ok = invoke("book", "KXHIGHNY-26AUG12-B85.5")
    assert ok.exit_code == 0
    assert "snapshot:" not in ok.stdout
    assert fake.calls["get_orderbook"] == 1


def test_bt_book_has_no_live_flag_to_need(two_generations, no_network):
    """The flag would imply a cached alternative exists. It must not."""
    res = invoke("book", "KXHIGHNY-26AUG12-B85.5", "--live")
    assert res.exit_code == 2                      # unknown option, not a silent no-op


# --------------------------------------------------------------------------- bounds
def test_the_settled_slice_is_bounded_and_says_so(ledger, settings):
    """``status=settled`` is every market the exchange has ever resolved and the listing
    endpoint this client exposes has no time filter, so it is the one slice that could grow
    without limit. Bounded — and a bounded settled count is announced as a floor."""
    fake = FakeKalshi()
    fake.add_market("KXOPEN-26AUG12-B1", title="open one", close_time=CLOSE_SOON,
                    yes_ask=D("0.42"), yes_ask_size=10)
    for i in range(6):
        fake.add_market(f"KXOLD-26AUG0{i}-B1", title=f"resolved {i}", status="finalized",
                        close_time=NOW - timedelta(days=i + 1))
    settings.board.settled_max = 2

    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)

    assert result["settled_truncated"] is True
    assert result["n_markets"] == 3                 # one open + the two settled taken
    res = invoke("series")
    assert res.exit_code == 0
    assert "`settled` column is a FLOOR" in res.stdout
    # The header states the cap, not just that one was hit: a floor is worth little
    # without the number it was cut at.
    with board.Board.open(result["path"]) as b:
        assert b.meta("settled_max") == "2"


def test_the_settled_slice_is_two_thousand_rows_by_default(ledger, settings):
    """Cut from the spec's 20000: that slice was 57% of the generation, 89% byte-identical
    between refreshes, and it serves one column (``n_settled`` in ``bt series``)."""
    assert settings.board.settled_max == 2000
    result = board.refresh_board_cache(ledger, _fake_board(settled=True), settings, now=NOW)

    with board.Board.open(result["path"]) as b:
        assert b.meta("settled_max") == "2000"      # the cap the generation was built with


def test_an_unbounded_settled_slice_makes_no_such_claim(ledger, settings):
    fake = _fake_board(settled=True)
    result = board.refresh_board_cache(ledger, fake, settings, now=NOW)
    assert result["settled_truncated"] is False
    assert "FLOOR" not in invoke("series").stdout


def test_max_markets_is_a_hard_stop_on_the_whole_pull(ledger, settings):
    fake = FakeKalshi()
    for i in range(20):
        fake.add_market(f"KXM-26AUG12-B{i}", title=f"m{i}", close_time=CLOSE_SOON,
                        yes_ask=D("0.42"), yes_ask_size=10)
    settings.board.max_markets = 5

    assert board.refresh_board_cache(ledger, fake, settings, now=NOW)["n_markets"] == 5


def test_an_unreadable_snapshot_is_named_not_a_traceback(settings, two_generations,
                                                         no_network):
    """A generation is published atomically so a TORN one is impossible, but a file can
    still go bad on disk — and BT-2's lesson is that a raw traceback out of the toolkit is
    indistinguishable to a session from an upstream failure."""
    latest = board.generations(settings.board_dir)[0]
    latest.write_bytes(b"SQLite format 3\x00" + b"\x00" * 4000)   # a plausible-looking ruin

    res = invoke("series")

    assert res.exit_code == 5, res.stdout
    assert "board snapshot is unreadable" in res.stdout
    assert "Traceback" not in res.stdout


# --------------------------------------------------------------------------- read-only
def test_no_lens_modifies_the_cache(two_generations, no_network):
    """``bt`` is read-only by construction, and the cache is now part of what that means:
    running every lens must leave the generation files byte-identical."""
    _first, second = two_generations
    board_dir = second["path"].parent

    def _fingerprint():
        return {p.name: (p.stat().st_size, p.read_bytes()[:64])
                for p in sorted(board_dir.iterdir())}

    before = _fingerprint()
    for args in _OFFLINE_LENSES:
        assert invoke(*args).exit_code == 0
    assert _fingerprint() == before


def test_markets_out_writes_the_cached_result_and_names_the_snapshot(
    two_generations, no_network, tmp_path, monkeypatch
):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    res = invoke("markets", "--out", "board.json", "--closing-within", "200")

    assert res.exit_code == 0, res.stdout
    assert "wrote 4 markets to" in res.stdout
    assert iso(T_NEW) in res.stdout                  # provenance travels with the file
    rows = json.loads((workdir / "board.json").read_text())
    assert {r["ticker"] for r in rows} == {m.ticker for m in _gen_new()}
    # The default 72 h bound is unchanged by the new source: one fixture closes in 100 h.
    assert "wrote 3 markets to" in invoke("markets", "--out", "narrow.json").stdout


# --------------------------------------------------------------------------- wiring
def test_the_cache_directory_comes_from_settings_not_a_constant(tmp_path):
    """Nothing hardcodes ``data/board``: point Settings elsewhere and the cache follows —
    which is what keeps this whole suite off the live tree."""
    (tmp_path / "elsewhere").mkdir()
    s = load_settings(root=tmp_path / "elsewhere")
    assert s.board_dir == tmp_path / "elsewhere" / "data" / "board"
    board.write_generation(s.board_dir, iter(_gen_old()), captured_at=T_OLD)
    assert (tmp_path / "elsewhere" / "data" / "board").is_dir()


def test_init_creates_the_board_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("BT_ROOT", str(tmp_path))
    assert runner.invoke(cli.app, ["init"]).exit_code == 0
    assert load_settings(root=tmp_path).board_dir.is_dir()


def test_the_board_defaults_are_the_spec_values(settings):
    """docs/22 section 6 states them: 120 hours of close window, one family and two
    categories left out. Two generations are kept rather than one, because ``bt movers``
    compares the newest against the one before it and a bounded generation is small enough
    that the pair costs little. The cadence is 60 rather than the spec's 150: the spec
    priced a refresh at about 650 requests, and carrying series categories across
    generations took most of those away."""
    assert settings.board.refresh_min_interval_min == 60
    assert settings.board.generations_keep == 2
    assert settings.board.close_bound_hours == 120
    assert settings.board.excluded_series == ["KXMVECROSS"]
    assert settings.board.excluded_categories == ["Sports", "Entertainment"]
    assert board.board_interval(settings) == timedelta(minutes=60)
