"""The daily cell plan, its arms, and the four renderings (docs/22 sections 8.5 and 8.7).

Two things are worth pinning here. The plan is a permutation of the configured counts,
stored once a day and reproducible from its own seed, because a cell that quietly drifted
toward a time of day would put the whole comparison back where the arms were. The same
holds for each slot's arm, a model and an effort drawn beside its cell. And the four
renderings are exactly the sections section 8.5 lists and nothing else, because CONTEXT.md
is the only thing that differs between the cells and any extra line in it is an
uncontrolled variable.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal as D

import pytest

from betting_agent.config import load_settings
from betting_agent.harness import cells
from betting_agent.ledger import history
from betting_agent.ledger.db import Ledger

CLAIM = """## Markets
KXHORMUZWEEKLY-25SEP05-T3 and its ladder siblings.

## Why this is profitable
The strait has stayed open through three escalations and the ladder still prices a
closure at six in ten.

## Why the opportunity exists and persists
Retail reads the headline; the shipping trackers are paywalled.
"""

HYPOTHESIS = """## If we're right
The ladder settles no and both legs pay.

## If we're wrong
A closure is announced and the ladder gaps.

## Kill criteria
Any Lloyd's list closure notice.
"""

PAGE = """# Director run 2026-09-20

## Standing direction
Keep sweeping the strait ladders and leave the weather families alone.

## Today
Yesterday's three refusals were all the per-market cap. Size smaller or bet elsewhere.

## Watching
Whether the closure ladder reprices after Friday's convoy.
"""


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "principles.md").write_text(
        "# Principles to guide you\n\n1. Be ambitious.\n", encoding="utf-8"
    )
    return load_settings(root=tmp_path)


@pytest.fixture
def lg(settings):
    ledger = Ledger.open(settings.ledger_path)
    ledger.migrate()
    yield ledger
    ledger.close()


def _cells(plan: list[dict]) -> list[str]:
    return [entry["cell"] for entry in plan]


def _attempt(lg, *, slot, status="no_bets", cell="static", claim=CLAIM):
    _seq, aid = lg.create_attempt(
        env="prod", model="claude-opus-5", effort="high", memory_mode="on",
        prompt_version="p", toolkit_version="0.1.0", workspace_path="/ws",
        slot=slot, cell=cell, cell_effective=cell, era="live-v2",
    )
    lg.transition(aid, "running")
    lg.set_ticket_texts(aid, claim, HYPOTHESIS, "")
    lg.update_attempt_fields(aid, wall_seconds=1860, cost_usd=D("8.30"))
    lg.transition(aid, status)
    return aid


def _seed_attempts(lg, n=12):
    return [_attempt(lg, slot=f"slot:2026-09-{10 + i:02d}/01:00") for i in range(n)]


def _director_run(lg, sets, *, run_id="D-2026-09-20", status="valid", page=PAGE):
    lg.conn.execute(
        "INSERT INTO director_runs (run_id, run_date, cohort_date, started_at, model, "
        "status, page_md, page_hash, sets_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "2026-09-20", "2026-09-19", "2026-09-20T04:00:00Z", "claude-fable-5",
         status, page, "abc123abc123", json.dumps(sets)),
    )
    lg.conn.commit()


# --------------------------------------------------------------------------- the plan
def test_the_plan_is_a_permutation_of_the_configured_multiset(lg, settings):
    """Nine cells over nine slots: every cell runs exactly its configured number of times,
    so the baseline and the focused cell are daily and the plan is a permutation, not a
    sample."""
    plan = _cells(cells.plan(lg, settings, "2026-09-20"))
    assert len(plan) == len(settings.schedule.slots) == 9
    counts = settings.cells.counts()
    assert counts == {"baseline": 1, "static": 3, "director": 4, "focused": 1}
    assert sorted(plan) == sorted(
        name for name, n in counts.items() for _ in range(n)
    )
    for name in cells.CELLS:
        assert plan.count(name) == counts[name]


def test_the_plan_is_stored_once_and_read_back_thereafter(lg, settings):
    first = cells.plan(lg, settings, "2026-09-20")
    row = lg.cell_plan("2026-09-20")
    assert json.loads(row["plan"]) == first
    # Called again, it returns the stored list rather than drawing a second one.
    for _ in range(5):
        assert cells.plan(lg, settings, "2026-09-20") == first
    assert row["created_at"] == lg.cell_plan("2026-09-20")["created_at"]


def test_the_plan_is_deterministic_under_its_stored_seed(lg, settings):
    plan = cells.plan(lg, settings, "2026-09-20")
    seed = lg.cell_plan("2026-09-20")["seed"]
    assert cells.draw(settings, seed) == plan


def test_the_plan_differs_across_days(lg, settings):
    """The cells and the arms both: the Fable slots land in different places on
    different days, so no arm is welded to a time of day either."""
    days = [cells.plan(lg, settings, f"2026-09-{d:02d}") for d in range(10, 25)]
    assert len({tuple(_cells(p)) for p in days}) > 1
    fable = settings.attempt.fable_model
    assert len({tuple(i for i, e in enumerate(p) if e["model"] == fable) for p in days}) > 1


def test_a_misconfigured_day_still_plans_what_it_has(lg, settings):
    """``config_warnings`` is what names a count that does not fill the day; the draw does
    not crash on one, it just returns the cells it was given."""
    settings.cells.static = 0
    settings.cells.director = 0
    settings.cells.focused = 0
    assert _cells(cells.plan(lg, settings, "2026-09-20")) == ["baseline"]


def test_a_surplus_of_cells_is_cut_to_the_day(lg, settings):
    """The other half of the same misconfiguration: the plan never names more cells than
    the day has slots, whatever the counts say."""
    settings.cells.static = 40
    assert len(cells.plan(lg, settings, "2026-09-20")) == len(settings.schedule.slots)


# --------------------------------------------------------------------------- the arms
def test_each_day_runs_exactly_the_configured_fable_slots(settings):
    """Two slots a day on Fable at its own effort, every other slot on ``attempt.model``."""
    attempt = settings.attempt
    for seed in range(40):
        plan = cells.draw(settings, seed)
        fable = [e for e in plan if e["model"] == attempt.fable_model]
        assert len(fable) == attempt.fable_per_day == 2
        assert {e["effort"] for e in fable} == {attempt.fable_effort}
        others = [e for e in plan if e["model"] != attempt.fable_model]
        assert len(others) == len(plan) - 2
        assert {e["model"] for e in others} == {attempt.model}


@pytest.mark.parametrize("n_slots, split", [(9, [3, 4]), (15, [6, 7])])
def test_the_other_slots_split_the_listed_efforts_as_evenly_as_the_count_allows(
    settings, n_slots, split
):
    """Nine slots leave seven for ``attempt.model`` (four and three) and fifteen leave
    thirteen (seven and six). Which level gets the extra slot is the draw's choice, so
    across days each level gets it."""
    settings.schedule.slots = [f"{h:02d}:00" for h in range(n_slots)]
    settings.cells.static = n_slots - 6
    got_the_extra = Counter()
    for seed in range(40):
        plan = cells.draw(settings, seed)
        efforts = Counter(e["effort"] for e in plan if e["model"] == settings.attempt.model)
        assert sorted(efforts.values()) == split
        got_the_extra[efforts.most_common(1)[0][0]] += 1
    assert set(got_the_extra) == {"high", "max"}


def test_a_one_level_list_gives_every_other_slot_that_level(settings):
    settings.attempt.efforts = ["xhigh"]
    plan = cells.draw(settings, 7)
    assert {e["effort"] for e in plan if e["model"] == settings.attempt.model} == {"xhigh"}


def test_a_plan_stored_before_arms_reads_as_the_configured_model_and_effort(lg, settings):
    """Plans stored before 2026-09-26 are bare cell names. Every slot of those days ran
    ``attempt.model`` at ``attempt.effort``, so that is what they read as."""
    legacy = ["baseline", "static", "static", "static", "director", "director",
              "director", "director", "focused"]
    lg.set_cell_plan("2026-09-26", 12345, json.dumps(legacy))
    arm = (settings.attempt.model, settings.attempt.effort)

    assert cells.plan(lg, settings, "2026-09-26") == [
        {"cell": cell, "model": arm[0], "effort": arm[1]} for cell in legacy
    ]
    first = f"slot:2026-09-26/{settings.schedule.slots[0]}"
    assert cells.cell_for_slot(lg, settings, first) == "baseline"
    assert cells.arm_for_slot(lg, settings, first) == arm


# --------------------------------------------------------------------------- the slot
def test_the_slot_takes_the_cell_and_the_arm_at_its_index(lg, settings):
    plan = cells.plan(lg, settings, "2026-09-20")
    for index, hhmm in enumerate(settings.schedule.slots):
        slot_key = f"slot:2026-09-20/{hhmm}"
        assert cells.cell_for_slot(lg, settings, slot_key) == plan[index]["cell"]
        assert cells.arm_for_slot(lg, settings, slot_key) == (
            plan[index]["model"], plan[index]["effort"])


def test_a_slot_key_the_schedule_does_not_name_runs_static(lg, settings):
    assert cells.cell_for_slot(lg, settings, "slot:2026-09-20/04:07") == "static"
    assert cells.cell_for_slot(lg, settings, "garbage") == "static"
    assert cells.cell_for_slot(lg, settings, None) == "static"


def test_a_slot_the_plan_cannot_place_runs_the_configured_arm(lg, settings):
    """The same slots as above, and a manual attempt with no slot at all."""
    arm = (settings.attempt.model, settings.attempt.effort)
    assert cells.arm_for_slot(lg, settings, "slot:2026-09-20/04:07") == arm
    assert cells.arm_for_slot(lg, settings, "garbage") == arm
    assert cells.arm_for_slot(lg, settings, None) == arm


def test_looking_a_slot_up_draws_the_day_if_the_tick_has_not(lg, settings):
    assert lg.cell_plan("2026-09-21") is None
    cells.cell_for_slot(lg, settings, "slot:2026-09-21/01:00")
    assert lg.cell_plan("2026-09-21") is not None


# --------------------------------------------------------------------------- the renderings
def test_baseline_sees_nothing_at_all(lg, settings):
    _seed_attempts(lg)
    context, memory, principles, ids, direction, effective = cells.render(
        lg, settings, "baseline")
    assert context is None
    assert principles == ""
    assert ids == []
    assert direction is None
    assert effective == "baseline"
    assert memory.strip() == (
        "This attempt runs without access to past attempts or guidance. Work from the "
        "markets alone."
    )


def test_static_holds_the_configured_number_of_recent_records(lg, settings):
    seeded = _seed_attempts(lg, 12)
    context, memory, principles, ids, direction, effective = cells.render(
        lg, settings, "static")
    assert effective == "static"
    assert direction is None
    assert principles.strip().startswith("# Principles to guide you")
    assert memory.strip().startswith("Study ../CONTEXT.md, which holds the ten most recent")
    assert ids == history.recent_completed(lg, limit=settings.cells.static_recent)
    assert len(ids) == settings.cells.static_recent == 10
    assert ids[0] == seeded[-1]              # newest first
    assert context.startswith("## Past attempts (the ten most recent)\n")
    assert "## Direction" not in context
    for aid in ids:
        assert aid in context


def test_the_static_set_waits_for_an_attempt_to_settle(lg, settings):
    """Arno's choice of 2026-09-21: an attempt is not an example until its bets have
    outcomes, and the days it spends waiting for settle are the accepted price."""
    _seed_attempts(lg, 10)
    fresh = _attempt(lg, slot="slot:2026-09-21/01:00", status="placed")
    lg.insert_bet(bet_id=f"{fresh}-B01", attempt_id=fresh, ticket_index=1,
                  ticker="KXHORMUZWEEKLY-25SEP26-T3", category="Politics", side="no",
                  limit_price=D("0.55"), rationale="r", is_real=1, status="filled",
                  contracts=1, fill_price=D("0.55"), stake=D("0.55"), fee=D("0.02"),
                  client_order_id=f"{fresh}-B01", placed_at="2026-09-21T05:10:00Z")
    assert history.is_complete(lg, fresh) is False
    _context, _memory, _principles, ids, _direction, _effective = cells.render(
        lg, settings, "static")
    assert fresh not in ids


def test_the_static_count_reaches_the_heading_and_the_memory_sentence(lg, settings):
    """The spec writes both with the word "ten", which is the default; a changed setting
    has to move both or the page claims a number of records it does not hold."""
    _seed_attempts(lg, 6)
    settings.cells.static_recent = 4

    context, memory, _principles, ids, _direction, _effective = cells.render(
        lg, settings, "static")

    assert len(ids) == 4
    assert context.splitlines()[0] == "## Past attempts (the 4 most recent)"
    assert "which holds the 4 most recent attempts" in memory
    assert "ten" not in memory


def test_the_default_count_renders_the_spec_s_own_wording(lg, settings):
    """At the configured ten the heading and the sentence are the spec's verbatim texts."""
    _seed_attempts(lg, 2)
    context, memory, *_ = cells.render(lg, settings, "static")
    assert settings.cells.static_recent == 10
    assert context.splitlines()[0] == "## Past attempts (the ten most recent)"
    assert memory == (
        "\nStudy ../CONTEXT.md, which holds the ten most recent attempts, before choosing "
        "a target. `bt past` searches the whole history."
    )


def test_a_set_naming_an_attempt_the_ledger_does_not_have_renders_the_rest(lg, settings):
    """A stored set is a list of ids written days earlier. One id that names nothing must
    not fail every director and focused slot until a newer page validates, and the row
    must record only the examples the attempt actually saw."""
    seeded = _seed_attempts(lg, 3)
    _director_run(lg, {
        "balanced": [seeded[0], "A-9999", seeded[1]], "balanced_note": "note",
        "focused": ["A-9999", seeded[2]], "focused_lens": "one lens",
        "focused_direction": "Read the ladders nobody reprices overnight.",
    })

    context, _memory, _principles, ids, _direction, effective = cells.render(
        lg, settings, "director")

    assert effective == "director"
    assert ids == [seeded[0], seeded[1]]          # render order, stranger dropped
    assert "A-9999" not in context
    for aid in ids:
        assert aid in context


def test_the_focused_set_drops_a_stranger_too(lg, settings):
    seeded = _seed_attempts(lg, 3)
    _director_run(lg, {
        "balanced": seeded[:1], "balanced_note": "note",
        "focused": ["A-9999", seeded[2]], "focused_lens": "one lens",
        "focused_direction": "Read the ladders nobody reprices overnight.",
    })

    context, _memory, _principles, ids, _direction, _effective = cells.render(
        lg, settings, "focused")

    assert ids == [seeded[2]]
    assert "A-9999" not in context


def test_director_shows_the_direction_then_the_balanced_set(lg, settings):
    seeded = _seed_attempts(lg, 4)
    _director_run(lg, {
        "balanced": seeded[:2], "balanced_note": "Two entries and two passes.",
        "focused": seeded[2:], "focused_lens": "stale weather ladders",
        "focused_direction": "Read the ladders nobody reprices overnight.",
    })
    context, memory, principles, ids, direction, effective = cells.render(
        lg, settings, "director")
    assert effective == "director"
    assert ids == seeded[:2]
    assert memory.strip().startswith("Study ../CONTEXT.md first:")
    assert principles.strip().startswith("# Principles to guide you")
    assert context.startswith("## Direction\n")
    assert "Keep sweeping the strait ladders" in context
    assert "Yesterday's three refusals" in context
    # The page's third heading is not direction and does not travel with it.
    assert "Whether the closure ladder reprices" not in context
    assert "## Past attempts (chosen for today)" in context
    assert "Two entries and two passes." in context
    assert direction == cells._sha12(
        "Keep sweeping the strait ladders and leave the weather families alone.\n\n"
        "Yesterday's three refusals were all the per-market cap. Size smaller or bet "
        "elsewhere."
    )


def test_focused_shows_its_own_direction_and_lens(lg, settings):
    seeded = _seed_attempts(lg, 4)
    _director_run(lg, {
        "balanced": seeded[:2], "balanced_note": "note",
        "focused": seeded[2:], "focused_lens": "stale weather ladders",
        "focused_direction": "Read the ladders nobody reprices overnight.",
    })
    context, memory, _principles, ids, direction, effective = cells.render(
        lg, settings, "focused")
    assert effective == "focused"
    assert ids == seeded[2:]
    assert memory.strip().startswith("Study ../CONTEXT.md first:")
    assert context.startswith("## Direction\n")
    assert "Read the ladders nobody reprices overnight." in context
    assert "## Past attempts (stale weather ladders)" in context
    # The standing direction belongs to the director cell, not this one.
    assert "Keep sweeping the strait ladders" not in context
    assert direction == cells._sha12("Read the ladders nobody reprices overnight.")


@pytest.mark.parametrize("cell", ["director", "focused"])
def test_director_and_focused_fall_back_to_static_with_no_valid_run(lg, settings, cell):
    _seed_attempts(lg, 3)
    context, memory, _principles, ids, direction, effective = cells.render(
        lg, settings, cell)
    assert effective == "static"
    assert direction is None
    assert context.startswith("## Past attempts (the ten most recent)\n")
    assert memory.strip().startswith("Study ../CONTEXT.md, which holds the ten most recent")
    assert ids == history.recent_completed(lg, limit=settings.cells.static_recent)


def test_an_invalid_run_is_not_a_run(lg, settings):
    seeded = _seed_attempts(lg, 2)
    _director_run(lg, {"balanced": seeded, "balanced_note": "n"}, status="invalid")
    _context, _memory, _principles, _ids, _direction, effective = cells.render(
        lg, settings, "director")
    assert effective == "static"


def test_the_records_are_the_same_text_the_history_tool_prints(lg, settings):
    """docs/22 section 14: one renderer, so what an attempt reads and what ``bt past
    attempt`` prints cannot disagree."""
    seeded = _seed_attempts(lg, 3)
    context, *_ = cells.render(lg, settings, "static")
    for aid in seeded:
        assert history.render_record(lg, aid) in context


def test_an_empty_history_says_so_rather_than_rendering_a_bare_heading(lg, settings):
    context, _memory, _principles, ids, _direction, _effective = cells.render(
        lg, settings, "static")
    assert ids == []
    assert context.strip().endswith("none yet")


# --------------------------------------------------------------------------- principles
def test_the_principles_page_is_read_whole_from_the_top(lg, settings):
    page = (settings.root / "docs" / "principles.md").read_text()
    for cell in ("static", "director", "focused"):
        assert cells.principles_section(settings, cell) == page.strip()
    assert cells.principles_section(settings, "baseline") == ""


def test_a_missing_principles_page_renders_nothing(lg, settings):
    (settings.root / "docs" / "principles.md").unlink()
    assert cells.principles_section(settings, "static") == ""
    _context, _memory, principles, *_ = cells.render(lg, settings, "static")
    assert principles == ""
