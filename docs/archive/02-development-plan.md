# Betting Agent — Development Plan

*Status: archived. This is the original plan from July 2026; the current design is described in the README and in `docs/22-rebuild-spec-sep13.md`.*

*Document 2 of 3 — July 6, 2026*
*Reads after `01-outline.md` (what and why). `03-spec.md` is the normative spec; where this document and the spec disagree, the spec wins.*

This document records the technical decisions, the architecture, the build order, and the acceptance criteria for each phase. It is written so that an engineer can understand every choice and its rationale before opening the spec.

## 1. Architecture overview

The system is one Python package, one SQLite file, and a set of agent sessions launched as subprocesses. There is no server, no daemon, and no web UI.

```
                    ┌─────────────────────────────────────────────┐
                    │              HARNESS (deterministic)        │
  cron ──► tick ──► │  scheduler · session runner · ticket        │
                    │  validator · order executor · settler ·     │
                    │  retro/curator triggers · reports · safety  │
                    └────┬───────────────┬───────────────┬────────┘
                         │ launches      │ reads/writes  │ REST
                         ▼               ▼               ▼
                ┌────────────────┐  ┌──────────┐  ┌──────────────┐
                │ ATTEMPT        │  │ LEDGER   │  │ KALSHI       │
                │ (Claude Code   │  │ (SQLite  │  │ (demo/prod,  │
                │  headless, own │  │  + FTS5) │  │  RSA-signed  │
                │  workspace)    │  │          │  │  REST v2)    │
                └───────┬────────┘  └──────────┘  └──────────────┘
                        │ read-only
                        ▼
                ┌────────────────┐   also launched by harness:
                │ TOOLKIT  `bt`  │   GRADER session (retrospectives)
                │ markets, book, │   CURATOR session (playbook)
                │ ledger search  │
                └────────────────┘
```

The division of responsibility is strict and is the load-bearing design idea:

- **The harness never thinks.** It schedules, validates against mechanical rules, executes, records, and enforces caps. Every judgment-free behavior lives here, in ordinary tested Python.
- **Attempts never execute.** An attempt produces a *ticket* — edge claim, hypothesis, and a list of proposed bets with its own probabilities. The harness validates and places. This makes pre-registration structural (no ticket, no bets), keeps the toolkit neutral, and means a misbehaving agent session can cost us its compute budget but not the bankroll.
- **Graders never bet, attempts never grade.** Retrospectives are written by a separate session that sees the record and the outcomes but cannot edit either.

## 2. Decision log

Each decision below is final for v1 unless the spec says otherwise. Alternatives were considered and rejected for stated reasons — revisit only with evidence.

**D1 — Language and tooling: Python ≥3.11, `uv`, minimal dependencies.** The scaffold already exists (hatchling, pytest, ruff, src layout). Dependencies: `httpx` (HTTP), `cryptography` (RSA-PSS request signing), `pydantic` v2 + `pydantic-settings` (config and schemas), `typer` (CLIs). Nothing else at runtime. Rejected: any agent framework or orchestration library — the harness is a few hundred lines of control flow and gains nothing from a framework; the intelligence lives in Claude Code sessions, not in Python.

**D2 — Ledger: a single SQLite file with FTS5.** SQLite is transactional, queryable, zero-ops, trivially backed up, and comfortably sufficient at our scale (two attempts a day for a year is under a thousand rows in the largest table). Full-text search over claims, retrospectives, summaries, and tags comes from FTS5 with triggers — no vector database in v1; if retrieval quality ever demands embeddings, that is an additive change. Money is stored as fixed-point decimal TEXT (Kalshi's API itself moved to fixed-point dollar strings in March 2026); timestamps are UTC ISO-8601 TEXT. Schema migrations use `PRAGMA user_version` with numbered SQL files. Rejected: Postgres (operational weight, no benefit at this scale) and an ORM (the schema is small and DDL-first; raw SQL through a thin DAO is clearer).

**D3 — Attempts are headless Claude Code sessions.** Each attempt runs `claude -p` as a subprocess in a fresh workspace directory with permissions bypassed inside that sandbox, full tool access (shell, file edits, web search and fetch), a task prompt, and a context pack assembled from the ledger. This is the ambition decision: a session that can write and run arbitrary code can build real models — backtests, simulations, ensembles — not just prompt-sized heuristics. The runner command is a config value, which also gives us test injection (a scripted fake agent) and future model/CLI flexibility for free. Model, prompt version, and memory mode are recorded per attempt as first-class experiment variables.

**D4 — First-party Kalshi client, not the SDK.** A thin client over the REST v2 API: markets, orderbooks, order placement, fills, settlements, exchange status, with RSA-PSS signing. It is ~200 lines, we control retry and idempotency behavior precisely (order placement must never blind-retry), and we avoid tracking SDK churn — Kalshi deprecated its old Python SDK and migrated API number formats within the last year. A demo/prod switch exists in config, but the demo environment was skipped by decision (`docs/decisions.md`): development runs against recorded fake responses plus prod read-only access, with real orders gated behind sign-off.

**D5 — Stakes, sizing, and order style are fixed and boring.** Every bet targets one dollar: `contracts = max(1, floor(1.00 / limit_price))`, immediate-or-cancel limit orders only, taker by default, hold to resolution, no exits. Paper bets are filled from a live orderbook snapshot under the same rule real orders face, with fees simulated by Kalshi's published formula, so paper and real results stay comparable. Rejected: resting maker orders (cheaper fees but introduces fill-timing judgment and unfilled ambiguity; revisit after v1) and variable sizing (destroys cross-attempt comparability, invites miscalibrated confidence to compound).

**D6 — Learning machinery: three-layer context pack, separate grader, curated playbook.** A new attempt receives: the playbook (distilled, citation-backed lessons), a pooled scoreboard snapshot, and one-paragraph summaries of recent attempts — plus a search tool over the full archive. Retrospectives are written by a grader session using a fixed verdict taxonomy that separates process from outcome. The playbook is only modified by a periodic curator session, and every entry must cite the attempts that support it. Rejected: letting attempts write directly to shared memory (self-serving drift) and dumping full transcripts into context (token blowup for no retrieval precision).

**D7 — Scheduling is cron calling an idempotent `tick`.** A single entry point runs every 15 minutes: settle anything resolvable, run due retrospectives and curation, and launch an attempt if a slot (10:00 / 16:00 ET) is due and no attempt is running. Lock files prevent overlap; a `HALT` file stops everything. Rejected: a resident daemon (state to babysit, nothing gained at two attempts a day).

**D8 — Safety is caps plus audit, not supervision.** No human approves bets, so the guardrails are mechanical: bets per attempt, real bets per attempt, real dollars per day, real dollars per market, wall-clock and turn limits per session, an append-only audit log of every order and control action, and the kill switch. The real-money subset is chosen deterministically (most liquid first), never by the agent.

**D9 — Reports are generated markdown.** A `report` command renders the scoreboard: profit and loss (paper and real, fee-inclusive), price-bucket calibration, model-vs-market Brier comparison, verdict mix over time, per-edge-type performance, and memory-on/off splits. Rejected: dashboards — a markdown file in the repo is inspectable, diffable, and enough.

**D10 — Two edge classes, one grading machine.** Attempts declare their claim as `probability` (my probability beats the market's price) or `structural` (these prices are jointly inconsistent; this *combination* of positions profits regardless of outcome). Probability claims are validated per bet (minimum edge after fees) and scored by pooled calibration and model-vs-market Brier. Structural claims group their legs, declare an exhaustive scenario table, and are validated by a harness-recomputed worst-case P/L (positive after fees, with balanced contract counts across legs); they are excluded from the Brier pool and scored on realized-versus-declared payoff — and the graded hypothesis is the claimed linkage itself, since subtly mismatched resolution rules are how arbitrage actually fails. Rejected: forcing everything through Brier (it blocks true arbitrage at validation, since arb legs carry no per-leg probability edge, and dilutes the calibration signal) and dropping model probabilities on structural legs (still required as honest estimates).

## 3. Repository structure

```
src/betting_agent/
  cli.py              # `betting-agent` — tick, attempt, settle, retro, curate, report, ledger, halt/resume/status, init
  bt.py               # `bt` — the neutral read-only toolkit exposed to attempt sessions
  config.py           # pydantic-settings; config.toml + env overrides
  ids.py, moneymath.py, timeutil.py
  kalshi/client.py    # signed REST client (demo/prod)
  kalshi/types.py
  ledger/schema/      # 000_init.sql, ...
  ledger/db.py        # connection, migrations, DAO
  ledger/search.py    # FTS queries, summaries, context-pack assembly
  harness/attempt.py  # workspace setup, session runner, ticket intake
  harness/validate.py # ticket validation (pure functions)
  harness/execute.py  # paper fills, real order placement, real-subset selection
  harness/settle.py
  harness/retro.py    # grader session + verdict derivation
  harness/curator.py
  harness/safety.py   # caps, HALT, audit
  harness/report.py
  prompts/            # attempt.md, grader.md, curator.md (versioned, hashed)
tests/                # unit + fake-agent E2E + demo-env integration (marked)
data/                 # gitignored: ledger.db, attempts/, reports/, logs/, backups/
docs/
```

Two console scripts: `betting-agent` (the operator CLI) and `bt` (the attempt-facing toolkit). `bt` opens the ledger read-only and has no order-placement capability at all.

## 4. Build order

Each phase has acceptance criteria; a phase is done when they pass, not before. Phases 1–3 are pure engineering with no LLM in the loop and are well-suited to delegation as self-contained specs; the prompts, validation semantics, and grading taxonomy (phases 4+) are the judgment-heavy core and stay close to home.

**Phase 0 — Access and spike. ✅ Done 2026-07-07 (`docs/decisions.md`).** The demo environment was skipped by decision, so the spike ran read-only against production: signing verified (RSA-PSS over `timestamp_ms + METHOD + path` with the `/trade-api/v2` prefix, query strings excluded), the working host confirmed as `api.elections.kalshi.com` (the spec's original guess doesn't resolve), and balance plus 72-hour market queries returning cleanly on the real account. The order-placement round trip is deferred to go-live, where it becomes the checklist's first manual $1 bet after written sign-off.

**Phase 1 — Ledger and pure math.** Schema DDL, migrations, DAO, FTS triggers, and the pure functions: fee formula (with its round-up-per-order subtlety), contract sizing, P/L, verdict derivation, calibration bucketing. *Accept when: unit tests pass against the spec's worked-example vectors, and FTS search returns tagged fixtures correctly.*

**Phase 2 — Kalshi client and toolkit.** The signed client with retry/idempotency rules, then `bt markets`, `bt book`, `bt fees`, `bt ledger …`, `bt playbook`, `bt ticket validate` on top of it. This phase also pins the real payload field mapping (`close_time` ISO format, ask/category fields) observed only partially in the spike. *Accept when: prod read-only integration tests pass; `bt` outputs match the spec's shapes; mutation of the ledger via `bt` is impossible.*

**Phase 3 — Lifecycle end-to-end with a fake agent.** The full attempt lifecycle driven by a scripted stand-in agent that copies a fixture ticket into place: launch → validate → paper fills and real-subset selection against a recorded-response fake exchange → settle → retro stub → report. This proves every joint in the pipeline before any LLM spends a token or the live API is touched. *Accept when: one command runs the whole loop against the fake transport and the ledger ends in the correct terminal states, twice in a row, including a crash-and-resume in the middle.*

**Phase 4 — Real agents.** The attempt prompt, context-pack assembly, the session runner (headless Claude Code with logging, cost capture, timeout kill), and the grader with the verdict taxonomy. Run supervised all-paper attempts on live prod data (`live_trading` off); read every artifact; iterate on prompts until tickets are consistently well-formed and retrospectives are honest and specific. *Accept when: five consecutive unsupervised paper attempts produce valid tickets, graded retrospectives, and clean ledger entries.*

**Phase 5 — Autonomy and go-live.** Scheduler, caps, HALT, audit, backups, reports. Run a three-day fully autonomous all-paper pilot on prod data (~6 attempts). Then the go-live checklist (spec §18), Arno's written sign-off, the `live_trading` flip, and real one-dollar verification bets. *Accept when: the pilot ran with zero manual intervention and the first real bet is confirmed against the account statement.*

**Phase 6 — The experiment begins.** Run ~20 attempts to seed the ledger; these are the memory-free baseline (the ledger is empty anyway — the flag makes it explicit). Then enable memory, activate the curator, and produce the first baseline-vs-memory report. From here the work is analysis and iteration, not construction.

There are no calendar estimates here deliberately; the phases are small, and the gates are what matter. Nothing blocks Phase 1–3 work on Phase 0 beyond the spike itself.

## 5. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Luck mistaken for skill corrupts the library | Pre-registration by construction; grader separates process from outcome; pooled calibration is the headline metric; flat stakes |
| Kalshi API drift (fixed-point migration, endpoint deprecations) | Thin first-party client; integration tests against demo; endpoint names confirmed at Phase 0, isolated in one module |
| Agent sessions run away (time, tokens, junk bets) | Wall-clock kill, turn caps, bets-per-attempt cap, ticket validation, cost recorded per attempt and alarmed in reports |
| Real-money bugs | Demo-first for every phase; real orders only through one code path with idempotency keys; daily and per-market dollar caps; HALT file; $1 stakes bound worst case to pocket change |
| Credentials readable by attempt sessions on the same machine | Keys live outside workspaces with tight permissions; the daily real-dollar cap bounds abuse; acceptable for v1 and noted in the spec's trust model |
| Fees and spread drown the signal at $1 | Real bets are plumbing verification only; the learning signal is pooled paper-price calibration, fee-modeled |
| Early lessons fossilize in the playbook | Curator-only writes, evidence counts on every entry, mandatory revision when contradicted |
| Some market categories become unavailable to the account | Config-level category exclusion list; categories excludable without code changes |
| Orders the harness did not place contaminate results — or the system trades before authorization | Genesis timestamp + client-order-ID attribution; `live_trading` gate defaults off until written sign-off (spec §10/§14) |

## 6. Verification strategy

Unit tests pin the money math (fee rounding boundaries, sizing, P/L), the validation matrix (every rejection code in the spec), verdict derivation (all cells of the process×outcome grid), and calibration math. The fake-agent E2E is the workhorse: it exercises scheduler, executor, settler, grader plumbing, and reports with zero API-key or LLM cost, and it runs in CI. Prod read-only integration tests (marked, excluded from CI) cover the client; the real order flow is exercised exactly once, at go-live, after sign-off. Prompt changes are verified the only way they can be: run attempts on demo, read the artifacts, and hold them against the acceptance bar of Phase 4. Every prompt file is hashed and the hash recorded per attempt, so behavioral shifts are always attributable.
