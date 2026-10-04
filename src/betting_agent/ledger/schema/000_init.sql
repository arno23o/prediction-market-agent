-- Ledger schema migration 000 (spec §6.1). Ends by setting PRAGMA user_version = 1.

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE attempts (
  seq              INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id       TEXT NOT NULL UNIQUE,
  created_at       TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'created'
                   CHECK (status IN ('created','running','placed','no_bets',
                                     'ticket_invalid','failed','settled','reviewed')),
  slot             TEXT,            -- the stored form: 'slot:YYYY-MM-DD/HH:MM'
  env              TEXT NOT NULL,   -- 'demo'|'prod'
  model            TEXT NOT NULL,
  effort           TEXT,
  memory_mode      TEXT NOT NULL CHECK (memory_mode IN ('on','off')),
  edge_class       TEXT NOT NULL DEFAULT 'probability'
                   CHECK (edge_class IN ('probability','structural')),
  prompt_version   TEXT NOT NULL,   -- sha256[:12] of rendered prompt template
  toolkit_version  TEXT NOT NULL,   -- package __version__
  variant          TEXT,            -- free-form JSON, experiment label
  context_pack_hash TEXT,
  workspace_path   TEXT NOT NULL,
  claude_session_id TEXT,
  session_exit     TEXT,            -- 'ok'|'error'|'timeout'|'killed'|'env_not_hermetic'
  num_turns        INTEGER, cost_usd TEXT,
  input_tokens     INTEGER, output_tokens INTEGER,
  wall_seconds     INTEGER,
  edge_claim_md    TEXT, hypothesis_md TEXT, manifest_md TEXT,
  error            TEXT
);

CREATE TABLE bet_groups (
  group_id   TEXT PRIMARY KEY,           -- 'A-0042-G1'
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  scenarios  TEXT NOT NULL,              -- JSON scenario table (§9.5)
  declared_worst_case TEXT NOT NULL,     -- Decimal, after fees
  status     TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending','filled','broken','settled')),
  realized_pnl TEXT
);

CREATE TABLE bets (
  bet_id           TEXT PRIMARY KEY,
  attempt_id       TEXT NOT NULL REFERENCES attempts(attempt_id),
  ticket_index     INTEGER NOT NULL,
  ticker           TEXT NOT NULL,
  market_title     TEXT,
  category         TEXT,
  side             TEXT NOT NULL CHECK (side IN ('yes','no')),
  limit_price      TEXT NOT NULL,
  model_prob       TEXT NOT NULL,
  rationale        TEXT NOT NULL,
  is_real          INTEGER NOT NULL DEFAULT 0,
  group_id         TEXT REFERENCES bet_groups(group_id),
  status           TEXT NOT NULL
                   CHECK (status IN ('rejected','no_fill','filled','settled','voided')),
  reject_code      TEXT,
  contracts        INTEGER,
  fill_price       TEXT, stake TEXT, fee TEXT,
  order_id         TEXT, client_order_id TEXT UNIQUE,
  book_snapshot    TEXT,            -- JSON: top 5 levels both sides + ts
  close_ts         TEXT, expected_resolution_ts TEXT,
  placed_at        TEXT, settled_at TEXT,
  outcome          TEXT CHECK (outcome IN ('win','loss','void')),
  pnl              TEXT
);
CREATE UNIQUE INDEX bets_attempt_ticker
  ON bets(attempt_id, ticker) WHERE status != 'rejected';

CREATE TABLE retrospectives (
  attempt_id       TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  created_at       TEXT NOT NULL,
  grader_model     TEXT NOT NULL,
  grader_session_id TEXT,
  hypothesis_grade TEXT NOT NULL
                   CHECK (hypothesis_grade IN ('confirmed','refuted','mixed','unresolvable')),
  verdict          TEXT NOT NULL
                   CHECK (verdict IN ('right_for_stated_reason','lucky','unlucky',
                                      'wrong','unresolvable','no_result')),
  what_went_right  TEXT, what_went_wrong TEXT,
  lessons          TEXT,            -- JSON array of strings
  summary          TEXT NOT NULL,   -- one paragraph, <=120 words
  retro_md         TEXT NOT NULL
);

CREATE TABLE tags (
  attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
  tag        TEXT NOT NULL,         -- normalized: lowercase kebab-case
  source     TEXT NOT NULL CHECK (source IN ('attempt','grader','curator')),
  PRIMARY KEY (attempt_id, tag, source)
);

CREATE TABLE playbook (
  entry_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','retired')),
  lesson     TEXT NOT NULL,
  evidence   TEXT NOT NULL,         -- JSON array of attempt_ids, len >= 1
  evidence_count INTEGER NOT NULL,
  notes      TEXT
);

CREATE TABLE playbook_proposals (
  proposal_id INTEGER PRIMARY KEY AUTOINCREMENT,
  attempt_id  TEXT NOT NULL REFERENCES attempts(attempt_id),
  created_at  TEXT NOT NULL,
  proposal    TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending'
              CHECK (status IN ('pending','merged','rejected'))
);

CREATE TABLE audit_log (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  ts     TEXT NOT NULL,
  event  TEXT NOT NULL,             -- enumerated in §14
  attempt_id TEXT, bet_id TEXT,
  detail TEXT                       -- JSON
);

CREATE VIRTUAL TABLE ledger_fts USING fts5(
  attempt_id UNINDEXED, kind, content, tokenize='porter unicode61'
);

PRAGMA user_version = 1;
