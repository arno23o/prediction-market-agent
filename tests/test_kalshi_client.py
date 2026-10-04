"""Client tests over httpx.MockTransport: auth/signature, pagination, GET retry,
create_order non-retry + OrderAmbiguous, dollar-string parsing, and query-string
exclusion from the signed path."""
import base64
import json
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import betting_agent.kalshi.client as client_mod
from betting_agent.kalshi.client import KalshiAPIError, KalshiClient, OrderAmbiguous

BASE = "https://api.elections.kalshi.com/trade-api/v2"
_PSS = padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32)


@pytest.fixture(scope="module")
def keypair(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path_factory.mktemp("keys") / "key.pem"
    path.write_bytes(pem)
    return key.public_key(), str(path)


def make_client(handler, key_path, throttle_rps=1000.0):
    return KalshiClient(
        BASE, "test-key-id", key_path,
        transport=httpx.MockTransport(handler),
        throttle_rps=throttle_rps,
    )


def verify_sig(request, public_key):
    """Verify the signature over ``ts + METHOD + path`` — path is request.url.path,
    which EXCLUDES the query string, so this only passes if the client signed path-only."""
    ts = request.headers["KALSHI-ACCESS-TIMESTAMP"]
    sig = base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"])
    message = f"{ts}{request.method}{request.url.path}"
    public_key.verify(sig, message.encode("utf-8"), _PSS, hashes.SHA256())


def test_auth_headers_present_and_signature_verifiable(keypair):
    pub, key_path = keypair
    seen = {}

    def handler(request):
        assert request.headers["KALSHI-ACCESS-KEY"] == "test-key-id"
        assert request.headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()
        verify_sig(request, pub)  # raises if signature/recipe/path is wrong
        seen["path"] = request.url.path
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

    status = make_client(handler, key_path).get_exchange_status()
    assert status.active is True
    assert seen["path"] == "/trade-api/v2/exchange/status"


def test_pagination_across_two_pages(keypair):
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        cursor = request.url.params.get("cursor")
        if not cursor:
            return httpx.Response(200, json={"markets": [{"ticker": "A"}], "cursor": "CURSOR2"})
        assert cursor == "CURSOR2"
        return httpx.Response(200, json={"markets": [{"ticker": "B"}], "cursor": ""})

    client = make_client(handler, key_path)
    tickers = [m.ticker for m in client.iter_markets(max_close_ts=123)]
    assert tickers == ["A", "B"]

    first_page, cur = client.get_markets(max_close_ts=123)
    assert [m.ticker for m in first_page] == ["A"] and cur == "CURSOR2"


def test_get_retries_on_500_then_succeeds(keypair, monkeypatch):
    pub, key_path = keypair
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)  # no real backoff wait
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="server boom")
        return httpx.Response(200, json={"balance_dollars": "10.0000"})

    bal = make_client(handler, key_path).get_balance()
    assert bal.dollars == Decimal("10.0000")
    assert calls["n"] == 2  # one failure, one success


def test_get_retries_exhaust_then_raise(keypair, monkeypatch):
    pub, key_path = keypair
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="always down")

    with pytest.raises(KalshiAPIError) as ei:
        make_client(handler, key_path).get_exchange_status()
    assert ei.value.status == 503
    assert calls["n"] == 4  # initial + 3 retries


def test_get_retries_on_transport_error(keypair, monkeypatch):
    pub, key_path = keypair
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

    assert make_client(handler, key_path).get_exchange_status().active is True
    assert calls["n"] == 3


def test_a_429_waits_the_servers_retry_after_instead_of_our_ladder(keypair, monkeypatch):
    """A 429 carrying ``Retry-After`` is the exchange stating the rate it will accept.
    Coming back on our own 0.5 s guess is how a throttle becomes a block, and the board
    refresh is the heaviest reader on this account."""
    pub, key_path = keypair
    waits: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", waits.append)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, text="slow down", headers={"Retry-After": "3"})
        return httpx.Response(200, json={"exchange_active": True, "trading_active": True})

    assert make_client(handler, key_path).get_exchange_status().active is True
    assert [w for w in waits if w >= 0.01] == [3.0]     # not the ladder's 0.5


def test_a_5xx_without_the_header_keeps_the_jittered_ladder(keypair, monkeypatch):
    pub, key_path = keypair
    waits: list[float] = []
    monkeypatch.setattr(client_mod.time, "sleep", waits.append)

    def handler(request):
        return httpx.Response(503, text="always down")

    with pytest.raises(KalshiAPIError):
        make_client(handler, key_path).get_exchange_status()

    steps = [w for w in waits if w >= 0.01]
    assert all(base <= w < base * 1.5
               for base, w in zip((0.5, 2.0, 8.0), steps, strict=True))


def test_the_backoff_carries_jitter_so_retries_do_not_return_in_lockstep():
    """Every session and the board child retry on the same ladder against one account."""
    waits = {client_mod._retry_wait(2.0, None) for _ in range(200)}
    assert min(waits) >= 2.0 and max(waits) < 3.0
    assert len(waits) > 1                              # jittered, not a constant


def test_an_unreadable_retry_after_falls_back_and_an_absurd_one_is_capped():
    """Only the numeric form is read (the form Kalshi sends); a date falls back to the
    ladder, and no header may park a pull for longer than a minute."""
    assert 0.5 <= client_mod._retry_wait(0.5, "Wed, 21 Oct 2026 07:28:00 GMT") < 0.75
    assert client_mod._retry_wait(0.5, "99999") == 60.0
    assert client_mod._retry_wait(0.5, " 4 ") == 4.0
    assert client_mod._retry_wait(0.5, "-7") == 0.0


def test_create_order_does_not_retry_and_raises_ambiguous_on_timeout(keypair):
    pub, key_path = keypair
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        raise httpx.ReadTimeout("timeout", request=request)

    client = make_client(handler, key_path)
    with pytest.raises(OrderAmbiguous) as ei:
        client.create_order("KXA", "yes", Decimal("0.42"), 2, "A-0001-B01")
    assert ei.value.client_order_id == "A-0001-B01"
    assert calls["n"] == 1  # NEVER auto-retries


def test_create_order_5xx_raises_ambiguous(keypair):
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(OrderAmbiguous):
        make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 1, "A-0001-B02")


def test_create_order_4xx_is_definite_error_not_ambiguous(keypair):
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(400, text="bad request")

    with pytest.raises(KalshiAPIError) as ei:
        make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 1, "A-0001-B03")
    assert ei.value.status == 400


def test_create_order_v2_yes_buy_sends_bid_and_parses_result(keypair):
    """V2 wire shape (POST /portfolio/events/orders, OpenAPI spec 2026-08-01): buy YES
    -> side "bid" at the YES price; fixed-point string count; V2 tif enum; required
    self_trade_prevention_type; flat (unwrapped) response carrying both the exchange's
    total fee and its per-contract average."""
    pub, key_path = keypair
    captured = {}

    def handler(request):
        verify_sig(request, pub)
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "order_id": "OID-1", "client_order_id": "A-0001-B01",
            "fill_count": "2.00", "remaining_count": "0.00",
            "average_fill_price": "0.4200", "average_fee_paid": "0.0200",
            "taker_fees_dollars": "0.0342", "maker_fees_dollars": "0.0000",
            "ts_ms": 1785476472000,
        })

    r = make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 2, "A-0001-B01")
    assert captured["path"] == "/trade-api/v2/portfolio/events/orders"
    body = captured["body"]
    assert body["side"] == "bid" and body["price"] == "0.4200"
    assert body["count"] == "2"  # fixed-point count string
    assert body["time_in_force"] == "immediate_or_cancel"  # "ioc" mapped to the V2 enum
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    assert body["client_order_id"] == "A-0001-B01"
    assert "action" not in body and "type" not in body  # legacy fields must not leak
    assert r.filled_count == 2 and r.status == "executed"
    assert r.avg_fill_price == Decimal("0.4200")
    assert r.fee == Decimal("0.0342")  # the reported TOTAL, not 0.02 x 2


def test_create_order_v2_no_buy_inverts_to_ask_and_back(keypair):
    """Buy NO at q == ask at 1-q on the single YES-quoted book; the fill price is
    converted back to NO terms (1 - average_fill_price)."""
    pub, key_path = keypair
    captured = {}

    def handler(request):
        verify_sig(request, pub)
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={
            "order_id": "OID-2", "client_order_id": "A-0001-B02",
            "fill_count": "1.00", "remaining_count": "0.00",
            "average_fill_price": "0.9300", "average_fee_paid": "0.0100",
            "taker_fees_dollars": "0.0046",
            "ts_ms": 1785476473000,
        })

    r = make_client(handler, key_path).create_order("KXA", "no", Decimal("0.07"), 1, "A-0001-B02")
    body = captured["body"]
    assert body["side"] == "ask" and body["price"] == "0.9300"
    assert r.filled_count == 1
    assert r.avg_fill_price == Decimal("0.0700")  # back in NO terms
    assert r.fee == Decimal("0.0046")               # the reported total, taker only


def test_create_order_v2_ioc_miss_is_canceled_no_fill(keypair):
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(201, json={
            "order_id": "OID-3", "client_order_id": "A-0001-B03",
            "fill_count": "0.00", "remaining_count": "0.00", "ts_ms": 1785476474000,
        })

    r = make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 1, "A-0001-B03")
    assert r.filled_count == 0 and r.status == "canceled"
    assert r.avg_fill_price is None and r.fee is None


def test_api_error_message_carries_the_response_body(keypair):
    """The 2026-07-31 410 surfaced as a bare "Gone" — the body must ride the message."""
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(
            410, text='{"error":"endpoint retired, use /portfolio/events/orders"}'
        )

    with pytest.raises(KalshiAPIError) as ei:
        make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 1, "A-0001-B04")
    assert ei.value.status == 410
    assert "endpoint retired" in str(ei.value)


def test_fixed_point_dollar_string_parsing(keypair):
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(200, json={"markets": [{
            "ticker": "KXA", "yes_ask_dollars": "0.4200", "yes_bid_dollars": "0.4100",
            "no_ask_dollars": "0.5900", "volume_fp": "1234.00", "open_interest_fp": "56.78",
            "close_time": "2026-07-08T12:00:00Z",
        }], "cursor": ""})

    markets, _ = make_client(handler, key_path).get_markets(max_close_ts=123)
    m = markets[0]
    assert m.yes_ask == Decimal("0.4200") and isinstance(m.yes_ask, Decimal)
    assert m.yes_bid == Decimal("0.4100") and m.no_ask == Decimal("0.5900")
    assert m.volume == 1234 and m.open_interest == 56  # *_fp truncated to int
    assert m.close_time.tzinfo is not None


def test_query_string_excluded_from_signed_path(keypair):
    pub, key_path = keypair
    seen = {}

    def handler(request):
        # verify_sig signs over request.url.path (no query). If the client had included
        # the query in the signature, this verify would FAIL.
        verify_sig(request, pub)
        seen["query"] = str(request.url.query)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    make_client(handler, key_path).get_markets(max_close_ts=999, status="open", limit=50)
    assert "max_close_ts=999" in seen["query"]  # a query string really was sent
    assert "?" not in seen["path"] and seen["path"] == "/trade-api/v2/markets"


def test_every_markets_call_asks_the_server_to_leave_the_parlays_out(keypair):
    """docs/25. Parlays are about 2.5 million of the roughly 2.6 million rows this
    endpoint will serve, so filtering them client-side meant paging all of them: a board
    refresh took 2 hours 40 minutes against a 150-minute cadence, which is a pull still
    running when the next one is due. ``mve_filter=exclude`` is server-side and is sent on
    every call, because no caller in this system wants them."""
    pub, key_path = keypair
    seen = []

    def handler(request):
        verify_sig(request, pub)
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    client = make_client(handler, key_path)
    client.get_markets()
    client.get_markets(max_close_ts=999, status="settled", limit=50)
    list(client.iter_markets(max_close_ts=999))

    assert seen and all(p.get("mve_filter") == "exclude" for p in seen)


def test_find_fills_by_client_order_id_joins_orders_then_fills(keypair):
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        path = request.url.path
        if path.endswith("/portfolio/orders"):
            # client_order_id filter is ignored server-side: return all, client matches
            return httpx.Response(200, json={"orders": [
                {"order_id": "O1", "client_order_id": "A-0001-B01"},
                {"order_id": "O2", "client_order_id": "A-0001-B02"},
            ], "cursor": ""})
        if path.endswith("/portfolio/fills"):
            if request.url.params.get("order_id") == "O1":
                return httpx.Response(200, json={"fills": [{
                    "ticker": "KXA", "side": "yes", "count_fp": "2.00",
                    "yes_price_dollars": "0.4200", "created_time": "2026-07-06T15:30:00Z",
                    "is_taker": True,
                }], "cursor": ""})
            return httpx.Response(200, json={"fills": [], "cursor": ""})
        return httpx.Response(404, json={})

    fills = make_client(handler, key_path).find_fills_by_client_order_id("A-0001-B01")
    assert len(fills) == 1
    assert fills[0].client_order_id == "A-0001-B01"  # stamped from the join
    assert fills[0].price == Decimal("0.4200") and fills[0].ticker == "KXA"


def test_find_fills_returns_empty_when_order_never_landed(keypair):
    pub, key_path = keypair
    pages = {"orders": 0}

    def handler(request):
        verify_sig(request, pub)
        if request.url.path.endswith("/portfolio/orders"):
            # two pages, neither carrying our client_order_id -> proves absence
            if not request.url.params.get("cursor"):
                pages["orders"] += 1
                return httpx.Response(200, json={
                    "orders": [{"order_id": "O9", "client_order_id": "A-0002-B01"}],
                    "cursor": "P2",
                })
            pages["orders"] += 1
            return httpx.Response(200, json={"orders": [], "cursor": ""})
        return httpx.Response(200, json={"fills": [], "cursor": ""})

    fills = make_client(handler, key_path).find_fills_by_client_order_id("A-0001-B01")
    assert fills == []  # ambiguity path can safely conclude "no fill"
    assert pages["orders"] == 2  # paged to exhaustion to prove the order is absent


def test_get_market_enriches_category_from_event(keypair):
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        if "/events/" in request.url.path:
            return httpx.Response(200, json={"event": {"category": "Elections"}})
        return httpx.Response(200, json={"market": {
            "ticker": "KXA", "event_ticker": "KXEVT-1", "yes_ask_dollars": "0.4200",
        }})

    m = make_client(handler, key_path).get_market("KXA")
    assert m.category == "Elections"  # markets carry no category; joined from the event
    assert m.yes_ask == Decimal("0.4200")


def test_get_orders_filters_min_ts_client_side(keypair):
    """The server ignores its filters (verified live), so min_ts must be applied
    client-side on created_time; an order missing created_time is retained."""
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        return httpx.Response(200, json={"orders": [
            {"order_id": "O-OLD", "client_order_id": "A-0001-B01",
             "created_time": "2026-07-01T00:00:00Z"},
            {"order_id": "O-NEW", "client_order_id": "A-0002-B01",
             "created_time": "2026-07-06T15:30:00Z"},
            {"order_id": "O-NOTS"},  # no created_time -> never silently dropped
        ], "cursor": ""})

    from datetime import UTC, datetime
    min_ts = datetime(2026, 7, 5, tzinfo=UTC)
    orders, cur = make_client(handler, key_path).get_orders(min_ts=min_ts)
    assert [o["order_id"] for o in orders] == ["O-NEW", "O-NOTS"]
    assert cur is None


def test_get_orders_no_filter_returns_all(keypair):
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        return httpx.Response(200, json={"orders": [
            {"order_id": "O1", "created_time": "2026-07-01T00:00:00Z"},
        ], "cursor": "NEXT"})

    orders, cur = make_client(handler, key_path).get_orders()
    assert [o["order_id"] for o in orders] == ["O1"]
    assert cur == "NEXT"


def test_iter_orders_pages_and_filters_across_pages(keypair):
    """A page that filters to empty must not stop iteration — the cursor drives it."""
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        cursor = request.url.params.get("cursor")
        if not cursor:
            return httpx.Response(200, json={"orders": [
                {"order_id": "P1-OLD", "created_time": "2026-07-01T00:00:00Z"},
            ], "cursor": "PAGE2"})
        assert cursor == "PAGE2"
        return httpx.Response(200, json={"orders": [
            {"order_id": "P2-NEW", "created_time": "2026-07-06T12:00:00Z"},
        ], "cursor": ""})

    from datetime import UTC, datetime
    min_ts = datetime(2026, 7, 5, tzinfo=UTC)
    ids = [o["order_id"] for o in make_client(handler, key_path).iter_orders(min_ts=min_ts)]
    assert ids == ["P2-NEW"]  # page 1 filtered empty, cursor still followed


def test_order_created_at_parses_fixture_field():
    from datetime import UTC, datetime

    from betting_agent.kalshi.client import order_created_at

    assert order_created_at(
        {"created_time": "2026-07-06T15:30:00Z"}
    ) == datetime(2026, 7, 6, 15, 30, tzinfo=UTC)
    assert order_created_at({}) is None


# --------------------------------------------------------------------------- candlesticks
_CANDLE = {
    "end_period_ts": 1783753200,
    "open_interest_fp": "731794.45",
    "price": {"open_dollars": "0.9130", "high_dollars": "0.9140", "low_dollars": "0.9120",
              "close_dollars": "0.9135", "mean_dollars": "0.9130", "previous_dollars": "0.9130"},
    "volume_fp": "3847.98",
    "yes_ask": {"close_dollars": "0.9140"},
    "yes_bid": {"close_dollars": "0.9130"},
}


def test_get_candlesticks_derives_series_signs_path_and_parses(keypair):
    """Series is derived market->event, the candlesticks path is signed WITHOUT the query,
    the required params are sent, and the payload parses to Decimal OHLC + int volume/OI."""
    pub, key_path = keypair
    seen = {}

    def handler(request):
        verify_sig(request, pub)  # signs over path only; query excluded
        path = request.url.path
        if path.endswith("/markets/KXA"):
            return httpx.Response(200, json={"market": {
                "ticker": "KXA", "event_ticker": "KXEVT-1",
            }})
        if "/events/" in path:
            assert path.endswith("/events/KXEVT-1")
            return httpx.Response(200, json={"event": {"series_ticker": "KXSER"}})
        if path.endswith("/candlesticks"):
            seen["path"] = path
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"ticker": "KXA", "candlesticks": [_CANDLE]})
        return httpx.Response(404, json={})

    candles = make_client(handler, key_path).get_candlesticks("KXA", period_minutes=60)
    # path is /series/{series}/markets/{ticker}/candlesticks, query excluded from it
    assert seen["path"] == "/trade-api/v2/series/KXSER/markets/KXA/candlesticks"
    assert "?" not in seen["path"]
    p = seen["params"]
    assert p["period_interval"] == "60"
    assert p["start_ts"].isdigit() and p["end_ts"].isdigit()
    assert int(p["start_ts"]) < int(p["end_ts"])  # start before end
    # default window is 72h
    assert int(p["end_ts"]) - int(p["start_ts"]) == 72 * 3600
    assert len(candles) == 1
    c = candles[0]
    assert c.open == Decimal("0.9130") and isinstance(c.open, Decimal)
    assert c.high == Decimal("0.9140") and c.low == Decimal("0.9120")
    assert c.close == Decimal("0.9135")
    assert c.volume == 3847 and c.open_interest == 731794  # *_fp truncated to int
    assert c.ts is not None and c.ts.tzinfo is not None
    assert c.raw["yes_ask"]["close_dollars"] == "0.9140"  # full payload preserved


def test_get_candlesticks_explicit_series_skips_derivation(keypair):
    """Passing series_ticker skips the market+event lookups: only candlesticks is called."""
    pub, key_path = keypair
    calls = []

    def handler(request):
        verify_sig(request, pub)
        calls.append(request.url.path)
        return httpx.Response(200, json={"ticker": "KXA", "candlesticks": [_CANDLE]})

    from datetime import UTC, datetime

    end = datetime(2026, 7, 10, tzinfo=UTC)
    start = datetime(2026, 7, 9, tzinfo=UTC)
    candles = make_client(handler, key_path).get_candlesticks(
        "KXA", period_minutes=1440, start=start, end=end, series_ticker="KXSER"
    )
    assert calls == ["/trade-api/v2/series/KXSER/markets/KXA/candlesticks"]  # no /markets, /events
    assert len(candles) == 1


def test_get_candlesticks_explicit_window_sends_epoch_seconds(keypair):
    pub, key_path = keypair
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"ticker": "KXA", "candlesticks": []})

    from datetime import UTC, datetime

    start = datetime(2026, 7, 9, tzinfo=UTC)
    end = datetime(2026, 7, 10, tzinfo=UTC)
    make_client(handler, key_path).get_candlesticks(
        "KXA", period_minutes=60, start=start, end=end, series_ticker="KXSER"
    )
    assert seen["params"]["start_ts"] == str(int(start.timestamp()))
    assert seen["params"]["end_ts"] == str(int(end.timestamp()))


def test_get_candlesticks_unknown_ticker_404s_at_series_derivation(keypair):
    pub, key_path = keypair

    def handler(request):
        if request.url.path.endswith("/markets/NOPE"):
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json={})

    with pytest.raises(KalshiAPIError) as ei:
        make_client(handler, key_path).get_candlesticks("NOPE")
    assert ei.value.status == 404


def test_series_ticker_derivation_is_cached(keypair):
    """A second candlesticks call reuses the cached series (no repeat market/event fetch)."""
    pub, key_path = keypair
    counts = {"markets": 0, "events": 0, "candles": 0}

    def handler(request):
        path = request.url.path
        if path.endswith("/markets/KXA"):
            counts["markets"] += 1
            return httpx.Response(200, json={"market": {"ticker": "KXA",
                                                        "event_ticker": "KXEVT-1"}})
        if "/events/" in path:
            counts["events"] += 1
            return httpx.Response(200, json={"event": {"series_ticker": "KXSER"}})
        counts["candles"] += 1
        return httpx.Response(200, json={"ticker": "KXA", "candlesticks": []})

    client = make_client(handler, key_path)
    client.get_candlesticks("KXA")
    client.get_candlesticks("KXA")
    assert counts == {"markets": 1, "events": 1, "candles": 2}  # derivation cached


# --------------------------------------------------------- order-path money integrity
def test_create_order_malformed_2xx_body_is_ambiguous_not_a_crash(keypair):
    """KC-1: the order LANDED; an unreadable receipt is ambiguity, not a JSONDecodeError
    escaping the declared taxonomy (which would abandon the ledger row of a real fill)."""
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(201, text="<html>gateway ate it</html>")

    with pytest.raises(OrderAmbiguous) as ei:
        make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 1, "A-0001-B01")
    assert ei.value.client_order_id == "A-0001-B01"
    assert "unparseable 2xx body" in str(ei.value)


def test_create_order_fractional_fill_count_is_ambiguous(keypair):
    """KC-2: a 0.90-contract fill must never be reported as a clean int (0 = money gone,
    fee dropped). It goes to the fills join, which is exact."""
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(201, json={
            "order_id": "OID-9", "client_order_id": "A-0001-B01",
            "fill_count": "0.90", "average_fill_price": "0.4200",
            "average_fee_paid": "0.0100",
        })

    with pytest.raises(OrderAmbiguous) as ei:
        make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 1, "A-0001-B01")
    assert "fractional_fill" in str(ei.value)


def test_create_order_prefers_the_fixed_point_fill_count(keypair):
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(201, json={
            "order_id": "OID-10", "client_order_id": "A-0001-B01",
            "fill_count_fp": "2.00", "fill_count": "2",
            "average_fill_price": "0.4200", "taker_fees_dollars": "0.0342",
        })

    r = make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 2, "A-0001-B01")
    assert r.filled_count == Decimal("2.00") and isinstance(r.filled_count, Decimal)
    assert r.fee == Decimal("0.0342")


def test_create_order_refuses_an_off_grid_price(keypair):
    """KC-4: quantize would silently re-price 0.42005 to 0.4200 (banker's rounding).
    This client places the price it was handed, or nothing at all — and sends no HTTP."""
    pub, key_path = keypair
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(201, json={"fill_count": "0.00"})

    client = make_client(handler, key_path)
    with pytest.raises(ValueError, match="4dp grid"):
        client.create_order("KXA", "yes", Decimal("0.42005"), 1, "A-0001-B01")
    with pytest.raises(ValueError, match="whole contracts"):
        client.create_order("KXA", "yes", Decimal("0.4200"), Decimal("1.5"), "A-0001-B02")
    assert calls["n"] == 0  # neither refusal reached the exchange


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"action": "sell"}, "only buys"),
        ({"action": "SELL"}, "only buys"),
        ({"side": "bid"}, "side must be"),       # V2's WIRE vocabulary, not this surface
        ({"side": "ask"}, "side must be"),
        ({"side": "YES"}, "side must be"),
        ({"time_in_force": "gtc"}, "unsupported time_in_force"),
        ({"time_in_force": ""}, "unsupported time_in_force"),
        ({"price": Decimal("0.42005")}, "4dp grid"),
        ({"count": Decimal("1.5")}, "whole contracts"),
    ],
)
def test_create_order_guard_matrix_refuses_locally_without_sending(keypair, kwargs, match):
    """KC-7: every refusal is local — the guard matrix must cost zero HTTP calls, because
    a request that reaches the exchange can fill, and this client has no way to un-fill it.
    ``side='bid'``/``'ask'`` is the sharp one: those are V2's wire values for the single
    YES-quoted book, and accepting them here would route NO exposure onto the YES leg."""
    pub, key_path = keypair
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(201, json={"fill_count": "1.00"})

    args = {"ticker": "KXA", "side": "yes", "price": Decimal("0.4200"),
            "count": 1, "client_order_id": "A-0001-B01", **kwargs}
    with pytest.raises(ValueError, match=match):
        make_client(handler, key_path).create_order(**args)
    assert calls["n"] == 0


@pytest.mark.parametrize("tif", ["ioc", "fill_or_kill", "good_till_canceled"])
def test_create_order_accepts_the_supported_tifs_and_maps_ioc(keypair, tif):
    pub, key_path = keypair
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(201, json={"fill_count": "0.00"})

    make_client(handler, key_path).create_order(
        "KXA", "yes", Decimal("0.4200"), 1, "A-0001-B01", time_in_force=tif
    )
    assert captured["body"]["time_in_force"] == (
        "immediate_or_cancel" if tif == "ioc" else tif
    )


def test_create_order_fee_is_the_reported_total_never_the_per_contract_average(keypair):
    """KC-6, closed by docs/25. ``average_fee_paid`` is PER CONTRACT and already rounded
    by the exchange, so multiplying it by the fill count is a reconstruction that is lossy
    in the last hundredth of a cent. Pinned on the live 2026-09-17 leg: 3 contracts at
    $0.18, charged $0.0310, where the product of the reported average is $0.0309. Three
    legs like it put the first nightly walk under the new regime $0.0003 out.

    The reported total wins whenever it is there, and the average is never multiplied.
    """
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(201, json={
            "order_id": "OID-11", "client_order_id": "A-0001-B01",
            "fill_count": "3.00", "average_fill_price": "0.1800",
            "average_fee_paid": "0.0103", "taker_fees_dollars": "0.0310",
        })

    r = make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.18"), 3,
                                                    "A-0001-B01")
    assert r.filled_count == Decimal("3.00")
    assert r.fee == Decimal("0.0310")           # the receipt…
    assert r.fee != Decimal("0.0103") * 3       # …and not the product of the average


def test_create_order_reports_no_fee_when_the_exchange_reports_no_total(keypair):
    """With only a per-contract average on the payload there is no total to store, and a
    guess is worse than none: ``None`` leaves the caller's ceiling-on-the-leg-total model
    to stand in, which reproduces every fee this exchange has reported to us."""
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(201, json={
            "order_id": "OID-12", "client_order_id": "A-0001-B01",
            "fill_count": "3.00", "average_fill_price": "0.1800",
            "average_fee_paid": "0.0103",
        })

    r = make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.18"), 3,
                                                    "A-0001-B01")
    assert r.filled_count == Decimal("3.00") and r.fee is None


def test_create_order_fill_without_fee_or_price_fields_reports_none(keypair):
    """A filled order whose receipt names no fee/price: both stay ``None`` rather than
    becoming a fabricated zero. The executor's fee fallback (``fee is None`` and
    ``filled > 0`` -> the §8 model estimate) exists precisely for this receipt."""
    pub, key_path = keypair

    def handler(request):
        return httpx.Response(201, json={
            "order_id": "OID-12", "client_order_id": "A-0001-B01", "fill_count": "1.00",
        })

    r = make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 1,
                                                    "A-0001-B01")
    assert r.filled_count == 1 and r.status == "executed"
    assert r.fee is None and r.avg_fill_price is None


def test_create_order_5xx_makes_exactly_one_http_call(keypair, monkeypatch):
    """KC-7 double-submit guard: the ambiguity path must not resend, and the GET retry
    ladder must not apply to POST /orders. One call, always."""
    pub, key_path = keypair
    monkeypatch.setattr(client_mod.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    with pytest.raises(OrderAmbiguous):
        make_client(handler, key_path).create_order("KXA", "yes", Decimal("0.42"), 1, "A-0001-B01")
    assert calls["n"] == 1


# ------------------------------------------------ BT-3: the public event_category helper
def test_event_category_is_public_cached_and_costs_one_request_per_event(keypair):
    """The bulk listing carries no category, so filtering a board by one has to ask the
    EVENT. Asking once per event (cached) is the whole difference between that and the
    per-market GET fan-out it replaces."""
    pub, key_path = keypair
    calls = []

    def handler(request):
        verify_sig(request, pub)
        calls.append(request.url.path)
        return httpx.Response(200, json={"event": {"category": "Elections"}})

    c = make_client(handler, key_path)
    assert c.event_category("KXEVT-1") == "Elections"
    assert c.event_category("KXEVT-1") == "Elections"
    assert c.event_category("KXEVT-2") == "Elections"

    assert calls == ["/trade-api/v2/events/KXEVT-1", "/trade-api/v2/events/KXEVT-2"]


def test_event_category_raises_by_default_so_a_failure_is_never_read_as_no_match(keypair):
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        return httpx.Response(404, text="no such event")

    with pytest.raises(KalshiAPIError):
        make_client(handler, key_path).event_category("KXEVT-1")


def test_event_category_can_be_asked_to_swallow_for_enrichment(keypair):
    """``get_market`` wants a category if there is one and a market either way."""
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        return httpx.Response(404, text="no such event")

    c = make_client(handler, key_path)
    assert c.event_category("KXEVT-1", raise_on_error=False) is None
    assert c.event_category("KXEVT-1", raise_on_error=False) is None  # cached, no re-ask


def test_get_market_enrichment_still_survives_a_broken_event_lookup(keypair):
    pub, key_path = keypair

    def handler(request):
        verify_sig(request, pub)
        if "/events/" in request.url.path:
            return httpx.Response(404, text="no such event")  # non-retryable: no backoff
        return httpx.Response(200, json={"market": {
            "ticker": "KXA", "event_ticker": "KXEVT-1", "yes_ask_dollars": "0.4200",
        }})

    m = make_client(handler, key_path).get_market("KXA")
    assert m.ticker == "KXA" and m.category is None  # a category is a nice-to-have there
