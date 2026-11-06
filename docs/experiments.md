# Experiment decisions and accounting

Alpha executes the baseline and every candidate against fresh copies of the initial world. Changes are recipes, not edits to recorded traces. All downstream model work is rerun. Expected outcomes and executable checks reach only the grader after the workflow exits.

## Discovery and holdout

Discovery excludes candidates with unsafe outcomes, execution errors, zero quality, quality below the configured floor, or an observed regression rate above the tolerance. An imperfect baseline is allowed. The primary candidate follows `--objective tokens` (default), `estimated-cost`, or `latency`. Cheapest, fastest, and highest-quality labels are also frozen on discovery. Cheapest always uses estimated dollars when prices are supplied, otherwise reported tokens. All eligible candidate recipes are frozen before any holdout execution, and all frozen candidates are tested on holdout. Holdout never selects a new primary winner.

Default policy: 95% confidence, maximum regression probability 10%, at least 20 independent task units, and at least two trials per case. Set these **before starting** with `--confidence`, `--max-regression-rate`, `--min-cases`, `--min-trials`, and `--minimum-quality`. Lowering requirements changes the claim; it does not create more evidence.

A unit regresses if any paired trial passes under the baseline and fails under the candidate. Alpha computes an exact one-sided binomial upper bound on that probability, splitting the error budget across frozen candidates with Bonferroni correction. Zero observed regressions among `n` units gives `1 - alpha^(1/n)`. The general bound inverts the binomial CDF, following the [exact confidence-limit method described by NIST](https://itl.nist.gov/div898/handbook/prc/section2/prc241.htm). The probability of any regression conservatively bounds mean binary quality loss for the specified trial protocol.

Use `evaluation_unit` to group related cases; its default is the case ID. Ten assertions and ten repeated trials do not become 100 independent observations. Units must be independent and representative for the statistical interpretation to hold. Related discovery and holdout units are rejected when they have the same declared unit ID. Alpha cannot infer undeclared dependencies or prove representativeness.

A candidate is **validated** only when its upper bound is within tolerance, sample and repeat requirements hold, its quality meets the floor, neither side has execution errors, and it has no unsafe outcomes. Insufficient evidence is **inconclusive**; unsafe outcomes, execution errors, or a failed quality floor produce **rejected**. Scripted transports used in tests and fixture runs are engineering checks, never model-quality evidence. CLI fixture runs always say **scripted** and never validate a live change. The small bundled suites are development suites; their counts do not establish independent benchmark performance. The historical version-one examples retain their original evidence and decisions; current policy would treat their small live samples as inconclusive.

## Candidate recipes

`--search expanded` adds deterministic and conditional review, a shorter reviewer prompt, less resolver context, and a combined change to the support fleet. `--candidate-model MODEL` adds an explicit per-role alternative-model candidate. Alpha does not assume that a model is cheaper from its name. Provide rates and use the estimated-cost objective to compare price changes.

For custom combinations, pass `--candidates recipes.json`, a JSON list starting with the unchanged baseline. Each recipe has `id`, `label`, and an ordered subset of baseline `roles`. Optional role-keyed maps are `models`, `prompt_overrides`, `drop_context` (lists of context keys), `deterministic`, and `conditional`. Adapters declare supported options and rule names; unsupported settings fail before running models. The support reviewer rule is `refund-cap`; the writing adapter rule is `structural-post`. Rule execution uses visible state only. A structural writing check does not replace unmeasured subjective judgment.

```json
[
  {"id":"baseline","label":"Original","roles":["planner","researcher","policy","resolver","reviewer"]},
  {"id":"combined","label":"Combined change","roles":["researcher","policy","resolver","reviewer"],
   "models":{"resolver":"YOUR_ALTERNATIVE_MODEL"},
   "drop_context":{"resolver":["plan"]},"deterministic":{"reviewer":"refund-cap"}}
]
```

`candidate-config.json` records the complete primary recipe. For advanced changes, `proposal.patch` adds that recipe file, which is consumed with `--candidates candidate-config.json`. Simple removals can still propose the existing fleet configuration patch. No patch is applied automatically. Inconclusive live runs produce an explanation instead of an applicable patch. Scripted demos may produce a patch to demonstrate mechanics; they remain labeled scripted.

## Usage, price, and latency

Every admitted provider attempt is written to the checkpoint before starting. Completed, failed, and interrupted attempts remain in the ledger, including attempts from unfinished workflows retried after resume. Failed requests with unreported usage stay **unknown**, never zero. Usage summaries retain known partial totals while marking whether they are complete. Per-success metrics include failed-attempt usage within those workflows; experiment-wide accounting also shows unfinished workflows.

Input tokens include cached input. Optional `--prices prices.json` supplies USD per million tokens:

```json
{"YOUR_MODEL":{"input":2.0,"cached_input":0.5,"output":4.0}}
```

These numbers illustrate the format; they are not current provider prices. Alpha charges uncached input at `input`, cached input at `cached_input`, and output at `output`. Declared chargeable tool events can add `cost_usd`; missing tool prices keep total dollars unknown. Missing usage or model rates keep the complete estimate unknown. Billing cost remains `null`, because Alpha has not read billing records. Prices are frozen in the experiment fingerprint.

Tradeoffs show quality, estimated dollars or tokens per success, median and p95 workflow wall time. Latency includes process startup, retries, and actual runtime control flow. Trace step IDs and parent IDs preserve declared dependencies; adapters may report branches and joins rather than pretending parallel work was sequential. Alpha does not synthesize parallel execution by subtracting step times. The live Pareto frontier includes only validated candidates and the baseline. An option dominates another only when quality, known comparable cost, and median latency are all no worse and at least one is better. Unknown cost cannot demonstrate dominance. Frozen labels are discovery labels even when holdout results differ.

## Budgets and resume

```sh
python3 -m alpha experiment --backend codex --model YOUR_MODEL \
  --max-calls 50 --max-tokens 200000 --deadline 600 --out runs/bounded
python3 -m alpha experiment --resume runs/bounded --max-calls 100 --deadline 1200
```

`--max-calls` is a hard admission ceiling across concurrent workflows and native agents. `--max-tokens` stops new admissions once **observed** input-plus-output tokens reach the threshold. Already admitted concurrent requests can overshoot; this is not a guaranteed spend ceiling. Unknown usage after a failed or interrupted attempt blocks further token-budget admissions. `--deadline` measures cumulative active experiment time across resume, excluding the pause. Cancellation terminates owned subprocess groups and gateway requests. A budget stop writes partial reports and exits 3; interruption exits 130. It does not score an unfinished workflow as a completed failure.

`--resume DIR` is explicit. It restores the original settings and allows budget increases only. Pin `--model` when starting any live experiment you intend to resume; a mutable CLI default is refused on resume. The fingerprint covers the complete dataset including private expected values, prompts, variants, policy, prices, trial/concurrency settings, backend executable and timeout, core code/templates, adapter manifest/files, declared upstream pins, and native interpreter/package versions. Changes require a new experiment. Provider-side changes behind an unchanged model name remain outside Alpha's ability to verify.

`progress.jsonl` contains append-only, hash-chained completed trajectories. `checkpoint.json` uses atomic replacement and a checksum, with admitted attempts and the frozen discovery decision. Resume verifies both, recovers complete journal records written just before a crash, skips completed jobs, and retries unfinished jobs from fresh initial state. Earlier attempts remain chargeable evidence. A truncated journal line, modified trajectory, duplicate job, mismatched checkpoint, or changed frozen evidence fails closed. Checksums detect accidental alteration; they are not signatures against a malicious editor who recomputes them.

Run directories contain task content, prompts, and outputs. They stay ignored by Git. The checkpoint records experiment configuration, never credential files, environment snapshots, or provider stderr. Runtime authentication is used in place and is not exported. External adapters are trusted local code: the protocol prevents accidental oracle leakage and centralizes cooperative model requests; it is not an operating-system isolation boundary for arbitrary workflow code.
