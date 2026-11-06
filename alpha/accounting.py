# Request admission is serialized before provider work and persisted through a
# write-ahead attempt ledger. Failed/interrupted requests retain unknown usage.
# Token ceilings stop new admissions after observed usage reaches the ceiling;
# concurrent in-flight responses can exceed it. Prices are supplied estimates.

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, dataclass
import math
from time import monotonic
from typing import Any, Awaitable, Callable

from .contracts import JSON, ModelResult

JOB: ContextVar[JSON | None] = ContextVar('alpha_job', default=None)


class BudgetStop(Exception):
    pass


@dataclass(frozen=True)
class BudgetPolicy:
    max_calls: int | None = None
    max_tokens: int | None = None
    deadline_seconds: float | None = None

    def __post_init__(self):
        for value in (self.max_calls, self.max_tokens):
            if value is not None and (type(value) is not int or value < 1):
                raise ValueError('Request and reported-token budgets must be positive integers.')
        if self.deadline_seconds is not None and (not math.isfinite(self.deadline_seconds) or self.deadline_seconds <= 0):
            raise ValueError('Deadline must be a positive finite number of active seconds.')

    def to_dict(self):
        return asdict(self)


def validate_prices(prices: JSON | None) -> JSON:
    prices = deepcopy(prices or {})
    for model, rates in prices.items():
        if not isinstance(model, str) or not isinstance(rates, dict) or set(rates) != {'input', 'cached_input', 'output'}:
            raise ValueError('Each model price needs input, cached_input, and output rates per million tokens.')
        if any(type(v) not in (float, int) or not math.isfinite(v) or v < 0 for v in rates.values()):
            raise ValueError('Prices must be nonnegative finite numbers.')
    return prices


class Controller:
    def __init__(self, policy: BudgetPolicy, state: JSON, persist: Callable[[], None], prices: JSON | None = None):
        self.policy, self.state, self.persist = policy, state, persist
        self.prices = validate_prices(prices)
        self.lock = asyncio.Lock()
        self.started = monotonic()
        self.previously_elapsed = state.get('active_seconds', 0.0)
        self.stop_reason: str | None = None

    @property
    def attempts(self) -> list[JSON]:
        return self.state.setdefault('attempts', [])

    def elapsed(self) -> float:
        return self.previously_elapsed + monotonic() - self.started

    def remaining(self) -> float | None:
        return None if self.policy.deadline_seconds is None else max(0.0, self.policy.deadline_seconds - self.elapsed())

    def snapshot(self) -> None:
        self.state['active_seconds'] = self.elapsed()
        self.persist()

    def stop(self, reason: str):
        self.stop_reason = reason
        raise BudgetStop(reason)

    def check(self) -> None:
        if self.stop_reason:
            raise BudgetStop(self.stop_reason)
        remaining = self.remaining()
        if remaining is not None and remaining <= 0:
            self.stop('Active experiment deadline reached.')
        if self.policy.max_calls is not None and len(self.attempts) >= self.policy.max_calls:
            self.stop('Experiment request budget reached.')
        if self.policy.max_tokens is not None:
            tokens = sum((a.get('usage') or {}).get('input_tokens', 0) + (a.get('usage') or {}).get('output_tokens', 0) for a in self.attempts)
            if tokens >= self.policy.max_tokens:
                self.stop('Observed token budget reached; already admitted requests can overshoot the limit.')
            if any(a['status'] != 'running' and a.get('usage') is None for a in self.attempts):
                self.stop('Unknown usage after a failed/interrupted request prevents enforcing the token budget.')

    async def call(self, operation: Callable[[], Awaitable[ModelResult]], *, model: str | None, role: str | None = None) -> ModelResult:
        job = JOB.get() or {}
        async with self.lock:
            self.check()
            attempt: JSON = {'id': len(self.attempts) + 1, 'job': job.get('key'), 'role': role,
                       'model': model, 'status': 'running', 'usage': None}
            self.attempts.append(attempt)
            self.snapshot()
        started = monotonic()
        try:
            remaining = self.remaining()
            if remaining is None:
                result = await operation()
            else:
                async with asyncio.timeout(remaining):
                    result = await operation()
            usage = result.get('usage')
            if usage is not None:
                if (not isinstance(usage, dict) or any(type(usage.get(k)) is not int or usage[k] < 0 for k in ('input_tokens', 'output_tokens'))
                        or type(usage.get('cached_input_tokens', 0)) is not int
                        or not 0 <= usage.get('cached_input_tokens', 0) <= usage['input_tokens']):
                    raise ValueError('Invalid provider token usage.')
            attempt['usage'] = deepcopy(usage)
            attempt['status'] = 'completed'
            result['attempt_id'] = attempt['id']
            return result
        except TimeoutError:
            attempt['status'] = 'interrupted'
            remaining = self.remaining()
            if remaining is not None and remaining <= 0:
                self.stop_reason = 'Active experiment deadline reached.'
                raise BudgetStop(self.stop_reason) from None
            raise
        except asyncio.CancelledError:
            attempt['status'] = 'interrupted'
            raise
        except Exception:
            attempt['status'] = 'failed'
            raise
        finally:
            attempt['elapsed_ms'] = (monotonic() - started) * 1000
            self.snapshot()


def usage_summary(attempts: list[JSON], prices: JSON | None = None, *, scripted: bool = False) -> JSON:
    prices = prices or {}
    known = [a for a in attempts if a.get('usage') is not None]
    input_tokens = sum(a['usage'].get('input_tokens', 0) for a in known)
    output_tokens = sum(a['usage'].get('output_tokens', 0) for a in known)
    cached = sum(a['usage'].get('cached_input_tokens', 0) for a in known)
    estimated = 0.0
    unknown_price = 0
    for attempt in known:
        rates = prices.get(attempt.get('model') or '')
        if rates is None:
            unknown_price += 1
            continue
        usage = attempt['usage']
        hit = usage.get('cached_input_tokens', 0)
        estimated += ((usage.get('input_tokens', 0) - hit) * rates['input'] + hit * rates['cached_input']
                      + usage.get('output_tokens', 0) * rates['output']) / 1_000_000
    unknown_usage = len(attempts) - len(known)
    complete = not unknown_usage and not unknown_price and bool(attempts)
    return {'attempted_calls': len(attempts), 'completed_calls': sum(a['status'] == 'completed' for a in attempts),
            'failed_calls': sum(a['status'] == 'failed' for a in attempts),
            'interrupted_calls': sum(a['status'] in ('running', 'interrupted') for a in attempts),
            'unknown_usage_attempts': unknown_usage, 'unknown_price_attempts': unknown_price,
            'input_tokens': input_tokens if known and not scripted else None,
            'cached_input_tokens': cached if known and not scripted else None,
            'output_tokens': output_tokens if known and not scripted else None,
            'reported_tokens': input_tokens + output_tokens if known and not scripted else None,
            'usage_complete': not unknown_usage and bool(attempts) and not scripted,
            'estimated_model_cost_usd': estimated if complete and not scripted else None,
            'partial_estimated_model_cost_usd': estimated if known and not scripted else None,
            'billing_cost_usd': None, 'billing_status': 'not_observed'}


def frontier(rows: list[JSON]) -> list[str]:
    eligible = [r for r in rows if r.get('acceptable')]
    def dominates(left, right):
        if left.get('estimated_cost_usd') is not None and right.get('estimated_cost_usd') is not None:
            costs = (left['estimated_cost_usd'], right['estimated_cost_usd'])
        elif left.get('estimated_cost_usd') is None and right.get('estimated_cost_usd') is None and left.get('tokens_per_success') is not None and right.get('tokens_per_success') is not None:
            costs = (left['tokens_per_success'], right['tokens_per_success'])
        else:
            return False
        a = (-left['quality'], costs[0], left['median_ms'])
        b = (-right['quality'], costs[1], right['median_ms'])
        return all(x <= y for x, y in zip(a, b)) and any(x < y for x, y in zip(a, b))
    return [r['id'] for r in eligible if not any(dominates(other, r) for other in eligible if other is not r)]
