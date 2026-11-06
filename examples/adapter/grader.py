# Private expected text is consumed only after the workflow has exited.

import json
import sys

request = json.load(sys.stdin)
passed = request['artifact'] == request['expected'] and request['final_state']['counter'] == 1
print(json.dumps({'passed': passed, 'unsafe': False, 'checks': [{'name': 'Output and fresh state', 'passed': passed}]}))
