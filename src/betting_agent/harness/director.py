"""The daily director session (docs/22 section 8.2).

One session a day, between the last slot of one day and the first of the next. It reads
yesterday's cohort, the cohorts that have settled since it last ran, the status digest and
its own previous pages, and it writes three files: a ranking of the attempts with a
paragraph each, a one-page standing direction, and the two sets of past attempts the
cells will read.

What it directs is attention, never throughput. The page may say which families and
theses to look at and which to leave alone; it may not say how many bets to place, how
large, or whether to pass. Those are fixed above the director, the brief says so, and
:func:`validate_outputs` rejects a page that says otherwise.

Four parts, in the order the run uses them:

* :func:`build_workspace` lays out ``data/director/<run_date>/``, which is everything the
  session can see.
* :func:`_launch_director` runs the session inside it.
* :func:`validate_outputs` reads the three files back and returns the first reason they
  cannot be used, if there is one.
* :func:`store` writes a valid run into ``director_runs``, ``attempt_reviews`` and
  ``cohort_reviews``.

Nothing downstream blocks on a run that fails or is rejected: every consumer of the
direction reads the latest *valid* run (``history.latest_valid_run``), so a bad night
means the day runs on yesterday's page. Nothing is retried the same day.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from betting_agent.harness.attempt import (
    _render_task,
    close_session_row,
    open_session_row,
)
from betting_agent.harness.digest import status_digest
from betting_agent.ledger import history
from betting_agent.moneymath import D
from betting_agent.sessions import SessionSpec, run_session
from betting_agent.timeutil import et_day, iso, utc_now

PROSPECTIVE = "prospective"
RETROSPECTIVE = "retrospective"

# The bootstrap prompt of every session in the system (docs/22 section 11). ``TASK.md``
# sits one level above the session's own directory, exactly as it does for an attempt.
_BOOTSTRAP = "Read ../TASK.md and carry it out completely."

# Read, Grep and Glob to study the workspace; Bash for `bt past`. Write and Edit are a
# deliberate departure from spec 8.2's four tools: the run's whole product is three files,
# and without them every one of them depends on getting a shell heredoc right.
_TOOLS = "Read,Grep,Glob,Bash,Write,Edit"

_PAGES_KEPT = 7
_PARAGRAPH_MIN, _PARAGRAPH_MAX = 200, 1500
_DIRECTION_MIN, _DIRECTION_MAX = 200, 1200
_SET_MIN, _SET_MAX = 6, 12
_PAGE_HEADINGS = ("## Standing direction", "## Today", "## Watching")
# How many completed cohorts one run is asked to review retrospectively. Without a bound
# the first run of the new loop would be handed every complete cohort in the ledger and
# asked to rank them all inside a 45-minute envelope, which it cannot do: it would fail
# validation, and the system would never get a first valid page.
_SETTLED_MAX = 3

_ERROR_MAX = 500  # director_runs.error is a breadcrumb, not a transcript
_LINE_MAX = 120   # how much of an offending page line a rejection reason quotes

# An attempt whose session has not ended yet: left out of the prospective review and
# reviewed retrospectively with its cohort (docs/22 section 5.1).
_UNFINISHED = ("created", "running")
# A leg the exchange gave us a position on, whatever happened to it since. Only these
# contracts are in the denominator of net per contract: a refused leg declared a size it
# never held.
_FILLED = ("filled", "settled", "voided")

# A count an instruction would use: a numeral, or one to ten spelled out.
_COUNT = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"
_UNIT = r"(?:bets?|contracts?|positions?|markets?)"

# Throughput instructions the standing direction may not carry (docs/22 section 8.4).
# Matched case-insensitively, one line at a time, and only against the standing direction:
# the other two sections describe what happened and what to watch, and a description
# ("the cohort placed 11 contracts") is not an instruction. A small list that can grow.
_THROUGHPUT_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    # An imperative verb and, later in the line, a count of something bettable.
    rf"\b(?:place|make|take|put on|bet|limit)\b[^.]*\b{_COUNT}\b[^.]*\b{_UNIT}\b",
    # A refusal, where it opens a sentence or follows "should", so that "whether the
    # ladder will pass today's convoy" stays a sentence about the world.
    r"(?:^|[.;:]\s+|\bshould\s+)"
    r"(?:no\s+bets|zero\s+bets|do\s+not\s+bet|do\s+not\s+place|skip\s+today|pass\s+today)\b",
    # A bound on the count, with or without a verb in front of it.
    rf"\b(?:at most|no more than|exactly)\s+{_COUNT}\s+{_UNIT}\b",
    # A rate per period.
    rf"\b{_COUNT}\s+bets?\s+per\s+(?:day|slot|attempt)\b",
    # A bare count and unit opening the line, which reads as an instruction and nothing
    # else ("3 contracts on every leg").
    rf"^\s*{_COUNT}\s+{_UNIT}\b",
))

# The top-level sections of ``history.render_record(full=True)``, and the file each one
# becomes in the workspace. The record is the single renderer (docs/22 section 8.5), so
# what the director reads here is byte for byte what ``bt past attempt`` prints.
_RECORD_FILES = (
    ("edge_claim.md", "edge_claim.md"),
    ("hypothesis.md", "hypothesis.md"),
    ("MANIFEST.md", "MANIFEST.md"),
    ("Legs", "legs.md"),
    ("Activity", "activity.md"),
    ("Session summary", "summary.md"),
)
# The record's three ticket sections, whose bodies are free text the model wrote, and the
# five sections after them, which the renderer always writes in this order. The tail is
# located from the end backwards and the ticket headings only in front of it, so a claim
# that happens to contain a line reading ``## Legs`` cannot truncate ``edge_claim.md``.
_TICKET_HEADINGS = ("edge_claim.md", "hypothesis.md", "MANIFEST.md")
_TAIL_HEADINGS = ("Legs", "Outcome", "Activity", "Session summary", "Review")


@dataclass(frozen=True)
class Workspace:
    """Where the session runs and what was put in front of it."""

    run_date: str
    cohort_date: str
    run_dir: Path          # data/director/<run_date>, holding TASK.md
    work_dir: Path         # its workspace/, the session's cwd and where it writes
    cohort: list[str]      # yesterday's attempts, still-running ones left out
    settled: dict[str, list[str]]   # completed cohort date -> its attempts


@dataclass(frozen=True)
class Outputs:
    """The three files the session wrote, once they have passed validation."""

    review: dict
    page: str
    sets: dict


def run_id_for(run_date: str) -> str:
    return f"D-{run_date}"


def cohort_date_for(run_date: str) -> str:
    """The cohort a run reviews prospectively: the Eastern day before it."""
    return (date.fromisoformat(run_date) - timedelta(days=1)).isoformat()


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- the cohort
def prospective_cohort(ledger, cohort_date: str) -> list[str]:
    """Yesterday's cohort as the review sees it (docs/22 sections 5.1 and 8.1).

    Every attempt whose slot fell on that Eastern day, minus any whose session is still
    running when the workspace is built. A run that waited the configured hour and went
    ahead anyway leaves the straggler out here; it is reviewed retrospectively with the
    rest of its cohort once the cohort completes.
    """
    out = []
    for attempt_id in history.cohort(ledger, cohort_date):
        row = ledger.get_attempt(attempt_id)
        if row is not None and row["status"] not in _UNFINISHED:
            out.append(attempt_id)
    return out


def cohort_still_running(ledger, cohort_date: str) -> str | None:
    """The first attempt of a cohort whose session has not ended, if any.

    Cohort membership is the slot's day, and an attempt run by hand has no slot key at
    all, so this asks the cohort rather than scanning slot markers: an attempt the review
    would include is an attempt the review should wait for.
    """
    for attempt_id in history.cohort(ledger, cohort_date):
        row = ledger.get_attempt(attempt_id)
        if row is not None and row["status"] in _UNFINISHED:
            return attempt_id
    return None


def settled_cohorts(ledger, settings, *, before: str) -> dict[str, list[str]]:
    """The cohorts this run reviews retrospectively, oldest day first.

    Four conditions and a bound. A cohort qualifies when every one of its attempts belongs
    to the current era, when it is complete (docs/22 section 8.1: every real leg settled or
    voided, every refused or unfilled leg scored hypothetically), and when it has no
    retrospective review yet. Old-era cohorts belong to the loop this one replaced and are
    never reviewed, which is also what keeps the first run of the new loop from being handed
    two months of history.

    ``before`` excludes yesterday's cohort, whatever state it is in. A cohort of passes is
    complete the moment its last session ends, and reviewing it retrospectively in the same
    run that ranks it prospectively would put the outcomes in the workspace the prospective
    ranking is supposed to be written without.

    At most :data:`_SETTLED_MAX` of them, the most recent first into the set and oldest day
    first out of it. A run has forty-five minutes; a cohort it cannot read is a cohort it
    cannot rank.
    """
    reviewed = ledger.reviewed_cohorts(RETROSPECTIVE)
    era = settings.history.current_era
    days: list[str] = []
    members_by_day: dict[str, list[str]] = {}
    for day in history.cohort_days(ledger):
        if day >= before or day in reviewed:
            continue
        members = history.cohort(ledger, day)
        if not members:
            continue
        if any(ledger.get_attempt(a)["era"] != era for a in members):
            continue
        if all(history.is_complete(ledger, a) for a in members):
            days.append(day)
            members_by_day[day] = members
    return {day: members_by_day[day] for day in sorted(days[-_SETTLED_MAX:])}


# --------------------------------------------------------------------------- the workspace
def _last_index(lines: list[str], heading: str, before: int) -> int | None:
    for index in range(before - 1, -1, -1):
        if lines[index].strip() == heading:
            return index
    return None


def _first_index(lines: list[str], heading: str, start: int, end: int) -> int | None:
    for index in range(start, end):
        if lines[index].strip() == heading:
            return index
    return None


def _sections(full_record: str) -> dict[str, str]:
    """The full record split at its own headings, body keyed by heading.

    The ticket sections hold free text a model wrote, so a line in a claim can read
    exactly like one of the record's own headings. The five sections after the ticket are
    always written, always in order, so they are found from the end of the document
    backwards; the ticket headings are then looked for only in front of them, in order.
    Nothing a ticket says can move either boundary.
    """
    lines = full_record.splitlines()
    marks: list[tuple[str, int]] = []
    end = len(lines)
    for name in reversed(_TAIL_HEADINGS):
        index = _last_index(lines, f"## {name}", end)
        if index is None:
            continue
        marks.append((name, index))
        end = index
    marks.reverse()
    start = 0
    ticket: list[tuple[str, int]] = []
    for name in _TICKET_HEADINGS:
        index = _first_index(lines, f"## {name}", start, end)
        if index is None:
            continue
        ticket.append((name, index))
        start = index + 1
    marks = ticket + marks
    out: dict[str, str] = {}
    for position, (name, index) in enumerate(marks):
        stop = marks[position + 1][1] if position + 1 < len(marks) else len(lines)
        out[name] = "\n".join(lines[index + 1:stop]).strip()
    return out


def _amount(value) -> str:
    return "none" if value is None else str(value)


def _outcomes_md(ledger, attempt_id: str) -> str:
    """``outcomes.md``: profit and fee per leg, then the attempt's and each family's net."""
    record = history.outcome_record(ledger, attempt_id)
    out = [f"# Outcome {attempt_id}", ""]
    for leg in record["legs"]:
        out.append(
            f"{leg['ticker']} {leg['side']} @{leg['price']} x{_amount(leg['contracts'])} "
            f"· {leg['status']} · {leg['outcome'] or 'no outcome'} "
            f"· profit {_amount(leg['profit'])} · fee {_amount(leg['fee'])}"
        )
    if not record["legs"]:
        out.append("no legs")
    totals = record["totals"]
    out += ["", f"Net {totals['net']} on {totals['stake']} staked over {totals['legs']} "
                f"legs, {totals['wins']} won"]
    for family in record["families"]:
        out.append(
            f"{family['family']}: net {family['net']} on {family['stake']} staked, "
            f"{family['wins']} of {family['legs']} legs won"
        )
    return "\n".join(out) + "\n"


def _write_attempt(ledger, dest: Path, attempt_id: str, *, with_outcomes: bool) -> None:
    """One attempt's folder: its ticket, legs, activity and closing summary.

    The files are the sections of the one record renderer rather than a second rendering
    of the same rows, so a change to what ``bt past attempt`` shows is a change to what
    the director sees. A session that failed before writing a ticket simply has no ticket
    files, which is what "a member with an empty record" looks like on disk.
    """
    dest.mkdir(parents=True, exist_ok=True)
    sections = _sections(history.render_record(ledger, attempt_id, full=True))
    for heading, filename in _RECORD_FILES:
        body = sections.get(heading)
        if body is not None:
            (dest / filename).write_text(body + "\n", encoding="utf-8")
    if with_outcomes:
        (dest / "outcomes.md").write_text(_outcomes_md(ledger, attempt_id), encoding="utf-8")


def _write_pages(ledger, pages_dir: Path) -> None:
    """The last seven valid pages, newest first, each named by its run date."""
    rows = ledger.conn.execute(
        "SELECT run_date, page_md FROM director_runs WHERE status='valid' "
        "AND page_md IS NOT NULL ORDER BY run_date DESC, started_at DESC LIMIT ?",
        (_PAGES_KEPT,),
    ).fetchall()
    for row in rows:
        (pages_dir / f"{row['run_date']}.md").write_text(row["page_md"], encoding="utf-8")


def task_md(run_date: str, cohort_date: str, *, attempts_per_day: int) -> str:
    """``prompts/director.md`` with its three placeholders filled (docs/22 appendix C).

    ``attempts_per_day`` is the number of slots the schedule runs. The brief used to say
    "nine" in two places, which was the count docs/22 was written against; the schedule has
    fifteen slots now, and a brief that misstates the throughput it is told not to steer is
    a brief the director has reason to distrust.
    """
    template = (files("betting_agent") / "prompts" / "director.md").read_bytes()
    return _render_task(template.decode("utf-8"),
                        {"run_date": run_date, "cohort_date": cohort_date,
                         "attempts_per_day": str(attempts_per_day)})


def build_workspace(ledger, settings, *, run_date: str, client=None, now=None) -> Workspace:
    """Lay out ``data/director/<run_date>/`` (docs/22 section 8.2).

    ``TASK.md`` sits in the run directory and everything the session reads and writes sits
    in ``workspace/`` below it, which is the session's own directory. That is the attempt's
    layout exactly, and it is what makes the shared bootstrap prompt's ``../TASK.md`` land
    in both places.
    """
    now = now or utc_now()
    cohort_date = cohort_date_for(run_date)
    run_dir = settings.director_dir / run_date
    work_dir = run_dir / "workspace"
    for path in (work_dir / "cohort", work_dir / "settled", work_dir / "pages"):
        path.mkdir(parents=True, exist_ok=True)

    # The digest is about the day being reviewed, which is the day that has just finished,
    # the same day the tick's own digest step files.
    (work_dir / "digest.md").write_text(
        status_digest(ledger, settings, client, day=cohort_date, now=now), encoding="utf-8"
    )
    members = prospective_cohort(ledger, cohort_date)
    for attempt_id in members:
        _write_attempt(ledger, work_dir / "cohort" / attempt_id, attempt_id,
                       with_outcomes=False)
    settled = settled_cohorts(ledger, settings, before=cohort_date)
    for day, ids in settled.items():
        for attempt_id in ids:
            _write_attempt(ledger, work_dir / "settled" / day / attempt_id, attempt_id,
                           with_outcomes=True)
    _write_pages(ledger, work_dir / "pages")
    (run_dir / "TASK.md").write_text(
        task_md(run_date, cohort_date, attempts_per_day=len(settings.schedule.slots)),
        encoding="utf-8",
    )
    return Workspace(run_date=run_date, cohort_date=cohort_date, run_dir=run_dir,
                     work_dir=work_dir, cohort=members, settled=settled)


# --------------------------------------------------------------------------- the session
def _launch_director(ledger, settings, *, session_id: str, workspace: Workspace, model: str):
    """Run the session and close its ``sessions`` row.

    The row is opened by the caller rather than here, because ``director_runs.session_id``
    references it and the run row has to exist before a paid session starts.

    ``model`` arrives already resolved through the substitution switch, so that the
    ``sessions`` row and the ``director_runs`` row name the same model, and both name the
    one that actually ran.
    """
    logs_dir = workspace.run_dir / "logs"
    spec = SessionSpec(
        kind="director",
        cwd=workspace.work_dir,
        prompt=_BOOTSTRAP,
        model=model,
        # Arno's call of 2026-09-16: the director runs at its own effort, higher than
        # the attempts, to be reduced only if the usage limits bite.
        effort=settings.director.effort,
        max_turns=settings.director.max_turns,
        wall_time_s=settings.director.wall_time_min * 60,
        session_id=session_id,
        add_dirs=[workspace.run_dir],
        max_budget_usd=settings.director.max_budget_usd,
        tools=_TOOLS,
        extra_env={"BT_PAST": "on", "BT_ROOT": str(settings.root)},
        # The same rule as an attempt: a user-global skill must not leak into a session
        # this harness is paying for.
        disallowed_tools="Skill",
    )
    result = run_session(
        spec,
        runner_cmd=settings.attempt.runner,
        stream_path=logs_dir / "session.stream.jsonl",
        err_path=logs_dir / "session.err",
    )
    close_session_row(ledger, session_id, result, ended_at=iso(utc_now()))
    return result


# --------------------------------------------------------------------------- validation
def _load_json(path: Path, name: str) -> tuple[object, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError):
        return None, f"{name} is missing or does not parse"


def _check_ranking(entry: dict, *, label: str, ids: list[str], cohort_date: str) -> str | None:
    """The ranking and paragraph rules, shared by both kinds (docs/22 section 8.2)."""
    ranking = entry.get("ranking")
    if not isinstance(ranking, list) or any(not isinstance(a, str) for a in ranking):
        return f"{label} ranking is not a list of attempt ids"
    wanted = set(ids)
    seen: set[str] = set()
    for attempt_id in ranking:
        if attempt_id in seen:
            return f"{label} ranking names {attempt_id} more than once"
        if attempt_id not in wanted:
            return (f"{label} ranking names {attempt_id}, which is not in the "
                    f"{cohort_date} cohort")
        seen.add(attempt_id)
    missing = [a for a in ids if a not in seen]
    if missing:
        return f"{label} ranking is missing {missing[0]}"
    paragraphs = entry.get("paragraphs")
    if not isinstance(paragraphs, dict):
        return f"{label} has no paragraphs object"
    for attempt_id in ids:
        text = paragraphs.get(attempt_id)
        if not isinstance(text, str):
            return f"{label} has no paragraph for {attempt_id}"
        if not _PARAGRAPH_MIN <= len(text) <= _PARAGRAPH_MAX:
            return (f"{label} paragraph for {attempt_id} is {len(text)} characters, "
                    f"outside {_PARAGRAPH_MIN} to {_PARAGRAPH_MAX}")
    return None


def _check_review(review, *, cohort_date: str, cohort_ids: list[str],
                  settled: dict[str, list[str]]) -> str | None:
    if not isinstance(review, dict):
        return "review.json is not a JSON object"
    prospective = review.get(PROSPECTIVE)
    if not isinstance(prospective, dict):
        return "review.json has no prospective object"
    named = str(prospective.get("cohort") or "")
    if named != cohort_date:
        return f"prospective names cohort {named or 'nothing'}, not {cohort_date}"
    reason = _check_ranking(prospective, label=PROSPECTIVE, ids=cohort_ids,
                            cohort_date=cohort_date)
    if reason:
        return reason
    entries = review.get(RETROSPECTIVE) or []
    if not isinstance(entries, list):
        return "review.json retrospective is not a list of cohort entries"
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return "a retrospective entry is not a JSON object"
        day = str(entry.get("cohort") or "")
        if day not in settled:
            return (f"retrospective names cohort {day or 'nothing'}, which has no folder "
                    "in settled/")
        seen.add(day)
        reason = _check_ranking(entry, label=f"retrospective {day}", ids=settled[day],
                                cohort_date=day)
        if reason:
            return reason
    for day in settled:
        if day not in seen:
            return f"review.json has no retrospective entry for cohort {day}"
    return None


def _check_page(page: str, settings) -> str | None:
    limit = settings.director.page_max_chars
    if len(page) > limit:
        return f"page.md is {len(page)} characters, over the {limit} limit"
    headings = [line.strip() for line in page.splitlines() if line.strip().startswith("## ")]
    if headings != list(_PAGE_HEADINGS):
        return "page.md must carry exactly the headings " + ", ".join(_PAGE_HEADINGS)
    # Only the standing direction: it is the part that tells the next attempts what to do,
    # and it is the only part where a count of bets or contracts can be an instruction.
    # "Today" reports what the cohort did and "Watching" says what would change the
    # director's mind, and both of those legitimately count things.
    direction = history._section(page, "Standing direction")
    for line in direction.splitlines():
        if any(pattern.search(line) for pattern in _THROUGHPUT_PATTERNS):
            return f"page.md sets throughput: {line.strip()[:_LINE_MAX]}"
    return None


def _check_sets(ledger, sets) -> str | None:
    if not isinstance(sets, dict):
        return "sets.json is not a JSON object"
    for name in ("balanced", "focused"):
        ids = sets.get(name)
        if not isinstance(ids, list) or any(not isinstance(a, str) for a in ids):
            return f"{name} is not a list of attempt ids"
        if not _SET_MIN <= len(ids) <= _SET_MAX:
            return f"{name} holds {len(ids)} ids, outside {_SET_MIN} to {_SET_MAX}"
        seen: set[str] = set()
        for attempt_id in ids:
            # A set is ten attempts to read, so a repeat is nine attempts and a mistake.
            if attempt_id in seen:
                return f"{name} names {attempt_id} more than once"
            seen.add(attempt_id)
            if ledger.get_attempt(attempt_id) is None:
                return f"{name} names {attempt_id}, which is not an attempt"
    for name in ("balanced_note", "focused_lens"):
        if not str(sets.get(name) or "").strip():
            return f"{name} is empty"
    direction = sets.get("focused_direction")
    length = len(direction) if isinstance(direction, str) else 0
    if not _DIRECTION_MIN <= length <= _DIRECTION_MAX:
        return (f"focused_direction is {length} characters, outside {_DIRECTION_MIN} to "
                f"{_DIRECTION_MAX}")
    return None


def validate_outputs(ledger, settings, workspace: Workspace) -> tuple[Outputs | None, str | None]:
    """The three files, or the first reason they cannot be used (docs/22 section 8.2).

    One reason, not a list: a run is rejected whole, the reason lands in
    ``director_runs.error`` and in the ``director_invalid`` audit, and the day carries on
    with the last page that validated.
    """
    review, reason = _load_json(workspace.work_dir / "review.json", "review.json")
    if reason:
        return None, reason
    try:
        page = (workspace.work_dir / "page.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Bytes that are not UTF-8 are a rejected run, not an exception that leaves the
        # run open forever. The two JSON loads get this for free: a decode error is a
        # ValueError.
        return None, "page.md is missing or does not parse"
    sets, reason = _load_json(workspace.work_dir / "sets.json", "sets.json")
    if reason:
        return None, reason
    reason = (
        _check_review(review, cohort_date=workspace.cohort_date,
                      cohort_ids=workspace.cohort, settled=workspace.settled)
        or _check_page(page, settings)
        or _check_sets(ledger, sets)
    )
    if reason:
        return None, reason
    return Outputs(review=review, page=page, sets=sets), None


# --------------------------------------------------------------------------- storage
def realized_order(ledger, attempt_ids: list[str]) -> list[str]:
    """The cohort ordered by realized net, ties broken by net per contract (section 8.3).

    Computed here rather than asked of the director: it is the pure-profit ordering the
    retrospective ranking is meant to be compared against, so it must come from the bet
    rows and nowhere else.
    """
    def key(attempt_id: str) -> tuple[Decimal, Decimal, str]:
        record = history.outcome_record(ledger, attempt_id)
        net = D(str(record["totals"]["net"]))
        # Filled contracts only. A refused leg declares the size it wanted and holds
        # nothing, so counting it would divide an attempt's profit by contracts it never
        # had and drop it below an identical attempt that proposed less.
        contracts = sum(
            (D(str(leg["contracts"])) for leg in record["legs"]
             if leg["contracts"] is not None and leg["status"] in _FILLED), D("0")
        )
        per_contract = net / contracts if contracts else net
        return (-net, -per_contract, attempt_id)

    return sorted(attempt_ids, key=key)


def _review_rows(kind: str, cohort_date: str, entry: dict) -> list[dict]:
    ranking = list(entry["ranking"])
    return [
        {"attempt_id": attempt_id, "kind": kind, "cohort_date": cohort_date, "rank": rank,
         "cohort_size": len(ranking), "paragraph": entry["paragraphs"][attempt_id]}
        for rank, attempt_id in enumerate(ranking, start=1)
    ]


def store(ledger, *, run_id: str, outputs: Outputs, ended_at: str) -> None:
    """Write a valid run: the page, the sets, both rankings and every paragraph.

    Everything goes down in one transaction, the rows before the status (see
    ``Ledger.store_director_review``), so a run that is ``valid`` is a run whose review can
    be read.
    """
    prospective = outputs.review[PROSPECTIVE]
    attempt_rows = _review_rows(PROSPECTIVE, prospective["cohort"], prospective)
    cohort_rows = [{
        "cohort_date": prospective["cohort"], "kind": PROSPECTIVE,
        "ranking": json.dumps(list(prospective["ranking"])), "realized": None,
    }]
    for entry in outputs.review.get(RETROSPECTIVE) or []:
        ranking = list(entry["ranking"])
        attempt_rows += _review_rows(RETROSPECTIVE, entry["cohort"], entry)
        cohort_rows.append({
            "cohort_date": entry["cohort"], "kind": RETROSPECTIVE,
            "ranking": json.dumps(ranking),
            "realized": json.dumps(realized_order(ledger, ranking)),
        })
    ledger.store_director_review(
        run_id, ended_at=ended_at, page_md=outputs.page, page_hash=_sha12(outputs.page),
        sets_json=json.dumps(outputs.sets, sort_keys=True), attempt_reviews=attempt_rows,
        cohort_reviews=cohort_rows,
    )


# --------------------------------------------------------------------------- public
def run_director(ledger, settings, *, run_date: str | None = None, client=None,
                 now=None) -> dict:
    """Build, run, validate and store one director run; returns its id and status.

    The ``director_runs`` row opens at ``running`` before the workspace is built, and
    everything after it runs inside one wrapper: whatever fails, from an unreadable ledger
    during the build to a ledger error while the review is being written, the day's run
    ends ``failed`` with the error on the row. The alternative, which this replaces, is a
    day with no row at all, and a tick that spawns a fresh director every fifteen minutes
    until midnight.

    A session that breached its envelope or errored is ``failed``; a session that ran and
    wrote files the harness cannot use is ``invalid``, with the reason on the row and in a
    ``director_invalid`` audit.
    """
    now = now or utc_now()
    run_date = run_date or et_day(now)
    run_id = run_id_for(run_date)
    model = settings.effective_model(settings.director.model, now)

    ledger.insert_director_run(
        run_id, run_date=run_date, cohort_date=cohort_date_for(run_date),
        started_at=iso(now), model=model,
    )
    try:
        workspace = build_workspace(ledger, settings, run_date=run_date, client=client,
                                    now=now)
        # The session row opens at the moment the session does, and the run is pointed at
        # it once it exists: ``director_runs.session_id`` references ``sessions``, so it
        # cannot be written any earlier than this.
        session_id = str(uuid4())
        open_session_row(ledger, session_id=session_id, kind="director", model=model,
                         started_at=iso(utc_now()))
        ledger.set_director_session(run_id, session_id)
        result = _launch_director(ledger, settings, session_id=session_id,
                                  workspace=workspace, model=model)

        if result.exit_kind != "ok":
            return _fail(ledger, run_id, f"session exited {result.exit_kind}")

        outputs, reason = validate_outputs(ledger, settings, workspace)
        if reason is not None:
            ledger.finish_director_run(run_id, status="invalid", ended_at=iso(utc_now()),
                                       error=reason[:_ERROR_MAX])
            ledger.audit("director_invalid", detail={"run_id": run_id, "reason": reason})
            return {"run_id": run_id, "status": "invalid", "error": reason}

        store(ledger, run_id=run_id, outputs=outputs, ended_at=iso(utc_now()))
    except (Exception, KeyboardInterrupt) as exc:
        # The row is already open: a crash must leave a finished run behind rather than a
        # row that says the director is still working. The exception still propagates, so
        # the command exits nonzero and leaves its traceback in the spawn log.
        _fail(ledger, run_id, f"{type(exc).__name__}: {exc}")
        raise
    return {"run_id": run_id, "status": "valid", "error": None}


def _fail(ledger, run_id: str, error: str) -> dict:
    ledger.finish_director_run(run_id, status="failed", ended_at=iso(utc_now()),
                               error=error[:_ERROR_MAX])
    return {"run_id": run_id, "status": "failed", "error": error}
