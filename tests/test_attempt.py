"""Attempt orchestration: workspace, rendering, contract rule, intake (docs/22 section 5.2).

The validator/executor are mocked (injected into ``sys.modules`` before the lazy import
inside ``run_attempt``); a real stub session runner drives the subprocess path.

One loop, one session, one experimental knob: the cell. What used to be tested here about
ideation, critics, rankings, shadow candidates, recipes and the priors arm is gone with the
code that did it (docs/22 section 2.1).
"""

import hashlib
import json
import sys
import types
from datetime import timedelta
from decimal import Decimal as D
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from betting_agent.config import load_settings
from betting_agent.harness import attempt as attempt_mod
from betting_agent.harness import cells
from betting_agent.harness.attempt import run_attempt
from betting_agent.ledger.db import Ledger
from betting_agent.sessions import SessionResult
from betting_agent.timeutil import iso, parse_iso
from helpers import stub_runner_cmd

_NOW = parse_iso("2026-07-07T14:00:00Z")

PRINCIPLES = """# Principles to guide you

1. Be ambitious. Compute is abundant here, and depth is rewarded.

## What the record says

Start from the price as the baseline.
"""


@pytest.fixture
def env(tmp_path):
    (tmp_path / "data").mkdir()
    ledger = Ledger.open(tmp_path / "data" / "ledger.db")
    ledger.migrate()
    settings = load_settings(root=tmp_path)
    settings.attempt.runner = stub_runner_cmd()
    client = SimpleNamespace(
        get_market=lambda *a, **k: None, get_orderbook=lambda *a, **k: None
    )
    yield settings, ledger, client
    ledger.close()


def _write_principles(root: Path, text: str = PRINCIPLES) -> Path:
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    path = docs / "principles.md"
    path.write_text(text, encoding="utf-8")
    return path


def _parsed(**over):
    kw = dict(
        ok=True, edge_claim_md="## Markets\nNYC weather", hypothesis_md="## If we're right\nx",
        manifest_md="method notes",
    )
    kw.update(over)
    return SimpleNamespace(**kw)


def _inject_intake(monkeypatch, parsed, *, execute_ret="placed"):
    """Install fake validate/execute modules so the lazy imports resolve to mocks."""
    v = types.ModuleType("betting_agent.harness.validate")
    v.parse_ticket = MagicMock(return_value=parsed)
    v.validate_ticket = MagicMock(return_value="validated-obj")
    e = types.ModuleType("betting_agent.harness.execute")
    e.execute_attempt = MagicMock(return_value=execute_ret)
    monkeypatch.setitem(sys.modules, "betting_agent.harness.validate", v)
    monkeypatch.setitem(sys.modules, "betting_agent.harness.execute", e)
    return v, e


def _write_fixture_ticket(tmp_path) -> Path:
    d = tmp_path / "fixture_ticket"
    d.mkdir()
    (d / "bets.json").write_text(json.dumps({
        "attempt": "A-0001",
        "bets": [{"ticker": "T1", "side": "yes", "limit_price": "0.4200",
                  "contracts": 1, "rationale": "r"}],
    }))
    (d / "edge_claim.md").write_text("## Markets\n## Why this is profitable\n")
    return d


def _stub_one_loop(monkeypatch, tmp_path, *, execute_ret="placed"):
    """Run the happy path with the real stub runner writing a ticket."""
    fixture = _write_fixture_ticket(tmp_path)
    monkeypatch.setenv("STUB_BEHAVIOR", "ok")
    monkeypatch.setenv("STUB_WRITE_TICKET", str(fixture))
    return _inject_intake(monkeypatch, _parsed(), execute_ret=execute_ret)


def _completed_attempt(ledger, slot: str) -> str:
    """A finished attempt with a ticket, so ``history.recent_completed`` returns it."""
    _seq, aid = ledger.create_attempt(
        env="prod", model="claude-opus-5", effort="high", memory_mode="on",
        prompt_version="p", toolkit_version="0.1.0", workspace_path="/ws",
        slot=slot, cell="static", cell_effective="static", era="live-v2",
    )
    ledger.transition(aid, "running")
    ledger.set_ticket_texts(aid, "## Markets\nm\n\n## Why this is profitable\ne", "", "")
    ledger.update_attempt_fields(aid, wall_seconds=60, cost_usd=D("1.00"))
    ledger.transition(aid, "no_bets")
    return aid


def _capture_spec(monkeypatch, *, exit_kind="ok", result_text="a closing paragraph",
                  write_ticket=True):
    """Replace ``run_session`` with a double that records the spec it was handed."""
    captured: dict = {}

    def fake_run_session(spec, runner_cmd=None, stream_path=None, err_path=None):
        captured["spec"] = spec
        if write_ticket:
            (spec.add_dirs[0] / "ticket" / "bets.json").write_text(
                '{"attempt":"A-0001","bets":[]}')
        return SessionResult(
            exit_kind=exit_kind, is_error=exit_kind != "ok", result_text=result_text,
            structured_output=None, session_id=spec.session_id, cost_usd=D("0.01"),
            input_tokens=10, output_tokens=5, num_turns=1, wall_seconds=1,
            init_manifest=None,
        )

    monkeypatch.setattr(attempt_mod, "run_session", fake_run_session)
    return captured


# ------------------------------------------------------------------ happy path (real stub)
def test_run_attempt_placed_full_pipeline(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _write_principles(tmp_path)
    fixture = _write_fixture_ticket(tmp_path)
    monkeypatch.setenv("STUB_BEHAVIOR", "ok")
    monkeypatch.setenv("STUB_WRITE_TICKET", str(fixture))
    parsed = _parsed()
    v, e = _inject_intake(monkeypatch, parsed)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")

    assert aid == "A-0001"
    a = ledger.get_attempt(aid)
    assert a["status"] == "placed"

    # Workspace layout (spec §9.2).
    adir = settings.attempts_dir / aid
    assert (adir / "workspace").is_dir()
    assert (adir / "ticket").is_dir()
    assert (adir / "logs").is_dir()
    assert (adir / "TASK.md").exists()
    assert (adir / "CONTEXT.md").exists()  # every cell but baseline
    assert (adir / "logs" / "session.stream.jsonl").exists()
    assert a["workspace_path"] == str(adir)

    # prompt_version = sha256(template bytes)[:12]
    tb = (files("betting_agent") / "prompts" / "attempt.md").read_bytes()
    assert a["prompt_version"] == hashlib.sha256(tb).hexdigest()[:12]
    assert a["toolkit_version"] == "0.1.0"

    # Intake wiring: parse → texts; validate → execute.
    assert a["edge_claim_md"] == parsed.edge_claim_md
    assert a["hypothesis_md"] == parsed.hypothesis_md
    v.validate_ticket.assert_called_once()
    assert v.validate_ticket.call_args.kwargs["market_fetch"] is client.get_market
    assert v.validate_ticket.call_args.kwargs["book_fetch"] is client.get_orderbook
    e.execute_attempt.assert_called_once()

    # Session result recorded + exactly the two attempt-owned audit events.
    assert a["session_exit"] == "ok"
    assert a["cost_usd"] == "0.0123"
    events = {row["event"] for row in ledger.audit_events(limit=50)}
    assert "attempt_launched" in events
    assert "session_end" in events


# ------------------------------------------------------------------ docs/22 the cell row
def test_a_static_run_fills_every_column_the_rebuild_added(env, tmp_path, monkeypatch):
    """docs/22 sections 4.1 and 5.2: the row says which cell ran, what it was shown, and
    what the session said on the way out."""
    settings, ledger, client = env
    page = _write_principles(tmp_path)
    _stub_one_loop(monkeypatch, tmp_path)
    _capture_spec(monkeypatch)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")

    a = ledger.get_attempt(aid)
    assert a["cell"] == "static"
    assert a["cell_effective"] == "static"
    assert a["cell_forced"] == 0
    assert a["era"] == settings.history.current_era == "live-v2"
    assert json.loads(a["example_ids"]) == []       # nothing completed yet
    assert a["direction_hash"] is None              # static carries no direction

    assert a["session_summary"] == "a closing paragraph"
    assert a["memory_mode"] == "on" and a["priors_mode"] == "on"
    assert a["loop_mode"] == "one"
    assert a["recipe_id"] is None and a["grader_blind"] is None

    context = (settings.attempts_dir / aid / "CONTEXT.md").read_text()
    assert a["context_pack_hash"] == hashlib.sha256(context.encode()).hexdigest()[:12]
    variant = json.loads(a["variant"])
    assert variant["principles_hash"] == hashlib.sha256(
        page.read_text().strip().encode()).hexdigest()[:12]


def test_example_ids_names_the_attempts_the_context_actually_holds(env, tmp_path,
                                                                   monkeypatch):
    """docs/22 section 4.1: ``example_ids`` is the list rendered into CONTEXT.md, in order.
    Empty is only right when the history is empty."""
    settings, ledger, client = env
    _write_principles(tmp_path)
    _stub_one_loop(monkeypatch, tmp_path)
    _capture_spec(monkeypatch)
    seeded = [_completed_attempt(ledger, f"slot:2026-07-0{i}/01:00") for i in range(1, 4)]

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")

    rendered = json.loads(ledger.get_attempt(aid)["example_ids"])
    assert rendered == list(reversed(seeded))      # newest first, the render order
    context = (settings.attempts_dir / aid / "CONTEXT.md").read_text()
    assert [line.split()[0] for line in context.splitlines()
            if line.startswith("A-")] == rendered


def test_a_render_that_raises_leaves_the_attempt_failed_not_created(env, tmp_path,
                                                                    monkeypatch):
    """A malformed stored page or an unreadable ledger used to strand the row in
    ``created``, which no reaper sweeps and no report counts. The render sits inside the
    wrapper now, so it ends the attempt the way a session failure does."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("director page is unreadable")

    monkeypatch.setattr(cells, "render", boom)

    with pytest.raises(RuntimeError, match="unreadable"):
        run_attempt(ledger, client, settings, now=_NOW, cell="director")

    a = ledger.get_attempt("A-0001")
    assert a["status"] == "failed"
    assert "RuntimeError: director page is unreadable" in a["error"]
    assert _audit_detail(ledger, "attempt_crashed")[0]["source"] == "run_attempt"
    # Nothing was launched, so nothing says it was.
    assert ledger.audit_events(event="attempt_launched") == []


def test_the_session_summary_is_searchable_as_a_closing_paragraph(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    _capture_spec(monkeypatch, result_text="I swept the strait ladders and passed.")

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")

    row = ledger.conn.execute(
        "SELECT content FROM ledger_fts WHERE attempt_id=? AND kind='closing'", (aid,)
    ).fetchone()
    assert row["content"] == "I swept the strait ladders and passed."


def test_baseline_writes_no_context_and_gates_bt_past_off(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _write_principles(tmp_path)
    _stub_one_loop(monkeypatch, tmp_path)
    captured = _capture_spec(monkeypatch)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert not (settings.attempts_dir / aid / "CONTEXT.md").exists()
    a = ledger.get_attempt(aid)
    assert a["cell"] == "baseline" and a["cell_effective"] == "baseline"
    assert a["context_pack_hash"] is None
    assert a["memory_mode"] == "off" and a["priors_mode"] == "off"
    assert json.loads(a["variant"] or "{}") == {}     # no principles page for baseline
    assert captured["spec"].extra_env["BT_PAST"] == "off"
    task = (settings.attempts_dir / aid / "TASK.md").read_text()
    assert "This attempt runs without access to past attempts" in task
    assert "# Principles to guide you" not in task
    assert ledger.audit_events(event="principles_missing") == []


@pytest.mark.parametrize("cell", ["static", "director", "focused"])
def test_every_other_cell_gates_bt_past_on(env, tmp_path, monkeypatch, cell):
    settings, ledger, client = env
    _write_principles(tmp_path)
    _stub_one_loop(monkeypatch, tmp_path)
    captured = _capture_spec(monkeypatch)

    run_attempt(ledger, client, settings, now=_NOW, cell=cell)

    assert captured["spec"].extra_env["BT_PAST"] == "on"


def test_a_forced_cell_is_marked_as_forced(env, tmp_path, monkeypatch):
    """An operator naming a cell with no slot bypassed the day's plan."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    _capture_spec(monkeypatch)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline",
                      cell_forced=True)

    assert ledger.get_attempt(aid)["cell_forced"] == 1


def test_a_slot_with_no_cell_takes_the_days_plan(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    _capture_spec(monkeypatch)
    slot_key = f"slot:2026-07-07/{settings.schedule.slots[0]}"
    expected = cells.cell_for_slot(ledger, settings, slot_key)

    aid = run_attempt(ledger, client, settings, now=_NOW, slot=slot_key)

    a = ledger.get_attempt(aid)
    assert a["cell"] == expected
    assert a["cell_forced"] == 0


def test_no_slot_and_no_cell_runs_static(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    _capture_spec(monkeypatch)
    aid = run_attempt(ledger, client, settings, now=_NOW)
    assert ledger.get_attempt(aid)["cell"] == "static"


def test_an_unknown_cell_is_rejected(env):
    settings, ledger, client = env
    with pytest.raises(ValueError, match="cell must be one of"):
        run_attempt(ledger, client, settings, now=_NOW, cell="sometimes")


def test_the_attempt_launched_audit_names_the_cell(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _write_principles(tmp_path)
    _stub_one_loop(monkeypatch, tmp_path)
    _capture_spec(monkeypatch)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="director")

    detail = json.loads(ledger.audit_events(event="attempt_launched")[0]["detail"])
    assert set(detail) == {
        "slot", "model", "effort", "cell", "cell_effective", "session_id", "example_ids",
        "direction_hash", "model_substitutions",
    }
    # No valid director run exists, so the slot ran as static and says so.
    assert detail["cell"] == "director"
    assert detail["cell_effective"] == "static"
    assert ledger.get_attempt(aid)["cell_effective"] == "static"


# ------------------------------------------------------------------ the arm (2026-09-26)
def _ran(captured, ledger, aid) -> dict:
    """The arm as the session, the row and the launch audit each carry it."""
    row = ledger.get_attempt(aid)
    launched = [json.loads(e["detail"]) for e in ledger.audit_events(event="attempt_launched")
                if e["attempt_id"] == aid][0]
    return {
        "session": (captured["spec"].model, captured["spec"].effort),
        "row": (row["model"], row["effort"]),
        "audit": (launched["model"], launched["effort"]),
    }


@pytest.mark.parametrize("on_fable", [True, False])
def test_a_slot_runs_the_arm_its_days_plan_drew(env, tmp_path, monkeypatch, on_fable):
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    captured = _capture_spec(monkeypatch)
    plan = cells.plan(ledger, settings, "2026-07-07")
    index = next(i for i, e in enumerate(plan)
                 if (e["model"] == settings.attempt.fable_model) == on_fable)
    arm = (plan[index]["model"], plan[index]["effort"])

    aid = run_attempt(ledger, client, settings, now=_NOW,
                      slot=f"slot:2026-07-07/{settings.schedule.slots[index]}")

    assert _ran(captured, ledger, aid) == {"session": arm, "row": arm, "audit": arm}


def test_an_override_beats_the_plan_one_half_of_the_arm_at_a_time(env, tmp_path,
                                                                 monkeypatch):
    """``--model`` and ``--effort`` each replace their own half of the slot's arm."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    captured = _capture_spec(monkeypatch)
    slot_key = f"slot:2026-07-07/{settings.schedule.slots[0]}"
    planned_model, _planned_effort = cells.arm_for_slot(ledger, settings, slot_key)

    both = run_attempt(ledger, client, settings, now=_NOW, slot=slot_key,
                       model="claude-sonnet-5", effort="low")
    arm = ("claude-sonnet-5", "low")
    assert _ran(captured, ledger, both) == {"session": arm, "row": arm, "audit": arm}

    effort_only = run_attempt(ledger, client, settings, now=_NOW, slot=slot_key,
                              effort="medium")
    arm = (planned_model, "medium")
    assert _ran(captured, ledger, effort_only) == {"session": arm, "row": arm, "audit": arm}


def test_a_manual_attempt_with_no_slot_runs_the_configured_model_and_effort(
    env, tmp_path, monkeypatch
):
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    captured = _capture_spec(monkeypatch)
    settings.attempt.effort = "medium"      # not one of the plan's levels, so unmistakable

    aid = run_attempt(ledger, client, settings, now=_NOW)

    arm = (settings.attempt.model, "medium")
    assert _ran(captured, ledger, aid) == {"session": arm, "row": arm, "audit": arm}


def test_a_missing_principles_page_audits_and_still_runs(env, tmp_path, monkeypatch):
    settings, ledger, client = env      # no docs/principles.md written
    _stub_one_loop(monkeypatch, tmp_path)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")

    assert ledger.get_attempt(aid)["status"] == "placed"   # the attempt is unharmed
    task = (settings.attempts_dir / aid / "TASK.md").read_text()
    assert "# Principles to guide you" not in task
    events = ledger.audit_events(event="principles_missing")
    assert len(events) == 1 and events[0]["attempt_id"] == aid
    assert json.loads(events[0]["detail"])["cell"] == "static"
    assert json.loads(ledger.get_attempt(aid)["variant"] or "{}") == {}


def test_the_principles_hash_merges_into_an_operator_variant(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _write_principles(tmp_path)
    _stub_one_loop(monkeypatch, tmp_path)
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static",
                      variant='{"study":"hand","n":2}')
    variant = json.loads(ledger.get_attempt(aid)["variant"])
    assert variant["study"] == "hand" and variant["n"] == 2   # operator keys preserved
    assert len(variant["principles_hash"]) == 12


# ------------------------------------------------------------------ the prompt
def test_task_md_substituted_but_keeps_literal_json_braces(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _write_principles(tmp_path)
    _stub_one_loop(monkeypatch, tmp_path)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")
    task = (settings.attempts_dir / aid / "TASK.md").read_text()

    # Placeholders substituted.
    assert "# Attempt A-0001" in task
    assert "Kalshi (prod)" in task
    assert "within 120 hours" in task
    # The wall clock is a silent safeguard (decision 2026-07-29): no deadline,
    # no budget scarcity language in the rendered prompt.
    assert "deadline" not in task.lower()
    assert "budget" not in task.lower()
    # The static cell's line and the principles page both rendered.
    assert "Study ../CONTEXT.md, which holds the ten most recent attempts" in task
    assert "# Principles to guide you" in task
    # No placeholder token survives.
    for token in ["{attempt_id}", "{env}", "{window_hours}", "{memory_section}",
                  "{priors_section}"]:
        assert token not in task
    # Literal JSON example braces DO survive (this is why str.format is forbidden).
    assert '{"ticker":' in task
    assert '"attempt": "A-0001"' in task  # {attempt_id} substituted inside the JSON example


def test_exactly_five_substitutions_reach_the_renderer(env, tmp_path, monkeypatch):
    """docs/22 section 5.2 step 6: ``max_bets``, ``min_edge``, ``memory_mode`` and
    ``k_candidates`` left the template and must leave the subs dict with it."""
    settings, ledger, client = env
    _write_principles(tmp_path)
    _stub_one_loop(monkeypatch, tmp_path)
    seen: dict = {}
    real = attempt_mod._render_task

    def spy(template, subs):
        seen["subs"] = dict(subs)
        return real(template, subs)

    monkeypatch.setattr(attempt_mod, "_render_task", spy)
    run_attempt(ledger, client, settings, now=_NOW, cell="static")

    assert set(seen["subs"]) == {
        "attempt_id", "env", "window_hours", "memory_section", "priors_section"
    }


# ------------------------------------------------------------------ contract rule
def test_run_attempt_contract_rule_ticket_survives_timeout(env, tmp_path, monkeypatch):
    """exit_kind=timeout but a parseable ticket exists -> intake still runs (spec §9.4)."""
    settings, ledger, client = env
    parsed = _parsed()
    _v, e = _inject_intake(monkeypatch, parsed)
    _capture_spec(monkeypatch, exit_kind="timeout", result_text=None)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")
    assert ledger.get_attempt(aid)["status"] == "placed"
    e.execute_attempt.assert_called_once()  # contract rule honored despite the timeout


def test_run_attempt_session_disallows_skill(env, tmp_path, monkeypatch):
    """The attempt session must ban the Skill tool so user-global skills (e.g. a
    `deep-research` skill) can't leak past --safe-mode and drive a cost cascade; the Task
    tool (legitimate nested agents) is untouched (F1 / docs/decisions.md pilot audit)."""
    settings, ledger, client = env
    _inject_intake(monkeypatch, _parsed())
    captured = _capture_spec(monkeypatch)

    run_attempt(ledger, client, settings, now=_NOW, cell="baseline")
    spec = captured["spec"]
    assert spec.kind == "attempt"
    assert spec.disallowed_tools == "Skill"
    assert spec.tools is None  # nothing else about the tool surface is constrained
    assert spec.max_turns == settings.attempt.max_turns
    assert spec.wall_time_s == settings.attempt.wall_time_min * 60
    assert spec.max_budget_usd == settings.attempt.max_budget_usd
    assert spec.extra_env["BT_ATTEMPT_ID"] == "A-0001"
    assert spec.extra_env["BT_ROOT"] == str(settings.root)


def test_run_attempt_no_bets_transition(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path, execute_ret="no_bets")
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")
    assert ledger.get_attempt(aid)["status"] == "no_bets"


# ------------------------------------------------------------------ whole-ticket invalid
def test_whole_ticket_error_is_ticket_invalid(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    fixture = _write_fixture_ticket(tmp_path)
    monkeypatch.setenv("STUB_BEHAVIOR", "ok")
    monkeypatch.setenv("STUB_WRITE_TICKET", str(fixture))
    # parse succeeds enough to store the texts, but the ticket is not ok (e.g. V02).
    _v, e = _inject_intake(monkeypatch, _parsed(ok=False))
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")
    a = ledger.get_attempt(aid)
    assert a["status"] == "ticket_invalid"
    assert a["edge_claim_md"] == "## Markets\nNYC weather"  # texts still stored
    e.execute_attempt.assert_not_called()  # never reaches execution


# ------------------------------------------------------------------ no ticket
def test_no_ticket_clean_exit_is_ticket_invalid(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    monkeypatch.setenv("STUB_BEHAVIOR", "ok")  # no STUB_WRITE_TICKET -> no ticket
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")
    a = ledger.get_attempt(aid)
    assert a["status"] == "ticket_invalid"
    assert not (settings.attempts_dir / aid / "CONTEXT.md").exists()


def test_crash_without_ticket_is_failed(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    monkeypatch.setenv("STUB_BEHAVIOR", "error")  # exit 1, no ticket
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")
    a = ledger.get_attempt(aid)
    assert a["status"] == "failed"
    assert a["session_exit"] == "error"


def test_intake_exception_records_error_and_fails(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _v, e = _stub_one_loop(monkeypatch, tmp_path)
    e.execute_attempt.side_effect = RuntimeError("boom")
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")
    a = ledger.get_attempt(aid)
    assert a["status"] == "failed"
    assert "RuntimeError: boom" in a["error"]


# ------------------------------------------------------------------ L18 sessions rows
def test_one_run_writes_exactly_one_attempt_session_row(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")

    rows = ledger.sessions_for_attempt(aid)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "attempt"
    assert row["model"] == settings.attempt.model
    assert row["exit"] == "ok"
    assert row["cost_usd"] == "0.0123" and row["num_turns"] == 3
    assert row["input_tokens"] == 100 and row["output_tokens"] == 50
    assert row["started_at"] is not None and row["ended_at"] is not None
    assert row["error"] is None


def test_session_row_records_error_text_on_failure(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    monkeypatch.setenv("STUB_BEHAVIOR", "error")
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")
    row = ledger.sessions_for_attempt(aid)[0]
    assert row["exit"] == "error"
    assert row["error"] == "stub error done"


def test_session_row_failure_never_kills_the_attempt(env, tmp_path, monkeypatch):
    """A sessions-row write failure is audited, not fatal (L18)."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    monkeypatch.setattr(
        ledger, "insert_session",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sessions table gone")),
    )
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")
    assert ledger.get_attempt(aid)["status"] == "placed"
    events = ledger.audit_events(event="session_row_error")
    # Both halves audit: the insert raised, so the finish has no row to update.
    steps = {json.loads(e["detail"])["step"] for e in events}
    assert steps == {"insert", "finish"}
    assert any("sessions table gone" in e["detail"] for e in events)
    assert ledger.sessions_for_attempt(aid) == []


# ------------------------------------------------------------------ docs/22 section 12
def test_session_exit_is_the_exit_kind_and_terminal_reason_rides_beside_it(
    env, tmp_path, monkeypatch
):
    """docs/22 section 12: the kill-versus-timeout ambiguity is recorded, never folded
    into ``session_exit``."""
    settings, ledger, client = env
    _inject_intake(monkeypatch, _parsed())

    def fake_run_session(spec, runner_cmd=None, stream_path=None, err_path=None):
        (spec.add_dirs[0] / "ticket" / "bets.json").write_text(
            '{"attempt":"A-0001","bets":[]}')
        return SessionResult(
            exit_kind="killed", is_error=True, result_text="stopped", structured_output=None,
            session_id=spec.session_id, cost_usd=D("0.01"), input_tokens=1, output_tokens=1,
            num_turns=1, wall_seconds=1, init_manifest=None,
            terminal_reason="max_turns_exceeded",
        )

    monkeypatch.setattr(attempt_mod, "run_session", fake_run_session)
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    a = ledger.get_attempt(aid)
    assert a["session_exit"] == "killed"
    assert json.loads(a["variant"])["terminal_reason"] == "max_turns_exceeded"
    detail = json.loads(ledger.audit_events(event="session_end")[0]["detail"])
    assert detail["exit_kind"] == "killed"
    assert detail["terminal_reason"] == "max_turns_exceeded"


# ------------------------------------------------------------------ D9 render-time clock
def test_task_md_carries_the_render_time_clock(env, tmp_path, monkeypatch):
    """docs/14 D9 / docs/12 §9.11: two A-0055 Fable sessions fabricated timestamps in
    their artifacts for lack of any real clock in the rendered prompt. Fake-clock
    testable, not wall-clock flaky."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    render_time = parse_iso("2026-08-10T07:00:00Z")
    monkeypatch.setattr(attempt_mod, "utc_now", lambda: render_time)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")

    adir = settings.attempts_dir / aid
    assert (adir / "TASK.md").read_text().startswith(f"Current time: {iso(render_time)}\n\n")


def test_context_md_is_the_cells_text_and_nothing_else(env, tmp_path, monkeypatch):
    """docs/22 section 8.5: nothing else goes into CONTEXT.md, clock line included. The
    hash is of exactly what was written, so two attempts that saw the same history hash
    the same whenever they ran."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    monkeypatch.setattr(attempt_mod, "utc_now",
                        lambda: parse_iso("2026-08-10T07:00:00Z"))
    # What the cell would render right now, before this attempt joins the history.
    expected, *_ = cells.render(ledger, settings, "static")

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")

    context = (settings.attempts_dir / aid / "CONTEXT.md").read_text()
    assert not context.startswith("Current time:")
    assert context.startswith("## Past attempts (the ten most recent)")
    assert context == expected


# ------------------------------------------------------------------ D9 sleep-stretch flag
def _run_session_returning(wall_seconds: int):
    """A ``run_session`` double with a specific monotonic ``wall_seconds`` — the sleep-
    stretch check needs that controlled independently of the real clock reads."""
    def fake_run_session(spec, runner_cmd=None, stream_path=None, err_path=None):
        return SessionResult(
            exit_kind="ok", is_error=False, result_text="done", structured_output=None,
            session_id=spec.session_id, cost_usd=D("0.01"), input_tokens=10,
            output_tokens=5, num_turns=1, wall_seconds=wall_seconds, init_manifest=None,
        )
    return fake_run_session


def _sleep_ticks(monkeypatch, excess_s: int, wall_seconds: int = 1500):
    """The three ``utc_now`` reads a run makes: the TASK.md stamp, started, ended."""
    ticks = [_NOW, _NOW, _NOW + timedelta(seconds=wall_seconds + excess_s)]
    monkeypatch.setattr(attempt_mod, "utc_now", lambda: ticks.pop(0))


def test_sleep_stretched_set_when_real_span_exceeds_by_over_ten_minutes(
    env, tmp_path, monkeypatch
):
    """docs/14 D9, the A-0053 shape: a session's real wall-clock span materially longer
    than its monotonic wall_seconds means the host almost certainly slept mid-session."""
    settings, ledger, client = env
    _inject_intake(monkeypatch, _parsed())
    monkeypatch.setattr(attempt_mod, "run_session", _run_session_returning(1500))
    _sleep_ticks(monkeypatch, 601)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert ledger.get_attempt(aid)["sleep_stretched"] == 1


def test_sleep_stretched_not_set_at_exactly_ten_minutes_over(env, tmp_path, monkeypatch):
    """The bound is strict '>', not '>=': exactly ten minutes of excess must not flag."""
    settings, ledger, client = env
    _inject_intake(monkeypatch, _parsed())
    monkeypatch.setattr(attempt_mod, "run_session", _run_session_returning(1500))
    _sleep_ticks(monkeypatch, 600)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert ledger.get_attempt(aid)["sleep_stretched"] is None


def test_sleep_stretched_not_set_at_nine_minutes_over(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _inject_intake(monkeypatch, _parsed())
    monkeypatch.setattr(attempt_mod, "run_session", _run_session_returning(1500))
    _sleep_ticks(monkeypatch, 540)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert ledger.get_attempt(aid)["sleep_stretched"] is None


def test_sleep_stretched_stays_unset_for_an_ordinary_session(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")
    assert ledger.get_attempt(aid)["sleep_stretched"] is None


# ------------------------------------------------------------------ PC-2 ticket_invalid audit
def test_ticket_invalid_audit_carries_the_reason(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    fixture = _write_fixture_ticket(tmp_path)
    monkeypatch.setenv("STUB_BEHAVIOR", "ok")
    monkeypatch.setenv("STUB_WRITE_TICKET", str(fixture))
    _inject_intake(monkeypatch, _parsed(
        ok=False, whole_ticket_errors=["V01"], error_detail=["bets[0] bad limit_price"]))

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert ledger.get_attempt(aid)["status"] == "ticket_invalid"
    events = ledger.audit_events(event="ticket_invalid")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["codes"] == ["V01"]
    assert detail["detail"] == ["bets[0] bad limit_price"]


def test_ticket_invalid_audit_when_no_ticket_was_written(env, tmp_path, monkeypatch):
    settings, ledger, client = env
    monkeypatch.setenv("STUB_BEHAVIOR", "ok")          # clean exit, no ticket
    _inject_intake(monkeypatch, _parsed())

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert ledger.get_attempt(aid)["status"] == "ticket_invalid"
    detail = json.loads(ledger.audit_events(event="ticket_invalid")[0]["detail"])
    assert detail["codes"] == ["V01"]
    assert "missing or does not parse" in detail["detail"][0]


def test_crashed_session_without_a_ticket_is_not_a_contract_failure(env, tmp_path,
                                                                    monkeypatch):
    """`failed` is a crash, not a format failure — it must not land in the D3 telemetry."""
    settings, ledger, client = env
    monkeypatch.setenv("STUB_BEHAVIOR", "error")       # exit 1, no ticket
    _inject_intake(monkeypatch, _parsed())

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert ledger.get_attempt(aid)["status"] == "failed"
    assert ledger.audit_events(event="ticket_invalid") == []


# ------------------------------------------------------------------ CI-2 hermeticity gate
def test_not_hermetic_with_a_parseable_ticket_fails_the_attempt(env, tmp_path, monkeypatch):
    """CI-2: the exit kind outranks the contract rule — the reproduction, inverted.

    ``bad_init`` leaks a user CLAUDE.md into the session's init line *and* still writes a
    complete, parseable ticket. Before the fix the exit kind was consulted only when the
    ticket was missing, so this attempt placed real bets from a session that had escaped
    its experimental envelope.
    """
    settings, ledger, client = env
    fixture = _write_fixture_ticket(tmp_path)
    monkeypatch.setenv("STUB_BEHAVIOR", "bad_init")
    monkeypatch.setenv("STUB_WRITE_TICKET", str(fixture))
    v, e = _inject_intake(monkeypatch, _parsed())

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    a = ledger.get_attempt(aid)
    assert a["status"] == "failed"
    assert a["session_exit"] == "env_not_hermetic"
    assert "env_not_hermetic" in a["error"]
    # The ticket really was there and really did parse — this is not the no-ticket path.
    assert (settings.attempts_dir / aid / "ticket" / "bets.json").exists()
    # Nothing downstream of the gate ran: no parse, no validation, and above all no orders.
    v.parse_ticket.assert_not_called()
    v.validate_ticket.assert_not_called()
    e.execute_attempt.assert_not_called()

    detail = json.loads(ledger.audit_events(event="env_not_hermetic")[0]["detail"])
    assert detail["phase"] == "attempt"
    assert detail["exit_kind"] == "env_not_hermetic"
    # It is a hermeticity failure, not a malformed ticket.
    assert ledger.audit_events(event="ticket_invalid") == []


# ------------------------------------------------------------------ AE-2/AE-8 crash paths
def _audit_detail(ledger, event):
    rows = ledger.audit_events(event=event)
    return [json.loads(r["detail"]) for r in rows]


def test_a_harness_crash_after_launch_fails_the_attempt(env, tmp_path, monkeypatch):
    """AE-2/AE-8: there was NO path out of 'running' when the harness itself died. A
    Popen that raises, a ledger error, a Ctrl-C — the row sat in 'running' forever:
    never settled, never counted, invisible to every population keyed on a terminal
    status. The exception still propagates; the record is terminal before it does."""
    settings, ledger, client = env

    def boom(*a, **k):
        raise OSError("fork failed: too many open files")

    monkeypatch.setattr(attempt_mod, "run_session", boom)

    with pytest.raises(OSError, match="fork failed"):
        run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    a = ledger.get_attempt("A-0001")
    assert a["status"] == "failed"
    assert "OSError: fork failed" in a["error"]
    detail = _audit_detail(ledger, "attempt_crashed")
    assert len(detail) == 1 and detail[0]["source"] == "run_attempt"


def test_a_crash_between_the_session_and_intake_fails_the_attempt(env, tmp_path, monkeypatch):
    """The window the review named: the session is paid for, then the bookkeeping dies."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(attempt_mod, "_intake", boom)

    with pytest.raises(RuntimeError, match="database is locked"):
        run_attempt(ledger, client, settings, now=_NOW, cell="static")

    assert ledger.get_attempt("A-0001")["status"] == "failed"


def test_a_keyboard_interrupt_mid_attempt_still_fails_the_attempt(env, tmp_path, monkeypatch):
    """An operator's Ctrl-C is one of the two ways this process dies mid-attempt, and it
    is the one we can still record (SIGKILL is the reaper's job)."""
    settings, ledger, client = env

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(attempt_mod, "run_session", interrupted)

    with pytest.raises(KeyboardInterrupt):
        run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert ledger.get_attempt("A-0001")["status"] == "failed"


def test_an_intake_ledger_error_before_validation_fails_the_attempt(env, tmp_path, monkeypatch):
    """The pre-try writes (parse, ticket texts) used to sit OUTSIDE _intake's try,
    precisely the writes a locked or full database fails on."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    real = ledger.set_ticket_texts

    def boom(*a, **k):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(ledger, "set_ticket_texts", boom)

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")   # no propagation

    monkeypatch.setattr(ledger, "set_ticket_texts", real)
    a = ledger.get_attempt(aid)
    assert a["status"] == "failed"
    assert "RuntimeError: disk I/O error" in a["error"]
    assert _audit_detail(ledger, "attempt_crashed")[0]["source"] == "intake"


def test_a_crash_after_a_terminal_status_does_not_rewrite_it(env, tmp_path, monkeypatch):
    """A 'placed' attempt whose later bookkeeping blew up is placed, not failed. The
    strict state machine is not softened anywhere in this package."""
    settings, ledger, client = env
    _stub_one_loop(monkeypatch, tmp_path)
    aid = run_attempt(ledger, client, settings, now=_NOW, cell="static")
    assert ledger.get_attempt(aid)["status"] == "placed"

    assert attempt_mod._fail_running(
        ledger, aid, RuntimeError("late failure"), source="test"
    ) is False
    assert ledger.get_attempt(aid)["status"] == "placed"


def test_fail_running_never_raises_on_an_unknown_attempt(env):
    settings, ledger, client = env
    assert attempt_mod._fail_running(
        ledger, "A-9999", RuntimeError("x"), source="test"
    ) is False


# ------------------------------------------------------------------ D2: infra vs contract
def test_auth_death_is_an_infra_failure_not_a_bad_ticket(env, tmp_path, monkeypatch):
    """The real subprocess path: the stub emits the Aug-5 wedge's stream and the attempt
    row says why it died instead of just 'failed' with an empty error column."""
    settings, ledger, client = env
    monkeypatch.setenv("STUB_BEHAVIOR", "auth_error")   # no ticket, no cost, exit 1

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    a = ledger.get_attempt(aid)
    assert a["status"] == "failed"
    assert a["error"] == "session_infra_auth (attempt)"
    assert ledger.audit_events(event="ticket_invalid") == []   # NOT a contract failure
    detail = _audit_detail(ledger, "phase_infra_error")
    assert len(detail) == 1
    assert detail[0]["phase"] == "attempt" and detail[0]["kind"] == "auth"
    assert detail[0]["error_code"] == "authentication_failed"


def test_a_session_that_ran_and_wrote_no_ticket_is_not_reclassified(env, tmp_path, monkeypatch):
    """Regression: the stub's plain ``error`` behavior bills $0.0123 and carries no
    machine signal — a session that ran. It stays exactly as it was recorded before D2."""
    settings, ledger, client = env
    monkeypatch.setenv("STUB_BEHAVIOR", "error")

    aid = run_attempt(ledger, client, settings, now=_NOW, cell="baseline")

    assert ledger.get_attempt(aid)["status"] == "failed"
    assert ledger.audit_events(event="phase_infra_error") == []
