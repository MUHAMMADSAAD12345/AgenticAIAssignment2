"""Tolerant parsers for SpaceMolt responses (field names from spacemolt.com/api.md core objects)."""
from __future__ import annotations
import math
import re
from typing import Any

STAT_KEYS = {
    'credits_earned': ['credits_earned', 'total_credits_earned', 'lifetime_credits_earned', 'earned'],
    'credits_spent': ['credits_spent', 'total_credits_spent', 'lifetime_credits_spent', 'spent'],
    'ore_mined': ['ore_mined', 'total_ore_mined', 'mined'],
    'trades_completed': ['trades_completed', 'trades'],
    'exchange_items_sold': ['exchange_items_sold', 'items_sold', 'exchange_sales'],
    'distance_traveled': ['distance_traveled', 'distance_travelled', 'distance'],
    'systems_explored': ['systems_explored', 'systems_visited', 'explored'],
}
BELT_WORDS = ('asteroid', 'belt')


def find_dict(obj, pred, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if pred(obj):
            return obj
        for v in obj.values():
            r = find_dict(v, pred, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj[:200]:
            r = find_dict(v, pred, depth + 1)
            if r is not None:
                return r
    return None


def find_list(obj, pred, depth=0):
    """First list of dicts whose first element satisfies pred."""
    if depth > 6:
        return None
    if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:5]) and pred(obj[0]):
        return obj
    if isinstance(obj, dict):
        for v in obj.values():
            r = find_list(v, pred, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj[:50]:
            r = find_list(v, pred, depth + 1)
            if r is not None:
                return r
    return None


def num(x, default=None):
    if isinstance(x, bool):
        return default
    if isinstance(x, (int, float)):
        return x
    if isinstance(x, str):
        m = re.search(r'-?\d+(?:\.\d+)?', x.replace(',', ''))
        if m:
            return float(m.group()) if '.' in m.group() else int(m.group())
    if isinstance(x, dict):
        for k in ('price', 'price_each', 'value', 'amount', 'credits'):
            if k in x:
                return num(x[k], default)
    return default


def normalize_stats(stats: dict | None) -> dict:
    stats = stats or {}
    out = {}
    lower = {str(k).lower(): v for k, v in stats.items()}
    for canon, keys in STAT_KEYS.items():
        val = None
        for k in keys:
            if k in lower:
                val = num(lower[k])
                break
        if val is None:
            for k, v in lower.items():
                if any(c in k for c in keys[:2]):
                    val = num(v)
                    break
        out[canon] = val
    return out


def cargo_map(ship: dict | None, extra: Any = None) -> dict[str, int]:
    items = (ship or {}).get('cargo')
    if not isinstance(items, list):
        items = find_list(extra, lambda d: 'item_id' in d and 'quantity' in d) or []
    out: dict[str, int] = {}
    for it in items:
        if isinstance(it, dict) and it.get('item_id'):
            out[it['item_id']] = out.get(it['item_id'], 0) + int(num(it.get('quantity'), 0) or 0)
    return out


def parse_status(data: dict) -> dict:
    player = data.get('player') if isinstance(data.get('player'), dict) else \
        find_dict(data, lambda d: 'credits' in d and ('username' in d or 'current_system' in d)) or {}
    ship = data.get('ship') if isinstance(data.get('ship'), dict) else \
        find_dict(data, lambda d: 'cargo_capacity' in d or 'max_fuel' in d) or {}
    fuel, max_fuel = num(ship.get('fuel'), 0), num(ship.get('max_fuel'), 0)
    hull, max_hull = num(ship.get('hull'), 0), num(ship.get('max_hull'), 0)
    used, cap = num(ship.get('cargo_used'), 0), num(ship.get('cargo_capacity'), 0)
    docked = player.get('docked_at_base')
    return {
        'username': player.get('username'),
        'empire': player.get('empire'),
        'credits': num(player.get('credits'), 0),
        'system': player.get('current_system') or (data.get('system') or {}).get('id'),
        'poi': player.get('current_poi') or (data.get('poi') or {}).get('id'),
        'docked_at': docked or None,
        'docked': bool(docked),
        'home_base': player.get('home_base'),
        'fuel': fuel, 'max_fuel': max_fuel,
        'fuel_frac': (fuel / max_fuel) if max_fuel else 1.0,
        'hull': hull, 'max_hull': max_hull,
        'hull_frac': (hull / max_hull) if max_hull else 1.0,
        'cargo_used': used, 'cargo_capacity': cap,
        'cargo_free': max(0, (cap or 0) - (used or 0)),
        'cargo': cargo_map(ship, data),
        'speed': num(ship.get('speed')),
        'stats': normalize_stats(player.get('stats')),
        'in_transit': bool(data.get('in_transit') or player.get('in_transit')),
    }


def parse_system(data: dict) -> dict:
    sysobj = data.get('system') if isinstance(data.get('system'), dict) else data
    pois = sysobj.get('pois') or find_list(data, lambda d: 'id' in d and 'type' in d and ('has_base' in d or 'position' in d)) or []
    conns = sysobj.get('connections') or find_list(data, lambda d: 'system_id' in d) or []
    return {
        'id': sysobj.get('id'), 'name': sysobj.get('name'), 'empire': sysobj.get('empire'),
        'police_level': sysobj.get('police_level'),
        'pois': [{'id': p.get('id'), 'name': p.get('name'), 'type': str(p.get('type') or ''),
                  'position': p.get('position'), 'has_base': bool(p.get('has_base')), 'base_id': p.get('base_id'),
                  'base_name': p.get('base_name')} for p in pois if isinstance(p, dict)],
        'connections': [{'system_id': c.get('system_id') or c.get('id'), 'name': c.get('name'),
                         'distance': num(c.get('distance'))} for c in conns if isinstance(c, dict)],
    }


def parse_poi(data: dict) -> dict:
    poi = data.get('poi') if isinstance(data.get('poi'), dict) else data
    res = poi.get('resources') or find_list(data, lambda d: 'resource_id' in d) or []
    return {'id': poi.get('id'), 'type': poi.get('type'), 'base_id': poi.get('base_id'),
            'resources': [{'resource_id': r.get('resource_id'), 'richness': num(r.get('richness')),
                           'remaining': num(r.get('remaining'))} for r in res if isinstance(r, dict)]}


def is_belt(poi: dict) -> bool:
    t = f"{poi.get('type', '')} {poi.get('name', '')}".lower()
    return any(w in t for w in BELT_WORDS)


def dist(a: dict | None, b: dict | None) -> float:
    try:
        return math.hypot(float(a['x']) - float(b['x']), float(a['y']) - float(b['y']))
    except (TypeError, KeyError, ValueError):
        return 1e9


def poi_by_id(system: dict, poi_id):
    return next((p for p in system['pois'] if p['id'] == poi_id), None)


def base_poi(system: dict, status: dict):
    bases = [p for p in system['pois'] if p['has_base']]
    for p in bases:
        if status.get('home_base') and status['home_base'] in (p.get('base_id'), p.get('id')):
            return p
    for p in bases:
        if status.get('docked_at') and status['docked_at'] in (p.get('base_id'), p.get('id')):
            return p
    here = poi_by_id(system, status.get('poi'))
    return min(bases, key=lambda p: dist((here or {}).get('position'), p.get('position'))) if bases else None


def belts_by_distance(system: dict, status: dict, avoid=()):
    here = poi_by_id(system, status.get('poi'))
    belts = [p for p in system['pois'] if is_belt(p) and p['id'] not in avoid]
    return sorted(belts, key=lambda p: dist((here or {}).get('position'), p.get('position')))


def parse_missions(data: dict) -> list[dict]:
    items = data.get('missions')
    if not isinstance(items, list):
        items = find_list(data, lambda d: ('mission_id' in d or 'id' in d or 'template_id' in d) and
                          ('title' in d or 'description' in d)) or []
    out = []
    for m in items:
        if not isinstance(m, dict):
            continue
        rewards = m.get('rewards') or m.get('reward') or {}
        credits = num(rewards.get('credits') if isinstance(rewards, dict) else rewards, 0) or num(m.get('reward_credits'), 0)
        objectives = []
        for o in m.get('objectives') or []:
            if isinstance(o, dict):
                objectives.append({'item_id': o.get('item_id') or o.get('resource_id') or o.get('target'),
                                   'quantity': num(o.get('quantity') or o.get('required') or o.get('target_quantity'), 0),
                                   'current': num(o.get('current') or o.get('progress'), 0),
                                   'type': o.get('type'), 'description': o.get('description')})
        text = f"{m.get('title', '')} {m.get('description', '')}"
        if not any(o['item_id'] for o in objectives):
            mm = re.search(r'(\d+)\s+(?:units of\s+)?([a-z][a-z ]*?ore)\b', text.lower())
            if mm:
                objectives.append({'item_id': mm.group(2).strip().replace(' ', '_'), 'quantity': int(mm.group(1)),
                                   'current': 0, 'type': 'deliver', 'description': None})
        out.append({'id': m.get('mission_id') or m.get('id') or m.get('template_id'),
                    'template_id': m.get('template_id'),
                    'title': m.get('title'), 'type': str(m.get('type') or ''),
                    'description': (m.get('description') or '')[:240],
                    'difficulty': m.get('difficulty'), 'reward_credits': credits,
                    'objectives': objectives, 'percent_complete': m.get('percent_complete'),
                    'issuing_base': m.get('issuing_base'), 'expires_in_ticks': m.get('expires_in_ticks')})
    return out


def mission_kind(m: dict) -> str:
    t = f"{m.get('type', '')} {m.get('title', '')} {m.get('description', '')}".lower()
    if any(w in t for w in ('combat', 'bounty', 'kill', 'destroy', 'pirate', 'escort', 'smuggl')):
        return 'unsafe'
    if any(w in t for w in ('ore', 'mining', 'mine ', 'supply run', 'requisition')):
        return 'mining'
    if any(w in t for w in ('sell order', 'buy order', 'listing', 'market participation', 'exchange')):
        return 'market'
    if any(w in t for w in ('deliver', 'delivery', 'resupply', 'haul', 'cargo')):
        return 'delivery'
    if any(w in t for w in ('visit', 'explore', 'survey', 'tour', 'audit')):
        return 'exploration'
    return 'other'


def parse_market(data: dict) -> dict[str, dict]:
    items = data.get('items') if isinstance(data.get('items'), list) else \
        find_list(data, lambda d: 'item_id' in d) or []
    out = {}
    for it in items:
        if not isinstance(it, dict) or not it.get('item_id'):
            continue
        bid = None
        for k in ('best_buy', 'best_bid', 'highest_bid', 'buy_price', 'bid'):
            if k in it:
                bid = num(it[k])
                break
        ask = None
        for k in ('best_sell', 'best_ask', 'lowest_ask', 'sell_price', 'ask'):
            if k in it:
                ask = num(it[k])
                break
        if bid is None and isinstance(it.get('buy_orders'), list) and it['buy_orders']:
            bid = max(num(o.get('price') or o.get('price_each'), 0) or 0 for o in it['buy_orders'])
        out[it['item_id']] = {'bid': bid, 'ask': ask, 'name': it.get('name')}
    return out


def sale_value(data: dict) -> float | None:
    for k in ('credits_earned', 'total', 'total_price', 'credits_received', 'earned', 'proceeds'):
        v = data.get(k)
        if isinstance(v, (int, float)):
            return v
    return None
