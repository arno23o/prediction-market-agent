-- 008_attempt_activity.sql — what each attempt actually DID (2026-09-11).
--
-- The ledger records what an attempt produced (a ticket, bets, a retrospective) and what it
-- cost (turns, tokens, dollars, wall seconds). It has never recorded what it did: how many
-- markets it looked at, which tools it reached for, how much of the web it read, how long it
-- deliberated before committing to a ticket. Those facts were only ever in the session
-- streams, so a question like "did the attempts that examined many markets do better?" meant
-- reading 200 transcripts by hand — which is to say nobody asked it.
--
-- One row per attempt, keyed by attempt_id, written by `harness/activity.extract_activity`
-- from `data/attempts/<id>/logs/session*.stream.jsonl` plus the attempt's own ticket. Every
-- column is a count, a small JSON blob, or a stamp. NOTHING HERE IS MONEY and nothing here
-- feeds a decision: no cap, no gate, no verdict reads this table. It is measurement, in the
-- same spirit as `bets.resolution_event` (006) — and, like that column, it is deliberately
-- additive, so every earlier row and every earlier reader keeps working untouched.
--
-- Recording is best-effort by construction: the harness's session-end hook wraps the whole
-- extraction in a try/except that logs and never raises, because an attempt that placed real
-- money must never fail over a bookkeeping row.
--
-- Counting conventions worth knowing before you query this table:
--   * markets_probed counts distinct tickers in commands the agent TYPED; markets_seen counts
--     distinct tickers in what came BACK. One `bt board` prints thousands of tickers nobody
--     read, which is why only the first is a measure of attention.
--   * first_commit_frac is the first ticket write's offset from the session's first
--     timestamp, over the attempt's recorded wall_seconds. It is NULL for every attempt whose
--     streams predate Claude Code stamping `timestamp` on assistant records (through A-0076),
--     and it is not clamped: a value above 1 means the wall clock outran the recorded compute
--     (the D9 host-sleep signature), which is a finding, not a value to hide.
--   * passed is 1 when a ticket exists and proposes no bets, 0 when it proposes some, NULL
--     when there is no readable bets.json at all.
--   * Every count column is NULL-free for a row this code wrote, but an absent ROW means "not
--     extracted", which is not the same as zero activity.
--
-- THE CODE STAMP (git_commit, git_dirty, config_sha, prompt_version) is only as honest as
-- when it was taken. A row written by the session-end hook carries the tree the attempt
-- actually ran under. A row written by `betting-agent activity --backfill` carries TODAY's
-- tree, which says nothing about the attempt — so the row records which kind it is in
-- stamped_at_backfill (1 = backfilled, 0 = stamped live at session end), and any analysis
-- lining code changes up against outcomes must filter on it. prompt_version is copied from
-- `attempts` so the row stands on its own; it is the one stamp field that is true either way.

CREATE TABLE attempt_activity (
  attempt_id            TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  extracted_at          TEXT NOT NULL,

  -- the streams this row was read from
  n_streams             INTEGER,
  stream_bytes          INTEGER,

  -- what the session reached for
  tool_calls            TEXT,          -- JSON object: {tool name: count}
  n_tool_calls          INTEGER,
  bt_calls              TEXT,          -- JSON object: {bt subcommand: count}
  n_bt_calls            INTEGER,

  -- what it looked at
  markets_probed        INTEGER,       -- distinct tickers in commands the agent typed
  series_probed         INTEGER,       -- distinct series among those
  series_list           TEXT,          -- JSON array, first 20 by name
  markets_seen          INTEGER,       -- distinct tickers in tool RESULTS
  web_fetches           INTEGER,
  web_searches          INTEGER,
  domains               TEXT,          -- JSON array of hostnames fetched, capped at 50
  n_domains             INTEGER,       -- the true count, even when the list is capped
  code_runs             INTEGER,       -- Bash calls invoking python/node or a script file
  files_written         INTEGER,       -- Write + Edit tool uses
  agent_calls           INTEGER,       -- subagents the session launched

  -- when it committed
  first_ticket_write_ts TEXT,
  first_commit_frac     REAL,

  -- what the ticket says
  cites_predecessor     INTEGER,       -- distinct OTHER attempt ids referenced in ticket/*.md
  playbook_refs         INTEGER,       -- distinct playbook entry numbers referenced
  kill_criteria_n       INTEGER,
  ticket_chars          INTEGER,
  passed                INTEGER CHECK (passed IN (0,1)),
  bets_proposed         INTEGER,

  -- the code and configuration stamp (see the caveat above)
  git_commit            TEXT,
  git_dirty             INTEGER CHECK (git_dirty IN (0,1)),
  config_sha            TEXT,
  prompt_version        TEXT,
  stamped_at_backfill   INTEGER NOT NULL DEFAULT 0 CHECK (stamped_at_backfill IN (0,1))
);

PRAGMA user_version = 9;
