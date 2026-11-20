# Assessed run log (MenInBlack-P1 = A, MenInBlack-P2 = B)

Times are UTC on 2026-11-20. Minutes come from `logs/runtime.sqlite3` (`python scripts\runtime.py status`)
and LLM usage from `logs/budget.sqlite3` (`python scripts\budget.py`). Each run is closed at its
coordinator's last logged event.

| Run | Window | Minutes | What happened |
|---|---|---|---|
| 1 | 11:24-11:25 | 0.95 | Gateway HTTP 429 (60 requests/min per IP). Stopped. See `archive/2026-11-20_run1_and_incident/INCIDENT.md`, which also covers the e2e-harness incident (one mission accept per pilot). |
| 2 | 11:40-11:52 | 11.51 | First full chain: observe, plan, A2A, MCP. Pilots accepted different missions; the LLM planned `mine_cycle`; both flew to Commerce Fields (Haven's only belt) and got `depleted`; recovery flew them home and docked. Stopped because the planner did not know about the depletion. |
| 3 | 12:18-12:43 | 24.44 | Memory/relocation build. Coordinator rebuilt memory and resumed 2 unfinished mining tasks. The belt had regenerated a little: 20 mine actions gave 20 units (ore_mined 20). The fallback sent A to scout Market Prime (407): **no belts** there. The LLM sent A home (407). The fallback then re-scouted Market Prime (bug: a visited system with no belts was not treated as unusable). Stopped by hand during that jump. |

Totals after run 3 (live check 12:47): ore_mined 20 (10 + 10), distance 1,259 (A 1,250, B 9),
systems_explored 1 (A), credits_earned 0, credits_spent 0. Assessed time 36.9 / 480 min. LLM 6 / 300 requests,
11,583 / 250,000 tokens.

## Lessons and fixes (all with tests)

* Rate limit: shared cross-process pacer plus session reuse (run 1).
* Depletion memory, the `relocate` skill, scout-then-follow (run 2).
* A visited system with no belts counts as exhausted; a belt that yields again is no longer marked
  depleted; an avoid list beats a belt pinned by the plan; cargo names map to item ids (run 3). The cost
  was about 800 extra distance from the A round trip and re-jump.
