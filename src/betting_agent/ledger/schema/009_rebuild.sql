-- 009_rebuild.sql: the rebuild (docs/22 section 4), applied in one step.
--
-- The learning loop is replaced: five model jobs become one daily director, seventeen
-- experimental arms become four cells, and trading on the account outside the harness
-- becomes a term in the balance walk instead of an audit row nobody reads. This file
-- carries the whole schema move so the ledger goes from 9 to 10 once, rather than in four
-- steps spread over four review cycles. Later phases fill the columns added here; a column
-- nothing writes yet is NULL, which is the same thing it was before the column existed.
--
-- Nothing is dropped. Tables that stop being written (retrospectives, tags, playbook,
-- shadow_*, deep_reviews, recipes) stay exactly as they are and remain readable history.
--
-- Two tables are rebuilt in place, because SQLite cannot ALTER a constraint: `bets` (to
-- drop NOT NULL from `model_prob`) and `sessions` (to widen the `kind` CHECK). Both follow
-- 007's rename, create, copy, drop pattern inside this file's single transaction.
--
-- Rebuild order note: `sessions` is rebuilt BEFORE `director_runs` is created, because
-- `director_runs.session_id` references it and a RENAME with foreign_keys=ON rewrites the
-- REFERENCES clauses of tables that already point at the renamed one. Rebuilding first
-- means there is nothing pointing at it yet.

-- ------------------------------------------------------------------ attempts (4.1)
-- The cell an attempt was planned for, the cell it actually rendered, and whether an
-- operator forced it. `cell_effective` differs from `cell` only when a director or focused
-- slot found no valid director run and ran as static.
ALTER TABLE attempts ADD COLUMN cell TEXT;
ALTER TABLE attempts ADD COLUMN cell_effective TEXT;
ALTER TABLE attempts ADD COLUMN cell_forced INTEGER DEFAULT 0;
-- The era an attempt belongs to: 'pilot' before real money, 'live-v1' the arms era,
-- 'live-v2' from the rebuild on. Stamped by create_attempt from history.current_era.
ALTER TABLE attempts ADD COLUMN era TEXT;
-- What the attempt was shown and what it said at the end.
ALTER TABLE attempts ADD COLUMN example_ids TEXT;      -- JSON list of attempt ids, in order
ALTER TABLE attempts ADD COLUMN direction_hash TEXT;   -- sha256[:12] of the page text, or NULL
ALTER TABLE attempts ADD COLUMN session_summary TEXT;  -- the session's closing paragraph

-- The era boundary is the live genesis instant recorded in meta on 2026-07-31. It is
-- written literally here rather than read from meta because a migration must produce the
-- same ledger on every machine, including a fresh one that has no genesis row at all.
UPDATE attempts
   SET era = CASE WHEN created_at < '2026-07-31T05:44:29Z' THEN 'pilot' ELSE 'live-v1' END;

-- ------------------------------------------------------------------ bets (4.2)
-- Two changes, both needing the table rebuilt, so they happen together in one CREATE.
--
-- `model_prob` loses NOT NULL. Section 4.2 keeps the column and stops writing it: the
-- probability field leaves the ticket, the historical rows stay readable, and every new
-- row carries NULL there. A NOT NULL column cannot hold NULL, and 009 is the one
-- migration this rebuild gets, so the constraint has to go now rather than when phase two
-- stops passing the value.
--
-- `reject_reason` arrives beside `reject_code`. The code stays the machine key; this is
-- the plain sentence a person or a model reads, and it is what makes a silent exchange
-- refusal (the August residency block) visible on the row rather than only in an audit
-- detail nobody was reading.
--
-- Rebuild by 007's own pattern: rename, create, copy, drop, inside this file's single
-- transaction. Safe with foreign_keys=ON, for 007's reason: `bets` is a CHILD of
-- `attempts` and `bet_groups` and NOTHING references `bets`, so no other table's
-- REFERENCES clause moves. Every other CHECK is carried over unchanged, every row is
-- copied verbatim, and the partial unique index is recreated after the copy.

ALTER TABLE bets RENAME TO bets_pre_rebuild;

CREATE TABLE bets (
  bet_id           TEXT PRIMARY KEY,
  attempt_id       TEXT NOT NULL REFERENCES attempts(attempt_id),
  ticket_index     INTEGER NOT NULL,
  ticker           TEXT NOT NULL,
  market_title     TEXT,
  category         TEXT,
  side             TEXT NOT NULL CHECK (side IN ('yes','no')),
  limit_price      TEXT NOT NULL,
  model_prob       TEXT,            -- NULL from the rebuild on (section 4.2)
  rationale        TEXT NOT NULL,
  is_real          INTEGER NOT NULL DEFAULT 0,
  group_id         TEXT REFERENCES bet_groups(group_id),
  status           TEXT NOT NULL
                   CHECK (status IN ('rejected','no_fill','filled','settled','voided')),
  reject_code      TEXT,
  reject_reason    TEXT,            -- the plain sentence beside the code
  contracts        INTEGER,
  fill_price       TEXT, stake TEXT, fee TEXT,
  order_id         TEXT, client_order_id TEXT UNIQUE,
  book_snapshot    TEXT,            -- JSON: top 5 levels both sides + ts
  close_ts         TEXT, expected_resolution_ts TEXT,
  placed_at        TEXT, settled_at TEXT,
  outcome          TEXT CHECK (outcome IN ('win','loss','void','scalar')),
  pnl              TEXT,
  hypothetical_outcome TEXT
                   CHECK (hypothetical_outcome IN ('win','loss','void')),
  hypothetical_pnl TEXT,
  hypothetical_scored_at TEXT,
  declared_contracts INTEGER,
  resolution_event TEXT
);

INSERT INTO bets (bet_id, attempt_id, ticket_index, ticker, market_title, category,
                  side, limit_price, model_prob, rationale, is_real, group_id, status,
                  reject_code, contracts, fill_price, stake, fee, order_id,
                  client_order_id, book_snapshot, close_ts, expected_resolution_ts,
                  placed_at, settled_at, outcome, pnl, hypothetical_outcome,
                  hypothetical_pnl, hypothetical_scored_at, declared_contracts,
                  resolution_event)
SELECT bet_id, attempt_id, ticket_index, ticker, market_title, category,
       side, limit_price, model_prob, rationale, is_real, group_id, status,
       reject_code, contracts, fill_price, stake, fee, order_id,
       client_order_id, book_snapshot, close_ts, expected_resolution_ts,
       placed_at, settled_at, outcome, pnl, hypothetical_outcome,
       hypothetical_pnl, hypothetical_scored_at, declared_contracts,
       resolution_event
FROM bets_pre_rebuild;

DROP TABLE bets_pre_rebuild;

CREATE UNIQUE INDEX bets_attempt_ticker
  ON bets(attempt_id, ticker) WHERE status != 'rejected';

-- ------------------------------------------------------------------ sessions (4.3)
-- `kind` gains 'director'. SQLite cannot ALTER a CHECK, so the table is rebuilt in place
-- following the pattern of 003 and 007: rename, create, copy, drop, all inside this file's
-- single transaction. Every old kind stays legal so the history reads unchanged.
ALTER TABLE sessions RENAME TO sessions_pre_director;

CREATE TABLE sessions (
  session_id   TEXT PRIMARY KEY,
  attempt_id   TEXT REFERENCES attempts(attempt_id),   -- NULL for the director and curator
  kind         TEXT NOT NULL CHECK (kind IN
               ('attempt','ideation','critic','implementation',
                'grader','curator','deep_review','director')),
  model        TEXT NOT NULL,
  started_at   TEXT NOT NULL, ended_at TEXT,
  exit         TEXT, num_turns INTEGER, cost_usd TEXT,
  input_tokens INTEGER, output_tokens INTEGER,
  wall_seconds INTEGER, error TEXT,
  api_retries  INTEGER, throttle_errors INTEGER, error_kinds TEXT
);

INSERT INTO sessions (session_id, attempt_id, kind, model, started_at, ended_at, exit,
                      num_turns, cost_usd, input_tokens, output_tokens, wall_seconds,
                      error, api_retries, throttle_errors, error_kinds)
SELECT session_id, attempt_id, kind, model, started_at, ended_at, exit,
       num_turns, cost_usd, input_tokens, output_tokens, wall_seconds,
       error, api_retries, throttle_errors, error_kinds
FROM sessions_pre_director;

DROP TABLE sessions_pre_director;

-- ------------------------------------------------------------------ new tables (4.4)
CREATE TABLE cell_plans (
  day        TEXT PRIMARY KEY,        -- Eastern date, YYYY-MM-DD
  seed       INTEGER NOT NULL,
  plan       TEXT NOT NULL,           -- JSON list of cell names, one per slot in schedule order
  created_at TEXT NOT NULL
);

CREATE TABLE director_runs (
  run_id       TEXT PRIMARY KEY,      -- 'D-' + run date, e.g. D-2026-09-20
  run_date     TEXT NOT NULL,         -- Eastern date the run belongs to (the day whose slots it directs)
  cohort_date  TEXT NOT NULL,         -- the cohort reviewed prospectively (run_date - 1)
  started_at   TEXT NOT NULL, ended_at TEXT,
  session_id   TEXT REFERENCES sessions(session_id),
  model        TEXT NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('running','valid','invalid','failed')),
  page_md      TEXT,
  page_hash    TEXT,                  -- sha256[:12] of page_md
  sets_json    TEXT,                  -- the validated sets.json
  error        TEXT
);

CREATE TABLE attempt_reviews (
  attempt_id   TEXT NOT NULL REFERENCES attempts(attempt_id),
  kind         TEXT NOT NULL CHECK (kind IN ('prospective','retrospective')),
  run_id       TEXT NOT NULL REFERENCES director_runs(run_id),
  cohort_date  TEXT NOT NULL,
  rank         INTEGER NOT NULL,      -- 1 = best in the cohort
  cohort_size  INTEGER NOT NULL,
  paragraph    TEXT NOT NULL,
  PRIMARY KEY (attempt_id, kind)
);

CREATE TABLE cohort_reviews (
  cohort_date   TEXT NOT NULL,
  kind          TEXT NOT NULL CHECK (kind IN ('prospective','retrospective')),
  run_id        TEXT NOT NULL REFERENCES director_runs(run_id),
  ranking       TEXT NOT NULL,        -- JSON list of attempt ids, best first
  realized      TEXT,                 -- JSON list of attempt ids ordered by realized net, retrospective only
  PRIMARY KEY (cohort_date, kind)
);

-- Orders placed on the account outside the harness, written by settle's shared-account
-- scan and read by the balance walk. Before this table the walk could not explain such an
-- order, so one placed in August sat as unexplained drift and blocked a deposit. `cost`
-- is contracts times price summed over the order's fills and does NOT include the fee,
-- which is its own column: the walk subtracts both, exactly once each.
CREATE TABLE personal_orders (
  order_id           TEXT PRIMARY KEY,
  ticker             TEXT NOT NULL,
  side               TEXT NOT NULL CHECK (side IN ('yes','no')),
  created_time       TEXT NOT NULL,
  contracts          TEXT,            -- 4dp decimal string, fractional fills allowed
  cost               TEXT,            -- contracts x price summed over fills, 4dp
  fee                TEXT,            -- 4dp
  fee_source         TEXT CHECK (fee_source IN ('exchange','computed')),
  on_harness_ticker  INTEGER NOT NULL DEFAULT 0,
  settled_at         TEXT,
  payout             TEXT,            -- 4dp
  first_seen_at      TEXT NOT NULL
);

-- ------------------------------------------------------------------ attempt_activity (4.5)
-- `bt past` is the new history tool, counted the way `bt` calls already are.
ALTER TABLE attempt_activity ADD COLUMN past_calls INTEGER;
ALTER TABLE attempt_activity ADD COLUMN past_subcommands TEXT;  -- JSON {subcommand: count}

PRAGMA user_version = 10;
