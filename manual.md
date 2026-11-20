# Manual steps (things only you can do)

Everything else (the coordinator, both worker agents, MCP client, memory, recovery, logs, baseline,
comparison script, tests, diagram, README, report template, defense notes) is already in this folder.
Work through these steps in order. Commands are for Windows Anaconda Prompt / CMD, run from the project folder.

## 1. Set up (about 15 min, both students)

1. Extract the ZIP, e.g. to `C:\MAS\SpaceMolt_MAS_Starter`, and open Anaconda Prompt there.
2. Install and test offline:
   ```cmd
   py -3.11 -m venv .venv
   .venv\Scripts\activate
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   python -m pip check
   python -m scripts.verify_local
   python -m tests.e2e_mock
   ```
   Both must end with `PASS`. `e2e_mock` uses ports 8765/9998/9999 and takes about 2 minutes.

## 2. Registering pilots: how it works

Read this once before registering anything.

* **Registration code.** One-time-use text from your SpaceMolt owner account. It only creates
  accounts. It is not a password and not a Groq key. Never paste it into a chat, a file or a screenshot.
* **Username.** Permanent; it can never be renamed. Type it exactly, with the same capital letters
  everywhere (register command, `config.json`, profile URL).
* **Password.** The game generates it (a long random token); you never choose it.
  `examples\pilot_setup.py` saves it straight into **Windows Credential Manager**
  (service name `spacemolt-course-test`) and never prints it. The MAS reads it from there.
* **Credential Manager is per Windows user, per PC.** The PC that runs the MAS must hold the passwords of
  **both** competition pilots (see 3.6 if you registered on two different laptops).
* **`register` is permanent.** If a call ends in an error after the request was sent, the account may
  still exist. Check before you try again (see 3.4).
* All commands below assume Anaconda Prompt in the project folder with `.venv` activated
  (prompt starts with `(.venv)`).

### 2.1 Get a registration code

1. Open https://spacemolt.com/dashboard in a browser and sign in (create the owner account if you have
   none). One student's owner account is enough for the group.
2. Find the registration code on the dashboard and copy it. If it is shown as `` `ABC...` `` with
   backticks, the script strips them. If a code stops working, refresh the dashboard for a new one.
3. Whether one code can register several pilots depends on the dashboard. If the second registration says
   the code is used or invalid, get a fresh code and continue.

## 3. Register the pilots

### 3.1 Choose the names

| Pilot | Username | Used for |
|---|---|---|
| Practice | `MIB-practice` | testing on the real server and the baseline run. Does **not** count. |
| Competition A | `MenInBlack-P1` | scored |
| Competition B | `MenInBlack-P2` | scored |

Team identifier: `MenInBlack`. Keep the practice name clearly different from the
`MenInBlack-P1/P2` pattern so nobody confuses it with a scored pilot. All three use the `nebula` empire.

### 3.2 Register the practice pilot first

```cmd
python examples\pilot_setup.py register --username MIB-practice --empire nebula
```

What you will see:

1. `Get registration code from https://spacemolt.com/dashboard` and a reminder line.
2. `Registration code (hidden):` Paste the code and press Enter. In CMD you paste with **right-click**
   (or Ctrl+V on Windows 10/11). **Nothing appears while you paste**; that is normal.
3. Success: `[PASS] Registered MIB-practice (nebula); password saved to Windows Credential Manager`.

### 3.3 Register the two competition pilots

Only after the practice pilot works:

```cmd
python examples\pilot_setup.py register --username MenInBlack-P1 --empire nebula
python examples\pilot_setup.py register --username MenInBlack-P2 --empire nebula
```

Each must print its own `[PASS] Registered ...` line.

### 3.4 If registration fails

| Message | Meaning | What to do |
|---|---|---|
| `SpaceMolt HTTP 4xx: error=...` mentioning code / invalid | bad, used or expired code | get a fresh code, run the same command again |
| `... error=...` mentioning username taken / exists | name already exists | if it was you (earlier attempt), go to 3.5; otherwise pick another name |
| `error=rate_limited` | too many attempts from your IP | wait 2-30 minutes, then retry |
| `Registration may have completed, but password was not machine-readable ... Do NOT retry blindly` | account probably exists, password not captured | **do not re-run register.** Check the dashboard / `https://spacemolt.com/player/<name>`. Recover the password through the dashboard, then store it with 3.5 |
| network error / timeout | unknown outcome | check `https://spacemolt.com/player/<name>` first. Re-run only if the profile does not exist |
| `No module named keyring` | venv not active | `.venv\Scripts\activate`, then retry |

### 3.5 Verify each pilot (read-only)

```cmd
python examples\pilot_setup.py status --username MIB-practice
python examples\pilot_setup.py status --username MenInBlack-P1
python examples\pilot_setup.py status --username MenInBlack-P2
```

Expected: `[PASS] Authenticated pilot: ...`, then credits, home system, and `Ore mined: 0`
(new pilots must start at 0). Also open `https://spacemolt.com/player/MenInBlack-P1` and `...-P2` in a
browser: the profile exists, shows the Nebula empire, and all Service Record counters are 0.

If `status` asks `Existing pilot password (hidden):`, the password is not in Credential Manager on this
PC (for example, you registered in the website client). Paste the password, then answer `y` to
`Store in Windows Credential Manager? [y/N]`.

Optional check without showing the password (prints `True` or `False`):

```cmd
python -c "import keyring; print(bool(keyring.get_password('spacemolt-course-test','MenInBlack-P1')))"
```

### 3.6 Back up the passwords, and use one PC for the run

The password exists only in Credential Manager on the PC you registered from. Back it up into a
password manager (Bitwarden, 1Password, your browser's vault) without showing it on screen:

```cmd
python -c "import keyring; print(keyring.get_password('spacemolt-course-test','MenInBlack-P1'), end='')" | clip
```

This copies it to the clipboard. Paste it into the password manager, then copy something else to
clear the clipboard. Repeat for `-B` and the practice pilot.

If student 1 registered `-A` on laptop 1 and student 2 registered `-B` on laptop 2, choose one
laptop for the MAS. On that laptop, run `python examples\pilot_setup.py status --username MenInBlack-P2`,
paste the password at the hidden prompt and answer `y`. Move it only through a password manager's
share feature or by typing it yourself. Never send it over WhatsApp or email, and never put it in a file.

Alternative to Credential Manager (only for the current window; not saved anywhere):
`set /p SPACEMOLT_PASSWORD_A=Password A:` and `set /p SPACEMOLT_PASSWORD_B=Password B:`. The input is
visible while typing, so hide the screen.

## 3b. Enter your group and pilots in `config.json`

### 3b.1 Competition config

```cmd
copy config.example.json config.json
notepad config.json
```

Change only the top lines (already set for team `MenInBlack` in `config.example.json`):

```json
{
  "group_id": "MenInBlack",
  "pilots": {
    "A": "MenInBlack-P1",
    "B": "MenInBlack-P2"
  },
  "starting_empire": "nebula",
  ...
```

* `group_id` prefixes every task id in the logs (e.g. `MenInBlack-c0003-A-mine_cycle`).
* The names in `pilots` must match the registered usernames **exactly** (same capitals).
* Keep the quotes and commas exactly as they are. Do not add a comma after the last item in a `{ }` block.
* Never put passwords or keys in this file. It is included in the submission.
* Leave every other setting as it is unless you have a reason. The defaults keep spending and distance
  under the scoring caps.

Save (Ctrl+S) and check that it loads:

```cmd
python -c "from mas import config; c=config.load(); print(c['group_id'], c['pilots'])"
```

Expected: `MenInBlack {'A': 'MenInBlack-P1', 'B': 'MenInBlack-P2'}`. A `JSONDecodeError ... line N` means a
typo on line N (usually a missing quote or a stray comma).

### 3b.2 Practice config (keeps practice separate)

Create `config.practice.json` with only the practice pilot:

```json
{
  "group_id": "MIB-PRACTICE",
  "pilots": { "A": "MIB-practice" }
}
```

Use a separate log folder for practice so it never mixes with assessed evidence or the assessed LLM
budget, and never add `--assessed`:

```cmd
set MAS_LOGS=logs_practice
python -m scripts.live_check --config config.practice.json
python -m baseline.single_agent --username MIB-practice --minutes 30
```

To try the full MAS on the practice pilot, open three windows, run `set MAS_LOGS=logs_practice` in each,
then:

```cmd
python -m mas.agents.mining_agent --port 9999 --config config.practice.json
python -m mas.agents.trading_agent --port 9998 --config config.practice.json
python -m mas.coordinator --config config.practice.json --max-minutes 20
```

Close those windows before the assessed run. A new window without `MAS_LOGS` writes to `logs\` again.
For step 6, compare the baseline with the MAS: `python -m scripts.compare_runs --logs logs_practice`
(practice MAS vs baseline), or copy the baseline lines into `logs\events.jsonl`.

## 4. Read-only live check (no gameplay)

```cmd
python scripts\preflight.py
python examples\mcp_discover.py
python -m scripts.live_check
```

Look at the `live_check` output for each pilot:

* `Parsed status` shows the right username, `nebula`, credits, fuel and cargo.
* `Parsed stats` shows numbers (not all `None`).
* At least one belt and one base are listed.
* Missions and market items parse.

If you see a `WARNING` line, the live server uses different field names from the docs. Open
`logs\mcp_tools.json` and the printed keys, and adjust the matching key list at the top of
`mas\game_state.py` (`STAT_KEYS`, `BELT_WORDS`) or the parser it names. Re-run `live_check` until it is clean.
I could not reach the game server from my environment, so this check is the one thing I could not do for you.

## 5. Assessed run (live, counts toward the 8 hours)

1. Get a new private Groq key at https://console.groq.com/keys.
2. In the Anaconda Prompt (hide the screen while typing):
   ```cmd
   set /p GROQ_API_KEY=Enter key:
   START_MAS.cmd
   ```
   Three windows open: mining agent, trading agent, coordinator (`--assessed`). It stops by itself when the
   combined targets are met. To stop early, press Ctrl+C in the coordinator window; restart with the same
   command and it resumes.
3. While it runs, check both public profiles at `https://spacemolt.com/player/MenInBlack-P1` and `...-P2`.
   If the stats there disagree with the coordinator's `progress` events, trust the profile and stop with Ctrl+C.
4. After the run:
   ```cmd
   python scripts\runtime.py status
   python scripts\budget.py
   python -m scripts.platform_score --earned <A+B> --spent <A+B> --ore <A+B> --trades <A+B> --exchange-sales <A+B> --distance <A+B> --systems <A+B>
   ```
   If targets were not met (thin market, depleted belt), run `START_MAS.cmd` again. The ledger prevents replays.

## 6. Baseline comparison

If you did not do it in step 3b.2, run the baseline on the practice pilot for about 30 minutes, then:

```cmd
python -m scripts.compare_runs --out logs\comparison.md
```

## 7. Fill in the report

In `REPORT.md`, replace every `FILL IN`: group, names, profile URLs, the outcome table (from the live
profiles), runtime and LLM usage, one real evidence chain (copy a task id from `logs\events.jsonl`),
the baseline table (from `logs\comparison.md`) and a short discussion, and the live restart you did.
Also update the AI-assistant disclosure line at the end of `README.md` if needed.

## 8. Prepare the defense (each student)

* Read `docs\DEFENSE_NOTES.md` and split the modules as suggested.
* Each of you: read your files end to end, run one cycle, and be able to point to the log lines for one decision.
* Rehearse the demo (step 9) once together.

## 9. Live TA demo checklist

1. Open both public profiles; show the names, the `nebula` empire and the six counters.
2. Start `START_MAS.cmd` (or the three commands from `README.md`). Show in the coordinator window:
   `plan.decided` (rationale), `task.assigned`, and in a worker window `mcp.result` / `game.confirmed`.
3. Controlled failure: press Ctrl+C in the mining agent window during a `mine_cycle`, wait for
   `a2a.handoff_failed` / `worker.missing` in the coordinator, restart it with
   `python -m mas.agents.mining_agent --port 9999`, and show `recovery.resume_mission`.
   Optionally kill and restart the coordinator to show `recovery.resume_task` and `idempotency.replay_blocked`.
4. If the TA asks for a behavior change, use the table at the end of `docs\DEFENSE_NOTES.md`.

## 10. Submit

```cmd
python -m scripts.make_submission --group MenInBlack
```

Before uploading `MenInBlack_submission.zip`, open it and search for your passwords, the registration code
and `gsk_`. Nothing should match. Add the two profile URLs (and any exchange-sale screenshot) to your
submission message.

## Never do

* Register a pilot twice, or put passwords, the registration code or the Groq key in any file, screenshot or chat.
* Play the competition pilots by hand and present it as autonomous.
* Transfer credits or items between your pilots, or use old accounts.
* Run assessed sessions without `--assessed` (it records the 8-hour budget honestly).
