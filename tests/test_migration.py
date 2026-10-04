"""safe_migrate — the upgrade protocol (Jul29 spec L1), from v1 forward."""

import json
import shutil
import sqlite3
from decimal import Decimal as D
from pathlib import Path

import pytest

from betting_agent.ledger import db as db_mod
from betting_agent.ledger.db import (
    _SCHEMA_DIR,
    Ledger,
    LedgerError,
    _schema_expectations,
    _split_schema,
    safe_migrate,
)

# Derived, not literal (LG-6): "the current version" is whatever the schema dir says.
_SCHEMA_VERSION = _schema_expectations()[0]


def _build_v1(path: Path) -> None:
    """Construct a genuine version-1 ledger (000_init only) with seeded rows."""
    lg = Ledger.open(path)
    lg.conn.executescript((_SCHEMA_DIR / "000_init.sql").read_text())
    assert lg._user_version() == 1
    aid = _v1_attempt(lg)
    lg.transition(aid, "running")
    lg.transition(aid, "placed")
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
        side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
        status="settled", outcome="win", pnl=D("0.56"),
    )
    lg.transition(aid, "settled")
    # Raw SQL: nothing writes ``retrospectives`` any more (docs/22 section 4.6), and what
    # these tests need is the v1 row a migration has to carry forward.
    lg.conn.execute(
        "INSERT INTO retrospectives (attempt_id, created_at, grader_model, "
        "grader_session_id, hypothesis_grade, verdict, what_went_right, what_went_wrong, "
        "lessons, summary, retro_md) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (aid, "2026-07-13T15:00:00Z", "m", "sid", "confirmed", "right_for_stated_reason",
         "", "", "[]", "the summary", "the retro"),
    )
    lg.conn.execute(
        "INSERT INTO ledger_fts (attempt_id, kind, content) VALUES (?,?,?), (?,?,?)",
        (aid, "retro", "the retro", aid, "summary", "the summary"),
    )
    lg.conn.commit()
    lg.close()


def _v1_attempt(lg) -> str:
    """Insert a 'created' attempt with raw SQL, as a v1 ledger would have held one.

    Not ``create_attempt``: the DAO writes today's columns (``era`` and the cell columns
    arrived with 009), and the point of every test below is a row that predates them.
    """
    lg.conn.execute(
        "INSERT INTO attempts (attempt_id, created_at, status, env, model, effort, "
        "memory_mode, edge_class, prompt_version, toolkit_version, workspace_path) "
        "VALUES ('A-0001','2026-07-13T14:08:07Z','created','prod','claude-sonnet-5',"
        "'high','off','probability','p1','0.1.0','/ws')"
    )
    lg.conn.commit()
    return "A-0001"


def test_v1_to_current_safe_migrate(tmp_path):
    db = tmp_path / "ledger.db"
    _build_v1(db)
    report = safe_migrate(db, tmp_path / "backups")

    assert report["ok"] is True
    assert report["before"]["user_version"] == 1
    assert report["after"]["user_version"] == _SCHEMA_VERSION
    assert report["before"]["attempts"] == report["after"]["attempts"] == 1
    assert report["before"]["bets"] == report["after"]["bets"] == 1
    assert Path(report["backup_path"]).exists()
    assert all(report["checks"].values())

    lg = Ledger.open(db, readonly=True)
    # Legacy row reads back with the new columns defaulted.
    row = lg.get_attempt("A-0001")
    assert row["loop_mode"] == "one"
    assert row["priors_mode"] is None
    assert row["grader_blind"] is None
    # New tables exist and are queryable; FTS survived with content intact.
    for t in ("recipes", "sessions", "shadow_candidates", "shadow_bets",
              "deep_reviews", "reconciliations"):
        assert lg.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"] == 0
    hit = lg.conn.execute(
        "SELECT attempt_id FROM ledger_fts WHERE ledger_fts MATCH 'summary'"
    ).fetchall()
    assert {h["attempt_id"] for h in hit} == {"A-0001"}
    lg.close()


# ------------------------------------------------------------------ 002_compute_health
_CH_COLS = {"api_retries", "throttle_errors", "error_kinds"}


def _session_columns(lg) -> set[str]:
    return {r["name"] for r in lg.conn.execute("PRAGMA table_info(sessions)").fetchall()}


def test_compute_health_columns_exist_on_a_fresh_ledger(tmp_path):
    """The fresh-create path: 000 -> 001 -> 002 in one migrate() call."""
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == _SCHEMA_VERSION
    assert _CH_COLS <= _session_columns(lg)
    lg.close()


def test_a_v2_ledger_upgrades_and_keeps_its_sessions(tmp_path):
    """The upgrade path, which is the one the live ledger will actually take: a v2 file
    with real session rows gains three nullable columns and loses nothing."""
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db)
    for name in ("000_init.sql", "001_live.sql"):
        lg._apply_schema_file(_SCHEMA_DIR / name)
    assert lg._user_version() == 2
    # Raw SQL, not the DAO: ``finish_session`` writes the v3 columns, and the point of
    # this test is a row that predates them.
    lg.conn.execute(
        "INSERT INTO sessions (session_id, kind, model, started_at, ended_at, exit, "
        "num_turns, cost_usd, wall_seconds) VALUES (?,?,?,?,?,?,?,?,?)",
        ("s-old", "attempt", "claude-sonnet-5", "2026-07-30T10:00:00Z",
         "2026-07-30T11:00:00Z", "ok", 7, "1.2500", 3600),
    )
    lg.conn.commit()
    lg.close()

    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True
    assert report["before"]["user_version"] == 2
    assert report["after"]["user_version"] == _SCHEMA_VERSION

    lg = Ledger.open(db)
    assert _CH_COLS <= _session_columns(lg)
    row = lg.conn.execute("SELECT * FROM sessions WHERE session_id='s-old'").fetchone()
    assert row["cost_usd"] == "1.2500" and row["num_turns"] == 7   # nothing was lost
    assert row["api_retries"] is None                              # …and nothing invented
    # The new columns are writable on the upgraded file, not merely present.
    lg.finish_session("s-old", ended_at="2026-07-30T11:00:00Z", exit="ok",
                      api_retries=3, throttle_errors=1)
    assert lg.conn.execute(
        "SELECT api_retries FROM sessions WHERE session_id='s-old'"
    ).fetchone()["api_retries"] == 3
    lg.close()


def test_the_upgrade_is_a_no_op_the_second_time(tmp_path):
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db)
    lg.migrate()
    lg.insert_session("s", "attempt", "m", "2026-07-30T10:00:00Z")
    lg.finish_session("s", ended_at="2026-07-30T11:00:00Z", exit="ok", api_retries=2)
    assert lg.migrate() == _SCHEMA_VERSION
    assert lg.conn.execute(
        "SELECT api_retries FROM sessions WHERE session_id='s'"
    ).fetchone()["api_retries"] == 2
    lg.close()


def test_safe_migrate_is_rerunnable(tmp_path):
    db = tmp_path / "ledger.db"
    _build_v1(db)
    safe_migrate(db, tmp_path / "backups")
    report2 = safe_migrate(db, tmp_path / "backups")  # no-op second pass
    assert report2["ok"] is True
    assert report2["before"]["user_version"] == _SCHEMA_VERSION
    assert report2["after"]["user_version"] == _SCHEMA_VERSION


def test_safe_migrate_failure_names_backup(tmp_path, monkeypatch):
    # LG-10: this used to monkeypatch ``migrate`` into a no-op, which proved only that
    # the verifier notices nothing happened. A *derived* expectation the ledger cannot
    # satisfy (LG-6) is the real shape of the failure this guards: the schema promises
    # something the migrated file does not have.
    db = tmp_path / "ledger.db"
    _build_v1(db)
    monkeypatch.setattr(db_mod, "_schema_expectations", lambda: (99, frozenset({"nope"})))
    with pytest.raises(LedgerError) as ei:
        safe_migrate(db, tmp_path / "backups")
    assert "backup at" in str(ei.value)
    assert "user_version" in str(ei.value)
    backups = list((tmp_path / "backups").glob("ledger-*.db"))
    assert len(backups) == 1 and backups[0].exists()   # the backup is real, not a name


# ------------------------------------------------------------------ 003 verdict grid (D4)
def _insertable(lg, aid, *, grade, verdict) -> bool:
    """Does the `retrospectives` CHECK accept this (grade, verdict) pair?"""
    try:
        lg.conn.execute(
            "INSERT INTO retrospectives (attempt_id, created_at, grader_model, "
            "hypothesis_grade, verdict, summary, retro_md) VALUES (?,?,?,?,?,?,?)",
            (aid, "2026-08-10T00:00:00Z", "m", grade, verdict, "s", "r"),
        )
    except sqlite3.IntegrityError:
        return False
    lg.conn.execute("DELETE FROM retrospectives WHERE attempt_id=?", (aid,))
    return True


def test_003_widens_the_enums_on_a_fresh_ledger(tmp_path):
    """Fresh-create path: the rebuilt table ships with D4's grade and verdict, and the
    CHECK still refuses everything else — a widened enum is not an open one."""
    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    _seq, aid = lg.create_attempt(
        env="prod", model="m", effort="high", memory_mode="off", prompt_version="p1",
        toolkit_version="0.1.0", workspace_path="/ws",
    )

    assert _insertable(lg, aid, grade="variance_consistent", verdict="unlucky")
    assert _insertable(lg, aid, grade="mixed", verdict="mixed_loss")
    assert _insertable(lg, aid, grade="confirmed", verdict="right_for_stated_reason")
    assert not _insertable(lg, aid, grade="made_up", verdict="unlucky")
    assert not _insertable(lg, aid, grade="mixed", verdict="sort_of_wrong")
    lg.close()


def test_003_upgrade_preserves_every_retrospective_and_its_fts(tmp_path):
    """Upgrade path: the table is REBUILT (SQLite cannot ALTER a CHECK), so the rows,
    their FTS entries, and the foreign key all have to come through untouched."""
    db = tmp_path / "ledger.db"
    _build_v1(db)                       # one attempt, one settled bet, one retrospective
    lg = Ledger.open(db, check_version=False)   # a v1 file, on its way forward
    before = lg.conn.execute("SELECT * FROM retrospectives").fetchall()
    assert len(before) == 1

    lg.migrate()

    assert lg.conn.execute("SELECT * FROM retrospectives").fetchall() == before
    assert lg.conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE name='retrospectives_pre_d4'"
    ).fetchone()["n"] == 0                                  # no scaffolding left behind
    hit = lg.conn.execute(
        "SELECT attempt_id FROM ledger_fts WHERE ledger_fts MATCH 'summary'"
    ).fetchall()
    assert {h["attempt_id"] for h in hit} == {"A-0001"}     # FTS survived the rebuild
    # The FK to `attempts` came through with the rebuild, not just the columns.
    assert lg.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with pytest.raises(sqlite3.IntegrityError):     # the FK still bites on a stranger
        lg.conn.execute(
            "INSERT INTO retrospectives (attempt_id, created_at, grader_model, "
            "hypothesis_grade, verdict, summary, retro_md) "
            "VALUES ('A-9999','t','m','mixed','mixed_loss','s','r')"
        )
    # And the migrated table accepts the widened enums.
    lg.conn.execute("DELETE FROM retrospectives WHERE attempt_id='A-0001'")
    assert _insertable(lg, "A-0001", grade="variance_consistent", verdict="unlucky")
    assert _insertable(lg, "A-0001", grade="mixed", verdict="mixed_loss")
    assert not _insertable(lg, "A-0001", grade="mixed", verdict="sort_of_wrong")
    lg.close()


def test_003_upgrade_leaves_historical_verdicts_alone(tmp_path):
    """A migration relabels nothing. The re-derivation is a separate audited pass."""
    db = tmp_path / "ledger.db"
    _build_v1(db)
    lg = Ledger.open(db, check_version=False)   # a v1 file, on its way forward
    lg.conn.execute(
        "UPDATE retrospectives SET hypothesis_grade='mixed', verdict='wrong'"
    )
    lg.migrate()
    row = lg.conn.execute("SELECT hypothesis_grade, verdict FROM retrospectives").fetchone()
    assert row == {"hypothesis_grade": "mixed", "verdict": "wrong"}
    lg.close()


# ------------------------------------------------------------------ 004 no-fill scoring (D12)
_D12_COLS = {"hypothetical_outcome", "hypothetical_pnl", "hypothetical_scored_at",
             "declared_contracts"}


def _bet_columns(lg) -> set[str]:
    return {r["name"] for r in lg.conn.execute("PRAGMA table_info(bets)").fetchall()}


def test_004_adds_the_hypothetical_columns_on_a_fresh_ledger(tmp_path):
    """Fresh-create path: 000 -> 004 in one migrate() call, and the new outcome column
    carries its CHECK rather than accepting any string that turns up."""
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == _SCHEMA_VERSION
    assert _D12_COLS <= _bet_columns(lg)

    _seq, aid = lg.create_attempt(
        env="prod", model="m", effort="high", memory_mode="off", prompt_version="p1",
        toolkit_version="0.1.0", workspace_path="/ws",
    )
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1", side="yes",
        limit_price=D("0.40"), model_prob=D("0.55"), rationale="r", status="no_fill",
        contracts=D("1"),
    )
    lg.score_nofill_bet(f"{aid}-B01", outcome="win", hypothetical_pnl=D("0.5832"),
                        scored_at="2026-08-10T00:00:00Z")
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (f"{aid}-B01",)).fetchone()
    assert row["hypothetical_outcome"] == "win"
    assert row["hypothetical_pnl"] == "0.5832"          # 4dp TEXT, like every money column
    assert (row["status"], row["outcome"], row["pnl"]) == ("no_fill", None, None)
    with pytest.raises(sqlite3.IntegrityError):         # widened, not opened
        lg.conn.execute("UPDATE bets SET hypothetical_outcome='sideways'")
    lg.close()


def test_a_v4_ledger_upgrades_and_its_bets_keep_every_figure(tmp_path):
    """The upgrade path the live ledger takes: four nullable columns arrive, the money on
    the existing rows is untouched, and nothing is invented for rows that predate them."""
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db)
    for name in ("000_init.sql", "001_live.sql", "002_compute_health.sql",
                 "003_verdict_grid.sql"):
        lg._apply_schema_file(_SCHEMA_DIR / name)
    assert lg._user_version() == 4
    aid = _v1_attempt(lg)
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1", side="yes",
        limit_price=D("0.40"), model_prob=D("0.55"), rationale="r", status="settled",
        is_real=1, contracts=D("1"), fill_price=D("0.40"), stake=D("0.40"),
        fee=D("0.0168"), outcome="win", pnl=D("0.5832"),
    )
    lg.insert_bet(
        bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T2", side="no",
        limit_price=D("0.55"), model_prob=D("0.70"), rationale="r", status="no_fill",
        is_real=1, contracts=D("1"),
    )
    lg.close()

    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True
    assert report["before"]["user_version"] == 4
    assert report["after"]["user_version"] == _SCHEMA_VERSION
    assert report["before"]["bets"] == report["after"]["bets"] == 2

    lg = Ledger.open(db)
    assert _D12_COLS <= _bet_columns(lg)
    settled = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (f"{aid}-B01",)).fetchone()
    assert (settled["pnl"], settled["fee"], settled["stake"]) == ("0.5832", "0.0168", "0.4000")
    assert settled["hypothetical_pnl"] is None          # nothing invented
    assert settled["declared_contracts"] is None
    # The pre-existing no-fill is now findable and scorable on the upgraded file — the
    # whole point of the migration is that the backlog gets scored, not just new rows.
    assert [b["bet_id"] for b in lg.unscored_nofill_bets()] == [f"{aid}-B02"]
    lg.score_nofill_bet(f"{aid}-B02", outcome="loss", hypothetical_pnl=D("-0.5674"),
                        scored_at="2026-08-10T00:00:00Z")
    assert lg.unscored_nofill_bets() == []
    lg.close()


def test_scoring_a_nofill_twice_is_refused(tmp_path):
    """``score_nofill_bet`` is the only writer of these columns and it will not overwrite a
    score: the guard is in the WHERE clause, not in the caller's memory."""
    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    _seq, aid = lg.create_attempt(
        env="prod", model="m", effort="high", memory_mode="off", prompt_version="p1",
        toolkit_version="0.1.0", workspace_path="/ws",
    )
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1", side="yes",
        limit_price=D("0.40"), model_prob=D("0.55"), rationale="r", status="no_fill",
        contracts=D("1"),
    )
    lg.score_nofill_bet(f"{aid}-B01", outcome="win", hypothetical_pnl=D("0.5832"),
                        scored_at="2026-08-10T00:00:00Z")
    with pytest.raises(LedgerError):
        lg.score_nofill_bet(f"{aid}-B01", outcome="loss", hypothetical_pnl=D("-0.4168"),
                            scored_at="2026-08-11T00:00:00Z")
    with pytest.raises(LedgerError):                    # and never on a filled bet
        lg.score_nofill_bet("A-9999-B01", outcome="win", hypothetical_pnl=D("1"),
                            scored_at="2026-08-11T00:00:00Z")
    row = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (f"{aid}-B01",)).fetchone()
    assert (row["hypothetical_outcome"], row["hypothetical_pnl"]) == ("win", "0.5832")

    # …and the generic UPDATE path cannot reach the columns at all, which is what makes
    # ``score_nofill_bet`` the single writer rather than merely the intended one.
    for col in ("hypothetical_outcome", "hypothetical_pnl", "hypothetical_scored_at",
                "declared_contracts"):
        with pytest.raises(LedgerError, match="non-writable"):
            lg.update_bet(f"{aid}-B01", **{col: "loss" if "outcome" in col else D("9")})
    lg.close()


# ------------------------------------------------------------------ LG-1 / LG-10 atomicity
def _schema_dir_with_broken_second_file(tmp_path) -> Path:
    """A schema dir whose 001 file creates a table and then fails, version bump last."""
    d = tmp_path / "schema"
    d.mkdir()
    shutil.copy(_SCHEMA_DIR / "000_init.sql", d / "000_init.sql")
    (d / "001_broken.sql").write_text(
        "-- a migration that dies halfway, exactly like a disk-full or a typo would\n"
        "CREATE TABLE half_migrated (a INTEGER);\n"
        "ALTER TABLE attempts ADD COLUMN new_column TEXT;\n"
        "INSERT INTO table_that_does_not_exist (x) VALUES (1);\n"
        "PRAGMA user_version = 2;\n"
    )
    return d


def test_interrupted_migration_leaves_version_and_schema_untouched(tmp_path, monkeypatch):
    """LG-1 (reproduced in review): under ``executescript`` every DDL statement committed
    on its own, so a mid-file failure left half a schema carrying the OLD user_version —
    and the re-run then died on "table already exists". A bricked ledger, by luck of
    where the file stopped. One BEGIN IMMEDIATE per file makes the failure a no-op."""
    db = tmp_path / "ledger.db"
    _build_v1(db)
    monkeypatch.setattr(db_mod, "_SCHEMA_DIR", _schema_dir_with_broken_second_file(tmp_path))

    lg = Ledger.open(db, check_version=False)
    with pytest.raises(sqlite3.OperationalError):
        lg.migrate()

    assert lg._user_version() == 1                       # the version never moved
    tables = {r["name"] for r in lg.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "half_migrated" not in tables                 # nor did the half-built schema
    cols = {r["name"] for r in lg.conn.execute("PRAGMA table_info(attempts)").fetchall()}
    assert "new_column" not in cols
    assert lg.get_attempt("A-0001")["status"] == "settled"   # the data is untouched
    lg.close()


def test_a_failed_migration_is_re_runnable_once_the_file_is_fixed(tmp_path, monkeypatch):
    """The half-migrated state is what made a retry impossible. Prove the retry works."""
    db = tmp_path / "ledger.db"
    _build_v1(db)
    broken = _schema_dir_with_broken_second_file(tmp_path)
    monkeypatch.setattr(db_mod, "_SCHEMA_DIR", broken)
    lg = Ledger.open(db, check_version=False)
    with pytest.raises(sqlite3.OperationalError):
        lg.migrate()
    lg.close()

    # The operator fixes the file and runs it again — no hand surgery in between.
    (broken / "001_broken.sql").write_text(
        "CREATE TABLE half_migrated (a INTEGER);\n"
        "ALTER TABLE attempts ADD COLUMN new_column TEXT;\n"
        "PRAGMA user_version = 2;\n"
    )
    lg = Ledger.open(db, check_version=False)
    assert lg.migrate() == 2
    cols = {r["name"] for r in lg.conn.execute("PRAGMA table_info(attempts)").fetchall()}
    assert "new_column" in cols
    lg.close()


def test_user_version_is_applied_last_whatever_the_file_order(tmp_path, monkeypatch):
    """The version is the claim that the tables landed, so it is written after them —
    even when a file puts the pragma first."""
    d = tmp_path / "schema"
    d.mkdir()
    (d / "000_init.sql").write_text(
        "PRAGMA user_version = 1;\nCREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);\n"
        "INSERT INTO meta (key, value) SELECT 'v', CAST(x AS TEXT) FROM "
        "(SELECT 1 AS x) WHERE (SELECT COUNT(*) FROM sqlite_master WHERE name='meta') = 1;\n"
    )
    monkeypatch.setattr(db_mod, "_SCHEMA_DIR", d)
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == 1
    assert lg.meta_get("v") == "1"      # the CREATE ran before the pragma, not after
    lg.close()


def test_split_schema_keeps_multi_line_statements_whole():
    body, versions = _split_schema(
        "-- leading comment\n"
        "CREATE TABLE t (\n  a INTEGER,\n  b TEXT DEFAULT 'x; y'\n);\n"
        "PRAGMA user_version = 3;\n"
        "-- trailing comment\n"
    )
    assert body[0].startswith("-- leading comment") and body[0].endswith(");")
    assert "'x; y'" in body[0]           # the semicolon inside the literal did not split
    assert versions == ["PRAGMA user_version = 3;"]
    # A trailing comment rides along as its own fragment; sqlite executes it as a no-op
    # rather than this splitter deciding what counts as "real" SQL.
    assert body[1:] == ["-- trailing comment"]
    conn = sqlite3.connect(":memory:")
    conn.execute(body[1])                # proves the no-op claim above
    conn.close()



# ------------------------------------------------------------------ 005 sleep_stretched (D9)
def _attempt_columns(lg) -> set[str]:
    return {r["name"] for r in lg.conn.execute("PRAGMA table_info(attempts)").fetchall()}


def test_sleep_stretched_column_exists_on_a_fresh_ledger(tmp_path):
    """Fresh-create path: 000 -> ... -> 004 in one migrate() call."""
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == _SCHEMA_VERSION
    assert "sleep_stretched" in _attempt_columns(lg)
    lg.close()


def test_a_pre_004_ledger_upgrades_and_keeps_its_attempts(tmp_path):
    """Upgrade path: a ledger built before 004 gains the nullable column and loses
    nothing recorded on its existing attempt."""
    db = tmp_path / "ledger.db"
    _build_v1(db)                       # one attempt, one settled bet, one retrospective
    lg = Ledger.open(db, check_version=False)   # a v1 file, on its way forward
    before = lg.get_attempt("A-0001")
    lg.close()

    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True
    assert report["before"]["user_version"] == 1
    assert report["after"]["user_version"] == _SCHEMA_VERSION

    lg = Ledger.open(db)
    assert "sleep_stretched" in _attempt_columns(lg)
    after = lg.get_attempt("A-0001")
    assert after["sleep_stretched"] is None             # additive; nothing invented
    for key in before:
        if key != "sleep_stretched":
            assert after[key] == before[key]             # nothing else moved
    # The new column is writable on the upgraded file, not merely present.
    lg.update_attempt_fields("A-0001", sleep_stretched=True)
    assert lg.get_attempt("A-0001")["sleep_stretched"] == 1
    lg.close()


# ------------------------------------------------------------------ LG-1 / LG-10 atomicity



# ------------------------------------------------------------------ 006 correlation (D7)
def _bets_columns(lg) -> set[str]:
    return {r["name"] for r in lg.conn.execute("PRAGMA table_info(bets)").fetchall()}


def test_006_resolution_event_column_exists_on_a_fresh_ledger(tmp_path):
    """Fresh-create path: 000 -> 001 -> 002 -> 003 -> 005 in one migrate() call. 004 is a
    sibling's file and is not expected to exist in this worktree (docs/14 D7 note)."""
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == _SCHEMA_VERSION
    assert "resolution_event" in _bets_columns(lg)
    lg.close()


def test_006_upgrade_keeps_old_bets_and_makes_the_column_writable(tmp_path):
    """The upgrade path: a ledger at 003 (the state right after Wave 1, before either
    004 or 005) gains the nullable column and loses nothing already written."""
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db)
    for name in ("000_init.sql", "001_live.sql", "002_compute_health.sql",
                 "003_verdict_grid.sql"):
        lg._apply_schema_file(_SCHEMA_DIR / name)
    assert lg._user_version() == 4
    aid = _v1_attempt(lg)
    # Pre-migration bet row, written the way every ticket before this migration looked —
    # no resolution_event column exists yet to pass.
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1",
        side="yes", limit_price=D("0.40"), model_prob=D("0.50"), rationale="r",
        status="settled", outcome="win", pnl=D("0.56"),
    )
    lg.close()

    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True
    assert report["before"]["user_version"] == 4
    assert report["after"]["user_version"] == _SCHEMA_VERSION
    assert report["before"]["bets"] == report["after"]["bets"] == 1

    lg = Ledger.open(db)
    assert "resolution_event" in _bets_columns(lg)
    old = lg.conn.execute("SELECT resolution_event FROM bets WHERE bet_id=?",
                          (f"{aid}-B01",)).fetchone()
    assert old["resolution_event"] is None       # nothing invented for the old row
    # The column is writable on the upgraded file, not merely present.
    lg.insert_bet(
        bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T2",
        side="yes", limit_price=D("0.30"), model_prob=D("0.60"), rationale="r",
        status="filled", resolution_event="OWGR-2026-08-03",
    )
    new = lg.conn.execute("SELECT resolution_event FROM bets WHERE bet_id=?",
                          (f"{aid}-B02",)).fetchone()
    assert new["resolution_event"] == "OWGR-2026-08-03"
    lg.close()


# ------------------------------------------------------- 007: bets.outcome learns 'scalar'
def _bets_ddl(lg) -> str:
    return lg.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='bets'"
    ).fetchone()["sql"]


def test_007_admits_a_scalar_outcome_on_a_fresh_ledger(tmp_path):
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == _SCHEMA_VERSION
    _seq, aid = lg.create_attempt(
        env="prod", model="m", effort="high", memory_mode="off", prompt_version="p1",
        toolkit_version="0.1.0", workspace_path="/ws",
    )
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1", side="no",
        limit_price=D("0.75"), model_prob=D("0.50"), rationale="r", status="settled",
        outcome="scalar", pnl=D("0.0568"),
    )
    assert lg.conn.execute("SELECT outcome FROM bets").fetchone()["outcome"] == "scalar"
    # ...and the widening is exactly one word wide: nothing else gets in.
    with pytest.raises(sqlite3.IntegrityError):
        lg.insert_bet(
            bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T2", side="no",
            limit_price=D("0.75"), model_prob=D("0.50"), rationale="r", status="settled",
            outcome="partial",
        )
    # The counterfactual column is deliberately NOT widened: a no-fill held no position,
    # so no exchange revenue exists to read a scalar value out of (see 007's header).
    assert "hypothetical_outcome IN ('win','loss','void')" in " ".join(_bets_ddl(lg).split())
    lg.close()


def test_007_upgrade_copies_every_bet_verbatim_and_keeps_the_unique_index(tmp_path):
    """The rebuild path. Every column added by 004/006 has to survive it, the partial
    unique index has to come back, and no historical outcome may be re-derived."""
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db)
    for name in ("000_init.sql", "001_live.sql", "002_compute_health.sql",
                 "003_verdict_grid.sql", "004_nofill_scoring.sql",
                 "005_sleep_stretched.sql", "006_correlation.sql"):
        lg._apply_schema_file(_SCHEMA_DIR / name)
    assert lg._user_version() == 7
    aid = _v1_attempt(lg)
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1", side="no",
        market_title="a title", category="Sports", limit_price=D("0.75"),
        model_prob=D("0.50"), rationale="r", is_real=1, status="voided", outcome="void",
        contracts=1, fill_price=D("0.7500"), stake=D("0.7500"), fee=D("0.0000"),
        order_id="OID-1", client_order_id=f"{aid}-B01", book_snapshot="{}",
        close_ts="2026-08-13T05:00:00Z", expected_resolution_ts="2026-08-13T09:00:00Z",
        placed_at="2026-08-12T13:05:53Z", pnl=D("0.0000"),
        resolution_event="NPB-2026-08-13", declared_contracts=1,
    )
    lg.update_bet(f"{aid}-B01", settled_at="2026-08-15T14:48:49Z")
    lg.insert_bet(
        bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T2", side="yes",
        limit_price=D("0.30"), model_prob=D("0.60"), rationale="r", status="no_fill",
        contracts=1,
    )
    lg.score_nofill_bet(f"{aid}-B02", outcome="loss", hypothetical_pnl=D("-0.3018"),
                        scored_at="2026-08-15T14:48:49Z")
    before = [dict(r) for r in lg.conn.execute("SELECT * FROM bets ORDER BY bet_id")]
    lg.close()

    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True
    assert report["before"]["user_version"] == 7
    # 007 lands, and so does every migration added after it: this test is about what the
    # rebuild preserves, not about 007 being the newest file.
    assert report["after"]["user_version"] == _SCHEMA_VERSION >= 8
    assert report["before"]["bets"] == report["after"]["bets"] == 2

    lg = Ledger.open(db)
    after = [dict(r) for r in lg.conn.execute("SELECT * FROM bets ORDER BY bet_id")]
    # Every column the rows HAD, verbatim. Columns added by later migrations (009's
    # ``reject_reason``) are compared separately, below: the claim here is that the 007
    # rebuild preserved what it copied, not that nothing has been added since.
    assert [{k: r[k] for k in before[0]} for r in after] == before
    assert [r["reject_reason"] for r in after] == [None, None]   # nothing invented
    assert after[0]["outcome"] == "void"         # nothing re-derived by the migration
    assert after[1]["hypothetical_outcome"] == "loss"
    assert after[1]["hypothetical_pnl"] == "-0.3018"

    # The partial unique index survives the table rebuild...
    idx = lg.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='bets_attempt_ticker'"
    ).fetchone()
    assert idx is not None and "WHERE status != 'rejected'" in idx["sql"]
    with pytest.raises(sqlite3.IntegrityError):
        lg.insert_bet(
            bet_id=f"{aid}-B03", attempt_id=aid, ticket_index=3, ticker="T1", side="yes",
            limit_price=D("0.20"), model_prob=D("0.30"), rationale="r", status="filled",
        )
    # ...and so does the UNIQUE on client_order_id (it comes back with the CREATE).
    with pytest.raises(sqlite3.IntegrityError):
        lg.insert_bet(
            bet_id=f"{aid}-B04", attempt_id=aid, ticket_index=4, ticker="T4", side="yes",
            limit_price=D("0.20"), model_prob=D("0.30"), rationale="r", status="filled",
            client_order_id=f"{aid}-B01",
        )
    # And the correction the migration exists for is now writable on the upgraded file.
    lg.update_bet(f"{aid}-B01", status="settled", outcome="scalar", pnl=D("0.0568"),
                  fee=D("0.0132"))
    fixed = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?",
                            (f"{aid}-B01",)).fetchone()
    assert fixed["outcome"] == "scalar" and fixed["pnl"] == "0.0568"
    assert fixed["settled_at"] == "2026-08-15T14:48:49Z"
    lg.close()


def test_007_leaves_a_fresh_and_an_upgraded_bets_table_identical(tmp_path):
    """The rebuild folds 004's and 006's ALTERs into its CREATE, so the two paths must not
    drift apart — a schema that depends on how you got there is one nobody can read."""
    upgraded = tmp_path / "upgraded.db"
    lg = Ledger.open(upgraded)
    # Everything up to and including 006, by number rather than by "all but the last file":
    # the pre-007 state is a fixed point in this test, not a moving one.
    for path in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
        if int(path.name.split("_", 1)[0]) <= 6:
            lg._apply_schema_file(path)
    assert lg._user_version() == 7
    lg.migrate()
    upgraded_cols = [tuple(dict(c).values())
                     for c in lg.conn.execute("PRAGMA table_info(bets)")]
    lg.close()

    fresh = Ledger.open(tmp_path / "fresh.db")
    fresh.migrate()
    fresh_cols = [tuple(dict(c).values())
                  for c in fresh.conn.execute("PRAGMA table_info(bets)")]
    assert upgraded_cols == fresh_cols
    fresh.close()


# ------------------------------------------------------------------ LG-1 / LG-10 atomicity


# ------------------------------------------------------------------ LG-6 derived expectations
def test_expectations_come_from_the_schema_dir_not_a_hardcoded_two():
    # The one place a literal is the point: it proves the derivation really reads the
    # files. Bump it with each new schema file (today: 010_credits.sql -> 11).
    version, tables = _schema_expectations()
    assert version == 11
    assert {"attempts", "bets", "meta", "ledger_fts", "recipes", "sessions",
            "shadow_candidates", "shadow_bets", "deep_reviews",
            "reconciliations", "attempt_activity", "cell_plans", "director_runs",
            "attempt_reviews", "cohort_reviews", "personal_orders", "credits"} <= tables


def test_a_future_migration_moves_the_expectation_with_it(tmp_path, monkeypatch):
    """The hardcoded ``== 2`` made the first 002 file report failure AFTER succeeding."""
    d = tmp_path / "schema"
    d.mkdir()
    for name in ("000_init.sql", "001_live.sql"):
        shutil.copy(_SCHEMA_DIR / name, d / name)
    (d / "002_next.sql").write_text(
        "CREATE TABLE future_thing (a INTEGER);\nPRAGMA user_version = 3;\n"
    )
    monkeypatch.setattr(db_mod, "_SCHEMA_DIR", d)

    version, tables = _schema_expectations()
    assert version == 3 and "future_thing" in tables

    db = tmp_path / "ledger.db"
    _build_v1(db)
    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True and report["after"]["user_version"] == 3


# ------------------------------------------------------------------ 009 the rebuild
_ATTEMPT_009 = {"cell", "cell_effective", "cell_forced", "era", "example_ids",
                "direction_hash", "session_summary"}
_NEW_TABLES_009 = ("cell_plans", "director_runs", "attempt_reviews", "cohort_reviews",
                   "personal_orders")


def _kind_accepted(lg, session_id, kind) -> bool:
    """Does the ``sessions`` CHECK accept this kind?"""
    try:
        lg.conn.execute(
            "INSERT INTO sessions (session_id, kind, model, started_at) VALUES (?,?,?,?)",
            (session_id, kind, "m", "2026-09-13T00:00:00Z"),
        )
    except sqlite3.IntegrityError:
        return False
    return True


def test_009_lands_every_column_and_table_on_a_fresh_ledger(tmp_path):
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == _SCHEMA_VERSION
    assert _ATTEMPT_009 <= _attempt_columns(lg)
    assert "reject_reason" in _bet_columns(lg)
    activity_cols = {r["name"] for r in lg.conn.execute("PRAGMA table_info(attempt_activity)")}
    assert {"past_calls", "past_subcommands"} <= activity_cols
    for table in _NEW_TABLES_009:
        assert lg.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"] == 0
    lg.close()


def test_009_makes_model_prob_nullable_and_keeps_every_other_check(tmp_path):
    """Section 4.2: the column stays and stops being written, so NOT NULL has to go. It is
    the reason ``bets`` is rebuilt here, and the rebuild must not quietly relax anything
    else on the way through."""
    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    aid = _v1_attempt(lg)
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1", side="yes",
        limit_price=D("0.40"), rationale="r", status="filled", contracts=D("1"),
    )
    row = lg.conn.execute("SELECT * FROM bets").fetchone()
    assert row["model_prob"] is None and row["reject_reason"] is None

    # …and every other CHECK still bites exactly as it did before the rebuild.
    for col, bad in (("side", "sideways"), ("status", "pending"), ("outcome", "partial"),
                     ("hypothetical_outcome", "scalar")):
        with pytest.raises(sqlite3.IntegrityError):
            lg.conn.execute(f"UPDATE bets SET {col}=?", (bad,))
    assert "limit_price      TEXT NOT NULL" in _bets_ddl(lg)   # still required
    lg.close()


def test_a_null_model_prob_is_refused_before_009(tmp_path):
    """The control: the constraint really was there, so the rebuild really is the fix."""
    lg = Ledger.open(tmp_path / "ledger.db", check_version=False)
    for path in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
        if int(path.name.split("_", 1)[0]) <= 8:
            lg._apply_schema_file(path)
    aid = _v1_attempt(lg)
    with pytest.raises(sqlite3.IntegrityError):
        lg.insert_bet(
            bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1", side="yes",
            limit_price=D("0.40"), rationale="r", status="filled", contracts=D("1"),
        )
    lg.close()


def test_009_upgrade_copies_every_bet_verbatim_and_keeps_the_unique_index(tmp_path):
    """The rebuild path. Rows written under the old constraint come through untouched, the
    partial unique index and the coid UNIQUE come back, and the new columns are writable."""
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db, check_version=False)
    for path in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
        if int(path.name.split("_", 1)[0]) <= 8:
            lg._apply_schema_file(path)
    assert lg._user_version() == 9
    aid = _v1_attempt(lg)
    lg.insert_bet(
        bet_id=f"{aid}-B01", attempt_id=aid, ticket_index=1, ticker="T1", side="no",
        market_title="a title", category="Sports", limit_price=D("0.75"),
        model_prob=D("0.50"), rationale="r", is_real=1, status="settled", outcome="win",
        contracts=1, fill_price=D("0.7500"), stake=D("0.7500"), fee=D("0.0132"),
        order_id="OID-1", client_order_id=f"{aid}-B01", book_snapshot="{}",
        close_ts="2026-08-13T05:00:00Z", expected_resolution_ts="2026-08-13T09:00:00Z",
        placed_at="2026-08-12T13:05:53Z", settled_at="2026-08-15T14:48:49Z",
        pnl="0.2368", resolution_event="NPB-2026-08-13", declared_contracts=1,
    )
    before = [dict(r) for r in lg.conn.execute("SELECT * FROM bets ORDER BY bet_id")]
    lg.close()

    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True
    assert report["before"]["bets"] == report["after"]["bets"] == 1

    lg = Ledger.open(db)
    after = [dict(r) for r in lg.conn.execute("SELECT * FROM bets ORDER BY bet_id")]
    assert [{k: r[k] for k in before[0]} for r in after] == before   # verbatim
    assert after[0]["reject_reason"] is None                        # nothing invented
    assert after[0]["model_prob"] == "0.5000"                       # nothing dropped

    idx = lg.conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='bets_attempt_ticker'"
    ).fetchone()
    assert idx is not None and "WHERE status != 'rejected'" in idx["sql"]
    assert lg.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert lg.conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE name='bets_pre_rebuild'"
    ).fetchone()["n"] == 0                       # no scaffolding left behind
    with pytest.raises(sqlite3.IntegrityError):  # the index still bites
        lg.insert_bet(
            bet_id=f"{aid}-B02", attempt_id=aid, ticket_index=2, ticker="T1", side="yes",
            limit_price=D("0.20"), rationale="r", status="filled",
        )
    with pytest.raises(sqlite3.IntegrityError):  # and so does the coid UNIQUE
        lg.insert_bet(
            bet_id=f"{aid}-B03", attempt_id=aid, ticket_index=3, ticker="T3", side="yes",
            limit_price=D("0.20"), rationale="r", status="filled",
            client_order_id=f"{aid}-B01",
        )
    # the two things the rebuild exists for are writable on the upgraded file
    lg.insert_bet(
        bet_id=f"{aid}-B04", attempt_id=aid, ticket_index=4, ticker="T4", side="yes",
        limit_price=D("0.20"), rationale="r", status="rejected",
        reject_code="drawdown_floor", reject_reason="the floor refused",
    )
    fresh = lg.conn.execute("SELECT * FROM bets WHERE bet_id=?", (f"{aid}-B04",)).fetchone()
    assert fresh["model_prob"] is None and fresh["reject_reason"] == "the floor refused"
    lg.close()


def test_009_leaves_a_fresh_and_an_upgraded_bets_table_identical(tmp_path):
    """Two rebuilds of the same table (007's and 009's) on the upgrade path against one
    CREATE on the fresh path: a schema that depends on how you got there is one nobody can
    read."""
    upgraded = tmp_path / "upgraded.db"
    lg = Ledger.open(upgraded, check_version=False)
    for path in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
        if int(path.name.split("_", 1)[0]) <= 8:
            lg._apply_schema_file(path)
    lg.migrate()
    upgraded_cols = [tuple(dict(c).values())
                     for c in lg.conn.execute("PRAGMA table_info(bets)")]
    lg.close()

    fresh = Ledger.open(tmp_path / "fresh.db")
    fresh.migrate()
    fresh_cols = [tuple(dict(c).values())
                  for c in fresh.conn.execute("PRAGMA table_info(bets)")]
    assert upgraded_cols == fresh_cols
    assert [c for c in fresh_cols if c[1] == "model_prob"][0][3] == 0   # notnull flag off
    fresh.close()


def test_009_widens_the_session_kinds_without_opening_them(tmp_path):
    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    assert _kind_accepted(lg, "s-director", "director")      # the new one
    assert _kind_accepted(lg, "s-attempt", "attempt")        # and every old one
    assert _kind_accepted(lg, "s-grader", "grader")
    assert _kind_accepted(lg, "s-deep", "deep_review")
    assert not _kind_accepted(lg, "s-stranger", "curator_v2")
    assert not _kind_accepted(lg, "s-blank", "")
    lg.close()


def test_009_upgrade_keeps_every_session_and_stamps_the_eras(tmp_path):
    """The upgrade path the live ledger takes: ``sessions`` is REBUILT for its CHECK, so
    its rows have to come through untouched, and ``attempts.era`` is derived from
    ``created_at`` rather than left for a later pass to guess at."""
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db, check_version=False)
    for path in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
        if int(path.name.split("_", 1)[0]) <= 8:
            lg._apply_schema_file(path)
    assert lg._user_version() == 9
    # Two attempts either side of the live genesis instant, written raw: the DAO writes
    # today's columns and this file predates them.
    for aid, created in (("A-0001", "2026-07-13T14:08:07Z"),
                         ("A-0002", "2026-08-29T01:00:00Z")):
        lg.conn.execute(
            "INSERT INTO attempts (attempt_id, created_at, status, env, model, effort, "
            "memory_mode, edge_class, prompt_version, toolkit_version, workspace_path) "
            "VALUES (?,?,'created','prod','m','high','off','probability','p','0.1.0','/ws')",
            (aid, created),
        )
    lg.insert_session("s-old", "grader", "claude-sonnet-5", "2026-08-29T02:00:00Z")
    lg.finish_session("s-old", ended_at="2026-08-29T02:30:00Z", exit="ok", num_turns=7,
                      cost_usd=D("1.25"), api_retries=3)
    lg.conn.execute(
        "INSERT INTO attempt_activity (attempt_id, extracted_at, bt_calls, n_bt_calls) "
        "VALUES ('A-0002','2026-08-29T03:00:00Z','{}',4)"
    )
    lg.conn.commit()
    before = lg.conn.execute("SELECT * FROM sessions").fetchall()
    lg.close()

    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True
    assert report["before"]["user_version"] == 9
    assert report["after"]["user_version"] == _SCHEMA_VERSION

    lg = Ledger.open(db)
    assert lg.conn.execute("SELECT * FROM sessions").fetchall() == before
    assert lg.conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE name='sessions_pre_director'"
    ).fetchone()["n"] == 0                       # no scaffolding left behind
    assert lg.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert _kind_accepted(lg, "s-new", "director")

    eras = {r["attempt_id"]: r["era"] for r in
            lg.conn.execute("SELECT attempt_id, era FROM attempts")}
    assert eras == {"A-0001": "pilot", "A-0002": "live-v1"}
    rows = {r["attempt_id"]: r for r in lg.conn.execute("SELECT * FROM attempts")}
    assert rows["A-0002"]["cell"] is None            # nothing invented
    assert rows["A-0002"]["cell_forced"] == 0        # …except the default
    act = lg.activity("A-0002")
    assert act["n_bt_calls"] == 4 and act["past_calls"] is None
    assert lg.backfill_past_counts() == 1
    act = lg.activity("A-0002")
    assert act["past_calls"] == 0 and act["past_subcommands"] == "{}"
    assert lg.backfill_past_counts() == 0            # and it is idempotent
    lg.close()


def test_009_leaves_a_fresh_and_an_upgraded_sessions_table_identical(tmp_path):
    """The rebuild folds 002's ALTERs into its CREATE, so the two paths must not drift."""
    upgraded = tmp_path / "upgraded.db"
    lg = Ledger.open(upgraded, check_version=False)
    for path in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
        if int(path.name.split("_", 1)[0]) <= 8:
            lg._apply_schema_file(path)
    lg.migrate()
    upgraded_cols = [tuple(dict(c).values())
                     for c in lg.conn.execute("PRAGMA table_info(sessions)")]
    lg.close()

    fresh = Ledger.open(tmp_path / "fresh.db")
    fresh.migrate()
    fresh_cols = [tuple(dict(c).values())
                  for c in fresh.conn.execute("PRAGMA table_info(sessions)")]
    assert upgraded_cols == fresh_cols
    fresh.close()


def test_009_is_a_no_op_the_second_time(tmp_path):
    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    lg.conn.execute(
        "INSERT INTO cell_plans (day, seed, plan, created_at) "
        "VALUES ('2026-09-20', 7, '[\"baseline\"]', '2026-09-20T04:00:00Z')"
    )
    lg.conn.commit()
    assert lg.migrate() == _SCHEMA_VERSION
    assert lg.conn.execute("SELECT COUNT(*) AS n FROM cell_plans").fetchone()["n"] == 1
    lg.close()


# ------------------------------------------------- the one-off session_exit correction
def _exit_row(lg, attempt_id, session_exit, num_turns):
    """An attempt row carrying nothing but the fields the correction reads."""
    lg.conn.execute(
        "INSERT INTO attempts (attempt_id, created_at, status, env, model, memory_mode, "
        "prompt_version, toolkit_version, workspace_path, session_exit, num_turns) "
        "VALUES (?,'2026-08-24T05:00:00Z','placed','prod','claude-opus-5','on','p1',"
        "'0.1.0','/ws',?,?)",
        (attempt_id, session_exit, num_turns),
    )
    lg.conn.commit()


def test_migrate_refiles_the_one_session_exit_the_record_can_settle(tmp_path):
    """docs/22 section 12. A-0162 timed out on the wall clock with its result record
    already written, so the row says ``ok``. A-0117 reads ``ok`` and stays ``ok``: nothing
    in the ledger tells it from a clean exit. Nothing else is in scope."""
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db)
    lg.migrate()
    _exit_row(lg, "A-0117", "ok", 40)
    _exit_row(lg, "A-0162", "timeout", 63)
    _exit_row(lg, "A-0999", "timeout", 12)      # a genuine timeout, not in scope
    _exit_row(lg, "A-0162x", "timeout", None)   # no result record, nothing to go on
    lg.close()

    assert safe_migrate(db, tmp_path / "backups")["ok"]
    lg = Ledger.open(db)
    exits = {
        r["attempt_id"]: r["session_exit"]
        for r in lg.conn.execute("SELECT attempt_id, session_exit FROM attempts")
    }
    assert exits == {"A-0117": "ok", "A-0162": "ok",
                     "A-0999": "timeout", "A-0162x": "timeout"}
    audits = lg.audit_events("session_exit_corrected")
    assert len(audits) == 1
    assert json.loads(audits[0]["detail"])["corrected"] == [
        {"attempt_id": "A-0162", "was": "timeout", "now": "ok"}
    ]
    lg.close()

    safe_migrate(db, tmp_path / "backups")      # idempotent: no second fix, no second row
    lg = Ledger.open(db)
    assert lg.get_attempt("A-0162")["session_exit"] == "ok"
    assert lg.audit_count("session_exit_corrected") == 1
    lg.close()


def test_a_ledger_with_neither_row_is_left_alone_and_audits_nothing(tmp_path):
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db)
    lg.migrate()
    _exit_row(lg, "A-0001", "timeout", 9)
    lg.close()

    safe_migrate(db, tmp_path / "backups")
    lg = Ledger.open(db)
    assert lg.get_attempt("A-0001")["session_exit"] == "timeout"
    assert lg.audit_count("session_exit_corrected") == 0
    lg.close()


def test_the_personal_orders_table_holds_its_checks(tmp_path):
    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    lg.upsert_personal_order(
        "OID-1", ticker="KXBIKE", side="yes", created_time="2026-08-30T12:00:00Z",
        contracts=D("1"), cost=D("0.9991"), fee=D("0.0175"), fee_source="exchange",
        first_seen_at="2026-08-30T12:05:00Z",
    )
    row = lg.personal_orders()[0]
    assert (row["cost"], row["fee"]) == ("0.9991", "0.0175")   # 4dp TEXT, like all money
    assert row["contracts"] == "1.0000" and row["on_harness_ticker"] == 0
    with pytest.raises(sqlite3.IntegrityError):
        lg.conn.execute(
            "INSERT INTO personal_orders (order_id, ticker, side, created_time, "
            "first_seen_at) VALUES ('OID-2','T','sideways','t','t')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        lg.conn.execute(
            "INSERT INTO personal_orders (order_id, ticker, side, created_time, "
            "fee_source, first_seen_at) VALUES ('OID-3','T','yes','t','guessed','t')"
        )
    lg.close()


# ------------------------------------------------------------------ 010 exchange credits
def test_010_lands_the_credits_table_on_a_fresh_ledger(tmp_path):
    """Fresh-create path: 000 through 010 in one ``migrate()``, and the table is writable
    rather than merely present."""
    lg = Ledger.open(tmp_path / "ledger.db")
    assert lg.migrate() == _SCHEMA_VERSION
    assert lg.conn.execute("SELECT COUNT(*) AS n FROM credits").fetchone()["n"] == 0

    credit_id = lg.insert_credit(
        credited_at="2026-09-20T04:45:00Z", amount=D("0.01"), kind="incentive",
        reason="Volume Incentive For Event KXRAINDNYC-260919",
        recorded_at="2026-09-21T18:00:00Z",
    )
    row = lg.conn.execute("SELECT * FROM credits WHERE credit_id=?", (credit_id,)).fetchone()
    assert row["amount"] == "0.0100"              # 4dp TEXT, like every other money column
    assert row["kind"] == "incentive"
    assert row["reason"].startswith("Volume Incentive")
    # No CHECK on ``kind``: the next programme the exchange invents must not need a
    # migration to be recorded.
    lg.insert_credit(credited_at="2026-10-01T00:00:00Z", amount=D("0.05"),
                     kind="whatever-they-call-it-next", recorded_at="2026-10-01T01:00:00Z")
    with pytest.raises(LedgerError):              # …but a float never reaches the column
        lg.insert_credit(credited_at="2026-10-02T00:00:00Z", amount=0.05, kind="incentive",
                         recorded_at="2026-10-02T01:00:00Z")
    lg.close()


def test_010_upgrade_adds_the_table_and_keeps_everything_else(tmp_path):
    """The upgrade path, which is the one the live ledger takes: a v10 file with real rows
    gains one table and loses nothing."""
    db = tmp_path / "ledger.db"
    lg = Ledger.open(db)
    for name in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
        if name.stem.startswith("010"):
            continue
        lg._apply_schema_file(name)
    assert lg._user_version() == 10
    assert lg.conn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE name='credits'"
    ).fetchone()["n"] == 0
    lg.upsert_personal_order(
        "OID-1", ticker="KXBIKE", side="yes", created_time="2026-08-30T12:00:00Z",
        contracts=D("1"), cost=D("0.9991"), fee=D("0.0175"), fee_source="exchange",
        first_seen_at="2026-08-30T12:05:00Z",
    )
    lg.close()

    report = safe_migrate(db, tmp_path / "backups")
    assert report["ok"] is True
    assert report["before"]["user_version"] == 10
    assert report["after"]["user_version"] == _SCHEMA_VERSION

    lg = Ledger.open(db)
    assert lg.personal_orders()[0]["cost"] == "0.9991"          # nothing was lost
    lg.insert_credit(credited_at="2026-09-20T04:45:00Z", amount=D("0.01"),
                     kind="incentive", recorded_at="2026-09-21T18:00:00Z")
    assert len(lg.credits_since("2026-09-01T00:00:00Z")) == 1   # …and the table is usable
    assert lg.migrate() == _SCHEMA_VERSION                      # a no-op the second time
    assert lg.conn.execute("SELECT COUNT(*) AS n FROM credits").fetchone()["n"] == 1
    lg.close()


def test_credits_before_genesis_are_already_in_the_anchor(tmp_path):
    """The walk may only spend credits dated at or after the era boundary; an earlier one
    is inside ``live_genesis_balance`` and counting it again invents money. A credit nobody
    can date is retained, the rule every other window here follows."""
    lg = Ledger.open(tmp_path / "ledger.db")
    lg.migrate()
    for ts in ("2026-09-01T00:00:00Z", "2026-09-20T04:45:00Z", "not-a-timestamp"):
        lg.insert_credit(credited_at=ts, amount=D("0.01"), kind="incentive",
                         recorded_at="2026-09-21T18:00:00Z")

    kept = {r["credited_at"] for r in lg.credits_since("2026-09-17T01:30:00Z")}
    assert kept == {"2026-09-20T04:45:00Z", "not-a-timestamp"}
    assert len(lg.credits_since(None)) == 3      # no window, no filtering
    lg.close()
