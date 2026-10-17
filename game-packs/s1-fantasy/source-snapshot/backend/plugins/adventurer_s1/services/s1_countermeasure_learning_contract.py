"""Mechanical contract for durable S1 adaptive-learning records."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)
from plugins.adventurer_s1.services.s1_countermeasure_evidence import (
    bind_final_event_evidence,
    normalize_evidence_quotes,
)


COUNTERMEASURE_LEARNING_FIELD = 'countermeasure_learning'
COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD = '_countermeasure_learning_owner'
COUNTERMEASURE_LEARNING_OWNER_SCHEMA_VERSION = 2
# Shared by the generation handoff and learning input/output boundaries.
# These values belong to the persisted encounter, never to an AI response.
COUNTERMEASURE_LEARNING_FROZEN_FIELDS = (
    'objective_outline', 'objective_world_intel_injection',
    'generation_world_intel_injection', 'audit_member_snapshot', '_member_id_to_name',
)
COUNTERMEASURE_LEARNING_MAX_RECORDS = 96
COUNTERMEASURE_SOURCE_KINDS = frozenset({
    'equipment', 'skill', 'trait', 'state', 'event',
})
COUNTERMEASURE_KNOWLEDGE_LEVELS = frozenset({
    'observed', 'understood', 'prepared',
})
COUNTERMEASURE_SOURCE_REF_KEYS = frozenset({
    'adventurer_id',
    'source_kind',
    'source_name',
    'source_asset_id',
})
COUNTERMEASURE_LEARNING_KEYS = frozenset({
    'observer_world_actor_id',
    'observer_capability_revision',
    'matched_knowledge_id',
    'source_refs',
    'encountered_method',
    'unresolved_reason',
    'evidence_event_indexes',
    'evidence_event_quotes',
    'observed_effect',
    'learned_boundary',
    'countermeasure_direction',
    'knowledge_level',
})
COUNTERMEASURE_NON_BLOCKING_DIAGNOSTICS = frozenset({
    'record_target_exceeded_preserved',
})
_MAX_DB_INTEGER = 2_147_483_647


def refresh_learning_frozen_context(target: dict, persisted: dict) -> None:
    """Refresh every learning authority after generation, without aliasing it."""
    for key in COUNTERMEASURE_LEARNING_FROZEN_FIELDS:
        if persisted.get(key) is not None:
            target[key] = deepcopy(persisted[key])


def _positive_int(value: object) -> int | None:
    normalized, _ = normalize_ascii_integer(
        value,
        minimum=1,
        maximum=_MAX_DB_INTEGER,
    )
    if type(normalized) is not int or not 1 <= normalized <= _MAX_DB_INTEGER:
        return None
    return normalized


def _non_negative_int(value: object) -> int | None:
    normalized, _ = normalize_ascii_integer(
        value,
        minimum=0,
        maximum=_MAX_DB_INTEGER,
    )
    if type(normalized) is not int or not 0 <= normalized <= _MAX_DB_INTEGER:
        return None
    return normalized


def _clean_text(value: object, limit: int, *, allow_empty: bool = False) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    if len(clean) > max(0, int(limit)):
        return None
    if clean or allow_empty:
        return clean
    return None


def _normalize_source_refs(value: object) -> tuple[list[dict] | None, str | None]:
    if not isinstance(value, list) or not value:
        return None, 'source_refs_not_non_empty_array'
    normalized: list[dict] = []
    seen: set[tuple] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            return None, f'source_refs[{index}].not_object'
        projected = {
            key: raw[key]
            for key in COUNTERMEASURE_SOURCE_REF_KEYS
            if key in raw
        }
        required_keys = COUNTERMEASURE_SOURCE_REF_KEYS - {'source_asset_id'}
        if not required_keys.issubset(projected):
            return None, f'source_refs[{index}].required_fields_missing'
        adventurer_id = _positive_int(projected.get('adventurer_id'))
        source_kind = projected.get('source_kind')
        source_name = _clean_text(projected.get('source_name'), 120)
        if adventurer_id is None:
            return None, f'source_refs[{index}].adventurer_id_invalid'
        if not isinstance(source_kind, str) or source_kind not in COUNTERMEASURE_SOURCE_KINDS:
            return None, f'source_refs[{index}].source_kind_invalid'
        if source_name is None:
            return None, f'source_refs[{index}].source_name_invalid'
        raw_asset_id = projected.get('source_asset_id')
        asset_id = _positive_int(raw_asset_id) if raw_asset_id is not None else None
        if raw_asset_id is not None and (
            asset_id is None or source_kind in {'state', 'event'}
        ):
            return None, f'source_refs[{index}].source_asset_id_invalid'
        key = (adventurer_id, source_kind, asset_id or source_name.casefold())
        if key in seen:
            return None, f'source_refs[{index}].duplicate'
        seen.add(key)
        normalized.append({
            'adventurer_id': adventurer_id,
            'source_kind': source_kind,
            'source_name': source_name,
            **({'source_asset_id': asset_id} if 'source_asset_id' in projected else {}),
        })
    return normalized, None


def _normalize_event_indexes(value: object) -> tuple[list[int] | None, str | None]:
    if not isinstance(value, list):
        return None, 'evidence_event_indexes_not_array'
    normalized: list[int] = []
    for index, raw in enumerate(value):
        event_index = _non_negative_int(raw)
        if event_index is None:
            return None, f'evidence_event_indexes[{index}].invalid'
        if event_index in normalized:
            return None, f'evidence_event_indexes[{index}].duplicate'
        normalized.append(event_index)
    return normalized, None


def _normalize_row(
    value: object,
    *,
    field_chars: int,
) -> tuple[dict | None, str | None]:
    if not isinstance(value, dict):
        return None, 'not_object'
    projected = {
        key: value[key]
        for key in COUNTERMEASURE_LEARNING_KEYS
        if key in value
    }
    required_keys = COUNTERMEASURE_LEARNING_KEYS - {'evidence_event_quotes'}
    if not required_keys.issubset(projected):
        return None, 'required_fields_missing'

    observer_id = _positive_int(projected.get('observer_world_actor_id'))
    capability_revision = _positive_int(projected.get('observer_capability_revision'))
    matched_knowledge_id = projected.get('matched_knowledge_id')
    if observer_id is None:
        return None, 'observer_world_actor_id_invalid'
    if capability_revision is None:
        return None, 'observer_capability_revision_invalid'
    if matched_knowledge_id is not None:
        matched_knowledge_id = _positive_int(matched_knowledge_id)
        if matched_knowledge_id is None:
            return None, 'matched_knowledge_id_invalid'

    source_refs, source_refs_error = _normalize_source_refs(projected.get('source_refs'))
    if source_refs is None:
        return None, source_refs_error
    event_indexes, event_indexes_error = _normalize_event_indexes(
        projected.get('evidence_event_indexes'),
    )
    if event_indexes is None:
        return None, event_indexes_error

    evidence_quotes = None
    if 'evidence_event_quotes' in projected:
        evidence_quotes = normalize_evidence_quotes(
            projected['evidence_event_quotes'], event_indexes,
        )
        if evidence_quotes is None:
            return None, 'evidence_event_quotes_invalid'

    knowledge_level = projected.get('knowledge_level')
    if not isinstance(knowledge_level, str) or knowledge_level not in COUNTERMEASURE_KNOWLEDGE_LEVELS:
        return None, 'knowledge_level_invalid'

    encountered_method = _clean_text(
        projected.get('encountered_method'),
        field_chars,
    )
    unresolved_reason = _clean_text(
        projected.get('unresolved_reason'),
        field_chars,
        allow_empty=True,
    )
    observed_effect = _clean_text(projected.get('observed_effect'), field_chars)
    learned_boundary = _clean_text(projected.get('learned_boundary'), field_chars)
    countermeasure_direction = _clean_text(
        projected.get('countermeasure_direction'),
        field_chars,
        allow_empty=True,
    )
    if encountered_method is None:
        return None, 'encountered_method_invalid_or_too_long'
    if unresolved_reason is None:
        return None, 'unresolved_reason_invalid_or_too_long'
    if observed_effect is None:
        return None, 'observed_effect_invalid_or_too_long'
    if learned_boundary is None:
        return None, 'learned_boundary_invalid_or_too_long'
    if countermeasure_direction is None:
        return None, 'countermeasure_direction_invalid_or_too_long'
    if knowledge_level == 'observed' and countermeasure_direction:
        return None, 'observed_must_not_claim_countermeasure_direction'
    if knowledge_level == 'prepared' and not countermeasure_direction:
        return None, 'advanced_level_requires_countermeasure_direction'

    return {
        'observer_world_actor_id': observer_id,
        'observer_capability_revision': capability_revision,
        'matched_knowledge_id': matched_knowledge_id,
        'source_refs': source_refs,
        'encountered_method': encountered_method,
        'unresolved_reason': unresolved_reason,
        'evidence_event_indexes': event_indexes,
        **({'evidence_event_quotes': evidence_quotes} if evidence_quotes is not None else {}),
        'observed_effect': observed_effect,
        'learned_boundary': learned_boundary,
        'countermeasure_direction': countermeasure_direction,
        'knowledge_level': knowledge_level,
    }, None


def normalize_countermeasure_learning(
    value: object,
    *,
    max_records: int = COUNTERMEASURE_LEARNING_MAX_RECORDS,
    field_chars: int = 240,
    final_events: list | None = None,
) -> tuple[list[dict], list[str]]:
    """Project, validate and deduplicate generation-side records.

    Invalid rows are reported for Observer repair. The normal record target is
    advisory: valid independent rows beyond it are preserved instead of being
    truncated.
    """
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['not_array']
    normalized: list[dict] = []
    diagnostics: list[str] = []
    seen: set[tuple] = set()
    for index, raw in enumerate(value):
        row, error = _normalize_row(
            raw,
            field_chars=max(80, int(field_chars)),
        )
        if row is None:
            diagnostics.append(f'[{index}].{error}')
            continue
        if final_events is not None and 'evidence_event_quotes' in row:
            bound_indexes, evidence_error = bind_final_event_evidence(
                row['evidence_event_indexes'], row['evidence_event_quotes'], final_events,
                allow_rebind=True,
            )
            if evidence_error:
                diagnostics.append(f'[{index}].{evidence_error}')
                continue
            row['evidence_event_indexes'] = bound_indexes
        key = (
            row['observer_world_actor_id'],
            tuple(
                (
                    ref['adventurer_id'],
                    ref['source_kind'],
                    ref['source_name'].casefold(),
                    ref.get('source_asset_id'),
                )
                for ref in row['source_refs']
            ),
            row['encountered_method'].casefold(),
            tuple(row['evidence_event_indexes']),
        )
        if key in seen:
            diagnostics.append(f'[{index}].duplicate')
            continue
        seen.add(key)
        normalized.append(row)
    if len(value) > max(0, int(max_records)):
        diagnostics.append('record_target_exceeded_preserved')
    return normalized, diagnostics


def blocking_countermeasure_diagnostics(diagnostics: list[str]) -> list[str]:
    return [
        diagnostic
        for diagnostic in diagnostics
        if diagnostic not in COUNTERMEASURE_NON_BLOCKING_DIAGNOSTICS
    ]


def validate_countermeasure_learning_strict(
    value: object,
    *,
    max_records: int = COUNTERMEASURE_LEARNING_MAX_RECORDS,
    field_chars: int = 240,
    final_events: list | None = None,
) -> tuple[list[dict], list[str]]:
    normalized, diagnostics = normalize_countermeasure_learning(
        value,
        max_records=max_records,
        field_chars=field_chars,
        final_events=final_events,
    )
    blocking = blocking_countermeasure_diagnostics(diagnostics)
    if not isinstance(value, list):
        return normalized, blocking
    if blocking or len(normalized) != len(value):
        return normalized, blocking or ['records_not_preserved']
    return normalized, []


def countermeasure_fingerprint(row: dict) -> str:
    """Build the provisional identity for a newly observed mechanism.

    Stable ``matched_knowledge_id`` is the authority for semantic reuse.  This
    fallback only deduplicates new records whose structured mechanism, effect,
    and boundary summaries are already identical. Exact source names are
    mechanically neutralized if they were copied into any identity summary;
    source/player/asset names are provenance and must never become the learned
    mechanism's identity.
    """
    identity_texts = {
        field_name: str(row.get(field_name) or '').casefold()
        for field_name in (
            'encountered_method',
            'observed_effect',
            'learned_boundary',
        )
    }
    source_names = sorted({
        str(ref.get('source_name') or '').strip().casefold()
        for ref in row.get('source_refs') or []
        if isinstance(ref, dict) and str(ref.get('source_name') or '').strip()
    }, key=len, reverse=True)
    for source_name in source_names:
        for field_name, field_value in identity_texts.items():
            identity_texts[field_name] = field_value.replace(
                source_name,
                '<source>',
            )
    payload = {
        'schema': 'semantic-mechanism-v2',
        **identity_texts,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()
