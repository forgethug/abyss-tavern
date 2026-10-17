"""Schema-scoped, lossless spelling compatibility for AI object keys.

This is not a validator, projector, inflector or JSON repairer. The observed
single terminal ``s`` variation and exact ``desc``/``description`` aliases are
supported. Owners supply their current field
schema and must validate the returned candidate before consuming it. No plugin
rules, defaults, values or authority are inferred here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field


_DESCRIPTION_FIELD_ALIASES = {'desc': 'description', 'description': 'desc'}


def normalize_ai_description_field(value: object, *, canonical_field: str = 'desc'):
    """Normalize a description at an owner-declared current-result object.

    Canonical presence wins, including invalid values for the owner to reject.
    No value, default, historical evidence, or nested object is inferred here.
    """
    alias = _DESCRIPTION_FIELD_ALIASES[canonical_field]
    normalized = deepcopy(value)
    if isinstance(normalized, dict):
        if canonical_field not in normalized and alias in normalized:
            normalized[canonical_field] = normalized[alias]
        normalized.pop(alias, None)
    return normalized


@dataclass
class FieldNameCompatibility:
    data: object
    repairs: list[dict] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)


def normalize_ai_field_names(value: object, schema: dict) -> FieldNameCompatibility:
    """Return a new candidate using only properties declared at each exact path.

    Supports JSON Schema properties/items, local refs and unions distinguishable
    by JSON type. Ambiguous unions, dynamic mapping keys and unspecified types
    are never guessed. Constraints and optional defaults remain owner-owned.
    Terminal-s collisions/ambiguity invalidate the entire candidate, including
    equal values. For the exact description pair, an explicit canonical field
    retains ownership; the undeclared alias remains inert for owner projection.
    """
    result = FieldNameCompatibility(data=None)

    def resolve(node):
        seen = set()
        while isinstance(node, dict) and '$ref' in node:
            ref = node['$ref']
            if not isinstance(ref, str) or not ref.startswith('#/') or ref in seen:
                return {}
            seen.add(ref)
            target = schema
            for part in ref[2:].split('/'):
                target = target.get(part.replace('~1', '/').replace('~0', '~'), {})
                if not isinstance(target, dict):
                    return {}
            # Only a pure reference is safe; composed constraints belong to the owner.
            if set(node) - {'$ref', 'title', 'description', 'default'}:
                return {}
            node = target
        return node if isinstance(node, dict) else {}

    def matches_type(item, node):
        node = resolve(node)
        branches = node.get('anyOf', node.get('oneOf'))
        if isinstance(branches, list):
            return any(matches_type(item, branch) for branch in branches)
        kind = node.get('type')
        kinds = kind if isinstance(kind, list) else [kind]
        return any({
            'object': isinstance(item, dict),
            'array': isinstance(item, list),
            'string': isinstance(item, str),
            'integer': type(item) is int,
            'number': type(item) in (int, float),
            'boolean': type(item) is bool,
            'null': item is None,
        }.get(kind, False) for kind in kinds)

    def walk(item, node, path):
        node = resolve(node)
        if 'allOf' in node:
            return deepcopy(item)
        branches = node.get('anyOf', node.get('oneOf'))
        if isinstance(branches, list):
            matching = [branch for branch in branches if matches_type(item, branch)]
            if len(matching) != 1:
                return deepcopy(item)
            return walk(item, matching[0], path)
        kind = node.get('type')
        if isinstance(item, list) and (kind == 'array' or isinstance(kind, list) and 'array' in kind):
            return [walk(child, node.get('items', {}), f'{path}[{i}]')
                    for i, child in enumerate(item)]
        properties = node.get('properties')
        additional = node.get('additionalProperties')
        if not isinstance(item, dict):
            return deepcopy(item)
        if not isinstance(properties, dict):
            if isinstance(additional, dict):
                # Mapping keys are identities, not field names. Only descend
                # into values whose schema the owner explicitly supplied.
                return {key: walk(child, additional, f'{path}.{key}')
                        for key, child in item.items()}
            return deepcopy(item)

        proposals = {}
        for key, child in item.items():
            if key in properties or not isinstance(key, str):
                continue
            candidates = sorted(name for name in properties if (
                key == name + 's' or name == key + 's'
            ))
            description_target = _DESCRIPTION_FIELD_ALIASES.get(key)
            if description_target in properties:
                candidates = sorted(set(candidates) | {description_target})
            if not candidates:
                continue
            detail = {'path': f'{path}.{key}', 'before': key,
                      'candidates': candidates,
                      'rule': ('description_alias_v1' if description_target in candidates
                               else 'terminal_s_v1')}
            if len(candidates) != 1:
                result.issues.append({**detail, 'reason': 'ambiguous_field_name'})
                continue
            target = candidates[0]
            detail['after'] = target
            if target in item:
                if detail['rule'] == 'description_alias_v1':
                    # An unused extra must not make an already explicit
                    # canonical value fail; validation still checks that value.
                    continue
                result.issues.append({**detail, 'reason': 'canonical_and_alias_present'})
            elif not matches_type(child, properties[target]):
                result.issues.append({**detail, 'reason': 'incompatible_or_unspecified_type'})
            else:
                proposals.setdefault(target, []).append((key, detail))

        renames = {}
        for target, sources in proposals.items():
            if len(sources) != 1:
                result.issues.extend({**detail, 'reason': 'multiple_aliases'}
                                     for _, detail in sources)
                continue
            source, detail = sources[0]
            renames[source] = target
            result.repairs.append({key: item for key, item in detail.items() if key != 'candidates'})
        return {renames.get(key, key): walk(child, properties.get(
                                              renames.get(key, key),
                                              additional if isinstance(additional, dict) else {}),
                                          f'{path}.{renames.get(key, key)}')
                for key, child in item.items()}

    candidate = walk(value, schema, '$')
    if not result.issues:
        result.data = candidate
    return result
