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


def main():
    test_schema_validation()
    test_parsers()
    test_error_classification()
    test_policy()
    test_idempotent_mutation()
    test_llm_planner_budget()
    print('MAS OFFLINE TESTS PASS')


if __name__ == '__main__':
    main()
