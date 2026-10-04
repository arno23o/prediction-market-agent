"""The history module: records, outcomes, families, search (docs/22 sections 8 and 9).

One seeded ledger carrying the cases the loop actually meets: a new-era attempt with two
legs and both director paragraphs, an old-era attempt with the old headings, a pass, a
failed session with no ticket, an attempt still waiting on settlement, and a leg the
exchange never filled. Every assertion below is against those rows, because a record that
disagrees with the ledger is worse than no record.
"""

from __future__ import annotations

import json
from decimal import Decimal as D

import pytest

from betting_agent.config import load_settings
from betting_agent.ledger import history
from betting_agent.ledger.db import Ledger

CLAIM = """## Markets
KXHORMUZWEEKLY-25SEP05-T3 and its ladder siblings.

## Why this is profitable
The strait has stayed open through three escalations and the ladder still prices a
closure at six in ten, which is the war headline rather than the shipping data.

## Why the opportunity exists and persists
Retail reads the headline; the shipping trackers are paywalled.
"""

HYPOTHESIS = """## If we're right
The ladder settles no and both legs pay.

## If we're wrong
A closure is announced and the ladder gaps.

## Kill criteria
Any Lloyd's list closure notice, or a tanker rerouting above 20 a day.
"""

OLD_CLAIM = """## Markets
KXHIGHNY-26JUL08-B85.5, the New York high ladder.

## The edge
The model says 0.31 against a 0.44 ask, off a NWS forecast four hours newer than the
last trade.

## Why it exists and persists
Nobody reprices the weather ladder overnight.

## Edge type tags
stale-price
"""

PASS_CLAIM = """## Markets
KXHORMUZWEEKLY-25SEP07-T1 and KXHORMUZWEEKLY-25SEP07-T5.

## Why this is profitable
Nothing cleared the bar: the book was one-sided at every strike.

## Why the opportunity exists and persists
It does not; this is a pass.
"""


@pytest.fixture
def settings(tmp_path):
    (tmp_path / "data").mkdir()
    return load_settings(root=tmp_path)


@pytest.fixture
def lg(settings):
    ledger = Ledger.open(settings.ledger_path)
    ledger.migrate()
    yield ledger
    ledger.close()


def _attempt(lg, *, slot, status, cell=None, era="live-v2", claim=None, hypothesis=None,
             summary=None, wall_seconds=1860):
    _seq, aid = lg.create_attempt(
        env="prod", model="claude-opus-5", effort="high", memory_mode="on",
        prompt_version="p", toolkit_version="0.1.0", workspace_path="/ws",
        slot=slot, cell=cell, cell_effective=cell, era=era,
    )
    lg.transition(aid, "running")
    if claim is not None:
        lg.set_ticket_texts(aid, claim, hypothesis or HYPOTHESIS, "manifest text")
    if summary is not None:
        lg.set_session_summary(aid, summary)
        lg.fts_upsert(aid, "closing", summary)
    lg.update_attempt_fields(aid, wall_seconds=wall_seconds, cost_usd=D("11.94"))
    lg.transition(aid, status)
    return aid


def _director_run(lg, run_id="D-2026-08-30", run_date="2026-08-30", status="valid",
                  page="## Standing direction\nKeep sweeping the strait ladders.",
                  started_at="2026-08-30T04:00:00Z"):
    lg.conn.execute(
        "INSERT INTO director_runs (run_id, run_date, cohort_date, started_at, model, "
        "status, page_md, page_hash, sets_json) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, run_date, "2026-08-29", started_at, "claude-fable-5", status, page,
         "abc123abc123", json.dumps({"balanced": []})),
    )
    lg.conn.commit()
    return run_id


def _review(lg, aid, kind, run_id, *, rank, size, paragraph):
    lg.conn.execute(
        "INSERT INTO attempt_reviews (attempt_id, kind, run_id, cohort_date, rank, "
        "cohort_size, paragraph) VALUES (?,?,?,?,?,?,?)",
        (aid, kind, run_id, "2026-08-29", rank, size, paragraph),
    )
    lg.conn.commit()


def _old_fts(lg, aid, kind, content):
    """Write an index row under a kind the ledger no longer accepts from `fts_upsert`."""
    lg.conn.execute(
        "INSERT INTO ledger_fts (attempt_id, kind, content) VALUES (?,?,?)",
        (aid, kind, content),
    )
    lg.conn.commit()


# The category refusal the exchange sent between 2026-09-18 and 09-20, with the state
# replaced by a generic phrase, in the shape `execute` stores it: the exception's own
# string, whose JSON body `KalshiAPIError` cuts at 300 characters, so the blob ends
# mid-object and does not parse.
BLOCKED_REASON = (
    'KalshiAPIError: HTTP 403: Forbidden — {"error":{"code":"forbidden_location",'
    '"message":"Residents of this state are not currently allowed to open positions in '
    'Sports, Elections and Entertainment. Check your email for more details.","service":"orde'
)


def _blocked(lg, *, reason=BLOCKED_REASON, slot="slot:2026-09-19/01:00"):
    """An attempt whose one leg the exchange refused: `no_fill` carrying the refusal text."""
    aid = _attempt(lg, slot=slot, status="no_bets", cell="static", claim=CLAIM)
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1,
                  ticker="KXPRESPARTY-26NOV03-DEM", market_title="Party of the president",
                  category="Politics", side="yes", limit_price=D("0.32"), rationale="r",
                  is_real=1, status="no_fill", contracts=1, declared_contracts=1,
                  client_order_id=f"{aid}-B01", placed_at="2026-09-19T05:10:00Z",
                  reject_reason=reason)
    return aid


@pytest.fixture
def seeded(lg):
    """The six attempts every test below reads."""
    ids = {}

    # A new-era director attempt: one settled winner, one refusal with its stated reason.
    ids["director"] = a = _attempt(
        lg, slot="slot:2026-08-29/01:00", status="placed", cell="director", claim=CLAIM,
        summary="Swept the strait ladder, bet two strikes, one was refused by the cap.",
    )
    lg.insert_bet(bet_id=f"{a}-B01", attempt_id=a, ticket_index=1,
                  ticker="KXHORMUZWEEKLY-25SEP05-T3", market_title="Strait open",
                  category="Politics", side="no", limit_price=D("0.62"), rationale="r",
                  is_real=1, status="settled", contracts=2, fill_price=D("0.62"),
                  stake=D("1.24"), fee=D("0.04"), outcome="win", pnl=D("0.72"),
                  order_id="o1", client_order_id=f"{a}-B01",
                  placed_at="2026-08-29T05:10:00Z", settled_at="2026-08-30T05:00:00Z")
    lg.insert_bet(bet_id=f"{a}-B02", attempt_id=a, ticket_index=2,
                  ticker="KXHORMUZWEEKLY-25SEP05-T5", market_title="Strait open",
                  category="Politics", side="yes", limit_price=D("0.21"), rationale="r",
                  is_real=1, status="rejected", declared_contracts=1,
                  reject_code="cap_daily", reject_reason="daily cap: $9.80 of $10.00 "
                  "already committed today")
    lg.score_rejected_bet(f"{a}-B02", outcome="loss", hypothetical_pnl=D("-0.21"),
                          scored_at="2026-08-30T05:00:00Z")
    run = _director_run(lg)
    _review(lg, a, "prospective", run, rank=2, size=4,
            paragraph="A clean sweep of one family with a stated kill criterion. " * 4)
    _review(lg, a, "retrospective", run, rank=1, size=4,
            paragraph="It won for the reason it gave, and the refusal cost it nothing. " * 4)
    lg.upsert_activity({
        "attempt_id": a, "markets_probed": 14, "series_probed": 3,
        "series_list": json.dumps(["KXHORMUZWEEKLY", "KXHIGHNY", "KXOPEC"]),
        "web_fetches": 2, "web_searches": 1, "n_domains": 4, "code_runs": 3,
        "files_written": 7, "past_calls": 3, "past_subcommands": json.dumps({"family": 3}),
    })

    # An old-era attempt: the old headings, no cell, a settled loss.
    ids["old"] = b = _attempt(lg, slot="slot:2026-07-08/10:00", status="placed",
                              era="live-v1", claim=OLD_CLAIM,
                              hypothesis=HYPOTHESIS.replace("Kill criteria", "Kill criteria"))
    lg.insert_bet(bet_id=f"{b}-B01", attempt_id=b, ticket_index=1,
                  ticker="KXHIGHNY-26JUL08-B85.5", market_title="NY high",
                  category="Weather", side="yes", limit_price=D("0.44"), rationale="r",
                  is_real=1, status="settled", contracts=1, fill_price=D("0.44"),
                  stake=D("0.44"), fee=D("0.02"), outcome="loss", pnl=D("-0.46"),
                  order_id="o2", client_order_id=f"{b}-B01",
                  placed_at="2026-07-08T14:10:00Z", settled_at="2026-07-09T05:00:00Z")
    # `retro`, `summary` and `tags` are retired write targets (docs/22 section 13), so
    # these old rows go in the way history left them: straight into the index.
    _old_fts(lg, b, "retro", "the grader called this hindenburg reasoning")
    _old_fts(lg, b, "summary", "a hindenburg of a weather bet")
    _old_fts(lg, b, "tags", "hindenburg stale-price")

    # A pass: a ticket, no legs.
    ids["pass"] = _attempt(lg, slot="slot:2026-08-29/03:40", status="no_bets", cell="static",
                           claim=PASS_CLAIM, summary="Looked at the strait, passed.")

    # A failed session with no ticket at all.
    ids["failed"] = _attempt(lg, slot="slot:2026-08-29/06:20", status="failed", cell="static")

    # Still waiting on settlement: a filled leg that has not resolved.
    ids["open"] = c = _attempt(lg, slot="slot:2026-08-30/01:00", status="placed",
                               cell="static", claim=CLAIM)
    lg.insert_bet(bet_id=f"{c}-B01", attempt_id=c, ticket_index=1,
                  ticker="KXHORMUZWEEKLY-25SEP12-T3", market_title="Strait open",
                  category="Politics", side="no", limit_price=D("0.55"), rationale="r",
                  is_real=1, status="filled", contracts=1, fill_price=D("0.55"),
                  stake=D("0.55"), fee=D("0.02"), order_id="o3",
                  client_order_id=f"{c}-B01", placed_at="2026-08-30T05:10:00Z")

    # A leg the exchange never filled, scored hypothetically: complete.
    ids["nofill"] = d = _attempt(lg, slot="slot:2026-08-28/22:20", status="placed",
                                 cell="focused", claim=OLD_CLAIM)
    lg.insert_bet(bet_id=f"{d}-B01", attempt_id=d, ticket_index=1,
                  ticker="KXHIGHNY-26AUG28-B90.5", market_title="NY high",
                  category="Weather", side="yes", limit_price=D("0.30"), rationale="r",
                  is_real=1, status="no_fill", contracts=1, declared_contracts=1,
                  order_id="o4", client_order_id=f"{d}-B01")
    lg.score_nofill_bet(f"{d}-B01", outcome="win", hypothetical_pnl=D("0.70"),
                        scored_at="2026-08-29T05:00:00Z")
    return ids


# --------------------------------------------------------------------------- records
def test_the_short_record_is_the_layout_of_section_8_5(lg, seeded):
    text = history.render_record(lg, seeded["director"])
    lines = text.splitlines()
    assert lines[0] == (
        f"{seeded['director']} · 2026-08-29 01:00 · director cell · claude-opus-5 high · "
        "Politics · KXHORMUZWEEKLY"
    )
    assert lines[1].startswith("Claim: The strait has stayed open through three escalations")
    assert lines[2] == ("Bets: KXHORMUZWEEKLY-25SEP05-T3 no @0.6200 ×2 → filled, won, +$0.72")
    assert lines[3].strip() == (
        "KXHORMUZWEEKLY-25SEP05-T5 yes @0.2100 ×1 → refused: daily cap: $9.80 of "
        "$10.00 already committed today"
    )
    assert lines[4].startswith("Kill: Any Lloyd's list closure notice")
    assert lines[5] == "Net: +$0.72 on $1.24 staked · probed 14 markets · 31 minutes"
    assert lines[6].startswith("Review: It won for the reason it gave")


def test_the_record_prefers_the_retrospective_paragraph(lg, seeded):
    """Both paragraphs exist; the one written with the outcome known is the one shown."""
    assert "Review: It won for the reason" in history.render_record(lg, seeded["director"])


def test_a_record_without_a_review_omits_the_line(lg, seeded):
    assert "Review:" not in history.render_record(lg, seeded["open"])


def test_the_record_names_the_arm_the_attempt_ran(lg, seeded):
    """The model and effort on the row, right after the cell word (2026-09-26)."""
    lg.update_attempt_fields(seeded["director"], model="claude-fable-5-1", effort="max")
    header = history.render_record(lg, seeded["director"]).splitlines()[0]
    assert " · director cell · claude-fable-5-1 max · Politics · " in header


def test_a_pass_renders_its_markets_and_no_legs(lg, seeded):
    text = history.render_record(lg, seeded["pass"])
    assert text.splitlines()[0].endswith("· static cell · claude-opus-5 high · passed")
    assert "Bets: none (passed) KXHORMUZWEEKLY-25SEP07-T1" in text
    assert "Net: +$0.00 on $0.00 staked" in text


def test_an_old_era_ticket_renders_from_its_first_section(lg, seeded):
    """No `## Why this is profitable` heading and no cell: the record says what exists."""
    text = history.render_record(lg, seeded["old"])
    assert text.splitlines()[0] == (
        f"{seeded['old']} · 2026-07-08 10:00 · live-v1 era · claude-opus-5 high · Weather · "
        "KXHIGHNY"
    )
    assert text.splitlines()[1].startswith("Claim: KXHIGHNY-26JUL08-B85.5, the New York high")
    assert "→ filled, lost, -$0.46" in text


def test_a_failed_session_with_no_ticket_renders_as_an_empty_record(lg, seeded):
    """docs/22 section 8.1: it is a member of its cohort and it passed on nothing.

    It never got as far as having something to pass on, so the record says the session
    failed without a ticket, and the claim and kill lines it has no text for are absent
    rather than blank.
    """
    lines = history.render_record(lg, seeded["failed"]).splitlines()
    assert lines[0].endswith("· static cell · claude-opus-5 high · no ticket")
    assert lines[1] == "Bets: none (session failed, no ticket)"
    assert lines[2].startswith("Net: +$0.00 on $0.00 staked")
    assert not any(line.startswith(("Claim:", "Kill:")) for line in lines)
    assert "none (session failed, no ticket)" in history.render_record(
        lg, seeded["failed"], full=True
    )


def test_an_unfilled_leg_reads_as_no_fill_and_an_unsettled_one_as_open(lg, seeded):
    assert "→ no fill" in history.render_record(lg, seeded["nofill"])
    assert "→ open" in history.render_record(lg, seeded["open"])


def test_an_exchange_refusal_says_what_the_exchange_said(lg, seeded):
    """The category block, 2026-09-18 to 09-20: 68 legs refused by the exchange, every one
    of them rendered as a bare "no fill", and the two attempts that read the history raised
    their limits from 0.32 to 0.90 against a door that was shut."""
    aid = _blocked(lg)
    text = history.render_record(lg, aid)
    assert (
        "KXPRESPARTY-26NOV03-DEM yes @0.3200 ×1 → refused by the exchange: Residents of "
        "this state are not currently allowed to open positions in Sports, Elections and "
        "Entertainment. Check your email for more details."
    ) in text
    assert "no fill" not in text
    assert "KalshiAPIError" not in text and '"message"' not in text
    assert len(text) <= history.SHORT_MAX
    assert "Net: +$0.00 on $0.00 staked" in text      # a refusal holds no position


def test_the_refusal_wording_is_the_same_everywhere_a_leg_is_rendered(lg, seeded):
    aid = _blocked(lg)
    wording = "refused by the exchange: Residents of this state"
    assert wording in history.render_record(lg, aid, full=True)
    entry = history.family(lg, "KXPRESPARTY")["entries"][0]
    assert entry["legs"][0]["fill"].startswith(wording)


def test_a_refusal_with_no_message_field_shows_what_there_is(lg, seeded):
    aid = _blocked(lg, reason="KalshiAPIError: HTTP 503: Service Unavailable")
    assert "→ refused by the exchange: KalshiAPIError: HTTP 503: Service Unavailable" in (
        history.render_record(lg, aid)
    )


def test_a_no_fill_with_no_reason_is_still_a_no_fill(lg, seeded):
    """A limit the book never reached is not a refusal, and must not start reading as one."""
    assert "→ no fill" in history.render_record(lg, seeded["nofill"])
    assert "refused" not in history.render_record(lg, seeded["nofill"])


def test_the_short_record_stays_inside_its_budget(lg):
    """Every field at its cap and ten legs: the record is still a CONTEXT.md unit."""
    long = "x" * 4000
    aid = _attempt(lg, slot="slot:2026-08-29/01:00", status="placed", cell="director",
                   claim=f"## Markets\n{long}\n\n## Why this is profitable\n{long}\n",
                   hypothesis=f"## If we're right\n{long}\n\n## Kill criteria\n{long}\n")
    for i in range(10):
        lg.insert_bet(bet_id=f"{aid}-B{i:02d}", attempt_id=aid, ticket_index=i,
                      ticker=f"KXVERYLONGFAMILYNAME-26AUG29-T{i}", category="Politics",
                      side="yes", limit_price=D("0.40"), rationale="r", is_real=1,
                      status="settled", contracts=3, fill_price=D("0.40"), stake=D("1.20"),
                      fee=D("0.04"), outcome="win", pnl=D("1.74"),
                      client_order_id=f"{aid}-B{i:02d}")
    run = _director_run(lg, run_id="D-2026-08-29", run_date="2026-08-29")
    _review(lg, aid, "retrospective", run, rank=1, size=9, paragraph=long)
    text = history.render_record(lg, aid)
    assert len(text) <= history.SHORT_MAX
    assert text.endswith("…")


def test_the_full_record_carries_the_ticket_the_outcome_and_both_paragraphs(lg, seeded):
    text = history.render_record(lg, seeded["director"], full=True)
    assert "## edge_claim.md" in text and "## hypothesis.md" in text
    assert "## MANIFEST.md" in text
    assert "Retail reads the headline" in text        # the claim, verbatim
    assert "## Legs" in text and "KXHORMUZWEEKLY-25SEP05-T5" in text
    assert "+$0.72 on $1.24 staked · 1 of 2 legs won" in text
    assert "probed 14 markets in 3 families" in text and "3 bt past calls" in text
    assert "Swept the strait ladder" in text          # the session summary
    assert "prospective (rank 2 of 4, cohort 2026-08-29):" in text
    assert "retrospective (rank 1 of 4, cohort 2026-08-29):" in text


def test_the_full_record_of_an_old_attempt_says_what_is_missing(lg, seeded):
    text = history.render_record(lg, seeded["old"], full=True)
    assert "no activity row recorded" in text
    assert "none yet" in text          # no director paragraphs in the old era


def test_an_unknown_attempt_raises(lg, seeded):
    with pytest.raises(KeyError):
        history.render_record(lg, "A-9999")


# --------------------------------------------------------------------------- outcomes
def test_the_outcome_record_reconciles_with_the_bet_rows(lg, seeded):
    record = history.outcome_record(lg, seeded["director"])
    assert [leg["ticker"] for leg in record["legs"]] == [
        "KXHORMUZWEEKLY-25SEP05-T3", "KXHORMUZWEEKLY-25SEP05-T5",
    ]
    won = record["legs"][0]
    assert (won["price"], won["contracts"], won["status"]) == (D("0.6200"), 2, "settled")
    assert (won["outcome"], won["profit"], won["fee"]) == ("win", D("0.7200"), D("0.0400"))
    assert record["legs"][1]["profit"] is None
    rows = lg.bets_for_attempt(seeded["director"])
    assert record["totals"] == {
        "legs": len(rows), "wins": 1,
        "stake": sum((D(r["stake"]) for r in rows if r["stake"]), D("0")),
        "net": sum((D(r["pnl"]) for r in rows if r["pnl"]), D("0")),
    }
    assert record["families"] == [{"family": "KXHORMUZWEEKLY", "legs": 2, "wins": 1,
                                  "stake": D("1.2400"), "net": D("0.7200")}]


def test_a_pass_has_an_empty_outcome_record(lg, seeded):
    record = history.outcome_record(lg, seeded["pass"])
    assert record["legs"] == [] and record["families"] == []
    assert record["totals"] == {"legs": 0, "wins": 0, "stake": D("0"), "net": D("0")}


# --------------------------------------------------------------------------- completeness
def test_is_complete_on_every_case(lg, seeded):
    assert history.is_complete(lg, seeded["director"]) is True   # settled + scored refusal
    assert history.is_complete(lg, seeded["old"]) is True
    assert history.is_complete(lg, seeded["pass"]) is True       # no legs, session ended
    assert history.is_complete(lg, seeded["failed"]) is True     # nothing left to happen
    assert history.is_complete(lg, seeded["nofill"]) is True     # scored hypothetically
    assert history.is_complete(lg, seeded["open"]) is False      # a leg still unsettled
    assert history.is_complete(lg, "A-9999") is False


def test_an_unscored_refusal_is_not_complete(lg, seeded):
    aid = _attempt(lg, slot="slot:2026-08-31/01:00", status="placed", cell="static",
                   claim=CLAIM)
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1,
                  ticker="KXHORMUZWEEKLY-25SEP19-T3", category="Politics", side="no",
                  limit_price=D("0.50"), rationale="r", is_real=1, status="rejected",
                  reject_code="cap_daily", reject_reason="daily cap",
                  client_order_id=f"{aid}-B01")
    assert history.is_complete(lg, aid) is False
    lg.score_rejected_bet(f"{aid}-B01", outcome="loss", hypothetical_pnl=D("-0.50"),
                          scored_at="2026-09-01T05:00:00Z")
    assert history.is_complete(lg, aid) is True


def test_a_running_attempt_is_never_complete(lg):
    _seq, aid = lg.create_attempt(env="prod", model="m", effort="high", memory_mode="on",
                                  prompt_version="p", toolkit_version="0.1.0",
                                  workspace_path="/ws", slot="slot:2026-08-29/09:00")
    lg.transition(aid, "running")
    assert history.is_complete(lg, aid) is False


# --------------------------------------------------------------------------- recent
def test_recent_completed_is_newest_first_by_slot_day_then_time(lg, seeded):
    assert history.recent_completed(lg) == [
        seeded["pass"],      # 2026-08-29/03:40
        seeded["director"],  # 2026-08-29/01:00
        seeded["nofill"],    # 2026-08-28/22:20
        seeded["old"],       # 2026-07-08/10:00
    ]


def test_recent_completed_excludes_the_unfinished_and_the_ticketless_failure(lg, seeded):
    ids = history.recent_completed(lg)
    assert seeded["open"] not in ids        # still waiting on settlement
    assert seeded["failed"] not in ids      # complete, but not an example of anything


def test_an_unscored_refusal_holds_the_attempt_out_of_the_set(lg, seeded):
    """Arno's choice of 2026-09-21: an example whose legs have no outcomes yet is not an
    example, and the lag before settle scores a refusal is accepted."""
    aid = _attempt(lg, slot="slot:2026-08-31/01:00", status="placed", cell="static",
                   claim=CLAIM)
    lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1,
                  ticker="KXHORMUZWEEKLY-25SEP19-T3", category="Politics", side="no",
                  limit_price=D("0.50"), rationale="r", is_real=1, status="rejected",
                  declared_contracts=1, reject_code="cap_daily", reject_reason="daily cap",
                  client_order_id=f"{aid}-B01")
    assert history.is_complete(lg, aid) is False
    assert aid not in history.recent_completed(lg)


def test_recent_completed_honors_the_limit_and_the_era(lg, seeded):
    assert history.recent_completed(lg, limit=2) == [seeded["pass"], seeded["director"]]
    assert history.recent_completed(lg, era="live-v1") == [seeded["old"]]


def test_an_unsettled_record_does_not_read_as_a_finished_one(lg, seeded):
    text = history.render_record(lg, seeded["open"])
    assert "→ open" in text
    assert "Net: open (1 leg unsettled), +$0.00 so far on $0.55 staked" in text
    full = history.render_record(lg, seeded["open"], full=True)
    assert "open (1 leg unsettled), +$0.00 so far on $0.55 staked · 0 of 1 legs won" in full


def test_a_settled_record_still_states_its_net_plainly(lg, seeded):
    text = history.render_record(lg, seeded["director"])
    assert "Net: +$0.72 on $1.24 staked" in text
    assert "unsettled" not in text


# --------------------------------------------------------------------------- families
def test_families_counts_legs_wins_and_net_per_family(lg, seeded):
    rows = {r["family"]: r for r in history.families(lg)}
    strait = rows["KXHORMUZWEEKLY"]
    assert strait["attempts"] == 2 and strait["legs"] == 3
    assert strait["filled"] == 2 and strait["wins"] == 1
    assert strait["net"] == D("0.7200")
    assert strait["last_entry"] == "2026-08-30"
    assert rows["KXHIGHNY"]["attempts"] == 2 and rows["KXHIGHNY"]["wins"] == 0


def test_families_counts_passes_and_their_most_common_stated_reason(lg, seeded):
    """A pass has no legs, so it is counted against every family its ticket named."""
    for slot in ("slot:2026-08-30/03:40", "slot:2026-08-31/03:40"):
        _attempt(lg, slot=slot, status="no_bets", cell="static", claim=PASS_CLAIM)
    other = _attempt(lg, slot="slot:2026-09-01/03:40", status="no_bets", cell="static",
                     claim=PASS_CLAIM.replace("the book was one-sided at every strike",
                                              "the model agreed with the market"))
    assert other
    strait = {r["family"]: r for r in history.families(lg)}["KXHORMUZWEEKLY"]
    assert strait["passes"] == 4
    assert strait["pass_reason"] == (
        "Nothing cleared the bar: the book was one-sided at every strike."
    )


def test_a_pass_that_names_only_its_family_is_counted_against_it(lg, seeded):
    """A ticket that passed on a whole ladder names the series, not one of its strikes.

    The attribution pattern used to require a dashed tail, so `KXHIGHNY` on its own was
    read as no family at all and the pass was counted against nothing.
    """
    _attempt(lg, slot="slot:2026-08-31/06:20", status="no_bets", cell="static",
             claim="## Markets\nKXHIGHNY, the whole New York high ladder.\n\n"
                   "## Why this is profitable\nOne-sided at every strike; passed.\n")

    rows = {r["family"]: r for r in history.families(lg)}
    assert rows["KXHIGHNY"]["passes"] == 1
    assert rows["KXHIGHNY"]["pass_reason"] == "One-sided at every strike; passed."
    assert history.family(lg, "KXHIGHNY")["totals"]["passes"] == 1


def test_families_is_sorted_by_attempts_and_respects_the_limit(lg, seeded):
    """The two seeded families tie at two attempts each, so a third with a count of its
    own is what makes the order assertion able to fail."""
    for i in range(3):
        aid = _attempt(lg, slot=f"slot:2026-08-31/0{i + 1}:00", status="placed",
                       cell="static", claim=CLAIM)
        lg.insert_bet(bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1,
                      ticker=f"KXOPEC-26AUG31-T{i}", market_title="OPEC cut",
                      category="Politics", side="yes", limit_price=D("0.30"),
                      rationale="r", is_real=1, status="settled", contracts=1,
                      fill_price=D("0.30"), stake=D("0.30"), fee=D("0.02"),
                      outcome="loss", pnl=D("-0.32"), client_order_id=f"{aid}-B01")

    rows = history.families(lg)
    assert [r["family"] for r in rows] == ["KXOPEC", "KXHORMUZWEEKLY", "KXHIGHNY"]
    assert [r["attempts"] for r in rows] == [3, 2, 2]
    assert [r["family"] for r in history.families(lg, limit=1)] == ["KXOPEC"]


def test_families_filters_by_era_category_outcome_and_since(lg, seeded):
    assert [r["family"] for r in history.families(lg, era="live-v1")] == ["KXHIGHNY"]
    assert [r["family"] for r in history.families(lg, category="Weather")] == ["KXHIGHNY"]
    assert [r["family"] for r in history.families(lg, outcome="win")] == ["KXHORMUZWEEKLY"]
    assert [r["family"] for r in history.families(lg, outcome="nofill")] == ["KXHIGHNY"]
    assert [r["family"] for r in history.families(lg, since="2026-08-29")] == [
        "KXHORMUZWEEKLY"
    ]


def test_a_family_the_exchange_refused_answers_to_the_refused_filter(lg, seeded):
    """A refusal the exchange sent is stored as a no-fill, so `--outcome refused` used to
    miss the 68 legs that most needed finding. It answers to both filters now: something
    turned the leg down, and nothing was filled."""
    _blocked(lg)
    assert "KXPRESPARTY" in {r["family"] for r in history.families(lg, outcome="refused")}
    assert "KXPRESPARTY" in {r["family"] for r in history.families(lg, outcome="nofill")}
    # The harness's own refusals are still refusals, and a plain no-fill is still not one.
    assert "KXHORMUZWEEKLY" in {r["family"] for r in history.families(lg, outcome="refused")}
    assert "KXHIGHNY" not in {r["family"] for r in history.families(lg, outcome="refused")}


# --------------------------------------------------------------------------- one family
def test_family_lists_every_entry_oldest_first_with_its_totals(lg, seeded):
    data = history.family(lg, "KXHORMUZWEEKLY")
    assert [e["day"] for e in data["entries"]] == ["2026-08-29", "2026-08-30"]
    first = data["entries"][0]
    assert first["attempt_id"] == seeded["director"] and first["cell"] == "director"
    assert first["claim"].startswith("The strait has stayed open")
    assert first["legs"][1]["fill"].startswith("refused: daily cap")
    assert first["net"] == D("0.7200") and first["stake"] == D("1.2400")
    assert data["totals"] == {"attempts": 2, "legs": 3, "filled": 2, "wins": 1,
                              "stake": D("1.7900"), "net": D("0.7200"), "passes": 1}


def test_an_unentered_family_is_empty(lg, seeded):
    data = history.family(lg, "KXNOSUCH")
    assert data["entries"] == [] and data["totals"]["attempts"] == 0


# --------------------------------------------------------------------------- search
def test_search_returns_one_record_per_hit_newest_first(lg, seeded):
    rows = history.search(lg, "strait")
    assert [r["attempt_id"] for r in rows][0] == seeded["open"]     # 2026-08-30
    assert {r["kind"] for r in rows} <= {"claim", "hypothesis", "closing"}
    assert rows[0]["record"].startswith(seeded["open"])
    assert "[strait]" in rows[0]["snippet"] or "strait" in rows[0]["snippet"]


def test_search_never_returns_the_old_graders_kinds(lg, seeded):
    """`retro`, `summary` and `tags` are in the index and are never served."""
    assert lg.conn.execute(
        "SELECT COUNT(*) AS n FROM ledger_fts WHERE content LIKE '%hindenburg%'"
    ).fetchone()["n"] == 3
    assert history.search(lg, "hindenburg") == []


def test_search_matches_the_content_and_not_the_kind_column(lg, seeded):
    """`bt past search claim` used to return every claim in the ledger.

    The index carries the kind beside the text and the MATCH named no column, so the
    query read both and any search for one of the kind words answered with the whole
    corpus. Only the rows whose text says it come back.
    """
    aid = _attempt(lg, slot="slot:2026-09-02/01:00", status="no_bets", cell="static",
                   claim=PASS_CLAIM.replace("Nothing cleared the bar",
                                            "The claim we were testing died"))

    rows = history.search(lg, "claim")

    assert [(r["attempt_id"], r["kind"]) for r in rows] == [(aid, "claim")]


def test_search_reads_the_closing_paragraph(lg, seeded):
    rows = history.search(lg, "refused")
    assert [(r["attempt_id"], r["kind"]) for r in rows] == [
        (seeded["director"], "closing")
    ]


def test_search_applies_the_shared_filters(lg, seeded):
    assert history.search(lg, "strait", era="live-v1") == []
    assert {r["attempt_id"] for r in history.search(lg, "ladder", category="Weather")} == {
        seeded["nofill"], seeded["old"],
    }
    assert history.search(lg, "  ") == []
    assert len(history.search(lg, "strait", limit=1)) == 1


# --------------------------------------------------------------------------- era default
def test_era_default_flips_at_fifty_current_era_attempts(lg, settings, seeded):
    assert settings.history.current_era == "live-v2"
    assert settings.history.era_default_min == 50
    assert history.era_default(lg, settings) == "all"

    already = lg.conn.execute(
        "SELECT COUNT(*) AS n FROM attempts WHERE era='live-v2'"
    ).fetchone()["n"]
    for i in range(49 - already):
        _attempt(lg, slot=f"slot:2026-09-01/{i % 24:02d}:00", status="no_bets",
                 cell="static", claim=PASS_CLAIM)
    assert history.era_default(lg, settings) == "all"     # forty-nine is not yet fifty

    _attempt(lg, slot="slot:2026-09-02/01:00", status="no_bets", cell="static",
             claim=PASS_CLAIM)
    assert history.era_default(lg, settings) == "current"


# --------------------------------------------------------------------------- the director
def test_latest_valid_run_takes_the_newest_valid_page(lg, seeded):
    _director_run(lg, run_id="D-2026-08-31", run_date="2026-08-31", status="invalid",
                  page="## Standing direction\nnever stored")
    _director_run(lg, run_id="D-2026-08-28", run_date="2026-08-28",
                  page="## Standing direction\nan older page")
    run = history.latest_valid_run(lg)
    assert run["run_id"] == "D-2026-08-30"
    assert history.latest_valid_run(lg, on_or_before="2026-08-29")["run_id"] == "D-2026-08-28"
    assert history.latest_valid_run(lg, on_or_before="2026-08-01") is None


def test_latest_valid_run_is_none_on_an_empty_table(lg):
    assert history.latest_valid_run(lg) is None


def test_reviews_for_returns_both_paragraphs_with_their_ranks(lg, seeded):
    reviews = history.reviews_for(lg, seeded["director"])
    assert reviews["prospective"]["rank"] == 2 and reviews["prospective"]["cohort_size"] == 4
    assert reviews["retrospective"]["rank"] == 1
    assert reviews["retrospective"]["cohort_date"] == "2026-08-29"
    assert history.reviews_for(lg, seeded["pass"]) == {"prospective": None,
                                                       "retrospective": None}


def test_an_allowance_refusal_renders_like_a_cap_refusal():
    """``cap_attempt`` (2026-09-27) takes the same path as ``cap_daily``: the row's own
    sentence after "refused:", with the declared size."""
    bet = {"ticker": "KXA-T3", "side": "no", "fill_price": None, "limit_price": D("0.94"),
           "contracts": None, "declared_contracts": 3, "status": "rejected",
           "reject_code": "cap_attempt", "outcome": None, "pnl": None,
           "reject_reason": "attempt allowance: $7.5000 of $8.0000 already committed by "
                            "this attempt, this leg needed $2.8200"}
    assert history._leg_line(bet) == (
        "KXA-T3 no @0.9400 ×3 → refused: attempt allowance: $7.5000 of $8.0000 already "
        "committed by this attempt, this leg needed $2.8200"
    )
