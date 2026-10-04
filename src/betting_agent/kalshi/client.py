"""Thin synchronous Kalshi trade-api/v2 client.

ENDPOINTS — verified LIVE read-only against production on 2026-07-07 (HTTP 200 each;
zero orders placed). ``path`` in every signature INCLUDES the ``/trade-api/v2`` prefix
and EXCLUDES the query string.

    GET  /trade-api/v2/exchange/status
    GET  /trade-api/v2/markets                       (params: status, max_close_ts,
                                                       category*, mve_filter, limit,
                                                       cursor)
    GET  /trade-api/v2/markets/{ticker}
    GET  /trade-api/v2/markets/{ticker}/orderbook    (param: depth)
    GET  /trade-api/v2/series/{series}/markets/{ticker}/candlesticks
                                                     (params: start_ts, end_ts [epoch s,
                                                      BOTH required], period_interval
                                                      [minutes: 1/60/1440]; ≤5000 candles
                                                      per call — verified 2026-07-13/14)
    GET  /trade-api/v2/events/{event_ticker}         (category lookup — see below; also
                                                       carries series_ticker, used to
                                                       build the candlesticks path)
    GET  /trade-api/v2/portfolio/balance
    GET  /trade-api/v2/portfolio/fills               (params: min_ts, order_id, cursor)
    GET  /trade-api/v2/portfolio/orders              (server ignores its filters; the
                                                       client filters min_ts client-side
                                                       on created_time — see get_orders)
    GET  /trade-api/v2/portfolio/settlements         (params: min_ts, cursor)
    POST /trade-api/v2/portfolio/orders              [request/response verified only
                                                       for the ORDER OBJECT shape via
                                                       GET /portfolio/orders; the create
                                                       REQUEST body is built to Kalshi's
                                                       docs — see create_order — and is
                                                       marked [verify at go-live]]

AUTH (verified): headers ``KALSHI-ACCESS-KEY`` (key id), ``KALSHI-ACCESS-TIMESTAMP``
(epoch ms), ``KALSHI-ACCESS-SIGNATURE`` = base64(RSA-PSS-SHA256, salt_length=32, over
``f"{ts_ms}{METHOD}{path}"``).

CORRECTIONS / DISCOVERIES vs. the spec's Phase-0 guesses:
* ``category`` is NOT on the market payload and the ``category`` query param is IGNORED
  server-side (verified) — category lives on the parent EVENT. ``get_market`` enriches
  it via a cached ``GET /events/{event_ticker}`` lookup; ``get_markets`` (bulk) leaves
  ``category=None`` for speed. The param is still forwarded (forward-compatible no-op).
  Callers filtering a bulk listing by category use ``event_category(event_ticker)``
  directly — one cached lookup per distinct EVENT, not a market GET per row.
* Fills carry NO ``client_order_id`` (only ``order_id``). Attribution is an
  orders->fills join: ``GET /portfolio/orders`` (which DOES carry ``client_order_id``)
  paged and matched client-side (the ``client_order_id`` query filter is IGNORED), then
  ``GET /portfolio/fills?order_id=...`` (that filter WORKS). See
  ``find_fills_by_client_order_id``.
* Market ``status`` values are ``"active"`` (open) and ``"finalized"`` (resolved,
  ``result`` set). The list ``status`` filter still accepts ``"open"``.
* ``mve_filter=exclude`` keeps multi-variant events (the parlay families) out of the
  listing, SERVER-SIDE, and is sent on every ``get_markets`` call. Verified live on
  2026-09-17: with it the first page holds no ``KXMVECROSSCATEGORY`` rows. It matters
  because parlays are about 2.5 million of the roughly 2.6 million rows the endpoint will
  serve, and filtering them out client-side meant paging all of them: 2 hours 40 minutes
  per board refresh against a 150-minute cadence, so a pull was still running when the
  next one was due. Every caller in this system wants parlays out, so the param is not
  optional; ``board.py``'s series exclusion stays as the belt to this braces.
* Everything is fixed-point: prices as ``*_dollars`` strings, counts/sizes as ``*_fp``.
* Order prices are serialized to the API as dollar strings (``{side}_price_dollars``).

RETRY: GETs retry x3 with 0.5s/2s/8s backoff (jittered, and a 429's ``Retry-After`` wins
over the ladder) on 429/5xx/transport/timeout. ``create_order``
NEVER auto-retries: on timeout or 5xx it raises ``OrderAmbiguous`` (the executor then
re-queries fills by client_order_id before deciding — never a blind re-send). GETs are
throttled to ``throttle_rps`` (default 5).
"""
from __future__ import annotations

import base64
import random
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from .types import (
    Balance,
    Candle,
    ExchangeStatus,
    Fill,
    Market,
    Orderbook,
    OrderResult,
    Settlement,
    parse_count,
    parse_ts,
    reported_fee,
)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_BACKOFFS = (0.5, 2.0, 8.0)
# Up to +50% on each step. The board child and a session's own reads throttle against the
# same account, so an unjittered ladder has everything that got a 429 coming back in
# lockstep and asking for the same refusal again.
_JITTER = 0.5
# A server-set Retry-After is obeyed over the ladder, but never past this: the refresh is
# on a clock, and a stray or absurd header must not park a pull for an hour.
_RETRY_AFTER_MAX = 60.0
_Q4 = Decimal("0.0001")


def _retry_wait(backoff: float, retry_after: str | None) -> float:
    """Seconds to wait before the next attempt: the server's number, else a jittered step.

    A 429 carrying ``Retry-After`` is the exchange stating the rate it will accept, and
    coming back on our own 0.5 s guess instead is how a throttle becomes a block. Only the
    numeric (seconds) form is read, which is the form Kalshi sends; an HTTP-date falls back
    to the ladder rather than being parsed for one header.
    """
    if retry_after:
        try:
            return max(0.0, min(float(str(retry_after).strip()), _RETRY_AFTER_MAX))
        except ValueError:
            pass
    return backoff * (1.0 + random.random() * _JITTER)


# --------------------------------------------------------------------------- #
# signing
# --------------------------------------------------------------------------- #
def _sign_with_key(key: RSAPrivateKey, message: str) -> str:
    signature = key.sign(
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def sign_pss(private_key_pem: bytes, message: str) -> str:
    """RSA-PSS-SHA256 (salt length 32) over ``message``, base64-encoded.

    Pure function: loads ``private_key_pem`` and signs. The client caches the loaded
    key for per-request signing; this standalone form is what the unit tests exercise
    against a throwaway keypair.
    """
    key = serialization.load_pem_private_key(private_key_pem, password=None)
    return _sign_with_key(key, message)


# --------------------------------------------------------------------------- #
# exceptions
# --------------------------------------------------------------------------- #
class KalshiAPIError(Exception):
    """A definite (non-ambiguous) upstream error: a non-retryable 4xx, or retries
    exhausted. Carries the HTTP status (``None`` for transport-level failures)."""

    def __init__(self, status: int | None, message: str, body: str | None = None):
        self.status = status
        self.body = body
        # The body names the offending field/endpoint — without it, the 2026-07-31
        # HTTP 410 incident surfaced as a bare "Gone" through three attempts and a
        # canary before anyone could see WHAT was gone.
        detail = f" — {body.strip()[:300]}" if body and body.strip() else ""
        super().__init__(f"HTTP {status}: {message}{detail}")


class OrderAmbiguous(Exception):
    """Raised by ``create_order`` when it cannot know whether the order landed
    (timeout or 5xx). Carries ``client_order_id`` so the executor can re-query fills."""

    def __init__(self, client_order_id: str, cause: str = ""):
        self.client_order_id = client_order_id
        super().__init__(f"order outcome ambiguous for client_order_id={client_order_id}: {cause}")


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class KalshiClient:
    def __init__(
        self,
        base_url: str,
        key_id: str,
        private_key_path: str | Path,
        transport: httpx.BaseTransport | None = None,
        throttle_rps: float = 5.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._base_path = urlsplit(self._base_url).path.rstrip("/")
        self._key_id = key_id
        key_bytes = Path(private_key_path).expanduser().read_bytes()
        self._key = serialization.load_pem_private_key(key_bytes, password=None)
        self._client = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._min_interval = 1.0 / throttle_rps if throttle_rps > 0 else 0.0
        self._last_get = 0.0
        self._event_category_cache: dict[str, str | None] = {}
        self._series_ticker_cache: dict[str, str] = {}

    # -- lifecycle ------------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> KalshiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low-level ------------------------------------------------------------
    def _headers(self, method: str, endpoint_path: str) -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        signed_path = self._base_path + endpoint_path
        return {
            "KALSHI-ACCESS-KEY": self._key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": _sign_with_key(self._key, f"{ts}{method}{signed_path}"),
            "Accept": "application/json",
        }

    def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        wait = self._min_interval - (time.monotonic() - self._last_get)
        if wait > 0:
            time.sleep(wait)
        self._last_get = time.monotonic()

    def _get(self, endpoint_path: str, params: dict | None = None) -> dict:
        """GET with signing, throttle, and 3x retry on 429/5xx/transport errors."""
        params = {k: v for k, v in (params or {}).items() if v is not None}
        last_exc: Exception | None = None
        for attempt in range(len(_BACKOFFS) + 1):
            self._throttle()
            headers = self._headers("GET", endpoint_path)  # fresh timestamp each attempt
            retry_after = None
            try:
                r = self._client.get(self._base_url + endpoint_path, params=params, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
            else:
                if r.status_code == 200:
                    return r.json()
                if r.status_code in _RETRYABLE_STATUS:
                    last_exc = KalshiAPIError(r.status_code, r.reason_phrase, r.text)
                    if r.status_code == 429:
                        retry_after = r.headers.get("Retry-After")
                else:
                    raise KalshiAPIError(r.status_code, r.reason_phrase, r.text)
            if attempt < len(_BACKOFFS):
                time.sleep(_retry_wait(_BACKOFFS[attempt], retry_after))
        if isinstance(last_exc, KalshiAPIError):
            raise last_exc
        raise KalshiAPIError(None, f"GET {endpoint_path} failed after retries: {last_exc}")

    # -- exchange / account ---------------------------------------------------
    def get_exchange_status(self) -> ExchangeStatus:
        return ExchangeStatus.from_api(self._get("/exchange/status"))

    def get_balance(self) -> Balance:
        return Balance.from_api(self._get("/portfolio/balance"))

    # -- markets --------------------------------------------------------------
    def get_markets(
        self,
        max_close_ts: int | None = None,
        status: str = "open",
        category: str | None = None,
        limit: int = 200,
        cursor: str | None = None,
    ) -> tuple[list[Market], str | None]:
        """One page of markets. Returns ``(markets, next_cursor)``; ``next_cursor`` is
        ``None``/``""`` when exhausted. ``category`` is forwarded but ignored server-side
        (see module docstring) and markets come back with ``category=None``.

        ``mve_filter=exclude`` is always sent, so the parlay families never reach a caller
        and never cost a page. It is not a parameter because there is no caller in this
        system that wants them: see the module docstring for the measurement.
        """
        data = self._get(
            "/markets",
            {
                "status": status,
                "max_close_ts": max_close_ts,
                "category": category,
                "mve_filter": "exclude",
                "limit": limit,
                "cursor": cursor,
            },
        )
        markets = [Market.from_api(m) for m in data.get("markets", [])]
        return markets, (data.get("cursor") or None)

    def iter_markets(
        self,
        max_close_ts: int | None = None,
        status: str = "open",
        category: str | None = None,
        limit: int = 200,
    ) -> Iterator[Market]:
        """Yield every market across all pages, following the cursor transparently."""
        cursor: str | None = None
        while True:
            markets, cursor = self.get_markets(
                max_close_ts=max_close_ts,
                status=status,
                category=category,
                limit=limit,
                cursor=cursor,
            )
            yield from markets
            if not cursor:
                return

    def get_market(self, ticker: str) -> Market:
        data = self._get(f"/markets/{ticker}")
        market = Market.from_api(data.get("market", data))
        if market.category is None and market.event_ticker:
            category = self.event_category(market.event_ticker, raise_on_error=False)
            if category is not None:
                market.category = category
        return market

    def event_category(self, event_ticker: str, *, raise_on_error: bool = True) -> str | None:
        """Cached category lookup — category lives on the EVENT, not the market (BT-3).

        Public because bulk listings carry no category and the only honest way to filter
        by one is to ask each distinct event once. ``bt markets --category`` used to reach
        for ``get_market`` per listed row instead: a full market GET whose category came
        from *this* lookup anyway, one per row, up to a few hundred serial requests inside
        a session that pays for the wait. One cached event lookup per distinct event does
        the same job, and events are far fewer than markets.

        ``raise_on_error=True`` re-raises :class:`KalshiAPIError`, so a caller filtering a
        board cannot mistake "the lookup failed" for "not this category". ``False`` is the
        enrichment posture ``get_market`` wants: a category is a nice-to-have there, and a
        market should still come back without one. A swallowed failure caches ``None``, so
        a later strict call sees that cached answer rather than re-asking — the cache is
        per-client and per-process, which bounds how long a transient failure lingers.
        """
        if event_ticker in self._event_category_cache:
            return self._event_category_cache[event_ticker]
        try:
            data = self._get(f"/events/{event_ticker}")
        except KalshiAPIError:
            if raise_on_error:
                raise
            self._event_category_cache[event_ticker] = None
            return None
        event = data.get("event", data)
        category = event.get("category")
        self._event_category_cache[event_ticker] = category
        return category

    def get_orderbook(self, ticker: str, depth: int = 5) -> Orderbook:
        data = self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        return Orderbook.from_api(data, ticker=ticker)

    # -- price history --------------------------------------------------------
    def get_candlesticks(
        self,
        ticker: str,
        *,
        period_minutes: int = 60,
        start: datetime | None = None,
        end: datetime | None = None,
        series_ticker: str | None = None,
    ) -> list[Candle]:
        """Market price HISTORY (candlesticks), YES-side, in the server's (ascending) order.

        VERIFIED live read-only 2026-07-13/14 (HTTP 200; zero orders placed):

            GET /series/{series_ticker}/markets/{ticker}/candlesticks
            params: start_ts, end_ts (epoch SECONDS — BOTH REQUIRED; omitting start_ts
            400s "Query argument start_ts is required"), period_interval (MINUTES; 1, 60,
            1440 confirmed). The span is capped at 5000 candles server-side:
            (end_ts - start_ts) / (period_interval * 60) > 5000 -> HTTP 400.

        Each candle carries ``end_period_ts`` plus a ``price`` OHLC sub-object of YES-side
        ``*_dollars`` strings (with ``mean``/``previous``), separate ``yes_bid``/``yes_ask``
        OHLC sub-objects, and ``volume_fp``/``open_interest_fp`` fixed-point counts — see
        ``Candle``.

        ``series_ticker`` is derived when not given: the market payload carries none
        (verified — neither the list nor the single-market GET), so it is read from the
        parent event's ``series_ticker`` and cached (see ``_series_ticker_for``). Defaults:
        ``end`` = now (UTC), ``start`` = ``end`` − 72h. Same GET retry policy as other reads.
        """
        if series_ticker is None:
            series_ticker = self._series_ticker_for(ticker)
        end = end or datetime.now(UTC)
        start = start or end - timedelta(hours=72)
        data = self._get(
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            {
                "start_ts": int(start.timestamp()),
                "end_ts": int(end.timestamp()),
                "period_interval": period_minutes,
            },
        )
        return [Candle.from_api(c) for c in data.get("candlesticks", [])]

    def _series_ticker_for(self, ticker: str) -> str:
        """Resolve a market's ``series_ticker`` (cached), needed for the candlesticks path.

        The market payload carries no ``series_ticker`` (verified live) — it lives on the
        parent event — so this joins market->event: ``GET /markets/{ticker}`` for the
        ``event_ticker``, then ``GET /events/{event_ticker}`` for ``series_ticker`` (the
        authoritative source; a heuristic split of the ticker is NOT reliable). An unknown
        ticker surfaces as the market fetch's ``KalshiAPIError(404)``.
        """
        if ticker in self._series_ticker_cache:
            return self._series_ticker_cache[ticker]
        market = self._get(f"/markets/{ticker}")
        m = market.get("market", market)
        event_ticker = m.get("event_ticker")
        if not event_ticker:
            raise KalshiAPIError(None, f"cannot derive series_ticker for {ticker}: no event_ticker")
        event = self._get(f"/events/{event_ticker}")
        ev = event.get("event", event)
        series = ev.get("series_ticker")
        if not series:
            raise KalshiAPIError(
                None, f"cannot derive series_ticker for {ticker}: event has no series_ticker"
            )
        self._series_ticker_cache[ticker] = series
        return series

    # -- orders ---------------------------------------------------------------
    def create_order(
        self,
        ticker: str,
        side: str,
        price: Decimal,
        count: int,
        client_order_id: str,
        time_in_force: str = "ioc",
        action: str = "buy",
    ) -> OrderResult:
        """Place a limit IOC order. NEVER auto-retries. On timeout/5xx raises
        ``OrderAmbiguous`` (outcome unknown — re-query fills, never blind-resend); a
        non-retryable 4xx raises ``KalshiAPIError`` (a definite rejection — no order).

        Uses the V2 endpoint ``POST /portfolio/events/orders`` (verified against the
        published OpenAPI spec, 2026-08-01). The legacy ``POST /portfolio/orders``
        was sunset (announced "no earlier than 2026-05-06") and returns HTTP 410
        Gone — observed live 2026-07-31 across the canary and three attempts.

        V2 quotes EVERYTHING from the YES leg of the single book (``BookSide``:
        ``bid`` = buy YES, ``ask`` = sell YES). The harness always BUYS ``side``
        exposure, so the mapping is:

            side "yes" -> {"side": "bid", "price": price}
            side "no"  -> {"side": "ask", "price": 1 - price}

        and a NO buy's fill price is converted back as ``1 - average_fill_price``.
        The returned ``fee`` is the exchange's own TOTAL for the order
        (``taker_fees_dollars + maker_fees_dollars``), or ``None`` when the response
        reports neither; it is never derived from ``average_fee_paid``, which is a
        per-contract average the exchange has already rounded (see
        :func:`~betting_agent.kalshi.types.reported_fee`). ``count`` is a fixed-point
        string; ``time_in_force``
        "ioc" maps to the V2 enum value ``immediate_or_cancel``;
        ``self_trade_prevention_type`` is required — ``taker_at_cross`` (we are an
        IOC taker on a dedicated account).

        Three refusals guard the money path. An off-grid ``price`` raises ``ValueError``
        rather than being silently re-priced by quantize (KC-4): this client places the
        price it was asked to place, or none at all. A fractional ``count`` likewise
        raises — the harness only ever buys whole contracts. And an unreadable 2xx body
        raises ``OrderAmbiguous``, never a bare ``JSONDecodeError``: the order landed, so
        the honest state is "outcome unknown, go ask the fills endpoint" (KC-1).
        """
        if action != "buy":
            raise ValueError(f"create_order only buys (the harness never sells): {action!r}")
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        tif = {"ioc": "immediate_or_cancel"}.get(time_in_force, time_in_force)
        if tif not in ("immediate_or_cancel", "fill_or_kill", "good_till_canceled"):
            raise ValueError(f"unsupported time_in_force: {time_in_force!r}")
        p = Decimal(str(price))
        if p != p.quantize(_Q4):
            raise ValueError(f"price finer than the 4dp grid, refusing to re-price: {price!r}")
        p = p.quantize(_Q4)  # canonical form for the wire string
        c = Decimal(str(count))
        if c != c.to_integral_value():
            raise ValueError(f"count must be whole contracts, got {count!r}")
        wire_price = p if side == "yes" else (Decimal(1) - p).quantize(_Q4)
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": "bid" if side == "yes" else "ask",
            "count": str(int(c)),
            "price": str(wire_price),
            "client_order_id": client_order_id,
            "time_in_force": tif,
            "self_trade_prevention_type": "taker_at_cross",
        }
        endpoint = "/portfolio/events/orders"
        headers = self._headers("POST", endpoint)
        try:
            r = self._client.post(self._base_url + endpoint, json=body, headers=headers)
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            raise OrderAmbiguous(client_order_id, f"transport/timeout: {exc}") from exc
        if r.status_code >= 500:
            raise OrderAmbiguous(client_order_id, f"HTTP {r.status_code}")
        if r.status_code not in (200, 201):
            raise KalshiAPIError(r.status_code, r.reason_phrase, r.text)
        # Parsed exactly once (EF-5). The order already landed, so an unreadable body is
        # ambiguity, not a crash (KC-1).
        try:
            payload = r.json()
        except ValueError as exc:
            raise OrderAmbiguous(client_order_id, f"unparseable 2xx body: {exc}") from exc
        d = payload if isinstance(payload, dict) else {}
        filled = parse_count(d, "fill_count") or Decimal(0)
        if filled != filled.to_integral_value():
            # Fractional trading is live; a fractional fill is real money we must not
            # report as a clean int (KC-2). Hand it to the fills join, which is exact.
            raise OrderAmbiguous(client_order_id, f"fractional_fill: fill_count={filled}")
        avg = None
        if filled > 0 and d.get("average_fill_price") is not None:
            avg_yes = Decimal(str(d["average_fill_price"]))
            avg = ((Decimal(1) - avg_yes) if side == "no" else avg_yes).quantize(_Q4)
        # KC-6, closed by docs/25. This used to return ``average_fee_paid x filled``, and
        # the hazard the old comment called "moot at today's 1-contract sizing" arrived the
        # day sizing did: the exchange rounds that average per contract, so on three
        # 3-contract legs the product came to $0.0099 against a charged $0.0100 and the
        # nightly walk was $0.0003 out. The reported TOTAL is a receipt and is what lands
        # on the row; ``None`` when the response carries none, which leaves the caller's
        # own ceiling-on-the-leg-total model to stand in (it reproduces every fee this
        # exchange has ever reported to us).
        fee_total = reported_fee(d) if filled > 0 else None
        return OrderResult(
            order_id=d.get("order_id"),
            client_order_id=d.get("client_order_id") or client_order_id,
            # V2 returns no status field; for an IOC the unfilled remainder is
            # canceled by construction.
            status="executed" if filled > 0 else "canceled",
            filled_count=filled,
            avg_fill_price=avg,
            fee=fee_total,
            raw=d,
        )

    def get_orders(
        self, min_ts: datetime | None = None, cursor: str | None = None
    ) -> tuple[list[dict], str | None]:
        """One page of raw order dicts from ``/portfolio/orders`` (shape per the
        recorded fixture: ``order_id``, ``client_order_id``, ``ticker``, ``side``,
        ``status``, ``created_time``, ``*_fp`` counts, ``*_dollars`` prices).

        The server IGNORES its query filters (verified live), so ``min_ts`` is applied
        CLIENT-side on the order's ``created_time``; it is still forwarded as an epoch
        param (harmless today, a bandwidth win if Kalshi ever honors it). An order
        missing/unparseable ``created_time`` is RETAINED under a ``min_ts`` filter —
        the §10 impostor tripwire must never silently drop account activity. A page
        may filter to empty while ``next_cursor`` is still set; keep paging.
        """
        data = self._get(
            "/portfolio/orders",
            {"min_ts": _to_epoch_s(min_ts), "cursor": cursor, "limit": 200},
        )
        orders = data.get("orders", [])
        if min_ts is not None:
            orders = [
                o for o in orders
                if (created := order_created_at(o)) is None or created >= min_ts
            ]
        return orders, (data.get("cursor") or None)

    def iter_orders(self, min_ts: datetime | None = None) -> Iterator[dict]:
        """Yield every order (optionally ``created_time >= min_ts``) across all pages."""
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
        data = self._get(
            "/portfolio/fills",
            {"min_ts": _to_epoch_s(min_ts), "cursor": cursor, "limit": 200},
        )
        fills = [Fill.from_api(f) for f in data.get("fills", [])]
        return fills, (data.get("cursor") or None)

    def get_settlements(
        self, min_ts: datetime | None = None, cursor: str | None = None
    ) -> tuple[list[Settlement], str | None]:
        data = self._get(
            "/portfolio/settlements",
            {"min_ts": _to_epoch_s(min_ts), "cursor": cursor, "limit": 200},
        )
        settlements = [Settlement.from_api(s) for s in data.get("settlements", [])]
        return settlements, (data.get("cursor") or None)

    def find_fills_by_client_order_id(self, client_order_id: str) -> list[Fill]:
        """Resolve a client_order_id to its fills via the orders->fills join.

        Fills lack client_order_id, so we page ``/portfolio/orders`` and match it
        client-side (the ``client_order_id`` query filter is ignored server-side), collect
        matching ``order_id``s, then fetch ``/portfolio/fills?order_id=...`` for each and
        stamp our ``client_order_id`` onto the returned fills.

        client_order_id is a unique idempotency key, so we stop at the first match (the
        just-placed order is normally on page 1); a miss pages to exhaustion.

        An empty result means "not visible to this scan" — NOT "never landed". Fill and
        order visibility lags writes, and the lag correlates with exactly the 5xx/timeout
        conditions that make an order ambiguous in the first place (KC-3/AE-3). Callers
        concluding absence must re-scan after a delay; the executor does.
        """
        order_ids: list[str] = []
        for o in self.iter_orders():
            if o.get("client_order_id") == client_order_id and o.get("order_id"):
                order_ids.append(o["order_id"])
                break
        out: list[Fill] = []
        for oid in order_ids:
            cursor: str | None = None
            while True:
                data = self._get(
                    "/portfolio/fills",
                    {"order_id": oid, "cursor": cursor, "limit": 200},
                )
                for f in data.get("fills", []):
                    out.append(Fill.from_api(f, client_order_id=client_order_id))
                cursor = data.get("cursor") or None
                if not cursor:
                    break
        return out


def order_created_at(order: dict) -> datetime | None:
    """The order's creation timestamp (``created_time`` per live payloads), or ``None``.

    Shared by the client's client-side ``min_ts`` filter, ``FakeKalshi``, and the
    settlement scan's audit details.
    """
    return parse_ts(order.get("created_time"))


def _to_epoch_s(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.timestamp())
