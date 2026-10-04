"""Shared pytest configuration.

Two jobs: register the ``prod_ro`` marker used by the live read-only production
integration tests (opt-in via ``RUN_PROD_RO=1``), and isolate every test in the suite
from the operator's own environment.

Environment isolation (TQ-4)
----------------------------
This is the *live operator's* machine. The shell that runs ``pytest`` here is the same
shell that runs ``betting-agent``, so it may export ``BETTING_AGENT_STAKES__LIVE_TRADING``,
``KALSHI_API_KEY_ID``, ``BT_ROOT`` and friends — and ``Settings`` reads exactly those.
The isolation used to be a hand-copied ``_clean_env`` fixture in ten test files and absent
from six others that build ``Settings`` anyway, so whether a test saw the real
configuration depended on which file it happened to live in. One autouse fixture here
covers the whole suite, including the files nobody remembered to protect.

The stripped set is the union of every copy plus the prefixes they each half-covered:

* ``BETTING_AGENT_*`` — pydantic-settings' env prefix: every config field, live gate
  and spend cap included;
* ``KALSHI_*`` — the exchange credentials (``test_kalshi_prod_ro.py`` reads its creds
  out of ``.env`` by hand rather than from the process environment, so it is unaffected);
* ``BT_*`` — the toolkit's root/memory/review pointers a spawned session inherits;
* ``FAKE_*``/``STUB_*`` — the fake-agent and stub-runner scripting channels;
* ``ANTHROPIC_API_KEY`` — never wanted in a test process.

Tests that *need* one of these set it themselves through ``monkeypatch`` in a fixture,
which runs after this one (autouse fixtures are set up before the fixtures a test
requests explicitly) and is undone at teardown just the same.

Notification isolation (docs/14 D1)
-----------------------------------
The alerting path shells out to ``osascript``. This is the live operator's machine, and a
test run that WARNs — the audit suite does, deliberately — would post real banners to his
notification centre. ``notify_calls`` (autouse) replaces the one OS-touching seam with a
recorder, so the whole path still runs and is assertable, and nothing reaches the desktop.
"""

import os

import pytest

from betting_agent.harness import notify as notify_mod

# Prefix families, not a name list: a new BETTING_AGENT_* field or FAKE_* channel must
# not be able to leak in merely because nobody remembered to add it here.
_STRIP_PREFIXES = ("BETTING_AGENT_", "KALSHI_", "BT_", "FAKE_", "STUB_")
_STRIP_NAMES = ("ANTHROPIC_API_KEY",)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "prod_ro: live read-only production integration tests (opt-in via RUN_PROD_RO=1)",
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip every harness-visible environment variable, for every test (TQ-4)."""
    for key in list(os.environ):
        if key.startswith(_STRIP_PREFIXES) or key in _STRIP_NAMES:
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def notify_calls(monkeypatch):
    """Capture every AppleScript the alerting path would have run (docs/14 D1).

    Autouse so no test can post a real banner by accident; requestable by name so a test
    about alerting can assert on what would have been shown. The recorded value is the
    script text, which carries both the title and the message.
    """
    calls: list[str] = []

    def _fake(script: str) -> bool:
        calls.append(script)
        return True

    monkeypatch.setattr(notify_mod, "_osascript", _fake)
    return calls


@pytest.fixture(autouse=True)
def _stub_auth_preflight(monkeypatch):
    """Never probe the operator's real ``claude`` login (docs/14 D8).

    Same doctrine as ``_isolate_env``, one layer out: the D8 preflight shells out to
    ``claude auth status`` before every attempt, and left alone it would make this
    suite's results depend on whether *this machine* happens to be logged in — green
    normally, red during exactly the outage the preflight exists to survive. Every test
    gets a stubbed "ok"; the tests that exercise the preflight patch it themselves, and
    :func:`betting_agent.sessions.check_claude_auth` is unit-tested directly against
    fake CLI answers.
    """
    from betting_agent import cli

    monkeypatch.setattr(cli, "_auth_preflight", lambda settings: (True, "stubbed in tests"))
