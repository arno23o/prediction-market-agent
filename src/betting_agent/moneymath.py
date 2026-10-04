"""Money and probability math (spec §8).

Everything is :class:`decimal.Decimal`; floats are never used. Values are carried at
4 decimal places to match the ledger's TEXT storage convention (spec §5).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

D = Decimal

_Q4 = D("0.0001")
_ONE = D("1")


def q4(x: Decimal | str | int) -> Decimal:
    """Quantize ``x`` to 4 decimal places (bankers' rounding)."""
    return D(x).quantize(_Q4)


def unit_fee(price: Decimal, coef: Decimal) -> Decimal:
    """Per-contract taker fee, *unceiled*: ``coef * price * (1 - price)`` (spec §8).

    Carried at 4dp like all money. Reported by ``bt fees`` so a session can price one
    contract's fee before it sizes a bet.
    """
    price = D(price)
    return q4(D(coef) * price * (_ONE - price))


def fee(contracts: Decimal | int, price: Decimal, coef: Decimal) -> Decimal:
    """Taker fee charged per order (spec §8, model replaced by docs/14 D5).

    ``fee = ceil_to_0.0001(coef * contracts * price * (1 - price))`` — the ceiling is to
    the ledger's own 4dp grid, with **no 1¢ floor and no rounding to the cent**. The
    exchange dropped the cent floor: all 17 live fills through 2026-08-05 match this
    formula exactly (docs/12 §2), including a $0.0007 fee on a 1-cent contract that no
    cent-ceiling model can produce, and a 0.008925 -> 0.0090 case that proves ceiling
    rather than rounding. The old ``ceil_to_cent`` model overstated real fees by 58% on
    that record, and since paper and shadow fills are charged the *model* rather than an
    exchange receipt, it systematically pessimized the counterfactual record the
    experiments are measured against.

    A value already on the 4dp grid is left alone; the ceiling only bumps a remainder
    below 0.0001. Every fee estimator inherits this: paper fills, shadow scoring, the
    hypothetical scoring of no-fill legs (docs/14 D12), and the fee-absent fallback at
    settlement. ``unit_fee`` (V09's edge check) was always unceiled and is unchanged.
    """
    price = D(price)
    raw = D(coef) * D(contracts) * price * (_ONE - price)
    return raw.quantize(_Q4, rounding=ROUND_CEILING)


def bet_pnl(
    outcome: str, contracts: Decimal | int, fill_price: Decimal, fee_amt: Decimal,
    *, stake: Decimal | None = None,
) -> Decimal:
    """Settled-bet P/L (spec §8).

    This is the **metric of record**: net P/L after fees, by construction — the fee is
    subtracted on a win *and* on a loss (decision 2026-07-29, Jul29 spec L2). Gross P/L
    (= net + fees) is derived, never stored, and never reported alone.

    ``win  -> contracts * (1 - fill_price) - fee``
    ``loss -> -(contracts * fill_price) - fee``
    ``void -> 0``  (voids carry no fee)

    ``stake``, when given, is what the contracts actually cost and stands in for
    ``contracts * fill_price``: a win is then ``contracts - stake - fee`` and a loss
    ``-stake - fee``. A real leg that filled at several prices needs it, because its
    average price does not fit four decimal places and the product misses the exchange's
    own cost by a hundredth of a cent or so (A-0316-B01, 2026-09-27; see
    ``kalshi.types.reported_cost``). Without it the result is exactly what it always was.

    ``"scalar"`` is deliberately NOT accepted and raises like any other unknown outcome:
    a scalar settlement pays a value the exchange chooses, which no formula over
    count/price/fee can reproduce, so it needs the payout as an input and goes through
    :func:`scalar_pnl`. Raising here is what stops a scalar row from being scored as a
    win or a loss by a caller that forgot the difference.
    """
    c = D(contracts)
    f = D(fee_amt)
    s = D(stake) if stake is not None else c * D(fill_price)
    if outcome == "win":
        return q4(c - s - f)
    if outcome == "loss":
        return q4(-s - f)
    if outcome == "void":
        return q4(D(0))
    raise ValueError(f"unknown outcome: {outcome!r}")


def scalar_pnl(
    payout: Decimal, contracts: Decimal | int, fill_price: Decimal, fee_amt: Decimal,
    *, stake: Decimal | None = None,
) -> Decimal:
    """Settled P/L for a SCALAR settlement: ``payout - stake - fee``.

    Some markets do not resolve to a side. A game total on a shortened game settles with
    ``market_result: "scalar"``, and the exchange credits an arbitrary per-contract value
    — for A-0097-B01 (KXNPBTOTAL-26AUG130500HIRYAK-12, 2026-08-15) $0.82 on a 1-contract
    NO position bought at $0.75 — **and keeps the fee**. That is neither of the two shapes
    :func:`bet_pnl` knows: it is not a $1 win, not a zero loss, and emphatically not a
    void (a void refunds stake AND fee, which booked that bet at $0.00 against a real
    +$0.0568 and put the whole system into a false-alarm HALT).

    So the payout is an INPUT here rather than a derivation — the exchange's own
    ``revenue`` for our position is the only source of it — and the fee is subtracted, not
    refunded, keeping this the same net-of-fees metric of record :func:`bet_pnl` is
    (decision 2026-07-29, Jul29 spec L2).

    ``stake`` means what it means in :func:`bet_pnl`: the cost of the contracts when the
    caller knows it better than ``contracts * fill_price``.
    """
    s = D(stake) if stake is not None else D(contracts) * D(fill_price)
    return q4(D(payout) - s - D(fee_amt))
