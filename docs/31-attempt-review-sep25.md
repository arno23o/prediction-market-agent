# Attempt review, 2026-09-25 (live-v2 era, A-0240 to A-0312)

*The third attempt review, after docs/15 (Aug 15) and docs/18 (Aug 29), both now in docs/archive. It covers every attempt under the rebuilt system of docs/22, from go-live on 2026-09-17 through the last slot that ran on 2026-09-23. Method: one forensic reader per attempt (72 Opus sessions over the ticket, the full transcript, the workspace and the ledger row), then five analysts over all 72 records (numbers, approaches, winners, losers, director and cells), then three independent skeptics per analyst claim (48 claims, 42 survived in corrected form, 6 refuted), then a completeness critic. All headline numbers were recomputed from data/ledger.db by the coordinating session. Read-only throughout. Nothing in data/, src/ or config was touched. Section 0 is the summary, sections 3 to 5 are the answer to "what differentiated winners from losers", section 10 is the pushback, and appendix A lists every attempt.*

---

## 0. The short answer

1. **The money says nothing yet.** Sixty-one legs have settled for a net of +$8.65 on $70.72 staked. If every leg had won with the probability its fill price implied, the expected number of wins was 25.5 with a standard deviation of 2.9, and 29 won. The one-sided chance of a result at least this good under fair prices is about one in ten, and resampling whole attempts puts a fifth of the resamples at or below zero. Sixteen of the 61 legs settle on one OpenRouter week and supply $7.62 of the $8.65. Eighteen legs are still open.

2. **The price paid explains most of the split between winning and losing legs, and it is not skill.** Every leg filled at 0.70 or above won (16 of 16), and every leg filled at 0.15 or below lost (0 of 19). Both results are within what fair prices produce. The profit sits in four legs filled between 0.16 and 0.21 that paid $2.06 to $2.49 each.

3. **The attempts' own probabilities were inflated, and the inflation sits entirely in forecasts of unpublished numbers.** Across the 61 settled legs the attempts claimed 43.5 expected wins, the prices implied 25.5, and 29 happened. Below a claimed 90 percent, the win rate tracked the price paid and not the attempt's number. At a claimed 90 percent or above, 17 of 18 legs won against 13.9 implied, and nearly all of those were legs where most of the settling number was already published.

4. **The one process lead is reading the settling number rather than forecasting it, and it is a lead, not a finding.** Legs from attempts that had read the published part of the number won 21 of 29 against 15.1 implied, for +$16.93. Legs where the attempt had not lost 8 of 32 against 10.4 implied, for -$8.28. Three separate codings of the legs agree on this split. But the coding was done after the outcomes were known, two attempts on one OpenRouter week supply most of the read group's profit, and where several attempts bet the same market the losers had usually read the partial too. What separated them there was the model for the unpublished remainder and the side they took.

5. **The three most expensive avoidable losses were misreads of facts that were available.** A-0253 read a sell-off as anchoring when the low avocado print had been public for days. A-0287 checked for a live Vercel partial with a regex that failed and took the empty result as proof there was none. A-0288 wrote that the market maker "clearly knows the current level" and bet against that level anyway. Together they lost $5.67. The director's Sep 22 page had told both Vercel attempts that the series had "no partial number", which was wrong.

6. **Forty of 73 attempts placed nothing, and the reason was the system, not the agents.** Twenty-four attempts sent whole tickets into Politics and the exchange refused all 72 such orders. Twelve more attempts were refused entirely by the $25 daily cap on Sep 22 and 23. Graded at their limit prices against the settled values, the refused pool as a whole is roughly break-even (+$2 on 108 legs with known outcomes, and the Politics legs alone about -$3), so the block did not cost money on net. It did cost the test of the era's best-shaped work, and because the history showed refusals as "no fill", nine attempts learned to bid through the ask to fix a problem that was really a category refusal.

7. **The director changed what attempts targeted and not how they worked.** Thirteen of 15 directed attempts worked a family the page named, but only three reproduced past settlements and only one measured the unpublished remainder, which were the page's two instructions. Nine of 15 directed attempts proposed a strike the account already held. The direction concentrated crowding onto markets that fill, and repeated strikes spent a quarter to two fifths of each day's cap. At about $4.50 a ticket the cap binds after the fifth or sixth ticket of fifteen whatever the attempts do, so slot order decides which ideas get a money test.

8. **Nothing has run since the evening of Sep 23.** The nightly reconcile has shown the same $0.0103 drift since Sep 21 (the volume-incentive cent, still unrecorded because the credits migration on fix/records-sep21 is not merged), and the third identical night set the halt on Sep 24 at 03:08Z. The earlier gap, from the last slot of Sep 19 to the evening of Sep 21, was the $6 netting false alarm. Two markets that closed a week ago are still unsettled on the exchange (A-0255's four earthquake legs and the two New York low legs), so the ledger is not stuck on them.

---

## 1. The dataset and the timeline

The era runs from A-0240 (a sign-in failure with zero turns) to A-0312. Every attempt ran on claude-opus-5 at high effort with a 90-minute envelope. The director ran six times and produced three valid pages, on Sep 21, 22 and 23, so director and focused slots before A-0281 ran as static.

| Day (ET) | Attempts | Compute | Note |
|---|---|---|---|
| Sep 17 | 10 | $55 | first slot failed at sign-in; every Politics order was refused |
| Sep 18 | 15 | $106 | every Politics order was refused |
| Sep 19 | 14 | $114 | halt set at 03:14Z on Sep 20 for a $5.9968 drift (exchange netting of opposite-side legs) |
| Sep 20 | 0 | | halted |
| Sep 21 | 5 | $46 | halt cleared 21:45Z; Politics excluded from the board; first valid director page |
| Sep 22 | 15 | $124 | daily cap bound at the eighth attempt (10:45 ET) |
| Sep 23 | 14 | $91 | daily cap bound at the sixth attempt (09:03 ET); 23:24 slot skipped by the new halt |
| Sep 24 and 25 | 0 | | halted on the $0.0103 drift, third identical night |

Attempt compute came to $536 and the six director runs to $27, all at list prices as the CLI estimates them under a subscription login. The 40 attempts that placed nothing used $258 of the $536.

| Legs proposed | 197 |
|---|---|
| Filled and settled | 61 legs in 26 attempts, 29 won, 32 lost, +$8.65 net on $70.72 staked after $1.63 of fees |
| Filled and still open | 18 legs in 8 attempts, $29.10 staked |
| Refused by the exchange (HTTP 403) | 72 legs, 70 Politics and 2 Science and Technology |
| Refused by the harness caps | 42 legs, 41 daily cap and 1 per-market cap |
| Market no-fills | 4 legs |
| Effective cells | 50 static, 15 director, 3 focused, 5 baseline |
| Attempts that placed nothing | 40, of which 24 whole-Politics tickets, 12 cap only, 2 mixed, 1 deliberate pass (A-0312), 1 sign-in failure |

Against the live-v1 era for scale: live-v1 ran 192 attempts, of which 102 had a filled leg (53 percent), and settled 374 legs for +$11.75 on $177.92 staked at $2,743 of attempt compute. Live-v2 has run 73 attempts, of which 33 had a filled leg (45 percent), and settled 61 legs for +$8.65 on $70.72 at $536. Compute per dollar staked fell from about $15 to about $8, and realized profit per attempt is $0.06 against $0.12, with 18 legs open. Neither era's profit is distinguishable from zero.

Twenty-eight of the 197 proposed legs and 7 of the 61 settled legs were on bucket ("-B") tickers, against 137 of 312 settled legs in the live-v1 era. Those seven went 4 wins for +$3.03.

---

## 2. The approaches tried and how each did

The approaches analyst assigned every leg to one approach by market family and by the attempt's own method, and the skeptics corrected the counterfactual figures against the exchange's settled values. "Would have" figures grade a leg that never filled at its own limit price with the 0.07 fee, against the settled value. They carry no money, they count a crowded strike once per attempt that proposed it, and they assume the order would have filled at the limit. For 59 of the 73 Politics legs the limit sat through the offer, so the counterfactual at the stored ask is about $6.50 better than at the limit.

### 2.1 Tested with money and paid

| Approach | Attempts | Settled | Net | Comment |
|---|---|---|---|---|
| OpenRouter weekly author share, weekend measured in the settling series | A-0261, A-0268 | 6 won, 2 lost | +$10.01 on $7.71 | One lineage, one week; more than the era's whole net |
| Overnight low already recorded when bet | A-0254, A-0289 | 4 won, 0 lost | +$1.93 on $8.96 | Bought at 0.73 to 0.86; A-0241 made the same bet on New Orleans and lost |
| Overnight lows at 3 AM from station data | A-0285 | 2 won, 1 lost, 1 open | +$3.51 | Miami rested on a recorded 78.1F; Philadelphia assumed a flat trace and lost |
| TSA weekly average with 3 or 4 of 7 days published, far NO strike | A-0244, A-0263 | 2 won, 1 lost | +$3.93 | A-0275 read the same days, forecast the weekend from two years, bought YES on the same strike and lost $2.23 |
| USDA chicken-wing monthly mean, 3 of 4 prints read | A-0281, A-0284, A-0299 | 5 won, 0 lost | +$1.07 on $13.86 | Five entries on one T115 strike at 0.82 to 0.98; A-0282 and A-0293 were capped on the same strike |
| Vercel open-weights share, far strikes 2 to 3 days out | A-0286, A-0287, A-0288 | 3 won, 0 lost | +$2.64 | The value landed at 73.1 against A-0286's 74 to 82 band, so its own criteria call the T72 win a warning |
| Ornn GPU index read from its own API | A-0282 | 1 won | +$0.38 | Four-day forecast at about market odds; A-0297's H100 leg was capped and would have won |
| Rain contracts priced inconsistently with each other | A-0271 | 2 won | +$0.34 on $5.64 | Near-certain legs at 0.92 and 0.96; its main Truth Social leg was refused |

### 2.2 Tested with money and lost

| Approach | Attempts | Settled | Net | Comment |
|---|---|---|---|---|
| Vercel Moonshot daily spend, sold above the record from history on a day in progress | A-0287, A-0288 | 1 won, 3 lost | -$3.56 | The maker was quoting the live export; the day closed at 20.74 |
| AAA national gas drift | A-0250 | 0 won, 3 lost | -$3.08 | Its own momentum and seasonal studies sided with the market |
| OpenRouter share, weekend inferred from one weekend in a token proxy | A-0274 | 0 won, 6 lost | -$1.15 | Seven cheap tails, one against the account's own position |
| OpenRouter weekly tokens, strike inside its own 80 percent band | A-0273 | 1 won, 1 lost | -$1.24 | The prior week's exact settled value sat one field away in a response it had pulled |
| USDA avocado price rebound | A-0253 | 0 won, 2 lost | -$1.14 | The low print had been public for days before the sell-off it read as anchoring |
| Vercel open-weights, strike nearest the estimate | A-0304 | 0 won, 1 lost | -$1.16 | The page had called that edition a forecast |
| Temperature forecasts against the ladder (ensemble wings, afternoon highs, tails) | A-0283, A-0298, A-0307 | 0 won, 5 lost | -$0.91 | A-0307's two highs were capped and would have lost |
| Economics prints with nothing published (SOFR, CFNAI tails, ERCOT lottery) | A-0298, A-0260, A-0309 | 2 won, 4 lost | -$0.54 | A-0260's direction was right and its best leg did not fill |

### 2.3 Never tested with money, graded against the settled values

| Approach | Attempts | Blocked by | Would have | Comment |
|---|---|---|---|---|
| Suez weekly transits, sell both tails of an over-wide ladder | 11 attempts, A-0242 to A-0277 | 403 | 19 of 19 tail legs won (+$10.2), 5 of 6 centre legs lost (-$5.2) | One week (settled 293); A-0270 found that Kalshi settles on PortWatch's first print |
| Truth Social weekly post count from Roll Call's own feed | A-0258, A-0265, A-0266, A-0271, A-0272, A-0278 | 403 | 9 of 10 won (+$8.4) | A-0258 bet earliest with 34 hours left and lost; every later count won |
| Hormuz peak-day and weekly floors | A-0257, A-0259, A-0264, A-0267, A-0269, A-0279 | 403, 1 cap | Floors mostly won (Hormuz peak above 5 went 5 of 6, +$5.4) | Favourites bought repeatedly; outcomes turned on two or three ships |
| Hormuz, Bab el-Mandeb and Panama level fades against a news-driven repricing | same group plus A-0243, A-0251, A-0276, A-0277, A-0280 | 403 | 21 of 21 lost (about -$17) | The market had live AIS data the attempts did not |
| White House presidential-actions count, fade Friday | A-0243, A-0247, A-0249, A-0256 | 403 | 0 of 7 (-$8.3) | All four counted 8 correctly; a 4:30 PM Friday signing was on the public schedule |
| Vercel daily lab share read from the live export | A-0292, A-0294 | cap | 9 of 9 won (+$6.2) | One day; most legs priced 0.89 to 0.95 |
| Late-UTC-day biggest earthquake, history split at the same hour | A-0293, A-0308, A-0309, A-0310 | cap | 4 of 4 won (+$2.6) | Three market days in all, counting A-0255's open legs |
| Overnight lows, capped | A-0296, A-0305 | cap | Miami and Atlanta won (+$1.8); Newark and Trenton lost to 11:59 PM undercuts (-$4.6) | The same failure mode as A-0241 |
| Artificial Analysis leaderboard holds, mortgage rate | A-0284, A-0290, A-0295 | 403, cap | mostly unknown; mortgage would have lost | |

Across all 108 unplaced legs with a known outcome, the counterfactual is 66 wins and 42 losses for about +$2.00 at the limits. The 73 Politics legs come to 39 wins and 34 losses, which is 38.9 wins implied by their prices, so they show no edge in either direction, and their P/L runs from -$3.49 at the limits to +$3.46 at the stored asks. If the daily cap had applied to them, only about 35 would have filled, and they would have displaced 18 to 20 non-Politics legs that did trade, including all three of A-0261's.

### 2.4 Pending

Twelve legs from A-0300, A-0301, A-0302, A-0303 and A-0306 close on Sep 27 and 28. A-0300 found that the monthly OpenRouter token total is a 30-day trailing window with 25 of 30 days published. A-0303 re-bought A-0300's T500 and T525 strikes without checking holdings, and it and A-0306 also sold Tencent share on a model shut-off thesis. A-0302 bet the weekly total with two of seven days in hand and put maximum size on the strike nearest its estimate. A-0301 bet two temperature streak markets. A-0255's four earthquake legs from Sep 18 are still unsettled on the exchange, and USGS lists the day's maximum at M5.5, which would make all four winners for about +$1.17.

### 2.5 Notes on the approaches that carried the money

A-0261 found OpenRouter's undocumented chart endpoint by grepping 61 JavaScript chunks, and it showed that the share displayed to every trader covered Monday to Friday only, while the settling week has two weekend days. It sold Google at 18.8, 18.6 and 18.3 and won the first two. A-0268 found A-0261 through `bt past search`, took its endpoint, and added two things of its own. It isolated last weekend in the settling metric by differencing the trailing seven-day window against the week-to-date bucket, and it found that a junk query parameter busts the hourly cache, which gave it a live read of Saturday. It won four of five legs for +$6.70 on $5.13. The one leg each lost was the cheap strike at or beyond its own centre. Only A-0268 measured the weekend mainly in the settling series, and A-0261 leaned partly on a token proxy, so the readers coded both remainders as inferred. Half of the pair's money came from two 16-cent longshots, and $2.14 of A-0268's profit came from a Tencent momentum leg rather than from the weekend measurement.

A-0274 worked the same markets the same day, made no `bt past` calls, and inferred the weekend from a single differenced weekend measured in tokens while the market settles on requests. It missed a DeepSeek launch that changed the mix, bought seven cheap tails, one of them on the opposite side of a market A-0268 held, and lost all six that filled.

The daily-low family is the most repeated profitable shape. A-0254 turned a pre-era Philadelphia win into a method by checking that the six-hourly minimum group in the METAR remarks catches the true low between hourly readings, and it bought the San Antonio bucket after sunrise. A-0289 required each leg to survive four settlement-day conventions and skipped the Northeast cities because models showed cooling through midnight. The failure mode is a late evening undercutting a morning low, which happened once with money (A-0241 at New Orleans, where 81F fell to 78F at 23:53) and twice on capped legs (A-0305's Newark and Trenton at 11:59 PM). A-0291 treated a two-group Central Park reading of 55F as the settlement, the final climate report printed 59, and the market is still unsettled. A-0307 did the strongest source work in the family, reverse-engineering the settlement convention against 1,911 past settlements (the 24-hour remark group matched 248 of 250), and the cap refused its legs.

On the TSA week, A-0244 read Monday to Wednesday from tsa.gov, saw one buyer lift A2.40 on a thin book, and bought NO at 0.18. A-0263 reproduced 10 of 10 past settlements by paging Kalshi's public API and bought the same NO at 0.43. A-0275 read four days, more than A-0244, forecast the weekend from a two-year post-Labor-Day analogy with the calendar confound in plain view, and bought YES on A2.40 and A2.45. The week settled between 2.35M and 2.40M. The far strike on the published side won three times, the near strike lost twice, and the forecast YES lost.

The chicken-wing ladder shows the account paying compute repeatedly for one near-certain strike. A-0281 spent 17 minutes reaching the USDA file index through a proxy, read three of the four weekly prints, and won both legs. Four more attempts sent the same T115 NO, two filled and two were capped, and the five settled legs made $1.07 on $13.86.

On the Vercel markets the same day produced both the cleanest read and the largest loss. A-0292 found that Vercel's documented export carries the in-progress UTC day while the chart page shows only completed days, proved the two agree over 25,302 values, and found that rotating the `from` parameter bypasses the cache. A-0294 built on it three hours later. Both were capped, and all nine legs would have won. Earlier that morning A-0287 and A-0288 had sold Moonshot spend above its 356-day record from history alone, while the maker quoted the live partial above 20. A-0286 had pulled the export three hours before them and declined to bet against Moonshot.

---

## 3. What separated winners from losers

### 3.1 The price paid, which is not evidence of skill

| Fill price | Legs | Won | Net | Chance under fair prices |
|---|---|---|---|---|
| under 0.10 | 13 | 0 | -$1.88 | 52 percent that none wins |
| 0.10 to 0.30 | 16 | 4 | +$4.54 | about 2.6 wins expected |
| 0.30 to 0.70 | 16 | 9 | +$0.62 | |
| 0.70 to 0.90 | 8 | 8 | +$4.19 | 14 percent that all win |
| 0.90 and above | 8 | 8 | +$1.18 | 64 percent that all win |

Winning attempts' legs averaged a fill of 0.57 and losing attempts' legs 0.27. Nine of the 14 net-positive attempts bought mostly favourites and none of the 12 net-negative ones did. Seven legs bought between 0.37 and 0.64 at two or three contracts make up 61 percent of the $18.17 lost, and they come from A-0250, A-0275, A-0287, A-0288, A-0273 and A-0304. All 19 legs at 0.15 or below lost, for only $3.81 in total. Side, contract count and category show no difference beyond what price explains, and Economics is the only category with a net loss (9 of 21 for -$1.98).

### 3.2 The attempts' own probabilities

| Attempt's claimed chance | Legs | Mean claimed | Mean price paid | Actual win rate |
|---|---|---|---|---|
| under 0.40 | 10 | 0.29 | 0.10 | 0.10 |
| 0.40 to 0.70 | 10 | 0.52 | 0.15 | 0.10 |
| 0.70 to 0.90 | 23 | 0.79 | 0.40 | 0.43 |
| 0.90 and above | 18 | 0.96 | 0.77 | 0.94 |

On the 43 legs claimed below 0.90, the attempts expected 26.2 wins, the prices implied 11.6, and 12 won. If the attempts' numbers had been right, a result that bad would come up about once in a million tries. Legs where the attempt claimed more than 35 points of edge over the price were expected by the attempt to win 14.5 times and won 5. The whole gap sits in forecasts. On legs where the attempt had read the published part of the number it expected 22.9 wins and got 21, and on legs where it had not it expected 20.5 and got 8. The record's own guidance, that the price is probably well calibrated and our probabilities have not been, held again.

### 3.3 Reading the number against forecasting it

The winners analyst coded each settled leg for whether the attempt had pulled the already-published part of the settling number from the source, and the losers analyst coded the same legs independently. The two codings agree on 54 of 61 legs and put the same 25 legs in "pure forecast". The quant analyst used the readers' approach labels instead. All three give the same split.

| Coding | Read or retrieval legs | Not read or forecast legs |
|---|---|---|
| Winners analyst | 29 legs, 21 won against 15.1 implied, +$16.93 | 32 legs, 8 won against 10.4 implied, -$8.28 |
| Losers analyst | 26 legs, 17 won, +$12.95 | |
| Readers' labels | 34 legs, 20 won, +$12.40 | 24 legs, 7 won, -$4.23 |

At the attempt level, 11 of 14 winners had read the partial on at least half their legs against 3 of 12 losers. Ten of 15 retrieval attempts made money against 3 of 10 forecast attempts, a split that chance produces about 7 times in 100. Among attempts that bought mostly cheap legs, where price cannot explain the result, reading still separated them, 5 of 7 against 0 of 8.

The skeptics kept the split and cut it down. Two attempts on one OpenRouter week supply $10.01 of the read group's $17.15, and removing the top three retrieval attempts leaves +$0.12 on 24 legs, while removing the two worst forecast attempts turns that group positive. Under the readers' separate flag for "reached the exact settlement source" there is no gap at all, because 57 of 72 attempts reached it, losers included. The read flag was coded with the outcomes visible, and it was the best of about fourteen splits the analysts tried. Inside the weather family the split is entirely price, since the "recorded" legs were favourites and the "forecast" legs were 3 to 20 cent wings.

Where several attempts bet the same market the picture is sharper and less flattering to the label. On the TSA week all three attempts read the published days and the loser had read more of them than one winner. On the OpenRouter week A-0274 read the same source as A-0268 for the wrong window. On the Truth Social week every attempt counted from a feed, and the one that would have lost simply bet 34 hours before the close. What differed in every pair was the model for the unpublished remainder, how much of the outcome was still unknown at the time of the bet, and the side taken.

### 3.4 Strikes near the estimate and cheap tails

A strike near the attempt's own estimate appears on about 25 of the 32 losing legs and on 4 to 8 of the 29 winning legs, depending on who codes it. In five tickets the near strike lost while a farther strike on the same ladder won (A-0244 A2.35 and A2.40, A-0261 18.3, A-0268 Tencent 6.4, A-0273 T128 and T130, A-0288 T20 and T22). But a price cut at 0.30 reproduces the same split, the near legs won 4 times against 5.5 implied, and in a nested basket of strikes that all point the same way a split outcome can only mean the inner strike lost. Near strikes also won for A-0263, A-0268's DeepSeek 25.3, A-0254 and A-0281, and far strikes lost for A-0287 when the estimate itself was wrong. The skeptics' reading is that when an attempt disagreed with a liquid favourite the true value landed between the two and closer to the market, which is the same overconfidence as in 3.2. The one strike result that survives on its own is narrow. Near or beyond strikes on pure forecasts went 0 for 20 against 3.2 implied, a result with about a 2 percent chance under fair prices, across about ten independent events.

### 3.5 Misreads

Three attempts lost $5.67 by not checking one specific fact that was available. A-0253's price history showed the first avocado trade at 0.39 five days after the low print, so the sell-off it called anchoring was informed selling. A-0287 ran one regex for a live partial on the Vercel chart page, the regex failed, and it concluded that "Vercel publishes complete UTC days only". A-0288 never looked for the export at all and shaded its own 0.275 to 0.10 on a conditional sample of nine. A standing check, "is the settling day already in progress, and where is its live partial", would have caught two of the three.

### 3.6 What did not separate them

Effort did not. Winners were slightly ahead on every measure and none of the gaps is near what chance rules out.

| Median | Winners (14) | Losers (12) |
|---|---|---|
| Code runs | 39 | 34 |
| Wall minutes | 24.8 | 20.5 |
| Session cost | $9.39 | $7.54 |
| Kill criteria written | 6 | 6 |
| `bt past` calls | 0 | 0 |
| Ticket length | 14,000 characters | 14,500 characters |

Reproducing past settlements did not. Legs from attempts that reproduced went 6 of 20 for -$1.02, about what their prices implied, and the flag is coded inconsistently across records. Reaching the exact settlement source, citing a predecessor and using `bt past` all look favourable only because of A-0268, and without it each turns flat or negative. Sizing did not, since 49 of 61 settled legs were at the three-contract maximum in winners and losers alike, and in the seven attempts that mixed sizes the three-contract legs lost $3.11 while the two-contract legs made $0.26. Cell did not, on the eleven director-cell legs that have settled. One measure differs at a conventional level, which is the point in the session at which the ticket was first written (winners at 0.88 of the session, losers at 0.71), but it was one of fourteen tests and mixes realized and counterfactual outcomes, so it is suggestive only.

---

## 4. The thread through the winners

The winners read most of the settling number from the exact series the rule settles on, and then bet the strike that stayed clear of what was still unknown. A-0261 and A-0268 read three quarters of the OpenRouter week. A-0254 and A-0289 bought a minimum already on the tape. A-0244 and A-0263 read the published TSA days and sold the far strike. A-0281 read three of four USDA prints. A-0286 and A-0288 won on far strikes with nothing published, and A-0282's index forecast landed at about market odds. Their probabilities were honest. On read legs the attempts expected 22.9 wins and got 21.

Measured by dollars, four attempts whose mechanism was a read of the published number (A-0268, A-0261, A-0244 and A-0285's Miami leg) made $15.79 of the $22.65 earned by the fourteen positive attempts. The readers mark ten attempts as having won for their stated reason and one (A-0286) as lucky. Among the ten, A-0263, A-0282 and A-0271 are forecasts that happened to land or near-certain crumbs, so their wins do not test the mechanism they stated. A-0288's T22 leg won by 1.3 points after its own record-bound mechanism had failed on T20.

The lineage point holds as far as it goes. A-0268 improved on A-0261, A-0265 improved on A-0258, and A-0294 improved on A-0292, in each case by taking a validated source from a predecessor through `bt past` and extending the measurement. The attempts without that view either converged blindly (the eleven Suez tickets) or took the opposite side of the account's own position (A-0274 against A-0268, A-0275 against A-0244 and A-0263).

Timing is my own observation and was not put to the skeptics. Settled legs placed more than 36 hours before close went 20 of 29 for +$13.96, and legs placed inside 36 hours went 9 of 32 for -$5.31. The reads happened on weekly numbers with days left, and the forecasts on daily numbers with hours left, so this is the same split seen from the clock.

---

## 5. The thread through the losers

The losers forecast the part of the number that decided the bet, against a market that had better information or a better estimate, and put the strike at or past their own centre. Often they explained away evidence that pointed toward the market. A-0250's own momentum and seasonal studies sided with the market's slower gas drift and it bet the faster one. A-0298 printed a 21 percent persistence rate on its own subsample, wrote "critical caveat", and priced a fourth identical SOFR print at about 50 percent. A-0274 called its weekend assumption "load-bearing" and admitted it rested on one observation. A-0304 pre-registered the exact failure it then walked into, on a strike nearest its estimate, after the page had called the edition a forecast.

Eleven of the twelve losing attempts had written the losing scenario down in advance as a main risk or a kill criterion. The exception is A-0260, whose direction held while its sizing was wrong. Writing down what would refute a thesis was not the gap. Measuring it before betting was. The losers did not differ from the winners on effort, on source reached, on history use or on kill criteria, and several of them (A-0273, A-0275, A-0274) had read the published partial. The difference was what they did with the unpublished part.

How the losing dollars split between wrong theses, misreads and named risks depends on who codes them. The losers analyst put 21 legs and $9.79 on wrong theses, 5 legs and $5.67 on misreads and 6 legs and $2.71 on named risks beside winning main legs. The readers' own verdicts put 45 percent on wrong theses and 36 percent on named risks. What does not depend on the coder is that seven mid-priced legs at maximum size, where the attempt disagreed with a liquid favourite, are 61 percent of the losses.

The refused Politics work splits the same way when graded against the settled values. The Truth Social counts read from Roll Call late in the week would have won 9 of 10. The White House count, measured correctly at 8 and then faded on weekday base rates while a Friday signing was on the public schedule, would have lost 7 of 7. The Hormuz and Bab el-Mandeb level fades against a book repricing on live AIS data would have lost 21 of 21, and A-0280, which harvested candles for 132 strikes and showed the Hormuz book's Monday-night median was accurate to about one ship, was right to decline. The Politics pool as a whole is break-even against its own prices, so the same lesson applies there as to the placed legs. Where the attempts read, they were right, and where they forecast against an informed book, the book was right.

---

## 6. The director and the cells

The pages steered target choice. Thirteen of 15 directed attempts worked a family their page named (chicken wings, Vercel, the OpenRouter token and share ladders, the late-day earthquake), while 7 of 14 static attempts on the same days drifted to daily temperature ladders. The pages did not transmit the method. Their first instruction, "reproduce past settled weeks first", was followed by 3 of 15, and their second, "measure the remainder rather than assume it", by one (A-0310). Some directed attempts adopted the vocabulary instead. A-0288, A-0304 and A-0287 describe forecasts as "a number I can already read most of". The best instances of the shape the page described came from a focused attempt (A-0292) and a static one (A-0294) on the live Vercel export, at a time when the Sep 22 page said Vercel had "no partial number".

The director's retrospective process rankings tracked outcomes reasonably (Spearman 0.55 and 0.66 against realized net for the Sep 17 and Sep 19 cohorts, 0.78 and 0.71 with counterfactuals). Its one prospective ranking, for Sep 22, was weaker (0.34). It put A-0285 last, and A-0285 had the best realized result of the cohort at +$3.51, on a Miami leg that rested on a minimum already recorded, which the paragraph said had not been. It put A-0291 second on a Central Park reading of 55 that the final climate report contradicts. Its claim that only the partly-published shape had paid held in direction for the Sep 17 to 19 attempts (+$10.32 against -$4.28) and narrowed after Sep 21 (+$2.08 against +$0.52). The shape also held four of the era's clearest losers.

Two statements in the pages were wrong and became direction. The Sep 22 page's "no partial number" for Vercel plausibly discouraged A-0288's search, and A-0287's failed regex confirmed it. The Sep 23 page's rule that settlement follows the six-hourly synoptic groups, "at Central Park they differed by four degrees", comes from A-0291's reading, was never checked against a past settlement, and is contradicted by A-0307's finding that the 24-hour remark group matched 248 of 250 settlements. Several of the page's attempt summaries are also weaker than presented. A-0263's winning strike sat about 0.4 of its own error from its estimate, which is the near strike, and A-0273's centre missed the settlement by 1.7 trillion tokens, so the level was low rather than right.

The direction concentrated crowding onto markets that fill. Before the pages, crowding was worse but invisible, because Suez T300 went out from eight attempts and Suez T260 from seven, all into a category that refused every order. After the pages, nine of 15 directed attempts proposed a strike the account already held, against 2 of 14 static. Repeated strikes spent about $9.30 of Sep 22's cap (38 percent) and $6.30 of Sep 23's (25 percent). The cap itself is structural. At about $4.50 a ticket, fifteen slots ask for three times the cap, and it bound at the eighth attempt on Sep 22 and the sixth on Sep 23, so every later slot lost legs. On Sep 22 the refused tickets were worth about +$6 counterfactually, mostly the two live-Vercel tickets, and the legs that filled first were the crowded and forecast-based Moonshot legs that lost. On Sep 23 the refused tickets were worth about -$6.5, so the cap protected money. Which ideas get a money test is decided by slot order and has nothing to do with their quality. No attempt could see committed stake, and A-0310, which inferred it correctly, still submitted a leg that could not fit.

The baseline cell ran four times. A-0264, A-0269 and A-0309 landed on the same targets as history-fed attempts on the same day (Hormuz peak, the Suez crowd, the earthquake ladder), which says the board chooses the target and the history does not. A-0298 made the short-horizon print forecast the record had already lost on twice and overrode its own subsample to do it, which is the one place no history plausibly hurt. The static set was frozen on Aug 30 records until Sep 21 because the set waited on settlement. No attempt copied an example leg. Seven tickets copied the form, reusing the kill-criteria wording "no-fill legs are graded by their counterfactual outcome". The set mattered by omission, since it carried no Politics warning while nine of the first fifteen static attempts sent whole Politics tickets.

`bt past` was called by 15 of the 40 attempts before Sep 21 and 14 of 32 after, and the median for winners and losers alike is zero calls. It helped when an attempt took a validated source from a predecessor and extended it (A-0268, A-0262, A-0265, A-0294, A-0299, A-0302). It misled nine attempts (A-0252, A-0256, A-0257, A-0266, A-0272, A-0290, A-0291, A-0295, A-0297) into reading 403 refusals as price problems and bidding 1 to 8 cents through the offer. That lesson cost almost nothing in money, because 56 of 79 fills came in below the limit for about $5.14 of price improvement in total, and the harm was the misdiagnosis rather than overpaying.

---

## 7. The plumbing that shaped the record

The exchange refused 72 orders between Sep 17 16:33Z and Sep 22 15:13Z: 70 in Politics, a category that is not available to this account, and two on Artificial Analysis markets listed under Science and Technology. The harness kept sending Politics orders for four days after the first refusal, the ledger and `bt past` rendered every refusal as "no fill", and the board kept listing Politics until Sep 21. The fix on fix/records-sep21 (refusals rendered with the exchange's sentence) is still unmerged.

The harness caps refused 42 legs, 35 of them on Sep 22 and 23 across 16 attempts. Their known counterfactuals are 25 wins and 8 losses for about +$2.60, and they include every leg in the era whose remainder the readers marked as measured (A-0292, A-0294, A-0310). No attempt with a measured remainder ever traded.

Fifty-six percent of legs (110 of 197) went to a ticker that another attempt also proposed, across 32 tickers. The account ended up on both sides of two filled tickers, where the exchange nets the position and both fees are paid (KXOPENSHARE-26SEP21-18, where A-0268's NO won and A-0274's YES lost, and KXTSAW-26SEP20-A2.40, where A-0244 and A-0263's NO won and A-0275's YES lost).

Two markets that closed a week ago are still unsettled on the exchange, not in the ledger. The four A-0255 earthquake legs ($8.76 staked) closed Sep 18 and the two New York low legs (A-0285 B59.5 YES and A-0291 B57.5 NO) closed Sep 23, and all show a closed status with an empty book. The settle command has nothing to book until Kalshi settles them.

Compute came to $536 for attempts and $27 for the director, which is $8.79 per settled leg and $7.58 per dollar staked ($5.37 counting open legs). The realized profit is 1.6 percent of compute. No attempt's profit covered its own session, and the best, A-0268, made $6.70 for $14.24.

---

## 8. Claims that were proposed and refuted

The skeptics killed six of the 48 claims, and each refutation is worth knowing.

- **"Temperature legs on a recorded value all won and forecast legs nearly all lost."** The split is price. The recorded legs were favourites at 0.73 to 0.86 and the forecast legs were 3 to 20 cent wings that won about as often as their prices said. A-0241 made the same bet as A-0254 and lost, and the sorting was done by outcome.
- **"A far strike helped only when the estimate rested on read data."** Reading helped at every strike distance (near strikes on read legs went 7 of 15 against 4.4 implied). Distance mattered more on forecasts, not less, and "far" in the coding meant the attempt gave itself 85 percent, not that the strike sat far from its estimate.
- **"Holding the event fixed, readers won and forecasters lost on five events."** On the two events with money on both sides (TSA and OpenRouter) the losers had also read the partial, one of them more of it than a winner. Only Moonshot fits, and its winning side never traded.
- **"The unplaced legs repeat the pattern."** Nearly every unplaced "read" leg was itself a partial plus a forecast remainder (Truth Social with Saturday left, Vercel with a tenth of the day left, earthquakes with four hours left). The raw gap mostly reflects price and clustering, since the read group averaged a limit of 0.74 and the forecast group 0.41.
- **"Most losing dollars came from wrong theses and named-risk losses were small."** The share is 45 to 54 percent depending on the coder, A-0260 belongs in neither bucket, and losing dollars track price paid more than failure type.
- **"The Politics tickets would have lost money overall."** They came out even against their own prices (39 wins against 38.9 implied). The sign depends on whether legs are graded at the limit or at the stored ask.

---

## 9. Open legs and what would change the picture

Eighteen legs with $29.10 at stake are open. Twelve close on Sep 27 and 28 on the OpenRouter monthly and weekly token ladders, the Tencent share ladder and two temperature streak markets. The OpenRouter Sep 28 ladders are the second settled week of the weekend factor that carried the era, and if the factor flips sign the composition read in A-0261 and A-0268 was noise. The monthly T525 legs turn on whether the settling window is 30 days or 28. The New York settlement decides whether A-0285's Miami-and-Minneapolis result stays at +$3.51 or rises, and whether A-0291's hedge pays. The earthquake legs are very likely winners and would add about $1.17.

Three things would move the conclusions. A settled week where read legs lose against their prices would remove the one process lead. A second and third Vercel day where the live export is read and bet would tell whether the Sep 22 result generalises. And a run of days where the cap does not bind by noon would show whether the afternoon slots produce anything different from the morning ones, which the current record cannot show because the afternoons were refused.

---

## 10. Pushback

The strongest case against this review is that everything in sections 3 to 5 is a description of one week of favourites winning. Sixteen favourites won, nineteen cheap tails lost, and both are within what fair prices produce. The read-versus-forecast split was coded after the outcomes were known, it was the best of about fourteen splits tried, its profit sits in two attempts on one OpenRouter weekend, and under a neighbouring flag (reached the source) there is no gap at all. On the only two events with real money on both sides, the losers had read the partial too. A skeptic can reasonably say the lead is "attempts that bought favourites late in weekly windows did fine, and attempts that bought longshots on daily numbers did not", which is close to a description of price.

The second objection is that the counterfactual record is doing too much work. The refused legs never faced fills, the Politics legs sat through the offer 59 times out of 73, and the strongest untested families (Truth Social, Suez tails, the Vercel export) rest on one settled number each. Graded against their own prices the Politics pool shows no edge either way, and the whole unplaced pool is +$2 on 108 legs. "Half the good ideas were never tested" is an overstatement, and "the block probably saved money once the cap is applied" is the more defensible reading.

The third objection is to the loss diagnosis. Whether a loss was a wrong thesis, a misread or a named risk is a judgment, the coders disagreed on four attempts, and the one description that does not depend on a coder is that seven mid-priced legs at maximum size are 61 percent of the losses. That points at sizing and price rather than at reasoning.

The fourth objection is that the director analysis rests on 13 settled directed attempts and 11 settled legs, most of the directed work is still open or was capped, and the two losses that make the director cell negative were attempts that did the opposite of their page. A fair reading is that the director's effect on outcomes is unmeasured.

What survives all four is narrow. The attempts' own probabilities were inflated on every leg below a claimed 90 percent and honest above it, and the honest legs were the ones where the number was mostly on the tape. Whether that becomes a repeatable edge depends on the weeks that have not settled.

---

## 11. What this review answers on the check-in list

Entry 9 (the placement funnel, due Sep 23): 33 of 73 attempts placed a leg (45 percent), against the Sep 12 baseline of 49.5 percent, with 24 refused entirely by the exchange, 12 refused entirely by the cap, 2 mixed, 1 pass and 1 sign-in failure. Entry 10 (bucket contracts): 28 of 197 proposed legs and 7 of 61 settled legs were on bucket tickers, against 137 of 312 in the live-v1 era. Entry 11 (component effects): history use, predecessor citation and breadth did not separate winners from losers, and the prospective ranking correlated 0.34 with the counterfactual record. Entry 13 (the probability field): section 3.2 is the calibration measurement the field was meant to provide, taken from the tickets' prose instead. The list itself is unchanged, per your instruction.

---

## Appendix A. Every attempt

Cost is the session's estimated compute. "Remainder" is the readers' label for how the attempt handled the unpublished part of the settling number. The verdict is the reader's.

| Attempt | Day (ET) | Cell | Cost | Family | Approach | Remainder | Legs and result | Verdict |
|---|---|---|---|---|---|---|---|---|
| A-0240 | 2026-09-17 | baseline | 0.0000 |  | failed at sign-in (0 turns) |  | no legs | failed |
| A-0241 | 2026-09-17 | static | 11.3442 | KXLOWTNOLA, KXLOWTSFO (worked all 23 KXLOWT Sep-17 daily-low ladders) | Morning low already banked; bet the thin top bin that the evening won't undercut it | forecast | 0W/1L -0.16; 1 no fill | lost_wrong_thesis |
| A-0242 | 2026-09-17 | static | 5.9883 | KXSUEZWEEKLY (also worked and passed KXHORMUZWEEKLY, KXBABELMANDEBWEEKLY, KXPANAMAWEEKLY) | Rebuilt PortWatch weekly sums; sold both Suez ladder wings on a too-wide implied width | forecast | 3 refused 403 | refused_or_no_fill |
| A-0243 | 2026-09-17 | static | 5.1252 | KXHORMUZWEEKLY (primary, 3 legs), KXTRUMPACT (secondary, 1 leg); also worked KXSUEZWEEKLY/KXPANAMAWEEKLY/KXBABELMANDEBWEEKLY as field-identification checks | Rebuild weekly Hormuz transit count from IMF PortWatch; bet 8-week base rate vs mid-week crash | forecast | 4 refused 403 | refused_or_no_fill |
| A-0244 | 2026-09-17 | static | 3.7997 | KXTSAW (bet); KXSUEZWEEKLY, KXBABELMANDEBWEEKLY, KXHORMUZWEEKLY, KXPANAMAWEEKLY (worked and rejected) | Partial-week TSA average: 3 of 7 days published, forecast remainder, fade one-buyer spike | forecast | 1W/1L +2.27 | mixed |
| A-0245 | 2026-09-17 | static | 5.5640 | KXSUEZWEEKLY (also worked KXHORMUZWEEKLY, KXHORMUZPEAK, KXBABELMANDEBWEEKLY, KXPANAMAWEEKLY) | Sell both tails of a thin Suez weekly transit ladder priced too wide versus PortWatch history | forecast | 3 refused 403 | refused_or_no_fill |
| A-0246 | 2026-09-17 | static | 5.3483 | KXSUEZWEEKLY (also worked and rejected KXHORMUZWEEKLY, KXBABELMANDEBWEEKLY; glanced at KXPANAMAWEEKLY) | Rebuild IMF PortWatch weekly transit sums; sell the overpriced upper-tail Suez rung | forecast | 1 refused 403 | refused_or_no_fill |
| A-0247 | 2026-09-17 | static | 3.3958 | KXTRUMPACT (worked briefly first: KXBTCD/KXBTC cross-series arb scan, clean) | Count-ladder base rate: whitehouse.gov weekday publication rates vs a re-marked ladder | inferred | 2 refused 403 | refused_or_no_fill |
| A-0248 | 2026-09-17 | static | 4.4062 | KXSUEZWEEKLY (worked and rejected: KXHORMUZWEEKLY, KXBABELMANDEBWEEKLY, KXPANAMAWEEKLY) | Suez weekly ladder: sell both tails, implied sd ~22 vs realized ~10.6 from PortWatch | forecast | 2 refused 403 | refused_or_no_fill |
| A-0249 | 2026-09-17 | static | 10.4396 | KXTRUMPACT (bet); KXHORMUZWEEKLY / KXBABELMANDEBWEEKLY / KXSUEZWEEKLY / KXPANAMAWEEKLY (worked, passed) | Scrape whitehouse.gov to count 8 of week's actions, fade Fri+Sat remainder with day-of-week base rate | forecast | 2 refused 403 | refused_or_no_fill |
| A-0250 | 2026-09-18 | static | 6.0677 | KXAAAGASW (worked KXAAAGASD as anchor, KXDIESELW as control; glanced at KXWTI/KXWTIW) | Weekly AAA gas ladder: 3-day drift forecast from compressed retail-spot margin | forecast | 0W/3L -3.08 | lost_wrong_thesis |
| A-0251 | 2026-09-18 | static | 5.3733 | KXSUEZWEEKLY, KXBABELMANDEBWEEKLY, KXPANAMAWEEKLY (KXHORMUZWEEKLY worked and deliberately passed) | Rebuild PortWatch weekly chokepoint sums from public ArcGIS API; sell over-wide ladder tails | forecast | 4 refused 403 | refused_or_no_fill |
| A-0252 | 2026-09-18 | static | 6.3665 | KXSUEZWEEKLY (also worked KXBABELMANDEBWEEKLY, glanced at KXHORMUZWEEKLY and KXPANAMAWEEKLY) | Rebuild PortWatch weekly Suez sum, sell the over-wide upper tail of the ladder | forecast | 2 refused 403 | refused_or_no_fill |
| A-0253 | 2026-09-18 | static | 11.0038 | KXAMSAVO (bet); KXTSAW, KXTXERCOTPEAKD, KXBIGGESTQUAKE worked and passed | Rebuild 84 weekly USDA avocado ad-price reports; bet rebound after a deep promo week | forecast | 0W/2L -1.14 | lost_for_named_risk |
| A-0254 | 2026-09-18 | static | 14.7572 | KXLOWTSATX (worked the whole KXLOWT* daily-low family, 24 ladders / 144 markets; also surveyed KXAVGTK* streak family, passed) | Locked daily minimum: read the METAR 6-hour min group after sunrise, buy the settled bucket | forecast | 2W/0L +0.82 | won_for_stated_reason |
| A-0255 | 2026-09-18 | static | 3.5167 | KXBIGGESTQUAKE | Half-observed quake day: empirical 20-yr USGS remainder-of-day max vs Poisson-priced ladder | forecast | 4 open | open |
| A-0256 | 2026-09-18 | static | 7.4134 | KXTRUMPACT (bet); KXTRUTHSOCIAL (negative control); KXHORMUZWEEKLY/KXSUEZWEEKLY/KXPANAMAWEEKLY/Bab el-Mandeb PortWatch ladders and KXANTHSHARE/KXTOKENUSE (looked at, passed) | Count whitehouse.gov actions to date (8), fade Friday-afternoon+Saturday arrivals via base rate | forecast | 2 refused 403 | refused_or_no_fill |
| A-0257 | 2026-09-18 | static | 6.4866 | KXHORMUZPEAK, KXHORMUZMAX, KXHORMUZWEEKLY (Politics category) | Cross-ladder coherence: Hormuz peak-day and argmax ladders vs the weekly-total ladder | forecast | 5 refused 403 | refused_or_no_fill |
| A-0258 | 2026-09-18 | static | 3.5550 | KXTRUTHSOCIAL | Partial weekly Truth Social count from a Roll Call mirror plus a modeled 30-hour tail | forecast | 2 refused 403 | refused_or_no_fill |
| A-0259 | 2026-09-18 | static | 4.5654 | KXHORMUZWEEKLY, KXHORMUZPEAK, KXHORMUZMAX (IMF PortWatch Strait of Hormuz, week Sep 14-20) | Rebuild PortWatch Hormuz series, verify vs settled ladders, fade a 24h news-driven markdown | forecast | 5 refused 403 | refused_or_no_fill |
| A-0260 | 2026-09-18 | static | 5.7758 | KXCFNAI (primary, 4 legs); KXSPRLVL (1 independent leg); KXEIACRUDEW looked at and dropped (no book) | Nowcast CFNAI from already-published inputs; bet the ladder is stale after a same-day IP miss | inferred | 2W/2L -0.06; 1 no fill | mixed |
| A-0261 | 2026-09-18 | static | 9.5047 | KXGOOGSHARE (also modelled and passed: KXTOKENUSE, sibling OpenRouter *SHARE ladders) | Weekday-only partial weekly bucket overstates Google share; correct for 2 weekend days | forecast | 2W/1L +3.31 | won_for_stated_reason |
| A-0262 | 2026-09-18 | static | 7.2100 | KXSUEZWEEKLY (also worked KXBABELMANDEBWEEKLY, KXPANAMAWEEKLY, KXHORMUZWEEKLY; brief detours into KXTRUMPACT, KXSPRLVL) | Sell both tails of the Suez weekly ladder: implied sigma ~22 vs realised ~12 on PortWatch | forecast | 1 refused 403; 2 capped | refused_or_no_fill |
| A-0263 | 2026-09-18 | static | 4.8976 | KXTSAW (weekly TSA average, week ending Sep 20); KXAVGTK* weekly hot-streak ladders scouted and dropped | Partial-week TSA average: 4 of 7 days published, model the Thursday pass-through to the weekend | forecast | 1W/0L +1.66; 1 capped | won_for_stated_reason |
| A-0264 | 2026-09-18 | baseline | 9.1949 | KXHORMUZPEAK (bet); KXTSAW, KXANTHSHARE/OpenRouter share ladders, KXHORMUZWEEKLY, KXRAIN worked and dropped | Hormuz peak-day YES: PortWatch daily burstiness weighted by market's own weekly-total ladder | inferred | 1 capped | refused_or_no_fill |
| A-0265 | 2026-09-19 | static | 6.9403 | KXTRUTHSOCIAL | Read Roll Call's own JSON feed (the named settlement source), count a week 6/7 done | forecast | 2 refused 403 | refused_or_no_fill |
| A-0266 | 2026-09-19 | static | 5.1100 | KXTRUTHSOCIAL (bet); KXTRUMPSAY and KXANTHSHARE worked and rejected | Page Roll Call's JSON feed for the banked weekly count, base-rate the last Saturday | forecast | 1 refused 403 | refused_or_no_fill |
| A-0267 | 2026-09-19 | static | 9.2433 | KXHORMUZPEAK, KXHORMUZWEEKLY, KXBABELMANDEBWEEKLY (also modelled and passed KXSUEZWEEKLY, KXPANAMAWEEKLY) | Pull lagged IMF PortWatch series, measure unpublished Mon-Thu via calibrated AIS proxies | inferred | 5 refused 403 | refused_or_no_fill |
| A-0268 | 2026-09-19 | static | 14.2363 | KXDEEPSHARE, KXOPENSHARE, KXTENCENTSHARE (OpenRouter author-share ladders); also worked KXBABASHARE, KXGOOGSHARE, KXANTHSHARE; scanned and dropped KXAVGTK* and KXTRUTHSOCIAL | OpenRouter weekly share 75% banked; weekend remainder from last weekend's factor plus a live 1h cache-busted read | inferred | 4W/1L +6.70 | won_for_stated_reason |
| A-0269 | 2026-09-19 | baseline | 4.3217 | KXSUEZWEEKLY, KXBABELMANDEBWEEKLY, KXHORMUZPEAK (also worked KXHORMUZWEEKLY, KXPANAMAWEEKLY as controls) | IMF PortWatch chokepoint base rates vs war-headline-discounted weekly transit ladders | forecast | 3 refused 403 | refused_or_no_fill |
| A-0270 | 2026-09-19 | static | 5.6821 | KXSUEZWEEKLY (also worked and passed: KXHORMUZWEEKLY, KXBABELMANDEBWEEKLY, KXPANAMAWEEKLY) | Suez weekly transit condor: short both tails using full PortWatch archive vs stale ladder | forecast | 4 refused 403 | refused_or_no_fill |
| A-0271 | 2026-09-19 | static | 12.9464 | KXTRUTHSOCIAL (primary, refused); KXRAINWKND + KXRAINDNYC (filled); worked but passed: KXHORMUZWEEKLY/KXHORMUZPEAK/KXBABELMANDEBWEEKLY (IMF PortWatch), KXTRUMPSAY | Count a half-elapsed Truth Social week from a mirror, plus two rain duplicate/union crumbs | forecast | 2W/0L +0.34; 1 refused 403 | won_for_stated_reason |
| A-0272 | 2026-09-19 | static | 7.6595 | KXTRUTHSOCIAL (also looked at KXANTHSHARE/OpenRouter share ladders, KXTOKENUSE, KXTRUMPPHOTO, KXTRUMPACT, and a whole-board no-arbitrage sweep) | Count the week from Kalshi's named source (Roll Call JSON), bet the bucket holds on a quiet Saturday | forecast | 2 refused 403 | refused_or_no_fill |
| A-0273 | 2026-09-19 | static | 9.4184 | KXTOKENUSE (bet); KXAVGTK*, KXHORMUZ*/KXSUEZWEEKLY/KXPANAMAWEEKLY/KXBABELMANDEBWEEKLY, KXTSAW, KX*SHARE worked and passed | Difference OpenRouter's rolling 7-day token total against the fixed Sep 14-20 contract week | forecast | 1W/1L -1.24 | lost_for_named_risk |
| A-0274 | 2026-09-19 | static | 13.6427 | KXOPENSHARE, KXDEEPSHARE, KXANTHSHARE, KXGOOGSHARE, KXBABASHARE (OpenRouter weekly author request share, event 26SEP21); also worked and passed: KXHIGH*/KXLOWT* Sep-19 ladders, KXHORMUZWEEKLY, KXTOKENUSE | Weekend-composition swap: price OpenRouter author shares off one differenced prior weekend | inferred | 0W/6L -1.15; 1 no fill | lost_wrong_thesis |
| A-0275 | 2026-09-19 | static | 3.8785 | KXTSAW | Partly published TSA weekly mean; forecast Fri-Sun from a two-year post-Labor-Day ratio jump | forecast | 0W/2L -2.23 | lost_wrong_thesis |
| A-0276 | 2026-09-19 | static | 7.3579 | KXSUEZWEEKLY, KXPANAMAWEEKLY (worked and dropped: KXBABELMANDEBWEEKLY, KXHORMUZWEEKLY, KXTOKENUSE) | Backtested prior-6-week mean of PortWatch transits vs Suez ladder shape; forecast, not retrieval | forecast | 3 refused 403 | refused_or_no_fill |
| A-0277 | 2026-09-19 | static | 5.6142 | KXBABELMANDEBWEEKLY, KXSUEZWEEKLY, KXHORMUZWEEKLY (KXPANAMAWEEKLY used as control) | Rebuilt IMF PortWatch chokepoint series, verified vs 211 settlements, forecast unseen week | forecast | 4 refused 403 | refused_or_no_fill |
| A-0278 | 2026-09-19 | static | 7.7295 | KXTRUTHSOCIAL (bet); also worked KXHORMUZWEEKLY/KXSUEZWEEKLY/KXBABELMANDEBWEEKLY, KXAVGTK*, KXTOKENUSE, KXTRUMPPHOTO (passed) | Count Trump's posts from Roll Call's JSON feed with under two hours left in the week | inferred | 2 refused 403 | refused_or_no_fill |
| A-0279 | 2026-09-21 | static | 7.3677 | KXHORMUZWEEKLY, KXHORMUZPEAK (also surveyed KXSUEZWEEKLY, KXBABELMANDEBWEEKLY, KXPANAMAWEEKLY, KXHORMUZMAX) | Cross-market gap: Hormuz weekly-total ladder vs peak-day market, via day-split model | forecast | 3 refused 403 | refused_or_no_fill |
| A-0280 | 2026-09-21 | static | 4.9373 | KXBABELMANDEBWEEKLY (also worked KXHORMUZWEEKLY, KXPANAMAWEEKLY as controls) | Bab el-Mandeb weekly ladder: 401-week floor plus a book-accuracy residual study vs Hormuz | forecast | 2 refused 403 | refused_or_no_fill |
| A-0281 | 2026-09-21 | director | 7.1919 | KXCHICKENWINGM | Monthly USDA wing-price average 3/4 published; AR(1) forecast of the last weekly print | forecast | 2W/0L +0.68 | won_for_stated_reason |
| A-0282 | 2026-09-21 | focused | 14.5766 | KXRTX5090WS (placed); KXCHICKENWINGM (refused by per-market cap); also worked KXH200WS, KXB200WS, KXA100WS, KXH100WS, Vercel AI Gateway share ladders (KXGOOGVREQ etc., KXOPENSOURCESHARE) | Found Ornn GPU index API behind five ladders; bet RTX 5090 stays above strike 4 days out | forecast | 1W/0L +0.38; 1 capped | won_for_stated_reason |
| A-0283 | 2026-09-21 | static | 12.0064 | KXHIGHTLV, KXHIGHTSDF, KXLOWTATL (bet); KXCHICKENWINGM and Vercel AI Gateway share (KXOPENVREQ etc.) worked and passed | Station-bias-corrected GEFS+ECMWF ensemble to buy cheap temperature-ladder wings | forecast | 0W/4L -0.80 | lost_for_named_risk |
| A-0284 | 2026-09-22 | director | 9.9541 | KXCHICKENWINGM, KXIMAGEAI (bet); KXA100WS/KXH100WS/KXH200WS/KXB200WS/KXRTX5090WS, KXVIDEOAI, KXOPENINTAI, KXSPEECHAI (worked and passed) | Partly-published USDA monthly mean read from the settling PDF, plus a leaderboard-state read | inferred | 1W/0L +0.17; 1 refused 403 | won_for_stated_reason |
| A-0285 | 2026-09-22 | static | 4.9607 | KXLOWTMIA, KXLOWTMIN, KXLOWTNYC, KXLOWTPHIL (scanned all 17 East/Central KXLOWT ladders for Sep 22) | 3 AM overnight-low nowcast: min-so-far + dewpoint floor + 6-night cooling deltas vs stale ladders | inferred | 2W/1L +3.51; 1 open | mixed |
| A-0286 | 2026-09-22 | director | 14.2416 | KXOPENSOURCESHARE (bet); also worked KX*WS Ornn GPU ladders, KXOPENINTAI, KXVIDEOAI, KXSPEECHAI, KXIMAGEAI, KXCHICKENWINGM, and the seven Sep-22 Vercel lab ladders (KXOPENVREQ, KXOPENVSPEND, KXDEEPVREQ, KXGOOGVREQ, KXANTHVREQ, KXANTHVSPEND, KXMOONVSPEND) | Exact Vercel open-weights chart series, 3-day walk-forward forecast, buy the strike 1.5 RMSE below | forecast | 1W/0L +1.00 | won_lucky |
| A-0287 | 2026-09-22 | director | 5.7372 | KXMOONVSPEND, KXOPENSOURCESHARE (also screened KXOPENVREQ, KXOPENVSPEND, KXDEEPVREQ, KXGOOGVREQ) | Forecast new Vercel share ladders from scraped history; fade Moonshot's record-high rungs | forecast | 1W/2L -2.40; 1 no fill | lost_for_named_risk |
| A-0288 | 2026-09-22 | director | 9.2748 | KXMOONVSPEND, KXOPENSOURCESHARE (Vercel AI Gateway share ladders); also worked KXOPENVREQ/KXOPENVSPEND/KXANTHVSPEND/KXDEEPVREQ/KXGOOGVREQ, Ornn KX*WS, KXOPENINTAI/KXIMAGEAI, KXTRUEV | NO on Vercel share strikes set above the settling chart's 356-day record | forecast | 2W/1L +0.48 | mixed |
| A-0289 | 2026-09-22 | static | 7.8605 | KXLOWT (KXLOWTOKC, KXLOWTMIA; screened all 24 KXLOWT ladders) | Daily-minimum ladders after dawn: the recorded low caps the settle, price the late-day undercut | forecast | 2W/0L +1.11 | won_for_stated_reason |
| A-0290 | 2026-09-22 | static | 12.8067 | KXOPENINTAI, KX30YMORTW (also looked at KXIMAGEAI, KXSPEECHAI, KXVIDEOAI, KXSPRLVL) | Leaderboard leader-hold read from AA page payload, plus MND-to-PMMS mortgage nowcast | forecast | 1 refused 403; 1 capped | refused_or_no_fill |
| A-0291 | 2026-09-22 | static | 6.5969 | KXLOWTNYC, KXLOWTMIA (surveyed all 24 KXLOWT ladders; Boston, DC, Atlanta, Seattle, Trenton worked and passed) | Read ASOS 6-hour min (2-group) in METAR remarks as early read of CLI daily minimum | forecast | 1 open; 2 capped | open |
| A-0292 | 2026-09-22 | focused | 9.0047 | KXOPENVSPEND, KXMOONVSPEND, KXOPENVREQ (Vercel AI Gateway daily lab-share ladders, Sep 22); KXDEEPVREQ and KXGOOGVREQ worked but not bet | Live read of the Vercel export's in-progress UTC day that the settling chart page hides | measured | 5 capped | refused_or_no_fill |
| A-0293 | 2026-09-22 | director | 8.9119 | KXBIGGESTQUAKE, KXCHICKENWINGM (worked and declined: KXTXERCOTPEAKD, KXSPEECHAI, KXVIDEOAI, KXIMAGEAI, KXOPENINTAI, KXMOONVSPEND) | Daily quake max already on tape; late-window M5.2 odds from 26 years of USGS catalogue | inferred | 2 capped | refused_or_no_fill |
| A-0294 | 2026-09-22 | static | 5.5809 | KXDEEPVREQ, KXOPENVSPEND, KXMOONVSPEND, KXOPENVREQ (Vercel AI Gateway daily lab-share ladders, Sep 22); KXGOOGVREQ probed | Poll Vercel's leaderboard export for the live in-progress UTC day and bet strikes it can't reach | measured | 4 capped | refused_or_no_fill |
| A-0295 | 2026-09-22 | director | 7.9373 | KXOPENINTAI, KXIMAGEAI, KXVIDEOAI (KXSPEECHAI examined, not bet) | Read the named Artificial Analysis leaderboards now, measure their drift from Wayback captures | inferred | 3 capped | refused_or_no_fill |
| A-0296 | 2026-09-22 | director | 7.8196 | KXLOWTNYC, KXLOWTMIA, KXLOWTATL (bet); KXMOONVSPEND/KXOPENVSPEND/Vercel share ladders, KXCHICKENWINGM, KXOPENSOURCESHARE, KXOPENINTAI (worked and passed) | Daily-minimum temp bins already fixed by 6-hourly synoptic groups; bet evening won't undercut | forecast | 3 capped | refused_or_no_fill |
| A-0297 | 2026-09-22 | static | 6.3952 | KXH100WS (worked all five Ornn GPU ladders KXH100WS/KXH200WS/KXB200WS/KXA100WS/KXRTX5090WS; also examined KXDDR5WS/KXDDR5EWS and KXAAAGASD*) | Read the Ornn GPU index API directly and buy a stale YES below the live index | forecast | 1 capped | refused_or_no_fill |
| A-0298 | 2026-09-22 | baseline | 6.9829 | KXSOFRD (primary), KXHIGHLAX (secondary); worked but dropped: all 24-city KXHIGH*/KXLOWT* Sep-23 ladders | Bet SOFR stays at its 3-day post-hike print (NO 3.87, YES 3.85) plus cheap LAX warm tail | forecast | 0W/2L -0.49; 1 capped | lost_wrong_thesis |
| A-0299 | 2026-09-23 | static | 8.3758 | KXCHICKENWINGM (also worked and rejected: KXTRUEV, KXSPRLVL, KXHIGHLAX/KXRAIN Sep 22 ladders) | Read 2 of 4 weekly USDA wing prints from the PDF; bet the far strikes the arithmetic already decides | inferred | 2W/0L +0.22 | won_for_stated_reason |
| A-0300 | 2026-09-23 | static | 4.3839 | KXTOKENUSEM (sibling KXTOKENUSE read as a cross-check, not bet) | OpenRouter monthly token total: 25 of 30 trailing days already published, buy YES below estimate | forecast | 2 open | open |
| A-0301 | 2026-09-23 | static | 3.5174 | KXAVGTKPHX, KXAVGTKLAS (worked the whole KXAVGTK* weekly average-temperature streak family, 10 cities) | Measure 2 settled days of an hourly-mean streak ladder, forecast the other 5, fade the rungs | forecast | 2 open | open |
| A-0302 | 2026-09-23 | director | 7.2773 | KXTOKENUSE (also worked KXOPENSOURCESHARE, KXTOKENUSEM) | OpenRouter weekly tokens: 2 of 7 days banked (Mon via change-field inversion), band T142Y/T154N/T134Y | forecast | 3 open | open |
| A-0303 | 2026-09-23 | director | 5.3487 | KXTOKENUSEM, KXTENCENTSHARE (OpenRouter rankings); also scanned KXTOKENUSE, KXGOOGSHARE, KXDEEPSHARE and passed on them | Read OpenRouter's rankings API as the settling source; month total and Tencent share already mostly published | inferred | 4 open | open |
| A-0304 | 2026-09-23 | director | 8.0953 | KXOPENSOURCESHARE (filled, lost); KXTOKENUSEM (cap-refused); worked and passed KXBIGGESTQUAKE, KXAAAGASWNJ | Two-day-ahead forecast of Vercel open-weights share on a copied chart read, plus rolled OpenRouter month | forecast | 0W/1L -1.16; 1 capped | lost_wrong_thesis |
| A-0305 | 2026-09-23 | focused | 6.2191 | KXLOWT (KXLOWTMIA, KXLOWTEWR, KXLOWTTTN; KXLOWTPHX built then dropped; all 24 KXLOWT cities scanned) | Read banked daily minimum from METAR tenths and synoptic groups; NWS hourly guards the evening | forecast | 3 capped | refused_or_no_fill |
| A-0306 | 2026-09-23 | director | 12.1086 | KXTOKENUSEM, KXOPENSOURCESHARE, KXTENCENTSHARE (also worked KXTOKENUSE weekly, KXBIGGESTQUAKE) | Partly-published ladders: monthly tokens from settled weeklies, Vercel nowcast, Tencent model split | forecast | 1 open; 2 capped | open |
| A-0307 | 2026-09-23 | static | 8.7431 | KXHIGHTEWR, KXLOWTMIA, KXHIGHTATL (daily city temperature ladders, Sep 23); also worked KXLOWTSATX and KXLOWTEWR but dropped them | Read the settling temperature early from ASOS 6-hour remark groups, validated on 250 past settlements | forecast | 3 capped | refused_or_no_fill |
| A-0308 | 2026-09-23 | director | 3.7512 | KXBIGGESTQUAKE | Late-day earthquake max: running max 5.7 at 19:29Z, NO on 5.8 from same-hour catalog split | inferred | 1 capped | refused_or_no_fill |
| A-0309 | 2026-09-23 | baseline | 4.5996 | KXTXERCOTPEAKD, KXBIGGESTQUAKE (also worked and rejected: KXHIGH*/KXLOWT* same-day temperature ladders, KXAAAGASD) | Late-in-day bets on outcomes already mostly visible in live feeds (ERCOT dashboard, USGS) | forecast | 0W/1L -0.10; 2 capped | lost_for_named_risk |
| A-0310 | 2026-09-23 | director | 2.7642 | KXBIGGESTQUAKE (worked; also probed KXOPENVREQ and KXTXERCOTPEAKD and declined them) | Late-day earthquake max: sell first strike above a fixed M5.7 using an hour-split history | measured | 1 capped | refused_or_no_fill |
| A-0311 | 2026-09-23 | director | 5.4616 | KXTENCENTSHARE, KXGOOGSHARE (OpenRouter weekly author share); glanced at KXTOKENUSE/KXTOKENUSEM, KXHIGHNY, KXCHINAAI, KXSPEECHAI, KXIMAGEAI | Read the 44%-published OpenRouter weekly share bucket, then decompose Tencent by model SKU | inferred | 2 capped | refused_or_no_fill |
| A-0312 | 2026-09-23 | static | 10.4761 | KXHIGH*/KXLOWT* Sep 23 daily temperature ladders (48 ladders, 288 markets); glanced at KXAAAGASD* | Settle-already-observed temp ladders: TWC endpoint + 2,786-day offset calibration; passed | inferred | no legs | pass |

## Appendix B. Method and files

The per-attempt records, the five analyst reports, the leg codings and the verification scripts live in the session scratchpad and are not part of the repository. The per-attempt facts came from `attempts`, `bets`, `attempt_activity`, `attempt_reviews`, `director_runs` and `audit_log` in data/ledger.db, and the transcripts from each attempt's `logs/session.stream.jsonl`. Counterfactual grades for unplaced legs were checked by the skeptics against Kalshi's public market endpoint and match the ledger's own `hypothetical_outcome` column on all 73 Politics legs. Realized figures are exact ledger sums. Counterfactual figures are approximate and assume a fill at the limit.
