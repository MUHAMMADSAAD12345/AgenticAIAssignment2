"""Offline unit tests for the MAS (no game, no network, no real LLM).
Run: python -m tests.test_offline
"""
from __future__ import annotations
import asyncio
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

TMP = tempfile.mkdtemp(prefix='mas_unit_')
os.environ['MAS_LOGS'] = TMP
os.environ['GROQ_API_KEY'] = 'gsk_FAKEKEYFORTESTSONLY000000000000'

from mas import config, game_state as gsx  # noqa: E402
from mas.llm import Planner  # noqa: E402
from mas.mcp_game import GameError, classify_error, parse_tool_result, validate_args, schema_actions  # noqa: E402
from mas.ops import PilotOps  # noqa: E402
from mas.policy import combined_progress, fallback_plan, validate_plan  # noqa: E402
from mas.store import Store  # noqa: E402

CFG = config.load()
SCHEMA = {'type': 'object', 'required': ['action'], 'properties': {
    'action': {'type': 'string', 'enum': ['travel', 'mine', 'sell', 'get_status']},
    'id': {'type': 'string'}, 'quantity': {'type': 'integer', 'minimum': 1}}}

STATUS = {'player': {'username': 'AI26-G01-A', 'empire': 'nebula', 'credits': 300, 'current_system': 'sys1',
                     'current_poi': 'st1', 'home_base': 'b1', 'docked_at_base': 'b1',
                     'stats': {'credits_earned': 120, 'credits_spent': 40, 'ore_mined': 12, 'trades_completed': 0,
                               'exchange_items_sold': 3, 'distance_traveled': 200, 'systems_explored': 1}},
          'ship': {'fuel': 80, 'max_fuel': 100, 'hull': 90, 'max_hull': 100, 'cargo_used': 20, 'cargo_capacity': 60,
                   'cargo': [{'item_id': 'iron_ore', 'quantity': 20}]}}


def test_schema_validation():
    assert schema_actions(SCHEMA) == {'travel', 'mine', 'sell', 'get_status'}
    assert validate_args(SCHEMA, {'action': 'travel', 'id': 'belt'}) == []
    assert any('unknown parameter' in p for p in validate_args(SCHEMA, {'action': 'travel', 'poi': 'x'}))
    assert any('should be' in p for p in validate_args(SCHEMA, {'action': 'sell', 'id': 'x', 'quantity': '5'}))
    assert any('not in allowed' in p for p in validate_args(SCHEMA, {'action': 'attack'}))
    assert any('minimum' in p for p in validate_args(SCHEMA, {'action': 'sell', 'id': 'x', 'quantity': 0}))
    assert any('missing' in p for p in validate_args(SCHEMA, {'id': 'x'}))
    print('[PASS] schema-aware argument validation')


def test_parsers():
    st = gsx.parse_status(STATUS)
    assert st['credits'] == 300 and st['docked'] and st['cargo'] == {'iron_ore': 20}
    assert abs(st['fuel_frac'] - 0.8) < 1e-9 and st['cargo_free'] == 40
    assert st['stats']['ore_mined'] == 12 and st['stats']['exchange_items_sold'] == 3
    ms = gsx.parse_missions({'missions': [{'mission_id': 'm1', 'title': 'Copper Requisition',
                                           'description': 'deliver 25 copper ore for 1,800 credits', 'rewards': {'credits': 1800}}]})
    assert ms[0]['objectives'][0] == {'item_id': 'copper_ore', 'quantity': 25, 'current': 0, 'type': 'deliver', 'description': None}
    assert gsx.mission_kind(ms[0]) == 'mining'
    assert gsx.mission_kind({'title': 'Pirate Bounty', 'type': 'combat', 'description': ''}) == 'unsafe'
    mk = gsx.parse_market({'items': [{'item_id': 'iron_ore', 'best_buy': {'price': 5, 'quantity': 100}, 'best_sell': 9}]})
    assert mk['iron_ore']['bid'] == 5 and mk['iron_ore']['ask'] == 9
    print('[PASS] tolerant response parsers')


def test_error_classification():
    def res(sc, text='', is_error=False):
        return SimpleNamespace(structured_content=sc, content=[SimpleNamespace(text=text)], is_error=is_error)
    for sc, code in [({'error': {'code': 'action_pending', 'message': 'wait'}}, 'action_pending'),
                     ({'error': 'rate_limited', 'retry_after': 7}, 'rate_limited'),
                     ({'error': {'message': 'Session expired, please login'}}, 'session_invalid')]:
        data, text = parse_tool_result(res(sc))
        e = classify_error(res(sc), data, text)
        assert e and e.code == code, (sc, e and e.code)
    data, text = parse_tool_result(res(None, 'Error: ship in transit, arriving in 20 seconds', True))
    e = classify_error(res(None, 'x', True), data, 'Error: ship in transit, arriving in 20 seconds')
    assert e.code == 'in_transit' and e.retry_after == 20
    ok = res({'player': {}, 'error': None})
    assert classify_error(ok, *parse_tool_result(ok)) is None
    print('[PASS] game error classification (pending / transit / rate limit / session)')


def obs_for(status_overrides=None, missions=None):
    raw = json.loads(json.dumps(STATUS))
    st = gsx.parse_status(raw)
    st.update(status_overrides or {})
    return {'status': st, 'available_missions': missions or [], 'active_missions': [], 'market': {}}


def test_policy():
    m = {'id': 'm1', 'title': 'Iron Supply Run', 'type': 'mining', 'description': '', 'reward_credits': 1500,
         'objectives': [{'item_id': 'iron_ore', 'quantity': 30}]}
    bad = {'id': 'm9', 'title': 'Pirate Bounty', 'type': 'combat', 'description': '', 'reward_credits': 9000, 'objectives': []}
    obs = {'A': obs_for({'cargo': {}, 'cargo_used': 0, 'cargo_free': 60}, [m, bad]), 'B': obs_for()}
    prog = combined_progress(obs, CFG)
    assert prog['totals']['ore_mined'] == 24 and not prog['all_targets_met']
    plan = {'assignments': [{'pilot': 'A', 'skill': 'accept_mission', 'params': {'mission_id': 'm9'}},
                            {'pilot': 'A', 'skill': 'mine_cycle', 'params': {}},
                            {'pilot': 'B', 'skill': 'attack', 'params': {}},
                            {'pilot': 'C', 'skill': 'mine_cycle', 'params': {}}]}
    clean, problems = validate_plan(plan, obs, CFG, prog)
    assert clean == [] or all(a['pilot'] == 'A' and a['skill'] != 'accept_mission' for a in clean)
    assert any('unsafe' in p for p in problems) and any('not allowed' in p for p in problems)
    clean, _ = validate_plan({'assignments': [{'pilot': 'A', 'skill': 'mine_cycle', 'params': {'max_mining_actions': 999}}]},
                             obs, CFG, prog)
    assert clean[0]['params']['max_mining_actions'] == CFG['limits']['max_mining_actions_per_cycle']
    assert 'spend_allowance' in clean[0]['params']
    clean, problems = validate_plan({'assignments': [{'pilot': 'A', 'skill': 'accept_mission', 'params': {'mission_id': 'gone'}}]},
                                    obs, CFG, prog)
    assert not clean and 'not in fresh observation' in problems[0]
    fb = fallback_plan(obs, prog, CFG, [])
    skills = {a['pilot']: a['skill'] for a in fb['assignments']}
    assert skills['A'] == 'accept_mission' and skills['B'] == 'sell_and_complete', skills
    print('[PASS] plan validation (whitelist, safety, freshness, caps) and fallback policy')


class FakeSession:
    pilot_key = 'A'

    def __init__(self):
        self.state = json.loads(json.dumps(STATUS))
        self.sent = []

    def tool_for(self, action):
        return 'spacemolt'

    async def call(self, action, **kw):
        if action == 'get_status':
            return json.loads(json.dumps(self.state))
        self.sent.append((action, kw))
        if action == 'sell':
            self.state['ship']['cargo'] = []
            self.state['player']['credits'] += 100
        return {'message': 'ok', '_text': 'ok'}


def test_idempotent_mutation():
    store = Store(Path(TMP) / 'unit.sqlite3')
    gs = FakeSession()
    ops = PilotOps(gs, store, 'M-1', 'unit', CFG, {})

    async def rec(pre):
        return (await ops.status())['cargo'].get('iron_ore', 0) < (pre.get('cargo') or {}).get('iron_ore', 0)

    async def go():
        await ops.mutate('sell:iron_ore', 'sell', reconcile=rec, id='iron_ore', quantity=20)
        await ops.mutate('sell:iron_ore', 'sell', reconcile=rec, id='iron_ore', quantity=20)
        store.action_sent('M-2:sell:iron_ore', 'M-2', 'A', 'spacemolt', 'sell', {}, {'cargo': {'iron_ore': 20}})
        ops2 = PilotOps(gs, store, 'M-2', 'unit', CFG, {})
        r = await ops2.mutate('sell:iron_ore', 'sell', reconcile=rec, id='iron_ore', quantity=20)
        return r
    r = asyncio.run(go())
    assert [a for a, _ in gs.sent] == ['sell'], gs.sent
    assert r == {'reconciled': True}
    assert store.action_get('M-1:sell:iron_ore')['status'] == 'confirmed'
    store.mission_begin('X', 'unit', 'mine_cycle', 'A', {})
    store.mission_finish('X', 'completed', {'ok': True})
    assert store.mission_get('X')['status'] == 'completed'
    print('[PASS] mutation ledger: confirmed step skipped, unknown-outcome step reconciled (no replay)')


def test_llm_planner_budget():
    planner = Planner(CFG)
    canned = {'rationale': 'mine first', 'assignments': [{'pilot': 'A', 'skill': 'mine_cycle', 'params': {}}]}

    def fake_call(prompt):
        assert 'gsk_' not in prompt
        msg = SimpleNamespace(content=json.dumps(canned))
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=500, completion_tokens=120),
                               choices=[SimpleNamespace(message=msg)])
    planner._call = fake_call
    plan = asyncio.run(planner.plan({'cycle': 1}, 'cycle-1'))
    assert plan == canned
    b = planner.budget()
    assert b['requests'] == 1 and b['tokens'] == 620
    log = (Path(TMP) / 'events.jsonl').read_text()
    assert 'gsk_FAKE' not in log
    print('[PASS] LLM planner: JSON plan parsed, budget ledger updated, key never logged')


LIVE_STATUS = ('MenInBlack-P1 [nebula] | 250cr | Haven\n'
               'Ship: Prospect (prospect) | Hull: 95/95 | Shield: 50/50 (+1/tick) | Armor: 4 | Speed: 1\n'
               'Fuel: 130/130 | Cargo: 12/110 | CPU: 4/13 | Power: 10/26\n'
               'Docked at: grand_exchange_station\nSecurity: Maximum Security (empire capital)\n'
               'Connections: market_prime, traders_rest\n')
LIVE_SYSTEM = ('System: Haven (haven) | Empire: nebula | Security: Maximum Security (empire capital)\n'
               'Haven is the commercial heart of the Federation.\nPOIs (3):\n'
               'id\tname\ttype\tclass\tbase\tbase_type\n'
               'haven_star\tHaven Star\tsun\tG5V\t\t\n'
               'grand_exchange\tGrand Exchange\tstation\t\tGrand Exchange Station\tstation\n'
               'commerce_fields\tCommerce Fields\tasteroid_belt\tmetallic\t\t\n'
               'Connections (2):\nsystem_id\tname\tdistance\n'
               'market_prime\tMarket Prime\t407 GU\ntraders_rest\tTrader\'s Rest\t514 GU\n')
LIVE_POI = ('POI: Grand Exchange (grand_exchange) | Type: station\nThe largest market.\n'
            'Station: Grand Exchange Station (grand_exchange_station)\nServices: market, missions, refuel\n')
LIVE_MISSIONS = ('Fixer network: debt 0cr; allowance 2,500cr; exposure 0cr; available 2,500cr.\n'
                 'Missions at Grand Exchange Station (2):\n'
                 '--- Titanium Extraction Contract [titanium_extraction_contract] (mining, difficulty 3) ---\n'
                 'Titanium is in high demand for hull plating.\nFrom: Procurement Agent\n'
                 'Rewards: 3,500cr, +25 mining XP\nObjectives:\n  - Mine 20 units of Titanium Ore\n'
                 '--- Market Participation: Selling [market_participation_selling] (trading, difficulty 1) ---\n'
                 'Sell items on the station exchange.\nRewards: 1,000cr, +15 trading XP\n'
                 'Objectives:\n  - Sell 10 units of any item on the exchange\n')
LIVE_MARKET = ('Market summary at Grand Exchange Station (2 items):\n'
               'category\titem_id\titem_name\tbest_sell\tbest_buy\tspread\tsell_qty\tbuy_qty\n'
               'ore\tiron_ore\tIron Ore\t12cr\t8cr\t4cr\t500\t1,200\n'
               'ammo\temp_missiles\tEMP Missiles\t1,900cr\t2cr\t1,898cr\t21\t303\n')


def test_live_text_formats():
    st = gsx.parse_status({'_text': LIVE_STATUS})
    assert st['username'] == 'MenInBlack-P1' and st['empire'] == 'nebula' and st['credits'] == 250
    assert st['docked'] and st['docked_at'] == 'grand_exchange_station'
    assert (st['fuel'], st['max_fuel'], st['cargo_used'], st['cargo_capacity'], st['cargo_free']) == (130, 130, 12, 110, 98)
    assert st['hull_frac'] == 1.0
    sy = gsx.parse_system({'_text': LIVE_SYSTEM})
    assert sy['id'] == 'haven' and [c['system_id'] for c in sy['connections']] == ['market_prime', 'traders_rest']
    assert sy['connections'][0]['distance'] == 407
    assert [p['id'] for p in gsx.belts_by_distance(sy, st)] == ['commerce_fields']
    assert gsx.base_poi(sy, dict(st, home_poi='grand_exchange'))['id'] == 'grand_exchange'
    po = gsx.parse_poi({'_text': LIVE_POI})
    assert po['id'] == 'grand_exchange' and po['base_id'] == 'grand_exchange_station'
    ms = gsx.parse_missions({'_text': LIVE_MISSIONS})
    assert [m['id'] for m in ms] == ['titanium_extraction_contract', 'market_participation_selling']
    assert ms[0]['reward_credits'] == 3500 and ms[0]['type'] == 'mining' and ms[0]['difficulty'] == 3
    assert ms[0]['objectives'][0]['item_id'] == 'titanium_ore' and ms[0]['objectives'][0]['quantity'] == 20
    assert gsx.mission_kind(ms[0]) == 'mining' and gsx.mission_kind(ms[1]) == 'market'
    assert gsx.mission_doable_here(ms[0]) and gsx.mission_doable_here(ms[1])
    far = gsx.parse_missions({'_text': '--- Overdue Accounts [overdue_accounts] (mining, difficulty 2) ---\n'
                              'Rewards: 7,000cr\nObjectives:\n  - Travel to the Procyon system\n'
                              '  - Deliver 10 Platinum Ore to Procyon Colonial Station\n'
                              '--- First Haul [first_haul] (salvage, difficulty 1) ---\nSalvage before more pilots arrive.\n'
                              'Rewards: 500cr\nObjectives:\n  - Sell 1 wreck at a salvage yard\n'
                              '--- Seam Glass [seam] (mining, difficulty 2) ---\nRewards: 7,000cr\nObjectives:\n'
                              '  - Mine 5 Seam Glass past the rim\n'})
    assert not any(gsx.mission_doable_here(m) for m in far), [m['id'] for m in far if gsx.mission_doable_here(m)]
    assert gsx.mission_kind(far[1]) != 'mining'  # 'more'/'before' must not read as 'ore'
    mk = gsx.parse_market({'_text': LIVE_MARKET})
    assert mk['iron_ore']['bid'] == 8 and mk['iron_ore']['ask'] == 12 and mk['emp_missiles']['ask'] == 1900
    from mas import text_formats as tf
    assert tf.cargo('Cargo is empty. Capacity: 0/0, 0 available.\nCredits: 250cr') == {}
    assert tf.cargo('item_id\tname\tquantity\tsize\niron_ore\tIron Ore\t12\t1\n') == {'iron_ore': 12}
    assert tf.cargo('Iron Ore (iron_ore) x12\nCopper Ore (copper_ore) x3') == {'iron_ore': 12, 'copper_ore': 3}
    # get_player keeps JSON: a reply with structured keys must not be treated as text-only
    assert gsx.parse_status({'player': {'username': 'x', 'credits': 5}, '_text': 'x'})['credits'] == 5
    print('[PASS] live MCP v2 text formats (status, system, poi, missions, market, cargo)')


def test_relocation_rules():
    from mas.policy import relocation_target, allowances
    o = obs_for({'cargo': {}, 'cargo_used': 0, 'cargo_free': 60})
    o['status']['system'] = 'haven'
    o.update({'belts': [{'id': 'commerce_fields'}], 'belts_ok': [], 'depleted_belts_here': ['commerce_fields'],
              'connections': [{'system_id': 'market_prime', 'distance': 407}, {'system_id': 'traders_rest', 'distance': 514}],
              'system_yield': {}, 'exhausted_systems': ['haven'], 'active_missions': [{'id': 'm'}] * 4})
    obs = {'A': o, 'B': json.loads(json.dumps(o))}
    prog = combined_progress(obs, CFG)
    al = allowances(prog, CFG)
    assert relocation_target('A', obs, o, al, set())[0] == 'market_prime'          # scout nearest
    assert relocation_target('B', obs, o, al, {'market_prime'})[0] is None          # wait for the scout
    obs['A']['status']['system'] = 'market_prime'
    obs['B']['system_yield'] = {'market_prime': 12}
    assert relocation_target('B', obs, obs['B'], al, set())[0] == 'market_prime'    # follow proven yield
    clean, problems = validate_plan({'assignments': [
        {'pilot': 'B', 'skill': 'mine_cycle', 'params': {}}]}, obs, CFG, prog)
    assert not clean and 'known depleted' in problems[0]
    clean, problems = validate_plan({'assignments': [
        {'pilot': 'B', 'skill': 'relocate', 'params': {'target_system': 'sirius'}}]}, obs, CFG, prog)
    assert not clean and 'not adjacent' in problems[0]
    clean, _ = validate_plan({'assignments': [
        {'pilot': 'B', 'skill': 'relocate', 'params': {'target_system': 'market_prime', 'x': 1}}]}, obs, CFG, prog)
    assert clean[0]['params']['target_system'] == 'market_prime' and 'x' not in clean[0]['params']
    obs['B']['available_missions'] = [{'id': 'iron', 'title': 'Iron', 'type': 'mining', 'description': '',
                                       'objectives': [{'item_id': 'iron_ore', 'quantity': 10}]}]
    clean, problems = validate_plan({'assignments': [
        {'pilot': 'B', 'skill': 'accept_mission', 'params': {'mission_id': 'iron'}}]}, obs, CFG, prog)
    assert not clean and 'maximum number of active missions' in problems[0]
    assert gsx.unsafe_security('Lawless (no police)') and not gsx.unsafe_security('Maximum Security (empire capital)')
    print('[PASS] relocation policy (scout / wait / follow) and validator rules (depleted, adjacency, mission cap)')


def main():
    test_schema_validation()
    test_relocation_rules()
    test_parsers()
    test_live_text_formats()
    test_error_classification()
    test_policy()
    test_idempotent_mutation()
    test_llm_planner_budget()
    print('MAS OFFLINE TESTS PASS')


if __name__ == '__main__':
    main()
