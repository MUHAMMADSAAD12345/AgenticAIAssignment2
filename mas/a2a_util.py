"""A2A v1 helpers: a JSON task worker server and a client that hands off tasks and reads results."""
from __future__ import annotations
import asyncio
import json
import time
import traceback
from typing import Awaitable, Callable

import httpx
import uvicorn
from starlette.applications import Starlette
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import get_message_text, new_task_from_user_message, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (AgentCapabilities, AgentCard, AgentInterface, AgentSkill, Role,
                       SendMessageRequest, TaskState)

from mas.log import event

Handler = Callable[[dict, Callable[[str], Awaitable[None]], str], Awaitable[dict]]

TERMINAL = {TaskState.TASK_STATE_COMPLETED: 'completed', TaskState.TASK_STATE_FAILED: 'failed',
            TaskState.TASK_STATE_CANCELED: 'canceled', TaskState.TASK_STATE_REJECTED: 'rejected'}


class JsonTaskExecutor(AgentExecutor):
    """Receives {"mission_id","skill","pilot","params"} JSON and returns a JSON result artifact."""

    def __init__(self, agent: str, handler: Handler):
        self.agent, self.handler = agent, handler

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        raw = get_message_text(context.message) or ''
        try:
            payload = json.loads(raw)
            assert isinstance(payload, dict) and payload.get('mission_id') and payload.get('skill')
        except Exception:
            await updater.add_artifact(parts=[new_text_part(text=json.dumps({'ok': False, 'error': 'bad_request',
                                       'detail': 'expected JSON with mission_id and skill'}), media_type='application/json')])
            await updater.update_status(state=TaskState.TASK_STATE_REJECTED, message=new_text_message('rejected: bad request'))
            return
        event('a2a.task_received', agent=self.agent, task_id=payload['mission_id'], a2a_task_id=task.id,
              skill=payload['skill'], pilot=payload.get('pilot'), issuer=payload.get('issuer'))
        await updater.update_status(state=TaskState.TASK_STATE_WORKING,
                                    message=new_text_message(f'{self.agent} working on {payload["skill"]}'))

        async def progress(text: str):
            await updater.update_status(state=TaskState.TASK_STATE_WORKING, message=new_text_message(text[:500]))

        try:
            result = await self.handler(payload, progress, task.id)
            ok = bool(result.get('ok'))
        except Exception as e:
            result = {'ok': False, 'error': f'{type(e).__name__}: {str(e)[:300]}'}
            ok = False
            event('agent.exception', agent=self.agent, task_id=payload['mission_id'], a2a_task_id=task.id,
                  error=result['error'], trace=traceback.format_exc()[-1200:])
        await updater.add_artifact(parts=[new_text_part(text=json.dumps(result, default=str), media_type='application/json')],
                                   name='result')
        state = TaskState.TASK_STATE_COMPLETED if ok else TaskState.TASK_STATE_FAILED
        await updater.update_status(state=state, message=new_text_message('done' if ok else f'failed: {result.get("error")}'))
        event('a2a.task_finished', agent=self.agent, task_id=payload['mission_id'], a2a_task_id=task.id,
              ok=ok, summary=result.get('summary'))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return


def serve(agent: str, description: str, port: int, skills: list[tuple[str, str]], handler: Handler, host='127.0.0.1'):
    url = f'http://{host}:{port}'
    card = AgentCard(
        name=f'SpaceMolt {agent.title()} Agent', description=description, version='1.0.0',
        default_input_modes=['application/json'], default_output_modes=['application/json'],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(protocol_binding='JSONRPC', url=url, protocol_version='1.0')],
        skills=[AgentSkill(id=sid, name=sid.replace('_', ' ').title(), description=desc,
                           input_modes=['application/json'], output_modes=['application/json'],
                           tags=['spacemolt', agent], examples=[json.dumps({'mission_id': 'MenInBlack-c1-A-' + sid, 'skill': sid,
                                                                            'pilot': 'A', 'params': {}})])
                for sid, desc in skills])
    h = DefaultRequestHandler(agent_executor=JsonTaskExecutor(agent, handler), task_store=InMemoryTaskStore(), agent_card=card)
    routes = [*create_agent_card_routes(card), *create_jsonrpc_routes(h, '/')]
    event('a2a.worker_started', agent=agent, url=url, card=f'{url}/.well-known/agent-card.json',
          skills=[s for s, _ in skills])
    uvicorn.run(Starlette(routes=routes), host=host, port=port, log_level='warning')


async def discover(url: str, timeout: float = 5.0):
    async with httpx.AsyncClient(timeout=timeout) as http:
        return await A2ACardResolver(httpx_client=http, base_url=url).get_agent_card()


async def send_task(url: str, payload: dict, timeout: float) -> dict:
    """Hand a task to a worker; returns {'a2a_task_id','state','result','seconds'}. Raises on transport failure."""
    t0 = time.time()
    card = await discover(url)
    http = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))
    client = await create_client(agent=card, client_config=ClientConfig(streaming=False, httpx_client=http))
    out = {'a2a_task_id': None, 'state': 'unknown', 'result': None, 'agent_card': card.name}
    try:
        msg = new_text_message(json.dumps(payload), role=Role.ROLE_USER)
        async for resp in client.send_message(SendMessageRequest(message=msg)):
            if resp.WhichOneof('payload') != 'task':
                continue
            t = resp.task
            out['a2a_task_id'] = t.id
            out['state'] = TERMINAL.get(t.status.state, str(t.status.state))
            for art in t.artifacts:
                for part in art.parts:
                    if part.text:
                        try:
                            out['result'] = json.loads(part.text)
                        except ValueError:
                            out['result'] = {'ok': False, 'raw': part.text[:500]}
    finally:
        try:
            await client.close()
        finally:
            await http.aclose()
    out['seconds'] = round(time.time() - t0, 1)
    return out


async def wait_for_worker(url: str, agent: str, max_wait: float = 300) -> bool:
    t0 = time.time()
    delay = 2
    while time.time() - t0 < max_wait:
        try:
            await discover(url, timeout=3)
            return True
        except Exception:
            event('worker.missing', agent='coordinator', worker=agent, url=url, retry_in=delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
    return False
