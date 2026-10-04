"""Prompt/parser drift guard (PC-10 — the vaccine).

Every literal a parser looks for must be present in the prompt that is supposed to
produce it, and every shape a prompt shows must be one the parser accepts. This is the
test that would have caught the July-31 runaway: `deep_review.md` never asked for the
`## Grade changes` heading the harness rejected 29 paid reviews for missing.

The rule for anything added here: assert against the parser's own constant (imported,
never retyped), or feed the prompt's example through the real parser. A test that
hardcodes the string on both sides guards nothing.

Two prompts are left to guard: the attempt's, whose literals a parser reads back, and the
director's, which has no parser but does have placeholders. The two-loop phase prompts are
archived (docs/22 section 2.2), and the grader, curator and deep-review prompts went with
their modules in phase three; nothing here reads them any more.
"""

import json
import re
from importlib.resources import files

import pytest

from betting_agent.bt import LISTING_LENSES
from betting_agent.config import Settings
from betting_agent.harness.attempt import _render_task
from betting_agent.harness.cells import CELLS
from betting_agent.harness.validate import (
    _BET_ALLOWED,
    _BET_REQUIRED,
    _EDGE_HEADINGS,
    _HYP_HEADINGS,
    _MAX_RESOLUTION_EVENT,
    _PRICE_RE,
    parse_ticket,
)

_ALL_PROMPTS = ("attempt.md", "director.md")


def prompt(name: str = "attempt.md") -> str:
    return (files("betting_agent") / "prompts" / name).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- headings
def test_the_attempt_prompt_names_every_required_heading():
    """V02 rejects the whole ticket for a missing H2, so the prompt lists all six."""
    text = " ".join(prompt().split())
    for heading in _EDGE_HEADINGS + _HYP_HEADINGS:
        assert heading.removeprefix("## ") in text, f"attempt.md never mentions {heading!r}"


def test_the_attempt_prompt_names_the_file_the_harness_reads():
    assert "bets.json" in prompt()


# --------------------------------------------------------------------------- formats (D3)
def test_price_examples_match_the_regex_that_makes_them_fatal():
    """PC-4/SV-6: prices are 4dp JSON strings; every example must satisfy `_PRICE_RE`."""
    text = prompt()
    quoted = re.findall(r'"(limit_price)": "([^"]*)"', text)
    assert quoted, "attempt.md shows no price example at all"
    for key, value in quoted:
        assert _PRICE_RE.match(value), f"attempt.md shows {key}={value!r}, which V01 rejects"


def test_format_rules_are_stated_not_merely_implied():
    """The caps are whole-ticket fatal, so they are stated in prose, not left to example."""
    text = " ".join(prompt().split())
    assert "four decimals" in text
    assert "1 to 400 characters" in text   # rationale cap, V01-fatal
    assert "1 to 80" in text               # ticker cap, V01-fatal


# --------------------------------------------------------------------------- resolution_event (D7)
def test_resolution_event_is_documented_where_the_ticket_is_written():
    """docs/14 D7: an optional per-bet key the validator accepts (``_BET_ALLOWED``) is
    invisible to a model unless the prompt names it, and neutrally, per Arno's ruling
    (measurement only, no steering language)."""
    assert "resolution_event" in _BET_ALLOWED
    text = prompt()
    assert '"resolution_event"' in text
    # The bound has to be stated, because breaking it is fatal to the WHOLE ticket: the
    # validator files a bad ``resolution_event`` as V01 alongside a malformed price, so an
    # over-long event name costs the session everything it just paid for, over a field D7
    # defines as measurement only.
    assert f"1 to {_MAX_RESOLUTION_EVENT} characters" in " ".join(text.split())
    # Neutral means no advice about avoidance, limits, or when you "should"/"must"
    # declare it, just what the field is and that it names a real-world event.
    steering_phrases = (
        "avoid ", "should declare", "must declare", "do not declare",
        "limit your exposure", "too many bets on",
    )
    window = text.lower().split("resolution_event", 1)[-1][:400]
    for phrase in steering_phrases:
        assert phrase not in window, (
            f"attempt.md adds steering language near resolution_event ({phrase!r})"
        )


# --------------------------------------------------------------------------- lenses (C3)
def _flat(text: str) -> str:
    """Whitespace-normalized, lowercased: this prompt hard-wraps, and a contract about
    what it SAYS must not depend on where a line happened to break."""
    return " ".join(text.split()).lower()


def test_the_prompt_names_every_listing_lens():
    """docs/14 C3: a lens a session is never told about does not exist as far as the
    session is concerned — the ``bt history`` precedent, which shipped unnamed and went
    unused until a prompt line was added for it. Asserted against ``bt.LISTING_LENSES``,
    never a list retyped here (PC-10's own rule)."""
    text = _flat(prompt())
    for lens in LISTING_LENSES:
        assert f"`bt {lens}" in text, f"attempt.md never names `bt {lens}`"


def test_the_prompt_states_the_cache_timestamp_convention():
    """The convention is the whole point of C1: a cached board is safe only while nobody
    can mistake it for a live one. The prompt calls it a board snapshot rather than a
    cached snapshot; the convention it states is the same one."""
    text = _flat(prompt())
    assert "board snapshot" in text
    assert "capture time" in text
    assert "--live" in text
    assert "`bt book` is always live" in text


def test_the_lens_lines_survive_rendering_braces_and_all():
    """``_render_task`` replaces only enumerated placeholders (its docstring says so,
    because the bets.json example already needed that), so literal braces pass through.
    Run the real renderer to prove it rather than trusting the reading."""
    source = prompt()
    rendered = _render_task(source, _SUBS)

    # The general property, which covers the bets.json example: a brace token that is not
    # an enumerated placeholder survives verbatim.
    placeholders = {"{" + key + "}" for key in _SUBS}
    for token in sorted(set(re.findall(r"\{[^{}\n]*\}", source)) - placeholders):
        assert token in rendered, f"attempt.md: rendering ate {token!r}"
    for lens in LISTING_LENSES:
        assert f"`bt {lens}" in _flat(rendered)
    # …and the placeholders really were substituted, so the property above is not vacuous.
    assert not (placeholders & set(re.findall(r"\{[^{}\n]*\}", rendered)))


def test_the_lens_descriptions_carry_no_strategy_guidance():
    """C3 is explicit: tool descriptions only. docs/13's own prose about the lenses says
    fresh listings are "plausibly where a slow exchange is most wrong" — true or not, that
    is a hypothesis for a session to form, and putting it in the prompt would make every
    attempt's answer to "where is the edge" partly the harness's answer."""
    text = _flat(prompt())
    steering = (
        "where the market is wrong", "most wrong", "where the edge", "are where",
        "look for", "focus on", "best place", "start with `bt", "most promising",
        "prefer ", "avoid the", "ignore the", "dead weight", "worth betting",
        # "opportunit" was on this list until the required heading became `## Why the
        # opportunity exists and persists` (docs/22 section 5.4). Asking a session to
        # justify its own claim is not telling it where to hunt, which is what the rest
        # of this list is about.
    )
    for phrase in steering:
        assert phrase not in text, f"attempt.md steers the hunt ({phrase!r})"


# --------------------------------------------------------------------------- the bet shape
def test_the_attempt_prompt_shows_every_required_bet_key():
    text = prompt()
    for key in sorted(_BET_REQUIRED):
        assert f'"{key}"' in text, f"attempt.md never shows the bet key {key!r}"
    # And the fields the ticket dropped must not be advertised any more.
    for gone in ("model_prob", "edge_class", "groups", "candidates", "chosen"):
        assert f'"{gone}"' not in text, f"attempt.md still shows {gone!r}"


def _documented_ticket() -> dict:
    """The ``bets.json`` object the prompt prints, as JSON.

    The slim prompt shows the shape inline in backticks rather than in a fenced block, and
    it documents two fields by their vocabulary rather than by a value: ``...`` for the
    ticker and ``"yes"|"no"`` for the side. Both are resolved to one legal value here and
    nothing else is touched, so what the parser sees is the prompt's own object.
    """
    text = prompt().replace("\n", " ")
    match = re.search(r"`(\{\"attempt\".*?\}\]\})`", text)
    assert match, "attempt.md no longer prints a bets.json object"
    literal = (match.group(1)
               .replace("{attempt_id}", "A-0001")
               .replace('"yes"|"no"', '"yes"')
               .replace("...", '"KXTEST-26SEP20-T1"'))
    return json.loads(literal)


def test_the_attempt_prompts_bets_example_actually_parses(tmp_path):
    """The strongest form of this test: run the documented example through V01 itself."""
    ticket = _documented_ticket()
    assert set(ticket["bets"][0]) >= _BET_REQUIRED

    tdir = tmp_path / "ticket"
    tdir.mkdir()
    (tdir / "bets.json").write_text(json.dumps(ticket))
    (tdir / "edge_claim.md").write_text("\n".join(_EDGE_HEADINGS) + "\n")
    (tdir / "hypothesis.md").write_text("\n".join(_HYP_HEADINGS) + "\n")

    parsed = parse_ticket(tdir)
    assert parsed.whole_ticket_errors == [], parsed.error_detail
    assert parsed.bets and all(1 <= b.contracts <= 3 for b in parsed.bets)


def test_the_attempt_prompt_states_the_size_range_the_validator_enforces():
    """V10 refuses anything outside 1 to ``stakes.max_contracts_per_bet``."""
    text = " ".join(prompt().split())
    assert "1, 2 or 3 contracts a bet" in text
    assert "`contracts` is 1, 2 or 3" in text


def test_the_attempt_prompt_states_the_allowance_the_validator_enforces():
    """V16 refuses the legs past ``stakes.per_attempt_real_cap`` (Arno, 2026-09-27; $11.20 from
    2026-10-01). The
    figure is written into the prompt rather than substituted, so it is pinned here to
    the code default; ``test_activation_config`` pins the live config to the same value."""
    text = " ".join(prompt().split())
    assert "Each attempt also has a stake allowance of $11.20 in total" in text
    assert "`bt ticket validate` refuses the legs that go past it" in text
    assert "rank your legs and keep the ones that matter most" in text
    assert f"${Settings().stakes.per_attempt_real_cap}" in text


def test_the_prompt_asks_for_the_closing_paragraph_the_runner_stores():
    """``attempts.session_summary`` is ``SessionResult.result_text``, which is only a
    summary because the prompt's last line asks for one (docs/22 section 5.2 step 9)."""
    assert "one-paragraph summary" in " ".join(prompt().split())


# --------------------------------------------------------------------------- placeholders
# The substitutions the prompt is rendered with. Keep in sync with ``run_attempt``'s
# ``subs`` dict: five placeholders, no more.
_SUBS = {
    "attempt_id": "A-0001",
    "env": "prod",
    "window_hours": "120",
    "memory_section": "\nmemory line",
    "priors_section": "\n\n# Principles to guide you",
}
_ALLOWED_PLACEHOLDERS = {
    "attempt.md": set(_SUBS),
    # docs/22 section 11 named two; ``attempts_per_day`` joined them on 2026-09-21, when
    # the schedule's fifteen slots made the brief's "nine attempts a day" a false statement.
    "director.md": {"run_date", "cohort_date", "attempts_per_day"},
}


_DIRECTOR_SUBS = {"run_date": "2026-09-20", "cohort_date": "2026-09-19",
                  "attempts_per_day": "15"}


@pytest.mark.parametrize("name", _ALL_PROMPTS)
def test_no_prompt_carries_a_placeholder_nobody_substitutes(name):
    """An unrendered `{placeholder}` reaches the model verbatim — the PC-7 failure mode."""
    found = set(re.findall(r"\{([a-z_]+)\}", prompt(name)))
    assert found <= _ALLOWED_PLACEHOLDERS[name], (
        f"{name} uses {sorted(found - _ALLOWED_PLACEHOLDERS[name])}, which nothing renders"
    )


def test_the_director_briefs_json_skeletons_survive_rendering():
    """The brief shows the two file shapes as literal JSON, so it is full of braces.

    ``_render_task`` replaces only the enumerated placeholders, which is what lets a
    prompt carry a JSON example at all; the attempt's ``bets.json`` example needed the
    same property. Run the real renderer rather than trusting the reading.
    """
    source = prompt("director.md")
    rendered = _render_task(source, _DIRECTOR_SUBS)

    placeholders = {"{" + key + "}" for key in _DIRECTOR_SUBS}
    for token in sorted(set(re.findall(r"\{[^{}\n]*\}", source)) - placeholders):
        assert token in rendered, f"director.md: rendering ate {token!r}"
    # …and the placeholders really were substituted, so the property above is not vacuous.
    assert not (placeholders & set(re.findall(r"\{[^{}\n]*\}", rendered)))
    assert '"cohort": "2026-09-19"' in rendered
    assert "# Director run 2026-09-20" in rendered


def test_the_brief_states_the_throughput_the_schedule_actually_runs():
    """The count is rendered, not typed. It said "nine" while fifteen slots ran, in the
    opening paragraph and in the rule that tells the director a day's profit is noise, and
    a brief that misstates the throughput it forbids steering on is one to distrust."""
    from betting_agent.config import ScheduleSettings
    from betting_agent.harness.director import task_md

    source = prompt("director.md")
    assert "nine attempts a day" not in source.lower()
    assert source.count("{attempts_per_day} attempts a day") == 2

    slots = len(ScheduleSettings().slots)
    rendered = task_md("2026-09-20", "2026-09-19", attempts_per_day=slots)
    assert f"runs\n{slots} attempts a day" in rendered
    assert (f"- {slots} attempts a day at one to three contracts a leg means a\n"
            "  day's profit column is mostly noise.") in rendered


def test_the_director_brief_states_the_limits_the_validator_enforces():
    """The first live run was discarded for a shape the brief never described.

    It wrote its ranking as a list of objects under a key of its own invention and a page
    a third over the limit, because the brief described the three files in prose and named
    no shape and no number. Every bound the validator applies is pinned here against the
    validator's own constant, so the two cannot drift apart again.
    """
    from betting_agent.config import DirectorSettings
    from betting_agent.harness import director

    flat = _flat(prompt("director.md"))      # whitespace-normalized and lowercased

    assert f"at most {DirectorSettings().page_max_chars:,} characters" in flat
    assert (f"each between {director._PARAGRAPH_MIN} and {director._PARAGRAPH_MAX:,} "
            "characters") in flat
    assert f"each hold {director._SET_MIN} to {director._SET_MAX} attempt ids" in flat
    assert (f"is between {director._DIRECTION_MIN} and {director._DIRECTION_MAX:,} "
            "characters") in flat
    for heading in director._PAGE_HEADINGS:
        assert f"`{heading.lower()}`" in flat


def test_the_two_placeholders_render_as_their_own_paragraphs():
    """``{memory_section}{priors_section}`` share one template line, so each block carries
    its own leading newline; the rendered result must be ordinary Markdown paragraphs, not
    a run-on line glued to the last guideline."""
    rendered = _render_task(prompt(), _SUBS)
    assert "\n\nmemory line\n\n# Principles to guide you\n" in rendered
    assert "A pass is a valid result.\n\nmemory line" in rendered
    assert "# Principles to guide you\n\nEnd the session" in rendered


def test_a_baseline_render_leaves_no_stray_blank_run():
    """The baseline cell shows no principles page, so the memory line stands alone."""
    subs = dict(_SUBS, memory_section="\nbaseline line", priors_section="")
    rendered = _render_task(prompt(), subs)
    assert "A pass is a valid result.\n\nbaseline line\n\nEnd the session" in rendered
    assert "baseline" in CELLS
