"""Ticket parsing and validation (docs/22 sections 5.4 and 5.5; pure functions).

Two stages, both side-effect free:

* :func:`parse_ticket` reads a ``ticket/`` directory into a :class:`ParsedTicket` (the
  markdown files plus ``bets.json``), running the whole-ticket checks: V01 (``bets.json``
  parses and conforms to section 5.4) and V02 (the required H2 headings are present). It
  never touches the network. Every fatal code also lands a human-readable line in
  ``ParsedTicket.error_detail``, because the session repairing its ticket needs the
  reason, not the code (PC-2).
* :func:`validate_ticket` runs the per-leg checks against callables that fetch markets
  and orderbooks (``client.get_market`` / ``client.get_orderbook`` in production, or the
  fake exchange in tests), producing a :class:`ValidationOutcome`. The first failing rule
  wins per bet, and a rejected bet keeps its ``reject_code``.

There are nine codes, and each one has a plain sentence in :data:`REJECT_REASONS`: V01
and V02 above, then V03 (duplicate ticker), V04 (market missing or not open), V05
(resolves outside the window), V07 (price outside the bounds or off the tick band), V10
(``contracts`` is not an integer in range), V11 (the book cannot be read, or holds
less depth than the leg asks for) and V16 (the leg takes the ticket past the attempt's
stake allowance). That sentence is what ``bt ticket validate`` prints and what
``bets.reject_reason`` carries (docs/22 section 7.5); V16 carries a longer one of its
own, with the ticket's total and the allowance in it.

V16 is the next number no code has used. V06, V08, V09 and V12 to V15 were retired with
the rebuild (docs/22 section 2.1), and rows carrying them may still exist, so a retired
number is not reused for a different rule.

Implementation notes:

* **V01 by hand.** No ``jsonschema`` dependency; the shape in section 5.4 is checked
  field by field.
* **Sizing comes from the ticket.** ``contracts`` is declared per bet and bounded by
  ``stakes.max_contracts_per_bet``. Nothing here computes a size, and V11 asks the book
  for the depth the leg actually wants, so a three-contract bet on a one-contract book is
  refused before an order is sent rather than partially filled.
* **Open status.** Per ``docs/decisions.md`` a market is open when ``status ==
  "active"`` (``"finalized"`` when resolved); ``"open"`` is also accepted for the
  ``get_markets`` filter vocabulary.
* **Tick alignment (V07).** Ticks are tapered: the price must align to the
  ``price_ranges`` band covering it (``market.raw["price_ranges"]``), falling back to
  ``market.tick_size`` (the 0.50-band step) when no schedule is present.
* **The attempt allowance (V16, Arno 2026-09-27).** ``stakes.per_attempt_real_cap``
  bounds one attempt's stake, contracts times limit price summed over its legs. The
  limit price is already the price of the side the leg buys, so a NO leg at 0.94 for
  three contracts stakes $2.82, the same projection execute uses for its caps. The
  check runs after every leg has passed V03 to V11 and walks the surviving legs in
  ticket order, the order execute places them: a leg that would take the running total
  past the allowance is refused, and a refused leg adds nothing to the total, so a
  smaller later leg may still fit. The session reads this from ``bt ticket validate``
  before it submits, which is the point: nothing else tells it a budget exists.
  ``execute.py`` refuses a leg past the allowance again, under ``cap_attempt``, as a
  second guard.
* **Optional ``resolution_event``.** A bet MAY carry a free string naming the real-world
  event its resolution shares with other legs, for example an event key or a match id. It
  is passed through unexamined beyond "non-empty, at most 80 characters" onto
  :class:`BetSpec` and, downstream, the ``bets`` column. Measurement only: nothing here
  caps, rejects, or steers on it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from betting_agent.kalshi.client import KalshiAPIError
from betting_agent.moneymath import D, q4

# Open (tradeable) market statuses; resolved markets are "finalized".
_OPEN_STATUSES = frozenset({"active", "open"})

# Required H2 headings (docs/22 section 5.4), matched exactly against a stripped line.
_EDGE_HEADINGS = (
    "## Markets",
    "## Why this is profitable",
    "## Why the opportunity exists and persists",
)
_HYP_HEADINGS = (
    "## If we're right",
    "## If we're wrong",
    "## Kill criteria",
)

# The ticket's shape (docs/22 section 5.4).
_TOP_ALLOWED = frozenset({"attempt", "bets"})
_BET_REQUIRED = frozenset({"ticker", "side", "limit_price", "contracts", "rationale"})
# ``resolution_event`` is the one optional key: a free string a leg MAY declare to name
# the shared real-world event its resolution rides on, e.g. "OWGR-2026-08-03" or a match
# id. Never required, no format beyond non-empty (see ``_bet_errors``).
_BET_ALLOWED = _BET_REQUIRED | {"resolution_event"}

_ATTEMPT_RE = re.compile(r"^A-\d{4}$")
_PRICE_RE = re.compile(r"^0\.\d{4}$")

# The parse bound. The execution bound is ``limits.max_bets_per_attempt``, applied as
# silent truncation in :func:`validate_ticket`.
_MAX_BETS = 50
# A sanity bound on ``resolution_event``, not format policing: the same order of
# magnitude as ``ticker``'s.
_MAX_RESOLUTION_EVENT = 80

# One plain sentence per code (docs/22 section 7.5). The code stays the machine key; this
# is what ``bets.reject_reason`` carries and what ``bt ticket validate`` prints, so a
# refused leg says why in words the models and the reviews read rather than in a number
# they have to look up. V01 and V02 void the whole ticket, so their rows never reach
# execution; their sentence is the headline and ``ParsedTicket.error_detail`` says which
# field or heading was wrong.
REJECT_REASONS: dict[str, str] = {
    "V01": "bets.json is missing, unreadable, or does not match the ticket's shape",
    "V02": "edge_claim.md or hypothesis.md is missing a required heading",
    "V03": "another bet in this ticket already proposed the same ticker",
    "V04": "the market does not exist or is not open for trading",
    "V05": "the market does not resolve inside the attempt's resolution window",
    "V07": "the limit price is outside 0.01 to 0.99, or off the market's tick band",
    "V10": "the declared contract count is not a whole number inside the allowed range",
    "V11": "the order book could not be read, or holds less depth than this bet asked for",
    "V16": "this bet would take the ticket past the attempt's stake allowance",
}


def reason_for_code(code: str | None) -> str | None:
    """The plain sentence for a reject code, or ``None`` for a code with no text yet."""
    return REJECT_REASONS.get(code) if code else None


# --------------------------------------------------------------------------- #
# dataclasses
# --------------------------------------------------------------------------- #
@dataclass
class BetSpec:
    ticket_index: int
    ticker: str
    side: str
    limit_price: Decimal
    contracts: int
    rationale: str
    # The leg's declared shared-resolution key (free string), or None if undeclared.
    resolution_event: str | None = None


@dataclass
class ParsedTicket:
    attempt_id: str
    bets: list[BetSpec]
    edge_claim_md: str | None
    hypothesis_md: str | None
    manifest_md: str | None
    whole_ticket_errors: list[str]  # fatal codes V01 / V02
    # Human-readable reasons behind ``whole_ticket_errors`` (PC-2). The code list above
    # stays codes-only; this is the actionable half, for `bt ticket validate` and the
    # ``ticket_invalid`` audit.
    error_detail: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True iff the ticket cleared the whole-ticket checks (V01/V02)."""
        return not self.whole_ticket_errors


@dataclass
class ValidatedBet:
    spec: BetSpec
    status: str  # "validated" | "rejected"
    reject_code: str | None
    market: Any | None
    book: Any | None
    contracts: Decimal | None
    # A sentence for this leg alone, when the code's own sentence is not enough. Only V16
    # sets it, because its refusal needs the ticket's total and the allowance in it.
    reject_reason: str | None = None


@dataclass
class ValidationOutcome:
    ticket_valid: bool
    whole_ticket_error: str | None
    bets: list[ValidatedBet]
    truncated_count: int


# --------------------------------------------------------------------------- #
# parse (V01, V02)
# --------------------------------------------------------------------------- #
def _read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _has_headings(md: str | None, headings: tuple[str, ...]) -> bool:
    if not md:
        return False
    lines = {ln.strip() for ln in md.splitlines()}
    return all(h in lines for h in headings)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return D(str(value))
    except (InvalidOperation, ValueError):
        return None


def _is_int(value: Any) -> bool:
    """A JSON integer. ``bool`` is an ``int`` subclass; ``true`` is not a count."""
    return isinstance(value, int) and not isinstance(value, bool)


def _bet_errors(i: int, b: Any) -> list[str]:
    if not isinstance(b, dict):
        return [f"bets[{i}] not object"]
    e: list[str] = []
    missing = _BET_REQUIRED - set(b)
    if missing:
        e.append(f"bets[{i}] missing {sorted(missing)}")
    extra = set(b) - _BET_ALLOWED
    if extra:
        e.append(f"bets[{i}] unexpected {sorted(extra)}")
    tick = b.get("ticker")
    if not (isinstance(tick, str) and 1 <= len(tick) <= 80):
        e.append(f"bets[{i}] bad ticker")
    if b.get("side") not in ("yes", "no"):
        e.append(f"bets[{i}] bad side")
    price = b.get("limit_price")
    if not (isinstance(price, str) and _PRICE_RE.match(price)):
        e.append(f"bets[{i}] bad limit_price")
    # Type only. A whole number outside the allowed range is the leg's own failure (V10),
    # not a malformed ticket, so it must not void the other legs here.
    if "contracts" in b and not _is_int(b["contracts"]):
        e.append(f"bets[{i}] bad contracts (want a whole number, not a string or decimal)")
    rat = b.get("rationale")
    if not (isinstance(rat, str) and 1 <= len(rat) <= 400):
        e.append(f"bets[{i}] bad rationale")
    if "resolution_event" in b and not (
        isinstance(b["resolution_event"], str)
        and 1 <= len(b["resolution_event"]) <= _MAX_RESOLUTION_EVENT
    ):
        e.append(f"bets[{i}] bad resolution_event")
    return e


def _schema_errors(data: Any) -> list[str]:
    """Return the ticket's shape errors ([] means valid)."""
    if not isinstance(data, dict):
        return ["top-level must be an object"]
    e: list[str] = []
    extra = set(data) - _TOP_ALLOWED
    if extra:
        e.append(f"unexpected top-level keys {sorted(extra)}")
    if "attempt" not in data:
        e.append("missing attempt")
    elif not (isinstance(data["attempt"], str) and _ATTEMPT_RE.match(data["attempt"])):
        e.append("bad attempt")
    bets = data.get("bets")
    if "bets" not in data:
        e.append("missing bets")
    elif not isinstance(bets, list):
        e.append("bets not an array")
    elif len(bets) > _MAX_BETS:
        e.append(f"too many bets (>{_MAX_BETS})")
    else:
        for i, b in enumerate(bets):
            e += _bet_errors(i, b)
    return e


def _extract(data: dict) -> list[BetSpec]:
    """Best-effort structural extraction (used even when V01 fails, for callers)."""
    bets: list[BetSpec] = []
    for i, b in enumerate(data.get("bets", []) or []):
        if not isinstance(b, dict):
            continue
        bets.append(
            BetSpec(
                ticket_index=i + 1,
                ticker=str(b.get("ticker", "")),
                side=str(b.get("side", "")),
                limit_price=_to_decimal(b.get("limit_price")) or D("0"),
                # 0 when the ticket did not declare a whole number; V01 has already
                # voided such a ticket, and V10 would refuse the leg in any case.
                contracts=b["contracts"] if _is_int(b.get("contracts")) else 0,
                rationale=str(b.get("rationale", "")),
                resolution_event=(
                    b.get("resolution_event")
                    if isinstance(b.get("resolution_event"), str)
                    else None
                ),
            )
        )
    return bets


def parse_ticket(ticket_dir: Path) -> ParsedTicket:
    """Read a ``ticket/`` directory, running the whole-ticket checks V01 and V02."""
    ticket_dir = Path(ticket_dir)
    whole: list[str] = []
    detail: list[str] = []

    edge_claim_md = _read_text(ticket_dir / "edge_claim.md")
    hypothesis_md = _read_text(ticket_dir / "hypothesis.md")
    # Stored if present and read by nothing (docs/22 section 5.4).
    manifest_md = _read_text(ticket_dir / "MANIFEST.md")

    attempt_id = ""
    bets: list[BetSpec] = []

    bets_path = ticket_dir / "bets.json"
    data: Any = None
    raw = _read_text(bets_path)
    if raw is None:
        whole.append("V01")
        detail.append(f"bets.json is missing or unreadable at {bets_path}")
    else:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            whole.append("V01")
            detail.append(f"bets.json is not valid JSON: {exc}")

    if data is not None:
        schema = _schema_errors(data)
        if schema:
            detail += schema
            if "V01" not in whole:
                whole.append("V01")
        if isinstance(data, dict):
            attempt_id = data.get("attempt", "") if isinstance(data.get("attempt"), str) else ""
            bets = _extract(data)

    missing_headings = [h for h in _EDGE_HEADINGS if not _has_headings(edge_claim_md, (h,))]
    missing_headings += [h for h in _HYP_HEADINGS if not _has_headings(hypothesis_md, (h,))]
    if missing_headings:
        whole.append("V02")
        detail.append(
            "missing required headings (exact H2 lines): " + ", ".join(missing_headings)
            + (" (edge_claim.md is missing or empty)" if not edge_claim_md else "")
            + (" (hypothesis.md is missing or empty)" if not hypothesis_md else "")
        )

    return ParsedTicket(
        attempt_id=attempt_id,
        bets=bets,
        edge_claim_md=edge_claim_md,
        hypothesis_md=hypothesis_md,
        manifest_md=manifest_md,
        whole_ticket_errors=whole,
        error_detail=detail,
    )


# --------------------------------------------------------------------------- #
# validate (V03 - V16)
# --------------------------------------------------------------------------- #
def _tick_step(market, price: Decimal) -> Decimal:
    """The tick step of the ``price_ranges`` band covering ``price`` (else tick_size)."""
    raw = getattr(market, "raw", None) or {}
    ranges = raw.get("price_ranges")
    if isinstance(ranges, list):
        for pr in ranges:
            try:
                start = D(str(pr["start"]))
                end = D(str(pr["end"]))
                step = D(str(pr["step"]))
            except (KeyError, TypeError, ValueError, InvalidOperation):
                continue
            if step > 0 and start <= price < end:
                return step
    ts = getattr(market, "tick_size", None)
    return D(ts) if ts else D("0.01")


def _aligned(price: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return True
    return (price % step) == 0


def _safe_fetch(fn: Callable | None, ticker: str):
    if fn is None:
        return None
    try:
        return fn(ticker)
    except KalshiAPIError:
        return None


def _check_leg(spec, market_fetch, book_fetch, settings, now, seen):
    """Run V03 to V11 for one bet; return (reject_code|None, market, book, contracts)."""
    # V03: duplicate ticker among non-rejected bets (later index loses)
    if spec.ticker in seen:
        return "V03", None, None, None
    # V04: market exists and is open
    market = _safe_fetch(market_fetch, spec.ticker)
    if market is None or market.status not in _OPEN_STATUSES:
        return "V04", market, None, None
    # V05: within the resolution window (either time may be None; both None -> reject)
    times = [t for t in (market.close_time, market.expected_expiration) if t is not None]
    if not times:
        return "V05", market, None, None
    if min(times) > now + timedelta(hours=settings.limits.max_resolve_hours):
        return "V05", market, None, None
    # V07: price in [0.01, 0.99] and aligned to the covering tick band
    price = spec.limit_price
    if not (D("0.01") <= price <= D("0.99")):
        return "V07", market, None, None
    if not _aligned(price, _tick_step(market, price)):
        return "V07", market, None, None
    # V10: the declared size is a whole number inside the allowed range
    if not (1 <= spec.contracts <= settings.stakes.max_contracts_per_bet):
        return "V10", market, None, None
    contracts = D(spec.contracts)
    # V11: orderbook obtainable, with enough real depth on the bet's side for the size
    # it asked for, summed across every level as an exact Decimal (docs/14 D3). A single
    # fractional top level (e.g. 0.96) used to int-truncate to size 0 and reject outright
    # even with real size resting behind it, the shape that flipped A-0020's sign. the
    # full book depth is what counts, not the top level alone.
    book = _safe_fetch(book_fetch, spec.ticker)
    if book is None:
        return "V11", market, None, None
    if book.ask_depth(spec.side) < contracts:
        return "V11", market, book, None
    return None, market, book, contracts


def _usd(x: Decimal) -> str:
    """``$8.00``, or four places when the amount has them (a 0.0050 tick)."""
    x = q4(x)
    return f"${x.quantize(D('0.01'))}" if x == x.quantize(D("0.01")) else f"${x}"


def _check_allowance(validated_bets: list[ValidatedBet], settings) -> None:
    """V16: refuse the legs that take the ticket past ``stakes.per_attempt_real_cap``.

    Walks the legs still validated, in ticket order, adding contracts times limit price.
    A leg that would take the running total past the allowance is refused, and it adds
    nothing, so a smaller later leg may still fit. Every refused leg carries the same
    sentence, with the ticket's total over all validated legs and the allowance.
    """
    cap = D(settings.stakes.per_attempt_real_cap)
    legs = [vb for vb in validated_bets if vb.status == "validated"]
    total = q4(sum((vb.contracts * vb.spec.limit_price for vb in legs), D("0")))
    if total <= cap:
        return
    reason = (
        f"this ticket stakes {_usd(total)} in total at its limit prices (contracts times "
        f"price, summed over its legs), above the {_usd(cap)} allowance for one attempt; "
        f"this bet does not fit, so drop or shrink legs until the total is {_usd(cap)} "
        f"or less"
    )
    running = D("0")
    for vb in sorted(legs, key=lambda x: x.spec.ticket_index):
        stake = q4(vb.contracts * vb.spec.limit_price)
        if running + stake > cap:
            vb.status = "rejected"
            vb.reject_code = "V16"
            vb.reject_reason = reason
            continue
        running += stake


def validate_ticket(
    parsed: ParsedTicket,
    market_fetch: Callable,
    book_fetch: Callable,
    settings,
    now: datetime,
    attempt_id: str | None = None,
) -> ValidationOutcome:
    """Validate a parsed ticket (V03 to V16). Pure w.r.t. the two fetch callables.

    ``attempt_id`` is accepted and ignored; it named each group's ``group_db_id``, and it
    goes when ``attempt.py`` stops passing it.
    """
    if parsed.whole_ticket_errors:
        return ValidationOutcome(
            ticket_valid=False,
            whole_ticket_error=parsed.whole_ticket_errors[0],
            bets=[],
            truncated_count=0,
        )

    # ---- truncation past the execution cap: silent here, audited by execute ----
    cap = settings.limits.max_bets_per_attempt
    surviving = [b for b in parsed.bets if b.ticket_index <= cap]
    truncated_count = len(parsed.bets) - len(surviving)

    # ---- per-leg checks in ticket order (so V03 sees prior non-rejected tickers) ----
    seen: set[str] = set()
    validated_bets: list[ValidatedBet] = []
    for b in sorted(surviving, key=lambda x: x.ticket_index):
        code, market, book, contracts = _check_leg(
            b, market_fetch, book_fetch, settings, now, seen
        )
        if code is None:
            seen.add(b.ticker)
        validated_bets.append(
            ValidatedBet(
                spec=b,
                status="validated" if code is None else "rejected",
                reject_code=code,
                market=market,
                book=book,
                contracts=contracts,
            )
        )
    _check_allowance(validated_bets, settings)

    return ValidationOutcome(
        ticket_valid=True,
        whole_ticket_error=None,
        bets=validated_bets,
        truncated_count=truncated_count,
    )
