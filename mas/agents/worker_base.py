"""Shared worker runtime: A2A task intake, mission idempotency, pilot locking, MCP session lifecycle."""
from __future__ import annotations
import asyncio
import time

from mas import config
from mas.game_state import belts_by_distance
from mas.log import event
from mas.mcp_game import GameError, GameSession
from mas.ops import PilotOps, SafetyStop
from mas.store import Store


class Worker:
    agent = 'worker'
    description = ''
    skills: dict[str, str] = {}

    def __init__(self, cfg: dict | None = None, db_path: str | None = None):
        self.cfg = cfg or config.load()
        self.store = Store(db_path or (config.LOGS / f'{self.agent}_agent.sqlite3'))
        self.pilot_locks: dict[str, asyncio.Lock] = {}
        self.inflight: dict[str, asyncio.Future] = {}

    async def handle(self, payload: dict, progress, a2a_task_id: str) -> dict:
        mid, skill, pilot = payload['mission_id'], payload['skill'], payload.get('pilot')
        params = payload.get('params') or {}
        if skill not in self.skills:
            return {'ok': False, 'error': f'unsupported skill {skill!r}', 'supported': list(self.skills)}
        if pilot not in self.cfg['pilots']:
            return {'ok': False, 'error': f'unknown pilot {pilot!r}'}
        prior = self.store.mission_get(mid)
        if prior and prior['status'] == 'completed':
            event('idempotency.replay_blocked', agent=self.agent, task_id=mid, a2a_task_id=a2a_task_id,
                  note='mission already completed; returning stored result')
            return {**(prior['result'] or {}), 'replayed_from_memory': True}
        if mid in self.inflight:
            event('idempotency.join_inflight', agent=self.agent, task_id=mid)
            return await asyncio.shield(self.inflight[mid])
        fut = asyncio.get_running_loop().create_future()
        self.inflight[mid] = fut
        try:
            result = await self._run(mid, skill, pilot, params, progress, a2a_task_id, resumed=bool(prior))
        except BaseException as e:
            fut.set_exception(e)
            fut.exception()
            raise
        else:
            fut.set_result(result)
            return result
        finally:
            self.inflight.pop(mid, None)

    async def _run(self, mid, skill, pilot, params, progress, a2a_task_id, resumed):
        lock = self.pilot_locks.setdefault(pilot, asyncio.Lock())
        async with lock:
            rec = self.store.mission_begin(mid, self.agent, skill, pilot, params)
            if resumed:
                event('recovery.resume_mission', agent=self.agent, task_id=mid, a2a_task_id=a2a_task_id,
                      previous_status='running', runs=rec['runs'])
            t0 = time.time()
            try:
                async with GameSession(self.cfg, pilot, self.agent, task_id=mid) as gs:
                    ops = PilotOps(gs, self.store, mid, self.agent, self.cfg, params)
                    result = await getattr(self, 'skill_' + skill)(ops, params, progress)
                    result.setdefault('ok', True)
                    result['mcp_calls'] = gs.calls
                    result['mcp_mutations'] = gs.mutations
                    result['spent_this_task'] = ops.spent_here
                    if ops.notes:
                        result['notes'] = ops.notes
            except SafetyStop as e:
                result = {'ok': False, 'error': 'safety_stop', 'detail': str(e)}
                event('safety.stop', agent=self.agent, task_id=mid, detail=str(e))
            except GameError as e:
                result = {'ok': False, 'error': e.code, 'detail': e.message[:300]}
            result['seconds'] = round(time.time() - t0, 1)
            result['pilot'] = pilot
            result['skill'] = skill
            self.store.mission_finish(mid, 'completed' if result.get('ok') else 'failed', result, result.get('error'))
            return result

    async def skill_observe(self, ops: PilotOps, params: dict, progress) -> dict:
        """Fresh read-only snapshot used by the coordinator for planning (no tick cost)."""
        st = await ops.status()
        system = await ops.system()
        out = {'status': st, 'system': {k: system[k] for k in ('id', 'name', 'empire', 'police_level')},
               'connections': system['connections'][:8],
               'belts': [{'id': b['id'], 'name': b['name'], 'type': b['type']} for b in belts_by_distance(system, st)][:5],
               'bases': [{'id': p['id'], 'name': p['name'], 'base_id': p['base_id']} for p in system['pois'] if p['has_base']][:5],
               'observed_at': time.time()}
        out['active_missions'] = await ops.missions_active()
        if st['docked']:
            out['available_missions'] = (await ops.missions_available())[:12]
            market = await ops.market()
            out['market'] = {k: v for k, v in market.items() if k in st['cargo'] or k.endswith('_ore')}
        else:
            out['available_missions'], out['market'] = [], {}
        if not st['docked'] and any(p['id'] == st['poi'] for p in system['pois']):
            try:
                out['poi'] = await ops.poi()
            except GameError:
                pass
        out['summary'] = (f"{st['system']}/{st['poi']} docked={st['docked']} credits={st['credits']} "
                          f"fuel={st['fuel_frac']:.0%} cargo={st['cargo_used']}/{st['cargo_capacity']}")
        return out


def run_worker(worker_cls, default_port: int):
    import argparse
    from mas.a2a_util import serve
    p = argparse.ArgumentParser()
    p.add_argument('--port', type=int, default=default_port)
    p.add_argument('--config', default=None)
    a = p.parse_args()
    w = worker_cls(config.load(a.config))
    serve(w.agent, w.description, a.port, list(w.skills.items()), w.handle)
