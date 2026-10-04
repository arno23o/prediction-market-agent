"""The rebuild's operating config resolves to the parameters docs/22 rules.

``tests/fixtures/activation-config.toml`` is the repo's ``config.toml``, byte for byte,
held here as the artifact under test. Every test below points ``Settings`` at a
``tmp_path`` root, so loading it cannot touch the live system.

The point of the sweep is that the file an operator will actually install produces zero
unknown-key reports and zero config-health warnings. The two flag-do-not-stop surfaces
(D5, A3) are only useful if the config they run against is silent on both. The rest pins
the operating values: the slots, the attempt model and its arms, the caps, the cell
counts, the director's hour, and the era.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from betting_agent.config import config_warnings, load_settings, unknown_keys
from betting_agent.timeutil import parse_iso

ACTIVATION_TOML = Path(__file__).resolve().parent / "fixtures" / "activation-config.toml"


@pytest.fixture
def activated(tmp_path):
    """A tmp root whose ``config.toml`` is the activation artifact, byte for byte."""
    (tmp_path / "config.toml").write_bytes(ACTIVATION_TOML.read_bytes())
    return load_settings(root=tmp_path)


def test_the_activation_config_reports_no_unknown_keys(activated):
    """Every key in the file names something. A typo here reverts a knob to its default
    silently at 03:40, which is the failure CI-1/D5 exist to make loud."""
    assert unknown_keys(activated, env={}) == []


def test_the_activation_config_raises_no_health_warnings(activated):
    """Nothing in the file is recognized but inert: the cell counts cover the day's slots
    and no model substitution is armed. A standing warning would make the surface noise."""
    assert config_warnings(activated, now=parse_iso("2026-09-20T00:00:00Z")) == []


def test_the_substitution_switch_is_available_and_not_armed(activated):
    """The mechanism stays for a quota outage; the expired 2026-08 entry is gone."""
    assert activated.models.substitute == {}
    assert activated.substitution_deadline() is None
    assert activated.substitution_active(parse_iso("2026-09-20T00:00:00Z")) is False
    assert activated.effective_model("claude-opus-5") == "claude-opus-5"


def test_the_slots_are_ordered_and_further_apart_than_the_grace_window(activated):
    """The standing schedule is nine slots 2h40 apart; a surge (2026-09-16, fifteen slots 96
    minutes apart to use surplus compute) is a legitimate operating config too. What must
    hold in both: slots ascend, no two coincide, and every gap clears the grace window so a
    late slot can never collide with its neighbour."""
    slots = activated.schedule.slots
    minutes = [int(s[:2]) * 60 + int(s[3:]) for s in slots]
    assert minutes == sorted(minutes) and len(set(minutes)) == len(minutes)
    assert activated.schedule.slot_grace_min == 90
    gaps = [b - a for a, b in zip(minutes, minutes[1:], strict=False)]
    assert min(gaps) > activated.schedule.slot_grace_min
    assert 9 <= len(slots) <= 15


def test_the_attempt_model_and_the_manual_effort(activated):
    """The old per-slot model grid stays gone (docs/22 section 3.2): a slot's model comes
    from the day's drawn plan, and ``effort`` is what a manual attempt runs."""
    assert activated.attempt.model == "claude-opus-5-5"
    assert activated.attempt.effort == "high"
    assert activated.attempt.wall_time_min == 90
    # That a stale ``[schedule.slot_models]`` would be REPORTED rather than silently
    # honored is pinned in test_config.py, against a config that actually carries one;
    # asserting its absence here says nothing, because this file never had the key.


def test_two_slots_a_day_run_fable_and_the_rest_split_high_and_max(activated):
    """Two Fable slots a day at max effort, and the other slots split evenly between high
    and max."""
    attempt = activated.attempt
    assert attempt.fable_model == "claude-fable-5-1"
    assert attempt.fable_per_day == 2
    assert attempt.fable_effort == "max"
    assert attempt.efforts == ["high", "max"]


def test_the_caps_are_the_ruled_numbers_as_decimal(activated):
    """TEXT in TOML so money never round-trips through float (docs/22 section 3.2)."""
    stakes = activated.stakes
    assert isinstance(stakes.daily_real_stake_cap, Decimal)
    assert stakes.daily_real_stake_cap == Decimal("56.00")
    assert stakes.per_market_real_cap == Decimal("4.00")
    # the allowance the attempt prompt states in words
    assert stakes.per_attempt_real_cap == Decimal("11.20")
    assert stakes.max_contracts_per_bet == 3
    assert stakes.drawdown_floor_pct == Decimal("0.50")
    # Whether to trade is the operator's choice, so either value is valid here; the safe
    # default (false when the file is silent) is pinned in test_config.py.
    assert isinstance(stakes.live_trading, bool)
    assert activated.limits.max_resolve_hours == 120


def test_the_cell_counts_fill_the_day_exactly(activated):
    """One cell per slot, so every cell runs its configured number of times every day and
    the two controls are daily (docs/22 sections 3.3 and 8.7)."""
    counts = activated.cells.counts()
    assert counts["baseline"] == 1 and counts["focused"] == 1
    assert counts["director"] >= counts["static"] >= 1
    assert activated.cells.static_recent == 10
    assert sum(counts.values()) == len(activated.schedule.slots)


def test_the_director_runs_at_midnight_eastern_on_fable(activated):
    director = activated.director
    assert director.model == "claude-fable-5-1"
    assert director.effort == "xhigh"
    assert director.hour_et == "00:00"
    assert director.hour_minute() == (0, 0)
    assert director.max_turns == 120
    assert director.max_budget_usd == Decimal("40.00")
    assert director.wall_time_min == 45
    assert director.set_size == 10
    assert director.page_max_chars == 4000


def test_the_era_is_live_v2_with_a_fifty_attempt_default(activated):
    assert activated.history.current_era == "live-v2"
    assert activated.history.era_default_min == 50


def test_the_board_is_bounded_and_excludes_what_cannot_be_bet(activated):
    board = activated.board
    # Hourly, and the settled slice cut to 2000 rows: both are what made an hourly refresh
    # affordable, and the file states them rather than leaving them to the code defaults.
    assert board.refresh_min_interval_min == 60
    assert board.settled_max == 2000
    # Two generations, not one: `bt movers` compares the newest against the previous.
    assert board.generations_keep == 2
    assert board.close_bound_hours == 120
    # The parlay family plus six series in which the exchange refused this account's orders.
    assert board.excluded_series == [
        "KXMVECROSS", "KXXIAOMISHARE", "KXBABASHARE", "KXANTHSHARE", "KXDEEPSHARE",
        "KXLAUNCHES", "KXSPACEXCOUNT",
    ]
    assert board.excluded_categories == ["Sports", "Entertainment", "Politics"]


def test_the_alerting_defaults_need_no_activation_keys(activated):
    """D1 thresholds are code defaults on purpose: the file says nothing about them, and
    the values it inherits are the ruled ones."""
    assert activated.alerts.enabled is True
    assert activated.alerts.step_failure_streak == 3
    assert activated.alerts.settle_halt_streak == 20


def test_the_repo_config_is_the_tested_activation_artifact():
    """The repo's ``config.toml`` IS the artifact this file tests, byte for byte. A future
    config change must update the fixture with it, so the change inherits every assertion
    above instead of drifting untested."""
    repo_config = Path(__file__).resolve().parents[1] / "config.toml"
    assert repo_config.read_bytes() == ACTIVATION_TOML.read_bytes()
