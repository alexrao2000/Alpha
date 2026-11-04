# The native fleet sends only its current conversation. Provider transport is
# Python throughout; the CLI's own tools and authentication files stay outside it.

import asyncio
import json
import sys

from .backends import CodexBackend
from .contracts import compact_json


async def respond(request):
    backend = CodexBackend(model=request.get("model"), binary=request.get("model_binary", "codex"))
    schema = {"type": "object", "properties": {"response": {"type": "string"}}, "required": ["response"], "additionalProperties": False}
    prompt = ("Provide the next assistant response for the conversation below. "
              "Keep the exact response format requested by its system message, including any ReAct markers. "
              "Do not inspect files, run commands, browse, or use your own tools. "
              "If the conversation calls for a tool, express that call in its requested text format; the host framework executes it. "
              "Return the assistant response text as the response JSON field.\n\n" + compact_json(request["messages"]))
    result = await backend.complete(schema=schema, prompt=prompt)
    if not isinstance(result["output"].get("response"), str):
        raise RuntimeError("The model transport returned an invalid response.")
    return {"response": result["output"]["response"], "usage": result["usage"], "model_calls": result["model_calls"]}


def main():
    try:
        print(compact_json(asyncio.run(respond(json.load(sys.stdin)))))
        return 0
    except Exception:
        print("The model transport failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
