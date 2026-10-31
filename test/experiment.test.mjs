/* Tests protect the experiment's evidence boundaries and negative controls.
   Fixture success is a runner check, never evidence about model quality. */

import test from 'node:test';
import assert from 'node:assert/strict';
import { createWorld, grade, loadCases, makeVariants, roleContext, validateOutput, VARIANTS } from '../src/domain.mjs';
import { FixtureBackend } from '../src/backends.mjs';
import { runCase, loadPrompts, experiment, compare, summarize, chooseCandidate } from '../src/engine.mjs';
import { renderReport } from '../src/report.mjs';

const cases = await loadCases();
const prompts = await loadPrompts();
const fixture = new FixtureBackend();
const demo = await experiment({ cases, prompts, backend: fixture, trials: 2 });

test('full fleet passes every authored case across both splits', () => {
  const runs = demo.runs.filter(r => r.variant === 'baseline');
  assert.equal(runs.length, 64);
  assert.equal(summarize(runs).pass_rate, 1);
});

test('discovery selects removal of the planner and frozen holdout confirms it', () => {
  assert.equal(demo.selected, 'without-planner');
  assert.equal(demo.validated, true);
  assert.deepEqual(demo.comparison.regressions, []);
  assert.equal(demo.runs.filter(r => r.split === 'holdout' && r.variant !== 'baseline').length, 32);
  assert.equal(summarize(demo.runs.filter(r => r.split === 'holdout' && r.variant === demo.selected)).role_calls, 128);
});

test('negative controls expose lost policy, missing research, and excess refunds', () => {
  for (const id of ['without-policy', 'without-researcher', 'without-reviewer', 'lean']) {
    assert(summarize(demo.runs.filter(r => r.variant === id)).pass_rate < 1, id);
  }
  assert(summarize(demo.runs.filter(r => r.variant === 'without-reviewer')).unsafe > 0);
});

test('every candidate reruns downstream roles with no baseline output cache', async () => {
  const calls = [];
  const backend = { call: async (...args) => { calls.push(args[0]); return fixture.call(...args); } };
  await runCase({ testCase: cases[0], variant: VARIANTS[0], backend, prompts });
  await runCase({ testCase: cases[0], variant: VARIANTS.find(v => v.id === 'without-planner'), backend, prompts });
  assert.equal(calls.length, 9);
  assert.equal(calls.filter(r => r === 'resolver').length, 2);
});

test('worlds and original fixtures remain isolated after mutations', () => {
  const before = JSON.stringify(cases[0]);
  const a = createWorld(cases[0].initial), b = createWorld(cases[0].initial);
  a.apply({ action: 'refund', amount_cents: 5000 }, cases[0].input);
  assert.equal(b.state.refunds.length, 0);
  assert.equal(JSON.stringify(cases[0]), before);
  const lookedUp = b.lookup(cases[0].input.order_id);
  lookedUp.status = 'cancelled';
  assert.equal(b.state.orders[0].status, 'delivered');
});

test('backend contexts never contain oracle answers, split, IDs, or catalogs', async () => {
  const backend = { call: async (role, instruction, context) => {
    for (const forbidden of ['expected', 'initial', 'category', 'split', 'case_id']) {
      assert(!Object.hasOwn(context, forbidden));
    }
    assert(!JSON.stringify(context).includes('refund_count'));
    return fixture.call(role, instruction, context);
  } };
  const run = await runCase({ testCase: cases[0], variant: VARIANTS[0], backend, prompts });
  assert(run.verdict.passed);
});

test('policy does not see planner outputs or a hidden order when research is removed', () => {
  assert.deepEqual(roleContext('policy', cases[0].input, { planner: { order: cases[0].initial.orders[0] } }),
    { request: cases[0].input, order: null });
});

test('grader rejects overpayment, wrong target, duplicates, and wrong disposition', () => {
  const c = cases[0];
  const correct = createWorld(c.initial);
  correct.apply({ action: 'refund', amount_cents: 5000 }, c.input);
  assert(grade(correct.state, c.initial, c.expected).passed);
  for (const mutate of [
    s => { s.refunds[0].amount_cents = 5001; },
    s => { s.refunds[0].order_id = 'wrong-order'; },
    s => { s.refunds.push({ ...s.refunds[0], amount_cents: 0 }); },
    s => { s.ticket.action = 'deny'; },
  ]) {
    const s = structuredClone(correct.state); mutate(s);
    assert(!grade(s, c.initial, c.expected).passed);
  }
});

test('action tool does not silently enforce eligibility', () => {
  const c = cases.find(c => c.category === 'expired-refund');
  const w = createWorld(c.initial);
  w.apply({ action: 'refund', amount_cents: 7000 }, c.input);
  assert.equal(w.state.refunds.length, 1);
  assert(grade(w.state, c.initial, c.expected).unsafe);
});

test('altering holdout expectations cannot change the discovery winner', async () => {
  const changed = structuredClone(cases);
  for (const c of changed.filter(c => c.split === 'holdout')) c.expected.refund_cents = 123456;
  const result = await experiment({ cases: changed, prompts, backend: fixture, trials: 1 });
  assert.equal(result.selected, demo.selected);
  assert.equal(result.validated, false);
});

test('provider failures are visible and cannot become a cheap winning candidate', async () => {
  const backend = { name: 'broken', model: 'none', call: async () => { throw new Error('test provider failure'); } };
  const result = await experiment({ cases, prompts, backend, trials: 1, limit: 1 });
  assert.equal(result.selected, null);
  assert.equal(result.validated, false);
  assert(result.runs.every(r => r.error));
});

test('comparison refuses unpaired, duplicate, or mismatched trials', () => {
  const base = demo.runs.filter(r => r.variant === 'baseline' && r.split === 'discovery');
  const candidate = demo.runs.filter(r => r.variant === demo.selected && r.split === 'discovery');
  assert.throws(() => compare(base, candidate.slice(1)), /unpaired/);
  assert.throws(() => compare(base, candidate.map(() => candidate[0])), /different/);
});

test('fixture counts are never reported as model usage', () => {
  const s = summarize(demo.runs);
  assert.equal(s.model_calls, 0);
  assert.equal(s.input_tokens, null);
  assert.equal(s.tokens_per_success, null);
  assert(s.role_calls > 0);
  assert.equal(demo.fixture, true);
});

test('selection optimizes measured tokens among candidates with equal quality', () => {
  const base = demo.runs.filter(r => r.variant === 'baseline' && r.split === 'discovery');
  const variants = [VARIANTS[0], { id: 'cheap', roles: ['resolver'] }, { id: 'expensive', roles: ['resolver'] }];
  const runs = [...base, ...base.map(r => ({ ...r, variant: 'cheap', trace: [{ model_calls: 1, usage: { input_tokens: 10, output_tokens: 5 } }] })),
    ...base.map(r => ({ ...r, variant: 'expensive', trace: [{ model_calls: 1, usage: { input_tokens: 100, output_tokens: 50 } }] }))];
  assert.equal(chooseCandidate(runs, variants).id, 'cheap');
});

test('configuration rejects missing resolver, unknown, duplicate, and reordered roles', () => {
  for (const roles of [[], ['planner'], ['resolver', 'unknown'], ['resolver', 'resolver'], ['reviewer', 'resolver']]) {
    assert.throws(() => makeVariants(roles));
  }
  assert.deepEqual(makeVariants(['policy', 'resolver'])[0].roles, ['policy', 'resolver']);
});

test('malformed role responses fail instead of mutating world state', () => {
  assert.throws(() => validateOutput('resolver', { action: 'refund', amount_cents: -1, reason: '' }));
  assert.throws(() => validateOutput('policy', { disposition: 'approve', max_refund_cents: 0, reason: '' }));
  assert.throws(() => validateOutput('planner', { steps: [42] }));
});

test('experiment rejects empty splits and invalid trial counts', async () => {
  await assert.rejects(experiment({ cases: [], prompts, backend: fixture }), /Both/);
  await assert.rejects(experiment({ cases, prompts, backend: fixture, trials: 0 }), /positive/);
  await assert.rejects(experiment({ cases, prompts, backend: fixture, limit: 0 }), /positive/);
  await assert.rejects(experiment({ cases, prompts, backend: fixture, concurrency: 9 }), /Concurrency/);
});

test('bounded parallel runs preserve pairing, isolation, and selection', async () => {
  const result = await experiment({ cases, prompts, backend: fixture, trials: 1, limit: 4, concurrency: 3 });
  assert.equal(result.selected, 'without-planner');
  assert.equal(result.validated, true);
  assert.equal(result.runs.length, 32);
});

test('report escapes untrusted strings, including script terminators', () => {
  const poisoned = structuredClone(demo);
  poisoned.runs[0].category = '</script><script>alert(1)</script>';
  const html = renderReport(poisoned);
  assert(!html.includes('</script><script>alert(1)</script>'));
  assert(html.includes('SCRIPTED DEMO'));
  assert(html.includes('Unmeasured'));
});
