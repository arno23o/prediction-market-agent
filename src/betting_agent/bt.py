"""``bt`` — the neutral, read-only toolkit used by agent sessions (spec §13).

**Neutrality**: outputs are facts only — no ranking by attractiveness, no advice, no
filtering the harness does not document. Server/ledger order is preserved as-is.

**Read-only by construction**: the ledger is always opened with ``readonly=True``, and
the exchange is only ever read. The single exception is ``--out``, which writes the full
result of a listing to a file the caller names *inside its own working directory* — see
:func:`_resolve_out`, which refuses anything else (BT-1). The docstring used to claim
"zero write calls", which was untrue the moment ``--out`` landed and was worth more than
it looked: it was the reason nobody had checked where that path could point.

Human output is the default (compact plain-text tables); ``--json`` emits machine output
with every ``Decimal`` serialized as a string.

**The board cache (docs/14 C1).** Every LISTING lens (``markets``, ``series``, ``search``,
``new``, ``movers``, ``board``, ``calendar``) reads the harness's latest snapshot of the whole
market listing (``data/board``, via :mod:`betting_agent.board`) and prints that snapshot's
capture time before its table, so a cached answer is never mistaken for a live one.
``--live`` forces a direct pull on the lenses that can express one; ``bt book`` is ALWAYS
live and never touches the cache, because an order book is what you are about to trade
against. A cold cache is not a silent empty answer: the tool says there is no snapshot yet
and names ``--live``.

Exit codes (spec §13): ``0`` ok · ``2`` usage error · ``3`` history disabled ·
``4`` not found (unknown ticker/attempt, or no board snapshot to answer from) ·
``5`` upstream API error.

History gate (docs/22 section 9): ``bt past`` serves the record of past attempts and runs
only when ``BT_PAST == "on"``, which the runner sets per cell. Otherwise every subcommand
prints exactly ``history is not available to this attempt`` and exits ``3``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated

import typer

from betting_agent import board as board_cache
from betting_agent.config import load_settings
from betting_agent.kalshi.client import KalshiAPIError
from betting_agent.kalshi.types import parse_price
from betting_agent.ledger import history as _history
from betting_agent.ledger.db import Ledger
from betting_agent.moneymath import D, q4, unit_fee
from betting_agent.moneymath import fee as calc_fee
from betting_agent.timeutil import iso, parse_iso, utc_now

# Kalshi caps a single ``/markets`` page at 500 rows (a larger page 400s). ``bt markets``
# clamps every API page to this and paginates to reach the caller's total ``--limit``.
_MARKETS_PAGE_CAP = 500

# How many markets ``bt markets`` will LOOK AT before it stops, however few matched
# (BT-3). Filtering now happens before ``--limit``, so a narrow ``--category`` on a board
# with no matches would otherwise walk every page there is. The eligible board is
# typically 1000-2000 rows, so this cap is slack, not a limit — and when it does bite the
# caller is told, because a silently short answer to "what is on the board" is worse than
# a long one.
_MARKETS_SCAN_CAP = 5000

# ``bt history`` period labels -> Kalshi ``period_interval`` minutes (verified 1/60/1440).
_HISTORY_PERIODS = {"1m": 1, "1h": 60, "1d": 1440}

# How many candle rows ``bt history`` prints before truncating (BT-5). A 1-minute period
# over three days is ~4,300 rows going straight into a session's context window, which is
# both expensive and unreadable; ``--out`` writes the whole series to a file instead.
_HISTORY_ROW_CAP = 500

# The same discipline for the cache lenses (docs/14 C2): a ladder or a calendar window can
# be thousands of rows, and "one screen" is the point of these tools. The cap is what gets
# READ ALOUD — ``--limit`` raises it and ``--out`` (where offered) is never truncated.
_LENS_ROW_CAP = 500

# How many rows a series rollup prints. The whole board collapses to a few hundred series,
# so this is slack rather than a limit; the parlay families that flooded the listing are
# single rows here (docs/12 §3).
_SERIES_ROW_CAP = 300

# ``bt new``'s default window. Hourly generations plus ``first_seen`` carry-forward make
# any window answerable; a day is the question docs/12 §3 actually asked ("the Valorant
# markets listed between two sessions' scans").
_NEW_DEFAULT_HOURS = 24

# The lenses that read the board snapshot (docs/14 C1/C2). One named set, used three ways:
# it is enumerated in ``bt --help`` below, it is what the attempt/ideation/implementation
# prompts must name (tests/test_prompt_contracts.py asserts against THIS, never a retyped
# list), and it is the boundary the cache convention applies to — everything outside it
# (``book``, ``market``, ``history``) goes to the exchange every time.
LISTING_LENSES = ("markets", "series", "search", "new", "movers", "board", "calendar")

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help=(
        "bt — neutral, read-only toolkit for agent sessions (facts only). Listing lenses ("
        + ", ".join(LISTING_LENSES)
        + ") read the harness's latest board snapshot and print its capture time; --live "
        "pulls from the exchange instead. `bt book` is always live."
    ),
)
ticket_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Ticket preflight.")
past_app = typer.Typer(
    add_completion=False, invoke_without_command=True,
    help=(
        "Past attempts, as records (read-only; needs BT_PAST=on). With no subcommand it "
        "prints the families table, the map. Filters: --era, --category, --outcome, "
        "--since, --limit, --json."
    ),
)
app.add_typer(ticket_app, name="ticket")
app.add_typer(past_app, name="past")


# --------------------------------------------------------------------------- wiring
def _settings():
    """Settings rooted at ``$BT_ROOT`` if set, else the current working directory."""
    root = os.environ.get("BT_ROOT")
    return load_settings(root=Path(root).expanduser() if root else Path.cwd())


def _client(settings):
    """Build a :class:`KalshiClient` from settings + env creds.

    Structured as a standalone factory so tests can monkeypatch it with ``FakeKalshi``.
    """
    from betting_agent.kalshi.client import KalshiClient

    return KalshiClient(
        base_url=settings.kalshi.base_url,
        key_id=settings.kalshi.key_id or "",
        private_key_path=settings.kalshi.private_key_path,
    )


def _client_or_exit(settings):
    try:
        return _client(settings)
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken client is an upstream/config failure
        print(f"error: Kalshi client unavailable: {exc}")
        raise typer.Exit(5) from exc


def _open_ledger_ro(settings) -> Ledger | None:
    """Open the ledger read-only, or ``None`` when the file does not exist yet."""
    if not settings.ledger_path.exists():
        return None
    return Ledger.open(settings.ledger_path, readonly=True)


def _coef_for(settings, category: str | None) -> Decimal:
    if category:
        coef = settings.fees.category_coefs.get(category)
        if coef is not None:
            return D(coef)
    return D(settings.fees.default_coef)


# --------------------------------------------------------------------------- input hardening
def _price_or_exit(raw: str, flag: str = "--price") -> Decimal:
    """Parse a decimal option, or exit 2 (BT-2).

    ``D("0,42")`` raises ``InvalidOperation``, which reached the session as a raw
    traceback and exit 1 — indistinguishable from an upstream failure, when the honest
    answer is "you typed it wrong" (spec §13: 2 == usage error). ``"NaN"`` and
    ``"Infinity"`` parse *successfully* as Decimals and then poison every number computed
    from them, so they are rejected here too.
    """
    try:
        value = D(raw)
    except (InvalidOperation, ArithmeticError, ValueError, TypeError):
        print(f"error: invalid {flag} {raw!r}: expected a decimal number, e.g. 0.42")
        raise typer.Exit(2) from None
    if not value.is_finite():
        print(f"error: invalid {flag} {raw!r}: expected a finite decimal number")
        raise typer.Exit(2)
    return value


def _contracts_or_exit(contracts: int) -> int:
    """A contract count is a non-negative integer; anything else is a usage error (BT-2).

    Typer already turns a non-integer into exit 2; a *negative* one parsed cleanly and
    produced a negative fee — a number that means nothing and reads as if it did.
    """
    if contracts < 0:
        print(f"error: invalid --contracts {contracts}: expected a non-negative integer")
        raise typer.Exit(2)
    return contracts


def _resolve_out(raw: str) -> Path:
    """Resolve ``--out`` strictly inside the current working directory, or exit 2 (BT-1).

    ``bt`` runs with the session's workspace as its cwd, and that workspace is the only
    place a read-only toolkit has any business writing. Absolute paths, ``~`` expansions
    and ``..`` traversal all escaped it: the reproduction silently truncated an existing
    file outside the workspace. Sessions do have full Bash, so this is defense in depth
    rather than containment — but a *neutral read-only tool* should not be the thing that
    overwrites the ledger, a prompt, or a config file on a typo'd flag.
    """
    base = Path.cwd().resolve()
    candidate = Path(raw).expanduser()
    try:
        resolved = (candidate if candidate.is_absolute() else base / candidate).resolve()
    except OSError as exc:
        print(f"error: cannot resolve --out {raw!r}: {exc}")
        raise typer.Exit(2) from exc
    if base not in resolved.parents:
        print(
            f"error: --out must stay inside the working directory ({base}); "
            f"{raw!r} resolves to {resolved}"
        )
        raise typer.Exit(2)
    return resolved


def _write_out(path: Path, payload: str) -> None:
    """Write ``--out``; an OSError is a usage error (exit 2), never a traceback (BT-1)."""
    try:
        path.write_text(payload)
    except OSError as exc:
        print(f"error: cannot write --out {path}: {exc}")
        raise typer.Exit(2) from exc


# --------------------------------------------------------------------------- output
def _json_default(obj):
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, datetime):
        return iso(obj) if obj.tzinfo else obj.isoformat()
    return str(obj)


def _print_json(obj) -> None:
    print(json.dumps(obj, default=_json_default, indent=2))


def _notice(text: str, *, json_out: bool) -> None:
    """Print a truncation notice where it cannot be missed and cannot corrupt anything.

    Under ``--json`` the only thing on stdout must be the JSON document, so the notice
    goes to stderr; otherwise it follows the table. Either way it is printed — a result
    that was cut short and does not say so is the failure mode both BT-3 and BT-5 are
    about.
    """
    print(text, file=sys.stderr) if json_out else print(text)


def _cell(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return iso(value) if value.tzinfo else value.isoformat()
    return str(value)


def _print_table(headers: list[str], rows: list[list]) -> None:
    body = [[_cell(c) for c in r] for r in rows]
    widths = [len(h) for h in headers]
    for r in body:
        for i, cell in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def _line(vals: list[str]) -> str:
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(vals)).rstrip()

    print(_line(headers))
    for r in body:
        print(_line(r))
    if not body:
        print("(none)")


# --------------------------------------------------------------------------- fetch helpers
def _fetch_market(client, ticker: str):
    try:
        return client.get_market(ticker)
    except KalshiAPIError as exc:
        if exc.status == 404:
            print(f"error: market not found: {ticker}")
            raise typer.Exit(4) from exc
        print(f"error: upstream API error: {exc}")
        raise typer.Exit(5) from exc


def _fetch_book(client, ticker: str, depth: int):
    try:
        return client.get_orderbook(ticker, depth=depth)
    except KalshiAPIError as exc:
        if exc.status == 404:
            print(f"error: market not found: {ticker}")
            raise typer.Exit(4) from exc
        print(f"error: upstream API error: {exc}")
        raise typer.Exit(5) from exc


def _fetch_candles(client, ticker: str, period_minutes: int, start, end):
    try:
        return client.get_candlesticks(
            ticker, period_minutes=period_minutes, start=start, end=end
        )
    except KalshiAPIError as exc:
        # An unknown ticker 404s during series derivation; a too-wide window / other
        # upstream failure is a definite API error (exit 5).
        if exc.status == 404:
            print(f"error: market not found: {ticker}")
            raise typer.Exit(4) from exc
        print(f"error: upstream API error: {exc}")
        raise typer.Exit(5) from exc


# --------------------------------------------------------------------------- board lenses
def _banner(text: str, *, json_out: bool) -> None:
    """Print a lens's SOURCE line before its table, where it cannot be missed.

    Same stream discipline as :func:`_notice` (under ``--json`` stdout carries only the
    JSON document, so the line goes to stderr), but printed FIRST rather than last: a
    snapshot timestamp that arrives after four hundred rows of table has already been read
    as live data. This is the one line that keeps a cached board honest.
    """
    print(text, file=sys.stderr) if json_out else print(text)


def _cache_or_exit(settings, *, needs: str = "snapshot", live_hint: str | None = "--live"):
    """The newest generation, or exit 4 saying there is none (docs/14 C1).

    Never an empty table: "the board has nothing in it" and "nobody has snapshotted the
    board yet" are opposite facts, and a session that cannot tell them apart will conclude
    the exchange is empty. ``live_hint`` names the escape for lenses that have one.
    """
    b = board_cache.latest(settings.board_dir)
    if b is None:
        hint = f"; use {live_hint} for a direct pull" if live_hint else ""
        print(
            f"error: no board {needs} yet ({settings.board_dir} holds no generation); "
            f"the harness's next refresh builds one{hint}"
        )
        raise typer.Exit(4)
    return b


def _live_board(settings, *, json_out: bool):
    """A direct pull loaded into an in-memory generation — the ``--live`` path.

    Bounded by ``_MARKETS_SCAN_CAP`` examined rows, the same bound ``bt markets`` has had
    since BT-3, and it says so when it bites. The cache is the unbounded full-board path
    now; ``--live`` is for freshness, not for volume.
    """
    client = _client_or_exit(settings)
    scanned = 0
    truncated = False

    def _stream():
        nonlocal scanned, truncated
        seen: set[str] = set()
        for status in board_cache.board_statuses(settings):
            for market in client.iter_markets(status=status, limit=_MARKETS_PAGE_CAP):
                if scanned >= _MARKETS_SCAN_CAP:
                    truncated = True
                    return
                scanned += 1
                if market.ticker in seen:
                    continue
                seen.add(market.ticker)
                yield market

    try:
        b = board_cache.memory_board(_stream(), captured_at=utc_now())
    except KalshiAPIError as exc:
        print(f"error: upstream API error: {exc}")
        raise typer.Exit(5) from exc
    if truncated:
        _notice(
            f"notice: --live stopped after examining {scanned} markets (scan cap "
            f"{_MARKETS_SCAN_CAP}); the cached snapshot holds the whole board",
            json_out=json_out,
        )
    return b


def _lens_board(settings, *, live: bool, json_out: bool, live_hint: str | None = "--live"):
    return _live_board(settings, json_out=json_out) if live else _cache_or_exit(
        settings, live_hint=live_hint
    )


@contextmanager
def _reading(b):
    """Query one board and close it; a snapshot that cannot be read is exit 5, not a
    traceback.

    A generation is published atomically, so a *torn* one is impossible — but a file on a
    filesystem can still go bad, and BT-2's lesson is that a raw ``sqlite3`` traceback out
    of a read-only toolkit is indistinguishable to the session from an upstream failure.
    Named as such, with the fix (the next refresh replaces it) in the same line.
    """
    try:
        yield b
    except sqlite3.Error as exc:
        print(f"error: this board snapshot is unreadable ({exc}); the harness's next "
              "refresh replaces it, or use --live where the lens offers it")
        raise typer.Exit(5) from exc
    finally:
        b.close()


def _emit_rows(rows: list[dict], headers: list[tuple[str, str]], *, json_out: bool,
               cap: int | None = None, what: str = "rows") -> None:
    """Render lens rows as JSON or a table, truncating what is READ ALOUD only."""
    if json_out:
        _print_json(rows)
        return
    shown = rows[:cap] if cap is not None and len(rows) > cap else rows
    _print_table([h for h, _ in headers], [[r.get(k) for _, k in headers] for r in shown])
    _cap_notice(len(shown), len(rows), what, json_out=False)


def _cap_notice(shown: int, total: int, what: str, *, json_out: bool) -> None:
    """The one notice a capped result prints, wherever the capping happens.

    Tables emit it from :func:`_emit_rows`; the two ``bt past`` lenses that print records
    rather than a table emit it themselves, so that every capped answer says so in the
    same words instead of two of them cutting silently.
    """
    if shown < total:
        _notice(
            f"notice: showing {shown} of {total} {what}; "
            f"narrow the query or raise --limit",
            json_out=json_out,
        )


_MARKET_HEADERS = [
    ("ticker", "ticker"), ("series", "series"), ("title", "title"), ("category", "category"),
    ("yes_bid", "yes_bid"), ("yes_ask", "yes_ask"), ("no_bid", "no_bid"), ("no_ask", "no_ask"),
    ("last", "last_price"), ("volume", "volume"), ("oi", "open_interest"),
    ("close_time", "close_time"),
]


# --------------------------------------------------------------------------- markets
def _markets_from_cache(settings, *, category, series, min_volume, hours, sort, limit,
                        out_path, json_out) -> None:
    """``bt markets`` over the snapshot: the same answer shape, computed in SQL.

    Split out rather than folded into the command so the ``--live`` branch below is byte
    for byte the code the pilot's tests pinned — the cache is a new source, not a rewrite
    of the direct pull.
    """
    with _reading(_cache_or_exit(settings)) as b:
        rows = b.markets(
            series=series, category=category, min_volume=min_volume,
            closing_within_hours=hours, sort=sort, limit=limit,
        )
        label = b.label()
    if out_path is not None:
        _write_out(out_path, json.dumps(rows, default=_json_default, indent=2))
        print(f"wrote {len(rows)} markets to {out_path}")
        print(label)
        return
    _banner(label, json_out=json_out)
    _emit_rows(rows, _MARKET_HEADERS, json_out=json_out, cap=None, what="markets")


def _market_row(market, category) -> dict:
    # ``series`` and ``last_price`` are here so a --live listing renders the SAME columns as
    # the cached one (docs/14 C2's "listings grow volume/liquidity columns everywhere it
    # makes sense"): a session comparing a fresh pull against the snapshot should be reading
    # one table shape, not two. Both are derived from what the listing already carries.
    return {
        "ticker": market.ticker,
        "series": board_cache.series_of(market.ticker),
        "title": market.title,
        "category": category,
        "yes_bid": market.yes_bid,
        "yes_ask": market.yes_ask,
        "no_bid": market.no_bid,
        "no_ask": market.no_ask,
        "last_price": parse_price(market.raw or {}, "last_price"),
        "volume": market.volume,
        "open_interest": market.open_interest,
        "close_time": market.close_time,
    }


@app.command()
def markets(
    hours: Annotated[int, typer.Option(help="resolution window (max close, in hours)")] = 72,
    category: Annotated[
        str | None,
        typer.Option(
            help=(
                "filter to a category. Applied BEFORE --limit, so --limit counts matching "
                "rows. Category lives on the parent event; against the cache it is a column "
                "(one lookup per series when the snapshot was built), and under --live it "
                "costs one cached event lookup per distinct event."
            )
        ),
    ] = None,
    series: Annotated[
        str | None,
        typer.Option("--series", help="filter to one series (ticker prefix, e.g. KXHIGHNY)"),
    ] = None,
    min_volume: Annotated[int | None, typer.Option("--min-volume", help="minimum volume")] = None,
    closing_within: Annotated[
        int | None,
        typer.Option(
            "--closing-within",
            help="only markets closing within N hours (the same bound as --hours, by its "
                 "docs/14 C2 name; wins when both are given)",
        ),
    ] = None,
    sort: Annotated[
        str,
        typer.Option(help="cache order: volume | oi | close | ticker (--live keeps server order)"),
    ] = "close",
    limit: Annotated[
        int,
        typer.Option(
            help=(
                "total MATCHING rows wanted; paginates (each API page is capped at 500 "
                "server-side, so larger totals fan out across pages). The full eligible "
                "board is typically ~1000-2000 rows."
            )
        ),
    ] = 200,
    live: Annotated[
        bool,
        typer.Option("--live", help="pull directly from the exchange instead of the snapshot"),
    ] = False,
    out: Annotated[
        str | None,
        typer.Option(
            "--out",
            help="write the FULL JSON result to this path and print only a one-line summary",
        ),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """List markets (facts only). Reads the board snapshot; ``--live`` pulls directly.

    Cached by default (docs/14 C1) — the snapshot's capture time is printed before the
    table, and filters/sorting run in SQL over the whole board rather than over the first
    page of it. ``--live`` is the direct pull, unchanged: ``--limit`` is the total number of
    MATCHING rows wanted, not a single page size (any API page is clamped to 500, Kalshi's
    server cap — a larger page 400s), so a large ``--limit`` never surfaces a raw upstream
    400, and server order is preserved as-is.

    Filters run BEFORE ``--limit`` (BT-4). The other order — take 200 rows, then filter —
    meant a category query could truthfully answer "none" while dozens of matches sat on
    page three, and nothing in the output said the search had been that shallow. The cost
    of looking further is bounded by ``_MARKETS_SCAN_CAP`` markets examined, and hitting
    that bound prints a notice rather than passing for an exhaustive answer.
    """
    settings = _settings()
    out_path = _resolve_out(out) if out is not None else None
    if sort not in board_cache.market_sorts():
        print(f"error: invalid --sort {sort!r}; choose one of "
              f"{', '.join(board_cache.market_sorts())}")
        raise typer.Exit(2)
    # BT-6: `max(1, ...)` below turned a zero/negative limit into a one-row page, and the
    # `len(listed) >= limit` break then fired after the first row — "give me nothing"
    # answered with one market. Nothing wanted, nothing fetched, no request sent. It is
    # checked before the source is chosen, so it costs neither a request nor a cache open.
    if limit <= 0:
        rows: list[dict] = []
        if out_path is not None:
            _write_out(out_path, json.dumps(rows, default=_json_default, indent=2))
            print(f"wrote 0 markets to {out_path}")
        elif json_out:
            _print_json(rows)
        else:
            _print_table([h for h, _ in _MARKET_HEADERS], [])
        return
    if not live:
        _markets_from_cache(
            settings, category=category, series=series, min_volume=min_volume,
            hours=closing_within if closing_within is not None else hours,
            sort=sort, limit=limit, out_path=out_path, json_out=json_out,
        )
        return
    if closing_within is not None:
        hours = closing_within
    client = _client_or_exit(settings)
    if out_path is None:
        _banner(f"source: LIVE pull at {iso(utc_now())}", json_out=json_out)
    max_close_ts = int((utc_now() + timedelta(hours=hours)).timestamp())
    filtering = category is not None or min_volume is not None or series is not None
    # With a filter on, the rows we want can be anywhere on the board, so pages are as
    # large as the server allows. With no filter the first ``limit`` rows ARE the answer,
    # so the page stays exactly as small as it used to be.
    page_size = _MARKETS_PAGE_CAP if filtering else max(1, min(limit, _MARKETS_PAGE_CAP))
    scanned = 0
    truncated = False
    try:
        rows = []
        for market in client.iter_markets(
            max_close_ts=max_close_ts, status="open", limit=page_size
        ):
            if scanned >= _MARKETS_SCAN_CAP:
                truncated = True
                break
            scanned += 1
            if series is not None and board_cache.series_of(market.ticker) != series.upper():
                continue
            cat = market.category
            if category is not None:
                # Category lives on the parent EVENT and the lookup is cached per event,
                # so a board of hundreds of markets costs a handful of requests instead of
                # one full market GET per row. An API failure here must not read as "not
                # this category", so it raises and surfaces as exit 5 below.
                cat = (
                    client.event_category(market.event_ticker)
                    if market.event_ticker else None
                )
                if cat != category:
                    continue
            if min_volume is not None and (market.volume or 0) < min_volume:
                continue
            rows.append(_market_row(market, cat))
            if len(rows) >= limit:
                break
    except KalshiAPIError as exc:
        print(f"error: upstream API error: {exc}")
        raise typer.Exit(5) from exc

    truncation = (
        f"notice: stopped after examining {scanned} markets (scan cap "
        f"{_MARKETS_SCAN_CAP}); more may match — narrow --hours/--category or raise it"
        if truncated else None
    )
    if out_path is not None:
        _write_out(out_path, json.dumps(rows, default=_json_default, indent=2))
        print(f"wrote {len(rows)} markets to {out_path}")
        if truncation:
            print(truncation)
        return
    if json_out:
        _print_json(rows)
        if truncation:
            _notice(truncation, json_out=True)
        return
    _print_table(
        [h for h, _ in _MARKET_HEADERS],
        [[r.get(k) for _, k in _MARKET_HEADERS] for r in rows],
    )
    if truncation:
        _notice(truncation, json_out=False)


# ------------------------------------------------------------------- lenses (docs/14 C2)
@app.command()
def series(
    sort: Annotated[
        str, typer.Option(help="volume | oi | markets | close | series")
    ] = "volume",
    limit: Annotated[int, typer.Option(help="max series rows")] = _SERIES_ROW_CAP,
    live: Annotated[
        bool, typer.Option("--live", help="roll up a direct pull instead of the snapshot")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """One row per SERIES: count, volume, OI, soonest close, settled count (facts only).

    The structural answer to the flood docs/12 §3 measured: a family of hundreds of dead
    multigame-parlay markets is one row here, carrying its own market count and volume, so
    a survey sees the whole board's shape at series granularity instead of paging through
    it — and thin families stop being invisible to scan-based ideation (docs/12 §4).
    ``title`` and ``category`` are a representative market's (the busiest in the family).
    No denylist anywhere: every series is listed, including the empty ones.
    """
    if sort not in board_cache.series_sorts():
        print(f"error: invalid --sort {sort!r}; choose one of "
              f"{', '.join(board_cache.series_sorts())}")
        raise typer.Exit(2)
    settings = _settings()
    with _reading(_lens_board(settings, live=live, json_out=json_out)) as b:
        rows = b.series_rollup(sort=sort, limit=max(0, limit))
        label = b.label()
        partial_settled = b.settled_truncated
    _banner(label, json_out=json_out)
    if partial_settled:
        _banner(
            "notice: this snapshot's settled-market slice hit its row bound, so the "
            "`settled` column is a FLOOR, not a total (open markets are complete)",
            json_out=json_out,
        )
    _emit_rows(
        rows,
        [("series", "series"), ("title", "title"), ("category", "category"),
         ("markets", "n_markets"), ("volume", "volume"), ("oi", "open_interest"),
         ("settled", "n_settled"), ("soonest_close", "soonest_close")],
        json_out=json_out, cap=_LENS_ROW_CAP, what="series",
    )


@app.command()
def search(
    text: Annotated[str, typer.Argument(help="words to find; ALL must appear")],
    limit: Annotated[int, typer.Option(help="max matches")] = 100,
    live: Annotated[
        bool, typer.Option("--live", help="search a direct pull instead of the snapshot")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Full-text search over market TITLES and rules text in the snapshot (facts only).

    Case-insensitive substring matching, every word required, ordered by volume. Rules text
    is searchable only for markets whose listing payload carried it; titles always are. The
    content-first lens: a session that knows what it is looking for should not have to page
    a board to find out whether it is listed.
    """
    settings = _settings()
    with _reading(_lens_board(settings, live=live, json_out=json_out)) as b:
        rows = b.search(text, limit=max(0, limit))
        label = b.label()
    _banner(label, json_out=json_out)
    _emit_rows(rows, _MARKET_HEADERS, json_out=json_out, cap=_LENS_ROW_CAP, what="matches")


@app.command()
def new(
    since: Annotated[
        str | None,
        typer.Option("--since", help=f"ISO timestamp; default {_NEW_DEFAULT_HOURS}h ago"),
    ] = None,
    limit: Annotated[int, typer.Option(help="max rows")] = 200,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Markets first seen in the cache at or after ``--since`` (default ~24h) — facts only.

    Freshness, from the snapshot chain: each generation inherits every surviving ticker's
    ``first_seen`` from the one before it, so a market that has been listed for a week
    carries its original first sighting and a delisted market is simply absent. There is no
    ``--live`` here — "what is new" is a statement about two points in time, and a single
    pull has only one.

    ``--since`` narrows by first sighting: markets whose ``first_seen`` is at or after
    the given timestamp. Note that ``--since <prior generation ts>`` therefore includes
    markets first seen IN that prior generation, not only ones newer than it — the strict
    "absent from the generation before" answer needs the latest snapshot's own timestamp.
    """
    settings = _settings()
    since_dt = utc_now() - timedelta(hours=_NEW_DEFAULT_HOURS)
    if since is not None:
        try:
            since_dt = parse_iso(since)
        except (ValueError, TypeError):
            print(f"error: invalid --since {since!r}: expected an ISO timestamp, "
                  "e.g. 2026-08-10T14:00:00Z")
            raise typer.Exit(2) from None
    prior = board_cache.prior_generation(settings.board_dir)
    with _reading(_cache_or_exit(settings, live_hint=None)) as b:
        rows = b.new_since(since_dt, limit=max(0, limit))
        label = b.label()
        first_generation = not b.carried_forward
    _banner(label, json_out=json_out)
    _banner(f"window: first seen at or after {iso(since_dt)}", json_out=json_out)
    prior_ts = board_cache.read_captured_at(prior) if prior is not None else None
    if prior_ts is not None:
        _banner(f"prior generation: {iso(prior_ts)}", json_out=json_out)
    if first_generation:
        _banner(
            "notice: this is the first generation in the cache — every market's first_seen "
            "is the snapshot time, so newly listed and merely first-seen cannot be told "
            "apart yet",
            json_out=json_out,
        )
    _emit_rows(
        rows,
        [("ticker", "ticker"), ("series", "series"), ("title", "title"),
         ("category", "category"), ("yes_bid", "yes_bid"), ("yes_ask", "yes_ask"),
         ("volume", "volume"), ("oi", "open_interest"), ("close_time", "close_time"),
         ("first_seen", "first_seen")],
        json_out=json_out, cap=_LENS_ROW_CAP, what="new markets",
    )


@app.command()
def movers(
    top: Annotated[int, typer.Option("--top", help="how many rows")] = 20,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Largest absolute YES-mid price MOVES between the two newest snapshots (facts only).

    The change-first lens. ``yes_mid`` is the midpoint of the quoted bid/ask (one side when
    only one is quoted); a market quoted in only one of the two generations is excluded
    rather than given an invented delta, and the count of comparable markets is stated. No
    ``--live``: a move needs two observations.
    """
    settings = _settings()
    prior = board_cache.prior_generation(settings.board_dir)
    with _reading(_cache_or_exit(settings, live_hint=None)) as b:
        label = b.label()
        if prior is None:
            print(
                "error: only one board generation exists and a price move needs two; "
                "the harness's next refresh makes this answerable"
            )
            raise typer.Exit(4)
        rows = b.movers(prior, top=max(0, top))
    _banner(label, json_out=json_out)
    _banner(f"compared against: {prior.name}", json_out=json_out)
    _emit_rows(
        rows,
        [("ticker", "ticker"), ("series", "series"), ("title", "title"),
         ("prior_yes_mid", "prior_yes_mid"), ("yes_mid", "yes_mid"), ("move", "move"),
         ("volume", "volume"), ("oi", "open_interest"), ("close_time", "close_time")],
        json_out=json_out, cap=_LENS_ROW_CAP, what="movers",
    )


@app.command("board")
def board_cmd(
    series_ticker: Annotated[str, typer.Argument(help="series ticker prefix, e.g. KXHIGHNY")],
    limit: Annotated[int, typer.Option(help="max strike rows")] = _LENS_ROW_CAP,
    live: Annotated[
        bool, typer.Option("--live", help="build the ladder from a direct pull")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """One series' full STRIKE LADDER: per-strike bid/ask/last/volume/OI (facts only).

    Ordered by close time then numeric strike, so a family's ladder reads top to bottom on
    one screen — the A-0056 sum-constraint check as a single command instead of a filtered
    listing reassembled by hand. This is the LISTING lens; ``bt book`` is the live order
    book for one market and is never served from the cache.
    """
    settings = _settings()
    with _reading(_lens_board(settings, live=live, json_out=json_out)) as b:
        rows = b.ladder(series_ticker, limit=max(0, limit))
        label = b.label()
        # The error below is one line, and the label is two; the snapshot line is the half
        # that belongs in a parenthesis.
        snapshot = b.label_lines()[0]
    if not rows:
        print(f"error: no markets for series {series_ticker.upper()} in this source "
              f"({snapshot})")
        raise typer.Exit(4)
    _banner(label, json_out=json_out)
    _emit_rows(
        rows,
        [("strike", "strike_label"), ("ticker", "ticker"), ("title", "title"),
         ("status", "status"), ("yes_bid", "yes_bid"), ("yes_ask", "yes_ask"),
         ("no_bid", "no_bid"), ("no_ask", "no_ask"), ("last", "last_price"),
         ("volume", "volume"), ("oi", "open_interest"), ("close_time", "close_time")],
        json_out=json_out, cap=_LENS_ROW_CAP, what="strikes",
    )


@app.command()
def calendar(
    hours: Annotated[int, typer.Option(help="window ahead, in hours")] = 72,
    limit: Annotated[int, typer.Option(help="max rows")] = _LENS_ROW_CAP,
    live: Annotated[
        bool, typer.Option("--live", help="build the calendar from a direct pull")
    ] = False,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Markets closing inside the next N hours, soonest first (facts only).

    The time lens: the window is bounded at both ends (nothing already closed), so what is
    listed is what can still resolve inside it.
    """
    settings = _settings()
    with _reading(_lens_board(settings, live=live, json_out=json_out)) as b:
        rows = b.calendar(hours=hours, limit=max(0, limit))
        label = b.label()
    _banner(label, json_out=json_out)
    _emit_rows(
        rows,
        [("close_time", "close_time"), ("ticker", "ticker"), ("series", "series"),
         ("title", "title"), ("category", "category"), ("yes_bid", "yes_bid"),
         ("yes_ask", "yes_ask"), ("volume", "volume"), ("oi", "open_interest")],
        json_out=json_out, cap=_LENS_ROW_CAP, what="markets",
    )


@app.command()
def market(
    ticker: Annotated[str, typer.Argument(help="market ticker")],
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Full market detail (including tick size and resolution rules text if present)."""
    settings = _settings()
    client = _client_or_exit(settings)
    m = _fetch_market(client, ticker)
    raw = m.raw or {}
    detail = {
        "ticker": m.ticker,
        "title": m.title,
        "category": m.category,
        "status": m.status,
        "close_time": m.close_time,
        "expected_expiration": m.expected_expiration,
        "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask,
        "no_bid": m.no_bid,
        "no_ask": m.no_ask,
        "volume": m.volume,
        "open_interest": m.open_interest,
        "tick_size": m.tick_size,
        "rules_primary": raw.get("rules_primary"),
        "rules_secondary": raw.get("rules_secondary"),
    }
    if json_out:
        _print_json(detail)
        return
    for key in ("ticker", "title", "category", "status", "close_time", "expected_expiration",
                "yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "open_interest", "tick_size"):
        print(f"{key}: {_cell(detail[key])}")
    if detail["rules_primary"]:
        print(f"rules_primary: {detail['rules_primary']}")
    if detail["rules_secondary"]:
        print(f"rules_secondary: {detail['rules_secondary']}")


@app.command()
def book(
    ticker: Annotated[str, typer.Argument(help="market ticker")],
    depth: Annotated[int, typer.Option(help="levels per side")] = 5,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Orderbook levels (both sides) plus derived best asks and sizes."""
    settings = _settings()
    client = _client_or_exit(settings)
    ob = _fetch_book(client, ticker, depth)
    data: dict = {"ticker": ticker}
    for side in ("yes", "no"):
        levels = ob.yes_levels if side == "yes" else ob.no_levels
        data[side] = {
            "bids": [[p, s] for (p, s) in levels],
            "best_bid": ob.best_bid(side),
            "best_bid_size": ob.best_bid_size(side),
            "best_ask": ob.best_ask(side),
            "best_ask_size": ob.best_ask_size(side),
        }
    if json_out:
        _print_json(data)
        return
    for side in ("yes", "no"):
        sd = data[side]
        print(
            f"[{side}] best_bid={_cell(sd['best_bid'])} ({_cell(sd['best_bid_size'])})  "
            f"best_ask={_cell(sd['best_ask'])} ({_cell(sd['best_ask_size'])})"
        )
        _print_table(["price", "size"], [[p, s] for (p, s) in sd["bids"]])


@app.command()
def history(
    ticker: Annotated[str, typer.Argument(help="market ticker")],
    period: Annotated[
        str, typer.Option(help="candle interval: 1m (1 min), 1h (60 min), or 1d (daily)")
    ] = "1h",
    hours: Annotated[int, typer.Option(help="lookback window, in hours (ends now)")] = 72,
    out: Annotated[
        str | None,
        typer.Option(
            "--out",
            help="write the FULL JSON series to this path and print only a one-line summary",
        ),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Raw market price HISTORY — YES-side OHLC candlesticks, oldest first (facts only).

    Neutral: the verbatim candle series (ts, open, high, low, close, volume, and open
    interest when present) with NO derived signals — no trends, moving averages, or
    rankings. The series_ticker needed for the upstream call is derived automatically.
    A wide window at a fine period can exceed Kalshi's 5000-candle cap and then surfaces
    as an upstream error (exit 5).

    Printed output stops at 500 rows (BT-5). ``--period 1m --hours 72`` is ~4,300 rows,
    and dumping those into a session's context is expensive and unreadable; when the cap
    bites, the notice says how many rows were dropped and how to get them. ``--out``
    writes the WHOLE series to a file inside the working directory and is never truncated
    — the cap is about what gets read aloud, not about what the tool will give you.
    """
    if period not in _HISTORY_PERIODS:
        print(f"error: invalid --period {period!r}; choose one of {', '.join(_HISTORY_PERIODS)}")
        raise typer.Exit(2)
    settings = _settings()
    out_path = _resolve_out(out) if out is not None else None
    client = _client_or_exit(settings)
    end = utc_now()
    start = end - timedelta(hours=hours)
    candles = _fetch_candles(client, ticker, _HISTORY_PERIODS[period], start, end)
    rows = [
        {
            "ts": c.ts,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "open_interest": c.open_interest,
        }
        for c in candles
    ]
    if out_path is not None:
        _write_out(out_path, json.dumps(rows, default=_json_default, indent=2))
        print(f"wrote {len(rows)} candles to {out_path}")
        return

    omitted = max(0, len(rows) - _HISTORY_ROW_CAP)
    shown = rows[-_HISTORY_ROW_CAP:] if omitted else rows  # the newest end of the series
    truncation = (
        f"notice: showing the most recent {len(shown)} of {len(rows)} candles; "
        f"{omitted} rows omitted — use --out or narrow the window"
        if omitted else None
    )
    if json_out:
        _print_json(shown)
        if truncation:
            _notice(truncation, json_out=True)
        return
    has_oi = any(r["open_interest"] is not None for r in shown)
    headers = ["ts", "open", "high", "low", "close", "volume"] + (["oi"] if has_oi else [])
    _print_table(
        headers,
        [
            [r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]]
            + ([r["open_interest"]] if has_oi else [])
            for r in shown
        ],
    )
    if truncation:
        _notice(truncation, json_out=False)


# --------------------------------------------------------------------------- pure money math
@app.command()
def fees(
    price: Annotated[str, typer.Option("--price", help="limit price, e.g. 0.42")],
    contracts: Annotated[int, typer.Option("--contracts", help="number of contracts")],
    category: Annotated[str | None, typer.Option(help="fee-coefficient category")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Taker fee under spec §8 (pure; no network)."""
    p = _price_or_exit(price)
    contracts = _contracts_or_exit(contracts)
    settings = _settings()
    coef = _coef_for(settings, category)
    fee_amt = calc_fee(contracts, p, coef)
    uf = unit_fee(p, coef)
    data = {"price": p, "contracts": contracts, "category": category,
            "coef": coef, "fee": fee_amt, "unit_fee": uf}
    if json_out:
        _print_json(data)
        return
    print(f"fee={fee_amt} (contracts={contracts} price={p} coef={coef} unit_fee={uf})")


@app.command()
def size(
    price: Annotated[str, typer.Option("--price", help="limit price, e.g. 0.42")],
    contracts: Annotated[int, typer.Option("--contracts", help="number of contracts")] = 1,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Stake for one bet at a price and a size (pure; no network).

    Sizing is the ticket's own decision now, so this is a calculator: stake is contracts
    times the limit price. The maximum a ticket may declare is shown alongside it.
    """
    p = _price_or_exit(price)
    c = _contracts_or_exit(contracts)
    try:
        cap = _settings().stakes.max_contracts_per_bet
    except Exception:  # noqa: BLE001 - a pure calculator must not die on a bad config
        cap = 3
    stake = q4(D(c) * p)
    data = {"price": p, "contracts": c, "stake": stake, "max_contracts_per_bet": cap}
    if json_out:
        _print_json(data)
        return
    print(f"contracts={c} stake={stake} (price={p} max={cap})")


# --------------------------------------------------------------------------- past (gated)
PAST_DISABLED_MSG = "history is not available to this attempt"

# The shared filters (docs/22 section 9). ``--limit`` defaults to 50 on the tables and to
# ten on ``recent``, which is the static cell's set size.
_PAST_OUTCOMES = ("win", "loss", "refused", "nofill", "pass")
_PAST_ERAS = ("current", "all")
_PAST_ROW_LIMIT = 50
_PAST_RECENT_LIMIT = 10

_PAST_FAMILY_HEADERS = [
    ("family", "family"), ("attempts", "attempts"), ("legs", "legs"), ("filled", "filled"),
    ("wins", "wins"), ("net", "net"), ("passes", "passes"), ("last_entry", "last_entry"),
    ("pass_reason", "pass_reason"),
]


def _gate_past() -> None:
    """Enforce the docs/22 section 9 history gate.

    The baseline cell runs without the past on purpose, and an attempt that is refused has
    to be told so in one line rather than left to read an empty answer as "nothing has
    ever been tried".
    """
    if os.environ.get("BT_PAST") != "on":
        print(PAST_DISABLED_MSG)
        raise typer.Exit(3)


def _past_ledger(settings):
    """The ledger, read-only; exit 4 when there is none, the way a cold board lens does."""
    lg = _open_ledger_ro(settings)
    if lg is None:
        print(f"error: no history yet ({settings.ledger_path} does not exist)")
        raise typer.Exit(4)
    return lg


def _check_past_filters(*, era=None, outcome=None, since=None) -> None:
    """Reject a malformed filter value (exit 2), before the ledger is opened.

    A bad flag is a usage error whether or not there is any history to run it against;
    checking after :func:`_past_ledger` made ``--outcome brilliant`` on a fresh box exit 4
    and say the ledger was missing, which is the wrong sentence and the wrong code.
    """
    if era is not None and era not in _PAST_ERAS:
        print(f"error: --era must be one of {', '.join(_PAST_ERAS)}, got {era!r}")
        raise typer.Exit(2)
    if outcome is not None and outcome not in _PAST_OUTCOMES:
        print(f"error: --outcome must be one of {', '.join(_PAST_OUTCOMES)}, got {outcome!r}")
        raise typer.Exit(2)
    if since is not None:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            print(f"error: --since must be a date, e.g. 2026-09-01, got {since!r}")
            raise typer.Exit(2) from None


def _past_scope(settings, lg, *, era, category, outcome, since, json_out) -> dict:
    """Print the era header and return the filter kwargs, the values already checked.

    The header is printed before anything else, for ``_banner``'s reason: an answer scoped
    to one era that does not say so reads as the whole history.
    """
    choice = era or _history.era_default(lg, settings)
    era_value = settings.history.current_era if choice == "current" else None
    _banner(f"era: {choice}" + (f" ({era_value})" if era_value else ""), json_out=json_out)
    return {"era": era_value, "category": category, "outcome": outcome, "since": since}


@past_app.callback(invoke_without_command=True)
def past_main(
    ctx: typer.Context,
    era: Annotated[str | None, typer.Option("--era", help="current | all")] = None,
    category: Annotated[str | None, typer.Option(help="narrow to one category")] = None,
    outcome: Annotated[str | None, typer.Option(help="win|loss|refused|nofill|pass")] = None,
    since: Annotated[str | None, typer.Option(help="on or after this date, YYYY-MM-DD")] = None,
    limit: Annotated[int, typer.Option(help="rows to print")] = _PAST_ROW_LIMIT,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """``bt past`` on its own is the map: the families table, filters and all.

    A session that types ``bt past --limit 60`` means the map with more rows, and being
    told "No such option" for it is a wasted turn. The subcommands are unchanged and
    ``bt past --help`` still lists them.
    """
    if ctx.invoked_subcommand is not None:
        return
    past_families(era=era, category=category, outcome=outcome, since=since,
                  limit=limit, json_out=json_out)


@past_app.command("families")
def past_families(
    era: Annotated[str | None, typer.Option("--era", help="current | all")] = None,
    category: Annotated[str | None, typer.Option(help="narrow to one category")] = None,
    outcome: Annotated[str | None, typer.Option(help="win|loss|refused|nofill|pass")] = None,
    since: Annotated[str | None, typer.Option(help="on or after this date, YYYY-MM-DD")] = None,
    limit: Annotated[int, typer.Option(help="rows to print")] = _PAST_ROW_LIMIT,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Every family the record touches, with its legs, wins, net and passes."""
    _gate_past()
    _check_past_filters(era=era, outcome=outcome, since=since)
    settings = _settings()
    lg = _past_ledger(settings)
    scope = _past_scope(settings, lg, era=era, category=category, outcome=outcome,
                        since=since, json_out=json_out)
    rows = _history.families(lg, **scope, limit=None)
    lg.close()
    _emit_rows(rows, _PAST_FAMILY_HEADERS, json_out=json_out, cap=limit, what="families")


@past_app.command("family")
def past_family(
    series: Annotated[str, typer.Argument(help="e.g. KXHIGHNY")],
    era: Annotated[str | None, typer.Option("--era", help="current | all")] = None,
    category: Annotated[str | None, typer.Option(help="narrow to one category")] = None,
    outcome: Annotated[str | None, typer.Option(help="win|loss|refused|nofill|pass")] = None,
    since: Annotated[str | None, typer.Option(help="on or after this date, YYYY-MM-DD")] = None,
    limit: Annotated[int, typer.Option(help="entries to print")] = _PAST_ROW_LIMIT,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """One family's history: every attempt that entered it, oldest first, then its totals."""
    _gate_past()
    _check_past_filters(era=era, outcome=outcome, since=since)
    settings = _settings()
    lg = _past_ledger(settings)
    scope = _past_scope(settings, lg, era=era, category=category, outcome=outcome,
                        since=since, json_out=json_out)
    data = _history.family(lg, series, **scope)
    lg.close()
    entries, totals = data["entries"], data["totals"]
    # Checked before the JSON branch: "no attempt has entered this family" is a property
    # of the question, not of the format the answer would have been printed in.
    if not entries and not totals["passes"]:
        print(f"error: no attempt has entered {series}")
        raise typer.Exit(4)
    if json_out:
        _print_json(data)
        return
    shown = entries[-limit:] if limit > 0 else []
    if len(shown) < len(entries):
        _notice(
            f"notice: showing the {len(shown)} most recent of {len(entries)} entries; "
            f"raise --limit for the older ones",
            json_out=False,
        )
    for e in shown:
        print(f"{e['day']} · {e['attempt_id']} · {e['cell']} · "
              f"net {e['net']} on {e['stake']} staked")
        print(f"  claim: {e['claim']}")
        for leg in e["legs"]:
            print(f"  {leg['ticker']} {leg['side']} @{leg['price']} "
                  f"×{leg['contracts']} → {leg['fill']}")
    print(
        f"totals: attempts {totals['attempts']} · legs {totals['legs']} · "
        f"filled {totals['filled']} · wins {totals['wins']} · staked {totals['stake']} · "
        f"net {totals['net']} · passes {totals['passes']}"
    )


@past_app.command("search")
def past_search(
    text: Annotated[str, typer.Argument(help="full-text query")],
    era: Annotated[str | None, typer.Option("--era", help="current | all")] = None,
    category: Annotated[str | None, typer.Option(help="narrow to one category")] = None,
    outcome: Annotated[str | None, typer.Option(help="win|loss|refused|nofill|pass")] = None,
    since: Annotated[str | None, typer.Option(help="on or after this date, YYYY-MM-DD")] = None,
    limit: Annotated[int, typer.Option(help="matches to return")] = _PAST_ROW_LIMIT,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Full-text search over claims, hypotheses and closing paragraphs; one record per hit."""
    _gate_past()
    _check_past_filters(era=era, outcome=outcome, since=since)
    settings = _settings()
    lg = _past_ledger(settings)
    scope = _past_scope(settings, lg, era=era, category=category, outcome=outcome,
                        since=since, json_out=json_out)
    hits = _history.search(lg, text, **scope, limit=None)
    lg.close()
    rows = hits[:max(0, limit)]
    if json_out:
        _print_json(rows)
        _cap_notice(len(rows), len(hits), "matches", json_out=True)
        return
    for row in rows:
        print(f"match in {row['kind']}: {row['snippet']}")
        print(row["record"])
        print()
    if not rows:
        print("(none)")
    _cap_notice(len(rows), len(hits), "matches", json_out=False)


@past_app.command("attempt")
def past_attempt(
    attempt_id: Annotated[str, typer.Argument(help="e.g. A-0187")],
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """One attempt in full: its ticket, its legs, its outcome and its reviews."""
    _gate_past()
    settings = _settings()
    lg = _past_ledger(settings)
    try:
        record = _history.render_record(lg, attempt_id, full=True)
        outcome = _history.outcome_record(lg, attempt_id)
    except KeyError:
        lg.close()
        print(f"error: attempt not found: {attempt_id}")
        raise typer.Exit(4) from None
    lg.close()
    if json_out:
        _print_json({"attempt_id": attempt_id, "record": record, "outcome": outcome})
        return
    print(record, end="")


@past_app.command("page")
def past_page(
    date: Annotated[str | None, typer.Option("--date", help="the latest page on or before "
                                             "this date, YYYY-MM-DD")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """The director's page: the standing direction, today's note and what it is watching."""
    _gate_past()
    settings = _settings()
    lg = _past_ledger(settings)
    run = _history.latest_valid_run(lg, on_or_before=date)
    lg.close()
    if run is None:
        print("error: no valid director page yet"
              + (f" on or before {date}" if date else ""))
        raise typer.Exit(4)
    if json_out:
        _print_json({
            "run_id": run["run_id"], "run_date": run["run_date"],
            "cohort_date": run["cohort_date"], "model": run["model"],
            "page_hash": run["page_hash"], "page_md": run["page_md"],
        })
        return
    print(f"page: {run['run_id']} · run {run['run_date']} · cohort {run['cohort_date']} · "
          f"{run['model']}")
    print(run["page_md"] or "(empty)")


@past_app.command("recent")
def past_recent(
    limit: Annotated[int, typer.Option(help="how many records")] = _PAST_RECENT_LIMIT,
    era: Annotated[str | None, typer.Option("--era", help="current | all")] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """The most recently completed attempts as short records, newest first.

    This is the static cell's own set, exposed. It takes no ``--category``, ``--outcome``
    or ``--since``: the set is defined as any cell, any outcome, newest first (docs/22
    section 8.5), and a filtered version of it would not be the thing an attempt reads.
    """
    _gate_past()
    _check_past_filters(era=era)
    settings = _settings()
    lg = _past_ledger(settings)
    scope = _past_scope(settings, lg, era=era, category=None, outcome=None,
                        since=None, json_out=json_out)
    complete = _history.recent_completed(lg, limit=None, era=scope["era"])
    ids = complete[:max(0, limit)]
    rows = [{"attempt_id": aid, "record": _history.render_record(lg, aid)} for aid in ids]
    lg.close()
    if json_out:
        _print_json(rows)
        _cap_notice(len(ids), len(complete), "records", json_out=True)
        return
    for row in rows:
        print(row["record"])
        print()
    if not rows:
        print("(none)")
    _cap_notice(len(ids), len(complete), "records", json_out=False)


# --------------------------------------------------------------------------- ticket preflight
def _resolve_ticket_dir(directory: str | None) -> Path:
    """Resolve the ticket directory for ``bt ticket validate`` (spec §13).

    Resolution order: an explicit ``DIR`` argument (absolute or relative, used as given) →
    else ``../ticket`` when it exists relative to the cwd → else
    ``$BT_ROOT/data/attempts/$BT_ATTEMPT_ID/ticket`` when both env vars are set → else a
    usage error (exit 2) naming all three options. The relative default broke when a
    session ran the command from the repo root; the env fallback recovers it.
    """
    if directory is not None:
        return Path(directory)
    dot_dot = Path("../ticket")
    if dot_dot.is_dir():
        return dot_dot
    attempt_id = os.environ.get("BT_ATTEMPT_ID")
    root = os.environ.get("BT_ROOT")
    if attempt_id and root:
        return Path(root).expanduser() / "data" / "attempts" / attempt_id / "ticket"
    print(
        "error: no ticket directory resolved. Pass DIR explicitly, or run from an attempt "
        "workspace so ../ticket resolves, or set BT_ATTEMPT_ID and BT_ROOT."
    )
    raise typer.Exit(2)


@ticket_app.command("validate")
def ticket_validate(
    directory: Annotated[
        str | None,
        typer.Argument(
            help=(
                "ticket directory; defaults to ../ticket, then "
                "$BT_ROOT/data/attempts/$BT_ATTEMPT_ID/ticket"
            )
        ),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json", help="machine-readable JSON")] = False,
) -> None:
    """Preflight a ticket dir (docs/22 section 5.5). Exit 0 when it ran (rejects
    included), 2 on usage.

    Every failure prints the plain reason beside its code: a whole-ticket one also names
    the field or the heading that is wrong, and a refused leg says why in the same
    sentence its ledger row will carry.
    """
    from betting_agent.harness.validate import parse_ticket, reason_for_code, validate_ticket

    d = _resolve_ticket_dir(directory)
    if not json_out:
        print(f"resolved ticket dir: {d}")
    if not d.is_dir():
        print(f"error: not a directory: {d}")
        raise typer.Exit(2)
    settings = _settings()
    client = _client_or_exit(settings)
    parsed = parse_ticket(d)
    outcome = validate_ticket(
        parsed,
        market_fetch=client.get_market,
        book_fetch=client.get_orderbook,
        settings=settings,
        now=utc_now(),
    )
    detail = list(getattr(parsed, "error_detail", []))
    if json_out:
        _print_json({
            "resolved_dir": str(d),
            "ticket_valid": outcome.ticket_valid,
            "whole_ticket_errors": parsed.whole_ticket_errors,
            "detail": detail,
            "bets": [{"ticket_index": b.spec.ticket_index, "ticker": b.spec.ticker,
                      "status": b.status, "reject_code": b.reject_code,
                      "reject_reason": b.reject_reason or reason_for_code(b.reject_code)}
                     for b in outcome.bets],
            "truncated": outcome.truncated_count,
        })
        return
    if parsed.whole_ticket_errors:
        print("whole-ticket errors: " + ", ".join(
            f"{c} ({reason_for_code(c)})" for c in parsed.whole_ticket_errors
        ))
        # The reason, not just the code (PC-2): a session repairing a V01 ticket needs to
        # know WHICH field is wrong, and these lines were computed and thrown away.
        for line in detail:
            print(f"  - {line}")
    for b in outcome.bets:
        reason = b.reject_reason or reason_for_code(b.reject_code)
        line = f"{b.spec.ticket_index} {b.spec.ticker} {b.status} {b.reject_code or ''}".rstrip()
        print(f"{line} ({reason})" if reason else line)
    if outcome.truncated_count:
        print(f"truncated: {outcome.truncated_count}")


if __name__ == "__main__":
    app()
