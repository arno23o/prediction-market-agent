-- 003_verdict_grid.sql — the verdict grid widening (docs/14 D4, from docs/12 §8.6).
--
-- Two enum widenings on `retrospectives`, both expressed as CHECK constraints, and
-- SQLite cannot ALTER a CHECK — so the table is rebuilt in place:
--
--   hypothesis_grade += 'variance_consistent'  — the loss sits inside the model's own
--     stated variance and the precondition held (the A-0035 shape; grader.md documents
--     it). It is a statement about the model's stated variance, so a blind grader can
--     reach it without seeing the money.
--   verdict          += 'mixed_loss'  — a losing attempt whose mechanism graded `mixed`.
--     Collapsing it into `wrong` is what manufactured the 17-losses/zero-unlucky
--     signature the blinding arm exists to measure; the arm would have inherited it.
--
-- Rebuild order is rename → create → copy → drop, which is safe with foreign_keys=ON
-- inside the migration's single transaction: `retrospectives` is a child of `attempts`
-- and no table references it, so nothing else's REFERENCES clause moves.
--
-- Historical rows are copied VERBATIM. Re-deriving them is a separate, audited,
-- owner-run pass (`betting-agent rederive-verdicts`) — never a silent side effect of a
-- schema migration.

ALTER TABLE retrospectives RENAME TO retrospectives_pre_d4;

CREATE TABLE retrospectives (
  attempt_id       TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
  created_at       TEXT NOT NULL,
  grader_model     TEXT NOT NULL,
  grader_session_id TEXT,
  hypothesis_grade TEXT NOT NULL
                   CHECK (hypothesis_grade IN ('confirmed','refuted','mixed',
                                               'variance_consistent','unresolvable')),
  verdict          TEXT NOT NULL
                   CHECK (verdict IN ('right_for_stated_reason','lucky','unlucky',
                                      'wrong','mixed_loss','unresolvable','no_result')),
  what_went_right  TEXT, what_went_wrong TEXT,
  lessons          TEXT,            -- JSON array of strings
  summary          TEXT NOT NULL,   -- one paragraph, <=120 words
  retro_md         TEXT NOT NULL
);

INSERT INTO retrospectives (attempt_id, created_at, grader_model, grader_session_id,
                            hypothesis_grade, verdict, what_went_right, what_went_wrong,
                            lessons, summary, retro_md)
SELECT attempt_id, created_at, grader_model, grader_session_id, hypothesis_grade,
       verdict, what_went_right, what_went_wrong, lessons, summary, retro_md
FROM retrospectives_pre_d4;

DROP TABLE retrospectives_pre_d4;

PRAGMA user_version = 4;
