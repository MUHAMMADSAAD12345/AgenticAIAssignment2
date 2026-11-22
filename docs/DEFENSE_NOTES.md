# Defense notes

Each student is questioned separately. Suggested ownership (swap if you prefer, but each person must
be able to walk through their own files line by line and explain the other person's files at a high level).

| Muhammad Saad (i232084): planning and coordination | Muhammad Alyun (i230022): game access and agents |
|---|---|
| `mas/coordinator.py`: cycle, world memory (`learn`, `annotate`, `rebuild_memory`), restart recovery, runtime ledger | `mas/mcp_game.py`: MCP session, schema validation, login/session id, error classification, 429 handling |
| `mas/policy.py`: `validate_plan`, `fallback_plan`, relocation (`relocation_target`), selling rules (`sale_value`, `mission_reserve`, `listing_choice`) | `mas/pacer.py`: cross-process HTTP pacer (60 req/min/IP gateway) |
| `mas/llm.py`: budgeted Groq planner, prompt, budget ledger | `mas/text_formats.py`, `mas/game_state.py`: parsers for the live server's text replies |
| `mas/a2a_util.py`, `mas/agents/worker_base.py`: A2A intake, idempotency, session reuse | `mas/ops.py`: `read_status`, idempotent `mutate`, reconcilers, safety gates, movement |
| `baseline/`, `scripts/compare_runs.py`, `scripts/platform_score.py` | `mas/agents/mining_agent.py`, `mas/agents/trading_agent.py` (incl. `relocate`), `mas/store.py`, `tests/` |

## Task flow (one cycle)

1. The coordinator sends an A2A `observe` task per pilot (e.g. `MenInBlack-c0007-A-observe-<ts>`) to the trading
   agent, falling back to the mining agent.
2. The worker uses its **reused** MCP session for that pilot (opened once: `initialize` → `list_tools` →
   `spacemolt_auth login`). It reads status: `get_status` (text), `get_player` (JSON stats), `get_system` /
   `get_poi` (location ids), `get_cargo`, missions and market. Every HTTP request takes a slot from the
   shared pacer.
3. The coordinator annotates the observations with world memory (`belts_ok`, `depleted_belts_here`,
   `system_yield`, `exhausted_systems`), sums both pilots' counters, and asks the LLM (every 2nd cycle or
   after a failure) for one task per pilot.
4. `validate_plan` checks the plan: skill whitelist, mission id in the fresh observation, unsafe missions,
   mission cap, mining on depleted belts, relocation adjacency and distance budget, explore budget. Invalid
   parts are replaced by `fallback_plan`.
5. Tasks go out over A2A in parallel. The coordinator stores `planned` → `dispatched` → `completed`/`failed`,
   then `learn()` turns the results into memory (depleted belts, yield per system, visited systems).

## Why the system chose an action (how to answer)

Show `plan.decided` in `logs\events_coordinator.jsonl` (source + rationale), then the `task.assigned` with the same
task id, then the worker's `mcp.result` / `game.confirmed` lines with that task id.

* **Selling:** if a docked pilot's surplus (cargo minus `mission_reserve`) has buy orders worth ≥ 50 cr, sell
  now. Items with no buy orders are skipped ("No buyers … not moved" is never counted as a sale). That is
  how B sold 9 Trade Crystals for 2,421 cr into the Grand Exchange station order.
* **Mining location:** only belts in `belts_ok`; the mining agent skips `avoid_belts` even if the plan pinned one.
* **Relocation (scout then follow):** if every belt here is depleted, follow a pilot whose system has proven yield;
  else wait if someone is already scouting; else scout the nearest neighbour that isn't exhausted and fits the
  distance budget. A visited system with no belts (Market Prime) counts as exhausted.
* **Missions:** only missions finishable at the current station (every objective "Mine N units of X Ore" with a
  usable belt here, or "Sell N units on the exchange"); combat/escort/bounty are always rejected.
* **Stop:** when all targets are met, or after 3 idle cycles (nothing productive within the budgets).

## Likely questions

**Where is MCP initialization and schema discovery?** `GameSession.connect` in `mas/mcp_game.py`:
`session.initialize()`, `list_tools()`, then each tool's `input_schema` is stored (`logs/mcp_tools.json`). Actions are routed
to the tool whose schema lists them, or to the documented default (`spacemolt` takes a free-form action string).

**How are parameters validated?** `validate_args` checks required fields, unknown fields, JSON types, enums and
minimums against the discovered schema before anything is sent (`mcp.invalid_params`).

**How does login work on the live server?** The login reply is plain text ("Session ID: …"); `login()` extracts the
id and sends it as `session_id` on every call. Expired sessions trigger one re-login (`recovery.relogin`).
The session id is redacted from all logs (`scripts/evidence.py`).

**Why does the system wait sometimes?** The gateway caps HTTP requests at 60/min per IP for everything we run.
`mas/pacer.py` keeps a shared SQLite window (50/min). On a 429 it records Retry-After, and every process pauses
instead of reconnecting, because a reconnect plus re-login would cost even more requests.

**How do you know an action really happened?** Every mutation is followed by a fresh read (cargo after `mine`,
POI after `travel`, docked flag after `dock`, system after `jump`, cargo and credits after `sell`). Only then
is `game.confirmed` logged; a sell that moved nothing is logged as `trade.not_filled`.

**What prevents a duplicate sale after a crash?** The worker writes `sent` to its `actions` table before calling
`sell` and `confirmed` after. On restart a `confirmed` step is skipped; a `sent` step with no reply is reconciled
by comparing cargo with the stored pre-state. A completed mission id returns its stored result
(`idempotency.replay_blocked`). Live proof: run 4's worker restart and run 6's resumed sale (REPORT §6).

**Why separate processes?** The coordinator only talks A2A. Each worker has its own SQLite ledger and MCP
sessions and can be killed and restarted independently, which the demo shows.

**Why is your score 25 on the profile but 23.5 in the API?** The public profile counts the home system in
*Systems Explored*; the API's `systems_explored` does not. REPORT §4 documents both. We didn't jump again
because it would exceed the 1,500 AU band.

**What went wrong, honestly?** Run 1 hit the 429 limit. A test harness once reached the real workers (two
free mission accepts, documented in `logs/archive/…/INCIDENT.md`). A re-scouting bug in run 3 cost about 800 AU;
it was fixed and a regression test added. All of this is in `logs/RUN_LOG.md`.
