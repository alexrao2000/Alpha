/* The native runtime has live evidence; these tests cover Alpha's transport,
   provenance, and experiment interfaces without making provider requests. */

import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, chmod, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadExternalFleet } from '../src/external-fleet.mjs';
import { CodexBackend } from '../src/backends.mjs';
import { experiment } from '../src/engine.mjs';
import { renderReport } from '../src/report.mjs';

test('external source is pinned and different games are held out', async () => {
  const f = await loadExternalFleet();
  assert.match(f.fleet.provenance.commit, /^[a-f0-9]{40}$/);
  assert.equal(f.variants[0].roles.length, 3);
  assert.equal(f.fleet.provenance.runtime.version, '0.152.0');
  const discovery = new Set(f.cases.filter(c => c.split === 'discovery').map(c => c.category));
  assert(f.cases.filter(c => c.split === 'holdout').every(c => !discovery.has(c.category)));
});

test('generic model transport returns text and usage and rejects CLI tool execution', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'alpha-transport-test-'));
  try {
    const binary = join(dir, 'fake-codex');
    const source = `#!${process.execPath}\nimport{writeFileSync}from'node:fs';const args=process.argv;writeFileSync(args[args.indexOf('--output-last-message')+1],JSON.stringify({response:'Final Answer: example'}));console.log(JSON.stringify({type:'turn.completed',usage:{input_tokens:10,output_tokens:4}}));`;
    await writeFile(binary, source); await chmod(binary, 0o755);
    const backend = new CodexBackend({ binary });
    const r = await backend.complete({ schema: { type: 'object' }, prompt: 'Test transport.' });
    assert.equal(r.output.response, 'Final Answer: example');
    assert.equal(r.usage.input_tokens, 10);
    await writeFile(binary, source + `console.log(JSON.stringify({type:'item.completed',item:{type:'command_execution'}}));`);
    await assert.rejects(backend.complete({ schema: { type: 'object' }, prompt: 'Test transport.' }), /external tool/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('external runner is invoked for every variant and the completed-run hook persists all evidence', async () => {
  const f = await loadExternalFleet();
  const calls = [], journal = [];
  const runner = async ({ testCase, variant, trial }) => {
    calls.push({ id: testCase.id, variant: variant.id });
    return { case_id: testCase.id, split: testCase.split, category: testCase.category, trial,
      variant: variant.id, trace: variant.roles.map(role => ({ role, model_calls: 1, usage: { input_tokens: 10, output_tokens: 1 } })),
      verdict: { passed: true, unsafe: false, checks: [] }, error: null, elapsed_ms: 1, tools: [], final_state: {} };
  };
  const report = await experiment({ ...f, backend: { name: 'test', model: 'test' }, trials: 1, concurrency: 3,
    runTask: runner, onRun: async run => journal.push(run) });
  assert.equal(report.runs.length, 20);
  assert.equal(calls.length, 20);
  assert.equal(journal.length, 20);
  assert.equal(report.validated, true);
  assert(report.runs.filter(r => r.split === 'holdout').every(r => ['baseline', report.selected].includes(r.variant)));
});

test('external reports describe code execution and preserve upstream provenance', async () => {
  const f = await loadExternalFleet();
  const sample = JSON.parse(await readFile(new URL('../examples/live/experiment.json', import.meta.url), 'utf8'));
  sample.fleet = f.fleet;
  const html = renderReport(sample);
  assert(html.includes('Independent upstream crew'));
  assert(html.includes(f.fleet.provenance.commit));
  assert(html.includes('Hidden input/output checks'));
  assert(!html.includes('Actual disposition, ticket state'));
});
