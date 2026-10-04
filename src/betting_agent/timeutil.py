"""Time helpers (spec §5).

Timestamps are stored as UTC ISO-8601 with a ``Z`` suffix. Scheduling logic runs in
``America/New_York`` via :mod:`zoneinfo`; the "day" used for spending caps is the ET
calendar day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def utc_now() -> datetime:
    """Current time, tz-aware in UTC."""
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Format a tz-aware datetime as UTC ISO-8601 with a ``Z`` suffix, second precision.

    ``2026-07-07T14:30:00+00:00 -> "2026-07-07T14:30:00Z"``. Naive datetimes are rejected:
    their UTC meaning is ambiguous and the codebase always carries tz-aware times.
    """
    if dt.tzinfo is None:
        raise ValueError("iso() requires a tz-aware datetime")
    u = dt.astimezone(UTC).replace(microsecond=0)
    return u.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    """Parse an ISO-8601 string (``Z`` or explicit offset) to a tz-aware datetime.

    A string carrying no timezone is interpreted as UTC (stored timestamps always are).
    """
    txt = s.strip()
    if txt.endswith(("Z", "z")):
        txt = txt[:-1] + "+00:00"
    dt = datetime.fromisoformat(txt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def parse_iso_offset(s: str) -> datetime | None:
    """Parse an ISO-8601 instant that MUST carry an explicit offset; ``None`` otherwise.

    The strict sibling of :func:`parse_iso`, which exists for *stored* timestamps and so
    reads a naive string as UTC. That default is exactly wrong for an operator-authored
    deadline (model substitution, 2026-08-18): ``"2026-08-22T00:00:00"`` means midnight
    *somewhere*, and guessing UTC would silently move a deadline meant for UTC-7 seven
    hours earlier. ``None`` for a naive or unparseable string, so the caller can refuse the
    setting and say why rather than act on an instant nobody wrote.
    """
    txt = (s or "").strip()
    if not txt:
        return None
    if txt.endswith(("Z", "z")):
        txt = txt[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(txt)
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo is not None else None


def et_day(dt: datetime) -> str:
    """The ET calendar day (``"YYYY-MM-DD"``) containing ``dt``.

    Naive datetimes are treated as UTC before conversion, so a late-evening UTC stamp
    correctly maps to the previous ET day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(ET).strftime("%Y-%m-%d")


def slot_datetimes(day: date, slots: list[str]) -> list[datetime]:
    """ET-localized, tz-aware launch datetimes for ``day`` and ``"HH:MM"`` slot strings."""
    out: list[datetime] = []
    for s in slots:
        hh, mm = s.split(":")
        out.append(datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=ET))
    return out
