"""Mining / resource-acquisition agent (independent A2A service, default port 9999).

Skills:
  observe     read-only snapshot of a pilot
  mine_cycle  travel to a belt, mine with game-confirmed cargo gains, return to base and dock
"""
from __future__ import annotations
from mas import game_state as gsx
from mas.agents.worker_base import Worker, run_worker
from mas.mcp_game import GameError
from mas.ops import PilotOps, SafetyStop

STOP_CODES = {'cargo_full', 'depleted', 'too_sparse', 'no_resources', 'nothing_to_mine'}


class MiningAgent(Worker):
    agent = 'mining'
    description = 'Selects asteroid belts, mines ore through SpaceMolt MCP and returns cargo to base.'
    skills = {
        'observe': 'Read-only pilot snapshot (status, system, missions, market).',
        'mine_cycle': 'Travel to a belt, mine until cargo full or action cap, return to base and dock. '
                      'params: belt_poi_id?, max_mining_actions, target_item?, return_to_base',
    }

    async def skill_mine_cycle(self, ops: PilotOps, params: dict, progress) -> dict:
        max_actions = int(min(params.get('max_mining_actions', 20), self.cfg['limits']['max_mining_actions_per_cycle']))
        target_item = params.get('target_item')
        st = await ops.status()
        start_cargo = dict(st['cargo'])
        start_ore_stat = st['stats'].get('ore_mined')
        system = await ops.system()
        cp = ops.store.mission_get(ops.mid)['checkpoint'] or {}
        tried = list(cp.get('tried_belts', []))
        belts = gsx.belts_by_distance(system, st, avoid=tried)
        if params.get('belt_poi_id') and params['belt_poi_id'] not in tried:
            belts = [b for b in system['pois'] if b['id'] == params['belt_poi_id']] + \
                    [b for b in belts if b['id'] != params['belt_poi_id']]
        if not belts and not gsx.is_belt(gsx.poi_by_id(system, st['poi']) or {}):
            raise SafetyStop('no asteroid belt visible in this system')
        if st['cargo_free'] <= 0 and st['cargo_capacity']:
            ops.notes.append('cargo already full; skipping mining')
            belts = []
        mined_actions, belt_used, stop_reason = int(cp.get('mined_actions', 0)), cp.get('belt'), None
        for belt in belts[:3]:
            here = gsx.poi_by_id(system, st['poi'])
            if not (here and gsx.is_belt(here) and here['id'] == belt['id']):
                await progress(f'travelling to belt {belt["name"]}')
                st = await ops.travel(belt['id'], f'travel:belt:{belt["id"]}')
            belt_used = belt['id']
            try:
                poi = await ops.poi()
                ops.log('mining.belt_selected', belt=belt['id'], resources=poi['resources'][:6],
                        rationale='nearest belt not yet tried' if not params.get('belt_poi_id') else 'coordinator choice')
            except GameError:
                pass
            stop_reason = None
            while mined_actions < max_actions:
                before = st['cargo_used']
                try:
                    await ops.mutate(f'mine:{mined_actions}', 'mine')
                except GameError as e:
                    stop_reason = e.code
                    ops.log('mining.stopped', belt=belt['id'], code=e.code, message=e.message[:160])
                    break
                mined_actions += 1
                st = await ops.status(full=False)  # get_status only: cargo_used, fuel, hull
                gained = (st['cargo_used'] or 0) - (before or 0)
                ops.log('game.confirmed', what='mined', belt=belt['id'], cargo_gain=gained,
                        cargo=f'{st["cargo_used"]}/{st["cargo_capacity"]}')
                ops.store.mission_checkpoint(ops.mid, {'tried_belts': tried, 'belt': belt['id'], 'mined_actions': mined_actions})
                if mined_actions % 3 == 0:
                    await progress(f'mined {mined_actions} actions, cargo {st["cargo_used"]}/{st["cargo_capacity"]}')
                if st['cargo_capacity'] and st['cargo_free'] <= 0:
                    stop_reason = 'cargo_full'
                    break
                if gained <= 0:
                    stop_reason = 'no_gain'
                    break
                if target_item and params.get('target_quantity') and \
                        st['cargo'].get(target_item, 0) >= int(params['target_quantity']):
                    stop_reason = 'target_reached'
                    break
            if stop_reason in ('cargo_full', 'target_reached') or mined_actions >= max_actions:
                break
            tried.append(belt['id'])
            ops.store.mission_checkpoint(ops.mid, {'tried_belts': tried, 'belt': belt['id'], 'mined_actions': mined_actions})
            ops.log('recovery.alternate_belt', reason=stop_reason, tried=tried)
        if params.get('return_to_base', True):
            await progress('returning to base')
            st = await ops.go_to_base_and_dock(f'after_mining')
        st = await ops.status()
        gained = {k: st['cargo'].get(k, 0) - start_cargo.get(k, 0) for k in st['cargo']}
        gained = {k: v for k, v in gained.items() if v > 0}
        ore_stat = st['stats'].get('ore_mined')
        return {'ok': mined_actions > 0 or bool(gained) or stop_reason == 'cargo_full',
                'error': None if (mined_actions or gained) else (stop_reason or 'nothing_mined'),
                'summary': f'mined {mined_actions} actions at {belt_used}, gained {gained}, stop={stop_reason}',
                'belt': belt_used, 'mining_actions': mined_actions, 'cargo_gained': gained,
                'ore_mined_stat_delta': (ore_stat - start_ore_stat) if None not in (ore_stat, start_ore_stat) else None,
                'stop_reason': stop_reason, 'docked': st['docked'], 'cargo': st['cargo'],
                'cargo_used': st['cargo_used'], 'cargo_capacity': st['cargo_capacity']}


if __name__ == '__main__':
    run_worker(MiningAgent, 9999)
