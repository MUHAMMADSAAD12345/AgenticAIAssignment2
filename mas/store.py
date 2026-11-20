"""Durable SQLite memory shared by coordinator and workers (each process uses its own file)."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY, cycle INT, pilot TEXT, agent TEXT, skill TEXT,
  params TEXT, status TEXT, a2a_task_id TEXT, attempts INT DEFAULT 0,
  rationale TEXT, plan_source TEXT, result TEXT, error TEXT,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS cycles(
  cycle INT PRIMARY KEY, started REAL, finished REAL, plan_source TEXT,
  rationale TEXT, plan TEXT, progress TEXT);
CREATE TABLE IF NOT EXISTS observations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL, pilot TEXT, source TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS missions(
  mission_id TEXT PRIMARY KEY, agent TEXT, skill TEXT, pilot TEXT, params TEXT,
  status TEXT, checkpoint TEXT, result TEXT, error TEXT, runs INT DEFAULT 0,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS actions(
  action_key TEXT PRIMARY KEY, mission_id TEXT, pilot TEXT, tool TEXT, action TEXT,
  args TEXT, status TEXT, pre_state TEXT, response TEXT, error TEXT,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT, updated REAL);
"""


def _j(x):
    return None if x is None else json.dumps(x, ensure_ascii=False, default=str)


def _u(x):
    return None if x is None else json.loads(x)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(SCHEMA)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _exec(self, sql, args=()):
        db = self._db()
        try:
            cur = db.execute(sql, args)
            db.commit()
            return cur
        finally:
            db.close()

    def _rows(self, sql, args=()):
        db = self._db()
        try:
            return [dict(r) for r in db.execute(sql, args).fetchall()]
        finally:
            db.close()

    # key/value
    def kv_get(self, k, default=None):
        rows = self._rows('SELECT v FROM kv WHERE k=?', (k,))
        return _u(rows[0]['v']) if rows else default

    def kv_set(self, k, v):
        self._exec('INSERT INTO kv(k,v,updated) VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated=excluded.updated',
                   (k, _j(v), time.time()))

    # coordinator tasks
    def task_create(self, task_id, cycle, pilot, agent, skill, params, rationale, plan_source):
        now = time.time()
        self._exec('INSERT OR IGNORE INTO tasks(task_id,cycle,pilot,agent,skill,params,status,rationale,plan_source,created,updated) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (task_id, cycle, pilot, agent, skill, _j(params), 'planned', rationale, plan_source, now, now))

    def task_update(self, task_id, **fields):
        if not fields:
            return
        for k in ('params', 'result'):
            if k in fields:
                fields[k] = _j(fields[k])
        fields['updated'] = time.time()
        cols = ', '.join(f'{k}=?' for k in fields)
        self._exec(f'UPDATE tasks SET {cols} WHERE task_id=?', (*fields.values(), task_id))

    def task_bump_attempt(self, task_id):
        self._exec('UPDATE tasks SET attempts=attempts+1, updated=? WHERE task_id=?', (time.time(), task_id))

    def task_get(self, task_id):
        rows = self._rows('SELECT * FROM tasks WHERE task_id=?', (task_id,))
        return self._task(rows[0]) if rows else None

    def tasks_unfinished(self):
        return [self._task(r) for r in self._rows(
            "SELECT * FROM tasks WHERE status IN ('planned','dispatched','interrupted') ORDER BY created")]

    def tasks_recent(self, n=10):
        return [self._task(r) for r in self._rows('SELECT * FROM tasks ORDER BY updated DESC LIMIT ?', (n,))]

    @staticmethod
    def _task(r):
        r['params'] = _u(r['params'])
        r['result'] = _u(r['result'])
        return r

    # cycles
    def cycle_last(self):
        rows = self._rows('SELECT MAX(cycle) AS c FROM cycles')
        return rows[0]['c'] or 0

    def cycle_start(self, cycle, plan_source, rationale, plan, progress):
        self._exec('INSERT OR REPLACE INTO cycles(cycle,started,plan_source,rationale,plan,progress) VALUES(?,?,?,?,?,?)',
                   (cycle, time.time(), plan_source, rationale, _j(plan), _j(progress)))

    def cycle_finish(self, cycle):
        self._exec('UPDATE cycles SET finished=? WHERE cycle=?', (time.time(), cycle))

    # observations
    def observe(self, pilot, source, data):
        self._exec('INSERT INTO observations(t,pilot,source,data) VALUES(?,?,?,?)', (time.time(), pilot, source, _j(data)))

    def latest_observation(self, pilot):
        rows = self._rows('SELECT * FROM observations WHERE pilot=? ORDER BY id DESC LIMIT 1', (pilot,))
        if not rows:
            return None
        r = rows[0]
        r['data'] = _u(r['data'])
        return r

    # worker missions (idempotency of A2A tasks)
    def mission_get(self, mission_id):
        rows = self._rows('SELECT * FROM missions WHERE mission_id=?', (mission_id,))
        if not rows:
            return None
        r = rows[0]
        for k in ('params', 'checkpoint', 'result'):
            r[k] = _u(r[k])
        return r

    def mission_begin(self, mission_id, agent, skill, pilot, params):
        now = time.time()
        self._exec('INSERT OR IGNORE INTO missions(mission_id,agent,skill,pilot,params,status,checkpoint,created,updated) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (mission_id, agent, skill, pilot, _j(params), 'running', _j({}), now, now))
        self._exec("UPDATE missions SET runs=runs+1, status='running', updated=? WHERE mission_id=?", (now, mission_id))
        return self.mission_get(mission_id)

    def mission_checkpoint(self, mission_id, checkpoint):
        self._exec('UPDATE missions SET checkpoint=?, updated=? WHERE mission_id=?', (_j(checkpoint), time.time(), mission_id))

    def mission_finish(self, mission_id, status, result=None, error=None):
        self._exec('UPDATE missions SET status=?, result=?, error=?, updated=? WHERE mission_id=?',
                   (status, _j(result), error, time.time(), mission_id))

    # MCP mutation ledger
    def action_get(self, key):
        rows = self._rows('SELECT * FROM actions WHERE action_key=?', (key,))
        if not rows:
            return None
        r = rows[0]
        for k in ('args', 'pre_state', 'response'):
            r[k] = _u(r[k])
        return r

    def action_sent(self, key, mission_id, pilot, tool, action, args, pre_state):
        now = time.time()
        self._exec('INSERT OR REPLACE INTO actions(action_key,mission_id,pilot,tool,action,args,status,pre_state,created,updated) '
                   'VALUES(?,?,?,?,?,?,?,?,?,?)', (key, mission_id, pilot, tool, action, _j(args), 'sent', _j(pre_state), now, now))

    def action_done(self, key, status, response=None, error=None):
        self._exec('UPDATE actions SET status=?, response=?, error=?, updated=? WHERE action_key=?',
                   (status, _j(response), error, time.time(), key))

    def actions_count(self, action=None, status='confirmed'):
        if action:
            return self._rows('SELECT COUNT(*) AS n FROM actions WHERE action=? AND status=?', (action, status))[0]['n']
        return self._rows('SELECT COUNT(*) AS n FROM actions WHERE status=?', (status,))[0]['n']
