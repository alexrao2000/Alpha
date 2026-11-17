"""Human-reference analysis of optional 1–5 and A/B judgments.

The 2x2 matrix describes who evaluates (human/LLM) and how (stars/A-B).
Agreement is calculated only on matched run IDs or matched pair IDs.
It is descriptive evidence, not a certified judge or a change to Alpha's gates.
"""

from __future__ import annotations

from collections import Counter
from statistics import mean


CHOICES = ("baseline", "candidate", "tie")


def _unique(rows: list[dict], fields: tuple[str, ...]) -> dict:
    indexed = {}
    for row in rows:
        key = tuple(row[field] for field in fields)
        if key in indexed:
            raise ValueError("Duplicate judgment on the same run or pair.")
        indexed[key] = row
    return indexed


def _rating_cell(rows: list[dict]) -> dict:
    scores = [r["score"] for r in rows]
    return {
        "count": len(rows),
        "mean": round(mean(scores), 3) if scores else None,
        "distribution": {str(n): sum(score == n for score in scores) for n in range(1, 6)},
    }


def _pair_cell(rows: list[dict]) -> dict:
    counts = Counter(r["preferred"] for r in rows)
    return {"count": len(rows), "preferences": {k: counts[k] for k in CHOICES}}


def analyze_judgments(
    llm_scores: list[dict], llm_pairs: list[dict],
    human_scores: list[dict], human_pairs: list[dict],
) -> dict:
    """Build the four cells and compare matched human/LLM judgments.

    Missing counterparts are excluded from agreement denominators; ties count
    as a distinct A/B outcome. Human scores are a reference, not ground truth.
    """
    human_r = _unique(human_scores, ("key",))
    model_r = _unique(llm_scores, ("key",))
    human_p = _unique(human_pairs, ("baseline_key", "candidate_key"))
    model_p = _unique(llm_pairs, ("baseline_key", "candidate_key"))

    matrix = {
        "human": {"stars": _rating_cell(human_scores), "ab": _pair_cell(human_pairs)},
        "llm": {"stars": _rating_cell(llm_scores), "ab": _pair_cell(llm_pairs)},
    }

    common_r = sorted(human_r.keys() & model_r.keys())
    rating_errors = [
        model_r[k]["score"] - human_r[k]["score"] for k in common_r
    ]
    rating_disagreements = [
        {"key": k[0], "human": human_r[k]["score"], "llm": model_r[k]["score"],
         "difference": model_r[k]["score"] - human_r[k]["score"]}
        for k in common_r if model_r[k]["score"] != human_r[k]["score"]
    ]
    rating_disagreements.sort(key=lambda r: (-abs(r["difference"]), r["key"]))
    n_r = len(common_r)

    common_p = sorted(human_p.keys() & model_p.keys())
    equal_p = sum(human_p[k]["preferred"] == model_p[k]["preferred"] for k in common_p)
    pair_disagreements = [
        {"baseline_key": k[0], "candidate_key": k[1],
         "human": human_p[k]["preferred"], "llm": model_p[k]["preferred"]}
        for k in common_p
        if human_p[k]["preferred"] != model_p[k]["preferred"]
    ]
    confusion = {
        human: {
            model: sum(human_p[k]["preferred"] == human and
                       model_p[k]["preferred"] == model for k in common_p)
            for model in CHOICES
        } for human in CHOICES
    }
    n_p = len(common_p)

    notes = [
        "Human ratings are a reference sample, not objective ground truth.",
        "Agreement uses exact matching run/pair IDs; unmatched judgments are excluded.",
        "A/B ties are a third outcome and count as disagreements with non-ties.",
    ]
    if not human_scores and not human_pairs:
        notes.append("No human judgments: the LLM judge has not been calibrated.")
    if not llm_scores and not llm_pairs:
        notes.append("No LLM judgments: human judgments are available but no judge comparison can be computed.")
    if not n_r:
        notes.append("No matched 1–5 ratings; rating agreement is unavailable.")
    if not n_p:
        notes.append("No matched A/B preferences; pairwise agreement is unavailable.")
    if (0 < n_r < 10) or (0 < n_p < 10):
        notes.append("Small matched sample: agreement estimates are exploratory and uncertain.")

    return {
        "matrix": matrix,
        "calibration": {
            "reference": "human",
            "status": "exploratory" if n_r or n_p else "unavailable",
            "ratings": {
                "matched": n_r, "human_total": len(human_scores),
                "llm_total": len(llm_scores),
                "exact_agreement": round(sum(e == 0 for e in rating_errors) / n_r, 3) if n_r else None,
                "within_one_star": round(sum(abs(e) <= 1 for e in rating_errors) / n_r, 3) if n_r else None,
                "mean_absolute_error": round(mean(abs(e) for e in rating_errors), 3) if n_r else None,
                "mean_signed_error": round(mean(rating_errors), 3) if n_r else None,
                "disagreements": rating_disagreements,
            },
            "pairwise": {
                "matched": n_p, "human_total": len(human_pairs),
                "llm_total": len(llm_pairs),
                "exact_agreement": round(equal_p / n_p, 3) if n_p else None,
                "confusion": confusion,
                "disagreements": pair_disagreements,
            },
            "notes": notes,
        },
    }
