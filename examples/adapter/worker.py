# Scripted protocol example: independent normalization and formatting stages.
# Each invocation starts from initial files; private grader expectations are absent.

import json
from pathlib import Path
import sys

request = json.load(sys.stdin)
assert 'expected' not in request and 'checks' not in request
original = Path('counter.txt').read_text()
assert original == '0'
Path('counter.txt').write_text('1')
text = request['input']['text']
trace = []
for role in request['variant']['roles']:
    before = text
    text = ' '.join(text.split()).lower() if role == 'normalize' else '[' + text + ']'
    trace.append({'id': role, 'parent_ids': [trace[-1]['id']] if trace else [], 'role': role,
                  'context': {'text': before}, 'output': {'text': text}, 'elapsed_ms': 0,
                  'usage': None, 'model_calls': 0, 'execution': 'scripted'})
print(json.dumps({'artifact': text, 'trace': trace, 'tools': [], 'final_state': {'counter': 1}}))
