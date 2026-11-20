"""Compare the single-agent baseline with the MAS from their JSONL event logs.

Usage: python -m scripts.compare_runs [--logs logs] [--out logs/comparison.md]
Metrics come only from logged, game-confirmed events (no invented numbers).
"""
from __future__ import annotations
import argparse
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path


def load(path):
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def ts(e):
    return datetime.fromisoformat(e['time_utc']).timestamp()


def metrics(rows):
    m = defaultdict(float)
    if not rows:
        return m
    m['minutes'] = (ts(rows[-1]) - ts(rows[0])) / 60
    for e in rows:
        d, ev = e.get('data') or {}, e['event']
        if ev == 'mcp.result':
            m['mcp_calls'] += 1
            m['mcp_mutations'] += 1 if d.get('mutation') else 0
        elif ev == 'mcp.error':
            m['mcp_errors'] += 1
        elif ev.startswith('recovery.'):
            m['recoveries'] += 1
        elif ev == 'game.confirmed':
            what = d.get('what')
            if what == 'mined':
                m['ore_units'] += max(0, d.get('cargo_gain') or 0)
                m['mine_actions'] += 1
            elif what == 'exchange_sale':
                m['sale_credits'] += d.get('credits') or 0
                m['sales'] += 1
            elif what == 'mission_completed':
                m['mission_credits'] += d.get('reward') or 0
                m['missions'] += 1
        elif ev == 'llm.plan':
            m['llm_requests'] += 1
            m['llm_toks'] += (d.get('in_toks') or 0) + (d.get('out_toks') or 0)
        elif ev == 'task.assigned':
            m['a2a_tasks'] += 1
        elif ev == 'plan.rejected':
            m['plans_rejected'] += 1
    m['credits'] = m['sale_credits'] + m['mission_credits']
    if m['minutes']:
        m['credits_per_hour'] = m['credits'] / m['minutes'] * 60
        m['ore_per_hour'] = m['ore_units'] / m['minutes'] * 60
    if m['mcp_mutations']:
        m['credits_per_mutation'] = m['credits'] / m['mcp_mutations']
    return m


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--logs', default='logs')
    p.add_argument('--out', default=None)
    a = p.parse_args()
    rows = load(Path(a.logs) / 'events.jsonl')
    runs = defaultdict(list)
    for e in rows:
        runs[(e.get('data') or {}).get('run_id', 'unknown')].append(e)
    keys = ['minutes', 'a2a_tasks', 'mcp_calls', 'mcp_mutations', 'mcp_errors', 'recoveries', 'mine_actions',
            'ore_units', 'sales', 'sale_credits', 'missions', 'mission_credits', 'credits', 'credits_per_hour',
            'ore_per_hour', 'credits_per_mutation', 'llm_requests', 'llm_toks', 'plans_rejected']
    names = sorted(r for r in runs if r not in ('unknown',) and not r.startswith('e2e'))
    table = {n: metrics(runs[n]) for n in names}
    lines = ['| metric | ' + ' | '.join(names) + ' |', '|---|' + '---|' * len(names)]
    for k in keys:
        lines.append(f'| {k} | ' + ' | '.join(f'{table[n].get(k, 0):.1f}' for n in names) + ' |')
    out = '\n'.join(lines)
    print(out)
    if a.out:
        Path(a.out).write_text('# Baseline vs MAS (from event logs)\n\n' + out + '\n', encoding='utf-8')
        print('written', a.out)


if __name__ == '__main__':
    main()
