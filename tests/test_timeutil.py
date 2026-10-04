"""Time helpers — spec §5."""

from datetime import UTC, date, datetime, timedelta

import pytest

from betting_agent.timeutil import (
    ET,
    et_day,
    iso,
    parse_iso,
    slot_datetimes,
    utc_now,
)


def test_utc_now_is_aware():
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset().total_seconds() == 0


def test_iso_z_suffix_second_precision():
    dt = datetime(2026, 7, 7, 14, 30, 15, 123456, tzinfo=UTC)
    assert iso(dt) == "2026-07-07T14:30:15Z"


def test_iso_converts_to_utc():
    # 10:00 ET in July (EDT, UTC-4) -> 14:00 UTC.
    dt = datetime(2026, 7, 7, 10, 0, tzinfo=ET)
    assert iso(dt) == "2026-07-07T14:00:00Z"


def test_iso_rejects_naive():
    with pytest.raises(ValueError):
        iso(datetime(2026, 7, 7, 14, 30))


def test_parse_iso_roundtrip_z_and_offset():
    dt = parse_iso("2026-07-07T14:30:00Z")
    assert dt == datetime(2026, 7, 7, 14, 30, tzinfo=UTC)
    off = parse_iso("2026-07-07T19:30:00+05:00")
    assert off.astimezone(UTC) == datetime(2026, 7, 7, 14, 30, tzinfo=UTC)
    assert iso(off) == "2026-07-07T14:30:00Z"


def test_et_day_crosses_midnight():
    # ET midnight in July is 04:00 UTC. Straddle it on the same UTC date.
    assert et_day(parse_iso("2026-07-07T03:30:00Z")) == "2026-07-06"  # 23:30 ET, prev day
    assert et_day(parse_iso("2026-07-07T04:30:00Z")) == "2026-07-07"  # 00:30 ET, same day


def test_slot_datetimes_are_et_aware():
    slots = slot_datetimes(date(2026, 7, 7), ["10:00", "16:00"])
    assert [s.hour for s in slots] == [10, 16]
    assert all(s.tzinfo is ET for s in slots)
    # 10:00 ET -> 14:00 UTC in July.
    assert iso(slots[0]) == "2026-07-07T14:00:00Z"


# ===========================================================================
# CI-6 — DST transitions
#
# Everything above is dated in July, i.e. entirely inside EDT. But the spend-cap day and
# the slot schedule are ET, and ET moves twice a year: the day boundary shifts by an hour
# in winter, one local hour does not exist in March, and one happens twice in November —
# and `config.toml` really does schedule a 01:00 slot, the hour that doubles. The vectors
# below pin what the harness does on each of those three nights.
# ===========================================================================
# The 2026 US transitions (second Sunday in March / first Sunday in November).
SPRING_FORWARD = date(2026, 3, 8)   # 02:00 EST -> 03:00 EDT; 02:00-02:59 never happens
FALL_BACK = date(2026, 11, 1)       # 02:00 EDT -> 01:00 EST; 01:00-01:59 happens twice


def test_et_day_at_the_est_winter_boundary():
    """In winter the ET day starts at 05:00 UTC, not 04:00 — an hour later than July.

    A cap charged at 04:30 UTC on a January morning belongs to the PREVIOUS ET day. If
    this ever drifted to the summer boundary, one hour of every winter night would be
    charged against tomorrow's daily real-stake cap.
    """
    assert et_day(parse_iso("2026-01-15T04:30:00Z")) == "2026-01-14"  # 23:30 EST, prev day
    assert et_day(parse_iso("2026-01-15T05:00:00Z")) == "2026-01-15"  # 00:00 EST exactly
    assert et_day(parse_iso("2026-01-15T05:30:00Z")) == "2026-01-15"  # 00:30 EST, same day


def test_slot_datetimes_are_est_in_winter():
    """The same wall-clock slots sit an hour later in UTC once EDT ends."""
    slots = slot_datetimes(date(2026, 1, 15), ["01:00", "20:00"])
    assert [s.utcoffset() for s in slots] == [timedelta(hours=-5)] * 2
    assert iso(slots[0]) == "2026-01-15T06:00:00Z"
    assert iso(slots[1]) == "2026-01-16T01:00:00Z"  # an evening ET slot lands tomorrow UTC


def test_et_day_across_the_spring_forward_night():
    """The day boundary on the spring-forward date is still EST (the jump is at 02:00),
    and the hour 02:00-02:59 local is simply skipped: 06:30 UTC is 01:30 EST, and the
    next UTC half-hour is already 03:30 EDT. Both are the same ET day."""
    assert et_day(parse_iso("2026-03-08T04:30:00Z")) == "2026-03-07"  # 23:30 EST, prev day
    assert et_day(parse_iso("2026-03-08T05:30:00Z")) == "2026-03-08"  # 00:30 EST
    assert parse_iso("2026-03-08T06:30:00Z").astimezone(ET).strftime("%H:%M") == "01:30"
    assert parse_iso("2026-03-08T07:30:00Z").astimezone(ET).strftime("%H:%M") == "03:30"
    assert et_day(parse_iso("2026-03-08T07:30:00Z")) == "2026-03-08"


def test_spring_forward_nonexistent_slots_resolve_to_the_pre_transition_offset():
    """A 02:xx slot on 2026-03-08 names a local time that does not exist.

    ``datetime(..., tzinfo=ET)`` does not raise on it; PEP 495 gives a nonexistent time
    the offset in effect BEFORE the gap (EST, -5), so 02:00 local resolves to 07:00 UTC —
    the very instant that is also 03:00 EDT. Two scheduled slots would therefore fire at
    the same moment, one of them under a name the clock never shows. Nothing in
    ``config.toml`` schedules 02:xx today; this pins the arithmetic so that a future
    schedule edit into that hour is a visible decision rather than a silent collision.
    """
    two, two_thirty, three = slot_datetimes(SPRING_FORWARD, ["02:00", "02:30", "03:00"])
    assert two.utcoffset() == timedelta(hours=-5)      # pre-transition (EST)
    assert three.utcoffset() == timedelta(hours=-4)    # post-transition (EDT)
    assert iso(two) == "2026-03-08T07:00:00Z"
    assert iso(two_thirty) == "2026-03-08T07:30:00Z"
    assert iso(three) == iso(two)                      # the collision, stated outright
    # And the real schedule's neighbours are unaffected.
    one, five = slot_datetimes(SPRING_FORWARD, ["01:00", "05:00"])
    assert (iso(one), iso(five)) == ("2026-03-08T06:00:00Z", "2026-03-08T09:00:00Z")


def test_fall_back_resolves_the_doubled_one_am_slot_to_its_first_occurrence():
    """01:00 ET happens twice on 2026-11-01, and ``config.toml`` schedules a 01:00 slot.

    ``slot_datetimes`` builds its datetimes with the default ``fold=0``, which selects
    the FIRST occurrence — 01:00 EDT, 05:00 UTC. The second (01:00 EST, 06:00 UTC) is
    reachable only by asking for ``fold=1`` explicitly, which nothing does. So the slot
    fires once, an hour earlier in UTC than the winter slots that follow it, and the
    hour that repeats does not repeat the attempt.
    """
    (one,) = slot_datetimes(FALL_BACK, ["01:00"])
    assert one.fold == 0
    assert one.utcoffset() == timedelta(hours=-4)      # EDT: the first 01:00
    assert iso(one) == "2026-11-01T05:00:00Z"
    # The other 01:00 that day is a real, distinct instant an hour later.
    assert iso(one.replace(fold=1)) == "2026-11-01T06:00:00Z"
    # Slot identity is the local ``YYYY-MM-DD/HH:MM`` string (see ``cli._slot_key``), so
    # both occurrences name the SAME slot — one meta key, therefore one spawn.
    assert one.strftime("%Y-%m-%d/%H:%M") == one.replace(fold=1).strftime("%Y-%m-%d/%H:%M")
    # A slot after the transition is back on EST.
    (five,) = slot_datetimes(FALL_BACK, ["05:00"])
    assert five.utcoffset() == timedelta(hours=-5)
    assert iso(five) == "2026-11-01T10:00:00Z"


def test_et_day_is_stable_across_the_repeated_fall_back_hour():
    """Both 01:30s and the 00:30 before them are the same ET day, so an attempt placed in
    the repeated hour is charged against 2026-11-01's cap either way round."""
    assert et_day(parse_iso("2026-11-01T04:30:00Z")) == "2026-11-01"  # 00:30 EDT
    assert et_day(parse_iso("2026-11-01T05:30:00Z")) == "2026-11-01"  # 01:30 EDT (first)
    assert et_day(parse_iso("2026-11-01T06:30:00Z")) == "2026-11-01"  # 01:30 EST (again)
    # The ET day still ends at the EDT boundary that morning, not the EST one.
    assert et_day(parse_iso("2026-11-01T03:30:00Z")) == "2026-10-31"  # 23:30 EDT
