#!/usr/bin/env python3
"""Spec §18 fake agent — the fake-agent E2E runner used as ``attempt.runner``.

Invoked exactly like ``claude -p`` (as argv[0]); it never calls an LLM. It is a thin
wrapper over ``tests/helpers/stub_runner.py`` (the Claude-CLI stream double): rather than
re-implement the stream protocol it translates ``FAKE_*`` env into the ``STUB_*`` env the
stub already understands and executes the stub as ``__main__``. Behaviour is driven by env:

  FAKE_FIXTURE=<name>  copy ``tests/fixtures/tickets/<name>/`` into ``../ticket/`` then
                       emit a valid ``ok`` stream (stub STUB_BEHAVIOR=ok + STUB_WRITE_TICKET).
  FAKE_FIXTURE=crash   emit only an init line, then exit 1 — no ticket, no result line
                       (a hard crash; the harness records ``failed``).
  FAKE_FIXTURE=auth_error
                       emit the OAuth wedge's stream (stub STUB_BEHAVIOR=auth_error) for
                       EVERY session kind, because an outage does not care what the
                       session was for (docs/14 D2).
  FAKE_FIXTURE=slow    write the ``valid_basic`` ticket, then sleep past the harness wall
                       clock (stub STUB_BEHAVIOR=hang) — proving the §9.4 contract rule
                       (a killed session whose ticket is finished still counts).
  FAKE_FIXTURE=director
                       write the three director files into the workspace this session was
                       handed, then emit a valid ``ok`` stream (docs/22 section 8.2).

Only stdlib is used, so any ``python3`` on PATH runs it.
"""
from __future__ import annotations

import datetime
import json
import os
import runpy
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_STUB = _HERE / "helpers" / "stub_runner.py"
_FIXTURES = _HERE / "fixtures" / "tickets"


def _argv_value(flag: str) -> str | None:
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return None


def _run_stub() -> None:
    """Execute stub_runner as ``__main__``; it reads the STUB_* env just set and exits."""
    runpy.run_path(str(_STUB), run_name="__main__")


def _emit_crash() -> None:
    """A hard crash: one init line, then exit 1 — no result line, no ticket written."""
    init = {
        "type": "system", "subtype": "init",
        "session_id": _argv_value("--session-id") or "fake-session",
        "cwd": os.getcwd(),
        "model": _argv_value("--model") or "fake-model",
        "tools": ["Read", "Bash"], "permissionMode": "bypassPermissions",
    }
    sys.stdout.write(json.dumps(init) + "\n")
    sys.stdout.flush()
    sys.exit(1)


_PARAGRAPH = (
    "{aid} worked one family and said why in the ticket. The claim named the market, the "
    "fee was priced before the limit was set, and the book was read at the size the leg "
    "asked for. What a later attempt should take from it is the habit of stating the kill "
    "criteria first and sizing to the book rather than to the idea. What it should not "
    "repeat is treating a cheap entry as evidence that the family stays cheap."
)
_PAGE = """## Standing direction
Keep sweeping the strait ladders and leave the weather families alone. The cohorts that
have settled so far say the ladders reprice slowly and the weather markets do not.

## Today
The cohort read the same board and split on the same family, which is what a shared
direction is supposed to produce.

## Watching
Whether the closure ladder reprices after the convoy, and whether the passes were right.
"""
_FOCUSED = (
    "Read these entries for how a claim is turned into a leg: each one names the market, "
    "the reason the price is wrong, and the fact that would end the position. Work the "
    "same family they worked, and if the price has moved, say so in the ticket rather "
    "than restating the old claim as if nothing had changed since it was written."
)


def _director_outputs() -> None:
    """Write ``review.json``, ``page.md`` and ``sets.json`` into the director's workspace.

    It reads the workspace it was handed rather than being told what to say, so it ranks
    whatever cohort the harness actually built: the cwd holds ``cohort/<attempt id>/`` and
    ``settled/<cohort date>/<attempt id>/``, and the run date is the directory the
    workspace sits in.
    """
    cwd = Path.cwd()
    run_date = datetime.date.fromisoformat(cwd.parent.name)
    cohort_date = (run_date - datetime.timedelta(days=1)).isoformat()

    def ids(path: Path) -> list[str]:
        return sorted(p.name for p in path.iterdir() if p.is_dir()) if path.is_dir() else []

    def entry(day: str, members: list[str]) -> dict:
        return {"cohort": day, "ranking": members,
                "paragraphs": {a: _PARAGRAPH.format(aid=a) for a in members}}

    cohort = ids(cwd / "cohort")
    settled = {day: ids(cwd / "settled" / day) for day in ids(cwd / "settled")}
    review = {
        "prospective": entry(cohort_date, cohort),
        "retrospective": [entry(day, members) for day, members in sorted(settled.items())],
    }
    pool = sorted({a for members in settled.values() for a in members} | set(cohort))
    while 0 < len(pool) < 6:
        pool = pool + pool
    (cwd / "review.json").write_text(json.dumps(review, indent=2))
    (cwd / "page.md").write_text(_PAGE)
    (cwd / "sets.json").write_text(json.dumps({
        "balanced": pool[:10],
        "balanced_note": "A spread of families, outcomes and passes.",
        "focused": pool[:10],
        "focused_lens": "the attempts whose reasoning was most precise",
        "focused_direction": _FOCUSED,
    }, indent=2))


def main() -> None:
    # An auth outage takes down every session kind exactly as it takes down attempts,
    # and that is the whole point of the fixture (docs/14 D2).
    if os.environ.get("FAKE_FIXTURE") == "auth_error":
        os.environ["STUB_BEHAVIOR"] = "auth_error"
        os.environ.pop("STUB_WRITE_TICKET", None)
        _run_stub()
        return

    fixture = os.environ.get("FAKE_FIXTURE", "valid_basic")
    if fixture == "crash":
        _emit_crash()
        return
    if fixture == "director":
        _director_outputs()
        os.environ["STUB_BEHAVIOR"] = "ok"
        os.environ.pop("STUB_WRITE_TICKET", None)
        _run_stub()
        return
    if fixture == "slow":
        os.environ["STUB_BEHAVIOR"] = "hang"
        os.environ["STUB_WRITE_TICKET"] = str(_FIXTURES / "valid_basic")
        _run_stub()
        return

    os.environ["STUB_BEHAVIOR"] = "ok"
    os.environ["STUB_WRITE_TICKET"] = str(_FIXTURES / fixture)
    _run_stub()


if __name__ == "__main__":
    main()
