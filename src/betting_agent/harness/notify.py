"""Escalation: macOS notifications and consecutive-failure streaks (docs/14 D1).

Before this module the system had no path from "something is broken" to "a human knows".
docs/12 §8.1 is the proof: a Kalshi 403 recurring every 15 minutes wrote one
``tick_step_error`` row per tick for three days, reached nobody, and cost the settlement
backlog. Everything that wants to escalate now goes through here, and here has exactly
three jobs:

* :func:`notify` — one macOS user notification via ``osascript``. **It never raises.**
  A machine with no ``osascript``, a denied notification permission, a hung
  ``osascript``: all of those are less important than whatever was being reported, so
  they are swallowed and reported in the return value instead.
* :func:`raise_alert` — a notification plus an ``alert_raised`` audit row. The audit row
  is the durable half: notifications are ephemeral and un-queryable, so the ledger keeps
  the record whether or not the banner appeared.
* :func:`record_failure` / :func:`record_success` — the **generic streak core**. A streak
  is any repeated failure worth escalating, identified by a free-form ``name``: the tick's
  per-step failures (``step:settle``), attempt-spawn deaths (``attempt_spawn``), and the
  drawdown floor refusing an attempt's whole book (``drawdown_floor``, at threshold one).
  A caller supplies the name, the threshold and the message; nothing here knows what any
  particular streak means.

**One notification per streak, not per failure.** The counter and its "already notified"
flag live in ``meta`` (key ``alert_streak:<name>``), because ticks are separate processes
and a counter in memory resets every fifteen minutes. The flag re-arms on the first
success, so a flapping step notifies once per outage rather than once per tick.
"""

from __future__ import annotations

import json
import subprocess

_META_PREFIX = "alert_streak:"

# osascript is a subprocess on the tick's critical path; a wedged one must not become the
# new way the tick hangs. Two seconds is an eternity for a notification and nothing next
# to a 15-minute tick.
_NOTIFY_TIMEOUT_S = 2


def _osa_string(text: str) -> str:
    """AppleScript string literal body: only backslash and double quote need escaping."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _osascript(script: str) -> bool:
    """Run one AppleScript. The single seam that touches the operating system.

    Isolated on purpose: it is what the test suite replaces (``tests/conftest.py``) so
    that running pytest on the live machine never actually posts a banner, and it is the
    one place that has to swallow everything the OS can throw.
    """
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["osascript", "-e", script],
            capture_output=True,
            timeout=_NOTIFY_TIMEOUT_S,
            check=False,
        )
    except Exception:  # noqa: BLE001 - notifying must never be the thing that fails
        return False
    return proc.returncode == 0


def notify(settings, title: str, message: str) -> bool:
    """Post one macOS user notification. Never raises; returns whether it went out.

    ``alerts.enabled`` (default on) is the operator's off switch, and every failure mode
    of the notification itself — no ``osascript``, notifications denied, a hang — returns
    ``False`` rather than propagating. The caller is in the middle of reporting a problem;
    it must not acquire a second one here (docs/14 D1: "failure to notify is itself
    non-fatal").
    """
    alerts = getattr(settings, "alerts", None)
    if alerts is not None and not getattr(alerts, "enabled", True):
        return False
    try:
        return _osascript(
            f'display notification "{_osa_string(message)}" '
            f'with title "{_osa_string(title)}"'
        )
    except Exception:  # noqa: BLE001 - belt to _osascript's braces; the contract is total
        return False


def raise_alert(ledger, settings, *, key: str, title: str, message: str,
                detail: dict | None = None) -> bool:
    """Notify **and** audit ``alert_raised``; never raises. Returns the notify result.

    ``key`` names what is being escalated (``step:settle``, ``invariant:orphan_bets``)
    and lands in the audit detail, so "what alerted, when, how often" is answerable
    from SQL even though the banners themselves are gone.
    """
    delivered = notify(settings, title, message)
    payload = {"key": key, "message": message, "notified": delivered}
    if detail:
        payload.update(detail)
    try:
        ledger.audit("alert_raised", detail=payload)
    except Exception:  # noqa: BLE001 - the alert matters more than its own bookkeeping
        pass
    return delivered


# --------------------------------------------------------------------------- streak core
def _meta_key(name: str) -> str:
    return f"{_META_PREFIX}{name}"


def streak_state(ledger, name: str) -> dict:
    """``{"n": int, "notified": bool}`` for ``name``; zeroed when absent or unreadable.

    An unreadable marker is treated as no streak rather than as an error: a corrupt
    counter must not stop the tick, and the next failure rebuilds it from 1.
    """
    raw = ledger.meta_get(_meta_key(name))
    if not raw:
        return {"n": 0, "notified": False}
    try:
        data = json.loads(raw)
        return {"n": int(data.get("n", 0)), "notified": bool(data.get("notified", False))}
    except (ValueError, TypeError, AttributeError):
        return {"n": 0, "notified": False}


def record_success(ledger, name: str) -> None:
    """Clear ``name``'s streak — the re-arm. Never raises.

    Called on every success of every tick step, so the steady state has to be cheap: it
    writes only when there is actually a streak to clear, which after the first recovery
    is never again. One ``meta`` read per step per tick is the whole ongoing cost.
    """
    try:
        state = streak_state(ledger, name)
        if state["n"] == 0 and not state["notified"]:
            return
        ledger.meta_set(_meta_key(name), json.dumps({"n": 0, "notified": False}))
    except Exception:  # noqa: BLE001 - streak bookkeeping is never worth a raise
        pass


def record_failure(ledger, settings, name: str, *, threshold: int, title: str,
                   message: str, detail: dict | None = None) -> dict:
    """Count one failure of ``name``; alert on the tick that reaches ``threshold``.

    Returns ``{"n": …, "alerted": bool}`` — ``n`` is the streak length **including** this
    failure, and ``alerted`` is true only on the single call that crossed the line. The
    "already notified" flag is what keeps a three-day outage to one banner; only
    :func:`record_success` lowers it.

    ``message`` is formatted by the caller and should say what is broken and for how long;
    it is what Arno reads on the lock screen at 3am.

    A notification that failed to *deliver* still arms the flag. Retrying each tick would
    turn a machine with notifications switched off into one ``alert_raised`` row every
    fifteen minutes — the very noise this replaces. The audit row records
    ``notified: false``, so the delivery failure is visible where it matters.
    """
    state = streak_state(ledger, name)
    n = state["n"] + 1
    notified = state["notified"]
    alerted = False
    if n >= threshold and not notified:
        raise_alert(ledger, settings, key=name, title=title, message=message,
                    detail={**(detail or {}), "streak": n, "threshold": threshold})
        notified = True
        alerted = True
    try:
        ledger.meta_set(_meta_key(name), json.dumps({"n": n, "notified": notified}))
    except Exception:  # noqa: BLE001 - the alert already went out; the counter is advisory
        pass
    return {"n": n, "alerted": alerted}
