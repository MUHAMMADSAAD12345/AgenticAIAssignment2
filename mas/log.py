"""Structured, redacted event logging (JSONL per agent + shared timeline)."""
from __future__ import annotations
import os
from scripts.evidence import record, scrub
from mas.config import LOGS

RUN_ID = os.environ.get('MAS_RUN_ID', 'mas')


def event(name, *, agent, task_id=None, **data):
    data.setdefault('run_id', RUN_ID)
    item = record(name, agent=agent, task_id=task_id, data=data, path=str(LOGS / f'events_{agent}.jsonl'))
    with open(LOGS / 'events.jsonl', 'a', encoding='utf-8') as f:
        import json
        f.write(json.dumps(item, ensure_ascii=False, default=str) + '\n')
    return item


def sanitize(args: dict | None) -> dict:
    return scrub(dict(args or {}))
