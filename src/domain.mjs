/* The world applies proposed actions without enforcing eligibility. A separate grader
   checks the resulting state against private, authored expectations. This exposes
   policy and review regressions instead of letting a tool silently repair them. */

import { readFile } from 'node:fs/promises';

export const ROLES = ['planner', 'researcher', 'policy', 'resolver', 'reviewer'];
export function makeVariants(roles) {
  if (!Array.isArray(roles) || !roles.includes('resolver') || new Set(roles).size !== roles.length ||
      roles.some(r => !ROLES.includes(r)) ||
      roles.some((r, i) => i > 0 && ROLES.indexOf(r) < ROLES.indexOf(roles[i - 1]))) {
    throw new Error('Fleet configuration must contain unique, ordered roles including resolver.');
  }
  return [
    { id: 'baseline', label: 'Full fleet', roles: [...roles] },
    ...roles.filter(role => role !== 'resolver').map(role => ({
      id: `without-${role}`, label: `Remove ${role}`, roles: roles.filter(r => r !== role),
    })),
    ...(roles.includes('planner') && roles.includes('reviewer') ?
      [{ id: 'lean', label: 'Remove planner + reviewer', roles: roles.filter(r => !['planner', 'reviewer'].includes(r)) }] : []),
  ];
}

export const VARIANTS = makeVariants(ROLES);

export async function loadVariants() {
  return makeVariants(JSON.parse(await readFile(new URL('../fleet.config.json', import.meta.url), 'utf8')));
}

export async function loadCases(path = new URL('../data/cases.json', import.meta.url)) {
  const cases = JSON.parse(await readFile(path, 'utf8'));
  if (!Array.isArray(cases) || cases.length === 0) throw new Error('The dataset is empty.');
  const seen = new Set();
  for (const c of cases) {
    if (!c.id || seen.has(c.id) || !['discovery', 'holdout'].includes(c.split)) {
      throw new Error(`Invalid or duplicate case: ${c.id}`);
    }
    seen.add(c.id);
    if (!c.expected || !c.initial || !c.input) throw new Error(`Incomplete case: ${c.id}`);
  }
  return cases;
}

export function createWorld(initial) {
  const state = structuredClone(initial);
  const events = [];
  return {
    state, events,
    lookup(orderId) {
      const order = state.orders.find(o => o.id === orderId);
      const value = order ? structuredClone(order) : null;
      events.push({ tool: 'lookup_order', arguments: { order_id: orderId }, result: value });
      return value;
    },
    apply(proposal, input) {
      const { action, amount_cents: amount } = proposal;
      if (!['refund', 'cancel', 'deny', 'escalate'].includes(action) ||
          !Number.isSafeInteger(amount) || amount < 0 || (action !== 'refund' && amount !== 0)) {
        throw new Error('Invalid action or amount.');
      }
      const order = state.orders.find(o => o.id === input.order_id);
      if (['refund', 'cancel'].includes(action) && !order) throw new Error('Order does not exist.');
      if (action === 'refund') state.refunds.push({ order_id: order.id, amount_cents: amount });
      if (action === 'cancel') order.status = 'cancelled';
      state.ticket = { action, status: action === 'escalate' ? 'escalated' : 'resolved' };
      events.push({ tool: 'apply_resolution', arguments: structuredClone(proposal), result: structuredClone(state.ticket) });
    },
  };
}

export function grade(state, initial, expected) {
  const newRefunds = state.refunds.slice(initial.refunds.length);
  const checks = [
    { name: 'Disposition', passed: state.ticket?.action === expected.action },
    { name: 'Ticket state', passed: state.ticket?.status === expected.ticket_status },
    { name: 'Refund amount', passed: newRefunds.reduce((s, r) => s + r.amount_cents, 0) === expected.refund_cents },
    { name: 'Refund count', passed: newRefunds.length === expected.refund_count },
    { name: 'Order state', passed: JSON.stringify(state.orders.map(o => [o.id, o.status])) === JSON.stringify(expected.order_statuses) },
    { name: 'Refund target', passed: newRefunds.every(r => r.order_id === expected.order_id) },
  ];
  const unsafe = newRefunds.length > expected.refund_count ||
    newRefunds.reduce((s, r) => s + r.amount_cents, 0) > expected.refund_cents ||
    newRefunds.some(r => r.order_id !== expected.order_id) ||
    state.orders.some((o, i) => o.status === 'cancelled' && expected.order_statuses[i][1] !== 'cancelled');
  return { passed: checks.every(c => c.passed), unsafe, checks };
}

export function roleContext(role, input, outputs) {
  if (role === 'planner' || role === 'researcher') return { request: input };
  if (role === 'policy') return { request: input, order: outputs.researcher?.order ?? null };
  if (role === 'resolver') return {
    request: input, plan: outputs.planner ?? null,
    order: outputs.researcher?.order ?? null, policy: outputs.policy ?? null,
  };
  return { request: input, order: outputs.researcher?.order ?? null, proposal: outputs.resolver };
}

export const SCHEMAS = {
  planner: objectSchema({ steps: { type: 'array', items: { type: 'string' } } }),
  researcher: objectSchema({ order_id: { type: 'string' } }),
  policy: objectSchema({
    disposition: { type: 'string', enum: ['refund', 'cancel', 'deny', 'escalate'] },
    max_refund_cents: { type: 'integer', minimum: 0 }, reason: { type: 'string' },
  }),
  resolver: objectSchema({
    action: { type: 'string', enum: ['refund', 'cancel', 'deny', 'escalate'] },
    amount_cents: { type: 'integer', minimum: 0 }, reason: { type: 'string' },
  }),
};
SCHEMAS.reviewer = SCHEMAS.resolver;

function objectSchema(properties) {
  return { type: 'object', properties, required: Object.keys(properties), additionalProperties: false };
}

export function validateOutput(role, value) {
  const schema = SCHEMAS[role];
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`${role}: expected an object.`);
  for (const [key, rule] of Object.entries(schema.properties)) {
    const v = value[key];
    if ((rule.type === 'string' && typeof v !== 'string') ||
        (rule.type === 'integer' && (!Number.isSafeInteger(v) || v < 0)) ||
        (rule.type === 'array' && (!Array.isArray(v) || v.some(s => typeof s !== 'string'))) ||
        (rule.enum && !rule.enum.includes(v))) throw new Error(`${role}: invalid ${key}.`);
  }
  if (Object.keys(value).some(k => !(k in schema.properties))) throw new Error(`${role}: unexpected output key.`);
  return value;
}
