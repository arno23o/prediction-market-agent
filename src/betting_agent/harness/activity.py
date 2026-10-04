"""What an attempt actually did, read off its own session streams.

The ledger has always recorded what an attempt *produced* (a ticket, some bets, a
retrospective) and what it *cost* (turns, tokens, dollars, wall seconds). It has never
recorded what it **did**: how many markets it looked at, which tools it reached for, how
much of the web it read, how long it deliberated before committing to a ticket. Answering
"did the attempts that examined many markets do better?" meant reading 210 transcripts by
hand, so nobody answered it.

This module turns each attempt's ``logs/session*.stream.jsonl`` into one row of counts.
Every field is a count or a small JSON blob; nothing here is money, nothing here feeds a
decision, and nothing here can fail an attempt (the caller wraps it in a try/except, and
the extractor itself is written not to raise on a truncated, half-written or junk stream).

**Provenance, and its limits.**

* The streams are read through :func:`betting_agent.sessions.iter_stream_records` — the one
  junk-tolerant reader, which also handles the ``.gz`` files the retention pass leaves
  behind. There is deliberately no second parser for this format.
* Only the attempt's own sessions are read: ``session.stream.jsonl`` for a one-loop attempt
  and ``session-ideation`` / ``session-critic`` / ``session-implementation`` for a two-loop
  one. Grading and deep-review streams live beside them and are NOT the attempt's activity.
* ``markets_probed`` counts tickers in commands the agent **typed**; ``markets_seen`` counts
  tickers in what came **back**. One `bt board` prints thousands of tickers the agent never
  looked at, which is exactly why the two are separate columns and why only the first is a
  measure of attention.
* Streams written before Claude Code started stamping ``timestamp`` on assistant records
  (every attempt up to A-0076) carry no clock at all, so ``first_ticket_write_ts`` and
  ``first_commit_frac`` are NULL for them. NULL means "not instrumented", never "zero".
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from betting_agent.sessions import iter_stream_records
from betting_agent.timeutil import parse_iso

# The attempt's own phases, in the order they ran. ``session.stream.jsonl`` is the one-loop
# stream; the three ``session-<phase>`` files are the two-loop ones. Grading and review
# streams (``grading.N.stream.jsonl``) share the directory and are not activity.
_PHASE_ORDER = {"": 0, "ideation": 1, "critic": 2, "implementation": 3}

# A Kalshi market ticker: a KX series followed by one or more dash-separated segments
# (``KXHIGHTPHX-26AUG15-B104.5``). The series is the part before the first hyphen.
_TICKER_RE = re.compile(r"\bKX[A-Z0-9]+(?:-[A-Z0-9.]+)+\b")

# Another attempt's id, as a ticket cites it.
_ATTEMPT_RE = re.compile(r"\bA-\d{4}\b")

# A markdown list item: "- x", "* x", "+ x", "1. x", "1) x".
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\S")

# ``## Kill criteria`` in ticket/hypothesis.md, and the heading that ends the section.
_KILL_HEADING_RE = re.compile(r"^\s*#{1,6}\s*kill\s+criteria\b", re.IGNORECASE)
_ANY_HEADING_RE = re.compile(r"^\s*#{1,6}\s+\S")

# The toolkit's registered commands (``bt.py``), so the counts are a clean vocabulary rather
# than whatever token followed the word "bt" in a heredoc. Groups take a second word:
# ``past search``, ``past family``, ``ticket validate``. Anything unrecognized is
# counted once under "other" — see ``tests/test_activity.py``, which asserts this set still
# matches what ``bt`` actually registers.
_BT_GROUPS = {
    "ticket": {"validate"},
    # docs/22 section 9. `past` is also counted on its own, in `past_calls` and
    # `past_subcommands`: how much of the past an attempt read is a question about the
    # learning loop, and it should not have to be dug out of the `bt_calls` blob.
    "past": {"families", "family", "search", "attempt", "page", "recent"},
}
_BT_COMMANDS = frozenset({
    "markets", "series", "search", "new", "movers", "board", "calendar", "market",
    "book", "history", "fees", "size",
}) | set(_BT_GROUPS)
_BT_OTHER = "other"
_BT_PAST = "past"

# A Bash call counts as a code run when it invokes an interpreter or a script directly.
_INTERPRETERS = frozenset({"python", "python3", "node"})
_SCRIPT_SUFFIXES = (".py", ".js", ".sh")

# Shell redirects and ``tee``, the two ways a session writes a file without the Write tool.
_REDIRECT_RE = re.compile(r"(?:>>?|\btee(?:\s+-a)?\s)\s*([^\s;|&<>]+)")

# The two filenames that mean "the ticket" wherever they are written; every other target has
# to be under a ``ticket/`` directory to count. ``hypothesis.md`` and ``MANIFEST.md`` are
# deliberately not here — sessions draft files by those names in the workspace, and the
# question this column answers is when the agent started committing, not when it took notes.
_TICKET_FILENAMES = frozenset({"bets.json", "edge_claim.md"})

# The domains list is a label, not a dataset: enough to see where an attempt read, capped so
# one crawl-happy session cannot write a kilobyte of JSON into every query that reads this
# table. ``n_domains`` carries the true count when the list is truncated.
_MAX_DOMAINS = 50
_MAX_SERIES = 20


# --------------------------------------------------------------------------- helpers
def stream_paths(attempt_dir: Path) -> list[Path]:
    """The attempt's own session streams, in phase order.

    Compressed twins count too: the retention pass rewrites an old stream as ``<name>.gz``,
    and an attempt swept by it still has activity. When both spellings exist (a crash
    between writing the ``.gz`` and removing the original) the uncompressed one wins:
    same bytes, cheaper to read.
    """
    logs = Path(attempt_dir) / "logs"
    if not logs.is_dir():
        return []
    by_stem: dict[str, Path] = {}
    for path in sorted(logs.glob("session*.stream.jsonl*")):
        if not (path.name.endswith(".stream.jsonl") or path.name.endswith(".stream.jsonl.gz")):
            continue
        stem = path.name.split(".")[0]
        if stem in by_stem and not by_stem[stem].name.endswith(".gz"):
            continue
        by_stem[stem] = path
    return sorted(
        by_stem.values(),
        key=lambda p: (_PHASE_ORDER.get(p.name.split(".")[0].removeprefix("session").lstrip("-"),
                                        99), p.name),
    )


def _tokens(command: str) -> list[str]:
    """Shell tokens of ``command``; whitespace split when it will not parse.

    An agent's command line is not guaranteed to be well-formed shell (an unbalanced quote
    inside a heredoc is enough), and a command that will not tokenize is still evidence.
    """
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _bt_invocations(tokens: list[str]) -> list[str]:
    """The ``bt`` subcommands one command line invokes, e.g. ``["past search"]``.

    Flags are skipped, so ``bt --json market X`` and ``bt market --json X`` both read as
    ``market``. A group name takes the next non-flag word with it when that word is one of
    the group's own commands. Everything else is ``other``.
    """
    out: list[str] = []
    for i, token in enumerate(tokens):
        if token != "bt" and not token.endswith("/bt"):
            continue
        rest = [t for t in tokens[i + 1:] if not t.startswith("-")]
        if not rest:
            continue
        name = rest[0]
        if name in _BT_GROUPS:
            sub = rest[1] if len(rest) > 1 else None
            out.append(f"{name} {sub}" if sub in _BT_GROUPS[name] else name)
        elif name in _BT_COMMANDS:
            out.append(name)
        else:
            out.append(_BT_OTHER)
    return out


def _past_counts(bt_calls: Counter[str]) -> tuple[int, dict[str, int]]:
    """``bt past`` calls and the subcommands they named (docs/22 section 4.5).

    Derived from the ``bt`` counts rather than from a second pass over the commands, so
    "how a ``bt past`` call is detected" has exactly one answer. A call with no subcommand,
    or one this toolkit does not register, lands under ``other`` the way it does there.
    """
    subs: Counter[str] = Counter()
    for name, count in bt_calls.items():
        if name != _BT_PAST and not name.startswith(f"{_BT_PAST} "):
            continue
        sub = name[len(_BT_PAST) + 1:] or _BT_OTHER
        subs[sub] += count
    return sum(subs.values()), dict(sorted(subs.items()))


def _is_code_run(tokens: list[str]) -> bool:
    """True when the command RUNS code: an interpreter, or a script in command position.

    Segment-aware on purpose. An interpreter name anywhere in the line is an invocation
    (``python3 -c``, ``cat x | python3 -``), but a script file is only being run when it
    leads a segment — otherwise ``cat model.py`` and ``grep x model.py``, which are reading
    about code rather than running it, would count as computation.
    """
    for segment in _segments(tokens):
        if not segment:
            continue
        if any(t.rsplit("/", 1)[-1] in _INTERPRETERS for t in segment):
            return True
        head = segment[0]
        if head.endswith(_SCRIPT_SUFFIXES) or head.startswith("./"):
            return True
    return False


def _segments(tokens: list[str]) -> list[list[str]]:
    """Split a token list on the shell operators that start a new command."""
    out: list[list[str]] = [[]]
    for token in tokens:
        if token in ("|", "||", "&&", ";", "&"):
            out.append([])
        else:
            out[-1].append(token)
    return out


def _is_ticket_target(path: str) -> bool:
    """True when a written path is part of the ticket (see ``_TICKET_FILENAMES``)."""
    cleaned = path.strip().strip("'\"")
    if not cleaned:
        return False
    if "ticket/" in cleaned:
        return True
    return cleaned.rsplit("/", 1)[-1] in _TICKET_FILENAMES


def _result_text(block: dict) -> str:
    """The text of one ``tool_result`` block, whichever shape it arrived in."""
    inner = block.get("content")
    if isinstance(inner, str):
        return inner
    if isinstance(inner, list):
        return "\n".join(
            str(part.get("text") or "") for part in inner if isinstance(part, dict)
        )
    return ""


def _host_of(url) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        host = urlparse(url if "//" in url else f"https://{url}").hostname
    except ValueError:
        return None
    return host.lower() if host else None


# --------------------------------------------------------------------------- the ticket
def _ticket_facts(attempt_dir: Path, attempt_id: str | None) -> dict:
    """Everything the extractor reads off ``ticket/`` rather than off the streams."""
    ticket = Path(attempt_dir) / "ticket"
    facts = {
        "cites_predecessor": 0, "playbook_refs": None, "kill_criteria_n": 0,
        "ticket_chars": 0, "passed": None, "bets_proposed": None,
    }
    if not ticket.is_dir():
        return facts

    cited: set[str] = set()
    chars = 0
    for md in sorted(ticket.glob("*.md")):
        try:
            text = md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chars += len(text)
        cited.update(_ATTEMPT_RE.findall(text))
    cited.discard(attempt_id or "")
    facts["cites_predecessor"] = len(cited)
    facts["ticket_chars"] = chars
    facts["kill_criteria_n"] = _kill_criteria_n(ticket / "hypothesis.md")

    bets_path = ticket / "bets.json"
    if bets_path.is_file():
        try:
            obj = json.loads(bets_path.read_text(encoding="utf-8", errors="replace"))
            bets = obj.get("bets") if isinstance(obj, dict) else None
        except (OSError, ValueError):
            bets = None
        if isinstance(bets, list):
            facts["bets_proposed"] = len(bets)
            # "Passed" is the attempt that looked and declined: a ticket that exists and
            # proposes nothing. An unreadable or malformed bets.json is neither a pass nor
            # a bet, so both columns stay NULL for it.
            facts["passed"] = 1 if not bets else 0
    return facts


def _kill_criteria_n(path: Path) -> int:
    """List items under the ``## Kill criteria`` heading of ``hypothesis.md``.

    The section ends at the next heading of any level. Wrapped continuation lines do not
    start with a list marker and so are not counted; a nested item is, which is the same
    rule a reader applies by eye.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    n = 0
    inside = False
    for line in lines:
        if _KILL_HEADING_RE.match(line):
            inside = True
            continue
        if not inside:
            continue
        if _ANY_HEADING_RE.match(line):
            break
        if _LIST_ITEM_RE.match(line):
            n += 1
    return n


# --------------------------------------------------------------------------- the stamps
def _repo_root(attempt_dir: Path) -> Path | None:
    """The repository this attempt directory lives in, found by walking up to ``.git``."""
    for candidate in [Path(attempt_dir).resolve(), *Path(attempt_dir).resolve().parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git(root: Path, *args: str) -> str | None:
    """``git <args>`` in ``root``, or None if git cannot answer. Read-only by construction."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=str(root), capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def code_stamp(attempt_dir: Path, root: Path | None = None) -> dict:
    """``{git_commit, git_dirty, config_sha}`` for the tree the extractor is running in.

    The point is to line changes in the code and the prompts up against changes in outcome.
    Read the caveat on the column: for a row written by the session-end hook this is the
    tree the attempt actually ran under, and for a backfilled row it is today's tree, which
    is why ``stamped_at_backfill`` exists and why a backfilled stamp answers nothing about
    the attempt. Every field degrades to NULL rather than raising: no git, no repo, a
    deleted config file, a machine without a working ``git`` — none of those are a reason
    to lose the counts this row is actually for.
    """
    root = root or _repo_root(attempt_dir)
    if root is None:
        return {"git_commit": None, "git_dirty": None, "config_sha": None}
    # Cached per repo for the life of the process: a backfill over 200 attempts asks the
    # same question 200 times and the answer cannot change under it.
    return dict(_code_stamp_cached(Path(root)))


@lru_cache(maxsize=8)
def _code_stamp_cached(root: Path) -> tuple[tuple[str, object], ...]:
    stamp: dict = {"git_commit": None, "git_dirty": None, "config_sha": None}
    head = _git(root, "rev-parse", "--short", "HEAD")
    stamp["git_commit"] = head.strip() or None if head is not None else None
    porcelain = _git(root, "status", "--porcelain")
    if porcelain is not None:
        stamp["git_dirty"] = 1 if porcelain.strip() else 0
    try:
        stamp["config_sha"] = hashlib.sha256(
            (root / "config.toml").read_bytes()
        ).hexdigest()[:12]
    except OSError:
        pass
    return tuple(stamp.items())


# --------------------------------------------------------------------------- extraction
def extract_activity(attempt_dir: Path, wall_seconds: int | None) -> dict:
    """One row of "what this attempt did", from its streams and its ticket.

    ``wall_seconds`` is the attempt's own recorded compute time (the sum across phases for
    a two-loop attempt); it is the denominator of ``first_commit_frac`` and nothing else, so
    passing None costs that one field. The fraction is left un-clamped on purpose: a value
    above 1 means the session's wall clock outran the compute it recorded, which is the
    host-sleep signature D9 already tracks, and hiding it behind a clamp would make a
    measurement out of an anomaly.

    Never raises on stream content. Every reader below is defensive because these files are
    logs of processes that were sometimes killed mid-write.
    """
    attempt_dir = Path(attempt_dir)
    paths = stream_paths(attempt_dir)

    tools: Counter[str] = Counter()
    bt_calls: Counter[str] = Counter()
    probed: set[str] = set()
    seen: set[str] = set()
    domains: Counter[str] = Counter()
    code_runs = 0
    files_written = 0
    first_ts: str | None = None
    first_ticket_ts: str | None = None
    stream_bytes = 0

    for path in paths:
        try:
            stream_bytes += path.stat().st_size
        except OSError:
            pass
        for record in iter_stream_records(path):
            ts = record.get("timestamp")
            if isinstance(ts, str) and ts and (first_ts is None or ts < first_ts):
                first_ts = ts
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                kind = block.get("type")
                if kind == "tool_result":
                    seen.update(_TICKER_RE.findall(_result_text(block)))
                    continue
                if kind != "tool_use":
                    continue
                name = block.get("name")
                name = name if isinstance(name, str) and name else "unknown"
                tools[name] += 1
                inputs = block.get("input")
                inputs = inputs if isinstance(inputs, dict) else {}
                wrote_ticket = False

                if name == "Bash":
                    command = inputs.get("command")
                    command = command if isinstance(command, str) else ""
                    probed.update(_TICKER_RE.findall(command))
                    tokens = _tokens(command)
                    bt_calls.update(_bt_invocations(tokens))
                    if _is_code_run(tokens):
                        code_runs += 1
                    wrote_ticket = any(
                        _is_ticket_target(target)
                        for target in _REDIRECT_RE.findall(command)
                    )
                elif name in ("Write", "Edit"):
                    files_written += 1
                    target = inputs.get("file_path")
                    wrote_ticket = _is_ticket_target(target if isinstance(target, str) else "")
                elif name in ("WebFetch", "WebSearch"):
                    host = _host_of(inputs.get("url"))
                    if host:
                        domains[host] += 1

                if wrote_ticket and isinstance(ts, str) and ts and (
                    first_ticket_ts is None or ts < first_ticket_ts
                ):
                    first_ticket_ts = ts

    series = {t.split("-", 1)[0] for t in probed}
    past_calls, past_subcommands = _past_counts(bt_calls)
    row = {
        "n_streams": len(paths),
        "stream_bytes": stream_bytes,
        "tool_calls": json.dumps(dict(sorted(tools.items())), sort_keys=True),
        "n_tool_calls": sum(tools.values()),
        "bt_calls": json.dumps(dict(sorted(bt_calls.items())), sort_keys=True),
        "n_bt_calls": sum(bt_calls.values()),
        "past_calls": past_calls,
        "past_subcommands": json.dumps(past_subcommands, sort_keys=True),
        "markets_probed": len(probed),
        "series_probed": len(series),
        "series_list": json.dumps(sorted(series)[:_MAX_SERIES]),
        "markets_seen": len(seen),
        "web_fetches": tools.get("WebFetch", 0),
        "web_searches": tools.get("WebSearch", 0),
        "domains": json.dumps([host for host, _n in domains.most_common(_MAX_DOMAINS)]),
        "n_domains": len(domains),
        "code_runs": code_runs,
        "files_written": files_written,
        # Subagents the session launched. A delegating attempt and a solo one spend their
        # turns very differently, and the tool name is the only place that shows.
        "agent_calls": tools.get("Agent", 0) + tools.get("Task", 0),
        "first_ticket_write_ts": first_ticket_ts,
        "first_commit_frac": _commit_frac(first_ts, first_ticket_ts, wall_seconds),
    }
    row.update(_ticket_facts(attempt_dir, attempt_dir.name))
    row.update(code_stamp(attempt_dir))
    return row


def _commit_frac(first_ts, ticket_ts, wall_seconds) -> float | None:
    """How far into the session's compute the first ticket write landed, or None."""
    if not first_ts or not ticket_ts or not wall_seconds:
        return None
    try:
        start = parse_iso(first_ts)
        wrote = parse_iso(ticket_ts)
    except (ValueError, TypeError):
        return None
    if start is None or wrote is None:
        return None
    return round((wrote - start).total_seconds() / float(wall_seconds), 4)


def activity_row(attempt_dir: Path, attempt_id: str, wall_seconds: int | None, *,
                 prompt_version: str | None = None, backfill: bool = False) -> dict:
    """:func:`extract_activity` plus the three fields only the caller knows.

    ``attempt_id`` keys the row, ``prompt_version`` is copied off the attempt so the row is
    self-contained, and ``stamped_at_backfill`` records which kind of stamp the code fields
    carry (see :func:`code_stamp`).
    """
    row = extract_activity(attempt_dir, wall_seconds)
    row["attempt_id"] = attempt_id
    row["prompt_version"] = prompt_version
    row["stamped_at_backfill"] = 1 if backfill else 0
    return row


def format_activity(row: dict) -> str:
    """One line per attempt, for the CLI."""
    frac = row.get("first_commit_frac")
    return (
        f"{row.get('attempt_id')}: streams={row.get('n_streams')} "
        f"tools={row.get('n_tool_calls')} bt={row.get('n_bt_calls')} "
        f"past={row.get('past_calls')} "
        f"probed={row.get('markets_probed')} seen={row.get('markets_seen')} "
        f"web={row.get('web_fetches')}/{row.get('web_searches')} "
        f"domains={row.get('n_domains')} code={row.get('code_runs')} "
        f"writes={row.get('files_written')} "
        f"commit_frac={'—' if frac is None else f'{frac:.2f}'} "
        f"bets={row.get('bets_proposed') if row.get('bets_proposed') is not None else '—'}"
    )
