# Run 1 (assessed) and test-harness incident, 2026-11-20

All times UTC. Raw logs for this period are in this folder (`events*.jsonl`, the agents' SQLite files,
and `run1/*.out`).

## Assessed run 1: 11:24:23 to 11:25:20 (0.95 min in `logs/runtime.sqlite3`)

* Workers and coordinator were started with `--assessed --max-minutes 20`.
* Cycle 1: both pilots were observed over A2A (MCP `get_status` etc.), and the fallback policy assigned
  `accept_mission` (Titanium Extraction Contract) to both.
* Both accept tasks failed before any game action was sent. The gateway answered **HTTP 429**
  ("public API requests are capped at 60/min for your IP"). At that point every task reconnected and logged
  in again, and status reads made 4-5 queries each.
* The run was stopped by hand after about 1 minute. Its runtime row was closed at the coordinator's last
  logged event, 11:25:20.

## Incident: offline e2e test reached the real workers (about 11:30 to 11:35)

* While fixing the 429 problem, `python -m tests.e2e_mock` was run twice. Run 1's real worker processes
  were still listening on ports 9999/9998, so the test's own mock workers could not bind
  (`[Errno 10048]`). The test coordinator then sent its tasks to the **real** workers, which acted on
  the real pilots.
* Game actions confirmed by the server (the only state-changing results in `events.jsonl`):
  * 11:30:49 MenInBlack-P1 `accept_mission` `titanium_extraction_contract`
  * 11:30:49 MenInBlack-P2 `accept_mission` `copper_requisition`
* Other tasks the test sent (observe, accept, mine_cycle, explore_adjacent) failed on 429 before any
  game action. No travel, jump, mine, sell, refuel or purchase happened.
* A read-only check afterwards confirmed both pilots docked at grand_exchange (Haven) with 250 cr,
  all Service Record counters at 0, and one active mission each. Accepting a mission is free.
* These two acceptances were made by the MAS worker code, but they were triggered by the test
  harness, not by an assessed coordinator run. They are recorded here so they are not presented as
  part of an assessed run.

## Fixes made

* `tests/e2e_mock.py` refuses to start if ports 8765/9998/9999 are already in use.
* Shared gateway rate limit: `mas/pacer.py` (cross-process cap of 50 HTTP requests/min). A 429 is
  handled as `rate_limited` and waits for Retry-After instead of reconnecting.
* Each worker keeps one MCP session per pilot across tasks. Inside the mining loop, status is read with
  `get_status` only.
* The fallback policy gives the two pilots different missions.
* The archived agent databases were moved here so that the test's task ids (`MenInBlack-c000N-...`)
  cannot collide with the next run's ids in the duplicate-protection ledgers.
