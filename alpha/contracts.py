# Version-one JSON contracts stay compatible with saved experiments. Runtime role
# validation lives in domain.py; type annotations document the adapter boundary.

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sysconfig
from typing import Any, NotRequired, Protocol, TypedDict

SOURCE_ROOT = Path(__file__).resolve().parent.parent
ROOT = SOURCE_ROOT if (SOURCE_ROOT / "fleet.config.json").is_file() else Path(sysconfig.get_path("data")) / "share/alpha-agent-lab"
JSON = dict[str, Any]


class Variant(TypedDict):
    id: str
    label: str
    roles: list[str]
    models: NotRequired[dict[str, str]]
    prompt_overrides: NotRequired[dict[str, str]]
    drop_context: NotRequired[dict[str, list[str]]]
    deterministic: NotRequired[dict[str, str]]
    conditional: NotRequired[dict[str, str]]


class Job(TypedDict):
    test_case: JSON
    variant: Variant
    trial: int


class Usage(TypedDict):
    input_tokens: int
    output_tokens: int
    cached_input_tokens: NotRequired[int]


class ModelResult(TypedDict):
    output: JSON
    usage: Usage | None
    model_calls: int
    attempt_id: NotRequired[int]


class Step(TypedDict):
    id: NotRequired[str]
    parent_ids: NotRequired[list[str]]
    model: NotRequired[str | None]
    execution: NotRequired[str]
    attempt_id: NotRequired[int]
    role: str
    context: JSON
    output: NotRequired[JSON]
    error: NotRequired[str]
    elapsed_ms: float
    usage: Usage | None
    model_calls: int


class Verdict(TypedDict):
    passed: bool
    unsafe: bool
    checks: list[JSON]
    results: NotRequired[list[JSON]]
    execution_error: NotRequired[str]


class Run(TypedDict):
    evaluation_unit: NotRequired[str]
    attempts: NotRequired[list[JSON]]
    previous_record_sha256: NotRequired[str]
    record_sha256: NotRequired[str]
    case_id: str
    category: str
    split: str
    variant: str
    trial: int
    trace: list[Step]
    tools: list[JSON]
    final_state: JSON
    verdict: Verdict
    error: str | None
    elapsed_ms: float


class Experiment(TypedDict):
    decision: NotRequired[str]
    status: NotRequired[str]
    stop_reason: NotRequired[str | None]
    policy: NotRequired[JSON]
    assessments: NotRequired[JSON]
    frozen_candidates: NotRequired[list[str]]
    recommendations: NotRequired[JSON]
    tradeoffs: NotRequired[list[JSON]]
    frontier: NotRequired[list[str]]
    accounting: NotRequired[JSON]
    attempts: NotRequired[list[JSON]]
    manifest_sha256: NotRequired[str]
    version: int
    id: str
    created_at: str
    backend: str
    model: str | None
    fixture: bool
    trials: int
    concurrency: int
    fleet: JSON | None
    dataset: JSON
    prompt_sha256: str
    variants: list[Variant]
    selected: str | None
    frozen_at: str
    validated: bool
    comparison: JSON | None
    runs: list[Run]


class Backend(Protocol):
    name: str
    model: str | None

    async def call(self, role: str, instructions: str, context: JSON,
                   *, signal: asyncio.Event | None = None) -> ModelResult: ...


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
