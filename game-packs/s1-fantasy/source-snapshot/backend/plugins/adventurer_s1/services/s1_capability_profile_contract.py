"""Mechanical contracts for S1 encounter and long-term actor capability profiles."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)


CAPABILITY_AXIS_KEYS = frozenset({
    'force', 'mobility', 'cognition', 'durability', 'perception',
})
CAPABILITY_RANKS = frozenset({
    'very_low', 'low', 'baseline', 'high', 'very_high', 'exceptional',
})
CAPABILITY_CONDITIONS = frozenset({'impaired', 'normal', 'enhanced'})
CAPABILITY_NUMBER_PRESSURES = frozenset({'none', 'low', 'medium', 'high', 'overwhelming'})
ACTOR_CAPABILITY_PROFILE_KEYS = frozenset({
    'basis_level', 'typical_profile', 'current_profile', 'condition',
    'number_pressure', 'capabilities', 'limitations',
})

# Field locations/types only. The existing normalizer below still owns values,
# numeric normalization, completeness and all capability semantics.
ACTOR_CAPABILITY_FIELD_SCHEMA = {
    'type': 'object',
    'properties': {
        **{key: {'type': 'string'} for key in ACTOR_CAPABILITY_PROFILE_KEYS},
        'basis_level': {'type': ['integer', 'string']},
        **{key: {'type': 'object', 'properties': {
            axis: {'type': 'string'} for axis in CAPABILITY_AXIS_KEYS
        }} for key in ('typical_profile', 'current_profile')},
        **{key: {'type': 'array', 'items': {'type': 'string'}}
           for key in ('capabilities', 'limitations')},
    },
}

ENCOUNTER_DEFINITION_FIELD = '_encounter_definition'


def _encounter_profiles_digest(outline: dict) -> str:
    return hashlib.sha256(json.dumps(
        outline.get('encounter_capability_profiles') or [],
        ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()


def freeze_encounter_definition(
    outline: dict, *, member_description_projection: dict,
) -> None:
    """Attach provenance after outline validation, before narrative generation.

    This is a definition-stage binding, not a model verdict that difficulty passed.
    The outline parser never accepts this private field from model output.
    """
    outline[ENCOUNTER_DEFINITION_FIELD] = {
        'schema_version': 1,
        'profiles_sha256': _encounter_profiles_digest(outline),
        'member_description_projection': deepcopy(member_description_projection),
    }


def project_encounter_definition_for_ai(outline: object) -> dict:
    """Expose only a valid definition binding; keep player cache receipts private."""
    if not isinstance(outline, dict):
        return {}
    binding = outline.get(ENCOUNTER_DEFINITION_FIELD)
    if (
        not isinstance(binding, dict)
        or type(binding.get('schema_version')) is not int
        or binding['schema_version'] != 1
        or binding.get('profiles_sha256') != _encounter_profiles_digest(outline)
    ):
        return {}
    return {'schema_version': 1}


def is_capability_axes(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    value = {key: value[key] for key in CAPABILITY_AXIS_KEYS if key in value}
    return (
        frozenset(value) == CAPABILITY_AXIS_KEYS
        and all(rank in CAPABILITY_RANKS for rank in value.values())
    )


def normalize_actor_capability_profile(value: object) -> dict[str, Any] | None:
    """Return a canonical profile only when the complete mechanical shape is valid."""
    if not isinstance(value, dict):
        return None
    value = {key: value[key] for key in ACTOR_CAPABILITY_PROFILE_KEYS if key in value}
    if frozenset(value) != ACTOR_CAPABILITY_PROFILE_KEYS:
        return None
    basis_level, _ = normalize_ascii_integer(value.get('basis_level'), minimum=1)
    if type(basis_level) is not int or basis_level < 1:
        return None
    if not is_capability_axes(value.get('typical_profile')):
        return None
    if not is_capability_axes(value.get('current_profile')):
        return None
    if value.get('condition') not in CAPABILITY_CONDITIONS:
        return None
    if value.get('number_pressure') not in CAPABILITY_NUMBER_PRESSURES:
        return None
    normalized: dict[str, Any] = {
        'basis_level': basis_level,
        'typical_profile': {
            key: value['typical_profile'][key] for key in CAPABILITY_AXIS_KEYS
        },
        'current_profile': {
            key: value['current_profile'][key] for key in CAPABILITY_AXIS_KEYS
        },
        'condition': value['condition'],
        'number_pressure': value['number_pressure'],
    }
    for key in ('capabilities', 'limitations'):
        items = value.get(key)
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            return None
        normalized[key] = [item.strip() for item in items]
    return normalized
