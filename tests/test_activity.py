"""Attempt activity extraction (schema 008): what an attempt actually did.

The fixture below is a miniature of a real stream — the shapes that carry every field this
module extracts, in the order a session emits them. The assertions are deliberately exact:
this table exists to be queried a year from now by someone who was not here, so a field
that silently changes meaning is worse than one that is missing.
"""

import gzip
import json
from pathlib import Path

import pytest

from betting_agent.harness import activity as act
from betting_agent.harness.activity import (
    activity_row,
    code_stamp,
    extract_activity,
    format_activity,
    stream_paths,
)
from betting_agent.ledger.db import Ledger, LedgerError

T0 = "2026-08-15T03:00:00.000Z"
T1 = "2026-08-15T03:05:00.000Z"
T2 = "2026-08-15T03:30:00.000Z"


def _assistant(ts, *blocks):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": list(blocks)}}


def _tool(name, inputs):
    return {"type": "tool_use", "id": "tu1", "name": name, "input": inputs}


def _result(text):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu1", "content": text}]}}


def _stream_lines():
    """A handful of NDJSON records covering every extracted shape."""
    return [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-opus-5"},
        _assistant(T0, _tool("Bash", {
            "command": ("bt market KXHIGHTPHX-26AUG15-B104.5 && "
                        "bt ticket validate | head -5 && "
                        "python3 model.py KXLOWTOKC-26AUG13-B76.5"),
            "description": "probe two markets",
        })),
        _result("KXHIGHTPHX-26AUG15-B104.5 yes_ask 0.41\nKXSPACEX-26AUG15-T5 yes_ask 0.10"),
        _assistant(T1, _tool("WebFetch", {"url": "https://forecast.weather.gov/x",
                                          "prompt": "today's high"})),
        _result("the forecast page"),
        _assistant(T2, _tool("Write", {"file_path": "../ticket/bets.json",
                                       "content": "{}"})),
        _result("wrote"),
        {"type": "result", "subtype": "success", "is_error": False, "num_turns": 3},
    ]


@pytest.fixture
def attempt_dir(tmp_path):
    """An attempt directory with one stream and a full ticket."""
    d = tmp_path / "attempts" / "A-0207"
    (d / "logs").mkdir(parents=True)
    (d / "ticket").mkdir(parents=True)
    (d / "logs" / "session.stream.jsonl").write_text(
        "\n".join(json.dumps(line) for line in _stream_lines()) + "\n", encoding="utf-8"
    )
    (d / "ticket" / "bets.json").write_text(
        json.dumps({"attempt": "A-0207", "bets": [{"ticker": "KXHIGHTPHX-26AUG15-B104.5"}]}),
        encoding="utf-8",
    )
    (d / "ticket" / "edge_claim.md").write_text(
        "# Edge claim\n\nA-0120 found this first; A-0126 confirmed it. See A-0207 (me).\n"
        "Per playbook #17 and playbook entry 4, check depth before trusting a ladder.\n",
        encoding="utf-8",
    )
    (d / "ticket" / "hypothesis.md").write_text(
        "# Hypothesis\n\n## Kill criteria\n\nStop if any of these show up:\n\n"
        "- The station moves its sensor.\n"
        "  A wrapped continuation line that is not an item.\n"
        "- The forecast is stale.\n"
        "- Fees exceed the edge.\n\n"
        "## Sizing\n\n- not a kill criterion\n",
        encoding="utf-8",
    )
    return d


# --------------------------------------------------------------------------- extraction
def test_every_field_of_a_small_stream(attempt_dir):
    row = extract_activity(attempt_dir, wall_seconds=3600)

    assert row["n_streams"] == 1
    assert row["stream_bytes"] == (attempt_dir / "logs" / "session.stream.jsonl").stat().st_size

    # Tools, by name and in total.
    assert json.loads(row["tool_calls"]) == {"Bash": 1, "WebFetch": 1, "Write": 1}
    assert row["n_tool_calls"] == 3
    assert row["agent_calls"] == 0

    # bt subcommands, including the two-word group form.
    assert json.loads(row["bt_calls"]) == {"market": 1, "ticket validate": 1}
    assert row["n_bt_calls"] == 2

    # Typed tickers versus returned tickers: KXSPACEX came back, it was never asked for.
    assert row["markets_probed"] == 2
    assert row["series_probed"] == 2
    assert json.loads(row["series_list"]) == ["KXHIGHTPHX", "KXLOWTOKC"]
    assert row["markets_seen"] == 2

    assert (row["web_fetches"], row["web_searches"]) == (1, 0)
    assert json.loads(row["domains"]) == ["forecast.weather.gov"]
    assert row["n_domains"] == 1
    assert row["code_runs"] == 1            # the python3 model.py leg of the first command
    assert row["files_written"] == 1

    # Commitment: the Write to ../ticket/bets.json, 30 minutes into a 60-minute session.
    assert row["first_ticket_write_ts"] == T2
    assert row["first_commit_frac"] == 0.5

    # The ticket's own facts.
    assert row["cites_predecessor"] == 2    # A-0120 and A-0126; its own id does not count
    assert row["playbook_refs"] is None     # docs/22 section 2.1: the column stays, NULL
    assert row["kill_criteria_n"] == 3      # the Sizing section's bullet is not one
    assert row["ticket_chars"] > 0
    assert (row["passed"], row["bets_proposed"]) == (0, 1)

    # The code stamp is taken from the tree the extractor ran in.
    assert set(row) >= {"git_commit", "git_dirty", "config_sha"}


def test_a_ticket_with_no_bets_is_a_pass_and_no_ticket_at_all_is_neither(attempt_dir, tmp_path):
    (attempt_dir / "ticket" / "bets.json").write_text(
        json.dumps({"attempt": "A-0207", "bets": []}), encoding="utf-8"
    )
    assert extract_activity(attempt_dir, 3600)["passed"] == 1
    assert extract_activity(attempt_dir, 3600)["bets_proposed"] == 0

    bare = tmp_path / "attempts" / "A-0208"
    (bare / "logs").mkdir(parents=True)
    row = extract_activity(bare, 3600)
    assert (row["passed"], row["bets_proposed"]) == (None, None)
    assert (row["n_streams"], row["n_tool_calls"], row["ticket_chars"]) == (0, 0, 0)


def test_a_shell_redirect_into_the_ticket_counts_as_the_commit(tmp_path):
    d = tmp_path / "attempts" / "A-0209"
    (d / "logs").mkdir(parents=True)
    lines = [
        _assistant(T0, _tool("Bash", {"command": "bt board | head -20"})),
        _assistant(T1, _tool("Bash", {"command": "cat > ../ticket/bets.json <<'EOF'\n{}\nEOF"})),
    ]
    (d / "logs" / "session.stream.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8"
    )
    row = extract_activity(d, 600)
    assert row["first_ticket_write_ts"] == T1
    assert row["first_commit_frac"] == 0.5     # 300 s into 600 s of recorded compute
    assert row["files_written"] == 0           # a redirect is not a Write tool call


def test_a_commit_fraction_past_one_is_reported_not_clamped(tmp_path):
    """The wall clock can outrun the compute a session recorded (a slept host, the gaps
    between two-loop phases). That is the D9 signature, and hiding it behind a clamp would
    turn an anomaly into a measurement."""
    d = tmp_path / "attempts" / "A-0214"
    (d / "logs").mkdir(parents=True)
    lines = [
        _assistant(T0, _tool("Bash", {"command": "bt board"})),
        _assistant(T2, _tool("Write", {"file_path": "../ticket/bets.json", "content": "{}"})),
    ]
    (d / "logs" / "session.stream.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8"
    )
    assert extract_activity(d, 600)["first_commit_frac"] == 3.0    # 1800 s over 600 s


def test_a_stream_without_timestamps_scores_no_commit_fraction(tmp_path):
    """Every attempt through A-0076 has streams with no ``timestamp`` field at all. NULL
    there means "not instrumented", and must never read as zero."""
    d = tmp_path / "attempts" / "A-0060"
    (d / "logs").mkdir(parents=True)
    lines = [
        {"type": "assistant", "message": {"role": "assistant", "content": [
            _tool("Write", {"file_path": "/x/ticket/bets.json", "content": "{}"})]}},
    ]
    (d / "logs" / "session.stream.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8"
    )
    row = extract_activity(d, 1800)
    assert row["files_written"] == 1
    assert row["first_ticket_write_ts"] is None
    assert row["first_commit_frac"] is None


def test_no_wall_seconds_means_no_fraction(attempt_dir):
    assert extract_activity(attempt_dir, None)["first_commit_frac"] is None
    assert extract_activity(attempt_dir, 0)["first_commit_frac"] is None
    assert extract_activity(attempt_dir, None)["first_ticket_write_ts"] == T2


def test_junk_truncated_and_compressed_streams_are_all_readable(tmp_path):
    """These files are logs of processes that were sometimes killed mid-write."""
    d = tmp_path / "attempts" / "A-0210"
    (d / "logs").mkdir(parents=True)
    good = json.dumps(_assistant(T0, _tool("Bash", {"command": "bt fees"})))
    (d / "logs" / "session.stream.jsonl").write_text(
        f"not json\n{good}\n[1,2,3]\n{{\"type\": \"assist", encoding="utf-8"
    )
    with gzip.open(d / "logs" / "session-critic.stream.jsonl.gz", "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_assistant(T1, _tool("WebSearch", {"query": "weather"}))) + "\n")

    row = extract_activity(d, 3600)
    assert row["n_streams"] == 2
    assert json.loads(row["tool_calls"]) == {"Bash": 1, "WebSearch": 1}
    assert json.loads(row["bt_calls"]) == {"fees": 1}
    assert row["web_searches"] == 1


def test_only_the_attempts_own_streams_are_read(tmp_path):
    """A grading stream sits in the same directory and is not this attempt's activity."""
    d = tmp_path / "attempts" / "A-0211"
    (d / "logs").mkdir(parents=True)
    (d / "logs" / "session.stream.jsonl").write_text(
        json.dumps(_assistant(T0, _tool("Bash", {"command": "bt board"}))) + "\n",
        encoding="utf-8",
    )
    (d / "logs" / "grading.0.stream.jsonl").write_text(
        json.dumps(_assistant(T1, _tool("Bash", {"command": "bt market KXGRADE-1"}))) + "\n",
        encoding="utf-8",
    )
    assert [p.name for p in stream_paths(d)] == ["session.stream.jsonl"]
    assert json.loads(extract_activity(d, 60)["bt_calls"]) == {"board": 1}


def test_two_loop_streams_are_read_in_phase_order(tmp_path):
    d = tmp_path / "attempts" / "A-0212"
    (d / "logs").mkdir(parents=True)
    for phase in ("ideation", "critic", "implementation"):
        (d / "logs" / f"session-{phase}.stream.jsonl").write_text(
            json.dumps(_assistant(T0, _tool("Bash", {"command": f"bt market KX{phase.upper()}-1"})))
            + "\n", encoding="utf-8",
        )
    assert [p.name.split(".")[0] for p in stream_paths(d)] == [
        "session-ideation", "session-critic", "session-implementation"]
    row = extract_activity(d, 3600)
    assert row["n_streams"] == 3 and row["markets_probed"] == 3


def test_the_bt_vocabulary_still_matches_what_bt_registers():
    """The counts are only a clean vocabulary while this list matches the toolkit. A new
    ``bt`` command would otherwise land silently in the ``other`` bucket forever."""
    from betting_agent import bt

    registered = {c.name or c.callback.__name__ for c in bt.app.registered_commands}
    registered |= {g.name for g in bt.app.registered_groups}
    assert registered == set(act._BT_COMMANDS)
    for group in bt.app.registered_groups:
        subs = {c.name or c.callback.__name__ for c in group.typer_instance.registered_commands}
        assert subs == act._BT_GROUPS[group.name]


def test_reading_about_code_is_not_running_it(tmp_path):
    """``cat model.py`` is not computation; ``python3 model.py`` and ``./run.sh`` are."""
    d = tmp_path / "attempts" / "A-0215"
    (d / "logs").mkdir(parents=True)
    lines = [
        _assistant(T0, _tool("Bash", {"command": "cat model.py | head -40"})),
        _assistant(T0, _tool("Bash", {"command": "grep -n solve model.py"})),
        _assistant(T1, _tool("Bash", {"command": "bt book KXA-1 > b.json && python3 model.py"})),
        _assistant(T1, _tool("Bash", {"command": "./run.sh"})),
    ]
    (d / "logs" / "session.stream.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines) + "\n", encoding="utf-8"
    )
    assert extract_activity(d, 60)["code_runs"] == 2


def test_an_unknown_bt_subcommand_buckets_as_other(tmp_path):
    d = tmp_path / "attempts" / "A-0213"
    (d / "logs").mkdir(parents=True)
    command = "bt wibble && bt --json market KXA-1"
    (d / "logs" / "session.stream.jsonl").write_text(
        json.dumps(_assistant(T0, _tool("Bash", {"command": command}))) + "\n",
        encoding="utf-8",
    )
    assert json.loads(extract_activity(d, 60)["bt_calls"]) == {"other": 1, "market": 1}


def _bash_stream(tmp_path, attempt_id, command):
    d = tmp_path / "attempts" / attempt_id
    (d / "logs").mkdir(parents=True)
    (d / "logs" / "session.stream.jsonl").write_text(
        json.dumps(_assistant(T0, _tool("Bash", {"command": command}))) + "\n",
        encoding="utf-8",
    )
    return d


def test_one_bt_past_call_is_counted_by_its_subcommand(tmp_path):
    """docs/22 section 4.5: how much of the past an attempt read is its own column."""
    d = _bash_stream(tmp_path, "A-0214", "bt past family KXHORMUZWEEKLY")
    row = extract_activity(d, 60)
    assert row["past_calls"] == 1
    assert json.loads(row["past_subcommands"]) == {"family": 1}
    assert json.loads(row["bt_calls"]) == {"past family": 1}     # and in the blob as usual


def test_past_counts_follow_the_bt_vocabulary(tmp_path):
    """A bare ``bt past`` names no subcommand, so it buckets the way ``bt`` calls do."""
    d = _bash_stream(
        tmp_path, "A-0215",
        "bt past search strait && bt past family KXA && bt past && bt board",
    )
    row = extract_activity(d, 60)
    assert row["past_calls"] == 3
    assert json.loads(row["past_subcommands"]) == {"family": 1, "other": 1, "search": 1}
    assert row["n_bt_calls"] == 4                                # the board call too


def test_an_attempt_that_read_no_history_says_zero(tmp_path):
    d = _bash_stream(tmp_path, "A-0216", "bt board")
    row = extract_activity(d, 60)
    assert row["past_calls"] == 0 and row["past_subcommands"] == "{}"


def test_the_code_stamp_degrades_to_nulls_outside_a_repo(tmp_path):
    stamp = code_stamp(tmp_path / "nowhere")
    assert stamp == {"git_commit": None, "git_dirty": None, "config_sha": None}


def test_activity_row_adds_what_only_the_caller_knows(attempt_dir):
    row = activity_row(attempt_dir, "A-0207", 3600, prompt_version="p7", backfill=True)
    assert row["attempt_id"] == "A-0207"
    assert row["prompt_version"] == "p7"
    assert row["stamped_at_backfill"] == 1
    assert activity_row(attempt_dir, "A-0207", 3600)["stamped_at_backfill"] == 0
    assert "A-0207" in format_activity(row) and "probed=2" in format_activity(row)
    # docs/22 section 4.5: how much of the past an attempt read prints beside the bt count.
    assert "bt=2 past=0" in format_activity(row)
    assert "past=3" in format_activity(dict(row, past_calls=3))


# --------------------------------------------------------------------------- the ledger
@pytest.fixture
def ledger(tmp_path):
    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    _, aid = lg.create_attempt(
        env="prod", model="m", effort="high", memory_mode="on", prompt_version="p7",
        toolkit_version="0.1.0", workspace_path="/ws",
    )
    yield lg, aid
    lg.close()


def test_upsert_read_and_list(ledger, attempt_dir):
    lg, aid = ledger
    row = activity_row(attempt_dir, aid, 3600, prompt_version="p7")
    lg.upsert_activity(row)

    stored = lg.activity(aid)
    assert stored["attempt_id"] == aid
    assert stored["markets_probed"] == 2 and stored["n_bt_calls"] == 2
    assert stored["extracted_at"]                      # stamped by the DAO
    assert json.loads(stored["bt_calls"]) == {"market": 1, "ticket validate": 1}
    assert [r["attempt_id"] for r in lg.activity_rows()] == [aid]
    assert lg.activity("A-9999") is None


def test_upsert_replaces_rather_than_duplicating(ledger, attempt_dir):
    lg, aid = ledger
    lg.upsert_activity(activity_row(attempt_dir, aid, 3600))
    lg.upsert_activity({"attempt_id": aid, "markets_probed": 99,
                        "extracted_at": "2026-09-11T00:00:00Z"})
    rows = lg.activity_rows()
    assert len(rows) == 1
    assert rows[0]["markets_probed"] == 99
    assert rows[0]["extracted_at"] == "2026-09-11T00:00:00Z"
    assert rows[0]["n_bt_calls"] is None                # REPLACE writes a whole new row


def test_unknown_columns_and_a_missing_id_are_refused(ledger):
    lg, aid = ledger
    with pytest.raises(LedgerError, match="unknown activity columns"):
        lg.upsert_activity({"attempt_id": aid, "markets_prodded": 3})
    with pytest.raises(LedgerError, match="attempt_id"):
        lg.upsert_activity({"markets_probed": 3})


def test_the_row_is_keyed_to_a_real_attempt(ledger):
    lg, _aid = ledger
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        lg.upsert_activity({"attempt_id": "A-9999", "markets_probed": 1})


# --------------------------------------------------------------------------- the CLI
def test_activity_cli_records_and_skips(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from betting_agent import cli

    (tmp_path / "data").mkdir()
    lg = Ledger.open(tmp_path / "data" / "ledger.db")
    lg.migrate()
    _, aid = lg.create_attempt(
        env="prod", model="m", effort="high", memory_mode="on", prompt_version="p7",
        toolkit_version="0.1.0", workspace_path="/ws",
    )
    lg.close()
    logs = tmp_path / "data" / "attempts" / aid / "logs"
    logs.mkdir(parents=True)
    (logs / "session.stream.jsonl").write_text(
        json.dumps(_assistant(T0, _tool("Bash", {"command": "bt market KXA-1"}))) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("BT_ROOT", str(tmp_path))
    runner = CliRunner()

    bare = runner.invoke(cli.app, ["activity"])
    assert bare.exit_code == 2 and "--attempt" in bare.stdout

    first = runner.invoke(cli.app, ["activity", "--backfill"])
    assert first.exit_code == 0
    assert "recorded 1 attempt(s), skipped 0" in first.stdout
    assert f"{aid}: streams=1" in first.stdout

    again = runner.invoke(cli.app, ["activity", "--backfill"])
    assert "recorded 0 attempt(s), skipped 1" in again.stdout

    # docs/22 section 4.5: a row written before schema 009 says NULL for the `bt past`
    # counters, and every such attempt ran before `bt past` existed, so the honest
    # backfill value is zero rather than "not measured". Emptied here to stand in for a
    # pre-009 row, then filled by the same command.
    lg = Ledger.open(tmp_path / "data" / "ledger.db")
    lg.conn.execute("UPDATE attempt_activity SET past_calls=NULL, past_subcommands=NULL")
    lg.conn.commit()
    lg.close()
    filled = runner.invoke(cli.app, ["activity", "--backfill"])
    assert "filled zero `bt past` counts on 1 pre-009 row(s)" in filled.stdout
    quiet = runner.invoke(cli.app, ["activity", "--backfill"])
    assert "pre-009" not in quiet.stdout                # idempotent, and silent when done
    lg = Ledger.open(tmp_path / "data" / "ledger.db", readonly=True)
    row = lg.activity(aid)
    assert row["past_calls"] == 0 and row["past_subcommands"] == "{}"
    lg.close()

    forced = runner.invoke(cli.app, ["activity", "--attempt", aid, "--force"])
    assert forced.exit_code == 0 and "recorded 1 attempt(s)" in forced.stdout

    missing = runner.invoke(cli.app, ["activity", "--attempt", "A-9999"])
    assert missing.exit_code == 1 and "no such attempt" in missing.stdout

    lg = Ledger.open(tmp_path / "data" / "ledger.db", readonly=True)
    stored = lg.activity(aid)
    assert stored["markets_probed"] == 1 and stored["prompt_version"] == "p7"
    assert stored["stamped_at_backfill"] == 1          # the CLI always backfills
    lg.close()


def test_recording_never_fails_an_attempt(tmp_path, monkeypatch):
    """The hook's whole contract: a broken extractor costs a row and an audit event, never
    the attempt that just placed real orders."""
    from types import SimpleNamespace

    from betting_agent.harness import attempt as attempt_mod

    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    _, aid = lg.create_attempt(
        env="prod", model="m", effort="high", memory_mode="on", prompt_version="p7",
        toolkit_version="0.1.0", workspace_path="/ws",
    )
    monkeypatch.setattr(attempt_mod, "activity_row",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    settings = SimpleNamespace(attempts_dir=Path(tmp_path / "attempts"))

    attempt_mod.record_activity(lg, settings, aid, 100)      # must not raise

    assert lg.activity(aid) is None
    events = lg.audit_events(event="activity_record_error")
    assert len(events) == 1 and events[0]["attempt_id"] == aid
    lg.close()
