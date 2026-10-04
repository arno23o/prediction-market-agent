"""Live read-only PRODUCTION integration tests (never places orders).

Opt-in only: marked ``prod_ro`` and auto-skipped unless ``RUN_PROD_RO=1``. Reads
credentials from the repo ``.env`` (key id + PEM path); the private key is never
printed or embedded. Exercises status/markets/orderbook/balance reads and asserts
they parse into the typed models.
"""
import os
import pathlib
import time
from decimal import Decimal

import pytest

pytestmark = [
    pytest.mark.prod_ro,
    pytest.mark.skipif(
        os.environ.get("RUN_PROD_RO") != "1",
        reason="set RUN_PROD_RO=1 to run live read-only production tests",
    ),
]

PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _load_env():
    root = pathlib.Path(__file__).resolve().parents[1]
    env = {}
    for line in (root / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


@pytest.fixture(scope="module")
def client():
    from betting_agent.kalshi import KalshiClient

    env = _load_env()
    key_id = env.get("KALSHI_API_KEY_ID")
    key_path = os.path.expanduser(env.get("KALSHI_PRIVATE_KEY_PATH", ""))
    if not key_id or not pathlib.Path(key_path).exists():
        pytest.skip("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not configured in .env")
    c = KalshiClient(PROD_BASE, key_id, key_path)
    yield c
    c.close()


def test_exchange_status_reads(client):
    status = client.get_exchange_status()
    assert isinstance(status.active, bool)


def test_balance_reads(client):
    balance = client.get_balance()
    assert isinstance(balance.dollars, Decimal)
    assert balance.dollars >= 0


def test_markets_and_orderbook_read_and_parse(client):
    markets, cursor = client.get_markets(
        max_close_ts=int(time.time()) + 72 * 3600, status="open", limit=20
    )
    assert markets, "expected at least one open market in the next 72h"
    m = markets[0]
    assert m.ticker and m.close_time is not None and m.close_time.tzinfo is not None
    assert cursor is None or isinstance(cursor, str)

    book = client.get_orderbook(m.ticker)
    ask = book.best_ask("yes")
    assert ask is None or isinstance(ask, Decimal)


def test_get_market_detail_reads(client):
    markets, _ = client.get_markets(
        max_close_ts=int(time.time()) + 72 * 3600, status="open", limit=5
    )
    detail = client.get_market(markets[0].ticker)
    assert detail.ticker == markets[0].ticker
    assert isinstance(detail.tick_size, Decimal)


def test_fills_and_settlements_paginate(client):
    fills, fcur = client.get_fills()
    settlements, scur = client.get_settlements()
    assert isinstance(fills, list) and isinstance(settlements, list)
    assert fcur is None or isinstance(fcur, str)
    assert scur is None or isinstance(scur, str)


def test_candlesticks_read_and_parse(client):
    """Price history reads for a liquid live market: series is derived automatically and
    the payload parses into Candle (Decimal OHLC, int volume/OI, tz-aware ts, ascending)."""
    markets, _ = client.get_markets(
        max_close_ts=int(time.time()) + 7 * 24 * 3600, status="open", limit=100
    )
    assert markets, "expected at least one open market"
    liquid = max(markets, key=lambda m: (m.volume or 0))
    candles = client.get_candlesticks(liquid.ticker, period_minutes=60)
    assert isinstance(candles, list)
    assert candles, f"expected hourly history for liquid market {liquid.ticker}"
    c = candles[-1]
    assert c.ts is not None and c.ts.tzinfo is not None
    for v in (c.open, c.high, c.low, c.close):
        assert v is None or isinstance(v, Decimal)
    assert c.volume is None or isinstance(c.volume, int)
    assert c.open_interest is None or isinstance(c.open_interest, int)
    ts = [x.ts for x in candles if x.ts is not None]
    assert ts == sorted(ts)  # server returns oldest-first; preserved
