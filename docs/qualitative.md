# Optional qualitative evaluations

Alpha's objective checks and candidate selection remain unchanged. These are post-hoc demonstrations, not additional quality gates. They never change the frozen candidate, validated status, regression bounds, original token accounting, or proposal.patch. All modes are off by default.

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
- --human-template: Creates blank scores and pairwise preferences for the saved holdout outputs. Inspect the standard HTML report first, then fill score (integer 1-5) and preferred (baseline, candidate, tie), leaving unknown ones null. Pass the completed file to --human. No human judgment is fabricated.
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

These methods have no calibrated human reference set, consensus ratings, or confidence bounds for subjective outcomes. They are exploratory signals, not proof that removing a role preserves quality.
