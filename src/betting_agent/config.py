"""Configuration (spec §4, Appendix E).

Precedence (lowest to highest): model defaults -> ``config.toml`` at the repo root
(may be absent) -> environment variables prefixed ``BETTING_AGENT_`` with ``__`` nesting.
Secrets are read only from plain env / a ``.env`` file: ``KALSHI_API_KEY_ID`` and
``KALSHI_PRIVATE_KEY_PATH`` (``expanduser``-ed, as is a config-sourced key path).

**Unknown keys are flagged, never fatal (decision D5, 2026-08-03).** Every model runs
``extra="allow"``, so a typo'd key/section/env var lands in ``model_extra`` instead of
silently reverting to the default (CI-1) — and instead of stopping the loop, which is
what ``extra="forbid"`` would have done to a live autonomous system at 03:00.
:func:`unknown_keys` walks the loaded settings and reports what it found; callers surface
it three ways (a startup warning on stderr, a once-per-ET-day ``config_unknown_keys``
audit, and the status digest, which reads those audit rows) and then run on defaults.

:func:`config_warnings` is its sibling for the opposite failure (docs/14 A3): keys that are
all recognized but silently inert: cell counts that cannot fill the day's slots, or a
model substitution with no usable expiry. Same three surfaces, same rule (flag, never stop),
separate audit event, because "unknown key" is the wrong name for them.
"""

from __future__ import annotations

import os
import tomllib
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from betting_agent.timeutil import iso, parse_iso_offset, utc_now

_SECRET_KEYS = ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "ANTHROPIC_API_KEY")

# The levels ``claude --effort`` accepts. ``config_warnings`` names any other configured
# value, and ``betting-agent attempt --effort`` refuses one.
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _resolve_root(root: Path | None) -> Path:
    """Repo root: the given path, else three levels up from this file (src/betting_agent/)."""
    if root is not None:
        return Path(root)
    return Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- models


class _Section(BaseModel):
    """Base for every config section: unknown keys are kept, not dropped (CI-1 / D5).

    ``extra="allow"`` parks an unrecognized key in ``model_extra`` so
    :func:`unknown_keys` can name it. The alternative — ``extra="forbid"`` — turns a
    one-character typo into a hard stop for an autonomous system that has to keep
    settling and reconciling real money; the alternative we *had* — ``extra="ignore"``
    — turned it into a silent revert to defaults. Inheriting rather than repeating the
    config means a section added later is covered by construction.
    """

    model_config = ConfigDict(extra="allow")


class KalshiSettings(_Section):
    env: Literal["demo", "prod"] = "prod"
    prod_base: str = "https://api.elections.kalshi.com/trade-api/v2"
    demo_base: str = "https://demo-api.kalshi.co/trade-api/v2"
    key_id: str | None = None
    private_key_path: Path | None = None

    @property
    def base_url(self) -> str:
        return self.prod_base if self.env == "prod" else self.demo_base


class ScheduleSettings(_Section):
    # Nine slots, two hours and forty minutes apart (docs/22 section 5.1). The cell, the
    # model and the effort each slot runs come from the day's stored plan, not from
    # config.
    slots: list[str] = Field(default_factory=lambda: [
        "01:00", "03:40", "06:20", "09:00", "11:40", "14:20", "17:00", "19:40", "22:20",
    ])
    slot_grace_min: int = 120


class LimitsSettings(_Section):
    max_resolve_hours: int = 120
    max_bets_per_attempt: int = 20


class StakesSettings(_Section):
    # The ticket declares a size per bet; this bounds it, and the validator
    # refuses a leg outside 1 to this many contracts under V10 (docs/22 7.6).
    max_contracts_per_bet: int = 3
    daily_real_stake_cap: Decimal = Decimal("25.00")
    per_market_real_cap: Decimal = Decimal("4.00")
    # One attempt's stake at limit prices, summed over its legs (Arno, 2026-09-27; raised
    # from $8.00 with the daily cap on 2026-10-01, and the prompt states it in words). The
    # validator refuses the legs that go past it under V16, so the session learns of it
    # from `bt ticket validate` before it submits, and execute refuses a leg past it
    # under ``cap_attempt`` as a second guard.
    per_attempt_real_cap: Decimal = Decimal("11.20")
    # Refuse new real orders when the account's cash plus its open positions falls
    # below this fraction of the live-era genesis balance (Jul29 spec L4; cash alone
    # until 2026-10-01, see ``harness/safety.py``).
    drawdown_floor_pct: Decimal = Decimal("0.50")
    live_trading: bool = False


class AttemptSettings(_Section):
    """The attempt session, and the arms the day's plan deals out to the slots.

    Each slot's arm is a model and an effort, drawn once a day with its cell (see
    ``harness/cells.py``). ``fable_per_day`` slots run ``fable_model`` at
    ``fable_effort``; every other slot runs ``model`` at one of ``efforts``, split as
    evenly as the count allows. ``effort`` is the fallback for a manual attempt with no
    slot, and for a day whose plan was stored before arms existed.
    """

    runner: str = "claude"
    model: str = "claude-opus-5-5"
    effort: str = "high"
    efforts: list[str] = Field(default_factory=lambda: ["high", "max"])
    fable_model: str = "claude-fable-5-1"
    fable_per_day: int = 2
    fable_effort: str = "max"
    max_turns: int = 250
    # Silent ceiling — never surfaced to the session (decision 2026-07-29).
    max_budget_usd: Decimal = Decimal("30.00")
    wall_time_min: int = 180


class CellsSettings(_Section):
    """How many of the day's slots run each cell (docs/22 section 8.7).

    The four counts are a multiset, not a schedule: the tick shuffles them once a day and
    stores the permutation, so a cell is never welded to a time of day. They must total the
    day's slot count exactly, and :func:`config_warnings` says so in either direction. The
    rule is Arno's: the baseline and the focused cell run once a day, every day, so a list
    that had to be cut to fit would drop one of them at random on some days and the two
    controls would stop being daily. ``static_recent`` is how many past attempts the static
    cell's CONTEXT.md holds.
    """

    baseline: int = 1
    static: int = 3
    director: int = 4
    focused: int = 1
    static_recent: int = 10

    def counts(self) -> dict[str, int]:
        """``{cell: how many slots}``, in the order the plan's multiset is built."""
        return {
            "baseline": self.baseline, "static": self.static,
            "director": self.director, "focused": self.focused,
        }


class DirectorSettings(_Section):
    """The daily director session (docs/22 section 8.2).

    ``hour_et`` is a ``"HH:MM"`` Eastern time parsed like a slot, not an integer hour: the
    run sits at midnight, between the last slot of one day and the first of the next.
    """

    model: str = "claude-fable-5-1"
    effort: str = "xhigh"
    hour_et: str = "00:00"
    max_turns: int = 120
    max_budget_usd: Decimal = Decimal("40.00")
    wall_time_min: int = 45
    set_size: int = 10
    page_max_chars: int = 4000

    def hour_minute(self) -> tuple[int, int]:
        """``hour_et`` as ``(hour, minute)``.

        Raises ``ValueError`` on anything that is not an ``HH:MM`` clock time in range.
        It used to answer ``(0, 0)``, which silently moved a mistyped director hour to
        midnight and gave an operator no way to find out; :func:`config_warnings` is where
        an unreadable value is said out loud instead.
        """
        raw = str(self.hour_et)
        hh, _, mm = raw.partition(":")
        try:
            hour, minute = int(hh), int(mm)
        except ValueError:
            raise ValueError(
                f"director.hour_et: {raw!r} is not an HH:MM Eastern time"
            ) from None
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"director.hour_et: {raw!r} is not an HH:MM Eastern time")
        return hour, minute


class ReconcileSettings(_Section):
    """The night's verdict bands and the watch on what they absorb (Arno, 2026-09-27).

    A drift at or under ``absorb_usd``, with every cross-check passing, is absorbed: it
    becomes a term in the balance walk from then on, with no alert and no halt. Above it
    and at or under ``halt_drift_usd`` it is noted: one alert, and the attempts keep
    running. Above that, or with any failing check, the night halts as it always did.

    The residual watch raises one alert, never a halt, when the absorbed nights of the
    last ``residual_window_days`` days add up to more than ``residual_alert_usd`` in
    absolute value, or number more than ``residual_alert_nights``.
    """

    hour_et: int = 23
    absorb_usd: Decimal = Decimal("0.50")
    halt_drift_usd: Decimal = Decimal("1.00")
    residual_alert_usd: Decimal = Decimal("2.50")
    residual_alert_nights: int = 10
    residual_window_days: int = 30


class AlertsSettings(_Section):
    """Escalation thresholds (docs/14 D1). Defaults are live; config.toml need not set them.

    ``step_failure_streak`` = 3 ticks is 45 minutes of the same step failing — long enough
    that a single transient blip stays quiet, short enough that docs/12 §8.1's three-day
    silent wedge would have reached a human before its first settlement was late.
    ``settle_halt_streak`` = 20 ticks (~5 h) is the hard line: a settle step that cannot
    reach the exchange for five hours means the money picture is stale, and placement must
    stop rather than keep betting blind (docs/14 D1(d)).
    """

    enabled: bool = True
    step_failure_streak: int = 3
    spawn_failure_streak: int = 3
    settle_halt_streak: int = 20


class BoardSettings(_Section):
    """The shared market-listing cache (docs/14 C1, docs/22 section 6). Defaults ARE the
    spec's values.

    ``refresh_min_interval_min`` = 60 and ``generations_keep`` = 2 are the cadence and the
    retention; both live here rather than as constants so an operator can slow the pull
    down without a code change. The spec set the cadence at 150 minutes because a refresh
    cost about 650 requests and three minutes; carrying series categories across
    generations took most of that away, so the pull now runs on the hour and a slot reads a
    board at most an hour old instead of one up to two and a half hours old.

    Two generations and not one because ``bt movers`` compares
    the newest against the one before it, and a generation under the close-time bound is
    small enough that keeping the pair costs little. ``statuses`` is what the survey needs:
    open markets plus
    settled ones, which is where ``bt series``' settled-market count comes from.
    ``category_lookup_cap`` bounds the one-event-lookup-per-series enrichment (bulk
    listings carry no category at all); ``max_markets`` is a safety stop for a board that
    grows past anything seen in the wild (400k rows so far), null = none.

    The three retrieval bounds are the reason a generation is tens of megabytes instead of
    1.5 GB. ``close_bound_hours`` is the only one the exchange applies for us (the
    ``max_close_ts`` parameter of the markets listing), and it is the same number as
    ``limits.max_resolve_hours``: a market nobody may bet is a market nobody needs cached.
    ``excluded_series`` drops every family whose series name starts with an entry (the
    live parlay families are ``KXMVECROSSCATEGORY`` and ``KXMVECROSSCATEGORY0``), and
    ``excluded_categories`` drops whatever the exchange refuses to take our orders on;
    both are named in every listing's header so a session sees what it is not being
    shown.
    """

    refresh_min_interval_min: int = 60
    generations_keep: int = 2
    statuses: list[str] = Field(default_factory=lambda: ["open", "settled"])
    category_lookup_cap: int = 2000
    max_markets: int | None = None
    close_bound_hours: int = 120
    excluded_series: list[str] = Field(default_factory=lambda: ["KXMVECROSS"])
    excluded_categories: list[str] = Field(
        default_factory=lambda: ["Sports", "Entertainment"]
    )
    # The settled listing has no time bound — it is every market the exchange has ever
    # resolved — so it is the one slice that could grow without limit. Capped, and the cap
    # is recorded in the snapshot so ``bt series`` can call its settled counts a floor
    # rather than a total. The open listing carries no such cap: it IS the survey.
    #
    # 2000 rather than the spec's 20000: at 20000 the slice was 57% of the generation and
    # 89% byte-identical between refreshes, and it serves exactly one column, ``n_settled``
    # in ``bt series``. That column is already announced as a floor whenever the cap bites,
    # which at 2000 it always will, so the cut costs a number no session was reading as a
    # total anyway and buys back most of the file and the pages fetched to fill it.
    settled_max: int = 2000


class ModelsSettings(_Section):
    """A time-boxed model substitution (model substitution, 2026-08-18).

    Every other model name in this file is the experiment's *record of intent*: the 05:00
    slot is the F2 cell because it is configured Fable, and hand-editing those names to
    route around a quota outage would destroy the only written statement of what each slot
    is for. This section routes around it instead, without touching them — one
    requested → effective mapping, applied at every session spawn, expiring on its own.

    ``substitute_until`` is mandatory whenever ``substitute`` is non-empty and must carry
    an explicit UTC offset. Both rules exist for the same reason: the failure mode of a
    substitution is not that it fails to apply, it is that it never stops applying. An
    unbounded switch, or one whose deadline means midnight in an unstated zone, is refused
    outright (:func:`config_warnings` says so on all three surfaces) rather than applied on
    a guess — running the configured model is always the safe direction to fail.
    """

    # requested model name -> the model that actually runs. Empty/absent = no-op.
    # Looked up exactly once per model name, never chained: a mapping is a substitution,
    # not a rewrite rule, and ``{a: b, b: c}`` resolving ``a`` to ``c`` would make the
    # effective model depend on dict ordering.
    substitute: dict[str, str] = Field(default_factory=dict)
    # ISO 8601 WITH offset, e.g. "2026-08-22T00:00:00-07:00". At and after this instant
    # the switch is off with no code change and nothing to clean up.
    substitute_until: str | None = None


class HistorySettings(_Section):
    """What ``bt past`` reads by default (docs/22 sections 4.1 and 9).

    ``current_era`` is stamped on every attempt created from the rebuild on, and it is the
    scope ``bt past`` searches once there is enough of it to search: an era that holds
    fewer than ``era_default_min`` attempts would tell a session in its first week that
    nothing has ever been tried, so until then the default is the whole ledger.
    """

    era_default_min: int = 50
    current_era: str = "live-v2"


class FeesSettings(_Section):
    default_coef: Decimal = Decimal("0.07")
    category_coefs: dict[str, Decimal] = Field(
        default_factory=lambda: {"index": Decimal("0.035")}
    )


# --------------------------------------------------------------------------- settings


class _DictSource(PydanticBaseSettingsSource):
    """A settings source that yields a pre-loaded ``config.toml`` dict.

    Slotted between env and file-secret sources so env wins over the toml, which wins
    over model defaults. pydantic-settings deep-merges nested-model dicts across sources,
    so a partial env override keeps the toml's other fields.
    """

    def __init__(self, settings_cls: type[BaseSettings], data: dict) -> None:
        super().__init__(settings_cls)
        self._data = data or {}

    def get_field_value(self, field, field_name):  # noqa: ANN001 - base signature
        return self._data.get(field_name), field_name, False

    def __call__(self) -> dict:
        return dict(self._data)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BETTING_AGENT_",
        env_nested_delimiter="__",
        # D5: keep the unknown so it can be reported, rather than dropping it silently
        # ("ignore", the CI-1 defect) or refusing to boot over it ("forbid").
        extra="allow",
    )

    _root: Path = PrivateAttr(default_factory=lambda: _resolve_root(None))

    kalshi: KalshiSettings = Field(default_factory=KalshiSettings)
    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)
    stakes: StakesSettings = Field(default_factory=StakesSettings)
    attempt: AttemptSettings = Field(default_factory=AttemptSettings)
    cells: CellsSettings = Field(default_factory=CellsSettings)
    director: DirectorSettings = Field(default_factory=DirectorSettings)
    history: HistorySettings = Field(default_factory=HistorySettings)
    fees: FeesSettings = Field(default_factory=FeesSettings)
    reconcile: ReconcileSettings = Field(default_factory=ReconcileSettings)
    alerts: AlertsSettings = Field(default_factory=AlertsSettings)
    board: BoardSettings = Field(default_factory=BoardSettings)
    models: ModelsSettings = Field(default_factory=ModelsSettings)

    # ----------------------------------------------- model substitution (2026-08-18)
    def model_substitutions(self, now: datetime | None = None) -> dict[str, str]:
        """The requested → effective map in force at ``now``; ``{}`` when the switch is off.

        Off means every one of: no mapping configured, a mapping whose expiry is missing,
        naive or unparseable (see :class:`ModelsSettings`), or an expiry that ``now`` has
        reached. The comparison is between two instants in UTC, so the deadline's own
        offset is honored rather than reinterpreted.

        Identity and non-string entries are dropped here rather than at the call sites, so
        ``effective_model`` and the substitution marker recorded on an attempt agree by
        construction about what "a substitution applied" means.
        """
        deadline = self.substitution_deadline()
        if deadline is None:
            return {}
        now = now or utc_now()
        if now.tzinfo is None:  # a naive clock has no instant to compare (never in prod)
            return {}
        if now >= deadline:
            return {}
        return {
            k: v
            for k, v in (self.models.substitute or {}).items()
            if isinstance(k, str) and isinstance(v, str) and k and v and k != v
        }

    def substitution_deadline(self) -> datetime | None:
        """The instant the switch turns itself off, or ``None`` when it is not armed.

        ``None`` covers "nothing configured" and "configured but refused" alike: the
        caller's job is the same either way (run the configured model), and
        :func:`config_warnings` is where the difference is spelled out to a human.
        """
        if not (self.models.substitute or {}):
            return None
        return parse_iso_offset(self.models.substitute_until or "")

    def substitution_active(self, now: datetime | None = None) -> bool:
        """Is a model substitution in force right now? The companion predicate."""
        return bool(self.model_substitutions(now))

    def effective_model(self, requested: str, now: datetime | None = None) -> str:
        """The model that should ACTUALLY run where ``requested`` was configured.

        The single resolution point for every session spawn (2026-08-18). A model the map
        does not name passes through untouched, and so does every model once the switch
        expires — which is what makes reverting a matter of the clock rather than of a
        deploy.
        """
        return self.model_substitutions(now).get(requested, requested)

    # --- derived paths (all under <root>/data) ---
    @property
    def root(self) -> Path:
        return self._root

    @property
    def data_dir(self) -> Path:
        return self._root / "data"

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.db"

    @property
    def attempts_dir(self) -> Path:
        return self.data_dir / "attempts"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    # The daily status digest (docs/22 section 10), one dated Markdown page per ET day.
    @property
    def status_dir(self) -> Path:
        return self.data_dir / "status"

    # One workspace per director run (docs/22 section 8.2), named by its Eastern run date.
    @property
    def director_dir(self) -> Path:
        return self.data_dir / "director"

    @property
    def locks_dir(self) -> Path:
        return self.data_dir / "locks"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    # The board cache's generations (docs/14 C1). Derived like every other data path, so
    # tests point it at tmp_path by rooting Settings there and nothing hardcodes data/board.
    @property
    def board_dir(self) -> Path:
        return self.data_dir / "board"

    @property
    def halt_path(self) -> Path:
        return self.data_dir / "HALT"


# --------------------------------------------------------------------------- unknown keys (CI-1)

# Every ``Settings`` field that is itself a config section. Anything in a *known* section's
# ``model_extra`` is an unknown key inside it; anything in ``Settings.model_extra`` is an
# unknown top-level key or section.
def _section_fields(settings: Settings) -> dict[str, BaseModel]:
    return {
        name: value
        for name, value in ((n, getattr(settings, n, None)) for n in type(settings).model_fields)
        if isinstance(value, BaseModel)
    }


def _unknown_env_keys(settings: Settings, env) -> list[str]:
    """Dotted paths of ``BETTING_AGENT_*`` vars that name nothing.

    Needed because ``model_extra`` cannot see all of them: pydantic-settings resolves
    top-level env vars by *asking each known field for its name*, so a typo'd
    ``BETTING_AGENT_STAKEZ`` is never read by anything and would leave no trace. Nested
    typos (``BETTING_AGENT_ATTEMPT__MAX_TURNZ``) do reach ``model_extra``; this walk
    finds them too, and the caller de-duplicates.
    """
    prefix = "BETTING_AGENT_"
    found: list[str] = []
    for raw in env:
        if not raw.startswith(prefix) or len(raw) == len(prefix):
            continue
        parts = [p.lower() for p in raw[len(prefix):].split("__") if p]
        model: BaseModel = settings
        path: list[str] = []
        for part in parts:
            fields = type(model).model_fields
            if part not in fields:
                found.append(".".join([*path, part]))
                break
            path.append(part)
            child = getattr(model, part, None)
            if not isinstance(child, BaseModel):
                break  # a leaf (or a dict field like models.substitute): the rest is data
            model = child
    return found


def unknown_keys(settings: Settings, env=None) -> list[str]:
    """Dotted paths of every configured key no model recognizes (CI-1, D5).

    Sources are indistinguishable by the time they reach the model — a stray
    ``[limtis]`` in ``config.toml`` and a stray ``BETTING_AGENT_LIMTIS__X`` env var both
    land in ``Settings.model_extra`` — so the report names the *path*, which is what an
    operator needs either way. Sorted and de-duplicated, so a warning line and an audit
    detail written by different processes are byte-identical.

    Only ``BaseModel`` children are walked. That is what keeps the quoted-key tables
    (``[models.substitute]``, ``fees.category_coefs``) from misfiring: they are ``dict``
    *values* of known fields, so ``"claude-fable-5"`` is data, not an unrecognized field
    name, and neither walk descends into a dict.
    """
    found: list[str] = []
    for key in getattr(settings, "model_extra", None) or {}:
        # An unknown top-level key OR a whole unknown section: report it once, by name.
        # Descending into it would print one line per key of a section that is entirely
        # unrecognized — noise around the single fact that matters.
        found.append(str(key))
    for name, section in _section_fields(settings).items():
        for key in getattr(section, "model_extra", None) or {}:
            found.append(f"{name}.{key}")
    found += _unknown_env_keys(settings, os.environ if env is None else env)
    return sorted(set(found))


def unknown_keys_message(unknown: list[str]) -> str:
    """The one-line human rendering shared by the CLI warning, the audit and the report."""
    return (
        f"unknown config keys ignored (running on defaults): {', '.join(unknown)}"
        if unknown
        else "none"
    )


# --------------------------------------------------------------- config warnings (docs/14 A3)
def _substitution_warnings(settings: Settings, now: datetime | None) -> list[str]:
    """The model-substitution lines of :func:`config_warnings` (2026-08-18).

    Three states, three sentences. **Refused** (an expiry that is missing, naive or
    unparseable): the config asks for a substitution and none is happening, which is the
    A3 failure shape exactly. **Active**: the config names Fable and Opus is running, so an
    operator reading ``config.toml`` next to a ``sessions`` row must not have to guess why
    they disagree. **Expired**: nothing at all — the line has to vanish by itself on the
    day the quota comes back, or the next substitution's warning is already noise.
    """
    table = settings.models.substitute or {}
    if not table:
        return []
    raw_until = (settings.models.substitute_until or "").strip()
    if not raw_until:
        return [
            "models.substitute_until: missing — a substitution with no expiry is the one "
            "that outlives its reason; no substitution applied "
            f"({', '.join(f'{k} stays {k}' for k in sorted(table))})"
        ]
    if parse_iso_offset(raw_until) is None:
        return [
            f'models.substitute_until: "{raw_until}" is not an ISO 8601 instant with a '
            "UTC offset (e.g. 2026-08-22T00:00:00-07:00) — midnight in an unstated zone "
            "is not an instant; no substitution applied"
        ]
    active = settings.model_substitutions(now)
    if not active:
        return []  # expired: reverted on its own, and says nothing about it
    deadline = settings.substitution_deadline()
    pairs = ", ".join(f"{k} runs as {active[k]}" for k in sorted(active))
    return [
        f"models.substitute: {pairs} until {iso(deadline)} — config.toml's model names "
        "are the configured intent, not what is running; sessions and the substitution "
        "marker on each attempt's variant record what actually ran"
    ]


def config_warnings(settings: Settings, now: datetime | None = None) -> list[str]:
    """Configurations that are valid, readable, and quietly not doing what they say.

    The D5 sibling of :func:`unknown_keys`, and surfaced the same three ways (stderr once
    per process, a once-per-ET-day ``config_health_warning`` audit, and the status digest
    that reads those rows) with the same rule: flag, never stop. These are not typos, every
    key here is recognized, so folding them into the unknown-key line would mislabel them.

    ``now`` exists only so the substitution check below can be tested on both sides of its
    expiry; every production caller omits it and gets the wall clock.

    Seven checks.

    * **Cells that do not fill the day (docs/22 sections 3.3 and 8.7).** The day's plan is
      the four cell counts as a multiset, shuffled over the day's slots, so the counts have
      to total the slot count exactly. Too few leaves slots with no cell at all; too many
      means a cell is dropped from the draw, and since the baseline and the focused cell
      run once a day it would be one of the two controls that vanished.
    * **An effort level ``claude`` does not accept.** Every configured effort reaches a
      session's ``--effort`` flag, so a misspelled one is a slot that cannot start.
    * **More Fable slots than the day has.** The draw gives every slot to Fable and none to
      ``attempt.model``, which is a different experiment from the one configured.
    * **An unreadable director hour (docs/22 section 8.2).** ``director.hour_et`` is an
      ``HH:MM`` Eastern time parsed like a slot. A value that does not parse is not a
      substitute midnight; it is a config nobody can act on, and the tick raises on it
      rather than guessing, so it has to be readable here first.
    * **An absorb band wider than the halt band (2026-09-27).** ``reconcile.absorb_usd``
      above ``reconcile.halt_drift_usd`` would absorb drifts that are meant to halt, since
      the absorbed band is tested first. The config still loads; the warning says so.
    * **An attempt allowance above the daily cap (2026-09-27).**
      ``stakes.per_attempt_real_cap`` above ``stakes.daily_real_stake_cap`` can never bind,
      because the daily cap refuses first, so the allowance the prompt promises is not the
      one in force.
    * **The model-substitution switch (2026-08-18):** see :func:`_substitution_warnings`.
      It is here rather than on a channel of its own because it is the same sentence as the
      others, in that the config says one thing and another is happening, and because "it
      disappears when it stops being true" is already how this surface behaves.
    """
    warnings: list[str] = []
    slots = list(settings.schedule.slots or [])
    counts = settings.cells.counts()
    total = sum(counts.values())
    if slots and total != len(slots):
        spelled = ", ".join(f"{name}={n}" for name, n in counts.items())
        warnings.append(
            f"cells: {spelled} total {total}, which is not the {len(slots)} slots/day in "
            f"schedule.slots; the day's plan is one cell per slot, so the counts have to "
            f"total the slot count exactly"
        )
    attempt = settings.attempt
    for key, value in (
        ("attempt.effort", attempt.effort),
        *(("attempt.efforts", level) for level in attempt.efforts),
        ("attempt.fable_effort", attempt.fable_effort),
        ("director.effort", settings.director.effort),
    ):
        if value not in EFFORTS:
            warnings.append(
                f'{key}: "{value}" is not one of {", ".join(EFFORTS)}, the levels '
                f"claude --effort accepts; a session launched with it cannot start"
            )
    if slots and attempt.fable_per_day > len(slots):
        warnings.append(
            f"attempt.fable_per_day: {attempt.fable_per_day} is more than the "
            f"{len(slots)} slots/day in schedule.slots, so every slot runs "
            f"{attempt.fable_model} and none runs {attempt.model}"
        )
    try:
        settings.director.hour_minute()
    except ValueError:
        warnings.append(
            f'director.hour_et: "{settings.director.hour_et}" is not an HH:MM Eastern '
            f"time (e.g. 00:00), which is how a slot is written; the director step "
            f"cannot say when it is due"
        )
    rec = settings.reconcile
    if rec.absorb_usd > rec.halt_drift_usd:
        warnings.append(
            f"reconcile.absorb_usd: {rec.absorb_usd} is more than reconcile.halt_drift_usd "
            f"{rec.halt_drift_usd}, so every drift up to {rec.absorb_usd} is absorbed "
            f"into the walk and no drift is ever noted before it halts"
        )
    stakes = settings.stakes
    if stakes.per_attempt_real_cap > stakes.daily_real_stake_cap:
        warnings.append(
            f"stakes.per_attempt_real_cap: {stakes.per_attempt_real_cap} is more than "
            f"stakes.daily_real_stake_cap {stakes.daily_real_stake_cap}, so the daily cap "
            f"refuses first and the attempt allowance never binds"
        )
    warnings += _substitution_warnings(settings, now)
    return warnings


def config_warnings_message(warnings: list[str]) -> str:
    """The one-line human rendering shared by the CLI warning, the audit and the report."""
    return "; ".join(warnings) if warnings else "none"


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def _read_secret_env(root: Path) -> dict[str, str]:
    """Secrets from a ``.env`` file overlaid by the live process environment."""
    out: dict[str, str] = {}
    env_path = root / ".env"
    if env_path.exists():
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    for k in _SECRET_KEYS:
        if k in os.environ:
            out[k] = os.environ[k]
    return out


def load_settings(root: Path | None = None) -> Settings:
    """Load settings with the spec §4 precedence and inject Kalshi secrets."""
    root = _resolve_root(root)
    toml_data = _read_toml(root / "config.toml")

    class _RootedSettings(Settings):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            return (
                init_settings,
                env_settings,
                dotenv_settings,
                _DictSource(settings_cls, toml_data),
                file_secret_settings,
            )

    settings = _RootedSettings()
    settings._root = root

    secrets = _read_secret_env(root)
    if secrets.get("KALSHI_API_KEY_ID"):
        settings.kalshi.key_id = secrets["KALSHI_API_KEY_ID"]
    if secrets.get("KALSHI_PRIVATE_KEY_PATH"):
        settings.kalshi.private_key_path = Path(secrets["KALSHI_PRIVATE_KEY_PATH"]).expanduser()

    # CI-5: a key path written as "~/keys/prod.pem" in config.toml (or in
    # BETTING_AGENT_KALSHI__PRIVATE_KEY_PATH) reached the signer verbatim and failed to
    # open, while the same string in .env worked — the two paths had different rules for
    # no reason. Applied last so it covers every source; ``expanduser`` is idempotent.
    if settings.kalshi.private_key_path is not None:
        settings.kalshi.private_key_path = Path(settings.kalshi.private_key_path).expanduser()

    return settings
