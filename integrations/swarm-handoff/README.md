# Native language handoff

This adapter executes [the original handoff example](https://github.com/openai/swarm/blob/6af0b4caf37dca4526dfd98e9fbd8ce36e7eeb22/examples/basic/agent_handoff.py) and its pinned native runtime, including the English agent, Spanish agent, and original transfer function. The upstream source is byte-identical; [provenance.json](provenance.json) lists SHA-256 identities. The original MIT [license](upstream/LICENSE) is retained.

```sh
npm run setup:external
python3 -m alpha experiment --fleet swarm-handoff --model YOUR_MODEL --trials 2 --max-calls 50
```

The runtime source is vendored. The shared native environment supplies its OpenAI and Pydantic types, but model requests use Alpha's existing CLI gateway. No separate provider API key is needed. The adapter defers the sample's import-time demo, injects visible task messages, converts structured model replies to native completion objects, and records actual native function dispatch. These are declared transport changes; the original runtime still performs routing and handoffs. Removing the Spanish role removes the corresponding handoff function.

Four discovery and four holdout messages test literal output and the final active agent. Grading is separate from the workflow and expected routing never enters model context. This narrow functional rubric does not measure general multilingual fluency, routing robustness, or an independent benchmark. The negative removal is not expected to save work safely on Spanish requests. No redundant role was planted. Scripted transport tests exercise actual transfer calls but establish no model-quality result.

See the [adapter protocol](../../docs/adapters.md) and [validation policy](../../docs/experiments.md).
