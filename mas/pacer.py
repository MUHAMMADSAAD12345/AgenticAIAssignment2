"""Cross-process HTTP request pacer for the SpaceMolt MCP gateway.

The live gateway caps requests at 60 per rolling minute *per IP address* (HTTP 429, JSON-RPC error -32029,
with a Retry-After header). Both pilots and both worker processes share one IP, so a per-process limit is
not enough: every outgoing HTTP request takes a slot from a SQLite table shared by all MAS processes.

The cap defaults to 50/min (headroom for the coordinator's own checks); set MAS_HTTP_PER_MIN to change it.
"""
from __future__ import annotations
import asyncio
import os
import sqlite3
import time

from mas.config import LOGS

WINDOW = 60.0
CAP = int(os.environ.get('MAS_HTTP_PER_MIN', '50'))
DB = LOGS / 'http_pacer.sqlite3'


def _connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(DB, timeout=10, isolation_level=None)


def _try_take(now: float) -> float:
    """Record a request slot if one is free; otherwise return seconds to wait."""
    db = _connect()
    try:
        db.execute('CREATE TABLE IF NOT EXISTS reqs(t REAL)')
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM reqs WHERE t < ?', (now - WINDOW,))
        count, oldest = db.execute('SELECT COUNT(*), MIN(t) FROM reqs').fetchone()
        if count < CAP:
            db.execute('INSERT INTO reqs(t) VALUES(?)', (now,))
            db.execute('COMMIT')
            return 0.0
        db.execute('COMMIT')
        return max(0.2, (oldest + WINDOW) - now)
    finally:
        db.close()


def penalize(seconds: float):
    """After a 429, block everyone until Retry-After by filling the window."""
    now = time.time()
    db = _connect()
    try:
        db.execute('CREATE TABLE IF NOT EXISTS reqs(t REAL)')
        start = now + seconds - WINDOW
        db.executemany('INSERT INTO reqs(t) VALUES(?)', [(start,)] * CAP)
    finally:
        db.close()


async def acquire():
    while True:
        wait = await asyncio.to_thread(_try_take, time.time())
        if wait <= 0:
            return
        await asyncio.sleep(min(wait, 15))


async def on_request(request):  # httpx event hook
    await acquire()
