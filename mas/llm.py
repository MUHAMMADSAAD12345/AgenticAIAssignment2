"""Budgeted LLM planner (Groq by default). Shares logs/budget.sqlite3 with scripts/budget.py."""
from __future__ import annotations
import asyncio
import json
import os
import re
import time

from scripts.budget import CAP_REQUESTS, CAP_TOKENS, MAX_PER_60_SECONDS, complete, connection, reserve, usage
from mas.config import LOGS
from mas.log import event

BUDGET_DB = str(LOGS / 'budget.sqlite3')

SYSTEM_PROMPT = """You are the strategic coordinator of a two-pilot SpaceMolt mining/trading company.
Goal (combined over both pilots): credits_earned >= 2000, ore_mined >= 30, at least 1 genuine exchange sale,
systems_explored >= 2, while keeping credits_spent <= 1500 and distance_traveled <= 1500.
Mission rewards are the main income; raw ore sells for ~5-45 cr/unit. Avoid combat, avoid wasting fuel/distance.
Each cycle assign AT MOST ONE task per pilot from these skills:
- mine_cycle {max_mining_actions:int, belt_poi_id?:str, target_item?:str, return_to_base:true}
- accept_mission {mission_id:str}   (must be an id from that pilot's candidate_missions, pilot must be docked)
- sell_and_complete {keep_items?:[str], min_price?:int}  (dock, complete satisfied missions, sell surplus)
- list_item {item_id:str, quantity:int, price_each:int}  (only for market-participation missions)
- explore_adjacent {max_distance:number, return_home:true}  (only if systems_explored target unmet)
- relocate {target_system:str}  (one jump to an ADJACENT system from that pilot's connections, then dock there)
- wait {}
Belts: a pilot can only mine where belts_ok is non-empty. If belts_ok is empty, mining there is impossible:
follow the other pilot to a system whose system_yield > 0, or (if no system is proven) send ONE pilot to scout
the nearest neighbour not in exhausted_systems while the other waits. Each jump adds its distance to
distance_traveled, so stay within allowances.distance_left. accept_mission needs mission_slots_free > 0.
Selling: if sale_value_now >= 50, sell_and_complete earns those credits now (station/player buy orders).
If market_mission_units_needed > 0 and nothing sells instantly, list_item surplus just under the lowest ask.
Use only fresh facts given. Reply with ONLY a JSON object:
{"rationale":"<=40 words","assignments":[{"pilot":"A","skill":"...","params":{...}}]}"""


class Planner:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.model = cfg['llm']['model']
        self.enabled = bool(os.environ.get('GROQ_API_KEY')) and cfg['llm'].get('provider') == 'groq'
        self.last_error = None

    def budget(self) -> dict:
        db = connection(BUDGET_DB)
        try:
            u = usage(db)
        finally:
            db.close()
        u['requests_left'] = CAP_REQUESTS - u['requests']
        u['tokens_left'] = CAP_TOKENS - u['tokens']
        return u

    def can_plan(self) -> tuple[bool, str]:
        if not self.enabled:
            return False, 'no GROQ_API_KEY set'
        b = self.budget()
        if b['requests_left'] <= self.cfg['llm']['reserve_requests'] or b['tokens_left'] <= self.cfg['llm']['reserve_tokens']:
            return False, f'LLM budget reserve reached ({b["requests_left"]} req, {b["tokens_left"]} tok left)'
        return True, 'ok'

    async def _rate_slot(self):
        for _ in range(40):
            if self.budget()['requests_last_60s'] < MAX_PER_60_SECONDS:
                return
            await asyncio.sleep(3)
        raise RuntimeError('rolling 60s LLM rate window did not clear')

    async def plan(self, context: dict, task_id: str) -> dict | None:
        ok, why = self.can_plan()
        if not ok:
            event('llm.skipped', agent='coordinator', task_id=task_id, reason=why)
            return None
        await self._rate_slot()
        try:
            rid = reserve(self.model, BUDGET_DB)
        except RuntimeError as e:
            event('llm.skipped', agent='coordinator', task_id=task_id, reason=str(e)[:200])
            return None
        prompt = json.dumps(context, separators=(',', ':'), default=str)
        t0 = time.time()
        try:
            resp = await asyncio.to_thread(self._call, prompt)
            u = resp.usage
            tin, tout = getattr(u, 'prompt_tokens', 0) or 0, getattr(u, 'completion_tokens', 0) or 0
            complete(rid, tin, tout, ok=True, db_path=BUDGET_DB)
            text = resp.choices[0].message.content or ''
            plan = _extract_json(text)
            event('llm.plan', agent='coordinator', task_id=task_id, model=self.model, in_toks=tin, out_toks=tout,
                  seconds=round(time.time() - t0, 1), rationale=(plan or {}).get('rationale'),
                  assignments=(plan or {}).get('assignments'), parse_ok=plan is not None)
            return plan
        except Exception as e:
            complete(rid, 0, 0, ok=False, db_path=BUDGET_DB)
            self.last_error = f'{type(e).__name__}: {str(e)[:200]}'
            event('llm.error', agent='coordinator', task_id=task_id, error=self.last_error)
            return None

    def _call(self, prompt: str):
        from groq import Groq
        client = Groq(api_key=os.environ['GROQ_API_KEY'], timeout=60, max_retries=1)
        kwargs = dict(model=self.model, temperature=0.2, max_completion_tokens=self.cfg['llm']['max_completion_tokens'],
                      messages=[{'role': 'system', 'content': SYSTEM_PROMPT}, {'role': 'user', 'content': prompt}],
                      response_format={'type': 'json_object'})
        if 'gpt-oss' in self.model:
            kwargs['reasoning_effort'] = 'low'
        return client.chat.completions.create(**kwargs)


def _extract_json(text: str):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r'\{.*\}', text, re.S)
        if m:
            try:
                return json.loads(m.group())
            except ValueError:
                return None
    return None
