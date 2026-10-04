-- 010_credits.sql: money the exchange gives the account (2026-09-21).
--
-- Kalshi credited $0.01 on 2026-09-20 04:45 as "Incentive+: Volume Incentive For Event
-- KXRAINDNYC-260919". It is real money in the balance, it was never a fill, a settlement,
-- a deposit or an order placed outside the harness, and no API reports it: it is visible only in
-- the app under Account, Activity, Credits. The balance walk therefore had no term for it,
-- and it arrived as a cent of drift the nightly reconciliation could not explain.
--
-- These credits recur whenever the account trades a market in an incentive program, so the
-- answer is a term rather than a one-off correction. One row per credit, entered by hand
-- with `betting-agent credit` from what the app shows, read by the walk over rows at or
-- after genesis and by the digest's account section.
--
-- `amount` is a 4dp decimal string like every other money column here, and it is always
-- positive: a credit only ever adds. `kind` is free text ('incentive' is the only one seen)
-- and deliberately carries no CHECK, because the next programme the exchange invents must
-- not need a migration to be recorded. `reason` is the exchange's own words, copied so the
-- row can be matched against the app months later. `credited_at` is when the exchange says
-- it paid; `recorded_at` is when the owner typed it in, and the two are days apart on a
-- credit nobody noticed at once.
--
-- The anchor is deliberately NOT moved for a credit, which is what separates this from a
-- deposit: a deposit is money the owner put in and the walk cannot see it coming, so the
-- genesis balance absorbs it, while a credit is an event with a date and the walk adds it
-- as a flow. Recording one is therefore safe at any time, because the next reconciliation
-- either explains the cent or does not and nothing has been absorbed either way.

CREATE TABLE credits (
  credit_id    INTEGER PRIMARY KEY,
  credited_at  TEXT NOT NULL,        -- when the exchange says it paid (ISO 8601 UTC)
  amount       TEXT NOT NULL,        -- 4dp decimal string, always positive
  kind         TEXT NOT NULL,        -- 'incentive', or whatever the exchange calls it next
  reason       TEXT,                 -- the exchange's own words, verbatim
  recorded_at  TEXT NOT NULL         -- when this row was entered
);

PRAGMA user_version = 11;
