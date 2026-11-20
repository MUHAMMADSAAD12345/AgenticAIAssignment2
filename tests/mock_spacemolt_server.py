"""LOCAL TEST DOUBLE of the SpaceMolt MCP v2 surface. Never used for scoring or the demo.

It mimics the documented tool layout (action-dispatched tools spacemolt, spacemolt_auth,
spacemolt_market) and core object field names so the MAS can be tested offline,
including injected faults (action_pending, session expiry).

Run: python -m tests.mock_spacemolt_server --port 8765   ->  http://127.0.0.1:8765/mcp
"""
from __future__ import annotations
import argparse
import asyncio
import math
import os
import random
from typing import Literal, Optional

from mcp.server.mcpserver import Context, MCPServer

TICK = float(os.environ.get('MOCK_TICK', '0.15'))
FAULTS = os.environ.get('MOCK_FAULTS', '1') == '1'

SYSTEMS = {
    'nebula_prime': {'id': 'nebula_prime', 'name': 'Nebula Prime', 'empire': 'nebula', 'police_level': 3,
                     'pois': [
                         {'id': 'nebula_station', 'name': 'Nebula Central', 'type': 'station', 'position': {'x': 0, 'y': 0},
                          'has_base': True, 'base_id': 'nebula_base', 'base_name': 'Nebula Central'},
                         {'id': 'nebula_belt', 'name': 'Inner Belt', 'type': 'asteroid_belt', 'position': {'x': 30, 'y': 40},
                          'has_base': False},
                         {'id': 'nebula_outer', 'name': 'Outer Belt', 'type': 'asteroid_belt', 'position': {'x': 90, 'y': 0},
                          'has_base': False}],
                     'connections': [{'system_id': 'veil', 'name': 'Veil', 'distance': 120},
                                     {'system_id': 'drift', 'name': 'Drift', 'distance': 300}]},
    'veil': {'id': 'veil', 'name': 'Veil', 'empire': 'nebula', 'police_level': 2,
             'pois': [{'id': 'veil_station', 'name': 'Veil Port', 'type': 'station', 'position': {'x': 0, 'y': 0},
                       'has_base': True, 'base_id': 'veil_base'},
                      {'id': 'veil_belt', 'name': 'Veil Belt', 'type': 'asteroid_belt', 'position': {'x': 20, 'y': 10},
                       'has_base': False}],
             'connections': [{'system_id': 'nebula_prime', 'name': 'Nebula Prime', 'distance': 120}]},
}
JUMP_ENTRY = {'veil': 'veil_station', 'nebula_prime': 'nebula_station'}
DEPOSITS = {'nebula_belt': {'iron_ore': 60, 'copper_ore': 40}, 'nebula_outer': {'iron_ore': 200},
            'veil_belt': {'iron_ore': 300, 'copper_ore': 120}}
if os.environ.get('MOCK_DEPLETED_HOME') == '1':  # scenario: home belts mined out (as on the live server)
    DEPOSITS['nebula_belt'] = {'iron_ore': 0, 'copper_ore': 0}
    DEPOSITS['nebula_outer'] = {'iron_ore': 0}
BIDS = {'iron_ore': 5, 'copper_ore': 8}
MISSIONS = [
    {'mission_id': 'msn_iron_30', 'template_id': 'iron_supply', 'title': 'Iron Supply Run', 'type': 'mining',
     'description': 'Deliver 30 iron ore to Nebula Central.', 'difficulty': 1,
     'rewards': {'credits': 1600}, 'objectives': [{'item_id': 'iron_ore', 'quantity': 30, 'current': 0}]},
    {'mission_id': 'msn_copper_20', 'template_id': 'copper_req', 'title': 'Copper Requisition', 'type': 'mining',
     'description': 'Deliver 20 copper ore.', 'difficulty': 1,
     'rewards': {'credits': 1200}, 'objectives': [{'item_id': 'copper_ore', 'quantity': 20, 'current': 0}]},
    {'mission_id': 'msn_bounty', 'template_id': 'bounty', 'title': 'Pirate Bounty', 'type': 'combat',
     'description': 'Destroy 3 pirates.', 'difficulty': 4, 'rewards': {'credits': 9000}, 'objectives': []},
]

PLAYERS: dict[str, dict] = {}
SESSIONS: dict[int, dict] = {}


def player(username):
    if username not in PLAYERS:
        PLAYERS[username] = {
            'password': os.environ.get('MOCK_PASSWORD', 'test-password'),
            'player': {'id': 'p_' + username, 'username': username, 'empire': 'nebula', 'credits': 250,
                       'current_system': 'nebula_prime', 'current_poi': 'nebula_station', 'home_base': 'nebula_base',
                       'docked_at_base': 'nebula_base',
                       'stats': {'credits_earned': 0, 'credits_spent': 0, 'ore_mined': 0, 'trades_completed': 0,
                                 'exchange_items_sold': 0, 'distance_traveled': 0, 'systems_explored': 1}},
            'ship': {'id': 's_' + username, 'class_id': 'starter', 'hull': 100, 'max_hull': 100, 'fuel': 100,
                     'max_fuel': 100, 'speed': 2, 'cargo_used': 0, 'cargo_capacity': 66, 'cargo': []},
            'active': [], 'explored': {'nebula_prime'}, 'pending_until': 0.0, 'mine_calls': 0,
        }
    return PLAYERS[username]


def err(code, message, **extra):
    return {'error': {'code': code, 'message': message, **extra}}


def sid(ctx: Context):
    req = ctx.request_context.request
    h = getattr(req, 'headers', None)
    return (h.get('mcp-session-id') if h else None) or str(id(ctx.session))


def me(ctx: Context):
    s = SESSIONS.get(sid(ctx))
    return player(s['username']) if s else None


def cargo_get(p, item):
    return next((c['quantity'] for c in p['ship']['cargo'] if c['item_id'] == item), 0)


def cargo_add(p, item, qty):
    for c in p['ship']['cargo']:
        if c['item_id'] == item:
            c['quantity'] += qty
            break
    else:
        p['ship']['cargo'].append({'item_id': item, 'quantity': qty})
    p['ship']['cargo'] = [c for c in p['ship']['cargo'] if c['quantity'] > 0]
    p['ship']['cargo_used'] = sum(c['quantity'] for c in p['ship']['cargo'])


def earn(p, credits):
    p['player']['credits'] += credits
    p['player']['stats']['credits_earned'] += credits


def spend(p, credits):
    p['player']['credits'] -= credits
    p['player']['stats']['credits_spent'] += credits


server = MCPServer('spacemolt-mock')


@server.tool(name='spacemolt_auth', description='MOCK auth: login')
async def spacemolt_auth(ctx: Context, action: Literal['login', 'help'], username: Optional[str] = None,
                         password: Optional[str] = None) -> dict:
    if action == 'help':
        return {'result': 'login(username,password)'}
    p = player(username)
    if password != p['password']:
        return err('invalid_credentials', 'bad username or password')
    SESSIONS[sid(ctx)] = {'username': username, 'calls': 0}
    return {'message': f'Logged in as {username}', 'player': p['player'], 'ship': p['ship'], 'session_id': 'mock-session'}


@server.tool(name='spacemolt_market', description='MOCK market tools')
async def spacemolt_market(ctx: Context, action: Literal['view_market', 'create_sell_order', 'help'],
                           item_id: Optional[str] = None, quantity: Optional[int] = None,
                           price_each: Optional[int] = None) -> dict:
    p = me(ctx)
    if not p:
        return err('session_invalid', 'not logged in')
    if not p['player']['docked_at_base']:
        return err('not_docked', 'you must be docked to use the market')
    if action == 'view_market':
        return {'station': p['player']['docked_at_base'],
                'items': [{'item_id': k, 'name': k.replace('_', ' '), 'best_buy': v, 'best_sell': v * 2} for k, v in BIDS.items()]}
    if action == 'create_sell_order':
        await asyncio.sleep(TICK)
        if cargo_get(p, item_id) < (quantity or 0):
            return err('insufficient_cargo', 'not enough items')
        cargo_add(p, item_id, -quantity)
        spend(p, max(1, int(quantity * price_each * 0.01)))
        return {'message': f'Listed {quantity} {item_id} at {price_each}', 'order_id': 'ord_1'}
    return {'result': 'view_market, create_sell_order'}


@server.tool(name='spacemolt', description='MOCK core gameplay tool (action-dispatched)')
async def spacemolt(ctx: Context,
                    action: Literal['get_status', 'get_system', 'get_poi', 'get_cargo', 'get_missions', 'get_active_missions',
                                    'travel', 'jump', 'dock', 'undock', 'mine', 'sell', 'accept_mission',
                                    'complete_mission', 'refuel', 'repair', 'help'],
                    id: Optional[str] = None, quantity: Optional[int] = None, template_id: Optional[str] = None) -> dict:
    p = me(ctx)
    if not p:
        return err('session_invalid', 'Session invalid: please login again')
    s = SESSIONS[sid(ctx)]
    s['calls'] += 1
    if FAULTS and s['calls'] == 25 and not s.get('expired_once'):
        s['expired_once'] = True
        del SESSIONS[sid(ctx)]
        return err('session_invalid', 'Session expired, login again')
    pl, sh = p['player'], p['ship']
    system = SYSTEMS[pl['current_system']]
    if action == 'get_status':
        return {'player': pl, 'ship': sh}
    if action == 'get_system':
        return {'system': system}
    if action == 'get_poi':
        poi = next(x for x in system['pois'] if x['id'] == pl['current_poi'])
        res = [{'resource_id': k, 'richness': 40, 'remaining': v} for k, v in DEPOSITS.get(poi['id'], {}).items()]
        return {'poi': {**poi, 'system_id': system['id'], 'resources': res}}
    if action == 'get_cargo':
        return {'cargo': sh['cargo'], 'cargo_used': sh['cargo_used'], 'cargo_capacity': sh['cargo_capacity']}
    if action == 'get_missions':
        if not pl['docked_at_base']:
            return err('not_docked', 'dock to view missions')
        taken = {m['mission_id'] for m in p['active']}
        return {'missions': [m for m in MISSIONS if m['mission_id'] not in taken]}
    if action == 'get_active_missions':
        for m in p['active']:
            for o in m['objectives']:
                o['current'] = 0
            m['percent_complete'] = 0
        return {'missions': p['active']}
    if action == 'help':
        return {'result': 'mock help'}
    # mutations: one per tick
    if FAULTS and action == 'mine' and p['mine_calls'] == 0:
        p['mine_calls'] += 1
        return err('action_pending', 'An action is already queued this tick', retry_after=1)
    await asyncio.sleep(TICK)
    if action == 'undock':
        pl['docked_at_base'] = None
        return {'message': 'Undocked'}
    if action == 'dock':
        poi = next(x for x in system['pois'] if x['id'] == pl['current_poi'])
        if not poi.get('has_base'):
            return err('no_base', 'No base at this POI')
        pl['docked_at_base'] = poi['base_id']
        return {'message': f'Docked at {poi["base_id"]}'}
    if action == 'travel':
        dest = next((x for x in system['pois'] if x['id'] == id), None)
        here = next(x for x in system['pois'] if x['id'] == pl['current_poi'])
        if not dest:
            return err('not_found', f'POI {id} not in system')
        if sh['fuel'] < 2:
            return err('insufficient_fuel', 'Not enough fuel')
        d = math.hypot(dest['position']['x'] - here['position']['x'], dest['position']['y'] - here['position']['y'])
        auto = bool(pl['docked_at_base'])
        pl['docked_at_base'] = None
        await asyncio.sleep(TICK * 2)
        pl['current_poi'] = id
        sh['fuel'] -= 3
        pl['stats']['distance_traveled'] += round(d)
        return {'destination': id, 'arrival_tick': 1, 'auto_undocked': auto, 'message': f'Arrived at {dest["name"]}'}
    if action == 'jump':
        conn = next((c for c in system['connections'] if c['system_id'] == id), None)
        if not conn:
            return err('not_found', f'No jump route to {id}')
        if sh['fuel'] < 10:
            return err('insufficient_fuel', 'Not enough fuel')
        pl['docked_at_base'] = None
        await asyncio.sleep(TICK * 3)
        pl['current_system'], pl['current_poi'] = id, JUMP_ENTRY[id]
        sh['fuel'] -= 10
        pl['stats']['distance_traveled'] += conn['distance']
        if id not in p['explored']:
            p['explored'].add(id)
            pl['stats']['systems_explored'] += 1
        return {'message': f'Jumped to {id}'}
    if action == 'mine':
        p['mine_calls'] += 1
        dep = DEPOSITS.get(pl['current_poi'])
        if dep is None:
            return err('not_minable', 'Nothing to mine here')
        if sh['cargo_used'] >= sh['cargo_capacity']:
            return err('cargo_full', 'Cargo hold is full')
        avail = [k for k, v in dep.items() if v > 0]
        if not avail:
            return err('depleted', 'the beam disperses what little remains')
        item = random.choice(avail)
        qty = min(6, dep[item], sh['cargo_capacity'] - sh['cargo_used'])
        dep[item] -= qty
        cargo_add(p, item, qty)
        pl['stats']['ore_mined'] += qty
        return {'message': f'Mined {qty} {item}', 'item_id': item, 'quantity': qty}
    if not pl['docked_at_base']:
        return err('not_docked', 'You must be docked')
    if action == 'sell':
        have = cargo_get(p, id)
        q = min(quantity or 0, have)
        if q <= 0:
            return err('insufficient_cargo', f'No {id} in cargo')
        if id not in BIDS:
            return err('no_buyers', 'No buy orders for this item')
        cargo_add(p, id, -q)
        earn(p, q * BIDS[id])
        pl['stats']['exchange_items_sold'] += q
        return {'message': f'Sold {q} {id} for {q * BIDS[id]} credits', 'total': q * BIDS[id]}
    if action == 'accept_mission':
        m = next((m for m in MISSIONS if m['mission_id'] == id), None)
        if not m:
            return err('not_found', 'mission not found')
        if any(a['mission_id'] == id for a in p['active']):
            return err('already_accepted', 'mission already active')
        p['active'].append({**m, 'objectives': [dict(o) for o in m['objectives']], 'percent_complete': 0,
                            'issuing_base': 'nebula_base', 'expires_in_ticks': 5000, 'accepted_at': 1, 'rewards': m['rewards']})
        return {'mission_id': id, 'title': m['title'], 'type': m['type'], 'expires_at': 99999, 'message': 'Mission accepted'}
    if action == 'complete_mission':
        m = next((m for m in p['active'] if m['mission_id'] == id), None)
        if not m:
            return err('not_found', 'mission not active')
        for o in m['objectives']:
            if cargo_get(p, o['item_id']) < o['quantity']:
                return err('objectives_incomplete', f'need {o["quantity"]} {o["item_id"]}')
        for o in m['objectives']:
            cargo_add(p, o['item_id'], -o['quantity'])
        p['active'].remove(m)
        earn(p, m['rewards']['credits'])
        return {'message': f'Mission complete: +{m["rewards"]["credits"]} credits'}
    if action == 'refuel':
        cost = int((sh['max_fuel'] - sh['fuel']) * 0.5)
        sh['fuel'] = sh['max_fuel']
        spend(p, cost)
        return {'message': f'Refuelled for {cost}'}
    if action == 'repair':
        sh['hull'] = sh['max_hull']
        return {'message': 'Repaired'}
    return err('unknown_action', action)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8765)
    a = ap.parse_args()
    server.run(transport='streamable-http', host='127.0.0.1', port=a.port, streamable_http_path='/mcp')
