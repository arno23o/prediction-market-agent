"""The vaccine (FK-9): FakeKalshi's observable surface, diffed against the real client's.

Every test below runs ONE scenario twice — once against ``FakeKalshi``, once against a
``KalshiClient`` over ``httpx.MockTransport`` scripted to answer the way the exchange
answers that scenario — and asserts a single fact holds identically on both. Nothing here
tests behavior for its own sake; each fact is one the harness reads and would act on.

Why this file exists: the 2026-07-31 outage (the legacy order endpoint had been retired
and returned HTTP 410) burned three attempts and a canary before anyone saw it, and the
suite was green throughout, because the fake could not represent a rejection at all. The
suite's confidence was a statement about the fake, not about production. Every drift the
August review found — a fake that validated nothing, emitted a status the real client
never synthesizes, returned 0/synthetic ids where the client returns ``None``, and served
fills carrying a ``client_order_id`` the live endpoint has never carried — was invisible
for the same reason: nothing compared the two surfaces.

So this is a diff, and it is meant to FAIL when the two drift apart again. If a change
here needs both sides edited to agree, that is the file doing its job; if it needs only
one side edited, that is drift, and the fix belongs in the fake (or in the client).
"""
import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING
from decimal import Decimal as D

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from betting_agent.kalshi.client import KalshiAPIError, KalshiClient
from betting_agent.kalshi.testing import FakeKalshi

BASE = "https://api.elections.kalshi.com/trade-api/v2"
TICKER = "KXCONTRACT-26AUG04-B50"
COID = "A-0001-B01"
LIMIT = D("0.4200")
CLOSE = datetime.now(UTC) + timedelta(hours=6)
ORDER_ID = "OID-1"

# One fill, in the recorded live shape: order_id, *_fp count, *_dollars price — and no
# client_order_id, because the endpoint has never served one.
_FILL_PAYLOAD = {
    "order_id": ORDER_ID, "ticker": TICKER, "side": "yes", "count_fp": "1.00",
    "yes_price_dollars": "0.4200", "created_time": "2026-08-04T15:30:00Z", "is_taker": True,
}


@pytest.fixture(scope="module")
def key_path(tmp_path_factory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = tmp_path_factory.mktemp("contract-keys") / "key.pem"
    path.write_bytes(pem)
    return str(path)


# --------------------------------------------------------------------------- the worlds
def fake_world(*, ask: str = "0.4200", size=10, balance: str = "100.0000") -> FakeKalshi:
    """The fake exchange, with one market whose book the scenario chooses."""
    f = FakeKalshi(balance=balance, fee_coef=D("0.07"))
    f.add_market(TICKER, title="contract market", close_time=CLOSE,
                 yes_ask=D(ask), yes_ask_size=size)
    return f


def real_world(key_path, *, filled: str = "1.00", order_status: int = 201,
               order_body: str = "", calls: dict | None = None) -> KalshiClient:
    """The real client, over a transport scripted to answer as the exchange would.

    ``filled`` is the receipt's ``fill_count``; ``order_status`` != 201 makes the create
    fail with that status and body. The portfolio reads answer with the recorded shapes,
    so ``get_fills`` and the orders->fills join can be diffed too.
    """
    counters = calls if calls is not None else {}

    def handler(request):
        path = request.url.path
        counters[path] = counters.get(path, 0) + 1
        if path.endswith("/portfolio/events/orders"):
            if order_status != 201:
                return httpx.Response(order_status, text=order_body)
            payload = {
                "order_id": ORDER_ID, "client_order_id": COID,
                "fill_count": filled, "remaining_count": "0.00", "ts_ms": 1785476472000,
            }
            if D(filled) > 0:  # a receipt names a price and a fee only if something traded
                payload["average_fill_price"] = "0.4200"
                # Both fee fields the live order object carries (docs/25): the leg TOTAL,
                # which is what the client stores, beside the per-contract average, which
                # it must never multiply.
                payload["average_fee_paid"] = "0.0200"
                payload["taker_fees_dollars"] = str(
                    (D("0.07") * D(filled) * LIMIT * (D(1) - LIMIT))
                    .quantize(D("0.0001"), rounding=ROUND_CEILING)
                )
                payload["maker_fees_dollars"] = "0.0000"
            return httpx.Response(201, json=payload)
        if path.endswith("/portfolio/fills"):
            return httpx.Response(200, json={"fills": [_FILL_PAYLOAD], "cursor": ""})
        if path.endswith("/portfolio/orders"):
            return httpx.Response(200, json={
                "orders": [{"order_id": ORDER_ID, "client_order_id": COID}], "cursor": "",
            })
        return httpx.Response(404, text=f"unscripted path {path}")

    return KalshiClient(BASE, "test-key-id", key_path,
                        transport=httpx.MockTransport(handler), throttle_rps=1000.0)


# --------------------------------------------------------------------------- scenarios
def full_fill(key_path):
    """1 contract wanted, 1 resting at the limit: the whole order trades."""
    fake, real = fake_world(size=10), real_world(key_path, filled="1.00")
    return (fake.create_order(TICKER, "yes", LIMIT, 1, COID),
            real.create_order(TICKER, "yes", LIMIT, 1, COID))


def no_fill(key_path):
    """The IOC misses: the ask sits above our limit, so nothing trades and the order dies."""
    fake, real = fake_world(ask="0.5000"), real_world(key_path, filled="0.00")
    return (fake.create_order(TICKER, "yes", LIMIT, 1, COID),
            real.create_order(TICKER, "yes", LIMIT, 1, COID))


def partial_fill(key_path):
    """2 contracts wanted, 1 resting: half trades, the remainder is canceled."""
    fake, real = fake_world(size=1), real_world(key_path, filled="1.00")
    return (fake.create_order(TICKER, "yes", LIMIT, 2, COID),
            real.create_order(TICKER, "yes", LIMIT, 2, COID))


# ------------------------------------------------------------------------- a full fill
def test_a_full_fill_is_executed_on_both(key_path):
    """FACT: a filled order's status is ``executed``."""
    fake_r, real_r = full_fill(key_path)
    assert fake_r.status == real_r.status == "executed"


def test_a_full_fill_reports_the_whole_count_on_both(key_path):
    """FACT: ``filled_count`` is the exact count traded, as a ``Decimal``."""
    fake_r, real_r = full_fill(key_path)
    assert fake_r.filled_count == real_r.filled_count == D("1")
    assert isinstance(fake_r.filled_count, D) and isinstance(real_r.filled_count, D)


def test_a_full_fill_carries_a_fee_and_a_price_on_both(key_path):
    """FACT: something traded, so a fee and an average price exist.

    The VALUES are allowed to differ — the fake charges the §8 model (matched to the live
    schedule by docs/14 D5, but a reconstruction all the same), the exchange charges what
    it charges — but "is there a fee at all" is what the executor branches on.

    Since docs/25 both sides read a leg TOTAL rather than a per-contract average times the
    count, so on this one-contract scenario they also agree on the figure.
    """
    fake_r, real_r = full_fill(key_path)
    assert fake_r.fee is not None and real_r.fee is not None
    assert fake_r.avg_fill_price is not None and real_r.avg_fill_price is not None
    assert fake_r.fee == real_r.fee == D("0.0171")


def test_a_full_fill_names_its_order_on_both(key_path):
    """FACT: a filled order has an order_id — the key the fills join runs on."""
    fake_r, real_r = full_fill(key_path)
    assert fake_r.order_id and real_r.order_id


# ---------------------------------------------------------------------------- no fill
# NOT diffed here, deliberately: ``order_id`` on a no-fill. The fake returns ``None``
# (WP5/FK-3, as specified — nothing traded, so it claims nothing), while a live receipt
# may well name the order it just canceled. Nothing in production reads the id of an
# unfilled order — the fills join runs on filled ones — so pinning either answer would be
# pinning a guess about a field no code consults. The three fields that ARE consulted on a
# no-fill (status, fee, filled_count) are diffed below.
def test_a_no_fill_is_canceled_on_both(key_path):
    """FACT: nothing traded, so the IOC's status is ``canceled`` — never a third word."""
    fake_r, real_r = no_fill(key_path)
    assert fake_r.status == real_r.status == "canceled"


def test_a_no_fill_charges_no_fee_on_both(key_path):
    """FACT: ``fee is None`` on a no-fill (FK-3).

    The fake used to return ``Decimal(0)``, which is a different claim: zero says "charged
    nothing", ``None`` says "no fee was reported". The executor's fee fallback keys on
    exactly this, so the difference is money.
    """
    fake_r, real_r = no_fill(key_path)
    assert fake_r.fee is None and real_r.fee is None


def test_a_no_fill_has_no_average_price_on_both(key_path):
    """FACT: nothing traded, so there is no traded price."""
    fake_r, real_r = no_fill(key_path)
    assert fake_r.avg_fill_price is None and real_r.avg_fill_price is None


def test_a_no_fill_counts_zero_on_both(key_path):
    """FACT: ``filled_count`` is zero, not ``None`` — it is always a number."""
    fake_r, real_r = no_fill(key_path)
    assert fake_r.filled_count == real_r.filled_count == 0


# --------------------------------------------------------------------------- partial
def test_a_partial_fill_is_executed_on_both(key_path):
    """FACT: a partial fill is ``executed`` too — the vocabulary has no third value."""
    fake_r, real_r = partial_fill(key_path)
    assert fake_r.status == real_r.status == "executed"


def test_a_partial_fill_reports_less_than_was_asked_for_on_both(key_path):
    """FACT: the partial-ness lives in ``filled_count``, and only there."""
    fake_r, real_r = partial_fill(key_path)
    assert fake_r.filled_count == real_r.filled_count == D("1")
    assert fake_r.filled_count < 2 and real_r.filled_count < 2


# ------------------------------------------------------------------ refusals (FK-1)
@pytest.mark.parametrize(
    "bad",
    [
        {"action": "sell"},
        {"side": "bid"},    # V2's WIRE vocabulary: bid/ask quote the single YES book
        {"side": "ask"},
        {"time_in_force": "gtc"},
        {"price": D("0.42005")},
        {"count": D("1.5")},
    ],
)
def test_a_refused_request_fails_identically_on_both(key_path, bad):
    """FACT: the same bad request raises ``ValueError`` with the same message on both.

    Message equality is deliberate. These refusals are the client's local contract, and a
    fake that accepts what the client refuses lets a test prove a request is fine when
    production would never send it — which is how ``side='bid'`` could be routed to the NO
    book in the fake and rejected by the exchange.
    """
    args = {"ticker": TICKER, "side": "yes", "price": LIMIT, "count": 1,
            "client_order_id": COID, **bad}
    fake, real = fake_world(), real_world(key_path)

    with pytest.raises(ValueError) as fake_exc:
        fake.create_order(**args)
    with pytest.raises(ValueError) as real_exc:
        real.create_order(**args)

    assert str(fake_exc.value) == str(real_exc.value)


def test_a_refused_request_reaches_neither_exchange(key_path):
    """FACT: a refusal costs no request. A request that lands can fill, and neither side
    has any way to un-fill it."""
    calls: dict = {}
    fake, real = fake_world(), real_world(key_path, calls=calls)

    for client in (fake, real):
        with pytest.raises(ValueError):
            client.create_order(TICKER, "bid", LIMIT, 1, COID)

    assert fake.orders_placed == [] and fake.calls == {}
    assert calls == {}


def test_an_unknown_ticker_is_a_definite_rejection_on_both(key_path):
    """FACT: an unknown ticker is ``KalshiAPIError(404)`` — a definite rejection, not an
    ambiguity and not a graceful no-fill. The executor's taxonomy splits on exactly this:
    a definite error records a no-fill and moves on, an ambiguity re-queries the fills."""
    fake = fake_world()  # nothing registered under the ghost ticker
    real = real_world(key_path, order_status=404, order_body="market not found")

    with pytest.raises(KalshiAPIError) as fake_exc:
        fake.create_order("KXGHOST-26AUG04-B50", "yes", LIMIT, 1, COID)
    with pytest.raises(KalshiAPIError) as real_exc:
        real.create_order("KXGHOST-26AUG04-B50", "yes", LIMIT, 1, COID)

    assert fake_exc.value.status == real_exc.value.status == 404


# ------------------------------------------------------------- fills read-back (FK-4)
def test_served_fills_carry_no_client_order_id_on_both(key_path):
    """FACT: ``get_fills`` never yields a ``client_order_id``.

    The live endpoint serves only ``order_id``; attribution is the orders->fills join.
    Anything written against fake fills-with-coids works in the suite and reads ``None``
    in production — and §10's impostor test, which turns on personal fills having no coid,
    cannot even be stated on a surface that invents one.
    """
    fake, real = fake_world(), real_world(key_path)
    fake.create_order(TICKER, "yes", LIMIT, 1, COID)

    fake_fills, _ = fake.get_fills()
    real_fills, _ = real.get_fills()

    assert fake_fills and real_fills  # both really served a fill
    assert all(f.client_order_id is None for f in fake_fills)
    assert all(f.client_order_id is None for f in real_fills)


def test_the_join_stamps_the_coid_on_both(key_path):
    """FACT: ``find_fills_by_client_order_id`` returns fills carrying the coid it joined
    on — the other half of FK-4, and the only reason attribution works at all."""
    fake, real = fake_world(), real_world(key_path)
    fake.create_order(TICKER, "yes", LIMIT, 1, COID)

    fake_joined = fake.find_fills_by_client_order_id(COID)
    real_joined = real.find_fills_by_client_order_id(COID)

    assert fake_joined and real_joined
    assert all(f.client_order_id == COID for f in fake_joined)
    assert all(f.client_order_id == COID for f in real_joined)


def test_a_served_fill_carries_the_same_money_fields_on_both(key_path):
    """FACT: count, price and side land in the same fields with the same types — the three
    values every settle/reconcile comparison is made of."""
    fake, real = fake_world(), real_world(key_path)
    fake.create_order(TICKER, "yes", LIMIT, 1, COID)

    fake_fill = fake.get_fills()[0][0]
    real_fill = real.get_fills()[0][0]

    assert fake_fill.count == real_fill.count == D("1")
    assert fake_fill.price == real_fill.price == D("0.4200")
    assert fake_fill.side == real_fill.side == "yes"
    assert fake_fill.ticker == real_fill.ticker == TICKER


# ------------------------------------------------------------------- the wire request
def test_the_request_the_client_would_send_is_the_one_the_fake_records(key_path):
    """FACT: the fake's transmission log holds the same order the client puts on the wire
    — same ticker, side, price, count and coid. The client translates ``side`` to V2's
    ``bid``/``ask`` and the price with it; the fake records the caller's terms, and this
    pins the translation so a NO bet cannot silently become a YES one on one side only."""
    sent: dict = {}

    def handler(request):
        sent["body"] = json.loads(request.content)
        return httpx.Response(201, json={"order_id": ORDER_ID, "client_order_id": COID,
                                         "fill_count": "0.00"})

    real = KalshiClient(BASE, "test-key-id", key_path,
                        transport=httpx.MockTransport(handler), throttle_rps=1000.0)
    fake = fake_world(ask="0.5000")

    fake.create_order(TICKER, "no", D("0.0700"), 1, COID)
    real.create_order(TICKER, "no", D("0.0700"), 1, COID)

    recorded = fake.orders_placed[-1]
    assert recorded["ticker"] == sent["body"]["ticker"] == TICKER
    assert recorded["client_order_id"] == sent["body"]["client_order_id"] == COID
    assert recorded["side"] == "no" and sent["body"]["side"] == "ask"  # NO buy = sell YES
    assert recorded["price"] == D("0.0700")
    assert D(sent["body"]["price"]) == D(1) - recorded["price"]  # quoted off the YES leg
    assert str(int(recorded["count"])) == sent["body"]["count"]
