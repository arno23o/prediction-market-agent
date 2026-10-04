-- 004_nofill_scoring.sql — docs/14 D12 (Aug-9 throughput spec), from docs/12 §7/§8.
--
-- Calibration has been judged on the FILLED subset of every ticket, which is not a
-- random sample of what the attempt proposed: an order fills only when the book was at
-- or better than our limit, so the filled legs are adversely selected relative to the
-- book we declared. Four independent deep reviews asked for the same fix. These columns
-- carry the counterfactual for the legs that carried no position, so the grader packet
-- can show the whole proposed book.
--
-- Additive and nullable throughout. Old code keeps working against the new schema, and
-- rows written before this migration read back NULL — which every renderer shows as
-- "not scored", never as zero.
--
-- HYPOTHETICAL COLUMNS ARE NOT MONEY. They parallel `outcome`/`pnl`/`settled_at` and are
-- deliberately named apart from them so that no query summing the record can pick one up
-- by accident: `pnl` remains the metric of record, and settlement, group realization,
-- attempt P/L and the reconciliation walk never read anything below. Populated by
-- `settle._settle_nofills` at the same point in the pass, and by the same market
-- resolution, as the shadow-bet scoring it borrows (Jul29 spec L16).
ALTER TABLE bets ADD COLUMN hypothetical_outcome TEXT
                 CHECK (hypothetical_outcome IN ('win','loss','void'));
ALTER TABLE bets ADD COLUMN hypothetical_pnl TEXT;      -- 4dp TEXT, net of the D5 fee
ALTER TABLE bets ADD COLUMN hypothetical_scored_at TEXT;

-- The declared size of a leg no order was ever sent for. A `no_fill` row already records
-- its intended count in `contracts`; a CAP-REJECTED row deliberately does not (every
-- execution field on it is NULL — it records an intention, not a position, and `contracts`
-- is one of them). That left the grader with no way to see the size of a leg the daily or
-- per-market cap refused, which is the A-0058 case: the caps kept 1 of 3 legs on one match
-- and nothing told the grader the portfolio it was judging was not the one proposed.
-- Written only where it is otherwise absent; NULL everywhere else.
ALTER TABLE bets ADD COLUMN declared_contracts INTEGER;

PRAGMA user_version = 5;
