"""Versioned, shared instructions for human and model quality reviewers.

The rubric controls optional subjective review only. It never alters Alpha's
objective grader, candidate selection, or experiment confidence bounds.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from .contracts import ROOT


DEFAULT_RUBRIC = {
    "id": "general-output-quality",
    "version": "1.0",
    "purpose": (
        "Judge the quality of the output for the user's actual request, using "
        "only the information provided. This is subjective evidence, not proof "
        "of functional correctness."
    ),
    "criteria": [
        {
            "id": "relevance", "label": "Relevance",
            "description": "Does the output address the user's request rather than a different problem?",
        },
        {
            "id": "completeness", "label": "Completeness",
            "description": "Does it cover the important parts of the request without obvious omissions?",
        },
        {
            "id": "clarity", "label": "Clarity",
            "description": "Is it understandable, organized, and appropriately concise for the task?",
        },
        {
            "id": "instruction_following", "label": "Instruction following",
            "description": "Does it respect stated constraints, format, scope, and priorities?",
        },
        {
            "id": "helpfulness", "label": "Helpfulness",
            "description": "Does it meaningfully move the user toward the requested result?",
        },
    ],
    "anchors": {
        "1": "Poor: largely misses the request or fails major criteria.",
        "2": "Weak: some relevant material, but serious gaps prevent useful completion.",
        "3": "Adequate: mostly addresses the task, with noticeable weaknesses.",
        "4": "Good: addresses the task well, with only minor gaps.",
        "5": "Excellent: satisfies the visible request and constraints clearly and thoroughly.",
    },
    "pairwise": {
        "instruction": (
            "Apply the same criteria to both outputs for the SAME task. Pick "
            "the output that meaningfully better meets the user's needs. "
            "Do not reward extra length or polished wording by itself."
        ),
        "tie_rule": (
            "Choose tie when neither output has a meaningful overall advantage, "
            "including when both are similarly weak. Explain the strongest reason."
        ),
    },
    "limitations": (
        "Do not invent missing task context or assume an output is factually "
        "correct without evidence. Mark uncertain judgments as uncertain in "
        "the reason. Do not use hidden pass/fail outcomes or candidate identities."
    ),
}


def validate_rubric(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Rubric must be a JSON object.")
    if set(value) != {"id", "version", "purpose", "criteria", "anchors", "pairwise", "limitations"}:
        raise ValueError("Rubric keys must match the documented rubric format.")
    def meaningful(text: object) -> bool:
        return isinstance(text, str) and 0 < len(text.strip()) <= 2000
    if any(not meaningful(value[key]) for key in ("id", "version", "purpose", "limitations")):
        raise ValueError("Rubric identity and instructions must be nonempty text.")
    criteria = value["criteria"]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 12:
        raise ValueError("Rubric must have 1–12 grading criteria.")
    ids = set()
    for criterion in criteria:
        if not isinstance(criterion, dict) or set(criterion) != {"id", "label", "description"}:
            raise ValueError("Every criterion needs id, label, and description.")
        if any(not meaningful(criterion[key]) for key in ("id", "label", "description")):
            raise ValueError("Criterion fields must be nonempty text.")
        if criterion["id"] in ids:
            raise ValueError("Rubric criterion IDs must be unique.")
        ids.add(criterion["id"])
    anchors = value["anchors"]
    if not isinstance(anchors, dict) or set(anchors) != {str(n) for n in range(1, 6)}:
        raise ValueError("Rubric requires all five rating anchors.")
    if any(not meaningful(item) for item in anchors.values()):
        raise ValueError("Rating anchors must have nonempty descriptions.")
    pairwise = value["pairwise"]
    if not isinstance(pairwise, dict) or set(pairwise) != {"instruction", "tie_rule"}:
        raise ValueError("Rubric needs pairwise instruction and tie_rule.")
    if any(not meaningful(item) for item in pairwise.values()):
        raise ValueError("Pairwise guidance must have nonempty descriptions.")
    return deepcopy(value)


# Named rubrics are discovered from ./rubrics and the bundled examples.
# Operators can either name a JSON file, use the rubric's declared ID, or
# supply an explicit path. Avoid silently picking one of ambiguous matches.
_DEFAULT_NAMES = frozenset(("default", "general", "general-output-quality"))
_NAME_ALIASES = {"writing": "writing-quality"}


def _rubric_directories() -> list[Path]:
    directories = (
        Path.cwd() / "rubrics",
        ROOT / "rubrics",
        ROOT / "examples" / "rubrics",
    )
    found = []
    seen = set()
    for directory in directories:
        canonical = directory.resolve()
        if canonical not in seen and canonical.is_dir():
            seen.add(canonical)
            found.append(canonical)
    return found


def _read_rubric_file(path: Path) -> dict:
    # Prevent accidentally embedding a very large document in a model prompt.
    if path.stat().st_size > 30_000:
        raise ValueError("Rubric JSON must not exceed 30 KB.")
    return validate_rubric(json.loads(path.read_text(encoding="utf-8")))


def available_rubrics() -> list[dict]:
    """List usable rubrics found in the operator's and bundled directories."""
    entries = [{
        "name": "default", "id": DEFAULT_RUBRIC["id"],
        "path": None, "aliases": ["general", "general-output-quality"],
    }]
    for folder in _rubric_directories():
        for path in sorted(folder.glob("*.json")):
            try:
                rubric = _read_rubric_file(path)
            except (OSError, ValueError, TypeError):
                # Invalid custom files cannot be used, but must not prevent
                # selecting other valid rubrics. Explicit paths fail loudly.
                continue
            aliases = [name for name, target in _NAME_ALIASES.items()
                       if target == path.stem or target == rubric["id"]]
            entries.append({
                "name": path.stem, "id": rubric["id"],
                "path": str(path), "aliases": aliases,
            })
    return entries


def resolve_rubric(value: str | Path | None) -> Path | None:
    """Resolve a short rubric name, declared ID, or explicit JSON file path."""
    if value is None:
        return None
    name = str(value)
    if not name.strip():
        raise ValueError("Rubric name cannot be empty.")
    if name in _DEFAULT_NAMES:
        return None
    direct = Path(name).expanduser()
    # A path (with directory, extension, absolute prefix or existing file)
    # must be respected exactly; never silently fall back to a different file.
    looks_like_path = (
        direct.is_file() or direct.is_absolute() or
        "/" in name or "\\" in name or name.endswith(".json")
    )
    if looks_like_path:
        if not direct.is_file():
            raise ValueError(f"Rubric file does not exist: {direct}")
        return direct
    alias = _NAME_ALIASES.get(name, name)
    matches = {}
    for entry in available_rubrics()[1:]:
        if alias in (entry["name"], entry["id"]):
            path = Path(entry["path"])
            matches[str(path.resolve())] = path
    if len(matches) > 1:
        locations = ", ".join(sorted(matches))
        raise ValueError(
            f"Ambiguous rubric name {name!r}: {locations}. Use an explicit path."
        )
    if not matches:
        raise ValueError(
            f"Unknown rubric {name!r}. Run 'alpha rubrics' to list names, "
            "save your custom JSON under ./rubrics/<name>.json, "
            "or provide an explicit file path."
        )
    return next(iter(matches.values()))


def load_rubric(path: Path | str | None = None) -> dict:
    resolved = resolve_rubric(path)
    if resolved is None:
        return validate_rubric(DEFAULT_RUBRIC)
    return _read_rubric_file(resolved)


def rubric_record(value: dict) -> dict:
    definition = validate_rubric(value)
    canonical = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"definition": definition, "sha256": sha256(canonical.encode("utf-8")).hexdigest()}


def checked_record(record: object) -> dict:
    if not isinstance(record, dict) or set(record) != {"definition", "sha256"}:
        raise ValueError("Grading rubric identity is missing or malformed.")
    expected = rubric_record(record["definition"])
    if record["sha256"] != expected["sha256"]:
        raise ValueError("Grading rubric contents do not match the recorded hash.")
    return expected


def guidance(value: dict, *, pairwise: bool) -> str:
    rubric = validate_rubric(value)
    lines = [
        "Grading rubric: " + rubric["id"] + " (version " + rubric["version"] + ")",
        rubric["purpose"],
        "Evaluate the following criteria against the visible user request:",
    ]
    lines += [
        "- " + item["label"] + ": " + item["description"]
        for item in rubric["criteria"]
    ]
    lines += ["Shared 1–5 rating anchors:"]
    lines += [str(n) + ": " + rubric["anchors"][str(n)] for n in range(1, 6)]
    if pairwise:
        lines += ["For this A/B comparison:", rubric["pairwise"]["instruction"],
                  rubric["pairwise"]["tie_rule"]]
    else:
        lines += ["Give one overall integer rating from 1–5 using these criteria and anchors."]
    lines += [rubric["limitations"]]
    return "\n".join(lines)
