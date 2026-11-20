"""Deterministic single-agent baseline: one process, one pilot, no A2A, no LLM.

Fixed loop: accept first mining mission -> mine nearest belt -> return -> complete -> sell everything.
It reuses the same MCP client and safety gates as the MAS so the comparison isolates the
architecture (planning, delegation, recovery) rather than the game API code.

Run (prefer a PRACTICE pilot so it does not inflate the competition profiles):
  python -m baseline.single_agent --pilot A --minutes 30
  python -m baseline.single_agent --username MyPracticePilot --minutes 30
"""
from __future__ import annotations
import argparse
import asyncio
import os
import time

os.environ.setdefault('MAS_RUN_ID', 'baseline')

from mas import config, game_state as gsx  # noqa: E402
from mas.log import event  # noqa: E402
from mas.mcp_game import GameError, GameSession  # noqa: E402
from mas.ops import PilotOps, SafetyStop  # noqa: E402
from mas.store import Store  # noqa: E402

AGENT = 'baseline'


async def run(cfg, pilot, minutes, max_loops):
    store = Store(config.LOGS / 'baseline.sqlite3')
    deadline = time.time() + minutes * 60
    loop_no = store.kv_get('loop', 0)
    async with GameSession(cfg, pilot, AGENT) as gs:
        start = gsx.parse_status(await gs.call('get_status'))
        event('baseline.start', agent=AGENT, pilot=pilot, stats=start['stats'], credits=start['credits'])
        while time.time() < deadline and loop_no < max_loops:
            loop_no += 1
            store.kv_set('loop', loop_no)
            mid = f'baseline-{pilot}-{loop_no}'
            ops = PilotOps(gs, store, mid, AGENT, cfg, {})
            try:
                st = await ops.go_to_base_and_dock('start')
                active = await ops.missions_active()
                if not active:
                    for m in await ops.missions_available():
                        if gsx.mission_kind(m) == 'mining':
                            await ops.mutate(f'accept:{m["id"]}', 'accept_mission', id=m['id'])
                            ops.remember_mission(m)
                            break
                system = await ops.system()
                belts = gsx.belts_by_distance(system, st)
                if not belts:
                    raise SafetyStop('no belt')
                await ops.travel(belts[0]['id'], 'travel:belt')
                for i in range(cfg['limits']['max_mining_actions_per_cycle']):
                    before = (await ops.status())['cargo_used']
                    try:
                        await ops.mutate(f'mine:{i}', 'mine')
                    except GameError:
                        break
                    st = await ops.status()
                    ops.log('game.confirmed', what='mined', cargo_gain=st['cargo_used'] - before)
                    if st['cargo_free'] <= 0:
                        break
                await ops.go_to_base_and_dock('end')
                for m in await ops.missions_active():
                    try:
                        await ops.mutate(f'complete:{m["id"]}', 'complete_mission', id=m['id'])
                        ops.remember_mission(m, done=True)
                        ops.log('game.confirmed', what='mission_completed', mission=m['id'], reward=m['reward_credits'])
                    except GameError:
                        pass
                st = await ops.status()
                for item, qty in st['cargo'].items():
                    if qty > 0 and 'fuel' not in item:
                        cr = (await ops.status())['credits']
                        try:
                            await ops.mutate(f'sell:{item}', 'sell', id=item, quantity=qty)
                        except GameError:
                            continue
                        ops.log('game.confirmed', what='exchange_sale', item=item, quantity=qty,
                                credits=(await ops.status())['credits'] - cr)
                st = await ops.status()
                st = await ops.ensure_fuel(st)
                event('baseline.loop', agent=AGENT, task_id=mid, credits=st['credits'], stats=st['stats'])
            except (GameError, SafetyStop) as e:
                event('baseline.error', agent=AGENT, task_id=mid, error=str(e)[:200])
                await asyncio.sleep(15)
        end = gsx.parse_status(await gs.call('get_status'))
        event('baseline.end', agent=AGENT, pilot=pilot, stats=end['stats'], credits=end['credits'],
              mcp_calls=gs.calls, mutations=gs.mutations)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--pilot', default='A', help='pilot key from config (A/B)')
    p.add_argument('--username', default=None, help='use a different (practice) pilot username')
    p.add_argument('--minutes', type=float, default=30)
    p.add_argument('--loops', type=int, default=50)
    p.add_argument('--config', default=None)
    a = p.parse_args()
    cfg = config.load(a.config)
    pilot = a.pilot
    if a.username:
        cfg['pilots']['P'] = a.username
        pilot = 'P'
    asyncio.run(run(cfg, pilot, a.minutes, a.loops))


if __name__ == '__main__':
    main()
