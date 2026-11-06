# Exact one-sided binomial bounds count independent task units, not assertions or
# repeated trials. Any paired regression marks a unit as regressed. The bound is
# conservative for mean quality loss; independence and representativeness remain
# assumptions of the supplied evaluation units. Frozen comparisons split alpha.

from __future__ import annotations

from dataclasses import asdict, dataclass
import math

from .contracts import JSON


@dataclass(frozen=True)
class ValidationPolicy:
    confidence: float = 0.95
    max_regression_rate: float = 0.10
    min_cases: int = 20
    min_trials: int = 2
    minimum_quality: float = 0.0

    def __post_init__(self):
        if not 0 < self.confidence < 1 or not 0 <= self.max_regression_rate < 1:
            raise ValueError('Confidence must be in (0, 1) and regression tolerance in [0, 1).')
        if type(self.min_cases) is not int or self.min_cases < 1 or type(self.min_trials) is not int or self.min_trials < 1:
            raise ValueError('Minimum cases and trials must be positive integers.')
        if not 0 <= self.minimum_quality <= 1:
            raise ValueError('Minimum quality must be in [0, 1].')

    def to_dict(self):
        return asdict(self)


def binomial_cdf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0
    if p >= 1:
        return float(k >= n)
    logs = [math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
            + i * math.log(p) + (n - i) * math.log1p(-p) for i in range(k + 1)]
    maximum = max(logs)
    return min(1.0, math.exp(maximum) * math.fsum(math.exp(x - maximum) for x in logs))


def regression_upper(k: int, n: int, alpha: float) -> float:
    if type(k) is not int or type(n) is not int or not 0 <= k <= n or not 0 < alpha < 1:
        raise ValueError('Invalid binomial bound arguments.')
    if n == 0 or k == n:
        return 1.0
    if k == 0:
        return -math.expm1(math.log(alpha) / n)
    low, high = 0.0, 1.0
    for _ in range(70):
        middle = (low + high) / 2
        if binomial_cdf(k, n, middle) > alpha:
            low = middle
        else:
            high = middle
    return high


def assess(baseline: list[JSON], candidate: list[JSON], policy: ValidationPolicy,
           *, comparisons: int = 1, scripted: bool = False) -> JSON:
    original = {(r['case_id'], r['trial']): r for r in baseline}
    changed = {(r['case_id'], r['trial']): r for r in candidate}
    if not original or len(original) != len(baseline) or len(changed) != len(candidate) or original.keys() != changed.keys():
        raise ValueError('Quality comparison requires unique, complete paired trials.')
    units: dict[str, bool] = {}
    trials: dict[str, set[int]] = {}
    regressions, improvements = [], []
    for key, base in original.items():
        run = changed[key]
        unit = str(base.get('evaluation_unit', base['case_id']))
        if run.get('evaluation_unit', run['case_id']) != unit:
            raise ValueError('Paired evaluation units differ.')
        regression = base['verdict']['passed'] and not run['verdict']['passed']
        units[unit] = units.get(unit, False) or regression
        trials.setdefault(base['case_id'], set()).add(base['trial'])
        if regression:
            regressions.append(f'{key[0]}:{key[1]}')
        if not base['verdict']['passed'] and run['verdict']['passed']:
            improvements.append(f'{key[0]}:{key[1]}')
    n, losses = len(units), sum(units.values())
    alpha = (1 - policy.confidence) / max(1, comparisons)
    upper = regression_upper(losses, n, alpha)
    quality = sum(r['verdict']['passed'] for r in candidate) / len(candidate)
    base_quality = sum(r['verdict']['passed'] for r in baseline) / len(baseline)
    reasons = []
    hard = any(r['verdict']['unsafe'] for r in candidate) or any(r.get('error') for r in candidate)
    if hard:
        reasons.append('Candidate has an unsafe outcome or execution error.')
    if quality < policy.minimum_quality:
        hard = True
        reasons.append('Candidate does not meet the minimum quality floor.')
    if n < policy.min_cases:
        reasons.append('Too few independent task units.')
    if min(map(len, trials.values())) < policy.min_trials:
        reasons.append('Too few repeated trials per case.')
    if any(r.get('error') for r in baseline):
        reasons.append('Baseline has execution errors; the comparison needs clean execution evidence.')
    if upper > policy.max_regression_rate:
        reasons.append('The upper regression bound exceeds the predeclared tolerance.')
    decision = 'rejected' if hard else 'inconclusive' if reasons else 'validated'
    if scripted:
        decision = 'scripted'
        reasons = ['Scripted responses verify mechanics, not model quality.'] + reasons
    return {'decision': decision, 'reasons': reasons, 'independent_units': n,
            'regressed_units': losses, 'observed_regression_rate': losses / n,
            'regression_upper_bound': upper, 'quality_loss_upper_bound': upper,
            'confidence': policy.confidence, 'simultaneous_comparisons': max(1, comparisons),
            'method': 'one-sided exact binomial, any paired regression per independent unit, Bonferroni',
            'quality': quality, 'baseline_quality': base_quality, 'quality_delta': quality - base_quality,
            'regressions': regressions, 'improvements': improvements,
            'functional_passed': not hard and not regressions}


def discovery_eligible(baseline: list[JSON], candidate: list[JSON], policy: ValidationPolicy) -> bool:
    result = assess(baseline, candidate, policy)
    return (not any(r.get('error') or r['verdict']['unsafe'] for r in candidate)
            and result['quality'] > 0 and result['quality'] >= policy.minimum_quality
            and result['observed_regression_rate'] <= policy.max_regression_rate)
