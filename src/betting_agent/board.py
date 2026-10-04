"""The shared board cache: periodic snapshots of the whole market listing
(docs/14 C1, docs/22 section 6).

Why this exists (docs/12 §3): every attempt independently re-derived the same workaround
for the ~400,000 dead multigame-parlay rows that flood the listing — escalating
``--limit`` pulls, min-volume filters, 233MB of JSON left in one workspace — a five-to-nine
minute tax on every session before any research began. Nine sessions a day cannot each
pull the board. So the tick pulls it once an hour and every listing tool reads that
snapshot; only order books stay live (``bt book``), because a book is what you are about
to trade against.

Layout — one file per generation, under ``settings.board_dir``:

    data/board/board-20260810T140000Z.sqlite3     newest generation
    data/board/board-20260810T130000Z.sqlite3     prior generation

**Storage is SQLite, one file per generation.** The listing ran 30k-400k rows in the wild
and every lens over it is a query, not a scan: ``bt series`` is a GROUP BY, ``bt markets``
is a filter plus an ORDER BY, ``bt calendar`` is an index range, ``bt movers`` is a join
against the prior generation. JSONL would make each of those a full parse of a
hundred-megabyte file inside a session's Python process, per invocation; SQLite makes them
indexed queries against a file the reader never has to hold in memory, and lets the
*writer* stream pages straight to disk instead of accumulating 400k model objects.

Measured at the wild upper bound (400,000 rows, 200 series plus one 200k-row parlay family,
rules text on every row): a build takes 11-12 s and peaks at ~50 MB of process memory —
which is the streaming design paying off, since the rows themselves never accumulate — and
the generation is ~133 MB on disk, so the two-generation retention costs ~270 MB, bounded
and self-limiting rather than growing like the logs the GC had to be built for. Query
times off that file: ``series`` 0.74 s, ``markets`` 0.07 s, ``search`` 0.23 s, ``new``
0.23 s, ``movers`` 0.24 s, ``board`` 0.02 s, ``calendar`` 0.17 s. Against the alternative
each session was paying — a five-to-nine minute survey tax and a 233 MB JSON dump — this is
the whole point.

**A reader never sees a torn file.** A generation is built under a dot-prefixed temp name
that cannot match the reader's glob (:data:`_GEN_GLOB`), then published with a single
``os.replace``. A crash mid-build leaves a temp file nobody reads and the previous
generation still serving; :func:`generations` is therefore always a list of complete
snapshots. Readers open with SQLite's ``mode=ro`` URI, so the toolkit cannot write the
cache even by accident.

**Timestamps are the point.** Every tool prints the snapshot's ``captured_at`` (stored in
the generation's own ``meta`` table, not inferred from mtime, which a copy would change).
A stale snapshot is fine as long as nobody mistakes it for now — the failure mode this
package exists to prevent is a session reasoning about a board that moved an hour ago
without knowing it.

**``first_seen`` carries forward.** Only two generations are kept, so "what listed since
yesterday" cannot be a two-file diff. Instead each build inherits every surviving ticker's
``first_seen`` from the prior generation, which makes ``bt new`` a range scan over any
window — and makes a delisted market simply absent rather than newly appearing. The very
first generation in a cache has no prior to inherit from, so its ``carried_forward`` meta
flag is ``0`` and ``bt new`` says it cannot distinguish new from merely-first-seen yet.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from betting_agent.kalshi.types import Market, parse_price
from betting_agent.timeutil import iso, parse_iso, utc_now

# A generation's file name carries its capture time in compact ISO basic form, which sorts
# lexicographically in chronological order — so "newest" is a sort, not a stat() fan-out.
_GEN_PREFIX = "board-"
_GEN_SUFFIX = ".sqlite3"
_GEN_GLOB = f"{_GEN_PREFIX}*{_GEN_SUFFIX}"
# Deliberately dot-prefixed: a half-written generation MUST NOT match _GEN_GLOB.
_TMP_PREFIX = ".board-"
_TMP_SUFFIX = ".tmp"

# Prices are stored as TEXT (the codebase's Decimal-as-TEXT convention) and ranked through
# an integer companion column: 10^-4 dollars, the ledger's money resolution. Ranking on
# TEXT would be lexicographic and ranking on REAL would put floats in the middle of a
# number a session reads — an integer does neither.
_E4 = Decimal(10000)
_TWO = Decimal(2)
# Largest order of magnitude a ticker-tail value can have and still be a strike (see
# _strike): ``Decimal.adjusted()`` of 12 admits values below 10^13. Dollar ladders top
# out around 10^5 (BTC); the headroom keeps the e4 scaling (10^17 worst case) far inside
# both Decimal's Emax and SQLite's signed-64-bit INTEGER. adjusted() is pure exponent
# inspection — even abs() on these values is a context operation that itself overflows.
_STRIKE_MAX_ADJUSTED = 12

# ``status`` as the market payload carries it: "active" when open, "finalized" when
# resolved (kalshi/types.py, verified live). Both vocabularies appear in the wild.
_SETTLED_STATUSES = frozenset({"settled", "finalized"})

# Kalshi caps one ``/markets`` page at 500 rows server-side (kalshi/client.py).
_PAGE_CAP = 500
# Rows inserted per executemany batch while streaming a pull to disk.
_INSERT_BATCH = 2000

_SCHEMA = """
CREATE TABLE markets (
    ticker        TEXT PRIMARY KEY,
    series        TEXT NOT NULL,
    event_ticker  TEXT,
    title         TEXT,
    category      TEXT,
    status        TEXT,
    is_settled    INTEGER NOT NULL DEFAULT 0,
    close_time    TEXT,
    yes_bid       TEXT,
    yes_ask       TEXT,
    no_bid        TEXT,
    no_ask        TEXT,
    last_price    TEXT,
    yes_mid_e4    INTEGER,
    strike_label  TEXT,
    strike_e4     INTEGER,
    volume        INTEGER,
    open_interest INTEGER,
    rules         TEXT,
    first_seen    TEXT NOT NULL
);
CREATE INDEX markets_series ON markets(series);
CREATE INDEX markets_close ON markets(close_time);
CREATE INDEX markets_volume ON markets(volume);
CREATE INDEX markets_first_seen ON markets(first_seen);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- Every series this generation knows a category for, written BEFORE the excluded
-- categories are deleted, so the next build inherits the answer instead of paying one
-- event lookup per series again (see _enrich_categories). It holds the excluded families
-- too: those are the ones whose rows are about to go, and re-resolving them every
-- generation was the largest single cost in the refresh.
CREATE TABLE series_category (series TEXT PRIMARY KEY, category TEXT);
"""

_MARKET_COLUMNS = (
    "ticker", "series", "event_ticker", "title", "category", "status", "is_settled",
    "close_time", "yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "yes_mid_e4",
    "strike_label", "strike_e4", "volume", "open_interest", "rules", "first_seen",
)
_INSERT_SQL = (
    f"INSERT OR REPLACE INTO markets ({', '.join(_MARKET_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_MARKET_COLUMNS))})"
)

# The columns a listing row hands back, in the order ``bt`` prints them. A superset of the
# pre-cache ``bt markets --json`` shape, so a session's existing parsing keeps working.
_ROW_SELECT = (
    "ticker, series, title, category, status, close_time, yes_bid, yes_ask, no_bid, "
    "no_ask, last_price, volume, open_interest, strike_label, first_seen"
)
_DECIMAL_FIELDS = ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price")

# The listing lenses answer about markets a session could actually bet (docs/14 C2). The
# cache also holds the ``status=settled`` slice, but that slice exists for ONE consumer —
# ``bt series``' settled-market count (see :class:`~betting_agent.config.BoardSettings`) —
# and it is up to ``board.settled_max`` rows of resolved history against a few thousand
# open ones. Unfiltered it swamps every listing: the default ``--sort close`` is ascending,
# so the oldest resolutions sort FIRST, and ``--sort volume`` is no escape either because a
# resolved market has accumulated its whole lifetime's volume. Before the cache, ``bt
# markets`` pulled ``status="open"`` only; these lenses keep that population.
#
# ``bt calendar`` and ``bt movers`` need no such clause and deliberately do not carry one:
# the calendar's window has a lower bound at ``now``, and a mover must be quoted in both
# generations, which a resolved market is not. ``series_rollup`` must count settled rows.
_TRADEABLE = "is_settled = 0"

_MARKET_SORTS = {
    # A stated sort over facts, never a ranking by attractiveness (bt.py's neutrality
    # rule): the caller names the column and the direction is the obvious one for it.
    #
    # Bare columns, not ``COALESCE(x, 0) DESC``: SQLite cannot use an index for an
    # expression, and the two forms are identical here anyway — DESC puts NULLs last,
    # exactly where a zero would land. Measured on a 400k-row generation, ``--sort volume``
    # is 0.07s this way.
    "volume": "volume DESC, ticker",
    "oi": "open_interest DESC, ticker",
    "close": "close_time IS NULL, close_time, ticker",
    "ticker": "ticker",
}
_SERIES_SORTS = {
    "volume": "volume DESC, series",
    "oi": "open_interest DESC, series",
    "markets": "n_markets DESC, series",
    "close": "soonest_close IS NULL, soonest_close, series",
    "series": "series",
}


def market_sorts() -> tuple[str, ...]:
    """The ``--sort`` vocabulary for market listings (the CLI validates against this)."""
    return tuple(_MARKET_SORTS)


def series_sorts() -> tuple[str, ...]:
    return tuple(_SERIES_SORTS)


# --------------------------------------------------------------------------- naming
def stamp(dt: datetime) -> str:
    """The compact UTC stamp used in a generation's file name."""
    return iso(dt).replace("-", "").replace(":", "")


def generations(board_dir: Path) -> list[Path]:
    """Every COMPLETE generation, newest first. Partial builds are unmatchable by name."""
    d = Path(board_dir)
    if not d.is_dir():
        return []
    return sorted((p for p in d.glob(_GEN_GLOB) if p.is_file()), reverse=True)


def rotate(board_dir: Path, *, keep: int = 2, now: datetime | None = None) -> list[Path]:
    """Delete all but the ``keep`` newest generations, and any stale temp file.

    A build that dies between ``_connect_new`` and ``os.replace`` leaves its dot-prefixed
    temp file behind. No reader can glob it, so it is harmless, but nothing ever removed it
    either and a generation is tens of megabytes. Anything older than a day cannot belong to
    a live build (a pull is minutes), so it is swept here, where the disk is already being
    tidied. Returns what was removed.
    """
    removed: list[Path] = []
    for path in generations(board_dir)[max(1, keep):]:
        try:
            path.unlink()
        except OSError:  # a generation we cannot remove is not worth failing the refresh
            continue
        removed.append(path)
    removed += _sweep_temp_files(board_dir, now=now or utc_now())
    return removed


def _sweep_temp_files(board_dir: Path, *, now: datetime) -> list[Path]:
    """Delete abandoned ``.board-*.tmp`` builds older than a day. Never a live one."""
    d = Path(board_dir)
    if not d.is_dir():
        return []
    cutoff = (now - timedelta(days=1)).timestamp()
    removed: list[Path] = []
    for path in sorted(d.glob(f"{_TMP_PREFIX}*{_TMP_SUFFIX}")):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            path.unlink()
        except OSError:  # same rule as a generation: tidying must not fail the refresh
            continue
        removed.append(path)
    return removed


# --------------------------------------------------------------------------- row building
def _series_excluded(series: str, excluded) -> bool:
    """True when ``series`` starts with any entry of ``board.excluded_series``."""
    return any(series.startswith(prefix) for prefix in excluded)


def series_of(ticker: str) -> str:
    """The series (family) a ticker belongs to: everything before its first dash.

    ``KXHIGHNY-26JUL08-B85.5`` -> ``KXHIGHNY``. This is a *heuristic* — the authoritative
    series_ticker lives on the parent event (kalshi/client.py says so explicitly) and
    resolving it per market would be one request per row. docs/14 C2 asks for the ticker
    prefix by name, which is what the live board's naming actually encodes, and the cost of
    the honest version is a hundred thousand requests an hour.
    """
    return ticker.split("-", 1)[0] if "-" in ticker else ticker


def _strike(ticker: str) -> tuple[str | None, int | None]:
    """``(label, strike x 10^4)`` from a ticker's last segment, e.g. ``B85.5`` -> 855000.

    A non-numeric segment (``-TEAM``, a single-market series) keeps its label and gets no
    sort key; the ladder then orders those by ticker after the numeric strikes.
    """
    if "-" not in ticker:
        return None, None
    label = ticker.rsplit("-", 1)[1] or None
    if label is None:
        return None, None
    digits = label.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    try:
        value = Decimal(digits)
    except (InvalidOperation, ValueError):
        return label, None
    # A digits-E-digits segment (game-id-shaped tails) parses as scientific notation with
    # an exponent big enough that ``is_finite()`` is true and the * 10^4 scaling raises
    # decimal.Overflow — seen on the live board's first refresh (2026-08-12). No real
    # strike is anywhere near the bound; anything past it is an id, not a price level.
    if not value.is_finite() or value.adjusted() > _STRIKE_MAX_ADJUSTED:
        return label, None
    return label, int((value * _E4).to_integral_value(rounding=ROUND_HALF_UP))


def _mid_e4(yes_bid: Decimal | None, yes_ask: Decimal | None) -> int | None:
    """The YES-side mid in 10^-4 dollars, or one side when only one is quoted.

    ``None`` when neither side is quoted — a market with no price cannot have moved, and
    ``bt movers`` must not manufacture a delta out of a missing quote.
    """
    prices = [p for p in (yes_bid, yes_ask) if p is not None and p.is_finite()]
    if not prices:
        return None
    mid = (prices[0] + prices[1]) / _TWO if len(prices) == 2 else prices[0]
    return int((mid * _E4).to_integral_value(rounding=ROUND_HALF_UP))


def _rules_text(raw: dict) -> str | None:
    parts = [str(raw.get(k) or "").strip() for k in ("rules_primary", "rules_secondary")]
    text = " ".join(p for p in parts if p)
    return text or None


def _price_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def market_row(market: Market, *, first_seen: str) -> tuple:
    """One market as its cache row, in :data:`_MARKET_COLUMNS` order."""
    raw = market.raw or {}
    label, strike_e4 = _strike(market.ticker)
    status = (market.status or "").lower()
    last = parse_price(raw, "last_price")
    return (
        market.ticker,
        series_of(market.ticker),
        market.event_ticker,
        market.title,
        market.category,
        market.status,
        1 if status in _SETTLED_STATUSES else 0,
        iso(market.close_time) if market.close_time else None,
        _price_text(market.yes_bid),
        _price_text(market.yes_ask),
        _price_text(market.no_bid),
        _price_text(market.no_ask),
        _price_text(last),
        _mid_e4(market.yes_bid, market.yes_ask),
        label,
        strike_e4,
        market.volume,
        market.open_interest,
        _rules_text(raw),
        first_seen,
    )


# --------------------------------------------------------------------------- writing
def _connect_new(path: Path) -> sqlite3.Connection:
    # ``uri=True`` is not about this file (its name is an ordinary path, and SQLite only
    # reads a name as a URI when it starts with "file:"): it is what lets the connection
    # ATTACH the prior generation read-only in _carry_first_seen. SQLite only parses a URI
    # filename in ATTACH when the connection itself was opened with URI handling on, so
    # without this the attach fails, the carry-forward silently does nothing, and every
    # generation reports the whole board as first seen this minute.
    conn = sqlite3.connect(path, isolation_level=None, uri=True)
    # A generation is a rebuildable cache, not a ledger: durability pragmas buy nothing
    # here, and a crash mid-build must lose the TEMP file (which no reader can see) rather
    # than cost an hour of the tick's time.
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(_SCHEMA)
    return conn


def _meta_set(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))


def write_generation(
    board_dir: Path,
    markets: Iterable[Market],
    *,
    captured_at: datetime,
    prior: Path | None = None,
    enrich_category: Callable[[str], str | None] | None = None,
    category_cap: int = 2000,
    max_markets: int | None = None,
    settled_truncated: Callable[[], bool] | None = None,
    close_bound_hours: int | None = None,
    excluded_series: Sequence[str] = (),
    excluded_categories: Sequence[str] = (),
    category_refusal_text: str | None = None,
    settled_max: int | None = None,
) -> dict:
    """Build one generation and publish it atomically. Returns the build's stats.

    ``markets`` is consumed as a stream and written in batches, so a 400,000-row board
    never exists as 400,000 objects in memory. ``prior`` (a published generation) supplies
    the ``first_seen`` carry-forward AND the known series categories. ``enrich_category``
    is called at most once per distinct series the prior generation did not already know a
    category for. Categories live on the parent event, bulk listings carry none, and a
    family's category is a property of the family (kalshi/client.py's ``event_category``,
    cached there too).

    The last five arguments are the generation's header (docs/22 section 6): what the pull
    was bounded to and what it left out, recorded in the file itself so :meth:`Board.label`
    can state it on every listing rather than leaving a session to wonder why a market it
    expected is missing. ``excluded_categories`` is the one that acts here, because a
    category is only known after the enrichment, so those rows are written and then deleted
    before the generation is published; the bound and the series exclusion happened upstream
    in the pull and are only recorded here. ``category_refusal_text`` is the exchange's own
    words for why it will not take our orders in those categories, set by the operator.
    ``settled_max`` is recorded rather than applied here (the pull enforces it): a settled
    count that is a floor is worth little without the number it was cut at.

    The file is published with a single ``os.replace`` from a name no reader can glob, so
    ``generations()`` only ever lists complete snapshots.
    """
    d = Path(board_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamped = stamp(captured_at)
    tmp = d / f"{_TMP_PREFIX}{stamped}{_GEN_SUFFIX}{_TMP_SUFFIX}"
    final = d / f"{_GEN_PREFIX}{stamped}{_GEN_SUFFIX}"
    tmp.unlink(missing_ok=True)

    conn = _connect_new(tmp)
    try:
        n = _insert_stream(conn, markets, first_seen=iso(captured_at), max_markets=max_markets)
        carried = _carry_first_seen(conn, prior) if prior is not None else 0
        enriched, carried_categories, capped = _enrich_categories(
            conn, enrich_category, category_cap, prior=prior
        )
        # BEFORE the drop: an excluded family's category is exactly the one worth keeping,
        # since its rows are about to be deleted and it would otherwise be looked up again
        # every generation for as long as the exchange keeps refusing our orders in it.
        _record_series_categories(conn)
        excluded_rows = _drop_excluded_categories(conn, excluded_categories)
        if excluded_rows:
            n = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
        _meta_set(conn, "captured_at", iso(captured_at))
        _meta_set(conn, "n_markets", n)
        if close_bound_hours is not None:
            _meta_set(conn, "close_bound_hours", int(close_bound_hours))
        _meta_set(conn, "excluded_series", ", ".join(excluded_series))
        _meta_set(conn, "excluded_categories", ", ".join(excluded_categories))
        _meta_set(conn, "excluded_category_rows", excluded_rows)
        if category_refusal_text:
            _meta_set(conn, "category_refusal_text", category_refusal_text)
        _meta_set(conn, "carried_forward", 1 if prior is not None else 0)
        _meta_set(conn, "prior_generation", prior.name if prior is not None else "")
        _meta_set(conn, "categories_resolved", enriched)
        # Split on purpose: ``categories_resolved`` is what this build spent requests on and
        # ``categories_carried`` is what the prior generation already knew, which is the
        # difference between a 580-request refresh and a 30-request one.
        _meta_set(conn, "categories_carried", carried_categories)
        _meta_set(conn, "category_lookup_capped", 1 if capped else 0)
        if settled_max is not None:
            _meta_set(conn, "settled_max", int(settled_max))
        # Read AFTER the stream is drained: the flag is only true once the pull has hit its
        # bound, and it is the reason a settled count can be a floor rather than a total.
        cut = bool(settled_truncated()) if settled_truncated is not None else False
        _meta_set(conn, "settled_truncated", 1 if cut else 0)
        n_series = conn.execute("SELECT COUNT(DISTINCT series) FROM markets").fetchone()[0]
        _meta_set(conn, "n_series", n_series)
        conn.close()
        os.replace(tmp, final)
    except BaseException:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        tmp.unlink(missing_ok=True)
        raise
    return {
        "path": final, "generation": final.name, "n_markets": n, "n_series": n_series,
        "carried_first_seen": carried, "categories_resolved": enriched,
        "categories_carried": carried_categories,
        "category_lookup_capped": capped, "settled_truncated": cut,
        "excluded_category_rows": excluded_rows,
        "captured_at": iso(captured_at),
    }


def _insert_stream(
    conn: sqlite3.Connection,
    markets: Iterable[Market],
    *,
    first_seen: str,
    max_markets: int | None,
) -> int:
    batch: list[tuple] = []
    n = 0
    for market in markets:
        if not getattr(market, "ticker", None):
            continue
        batch.append(market_row(market, first_seen=first_seen))
        n += 1
        if len(batch) >= _INSERT_BATCH:
            conn.executemany(_INSERT_SQL, batch)
            batch.clear()
        if max_markets is not None and n >= max_markets:
            break
    if batch:
        conn.executemany(_INSERT_SQL, batch)
    return conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]


def _carry_first_seen(conn: sqlite3.Connection, prior: Path) -> int:
    """Inherit ``first_seen`` for every ticker the prior generation already knew.

    Set-based on purpose: the alternative is a dict of 400,000 tickers in the tick's
    memory. A prior file that cannot be opened is not fatal — the generation is still
    correct, it just cannot tell new listings from first sightings, so the flag says so.
    """
    try:
        conn.execute("ATTACH DATABASE ? AS prior", (f"file:{prior}?mode=ro",))
    except sqlite3.Error:
        _meta_set(conn, "carry_forward_error", f"cannot attach {prior.name}")
        return 0
    try:
        cur = conn.execute(
            "UPDATE markets SET first_seen = ("
            "  SELECT p.first_seen FROM prior.markets p WHERE p.ticker = markets.ticker"
            ") WHERE EXISTS ("
            "  SELECT 1 FROM prior.markets p WHERE p.ticker = markets.ticker"
            ")"
        )
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except sqlite3.Error:
        _meta_set(conn, "carry_forward_error", f"cannot read {prior.name}")
        return 0
    finally:
        try:
            conn.execute("DETACH DATABASE prior")
        except sqlite3.Error:
            pass


def _enrich_categories(
    conn: sqlite3.Connection,
    enrich: Callable[[str], str | None] | None,
    cap: int,
    *,
    prior: Path | None = None,
) -> tuple[int, int, bool]:
    """Categories for every series: carried from the prior generation, else looked up.

    Bulk listings carry no category at all (kalshi/types.py), so without this every
    category column in the cache would read ``-`` and ``bt markets --category`` — which
    works today via live event lookups — would answer "none" against the cache. One
    lookup per *series* rather than per market or per event is what makes it affordable
    at board scale.

    The lookups were still the refresh's largest cost by far (about 500 of about 650
    requests, most of them on the Sports and Entertainment families that are deleted
    minutes later), and they bought almost nothing: measured on the live board, about 26
    of about 220 series are new from one generation to the next. So the prior generation's
    ``series_category`` table seeds this one first and only the series it does not name are
    looked up. A series whose category was never resolved is absent from that table, so it
    is tried again; the cap bounds the NEW lookups, and is recorded so a tool can be honest
    about a partial answer.

    Returns ``(looked_up, carried, capped)``.
    """
    carried = _seed_categories(conn, prior) if prior is not None else 0
    if enrich is None or cap <= 0:
        return 0, carried, False
    rows = conn.execute(
        "SELECT series, MIN(event_ticker) FROM markets "
        "WHERE category IS NULL AND event_ticker IS NOT NULL "
        f"GROUP BY series ORDER BY series LIMIT {int(cap) + 1}"
    ).fetchall()
    capped = len(rows) > cap
    resolved = 0
    for series, event_ticker in rows[:cap]:
        try:
            category = enrich(event_ticker)
        except Exception:  # noqa: BLE001 - a lookup failure leaves the category unknown
            continue
        if category:
            conn.execute(
                "UPDATE markets SET category = ? WHERE series = ? AND category IS NULL",
                (category, series),
            )
            resolved += 1
    return resolved, carried, capped


def _seed_categories(conn: sqlite3.Connection, prior: Path) -> int:
    """Fill categories from the prior generation's ``series_category``. Returns how many
    series were answered that way.

    Attached read-only and tolerant of a prior that cannot be read or predates the table:
    a missing carry-over costs requests, not correctness, so it must never fail a build.
    """
    try:
        conn.execute("ATTACH DATABASE ? AS cats", (f"file:{prior}?mode=ro",))
    except sqlite3.Error:
        return 0
    try:
        (carried,) = conn.execute(
            "SELECT COUNT(DISTINCT m.series) FROM markets m JOIN cats.series_category c"
            " ON c.series = m.series WHERE m.category IS NULL AND c.category IS NOT NULL"
        ).fetchone()
        conn.execute(
            "UPDATE markets SET category = ("
            "  SELECT c.category FROM cats.series_category c WHERE c.series = markets.series"
            ") WHERE category IS NULL AND EXISTS ("
            "  SELECT 1 FROM cats.series_category c WHERE c.series = markets.series"
            "  AND c.category IS NOT NULL"
            ")"
        )
        return int(carried or 0)
    except sqlite3.Error:  # a prior built before this table exists simply seeds nothing
        _meta_set(conn, "category_carry_error", f"cannot read {prior.name}")
        return 0
    finally:
        try:
            conn.execute("DETACH DATABASE cats")
        except sqlite3.Error:
            pass


def _record_series_categories(conn: sqlite3.Connection) -> int:
    """Store this generation's series-to-category map for the next build to inherit."""
    cur = conn.execute(
        "INSERT OR REPLACE INTO series_category (series, category) "
        "SELECT series, MIN(category) FROM markets WHERE category IS NOT NULL GROUP BY series"
    )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def _drop_excluded_categories(conn: sqlite3.Connection, categories: Sequence[str]) -> int:
    """Delete the rows of every excluded category. Returns how many went (docs/22 §6).

    Deleted rather than filtered by each lens: the exchange refuses our orders in these
    categories, so a market in one is not a market this account can bet, and the whole
    cost of the category block (docs/18) was sessions researching markets whose orders were
    never going to be accepted. The category is matched exactly as the exchange spells it,
    which is what ``_enrich_categories`` stored.
    """
    names = [c for c in categories if c]
    if not names:
        return 0
    placeholders = ", ".join("?" * len(names))
    cur = conn.execute(f"DELETE FROM markets WHERE category IN ({placeholders})", tuple(names))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


# --------------------------------------------------------------------------- refresh (tick)
def board_interval(settings) -> timedelta:
    minutes = int(getattr(getattr(settings, "board", None), "refresh_min_interval_min", 60))
    return timedelta(minutes=max(0, minutes))


def cache_age(board_dir: Path, now: datetime) -> timedelta | None:
    """How old the newest generation's *capture* is, or ``None`` when there is none."""
    latest = generations(board_dir)[:1]
    if not latest:
        return None
    captured = read_captured_at(latest[0])
    return None if captured is None else now - captured


def read_captured_at(path: Path) -> datetime | None:
    """A generation's capture time from its own ``meta`` table, else from its file name.

    Never mtime: a copy, a restore or a backup pass rewrites mtime, and a snapshot that
    lies about its age is worse than one that admits it is old.
    """
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        conn = None
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='captured_at'").fetchone()
            if row and row[0]:
                return parse_iso(row[0])
        except (sqlite3.Error, ValueError):
            pass
        finally:
            conn.close()
    name = path.name
    if name.startswith(_GEN_PREFIX) and name.endswith(_GEN_SUFFIX):
        raw = name[len(_GEN_PREFIX):-len(_GEN_SUFFIX)]
        try:
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def board_statuses(settings) -> tuple[str, ...]:
    raw = getattr(getattr(settings, "board", None), "statuses", None) or ["open", "settled"]
    return tuple(str(s) for s in raw)


class _Pull:
    """Streams the board across statuses, deduplicated by ticker, and reports what it cut.

    The first status that yields a ticker wins, so an ``open`` row is never overwritten by a
    stale ``settled`` copy of itself. Only the ticker set is held in memory (strings, not
    models) — the rows themselves stream straight to disk.

    ``settled_max`` bounds the one slice with no natural end: ``status=settled`` is every
    market the exchange has ever resolved, and there is no time filter on the listing
    endpoint this client exposes. When the bound bites, :attr:`settled_truncated` says so,
    which is what lets ``bt series`` call its settled counts a floor instead of quietly
    presenting a partial total as a total.

    ``max_close_ts`` is the open slice's bound and the only one the exchange applies for
    us: the rows never leave the server, which is the whole difference between a 1.5 GB
    generation and a small one (docs/22 section 6). It is a Unix timestamp in seconds, the
    unit the ``/markets`` parameter of that name takes. The settled slice keeps its own
    ``settled_max`` cap instead: those markets closed in the past, so a future bound would
    take all of them.

    ``excluded_series`` drops a family before it is ever written, so an excluded series is
    absent from the generation entirely rather than filtered by each reader in turn.
    """

    def __init__(self, client, statuses: Iterable[str], *, settled_max: int | None,
                 max_close_ts: int | None = None,
                 excluded_series: Iterable[str] = ()):
        self._client = client
        self._statuses = list(statuses)
        self._settled_max = settled_max
        self._max_close_ts = max_close_ts
        self._excluded_series = frozenset(excluded_series)
        self.settled_truncated = False
        self.n_settled_taken = 0

    def __iter__(self) -> Iterator[Market]:
        seen: set[str] = set()
        for status in self._statuses:
            bound = None if status == "open" else self._settled_max
            max_close_ts = self._max_close_ts if status == "open" else None
            taken = 0
            for market in self._client.iter_markets(
                status=status, limit=_PAGE_CAP, max_close_ts=max_close_ts
            ):
                if bound is not None and taken >= bound:
                    self.settled_truncated = True
                    break
                ticker = getattr(market, "ticker", None)
                if not ticker or ticker in seen:
                    continue
                # An entry is a prefix: the live parlay families are KXMVECROSSCATEGORY and
                # KXMVECROSSCATEGORY0 (millions of short-dated markets), not KXMVECROSS.
                if _series_excluded(series_of(ticker), self._excluded_series):
                    continue
                seen.add(ticker)
                taken += 1
                if status != "open":
                    self.n_settled_taken += 1
                yield market


def refresh_board_cache(ledger, client, settings, *, now: datetime | None = None,
                        force: bool = False) -> dict:
    """The board pull: refresh ``data/board`` when the newest snapshot is stale.

    Bounded by ``board.refresh_min_interval_min`` (60 by default, docs/22 section 6) and
    derived from the newest generation's own capture time rather than from tick
    bookkeeping, so a restart, a HALT or a missed tick cannot make it refetch early or
    forget to refetch at all. Without a client (no creds) it is a quiet no-op: the stale
    snapshot keeps serving with its honest timestamp, which is exactly the degradation the
    tools are built for.

    What the pull is allowed to retrieve is three settings wide, and all three are recorded
    in the generation's header: markets closing inside ``board.close_bound_hours``, minus
    ``board.excluded_series``, minus ``board.excluded_categories``.
    """
    now = now or utc_now()
    board_dir = settings.board_dir
    keep = max(1, int(getattr(getattr(settings, "board", None), "generations_keep", 2)))
    age = cache_age(board_dir, now)
    interval = board_interval(settings)
    if not force and age is not None and age < interval:
        return {"status": "fresh", "age_seconds": int(age.total_seconds())}
    if client is None:
        return {"status": "no_client"}

    gens = generations(board_dir)
    # A generation's name is its capture second, so a rebuild inside the same second would
    # publish over the file it is carrying ``first_seen`` forward from and leave one
    # generation where there were two. Unreachable under the refresh interval; reachable
    # with ``force``, which is the only caller that can ask twice in a second.
    if gens and stamp(now) == gens[0].name[len(_GEN_PREFIX):-len(_GEN_SUFFIX)]:
        return {"status": "fresh", "age_seconds": 0, "same_second": True}

    prior = gens[0] if gens else None
    enrich = getattr(client, "event_category", None)
    cfg = getattr(settings, "board", None)
    close_bound_hours = int(getattr(cfg, "close_bound_hours", 120))
    excluded_series = list(getattr(cfg, "excluded_series", None) or ())
    excluded_categories = list(getattr(cfg, "excluded_categories", None) or ())
    settled_max = getattr(cfg, "settled_max", 2000)
    pull = _Pull(
        client, board_statuses(settings),
        settled_max=settled_max,
        # Seconds since the epoch is the unit the ``/markets`` listing takes.
        max_close_ts=int((now + timedelta(hours=close_bound_hours)).timestamp()),
        excluded_series=excluded_series,
    )
    stats = write_generation(
        board_dir,
        pull,
        captured_at=now,
        prior=prior,
        enrich_category=(
            (lambda ev: enrich(ev, raise_on_error=False)) if callable(enrich) else None
        ),
        category_cap=int(getattr(cfg, "category_lookup_cap", 2000)),
        max_markets=getattr(cfg, "max_markets", None),
        settled_truncated=lambda: pull.settled_truncated,
        close_bound_hours=close_bound_hours,
        excluded_series=excluded_series,
        excluded_categories=excluded_categories,
        category_refusal_text=ledger.meta_get("category_refusal_text"),
        settled_max=settled_max,
    )
    removed = rotate(board_dir, keep=keep, now=now)
    detail = {
        "generation": stats["generation"], "n_markets": stats["n_markets"],
        "n_series": stats["n_series"], "categories_resolved": stats["categories_resolved"],
        "categories_carried": stats["categories_carried"],
        "settled_truncated": stats["settled_truncated"],
        "excluded_category_rows": stats["excluded_category_rows"],
        "rotated_out": [p.name for p in removed],
    }
    try:
        ledger.audit("board_cache_refreshed", detail=detail)
    except Exception:  # noqa: BLE001 - the snapshot is what matters; its audit row is not
        pass
    print(
        f"board cache refreshed: {stats['n_markets']} markets, "
        f"{stats['n_series']} series ({stats['generation']})"
    )
    return {"status": "refreshed", **stats, "rotated_out": [p.name for p in removed]}


# --------------------------------------------------------------------------- reading
class Board:
    """A read-only query surface over one generation (or an in-memory ``--live`` pull).

    Every lens in ``bt`` is one method here, so the cache path and the ``--live`` path run
    the *same* query against the same schema — a live pull is loaded into an in-memory
    generation by :func:`memory_board` rather than re-implemented as list comprehensions.
    """

    def __init__(self, conn: sqlite3.Connection, *, path: Path | None,
                 captured_at: datetime | None, live: bool = False):
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self.path = path
        self.captured_at = captured_at
        self.live = live

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def open(cls, path: Path) -> Board:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        return cls(conn, path=path, captured_at=read_captured_at(path))

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> Board:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- provenance --------------------------------------------------------
    def meta(self, key: str, default: str | None = None) -> str | None:
        try:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.Error:
            return default
        return row[0] if row else default

    @property
    def carried_forward(self) -> bool:
        return self.meta("carried_forward") == "1"

    @property
    def settled_truncated(self) -> bool:
        """True when the settled slice hit its bound, making settled counts a floor."""
        return self.meta("settled_truncated") == "1"

    @property
    def n_markets(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0])

    def label_lines(self, now: datetime | None = None) -> tuple[str, str | None]:
        """The two provenance lines separately: the snapshot line, and the scope line when
        the generation carries a header.

        A caller with one line to fill (an error message, a bullet) takes the snapshot line
        from here rather than splitting :meth:`label`, which is how the scope line ended up
        swallowed mid-sentence in the digest and in ``bt ladder``'s error.
        """
        return self._snapshot_line(now), self._scope_line()

    def label(self, now: datetime | None = None) -> str:
        """The provenance lines every tool prints: when this snapshot was taken, and what
        the pull that built it was not allowed to include."""
        line, scope = self.label_lines(now)
        return f"{line}\n{scope}" if scope else line

    def _snapshot_line(self, now: datetime | None = None) -> str:
        """When this came from. Age included: a timestamp alone still leaves a session
        doing arithmetic it can get wrong (docs/12 §9.11's clock lesson)."""
        if self.live:
            return f"source: LIVE pull at {iso(now or utc_now())}"
        if self.captured_at is None:
            return f"snapshot: {self.path.name if self.path else 'unknown'} (timestamp unknown)"
        seconds = ((now or utc_now()) - self.captured_at).total_seconds()
        if seconds < 0:
            # Never rendered as a negative age: "-375 min old" reads as a bug, and a
            # snapshot stamped ahead of the clock is a fact worth naming rather than hiding.
            return (f"snapshot: {iso(self.captured_at)} (stamped {int(-seconds // 60)} min "
                    f"AHEAD of this clock — clock skew; cached board listing)")
        return (f"snapshot: {iso(self.captured_at)} ({int(seconds // 60)} min old, "
                f"cached board listing)")

    def _scope_line(self) -> str | None:
        """What the pull left out, from the generation's own header (docs/22 section 6).

        A cache that silently holds less than the board is the failure this line exists to
        prevent: a session that searches for a market the bound or an exclusion removed
        reads an empty answer as "the exchange has nothing", which is how the category block
        cost two weeks of attempts (docs/18). A generation built before the header existed
        carries none of these keys and gets no line, because a snapshot must not be made to
        claim a bound it was not built with.
        """
        hours = self.meta("close_bound_hours")
        if not hours:
            return None
        return (
            f"bound: markets closing within {hours} hours; "
            f"excluded series: {self.meta('excluded_series') or 'none'}; "
            f"excluded categories: {self.meta('excluded_categories') or 'none'} "
            f"({self.meta('excluded_category_rows', '0')} removed)"
        )

    # -- lenses ------------------------------------------------------------
    def _rows(self, sql: str, params: tuple) -> list[dict]:
        return [_row_dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def series_rollup(self, *, sort: str = "volume", limit: int | None = None) -> list[dict]:
        """One row per series: the structural answer to the parlay flood (docs/14 C2).

        A family of hundreds of dead parlay markets collapses to a single row carrying its
        own count and volume, so a survey sees it, sizes it and moves on instead of paging
        through it. ``title``/``category`` are a representative market's — the busiest one,
        deterministically tie-broken — because a series has no title of its own in the
        listing.
        """
        order = _SERIES_SORTS.get(sort, _SERIES_SORTS["volume"])
        sql = (
            "SELECT m.series AS series,"
            " COUNT(*) AS n_markets,"
            " SUM(COALESCE(m.volume, 0)) AS volume,"
            " SUM(COALESCE(m.open_interest, 0)) AS open_interest,"
            " MIN(m.close_time) AS soonest_close,"
            " SUM(m.is_settled) AS n_settled,"
            " (SELECT t.title FROM markets t WHERE t.series = m.series"
            "  ORDER BY COALESCE(t.volume, 0) DESC, t.ticker LIMIT 1) AS title,"
            " (SELECT c.category FROM markets c WHERE c.series = m.series"
            "  AND c.category IS NOT NULL ORDER BY c.ticker LIMIT 1) AS category"
            " FROM markets m GROUP BY m.series"
            f" ORDER BY {order}"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def markets(self, *, series: str | None = None, category: str | None = None,
                min_volume: int | None = None, closing_within_hours: int | None = None,
                sort: str = "close", limit: int | None = 200,
                now: datetime | None = None,
                include_settled: bool = False) -> list[dict]:
        where, params = self._market_filters(
            series=series, category=category, min_volume=min_volume,
            closing_within_hours=closing_within_hours, now=now,
            include_settled=include_settled,
        )
        order = _MARKET_SORTS.get(sort, _MARKET_SORTS["close"])
        sql = f"SELECT {_ROW_SELECT} FROM markets{where} ORDER BY {order}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._rows(sql, tuple(params))

    def _market_filters(self, *, series, category, min_volume, closing_within_hours,
                        now, include_settled: bool = False) -> tuple[str, list]:
        clauses: list[str] = []
        params: list = []
        if not include_settled:
            clauses.append(_TRADEABLE)
        if series:
            clauses.append("series = ?")
            params.append(series.upper())
        if category:
            clauses.append("category = ?")
            params.append(category)
        if min_volume is not None:
            clauses.append("COALESCE(volume, 0) >= ?")
            params.append(int(min_volume))
        if closing_within_hours is not None:
            clauses.append("close_time IS NOT NULL AND close_time <= ?")
            params.append(iso((now or utc_now()) + timedelta(hours=closing_within_hours)))
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def search(self, text: str, *, limit: int | None = 100,
               include_settled: bool = False) -> list[dict]:
        """Case-insensitive substring match over titles and rules text, ALL terms present.

        Substring rather than tokenized FTS on purpose: a session hunting ``OWGR`` or
        ``Valorant`` wants the literal string wherever it appears (``Golfer`` matching
        ``golf`` is a feature here), the answer is exactly reproducible, and the cache
        stays one table instead of one table plus an index of its own text. Rules text is
        searchable only where the listing payload carried it — the caller is told.
        """
        terms = [t for t in text.lower().split() if t]
        if not terms:
            return []
        clause = " AND ".join(
            ["lower(COALESCE(title, '') || ' ' || COALESCE(rules, '')) LIKE ?"] * len(terms)
        )
        if not include_settled:
            clause = f"{_TRADEABLE} AND {clause}"
        params = [f"%{t}%" for t in terms]
        sql = (f"SELECT {_ROW_SELECT} FROM markets WHERE {clause} "
               f"ORDER BY COALESCE(volume, 0) DESC, ticker")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._rows(sql, tuple(params))

    def new_since(self, since: datetime, *, limit: int | None = 200) -> list[dict]:
        """Markets first seen at or after ``since`` — the carry-forward window (C1).

        Tradeable only: the settled slice is bounded (``board.settled_max``), so resolutions
        entering it for the first time carry a fresh ``first_seen`` and would otherwise be
        reported as newly listed markets — the opposite of what this lens answers.
        """
        sql = (f"SELECT {_ROW_SELECT} FROM markets WHERE {_TRADEABLE} AND first_seen >= ? "
               f"ORDER BY first_seen DESC, ticker")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._rows(sql, (iso(since),))

    def movers(self, prior: Path, *, top: int = 20) -> list[dict]:
        """Largest absolute YES-mid moves against ``prior``, biggest first.

        Only markets quoted in BOTH generations can have moved; a listing that appeared
        since the prior snapshot is ``bt new``'s answer, not a mover, and manufacturing a
        delta from a missing quote is how a session ends up trading a phantom.
        """
        self._conn.execute("ATTACH DATABASE ? AS prior", (f"file:{prior}?mode=ro",))
        try:
            rows = self._conn.execute(
                "SELECT l.ticker AS ticker, l.series AS series, l.title AS title,"
                " l.category AS category, l.close_time AS close_time, l.volume AS volume,"
                " l.open_interest AS open_interest,"
                " p.yes_mid_e4 AS prior_mid_e4, l.yes_mid_e4 AS mid_e4"
                " FROM markets l JOIN prior.markets p ON p.ticker = l.ticker"
                " WHERE l.yes_mid_e4 IS NOT NULL AND p.yes_mid_e4 IS NOT NULL"
                "   AND l.yes_mid_e4 <> p.yes_mid_e4"
                " ORDER BY abs(l.yes_mid_e4 - p.yes_mid_e4) DESC, l.ticker"
                f" LIMIT {int(max(0, top))}"
            ).fetchall()
        finally:
            try:
                self._conn.execute("DETACH DATABASE prior")
            except sqlite3.Error:
                pass
        out: list[dict] = []
        for r in rows:
            prior_price = Decimal(r["prior_mid_e4"]) / _E4
            price = Decimal(r["mid_e4"]) / _E4
            out.append({
                "ticker": r["ticker"], "series": r["series"], "title": r["title"],
                "category": r["category"], "prior_yes_mid": prior_price,
                "yes_mid": price, "move": price - prior_price,
                "volume": r["volume"], "open_interest": r["open_interest"],
                "close_time": r["close_time"],
            })
        return out

    def ladder(self, series: str, *, limit: int | None = 500) -> list[dict]:
        """One series' full strike ladder, by close time then numeric strike (C2).

        The A-0056 sum-constraint check as one command: every strike of a family on one
        screen with its own bid/ask/last/volume/OI, so a session can see the ladder's
        shape instead of reconstructing it from a filtered listing.

        The live ladder, not the family's history: a daily family (``KXHIGHNY`` and its
        kind) has one open rung per strike and every prior day's resolved, unquoted rungs
        behind it, which would push the tradeable ones off "one screen".
        """
        sql = (
            "SELECT ticker, series, title, category, status, close_time, yes_bid, yes_ask,"
            " no_bid, no_ask, last_price, volume, open_interest, strike_label, first_seen"
            f" FROM markets WHERE {_TRADEABLE} AND series = ?"
            " ORDER BY close_time IS NULL, close_time,"
            " strike_e4 IS NULL, strike_e4, ticker"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._rows(sql, (series.upper(),))

    def calendar(self, *, hours: int = 72, limit: int | None = 500,
                 now: datetime | None = None) -> list[dict]:
        """Markets closing inside the window, soonest first — the time lens (C2)."""
        now = now or utc_now()
        sql = (f"SELECT {_ROW_SELECT} FROM markets"
               " WHERE close_time IS NOT NULL AND close_time >= ? AND close_time <= ?"
               " ORDER BY close_time, ticker")
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self._rows(sql, (iso(now), iso(now + timedelta(hours=hours))))


def _row_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    for key in _DECIMAL_FIELDS:
        if key in out and out[key] is not None:
            out[key] = Decimal(out[key])
    return out


def latest(board_dir: Path) -> Board | None:
    """The newest complete generation as a :class:`Board`, or ``None`` on a cold cache."""
    gens = generations(board_dir)
    if not gens:
        return None
    try:
        return Board.open(gens[0])
    except sqlite3.Error:
        return None


def prior_generation(board_dir: Path) -> Path | None:
    gens = generations(board_dir)
    return gens[1] if len(gens) > 1 else None


def memory_board(markets: Iterable[Market], *, captured_at: datetime | None = None) -> Board:
    """A ``--live`` pull loaded into an in-memory generation (docs/14 C1's escape hatch).

    Raw full pulls must remain possible — the tools shape convenience, not possibility —
    and running them through the same schema is what keeps ``--live`` output identical in
    shape to the cached answer instead of a second, subtly different implementation.
    """
    captured_at = captured_at or utc_now()
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.executescript(_SCHEMA)
    _insert_stream(conn, markets, first_seen=iso(captured_at), max_markets=None)
    _meta_set(conn, "captured_at", iso(captured_at))
    _meta_set(conn, "carried_forward", 0)
    return Board(conn, path=None, captured_at=captured_at, live=True)
