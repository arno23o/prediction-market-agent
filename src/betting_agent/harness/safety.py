"""Safety: kill switch and the live-trading gate (spec §14).

Two independent guards decide whether a real production order may ever be sent:

* **HALT** — the file ``settings.halt_path`` (``data/HALT``). Its presence stops all
  real orders immediately; ``tick`` also exits on it. Created by ``betting-agent halt``,
  removed by ``resume`` — modelled here as :func:`set_halt` / :func:`clear_halt`.
* **Live-trading gate** — ``stakes.live_trading`` (default ``False``). Real *prod*
  orders additionally require it ``True``; flipping it is a human act (§14). No code
  path, prompt, or agent session may set it — this module never writes it.

The demo environment is skipped by decision (``docs/decisions.md``) but remains a
config option: on ``env == "demo"`` real orders are allowed unconditionally (the demo
exchange has no real money), still subject to HALT.

A third, softer guard arrived with live money (Jul29 spec L4): the **drawdown floor**.
When a ledger is available, :func:`real_orders_allowed` also refuses once the account's
cash plus its open positions falls below ``stakes.drawdown_floor_pct`` of the live-era
genesis balance. It only refuses — it does not HALT.
"""

from __future__ import annotations

from decimal import InvalidOperation
from typing import TYPE_CHECKING

from betting_agent.moneymath import D, q4
from betting_agent.timeutil import iso, utc_now

if TYPE_CHECKING:
    from betting_agent.config import Settings

# The machine key a floor refusal carries, on the reason string here and on
# ``bets.reject_code`` in ``execute.py``. One constant so the two can never drift apart.
FLOOR_CODE = "drawdown_floor"


def is_halted(settings: Settings) -> bool:
    """True iff the HALT file exists."""
    return settings.halt_path.exists()


def halt_reason(settings: Settings) -> str | None:
    """The reason recorded in the HALT file, or ``None`` if not halted.

    The file's first line is the reason; a trailing ISO timestamp line is ignored.
    An empty/whitespace HALT file yields an empty string (still halted).
    """
    if not settings.halt_path.exists():
        return None
    text = settings.halt_path.read_text()
    first = text.splitlines()[0].strip() if text.strip() else ""
    return first


def set_halt(settings: Settings, reason: str) -> None:
    """Create the HALT file with ``reason`` and an ISO timestamp (one per line)."""
    settings.halt_path.parent.mkdir(parents=True, exist_ok=True)
    settings.halt_path.write_text(f"{reason}\n{iso(utc_now())}\n")


def clear_halt(settings: Settings) -> None:
    """Remove the HALT file if present (idempotent)."""
    settings.halt_path.unlink(missing_ok=True)


def _open_cost(ledger):
    """Stake plus fee of the real legs still open: the money out at the exchange, at cost."""
    total = D("0")
    for b in ledger.filled_unsettled_bets():
        if b["is_real"]:
            total += D(str(b["stake"] or 0)) + D(str(b["fee"] or 0))
    return total


def _floor_balance(ledger, client) -> tuple[str, str, object, str] | None:
    """``(cash, cash source, positions, positions source)`` for the floor (docs/22 7.4).

    The floor reads the account's cash *plus* its open positions (Arno, 2026-10-01). It
    used to read cash alone, so money waiting on open legs looked like money lost: with
    $100 out on five-day legs and $96 of cash, the floor sat $21 from refusing every
    ticket while the account was up. A drawdown floor should trip on losses, not on
    capital in flight.

    Cash prefers a live read, because the reconciliation is as stale as the last night
    and stops under a HALT; the stored reconciliation stays as the fallback for a failed
    call and for callers with no client. Positions come from the same live call (the
    exchange's market value, so a position marked down counts as the loss it is), and
    from the ledger's open legs at cost when the call carries no value or did not happen.
    Each source is named, so a refusal says which numbers refused it.
    """
    if client is not None:
        try:
            balance = client.get_balance()
        except Exception:  # noqa: BLE001 - any client failure falls back to the record
            balance = None
        if balance is not None:
            positions = getattr(balance, "positions", None)
            if positions is not None:
                return str(balance.dollars), "live balance", positions, "positions at market"
            return str(balance.dollars), "live balance", _open_cost(ledger), "positions at cost"
    latest = ledger.latest_reconciliation()
    if latest is None:
        return None
    cash = str(latest["actual_balance"])
    return cash, "last reconciliation", _open_cost(ledger), "positions at cost"


def _drawdown_refusal(settings: Settings, ledger, client=None) -> str | None:
    """The drawdown-floor refusal reason, or ``None`` when real orders may proceed (L4).

    Needs both a live-era genesis balance and a balance to compare it with; absent either
    there is nothing to compare, so the floor cannot trip. A pre-live or freshly-flipped
    account is not in drawdown. A *present but unreadable* number is a different animal:
    the floor is then unevaluable, and an unevaluable money guard must not authorize new
    spend, so it refuses with its own reason.
    """
    genesis = ledger.meta_get("live_genesis_balance")
    if genesis is None:
        return None
    found = _floor_balance(ledger, client)
    if found is None:
        return None
    raw, source, positions, positions_source = found
    try:
        floor = D(settings.stakes.drawdown_floor_pct) * D(str(genesis))
        cash = D(raw)
        held = D(str(positions))
    except InvalidOperation:
        return f"{FLOOR_CODE}: unreadable balance"
    actual = cash + held
    if actual >= floor:
        return None
    return (
        f"{FLOOR_CODE}: {source} {q4(cash)} plus {positions_source} {q4(held)} is "
        f"{q4(actual)}, below the {q4(floor)} floor "
        f"({settings.stakes.drawdown_floor_pct} of the {q4(D(str(genesis)))} genesis)"
    )


def real_orders_allowed(settings: Settings, ledger=None, client=None) -> tuple[bool, str]:
    """Whether real orders may be sent right now, with a human-readable reason.

    Order of precedence (§14, extended by Jul29 spec L4 and docs/22 section 7.4):

    1. HALT present            -> ``(False, "halted: <reason>")``
    2. ``env == "demo"``       -> ``(True, "demo")`` (no real money on demo)
    3. ``live_trading == True`` -> ``(True, "live_trading")``
    4. otherwise               -> ``(False, "live_trading disabled")``
    5. …then, on either allowing outcome above and only when a ``ledger`` is supplied:
       the **drawdown floor**, i.e. ``meta.live_genesis_balance`` exists and the account's
       cash plus open positions is below ``stakes.drawdown_floor_pct × live_genesis_balance``
       -> ``(False, "drawdown_floor: …")``.

    The cash the floor reads is the exchange's own when a ``client`` is supplied, and the
    latest reconciliation otherwise or when the call fails; positions are the exchange's
    market value from that same call, or the ledger's open legs at cost; the reason says
    which.

    The floor refuses new real orders; it never writes HALT (reconcile owns HALT, §8).
    Callers with no ledger in hand skip the floor check entirely, which keeps the pure
    gate-truth-table callers (CLI status) unchanged.
    """
    if is_halted(settings):
        return False, f"halted: {halt_reason(settings) or ''}"
    if settings.kalshi.env == "demo":
        allowed, why = True, "demo"
    elif settings.stakes.live_trading:
        allowed, why = True, "live_trading"
    else:
        return False, "live_trading disabled"

    if ledger is not None:
        refusal = _drawdown_refusal(settings, ledger, client)
        if refusal is not None:
            return False, refusal
    return allowed, why
