# Candidate recipes are explicit configurations. Changes run through the same
# fresh workflow; deterministic/conditional review uses only visible order state,
# never a grader oracle. Arbitrary combinations are supplied in candidate JSON.

from __future__ import annotations

from copy import deepcopy
from typing import cast

from .contracts import JSON, Variant
from .domain import ROLES

OPTIONS = {'models', 'prompt_overrides', 'drop_context', 'deterministic', 'conditional'}


def validate_variants(variants: list, roles: list[str], capabilities: set[str], rules: JSON | None = None) -> list[Variant]:
    rules = {"reviewer": ["refund-cap"]} if rules is None else rules
    if not variants or variants[0].get('id') != 'baseline':
        raise ValueError('Candidates must begin with baseline.')
    seen = set()
    for variant in variants:
        if (not isinstance(variant.get('id'), str) or not variant['id'] or variant['id'] in seen
                or not isinstance(variant.get('label'), str) or not isinstance(variant.get('roles'), list)):
            raise ValueError('Invalid or duplicate candidate.')
        seen.add(variant['id'])
        selected = variant['roles']
        if not selected or any(r not in roles for r in selected) or len(set(selected)) != len(selected) or selected != sorted(selected, key=roles.index):
            raise ValueError('Candidate roles must be a unique ordered subset of the baseline.')
        if set(variant) - {'id', 'label', 'roles', *OPTIONS}:
            raise ValueError('Unknown candidate setting.')
        for option in OPTIONS & variant.keys():
            if option not in capabilities or not isinstance(variant[option], dict):
                raise ValueError(f'Adapter does not support {option}.')
            for role, setting in variant[option].items():
                if role not in selected:
                    raise ValueError('Candidate setting targets a removed or unknown role.')
                if option in ('models', 'prompt_overrides') and (not isinstance(setting, str) or not setting):
                    raise ValueError('Model and prompt settings must be nonempty strings.')
                if option == 'drop_context' and (not isinstance(setting, list) or any(not isinstance(k, str) for k in setting)):
                    raise ValueError('Context drops must be lists of keys.')
                if option in ('deterministic', 'conditional') and (setting not in rules.get(role, [])):
                    raise ValueError('Unknown deterministic or conditional rule.')
        if variant['id'] == 'baseline' and OPTIONS & variant.keys():
            raise ValueError('The baseline cannot contain optimization overrides.')
    return cast(list[Variant], deepcopy(variants))


def expanded_variants(variants: list[Variant], *, model: str | None = None, support: bool = False) -> list[Variant]:
    result = deepcopy(variants)
    baseline = result[0]
    if support and 'reviewer' in baseline['roles']:
        settings_list: list[tuple[str, JSON]] = [
            ('deterministic-review', {'deterministic': {'reviewer': 'refund-cap'}}),
            ('conditional-review', {'conditional': {'reviewer': 'refund-cap'}}),
            ('concise-review', {'prompt_overrides': {'reviewer': 'Review the proposal using the supplied order and request. For refunds, cap amount_cents at the requested amount and remaining paid balance. Preserve other actions. Return action, amount_cents, and reason.'}}),
            ('less-resolver-context', {'drop_context': {'resolver': ['plan']}}),
            ('combined-review', {'roles': [r for r in baseline['roles'] if r != 'planner'], 'deterministic': {'reviewer': 'refund-cap'}, 'drop_context': {'resolver': ['plan']}}),
        ]
        for name, settings in settings_list:
            recipe = dict(deepcopy(baseline))
            recipe.update(id=name, label=name.replace('-', ' ').capitalize(), **settings)
            result.append(cast(Variant, recipe))
    if model:
        last = baseline['roles'][-1]
        result.append({**deepcopy(baseline), 'id': 'alternate-model', 'label': 'Alternate model on final role', 'models': {last: model}})
    return result


def needs_review(context: JSON) -> bool:
    order = context.get('order')
    proposal, request = context['proposal'], context['request']
    return (not order or order['risk_flag'] or order['customer_id'] != request['customer_id']
            or (proposal['action'] == 'refund' and proposal['amount_cents'] > min(request['requested_cents'], max(0, order['paid_cents'] - order['refunded_cents']))))


def deterministic_review(context: JSON) -> JSON:
    proposal = deepcopy(context['proposal'])
    order = context.get('order')
    if proposal['action'] == 'refund' and order:
        proposal['amount_cents'] = min(proposal['amount_cents'], context['request']['requested_cents'], max(0, order['paid_cents'] - order['refunded_cents']))
    return proposal
