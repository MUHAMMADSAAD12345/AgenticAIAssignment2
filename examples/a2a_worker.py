"""Minimal local A2A worker using official A2A Python SDK, protocol v1 routes.
Use as an interoperability smoke test; students must add real business logic.
Start: python examples/a2a_worker.py --role mining --port 9999
"""
import argparse,asyncio,json
import uvicorn
from starlette.applications import Starlette
from a2a.server.agent_execution import AgentExecutor,RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes,create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore,TaskUpdater
from a2a.types import AgentCapabilities,AgentCard,AgentInterface,AgentSkill,TaskState
from a2a.helpers import get_message_text, new_task_from_user_message
from a2a.helpers import new_text_message,new_text_part

class Worker(AgentExecutor):
    def __init__(self,role): self.role=role
    async def execute(self,context:RequestContext,event_queue:EventQueue)->None:
        task=context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None: await event_queue.enqueue_event(task)
        updater=TaskUpdater(event_queue=event_queue,task_id=task.id,context_id=task.context_id)
        await updater.update_status(state=TaskState.TASK_STATE_WORKING,message=new_text_message('Working on request'))
        request=get_message_text(context.message) or ''
        # Demo only: preserve input, no game actions, no deceptive simulated success.
        result=json.dumps({'role':self.role,'task_request':request,'status':'acknowledged_only',
                           'next_step':'implement validated game-task logic'},ensure_ascii=False)
        await updater.add_artifact(parts=[new_text_part(text=result,media_type='text/plain')])
        await updater.update_status(state=TaskState.TASK_STATE_COMPLETED,message=new_text_message('Demo acknowledgement finished'))
    async def cancel(self,context:RequestContext,event_queue:EventQueue)->None:
        # This demonstration has no long-running operation to cancel.
        return

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--role',default='mining');parser.add_argument('--port',type=int,default=9999);args=parser.parse_args()
    card=AgentCard(name=f'FAST {args.role.title()} Worker',description=f'Demonstration {args.role} A2A worker',version='0.1.0',
                   default_input_modes=['text/plain'],default_output_modes=['text/plain'],
                   capabilities=AgentCapabilities(streaming=False),
                   supported_interfaces=[AgentInterface(protocol_binding='JSONRPC',url=f'http://127.0.0.1:{args.port}',protocol_version='1.0')],
                   skills=[AgentSkill(id=f'{args.role}_task',name=f'{args.role.title()} Task',description='Acknowledges an assigned task',input_modes=['text/plain'],output_modes=['text/plain'],tags=['mas','training'],examples=['inspect current task'])])
    handler=DefaultRequestHandler(agent_executor=Worker(args.role),task_store=InMemoryTaskStore(),agent_card=card)
    routes=[*create_agent_card_routes(card),*create_jsonrpc_routes(handler,'/')]
    print(f'A2A {args.role} at http://127.0.0.1:{args.port}/.well-known/agent-card.json')
    uvicorn.run(Starlette(routes=routes),host='127.0.0.1',port=args.port)
if __name__=='__main__':main()
