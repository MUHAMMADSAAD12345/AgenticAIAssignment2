"""Parsers for the plain-text replies of the live SpaceMolt MCP v2 server.

The v2 tools answer most queries in human-readable text rather than JSON (verified against the live
server), for example:

  get_status   "MenInBlack-P1 [nebula] | 250cr | Haven"
               "Fuel: 130/130 | Cargo: 0/110 | ..."   "Docked at: grand_exchange_station"
  get_system   "System: Haven (haven) | Empire: nebula | Security: ..." then tab tables
               "POIs (7):" (id name type class base base_type) and "Connections (2):" (system_id name distance)
  get_poi      "POI: Grand Exchange (grand_exchange) | Type: station"   "Station: Name (base_id)"
  get_missions blocks "--- Title [mission_id] (type, difficulty N) ---" with "Rewards: 3,500cr, ..." and
               "Objectives:" bullet lines such as "- Mine 20 units of Titanium Ore"
  view_market  tab table: category item_id item_name best_sell best_buy spread sell_qty buy_qty

get_player is the exception: it returns JSON ({"player": {..., "stats": {...}}}) with the Service Record
counters, so stats always come from there. mas/game_state.py falls back to these functions whenever a
reply carries only text.
"""
from __future__ import annotations
import re


def _num(s, default=None):
    if s is None:
        return default
    m = re.search(r'-?\d+(?:\.\d+)?', str(s).replace(',', ''))
    if not m:
        return default
    return float(m.group()) if '.' in m.group() else int(m.group())


def _int(s, default=None):
    v = _num(s, default)
    return int(v) if isinstance(v, (int, float)) else default


def slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', (name or '').strip().lower()).strip('_')


def text_tables(text: str) -> list[tuple[str, list[dict]]]:
    """Tab-separated tables: a header row followed by rows that also contain tabs."""
    out, lines, i = [], text.split('\n'), 0
    while i < len(lines):
        if '\t' in lines[i]:
            title = lines[i - 1].strip() if i else ''
            header = [h.strip().lower() for h in lines[i].split('\t')]
            rows, j = [], i + 1
            while j < len(lines) and '\t' in lines[j]:
                cells = lines[j].split('\t')
                rows.append({header[k]: (cells[k].strip() if k < len(cells) else '') for k in range(len(header))})
                j += 1
            out.append((title, rows))
            i = j
        else:
            i += 1
    return out


def table(text: str, *need: str) -> list[dict]:
    for _, rows in text_tables(text):
        if rows and all(n in rows[0] for n in need):
            return rows
    return []


def _pair(text: str, label: str):
    m = re.search(rf'\b{label}:\s*([\d,.]+)\s*/\s*([\d,.]+)', text, re.I)
    return (_int(m.group(1), 0), _int(m.group(2), 0)) if m else (0, 0)


def status(text: str) -> dict:
    head = re.search(r'^(.+?) \[([\w-]+)\] \| ([\d,]+)\s*cr \| (.+)$', text, re.M)
    fuel, max_fuel = _pair(text, 'Fuel')
    hull, max_hull = _pair(text, 'Hull')
    used, cap = _pair(text, 'Cargo')
    docked = re.search(r'^Docked at:\s*(\S+)', text, re.M)
    speed = re.search(r'\bSpeed:\s*([\d.]+)', text)
    conns = re.search(r'^Connections:\s*(.+)$', text, re.M)
    return {
        'username': head.group(1).strip() if head else None,
        'empire': head.group(2) if head else None,
        'credits': _int(head.group(3), 0) if head else 0,
        'system_name': head.group(4).strip() if head else None,
        'docked_at': docked.group(1) if docked else None,
        'fuel': fuel, 'max_fuel': max_fuel, 'hull': hull, 'max_hull': max_hull,
        'cargo_used': used, 'cargo_capacity': cap,
        'speed': _num(speed.group(1)) if speed else None,
        'connection_ids': [c.strip() for c in conns.group(1).split(',') if c.strip()] if conns else [],
        'in_transit': bool(re.search(r'\bin[ _]transit\b', text, re.I)),
    }


_CARGO_LINE = [
    (re.compile(r'\(([a-z][a-z0-9_]+)\)\D{0,4}?([\d,]+)\b', re.I), False),          # Iron Ore (iron_ore) x12
    (re.compile(r'^\s*[-*]?\s*([a-z][a-z0-9_]+)\s*(?:x|:)\s*([\d,]+)\b', re.I), False),  # iron_ore: 12
    (re.compile(r'^\s*[-*]?\s*([\d,]+)\s*x?\s+([A-Za-z][A-Za-z ]+?)\s*$'), True),   # 12x Iron Ore
]


def cargo(text: str) -> dict[str, int]:
    """Cargo contents. The non-empty format has not been observed live yet, so several layouts are accepted;
    callers also rely on the 'Cargo: used/capacity' counter, which has been observed."""
    out: dict[str, int] = {}
    rows = table(text, 'quantity') or table(text, 'qty')
    for r in rows:
        # live format: "item\tqty\tsize" with display names ("Copper Ore") -> item ids ("copper_ore")
        item = r.get('item_id') or r.get('id') or slug(r.get('item') or r.get('name') or r.get('item_name') or '')
        q = _int(r.get('quantity') or r.get('qty'), 0)
        if item and q:
            out[item] = out.get(item, 0) + q
    if out:
        return out
    for line in text.split('\n'):
        if re.match(r'\s*(capacity|credits|cargo is empty|cargo:)', line, re.I):
            continue
        for pat, name_last in _CARGO_LINE:
            m = pat.search(line)
            if m:
                item, q = (slug(m.group(2)), m.group(1)) if name_last else (m.group(1).lower(), m.group(2))
                if _int(q, 0):
                    out[item] = out.get(item, 0) + _int(q, 0)
                break
    return out


def system(text: str) -> dict:
    head = re.search(r'^System:\s*(.+?)\s*\(([\w-]+)\)', text, re.M)
    emp = re.search(r'\bEmpire:\s*([\w-]+)', text)
    sec = re.search(r'\bSecurity:\s*([^|\n]+)', text)
    pois = table(text, 'id', 'type')
    conns = table(text, 'system_id')
    return {
        'id': head.group(2) if head else None, 'name': head.group(1) if head else None,
        'empire': emp.group(1) if emp else None,
        'police_level': sec.group(1).strip() if sec else None,
        'pois': [{'id': r['id'], 'name': r.get('name'), 'type': f"{r.get('type', '')} {r.get('class', '')}".strip(),
                  'position': None, 'has_base': bool(r.get('base')), 'base_id': None,
                  'base_name': r.get('base') or None} for r in pois if r.get('id')],
        'connections': [{'system_id': r['system_id'], 'name': r.get('name'), 'distance': _num(r.get('distance'))}
                        for r in conns if r.get('system_id')],
    }


def poi(text: str) -> dict:
    head = re.search(r'^POI:\s*(.+?)\s*\(([\w-]+)\)(?:\s*\|\s*Type:\s*([\w-]+))?', text, re.M)
    base = re.search(r'^Station:\s*.+?\(([\w-]+)\)', text, re.M)
    rows = [r for _, rs in text_tables(text) for r in rs if any('resource' in k or k == 'ore' for k in r)]
    res = []
    for r in rows:
        rid = r.get('resource_id') or r.get('resource') or r.get('ore') or r.get('id')
        res.append({'resource_id': rid, 'richness': _num(r.get('richness')), 'remaining': _num(r.get('remaining'))})
    return {'id': head.group(2) if head else None, 'name': head.group(1) if head else None,
            'type': head.group(3) if head else None, 'base_id': base.group(1) if base else None, 'resources': res}


_OBJ_ITEM = re.compile(r'\b(?:mine|deliver|collect|bring|supply|gather|haul)\s+([\d,]+)\s+(?:units?\s+of\s+)?'
                       r'([A-Za-z][A-Za-z ]*?)\s*(?=[:(\[,;|]|\s-\s|-\s|\s\d|$|\bto\b|\bat\b)', re.I)


def missions(text: str) -> list[dict]:
    out = []
    for block in re.split(r'^--- ', text, flags=re.M)[1:]:
        head = re.match(r'(.+?)\s*\[([\w-]+)\]\s*(?:\(([\w ]+?)(?:,\s*difficulty\s*(\d+))?\))?\s*---', block)
        if not head:
            continue
        body = block[head.end():]
        rew = re.search(r'^\s*Rewards?:\s*([\d,]+)\s*cr', body, re.M | re.I)
        lines = [l for l in body.strip().split('\n') if l.strip()]
        objectives = []
        if 'Objectives:' in body:
            for line in body.split('Objectives:', 1)[1].split('\n'):
                line = line.strip()
                if not line.startswith('-'):
                    continue
                line = line.lstrip('- ').strip()
                prog = re.search(r'([\d,]+)\s*/\s*([\d,]+)', line)
                m = _OBJ_ITEM.search(line)
                objectives.append({
                    'item_id': slug(m.group(2)) if m else None,
                    'quantity': _int(m.group(1), 0) if m else (_int(prog.group(2), 0) if prog else 0),
                    'current': _int(prog.group(1), 0) if prog else 0,
                    'type': 'deliver' if m else None, 'description': line})
        pct = re.search(r'(\d+)\s*%\s*(?:complete|done)', body, re.I)
        out.append({'id': head.group(2), 'template_id': head.group(2), 'title': head.group(1).strip(),
                    'type': (head.group(3) or '').strip(), 'description': (lines[0] if lines else '')[:240],
                    'difficulty': _int(head.group(4)), 'reward_credits': _int(rew.group(1), 0) if rew else 0,
                    'objectives': objectives, 'percent_complete': _int(pct.group(1)) if pct else None,
                    'issuing_base': None, 'expires_in_ticks': None})
    return out


def market(text: str) -> dict[str, dict]:
    out = {}
    for r in table(text, 'item_id'):
        if r.get('item_id'):
            out[r['item_id']] = {'bid': _num(r.get('best_buy') or None), 'ask': _num(r.get('best_sell') or None),
                                 'name': r.get('item_name'), 'bid_qty': _int(r.get('buy_qty'), 0),
                                 'ask_qty': _int(r.get('sell_qty'), 0)}
    return out
