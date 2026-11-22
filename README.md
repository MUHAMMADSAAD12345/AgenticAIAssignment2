# SpaceMolt Multi-Agent System (FAST-NUCES Agentic AI, seven-day assignment)

A coordinator agent plans for two SpaceMolt pilots and delegates real work over A2A to two independent
worker services. The workers play the game through authenticated SpaceMolt MCP v2.

| Role | Process | Port | Skills |
|---|---|---|---|
| Coordinator / planner | `python -m mas.coordinator` | - | observe, plan (LLM + validation + fallback), delegate, recover |
| Mining / acquisition | `python -m mas.agents.mining_agent` | 9999 | `observe`, `mine_cycle` |
| Trading / logistics | `python -m mas.agents.trading_agent` | 9998 | `observe`, `accept_mission`, `sell_and_complete`, `list_item`, `explore_adjacent`, `relocate` |

Architecture diagram: `docs/architecture.svg` (PNG: `docs/architecture.png`). Report: `REPORT.md`.

## 1. Install (Windows, Anaconda Prompt or CMD)

```cmd
cd /d C:\MAS\SpaceMolt_MAS_Starter
py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip check
python -m scripts.verify_local
```

`verify_local` runs the starter self-test plus the MAS unit tests (schema validation, parsers, error
classification, plan validation, idempotent ledger, LLM budget). It needs no network.

Optional full offline rehearsal against a local mock of the game (kills and restarts agents on purpose):

```cmd
python -m tests.e2e_mock
```

The mock (`tests/mock_spacemolt_server.py`) is a test double only. It is never used for scoring.

## 2. Configure

```cmd
copy config.example.json config.json   (only if config.json is missing)
notepad config.json
```

`config.json` is already set for our team: `group_id` = `MenInBlack`, pilots `MenInBlack-P1` (A) and
`MenInBlack-P2` (B), both in the `nebula` empire. Per the instructor's naming rule (both pilots share the
team identifier, `<TeamID>-P1` / `<TeamID>-P2`), this replaces the `AI26-Gxx-A/B` pattern in the assignment PDF.

Pilot profiles: https://spacemolt.com/player/MenInBlack-P1 and https://spacemolt.com/player/MenInBlack-P2
Everything else has safe defaults (spend and distance soft caps, fuel and hull floors, mining caps).

Secrets never go in files:

* Pilot passwords: stored in Windows Credential Manager by `examples\pilot_setup.py register`
  (service `spacemolt-course-test`). If a pilot was created another way, run
  `python examples\pilot_setup.py status --username <name>` once and answer `y` to store it.
* Groq key: set it only in the terminal you start the coordinator from:
  `set /p GROQ_API_KEY=Enter key:` (hide the screen while typing).

## 3. Read-only live check (no game state changes)

```cmd
python -m scripts.live_check
```

For each pilot it initializes MCP, saves every tool schema to `logs\mcp_tools.json`, logs in, and prints
parsed status, stats, belts, bases, missions and market bids. Any `WARNING` line means the live
server differs from the documented field names; fix that before the assessed run.

## 4. Run the MAS (assessed run)

One command opens three windows (mining agent, trading agent, coordinator):

```cmd
set /p GROQ_API_KEY=Enter key:
START_MAS.cmd
```

Or by hand, each in its own activated window:

```cmd
python -m mas.agents.mining_agent --port 9999
python -m mas.agents.trading_agent --port 9998
python -m mas.coordinator --assessed
```

Useful coordinator flags: `--no-llm` (rule policy only), `--cycles N`, `--max-minutes M`,
`--worker-wait S`. `--assessed` opens and closes a row in `logs\runtime.sqlite3` automatically and
refuses to start once 8 hours are used. The coordinator stops by itself when all combined targets are met.

## 5. What a cycle does

1. The coordinator sends an A2A `observe` task per pilot (trading agent, mining agent as fallback).
   Workers answer with fresh game state (free MCP queries, no ticks).
2. It sums both pilots' Service Record counters and compares them with the targets and caps.
3. The LLM gets a compact JSON context (progress, budgets, cargo, candidate missions, recent failures) and
   returns one task per pilot plus a rationale. `mas/policy.validate_plan` enforces a skill whitelist,
   one task per pilot, mission ids from the fresh observation only, unsafe/combat missions rejected,
   mining caps and spend/distance allowances. Invalid plans, no key, or a low budget fall back to the
   deterministic policy. Uncovered pilots are filled by the fallback.
4. Tasks go out over A2A in parallel (mission id = `<group>-c<cycle>-<pilot>-<skill>`).
5. Workers validate every MCP call against the discovered schema, apply safety gates, and record each
   state-changing step in their SQLite ledger (`sent` then `confirmed`) before reporting a JSON result.

## 6. Reliability

| Failure | Handling |
|---|---|
| Worker killed mid-task | Coordinator logs `a2a.handoff_failed`, waits for the worker, re-sends the same mission id. Worker resumes from its checkpoint; confirmed steps are skipped. |
| Coordinator killed | On restart, unfinished tasks are re-sent with their original ids. A completed mission returns its stored result (`idempotency.replay_blocked`). |
| Reply lost after a sale/jump/travel | Step marked `unknown`; reconciled against fresh cargo/location before any retry. |
| `action_pending` / `in_transit` / `rate_limited` | Wait one tick / arrival time / `retry_after`, then retry. |
| `session_invalid` / expired | Re-login once and retry. |
| Invalid parameters | Rejected locally before sending (`mcp.invalid_params`). |
| Stale observation | Re-observed every cycle; `accept_mission` re-checks the offer and returns `stale_observation`. |
| Belt depleted / no gain | Alternate belt (`recovery.alternate_belt`); the coordinator records it in durable world memory and stops sending miners there. If every local belt is depleted, one pilot scouts the nearest affordable neighbour (`relocate`) and the other follows only once ore is proven. |
| Gateway HTTP 429 (60 requests/min per IP) | Shared cross-process pacer (`mas/pacer.py`, 50/min); a 429 becomes `mcp.rate_limited` and every process waits for Retry-After instead of reconnecting. |
| Coordinator restart | World memory is rebuilt from stored task results (`recovery.memory_rebuilt`); resumed tasks are re-validated against it. |
| Missing worker | `worker.missing` with backoff; the other pilot keeps working. |
| Low fuel / hull | Refuel/repair when docked within the spend allowance, otherwise `safety.stop`. |

Forbidden actions (attack, gifts, player trades, ship purchases, faction orders, register) are blocked in
`mas/mcp_game.py` regardless of what the planner says.

## 7. Logs, budget, runtime

* `logs\events.jsonl` (timeline) and `logs\events_<agent>.jsonl`: timestamp, agent, task id, A2A task id,
  MCP tool/action, sanitized args, server confirmation, errors, recovery actions, rationale. Secrets are
  redacted by `scripts/evidence.scrub`; the password is never passed to the logger.
* `python scripts\budget.py` shows LLM requests/tokens (shared ledger `logs\budget.sqlite3`).
* `python scripts\runtime.py status` shows assessed minutes used.

## 8. Baseline comparison

```cmd
python -m baseline.single_agent --username <practice-pilot> --minutes 30
python -m scripts.compare_runs --out logs\comparison.md
```

The baseline is one process, one pilot, a fixed loop, no A2A and no LLM. It reuses the same MCP client
so the comparison isolates the architecture.

## 9. Submission bundle

```cmd
python -m scripts.make_submission --group MenInBlack
```

Writes `submission\` (LLM usage summary, runtime summary, baseline comparison, re-scrubbed logs) and
`MenInBlack_submission.zip` (source without `.venv`, `.env` or raw logs).

## 10. Files

```
mas/coordinator.py          planner, delegation, restart recovery, runtime ledger
mas/agents/worker_base.py   A2A intake, mission idempotency, pilot locks, observe skill
mas/agents/mining_agent.py  mine_cycle
mas/agents/trading_agent.py accept/complete missions, sales, listings, exploration, relocation
mas/mcp_game.py             MCP session, schema validation, error recovery, forbidden actions
mas/pacer.py                cross-process HTTP pacer for the 60 req/min/IP gateway limit
mas/text_formats.py         parsers for the live server's plain-text replies
mas/ops.py                  idempotent mutations, reconcilers, safety gates, movement
mas/game_state.py           tolerant parsers for SpaceMolt objects
mas/policy.py               plan validation, fallback policy, progress vs targets
mas/llm.py                  budgeted Groq planner
mas/a2a_util.py             A2A server/client helpers
mas/store.py                SQLite memory
baseline/single_agent.py    single-agent baseline
scripts/live_check.py       read-only live MCP check
scripts/compare_runs.py     baseline vs MAS metrics
scripts/make_submission.py  submission bundle
tests/                      unit tests, mock server, e2e restart test (e2e_mock), e2e depletion/relocation test (e2e_relocate)
logs/RUN_LOG.md             per-run record of the assessed runs (with archive/.../INCIDENT.md)
```

Coding-assistant disclosure: parts of this code were written with an AI coding assistant; both group members
reviewed it and can explain every module (see `docs/DEFENSE_NOTES.md`).
