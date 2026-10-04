"""The time-boxed model-substitution switch (2026-08-18).

The Fable quota ran out, so every session that ``config.toml`` configures as Fable has to
run Opus until it comes back — without editing a single Fable name, because those names are
the experiment's record of intent. Four things have to be true at once, and each has a
distinct way of being silently wrong:

* **It applies everywhere a session is born.** A spawn site that skipped the helper would
  spend real money on a model the ledger says did not run. Every ``SessionSpec`` in ``src``
  is checked structurally *and* driven end to end below.
* **It stops on its own.** A switch that fails closed forever is the failure this file
  cares about most: it is invisible (Opus works fine), it corrupts every recipe comparison
  it touches, and nothing would ever prompt anyone to look. The expiry instant is tested on
  both sides, and refused outright when it is missing or has no offset.
* **The record stays honest.** The day's plan keeps the arm a slot was drawn for; the
  attempt row, its launch audit and every ``sessions`` row carry what actually ran, and
  ``variant.model_substitutions`` names the model that was asked for (2026-09-26).
* **It says so out loud** on all three config-health surfaces while it is up, and on none
  of them once it has expired.
"""

from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from betting_agent import cli
from betting_agent.config import Settings, config_warnings, load_settings
from betting_agent.harness import attempt as attempt_mod
from betting_agent.harness import cells
from betting_agent.harness.attempt import run_attempt
from betting_agent.ledger.db import Ledger
from betting_agent.sessions import SessionResult
from betting_agent.timeutil import parse_iso

runner = CliRunner()

FABLE = "claude-fable-5"
OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"

# The live switch's deadline: end of Friday Aug 21 at UTC-7, i.e. 07:00Z on the 22nd.
UNTIL = "2026-08-22T00:00:00-07:00"
DEADLINE = parse_iso("2026-08-22T07:00:00Z")
DURING = parse_iso("2026-08-18T12:00:00Z")

# Wall-clock-independent bounds, for the paths that read the real clock at spawn time.
FOREVER = "2099-01-01T00:00:00+00:00"
LONG_GONE = "2000-01-01T00:00:00+00:00"

_NOW = parse_iso("2026-08-18T12:00:00Z")


def _switch(settings, until: str | None = UNTIL, table=None):
    """Arm the switch on an already-loaded Settings and hand it back."""
    settings.models.substitute = dict(table if table is not None else {FABLE: OPUS})
    settings.models.substitute_until = until
    return settings


# =========================================================================== D1: the helper
def test_a_model_the_table_names_is_substituted_before_the_expiry():
    s = _switch(Settings())
    assert s.effective_model(FABLE, DURING) == OPUS
    assert s.substitution_active(DURING) is True
    assert s.model_substitutions(DURING) == {FABLE: OPUS}


def test_the_switch_is_off_at_the_expiry_instant_and_after_it():
    """The boundary is ``>=``: at the instant itself the quota is back, and a switch that
    lingers 'just past' its deadline is a switch nobody trusts to end."""
    s = _switch(Settings())
    assert s.effective_model(FABLE, DEADLINE) == FABLE
    assert s.substitution_active(DEADLINE) is False
    later = parse_iso("2026-08-22T07:00:01Z")
    assert s.effective_model(FABLE, later) == FABLE
    assert s.model_substitutions(later) == {}


def test_the_switch_is_still_on_one_second_before_the_expiry_instant():
    """The other side of the same boundary — off-by-one in this direction reverts a day
    early and spends a quota that is not back yet."""
    s = _switch(Settings())
    just_before = parse_iso("2026-08-22T06:59:59Z")
    assert s.effective_model(FABLE, just_before) == OPUS


def test_the_deadlines_own_offset_is_honored_not_reinterpreted():
    """``2026-08-22T00:00:00-07:00`` is 07:00Z, not 00:00Z. Reading the wall-clock digits
    and ignoring the offset would end the switch seven hours early."""
    s = _switch(Settings())
    assert s.substitution_deadline() == DEADLINE
    assert s.effective_model(FABLE, parse_iso("2026-08-22T03:00:00Z")) == OPUS
    assert s.effective_model(FABLE, parse_iso("2026-08-22T08:00:00Z")) == FABLE


def test_an_empty_table_is_a_no_op_and_arms_nothing():
    s = Settings()
    assert s.models.substitute == {}
    assert s.effective_model(FABLE, DURING) == FABLE
    assert s.substitution_active(DURING) is False
    assert s.substitution_deadline() is None
    # Even with an expiry set: no table, no switch, and nothing to warn about.
    s.models.substitute_until = UNTIL
    assert s.substitution_deadline() is None
    assert config_warnings(s, now=DURING) == []


def test_a_model_the_table_does_not_name_passes_through():
    s = _switch(Settings())
    assert s.effective_model(SONNET, DURING) == SONNET
    assert s.effective_model(OPUS, DURING) == OPUS
    assert s.effective_model("some-model-nobody-has-run", DURING) == (
        "some-model-nobody-has-run"
    )


def test_the_table_is_not_chained_a_substitution_is_looked_up_once():
    """``{a: b, b: c}`` must resolve ``a`` to ``b``. Chaining would make the effective
    model depend on dict ordering, which is not a property anyone should have to reason
    about while a real-money loop is running."""
    s = _switch(Settings(), table={FABLE: OPUS, OPUS: SONNET})
    assert s.effective_model(FABLE, DURING) == OPUS


def test_an_identity_mapping_is_not_a_substitution():
    """``{fable: fable}`` changes nothing, so it must not raise a marker or a warning that
    says something is being substituted."""
    s = _switch(Settings(), table={FABLE: FABLE})
    assert s.model_substitutions(DURING) == {}
    assert s.substitution_active(DURING) is False
    assert config_warnings(s, now=DURING) == []


@pytest.mark.parametrize("until", [None, "", "   "])
def test_a_substitution_with_no_expiry_is_refused_and_warned_about(until):
    """The failure mode of a substitution is not that it fails to apply — it is that it
    never stops. An unbounded one is the exact shape that outlives its reason, so it is
    refused rather than applied indefinitely."""
    s = _switch(Settings(), until=until)
    assert s.effective_model(FABLE, DURING) == FABLE
    assert s.substitution_active(DURING) is False
    warned = config_warnings(s, now=DURING)
    assert len(warned) == 1
    assert "models.substitute_until: missing" in warned[0]
    assert "no substitution applied" in warned[0]


@pytest.mark.parametrize("until", [
    "2026-08-22T00:00:00",      # naive: midnight in an unstated zone
    "2026-08-22",               # naive date
    "friday",                   # not a timestamp at all
    "2026-13-45T99:00:00Z",     # well-shaped and impossible
])
def test_a_naive_or_unreadable_expiry_is_refused_and_warned_about(until):
    """Never silently apply an unbounded substitution: a deadline nobody can place on the
    timeline is not a deadline, and guessing UTC would move one meant for UTC-7 seven hours."""
    s = _switch(Settings(), until=until)
    assert s.effective_model(FABLE, DURING) == FABLE
    assert s.substitution_active(DURING) is False
    warned = config_warnings(s, now=DURING)
    assert len(warned) == 1
    assert "models.substitute_until" in warned[0]
    assert "UTC offset" in warned[0]
    assert "no substitution applied" in warned[0]


def test_the_switch_reads_out_of_a_config_toml_with_no_code_change(tmp_path):
    """The whole point is that arming and disarming this is a config edit and then a
    calendar date, not a deploy."""
    (tmp_path / "config.toml").write_text(
        '[models]\n'
        f'substitute = {{ "{FABLE}" = "{OPUS}" }}\n'
        f'substitute_until = "{UNTIL}"\n'
    )
    s = load_settings(root=tmp_path)
    assert s.models.substitute == {FABLE: OPUS}
    assert s.effective_model(FABLE, DURING) == OPUS
    assert s.effective_model(FABLE, DEADLINE) == FABLE


# ================================================================== D4: the three surfaces
def _root(tmp_path, toml: str) -> Path:
    (tmp_path / "data").mkdir()
    lg = Ledger.open(tmp_path / "data" / "ledger.db")
    lg.migrate()
    lg.close()
    (tmp_path / "config.toml").write_text(toml)
    return tmp_path


def _switch_toml(until: str) -> str:
    return (
        '[models]\n'
        f'substitute = {{ "{FABLE}" = "{OPUS}" }}\n'
        f'substitute_until = "{until}"\n'
    )


def _reset_warned(monkeypatch):
    """Both stderr warnings are once-per-process; the whole suite shares one process."""
    monkeypatch.setattr(cli, "_CONFIG_WARNED", False)
    monkeypatch.setattr(cli, "_SCHEDULE_WARNED", False)


def test_an_active_substitution_is_named_on_both_config_health_surfaces(
    tmp_path, monkeypatch
):
    """An operator must never have to wonder why Opus is running in a Fable slot. Same
    channels as every other config-health warning: stderr and a once-per-ET-day audit
    event. There was a third, the daily report's health line, until docs/22 phase one
    deleted the report."""
    root = _root(tmp_path, _switch_toml(FOREVER))
    monkeypatch.setenv("BT_ROOT", str(root))
    _reset_warned(monkeypatch)

    res = runner.invoke(cli.app, ["status"])
    assert res.exit_code == 0                                  # flag, never stop
    assert f"{FABLE} runs as {OPUS}" in res.stderr
    assert "2099-01-01T00:00:00Z" in res.stderr                # ... and when it ends

    lg = Ledger.open(root / "data" / "ledger.db", readonly=True)
    events = lg.audit_events(event="config_health_warning")
    lg.close()
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert any(f"{FABLE} runs as {OPUS}" in w for w in detail["warnings"])


def test_an_expired_substitution_says_nothing_on_any_surface(tmp_path, monkeypatch):
    """The line has to vanish by itself on the day the quota comes back. A warning that
    outlives the condition it describes is how the next real one gets ignored."""
    root = _root(tmp_path, _switch_toml(LONG_GONE))
    monkeypatch.setenv("BT_ROOT", str(root))
    _reset_warned(monkeypatch)

    res = runner.invoke(cli.app, ["status"])
    assert res.exit_code == 0
    assert "runs as" not in res.stderr

    lg = Ledger.open(root / "data" / "ledger.db", readonly=True)
    assert lg.audit_events(event="config_health_warning") == []
    lg.close()


# ============================================================ D2: every spawn site resolves
@pytest.fixture
def env(tmp_path):
    (tmp_path / "data").mkdir()
    ledger = Ledger.open(tmp_path / "data" / "ledger.db")
    ledger.migrate()
    settings = load_settings(root=tmp_path)
    client = SimpleNamespace(
        get_market=lambda *a, **k: None, get_orderbook=lambda *a, **k: None
    )
    yield settings, ledger, client
    ledger.close()


def _inject_intake(monkeypatch, *, execute_ret="placed"):
    parsed = SimpleNamespace(
        ok=True, edge_claim_md="## Markets\nx", hypothesis_md="## If we're right\nx",
        manifest_md="m",
    )
    v = types.ModuleType("betting_agent.harness.validate")
    v.parse_ticket = MagicMock(return_value=parsed)
    v.validate_ticket = MagicMock(return_value="validated-obj")
    e = types.ModuleType("betting_agent.harness.execute")
    e.execute_attempt = MagicMock(return_value=execute_ret)
    monkeypatch.setitem(sys.modules, "betting_agent.harness.validate", v)
    monkeypatch.setitem(sys.modules, "betting_agent.harness.execute", e)


_TICKET = {
    "attempt": "A-0001",
    "bets": [{"ticker": "T1", "side": "yes", "limit_price": "0.4200",
              "contracts": 1, "rationale": "r"}],
}


def _attempt_runner(monkeypatch):
    """Stub ``run_session`` for the attempt; returns the specs it saw."""
    seen: list = []

    def fake_run_session(spec, runner_cmd=None, stream_path=None, err_path=None):
        seen.append(spec)
        tdir = spec.add_dirs[0] / "ticket"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "bets.json").write_text(json.dumps(_TICKET))
        (tdir / "edge_claim.md").write_text("## Markets\n## Why this is profitable\n")
        return SessionResult(
            exit_kind="ok", is_error=False, result_text=None, structured_output=None,
            session_id=spec.session_id, cost_usd=None, input_tokens=None,
            output_tokens=None, num_turns=2, wall_seconds=1, init_manifest=None,
        )

    monkeypatch.setattr(attempt_mod, "run_session", fake_run_session)
    return seen


def _session_models(ledger, attempt_id: str) -> dict[str, str]:
    rows = ledger.conn.execute(
        "SELECT kind, model FROM sessions WHERE attempt_id=?", (attempt_id,)
    ).fetchall()
    return {r["kind"]: r["model"] for r in rows}


def test_an_attempt_on_a_substituted_model_runs_the_effective_one(env, monkeypatch):
    """The session, its ``sessions`` row and the attempt row all say Opus, which is what
    ran (2026-09-26); the marker keeps the Fable that was asked for."""
    settings, ledger, client = env
    _switch(settings, until=FOREVER)
    _inject_intake(monkeypatch)
    seen = _attempt_runner(monkeypatch)

    aid = run_attempt(ledger, client, settings, now=_NOW, model=FABLE, cell="baseline")

    assert [s.model for s in seen] == [OPUS]
    assert _session_models(ledger, aid) == {"attempt": OPUS}
    row = ledger.get_attempt(aid)
    assert row["model"] == OPUS                       # what ran
    assert json.loads(row["variant"])["model_substitutions"] == {FABLE: OPUS}


def test_the_same_attempt_runs_fable_again_once_the_switch_expires(env, monkeypatch):
    """Reversion is the clock's job, not a deploy's. Nothing about the attempt differs
    from an attempt run before the switch existed — the marker is absent, not empty."""
    settings, ledger, client = env
    _switch(settings, until=LONG_GONE)
    _inject_intake(monkeypatch)
    seen = _attempt_runner(monkeypatch)

    aid = run_attempt(ledger, client, settings, now=_NOW, model=FABLE, cell="baseline")

    assert [s.model for s in seen] == [FABLE]
    assert _session_models(ledger, aid) == {"attempt": FABLE}
    assert ledger.get_attempt(aid)["variant"] is None


def test_an_unsubstituted_attempt_records_no_marker_at_all(env, monkeypatch):
    """An Opus attempt under a live Fable→Opus switch must be indistinguishable from the
    same attempt with no switch configured at all, ``variant`` bytes included."""
    settings, ledger, client = env
    _switch(settings, until=FOREVER)
    _inject_intake(monkeypatch)
    seen = _attempt_runner(monkeypatch)

    aid = run_attempt(ledger, client, settings, now=_NOW, model=SONNET, cell="baseline")

    assert [s.model for s in seen] == [SONNET]
    assert ledger.get_attempt(aid)["variant"] is None


def test_the_attempt_launched_audit_carries_configured_and_effective(env, monkeypatch):
    settings, ledger, client = env
    _switch(settings, until=FOREVER)
    _inject_intake(monkeypatch)
    _attempt_runner(monkeypatch)

    aid = run_attempt(ledger, client, settings, now=_NOW, model=FABLE, cell="baseline")

    events = [e for e in ledger.audit_events(event="attempt_launched")
              if e["attempt_id"] == aid]
    detail = json.loads(events[0]["detail"])
    assert detail["model"] == OPUS                    # what ran
    assert detail["model_substitutions"] == {FABLE: OPUS}   # and what was asked for


def test_a_fable_slot_runs_the_substitute_at_its_own_effort(env, monkeypatch):
    """The switch swaps the model of a Fable slot and leaves its effort alone. The day's
    plan still names the Fable arm the slot was drawn for."""
    settings, ledger, client = env
    fable = settings.attempt.fable_model
    _switch(settings, until=FOREVER, table={fable: OPUS})
    _inject_intake(monkeypatch)
    seen = _attempt_runner(monkeypatch)
    plan = cells.plan(ledger, settings, "2026-08-18")
    index = next(i for i, e in enumerate(plan) if e["model"] == fable)

    aid = run_attempt(ledger, client, settings, now=_NOW,
                      slot=f"slot:2026-08-18/{settings.schedule.slots[index]}")

    effort = settings.attempt.fable_effort
    assert [(s.model, s.effort) for s in seen] == [(OPUS, effort)]
    assert _session_models(ledger, aid) == {"attempt": OPUS}
    row = ledger.get_attempt(aid)
    assert (row["model"], row["effort"]) == (OPUS, effort)
    assert json.loads(row["variant"])["model_substitutions"] == {fable: OPUS}
    assert cells.plan(ledger, settings, "2026-08-18")[index]["model"] == fable


# ============================================ D2: a new spawn site cannot skip the helper
_SRC = Path(__file__).resolve().parents[1] / "src" / "betting_agent"


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"), str(p)))
            for p in sorted(_SRC.rglob("*.py"))]


def _func_of(tree: ast.Module, node: ast.AST) -> ast.FunctionDef | None:
    """The innermost ``def`` containing ``node`` (by deepest enclosing lineno)."""
    best = None
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if any(n is node for n in ast.walk(fn)) and (
            best is None or fn.lineno > best.lineno
        ):
            best = fn
    return best


def _resolved_names(fn: ast.FunctionDef) -> set[str]:
    """Names this function binds to an ``…effective_model(…)`` call."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "effective_model":
            continue
        out.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return out


def _model_kwarg(call: ast.Call) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == "model":
            return kw.value
    return None


def _callers_pass_a_resolved_model(func_name: str, modules) -> bool:
    """Every in-``src`` call to ``func_name`` passes a ``model=`` the caller resolved."""
    found = False
    for _path, tree in modules:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = (node.func.attr if isinstance(node.func, ast.Attribute)
                      else getattr(node.func, "id", None))
            if target != func_name:
                continue
            value = _model_kwarg(node)
            if not isinstance(value, ast.Name):
                return False
            caller = _func_of(tree, node)
            if caller is None or value.id not in _resolved_names(caller):
                return False
            found = True
    return found


def test_every_session_spawn_site_resolves_its_model_through_the_switch():
    """A structural guard, because the end-to-end tests above can only cover the spawn
    sites that exist today. A new one added next month that writes
    ``model=settings.whatever.model`` would spend real money on Fable during a quota
    outage and record a model that never ran — and no behavioral test would notice,
    because nothing would call it. The rule enforced here: the ``model=`` handed to a
    ``SessionSpec`` is a name bound by ``effective_model`` in the same function, or a
    parameter every in-``src`` caller resolves the same way.
    """
    modules = _modules()
    sites = 0
    for path, tree in modules:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (node.func.attr if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", None))
            if name != "SessionSpec":
                continue
            sites += 1
            where = f"{path.name}:{node.lineno}"
            value = _model_kwarg(node)
            assert isinstance(value, ast.Name), (
                f"{where}: SessionSpec(model=…) must be a name resolved through "
                f"Settings.effective_model, not an expression read straight off settings"
            )
            fn = _func_of(tree, node)
            assert fn is not None, f"{where}: SessionSpec built outside any function"
            if value.id in _resolved_names(fn):
                continue
            assert value.id in {a.arg for a in fn.args.args + fn.args.kwonlyargs}, (
                f"{where}: '{value.id}' is neither resolved by effective_model here nor "
                f"a parameter of {fn.name}()"
            )
            assert _callers_pass_a_resolved_model(fn.name, modules), (
                f"{where}: {fn.name}() takes its model from callers, and at least one "
                f"caller passes a model that never went through effective_model"
            )
    # Two sites since docs/22 section 8.2: the attempt and the daily director. The floor
    # is what keeps this test from passing by scanning nothing at all.
    assert sites >= 2, f"expected at least two spawn sites, scanned {sites}"
