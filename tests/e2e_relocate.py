"""End-to-end scenario against the LOCAL MOCK: home belts are depleted (as found on the live server in Haven).

Expected autonomous behaviour (rule policy, no LLM):
  1. a pilot tries to mine at home, the game answers `depleted`, and the coordinator records it in memory
  2. the planner sends ONE pilot to scout the nearest affordable neighbour (`relocate`); the other waits
  3. the scout mines there; once that system has proven yield, the other pilot follows
  4. no pilot is sent back to a belt known to be depleted

Run: python -m tests.e2e_relocate   (ports 8765/9998/9999 must be free; never talks to the real game)
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from tests.e2e_mock import ports_in_use

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def env(logs):
    e = dict(os.environ)
    e.update({'PYTHONPATH': str(ROOT), 'MAS_LOGS': str(logs), 'SPACEMOLT_MCP_URL': 'http://127.0.0.1:8765/mcp',
              'SPACEMOLT_PASSWORD_A': 'test-password', 'SPACEMOLT_PASSWORD_B': 'test-password',
              'MAS_NONINTERACTIVE': '1', 'MOCK_TICK': '0.2', 'MOCK_FAULTS': '0', 'MOCK_DEPLETED_HOME': '1',
              'MAS_RUN_ID': 'e2e-relocate', 'PYTHONIOENCODING': 'utf-8'})
    e.pop('GROQ_API_KEY', None)
    return e


def start(args, logs, name):
    out = open(Path(logs) / f'{name}.out', 'a')
    return subprocess.Popen([PY, '-m', *args], cwd=ROOT, env=env(logs), stdout=out, stderr=subprocess.STDOUT)


def main():
    busy = ports_in_use()
    if busy:
        raise SystemExit(f'E2E RELOCATE TEST ABORTED: ports {busy} are in use (are real MAS workers running?)')
    logs = Path(tempfile.mkdtemp(prefix='mas_relocate_'))
    procs = []
    try:
        procs.append(start(['tests.mock_spacemolt_server', '--port', '8765'], logs, 'mock'))
        time.sleep(3)
        procs.append(start(['mas.agents.mining_agent', '--port', '9999'], logs, 'mining'))
        procs.append(start(['mas.agents.trading_agent', '--port', '9998'], logs, 'trading'))
        time.sleep(4)
        coord = start(['mas.coordinator', '--no-llm', '--cycles', '10', '--worker-wait', '30'], logs, 'coord')
        coord.wait(timeout=900)
    finally:
        for p in procs:
            p.kill()
    ev = [json.loads(l) for l in open(logs / 'events.jsonl', encoding='utf-8')]
    assigned = [(e['data'].get('pilot'), e['data'].get('skill'), e['data'].get('params') or {})
                for e in ev if e['event'] == 'task.assigned' and e['data'].get('skill') != 'observe']
    results = [e['data'] for e in ev if e['event'] == 'task.result']
    relocs = [(p, prm.get('target_system')) for p, s, prm in assigned if s == 'relocate']
    reloc_results = [e['data'] for e in ev if e['event'] == 'task.result' and e['task_id'].endswith('-relocate')]
    mem = [e['data'] for e in ev if e['event'] == 'memory.updated']
    home_mines_after_learning = 0
    learned = False
    for e in ev:
        if e['event'] == 'memory.updated' and 'nebula_outer' in (e['data'].get('depleted_belts') or []):
            learned = True
        if learned and e['event'] == 'mcp.result' and e['data'].get('action') == 'travel' and \
                e['data'].get('args', {}).get('id') in ('nebula_belt', 'nebula_outer'):
            home_mines_after_learning += 1
    veil_yield = max((m.get('system_yield', {}).get('veil', 0) for m in mem), default=0)
    checks = {
        'home depletion learned into memory': learned,
        'one pilot scouted veil first': bool(relocs) and relocs[0][1] == 'veil',
        'scout mined ore in veil (proven yield)': veil_yield > 0,
        'second pilot followed to veil': len({p for p, t in relocs if t == 'veil'}) == 2,
        'never sent back to a depleted home belt': home_mines_after_learning == 0,
        'every relocate succeeded': bool(reloc_results) and all(r.get('ok') for r in reloc_results),
    }
    for k, v in checks.items():
        print(('[PASS] ' if v else '[FAIL] ') + k)
    print('relocations:', relocs, '| veil yield:', veil_yield, '| logs:', logs)
    if not all(checks.values()):
        raise SystemExit('E2E RELOCATE TEST FAILED')
    print('E2E RELOCATE TEST PASS')


if __name__ == '__main__':
    main()
