"""Trading / logistics agent (independent A2A service, default port 9998).

Skills:
  observe            read-only snapshot of a pilot
  accept_mission     accept a mission chosen by the coordinator
  sell_and_complete  dock, complete satisfied missions, sell surplus ore on the station exchange
  list_item          post a small sell order (market-participation missions)
  explore_adjacent   jump to the nearest safe neighbouring system, record its market, optionally return
"""
from __future__ import annotations
from mas import game_state as gsx
from mas.agents.worker_base import Worker, run_worker
from mas.mcp_game import GameError
from mas.ops import PilotOps, SafetyStop


class TradingAgent(Worker):
    agent = 'trading'
    description = 'Handles missions, station-exchange sales, listings and inter-system logistics via SpaceMolt MCP.'
    skills = {
        'observe': 'Read-only pilot snapshot (status, system, missions, market).',
        'accept_mission': 'Accept a mission at the docked base. params: mission_id',
        'sell_and_complete': 'Dock, complete satisfied missions, sell surplus cargo. params: keep_items?, min_price?, sell?',
        'list_item': 'Create a sell order. params: item_id, quantity, price_each',
        'explore_adjacent': 'Jump to nearest neighbouring system and back. params: max_distance, return_home',
    }

    async def skill_accept_mission(self, ops: PilotOps, params: dict, progress) -> dict:
        mission_id = params.get('mission_id')
        if not mission_id:
            return {'ok': False, 'error': 'invalid_params', 'detail': 'mission_id required'}
        st = await ops.go_to_base_and_dock('accept')
        active = await ops.missions_active()
        if any(m['id'] == mission_id for m in active):
            return {'ok': True, 'summary': f'mission {mission_id} already active', 'already_active': True}
        if len(active) >= self.cfg['limits']['max_active_missions_per_pilot']:
            return {'ok': False, 'error': 'too_many_active_missions', 'active': [m['id'] for m in active]}
        available = await ops.missions_available()
        m = next((x for x in available if x['id'] == mission_id or x.get('template_id') == mission_id), None)
        if not m:
            return {'ok': False, 'error': 'stale_observation', 'detail': f'mission {mission_id} no longer offered',
                    'available': [x['id'] for x in available][:10]}
        if gsx.mission_kind(m) == 'unsafe':
            return {'ok': False, 'error': 'safety_policy', 'detail': 'combat-type mission rejected'}

        async def rec(pre):
            return any(x['id'] == mission_id for x in await ops.missions_active())
        # Live MCP v2 schema only accepts `id` for accept_mission (no template_id parameter).
        args = {'id': m['id'] or m['template_id']}
        resp = await ops.mutate(f'accept:{mission_id}', 'accept_mission', reconcile=rec, **args)
        ops.remember_mission(m)
        ops.log('game.confirmed', what='mission_accepted', mission=mission_id, title=m['title'],
                reward=m['reward_credits'], server=(resp.get('message') or resp.get('_text') or '')[:160])
        return {'ok': True, 'summary': f'accepted {m["title"]} ({m["reward_credits"]} cr)', 'mission': m}

    async def skill_sell_and_complete(self, ops: PilotOps, params: dict, progress) -> dict:
        st = await ops.go_to_base_and_dock('sell')
        credits0 = st['credits']
        completed, sold, skipped = [], [], []
        for m in await ops.missions_active():
            need = [o for o in m['objectives'] if o.get('item_id') and o.get('quantity')]
            have_all = all(st['cargo'].get(o['item_id'], 0) + (o.get('current') or 0) >= o['quantity'] for o in need)
            if need and not have_all and (m.get('percent_complete') or 0) < 100:
                continue

            async def rec(pre, mid=m['id']):
                return not any(x['id'] == mid for x in await ops.missions_active())
            try:
                resp = await ops.mutate(f'complete:{m["id"]}', 'complete_mission', reconcile=rec, id=m['id'])
                completed.append({'id': m['id'], 'title': m['title'], 'reward': m['reward_credits'],
                                  'server': (resp.get('message') or resp.get('_text') or '')[:160]})
                ops.log('game.confirmed', what='mission_completed', mission=m['id'], reward=m['reward_credits'])
                ops.remember_mission(m, done=True)
                st = await ops.status()
            except GameError as e:
                skipped.append({'mission': m['id'], 'code': e.code, 'message': e.message[:120]})
        reserve: dict[str, int] = {}
        for m in await ops.missions_active():
            for o in m['objectives']:
                if o.get('item_id') and o.get('quantity'):
                    reserve[o['item_id']] = reserve.get(o['item_id'], 0) + int(o['quantity'] - (o.get('current') or 0))
        for k in params.get('keep_items') or []:
            reserve[k] = 10 ** 9
        if params.get('sell', True):
            market = await ops.market()
            min_price = float(params.get('min_price', self.cfg['limits']['min_sell_price']))
            for item, qty in sorted(st['cargo'].items()):
                surplus = qty - reserve.get(item, 0)
                if surplus <= 0 or 'fuel' in item:
                    continue
                bid = (market.get(item) or {}).get('bid')
                if market and bid is not None and bid < min_price:
                    skipped.append({'item': item, 'reason': f'best bid {bid} < floor {min_price}'})
                    continue
                if market and item not in market and bid is None:
                    skipped.append({'item': item, 'reason': 'no buyers listed at this station'})
                    continue
                before_qty, before_cr = qty, st['credits']

                async def rec(pre, item=item):
                    now = await ops.status()
                    return now['cargo'].get(item, 0) < (pre.get('cargo') or {}).get(item, 0)
                try:
                    resp = await ops.mutate(f'sell:{item}:{qty}', 'sell', reconcile=rec, id=item, quantity=int(surplus))
                except GameError as e:
                    skipped.append({'item': item, 'code': e.code, 'message': e.message[:120]})
                    continue
                st = await ops.status()
                sold_qty = before_qty - st['cargo'].get(item, 0)
                earned = st['credits'] - before_cr
                sold.append({'item': item, 'quantity': sold_qty, 'credits': earned, 'bid_seen': bid,
                             'server': (resp.get('message') or resp.get('_text') or '')[:160]})
                ops.log('game.confirmed', what='exchange_sale', item=item, quantity=sold_qty, credits=earned)
        st = await ops.status()
        if st['fuel_frac'] < self.cfg['limits']['min_fuel_fraction']:
            try:
                st = await ops.ensure_fuel(st)
            except SafetyStop as e:
                ops.notes.append(str(e))
        return {'ok': True, 'summary': f'completed {len(completed)} missions, {len(sold)} sales, '
                                       f'credits {credits0}->{st["credits"]}',
                'completed': completed, 'sold': sold, 'skipped': skipped, 'reserved': reserve,
                'credits_before': credits0, 'credits_after': st['credits'], 'cargo': st['cargo']}

    async def skill_list_item(self, ops: PilotOps, params: dict, progress) -> dict:
        item, qty, price = params.get('item_id'), int(params.get('quantity', 1)), params.get('price_each')
        st = await ops.go_to_base_and_dock('list')
        if st['cargo'].get(item, 0) < qty:
            return {'ok': False, 'error': 'insufficient_cargo', 'detail': f'have {st["cargo"].get(item, 0)} {item}'}
        if price is None:
            m = (await ops.market()).get(item) or {}
            price = int(max((m.get('ask') or 0), (m.get('bid') or 0) * 2, 10))
        resp = await ops.mutate(f'list:{item}:{qty}:{price}', 'create_sell_order',
                                item_id=item, quantity=qty, price_each=int(price))
        ops.log('game.confirmed', what='sell_order_listed', item=item, quantity=qty, price_each=price)
        return {'ok': True, 'summary': f'listed {qty} {item} @ {price}', 'server': (resp.get('_text') or '')[:200]}

    async def skill_explore_adjacent(self, ops: PilotOps, params: dict, progress) -> dict:
        st = await ops.status()
        home = st['system']
        cp = ops.store.mission_get(ops.mid)['checkpoint'] or {}
        if not cp.get('origin'):
            cp['origin'] = home
            ops.store.mission_checkpoint(ops.mid, cp)
        origin = cp['origin']
        system = await ops.system()
        max_d = float(params.get('max_distance', 400))
        target = cp.get('target')
        if not target and st['system'] == origin:
            options = sorted([c for c in system['connections'] if c['system_id']], key=lambda c: c['distance'] or 1e9)
            options = [c for c in options if (c['distance'] or 0) <= max_d]
            if not options:
                return {'ok': False, 'error': 'no_safe_neighbour', 'detail': f'no connection within {max_d}',
                        'connections': system['connections']}
            target = options[0]['system_id']
            cp['target'] = target
            ops.store.mission_checkpoint(ops.mid, cp)
            ops.log('explore.selected', target=target, distance=options[0]['distance'],
                    rationale='shortest jump keeps distance budget low')
        if st['system'] != target and st['system'] == origin:
            st = await ops.ensure_fuel(st, max(self.cfg['limits']['min_fuel_fraction'], 0.5))
            await progress(f'jumping to {target}')

            async def rec(pre):
                return (await ops.status())['system'] == target
            await ops.mutate(f'jump:out:{target}', 'jump', reconcile=rec, id=target)
            st = await ops.status()
            ops.log('game.confirmed', what='jumped', system=st['system'])
        visited = await ops.system()
        report = {'system': visited['id'], 'name': visited['name'], 'police_level': visited['police_level'],
                  'bases': [p['name'] for p in visited['pois'] if p['has_base']], 'belts': [p['name'] for p in visited['pois'] if gsx.is_belt(p)]}
        unsafe = self.cfg['safety'].get('explore_requires_police') and visited['police_level'] == 0
        if params.get('return_home', True) or unsafe:
            if st['system'] != origin:
                st = await ops.ensure_fuel(st, 0.0)

                async def rec_back(pre):
                    return (await ops.status())['system'] == origin
                await ops.mutate(f'jump:back:{origin}', 'jump', reconcile=rec_back, id=origin)
                st = await ops.status()
            st = await ops.go_to_base_and_dock('after_explore')
        return {'ok': True, 'summary': f'explored {report["system"]}, now at {st["system"]}', 'explored': report,
                'unsafe_police_level': unsafe, 'docked': st['docked'], 'stats': st['stats']}


if __name__ == '__main__':
    run_worker(TradingAgent, 9998)
