# The two-loop attempt, and why it was retired

*2026-09-13. Archive note for `ideation.md`, `critic.md` and `implementation.md`, which
sit beside this file. Nothing here is live. Written when the rebuild (docs/22 section 2.2)
deleted the code that ran it.*

## The setup

An attempt in two-loop mode ran three sessions inside one attempt row, all under status
`running`:

1. **Ideation.** One session read the board and wrote `ticket/candidates.json`, a set of
   `k_candidates` distinct edges with a thesis and sketch bets for each.
2. **Critic.** A second session ranked those candidates into `ticket/ranking.json`. The
   critic model was a single global setting (`attempt.critic_model`, Sonnet) rather than a
   per-slot choice, so that every recipe held the critic fixed and a recipe comparison
   measured the ideation and implementation composition and nothing else. When the critic
   produced nothing usable the harness wrote the fallback ranking, file order, to that same
   file, so the implementation prompt's instruction to read it was never a lie.
3. **Implementation.** A third session took the ranking, deepened one candidate, and wrote
   the ordinary ticket files, declaring in `bets.json` which candidate it had landed on.

The recipe grid named the mixed cell `X2`: Fable ideated, the global Sonnet critic ranked,
Opus implemented. The other two-loop cells ran one model across both ends.

The shadow ledger was the measurement. Every candidate was written to `shadow_candidates`
with the critic's rank and a flag for the one the implementation session chose, and the
sketch bets of the candidates it did not choose went to `shadow_bets` and were scored
hypothetically. The idea was that if the critic added value, the chosen candidates would
beat the unchosen ones over enough attempts.

## The measured result

From docs/20 section d, over the live era:

- Cost per attempt: $21.69 for two loops against $8.30 for one.
- Cost per settled leg: $17.61 against $4.13.
- Calibration was identical between the two shapes.
- The shadow ledger was unable to show that the critic adds value.

Two loops cost about 2.6 times as much per attempt and produced no quality gain that the
data could see. The experiment was closed and the shape deleted.

## What stays

The `shadow_candidates` and `shadow_bets` tables stay in the ledger as read-only history of
the era, along with the attempt rows whose `loop_mode` is `two`. The three prompts stay here
so that what those attempts were asked to do is still readable.
