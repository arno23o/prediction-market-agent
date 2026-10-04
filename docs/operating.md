# Operating the betting agent

*The internal README until 2026-10-04, kept as the operator's guide. The public README is at the repository root.*

An autonomous betting agent that improves through accumulated written experience,
with no retraining. Standalone agent "attempts" scan Kalshi for markets resolving
within five days, pre-register an edge claim and falsifiable hypothesis, build whatever
model the idea deserves, and propose bets of one to three contracts that a deterministic
harness validates and places. Every attempt, bet and outcome lands in a permanent
searchable ledger, and one director session a day reads the previous day's attempts, ranks
them, and writes the direction and the examples the next day's attempts are handed.

**The documents.** `docs/README.md` indexes the published documents. Start with
`docs/proposal.md`, the founding brief, and `docs/22-rebuild-spec-sep13.md`, the
specification for version 2.

## Setup

```bash
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env       # fill in Kalshi credentials
.venv/bin/betting-agent init
```

## Operating

```bash
betting-agent status                  # today's digest: slots, spend, positions, HALT state
betting-agent tick                    # the idempotent scheduler pass, every 15 minutes
betting-agent attempt --now           # run one attempt (--cell, --model, --effort override)
betting-agent plan                    # an Eastern day's cells and arms, drawn if not yet
betting-agent director                # the day's director session
betting-agent board-refresh           # pull the board into data/board
betting-agent settle                  # one settlement and reconciliation pass
betting-agent reconcile               # the live-era balance walk against the exchange
betting-agent activity                # record what each attempt did, from its own streams
betting-agent migrate                 # back up, migrate and verify the ledger schema
betting-agent backup                  # back up the ledger and keep the newest 30
betting-agent halt "reason"           # kill switch (resume to clear)
betting-agent deposit --amount 100.00 # owner-run balance adjustment; withdraw mirrors it
bt markets --hours 72                 # the agent's read-only toolkit: markets, series,
                                      # search, new, movers, board, calendar over the
                                      # board snapshot (--live pulls from the exchange)
bt market / book / history            # one market: detail, order book, price history
bt fees / size                        # fee and stake arithmetic, no network
bt ticket validate                    # ticket preflight
bt past recent | families | family | attempt | search | page   # the record (BT_PAST=on)
```

The scheduler is a launchd job running `tick` every 15 minutes:
`launchctl load ~/Library/LaunchAgents/com.betting-agent.tick.plist`
(plist in `ops/`). Attempt slots are set in `config.toml` (`schedule.slots`).

## Development

```bash
.venv/bin/pytest                     # about 1,680 tests; fake-agent E2E, no API cost
RUN_PROD_RO=1 .venv/bin/pytest tests/test_kalshi_prod_ro.py   # live read-only
.venv/bin/ruff check src tests
```

## Layout

```
src/betting_agent/
  ledger/      SQLite+FTS5 ledger and the history tool: the system's memory (spec §6)
  kalshi/      signed REST client + FakeKalshi test double (spec §7)
  harness/     validate, execute, settle, reconcile, safety, cells, director, digest
  sessions.py  headless Claude Code runner (spec §9.4)
  prompts/     the attempt and director prompts (versioned by hash)
  bt.py        read-only toolkit exposed to agent sessions (spec §13)
  cli.py       operator CLI + tick scheduler (spec §15)
data/          gitignored: ledger.db, per-attempt workspaces, logs, backups
```

Safety: real production orders require `stakes.live_trading = true` (human-set only), a
daily stake cap, a per-attempt allowance and a per-market cap (set in `config.toml`; today
$56.00, $11.20 and $4.00), charged to the Eastern day of placement, at most three contracts
per bet, a drawdown floor at half the live genesis balance counting cash and open positions, and a HALT file checked before every order. HALT stops placement and new
sessions only: settlement, reconciliation, the backup and the digest keep running under
it.
