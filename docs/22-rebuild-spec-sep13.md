# The rebuild spec, version 2

*Status: implemented and live since 2026-09-17; later changes are recorded in the code and config.*

*2026-09-13. Version 2 supersedes version 1 of the same date and the plans in docs/20 sections g, h, i, k and L. It incorporates every decision Arno made on 2026-09-13 in the two follow-up rounds after docs/21. Line references are to commit a5c5232 on main. Three files in `docs/drafts/` are normative parts of this spec: `attempt-slim-sep13.md` (the task prompt), `principles-sep13.md` (the page that replaces the priors page), and `director-sep13.md` (the director's brief).*

## 0. Summary

The system keeps its money path, its board reader, its toolkit and its ledger, and replaces its learning loop. Five model jobs (grader, curator, deep review, two-loop ideation and critic) become one: a daily Fable director that reviews each day's attempts as a ranked cohort, twice, writes one page of direction, and chooses which past attempts the next attempts read. Seventeen experimental cells become four (baseline, static, director, focused), drawn in a stored random order over nine Opus slots a day. A new `bt past` tool makes the history of attempts as queryable as the board and serves records, never the old grader's judgments. The task prompt drops from 9,508 characters to about 3,100 and the 74,509-character context pack is deleted; an attempt reads about 12,000 characters before it looks at a market, most of them past attempts. The horizon goes to five days, the model chooses one to three contracts a bet, the daily cap rises to $25, and the board stops retrieving what can never be bet. The halt stops only betting; a small unexplained drift alerts instead of halting; the drawdown floor reads a fresh balance and never refuses silently. The probability field leaves the ticket for now, to be revisited in two weeks.

## 1. Principles

- The base prompt does not tilt toward any family or shape of bet. The model decides what to look at, how many bets to place, and how large.
- Steering happens only at the director layer, where it is dated, logged on every attempt row, and measured against two control cells.
- Arno sets throughput and structure. The director directs attention and nothing else.
- Everything the loop judges by is something the world settled: a fill, a refusal, a resolution, a profit. The models' own claims are not signals.
- Where a choice is between a rule and a record, prefer the record. Bad attempts are read too; the rankings and paragraphs tell them apart.
- Delete, do not tidy. Tables that stop being written stay as history. Code that has no caller goes.

## 2. Inventory

### 2.1 Deleted

**Modules.** `harness/retro.py`, `harness/curator.py`, `harness/deep_review.py`, `harness/salvage.py`, `harness/report.py`, `harness/canary.py`, `harness/audit.py` (after its section 4 invariants move to `harness/invariants.py`), `ledger/search.py` (replaced by `ledger/history.py`), `prompts/grader.md`, `prompts/curator.md`, `prompts/deep_review.md`.

**Inside kept modules.**

- `harness/attempt.py`: the two-loop surface named in the survey: `_run_two_loop` (817-999) with its closures `_phase`, `_finish`, `_not_hermetic`; `_candidates_from`, `_pass_reasoning`, `_ranking_from`, `_write_fallback_ranking`, `_sketch_tickers`, `_chosen_candidate`, `_record_shadows`, `_archive_task`, `_sum_opt`, `_sum_cost`, `_read_json`, `_WS`, `_PHASES`, the three `_CRITIC_*` constants, the `loop` and `phase_models` parameters of `run_attempt`, the two-loop branches of `_planned_models` and `_model_substitutions_for`, the `declared_choice` and `shadow_bet_id` imports, the `playbook_version` read (1087-1097), the dead `{memory_mode}` substitution (1157), `_MEMORY_SECTION`, `_PRIORS_HEADER`, `_PRIORS_MARKER`, the recipe knobs and `insert_recipe` call (1111-1136), the `ledger.add_tags` call (757).
- `harness/validate.py`: codes V06, V08, V09, V12, V13, V14, V15; `_group_errors`, `_scenario_errors`, `_candidate_errors`, `_declaration_errors`, `GroupSpec`, `ValidatedGroup`, the group half of `_extract`, `_V_DECLARATION`, `ParsedTicket.tags`, `ParsedTicket.defects`, the `edge_class`, `groups`, `candidates` and `chosen` keys of `_TOP_ALLOWED`, `_GID_RE`, `_MAX_GROUPS`, `_MAX_CANDIDATES`, `_EDGE_HEADINGS`'s old entries.
- `harness/execute.py`: the group snapshot and realization paths (`group_broken` at 469 and 512 and everything that serves only groups), the group branch of `_leg_contracts`, the `order_insufficient_funds` trio (308-333, replaced by the reject reason on the row), `stakes.real_bets_per_attempt` handling (532).
- `harness/reconcile.py`: the 26-line duplicate impostor scan named in docs/19 section 4A (settle's scan is the one kept).
- `moneymath.py`: `contracts_for`, `group_contracts`.
- `bt.py`: `ledger search`, `ledger attempt`, `ledger summaries`, `scoreboard`, `playbook`, `review reveal`, `_gate_memory`, `_loop_context`, and the `loop_mode` and `candidate_ids` arguments of `ticket validate`.
- `cli.py`: the commands `canary`, `audit`, `salvage-reviews`, `deep-review`, `correct-scalar-settlement`, `retro`, `rederive-verdicts`, `curate`, `report`, `ledger search`, `ledger show`; the functions `_arms_enabled`, `_arms_for`, `_arms_status`, `_assign_arms`, `_deep_review_if_due`, `_daily_report`, `_audit_if_due`; the tick steps `audit`, `retro`, `curate`, `daily_report`, `deep_review`; the early HALT exit at 1108-1110; the `--loop` and `--memory` options of `attempt`; the `deep_reviews` clauses in the GC query (552, 565); the arms and needs-retro lines of `status`.
- `config.py`: the sections and keys in section 3.1, `ARM_CYCLE`, `arm_cycle`, `recipe_label`, `_model_letter`, `_MIXED_RECIPE`, `_LOOP_DIGIT`, `PHASE_MODEL_PHASES`, `ScheduleSettings.phase_models_for`, the coprimality and phase-model warnings in `config_warnings`.
- `ledger/db.py`: the writers listed in section 4.6; `_LEGAL_TRANSITIONS` keeps `reviewed` so old rows read.
- `harness/activity.py`: `_PLAYBOOK_NUM_RE` and `playbook_refs` counting (the column stays, NULL from now on).
- `sessions.py`: nothing.
- `board.py`: nothing removed.

**Tests.** `test_retro.py`, `test_curator.py`, `test_deep_review.py`, `test_audit.py`, `test_report.py`, `test_canary.py`, `test_schedule_recipes.py`, `test_search.py`; the two-loop, critic and recipe cases in `test_attempt.py`, `test_cli.py`, `test_e2e.py`, `test_activity.py`, `test_ledger_db.py`, `test_model_substitution.py`, `test_era.py`; the V06, V08, V09, V12, V13, V14, V15 and group cases in `test_validate.py` and `test_execute.py`; `fixtures/activation-config.toml` is rewritten to the new config.

**Documents.** `docs/04-revision-proposal.md` and `docs/05-revision-spec.md` (superseded); `docs/scaffold-triage.md` (retires with salvage).

### 2.2 Archived, not deleted

`prompts/ideation.md`, `prompts/critic.md`, `prompts/implementation.md` move to `docs/archive/prompts/` beside a one-page `two-loop.md` that records the setup (the three phases, the global Sonnet critic, the X2 recipe, the shadow ledger) and the measured result (docs/20 section d: $21.69 against $8.30 per attempt, $17.61 against $4.13 per settled leg, calibration identical, the shadow ledger unable to show the critic adds value). The `shadow_candidates` and `shadow_bets` tables stay in the ledger. The eighteen dated documents move to `docs/archive/`. `docs/priors.md` moves to `docs/archive/priors-jul29.md` when `docs/principles.md` lands.

### 2.3 Kept

The placement core in `execute.py` with its write-down ordering and the order-ambiguity rescan; `settle.py` whole, including refused-leg scoring and the account scan; `reconcile.py` with its walk, three cross-checks and provisional gate; `safety.py`; `notify.py`; `board.py`; the `bt` listing and detail lenses and `ticket validate`; `sessions.py`; `activity.py`; `ledger/db.py`; `moneymath.py` fee and profit arithmetic; `timeutil.py`; `ids.py`; the Kalshi client and types; the tick spine, its locks, the reaper, gc and the daily backup; the six ledger invariants; the model substitution switch; every ledger table and every existing test not named above.

### 2.4 New

`harness/director.py` (workspace build, session, validation, storage), `harness/cells.py` (the daily plan and the per-cell rendering of CONTEXT.md and the prompt placeholders), `harness/digest.py`, `harness/invariants.py`, `ledger/history.py` (records, outcomes, families, search), `prompts/director.md`, `docs/principles.md`, `ledger/schema/009_rebuild.sql`, the `bt past` group, the CLI commands `director`, `board-refresh` and `plan`, the `--cell` option of `attempt`, and the tests in section 14.

## 3. Configuration

`config.py` accepts unknown keys and warns about them on every command (`extra="allow"` at `config.py:106` and `377`, reported by `unknown_keys` at `config.py:547`), so every new key below needs its field added to the matching `_Section` class, and every removed key needs its field removed so a stale `config.toml` line warns rather than silently steering. `config.toml` is rewritten in full; the current file carries an expired substitution (`config.toml:58-59`) and two past-due audit reminders (`config.toml:92-98`).

### 3.1 Removed

| Section and key | Today | Why it goes |
|---|---|---|
| `schedule.slot_models`, `slot_loops`, `slot_phase_models` and `ScheduleSettings.phase_models_for` | per-slot recipe grid | one model, one loop |
| `limits.min_edge`, `limits.group_min_worst_case`, `limits.excluded_categories` | `0.03`, `0.01`, `[]` | V09 and the structural schema go; category exclusion moves to the board |
| `stakes.sizing_mode`, `stakes.real_bets_per_attempt` | `one_contract`, `2` (ignored) | the ticket declares contracts |
| `attempt.memory`, `memory_ab`, `memory_off_period`, `priors_ab`, `k_candidates`, `critic_model` | the arms and two-loop knobs | cells replace arms; two-loop goes |
| `[grader]` entire section | model, turns, `blind_ab`, `zero_bet_delay_hours` | no grader session |
| `[curator]`, `[context]`, `[deep_review]`, `[audit]` entire sections | | deleted components |
| `[models].substitute`, `substitute_until` | expired 2026-08-22 | kept as a mechanism (section 3.3), the stale lines removed |
| `ARM_CYCLE`, `arm_cycle()`, the coprimality warning at `config.py:689`, the `slot_phase_models` warnings at `config.py:705-730` | | no arms |

### 3.2 Changed

| Key | Was | Becomes |
|---|---|---|
| `schedule.slots` | 01:00 03:00 05:00 08:00 11:00 15:00 17:00 20:00 23:00 | 01:00 03:40 06:20 09:00 11:40 14:20 17:00 19:40 22:20 (section 5.1) |
| `schedule.slot_grace_min` | 90 | 90 |
| `limits.max_resolve_hours` | 72 | 120 |
| `limits.max_bets_per_attempt` | 20 | 20 (not shown in the prompt) |
| `stakes.daily_real_stake_cap` | `"15.00"` | `"25.00"` |
| `stakes.per_market_real_cap` | `"2.00"` | `"4.00"`, scoped to the charge day |
| `attempt.model` | `claude-sonnet-5` default, per-slot overrides | `claude-opus-5`, every slot |
| `attempt.wall_time_min` | 90 | 90 |
| `board.generations_keep` | 2 | 1 |
| `board.refresh_min_interval_min` | 60 | 150 |
| `reconcile.hour_et` | 23 | 23 |

### 3.3 Added

```toml
[stakes]
max_contracts_per_bet = 3

[reconcile]
halt_drift_usd = "2.00"
halt_after_nights = 3

[board]
close_bound_hours = 120
excluded_series = ["KXMVECROSS"]
excluded_categories = ["Sports", "Entertainment"]

[cells]
# Per day, in a stored random order over the slots.
baseline = 1
static = 4
director = 4
focused = 1
static_recent = 10

[director]
model = "claude-fable-5"
hour_et = "00:00"
max_turns = 120
max_budget_usd = "40.00"
wall_time_min = 45
set_size = 10
page_max_chars = 4000

[history]
era_default_min = 50
current_era = "live-v2"

[models]
# The substitution switch stays available for a quota outage; leave it empty otherwise.
substitute = {}
```

`director.hour_et` is a string `"HH:MM"` in Eastern time, parsed like a slot. `cells.*` must sum to `len(schedule.slots)`; `config_warnings` checks it. The `[models]` block keeps `Settings.model_substitutions`, `substitution_active` and `effective_model` (`config.py:399-450`) unchanged, and the director resolves its model through `effective_model` like every other session.

### 3.4 The complete `config.toml` after the change

```toml
# Operating configuration (2026-09, rebuild spec docs/22).

[schedule]
slots = ["01:00", "03:40", "06:20", "09:00", "11:40", "14:20", "17:00", "19:40", "22:20"]
slot_grace_min = 90

[attempt]
model = "claude-opus-5"
effort = "high"
wall_time_min = 90

[limits]
max_resolve_hours = 120
max_bets_per_attempt = 20

[stakes]
live_trading = true
daily_real_stake_cap = "25.00"
per_market_real_cap = "4.00"
max_contracts_per_bet = 3
drawdown_floor_pct = "0.50"

[reconcile]
hour_et = 23
halt_drift_usd = "2.00"
halt_after_nights = 3

[board]
refresh_min_interval_min = 150
generations_keep = 1
close_bound_hours = 120
excluded_series = ["KXMVECROSS"]
excluded_categories = ["Sports", "Entertainment"]

[cells]
baseline = 1
static = 4
director = 4
focused = 1
static_recent = 10

[director]
model = "claude-fable-5"
hour_et = "00:00"
max_turns = 120
max_budget_usd = "40.00"
wall_time_min = 45
set_size = 10
page_max_chars = 4000

[history]
era_default_min = 50
current_era = "live-v2"

[alerts]
enabled = true
```

## 4. Ledger changes

One migration, `src/betting_agent/ledger/schema/009_rebuild.sql`, ending in `PRAGMA user_version = 10;`, applied by `betting-agent migrate` (which backs up first through `safe_migrate`, `ledger/db.py:1402`, and holds both locks). Nothing is dropped; tables that stop being written stay as read-only history.

### 4.1 `attempts`

New columns, all nullable, added with `ALTER TABLE`:

| Column | Type | Meaning |
|---|---|---|
| `cell` | TEXT | `baseline`, `static`, `director`, `focused`; the planned cell |
| `cell_effective` | TEXT | the cell actually rendered; differs from `cell` only when no valid director run existed and the slot ran as static |
| `cell_forced` | INTEGER DEFAULT 0 | 1 when `betting-agent attempt --cell` bypassed the plan |
| `era` | TEXT | `pilot`, `live-v1`, `live-v2` |
| `example_ids` | TEXT | JSON list of the attempt ids rendered into CONTEXT.md, in order |
| `direction_hash` | TEXT | sha256[:12] of the direction text shown, NULL when none |
| `session_summary` | TEXT | the closing paragraph of the session, `SessionResult.result_text`, the final text of the stream (`sessions.py:88`) |

Backfill inside the migration: `era = 'pilot'` where `created_at < '2026-07-31T05:44:29Z'`, else `'live-v1'`. New attempts are stamped `history.current_era` by `create_attempt`. The existing columns keep meaningful values so old queries still run: `memory_mode` is `'off'` for baseline and `'on'` otherwise, `priors_mode` the same, `loop_mode` `'one'`, `grader_blind` NULL, `recipe_id` NULL. `attempts.status` keeps its enum; new attempts end at `placed` or `no_bets` and move to `settled` through `_settle_attempts` (`settle.py:463`); `reviewed` is no longer written.

### 4.2 `bets`

- `reject_reason` TEXT, nullable: the plain-English reason beside `reject_code`.
- `contracts` already exists (INTEGER, `000_init.sql`) and `declared_contracts` (`004_nofill_scoring.sql:32`); both are used as today, with values 1 to 3 from the ticket.
- `model_prob` stays as a column (NULL from now on); `_BET_COLS` (`ledger/db.py:82`) keeps it so old rows still round-trip; `insert_bet` stops receiving it.

### 4.3 `sessions`

`kind` is rebuilt to `CHECK (kind IN ('attempt','ideation','critic','implementation','grader','curator','deep_review','director'))` by the copy-rename pattern of `003_verdict_grid.sql` and `007_scalar_outcome.sql`. Old kinds stay valid so history reads.

### 4.4 New tables

```sql
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
```

### 4.5 `attempt_activity`

Two columns added: `past_calls` INTEGER (count of `bt past` invocations in the session's Bash calls, detected the way `bt_calls` is) and `past_subcommands` TEXT (JSON object of counts by subcommand). `_ACTIVITY_COLS` (`ledger/db.py:110`) gains both; `betting-agent activity --backfill` fills zeros for old rows.

### 4.6 What stops being written

`retrospectives`, `tags`, `playbook`, `playbook_proposals`, `deep_reviews`, `shadow_candidates`, `shadow_bets`, `recipes`, and the session kinds `ideation`, `critic`, `implementation`, `grader`, `curator`, `deep_review`. Their writer functions in `ledger/db.py` (`persist_retro`, `insert_retrospective`, `relabel_verdicts`, `amend_retrospective`, `add_tags`, `insert_playbook_proposal`, `playbook_add`, `playbook_revise`, `playbook_retire`, `set_proposal_status`, `insert_recipe`, `insert_shadow_candidate`, `insert_shadow_bet`, `score_shadow_bet`, `insert_deep_review`, `delete_deep_review`, `set_deep_review_grade_deltas`, `mark_curation_done`) are deleted with their callers. `meta.arm_counter` and `meta.memory_ab_counter` stop being read.

### 4.7 The full-text index

`ledger_fts` (kinds and content written by `fts_upsert`, `ledger/db.py:433`) keeps indexing `edge_claim.md` and `hypothesis.md` under the new headings, plus `session_summary` as a new kind. It stops indexing retro summaries and lessons for new attempts; the old rows remain searchable by `bt past search` only when `--era all` is given, and even then the search returns records, never the old lessons text.

## 5. The attempt

### 5.1 The day

Nine slots, every two hours and forty minutes: 01:00, 03:40, 06:20, 09:00, 11:40, 14:20, 17:00, 19:40, 22:20 Eastern. The last slot's session ends by 23:50 when it starts on time; with the 90-minute grace window it can start as late as 23:50 and end after 01:00.

The director step runs at the first tick on or after `director.hour_et` (00:00) once no attempt of the previous day is still `running`, and unconditionally at the first tick on or after `hour_et + 60` minutes; an attempt still running at that point is left out of the prospective review and is reviewed retrospectively with its cohort. The 01:00 slot spawns whether or not the director has finished; an attempt always renders the latest valid page and sets, which on that morning may be the previous day's. That is the standing-direction design working as intended, not a race.

Cell plans are drawn at the first tick on or after 00:00 Eastern for that day (section 8.7) and stored before either the director or the first slot runs, so `betting-agent status` can print the day's plan in advance.

### 5.2 The runner

`run_attempt` (`attempt.py:1003`) keeps its one-loop shape and loses the `loop`, `phase_models`, `memory`, `priors_mode` and `grader_blind` parameters in favour of one: `cell`. Its steps become:

1. Normalize: `now`, `model = settings.attempt.model` (Opus), `effort`, `env`, `cell` (from the day's plan, or `--cell`).
2. `prompt_version = _sha12(prompts/attempt.md bytes)`; `toolkit_version` as today.
3. `create_attempt(...)` with `memory_mode = 'off' if cell == 'baseline' else 'on'`, `priors_mode` the same, `loop_mode = 'one'`, `era = settings.history.current_era`, `cell`, `cell_forced`.
4. Workspace: `data/attempts/<id>/{workspace,ticket,logs}` as today.
5. Render the cell's context through `cells.render(ledger, settings, cell)`, which returns `(context_md | None, memory_section, principles_section, example_ids, direction_hash, cell_effective)`. Write `CONTEXT.md` when non-None, record `context_pack_hash = sha12(context_md)`, `example_ids`, `direction_hash`, `cell_effective`, and `principles_hash` in `variant`.
6. Substitutions: `{attempt_id}`, `{env}`, `{window_hours}`, `{memory_section}`, `{priors_section}` only. `{max_bets}`, `{min_edge}`, `{memory_mode}` and `{k_candidates}` are gone from `_render_task`'s `subs` and from the template. `_render_task`'s sequential replace stays (the template has literal JSON braces).
7. Reserve `session_id`, transition to `running`, audit `attempt_launched` with `{slot, model, cell, cell_effective, session_id, example_ids, direction_hash, model_substitutions}`.
8. Render `TASK.md`, run the session with `kind='attempt'`, `BT_PAST = 'off' if cell == 'baseline' else 'on'`, `BT_ATTEMPT_ID`, `BT_ROOT`, `disallowed_tools='Skill'`, `add_dirs=[attempt_dir]`, the attempt envelope (`max_turns`, `wall_time_min` 90, `max_budget_usd`).
9. `set_session_result(...)` as today, plus `session_summary = result.result_text` (the session's final text, which the prompt asks to be a one-paragraph summary; `SessionResult.result_text` at `sessions.py:88` already carries it). Write it through `fts_upsert(attempt_id, 'closing', text)` as well.
10. `record_activity` before `_intake`, as today.
11. `_intake` as today minus `add_tags`, `edge_class`, and the two-loop branches. Terminal statuses stay `placed`, `no_bets`, `ticket_invalid`, `failed`.

`betting-agent attempt` (`cli.py:1302`) gains `--cell {baseline,static,director,focused}` (operator-forced, sets `cell_forced = 1`) and loses `--loop` and `--memory`. `_spawn_attempt` (`cli.py:683`) passes `--slot` and `--cell`. `_run_slots` (`cli.py:768`) looks the cell up in the day's plan by slot index instead of reading `slot_models` and `slot_loops`.

### 5.3 The prompt

`prompts/attempt.md` is replaced by the text in `docs/drafts/attempt-slim-sep13.md`, about 3,100 characters. Its structure: the goal (make money; how is the model's to decide), the ticket (three files), the account (horizon, immediate-or-cancel, sizing, workspace, toolkit), six helpful guidelines, the `{memory_section}{priors_section}` line, and the closing-summary instruction. Every listing lens is named once so `tests/test_prompt_contracts.py:216-224` stays green; that test's parametrization shrinks to `attempt.md` alone, its `subs` dict (`:238-260`) drops `min_edge`, `memory_mode` and `k_candidates`, and its cache-convention assertion (`:227-235`) keeps the four literals, which the new text carries.

`{memory_section}` renders by cell (section 8.5). `{priors_section}` renders `docs/principles.md` whole (no marker, no header line) for static, director and focused, and empty for baseline; `_priors_section` becomes `_principles_section` and reads the file from the top. A missing file audits `principles_missing` and the attempt runs without it.

### 5.4 The ticket

`edge_claim.md` must carry exactly `## Markets`, `## Why this is profitable`, `## Why the opportunity exists and persists` (`_EDGE_HEADINGS`, `validate.py:76-81`, matched by `_has_headings` as whole stripped lines). `hypothesis.md` is unchanged: `## If we're right`, `## If we're wrong`, `## Kill criteria`. `MANIFEST.md` is neither required nor mentioned; if present it is stored in `manifest_md` as today and nothing reads it.

`bets.json`: `{"attempt": "A-NNNN", "bets": [...]}`; `_TOP_ALLOWED = {"attempt", "bets"}`. Each bet: `_BET_REQUIRED = {"ticker", "side", "limit_price", "contracts", "rationale"}`, `_BET_ALLOWED = _BET_REQUIRED | {"resolution_event"}`. `limit_price` keeps `_PRICE_RE` (a string of exactly four decimals). `contracts` is a JSON integer 1 to `stakes.max_contracts_per_bet`. `rationale` 1 to 400 characters, `ticker` 1 to 80, `resolution_event` 1 to 80. Unknown keys at any level still void the ticket under V01. `_MAX_BETS = 50` stays as the parse bound; `limits.max_bets_per_attempt = 20` stays as the execution bound.

### 5.5 The validator

Eight codes, each with a plain reason string that both `bt ticket validate` and the `ticket_invalid` audit carry:

| Code | Checks | Scope |
|---|---|---|
| V01 | `bets.json` missing, unreadable, or off-shape (unknown keys, wrong types, bad price string, contracts out of range is V10 not V01) | whole ticket, fatal |
| V02 | a required heading missing in `edge_claim.md` or `hypothesis.md` | whole ticket, fatal |
| V03 | duplicate ticker among the ticket's bets, later index loses | leg |
| V04 | market missing or not open | leg |
| V05 | no close time, or `min(close_time, expected_expiration) > now + limits.max_resolve_hours` (120) | leg |
| V07 | `limit_price` outside [0.01, 0.99] or off the tick band | leg |
| V10 | `contracts` not an integer in 1 to `stakes.max_contracts_per_bet` | leg |
| V11 | order book unobtainable, or ask depth on the side below the leg's `contracts` | leg |

V11 changes from "below 1 contract" (`validate.py:724`) to "below the leg's contracts", so a three-contract bet on a one-contract book is refused before it is sent rather than partially filled. Truncation past `max_bets_per_attempt` stays silent in the validator and audited as `ticket_truncated` by execute (`execute.py:672`); there is no V-code for it. `ValidatedBet.contracts` comes from the ticket. `_check_leg` keeps first-failure-wins.

### 5.6 Execution and recording

`execute_attempt` (`execute.py:654`) is unchanged in shape. Per leg: `contracts` from the ticket; stake projection at the limit price times contracts (`execute.py:562`); the daily cap and the day-scoped per-market cap (section 7.6); the live gate and the fresh-balance floor (section 7.4); immediate-or-cancel at the limit price with `client_order_id = bet_id`; the ambiguity rescan; the bet row with `contracts`, `fill_price`, `stake`, `fee`, `order_id`, `status`, and now `reject_reason` on every refusal. `declared_contracts` keeps carrying the intended size on refused rows.

Recorded on the attempt row: `cell`, `cell_effective`, `cell_forced`, `era`, `example_ids`, `direction_hash`, `context_pack_hash`, `session_summary`, `prompt_version`, `toolkit_version`, `variant` (`principles_hash`, `model_substitutions`), the session cost and token fields, `session_exit`, `wall_seconds`. Recorded on the activity row: everything in migration 008 plus `past_calls` and `past_subcommands`. `playbook_refs` is left NULL.

### 5.7 Sessions

Every attempt session is `claude-opus-5` through `settings.effective_model`, `effort = high`, `max_turns = 250`, `max_budget_usd = 30.00`, `wall_time_min = 90`, `--permission-mode bypassPermissions` with `--safe-mode` and `--disallowedTools Skill` (`sessions.py:102-133`), `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS = 60000`, the hermeticity check on `memory_paths` and `mcp_servers` (`sessions.py:420-434`). The workspace rule in the prompt is a stated rule, not a sandbox; this is unchanged and stated here so nobody assumes otherwise. `sessions.kind` gains `director`; the `SessionSpec.kind` docstring at `sessions.py:60` is corrected to the full list.

## 6. The board

The pull is `refresh_board_cache` (`board.py:569`), whose `_Pull.__iter__` (`board.py:550`) calls `client.iter_markets(status=status, limit=_PAGE_CAP)` at `board.py:555` without a close-time bound, although `Client.iter_markets` already accepts `max_close_ts` (`kalshi/client.py:255`). Categories arrive by per-series enrichment (`_enrich_categories`, `board.py:443`), not on the market payload. Four changes:

1. **Time bound.** Pass `max_close_ts = now + board.close_bound_hours` (120 hours) into `iter_markets` for the `open` status. The `settled` status keeps its own `settled_max` cap. The bettable window and the retrieval window are the same number.
2. **Series exclusion.** In `_Pull.__iter__`, skip any market whose `series_of(ticker)` (`board.py:218`) is in `board.excluded_series`. The parlay family `KXMVECROSS` is the first entry. Excluded series do not appear in the generation at all, including in `series_rollup` (`board.py:708`); the header names them.
3. **Category exclusion.** After `_enrich_categories`, delete rows whose category is in `board.excluded_categories` before the generation is finalized, and count them. The header states the excluded categories, the count removed, and the exchange's verbatim refusal text, which is stored once in `meta.category_refusal_text` and set by the operator with `betting-agent board-refresh --set-refusal-text "..."`. Politics is not on the list.
4. **Lock and cadence.** The board step leaves the tick's critical path. The tick spawns `betting-agent board-refresh` as a detached child (the pattern of `_spawn_attempt`, `cli.py:641-690`) when the newest generation is older than `board.refresh_min_interval_min` (150) and `board.lock` is free; the child holds `board.lock` for its duration and the tick never waits on it. `generations_keep` becomes 1; `rotate` (`board.py:205`) already handles it.

`Board.label()` (`board.py:688`) gains one line: `bound: markets closing within 120 hours; excluded series: KXMVECROSS; excluded categories: Sports, Entertainment (N removed)`, which `_banner` (`bt.py:347`) prints on every listing. `board.statuses`, `category_lookup_cap`, `max_markets` and `settled_max` are unchanged. The expected effect is the one docs/20 estimated: from about 1.5 GB and 33 minutes per generation to tens of megabytes and about ten minutes, since only the time bound is server-side.

## 7. The money path

Every change here is to policy around placement, not to the order write-down ordering in `harness/execute.py`, which stays as it is. Money-path diffs get Arno's eyes before they land.

### 7.1 What the halt gates

Today `tick` exits at the top when `data/HALT` exists (`cli.py:1108-1110`), before taking `tick.lock`, so nothing runs. The change: delete that early exit. The halt is then enforced in exactly three places, all of which already exist: `_may_spawn` (`cli.py:1056`) refuses to spawn attempt sessions and audits `halt_mid_tick` once per tick; `_real_order` (`execute.py:377`) refuses to transmit an order and audits `halt_mid_attempt`; and `real_orders_allowed` (`safety.py:106`) reports the halt as the first reason. The director step is added to the `_may_spawn` gate as well, since it spawns a session. Settle, reap, genesis, reconcile, invariants, backup, gc, board and digest all run under a halt. `betting-agent attempt` keeps its own refusal under HALT (`cli.py:1302`) and its `--force`.

### 7.2 Drift policy

`reconcile_once` (`reconcile.py:1178`) keeps its walk, its three cross-checks, and its provisional gate. The decision at `reconcile.py:1243`, `ok = drift == _ZERO and not failed`, becomes a three-way verdict:

- `exact`: drift is zero and every check passed. Audit `reconcile_ok` as today.
- `small`: no check failed and `0 < |drift| <= reconcile.halt_drift_usd` (default `"2.00"`). Audit `reconcile_drift` with the drift, expected, actual and the walk, raise an alert through `raise_alert` with key `reconcile_drift_small`, write the reconciliations row with `ok = 0`, and do not halt. If the drift to the cent equals the drift of the previous `reconcile.halt_after_nights - 1` consecutive runs (default 3 in total), treat it as `large`: the same unexplained number three nights running is a fact the walk is missing, not noise.
- `large`: any failed cross-check, or `|drift| > halt_drift_usd`, or the repeated-drift rule. Behaviour is today's dirty branch (`reconcile.py:1270-1283`): halt with reason `reconcile_drift`, report, `halt_set`, alert.

The fills watermark (`meta.fills_verified_through`) advances whenever `fills_match` passed, in all three verdicts, not only inside the `ok` branch as at `reconcile.py:1266-1268`. Two new config keys carry the numbers: `reconcile.halt_drift_usd` (Decimal, `"2.00"`) and `reconcile.halt_after_nights` (int, `3`).

### 7.3 Omitted

This section is omitted from the published copy.

### 7.4 The drawdown floor

`_drawdown_refusal` (`safety.py:63`) reads `reconciliations.actual_balance` from the latest row (`safety.py:76`), which is as stale as the last reconciliation, and `_select_real` discards its reason (`execute.py:552`) and leaves every unit paper with no audit row, which is what held seven attempts on Aug 17. Three changes:

- `real_orders_allowed` takes the client and reads a live balance (`client.get_balance()`), falling back to the latest reconciliation only when the call fails, and says which in its reason.
- When the floor refuses, `_select_real` records every unit as a rejected real bet with `reject_code = 'drawdown_floor'` and a plain reason, exactly as cap refusals are recorded (`_cap_rejected`, `execute.py:198`), so settle's `_settle_rejects` scores them. No paper rows are written.
- It audits `floor_stop` once per attempt and raises an alert through `record_failure` with threshold 1 and key `drawdown_floor`.

The floor stays at `stakes.drawdown_floor_pct = 0.50` of `meta.live_genesis_balance`.

### 7.5 Refusal reasons

`bets.reject_code` stays the machine key. A new column `bets.reject_reason` (TEXT) carries one plain sentence, written wherever a code is written: cap refusals (`execute.py:569`, `578`: "daily cap: $23.40 of $25.00 already committed today, this leg needed $1.86"), the floor, exchange refusals (from the `order_error` text at `execute.py:346`, for example the exchange's refusal message verbatim), and validator rejections (the same text `bt ticket validate` prints). `bt past attempt` and the director's `legs.md` show the reason, not the code.

### 7.6 The caps and sizing

- `stakes.daily_real_stake_cap` becomes `"25.00"`, charged to the Eastern day of placement as today (`et_day(place_now)`, `execute.py:701`).
- `stakes.per_market_real_cap` becomes `"4.00"` and is scoped to the charge day. Today `per_market_real_stake` (`ledger/db.py:1056`) sums a ticker's real stake for all time, which would close a ticker permanently once touched; the query gains the same ET-day filter as `daily_real_spend` (`ledger/db.py:1044`).
- `stakes.sizing_mode` and `moneymath.contracts_for` are removed. `_leg_contracts` (`execute.py:170`) returns the ticket's `contracts` for every leg. `stakes.max_contracts_per_bet = 3` bounds it and the validator enforces it (section 5.5). `stakes.real_bets_per_attempt`, already ignored (`execute.py:532`), is removed.
- The projection at `execute.py:562` already multiplies contracts by the limit price; no change.
- `ticket_truncated` (`execute.py:672`) keeps truncating to `limits.max_bets_per_attempt = 20`.

### 7.7 Order of operations at go-live

1. Land 7.1 to 7.5 with tests, reviewed by Arno.
2. Arno runs `betting-agent deposit --amount 100.00`. The command holds `tick.lock`, needs credentials, and does not check HALT (`cli.py:1575`). Expected result: implied and stated agree within a cent; the anchor moves from 50.1622 to 150.1622 and `deposit_recorded` is audited.
3. Arno runs `betting-agent resume`.
4. The first reconcile after resume must come back `exact`. If it comes back `small`, the walk is missing something and the spec's carve-out is incomplete; stop and look before any attempt runs.

## 8. The learning loop

### 8.1 Cohorts

A cohort is the set of attempts whose slot falls on one Eastern calendar day. The slot label already carries the day (`2026-09-20/01:00`), so cohort membership is the slot's date, never the placement timestamp; an attempt that places after midnight still belongs to its slot day. A cohort is complete when every real leg it placed has reached a terminal state (settled or voided) and every refused or unfilled leg has been hypothetically scored. Cohorts with no placed legs are complete the moment their last session ends. Failed sessions with no ticket are members of the cohort and appear in the review with an empty record.

### 8.2 The director session

One session a day, kind `director`, model `director.model` (default `claude-fable-5`, subject to the substitution switch so that it runs on Opus rather than not at all), started by the tick at `director.hour_et` (default `00:00`), after the last slot of the previous day has ended and before the first slot of the new day. Envelope: wall time 45 minutes, cost cap $40, turn cap 120; on breach the run is marked failed and the previous day's page and sets remain the latest available.

The tick prepares a workspace at `data/director/<run_date>/` before spawning:

- `digest.md`: the status digest (section 10) as of the run.
- `cohort/<attempt_id>/`: for every attempt in yesterday's cohort, the three ticket files, `legs.md` (every leg with side, price, contracts, status, fill or refusal and its reason), `activity.md` (the activity row rendered: markets probed, series, fetches, domains, code runs, files written, `bt past` calls, wall time, cost), and `summary.md` (the session's closing paragraph, from `attempts.session_summary`).
- `settled/<cohort_date>/<attempt_id>/`: the same, plus `outcomes.md` (outcome, profit and fee per leg, net per attempt, per family), for every cohort that completed since the previous run and has not yet had a retrospective review.
- `pages/`: the last seven `page.md` files, newest first.
- `TASK.md`: the director brief (section 11 and appendix C), with `{run_date}` and `{cohort_date}` substituted.

The session runs with `BT_PAST=on`, tools Read, Grep, Glob and Bash, and the same workspace confinement rule as attempts. It writes three files into the workspace, which the tick validates and stores:

- `review.json`: `{"prospective": {"cohort": "<date>", "ranking": ["A-…", …], "paragraphs": {"A-…": "…"}}, "retrospective": [{"cohort": "<date>", "ranking": […], "paragraphs": {…}}, …]}`. Every attempt in the cohort must appear exactly once in its ranking and have a paragraph of 200 to 1,500 characters. A retrospective entry is required for every cohort in `settled/`.
- `page.md`: at most 4,000 characters, with exactly the three headings `## Standing direction`, `## Today`, `## Watching`.
- `sets.json`: `{"balanced": ["A-…", …], "balanced_note": "…", "focused": ["A-…", …], "focused_lens": "…", "focused_direction": "…"}`. Each list holds 6 to 12 existing attempt ids; `focused_direction` is 200 to 1,200 characters.

Validation failure of any file marks the run `invalid` with the reason in an audit row `director_invalid`; a valid run is stored in `director_runs` and `attempt_reviews` (section 4). The tick never retries a director run the same day; the next run is the next day's. Nothing downstream blocks on a failed run, because every consumer reads the latest valid run.

### 8.3 The review

Both rankings answer one question: how good was this attempt at maximizing profit and minimizing loss, with a solid, precise process for getting there? The prospective ranking answers it for yesterday's cohort with no outcome known; the retrospective ranking answers it for a completed cohort with outcomes known. The two rankings of a cohort are over the same attempt set by construction, so their rank correlation is defined, and the tick stores beside them a third ordering computed in code, realized net per attempt (ties broken by net per contract), so the retrospective ranking can be compared with pure profit. Nothing in the loop excludes an attempt from being read on the strength of a ranking; the rankings and paragraphs are how good and bad attempts are told apart by the models that read them.

### 8.4 The page

The page has a standing direction that carries forward run to run. The director may change it only by saying why and naming the settled cohorts it is reacting to. The direction addresses attention: families, theses, approaches, information sources, what to avoid and why. It may not set the number of bets, contracts, whether to pass, or any limit; the brief says so, and the tick rejects a page that contains a numeric bet count or contract instruction (a line matching "place N bets", "N contracts", "do not bet today", or similar, checked by a small pattern list that can grow). Every attempt that reads a page records its hash.

### 8.5 The sets and the record format

The balanced set is about ten past attempts chosen for variety across families, approaches and outcomes, with losses and passes included when they teach something. The focused set is about ten attempts chosen through one stated lens. The static cell's set is computed by code: the ten most recently completed attempts (any cell, any era, any outcome; failed sessions with no ticket excluded), newest first.

A record is rendered by one function, `render_record(attempt_id, *, full=False)`, used identically by `bt past attempt`, the director workspace and the attempt's CONTEXT.md, so that what the director sees, what the history tool shows, and what an attempt reads are the same text. The short form is at most 900 characters:

```
A-0187 · 2026-08-29 01:00 · director cell · Politics · KXHORMUZWEEKLY
Claim: <first 320 characters of "Why this is profitable">
Bets: KXHORMUZWEEKLY-25SEP05-T3 no @0.6200 ×2 → filled, won, +$0.72
      KXHORMUZWEEKLY-25SEP05-T5 yes @0.2100 ×1 → refused: daily cap
Kill: <first 160 characters of "Kill criteria">
Net: +$0.72 on $1.45 staked · probed 14 markets · 31 minutes
Review: <the retrospective paragraph if one exists, else the prospective, first 300 characters>
```

Passes render with `Bets: none (passed)` and the first 320 characters of `## Markets`. The full form (`full=True`) is the three ticket files verbatim, every leg, the activity summary, the session summary, and both paragraphs in full; it is what the director's workspace holds and what `bt past attempt` prints.

CONTEXT.md is assembled per cell from these pieces and nothing else:

- static: `## Past attempts (the ten most recent)` followed by ten short records.
- director: `## Direction` with the standing direction and today's note from the latest valid page, then `## Past attempts (chosen for today)` with the balanced set's short records and the `balanced_note`.
- focused: `## Direction` with `focused_direction`, then `## Past attempts (<focused_lens>)` with the focused set's short records.
- baseline: no CONTEXT.md.

The `{memory_section}` placeholder in the task prompt renders, by cell: baseline, "This attempt runs without access to past attempts or guidance. Work from the markets alone."; static, "Study ../CONTEXT.md, which holds the ten most recent attempts, before choosing a target. `bt past` searches the whole history."; director and focused, "Study ../CONTEXT.md first: it carries the day's direction and the past attempts chosen for you. `bt past` searches the whole history." The `{priors_section}` placeholder renders the principles page for static, director and focused, and nothing for baseline.

### 8.6 The retro

There is no retro session and no retrospectives row for new attempts. The retro of an attempt is a view assembled at read time from three sources: the hypothesis from its ticket, the outcome record computed from its bets rows (`outcome_record(attempt_id)`: legs with price, contracts, status, outcome, profit and fee; totals for stake, net, wins over legs; the same per family), and its rows in `attempt_reviews` (the prospective and retrospective paragraphs with their ranks). `bt past attempt` prints all three. The `retrospectives`, `tags` and playbook tables stay in the ledger as read-only history of the old era and are not served by `bt past`.

### 8.7 What each cell sees

| Cell | Per day | CONTEXT.md | Principles page | `bt past` |
|---|---|---|---|---|
| baseline | 1 | none | no | refused (exit 3) |
| static | 4 | ten most recent attempts | yes | allowed |
| director | 4 | standing direction and today's note; balanced set | yes | allowed |
| focused | 1 | focused direction; focused set | yes | allowed |

Cell assignment is a stored daily permutation: at the first tick on or after 00:00 Eastern the tick draws a seed, shuffles the list `[baseline, static, static, static, static, director, director, director, director, focused]` truncated to the day's slot count in schedule order, and stores `(day, seed, plan)` in `cell_plans`. A slot's attempt takes the cell at its index. Operator-forced attempts (`betting-agent attempt --cell …`) bypass the plan and are recorded with `cell_forced = 1`. If no valid director run exists yet, director and focused slots run as static and record `cell_effective = 'static'` beside `cell`.

### 8.8 What feeds what

The direction is fed by completed cohorts (outcomes) and by same-day world facts from yesterday's cohort and the digest (fills, refusals, passes, families being re-swept). The prospective ranking feeds nothing; it exists so that the two rankings and the realized ordering can be compared over time (component-effects entry 4). The principles page is revised by Arno and Claude on a monthly review of the pages and the ledger, never by the director.

## 9. History: `bt past`

A new subcommand group in the existing toolkit, read-only, serving records and never the old grader's judgments. Gated by `BT_PAST` (set by the runner per cell; the director gets `on`); when off, every subcommand exits 3 with "history is not available to this attempt". Every invocation is counted into the attempt's activity row (`past_calls`, and `past_subcommands` as a JSON object of counts).

| Subcommand | Arguments | Output |
|---|---|---|
| `bt past families` | filters | One row per family (the ticker's series prefix): attempts, legs, filled, wins, net, passes, last entry date, and the most common stated pass reason. Sorted by attempts. |
| `bt past family <series>` | filters | Every attempt that entered the family, oldest first: date, cell, claim (first 200 characters), legs with side, price, contracts, fill, outcome, profit; then the family's totals. |
| `bt past search <text>` | filters, `--limit` | Full-text search over `edge_claim.md` and `hypothesis.md` (the existing index, extended to the new headings), one short record per hit, newest first. |
| `bt past attempt <id>` | none | The full record: the three ticket files verbatim, every leg, the outcome record, the activity summary, the session summary, and both director paragraphs with their ranks. |
| `bt past page [--date D]` | none | The director's page for that date, default the latest valid one, with the run date and the model that wrote it. |
| `bt past recent [--limit N]` | filters | The N most recently completed attempts as short records, default 10. This is the static set's query, exposed. |

Shared filters: `--era {current,all}` (default `current` once the current era holds fifty attempts, else `all`; stated in the output header), `--category <name>`, `--outcome {win,loss,refused,nofill,pass}`, `--since <date>`, `--limit N` (default 50 for tables, none for `attempt` and `page`), `--json`. Conventions copied from the markets lenses: text tables by default; `--json` never truncated; a notice on stderr when a table is capped; the ledger opened read-only; exit codes 0, 2 (usage), 3 (disabled), 4 (not found). Not copied: the differing JSON shapes for cached and live output of one command; every `bt past` command has one JSON shape.

`bt past pair <id> <id>` is reserved and not implemented in this version (component-effects entry 7).

## 10. The status digest

One function, `status_digest(ledger, settings, client=None, *, day) -> str`, in a new module `harness/digest.py`, replaces the 107 KB daily report and the eight-section audit as the thing a person or the director reads. It returns Markdown of at most about sixty lines:

- Account: the latest reconciliation's verdict, drift and time; the live balance if a client was given; money committed in open positions and the count; the halt state and reason.
- The day: attempts by cell and status (placed, passed, failed, refused, no-fill), legs proposed, sent, accepted, filled, refused with the top three refusal reasons, contracts placed by size, stake committed against the cap.
- The board: generation age, market count, excluded counts, refusal text.
- Fills and refusals by category for the day and the trailing seven days.
- Settlements since the previous digest: legs, wins, net, and which cohorts completed.
- Compute: sessions and cost by kind for the day, cost per settled leg over the trailing seven days (the standing line docs/19 asked for).
- Liveness: scheduled slots that did not run since the previous digest, with the marker reason (`skipped`, `lost`, `past_grace_sleep`, `past_grace_other`), and the last tick time.
- The six ledger invariants from the audit's section 4 (kept as `harness/invariants.py`, about 120 lines), each PASS or the failing rows.

The tick writes it once a day to `data/status/<date>.md` at the first tick on or after 00:00 Eastern, appends one line to `data/logs/status.log` (`<ts> balance=… open=… attempts=… placed=… halted=…`), and stages the same text into the director's workspace as `digest.md`. `betting-agent status` prints it on demand. Any invariant failure raises an alert through `raise_alert` with key `invariant:<name>`; nothing else in the digest alerts. There is no heartbeat in this version.

## 11. The three texts

The task prompt (`docs/drafts/attempt-slim-sep13.md`) is reproduced at the end of this document as appendix A and is the version to land, byte for byte apart from the leading HTML comment. The principles page (`docs/drafts/principles-sep13.md`, appendix B) is omitted from the published copy. The director's brief (`docs/drafts/director-sep13.md`, appendix C) becomes `prompts/director.md`, rendered into the director workspace as `TASK.md` with `{run_date}` and `{cohort_date}` substituted; the session's bootstrap prompt is `"Read ../TASK.md and carry it out completely."`, the pattern of every other session (`attempt.py:80-83`). The brief asks one question of both rankings, forbids throughput or limit instructions, tells the director that a day's profit column is noise, and requires a standing direction that changes only on settled evidence with the cohorts named.

## 12. Housekeeping

- `docs/decisions.md` gains one entry per pre-registered experiment, each closed with its numbers: one loop versus two (no quality gain, 2.6 times the cost, calibration identical); memory on versus off (unresolvable as run: the pack broke on Aug 17 and 65 percent of memory-on attempts ran against it; replaced by the cells); priors on versus off (banked: the only arm that separated from zero, on a page that named weather; re-measured by the baseline cell against the others); blind grading (null twice, the packet showed the win); the recipe grid (a $4.07 spread inside a $17 noise floor). Each entry names the tables that hold the data and the date the experiment stopped.
- `session_exit` misclassification: `_intake` sets `session_exit` from `SessionResult.exit_kind` only; the kill-versus-timeout ambiguity at `sessions.py:483-484` is resolved by recording `terminal_reason` beside it. A-0117 and A-0162 are corrected by a one-off update audited as `session_exit_corrected`.
- A-0131's "test" retrospective stub is left in `retrospectives` (the table is history) and noted in decisions.md.
- The two past-due `[[audit.reminders]]` and the expired substitution leave `config.toml` (section 3.4). The reminder mechanism's job moves to `docs/checkin-list.md`.
- `README.md`: test count, document list, the account sentence, the command list.
- The `attempts.slot` schema comment (`000_init.sql:12`) is corrected to the stored form `slot:YYYY-MM-DD/HH:MM`.

## 13. Order of work

Four phases, each a reviewable diff on top of a5c5232 with its tests green and ruff clean, landed in order. The system can run live after phase two. Every phase ends with `betting-agent migrate` where a migration is involved (only phase one carries one, and it carries all of 009 at once so the ledger moves in a single step), `pytest`, and Arno's read of the diff.

**Phase one: money and liveness.** Section 7 entire, section 10, section 4 (the whole migration, including the columns phases two and three will fill), two-loop off by configuration (`schedule.slot_loops` removed from `config.toml`; the code still exists at this point), the tick spine changes (halt gating, the `invariants`, `board-refresh` child and `digest` steps, the audit and daily report steps removed), `notify` unchanged. Tests: `test_reconcile.py` gains the three verdicts, the watermark rule, and the deposit path; `test_safety.py` gains the fresh-balance floor and its fallback; `test_execute.py` gains `drawdown_floor` and `reject_reason` rows; `test_cli.py` gains the tick under HALT (settle runs, slots do not) and the detached board child; `test_migration.py` gains 009 in both the fresh and upgrade paths; `test_digest.py` and `test_invariants.py` are new. Acceptance: with `data/HALT` present, `betting-agent tick` settles and reconciles and spawns nothing; `betting-agent deposit --amount 100.00` records; the first reconcile after `resume` returns `exact`.

**Phase two: board and attempt.** Section 6, section 3 (the full config rewrite), section 5 (prompt, ticket, validator, sizing, caps, era stamping, session summary), `docs/principles.md`, the removal of the arms functions and the two-loop code, prompts and tests, the archive. Tests: `test_validate.py` rewritten to the eight codes with `contracts`; `test_execute.py` for sizes 1 to 3, the day-scoped per-market cap, and V11 depth against contracts; `test_board.py` for the bound, the two exclusions and the header line; `test_attempt.py` for the new runner shape and `session_summary`; `test_prompt_contracts.py` for the one prompt; `test_config.py` for the new sections and the cell-count check; `test_activation_config.py` against the rewritten fixture. Acceptance: an attempt run with `--cell static` produces a ticket that validates under the eight codes, places one to three contracts, and lands a row with `cell`, `era`, `example_ids` and `session_summary` filled; the board generation is under 100 MB and its header names the bound and the exclusions.

**Phase three: the loop.** Sections 8 and 9: `ledger/history.py`, `bt past`, `harness/cells.py`, `harness/director.py`, `prompts/director.md`, the `director` and `plan` commands, the tick's `cell_plan` and `director` steps, the removal of retro, curator, deep review, salvage, report, canary and search with their tests and tables' writers, `activity.py`'s `past_calls`. Tests: `test_history.py` (records, outcomes, families, search, era default), `test_cells.py` (the plan is a permutation with the configured counts, the four renderings, the fallback to static, the placeholders), `test_director.py` (workspace build from a seeded ledger, output validation including every rejection case, storage, the latest-valid rule, the failed-run path), `test_bt.py` for `bt past` and the `BT_PAST` gate, `test_activity.py` for `past_calls`, `test_e2e.py` rewritten around one cohort: nine attempts across four cells, a director run, settlement, the retrospective review, and `bt past attempt` printing all of it. Acceptance: two consecutive director runs on a seeded ledger produce a valid page and sets each, the second run's retrospective ranking covers exactly the first day's cohort, and `attempt_reviews` holds two rows per attempt.

**Phase four: housekeeping.** Section 12, the `docs/archive/` move, `docs/decisions.md`, README, the deletion of anything that lost its last caller in phase three (`bt review reveal`, `_gate_memory`, `ledger_fts` kinds `retro`, `summary` and `tags` as write targets), and `docs/checkin-list.md` entries 9 to 13 confirmed against what landed.

Nothing in phase three blocks on phase two being live for a while first; the order is for review load, not dependency. If Arno wants the loop sooner, phases two and three can land as one diff.

## 14. Tests

Today: 1,812 collected across 39 files. The rebuild removes about 2,800 test lines with their modules (`test_retro`, `test_curator`, `test_deep_review`, `test_audit`, `test_report`, `test_canary`, `test_schedule_recipes`, `test_search`, and the two-loop, recipe, arm and group cases inside eight other files) and adds six files (`test_history`, `test_cells`, `test_director`, `test_digest`, `test_invariants`, plus the rewritten `test_e2e`). The count should land near 1,400. Rules the new tests must keep, all of which the existing suite already enforces and `tests/test_migration.py` spells out: every migration tested on both the fresh and upgrade paths with byte-identical `PRAGMA table_info`, one transaction per schema file, idempotent reruns, a real backup before mutation, widened CHECKs that still reject out-of-scope values, FTS integrity after a rebuild; money arithmetic in `Decimal` end to end with `FakeKalshi.balance_ledger()` reconciling to the cent; the prompt contract test pinning every listing lens and the cache convention; `_BT_COMMANDS` in `activity.py` pinned against what `bt` registers.

Specific new assertions worth naming:

- A `small` verdict writes `reconcile_drift`, raises `reconcile_drift_small`, advances the watermark, and does not create `data/HALT`; the same drift three runs running does.
- With `data/HALT` present, `_tick_run` runs settle, reconcile, backup and digest and `_may_spawn` refuses slots and the director.
- The floor refusing writes `drawdown_floor` rows with reasons, audits `floor_stop`, raises the alert, and writes no paper rows; settle later scores those rows.
- `cells.plan(day)` is a permutation of the configured multiset, deterministic under its stored seed, and different across days.
- `cells.render` for each of the four cells produces exactly the CONTEXT.md sections of section 8.5 and the right `BT_PAST` value; director and focused fall back to static with `cell_effective = 'static'` when no valid run exists.
- `director.validate_outputs` rejects a ranking missing an attempt, a ranking with a stranger, a paragraph under 200 characters, a page over 4,000 characters or without the three headings, a page containing "place 2 bets" or "do not bet today", a set with a nonexistent id, and a set outside 6 to 12 ids, each with a distinct reason in `director_invalid`.
- `bt past families` counts passes and their reasons; `bt past family` orders entries by slot day; `bt past attempt` prints both paragraphs with ranks; `bt past search` never returns old `retro`, `summary` or `tags` kinds; `--era` defaults flip at fifty current-era attempts.
- `render_record` output is identical between the history tool, the director workspace and CONTEXT.md for the same attempt.
- `record_activity` counts `bt past family KXHORMUZWEEKLY` as one `past_calls` and `{"family": 1}` in `past_subcommands`.
- The prompt render leaves every non-placeholder brace intact and substitutes exactly the five placeholders.

## 15. Check-ins and deferred items

`docs/checkin-list.md` carries entries 9 to 12 from 2026-09-13 (the placement funnel on 2026-09-23 or ten days after phase two is live; bucket contracts two weeks after the prompt lands; the component-effects list every two weeks; the deposit and the first reconcile after resume) and gains entry 13: two weeks after phase two lands, decide whether `model_prob` returns to the ticket, with the argument for it (calibration was the one measurement that separated arms when profit could not) and against it (Arno's read that the field biases attempts toward modeling and that the attempts have been lacking). `docs/component-effects.md` holds the standing questions, worked every two weeks.

Deferred, by decision: `model_prob` and every calibration measure derived from it; example pairs (`bt past pair`, component-effects entry 7); the heartbeat service; the director's Brier-style probability field from docs/21 section 18.

## 16. Cost

At full attendance: nine Opus one-loop attempts at the ledger's $11.94 average, about $107 a day, plus one director session at $20 to $30, about $130 a day, $3,900 to $4,000 a month. The attempts should come in under that, since today's Opus one-loop attempt reads 85,000 characters and the new surface is about 12,000. Live-era compute averaged $98.50 a day at 6.2 attempts a day, of which $60 a day was two-loop machinery that no longer exists. The digest prints cost per settled leg over the trailing week as its standing line.

## Appendix A. The task prompt (`prompts/attempt.md`)

```markdown
# Attempt {attempt_id}

You are one attempt in a long-running experiment: an autonomous bettor on
Kalshi ({env}). Your goal is to make money. How you find opportunities, what
you look at, which tools and methods you use, how many bets you place and how
large: yours to decide, within the account rules below.

## The ticket

Your session succeeds only if these files exist in `../ticket/` and validate:

1. `edge_claim.md`, with headings exactly `## Markets`, `## Why this is
   profitable`, and `## Why the opportunity exists and persists`. Say why the
   bets you propose will make money and why the market has not already taken
   that away.
2. `hypothesis.md`, with headings exactly `## If we're right`, `## If we're
   wrong`, `## Kill criteria`. Written before you see any outcome.
3. `bets.json`:
   `{"attempt": "{attempt_id}", "bets": [{"ticker": ..., "side": "yes"|"no",
     "limit_price": "0.4200", "contracts": 1, "rationale": "one sentence"}]}`
   `limit_price` is a JSON string of exactly four decimals (`"0.4200"`, never
   `0.42`). `contracts` is 1, 2 or 3. `rationale` is 1 to 400 characters and
   `ticker` 1 to 80. The only other key a bet may carry is `"resolution_event"`
   (1 to 80 characters naming the real-world event the bet turns on).

## The account

- Markets must resolve within {window_hours} hours. Positions are held to
  resolution; there are no exits.
- The harness places your orders after your session ends; you never place
  orders. Orders are immediate-or-cancel at your `limit_price`: a limit below
  the ask does not rest, it simply does not fill.
- Sizing is yours: 1, 2 or 3 contracts a bet. Larger bets can produce more
  profit and put more capital at risk. Daily and per-market caps apply.
- Keep every file inside your workspace. Do not read, list, or enumerate paths
  outside your workspace, `../ticket/`, `../TASK.md` and `../CONTEXT.md`.
- The toolkit is `bt --help`. Listing: `bt markets`, `bt series`, `bt search`,
  `bt new`, `bt movers`, `bt board`, `bt calendar`. Detail: `bt market`,
  `bt book`, `bt history`, `bt fees`, `bt size`. Past attempts: `bt past`. The
  listing commands read a board snapshot the harness refreshes and print its
  capture time; `--live` pulls from the exchange, and `bt book` is always live.

## Helpful guidelines

- Read a market's resolution rules (`bt market <ticker>`) before betting it. A
  large disagreement with a deep two-sided book is usually a misread rule.
- A listing that returns exactly as many rows as its `--limit` is truncated.
  Attempts have called the whole board efficient after seeing 2% of it.
- Never end your turn waiting on background work: the runner kills a session
  that idles 60 seconds, and everything unwritten is lost.
- Write your ticket files as soon as your thesis is settled, then refine them.
- Finish by running `bt ticket validate` and fixing what it flags.
- If nothing clears the bar after honest work, say so in `edge_claim.md` and
  submit an empty bets list. A pass is a valid result.
{memory_section}{priors_section}

End the session with a one-paragraph summary of what you did and why.
```

## Appendix B. The principles page (`docs/principles.md`)

The principles page is omitted from the published copy.

## Appendix C. The director's brief (`prompts/director.md`)

```markdown
# Director run {run_date}

You direct a long-running experiment: an autonomous bettor on Kalshi that runs
{attempts_per_day} attempts a day, each a fresh model session that reads a
short prompt, looks at the board, and proposes bets which the harness places.
The goal of the experiment is profit. Arno sets the throughput and the
structure. You direct attention: what the next attempts should look at, and
which past attempts they should read.

## What is in this workspace

- `digest.md`: the state of the account and the day: balance, open positions,
  fills and refusals by category, passes, halts, slot liveness, cost.
- `cohort/`: every attempt from yesterday's cohort ({cohort_date}), one folder
  each, with its ticket files, its legs with fills, refusals and reasons, its
  activity summary (what it probed, fetched and ran), and its own closing
  summary. None of these has settled yet.
- `settled/`: every cohort that completed since the last run, in the same form,
  with outcomes and profit per leg and per attempt.
- `pages/`: your previous pages, newest first.
- `bt past`: the history tool. `bt past --help` lists it. `bt past families`
  is the map; `bt past family <series>` is every entry into one family; `bt past
  attempt <id>` is one full record; `bt past search <text>` searches claims.

## What you write

1. `review.json`. For yesterday's cohort, a prospective ranking: every attempt
   id, best first, with one paragraph each. For each cohort in `settled/`, a
   retrospective ranking in the same form. Both answer the same question: how
   good was this attempt at maximizing profit and minimizing loss, with a solid,
   precise process for getting there? The prospective ranking answers it without
   the outcome; the retrospective ranking answers it with the outcome known.
   Results and process both count, and process facts (whether it read the
   rules, priced the fee, checked the book, researched the question) count as
   they bear on profit. Say in each paragraph what the attempt did, what it got
   right or wrong, and what a future attempt should take from it. Bad attempts
   are instructive too; say what not to repeat.
2. `page.md`, one page, three headings. `## Standing direction`: where the next
   attempts should look and what they should try, as families, theses,
   approaches and sources of information. It carries forward from your previous
   page unless you change it; when you change it, say why and name the settled
   cohorts whose outcomes you are reacting to. `## Today`: what you saw in
   yesterday's cohort and in the settlements, in a few lines. `## Watching`:
   what would change your mind.
3. `sets.json`. `balanced`: about ten attempt ids for the next attempts to read,
   varied across families, approaches and outcomes, including losses and passes
   when they teach something, with a one-line `balanced_note` on why this set.
   `focused`: about ten ids chosen through one lens that you state in
   `focused_lens` (one family, the most profitable fifth, the attempts whose
   reasoning you rate highest, or another single lens), with a one-paragraph
   `focused_direction` for the one attempt that will read them.

## File shapes and limits

Every check below is applied exactly as written, and a run that fails any one of
them is discarded whole: no page, no sets, no review. Read this section again
before you finish.

`review.json` is one object with two keys:

    {"prospective": {"cohort": "{cohort_date}",
                     "ranking": ["A-0187", "A-0188"],
                     "paragraphs": {"A-0187": "...", "A-0188": "..."}},
     "retrospective": [{"cohort": "YYYY-MM-DD",
                        "ranking": ["A-0170"],
                        "paragraphs": {"A-0170": "..."}}]}

- `prospective.cohort` is exactly `{cohort_date}`.
- `prospective.ranking` holds every attempt id in `cohort/`, each of them once,
  best first, and no other id.
- `prospective.paragraphs` has one entry per id in the ranking, each between
  200 and 1,500 characters.
- `retrospective` is a list with one entry per folder in `settled/`, each naming
  that folder's date and following the same two rules over the attempt ids
  inside it. It is `[]` when `settled/` is empty.

`page.md`:

- At most 4,000 characters, counting every character in the file.
- Exactly three `##` headings, in this order: `## Standing direction`,
  `## Today`, `## Watching`. A `#` title line above them is allowed; a fourth
  `##` heading anywhere in the page is not.
- No line of the standing direction may set a number of bets, contracts,
  positions or markets, or tell an attempt to pass.

`sets.json` is one object with five keys:

    {"balanced": ["A-0187", "A-0188"], "balanced_note": "...",
     "focused": ["A-0187", "A-0190"], "focused_lens": "...",
     "focused_direction": "..."}

- `balanced` and `focused` each hold 6 to 12 attempt ids, no id twice in a list,
  and every id must name an attempt that exists.
- `focused_direction` is between 200 and 1,200 characters.
- `balanced_note` and `focused_lens` are both non-empty.

## Rules

- Judge by what the world settled: fills, refusals, resolutions, profit. The
  models' own stated confidence is not evidence.
- {attempts_per_day} attempts a day at one to three contracts a leg means a
  day's profit column is mostly noise. Say so when it is, and do not steer on
  it.
- You direct attention only. You do not set how many bets an attempt places,
  how large, whether it passes, or any limit. Those are fixed above you.
- Keep the standing direction stable. A direction needs many attempts before
  its outcomes say anything; change it on settled evidence, not on yesterday.
- Keep every file you write inside this workspace. Do not read, list, or
  enumerate paths outside it and `../TASK.md`; `bt past` is how you reach
  the history.
- Write plainly. The next attempts read your page cold.
```
