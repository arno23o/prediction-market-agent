"""The ``betting-agent`` operator CLI — the tick algorithm, attempt lock, safety (spec §15)."""

import fcntl
import gzip
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from typer.testing import CliRunner

from betting_agent import cli
from betting_agent.ledger.db import Ledger, _schema_expectations
from betting_agent.timeutil import ET, iso

# Derived, not typed: the schema version moves with every migration wave (see
# tests/test_migration.py), and these tests are not about migrations.

runner = CliRunner()

# Derived from the schema dir, never a literal (LG-6): a new migration must not break a
# test about the CLI.
_SCHEMA_VERSION = _schema_expectations()[0]

# Fixed clocks (America/New_York), converted to the UTC that ``utc_now`` returns.
SLOT_NOW = datetime(2026, 7, 7, 10, 5, tzinfo=ET).astimezone(UTC)   # inside 10:00 slot + grace
GRACE_NOW = datetime(2026, 7, 7, 13, 0, tzinfo=ET).astimezone(UTC)  # past 10:00 grace, before 16:00
EARLY_NOW = datetime(2026, 7, 7, 3, 0, tzinfo=ET).astimezone(UTC)   # before either slot
REPORT_NOW = datetime(2026, 7, 7, 21, 30, tzinfo=ET).astimezone(UTC)  # before 23:00 ET
LATE_NOW = datetime(2026, 7, 7, 23, 15, tzinfo=ET).astimezone(UTC)  # after reconcile.hour_et


@pytest.fixture
def root(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    # Two slots at the fixed clocks above, pinned rather than inherited: the operating
    # schedule is nine slots two hours and forty minutes apart, and these tests are about
    # the slot machinery, not the times. The cell counts are pinned with them, because
    # they have to total the slot count or every command here warns about the config.
    (tmp_path / "config.toml").write_text(
        '[schedule]\nslots = ["10:00", "16:00"]\n\n'
        "[cells]\nbaseline = 1\nstatic = 1\ndirector = 0\nfocused = 0\n"
    )
    lg = Ledger.open(tmp_path / "data" / "ledger.db")
    lg.migrate()
    lg.close()
    monkeypatch.setenv("BT_ROOT", str(tmp_path))
    return tmp_path


class Recorder:
    """Records calls; returns ``ret``. Accepts any signature (tick/attempt seams)."""

    def __init__(self, ret=None):
        self.calls = []
        self.ret = ret

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret

    @property
    def count(self):
        return len(self.calls)


def _patch_steps(monkeypatch, *, settle=None, reconcile=None, board=None, digest=None,
                 director=None):
    """Patch the tick's core seams with no-ops (or supplied recorders)."""
    # docs/22 section 6 item 4: the board step now only decides whether a detached child is
    # due. It belongs in the same neutralized set as settle/reconcile: a test about
    # another step must not spawn a process.
    monkeypatch.setattr(cli, "_board_if_due",
                        board or Recorder(ret={"status": "fresh", "age_seconds": 0}))
    # docs/22 section 8.2, for the same reason: from midnight on, every tick on a day with
    # no director run spawns one, and a test about the slot machinery must not see it.
    monkeypatch.setattr(cli, "_director_if_due",
                        director or Recorder(ret={"status": "not_due"}))
    monkeypatch.setattr(cli, "settle_once", settle or Recorder())
    monkeypatch.setattr(cli, "reconcile_once", reconcile or Recorder(ret={"ok": True}))
    # Hermetic default: the live-genesis step sees a closed gate unless a test opens it.
    monkeypatch.setattr(cli, "real_orders_allowed", lambda settings, ledger=None: (False, "test"))
    if digest is not None:
        monkeypatch.setattr(cli, "_digest_if_due", digest)


def _fake_popen(monkeypatch):
    rec = Recorder()

    def popen(argv, **kwargs):
        rec.calls.append((argv, kwargs))
        return object()

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    return rec


def _open_ro(root):
    return Ledger.open(root / "data" / "ledger.db", readonly=True)


def _audit_count(root, event):
    lg = _open_ro(root)
    n = len(lg.audit_events(event=event))
    lg.close()
    return n


def invoke(*args, env=None):
    return runner.invoke(cli.app, list(args), env=env)


# --------------------------------------------------------------------------- tick: guards
def test_tick_under_halt_still_settles_reconciles_backs_up_and_writes_the_digest(
    root, monkeypatch,
):
    """docs/22 section 7.1: the halt gates betting, not the machine.

    The old early exit meant a halted account stopped writing down what the exchange was
    doing to the positions it already held. Spec 7.1's list runs; the slot step, which is
    what buys a model session, does not.
    """
    (root / "data" / "HALT").write_text("stopped\n")
    settle, reconcile = Recorder(), Recorder(ret={"ok": True})
    _patch_steps(monkeypatch, settle=settle, reconcile=reconcile)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    popen = _fake_popen(monkeypatch)

    res = invoke("tick")

    assert res.exit_code == 0
    assert settle.count == 1
    assert reconcile.count == 1
    lg = _open_ro(root)
    assert lg.meta_get("last_backup_ts") is not None
    assert lg.meta_get("last_digest_date") == "2026-07-06"   # the day that just finished
    lg.close()
    assert (root / "data" / "status" / "2026-07-06.md").exists()
    assert popen.count == 0                      # nothing was spawned
    assert "HALT set mid-tick" in res.stdout


def test_the_tick_refreshes_no_board_under_halt(root, monkeypatch):
    """The board child was the one client-dependent step a halt did not stop, so a stopped
    system kept pulling the whole market listing every interval: 15 refreshes and several
    thousand requests across the 42-hour halt of 2026-09-20, for snapshots no attempt
    opened. It stops at the HALT file now, ahead of the staleness check."""
    real_board_step = cli._board_if_due
    (root / "data" / "HALT").write_text("stopped\n")
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "_board_if_due", real_board_step)       # the real gate
    monkeypatch.setattr(cli, "_maybe_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    popen = _fake_popen(monkeypatch)

    assert invoke("tick").exit_code == 0

    assert popen.count == 0          # a cold cache and a client, and still no child

    (root / "data" / "HALT").unlink()
    assert invoke("tick").exit_code == 0
    assert "board-refresh" in [a[0][-1] for a in popen.calls]        # and resume pulls one


def test_tick_under_halt_refuses_every_step_that_buys_a_session_and_audits_once(
    root, monkeypatch,
):
    """``_may_spawn`` is the gate now, and spec 7.1's list is what it lets through.

    The slot step is on the wrong side of it: it starts a paid model session, and it is not
    named in the spec's list of what runs under a halt. The audit row is one per tick, not
    one per gated step.
    """
    (root / "data" / "HALT").write_text("stopped\n")
    slots = Recorder()
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "_run_slots", slots)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)

    assert invoke("tick").exit_code == 0

    assert slots.count == 0
    assert _audit_count(root, "halt_mid_tick") == 1


def test_a_tick_on_a_ledger_behind_the_schema_exits_one_with_the_guards_message(
    root, monkeypatch,
):
    """The tick runs from an editable install every fifteen minutes, so it is the first
    thing to hit a merged migration. One clean line beats a traceback from whichever
    statement first touched a missing column."""
    _patch_steps(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION - 1}")
    lg.close()

    res = invoke("tick")

    assert res.exit_code == 1
    assert "run `betting-agent migrate`" in res.stdout


def test_tick_lock_contention_exits_zero(root, monkeypatch):
    settle = Recorder()
    _patch_steps(monkeypatch, settle=settle)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "tick.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = invoke("tick")
        assert res.exit_code == 0
        assert settle.count == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_tick_runs_steps_in_order(root, monkeypatch):
    settle = Recorder()
    _patch_steps(monkeypatch, settle=settle)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    assert invoke("tick").exit_code == 0
    assert settle.count == 1


def test_tick_step_order_includes_reconcile_invariants_and_the_digest(root, monkeypatch):
    """settle → reap → live_genesis → reconcile → invariants → backup → gc → board →
    cell_plan → director → slots → digest. (The stale-running reaper (ST-7) joins right
    after settle, the monthly retention pass (ST-11) sits after backup where it can only
    ever run on files an up-to-date backup has already outlived, docs/22 section 10 puts
    the invariants right after reconcile and the digest last of all, and section 8.2 puts
    the director between the day's plan and the first slot that reads its page.)"""
    order = []

    def record(name):
        def fn(*a, **k):
            order.append(name)
            return None
        return fn

    monkeypatch.setattr(cli, "settle_once", record("settle"))
    monkeypatch.setattr(cli, "_reap_stale_running", record("reap"))
    monkeypatch.setattr(cli, "_live_genesis_if_due", record("live_genesis"))
    monkeypatch.setattr(cli, "_reconcile_if_due", record("reconcile"))
    monkeypatch.setattr(cli, "_invariants_step", record("invariants"))
    monkeypatch.setattr(cli, "_backup_if_stale", record("backup"))
    monkeypatch.setattr(cli, "_gc_if_due", record("gc"))
    monkeypatch.setattr(cli, "_board_if_due", record("board"))
    monkeypatch.setattr(cli, "_cell_plan_if_due", record("cell_plan"))
    monkeypatch.setattr(cli, "_director_if_due", record("director"))
    monkeypatch.setattr(cli, "_run_slots", record("slots"))
    monkeypatch.setattr(cli, "_digest_if_due", record("digest"))
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)

    assert invoke("tick").exit_code == 0
    assert order == ["settle", "reap", "live_genesis", "reconcile", "invariants", "backup",
                     "gc", "board", "cell_plan", "director", "slots", "digest"]


# --------------------------------------------------------------------------- tick: reconcile
def test_reconcile_step_runs_after_hour_and_stamps_once(root, monkeypatch):
    rec = Recorder(ret={"ok": True, "drift": "0.0000"})
    _patch_steps(monkeypatch, reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    _fake_popen(monkeypatch)

    invoke("tick")
    invoke("tick")  # same ET day -> gated by the stamp

    assert rec.count == 1
    lg = _open_ro(root)
    assert lg.meta_get("last_reconcile_date") == "2026-07-07"
    lg.close()


def test_reconcile_step_not_before_hour(root, monkeypatch):
    rec = Recorder(ret={"ok": True})
    _patch_steps(monkeypatch, reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: REPORT_NOW)  # 21:30 ET < 23:00
    _fake_popen(monkeypatch)
    invoke("tick")
    assert rec.count == 0


def test_reconcile_paper_era_skip_leaves_the_day_unstamped(root, monkeypatch):
    rec = Recorder(ret={"skipped": "paper_era"})
    _patch_steps(monkeypatch, reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    _fake_popen(monkeypatch)
    invoke("tick")
    lg = _open_ro(root)
    assert lg.meta_get("last_reconcile_date") is None
    lg.close()


# --------------------------------------------------------------------------- tick: slots
def test_slot_fires_once_across_two_ticks(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)
    invoke("tick")
    invoke("tick")
    assert popen.count == 1
    argv = popen.calls[0][0]
    assert " ".join(argv[1:]).startswith(
        "-m betting_agent.cli attempt --slot slot:2026-07-07/10:00 --cell"
    )


def test_the_spawn_carries_the_cell_and_the_arm_the_days_plan_drew(root, monkeypatch):
    """docs/22 section 8.7: the tick reads the cell out of the stored plan and passes it,
    so the spawn log says which cell was launched. The slot's arm, a model and an effort,
    travels the same way (2026-09-26)."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)
    invoke("tick")

    first = _plan(root)[0]
    argv = popen.calls[0][0]
    assert " ".join(argv[1:]) == (
        f"-m betting_agent.cli attempt --slot slot:2026-07-07/10:00 --cell {first['cell']} "
        f"--model {first['model']} --effort {first['effort']}"
    )
    assert "--loop" not in argv and "--memory" not in argv


def test_a_failing_cell_lookup_is_counted_as_a_spawn_that_never_happened(root, monkeypatch):
    """The slot is consumed before the cell is looked up, so a ledger error reading the
    day's plan is as much a spawn that never happened as a ``Popen`` that raised. It used
    to escape uncounted, with the slot burned and nothing saying why."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(cli.cells, "cell_for_slot", boom)

    assert invoke("tick").exit_code == 0        # the step records it; the tick carries on
    assert popen.count == 0                     # nothing was spawned

    # Counted toward the spawn streak, the same as a Popen that raised, so a day of this
    # reaches a human instead of burning the slots silently.
    assert _streak(root, "attempt_spawn")["n"] == 1
    lg = _open_ro(root)
    errors = [json.loads(r["detail"]) for r in lg.audit_events(event="tick_step_error")]
    lg.close()
    assert [e["step"] for e in errors] == ["slots"]
    assert "database is locked" in errors[0]["error"]


def test_slot_past_grace_skipped_and_audited(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)
    popen = _fake_popen(monkeypatch)
    assert invoke("tick").exit_code == 0
    assert popen.count == 0  # 10:00 past grace, 16:00 not yet due
    assert _audit_count(root, "slot_skipped") == 1


def _skip_reason(root) -> str:
    lg = _open_ro(root)
    events = lg.audit_events(event="slot_skipped")
    lg.close()
    assert len(events) == 1
    return json.loads(events[0]["detail"])["reason"]


def test_slot_skip_reason_is_sleep_when_the_tick_gap_implies_sleep(root, monkeypatch):
    """docs/14 D9: a catch-up tick landing hours after the last one explains its skip as
    sleep, not some other cause (the Aug-1 shape, docs/12 §6 — a ~12.4h sleep burned three
    slots in one catch-up pass)."""
    _patch_steps(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("last_tick_ts", iso(GRACE_NOW - timedelta(hours=3)))
    lg.close()
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)

    assert invoke("tick").exit_code == 0
    assert _skip_reason(root) == "past_grace_sleep"


def test_slot_skip_reason_is_other_when_ticks_were_landing_on_schedule(root, monkeypatch):
    """A tick arriving on the normal ~15-minute cadence did not miss the slot by sleeping
    — something else did (HALT, lock contention) — and D9 must not mislabel it."""
    _patch_steps(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("last_tick_ts", iso(GRACE_NOW - timedelta(minutes=15)))
    lg.close()
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)

    assert invoke("tick").exit_code == 0
    assert _skip_reason(root) == "past_grace_other"


def test_slot_skip_reason_defaults_to_other_with_no_prior_tick(root, monkeypatch):
    """No ``last_tick_ts`` yet (the very first tick ever) can't imply sleep from a gap
    that doesn't exist — it must not guess."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)

    assert invoke("tick").exit_code == 0
    assert _skip_reason(root) == "past_grace_other"


def test_slot_not_spawned_when_attempt_lock_busy(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "attempt.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        invoke("tick")
        assert popen.count == 0
        # slot left unconsumed so a later tick can retry within grace
        lg = _open_ro(root)
        assert lg.meta_get("slot:2026-07-07/10:00") is None
        lg.close()
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


# ------------------------------------------------- tick: failed-slot re-offer (docs/14 D8)
_SLOT_KEY = "slot:2026-07-07/10:00"


def _marker(root, key=_SLOT_KEY):
    lg = _open_ro(root)
    value = lg.meta_get(key)
    lg.close()
    return value


def _stub_attempt_child(monkeypatch, *, status="failed"):
    """Stand in for ``run_attempt``: a real attempt row that reaches ``status``."""
    ids = []

    def fake_run_attempt(ledger, client, settings, *, model, variant, slot,
                         effort=None, cell=None, cell_forced=False):
        _seq, aid = ledger.create_attempt(
            env="prod", model="m", effort="high", memory_mode="on", prompt_version="p",
            toolkit_version="0.1.0", workspace_path="/ws", slot=slot, cell=cell,
            cell_forced=int(bool(cell_forced)),
        )
        ledger.transition(aid, "running")
        ledger.transition(aid, status)
        ids.append(aid)
        return aid

    monkeypatch.setattr(cli, "run_attempt", fake_run_attempt)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    return ids


def test_a_failed_attempt_leaves_its_slot_re_offerable(root, monkeypatch):
    """docs/12 §8.12: a slot consumed by a spawn that died in seconds looked exactly like
    one that ran for three hours, so no wedged slot was ever retried."""
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    ids = _stub_attempt_child(monkeypatch)

    assert invoke("attempt", "--slot", _SLOT_KEY).exit_code == 0

    assert _marker(root) == f"failed:{ids[0]}"
    lg = _open_ro(root)
    events = lg.audit_events(event="slot_attempt_failed")
    lg.close()
    assert len(events) == 1
    assert json.loads(events[0]["detail"])["attempt_id"] == ids[0]


def test_a_finished_attempt_still_consumes_its_slot(root, monkeypatch):
    """Regression: only ``failed`` is re-offerable. placed/no_bets/ticket_invalid are the
    day's answer for that slot, whatever the attempt thought of the markets."""
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    ids = _stub_attempt_child(monkeypatch, status="no_bets")

    assert invoke("attempt", "--slot", _SLOT_KEY).exit_code == 0

    assert _marker(root) == f"running:{ids[0]}"
    assert _audit_count(root, "slot_attempt_failed") == 0


def test_a_failure_past_its_grace_window_consumes_the_slot(root, monkeypatch):
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)   # 13:00 ET, 10:00 + 2h gone
    ids = _stub_attempt_child(monkeypatch)

    assert invoke("attempt", "--slot", _SLOT_KEY).exit_code == 0

    assert _marker(root) == f"running:{ids[0]}"              # nothing left to re-offer into
    assert _audit_count(root, "slot_attempt_failed") == 0


def test_a_failed_slot_is_re_offered_within_grace_and_succeeds_on_the_retry(root, monkeypatch):
    """The whole D8 cycle across two ticks: spawn, fail, re-offer, run."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)

    invoke("tick")                                            # tick 1 consumes the slot
    assert popen.count == 1
    assert _marker(root).startswith("pending:")

    failing = _stub_attempt_child(monkeypatch)                # the child dies on arrival
    assert invoke("attempt", "--slot", _SLOT_KEY).exit_code == 0
    assert _marker(root) == f"failed:{failing[0]}"

    invoke("tick")                                            # tick 2 re-offers it
    assert popen.count == 2                                   # no spawn grace to wait out
    assert _marker(root).startswith("pending:")

    succeeding = _stub_attempt_child(monkeypatch, status="placed")
    assert invoke("attempt", "--slot", _SLOT_KEY).exit_code == 0

    assert succeeding[0] != failing[0]                        # a fresh attempt id, as required
    assert _marker(root) == f"running:{succeeding[0]}"
    lg = _open_ro(root)
    respawned = lg.audit_events(event="slot_respawned")
    lg.close()
    assert [json.loads(e["detail"])["was"] for e in respawned] == [f"failed:{failing[0]}"]


def test_a_failed_slot_stops_being_re_offered_when_grace_expires(root, monkeypatch):
    _patch_steps(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set(_SLOT_KEY, "failed:A-0001")
    lg.close()
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)
    popen = _fake_popen(monkeypatch)

    invoke("tick")

    assert popen.count == 0
    assert _marker(root) == "failed:A-0001"   # kept: an attempt DID run, it is not 'lost'
    assert _audit_count(root, "slot_spawn_lost") == 0
    assert _audit_count(root, "slot_respawned") == 0


def test_a_failed_slot_is_not_re_offered_while_an_attempt_holds_the_lock(root, monkeypatch):
    _patch_steps(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set(_SLOT_KEY, "failed:A-0001")
    lg.close()
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "attempt.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        invoke("tick")
        assert popen.count == 0
        assert _marker(root) == "failed:A-0001"
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


# ------------------------------------------------- attempt: auth preflight (docs/14 D8)
def test_auth_preflight_failure_burns_neither_an_attempt_row_nor_the_slot(root, monkeypatch):
    """docs/12 §8.12: every OAuth-dead spawn consumed a slot, for an attempt that never
    existed. OR-4 covered Kalshi credentials only."""
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    run_attempt = Recorder(ret="A-0007")
    monkeypatch.setattr(cli, "run_attempt", run_attempt)
    client = Recorder(ret=object())
    monkeypatch.setattr(cli, "_client", client)
    monkeypatch.setattr(cli, "_auth_preflight", lambda settings: (False, "logged out"))

    res = invoke("attempt", "--slot", _SLOT_KEY)

    assert res.exit_code == 1
    assert "preflight failed" in res.stdout
    assert run_attempt.count == 0        # no session, no attempt row
    assert client.count == 1             # OR-4's ordering is unchanged: client, then this
    assert _marker(root).startswith("pending:")   # the slot is offerable again
    lg = _open_ro(root)
    events = lg.audit_events(event="auth_preflight_failed")
    lg.close()
    assert len(events) == 1
    assert json.loads(events[0]["detail"])["slot"] == _SLOT_KEY


def test_the_re_offered_slot_runs_once_auth_is_back(root, monkeypatch):
    """The refused slot goes back to ``pending``, so the next tick re-offers it through
    the ordinary OR-2/ST-5 path — spawn grace and all."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    monkeypatch.setattr(cli, "run_attempt", Recorder(ret="A-0007"))
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "_auth_preflight", lambda settings: (False, "logged out"))
    invoke("attempt", "--slot", _SLOT_KEY)

    popen = _fake_popen(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW + timedelta(minutes=15))
    invoke("tick")                        # the next tick, one tick interval later

    assert popen.count == 1
    assert f"attempt --slot {_SLOT_KEY} --cell" in " ".join(popen.calls[0][0][1:])


def test_a_passing_preflight_changes_nothing(root, monkeypatch):
    calls = []
    _capture_run_attempt(monkeypatch, calls)
    monkeypatch.setattr(cli, "_auth_preflight", lambda settings: (True, "logged in"))
    assert invoke("attempt").exit_code == 0
    assert len(calls) == 1


# --------------------------------------------------------------------------- tick: backup
def test_backup_when_stale_then_skipped_when_fresh(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    invoke("tick")  # last_backup_ts absent -> stale -> backup
    files = list((root / "data" / "backups").glob("*.db"))
    assert len(files) == 1
    lg = _open_ro(root)
    assert lg.meta_get("last_backup_ts") is not None
    lg.close()
    invoke("tick")  # same now -> fresh -> no new backup
    assert len(list((root / "data" / "backups").glob("*.db"))) == 1


# --------------------------------------------------------------------------- tick: digest
def test_the_digest_step_archives_the_finished_day_once(root, monkeypatch):
    """docs/22 section 10: the first tick on or after 00:00 Eastern files the day that has
    just FINISHED, not the one that has just started.

    A page written at 03:00 about the day it names would describe three hours. The tick at
    2026-07-07 files 2026-07-06, and stamps that day so the rest of the day's ticks write
    nothing.
    """
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)

    assert invoke("tick").exit_code == 0
    assert invoke("tick").exit_code == 0             # same ET day: nothing written twice

    page = root / "data" / "status" / "2026-07-06.md"
    assert page.exists()
    assert not (root / "data" / "status" / "2026-07-07.md").exists()
    text = page.read_text(encoding="utf-8")
    assert text.startswith("# Status 2026-07-06")
    assert "## Invariants" in text

    log = (root / "data" / "logs" / "status.log").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1
    assert " balance=" in log[0] and " open=" in log[0] and " attempts=" in log[0]
    assert " placed=" in log[0] and log[0].endswith("halted=no")

    lg = _open_ro(root)
    assert lg.meta_get("last_digest_date") == "2026-07-06"
    assert lg.meta_get("last_digest_ts") is not None
    lg.close()

    # The next ET day files its own finished day beside the first.
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW + timedelta(days=1))
    assert invoke("tick").exit_code == 0
    assert (root / "data" / "status" / "2026-07-07.md").exists()
    assert len((root / "data" / "logs" / "status.log")
               .read_text(encoding="utf-8").splitlines()) == 2


def test_status_renders_today_while_the_tick_archives_yesterday(root, monkeypatch):
    """The two callers ask different questions and the renderer answers both."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)

    assert invoke("tick").exit_code == 0
    assert (root / "data" / "status" / "2026-07-06.md").exists()

    res = invoke("status")

    assert res.exit_code == 0
    assert "# Status 2026-07-07" in res.stdout


def test_the_digest_step_writes_the_page_under_halt(root, monkeypatch):
    """The day the digest is most worth reading is the day nothing ran."""
    (root / "data" / "HALT").write_text("stopped\n")
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)

    assert invoke("tick").exit_code == 0

    page = (root / "data" / "status" / "2026-07-06.md").read_text(encoding="utf-8")
    assert "- halt: YES (stopped)" in page


# --------------------------------------------------------------------------- tick: invariants
def _seed_an_orphan_bet(root):
    """One bet whose attempt does not exist: the shape ``no_orphan_bets`` exists to find."""
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.conn.execute("PRAGMA foreign_keys=OFF")      # the defect the check exists to find
    lg.conn.execute(
        "INSERT INTO bets (bet_id, attempt_id, ticket_index, ticker, side, limit_price, "
        "rationale, status) VALUES ('X-B01','A-9999',1,'T','yes','0.4000','r','no_fill')"
    )
    lg.conn.commit()
    lg.close()


def test_a_failing_invariant_alerts_once_per_outage_and_not_once_per_tick(
    root, monkeypatch, notify_calls,
):
    """docs/22 section 10 says the step "raises an alert", and read literally that is one
    banner every fifteen minutes: ninety-six a day for one orphan row, which is the noise
    docs/12 §8.1 exists to stop. It goes through the streak core at threshold one instead,
    so the first failing tick alerts and the rest are counted under their own key.
    """
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _seed_an_orphan_bet(root)

    for _ in range(4):
        assert invoke("tick").exit_code == 0

    lg = _open_ro(root)
    alerts = [json.loads(r["detail"]) for r in lg.audit_events(event="alert_raised")]
    streak = json.loads(lg.meta_get("alert_streak:invariant:no_orphan_bets"))
    lg.close()
    assert [a["key"] for a in alerts] == ["invariant:no_orphan_bets"]
    assert "X-B01" in alerts[0]["failing"]
    assert alerts[0]["streak"] == 1 and alerts[0]["threshold"] == 1
    assert streak == {"n": 4, "notified": True}
    assert len(notify_calls) == 1


def test_a_cleared_invariant_re_arms_its_alert(root, monkeypatch, notify_calls):
    """The next outage has to reach a human too, so a passing tick clears the streak."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _seed_an_orphan_bet(root)
    assert invoke("tick").exit_code == 0
    assert len(notify_calls) == 1

    lg = Ledger.open(root / "data" / "ledger.db")
    lg.conn.execute("DELETE FROM bets WHERE bet_id='X-B01'")
    lg.conn.commit()
    lg.close()
    assert invoke("tick").exit_code == 0

    lg = _open_ro(root)
    assert json.loads(lg.meta_get("alert_streak:invariant:no_orphan_bets")) == {
        "n": 0, "notified": False,
    }
    lg.close()

    _seed_an_orphan_bet(root)
    assert invoke("tick").exit_code == 0
    assert len(notify_calls) == 2


# --------------------------------------------------------------------------- tick: step isolation
def test_step_failure_does_not_block_later_steps(root, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("settle broke")

    backup = Recorder()
    _patch_steps(monkeypatch, settle=boom)
    monkeypatch.setattr(cli, "_backup_if_stale", backup)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    res = invoke("tick")
    assert res.exit_code == 0
    assert backup.count == 1
    assert _audit_count(root, "tick_step_error") == 1
    lg = _open_ro(root)
    assert lg.meta_get("last_tick_ts") is not None  # stamped despite the failure
    lg.close()


# --------------------------------------------------------------------------- attempt
def _capture_run_attempt(monkeypatch, calls):
    def fake_run_attempt(ledger, client, settings, *, model, effort, variant, slot,
                         cell=None, cell_forced=False):
        calls.append({"variant": variant, "model": model, "effort": effort, "slot": slot,
                      "cell": cell, "cell_forced": cell_forced})
        return f"A-{len(calls):04d}"

    monkeypatch.setattr(cli, "run_attempt", fake_run_attempt)
    monkeypatch.setattr(cli, "_client", lambda settings: object())


def _plan_cell(root, hhmm, day="2026-07-07"):
    """The cell the day's plan gives a slot, drawing the plan first if it is not stored."""
    invoke("plan", "--date", day)
    return _plan(root, day)[cli._settings().schedule.slots.index(hhmm)]["cell"]


def test_attempt_calls_run_attempt_with_kwargs_and_releases_lock(root, monkeypatch):
    calls = []
    _capture_run_attempt(monkeypatch, calls)
    planned = _plan_cell(root, "10:00")
    res = invoke("attempt", "--slot", _SLOT_KEY, "--cell", planned, "--model", "m2")
    assert res.exit_code == 0
    assert res.stdout.strip() == "A-0001"
    assert calls[0] == {"model": "m2", "effort": None, "variant": None,
                        "slot": _SLOT_KEY, "cell": planned,
                        # The tick passes the plan's own cell alongside its slot, so a
                        # spawned attempt is not a forced one.
                        "cell_forced": False}
    # lock is released: acquiring it now must succeed
    assert cli._lock_free(root / "data" / "locks" / "attempt.lock")


def test_a_cell_with_no_slot_is_an_operator_forced_run(root, monkeypatch):
    """docs/22 section 8.7: ``--cell`` with no slot to check it against bypassed the day's
    plan by construction, and the row says so."""
    calls = []
    _capture_run_attempt(monkeypatch, calls)
    assert invoke("attempt", "--cell", "baseline").exit_code == 0
    assert calls[0]["cell"] == "baseline" and calls[0]["cell_forced"] is True


def test_a_cell_that_matches_the_slots_plan_is_not_forced(root, monkeypatch):
    """The tick's own spawn: the plan's cell alongside the slot it came from."""
    calls = []
    _capture_run_attempt(monkeypatch, calls)
    planned = _plan_cell(root, "10:00")

    assert invoke("attempt", "--slot", _SLOT_KEY, "--cell", planned).exit_code == 0

    assert calls[0]["cell"] == planned and calls[0]["cell_forced"] is False


def test_a_cell_that_overrides_the_slots_plan_is_forced(root, monkeypatch):
    """An operator naming a different cell for a scheduled slot bypassed the plan just as
    surely as one naming a cell with no slot at all."""
    calls = []
    _capture_run_attempt(monkeypatch, calls)
    planned = _plan_cell(root, "10:00")
    other = next(c for c in ("baseline", "static", "director") if c != planned)

    assert invoke("attempt", "--slot", _SLOT_KEY, "--cell", other).exit_code == 0

    assert calls[0]["cell"] == other and calls[0]["cell_forced"] is True


def test_attempt_without_a_cell_leaves_the_plan_to_the_runner(root, monkeypatch):
    calls = []
    _capture_run_attempt(monkeypatch, calls)
    assert invoke("attempt").exit_code == 0
    assert calls[0]["cell"] is None and calls[0]["cell_forced"] is False


@pytest.mark.parametrize("cell", ["baseline", "static", "director", "focused"])
def test_every_cell_name_is_accepted(root, monkeypatch, cell):
    calls = []
    _capture_run_attempt(monkeypatch, calls)
    assert invoke("attempt", "--cell", cell).exit_code == 0
    assert calls[0]["cell"] == cell


def test_attempt_bad_cell_is_usage_error(root):
    res = invoke("attempt", "--cell", "sometimes")
    assert res.exit_code == 2


def test_attempt_hands_the_model_and_effort_overrides_to_the_runner(root, monkeypatch):
    """``--model`` and ``--effort`` override the slot's arm. Left out, each reaches the
    runner as ``None``, and the runner takes it from the slot's plan."""
    calls = []
    _capture_run_attempt(monkeypatch, calls)

    assert invoke("attempt", "--slot", _SLOT_KEY, "--model", "claude-sonnet-5",
                  "--effort", "low").exit_code == 0
    assert invoke("attempt", "--slot", _SLOT_KEY).exit_code == 0

    assert (calls[0]["model"], calls[0]["effort"]) == ("claude-sonnet-5", "low")
    assert (calls[1]["model"], calls[1]["effort"]) == (None, None)


def test_attempt_bad_effort_is_usage_error(root, monkeypatch):
    """Refused before anything is written down: ``claude`` would refuse it anyway, after
    the attempt row and the slot marker existed."""
    run_attempt = Recorder(ret="A-0001")
    monkeypatch.setattr(cli, "run_attempt", run_attempt)

    res = invoke("attempt", "--effort", "extreme")

    assert res.exit_code == 2
    assert "--effort must be one of low, medium, high, xhigh, max" in res.stdout
    assert run_attempt.count == 0


@pytest.mark.parametrize("flag", ["--loop", "--memory"])
def test_the_retired_flags_are_gone(root, flag):
    """``--loop`` and ``--memory`` left with the two-loop code and the arms."""
    assert invoke("attempt", flag, "two").exit_code == 2


def test_attempt_busy_lock_exits_1(root, monkeypatch):
    monkeypatch.setattr(cli, "run_attempt", Recorder(ret="X"))
    monkeypatch.setattr(cli, "_client", lambda settings: None)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "attempt.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = invoke("attempt")
        assert res.exit_code == 1
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_client_construction_failure_records_no_attempt(root, monkeypatch):
    """OR-4: a missing-credentials failure happens before anything is written down."""
    run_attempt = Recorder(ret="A-0007")
    monkeypatch.setattr(cli, "run_attempt", run_attempt)
    monkeypatch.setattr(
        cli, "_client",
        lambda settings: (_ for _ in ()).throw(RuntimeError("no credentials")),
    )
    res = invoke("attempt")
    assert res.exit_code != 0
    assert run_attempt.count == 0          # run_attempt was never reached
    lg = _open_ro(root)
    assert lg.conn.execute("SELECT COUNT(*) AS n FROM attempts").fetchone()["n"] == 0
    lg.close()


def test_the_operator_variant_reaches_the_runner_untouched(root, monkeypatch):
    calls = []
    _capture_run_attempt(monkeypatch, calls)
    res = invoke("attempt", "--variant", '{"note":"pilot"}')
    assert res.exit_code == 0
    assert json.loads(calls[0]["variant"]) == {"note": "pilot"}


# --------------------------------------------------------------------------- the day's plan
def _plan(root, day="2026-07-07"):
    lg = _open_ro(root)
    row = lg.cell_plan(day)
    lg.close()
    return json.loads(row["plan"]) if row else None


def test_the_tick_draws_the_days_plan_before_the_slots(root, monkeypatch):
    """docs/22 sections 5.1 and 8.7: stored before the first slot runs, so the day is
    knowable in advance."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _fake_popen(monkeypatch)
    assert _plan(root) is None

    invoke("tick")

    plan = _plan(root)
    assert plan is not None and len(plan) == 2       # two slots in this fixture's config


def test_the_plan_is_drawn_once_a_day(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _fake_popen(monkeypatch)
    invoke("tick")
    first = _plan(root)
    invoke("tick")
    assert _plan(root) == first


def _plan_line(hhmm: str, entry: dict) -> str:
    return f"  {hhmm}  {entry['cell']:<8}  {entry['model']} {entry['effort']}"


def test_the_plan_command_prints_the_day_slot_by_slot_with_each_arm(root):
    res = invoke("plan")
    assert res.exit_code == 0
    plan = _plan(root, cli.et_day(cli.utc_now()))
    assert plan is not None
    for hhmm, entry in zip(["10:00", "16:00"], plan, strict=True):
        assert _plan_line(hhmm, entry) in res.stdout.splitlines()


def test_the_plan_command_draws_a_named_day(root):
    res = invoke("plan", "--date", "2026-07-07")
    assert res.exit_code == 0
    assert "cell plan 2026-07-07:" in res.stdout
    assert _plan(root) is not None


def test_the_plan_command_refuses_a_bad_date(root):
    assert invoke("plan", "--date", "yesterday").exit_code == 2


def test_status_prints_todays_plan_after_the_digest(root):
    invoke("plan")
    res = invoke("status")
    assert res.exit_code == 0
    today = cli.et_day(cli.utc_now())
    assert f"cell plan {today}:" in res.stdout
    for hhmm, entry in zip(["10:00", "16:00"], _plan(root, today), strict=True):
        assert _plan_line(hhmm, entry) in res.stdout.splitlines()


def test_status_reads_a_plan_stored_before_arms(root):
    """Today's live row is bare cell names; it reads as ``attempt.model`` at
    ``attempt.effort``, on the page and in ``--json``."""
    today = cli.et_day(cli.utc_now())
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.set_cell_plan(today, 7, json.dumps(["static", "baseline"]))
    lg.close()

    res = invoke("status", "--json")

    assert res.exit_code == 0
    assert "  10:00  static    claude-opus-5-5 high" in res.stdout.splitlines()
    assert "  16:00  baseline  claude-opus-5-5 high" in res.stdout.splitlines()
    data = json.loads(res.stdout[res.stdout.index("\n{") + 1:])
    assert data["cell_plan"][0] == {"cell": "static", "model": "claude-opus-5-5",
                                    "effort": "high"}


def test_status_says_so_when_the_plan_has_not_been_drawn(root):
    res = invoke("status")
    assert res.exit_code == 0
    assert "not drawn yet" in res.stdout


# --------------------------------------------------------------------------- the director
# The real step, captured before ``_patch_steps`` neutralizes it: these tests are the ones
# that want it to run.
_REAL_DIRECTOR_STEP = cli._director_if_due

# A later director hour than the operating 00:00, because "before the hour" is not a time
# that exists when the hour is midnight. The cohort under review is 2026-07-06.
DIRECTOR_BEFORE = datetime(2026, 7, 7, 3, 30, tzinfo=ET).astimezone(UTC)
DIRECTOR_AT = datetime(2026, 7, 7, 4, 5, tzinfo=ET).astimezone(UTC)
DIRECTOR_LATE = datetime(2026, 7, 7, 5, 10, tzinfo=ET).astimezone(UTC)  # the hour plus 60


def _director_hour(root, hhmm="04:00"):
    with open(root / "config.toml", "a") as f:
        f.write(f'\n[director]\nhour_et = "{hhmm}"\n')


def _director_tick(root, monkeypatch, now):
    """A tick with only the director step live, and every spawn recorded."""
    _director_hour(root)
    _patch_steps(monkeypatch, director=_REAL_DIRECTOR_STEP)
    monkeypatch.setattr(cli, "utc_now", lambda: now)
    popen = _fake_popen(monkeypatch)
    res = invoke("tick")
    assert res.exit_code == 0, res.stdout
    return [a[0] for a in popen.calls], res


def _spawned_director(argvs) -> list[list[str]]:
    return [a[3:] for a in argvs if "director" in a]


def _yesterdays_attempt(root, *, status="running"):
    lg = Ledger.open(root / "data" / "ledger.db")
    _seq, aid = lg.create_attempt(
        env="demo", model="m", effort="high", memory_mode="on", prompt_version="p",
        toolkit_version="0.1.0", workspace_path="/ws", slot="slot:2026-07-06/22:20",
    )
    lg.transition(aid, "running")
    if status != "running":
        lg.transition(aid, status)
    lg.close()
    return aid


def _seed_director_run(root, run_date="2026-07-07", status="valid"):
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.insert_director_run(f"D-{run_date}", run_date=run_date, cohort_date="2026-07-06",
                           started_at="2026-07-07T04:00:00Z", session_id=None, model="m")
    if status != "running":
        lg.finish_director_run(f"D-{run_date}", status=status,
                               ended_at="2026-07-07T04:30:00Z", page_md="p",
                               page_hash="deadbeefcafe")
    lg.close()


def test_the_director_does_not_run_before_its_hour(root, monkeypatch):
    argvs, _res = _director_tick(root, monkeypatch, DIRECTOR_BEFORE)
    assert _spawned_director(argvs) == []


def test_the_director_waits_for_a_slotless_attempt_of_the_cohort(root, monkeypatch):
    """An attempt an operator ran by hand has no slot key and is a member all the same.

    Cohort membership falls back to the Eastern day the attempt was created on, so the
    wait rule asks the cohort rather than reading slot markers.
    """
    lg = Ledger.open(root / "data" / "ledger.db")
    _seq, aid = lg.create_attempt(
        env="demo", model="m", effort="high", memory_mode="on", prompt_version="p",
        toolkit_version="0.1.0", workspace_path="/ws", slot=None,
    )
    lg.transition(aid, "running")
    # 23:30 ET on the cohort's day, half an hour before the tick: on the right day, and
    # recent enough that the stale-running reaper two steps earlier leaves it alone.
    lg.conn.execute("UPDATE attempts SET created_at=? WHERE attempt_id=?",
                    ("2026-07-07T03:30:00Z", aid))
    lg.conn.commit()
    lg.close()

    argvs, _res = _director_tick(root, monkeypatch, DIRECTOR_AT)

    assert _spawned_director(argvs) == []


def test_the_director_waits_for_an_attempt_of_the_cohort_it_reviews(root, monkeypatch):
    """docs/22 section 5.1: the review wants the whole cohort, so it waits for the hour."""
    _yesterdays_attempt(root)

    argvs, _res = _director_tick(root, monkeypatch, DIRECTOR_AT)

    assert _spawned_director(argvs) == []


def test_the_director_spawns_at_its_hour_once_the_cohort_has_finished(root, monkeypatch):
    _yesterdays_attempt(root, status="no_bets")

    argvs, _res = _director_tick(root, monkeypatch, DIRECTOR_AT)

    assert _spawned_director(argvs) == [["director", "--date", "2026-07-07"]]


def test_an_attempt_still_running_an_hour_later_does_not_cost_the_day_its_direction(
    root, monkeypatch,
):
    """It is left out of the prospective review and reviewed with its cohort later."""
    _yesterdays_attempt(root)

    argvs, _res = _director_tick(root, monkeypatch, DIRECTOR_LATE)

    assert _spawned_director(argvs) == [["director", "--date", "2026-07-07"]]


def test_the_director_is_never_spawned_twice_for_one_run_date(root, monkeypatch):
    """The row the child opens is what says the day has had its run, whatever it said."""
    _seed_director_run(root, status="invalid")

    argvs, _res = _director_tick(root, monkeypatch, DIRECTOR_AT)

    assert _spawned_director(argvs) == []


def test_a_second_tick_the_same_day_spawns_no_second_director(root, monkeypatch):
    _director_hour(root)
    _patch_steps(monkeypatch, director=_REAL_DIRECTOR_STEP)
    monkeypatch.setattr(cli, "utc_now", lambda: DIRECTOR_AT)
    popen = _fake_popen(monkeypatch)

    invoke("tick")
    _seed_director_run(root, status="running")     # the child's row, as it would be
    invoke("tick")

    assert len(_spawned_director([a[0] for a in popen.calls])) == 1


def test_the_director_step_is_refused_under_halt(root, monkeypatch):
    (root / "data" / "HALT").write_text("stopped\n")

    argvs, res = _director_tick(root, monkeypatch, DIRECTOR_AT)

    assert _spawned_director(argvs) == []
    assert "HALT set mid-tick" in res.stdout
    assert _audit_count(root, "halt_mid_tick") == 1


def test_the_tick_creates_the_director_directory(root, monkeypatch):
    _director_tick(root, monkeypatch, DIRECTOR_AT)
    assert (root / "data" / "director").is_dir()


def test_an_unreadable_director_hour_costs_the_step_and_nothing_else(root, monkeypatch):
    """The hour raises rather than meaning midnight, and a tick step isolates that."""
    _director_hour(root, hhmm="lunchtime")
    _patch_steps(monkeypatch, director=_REAL_DIRECTOR_STEP)
    monkeypatch.setattr(cli, "utc_now", lambda: DIRECTOR_AT)
    popen = _fake_popen(monkeypatch)

    res = invoke("tick")

    assert res.exit_code == 0
    assert "step 'director' failed" in res.stdout
    assert _spawned_director([a[0] for a in popen.calls]) == []
    assert _audit_count(root, "tick_step_error") == 1


# ------------------------------------------------------------------ the director command
def _fake_run_director(monkeypatch, ret=None):
    rec = Recorder(ret=ret or {"run_id": "D-2026-07-07", "status": "valid", "error": None})
    monkeypatch.setattr(cli, "run_director", rec)
    return rec


def test_the_director_command_runs_the_day_and_prints_its_id_and_status(root, monkeypatch):
    rec = _fake_run_director(monkeypatch)

    res = invoke("director", "--date", "2026-07-07")

    assert res.exit_code == 0, res.stdout
    assert "D-2026-07-07 valid" in res.stdout
    assert rec.calls[0][1]["run_date"] == "2026-07-07"


def test_the_director_command_prints_the_reason_a_run_was_rejected(root, monkeypatch):
    _fake_run_director(monkeypatch, ret={"run_id": "D-2026-07-07", "status": "invalid",
                                         "error": "balanced_note is empty"})

    res = invoke("director", "--date", "2026-07-07")

    assert res.exit_code == 0
    assert "D-2026-07-07 invalid: balanced_note is empty" in res.stdout


def test_the_director_command_defaults_to_today(root, monkeypatch):
    rec = _fake_run_director(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: DIRECTOR_AT)

    assert invoke("director").exit_code == 0
    assert rec.calls[0][1]["run_date"] == "2026-07-07"


def test_the_director_command_refuses_under_halt_and_force_overrides(root, monkeypatch):
    rec = _fake_run_director(monkeypatch)
    (root / "data" / "HALT").write_text("stopped\n")

    refused = invoke("director", "--date", "2026-07-07")
    assert refused.exit_code == 0 and rec.count == 0
    assert "refusing (use --force)" in refused.stdout

    forced = invoke("director", "--date", "2026-07-07", "--force")
    assert forced.exit_code == 0 and rec.count == 1


def test_the_director_command_refuses_a_bad_date(root, monkeypatch):
    rec = _fake_run_director(monkeypatch)
    assert invoke("director", "--date", "yesterday").exit_code == 2
    assert rec.count == 0


def test_the_director_command_refuses_a_day_that_already_ran(root, monkeypatch):
    rec = _fake_run_director(monkeypatch)
    _seed_director_run(root, status="failed")

    res = invoke("director", "--date", "2026-07-07")

    assert res.exit_code == 0 and rec.count == 0
    assert "already ran (failed)" in res.stdout


def test_the_director_command_refuses_while_its_lock_is_held(root, monkeypatch):
    rec = _fake_run_director(monkeypatch)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "director.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = invoke("director", "--date", "2026-07-07")
        assert res.exit_code == 1 and rec.count == 0
        assert "director.lock busy" in res.stdout
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_status_prints_the_latest_director_run_after_the_plan(root):
    _seed_director_run(root)

    res = invoke("status")

    assert res.exit_code == 0
    assert "director 2026-07-07: valid  page deadbeefcafe" in res.stdout
    assert res.stdout.index("cell plan") < res.stdout.index("director 2026-07-07")


def test_status_says_so_when_no_director_has_run(root):
    assert "director: no run yet" in invoke("status").stdout


# --------------------------------------------------------------------------- halt / resume / status
def test_halt_creates_file_and_audits(root):
    res = invoke("halt", "manual stop")
    assert res.exit_code == 0
    assert (root / "data" / "HALT").exists()
    assert _audit_count(root, "halt_set") == 1


def test_resume_clears_halt_and_audits(root):
    invoke("halt", "manual stop")
    res = invoke("resume")
    assert res.exit_code == 0
    assert not (root / "data" / "HALT").exists()
    assert _audit_count(root, "halt_cleared") == 1


def test_status_prints_the_digest(root):
    """docs/22 section 10: ``status`` is the digest on demand, not its own summary."""
    lg = Ledger.open(root / "data" / "ledger.db")
    _, a2 = lg.create_attempt(env="prod", model="m", effort="high", memory_mode="off",
                              prompt_version="p", toolkit_version="0.1.0", workspace_path="/ws")
    lg.transition(a2, "running")
    lg.transition(a2, "placed")
    lg.insert_bet(bet_id=f"{a2}-B01", attempt_id=a2, ticket_index=1, ticker="T", side="yes",
                  limit_price=D("0.40"), model_prob=D("0.55"), rationale="r", is_real=1,
                  status="settled", fill_price=D("0.40"), outcome="win", pnl=D("1.16"),
                  fee=D("0.04"), stake=D("1.00"),
                  placed_at="2026-07-07T14:00:00Z")
    lg.transition(a2, "settled")
    lg.close()

    res = invoke("status")

    assert res.exit_code == 0
    for heading in ("## Account", "## The day", "## The board", "## Settlements",
                    "## Compute", "## Liveness", "## Invariants"):
        assert heading in res.stdout
    assert "- halt: no" in res.stdout


def test_status_json_still_carries_the_meta_stamps(root):
    """The dict is the machine surface and comes after the page; the keys the rebuild
    retired (needs_retro, last_daily_report, last_audit_date, last_deep_review_date) are
    gone from it."""
    res = invoke("status", "--json")
    assert res.exit_code == 0
    data = json.loads(res.stdout[res.stdout.index("{"):])
    assert data["halted"] is False
    assert "needs_retro" not in data
    assert "last_daily_report" not in data and "last_audit_date" not in data
    assert "last_deep_review_date" not in data


def test_status_reflects_halt(root):
    invoke("halt", "boom")
    res = invoke("status")
    assert "- halt: YES (boom)" in res.stdout


def test_status_no_longer_prints_an_arms_line(root):
    """The arm studies are gone with the cells that replaced them (docs/22 section 2.1),
    so there is no configuration in which this line comes back."""
    assert "arms:" not in invoke("status").stdout


def test_status_shows_meta_stamps_for_the_new_steps(root):
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("last_reconcile_date", "2026-07-06")
    lg.close()
    out = invoke("status", "--json").stdout
    data = json.loads(out[out.index("{"):])
    assert data["last_reconcile_date"] == "2026-07-06"


# --------------------------------------------------------------------------- migrate (L1)
def test_migrate_runs_and_audits(root, monkeypatch):
    rec = Recorder(ret={"backup_path": str(root / "data" / "backups" / "b.db"),
                        "before": {"user_version": 1}, "after": {"user_version": 2},
                        "checks": {"user_version": True}, "ok": True})
    monkeypatch.setattr(cli, "safe_migrate", rec)
    res = invoke("migrate")
    assert res.exit_code == 0
    assert "backup: " in res.stdout and "schema v1 → v2" in res.stdout
    assert rec.calls[0][0][0] == root / "data" / "ledger.db"
    assert _audit_count(root, "migration_applied") == 1


def test_migrate_refuses_while_attempt_lock_is_held(root, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(cli, "safe_migrate", rec)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "attempt.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = invoke("migrate")
        assert res.exit_code == 1
        assert "attempt.lock" in res.stdout
        assert rec.count == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_migrate_refuses_while_tick_lock_is_held(root, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(cli, "safe_migrate", rec)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "tick.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert invoke("migrate").exit_code == 1
        assert rec.count == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_migrate_runs_under_halt(root, monkeypatch):
    """A paused system is exactly when a migration is safe (amendment to L1)."""
    (root / "data" / "HALT").write_text("paused for maintenance\n")
    rec = Recorder(ret={"backup_path": "/tmp/b.db", "before": {"user_version": 1},
                        "after": {"user_version": 2}, "checks": {}, "ok": True})
    monkeypatch.setattr(cli, "safe_migrate", rec)
    assert invoke("migrate").exit_code == 0
    assert rec.count == 1


def test_migrate_real_ledger_on_a_tmp_root(root):
    """The real safe_migrate against a freshly migrated tmp ledger: idempotent, verified."""
    res = invoke("migrate")
    assert res.exit_code == 0
    assert '"ok": true' in res.stdout
    assert len(list((root / "data" / "backups").glob("*.db"))) == 1
    lg = _open_ro(root)
    assert (lg.conn.execute("PRAGMA user_version").fetchone()["user_version"]
            == _SCHEMA_VERSION)
    lg.close()


def test_migrate_failure_exits_nonzero_with_backup_path(root, monkeypatch):
    def boom(*a, **k):
        raise cli.LedgerError("migration verification failed: {...}; backup at /tmp/b.db")

    monkeypatch.setattr(cli, "safe_migrate", boom)
    res = invoke("migrate")
    assert res.exit_code == 1
    assert "migrate FAILED" in res.stdout and "/tmp/b.db" in res.stdout
    assert _audit_count(root, "migration_applied") == 0


# --------------------------------------------------------------------------- reconcile / audit
def test_reconcile_command_prints_and_exits_zero_when_clean(root, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "reconcile_once",
                        Recorder(ret={"ok": True, "drift": "0.0000"}))
    res = invoke("reconcile")
    assert res.exit_code == 0 and '"drift": "0.0000"' in res.stdout


def test_reconcile_command_exits_nonzero_on_drift(root, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "reconcile_once", Recorder(ret={"ok": False, "drift": "-1.0000"}))
    assert invoke("reconcile").exit_code == 1


@pytest.mark.parametrize(
    ("verdict", "code"),
    [("exact", 0), ("absorbed", 0), ("reversed", 0),
     ("noted", 1), ("large", 1), ("provisional", 1)],
)
def test_reconcile_command_exits_zero_only_when_nobody_is_needed(root, monkeypatch,
                                                                 verdict, code):
    """Arno, 2026-09-27: an absorbed night is carried by the walk and needs nobody, so a
    manual run says so with its exit code. The JSON is printed either way."""
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "reconcile_once", Recorder(
        ret={"ok": verdict == "exact", "verdict": verdict, "drift": "0.0101"}))
    res = invoke("reconcile")
    assert res.exit_code == code
    assert f'"verdict": "{verdict}"' in res.stdout


def test_reconcile_command_skipped_paper_era_exits_zero(root, monkeypatch):
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "reconcile_once", Recorder(ret={"skipped": "paper_era"}))
    assert invoke("reconcile").exit_code == 0


def test_reconcile_double_tap_in_one_second_is_friendly(root, monkeypatch):
    """reconciliations.run_at is the PK at second precision — a collision is not an error."""
    import sqlite3

    def collide(*a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: reconciliations.run_at")

    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "reconcile_once", collide)
    res = invoke("reconcile")
    assert res.exit_code == 0
    assert "already recorded this instant" in res.stdout


# --------------------------------------------------------------------------- init / backup
def test_init_creates_dirs_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("BT_ROOT", str(tmp_path))
    res = invoke("init")
    assert res.exit_code == 0
    assert (tmp_path / "data" / "ledger.db").exists()
    for sub in ("attempts", "reports", "logs", "locks", "backups", "status", "board",
                "director"):
        assert (tmp_path / "data" / sub).is_dir()


def test_init_succeeds_offline_without_creds(tmp_path, monkeypatch):
    # no KALSHI creds in env -> genesis snapshot is skipped, init still succeeds
    monkeypatch.setenv("BT_ROOT", str(tmp_path))
    res = invoke("init")
    assert res.exit_code == 0


def test_backup_command(root):
    res = invoke("backup")
    assert res.exit_code == 0
    assert len(list((root / "data" / "backups").glob("*.db"))) == 1
    assert _audit_count(root, "backup_done") == 1


def _fake_exchange(monkeypatch, **kw):
    """Wire a FakeKalshi in as the command's client (the creds boundary)."""
    from betting_agent.kalshi.testing import FakeKalshi

    fake = FakeKalshi(**kw)
    monkeypatch.setattr(cli, "_client", lambda settings: fake)
    return fake


# ----------------------------------------------------------------------- settle
def test_settle_command_runs_a_real_pass_and_prints_its_counts(root, monkeypatch):
    """The whole command, with the real ``settle_once``: a resolved real bet is closed and
    the counts land on stdout as JSON an operator (or a script) can read."""
    fake = _fake_exchange(monkeypatch, balance=D("30.16"))
    coid = "A-0001-B01"
    close = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
    fake.add_market("KXCLI", title="m", close_time=close, yes_ask=D("0.40"), yes_ask_size=10)
    fake.create_order("KXCLI", "yes", D("0.40"), 1, coid)
    lg = Ledger.open(root / "data" / "ledger.db")
    _seq, aid = lg.create_attempt(env="prod", model="m", effort="high", memory_mode="on",
                                  prompt_version="p", toolkit_version="0.1.0",
                                  workspace_path="/ws")
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    lg.insert_bet(bet_id=coid, attempt_id=aid, ticket_index=1, ticker="KXCLI", side="yes",
                  limit_price=D("0.40"), model_prob=D("0.60"), rationale="r", is_real=1,
                  status="filled", contracts=1, fill_price=D("0.40"), stake=D("0.40"),
                  fee=D("0.02"), client_order_id=coid, placed_at="2026-07-07T12:00:00Z")
    lg.close()
    fake.resolve("KXCLI", "yes")

    res = invoke("settle")

    assert res.exit_code == 0
    counts = json.loads(res.stdout)
    assert counts["bets_settled"] == 1 and counts["errors"] == 0
    lg = _open_ro(root)
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    lg.close()
    assert row["status"] == "settled" and row["outcome"] == "win"
    assert cli._lock_free(root / "data" / "locks" / "tick.lock")


def test_settle_command_on_an_empty_ledger_is_a_clean_no_op(root, monkeypatch):
    _fake_exchange(monkeypatch)
    res = invoke("settle")
    assert res.exit_code == 0
    assert json.loads(res.stdout)["bets_settled"] == 0


def test_settle_command_without_credentials_refuses_loudly(root):
    """No creds configured, so ``_client`` cannot be built.

    The manual commands deliberately use ``_client`` and not the tick's tolerant
    ``_maybe_client``: an operator who typed ``settle`` asked for a settlement pass, and
    silently doing nothing would be the wrong answer. What must hold is that the refusal
    is total — nonzero exit, the lock released (so the tick is not wedged), and not one
    row touched. (The failure is presently a raw traceback rather than a message; that is
    a cosmetic wart, not a safety one, and it is pinned here as current behavior.)
    """
    res = invoke("settle")

    assert res.exit_code != 0
    assert cli._lock_free(root / "data" / "locks" / "tick.lock")
    lg = _open_ro(root)
    assert lg.conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"] == 0
    lg.close()


# --------------------------------------------------------------------------- tick: live genesis
def _genesis_client(dollars="30.0500"):
    class _Bal:
        def __init__(self, d):
            self.dollars = d

    class _Client:
        def get_balance(self):
            return _Bal(dollars)

    return _Client()


def test_live_genesis_stamps_once_when_gate_open(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "real_orders_allowed",
                        lambda settings, ledger=None: (True, "live_trading"))
    monkeypatch.setattr(cli, "_client", lambda settings: _genesis_client())
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _fake_popen(monkeypatch)

    invoke("tick")
    invoke("tick")  # second pass must be a no-op

    lg = _open_ro(root)
    ts = lg.meta_get("live_genesis_ts")
    balance = lg.meta_get("live_genesis_balance")
    lg.close()
    assert ts is not None
    assert balance == "30.0500"
    assert _audit_count(root, "live_genesis") == 1


def test_live_genesis_skipped_when_gate_closed(root, monkeypatch):
    _patch_steps(monkeypatch)  # gate closed by the hermetic default
    monkeypatch.setattr(cli, "_client", lambda settings: _genesis_client())
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _fake_popen(monkeypatch)

    invoke("tick")

    lg = _open_ro(root)
    assert lg.meta_get("live_genesis_ts") is None
    lg.close()
    assert _audit_count(root, "live_genesis") == 0


def test_live_genesis_skipped_without_client(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "real_orders_allowed",
                        lambda settings, ledger=None: (True, "live_trading"))
    monkeypatch.setattr(cli, "_client", lambda settings: (_ for _ in ()).throw(RuntimeError))
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _fake_popen(monkeypatch)

    invoke("tick")

    lg = _open_ro(root)
    assert lg.meta_get("live_genesis_ts") is None
    lg.close()


# ------------------------------------------------- tick: reconcile deferral (MP-1, ST-4)
def _meta(root, key):
    lg = _open_ro(root)
    v = lg.meta_get(key)
    lg.close()
    return v


def _deferral_reasons(root):
    lg = _open_ro(root)
    reasons = [json.loads(e["detail"])["reason"]
               for e in lg.audit_events(event="reconcile_deferred")]
    lg.close()
    return reasons


def test_reconcile_defers_when_this_ticks_settle_errored(root, monkeypatch):
    """MP-1 item 1b: a settle pass that swallowed an error may not have written down a
    settlement the exchange already paid. Reconciling on top of that is how the loop
    HALTed itself overnight on 2026-08-02."""
    rec = Recorder(ret={"ok": True})
    _patch_steps(monkeypatch, settle=Recorder(ret={"bets_settled": 0, "errors": 1}),
                 reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    _fake_popen(monkeypatch)

    invoke("tick")

    assert rec.count == 0
    assert _deferral_reasons(root) == ["settle_errored"]
    assert _meta(root, "last_reconcile_date") is None       # the night stays open
    assert json.loads(_meta(root, "reconcile_deferrals")) == {"date": "2026-07-07", "n": 1}


def test_reconcile_defers_while_an_attempt_holds_the_lock(root, monkeypatch):
    """ST-4: a detached attempt may be mid-placement; a balance read that straddles an
    order landing is drift that is not drift."""
    rec = Recorder(ret={"ok": True})
    _patch_steps(monkeypatch, reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    _fake_popen(monkeypatch)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "attempt.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        invoke("tick")
        assert rec.count == 0
        assert _deferral_reasons(root) == ["attempt_in_progress"]
        assert _meta(root, "last_reconcile_date") is None
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_reconcile_runs_anyway_once_the_nights_deferral_bound_is_spent(root, monkeypatch):
    """"Defer" must never become "never": after four deferrals the night reconciles
    regardless, with the provisional gate off, so a real problem HALTs before morning."""
    rec = Recorder(ret={"ok": True})
    _patch_steps(monkeypatch, reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    _fake_popen(monkeypatch)
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "attempt.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        for _ in range(cli._MAX_RECONCILE_DEFERRALS):
            invoke("tick")
        assert rec.count == 0                     # four deferrals, no run
        assert len(_deferral_reasons(root)) == cli._MAX_RECONCILE_DEFERRALS

        invoke("tick")                            # the fifth attempt runs regardless

        assert rec.count == 1
        assert rec.calls[0][1]["allow_provisional"] is False
        assert _meta(root, "last_reconcile_date") == "2026-07-07"
        assert len(_deferral_reasons(root)) == cli._MAX_RECONCILE_DEFERRALS
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_a_provisional_reconcile_defers_without_stamping(root, monkeypatch):
    """D1's attribution gate reports through the same deferral channel."""
    rec = Recorder(ret={"ok": False, "halted": False,
                        "provisional": {"attributed": "1.0000"}})
    _patch_steps(monkeypatch, reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    _fake_popen(monkeypatch)

    invoke("tick")
    invoke("tick")

    assert rec.count == 2                          # unstamped: the same night retries
    assert _deferral_reasons(root) == ["provisional", "provisional"]
    assert _meta(root, "last_reconcile_date") is None


def test_a_halting_reconcile_still_stamps_the_day(root, monkeypatch):
    """The stamping rule is "completed run", not "clean run": a HALT is a completed run."""
    rec = Recorder(ret={"ok": False, "halted": True, "drift": "-1.0000"})
    _patch_steps(monkeypatch, reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    _fake_popen(monkeypatch)

    invoke("tick")

    assert rec.count == 1
    assert _meta(root, "last_reconcile_date") == "2026-07-07"


def test_a_raising_reconcile_leaves_the_day_unstamped(root, monkeypatch):
    """It used to stamp before the failure could be seen, killing the same-night retry."""
    def boom(*a, **k):
        raise RuntimeError("balance endpoint down")

    _patch_steps(monkeypatch, reconcile=boom)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)
    _fake_popen(monkeypatch)

    invoke("tick")

    assert _meta(root, "last_reconcile_date") is None
    assert _audit_count(root, "tick_step_error") == 1


# --------------------------------------------------------------------------- tick: OR-1 HALT
def _halting_settle(root):
    def settle(*a, **k):
        (root / "data" / "HALT").write_text("impostor order detected: A-9999-B01\n")
        return {"impostors": 1}
    return settle


def test_a_halt_set_mid_tick_spawns_no_attempt(root, monkeypatch):
    """OR-1: the settle step's impostor scan halts, and the SAME tick used to go on and
    spawn a fresh attempt session."""
    _patch_steps(monkeypatch, settle=_halting_settle(root))
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)

    assert invoke("tick").exit_code == 0

    assert popen.count == 0
    assert _meta(root, "slot:2026-07-07/10:00") is None   # the slot is not burned either
    assert _audit_count(root, "halt_mid_tick") == 1       # audited once, not per step


# --------------------------------------------------------- subcommand HALT gates (OR-1)
def test_attempt_refuses_under_halt_and_force_overrides(root, monkeypatch):
    rec = Recorder(ret="A-0007")
    monkeypatch.setattr(cli, "run_attempt", rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    (root / "data" / "HALT").write_text("reconcile_drift\n")

    res = invoke("attempt")
    assert res.exit_code == 0 and rec.count == 0
    assert "HALT present" in res.stdout and "--force" in res.stdout

    assert invoke("attempt", "--force").exit_code == 0
    assert rec.count == 1


# ------------------------------------------- the MP-1 reproduction, end to end (2026-08-02)
class _BlindToMarket:
    """The fake with one market it can never read — a persistent 429/network blip."""

    def __init__(self, inner, ticker):
        self._inner, self._ticker = inner, ticker

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def get_market(self, ticker):
        if ticker == self._ticker:
            raise RuntimeError("429 slow down")
        return self._inner.get_market(ticker)


def _late_settlement_world(root, ticker="KXLATE"):
    """A live-era ledger with one real ``filled`` bet whose market the exchange has already
    settled and paid out. Built through the fake's own money trail, so the balance is real."""
    from betting_agent.kalshi.testing import FakeKalshi

    fake = FakeKalshi(balance=D("30.16"))
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("live_genesis_ts", "2026-07-07T12:00:00Z")
    lg.meta_set("live_genesis_balance", str(fake.balance))
    _seq, aid = lg.create_attempt(env="prod", model="m", effort="high", memory_mode="on",
                                  prompt_version="p", toolkit_version="0.1.0",
                                  workspace_path="/ws")
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    fake.add_market(ticker, title=ticker, close_time=datetime(2026, 8, 1, 12, tzinfo=UTC),
                    yes_ask=D("0.40"), yes_ask_size=50)
    coid = f"{aid}-B01"
    r = fake.create_order(ticker, "yes", D("0.40"), 1, coid)
    lg.insert_bet(bet_id=coid, attempt_id=aid, ticket_index=1, ticker=ticker, side="yes",
                  limit_price=D("0.40"), model_prob=D("0.60"), rationale="r", is_real=1,
                  status="filled", contracts=1, fill_price=r.avg_fill_price,
                  stake=D("0.40"), fee=D(r.fee), order_id=r.order_id, client_order_id=coid,
                  placed_at="2026-07-07T13:00:00Z")
    fake.resolve(ticker, "yes", ts=datetime(2026, 7, 7, 22, 0, tzinfo=UTC))
    lg.close()
    return fake, aid, coid


def _tick_with(root, client, monkeypatch, now):
    """One real tick — settle and reconcile are the production functions; only the steps
    that would spawn or buy a session are stubbed out."""
    monkeypatch.setattr(cli, "_board_if_due", Recorder(ret={"status": "fresh"}))
    monkeypatch.setattr(cli, "_digest_if_due", Recorder())
    monkeypatch.setattr(cli, "real_orders_allowed",
                        lambda settings, ledger=None: (False, "test"))
    lg = Ledger.open(root / "data" / "ledger.db")
    try:
        cli._tick_run(lg, client, cli._settings(), now)
    finally:
        lg.close()


def test_mp1_a_settle_blip_defers_and_the_next_tick_reconciles_clean(root, monkeypatch):
    """The named reproduction (MP-1, live 2026-08-02). A market finalizes and pays out
    while settle cannot reach it; the 23:00 reconciliation then sees a credit with no
    matching row. That HALTed the loop overnight. It must now defer, and the next tick —
    with the exchange reachable again — must settle and reconcile to the cent."""
    fake, _aid, coid = _late_settlement_world(root)
    _fake_popen(monkeypatch)

    _tick_with(root, _BlindToMarket(fake, "KXLATE"), monkeypatch, LATE_NOW)

    lg = _open_ro(root)
    assert lg.conn.execute(
        "SELECT status FROM bets WHERE bet_id=?", (coid,)).fetchone()["status"] == "filled"
    assert lg.latest_reconciliation() is None            # the run never concluded
    lg.close()
    assert not (root / "data" / "HALT").exists()
    assert _deferral_reasons(root) == ["settle_errored"]
    assert _meta(root, "last_reconcile_date") is None    # the same night will retry

    _tick_with(root, fake, monkeypatch, LATE_NOW)        # the exchange is back

    lg = _open_ro(root)
    bet = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (coid,)).fetchone()
    row = lg.latest_reconciliation()
    lg.close()
    assert bet["status"] == "settled" and bet["outcome"] == "win"
    assert row["ok"] == 1 and row["drift"] == "0.0000"
    assert _meta(root, "last_reconcile_date") == "2026-07-07"
    assert not (root / "data" / "HALT").exists()
    assert _deferral_reasons(root) == ["settle_errored"]  # exactly one deferral, then done


def test_mp1_a_payout_that_landed_after_the_settle_pass_defers_provisionally(
        root, monkeypatch):
    """The other arm of D1: settle is perfectly healthy, the payout simply landed after
    the settle pass and before the balance read — no error anywhere for rule 1b to see.
    The attribution gate is what catches it, and the next tick closes it."""
    fake, _aid, coid = _late_settlement_world(root)
    _fake_popen(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    out = cli.reconcile_once(lg, fake, cli._settings(), now=LATE_NOW)
    lg.close()

    assert out["provisional"]["attributed"] == "1.0000"
    assert [p["bet_id"] for p in out["provisional"]["pending"]] == [coid]
    assert not (root / "data" / "HALT").exists()

    _tick_with(root, fake, monkeypatch, LATE_NOW)

    lg = _open_ro(root)
    assert lg.latest_reconciliation()["drift"] == "0.0000"
    lg.close()
    assert _meta(root, "last_reconcile_date") == "2026-07-07"


# --------------------------------------------------------------------------- CI-1 startup warning
def _reset_config_warning(monkeypatch):
    """The stderr warning is once-per-process; tests share a process."""
    monkeypatch.setattr(cli, "_CONFIG_WARNED", False)


def test_unknown_config_key_warns_on_stderr_and_the_command_still_runs(root, monkeypatch):
    """D5: flag, don't stop. The typo is named, and the command it would have blocked runs."""
    _reset_config_warning(monkeypatch)
    (root / "config.toml").write_text('[limits]\nmax_bets_per_attemp = 3\n')
    res = runner.invoke(cli.app, ["status"], env={"BT_ROOT": str(root)})

    assert res.exit_code == 0
    assert "unknown config keys ignored" in res.stderr
    assert "limits.max_bets_per_attemp" in res.stderr
    assert res.stdout.strip()                      # the command produced its normal output


def test_unknown_config_section_and_env_var_are_both_named(root, monkeypatch):
    _reset_config_warning(monkeypatch)
    (root / "config.toml").write_text('[stakez]\nlive_trading = true\n')
    monkeypatch.setenv("BETTING_AGENT_ATTEMPT__MAX_TURNZ", "9")

    res = runner.invoke(cli.app, ["status"])

    assert res.exit_code == 0
    assert "stakez" in res.stderr
    assert "attempt.max_turnz" in res.stderr


def test_unknown_config_keys_audit_once_per_et_day(root, monkeypatch):
    """The tick fires 96×/day; an audit row per tick would bury the signal it raises."""
    _reset_config_warning(monkeypatch)
    (root / "config.toml").write_text('[limits]\nmax_bets_per_attemp = 3\n')

    for _ in range(3):
        assert runner.invoke(cli.app, ["status"]).exit_code == 0

    assert _audit_count(root, "config_unknown_keys") == 1
    lg = _open_ro(root)
    detail = json.loads(lg.audit_events(event="config_unknown_keys")[0]["detail"])
    lg.close()
    assert detail["keys"] == ["limits.max_bets_per_attemp"]


def test_a_clean_config_warns_about_nothing(root, monkeypatch):
    _reset_config_warning(monkeypatch)
    res = runner.invoke(cli.app, ["status"])
    assert res.exit_code == 0
    assert "unknown config keys" not in res.stderr
    assert _audit_count(root, "config_unknown_keys") == 0


def test_config_sweep_never_breaks_a_command(root, monkeypatch):
    """A diagnostic must not take down the command it is diagnosing."""
    _reset_config_warning(monkeypatch)

    def boom(settings, env=None):
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(cli, "unknown_keys", boom)
    assert runner.invoke(cli.app, ["status"]).exit_code == 0


# --------------------------------------------------------------------------- ST-7 reaper
def _running_attempt(root, created_at: str) -> str:
    lg = Ledger.open(root / "data" / "ledger.db")
    _seq, aid = lg.create_attempt(env="prod", model="m", effort="high", memory_mode="on",
                                  prompt_version="p", toolkit_version="0.1.0",
                                  workspace_path="/ws")
    lg.transition(aid, "running")
    lg.conn.execute("UPDATE attempts SET created_at=? WHERE attempt_id=?", (created_at, aid))
    lg.close()
    return aid


def _status_of(root, aid):
    lg = _open_ro(root)
    row = lg.get_attempt(aid)
    lg.close()
    return row


def test_reaper_fails_a_stale_running_attempt_and_leaves_a_fresh_one(root, monkeypatch):
    """ST-7: SIGKILL and a sleeping host are the two crashes the in-process guard cannot
    see, and nothing anywhere reaped what they left behind — the rows sat in 'running'
    forever, missing from settlement and from every population keyed on a terminal
    status."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _fake_popen(monkeypatch)
    stale = _running_attempt(root, "2026-07-01T00:00:00Z")     # six days old
    fresh = _running_attempt(root, cli.iso(EARLY_NOW))          # started this instant

    assert invoke("tick").exit_code == 0

    assert _status_of(root, stale)["status"] == "failed"
    assert _status_of(root, stale)["error"] == "stale_running_reaped"
    assert _status_of(root, fresh)["status"] == "running"       # untouched
    lg = _open_ro(root)
    events = lg.audit_events(event="stale_running_reaped")
    lg.close()
    assert len(events) == 1 and events[0]["attempt_id"] == stale


def test_reaper_leaves_a_long_but_legitimate_attempt_alone(root, monkeypatch):
    """The bound is derived from the settings an attempt actually runs under, and it has
    to clear the whole configured wall budget with room to spare, or the reaper eats live
    attempts."""
    settings = cli._settings()
    bound = cli._stale_running_after(settings)
    longest = timedelta(minutes=settings.attempt.wall_time_min)
    assert bound > longest

    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _fake_popen(monkeypatch)
    aid = _running_attempt(root, cli.iso(EARLY_NOW - longest))

    invoke("tick")

    assert _status_of(root, aid)["status"] == "running"


def test_reaper_bound_tracks_the_configured_wall_budget(root):
    """Raise the budget and the bound moves with it; hardcoding it is how a reaper
    quietly starts eating live attempts."""
    settings = cli._settings()
    before = cli._stale_running_after(settings)
    settings.attempt.wall_time_min *= 2
    assert cli._stale_running_after(settings) > before


def test_reaper_leaves_an_unreadable_timestamp_for_a_human(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    _fake_popen(monkeypatch)
    aid = _running_attempt(root, "not-a-timestamp")

    invoke("tick")

    assert _status_of(root, aid)["status"] == "running"
    assert _audit_count(root, "stale_running_reaped") == 0


# ------------------------------------------------------- ST-8 manual commands take tick.lock
@pytest.mark.parametrize("command", ["settle", "reconcile"])
def test_manual_step_commands_refuse_while_a_tick_holds_the_lock(root, monkeypatch, command):
    """ST-8: these are the tick's own steps. The loser of a settle race tracebacked out
    mid-pass, and what it skipped on the way out was the impostor scan."""
    rec = Recorder(ret={"ok": True})
    monkeypatch.setattr(cli, "settle_once", rec)
    monkeypatch.setattr(cli, "reconcile_once", rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "tick.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = invoke(command)
        assert res.exit_code == 1
        assert "tick.lock busy" in res.stdout
        assert rec.count == 0                 # the pass did not half-run
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_two_concurrent_settle_passes_leave_one_winner_and_one_polite_exit(root, monkeypatch):
    """The named acceptance case: the winner completes a whole pass, the loser says so
    and does nothing — instead of both walking the same bets."""
    passes = []

    def settle(ledger, client, settings, now=None, *, full_scan=False):
        # Re-entering the command while this one holds the lock is the concurrency.
        inner = invoke("settle")
        passes.append(("inner", inner.exit_code, inner.stdout))
        return {"bets_settled": 1}

    monkeypatch.setattr(cli, "settle_once", settle)
    monkeypatch.setattr(cli, "_client", lambda settings: object())

    outer = invoke("settle")

    assert outer.exit_code == 0 and '"bets_settled": 1' in outer.stdout
    assert len(passes) == 1
    _kind, code, out = passes[0]
    assert code == 1 and "tick.lock busy" in out


def test_settle_full_asks_for_the_whole_shared_account_history(root, monkeypatch):
    """docs/22 section 7.7: the go-live sequence runs this before ``deposit``, so that the
    outside orders are in ``personal_orders`` and the balance walk can explain them,
    instead of waiting up to a week for the tick's own full scan."""
    rec = Recorder(ret={"bets_settled": 0})
    monkeypatch.setattr(cli, "settle_once", rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())

    assert invoke("settle").exit_code == 0
    assert rec.calls[0][1]["full_scan"] is False        # the ordinary incremental pass

    assert invoke("settle", "--full").exit_code == 0
    assert rec.calls[1][1]["full_scan"] is True


def test_manual_step_commands_release_the_lock_when_they_finish(root, monkeypatch):
    monkeypatch.setattr(cli, "settle_once", Recorder(ret={"bets_settled": 0}))
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    assert invoke("settle").exit_code == 0
    assert cli._lock_free(root / "data" / "locks" / "tick.lock")


def test_manual_step_commands_release_the_lock_when_they_raise(root, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("exchange on fire")

    monkeypatch.setattr(cli, "settle_once", boom)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    assert invoke("settle").exit_code != 0
    assert cli._lock_free(root / "data" / "locks" / "tick.lock")


# --------------------------------------------------------- OR-2/ST-5 slot lifecycle
def _slot_marker(root, key="slot:2026-07-07/10:00"):
    return _meta(root, key)


def test_a_consumed_slot_records_when_it_was_consumed(root, monkeypatch):
    """The flat 'pending' marker carried no clock, so a slot lost to a failed spawn was
    indistinguishable from one running right now — and stayed lost."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    _fake_popen(monkeypatch)
    invoke("tick")
    assert _slot_marker(root).startswith("pending:")
    assert cli.parse_iso(_slot_marker(root).split("pending:")[1]) == SLOT_NOW


def test_a_dead_spawn_is_re_offered_once_the_grace_passes(root, monkeypatch):
    """OR-2/ST-5: a Popen that raised, a child that lost the lock race, a child that
    refused under HALT — all identical from here, and all used to burn the slot for the
    day. The re-offer is what gets the day's one attempt back."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("slot:2026-07-07/10:00", f"pending:{cli.iso(SLOT_NOW - timedelta(minutes=5))}")
    lg.close()

    invoke("tick")

    assert popen.count == 1
    assert " ".join(popen.calls[0][0][1:]).startswith(
        "-m betting_agent.cli attempt --slot slot:2026-07-07/10:00 --cell"
    )
    assert _slot_marker(root) == f"pending:{cli.iso(SLOT_NOW)}"   # re-consumed, re-clocked
    assert _audit_count(root, "slot_respawned") == 1


def test_a_spawn_still_inside_its_grace_is_not_re_offered(root, monkeypatch):
    """The whole point of the spawn grace: a child that is merely starting up is not a
    child that died."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    marker = f"pending:{cli.iso(SLOT_NOW - timedelta(minutes=1))}"
    lg.meta_set("slot:2026-07-07/10:00", marker)
    lg.close()

    invoke("tick")

    assert popen.count == 0
    assert _slot_marker(root) == marker


def test_a_live_attempt_is_never_double_spawned(root, monkeypatch):
    """The other half of the acceptance case: the marker is stale AND the window is open,
    but an attempt holds the lock — so there is nothing to recover."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("slot:2026-07-07/10:00", f"pending:{cli.iso(SLOT_NOW - timedelta(hours=1))}")
    lg.close()
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "attempt.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        invoke("tick")
        assert popen.count == 0
        assert _audit_count(root, "slot_respawned") == 0
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_a_running_marker_is_never_re_offered(root, monkeypatch):
    """A child that took attempt.lock stamps the slot; that stamp is the CAS that makes
    the re-offer impossible even in the instant the lock looks free."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    popen = _fake_popen(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("slot:2026-07-07/10:00", "running:A-0001")
    lg.close()

    invoke("tick")

    assert popen.count == 0
    assert _slot_marker(root) == "running:A-0001"


def test_a_slot_that_never_ran_is_audited_lost_once_its_window_closes(root, monkeypatch):
    """The loss used to be invisible: the slot read 'pending' forever and nothing said a
    scheduled attempt had not happened."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)   # 13:00 ET, past 10:00 + 2 h
    _fake_popen(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("slot:2026-07-07/10:00", f"pending:{cli.iso(SLOT_NOW)}")
    lg.close()

    invoke("tick")
    invoke("tick")   # the CAS makes it a one-time event, not a per-tick drumbeat

    assert _audit_count(root, "slot_spawn_lost") == 1
    assert _slot_marker(root) == "lost"
    assert _audit_count(root, "slot_skipped") == 0     # it was consumed, not skipped


def test_a_slot_lost_to_a_halt_window_is_audited_like_any_other_loss(root, monkeypatch):
    """The fold-in case: the child started, saw the HALT, and politely refused. The slot
    keeps its pending marker (so it is re-offerable while the window is open), and once
    the window closes it is recorded as the loss it is."""
    _patch_steps(monkeypatch)
    _fake_popen(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    monkeypatch.setattr(cli, "run_attempt", Recorder(ret="A-0001"))
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    invoke("tick")                                       # consumes the slot, spawns
    assert _slot_marker(root).startswith("pending:")

    (root / "data" / "HALT").write_text("impostor order detected\n")
    res = invoke("attempt", "--slot", "slot:2026-07-07/10:00")   # the child, refusing
    assert res.exit_code == 0 and "HALT present" in res.stdout
    assert _slot_marker(root).startswith("pending:")     # untouched by the refusal

    (root / "data" / "HALT").unlink()
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)
    invoke("tick")

    assert _audit_count(root, "slot_spawn_lost") == 1
    assert _slot_marker(root) == "lost"


def test_a_halted_child_leaves_a_slot_the_next_tick_can_still_use(root, monkeypatch):
    """And while the window is still open, the HALT-refused slot is simply re-offered."""
    _patch_steps(monkeypatch)
    popen = _fake_popen(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("slot:2026-07-07/10:00", f"pending:{cli.iso(SLOT_NOW - timedelta(minutes=10))}")
    lg.close()

    invoke("tick")

    assert popen.count == 1
    assert _audit_count(root, "slot_respawned") == 1


def test_an_unconsumed_slot_past_its_window_is_still_just_skipped(root, monkeypatch):
    """Back-compat: 'skipped' still means nobody ever tried, and is distinct from 'lost'."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)
    _fake_popen(monkeypatch)
    invoke("tick")
    assert _slot_marker(root) == "skipped"
    assert _audit_count(root, "slot_skipped") == 1
    assert _audit_count(root, "slot_spawn_lost") == 0


def test_a_child_that_gets_the_lock_stamps_its_slot_with_the_attempt_id(root, monkeypatch):
    """The stamp is what tells a later tick the slot was actually used."""
    monkeypatch.setattr(cli, "run_attempt", Recorder(ret="A-0042"))
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("slot:2026-07-07/10:00", f"pending:{cli.iso(SLOT_NOW)}")
    lg.close()

    assert invoke("attempt", "--slot", "slot:2026-07-07/10:00").exit_code == 0

    assert _slot_marker(root) == "running:A-0042"


def test_a_child_that_loses_the_lock_race_leaves_the_slot_pending(root, monkeypatch):
    """A manual attempt beat the scheduled one to the lock: the scheduled slot is not
    consumed by the loser, so it is still there to be re-offered."""
    rec = Recorder(ret="A-0007")
    monkeypatch.setattr(cli, "run_attempt", rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    marker = f"pending:{cli.iso(SLOT_NOW)}"
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("slot:2026-07-07/10:00", marker)
    lg.close()
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "attempt.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert invoke("attempt", "--slot", "slot:2026-07-07/10:00").exit_code == 1
        assert rec.count == 0
        assert _slot_marker(root) == marker
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


def test_a_manual_attempt_without_a_slot_writes_no_marker(root, monkeypatch):
    monkeypatch.setattr(cli, "run_attempt", Recorder(ret="A-0007"))
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    assert invoke("attempt").exit_code == 0
    assert _slot_marker(root) is None


# ------------------------------------------------------------- AE-2 in the child wrapper
def test_a_crashing_attempt_command_marks_the_attempt_failed_and_exits_nonzero(
        root, monkeypatch):
    """AE-2's backstop: run_attempt transitions its own attempt, but if THAT bookkeeping
    also fails the child must not exit leaving a row in 'running'. Under attempt.lock no
    other process can legitimately have one, so anything still there is this crash's."""
    def crash(ledger, client, settings, **kwargs):
        _seq, aid = ledger.create_attempt(
            env="prod", model="m", effort="high", memory_mode="on", prompt_version="p",
            toolkit_version="0.1.0", workspace_path="/ws",
        )
        ledger.transition(aid, "running")
        raise RuntimeError("harness died before it could tidy up")

    monkeypatch.setattr(cli, "run_attempt", crash)
    monkeypatch.setattr(cli, "_client", lambda settings: object())

    res = invoke("attempt")

    assert res.exit_code != 0
    row = _status_of(root, "A-0001")
    assert row["status"] == "failed"
    assert "RuntimeError: harness died" in row["error"]
    assert _audit_count(root, "attempt_crashed") == 1
    assert cli._lock_free(root / "data" / "locks" / "attempt.lock")


def test_a_crash_that_run_attempt_already_handled_is_not_transitioned_twice(root, monkeypatch):
    def crash(ledger, client, settings, **kwargs):
        _seq, aid = ledger.create_attempt(
            env="prod", model="m", effort="high", memory_mode="on", prompt_version="p",
            toolkit_version="0.1.0", workspace_path="/ws",
        )
        ledger.transition(aid, "running")
        ledger.transition(aid, "failed")      # run_attempt's own wrapper did its job
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "run_attempt", crash)
    monkeypatch.setattr(cli, "_client", lambda settings: object())

    assert invoke("attempt").exit_code != 0

    assert _status_of(root, "A-0001")["status"] == "failed"
    assert _audit_count(root, "attempt_crashed") == 0     # nothing left to sweep


# --------------------------------------------------------------- OR-3/ST-9 migrate holds locks
def test_migrate_holds_both_locks_for_the_whole_migration(root, monkeypatch):
    """OR-3/ST-9: probing the locks and letting go proves only that the system was idle a
    moment ago — a 15-minute tick could and would begin mid-DDL."""
    seen = {}

    def check_locks(db_path, backups_dir):
        seen["attempt"] = cli._lock_free(root / "data" / "locks" / "attempt.lock")
        seen["tick"] = cli._lock_free(root / "data" / "locks" / "tick.lock")
        return {"backup_path": "/tmp/b.db", "before": {"user_version": 1},
                "after": {"user_version": 2}, "checks": {}, "ok": True}

    monkeypatch.setattr(cli, "safe_migrate", check_locks)

    assert invoke("migrate").exit_code == 0

    assert seen == {"attempt": False, "tick": False}    # both HELD during the migration
    assert cli._lock_free(root / "data" / "locks" / "attempt.lock")   # both released after
    assert cli._lock_free(root / "data" / "locks" / "tick.lock")


def test_migrate_releases_its_locks_when_the_migration_fails(root, monkeypatch):
    def boom(*a, **k):
        raise cli.LedgerError("migration verification failed; backup at /tmp/b.db")

    monkeypatch.setattr(cli, "safe_migrate", boom)
    assert invoke("migrate").exit_code == 1
    assert cli._lock_free(root / "data" / "locks" / "attempt.lock")
    assert cli._lock_free(root / "data" / "locks" / "tick.lock")


def test_migrate_does_not_keep_the_attempt_lock_when_the_tick_lock_is_busy(root, monkeypatch):
    """The half-acquired case: refusing on the second lock must not strand the first."""
    monkeypatch.setattr(cli, "safe_migrate", Recorder())
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    held = open(root / "data" / "locks" / "tick.lock", "a")
    fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert invoke("migrate").exit_code == 1
        assert cli._lock_free(root / "data" / "locks" / "attempt.lock")
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()


@pytest.mark.parametrize("legacy,now", [("pending", SLOT_NOW), ("pending", GRACE_NOW),
                                        ("skipped", GRACE_NOW)])
def test_a_pre_wp3_flat_slot_marker_is_inert(root, monkeypatch, legacy, now):
    """The live ledger holds clockless 'pending'/'skipped' values from before the marker
    carried a timestamp. They must neither be re-offered nor audited as losses."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: now)
    popen = _fake_popen(monkeypatch)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("slot:2026-07-07/10:00", legacy)
    lg.close()

    invoke("tick")

    assert popen.count == 0
    assert _slot_marker(root) == legacy
    assert _audit_count(root, "slot_spawn_lost") == 0
    assert _audit_count(root, "slot_respawned") == 0


# ------------------------------------------------------ WP4: weekly full account scan
def _settle_recorder():
    """A settle stand-in that records the ``full_scan`` flag it was asked for."""

    class _R(Recorder):
        @property
        def full_flags(self):
            return [k.get("full_scan") for _a, k in self.calls]

    return _R(ret={"errors": 0})


def test_the_first_tick_asks_for_a_full_account_scan_and_stamps_the_day(root, monkeypatch):
    settle = _settle_recorder()
    _patch_steps(monkeypatch, settle=settle)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)

    assert invoke("tick").exit_code == 0

    assert settle.full_flags == [True]
    lg = _open_ro(root)
    assert lg.meta_get("last_full_scan_date") == "2026-07-07"
    lg.close()


def test_the_next_tick_the_same_week_scans_incrementally(root, monkeypatch):
    settle = _settle_recorder()
    _patch_steps(monkeypatch, settle=settle)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("last_full_scan_date", "2026-07-05")  # two days ago
    lg.close()

    assert invoke("tick").exit_code == 0

    assert settle.full_flags == [False]


def test_a_week_later_the_full_scan_comes_round_again(root, monkeypatch):
    settle = _settle_recorder()
    _patch_steps(monkeypatch, settle=settle)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("last_full_scan_date", "2026-06-30")  # seven days ago
    lg.close()

    assert invoke("tick").exit_code == 0

    assert settle.full_flags == [True]


def test_a_settle_step_that_raised_does_not_stamp_the_full_scan(root, monkeypatch):
    """A pass that blew up did not finish its scan; next tick must try the full one again."""
    def boom(*a, **k):
        raise RuntimeError("settle exploded")

    _patch_steps(monkeypatch, settle=boom)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)

    assert invoke("tick").exit_code == 0

    lg = _open_ro(root)
    assert lg.meta_get("last_full_scan_date") is None
    lg.close()


# ------------------------------------------------------------- WP4: retention (ST-11/D4)
def _aged(path: Path, days: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("x" * 4096)
    old = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (old, old))
    return path


def _gc(root, now=None):
    lg = Ledger.open(root / "data" / "ledger.db")
    try:
        return cli._gc_if_due(lg, cli._settings(), now or datetime.now(UTC))
    finally:
        lg.close()


def test_gc_compresses_an_old_session_stream_and_keeps_its_content(root, monkeypatch):
    monkeypatch.setenv("BT_ROOT", str(root))
    stream = _aged(root / "data" / "attempts" / "A-0001" / "logs" / "session.stream.jsonl", 30)
    stream.write_text('{"type":"result"}\n')
    _aged(stream, 30)

    out = _gc(root)

    assert out["compressed"] == 1
    assert not stream.exists()
    with gzip.open(stream.with_name(stream.name + ".gz"), "rt") as fh:
        assert fh.read() == '{"type":"result"}\n'


def test_gc_leaves_a_recent_stream_alone(root, monkeypatch):
    monkeypatch.setenv("BT_ROOT", str(root))
    stream = _aged(root / "data" / "attempts" / "A-0001" / "logs" / "session.stream.jsonl", 2)

    assert _gc(root)["compressed"] == 0
    assert stream.exists()


def test_gc_compresses_spawn_logs_only_past_ninety_days(root, monkeypatch):
    monkeypatch.setenv("BT_ROOT", str(root))
    old = _aged(root / "data" / "logs" / "attempt-spawn-slot-2026-01-01-10-00.log", 120)
    recent = _aged(root / "data" / "logs" / "deep-review-spawn.log", 30)

    assert _gc(root)["compressed"] == 1
    assert not old.exists() and old.with_name(old.name + ".gz").exists()
    assert recent.exists()


def test_gc_never_touches_anything_outside_its_allowlist(root, monkeypatch):
    """The allowlist is the whole safety argument: name, directory, and age, all three."""
    monkeypatch.setenv("BT_ROOT", str(root))
    untouchable = [
        _aged(root / "data" / "backups" / "ledger-2026-01-01.db", 400),
        _aged(root / "data" / "reports" / "audit-2026-01-01.md", 400),
        _aged(root / "data" / "ledger.db", 400),
        _aged(root / "data" / "HALT", 400),
        _aged(root / "data" / "logs" / "cron.log", 400),
        _aged(root / "data" / "attempts" / "A-0001" / "ticket" / "bets.json", 400),
        _aged(root / "data" / "attempts" / "A-0001" / "logs" / "session.err", 400),
    ]

    assert _gc(root)["compressed"] == 0
    for path in untouchable:
        assert path.exists(), path
        assert not path.with_name(path.name + ".gz").exists(), path


def test_gc_is_idempotent_and_skips_what_it_already_compressed(root, monkeypatch):
    monkeypatch.setenv("BT_ROOT", str(root))
    _aged(root / "data" / "attempts" / "A-0001" / "logs" / "session.stream.jsonl", 30)

    assert _gc(root)["compressed"] == 1
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("last_gc_date", "1999-01-01")  # force it due again
    lg.close()
    assert _gc(root)["compressed"] == 0  # the .gz is left exactly as it is


def test_gc_runs_monthly_not_every_tick(root, monkeypatch):
    monkeypatch.setenv("BT_ROOT", str(root))
    _aged(root / "data" / "attempts" / "A-0001" / "logs" / "session.stream.jsonl", 30)
    now = datetime.now(UTC)
    assert _gc(root, now)["compressed"] == 1
    _aged(root / "data" / "attempts" / "A-0002" / "logs" / "session.stream.jsonl", 30)

    assert _gc(root, now + timedelta(days=29)) is None       # not due
    assert _gc(root, now + timedelta(days=31))["compressed"] == 1


# ------------------------------------------------------------------ WP4: locks and flags
@pytest.mark.parametrize("argv,expected", [(["reconcile"], False), (["reconcile", "--full"], True)])
def test_reconcile_full_flag_reaches_reconcile_once(root, monkeypatch, argv, expected):
    rec = Recorder(ret={"ok": True})
    monkeypatch.setattr(cli, "reconcile_once", rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())

    invoke(*argv)

    assert rec.calls[0][1]["full"] is expected


def test_the_ticks_own_reconcile_never_asks_for_a_full_re_verification(root, monkeypatch):
    """The watermark exists to keep the nightly run cheap; only an operator overrides it."""
    rec = Recorder(ret={"ok": True, "drift": "0.0000"})
    _patch_steps(monkeypatch, reconcile=rec)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "utc_now", lambda: LATE_NOW)

    assert invoke("tick").exit_code == 0

    assert rec.calls[0][1].get("full") in (None, False)


# ----------------------------------------------------- D1 alerting (docs/14, docs/12 §8.1)
class _WedgedExchange:
    """Every endpoint 403s — docs/12 §1's WAF block, which nothing escalated for 3 days."""

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name):
        def _blocked(*a, **k):
            from betting_agent.kalshi.client import KalshiAPIError

            self.calls += 1
            raise KalshiAPIError(403, f"GET {name} blocked")

        return _blocked


def _wedge_the_transport(root, monkeypatch, *, now=EARLY_NOW):
    """A tick whose ONLY failing step is settle, against a 403ing exchange.

    ``genesis_ts`` is what makes the shared-account scan actually page orders; without it
    the scan short-circuits and a wedged transport is never even reached.
    """
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("genesis_ts", "2026-07-01T00:00:00Z")
    lg.close()
    wedged = _WedgedExchange()
    monkeypatch.setattr(cli, "_maybe_client", lambda settings: wedged)
    monkeypatch.setattr(cli, "_client", lambda settings: wedged)
    # The board step would spawn a detached child; stubbed so "the ONLY failing step"
    # above stays true and the streak under test is settle's alone.
    monkeypatch.setattr(cli, "_board_if_due",
                        Recorder(ret={"status": "fresh", "age_seconds": 0}))
    # Same reason for the digest: it reads a live balance through the same transport, and
    # one extra 403 there would be counted against the settle streak under test.
    monkeypatch.setattr(cli, "_digest_if_due", Recorder())
    # And the director, which would spawn a child of its own from midnight on.
    monkeypatch.setattr(cli, "_director_if_due", Recorder(ret={"status": "not_due"}))
    monkeypatch.setattr(cli, "utc_now", lambda: now)
    _fake_popen(monkeypatch)
    return wedged


def _alert_details(root):
    lg = _open_ro(root)
    rows = [json.loads(r["detail"]) for r in lg.audit_events(event="alert_raised")]
    lg.close()
    return rows


def _streak(root, name):
    lg = _open_ro(root)
    raw = lg.meta_get(f"alert_streak:{name}")
    lg.close()
    return json.loads(raw) if raw else None


def test_a_wedged_settle_notifies_once_per_streak(root, monkeypatch, notify_calls):
    """docs/12 §8.1: the 403 wrote one audit row per tick and reached nobody. Now the
    third consecutive failure escalates — once, not ninety-six times a day."""
    wedged = _wedge_the_transport(root, monkeypatch)

    for _ in range(6):
        assert invoke("tick").exit_code == 0

    assert wedged.calls == 6                      # every tick really did try the exchange
    assert _audit_count(root, "tick_step_error") == 6   # the per-tick record is unchanged
    assert len(notify_calls) == 1                 # …and exactly one human-facing banner
    assert "step 'settle' has failed 3 consecutive ticks" in notify_calls[0]
    (alert,) = _alert_details(root)
    assert alert["key"] == "step:settle"
    assert alert["streak"] == 3 and alert["threshold"] == 3
    assert alert["notified"] is True


def test_a_partially_failing_settle_neither_feeds_nor_resets_the_streak(root, monkeypatch,
                                                                        notify_calls):
    """Conformance OPEN-2: in-band partial failure (MP-2's {"errors": n}) is NEUTRAL.

    It must not count toward the settle_unreachable HALT (a delisted market is not an
    unreachable exchange) — and it must not re-arm a streak a real wedge is building
    either, or a wedge that surfaces in-band holds the streak at zero forever."""
    _wedge_the_transport(root, monkeypatch)
    for _ in range(2):
        invoke("tick")
    assert _streak(root, "step:settle")["n"] == 2

    monkeypatch.setattr(cli, "settle_once", Recorder(ret={"errors": 2}))
    invoke("tick")
    assert _streak(root, "step:settle")["n"] == 2      # neutral: no advance, no reset
    assert len(notify_calls) == 0                      # and no threshold crossing

    monkeypatch.setattr(cli, "settle_once", Recorder(ret={"errors": 0}))
    invoke("tick")
    assert _streak(root, "step:settle") == {"n": 0, "notified": False}  # clean pass re-arms


def test_the_streak_re_arms_after_the_step_recovers(root, monkeypatch, notify_calls):
    _wedge_the_transport(root, monkeypatch)
    for _ in range(3):
        invoke("tick")
    assert len(notify_calls) == 1

    monkeypatch.setattr(cli, "settle_once", Recorder(ret={"errors": 0}))   # the network is back
    invoke("tick")
    assert _streak(root, "step:settle") == {"n": 0, "notified": False}

    def _blocked_again(*a, **k):
        raise OSError("the network is down again")

    monkeypatch.setattr(cli, "settle_once", _blocked_again)
    for _ in range(3):
        invoke("tick")
    assert len(notify_calls) == 2      # a second outage is a second notification


def test_a_notifier_that_fails_never_costs_the_tick(root, monkeypatch):
    """'Failure to notify is itself non-fatal' (docs/14 D1) — proven at the tick level."""
    _wedge_the_transport(root, monkeypatch)
    from betting_agent.harness import notify as notify_mod

    def _boom(script):
        raise RuntimeError("notification centre is on fire")

    monkeypatch.setattr(notify_mod, "_osascript", _boom)

    for _ in range(4):
        assert invoke("tick").exit_code == 0

    assert _audit_count(root, "tick_step_error") == 4
    # The alert row is still written: the ledger half of the escalation does not depend
    # on the desktop half.
    assert [a["notified"] for a in _alert_details(root)] == [False]


def test_a_healthy_step_leaves_no_streak_behind(root, monkeypatch, notify_calls):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: EARLY_NOW)
    invoke("tick")
    assert _streak(root, "step:settle") is None
    assert notify_calls == []


# ----------------------------------------------------- D1(d): the settle HALT hard line
def test_twenty_consecutive_settle_failures_halt_settle_unreachable(root, monkeypatch):
    """~5 hours of not reaching the exchange means the money picture is stale; placement
    stops rather than betting on it (docs/14 D1(d))."""
    _wedge_the_transport(root, monkeypatch)

    for i in range(19):
        invoke("tick")
        assert not (root / "data" / "HALT").exists(), f"halted early at tick {i + 1}"
    invoke("tick")                                            # the 20th

    assert cli.halt_reason(cli._settings()) == "settle_unreachable"
    lg = _open_ro(root)
    halts = [json.loads(r["detail"]) for r in lg.audit_events(event="halt_set")]
    lg.close()
    assert halts == [{"reason": "settle_unreachable", "consecutive_settle_failures": 20}]
    # The streak re-arms as the HALT is written: a resume that instantly re-HALTs is a
    # resume that does not work.
    assert _streak(root, "step:settle")["n"] == 0


def test_the_settle_halt_stops_placement_and_not_the_tick(root, monkeypatch):
    """docs/22 section 7.1: the HALT stops betting, so the slot step is what stops.

    The tick itself keeps running, which is the point of the change: an account that has
    just decided to stop trading still has open positions the exchange will settle, and
    somebody has to keep writing them down.
    """
    _wedge_the_transport(root, monkeypatch)
    for _ in range(20):
        invoke("tick")
    assert (root / "data" / "HALT").exists()

    before = _audit_count(root, "tick_step_error")
    res = invoke("tick")
    assert res.exit_code == 0
    assert "HALT set mid-tick (settle_unreachable); skipping 'slots'" in res.stdout
    assert _audit_count(root, "tick_step_error") == before + 1   # settle still tried

    ran = Recorder(ret="A-0001")
    monkeypatch.setattr(cli, "run_attempt", ran)
    res = invoke("attempt", "--now")
    assert res.exit_code == 0 and "refusing" in res.stdout
    assert ran.count == 0

    from betting_agent.harness.safety import real_orders_allowed

    allowed, why = real_orders_allowed(cli._settings())
    assert allowed is False and why.startswith("halted: settle_unreachable")


def test_nineteen_failures_do_not_halt(root, monkeypatch):
    _wedge_the_transport(root, monkeypatch)
    for _ in range(19):
        invoke("tick")
    assert not (root / "data" / "HALT").exists()


# ----------------------------------------------------- D1(b): attempt-spawn streaks
def test_attempt_spawn_losses_streak_into_one_notification(root, monkeypatch, notify_calls):
    """docs/12 §6: a spawn that dies in 54ms is indistinguishable from a healthy one
    unless somebody reads the ledger. Three in a row now says so out loud."""
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: GRACE_NOW)   # past the 10:00 grace
    _fake_popen(monkeypatch)

    for i in range(3):
        # One tick can only lose one slot, so the slot is re-armed between rounds: this
        # test is about the streak across ticks, not about the slot machinery.
        lg = Ledger.open(root / "data" / "ledger.db")
        lg.meta_set("slot:2026-07-07/10:00", f"pending:2026-07-07T0{i}:00:00Z")
        lg.close()
        invoke("tick")

    assert _audit_count(root, "slot_spawn_lost") == 3
    assert len(notify_calls) == 1
    assert "3 consecutive attempt spawns failed" in notify_calls[0]
    (alert,) = _alert_details(root)
    assert alert["key"] == "attempt_spawn"
    assert alert["why"] == "spawn never became an attempt"


def test_a_child_claiming_its_slot_re_arms_the_spawn_streak(root, monkeypatch):
    """The re-arm point is the child taking ``attempt.lock`` — that, not ``Popen``
    returning, is what 'the spawn worked' means."""
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("alert_streak:attempt_spawn", json.dumps({"n": 2, "notified": False}))
    lg.close()

    monkeypatch.setattr(cli, "run_attempt", Recorder(ret="A-0001"))
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    assert invoke("attempt", "--slot", "slot:2026-07-07/10:00").exit_code == 0

    assert _streak(root, "attempt_spawn") == {"n": 0, "notified": False}


def test_a_child_refused_by_the_preflight_does_not_re_arm_the_spawn_streak(root, monkeypatch):
    """D8's preflight must not silence D1(b). The child stamps its slot marker before it
    knows whether Claude will answer, so re-arming there let the auth-dead child clear the
    very streak the ``slot_spawn_lost`` sweep was building."""
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("alert_streak:attempt_spawn", json.dumps({"n": 2, "notified": False}))
    lg.close()

    monkeypatch.setattr(cli, "run_attempt", Recorder(ret="A-0001"))
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "_auth_preflight", lambda settings: (False, "logged out"))
    assert invoke("attempt", "--slot", "slot:2026-07-07/10:00").exit_code == 1

    assert _streak(root, "attempt_spawn") == {"n": 2, "notified": False}


def test_an_auth_outage_still_reaches_the_spawn_failure_alert(root, monkeypatch, notify_calls):
    """The composition D8 and D1(b) have to hold together (docs/12 §6, §8.12): a dead
    Claude login burns neither an arm cell nor a slot, AND three days of it still reach a
    human. Each day: the child is refused by the preflight and hands the slot back, then
    the slot's window closes unrun and the sweep counts it."""
    monkeypatch.setattr(cli, "run_attempt", Recorder(ret="A-0001"))
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "_auth_preflight", lambda settings: (False, "logged out"))
    _patch_steps(monkeypatch)
    _fake_popen(monkeypatch)

    for day in (7, 8, 9):
        slot_key = f"slot:2026-07-0{day}/10:00"
        lg = Ledger.open(root / "data" / "ledger.db")
        lg.meta_set(slot_key, f"pending:2026-07-0{day}T09:00:00Z")
        lg.close()
        child_at = datetime(2026, 7, day, 10, 5, tzinfo=ET).astimezone(UTC)
        monkeypatch.setattr(cli, "utc_now", lambda c=child_at: c)
        invoke("attempt", "--slot", slot_key)      # the detached child, refused
        sweep_at = datetime(2026, 7, day, 13, 0, tzinfo=ET).astimezone(UTC)
        monkeypatch.setattr(cli, "utc_now", lambda s=sweep_at: s)
        invoke("tick")                             # window closed: slot_spawn_lost

    assert _audit_count(root, "auth_preflight_failed") == 3
    assert _audit_count(root, "slot_spawn_lost") == 3
    assert _streak(root, "attempt_spawn")["n"] == 3
    assert len(notify_calls) == 1
    assert "3 consecutive attempt spawns failed" in notify_calls[0]


def test_a_spawn_that_raises_is_counted_before_it_propagates(root, monkeypatch):
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)

    def _explode(*a, **k):
        raise OSError("fork failed")

    monkeypatch.setattr(cli, "_spawn_attempt", _explode)
    assert invoke("tick").exit_code == 0        # the step is still isolated

    assert _streak(root, "attempt_spawn")["n"] == 1
    assert _audit_count(root, "tick_step_error") == 1


# ------------------------------------------ deposit / withdraw (docs/14 B2, owner-run)
DEPOSIT_GENESIS = "2026-07-01T00:00:00Z"


def _go_live(root, balance="30.1622"):
    """Stamp a live era with ``balance`` as the anchor and no flows since."""
    lg = Ledger.open(root / "data" / "ledger.db")
    lg.meta_set("live_genesis_ts", DEPOSIT_GENESIS)
    lg.meta_set("live_genesis_balance", balance)
    lg.close()


def _anchor(root):
    lg = _open_ro(root)
    value = lg.meta_get("live_genesis_balance")
    ts = lg.meta_get("live_genesis_ts")
    lg.close()
    return value, ts


def _exchange(monkeypatch, balance):
    """A client whose only job is to answer ``get_balance`` (the command's one read)."""
    from betting_agent.kalshi.testing import FakeKalshi

    fake = FakeKalshi(balance=D(balance))
    monkeypatch.setattr(cli, "_client", lambda settings: fake)
    return fake


def test_deposit_command_records_the_anchor_and_prints_the_decisions_line(root, monkeypatch):
    """docs/14 B2 end to end: verify, move the anchor, hand back the line to paste."""
    _go_live(root)
    _exchange(monkeypatch, "50.1622")          # the $20 has landed; no flows since genesis

    res = invoke("deposit", "--amount", "20.00")

    assert res.exit_code == 0
    assert "deposit recorded" in res.stdout
    assert "Paste into docs/decisions.md" in res.stdout
    assert "meta.live_genesis_balance" in res.stdout
    assert "`live_genesis_ts` unchanged" in res.stdout
    balance, ts = _anchor(root)
    assert D(balance) == D("50.1622")
    assert ts == DEPOSIT_GENESIS               # the era boundary never moves
    assert _audit_count(root, "deposit_recorded") == 1


def test_deposit_command_refuses_a_wrong_amount_and_writes_nothing(root, monkeypatch):
    """The acceptance criterion: a disagreeing amount is refused, non-zero, no writes."""
    _go_live(root)
    _exchange(monkeypatch, "50.1622")          # $20 arrived...

    res = invoke("deposit", "--amount", "25.00")   # ...and $25 was claimed

    assert res.exit_code == 1
    assert "NOTHING was written" in res.stdout
    assert "5.0000 unaccounted for" in res.stdout
    assert D(_anchor(root)[0]) == D("30.1622")
    assert _audit_count(root, "deposit_recorded") == 0


def test_withdraw_command_moves_the_anchor_down(root, monkeypatch):
    _go_live(root)
    _exchange(monkeypatch, "25.1622")          # $5 has left the account

    res = invoke("withdraw", "--amount", "5.00")

    assert res.exit_code == 0
    assert D(_anchor(root)[0]) == D("25.1622")
    assert _anchor(root)[1] == DEPOSIT_GENESIS
    assert _audit_count(root, "withdrawal_recorded") == 1
    assert _audit_count(root, "deposit_recorded") == 0


def test_deposit_command_refuses_in_the_paper_era(root, monkeypatch):
    _exchange(monkeypatch, "50.1622")
    res = invoke("deposit", "--amount", "20.00")
    assert res.exit_code == 1 and "no live era yet" in res.stdout


def test_deposit_command_rejects_an_unparseable_amount(root, monkeypatch):
    _go_live(root)
    _exchange(monkeypatch, "50.1622")
    res = invoke("deposit", "--amount", "twenty")
    assert res.exit_code == 1 and "must be a dollar figure" in res.stdout
    assert D(_anchor(root)[0]) == D("30.1622")


def test_deposit_command_reports_an_unreachable_exchange_without_writing(root, monkeypatch):
    """No verification, no write. The amount is only ever as good as the balance read."""
    _go_live(root)

    def _no_creds(settings):
        raise RuntimeError("no private key")

    monkeypatch.setattr(cli, "_client", _no_creds)
    res = invoke("deposit", "--amount", "20.00")

    assert res.exit_code == 1
    assert "cannot reach the exchange" in res.stdout
    assert D(_anchor(root)[0]) == D("30.1622")


def test_deposit_command_waits_for_the_tick(root, monkeypatch):
    """Holds ``tick.lock``: an anchor that moves mid-reconciliation is a phantom-drift HALT."""
    _go_live(root)
    _exchange(monkeypatch, "50.1622")
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    holder = open(root / "data" / "locks" / "tick.lock", "a")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = invoke("deposit", "--amount", "20.00")
    finally:
        holder.close()

    assert res.exit_code == 1 and "tick.lock busy" in res.stdout
    assert D(_anchor(root)[0]) == D("30.1622")


# ------------------------------------------ credit (schema 010, owner-run)
CREDIT_ARGS = ("credit", "--amount", "0.01", "--date", "2026-09-20T04:45:00Z",
               "--kind", "incentive", "--reason",
               "Volume Incentive For Event KXRAINDNYC-260919")


def _credits(root):
    lg = _open_ro(root)
    rows = lg.conn.execute("SELECT * FROM credits ORDER BY credit_id").fetchall()
    lg.close()
    return rows


def test_credit_command_records_the_row_and_prints_the_decisions_line(root):
    """The 2026-09-20 incentive credit, end to end. It calls no exchange and moves no
    anchor: the credit is a dated flow, so the next reconciliation is what confirms it."""
    res = invoke(*CREDIT_ARGS)

    assert res.exit_code == 0
    assert "recorded $0.0100 incentive credited 2026-09-20T04:45:00Z" in res.stdout
    assert "Paste into docs/decisions.md" in res.stdout
    assert "$0.01 exchange credit recorded" in res.stdout
    assert "Volume Incentive For Event KXRAINDNYC-260919" in res.stdout

    rows = _credits(root)
    assert len(rows) == 1
    assert rows[0]["amount"] == "0.0100"
    assert rows[0]["kind"] == "incentive"
    assert rows[0]["credited_at"] == "2026-09-20T04:45:00Z"
    assert rows[0]["recorded_at"] and rows[0]["recorded_at"] != rows[0]["credited_at"]
    assert _audit_count(root, "credit_recorded") == 1

    lg = _open_ro(root)
    detail = json.loads(lg.audit_events(event="credit_recorded")[0]["detail"])
    lg.close()
    assert detail["amount"] == "0.0100"
    assert detail["kind"] == "incentive"
    assert detail["credited_at"] == "2026-09-20T04:45:00Z"
    assert detail["reason"].startswith("Volume Incentive")
    assert detail["credit_id"] == rows[0]["credit_id"]


def test_credit_command_refuses_nonsense_without_writing(root):
    bad_amount = invoke("credit", "--amount", "a penny", "--date", "2026-09-20T04:45:00Z")
    assert bad_amount.exit_code == 1 and "must be a dollar figure" in bad_amount.stdout

    negative = invoke("credit", "--amount", "-0.01", "--date", "2026-09-20T04:45:00Z")
    assert negative.exit_code == 1 and "must be positive" in negative.stdout

    bad_date = invoke("credit", "--amount", "0.01", "--date", "last Tuesday")
    assert bad_date.exit_code == 1 and "must be a timestamp" in bad_date.stdout

    assert _credits(root) == []
    assert _audit_count(root, "credit_recorded") == 0


def test_credit_command_waits_for_the_tick(root):
    """Holds ``tick.lock`` for ``deposit``'s reason: the reconciliation walks and then
    fetches a balance, and a term appearing between those two reads is a phantom drift."""
    (root / "data" / "locks").mkdir(parents=True, exist_ok=True)
    holder = open(root / "data" / "locks" / "tick.lock", "a")
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = invoke(*CREDIT_ARGS)
    finally:
        holder.close()

    assert res.exit_code == 1 and "tick.lock busy" in res.stdout
    assert _credits(root) == []


def test_a_credit_does_not_move_the_anchor(root):
    """The line between the two commands: a deposit is money with no date the walk can
    see, so it is absorbed into the anchor; a credit has a date, so it is a flow."""
    _go_live(root)
    assert invoke(*CREDIT_ARGS).exit_code == 0
    assert _anchor(root) == ("30.1622", DEPOSIT_GENESIS)
    assert _audit_count(root, "deposit_recorded") == 0


def test_the_tick_never_records_a_deposit(root, monkeypatch):
    """Owner-run only (docs/14 B2): the tick has no path to the money anchor.

    A transfer is an owner action with a stated amount, and nothing automated has an amount
    to state. A full tick must therefore leave both the anchor and the adjustment events
    exactly as it found them.
    """
    _go_live(root)
    _patch_steps(monkeypatch)
    monkeypatch.setattr(cli, "utc_now", lambda: SLOT_NOW)
    monkeypatch.setattr(cli, "_client", lambda settings: object())
    monkeypatch.setattr(cli, "_spawn_attempt", Recorder())

    assert invoke("tick").exit_code == 0

    assert _anchor(root) == ("30.1622", DEPOSIT_GENESIS)
    assert _audit_count(root, "deposit_recorded") == 0
    assert _audit_count(root, "withdrawal_recorded") == 0
