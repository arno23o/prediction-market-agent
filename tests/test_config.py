"""Config precedence and typing — spec §4, Appendix E."""

import os
from decimal import Decimal
from pathlib import Path

import pytest

from betting_agent.config import (
    config_warnings,
    load_settings,
    unknown_keys,
    unknown_keys_message,
)


def test_defaults_match_spec(tmp_path):
    s = load_settings(root=tmp_path)
    assert s.kalshi.env == "prod"
    assert s.kalshi.prod_base == "https://api.elections.kalshi.com/trade-api/v2"
    assert s.kalshi.base_url == s.kalshi.prod_base
    assert s.schedule.slots == [
        "01:00", "03:40", "06:20", "09:00", "11:40", "14:20", "17:00", "19:40", "22:20"
    ]
    assert s.schedule.slot_grace_min == 120
    assert s.limits.max_resolve_hours == 120
    assert s.limits.max_bets_per_attempt == 20
    assert s.stakes.max_contracts_per_bet == 3
    assert s.stakes.daily_real_stake_cap == Decimal("25.00")
    assert s.stakes.per_market_real_cap == Decimal("4.00")
    assert s.stakes.per_attempt_real_cap == Decimal("11.20")
    assert s.stakes.drawdown_floor_pct == Decimal("0.50")
    assert s.attempt.model == "claude-opus-5-5"
    assert s.attempt.effort == "high"
    assert s.attempt.efforts == ["high", "max"]
    assert s.attempt.fable_model == "claude-fable-5-1"
    assert s.attempt.fable_per_day == 2
    assert s.attempt.fable_effort == "max"
    assert s.attempt.max_turns == 250
    assert s.attempt.max_budget_usd == Decimal("30.00")
    assert s.attempt.wall_time_min == 180
    assert s.reconcile.hour_et == 23
    assert s.cells.counts() == {"baseline": 1, "static": 3, "director": 4, "focused": 1}
    assert sum(s.cells.counts().values()) == len(s.schedule.slots)
    assert s.cells.static_recent == 10
    assert s.director.model == "claude-fable-5-1"
    assert s.director.effort == "xhigh"
    assert s.director.hour_et == "00:00"
    assert s.director.max_turns == 120
    assert s.director.max_budget_usd == Decimal("40.00")
    assert s.director.wall_time_min == 45
    assert s.director.set_size == 10
    assert s.director.page_max_chars == 4000
    assert s.history.current_era == "live-v2"
    assert s.history.era_default_min == 50
    assert s.fees.default_coef == Decimal("0.07")
    assert s.fees.category_coefs == {"index": Decimal("0.035")}


def test_live_trading_defaults_false(tmp_path):
    assert load_settings(root=tmp_path).stakes.live_trading is False


def test_decimal_fields_are_decimal(tmp_path):
    s = load_settings(root=tmp_path)
    assert isinstance(s.stakes.per_market_real_cap, Decimal)
    assert isinstance(s.director.max_budget_usd, Decimal)
    assert isinstance(s.stakes.daily_real_stake_cap, Decimal)
    assert isinstance(s.attempt.max_budget_usd, Decimal)
    assert isinstance(s.fees.default_coef, Decimal)
    assert all(isinstance(v, Decimal) for v in s.fees.category_coefs.values())


def test_config_toml_override(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[kalshi]\nenv = "demo"\n\n'
        '[limits]\nmax_resolve_hours = 48\nmax_bets_per_attempt = 15\n\n'
        '[stakes]\ndaily_real_stake_cap = "20.00"\n'
    )
    s = load_settings(root=tmp_path)
    assert s.kalshi.env == "demo"
    assert s.kalshi.base_url == s.kalshi.demo_base
    assert s.limits.max_resolve_hours == 48
    assert s.limits.max_bets_per_attempt == 15
    assert s.stakes.daily_real_stake_cap == Decimal("20.00")


def test_env_override_wins_and_deep_merges(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        '[limits]\nmax_resolve_hours = 48\nmax_bets_per_attempt = 15\n'
    )
    monkeypatch.setenv("BETTING_AGENT_LIMITS__MAX_RESOLVE_HOURS", "36")
    monkeypatch.setenv("BETTING_AGENT_STAKES__LIVE_TRADING", "true")
    s = load_settings(root=tmp_path)
    # env beats toml:
    assert s.limits.max_resolve_hours == 36
    # toml's sibling field survives the partial env override (deep merge):
    assert s.limits.max_bets_per_attempt == 15
    assert s.stakes.live_trading is True


def test_cell_counts_toml_and_env_override(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text("[cells]\nbaseline = 2\n")
    assert load_settings(root=tmp_path).cells.baseline == 2
    # env beats toml
    monkeypatch.setenv("BETTING_AGENT_CELLS__BASELINE", "3")
    assert load_settings(root=tmp_path).cells.baseline == 3


def test_secrets_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KALSHI_API_KEY_ID", "key-123")
    monkeypatch.setenv("KALSHI_PRIVATE_KEY_PATH", "~/keys/prod.pem")
    s = load_settings(root=tmp_path)
    assert s.kalshi.key_id == "key-123"
    assert str(s.kalshi.private_key_path) == os.path.expanduser("~/keys/prod.pem")


def test_secrets_from_dotenv(tmp_path):
    (tmp_path / ".env").write_text(
        "KALSHI_API_KEY_ID=from-dotenv\nKALSHI_PRIVATE_KEY_PATH=/abs/key.pem\n"
    )
    s = load_settings(root=tmp_path)
    assert s.kalshi.key_id == "from-dotenv"
    assert str(s.kalshi.private_key_path) == "/abs/key.pem"


def test_path_properties(tmp_path):
    s = load_settings(root=tmp_path)
    assert s.data_dir == tmp_path / "data"
    assert s.ledger_path == tmp_path / "data" / "ledger.db"
    assert s.attempts_dir == tmp_path / "data" / "attempts"
    assert s.reports_dir == tmp_path / "data" / "reports"
    assert s.logs_dir == tmp_path / "data" / "logs"
    assert s.locks_dir == tmp_path / "data" / "locks"
    assert s.backups_dir == tmp_path / "data" / "backups"
    assert s.halt_path == tmp_path / "data" / "HALT"


# --------------------------------------------------------------------------- CI-1 / D5
def test_unknown_key_is_flagged_and_the_run_continues_on_defaults(tmp_path):
    """A typo'd key inside a known section: named, and the default still applies.

    Decision D5 — flag, don't stop. ``extra="forbid"`` would refuse to construct
    ``Settings`` at all, which for an autonomous system means a one-character typo stops
    settling and reconciling real money at 03:00. ``extra="ignore"`` (the CI-1 defect)
    reverted the knob silently.
    """
    (tmp_path / "config.toml").write_text(
        '[limits]\nmax_bets_per_attemp = 3\nmax_resolve_hours = 48\n'
    )
    s = load_settings(root=tmp_path)

    assert unknown_keys(s) == ["limits.max_bets_per_attemp"]
    assert s.limits.max_bets_per_attempt == 20      # the default, not the typo's 3
    assert s.limits.max_resolve_hours == 48         # the sibling key still applies


def test_a_key_the_rebuild_removed_is_reported_not_silently_honored(tmp_path):
    """Section 3's whole purpose: every removed key loses its field, so a stale config.toml
    line warns instead of quietly steering. ``memory_ab`` drove the retired memory arm and
    ``slot_models`` the retired per-slot model grid; both have to be named."""
    (tmp_path / "config.toml").write_text(
        "[attempt]\nmemory_ab = true\nmodel = \"claude-fable-5\"\n\n"
        '[schedule]\nslots = ["01:00"]\n'
        '[schedule.slot_models]\n"01:00" = "claude-sonnet-5"\n'
    )
    s = load_settings(root=tmp_path)

    assert unknown_keys(s, env={}) == ["attempt.memory_ab", "schedule.slot_models"]
    # The keys beside them still apply, so the report is about the stale lines only.
    assert s.attempt.model == "claude-fable-5"
    assert s.schedule.slots == ["01:00"]


def test_unknown_section_is_flagged_once_by_name(tmp_path):
    """A whole mistyped section reports as one line, not one per key inside it."""
    (tmp_path / "config.toml").write_text(
        '[stakez]\nlive_trading = true\ndaily_real_stake_cap = "99.00"\n'
    )
    s = load_settings(root=tmp_path)

    assert unknown_keys(s) == ["stakez"]
    assert s.stakes.live_trading is False           # the safe default held
    assert s.stakes.daily_real_stake_cap == Decimal("25.00")


def test_unknown_env_var_is_flagged_nested_and_top_level(tmp_path, monkeypatch):
    """Both env shapes. The top-level one needs its own walk: pydantic-settings resolves
    top-level vars by asking each *known* field for its name, so a typo there is never
    read by anything and leaves no ``model_extra`` trace at all."""
    monkeypatch.setenv("BETTING_AGENT_ATTEMPT__MAX_TURNZ", "9")
    monkeypatch.setenv("BETTING_AGENT_STAKEZ", "1")
    monkeypatch.setenv("BETTING_AGENT_ATTEMPT__MAX_TURNS", "77")

    s = load_settings(root=tmp_path)

    assert unknown_keys(s) == ["attempt.max_turnz", "stakez"]
    assert s.attempt.max_turns == 77                # the correctly-spelled one still wins


def test_quoted_key_tables_never_misfire(tmp_path):
    """``[models.substitute]``/``fees.category_coefs`` are ``dict`` fields, so their keys
    are data. Flagging them would make the warning permanently noisy on the production
    config and train everyone to ignore it."""
    (tmp_path / "config.toml").write_text(
        '[models]\nsubstitute = { "claude-fable-5" = "claude-opus-5" }\n'
        'substitute_until = "2099-01-01T00:00:00-07:00"\n'
        '[fees.category_coefs]\nindex = "0.035"\ncrypto = "0.02"\n'
    )
    s = load_settings(root=tmp_path)

    assert unknown_keys(s) == []
    assert s.models.substitute == {"claude-fable-5": "claude-opus-5"}
    assert s.fees.category_coefs["crypto"] == Decimal("0.02")


def test_production_config_toml_has_no_unknown_keys(tmp_path):
    """The live config.toml is the one file this sweep must never cry wolf about."""
    src = Path(__file__).resolve().parents[1] / "config.toml"
    (tmp_path / "config.toml").write_bytes(src.read_bytes())

    assert unknown_keys(load_settings(root=tmp_path)) == []


def test_clean_config_reports_nothing(tmp_path):
    assert unknown_keys(load_settings(root=tmp_path)) == []
    assert unknown_keys_message([]) == "none"


def test_unknown_keys_message_names_every_key(tmp_path):
    msg = unknown_keys_message(["limits.max_bets_per_attemp", "stakez"])
    assert "limits.max_bets_per_attemp" in msg
    assert "stakez" in msg
    assert "running on defaults" in msg


# --------------------------------------------------------------------------- CI-5
def test_config_sourced_private_key_path_is_expanduser_ed(tmp_path):
    """CI-5: the same "~/keys/prod.pem" worked from .env and failed from config.toml."""
    (tmp_path / "config.toml").write_text('[kalshi]\nprivate_key_path = "~/keys/fake.pem"\n')
    s = load_settings(root=tmp_path)
    assert str(s.kalshi.private_key_path) == os.path.expanduser("~/keys/fake.pem")
    assert "~" not in str(s.kalshi.private_key_path)


def test_env_sourced_private_key_path_is_expanduser_ed(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTING_AGENT_KALSHI__PRIVATE_KEY_PATH", "~/keys/fake-env.pem")
    s = load_settings(root=tmp_path)
    assert str(s.kalshi.private_key_path) == os.path.expanduser("~/keys/fake-env.pem")


def test_absolute_private_key_path_is_untouched(tmp_path):
    (tmp_path / "config.toml").write_text('[kalshi]\nprivate_key_path = "/abs/fake.pem"\n')
    assert str(load_settings(root=tmp_path).kalshi.private_key_path) == "/abs/fake.pem"


# --------------------------------------------------------------------------- docs/22 §6
def test_board_defaults_match_the_rebuild_spec(tmp_path):
    """Two of them have moved off the spec's numbers, both to make the refresh cheap
    enough to run hourly: the cadence (150 minutes to 60) and the settled cap (20000 rows
    to 2000, a slice that was 57% of the file and serves one column)."""
    b = load_settings(root=tmp_path).board
    assert b.refresh_min_interval_min == 60
    assert b.generations_keep == 2
    assert b.close_bound_hours == 120
    assert b.excluded_series == ["KXMVECROSS"]
    assert b.excluded_categories == ["Sports", "Entertainment"]
    assert b.statuses == ["open", "settled"]
    assert b.category_lookup_cap == 2000
    assert b.settled_max == 2000
    assert b.max_markets is None


def test_the_board_exclusions_are_configurable(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[board]\nexcluded_series = ["KXA", "KXB"]\nexcluded_categories = []\n'
    )
    b = load_settings(root=tmp_path).board
    assert b.excluded_series == ["KXA", "KXB"]
    assert b.excluded_categories == []


# --------------------------------------------------------------------------- docs/22 §3.3
def test_too_few_cells_to_fill_the_day_is_flagged(tmp_path):
    """One cell per slot: a total below the slot count leaves slots with no cell at all."""
    (tmp_path / "config.toml").write_text(
        '[schedule]\nslots = ["01:00", "05:00", "09:00", "13:00"]\n'
        "[cells]\nbaseline = 1\nstatic = 1\ndirector = 0\nfocused = 0\n"
    )
    warned = config_warnings(load_settings(root=tmp_path))
    assert len(warned) == 1
    assert warned[0].startswith("cells:")
    assert "total 2, which is not the 4 slots/day" in warned[0]


def test_too_many_cells_for_the_day_is_flagged_too(tmp_path):
    """The check is strict in both directions: a surplus means a cell is dropped from the
    draw, and the baseline and focused cells run once a day, every day."""
    (tmp_path / "config.toml").write_text(
        '[schedule]\nslots = ["01:00", "05:00", "09:00"]\n'
        "[cells]\nbaseline = 1\nstatic = 2\ndirector = 1\nfocused = 1\n"
    )
    warned = config_warnings(load_settings(root=tmp_path))
    assert len(warned) == 1
    assert "total 5, which is not the 3 slots/day" in warned[0]


def test_counts_that_match_the_slot_count_are_silent(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[schedule]\nslots = ["01:00", "05:00", "09:00"]\n'
        "[cells]\nbaseline = 1\nstatic = 1\ndirector = 0\nfocused = 1\n"
    )
    assert config_warnings(load_settings(root=tmp_path)) == []


def test_the_defaults_raise_no_warnings(tmp_path):
    """The shipped defaults are nine slots and nine cells, so a bare Settings is silent."""
    assert config_warnings(load_settings(root=tmp_path)) == []


# --------------------------------------------------------------------------- 2026-09-27
def test_the_reconcile_bands_and_the_residual_watch_default_to_the_decision(tmp_path):
    rec = load_settings(root=tmp_path).reconcile
    assert rec.absorb_usd == Decimal("0.50") and rec.halt_drift_usd == Decimal("1.00")
    assert rec.residual_alert_usd == Decimal("2.50")
    assert rec.residual_alert_nights == 10 and rec.residual_window_days == 30
    assert not hasattr(rec, "halt_after_nights")


def test_an_absorb_band_wider_than_the_halt_band_is_flagged(tmp_path):
    """Absorption is tested first, so an absorb band past the halt band would absorb
    drifts that are meant to halt."""
    (tmp_path / "config.toml").write_text(
        '[reconcile]\nabsorb_usd = "1.50"\nhalt_drift_usd = "1.00"\n'
    )
    warned = config_warnings(load_settings(root=tmp_path))
    assert len(warned) == 1
    assert warned[0].startswith("reconcile.absorb_usd: 1.50 is more than")


def test_an_absorb_band_equal_to_the_halt_band_is_silent(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[reconcile]\nabsorb_usd = "1.00"\nhalt_drift_usd = "1.00"\n'
    )
    assert config_warnings(load_settings(root=tmp_path)) == []


def test_the_retired_repeat_rule_key_is_an_unknown_key(tmp_path):
    (tmp_path / "config.toml").write_text("[reconcile]\nhalt_after_nights = 3\n")
    assert "reconcile.halt_after_nights" in unknown_keys(load_settings(root=tmp_path))


# --------------------------------------------------------------------------- the arms
@pytest.mark.parametrize("toml, key", [
    ('[attempt]\neffort = "hi"\n', "attempt.effort"),
    ('[attempt]\nefforts = ["high", "maximum"]\n', "attempt.efforts"),
    ('[attempt]\nfable_effort = "ultra"\n', "attempt.fable_effort"),
    ('[director]\neffort = "extra"\n', "director.effort"),
])
def test_an_effort_claude_does_not_accept_is_flagged(tmp_path, toml, key):
    """Every configured effort reaches a session's ``--effort``, so a misspelled one is a
    slot that cannot start. The line names the key, the value and the accepted levels."""
    (tmp_path / "config.toml").write_text(toml)
    warned = config_warnings(load_settings(root=tmp_path))
    assert len(warned) == 1
    assert warned[0].startswith(f"{key}: ")
    assert "low, medium, high, xhigh, max" in warned[0]


def test_more_fable_slots_than_the_day_has_is_flagged(tmp_path):
    three_slots = (
        '[schedule]\nslots = ["01:00", "05:00", "09:00"]\n'
        "[cells]\nbaseline = 1\nstatic = 1\ndirector = 0\nfocused = 1\n"
    )
    (tmp_path / "config.toml").write_text(three_slots + "[attempt]\nfable_per_day = 4\n")
    warned = config_warnings(load_settings(root=tmp_path))
    assert len(warned) == 1
    assert warned[0].startswith("attempt.fable_per_day: 4 is more than the 3 slots/day")

    # Every slot on Fable is a legitimate day; only a count the day cannot hold warns.
    (tmp_path / "config.toml").write_text(three_slots + "[attempt]\nfable_per_day = 3\n")
    assert config_warnings(load_settings(root=tmp_path)) == []


# --------------------------------------------------------------------------- docs/22 §8.2
def test_the_director_hour_is_read_like_a_slot(tmp_path):
    (tmp_path / "config.toml").write_text('[director]\nhour_et = "04:30"\n')
    s = load_settings(root=tmp_path)
    assert s.director.hour_minute() == (4, 30)
    assert config_warnings(s) == []


@pytest.mark.parametrize("value", ["", "midnight", "0", "24:00", "00:60", "0:0:0"])
def test_an_unreadable_director_hour_raises_and_is_warned_about(tmp_path, value):
    """It used to answer ``(0, 0)``, which moved a mistyped hour to midnight and told
    nobody. The reader raises and the health sweep names the value."""
    (tmp_path / "config.toml").write_text(f'[director]\nhour_et = "{value}"\n')
    s = load_settings(root=tmp_path)

    with pytest.raises(ValueError, match="director.hour_et"):
        s.director.hour_minute()

    warned = config_warnings(s)
    assert len(warned) == 1
    assert warned[0].startswith("director.hour_et:")
    assert f'"{value}"' in warned[0]


# --------------------------------------------------------------------------- the allowance
def test_an_attempt_allowance_above_the_daily_cap_is_flagged(tmp_path):
    """The daily cap refuses first, so an allowance above it never binds (2026-09-27)."""
    (tmp_path / "config.toml").write_text(
        '[stakes]\ndaily_real_stake_cap = "5.00"\nper_attempt_real_cap = "8.00"\n'
    )
    warned = config_warnings(load_settings(root=tmp_path))
    assert len(warned) == 1
    assert warned[0].startswith("stakes.per_attempt_real_cap: 8.00 is more than")
    assert "never binds" in warned[0]


def test_an_attempt_allowance_equal_to_the_daily_cap_is_silent(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[stakes]\ndaily_real_stake_cap = "8.00"\nper_attempt_real_cap = "8.00"\n'
    )
    s = load_settings(root=tmp_path)
    assert s.stakes.per_attempt_real_cap == Decimal("8.00")
    assert config_warnings(s) == []
