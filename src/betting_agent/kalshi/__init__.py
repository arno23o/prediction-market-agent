"""Kalshi exchange layer: typed models, signing, the HTTP client, and a fake exchange.

Verified live (read-only, production) 2026-07-07 — see ``client`` module docstring for
the endpoint report and the spec corrections it encodes.
"""
from .client import KalshiAPIError, KalshiClient, OrderAmbiguous, sign_pss
from .testing import FakeKalshi
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

__all__ = [
    "KalshiClient",
    "KalshiAPIError",
    "OrderAmbiguous",
    "sign_pss",
    "FakeKalshi",
    "Market",
    "Orderbook",
    "OrderResult",
    "Fill",
    "Settlement",
    "Balance",
    "Candle",
    "ExchangeStatus",
]
