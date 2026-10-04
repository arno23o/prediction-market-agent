"""Order execution (docs/22 sections 5.6 and 7.6).

Takes a :class:`~betting_agent.harness.validate.ValidationOutcome` and turns it into
ledger rows: every proposed bet, validated or rejected, gets exactly one ``bets`` row.
The flow:

1. **Snapshot** each bet's orderbook (already on ``ValidatedBet.book``) into
   ``book_snapshot`` (top 5 levels per side + a timestamp).
2. **Live gate + real placement** — real orders require :func:`safety.real_orders_allowed`
   (which also consults the drawdown floor, against a freshly read balance). When the gate
   is open, **every** validated bet is real, walked in **ticket order**, because the
   pre-registered order is the honest one. A bet whose projected stake at limit price
   (``contracts`` from the ticket times the limit price) would breach
   ``daily_real_stake_cap``, the charge day's ``per_market_real_cap`` or the attempt's
   ``per_attempt_real_cap`` is **cap-rejected**: not placed, not papered,
   ``status='rejected'`` with ``reject_code='cap_daily'`` / ``'cap_market'`` /
   ``'cap_attempt'`` and a ``cap_stop`` audit. Rejection consumes no headroom, so a
   smaller later bet may still fit. The attempt allowance is enforced first by the
   validator (V16), which the session runs before it submits; ``cap_attempt`` is the
   second guard, and on a ticket the validator has already passed it does not bind.
   When the **drawdown floor** refuses (docs/22 section 7.4), every bet is recorded the
   same way with ``reject_code='drawdown_floor'``, a ``floor_stop`` audit and one alert,
   and again no paper row is written. When the gate is closed for any other reason (HALT,
   ``live_trading`` off), everything is paper and no cap projection runs. Every refusal,
   those and the exchange's own, also writes ``reject_reason``: one plain sentence on the
   row saying why, which is what the reviews and the models read (section 7.5).
3. **Paper fills** — filled iff ``best_ask(side) ≤ limit_price`` at snapshot, filled at
   ``best_ask``; fee simulated per §8.
4. **Real orders** — IOC limit at ``limit_price``, ``client_order_id = bet_id``, placed
   sequentially; partial → filled with actual count/avg, zero → no_fill,
   ``OrderAmbiguous`` → resolved via a fills query before recording.

A cap-rejected row also carries ``declared_contracts`` (docs/14 D12): every execution field
on it is NULL by design, which left the size of a cap-refused leg unrecorded and so
invisible to the grader packet that has to show the portfolio the caps truncated.

**Write-down order is a money-safety property** (AE-1/ST-1). Rows that can be known
before any order is sent — validation-rejected and cap-rejected bets — are inserted
first; then each bet's row is inserted *in the same loop iteration that resolved its
order*. So at every instant between two orders, everything already transacted is
durably in the ledger. Nothing in the placement path may raise past that loop either: a
definite ``KalshiAPIError`` and any other client exception both record the bet as a
no-fill with an ``order_error`` audit.

Counts are exact ``Decimal`` end to end (KC-2): the exchange trades fractional contracts
and fills in fractional pieces, so ``int()`` anywhere on this path is a money bug.

The attempt's final status (``"placed"`` if ≥1 fill else ``"no_bets"``) is *returned* —
this module never calls ``ledger.transition``; the attempt orchestrator owns transitions.
Each ledger write is individually atomic; none are nested.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from betting_agent.harness.notify import record_failure, record_success
from betting_agent.harness.safety import FLOOR_CODE, is_halted, real_orders_allowed
from betting_agent.harness.validate import (
    ValidatedBet,
    ValidationOutcome,
    reason_for_code,
)
from betting_agent.ids import bet_id as make_bet_id
from betting_agent.kalshi.client import KalshiAPIError, OrderAmbiguous
from betting_agent.moneymath import D, q4
from betting_agent.moneymath import fee as fee_calc
from betting_agent.timeutil import et_day, iso, utc_now

# An empty first fills scan does not prove absence (KC-3/AE-3): visibility lags writes,
# and the lag correlates with the very conditions that make an order ambiguous. Wait this
# long, then scan once more; only the second empty answer is a no-fill.
_AMBIGUITY_RESCAN_DELAY_S = 4.0
_sleep = time.sleep  # module-level indirection so tests can wait instantly

_ZERO = D(0)

# The drawdown floor's alert (docs/22 section 7.4). Threshold ONE: the floor refusing is
# not a blip, it is a whole attempt's book turned into refusals, and the operator has to
# know the same day. The streak's "already notified" flag still holds the banner to one
# per outage.
_FLOOR_THRESHOLD = 1


def _coef_for(settings, category: str | None) -> Decimal:
    if category is not None:
        c = settings.fees.category_coefs.get(category)
        if c is not None:
            return D(c)
    return D(settings.fees.default_coef)


def _iso_or_none(dt) -> str | None:
    return iso(dt) if dt is not None else None


def _snapshot(book, ts_iso: str) -> str | None:
    """Serialize the top 5 bid levels per side plus a timestamp."""
    if book is None:
        return None

    def lv(levels):
        return [[str(p), str(s)] for p, s in list(levels)[:5]]

    return json.dumps({"yes": lv(book.yes_levels), "no": lv(book.no_levels), "ts": ts_iso})


@dataclass
class _Unit:
    """One validated bet on its way to placement.

    Units are walked in ticket order; book depth no longer influences selection now that
    every validated bet is real (Jul29 spec L4). ``reject_code`` is set (``"cap_daily"`` /
    ``"cap_market"`` / ``"cap_attempt"`` / ``"drawdown_floor"``) when the bet was refused
    before placement; such a bet is never executed at all, neither real nor paper, and
    ``reject_reason`` carries the plain sentence that goes on its row.
    """

    leg: ValidatedBet
    is_real: bool = False
    reject_code: str | None = None
    reject_reason: str | None = None

    @property
    def index(self) -> int:
        return self.leg.spec.ticket_index


@dataclass
class _Ctx:
    """Everything the placement path needs, carried once instead of threaded per call.

    ``halt_audited`` makes the mid-attempt HALT audit fire **once per attempt** rather
    than once per refused leg (SV-7/ST-3).
    """

    ledger: Any
    client: Any
    settings: Any
    attempt_id: str
    now_iso: str
    halt_audited: bool = False


def _leg_contracts(leg: ValidatedBet) -> Decimal:
    """The size the ticket declared for this leg (docs/22 section 7.6)."""
    return leg.contracts


# --------------------------------------------------------------------------- #
# result records (one per attempted bet; validation-rejected bets get None)
# --------------------------------------------------------------------------- #
def _no_fill(is_real, contracts, order_id, coid, placed_at, reject_reason=None) -> dict:
    """``placed_at`` is the "an order was actually sent" discriminator (AE-6) — NOT
    ``order_id``, which is also ``None`` on a genuine real no-fill. A caller recording a
    real leg that was never transmitted at all (a HALT arriving mid-attempt) must pass
    ``placed_at=None`` even though ``is_real`` stays 1: it was a real intention, just
    never one an order left the process for."""
    return {
        "status": "no_fill", "is_real": is_real, "contracts": contracts,
        "fill_price": None, "stake": None, "fee": None,
        "order_id": order_id, "client_order_id": coid, "placed_at": placed_at,
        "reject_reason": reject_reason,
    }


def _filled(is_real, contracts, fill_price, fee_amt, order_id, coid, placed_at) -> dict:
    return {
        "status": "filled", "is_real": is_real, "contracts": contracts,
        "fill_price": fill_price, "stake": q4(D(contracts) * D(fill_price)),
        "fee": fee_amt, "order_id": order_id, "client_order_id": coid, "placed_at": placed_at,
    }


def _cap_rejected(code: str, declared: Decimal, reason: str | None = None) -> dict:
    """A leg refused before placement: nothing was attempted, so every execution field is
    ``None`` and ``contracts`` too — the row records an intention, not a position.

    Two refusals share this shape: a spend cap, and the drawdown floor (docs/22 7.4). The
    floor used to leave every unit paper with nothing written down at all, which is how
    seven attempts on 2026-08-17 looked like ordinary no-bets days. A refused real leg is
    a refused real leg whichever guard refused it, so both are recorded the same way and
    both are scored later by settle's ``_settle_rejects``.

    ``declared_contracts`` records the size that intention had (docs/14 D12). It is not an
    execution field and never becomes one; it exists because a cap-refused leg was
    otherwise sizeless in the ledger, so the grader could not see the shape of the
    portfolio the caps truncated (the A-0058 case: 1 of 3 legs on one match kept, with
    nothing telling the grader the book it judged was not the book proposed).
    """
    return {
        "status": "rejected", "is_real": False, "contracts": None,
        "fill_price": None, "stake": None, "fee": None,
        "order_id": None, "client_order_id": None, "placed_at": None,
        "reject_code": code, "reject_reason": reason, "declared_contracts": declared,
    }


# --------------------------------------------------------------------------- #
# real order placement
# --------------------------------------------------------------------------- #
def _no_fill_result() -> tuple[Decimal, None, Decimal, None]:
    """The tuple every refusal/failure returns: nothing filled, nothing charged."""
    return _ZERO, None, _ZERO, None


def _find_fills(ctx: _Ctx, bid: str) -> tuple[list, bool]:
    """One fills-join pass. Returns ``(fills, errored)``.

    A failing scan must never propagate: we are inside the ambiguity handler, and an
    exception escaping here abandons the ledger rows of orders that already filled —
    the exact AE-1 hole. An unreadable scan is treated as "found nothing yet".
    """
    try:
        return list(ctx.client.find_fills_by_client_order_id(bid)), False
    except Exception:  # noqa: BLE001 - any client failure is just an unreadable scan
        return [], True


def _resolve_ambiguous(ctx: _Ctx, bid: str, idx: int, coef: Decimal):
    """Decide what an ambiguous order actually did, via the orders->fills join.

    An empty FIRST pass is not proof of absence (KC-3/AE-3), so it is retried once after
    a bounded delay; only an empty SECOND pass records a no-fill. Counts are summed as
    exact Decimals, so an order filled in fractional pieces resolves to its true size.
    """
    fills, errored = _find_fills(ctx, bid)
    rescanned = False
    if not fills:
        _sleep(_AMBIGUITY_RESCAN_DELAY_S)
        fills, errored_2 = _find_fills(ctx, bid)
        rescanned, errored = True, errored or errored_2

    filled = sum((D(str(f.count)) for f in fills), _ZERO)
    avg = None
    order_id = None
    if filled > 0:
        # The position is every fill; the average price is weighted over the PRICED ones
        # only — a fill with no price carries no price information, and multiplying it in
        # as Decimal(None) would raise straight back out of the ambiguity handler.
        priced = [f for f in fills if f.price is not None]
        priced_count = sum((D(str(f.count)) for f in priced), _ZERO)
        if priced_count > 0:
            notional = sum((D(f.price) * D(str(f.count)) for f in priced), _ZERO)
            avg = q4(notional / priced_count)
        order_id = next((f.raw.get("order_id") for f in fills if f.raw.get("order_id")), None)
    ctx.ledger.audit("order_ambiguous", ctx.attempt_id, bid, {
        "ticket_index": idx, "resolved_filled": str(filled), "n_fills": len(fills),
        "rescanned": rescanned, "scan_errored": errored,
    })
    fee_amt = fee_calc(filled, avg, coef) if (filled > 0 and avg is not None) else _ZERO
    ctx.ledger.audit(
        "order_result", ctx.attempt_id, bid, {"filled": str(filled), "via": "fills_query"}
    )
    return filled, avg, fee_amt, order_id


def _direct_result(ctx: _Ctx, bid: str, r, coef: Decimal):
    """Read an unambiguous order response into the executor's result tuple."""
    filled = D(str(r.filled_count))
    avg = r.avg_fill_price
    if r.fee is not None:
        fee_amt = D(r.fee)
    elif filled > 0 and avg is not None:
        fee_amt = fee_calc(filled, avg, coef)
    else:
        fee_amt = _ZERO
    ctx.ledger.audit(
        "order_result", ctx.attempt_id, bid,
        {"filled": str(filled), "avg": (str(avg) if avg is not None else None),
         "status": r.status},
    )
    return filled, avg, fee_amt, r.order_id


def _order_error(ctx: _Ctx, bid: str, idx: int, ticker: str, exc: BaseException):
    """Record a failed placement as a no-fill instead of losing the whole ticket.

    ``order_error`` stays the money-path record of *every* rejection, and the same text now
    also lands on the bet row as ``reject_reason`` (docs/22 section 7.5). That is what the
    August residency block needed: the exchange refused every order with a plain sentence,
    the sentence went into an audit detail nobody was reading, and the rows themselves said
    only "no fill" while the attempts kept proposing bets into a closed door. The row is
    what the models and the reviews read, so the row is where the sentence belongs. The
    separate insufficient-funds event and alert are gone with it: one rejection, one
    record, and the reason on the row says which rejection it was.
    """
    reason = f"{type(exc).__name__}: {exc}"
    ctx.ledger.audit("order_error", ctx.attempt_id, bid, {
        "ticket_index": idx, "coid": bid, "ticker": ticker,
        "status": getattr(exc, "status", None),
        "error": reason,
    })
    return (*_no_fill_result(), reason)


def _real_order(ctx: _Ctx, bid, idx, ticker, side, price, contracts, coef):
    """Place one IOC real order; audit; resolve ambiguity via a fills query.

    Returns ``(filled_count, avg_fill_price, fee, order_id, transmitted, reject_reason)``
    with ``filled_count`` an exact ``Decimal``. ``transmitted`` is the AE-6 discriminator the
    caller writes to ``placed_at``: ``False`` only on the HALT refusal below, where
    nothing was ever sent. Every other path keeps ``True`` — a plain miss, an
    ``OrderAmbiguous`` resolution, and a definite ``KalshiAPIError`` rejection all mean
    the request reached (or, for the ambiguous case, may have reached) the exchange, and
    an unclassified transport/parse exception has genuinely unknown transmission state,
    so it conservatively keeps its timestamp too rather than claiming an absence it
    cannot prove. The caller must trust this return value rather than re-checking
    ``is_halted`` itself — a second read could see a HALT that arrived a moment after
    this call already committed to transmitting (or not).

    Nothing here raises. A HALT arriving mid-attempt refuses to transmit (SV-7/ST-3); a
    definite ``KalshiAPIError`` and any other client exception both audit ``order_error``
    and record a no-fill (AE-1). Each of those leaves the caller free to write the row it
    already has and carry on with the ticket. ``reject_reason`` is the exchange's own
    refusal text on those two paths and ``None`` everywhere else (docs/22 section 7.5).
    """
    ledger = ctx.ledger
    if is_halted(ctx.settings):
        if not ctx.halt_audited:  # once per attempt, not once per refused leg
            ledger.audit("halt_mid_attempt", ctx.attempt_id, bid, {"ticket_index": idx})
            ctx.halt_audited = True
        return (*_no_fill_result(), False, None)  # AE-6: never transmitted

    ledger.audit(
        "order_placed", ctx.attempt_id, bid,
        {"ticker": ticker, "side": side, "price": str(price), "count": str(contracts)},
    )
    try:
        r = ctx.client.create_order(
            ticker, side, price, contracts, client_order_id=bid, time_in_force="ioc"
        )
    except OrderAmbiguous:
        outcome = _resolve_ambiguous(ctx, bid, idx, coef)
    except KalshiAPIError as exc:
        filled, avg, fee_amt, oid, reason = _order_error(ctx, bid, idx, ticker, exc)
        return filled, avg, fee_amt, oid, True, reason
    except Exception as exc:  # noqa: BLE001 - transport bugs, JSON surprises, anything
        filled, avg, fee_amt, oid, reason = _order_error(ctx, bid, idx, ticker, exc)
        return filled, avg, fee_amt, oid, True, reason
    else:
        outcome = _direct_result(ctx, bid, r, coef)

    filled, avg = outcome[0], outcome[1]
    if filled != filled.to_integral_value():
        ledger.audit("fractional_fill", ctx.attempt_id, bid, {
            "ticket_index": idx, "ticker": ticker, "filled": str(filled),
            "avg": (str(avg) if avg is not None else None),
        })
    return (*outcome, True, None)


# --------------------------------------------------------------------------- #
# per-unit execution
# --------------------------------------------------------------------------- #
def _exec_single(unit, ctx: _Ctx, results):
    vb = unit.leg
    idx = vb.spec.ticket_index
    bid = make_bet_id(ctx.attempt_id, idx)
    contracts = vb.contracts
    coef = _coef_for(ctx.settings, getattr(vb.market, "category", None))
    side = vb.spec.side
    limit = vb.spec.limit_price
    now_iso = ctx.now_iso

    if unit.is_real:
        filled, avg, fee_amt, order_id, transmitted, refusal = _real_order(
            ctx, bid, idx, vb.spec.ticker, side, limit, contracts, coef
        )
        if filled > 0:
            results[idx] = _filled(True, filled, avg, fee_amt, order_id, bid, now_iso)
        else:
            # AE-6: placed_at records "an order was sent", not "the unit resolved real".
            results[idx] = _no_fill(
                True, contracts, order_id, bid, now_iso if transmitted else None, refusal
            )
        return

    ask = vb.book.best_ask(side)
    if ask is not None and ask <= limit:
        fee_amt = fee_calc(contracts, ask, coef)
        results[idx] = _filled(False, contracts, ask, fee_amt, None, None, now_iso)
    else:
        results[idx] = _no_fill(False, contracts, None, None, now_iso)


# --------------------------------------------------------------------------- #
# real placement selection
# --------------------------------------------------------------------------- #
def _floor_refused(attempt_id, units, ledger, settings, why: str) -> None:
    """Record a whole ticket refused by the drawdown floor (docs/22 section 7.4).

    The floor used to return a reason string that this module threw away: every unit
    stayed paper, no audit row was written, no alert was raised, and the attempt looked
    from the outside like an ordinary day of paper bets. Seven attempts ran that way on
    2026-08-17 before anybody noticed. Now the refusal is written down three times over:
    a rejected row per leg carrying the floor's own sentence, one ``floor_stop`` audit for
    the attempt, and one banner. No paper row is written at all, because a paper row on
    this path is a claim the harness would have taken a position it was refused.
    """
    for u in units:
        u.reject_code = FLOOR_CODE
        u.reject_reason = why
    ledger.audit("floor_stop", attempt_id, None, {
        "reason": why, "units": len(units), "legs": len(units),
    })
    try:
        record_failure(
            ledger, settings, FLOOR_CODE, threshold=_FLOOR_THRESHOLD,
            title="betting-agent: the drawdown floor refused real orders",
            message=f"attempt {attempt_id} placed nothing: {why}",
            detail={"attempt_id": attempt_id, "reason": why},
        )
    except Exception:  # noqa: BLE001 - escalation must never become the failure (AE-1)
        pass


def _cap_reason(code: str, *, leg_stake: Decimal, day_spent: Decimal,
                daily_cap: Decimal, ticker: str | None, consumed: Decimal | None,
                market_cap: Decimal, attempt_spent: Decimal = _ZERO,
                attempt_cap: Decimal = _ZERO) -> str:
    """The plain sentence a cap refusal puts on its row (docs/22 section 7.5)."""
    if code == "cap_daily":
        return (f"daily cap: ${q4(day_spent)} of ${q4(daily_cap)} already committed "
                f"today, this leg needed ${q4(leg_stake)}")
    if code == "cap_attempt":
        return (f"attempt allowance: ${q4(attempt_spent)} of ${q4(attempt_cap)} already "
                f"committed by this attempt, this leg needed ${q4(leg_stake)}")
    return (f"per-market cap: ${q4(consumed or D(0))} of ${q4(market_cap)} already "
            f"committed on {ticker} today, this leg needed ${q4(leg_stake)}")


def _select_real(attempt_id, units, ledger, settings, client, charge_day: str) -> None:
    """Mark every unit real, refusing the ones that do not fit.

    ``units`` must already be in ticket order — the caller establishes it, and the caps
    are charged in exactly the order the orders are then sent. ``charge_day`` is the ET
    day of the *placement* clock, passed in rather than re-derived from a second
    ``utc_now()`` call: the day a stake is charged against and the day its ``placed_at``
    records must be the same day, always (AE-4).

    Every validated bet is real when the gate is open; the daily, per-market and
    per-attempt spend caps are the only limits (Jul29 spec L4). They are tested in that
    order, so a leg both the daily cap and the allowance would refuse is recorded as
    ``cap_daily``.

    The attempt allowance (``per_attempt_real_cap``, Arno 2026-09-27) is the attempt's
    stake so far plus this leg's stake at its limit. Nothing of this attempt has filled
    when the projection runs, so the running total starts at zero and counts each leg
    already accepted at its limit, which is the most it can fill for. The validator
    refuses the same legs first under V16, so this is a second guard and records
    ``cap_attempt`` only for a ticket that reached execution without that check.

    Cap projection uses the ticket's ``contracts`` at the *limit* price (the worst price
    we could pay). A refused bet consumes no headroom, so a smaller later bet still gets
    its own honest check; the stop is per bet, not applied to the whole tail.

    **The caps count STAKE ONLY** (docs/14 B1, the recorded choice; docs/12 §2 raised it).
    Both projections here are ``contracts x price`` with no fee term, and
    ``ledger.daily_real_spend`` sums the ``stake`` column, which is likewise fee-free. So
    fees sit *outside* the cap, as does the canary's cost (never a ``bets`` row at all),
    and a maxed day's actual cash outflow exceeds ``daily_real_stake_cap`` by roughly the
    fee rate — ~2% at the $0.07 coefficient (Aug-2 spent $9.8479 of cash against a $10
    stake cap). This is deliberate: the cap governs how much conviction we put at risk per
    day, and a fee is a cost of transacting rather than a position. The consequence to
    remember is that the cap is not a cash-outflow limit — sizing the account's headroom
    (B3) has to allow for stake + fees, not the cap alone.
    """
    allowed, why = real_orders_allowed(settings, ledger, client)
    if not allowed:
        if why.startswith(FLOOR_CODE):
            _floor_refused(attempt_id, units, ledger, settings, why)
        return  # gate closed: paper, or (for the floor) rejected rows and nothing sent
    try:
        # The floor is not refusing, which is the only honest signal that a drawdown ended.
        # Without this the threshold-1 streak would announce the first floor stop this
        # system ever has and then stay silent through every one after it.
        record_success(ledger, FLOOR_CODE)
    except Exception:  # noqa: BLE001 - streak bookkeeping is never worth a raise
        pass

    daily_cap = D(settings.stakes.daily_real_stake_cap)
    market_cap = D(settings.stakes.per_market_real_cap)
    attempt_cap = D(settings.stakes.per_attempt_real_cap)
    day_spent = ledger.daily_real_spend(charge_day)
    attempt_spent = _ZERO
    market_base: dict[str, Decimal] = {}
    running_market: dict[str, Decimal] = {}

    for u in units:
        ticker = u.leg.spec.ticker
        leg_stake = q4(D(_leg_contracts(u.leg)) * u.leg.spec.limit_price)

        code: str | None = None
        market_headroom: Decimal | None = None
        market_consumed: Decimal | None = None
        if day_spent + leg_stake > daily_cap:
            code = "cap_daily"
        else:
            base = market_base.setdefault(
                ticker, ledger.per_market_real_stake(ticker, charge_day)
            )
            market_consumed = base + running_market.get(ticker, D(0))
            if market_consumed + leg_stake > market_cap:
                code = "cap_market"
                market_headroom = q4(market_cap - market_consumed)
            elif attempt_spent + leg_stake > attempt_cap:
                code = "cap_attempt"

        if code is not None:
            u.reject_code = code
            u.reject_reason = _cap_reason(
                code, leg_stake=leg_stake, day_spent=day_spent, daily_cap=daily_cap,
                ticker=ticker, consumed=market_consumed, market_cap=market_cap,
                attempt_spent=attempt_spent, attempt_cap=attempt_cap,
            )
            ledger.audit("cap_stop", attempt_id, None, {
                "unit_min_index": u.index,
                "code": code,
                "unit_stake": str(leg_stake),
                "day_headroom": str(q4(daily_cap - day_spent)),
                "market": ticker if code == "cap_market" else None,
                "market_headroom": (str(market_headroom) if market_headroom is not None else None),
                "attempt_headroom": str(q4(attempt_cap - attempt_spent)),
            })
            continue

        u.is_real = True
        day_spent = q4(day_spent + leg_stake)
        attempt_spent = q4(attempt_spent + leg_stake)
        running_market[ticker] = running_market.get(ticker, D(0)) + leg_stake


# --------------------------------------------------------------------------- #
# row writing
# --------------------------------------------------------------------------- #
def _insert_bet_row(ledger, attempt_id, vb: ValidatedBet, r: dict | None,
                    now_iso: str) -> None:
    """Write one bet's row. ``r`` is its execution record, or ``None`` for a bet that was
    never executed at all (a validation-rejected one)."""
    idx = vb.spec.ticket_index
    if r is None:
        r = {
            "status": "rejected", "is_real": False, "contracts": None,
            "fill_price": None, "stake": None, "fee": None,
            "order_id": None, "client_order_id": None, "placed_at": None,
        }
    market = vb.market
    # a cap or floor rejection happened after validation passed, so it wins over the
    # (absent) validation code; validation-rejected legs have no results record
    code = r.get("reject_code") or vb.reject_code
    # The reason the execution path already computed, else the validator's sentence for
    # this leg (only V16 writes one), else its sentence for the code (docs/22 section 7.5).
    reason = r.get("reject_reason") or vb.reject_reason or reason_for_code(code)
    ledger.insert_bet(
        bet_id=make_bet_id(attempt_id, idx),
        attempt_id=attempt_id,
        ticket_index=idx,
        ticker=vb.spec.ticker,
        market_title=getattr(market, "title", None),
        category=getattr(market, "category", None),
        side=vb.spec.side,
        limit_price=vb.spec.limit_price,
        # model_prob is gone from the ticket (docs/22 section 5.4); the column stays NULL.
        rationale=vb.spec.rationale,
        resolution_event=vb.spec.resolution_event,  # docs/14 D7, free string or None
        is_real=r["is_real"],
        status=r["status"],
        reject_code=code,
        reject_reason=reason,
        contracts=r["contracts"],
        fill_price=r["fill_price"],
        stake=r["stake"],
        fee=r["fee"],
        order_id=r["order_id"],
        client_order_id=r["client_order_id"],
        book_snapshot=_snapshot(vb.book, now_iso),
        close_ts=_iso_or_none(getattr(market, "close_time", None)),
        expected_resolution_ts=_iso_or_none(getattr(market, "expected_expiration", None)),
        placed_at=r["placed_at"],
        # docs/14 D12: only cap-rejected legs carry this, and V16 legs, which the
        # validator refused for the same reason a cap does and after the size had passed
        # V10 and V11 (``vb.contracts`` is set for them and ``None`` for every other
        # validation rejection). Everything else either has a real ``contracts`` (fills,
        # no-fills) or was refused before a size was ever meaningful.
        declared_contracts=(r.get("declared_contracts")
                            or (vb.contracts if vb.reject_code == "V16" else None)),
    )


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def execute_attempt(
    attempt_id: str, outcome: ValidationOutcome, ledger, client, settings, now=None
) -> str:
    """Execute a validated ticket; insert all rows; return ``"placed"`` or ``"no_bets"``.

    ``now`` (optional) fixes the placement clock for deterministic tests; it otherwise
    defaults to :func:`utc_now` **read here, at placement time** — never the attempt's
    start clock (AE-4). That single instant stamps every ``placed_at`` *and* picks the ET
    day the caps are charged against, so the day a bet is recorded on and the day it is
    charged to can never diverge.

    Note the mandated argument *order* is
    ``(attempt_id, outcome, ledger, client, settings)`` — the caller must match it.
    """
    place_now = now if now is not None else utc_now()
    now_iso = iso(place_now)

    if outcome.truncated_count > 0:
        ledger.audit("ticket_truncated", attempt_id, None, {"count": outcome.truncated_count})

    # 1. one placement unit per validated bet, in ticket order: both the cap projection
    #    and the placement loop below walk it, so the order a bet is charged against the
    #    caps is the order it is sent.
    units = [
        _Unit(vb)
        for vb in sorted(outcome.bets, key=lambda x: x.spec.ticket_index)
        if vb.status == "validated"
    ]

    _select_real(attempt_id, units, ledger, settings, client, et_day(place_now))

    # 2. everything knowable before money can move is written down FIRST (AE-1/ST-1):
    #    cap-rejected bets and validation-rejected bets. After this loop every
    #    ticket_index that will never carry an order is already durable.
    results: dict[int, dict] = {}
    live_units: list[_Unit] = []
    for u in units:
        if u.reject_code is None:
            live_units.append(u)
            continue
        # Cap- or floor-rejected: neither placed nor papered.
        results[u.index] = _cap_rejected(
            u.reject_code, _leg_contracts(u.leg), u.reject_reason
        )

    to_place = {u.index for u in live_units}
    for vb in sorted(outcome.bets, key=lambda x: x.spec.ticket_index):
        if vb.spec.ticket_index not in to_place:
            _insert_bet_row(
                ledger, attempt_id, vb, results.get(vb.spec.ticket_index), now_iso
            )

    # 3. execute each remaining bet and record its row in the SAME iteration, so a
    #    failure on bet N+1 can never lose the accounting of bet N's real fill.
    ctx = _Ctx(ledger, client, settings, attempt_id, now_iso)
    for u in live_units:
        _exec_single(u, ctx, results)
        _insert_bet_row(ledger, attempt_id, u.leg, results.get(u.index), now_iso)

    any_filled = any(r["status"] == "filled" for r in results.values())
    return "placed" if any_filled else "no_bets"
