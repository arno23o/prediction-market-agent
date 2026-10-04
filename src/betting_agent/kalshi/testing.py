"""In-memory fake exchange with the same public surface as ``KalshiClient``.

Later waves' tests depend on this: it lets execute/settle/E2E run without the network.
It returns the SAME typed models (``Market``, ``Orderbook``, ``OrderResult``, ``Fill``,
``Settlement``, ``Balance``, ``ExchangeStatus``) as the real client, so consumers cannot
tell the difference by type.

Usage sketch::

    fake = FakeKalshi()
    fake.add_market("KXHIGHNY-26JUL08-B85.5", title="High temp", close_time=dt,
                    yes_ask=Decimal("0.42"), yes_ask_size=500)
    r = fake.create_order("KXHIGHNY-26JUL08-B85.5", "yes", Decimal("0.42"), 2, "A-0001-B01")
    # r.filled_count == 2 (resting ask 0.42 <= limit 0.42, size 500 >= 2); fee per §8.
    fake.resolve("KXHIGHNY-26JUL08-B85.5", "yes")
    settlements, _ = fake.get_settlements()

Order scripting: ``set_order_behavior(ticker, behavior)`` with behavior in
``{"fill", "no_fill", "partial:N", "ambiguous", "ambiguous_after_fill",
"reject:<status>[:<body>]"}`` overrides the default
``min(count, resting ask size)``-iff-ask<=limit rule. ``reject:`` raises
``KalshiAPIError(status, body)`` — a definite rejection, so no order enters the book, no
fill is booked and the balance does not move (the request is still visible in
``orders_placed``, which records what was transmitted, not what landed).

Fidelity to the real client (WP5)
---------------------------------
The 410 sunset got through because this fake could not represent it, so the surface is
now pinned to ``KalshiClient``'s by ``tests/test_fake_contract.py``. What that pinning
required:

* ``create_order`` REFUSES what the client refuses — ``action != "buy"``, a side that is
  not ``yes``/``no`` (V2's wire vocabulary ``bid``/``ask`` included), an unsupported
  ``time_in_force``, an off-grid price, a fractional count — with the client's own
  messages, before anything is recorded (FK-1). An unknown ticker is a
  ``KalshiAPIError(404)``, not a graceful cancel.
* ``OrderResult`` speaks only the vocabulary production can observe: ``executed`` or
  ``canceled``, and a no-fill carries ``fee=None``/``avg_fill_price=None``/
  ``order_id=None`` (FK-3). ``partially_filled`` was a fake-only status that let tests
  pin a contract the real client never emits.
* Fills served by ``get_fills`` carry NO ``client_order_id`` (FK-4) — the live endpoint
  does not, which is why attribution is an orders->fills join at all. The coid is stamped
  back on by ``find_fills_by_client_order_id``, exactly as the client stamps it.
* Fills respect the book (FK-5/FK-6): the default rule fills ``min(count, ask size)`` at
  or under the limit, so organic partials are possible and an order for more than the
  displayed size no longer gets an exchange-impossible full fill.
* ``get_candlesticks`` honors the requested window (FK-7).

Fault injection (FK-2): ``set_order_behavior(..., "reject:<status>[:<body>]")``,
``"ambiguous"`` and ``"ambiguous_after_fill"`` cover the order path; ``fail_next(method,
exc)`` is the one-shot injector for the READ path; ``enforce_balance=True`` (opt-in,
default off) makes an order the balance cannot cover fail the way the exchange fails it,
with a 400.

Shared-account (§10) scripting: every ``create_order`` is also recorded as a raw order
dict served by ``get_orders``/``iter_orders`` (same shape as the live
``/portfolio/orders`` payload, which carries ``client_order_id`` — fills do not).
``add_personal_order(...)`` injects an order with an absent/foreign client_order_id
(a manual app trade); ``add_impostor_order(coid, ...)`` injects one whose id matches
our pattern without being ours. ``add_personal_fill(...)`` still injects bare fills
(used to script the reconcile join).

Money world (§8 reconciliation)
-------------------------------
The balance is DYNAMIC and world-consistent: a reconciliation walk over this fake
balances to the cent. Every event that moves money on the real exchange moves the fake
balance, and each move is appended to a debug trail (``balance_ledger()``):

* a **fill** debits ``contracts x fill_price + fee`` (the ceil-to-$0.0001 taker fee in
  ``_fee``, the live exchange's own model per docs/14 D5); a partial fill debits only the
  filled portion; a no-fill debits nothing;
* a **resolve** credits ``contracts x $1.00`` per winning fill, nothing per losing
  fill, and refunds ``stake + fee`` on a void (matching ``moneymath.bet_pnl``, where a
  void nets zero). Net effect per settled fill is therefore exactly ``bet_pnl``:
  win ``c*(1-p) - fee``, loss ``-(c*p) - fee``, void ``0``.
* ``set_balance(x)`` reseeds to ``x`` (recorded as a ``seed`` move), so the trail's
  running column always ends at ``balance`` and the deltas always sum to it.

Personal (non-harness) activity moves the balance too — ``add_personal_fill`` and the
filled portion of ``add_personal_order`` both debit. That is deliberate: §8 treats an
unexplained balance move as drift, so the fake must be able to *produce* drift against
a ledger-only walk. They are independent injection points, so scripting both for the
same trade debits twice; pass ``move_balance=False`` on one of them to script an order
and its fill as a single money event.

Request cost (EF-7)
-------------------
Costs that grow with account age were invisible to the suite, because this fake served
everything on one page and counted nothing. Two knobs fix that:

* ``.calls`` counts every request-shaped method by name (``fake.calls["get_orders"]``),
  and ``reset_calls()`` zeroes it. Paginated reads count ONE per page, so a caller that
  re-pages the world per bet shows up as the quadratic it is.
* ``page_size`` (default ``None`` = one page, exactly as before) makes ``get_orders``,
  ``get_fills``, ``get_settlements`` and ``get_markets`` emit real cursors. The default
  is off so no existing test changes behavior; the regression guards on
  ``settle_once``/``reconcile_once`` switch it on and simulate a year-old account.

``find_fills_by_client_order_id`` performs the real orders->fills join over those pages
rather than answering from the fill log, so it costs what production costs. Fills that
were injected directly with a ``client_order_id`` and no order behind them (the way many
tests script the join — ``ambiguous`` raises before an order is recorded, so the fill has
to be injected on its own) are still resolved, as a documented fake-only fallback. Under
FK-4 that coid lives only in the fake's private fill log: it is never served by
``get_fills``, and the fallback matches on it the way a bookkeeper would, not the way the
API would.
"""
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .client import KalshiAPIError, OrderAmbiguous, order_created_at
from .types import (
    Balance,
    Candle,
    ExchangeStatus,
    Fill,
    Market,
    Orderbook,
    OrderResult,
    Settlement,
)

_OPEN_STATUSES = {"open", "active"}
# The listing endpoint's ``status`` filter and a market's own ``status`` field speak
# different vocabularies: ``status=open`` selects markets whose field reads "active", and
# ``status=settled`` selects markets whose field reads "finalized" (kalshi/types.py, pinned
# live). The open half of that mapping has always been here; the settled half arrived with
# the board cache (docs/14 C1), which snapshots settled markets so ``bt series`` can count
# them — without it the fake could not represent a settled listing at all.
_SETTLED_STATUSES = {"settled", "finalized"}
_DECISIVE = frozenset({"yes", "no"})  # a side won; 'scalar' pays a value, else it is a void
_SCALAR = "scalar"
_ONE_DOLLAR = Decimal("1.00")
_Q4 = Decimal("0.0001")
_Q2 = Decimal("0.01")
_ZERO = Decimal(0)
# The tif values ``KalshiClient.create_order`` accepts, post-mapping ("ioc" -> the V2 enum).
_TIF_VALUES = ("immediate_or_cancel", "fill_or_kill", "good_till_canceled")
# Every read ``fail_next`` can script. Order-path faults are ``set_order_behavior``'s
# ``reject:``/``ambiguous``, so they are deliberately absent here.
_FAILABLE_READS = frozenset({
    "get_markets", "get_market", "get_orderbook", "get_balance", "get_orders",
    "get_fills", "get_settlements", "get_candlesticks",
})
# Server-side span cap on the candlesticks endpoint (verified live 2026-07-13/14).
_MAX_CANDLES = 5000
_DEFAULT_CANDLE_HOURS = 72  # the client's default lookback when no start is given


def _q4(x: Decimal) -> Decimal:
    """Quantize to the ledger's 4dp money convention (``moneymath.q4``, kept local so
    ``kalshi/`` stays free of parent-package imports)."""
    return Decimal(x).quantize(_Q4)


def _fp(count) -> str:
    """A count as the live API serves it: a fixed-point string, 2dp when that is exact
    (``"1.00"``), otherwise the count's own precision (``"0.28"``). Counts are Decimal
    everywhere on the money path, so this must never round one away."""
    d = Decimal(str(count))
    return str(d.quantize(_Q2)) if d == d.quantize(_Q2) else str(d)


def _fee(coef: Decimal, contracts, price: Decimal) -> Decimal:
    """Taker fee as the LIVE exchange charges it: ``ceil_to_0.0001(coef*C*P*(1-P))``.

    This fake stands in for the real exchange, so it charges what the real exchange was
    observed to charge — no 1¢ floor (docs/14 D5, from the fill-by-fill evidence in
    docs/12 §2). It deliberately duplicates ``moneymath.fee`` rather than importing it
    (``kalshi/`` stays free of parent-package imports); ``test_fake_kalshi`` asserts the
    two agree, which is what keeps the duplication honest.
    """
    raw = coef * Decimal(str(contracts)) * price * (Decimal(1) - price)
    return raw.quantize(_Q4, rounding=ROUND_CEILING)


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _event_ticker(ticker: str) -> str:
    """The event ticker a market belongs to (live settlements carry both)."""
    return ticker.rsplit("-", 1)[0] if "-" in ticker else ticker


class _FillRecord:
    """A fill plus the money facts the exchange knows but the ``Fill`` model does not:
    the fee actually charged (fills carry no fee field) and whether it has settled."""

    __slots__ = ("fill", "fee", "settled")

    def __init__(self, fill: Fill, fee: Decimal):
        self.fill = fill
        self.fee = fee
        self.settled = False

    @property
    def stake(self) -> Decimal:
        return _q4(Decimal(str(self.fill.count)) * Decimal(self.fill.price or 0))


class _FakeMarket:
    def __init__(
        self,
        ticker: str,
        *,
        title: str | None,
        category: str | None,
        close_time: datetime | None,
        status: str,
        tick_size: Decimal,
        yes_ask: Decimal | None,
        yes_ask_size: int,
        no_ask: Decimal | None,
        no_ask_size: int,
        volume: int | None = None,
        open_interest: int | None = None,
        last_price: Decimal | None = None,
        rules_primary: str | None = None,
        rules_secondary: str | None = None,
    ):
        self.ticker = ticker
        self.title = title
        self.category = category
        self.close_time = close_time
        self.status = status
        self.tick_size = tick_size
        self.yes_ask = yes_ask
        self.yes_ask_size = yes_ask_size
        self.no_ask = no_ask
        self.no_ask_size = no_ask_size
        self.volume = volume
        # Listing-payload fields the board cache reads (docs/14 C1/C2): open interest and
        # last price feed ``bt series``/``bt board``, and the rules text is what ``bt search``
        # searches. All optional and all absent by default, which is the live shape too —
        # plenty of markets carry no rules text and have never traded.
        self.open_interest = open_interest
        self.last_price = last_price
        self.rules_primary = rules_primary
        self.rules_secondary = rules_secondary
        self.result: str | None = None

    def ask(self, side: str) -> Decimal | None:
        return self.yes_ask if side == "yes" else self.no_ask

    def ask_size(self, side: str) -> int:
        return self.yes_ask_size if side == "yes" else self.no_ask_size

    def ask_size_exact(self, side: str) -> Decimal:
        """The resting size as the book really holds it (FK-5).

        Independent of ``Orderbook.best_ask_size``/``ask_depth`` (both exact Decimals
        themselves now, docs/14 D3): this reads ``_FakeMarket``'s own stored size
        directly, because the FILL rule needs it regardless of what a derived
        ``Orderbook`` would say — 0.9 contracts really are available, and an order for 1
        really does come back 0.9 filled. That is a live shape (A-0054-B01), so the fake
        has to be able to produce it.
        """
        return Decimal(str(self.ask_size(side) or 0))

    @property
    def event_ticker(self) -> str:
        return _event_ticker(self.ticker)

    def to_market(self) -> Market:
        return Market(
            ticker=self.ticker,
            title=self.title,
            category=self.category,
            status=self.status,
            close_time=self.close_time,
            expected_expiration=self.close_time,
            yes_ask=self.yes_ask,
            yes_bid=(Decimal(1) - self.no_ask) if self.no_ask is not None else None,
            no_ask=self.no_ask,
            no_bid=(Decimal(1) - self.yes_ask) if self.yes_ask is not None else None,
            volume=self.volume,
            open_interest=self.open_interest,
            tick_size=self.tick_size,
            # ``event_ticker`` is what ``Market.event_ticker`` reads, and what a bulk
            # listing gives a caller to resolve a category from (BT-3). Live payloads
            # carry it on every market; omitting it here made the fake unable to
            # represent the category path at all.
            raw={"ticker": self.ticker, "event_ticker": self.event_ticker,
                 "result": self.result or "",
                 **({"last_price_dollars": str(self.last_price)}
                    if self.last_price is not None else {}),
                 **({"rules_primary": self.rules_primary}
                    if self.rules_primary is not None else {}),
                 **({"rules_secondary": self.rules_secondary}
                    if self.rules_secondary is not None else {})},
        )

    def to_orderbook(self) -> Orderbook:
        # Encode asks as opposite-side bids so the real derivation (ask = 1 - opp bid)
        # reproduces the configured asks exactly.
        no_levels = (
            [(Decimal(1) - self.yes_ask, Decimal(self.yes_ask_size))]
            if self.yes_ask is not None
            else []
        )
        yes_levels = (
            [(Decimal(1) - self.no_ask, Decimal(self.no_ask_size))]
            if self.no_ask is not None
            else []
        )
        return Orderbook(ticker=self.ticker, yes_levels=yes_levels, no_levels=no_levels, raw={})


class FakeKalshi:
    def __init__(self, *, balance: Decimal | str = "10.0000", fee_coef: Decimal | str = "0.07",
                 page_size: int | None = None, enforce_balance: bool = False):
        self._markets: dict[str, _FakeMarket] = {}
        self._behavior: dict[str, str] = {}
        self._fill_log: list[_FillRecord] = []
        self._settlements: list[Settlement] = []
        # Tickers whose position the exchange netted (see ``net_matched_pair``): the
        # settlement record is still published for them, reporting the NET position.
        self._netted: set[str] = set()
        self._orders: list[dict] = []  # raw order dicts served by get_orders
        self._candles: dict[str, list[dict]] = {}  # raw candlestick payloads by ticker
        self._balance_log: list[dict] = []  # money trail, see balance_ledger()
        self.orders_placed: list[dict] = []
        self.balance: Decimal = Decimal(str(balance))
        self.fee_coef: Decimal = Decimal(str(fee_coef))
        self._order_seq = 0
        # EF-7. ``page_size=None`` keeps the historical single-page behavior; set it to
        # emit real cursors. ``calls`` counts one per request the real client would send.
        self.page_size: int | None = page_size
        self.calls: dict[str, int] = {}
        # FK-2. Opt-in (default off) so the deliberate-overspend tests — which need an
        # order the balance cannot cover to still go through — keep working unchanged.
        self.enforce_balance: bool = enforce_balance
        self._fail_next: dict[str, Exception] = {}
        self._record_balance("seed", self.balance, ts=datetime.now(UTC))

    # -- request accounting (EF-7) and read-side fault injection (FK-2) --------
    def _count(self, method: str) -> None:
        """Count one request — and raise a scripted failure if one is armed for it.

        Counted BEFORE it raises: an injected failure models a request that reached the
        server and came back wrong, so it costs a request like any other.
        """
        self.calls[method] = self.calls.get(method, 0) + 1
        exc = self._fail_next.pop(method, None)
        if exc is not None:
            raise exc

    def fail_next(self, method: str, exc: Exception | None = None) -> None:
        """Make the NEXT call to ``method`` raise ``exc``, once (FK-2).

        The read path had no way to fail at all, so every caller's "what if this lookup
        blows up mid-pass" branch — the settle market-fetch retry, reconcile's fills join,
        the canary's balance reads — was untestable without monkeypatching the object.
        One-shot by design: arm it, call the thing, the next call behaves normally.

        ``exc`` defaults to the shape the real client raises when GET retries are
        exhausted (``KalshiAPIError(None, ...)``, no HTTP status). Order-path faults are
        ``set_order_behavior``'s ``reject:``/``ambiguous``; an unknown or non-read method
        name raises ``ValueError`` immediately, because a fault injector that silently
        never fires is worse than no injector at all.
        """
        if method not in _FAILABLE_READS:
            raise ValueError(
                f"cannot inject a read failure into {method!r}; failable reads are "
                f"{sorted(_FAILABLE_READS)} (order faults: set_order_behavior)"
            )
        self._fail_next[method] = exc if exc is not None else KalshiAPIError(
            None, f"GET {method} failed after retries: injected"
        )

    def reset_calls(self) -> None:
        """Zero the request counters — the seam for "how many requests did THIS pass send"."""
        self.calls = {}

    def calls_total(self) -> int:
        return sum(self.calls.values())

    @staticmethod
    def _offset(cursor: str | None) -> int:
        """Cursors are opaque to callers; here they are just the next row index."""
        try:
            return max(0, int(cursor)) if cursor else 0
        except (TypeError, ValueError):
            return 0

    def _page(self, items: list, cursor: str | None, limit: int | None = None):
        """``(page, next_cursor)`` honoring ``page_size`` and the caller's ``limit``.

        With neither set this returns everything on one page with no cursor, which is what
        every pre-EF-7 test assumes. With ``page_size`` set, a caller that does not follow
        the cursor sees a truncated world — exactly the bug class the real API has and this
        fake could not previously express.

        A ``limit`` acts as a page size too, because on the wire that is what it is (FK-8).
        It used to slice the answer and return no cursor, so anything past ``limit``
        vanished silently — even through ``iter_markets``, whose whole job is to follow the
        cursor until the board is exhausted. A caller that pages now gets everything; one
        that reads a single page still gets exactly ``limit`` rows.
        """
        size = self.page_size if self.page_size is not None else limit
        if size is None:
            return list(items), None
        if limit is not None:
            size = min(size, limit)
        size = max(1, size)
        start = self._offset(cursor)
        page = items[start:start + size]
        nxt = str(start + size) if start + size < len(items) else None
        return page, nxt

    # -- money bookkeeping ----------------------------------------------------
    def _record_balance(self, reason: str, delta: Decimal, *, ts: datetime, **detail) -> None:
        """Append one entry to the money trail. ``delta`` is already applied to
        ``self.balance`` by the caller (or, for ``seed``, IS the new balance)."""
        self._balance_log.append(
            {"ts": ts, "reason": reason, "delta": _q4(delta), "running": _q4(self.balance),
             **detail}
        )

    def _move_balance(self, reason: str, delta: Decimal, *, ts: datetime, **detail) -> Decimal:
        """Apply ``delta`` (signed, dollars) to the balance and record it. Returns the
        new balance. A zero delta is still recorded — a processed-but-worthless event
        (a losing settlement) belongs in the trail, so the walk can prove it was seen."""
        delta = _q4(delta)
        self.balance = _q4(self.balance + delta)
        self._record_balance(reason, delta, ts=ts, **detail)
        return self.balance

    def balance_ledger(self) -> list[dict]:
        """Every balance mutation, oldest first: ``{ts, reason, delta, running, ...}``.

        Debug/assertion surface only (the real client has no such thing). ``reason`` is
        one of ``seed`` (constructor or ``set_balance``), ``fill``, ``personal_fill``,
        ``personal_order``, ``settlement_win``, ``settlement_loss`` (delta 0),
        ``settlement_void`` (stake+fee refund), ``settlement_scalar`` (the exchange's own
        per-contract value, fee kept), or ``position_netting`` (a matched YES/NO pair paid
        $1.00 a contract at the fill that matched it); extra keys carry the event's context
        (ticker, client_order_id, contracts, price, fee). Invariants: the last
        ``running`` equals ``balance``, and each ``running`` equals the running sum of
        ``delta`` (a ``seed`` delta is the jump to the seeded value)."""
        return [dict(e) for e in self._balance_log]

    # -- setup ----------------------------------------------------------------
    def add_market(
        self,
        ticker: str,
        *,
        title: str,
        category: str | None = None,
        close_time: datetime,
        yes_ask: Decimal | None = None,
        yes_ask_size: int = 0,
        no_ask: Decimal | None = None,
        no_ask_size: int = 0,
        status: str = "open",
        tick_size: Decimal = Decimal("0.01"),
        volume: int | None = None,
        open_interest: int | None = None,
        last_price: Decimal | None = None,
        rules_primary: str | None = None,
        rules_secondary: str | None = None,
    ) -> None:
        self._markets[ticker] = _FakeMarket(
            ticker,
            title=title,
            category=category,
            close_time=close_time,
            status=status,
            tick_size=tick_size,
            yes_ask=yes_ask,
            yes_ask_size=yes_ask_size,
            no_ask=no_ask,
            no_ask_size=no_ask_size,
            volume=volume,
            open_interest=open_interest,
            last_price=last_price,
            rules_primary=rules_primary,
            rules_secondary=rules_secondary,
        )

    def net_matched_pair(self, ticker: str, contracts: Decimal | str | int = 1, *,
                         ts: datetime | None = None) -> None:
        """Credit a matched YES/NO pair its guaranteed $1.00 per contract, now (docs/16 §5).

        Kalshi does not let one account hold both sides of a market. When a fill lands on
        the opposite side of an existing position the exchange NETS the matched contracts
        and pays the pair out **at that fill** — a guaranteed dollar each, before the market
        resolves — and its later settlement record then reports only the net position that
        remained. Three such pairs formed on 2026-08-15 and their +$3.00 was the bulk of
        the drift that HALTed the live system.

        The fake does not model netting inside ``create_order``: an order here fills against
        a scripted book, and inferring "this crosses a position you already hold" would put
        a second, guessing implementation of the exchange's matching engine into the test
        double. This is the explicit seam instead — a test that wants the netted world says
        so, in the world's own vocabulary, and the money moves for real so the balance walk
        still has something true to prove itself against.

        The matched fills are consumed: they are marked settled here, so a later ``resolve``
        pays only whatever net position remained and the settlement record it emits reports
        that net — ``revenue: 0`` when the pair was the whole position, which is exactly
        what the exchange published for all three Aug-15 markets. The ticker is remembered
        so that record is still emitted even when nothing remains.

        Matching consumes WHOLE fills, oldest first, one side against the other, and raises
        rather than splitting one — the fake's fills are per-order and a partial consumption
        would need a position model this double deliberately does not have.
        """
        want = Decimal(str(contracts))
        for side in ("yes", "no"):
            taken = Decimal(0)
            for rec in self._fill_log:
                if taken >= want:
                    break
                if rec.fill.ticker != ticker or rec.settled or (rec.fill.side or "yes") != side:
                    continue
                count = Decimal(str(rec.fill.count))
                if taken + count > want:
                    raise ValueError(
                        f"net_matched_pair({ticker!r}, {want}) would have to split a "
                        f"{count}-contract {side} fill; script whole fills instead"
                    )
                rec.settled = True
                taken += count
            if taken != want:
                raise ValueError(
                    f"net_matched_pair({ticker!r}, {want}) found only {taken} unsettled "
                    f"{side} contracts to match"
                )
        self._netted.add(ticker)
        self._move_balance(
            "position_netting", _q4(want * _ONE_DOLLAR),
            ts=ts or datetime.now(UTC), ticker=ticker, contracts=want,
        )

    def set_book(self, ticker: str, side: str, ask: Decimal | None, size: int) -> None:
        m = self._markets[ticker]
        if side == "yes":
            m.yes_ask, m.yes_ask_size = ask, size
        else:
            m.no_ask, m.no_ask_size = ask, size

    def set_order_behavior(self, ticker: str, behavior: str) -> None:
        """Script ``create_order`` for ``ticker``.

        ``"fill"`` | ``"no_fill"`` | ``"partial:N"`` | ``"ambiguous"`` |
        ``"ambiguous_after_fill"`` | ``"reject:<status>[:<body>]"`` |
        ``"levels:<count>@<price>,..."``. ``reject:`` raises
        ``KalshiAPIError(status, body)`` the way a definite 4xx rejection does — the order
        never lands, so nothing is added to the order book, no fill is booked and the
        balance does not move. ``levels:`` fills the order in pieces across several book
        levels, as the live exchange does when one level is not enough; see
        ``_fill_levels``.

        ``"fill"`` overrides the resting SIZE, not the limit price (FK-6): it fills the
        whole count out of a book that could only supply part of it, but never at a price
        above the limit — the exchange has no way to do that, so neither has this. Script
        a book the order can actually cross (or ``partial:N``, whose whole job is to
        divide a fill) when a test needs a fill at a given price.

        THE TWO AMBIGUITIES are opposite worlds, and the executor's whole ambiguity
        resolution exists to tell them apart:

        * ``"ambiguous"`` raises ``OrderAmbiguous`` BEFORE anything is recorded — the
          request never reached the matching engine, so there is no order, no fill and no
          balance move. This is the "truly absent" case the resolver must conclude no-fill
          for, after its bounded re-scan.
        * ``"ambiguous_after_fill"`` runs the order to completion first — the order lands
          in ``/portfolio/orders``, its fills are booked and the balance moves — and only
          then raises. This is the case that actually costs money: the trade happened and
          the receipt was lost. Before it existed, a test could only approximate that
          world with ``add_personal_fill(client_order_id=...)``, which hangs a fill on no
          order at all and so resolves through the documented fake-only fallback in
          ``find_fills_by_client_order_id`` rather than the real orders->fills join. With
          this behavior the resolver walks the same join it walks in production, against
          a coid that lives where the live API puts it: on the ORDER (FK-4).

        Scripted behavior is checked BEFORE the ticker exists: ``reject:``/``ambiguous``
        on an unregistered ticker still produce their scripted failure rather than the
        unknown-ticker 404, because a test that scripts an outcome for a request is
        declaring what the exchange did with it. ``ambiguous_after_fill`` is the exception
        — it has to reach the book to fill against it, so an unknown ticker still 404s.
        """
        self._behavior[ticker] = behavior

    def set_candles(self, ticker: str, candles: list[dict]) -> None:
        """Script the raw candlestick payloads ``get_candlesticks`` returns for ``ticker``
        (same shape as the live ``candlesticks`` array: ``end_period_ts``, a ``price``
        OHLC sub-object of ``*_dollars`` strings, ``volume_fp``/``open_interest_fp``)."""
        self._candles[ticker] = list(candles)

    def set_balance(self, dollars: Decimal | str) -> None:
        """Reseed the balance (unquantized, so ``str(balance)`` round-trips the input).
        Recorded in ``balance_ledger()`` as a ``seed`` move whose delta is the jump from
        the previous balance, keeping the trail's running sum consistent."""
        previous = self.balance
        self.balance = Decimal(str(dollars))
        self._record_balance(
            "seed", self.balance - previous, ts=datetime.now(UTC), previous=_q4(previous)
        )

    def resolve(self, ticker: str, result: str, *, ts: datetime | None = None,
                scalar_value: Decimal | str | None = None) -> None:
        """Resolve a market. ``result`` in {'yes','no','scalar','void'}. Marks the market
        finalized (status + result, unchanged) and settles the money.

        Every not-yet-settled fill on ``ticker`` is paid out: a fill on the winning side
        credits ``count x $1.00``, a fill on the losing side credits nothing, and any
        non-decisive result (``''``/``void``/``cancelled``) refunds ``stake + fee`` —
        mirroring ``settle.py``'s void semantics, so a void nets exactly zero.

        ``result="scalar"`` is the fourth shape, and the one that is neither (docs/16 §5):
        the exchange pays a value of its own choosing per contract and **keeps the fee**.
        ``scalar_value`` is that per-contract dollar figure **for the YES side**; a NO
        contract is paid ``1 − scalar_value``, the identity that makes a matched YES/NO
        pair still worth exactly $1.00. The live case this models is
        KXNPBTOTAL-26AUG130500HIRYAK-12 on 2026-08-15: a 1-contract NO position was paid
        $0.82, i.e. ``scalar_value=0.18``. ``scalar`` without a ``scalar_value`` is a
        programming error and raises rather than quietly paying zero.

        A ``Settlement`` becomes available from ``get_settlements`` only when a position
        was actually held (>= 1 fill), which is what the live ``/portfolio/settlements``
        reports; use ``add_settlement`` to script a settlement with no matching fill (the
        §8 settlements cross-check failure). Fills must precede the resolve — a fill
        booked on an already-resolved ticker stays unsettled. Re-resolving is safe: fills
        already settled are never paid twice.
        """
        if result == _SCALAR and scalar_value is None:
            raise ValueError("resolve(result='scalar') needs scalar_value (YES-side "
                             "dollars per contract)")
        yes_value = Decimal(str(scalar_value)) if scalar_value is not None else None
        now = ts or datetime.now(UTC)
        m = self._markets[ticker]
        m.result = result
        m.status = "finalized"

        won = result if result in _DECISIVE else None
        counts = {"yes": Decimal(0), "no": Decimal(0)}
        costs = {"yes": Decimal(0), "no": Decimal(0)}
        fees = Decimal(0)
        payout = Decimal(0)
        # A netted market still publishes a settlement record even when the netting
        # consumed the whole position — reporting revenue 0, which is what the exchange
        # published for all three Aug-15 pairs (docs/16 §5).
        held = ticker in self._netted

        for rec in self._fill_log:
            if rec.fill.ticker != ticker or rec.settled:
                continue
            rec.settled = True
            held = True
            side = rec.fill.side or "yes"
            counts[side] = counts.get(side, Decimal(0)) + Decimal(str(rec.fill.count))
            costs[side] = costs.get(side, Decimal(0)) + rec.stake
            fees += rec.fee
            common = {
                "ticker": ticker, "side": side, "contracts": rec.fill.count,
                "price": rec.fill.price, "client_order_id": rec.fill.client_order_id,
                "fee": rec.fee, "result": result,
            }
            if result == _SCALAR:  # the exchange's own value; the fee is NOT refunded
                per_contract = yes_value if side == "yes" else (_ONE_DOLLAR - yes_value)
                credit = _q4(Decimal(str(rec.fill.count)) * per_contract)
                self._move_balance("settlement_scalar", credit, ts=now, **common)
            elif won is None:  # void: stake and fee both come back
                credit = _q4(rec.stake + rec.fee)
                self._move_balance("settlement_void", credit, ts=now, **common)
            elif side == won:
                credit = _q4(Decimal(str(rec.fill.count)) * _ONE_DOLLAR)
                self._move_balance("settlement_win", credit, ts=now, **common)
            else:
                credit = Decimal(0)
                self._move_balance("settlement_loss", credit, ts=now, **common)
            payout += credit

        if held:
            self._settlements.append(
                self._make_settlement(
                    ticker, result, now,
                    yes_count=counts["yes"], no_count=counts["no"],
                    yes_cost=costs["yes"], no_cost=costs["no"],
                    fee_cost=fees, payout=payout,
                )
            )

    def _make_settlement(
        self,
        ticker: str,
        result: str,
        ts: datetime,
        *,
        yes_count=0,
        no_count=0,
        yes_cost: Decimal = Decimal(0),
        no_cost: Decimal = Decimal(0),
        fee_cost: Decimal = Decimal(0),
        payout: Decimal = Decimal(0),
    ) -> Settlement:
        """Build a ``Settlement`` from a raw payload shaped like the recorded live
        ``/portfolio/settlements`` row (``settled_time`` drives ``ts``; ``revenue`` and
        ``value`` are CENTS, per the fixture).

        Parsed THROUGH ``Settlement.from_api``, not assembled field by field beside the
        payload. The round-trip used to be a claim in this docstring rather than a fact:
        the model was constructed by hand, so every field ``from_api`` derives — the
        dollars-from-cents ``revenue`` and the exact ``*_count_fp`` counts the scalar path
        reads — came back ``None`` from this fake while the live client filled them in.
        A test double that silently drops the money field is worse than none.
        """
        cents = int((_q4(payout) * Decimal(100)).to_integral_value())
        return Settlement.from_api({
            "event_ticker": _event_ticker(ticker),
            "fee_cost": str(_q4(fee_cost)),
            "market_result": result,
            "no_count_fp": _fp(no_count),
            "no_total_cost_dollars": str(_q4(no_cost)),
            "revenue": cents,
            "settled_time": _iso_z(ts),
            "ticker": ticker,
            "value": cents,
            "yes_count_fp": _fp(yes_count),
            "yes_total_cost_dollars": str(_q4(yes_cost)),
        })

    def add_settlement(
        self,
        ticker: str,
        result: str = "yes",
        *,
        ts: datetime | None = None,
        contracts=1,
        price: Decimal | str = "0.50",
        side: str | None = None,
        revenue: Decimal | str | None = None,
    ) -> None:
        """Inject a settlement with no corresponding fill or ledger bet, and no balance
        move — the §8 ``settlements ⊆ our settled bets`` cross-check failure. Use
        ``resolve`` for real settlements; this is a bare payload injector.

        ``side`` places the injected position ("yes"/"no"); by default it sits on the
        winning side, or on yes for anything that did not resolve to a side. ``revenue``
        overrides the record's own payout in DOLLARS — needed to script a ``scalar``
        record, whose revenue is a value the exchange chose and no rule here can derive.
        """
        cost = _q4(Decimal(str(contracts)) * Decimal(str(price)))
        holding = side if side in ("yes", "no") else ("no" if result == "no" else "yes")
        side_kw = (
            {"no_count": contracts, "no_cost": cost}
            if holding == "no"
            else {"yes_count": contracts, "yes_cost": cost}
        )
        if revenue is not None:
            payout = _q4(Decimal(str(revenue)))
        else:
            payout = (
                _q4(Decimal(str(contracts)) * _ONE_DOLLAR) if result in _DECISIVE else cost
            )
        self._settlements.append(
            self._make_settlement(
                ticker, result, ts or datetime.now(UTC), payout=payout, **side_kw
            )
        )

    def _book_fill(
        self,
        fill: Fill,
        *,
        fee: Decimal,
        reason: str,
        move_balance: bool,
    ) -> _FillRecord:
        """Record a fill and (unless opted out) debit stake + fee from the balance."""
        rec = _FillRecord(fill, fee)
        self._fill_log.append(rec)
        if move_balance:
            self._move_balance(
                reason, -_q4(rec.stake + fee), ts=fill.ts or datetime.now(UTC),
                ticker=fill.ticker, side=fill.side, contracts=fill.count,
                price=fill.price, fee=fee, client_order_id=fill.client_order_id,
            )
        return rec

    def add_personal_fill(
        self,
        ticker: str,
        side: str,
        count,
        price: Decimal,
        *,
        client_order_id: str | None = None,
        ts: datetime | None = None,
        is_taker: bool = True,
        move_balance: bool = True,
    ) -> None:
        """Inject a fill with a foreign/absent client_order_id (manual app trade).

        Debits stake + fee like any other fill (taker fee per §8; a maker fill is free),
        because §8 must see somebody else's trade as an unexplained balance move. Pass
        ``move_balance=False`` to inject a fill whose money already moved (e.g. the fill
        behind an ``add_personal_order`` you already booked)."""
        price = Decimal(str(price))
        count = Decimal(str(count))  # counts are exact: fills can be fractional (KC-2)
        fee = _fee(self.fee_coef, count, price) if is_taker else Decimal("0")
        self._book_fill(
            Fill(
                client_order_id=client_order_id,
                ticker=ticker,
                side=side,
                count=count,
                price=price,
                ts=ts or datetime.now(UTC),
                is_taker=is_taker,
                raw={"ticker": ticker, "side": side},
            ),
            fee=fee,
            reason="personal_fill",
            move_balance=move_balance,
        )

    def add_personal_order(
        self,
        ticker: str,
        side: str = "yes",
        count=1,
        price: Decimal | str = "0.50",
        *,
        client_order_id: str | None = None,
        ts: datetime | None = None,
        status: str = "executed",
        order_id: str | None = None,
        filled=None,
        move_balance: bool = True,
        report_costs: bool = True,
        action: str = "buy",
    ) -> str:
        """Inject an order the harness did NOT place, as ``/portfolio/orders`` would
        report it. ``client_order_id=None`` omits the key entirely (a manual app
        trade); a foreign string exercises the personal path; a pattern-matching
        string not in the ledger is an impostor (see ``add_impostor_order``).
        Returns the ``order_id``.

        No ``Fill`` is created (orders and fills stay independent injection points — the
        §10 scan reads orders only), but the filled portion DOES debit the balance:
        somebody else's executed order is exactly the unexplained debit §8 calls drift.
        ``filled`` defaults to ``count``; ``move_balance=False`` injects a paper-only
        order.

        A filled order carries ``taker_fill_cost_dollars`` and ``taker_fees_dollars``, the
        way the live payload does (see ``tests/fixtures/recorded/orders.json``): those are
        what the settle pass reads to write a ``personal_orders`` row, and they are exactly
        the money this method moves, so a walk built from them reconciles against
        ``balance_ledger()`` to the cent. ``report_costs=False`` omits both, which is the
        older payload shape and the branch where the fee has to be modelled instead.

        ``action="sell"`` injects the other direction: an outside order closing a position rather
        than opening one. It CREDITS the account (proceeds in, fee out), which is why the
        settle pass refuses to book a sale as a cost."""
        price = Decimal(str(price))
        count = Decimal(str(count))
        filled = count if filled is None else Decimal(str(filled))
        when = ts or datetime.now(UTC)
        self._order_seq += 1
        oid = order_id or f"fake-order-{self._order_seq}"
        fee = _fee(self.fee_coef, filled, price) if filled > 0 else Decimal("0")
        order: dict = {
            "order_id": oid,
            "ticker": ticker,
            "action": action,
            "side": side,
            "type": "limit",
            "status": status,
            "created_time": _iso_z(when),
            "initial_count_fp": _fp(count),
            "fill_count_fp": _fp(filled),
            f"{side}_price_dollars": str(price),
        }
        if client_order_id is not None:
            order["client_order_id"] = client_order_id
        if report_costs and filled > 0:
            order["taker_fill_cost_dollars"] = str(_q4(filled * price))
            order["maker_fill_cost_dollars"] = "0.0000"
            order["taker_fees_dollars"] = str(_q4(fee))
            order["maker_fees_dollars"] = "0.0000"
        self._orders.append(order)
        if move_balance and filled > 0:
            notional = _q4(filled * price)
            delta = (notional - fee) if action == "sell" else -_q4(notional + fee)
            self._move_balance(
                "personal_order", delta, ts=when,
                ticker=ticker, side=side, contracts=filled, price=price, fee=fee,
                action=action, client_order_id=client_order_id, order_id=oid,
            )
        return oid

    def add_impostor_order(
        self,
        client_order_id: str,
        ticker: str = "KXIMPOSTOR",
        **kwargs,
    ) -> str:
        """Inject an order whose ``client_order_id`` matches our ``A-0000-B00`` pattern
        but was never placed by the harness — the §10 ``unknown_fill`` tripwire case."""
        return self.add_personal_order(ticker, client_order_id=client_order_id, **kwargs)

    # -- read surface (mirrors KalshiClient) ----------------------------------
    def get_exchange_status(self) -> ExchangeStatus:
        self._count("get_exchange_status")
        return ExchangeStatus(active=True, raw={"exchange_active": True, "trading_active": True})

    def get_balance(self) -> Balance:
        self._count("get_balance")
        return Balance(dollars=self.balance, raw={"balance_dollars": str(self.balance)})

    def get_markets(
        self,
        max_close_ts: int | None = None,
        status: str = "open",
        category: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[Market], str | None]:
        self._count("get_markets")
        out: list[Market] = []
        for m in self._markets.values():
            if status == "open" and m.status not in _OPEN_STATUSES:
                continue
            if status == "settled" and m.status not in _SETTLED_STATUSES:
                continue
            if status not in ("open", "settled", None) and m.status != status:
                continue
            if category is not None and m.category != category:
                continue
            if (
                max_close_ts is not None
                and m.close_time is not None
                and m.close_time.timestamp() > max_close_ts
            ):
                continue
            out.append(m.to_market())
        return self._page(out, cursor, limit)

    def iter_markets(
        self,
        max_close_ts: int | None = None,
        status: str = "open",
        category: str | None = None,
        limit: int = 200,
    ) -> Iterator[Market]:
        cursor: str | None = None
        while True:
            markets, cursor = self.get_markets(
                max_close_ts=max_close_ts, status=status, category=category,
                limit=limit, cursor=cursor,
            )
            yield from markets
            if not cursor:
                return

    def get_market(self, ticker: str) -> Market:
        self._count("get_market")
        m = self._markets.get(ticker)
        if m is None:
            raise KalshiAPIError(404, f"no such market {ticker}")
        return m.to_market()

    def event_category(self, event_ticker: str, *, raise_on_error: bool = True) -> str | None:
        """The category of any market under ``event_ticker`` (BT-3's client counterpart).

        The fake has no separate event objects, so the event's category is the category of
        its markets — which is how the live data reads too (category lives on the event
        and every market under it inherits it). An event with no markets is a 404, the
        same shape ``get_market`` gives an unknown ticker; ``raise_on_error=False``
        returns ``None`` instead, matching the client's enrichment posture.
        """
        self._count("event_category")
        for m in self._markets.values():
            if m.event_ticker == event_ticker:
                return m.category
        if raise_on_error:
            raise KalshiAPIError(404, f"no such event {event_ticker}")
        return None

    def get_orderbook(self, ticker: str, depth: int = 5) -> Orderbook:
        self._count("get_orderbook")
        m = self._markets.get(ticker)
        if m is None:
            raise KalshiAPIError(404, f"no such market {ticker}")
        return m.to_orderbook()

    def get_candlesticks(
        self,
        ticker: str,
        *,
        period_minutes: int = 60,
        start: datetime | None = None,
        end: datetime | None = None,
        series_ticker: str | None = None,
    ) -> list[Candle]:
        """The scripted candles for ``ticker`` that fall INSIDE the requested window (FK-7).

        It used to serve the whole scripted series regardless of what was asked for, so
        any caller that computes a window and trusts the API to honor it — ``bt history``,
        every future feature that reads price history — was untested against a server that
        actually does. Now ``start``/``end`` filter, exactly as the endpoint filters.

        Window and failure semantics mirror ``KalshiClient.get_candlesticks``:

        * defaults are the client's — ``end`` = now, ``start`` = ``end`` − 72h — so a
          caller relying on them behaves the same here as in production;
        * a missing ``period_minutes`` is ``KalshiAPIError(400)``: the client drops
          ``None`` params, and the endpoint 400s on a required query argument that never
          arrived (the reachable form of its "start_ts is required" refusal — ``start`` is
          not reachable, because the client always derives one);
        * a window wider than 5000 candles at the requested period is
          ``KalshiAPIError(400)``, the server's verified span cap;
        * an unknown ticker — neither scripted candles nor a registered market — is
          ``KalshiAPIError(404)``, raised FIRST, mirroring the client's series derivation
          failing before the candlesticks request is ever sent.

        The fake does not resample: candles are served at whatever period they were
        scripted at, so ``period_minutes`` governs the span cap, not the bucket size. A
        candle with no readable ``end_period_ts`` is retained rather than dropped.
        """
        self._count("get_candlesticks")
        if ticker not in self._candles and ticker not in self._markets:
            raise KalshiAPIError(404, f"no such market {ticker}")
        if not period_minutes or period_minutes <= 0:
            raise KalshiAPIError(
                400, "Bad Request", "Query argument period_interval is required"
            )
        end = end or datetime.now(UTC)
        start = start or end - timedelta(hours=_DEFAULT_CANDLE_HOURS)
        wanted = (end - start).total_seconds() / (period_minutes * 60)
        if wanted > _MAX_CANDLES:
            raise KalshiAPIError(
                400, "Bad Request",
                f"requested {int(wanted)} candles; the maximum is {_MAX_CANDLES}",
            )
        out: list[Candle] = []
        for raw in self._candles.get(ticker, []):
            candle = Candle.from_api(raw)
            if candle.ts is not None and not (start <= candle.ts <= end):
                continue
            out.append(candle)
        return out

    # -- order surface --------------------------------------------------------
    def create_order(
        self,
        ticker: str,
        side: str,
        price: Decimal,
        count,
        client_order_id: str,
        time_in_force: str = "ioc",
        action: str = "buy",
    ) -> OrderResult:
        """Place an IOC limit order, with the real client's refusals and result contract.

        VALIDATION (FK-1) runs first, in ``KalshiClient.create_order``'s order and with
        its messages verbatim: ``action``, ``side``, ``time_in_force``, an off-grid
        ``price``, a fractional ``count``. Each raises ``ValueError`` before ANYTHING is
        recorded — no counted request, no ``orders_placed`` entry — because the real
        client refuses these locally and never reaches the exchange. The fake used to
        accept all of them, which is how V2's ``side='bid'`` could be silently misrouted
        to the NO book and an ``action='sell'`` could debit like a buy.

        An UNKNOWN TICKER is deliberately the other kind of failure: ``KalshiAPIError(404,
        ...)``, the same shape ``get_market``/``get_orderbook`` give, and it DOES leave the
        request in ``orders_placed``. A 404 is the exchange answering, so the request was
        transmitted — the same split ``reject:`` already draws between "refused here" and
        "rejected there". Scripted behaviors are consulted before the ticker is looked up
        (see ``set_order_behavior``).

        FILLS are book-consistent (FK-5/FK-6). The default rule fills
        ``min(count, resting ask size)`` and only when the ask is at or under the limit,
        so an order for more than the displayed size fills PARTIALLY — organically, the
        way the exchange does it — and the size is read exactly: a 0.9-contract book fills
        0.9 of a 1-contract order. ``fill`` overrides the size but not the limit;
        ``partial:N`` overrides both.

        The RESULT speaks only the vocabulary production can observe (FK-3): ``executed``
        when anything filled, ``canceled`` when nothing did — for an IOC the unfilled
        remainder is canceled by construction, and the real client synthesizes nothing
        else. A no-fill carries ``fee=None``, ``avg_fill_price=None`` and
        ``order_id=None`` rather than the zero/synthetic values that let tests pin a
        contract production never sees. The canceled order itself still lands in the order
        log, because ``/portfolio/orders`` really does serve it.

        ``ambiguous_after_fill`` is the one behavior that produces every side effect and
        then raises instead of returning: the order is logged, its fills are booked and
        the balance moves, and only the RECEIPT is lost. See ``set_order_behavior``.
        """
        # -- refusals: local, before anything is recorded (FK-1) ---------------
        if action != "buy":
            raise ValueError(f"create_order only buys (the harness never sells): {action!r}")
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        tif = {"ioc": "immediate_or_cancel"}.get(time_in_force, time_in_force)
        if tif not in _TIF_VALUES:
            raise ValueError(f"unsupported time_in_force: {time_in_force!r}")
        p = Decimal(str(price))
        if p != p.quantize(_Q4):
            raise ValueError(f"price finer than the 4dp grid, refusing to re-price: {price!r}")
        c = Decimal(str(count))
        if c != c.to_integral_value():
            raise ValueError(f"count must be whole contracts, got {count!r}")

        self._count("create_order")
        price, count = p, c
        self.orders_placed.append(
            {
                "ticker": ticker,
                "side": side,
                "price": price,
                "count": count,
                "client_order_id": client_order_id,
                "time_in_force": time_in_force,
                "action": action,
            }
        )
        behavior = self._behavior.get(ticker, "default")

        if behavior == "ambiguous":
            raise OrderAmbiguous(client_order_id, "scripted ambiguous")

        if behavior.startswith("reject:"):
            # A definite rejection: the order never lands, so nothing below runs — no
            # order in the book, no fill, no balance move.
            parts = behavior.split(":", 2)  # reject:<status>[:<body>]
            raise KalshiAPIError(
                int(parts[1]), "rejected", parts[2] if len(parts) > 2 else None
            )

        market = self._markets.get(ticker)
        if market is None:  # FK-1: a 4xx, not a graceful cancel
            raise KalshiAPIError(404, f"no such market {ticker}")
        if behavior.startswith("levels:"):
            return self._fill_levels(ticker, side, price, count, client_order_id, action,
                                     behavior.split(":", 1)[1])
        ask = market.ask(side)
        available = market.ask_size_exact(side)
        crossable = ask is not None and ask <= price

        if behavior == "fill":  # overrides the SIZE, never the limit (FK-6)
            # ``ask is None`` means no book at all: nothing to price above, so a scripted
            # fill still stands (at the limit) rather than becoming a silent no-fill.
            filled = count if (ask is None or crossable) else _ZERO
        elif behavior == "no_fill":
            filled = _ZERO
        elif behavior.startswith("partial:"):
            filled = min(Decimal(behavior.split(":", 1)[1]), count)
        else:  # default: as much of the resting size as the limit can cross (FK-5)
            filled = min(count, available) if crossable else _ZERO
        filled = max(filled, _ZERO)

        fill_price = ask if (ask is not None) else price
        fee = _fee(self.fee_coef, filled, fill_price) if filled > 0 else None

        if filled > 0 and self.enforce_balance:
            # FK-2, opt-in: the exchange refuses an order it cannot fund. A 400 is a
            # definite rejection, so — like ``reject:`` — nothing lands and no money moves.
            cost = _q4(filled * fill_price + fee)
            if cost > self.balance:
                raise KalshiAPIError(
                    400, "Bad Request",
                    '{"error":{"code":"insufficient_balance","message":'
                    f'"order cost {cost} exceeds available balance {_q4(self.balance)}"}}}}',
                )

        self._order_seq += 1
        order_id = f"fake-order-{self._order_seq}"
        self._orders.append(
            {
                "order_id": order_id,
                "client_order_id": client_order_id,
                "ticker": ticker,
                "action": action,
                "side": side,
                "type": "limit",
                # The ORDER payload keeps the exchange's own vocabulary (which does report
                # a partial); FK-3 is about the OrderResult the client synthesizes.
                "status": "executed" if filled == count and filled > 0
                          else ("partially_filled" if filled > 0 else "canceled"),
                "created_time": _iso_z(datetime.now(UTC)),
                "initial_count_fp": _fp(count),
                "fill_count_fp": _fp(filled),
                f"{side}_price_dollars": str(price),
                # The money the live order object reports for what filled (docs/25): a
                # leg TOTAL, beside the per-contract average that must never be
                # multiplied. ``fee`` here is already the leg total, so the two agree.
                **({
                    "taker_fill_cost_dollars": str(_q4(filled * fill_price)),
                    "maker_fill_cost_dollars": "0.0000",
                    "taker_fees_dollars": str(_q4(fee)),
                    "maker_fees_dollars": "0.0000",
                } if filled > 0 else {}),
            }
        )

        if filled > 0:
            self._book_fill(
                Fill(
                    client_order_id=client_order_id,
                    ticker=ticker,
                    side=side,
                    count=filled,
                    price=fill_price,
                    ts=datetime.now(UTC),
                    is_taker=True,
                    raw={"ticker": ticker, "side": side, "order_id": order_id},
                ),
                fee=fee,
                reason="fill",
                move_balance=True,
            )
        if behavior == "ambiguous_after_fill":
            # The order LANDED and its fills are booked; only the receipt was lost. Raised
            # here, after every side effect, so the resolver has a real world to find.
            raise OrderAmbiguous(client_order_id, "scripted ambiguous after fill")
        if filled > 0:
            return OrderResult(
                order_id=order_id,
                client_order_id=client_order_id,
                status="executed",  # FK-3: filled is filled; there is no third status
                filled_count=filled,
                avg_fill_price=fill_price,
                fee=fee,
                raw={"order_id": order_id},
            )
        return OrderResult(
            order_id=None,
            client_order_id=client_order_id,
            status="canceled",
            filled_count=_ZERO,
            avg_fill_price=None,
            fee=None,
            # Nothing filled, so the fake claims nothing about the receipt. The canceled
            # order is still in the order log, where /portfolio/orders serves it.
            raw={},
        )

    def _fill_levels(self, ticker: str, side: str, price: Decimal, count: Decimal,
                     client_order_id: str, action: str, spec: str) -> OrderResult:
        """Fill one order in pieces across several book levels, as the live exchange does.

        ``spec`` is ``"count@price,count@price,..."`` on the bought side's own terms, in the
        order the book was walked. Each piece is its own fill at its own price. The fee is
        charged on the running total: a piece's fee is the step it adds to the exact
        running sum rounded up to $0.0001, so the order's fee is the ceiling of the exact
        sum, which is lower than a fee priced at the average when the prices are spread
        out. That is what the live fills of A-0316-B01 show (pieces of $0.0081, $0.0057,
        $0.0032 and $0.0073 for $0.0243, where the average prices at $0.0245).

        The order payload reports the exact cost and that fee. The result's average is the
        one the live create response reports, the YES-quoted average truncated to four
        places (true on every such live leg checked, 2026-09-27), so multiplying it back by
        the count misses the cost. The balance moves by each piece's cost and fee.
        """
        pieces = []
        for part in spec.split(","):
            c, p = part.split("@")
            pieces.append((Decimal(c), Decimal(p)))
        if any(p > price for _, p in pieces):
            raise ValueError("a scripted level is above the limit; nothing fills there")
        filled = sum((c for c, _ in pieces), _ZERO)
        if not _ZERO < filled <= count:
            raise ValueError(f"scripted levels fill {filled} of a {count}-contract order")
        cost = _q4(sum((c * p for c, p in pieces), _ZERO))
        running, charged, fees = _ZERO, _ZERO, []
        for c, p in pieces:
            running += self.fee_coef * c * p * (Decimal(1) - p)
            total = running.quantize(_Q4, rounding=ROUND_CEILING)
            fees.append(total - charged)
            charged = total

        self._order_seq += 1
        order_id = f"fake-order-{self._order_seq}"
        self._orders.append({
            "order_id": order_id, "client_order_id": client_order_id, "ticker": ticker,
            "action": action, "side": side, "type": "limit",
            "status": "executed" if filled == count else "partially_filled",
            "created_time": _iso_z(datetime.now(UTC)),
            "initial_count_fp": _fp(count), "fill_count_fp": _fp(filled),
            f"{side}_price_dollars": str(price),
            "taker_fill_cost_dollars": str(cost), "maker_fill_cost_dollars": "0.0000",
            "taker_fees_dollars": str(charged), "maker_fees_dollars": "0.0000",
        })
        now = datetime.now(UTC)
        for (c, p), piece_fee in zip(pieces, fees, strict=True):
            self._book_fill(
                Fill(client_order_id=client_order_id, ticker=ticker, side=side, count=c,
                     price=p, ts=now, is_taker=True, fee=piece_fee,
                     raw={"ticker": ticker, "side": side, "order_id": order_id,
                          "fee_cost": str(piece_fee)}),
                fee=piece_fee, reason="fill", move_balance=True,
            )
        average = cost / filled
        yes_average = average if side == "yes" else Decimal(1) - average
        reported = yes_average.quantize(_Q4, rounding=ROUND_FLOOR)
        return OrderResult(
            order_id=order_id, client_order_id=client_order_id, status="executed",
            filled_count=filled,
            avg_fill_price=reported if side == "yes" else Decimal(1) - reported,
            fee=charged, raw={"order_id": order_id},
        )

    # -- orders (raw dicts, mirrors KalshiClient.get_orders) -------------------
    def get_orders(
        self, min_ts: datetime | None = None, cursor: str | None = None
    ) -> tuple[list[dict], str | None]:
        """One page of raw order dicts (everything on page 1 unless ``page_size`` is set).

        Same ``min_ts`` semantics as the real client: filtered client-side on
        ``created_time``; an order missing that field is retained. The filter is applied
        BEFORE paging, as the client's is, so a narrowed window genuinely costs fewer
        pages — which is the whole point of settle's incremental scan (EF-2)."""
        self._count("get_orders")
        orders = [
            dict(o) for o in self._orders
            if min_ts is None
            or (created := order_created_at(o)) is None
            or created >= min_ts
        ]
        return self._page(orders, cursor)

    def iter_orders(self, min_ts: datetime | None = None) -> Iterator[dict]:
        cursor: str | None = None
        while True:
            orders, cursor = self.get_orders(min_ts=min_ts, cursor=cursor)
            yield from orders
            if not cursor:
                return

    # -- fills / settlements --------------------------------------------------
    def get_fills(
        self, min_ts: datetime | None = None, cursor: str | None = None
    ) -> tuple[list[Fill], str | None]:
        """One page of fills (everything on page 1 unless ``page_size`` is set).

        Fills carry NO ``client_order_id`` (FK-4). The live endpoint serves only
        ``order_id``, which is the whole reason attribution is an orders->fills join (see
        ``find_fills_by_client_order_id``). Serving the coid here meant anything written
        against fake fills "worked" in the suite and would have read ``None`` in
        production — and §10's impostor discriminator, which turns on personal fills
        having no coid to match, could not be expressed at all. The coid stays in the
        fake's private log; this read strips it, and serves copies, so a caller cannot
        reach back into the log through a served fill either.

        ``min_ts`` filters on the fill's creation time — the field the live server
        filters on, surfaced by ``Fill.from_api`` as ``ts``. A fill with an
        unparseable/absent ``ts`` is RETAINED, the same rule ``get_orders`` states: the
        §8/§10 tripwires must never silently drop account activity."""
        self._count("get_fills")
        fills = [
            r.fill.model_copy(update={"client_order_id": None})
            for r in self._fill_log
            if min_ts is None or r.fill.ts is None or r.fill.ts >= min_ts
        ]
        return self._page(fills, cursor)

    def get_settlements(
        self, min_ts: datetime | None = None, cursor: str | None = None
    ) -> tuple[list[Settlement], str | None]:
        """One page of settlements. ``min_ts`` filters on the settled time (``ts``, from
        the payload's ``settled_time``); a settlement missing it is retained, as in
        ``get_fills``."""
        self._count("get_settlements")
        settlements = [
            s for s in self._settlements if min_ts is None or s.ts is None or s.ts >= min_ts
        ]
        return self._page(settlements, cursor)

    def find_fills_by_client_order_id(self, client_order_id: str) -> list[Fill]:
        """The real orders->fills join, over this fake's own pages (EF-7).

        This used to answer straight out of the fill log — one dict scan, no requests —
        which is exactly why a reconciliation that called it once per bet, each call
        re-paging the entire order history, looked free in the suite and was quadratic in
        production. Now it costs what production costs: page ``/portfolio/orders`` until
        the ``client_order_id`` matches (a coid is an idempotency key, so the search stops
        at the first hit), then one fills lookup for that ``order_id``.

        The returned fills carry ``client_order_id`` — STAMPED here, exactly as the client
        stamps it onto the payloads it gets back (FK-4). That is the only place a coid and
        a fill ever meet: ``get_fills`` serves none, because the endpoint serves none.

        The fallback for fills injected WITHOUT an order behind them stays, and is a
        fake-only convenience rather than a claim about the API: it matches on the fake's
        private bookkeeping, not on anything the wire carries. Prefer
        ``set_order_behavior(..., "ambiguous_after_fill")`` for the "order landed, ACK
        lost" world — that books the order and its fills before raising, so this method
        runs its real join instead of the fallback.
        """
        order_id = None
        for o in self.iter_orders():
            if o.get("client_order_id") == client_order_id and o.get("order_id"):
                order_id = str(o["order_id"])
                break
        if order_id is None:
            return [
                self._stamped(r.fill, client_order_id)
                for r in self._fill_log
                if r.fill.client_order_id == client_order_id
            ]
        self._count("get_fills_by_order")  # GET /portfolio/fills?order_id=...
        return [
            self._stamped(r.fill, client_order_id)
            for r in self._fill_log
            if str((r.fill.raw or {}).get("order_id") or "") == order_id
            or (
                r.fill.client_order_id == client_order_id
                and not (r.fill.raw or {}).get("order_id")
            )
        ]

    @staticmethod
    def _stamped(fill: Fill, client_order_id: str) -> Fill:
        """A copy of ``fill`` with the joined coid stamped on (the client's last step)."""
        return fill.model_copy(update={"client_order_id": client_order_id})
