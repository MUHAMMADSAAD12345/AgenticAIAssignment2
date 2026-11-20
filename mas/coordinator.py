"""Strategic coordinator: observes both pilots via A2A, plans (LLM or fallback), delegates, records outcomes.

Run:  python -m mas.coordinator --assessed
"""
from __future__ import annotations
import argparse
import asyncio
import sqlite3
import time

from mas import config
from mas.a2a_util import send_task, wait_for_worker
from mas.llm import Planner
from mas.log import event
from mas.policy import (SKILL_AGENT, allowances, combined_progress, fallback_plan, market_mission_need,
                        mission_candidates, sale_value, validate_plan)
from mas.store import Store

AGENT = 'coordinator'
RUNTIME_DB = config.LOGS / 'runtime.sqlite3'


class RuntimeLedger:
    """Same table as scripts/runtime.py; opened/closed automatically for assessed runs."""

    def __init__(self, cap):
        self.cap = cap
        self.db = sqlite3.connect(RUNTIME_DB)
        self.db.execute('CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY,started REAL,ended REAL)')
        self.db.commit()
        self.row = None

    def used(self):
        closed = self.db.execute('SELECT COALESCE(SUM(ended-started),0) FROM sessions WHERE ended IS NOT NULL').fetchone()[0]
        open_ = self.db.execute('SELECT started FROM sessions WHERE ended IS NULL').fetchall()
        return closed + sum(time.time() - r[0] for r in open_)

    def start(self, last_heartbeat):
        stale = self.db.execute('SELECT id,started FROM sessions WHERE ended IS NULL').fetchall()
        for sid, started in stale:
            end = max(started, last_heartbeat or started)
            self.db.execute('UPDATE sessions SET ended=? WHERE id=?', (end, sid))
            event('recovery.runtime_session_closed', agent=AGENT, session=sid, ended_at_last_heartbeat=end)
        if self.used() >= self.cap:
            raise SystemExit('Eight-hour assessed runtime cap reached')
        cur = self.db.execute('INSERT INTO sessions(started,ended) VALUES(?,NULL)', (time.time(),))
        self.db.commit()
        self.row = cur.lastrowid

    def stop(self):
        if self.row:
            self.db.execute('UPDATE sessions SET ended=? WHERE id=?', (time.time(), self.row))
            self.db.commit()
            self.row = None


class Coordinator:
    def __init__(self, cfg: dict, args):
        self.cfg, self.args = cfg, args
        self.store = Store(config.LOGS / 'coordinator.sqlite3')
        self.planner = Planner(cfg)
        self.workers = cfg['workers']
        self.gid = cfg['group_id']
        self.obs: dict[str, dict] = {}
        self.pilots = list(cfg['pilots'])
        self.idle_cycles = 0

    # ---- shared world memory (durable in coordinator.sqlite3, survives restarts) ----
    DEPLETION_TTL = 6 * 3600  # belts regenerate slowly; re-try a depleted belt after a few hours

    def memory(self) -> dict:
        mem = self.store.kv_get('world', None) or {}
        mem.setdefault('depleted_belts', {})
        mem.setdefault('system_yield', {})
        mem.setdefault('visited', {})
        mem.setdefault('learned', [])  # task ids already applied (a restart rebuild must not double-count yield)
        now = time.time()
        mem['depleted_belts'] = {b: v for b, v in mem['depleted_belts'].items() if now - v.get('t', 0) < self.DEPLETION_TTL}
        return mem

    def learn(self, task: dict, result: dict, when: float | None = None, quiet: bool = False):
        """Turn worker results into durable observations the planner uses next cycle."""
        mem, changed = self.memory(), False
        system = result.get('system')
        when = when or time.time()
        if time.time() - when > self.DEPLETION_TTL or task['task_id'] in mem['learned']:
            return
        mem['learned'] = (mem['learned'] + [task['task_id']])[-500:]
        if task['skill'] == 'mine_cycle':
            dep = list(result.get('depleted_belts') or [])
            if not dep and result.get('belt') and not result.get('mining_actions') and                     result.get('stop_reason') in ('depleted', 'too_sparse', 'no_gain', 'no_resources'):
                dep = [result['belt']]  # results written before depleted_belts existed
            for b in dep:
                mem['depleted_belts'][b] = {'system': system, 't': when, 'by': task['pilot']}
                changed = True
            units = result.get('ore_units_gained') or 0
            if units and result.get('belt') in mem['depleted_belts']:
                # belts regenerate: a belt that yielded ore again is no longer treated as depleted
                mem['depleted_belts'].pop(result['belt'], None)
                changed = True
            if system and (units or result.get('mining_actions')):
                mem['system_yield'][system] = mem['system_yield'].get(system, 0) + units
                changed = True
        if task['skill'] in ('relocate', 'explore_adjacent') and isinstance(result.get('explored'), dict):
            rep = result['explored']
            mem['visited'][rep.get('system')] = {k: rep.get(k) for k in ('name', 'police_level', 'bases', 'belts')}
            changed = True
        if changed:
            self.store.kv_set('world', mem)
            if quiet:
                return
            event('memory.updated', agent=AGENT, task_id=task['task_id'], depleted_belts=sorted(mem['depleted_belts']),
                  system_yield=mem['system_yield'], visited=sorted(k for k in mem['visited'] if k))

    def annotate(self, obs: dict):
        """Add memory-derived facts to fresh observations (belts known depleted, systems with proven yield)."""
        mem = self.memory()
        dep = mem['depleted_belts']
        belts_by_system = {s: set(v.get('belts') or []) for s, v in mem['visited'].items() if s}
        for o in obs.values():
            if o:
                here = (o.get('status') or {}).get('system')
                belts_by_system.setdefault(here, set()).update(b['id'] for b in o.get('belts') or [])
        # exhausted = no mining possible: every known belt depleted and no ore gained there since, OR the system
        # was visited and has no belts at all (e.g. Market Prime, a pure trade hub). Without the second rule the
        # planner re-scouted a belt-less system it had just left (live run 3, 2026-11-20).
        no_belts = {s for s, v in mem['visited'].items() if s and not (v.get('belts') or [])}
        exhausted = sorted((s for s, belts in belts_by_system.items()
                            if belts and belts <= set(dep) and not mem['system_yield'].get(s)))
        exhausted = sorted(set(exhausted) | no_belts)
        for o in obs.values():
            if not o:
                continue
            belts = o.get('belts') or []
            o['depleted_belts_here'] = [b['id'] for b in belts if b['id'] in dep]
            o['belts_ok'] = [b for b in belts if b['id'] not in dep]
            o['system_yield'] = mem['system_yield']
            o['exhausted_systems'] = exhausted
        mem['exhausted_systems'] = exhausted
        return mem

    def tid(self, cycle, pilot, skill):
        return f'{self.gid}-c{cycle:04d}-{pilot}-{skill}'

    async def dispatch(self, task: dict) -> dict:
        agent = task['agent']
        url = self.workers[agent]
        payload = {'mission_id': task['task_id'], 'skill': task['skill'], 'pilot': task['pilot'],
                   'params': task['params'], 'issuer': AGENT, 'cycle': task['cycle']}
        attempts = self.cfg['limits']['worker_retry_attempts']
        for attempt in range(1, attempts + 1):
            if not await wait_for_worker(url, agent, max_wait=self.args.worker_wait):
                self.store.task_update(task['task_id'], status='interrupted', error='worker_missing')
                event('worker.unavailable', agent=AGENT, task_id=task['task_id'], worker=agent)
                return {'ok': False, 'error': 'worker_missing'}
            self.store.task_bump_attempt(task['task_id'])
            self.store.task_update(task['task_id'], status='dispatched')
            event('task.assigned', agent=AGENT, task_id=task['task_id'], assignee=agent, skill=task['skill'],
                  pilot=task['pilot'], params=task['params'], attempt=attempt, rationale=task.get('rationale'))
            try:
                out = await send_task(url, payload, timeout=self.cfg['limits']['a2a_timeout_seconds'])
            except Exception as e:
                self.store.task_update(task['task_id'], status='interrupted', error=f'{type(e).__name__}: {str(e)[:150]}')
                event('a2a.handoff_failed', agent=AGENT, task_id=task['task_id'], worker=agent,
                      error=f'{type(e).__name__}: {str(e)[:150]}', attempt=attempt,
                      recovery='resend same mission_id; worker ledger prevents replay')
                await asyncio.sleep(min(5 * attempt, 20))
                continue
            result = out['result'] or {'ok': False, 'error': 'no_result_artifact'}
            status = 'completed' if out['state'] == 'completed' and result.get('ok') else 'failed'
            self.store.task_update(task['task_id'], status=status, a2a_task_id=out['a2a_task_id'], result=result,
                                   error=result.get('error'))
            if task['skill'] != 'observe':
                self.learn(task, result)
            event('task.result', agent=AGENT, task_id=task['task_id'], a2a_task_id=out['a2a_task_id'], worker=agent,
                  state=out['state'], ok=result.get('ok'), summary=result.get('summary'), error=result.get('error'),
                  seconds=out['seconds'], replayed=result.get('replayed_from_memory', False))
            return result
        return {'ok': False, 'error': 'handoff_failed'}

    async def observe(self, cycle: int, pilot: str) -> dict | None:
        for agent in ('trading', 'mining'):
            tid = f'{self.gid}-c{cycle:04d}-{pilot}-observe-{int(time.time())}'
            self.store.task_create(tid, cycle, pilot, agent, 'observe', {}, 'fresh observation before planning', 'rule')
            task = self.store.task_get(tid)
            task['agent'] = agent
            self.store.task_update(tid, agent=agent)
            res = await self.dispatch(task)
            if res.get('ok'):
                self.store.observe(pilot, agent, res)
                return res
            event('observe.failed', agent=AGENT, task_id=tid, via=agent, error=res.get('error'))
        prev = self.store.latest_observation(pilot)
        if prev and time.time() - prev['t'] < self.cfg['limits']['max_observation_age_seconds']:
            return prev['data']
        return None

    def rebuild_memory(self):
        """After a restart, re-derive world memory from durable task results (no game calls needed)."""
        for t in reversed(self.store.tasks_recent(300)):
            if t['skill'] in ('mine_cycle', 'relocate', 'explore_adjacent') and t['status'] in ('completed', 'failed')                     and isinstance(t.get('result'), dict):
                self.learn(t, t['result'], when=t.get('updated'), quiet=True)
        mem = self.memory()
        event('recovery.memory_rebuilt', agent=AGENT, depleted_belts=sorted(mem['depleted_belts']),
              system_yield=mem['system_yield'], visited=sorted(k for k in mem['visited'] if k))

    async def recover_unfinished(self):
        pending = [t for t in self.store.tasks_unfinished() if t['skill'] != 'observe']
        dep = sorted(self.memory()['depleted_belts'])
        for t in pending:
            if t['skill'] == 'mine_cycle' and dep:
                # re-validate against fresh memory: never re-send a miner to a belt learned to be depleted
                t['params'] = dict(t.get('params') or {}, avoid_belts=dep)
        for t in pending:
            event('recovery.resume_task', agent=AGENT, task_id=t['task_id'], skill=t['skill'], pilot=t['pilot'],
                  previous_status=t['status'], note='re-sending same mission id after restart')
        await asyncio.gather(*(self.dispatch(t) for t in pending))
        for t in self.store.tasks_unfinished():
            if t['skill'] == 'observe':
                self.store.task_update(t['task_id'], status='failed', error='superseded after restart')

    def context(self, cycle, progress):
        pilots = {}
        for p, o in self.obs.items():
            if not o:
                continue
            st = o['status']
            pilots[p] = {
                'where': o.get('summary'), 'docked': st['docked'], 'credits': st['credits'],
                'fuel_frac': round(st['fuel_frac'], 2), 'hull_frac': round(st['hull_frac'], 2),
                'cargo': st['cargo'], 'cargo_free': st['cargo_free'],
                'active_missions': [{'id': m['id'], 'title': m['title'], 'objectives': m['objectives'][:3],
                                     'pct': m.get('percent_complete')} for m in o.get('active_missions', [])][:3],
                'candidate_missions': mission_candidates(o, self.cfg)[:5],
                'system': st.get('system'),
                'belts_ok': [b['id'] for b in o.get('belts_ok', o.get('belts', []))][:3],
                'depleted_belts_here': o.get('depleted_belts_here', [])[:5],
                'connections': [(c['system_id'], c.get('distance')) for c in o.get('connections', [])][:5],
                'sale_value_now': int(sale_value(o)) if st['docked'] else 0,
                'market_mission_units_needed': market_mission_need(o),
                'mission_slots_free': max(0, self.cfg['limits']['max_active_missions_per_pilot']
                                          - len(o.get('active_missions', []))),
                'market_bids': {k: v.get('bid') for k, v in (o.get('market') or {}).items()},
            }
        recent = [{'pilot': t['pilot'], 'skill': t['skill'], 'status': t['status'], 'error': t['error'],
                   'summary': (t['result'] or {}).get('summary')} for t in self.store.tasks_recent(8) if t['skill'] != 'observe']
        mem = self.memory()
        return {'cycle': cycle, 'progress': progress['totals'], 'targets_met': progress['met'],
                'memory': {'system_yield': mem['system_yield'],
                           'exhausted_systems': next((o.get('exhausted_systems') for o in self.obs.values() if o), []),
                           'visited': {k: v.get('belts') for k, v in mem['visited'].items() if k}},
                'allowances': allowances(progress, self.cfg), 'pilots': pilots, 'recent_tasks': recent[:6]}

    async def run_cycle(self, cycle: int) -> bool:
        obs = await asyncio.gather(*(self.observe(cycle, p) for p in self.pilots))
        self.obs = dict(zip(self.pilots, obs))
        mem = self.annotate(self.obs)
        if not any(self.obs.values()):
            event('cycle.no_observation', agent=AGENT, task_id=f'cycle-{cycle}', note='all workers/pilots unavailable')
            await asyncio.sleep(30)
            return True
        progress = combined_progress(self.obs, self.cfg)
        event('progress', agent=AGENT, task_id=f'cycle-{cycle}', totals=progress['totals'], met=progress['met'],
              unknown=progress['unknown_stats'])
        if progress['unknown_stats']:
            event('stats.unknown', agent=AGENT, task_id=f'cycle-{cycle}', fields=progress['unknown_stats'],
                  note='get_status did not expose these counters; check public profiles and use --max-minutes')
        if progress['all_targets_met'] and self.cfg['limits']['stop_when_targets_met']:
            event('mission.targets_met', agent=AGENT, task_id=f'cycle-{cycle}', totals=progress['totals'])
            return False
        history = [t for t in self.store.tasks_recent(12) if t['skill'] != 'observe']
        recent_failure = any(t['status'] == 'failed' for t in history[:2])
        use_llm = (cycle % max(1, self.cfg['llm']['plan_every_n_cycles']) == 0) or recent_failure
        plan, source = None, 'fallback'
        if use_llm and not self.args.no_llm:
            plan = await self.planner.plan(self.context(cycle, progress), task_id=f'cycle-{cycle}')
            source = 'llm' if plan else 'fallback'
        assignments, problems = validate_plan(plan, self.obs, self.cfg, progress) if plan else ([], [])
        if plan and (problems or not assignments):
            event('plan.rejected', agent=AGENT, task_id=f'cycle-{cycle}', source='llm', problems=problems,
                  recovery='fallback policy for invalid parts')
        if not assignments:
            fb = fallback_plan(self.obs, progress, self.cfg, history)
            assignments, problems2 = validate_plan(fb, self.obs, self.cfg, progress)
            plan, source = fb, 'fallback'
        else:
            covered = {a['pilot'] for a in assignments}
            fb = fallback_plan({p: o for p, o in self.obs.items() if p not in covered}, progress, self.cfg, history)
            extra, _ = validate_plan(fb, self.obs, self.cfg, progress)
            if extra:
                assignments += extra
                source = 'llm+fallback'
        rationale = (plan or {}).get('rationale', '')
        self.store.cycle_start(cycle, source, rationale, assignments, progress['totals'])
        event('plan.decided', agent=AGENT, task_id=f'cycle-{cycle}', source=source, rationale=rationale,
              assignments=[(a['pilot'], a['skill']) for a in assignments])
        work = []
        for a in assignments:
            if a['skill'] == 'wait' or SKILL_AGENT.get(a['skill']) is None:
                continue
            tid = self.tid(cycle, a['pilot'], a['skill'])
            self.store.task_create(tid, cycle, a['pilot'], SKILL_AGENT[a['skill']], a['skill'], a['params'], rationale, source)
            work.append(self.dispatch(self.store.task_get(tid)))
        if not work:
            self.idle_cycles += 1
            await asyncio.sleep(20)
            return self.idle_cycles < 3
        self.idle_cycles = 0
        await asyncio.gather(*work)
        self.store.cycle_finish(cycle)
        return True

    async def run(self):
        ledger = RuntimeLedger(self.cfg['limits']['max_assessed_runtime_seconds']) if self.args.assessed else None
        if ledger:
            ledger.start(self.store.kv_get('heartbeat'))
        event('coordinator.started', agent=AGENT, pilots=self.cfg['pilots'], workers=self.workers,
              llm=self.planner.enabled, assessed=bool(ledger))
        try:
            self.rebuild_memory()
            await self.recover_unfinished()
            cycle = self.store.cycle_last()
            max_cycles = self.args.cycles or self.cfg['limits']['max_cycles']
            deadline = time.time() + self.args.max_minutes * 60 if self.args.max_minutes else None
            for _ in range(max_cycles):
                if deadline and time.time() >= deadline:
                    event('runtime.max_minutes_reached', agent=AGENT, minutes=self.args.max_minutes)
                    break
                if ledger and ledger.used() >= self.cfg['limits']['max_assessed_runtime_seconds']:
                    event('runtime.cap_reached', agent=AGENT)
                    break
                cycle += 1
                self.store.kv_set('heartbeat', time.time())
                if not await self.run_cycle(cycle):
                    break
                self.store.kv_set('heartbeat', time.time())
        finally:
            if ledger:
                ledger.stop()
                event('runtime.session_stopped', agent=AGENT, minutes_used=round(ledger.used() / 60, 1))
            b = self.planner.budget()
            event('coordinator.stopped', agent=AGENT, llm_requests=b['requests'], llm_toks=b['tokens'])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--config', default=None)
    p.add_argument('--assessed', action='store_true', help='record this run in the 8-hour assessed runtime ledger')
    p.add_argument('--cycles', type=int, default=0, help='stop after N cycles (0 = config max_cycles)')
    p.add_argument('--no-llm', action='store_true', help='use deterministic fallback policy only')
    p.add_argument('--max-minutes', type=float, default=0, help='stop after this many minutes (0 = no limit)')
    p.add_argument('--worker-wait', type=float, default=180, help='seconds to wait for a missing worker')
    args = p.parse_args()
    cfg = config.load(args.config)
    try:
        asyncio.run(Coordinator(cfg, args).run())
    except KeyboardInterrupt:
        print('Coordinator interrupted; state is in logs/coordinator.sqlite3 and will resume on restart.')


if __name__ == '__main__':
    main()
