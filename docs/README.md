# Project documents

## A short history of the project

The project started with a founding brief on July 6, 2026, and a paper pilot with simulated bets ran in July. Version 1 traded real money from July 31 to August 30 and ran many experimental groups side by side. One of its designs split some attempts across separate sessions for ideas, ranking and the final ticket. A review in September found that the way attempts learned from earlier attempts was expensive and partly broken, because the history that attempts were meant to read had stopped reaching them in mid August. The project was then rebuilt into version 2, which replaces the old learning machinery with one daily review session, called the director, and assigns each attempt at random to one of four groups that differ in the history they read. Version 2 has traded real money since September 17, 2026.

## Published documents

| Document | What it is |
|---|---|
| [proposal.md](proposal.md) | The founding brief from July 6, 2026, with the research question and the first objections to it. |
| [archive/02-development-plan.md](archive/02-development-plan.md) | The original development plan from July 2026, with the architecture, the early design decisions and the build order. |
| [archive/prompts/two-loop.md](archive/prompts/two-loop.md) | A short note on the version 1 design that split an attempt across separate sessions, and on why it was retired. |
| [charts/chart3_loops.png](charts/chart3_loops.png) | A chart from the September review that compares the cost and results of the two attempt designs in version 1. |
| [22-rebuild-spec-sep13.md](22-rebuild-spec-sep13.md) | The specification for version 2, written on September 13, 2026. |
| [31-attempt-review-sep25.md](31-attempt-review-sep25.md) | A review of the version 2 attempts from September 17 to 23, in which separate reviewers checked each claim. |
| [operating.md](operating.md) | The operator's guide, with the setup steps and the everyday commands. |

## Document numbers cited in the code

Comments in the code cite internal documents by number, e.g., "docs/14" or "spec §4", so that a reader can trace a rule back to the document that set it. Most of the cited documents are internal working notes, and they are not published. The published documents keep their original numbers so that the citations in the code stay stable, and a citation to a number that is missing from the table above points to an internal document.
