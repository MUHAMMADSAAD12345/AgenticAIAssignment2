"""Build the submission bundle: LLM usage summary, runtime summary, baseline comparison,
re-scrubbed logs, and a ZIP of the source without venv/secrets.

Usage: python -m scripts.make_submission --group AI26-G01
"""
from __future__ import annotations
import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from scripts.evidence import scrub

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / 'logs'
SECRET_PAT = re.compile(r'gsk_[A-Za-z0-9_-]{15,}')
EXCLUDE_DIRS = {'.venv', '__pycache__', '.git', 'submission', '.idea', '.vscode'}
EXCLUDE_FILES = {'.env', 'config.local.json'}


def llm_summary() -> str:
    db = LOGS / 'budget.sqlite3'
    if not db.exists():
        return 'No LLM usage recorded.\n'
    con = sqlite3.connect(db)
    rows = con.execute('SELECT status, COUNT(*), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) '
                       'FROM usage GROUP BY status').fetchall()
    first_last = con.execute('SELECT MIN(t), MAX(t) FROM usage').fetchone()
    con.close()
    total_req = sum(r[1] for r in rows)
    total_tok = sum(r[2] + r[3] for r in rows)
    out = ['| status | requests | input tokens | output tokens |', '|---|---|---|---|']
    out += [f'| {s} | {n} | {i} | {o} |' for s, n, i, o in rows]
    span = ''
    if first_last[0]:
        f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        span = f'\nFirst request: {f(first_last[0])}; last request: {f(first_last[1])}\n'
    return '\n'.join(out) + f'\n\nTotal: {total_req}/300 requests, {total_tok}/250000 tokens.{span}\n'


def runtime_summary() -> str:
    db = LOGS / 'runtime.sqlite3'
    if not db.exists():
        return 'No assessed sessions recorded.\n'
    con = sqlite3.connect(db)
    rows = con.execute('SELECT id, started, ended FROM sessions ORDER BY id').fetchall()
    con.close()
    f = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC') if t else 'OPEN'
    out = ['| session | start | stop | minutes |', '|---|---|---|---|']
    total = 0
    for i, s, e in rows:
        mins = ((e or s) - s) / 60
        total += mins
        out.append(f'| {i} | {f(s)} | {f(e)} | {mins:.1f} |')
    return '\n'.join(out) + f'\n\nTotal assessed runtime: {total:.1f} / 480 minutes.\n'


def scrub_file(src: Path, dst: Path):
    with src.open(encoding='utf-8') as fi, dst.open('w', encoding='utf-8') as fo:
        for line in fi:
            try:
                fo.write(json.dumps(scrub(json.loads(line)), ensure_ascii=False) + '\n')
            except ValueError:
                fo.write(SECRET_PAT.sub('[REDACTED]', line))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--group', default='AI26-Gxx')
    a = p.parse_args()
    sub = ROOT / 'submission'
    if sub.exists():
        shutil.rmtree(sub)
    (sub / 'logs').mkdir(parents=True)
    (sub / 'llm_usage_summary.md').write_text('# LLM usage summary\n\n' + llm_summary(), encoding='utf-8')
    (sub / 'runtime_summary.md').write_text('# Assessed runtime\n\n' + runtime_summary(), encoding='utf-8')
    if (LOGS / 'events.jsonl').exists():
        subprocess.run([sys.executable, '-m', 'scripts.compare_runs', '--logs', str(LOGS),
                        '--out', str(sub / 'baseline_comparison.md')], cwd=ROOT, check=False)
    for f in LOGS.glob('events*.jsonl'):
        scrub_file(f, sub / 'logs' / f.name)
    zpath = ROOT / f'{a.group}_submission.zip'
    with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in ROOT.rglob('*'):
            rel = f.relative_to(ROOT)
            if f.is_dir() or set(rel.parts) & EXCLUDE_DIRS or f.name in EXCLUDE_FILES or f.suffix in ('.zip', '.pyc'):
                continue
            if rel.parts[0] == 'logs':
                continue
            z.write(f, str(Path(f'{a.group}_SpaceMolt_MAS') / rel))
        for f in sub.rglob('*'):
            if f.is_file():
                z.write(f, str(Path(f'{a.group}_SpaceMolt_MAS') / 'submission' / f.relative_to(sub)))
    print('Wrote', sub, 'and', zpath)
    print('Check the ZIP for secrets before submitting (search for your pilot passwords and gsk_).')


if __name__ == '__main__':
    main()
