"""End-to-end test against the LOCAL MOCK server, including forced failures:
  - kills the mining worker in the middle of a mine_cycle and restarts it
  - kills the coordinator while a trading task is running and restarts it
Then checks targets were reached and no sale was replayed.

Run: python -m tests.e2e_mock        (works on Windows and Linux; needs free ports 8765/9998/9999)
"""
from __future__ import annotations
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def env(logs):
    e = dict(os.environ)
    e.update({'PYTHONPATH': str(ROOT), 'MAS_LOGS': str(logs), 'SPACEMOLT_MCP_URL': 'http://127.0.0.1:8765/mcp',
              'SPACEMOLT_PASSWORD_A': 'test-password', 'SPACEMOLT_PASSWORD_B': 'test-password',
              'MAS_NONINTERACTIVE': '1', 'MOCK_TICK': '0.25', 'MOCK_FAULTS': '1', 'MAS_RUN_ID': 'e2e-mock'})
    e.pop('GROQ_API_KEY', None)
    return e


def start(args, logs, name):
    out = open(Path(logs) / f'{name}.out', 'a')
    return subprocess.Popen([PY, '-m', *args], cwd=ROOT, env=env(logs), stdout=out, stderr=subprocess.STDOUT)


def events(logs, agent='coordinator'):
    p = Path(logs) / f'events_{agent}.jsonl'
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def wait_for(pred, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.3)
    return False


def main():
    logs = Path(tempfile.mkdtemp(prefix='mas_e2e_'))
    procs = {}
    try:
        procs['mock'] = start(['tests.mock_spacemolt_server', '--port', '8765'], logs, 'mock')
        time.sleep(3)
        procs['mining'] = start(['mas.agents.mining_agent', '--port', '9999'], logs, 'mining')
        procs['trading'] = start(['mas.agents.trading_agent', '--port', '9998'], logs, 'trading')
        time.sleep(4)
        procs['coord'] = start(['mas.coordinator', '--no-llm', '--cycles', '14', '--worker-wait', '60'], logs, 'coord')

        assert wait_for(lambda: any(e['event'] == 'task.assigned' and e['data'].get('skill') == 'mine_cycle'
                                    for e in events(logs))), 'mine_cycle never dispatched'
        assert wait_for(lambda: sum(1 for e in events(logs, 'mining') if e['event'] == 'game.confirmed') >= 3)
        print('[TEST] killing mining worker mid mine_cycle')
        procs['mining'].kill()
        procs['mining'].wait()
        time.sleep(4)
        procs['mining'] = start(['mas.agents.mining_agent', '--port', '9999'], logs, 'mining')
        print('[TEST] mining worker restarted')

        assert wait_for(lambda: any(e['event'] == 'task.assigned' and e['data'].get('skill') == 'sell_and_complete'
                                    for e in events(logs)), 180), 'sell_and_complete never dispatched'
        time.sleep(0.8)
        print('[TEST] killing coordinator during trading task')
        procs['coord'].kill()
        procs['coord'].wait()
        time.sleep(2)
        procs['coord'] = start(['mas.coordinator', '--no-llm', '--cycles', '14', '--worker-wait', '60'], logs, 'coord')
        procs['coord'].wait(timeout=600)

        ev = events(logs)
        names = [e['event'] for e in ev]
        mining_ev = [e['event'] for e in events(logs, 'mining')]
        trading_ev = [e['event'] for e in events(logs, 'trading')]
        checks = {
            'targets met': 'mission.targets_met' in names,
            'coordinator resumed unfinished task': 'recovery.resume_task' in names,
            'A2A handoff failure detected': 'a2a.handoff_failed' in names or 'worker.missing' in names,
            'worker resumed mission from ledger': 'recovery.resume_mission' in mining_ev + trading_ev,
            'session expiry recovered (relogin)': 'recovery.relogin' in mining_ev + trading_ev,
            'action_pending recovered (wait)': 'recovery.wait' in mining_ev + trading_ev,
        }
        db = sqlite3.connect(logs / 'trading_agent.sqlite3')
        sells = db.execute("SELECT action_key, args FROM actions WHERE action='sell' AND status='confirmed'").fetchall()
        sold_qty = sum(json.loads(a)['quantity'] for _, a in sells)
        db.close()
        stats = asyncio.run(_final_stats(logs))
        exchange = sum(s['exchange_items_sold'] for s in stats.values())
        checks['no duplicate sale (ledger qty == game counter)'] = sold_qty == exchange > 0
        for k, v in checks.items():
            print(f'[{"PASS" if v else "FAIL"}] {k}')
        print('final combined stats:', {k: sum(s[k] or 0 for s in stats.values()) for k in next(iter(stats.values()))})
        print('logs:', logs)
        if not all(checks.values()):
            raise SystemExit(1)
        print('E2E MOCK TEST PASS')
    finally:
        for p in procs.values():
            if p.poll() is None:
                p.kill()


async def _final_stats(logs):
    os.environ.update(env(logs))
    from mas import config
    from mas.mcp_game import GameSession
    from mas.game_state import parse_status
    cfg = config.load()
    out = {}
    for pilot in cfg['pilots']:
        async with GameSession(cfg, pilot, 'e2e-check') as gs:
            out[pilot] = parse_status(await gs.call('get_status'))['stats']
    return out


if __name__ == '__main__':
    main()
