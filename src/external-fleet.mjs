/* The adapter verifies upstream blob identities before running the original
   CrewAI class. Task inputs and private executable checks cross separate paths. */

import { readFile, access } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { performance } from 'node:perf_hooks';

const home = new URL('../integrations/crewai-game-builder/', import.meta.url);

export async function loadExternalFleet() {
  const provenance = JSON.parse(await readFile(new URL('provenance.json', home), 'utf8'));
  for (const file of provenance.files) {
    const bytes = await readFile(new URL(`upstream/${file.path}`, home));
    const hash = createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex');
    if (hash !== file.git_blob_sha) throw new Error(`Upstream source changed: ${file.path}`);
  }
  const original = ['senior_engineer_agent', 'qa_engineer_agent', 'chief_qa_engineer_agent'];
  const roles = JSON.parse(await readFile(new URL('fleet.config.json', home), 'utf8'));
  if (!Array.isArray(roles) || !roles.includes(original[0]) || new Set(roles).size !== roles.length ||
      roles.some((r, i) => !original.includes(r) || (i > 0 && original.indexOf(r) < original.indexOf(roles[i - 1])))) {
    throw new Error('Invalid external fleet configuration. Keep the senior engineer and preserve upstream order.');
  }
  const variants = [{ id: 'baseline', label: roles.length === 3 ? 'Upstream full crew' : 'Configured crew', roles }, ...roles.slice(1).map(r => ({
    id: `without-${r}`, label: r === 'qa_engineer_agent' ? 'Remove QA engineer' : 'Remove chief QA', roles: roles.filter(x => x !== r),
  }))];
  const cases = JSON.parse(await readFile(new URL('cases.json', home), 'utf8'));
  const prompts = {};
  for (const path of ['src/game_builder_crew/config/agents.yaml', 'src/game_builder_crew/config/tasks.yaml', 'src/game_builder_crew/crew.py']) {
    prompts[path] = await readFile(new URL(`upstream/${path}`, home), 'utf8');
  }
  const adapter = createHash('sha256');
  for (const path of [new URL('worker.py', home), new URL('grade.py', home), new URL('./external-fleet.mjs', import.meta.url), new URL('./model-bridge.mjs', import.meta.url)]) {
    adapter.update(await readFile(path));
  }
  return { cases, variants, prompts, fleet: {
    id: 'crewai-game-builder', name: 'CrewAI Game Builder', provenance,
    description: 'Pinned upstream three-agent software-building crew, executed by its native CrewAI runtime.',
    dataset_description: 'Alpha-authored headless game programming tasks, with different games in discovery and holdout.',
    config_path: 'integrations/crewai-game-builder/fleet.config.json',
    adapter_sha256: adapter.digest('hex'),
  } };
}

export async function runExternalCase({ testCase, variant, backend, trial, signal }) {
  const python = process.env.ALPHA_PYTHON ?? fileURLToPath(new URL('../work/crewai-venv/bin/python', import.meta.url));
  await access(python).catch(() => { throw new Error('Install the external fleet runtime; see integrations/crewai-game-builder/README.md.'); });
  const started = performance.now();
  let native = { trace: [], artifact: '', tasks: [], error: null }, verdict;
  try {
    native = await runPython(python, new URL('worker.py', home), {
      roles: variant.roles, input: testCase.input, model: backend.requestedModel,
      node: process.execPath, max_calls: 12,
    }, signal, 900000);
    if (native.error) throw new Error(native.error);
    verdict = await runPython(python, new URL('grade.py', home), { code: native.artifact, checks: testCase.checks }, signal, 15000);
  } catch (error) {
    native.error = error.message;
    verdict = { passed: false, unsafe: false, checks: [{ name: 'Workflow execution', passed: false }], results: [] };
  }
  return { case_id: testCase.id, category: testCase.category, split: testCase.split, variant: variant.id, trial,
    trace: native.trace, tools: native.tasks.map(t => ({ tool: 'upstream_task', arguments: { agent: t.agent }, result: t.raw })),
    final_state: { artifact: native.artifact, test_results: verdict.results }, verdict,
    error: native.error ?? verdict.execution_error ?? null, elapsed_ms: performance.now() - started };
}

function runPython(python, script, data, signal, timeout) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, [fileURLToPath(script)], {
      stdio: ['pipe', 'pipe', 'pipe'], detached: true,
      env: { ...process.env, OTEL_SDK_DISABLED: 'true', CREWAI_TELEMETRY_ENABLED: 'false', LITELLM_LOCAL_MODEL_COST_MAP: 'True' },
    });
    let stdout = '', stderr = '';
    const stop = () => { try { process.kill(-child.pid, 'SIGTERM'); } catch {} };
    if (signal?.aborted) stop();
    signal?.addEventListener('abort', stop, { once: true });
    const timer = setTimeout(stop, timeout); timer.unref();
    child.stdout.on('data', chunk => { stdout += chunk; });
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-6000); });
    child.on('error', error => { clearTimeout(timer); signal?.removeEventListener('abort', stop); reject(error); });
    child.on('close', code => {
      clearTimeout(timer);
      signal?.removeEventListener('abort', stop);
      if (code !== 0) return reject(new Error(`External runtime exited (${code}). ${stderr.slice(-400)}`));
      try { resolve(JSON.parse(stdout)); } catch { reject(new Error('External runtime returned invalid JSON.')); }
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(data));
  });
}
