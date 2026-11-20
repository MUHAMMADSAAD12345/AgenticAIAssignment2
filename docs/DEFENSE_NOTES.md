# Defense notes

Each student is questioned separately. Suggested ownership (swap if you prefer, but each person must
be able to walk through their files line by line and explain the other person's at a high level).

| Student 1 | Student 2 |
|---|---|
| `mas/coordinator.py`, `mas/policy.py`, `mas/llm.py` | `mas/mcp_game.py`, `mas/ops.py`, `mas/game_state.py` |
| `mas/a2a_util.py`, `mas/agents/worker_base.py` | `mas/agents/mining_agent.py`, `mas/agents/trading_agent.py` |
| `baseline/`, `scripts/compare_runs.py` | `mas/store.py`, `tests/` |

## Task flow (one cycle)

1. Coordinator creates task `G01-c0007-A-observe-<ts>` and sends it over A2A JSON-RPC to the trading agent.
2. Trading agent opens an MCP session for pilot A: `initialize` -> `list_tools` -> `spacemolt_auth login`,
   then free queries `get_status`, `get_system`, `get_active_missions`, `get_missions`, `view_market`.
   The result goes back as a JSON artifact on the A2A task.
3. Coordinator sums both pilots' stats, builds a compact context and asks the LLM for a plan.
4. `validate_plan` checks it. Example rejections: unknown skill, mission id not in the fresh observation,
   combat mission, duplicate pilot. The rejected parts are replaced by the fallback policy.
5. Coordinator stores the task (`status=planned`), sends it (`dispatched`), and stores the result
   (`completed`/`failed`). Results feed the next cycle's `recent_tasks`, which is how the LLM sees failures.

## Why the system chose an action (how to answer)

Show `plan.decided` in `logs\events_coordinator.jsonl` (source + rationale), then the `task.assigned`
with the same task id, then the worker's `mcp.result` / `game.confirmed` lines with that task id.

* Mining location: nearest belt by POI position (fuel and distance budget), or the coordinator's
  `belt_poi_id`; on depletion/no gain the next belt is tried (`mining.belt_selected`, `recovery.alternate_belt`).
* Mission: highest reward among mining or market missions whose ore quantity fits within two cargo loads;
  combat/bounty/escort missions are always rejected.
* Sale: mission ore is reserved; only surplus is sold, and only if the station's best bid is above
  `min_sell_price` and the item has buyers. Station exchange sales count as Exchange Items Sold.
* Exploration: only if combined `systems_explored` < 2; the shortest connection is chosen and the pilot
  returns home, keeping distance (scored at most 1,500 AU) low.
* Stop: once all targets are met the coordinator stops, because extra activity only risks the spend and
  distance bands.

## Likely questions

**Where is MCP initialization and schema discovery?** `GameSession.connect` in `mas/mcp_game.py`:
`session.initialize()`, `list_tools()`, then each tool's `input_schema` is stored and its `action` enum
is used to route actions to tools.

**How are parameters validated?** `validate_args` checks required fields, unknown fields, JSON types,
enums and minimums against the discovered schema before any call is sent.

**How do you know an action really happened?** Every mutation is followed by a fresh `get_status`
(e.g. cargo increase after `mine`, POI after `travel`, docked flag after `dock`, credits/cargo after
`sell`). Only then is `game.confirmed` logged.

**What prevents a duplicate sale after a crash?** The worker writes `sent` to its `actions` table
before calling `sell` and `confirmed` after. On restart a `confirmed` step is skipped; a `sent` step with
no reply is reconciled by comparing cargo with the stored pre-state. A whole mission that already
completed returns its stored result (`idempotency.replay_blocked`).

**Why separate processes?** The coordinator only talks A2A. Each worker has its own SQLite file,
its own MCP sessions and can be killed and restarted independently, which the demo shows.

**How is the LLM budget enforced?** `mas/llm.py` reserves a row in `logs\budget.sqlite3` before every
call (shared with `scripts/budget.py`), waits for the rolling 60-second window (max 2 requests), keeps a
reserve of 10 requests and 8,000 tokens, and plans every 2nd cycle unless the last task failed.

**What if the LLM returns nonsense?** JSON parse failure or validation problems emit `plan.rejected`
and the fallback policy is used for that cycle.

**How are secrets kept out of logs?** The login call logs only the username (`log_args`), the
password comes from Windows Credential Manager or an env var, and `scrub()` redacts any key containing
password/token/session_id/api_key plus any `gsk_...` string.

**Stale observations?** Observations are refreshed every cycle; workers re-read state before every
mutation; `accept_mission` re-checks the live offer list.

## Live change the TA may ask for (where to edit)

| Request | Change |
|---|---|
| Mine fewer actions per cycle | `limits.max_mining_actions_per_cycle` in `config.json` |
| Do not sell below X | `limits.min_sell_price` |
| Disable LLM | run coordinator with `--no-llm` |
| Lower spend cap | `limits.spend_soft_cap` |
| Never explore | `targets.systems_explored` to 0, or remove the `explore_adjacent` branch in `fallback_plan` |
| Only one pilot | remove a pilot from `pilots` in `config.json` |
