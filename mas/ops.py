"""Safe, idempotent pilot operations built on GameSession.

Each state-changing step has a deterministic key (mission_id:step). The worker's
SQLite ledger records 'sent' before the MCP call and 'confirmed' after the game
replies. On restart, confirmed steps are skipped and 'sent' steps with no reply
are reconciled against fresh game state, so a sale is never replayed blindly.
"""
from __future__ import annotations
from mas import game_state as gsx
from mas import text_formats as tf
from mas.log import event
from mas.mcp_game import GameError, GameSession, UnknownOutcome
from mas.store import Store


CARRY = ('system', 'poi', 'stats', 'home_base', 'home_poi', 'home_system')


async def read_status(gs: GameSession, full: bool = True, cache: dict | None = None) -> dict:
    """Pilot status from free queries (no tick cost).

    JSON servers (and the local mock) return everything from get_status. The live MCP v2 server returns
    get_status as text without stats or location ids, so a *full* read also calls:
      get_player (JSON: credits, home, Service Record stats), get_system / get_poi headers (location ids)
      and get_cargo (item lines; cargo_used/capacity from get_status is the fallback for mining checks).
    A *light* read (full=False) is get_status only, carrying location/stats over from the last full read in
    `cache`. It is used inside the mining loop: the gateway allows only 60 HTTP requests/min per IP, and
    mining only needs cargo_used/fuel/hull, which get_status provides. Only travel/jump change location,
    and those always take a full read afterwards.
    """
    st = gsx.parse_status(await gs.call('get_status'))
    if st.get('system') is not None and st['stats'].get('credits_earned') is not None:
        return st
    if not full and cache:
        for k in CARRY:
            if cache.get(k) is not None:
                st[k] = cache[k]
        st['cargo_known'] = False
        return st
    try:
        pl = await gs.call('get_player')
        player = pl.get('player') if isinstance(pl.get('player'), dict) else {}
        if player:
            st['stats'] = gsx.normalize_stats(player.get('stats'))
            st['credits'] = gsx.num(player.get('credits'), st['credits'])
            for k in ('username', 'empire', 'home_base', 'home_poi'):
                st[k] = player.get(k) or st.get(k)
            st['home_system'] = player.get('home_system')
    except GameError as e:
        event('observe.player_unavailable', agent=gs.agent, task_id=gs.task_id, pilot=gs.pilot_key, code=e.code)
    if st.get('system') is None and not st.get('in_transit'):
        names = getattr(gs, '_system_ids', None)
        if names is None:
            names = gs._system_ids = {}
        if st.get('system_name') in names:
            st['system'] = names[st['system_name']]
        else:
            st['system'] = gsx.parse_system(await gs.call('get_system')).get('id')
            if st.get('system_name') and st['system']:
                names[st['system_name']] = st['system']
        st['poi'] = gsx.parse_poi(await gs.call('get_poi')).get('id')
    if not st['cargo'] and st.get('cargo_used'):
        st['cargo'] = tf.cargo((await gs.call('get_cargo')).get('_text') or '')
        if not st['cargo']:
            event('observe.cargo_unparsed', agent=gs.agent, task_id=gs.task_id, pilot=gs.pilot_key,
                  cargo_used=st['cargo_used'], note='item lines not recognised; mining uses cargo_used')
    return st


class SafetyStop(Exception):
    pass


class PilotOps:
    def __init__(self, gs: GameSession, store: Store, mission_id: str, agent: str, cfg: dict, params: dict):
        self.gs, self.store, self.mid, self.agent, self.cfg = gs, store, mission_id, agent, cfg
        self.limits, self.safety = cfg['limits'], cfg['safety']
        self.params = params
        self.spend_allowance = float(params.get('spend_allowance', self.limits['spend_soft_cap']))
        self.spent_here = 0.0
        self.notes: list[str] = []

    def log(self, name, **data):
        event(name, agent=self.agent, task_id=self.mid, pilot=self.gs.pilot_key, **data)

    # observations (free queries)
    async def status(self, full: bool = True) -> dict:
        st = await read_status(self.gs, full=full, cache=getattr(self, '_last_full', None))
        if full:
            self._last_full = st
        return st

    async def system(self) -> dict:
        return gsx.parse_system(await self.gs.call('get_system'))

    async def poi(self) -> dict:
        return gsx.parse_poi(await self.gs.call('get_poi'))

    async def missions_available(self) -> list[dict]:
        try:
            return gsx.parse_missions(await self.gs.call('get_missions'))
        except GameError as e:
            self.log('observe.missions_unavailable', code=e.code)
            return []

    async def missions_active(self) -> list[dict]:
        """Active missions from the game; falls back to missions this worker accepted (kept in SQLite)."""
        remembered = self.store.kv_get(f'accepted:{self.gs.pilot_key}', {}) or {}
        try:
            live = gsx.parse_missions(await self.gs.call('get_active_missions'))
        except GameError as e:
            self.log('observe.active_missions_unavailable', code=e.code, fallback='remembered accepted missions')
            return list(remembered.values())
        live_ids = {m['id'] for m in live}
        stale = [k for k in remembered if k not in live_ids]
        if stale:
            for k in stale:
                remembered.pop(k)
            self.store.kv_set(f'accepted:{self.gs.pilot_key}', remembered)
        return live

    def remember_mission(self, m: dict, done: bool = False):
        key = f'accepted:{self.gs.pilot_key}'
        rem = self.store.kv_get(key, {}) or {}
        if done:
            rem.pop(m['id'], None)
        else:
            rem[m['id']] = m
        self.store.kv_set(key, rem)

    async def market(self) -> dict:
        try:
            return gsx.parse_market(await self.gs.call('view_market'))
        except GameError as e:
            self.log('observe.market_unavailable', code=e.code)
            return {}

    async def snapshot(self, full: bool = True) -> dict:
        s = await self.status(full=full)
        return {k: s[k] for k in ('credits', 'system', 'poi', 'docked', 'docked_at', 'fuel', 'cargo', 'cargo_used')}

    # idempotent mutation
    async def mutate(self, step: str, action: str, reconcile=None, **params) -> dict:
        key = f'{self.mid}:{step}'
        rec = self.store.action_get(key)
        if rec and rec['status'] == 'confirmed':
            self.log('idempotency.skip', step=step, action=action, reason='already confirmed in ledger')
            return rec['response'] or {}
        if rec and rec['status'] in ('sent', 'unknown') and reconcile:
            happened = await reconcile(rec['pre_state'] or {})
            self.log('recovery.reconcile', step=step, action=action, happened=happened)
            if happened:
                self.store.action_done(key, 'confirmed', {'reconciled': True})
                return {'reconciled': True}
        # mine needs only cargo_used/fuel (light read); sales, missions and movement keep a full pre-state
        # because their reconcilers compare cargo items, location or credits.
        pre = await self.snapshot(full=action != 'mine')
        self.store.action_sent(key, self.mid, self.gs.pilot_key, self.gs.tool_for(action), action, params, pre)
        try:
            resp = await self.gs.call(action, **params)
        except UnknownOutcome:
            self.store.action_done(key, 'unknown', error='reply lost')
            if reconcile and await reconcile(pre):
                self.store.action_done(key, 'confirmed', {'reconciled': True})
                self.log('recovery.reconcile', step=step, action=action, happened=True)
                return {'reconciled': True}
            raise
        except GameError as e:
            self.store.action_done(key, 'failed', error=f'{e.code}: {e.message[:200]}')
            raise
        slim = {k: v for k, v in resp.items() if k != '_text' and not isinstance(v, (list, dict))}
        slim['text'] = (resp.get('_text') or '')[:300]
        self.store.action_done(key, 'confirmed', slim)
        return resp

    # safety gates
    async def ensure_fuel(self, st: dict, need_frac: float | None = None):
        need = need_frac if need_frac is not None else self.limits['min_fuel_fraction']
        if st['fuel_frac'] >= need:
            return st
        if st['docked'] and self.safety.get('allow_refuel', True):
            if self.spent_here >= self.spend_allowance:
                raise SafetyStop(f'fuel {st["fuel_frac"]:.0%} low and spend allowance exhausted')
            before = st['credits']
            await self.mutate(f'refuel:{st["system"]}:{st["poi"]}', 'refuel',
                              reconcile=self._rec_fuel_up)
            st = await self.status()
            self.spent_here += max(0, before - st['credits'])
            self.log('safety.refuelled', fuel_frac=round(st['fuel_frac'], 2), cost=before - st['credits'])
            return st
        raise SafetyStop(f'fuel {st["fuel_frac"]:.0%} below safety floor and not docked')

    async def ensure_hull(self, st: dict):
        if st['hull_frac'] >= self.limits['min_hull_fraction']:
            return st
        if st['docked'] and self.safety.get('allow_repair', True) and self.spent_here < self.spend_allowance:
            before = st['credits']
            await self.mutate(f'repair:{st["poi"]}', 'repair')
            st = await self.status()
            self.spent_here += max(0, before - st['credits'])
            return st
        raise SafetyStop(f'hull {st["hull_frac"]:.0%} below safety floor')

    # movement
    async def travel(self, poi_id: str, step: str, fuel_floor: float | None = None) -> dict:
        st = await self.status()
        if st['poi'] == poi_id:
            return st
        if fuel_floor is None:
            st = await self.ensure_hull(st)
        st = await self.ensure_fuel(st, fuel_floor)
        await self.mutate(step, 'travel', reconcile=lambda pre: self._rec_at_poi(poi_id), id=poi_id)
        st = await self.status()
        if st['poi'] != poi_id:
            raise GameError('travel_unconfirmed', f'expected {poi_id}, at {st["poi"]}')
        self.log('game.confirmed', what='arrived', poi=poi_id, fuel_frac=round(st['fuel_frac'], 2))
        return st

    async def dock(self, step: str) -> dict:
        st = await self.status()
        if st['docked']:
            return st
        await self.mutate(step, 'dock', reconcile=self._rec_docked)
        st = await self.status()
        if not st['docked']:
            raise GameError('dock_unconfirmed', 'still undocked after dock')
        self.log('game.confirmed', what='docked', base=st['docked_at'])
        return st

    async def go_to_base_and_dock(self, tag: str) -> dict:
        st = await self.status()
        if st['docked']:
            return st
        system = await self.system()
        base = gsx.base_poi(system, st)
        if not base:
            raise SafetyStop('no base found in current system')
        await self.travel(base['id'], f'travel:base:{tag}', fuel_floor=0.0)
        return await self.dock(f'dock:{tag}')

    # reconcilers
    async def _rec_at_poi(self, poi_id):
        return (await self.status())['poi'] == poi_id

    async def _rec_docked(self, pre):
        return (await self.status())['docked']

    async def _rec_fuel_up(self, pre):
        return (await self.status())['fuel'] > (pre.get('fuel') or 0)
