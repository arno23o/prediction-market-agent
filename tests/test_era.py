"""The pilot-era reset (docs/14 WP-E, from docs/12 §5/§9.15).

What is left of the work package after the rebuild removed the statistics surfaces it was
written against (docs/22 section 2.1): the boundary itself (E1), the scopes and the
n-labels built from it, and the money-path regressions that are the other half of the
claim, namely that money paths were NOT touched.

The `mixed` fixture is deliberately minimal and hand-computable: one pilot-era attempt and
one live-era attempt, one settled bet each, in different categories, so any leak of the
pilot row into a live-era answer is visible by inspection.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from betting_agent.config import load_settings
from betting_agent.harness import reconcile as reconcile_mod
from betting_agent.harness import settle as settle_mod
from betting_agent.kalshi.testing import FakeKalshi
from betting_agent.ledger.db import (
    ALL_ERAS,
    LIVE_ERA,
    PILOT_ERA,
    EraScope,
    Ledger,
    LedgerError,
    era_of,
)
from betting_agent.moneymath import fee as calc_fee
from betting_agent.moneymath import q4
from betting_agent.timeutil import parse_iso

# The real live genesis (2026-07-31T05:44:29Z) — used verbatim so the label assertions pin
# the string an operator will actually read. The code never hardcodes it; it reads meta.
GENESIS = "2026-07-31T05:44:29Z"
PILOT_TS = "2026-07-13T14:08:07Z"
POST_GENESIS_TS = "2026-08-02T14:00:00Z"

LIVE_LABEL = "live era, since Jul 31"
PILOT_LABEL = "pilot era, before Jul 31"
POOLED_LABEL = "both eras pooled (pilot + live), split at Jul 31"

NOW = parse_iso("2026-08-05T14:00:00Z")


@pytest.fixture
def lg(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    ledger = Ledger.open(tmp_path / "data" / "ledger.db")
    ledger.migrate()
    yield ledger
    ledger.close()


@pytest.fixture
def settings(tmp_path):
    return load_settings(root=tmp_path)


def _attempt(lg, *, created_at, memory="on", model="claude-sonnet-5"):
    _, aid = lg.create_attempt(
        env="prod", model=model, effort="high", memory_mode=memory,
        prompt_version="p1", toolkit_version="0.1.0", workspace_path="/ws",
    )
    # `created_at` is not writable through the DAO by design (spec §5); the raw UPDATE is
    # the escape every test that has to place an attempt in a particular era uses.
    lg.conn.execute("UPDATE attempts SET created_at=? WHERE attempt_id=?", (created_at, aid))
    return aid


def _settled_attempt(lg, *, created_at, category, outcome, pnl, fill, prob,
                     is_real=0, memory="on"):
    aid = _attempt(lg, created_at=created_at, memory=memory)
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker=f"KX{aid}", side="yes",
        limit_price=D(fill), model_prob=D(prob), rationale="r", is_real=is_real,
        contracts=1, status="settled", fill_price=D(fill), stake=D(fill), fee=D("0.04"),
        outcome=outcome, pnl=D(pnl), category=category,
    )
    lg.set_ticket_texts(aid, f"## Markets\n{category}", "## If we're right\nx", "manifest")
    lg.transition(aid, "settled")
    return aid


@pytest.fixture
def mixed(lg):
    """One pilot attempt (paper, Weather, a loss) and one live attempt (real, Politics, a win).

    Deliberately minimal and hand-computable, so any leak of the pilot row into a live-era
    answer is visible by inspection.
    """
    ids = {}
    ids["pilot"] = _settled_attempt(
        lg, created_at=PILOT_TS, category="Weather",
        outcome="loss", pnl="-0.2400", fill="0.2000", prob="0.8000", memory="off",
    )
    lg.meta_set("live_genesis_ts", GENESIS)
    lg.meta_set("live_genesis_balance", "30.16")
    ids["live"] = _settled_attempt(
        lg, created_at=POST_GENESIS_TS, category="Politics",
        outcome="win", pnl="0.3600", fill="0.6000", prob="0.5500", is_real=1,
    )
    return ids


# --------------------------------------------------------------------------- E1 boundary
def test_era_boundary_exactly_at_genesis_is_live():
    """E1: `'live'` iff `created_at >= genesis` — the boundary instant belongs to the live era."""
    assert era_of(GENESIS, GENESIS) == LIVE_ERA
    assert era_of("2026-07-31T05:44:28Z", GENESIS) == PILOT_ERA
    assert era_of("2026-07-31T05:44:30Z", GENESIS) == LIVE_ERA
    assert era_of(PILOT_TS, GENESIS) == PILOT_ERA


def test_era_of_with_no_genesis_is_pilot_by_e1s_letter():
    assert era_of(POST_GENESIS_TS, None) == PILOT_ERA
    assert era_of(None, GENESIS) == PILOT_ERA


def test_ledger_era_reads_the_stamp_from_meta(lg, mixed):
    """The boundary is read from `meta.live_genesis_ts`, never hardcoded — move the stamp
    and the same attempt changes era."""
    assert lg.era(mixed["pilot"]) == PILOT_ERA
    assert lg.era(mixed["live"]) == LIVE_ERA
    assert lg.era(lg.get_attempt(mixed["live"])) == LIVE_ERA   # row or id, same answer

    lg.meta_set("live_genesis_ts", "2026-08-09T00:00:00Z")
    assert lg.era(mixed["live"]) == PILOT_ERA


def test_era_scope_no_ops_without_a_genesis_stamp(lg):
    """A record with no live era is ONE era: the filter must not narrow it to nothing.

    This is the property that keeps every pre-live ledger (and every fixture in this suite
    that stamps no genesis) reading exactly as it did before WP-E.
    """
    scope = lg.era_scope(LIVE_ERA)
    assert scope.filtering is False
    assert scope.sql() == ("1", [])
    assert scope.keeps(PILOT_TS) is True
    assert scope.label() == "all history (no live era yet)"


def test_era_scope_sql_and_labels(lg, mixed):
    live = lg.era_scope(LIVE_ERA)
    assert live.filtering is True
    frag, params = live.sql("b.attempt_id")
    assert frag == "b.attempt_id IN (SELECT attempt_id FROM attempts WHERE created_at >= ?)"
    assert params == [GENESIS]
    assert live.label() == LIVE_LABEL
    assert live.label(7) == f"{LIVE_LABEL}, n=7"

    pilot = lg.era_scope(PILOT_ERA)
    assert pilot.sql()[0].endswith("created_at < ?)")
    assert pilot.label() == PILOT_LABEL
    assert lg.era_scope(ALL_ERAS).label() == POOLED_LABEL


def test_unknown_era_is_refused_at_construction(lg):
    with pytest.raises(LedgerError, match="unknown era"):
        EraScope("liv", GENESIS)
    with pytest.raises(LedgerError, match="unknown era"):
        lg.era_scope("everything")


# --------------------------------------------------------------------------- money paths
def test_reconcile_walks_a_pilot_era_attempts_post_genesis_fill(tmp_path):
    """The regression the era filter must never cause: money is keyed on `placed_at`, not on
    the attempt's era. An attempt created before genesis whose order fills after it spends
    real money, and a walk that skipped it would HALT the system on a phantom drift.
    """
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    settings = load_settings(root=tmp_path)
    fake = FakeKalshi(balance=D("30.16"))
    genesis = datetime(2026, 7, 31, 5, 44, 29, tzinfo=UTC)
    try:
        aid = _attempt(ledger, created_at="2026-07-13T14:08:07Z")   # PILOT era
        ledger.transition(aid, "running")
        ledger.transition(aid, "placed")
        ledger.meta_set("live_genesis_ts", "2026-07-31T05:44:29Z")

        fake.add_market("KXLATE", title="KXLATE",
                        close_time=genesis + timedelta(days=2), yes_ask=D("0.40"),
                        yes_ask_size=50)
        coid = f"{aid}-B01"
        r = fake.create_order("KXLATE", "yes", D("0.40"), 1, coid)
        ledger.insert_bet(
            bet_id=coid, attempt_id=aid, ticket_index=1, ticker="KXLATE", side="yes",
            limit_price=D("0.40"), model_prob=D("0.60"), rationale="r", is_real=1,
            status="filled", contracts=1, fill_price=r.avg_fill_price,
            stake=q4(r.avg_fill_price), fee=D(r.fee), order_id=r.order_id,
            client_order_id=coid,
            placed_at="2026-08-01T12:00:00Z",       # after genesis: real money, live walk
        )
        # The anchor is the balance as it stood at genesis — before this debit. The walk has
        # to find the debit to get from there back to the exchange's current balance.
        ledger.meta_set("live_genesis_balance", "30.16")
        assert fake.get_balance().dollars == q4(
            D("30.16") - D("0.40") - calc_fee(1, D("0.40"), D("0.07"))
        )

        out = reconcile_mod.reconcile_once(
            ledger, fake, settings, now=datetime(2026, 8, 1, 23, 5, tzinfo=UTC)
        )
        assert out["detail"]["debits"]["n"] == 1
        assert out["drift"] == D("0.0000")
        assert out["ok"] is True
        assert not settings.halt_path.exists()

        # ... and the attempt that spent it is still pilot-era, so a live-era scope excludes
        # it. Two different questions, two different answers, on purpose.
        assert ledger.era_scope(LIVE_ERA).keeps(PILOT_TS) is False
        assert ledger.era_scope(ALL_ERAS).keeps(PILOT_TS) is True
    finally:
        ledger.close()


def test_impostor_scan_still_flags_an_order_naming_a_pilot_era_attempt(tmp_path):
    """An impostor is never late, it is foreign — and an impostor citing a pilot-era attempt
    id is exactly the shape an era-filtered scan would wave through.

    Driven through settle's shared-account scan, which is the one that survived docs/22
    section 7.3: reconcile's duplicate of it ran nightly over the same order pages and
    reached the same verdict, while this one runs every fifteen minutes and halts on the
    spot.
    """
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    settings = load_settings(root=tmp_path)
    fake = FakeKalshi(balance=D("30.16"))
    genesis = datetime(2026, 7, 31, 5, 44, 29, tzinfo=UTC)
    try:
        aid = _attempt(ledger, created_at="2026-07-13T14:08:07Z")   # PILOT era
        ledger.meta_set_many({
            "live_genesis_ts": "2026-07-31T05:44:29Z", "live_genesis_balance": "30.16",
        })
        fake.add_personal_order(
            "KXIMPOSTOR", client_order_id=f"{aid}-B01", ts=genesis + timedelta(hours=3),
            move_balance=False,
        )
        counts = settle_mod.settle_once(
            ledger, fake, settings, now=datetime(2026, 8, 1, 23, 5, tzinfo=UTC)
        )
        assert counts["impostors"] == 1
        events = ledger.audit_events(event="unknown_fill")
        assert [json.loads(e["detail"])["client_order_id"] for e in events] == [f"{aid}-B01"]
        assert settings.halt_path.exists()
        # …and nothing about it was written into the owner's own-orders table.
        assert ledger.personal_orders() == []
    finally:
        ledger.close()


