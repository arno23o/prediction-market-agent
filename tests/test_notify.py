"""Escalation: the osascript notifier and the generic streak core (docs/14 D1).

The unit half of D1. The wiring, meaning the tick steps, the settle HALT line, attempt
spawns and the ledger invariants, is exercised where it lives (``test_cli.py``).
"""

import json

import pytest

from betting_agent.config import load_settings
from betting_agent.harness import notify as nf
from betting_agent.ledger.db import Ledger

# Captured before the autouse ``notify_calls`` fixture replaces it, so the OS-touching
# seam itself can still be tested.
_REAL_OSASCRIPT = nf._osascript


@pytest.fixture
def lg(tmp_path):
    ledger = Ledger.open(tmp_path / "ledger.db")
    ledger.migrate()
    yield ledger
    ledger.close()


@pytest.fixture
def settings(tmp_path):
    return load_settings(root=tmp_path)


def _alerts(ledger) -> list[dict]:
    return [json.loads(r["detail"]) for r in ledger.audit_events(event="alert_raised")]


# --------------------------------------------------------------------------- the notifier
def test_notify_builds_an_applescript_carrying_title_and_message(settings, notify_calls):
    assert nf.notify(settings, "a title", "a message") is True
    assert len(notify_calls) == 1
    assert 'with title "a title"' in notify_calls[0]
    assert 'display notification "a message"' in notify_calls[0]


def test_quotes_and_backslashes_are_escaped_not_injected(settings, notify_calls):
    nf.notify(settings, 'ti"tle', 'back\\slash and "quote"')
    script = notify_calls[0]
    assert 'ti\\"tle' in script
    assert 'back\\\\slash and \\"quote\\"' in script


def test_notify_is_off_when_alerts_are_disabled(tmp_path, monkeypatch, notify_calls):
    monkeypatch.setenv("BETTING_AGENT_ALERTS__ENABLED", "false")
    settings = load_settings(root=tmp_path)
    assert nf.notify(settings, "t", "m") is False
    assert notify_calls == []


def test_a_notifier_that_explodes_is_swallowed(settings, monkeypatch):
    """docs/14 D1: 'failure to notify is itself non-fatal'. The caller is already
    reporting a problem; it must not acquire a second one here."""
    def _boom(script):
        raise RuntimeError("no osascript on this machine")

    monkeypatch.setattr(nf, "_osascript", _boom)
    assert nf.notify(settings, "t", "m") is False   # …and notify() still does not raise


def test_the_real_seam_swallows_a_subprocess_failure(monkeypatch):
    """The unpatched ``_osascript`` is the thing that must never raise, so test it
    directly against a subprocess.run that fails the way a missing binary does."""
    def _explode(*a, **k):
        raise FileNotFoundError("osascript")

    monkeypatch.setattr(nf.subprocess, "run", _explode)
    assert _REAL_OSASCRIPT('display notification "x"') is False


# --------------------------------------------------------------------------- raise_alert
def test_raise_alert_writes_the_durable_half(lg, settings, notify_calls):
    nf.raise_alert(lg, settings, key="k", title="t", message="m", detail={"extra": 1})
    (row,) = _alerts(lg)
    assert row == {"key": "k", "message": "m", "notified": True, "extra": 1}
    assert len(notify_calls) == 1


def test_alert_survives_an_unwritable_ledger(settings, tmp_path, notify_calls):
    """The banner going out matters more than the row: a read-only ledger must not turn
    an alert into an exception on the tick's path."""
    ro = Ledger.open(tmp_path / "l.db")
    ro.migrate()
    ro.close()
    ro = Ledger.open(tmp_path / "l.db", readonly=True)
    assert nf.raise_alert(ro, settings, key="k", title="t", message="m") is True
    ro.close()


# --------------------------------------------------------------------------- streak core
def _fail(lg, settings, name="thing", threshold=3):
    return nf.record_failure(lg, settings, name, threshold=threshold,
                             title="t", message="m")


def test_one_notification_per_streak_not_per_failure(lg, settings, notify_calls):
    results = [_fail(lg, settings) for _ in range(6)]
    assert [r["n"] for r in results] == [1, 2, 3, 4, 5, 6]
    assert [r["alerted"] for r in results] == [False, False, True, False, False, False]
    assert len(notify_calls) == 1
    assert len(_alerts(lg)) == 1
    assert _alerts(lg)[0]["streak"] == 3


def test_success_re_arms_the_streak(lg, settings, notify_calls):
    for _ in range(3):
        _fail(lg, settings)
    assert len(notify_calls) == 1
    nf.record_success(lg, "thing")
    assert nf.streak_state(lg, "thing") == {"n": 0, "notified": False}
    for _ in range(3):
        _fail(lg, settings)
    assert len(notify_calls) == 2      # a second outage is a second notification


def test_streaks_are_independent_by_name(lg, settings, notify_calls):
    for _ in range(3):
        _fail(lg, settings, name="a")
    for _ in range(2):
        _fail(lg, settings, name="b")
    assert len(notify_calls) == 1
    assert nf.streak_state(lg, "a")["n"] == 3
    assert nf.streak_state(lg, "b")["n"] == 2


def test_threshold_of_one_alerts_immediately(lg, settings, notify_calls):
    assert _fail(lg, settings, threshold=1)["alerted"] is True
    assert len(notify_calls) == 1


def test_a_corrupt_counter_rebuilds_instead_of_raising(lg, settings):
    lg.meta_set("alert_streak:thing", "{not json")
    assert nf.streak_state(lg, "thing") == {"n": 0, "notified": False}
    assert _fail(lg, settings)["n"] == 1


def test_success_on_an_unknown_streak_writes_nothing(lg):
    nf.record_success(lg, "never-failed")
    assert lg.meta_get("alert_streak:never-failed") is None


def test_a_cleared_streak_is_not_rewritten_on_every_success(lg, settings):
    """``record_success`` runs on every step of every tick; the steady state is a read."""
    _fail(lg, settings)
    nf.record_success(lg, "thing")
    writes = []
    lg.meta_set = lambda *a, **k: writes.append(a)   # noqa: ARG005 - a write counter
    for _ in range(5):
        nf.record_success(lg, "thing")
    assert writes == []
