/* Candidate selection uses discovery cases only. The selected variant is frozen
   before holdout evaluation. Every trial reruns the remaining roles and tools. */

import { readFile } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';
import { createWorld, grade, roleContext, validateOutput, ROLES, VARIANTS } from './domain.mjs';

export async function loadPrompts() {
  return Object.fromEntries(await Promise.all(ROLES.map(async role => [role,
    await readFile(new URL(`../agent/subagents/${role}/instructions.md`, import.meta.url), 'utf8')])));
}

export async function runCase({ testCase, variant, backend, prompts, trial = 0, signal }) {
  const world = createWorld(testCase.initial);
  const outputs = {};
  const trace = [];
  const started = performance.now();
  let error = null;
  for (const role of variant.roles) {
    if (signal?.aborted) throw new Error('Experiment cancelled.');
    const context = roleContext(role, structuredClone(testCase.input), outputs);
    const before = performance.now();
    try {
      const result = await backend.call(role, prompts[role], structuredClone(context), { signal });
      const output = validateOutput(role, result.output);
      outputs[role] = structuredClone(output);
      if (role === 'researcher') outputs[role] = { order: world.lookup(output.order_id) };
      trace.push({ role, context, output, elapsed_ms: performance.now() - before,
        usage: result.usage, model_calls: result.model_calls });
    } catch (e) {
      error = e.message;
      trace.push({ role, context, error, elapsed_ms: performance.now() - before, usage: null, model_calls: 0 });
      break;
    }
  }
  if (!error) {
    try { world.apply(outputs.reviewer ?? outputs.resolver, testCase.input); }
    catch (e) { error = e.message; }
  }
  const verdict = grade(world.state, testCase.initial, testCase.expected);
  if (error) verdict.passed = false;
  return { case_id: testCase.id, category: testCase.category, split: testCase.split,
    variant: variant.id, trial, trace, tools: world.events, final_state: world.state,
    verdict, error, elapsed_ms: performance.now() - started };
}

export function summarize(runs) {
  const usage = runs.flatMap(r => r.trace).filter(s => s.usage).map(s => s.usage);
  const times = runs.map(r => r.elapsed_ms).sort((a, b) => a - b);
  const successes = runs.filter(r => r.verdict.passed).length;
  return { count: runs.length, passed: successes, pass_rate: runs.length ? successes / runs.length : null,
    unsafe: runs.filter(r => r.verdict.unsafe).length, errors: runs.filter(r => r.error).length,
    role_calls: runs.reduce((sum, r) => sum + r.trace.length, 0),
    model_calls: runs.reduce((sum, r) => sum + r.trace.reduce((s, t) => s + t.model_calls, 0), 0),
    input_tokens: usage.length ? usage.reduce((s, u) => s + (u.input_tokens ?? 0), 0) : null,
    cached_input_tokens: usage.length ? usage.reduce((s, u) => s + (u.cached_input_tokens ?? 0), 0) : null,
    output_tokens: usage.length ? usage.reduce((s, u) => s + (u.output_tokens ?? 0), 0) : null,
    median_ms: times.length ? times[Math.floor(times.length / 2)] : null,
    tokens_per_success: usage.length && successes ? usage.reduce((s, u) => s + (u.input_tokens ?? 0) + (u.output_tokens ?? 0), 0) / successes : null,
  };
}

export function compare(baseline, candidate) {
  const base = new Map(baseline.map(r => [`${r.case_id}:${r.trial}`, r]));
  if (candidate.length !== baseline.length) throw new Error('Cannot compare unpaired trials.');
  const regressions = [], improvements = [];
  for (const run of candidate) {
    const key = `${run.case_id}:${run.trial}`;
    const original = base.get(key);
    if (!original) throw new Error('Cannot compare different case IDs or trials.');
    base.delete(key);
    if (original.verdict.passed && !run.verdict.passed) regressions.push(key);
    if (!original.verdict.passed && run.verdict.passed) improvements.push(key);
  }
  return { regressions, improvements };
}

export function chooseCandidate(results, variants) {
  const baseline = results.filter(r => r.variant === 'baseline');
  const bs = summarize(baseline);
  if (!baseline.length || bs.errors || bs.pass_rate !== 1) return null;
  const eligible = variants.filter(v => v.id !== 'baseline').filter(v => {
    const runs = results.filter(r => r.variant === v.id);
    if (runs.length !== baseline.length) return false;
    const s = summarize(runs);
    return s.errors === 0 && s.unsafe === 0 && compare(baseline, runs).regressions.length === 0;
  });
  return eligible.sort((a, b) => {
    const sa = summarize(results.filter(r => r.variant === a.id));
    const sb = summarize(results.filter(r => r.variant === b.id));
    return (sa.tokens_per_success ?? sa.role_calls) - (sb.tokens_per_success ?? sb.role_calls) || a.id.localeCompare(b.id);
  })[0] ?? null;
}

export async function experiment({ cases, backend, prompts, trials = 2, limit, variants = VARIANTS, concurrency = 1, onProgress = () => {}, signal }) {
  if (!Number.isInteger(trials) || trials < 1) throw new Error('Trials must be a positive integer.');
  if (limit !== undefined && (!Number.isInteger(limit) || limit < 1)) throw new Error('Limit must be a positive integer.');
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 8) throw new Error('Concurrency must be an integer from 1 to 8.');
  const discovery = cases.filter(c => c.split === 'discovery').slice(0, limit);
  const holdout = cases.filter(c => c.split === 'holdout').slice(0, limit);
  if (!discovery.length || !holdout.length) throw new Error('Both discovery and holdout cases are required.');
  const runs = [];
  const execute = async (selectedCases, selectedVariants, phase) => {
    const jobs = [];
    for (const c of selectedCases) for (let trial = 0; trial < trials; trial++) {
      const offset = (selectedCases.indexOf(c) + trial) % selectedVariants.length;
      const rotated = [...selectedVariants.slice(offset), ...selectedVariants.slice(0, offset)];
      for (const variant of rotated) {
        jobs.push({ testCase: c, variant, trial });
      }
    }
    const completed = new Array(jobs.length);
    let next = 0;
    await Promise.all(Array.from({ length: Math.min(concurrency, jobs.length) }, async () => {
      while (next < jobs.length) {
        const index = next++, job = jobs[index];
        onProgress(`${phase} · ${job.testCase.id} · ${job.variant.id} · trial ${job.trial + 1}`);
        completed[index] = await runCase({ ...job, backend, prompts, signal });
      }
    }));
    runs.push(...completed);
  };
  await execute(discovery, variants, 'Discovery');
  const selected = chooseCandidate(runs, variants);
  const frozenAt = new Date().toISOString();
  await execute(holdout, selected ? [variants[0], selected] : [variants[0]], 'Holdout');
  const baselineHoldout = runs.filter(r => r.split === 'holdout' && r.variant === 'baseline');
  const selectedHoldout = selected ? runs.filter(r => r.split === 'holdout' && r.variant === selected.id) : [];
  const comparison = selected ? compare(baselineHoldout, selectedHoldout) : null;
  const hs = summarize(selectedHoldout);
  const validated = Boolean(selected && summarize(baselineHoldout).pass_rate === 1 && !hs.errors && !hs.unsafe && !comparison.regressions.length);
  return { version: 1, id: randomUUID(), created_at: new Date().toISOString(),
    backend: backend.name, model: backend.model, fixture: backend.name === 'fixture', trials, concurrency,
    dataset: { source: 'Alpha authored support development suite',
      sha256: createHash('sha256').update(JSON.stringify(cases)).digest('hex'),
      discovery_cases: discovery.length, holdout_cases: holdout.length },
    prompt_sha256: createHash('sha256').update(JSON.stringify(prompts)).digest('hex'),
    variants, selected: selected?.id ?? null, frozen_at: frozenAt, validated, comparison, runs };
}
