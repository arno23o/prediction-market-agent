# Security notes

These notes describe how the system runs, what protects the money, and what to change before you run it yourself.

## How the sessions run

The Claude Code sessions run with permission prompts turned off. They can browse the web, write and run code, and read files, and they run as the same operating system user as the harness. The rule that keeps a session inside its own folder is an instruction in its prompt, not a sandbox. The exchange credentials sit in `.env` and a key file on the same machine, because the read-only market tools need them to read live order books.

## What protects the money

A session has no command that places an order. Every order goes through the validator and the spending limits in the harness, and real orders also need the `live_trading` setting, which only the operator sets. Every 15 minutes the settlement pass reads all orders on the account, and it records any order that the harness did not place, so the nightly balance check counts it.

## Before you run it yourself

1. Keep `live_trading = false` in `config.toml` until you have read `harness/validate.py`, `harness/execute.py`, and `harness/safety.py`.
2. Run the sessions under a separate operating system user, or in a container, that cannot read the exchange key file.
3. Fund the exchange account with no more than you are willing to lose, and set the caps in `config.toml` to match.
4. Claude Code gives each session the email address of the logged-in account. Some public data services ask callers for a contact address in a request header, and a session may use that address. Log in with an account whose address you are willing to share, or add a line to `src/betting_agent/prompts/attempt.md` that gives the sessions a contact address to use.
