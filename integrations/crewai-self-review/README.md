# Native writing and review loop

This adapter runs [the published self-evaluation Flow](https://github.com/crewAIInc/crewAI-examples/tree/da94a91e691e1cf5b3151416bb15b5b62729bea8/flows/self_evaluation_loop_flow). The native writer crew, reviewer crew, character counter, router, and bounded retry loop remain in control. Upstream files are byte-identical; [provenance.json](provenance.json) records their identities.

```sh
npm run setup:external
python3 -m alpha experiment --fleet crewai-self-review --model YOUR_MODEL --trials 2 --max-calls 80
```

Alpha injects its CLI model transport, replaces the original hardcoded topic with each visible task topic, and aliases the old BaseTool import to the installed runtime's BaseTool. Logs and saved posts stay in the fresh workflow directory. Baseline prompts and retry decisions remain native. These adaptations are part of the fingerprint; results concern this adapted transport, not an unmodified provider deployment.

The baseline uses the original model reviewer. Two candidate recipes replace that call with structural checks or invoke it only for malformed posts. Structural checks use length, line count, and a conservative emoji range filter. They deliberately do not claim literary judgment. The separate private grader also checks topic retention and a simple Shakespearean word marker. Its emoji range heuristic does not implement a complete Unicode emoji parser. Four discovery topics and four different holdout topics are a small authored development suite. Passing this rubric does not establish humor, Shakespearean quality, audience preference, or safety outside the task. No redundant upstream role was added.

See the [adapter protocol](../../docs/adapters.md) and [validation policy](../../docs/experiments.md). Native dependencies use the same pinned environment as the existing game-building adapter.
