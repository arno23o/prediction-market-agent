"""What past attempts did, rendered as records (docs/22 sections 8.5, 8.6 and 9).

One module answers every question the loop asks about the past, and one function renders
an attempt. ``render_record`` is used identically by ``bt past attempt``, by the director's
workspace and by an attempt's CONTEXT.md, so what the director reads, what the history tool
prints and what an attempt studies are the same text. That is the whole point of putting it
here rather than in three callers.

Everything in this module reads. It opens no locks and writes no rows.

**What a record is not.** The old era graded attempts (a verdict, a hypothesis grade, tags,
a curated playbook) and the record served those judgments back. This one serves what
happened: the claim, the legs, the money, and the director's two paragraphs. The
``retrospectives``, ``tags`` and playbook tables stay in the ledger as history and are not
read here.

**Scale.** The queries load the attempt and bet rows and filter them in Python rather than
building five optional SQL clauses per question. The ledger holds hundreds of attempts and
a few hundred legs; the board is the big data, not this.

**Money.** ``Decimal`` end to end, 4dp as the ledger stores it. Records render dollars at
cents, which is the unit a person reads, and prices at four places, which is the unit the
exchange quotes.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from betting_agent.board import series_of
from betting_agent.moneymath import D, q4
from betting_agent.timeutil import ET, parse_iso

# The short record's budget (docs/22 section 8.5). Ten of these are rendered into a
# CONTEXT.md, so the bound is what keeps the cell's surface small enough to read.
SHORT_MAX = 900

_CLAIM_CHARS = 320
_MARKETS_CHARS = 320
_KILL_CHARS = 160
_REVIEW_CHARS = 300
# The claim as `bt past family` prints it, one line per attempt in a family's history.
_FAMILY_CLAIM_CHARS = 200
# A pass's stated reason as the families table prints it.
_REASON_CHARS = 120

# The new headings (docs/22 section 5.4). Old-era tickets carry `## The edge` and
# `## Why it exists and persists` instead; the claim falls back to the first section for
# them, which is `## Markets` in both eras.
_CLAIM_HEADING = "Why this is profitable"
_MARKETS_HEADING = "Markets"
_KILL_HEADING = "Kill criteria"

# A Kalshi ticker as a ticket names one, which is how a pass, having no bet rows, is
# attributed to the families it looked at. The dashed tail is optional because a ticket
# that passed on a whole ladder often names only the family (`KXHIGHNY`), and a pass that
# says which family it rejected is exactly the pass this counts. Every match goes through
# `series_of`, so a family and one of its strikes land on the same row.
_TICKER_RE = re.compile(r"\bKX[A-Z0-9]+(?:-[A-Z0-9.]+)*\b")

# The refusal sentence the exchange sent back, and how much of it a leg line shows. The
# sentence is worth a whole line: the category block's refusal runs about 140 characters,
# and a leg that says half of it is a leg a reader has to go and look up. A blob with no
# `message` in it was written for a log rather than a reader, so less of it is shown.
_MESSAGE_RE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')
_MESSAGE_CHARS = 160
_REFUSAL_CHARS = 120

_CENT = Decimal("0.01")
_ZERO = D("0")

# A leg that carried a position, whatever happened to it since.
_EVER_FILLED = ("filled", "settled", "voided")
# A leg the exchange or the harness never gave us a position on. Both are scored
# hypothetically by settle, and neither is complete until that score lands.
_REFUSED = ("rejected", "no_fill")
# An attempt whose session has not ended yet.
_UNFINISHED = ("created", "running")


# --------------------------------------------------------------------------- small helpers
def _dec(x) -> Decimal:
    try:
        return D(str(x))
    except (InvalidOperation, ValueError, TypeError):
        return _ZERO


def _money(x) -> str:
    """``+$0.72`` / ``-$1.45``: a signed dollar figure at cents."""
    value = _dec(x).quantize(_CENT)
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value)}"


def _dollars(x) -> str:
    """``$1.45``: an unsigned dollar figure at cents, for stakes."""
    return f"${_dec(x).quantize(_CENT)}"


def _price(x) -> str:
    return f"{q4(_dec(x))}"


def _count(x) -> str:
    """A contract count, integral where it is integral (fractional fills exist)."""
    value = _dec(x)
    return str(int(value)) if value == value.to_integral_value() else str(value.normalize())


def _clip(text: str | None, limit: int) -> str:
    """The first ``limit`` characters of ``text`` on one line.

    Whitespace is collapsed because these fields land on a single line of a record and a
    Markdown paragraph break in the middle of one makes the record unreadable.
    """
    return " ".join((text or "").split())[:limit]


def _section(md: str | None, heading: str) -> str:
    """The body under ``## <heading>``, up to the next heading. ``""`` when absent."""
    if not md:
        return ""
    out: list[str] = []
    taking = False
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if taking:
                break
            taking = stripped.lstrip("#").strip().lower() == heading.lower()
            continue
        if taking:
            out.append(line)
    return "\n".join(out).strip()


def _first_section(md: str | None) -> str:
    """The body after the first heading, whatever that heading is."""
    if not md:
        return ""
    lines = md.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            out: list[str] = []
            for rest in lines[i + 1:]:
                if rest.strip().startswith("#"):
                    break
                out.append(rest)
            return "\n".join(out).strip()
    return md.strip()


def _claim_text(row: dict) -> str:
    """The claim, under the new heading or from an old-era ticket's first section."""
    claim = _section(row.get("edge_claim_md"), _CLAIM_HEADING)
    return claim or _first_section(row.get("edge_claim_md"))


def _slot_parts(row: dict) -> tuple[str, str]:
    """``("2026-08-29", "01:00")`` from ``slot:2026-08-29/01:00``.

    An attempt with no slot (an operator ran it by hand) falls back to the Eastern day and
    time it was created, which is the same clock the slots are named in.
    """
    slot = row.get("slot") or ""
    if slot.startswith("slot:") and "/" in slot:
        day, _, hhmm = slot[len("slot:"):].partition("/")
        return day, hhmm
    try:
        created = parse_iso(row["created_at"]).astimezone(ET)
    except (KeyError, TypeError, ValueError):
        return "", ""
    return created.strftime("%Y-%m-%d"), created.strftime("%H:%M")


def _day(row: dict) -> str:
    return _slot_parts(row)[0]


def _cell_word(row: dict) -> str:
    """``director cell`` for a new attempt, ``live-v1 era`` for one that predates cells."""
    cell = row.get("cell_effective") or row.get("cell")
    if cell:
        return f"{cell} cell"
    era = row.get("era")
    return f"{era} era" if era else "-"


def _arm_word(row: dict) -> str:
    """``claude-opus-5 high``: the model and effort the attempt ran, as far as recorded."""
    return " ".join(part for part in (row.get("model"), row.get("effort")) if part)


def _family_of(bet: dict) -> str:
    return series_of(bet["ticker"] or "")


def _contracts_of(bet: dict):
    """The leg's size. A refused leg carried none, so it reports the size it declared."""
    return bet["contracts"] if bet["contracts"] is not None else bet["declared_contracts"]


def _families_named(row: dict) -> set[str]:
    """The families a ticket names, for an attempt that placed nothing."""
    text = " ".join(
        str(row.get(col) or "") for col in ("edge_claim_md", "hypothesis_md", "manifest_md")
    )
    return {series_of(t) for t in _TICKER_RE.findall(text)}


# --------------------------------------------------------------------------- the world
@dataclass(frozen=True)
class _Entry:
    """One attempt with its legs, the unit every question below is answered over."""

    row: dict
    bets: list[dict]

    @property
    def attempt_id(self) -> str:
        return self.row["attempt_id"]

    @property
    def day(self) -> str:
        return _day(self.row)

    @property
    def passed(self) -> bool:
        return not self.bets

    def families(self) -> set[str]:
        return {_family_of(b) for b in self.bets}

    def categories(self) -> set[str]:
        return {b["category"] for b in self.bets if b["category"]}


def _entries(ledger) -> list[_Entry]:
    """Every attempt with its legs attached, oldest first."""
    rows = ledger.conn.execute("SELECT * FROM attempts ORDER BY seq").fetchall()
    bets: dict[str, list[dict]] = {}
    for bet in ledger.conn.execute("SELECT * FROM bets ORDER BY ticket_index, bet_id"):
        bets.setdefault(bet["attempt_id"], []).append(bet)
    return [_Entry(row, bets.get(row["attempt_id"], [])) for row in rows]


def _leg_matches(bet: dict, outcome: str) -> bool:
    """One leg against one ``--outcome`` value.

    A leg the exchange refused is stored as ``no_fill`` carrying the refusal text, so it
    answers to both ``refused`` (something turned it down, and the row says what) and
    ``nofill`` (no position resulted). The two sets overlap on exactly those rows, which is
    the honest reading: leaving them out of ``refused`` is what hid the category block.
    """
    if outcome == "win":
        return bet["outcome"] == "win"
    if outcome == "loss":
        return bet["outcome"] == "loss"
    if outcome == "refused":
        return bet["status"] == "rejected" or (
            bet["status"] == "no_fill" and bool(bet["reject_reason"])
        )
    if outcome == "nofill":
        return bet["status"] == "no_fill"
    return False


def _keeps(entry: _Entry, *, era, category, outcome, since) -> bool:
    """The shared filters of ``bt past`` (docs/22 section 9), applied to one attempt."""
    if era and entry.row.get("era") != era:
        return False
    if category and category not in entry.categories():
        return False
    if since and entry.day < since:
        return False
    if outcome == "pass":
        return entry.passed
    if outcome:
        return any(_leg_matches(b, outcome) for b in entry.bets)
    return True


def _filtered(ledger, *, era, category, outcome, since) -> list[_Entry]:
    return [
        e for e in _entries(ledger)
        if _keeps(e, era=era, category=category, outcome=outcome, since=since)
    ]


# --------------------------------------------------------------------------- completeness
def _complete(row: dict | None, bets: list[dict]) -> bool:
    """The docs/22 section 8.1 rule, over rows already in hand."""
    if row is None or row["status"] in _UNFINISHED:
        return False
    for bet in bets:
        if bet["status"] in ("settled", "voided"):
            continue
        if bet["status"] in _REFUSED and bet["hypothetical_scored_at"] is not None:
            continue
        return False
    return True


def is_complete(ledger, attempt_id: str) -> bool:
    """Has this attempt finished being an attempt (docs/22 section 8.1)?

    Every real leg it placed has reached a terminal state, and every refused or unfilled
    leg has been scored hypothetically by settle. An attempt that placed nothing is
    complete once its session ended, which includes a failed session with no ticket: there
    is nothing left to happen to it.
    """
    row = ledger.get_attempt(attempt_id)
    return _complete(row, ledger.bets_for_attempt(attempt_id) if row else [])


def cohort(ledger, day: str) -> list[str]:
    """Every attempt whose slot falls on one Eastern day (docs/22 section 8.1).

    Membership is the slot's date and never the placement time, so an attempt that places
    after midnight still belongs to the day its slot was named for. A failed session with
    no ticket is a member like any other: it is reviewed with an empty record.
    """
    return [e.attempt_id for e in _entries(ledger) if e.day == day]


def cohort_days(ledger) -> list[str]:
    """Every Eastern day that has a cohort, oldest first."""
    return sorted({e.day for e in _entries(ledger) if e.day})


def _has_ticket(row: dict) -> bool:
    return bool(row.get("edge_claim_md"))


def _no_ticket(row: dict) -> bool:
    """A session that failed before it wrote a ticket (docs/22 section 8.1).

    It is a member of its cohort and it is complete, but it did not pass on anything: it
    never got as far as having something to pass on. Calling it a pass put words in its
    mouth, and the sections it has no text for are simply absent from its record.
    """
    return row["status"] == "failed" and not _has_ticket(row)


def recent_completed(ledger, *, limit: int | None = 10, era: str | None = None) -> list[str]:
    """The most recently completed attempts, newest first: the static cell's set.

    ``limit=None`` returns the whole set, which is how a caller counts what it is capping.
    Any cell, any era, any outcome. A failed session with no ticket is complete but is not
    an example of anything, so it is left out here while staying a member of its cohort.

    Completeness means settlement, so an attempt joins the set days after its session ends:
    Arno chose that on 2026-09-21, because an example whose bets have no outcomes yet is not
    an example he wants a session to learn from, and the lag is the price of it.
    """
    done = [
        e for e in _entries(ledger)
        if _complete(e.row, e.bets)
        and not _no_ticket(e.row)
        and (not era or e.row.get("era") == era)
    ]
    done.sort(key=lambda e: (_slot_parts(e.row), e.row["created_at"] or ""), reverse=True)
    return [e.attempt_id for e in done[:limit]]


# --------------------------------------------------------------------------- the outcome
def outcome_record(ledger, attempt_id: str) -> dict:
    """Legs, totals and per-family totals for one attempt (docs/22 section 8.6).

    This is the outcome half of what used to be a retrospective row: computed from the bet
    rows at read time rather than written once by a grader, so it cannot disagree with the
    ledger.
    """
    bets = ledger.bets_for_attempt(attempt_id)
    legs = []
    for bet in bets:
        legs.append({
            "bet_id": bet["bet_id"],
            "ticker": bet["ticker"],
            "family": _family_of(bet),
            "side": bet["side"],
            "price": q4(_dec(bet["fill_price"] if bet["fill_price"] else bet["limit_price"])),
            "contracts": _contracts_of(bet),
            "status": bet["status"],
            "outcome": bet["outcome"],
            "profit": q4(_dec(bet["pnl"])) if bet["pnl"] is not None else None,
            "fee": q4(_dec(bet["fee"])) if bet["fee"] is not None else None,
        })
    families = []
    for name in sorted({_family_of(b) for b in bets}):
        families.append({"family": name, **_leg_totals(
            [b for b in bets if _family_of(b) == name]
        )})
    return {
        "attempt_id": attempt_id,
        "legs": legs,
        "totals": _leg_totals(bets),
        "families": families,
    }


def _leg_totals(bets: list[dict]) -> dict:
    """Legs, wins, stake and net over a set of bet rows."""
    return {
        "legs": len(bets),
        "wins": sum(1 for b in bets if b["outcome"] == "win"),
        "stake": q4(sum((_dec(b["stake"]) for b in bets), _ZERO)),
        "net": q4(sum((_dec(b["pnl"]) for b in bets), _ZERO)),
    }


# --------------------------------------------------------------------------- the record
def _leg_line(bet: dict) -> str:
    """``KXA-T3 no @0.6200 ×2 → filled, won, +$0.72``."""
    price = _price(bet["fill_price"] if bet["fill_price"] else bet["limit_price"])
    head = f"{bet['ticker']} {bet['side']} @{price}"
    contracts = _contracts_of(bet)
    if contracts is not None:
        head += f" ×{_count(contracts)}"
    return f"{head} → {_leg_state(bet)}"


_OUTCOME_WORDS = {"win": "won", "loss": "lost", "void": "void", "scalar": "scalar"}


def _leg_state(bet: dict) -> str:
    """What became of one leg, in the words the record shows.

    The exchange's own refusals are the case worth naming. They are stored as ``no_fill``
    with the refusal text in ``reject_reason``, and until 2026-09-21 they rendered as a
    bare "no fill", which is what a limit set too low looks like. Sixty-eight Politics legs
    refused under the exchange's residency rule for this account read that way, and the
    two attempts that studied the history concluded the prices were wrong and raised their
    limits from 0.32 to 0.90. A refusal says so (docs/22 section 7.5); a leg that simply
    did not fill still says "no fill".
    """
    if bet["status"] == "rejected":
        reason = bet["reject_reason"] or bet["reject_code"] or "no reason recorded"
        return f"refused: {reason}"
    if bet["status"] == "no_fill":
        reason = _short_reason(bet["reject_reason"])
        return f"refused by the exchange: {reason}" if reason else "no fill"
    if bet["outcome"]:
        word = _OUTCOME_WORDS.get(bet["outcome"], bet["outcome"])
        return f"filled, {word}, {_money(bet['pnl'])}"
    return "open"


def _short_reason(reason: str | None) -> str:
    """The human sentence inside an exchange refusal, or the first of whatever there is.

    A stored refusal is ``f"{type(exc).__name__}: {exc}"`` over a ``KalshiAPIError``, so it
    reads ``KalshiAPIError: HTTP 403: Forbidden — {"error":{"code":…,"message":"Residents
    of this state are not currently allowed…"``, with the body cut at 300 characters by the
    exception itself. The sentence a model needs is the ``message``, and the cut means the
    blob often does not parse as JSON, so it is found by pattern rather than by parsing.
    The captured text goes back through ``json.loads`` as a JSON string, which is what
    turns ``\\"`` and ``\\n`` into the characters they stand for.
    """
    text = " ".join((reason or "").split())
    if not text:
        return ""
    found = _MESSAGE_RE.search(text)
    if not found:
        return _clip(text, _REFUSAL_CHARS)
    try:
        return _clip(json.loads(f'"{found.group(1)}"'), _MESSAGE_CHARS)
    except ValueError:
        return _clip(found.group(1), _MESSAGE_CHARS)


def _header_line(row: dict, bets: list[dict]) -> str:
    day, hhmm = _slot_parts(row)
    parts = [row["attempt_id"], f"{day} {hhmm}".strip(), _cell_word(row)]
    arm = _arm_word(row)
    if arm:
        parts.append(arm)
    if bets:
        parts.append(bets[0]["category"] or "-")
        parts.append(_family_of(bets[0]))
    else:
        parts.append("no ticket" if _no_ticket(row) else "passed")
    return " · ".join(parts)


def _review_line(reviews: dict) -> str | None:
    """The retrospective paragraph when there is one, else the prospective, else nothing."""
    row = reviews["retrospective"] or reviews["prospective"]
    return None if row is None else f"Review: {_clip(row['paragraph'], _REVIEW_CHARS)}"


def _activity_line(activity: dict | None, row: dict) -> str:
    """What the attempt did, from its activity row (docs/22 sections 8.2 and 5.6)."""
    if activity is None:
        return "no activity row recorded"
    try:
        series = json.loads(activity["series_list"] or "[]")
    except (TypeError, ValueError):
        series = []
    parts = [
        f"probed {activity['markets_probed']} markets in {activity['series_probed']} families",
        f"series: {', '.join(series) or '-'}",
        f"web {activity['web_fetches']} fetches / {activity['web_searches']} searches",
        f"{activity['n_domains']} domains",
        f"{activity['code_runs']} code runs",
        f"{activity['files_written']} files written",
        f"{activity['past_calls'] or 0} bt past calls",
        f"{_minutes(row)} minutes",
        f"cost {_dollars(row['cost_usd'])}",
    ]
    return " · ".join(parts)


def _minutes(row: dict) -> int:
    return int((row["wall_seconds"] or 0) // 60)


def _money_so_far(bets: list[dict]) -> str:
    """The money, said so that an unfinished attempt cannot read as a finished one.

    The static set now shows attempts the day they end rather than the day they settle, so
    most of what it shows has legs still running. A bare net on those is a number that
    looks final and is not. A leg is still running while it is ``filled``: it holds a
    position the exchange has not resolved.
    """
    totals = _leg_totals(bets)
    net, stake = _money(totals["net"]), _dollars(totals["stake"])
    open_legs = sum(1 for b in bets if b["status"] == "filled")
    if not open_legs:
        return f"{net} on {stake} staked"
    plural = "leg" if open_legs == 1 else "legs"
    return f"open ({open_legs} {plural} unsettled), {net} so far on {stake} staked"


def _net_line(bets: list[dict], activity: dict | None, row: dict) -> str:
    probed = activity["markets_probed"] if activity else None
    return (
        f"Net: {_money_so_far(bets)} · "
        f"probed {probed if probed is not None else '?'} markets · {_minutes(row)} minutes"
    )


def render_record(ledger, attempt_id: str, *, full: bool = False) -> str:
    """One attempt as the loop reads it (docs/22 section 8.5).

    The short form is at most :data:`SHORT_MAX` characters and is what a CONTEXT.md holds
    ten of. The full form is the three ticket files verbatim with every leg, the outcome,
    the activity row, the session's closing paragraph and both director paragraphs; it is
    what the director's workspace holds and what ``bt past attempt`` prints.

    Old-era attempts render without error. Their tickets carry the old headings, they have
    no cell and no director paragraphs, and what is missing is simply absent from the
    record rather than rendered as a blank field.
    """
    row = ledger.get_attempt(attempt_id)
    if row is None:
        raise KeyError(attempt_id)
    bets = ledger.bets_for_attempt(attempt_id)
    reviews = reviews_for(ledger, attempt_id)
    activity = ledger.activity(attempt_id)
    if full:
        return _render_full(ledger, row, bets, reviews, activity)
    return _render_short(row, bets, reviews, activity)


def _render_short(row: dict, bets: list[dict], reviews: dict, activity: dict | None) -> str:
    lines = [_header_line(row, bets)]
    empty = not bets and _no_ticket(row)
    if not empty:
        lines.append(f"Claim: {_clip(_claim_text(row), _CLAIM_CHARS)}")
    if bets:
        lines.append(f"Bets: {_leg_line(bets[0])}")
        lines.extend(f"      {_leg_line(b)}" for b in bets[1:])
    elif empty:
        lines.append("Bets: none (session failed, no ticket)")
    else:
        markets = _clip(_section(row.get("edge_claim_md"), _MARKETS_HEADING), _MARKETS_CHARS)
        lines.append(f"Bets: none (passed) {markets}".rstrip())
    if not empty:
        lines.append(
            f"Kill: {_clip(_section(row.get('hypothesis_md'), _KILL_HEADING), _KILL_CHARS)}"
        )
    lines.append(_net_line(bets, activity, row))
    review = _review_line(reviews)
    if review:
        lines.append(review)
    text = "\n".join(lines)
    # The per-field caps can sum past the budget on a record that is long everywhere. The
    # bound is what the CONTEXT.md budget is built on, so it wins, and the cut is visible.
    return text if len(text) <= SHORT_MAX else text[:SHORT_MAX - 1].rstrip() + "…"


def _render_full(ledger, row: dict, bets: list[dict], reviews: dict,
                 activity: dict | None) -> str:
    aid = row["attempt_id"]
    out = [_header_line(row, bets), ""]
    for label, col in (("edge_claim.md", "edge_claim_md"), ("hypothesis.md", "hypothesis_md"),
                       ("MANIFEST.md", "manifest_md")):
        if row[col]:
            out += [f"## {label}", "", row[col].strip(), ""]
    out += ["## Legs", ""]
    empty = "none (session failed, no ticket)" if _no_ticket(row) else "none (passed)"
    out += [_leg_line(b) for b in bets] or [empty]
    out.append("")
    record = outcome_record(ledger, aid)
    totals = record["totals"]
    out += ["## Outcome", ""]
    out.append(f"{_money_so_far(bets)} · {totals['wins']} of {totals['legs']} legs won")
    for fam in record["families"]:
        out.append(
            f"{fam['family']}: {_money(fam['net'])} on {_dollars(fam['stake'])} staked · "
            f"{fam['wins']} of {fam['legs']} legs won"
        )
    out += ["", "## Activity", "", _activity_line(activity, row), ""]
    out += ["## Session summary", "", (row["session_summary"] or "none recorded").strip(), ""]
    out += ["## Review", ""]
    said = False
    for kind in ("prospective", "retrospective"):
        review = reviews[kind]
        if review is None:
            continue
        said = True
        out.append(
            f"{kind} (rank {review['rank']} of {review['cohort_size']}, "
            f"cohort {review['cohort_date']}): {review['paragraph'].strip()}"
        )
    if not said:
        out.append("none yet")
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- the director
def reviews_for(ledger, attempt_id: str) -> dict:
    """The attempt's two director paragraphs, either of which may be missing."""
    out: dict = {"prospective": None, "retrospective": None}
    for row in ledger.conn.execute(
        "SELECT * FROM attempt_reviews WHERE attempt_id=?", (attempt_id,)
    ):
        out[row["kind"]] = row
    return out


def latest_valid_run(ledger, *, on_or_before: str | None = None) -> dict | None:
    """The newest valid director run, optionally on or before a run date.

    Every consumer of the direction reads this, which is why a failed or invalid run blocks
    nothing: the day simply runs on the last page that validated.
    """
    sql = "SELECT * FROM director_runs WHERE status='valid'"
    params: list = []
    if on_or_before:
        sql += " AND run_date <= ?"
        params.append(on_or_before)
    sql += " ORDER BY run_date DESC, started_at DESC LIMIT 1"
    return ledger.conn.execute(sql, params).fetchone()


# --------------------------------------------------------------------------- eras
def era_default(ledger, settings) -> str:
    """``current`` once the current era is big enough to answer from, else ``all``.

    A new era starts empty, and a history tool that silently searched an empty era would
    tell every attempt of the first week that nothing has ever been tried.
    """
    current = settings.history.current_era
    row = ledger.conn.execute(
        "SELECT COUNT(*) AS n FROM attempts WHERE era=?", (current,)
    ).fetchone()
    return "current" if (row["n"] if row else 0) >= settings.history.era_default_min else "all"


# --------------------------------------------------------------------------- families
def families(ledger, *, era=None, category=None, outcome=None, since=None,
             limit=50) -> list[dict]:
    """One row per family: attempts, legs, filled, wins, net, passes and the pass reason.

    A family is a ticker's series prefix (``board.series_of``). An attempt "entered" a
    family by placing a leg in it; an attempt that placed nothing is counted as a pass
    against every family its ticket named, which is the "this family has been passed on N
    times" question docs/20 asked and nothing else answers.
    """
    rows: dict[str, dict] = {}
    reasons: dict[str, Counter] = {}

    def _row(name: str) -> dict:
        return rows.setdefault(name, {
            "family": name, "attempts": 0, "legs": 0, "filled": 0, "wins": 0,
            "net": _ZERO, "passes": 0, "last_entry": "", "pass_reason": "",
        })

    for entry in _filtered(ledger, era=era, category=category, outcome=outcome, since=since):
        if entry.passed:
            reason = _clip(_claim_text(entry.row), _REASON_CHARS)
            for name in _families_named(entry.row):
                _row(name)["passes"] += 1
                if reason:
                    reasons.setdefault(name, Counter())[reason] += 1
            continue
        for name in entry.families():
            legs = [b for b in entry.bets if _family_of(b) == name]
            row = _row(name)
            row["attempts"] += 1
            row["legs"] += len(legs)
            row["filled"] += sum(1 for b in legs if b["status"] in _EVER_FILLED)
            row["wins"] += sum(1 for b in legs if b["outcome"] == "win")
            row["net"] += sum((_dec(b["pnl"]) for b in legs), _ZERO)
            row["last_entry"] = max(row["last_entry"], entry.day)
    for name, counter in reasons.items():
        rows[name]["pass_reason"] = counter.most_common(1)[0][0]
    for row in rows.values():
        row["net"] = q4(row["net"])
    out = sorted(rows.values(), key=lambda r: (-r["attempts"], -r["passes"], r["family"]))
    return out[:limit] if limit else out


def family(ledger, series: str, *, era=None, category=None, outcome=None,
           since=None) -> dict:
    """One family's whole history: every attempt that entered it, oldest first, then totals."""
    entries = []
    legs_all: list[dict] = []
    passes = 0
    for entry in _filtered(ledger, era=era, category=category, outcome=outcome, since=since):
        if entry.passed:
            if series in _families_named(entry.row):
                passes += 1
            continue
        legs = [b for b in entry.bets if _family_of(b) == series]
        if not legs:
            continue
        legs_all += legs
        entries.append({
            "attempt_id": entry.attempt_id,
            "day": entry.day,
            "cell": entry.row.get("cell_effective") or entry.row.get("cell") or "-",
            "claim": _clip(_claim_text(entry.row), _FAMILY_CLAIM_CHARS),
            "legs": [{
                "ticker": b["ticker"],
                "side": b["side"],
                "price": q4(_dec(b["fill_price"] if b["fill_price"] else b["limit_price"])),
                "contracts": _contracts_of(b),
                "fill": _leg_state(b),
                "outcome": b["outcome"],
                "profit": q4(_dec(b["pnl"])) if b["pnl"] is not None else None,
            } for b in legs],
            "net": q4(sum((_dec(b["pnl"]) for b in legs), _ZERO)),
            "stake": q4(sum((_dec(b["stake"]) for b in legs), _ZERO)),
        })
    entries.sort(key=lambda e: (e["day"], e["attempt_id"]))
    totals = _leg_totals(legs_all)
    totals["attempts"] = len(entries)
    totals["filled"] = sum(1 for b in legs_all if b["status"] in _EVER_FILLED)
    totals["passes"] = passes
    return {"family": series, "entries": entries, "totals": totals}


# --------------------------------------------------------------------------- search
def search(ledger, text: str, *, era=None, category=None, outcome=None, since=None,
           limit=50) -> list[dict]:
    """Full-text matches over the ticket claims, hypotheses and closing paragraphs.

    The index also holds the old era's retro summaries, lessons and tags. Those are the
    grader's judgments, which this tool does not serve at all, so the kinds are named here
    rather than excluded by era: an ``--era all`` search reads old attempts and still
    returns records, never lessons.
    """
    # Each term is quoted so FTS5 reads hyphens, colons and dots literally: an unquoted
    # `stale-price` or a ticker parses as an operator and raises. Terms are implicitly
    # AND-ed, and an all-whitespace query matches nothing rather than everything.
    terms = " ".join('"' + t.replace('"', '""') + '"' for t in text.split())
    if not terms:
        return []
    # Restricted to the `content` column (FTS5's column filter). Unrestricted, the query
    # also read `kind`, so searching for the word "claim" returned every claim ever
    # written rather than the ones that say it.
    match = f"content : ({terms})"
    hits = ledger.conn.execute(
        "SELECT f.attempt_id AS attempt_id, f.kind AS kind, "
        "snippet(ledger_fts, 2, '[', ']', ' … ', 12) AS snippet "
        "FROM ledger_fts f WHERE ledger_fts MATCH ? AND f.kind IN ('claim','hypothesis','closing')",
        (match,),
    ).fetchall()
    keep = {
        e.attempt_id: e for e in
        _filtered(ledger, era=era, category=category, outcome=outcome, since=since)
    }
    rows = [h for h in hits if h["attempt_id"] in keep]
    rows.sort(key=lambda h: (_slot_parts(keep[h["attempt_id"]].row), h["attempt_id"],
                             h["kind"]), reverse=True)
    return [
        {
            "attempt_id": h["attempt_id"],
            "kind": h["kind"],
            "snippet": h["snippet"],
            "record": render_record(ledger, h["attempt_id"]),
        }
        for h in rows[:limit]
    ]
