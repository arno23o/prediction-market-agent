"""Order execution (docs/22 sections 5.6 and 7.6): paper and real fills, the live gate,
the caps against the ticket's own sizes, OrderAmbiguous resolution, row and audit
completeness, and the money-path integrity properties of WP0, rows durable before the
next order, mid-ticket errors survived, HALT honored mid-placement, exact fractional
counts, one placement clock."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from betting_agent.config import Settings
from betting_agent.harness import execute as execute_mod
from betting_agent.harness import reconcile, safety, settle
from betting_agent.harness.execute import execute_attempt
from betting_agent.harness.notify import streak_state
from betting_agent.harness.validate import BetSpec, ParsedTicket, validate_ticket
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.ledger.db import Ledger
from betting_agent.moneymath import fee as fee_calc
from betting_agent.moneymath import q4
from betting_agent.timeutil import et_day, iso, parse_iso, utc_now

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)
GOOD_EDGE = "\n".join(
    ["## Markets", "m", "## Why this is profitable", "e",
     "## Why the opportunity exists and persists", "w"]
)
GOOD_HYP = "\n".join(
    ["## If we're right", "r", "## If we're wrong", "x", "## Kill criteria", "k"]
)


# --------------------------------------------------------------------------- helpers
def _settings(tmp_path, *, env="prod", live_trading=False, **over):
    s = Settings()
    s._root = tmp_path
    s.kalshi.env = env
    s.stakes.live_trading = live_trading
    for key, val in over.items():
        obj = s
        parts = key.split(".")
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], val)
    return s


def _bet(idx, ticker, *, side="yes", limit="0.4000", contracts=1, resolution_event=None):
    return BetSpec(idx, ticker, side, D(limit), contracts, "why", resolution_event)


def _parsed(bets, *, attempt="A-0001"):
    return ParsedTicket(attempt, bets, GOOD_EDGE, GOOD_HYP, "m", [])


def _add(fake, ticker, *, yes_ask=None, yes_size=0, no_ask=None, no_size=0,
         status="active", category=None):
    fake.add_market(
        ticker, title=ticker, category=category, close_time=NOW + timedelta(hours=1),
        yes_ask=D(yes_ask) if yes_ask is not None else None, yes_ask_size=yes_size,
        no_ask=D(no_ask) if no_ask is not None else None, no_ask_size=no_size, status=status,
    )


def _validate(fake, parsed, settings):
    return validate_ticket(parsed, fake.get_market, fake.get_orderbook, settings, NOW)


@pytest.fixture
def lg(tmp_path):
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    yield ledger
    ledger.close()


def _attempt(lg, edge_class="probability"):
    _seq, aid = lg.create_attempt(
        env="prod", model="m", effort="high", memory_mode="on",
        prompt_version="p", toolkit_version="0.1.0", workspace_path="/w", edge_class=edge_class,
    )
    return aid


def _rows(lg, aid):
    return {r["ticket_index"]: r for r in lg.bets_for_attempt(aid)}


# --------------------------------------------------------------------------- paper
def test_paper_fill_at_best_ask(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)  # ask better than the 0.40 limit
    s = _settings(tmp_path)
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)
    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "placed"
    row = _rows(lg, aid)[1]
    assert row["status"] == "filled"
    assert D(row["fill_price"]) == D("0.3800")  # filled at best_ask, not the limit
    assert row["contracts"] == 1  # the size the ticket declared
    assert D(row["stake"]) == D("0.3800")  # stake == fill price
    assert row["is_real"] == 0
    assert fake.orders_placed == []  # gate off -> no real orders


def test_paper_no_fill_when_ask_above_limit(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.45", yes_size=50)  # worse than the 0.40 limit
    s = _settings(tmp_path)
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)
    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "no_bets"
    row = _rows(lg, aid)[1]
    assert row["status"] == "no_fill"
    assert row["fill_price"] is None
    assert row["stake"] is None


def test_resolution_event_persists_to_the_ledger_row(tmp_path, lg):
    """docs/14 D7: the declared event rides the leg all the way from bets.json (via
    BetSpec, unexamined by validation) to the ``bets`` row. A leg that declares nothing
    keeps writing NULL, exactly as every ticket before D7 did."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    s = _settings(tmp_path)
    aid = _attempt(lg)
    out = _validate(fake, _parsed([
        _bet(1, "A", resolution_event="OWGR-2026-08-03"),
        _bet(2, "B", limit="0.3000"),
    ]), s)
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert rows[1]["resolution_event"] == "OWGR-2026-08-03"
    assert rows[2]["resolution_event"] is None


# ------------------------------------------------------------- sizing from the ticket
@pytest.mark.parametrize("contracts", [1, 2, 3])
def test_the_declared_size_is_what_is_ordered_staked_and_paid(tmp_path, lg, contracts):
    """docs/22 section 7.6: the ticket declares the size and nothing recomputes it. The
    row, the order and the exchange's own balance trail must agree to the cent."""
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A", contracts=contracts)]), s)
    before = fake.balance

    assert execute_attempt(aid, out, lg, fake, s) == "placed"

    row = _rows(lg, aid)[1]
    assert D(str(row["contracts"])) == D(contracts)
    assert fake.orders_placed[0]["count"] == D(contracts)
    assert D(row["stake"]) == D(contracts) * D("0.38")
    assert D(row["fee"]) == fee_calc(D(contracts), D("0.38"), D("0.07"))
    # the money that actually left the account is what the row says, to the cent
    moves = [e for e in fake.balance_ledger() if e["reason"] == "fill"]
    assert len(moves) == 1
    assert -moves[0]["delta"] == D(row["stake"]) + D(row["fee"])
    assert before - fake.balance == D(row["stake"]) + D(row["fee"])
    assert lg.daily_real_spend(et_day(utc_now())) == D(row["stake"])


def test_the_caps_are_charged_the_declared_size_not_one_contract(tmp_path, lg):
    """A 3-contract bet consumes three contracts' worth of the daily cap, projected at
    the limit price."""
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.38", yes_size=50)
    # 3 x 0.40 = 1.20 admitted, then 1 x 0.40 would make 1.60 > 1.50
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D("1.50")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A", contracts=3), _bet(2, "B")]), s)
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert rows[1]["is_real"] == 1 and D(rows[1]["stake"]) == D("1.14")  # 3 x 0.38 filled
    assert rows[2]["reject_code"] == "cap_daily"
    detail = json.loads(lg.audit_events(event="cap_stop")[0]["detail"])
    assert detail["unit_stake"] == "0.4000"       # the refused leg's own projection
    assert detail["day_headroom"] == "0.3000"     # 1.50 cap - 1.20 projected and committed


# --------------------------------------------------------------------------- live gate
def test_live_gate_off_all_paper(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=90)
    s = _settings(tmp_path, env="prod", live_trading=False)  # gate off
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)
    execute_attempt(aid, out, lg, fake, s)

    assert fake.orders_placed == []
    assert all(r["is_real"] == 0 for r in lg.bets_for_attempt(aid))


def test_real_orders_when_demo(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo")  # demo -> real orders allowed
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)
    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "placed"
    assert len(fake.orders_placed) == 1
    assert fake.orders_placed[0]["client_order_id"] == f"{aid}-B01"
    assert fake.orders_placed[0]["time_in_force"] == "ioc"
    row = _rows(lg, aid)[1]
    assert row["is_real"] == 1
    assert row["status"] == "filled"
    assert row["order_id"] is not None
    # order_placed + order_result audited
    assert lg.audit_events(event="order_placed")
    assert lg.audit_events(event="order_result")


# --------------------------------------------------------------------------- all-real
def test_gate_open_makes_every_validated_bet_real_in_ticket_order(tmp_path, lg):
    """Jul29 spec L4: no real subset any more — every validated bet is real, and the
    placement order is the pre-registered ticket order, not a liquidity heuristic."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.40", yes_size=5)     # thinnest book, first on the ticket
    _add(fake, "B", yes_ask="0.30", yes_size=900)   # deepest book, second
    _add(fake, "C", yes_ask="0.20", yes_size=50)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([
        _bet(1, "A"), _bet(2, "B", limit="0.3000"), _bet(3, "C", limit="0.2000"),
    ]), s)
    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "placed"
    rows = _rows(lg, aid)
    assert [rows[i]["is_real"] for i in (1, 2, 3)] == [1, 1, 1]
    assert all(rows[i]["contracts"] == 1 for i in (1, 2, 3))
    # ticket order, book size irrelevant
    assert [o["client_order_id"] for o in fake.orders_placed] == \
        [f"{aid}-B01", f"{aid}-B02", f"{aid}-B03"]
    assert lg.audit_events(event="cap_stop") == []


def _below_the_floor(lg, fake):
    """An account at $9 against a $20 genesis: half of 20 is 10, so the floor refuses."""
    lg.meta_set("live_genesis_balance", "20.0000")
    lg.insert_reconciliation(
        "2026-07-29T12:00:00Z", expected_balance=D("9.00"), actual_balance=D("9.00"),
        drift=D("0"), ok=True,
    )
    fake.set_balance("9.0000")


def test_the_drawdown_floor_writes_rejected_rows_and_no_paper(tmp_path, lg, notify_calls):
    """docs/22 section 7.4. The floor used to discard its reason and leave every unit
    paper with nothing written down, which is how seven attempts on 2026-08-17 looked
    like ordinary no-bets days. Every leg is now a refused row carrying the floor's own
    sentence, and no paper row is written at all."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    _below_the_floor(lg, fake)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)

    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "no_bets"
    assert fake.orders_placed == []
    rows = _rows(lg, aid)
    assert [rows[i]["status"] for i in (1, 2)] == ["rejected", "rejected"]
    assert [rows[i]["reject_code"] for i in (1, 2)] == ["drawdown_floor"] * 2
    assert all("drawdown_floor" in rows[i]["reject_reason"] for i in (1, 2))
    assert all("live balance 9.0000" in rows[i]["reject_reason"] for i in (1, 2))
    # the size the refusal cost is on the row, as it is for a cap refusal
    assert [rows[i]["declared_contracts"] for i in (1, 2)] == [1, 1]
    assert all(rows[i]["is_real"] == 0 for i in (1, 2))          # nothing was ever sent
    assert all(rows[i]["placed_at"] is None for i in (1, 2))
    assert lg.audit_events(event="cap_stop") == []               # not a cap stop


def test_the_floor_audits_once_per_attempt_and_alerts_at_once(tmp_path, lg, notify_calls):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    _below_the_floor(lg, fake)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)

    execute_attempt(aid, out, lg, fake, s)

    ev = lg.audit_events(event="floor_stop")
    assert len(ev) == 1                                # once per attempt, not per leg
    detail = json.loads(ev[0]["detail"])
    assert detail["legs"] == 2 and "drawdown_floor" in detail["reason"]
    alerts = lg.audit_events(event="alert_raised")
    assert len(alerts) == 1
    adetail = json.loads(alerts[0]["detail"])
    assert adetail["key"] == "drawdown_floor" and adetail["threshold"] == 1
    assert len(notify_calls) == 1
    assert streak_state(lg, "drawdown_floor") == {"n": 1, "notified": True}


def test_a_closed_live_gate_still_papers_rather_than_rejecting(tmp_path, lg):
    """Only the FLOOR writes refusals. ``live_trading`` off is the paper era working as
    designed, and a paper row there is a real record of what would have happened."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="prod", live_trading=False)
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)
    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert row["status"] == "filled" and row["is_real"] == 0
    assert row["reject_code"] is None and lg.audit_events(event="floor_stop") == []


# --------------------------------------------------------------------------- caps
def test_daily_cap_rejects_instead_of_papering(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.38", yes_size=50)
    # one_contract stakes are 0.40 each at limit; cap 0.50 admits only the first
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D("0.50")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B")]), s)
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert rows[1]["is_real"] == 1 and rows[1]["status"] == "filled"
    # cap-hit is a rejection, never a paper fallback (the sign-off's rule)
    assert rows[2]["status"] == "rejected"
    assert rows[2]["reject_code"] == "cap_daily"
    assert rows[2]["is_real"] == 0
    assert rows[2]["contracts"] is None
    assert rows[2]["fill_price"] is None and rows[2]["stake"] is None
    assert rows[2]["order_id"] is None and rows[2]["client_order_id"] is None
    assert rows[2]["placed_at"] is None
    assert len(fake.orders_placed) == 1  # nothing sent for the rejected unit

    ev = lg.audit_events(event="cap_stop")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["code"] == "cap_daily"
    assert detail["unit_min_index"] == 2
    assert detail["unit_stake"] == "0.4000"
    assert detail["day_headroom"] == "0.1000"  # 0.50 cap - 0.40 already committed
    assert detail["market"] is None and detail["market_headroom"] is None


def test_daily_cap_reads_prior_ledger_spend(tmp_path, lg):
    # a prior real bet today consumes most of the daily cap
    prior = _attempt(lg)
    lg.insert_bet(
        bet_id=f"{prior}-B01", attempt_id=prior, ticket_index=1, ticker="Z", side="yes",
        limit_price=D("0.50"), rationale="r", is_real=1, status="filled",
        contracts=19, fill_price=D("0.50"), stake=D("9.50"), fee=D("0.10"),
        placed_at=iso(utc_now()),
    )
    assert lg.daily_real_spend(et_day(utc_now())) == D("9.5000")

    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D("10.00")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B")]), s)
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert rows[1]["is_real"] == 1                  # 9.50 + 0.40 fits under 10.00
    assert rows[2]["reject_code"] == "cap_daily"    # a second 0.40 would breach it


def test_per_market_cap_rejects_with_cap_market(tmp_path, lg):
    # a prior real bet on ticker A has nearly exhausted its per-market cap
    prior = _attempt(lg)
    lg.insert_bet(
        bet_id=f"{prior}-B01", attempt_id=prior, ticket_index=1, ticker="A", side="yes",
        limit_price=D("0.50"), rationale="r", is_real=1, status="filled",
        contracts=4, fill_price=D("0.45"), stake=D("1.80"), fee=D("0.08"),
        placed_at=iso(utc_now()),
    )
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.per_market_real_cap": D("2.00")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B")]), s)
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert rows[1]["status"] == "rejected" and rows[1]["reject_code"] == "cap_market"
    assert rows[1]["is_real"] == 0
    assert rows[2]["is_real"] == 1  # a different market is untouched by A's cap

    ev = lg.audit_events(event="cap_stop")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["code"] == "cap_market"
    assert detail["market"] == "A"
    assert detail["market_headroom"] == "0.2000"  # 2.00 cap - 1.80 spent
    assert detail["unit_stake"] == "0.4000"


def test_rejected_unit_frees_no_headroom_so_a_smaller_one_still_fits(tmp_path, lg):
    """"Stop" applies per unit, not to the tail: the expensive unit is rejected and the
    cheaper one behind it is still checked on its own merits and placed."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.60", yes_size=50)  # stake 0.60 at limit
    _add(fake, "B", yes_ask="0.20", yes_size=50)  # stake 0.20 at limit
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D("0.50")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([
        _bet(1, "A", limit="0.6000"), _bet(2, "B", limit="0.2000"),
    ]), s)
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert rows[1]["reject_code"] == "cap_daily"
    assert rows[2]["is_real"] == 1 and rows[2]["status"] == "filled"
    assert [o["ticker"] for o in fake.orders_placed] == ["B"]
    # the rejected unit's stake never entered the running total
    detail = json.loads(lg.audit_events(event="cap_stop")[0]["detail"])
    assert detail["day_headroom"] == "0.5000"


# --------------------------------------------------------------------------- the allowance
def _allowance_run(tmp_path, lg, bets, *, allowance, **over):
    """Validate under a wide allowance, then execute under ``allowance``.

    The validator refuses the same legs first (V16), so ``cap_attempt`` only binds on a
    ticket that reached execution without that check; this is how one gets there.
    """
    fake = FakeKalshi()
    for b in bets:
        _add(fake, b.ticker, yes_ask="0.10", yes_size=50)
    wide = _settings(tmp_path, env="demo", **{"stakes.per_attempt_real_cap": D("100.00")})
    out = _validate(fake, _parsed(bets), wide)
    s = _settings(tmp_path, env="demo",
                  **{"stakes.per_attempt_real_cap": D(allowance), **over})
    aid = _attempt(lg)
    execute_attempt(aid, out, lg, fake, s)
    return aid, fake


def test_the_attempt_allowance_refuses_the_leg_past_it_with_cap_attempt(tmp_path, lg):
    bets = [_bet(i, t) for i, t in enumerate(("A", "B", "C", "D"), start=1)]  # 0.40 each
    aid, fake = _allowance_run(tmp_path, lg, bets, allowance="1.20")

    rows = _rows(lg, aid)
    assert [rows[i]["status"] for i in (1, 2, 3)] == ["filled"] * 3
    assert all(rows[i]["is_real"] == 1 for i in (1, 2, 3))
    row = rows[4]
    assert row["status"] == "rejected" and row["reject_code"] == "cap_attempt"
    assert row["is_real"] == 0 and row["contracts"] is None and row["placed_at"] is None
    assert row["declared_contracts"] == 1
    assert row["reject_reason"] == (
        "attempt allowance: $1.2000 of $1.2000 already committed by this attempt, "
        "this leg needed $0.4000"
    )
    assert [o["ticker"] for o in fake.orders_placed] == ["A", "B", "C"]

    ev = lg.audit_events(event="cap_stop")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["code"] == "cap_attempt"
    assert detail["unit_min_index"] == 4
    assert detail["unit_stake"] == "0.4000"
    assert detail["attempt_headroom"] == "0.0000"


def test_an_allowance_refusal_consumes_no_headroom(tmp_path, lg):
    bets = [_bet(1, "A"), _bet(2, "B"), _bet(3, "C"),      # 1.20 committed
            _bet(4, "D"),                                   # 1.60 > 1.30: refused
            _bet(5, "E", limit="0.1000")]                   # 1.30: fits
    aid, fake = _allowance_run(tmp_path, lg, bets, allowance="1.30")

    rows = _rows(lg, aid)
    assert rows[4]["reject_code"] == "cap_attempt"
    assert rows[5]["status"] == "filled" and rows[5]["is_real"] == 1
    assert [o["ticker"] for o in fake.orders_placed] == ["A", "B", "C", "E"]
    detail = json.loads(lg.audit_events(event="cap_stop")[0]["detail"])
    assert detail["attempt_headroom"] == "0.1000"


def test_the_daily_cap_is_tested_before_the_allowance(tmp_path, lg):
    """Leg 3 breaches both; the daily cap binds first, so that is what the row says."""
    bets = [_bet(1, "A"), _bet(2, "B"), _bet(3, "C")]      # 0.40 each
    aid, _fake = _allowance_run(tmp_path, lg, bets, allowance="0.90",
                                **{"stakes.daily_real_stake_cap": D("1.00")})

    rows = _rows(lg, aid)
    assert rows[3]["reject_code"] == "cap_daily"
    detail = json.loads(lg.audit_events(event="cap_stop")[0]["detail"])
    assert detail["code"] == "cap_daily"
    assert detail["attempt_headroom"] == "0.1000"


def test_a_v16_leg_carries_the_validator_s_own_sentence_and_its_size(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.per_attempt_real_cap": D("1.00")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A", contracts=2), _bet(2, "B", contracts=2)]), s)
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert rows[1]["status"] == "filled"
    assert rows[2]["reject_code"] == "V16" and rows[2]["is_real"] == 0
    assert "$1.60 in total" in rows[2]["reject_reason"]
    assert "$1.00 allowance" in rows[2]["reject_reason"]
    assert rows[2]["declared_contracts"] == 2
    assert lg.audit_events(event="cap_stop") == []     # the validator refused it, not a cap


def test_no_cap_projection_when_the_gate_is_closed(tmp_path, lg):
    # paper mode never consults the caps, so a tiny cap cannot reject a paper bet
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, live_trading=False, **{"stakes.daily_real_stake_cap": D("0.01")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)
    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert row["status"] == "filled" and row["is_real"] == 0
    assert lg.audit_events(event="cap_stop") == []


def test_the_per_market_cap_is_scoped_to_the_charge_day(tmp_path, lg):
    """docs/22 section 7.6: the per-market cap used to sum a ticker's real stake for all
    time, which closed a ticker permanently the first time it was traded. A bet yesterday
    must not spend today's allowance for that market."""
    yesterday = datetime(2026, 7, 7, 16, 0, tzinfo=UTC)   # ET 2026-07-07 12:00
    today = datetime(2026, 7, 8, 16, 0, tzinfo=UTC)       # ET 2026-07-08 12:00
    assert et_day(yesterday) != et_day(today)

    prior = _attempt(lg)
    lg.insert_bet(
        bet_id=f"{prior}-B01", attempt_id=prior, ticket_index=1, ticker="A", side="yes",
        limit_price=D("0.50"), rationale="r", is_real=1, status="filled",
        contracts=4, fill_price=D("0.45"), stake=D("1.80"), fee=D("0.08"),
        placed_at=iso(yesterday),
    )
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    # per-market cap 1.00: yesterday's 1.80 on A would close it forever if it counted
    s = _settings(tmp_path, env="demo", **{"stakes.per_market_real_cap": D("1.00")})

    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)
    execute_attempt(aid, out, lg, fake, s, now=today)
    assert _rows(lg, aid)[1]["status"] == "filled"      # a fresh day, a fresh allowance

    # ...and the same market is closed again once today's own spend fills the cap.
    again = _attempt(lg)
    out2 = _validate(fake, _parsed([_bet(1, "A", contracts=3)]), s)
    execute_attempt(again, out2, lg, fake, s, now=today)
    row = _rows(lg, again)[1]
    assert row["reject_code"] == "cap_market"          # 0.38 today + 1.20 > 1.00
    assert "on A today" in row["reject_reason"]


# --------------------------------------------------------------------------- partial fills
# TQ-1. This block used to be a single test that ordered ONE contract and scripted
# ``partial:1``, which is a full fill. It asserted `contracts == 1` and passed for the
# wrong reason, so `_exec_single`'s filled-but-short branch had no coverage at all under
# one-contract sizing. The ticket declares 1 to 3 contracts now, so the branch is reachable
# with a scripted short fill on a deep book: V11 already refuses a leg the book cannot
# cover at snapshot time, and what is left is the exchange filling short anyway.
def test_real_partial_fill_records_the_count_that_actually_filled(tmp_path, lg):
    """A 3-contract order the exchange answers with 2.

    Every execution field must describe the position that EXISTS: 2 contracts, $0.76
    staked, the fee on two contracts, not the 3 the ticket asked for. The stake is what the
    daily cap is charged and what the balance walk expects to see leave the account, so an
    intended-count stake here is drift at 23:00.
    """
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.38", yes_size=50)   # deep enough for all three at snapshot
    fake.set_order_behavior("A", "partial:2")
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A", contracts=3)]), s)
    assert out.bets[0].contracts == D("3")
    before = fake.balance
    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "placed"
    assert fake.orders_placed[0]["count"] == D("3")  # 3 asked for...
    row = _rows(lg, aid)[1]
    assert row["status"] == "filled" and row["is_real"] == 1
    assert row["contracts"] == 2                     # ...2 got
    assert row["fill_price"] == "0.3800"
    assert row["stake"] == "0.7600"                  # 2 x 0.38, NOT 3 x 0.38
    assert row["fee"] == str(fee_calc(D("2"), D("0.38"), D("0.07")))
    # The fake's balance moved by exactly the partial, so the row and the world agree.
    assert before - fake.balance == D(row["stake"]) + D(row["fee"])
    assert json.loads(lg.audit_events(event="order_result")[0]["detail"])["filled"] == "2"


def test_a_book_too_thin_for_the_declared_size_is_refused_before_any_order(tmp_path, lg):
    """docs/22 section 5.5: V11 measures depth against the leg's own ``contracts``, so a
    3-contract bet on a 1-contract book is refused rather than partially filled."""
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.38", yes_size=1)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A", contracts=3)]), s)

    assert execute_attempt(aid, out, lg, fake, s) == "no_bets"

    assert fake.orders_placed == []
    row = _rows(lg, aid)[1]
    assert row["status"] == "rejected" and row["reject_code"] == "V11"
    assert fake.balance == D("30.16")


# --------------------------------------------------------------------------- ambiguity
def test_order_ambiguous_resolved_via_fills(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    aid = _attempt(lg)
    bid = f"{aid}-B01"
    # the order actually landed, but the response was ambiguous
    fake.set_order_behavior("A", "ambiguous")
    fake.add_personal_fill("A", "yes", count=2, price=D("0.38"), client_order_id=bid)
    s = _settings(tmp_path, env="demo")
    out = _validate(fake, _parsed([_bet(1, "A")], attempt=aid), s)
    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "placed"
    row = _rows(lg, aid)[1]
    assert row["status"] == "filled"
    assert row["contracts"] == 2
    assert D(row["fill_price"]) == D("0.3800")
    assert row["is_real"] == 1
    assert lg.audit_events(event="order_ambiguous")


def test_an_order_that_landed_but_lost_its_receipt_reconciles_to_zero_drift(tmp_path, lg):
    """The ambiguity that costs money, scripted as the world really produces it.

    The test above says "the order landed" by hanging a bare fill on no order — which
    resolves through ``find_fills_by_client_order_id``'s documented fake-only fallback,
    not the orders->fills join production runs, and leaves the exchange with no order to
    show for the money it took. ``ambiguous_after_fill`` (added for this) books the order
    AND its fills and then loses only the receipt, so the resolver walks the real join,
    recovers the order id off the fill, and the whole chain closes to the cent — which is
    the property that actually matters: an ACK we never saw must not become drift at
    23:00.
    """
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    fake.set_order_behavior("A", "ambiguous_after_fill")
    s = _settings(tmp_path, env="demo")
    genesis = utc_now() - timedelta(hours=1)
    lg.meta_set("live_genesis_ts", iso(genesis))
    lg.meta_set("live_genesis_balance", str(fake.balance))

    aid = _attempt(lg)
    lg.transition(aid, "running")
    out = _validate(fake, _parsed([_bet(1, "A")], attempt=aid), s)
    assert execute_attempt(aid, out, lg, fake, s) == "placed"
    lg.transition(aid, "placed")

    row = _rows(lg, aid)[1]
    assert row["status"] == "filled" and row["is_real"] == 1
    assert row["contracts"] == 1 and row["fill_price"] == "0.3800"
    assert row["stake"] == "0.3800"
    assert row["fee"] == str(fee_calc(D("1"), D("0.38"), D("0.07")))
    assert row["order_id"] == "fake-order-1"      # recovered from the joined fill
    assert row["client_order_id"] == f"{aid}-B01"
    detail = json.loads(lg.audit_events(event="order_ambiguous")[0]["detail"])
    assert detail["resolved_filled"] == "1" and detail["n_fills"] == 1
    assert detail["rescanned"] is False and detail["scan_errored"] is False

    fake.resolve("A", "yes")
    counts = settle.settle_once(lg, fake, s)
    assert counts["bets_settled"] == 1 and counts["reconcile_mismatches"] == 0

    result = reconcile.reconcile_once(lg, fake, s, now=utc_now())
    assert result["drift"] == D("0.0000")
    assert result["ok"] is True and result["failed_checks"] == []
    assert not s.halt_path.exists()


# --------------------------------------------------------------------------- completeness
def test_every_bet_gets_row_including_rejected(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.40", yes_size=50, status="closed")
    s = _settings(tmp_path)
    aid = _attempt(lg)
    # bet 2's market is closed -> V04 rejected during validation
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.4000")]), s)
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert set(rows) == {1, 2}
    assert rows[1]["status"] == "filled"
    assert rows[2]["status"] == "rejected"
    assert rows[2]["reject_code"] == "V04"
    assert rows[2]["contracts"] is None
    # docs/22 section 5.4: a ticket no longer declares a probability, and nothing invents
    # one on the way to the row. The column stays for the old era's attempts.
    assert all(row["model_prob"] is None for row in rows.values())


def test_truncation_audited_and_excess_dropped(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    s = _settings(tmp_path, **{"limits.max_bets_per_attempt": 1})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)
    assert out.truncated_count == 1
    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert set(rows) == {1}  # the truncated bet gets no row
    ev = lg.audit_events(event="ticket_truncated")
    assert ev and '"count": 1' in ev[0]["detail"]


# --------------------------------------------------------------------------- client stubs
class _Wrapper:
    """Delegates the whole client surface to a FakeKalshi, overriding one method."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ExplodingClient(_Wrapper):
    """Raises something OUTSIDE the declared taxonomy on the nth create_order."""

    def __init__(self, inner, *, on_call, exc):
        super().__init__(inner)
        self._on_call, self._exc, self._n = on_call, exc, 0

    def create_order(self, *args, **kwargs):
        self._n += 1
        if self._n == self._on_call:
            raise self._exc
        return self._inner.create_order(*args, **kwargs)


class _LateFillsClient(_Wrapper):
    """Fills are invisible to the first n scans — read-after-write lag (KC-3/AE-3)."""

    def __init__(self, inner, *, blind_passes=1):
        super().__init__(inner)
        self.blind_passes, self.scans = blind_passes, 0

    def find_fills_by_client_order_id(self, coid):
        self.scans += 1
        if self.scans <= self.blind_passes:
            return []
        return self._inner.find_fills_by_client_order_id(coid)


class _HaltingClient(_Wrapper):
    """Writes HALT once the nth order has been transmitted (operator, or a tripwire)."""

    def __init__(self, inner, settings, *, after):
        super().__init__(inner)
        self._settings, self._after, self._n = settings, after, 0

    def create_order(self, *args, **kwargs):
        r = self._inner.create_order(*args, **kwargs)
        self._n += 1
        if self._n == self._after:
            safety.set_halt(self._settings, "impostor_detected")
        return r


# ------------------------------------------------------- mid-ticket errors (AE-1/ST-1)
def test_mid_ticket_4xx_keeps_the_earlier_real_fill_in_the_ledger(tmp_path, lg):
    """The ST-1 reproduction, inverted. Unit 1 fills for real, unit 2 is rejected 400.

    Before WP0 the exception propagated out of the placement loop, the insert step never
    ran, and a real fill existed on the exchange with NO ledger row: caps undercounted,
    settle never closed it, reconcile HALTed on a debit it could not explain.
    """
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    fake.set_order_behavior("B", "reject:400:insufficient balance")
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)

    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "placed"  # not 'failed' with lost fills
    rows = _rows(lg, aid)
    assert set(rows) == {1, 2}
    assert rows[1]["status"] == "filled" and rows[1]["is_real"] == 1
    assert D(rows[1]["stake"]) == D("0.3800")
    assert rows[2]["status"] == "no_fill" and rows[2]["order_id"] is None
    # the real fill is visible to the spend caps and to any later reconciliation
    assert lg.daily_real_spend(et_day(utc_now())) == D("0.3800")

    ev = lg.audit_events(event="order_error")
    assert len(ev) == 1
    detail = json.loads(ev[0]["detail"])
    assert detail["ticket_index"] == 2 and detail["status"] == 400
    assert "insufficient balance" in detail["error"]


def test_mid_ticket_failure_still_reconciles_to_zero_drift(tmp_path, lg):
    """The acceptance criterion: after a mid-ticket rejection the books explain the
    account balance to the cent, so the nightly reconcile stays clean."""
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    fake.set_order_behavior("B", "reject:400:insufficient balance")
    s = _settings(tmp_path, env="demo")
    genesis = utc_now() - timedelta(hours=1)
    lg.meta_set("live_genesis_ts", iso(genesis))
    lg.meta_set("live_genesis_balance", str(fake.balance))

    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)
    execute_attempt(aid, out, lg, fake, s)

    result = reconcile.reconcile_once(lg, fake, s, now=utc_now())
    assert result["drift"] == D("0.0000")
    assert result["ok"] is True and result["failed_checks"] == []
    assert not s.halt_path.exists()


def test_unknown_exception_mid_ticket_is_also_survived(tmp_path, lg):
    """Not just KalshiAPIError: a transport bug or a JSON surprise must not abandon the
    accounting either."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    client = _ExplodingClient(fake, on_call=2, exc=RuntimeError("client bug"))
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)

    status = execute_attempt(aid, out, lg, client, s)

    assert status == "placed"
    rows = _rows(lg, aid)
    assert rows[1]["status"] == "filled" and rows[2]["status"] == "no_fill"
    detail = json.loads(lg.audit_events(event="order_error")[0]["detail"])
    assert detail["error"] == "RuntimeError: client bug" and detail["status"] is None


def test_rows_for_never_placed_units_exist_before_any_order_is_sent(tmp_path, lg):
    """Up-front durability: validation- and cap-rejected rows are written before the
    first order leaves, so a crash mid-placement can never lose a ticket_index."""
    seen: list[int] = []
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.60", yes_size=50)   # stake 0.60 -> cap-rejected
    _add(fake, "B", yes_ask="0.20", yes_size=50)
    _add(fake, "C", yes_ask="0.40", yes_size=50, status="closed")  # V04 at validation
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D("0.50")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([
        _bet(1, "A", limit="0.6000"), _bet(2, "B", limit="0.2000"),
        _bet(3, "C", limit="0.4000"),
    ]), s)

    class _Recording(_Wrapper):
        def create_order(self, *args, **kwargs):
            seen.append(len(lg.bets_for_attempt(aid)))
            return self._inner.create_order(*args, **kwargs)

    execute_attempt(aid, out, lg, _Recording(fake), s)

    assert seen == [2]  # both rejected rows already durable when the only order went out
    rows = _rows(lg, aid)
    assert rows[1]["reject_code"] == "cap_daily" and rows[3]["reject_code"] == "V04"
    assert rows[2]["status"] == "filled"


# ------------------------------------------------------- ambiguity re-scan (KC-3/AE-3)
def test_zero_fill_ambiguity_rescans_before_concluding_absence(tmp_path, lg, monkeypatch):
    """One immediate empty scan is not proof the order never landed: the fill shows up on
    the second pass and must be recorded as the real position it is."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    aid = _attempt(lg)
    bid = f"{aid}-B01"
    fake.set_order_behavior("A", "ambiguous")
    fake.add_personal_fill("A", "yes", 1, D("0.38"), client_order_id=bid)
    slept: list[float] = []
    monkeypatch.setattr(execute_mod, "_sleep", slept.append)
    client = _LateFillsClient(fake, blind_passes=1)
    s = _settings(tmp_path, env="demo")
    out = _validate(fake, _parsed([_bet(1, "A")], attempt=aid), s)

    status = execute_attempt(aid, out, lg, client, s)

    assert status == "placed"
    row = _rows(lg, aid)[1]
    assert row["status"] == "filled" and D(str(row["contracts"])) == D("1")
    assert slept == [execute_mod._AMBIGUITY_RESCAN_DELAY_S]
    assert client.scans == 2
    detail = json.loads(lg.audit_events(event="order_ambiguous")[0]["detail"])
    assert detail["rescanned"] is True and detail["resolved_filled"] == "1"


def test_zero_fill_ambiguity_records_no_fill_when_truly_absent(tmp_path, lg, monkeypatch):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    aid = _attempt(lg)
    fake.set_order_behavior("A", "ambiguous")  # ambiguous, and no fill ever appears
    slept: list[float] = []
    monkeypatch.setattr(execute_mod, "_sleep", slept.append)
    client = _LateFillsClient(fake, blind_passes=0)
    s = _settings(tmp_path, env="demo")
    out = _validate(fake, _parsed([_bet(1, "A")], attempt=aid), s)

    status = execute_attempt(aid, out, lg, client, s)

    assert status == "no_bets"
    row = _rows(lg, aid)[1]
    assert row["status"] == "no_fill" and row["is_real"] == 1
    assert len(slept) == 1 and client.scans == 2  # absence is proven twice, not once
    detail = json.loads(lg.audit_events(event="order_ambiguous")[0]["detail"])
    assert detail["resolved_filled"] == "0" and detail["rescanned"] is True


def test_ambiguity_survives_a_failing_fills_scan(tmp_path, lg, monkeypatch):
    """A scan that raises must not propagate out of the placement loop — that is the
    AE-1 hole reopened from inside the ambiguity handler."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    aid = _attempt(lg)
    fake.set_order_behavior("A", "ambiguous")
    monkeypatch.setattr(execute_mod, "_sleep", lambda *_: None)

    class _BrokenScan(_Wrapper):
        def find_fills_by_client_order_id(self, coid):
            raise RuntimeError("fills endpoint down")

    s = _settings(tmp_path, env="demo")
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)
    status = execute_attempt(aid, out, lg, _BrokenScan(fake), s)

    assert status == "placed"  # bet 2 still placed and recorded
    rows = _rows(lg, aid)
    assert rows[1]["status"] == "no_fill" and rows[2]["status"] == "filled"
    detail = json.loads(lg.audit_events(event="order_ambiguous")[0]["detail"])
    assert detail["scan_errored"] is True


# --------------------------------------------------------------- fractional fills (KC-2)
def test_fractional_fill_records_the_exact_count_stake_and_fee(tmp_path, lg, monkeypatch):
    """A 0.90-contract fill is real money. It is recorded at its exact size — never
    truncated to a clean no-fill — and flagged with a fractional_fill audit."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.15", yes_size=50)
    aid = _attempt(lg)
    bid = f"{aid}-B01"
    fake.set_order_behavior("A", "ambiguous")
    fake.add_personal_fill("A", "yes", D("0.90"), D("0.15"), client_order_id=bid)
    monkeypatch.setattr(execute_mod, "_sleep", lambda *_: None)
    s = _settings(tmp_path, env="demo")
    out = _validate(fake, _parsed([_bet(1, "A", limit="0.1500")], attempt=aid), s)

    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "placed"
    row = _rows(lg, aid)[1]
    assert row["status"] == "filled"
    assert D(str(row["contracts"])) == D("0.90")
    assert D(row["fill_price"]) == D("0.1500")
    assert D(row["stake"]) == D("0.1350")  # 0.90 x 0.15, exact
    assert D(row["fee"]) == fee_calc(D("0.90"), D("0.1500"), D(s.fees.default_coef))
    detail = json.loads(lg.audit_events(event="fractional_fill")[0]["detail"])
    assert detail["filled"] == "0.90" and detail["ticket_index"] == 1


def test_three_fractional_fills_summing_to_one_record_as_one_contract(tmp_path, lg,
                                                                     monkeypatch):
    """The live A-0054-B01 shape: 0.28 + 0.34 + 0.38 at $0.15 is ONE contract for $0.15,
    and nothing about it is fractional once summed."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.15", yes_size=50)
    aid = _attempt(lg)
    bid = f"{aid}-B01"
    fake.set_order_behavior("A", "ambiguous")
    for piece in (D("0.28"), D("0.34"), D("0.38")):
        fake.add_personal_fill("A", "yes", piece, D("0.15"), client_order_id=bid)
    monkeypatch.setattr(execute_mod, "_sleep", lambda *_: None)
    s = _settings(tmp_path, env="demo")
    out = _validate(fake, _parsed([_bet(1, "A", limit="0.1500")], attempt=aid), s)

    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert D(str(row["contracts"])) == D("1")
    assert D(row["stake"]) == D("0.1500")
    assert D(row["fill_price"]) == D("0.1500")
    assert lg.audit_events(event="fractional_fill") == []  # the TOTAL is integral


# ------------------------------------------------------------- mid-attempt HALT (SV-7)
def test_halt_arriving_mid_attempt_stops_the_remaining_orders(tmp_path, lg):
    """A HALT written between unit 1 and unit 2 must stop transmission immediately —
    the gate used to be consulted once, before the first order."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    _add(fake, "C", yes_ask="0.20", yes_size=50)
    s = _settings(tmp_path, env="demo")
    client = _HaltingClient(fake, s, after=1)
    aid = _attempt(lg)
    out = _validate(fake, _parsed([
        _bet(1, "A"), _bet(2, "B", limit="0.3000"), _bet(3, "C", limit="0.2000"),
    ]), s)

    status = execute_attempt(aid, out, lg, client, s)

    assert status == "placed"
    assert [o["ticker"] for o in fake.orders_placed] == ["A"]  # 2 and 3 never sent
    rows = _rows(lg, aid)
    assert rows[1]["status"] == "filled"
    assert rows[2]["status"] == "no_fill" and rows[3]["status"] == "no_fill"
    ev = lg.audit_events(event="halt_mid_attempt")
    assert len(ev) == 1  # once per attempt, not once per refused leg
    assert json.loads(ev[0]["detail"])["ticket_index"] == 2


# ------------------------------------------------ AE-6 extension: HALT refusal transmits nothing
def test_halt_mid_attempt_leaves_the_refused_single_unit_without_placed_at(tmp_path, lg):
    """AE-6 extension: _real_order's HALT refusal means nothing was ever transmitted for
    unit B, so its real no-fill row must carry placed_at=NULL — is_real stays 1 (a real
    intention) and order_id is NOT the discriminator (a genuine real no-fill also has a
    NULL order_id — see the guard test below). No second is_halted() check happens in
    the caller; it trusts _real_order's own return."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.30", yes_size=50)
    s = _settings(tmp_path, env="demo")
    client = _HaltingClient(fake, s, after=1)  # HALT lands right after A transmits
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)

    execute_attempt(aid, out, lg, client, s)

    rows = _rows(lg, aid)
    assert rows[1]["status"] == "filled" and rows[1]["placed_at"] is not None  # A: sent
    assert rows[2]["is_real"] == 1 and rows[2]["status"] == "no_fill"
    assert rows[2]["order_id"] is None and rows[2]["placed_at"] is None       # B: never sent


def test_plain_transmitted_no_fill_still_carries_placed_at(tmp_path, lg):
    """Guard: a plain IOC miss (the order really was sent, it just filled zero) is a
    genuine transmission and must keep its placed_at — only the HALT refusal clears it.
    Confirms order_id alone cannot be the discriminator: this real no-fill also has a
    NULL order_id, same as the never-transmitted cases above."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    fake.set_order_behavior("A", "no_fill")
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "no_bets"
    assert len(fake.orders_placed) == 1              # the order really was sent
    row = _rows(lg, aid)[1]
    assert row["is_real"] == 1 and row["status"] == "no_fill"
    assert row["order_id"] is None                   # a real no-fill also has no order_id
    assert row["placed_at"] is not None               # but it WAS transmitted


# ------------------------------------------------------------- one placement clock (AE-4)
def test_placed_at_and_the_cap_charge_share_one_clock(tmp_path, lg):
    """AE-4: the ET day a stake is charged against is the day its placed_at records.

    These two instants straddle ET midnight. Before the fix the cap read ``utc_now()``
    while the row recorded the injected clock, so the same bet could be charged to one
    day and recorded on another.
    """
    before_midnight = datetime(2026, 7, 8, 3, 30, tzinfo=UTC)   # ET 2026-07-07 23:30
    after_midnight = datetime(2026, 7, 8, 4, 30, tzinfo=UTC)    # ET 2026-07-08 00:30
    assert et_day(before_midnight) != et_day(after_midnight)

    prior = _attempt(lg)
    lg.insert_bet(
        bet_id=f"{prior}-B01", attempt_id=prior, ticket_index=1, ticker="Z", side="yes",
        limit_price=D("0.50"), rationale="r", is_real=1,
        status="filled", contracts=1, fill_price=D("0.50"), stake=D("9.80"),
        fee=D("0.10"), placed_at=iso(before_midnight),
    )
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D("10.00")})

    same_day = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)
    execute_attempt(same_day, out, lg, fake, s, now=before_midnight)
    assert _rows(lg, same_day)[1]["reject_code"] == "cap_daily"  # 9.80 + 0.40 > 10.00

    next_day = _attempt(lg)
    out2 = _validate(fake, _parsed([_bet(1, "A")]), s)
    execute_attempt(next_day, out2, lg, fake, s, now=after_midnight)
    row = _rows(lg, next_day)[1]
    assert row["status"] == "filled"  # a fresh ET day, a fresh cap
    assert et_day(parse_iso(row["placed_at"])) == et_day(after_midnight)


def test_placement_clock_defaults_to_now_not_the_attempt_start(tmp_path, lg):
    """In two-loop mode the attempt's start clock is hours stale; execute reads its own
    at placement time, so placed_at is when the money actually moved."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    started = utc_now()
    execute_attempt(aid, out, lg, fake, s)

    placed = parse_iso(_rows(lg, aid)[1]["placed_at"])
    assert (placed - started) < timedelta(minutes=1)
    assert et_day(placed) == et_day(utc_now())


# ------------------------------------------------------------------ NO side end to end
def test_real_no_side_bet_runs_execute_settle_reconcile_at_zero_drift(tmp_path, lg):
    """MP-7, through the real writers. A NO buy is the sharpest edge of the V2 migration:
    it is quoted as an ask at 1-q on the single YES-quoted book and converted back on the
    way in. Nothing in the suite took one all the way through the fills join, the
    settlement math and the balance walk.
    """
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "N", no_ask="0.30", no_size=50)
    s = _settings(tmp_path, env="demo")
    genesis = utc_now() - timedelta(hours=1)
    lg.meta_set("live_genesis_ts", iso(genesis))
    lg.meta_set("live_genesis_balance", str(fake.balance))

    aid = _attempt(lg)
    lg.transition(aid, "running")
    out = _validate(fake, _parsed([_bet(1, "N", side="no", limit="0.3000")]), s)
    assert execute_attempt(aid, out, lg, fake, s) == "placed"
    lg.transition(aid, "placed")

    row = _rows(lg, aid)[1]
    assert row["side"] == "no" and row["is_real"] == 1
    assert D(row["fill_price"]) == D("0.3000")  # NO terms, not the 0.70 YES quote
    assert D(row["stake"]) == D("0.3000")
    assert [o["side"] for o in fake.orders_placed] == ["no"]

    fake.resolve("N", "no")  # the NO side wins
    counts = settle.settle_once(lg, fake, s)
    assert counts["bets_settled"] == 1 and counts["reconcile_mismatches"] == 0

    settled = _rows(lg, aid)[1]
    assert settled["outcome"] == "win"
    assert D(settled["pnl"]) == D("0.7000") - D(settled["fee"])  # 1 x (1 - 0.30) - fee

    result = reconcile.reconcile_once(lg, fake, s, now=utc_now())
    assert result["drift"] == D("0.0000")
    assert result["ok"] is True and result["failed_checks"] == []
    assert not s.halt_path.exists()


# ------------------------------------------------- AE-7: whose fee ends up in the row
class _FeeClient(_Wrapper):
    """Rewrites the ``fee`` of every ``OrderResult`` the inner client returns.

    ``FakeKalshi`` charges exactly the §8 model fee, so against it the executor's two fee
    branches — "the exchange told us what it charged, keep that" and "nothing came back,
    fall back to the model" — produce identical numbers and neither is actually pinned.
    Live they can still diverge: the receipt is ``average_fee_paid`` over the fills that
    actually happened, so a partial fill, a price improvement or a mis-guessed category
    coefficient moves it off the estimate — and the schedule is the exchange's to change
    (docs/14 D5 is the model catching up to it once already). This stub makes the two
    branches distinguishable by forcing the divergence.

    It rewrites only the RESULT, not the fake's balance move, so tests using it assert on
    the recorded row and do not walk the balance.
    """

    def __init__(self, inner, fee):
        super().__init__(inner)
        self._fee = fee

    def create_order(self, *args, **kwargs):
        r = self._inner.create_order(*args, **kwargs)
        return r.model_copy(update={"fee": self._fee})


def test_the_exchange_fee_is_what_gets_recorded_not_the_model_estimate(tmp_path, lg):
    """AE-7. The exchange's own number is the truth; §8 is only an estimate.

    Storing the model estimate instead would put model error straight into ``stake``,
    ``pnl`` and the nightly balance walk — and $0.0041 vs $0.0165 is a 4x error on the
    single most frequently repeated number in the ledger.
    """
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    model_fee = fee_calc(D("1"), D("0.38"), D("0.07"))
    assert model_fee == D("0.0165")           # what §8 says this order costs
    client = _FeeClient(fake, D("0.0041"))    # what the exchange actually charged
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    assert execute_attempt(aid, out, lg, client, s) == "placed"

    row = _rows(lg, aid)[1]
    assert row["status"] == "filled" and row["is_real"] == 1
    assert row["fee"] == "0.0041"             # the exchange's number, not 0.0165
    assert row["stake"] == "0.3800"


def test_a_fee_the_exchange_did_not_report_falls_back_to_the_model(tmp_path, lg):
    """TQ-7's other half: ``filled > 0`` with ``fee=None``.

    A no-fill legitimately carries ``fee=None`` (FK-3) and costs nothing. A FILL that
    carries ``fee=None`` is a receipt with the fee missing — the position exists and was
    charged something, so the row takes the §8 estimate rather than recording a free
    trade. Recording zero would understate every cost metric and drift the balance walk
    by the missing fee.
    """
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    client = _FeeClient(fake, None)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    assert execute_attempt(aid, out, lg, client, s) == "placed"

    row = _rows(lg, aid)[1]
    assert row["status"] == "filled"
    assert row["fee"] == str(fee_calc(D("1"), D("0.38"), D("0.07")))  # 0.0165, the §8 model
    assert row["fee"] != "0.0000"


def test_a_real_no_fill_records_no_fee_at_all(tmp_path, lg):
    """The contrast case that makes the one above meaningful: nothing filled, so there is
    nothing to charge and nothing to estimate — the row's fee stays NULL."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    fake.set_order_behavior("A", "no_fill")
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    assert execute_attempt(aid, out, lg, fake, s) == "no_bets"

    row = _rows(lg, aid)[1]
    assert row["status"] == "no_fill"
    assert row["fee"] is None and row["stake"] is None


# ------------------------------------------- FK-2: the exchange refuses what it can't fund
def test_an_order_the_balance_cannot_cover_is_survived_like_any_other_4xx(tmp_path, lg):
    """``enforce_balance=True`` (WP5's opt-in injector), driven through ``execute_attempt``.

    The 400 the exchange sends back for an unfundable order is a definite rejection, so
    nothing lands and no money moves — and the executor must treat it as the ordinary
    ``KalshiAPIError`` it is: audit ``order_error``, record the unit ``no_fill``, keep the
    unit that already filled, finish the ticket ``placed``. Before WP0 this exception
    escaped the placement loop and took the earlier real fill's ledger row with it, which
    is precisely the shape of failure a thin account produces most often.
    """
    fake = FakeKalshi(balance=D("0.50"), enforce_balance=True)
    _add(fake, "A", yes_ask="0.38", yes_size=50)   # 0.38 + 0.0165 fee = 0.3965; 0.1035 left
    _add(fake, "B", yes_ask="0.30", yes_size=50)   # 0.30 + 0.0147 fee = 0.3147 > 0.1035
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", limit="0.3000")]), s)

    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "placed"
    rows = _rows(lg, aid)
    assert rows[1]["status"] == "filled" and rows[1]["stake"] == "0.3800"
    assert rows[2]["status"] == "no_fill"
    assert rows[2]["placed_at"] is not None        # AE-6: the request really was sent
    assert rows[2]["fill_price"] is None and rows[2]["stake"] is None
    detail = json.loads(lg.audit_events(event="order_error")[0]["detail"])
    assert detail["ticket_index"] == 2 and detail["status"] == 400
    assert "insufficient_balance" in detail["error"]
    # The refused order moved no money: only the first fill was ever debited.
    assert fake.balance == D("0.50") - D("0.3965")
    assert [o["ticker"] for o in fake.orders_placed] == ["A", "B"]


# ------------------------------------------------- WP-B B1: cap semantics (stake only)
def test_daily_cap_counts_stake_only_so_fees_fall_outside_it(tmp_path, lg):
    """docs/14 B1, the documented choice: the cap governs STAKE, and fees sit outside it.

    Pinned exactly rather than approximately. With the cap set to one bet's stake, that bet
    is admitted, the day's recorded spend lands *on* the cap — and the cash that actually
    left the account is stake + fee, i.e. over it. A second bet is then refused, which is
    the other half of the semantics: the cap is exhausted by stake, not by cash.
    """
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.40", yes_size=50)   # ask == limit: stake is exactly 0.40
    _add(fake, "B", yes_ask="0.40", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D("0.40")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B")]), s)

    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert rows[1]["is_real"] == 1 and rows[1]["status"] == "filled"
    stake, fee_paid = D(rows[1]["stake"]), D(rows[1]["fee"])
    # D5 fee at this vector: ceil4(0.07 * 1 * 0.40 * 0.60) = 0.0168, already on the grid.
    assert stake == D("0.4000") and fee_paid == D("0.0168")

    cap = D(s.stakes.daily_real_stake_cap)
    # The cap accounting sees stake alone, and it is now exactly spent...
    assert lg.daily_real_spend(et_day(utc_now())) == cap
    # ...while the cash that actually left the account exceeded it by the fee.
    assert stake + fee_paid > cap
    # And the exchange charged exactly that: the fee is real money outside the cap.
    assert fake.balance == D("30.16") - (stake + fee_paid)

    # The second bet is refused on stake, not on cash — the cap is a stake budget.
    assert rows[2]["status"] == "rejected" and rows[2]["reject_code"] == "cap_daily"
    detail = json.loads(lg.audit_events(event="cap_stop")[0]["detail"])
    assert detail["day_headroom"] == "0.0000"


@pytest.mark.parametrize(("cap", "n_real"), [("0.40", 1), ("0.80", 2), ("1.20", 3)])
def test_daily_cap_admits_exactly_what_the_configured_cap_allows(tmp_path, lg, cap, n_real):
    """The admitted count tracks ``stakes.daily_real_stake_cap``, whatever it is set to."""
    fake = FakeKalshi(balance=D("30.16"))
    for t in ("A", "B", "C"):
        _add(fake, t, yes_ask="0.40", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D(cap)})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B"), _bet(3, "C")]), s)

    execute_attempt(aid, out, lg, fake, s)

    rows = _rows(lg, aid)
    assert sum(1 for r in rows.values() if r["is_real"] == 1) == n_real
    refused = [r["reject_code"] for i, r in sorted(rows.items()) if i > n_real]
    assert refused == ["cap_daily"] * (3 - n_real)


@pytest.mark.parametrize(("cap", "admitted"), [("10.00", False), ("15.00", True)])
def test_the_cap_is_read_from_config_not_hardcoded_at_ten(tmp_path, lg, cap, admitted):
    """B1's $10 -> $15 raise is a config flip, and this is what the flip buys.

    A day already $9.80 deep refuses another $0.40 bet at the old cap and admits it at the
    new one. Nothing changes between the two runs except ``daily_real_stake_cap``, so a $10
    baked into the enforcement path anywhere would fail the ``15.00`` case.
    """
    prior = _attempt(lg)
    lg.insert_bet(
        bet_id=f"{prior}-B01", attempt_id=prior, ticket_index=1, ticker="Z", side="yes",
        limit_price=D("0.50"), rationale="r", is_real=1,
        status="filled", contracts=20, fill_price=D("0.49"), stake=D("9.80"), fee=D("0.35"),
        placed_at=iso(utc_now()),
    )
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.40", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D(cap)})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert (row["is_real"] == 1) is admitted
    assert (row["reject_code"] == "cap_daily") is not admitted


# ------------------------------------- docs/22 section 7.5: the reason on the row
def test_an_exchange_refusal_puts_its_own_words_on_the_row(tmp_path, lg, notify_calls):
    """The refusal text the exchange sent lands on the bet row, not only in an audit
    detail. That is what the August residency block needed: the rows said "no fill" while
    the exchange was refusing every order in plain English, and the attempts kept
    proposing bets into a closed door for weeks.

    Driven through ``enforce_balance``, so the rejection carries the exchange's real shape
    (a 400 whose body names ``insufficient_balance``) rather than a hand-written body.
    """
    fake = FakeKalshi(balance=D("0.10"), enforce_balance=True)
    _add(fake, "A", yes_ask="0.38", yes_size=50)   # 0.38 + fee > 0.10
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "no_bets"
    row = _rows(lg, aid)[1]
    assert row["status"] == "no_fill"
    assert "insufficient_balance" in row["reject_reason"]
    assert row["reject_code"] is None              # the exchange refused; no gate did
    # order_error stays the money-path record of every rejection, and carries the same text
    ev = lg.audit_events(event="order_error")
    assert len(ev) == 1
    assert json.loads(ev[0]["detail"])["error"] == row["reject_reason"]
    # the separate insufficient-funds event and its alert are gone with the reason column
    assert lg.audit_events(event="order_insufficient_funds") == []
    assert notify_calls == []


def test_a_residency_refusal_is_readable_on_the_row(tmp_path, lg):
    """The docs/18 shape: a 403 naming the state, silent for weeks. One row now says it."""
    fake = FakeKalshi(balance=D("30.16"))
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    fake.set_order_behavior(
        "A", "reject:403:trading is not permitted from your location (XX)"
    )
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert "not permitted from your location (XX)" in row["reject_reason"]
    assert row["status"] == "no_fill" and row["placed_at"] is not None


def test_a_cap_refusal_says_which_cap_and_by_how_much(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _add(fake, "B", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.daily_real_stake_cap": D("0.50")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A"), _bet(2, "B", contracts=2)]), s)
    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[2]
    assert row["reject_code"] == "cap_daily"
    assert row["reject_reason"] == (
        "daily cap: $0.4000 of $0.5000 already committed today, this leg needed $0.8000"
    )
    # the size the refusal cost, which is what settle scores the leg at (docs/14 D12)
    assert row["declared_contracts"] == 2


def test_a_per_market_refusal_names_the_market(tmp_path, lg):
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    s = _settings(tmp_path, env="demo", **{"stakes.per_market_real_cap": D("0.10")})
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A", contracts=3)]), s)
    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert row["reject_code"] == "cap_market"
    assert row["reject_reason"] == (
        "per-market cap: $0.0000 of $0.1000 already committed on A today, "
        "this leg needed $1.2000"
    )
    assert row["declared_contracts"] == 3


def test_a_validator_refusal_carries_the_validator_s_sentence(tmp_path, lg):
    """A leg the validator refused says why in words too, from the validator's own map."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50, status="closed")   # V04
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)
    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert row["reject_code"] == "V04"
    assert row["reject_reason"] == "the market does not exist or is not open for trading"


def test_a_broken_notifier_never_costs_the_ledger_its_rows(tmp_path, lg, monkeypatch):
    """The escalation is a courtesy on top of the record (AE-1 discipline).

    ``record_failure`` is made to raise, whether from a locked ``meta`` row or a wedged
    ``osascript``, and the floor refusal must still write down every row it refused.
    Reporting a problem may never become a second problem.
    """
    def _boom(*a, **kw):
        raise RuntimeError("notifier down")

    monkeypatch.setattr(execute_mod, "record_failure", _boom)
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _below_the_floor(lg, fake)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A")]), s)

    status = execute_attempt(aid, out, lg, fake, s)

    assert status == "no_bets"
    assert _rows(lg, aid)[1]["reject_code"] == "drawdown_floor"      # the record survived
    assert len(lg.audit_events(event="floor_stop")) == 1


def test_the_floor_alert_re_arms_once_the_account_recovers(tmp_path, lg, notify_calls):
    """One banner per drawdown, and the floor passing is what ends one. Without the
    re-arm the "already notified" flag that holds a long outage to one banner would also
    hold every future outage to none."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.38", yes_size=50)
    _below_the_floor(lg, fake)
    s = _settings(tmp_path, env="demo")

    first = _attempt(lg)
    execute_attempt(first, _validate(fake, _parsed([_bet(1, "A")], attempt=first), s),
                    lg, fake, s)
    assert len(notify_calls) == 1
    assert streak_state(lg, "drawdown_floor")["notified"] is True

    fake.set_balance("18.0000")                    # funded again, above the 10.00 floor
    second = _attempt(lg)
    execute_attempt(second, _validate(fake, _parsed([_bet(1, "A")], attempt=second), s),
                    lg, fake, s)
    assert _rows(lg, second)[1]["status"] == "filled"
    assert streak_state(lg, "drawdown_floor") == {"n": 0, "notified": False}
    assert len(notify_calls) == 1                  # a placed attempt is not news

    fake.set_balance("9.0000")                     # back below the floor
    third = _attempt(lg)
    execute_attempt(third, _validate(fake, _parsed([_bet(1, "A")], attempt=third), s),
                    lg, fake, s)
    assert _rows(lg, third)[1]["reject_code"] == "drawdown_floor"
    assert len(notify_calls) == 2                  # the second drawdown is heard
    assert len(lg.audit_events(event="floor_stop")) == 2


# ------------------------------ docs/25: the fee that lands on the row is the receipt
def test_a_multi_contract_fill_stores_the_fee_the_exchange_charged(tmp_path, lg):
    """The three legs of 2026-09-17 carried a fee built by multiplying the exchange's
    already-rounded per-contract average by the fill count: $0.0099 where $0.0100 was
    charged, three times over, and the nightly walk was $0.0003 out. What lands on the row
    is the leg total the exchange reports."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.05", yes_size=50)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A", limit="0.0500", contracts=3)]), s)

    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert row["status"] == "filled" and row["contracts"] == 3
    assert D(row["fee"]) == D("0.0100")             # the leg total, rounded once
    assert D(row["fee"]) != fee_calc(1, D("0.05"), D("0.07")) * 3   # not per contract
    # and the exchange's own money trail agrees to the cent
    charged = [e for e in fake.balance_ledger() if e["reason"] == "fill"][-1]
    assert -charged["delta"] == q4(D(row["stake"]) + D(row["fee"]))


def test_a_one_contract_fill_is_unchanged(tmp_path, lg):
    """The figure a single contract produced was always right, which is why this went
    unseen across 352 legs."""
    fake = FakeKalshi()
    _add(fake, "A", yes_ask="0.05", yes_size=50)
    s = _settings(tmp_path, env="demo")
    aid = _attempt(lg)
    out = _validate(fake, _parsed([_bet(1, "A", limit="0.0500")]), s)

    execute_attempt(aid, out, lg, fake, s)

    row = _rows(lg, aid)[1]
    assert D(row["fee"]) == D("0.0034") == fee_calc(1, D("0.05"), D("0.07"))
