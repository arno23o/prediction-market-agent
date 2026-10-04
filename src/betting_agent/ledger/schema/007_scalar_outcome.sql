-- 007_scalar_outcome.sql — `bets.outcome` learns 'scalar' (docs/16 §5).
--
-- Some markets settle with `market_result: "scalar"`: the exchange pays a value of its
-- own choosing per contract and KEEPS the fee. KXNPBTOTAL-26AUG130500HIRYAK-12 (a game
-- total on a shortened game) settled that way on 2026-08-15 and paid $0.82 on A-0097-B01's
-- 1-contract NO position. `settle.py` knew win/loss/void only, so it booked a void —
-- stake refunded, fee zeroed, P/L $0.00 — against an exchange-true +$0.0568, and the
-- nightly balance walk HALTed the live system on the difference.
--
-- `scalar` is a fourth settled outcome, not a flavour of the other three. It is
-- deliberately NOT 'win': the payout is not $1, the position did not resolve to our side,
-- and every calibration/Brier/win-rate population in the codebase filters on
-- `outcome IN ('win','loss')` — a scalar row belongs outside those populations exactly as
-- a void does, and naming it apart is what keeps it out of them.
--
-- SQLite cannot ALTER a CHECK, so the table is rebuilt in place, following 003's pattern:
-- rename -> create -> copy -> drop, inside the migration's single transaction. Safe with
-- foreign_keys=ON: `bets` is a CHILD of `attempts` and `bet_groups` and NOTHING references
-- `bets`, so no other table's REFERENCES clause moves.
--
-- The new table carries every column the old one had, in order, including those added by
-- 004 (the three hypothetical_* counterfactual columns and declared_contracts) and 006
-- (resolution_event) — folded into the CREATE rather than re-ALTERed, so a fresh ledger
-- and an upgraded one end up with byte-identical schemas. `hypothetical_outcome`'s own
-- CHECK stays ('win','loss','void'): it scores a leg that carried NO position, and a
-- counterfactual has no exchange revenue to read a scalar value out of (settle.py scores
-- such a leg 'void' at zero and says so).
--
-- Every row is copied VERBATIM. No historical outcome is re-derived here: the one live row
-- this migration exists for is corrected by an explicit, audited, owner-run command
-- (`betting-agent correct-scalar-settlement`), never as a silent side effect of a schema
-- change.
--
-- The unique partial index is recreated after the copy (a rebuild drops it with the old
-- table); the PRIMARY KEY and the UNIQUE on client_order_id come back with the CREATE.

ALTER TABLE bets RENAME TO bets_pre_scalar;

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
FROM bets_pre_scalar;

DROP TABLE bets_pre_scalar;

CREATE UNIQUE INDEX bets_attempt_ticker
  ON bets(attempt_id, ticker) WHERE status != 'rejected';

PRAGMA user_version = 8;
