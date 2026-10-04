-- 006_correlation.sql — docs/14 D7 (Aug-9 throughput spec), from docs/12 §8.7/§9.14.
--
-- `bet_groups` only models STRUCTURAL hedges (declared scenarios, worst-case sizing) —
-- it has never had a row for the far more common case of several PROBABILITY bets that
-- just happen to share one real-world resolution event (A-0056's seven one-event legs,
-- A-0058's three legs on one match). That correlation passed every gate as independent
-- bets because nothing in the schema could even name it.
--
-- `bets.resolution_event` is that name: an optional free string a leg MAY declare
-- (the ticket validator passes it through unexamined — see harness/validate.py). Reports,
-- the grader packet, and audit §3 roll legs up by it (total stake, worst case if every
-- leg of the event loses). Measurement only, per Arno's ruling 2026-08-10: no cap, no
-- rejection, and nothing here changes what a ticket can do — a leg that declares nothing
-- (every leg, on every ticket written before this migration) behaves exactly as before.
--
-- Additive: one nullable column, no CHECK, no index (the table is small; a GROUP BY over
-- it costs nothing at this scale). Old code and old rows keep working unchanged.

ALTER TABLE bets ADD COLUMN resolution_event TEXT;

PRAGMA user_version = 7;
