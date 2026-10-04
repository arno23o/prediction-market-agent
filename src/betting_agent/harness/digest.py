"""The status digest (docs/22 section 10).

One page of Markdown that replaces the 107 KB daily report and the eight-section audit as
the thing a person reads to know what the system did and whether it is healthy. The tick
writes it once a day to ``data/status/<date>.md``; ``betting-agent status`` prints it on
demand; phase three stages the same text into the director's workspace.

Everything here reads. It opens no locks, writes no rows and raises no alerts: the
``invariants`` tick step is what alerts on a failing check, and this page only reports
what that step found. A section with nothing to say says so in one line rather than
printing an empty table, because the page is meant to be read in full every day and
sixty lines of headings with nothing under them teaches a reader to skim.

Money is ``Decimal`` end to end and rendered at cents, which is the unit a person
reconciles in. The ledger stores 4dp; the rounding happens here, at the last moment.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from betting_agent.harness.invariants import run_invariants
from betting_agent.harness.reconcile import residual_summary
from betting_agent.harness.safety import halt_reason, is_halted
from betting_agent.moneymath import D, q4
from betting_agent.timeutil import et_day, iso, parse_iso, utc_now

_CENT = Decimal("0.01")
_ZERO = D("0")

# Statuses meaning the exchange gave us the position, whatever happened to it since.
_EVER_FILLED = ("filled", "settled", "voided")

# How far back the trailing windows look. Seven days is the spec's number for both the
# category table and the cost-per-settled-leg line.
_TRAILING_DAYS = 7

_TOP_REASONS = 3

# The legs the harness held back for money before any order was sent: the daily,
# per-market and per-attempt caps, the drawdown floor, and V16, the validator's copy of
# the attempt allowance. Their rows carry ``is_real = 0`` because nothing was sent, which
# kept every one of them out of the funnel below until 2026-09-27. Each was a real leg
# refused, so the funnel counts them as refused and names their reasons.
_HELD_BACK = frozenset({"cap_daily", "cap_market", "cap_attempt", "drawdown_floor", "V16"})


def _cents(x) -> str:
    """``$12.34``. Every money figure on the page goes through here."""
    try:
        value = D(str(x if x is not None else "0"))
    except (InvalidOperation, ValueError, TypeError):
        return "$?"
    return f"${value.quantize(_CENT)}"


def _dec(x) -> Decimal:
    try:
        return D(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return _ZERO


def _day_of(ts: str | None) -> str | None:
    """The ET day a stored UTC timestamp falls on, or ``None`` when unreadable."""
    if not ts:
        return None
    try:
        return et_day(parse_iso(str(ts)))
    except (ValueError, TypeError):
        return None


def _since_days(day: str, n: int) -> str:
    """The ET day ``n`` days before ``day``, as the inclusive floor of a trailing window."""
    try:
        start = datetime.fromisoformat(day) - timedelta(days=n - 1)
    except (ValueError, TypeError):
        return day
    return start.strftime("%Y-%m-%d")


def _counts_line(counts: Counter, empty: str = "none") -> str:
    if not counts:
        return empty
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))


# --------------------------------------------------------------------------- sections
def _account(ledger, settings, client, now: datetime) -> list[str]:
    lines = ["## Account", ""]
    latest = ledger.latest_reconciliation()
    if latest is None:
        lines.append("- reconciliation: none recorded")
    else:
        verdict = _verdict_of(latest)
        lines.append(
            f"- reconciliation: {verdict}, drift {_cents(latest['drift'])}, "
            f"{latest['run_at']}"
        )
    residual = residual_summary(ledger, settings, now)
    if residual is not None:
        # 4dp, unlike every other figure on the page: an absorbed drift is under fifty
        # cents by definition and usually under one, so rounding it to the cent would
        # print $0.00 for the very number this line exists to show.
        lines.append(
            f"- absorbed residual: ${residual['total']} over {residual['nights']} "
            f"night(s) ({residual['window_days']}d: ${residual['window_abs']} over "
            f"{residual['window_nights']})"
        )
    balance, source = _balance(ledger, client)
    lines.append(f"- balance: {balance} ({source})")

    # Real legs only. ``filled_unsettled_bets`` has no ``is_real`` filter, because settle
    # walks paper legs too, but money committed at the exchange is money that actually
    # left the account, and counting a paper fill in it would overstate the exposure a
    # person reads this line for. Filtered here rather than in the DAO, whose other caller
    # wants both.
    open_bets = [b for b in ledger.filled_unsettled_bets() if b["is_real"]]
    committed = sum((_dec(b["stake"]) + _dec(b["fee"]) for b in open_bets), _ZERO)
    lines.append(
        f"- open positions: {len(open_bets)} real leg(s), {_cents(q4(committed))} committed"
    )

    genesis_ts = ledger.meta_get("live_genesis_ts")
    personal = ledger.personal_orders_summary(genesis_ts)
    lines.append(
        f"- personal orders since genesis: {personal['n']}, "
        f"net cost {_cents(personal['net_cost'])}"
    )

    # Beside the personal orders because they are the same kind of fact: money that moved
    # in this account without a bet of ours behind it. Unlike a personal order, a credit is
    # only here because somebody read it off the app and typed it in, so a line that says
    # zero is also the reminder that nobody has looked.
    given = ledger.credits_since(genesis_ts)
    lines.append(
        f"- exchange credits since genesis: {len(given)}, "
        f"{_cents(q4(sum((_dec(c['amount']) for c in given), _ZERO)))}"
    )
    if is_halted(settings):
        lines.append(f"- halt: YES ({halt_reason(settings)})")
    else:
        lines.append("- halt: no")
    lines.append("")
    return lines


def _verdict_of(row) -> str:
    """The verdict the reconciliation row records: ``exact``, ``absorbed``, ``reversed``,
    ``noted`` or ``large`` since 2026-09-27, and ``small`` on rows written before then.

    The verdict is in the row's own detail JSON. Rows written before the three-way verdict
    existed carry only ``ok``, and the honest reading of those is the two-way one they were
    written under.
    """
    try:
        detail = json.loads(row["detail"] or "{}")
        verdict = detail.get("verdict")
        if verdict:
            return str(verdict)
    except (ValueError, TypeError):
        pass
    return "exact" if row["ok"] else "large"


def _balance(ledger, client) -> tuple[str, str]:
    if client is not None:
        try:
            return _cents(client.get_balance().dollars), "live"
        except Exception:  # noqa: BLE001 - a failed read falls back to the record
            pass
    latest = ledger.latest_reconciliation()
    if latest is None:
        return "unknown", "no reconciliation yet"
    return _cents(latest["actual_balance"]), "last reconciliation"


def _bets_on(ledger, day: str) -> list[dict]:
    """Every leg belonging to ``day``, by its placement time or its attempt's creation.

    A rejected or unfilled leg has no ``placed_at``, and dropping those would leave the
    refusal counts describing a different population from the fill counts beside them.
    """
    rows = ledger.conn.execute(
        "SELECT b.*, a.created_at AS attempt_created_at FROM bets b "
        "JOIN attempts a ON a.attempt_id = b.attempt_id"
    ).fetchall()
    return [r for r in rows
            if _day_of(r["placed_at"] or r["attempt_created_at"]) == day]


def _the_day(ledger, settings, day: str) -> list[str]:
    lines = ["## The day", ""]

    by_cell: dict[str, Counter] = {}
    for row in ledger.conn.execute(
        "SELECT cell, status, created_at FROM attempts"
    ).fetchall():
        if _day_of(row["created_at"]) != day:
            continue
        cell = row["cell"] or "(none)"
        by_cell.setdefault(cell, Counter())[row["status"]] += 1
    if by_cell:
        for cell in sorted(by_cell):
            lines.append(f"- attempts, {cell}: {_counts_line(by_cell[cell])}")
    else:
        lines.append("- attempts: none")

    # One funnel, one population. ``proposed`` is every leg the models wrote, because that
    # is what a proposal is; every stage after it is real legs only, because a paper leg
    # was never sent, never accepted, never filled and never refused by anyone. Mixing the
    # two would give a funnel whose stages do not describe the same legs. A leg held back
    # for money (``_HELD_BACK``) counts as real here: it was refused, not papered.
    bets = _bets_on(ledger, day)
    real = [b for b in bets if b["is_real"] or b["reject_code"] in _HELD_BACK]
    sent = [b for b in real if b["status"] != "rejected"]
    accepted = [b for b in sent if b["order_id"]]
    filled = [b for b in real if b["status"] in _EVER_FILLED]
    refused = [b for b in real if b["status"] == "rejected"]
    lines.append(
        f"- legs: proposed {len(bets)}, sent {len(sent)}, accepted {len(accepted)}, "
        f"filled {len(filled)}, refused {len(refused)}"
    )
    reasons = Counter(b["reject_reason"] or b["reject_code"] or "(unrecorded)"
                      for b in refused)
    if reasons:
        top = "; ".join(f"{r} ({n})" for r, n in reasons.most_common(_TOP_REASONS))
        lines.append(f"- top refusals: {top}")

    sizes = Counter(int(b["contracts"]) for b in filled if b["contracts"] is not None)
    lines.append("- contracts placed: " + (
        ", ".join(f"{size}x{n}" for size, n in sorted(sizes.items())) if sizes else "none"
    ))

    spend = ledger.daily_real_spend(day)
    cap = _dec(settings.stakes.daily_real_stake_cap)
    lines.append(f"- stake committed: {_cents(spend)} of {_cents(cap)}")
    lines.append("")
    return lines


def _the_board(ledger, settings) -> list[str]:
    from betting_agent import board as board_mod

    lines = ["## The board", ""]
    latest = board_mod.latest(settings.board_dir)
    if latest is None:
        lines.append("- generation: none")
    else:
        try:
            # The label is two lines: what the snapshot is, and what its header says the
            # pull left out. One bullet each, so neither swallows the other.
            snapshot, scope = latest.label_lines()
            generation = f"{snapshot}, {latest.n_markets} markets"
        finally:
            latest.close()
        lines.append(f"- generation: {generation}")
        lines.append(f"- scope: {scope or 'none'}")
    refusal = ledger.meta_get("category_refusal_text")
    lines.append(f"- exchange refusal text: {refusal or 'none'}")
    lines.append("")
    return lines


def _by_category(ledger, day: str) -> list[str]:
    lines = ["## Fills and refusals by category", ""]
    since = _since_days(day, _TRAILING_DAYS)
    day_counts: dict[str, Counter] = {}
    week_counts: dict[str, Counter] = {}
    for row in ledger.conn.execute(
        "SELECT b.category AS category, b.status AS status, b.placed_at AS placed_at, "
        "a.created_at AS created_at FROM bets b "
        "JOIN attempts a ON a.attempt_id = b.attempt_id"
    ).fetchall():
        when = _day_of(row["placed_at"] or row["created_at"])
        if when is None or when > day or when < since:
            continue
        key = "filled" if row["status"] in _EVER_FILLED else (
            "refused" if row["status"] in ("rejected", "no_fill") else None
        )
        if key is None:
            continue
        category = row["category"] or "(none)"
        week_counts.setdefault(category, Counter())[key] += 1
        if when == day:
            day_counts.setdefault(category, Counter())[key] += 1
    if not week_counts:
        lines.append("- none in the last seven days")
        lines.append("")
        return lines
    lines.append("| category | filled today | refused today | filled 7d | refused 7d |")
    lines.append("|---|---|---|---|---|")
    for category in sorted(week_counts):
        d = day_counts.get(category, Counter())
        w = week_counts[category]
        lines.append(
            f"| {category} | {d['filled']} | {d['refused']} | "
            f"{w['filled']} | {w['refused']} |"
        )
    lines.append("")
    return lines


def _settlements(ledger, since_ts: str) -> list[str]:
    lines = ["## Settlements since the previous digest", ""]
    rows = ledger.conn.execute(
        "SELECT b.*, a.slot AS slot FROM bets b "
        "JOIN attempts a ON a.attempt_id = b.attempt_id "
        "WHERE b.settled_at IS NOT NULL"
    ).fetchall()
    fresh = [r for r in rows if str(r["settled_at"]) > since_ts]
    if not fresh:
        lines.append("- none")
        lines.append("")
        return lines
    wins = sum(1 for r in fresh if r["outcome"] == "win")
    net = sum((_dec(r["pnl"]) for r in fresh), _ZERO)
    lines.append(f"- legs {len(fresh)}, wins {wins}, net {_cents(q4(net))}")
    days = sorted({_slot_day(r["slot"]) for r in fresh} - {None})
    complete = [d for d in days if _cohort_complete(ledger, d)]
    lines.append("- cohorts complete: " + (", ".join(complete) if complete else "none"))
    lines.append("")
    return lines


def _slot_day(slot: str | None) -> str | None:
    """The Eastern day a stored ``slot:YYYY-MM-DD/HH:MM`` names (docs/22 section 8.1)."""
    if not slot:
        return None
    raw = slot[len("slot:"):] if slot.startswith("slot:") else slot
    head = raw.split("/", 1)[0]
    return head if len(head) == 10 else None


def _cohort_complete(ledger, day: str) -> bool:
    """Every real leg terminal and every refused or unfilled leg hypothetically scored."""
    rows = ledger.conn.execute(
        "SELECT b.status AS status, b.hypothetical_scored_at AS scored, a.slot AS slot "
        "FROM bets b JOIN attempts a ON a.attempt_id = b.attempt_id"
    ).fetchall()
    for row in rows:
        if _slot_day(row["slot"]) != day:
            continue
        if row["status"] in ("settled", "voided"):
            continue
        if row["status"] in ("rejected", "no_fill"):
            if row["scored"] is None:
                return False
            continue
        return False  # a real leg still open
    return True


def _groups_line(groups: dict[str, list]) -> str:
    """``name count ($cost)`` per group, sorted by name."""
    return ", ".join(f"{name} {len(costs)} ({_cents(q4(sum(costs, _ZERO)))})"
                     for name, costs in sorted(groups.items()))


def _compute(ledger, day: str) -> list[str]:
    """The day's sessions by kind, then the attempt sessions by model and effort.

    The model is the session row's, which is the model that ran, substitution included.
    The effort is the attempt row's, because a session row does not carry one.
    """
    lines = ["## Compute", ""]
    by_kind: dict[str, list] = {}
    by_arm: dict[str, list] = {}
    for row in ledger.conn.execute(
        "SELECT s.kind AS kind, s.model AS model, s.started_at AS started_at, "
        "s.cost_usd AS cost_usd, a.effort AS effort FROM sessions s "
        "LEFT JOIN attempts a ON a.attempt_id = s.attempt_id"
    ).fetchall():
        if _day_of(row["started_at"]) != day:
            continue
        cost = _dec(row["cost_usd"])
        by_kind.setdefault(row["kind"], []).append(cost)
        if row["kind"] == "attempt":
            arm = f"{row['model']} {row['effort'] or '(none)'}"
            by_arm.setdefault(arm, []).append(cost)
    lines.append("- sessions today: " + (_groups_line(by_kind) if by_kind else "none"))
    if by_arm:
        lines.append("- attempts by model and effort: " + _groups_line(by_arm))
    lines.append(f"- cost per settled leg (7d): {_cost_per_leg(ledger, day)}")
    lines.append("")
    return lines


def _cost_per_leg(ledger, day: str) -> str:
    since = _since_days(day, _TRAILING_DAYS)
    cost = _ZERO
    for row in ledger.conn.execute("SELECT started_at, cost_usd FROM sessions").fetchall():
        when = _day_of(row["started_at"])
        if when is None or when > day or when < since:
            continue
        cost += _dec(row["cost_usd"])
    legs = 0
    for row in ledger.conn.execute(
        "SELECT settled_at FROM bets WHERE status='settled'"
    ).fetchall():
        when = _day_of(row["settled_at"])
        if when is not None and since <= when <= day:
            legs += 1
    if not legs:
        return f"no settled legs ({_cents(q4(cost))} of compute)"
    return f"{_cents(q4(cost / legs))} ({_cents(q4(cost))} over {legs} leg(s))"


def _liveness(ledger, since_ts: str) -> list[str]:
    lines = ["## Liveness", ""]
    lines.append(f"- last tick: {ledger.meta_get('last_tick_ts') or 'never'}")
    missed: list[str] = []
    for event, marker in (("slot_skipped", "skipped"), ("slot_spawn_lost", "lost")):
        for row in ledger.audit_events(event=event, limit=200):
            if str(row["ts"]) <= since_ts:
                continue
            try:
                detail = json.loads(row["detail"] or "{}")
            except (ValueError, TypeError):
                detail = {}
            slot = detail.get("slot", "(unknown)")
            reason = detail.get("reason") or marker
            missed.append(f"{slot} {marker} ({reason})")
    lines.append("- missed slots: " + (", ".join(sorted(missed)) if missed else "none"))
    lines.append("")
    return lines


def _invariants(ledger) -> list[str]:
    lines = ["## Invariants", ""]
    for row in run_invariants(ledger):
        if row.get("skipped"):
            lines.append(f"- {row['name']}: SKIP ({row['detail']})")
        elif row["ok"]:
            lines.append(f"- {row['name']}: PASS")
        else:
            lines.append(f"- {row['name']}: FAIL, {row['detail']}")
    return lines


def _since_previous_digest(ledger, now: datetime) -> str:
    """The floor of the two "since the previous digest" windows.

    ``meta.last_digest_ts`` once there has been one. Before that there is no previous
    digest, and treating its absence as "since forever" made the first page list the
    ledger's whole lifetime of settlements and every slot ever missed, which is a wall of
    history where a day's news should be. A day is the honest default: it is the cadence
    the page is written at, so the first page covers the same span as the second.
    """
    stamp = ledger.meta_get("last_digest_ts")
    if stamp:
        return str(stamp)
    return iso(now - timedelta(days=1))


# --------------------------------------------------------------------------- entry point
def status_digest(ledger, settings, client=None, *, day: str,
                  now: datetime | None = None) -> str:
    """The day's digest as Markdown (docs/22 section 10).

    ``client`` is optional: with one, the account line carries a live balance; without, it
    carries the last reconciliation's and says so. ``day`` is an Eastern calendar date,
    ``YYYY-MM-DD``, and scopes every per-day figure on the page. The two "since the
    previous digest" windows read ``meta.last_digest_ts``, which the tick's digest step
    stamps after each write, and fall back to the last 24 hours before there is one.
    """
    now = now or utc_now()
    since_ts = _since_previous_digest(ledger, now)
    lines = [f"# Status {day}", ""]
    lines += _account(ledger, settings, client, now)
    lines += _the_day(ledger, settings, day)
    lines += _the_board(ledger, settings)
    lines += _by_category(ledger, day)
    lines += _settlements(ledger, since_ts)
    lines += _compute(ledger, day)
    lines += _liveness(ledger, since_ts)
    lines += _invariants(ledger)
    return "\n".join(lines) + "\n"


def status_log_line(ledger, settings, day: str, *, client=None,
                    now: datetime | None = None) -> str:
    """The one line the tick appends to ``data/logs/status.log`` beside the page.

    Deliberately not derived from the page text: the log is a grep target with a fixed
    shape, and parsing Markdown back out to build it would make the shape depend on how
    the page happens to be worded this month. It takes the same ``client`` as the page it
    is written beside, so the two cannot disagree about the balance in one pass.
    """
    now = now or utc_now()
    balance, _source = _balance(ledger, client)
    open_bets = [b for b in ledger.filled_unsettled_bets() if b["is_real"]]
    attempts = 0
    placed = 0
    for row in ledger.conn.execute("SELECT status, created_at FROM attempts").fetchall():
        if _day_of(row["created_at"]) != day:
            continue
        attempts += 1
        if row["status"] in ("placed", "settled", "reviewed"):
            placed += 1
    halted = "yes" if is_halted(settings) else "no"
    return (f"{iso(now)} balance={balance} open={len(open_bets)} attempts={attempts} "
            f"placed={placed} halted={halted}")
