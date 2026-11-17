# Optional qualitative evaluations

## Shared grading guidelines

Asking either a person or an LLM to say whether an answer is simply "good" makes
ratings hard to interpret. Instead, give **both** the same written instructions
(*rubric*): what qualities matter, what each 1–5 value means, and what counts
as a meaningful advantage in A/B.

The default `general-output-quality` rubric uses:

| Criterion | Question |
| --- | --- |
| Relevance | Does it address the actual request? |
| Completeness | Does it cover the important parts? |
| Clarity | Is it understandable and appropriately organized? |
| Instruction following | Does it respect the user's constraints and format? |
| Helpfulness | Does it move the user toward the intended outcome? |

The anchors are explicitly defined: **1** poor (major failure), **2** weak
(serious gaps), **3** adequate (useful but noticeable weaknesses), **4** good
(minor gaps), and **5** excellent (satisfies visible requirements). A/B reviewers
apply the same criteria to both outputs; ties are valid when neither is
meaningfully better. Reviewers should not automatically reward extra length
or infer missing facts.

**Implementation:** `alpha/rubrics.py` contains the default rubric and
validation. `alpha q human` embeds the full rubric in the standalone human
review page and its exported JSON. `alpha q judge` supplies the same rubric
in the LLM score and pairwise prompts. Results record the rubric definition
and its SHA-256 fingerprint. `alpha q calibrate` checks these fingerprints
before assessing human/model agreement. If the rubric differs, calibration
refuses the comparison. Legacy results without a recorded rubric are flagged
as unverified rather than assumed equivalent.

You may customize instructions without changing the experiment engine:

```sh
alpha q human --rubric examples/rubrics/writing-quality.json
alpha q judge --rubric examples/rubrics/writing-quality.json
alpha q calibrate --human /path/to/human-review-completed.json
```

The [writing rubric](../examples/rubrics/writing-quality.json) is an editable
example for Alpha's writing/review workflow. To create your own, copy it and
change the ID/version, purpose, criteria, 1–5 anchors, pairwise instructions,
and tie rule. Keep all five anchors, unique criterion IDs, and the JSON fields
shown in the example. **Use the same file for the human and LLM runs.** The
rubric is frozen with the saved results; changing it after the fact requires
new judgments, not only relabeling existing scores.

## The 2 × 2 evaluation matrix

Who evaluates and how they evaluate are independent choices:

| | 1–5 stars (single-output quality) | A/B (relative preference) |
| --- | --- | --- |
| **Human** | A person scores one output from 1 to 5 | A person picks the better of two outputs, or a tie |
| **LLM** | A model scores one output from 1 to 5 | A model picks the better of two outputs, or a tie |

**Judge calibration:** Use a sample of human-reviewed cases as the reference,
then compare the LLM against those *exact same saved runs and pairs*.
The comparison reports:

- 1–5 stars: number of matched ratings, exact agreement, agreement within one
  star, mean absolute error (average absolute rating difference), signed bias
  (LLM score minus human score), and mismatched cases.
- A/B: number of matched pairs, percentage of exact agreement (including ties),
  all nine human-vs-model choice combinations, and mismatched pairs.
- Coverage: counts for each cell of the 2 × 2 matrix; unmatched ratings never
  enter the agreement calculation. Empty matches mean "unavailable," not 0%.

Human judgments are a useful reference but are not inherently correct.
A small reviewer sample, especially a single reviewer, does not certify the
model judge. Agreement on this dataset does not establish general reliability.

### Human review → model judge → calibration

```sh
alpha q human
# Open human-review.html in the run directory, rate the outputs,
# then download human-review-completed.json from the page.

alpha q judge --max-judgments 8
alpha q calibrate --human /path/to/human-review-completed.json
```

`alpha q human` generates both a JSON template and a standalone
`human-review.html` with individual 1–5 ratings and visually blinded
A/B choices. All inputs and choices stay in your browser until you export JSON.
The source file contains the A/B mapping so that preferences can be reconciled,
so this is display-level blinding, not a security guarantee.

The calibration shortcut loads the saved `qualitative.json` produced by
`q judge`, uses the exported human answers, and produces
`calibration.json` plus `calibration-report.html`. No extra model calls
occur. It does not overwrite `qualitative.json`. To read an earlier judge
file, use `--judge-results PATH`; to choose an older experiment, pass its
directory. Mismatched experiment IDs, unknown run IDs, duplicates, and empty
review files are rejected.

If you only want the matrix, `alpha q judge` or `alpha q human` still works.
Without matched human/model judgments the calibration fields are unavailable,
and the report makes that limitation explicit.

Alpha's objective checks and candidate selection remain unchanged. These are post-hoc demonstrations, not additional quality gates. They never change the frozen candidate, validated status, regression bounds, original token accounting, or proposal.patch. All modes are off by default.

## Short commands

Install the local CLI once from the Alpha checkout:

    python3 -m pip install -e .

Then run these from the repository root. The latest saved experiment under ./runs is selected automatically:

    alpha q demo                              # Synthetic feedback, follow-ups, turn count
    alpha q judge                             # LLM scores plus blind pairwise comparison
    alpha q judge --rubric FILE               # Use custom shared grading criteria
    alpha q pair                              # Pairwise comparison only
    alpha q human                             # Create human review template
    alpha q turns --interactions messages.json # Count recorded turns, follow-ups, corrections
    alpha q all                               # All demonstrations (model calls included)
    alpha q calibrate --human FILE            # Compare saved LLM judgments to human ratings

Choose an older experiment by passing its directory or experiment.json:

    alpha q judge runs/MY-EXPERIMENT --max-judgments 4

Pass --out DIR to keep outputs from different modes separate. Otherwise each mode replaces the prior qualitative report in the run directory. For LLM modes, --judge-model MODEL pins the judge; --max-judgments caps additional model calls. The original long form (`alpha qualitative ...`) still works and allows individually selected features.

Run an experiment normally first to produce experiment.json. Then use these commands (substitute your run folder):

    python3 -m alpha qualitative runs/EXAMPLE/experiment.json --simulate-feedback
    python3 -m alpha qualitative runs/EXAMPLE/experiment.json --human-template
    python3 -m alpha qualitative runs/EXAMPLE/experiment.json --human runs/EXAMPLE/human-review-template.json
    python3 -m alpha qualitative runs/EXAMPLE/experiment.json --turn-count --feedback --interactions conversations.json
    python3 -m alpha qualitative runs/EXAMPLE/experiment.json --llm-judge --pairwise --max-judgments 8 --judge-model MODEL_NAME

Every run writes qualitative.json and qualitative-report.html. --human-template also writes human-review-template.json. Use --out DIRECTORY to keep separate results.

## Methods

- --llm-judge: Scores individual holdout outputs from 1 to 5 for apparent relevance, completeness, clarity, and usefulness. Requires the Codex CLI and login. This is subjective, not a correctness oracle.
- --pairwise: Blindly compares baseline and candidate holdout outputs, using a hash to alternate the A/B presentation order. Produces baseline, candidate, or tie.
- --human-template: Creates a blank JSON survey and a standalone browser review page for saved holdout outputs. In the page, score each output 1–5 and choose A, B, or tie; export the answers as JSON for calibration. You may also fill the JSON template manually. No human judgment is fabricated.
- --turn-count: Counts actual user and assistant messages from supplied interaction records. A user's initial request counts as one turn; subsequent user messages are follow-ups. Agent steps and model calls are not assumed to be conversational turns.
- --feedback: Summarizes supplied satisfaction ratings, abandonments, and manually labeled corrections. Follow-ups do not automatically count as corrections.
- --simulate-feedback: Generates deliberately synthetic conversations from objective pass/fail. Failed runs get a fake correction and a low rating; successful runs do not. It is circular and is NOT empirical evidence.

## Interaction JSON example

    {
      "experiment_id": "THE_SAVED_EXPERIMENT_ID",
      "interactions": [{
        "split": "holdout", "case_id": "CASE_ID", "trial": 0, "variant": "baseline",
        "source": "observed",
        "messages": [
          {"role": "user", "content": "Please complete the task"},
          {"role": "assistant", "content": "Result"},
          {"role": "user", "content": "That is incorrect", "correction": true},
          {"role": "assistant", "content": "Corrected result"}
        ],
        "satisfaction": 2, "abandoned": false
      }]
    }

Each interaction must match a saved run. Missing interaction records are not inferred. Correction flags are explicit labels, not sentiment analysis; unlabeled follow-ups are marked as such. Human review files must match the saved experiment ID.

## Model call budget, privacy, and caveats

LLM methods run after the existing experiment and make additional model calls. --max-judgments caps the total across scoring and pairwise modes (default 12); those calls do not appear in the original cost ledger. Pin --judge-model where possible.

A model judge receives a saved run's first-step context, final response or artifact, and resulting state. Only use it if this data may be sent to the judge provider. Human review, recorded interactions, and synthetic feedback make no model calls. Raw interaction messages are counted but not copied into the summary.

These methods now support comparison against supplied human judgments under matching written rubrics, but Alpha does not ship a pre-labeled human reference set or claim statistical certification. Agreement and errors are descriptive and do not provide confidence bounds on judge accuracy. They remain exploratory signals, not proof that removing a role preserves quality.
