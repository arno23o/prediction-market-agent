"""The day's cell plan and what each cell shows an attempt (docs/22 sections 8.5 and 8.7).

Four cells replace seventeen experimental arms. They differ in one thing only: what an
attempt reads before it looks at a market.

* **baseline** sees nothing. No ``CONTEXT.md``, no principles page, no ``bt past``. It is
  the control the other three are measured against.
* **static** sees the most recently completed attempts, chosen by code.
* **director** sees the day's direction and the balanced set the director picked.
* **focused** sees the focused direction and the set behind one stated lens.

Which slot runs which cell is a stored daily permutation, not a schedule: the counts in
``[cells]``, which total the day's slot count, are shuffled once a day under a stored seed,
so no cell is ever welded to a time of day and every cell runs its configured number of
times every day. The seed is stored beside the plan, so the draw can be reproduced and the
day's plan can be printed before any of it has run.

The same draw gives each slot an arm beside its cell, a model and an effort (2026-09-26):
``attempt.fable_per_day`` slots run ``attempt.fable_model`` at ``attempt.fable_effort``,
and every other slot runs ``attempt.model`` at one of ``attempt.efforts``. The arms are
independent of the cells, so an arm is welded neither to a cell nor to a time of day. A
stored entry is ``{"cell", "model", "effort"}``; a plan stored before arms existed holds
bare cell names, and those days read as ``attempt.model`` at ``attempt.effort``.

Nothing here writes a market, a bet or an attempt row. It reads history, renders text, and
stores one plan row a day.
"""

from __future__ import annotations

import hashlib
import json
import random

from betting_agent.ledger.history import (
    latest_valid_run,
    recent_completed,
    render_record,
)

# The four cells, in the order the plan's multiset is built. ``CELLS`` is what the CLI's
# ``--cell`` option and every test iterate over.
CELLS = ("baseline", "static", "director", "focused")

# The ``{memory_section}`` placeholder, by the cell that was actually rendered (docs/22
# section 8.5). Verbatim: these three sentences are the whole difference between the cells
# as far as the prompt is concerned. ``{n}`` is how many past attempts the static cell
# holds, which is ``cells.static_recent`` and not a constant, so the sentence and the
# CONTEXT.md heading cannot drift from the number of records actually rendered.
_MEMORY_SECTION = {
    "baseline": (
        "This attempt runs without access to past attempts or guidance. Work from the "
        "markets alone."
    ),
    "static": (
        "Study ../CONTEXT.md, which holds the {n} most recent attempts, before choosing a "
        "target. `bt past` searches the whole history."
    ),
    "director": (
        "Study ../CONTEXT.md first: it carries the day's direction and the past attempts "
        "chosen for you. `bt past` searches the whole history."
    ),
}
_MEMORY_SECTION["focused"] = _MEMORY_SECTION["director"]


def _count_word(n: int) -> str:
    """``ten`` at the configured default, the numeral at any other value.

    The spec writes both the heading and the memory sentence with the word "ten", so the
    default renders them exactly as written; a changed setting reads as a numeral rather
    than as a lie about the number of records below it.
    """
    return "ten" if n == 10 else str(n)


def _memory_section(settings, cell: str) -> str:
    return _MEMORY_SECTION[cell].replace("{n}", _count_word(settings.cells.static_recent))


def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- the plan
def _efforts(settings, rng: random.Random, n: int) -> list[str]:
    """``n`` levels from ``attempt.efforts``, split as evenly as ``n`` allows, in random order.

    The levels are shuffled before they are dealt, so when the split cannot be even the
    draw, not the order of the list, decides which level gets the extra slot. An empty
    list deals ``attempt.effort`` to every slot.
    """
    levels = list(settings.attempt.efforts) or [settings.attempt.effort]
    rng.shuffle(levels)
    dealt = [levels[i % len(levels)] for i in range(n)]
    rng.shuffle(dealt)
    return dealt


def draw(settings, seed: int) -> list[dict]:
    """The plan a given seed produces: the configured multiset, shuffled, then the arms.

    A pure function of the seed and the configuration, which is what makes the stored seed
    worth storing: the plan in the ledger can be checked against the draw it claims to be.
    The cells are shuffled first, so a seed yields the same cells it did before arms
    existed.

    The counts are meant to total the day's slot count exactly, and ``config_warnings``
    says so when they do not. The slice below is a guard for that misconfiguration only: it
    keeps a plan from naming more cells than the day has slots.
    """
    rng = random.Random(seed)
    pool: list[str] = []
    for name, count in settings.cells.counts().items():
        pool += [name] * max(int(count), 0)
    rng.shuffle(pool)
    day = pool[:len(settings.schedule.slots)]
    attempt = settings.attempt
    fable = set(rng.sample(range(len(day)), min(max(attempt.fable_per_day, 0), len(day))))
    efforts = iter(_efforts(settings, rng, len(day) - len(fable)))
    return [
        {"cell": cell, "model": attempt.fable_model, "effort": attempt.fable_effort}
        if index in fable else
        {"cell": cell, "model": attempt.model, "effort": next(efforts)}
        for index, cell in enumerate(day)
    ]


def entries(settings, plan_json: str) -> list[dict]:
    """A stored plan as one ``{"cell", "model", "effort"}`` per slot.

    A plan stored before arms existed is a list of bare cell names, and every slot of such
    a day ran ``attempt.model`` at ``attempt.effort``, so that is how it reads.
    """
    return [
        {"cell": raw, "model": settings.attempt.model, "effort": settings.attempt.effort}
        if isinstance(raw, str) else dict(raw)
        for raw in json.loads(plan_json)
    ]


def plan(ledger, settings, day: str) -> list[dict]:
    """The day's plan, drawing and storing it on the first call (docs/22 section 8.7).

    ``day`` is an Eastern date, ``YYYY-MM-DD``. The draw happens once: every later call
    returns the stored list, so a slot that runs at 22:20 gets the cell and the arm the
    00:00 tick wrote down for it, whatever has changed in between.
    """
    row = ledger.cell_plan(day)
    if row is None:
        seed = random.randrange(2 ** 31)
        ledger.set_cell_plan(day, seed, json.dumps(draw(settings, seed)))
        row = ledger.cell_plan(day)
    return entries(settings, row["plan"])


def _slot_day_time(slot_key: str | None) -> tuple[str, str] | None:
    """``("2026-09-20", "01:00")`` from ``slot:2026-09-20/01:00``; ``None`` if unreadable."""
    if not slot_key or not slot_key.startswith("slot:"):
        return None
    day, sep, hhmm = slot_key[len("slot:"):].partition("/")
    return (day, hhmm) if sep and day and hhmm else None


def _slot_entry(ledger, settings, slot_key: str | None) -> dict | None:
    """This slot's entry in its day's plan, or ``None`` when the plan cannot place it.

    The slot's position in ``schedule.slots`` is its index into the plan. A slot key the
    schedule does not name, or one past the end of a truncated plan, has no entry.
    """
    parts = _slot_day_time(slot_key)
    if parts is None:
        return None
    day, hhmm = parts
    slots = list(settings.schedule.slots or [])
    if hhmm not in slots:
        return None
    today = plan(ledger, settings, day)
    index = slots.index(hhmm)
    return today[index] if index < len(today) else None


def cell_for_slot(ledger, settings, slot_key: str) -> str:
    """The cell this slot runs, from the day's plan.

    A slot the plan cannot place runs static: static is the cell that needs nothing but
    the ledger, so it is the safe answer when the plan cannot say.
    """
    entry = _slot_entry(ledger, settings, slot_key)
    return entry["cell"] if entry else "static"


def arm_for_slot(ledger, settings, slot_key: str | None) -> tuple[str, str]:
    """``(model, effort)`` for this slot, from the day's plan.

    The model is the configured name; the session resolves it through the substitution
    switch. A slot the plan cannot place, and a manual attempt with no slot at all, runs
    ``attempt.model`` at ``attempt.effort``.
    """
    entry = _slot_entry(ledger, settings, slot_key)
    if entry is None:
        return settings.attempt.model, settings.attempt.effort
    return entry["model"], entry["effort"]


# --------------------------------------------------------------------------- the texts
def principles_section(settings, cell: str) -> str:
    """``docs/principles.md`` whole, or ``""`` for baseline and for a missing page.

    Read from the top with no marker and no header line: the page is written to be read as
    it stands, and the attempt sees exactly the file Arno last edited. The caller audits
    ``principles_missing`` when a cell that should have seen it got nothing.
    """
    if cell == "baseline":
        return ""
    try:
        text = (settings.root / "docs" / "principles.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    return text.strip()


def _md_section(md: str | None, heading: str) -> str:
    """The body under ``## <heading>``, up to the next heading. ``""`` when absent."""
    if not md:
        return ""
    out: list[str] = []
    taking = False
    for line in md.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            if taking:
                break
            taking = stripped.lstrip("#").strip().lower() == heading.lower()
            continue
        if taking:
            out.append(line)
    return "\n".join(out).strip()


def _records(ledger, attempt_ids: list[str]) -> tuple[list[str], list[str]]:
    """``(records, ids rendered)`` for the ids that name an attempt the ledger still has.

    A stored director set is a list of ids written days earlier, and an id that names
    nothing is not a reason to fail every director and focused slot until a newer page
    validates. The unknown id is skipped and the attempt row records only what its
    CONTEXT.md actually holds, so ``example_ids`` never claims an example nobody saw.
    """
    records: list[str] = []
    rendered: list[str] = []
    for aid in attempt_ids:
        if ledger.get_attempt(aid) is None:
            continue
        records.append(render_record(ledger, aid))
        rendered.append(aid)
    return records, rendered


def _static_context(ledger, settings) -> tuple[str, list[str]]:
    how_many = _count_word(settings.cells.static_recent)
    ids = recent_completed(ledger, limit=settings.cells.static_recent)
    records, rendered = _records(ledger, ids)
    parts = [f"## Past attempts (the {how_many} most recent)", ""]
    parts += _joined(records)
    return "\n".join(parts).rstrip() + "\n", rendered


def _joined(records: list[str]) -> list[str]:
    """Records as blocks with one blank line between them; a note when there are none."""
    if not records:
        return ["none yet"]
    out: list[str] = []
    for record in records:
        out += [record.rstrip(), ""]
    return out


def _direction_block(direction: str) -> list[str]:
    return ["## Direction", "", direction.strip(), ""]


def _sets(run: dict) -> dict:
    try:
        loaded = json.loads(run["sets_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def render(ledger, settings, cell: str):
    """What this cell shows an attempt (docs/22 section 8.5).

    Returns ``(context_md, memory_section, principles_section, example_ids,
    direction_hash, cell_effective)``. ``context_md`` is ``None`` for baseline, which gets
    no ``CONTEXT.md`` file at all. ``cell_effective`` differs from ``cell`` only when a
    director or focused slot found no valid director run and ran as static, which is what
    the first days of a new era look like and is not a failure.

    ``memory_section`` and ``principles_section`` are the two prompt placeholders, each
    carrying its own leading newline so that the template's single
    ``{memory_section}{priors_section}`` line renders as ordinary Markdown paragraphs after
    the guideline list.
    """
    effective = cell
    run = None
    if cell in ("director", "focused"):
        run = latest_valid_run(ledger)
        if run is None:
            effective = "static"

    direction = ""
    if effective == "baseline":
        context, ids = None, []
    elif effective == "static":
        context, ids = _static_context(ledger, settings)
    elif effective == "director":
        sets = _sets(run)
        direction = "\n\n".join(
            part for part in (
                _md_section(run["page_md"], "Standing direction"),
                _md_section(run["page_md"], "Today"),
            ) if part
        )
        records, ids = _records(ledger, list(sets.get("balanced") or []))
        parts = _direction_block(direction)
        parts += ["## Past attempts (chosen for today)", ""]
        note = str(sets.get("balanced_note") or "").strip()
        if note:
            parts += [note, ""]
        parts += _joined(records)
        context = "\n".join(parts).rstrip() + "\n"
    else:
        sets = _sets(run)
        direction = str(sets.get("focused_direction") or "").strip()
        records, ids = _records(ledger, list(sets.get("focused") or []))
        lens = str(sets.get("focused_lens") or "chosen through one lens").strip()
        parts = _direction_block(direction)
        parts += [f"## Past attempts ({lens})", ""]
        parts += _joined(records)
        context = "\n".join(parts).rstrip() + "\n"

    principles = principles_section(settings, effective)
    return (
        context,
        "\n" + _memory_section(settings, effective),
        ("\n\n" + principles) if principles else "",
        ids,
        _sha12(direction) if direction else None,
        effective,
    )
