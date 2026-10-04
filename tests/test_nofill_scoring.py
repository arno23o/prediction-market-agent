"""Hypothetical scoring of refused legs at settlement (docs/14 D12).

The counterfactual for the legs a ticket proposed and never got — the ones the exchange
declined (``no_fill``) and the ones the harness itself refused (``rejected``: a spend cap,
a validation gate). Every test here is about the same two properties: the score is the
shadow-bet counterfactual applied to a real proposal (declared limit, declared size, net of
the D5 fee), and it cannot touch money — ``status``/``outcome``/``pnl``/``settled_at`` stay
exactly as settlement left them, because the metric of record is the filled book and
nothing else.
"""

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from betting_agent.config import load_settings
from betting_agent.harness import settle
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.ledger.db import Ledger, LedgerError
from betting_agent.moneymath import bet_pnl
from betting_agent.moneymath import fee as calc_fee

NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
CLOSE = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)
COEF = D("0.07")


@pytest.fixture
def env(tmp_path):
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    settings = load_settings(root=tmp_path)
    fake = FakeKalshi()
    yield ledger, fake, settings
    ledger.close()


def _attempt(lg):
    _, aid = lg.create_attempt(
        env="prod", model="claude-sonnet-5", effort="high", memory_mode="on",
        prompt_version="p1", toolkit_version="0.1.0", workspace_path="/ws",
    )
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    return aid


def _nofill(lg, aid, idx, ticker, *, side="yes", limit="0.40", contracts=D("1"),
            is_real=1, category=None):
    """A no-fill row exactly as ``execute._no_fill`` writes one: the declared count is
    kept, every execution field is NULL."""
    bet_id = f"{aid}-B{idx:02d}"
    lg.insert_bet(
        bet_id=bet_id, attempt_id=aid, ticket_index=idx, ticker=ticker, side=side,
        limit_price=D(limit), model_prob=D("0.55"), rationale="r", status="no_fill",
        contracts=contracts, is_real=is_real, category=category,
        client_order_id=f"{bet_id}" if is_real else None,
        placed_at="2026-08-10T11:00:00Z",
    )
    return bet_id


def _rejected(lg, aid, idx, ticker, *, code="cap_daily", side="yes", limit="0.40",
              declared=D("1"), is_real=0, category=None):
    """A rejected row exactly as ``execute._cap_rejected`` writes one: every execution
    field (``contracts`` included) is NULL and the declared size lives in
    ``declared_contracts``. A V11 reject is the same shape with a different code."""
    bet_id = f"{aid}-B{idx:02d}"
    lg.insert_bet(
        bet_id=bet_id, attempt_id=aid, ticket_index=idx, ticker=ticker, side=side,
        limit_price=D(limit), model_prob=D("0.55"), rationale="r", status="rejected",
        reject_code=code, contracts=None, declared_contracts=declared, is_real=is_real,
        category=category,
    )
    return bet_id


def _settle(lg, fake, settings):
    return settle.settle_once(lg, fake, settings, now=NOW)


# --------------------------------------------------------------------------- scoring
@pytest.mark.parametrize(
    "side,result,outcome",
    [("yes", "yes", "win"), ("yes", "no", "loss"), ("no", "no", "win"), ("no", "yes", "loss")],
)
def test_a_nofill_is_scored_at_its_declared_limit_net_of_the_d5_fee(env, side, result, outcome):
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _nofill(lg, aid, 1, "NF-1", side=side, limit="0.40")
    fake.add_market("NF-1", title="NF-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("NF-1", result)

    counts = _settle(lg, fake, settings)

    assert counts["nofills_scored"] == 1 and counts["nofills_voided"] == 0
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    assert row["hypothetical_outcome"] == outcome
    # The assumption spelled out: one contract filled at the declared 0.40 limit, fee
    # 0.07*0.40*0.60 = 0.0168 exactly on the 4dp grid (docs/14 D5).
    expected = bet_pnl(outcome, D("1"), D("0.40"), calc_fee(1, D("0.40"), COEF))
    assert expected == (D("0.5832") if outcome == "win" else D("-0.4168"))
    assert row["hypothetical_pnl"] == str(expected)
    assert row["hypothetical_scored_at"] == "2026-08-10T12:00:00Z"


def test_the_money_columns_are_not_touched(env):
    """The load-bearing property. A no-fill stays a no-fill with no P/L: the hypothetical
    is a separate pair of columns precisely so no total can pick it up."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _nofill(lg, aid, 1, "NF-1")
    fake.add_market("NF-1", title="NF-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("NF-1", "yes")

    _settle(lg, fake, settings)

    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    assert row["status"] == "no_fill"
    assert (row["outcome"], row["pnl"], row["settled_at"]) == (None, None, None)
    assert (row["fill_price"], row["stake"], row["fee"]) == (None, None, None)
    assert row["hypothetical_pnl"] == "0.5832"          # …and it really did score


def test_an_unresolved_market_is_left_for_a_later_pass(env):
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _nofill(lg, aid, 1, "NF-1")
    fake.add_market("NF-1", title="NF-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)

    assert _settle(lg, fake, settings)["nofills_scored"] == 0
    assert [b["bet_id"] for b in lg.unscored_nofill_bets()] == [bid]

    fake.resolve("NF-1", "yes")
    assert _settle(lg, fake, settings)["nofills_scored"] == 1
    assert lg.unscored_nofill_bets() == []


def test_a_voided_market_scores_the_nofill_at_zero(env):
    """A void refunds the stake, so the counterfactual nets nothing — the same figure the
    real bets path writes for a voided position, on the table it shares with it."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _nofill(lg, aid, 1, "NF-1")
    fake.add_market("NF-1", title="NF-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("NF-1", "")                            # finalized with no decisive side

    counts = _settle(lg, fake, settings)

    assert counts["nofills_voided"] == 1 and counts["nofills_scored"] == 0
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    assert (row["hypothetical_outcome"], row["hypothetical_pnl"]) == ("void", "0.0000")


def test_the_category_coefficient_reaches_the_hypothetical_fee(env):
    """Index markets are cheaper (0.035), and the counterfactual has to be charged the
    coefficient the leg would actually have paid — the pessimization D5 exists to remove
    would otherwise come straight back in through the wrong coef."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _nofill(lg, aid, 1, "NF-IDX", limit="0.40", category="index")
    fake.add_market("NF-IDX", title="NF-IDX", close_time=CLOSE, yes_ask=D("0.40"),
                    yes_ask_size=10)
    fake.resolve("NF-IDX", "yes")

    _settle(lg, fake, settings)

    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    index_fee = calc_fee(1, D("0.40"), D(settings.fees.category_coefs["index"]))
    assert index_fee == D("0.0084")                      # 0.035*0.4*0.6, exact at 4dp
    assert row["hypothetical_pnl"] == str(bet_pnl("win", D("1"), D("0.40"), index_fee))
    assert row["hypothetical_pnl"] == "0.5916"           # cheaper coef → better hypothetical


def test_a_fractional_declared_size_is_scored_on_its_exact_size(env):
    """Counts are exact Decimals on this path too (KC-2): a group leg sized 0.90 must not
    be rounded to 1 by the counterfactual any more than by the money path."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _nofill(lg, aid, 1, "NF-1", limit="0.40", contracts=D("0.90"))
    fake.add_market("NF-1", title="NF-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("NF-1", "yes")

    _settle(lg, fake, settings)

    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    # fee = 0.07*0.90*0.40*0.60 = 0.01512 -> ceil 0.0152; pnl = 0.90*(1-0.40) - 0.0152.
    expected = bet_pnl("win", D("0.90"), D("0.40"), calc_fee(D("0.90"), D("0.40"), COEF))
    assert calc_fee(D("0.90"), D("0.40"), COEF) == D("0.0152")
    assert row["hypothetical_pnl"] == str(expected) == "0.5248"


def test_a_paper_nofill_is_scored_too(env):
    """The packet's job is the whole proposed book; a paper leg is as unfilled as a real
    one. (In live operation every validated unit is real, so this is the halted/paper-era
    shape rather than the common one.)"""
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _nofill(lg, aid, 1, "NF-1", is_real=0)
    fake.add_market("NF-1", title="NF-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("NF-1", "yes")

    assert _settle(lg, fake, settings)["nofills_scored"] == 1
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    assert row["hypothetical_pnl"] == "0.5832"


# --------------------------------------------------------------------------- idempotence
def test_settling_twice_does_not_rescore_or_duplicate(env):
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _nofill(lg, aid, 1, "NF-1")
    fake.add_market("NF-1", title="NF-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("NF-1", "yes")

    first = _settle(lg, fake, settings)
    second = _settle(lg, fake, settings)

    assert first["nofills_scored"] == 1
    assert second["nofills_scored"] == 0 and second["nofills_voided"] == 0
    assert second["errors"] == 0                        # not "scored again and refused"
    rows = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["hypothetical_scored_at"] == "2026-08-10T12:00:00Z"


def test_a_scoring_failure_does_not_abort_the_pass(env, monkeypatch):
    """MP-2's discipline, extended to the new stage: one bad row costs its own score and
    an audit event, never the settlements that follow it."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bad = _nofill(lg, aid, 1, "NF-1")
    good = _nofill(lg, aid, 2, "NF-2")
    for t in ("NF-1", "NF-2"):
        fake.add_market(t, title=t, close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
        fake.resolve(t, "yes")

    real_score = Ledger.score_nofill_bet

    def flaky(self, bet_id, **kw):
        if bet_id == bad:
            raise LedgerError("disk full")
        return real_score(self, bet_id, **kw)

    monkeypatch.setattr(Ledger, "score_nofill_bet", flaky)
    counts = _settle(lg, fake, settings)

    assert counts["nofills_scored"] == 1 and counts["errors"] == 1
    ev = lg.audit_events(event="nofill_score_error")
    assert len(ev) == 1 and ev[0]["bet_id"] == bad
    assert lg.conn.execute(
        "SELECT hypothetical_pnl AS p FROM bets WHERE bet_id=?", (good,)
    ).fetchone()["p"] == "0.5832"


# --------------------------------------------------------------------------- isolation
def test_scoring_does_not_move_the_attempt_or_the_exchange_balance(env):
    """Nothing about a counterfactual is a transaction: the attempt keeps the status
    settlement gave it and the exchange balance never moves."""
    lg, fake, settings = env
    aid = _attempt(lg)
    _nofill(lg, aid, 1, "NF-1")
    fake.add_market("NF-1", title="NF-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("NF-1", "yes")
    balance_before = fake.balance

    counts = _settle(lg, fake, settings)

    assert counts["nofills_scored"] == 1
    assert fake.balance == balance_before
    # The attempt flipped on terminal-ness alone (a no-fill was already terminal), and the
    # hypothetical score changed nothing about that.
    assert lg.get_attempt(aid)["status"] == "settled"
    assert counts["bets_settled"] == 0


# --------------------------------------------------------------------------- rejects
# The legs the HARNESS refused rather than the ones the exchange declined. Same
# counterfactual, its own counts pair, and the same hands-off-money property — checked
# again here rather than assumed, because it is the only property that matters.
@pytest.mark.parametrize("code", ["cap_daily", "V11"])
@pytest.mark.parametrize(
    "side,result,outcome",
    [("yes", "yes", "win"), ("no", "yes", "loss")],
)
def test_a_rejected_leg_is_scored_at_its_declared_limit_net_of_the_d5_fee(
    env, code, side, result, outcome
):
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _rejected(lg, aid, 1, "RJ-1", code=code, side=side, limit="0.40")
    fake.add_market("RJ-1", title="RJ-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("RJ-1", result)

    counts = _settle(lg, fake, settings)

    assert counts["rejects_scored"] == 1 and counts["rejects_voided"] == 0
    assert counts["nofills_scored"] == 0        # the two populations are counted apart
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    assert row["hypothetical_outcome"] == outcome
    expected = bet_pnl(outcome, D("1"), D("0.40"), calc_fee(1, D("0.40"), COEF))
    assert row["hypothetical_pnl"] == str(expected)
    assert expected == (D("0.5832") if outcome == "win" else D("-0.4168"))
    assert row["hypothetical_scored_at"] == "2026-08-10T12:00:00Z"
    # The reject's own record is untouched: still refused, still for the stated reason.
    assert (row["status"], row["reject_code"]) == ("rejected", code)
    assert (row["outcome"], row["pnl"], row["settled_at"]) == (None, None, None)
    assert (row["contracts"], row["fill_price"], row["stake"], row["fee"]) == (
        None, None, None, None)


def test_a_rejected_leg_is_sized_from_declared_contracts(env):
    """A cap-rejected row has ``contracts`` NULL by construction, so the size has to come
    from ``declared_contracts`` — the column migration 004 added for exactly this leg."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _rejected(lg, aid, 1, "RJ-1", declared=D("3"))
    fake.add_market("RJ-1", title="RJ-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("RJ-1", "yes")

    assert _settle(lg, fake, settings)["rejects_scored"] == 1
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    expected = bet_pnl("win", D("3"), D("0.40"), calc_fee(D("3"), D("0.40"), COEF))
    assert row["hypothetical_pnl"] == str(expected) == "1.7496"   # 3*(1-0.40) - 0.0504


def test_a_reject_with_neither_count_column_is_scored_at_one_contract(env):
    """Every reject written before migration 004 has no declared size at all. One contract
    is the live sizing rule, and it is better than leaving those rows unscored forever."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _rejected(lg, aid, 1, "RJ-1", declared=None)
    fake.add_market("RJ-1", title="RJ-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("RJ-1", "yes")

    assert _settle(lg, fake, settings)["rejects_scored"] == 1
    assert lg.conn.execute(
        "SELECT hypothetical_pnl AS p FROM bets WHERE bet_id=?", (bid,)
    ).fetchone()["p"] == "0.5832"


def test_a_voided_market_scores_the_reject_at_zero(env):
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _rejected(lg, aid, 1, "RJ-1")
    fake.add_market("RJ-1", title="RJ-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("RJ-1", "")

    counts = _settle(lg, fake, settings)

    assert counts["rejects_voided"] == 1 and counts["rejects_scored"] == 0
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (bid,)).fetchone()
    assert (row["hypothetical_outcome"], row["hypothetical_pnl"]) == ("void", "0.0000")


def test_an_unresolved_market_leaves_the_reject_for_a_later_pass(env):
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _rejected(lg, aid, 1, "RJ-1")
    fake.add_market("RJ-1", title="RJ-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)

    assert _settle(lg, fake, settings)["rejects_scored"] == 0
    assert [b["bet_id"] for b in lg.unscored_rejected_bets()] == [bid]

    fake.resolve("RJ-1", "yes")
    assert _settle(lg, fake, settings)["rejects_scored"] == 1
    assert lg.unscored_rejected_bets() == []


def test_scoring_rejects_twice_does_not_rescore(env):
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _rejected(lg, aid, 1, "RJ-1")
    fake.add_market("RJ-1", title="RJ-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("RJ-1", "yes")

    first = _settle(lg, fake, settings)
    second = _settle(lg, fake, settings)

    assert first["rejects_scored"] == 1
    assert (second["rejects_scored"], second["rejects_voided"], second["errors"]) == (0, 0, 0)
    assert lg.conn.execute(
        "SELECT hypothetical_scored_at AS t FROM bets WHERE bet_id=?", (bid,)
    ).fetchone()["t"] == "2026-08-10T12:00:00Z"


def test_a_reject_scoring_failure_does_not_abort_the_pass(env, monkeypatch):
    """MP-2 again, on the new stage and with its own audit event."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bad = _rejected(lg, aid, 1, "RJ-1")
    good = _rejected(lg, aid, 2, "RJ-2")
    for t in ("RJ-1", "RJ-2"):
        fake.add_market(t, title=t, close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
        fake.resolve(t, "yes")

    real_score = Ledger.score_rejected_bet

    def flaky(self, bet_id, **kw):
        if bet_id == bad:
            raise LedgerError("disk full")
        return real_score(self, bet_id, **kw)

    monkeypatch.setattr(Ledger, "score_rejected_bet", flaky)
    counts = _settle(lg, fake, settings)

    assert counts["rejects_scored"] == 1 and counts["errors"] == 1
    ev = lg.audit_events(event="reject_score_error")
    assert len(ev) == 1 and ev[0]["bet_id"] == bad
    assert lg.conn.execute(
        "SELECT hypothetical_pnl AS p FROM bets WHERE bet_id=?", (good,)
    ).fetchone()["p"] == "0.5832"


def test_a_real_reject_is_scored_too(env):
    """Every reject in the live ledger is a paper row — the refusal is why no order went
    out — but the writer is pinned to the status, not to ``is_real``, exactly as the
    no-fill writer is."""
    lg, fake, settings = env
    aid = _attempt(lg)
    bid = _rejected(lg, aid, 1, "RJ-1", is_real=1)
    fake.add_market("RJ-1", title="RJ-1", close_time=CLOSE, yes_ask=D("0.40"), yes_ask_size=10)
    fake.resolve("RJ-1", "yes")

    assert _settle(lg, fake, settings)["rejects_scored"] == 1
    assert lg.conn.execute(
        "SELECT hypothetical_pnl AS p FROM bets WHERE bet_id=?", (bid,)
    ).fetchone()["p"] == "0.5832"


def test_the_two_writers_cannot_reach_each_others_rows(env):
    """The load-bearing guarantee of having two named writers: each WHERE clause pins the
    status, so a counterfactual can never land on a row of the wrong shape."""
    lg, fake, settings = env
    aid = _attempt(lg)
    nofill = _nofill(lg, aid, 1, "NF-1")
    reject = _rejected(lg, aid, 2, "RJ-1")

    with pytest.raises(LedgerError):
        lg.score_rejected_bet(nofill, outcome="win", hypothetical_pnl=D("1"),
                              scored_at="2026-08-10T12:00:00Z")
    with pytest.raises(LedgerError):
        lg.score_nofill_bet(reject, outcome="win", hypothetical_pnl=D("1"),
                            scored_at="2026-08-10T12:00:00Z")
    for bid in (nofill, reject):
        assert lg.conn.execute(
            "SELECT hypothetical_scored_at AS t FROM bets WHERE bet_id=?", (bid,)
        ).fetchone()["t"] is None
