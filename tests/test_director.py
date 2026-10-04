"""The daily director run: its workspace, its validation, and what it stores (docs/22 8.2).

Three things are worth pinning here.

The workspace is the whole of what the session can see, so what is in it is the experiment:
yesterday's cohort with its tickets, legs, activity and closing summaries, the cohorts that
have settled since the last run with their outcomes, the digest, and the previous pages.
An attempt that is missing from it is an attempt the direction was written without.

Validation is a gate with one reason, and every rejection case has its own. A run that is
rejected must say which rule it broke, on the row and in the audit, because the alternative
is a page that quietly stops appearing and nobody knowing why.

Storage is what the rest of the loop reads. The page and the sets feed the cells, the
paragraphs feed ``bt past attempt``, and the realized ordering is computed here from the
bet rows so that the retrospective ranking can be compared with pure profit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

import pytest

from betting_agent.config import load_settings
from betting_agent.harness import director
from betting_agent.ledger import history
from betting_agent.ledger.db import Ledger
from betting_agent.timeutil import iso, parse_iso

RUN = "2026-09-21"          # the run date under test
DAY = "2026-09-20"          # the cohort it reviews prospectively
EARLIER = "2026-09-18"      # a cohort that has settled

NOW = parse_iso("2026-09-21T04:00:00Z")

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

PARAGRAPH = (
    "This attempt worked one family and said why. The claim named the market, the fee was "
    "priced before the limit was set, and the book was read at the size the leg asked for. "
    "A later attempt should take the habit of stating the kill criteria first, and should "
    "not repeat treating a cheap entry as evidence that the family stays cheap."
)
def _page(direction: str = "", today: str = "", watching: str = "") -> str:
    """A valid page, with a line added to any of its three sections."""
    def block(heading: str, body: str, extra: str) -> str:
        return f"## {heading}\n{body}\n" + (f"{extra}\n" if extra else "")

    return "\n".join([
        block("Standing direction",
              "Keep sweeping the strait ladders and leave the weather families alone.",
              direction),
        block("Today", "The cohort read the same board and split on the same family.",
              today),
        block("Watching", "Whether the closure ladder reprices after the convoy.",
              watching),
    ])


PAGE = _page()
FOCUSED_DIRECTION = (
    "Read these entries for how a claim becomes a leg: each names the market, the reason "
    "the price is wrong, and the fact that would end the position. Work the same family, "
    "and if the price has moved say so in the ticket rather than restating an old claim."
)


# --------------------------------------------------------------------------- the world
@pytest.fixture
def settings(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "principles.md").write_text("# Principles\n\n1. Be ambitious.\n")
    return load_settings(root=tmp_path)


@pytest.fixture
def lg(settings):
    ledger = Ledger.open(settings.ledger_path)
    ledger.migrate()
    yield ledger
    ledger.close()


def _attempt(lg, *, slot, status="no_bets", cell="static", claim=CLAIM, summary="Done.",
             stop_at=None, era="live-v2"):
    _seq, aid = lg.create_attempt(
        env="prod", model="claude-opus-5", effort="high", memory_mode="on",
        prompt_version="p", toolkit_version="0.1.0", workspace_path="/ws",
        slot=slot, cell=cell, cell_effective=cell, era=era,
    )
    lg.transition(aid, "running")
    if stop_at == "running":
        return aid
    if claim is not None:
        lg.set_ticket_texts(aid, claim, HYPOTHESIS, "manifest text")
    if summary is not None:
        lg.set_session_summary(aid, summary)
    lg.update_attempt_fields(aid, wall_seconds=1860, cost_usd=D("11.94"))
    lg.transition(aid, status)
    return aid


def _bet(lg, aid, index, ticker, *, status="filled", outcome=None, pnl=None, contracts=1,
         price="0.62", fee="0.04"):
    lg.insert_bet(
        bet_id=f"{aid}-B{index:02d}", attempt_id=aid, ticket_index=index, ticker=ticker,
        market_title=ticker, category="Politics", side="no", limit_price=D(price),
        rationale="r", is_real=1, status=status, contracts=contracts,
        fill_price=D(price), stake=D(price) * contracts, fee=D(fee),
        outcome=outcome, pnl=D(pnl) if pnl is not None else None,
        settled_at=iso(NOW) if status == "settled" else None,
    )


def _page_run(lg, run_date, *, status="valid", page=PAGE):
    """A stored director run, the way the two earlier ones already sit in the ledger."""
    lg.conn.execute(
        "INSERT INTO director_runs (run_id, run_date, cohort_date, started_at, model, "
        "status, page_md, page_hash, sets_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"D-{run_date}", run_date, director.cohort_date_for(run_date),
         f"{run_date}T04:00:00Z", "claude-fable-5", status, page, "abc123abc123",
         json.dumps({"balanced": []})),
    )
    lg.conn.commit()


@pytest.fixture
def seeded(lg):
    """Nine attempts on ``DAY`` across the four cells, and a settled cohort before them.

    The nine include the two shapes the review has to survive: a session that failed
    before it wrote a ticket, and an attempt that passed. The earlier cohort is complete,
    so it is the one the run reviews retrospectively, and its three attempts carry the
    tie the realized ordering has to break.
    """
    ids: dict[str, list[str]] = {"cohort": [], "settled": []}

    # The settled cohort: two winners tied on net, one loser. B-of-the-pair wins the tie
    # on net per contract (the same +0.72 on one contract instead of two).
    wide = _attempt(lg, slot=f"slot:{EARLIER}/01:00", status="placed", cell="director")
    _bet(lg, wide, 1, "KXA-T1", status="settled", outcome="win", pnl="0.36", contracts=2)
    _bet(lg, wide, 2, "KXA-T2", status="settled", outcome="win", pnl="0.36", contracts=2)
    lg.transition(wide, "settled")
    tight = _attempt(lg, slot=f"slot:{EARLIER}/03:40", status="placed", cell="static")
    _bet(lg, tight, 1, "KXB-T1", status="settled", outcome="win", pnl="0.72", contracts=1)
    lg.transition(tight, "settled")
    losing = _attempt(lg, slot=f"slot:{EARLIER}/06:20", status="placed", cell="focused")
    _bet(lg, losing, 1, "KXC-T1", status="settled", outcome="loss", pnl="-0.30")
    lg.transition(losing, "settled")
    ids["settled"] = [wide, tight, losing]

    # Yesterday's cohort: one baseline, three static, four director, one focused.
    plan = (["baseline"] + ["static"] * 3 + ["director"] * 4 + ["focused"])
    slots = ["01:00", "03:40", "06:20", "09:00", "11:40", "14:20", "17:00", "19:40", "22:20"]
    for index, (hhmm, cell) in enumerate(zip(slots, plan, strict=True)):
        if index == 4:      # the session that failed before it wrote a ticket
            aid = _attempt(lg, slot=f"slot:{DAY}/{hhmm}", status="failed", cell=cell,
                           claim=None, summary=None)
        elif index == 7:    # the pass
            aid = _attempt(lg, slot=f"slot:{DAY}/{hhmm}", status="no_bets", cell=cell,
                           summary="Read three ladders and took none of them.")
        else:
            aid = _attempt(lg, slot=f"slot:{DAY}/{hhmm}", status="placed", cell=cell)
            _bet(lg, aid, 1, f"KXD{index}-T1")
        ids["cohort"].append(aid)

    _page_run(lg, "2026-09-19")
    _page_run(lg, "2026-09-20")
    return ids


# --------------------------------------------------------------------------- helpers
def _outputs(seeded, *, review=None, page=None, sets=None) -> dict:
    """A valid triple over the seeded world, with one part swapped out at a time."""
    cohort = seeded["cohort"]
    settled = seeded["settled"]

    def entry(day, members):
        return {"cohort": day, "ranking": list(members),
                "paragraphs": {a: PARAGRAPH for a in members}}

    return {
        "review.json": review if review is not None else {
            "prospective": entry(DAY, cohort),
            "retrospective": [entry(EARLIER, settled)],
        },
        "page.md": PAGE if page is None else page,
        "sets.json": sets if sets is not None else {
            "balanced": cohort[:8],
            "balanced_note": "A spread of families and outcomes.",
            "focused": cohort[:8],
            "focused_lens": "the attempts whose reasoning was most precise",
            "focused_direction": FOCUSED_DIRECTION,
        },
    }


def _write(work_dir: Path, files: dict) -> None:
    for name, body in files.items():
        text = body if isinstance(body, str) else json.dumps(body, indent=2)
        (work_dir / name).write_text(text, encoding="utf-8")


def _stub_session(monkeypatch, files, *, exit_kind="ok"):
    """Replace the session with one that writes into the workspace.

    ``files`` is the mapping to write, a callable handed the workspace directory when a
    test needs to write something a mapping cannot express, or None for a session that
    writes nothing at all.
    """
    calls = []

    def fake(ledger, settings, *, session_id, workspace, model):
        calls.append({"session_id": session_id, "model": model,
                      "work_dir": workspace.work_dir})
        if callable(files):
            files(workspace.work_dir)
        elif files is not None:
            _write(workspace.work_dir, files)
        return SimpleNamespace(exit_kind=exit_kind, result_text="done")

    monkeypatch.setattr(director, "_launch_director", fake)
    return calls


def _run(lg, settings, monkeypatch, files, *, exit_kind="ok", run_date=RUN, now=NOW):
    _stub_session(monkeypatch, files, exit_kind=exit_kind)
    return director.run_director(lg, settings, run_date=run_date, now=now)


def _audit_reasons(lg) -> list[str]:
    return [json.loads(r["detail"])["reason"]
            for r in lg.audit_events(event="director_invalid")]


# --------------------------------------------------------------------------- the workspace
def test_the_workspace_holds_the_cohort_the_settled_cohorts_the_pages_and_the_task(
    lg, settings, seeded
):
    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)

    assert ws.run_dir == settings.director_dir / RUN
    assert ws.work_dir == ws.run_dir / "workspace"
    assert ws.cohort == seeded["cohort"]
    assert ws.settled == {EARLIER: seeded["settled"]}

    # The digest is about the day being reviewed, not the day it is written on.
    assert (ws.work_dir / "digest.md").read_text().startswith(f"# Status {DAY}")
    # Every member of yesterday's cohort has a folder, and only them.
    assert sorted(p.name for p in (ws.work_dir / "cohort").iterdir()) == sorted(ws.cohort)
    # The last seven valid pages, named by run date.
    assert sorted(p.name for p in (ws.work_dir / "pages").iterdir()) == \
        ["2026-09-19.md", "2026-09-20.md"]
    assert (ws.work_dir / "pages" / "2026-09-20.md").read_text() == PAGE

    task = (ws.run_dir / "TASK.md").read_text()
    assert task.startswith(f"# Director run {RUN}")
    assert f"({DAY})" in task
    assert "{run_date}" not in task and "{cohort_date}" not in task
    # The file shapes reach the session with their braces intact and the cohort date in
    # the skeleton, which is the whole point of substituting only the two placeholders.
    assert '{"prospective": {"cohort": "' + DAY + '",' in task
    assert '"paragraphs": {"A-0187": "...", "A-0188": "..."}}' in task


def test_each_attempt_folder_is_the_record_the_history_tool_prints(lg, settings, seeded):
    """docs/22 section 8.5: one renderer, so the director reads what ``bt past`` shows."""
    aid = seeded["cohort"][0]
    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)
    folder = ws.work_dir / "cohort" / aid

    assert sorted(p.name for p in folder.iterdir()) == \
        ["MANIFEST.md", "activity.md", "edge_claim.md", "hypothesis.md", "legs.md",
         "summary.md"]
    full = history.render_record(lg, aid, full=True)
    assert (folder / "edge_claim.md").read_text().strip() == CLAIM.strip()
    assert (folder / "hypothesis.md").read_text().strip() == HYPOTHESIS.strip()
    # The legs and the summary are the full record's own text, verbatim.
    assert (folder / "legs.md").read_text().strip() in full
    assert (folder / "summary.md").read_text().strip() == "Done."
    assert "KXD0-T1 no @0.6200" in (folder / "legs.md").read_text()
    assert "no activity row recorded" in (folder / "activity.md").read_text()
    # Nothing has settled in this cohort, so no outcome file.
    assert not (folder / "outcomes.md").exists()


def test_a_session_that_failed_before_its_ticket_is_a_member_with_an_empty_record(
    lg, settings, seeded
):
    failed = seeded["cohort"][4]
    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)
    folder = ws.work_dir / "cohort" / failed

    assert sorted(p.name for p in folder.iterdir()) == ["activity.md", "legs.md",
                                                        "summary.md"]
    assert (folder / "legs.md").read_text().strip() == "none (session failed, no ticket)"
    assert (folder / "summary.md").read_text().strip() == "none recorded"


def test_a_pass_says_it_passed_and_still_carries_its_claim(lg, settings, seeded):
    passed = seeded["cohort"][7]
    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)
    folder = ws.work_dir / "cohort" / passed

    assert (folder / "legs.md").read_text().strip() == "none (passed)"
    assert "Why this is profitable" in (folder / "edge_claim.md").read_text()
    assert "took none of them" in (folder / "summary.md").read_text()


def test_a_settled_cohort_carries_profit_and_fee_per_leg(lg, settings, seeded):
    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)
    folder = ws.work_dir / "settled" / EARLIER / seeded["settled"][0]

    outcomes = (folder / "outcomes.md").read_text()
    assert "KXA-T1 no @0.6200 x2 · settled · win · profit 0.3600 · fee 0.0400" in outcomes
    assert "Net 0.7200 on 2.4800 staked over 2 legs, 2 won" in outcomes
    assert "KXA: net 0.7200" in outcomes
    assert (folder / "legs.md").exists()          # the same six files, plus the outcome


def test_an_attempt_still_running_is_left_out_of_the_prospective_cohort(lg, settings, seeded):
    """docs/22 section 5.1: the hour passes and the run goes ahead without the straggler."""
    late = _attempt(lg, slot=f"slot:{DAY}/23:00", stop_at="running")

    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)

    assert late not in ws.cohort
    assert not (ws.work_dir / "cohort" / late).exists()
    assert len(ws.cohort) == 9


def test_an_incomplete_cohort_is_not_offered_for_retrospective_review(lg, settings, seeded):
    """The cohort under prospective review has open positions, so it is not in settled/."""
    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)
    assert DAY not in ws.settled
    assert sorted(p.name for p in (ws.work_dir / "settled").iterdir()) == [EARLIER]


def test_a_cohort_already_reviewed_retrospectively_is_not_offered_again(lg, settings, seeded):
    lg.conn.execute(
        "INSERT INTO cohort_reviews (cohort_date, kind, run_id, ranking) "
        "VALUES (?,'retrospective',?,?)",
        (EARLIER, "D-2026-09-20", json.dumps(seeded["settled"])),
    )
    lg.conn.commit()

    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)

    assert ws.settled == {}


def test_only_current_era_cohorts_are_offered_for_retrospective_review(lg, settings, seeded):
    """The old eras belong to the loop this one replaced, and are never reviewed.

    Without this the first run of the new loop is handed every complete cohort in the
    ledger and asked to rank all of them inside a 45-minute envelope.
    """
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        old = _attempt(lg, slot=f"slot:{day}/01:00", status="no_bets", era="live-v1")
        assert lg.get_attempt(old)["era"] == "live-v1"

    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)

    assert list(ws.settled) == [EARLIER]


def test_at_most_three_settled_cohorts_are_offered_and_they_are_the_newest(lg, settings,
                                                                          seeded):
    days = ["2026-09-10", "2026-09-11", "2026-09-12", "2026-09-13", "2026-09-14"]
    for day in days:
        _attempt(lg, slot=f"slot:{day}/01:00", status="no_bets")

    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)

    # Five unreviewed cohorts plus the seeded one: the three most recent, oldest first.
    assert list(ws.settled) == ["2026-09-13", "2026-09-14", EARLIER]
    assert sorted(p.name for p in (ws.work_dir / "settled").iterdir()) == \
        ["2026-09-13", "2026-09-14", EARLIER]


def test_yesterdays_cohort_is_never_reviewed_both_ways_in_one_run(lg, settings, monkeypatch):
    """A cohort of passes is complete the moment its last session ends.

    Offering it in ``settled/`` would put its outcomes in the workspace the prospective
    ranking is supposed to be written without, in the same run.
    """
    passes = [_attempt(lg, slot=f"slot:{DAY}/0{n}:00", status="no_bets") for n in (1, 3, 6)]
    assert all(history.is_complete(lg, a) for a in passes)

    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)

    assert ws.cohort == passes
    assert ws.settled == {}
    assert list((ws.work_dir / "settled").iterdir()) == []


# --------------------------------------------------------------------------- the brief
def _skeleton(task: str, label: str) -> dict:
    """The JSON example the brief shows under ``label``, parsed.

    The brief writes the two file shapes as indented literal JSON, so this reads one back
    the way the model is meant to: whatever sits under that line is what the run should
    look like.
    """
    lines = task.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(label)) + 1
    block = []
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        if line.strip():
            block.append(line)
    return json.loads("\n".join(block))


def test_the_brief_shows_the_shapes_the_validator_accepts(lg, settings, seeded, monkeypatch):
    """The skeletons in the rendered brief parse, carry the run's own cohort date, and an
    output built to them validates.

    The first live run was discarded at the first check for writing a shape of its own
    invention, because the brief described the three files in prose and showed none.
    """
    task = director.task_md(RUN, DAY, attempts_per_day=len(settings.schedule.slots))
    review = _skeleton(task, "`review.json`")
    sets = _skeleton(task, "`sets.json`")

    assert set(review) == {"prospective", "retrospective"}
    assert set(review["prospective"]) == {"cohort", "ranking", "paragraphs"}
    assert review["prospective"]["cohort"] == DAY          # the placeholder was substituted
    assert set(review["retrospective"][0]) == {"cohort", "ranking", "paragraphs"}
    assert set(sets) == {"balanced", "balanced_note", "focused", "focused_lens",
                         "focused_direction"}

    # The same shapes, filled with this world's ids, are what the validator wants.
    cohort, settled = seeded["cohort"], seeded["settled"]
    review["prospective"]["ranking"] = list(cohort)
    review["prospective"]["paragraphs"] = {a: PARAGRAPH for a in cohort}
    review["retrospective"] = [{"cohort": EARLIER, "ranking": list(settled),
                                "paragraphs": {a: PARAGRAPH for a in settled}}]
    sets.update(balanced=cohort[:8], focused=cohort[:8],
                balanced_note="A spread of families and outcomes.",
                focused_lens="the attempts whose reasoning was most precise",
                focused_direction=FOCUSED_DIRECTION)

    result = _run(lg, settings, monkeypatch,
                  {"review.json": review, "page.md": PAGE, "sets.json": sets})

    assert result == {"run_id": f"D-{RUN}", "status": "valid", "error": None}


def test_the_shape_the_first_live_run_wrote_is_still_rejected(lg, settings, seeded,
                                                              monkeypatch):
    """D-2026-09-19, the first real run: a ranking of objects under a key it invented.

    The brief now shows the file. The validator is unchanged, and this is the output it
    turned down, kept here so the reason it gave stays the reason it gives.
    """
    files = _outputs(seeded)
    files["review.json"] = {
        "written": "2026-09-19T00:13:00Z",
        "cohort": {
            "date": DAY, "kind": "prospective", "note": "nine attempts, one cohort",
            "ranking": [{"rank": 1, "id": seeded["cohort"][0], "paragraph": PARAGRAPH}],
        },
        "settled": [],
    }

    result = _run(lg, settings, monkeypatch, files)

    assert result["status"] == "invalid"
    assert result["error"] == "review.json has no prospective object"


def test_a_page_may_carry_a_title_line_above_its_three_headings(lg, settings, seeded,
                                                                monkeypatch):
    """The brief says a ``#`` title line is allowed, so the validator has to allow one."""
    files = _outputs(seeded, page=f"# Director page, written {RUN}\n\n{PAGE}")

    assert _run(lg, settings, monkeypatch, files)["status"] == "valid"


# --------------------------------------------------------------------------- the session
def test_the_session_runs_inside_the_workspace_with_the_history_open(lg, settings, seeded,
                                                                     monkeypatch):
    """The envelope, the tools and the two directories, pinned (docs/22 section 8.2)."""
    captured = {}

    def fake_run_session(spec, *, runner_cmd, stream_path, err_path):
        captured.update(spec=spec, runner=runner_cmd, stream=stream_path)
        return SimpleNamespace(exit_kind="ok", result_text="done")

    monkeypatch.setattr(director, "run_session", fake_run_session)

    director.run_director(lg, settings, run_date=RUN, now=NOW)

    spec = captured["spec"]
    assert spec.kind == "director"
    assert spec.prompt == "Read ../TASK.md and carry it out completely."
    assert spec.cwd == settings.director_dir / RUN / "workspace"
    assert spec.add_dirs == [settings.director_dir / RUN]
    assert spec.extra_env["BT_PAST"] == "on"
    assert spec.disallowed_tools == "Skill"
    # Write and Edit beside the four the spec names: the run's whole product is three
    # files, and a shell heredoc is a poor way to write one.
    assert spec.tools == "Read,Grep,Glob,Bash,Write,Edit"
    assert spec.effort == settings.director.effort == "xhigh"
    assert spec.model == "claude-fable-5-1"
    assert spec.max_turns == settings.director.max_turns
    assert spec.max_budget_usd == settings.director.max_budget_usd
    assert spec.wall_time_s == settings.director.wall_time_min * 60
    assert captured["runner"] == settings.attempt.runner
    # The session row is the run's own, and it closed.
    row = lg.conn.execute("SELECT * FROM sessions WHERE session_id=?",
                          (spec.session_id,)).fetchone()
    assert row["kind"] == "director" and row["ended_at"] is not None
    assert lg.director_run(f"D-{RUN}")["session_id"] == spec.session_id


# --------------------------------------------------------------------------- validation
def test_a_good_triple_is_accepted_and_stored(lg, settings, seeded, monkeypatch):
    result = _run(lg, settings, monkeypatch, _outputs(seeded))

    assert result == {"run_id": f"D-{RUN}", "status": "valid", "error": None}
    row = lg.director_run(f"D-{RUN}")
    assert row["status"] == "valid" and row["cohort_date"] == DAY
    assert row["page_md"] == PAGE
    assert row["page_hash"] == director._sha12(PAGE)
    assert row["ended_at"] is not None and row["error"] is None
    assert json.loads(row["sets_json"])["focused_lens"].startswith("the attempts")


@pytest.mark.parametrize("case,expected", [
    ("missing_attempt", "prospective ranking is missing"),
    ("stranger", "which is not in the 2026-09-20 cohort"),
    ("short_paragraph", "characters, outside 200 to 1500"),
    ("long_page", "over the 4000 limit"),
    ("no_headings", "must carry exactly the headings"),
    ("unknown_id", "which is not an attempt"),
    ("repeated_id", "more than once"),
    ("five_ids", "balanced holds 5 ids, outside 6 to 12"),
    ("thirteen_ids", "balanced holds 13 ids, outside 6 to 12"),
])
def test_every_rejection_case_has_its_own_reason(lg, settings, seeded, monkeypatch, case,
                                                 expected):
    """docs/22 section 14: each of these is rejected, and each says which rule it broke."""
    cohort = seeded["cohort"]
    files = _outputs(seeded)
    if case == "missing_attempt":
        files["review.json"]["prospective"]["ranking"] = cohort[:-1]
    elif case == "stranger":
        files["review.json"]["prospective"]["ranking"] = [*cohort, "A-9999"]
    elif case == "short_paragraph":
        files["review.json"]["prospective"]["paragraphs"][cohort[0]] = "Too short."
    elif case == "long_page":
        files["page.md"] = PAGE + "filler. " * 600
    elif case == "no_headings":
        files["page.md"] = "## Standing direction\nKeep sweeping the ladders.\n"
    elif case == "unknown_id":
        files["sets.json"]["balanced"] = [*cohort[:7], "A-9999"]
    elif case == "repeated_id":
        files["sets.json"]["balanced"] = [*cohort[:7], cohort[0]]
    elif case == "five_ids":
        files["sets.json"]["balanced"] = cohort[:5]
    elif case == "thirteen_ids":
        files["sets.json"]["balanced"] = (cohort * 2)[:13]

    result = _run(lg, settings, monkeypatch, files)

    assert result["status"] == "invalid"
    assert expected in result["error"]
    row = lg.director_run(f"D-{RUN}")
    assert row["status"] == "invalid" and row["error"] == result["error"]
    assert row["page_md"] is None and row["sets_json"] is None
    assert _audit_reasons(lg) == [result["error"]]
    assert lg.conn.execute("SELECT COUNT(*) AS n FROM attempt_reviews").fetchone()["n"] == 0


@pytest.mark.parametrize("line", [
    "Place two bets in the weather families tomorrow.",
    "No bets today.",
    "Skip today and wait for the convoy.",
    "Take at most two positions per slot.",
    "place 2 bets",
    "do not bet today",
    "3 contracts on every leg",
])
def test_the_standing_direction_may_not_set_throughput(lg, settings, seeded, monkeypatch,
                                                       line):
    """docs/22 section 8.4: the page directs attention, and throughput is fixed above it."""
    files = _outputs(seeded, page=_page(direction=line))

    result = _run(lg, settings, monkeypatch, files)

    assert result["status"] == "invalid"
    assert result["error"] == f"page.md sets throughput: {line}"
    assert _audit_reasons(lg) == [result["error"]]


@pytest.mark.parametrize("where,line", [
    ("today", "The cohort placed 11 contracts across five families and only three filled."),
    ("today", "Two attempts asked for 3 contracts on a book that showed one"),
    ("watching", "whether the closure ladder will pass today's convoy headline"),
])
def test_the_other_two_sections_may_count_what_happened(lg, settings, seeded, monkeypatch,
                                                        where, line):
    """"Today" reports and "Watching" wonders; neither instructs, and neither is checked.

    These three sentences are the reason the check is scoped to the standing direction: a
    page that cannot say what the cohort did is a page that cannot say anything.
    """
    files = _outputs(seeded, page=_page(**{where: line}))

    assert _run(lg, settings, monkeypatch, files)["status"] == "valid"


def test_a_page_that_is_not_utf8_is_rejected_rather_than_raised(lg, settings, seeded,
                                                                monkeypatch):
    """A run left ``running`` by a decode error is a run nobody ever closes."""
    files = _outputs(seeded)
    del files["page.md"]

    def write_then_break(work_dir):
        _write(work_dir, files)
        (work_dir / "page.md").write_bytes(b"## Standing direction\n\xff\xfe not utf-8\n")

    result = _run(lg, settings, monkeypatch, write_then_break)

    assert result["status"] == "invalid"
    assert result["error"] == "page.md is missing or does not parse"


@pytest.mark.parametrize("name", ["review.json", "page.md", "sets.json"])
def test_a_missing_file_is_rejected_by_name(lg, settings, seeded, monkeypatch, name):
    files = _outputs(seeded)
    del files[name]

    result = _run(lg, settings, monkeypatch, files)

    assert result["status"] == "invalid"
    assert result["error"] == f"{name} is missing or does not parse"


def test_a_missing_retrospective_entry_is_rejected(lg, settings, seeded, monkeypatch):
    files = _outputs(seeded)
    files["review.json"]["retrospective"] = []

    result = _run(lg, settings, monkeypatch, files)

    assert result["error"] == f"review.json has no retrospective entry for cohort {EARLIER}"


def test_a_retrospective_cohort_nobody_asked_for_is_rejected(lg, settings, seeded, monkeypatch):
    files = _outputs(seeded)
    files["review.json"]["retrospective"].append(
        {"cohort": "2026-01-01", "ranking": [], "paragraphs": {}})

    result = _run(lg, settings, monkeypatch, files)

    assert result["error"] == ("retrospective names cohort 2026-01-01, which has no folder "
                               "in settled/")


def test_a_prospective_review_of_the_wrong_cohort_is_rejected(lg, settings, seeded,
                                                              monkeypatch):
    files = _outputs(seeded)
    files["review.json"]["prospective"]["cohort"] = "2026-09-01"

    result = _run(lg, settings, monkeypatch, files)

    assert result["error"] == f"prospective names cohort 2026-09-01, not {DAY}"


def test_a_repeated_attempt_in_a_ranking_is_rejected(lg, settings, seeded, monkeypatch):
    files = _outputs(seeded)
    cohort = seeded["cohort"]
    files["review.json"]["prospective"]["ranking"] = [cohort[0], *cohort]

    result = _run(lg, settings, monkeypatch, files)

    assert result["error"] == f"prospective ranking names {cohort[0]} more than once"


@pytest.mark.parametrize("field,expected", [
    ("balanced_note", "balanced_note is empty"),
    ("focused_lens", "focused_lens is empty"),
])
def test_an_empty_set_note_is_rejected(lg, settings, seeded, monkeypatch, field, expected):
    files = _outputs(seeded)
    files["sets.json"][field] = "  "

    assert _run(lg, settings, monkeypatch, files)["error"] == expected


def test_a_short_focused_direction_is_rejected(lg, settings, seeded, monkeypatch):
    files = _outputs(seeded)
    files["sets.json"]["focused_direction"] = "Work the ladders."

    result = _run(lg, settings, monkeypatch, files)

    assert result["error"] == "focused_direction is 17 characters, outside 200 to 1200"


def test_a_retrospective_rejection_names_its_own_cohort(lg, settings, seeded, monkeypatch):
    """The two kinds share the ranking rules, so the reason has to say which one broke."""
    files = _outputs(seeded)
    files["review.json"]["retrospective"][0]["ranking"] = seeded["settled"][:1]

    result = _run(lg, settings, monkeypatch, files)

    assert result["error"] == (f"retrospective {EARLIER} ranking is missing "
                               f"{seeded['settled'][1]}")


# --------------------------------------------------------------------------- storage
def test_storage_writes_a_review_row_per_attempt_and_a_ranking_per_cohort(
    lg, settings, seeded, monkeypatch
):
    cohort, settled = seeded["cohort"], seeded["settled"]
    _run(lg, settings, monkeypatch, _outputs(seeded))

    rows = lg.conn.execute(
        "SELECT * FROM attempt_reviews ORDER BY kind, rank").fetchall()
    assert len(rows) == len(cohort) + len(settled)
    prospective = [r for r in rows if r["kind"] == "prospective"]
    assert [r["attempt_id"] for r in prospective] == cohort
    assert [r["rank"] for r in prospective] == list(range(1, 10))
    assert {r["cohort_size"] for r in prospective} == {9}
    assert {r["cohort_date"] for r in prospective} == {DAY}
    assert {r["run_id"] for r in rows} == {f"D-{RUN}"}
    assert prospective[0]["paragraph"] == PARAGRAPH

    ranking = lg.cohort_review(DAY, "prospective")
    assert json.loads(ranking["ranking"]) == cohort
    assert ranking["realized"] is None            # prospective knows no outcome


def test_the_retrospective_row_carries_the_realized_ordering(lg, settings, seeded,
                                                             monkeypatch):
    """docs/22 section 8.3: net first, then net per contract, computed from the bet rows.

    The two winners are tied at +0.72; the one that made it on a single contract ranks
    above the one that needed two.
    """
    wide, tight, losing = seeded["settled"]
    _run(lg, settings, monkeypatch, _outputs(seeded))

    row = lg.cohort_review(EARLIER, "retrospective")
    assert json.loads(row["ranking"]) == [wide, tight, losing]     # what the director said
    assert json.loads(row["realized"]) == [tight, wide, losing]    # what the money said


def test_the_realized_order_is_computed_and_not_asked_for(lg, seeded):
    wide, tight, losing = seeded["settled"]
    assert director.realized_order(lg, [losing, wide, tight]) == [tight, wide, losing]


def test_a_refused_leg_does_not_dilute_net_per_contract(lg):
    """The denominator is contracts held, not contracts proposed.

    Two attempts make the same $0.72 on one filled contract; one of them also had a leg
    the daily cap refused at three contracts. Counting the size it never held would divide
    its profit by four and drop it below the other for having asked for more.
    """
    refused_too = _attempt(lg, slot=f"slot:{EARLIER}/01:00", status="placed")
    _bet(lg, refused_too, 1, "KXA-T1", status="settled", outcome="win", pnl="0.72")
    lg.insert_bet(
        bet_id=f"{refused_too}-B02", attempt_id=refused_too, ticket_index=2,
        ticker="KXA-T2", side="no", limit_price=D("0.62"), rationale="r", is_real=1,
        status="rejected", reject_code="cap_daily", reject_reason="the daily cap",
        declared_contracts=3,
    )
    lg.transition(refused_too, "settled")
    plain = _attempt(lg, slot=f"slot:{EARLIER}/03:40", status="placed")
    _bet(lg, plain, 1, "KXB-T1", status="settled", outcome="win", pnl="0.72")
    lg.transition(plain, "settled")

    # Both are +0.72 on one held contract, so the tie falls to the attempt id.
    assert director.realized_order(lg, [plain, refused_too]) == [refused_too, plain]


def test_a_later_invalid_run_leaves_the_last_valid_page_standing(lg, settings, seeded,
                                                                 monkeypatch):
    """Nothing downstream blocks on a bad run: the cells read the latest VALID one."""
    _run(lg, settings, monkeypatch, _outputs(seeded))
    good = history.latest_valid_run(lg)
    assert good["run_id"] == f"D-{RUN}"

    later = "2026-09-22"
    files = _outputs(seeded)
    files["page.md"] = "## Standing direction\nOnly one heading.\n"
    result = _run(lg, settings, monkeypatch, files, run_date=later,
                  now=NOW + timedelta(days=1))

    assert result["status"] == "invalid"
    assert history.latest_valid_run(lg)["run_id"] == f"D-{RUN}"


def test_a_session_that_breached_its_envelope_is_a_failed_run(lg, settings, seeded,
                                                              monkeypatch):
    result = _run(lg, settings, monkeypatch, None, exit_kind="timeout")

    assert result["status"] == "failed"
    row = lg.director_run(f"D-{RUN}")
    assert row["status"] == "failed" and row["error"] == "session exited timeout"
    assert row["page_md"] is None
    assert _audit_reasons(lg) == []               # a failed session broke no rule
    assert history.latest_valid_run(lg)["run_date"] == "2026-09-20"   # yesterday's stands


def test_a_crashing_session_still_closes_its_run(lg, settings, seeded, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("popen said no")

    monkeypatch.setattr(director, "_launch_director", boom)

    with pytest.raises(RuntimeError):
        director.run_director(lg, settings, run_date=RUN, now=NOW)

    row = lg.director_run(f"D-{RUN}")
    assert row["status"] == "failed"
    assert row["error"] == "RuntimeError: popen said no"


def test_the_run_row_opens_before_the_session_and_names_the_model_that_ran(
    lg, settings, seeded, monkeypatch
):
    """The row exists while the session is running, so a run that dies is still a run."""
    seen = {}

    def fake(ledger, settings_, *, session_id, workspace, model):
        seen.update(row=dict(ledger.director_run(f"D-{RUN}")), model=model,
                    session_id=session_id)
        _write(workspace.work_dir, _outputs(seeded))
        return SimpleNamespace(exit_kind="ok", result_text="done")

    monkeypatch.setattr(director, "_launch_director", fake)
    settings.models.substitute = {"claude-fable-5-1": "claude-opus-5"}
    settings.models.substitute_until = "2099-01-01T00:00:00+00:00"

    director.run_director(lg, settings, run_date=RUN, now=NOW)

    assert seen["row"]["status"] == "running"
    assert seen["row"]["session_id"] == seen["session_id"]
    # The substitution switch applies, and the row names what actually ran.
    assert seen["model"] == "claude-opus-5"
    assert seen["row"]["model"] == "claude-opus-5"


def test_a_workspace_that_cannot_be_built_still_leaves_the_day_a_run(lg, settings, seeded,
                                                                     monkeypatch):
    """The row opens first, so a build that raises does not leave the day with no run.

    Without it the tick finds no row for the date and spawns a fresh director every
    fifteen minutes until midnight.
    """
    def boom(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(director, "build_workspace", boom)

    with pytest.raises(OSError):
        director.run_director(lg, settings, run_date=RUN, now=NOW)

    row = lg.director_run_for_date(RUN)
    assert row["status"] == "failed"
    assert row["error"] == "OSError: read-only file system"
    assert row["session_id"] is None          # no session was ever opened, or paid for


def test_a_failure_while_storing_leaves_neither_a_valid_run_nor_half_a_review(
    lg, settings, seeded, monkeypatch
):
    """The rows and the status flip are one transaction, rows first.

    A valid run whose paragraphs are missing is worse than no run: every reader of the
    loop would take the page and find nothing behind it.
    """
    real = Ledger.store_director_review

    def break_the_last_write(self, run_id, **kw):
        kw["cohort_reviews"] = [*kw["cohort_reviews"],
                                {"cohort_date": DAY, "kind": "sideways", "ranking": "[]",
                                 "realized": None}]
        return real(self, run_id, **kw)

    monkeypatch.setattr(Ledger, "store_director_review", break_the_last_write)

    with pytest.raises(sqlite3.IntegrityError):
        _run(lg, settings, monkeypatch, _outputs(seeded))

    row = lg.director_run(f"D-{RUN}")
    assert row["status"] == "failed" and row["page_md"] is None
    assert lg.conn.execute("SELECT COUNT(*) AS n FROM attempt_reviews").fetchone()["n"] == 0
    assert lg.conn.execute("SELECT COUNT(*) AS n FROM cohort_reviews").fetchone()["n"] == 0


# --------------------------------------------------------------------------- the record
def test_a_ticket_that_names_one_of_the_records_own_headings_is_not_truncated(
    lg, settings, monkeypatch
):
    """The ticket is free text, so a claim can contain a line reading ``## Legs``.

    The record's own sections are found from the end of the document backwards, so the
    line inside the claim stays inside ``edge_claim.md`` where the model put it.
    """
    claim = CLAIM + "\n## Legs\nThe ladder has three strikes worth entering.\n"
    aid = _attempt(lg, slot=f"slot:{DAY}/01:00", status="no_bets", claim=claim)

    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)
    folder = ws.work_dir / "cohort" / aid

    assert "The ladder has three strikes worth entering." in \
        (folder / "edge_claim.md").read_text()
    assert (folder / "legs.md").read_text().strip() == "none (passed)"
    assert "Kill criteria" in (folder / "hypothesis.md").read_text()


def test_every_per_attempt_file_is_a_section_of_what_bt_past_attempt_prints(lg, settings,
                                                                            seeded):
    """docs/22 section 8.5: one renderer, so the workspace cannot drift from the tool."""
    aid = seeded["settled"][0]
    printed = history.render_record(lg, aid, full=True)
    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)
    folder = ws.work_dir / "settled" / EARLIER / aid

    for name in ("edge_claim.md", "hypothesis.md", "MANIFEST.md", "legs.md", "activity.md",
                 "summary.md"):
        body = (folder / name).read_text().strip()
        assert body, f"{name} is empty"
        assert body in printed, f"{name} is not what bt past attempt prints"


def test_the_workspace_keeps_the_last_seven_pages(lg, settings):
    """Seven, newest first: a run reads its own recent history, not the whole archive."""
    for day in range(1, 10):
        _page_run(lg, f"2026-08-{day:02d}", page=_page(direction=f"Day {day}."))

    ws = director.build_workspace(lg, settings, run_date=RUN, now=NOW)

    kept = sorted(p.name for p in (ws.work_dir / "pages").iterdir())
    assert len(kept) == 7
    assert kept == ["2026-08-03.md", "2026-08-04.md", "2026-08-05.md", "2026-08-06.md",
                    "2026-08-07.md", "2026-08-08.md", "2026-08-09.md"]
