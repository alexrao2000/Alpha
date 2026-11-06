# Keep the pinned two-crew native Flow, prompts, tool, retry limit, and routing.
# Adapt the hardcoded topic into a visible task input and inject model transport.
# The old crewai_tools.BaseTool import aliases the native BaseTool for compatibility.
# Candidate-only structural checks replace or gate the original review call.

import contextlib
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
from time import perf_counter
import types

from alpha.sdk import conversation, step

HERE = Path(__file__).resolve().parent
PACKAGE = HERE / 'upstream/flows/self_evaluation_loop_flow/src'
sys.path.insert(0, str(PACKAGE))
request = json.load(sys.stdin)
trace, tools = [], []
with contextlib.redirect_stdout(sys.stderr):
    from crewai import Agent, BaseLLM
    from crewai.tools import BaseTool
    compatibility = types.ModuleType('crewai_tools')
    compatibility.BaseTool = BaseTool
    sys.modules['crewai_tools'] = compatibility
    flow_module = importlib.import_module('self_evaluation_loop_flow.main')
    writer_module = importlib.import_module('self_evaluation_loop_flow.crews.shakespeare_crew.shakespeare_crew')
    reviewer_module = importlib.import_module('self_evaluation_loop_flow.crews.x_post_review_crew.x_post_review_crew')
    counter_module = importlib.import_module('self_evaluation_loop_flow.tools.CharacterCounterTool')

variant = request['variant']
role_names = {writer_module.ShakespeareanXPostCrew().agents_config['shakespearean_bard']['role'].strip(): 'shakespearean_bard',
              reviewer_module.XPostReviewCrew().agents_config['x_post_verifier']['role'].strip(): 'x_post_verifier'}
class RecordingLLM(BaseLLM):
    def __init__(self, role):
        super().__init__(model=variant.get('models', {}).get(role) or request.get('model') or 'cli-default', temperature=0)
        self.alpha_role = role
    def supports_function_calling(self):
        return False
    def supports_stop_words(self):
        return False
    def call(self, messages, **kwargs):
        snapshot = deepcopy(messages)
        started = perf_counter()
        result = conversation(request['gateway'], self.alpha_role, snapshot)
        trace.append(step(self.alpha_role, snapshot, result, (perf_counter() - started) * 1000, len(trace)))
        return result['response']

def agent(*args, **kwargs):
    role = role_names[kwargs['config']['role'].strip()]
    if role in variant.get('prompt_overrides', {}):
        kwargs['config'] = {**kwargs['config'], 'goal': variant['prompt_overrides'][role]}
    return Agent(*args, llm=RecordingLLM(role), **kwargs)
writer_module.Agent = reviewer_module.Agent = agent
original_count = counter_module.CharacterCounterTool._run

def counted(self, text):
    result = original_count(self, text)
    tools.append({'tool': 'Character Counter Tool', 'arguments': {'text': text}, 'result': result,
                  'parent_ids': [trace[-1]['id']] if trace else [], 'chargeable': False})
    return result
counter_module.CharacterCounterTool._run = counted
NativeWriter = flow_module.ShakespeareanXPostCrew
NativeReviewer = flow_module.XPostReviewCrew

class Writer:
    def crew(self):
        native = NativeWriter().crew()
        class Kickoff:
            def kickoff(self, inputs):
                return native.kickoff(inputs={**inputs, 'topic': request['input']['topic']})
        return Kickoff()


def structural(post):
    return 200 <= len(post) <= 280 and len(post.splitlines()) == 5 and all(ord(c) < 0x1F000 and not 0x2600 <= ord(c) <= 0x27BF for c in post)

class Reviewer:
    def crew(self):
        class Kickoff:
            def kickoff(self, inputs):
                deterministic = 'x_post_verifier' in variant.get('deterministic', {})
                skip = 'x_post_verifier' in variant.get('conditional', {}) and structural(inputs['x_post'])
                if deterministic or skip:
                    result = {'valid': structural(inputs['x_post']), 'feedback': None if structural(inputs['x_post']) else 'Use five lines, 200–280 characters, and no emojis.'}
                    trace.append({'id': f'step-{len(trace)}', 'parent_ids': [trace[-1]['id']] if trace else [], 'role': 'x_post_verifier',
                                  'context': {'post': inputs['x_post']}, 'output': result, 'elapsed_ms': 0,
                                  'usage': None, 'model_calls': 0, 'execution': 'deterministic' if deterministic else 'condition_skipped'})
                    return result
                return NativeReviewer().crew().kickoff(inputs=inputs)
        return Kickoff()
flow_module.ShakespeareanXPostCrew = Writer
flow_module.XPostReviewCrew = Reviewer
with contextlib.redirect_stdout(sys.stderr):
    flow = flow_module.ShakespeareXPostFlow()
    flow.kickoff()
print(json.dumps({'artifact': flow.state.x_post, 'trace': trace, 'tools': tools,
                  'final_state': {'valid': flow.state.valid, 'retry_count': flow.state.retry_count}}))
