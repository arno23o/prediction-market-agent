"""The status digest (docs/22 section 10).

One seeded ledger, one render, and then every section asserted against what was seeded:
attempts across cells and statuses, legs that filled and legs that were refused with a
reason, a reconciliation row, a personal order, slot markers, sessions with a cost. The
money on the page has to add up against the rows, because this page is what a person
reads instead of the ledger.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from betting_agent import board
from betting_agent.config import load_settings
from betting_agent.harness.digest import status_digest, status_log_line
from betting_agent.kalshi.types import Market
from betting_agent.ledger.db import Ledger

DAY = "2026-07-07"
# Every seeded timestamp lands inside the Eastern day above.
MORNING = "2026-07-07T14:00:00Z"
EVENING = "2026-07-07T22:00:00Z"
NOW = datetime(2026, 7, 7, 22, 30, tzinfo=UTC)


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


class _Balance:
    dollars = D("61.25")


class _Client:
    def get_balance(self):
        return _Balance()


def _attempt(lg, *, cell, status, slot=None, created_at=MORNING):
    _seq, aid = lg.create_attempt(
        env="prod", model="claude-opus-5", effort="high", memory_mode="on",
        prompt_version="p", toolkit_version="0.1.0", workspace_path="/ws",
        slot=slot, cell=cell,
    )
    lg.conn.execute("UPDATE attempts SET created_at=? WHERE attempt_id=?",
                    (created_at, aid))
    lg.conn.commit()
    lg.transition(aid, "running")
    if status != "running":
        lg.transition(aid, status)
    return aid


@pytest.fixture
def seeded(lg, settings):
    """One day's world: two cells, a filled leg, two refusals, a settlement, a skip."""
    static = _attempt(lg, cell="static", status="placed", slot="slot:2026-07-07/10:00")
    lg.insert_bet(bet_id=f"{static}-B01", attempt_id=static, ticket_index=1, ticker="KXA",
                  market_title="A", category="Weather", side="yes", limit_price=D("0.40"),
                  model_prob=D("0.55"), rationale="r", is_real=1, status="settled",
                  contracts=2, fill_price=D("0.40"), stake=D("0.80"), fee=D("0.04"),
                  order_id="o1", client_order_id=f"{static}-B01", outcome="win",
                  pnl=D("1.16"), placed_at=MORNING, settled_at=EVENING)
    lg.insert_bet(bet_id=f"{static}-B02", attempt_id=static, ticket_index=2, ticker="KXB",
                  market_title="B", category="Politics", side="no", limit_price=D("0.30"),
                  model_prob=D("0.50"), rationale="r", is_real=1, status="rejected",
                  reject_code="cap_daily",
                  reject_reason="daily cap: $9.80 of $10.00 already committed today")
    lg.insert_bet(bet_id=f"{static}-B03", attempt_id=static, ticket_index=3, ticker="KXC",
                  market_title="C", category="Politics", side="yes", limit_price=D("0.20"),
                  model_prob=D("0.30"), rationale="r", is_real=1, status="rejected",
                  reject_code="drawdown_floor",
                  reject_reason="drawdown floor: live balance 20.00 is below the floor")

    _attempt(lg, cell=None, status="no_bets", slot="slot:2026-07-07/13:00")
    _attempt(lg, cell="baseline", status="failed", slot="slot:2026-07-07/16:00")

    lg.insert_session(session_id="S1", attempt_id=static, kind="attempt",
                      model="claude-opus-5", started_at=MORNING)
    lg.finish_session("S1", ended_at=EVENING, exit="ok", num_turns=12,
                      cost_usd=D("11.94"), input_tokens=1, output_tokens=2,
                      wall_seconds=600)

    lg.meta_set("live_genesis_ts", "2026-07-01T00:00:00Z")
    lg.meta_set("live_genesis_balance", "50.1622")
    lg.meta_set("last_tick_ts", "2026-07-07T22:15:00Z")
    lg.insert_reconciliation("2026-07-07T23:05:00Z", expected_balance=D("60.00"),
                             actual_balance=D("60.25"), drift=D("0.25"), ok=0,
                             detail=json.dumps({"verdict": "small"}))
    lg.upsert_personal_order("po-1", ticker="KXZ", side="yes",
                             created_time="2026-07-02T10:00:00Z", contracts=D("1"),
                             cost=D("0.9991"), fee=D("0.0100"), fee_source="computed",
                             first_seen_at="2026-07-02T11:00:00Z")
    lg.insert_credit(credited_at="2026-07-03T04:45:00Z", amount=D("0.01"),
                     kind="incentive", reason="Volume Incentive For Event KXRAINDNYC",
                     recorded_at="2026-07-04T12:00:00Z")
    lg.insert_credit(credited_at="2026-06-01T04:45:00Z", amount=D("5.00"),
                     kind="incentive", recorded_at="2026-07-04T12:00:00Z")   # pre-genesis
    lg.audit("slot_skipped", detail={"slot": "slot:2026-07-07/05:00",
                                     "reason": "past_grace_sleep"})
    lg.audit("slot_spawn_lost", detail={"slot": "slot:2026-07-07/08:00"})
    return static


def _render(lg, settings, client=None, day=DAY):
    """The page as of the seeded instant.

    ``now`` matters since the review: with no ``meta.last_digest_ts`` the "since the
    previous digest" windows start 24 hours back, so a test that let it default to the
    wall clock would be asking about a window three months after its own fixtures.
    """
    return status_digest(lg, settings, client, day=day, now=NOW)


def _section(text: str, heading: str) -> str:
    body = text.split(f"## {heading}", 1)[1]
    return body.split("\n## ", 1)[0]


def test_the_page_carries_every_section_in_order_and_stays_under_sixty_lines(lg, settings,
                                                                            seeded):
    text = _render(lg, settings)

    headings = [ln[3:] for ln in text.splitlines() if ln.startswith("## ")]
    assert headings == [
        "Account", "The day", "The board", "Fills and refusals by category",
        "Settlements since the previous digest", "Compute", "Liveness", "Invariants",
    ]
    assert text.startswith(f"# Status {DAY}")
    assert len(text.splitlines()) < 60


def test_the_account_section_reads_the_reconciliations_verdict_and_the_personal_orders(
    lg, settings, seeded,
):
    section = _section(_render(lg, settings), "Account")

    assert "- reconciliation: small, drift $0.25, 2026-07-07T23:05:00Z" in section
    # A row from before 2026-09-27 says "small", which was never absorbed.
    assert "- absorbed residual: $0.0000 over 0 night(s) (30d: $0.0000 over 0)" in section
    assert "- balance: $60.25 (last reconciliation)" in section
    # cost 0.9991 + fee 0.0100 - no payout, rounded to the cent.
    assert "- personal orders since genesis: 1, net cost $1.01" in section
    # The exchange's incentive credits, beside the personal orders: the same kind of fact,
    # money in this account with no bet of ours behind it. The $5.00 credit predates
    # genesis and is already inside the anchor, so it is not counted twice.
    assert "- exchange credits since genesis: 1, $0.01" in section
    assert "- halt: no" in section


def test_the_account_section_carries_the_absorbed_residual(lg, settings, seeded):
    """The walk's term, signed, and the watch's trailing window, in absolute value, so a
    pair of drifts that cancel in the walk still show up where the watch counts them.
    Four decimals: an absorbed drift is usually under a cent."""
    for run_at, drift in (("2026-05-01T03:05:00Z", "0.0101"),      # outside 30 days
                          ("2026-07-05T03:05:00Z", "-0.0003"),
                          ("2026-07-06T03:05:00Z", "0.0002")):
        lg.insert_reconciliation(run_at, expected_balance=D("60.00"),
                                 actual_balance=D("60.00") + D(drift), drift=D(drift),
                                 ok=0, detail=json.dumps({"verdict": "absorbed"}))
    lg.meta_set("live_genesis_ts", "2026-04-01T00:00:00Z")

    section = _section(_render(lg, settings), "Account")

    assert "- absorbed residual: $0.0100 over 3 night(s) (30d: $0.0005 over 2)" in section


def test_a_client_puts_a_live_balance_on_the_account_line(lg, settings, seeded):
    section = _section(_render(lg, settings, _Client()), "Account")
    assert "- balance: $61.25 (live)" in section


def test_a_halt_is_named_with_its_reason(lg, settings, seeded):
    settings.halt_path.parent.mkdir(parents=True, exist_ok=True)
    settings.halt_path.write_text("reconcile_drift\n2026-07-07T23:06:00Z\n")
    section = _section(_render(lg, settings), "Account")
    assert "- halt: YES (reconcile_drift)" in section


def test_open_positions_count_real_legs_only(lg, settings, seeded):
    """A paper fill never left the account, so it is not money committed at the exchange.

    ``filled_unsettled_bets`` carries both, because settle walks paper legs too; the
    filter belongs to this line, not to the DAO.
    """
    lg.insert_bet(bet_id=f"{seeded}-B04", attempt_id=seeded, ticket_index=4, ticker="KXD",
                  category="Weather", side="yes", limit_price=D("0.50"),
                  model_prob=D("0.60"), rationale="r", is_real=1, status="filled",
                  contracts=3, fill_price=D("0.50"), stake=D("1.50"), fee=D("0.07"),
                  order_id="o2", placed_at=MORNING)
    lg.insert_bet(bet_id=f"{seeded}-B05", attempt_id=seeded, ticket_index=5, ticker="KXE",
                  category="Weather", side="yes", limit_price=D("0.50"),
                  model_prob=D("0.60"), rationale="r", is_real=0, status="filled",
                  contracts=9, fill_price=D("0.50"), stake=D("4.50"), fee=D("0.21"),
                  placed_at=MORNING)

    section = _section(_render(lg, settings), "Account")

    assert "- open positions: 1 real leg(s), $1.57 committed" in section


def test_the_day_groups_attempts_by_cell_and_status_with_null_as_none(lg, settings, seeded):
    section = _section(_render(lg, settings), "The day")

    assert "- attempts, static: placed=1" in section
    assert "- attempts, baseline: failed=1" in section
    assert "- attempts, (none): no_bets=1" in section


def test_the_day_counts_the_legs_and_names_the_refusal_reasons(lg, settings, seeded):
    section = _section(_render(lg, settings), "The day")

    assert "- legs: proposed 3, sent 1, accepted 1, filled 1, refused 2" in section
    assert "daily cap: $9.80 of $10.00 already committed today (1)" in section
    assert "drawdown floor: live balance 20.00 is below the floor (1)" in section


def test_legs_held_back_for_money_count_as_refused_though_nothing_was_sent(lg, settings,
                                                                          seeded):
    """Execute writes a cap or allowance refusal with ``is_real = 0``, because no order
    left; the funnel still counts it, and the allowance's reasons are named."""
    for idx, code, reason in (
        (4, "cap_attempt", "attempt allowance: $8.0000 of $8.0000 already committed by "
                           "this attempt, this leg needed $0.9400"),
        (5, "V16", "this ticket stakes $9.10 in total at its limit prices"),
        (6, "V11", "the order book could not be read"),        # a validation refusal
    ):
        lg.insert_bet(bet_id=f"{seeded}-B0{idx}", attempt_id=seeded, ticket_index=idx,
                      ticker=f"KX{idx}", category="Weather", side="no",
                      limit_price=D("0.94"), rationale="r", is_real=0, status="rejected",
                      reject_code=code, reject_reason=reason, placed_at=None)

    section = _section(_render(lg, settings), "The day")

    assert "- legs: proposed 6, sent 1, accepted 1, filled 1, refused 4" in section
    assert "attempt allowance: $8.0000 of $8.0000" in section


def test_the_funnel_below_proposed_describes_real_legs_only(lg, settings, seeded):
    """One population per stage. A paper leg was proposed and nothing after that: it was
    never sent, never accepted, never filled and nobody refused it."""
    lg.insert_bet(bet_id=f"{seeded}-B04", attempt_id=seeded, ticket_index=4, ticker="KXD",
                  category="Weather", side="yes", limit_price=D("0.50"),
                  model_prob=D("0.60"), rationale="r", is_real=0, status="filled",
                  contracts=1, fill_price=D("0.50"), stake=D("0.50"), fee=D("0.02"),
                  placed_at=MORNING)

    section = _section(_render(lg, settings), "The day")

    assert "- legs: proposed 4, sent 1, accepted 1, filled 1, refused 2" in section


def test_the_day_shows_contracts_by_size_and_the_stake_against_the_cap(lg, settings, seeded):
    section = _section(_render(lg, settings), "The day")

    assert "- contracts placed: 2x1" in section
    # The one filled leg staked 0.80 against the configured daily cap.
    assert "- stake committed: $0.80 of $25.00" in section


def test_the_board_says_none_rather_than_a_placeholder_before_phase_two(lg, settings,
                                                                       seeded):
    section = _section(_render(lg, settings), "The board")
    assert "- generation: none" in section
    assert "- exchange refusal text: none" in section


def test_the_board_reports_the_generation_and_what_the_header_says_is_excluded(lg, settings,
                                                                              seeded):
    """A generation with no header at all: the scope bullet says none rather than nothing."""
    market = Market(ticker="KXA-26JUL07-B1", title="A", status="open", close_time=NOW)
    board.write_generation(settings.board_dir, iter([market]), captured_at=NOW)
    lg.meta_set("category_refusal_text", "Residents of this state may not trade this category.")

    section = _section(_render(lg, settings), "The board")

    assert "snapshot:" in section and "1 markets" in section
    assert "- scope: none" in section
    assert "Residents of this state" in section


def test_the_two_board_lines_land_on_one_bullet_each(lg, settings, seeded):
    """The label is two lines and the page has two bullets for it.

    Folded into one, the scope line was printed mid-sentence and its market count landed
    after the exclusions; both facts are on the page, once each, where they belong.
    """
    market = Market(ticker="KXA-26JUL07-B1", title="A", status="open", close_time=NOW)
    board.write_generation(
        settings.board_dir, iter([market]), captured_at=NOW,
        close_bound_hours=120, excluded_series=["KXMVECROSS"],
        excluded_categories=["Sports", "Entertainment"],
        category_refusal_text="Residents of this state may not trade this category.",
    )

    lines = _section(_render(lg, settings), "The board").splitlines()

    generation = next(line for line in lines if line.startswith("- generation:"))
    scope = next(line for line in lines if line.startswith("- scope:"))
    assert generation.startswith("- generation: snapshot:") and generation.endswith("1 markets")
    assert scope == (
        "- scope: bound: markets closing within 120 hours; "
        "excluded series: KXMVECROSS; "
        "excluded categories: Sports, Entertainment (0 removed)"
    )


def test_fills_and_refusals_split_by_category_over_the_day_and_the_week(lg, settings,
                                                                       seeded):
    section = _section(_render(lg, settings),
                       "Fills and refusals by category")

    assert "| Weather | 1 | 0 | 1 | 0 |" in section
    assert "| Politics | 0 | 2 | 0 | 2 |" in section


def test_settlements_since_the_previous_digest_count_legs_wins_and_net(lg, settings, seeded):
    lg.meta_set("last_digest_ts", "2026-07-07T12:00:00Z")

    section = _section(_render(lg, settings),
                       "Settlements since the previous digest")

    assert "- legs 1, wins 1, net $1.16" in section
    # The cohort still holds two refused legs nobody has scored counterfactually.
    assert "- cohorts complete: none" in section


def test_a_cohort_is_complete_once_every_refused_leg_has_been_scored(lg, settings, seeded):
    lg.conn.execute(
        "UPDATE bets SET hypothetical_scored_at=? WHERE status='rejected'", (EVENING,)
    )
    lg.conn.commit()

    section = _section(_render(lg, settings),
                       "Settlements since the previous digest")

    assert "- cohorts complete: 2026-07-07" in section


def test_the_first_digest_looks_back_a_day_and_not_to_the_beginning_of_time(
    lg, settings, seeded,
):
    """With no ``meta.last_digest_ts`` there is no previous digest to measure from.

    Treating that as "since forever" made the first page list the ledger's whole lifetime
    of settlements and every slot ever missed, which is a wall of history where a day's
    news should be. The floor is 24 hours, the cadence the page is written at.
    """
    assert lg.meta_get("last_digest_ts") is None
    lg.insert_bet(bet_id=f"{seeded}-B09", attempt_id=seeded, ticket_index=9, ticker="KXOLD",
                  category="Weather", side="yes", limit_price=D("0.40"),
                  model_prob=D("0.55"), rationale="r", is_real=1, status="settled",
                  contracts=1, fill_price=D("0.40"), stake=D("0.40"), fee=D("0.02"),
                  order_id="o9", outcome="win", pnl=D("0.58"),
                  placed_at="2026-05-01T14:00:00Z", settled_at="2026-05-02T20:00:00Z")

    section = _section(_render(lg, settings),
                       "Settlements since the previous digest")

    # Only the seeded leg that settled last night, never the one from May.
    assert "- legs 1, wins 1, net $1.16" in section


def test_a_settlement_before_the_previous_digest_is_out_of_the_window(lg, settings, seeded):
    lg.meta_set("last_digest_ts", "2026-07-07T23:00:00Z")

    section = _section(_render(lg, settings),
                       "Settlements since the previous digest")

    assert section.strip() == "- none"


def test_compute_reports_sessions_by_kind_and_the_cost_per_settled_leg(lg, settings, seeded):
    section = _section(_render(lg, settings), "Compute")

    assert "- sessions today: attempt 1 ($11.94)" in section
    assert "- cost per settled leg (7d): $11.94 ($11.94 over 1 leg(s))" in section


def _session(lg, sid, *, kind="attempt", model, effort=None, cost):
    """A finished session on the seeded day, with an attempt row when it is an attempt's."""
    aid = None
    if kind == "attempt":
        _seq, aid = lg.create_attempt(
            env="prod", model=model, effort=effort, memory_mode="on", prompt_version="p",
            toolkit_version="0.1.0", workspace_path="/ws",
        )
    lg.insert_session(session_id=sid, attempt_id=aid, kind=kind, model=model,
                      started_at=MORNING)
    lg.finish_session(sid, ended_at=EVENING, exit="ok", num_turns=1, cost_usd=D(cost),
                      input_tokens=1, output_tokens=1, wall_seconds=60)


def test_compute_groups_the_days_attempt_sessions_by_model_and_effort(lg, settings,
                                                                      seeded):
    """The seeded Opus high session plus one Fable max and two Opus max. The model is the
    session row's, which is what ran; the effort is the attempt row's."""
    _session(lg, "S2", model="claude-fable-5-1", effort="max", cost="21.50")
    _session(lg, "S3", model="claude-opus-5", effort="max", cost="9.10")
    _session(lg, "S4", model="claude-opus-5", effort="max", cost="8.00")
    _session(lg, "D1", kind="director", model="claude-fable-5-1", cost="12.00")

    section = _section(_render(lg, settings), "Compute")

    assert "- sessions today: attempt 4 ($50.54), director 1 ($12.00)" in section
    assert ("- attempts by model and effort: claude-fable-5-1 max 1 ($21.50), "
            "claude-opus-5 high 1 ($11.94), claude-opus-5 max 2 ($17.10)") in section


def test_liveness_names_the_missed_slots_with_their_marker_reason(lg, settings, seeded):
    section = _section(_render(lg, settings), "Liveness")

    assert "- last tick: 2026-07-07T22:15:00Z" in section
    assert "slot:2026-07-07/05:00 skipped (past_grace_sleep)" in section
    assert "slot:2026-07-07/08:00 lost (lost)" in section


def test_the_invariants_section_prints_one_line_per_check(lg, settings, seeded):
    section = _section(_render(lg, settings), "Invariants")

    assert "- fts_integrity: PASS" in section
    assert "- no_orphan_bets: PASS" in section
    assert len([ln for ln in section.splitlines() if ln.startswith("- ")]) == 6


def test_a_failing_invariant_is_printed_with_its_failing_rows(lg, settings, seeded):
    lg.conn.execute("PRAGMA foreign_keys=OFF")
    lg.conn.execute(
        "INSERT INTO bets (bet_id, attempt_id, ticket_index, ticker, side, limit_price, "
        "rationale, status) VALUES ('X-B01','A-9999',1,'T','yes','0.4000','r','no_fill')"
    )
    lg.conn.commit()

    section = _section(_render(lg, settings), "Invariants")

    assert "- no_orphan_bets: FAIL, 1 orphan bets: X-B01" in section


def test_an_empty_ledger_renders_every_section_without_placeholders(lg, settings):
    text = _render(lg, settings)

    assert "- reconciliation: none recorded" in text
    assert "- balance: unknown (no reconciliation yet)" in text
    assert "- attempts: none" in text
    assert "- none in the last seven days" in text
    assert "- sessions today: none" in text
    assert "- last tick: never" in text
    assert "- missed slots: none" in text
    assert len(text.splitlines()) < 60


def test_a_read_only_ledger_skips_the_fts_check_instead_of_failing_it(settings, seeded,
                                                                      lg):
    """``betting-agent status`` opens the ledger read-only, and FTS5's integrity-check is
    issued as a write, so the page used to report a corrupt index on every single run."""
    ro = Ledger.open(settings.ledger_path, readonly=True)
    try:
        section = _section(_render(ro, settings), "Invariants")
    finally:
        ro.close()

    assert "- fts_integrity: SKIP (read-only connection" in section
    assert "fts_integrity: FAIL" not in section
    assert "readonly database" not in section
    # The writable handle still runs it for real, which is where a real corruption shows.
    assert "- fts_integrity: PASS" in _section(_render(lg, settings), "Invariants")


def test_the_log_line_carries_the_five_fields_the_tick_greps_for(lg, settings, seeded):
    line = status_log_line(lg, settings, DAY, now=NOW)

    assert line.startswith("2026-07-07T22:30:00Z ")
    assert "balance=$60.25" in line
    assert "open=0" in line
    assert "attempts=3" in line
    assert "placed=1" in line
    assert line.endswith("halted=no")
