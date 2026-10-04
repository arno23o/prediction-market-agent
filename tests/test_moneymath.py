"""Money math — normative vectors from spec §8."""

from decimal import ROUND_HALF_UP
from decimal import Decimal as D

import pytest

from betting_agent.moneymath import bet_pnl, fee, q4, scalar_pnl, unit_fee


@pytest.mark.parametrize(
    "contracts,price,coef,expected",
    [
        (2, "0.50", "0.07", "0.0350"),   # 0.0350 exactly on the 4dp grid: untouched
        (1, "0.50", "0.07", "0.0175"),   # 0.0175 exactly on the grid
        (2, "0.42", "0.07", "0.0342"),   # 0.034104 -> ceil to 4dp
        (3, "0.33", "0.07", "0.0465"),   # 0.046431 -> ceil to 4dp
        (1, "0.05", "0.07", "0.0034"),   # 0.003325 -> ceil to 4dp
        (2, "0.50", "0.035", "0.0175"),  # index coef
    ],
)
def test_fee_vectors(contracts, price, coef, expected):
    assert fee(contracts, D(price), D(coef)) == D(expected)


# --------------------------------------------------------------------------- docs/14 D5
# The fee model is not a choice: these are the shapes the live exchange was observed to
# charge, fill by fill, across all 17 live fills through 2026-08-05 (docs/12 §2). Each one
# is impossible under the cent-ceiling model this replaced, and together they pin the two
# properties that distinguish the models — no floor, and ceiling rather than rounding.
def test_the_penny_contract_fee_proves_the_cent_floor_is_gone():
    # C=1 at P=0.01: 0.07 * 0.01 * 0.99 = 0.000693 -> $0.0007. A cent floor cannot
    # produce a seven-hundredth-of-a-cent fee; the exchange charged one.
    assert fee(1, D("0.01"), D("0.07")) == D("0.0007")


def test_the_ceiling_is_a_ceiling_not_a_rounding():
    # C=1 at P=0.15: 0.07 * 0.15 * 0.85 = 0.008925. Rounding to 4dp gives 0.0089
    # (half-even on the 2 that follows); the exchange charged 0.0090.
    assert fee(1, D("0.15"), D("0.07")) == D("0.0090")
    assert q4(D("0.008925")) == D("0.0089")     # what rounding would have said


def test_a_fee_already_on_the_4dp_grid_is_not_bumped():
    # 0.07*4*0.5*0.5 = 0.0700 and 0.07*2*0.5*0.5 = 0.0350, both exact at 4dp: the
    # ceiling must be a no-op, or every whole-grid fee would be overcharged by 0.0001.
    assert fee(4, D("0.50"), D("0.07")) == D("0.0700")
    assert fee(2, D("0.50"), D("0.07")) == D("0.0350")
    assert fee(1, D("0.20"), D("0.035")) == D("0.0056")   # 0.035*0.2*0.8, exact


def test_unit_fee_is_unceiled_and_unchanged_by_d5():
    # The per-contract quote was always the raw product (docs/14 D5 leaves it alone). At
    # a price whose product is already on the grid the ceiled per-order fee agrees with
    # it exactly; under the old cent-ceiling model this pair read 0.0175 vs 0.0200.
    assert unit_fee(D("0.50"), D("0.07")) == D("0.0175")
    assert fee(1, D("0.50"), D("0.07")) == D("0.0175")
    # Off the grid they differ by the ceiling only, never by a cent.
    assert unit_fee(D("0.42"), D("0.07")) == D("0.0171")   # 0.017052 -> q4 rounds
    assert fee(1, D("0.42"), D("0.07")) == D("0.0171")     # 0.017052 -> ceil


def test_pnl_vectors():
    assert bet_pnl("win", 2, D("0.42"), D("0.04")) == D("1.1200")
    assert bet_pnl("loss", 2, D("0.42"), D("0.04")) == D("-0.8800")
    assert bet_pnl("void", 2, D("0.42"), D("0.04")) == D("0.0000")


def test_pnl_unknown_outcome():
    with pytest.raises(ValueError):
        bet_pnl("push", 1, D("0.50"), D("0.00"))


def test_bet_pnl_refuses_a_scalar_outcome():
    """docs/16 §5: a scalar payout is an INPUT, not a derivation, so routing one through
    the binary formula must be impossible rather than merely wrong."""
    with pytest.raises(ValueError):
        bet_pnl("scalar", 1, D("0.75"), D("0.0132"))


def test_scalar_pnl_vectors():
    """The live A-0097-B01 numbers: 1 NO at $0.75, exchange paid $0.82, fee $0.0132 KEPT.
    Booked as a void this was $0.0000 — the residual behind the 2026-08-16 HALT."""
    assert scalar_pnl(D("0.82"), 1, D("0.75"), D("0.0132")) == D("0.0568")
    # the fee is subtracted, never refunded: the same net-of-fees metric bet_pnl is
    assert scalar_pnl(D("0.82"), 1, D("0.75"), D("0")) == D("0.0700")
    # a scalar can land either side of the stake, and below it is a real loss
    assert scalar_pnl(D("0.10"), 1, D("0.75"), D("0.0132")) == D("-0.6632")
    assert scalar_pnl(D("1.64"), 2, D("0.75"), D("0.0264")) == D("0.1136")


def test_pnl_against_a_stake_the_price_cannot_carry():
    """A-0316-B01 (2026-09-27): 3 contracts for $0.4043 across four prices, fee $0.0243.
    The average, $0.134766..., does not fit four places, so ``contracts x fill_price``
    misses the cost whichever way it is rounded. Given the stake, the P/L is exactly the
    payout less what the exchange took."""
    assert bet_pnl("win", 3, D("0.1348"), D("0.0243")) == D("2.5713")   # the old booking
    assert bet_pnl("win", 3, D("0.1348"), D("0.0243"), stake=D("0.4043")) == D("2.5714")
    assert bet_pnl("loss", 3, D("0.1348"), D("0.0243"), stake=D("0.4043")) == D("-0.4286")
    assert bet_pnl("void", 3, D("0.1348"), D("0.0243"), stake=D("0.4043")) == D("0.0000")
    assert scalar_pnl(D("1.50"), 3, D("0.1348"), D("0.0243"),
                      stake=D("0.4043")) == D("1.0714")
    # a stake equal to the product changes nothing, and the default is the product
    for outcome in ("win", "loss"):
        assert bet_pnl(outcome, 3, D("0.18"), D("0.031"), stake=D("0.54")) == bet_pnl(
            outcome, 3, D("0.18"), D("0.031"))


def test_a_two_sided_pair_still_locks_its_payoff():
    # Complementary pair: YES A @ 0.40, NO B @ 0.55, 1 contract each. Fees under D5:
    # 0.0168 exactly (0.07*0.4*0.6) and 0.0174 (0.017325 -> ceil).
    c = 1
    fa = fee(c, D("0.40"), D("0.07"))
    fb = fee(c, D("0.55"), D("0.07"))
    assert (fa, fb) == (D("0.0168"), D("0.0174"))
    happens = bet_pnl("win", c, D("0.40"), fa) + bet_pnl("loss", c, D("0.55"), fb)
    doesnt = bet_pnl("loss", c, D("0.40"), fa) + bet_pnl("win", c, D("0.55"), fb)
    # Nothing in the harness sizes a pair for you now, but the arithmetic that made the
    # payoff worth locking is unchanged: the 5c gross edge, less $0.0342 of fees.
    assert happens == doesnt == D("0.0158")


def test_q4_quantizes():
    assert q4(D("0.5")) == D("0.5000")
    assert str(q4(D("1"))) == "1.0000"


def test_q4_ties_round_half_to_even():
    """CI-8: ``q4``'s docstring claims bankers' rounding; nothing asserted it.

    A tie at the 5th decimal goes to the EVEN 4th digit, not away from zero: 0.00005
    rounds DOWN to 0.0000 (0 is even) while 0.00015 rounds UP to 0.0002. Every money
    value in the ledger passes through this function, so a silent switch to
    ROUND_HALF_UP — a one-word edit, or a caller setting a different decimal context —
    would bias every rounded half-cent in one direction forever.
    """
    assert q4(D("0.00005")) == D("0.0000")
    assert q4(D("0.00015")) == D("0.0002")
    # Symmetric on the negative side, and unmoved when there is no tie to break.
    assert q4(D("-0.00005")) == D("0.0000")
    assert q4(D("-0.00015")) == D("-0.0002")
    assert q4(D("0.00006")) == D("0.0001")
    assert q4(D("0.00025")) == D("0.0002")
    assert q4(D("0.00035")) == D("0.0004")


# ------------------------------------------------- docs/25: the live multi-contract fees
@pytest.mark.parametrize(
    "contracts,price,charged",
    [
        ("3", "0.0500", "0.0100"),   # A-0241-B01 and A-0244-B02, 2026-09-17
        ("3", "0.1800", "0.0310"),   # A-0244-B01, same day
        ("2.34", "0.4100", "0.0397"),  # the owner's cycling order, 2026-08-30
        ("1", "0.0100", "0.0007"),   # the penny contract, docs/12 §2
    ],
)
def test_the_model_reproduces_every_fee_the_exchange_has_reported(contracts, price, charged):
    """The model computes on the LEG TOTAL and rounds up at four decimals, and that
    reproduces every figure this exchange has ever charged us. The three 2026-09-17 legs
    are what proved the harness was doing something else: the client was multiplying the
    exchange's already-rounded per-contract average by the fill count, which gave $0.0099
    where $0.0100 was charged."""
    assert fee(D(contracts), D(price), D("0.07")) == D(charged)


def test_rounding_up_is_the_rule_and_rounding_half_up_is_not():
    """The two candidate rules agree on most figures. The owner's 2.34-contract order is
    the one that separates them: $0.03962322 exact, $0.0397 charged, which is rounding up.
    Half-up would have said $0.0396."""
    exact = D("0.07") * D("2.34") * D("0.41") * (D(1) - D("0.41"))
    assert exact.quantize(D("0.0001"), rounding=ROUND_HALF_UP) == D("0.0396")
    assert fee(D("2.34"), D("0.41"), D("0.07")) == D("0.0397")


def test_a_per_contract_fee_times_the_count_is_not_the_leg_fee():
    """The defect, stated as arithmetic. Rounding per contract and then multiplying
    compounds the rounding once per contract; the exchange rounds once, at the end."""
    per_contract = fee(D("1"), D("0.05"), D("0.07"))
    assert per_contract == D("0.0034")
    assert per_contract * 3 == D("0.0102")          # what per-contract ceiling gives
    assert fee(D("3"), D("0.05"), D("0.07")) == D("0.0100")   # what is charged
