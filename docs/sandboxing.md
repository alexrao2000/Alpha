# External agent sandboxing

Alpha can run external agent commands in optional Docker isolation through the `alpha` CLI. Built-in Codex calls are not sandboxed by this option. Operator-supplied commands without a policy retain their existing host-level permissions.

Use a policy JSON file:

```json
{
  "mode": "docker",
  "image": "your-registry/alpha-agent@sha256:REPLACE_WITH_64_HEX_CHARACTERS",
  "network": "none",
  "memory_mb": 512,
  "cpus": 1,
  "pids_limit": 128,
  "timeout_seconds": 300,
  "tmpfs_mb": 64,
  "max_output_kb": 1024
}
```

Replace the placeholder image digest with the actual SHA-256 digest of an image already present in Docker. The command must exist inside that image. Run `alpha orch run "task" --command "python -m my_agent" --sandbox-policy sandbox.json --db runs/task.sqlite`. Settings are stored with the run and reused on `alpha orch resume`. Docker must be available on every execution host. Alpha rejects new runs that specify a sandbox without an external command.

Default protections are: no network, read-only root filesystem, no Linux capabilities, no privilege escalation, non-root identity, and CPU/memory/process/time/output limits. A writable temporary directory is provided. No host workspace is mounted unless `workspace` is explicitly supplied as an absolute existing directory; the mount is read-only by default. Set `workspace_write: true` explicitly to allow writes. `network: bridge` permits container network access, and `runtime` selects a preconfigured Docker runtime.

This is an execution boundary, not a guarantee against malicious code, hostile Docker daemon access, or remote side effects. For adversarial code, choose hardened VM isolation such as a microVM, restrict egress, and avoid mounting secrets. Docker daemon credentials must never be exposed to the agent. Alpha cannot ensure exactly-once execution of third-party effects. Test the chosen image and runtime before using them in production.
