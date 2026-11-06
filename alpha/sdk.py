# Small synchronous client for trusted native workflow adapters. Only the active
# conversation, schema, and role cross the private model gateway; expected answers
# are host-side grader data and must never be added to these requests.

from __future__ import annotations

import json
import socket
from time import perf_counter

from .accounting import BudgetStop
from .contracts import JSON, compact_json


RESPONSE_SCHEMA = {'type': 'object', 'properties': {'response': {'type': 'string'}},
                   'required': ['response'], 'additionalProperties': False}


def complete(gateway: JSON, role: str, *, schema: JSON, prompt: str) -> JSON:
    if not gateway:
        raise ValueError('This adapter requires an Alpha model gateway.')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(900)
        connection.connect(gateway['socket'])
        connection.sendall((compact_json({'lease': gateway['lease'], 'role': role, 'schema': schema, 'prompt': prompt}) + '\n').encode())
        with connection.makefile('rb') as stream:
            line = stream.readline(16 * 1024 * 1024)
        reply = json.loads(line)
    if 'error' in reply:
        if reply['error']['kind'] == 'budget':
            raise BudgetStop(reply['error']['message'])
        raise RuntimeError(reply['error']['message'])
    return reply['result']


def conversation(gateway: JSON, role: str, messages: list[JSON]) -> JSON:
    prompt = ('Provide the next assistant response for the conversation below. Preserve the format requested by its system message, including ReAct markers. '
              'Do not use your own shell, browsing, file, or MCP tools. Express requested tool calls as text for the host runtime. '
              'Return the assistant text in the response field.\n\n' + compact_json(messages))
    result = complete(gateway, role, schema=RESPONSE_SCHEMA, prompt=prompt)
    result['response'] = result['output']['response']
    return result


def step(role: str, messages: list[JSON], result: JSON, elapsed_ms: float, index: int) -> JSON:
    return {'id': f'step-{index}', 'parent_ids': [f'step-{index - 1}'] if index else [], 'role': role,
            'context': {'messages': messages}, 'output': result['output'], 'elapsed_ms': elapsed_ms,
            'usage': result.get('usage'), 'model_calls': result.get('model_calls', 1), 'attempt_id': result.get('attempt_id'),
            'execution': 'model'}
