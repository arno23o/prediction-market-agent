"""Settlement and the account-wide order scan (spec §10, docs/archive/04-account-notes.md).

``settle_once`` is the deterministic per-tick pass:

1. **Settle** every ``filled`` bet whose market is ``finalized``: win/loss (bet side vs
   the market ``result``), ``scalar`` (the market paid a value rather than resolving to a
   side — payout from the exchange's own ``revenue``, fee retained, P/L via
   ``moneymath.scalar_pnl``; docs/16 §5), or ``void`` (a finalized market whose ``result``
   is none of those — ``""``/``void``/``cancelled``). Binary P/L via ``moneymath.bet_pnl``.
2. **Reconcile** real bets against the exchange via
   ``client.find_fills_by_client_order_id`` (the orders→fills join): the actual fill
   count, average price and cost (into ``stake``) override the recorded values, and a
   discrepancy over $0.01 is audited ``reconcile_mismatch``.
3. **Groups**: when every leg of a group is terminal, set ``realized_pnl``; a whole
   (non-broken) group flips to ``settled``, a broken group keeps ``broken`` but still
   records ``realized_pnl`` of its filled legs. Candidates come from the ledger (``filled``
   groups, plus ``broken`` ones still missing ``realized_pnl``) rather than from this
   pass's touched attempts, so a crash between the last leg and the group row heals on the
   next tick (MP-3).
4. **Attempts**: a ``placed`` attempt whose every non-rejected bet is terminal → ``settled``.
5. **Shadows** (Jul29 spec L16): every ``open`` row in ``shadow_bets`` whose market has
   finalized is scored — ``hypothetical_pnl`` for 1 contract *assumed filled at the
   candidate's limit price*, net of the §8 fee; a voided market voids the shadow. Shadows
   are a counterfactual record only: they never touch ``bets``, groups, attempt status,
   the scoreboard, era P/L, or reconciliation.
6. **Refused legs** (docs/14 D12): every ``no_fill`` bet, and every ``rejected`` one, whose
   market has finalized and which carries no hypothetical score yet gets one — outcome +
   hypothetical net P/L at the *declared limit*, on the same assumption and through the same
   market resolution as the shadow scoring above. A no-fill is a leg the exchange declined;
   a reject is a leg the harness itself never sent (a cap, a validation gate). Both carried
   no position, so both are scored by the same arithmetic and counted separately
   (``nofills_*``, ``rejects_*``). Written to the ``hypothetical_*`` columns only;
   ``status``, ``outcome``, ``pnl`` and ``settled_at`` are never touched, so no money total,
   group realization, attempt P/L or reconciliation walk can see them.
7. **Canary** (Jul29 spec L6 step 6): while ``meta.canary.settled`` is false, check that
   market; once finalized, stamp ``settled``/``payout`` and audit ``canary_settled``.
8. **Shared-account scan**: classify post-genesis ORDERS by ``client_order_id`` — ours
   (reconciled above; the scan only heals a fee or stake that disagrees with what the
   order says was charged), impostor (``unknown_fill`` + HALT), or personal
   (``personal_fill_observed`` once per order, plus a ``personal_orders`` row). The window
   runs from the LIVE genesis (falling back to the paper one) and, on ordinary passes, only
   from ``meta.impostor_seen_through`` minus a 24-hour overlap; a weekly full scan covers
   the rest. This is now the system's ONLY impostor check: reconcile's duplicate of it,
   which re-read the same order pages once a night to reach the same verdict, is gone
   (docs/19 section 4A, docs/22 section 7.3). See ``_shared_account_scan``: the guarantees
   there are load-bearing, not commentary.
9. **Personal orders** (docs/22 section 7.3): orders placed on the account outside the
   harness are a term in the nightly balance walk. The scan writes them down; this step
   stamps ``settled_at`` and ``payout`` once the exchange settles the market, from the same
   settlements feed the scalar path reads. Without it the walk cannot explain that outside
   trading, which is what left $0.9991 unexplained on 2026-08-30 and halted the system.

The scan is orders-based because fills carry no ``client_order_id`` on the live API —
only ``/portfolio/orders`` does. It pages ``client.get_orders`` (min_ts filtered
client-side on ``created_time``, since the server ignores its filters). There is no
fills-based fallback: both ``KalshiClient`` and ``FakeKalshi`` expose ``get_orders``,
so a guarded fallback would be permanently dead code. The audit event names
(``unknown_fill``, ``personal_fill_observed``) are kept from spec §14 even though the
classification source is the orders feed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from decimal import Decimal

from betting_agent.ids import CLIENT_ORDER_ID_RE
from betting_agent.kalshi.types import (
    parse_count,
    parse_price,
    parse_ts,
    reported_cost,
    reported_fee,
)
from betting_agent.moneymath import D, bet_pnl, q4, scalar_pnl
from betting_agent.moneymath import fee as calc_fee
from betting_agent.timeutil import iso, parse_iso, utc_now

_ZERO = D("0")
_ONE = D("1")
_CENT = D("0.01")
# The exchange's word for "this market paid a value, not a side" — and, since 007, the
# ``bets.outcome`` we record for it. Same string on both sides on purpose.
_SCALAR = "scalar"
# Fees live on the $0.0001 grid since docs/14 D5, so the cent tolerance that price
# comparisons keep (prices ARE cent-quantized) would out-scale every low-priced fee —
# an 11% fee error at 1 contract @ $0.15 is $0.0010 and must audit. $0.0010 still
# tolerates per-piece ceiling drift on multi-piece fills (≤ N × $0.0001).
_FEE_TOLERANCE = D("0.0010")
_TERMINAL_BET = frozenset({"settled", "voided", "no_fill", "rejected"})

# How far back of the already-scanned history the shared-account scan re-reads on every
# pass (EF-2). A day is generous next to the 15-minute tick and cheap next to re-paging
# from genesis; it is the slack that makes "incremental" safe rather than lossy.
_SCAN_OVERLAP = timedelta(hours=24)


# --------------------------------------------------------------------------- seams
def _set_halt(settings, reason: str) -> None:
    """Lazy seam over ``harness.safety`` (built concurrently): keeps this module
    importable before ``safety.py`` exists and lets tests monkeypatch ``_set_halt``."""
    from betting_agent.harness.safety import set_halt

    set_halt(settings, reason)


# --------------------------------------------------------------------------- helpers
def _is_finalized(market) -> bool:
    return (market.status or "").lower() == "finalized"


def _market_verdict(market) -> str | None:
    """How a finalized market resolved: ``"yes"``/``"no"``, ``"scalar"``, or ``None`` (void).

    A finalized market carries its outcome in ``raw['result']``. Three-way since docs/16
    §5: the exchange also settles some markets ``scalar``, paying a value of its own
    choosing per contract instead of resolving to a side (KXNPBTOTAL-26AUG130500HIRYAK-12,
    a game total on a shortened game, paid $0.82 a contract on 2026-08-15) and KEEPING the
    fee. Folding that into the void bucket — which is what this function did, because void
    was everything-not-yes-or-no — refunded a stake that was never refunded and zeroed a
    fee that was never returned. Anything still outside ``{"yes","no","scalar"}``
    (``""``, ``"void"``, ``"cancelled"``) remains a void.
    """
    result = (market.raw.get("result") or "").strip().lower()
    return result if result in ("yes", "no", _SCALAR) else None


def _market_result(market) -> str | None:
    """The BINARY reading of :func:`_market_verdict`: a winning side, or ``None``.

    For readers that can only score win/loss/void — the shadow and no-fill
    counterfactuals, and the canary's guarded path — a scalar market reads as ``None``
    here, i.e. as "not a side". Each of those callers documents what it then does; none of
    them may treat that ``None`` as "the exchange refunded us", which is why the money
    path (:func:`_settle_bet`) calls :func:`_market_verdict` instead.
    """
    verdict = _market_verdict(market)
    return verdict if verdict in ("yes", "no") else None


def _coef_for(settings, category: str | None) -> Decimal:
    coefs = settings.fees.category_coefs
    if category is not None and category in coefs:
        return D(coefs[category])
    return D(settings.fees.default_coef)


def _cached_market(client, market_cache: dict, ticker: str, counts: dict | None = None):
    """One market fetch per ticker per pass, with ONE retry before giving up (MP-1).

    A single transient ``get_market`` failure used to cache ``None`` outright, which left
    a finalized bet unsettled while the exchange had already paid it out — and the 23:00
    reconciliation then read that payout as unexplained money and HALTed the loop
    overnight. One immediate retry closes the common blip. A second failure still caches
    ``None`` (delisted market or a real outage: retry next tick) and, when ``counts`` is
    supplied, marks the pass as partially failed so ``cli._reconcile_if_due`` DEFERS
    tonight's reconciliation instead of halting on the settlement this pass just missed
    (spec WP1 item 1a/1b, amendment D1).
    """
    if ticker not in market_cache:
        market_cache[ticker] = None
        for attempt_no in range(2):  # initial + one retry
            try:
                market_cache[ticker] = client.get_market(ticker)
                break
            except Exception:  # noqa: BLE001 - transport blip, or the market is gone
                if attempt_no and counts is not None:
                    counts["errors"] += 1
    return market_cache[ticker]


def _page_fills(client, min_ts: datetime | None) -> list:
    """Every fill at/after ``min_ts``, following the cursor to exhaustion."""
    out: list = []
    cursor = None
    while True:
        fills, cursor = client.get_fills(min_ts=min_ts, cursor=cursor)
        out.extend(fills)
        if not cursor:
            return out


def _page_settlements(client, min_ts: datetime | None) -> list:
    """Every settlement at/after ``min_ts``, following the cursor to exhaustion."""
    out: list = []
    cursor = None
    while True:
        page, cursor = client.get_settlements(min_ts=min_ts, cursor=cursor)
        out.extend(page)
        if not cursor:
            return out


def _settlements_index(ledger, client, cache: dict) -> dict[str, list]:
    """``{ticker: [settlement, ...]}`` for this pass — paged ONCE, and only if needed.

    A scalar settlement is the only thing in the settle pass that needs the exchange's
    ``/portfolio/settlements`` feed: the market detail says *that* a market paid a value,
    and only the settlement record says *what* it paid (``revenue``). Scalars are rare, so
    this is lazy — a pass that meets none never makes the call, and the tick's cost is
    unchanged for every day that has no scalar in it.

    Windowed from the live genesis (falling back to the paper one, then to unfiltered),
    the same base ``_scan_start`` uses: settlements before genesis belong to an era this
    system does not reason about.

    A failed pagination caches the empty index and is NOT retried within the pass. The
    consequence is benign by construction: with no record, a scalar bet is left unsettled
    for a later pass (see :func:`_settle_bet`), which is the same conservative answer as an
    unreachable market.
    """
    if "index" in cache:
        return cache["index"]
    index: dict[str, list] = {}
    try:
        min_ts = _ts_or_none(
            ledger.meta_get("live_genesis_ts") or ledger.meta_get("genesis_ts")
        )
        for s in _page_settlements(client, min_ts):
            if s.ticker:
                index.setdefault(s.ticker, []).append(s)
    except Exception:  # noqa: BLE001 - transport blip: no record, so nothing is settled
        index = {}
    cache["index"] = index
    return index


def _scalar_settlement(ledger, client, cache: dict, ticker: str):
    """The one settlement record for ``ticker``, or ``None`` when it cannot be trusted.

    ``None`` covers both "the exchange has not published it yet" (the ordinary race: the
    market finalized between the settlements page and now) and "it published two records
    that disagree". Disagreement is never resolved by preference here — the same refusal
    ``reconcile._settlement_results`` applies to an ambiguous ticker, for the same reason:
    guessing which figure the exchange actually paid is precisely what a money path must
    not do.
    """
    records = _settlements_index(ledger, client, cache).get(ticker) or []
    if len(records) == 1:
        return records[0]
    if not records:
        return None
    revenues = {str(r.revenue) for r in records}
    return records[-1] if len(revenues) == 1 else None


def _row_stake(bet: dict) -> Decimal:
    """What the row says its position cost, read exactly as the balance walk reads it.

    One definition, ``reconcile._stake_of``, so the P/L settle books and the debit the
    walk takes cannot disagree. Imported here rather than at module scope for the reason
    :func:`_settle_personal_orders` gives: ``reconcile`` pulls in ``harness.safety``.
    """
    from betting_agent.harness.reconcile import _stake_of

    return _stake_of(bet)


def _reconcile_real(
    bet: dict, client, settings
) -> tuple[Decimal, Decimal, Decimal, Decimal, dict | None, dict | None]:
    """Return ``(contracts, fill_price, stake, fee, mismatch_detail, priceless_detail)``
    for a real bet.

    The recorded values are the baseline; if the orders→fills join yields fills they
    become the truth for count (summed), price (count-weighted) and stake (each fill's
    count times its own price, summed). The stake is that sum and not the new average
    times the count, because an order that fills in pieces at several prices has an
    average that does not fit four decimal places (2026-09-27): A-0316-B01 paid $0.4043
    for 3 contracts, an average of $0.134766..., and 3 x $0.1348 is $0.4044. The sum is
    what the exchange charged. When a fill carries no price the sum is incomplete, so the
    recorded stake stands if the count still matches and the count times the average
    stands in if it does not. The FEE is different: by the time a row settles, the
    recorded value is normally the exchange's own total, which the order scan
    (:func:`_correct_fee`) reads off the order listing within a tick of placement. That
    is a RECEIPT, where the §8 figure is a reconstruction. Since docs/14 D5 the model
    reproduces the live schedule (ceil-to-$0.0001, no cent floor), but the two still part
    company whenever the receipt describes something the model cannot see: an order that
    filled at several prices is charged piece by piece, not at its average (A-0316-B01),
    a partial fill or a price improvement moves the base, the category coefficient may be
    the wrong one, and the schedule is the exchange's to change without telling us. So the
    recorded fee is kept whenever it exists and the count still matches; the §8 estimate
    is used only as the comparison baseline for the mismatch audit, and as the fallback
    when the count changed or no fee was recorded. Overwriting the recorded fee with the
    model here would put model error into ``pnl`` and surface it as reconcile drift.
    ``mismatch_detail`` is non-``None`` when a price/fee discrepancy exceeds $0.01 or
    the count differs.

    Counts are exact ``Decimal`` on both sides (KC-2). One order can come back as several
    fractional fills — A-0054-B01 filled live as 0.28 + 0.34 + 0.38 — and truncating each
    fill to an int summed those to zero and reported a phantom count mismatch. ``Decimal``
    equality also spans storage classes, so a legacy INTEGER ``1`` compares equal to a
    ``1.00`` fill total.

    MP-6: the count-weighted average price must divide by the fills that actually carry a
    price, not by every fill — a fill with no price carries no price information, and
    dividing by the full count silently understated the average whenever one was missing.
    ``priceless_detail`` is non-``None`` whenever at least one fill lacked a price; when
    ALL of them do, there is nothing to average, so the recorded price is kept rather than
    dividing by zero (mirrors ``execute._resolve_ambiguous``'s convention from WP0).
    """
    contracts = D(str(bet["contracts"]))
    fill_price = D(bet["fill_price"])
    stake = _row_stake(bet)
    fee_amt = D(bet["fee"]) if bet["fee"] is not None else _ZERO

    fills = client.find_fills_by_client_order_id(bet["client_order_id"])
    actual_count = sum((D(str(f.count)) for f in fills), _ZERO)
    if actual_count <= 0:
        return contracts, fill_price, stake, fee_amt, None, None

    priced = [f for f in fills if f.price is not None]
    priced_count = sum((D(str(f.count)) for f in priced), _ZERO)
    priceless = None
    if priced_count != actual_count:
        priceless = {"total_count": str(actual_count), "priced_count": str(priced_count)}
    if priced_count > 0:
        notional = sum((D(f.price) * D(str(f.count)) for f in priced), _ZERO)
        actual_price = q4(notional / priced_count)
    else:
        actual_price = fill_price  # every fill priceless: nothing to average
    if priced_count == actual_count:
        actual_stake = q4(notional)  # each piece at its own price: the exchange's cost
    elif actual_count == contracts:
        actual_stake = stake  # a priceless piece leaves the sum short: keep the row's
    else:
        actual_stake = q4(actual_count * actual_price)
    fee_estimate = calc_fee(actual_count, actual_price, _coef_for(settings, bet["category"]))
    if bet["fee"] is not None and actual_count == contracts:
        fee_final = fee_amt  # exchange-charged fee, recorded at placement — the truth
    else:
        fee_final = fee_estimate  # no recorded fee / count changed: best-effort model

    mismatch = (
        actual_count != contracts
        or abs(actual_price - fill_price) > _CENT
        or abs(fee_estimate - fee_amt) > _FEE_TOLERANCE
    )
    detail = None
    if mismatch:
        detail = {
            "recorded": {"contracts": str(contracts), "fill_price": str(fill_price),
                         "fee": str(fee_amt)},
            "actual": {"contracts": str(actual_count), "fill_price": str(actual_price),
                       "fee_model_estimate": str(fee_estimate), "fee_used": str(fee_final)},
        }
    return actual_count, actual_price, actual_stake, fee_final, detail, priceless


def _settle_bet(ledger, bet: dict, market, client, settings, now: datetime, counts: dict,
                settlement_cache: dict | None = None) -> None:
    """Settle one finalized ``filled`` bet: update the row and bump ``counts``.

    Three shapes, and the third is the one docs/16 §5 added:

    * **void** — no payout, stake and fee both refunded, P/L zero by construction;
    * **win/loss** — the binary settlement, ``moneymath.bet_pnl``;
    * **scalar** — the exchange paid a value of its own choosing per contract and KEPT the
      fee, so the payout comes from its ``revenue`` for our position and the P/L is
      ``payout − stake − fee`` (``moneymath.scalar_pnl``). Nothing is inferred: with no
      usable settlement record the row is LEFT for a later pass, exactly as a filled row
      lacking execution data is. Booking a guess as a void is what caused the 2026-08-16
      HALT.

    P/L is taken against the row's ``stake``, the same figure the balance walk debits,
    rather than against ``contracts x fill_price`` (2026-09-27). For a real row the stake
    is what the fills cost, which the fills join below supplies; see :func:`_reconcile_real`.
    """
    verdict = _market_verdict(market)
    bet_id = bet["bet_id"]

    if verdict is None:  # void: no payout, fee refunded (spec §8/§10)
        ledger.update_bet(bet_id, status="voided", outcome="void",
                          pnl=_ZERO, fee=_ZERO, settled_at=iso(now))
        counts["bets_voided"] += 1
        return

    if bet["contracts"] is None or bet["fill_price"] is None:
        return  # a filled row lacking execution data: leave for a later pass
    contracts = D(str(bet["contracts"]))  # legacy INTEGER or exact 4dp count alike
    fill_price = D(bet["fill_price"])
    stake = _row_stake(bet)
    fee_amt = D(bet["fee"]) if bet["fee"] is not None else _ZERO

    # A scalar's payout lives only in the settlement record, and there is nothing to be
    # gained by paying for the fills join below on a row we are about to leave for a later
    # pass. Looked up here, applied after the reconcile — so the per-contract value is
    # multiplied by the count the EXCHANGE confirmed, not the recorded one.
    record = None
    if verdict == _SCALAR:
        record = _scalar_settlement(
            ledger, client,
            settlement_cache if settlement_cache is not None else {}, bet["ticker"],
        )
        if record is None:
            _defer_scalar(ledger, bet, contracts, None, counts)
            return

    updates: dict = {}
    if bet["is_real"]:
        contracts, fill_price, stake, fee_amt, mismatch, priceless = _reconcile_real(
            bet, client, settings
        )
        updates.update(contracts=contracts, fill_price=fill_price, stake=stake, fee=fee_amt)
        if mismatch is not None:
            counts["reconcile_mismatches"] += 1
            ledger.audit("reconcile_mismatch", attempt_id=bet["attempt_id"],
                         bet_id=bet_id, detail=mismatch)
        if priceless is not None:  # MP-6
            ledger.audit("priceless_fills", attempt_id=bet["attempt_id"],
                         bet_id=bet_id, detail=priceless)

    if verdict == _SCALAR:
        payout = record.payout_for(bet["side"], contracts)
        if payout is None:
            _defer_scalar(ledger, bet, contracts, record, counts)
            return  # the record cannot price this position: leave it for a human
        outcome = _SCALAR
        pnl = scalar_pnl(q4(payout), contracts, fill_price, fee_amt, stake=stake)
    else:
        outcome = "win" if bet["side"] == verdict else "loss"
        pnl = bet_pnl(outcome, contracts, fill_price, fee_amt, stake=stake)

    updates.update(status="settled", outcome=outcome, pnl=pnl, settled_at=iso(now))
    ledger.update_bet(bet_id, **updates)
    counts["bets_settled"] += 1
    if outcome == _SCALAR:
        counts["scalars_settled"] += 1


def _defer_scalar(ledger, bet: dict, contracts: Decimal, record, counts: dict) -> None:
    """Leave a scalar bet unsettled, loudly: audit it and count a pass error.

    The market detail says a market settled ``scalar``; only the settlement record says
    what it paid. Two things can go wrong with that — the exchange has not published the
    record yet (the ordinary race, which the next tick closes), or it published one that
    cannot attribute a payout to this position (see ``Settlement.payout_for``). Neither is
    a reason to invent a number, and the row is a position we still hold.

    Counting a pass error makes the night's reconciliation DEFER rather than HALT (MP-1's
    mechanism, reused). That is the right ladder: the early passes give the exchange time,
    and if the payout never becomes attributable the deferral bound runs out and the
    reconciliation HALTs — with a human-readable audit trail already in the ledger.
    Silence would be worse than either.
    """
    counts["errors"] += 1
    ledger.audit("scalar_settlement_deferred", attempt_id=bet["attempt_id"],
                 bet_id=bet["bet_id"], detail={
                     "ticker": bet["ticker"], "side": bet["side"],
                     "contracts": str(contracts),
                     "settlement_record": (
                         None if record is None
                         else {"revenue": str(record.revenue),
                               "yes_count": str(record.yes_count),
                               "no_count": str(record.no_count)}
                     ),
                     "reason": ("no settlement record for this ticker yet"
                                if record is None else
                                "the settlement record cannot attribute a payout to "
                                "this position (see Settlement.payout_for)"),
                 })


def _group_needs_settlement(group: dict) -> bool:
    """The MP-3 candidate predicate, mirroring the SQL below exactly.

    ``pending`` is excluded deliberately (verifier's correction): a group still in
    ``pending`` never completed placement, yet all of its legs can be terminal
    (``rejected``/``no_fill``) — settling it would stamp a phantom ``settled`` group with
    zero realized P/L over a hedge that was never on.
    """
    return group["status"] == "filled" or (
        group["status"] == "broken" and group["realized_pnl"] is None
    )


def _settle_groups(ledger, counts: dict) -> None:
    """Settle every candidate group whose legs are all terminal — self-healing (MP-3).

    Keyed on the ledger's own state, not on the attempts this pass happened to touch: a
    crash between the last leg's settlement and the group update used to strand
    ``realized_pnl`` forever, because the group was never revisited. The query is
    idempotent, so the next tick heals it.
    """
    rows = ledger.conn.execute(
        "SELECT DISTINCT attempt_id FROM bet_groups "
        "WHERE status = 'filled' OR (status = 'broken' AND realized_pnl IS NULL)"
    ).fetchall()
    for r in rows:
        aid = r["attempt_id"]
        bets = ledger.bets_for_attempt(aid)
        for group in ledger.groups_for_attempt(aid):
            if not _group_needs_settlement(group):
                continue
            gid = group["group_id"]
            legs = [b for b in bets if b["group_id"] == gid]
            if not legs or not all(b["status"] in _TERMINAL_BET for b in legs):
                continue
            realized = q4(sum((D(b["pnl"]) for b in legs if b["pnl"] is not None), _ZERO))
            if group["status"] == "broken":
                # keep 'broken'; only stamp realized_pnl (once) for the filled legs
                ledger.set_group(gid, realized_pnl=realized)
            else:
                ledger.set_group(gid, status="settled", realized_pnl=realized)
                counts["groups_settled"] += 1


def _settle_attempts(ledger, counts: dict) -> None:
    for a in ledger.attempts_by_status("placed"):
        bets = ledger.bets_for_attempt(a["attempt_id"])
        non_rejected = [b for b in bets if b["status"] != "rejected"]
        if non_rejected and all(b["status"] in _TERMINAL_BET for b in non_rejected):
            ledger.transition(a["attempt_id"], "settled")
            counts["attempts_settled"] += 1


# --------------------------------------------------------------------------- shadows
def _settle_shadows(ledger, client, settings, market_cache, now: datetime, counts: dict) -> None:
    """Score every open shadow bet whose market has finalized (Jul29 spec L16).

    A shadow is the counterfactual: the candidate we did *not* bet. It is scored at
    1 contract **assumed filled at its limit price**, net of the §8 fee — an assumption,
    which is why the number is labelled hypothetical everywhere it surfaces. A voided
    market voids the shadow (no P/L). Nothing here writes ``bets``, ``bet_groups``,
    attempt status, or any real-money total: shadows must never move the record.

    **A SCALAR market voids the shadow, deliberately** (docs/16 §5). ``_market_result``
    reads a scalar as "not a side", and unlike the money path there is nothing to fall
    back on: the exchange publishes a settlement only for a position actually HELD, and a
    shadow by definition held none — so no ``revenue`` exists to derive its scalar value
    from, and inventing one from the market detail would be a fabrication in a column the
    experiments are measured against. Void-at-zero is the honest answer here in a way it
    was never honest for a real position, whose money the exchange had already moved.
    """
    for row in ledger.open_shadow_bets():
        market = _cached_market(client, market_cache, row["ticker"])
        if market is None or not _is_finalized(market):
            continue
        result = _market_result(market)
        if result is None:
            ledger.score_shadow_bet(
                row["shadow_bet_id"], outcome="void", hypothetical_pnl=None,
                scored_at=iso(now),
            )
            counts["shadows_voided"] += 1
            continue
        outcome = "win" if row["side"] == result else "loss"
        price = D(row["limit_price"])
        fee_amt = calc_fee(1, price, _coef_for(settings, getattr(market, "category", None)))
        ledger.score_shadow_bet(
            row["shadow_bet_id"], outcome=outcome,
            hypothetical_pnl=bet_pnl(outcome, 1, price, fee_amt), scored_at=iso(now),
        )
        counts["shadows_scored"] += 1


# --------------------------------------------------------------------------- refused legs
def _declared_size(bet: dict):
    """The size a refused leg declared, from whichever column carries it for its shape.

    A ``no_fill`` row keeps its intended count in ``contracts`` (``execute._no_fill`` passes
    it through). A cap-rejected row deliberately has every execution field NULL and carries
    the same fact in ``declared_contracts`` instead (migration 004): it records an intention,
    not a position. One contract is the live sizing rule for a row that has neither, which is
    every reject written before 004 landed.
    """
    for col in ("contracts", "declared_contracts"):
        if bet[col] is not None:
            return D(str(bet[col]))
    return _ONE


def _score_refused_leg(ledger, bet: dict, market, settings, now: datetime, *, write) -> str:
    """Score one refused leg through ``write``; return ``"scored"`` or ``"voided"``.

    The arithmetic both refused-leg populations share, in one place so it cannot drift
    between them: **assume the fill happened at the declared limit price**, for the declared
    size, net of the §8 fee under the docs/14 D5 model, and read the outcome off the market's
    own result. ``write`` is the caller's counterfactual writer (``score_nofill_bet`` or
    ``score_rejected_bet``), which is what pins the score to a row of the right status.

    A market with no decisive side — including a **scalar** one (docs/16 §5) — scores
    ``void`` at zero. A leg that carried no position has no exchange revenue to read a
    scalar value out of, and ``hypothetical_outcome``'s CHECK cannot express one anyway.
    """
    result = _market_result(market)
    if result is None:
        write(bet["bet_id"], outcome="void", hypothetical_pnl=_ZERO, scored_at=iso(now))
        return "voided"
    outcome = "win" if bet["side"] == result else "loss"
    contracts = _declared_size(bet)
    price = D(bet["limit_price"])
    fee_amt = calc_fee(contracts, price, _coef_for(settings, bet["category"]))
    write(bet["bet_id"], outcome=outcome,
          hypothetical_pnl=bet_pnl(outcome, contracts, price, fee_amt), scored_at=iso(now))
    return "scored"


def _settle_nofills(ledger, client, settings, market_cache, now: datetime, counts: dict) -> None:
    """Score every unscored ``no_fill`` bet whose market has finalized (docs/14 D12).

    A no-fill is a leg the attempt proposed and the exchange never gave us: the order went
    out (or, for a broken hedge, never left) and came back with nothing. Until now those
    legs were invisible to the grader beyond a status word, which meant calibration was
    always measured on the FILLED subset — and the filled subset is adversely selected, not
    a random sample: an order fills exactly when the book was at or better than our limit.
    Four independent deep reviews asked for this (docs/12 §9.17).

    Same counterfactual as a shadow bet (Jul29 spec L16), same assumption, same place in the
    pass: **assume the fill happened at the declared limit price**, for the declared count,
    net of the §8 fee under the docs/14 D5 model. That assumption is why every surface calls
    the number hypothetical. A voided market scores ``void`` at zero, matching what the real
    bets path writes for a void.

    Nothing here touches money. ``status`` stays ``no_fill``, and ``outcome``/``pnl``/
    ``settled_at`` — the metric of record and its provenance — are unreachable from
    ``score_nofill_bet``. Groups, attempt status, the scoreboard, era P/L and the
    reconciliation walk are all keyed off those columns and cannot see these.

    ``_cached_market`` is called WITHOUT ``counts``, like the shadow pass and unlike the
    settlement loop: an unreachable market here costs a counterfactual, which is not a
    reason to defer tonight's balance walk (MP-1's deferral exists for missed settlements —
    money the exchange has already moved). The row simply stays unscored for the next tick.

    A **scalar** market scores ``void`` at zero, for the same reason the shadow pass voids
    one (docs/16 §5): a no-fill carried no position, so the exchange published no
    settlement record for it and there is no ``revenue`` to read a scalar value out of.
    ``hypothetical_outcome``'s CHECK is unchanged by migration 007 for exactly this reason
    — the counterfactual columns cannot reach a scalar and must not pretend to.
    """
    for bet in ledger.unscored_nofill_bets():
        market = _cached_market(client, market_cache, bet["ticker"])
        if market is None or not _is_finalized(market):
            continue
        try:
            kind = _score_refused_leg(ledger, bet, market, settings, now,
                                      write=ledger.score_nofill_bet)
            counts["nofills_voided" if kind == "voided" else "nofills_scored"] += 1
        except Exception as exc:  # noqa: BLE001 - MP-2: one row must not abort the pass
            counts["errors"] += 1
            ledger.audit("nofill_score_error", attempt_id=bet["attempt_id"],
                         bet_id=bet["bet_id"],
                         detail={"ticker": bet["ticker"],
                                 "error": f"{type(exc).__name__}: {exc}"})


def _settle_rejects(ledger, client, settings, market_cache, now: datetime, counts: dict) -> None:
    """Score every unscored ``rejected`` bet whose market has finalized (docs/14 D12).

    The no-fill pass's twin, over the legs the harness itself refused rather than the ones
    the exchange declined: a daily or per-market cap (``cap_daily``, ``cap_market``), the
    drawdown floor (``drawdown_floor``, docs/22 section 7.4), or a validation gate
    (``V11``, no obtainable book with real depth). The population is every ``rejected``
    row, so a new refusal code joins it by being written, not by being listed here; the
    codes are named to say what the population contains, never to select it. D12's
    argument applies
    to them word for word, and with more force — a cap refusal is not even adverse
    selection by the market, it is the harness spending its budget elsewhere, and A-0058 is
    the shape it hides: the caps kept 1 of 3 legs on one match and nothing told the grader
    that the portfolio it was judging was not the portfolio proposed.

    Same assumption, same arithmetic, same isolation as the no-fill pass (see
    ``_score_refused_leg``). ``status`` stays ``rejected`` and ``outcome``/``pnl``/
    ``settled_at`` are unreachable from ``score_rejected_bet``, so no money total, group
    realization, attempt P/L or reconciliation walk can see any of this. Real and paper
    rows alike are scored, exactly as the no-fill pass scores both: the packet's job is the
    whole proposed book, and in practice every reject is a paper row — the refusal is why
    no order was ever sent.

    ``_cached_market`` is called WITHOUT ``counts`` for the no-fill pass's reason: an
    unreachable market costs a counterfactual, never tonight's balance walk. These markets
    are also the oldest ones the pass ever asks about (a reject is terminal from the moment
    it is written, so a row can sit here for months), and a market the exchange has aged out
    simply stays unscored.
    """
    for bet in ledger.unscored_rejected_bets():
        market = _cached_market(client, market_cache, bet["ticker"])
        if market is None or not _is_finalized(market):
            continue
        try:
            kind = _score_refused_leg(ledger, bet, market, settings, now,
                                      write=ledger.score_rejected_bet)
            counts["rejects_voided" if kind == "voided" else "rejects_scored"] += 1
        except Exception as exc:  # noqa: BLE001 - MP-2: one row must not abort the pass
            counts["errors"] += 1
            ledger.audit("reject_score_error", attempt_id=bet["attempt_id"],
                         bet_id=bet["bet_id"],
                         detail={"ticker": bet["ticker"],
                                 "error": f"{type(exc).__name__}: {exc}"})


# --------------------------------------------------------------------------- canary
def _settle_canary(ledger, client, settings, market_cache, now: datetime, counts: dict) -> None:
    """Close out the manual canary bet once its market finalizes (Jul29 spec L6 step 6).

    Consumes ``meta.canary`` as written by the ``canary`` command:
    ``{ts, ticker, side, contracts, fill_price, fee, coid, …, settled: false,
    payout: null}``. Payout is ``contracts × $1`` on a win, ``0`` on a loss, and
    ``stake + fee`` on a void (mirroring the void refund the bets path applies). The
    stamped JSON gains ``settled: true`` and ``payout``, plus two additive fields —
    ``outcome`` and ``settled_at`` — so reconciliation's balance walk (L8) need not
    re-derive them.

    The canary lives outside ``bets`` on purpose (it is placed with a deliberately
    non-matching ``client_order_id``), so nothing here touches bet or attempt rows.
    """
    raw = ledger.meta_get("canary")
    if not raw:
        return
    try:
        canary = json.loads(raw)
    except ValueError:  # a hand-mangled meta value: leave it for a human
        return
    if not isinstance(canary, dict) or canary.get("settled"):
        return
    ticker = canary.get("ticker")
    if not ticker:
        return

    market = _cached_market(client, market_cache, ticker, counts)
    if market is None or not _is_finalized(market):
        return

    # Exact, never int-truncated (KC-2). ``canary.py`` writes the count as the string the
    # exchange reported, which is fractional whenever the fill was: ``int("0.90")`` raises
    # and ``int(Decimal("0.90"))`` is 0 — a canary that cost real money paying out nothing.
    # A SCALAR canary market is left unsettled, on purpose (docs/16 §5). The canary is real
    # money and a real term in the balance walk, and the three payout shapes below cannot
    # express "the exchange paid a value of its own choosing" — booking it as a void would
    # claim a stake-and-fee refund that never happened, which is precisely the bug that
    # HALTed the system on A-0097-B01. There has never been a scalar canary; if one ever
    # occurs the row simply stays open, the reconciliation says so, and a human decides.
    if _market_verdict(market) == _SCALAR:
        counts["errors"] += 1
        ledger.audit("scalar_settlement_deferred", detail={
            "ticker": ticker, "position": "canary",
            "reason": "the canary has no scalar payout path; left for a human",
        })
        return

    contracts = D(str(canary.get("contracts") or "0"))
    fill_price = D(str(canary.get("fill_price") or "0"))
    fee_amt = D(str(canary.get("fee") or "0"))
    result = _market_result(market)
    if result is None:
        outcome = "void"
        payout = q4(contracts * fill_price + fee_amt)  # stake and fee both refunded
    elif canary.get("side") == result:
        outcome = "win"
        payout = q4(contracts * _ONE)
    else:
        outcome = "loss"
        payout = q4(_ZERO)

    canary.update(settled=True, outcome=outcome, payout=str(payout), settled_at=iso(now))
    ledger.meta_set("canary", json.dumps(canary))
    ledger.audit("canary_settled", detail={
        "ticker": ticker, "outcome": outcome, "payout": str(payout),
    })
    counts["canary_settled"] = 1


# ----------------------------------------------------------------- personal orders (009)
def _personal_money(order: dict, settings) -> tuple[Decimal, Decimal, Decimal, str] | None:
    """``(contracts, cost, fee, fee_source)`` for one outside order, or ``None``.

    ``None`` means the order filled nothing, so there is no money to record and no position
    to settle. Everything else comes off the order payload the exchange serves, which
    reports its own fills in aggregate: ``fill_count_fp`` is the exact filled count and
    ``taker_fill_cost_dollars`` / ``maker_fill_cost_dollars`` are what those fills cost,
    which is why the cost is read from there rather than from the limit price (a resting
    order can fill better than it asked). The fee is the exchange's own ``*_fees_dollars``
    when the payload carries them, a receipt rather than a model, and falls back to the §8
    fee model, flagged ``computed``, when it does not. Cost excludes the fee, and the
    balance walk subtracts both.
    """
    contracts = parse_count(order, "fill_count") or _ZERO
    if contracts <= _ZERO:
        return None
    side = (order.get("side") or "").strip().lower()
    price = parse_price(order, f"{side}_price") or _ZERO

    taker_cost = parse_price(order, "taker_fill_cost")
    maker_cost = parse_price(order, "maker_fill_cost")
    if taker_cost is not None or maker_cost is not None:
        cost = (taker_cost or _ZERO) + (maker_cost or _ZERO)
    else:
        cost = contracts * price

    charged = reported_fee(order)
    if charged is not None:
        fee, source = charged, "exchange"
    else:
        fee, source = calc_fee(contracts, price, _coef_for(settings, None)), "computed"
    return q4(contracts), q4(cost), q4(fee), source


def _record_personal_order(ledger, settings, order: dict, key: str,
                           our_tickers: set[str], now: datetime) -> str | None:
    """Write one personal order into ``personal_orders`` (docs/22 section 7.3).

    Returns ``None`` when the row was written, else the reason it was not, which the
    caller audits once per order.

    Called for every personal order the scan SEES, not only the ones it audits: the audit
    fires once per order and the walk needs the row whether or not this pass is the one
    that first noticed it. That is also what lets the weekly full scan backfill orders
    classified before this table existed, which is how the outside order of 2026-08-30
    gets into the walk at all.

    Four orders are skipped, and every one of them leaves its money unexplained, which
    shows up as drift, alerts, and gets looked at by a person. That is the outcome a guess
    here would have hidden.

    * **not a buy.** This table models one shape: contracts bought, cost and fee out of
      the account, a payout back at settlement. A SELL is the opposite sign on every one
      of those terms, so booking one as a cost would move the expected balance by twice
      the trade. Until the table can express a sale, a sale is not recorded.
    * no readable ``created_time``: it cannot be placed in or out of the genesis window,
      and the column is NOT NULL rather than guessed.
    * a side the exchange did not report as yes or no.
    * nothing filled, so there is no money and no position (see :func:`_personal_money`).
    """
    action = (order.get("action") or "").strip().lower()
    if action != "buy":
        return f"action is {action or 'absent'}, not buy"
    created = order.get("created_time")
    side = (order.get("side") or "").strip().lower()
    if not created:
        return "no readable created_time"
    if side not in ("yes", "no"):
        return f"side is {side or 'absent'}, not yes or no"
    money = _personal_money(order, settings)
    if money is None:
        return "the order filled nothing"
    contracts, cost, fee, fee_source = money
    ledger.upsert_personal_order(
        key,
        ticker=order.get("ticker") or "",
        side=side,
        created_time=str(created),
        contracts=contracts, cost=cost, fee=fee, fee_source=fee_source,
        # Position netting pays a matched pair against the ACCOUNT at the later fill, not
        # against either order, so a personal order on a ticker we also hold real money in
        # has no payout this code can attribute. It is recorded and excluded from the walk's
        # arithmetic rather than guessed at.
        on_harness_ticker=int((order.get("ticker") or "") in our_tickers),
        first_seen_at=iso(now),
    )
    return None


def _settle_personal_orders(ledger, client, now: datetime, counts: dict,
                            settlement_cache: dict) -> None:
    """Stamp the exchange's settlement onto outside orders (docs/22 section 7.3).

    The same payout shapes the bets path uses, over rows that are not bets: a win pays
    ``contracts x $1``, a loss pays nothing, a void refunds cost and fee, and a scalar pays
    the settlement record's own value for that side with the fee kept.

    It reads the ``/portfolio/settlements`` feed rather than each market in turn, which is
    the same source the scalar path already uses and the reason this pass costs one
    pagination instead of one request per open personal position. An outside order on a
    market that never finalizes would otherwise be re-fetched on every tick forever. The
    feed carries both halves of what is needed: ``market_result`` says how the market
    resolved and ``revenue`` says what a scalar paid. A ticker whose records disagree is
    refused rather than guessed at, exactly as :func:`_scalar_settlement` refuses one, and
    the reading of ``market_result`` is the balance walk's own.
    """
    # ``on_harness_ticker`` rows are excluded, and excluded here rather than filtered out
    # of the query: they are out of the walk's arithmetic because the exchange netted them
    # against our own position, so there is no payout for this pass to attribute. Left in,
    # a netted or scalar market would take the deferred branch below and bump ``errors``
    # on every tick, forever, over a row that can never resolve.
    rows = [r for r in ledger.unsettled_personal_orders() if not r["on_harness_ticker"]]
    if not rows:
        return
    # The balance walk's own reading of ``market_result``, imported rather than written a
    # third time so the two can never disagree about what a settlement said. Imported here
    # rather than at module scope to keep this module's import graph as it is: ``reconcile``
    # pulls in ``harness.safety``, which this file deliberately reaches through a lazy seam.
    from betting_agent.harness.reconcile import _settlement_results

    index = _settlements_index(ledger, client, settlement_cache)
    for row in rows:
        ticker = row["ticker"]
        if ticker not in index:
            continue
        try:
            record = _scalar_settlement(ledger, client, settlement_cache, ticker)
            if record is None:
                continue  # two records that disagree: a human decides, not this pass
            verdict = _settlement_results([record])[0].get(ticker)
            contracts = D(str(row["contracts"] or "0"))
            if verdict == _SCALAR:
                payout = record.payout_for(row["side"], contracts)
                if payout is None:
                    counts["errors"] += 1
                    ledger.audit("scalar_settlement_deferred", detail={
                        "ticker": ticker, "position": "personal",
                        "order_id": row["order_id"],
                        "reason": "the settlement record cannot price this position",
                    })
                    continue
                payout = q4(payout)
            elif verdict is None:
                payout = q4(D(row["cost"] or "0") + D(row["fee"] or "0"))
            elif row["side"] == verdict:
                payout = q4(contracts * _ONE)
            else:
                payout = _ZERO
            ledger.settle_personal_order(
                row["order_id"], settled_at=iso(now), payout=payout
            )
            counts["personal_settled"] += 1
        except Exception as exc:  # noqa: BLE001 - MP-2: one row must not abort the pass
            counts["errors"] += 1
            ledger.audit("personal_settle_error", detail={
                "ticker": ticker, "order_id": row["order_id"],
                "error": f"{type(exc).__name__}: {exc}",
            })


# --------------------------------------------------------------------------- scan
def _page_orders(client, min_ts: datetime | None) -> list[dict]:
    """Every raw order dict at/after ``min_ts``, following the cursor to exhaustion.

    A page may filter to empty client-side while its cursor is still set, so the loop
    keys on the cursor, never on page emptiness.
    """
    out: list[dict] = []
    cursor = None
    while True:
        orders, cursor = client.get_orders(min_ts=min_ts, cursor=cursor)
        out.extend(orders)
        if not cursor:
            return out


def _order_key(order: dict) -> str:
    """Stable per-order dedup key: the exchange ``order_id`` (always present on live
    payloads); a content hash guards the pathological case where it is missing."""
    oid = order.get("order_id")
    if oid:
        return str(oid)
    key = "|".join(str(order.get(k, "")) for k in
                   ("ticker", "side", "client_order_id", "created_time"))
    return "hash:" + hashlib.sha1(key.encode()).hexdigest()[:16]


def _ts_or_none(value) -> datetime | None:
    """Parse a stored/meta timestamp, or ``None`` when absent or unreadable."""
    if not value:
        return None
    try:
        return parse_iso(str(value))
    except (ValueError, TypeError):
        return None


def _scan_start(ledger, *, full: bool) -> tuple[datetime, datetime | None] | None:
    """``(window_start, seen_through)`` for the shared-account scan, or ``None`` to skip.

    The window BASE is ``live_genesis_ts`` when the live era has begun, falling back to
    the paper-era ``genesis_ts`` (EF-2: the paper genesis is months earlier and re-paging
    from it every 15 minutes is a cost that only ever grows). With no genesis at all there
    is nothing to scan.

    From the base, the start moves forward to ``impostor_seen_through - 24h``: everything
    older than that was already classified, and the day of overlap is deliberate slack for
    an order that becomes visible late or carries a slightly-off ``created_time``.
    """
    base_iso = ledger.meta_get("live_genesis_ts") or ledger.meta_get("genesis_ts")
    if not base_iso:
        return None
    base = parse_iso(str(base_iso))
    seen_through = _ts_or_none(ledger.meta_get("impostor_seen_through"))
    if full or seen_through is None:
        return base, seen_through
    start = seen_through - _SCAN_OVERLAP
    return (start if start > base else base), seen_through


def _personal_seen(ledger) -> tuple[datetime | None, set[str], set[str], set[str]]:
    """``(horizon, recent_keys, undated_keys, legacy_keys)`` from
    ``meta.personal_orders_seen``.

    The key used to hold every order key ever observed — a list that only ever grew
    (ST-11). It now holds ``{"through": iso, "recent": [...], "undated": [...]}``:

    * ``through`` (the **audit horizon**) is the start of the overlap window as of the last
      pass. Anything strictly older was fully accounted for by an earlier pass and is never
      audited again — which is what stops the WEEKLY FULL SCAN, whose fetch window is much
      wider, from re-auditing the entire personal history every time it runs.
    * ``recent`` is the keys of orders at or after that horizon: bounded by 24 hours of
      account activity, not by account age. This is what makes the overlap real. A
      watermark alone cannot tell "already audited" from "new, but dated an hour ago", so
      an order that becomes visible late would be silently dropped — precisely what §10
      forbids.
    * ``undated`` is orders with no readable ``created_time``. No horizon can describe
      them, so their keys persist; in practice the live feed always dates its orders.

    A legacy list is read one last time as "already audited": the first pass after the
    upgrade has no ``impostor_seen_through``, so it covers the full window, sees every
    listed order, and writes the new shape.
    """
    raw = ledger.meta_get("personal_orders_seen")
    if not raw:
        return None, set(), set(), set()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None, set(), set(), set()
    if isinstance(data, list):
        return None, set(), set(), {str(k) for k in data}
    if isinstance(data, dict):
        return (
            _ts_or_none(data.get("through")),
            {str(k) for k in (data.get("recent") or [])},
            {str(k) for k in (data.get("undated") or [])},
            set(),
        )
    return None, set(), set(), set()


def _personal_state(horizon: datetime | None, recent: set[str], undated: set[str]) -> dict:
    """The serialized shape of ``meta.personal_orders_seen`` (see :func:`_personal_seen`)."""
    return {
        "through": iso(horizon) if horizon is not None else None,
        "recent": sorted(recent),
        "undated": sorted(undated),
    }


def _adjust_group_realization(ledger, bet: dict, delta: Decimal) -> str | None:
    """Move a settled group's cached ``realized_pnl`` by the same ``delta`` (docs/25).

    The one sum in this ledger that is stored rather than computed from the bet rows.
    ``_settle_groups`` stamps it once and never revisits a group it has settled, so a leg
    corrected afterwards would leave it stale. Nothing new can reach this: groups left the
    execution path with the rebuild, so every group in the ledger belongs to an earlier
    era. It is kept consistent anyway, because a stored total that disagrees with the rows
    under it is exactly the kind of thing nobody finds until it matters.
    """
    gid = bet["group_id"]
    if not gid:
        return None
    group = next(
        (g for g in ledger.groups_for_attempt(bet["attempt_id"]) if g["group_id"] == gid),
        None,
    )
    if group is None or group["realized_pnl"] is None:
        return None
    adjusted = q4(D(str(group["realized_pnl"])) - delta)
    ledger.set_group(gid, realized_pnl=adjusted)
    return str(adjusted)


def _correct_fee(ledger, bet: dict | None, order: dict) -> bool:
    """Heal one of our own rows whose stored fee is not what the exchange charged (docs/25).

    The client used to build a total by multiplying the exchange's per-contract fee
    average, which it has already rounded, by the fill count. At one contract a leg the
    two agreed; at three they parted by a hundredth of a cent, and the three legs of
    2026-09-17 put the first nightly walk under the new regime $0.0003 out. The client
    stores the reported total now, so no new row carries the error. This is what heals the
    rows already written, from the same order payload the scan is already holding, so the
    next settle pass fixes them and the walk in the same tick goes back to exact.

    It also heals new rows, in the other direction (2026-09-27). The create response
    usually carries no fee total, so placement stores the harness's model priced at the
    average fill price, and for an order that filled at several prices that is not what
    the exchange charged. A-0316-B01 was stored at $0.0245 and this corrected it to the
    $0.0243 the listing reported. That was right: the listing's figure equals the sum of
    the order's per-fill fees and matches the money the balance moved. The walk still went
    $0.0002 out that night, because the same leg's stake was $0.0002 short and the model's
    fee had been hiding it. :func:`_correct_stake` heals that half.

    A **settled** row is corrected too, and its ``pnl`` moves with its fee: ``pnl`` is
    ``payout - stake - fee``, so a fee that rises by a hundredth of a cent lowers the P/L
    by the same amount and every other term stays where it was. Both columns are written
    in one update, because a row carrying one without the other is a row that does not add
    up. Leaving settled rows alone was the first shape of this function and it was wrong
    for a reason worth stating: once fills stop adding to it, an uncorrected ledger sits at
    a CONSTANT sub-cent drift. That drift halted the system on its third night under the
    repeated-drift rule, and since 2026-09-27 it would be absorbed into the walk instead,
    which is quieter but still a number the ledger has wrong.

    A **voided** row is never touched: its fee is deliberately zeroed by
    :func:`_settle_bet` and the walk's refund term is built on that. Nor is a row whose
    count disagrees with the order's, which is ``fills_match``'s business, nor a settled
    row with no ``pnl`` to move.
    """
    if bet is None or bet["fee"] is None or bet["status"] not in ("filled", "settled"):
        return False
    charged = reported_fee(order)
    if charged is None:
        return False
    filled = parse_count(order, "fill_count")
    if filled is None or D(str(bet["contracts"] or "0")) != filled:
        return False
    charged = q4(charged)
    stored = q4(D(str(bet["fee"])))
    if stored == charged:
        return False
    detail = {
        "ticker": order.get("ticker"), "order_id": order.get("order_id"),
        "status": bet["status"], "contracts": str(filled),
        "stored_fee": str(stored), "charged_fee": str(charged),
    }
    if bet["status"] == "settled":
        if bet["pnl"] is None:
            return False
        delta = q4(charged - stored)
        stored_pnl = q4(D(str(bet["pnl"])))
        corrected_pnl = q4(stored_pnl - delta)
        ledger.update_bet(bet["bet_id"], fee=charged, pnl=corrected_pnl)
        detail.update(stored_pnl=str(stored_pnl), corrected_pnl=str(corrected_pnl))
        realized = _adjust_group_realization(ledger, bet, delta)
        if realized is not None:
            detail["group_realized_pnl"] = realized
    else:
        ledger.update_bet(bet["bet_id"], fee=charged)
    ledger.audit("fee_corrected", attempt_id=bet["attempt_id"], bet_id=bet["bet_id"],
                 detail=detail)
    return True


def _correct_stake(ledger, bet: dict | None, order: dict) -> bool:
    """Heal one of our own rows whose stake is not what the exchange charged (2026-09-27).

    The stake is the fee's twin in the balance walk and went wrong the same way, as a
    figure built from a rounded average. An order that fills in pieces at several prices
    has an average price that does not fit four decimal places, so ``contracts x
    fill_price`` misses the exchange's own cost. Placement takes the average from the
    create response, which the exchange truncates, and settlement used to round the fills'
    average again: A-0316-B01 bought 3 contracts for $0.4043 and was booked at $0.4041,
    then at $0.4044. Five live legs put the walk $0.0004 out. This reads the cost off the
    same order payload :func:`_correct_fee` reads (``kalshi.types.reported_cost``), which on
    every leg checked equals the sum over that order's fills and the money the balance
    moved.

    A **settled** win or loss has its ``pnl`` rebuilt from the corrected stake and the
    row's fee. It is rebuilt rather than moved by the difference because a row settled
    before this function existed took its P/L from ``contracts x fill_price``, which is not
    the stake it stored. A **scalar** row's payout survives on the row only as ``pnl +
    stake + fee``, so its ``pnl`` moves by the difference instead, which leaves the payout
    the walk recovers where it was. Voided rows, rows whose count disagrees with the
    order's, and settled rows with no ``pnl`` are left alone, for :func:`_correct_fee`'s
    reasons.
    """
    if bet is None or bet["status"] not in ("filled", "settled"):
        return False
    cost = reported_cost(order)
    if cost is None:
        return False
    filled = parse_count(order, "fill_count")
    if filled is None or D(str(bet["contracts"] or "0")) != filled:
        return False
    charged = q4(cost)
    stored = _row_stake(bet)
    if stored == charged:
        return False
    detail = {
        "ticker": order.get("ticker"), "order_id": order.get("order_id"),
        "status": bet["status"], "contracts": str(filled),
        "stored_stake": str(stored), "charged_stake": str(charged),
    }
    if bet["status"] == "settled":
        if bet["pnl"] is None:
            return False
        stored_pnl = q4(D(str(bet["pnl"])))
        fee_amt = D(str(bet["fee"])) if bet["fee"] is not None else _ZERO
        if bet["outcome"] == _SCALAR:
            corrected_pnl = q4(stored_pnl - (charged - stored))
        elif bet["outcome"] in ("win", "loss"):
            corrected_pnl = bet_pnl(bet["outcome"], filled, bet["fill_price"], fee_amt,
                                    stake=charged)
        else:
            return False  # no outcome this function knows how to re-price
        ledger.update_bet(bet["bet_id"], stake=charged, pnl=corrected_pnl)
        detail.update(stored_pnl=str(stored_pnl), corrected_pnl=str(corrected_pnl))
        realized = _adjust_group_realization(ledger, bet, q4(stored_pnl - corrected_pnl))
        if realized is not None:
            detail["group_realized_pnl"] = realized
    else:
        ledger.update_bet(bet["bet_id"], stake=charged)
    ledger.audit("stake_corrected", attempt_id=bet["attempt_id"], bet_id=bet["bet_id"],
                 detail=detail)
    return True


def _canary_order_ids(ledger) -> set[str]:
    """Both identifiers of the harness's own canary order, or an empty set (docs/24).

    The canary lives in ``meta.canary`` rather than in ``bets``, and it is placed with a
    deliberately non-matching ``client_order_id`` (``CANARY-<epoch>``) so the impostor
    tripwire's pattern cannot claim it. The scan below read that as "not ours, not an
    impostor, therefore an outside order" and filed the canary as a personal order on the
    first full scan. The balance walk then counted the same $0.0661 twice, once in its
    canary term and once in its personal term, and that is what refused a $100 deposit at
    go-live on 2026-09-16.

    Both identifiers are collected because the orders feed shows the canary under either:
    the ``coid`` it was placed with, and the exchange ``order_id`` its response carried.
    """
    from betting_agent.harness.reconcile import _load_canary

    canary = _load_canary(ledger)
    if not canary:
        return set()
    ids = set()
    if canary.get("coid"):
        ids.add(str(canary["coid"]))
    response = canary.get("response")
    if isinstance(response, dict) and response.get("order_id"):
        ids.add(str(response["order_id"]))
    return ids


def _shared_account_scan(ledger, client, settings, counts: dict, *,
                         full: bool = False, now: datetime | None = None) -> None:
    """Classify post-genesis ORDERS (spec §10). Absent genesis → scan is skipped.

    The canary is checked FIRST and skipped whole (docs/24): it carries a deliberately
    non-matching ``client_order_id``, so every branch below would misread it, and the one
    that did filed it as an outside order and double-counted its cost in the walk.

    ``client_order_id`` matches ``CLIENT_ORDER_ID_RE`` and is in the ledger → ours;
    matches but is NOT in the ledger → impostor: ``unknown_fill`` audit + HALT
    (re-audited every run — the halt stops the world, and a standing impostor must
    stay loud); absent/foreign → personal: ``personal_fill_observed`` audited once per
    order, keyed on ``created_time`` against ``meta.personal_orders_seen``, AND written to
    ``personal_orders`` on every sighting (docs/22 section 7.3) so the balance walk has a
    term for it. The row goes in before the once-per-order dedupe on purpose: an order
    classified before that table existed is otherwise never written down, and the weekly
    full scan is what backfills it.

    **Incremental, and the §10 ethos still holds (EF-2).** This ran on every 15-minute
    tick — ~96 times a day — and re-paged the entire order history since the *paper*
    genesis each time, growing without bound. It now starts at
    ``impostor_seen_through - 24h``. Nothing is silently dropped, and the guarantees are
    named here so a future edit cannot quietly remove one:

    * a **24-hour overlap** on every pass, so an order that surfaces late is still seen —
      and genuinely audited, not merely re-read: the overlap window's order keys are held
      alongside the audit horizon precisely so "already audited" and "new, but dated an
      hour ago" stay distinguishable (see :func:`_personal_seen`);
    * a **weekly full scan** from genesis (``full=True``, date-keyed in the tick), which
      re-judges everything the incremental window has moved past;
    * the watermark **does not advance when an impostor was found**, so a standing
      impostor keeps being re-detected — and re-HALTing — until a human deals with it,
      rather than ageing out of the window while the system is stopped;
    * this is the system's only impostor check since docs/22 section 7.3 removed
      reconcile's duplicate, which is why the three guarantees above are the ones that
      matter rather than a second opinion arriving at 23:00.
    """
    window = _scan_start(ledger, full=full)
    if window is None:
        return
    start, seen_through = window
    now = now or utc_now()

    our_coids = {
        r["client_order_id"]
        for r in ledger.conn.execute(
            "SELECT client_order_id FROM bets WHERE client_order_id IS NOT NULL"
        ).fetchall()
    }
    canary_ids = _canary_order_ids(ledger)
    # Our own rows that carry a fee, by the coid the order carries, so the loop below can
    # compare a stored fee and stake against the charged ones without a query per order.
    # Deliberately a second read rather than a widening of ``our_coids``: that set is the
    # impostor tripwire's input and its population must not move.
    our_rows = {
        r["client_order_id"]: r
        for r in ledger.conn.execute(
            "SELECT * FROM bets WHERE client_order_id IS NOT NULL "
            "AND status IN ('filled','settled')"
        ).fetchall()
    }
    # Real rows only: netting is about positions the exchange actually holds, and a paper
    # row is not one.
    our_tickers = {
        r["ticker"]
        for r in ledger.conn.execute(
            "SELECT DISTINCT ticker FROM bets WHERE is_real=1"
        ).fetchall()
    }
    horizon, recent, undated, legacy = _personal_seen(ledger)
    known = recent | undated | legacy
    # ``impostor_seen_through`` is stored as the exchange's OWN ``created_time`` string,
    # never a reformatted one: ``timeutil.iso`` truncates to whole seconds, and a window
    # boundary that rounds backwards re-reads that second on every pass forever.
    newest_order: datetime | None = None
    newest_order_raw = None
    processed: list[tuple[str, datetime | None]] = []

    for o in _page_orders(client, start):
        raw_created = o.get("created_time")
        created = parse_ts(raw_created)
        if created is not None and (newest_order is None or created > newest_order):
            newest_order, newest_order_raw = created, str(raw_created)
        coid = o.get("client_order_id")
        oid = o.get("order_id")
        if (coid and str(coid) in canary_ids) or (oid and str(oid) in canary_ids):
            # The harness's own canary (see _canary_order_ids). Not ours by coid pattern,
            # not an outside order, not an impostor: it is already a term in the balance walk
            # through meta.canary, so the scan's job here is to leave it alone and to undo
            # any personal row an earlier pass wrote for it.
            removed = ledger.delete_personal_order(_order_key(o))
            if removed:
                ledger.audit("personal_order_withdrawn", detail={
                    "ticker": o.get("ticker"), "order_id": o.get("order_id"),
                    "client_order_id": coid,
                    "reason": "this is the harness's own canary, already in the walk",
                })
            continue
        if coid and CLIENT_ORDER_ID_RE.match(coid):
            if coid in our_coids:
                # Ours: reconciled in the settle loop. What is worth reading off the order
                # here is what the exchange actually charged: the fee (docs/25) and the
                # cost of the contracts (2026-09-27). The row is read again after a fee
                # correction so the stake correction starts from the row as it now stands.
                row = our_rows.get(coid)
                if _correct_fee(ledger, row, o):
                    row = ledger.conn.execute(
                        "SELECT * FROM bets WHERE bet_id=?", (row["bet_id"],)
                    ).fetchone()
                _correct_stake(ledger, row, o)
                continue
            ledger.audit("unknown_fill", detail={
                "client_order_id": coid, "ticker": o.get("ticker"),
                "order_id": o.get("order_id"), "created_time": o.get("created_time"),
            })
            _set_halt(settings, f"impostor order detected: {coid}")
            counts["impostors"] += 1
            continue
        key = _order_key(o)
        processed.append((key, created))
        # The row goes in on every sighting; the audits below still fire once per order.
        unrecorded = _record_personal_order(ledger, settings, o, key, our_tickers, now)
        if key in known:
            continue  # audited already — by a previous pass or the legacy key list
        if created is not None and horizon is not None and created < horizon:
            # Older than the audit horizon: an earlier pass covered this ground in full.
            # Only the weekly full scan reaches back here, and it must not re-audit.
            continue
        ledger.audit("personal_fill_observed", detail={
            "ticker": o.get("ticker"), "side": o.get("side"),
            "order_id": o.get("order_id"), "created_time": o.get("created_time"),
        })
        if unrecorded is not None:
            # The balance walk has no term for this order, so its money will show up as
            # drift. Say why here, so the person the drift alert wakes can find it.
            ledger.audit("personal_order_unrecorded", detail={
                "ticker": o.get("ticker"), "side": o.get("side"),
                "action": o.get("action"), "order_id": o.get("order_id"),
                "created_time": o.get("created_time"), "reason": unrecorded,
            })
        counts["personal_seen"] += 1

    updates: dict = {}
    if (
        newest_order is not None
        and not counts["impostors"]
        and (seen_through is None or newest_order > seen_through)
    ):
        updates["impostor_seen_through"] = newest_order_raw
    # The horizon trails the newest order by the overlap and only ever moves forward;
    # ``recent`` is then exactly the keys the NEXT pass's window will serve back to us,
    # which is what keeps it bounded by a day of activity rather than by account age.
    new_horizon = (newest_order - _SCAN_OVERLAP) if newest_order is not None else horizon
    if new_horizon is not None and horizon is not None and new_horizon < horizon:
        new_horizon = horizon
    payload = _personal_state(
        new_horizon,
        {k for k, ts in processed if ts is not None
         and (new_horizon is None or ts >= new_horizon)},
        undated | {k for k, ts in processed if ts is None},
    )
    if legacy or payload != _personal_state(horizon, recent, undated):
        updates["personal_orders_seen"] = json.dumps(payload, sort_keys=True)
    if updates:
        # One transaction (OR-5): both keys describe the same pass, and the legacy-list
        # migration is only sound if it lands together with the state that replaces it.
        ledger.meta_set_many(updates)


# ------------------------------------------------------------------ owner correction
def correct_scalar_settlement(ledger, *, bet_id: str, revenue: Decimal, fee: Decimal,
                              now: datetime | None = None,
                              dry_run: bool = False) -> dict:
    """Rewrite ONE bet that a scalar settlement was mis-booked as a void (docs/16 §5).

    The narrow, owner-run repair for rows already in the ledger when the code learned what
    a scalar settlement is. It exists for exactly one row — A-0097-B01, booked ``voided``
    on 2026-08-15 against an exchange that had paid $0.82 and kept a $0.0132 fee — and its
    preconditions are written so it can only ever touch that shape:

    * the bet must exist and be ``voided``. A ``settled`` row is refused, which is what
      makes a second run a no-op rather than a double correction; a ``filled`` row is
      refused too, because that one belongs to the settle pass, not to a hand correction.
    * ``revenue`` and ``fee`` are supplied EXPLICITLY by the operator, read off the
      exchange, and the P/L is derived from them rather than from anything the ledger
      already believes: the ledger's belief is what is being corrected.

    Writes, on a real run: ``status='settled'``, ``outcome='scalar'``, the restored
    ``fee``, and ``pnl = revenue − contracts x fill_price − fee`` — and ONE
    ``scalar_settlement_corrected`` audit row carrying before and after in full.

    ``settled_at`` is deliberately NOT moved. The void stamped the moment the market
    actually finalized; the correction is bookkeeping about that moment, not a new one, and
    the balance walk windows credits on ``settled_at`` (rewriting it to "now" would move
    the payout into a different reconciliation's window).

    ``dry_run=True`` computes and returns everything and writes nothing at all — not the
    row, not the audit event.

    Returns a result dict; ``ok`` is the caller's exit status, ``message`` is the line to
    print. Never writes on ``not ok``.
    """
    now = now or utc_now()
    revenue = q4(D(revenue))
    fee_amt = q4(D(fee))

    row = ledger.conn.execute(
        "SELECT * FROM bets WHERE bet_id=?", (bet_id,)
    ).fetchone()
    if row is None:
        return {"ok": False, "reason": "no_such_bet", "bet_id": bet_id,
                "message": f"refusing: no bet {bet_id} in this ledger"}
    if row["status"] != "voided":
        was = row["status"] if not row["outcome"] else f"{row['status']} / {row['outcome']}"
        return {
            "ok": False, "reason": "not_voided", "bet_id": bet_id,
            "status": row["status"], "outcome": row["outcome"],
            "message": (
                f"refusing: {bet_id} is '{was}', not 'voided'. This command corrects a "
                "scalar settlement that was mis-booked as a void and nothing else — a row "
                "that is already settled needs no correction, and a row still filled "
                "belongs to the settle pass."
            ),
        }
    if row["contracts"] is None or row["fill_price"] is None:
        return {"ok": False, "reason": "no_execution_data", "bet_id": bet_id,
                "message": (f"refusing: {bet_id} has no contracts/fill_price recorded, so "
                            "there is no stake to net the payout against")}

    contracts = D(str(row["contracts"]))
    fill_price = D(row["fill_price"])
    pnl = scalar_pnl(revenue, contracts, fill_price, fee_amt)
    before = {"status": row["status"], "outcome": row["outcome"],
              "pnl": None if row["pnl"] is None else str(row["pnl"]),
              "fee": None if row["fee"] is None else str(row["fee"]),
              "settled_at": row["settled_at"]}
    after = {"status": "settled", "outcome": _SCALAR, "pnl": str(pnl),
             "fee": str(fee_amt), "settled_at": row["settled_at"]}
    detail = {
        "bet_id": bet_id, "ticker": row["ticker"], "side": row["side"],
        "contracts": str(contracts), "fill_price": str(fill_price),
        "revenue": str(revenue), "fee": str(fee_amt),
        "derivation": (f"payout {revenue} − stake {q4(contracts * fill_price)} "
                       f"(= {contracts} x {fill_price}) − fee {fee_amt} = pnl {pnl}"),
        "before": before, "after": after,
        "corrected_at": iso(now), "dry_run": dry_run,
        # Running the command IS the authorization: an owner keystroke, unreachable from
        # the tick (mirrors reconcile.record_balance_adjustment's own note).
        "authorized_by": "owner (betting-agent correct-scalar-settlement)",
    }
    message = (
        f"{'would correct' if dry_run else 'corrected'} {bet_id}: "
        f"status {before['status']} -> settled, outcome {before['outcome']} -> {_SCALAR}, "
        f"fee {before['fee']} -> {fee_amt}, pnl {before['pnl']} -> {pnl} "
        f"(settled_at {row['settled_at']} unchanged)\n"
        f"  derivation: {detail['derivation']}"
    )
    if dry_run:
        return {"ok": True, "reason": "dry_run", "bet_id": bet_id, "dry_run": True,
                "pnl": pnl, "fee": fee_amt, "detail": detail,
                "message": message + "\n  --dry-run: NOTHING was written"}

    # Audit first, then the row: a crash between the two leaves a recorded intention over
    # an unchanged row (loud at the next reconcile), never a changed row with no record of
    # why — the ordering ``record_balance_adjustment`` settled on for the same reason.
    ledger.audit("scalar_settlement_corrected", attempt_id=row["attempt_id"],
                 bet_id=bet_id, detail=detail)
    ledger.update_bet(bet_id, status="settled", outcome=_SCALAR, pnl=pnl, fee=fee_amt)
    return {"ok": True, "reason": None, "bet_id": bet_id, "dry_run": False,
            "pnl": pnl, "fee": fee_amt, "detail": detail, "message": message}


# --------------------------------------------------------------------------- public
def genesis_snapshot(ledger, client) -> None:
    """Write ``meta.genesis_ts`` and a balance/fill-count snapshot, once (spec §10).

    Idempotent: a no-op if ``genesis_ts`` is already set. The snapshot is a paper-trail
    record; nothing before ``genesis_ts`` is ever read again.

    **Order matters (MP-5).** The fills paging happens FIRST and the two keys are stamped
    together, in one transaction, last. Stamping the timestamp up front meant a transient
    failure in the (fallible, multi-page) fills call left ``genesis_ts`` set and
    ``genesis_snapshot`` absent — and because the presence of ``genesis_ts`` is exactly
    what makes this a no-op, the personal-fill baseline was then lost permanently, with no
    retry. Now a failure anywhere above the stamp simply leaves nothing written and the
    next ``init`` tries again.
    """
    if ledger.meta_get("genesis_ts") is not None:
        return
    now = utc_now()
    try:
        balance = client.get_balance().dollars
    except Exception:  # noqa: BLE001 - snapshot is best-effort paper trail
        balance = None
    fill_count = len(_page_fills(client, None))
    ledger.meta_set_many({
        "genesis_ts": iso(now),
        "genesis_snapshot": json.dumps({
            "ts": iso(now),
            "balance": str(balance) if balance is not None else None,
            "fill_count": fill_count,
        }),
    })


def settle_once(ledger, client, settings, now: datetime | None = None, *,
                full_scan: bool = False) -> dict:
    """One settlement pass (spec §10). Returns summary counts.

    ``counts["errors"]`` is the pass's own partial-failure signal (MP-2): a bet whose
    settlement raised, or a market this pass could not reach even after the retry. It is
    never fatal here — every later stage still runs — but the tick reads it and defers
    the nightly reconciliation, because a pass that did not see the whole world must not
    let the balance walk HALT on the settlement it missed (MP-1, decision D1).

    ``full_scan=True`` makes the shared-account scan re-read the whole post-genesis order
    history instead of the incremental window (EF-2). The tick asks for it once a week and
    stamps the date when the pass returns; that flag adds no key, so every caller that
    compares this dict whole keeps working across it.

    ``scalars_settled`` (docs/16 §5) is the one key added since: a subset of
    ``bets_settled``, reported so the rarest settlement shape in the system is visible in
    the tick summary and not only in the audit trail.
    """
    now = now or utc_now()
    counts = {
        "bets_settled": 0, "bets_voided": 0, "attempts_settled": 0,
        "groups_settled": 0, "reconcile_mismatches": 0,
        "personal_seen": 0, "personal_settled": 0, "impostors": 0,
        "shadows_scored": 0, "shadows_voided": 0, "canary_settled": 0,
        # docs/14 D12: hypothetical scoring of legs that carried no position. Counted
        # separately from ``bets_settled`` — nothing here settled, and nothing moved money.
        # ``rejects_*`` is the same scoring over the legs the HARNESS refused (a cap, a
        # validation gate) rather than the ones the exchange declined; kept as its own pair
        # because "the market never gave it to us" and "we never asked" are different
        # facts about an attempt, and only one of them is the market's doing.
        "nofills_scored": 0, "nofills_voided": 0,
        "rejects_scored": 0, "rejects_voided": 0,
        # docs/16 §5. A SUBSET of ``bets_settled`` (those rows did settle and did move
        # money), reported alongside it so a rare, easily-missed settlement shape is
        # visible in the tick's own summary rather than only in the audit trail.
        "scalars_settled": 0,
        "errors": 0,
    }

    market_cache: dict[str, object] = {}
    # Lazily populated by the scalar path only; see ``_settlements_index``.
    settlement_cache: dict = {}
    for bet in ledger.filled_unsettled_bets():
        market = _cached_market(client, market_cache, bet["ticker"], counts)
        if market is None or not _is_finalized(market):
            continue
        try:
            _settle_bet(ledger, bet, market, client, settings, now, counts,
                        settlement_cache)
        except Exception as exc:  # noqa: BLE001 - MP-2: one bet must not abort the pass
            counts["errors"] += 1
            ledger.audit("settle_bet_error", attempt_id=bet["attempt_id"],
                         bet_id=bet["bet_id"],
                         detail={"ticker": bet["ticker"],
                                 "error": f"{type(exc).__name__}: {exc}"})

    _settle_groups(ledger, counts)
    _settle_attempts(ledger, counts)
    _settle_shadows(ledger, client, settings, market_cache, now, counts)
    _settle_nofills(ledger, client, settings, market_cache, now, counts)
    _settle_rejects(ledger, client, settings, market_cache, now, counts)
    _settle_canary(ledger, client, settings, market_cache, now, counts)
    # The scan first: an order it records this pass is one this pass can also settle.
    _shared_account_scan(ledger, client, settings, counts, full=full_scan, now=now)
    _settle_personal_orders(ledger, client, now, counts, settlement_cache)
    return counts
