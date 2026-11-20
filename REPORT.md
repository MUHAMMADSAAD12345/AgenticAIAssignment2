# SpaceMolt MAS: report

Group: **MenInBlack** | Members: FILL IN (Student 1), FILL IN (Student 2)
Pilots: `https://spacemolt.com/player/MenInBlack-P1`, `https://spacemolt.com/player/MenInBlack-P2`

## 1. Strategy

The scoring rewards productive activity (2,000 credits earned, 30 ore, one genuine sale, two systems)
while penalising waste (more than 1,500 credits spent or 1,500 AU travelled). Our strategy follows from that:

* **Missions first.** Mining supply missions pay far more than raw ore (about 5-45 cr/unit), so each
  pilot accepts the best mining mission at its home station before mining, and the trading agent keeps
  that ore for delivery.
* **Sell only the surplus** on the station exchange. This gives the genuine exchange sale and some extra
  credits without buying anything.
* **Short trips.** Mining uses the nearest belt in the home system; one pilot does a single shortest jump
  only if the combined `systems_explored` counter is below 2, then returns.
* **Spend nothing optional.** No ships, modules or buy orders. Refuel/repair only below safety floors and
  within a spend allowance (`spend_soft_cap` 1,200 < 1,500).
* **Stop when all targets are met** so later activity cannot push spend or distance over the bands.

## 2. Architecture

See `docs/architecture.svg`. Three roles in three processes:

* **Coordinator** (`mas/coordinator.py`): observes both pilots through A2A, computes combined progress,
  asks the LLM for a plan, validates it, delegates one task per pilot in parallel, records outcomes.
* **Mining agent** (A2A service, port 9999): belt selection and `mine_cycle` with game-confirmed cargo
  gains, alternate belts on depletion, returns to base and docks.
* **Trading/logistics agent** (A2A service, port 9998): mission acceptance and completion, surplus sales
  above a bid floor, optional listings, exploration jump.

Protocols: A2A v1 JSON-RPC between coordinator and workers (task payload `{mission_id, skill, pilot,
params}`, result as a JSON artifact); authenticated SpaceMolt MCP v2 between workers and the game
(initialize, tool schema discovery, login, queries, validated mutations).

Memory: `coordinator.sqlite3` (tasks, cycles, observations, heartbeat), one SQLite file per worker
(missions, mutation ledger, checkpoints, accepted missions), `budget.sqlite3`, `runtime.sqlite3`.

## 3. Where the LLM is used and why

The LLM chooses *which* task each pilot does next from fresh observations: which mission to take (reward vs
quantity vs cargo), whether to mine more or deliver now, whether exploration is worth the distance, and how
to react after a failed task (recent failures are in its context and trigger an extra planning call).
Routine checks (schemas, safety, duplicate prevention, ore reservation) are deterministic. Budget: at most
one call every 2 cycles plus failure-triggered calls, 2 per rolling minute, a reserve of 10 requests and
8,000 tokens, after which the deterministic policy takes over.

## 4. Real outcomes (live API counters after run 6, 2026-11-20 18:27 UTC)

Verify on the public profiles before submitting: https://spacemolt.com/player/MenInBlack-P1 and
https://spacemolt.com/player/MenInBlack-P2

| Statistic | Pilot A (P1) | Pilot B (P2) | Combined | Band marks |
|---|---|---|---|---|
| Credits earned | 4 | 2,462 | 2,466 | 8/8 |
| Credits spent | 0 | 0 | 0 | 3/3 |
| Ore mined | 10 | 55 | 65 | 4/4 |
| Trades completed / Exchange items sold | 1 / 1 | 3 / 18 | 4 / 19 | 4/4 |
| Distance traveled (AU) | 1,253 | 34 | 1,287 | 3/3 |
| Systems explored | 1 | 0 | 1 | 1.5/3 |
| **Total** (`python -m scripts.platform_score --earned 2466 --spent 0 --ore 65 --trades 4 --exchange-sales 19 --distance 1287 --systems 1`) | | | | **23.5/25** |

The last 1.5 marks were deliberately not pursued. A second explored system needs a jump of at least 326,
which would push combined distance past 1,500 (losing 2 marks to gain 1.5).

Assessed runtime used: 109.8 / 480 minutes over 6 runs (`python scriptsuntime.py status`; per-run detail in
`logs/RUN_LOG.md`). LLM usage: 16 / 300 requests, 32,854 / 250,000 tokens (`python scriptsudget.py`).
Model: `openai/gpt-oss-20b` on Groq.

Example evidence chain (real, task `MenInBlack-c0020-B-sell_and_complete`): `plan.decided` cycle-20
(run 5) -> `task.assigned` (trading agent) -> the run was stopped before the reply -> after the restart
(run 6) `recovery.resume_task` re-sent the same id -> `a2a.task_received` -> `mcp.result` `sell`
`{id: trade_crystal, quantity: 16}` with server confirmation "Sold 9x Trade Crystal for 2421 credits
from Grand Exchange Station. 7 unsold (not moved)" -> `task.result` "credits 270->2712".

## 5. Baseline comparison (FILL IN from `submission\baseline_comparison.md`)

The baseline (`baseline/single_agent.py`) is a single process controlling one pilot with a fixed
loop (accept first mining mission, mine nearest belt, return, complete, sell everything), no A2A and no LLM.
It uses the same MCP client, so differences come from planning, delegation and recovery.

| Metric | Baseline | MAS |
|---|---|---|
| Minutes run | | |
| Credits from confirmed sales + missions | | |
| Ore units mined | | |
| Credits per hour | | |
| Credits per game mutation | | |
| MCP errors / recoveries | | |
| LLM requests / tokens | 0 / 0 | |

Discussion (FILL IN, 3-5 sentences): e.g. the MAS runs both pilots in parallel, reserves mission ore
instead of selling it, rejects unsafe missions, and recovers from restarts without replaying sales; the
baseline is simpler and cheaper in LLM tokens but idles on failures and cannot use the second pilot.

Offline rehearsal on the local mock server (not game evidence, test double only): with the same mock
world, the MAS reached 4,624 confirmed credits vs 2,070 for the baseline, 112.8 vs 62.7 credits per mutation,
because the MAS used both pilots and completed three missions instead of one.

## 6. Failure and recovery evidence

`python -m tests.e2e_mock` kills the mining worker during a `mine_cycle` and the coordinator during a
`sell_and_complete`, then checks the outcome. Result on our machine: all checks PASS (targets met, task
resumed, mission resumed from ledger, session expiry re-login, `action_pending` wait, ledger sale quantity
equals the game's exchange counter, i.e. no duplicate sale). FILL IN: the live demo restart you performed
and the event lines it produced.

## 7. Limitations

* Field names for some responses (missions, market) were not fully documented, so parsers are tolerant
  and `scripts/live_check.py` prints a warning when the live server differs.
* If the game does not expose a statistic in `get_status`, the coordinator cannot stop on it automatically;
  use `--max-minutes` and check the public profiles.
* Market depth is dynamic; if no station buys an ore, the trading agent skips it rather than dumping it.
