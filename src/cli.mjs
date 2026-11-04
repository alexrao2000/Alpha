/* Compatibility entry point for existing commands. Python owns execution and reports. */

import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { delimiter } from 'node:path';

const root = fileURLToPath(new URL('../', import.meta.url));
const child = spawn(process.env.ALPHA_CORE_PYTHON ?? 'python3', ['-m', 'alpha', ...process.argv.slice(2)], {
  stdio: 'inherit',
  env: { ...process.env, PYTHONPATH: [root, process.env.PYTHONPATH].filter(Boolean).join(delimiter) },
});
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
child.on('error', () => { console.error('Alpha: Cannot start Python 3.11+. Set ALPHA_CORE_PYTHON.'); process.exitCode = 1; });
child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 130 : 1); });
