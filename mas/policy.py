"""Plan schema, validation and the deterministic fallback policy used by the coordinator."""
from __future__ import annotations
from mas import game_state as gsx

SKILL_AGENT = {
    'mine_cycle': 'mining',
    'accept_mission': 'trading',
    'sell_and_complete': 'trading',
    'list_item': 'trading',
    'explore_adjacent': 'trading',
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


def fallback_plan(obs: dict[str, dict], progress: dict, cfg: dict, history: list[dict]) -> dict:
    """Rule-based policy: used when the LLM is unavailable/over budget or returns an invalid plan."""
    al = allowances(progress, cfg)
    explored_needed = not progress['met']['systems_explored']
    explore_done = any(h['skill'] == 'explore_adjacent' and h['status'] == 'completed' for h in history)
    assignments, reasons, taken = [], [], set()
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
        if st['docked'] and (st['cargo'] and any(not k.startswith('fuel') for k in st['cargo'])) and \
                (cargo_frac >= 0.5 or not active or any(_satisfied(m, st) for m in active)):
            assignments.append({'pilot': pilot, 'skill': 'sell_and_complete', 'params': {}})
            reasons.append(f'{pilot}: docked with cargo -> complete missions / sell')
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
                                           'target_item': target, 'return_to_base': True}})
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
        if skill == 'mine_cycle':
            params['max_mining_actions'] = int(min(params.get('max_mining_actions', 20),
                                                   cfg['limits']['max_mining_actions_per_cycle']))
            if st['cargo_capacity'] and st['cargo_free'] <= 0:
                problems.append(f'{pilot} cargo full: mining replaced by sell_and_complete')
                skill, params = 'sell_and_complete', {}
        if skill == 'list_item':
            if st['cargo'].get(params.get('item_id'), 0) < int(params.get('quantity', 1)):
                problems.append('list_item rejected: item not in cargo')
                continue
        params['spend_allowance'] = al['spend_allowance']
        seen.add(pilot)
        out.append({'pilot': pilot, 'skill': skill, 'params': params})
    return out, problems
