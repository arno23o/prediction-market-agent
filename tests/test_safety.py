"""Safety module: HALT lifecycle, the real_orders_allowed truth table (spec §14), and the
drawdown floor (Jul29 spec L4)."""

from decimal import Decimal as D

import pytest

from betting_agent.config import Settings
from betting_agent.harness import safety
from betting_agent.ledger.db import Ledger


def _settings(tmp_path, *, env="prod", live_trading=False, floor_pct=None):
    s = Settings()
    s._root = tmp_path
    s.kalshi.env = env
    s.stakes.live_trading = live_trading
    if floor_pct is not None:
        s.stakes.drawdown_floor_pct = D(floor_pct)
    return s


@pytest.fixture
def lg(tmp_path):
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    yield ledger
    ledger.close()


def _reconciled(lg, actual, *, run_at="2026-07-29T12:00:00Z"):
    lg.insert_reconciliation(
        run_at, expected_balance=D(actual), actual_balance=D(actual), drift=D("0"), ok=True,
    )


# --------------------------------------------------------------------------- HALT
def test_halt_lifecycle(tmp_path):
    s = _settings(tmp_path)
    assert not safety.is_halted(s)
    assert safety.halt_reason(s) is None

    safety.set_halt(s, "manual stop")
    assert safety.is_halted(s)
    assert s.halt_path.exists()
    assert safety.halt_reason(s) == "manual stop"
    # the file records a timestamp on a second line
    assert len(s.halt_path.read_text().splitlines()) == 2

    safety.clear_halt(s)
    assert not safety.is_halted(s)
    assert safety.halt_reason(s) is None


def test_clear_halt_idempotent(tmp_path):
    s = _settings(tmp_path)
    safety.clear_halt(s)  # no file yet -> no error
    safety.set_halt(s, "x")
    safety.clear_halt(s)
    safety.clear_halt(s)
    assert not safety.is_halted(s)


# --------------------------------------------------------------------------- gate
def test_gate_prod_no_live_trading(tmp_path):
    s = _settings(tmp_path, env="prod", live_trading=False)
    allowed, why = safety.real_orders_allowed(s)
    assert allowed is False
    assert "live_trading" in why


def test_gate_prod_live_trading(tmp_path):
    s = _settings(tmp_path, env="prod", live_trading=True)
    allowed, why = safety.real_orders_allowed(s)
    assert allowed is True
    assert why == "live_trading"


def test_gate_demo_always_allows(tmp_path):
    for lt in (False, True):
        s = _settings(tmp_path, env="demo", live_trading=lt)
        allowed, why = safety.real_orders_allowed(s)
        assert allowed is True
        assert why == "demo"


def test_gate_halt_overrides_everything(tmp_path):
    # halted beats demo and live_trading alike
    for env, lt in (("prod", True), ("demo", True), ("demo", False), ("prod", False)):
        s = _settings(tmp_path, env=env, live_trading=lt)
        safety.set_halt(s, "tripwire")
        allowed, why = safety.real_orders_allowed(s)
        assert allowed is False
        assert why.startswith("halted")
        safety.clear_halt(s)


# --------------------------------------------------------------------------- drawdown floor
def test_floor_refuses_when_balance_below_half_of_genesis(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)  # default floor_pct 0.50
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "9.9900")  # floor is 10.00

    allowed, why = safety.real_orders_allowed(s, lg)
    assert allowed is False
    assert why.startswith("drawdown_floor")
    assert "last reconciliation 9.9900" in why and "10.0000 floor" in why
    assert not s.halt_path.exists()  # the floor refuses; it never HALTs (reconcile owns HALT)


def test_floor_allows_exactly_at_the_floor(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "10.0000")  # strictly-below is the test, so the floor itself passes

    assert safety.real_orders_allowed(s, lg) == (True, "live_trading")


def test_floor_allows_above_the_floor(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "18.5000")

    assert safety.real_orders_allowed(s, lg) == (True, "live_trading")


def test_floor_applies_on_demo_too(tmp_path, lg):
    s = _settings(tmp_path, env="demo")
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "1.0000")

    allowed, why = safety.real_orders_allowed(s, lg)
    assert allowed is False and why.startswith("drawdown_floor")


def test_floor_respects_a_custom_pct(tmp_path, lg):
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "15.0000")
    # 15 clears a 50% floor but not a 90% one
    assert safety.real_orders_allowed(_settings(tmp_path, live_trading=True), lg)[0] is True
    strict = _settings(tmp_path, live_trading=True, floor_pct="0.90")
    allowed, why = safety.real_orders_allowed(strict, lg)
    assert allowed is False and why.startswith("drawdown_floor")


def test_floor_uses_the_latest_reconciliation(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "1.0000", run_at="2026-07-28T12:00:00Z")   # older, deep drawdown
    _reconciled(lg, "18.0000", run_at="2026-07-29T12:00:00Z")  # newest, recovered

    assert safety.real_orders_allowed(s, lg) == (True, "live_trading")


def test_floor_needs_a_reconciliation(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")  # genesis set, nothing reconciled yet

    assert safety.real_orders_allowed(s, lg) == (True, "live_trading")


def test_floor_needs_a_genesis_balance(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    _reconciled(lg, "0.5000")  # a tiny balance, but no live-era genesis to compare against

    assert safety.real_orders_allowed(s, lg) == (True, "live_trading")


def test_floor_refuses_when_the_stored_number_is_unreadable(tmp_path, lg):
    """An unevaluable money guard must not authorize new spend, and says so distinctly."""
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "twenty dollars")
    _reconciled(lg, "18.0000")

    allowed, why = safety.real_orders_allowed(s, lg)
    assert allowed is False
    assert why == "drawdown_floor: unreadable balance"
    assert not s.halt_path.exists()


def test_floor_skipped_entirely_without_a_ledger(tmp_path, lg):
    """Ledger-less callers keep the old two-argument truth table (CLI status, canary)."""
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "0.1000")

    assert safety.real_orders_allowed(s) == (True, "live_trading")
    assert safety.real_orders_allowed(s, None) == (True, "live_trading")


def test_halt_still_beats_the_floor(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "0.1000")
    safety.set_halt(s, "tripwire")

    allowed, why = safety.real_orders_allowed(s, lg)
    assert allowed is False
    assert why.startswith("halted")  # HALT keeps precedence; the floor never masks it


def test_gate_closed_reason_survives_the_floor(tmp_path, lg):
    # prod without live_trading refuses for the gate reason, not the floor
    s = _settings(tmp_path, env="prod", live_trading=False)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "0.1000")

    assert safety.real_orders_allowed(s, lg) == (False, "live_trading disabled")


# ------------------------------------------------- the fresh balance (docs/22 section 7.4)
class _Balance:
    def __init__(self, dollars):
        self.dollars = dollars


class _Client:
    """Just the one method the floor asks for. ``boom`` makes the call fail."""

    def __init__(self, dollars="0", boom=False):
        self._dollars = dollars
        self._boom = boom
        self.calls = 0

    def get_balance(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("exchange unreachable")
        return _Balance(D(self._dollars))


def test_the_floor_reads_a_live_balance_when_a_client_is_given(tmp_path, lg):
    """The floor used to be as stale as the last reconciliation, and under a HALT the
    reconciliation stops, so it froze at whatever it last saw. It asks the exchange now."""
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "18.0000")          # the stale number says everything is fine
    client = _Client("9.0000")          # the account actually holds $9

    allowed, why = safety.real_orders_allowed(s, lg, client)
    assert allowed is False
    assert "live balance 9.0000" in why and "10.0000 floor" in why
    assert client.calls == 1


def test_a_live_balance_above_the_floor_allows_despite_a_bad_reconciliation(tmp_path, lg):
    """The other direction: a deep drawdown that has since been funded stops refusing as
    soon as the money is back, rather than waiting for the next reconciliation."""
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "1.0000")
    assert safety.real_orders_allowed(s, lg, _Client("18.0000")) == (True, "live_trading")


def test_a_failed_balance_call_falls_back_to_the_reconciliation_and_says_so(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    _reconciled(lg, "9.0000")
    client = _Client(boom=True)

    allowed, why = safety.real_orders_allowed(s, lg, client)
    assert allowed is False
    assert "last reconciliation 9.0000" in why
    assert client.calls == 1            # it did try


def test_a_failed_balance_call_with_no_reconciliation_cannot_trip_the_floor(tmp_path, lg):
    """Nothing to compare against is not a drawdown. It was not one before the live read
    existed either, and a failed call must not invent one."""
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")
    assert safety.real_orders_allowed(s, lg, _Client(boom=True)) == (True, "live_trading")


# ------------------------------------- cash plus open positions (Arno, 2026-10-01)
class _BalanceWithPositions(_Balance):
    def __init__(self, dollars, positions):
        super().__init__(dollars)
        self.positions = positions


class _PositionsClient(_Client):
    def __init__(self, dollars, positions):
        super().__init__(dollars)
        self._positions = positions

    def get_balance(self):
        self.calls += 1
        return _BalanceWithPositions(D(self._dollars), self._positions)


def _open_leg(lg, *, stake, fee="0.0000", is_real=1, n=1):
    _, aid = lg.create_attempt(env="prod", model="m", effort="high", memory_mode="on",
                               prompt_version="p", toolkit_version="0.1.0", workspace_path="/ws")
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    lg.insert_bet(bet_id=f"{aid}-B0{n}", attempt_id=aid, ticket_index=n, ticker=f"KX{n}",
                  side="yes", limit_price=D("0.50"), rationale="r", is_real=is_real,
                  status="filled", contracts=3, fill_price=D("0.50"), stake=D(stake),
                  fee=D(fee), placed_at="2026-10-01T12:00:00Z")


def test_money_out_on_open_legs_does_not_trip_the_floor(tmp_path, lg):
    """The case that prompted the change: cash under the floor because it is out on legs
    that have not settled, while the account as a whole is well above it."""
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "150.0000")          # floor 75.00
    client = _PositionsClient("60.0000", D("101.5400"))       # cash alone would refuse
    assert safety.real_orders_allowed(s, lg, client) == (True, "live_trading")


def test_a_real_loss_still_trips_the_floor_and_names_both_numbers(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "150.0000")          # floor 75.00
    client = _PositionsClient("50.0000", D("20.0000"))        # 70 in all: a loss

    allowed, why = safety.real_orders_allowed(s, lg, client)
    assert allowed is False
    assert why.startswith("drawdown_floor: live balance 50.0000 plus positions at market "
                          "20.0000 is 70.0000, below the 75.0000 floor")


def test_positions_fall_back_to_the_ledger_cost_when_the_exchange_reports_none(tmp_path, lg):
    """A balance response without a position value (and every fake that omits it) counts
    the open real legs at cost, stake plus fee; paper legs never left the account."""
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")           # floor 10.00
    _open_leg(lg, stake="1.4000", fee="0.0600")
    _open_leg(lg, stake="5.0000", is_real=0, n=2)            # paper: not counted

    allowed, why = safety.real_orders_allowed(s, lg, _Client("8.5000"))
    assert allowed is False                                   # 8.50 + 1.46 = 9.96
    assert "live balance 8.5000 plus positions at cost 1.4600 is 9.9600" in why
    assert safety.real_orders_allowed(s, lg, _Client("8.6000")) == (True, "live_trading")


def test_the_reconciliation_fallback_adds_the_open_legs_at_cost(tmp_path, lg):
    s = _settings(tmp_path, env="prod", live_trading=True)
    lg.meta_set("live_genesis_balance", "20.0000")           # floor 10.00
    _reconciled(lg, "7.0000")
    _open_leg(lg, stake="3.0000")
    assert safety.real_orders_allowed(s, lg) == (True, "live_trading")
    assert safety.real_orders_allowed(s, lg, _Client(boom=True)) == (True, "live_trading")


def test_balance_reads_the_exchange_position_value():
    from betting_agent.kalshi.types import Balance

    b = Balance.from_api({"balance": 10324, "balance_dollars": "103.2411",
                          "portfolio_value": 8940})
    assert b.dollars == D("103.2411") and b.positions == D("89.40")
    assert Balance.from_api({"balance_dollars": "1.00", "portfolio_value_dollars": "2.5000",
                             "portfolio_value": 1}).positions == D("2.5000")
    assert Balance.from_api({"balance_dollars": "1.00"}).positions is None
