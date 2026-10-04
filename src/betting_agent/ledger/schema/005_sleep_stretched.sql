-- 005_sleep_stretched.sql -- docs/14 D9 (the A-0053 shape, docs/12 §3): a session's own
-- ``wall_seconds`` is a monotonic elapsed time that does not advance while the host
-- sleeps, so a real wall-clock span materially longer than the recorded ``wall_seconds``
-- is the signature of a session paused mid-flight (a laptop lid), not a slow session.
-- Captured once per session at close (harness/attempt.py's ``_launch``) so every later
-- reader can see it without re-deriving it. Additive: NULL on every attempt recorded
-- before this migration and on any attempt whose gap never crossed the ten-minute bound.

ALTER TABLE attempts ADD COLUMN sleep_stretched INTEGER CHECK (sleep_stretched IN (0,1));

PRAGMA user_version = 6;
