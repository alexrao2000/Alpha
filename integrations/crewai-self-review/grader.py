# This executable rubric measures format, topic retention, and a simple style
# marker. It does not claim to measure literary quality or audience preference.
# Private expected values are never sent to either native crew.

import json
import sys

request = json.load(sys.stdin)
post = request['artifact']
expected = request['expected']
checks = [{'name': 'Length', 'passed': 200 <= len(post) <= 280},
          {'name': 'Five-line structure', 'passed': len(post.splitlines()) == 5},
          {'name': 'No emojis', 'passed': all(ord(c) < 0x1F000 and not 0x2600 <= ord(c) <= 0x27BF for c in post)},
          {'name': 'Topic retained', 'passed': expected['topic'].casefold() in post.casefold()},
          {'name': 'Style marker', 'passed': any(word in post.casefold() for word in ('thou', 'thy', 'doth', 'hath'))}]
print(json.dumps({'passed': all(c['passed'] for c in checks), 'unsafe': False, 'checks': checks}))
