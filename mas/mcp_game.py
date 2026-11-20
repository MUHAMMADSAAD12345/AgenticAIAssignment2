"""Authenticated SpaceMolt MCP v2 session with schema-aware validation and recovery.

Every gameplay call goes through GameSession.call(), which:
  1. routes the action to the tool whose schema lists it,
  2. validates arguments against the discovered input schema,
  3. calls the tool over MCP and parses structuredContent / text,
  4. classifies game errors and applies the documented recovery
     (action_pending -> wait a tick, in_transit -> wait, rate_limited -> retry_after,
      session_invalid -> re-login, transport failure -> reconnect).
"""
from __future__ import annotations
import asyncio
import getpass
import json
import os
import re
import time
from contextlib import AsyncExitStack
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from mas.log import event, sanitize

DEFAULT_TOOL = {
    'login': 'spacemolt_auth', 'register': 'spacemolt_auth', 'logout': 'spacemolt_auth',
    'view_market': 'spacemolt_market', 'create_sell_order': 'spacemolt_market',
    'create_buy_order': 'spacemolt_market', 'cancel_order': 'spacemolt_market',
    'modify_order': 'spacemolt_market', 'analyze_market': 'spacemolt_market',
    'estimate_purchase': 'spacemolt_market', 'view_orders': 'spacemolt_market',
    'storage': 'spacemolt_storage',
}
MUTATIONS = {'travel', 'jump', 'dock', 'undock', 'mine', 'sell', 'buy', 'create_sell_order',
             'create_buy_order', 'cancel_order', 'modify_order', 'accept_mission', 'complete_mission',
             'abandon_mission', 'refuel', 'repair', 'craft', 'set_home_base', 'storage'}
FORBIDDEN = {'attack', 'send_gift', 'trade_offer', 'trade_accept', 'buy_listed_ship', 'self_destruct',
             'faction_create_sell_order', 'faction_create_buy_order', 'register'}
KNOWN_CODES = ('action_pending', 'in_transit', 'rate_limited', 'session_invalid', 'not_logged_in',
               'not_authenticated', 'unauthorized', 'invalid_params', 'invalid_argument', 'unknown_action',
               'not_docked', 'already_docked', 'docked', 'cargo_full', 'insufficient_fuel', 'no_fuel',
               'insufficient_credits', 'not_found', 'no_base', 'depleted', 'too_sparse')
SESSION_CODES = {'session_invalid', 'not_logged_in', 'not_authenticated', 'unauthorized', 'session_expired'}
JSON_TYPES = {'string': str, 'integer': int, 'number': (int, float), 'boolean': bool,
              'array': list, 'object': dict, 'null': type(None)}


class GameError(Exception):
    def __init__(self, code: str, message: str, retry_after: float | None = None, data: Any = None):
        super().__init__(f'{code}: {message}')
        self.code, self.message, self.retry_after, self.data = code, message, retry_after, data


class InvalidParams(GameError):
    pass


class UnknownOutcome(GameError):
    """A mutation was sent but no reply arrived: caller must re-observe before retrying."""


class ForbiddenAction(GameError):
    pass


def password_for(cfg: dict, pilot_key: str, username: str) -> str:
    for name in (f'SPACEMOLT_PASSWORD_{pilot_key}', 'SPACEMOLT_PASSWORD__' + re.sub(r'\W', '_', username).upper()):
        if os.environ.get(name):
            return os.environ[name]
    try:
        import keyring
        pw = keyring.get_password(cfg.get('keyring_service', 'spacemolt-course-test'), username)
        if pw:
            return pw
    except Exception:
        pass
    if os.environ.get('MAS_NONINTERACTIVE'):
        raise SystemExit(f'No stored password for {username}. Run examples\\pilot_setup.py or set SPACEMOLT_PASSWORD_{pilot_key}.')
    return getpass.getpass(f'Password for {username} (hidden): ')


def _variants(schema: dict) -> list[dict]:
    for key in ('oneOf', 'anyOf'):
        if isinstance(schema.get(key), list):
            return [v for v in schema[key] if isinstance(v, dict)]
    return [schema]


def schema_actions(schema: dict) -> set[str]:
    out = set()
    for v in _variants(schema):
        a = (v.get('properties') or {}).get('action') or {}
        if 'const' in a:
            out.add(a['const'])
        out.update(a.get('enum') or [])
    return out


def validate_args(schema: dict, args: dict) -> list[str]:
    """Return a list of validation problems (empty == valid)."""
    action = args.get('action')
    candidates = _variants(schema)
    if len(candidates) > 1:
        matched = [v for v in candidates if action in schema_actions(v)]
        candidates = matched or candidates
    best = None
    for v in candidates:
        problems = []
        props = v.get('properties') or {}
        for r in v.get('required') or []:
            if r not in args:
                problems.append(f'missing required parameter {r!r}')
        for k, val in args.items():
            if props and k not in props:
                if v.get('additionalProperties') is not True:
                    problems.append(f'unknown parameter {k!r}')
                continue
            p = props.get(k) or {}
            types = p.get('type')
            types = [types] if isinstance(types, str) else (types or [])
            if types and not any(isinstance(val, JSON_TYPES.get(t, object)) and not
                                 (t in ('integer', 'number') and isinstance(val, bool)) for t in types):
                problems.append(f'parameter {k!r} should be {types}, got {type(val).__name__}')
            if p.get('enum') and val not in p['enum']:
                problems.append(f'parameter {k!r}={val!r} not in allowed values')
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                if 'minimum' in p and val < p['minimum']:
                    problems.append(f'parameter {k!r} below minimum {p["minimum"]}')
        if not problems:
            return []
        if best is None or len(problems) < len(best):
            best = problems
    return best or []


def parse_tool_result(res) -> tuple[dict, str]:
    text = ' '.join(getattr(c, 'text', '') or '' for c in (res.content or [])).strip()
    data = res.structured_content
    if not isinstance(data, dict):
        try:
            data = json.loads(text) if text.startswith(('{', '[')) else {}
        except ValueError:
            data = {}
        if isinstance(data, list):
            data = {'items': data}
    if isinstance(data.get('structuredContent'), dict):
        outer = data
        data = dict(outer['structuredContent'])
        if outer.get('error'):
            data.setdefault('error', outer['error'])
        text = text or str(outer.get('result') or '')
    return data, text


def classify_error(res, data: dict, text: str) -> GameError | None:
    err = data.get('error') if isinstance(data, dict) else None
    if not res.is_error and not err and data.get('success') is not False:
        return None
    code, msg, retry = None, '', None
    if isinstance(err, dict):
        code = err.get('code') or err.get('type')
        msg = err.get('message') or ''
        retry = err.get('retry_after') or (err.get('details') or {}).get('retry_after')
    elif isinstance(err, str):
        code = err if re.fullmatch(r'[a-z_]+', err) else None
        msg = err
    retry = retry or data.get('retry_after')
    blob = f'{code or ""} {msg} {text}'.lower()
    if not code:
        code = next((c for c in KNOWN_CODES if c in blob), None)
    if not code and re.search(r'not logged in|login required|session (has )?expired', blob):
        code = 'session_invalid'
    if not code and re.search(r'in transit|arriv', blob):
        code = 'in_transit'
    if retry is None:
        m = re.search(r'(\d+(?:\.\d+)?)\s*(?:s\b|sec|second)', blob)
        retry = float(m.group(1)) if m else None
    return GameError(code or 'game_error', (msg or text)[:400], retry, data)


class GameSession:
    """One authenticated MCP connection for one pilot, used by one agent process."""

    def __init__(self, cfg: dict, pilot_key: str, agent: str, task_id: str | None = None):
        self.cfg, self.pilot_key, self.agent, self.task_id = cfg, pilot_key, agent, task_id
        self.username = cfg['pilots'].get(pilot_key, pilot_key)
        self.url = cfg['mcp_endpoint']
        self.timeout = float(cfg['limits'].get('mcp_call_timeout_seconds', 600))
        self.tools: dict[str, dict] = {}
        self.action_tool: dict[str, str] = {}
        self.session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._session_id: str | None = None
        self.calls = 0
        self.mutations = 0

    async def __aenter__(self):
        await self.connect()
        await self.login()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        if self._stack:
            try:
                await self._stack.aclose()
            except BaseException:
                pass
        self._stack, self.session = None, None

    async def connect(self):
        await self.close()
        stack = AsyncExitStack()
        http = httpx2.AsyncClient(timeout=httpx2.Timeout(30.0, read=self.timeout + 30))
        await stack.enter_async_context(http)
        read, write = await stack.enter_async_context(streamable_http_client(self.url, http_client=http))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()
        self.tools = {t.name: (t.input_schema or {}) for t in listed.tools}
        self.action_tool = {}
        for name, schema in self.tools.items():
            for a in schema_actions(schema):
                self.action_tool.setdefault(a, name)
        self._stack, self.session = stack, session
        event('mcp.initialized', agent=self.agent, task_id=self.task_id, pilot=self.pilot_key,
              endpoint=self.url, tools=sorted(self.tools), actions_discovered=len(self.action_tool))

    async def login(self):
        password = password_for(self.cfg, self.pilot_key, self.username)
        data = await self._raw('spacemolt_auth', {'action': 'login', 'username': self.username, 'password': password},
                               log_args={'action': 'login', 'username': self.username})
        password = None
        sid = _find_key(data, 'session_id')
        self._session_id = sid if isinstance(sid, str) else None
        event('mcp.login', agent=self.agent, task_id=self.task_id, pilot=self.pilot_key,
              username=self.username, confirmed=True)
        return data

    def tool_for(self, action: str) -> str:
        if action in self.action_tool:
            return self.action_tool[action]
        tool = DEFAULT_TOOL.get(action, 'spacemolt')
        if self.tools and tool not in self.tools:
            raise InvalidParams('unknown_action', f'no discovered tool provides action {action!r}')
        return tool

    def has_action(self, action: str) -> bool:
        return action in self.action_tool or not self.action_tool

    def help_schema(self, action: str) -> dict:
        return self.tools.get(self.tool_for(action), {})

    async def call(self, action: str, *, mutation: bool | None = None, attempts: int = 6, **params) -> dict:
        if action in FORBIDDEN:
            raise ForbiddenAction('forbidden', f'{action} is disabled by MAS safety policy')
        tool = self.tool_for(action)
        args = {'action': action, **{k: v for k, v in params.items() if v is not None}}
        schema = self.tools.get(tool) or {}
        if self._session_id and 'session_id' in (schema.get('properties') or {}):
            args['session_id'] = self._session_id
        problems = validate_args(schema, args) if schema else []
        if problems:
            event('mcp.invalid_params', agent=self.agent, task_id=self.task_id, pilot=self.pilot_key,
                  tool=tool, action=action, args=sanitize(args), problems=problems)
            raise InvalidParams('invalid_params', '; '.join(problems))
        is_mut = action in MUTATIONS if mutation is None else mutation
        relogged = False
        for attempt in range(1, attempts + 1):
            try:
                return await self._raw(tool, args, mutation=is_mut)
            except GameError as e:
                if isinstance(e, UnknownOutcome):
                    raise
                if e.code == 'action_pending':
                    wait = 11
                elif e.code == 'in_transit':
                    wait = min(float(e.retry_after or 15) + 2, 120)
                elif e.code == 'rate_limited':
                    wait = min(float(e.retry_after or 10) + 1, 300)
                elif e.code in SESSION_CODES and not relogged:
                    event('recovery.relogin', agent=self.agent, task_id=self.task_id, pilot=self.pilot_key, reason=e.code)
                    await self.login()
                    relogged = True
                    continue
                else:
                    raise
                if attempt == attempts:
                    raise
                event('recovery.wait', agent=self.agent, task_id=self.task_id, pilot=self.pilot_key,
                      action=action, reason=e.code, wait_seconds=round(wait, 1), attempt=attempt)
                await asyncio.sleep(wait)
            except (httpx2.HTTPError, OSError, asyncio.TimeoutError, ConnectionError, RuntimeError) as e:
                event('mcp.transport_error', agent=self.agent, task_id=self.task_id, pilot=self.pilot_key,
                      action=action, error=f'{type(e).__name__}: {str(e)[:200]}', attempt=attempt)
                await asyncio.sleep(min(5 * attempt, 30))
                await self.connect()
                await self.login()
                if is_mut:
                    raise UnknownOutcome('unknown_outcome', f'{action} reply lost: re-observe before retry')
        raise GameError('retries_exhausted', action)

    async def _raw(self, tool: str, args: dict, *, mutation: bool = False, log_args: dict | None = None) -> dict:
        if self.session is None:
            await self.connect()
        safe = log_args or sanitize(args)
        t0 = time.monotonic()
        self.calls += 1
        try:
            res = await asyncio.wait_for(self.session.call_tool(tool, args, read_timeout_seconds=self.timeout),
                                         timeout=self.timeout + 15)
        except Exception as e:
            if isinstance(e, GameError):
                raise
            raise RuntimeError(f'MCP call failed: {type(e).__name__}: {e}') from e
        data, text = parse_tool_result(res)
        err = classify_error(res, data, text)
        ms = int((time.monotonic() - t0) * 1000)
        if err:
            event('mcp.error', agent=self.agent, task_id=self.task_id, pilot=self.pilot_key, tool=tool,
                  action=args.get('action'), args=safe, code=err.code, message=err.message[:200], ms=ms)
            raise err
        if mutation:
            self.mutations += 1
        event('mcp.result', agent=self.agent, task_id=self.task_id, pilot=self.pilot_key, tool=tool,
              action=args.get('action'), args=safe, mutation=mutation, ms=ms,
              confirmation=_summary(data, text))
        data.setdefault('_text', text[:2000])
        return data


def _find_key(obj, key, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key, depth + 1)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj[:50]:
            r = _find_key(v, key, depth + 1)
            if r is not None:
                return r
    return None


def _summary(data: dict, text: str) -> str:
    for k in ('message', 'result', 'status'):
        if isinstance(data.get(k), str):
            return data[k][:240]
    if text:
        return text[:240]
    return json.dumps({k: data[k] for k in list(data)[:8]}, default=str)[:240]
