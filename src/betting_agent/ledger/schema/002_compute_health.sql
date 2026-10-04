-- 002_compute_health.sql — docs/14 D11 §7 (Aug-9 throughput spec).
-- Capture-once/audit-forever telemetry for the compute path: the audit stays local and
-- deterministic (no network, no re-parsing of streams that retention has compressed), so
-- what the stream says about throttling is written onto the session row at close.
-- Additive only: every column is nullable, and sessions recorded before this migration
-- keep NULL — which the audit renders as "not instrumented", never as zero.

ALTER TABLE sessions ADD COLUMN api_retries INTEGER;      -- system/api_retry records
ALTER TABLE sessions ADD COLUMN throttle_errors INTEGER;  -- 429/529/rate-limit/overloaded
ALTER TABLE sessions ADD COLUMN error_kinds TEXT;         -- JSON {kind: count}, sorted keys

PRAGMA user_version = 3;
