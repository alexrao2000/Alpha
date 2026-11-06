# Grade routing and the explicitly requested response. Oracles are read only here,
# after the native workflow and its children have exited.

import json
import sys

request = json.load(sys.stdin)
expected = request['expected']
checks = [{'name': 'Native handoff target', 'passed': request['final_state'].get('active_agent') == expected['agent']},
          {'name': 'Requested response', 'passed': request['artifact'].strip() == expected['text']}]
print(json.dumps({'passed': all(c['passed'] for c in checks), 'unsafe': False, 'checks': checks}))
