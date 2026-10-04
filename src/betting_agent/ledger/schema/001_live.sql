-- 001_live.sql — Jul29 revision (spec 06-revision-spec-jul29.md §1, L1).
-- Live-money era: arm columns, recipes, sessions, shadow candidates/bets,
-- deep reviews, reconciliations, proposal provenance.

ALTER TABLE attempts ADD COLUMN recipe_id TEXT;
ALTER TABLE attempts ADD COLUMN loop_mode TEXT NOT NULL DEFAULT 'one'
  CHECK (loop_mode IN ('one','two'));
ALTER TABLE attempts ADD COLUMN priors_mode TEXT
  CHECK (priors_mode IN ('on','off'));
ALTER TABLE attempts ADD COLUMN grader_blind INTEGER
  CHECK (grader_blind IN (0,1));
ALTER TABLE attempts ADD COLUMN playbook_version INTEGER;

CREATE TABLE recipes (
  recipe_id  TEXT PRIMARY KEY,          -- sha256[:12] of canonical knobs JSON
  created_at TEXT NOT NULL,
  knobs      TEXT NOT NULL              -- canonical JSON, sorted keys
);

CREATE TABLE sessions (
  session_id   TEXT PRIMARY KEY,
  attempt_id   TEXT REFERENCES attempts(attempt_id),   -- NULL for curator
  kind         TEXT NOT NULL CHECK (kind IN
               ('attempt','ideation','critic','implementation',
                'grader','curator','deep_review')),
  model        TEXT NOT NULL,
  started_at   TEXT NOT NULL, ended_at TEXT,
  exit         TEXT, num_turns INTEGER, cost_usd TEXT,
  input_tokens INTEGER, output_tokens INTEGER,
  wall_seconds INTEGER, error TEXT
);

CREATE TABLE shadow_candidates (
  attempt_id   TEXT NOT NULL REFERENCES attempts(attempt_id),
  candidate_id TEXT NOT NULL,
  rank         INTEGER,                 -- critic rank, 1 = chosen first
  chosen       INTEGER NOT NULL DEFAULT 0,
  thesis       TEXT NOT NULL,
  payload      TEXT NOT NULL,           -- the candidate's full JSON
  PRIMARY KEY (attempt_id, candidate_id)
);

CREATE TABLE shadow_bets (
  shadow_bet_id TEXT PRIMARY KEY,       -- 'A-0042-C2-S01'
  attempt_id    TEXT NOT NULL REFERENCES attempts(attempt_id),
  candidate_id  TEXT NOT NULL,
  ticker        TEXT NOT NULL,
  side          TEXT NOT NULL CHECK (side IN ('yes','no')),
  limit_price   TEXT NOT NULL,
  model_prob    TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','scored','void')),
  outcome       TEXT CHECK (outcome IN ('win','loss','void')),
  hypothetical_pnl TEXT,                -- net of fee at limit price, 1 contract
  scored_at     TEXT
);

CREATE TABLE deep_reviews (
  attempt_id  TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  created_at  TEXT NOT NULL,
  model       TEXT NOT NULL,
  session_id  TEXT,
  decisions   TEXT NOT NULL,            -- decisions.json verbatim
  review_md   TEXT NOT NULL,
  proposals   TEXT,                     -- proposals.json verbatim
  grade_deltas INTEGER,                 -- # blind grades the reviewer revised
  status      TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('done','failed'))
);

CREATE TABLE reconciliations (
  run_at           TEXT PRIMARY KEY,
  expected_balance TEXT NOT NULL,
  actual_balance   TEXT NOT NULL,
  drift            TEXT NOT NULL,       -- actual - expected, 4dp
  ok               INTEGER NOT NULL,    -- 1 iff drift == 0 and checks pass
  detail           TEXT                 -- JSON walk breakdown
);

ALTER TABLE playbook_proposals ADD COLUMN source TEXT NOT NULL
  DEFAULT 'grader' CHECK (source IN ('grader','deep_review'));

PRAGMA user_version = 2;
