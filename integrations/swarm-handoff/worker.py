# Execute the pinned handoff example's agents and functions with the pinned native
# runtime. Import-time demo execution is deferred; only model transport and task
# inputs change. No expected routing or answer is passed to this worker.

import contextlib
from copy import deepcopy
import json
from pathlib import Path
import runpy
import sys
from time import perf_counter
from types import SimpleNamespace

from alpha.sdk import complete, step

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'upstream'))
request = json.load(sys.stdin)
trace, tools = [], []
with contextlib.redirect_stdout(sys.stderr):
    import swarm
    from swarm.types import Response
    from openai.types.chat import ChatCompletion
    NativeSwarm = swarm.Swarm
    class Deferred:
        def run(self, **kwargs):
            return Response(messages=[{'role': 'assistant', 'content': 'Import-time demonstration deferred.'}])
    swarm.Swarm = lambda: Deferred()
    scope = runpy.run_path(str(HERE / 'upstream/examples/basic/agent_handoff.py'))
    swarm.Swarm = NativeSwarm

roles = {'English Agent': 'english', 'Spanish Agent': 'spanish'}
variant = request['variant']
english, spanish = scope['english_agent'], scope['spanish_agent']
if 'spanish' not in variant['roles']:
    english.functions = []
for agent in (english, spanish):
    role = roles[agent.name]
    if role in variant.get('prompt_overrides', {}):
        agent.instructions = variant['prompt_overrides'][role]

class Client:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)
        self.role = 'english'
    def create(self, **kwargs):
        started = perf_counter()
        messages = deepcopy(kwargs['messages'])
        schema = {'type': 'object', 'properties': {
            'content': {'type': 'string'}, 'tool_calls': {'type': 'array', 'items': {'type': 'object',
            'properties': {'name': {'type': 'string'}, 'arguments': {'type': 'string'}},
            'required': ['name', 'arguments'], 'additionalProperties': False}}},
            'required': ['content', 'tool_calls'], 'additionalProperties': False}
        prompt = json.dumps({'instruction': 'Supply the next assistant message for this native conversation. Preserve its system instructions. Use the supplied tool names and JSON argument strings when a handoff is needed. Do not use your own external tools.',
                             'messages': messages, 'tools': kwargs.get('tools')}, ensure_ascii=False)
        result = complete(request['gateway'], self.role, schema=schema, prompt=prompt)
        trace.append(step(self.role, messages, result, (perf_counter() - started) * 1000, len(trace)))
        output = result['output']
        calls = [{'id': f'call-{len(trace)}-{index}', 'type': 'function', 'function': call} for index, call in enumerate(output['tool_calls'])]
        return ChatCompletion(id='alpha-transport', created=0, model=kwargs['model'], object='chat.completion',
                              choices=[{'index': 0, 'finish_reason': 'tool_calls' if calls else 'stop',
                                        'message': {'role': 'assistant', 'content': output['content'], 'tool_calls': calls or None}}])

client = Client()
class RecordingSwarm(NativeSwarm):
    def get_chat_completion(self, agent, *args, **kwargs):
        client.role = roles[agent.name]
        return super().get_chat_completion(agent, *args, **kwargs)
    def handle_tool_calls(self, calls, *args, **kwargs):
        result = super().handle_tool_calls(calls, *args, **kwargs)
        for call in calls:
            tools.append({'tool': call.function.name, 'arguments': json.loads(call.function.arguments),
                          'parent_ids': [trace[-1]['id']], 'result': result.agent.name if result.agent else None, 'chargeable': False})
        return result

with contextlib.redirect_stdout(sys.stderr):
    result = RecordingSwarm(client=client).run(agent=english, messages=[{'role': 'user', 'content': request['input']['message']}], max_turns=12)
artifact = result.messages[-1].get('content') or ''
print(json.dumps({'artifact': artifact, 'trace': trace, 'tools': tools, 'final_state': {'active_agent': result.agent.name}}))
