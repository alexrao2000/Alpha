# Alpha

Alpha measures the contribution of individual agent steps to completed work.

- Keep the built-in fleet in `agent/subagents/` and native external adapters in `integrations/`.
- Preserve pinned upstream bytes and verify their hashes before external runs.
- Keep expected outcomes out of agent messages, role context, and model responses.
- Execute each task and variant against a fresh copy of its initial world.
- Reexecute downstream roles after a change. Do not edit recorded traces to score variants.
- Select candidates on discovery cases; freeze the choice before opening holdout cases.
- Label fixture results as scripted. Never present fixture counts as model tokens or API cost.
- Treat the bundled tasks as a development suite, not an independent benchmark.
- Use `npm test` and `npm run demo` to verify changes.
- Never commit credentials, local authentication, or raw provider diagnostics.
- Stage explicit paths and commit completed work.
