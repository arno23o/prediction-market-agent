"""Ledger DAO (spec §6).

One SQLite file, WAL, ``foreign_keys=ON``, dict rows. Every multi-row change is one
transaction. ``ledger_fts`` is DAO-maintained (no triggers): claim, hypothesis and
closing content is upserted in the same transaction as the row that produced it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from betting_agent.ids import attempt_id_from_seq
from betting_agent.moneymath import D, q4
from betting_agent.timeutil import ET, et_day, iso, parse_iso, utc_now

_SCHEMA_DIR = Path(__file__).parent / "schema"

# Multi-process contention budget (LG-8/ST-10): the tick, a detached attempt, a deep
# review and a full-database backup all touch this file, and Python's default is 5 s.
_BUSY_TIMEOUT_MS = 30000

# ``CREATE [VIRTUAL] TABLE [IF NOT EXISTS] <name>`` — used to derive the expected table
# set (and, with the pragma below, the expected version) from the schema files themselves
# rather than from a hardcoded list that the first future migration would invalidate (LG-6).
_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+(?:VIRTUAL\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`\[]?(\w+)",
    re.IGNORECASE,
)
_USER_VERSION_RE = re.compile(r"PRAGMA\s+user_version\s*=\s*(\d+)", re.IGNORECASE)

# Legal attempt-status transitions (spec §6.2); the DAO enforces exactly these.
_LEGAL_TRANSITIONS: dict[str, set[str]] = {
    "created": {"running"},
    "running": {"placed", "no_bets", "ticket_invalid", "failed"},
    "placed": {"settled"},
    "settled": {"reviewed"},
    "no_bets": {"reviewed"},
    "ticket_invalid": {"reviewed"},
    "failed": {"reviewed"},
    "reviewed": set(),
}

# Contract counts join the money discipline (KC-2): fractional trading is live, so a
# count is serialized through the same exact 4dp path and never assumed integral.
# Note the storage class: ``bets.contracts`` is declared INTEGER, so SQLite's numeric
# affinity converts the canonical literal back on the way in ("1.0000" -> INTEGER 1,
# "0.2800" -> REAL 0.28). Legacy rows hold plain INTEGER 1. Readers must therefore accept
# integer, real and text alike — every one of them goes through ``Decimal(str(value))``.
_COUNT_COLS = frozenset({"contracts", "declared_contracts"})

# Money columns are always stored as 4dp TEXT (spec §5).
_MONEY_COLS = frozenset({
    "cost_usd", "limit_price", "model_prob", "fill_price", "stake", "fee", "pnl",
    "declared_worst_case", "realized_pnl",
    # Jul29 revision (spec L1): shadow scoring and reconciliation money.
    "hypothetical_pnl", "expected_balance", "actual_balance", "drift",
    # docs/22 section 4.4 (schema 009): orders placed outside the harness, a walk term.
    "cost", "payout",
    # Schema 010: what the exchange credited the account, also a term in the walk.
    "amount",
})

_ATTEMPT_WRITABLE = frozenset({
    "slot", "env", "model", "effort", "memory_mode", "edge_class", "prompt_version",
    "toolkit_version", "variant", "context_pack_hash", "workspace_path",
    "claude_session_id", "session_exit", "num_turns", "cost_usd", "input_tokens",
    "output_tokens", "wall_seconds",
    # NOT writable here: edge_claim_md / hypothesis_md / manifest_md (LG-5). They are
    # FTS-synced columns and ``set_ticket_texts`` is their only writer — a generic
    # UPDATE through this whitelist would change the text and leave the search index
    # pointing at the old content. No caller passed them; the whitelist just allowed it.
    "error",
    # Jul29 revision (spec L1): experiment provenance columns.
    "recipe_id", "loop_mode", "priors_mode", "grader_blind", "playbook_version",
    # docs/14 D9: the A-0053 sleep-stretch flag (schema 004).
    "sleep_stretched",
})

_BET_COLS = frozenset({
    "bet_id", "attempt_id", "ticket_index", "ticker", "market_title", "category",
    "side", "limit_price", "model_prob", "rationale", "is_real", "group_id", "status",
    "reject_code", "contracts", "fill_price", "stake", "fee", "order_id",
    "client_order_id", "book_snapshot", "close_ts", "expected_resolution_ts",
    "placed_at", "settled_at", "outcome", "pnl",
    # docs/14 D12 (schema 004): the counterfactual for legs that carried no position.
    # NOT money — see 004_nofill_scoring.sql; nothing that totals the record reads these.
    "hypothetical_outcome", "hypothetical_pnl", "hypothetical_scored_at",
    "declared_contracts",
    # docs/14 D7: the leg's declared shared-resolution key (free string), or NULL.
    "resolution_event",
    # docs/22 section 4.2 (schema 009): the plain sentence beside ``reject_code``.
    "reject_reason",
})
# NOT writable through the generic UPDATE (docs/14 D12): the three hypothetical columns are
# reachable only from ``score_nofill_bet`` and ``score_rejected_bet``, whose own WHERE
# clauses are what guarantee a counterfactual is never re-scored and never lands on a row
# that holds a position; and ``declared_contracts`` is a pre-registration fact stamped at
# insert, like the FTS-synced ticket texts above. Excluding them here is what keeps "these
# columns have named writers" a property of the DAO rather than a convention a future caller
# has to remember.
_BET_WRITABLE = _BET_COLS - {
    "bet_id", "hypothetical_outcome", "hypothetical_pnl", "hypothetical_scored_at",
    "declared_contracts",
}

# What an attempt DID (schema 008). Measurement only: no cap, gate or verdict reads this
# table, and nothing in it is money. See 008_attempt_activity.sql for the counting
# conventions and for why the code stamp needs ``stamped_at_backfill`` read with it.
_ACTIVITY_COLS = frozenset({
    "extracted_at", "n_streams", "stream_bytes", "tool_calls", "n_tool_calls", "bt_calls",
    "n_bt_calls", "markets_probed", "series_probed", "series_list", "markets_seen",
    "web_fetches", "web_searches", "domains", "n_domains", "code_runs", "files_written",
    "agent_calls", "first_ticket_write_ts", "first_commit_frac", "cites_predecessor",
    "playbook_refs", "kill_criteria_n", "ticket_chars", "passed", "bets_proposed",
    "git_commit", "git_dirty", "config_sha", "prompt_version", "stamped_at_backfill",
    # docs/22 section 4.5 (schema 009): `bt past` calls, counted the way `bt` calls are.
    "past_calls", "past_subcommands",
})

# `closing` is the session's closing paragraph (docs/22 section 4.7), the third kind
# `bt past search` reads. `retro`, `summary` and `tags` are retired write targets: their
# writers are gone, so the set narrows and a new write under those kinds is an error. The
# old rows stay in the index and stay searchable under `--era all`.
_FTS_KINDS = frozenset({"claim", "hypothesis", "closing"})

# The two attempts docs/20 named as carrying a misfiled `session_exit`, corrected once on
# the migrate path (docs/22 section 12). See `Ledger.correct_session_exits`.
_SESSION_EXIT_FIX = ("A-0117", "A-0162")


class LedgerError(Exception):
    """Raised on illegal ledger operations (bad transition, unknown column, read-only)."""


# --------------------------------------------------------------------------- eras (WP-E)
# docs/14 E1. The pilot era is everything the machine did before it traded real money;
# the live era starts at ``meta.live_genesis_ts``. Nothing is deleted — statistics reset,
# lessons transfer (E2).
PILOT_ERA = "pilot"
LIVE_ERA = "live"
ALL_ERAS = "all"
ERAS = (LIVE_ERA, PILOT_ERA, ALL_ERAS)

# The genesis timestamp lives in ``meta`` and is read from there, never hardcoded — it is
# stamped once by the first tick that finds the live gate open (cli.py L7).
GENESIS_META = "live_genesis_ts"


def era_of(created_at: str | None, genesis: str | None) -> str:
    """``'live'`` iff ``created_at >= genesis``, else ``'pilot'`` (docs/14 E1).

    A plain string comparison: both sides are stored in the single UTC ``...Z`` form
    :func:`~betting_agent.timeutil.iso` writes, so lexical order *is* chronological order
    and an attempt created exactly at genesis is live. No ``genesis`` (the record has no
    live era yet) or no ``created_at`` reads as ``'pilot'``, which is E1's letter — but see
    :class:`EraScope` for why the *filters* built from that answer deliberately do not.
    """
    if genesis is None or not created_at:
        return PILOT_ERA
    return LIVE_ERA if created_at >= genesis else PILOT_ERA


@dataclass(frozen=True)
class EraScope:
    """One era selection, threaded through the statistics surfaces (docs/14 E2/E4).

    Carrying the genesis timestamp alongside the choice is what lets one object filter in
    SQL (:meth:`sql`), filter in Python (:meth:`keeps`) and label the result
    (:meth:`label`) without three independent copies of the boundary rule.

    **On a record with no ``live_genesis_ts`` the filter is a no-op.** The other reading —
    nothing is live before genesis, so a live-era default shows nothing — is precisely
    backwards: the reset exists to separate pilot from live, and before genesis there is
    only one era to look at. :func:`era_of` still answers ``'pilot'`` there; a scope built
    on it simply has nothing to narrow.
    """

    era: str = LIVE_ERA
    genesis: str | None = None

    def __post_init__(self) -> None:
        if self.era not in ERAS:
            raise LedgerError(f"unknown era {self.era!r} (expected one of {', '.join(ERAS)})")

    @classmethod
    def of(cls, ledger: Ledger, era: str = LIVE_ERA) -> EraScope:
        """The scope for ``era`` against this ledger's own genesis stamp."""
        return cls(era, ledger.genesis_ts())

    @property
    def filtering(self) -> bool:
        """True when this scope actually narrows a population."""
        return self.genesis is not None and self.era in (LIVE_ERA, PILOT_ERA)

    def keeps(self, created_at: str | None) -> bool:
        """Does an attempt created at ``created_at`` belong to this scope?"""
        return not self.filtering or era_of(created_at, self.genesis) == self.era

    def sql(self, attempt_col: str = "attempt_id") -> tuple[str, list]:
        """A WHERE fragment restricting ``attempt_col`` to this era, plus its params.

        ``("1", [])`` when nothing is narrowed, so a caller can always AND it in. The
        subquery reads ``attempts.created_at`` because the era is a property of the
        ATTEMPT, not of the row being counted: an attempt is one experiment and belongs
        wholly to the era it was launched in (report.py's ``_split_era`` rule, now shared).
        """
        if not self.filtering:
            return "1", []
        op = ">=" if self.era == LIVE_ERA else "<"
        return (
            f"{attempt_col} IN (SELECT attempt_id FROM attempts WHERE created_at {op} ?)",
            [self.genesis],
        )

    def label(self, n: int | None = None) -> str:
        """The visible scope label, with E4's ``n`` when a count is being labeled.

        E4 (the honesty rule): wherever a live-era statistic stands where the pooled one
        used to, the population size is shown next to it. Callers that have an ``n`` pass
        it; the label is never silently narrower than the number it sits beside.
        """
        # ``all`` is never "filtering", so its label is decided on the genesis stamp alone:
        # a record with a boundary is genuinely pooling two eras, one without has only one.
        if self.era == ALL_ERAS:
            base = (
                f"both eras pooled (pilot + live), split at {self._genesis_day()}"
                if self.genesis is not None else "all history"
            )
        elif not self.filtering:
            base = "all history (no live era yet)"
        elif self.era == LIVE_ERA:
            base = f"live era, since {self._genesis_day()}"
        else:
            base = f"pilot era, before {self._genesis_day()}"
        return base if n is None else f"{base}, n={n}"

    def _genesis_day(self) -> str:
        """``"Jul 31"`` — the genesis day in ET, the calendar this project speaks in."""
        if self.genesis is None:
            return "—"
        try:
            day = parse_iso(self.genesis).astimezone(ET)
        except ValueError:
            return self.genesis
        return f"{day:%b} {day.day}"


def _dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def _split_schema(script: str) -> tuple[list[str], list[str]]:
    """Split a schema file into ``(statements, user_version_statements)`` (LG-1).

    ``sqlite3.complete_statement`` is the same parser sqlite's own shell uses to decide
    where a statement ends: it accumulates lines until the buffer is a syntactically
    complete statement ending in a semicolon, so comments, embedded semicolons inside
    string literals, and multi-line ``CREATE TABLE`` bodies all split correctly.

    ``PRAGMA user_version = N`` statements come back separately so the caller can run
    them last — the version must be the final word of a migration, never a claim made
    before the tables it describes exist. A trailing fragment (a file not ending in a
    semicolon, or trailing comments) is kept as a statement so sqlite, not this splitter,
    decides whether it is legal.
    """
    body: list[str] = []
    versions: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if not sqlite3.complete_statement(buf):
            continue
        stmt = buf.strip()
        buf = ""
        if stmt:
            (versions if _USER_VERSION_RE.search(stmt) else body).append(stmt)
    tail = buf.strip()
    if tail:
        body.append(tail)
    return body, versions


def _schema_expectations() -> tuple[int, frozenset[str]]:
    """``(expected user_version, expected table names)`` read off the schema dir (LG-6).

    ``safe_migrate`` used to hardcode ``user_version == 2`` and the v2 table list, so the
    first future migration would have made a *successful* migration report failure. Both
    facts already live in the schema files; read them from there.
    """
    version = 0
    tables: set[str] = set()
    for path in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
        text = path.read_text()
        for m in _USER_VERSION_RE.finditer(text):
            version = max(version, int(m.group(1)))
        tables.update(m.group(1) for m in _CREATE_TABLE_RE.finditer(text))
    return version, frozenset(tables)


def _ser(col: str, val):
    """Serialize a value for storage: money and count columns become exact 4dp.

    A ``float`` reaching an exact-4dp column raises (LG-2). ``D(0.1)`` is
    ``0.1000000000000000055511151231257827…``, and quantizing it produces a
    plausible-looking ``"0.1000"`` — the laundering the floats-forbidden invariant
    exists to prevent, at the one choke point that can see it. **Scope chosen: money
    *and* count columns**, i.e. everything on the exact path. The finding narrows the
    rule to money so that honest non-money floats (``wall_seconds``) keep working, but
    counts share this exact serialization verbatim, and WP0 made counts Decimal
    end-to-end precisely so a fractional fill is never guessed at; a float count is the
    same defect wearing a different column name. Everything outside these two sets is
    untouched and may still be a float.
    """
    if val is None:
        return None
    if col in _MONEY_COLS or col in _COUNT_COLS:
        if isinstance(val, float):
            raise LedgerError(
                f"float is not an accepted value for the exact column {col!r}: {val!r} "
                "(pass a Decimal, int or str — floats cannot represent money exactly)"
            )
        return str(q4(D(val)))
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, bool):
        return int(val)
    return val


class Ledger:
    """Thin transactional DAO over the ledger SQLite file."""

    def __init__(self, conn: sqlite3.Connection, path: Path, readonly: bool) -> None:
        self.conn = conn
        self.path = path
        self.readonly = readonly

    # ------------------------------------------------------------------ lifecycle
    @classmethod
    def open(cls, path, readonly: bool = False, check_version: bool = True) -> Ledger:
        """Open the ledger. Every connection sets ``busy_timeout`` (LG-8/ST-10).

        This database routinely has three or more writer processes (the tick, a detached
        attempt, a deep review) plus a whole-database backup that holds a read lock for
        its duration. Python's implicit 5 s ``busy_timeout`` turned that ordinary
        contention into "database is locked" exceptions; 30 s waits out a backup instead.
        ``synchronous=NORMAL`` is the standard WAL pairing — durable across a process
        crash (which is what we actually defend against), fsync-cheap per transaction —
        and is set explicitly so it never depends on the sqlite build default.

        **The version guard is an addition the rebuild coordinator asked for, not part of
        docs/22.** The live tick runs from an editable install every fifteen minutes, so
        code that expects a new column can land before ``betting-agent migrate`` has run.
        Without a guard that gap is a crash in the middle of a tick, at an arbitrary
        statement. With it, every command refuses cleanly and says what to run. A file at
        ``user_version`` 0 is a brand-new ledger about to be created, so only a file that
        is behind (``0 < user_version < expected``) refuses. ``check_version=False`` is
        the escape the migration path itself needs: ``safe_migrate`` and ``init`` have to
        open a behind-the-times file in order to move it forward.
        """
        path = Path(path)
        if readonly:
            conn = sqlite3.connect(
                f"file:{path}?mode=ro", uri=True, isolation_level=None,
                check_same_thread=False,
            )
        else:
            conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        conn.row_factory = _dict_factory
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=NORMAL")
        lg = cls(conn, path, readonly)
        if check_version:
            expected = _schema_expectations()[0]
            found = lg._user_version()
            if 0 < found < expected:
                conn.close()
                raise LedgerError(
                    f"ledger schema is v{found} but this code expects v{expected}: "
                    "run `betting-agent migrate` before anything else touches the ledger"
                )
        return lg

    def close(self) -> None:
        self.conn.close()

    def _user_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()["user_version"]

    def migrate(self) -> int:
        """Apply ``schema/NNN_*.sql`` files whose number is >= the current user_version.

        Each file bumps ``user_version`` past its own number (000_init -> 1), so a fresh
        DB (version 0) applies 000_init and a re-run is a no-op.

        **One file, one transaction (LG-1).** ``executescript`` used to run these in
        autocommit, committing every DDL statement individually: a failure halfway
        through a file left a half-created schema carrying the *old* ``user_version``,
        so the re-run then died on "table already exists" — a bricked ledger needing
        hand surgery. Now each file is split into statements and executed inside one
        ``BEGIN IMMEDIATE``, with ``PRAGMA user_version`` last, so a mid-file failure
        rolls back to exactly the state before it and the migration is re-runnable.
        (We ran the v2 migration on the live ledger through the old code. It worked.
        That was luck.)
        """
        if self.readonly:
            raise LedgerError("cannot migrate a read-only ledger")
        for path in sorted(_SCHEMA_DIR.glob("[0-9]*.sql")):
            num = int(path.name.split("_", 1)[0])
            if num >= self._user_version():
                self._apply_schema_file(path)
        return self._user_version()

    def correct_session_exits(self) -> None:
        """Refile a `session_exit` the session's own record does not support (docs/22 §12).

        ``sessions.py`` reads the wall clock as the whole story: when the timeout fires the
        session is filed as ``timeout`` even if it had already finished its work and only
        the process lingered. A-0162 is that shape. It wrote a complete ticket and was
        graded, and the ledger still holds its final result record, which is the only thing
        that ever sets ``num_turns``, so ``ok`` is the exit its own row supports.

        A-0117, the other row docs/20 named, is left as it stands. It reads ``ok`` after the
        headless CLI cut its turn short, and ``killed`` in this ledger means our own SIGKILL
        after the wall clock, which never happened. Nothing in the ledger tells that row
        apart from a clean exit, so there is no corrected value to write.

        Scoped to those two attempts and to that one shape, so a second run changes nothing
        and no other row is ever touched. ``safe_migrate`` runs it after the schema files,
        which makes ``betting-agent migrate`` the one command that carries it.
        """
        rows = self.conn.execute(
            "SELECT attempt_id, session_exit FROM attempts WHERE attempt_id IN (?,?) "
            "AND session_exit IN ('timeout','killed') AND num_turns IS NOT NULL",
            _SESSION_EXIT_FIX,
        ).fetchall()
        if not rows:
            return
        with self._tx() as cur:
            cur.executemany(
                "UPDATE attempts SET session_exit='ok' WHERE attempt_id=?",
                [(r["attempt_id"],) for r in rows],
            )
        self.audit("session_exit_corrected", detail={
            "corrected": [
                {"attempt_id": r["attempt_id"], "was": r["session_exit"], "now": "ok"}
                for r in rows
            ],
        })

    def _apply_schema_file(self, path: Path) -> None:
        """Execute one schema file atomically (see :meth:`migrate`)."""
        body, version_stmts = _split_schema(path.read_text())
        with self._tx() as cur:
            for stmt in body:
                cur.execute(stmt)
            # user_version last: the version is the claim that everything above landed,
            # and inside this transaction it rolls back with them if anything did not.
            for stmt in version_stmts:
                cur.execute(stmt)

    @contextmanager
    def _tx(self):
        if self.readonly:
            raise LedgerError("ledger is read-only")
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ FTS
    @staticmethod
    def _fts_write(cur: sqlite3.Cursor, attempt_id: str, kind: str, content: str | None) -> None:
        if kind not in _FTS_KINDS:
            raise LedgerError(f"unknown FTS kind: {kind!r}")
        cur.execute("DELETE FROM ledger_fts WHERE attempt_id=? AND kind=?", (attempt_id, kind))
        if content:
            cur.execute(
                "INSERT INTO ledger_fts (attempt_id, kind, content) VALUES (?,?,?)",
                (attempt_id, kind, content),
            )

    def fts_upsert(self, attempt_id: str, kind: str, content: str) -> None:
        with self._tx() as cur:
            self._fts_write(cur, attempt_id, kind, content)

    # ------------------------------------------------------------------ attempts
    def create_attempt(
        self, env, model, effort, memory_mode, prompt_version, toolkit_version,
        workspace_path, slot=None, variant=None, edge_class="probability",
        era=None, cell=None, cell_effective=None, cell_forced=0,
    ) -> tuple[int, str]:
        """Insert a 'created' attempt, derive attempt_id from seq in the same txn.

        ``era``, ``cell``, ``cell_effective`` and ``cell_forced`` arrive with schema 009
        (docs/22 section 4.1) and are all optional: the runner passes them from phase two
        on, and every caller that predates them keeps writing NULL, which is what an
        attempt that ran before cells existed genuinely had.
        """
        placeholder = f"__pending__{uuid.uuid4().hex}"
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO attempts (attempt_id, created_at, status, slot, env, model, "
                "effort, memory_mode, edge_class, prompt_version, toolkit_version, variant, "
                "workspace_path, era, cell, cell_effective, cell_forced) "
                "VALUES (?,?, 'created', ?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (placeholder, iso(utc_now()), slot, env, model, effort, memory_mode,
                 edge_class, prompt_version, toolkit_version, variant, workspace_path,
                 era, cell, cell_effective, int(cell_forced or 0)),
            )
            seq = cur.lastrowid
            attempt_id = attempt_id_from_seq(seq)
            cur.execute("UPDATE attempts SET attempt_id=? WHERE seq=?", (attempt_id, seq))
        return seq, attempt_id

    def set_context_record(
        self, attempt_id: str, *, context_pack_hash=None, example_ids=None,
        direction_hash=None, cell_effective=None,
    ) -> None:
        """Record what an attempt was actually shown (docs/22 sections 4.1 and 5.2).

        One writer for the four facts the runner learns together once ``cells.render`` has
        run: the hash of the CONTEXT.md it wrote, the attempt ids inside it, the hash of
        the direction page it carried, and the cell it ended up rendering. They are written
        together because they describe one rendering; splitting them would allow a row that
        names examples from a cell it did not run.
        """
        cols = {
            "context_pack_hash": context_pack_hash, "example_ids": example_ids,
            "direction_hash": direction_hash, "cell_effective": cell_effective,
        }
        cols = {k: v for k, v in cols.items() if v is not None}
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        with self._tx() as cur:
            cur.execute(
                f"UPDATE attempts SET {sets} WHERE attempt_id=?",
                (*cols.values(), attempt_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such attempt: {attempt_id}")

    def set_session_summary(self, attempt_id: str, summary: str | None) -> None:
        """The session's closing paragraph, ``SessionResult.result_text`` (docs/22 5.2)."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE attempts SET session_summary=? WHERE attempt_id=?",
                (summary, attempt_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such attempt: {attempt_id}")

    def get_attempt(self, attempt_id: str) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()

    def attempts_by_status(self, *statuses: str) -> list[dict]:
        if not statuses:
            return []
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"SELECT * FROM attempts WHERE status IN ({marks}) ORDER BY seq", statuses
        ).fetchall()

    def transition(self, attempt_id: str, new_status: str) -> None:
        with self._tx() as cur:
            row = cur.execute(
                "SELECT status FROM attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise LedgerError(f"no such attempt: {attempt_id}")
            cur_status = row["status"]
            if new_status not in _LEGAL_TRANSITIONS.get(cur_status, set()):
                raise LedgerError(
                    f"illegal transition {cur_status!r} -> {new_status!r} for {attempt_id}"
                )
            cur.execute(
                "UPDATE attempts SET status=? WHERE attempt_id=?", (new_status, attempt_id)
            )

    def update_attempt_fields(self, attempt_id: str, **cols) -> None:
        bad = set(cols) - _ATTEMPT_WRITABLE
        if bad:
            raise LedgerError(f"non-writable attempt columns: {sorted(bad)}")
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = [_ser(k, v) for k, v in cols.items()]
        with self._tx() as cur:
            cur.execute(
                f"UPDATE attempts SET {sets} WHERE attempt_id=?", (*vals, attempt_id)
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such attempt: {attempt_id}")

    def set_ticket_texts(
        self, attempt_id: str, edge_claim_md: str, hypothesis_md: str, manifest_md: str
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE attempts SET edge_claim_md=?, hypothesis_md=?, manifest_md=? "
                "WHERE attempt_id=?",
                (edge_claim_md, hypothesis_md, manifest_md, attempt_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such attempt: {attempt_id}")
            self._fts_write(cur, attempt_id, "claim", edge_claim_md)
            self._fts_write(cur, attempt_id, "hypothesis", hypothesis_md)

    def set_session_result(
        self, attempt_id, claude_session_id, session_exit, num_turns, cost_usd,
        input_tokens, output_tokens, wall_seconds, error=None,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "UPDATE attempts SET claude_session_id=?, session_exit=?, num_turns=?, "
                "cost_usd=?, input_tokens=?, output_tokens=?, wall_seconds=?, error=? "
                "WHERE attempt_id=?",
                (claude_session_id, session_exit, num_turns, _ser("cost_usd", cost_usd),
                 input_tokens, output_tokens, wall_seconds, error, attempt_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such attempt: {attempt_id}")

    # ------------------------------------------------------------------ groups
    def insert_group(
        self, group_id: str, attempt_id: str, scenarios: str, declared_worst_case: str
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO bet_groups (group_id, attempt_id, scenarios, "
                "declared_worst_case, status) VALUES (?,?,?,?, 'pending')",
                (group_id, attempt_id, scenarios, _ser("declared_worst_case", declared_worst_case)),
            )

    def set_group(self, group_id: str, status=None, realized_pnl=None) -> None:
        sets, vals = [], []
        if status is not None:
            sets.append("status=?")
            vals.append(status)
        if realized_pnl is not None:
            sets.append("realized_pnl=?")
            vals.append(_ser("realized_pnl", realized_pnl))
        if not sets:
            return
        vals.append(group_id)
        with self._tx() as cur:
            cur.execute(f"UPDATE bet_groups SET {', '.join(sets)} WHERE group_id=?", vals)
            # LG-3: the DAO's convention is that a targeted update names a row that
            # exists. This was the only updater without the check.
            if cur.rowcount == 0:
                raise LedgerError(f"no such group: {group_id}")

    def groups_for_attempt(self, attempt_id: str) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM bet_groups WHERE attempt_id=? ORDER BY group_id", (attempt_id,)
        ).fetchall()

    # ------------------------------------------------------------------ bets
    def insert_bet(self, **fields) -> None:
        bad = set(fields) - _BET_COLS
        if bad:
            raise LedgerError(f"unknown bet columns: {sorted(bad)}")
        cols = list(fields)
        marks = ",".join("?" * len(cols))
        vals = [_ser(c, fields[c]) for c in cols]
        with self._tx() as cur:
            cur.execute(f"INSERT INTO bets ({','.join(cols)}) VALUES ({marks})", vals)

    def bets_for_attempt(self, attempt_id: str) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM bets WHERE attempt_id=? ORDER BY ticket_index, bet_id", (attempt_id,)
        ).fetchall()

    def update_bet(self, bet_id: str, **cols) -> None:
        bad = set(cols) - _BET_WRITABLE
        if bad:
            raise LedgerError(f"non-writable bet columns: {sorted(bad)}")
        if not cols:
            return
        sets = ", ".join(f"{k}=?" for k in cols)
        vals = [_ser(k, v) for k, v in cols.items()]
        with self._tx() as cur:
            cur.execute(f"UPDATE bets SET {sets} WHERE bet_id=?", (*vals, bet_id))
            if cur.rowcount == 0:
                raise LedgerError(f"no such bet: {bet_id}")

    def filled_unsettled_bets(self) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM bets WHERE status='filled' ORDER BY bet_id"
        ).fetchall()

    def unscored_nofill_bets(self) -> list[dict]:
        """``no_fill`` bets still awaiting their D12 hypothetical score (docs/14 D12).

        A no-fill is terminal, so settlement never revisits it and this is the only query
        that finds one. It is also the idempotence key: scoring stamps
        ``hypothetical_scored_at``, which drops the row out of this set permanently, so a
        second settle pass over the same world scores nothing twice.
        """
        return self.conn.execute(
            "SELECT * FROM bets WHERE status='no_fill' AND hypothetical_scored_at IS NULL "
            "ORDER BY bet_id"
        ).fetchall()

    def unscored_rejected_bets(self) -> list[dict]:
        """``rejected`` bets still awaiting a hypothetical score (docs/14 D12, extended).

        The other half of the refused book. A no-fill is a leg the exchange never gave us;
        a reject is a leg the harness itself never sent, because a cap (``cap_daily``,
        ``cap_market``), the drawdown floor (``drawdown_floor``) or a validation gate
        (``V11`` and friends) refused it. Both carried no position and both are invisible
        to the grader beyond a status word, so both get the same counterfactual, from the
        same columns, under the same assumption. The WHERE clause is ``status='rejected'``
        and nothing else: a new refusal code is in this population the day it is written,
        and the codes above are named to say what is here, never to select it.

        Same idempotence key as the no-fill query: ``hypothetical_scored_at`` drops the row
        out of this set for good.
        """
        return self.conn.execute(
            "SELECT * FROM bets WHERE status='rejected' AND hypothetical_scored_at IS NULL "
            "ORDER BY bet_id"
        ).fetchall()

    def score_nofill_bet(
        self, bet_id: str, *, outcome: str, hypothetical_pnl, scored_at: str
    ) -> None:
        """Stamp the D12 counterfactual on a no-fill row, and nothing else (docs/14 D12).

        Deliberately NOT routed through ``update_bet``, which cannot write these columns at
        all (see ``_BET_WRITABLE``): this is their only writer, and it cannot reach
        ``status``, ``outcome``, ``pnl`` or ``settled_at`` — the four a slip here would
        corrupt, on the table that is the money record. The WHERE clause re-asserts both
        preconditions (still a no-fill, still unscored) so the write is safe even if the row
        moved under a concurrent pass.
        """
        self._score_counterfactual(
            bet_id, "no_fill", outcome=outcome, hypothetical_pnl=hypothetical_pnl,
            scored_at=scored_at,
        )

    def score_rejected_bet(
        self, bet_id: str, *, outcome: str, hypothetical_pnl, scored_at: str
    ) -> None:
        """Stamp the counterfactual on a rejected row, and nothing else (docs/14 D12).

        The no-fill writer's twin, pinned to ``status='rejected'``. Two named methods
        rather than one with a status argument: each one's WHERE clause is the guarantee
        that a counterfactual score cannot land on a row holding a position, and a caller
        that has to pass the status in is a caller that can pass the wrong one.
        """
        self._score_counterfactual(
            bet_id, "rejected", outcome=outcome, hypothetical_pnl=hypothetical_pnl,
            scored_at=scored_at,
        )

    def _score_counterfactual(
        self, bet_id: str, status: str, *, outcome: str, hypothetical_pnl, scored_at: str
    ) -> None:
        """The single UPDATE behind both counterfactual writers.

        It reaches exactly three columns. ``status``, ``outcome``, ``pnl`` and
        ``settled_at`` — the money record and its provenance — are unreachable from here,
        which is the whole point of keeping this write out of ``update_bet``.
        """
        with self._tx() as cur:
            cur.execute(
                "UPDATE bets SET hypothetical_outcome=?, hypothetical_pnl=?, "
                "hypothetical_scored_at=? WHERE bet_id=? AND status=? "
                "AND hypothetical_scored_at IS NULL",
                (outcome, _ser("hypothetical_pnl", hypothetical_pnl), scored_at, bet_id,
                 status),
            )
            if cur.rowcount == 0:
                label = "no-fill" if status == "no_fill" else status
                raise LedgerError(
                    f"no unscored {label} bet to score: {bet_id} "
                    f"(absent, not a {status}, or already scored)"
                )

    # ------------------------------------------------------------------ audit
    def audit(self, event: str, attempt_id=None, bet_id=None, detail: dict | None = None) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO audit_log (ts, event, attempt_id, bet_id, detail) VALUES (?,?,?,?,?)",
                (iso(utc_now()), event, attempt_id, bet_id,
                 json.dumps(detail) if detail is not None else None),
            )

    def audit_count(self, event: str, since: str | None = None) -> int:
        """How many ``event`` rows exist (optionally with ``ts >= since``) — OR-9/EF-4.

        The report used to count by materializing up to a million full rows into Python
        and filtering them there, three times per render. ``audit_log`` grows forever by
        construction (every tick writes to it), so that cost is unbounded in account age
        for a number SQLite can produce from an index. ``since`` is compared as the ISO
        text the rows store, which is the same comparison the Python filter did.
        """
        if since is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE event=?", (event,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE event=? AND ts >= ?",
                (event, since),
            ).fetchone()
        return int(row["n"]) if row else 0

    def audit_events(self, event=None, limit=100) -> list[dict]:
        if event is not None:
            return self.conn.execute(
                "SELECT * FROM audit_log WHERE event=? ORDER BY id DESC LIMIT ?",
                (event, limit),
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    # ------------------------------------------------------------------ meta / slots
    def meta_get(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(value)),
            )

    def meta_set_many(self, values: dict) -> None:
        """Write several meta keys in ONE transaction (OR-5).

        Two related stamps written as two transactions can be interrupted between them.
        The live-genesis pair is the case that bit: ``live_genesis_ts`` without
        ``live_genesis_balance`` is a half-stamp that fails *every* subsequent
        reconciliation (the balance walk has an anchor date and no anchor balance) and
        never retries, because the presence of the timestamp is what says "already
        stamped". Either both land or neither does.
        """
        if not values:
            return
        with self._tx() as cur:
            for key, value in values.items():
                cur.execute(
                    "INSERT INTO meta (key, value) VALUES (?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, str(value)),
                )

    # ------------------------------------------------------------------ eras (docs/14 E1)
    def genesis_ts(self) -> str | None:
        """``meta.live_genesis_ts`` (spec L7), or ``None`` while the record is paper-only.

        The one reader of that key for era purposes — the boundary is never hardcoded, and
        an empty stored value reads the same as an absent one.
        """
        ts = self.meta_get(GENESIS_META)
        return str(ts) if ts else None

    def era(self, attempt) -> str:
        """``'live'`` or ``'pilot'`` for an attempt row or attempt id (docs/14 E1).

        E1's letter, which means a ledger with no genesis stamp answers ``'pilot'`` for
        everything. Code that *filters or labels* for display wants :meth:`era_scope`
        instead — it treats a genesis-less record as the single era it is.
        """
        if isinstance(attempt, str):
            row = self.get_attempt(attempt)
            created = row["created_at"] if row else None
        else:
            created = attempt.get("created_at") if attempt else None
        return era_of(created, self.genesis_ts())

    def era_scope(self, era: str = LIVE_ERA) -> EraScope:
        """This ledger's :class:`EraScope` for ``era`` (default: the live era, E2)."""
        return EraScope.of(self, era)

    def consume_slot(self, slot_key: str, attempt_id: str) -> bool:
        """Atomic first-writer-wins: True iff this call claimed the slot."""
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO NOTHING",
                (slot_key, attempt_id),
            )
            return cur.rowcount == 1

    def reclaim_slot(self, slot_key: str, expect: str, value: str) -> bool:
        """Compare-and-set a slot's value; True iff it still read ``expect`` (OR-2/ST-5).

        The re-offer counterpart of :meth:`consume_slot`. A slot consumed for a spawn
        that never became a running attempt must be offerable again, and the check must
        be atomic against the child that is starting *right now*: the child stamps the
        slot the moment it holds ``attempt.lock``, so a CAS that still sees the pending
        marker is proof no child has claimed it. Never resurrects a slot key that does
        not exist (an unclaimed slot goes through ``consume_slot``).
        """
        with self._tx() as cur:
            cur.execute(
                "UPDATE meta SET value=? WHERE key=? AND value=?", (value, slot_key, expect)
            )
            return cur.rowcount == 1

    # ------------------------------------------------------------------ spend caps
    def daily_real_spend(self, et_day_str: str) -> Decimal:
        """Sum of real-bet stakes whose placed_at (UTC) falls in the given ET day."""
        rows = self.conn.execute(
            "SELECT placed_at, stake FROM bets "
            "WHERE is_real=1 AND placed_at IS NOT NULL AND stake IS NOT NULL"
        ).fetchall()
        total = D("0")
        for r in rows:
            if et_day(parse_iso(r["placed_at"])) == et_day_str:
                total += D(r["stake"])
        return q4(total)

    def per_market_real_stake(self, ticker: str, et_day_str: str) -> Decimal:
        """Real stake on one ticker inside one ET day (docs/22 section 7.6).

        This used to sum a ticker's real stake for ALL time, with no day, era or genesis
        filter, while the daily cap next to it was day-scoped. The consequence was that a
        ticker which ever reached ``stakes.per_market_real_cap`` was closed to real orders
        permanently, so the per-market cap slowly ate the board. The day filter is the same
        one :meth:`daily_real_spend` applies, on the same column, so both caps now describe
        the same charge day.
        """
        rows = self.conn.execute(
            "SELECT placed_at, stake FROM bets "
            "WHERE is_real=1 AND ticker=? AND placed_at IS NOT NULL AND stake IS NOT NULL",
            (ticker,),
        ).fetchall()
        total = D("0")
        for r in rows:
            if et_day(parse_iso(r["placed_at"])) == et_day_str:
                total += D(r["stake"])
        return q4(total)

    # ------------------------------------------------------------- personal orders (009)
    def upsert_personal_order(
        self, order_id: str, *, ticker: str, side: str, created_time: str,
        contracts=None, cost=None, fee=None, fee_source=None, on_harness_ticker: int = 0,
        first_seen_at: str,
    ) -> None:
        """Record one order placed on the account outside the harness (docs/22 7.3).

        Insert on first sight, update the money on every later sight, and never move
        ``first_seen_at``: the scan re-reads a 24-hour overlap window on every pass and
        re-judges the whole history on the weekly full scan, so the same order arrives many
        times and the row must say when the harness first saw it, not when it last looked.

        ``on_harness_ticker`` is stamped once, for the same reason and a sharper one: it
        answers "was this order netted against a position of ours", which is a fact about
        the moment the order was placed. Re-deriving it on every sighting means a harness
        bet on that ticker WEEKS LATER flips a settled-into-the-walk order out of the
        arithmetic, and the expected balance jumps by its cost and fee on a night nothing
        happened.

        ``settled_at`` and ``payout`` are deliberately untouched here. They belong to
        :meth:`settle_personal_order`, which is the only writer that may claim the exchange
        has paid something out.
        """
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO personal_orders (order_id, ticker, side, created_time, "
                "contracts, cost, fee, fee_source, on_harness_ticker, first_seen_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(order_id) DO UPDATE SET ticker=excluded.ticker, "
                "side=excluded.side, created_time=excluded.created_time, "
                "contracts=excluded.contracts, cost=excluded.cost, fee=excluded.fee, "
                "fee_source=excluded.fee_source",
                (order_id, ticker, side, created_time, _ser("contracts", contracts),
                 _ser("cost", cost), _ser("fee", fee), fee_source,
                 int(on_harness_ticker), first_seen_at),
            )

    def settle_personal_order(self, order_id: str, *, settled_at: str, payout) -> None:
        """Stamp the exchange's settlement of a personal order. Never re-settles a row."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE personal_orders SET settled_at=?, payout=? "
                "WHERE order_id=? AND settled_at IS NULL",
                (settled_at, _ser("payout", payout), order_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no unsettled personal order: {order_id}")

    def delete_personal_order(self, order_id: str) -> int:
        """Remove one ``personal_orders`` row if it is there; return the rows deleted.

        One case needs this: an order an earlier scan misfiled as an outside one and a later
        one recognizes as the harness's own canary. Idempotent and deliberately unguarded
        against "no such row", unlike the other targeted writers here, because the caller
        runs it every time it recognizes the order and only the first run has anything to
        do.
        """
        with self._tx() as cur:
            cur.execute("DELETE FROM personal_orders WHERE order_id=?", (order_id,))
            return cur.rowcount

    def personal_orders(self) -> list[dict]:
        """Every personal order, oldest first. The walk applies its own genesis window."""
        return self.conn.execute(
            "SELECT * FROM personal_orders ORDER BY created_time, order_id"
        ).fetchall()

    def unsettled_personal_orders(self) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM personal_orders WHERE settled_at IS NULL "
            "ORDER BY created_time, order_id"
        ).fetchall()

    def personal_order_tickers(self) -> set[str]:
        """Tickers traded outside the harness, for the settlements cross-check's covered set."""
        return {
            r["ticker"]
            for r in self.conn.execute("SELECT DISTINCT ticker FROM personal_orders")
        }

    def personal_orders_summary(self, genesis_ts: str | None = None) -> dict:
        """``{"n": int, "net_cost": Decimal}`` since genesis, for the status line.

        ``net_cost`` is cost plus fee minus payout, i.e. what trading outside the harness has
        taken out of the account so far. An unreadable ``created_time`` is retained rather
        than dropped, the rule every other window in this system follows.
        """
        cutoff = None
        if genesis_ts:
            try:
                cutoff = parse_iso(str(genesis_ts))
            except (ValueError, TypeError):
                cutoff = None
        n = 0
        net = D("0")
        for r in self.personal_orders():
            if cutoff is not None:
                try:
                    if parse_iso(str(r["created_time"])) < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            n += 1
            net += D(r["cost"] or "0") + D(r["fee"] or "0") - D(r["payout"] or "0")
        return {"n": n, "net_cost": q4(net)}

    # ------------------------------------------------------------- exchange credits (010)
    def insert_credit(self, *, credited_at: str, amount, kind: str, recorded_at: str,
                      reason: str | None = None) -> int:
        """Record one credit the exchange paid the account; return its ``credit_id``.

        Entered by hand from the app, because no API reports these (schema 010). Nothing
        here is idempotent: the exchange gives no identifier to key on, two credits of the
        same size on the same day are a real thing, and a command the operator runs once with
        the amount in front of them is a better guard than a key invented here.
        """
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO credits (credited_at, amount, kind, reason, recorded_at) "
                "VALUES (?,?,?,?,?)",
                (credited_at, _ser("amount", amount), kind, reason, recorded_at),
            )
            return int(cur.lastrowid)

    def credits_since(self, genesis_ts: str | None = None) -> list[dict]:
        """Credits at or after genesis, oldest first: what the balance walk may spend.

        The window is applied here rather than by the caller because a credit from before
        the era boundary is already inside the genesis balance, and adding it again would
        invent money. An unreadable ``credited_at`` is retained, the rule every other window
        in this system follows: an event nobody can date shows up as drift rather than
        vanishing.
        """
        cutoff = None
        if genesis_ts:
            try:
                cutoff = parse_iso(str(genesis_ts))
            except (ValueError, TypeError):
                cutoff = None
        rows = self.conn.execute(
            "SELECT * FROM credits ORDER BY credited_at, credit_id"
        ).fetchall()
        if cutoff is None:
            return rows
        kept = []
        for r in rows:
            try:
                if parse_iso(str(r["credited_at"])) < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
            kept.append(r)
        return kept

    # ------------------------------------------------------------------ sessions (L18)
    def insert_session(
        self, session_id: str, kind: str, model: str, started_at: str, attempt_id=None
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO sessions (session_id, attempt_id, kind, model, started_at) "
                "VALUES (?,?,?,?,?)",
                (session_id, attempt_id, kind, model, started_at),
            )

    def finish_session(
        self, session_id: str, *, ended_at: str, exit: str, num_turns=None,
        cost_usd=None, input_tokens=None, output_tokens=None, wall_seconds=None,
        error=None, api_retries=None, throttle_errors=None, error_kinds=None,
    ) -> None:
        """Close a session row; the last three are compute health (docs/14 D11 §7).

        They stay ``None`` when the caller has nothing to say, which is how a session
        recorded before migration 002 — or by a caller that never saw a stream — is
        distinguishable from one that genuinely saw zero retries. ``error_kinds`` is a
        ``{kind: count}`` mapping stored as sorted-key JSON.
        """
        kinds_json = (
            json.dumps(error_kinds, sort_keys=True)
            if isinstance(error_kinds, dict) and error_kinds else None
        )
        with self._tx() as cur:
            cur.execute(
                "UPDATE sessions SET ended_at=?, exit=?, num_turns=?, cost_usd=?, "
                "input_tokens=?, output_tokens=?, wall_seconds=?, error=?, "
                "api_retries=?, throttle_errors=?, error_kinds=? "
                "WHERE session_id=?",
                (ended_at, exit, num_turns, _ser("cost_usd", cost_usd), input_tokens,
                 output_tokens, wall_seconds, error, api_retries, throttle_errors,
                 kinds_json, session_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such session: {session_id}")

    def sessions_for_attempt(self, attempt_id: str) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM sessions WHERE attempt_id=? ORDER BY started_at", (attempt_id,)
        ).fetchall()

    # ------------------------------------------------------------------ activity (008)
    def upsert_activity(self, row: dict) -> None:
        """Write one ``attempt_activity`` row, replacing any row already there.

        Upsert rather than insert because re-extraction is the normal case: the streams do
        not change, but the extractor does, and re-running it over an attempt has to be a
        no-cost operation rather than a duplicate-key error. ``extracted_at`` is stamped here
        when the caller does not supply one, so the row always says when it was read.

        Unknown columns are refused by name, the way ``insert_bet`` refuses them: this table
        is written from a dict assembled elsewhere, and a typo that silently dropped a column
        would show up as a zero in an analysis months later.
        """
        cols = set(row) - {"attempt_id"}
        bad = cols - _ACTIVITY_COLS
        if bad:
            raise LedgerError(f"unknown activity columns: {sorted(bad)}")
        if not row.get("attempt_id"):
            raise LedgerError("activity row needs an attempt_id")
        data = dict(row)
        data.setdefault("extracted_at", iso(utc_now()))
        names = list(data)
        marks = ",".join("?" * len(names))
        with self._tx() as cur:
            cur.execute(
                f"INSERT OR REPLACE INTO attempt_activity ({','.join(names)}) VALUES ({marks})",
                [data[n] for n in names],
            )

    def backfill_past_counts(self) -> int:
        """Fill zero ``bt past`` counts on rows recorded before schema 009 (docs/22 4.5).

        A row written before the columns existed says NULL, which reads as "not measured".
        Every one of those attempts ran before ``bt past`` existed, so the honest value is
        zero calls and an empty subcommand map, not "unknown". Returns the rows filled.
        """
        with self._tx() as cur:
            cur.execute(
                "UPDATE attempt_activity SET past_calls=0, past_subcommands='{}' "
                "WHERE past_calls IS NULL"
            )
            return cur.rowcount

    def activity(self, attempt_id: str) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM attempt_activity WHERE attempt_id=?", (attempt_id,)
        ).fetchone()

    def activity_rows(self) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM attempt_activity ORDER BY attempt_id"
        ).fetchall()

    # ------------------------------------------------------------------ cell plans (docs/22 8.7)
    def cell_plan(self, day: str) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM cell_plans WHERE day=?", (day,)
        ).fetchone()

    def set_cell_plan(self, day: str, seed: int, plan_json: str) -> None:
        """Store the day's drawn plan, once (docs/22 section 8.7).

        ``INSERT OR IGNORE``: the plan is drawn by whichever tick gets there first and is
        then the day's plan for everyone. A second writer racing the first must lose
        rather than replace it, because a slot that has already run read the earlier draw.
        """
        with self._tx() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO cell_plans (day, seed, plan, created_at) "
                "VALUES (?,?,?,?)",
                (day, int(seed), plan_json, iso(utc_now())),
            )

    # ------------------------------------------------------------------ director (docs/22 8.2)
    def insert_director_run(self, run_id: str, *, run_date: str, cohort_date: str,
                            started_at: str, model: str,
                            session_id: str | None = None) -> None:
        """Open the day's director run at ``running``, before anything else happens.

        The row exists before the workspace is built, so a run that dies anywhere after
        this line is still a run that happened, and the tick does not spawn another one
        every fifteen minutes for the rest of the day. ``session_id`` is filled in by
        :meth:`set_director_session` when the session opens, because the column references
        ``sessions`` and there is no session row to point at yet.
        """
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO director_runs (run_id, run_date, cohort_date, started_at, "
                "session_id, model, status) VALUES (?,?,?,?,?,?,'running')",
                (run_id, run_date, cohort_date, started_at, session_id, model),
            )

    def set_director_session(self, run_id: str, session_id: str) -> None:
        """Point an open run at the session that is running it."""
        with self._tx() as cur:
            cur.execute("UPDATE director_runs SET session_id=? WHERE run_id=?",
                        (session_id, run_id))
            if cur.rowcount == 0:
                raise LedgerError(f"no such director run: {run_id}")

    def finish_director_run(self, run_id: str, *, status: str, ended_at: str,
                            page_md=None, page_hash=None, sets_json=None, error=None) -> None:
        """Close the run at ``invalid`` or ``failed``; a valid run closes in
        :meth:`store_director_review`, which cannot flip the status without its rows."""
        with self._tx() as cur:
            cur.execute(
                "UPDATE director_runs SET status=?, ended_at=?, page_md=?, page_hash=?, "
                "sets_json=?, error=? WHERE run_id=?",
                (status, ended_at, page_md, page_hash, sets_json, error, run_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such director run: {run_id}")

    def director_run(self, run_id: str) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM director_runs WHERE run_id=?", (run_id,)
        ).fetchone()

    def director_run_for_date(self, run_date: str) -> dict | None:
        """The run belonging to an Eastern run date, whatever its status.

        The tick's "never twice for one run date" rule reads this, so an invalid or failed
        run counts as the day's run: nothing is retried the same day.
        """
        return self.conn.execute(
            "SELECT * FROM director_runs WHERE run_date=? ORDER BY started_at DESC LIMIT 1",
            (run_date,),
        ).fetchone()

    def latest_director_run(self) -> dict | None:
        """The newest run of any status, which is what ``betting-agent status`` prints."""
        return self.conn.execute(
            "SELECT * FROM director_runs ORDER BY run_date DESC, started_at DESC LIMIT 1"
        ).fetchone()

    def store_director_review(self, run_id: str, *, ended_at: str, page_md: str,
                              page_hash: str, sets_json: str, attempt_reviews: list[dict],
                              cohort_reviews: list[dict]) -> None:
        """Write a valid run's review and flip the run to ``valid``, in one transaction.

        The order is load-bearing: every paragraph and every ranking goes in first and the
        status changes last, inside a single transaction, so no reader can see a valid run
        whose paragraphs are missing. Either the whole review landed, or none of it did and
        the run is still ``running`` for the caller to mark ``failed``.

        ``INSERT OR REPLACE`` on both tables, because their primary keys are the attempt or
        the cohort and the kind: an operator re-running an old day by hand rewrites that
        day's review rather than failing on the key, and the tick never reaches here twice
        for one run date.
        """
        with self._tx() as cur:
            cur.executemany(
                "INSERT OR REPLACE INTO attempt_reviews (attempt_id, kind, run_id, "
                "cohort_date, rank, cohort_size, paragraph) VALUES (?,?,?,?,?,?,?)",
                [(r["attempt_id"], r["kind"], run_id, r["cohort_date"], int(r["rank"]),
                  int(r["cohort_size"]), r["paragraph"]) for r in attempt_reviews],
            )
            cur.executemany(
                "INSERT OR REPLACE INTO cohort_reviews (cohort_date, kind, run_id, "
                "ranking, realized) VALUES (?,?,?,?,?)",
                [(c["cohort_date"], c["kind"], run_id, c["ranking"], c["realized"])
                 for c in cohort_reviews],
            )
            cur.execute(
                "UPDATE director_runs SET status='valid', ended_at=?, page_md=?, "
                "page_hash=?, sets_json=?, error=NULL WHERE run_id=?",
                (ended_at, page_md, page_hash, sets_json, run_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such director run: {run_id}")

    def cohort_review(self, cohort_date: str, kind: str) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM cohort_reviews WHERE cohort_date=? AND kind=?",
            (cohort_date, kind),
        ).fetchone()

    def reviewed_cohorts(self, kind: str) -> set[str]:
        """The cohort dates already reviewed under one kind."""
        return {
            r["cohort_date"] for r in
            self.conn.execute("SELECT cohort_date FROM cohort_reviews WHERE kind=?", (kind,))
        }

    # ------------------------------------------------------------------ shadows (L16)
    # Nothing writes a new shadow bet since docs/22 section 4.6. These two stay because
    # ``settle.py`` still scores the rows already in the live ledger, the same way the
    # reconciliation walk still reads its canary rows.
    def open_shadow_bets(self) -> list[dict]:
        return self.conn.execute(
            "SELECT * FROM shadow_bets WHERE status='open' ORDER BY shadow_bet_id"
        ).fetchall()

    def score_shadow_bet(
        self, shadow_bet_id: str, *, outcome: str, hypothetical_pnl=None, scored_at: str
    ) -> None:
        status = "void" if outcome == "void" else "scored"
        with self._tx() as cur:
            cur.execute(
                "UPDATE shadow_bets SET status=?, outcome=?, hypothetical_pnl=?, "
                "scored_at=? WHERE shadow_bet_id=?",
                (status, outcome, _ser("hypothetical_pnl", hypothetical_pnl),
                 scored_at, shadow_bet_id),
            )
            if cur.rowcount == 0:
                raise LedgerError(f"no such shadow bet: {shadow_bet_id}")

    # ------------------------------------------------------------------ reconciliations (L8)
    def insert_reconciliation(
        self, run_at: str, *, expected_balance, actual_balance, drift, ok: bool,
        detail=None,
    ) -> None:
        with self._tx() as cur:
            cur.execute(
                "INSERT INTO reconciliations (run_at, expected_balance, actual_balance, "
                "drift, ok, detail) VALUES (?,?,?,?,?,?)",
                (run_at, _ser("expected_balance", expected_balance),
                 _ser("actual_balance", actual_balance), _ser("drift", drift),
                 int(ok), detail),
            )

    def latest_reconciliation(self) -> dict | None:
        return self.conn.execute(
            "SELECT * FROM reconciliations ORDER BY run_at DESC LIMIT 1"
        ).fetchone()

    def reconciliations(self) -> list[dict]:
        """Every reconciliation row, oldest first (the absorbed-residual term, 2026-09-27).

        The balance walk carries the drift of every night it absorbed, so it has to be
        able to read what the nights before it concluded. The table holds one to a few
        rows a night, which is a few hundred rows a year, so reading all of them is cheap.
        """
        return self.conn.execute(
            "SELECT * FROM reconciliations ORDER BY run_at"
        ).fetchall()

    # ------------------------------------------------------------------ backup
    def backup(self, dest_dir, keep: int = 30) -> Path:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        base = f"ledger-{utc_now().strftime('%Y%m%d-%H%M')}"
        dest = dest_dir / f"{base}.db"
        i = 1
        while dest.exists():
            dest = dest_dir / f"{base}-{i}.db"
            i += 1
        target = sqlite3.connect(str(dest))
        try:
            with target:
                self.conn.backup(target)
        finally:
            target.close()
        backups = sorted(
            dest_dir.glob("ledger-*.db"), key=lambda p: (p.stat().st_mtime, p.name)
        )
        excess = len(backups) - keep
        for old in backups[:excess] if excess > 0 else []:
            old.unlink()
        return dest


def safe_migrate(db_path, backups_dir) -> dict:
    """Backup -> migrate -> verify (Jul29 spec L1).

    Returns a report dict ``{backup_path, before, after, checks, ok}``; raises
    ``LedgerError`` naming the backup path when any post-migration check fails.

    The expected version and table set come from the schema directory (LG-6), so
    adding ``002_*.sql`` needs no edit here — the old hardcoded ``== 2`` would have
    made the first future migration report failure *after* succeeding.
    """
    expected_version, expected_tables = _schema_expectations()
    # The one caller that must be able to open a file the version guard would refuse:
    # moving it forward is this function's whole job.
    lg = Ledger.open(db_path, check_version=False)
    try:
        def _counts() -> dict:
            out = {"user_version": lg._user_version()}
            for t in ("attempts", "bets", "retrospectives"):
                out[t] = lg.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            return out

        before = _counts()
        backup_path = lg.backup(backups_dir)
        lg.migrate()
        lg.correct_session_exits()   # the one-off record fix, after the schema files
        after = _counts()

        checks = {
            "user_version": after["user_version"] == expected_version,
            "counts": all(
                after[t] == before[t] for t in ("attempts", "bets", "retrospectives")
            ),
        }
        try:
            lg.conn.execute("INSERT INTO ledger_fts(ledger_fts) VALUES('integrity-check')")
            checks["fts"] = True
        except sqlite3.DatabaseError:
            checks["fts"] = False
        have = {
            r["name"]
            for r in lg.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        checks["new_tables"] = expected_tables <= have

        ok = all(checks.values())
        report = {
            "backup_path": str(backup_path), "before": before, "after": after,
            "checks": checks, "ok": ok,
        }
        if not ok:
            raise LedgerError(
                f"migration verification failed: {checks}; backup at {backup_path}"
            )
        return report
    finally:
        lg.close()
