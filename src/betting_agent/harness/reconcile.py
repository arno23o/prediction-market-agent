"""Reconciliation — the master audit (Jul29 spec L8).

The Kalshi account is dedicated (decision 2026-07-29), so **the whole balance must be
explainable to the cent**. :func:`reconcile_once` walks the ledger forward from the live
genesis snapshot, compares the result against ``get_balance()``, runs two exchange
cross-checks, and records the outcome in ``reconciliations``.

The walk (spec §8.2, extended by docs/22 section 7.3)::

    expected = meta.live_genesis_balance
             - Σ (stake + fee)   over real bets that FILLED after genesis
             + Σ payout          over real bets SETTLED after genesis
             - canary cost + canary payout   (post-genesis events only)
             - personal cost - personal fee + personal payout
             + netting + exchange credits
             + absorbed residual   (the drift of every earlier absorbed night)

    payout: win -> contracts x $1 | loss -> 0 | void -> stake + fee refunded
            scalar -> pnl + stake + fee (the exchange's own credit; docs/16 §5)

Money conventions and deliberate choices, stated once here:

* Everything is :class:`~decimal.Decimal` at 4dp (``moneymath.q4``); the report renders
  4dp too, not the 2dp of ``report.py`` — a reconciliation exists to prove cents, and
  rounding the evidence would defeat it.
* ``stake`` is read from ``bets.stake``, which holds what the exchange charged for the
  contracts; ``contracts x fill_price`` is only the fallback for a row with no stake
  stored. Until 2026-09-27 it was the other way round, because settle rewrote the price
  from the fills and left the stake alone. That was fine while every fill landed at a
  whole-cent price. An order that fills in pieces at several prices has an average that
  does not fit four decimal places, so the product missed the exchange's own cost by up
  to $0.0002 a leg, and five live legs put the walk $0.0004 out (A-0316-B01 is the one
  that showed it). Settle now writes the exchange's cost into ``stake`` both when it
  settles a row and when its order scan finds one that disagrees.
* A **void** nets exactly zero by construction: ``settle.py`` zeroes ``fee`` on a void,
  and the same (zeroed) fee is used in both the debit and the refund term. The fee
  cross-check therefore skips ``voided`` bets — comparing a deliberately-zeroed fee
  against the fee the fills imply would be a guaranteed false alarm.
* A row whose timestamp will not parse is **retained** (treated as in-window), the rule
  ``kalshi/testing.py`` and ``settle.py``'s scans already follow: a tripwire must never
  silently drop account activity.
* The canary lives in ``meta.canary``, not in ``bets`` (it is placed with a deliberately
  non-matching ``client_order_id``), so its ticker is also accepted by the settlements
  cross-check, since otherwise its own settlement would read as an uncovered one. Every
  ticker in ``personal_orders`` is accepted for the same reason.
* Orders placed on the account outside the harness are a term here, written down by
  settle's shared-account scan (docs/22 section 7.3); ``cost`` excludes the fee, so the walk
  subtracts both and subtracts each exactly once. An order on a ticker the harness has
  also traded is excluded from the arithmetic, because position netting makes its payout
  inseparable from ours, and reported in the breakdown instead.

The night's verdict has four values since Arno's decision of 2026-09-27, which replaced
the three-way verdict of docs/22 section 7.2 and its repeated-drift rule.

* ``exact``: zero drift and every check passing. A ``reconcile_ok`` audit and nothing else.
* ``absorbed``: every check passing and a drift of at most ``reconcile.absorb_usd``. A
  ``reconcile_absorbed`` audit, a row with ``ok = 0``, no banner and no HALT. The drift
  becomes the absorbed-residual term of every later walk (see :func:`_absorbed_term`), so
  the next night is exact unless something else moves.
* ``reversed``: an absorbed-band drift that is exactly the negative of one earlier
  absorbed night still in force, which is what a later fix explaining that night looks
  like. The row names the earlier run in ``reversed_run_at`` and a
  ``reconcile_absorption_reversed`` audit records it. Both nights then drop out of the
  walk term and the watch, so the term nets to zero and the watch counts neither.
* ``noted``: every check passing and a drift above that and at most
  ``reconcile.halt_drift_usd``. A ``reconcile_drift`` audit carrying the walk, one banner,
  a row with ``ok = 0``, and no HALT. Nothing is absorbed, so the same drift is noted
  again the next night if it stands.
* ``large``: a failing check or a bigger drift. HALT (``reconcile_drift``), a dated
  report, the audit and a banner, exactly as before.

Why absorb at all. Under the three-way verdict the system halted on five of its first ten
nights, three of them over a cent in total, through the rule that halted on the same
drift three nights running. Every sub-cent drift turned out to be fill rounding and the
one cent was an exchange rebate; the cross-checks never failed. The walk is a model of
the exchange, and an incomplete model is not unsafe money. What guards against a slow
leak instead is the residual watch (:func:`_watch_residual`): one alert, never a halt,
when the absorbed nights of the trailing ``reconcile.residual_window_days`` add up past
``reconcile.residual_alert_usd`` in absolute value or number more than
``reconcile.residual_alert_nights``.

Paper era (no ``meta.live_genesis_ts``) -> no row, no audit, ``{"skipped": "paper_era"}``.

One exception, added by owner decision D1 (2026-08-03) as an amendment to L8 step 6: a
dirty run whose *every* residual is attributable to a settlement our own settle pass has
not recorded yet is **provisional** — audited, deferred, not halted. The pending
population is our still-``filled`` real bets *and* an unsettled ``meta.canary`` (the one
non-bet position this walk owns). See :func:`_attribute_pending_settlements` for the gate,
which is total by construction: anything left over halts exactly as before.

Owner deposits and withdrawals (docs/14 B2)
-------------------------------------------
:func:`record_balance_adjustment` lives here because the thing a deposit moves is *this
walk's anchor*: ``meta.live_genesis_balance``. Moving the anchor by exactly the transferred
amount leaves every flow on either side of the transfer intact, which is what lets a
mid-stream deposit reconcile to the cent both before and after it. The verification —
implied transfer versus stated amount — runs against :func:`expected_balance`, i.e. the
same ``_walk`` above, so the check and the nightly proof can never diverge. The one
difference is deliberate: the transfer is verified against the walk minus its absorbed
residual, so an amount the nightly walk absorbed on trust can never pass into the anchor
unexplained.

Cost (EF-1/MP-4)
----------------
This walk used to cost O(bets ever placed × order pages): every real bet ever recorded
got its own ``find_fills_by_client_order_id``, and each of those started a fresh full
pagination of ``/portfolio/orders`` because the server ignores that endpoint's filters.
Nightly, forever, at five requests a second — the same shape as the canary hang.

Two changes make it linear and keep it linear:

* **One shared pagination per run.** Post-genesis orders are paged ONCE for the
  ``coid -> order`` map; post-genesis fills are paged ONCE and indexed by ``order_id``.
  A bet's fills are then a dict lookup. The client's own join is
  still called — but only as a per-bet FALLBACK when the shared index has nothing for
  that bet, so "no fills" is never concluded from an index miss. That fallback is what
  keeps an index gap from becoming a false ``count_mismatch`` HALT.
* **A verification watermark.** ``meta.fills_verified_through`` holds the newest
  ``placed_at`` that a prior run's PASSING FILLS CHECK verified; bets at or before it are
  skipped, because a settled position's fills do not change. The
  population per run is therefore the *new* bets, not every bet ever placed.
  ``reconcile --full`` ignores the watermark, and the skip is never silent: the report
  and the returned ``fills_match`` check both state "verified n new, skipped m
  previously verified".

The watermark follows the fills check rather than the night's verdict: it advances
whenever ``fills_match`` passed, on any verdict alike,
because what it claims is that the fills were verified, and a balance drift does not
unmake that. A provisional run returns before the stamp and an errored run never reaches
it, so a night that concluded nothing still re-verifies in full next time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from betting_agent.harness import safety
from betting_agent.moneymath import D, q4
from betting_agent.moneymath import fee as calc_fee
from betting_agent.timeutil import et_day, iso, parse_iso, utc_now

_ZERO = D("0")
_ONE = D("1")
_CENT = D("0.01")
# Fee comparisons run on the $0.0001 grid since docs/14 D5 (see settle.py's twin
# constant): the cent band was calibrated for cent-granular fees and would pass an 11%
# error on a low-priced bet. Prices keep _CENT — they are cent-quantized by the exchange.
_FEE_TOLERANCE = D("0.0010")

# Statuses meaning "money left the account for this bet" (spec §6.2 lifecycle).
_EVER_FILLED = ("filled", "settled", "voided")
# Statuses meaning "the exchange has closed this position out".
_TERMINAL = ("settled", "voided")

# docs/19 section 4A: the impostor scan used to run here as well as in ``settle.py``, over
# the same order pages, reaching the same verdict. Settle's is the one kept: it runs every
# fifteen minutes rather than once a night, it HALTs on the spot, and its watermark refuses
# to advance while an impostor stands, so the condition keeps re-announcing itself.
_CHECKS = ("fills_match", "settlements_covered")

# The exchange's word for "this market paid a value, not a side", and the ``bets.outcome``
# settle records for it (schema 007). Same string here as in ``settle.py`` on purpose.
_SCALAR = "scalar"

# The night's verdict (Arno, 2026-09-27; see the module docstring). ``exact`` and
# ``large`` keep their docs/22 names because the digest and stored rows read them.
# ``noted`` is what docs/22 called ``small``, over a narrower band, and ``absorbed`` is new.
_EXACT = "exact"
_ABSORBED = "absorbed"
# A night in the absorbed band whose drift is the exact negative of one earlier absorbed
# night still in force: a later fix has explained that night's drift. It is recorded as a
# reversal of that night, not as a new absorption (see :func:`_reversal_target`).
_REVERSED = "reversed"
_NOTED = "noted"
_LARGE = "large"


# --------------------------------------------------------------------------- helpers
def _money(x) -> str:
    """4dp dollar string — the ledger's storage precision, kept in the report."""
    return "—" if x is None else f"${q4(D(x))}"


def _dec(val, default: Decimal = _ZERO) -> Decimal:
    """Parse stored money TEXT (or anything ``Decimal``-able) defensively."""
    if val is None:
        return default
    try:
        return D(str(val))
    except (InvalidOperation, ValueError, ArithmeticError):
        return default


def _ts_or_none(ts_str) -> datetime | None:
    """Parse a stored timestamp, or ``None`` when absent/unreadable.

    Distinct from :func:`_in_window`'s retain-the-unknown rule on purpose: this feeds the
    verification watermark, and a row we cannot date must never be *skipped* on the
    strength of a guess. Unreadable means re-verified, forever, which is the safe side.
    """
    if not ts_str:
        return None
    try:
        return parse_iso(str(ts_str))
    except (ValueError, TypeError):
        return None


def _in_window(ts_str: str | None, genesis: datetime) -> bool:
    """True iff ``ts_str`` is at/after ``genesis``. Absent/unparseable -> True.

    Retaining the unknown is the conservative direction: an event we cannot date still
    gets counted, so a bad timestamp shows up as drift rather than vanishing.
    """
    if not ts_str:
        return True
    try:
        return parse_iso(str(ts_str)) >= genesis
    except (ValueError, TypeError):
        return True


def _stake_of(row: dict) -> Decimal:
    """The stored ``stake``, else ``contracts x fill_price`` (see the module docstring).

    Settle reads a row's stake through this same function, so the walk and the P/L it
    books can never disagree about what a position cost.
    """
    if row.get("stake") is not None:
        return q4(_dec(row.get("stake")))
    contracts = row.get("contracts")
    fill_price = row.get("fill_price")
    if contracts is not None and fill_price is not None:
        return q4(_dec(contracts) * _dec(fill_price))  # exact count: fills are fractional
    return q4(_ZERO)


def _coef_for(settings, category: str | None) -> Decimal:
    """Fee coefficient for ``category`` (spec §8): category override else the default."""
    if category is not None:
        c = settings.fees.category_coefs.get(category)
        if c is not None:
            return D(c)
    return D(settings.fees.default_coef)


def _page_settlements(client, min_ts: datetime | None) -> list:
    out: list = []
    cursor = None
    while True:
        page, cursor = client.get_settlements(min_ts=min_ts, cursor=cursor)
        out.extend(page)
        if not cursor:
            return out


def _page_orders(client, min_ts: datetime | None) -> list[dict]:
    out: list[dict] = []
    cursor = None
    while True:
        page, cursor = client.get_orders(min_ts=min_ts, cursor=cursor)
        out.extend(page)
        if not cursor:
            return out


def _page_fills(client, min_ts: datetime | None) -> list:
    out: list = []
    cursor = None
    while True:
        page, cursor = client.get_fills(min_ts=min_ts, cursor=cursor)
        out.extend(page)
        if not cursor:
            return out


def _orders_by_coid(orders: list[dict]) -> dict[str, dict]:
    """``client_order_id -> order``, first writer wins (a coid is an idempotency key)."""
    index: dict[str, dict] = {}
    for o in orders:
        coid = o.get("client_order_id")
        if coid and coid not in index:
            index[coid] = o
    return index


def _fills_by_order(fills: list) -> dict[str, list]:
    """``order_id -> fills``. Live fills carry ``order_id`` and no ``client_order_id``,
    which is why attribution is an orders->fills join in the first place."""
    index: dict[str, list] = {}
    for f in fills:
        oid = (getattr(f, "raw", None) or {}).get("order_id")
        if oid:
            index.setdefault(str(oid), []).append(f)
    return index


def _fills_for_bet(client, coid: str, coid_orders: dict, fills_index: dict) -> list:
    """This bet's fills: the shared index first, the client's own join as a fallback.

    The fallback is not a nicety. An index miss has innocent causes — an order created
    just before genesis, a fill the ``min_ts`` page filter dropped, a fake that scripts
    fills without an order behind them — and the consequence of believing a miss is a
    ``count_mismatch`` against a position that is in fact exact, i.e. an overnight HALT
    over nothing (the A-0054 shape). So a miss costs one extra request and asks the
    exchange directly; only an empty answer from THAT is treated as no fills.
    """
    order = coid_orders.get(coid)
    oid = order.get("order_id") if order else None
    if oid:
        fills = fills_index.get(str(oid))
        if fills:
            return fills
    return client.find_fills_by_client_order_id(coid)


def _load_canary(ledger) -> dict | None:
    raw = ledger.meta_get("canary")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --------------------------------------------------------------------------- the walk
def _real_bets(ledger) -> list[dict]:
    return ledger.conn.execute(
        "SELECT * FROM bets WHERE is_real=1 ORDER BY bet_id"
    ).fetchall()


def _walk(ledger, settings, bets: list[dict], genesis: datetime,
          genesis_balance: Decimal) -> tuple[Decimal, dict]:
    """Expected balance plus the JSON-able breakdown that lands in ``detail``."""
    debits: list[dict] = []
    credits: list[dict] = []

    for b in bets:
        if b["status"] in _EVER_FILLED and _in_window(b["placed_at"], genesis):
            stake = _stake_of(b)
            fee = q4(_dec(b["fee"]))
            debits.append({
                "bet_id": b["bet_id"], "ticker": b["ticker"], "status": b["status"],
                "stake": str(stake), "fee": str(fee), "placed_at": b["placed_at"],
            })
        if b["status"] in _TERMINAL and _in_window(b["settled_at"], genesis):
            outcome = b["outcome"]
            contracts = _dec(b["contracts"])
            if outcome == "win":
                payout = q4(contracts * _ONE)
            elif outcome == "void":
                payout = q4(_stake_of(b) + _dec(b["fee"]))
            elif outcome == _SCALAR:
                # docs/16 §5. A scalar settlement's payout is a value the exchange chose;
                # no formula over count/price reproduces it, so it is recovered from the
                # row settle wrote: ``pnl = payout − stake − fee`` inverted. The debit term
                # above already took stake + fee, so the two net to exactly this row's
                # ``pnl``, which is the definition of the settlement being booked right.
                payout = q4(_dec(b["pnl"]) + _stake_of(b) + _dec(b["fee"]))
            else:  # loss (or a terminal row with no outcome recorded yet)
                payout = _ZERO
            credits.append({
                "bet_id": b["bet_id"], "ticker": b["ticker"],
                "outcome": outcome, "payout": str(payout),
                "settled_at": b["settled_at"],
            })

    debit_total = q4(sum((D(d["stake"]) + D(d["fee"]) for d in debits), _ZERO))
    credit_total = q4(sum((D(c["payout"]) for c in credits), _ZERO))

    canary = _load_canary(ledger)
    canary_cost = _ZERO
    canary_payout = _ZERO
    canary_detail: dict = {"present": canary is not None}
    if canary is not None:
        # Exact, never int-truncated: ``canary.py`` writes the count as the exact string
        # the exchange reported, and a fractional canary fill is real money. ``int()``
        # here turned a 0.9-contract canary into a zero-cost one and would have shown up
        # as drift the walk could not explain (KC-2, same class as A-0054).
        contracts = _dec(canary.get("contracts"))
        fill_price = _dec(canary.get("fill_price"))
        fee = _dec(canary.get("fee"))
        cost_in = _in_window(canary.get("ts"), genesis)
        if cost_in:
            canary_cost = q4(contracts * fill_price + fee)
        settle_ts = canary.get("settled_at") or canary.get("ts")
        payout_in = bool(canary.get("settled")) and _in_window(settle_ts, genesis)
        if payout_in:
            canary_payout = q4(_dec(canary.get("payout")))
        canary_detail.update({
            "ticker": canary.get("ticker"), "ts": canary.get("ts"),
            "contracts": str(contracts), "fill_price": str(fill_price), "fee": str(fee),
            "settled": bool(canary.get("settled")), "settled_at": canary.get("settled_at"),
            "cost_included": cost_in, "cost": str(q4(canary_cost)),
            "payout_included": payout_in, "payout": str(q4(canary_payout)),
        })

    personal_cost, personal_fee, personal_payout, personal_detail = _personal_terms(
        ledger, genesis
    )
    netting_total, netting_detail = _netting_term(bets)
    credit_given, credit_given_detail = _exchange_credit_term(ledger, genesis)
    absorbed, absorbed_detail = _absorbed_term(ledger, genesis)

    expected = q4(
        genesis_balance - debit_total + credit_total - canary_cost + canary_payout
        - personal_cost - personal_fee + personal_payout + netting_total + credit_given
        + absorbed
    )
    breakdown = {
        "genesis": {"ts": iso(genesis), "balance": str(q4(genesis_balance))},
        "debits": {"n": len(debits), "total": str(debit_total), "bets": debits},
        "credits": {"n": len(credits), "total": str(credit_total), "bets": credits},
        "canary": canary_detail,
        "personal": personal_detail,
        "netting": netting_detail,
        # Not folded into "credits" above, which is the bet payouts term and is read by
        # name in three places. Money from the exchange that was never a bet gets its own
        # key rather than quietly changing what an existing one means.
        "exchange_credits": credit_given_detail,
        "absorbed_residual": absorbed_detail,
        "expected": str(expected),
    }
    return expected, breakdown


def _absorbed_runs(ledger, genesis: datetime) -> list[dict]:
    """Every stored reconciliation since genesis that is an absorption still in force.

    Read from the row's own ``detail`` JSON, where each run records its verdict, so the
    term needs no column of its own. A row whose detail will not parse was not absorbed:
    absorption is a claim the row has to make, never one this walk infers. A row dated
    before genesis belongs to an earlier era whose money is inside the anchor already.

    An absorbed run is out of force once a later ``reversed`` row names it in
    ``reversed_run_at``: the fix that explained its drift is in the walk now, so carrying
    the absorption as well would count the same money twice.
    """
    absorbed: list[dict] = []
    reversed_at: set[str] = set()
    for row in ledger.reconciliations():
        try:
            detail = json.loads(row["detail"] or "{}")
            verdict = detail.get("verdict")
        except (ValueError, TypeError, AttributeError):
            continue
        if not _in_window(row["run_at"], genesis):
            continue
        if verdict == _REVERSED and detail.get("reversed_run_at"):
            reversed_at.add(str(detail["reversed_run_at"]))
        elif verdict == _ABSORBED:
            absorbed.append({"run_at": row["run_at"], "drift": str(q4(_dec(row["drift"])))})
    return [r for r in absorbed if r["run_at"] not in reversed_at]


def _absorbed_term(ledger, genesis: datetime) -> tuple[Decimal, dict]:
    """The absorbed residual, as ``(total, detail)`` (Arno, 2026-09-27).

    The sum of the drift of every earlier night the verdict absorbed and no later night
    reversed. Once a drift is absorbed the walk carries it as money the account holds, so
    the next night reads exact unless something else has moved. Only stored rows count,
    and tonight's row is written after the walk, so the term is always the nights
    strictly before this run.

    Signed, because a drift is. When a later fix explains an absorbed drift exactly, that
    night reads the same amount with the opposite sign and is recorded as a reversal
    rather than a second absorption, so both drop out of the term and out of the watch.
    """
    runs = _absorbed_runs(ledger, genesis)
    total = q4(sum((D(r["drift"]) for r in runs), _ZERO))
    return total, {"n": len(runs), "total": str(total), "runs": runs}


def _reversal_target(ledger, genesis: datetime, drift: Decimal) -> dict | None:
    """The earlier absorbed run tonight's drift reverses, or ``None``.

    A match is one run still in force whose drift is exactly the negative of tonight's, to
    the $0.0001. Only single runs are matched, never a combination of them: the case this
    exists for is one fix explaining one night (a fee heal, a scalar correction, a credit
    recorded for an absorbed rebate), and anything more elaborate would be guessing. When
    several runs match, the latest one is taken. No match means tonight absorbs as usual.
    """
    target = q4(-drift)
    matches = [r for r in _absorbed_runs(ledger, genesis) if D(r["drift"]) == target]
    return matches[-1] if matches else None


def _exchange_credit_term(ledger, genesis: datetime) -> tuple[Decimal, dict]:
    """What the exchange gave the account, as ``(total, detail)`` (schema 010).

    Kalshi pays incentive credits into the balance: $0.01 on 2026-09-20 for volume on
    KXRAINDNYC-260919, and more whenever the account trades a market in one of these
    programmes. No API reports them, so they are entered by hand with ``betting-agent
    credit`` from the app's Account, Activity, Credits list, and without this term every
    one of them is a cent of unexplained drift on the night it lands.

    Positive, always: a credit only ever adds. The genesis window is the DAO's, for the
    reason it is everywhere else here, and a credit dated before the anchor is already
    inside the genesis balance.
    """
    rows = ledger.credits_since(iso(genesis))
    entries = [
        {"credit_id": r["credit_id"], "credited_at": r["credited_at"],
         "amount": str(q4(_dec(r["amount"]))), "kind": r["kind"], "reason": r["reason"]}
        for r in rows
    ]
    total = q4(sum((D(e["amount"]) for e in entries), _ZERO))
    return total, {"n": len(entries), "total": str(total), "credits": entries}


def _netting_term(bets: list[dict]) -> tuple[Decimal, dict]:
    """Matched opposite-side contracts the exchange has already paid for (docs/26).

    Kalshi will not let one account hold YES and NO on the same market: when a fill lands
    opposite a position we already hold, it NETS the matched contracts and credits their
    guaranteed $1.00 each AT THAT FILL. Our ledger books each leg as a position held to
    resolution, so the walk credits nothing until settlement. The totals always agree; only
    the timing differs, and the gap is real money sitting in the account.

    That gap halted the live system twice. On 2026-08-16 three pairs put $3.00 in front of
    a reconciliation with no way to name it, and the answer then was an attribution gate:
    the night was DEFERRED rather than explained. Sizing brought it back at three contracts
    a leg. On 2026-09-20 two markets held on both sides by different attempts put $6.00 in
    front of the walk and it halted, and again the next night at +$6.0068. A deferral was
    the right emergency answer and is the wrong standing one: this is not money the walk
    cannot see, it is money the walk was not adding up.

    So it is a term. ``matched = min(yes, no)`` over the still-``filled`` real legs of a
    ticker, times $1.00, exactly the figure :func:`_netted_pairs` already computes for the
    gate. When the legs settle they leave the ``filled`` population, the term disappears,
    and their payouts land as ordinary credits in the same pass: on KXTSAW-26SEP20-A2.40 a
    +$3.00 term became two winning legs paying $6.00 against an exchange settlement of
    $3.00, and on KXOPENSHARE-26SEP21-18 a +$3.00 term became one winning leg paying $3.00
    against a settlement of $0.00. Both arrive at the same place from either side.

    Same population as the gate's, with no genesis window, because a leg that is still
    ``filled`` has had no payout booked by this walk at all and the netting credit is the
    only money the exchange has paid on it. The one shape that would double-count is a pair
    netted BEFORE genesis whose legs are both still open today, which would need a market
    that never finalized across the era boundary; it would show as drift rather than as a
    silent error.
    """
    pairs = _netted_pairs(bets)
    total = q4(sum((p["prepaid"] for p in pairs.values()), _ZERO))
    # Stringified here, as every other breakdown term is: this dict is JSON-serialized
    # into the ``reconciliations`` row, and ``_netted_pairs`` hands its money back as
    # Decimals for the gate's own arithmetic.
    detail = [
        {
            "ticker": ticker,
            "matched_contracts": str(p["matched_contracts"]),
            "prepaid": str(p["prepaid"]),
            "yes_contracts": str(p["yes_contracts"]),
            "no_contracts": str(p["no_contracts"]),
            "yes_bets": p["yes_bets"], "no_bets": p["no_bets"],
        }
        for ticker, p in ((t, pairs[t]) for t in sorted(pairs))
    ]
    return total, {"n": len(pairs), "total": str(total), "pairs": detail}


def _personal_terms(ledger, genesis: datetime) -> tuple[Decimal, Decimal, Decimal, dict]:
    """Trading outside the harness, as ``(cost, fee, payout, detail)`` (docs/22 section 7.3).

    An order placed on the account outside the harness is a real debit this walk used to
    have no term for: one such order on 2026-08-30 sat as $0.9991 of
    unexplained drift, halted the system, and blocked a deposit that could not be verified
    against a walk which did not balance. ``settle.py``'s shared-account scan already
    classified every such order; it now writes them down, and this is where they are spent.

    ``cost`` is contracts times price over the order's fills and does NOT include the fee,
    which is its own column, so the term subtracts BOTH, exactly once each, the same shape
    the bets debit uses (``stake + fee``). ``payout`` is what the exchange paid when the
    market settled.

    An order on a ticker the harness has ALSO traded is excluded from the arithmetic and
    reported on its own. Position netting makes such an order's payout inseparable from
    ours (the exchange pays the matched pair at the later fill, against the account, not
    against an order), so a number here would be a guess. It is named in the breakdown
    instead, and if it leaves a drift, the verdict bands are what handle it.
    """
    cost = _ZERO
    fee = _ZERO
    payout = _ZERO
    orders: list[dict] = []
    excluded: list[dict] = []
    for row in ledger.personal_orders():
        # Cost and payout are windowed separately, exactly as the canary's two terms are:
        # an order placed before genesis spent money this walk does not own, but if it
        # settles AFTER genesis the exchange credits the account inside the window and the
        # walk has to see that credit.
        cost_in = _in_window(row["created_time"], genesis)
        payout_in = bool(row["settled_at"]) and _in_window(row["settled_at"], genesis)
        if not cost_in and not payout_in:
            continue
        entry = {
            "order_id": row["order_id"], "ticker": row["ticker"], "side": row["side"],
            "created_time": row["created_time"], "contracts": row["contracts"],
            "cost": row["cost"], "fee": row["fee"], "fee_source": row["fee_source"],
            "settled_at": row["settled_at"], "payout": row["payout"],
            "cost_included": cost_in, "payout_included": payout_in,
        }
        if row["on_harness_ticker"]:
            excluded.append(entry)
            continue
        orders.append(entry)
        if cost_in:
            cost += _dec(row["cost"])
            fee += _dec(row["fee"])
        if payout_in:
            payout += _dec(row["payout"])
    detail = {
        "n": len(orders), "cost": str(q4(cost)), "fee": str(q4(fee)),
        "payout": str(q4(payout)), "orders": orders,
        "n_on_harness_ticker": len(excluded), "on_harness_ticker": excluded,
    }
    return q4(cost), q4(fee), q4(payout), detail


# --------------------------------------------------------------------------- cross-checks
def _check_fills_match(client, settings, bets: list[dict], genesis: datetime, *,
                       coid_orders: dict, fills_index: dict,
                       watermark: datetime | None) -> dict:
    """Our filled real bets vs the exchange fills reached through the coid join (§8.4).

    Count must match exactly; the count-weighted fill price must land within $0.01, and
    the fee within ``_FEE_TOLERANCE``. The fee compared is what the exchange charged: the
    sum of the fills' own ``fee_cost`` whenever every fill carries one (seen live since
    2026-09-27, summing to the order listing's fee on every leg checked). Only when a fill
    lacks it is the fee recomputed from the §8 model at the average price. The model at
    the average is not what the exchange charges for an order that fills at several
    prices: A-0391-B04 filled 3 NO at 0.75, 0.79, 0.55 and 0.55 for $0.0475 in fees, the
    model at the 0.6194 average says $0.0496, and that $0.0021 failed this check on a
    walk that was exact and HALTed the system on 2026-10-02.

    "Exactly" is ``Decimal`` equality, not integer equality: an order can fill in
    fractional pieces (A-0054-B01 came back as 0.28 + 0.34 + 0.38 = 1.00 on 2026-08-02),
    and int-truncating each piece summed them to 0 and HALTed the system over a
    count_mismatch against a position that was in fact exact (KC-2).

    Two things bound the cost (EF-1). The fills come from the caller's shared indexes
    rather than a per-bet pagination, and ``watermark`` skips every bet a prior clean run
    already verified — a settled position's fills are immutable, so re-proving them
    nightly forever buys nothing. ``n_bets``/``n_skipped`` report both populations so the
    skip is visible wherever this check is; ``verified_through`` is the newest
    ``placed_at`` this run stands behind, which the caller stamps only if the whole run
    came back clean.

    MP-6: the count-weighted average price divides by the fills that actually carry a
    price, not by every fill — a priceless fill carries no price information, and
    dividing by the full count silently understated the average whenever one was
    missing. ``priceless`` in the return value carries one entry per affected bet
    (``bet_id``, ``total_count``, ``priced_count``) for the caller to audit; when ALL of
    a bet's fills are priceless there is nothing to average, so the recorded price is
    kept rather than dividing by zero (mirrors ``settle._reconcile_real``'s convention).
    """
    problems: list[dict] = []
    priceless: list[dict] = []
    checked = 0
    skipped = 0
    verified_through: datetime | None = None
    # Stamped as the row's OWN ``placed_at`` string, never a reformatted one: ``iso()``
    # rounds down to whole seconds, and a watermark that rounds backwards can never cover
    # the bet it was derived from — that bet would be re-verified on every run forever.
    verified_raw: str | None = None

    def _cover(placed: datetime | None, raw) -> None:
        nonlocal verified_through, verified_raw
        if placed is not None and (verified_through is None or placed > verified_through):
            verified_through, verified_raw = placed, str(raw)

    for b in bets:
        if b["status"] not in _EVER_FILLED or not _in_window(b["placed_at"], genesis):
            continue
        placed = _ts_or_none(b["placed_at"])
        if watermark is not None and placed is not None and placed <= watermark:
            skipped += 1
            _cover(placed, b["placed_at"])
            continue
        coid = b["client_order_id"]
        if not coid:
            problems.append({"bet_id": b["bet_id"], "problem": "missing_client_order_id"})
            continue
        checked += 1
        _cover(placed, b["placed_at"])
        fills = _fills_for_bet(client, coid, coid_orders, fills_index)
        actual_count = sum((D(str(f.count)) for f in fills), _ZERO)
        recorded_count = _dec(b["contracts"])
        if actual_count != recorded_count:
            problems.append({
                "bet_id": b["bet_id"], "problem": "count_mismatch",
                "recorded": str(recorded_count), "actual": str(actual_count),
            })
            continue
        if actual_count == 0:
            continue
        priced = [f for f in fills if f.price is not None]
        priced_count = sum((D(str(f.count)) for f in priced), _ZERO)
        recorded_price = _dec(b["fill_price"])
        if priced_count != actual_count:
            priceless.append({
                "bet_id": b["bet_id"], "total_count": str(actual_count),
                "priced_count": str(priced_count),
            })
        if priced_count > 0:
            notional = sum((_dec(f.price) * D(str(f.count)) for f in priced), _ZERO)
            actual_price = q4(notional / priced_count)
        else:
            actual_price = recorded_price  # every fill priceless: nothing to average
        if abs(actual_price - recorded_price) > _CENT:
            problems.append({
                "bet_id": b["bet_id"], "problem": "price_mismatch",
                "recorded": str(recorded_price), "actual": str(actual_price),
            })
        # A voided bet's fee was deliberately zeroed by the void refund (settle.py),
        # so the fills-implied fee is not comparable — see the module docstring.
        if b["status"] == "voided":
            continue
        if all(f.fee is not None for f in fills):
            actual_fee, fee_source = q4(sum((f.fee for f in fills), _ZERO)), "fills"
        else:
            actual_fee = calc_fee(actual_count, actual_price,
                                  _coef_for(settings, b["category"]))
            fee_source = "model"
        recorded_fee = _dec(b["fee"])
        if abs(actual_fee - recorded_fee) > _FEE_TOLERANCE:
            problems.append({
                "bet_id": b["bet_id"], "problem": "fee_mismatch",
                "recorded": str(recorded_fee), "actual": str(actual_fee),
                "source": fee_source,
            })
    return {
        "ok": not problems, "n_bets": checked, "n_skipped": skipped,
        "watermark": iso(watermark) if watermark is not None else None,
        "verified_through": verified_raw,
        "problems": problems,
        "priceless": priceless,
    }


def _check_settlements_covered(ledger, bets: list[dict], settlements: list) -> dict:
    """Every post-genesis exchange settlement must map to a position we recorded (§8.4).

    Covered tickers are our terminal real bets' tickers, the canary's, and every ticker in
    ``personal_orders``. The canary is deliberately outside ``bets`` and outside orders
    were never in it, so each of their settlements is accounted for somewhere in
    this walk rather than being an orphan.

    ``settlements`` is paged once by the caller: the attribution gate below needs the very
    same list this check judged, so that "attributable to a settlement we already fetched"
    is a statement about one snapshot rather than two.
    """
    covered = {b["ticker"] for b in bets if b["status"] in _TERMINAL}
    canary = _load_canary(ledger)
    if canary and canary.get("ticker"):
        covered.add(canary["ticker"])
    covered |= ledger.personal_order_tickers()

    uncovered: list[dict] = []
    for s in settlements:
        if s.ticker not in covered:
            uncovered.append({
                "ticker": s.ticker, "market_result": s.market_result,
                "ts": iso(s.ts) if s.ts is not None else None,
            })
    return {"ok": not uncovered, "n_settlements": len(settlements), "uncovered": uncovered}


# --------------------------------------------------------------------------- attribution
def _settlement_results(
    settlements: list,
) -> tuple[dict[str, str | None], set[str], dict[str, object]]:
    """``({ticker: verdict}, ambiguous_tickers, {ticker: settlement})`` over the fetch.

    The verdict is ``"yes"``/``"no"``, ``"scalar"``, or ``None`` for a void — the same
    three-way mapping ``settle.py._market_verdict`` applies, so the gate below predicts
    exactly what the settle pass will write. A ticker that settled twice with different
    verdicts is ambiguous and is never attributed — guessing which outcome the exchange
    paid is exactly the guess a tripwire must refuse. (Unchanged rule: ambiguity is about
    the verdict. The record map keeps the last row per ticker, matching ``results``.)

    The records themselves come back because two callers need the money on them rather
    than only the verdict: a scalar's payout is its ``revenue`` and nothing else can
    reconstruct it, and a netted ticker's settlement ``revenue`` is what the exchange
    actually paid AFTER netting.
    """
    results: dict[str, str | None] = {}
    ambiguous: set[str] = set()
    records: dict[str, object] = {}
    for s in settlements:
        raw = (s.market_result or "").strip().lower()
        result = raw if raw in ("yes", "no", _SCALAR) else None
        if s.ticker in results and results[s.ticker] != result:
            ambiguous.add(s.ticker)
        results[s.ticker] = result
        records[s.ticker] = s
    return results, ambiguous, records


def _implied_payout(bet: dict, result: str | None,
                    record=None) -> tuple[str, Decimal] | None:
    """``(outcome, payout)`` the settle pass will record — the exchange's own credit.

    Mirrors ``settle.py`` term for term: a win pays ``contracts x $1``, a loss pays
    nothing, a void refunds ``stake + fee`` (``settle.py`` zeroes the fee on a void, so the
    refunded fee is money the walk will stop debiting), and a **scalar** pays the
    settlement's ``revenue`` for our position with the fee RETAINED — so the void's
    ``+ fee`` refund term explicitly does not apply to it (docs/16 §5).

    ``None`` — "this settlement cannot be turned into a number" — when a scalar record
    cannot attribute a payout to this position (see ``Settlement.payout_for``). The caller
    must then decline to attribute the whole run, because an all-or-nothing gate with a
    term it cannot compute is not total any more.
    """
    if result == _SCALAR:
        payout = record.payout_for(bet["side"], _dec(bet["contracts"])) \
            if record is not None else None
        return None if payout is None else (_SCALAR, q4(payout))
    if result is None:
        return "void", q4(_stake_of(bet) + _dec(bet["fee"]))
    if bet["side"] == result:
        return "win", q4(_dec(bet["contracts"]) * _ONE)
    return "loss", _ZERO


def _netted_pairs(bets: list[dict]) -> dict[str, dict]:
    """``{ticker: {...}}`` for every market where we hold BOTH sides open (docs/16 §5).

    Kalshi does not let one account hold YES and NO on the same market. When an order
    fills on the opposite side of an existing position the exchange NETS the matched
    contracts and credits the matched pair's guaranteed $1.00 each **at that fill**, not at
    settlement. Its later settlement record for such a market reports the NET position
    only, so the matched pair shows up there as ``revenue`` the walk cannot find.

    Our ledger books each leg as a position held to settlement — one leg will win $1, the
    other will lose — which is right in total and early in timing. That gap is money the
    account genuinely holds and the walk genuinely has not credited yet, and on 2026-08-16
    three such pairs put +$3.00 of it in front of a reconciliation that had no way to name
    it, and HALTed the live system overnight.

    So they are named here. ``matched = min(yes_contracts, no_contracts)`` over the
    still-``filled`` real legs of a ticker; ``prepaid = matched x $1.00``. Same-side legs
    beyond the match are untouched — they are an ordinary open position (the Aug-15
    KXTRUTHSOCIAL-B189 shape: 1 NO against 3 YES is one matched pair plus a live 2-YES
    position, and only the pair was pre-paid).

    Deliberately keyed on ``filled`` legs only: once settle books a leg, its payout becomes
    an ordinary walk credit and the timing gap it created is closed.
    """
    by_ticker: dict[str, dict] = {}
    for b in bets:
        if b["status"] != "filled":
            continue
        side = b["side"]
        if side not in ("yes", "no"):
            continue
        entry = by_ticker.setdefault(
            b["ticker"], {"yes": _ZERO, "no": _ZERO, "legs": {"yes": [], "no": []}}
        )
        entry[side] += _dec(b["contracts"])
        entry["legs"][side].append(b["bet_id"])

    pairs: dict[str, dict] = {}
    for ticker, e in by_ticker.items():
        matched = min(e["yes"], e["no"])
        if matched <= _ZERO:
            continue
        pairs[ticker] = {
            "ticker": ticker,
            "matched_contracts": q4(matched),
            "prepaid": q4(matched * _ONE),
            "yes_contracts": q4(e["yes"]), "no_contracts": q4(e["no"]),
            "yes_bets": sorted(e["legs"]["yes"]), "no_bets": sorted(e["legs"]["no"]),
        }
    return pairs


def _implied_canary_payout(canary: dict, result: str | None) -> tuple[str, Decimal]:
    """The same three terms for the canary, mirroring ``settle.py._settle_canary``.

    The canary's cost is already a walk term (``- canary_cost``) and its payout becomes one
    (``+ canary_payout``) the moment settle stamps it, so the drift a late canary
    settlement creates has exactly the shape a late bet's does.
    """
    contracts = _dec(canary.get("contracts"))
    if result is None:
        refund = q4(contracts * _dec(canary.get("fill_price")) + _dec(canary.get("fee")))
        return "void", refund
    if canary.get("side") == result:
        return "win", q4(contracts * _ONE)
    return "loss", _ZERO


def _pending_canary(canary: dict | None, results: dict, ambiguous: set) -> dict | None:
    """The canary when it is a settle-pending position, else ``None`` (D1, extended).

    The decision text says "still-``filled`` real bets", which under-enumerates: the canary
    is deliberately outside ``bets`` (it carries a non-matching ``client_order_id``) and is
    the one non-bet position the balance walk owns. A canary market that finalizes between
    the settle pass and the balance read produces the identical late-payout drift, so it
    belongs in the same attributable population — and, like everything else here, the
    all-or-nothing rule applies: it must account for its money exactly or the run HALTs.
    """
    if not isinstance(canary, dict) or canary.get("settled"):
        return None
    ticker = canary.get("ticker")
    if not ticker or ticker not in results or ticker in ambiguous:
        return None
    return canary


def _attribute_pending_settlements(bets: list[dict], canary: dict | None,
                                   settlements: list, checks: dict,
                                   drift: Decimal) -> dict | None:
    """The D1 attribution gate: the detail dict when this run is merely EARLY, else ``None``.

    Spec amendment (owner decision D1, 2026-08-03; amends L8 step 6). L8 says any nonzero
    drift HALTs. That is right when the money is unexplained and wrong when it is merely
    *not yet written down*: a market finalizes at 22:50, the exchange credits us, the
    22:45 settle pass had already run (or failed on a transient error), and the 23:00
    reconciliation sees a credit with no matching ledger row. Halting there stops the
    experiment overnight over bookkeeping that the next tick's settle pass closes on its
    own.

    So the run is classified **provisional** — deferred, not halted — under a gate that is
    deliberately total. EVERY one of these must hold:

    * a non-empty pending population: still-``filled`` real bets whose tickers appear in
      the settlements we just fetched, plus an unsettled ``meta.canary``, plus (docs/16
      §5) the legs of any **netted pair**, whose money the exchange has already paid;
    * every uncovered settlement ticker belongs to that population;
    * every ``fills_match`` problem belongs to a bet in that population (settle's own
      ``_reconcile_real`` overwrite is what would fix it); a problem anywhere else is
      unrelated to the lag and HALTs;
    * the drift equals, to the cent, the money those positions imply.

    Any residual at all, an extra ticker or an unattributed cent, returns ``None`` and the
    caller HALTs exactly as before. The three attributions compose: bets,
    the canary and netted pairs are summed into ONE ``attributed`` total that must match
    the drift exactly. Widening the population never weakens the gate, because the gate is
    the equality, not the population.

    **Netting and the double-count guard** (docs/16 §5). A netted pair is money the
    exchange credited at the LATER FILL, so it is attributable whether or not the market
    has settled — which is the whole point, since the settlements list has no record for a
    market that has not settled yet, and that is what the pre-netting gate could not
    absorb. When a netted ticker HAS also settled, both terms are still real and neither
    may be inferred twice: the exchange's settlement ``revenue`` already reflects the
    netting (the three Aug-15 pairs settled at ``revenue: 0``), so a netted ticker is
    attributed as ``prepaid + the record's actual revenue`` and its legs are NOT run
    through the per-bet ``$1-per-winning-contract`` inference, which would double-count the
    matched dollar.
    """
    results, ambiguous, records = _settlement_results(settlements)
    netted = _netted_pairs(bets)
    # A netted ticker whose settlement is ambiguous is refused like any other: two records
    # that disagree cannot say what was paid, and this term is money.
    netted = {t: p for t, p in netted.items() if t not in ambiguous}
    settled_pending = [
        b for b in bets
        if b["status"] == "filled"
        and b["ticker"] in results
        and b["ticker"] not in ambiguous
        and b["ticker"] not in netted
    ]
    netted_legs = [b for b in bets if b["status"] == "filled" and b["ticker"] in netted]
    pending_canary = _pending_canary(canary, results, ambiguous)
    if not settled_pending and not netted and pending_canary is None:
        return None

    attributed_tickers = {b["ticker"] for b in settled_pending} | set(netted)
    if pending_canary is not None:
        # In practice unreachable — `_check_settlements_covered` already counts the canary's
        # ticker as covered — but the population and the cover set must not disagree.
        attributed_tickers.add(pending_canary["ticker"])
    for u in checks["settlements_covered"]["uncovered"]:
        if u["ticker"] not in attributed_tickers:
            return None
    pending_ids = {b["bet_id"] for b in settled_pending} | {b["bet_id"] for b in netted_legs}
    for p in checks["fills_match"]["problems"]:
        if p.get("bet_id") not in pending_ids:
            return None

    legs: list[dict] = []
    attributed = _ZERO
    for b in settled_pending:
        implied = _implied_payout(b, results[b["ticker"]], records.get(b["ticker"]))
        if implied is None:
            return None  # a settlement whose payout cannot be computed: not "merely early"
        outcome, payout = implied
        attributed += payout
        legs.append({
            "bet_id": b["bet_id"], "ticker": b["ticker"], "side": b["side"],
            "market_result": results[b["ticker"]] or "void",
            "implied_outcome": outcome, "implied_payout": str(payout),
        })

    netted_detail: list[dict] = []
    netted_prepaid = _ZERO
    for ticker in sorted(netted):
        source = netted[ticker]
        # NOT added to ``attributed`` since docs/26: the walk carries the matched dollars
        # itself now, so they are no longer part of the drift this gate has to explain.
        # Everything else about this branch is still load-bearing: a netted ticker's legs
        # stay out of the per-leg $1 inference below, its settlement counts as covered, and
        # a settled one still contributes the exchange's own residual revenue.
        netted_prepaid += source["prepaid"]
        pair = {
            "ticker": ticker,
            "matched_contracts": str(source["matched_contracts"]),
            "prepaid": str(source["prepaid"]),
            "yes_contracts": str(source["yes_contracts"]),
            "no_contracts": str(source["no_contracts"]),
            "yes_bets": source["yes_bets"], "no_bets": source["no_bets"],
            "settled": ticker in results,
        }
        if ticker in results:
            record = records.get(ticker)
            revenue = getattr(record, "revenue", None)
            if revenue is None:
                # The market settled but the record carries no revenue: the exchange's own
                # figure is the only thing that can price a netted position, so refuse.
                return None
            attributed += q4(revenue)
            pair["market_result"] = results[ticker] or "void"
            pair["settlement_revenue"] = str(q4(revenue))
        pair["note"] = (
            "matched contracts were paid $1.00 each at the later fill (exchange netting); "
            + ("the settlement's own revenue covers whatever net position remained"
               if ticker in results else
               "the market has not settled yet, so there is no settlement record to find")
        )
        netted_detail.append(pair)

    canary_detail = None
    if pending_canary is not None:
        ticker = pending_canary["ticker"]
        outcome, payout = _implied_canary_payout(pending_canary, results[ticker])
        attributed += payout
        canary_detail = {
            "ticker": ticker, "side": pending_canary.get("side"),
            "market_result": results[ticker] or "void",
            "implied_outcome": outcome, "implied_payout": str(payout),
        }
    attributed = q4(attributed)
    if q4(drift) != attributed:
        return None

    reasons = ["settle-pending positions (bets and/or the canary)"] if (
        settled_pending or pending_canary is not None) else []
    if netted_detail:
        reasons.append(f"the settled residual of {len(netted_detail)} netted "
                       "opposite-side pair(s), whose matched dollars the walk already "
                       "carries")
    return {
        "drift": str(q4(drift)),
        "attributed": str(attributed),
        "n_pending": len(settled_pending) + (1 if pending_canary is not None else 0),
        "pending": legs,
        "canary": canary_detail,
        "n_netted": len(netted_detail),
        "netted": netted_detail,
        "netted_prepaid": str(q4(netted_prepaid)),
        "uncovered": [u["ticker"] for u in checks["settlements_covered"]["uncovered"]],
        "reason": "every uncovered settlement and the whole drift are " + " plus ".join(
            reasons or ["attributable positions"]
        ),
    }


# --------------------------------------------------------------------------- adjustments
# Owner deposits and withdrawals (docs/14 B2). A cent of tolerance: the walk and the
# exchange both speak 4dp and agree to the cent on every run this system has ever had, so
# a larger gap does not mean rounding — it means the two records disagree about something
# other than the transfer being recorded, and that is exactly what must not be absorbed
# into the anchor. This is the one guard standing between a typo'd amount and a genesis
# balance that quietly explains away real drift.
# Its own constant, deliberately not ``_CENT``: that one is the fills check's price/fee
# tolerance, and two unrelated quantities sharing a literal is how one of them silently
# moves when the other is retuned.
_ADJUSTMENT_TOLERANCE = q4(D("0.01"))

_DIRECTIONS = {"deposit": _ONE, "withdrawal": -_ONE}
_ADJUSTMENT_EVENT = {"deposit": "deposit_recorded", "withdrawal": "withdrawal_recorded"}


def expected_balance(ledger, settings) -> dict | None:
    """The ledger-expected live balance and the walk behind it, with no exchange call.

    Literally :func:`reconcile_once`'s own ``_walk`` — one implementation, so a deposit is
    verified against exactly the arithmetic tonight's reconciliation will then apply to it.
    ``None`` in the paper era (no ``live_genesis_ts``): no anchor to walk from, nothing to
    adjust.
    """
    genesis_iso = ledger.meta_get("live_genesis_ts")
    if not genesis_iso:
        return None
    genesis = parse_iso(str(genesis_iso))
    genesis_balance = _dec(ledger.meta_get("live_genesis_balance"))
    expected, breakdown = _walk(ledger, settings, _real_bets(ledger), genesis,
                                genesis_balance)
    return {
        "expected": expected, "genesis_ts": str(genesis_iso),
        "genesis_balance": q4(genesis_balance), "breakdown": breakdown,
    }


def _flows(breakdown: dict) -> dict:
    """The walk's between-anchor-and-now terms, flattened for the audit detail.

    The personal terms are here for the reason every other term is: the derivation below
    has to be arithmetic a person can re-do by hand and reach the same answer, and a walk
    that subtracts outside orders while the record of it does not mention them is
    a record that will not add up for whoever checks it.
    """
    canary = breakdown["canary"]
    personal = breakdown.get("personal") or {}
    given = breakdown.get("exchange_credits") or {}
    return {
        "debits_n": breakdown["debits"]["n"],
        "stakes_and_fees": breakdown["debits"]["total"],
        "credits_n": breakdown["credits"]["n"],
        "payouts": breakdown["credits"]["total"],
        "canary_cost": canary.get("cost", "0.0000") if canary.get("present") else "0.0000",
        "canary_payout": canary.get("payout", "0.0000") if canary.get("present") else "0.0000",
        "personal_n": personal.get("n", 0),
        "personal_cost_and_fees": str(
            q4(_dec(personal.get("cost")) + _dec(personal.get("fee")))
        ),
        "personal_payout": str(q4(_dec(personal.get("payout")))),
        "exchange_credits_n": given.get("n", 0),
        "exchange_credits": str(q4(_dec(given.get("total")))),
        "absorbed_n": (breakdown.get("absorbed_residual") or {}).get("n", 0),
        "absorbed_residual": str(q4(_dec(
            (breakdown.get("absorbed_residual") or {}).get("total")
        ))),
    }


def _derivation(walk: dict, flows: dict, expected: Decimal, actual: Decimal,
                implied: Decimal, direction: str) -> str:
    """The one-line derivation, in the walk's own terms — the string the audit row keeps.

    Deliberately reads as arithmetic a human can re-do by hand against the ledger: the
    2026-08-10 deposit was verified exactly this way before there was a command to do it,
    and the paste-able record of that check is what made it auditable afterwards.
    """
    parts = [
        f"anchor {walk['genesis_balance']} (genesis {walk['genesis_ts']})",
        f"- {flows['stakes_and_fees']} stakes+fees since",
        f"+ {flows['payouts']} payouts since",
    ]
    if flows["canary_cost"] != "0.0000" or flows["canary_payout"] != "0.0000":
        parts.append(f"- {flows['canary_cost']} canary cost")
        parts.append(f"+ {flows['canary_payout']} canary payout")
    if flows["personal_n"]:
        parts.append(
            f"- {flows['personal_cost_and_fees']} personal orders "
            f"({flows['personal_n']}, cost+fees)"
        )
        parts.append(f"+ {flows['personal_payout']} personal payouts")
    if flows["exchange_credits_n"]:
        parts.append(
            f"+ {flows['exchange_credits']} exchange credits "
            f"({flows['exchange_credits_n']})"
        )
    line = (
        " ".join(parts)
        + f" = {expected} expected; actual {actual}; "
        + f"implied {direction} {abs(implied)}"
    )
    # Named but not counted: a transfer is verified against the walk WITHOUT the absorbed
    # residual (see record_balance_adjustment), so the arithmetic above leaves it out and
    # this says so, which keeps the line something a person can re-do by hand.
    if flows["absorbed_n"]:
        line += (f"; absorbed residual {flows['absorbed_residual']} over "
                 f"{flows['absorbed_n']} night(s) not counted")
    return line


def _decisions_line(direction: str, amount: Decimal, old: Decimal, new: Decimal,
                    actual: Decimal, derivation: str, day: str) -> str:
    """The decisions.md entry to paste — the shape the 2026-08-10 hand-record set.

    Prose money is 2dp (docs/decisions.md's own register) while the anchors and the
    derivation stay 4dp: the entry is a narrative whose evidence has to be exact.
    """
    verb = "Deposited" if direction == "deposit" else "Withdrew"
    stated = amount.quantize(_CENT)
    return "\n".join([
        f"## {day} — ${stated} {direction} recorded",
        "",
        f"{verb} ${stated}. Mechanics: `meta.live_genesis_balance` {old} -> {new} plus a",
        f"`{_ADJUSTMENT_EVENT[direction]}` audit_log event carrying the derivation:",
        f"{derivation}.",
        f"Exchange balance after the {direction}: ${actual}. `live_genesis_ts` unchanged "
        "(the era",
        "boundary is a timestamp, not a balance).",
    ])


def record_balance_adjustment(ledger, client, settings, *, direction: str,
                              amount: Decimal, now: datetime | None = None) -> dict:
    """Record an owner deposit/withdrawal against the balance walk's anchor (docs/14 B2).

    Formalizes what was done by hand on 2026-08-10, when $20 went in and the anchor was
    edited minutes before the nightly reconcile would have HALTed on an unexplained credit.
    The mechanism is small because the walk already has the right shape: the genesis
    balance is the anchor every subsequent expectation is measured from, so moving the
    anchor by exactly the transferred amount leaves every past and future flow intact —
    which is why a mid-stream deposit reconciles to the cent both before and after.

    **What makes it safe is the verification, not the arithmetic.** The implied transfer is
    ``exchange balance − ledger expectation``; if that does not match the stated amount to
    within :data:`_ADJUSTMENT_TOLERANCE`, the account holds an amount this ledger cannot
    explain and the command **writes nothing**. That is the case where a hand edit would
    have folded real drift into the anchor and permanently destroyed the evidence — so a
    disagreement is reported and refused, never reconciled by fiat.

    ``live_genesis_ts`` is never touched: the era boundary is a timestamp, and a deposit is
    not a new era.

    **The absorbed residual is not counted** (2026-09-27). The nightly walk carries every
    absorbed drift as money the account holds, but that is trust, not an explanation, and
    a deposit is where an unexplained amount would become permanent. So ``expected`` here
    is the walk without that term, the term is printed beside the result either way, and a
    refusal caused by it (reason ``absorbed_residual``) says so and points at
    ``betting-agent credit`` for a known exchange item.

    Returns a result dict; ``ok`` is the caller's exit status. Never writes on ``not ok``.
    """
    sign = _DIRECTIONS.get(direction)
    if sign is None:
        raise ValueError(f"direction must be 'deposit' or 'withdrawal', got {direction!r}")
    amount = q4(D(amount))
    now = now if now is not None else utc_now()

    if amount <= _ZERO:
        return {
            "ok": False, "reason": "non_positive_amount", "direction": direction,
            "amount": amount,
            "message": (f"refusing a {direction} of {amount}: the amount must be "
                        "positive (direction is the command, not the sign)"),
        }

    walk = expected_balance(ledger, settings)
    if walk is None:
        return {
            "ok": False, "reason": "paper_era", "direction": direction, "amount": amount,
            "message": (f"refusing: no live era yet (meta.live_genesis_ts is unset), so "
                        f"there is no balance anchor for a {direction} to move"),
        }

    # The walk WITHOUT the absorbed residual. An absorbed drift is money the nightly walk
    # carries on trust; letting it into this check would let an unexplained amount under
    # fifty cents pass straight into the anchor, which is the one thing this guard exists
    # to stop. So a standing absorbed amount has to be explained (usually recorded as a
    # credit) before a transfer can be verified past it.
    absorbed_detail = walk["breakdown"].get("absorbed_residual") or {}
    absorbed = q4(_dec(absorbed_detail.get("total")))
    absorbed_n = int(absorbed_detail.get("n", 0))
    expected = q4(walk["expected"] - absorbed)
    actual = q4(_dec(client.get_balance().dollars))
    implied = q4(actual - expected)
    stated = q4(sign * amount)
    disagreement = q4(abs(implied - stated))
    flows = _flows(walk["breakdown"])
    derivation = _derivation(walk, flows, expected, actual, implied, direction)
    absorbed_line = (f"absorbed residual in the walk: {absorbed} over {absorbed_n} "
                     "night(s), not counted in the implied amount")

    old_genesis = walk["genesis_balance"]
    new_genesis = q4(old_genesis + stated)
    detail = {
        "amount": str(amount),
        "direction": direction,
        "old_genesis_balance": str(old_genesis),
        "new_genesis_balance": str(new_genesis),
        "exchange_balance_verified": str(actual),
        "ledger_expected_balance": str(expected),
        "absorbed_residual": str(absorbed),
        "absorbed_n": absorbed_n,
        "implied": str(implied),
        "stated": str(stated),
        "disagreement": str(disagreement),
        "tolerance": str(_ADJUSTMENT_TOLERANCE),
        "anchor": {"ts": walk["genesis_ts"], "balance": str(old_genesis)},
        "flows": flows,
        "derivation": derivation,
        "recorded_at": iso(now),
        # Present because the hand-recorded 2026-08-10 row carries it and event history
        # should stay uniformly queryable. Running the command IS the authorization — it is
        # an owner keystroke, never reachable from the tick.
        "authorized_by": f"owner (betting-agent {direction})",
    }

    if disagreement > _ADJUSTMENT_TOLERANCE:
        # Is the absorbed residual what stands between the two? Then say so by name,
        # because the fix is different: the amount is right and the walk is carrying
        # something nobody has written down yet.
        by_absorbed = (absorbed != _ZERO
                       and q4(abs(implied - stated - absorbed)) <= _ADJUSTMENT_TOLERANCE)
        if by_absorbed:
            why = (
                f"the difference is the walk's absorbed residual of {absorbed} over "
                f"{absorbed_n} night(s), which a {direction} is never verified against. "
                "If it is a known exchange item, such as an incentive credit in the "
                "app's Account, Activity, Credits list, record it with `betting-agent "
                "credit --amount ... --date ...` and run this again. "
            )
        else:
            why = (
                "Either the amount is wrong, or the account holds money this ledger "
                "cannot explain; run `betting-agent reconcile --full` and settle that "
                "question before recording anything. "
            )
        return {
            "ok": False,
            "reason": "absorbed_residual" if by_absorbed else "amount_disagrees",
            "direction": direction,
            "amount": amount, "expected": expected, "actual": actual, "implied": implied,
            "stated": stated, "disagreement": disagreement,
            "tolerance": _ADJUSTMENT_TOLERANCE, "detail": detail,
            "absorbed_residual": absorbed,
            "message": (
                f"refusing this {direction}: the exchange holds {actual} where the ledger "
                f"expects {expected}, an implied {implied} against your stated {stated}: "
                f"{disagreement} unaccounted for (tolerance "
                f"{_ADJUSTMENT_TOLERANCE}). " + why + "NOTHING was written.\n"
                f"  {absorbed_line}\n"
                f"  derivation: {derivation}"
            ),
        }

    # The anchor and ONLY the anchor. ``live_genesis_ts`` is the era boundary (docs/14 B2).
    # Audit first, then move the anchor: a crash between the two leaves a recorded
    # intention and an unmoved anchor (loud at the next reconcile), never a moved anchor
    # with no record of why.
    ledger.audit(_ADJUSTMENT_EVENT[direction], detail=detail)
    ledger.meta_set("live_genesis_balance", str(new_genesis))
    decisions_line = _decisions_line(
        direction, amount, old_genesis, new_genesis, actual, derivation, et_day(now)
    )
    return {
        "ok": True, "reason": None, "direction": direction, "amount": amount,
        "expected": expected, "actual": actual, "implied": implied, "stated": stated,
        "disagreement": disagreement, "tolerance": _ADJUSTMENT_TOLERANCE,
        "old_genesis_balance": old_genesis, "new_genesis_balance": new_genesis,
        "event": _ADJUSTMENT_EVENT[direction], "detail": detail,
        "decisions_line": decisions_line,
        "message": (
            f"{direction} recorded: meta.live_genesis_balance {old_genesis} -> "
            f"{new_genesis} (verified against an exchange balance of {actual}; implied "
            f"{implied} vs stated {stated}). live_genesis_ts unchanged.\n"
            f"  {absorbed_line}"
        ),
        "absorbed_residual": absorbed,
    }


# --------------------------------------------------------------------------- report
def _fills_population_line(fills_check: dict) -> str:
    """"verified n new, skipped m previously verified" — the EF-1 watermark, stated."""
    line = (
        f"Fills coverage: verified {fills_check.get('n_bets', 0)} new, "
        f"skipped {fills_check.get('n_skipped', 0)} previously verified"
    )
    mark = fills_check.get("watermark")
    return f"{line} (watermark {mark})." if mark else f"{line} (no watermark yet)."


def _report_text(now: datetime, breakdown: dict, actual: Decimal, drift: Decimal,
                 checks: dict, failed: list[str]) -> str:
    b = breakdown
    lines = [f"# Reconciliation — {et_day(now)}", ""]
    verdict = "FAIL" if (failed or drift != _ZERO) else "PASS"
    lines.append(f"Run at {iso(now)}. Result: **{verdict}**.")
    lines.append("")
    lines.append(
        "Every number is 4dp (the ledger's storage precision); P/L elsewhere is "
        "net P/L (after fees)."
    )
    lines.append("")
    lines.append("## Balance walk")
    lines.append("")
    lines.append("| term | amount |")
    lines.append("|---|---|")
    lines.append(f"| genesis balance ({b['genesis']['ts']}) | {_money(b['genesis']['balance'])} |")
    lines.append(
        f"| − real bet debits (stake + fee), n={b['debits']['n']} | "
        f"{_money(-D(b['debits']['total']))} |"
    )
    lines.append(
        f"| + real bet payouts, n={b['credits']['n']} | {_money(b['credits']['total'])} |"
    )
    can = b["canary"]
    if can.get("present"):
        lines.append(f"| − canary cost ({can.get('ticker')}) | {_money(-D(can['cost']))} |")
        lines.append(f"| + canary payout | {_money(can['payout'])} |")
    else:
        lines.append("| canary | none recorded |")
    net = b.get("netting") or {}
    if net.get("n"):
        tickers = ", ".join(p["ticker"] for p in net.get("pairs", []))
        lines.append(
            f"| + netted pairs, pre-paid at fill, n={net['n']} ({tickers}) | "
            f"{_money(net.get('total', '0'))} |"
        )
    per = b.get("personal") or {}
    if per.get("n") or per.get("n_on_harness_ticker"):
        lines.append(
            f"| − personal orders (cost + fee), n={per.get('n', 0)} | "
            f"{_money(-(D(per.get('cost', '0')) + D(per.get('fee', '0'))))} |"
        )
        lines.append(f"| + personal payouts | {_money(per.get('payout', '0'))} |")
        if per.get("n_on_harness_ticker"):
            lines.append(
                f"| personal orders on our own tickers, n={per['n_on_harness_ticker']} | "
                "excluded (position netting) |"
            )
    else:
        lines.append("| personal orders | none recorded |")
    given = b.get("exchange_credits") or {}
    if given.get("n"):
        kinds = ", ".join(sorted({c["kind"] for c in given.get("credits", [])}))
        lines.append(
            f"| + exchange credits, n={given['n']} ({kinds}) | "
            f"{_money(given.get('total', '0'))} |"
        )
    absorbed = b.get("absorbed_residual") or {}
    if absorbed.get("n"):
        lines.append(
            f"| + absorbed residual, n={absorbed['n']} earlier night(s) | "
            f"{_money(absorbed.get('total', '0'))} |"
        )
    lines.append(f"| **expected** | {_money(b['expected'])} |")
    lines.append(f"| **actual** (`get_balance`) | {_money(actual)} |")
    lines.append(f"| **drift** (actual − expected) | {_money(drift)} |")
    lines.append("")

    lines.append("## Cross-checks")
    lines.append("")
    for name in _CHECKS:
        c = checks.get(name, {})
        lines.append(f"- `{name}`: **{'PASS' if c.get('ok') else 'FAIL'}**")
    lines.append("")
    # The skip population is never silent (EF-1): a reader has to be able to see how much
    # of tonight's proof was carried over from an earlier clean run.
    lines.append(_fills_population_line(checks.get("fills_match", {})))
    lines.append("")

    if failed:
        lines.append("## Failing checks")
        lines.append("")
        for name in failed:
            lines.append(f"### `{name}`")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(checks.get(name, {}), indent=2, sort_keys=True))
            lines.append("```")
            lines.append("")

    if drift != _ZERO:
        lines.append(
            f"Drift is {_money(drift)}, which is unexplained money. HALT is set; do not "
            "resume until the walk balances."
        )
        lines.append("")

    lines.append("## Debits (stake + fee)")
    lines.append("")
    lines.append("| bet | ticker | status | stake | fee | placed_at |")
    lines.append("|---|---|---|---|---|---|")
    for d in b["debits"]["bets"]:
        lines.append(
            f"| {d['bet_id']} | {d['ticker']} | {d['status']} | {_money(d['stake'])} | "
            f"{_money(d['fee'])} | {d['placed_at']} |"
        )
    if not b["debits"]["bets"]:
        lines.append("| _none_ | | | | | |")
    lines.append("")

    lines.append("## Credits (payouts)")
    lines.append("")
    lines.append("| bet | ticker | outcome | payout | settled_at |")
    lines.append("|---|---|---|---|---|")
    for c in b["credits"]["bets"]:
        lines.append(
            f"| {c['bet_id']} | {c['ticker']} | {c['outcome']} | {_money(c['payout'])} | "
            f"{c['settled_at']} |"
        )
    if not b["credits"]["bets"]:
        lines.append("| _none_ | | | | |")
    lines.append("")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- public API
def _announce_halt(ledger, settings, drift: Decimal, failed: list[str],
                   report_path: Path) -> None:
    """Audit ``halt_set`` and post one banner for a drift HALT. **Never raises.**

    Two gaps this closes, both from docs/16 §5 (and Appendix A of docs/14, which tabled
    the first).

    ``halt_set`` with a ``detail.reason``: every other HALT in the system writes one — the
    ``halt`` command and the settle-unreachable line in ``cli.py`` — so "why is the system
    stopped" was answerable from the audit trail for every reason except the money one.

    And the banner. docs/12 §8.1 is the standing proof that a condition written only to a
    file reaches nobody: this HALT stopped live trading at 03:01Z on 2026-08-16 and was
    discovered by a human happening to look, hours of lost slots later. ``raise_alert``
    posts once and records ``alert_raised`` whether or not the banner was delivered.

    Called strictly AFTER ``set_halt`` and the report, and wrapped whole: the HALT is the
    protection, and announcing it must never be able to undo it. That is the same ordering
    and the same posture ``cli._settle_halt_if_unreachable`` and ``audit._notify_non_pass``
    already take (docs/14 D1: "failure to notify is itself non-fatal").
    """
    detail = {
        "reason": "reconcile_drift", "drift": str(drift),
        "failed_checks": failed, "report": str(report_path),
    }
    try:
        ledger.audit("halt_set", detail=detail)
    except Exception:  # noqa: BLE001 - the HALT is what matters; its audit row is not
        pass
    try:
        from betting_agent.harness.notify import raise_alert

        raise_alert(
            ledger, settings, key="reconcile_drift",
            title="betting-agent: HALT (reconcile drift)",
            message=(
                f"reconciliation drift {drift} — live trading is HALTed"
                + (f"; failing checks: {', '.join(failed)}" if failed else "")
                + f". Report: {report_path}"
            ),
            detail=detail,
        )
    except Exception:  # noqa: BLE001 - a broken notifier must never unmake the HALT
        pass


def _run_day(run_at) -> str | None:
    """The Eastern day a stored reconciliation belongs to, or ``None`` if undatable."""
    try:
        return et_day(parse_iso(str(run_at)))
    except (ValueError, TypeError):
        return None


def _verdict(drift: Decimal, failed: list[str], settings) -> str:
    """``exact`` / ``absorbed`` / ``noted`` / ``large`` for tonight (Arno, 2026-09-27).

    A failing check is ``large`` at any drift, zero included: the checks are the part of
    the reconciliation that is not a model, and a failure there is never rounding. The
    bands are inclusive at their upper edge, so exactly ``absorb_usd`` is absorbed and
    exactly ``halt_drift_usd`` is noted.
    """
    if failed:
        return _LARGE
    if drift == _ZERO:
        return _EXACT
    size = abs(q4(drift))
    if size <= q4(D(settings.reconcile.absorb_usd)):
        return _ABSORBED
    if size <= q4(D(settings.reconcile.halt_drift_usd)):
        return _NOTED
    return _LARGE


def _announce_noted_drift(ledger, settings, drift: Decimal, expected: Decimal,
                          actual: Decimal) -> None:
    """Post the banner for a noted drift. **Never raises**, like every other alert here."""
    try:
        from betting_agent.harness.notify import raise_alert

        raise_alert(
            ledger, settings, key="reconcile_drift_noted",
            title="betting-agent: drift noted (attempts still running)",
            message=(
                f"reconciliation drift {drift}: expected {expected}, actual {actual}. "
                "Every cross-check passed. The drift is too large to absorb, so it is "
                "not carried into the walk; the attempts are still running."
            ),
            detail={"drift": str(drift), "expected": str(expected),
                    "actual": str(actual), "verdict": _NOTED},
        )
    except Exception:  # noqa: BLE001 - a broken notifier must never break the run
        pass


def residual_summary(ledger, settings, now: datetime) -> dict | None:
    """The absorbed residual, all of it and the watch's trailing window.

    ``total`` and ``nights`` are the walk's term: the signed sum of every absorbed drift
    since genesis and the Eastern days it came from. ``window_abs`` and ``window_nights``
    are what the watch compares against its limits: the same rows over the last
    ``reconcile.residual_window_days`` days, in absolute value, so drifts that cancel in
    the walk still add up here. Nights, not rows, for the count: a night reconciled twice
    is one night. ``None`` in the paper era.
    """
    genesis_iso = ledger.meta_get("live_genesis_ts")
    if not genesis_iso:
        return None
    runs = _absorbed_runs(ledger, parse_iso(str(genesis_iso)))
    days = int(settings.reconcile.residual_window_days)
    since = now - timedelta(days=days)
    window = [r for r in runs if (_ts_or_none(r["run_at"]) or now) >= since]
    return {
        "total": q4(sum((D(r["drift"]) for r in runs), _ZERO)),
        "nights": len({_run_day(r["run_at"]) for r in runs}),
        "window_days": days,
        "window_abs": q4(sum((abs(D(r["drift"])) for r in window), _ZERO)),
        "window_nights": len({_run_day(r["run_at"]) for r in window}),
    }


# The streak key the residual watch alerts under. One banner when the watch first goes
# over, silence while it stays over, and re-armed by the first night it is back under.
_RESIDUAL_ALERT = "reconcile_residual"


def _watch_residual(ledger, settings, now: datetime) -> dict | None:
    """The cumulative watch on absorbed residuals. Alerts, never halts. **Never raises.**

    Replaces the repeated-drift rule, which halted on the same drift three nights running.
    Under absorption a standing drift cannot repeat, because the walk carries it from the
    first night and the second night reads exact. What is left to watch for is many small
    drifts adding up, which is what a slow leak or a missing term would look like.

    Uses the streak core at threshold one, the shape the invariant alerts use: the first
    night over the limit alerts, the nights after it that stay over are counted quietly,
    and the first night back under re-arms it.
    """
    try:
        from betting_agent.harness.notify import record_failure, record_success

        summary = residual_summary(ledger, settings, now)
        if summary is None:
            return None
        rec = settings.reconcile
        over = (summary["window_abs"] > q4(D(rec.residual_alert_usd))
                or summary["window_nights"] > int(rec.residual_alert_nights))
        summary["over"] = over
        if not over:
            record_success(ledger, _RESIDUAL_ALERT)
            return summary
        record_failure(
            ledger, settings, _RESIDUAL_ALERT, threshold=1,
            title="betting-agent: absorbed residual is adding up (not halted)",
            message=(
                f"the last {summary['window_days']} days absorbed "
                f"${summary['window_abs']} over {summary['window_nights']} night(s), past "
                f"the watch's ${q4(D(rec.residual_alert_usd))} or "
                f"{rec.residual_alert_nights} nights. The attempts are still running; "
                "something the walk does not model is moving money."
            ),
            detail={"window_abs": str(summary["window_abs"]),
                    "window_nights": summary["window_nights"],
                    "total": str(summary["total"]), "nights": summary["nights"]},
        )
        return summary
    except Exception:  # noqa: BLE001 - a watch that breaks must never break the run
        return None


def _fills_watermark(ledger, full: bool) -> datetime | None:
    """``meta.fills_verified_through``, or ``None`` under ``--full``/an unreadable stamp."""
    if full:
        return None
    return _ts_or_none(ledger.meta_get("fills_verified_through"))


def reconcile_once(ledger, client, settings, now: datetime | None = None, *,
                   allow_provisional: bool = True, full: bool = False) -> dict:
    """Reconcile the live-era balance walk against the exchange (Jul29 spec L8).

    Returns ``{"skipped": "paper_era"}`` before genesis (no row, no audit). Otherwise
    inserts a ``reconciliations`` row and returns::

        {"ok": bool, "expected": Decimal, "actual": Decimal, "drift": Decimal,
         "checks": {name: {...}}, "failed_checks": [name, ...],
         "detail": {walk breakdown}, "report": Path | None, "halted": bool}

    What each verdict writes is in the module docstring. In short: ``exact`` audits
    ``reconcile_ok``; ``absorbed`` audits ``reconcile_absorbed`` and nothing else;
    ``reversed`` audits ``reconcile_absorption_reversed`` and nothing else;
    ``noted`` audits ``reconcile_drift`` and posts one banner; ``large`` writes HALT with
    reason ``reconcile_drift``, writes ``reports_dir/reconcile-<ET day>.md``, audits
    ``reconcile_drift`` and posts a banner. Every concluded run then runs the residual
    watch, whose summary comes back as ``residual``.

    **Provisional runs (owner decision D1, amending L8 step 6).** A dirty run whose every
    residual is attributable to a settlement our own settle pass has not written down yet
    is not a failure but an early read: it audits ``reconcile_provisional`` with the
    attribution, sets no HALT, writes no report and no ``reconciliations`` row (the run
    did not conclude), and returns ``provisional`` in place of the verdict. The caller
    defers and the next tick's settle pass closes the gap. ``allow_provisional=False``
    turns the gate off — the tick passes it once the night's deferral bound is spent, so a
    problem that is *not* mere lag still HALTs before morning.

    ``full=True`` (``betting-agent reconcile --full``) ignores
    ``meta.fills_verified_through`` and re-verifies every post-genesis bet from scratch —
    the operator's escape hatch when the watermark itself is what is in doubt.
    """
    now = now if now is not None else utc_now()

    genesis_iso = ledger.meta_get("live_genesis_ts")
    if not genesis_iso:
        return {"skipped": "paper_era"}
    genesis = parse_iso(str(genesis_iso))
    genesis_balance = _dec(ledger.meta_get("live_genesis_balance"))

    bets = _real_bets(ledger)
    expected, breakdown = _walk(ledger, settings, bets, genesis, genesis_balance)
    actual = q4(_dec(client.get_balance().dollars))
    drift = q4(actual - expected)

    settlements = _page_settlements(client, genesis)
    # EF-1: ONE orders pagination and ONE fills pagination for the whole run. The orders
    # page feeds the coid->order map; the fills page is indexed by order_id. Everything
    # below is dict lookups over these two lists.
    orders = _page_orders(client, genesis)
    coid_orders = _orders_by_coid(orders)
    fills_index = _fills_by_order(_page_fills(client, genesis))
    checks = {
        "fills_match": _check_fills_match(
            client, settings, bets, genesis, coid_orders=coid_orders,
            fills_index=fills_index, watermark=_fills_watermark(ledger, full),
        ),
        "settlements_covered": _check_settlements_covered(ledger, bets, settlements),
    }
    for p in checks["fills_match"]["priceless"]:  # MP-6
        ledger.audit("priceless_fills", bet_id=p["bet_id"],
                     detail={"total_count": p["total_count"], "priced_count": p["priced_count"]})
    failed = [name for name in _CHECKS if not checks[name]["ok"]]
    verdict = _verdict(drift, failed, settings)
    ok = verdict == _EXACT

    if not ok and allow_provisional:
        attribution = _attribute_pending_settlements(
            bets, _load_canary(ledger), settlements, checks, drift
        )
        if attribution is not None:
            ledger.audit("reconcile_provisional", detail=attribution)
            return {
                "ok": False, "verdict": "provisional", "provisional": attribution,
                "expected": expected, "actual": actual, "drift": drift, "checks": checks,
                "failed_checks": failed, "detail": dict(breakdown),
                "report": None, "halted": False,
            }

    reversal = _reversal_target(ledger, genesis, drift) if verdict == _ABSORBED else None
    if reversal is not None:
        verdict = _REVERSED

    detail = dict(breakdown)
    detail.update({"actual": str(actual), "drift": str(drift), "checks": checks,
                   "verdict": verdict})
    if reversal is not None:
        detail.update({"reversed_run_at": reversal["run_at"],
                       "reversed_drift": reversal["drift"]})
    ledger.insert_reconciliation(
        iso(now), expected_balance=expected, actual_balance=actual, drift=drift,
        ok=ok, detail=json.dumps(detail, sort_keys=True),
    )

    report_path: Path | None = None
    if verdict == _EXACT:
        ledger.audit("reconcile_ok", detail={"drift": str(drift)})
    elif verdict == _ABSORBED:
        # Recorded, carried into every later walk by the row just written, and not
        # announced: this is the band the decision says is not worth a person's attention
        # one night at a time. The residual watch below is what looks at it in aggregate.
        ledger.audit("reconcile_absorbed", detail={
            "verdict": _ABSORBED, "drift": str(drift), "expected": str(expected),
            "actual": str(actual),
        })
    elif verdict == _REVERSED:
        # Neither night counts from here on: the earlier absorption leaves the walk term
        # and neither row is read by the watch. Audited, because a reversal is the record
        # that a fix landed on money the walk had been carrying on trust.
        ledger.audit("reconcile_absorption_reversed", detail={
            "verdict": _REVERSED, "drift": str(drift), "expected": str(expected),
            "actual": str(actual), "reversed_run_at": reversal["run_at"],
            "reversed_drift": reversal["drift"],
        })
    elif verdict == _NOTED:
        # Recorded and announced, and the system keeps running. The walk itself rides
        # along in the audit detail: a noted drift is a question, and the person who comes
        # to answer it needs the arithmetic that produced it, not just the number.
        ledger.audit("reconcile_drift", detail={
            "verdict": _NOTED, "drift": str(drift), "expected": str(expected),
            "actual": str(actual), "failed_checks": failed, "walk": breakdown,
        })
        _announce_noted_drift(ledger, settings, drift, expected, actual)
    else:
        safety.set_halt(settings, "reconcile_drift")
        report_path = settings.reports_dir / f"reconcile-{et_day(now)}.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            _report_text(now, breakdown, actual, drift, checks, failed), encoding="utf-8"
        )
        ledger.audit("reconcile_drift", detail={
            "verdict": _LARGE,
            "drift": str(drift), "expected": str(expected), "actual": str(actual),
            "failed_checks": failed, "report": str(report_path),
        })
        _announce_halt(ledger, settings, drift, failed, report_path)

    # EF-1, amended by docs/22 section 7.2: the watermark follows the FILLS CHECK, not the
    # night's verdict. It means "a passing fills join stood behind every bet at or before
    # this placed_at", and that claim is exactly as true on a night whose balance drifted
    # by a dollar as on a clean one. Tying it to the verdict meant a single drift
    # re-verified the whole book the next night, and every night after, for nothing. A
    # provisional run still advances nothing: it returns above, having concluded nothing.
    if checks["fills_match"]["ok"]:
        through = checks["fills_match"].get("verified_through")
        if through:
            ledger.meta_set("fills_verified_through", through)

    residual = _watch_residual(ledger, settings, now)

    return {
        "ok": ok, "verdict": verdict, "expected": expected, "actual": actual,
        "drift": drift, "checks": checks, "failed_checks": failed, "detail": detail,
        "report": report_path, "halted": verdict == _LARGE, "residual": residual,
    }
