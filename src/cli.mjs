/* The CLI writes reproducible evidence and a standalone report. It never applies
   the selected change or sends messages to external people or applications. */

import { mkdir, writeFile, readFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { loadCases, loadVariants } from './domain.mjs';
import { FixtureBackend, CodexBackend } from './backends.mjs';
import { loadPrompts, experiment, summarize } from './engine.mjs';
import { renderReport } from './report.mjs';

async function main() {
  const [command = 'help', ...args] = process.argv.slice(2);
  if (command === 'help' || args.includes('--help')) {
    console.log(`Alpha · measure the value of each agent step\n\nnode src/cli.mjs experiment [options]\n  --backend fixture|codex  Scripted demo or live CLI model requests (default: fixture)\n  --limit N                Cases per split (default: all)\n  --trials N               Independent trials per case (default: 2)\n  --concurrency N          Independent workflows at once, 1–8 (default: 1)\n  --variants ID,ID         Candidate variants; baseline is always included\n  --model NAME             Optional model override for the live backend\n  --out DIR                Output directory (default: runs/<timestamp>)\n\nnode src/cli.mjs report PATH_TO_JSON [--out DIR]\n\nFixture mode does not call a model. Live mode uses your existing Codex CLI login.\nA complete live run can issue hundreds of model requests; use --limit 2 initially.`);
    return;
  }
  const options = {};
  const known = new Set(['backend', 'limit', 'trials', 'variants', 'model', 'out', 'concurrency']);
  let inputPath;
  for (let i = 0; i < args.length; i++) {
    if (command === 'report' && !args[i].startsWith('--') && !inputPath) { inputPath = args[i]; continue; }
    const key = args[i].slice(2);
    if (!args[i].startsWith('--') || !known.has(key) || !args[i + 1] || args[i + 1].startsWith('--')) {
      throw new Error(`Unknown or incomplete option: ${args[i]}`);
    }
    options[key] = args[++i];
  }
  let report;
  if (command === 'report') {
    if (!inputPath) throw new Error('Provide an experiment JSON path.');
    report = JSON.parse(await readFile(resolve(inputPath), 'utf8'));
  } else if (command === 'experiment') {
    const backendName = options.backend ?? 'fixture';
    if (!['fixture', 'codex'].includes(backendName)) throw new Error('Backend must be fixture or codex.');
    const backend = backendName === 'fixture' ? new FixtureBackend() : new CodexBackend({ model: options.model });
    const available = await loadVariants();
    let variants = available;
    if (options.variants) {
      const ids = new Set(['baseline', ...options.variants.split(',')]);
      if ([...ids].some(id => !available.some(v => v.id === id))) throw new Error('Unknown variant ID.');
      variants = available.filter(v => ids.has(v.id));
    }
    const controller = new AbortController();
    const cancel = () => controller.abort();
    process.once('SIGINT', cancel);
    process.once('SIGTERM', cancel);
    try {
      report = await experiment({ cases: await loadCases(), prompts: await loadPrompts(), backend,
        trials: options.trials === undefined ? 2 : Number(options.trials),
        concurrency: options.concurrency === undefined ? 1 : Number(options.concurrency),
        limit: options.limit === undefined ? undefined : Number(options.limit), variants,
        signal: controller.signal, onProgress: message => process.stderr.write(`${message}\n`) });
    } finally {
      process.off('SIGINT', cancel); process.off('SIGTERM', cancel);
    }
  } else throw new Error(`Unknown command: ${command}`);
  const out = resolve(options.out ?? join('runs', new Date().toISOString().replace(/[:.]/g, '-')));
  await mkdir(out, { recursive: true });
  await writeFile(join(out, 'experiment.json'), JSON.stringify(report, null, 2) + '\n');
  await writeFile(join(out, 'report.html'), renderReport(report));
  const selected = report.variants.find(v => v.id === report.selected);
  await writeFile(join(out, 'proposal.patch'), selected ?
    `--- a/fleet.config.json\n+++ b/fleet.config.json\n@@ -1 +1 @@\n-${JSON.stringify(report.variants[0].roles)}\n+${JSON.stringify(selected.roles)}\n` : 'No candidate selected.\n');
  console.log(JSON.stringify({ backend: report.backend, selected: report.selected, validated: report.validated,
    holdout: summarize(report.runs.filter(r => r.split === 'holdout' && r.variant === (report.selected ?? 'baseline'))),
    report: join(out, 'report.html') }, null, 2));
  if (report.runs.some(r => r.error)) process.exitCode = 2;
}

main().catch(error => { console.error(`Alpha: ${error.message}`); process.exitCode = 1; });
