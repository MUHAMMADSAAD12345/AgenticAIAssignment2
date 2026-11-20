"""Plan schema, validation and the deterministic fallback policy used by the coordinator."""
from __future__ import annotations
import re
from mas import game_state as gsx

SKILL_AGENT = {
    'mine_cycle': 'mining',
    'accept_mission': 'trading',
    'sell_and_complete': 'trading',
    'list_item': 'trading',
    'explore_adjacent': 'trading',
    'relocate': 'trading',
    'wait': None,
}
STAT_FIELDS = ('credits_earned', 'credits_spent', 'ore_mined', 'trades_completed',
               'exchange_items_sold', 'distance_traveled', 'systems_explored')


def combined_progress(obs: dict[str, dict], cfg: dict) -> dict:
    tot, unknown = {}, []
    for f in STAT_FIELDS:
        vals = [((o or {}).get('status') or {}).get('stats', {}).get(f) for o in obs.values()]
        if all(v is None for v in vals):
            unknown.append(f)
        tot[f] = sum(v or 0 for v in vals)
    t = cfg['targets']
    sold = tot['trades_completed'] + tot['exchange_items_sold']
    met = {
        'credits_earned': tot['credits_earned'] >= t['credits_earned'],
        'ore_mined': tot['ore_mined'] >= t['ore_mined'],
        'trade': sold >= t['trades'],
        'systems_explored': tot['systems_explored'] >= t['systems_explored'],
        'spend_ok': tot['credits_spent'] <= t['max_credits_spent'],
        'distance_ok': tot['distance_traveled'] <= t['max_distance'],
    }
    return {'totals': tot, 'unknown_stats': unknown, 'met': met,
            'all_targets_met': all(met.values()) and not {'credits_earned', 'ore_mined'} & set(unknown)}


def mission_candidates(o: dict, cfg: dict) -> list[dict]:
    st = o.get('status') or {}
    cap = st.get('cargo_capacity') or 60
    out = []
    for m in o.get('available_missions') or []:
        kind = gsx.mission_kind(m)
        if kind not in ('mining', 'market') or not gsx.mission_doable_here(m, kind):
            continue
        if kind == 'mining' and 'belts_ok' in o and not o['belts_ok']:
            continue  # no usable belt in this system (e.g. Market Prime): a mining mission cannot progress here
        qty = sum(ob.get('quantity') or 0 for ob in m['objectives'])
        if kind == 'mining' and qty > cap * 2:
            continue
        score = (m.get('reward_credits') or 0) / max(qty, 1)
        out.append({'id': m['id'], 'title': m['title'], 'kind': kind, 'reward': m.get('reward_credits'),
                    'items': [(ob.get('item_id'), ob.get('quantity')) for ob in m['objectives']], 'score': round(score, 1)})
    return sorted(out, key=lambda x: -(x['reward'] or 0))


def allowances(progress: dict, cfg: dict) -> dict:
    tot, lim = progress['totals'], cfg['limits']
    return {'spend_allowance': max(0, lim['spend_soft_cap'] - tot['credits_spent']),
            'distance_left': max(0, lim['distance_soft_cap'] - tot['distance_traveled'])}


def relocation_target(pilot: str, obs: dict, o: dict, al: dict, scouting: set) -> tuple[str | None, str]:
    """Where should a pilot with no usable belt go? Follow a proven system, else wait for a scout, else scout."""
    here = o['status'].get('system')
    yields = o.get('system_yield') or {}
    exhausted = set(o.get('exhausted_systems') or [])
    conns = {c['system_id']: c.get('distance') for c in o.get('connections') or [] if c.get('system_id')}
    for other, oo in obs.items():
        there = ((oo or {}).get('status') or {}).get('system')
        if other != pilot and there and there != here and yields.get(there, 0) > 0                 and there in conns and (conns[there] or 0) <= al['distance_left']:
            return there, f'follow {other}: {there} yielded {yields[there]} ore'
    for other, oo in obs.items():
        there = ((oo or {}).get('status') or {}).get('system')
        if other != pilot and there and there != here and there not in exhausted and not yields.get(there):
            return None, f'wait: {other} is scouting {there}'
    if scouting:
        return None, f'wait: scout already sent to {sorted(scouting)}'
    options = sorted((d, s) for s, d in conns.items() if s not in exhausted and d and d <= al['distance_left'])
    if options:
        return options[0][1], f'scout nearest non-exhausted neighbour {options[0][1]} ({options[0][0]} away)'
    return None, 'no affordable neighbour left within the distance budget'


def mission_reserve(o: dict) -> dict[str, int]:
    """Cargo that active missions still need. Mining missions only reserve ore where they can progress
    (a usable belt here); otherwise the ore is ordinary surplus (e.g. a pilot parked at belt-less Market Prime)."""
    can_mine = 'belts_ok' not in o or bool(o['belts_ok'])
    reserve: dict[str, int] = {}
    for m in o.get('active_missions') or []:
        if gsx.mission_kind(m) == 'mining' and not can_mine:
            continue
        for ob in m.get('objectives') or []:
            if ob.get('item_id') and ob.get('quantity'):
                left = int(ob['quantity'] - (ob.get('current') or 0))
                if left > 0:
                    reserve[ob['item_id']] = reserve.get(ob['item_id'], 0) + left
    return reserve


def surplus(o: dict) -> dict[str, int]:
    st, reserve = o['status'], mission_reserve(o)
    return {k: q - reserve.get(k, 0) for k, q in (st.get('cargo') or {}).items()
            if not k.startswith('fuel') and q - reserve.get(k, 0) > 0}


def sale_value(o: dict) -> float:
    """Credits the surplus would fetch right now from existing buy orders at this station."""
    market, total = o.get('market') or {}, 0.0
    for item, qty in surplus(o).items():
        m = market.get(item) or {}
        if m.get('bid'):
            total += min(qty, m.get('bid_qty') or qty) * m['bid']
    return total


def market_mission_need(o: dict) -> int:
    """Units still to sell for active 'sell N units on the exchange' missions."""
    need = 0
    for m in o.get('active_missions') or []:
        if gsx.mission_kind(m) == 'market':
            for ob in m.get('objectives') or []:
                if re.search(r'\bsell\b', str(ob.get('description') or ''), re.I):
                    need += max(0, int((ob.get('quantity') or 0) - (ob.get('current') or 0)))
    return need


def listing_choice(o: dict) -> dict | None:
    """Surplus item with no buyers here, listed just under the lowest ask (largest expected value first)."""
    market, need = o.get('market') or {}, market_mission_need(o)
    if need <= 0:
        return None
    best = None
    for item, qty in surplus(o).items():
        m = market.get(item) or {}
        if m.get('bid'):
            continue  # sells instantly via sell_and_complete instead
        price = int(m['ask']) - 1 if m.get('ask') and m['ask'] > 1 else None
        value = (price or 1) * min(qty, need)
        if best is None or value > best[0]:
            best = (value, {'item_id': item, 'quantity': int(min(qty, need)), 'price_each': price})
    return best[1] if best else None


def fallback_plan(obs: dict[str, dict], progress: dict, cfg: dict, history: list[dict]) -> dict:
    """Rule-based policy: used when the LLM is unavailable/over budget or returns an invalid plan."""
    al = allowances(progress, cfg)
    explored_needed = not progress['met']['systems_explored']
    explore_done = any(h['skill'] == 'explore_adjacent' and h['status'] == 'completed' for h in history)
    assignments, reasons, taken, scouting = [], [], set(), set()
    for pilot, o in obs.items():
        if not o:
            continue
        st = o['status']
        recent_fail = [h for h in history[:6] if h['pilot'] == pilot and h['status'] == 'failed']
        active = o.get('active_missions') or []
        active_ids = {m.get('id') for m in active}
        # spread the pilots over different missions (different ores) and never re-accept an active one
        cands = [c for c in mission_candidates(o, cfg) if c['id'] not in active_ids]
        cands = [c for c in cands if c['id'] not in taken] or cands
        cargo_frac = (st['cargo_used'] or 0) / (st['cargo_capacity'] or 1)
        if al['distance_left'] <= 0:
            if st['docked'] and st['cargo']:
                assignments.append({'pilot': pilot, 'skill': 'sell_and_complete', 'params': {}})
                reasons.append(f'{pilot}: distance budget spent, only selling docked cargo')
            continue
        value = sale_value(o) if st['docked'] else 0
        listing = listing_choice(o) if st['docked'] else None
        listed_recently = any(h['skill'] == 'list_item' and h['pilot'] == pilot and h['status'] == 'completed'
                              for h in history[:12])
        if st['docked'] and value >= 50:
            # buyers here would pay real credits for surplus cargo right now (e.g. a station order for crystals)
            assignments.append({'pilot': pilot, 'skill': 'sell_and_complete',
                                'params': {'reserve': mission_reserve(o)}})
            reasons.append(f'{pilot}: buy orders here worth ~{int(value)} cr for surplus cargo -> sell now')
        elif listing and not listed_recently:
            assignments.append({'pilot': pilot, 'skill': 'list_item', 'params': listing})
            reasons.append(f'{pilot}: market mission needs sales; no buyers for {listing["item_id"]} here -> '
                           f'list {listing["quantity"]} @ {listing["price_each"] or "auto"} (just under lowest ask)')
        elif st['docked'] and (st['cargo'] and any(not k.startswith('fuel') for k in st['cargo'])) and \
                (cargo_frac >= 0.5 or not active or any(_satisfied(m, st) for m in active)):
            assignments.append({'pilot': pilot, 'skill': 'sell_and_complete', 'params': {'reserve': mission_reserve(o)}})
            reasons.append(f'{pilot}: docked with cargo -> complete missions / sell')
        elif 'belts_ok' in o and not o['belts_ok'] and any(not k.startswith('fuel') for k in st['cargo']) and \
                not any(h['skill'] == 'sell_and_complete' and h['pilot'] == pilot for h in history[:2]):
            # cannot mine here but holding cargo: dock at the local station and sell the surplus (keeps mission ore)
            assignments.append({'pilot': pilot, 'skill': 'sell_and_complete', 'params': {}})
            reasons.append(f'{pilot}: no usable belt in {st.get("system")} -> dock and sell surplus cargo here')
        elif st['docked'] and 'belts_ok' in o and not o['belts_ok']:
            # every belt here is known depleted: mining is impossible, so move the operating base
            target, why = relocation_target(pilot, obs, o, al, scouting)
            if target:
                scouting.add(target)
                assignments.append({'pilot': pilot, 'skill': 'relocate', 'params': {'target_system': target}})
            reasons.append(f'{pilot}: belts depleted in {st.get("system")} -> {why}')
        elif st['docked'] and cands and len(active) < cfg['limits']['max_active_missions_per_pilot'] and \
                not any(h['skill'] == 'accept_mission' and h['pilot'] == pilot for h in recent_fail[:2]):
            assignments.append({'pilot': pilot, 'skill': 'accept_mission', 'params': {'mission_id': cands[0]['id']}})
            taken.add(cands[0]['id'])
            reasons.append(f'{pilot}: accept best mining/market mission {cands[0]["title"]}')
        elif pilot == 'B' and explored_needed and not explore_done and st['docked'] and al['distance_left'] > 300:
            assignments.append({'pilot': pilot, 'skill': 'explore_adjacent',
                                'params': {'max_distance': min(400, al['distance_left'] / 2), 'return_home': True}})
            reasons.append(f'{pilot}: systems_explored target not met -> one short jump')
        elif not st['docked'] and cargo_frac >= 0.9:
            assignments.append({'pilot': pilot, 'skill': 'sell_and_complete', 'params': {}})
            reasons.append(f'{pilot}: cargo full in space -> return and sell')
        else:
            target = next((ob.get('item_id') for m in active for ob in m['objectives'] if ob.get('item_id')), None)
            assignments.append({'pilot': pilot, 'skill': 'mine_cycle',
                                'params': {'max_mining_actions': cfg['limits']['max_mining_actions_per_cycle'],
                                           'target_item': target, 'return_to_base': True,
                                           'avoid_belts': o.get('depleted_belts_here') or []}})
            reasons.append(f'{pilot}: mine (ore {progress["totals"]["ore_mined"]}/{cfg["targets"]["ore_mined"]})')
    return {'rationale': '; '.join(reasons), 'assignments': assignments}


def _satisfied(m, st):
    need = [o for o in m['objectives'] if o.get('item_id') and o.get('quantity')]
    return bool(need) and all(st['cargo'].get(o['item_id'], 0) >= o['quantity'] for o in need)


def validate_plan(plan: dict, obs: dict, cfg: dict, progress: dict) -> tuple[list[dict], list[str]]:
    """Return (clean assignments, problems). Enforces skill whitelist, one task per pilot, safety caps."""
    problems, out, seen = [], [], set()
    al = allowances(progress, cfg)
    if not isinstance(plan, dict) or not isinstance(plan.get('assignments'), list):
        return [], ['plan must be an object with an assignments list']
    for a in plan['assignments'][:4]:
        if not isinstance(a, dict):
            problems.append('assignment not an object')
            continue
        pilot, skill, params = a.get('pilot'), a.get('skill'), a.get('params') or {}
        if pilot not in obs or not obs.get(pilot):
            problems.append(f'unknown/unobserved pilot {pilot!r}')
            continue
        if pilot in seen:
            problems.append(f'duplicate assignment for pilot {pilot}')
            continue
        if skill not in SKILL_AGENT:
            problems.append(f'skill {skill!r} not allowed')
            continue
        if not isinstance(params, dict):
            problems.append('params must be an object')
            continue
        st = obs[pilot]['status']
        if skill == 'accept_mission':
            if len(obs[pilot].get('active_missions') or []) >= cfg['limits']['max_active_missions_per_pilot']:
                problems.append(f'{pilot} already has the maximum number of active missions')
                continue
            ids = {m['id'] for m in obs[pilot].get('available_missions') or []}
            if params.get('mission_id') not in ids:
                problems.append(f'mission {params.get("mission_id")!r} not in fresh observation for {pilot}')
                continue
            m = next(m for m in obs[pilot]['available_missions'] if m['id'] == params['mission_id'])
            if gsx.mission_kind(m) == 'unsafe':
                problems.append('unsafe mission rejected')
                continue
            if not gsx.mission_doable_here(m):
                problems.append(f'mission {m["id"]!r} rejected: needs travel or objectives the MAS cannot do here')
                continue
        if skill == 'explore_adjacent':
            if progress['met']['systems_explored']:
                problems.append('explore rejected: systems_explored target already met')
                continue
            if al['distance_left'] < 200:
                problems.append('explore rejected: distance budget')
                continue
            params['max_distance'] = min(float(params.get('max_distance', 400)), al['distance_left'] / 2)
        if skill == 'relocate':
            o = obs[pilot]
            conns = {c['system_id']: c.get('distance') for c in o.get('connections') or []}
            target = params.get('target_system')
            if target not in conns:
                problems.append(f'relocate rejected: {target!r} is not adjacent to {st.get("system")}')
                continue
            if (conns[target] or 0) > al['distance_left']:
                problems.append(f'relocate rejected: {target} ({conns[target]}) exceeds distance budget {al["distance_left"]}')
                continue
            if target in (o.get('exhausted_systems') or []):
                problems.append(f'relocate rejected: belts in {target} are known depleted')
                continue
            params = {'target_system': target}
        if skill == 'mine_cycle':
            if 'belts_ok' in obs[pilot] and not obs[pilot]['belts_ok']:
                problems.append(f'mine_cycle rejected: every belt in {st.get("system")} is known depleted')
                continue
            params['avoid_belts'] = obs[pilot].get('depleted_belts_here') or []
            params['max_mining_actions'] = int(min(params.get('max_mining_actions', 20),
                                                   cfg['limits']['max_mining_actions_per_cycle']))
            if st['cargo_capacity'] and st['cargo_free'] <= 0:
                problems.append(f'{pilot} cargo full: mining replaced by sell_and_complete')
                skill, params = 'sell_and_complete', {}
        if skill == 'sell_and_complete' and 'reserve' in params:
            res = params['reserve'] if isinstance(params['reserve'], dict) else {}
            params['reserve'] = {str(k): int(v) for k, v in res.items()
                                 if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
        if skill == 'list_item':
            if st['cargo'].get(params.get('item_id'), 0) < int(params.get('quantity', 1)):
                problems.append('list_item rejected: item not in cargo')
                continue
        params['spend_allowance'] = al['spend_allowance']
        seen.add(pilot)
        out.append({'pilot': pilot, 'skill': skill, 'params': params})
    return out, problems
