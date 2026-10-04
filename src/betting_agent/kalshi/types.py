"""Typed Kalshi payload models.

All models are built from raw Kalshi JSON via ``from_api`` classmethods and keep the
original dict in ``raw`` for anything not surfaced as a field. Every price is a
``Decimal`` (parsed from the API's fixed-point *dollar strings* e.g. ``"0.4200"``,
tolerating legacy cent integers) and every timestamp is a tz-aware ``datetime`` (UTC).

Field names below were pinned against LIVE production payloads on 2026-07-07
(read-only). Notable realities the shapes encode (see client.py docstring for the
full endpoint report):

* Prices arrive as ``*_dollars`` strings; sizes/volume/open-interest as ``*_fp``
  fixed-point strings. Fractional trading is enabled, so counts really are fractional:
  every money-bearing count (``Fill.count``, ``OrderResult.filled_count``) is an exact
  ``Decimal``, and only the display-only sizes (volume, open interest, ``best_bid_size``)
  truncate to int. ``best_ask_size``/``ask_depth`` stay exact Decimals too (docs/14 D3):
  V11's liquidity check is money-bearing, and int-truncating a fractional top-of-book
  once flipped a bet's sign (A-0020).
* Market payloads carry NO ``category`` (it lives on the parent *event*) and the
  eligibility fields are ``close_time`` and ``expected_expiration_time``.
* Market ``status`` is ``"active"`` when open and ``"finalized"`` when resolved
  (with ``result`` set to the winning side).
* Orderbooks serve only *bid* levels (``orderbook_fp.yes_dollars`` / ``no_dollars``);
  asks are derived: ``best_ask(side) = 1 - best_bid(opposite_side)``.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
_EMPTY = (None, "")


def parse_ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 string (``Z`` or offset) or an epoch (s/ms) into UTC."""
    if value in _EMPTY:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:  # milliseconds
            v /= 1000.0
        return datetime.fromtimestamp(v, tz=UTC)
    s = str(value).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_price(d: dict, base: str) -> Decimal | None:
    """Price for ``base`` (e.g. ``"yes_ask"``): prefer ``base_dollars`` string,
    fall back to legacy ``base`` cents integer / 100."""
    ds = d.get(f"{base}_dollars")
    if ds not in _EMPTY:
        return Decimal(str(ds))
    cents = d.get(base)
    if cents not in _EMPTY:
        return Decimal(str(cents)) / Decimal(100)
    return None


def reported_fee(d: dict) -> Decimal | None:
    """The exchange's OWN total fee for an order, or ``None`` when it reports none.

    ``taker_fees_dollars`` and ``maker_fees_dollars`` are leg TOTALS: what was actually
    charged for everything that filled, on the exchange's own rounding. They are the
    figure to store, and the reason to insist on it is ``average_fee_paid``, which sits
    beside them and is a PER-CONTRACT average the exchange has ALREADY rounded.
    Multiplying that by the fill count is how three live legs on 2026-09-17 came to carry
    $0.0099 against a charged $0.0100, and put the first nightly walk under the new regime
    $0.0003 out (docs/25). At one contract a leg the two agreed, which is why 352 earlier
    legs reconciled to the cent and sizing is what exposed it.

    Where the totals live matters (2026-09-27). The order listing (``GET
    /portfolio/orders``) carries them, and on every leg checked they equal the sum of
    that order's per-fill ``fee_cost`` and the money the balance actually moved. The only
    live create response on record (the canary, 2026-08-01) carries no total at all, so a
    fee stored at placement is usually the harness's own model, priced at the order's
    average fill price. That model is exact for an order that fills at one price. It is
    not exact for an order that walks several book levels: the exchange prices each piece
    at its own price and rounds the running total up to the next $0.0001, and a fee priced
    at the average comes out higher. A-0316-B01 bought 3 contracts in four pieces between
    $0.11 and $0.20. The model at the average said $0.0245 and the exchange charged
    $0.0243. The settle scan reads the listing's figure, which is the one to trust.
    """
    taker = parse_price(d, "taker_fees")
    maker = parse_price(d, "maker_fees")
    if taker is None and maker is None:
        return None
    return (taker or Decimal(0)) + (maker or Decimal(0))


def reported_cost(d: dict) -> Decimal | None:
    """What the exchange charged for an order's contracts, fee excluded, or ``None``.

    ``taker_fill_cost_dollars`` plus ``maker_fill_cost_dollars``, read off the order
    listing: the exchange's own total over every piece that filled, each at its own price.
    It is the figure a row's ``stake`` should hold. ``contracts x fill_price`` can only
    match it when the average fill price fits the ledger's four decimal places, and an
    order that fills in fractional pieces at several prices has an average that does not.
    A-0316-B01 filled 3 contracts for $0.4043, an average of $0.134766..., and neither
    $0.1347 (the average the create response reported, which the exchange truncates) nor
    $0.1348 (the same average rounded) gives that total when multiplied back by 3.
    """
    taker = parse_price(d, "taker_fill_cost")
    maker = parse_price(d, "maker_fill_cost")
    if taker is None and maker is None:
        return None
    return (taker or Decimal(0)) + (maker or Decimal(0))


def parse_count(d: dict, base: str) -> Decimal | None:
    """EXACT count/size/volume for ``base``: prefer the ``base_fp`` fixed-point string,
    fall back to the legacy ``base`` value. Always a ``Decimal``.

    Fractional trading is enabled, so a count is not an integer: bet A-0054-B01 was
    filled live on 2026-08-02 as ``"0.28"`` + ``"0.34"`` + ``"0.38"``. Truncating those
    toward zero (what this function used to do) turns a real position into a phantom
    no-fill and drops the fee with it (KC-2), so money-bearing counts stay exact and only
    the display-only sizes go through :func:`parse_count_int`.
    """
    fp = d.get(f"{base}_fp")
    if fp not in _EMPTY:
        return Decimal(str(fp))
    v = d.get(base)
    if v not in _EMPTY:
        return Decimal(str(v))
    return None


def parse_count_int(d: dict, base: str) -> int | None:
    """:func:`parse_count` truncated toward zero, for the int-typed *display* counts
    (volume, open interest). Never use it on a fill count — see KC-2."""
    c = parse_count(d, base)
    return None if c is None else int(c)


def derive_tick_size(d: dict) -> Decimal:
    """Representative tick: the ``price_ranges`` step covering 0.50 (the band most
    bets live in). Tapered markets have finer ticks near 0/1 — the full schedule is
    preserved in ``raw['price_ranges']`` for exact tick validation."""
    ranges = d.get("price_ranges")
    fallback: Decimal | None = None
    if isinstance(ranges, list):
        half = Decimal("0.5")
        for pr in ranges:
            try:
                start = Decimal(str(pr["start"]))
                end = Decimal(str(pr["end"]))
                step = Decimal(str(pr["step"]))
            except (KeyError, TypeError, ArithmeticError):
                continue
            if start <= half < end:
                return step
            if fallback is None:
                fallback = step
    return fallback if fallback is not None else Decimal("0.01")


# --------------------------------------------------------------------------- #
# models
# --------------------------------------------------------------------------- #
class Market(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: str
    title: str | None = None
    category: str | None = None
    status: str | None = None
    close_time: datetime | None = None
    expected_expiration: datetime | None = None
    yes_bid: Decimal | None = None
    yes_ask: Decimal | None = None
    no_bid: Decimal | None = None
    no_ask: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    tick_size: Decimal = Decimal("0.01")
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict, *, category: str | None = None) -> Market:
        return cls(
            ticker=d.get("ticker"),
            title=d.get("title"),
            # markets carry no category field today; caller may inject from the event
            category=category if category is not None else d.get("category"),
            status=d.get("status"),
            close_time=parse_ts(d.get("close_time")),
            expected_expiration=parse_ts(d.get("expected_expiration_time")),
            yes_bid=parse_price(d, "yes_bid"),
            yes_ask=parse_price(d, "yes_ask"),
            no_bid=parse_price(d, "no_bid"),
            no_ask=parse_price(d, "no_ask"),
            volume=parse_count_int(d, "volume"),
            open_interest=parse_count_int(d, "open_interest"),
            tick_size=derive_tick_size(d),
            raw=d,
        )

    @property
    def event_ticker(self) -> str | None:
        return self.raw.get("event_ticker")


class Orderbook(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: str | None = None
    # each level is (price, size); the API serves only bids for each side
    yes_levels: list[tuple[Decimal, Decimal]] = Field(default_factory=list)
    no_levels: list[tuple[Decimal, Decimal]] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict, *, ticker: str | None = None) -> Orderbook:
        ob = d.get("orderbook_fp") or d.get("orderbook") or {}
        return cls(
            ticker=ticker,
            yes_levels=cls._levels(ob.get("yes_dollars"), ob.get("yes")),
            no_levels=cls._levels(ob.get("no_dollars"), ob.get("no")),
            raw=d,
        )

    @staticmethod
    def _levels(dollar_levels: Any, cent_levels: Any) -> list[tuple[Decimal, Decimal]]:
        out: list[tuple[Decimal, Decimal]] = []
        if dollar_levels:
            for lvl in dollar_levels:
                out.append((Decimal(str(lvl[0])), Decimal(str(lvl[1]))))
        elif cent_levels:
            for lvl in cent_levels:  # legacy: [price_cents, size]
                out.append((Decimal(str(lvl[0])) / Decimal(100), Decimal(str(lvl[1]))))
        return out

    def _best_bid_level(self, side: str) -> tuple[Decimal, Decimal] | None:
        levels = self.yes_levels if side == "yes" else self.no_levels
        if not levels:
            return None
        return max(levels, key=lambda lvl: lvl[0])

    def best_bid(self, side: str) -> Decimal | None:
        lvl = self._best_bid_level(side)
        return lvl[0] if lvl else None

    def best_bid_size(self, side: str) -> int | None:
        lvl = self._best_bid_level(side)
        return int(lvl[1]) if lvl else None

    def best_ask(self, side: str) -> Decimal | None:
        """Best price to BUY ``side`` — derived from the opposite side's best bid
        (someone bidding NO at 0.22 offers YES at 0.78). ``None`` if that book is empty."""
        opp = "no" if side == "yes" else "yes"
        lvl = self._best_bid_level(opp)
        if lvl is None:
            return None
        return Decimal(1) - lvl[0]

    def best_ask_size(self, side: str) -> Decimal | None:
        """The top ask level's exact resting size (docs/14 D3).

        Used to truncate to ``int`` "on purpose" — a 0.96-contract top level read as
        size 0 and V11 rejected it outright, which flipped A-0020's sign (+$5 forgone
        edge). Money-bearing, like a fill count (KC-2): never truncate it again. Callers
        that want the FULL book's liquidity (not just the top level) want
        :meth:`ask_depth`, which is what V11 checks now.
        """
        opp = "no" if side == "yes" else "yes"
        lvl = self._best_bid_level(opp)
        return lvl[1] if lvl else None

    def ask_depth(self, side: str) -> Decimal:
        """Total resting size across every level that could sell ``side`` to us — the
        full book depth, not just the top price (docs/14 D3).

        V11 asks "is at least 1 contract obtainable here at all": a fractional
        top-of-book (e.g. 0.96) with real size resting behind it (A-0020's shape) is
        real, fillable liquidity that :meth:`best_ask_size` alone can't see. Summed
        across ALL levels on the opposite side, unconditional on price — whether a
        specific bet's limit actually crosses that depth is decided at execution, not
        here: paper fills replay this exact book snapshot (``best_ask(side) <=
        limit_price``, execute.py), and a real IOC lets the exchange itself decide
        fill/no-fill. Folding a limit-price filter into V11 as well would make that
        paper-fill branch unreachable (the same snapshot would already guarantee
        ``best_ask <= limit`` for anything that passed V11) and would reject tickets
        whose price simply hasn't crossed yet — a normal, informative no-fill (docs/14
        D12), not a malformed bet. Empty book -> ``Decimal(0)``, never ``None``: V11
        compares this against 1, and "no levels" and "not enough levels" are the same
        failure.
        """
        opp = "no" if side == "yes" else "yes"
        levels = self.yes_levels if opp == "yes" else self.no_levels
        return sum((size for _, size in levels), Decimal(0))


class Candle(BaseModel):
    """One market-price candlestick from ``GET /series/{s}/markets/{t}/candlesticks``.

    ``ts`` is the candle's ``end_period_ts`` (epoch seconds → UTC). ``open``/``high``/
    ``low``/``close`` are the YES-side *traded* prices in dollars, read from the payload's
    ``price`` sub-object (``{open,high,low,close}_dollars``); a period with no trades
    leaves them ``None``. ``volume`` and ``open_interest`` come from the ``*_fp``
    fixed-point strings (truncated to int, per the codebase's size convention). The full
    payload — including ``price.mean``/``price.previous`` and the separate ``yes_bid`` /
    ``yes_ask`` OHLC sub-objects — is preserved verbatim in ``raw``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ts: datetime | None = None
    open: Decimal | None = None
    high: Decimal | None = None
    low: Decimal | None = None
    close: Decimal | None = None
    volume: int | None = None
    open_interest: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> Candle:
        price = d.get("price") or {}
        return cls(
            ts=parse_ts(d.get("end_period_ts")),
            open=parse_price(price, "open"),
            high=parse_price(price, "high"),
            low=parse_price(price, "low"),
            close=parse_price(price, "close"),
            volume=parse_count_int(d, "volume"),
            open_interest=parse_count_int(d, "open_interest"),
            raw=d,
        )


class OrderResult(BaseModel):
    """The outcome of one ``create_order``, as ``KalshiClient`` synthesizes it.

    There is deliberately no ``from_api`` (KC-5). One existed, parsing the legacy
    ``/portfolio/orders`` ORDER object — a different shape from the V2 create response,
    with a side fallback (``{side}_price``) that predates V2's single YES-quoted book and
    would have read a NO order's price straight off the wrong leg. Nothing in production
    ever called it; only a test did, which is exactly how a wrong parser survives a
    migration. ``status`` is ``executed`` or ``canceled`` and nothing else: V2 returns no
    status field, and for an IOC the unfilled remainder is canceled by construction.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    order_id: str | None = None
    client_order_id: str | None = None
    status: str | None = None
    # exact, never truncated: fills can be fractional (KC-2)
    filled_count: Decimal = Decimal(0)
    avg_fill_price: Decimal | None = None
    fee: Decimal | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Fill(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    client_order_id: str | None = None
    ticker: str | None = None
    side: str | None = None
    # exact, never truncated: a single order can fill in fractional pieces (KC-2)
    count: Decimal = Decimal(0)
    price: Decimal | None = None
    ts: datetime | None = None
    is_taker: bool | None = None
    # What the exchange charged for this piece (``fee_cost``, dollars, six places live).
    # ``None`` when the payload carries none. Summed over an order's fills it equals the
    # order listing's fee total, which a fee priced at the average fill price does not
    # when the order filled at several prices (A-0391-B04, 2026-10-02).
    fee: Decimal | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict, *, client_order_id: str | None = None) -> Fill:
        side = d.get("side")
        # ``fee_cost`` is already dollars ("0.010500"), unlike the legacy cent prices, so
        # it must not go through parse_price.
        fee_raw = d.get("fee_cost_dollars")
        if fee_raw in _EMPTY:
            fee_raw = d.get("fee_cost")
        # fills carry no client_order_id (verified live) — caller injects it via the
        # orders->fills join keyed on order_id.
        return cls(
            client_order_id=d.get("client_order_id") or client_order_id,
            ticker=d.get("ticker") or d.get("market_ticker"),
            side=side,
            count=parse_count(d, "count") or Decimal(0),
            price=parse_price(d, "no_price" if side == "no" else "yes_price"),
            ts=parse_ts(d.get("created_time")) or parse_ts(d.get("ts")),
            is_taker=d.get("is_taker"),
            fee=None if fee_raw in _EMPTY else Decimal(str(fee_raw)),
            raw=d,
        )


class Settlement(BaseModel):
    """One ``/portfolio/settlements`` row: what the exchange paid US for that market.

    ``revenue`` is the money field and the reason this model grew past ticker/result.
    The payload denominates it in **CENTS** — verified against the recorded fixture
    (``tests/fixtures/recorded/settlements.json``: ``revenue: 200`` on a 2-contract YES
    position that won, i.e. $2.00) and against the live scalar settlement of
    ``KXNPBTOTAL-26AUG130500HIRYAK-12`` on 2026-08-15 (``revenue: 82`` on
    ``no_count_fp: "1.00"``, i.e. $0.82). It is surfaced here in **dollars**, like every
    other money field in this module, through :func:`parse_price` — which is exactly the
    cents-integer-to-dollars rule, and which will prefer a ``revenue_dollars`` string if
    the API ever grows one.

    ``value`` is NOT parsed. In both the recorded fixture and the live scalar record it
    duplicates ``revenue`` rather than carrying a per-contract settlement value (fixture:
    ``value: 200`` on 2 contracts, not 100), so reading it as per-contract would be a
    guess. The per-contract figure is derived from ``revenue`` and the record's own
    position counts instead — see :meth:`payout_for`.

    ``yes_count``/``no_count`` are the position the record describes, exact (``*_count_fp``
    fixed-point strings, fractional trading — KC-2).

    A settlement is per-ACCOUNT-position, not per-order: one record covers every contract
    we held in that market, however many ledger legs bought them, and its ``revenue``
    already reflects any position netting the exchange applied.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    ticker: str | None = None
    market_result: str | None = None
    # dollars (payload: cents). ``None`` when the row carries no revenue at all.
    revenue: Decimal | None = None
    yes_count: Decimal | None = None
    no_count: Decimal | None = None
    ts: datetime | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> Settlement:
        return cls(
            ticker=d.get("ticker"),
            market_result=d.get("market_result"),
            revenue=parse_price(d, "revenue"),  # cents -> dollars; see the class docstring
            yes_count=parse_count(d, "yes_count"),
            no_count=parse_count(d, "no_count"),
            ts=parse_ts(d.get("settled_time")) or parse_ts(d.get("settled_ts")),
            raw=d,
        )

    def payout_for(self, side: str | None, contracts: Any) -> Decimal | None:
        """Our credit for ``contracts`` on ``side``, or ``None`` when this record cannot say.

        For a ``scalar`` settlement there is no $1-per-winning-contract inference to make:
        the exchange pays an arbitrary per-contract value and the only record of it is
        ``revenue``. That is a TOTAL over the position the record describes, so the
        per-contract figure is ``revenue / own_side_count`` and our leg's credit is
        ``contracts x`` that. On the live A-0097 shape (``revenue`` $0.82,
        ``no_count_fp`` 1.00, our leg 1 NO contract) that is $0.82.

        ``None`` — "this record cannot answer, do not guess" — in three cases, each of
        which a caller must treat as "leave it for a human/a later pass" rather than
        substitute a number for:

        * no ``revenue`` on the row;
        * no position recorded on our own side (absent or zero), so there is nothing to
          divide by;
        * a nonzero position on BOTH sides, i.e. the record nets two opposite positions
          into one revenue figure. Splitting that total between the sides is unrecoverable
          from the record alone.

        Unquantized on purpose: the money conventions (4dp, ``moneymath.q4``) belong to the
        caller that writes the ledger, and this module deliberately depends on nothing.
        """
        if self.revenue is None:
            return None
        own = self.yes_count if side == "yes" else self.no_count
        other = self.no_count if side == "yes" else self.yes_count
        if own is None or own <= 0:
            return None
        if other is not None and other > 0:
            return None
        return Decimal(str(contracts)) * (self.revenue / own)


class Balance(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    dollars: Decimal
    raw: dict[str, Any] = Field(default_factory=dict)
    # The exchange's market value of the open positions, apart from the cash above
    # (``portfolio_value``, in cents; a ``_dollars`` twin is read first if one appears).
    # ``None`` when the response carries neither, so a caller can tell "no positions"
    # from "not reported" (the drawdown floor falls back to the ledger's cost then).
    positions: Decimal | None = None

    @classmethod
    def from_api(cls, d: dict) -> Balance:
        ds = d.get("balance_dollars")
        if ds not in _EMPTY:
            dollars = Decimal(str(ds))
        else:  # legacy cents
            dollars = Decimal(str(d.get("balance", 0))) / Decimal(100)
        positions = None
        pd, pc = d.get("portfolio_value_dollars"), d.get("portfolio_value")
        if pd not in _EMPTY:
            positions = Decimal(str(pd))
        elif pc not in _EMPTY:
            positions = Decimal(str(pc)) / Decimal(100)
        return cls(dollars=dollars, raw=d, positions=positions)


class ExchangeStatus(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    active: bool
    raw: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_api(cls, d: dict) -> ExchangeStatus:
        flags = [
            d.get("exchange_active"),
            d.get("trading_active"),
        ]
        present = [bool(f) for f in flags if f is not None]
        active = all(present) if present else False
        return cls(active=active, raw=d)
