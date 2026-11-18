# Alpha

Alpha measures the contribution of individual agent steps to completed work.

- Keep the experiment engine and transports in `alpha/`; npm and legacy Node entry points are launchers.
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

## CLI-first development and delivery

- Use the installed `alpha` command as the canonical public interface: `alpha orch` and `alpha experiment`. Do not pass input files to a Python interpreter as a substitute for an available CLI command.
- When adding a user-facing feature, expose it through the CLI and update README.md. Keep internal Python entry points for internal development and testing.
- For repository implementation requests, edit actual source files and use normal Git workflows. Do not hand off a one-off patch installer instead of implementing the requested change.
- Protect private context: never publish private conversations, external company/project names, or identifying details in public code, docs, commits, branches, or PRs.
- Before committing, run relevant tests, lint, type checks, and builds. Before pushing upstream, run the exact local GitHub Actions matrix commands for Python 3.14 and 3.15, plus `npm test` and `npm run demo`. Do not commit or push known failures. If any required local check cannot be run, do not push and report the blocker.
