# Assessed run log (MenInBlack-P1 = A, MenInBlack-P2 = B)

Times are UTC on 2026-11-20. Minutes come from `logs/runtime.sqlite3` (`python scripts\runtime.py status`)
and LLM usage from `logs/budget.sqlite3` (`python scripts\budget.py`). Each run is closed at its
coordinator's last logged event.

| Run | Window | Minutes | What happened |
|---|---|---|---|
| 1 | 11:24-11:25 | 0.95 | Gateway HTTP 429 (60 requests/min per IP). Stopped. See `archive/2026-11-20_run1_and_incident/INCIDENT.md`, which also covers the e2e-harness incident (one mission accept per pilot). |
| 2 | 11:40-11:52 | 11.51 | First full chain: observe, plan, A2A, MCP. Pilots accepted different missions; the LLM planned `mine_cycle`; both flew to Commerce Fields (Haven's only belt) and got `depleted`; recovery flew them home and docked. Stopped because the planner did not know about the depletion. |
| 3 | 12:18-12:43 | 24.44 | Memory/relocation build. Coordinator rebuilt memory and resumed 2 unfinished mining tasks. The belt had regenerated a little: 20 mine actions gave 20 units (ore_mined 20). The fallback sent A to scout Market Prime (407): **no belts** there. The LLM sent A home (407). The fallback then re-scouted Market Prime (bug: a visited system with no belts was not treated as unusable). Stopped by hand during that jump. |
| 4 | 12:54-13:41 | 46.0 | Restart recovery: resumed A's relocate (docked at Market Prime). First **genuine exchange sales**: A sold 1 Copper Ore for 4 cr at Market Prime; B sold 2 Silicon Ore for 20 cr at Grand Exchange. Both workers were **restarted mid-run** at 13:04 to load fixes: the coordinator logged `a2a.handoff_failed`, re-sent the same mission ids, and the workers logged `recovery.resume_mission`. B made 3 mining trips at Commerce Fields (it regenerates slowly and is shared with other pilots): 42 units. The validator rejected an LLM explore plan over the distance budget. Stopped on its 45-min cap. |
| 5 | 13:44-14:05 | 21.27 | A accepted Market Participation: Selling at Market Prime. B made 3 mining trips at Commerce Fields that yielded 2, 1 and 0 units (the belt is drained by other pilots faster than it regenerates); memory then marked it depleted. The LLM proposed exploring twice and the validator rejected both (distance budget). Stopped by hand: no productive action left in Haven within the distance budget. |

Totals after run 4 (live check 13:43): credits_earned 24 (4 + 20), credits_spent 0, ore_mined 62 (10 + 52),
trades_completed 2, exchange_items_sold 3, distance 1,274, systems_explored 1. Assessed time 83.9 / 480 min.
LLM 10 / 300 requests, 20,235 / 250,000 tokens. B holds 15/25 copper (Copper Requisition, 1,800 cr) and
19/30 iron (Iron Supply Run, 1,500 cr).

Run 4 fixes: `sell` replies of "No buyers ... not moved" are no longer counted as sales; mission objectives
with progress suffixes ("Copper Ore: 2/25") parse, so mission ore is kept; memory learning is idempotent
per task (a restart no longer double-counts yield); mining missions are not offered where no belt exists.

Totals after run 3 (live check 12:47): ore_mined 20 (10 + 10), distance 1,259 (A 1,250, B 9),
systems_explored 1 (A), credits_earned 0, credits_spent 0. Assessed time 36.9 / 480 min. LLM 6 / 300 requests,
11,583 / 250,000 tokens.

## Lessons and fixes (all with tests)

* Rate limit: shared cross-process pacer plus session reuse (run 1).
* Depletion memory, the `relocate` skill, scout-then-follow (run 2).
* A visited system with no belts counts as exhausted; a belt that yields again is no longer marked
  depleted; an avoid list beats a belt pinned by the plan; cargo names map to item ids (run 3). The cost
  was about 800 extra distance from the A round trip and re-jump.
