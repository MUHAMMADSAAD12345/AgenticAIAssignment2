"""READ-ONLY live check against the real SpaceMolt MCP server (no mutations, no ticks).

For each pilot: MCP initialize -> list tools (schemas saved to logs/mcp_tools.json) ->
authenticated login -> get_status / get_system / get_poi / missions / market, then reports whether
every action the MAS needs is present and whether the responses parsed correctly.

Run: python -m scripts.live_check            (both pilots)
     python -m scripts.live_check --pilot A
"""
from __future__ import annotations
import argparse
import asyncio
import json

from mas import config, game_state as gsx
from mas.mcp_game import GameError, GameSession

NEEDED = ['login', 'get_status', 'get_system', 'get_poi', 'travel', 'dock', 'mine', 'sell', 'view_market',
          'get_missions', 'accept_mission', 'complete_mission', 'get_active_missions', 'refuel', 'jump']


async def check(cfg, pilot):
    print(f'\n=== Pilot {pilot}: {cfg["pilots"][pilot]} ===')
    async with GameSession(cfg, pilot, 'live_check') as gs:
        (config.LOGS / 'mcp_tools.json').write_text(json.dumps(gs.tools, indent=1), encoding='utf-8')
        print(f'MCP tools discovered: {len(gs.tools)} -> saved to logs/mcp_tools.json')
        missing = [a for a in NEEDED if a not in gs.action_tool]
        if gs.action_tool:
            print('Action routing:', {a: gs.action_tool.get(a, 'MISSING') for a in NEEDED})
            if missing:
                print('WARNING missing actions (MAS falls back where it can):', missing)
        else:
            print('NOTE: schemas do not enumerate actions; default tool mapping from docs will be used')
        raw = await gs.call('get_status')
        st = gsx.parse_status(raw)
        print('Parsed status:', {k: st[k] for k in ('username', 'empire', 'credits', 'system', 'poi', 'docked',
                                                     'fuel', 'max_fuel', 'cargo_used', 'cargo_capacity', 'cargo')})
        print('Parsed stats :', st['stats'])
        if st['username'] is None or not st['max_fuel']:
            print('WARNING: status fields not found. Top-level keys:', list(raw)[:20])
        if all(v is None for v in st['stats'].values()):
            print('WARNING: no stats parsed. player keys:', list((raw.get('player') or {}).keys())[:30])
        if st['empire'] and st['empire'] != 'nebula':
            print('WARNING: pilot empire is', st['empire'], '(assignment requires nebula)')
        system = gsx.parse_system(await gs.call('get_system'))
        print(f'System {system["id"]} police={system["police_level"]} POIs={len(system["pois"])} '
              f'belts={[p["id"] for p in gsx.belts_by_distance(system, st)]} '
              f'bases={[p["id"] for p in system["pois"] if p["has_base"]]}')
        print('Connections:', [(c['system_id'], c['distance']) for c in system['connections']][:6])
        for action in ('get_missions', 'get_active_missions', 'view_market'):
            try:
                data = await gs.call(action)
                if action == 'view_market':
                    mk = gsx.parse_market(data)
                    print(f'{action}: {len(mk)} items parsed; ore bids:',
                          {k: v['bid'] for k, v in mk.items() if 'ore' in k})
                else:
                    ms = gsx.parse_missions(data)
                    print(f'{action}: {len(ms)} parsed')
                    for m in ms[:6]:
                        print(f'   [{gsx.mission_kind(m)}] {m["id"]} {m["title"]} reward={m["reward_credits"]} '
                              f'objectives={m["objectives"][:2]}')
            except GameError as e:
                print(f'{action}: {e.code} ({e.message[:100]})')
    print('Live check finished: no state-changing action was sent.')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--pilot', default=None)
    p.add_argument('--config', default=None)
    a = p.parse_args()
    cfg = config.load(a.config)
    for pilot in ([a.pilot] if a.pilot else list(cfg['pilots'])):
        asyncio.run(check(cfg, pilot))


if __name__ == '__main__':
    main()
