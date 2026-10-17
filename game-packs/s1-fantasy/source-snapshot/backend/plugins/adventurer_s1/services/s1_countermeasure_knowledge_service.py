"""Persist and inject bounded S1 world-actor countermeasure knowledge."""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import and_, case, func, or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1ExpeditionObserverReview,
    AdventurerS1WorldActor,
    AdventurerS1WorldActorFactionRelation,
    AdventurerS1WorldCountermeasureKnowledge,
)
from plugins.adventurer_s1.services.s1_countermeasure_evidence import (
    bind_final_event_evidence,
    normalize_evidence_quotes,
)
from plugins.adventurer_s1.services.s1_countermeasure_learning_contract import (
    COUNTERMEASURE_LEARNING_FIELD,
    COUNTERMEASURE_LEARNING_KEYS,
    COUNTERMEASURE_LEARNING_MAX_RECORDS,
    COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD,
    COUNTERMEASURE_LEARNING_OWNER_SCHEMA_VERSION,
    blocking_countermeasure_diagnostics,
    countermeasure_fingerprint,
    normalize_countermeasure_learning,
)


_RESOURCE_SNAPSHOT_FIELDS = {
    'equipment': ('equipment', 'equipments'),
    'skill': ('skills',),
    'trait': ('traits',),
}
_RESOURCE_UPDATE_FIELDS = {
    'equipment': ('new_equipments',),
    'skill': ('new_skills',),
    'trait': ('new_traits',),
}
_KNOWLEDGE_LEVEL_RANK = {
    'observed': 1,
    'understood': 2,
    'prepared': 3,
}
COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD = (
    '_countermeasure_application_receipts'
)
COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD = (
    '_countermeasure_learning_disposition'
)
_COUNTERMEASURE_APPLICATION_DECISIONS = frozenset({
    'behavior_informed', 'no_matching_exposure', 'no_response_opportunity',
    'behavior_not_reflected',
})
_COUNTERMEASURE_APPLICATION_RECEIPT_LEGACY_VERSION = 1
_COUNTERMEASURE_APPLICATION_RECEIPT_DUAL_VERSION = 2
_COUNTERMEASURE_APPLICATION_RECEIPT_VERSION = 3
COUNTERMEASURE_KNOWLEDGE_SEMANTIC_FIELDS = (
    'encountered_method',
    'unresolved_reason',
    'observed_effect',
    'learned_boundary',
    'countermeasure_direction',
)
COUNTERMEASURE_PROMPT_TEMPLATE_SLOT = '{{S1_COUNTERMEASURE_PROMPT_BLOCK}}'


class CountermeasureLearningPersistenceError(RuntimeError):
    """Raised when mandatory approved learning cannot be persisted losslessly."""


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _contract_limits() -> tuple[int, int]:
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    field_key = 'ADVENTURER_S1_COUNTERMEASURE_LEARNING_FIELD_MAX_CHARS'
    field_default = AdventurerS1Plugin.default_config[field_key]
    return (
        max(0, _coerce_int(current_app.config.get(
            'ADVENTURER_S1_COUNTERMEASURE_LEARNING_MAX_RECORDS',
            COUNTERMEASURE_LEARNING_MAX_RECORDS,
        ), COUNTERMEASURE_LEARNING_MAX_RECORDS)),
        max(80, _coerce_int(current_app.config.get(
            field_key, field_default,
        ), field_default)),
    )


def normalize_for_runtime(value: object, *, final_events: list | None = None) -> tuple[list[dict], list[str]]:
    max_records, field_chars = _contract_limits()
    normalized, diagnostics = normalize_countermeasure_learning(
        value,
        max_records=max_records,
        field_chars=field_chars,
        final_events=final_events,
    )
    return normalized, blocking_countermeasure_diagnostics(diagnostics)


def _merged_full_log(
    final_result_data: dict | None,
    cached_full_log: dict | None = None,
) -> dict:
    merged = dict(cached_full_log or {}) if isinstance(cached_full_log, dict) else {}
    if isinstance(final_result_data, dict):
        merged.update(final_result_data)
    return merged


def objective_actor_revisions(objective_outline: object) -> dict[int, int]:
    """Return the stable actor/revision whitelist frozen by the objective outline."""
    outline = objective_outline
    if not isinstance(outline, dict):
        return {}
    result: dict[int, int] = {}
    for profile in outline.get('encounter_capability_profiles') or []:
        if not isinstance(profile, dict) or profile.get('scope') != 'individual':
            continue
        raw_actor_id = profile.get('world_actor_id')
        raw_revision = profile.get('capability_revision')
        if isinstance(raw_actor_id, bool) or isinstance(raw_revision, bool):
            continue
        actor_id = _coerce_int(raw_actor_id)
        revision = _coerce_int(raw_revision)
        if actor_id > 0 and revision > 0:
            result[actor_id] = revision
    return result


def _objective_actor_revisions(full_log: dict) -> dict[int, int]:
    return objective_actor_revisions(full_log.get('objective_outline'))


def _frozen_actor_faction_keys(full_log: dict) -> dict[int, set[str]]:
    result: dict[int, set[str]] = {}
    for snapshot_key in (
        'generation_world_intel_injection',
        'objective_world_intel_injection',
    ):
        snapshot = full_log.get(snapshot_key)
        if not isinstance(snapshot, dict):
            continue
        for actor_state in snapshot.get('world_actor_states') or []:
            if not isinstance(actor_state, dict):
                continue
            actor_id = _coerce_int(actor_state.get('id') or actor_state.get('world_actor_id'))
            faction_keys = actor_state.get('faction_keys')
            if (
                actor_id <= 0
                or actor_id in result
                or not isinstance(faction_keys, list)
            ):
                continue
            result[actor_id] = {
                str(key).strip()
                for key in faction_keys
                if str(key or '').strip()
            }
    return result


def _frozen_injected_knowledge_ids(full_log: dict) -> dict[int, set[int]]:
    """Return exact per-actor knowledge ids exposed to the generating call."""
    result: dict[int, set[int]] = {}
    generation_snapshot = full_log.get('generation_world_intel_injection')
    snapshot = (
        generation_snapshot
        if isinstance(generation_snapshot, dict)
        else full_log.get('objective_world_intel_injection')
    )
    if isinstance(snapshot, dict):
        for entry in snapshot.get('world_actor_prompt_entries') or []:
            if not isinstance(entry, dict):
                continue
            actor_id = _coerce_int(entry.get('world_actor_id'))
            raw_ids = entry.get('countermeasure_knowledge_ids')
            if actor_id <= 0 or not isinstance(raw_ids, list):
                continue
            result[actor_id] = {
                knowledge_id
                for knowledge_id in (_coerce_int(value) for value in raw_ids)
                if knowledge_id > 0
            }
    return result


def _member_ids(full_log: dict) -> set[int]:
    ids: set[int] = set()
    for row in full_log.get('audit_member_snapshot') or []:
        if isinstance(row, dict):
            member_id = _coerce_int(row.get('adventurer_id') or row.get('id'))
            if member_id > 0:
                ids.add(member_id)
    for row in full_log.get('member_updates') or []:
        if isinstance(row, dict):
            member_id = _coerce_int(row.get('id'))
            if member_id > 0:
                ids.add(member_id)
    return ids


def _names_from_value(value: object) -> set[str]:
    if isinstance(value, dict):
        return {
            str(name).strip().casefold()
            for name in value
            if str(name or '').strip()
        }
    if not isinstance(value, list):
        return set()
    names: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            name = item.get('name')
        else:
            name = item
        clean = str(name or '').strip()
        if clean:
            names.add(clean.casefold())
    return names


def _known_resource_names(full_log: dict, adventurer_id: int, source_kind: str) -> set[str]:
    names: set[str] = set()
    for row in full_log.get('audit_member_snapshot') or []:
        if not isinstance(row, dict):
            continue
        row_id = _coerce_int(row.get('adventurer_id') or row.get('id'))
        if row_id != adventurer_id:
            continue
        for field in _RESOURCE_SNAPSHOT_FIELDS.get(source_kind, ()):
            names.update(_names_from_value(row.get(field)))
    for row in full_log.get('member_updates') or []:
        if not isinstance(row, dict) or _coerce_int(row.get('id')) != adventurer_id:
            continue
        for field in _RESOURCE_UPDATE_FIELDS.get(source_kind, ()):
            names.update(_names_from_value(row.get(field)))
    return names


def _frozen_source_asset_name(full_log: dict, source_ref: dict) -> str | None:
    """Resolve an explicit asset identity only inside its owner's departure snapshot."""
    from plugins.adventurer_s1.services.s1_item_use_condition_service import (
        EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD,
    )

    member_rows = [
        row for row in full_log.get('audit_member_snapshot') or []
        if isinstance(row, dict)
        and _coerce_int(row.get('adventurer_id') or row.get('id'))
        == source_ref['adventurer_id']
    ]
    if len(member_rows) != 1:
        return None
    matches = []
    for field in _RESOURCE_SNAPSHOT_FIELDS.get(source_ref['source_kind'], ()):
        for asset in member_rows[0].get(field) or []:
            if not isinstance(asset, dict):
                continue
            if _coerce_int(asset.get('asset_id')) != source_ref['source_asset_id']:
                continue
            if (
                asset.get(EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD) is True
                or asset.get('equipped') is False
                or asset.get('is_archived') is True
            ):
                return None
            matches.append(str(asset.get('name') or '').strip())
    if len(matches) != 1 or not matches[0]:
        return None
    return matches[0]


def _source_ref_is_authoritative(full_log: dict, source_ref: dict) -> bool:
    source_kind = source_ref['source_kind']
    if source_kind in {'state', 'event'}:
        return True
    if source_ref.get('source_asset_id') is not None:
        return _frozen_source_asset_name(full_log, source_ref) == source_ref['source_name']
    return source_ref['source_name'].casefold() in _known_resource_names(
        full_log,
        source_ref['adventurer_id'],
        source_kind,
    )


def _has_independent_learning_owner(full_log: dict) -> bool:
    receipt = full_log.get(COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD)
    return isinstance(receipt, dict) and receipt.get('schema_version') == COUNTERMEASURE_LEARNING_OWNER_SCHEMA_VERSION


def _learning_contract_key(row: dict, *, complete: bool = False) -> tuple:
    if complete:
        return (json.dumps(
            {key: row[key] for key in COUNTERMEASURE_LEARNING_KEYS if key in row},
            ensure_ascii=False, sort_keys=True,
        ),)
    return (
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


def _validate_normalized_learning_rows(
    *,
    full_log: dict,
    indexed_rows: list[tuple[int, dict]],
) -> dict[int, list[str]]:
    """Batch-check normalized rows while retaining their original indexes."""
    errors_by_index = {index: [] for index, _row in indexed_rows}
    actor_revisions = _objective_actor_revisions(full_log)
    frozen_faction_keys = _frozen_actor_faction_keys(full_log)
    injected_knowledge_ids = _frozen_injected_knowledge_ids(full_log)
    member_ids = _member_ids(full_log)
    events = full_log.get('events')
    if not isinstance(events, list):
        events = []
    actor_ids = {
        row['observer_world_actor_id']
        for _index, row in indexed_rows
        if row['observer_world_actor_id'] in actor_revisions
    }
    existing_actor_ids = {
        int(actor_id)
        for actor_id, in (
            db_manager.query(AdventurerS1WorldActor.id)
            .filter(AdventurerS1WorldActor.id.in_(actor_ids))
            .all()
            if actor_ids else []
        )
    }
    matched_knowledge_ids = {
        int(row['matched_knowledge_id'])
        for _index, row in indexed_rows
        if row.get('matched_knowledge_id') is not None
    }
    matched_knowledge_by_id = {
        int(knowledge.id): knowledge
        for knowledge in (
            db_manager.query(AdventurerS1WorldCountermeasureKnowledge)
            .filter(AdventurerS1WorldCountermeasureKnowledge.id.in_(
                matched_knowledge_ids,
            ))
            .all()
            if matched_knowledge_ids else []
        )
    }
    for index, row in indexed_rows:
        row_errors = errors_by_index[index]
        actor_id = row['observer_world_actor_id']
        if actor_revisions.get(actor_id) != row['observer_capability_revision']:
            row_errors.append('observer_identity_revision_mismatch')
        elif actor_id not in existing_actor_ids:
            row_errors.append('observer_world_actor_missing')
        matched_knowledge_id = row.get('matched_knowledge_id')
        if matched_knowledge_id is not None:
            matched_knowledge = matched_knowledge_by_id.get(
                int(matched_knowledge_id),
            )
            allowed_audience_keys = {f'actor:{actor_id}'}
            allowed_audience_keys.update(
                f'faction:{faction_key}'
                for faction_key in frozen_faction_keys.get(actor_id, set())
            )
            if matched_knowledge is None:
                row_errors.append('matched_knowledge_missing')
            elif matched_knowledge.audience_key not in allowed_audience_keys:
                row_errors.append('matched_knowledge_not_accessible')
            elif int(matched_knowledge_id) not in injected_knowledge_ids.get(
                actor_id,
                set(),
            ):
                row_errors.append('matched_knowledge_not_injected')
            else:
                row['_matched_learning_fingerprint'] = (
                    matched_knowledge.learning_fingerprint
                )
        for ref_index, source_ref in enumerate(row['source_refs']):
            if source_ref['adventurer_id'] not in member_ids:
                row_errors.append(
                    f'source_refs[{ref_index}].adventurer_not_in_frozen_party'
                )
            elif not _source_ref_is_authoritative(full_log, source_ref):
                row_errors.append(
                    f'source_refs[{ref_index}].source_name_not_authoritative'
                )
        for event_index in row['evidence_event_indexes']:
            if event_index >= len(events):
                row_errors.append('evidence_event_index_out_of_range')
                break
        if 'evidence_event_quotes' in row:
            _, evidence_error = bind_final_event_evidence(
                row['evidence_event_indexes'], row['evidence_event_quotes'], events,
            )
            if evidence_error:
                row_errors.append(evidence_error)
    return errors_by_index


def validate_learning_for_persistence(
    *,
    final_result_data: dict | None,
    cached_full_log: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """Validate approved learning against frozen identities and exact sources."""
    full_log = _merged_full_log(final_result_data, cached_full_log)
    rows, diagnostics = normalize_for_runtime(
        full_log.get(COUNTERMEASURE_LEARNING_FIELD),
    )
    errors = [
        f'countermeasure_learning{diagnostic}'
        for diagnostic in diagnostics
    ]
    row_errors = _validate_normalized_learning_rows(
        full_log=full_log,
        indexed_rows=list(enumerate(rows)),
    )
    for index in range(len(rows)):
        errors.extend(
            f'countermeasure_learning[{index}].{error}'
            for error in row_errors.get(index, [])
        )
    return rows, errors


def _safe_json_evidence(value: object) -> object:
    max_chars = max(0, _coerce_int(current_app.config.get(
        'ADVENTURER_S1_AUXILIARY_DIAGNOSTIC_EVIDENCE_MAX_CHARS'
    )))
    if max_chars <= 0:
        return {'evidence_omitted': 'diagnostic_limit_disabled'}
    try:
        raw_json = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError, OverflowError):
        return {'unserializable_excerpt': str(value)[:max_chars]}
    if len(raw_json) > max_chars:
        return {
            'truncated_json_excerpt': raw_json[:max_chars],
            'original_json_chars': len(raw_json),
        }
    return json.loads(raw_json)


def _countermeasure_diagnostic_code(diagnostic: str) -> str:
    marker = '].'
    if marker in diagnostic:
        return diagnostic.split(marker, 1)[1]
    return diagnostic.lstrip('.') or 'contract_validation_failed'


def prepare_countermeasure_learning_for_settlement(
    *,
    final_result_data: dict | None,
    cached_full_log: dict | None = None,
    require_evidence_quotes: bool = False,
    include_persistence_metadata: bool = False,
) -> tuple[list[dict], dict | None]:
    """Consume future learning row-by-row without retrying the main story.

    Asset kinds are corrected only when the exact source name exists in one and
    only one other frozen asset kind. Irreducible rows retain bounded raw
    evidence in an admin-only disposition while valid siblings continue.
    Database and other unexpected failures intentionally still propagate.
    """
    full_log = _merged_full_log(final_result_data, cached_full_log)
    raw_value = full_log.get(COUNTERMEASURE_LEARNING_FIELD)
    if raw_value is None:
        return [], None
    _, field_chars = _contract_limits()
    audit_record_limit = max(0, _coerce_int(current_app.config.get(
        'ADVENTURER_S1_AUXILIARY_DIAGNOSTIC_EVIDENCE_MAX_RECORDS'
    )))
    if not isinstance(raw_value, list):
        return [], {
            'status': 'quarantined',
            'reason_code': 'learning_field_not_array',
            'total_records': 1,
            'accepted_records': 0,
            'quarantined_records': 1,
            'field_error_codes': ['not_array'],
            'raw_field': _safe_json_evidence(raw_value),
            'event_index_basis': 'candidate_final_events_zero_based',
        }

    indexed_rows: list[tuple[int, dict]] = []
    repairs_by_index: dict[int, list[dict]] = {}
    rejected_by_index: dict[int, dict] = {}
    for index, raw_row in enumerate(raw_value):
        normalized_rows, diagnostics = normalize_countermeasure_learning(
            [raw_row],
            max_records=1,
            field_chars=field_chars,
        )
        blocking = blocking_countermeasure_diagnostics(diagnostics)
        if blocking or len(normalized_rows) != 1:
            rejected_by_index[index] = {
                'record_index': index,
                'error_codes': [
                    _countermeasure_diagnostic_code(error)
                    for error in (blocking or ['records_not_preserved'])[:10]
                ],
                'raw_record': _safe_json_evidence(raw_row),
            }
            continue

        row = normalized_rows[0]
        row_repairs: list[dict] = []
        quotes = row.get('evidence_event_quotes')
        evidence_error = None
        if quotes is not None:
            bound_indexes, evidence_error = bind_final_event_evidence(
                row['evidence_event_indexes'], quotes, full_log.get('events'),
                allow_rebind=True,
            )
            if bound_indexes is not None and bound_indexes != row['evidence_event_indexes']:
                row_repairs.append({
                    'code': 'final_event_quote_exact_unique_rebound',
                    'from_indexes': list(row['evidence_event_indexes']),
                    'to_indexes': bound_indexes,
                })
                row['evidence_event_indexes'] = bound_indexes
        elif require_evidence_quotes:
            evidence_error = 'final_event_quotes_required'
        if evidence_error:
            rejected_by_index[index] = {
                'record_index': index,
                'error_codes': [evidence_error],
                'raw_record': _safe_json_evidence(raw_row),
            }
            continue
        for ref_index, source_ref in enumerate(row['source_refs']):
            source_kind = source_ref['source_kind']
            if source_ref.get('source_asset_id') is not None:
                canonical_name = _frozen_source_asset_name(full_log, source_ref)
                if canonical_name and canonical_name != source_ref['source_name']:
                    row_repairs.append({
                        'source_ref_index': ref_index,
                        'code': 'source_name_frozen_asset_canonicalized',
                        'adventurer_id': source_ref['adventurer_id'],
                        'source_asset_id': source_ref['source_asset_id'],
                        'from_name': source_ref['source_name'],
                        'to_name': canonical_name,
                    })
                    source_ref['source_name'] = canonical_name
                # Explicit IDs never fall back to a similarly named or differently typed asset.
                continue
            if source_kind in {'state', 'event'} or _source_ref_is_authoritative(
                full_log,
                source_ref,
            ):
                continue
            matching_kinds = [
                candidate_kind
                for candidate_kind in _RESOURCE_SNAPSHOT_FIELDS
                if candidate_kind != source_kind
                and source_ref['source_name'].casefold() in _known_resource_names(
                    full_log,
                    source_ref['adventurer_id'],
                    candidate_kind,
                )
            ]
            if len(matching_kinds) != 1:
                continue
            canonical_kind = matching_kinds[0]
            source_ref['source_kind'] = canonical_kind
            row_repairs.append({
                'source_ref_index': ref_index,
                'code': 'source_kind_exact_unique_canonicalized',
                'adventurer_id': source_ref['adventurer_id'],
                'source_name': source_ref['source_name'],
                'from_kind': source_kind,
                'to_kind': canonical_kind,
            })

        normalized_rows, diagnostics = normalize_countermeasure_learning(
            [row],
            max_records=1,
            field_chars=field_chars,
        )
        blocking = blocking_countermeasure_diagnostics(diagnostics)
        if blocking or len(normalized_rows) != 1:
            rejected_by_index[index] = {
                'record_index': index,
                'error_codes': [
                    _countermeasure_diagnostic_code(error)
                    for error in (blocking or ['records_not_preserved'])[:10]
                ],
                'raw_record': _safe_json_evidence(raw_row),
            }
            continue
        row = normalized_rows[0]
        indexed_rows.append((index, row))
        if row_repairs:
            repairs_by_index[index] = row_repairs

    row_errors = _validate_normalized_learning_rows(
        full_log=full_log,
        indexed_rows=indexed_rows,
    )
    accepted_rows: list[dict] = []
    accepted_repairs: list[dict] = []
    accepted_contract_keys: set[tuple] = set()
    for index, row in indexed_rows:
        errors = row_errors.get(index, [])
        if errors:
            rejected_by_index[index] = {
                'record_index': index,
                'error_codes': errors[:10],
                'raw_record': _safe_json_evidence(raw_value[index]),
            }
            continue
        contract_key = _learning_contract_key(row, complete=_has_independent_learning_owner(full_log))
        if contract_key in accepted_contract_keys:
            rejected_by_index[index] = {
                'record_index': index,
                'error_codes': ['duplicate_after_canonicalization'],
                'raw_record': _safe_json_evidence(raw_value[index]),
            }
            continue
        accepted_contract_keys.add(contract_key)
        if not include_persistence_metadata:
            row.pop('_matched_learning_fingerprint', None)
        accepted_rows.append(row)
        if index in repairs_by_index:
            accepted_repairs.append({
                'record_index': index,
                'source_ref_repairs': [
                    repair for repair in repairs_by_index[index]
                    if 'source_ref_index' in repair
                ],
                'event_evidence_repairs': [
                    repair for repair in repairs_by_index[index]
                    if 'source_ref_index' not in repair
                ],
            })

    rejected = [
        rejected_by_index[index]
        for index in sorted(rejected_by_index)
    ]
    if not rejected and not accepted_repairs:
        return accepted_rows, None
    if rejected and accepted_rows:
        status = 'partially_quarantined'
        reason_code = 'invalid_learning_records_quarantined'
    elif rejected:
        status = 'quarantined'
        reason_code = 'all_learning_records_quarantined'
    else:
        status = 'accepted_with_repairs'
        reason_code = (
            'final_event_evidence_rebound'
            if any(
                repair.get('code') == 'final_event_quote_exact_unique_rebound'
                for item in accepted_repairs
                for repair in item.get('event_evidence_repairs', [])
            )
            else
            'exact_source_asset_repaired'
            if any(
                repair.get('code') == 'source_name_frozen_asset_canonicalized'
                for item in accepted_repairs
                for repair in item.get('source_ref_repairs', [])
            )
            else 'exact_source_kind_repaired'
        )
    disposition = {
        'status': status,
        'reason_code': reason_code,
        'total_records': len(raw_value),
        'accepted_records': len(accepted_rows),
        'quarantined_records': len(rejected),
        'repaired_records': len(accepted_repairs),
        'repairs': accepted_repairs[:audit_record_limit],
        'rejected': rejected[:audit_record_limit],
        'repairs_omitted': max(0, len(accepted_repairs) - audit_record_limit),
        'rejected_records_omitted': max(0, len(rejected) - audit_record_limit),
        'event_index_basis': 'candidate_final_events_zero_based',
    }
    return accepted_rows, disposition


def project_countermeasure_learning_disposition_for_log(
    value: object,
    *,
    event_index_offset: int,
) -> dict | None:
    """Copy raw candidate evidence and record its full-log event offset."""
    if not isinstance(value, dict):
        return None
    try:
        projected = json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError, OverflowError) as exc:
        projected = {
            'status': str(value.get('status') or 'quarantined'),
            'reason_code': 'diagnostic_projection_failed',
            'evidence_omitted': type(exc).__name__,
        }
    projected['event_index_basis'] = 'candidate_final_events_zero_based'
    projected['full_log_event_index_offset'] = max(
        0,
        int(event_index_offset),
    )
    return projected


def _prefer_learning_row(current: dict, candidate: dict) -> dict:
    preferred = current
    if _KNOWLEDGE_LEVEL_RANK.get(candidate['knowledge_level'], 0) >= (
        _KNOWLEDGE_LEVEL_RANK.get(current['knowledge_level'], 0)
    ):
        preferred = candidate
    merged = dict(preferred)
    merged['source_refs'] = [
        dict(ref)
        for ref in {
            (
                int(ref['adventurer_id']),
                str(ref['source_kind']),
                str(ref['source_name']).casefold(),
                ref.get('source_asset_id'),
            ): ref
            for row in (current, candidate)
            for ref in row.get('source_refs') or []
            if isinstance(ref, dict)
        }.values()
    ]
    merged['evidence_event_indexes'] = sorted({
        int(event_index)
        for row in (current, candidate)
        for event_index in row.get('evidence_event_indexes') or []
    })
    # Both records belong to this settlement log. Keep each index paired with
    # its literal evidence; shared events prefer the retained knowledge tier.
    if all(
        normalize_evidence_quotes(row.get('evidence_event_quotes'), row['evidence_event_indexes'])
        is not None
        for row in (current, candidate)
    ):
        quotes_by_index = {
            index: quote
            for row in (current, candidate, preferred)
            for index, quote in zip(row['evidence_event_indexes'], row['evidence_event_quotes'])
        }
        merged['evidence_event_quotes'] = [
            quotes_by_index[index] for index in merged['evidence_event_indexes']
        ]
    else:
        merged.pop('evidence_event_quotes', None)
    return merged


def _learning_base_is_current(knowledge, frozen_ref: dict) -> bool:
    """Compare the exact authored base under the existing settlement row lock."""
    return (
        int(knowledge.last_source_expedition_log_id or 0)
        == _positive_int(frozen_ref.get('knowledge_source_expedition_log_id'))
        and knowledge.knowledge_level == frozen_ref.get('knowledge_level')
        and all(
            field in frozen_ref
            and str(getattr(knowledge, field) or '') == frozen_ref[field]
            for field in COUNTERMEASURE_KNOWLEDGE_SEMANTIC_FIELDS
        )
    )


def _learning_destination_error(candidate: dict, existing, matched, frozen_ref) -> str | None:
    """Never replace a memory whose complete base was unavailable to its author."""
    row = candidate['row']
    if candidate['encounters'] != 1:
        return 'learning_multiple_updates_for_audience'
    if row.get('matched_knowledge_id') is not None:
        if matched is None:
            return 'matched_knowledge_missing_after_lock'
        if frozen_ref is None:
            return 'learning_frozen_base_missing'
        if not _learning_base_is_current(matched, frozen_ref):
            return 'learning_source_changed_after_freeze'
    if existing is None:
        return None
    if frozen_ref is None:
        return 'learning_existing_destination_not_frozen'
    if not _learning_base_is_current(existing, frozen_ref) or (
        existing.id != row['matched_knowledge_id']
        and existing.observer_world_actor_id != frozen_ref.get('observer_world_actor_id')
    ):
        return 'learning_destination_changed_after_freeze'
    if _KNOWLEDGE_LEVEL_RANK[row['knowledge_level']] < _KNOWLEDGE_LEVEL_RANK[existing.knowledge_level]:
        return 'learning_progress_would_regress'
    return None


def persist_completed_learning(
    *,
    expedition_log_id: int,
    final_result_data: dict | None,
) -> dict:
    """Persist approved learning inside the existing settlement transaction."""
    if not isinstance(final_result_data, dict):
        return {'created': 0, 'updated': 0, 'encounters': 0}
    owner_receipt = final_result_data.get(COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD)
    independent_owner = (
        _has_independent_learning_owner(final_result_data)
        and owner_receipt.get('status') == 'completed'
    )
    validation_disposition = None
    if independent_owner:
        rows, validation_disposition = prepare_countermeasure_learning_for_settlement(
            final_result_data=final_result_data,
            include_persistence_metadata=True,
        )
    else:
        rows, errors = validate_learning_for_persistence(
            final_result_data=final_result_data,
        )
        if errors:
            raise CountermeasureLearningPersistenceError('; '.join(errors[:10]))

    frozen_faction_keys = _frozen_actor_faction_keys(final_result_data)
    aggregated: dict[tuple[str, str], dict] = {}
    for row in rows:
        actor_id = row['observer_world_actor_id']
        audiences = [('actor', f'actor:{actor_id}', None)]
        audiences.extend(
            ('faction', f'faction:{faction_key}', faction_key)
            for faction_key in sorted(frozen_faction_keys.get(actor_id, set()))
        )
        fingerprint = (
            row.get('_matched_learning_fingerprint')
            or countermeasure_fingerprint(row)
        )
        for audience_type, audience_key, faction_key in audiences:
            key = (audience_key, fingerprint)
            current = aggregated.get(key)
            if current is None:
                aggregated[key] = {
                    'row': row,
                    'rows': [row],
                    'audience_type': audience_type,
                    'audience_key': audience_key,
                    'faction_key': faction_key,
                    'fingerprint': fingerprint,
                    'encounters': 1,
                }
                continue
            # A current memory is authored as a whole. Choosing one of two
            # independent updates would silently discard the other progress.
            if not independent_owner:
                current['row'] = _prefer_learning_row(current['row'], row)
            current['rows'].append(row)
            current['encounters'] += 1

    if not aggregated:
        return {
            'created': 0, 'updated': 0, 'encounters': 0,
            **({'validation_disposition': validation_disposition,
                'status': 'quarantined'} if validation_disposition else {}),
        }
    audience_keys = {key[0] for key in aggregated}
    fingerprints = {key[1] for key in aggregated}
    existing_rows = (
        db_manager.query(AdventurerS1WorldCountermeasureKnowledge)
        .filter(
            AdventurerS1WorldCountermeasureKnowledge.audience_key.in_(audience_keys),
            AdventurerS1WorldCountermeasureKnowledge.learning_fingerprint.in_(
                fingerprints,
            ),
        )
        .order_by(
            AdventurerS1WorldCountermeasureKnowledge.audience_key,
            AdventurerS1WorldCountermeasureKnowledge.learning_fingerprint,
            AdventurerS1WorldCountermeasureKnowledge.id,
        )
        .with_for_update()
        .populate_existing()
        .all()
    )
    existing_by_key = {
        (row.audience_key, row.learning_fingerprint): row
        for row in existing_rows
    }
    existing_by_id = {int(row.id): row for row in existing_rows}
    frozen_refs = frozen_countermeasure_knowledge_refs(final_result_data, personal_only=False)
    deferred = {}
    # Check every destination before mutating any row. An actor write in this
    # transaction must not invalidate its still-identical faction copy.
    for key, candidate in aggregated.items():
        matched_id = candidate['row'].get('matched_knowledge_id')
        if independent_owner:
            existing = existing_by_key.get(key)
            if existing is not None and int(existing.last_source_expedition_log_id or 0) == int(expedition_log_id):
                continue
            row = candidate['row']
            frozen_ref = next((ref for ref in frozen_refs.get((
                row['observer_world_actor_id'], row['observer_capability_revision'],
            ), []) if ref['knowledge_id'] == matched_id), None)
            error = _learning_destination_error(
                candidate, existing, existing_by_id.get(matched_id), frozen_ref,
            )
            if error:
                deferred[key] = {
                    'audience_key': candidate['audience_key'],
                    'error_code': error,
                    'candidate_records': candidate['encounters'],
                    'raw_records': _safe_json_evidence([
                        {field: value for field, value in item.items() if not field.startswith('_')}
                        for item in candidate['rows']
                    ]),
                }
        elif matched_id is not None and int(matched_id) not in existing_by_id:
            raise CountermeasureLearningPersistenceError(
                'matched_knowledge_missing_after_lock',
            )

    created = 0
    updated = 0
    applied_audience_records = 0
    from .s1_countermeasure_growth_service import support_key
    persisted_support_keys = set()
    for key, candidate in aggregated.items():
        if key in deferred:
            continue
        if candidate['audience_type'] == 'actor':
            persisted_support_keys.update(support_key(row) for row in candidate['rows'])
        row = candidate['row']
        occurrence_count = int(candidate['encounters'])
        existing = existing_by_key.get(key)
        if existing is not None:
            if int(existing.last_source_expedition_log_id or 0) == int(expedition_log_id):
                continue
            existing.encounter_count = (
                int(existing.encounter_count or 0) + occurrence_count
            )
            if candidate['audience_type'] == 'actor':
                # Knowledge follows the stable actor.  The revision still
                # constrains each encounter, but evolution must not erase or
                # downgrade what this actor already learned.
                existing.observer_world_actor_id = row['observer_world_actor_id']
                existing.observer_capability_revision = max(
                    int(existing.observer_capability_revision or 0),
                    int(row['observer_capability_revision']),
                )
            existing.last_source_expedition_log_id = int(expedition_log_id)
            if _KNOWLEDGE_LEVEL_RANK.get(row['knowledge_level'], 0) >= (
                _KNOWLEDGE_LEVEL_RANK.get(existing.knowledge_level, 0)
            ):
                if candidate['audience_type'] != 'actor':
                    existing.observer_world_actor_id = row['observer_world_actor_id']
                    existing.observer_capability_revision = (
                        row['observer_capability_revision']
                    )
                existing.source_refs = row['source_refs']
                # A stable knowledge identity keeps its original mechanism and
                # effect. New evidence may refine boundaries and preparation,
                # but must not rename the mechanism behind the same ID.
                existing.unresolved_reason = row['unresolved_reason']
                existing.knowledge_level = row['knowledge_level']
                existing.evidence_event_indexes = row['evidence_event_indexes']
                existing.evidence_event_quotes = row.get('evidence_event_quotes')
                existing.evidence_source_expedition_log_id = int(expedition_log_id)
                existing.learned_boundary = row['learned_boundary']
                existing.countermeasure_direction = (
                    row['countermeasure_direction'] or None
                )
            updated += 1
            applied_audience_records += occurrence_count
            continue
        matched_identity = existing_by_id.get(row.get('matched_knowledge_id'))
        db_manager.add(AdventurerS1WorldCountermeasureKnowledge(
            first_source_expedition_log_id=int(expedition_log_id),
            last_source_expedition_log_id=int(expedition_log_id),
            evidence_source_expedition_log_id=int(expedition_log_id),
            observer_world_actor_id=row['observer_world_actor_id'],
            observer_capability_revision=row['observer_capability_revision'],
            audience_key=candidate['audience_key'],
            audience_type=candidate['audience_type'],
            faction_key=candidate['faction_key'],
            learning_fingerprint=candidate['fingerprint'],
            source_refs=row['source_refs'],
            encountered_method=(
                matched_identity.encountered_method
                if matched_identity is not None else row['encountered_method']
            ),
            unresolved_reason=row['unresolved_reason'],
            knowledge_level=row['knowledge_level'],
            evidence_event_indexes=row['evidence_event_indexes'],
            evidence_event_quotes=row.get('evidence_event_quotes'),
            observed_effect=(
                matched_identity.observed_effect
                if matched_identity is not None else row['observed_effect']
            ),
            learned_boundary=row['learned_boundary'],
            countermeasure_direction=row['countermeasure_direction'] or None,
            encounter_count=occurrence_count,
        ), flush=False)
        created += 1
        applied_audience_records += occurrence_count
    result = {
        'created': created,
        'updated': updated,
        'encounters': applied_audience_records,
    }
    if validation_disposition:
        result['validation_disposition'] = validation_disposition
        result['status'] = 'partially_persisted' if applied_audience_records else 'quarantined'
    if deferred:
        audit_limit = max(0, _coerce_int(current_app.config.get(
            'ADVENTURER_S1_AUXILIARY_DIAGNOSTIC_EVIDENCE_MAX_RECORDS',
        )))
        result.update({
            'status': 'partially_persisted' if applied_audience_records else 'quarantined',
            'reason_code': 'learning_audience_updates_deferred',
            'deferred_audience_records': len(deferred),
            'deferred_audiences': list(deferred.values())[:audit_limit],
            'deferred_audiences_omitted': max(0, len(deferred) - audit_limit),
        })
    if independent_owner:
        from .s1_countermeasure_growth_service import persist_growth
        growth_result = persist_growth(owner_receipt, expedition_log_id=int(expedition_log_id),
                                       persisted_support_keys=persisted_support_keys)
        if growth_result['updated'] or growth_result['deferred']:
            result['capability_growth'] = growth_result
    return result


def _with_legacy_matched_knowledge_compat(
    final_result_data: dict | None,
) -> dict | None:
    """Add the new nullable match reference only at the stored-log boundary."""
    if not isinstance(final_result_data, dict):
        return final_result_data
    if _has_independent_learning_owner(final_result_data):
        return final_result_data
    raw_rows = final_result_data.get(COUNTERMEASURE_LEARNING_FIELD)
    if not isinstance(raw_rows, list) or not any(
        isinstance(row, dict) and 'matched_knowledge_id' not in row
        for row in raw_rows
    ):
        return final_result_data
    compatible = dict(final_result_data)
    compatible[COUNTERMEASURE_LEARNING_FIELD] = [
        (
            {**row, 'matched_knowledge_id': None}
            if isinstance(row, dict) and 'matched_knowledge_id' not in row
            else row
        )
        for row in raw_rows
    ]
    return compatible


def persist_completed_learning_for_settlement(
    *,
    expedition_log_id: int,
    final_result_data: dict | None,
) -> dict:
    """Persist prepared learning and contain invalid historical rows.

    Future logs arrive with row-level disposition metadata and only accepted
    siblings in the public learning field.  The exception fallback remains for
    contract-invalid historical logs already in progress before that gate; its
    original rows stay in ``full_log`` for admin forensics.  Database and other
    unexpected failures still propagate and roll back the transaction.
    """
    try:
        result = persist_completed_learning(
            expedition_log_id=expedition_log_id,
            final_result_data=_with_legacy_matched_knowledge_compat(
                final_result_data,
            ),
        )
        disposition = (
            final_result_data.get(COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD)
            if isinstance(final_result_data, dict)
            else None
        )
        if not isinstance(disposition, dict):
            return result
        disposition_status = disposition.get('status')
        if disposition_status not in {
            'accepted_with_repairs',
            'partially_quarantined',
            'quarantined',
        }:
            return result
        result = dict(result)
        result.update({
            'status': result.get('status') or {
                'accepted_with_repairs': 'persisted_with_repairs',
                'partially_quarantined': 'partially_persisted',
                'quarantined': 'quarantined',
            }[disposition_status],
            'reason_code': disposition.get('reason_code'),
            'accepted_records': int(disposition.get('accepted_records') or 0),
            'quarantined_records': int(
                disposition.get('quarantined_records') or 0
            ),
            'repaired_records': int(disposition.get('repaired_records') or 0),
        })
        error_codes: list[str] = []
        for error in disposition.get('field_error_codes') or []:
            clean = str(error or '').strip()
            if clean and clean not in error_codes:
                error_codes.append(clean)
            if len(error_codes) >= 10:
                break
        for rejected in disposition.get('rejected') or []:
            if not isinstance(rejected, dict):
                continue
            for error in rejected.get('error_codes') or []:
                clean = str(error or '').strip()
                if clean and clean not in error_codes:
                    error_codes.append(clean)
                if len(error_codes) >= 10:
                    break
            if len(error_codes) >= 10:
                break
        if error_codes:
            result['errors'] = error_codes
        return result
    except CountermeasureLearningPersistenceError as exc:
        errors = [
            part.strip()
            for part in str(exc).split(';')
            if part.strip()
        ][:10]
        raw_rows = (
            final_result_data.get(COUNTERMEASURE_LEARNING_FIELD)
            if isinstance(final_result_data, dict)
            else None
        )
        quarantined_records = len(raw_rows) if isinstance(raw_rows, list) else 0
        current_app.logger.warning(
            'S1 适应知识结算隔离：保留原始证据并继续玩家结算 '
            'log=%s records=%s errors=%s',
            expedition_log_id,
            quarantined_records,
            errors,
        )
        return {
            'created': 0,
            'updated': 0,
            'encounters': 0,
            'status': 'quarantined',
            'reason_code': 'contract_validation_failed',
            'quarantined_records': quarantined_records,
            'errors': errors,
        }


def due_countermeasure_actor_ids(*, now: datetime | None = None) -> list[int]:
    """Return stable actors whose retained knowledge still needs a fair scene."""
    if not current_app.config.get(
        'ADVENTURER_S1_COUNTERMEASURE_OPPORTUNITY_ENABLED', True,
    ):
        return []
    if not current_app.config.get(
        'ADVENTURER_S1_COUNTERMEASURE_LEARNING_ENABLED', True,
    ):
        return []
    cooldown_hours = max(0, _coerce_int(current_app.config.get(
        'ADVENTURER_S1_COUNTERMEASURE_OPPORTUNITY_COOLDOWN_HOURS', 6,
    ), 6))
    actor_limit = max(0, _coerce_int(current_app.config.get(
        'ADVENTURER_S1_COUNTERMEASURE_OPPORTUNITY_ACTOR_LIMIT', 32,
    ), 32))
    if actor_limit <= 0:
        return []
    cutoff = (now or _portable_utc_now()) - timedelta(hours=cooldown_hours)
    knowledge = AdventurerS1WorldCountermeasureKnowledge
    actor = AdventurerS1WorldActor
    outstanding_condition = or_(
        knowledge.observer_capability_revision < actor.capability_revision,
        knowledge.last_applied_source_expedition_log_id.is_(None),
        knowledge.last_applied_source_expedition_log_id
        != knowledge.last_source_expedition_log_id,
    )
    has_outstanding = func.max(case((outstanding_condition, 1), else_=0))
    effective_opportunity_at = case((
        knowledge.last_opportunity_source_expedition_log_id
        == knowledge.last_source_expedition_log_id,
        knowledge.last_opportunity_at,
    ), else_=None)
    latest_actor_opportunity = func.max(effective_opportunity_at)
    oldest_outstanding_source_log_id = func.min(case(
        (outstanding_condition, knowledge.last_source_expedition_log_id),
        else_=None,
    ))
    rows = (
        db_manager.query(
            actor.id,
            latest_actor_opportunity.label('latest_actor_opportunity'),
            oldest_outstanding_source_log_id.label(
                'oldest_outstanding_source_log_id',
            ),
        )
        .select_from(knowledge)
        .join(actor, actor.id == knowledge.observer_world_actor_id)
        .filter(
            knowledge.audience_type == 'actor',
            actor.status == 'active',
            actor.capability_profile.isnot(None),
            actor.capability_revision > 0,
            knowledge.observer_capability_revision > 0,
            knowledge.observer_capability_revision <= actor.capability_revision,
        )
        .group_by(actor.id)
        .having(has_outstanding == 1)
        .having(or_(
            latest_actor_opportunity.is_(None),
            latest_actor_opportunity <= cutoff,
        ))
        .order_by(
            case((latest_actor_opportunity.is_(None), 0), else_=1),
            latest_actor_opportunity.asc(),
            oldest_outstanding_source_log_id.asc(),
            actor.id.asc(),
        )
        .limit(actor_limit)
        .all()
    )
    return [int(row[0]) for row in rows if int(row[0] or 0) > 0]


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    normalized = _coerce_int(value)
    return normalized if normalized > 0 else None


def _direct_knowledge_refs(snapshot: object, *, personal_only: bool = True) -> dict[tuple[int, int], list[dict]]:
    if not isinstance(snapshot, dict):
        return {}
    usage_by_actor: dict[int, tuple[int, str]] = {}
    for raw_usage in snapshot.get('world_actor_usages') or []:
        if not isinstance(raw_usage, dict):
            continue
        actor_id = _positive_int(raw_usage.get('world_actor_id'))
        revision = _positive_int(raw_usage.get('capability_revision'))
        usage_mode = str(raw_usage.get('usage_mode') or '')
        if actor_id is not None and revision is not None:
            usage_by_actor[actor_id] = (revision, usage_mode)
    result: dict[tuple[int, int], list[dict]] = {}
    for entry in snapshot.get('world_actor_prompt_entries') or []:
        if not isinstance(entry, dict):
            continue
        actor_id = _positive_int(entry.get('world_actor_id'))
        if actor_id is None:
            continue
        frozen_usage = usage_by_actor.get(actor_id)
        revision = _positive_int(entry.get('capability_revision'))
        usage_mode = str(entry.get('usage_mode') or '')
        if (
            frozen_usage is None
            or revision is None
            or revision != frozen_usage[0]
            or usage_mode != 'exclusive_direct_use'
            or frozen_usage[1] != 'exclusive_direct_use'
        ):
            continue
        refs: list[dict] = []
        seen_ids: set[int] = set()
        injected_ids = {
            _positive_int(value) for value in entry.get('countermeasure_knowledge_ids') or []
        }
        for raw_ref in entry.get('countermeasure_knowledge_refs') or []:
            if not isinstance(raw_ref, dict):
                continue
            knowledge_id = _positive_int(raw_ref.get('knowledge_id'))
            source_log_id = _positive_int(
                raw_ref.get('knowledge_source_expedition_log_id'),
            )
            knowledge_level = str(raw_ref.get('knowledge_level') or '')
            if (
                knowledge_id is None
                or source_log_id is None
                or knowledge_id in seen_ids
                or knowledge_level not in _KNOWLEDGE_LEVEL_RANK
                or raw_ref.get('audience_type') not in (
                    {'actor'} if personal_only else {'actor', 'faction'}
                )
                or (not personal_only and knowledge_id not in injected_ids)
            ):
                continue
            seen_ids.add(knowledge_id)
            projected_ref = {
                'knowledge_id': knowledge_id,
                'knowledge_source_expedition_log_id': source_log_id,
                'knowledge_level': knowledge_level,
            }
            for field_name in COUNTERMEASURE_KNOWLEDGE_SEMANTIC_FIELDS:
                field_value = raw_ref.get(field_name)
                if isinstance(field_value, str):
                    projected_ref[field_name] = field_value
            if not personal_only:
                for field_name in (
                    'audience_type', 'audience_key', 'faction_key',
                    'observer_world_actor_id', 'observer_capability_revision',
                ):
                    if field_name in raw_ref:
                        projected_ref[field_name] = raw_ref[field_name]
            refs.append(projected_ref)
        result[(actor_id, revision)] = refs
    return result


def frozen_countermeasure_knowledge_refs(full_log: object, *, personal_only: bool = True) -> dict[tuple[int, int], list[dict]]:
    """Project exact injected knowledge; application audits default to personal scope."""
    if not isinstance(full_log, dict):
        return {}
    generation = full_log.get('generation_world_intel_injection')
    if not isinstance(generation, dict):
        return {}
    # Opportunity/application authority is granted only by the exact snapshot
    # sent to the successful generation call.  Legacy objective snapshots (or
    # generation snapshots without frozen usages) fail closed and are reviewed
    # again instead of being retroactively treated as actual direct use.
    refs = _direct_knowledge_refs(generation, personal_only=personal_only)
    frozen_revisions = objective_actor_revisions(full_log.get('objective_outline'))
    if not frozen_revisions:
        return refs
    return {
        key: value
        for key, value in refs.items()
        if frozen_revisions.get(key[0]) == key[1]
    }


def mark_countermeasure_opportunities_from_snapshot(
    *,
    expedition_log_id: int,
    snapshot: dict | None,
    now: datetime | None = None,
) -> dict:
    """Mark only exact personal knowledge actually sent for direct actor use."""
    log_id = _positive_int(expedition_log_id)
    refs_by_target = _direct_knowledge_refs(snapshot)
    expected = {
        (
            actor_id,
            revision,
            int(ref['knowledge_id']),
            int(ref['knowledge_source_expedition_log_id']),
        )
        for (actor_id, revision), refs in refs_by_target.items()
        for ref in refs
    }
    if log_id is None or not expected:
        return {'marked': 0, 'already_marked': 0}
    knowledge_ids = {key[2] for key in expected}
    rows = (
        db_manager.query(AdventurerS1WorldCountermeasureKnowledge)
        .filter(AdventurerS1WorldCountermeasureKnowledge.id.in_(knowledge_ids))
        .order_by(
            AdventurerS1WorldCountermeasureKnowledge.audience_key,
            AdventurerS1WorldCountermeasureKnowledge.learning_fingerprint,
            AdventurerS1WorldCountermeasureKnowledge.id,
        )
        .with_for_update()
        .populate_existing()
        .all()
    )
    rows_by_id = {int(row.id): row for row in rows}
    marked = 0
    already_marked = 0
    opportunity_at = now or _portable_utc_now()
    for actor_id, revision, knowledge_id, source_log_id in sorted(expected):
        row = rows_by_id.get(knowledge_id)
        if (
            row is None
            or row.audience_type != 'actor'
            or row.audience_key != f'actor:{actor_id}'
            or int(row.observer_world_actor_id or 0) != actor_id
            or int(row.observer_capability_revision or 0) <= 0
            or int(row.observer_capability_revision or 0) > revision
            or int(row.last_source_expedition_log_id or 0) != source_log_id
        ):
            continue
        if int(row.last_opportunity_expedition_log_id or 0) == log_id:
            already_marked += 1
            continue
        row.last_opportunity_expedition_log_id = log_id
        row.last_opportunity_source_expedition_log_id = source_log_id
        row.last_opportunity_at = opportunity_at
        row.opportunity_count = int(row.opportunity_count or 0) + 1
        marked += 1
    return {'marked': marked, 'already_marked': already_marked}


def _normalize_receipt_indexes(value: object) -> list[int] | None:
    if not isinstance(value, list):
        return None
    result: list[int] = []
    for raw_index in value:
        if isinstance(raw_index, bool):
            return None
        index = _coerce_int(raw_index, -1)
        if index < 0 or index in result:
            return None
        result.append(index)
    return result


def _project_countermeasure_application_receipts(
    value: object,
    *,
    expected_version: int | None = None,
) -> tuple[list[dict], int | None]:
    """Project current dual evidence or persisted legacy single evidence."""
    supported_versions = {
        _COUNTERMEASURE_APPLICATION_RECEIPT_LEGACY_VERSION,
        _COUNTERMEASURE_APPLICATION_RECEIPT_DUAL_VERSION,
        _COUNTERMEASURE_APPLICATION_RECEIPT_VERSION,
    }
    if (
        not isinstance(value, list)
        or expected_version not in supported_versions | {None}
    ):
        return [], None
    projected: list[dict] = []
    detected_version = expected_version
    for raw_review in value:
        if not isinstance(raw_review, dict):
            return [], None
        actor_id = _positive_int(raw_review.get('observer_world_actor_id'))
        revision = _positive_int(raw_review.get('observer_capability_revision'))
        decision = str(raw_review.get('decision') or '')
        evidence = _normalize_receipt_indexes(
            raw_review.get('evidence_event_indexes'),
        )
        reason = raw_review.get('reason')
        raw_applications = raw_review.get('knowledge_applications')
        if (
            actor_id is None
            or revision is None
            or decision not in {
                'learning_recorded', 'no_direct_observation',
                'no_unresolved_boundary', 'applications_reviewed',
            }
            or evidence is None
            or not isinstance(reason, str)
            or not isinstance(raw_applications, list)
        ):
            return [], None
        applications: list[dict] = []
        for raw_application in raw_applications:
            if not isinstance(raw_application, dict):
                return [], None
            knowledge_id = _positive_int(raw_application.get('knowledge_id'))
            source_log_id = _positive_int(
                raw_application.get('knowledge_source_expedition_log_id'),
            )
            application_decision = str(raw_application.get('decision') or '')
            application_reason = raw_application.get('reason')
            if (
                knowledge_id is None
                or source_log_id is None
                or application_decision not in _COUNTERMEASURE_APPLICATION_DECISIONS
                or not isinstance(application_reason, str)
            ):
                return [], None
            has_legacy_evidence = 'evidence_event_indexes' in raw_application
            has_current_exposure = (
                'matching_exposure_event_indexes' in raw_application
            )
            has_current_response = 'response_event_indexes' in raw_application
            has_exposure_quotes = 'matching_exposure_event_quotes' in raw_application
            has_response_quotes = 'response_event_quotes' in raw_application
            if has_exposure_quotes != has_response_quotes:
                return [], None
            if has_legacy_evidence and (
                has_current_exposure or has_current_response
            ):
                return [], None
            application_version = (
                _COUNTERMEASURE_APPLICATION_RECEIPT_VERSION
                if has_current_exposure and has_current_response and has_exposure_quotes
                else _COUNTERMEASURE_APPLICATION_RECEIPT_DUAL_VERSION
                if has_current_exposure and has_current_response
                else _COUNTERMEASURE_APPLICATION_RECEIPT_LEGACY_VERSION
                if has_legacy_evidence
                else None
            )
            if (
                application_version is None
                or (
                    detected_version is not None
                    and application_version != detected_version
                )
            ):
                return [], None
            detected_version = application_version
            projected_application = {
                'knowledge_id': knowledge_id,
                'knowledge_source_expedition_log_id': source_log_id,
                'decision': application_decision,
                'reason': application_reason.strip(),
            }
            if application_version >= _COUNTERMEASURE_APPLICATION_RECEIPT_DUAL_VERSION:
                matching_exposure = _normalize_receipt_indexes(
                    raw_application.get('matching_exposure_event_indexes'),
                )
                response_evidence = _normalize_receipt_indexes(
                    raw_application.get('response_event_indexes'),
                )
                if matching_exposure is None or response_evidence is None:
                    return [], None
                if (
                    application_decision == 'no_matching_exposure'
                    and (matching_exposure or response_evidence)
                ):
                    return [], None
                if (
                    application_decision == 'behavior_informed'
                    and (
                        not matching_exposure
                        or not response_evidence
                        or min(response_evidence) < min(matching_exposure)
                    )
                ):
                    return [], None
                if (
                    application_decision in {
                        'behavior_not_reflected', 'no_response_opportunity',
                    }
                    and (not matching_exposure or response_evidence)
                ):
                    return [], None
                projected_application.update({
                    'matching_exposure_event_indexes': matching_exposure,
                    'response_event_indexes': response_evidence,
                })
                if application_version == _COUNTERMEASURE_APPLICATION_RECEIPT_VERSION:
                    for quote_field, indexes in (
                        ('matching_exposure_event_quotes', matching_exposure),
                        ('response_event_quotes', response_evidence),
                    ):
                        quotes = normalize_evidence_quotes(raw_application.get(quote_field), indexes)
                        if quotes is None:
                            return [], None
                        projected_application[quote_field] = quotes
            else:
                if application_decision == 'no_response_opportunity':
                    # This decision requires separate exposure/response evidence.
                    return [], None
                application_evidence = _normalize_receipt_indexes(
                    raw_application.get('evidence_event_indexes'),
                )
                if application_evidence is None:
                    return [], None
                projected_application['evidence_event_indexes'] = (
                    application_evidence
                )
            applications.append(projected_application)
        projected.append({
            'observer_world_actor_id': actor_id,
            'observer_capability_revision': revision,
            'decision': decision,
            'evidence_event_indexes': evidence,
            'reason': reason.strip(),
            'knowledge_applications': applications,
        })
    return (
        projected,
        detected_version or _COUNTERMEASURE_APPLICATION_RECEIPT_VERSION,
    )


def project_countermeasure_application_receipts(value: object) -> list[dict]:
    """Whitelist persisted receipts without exposing legacy AI output shape."""
    projected, _version = _project_countermeasure_application_receipts(value)
    return projected


def build_countermeasure_application_receipt_envelope(
    value: object,
    *,
    observer_review_id: int,
    receipt_version: int | None = None,
) -> dict | None:
    review_id = _positive_int(observer_review_id)
    if review_id is None or not isinstance(value, list):
        return None
    reviews, projected_version = _project_countermeasure_application_receipts(
        value,
        expected_version=receipt_version,
    )
    if projected_version is None or (value and not reviews):
        return None
    return {
        'version': projected_version,
        'observer_review_id': review_id,
        'event_index_offset': 0,
        'reviews': reviews,
    }


def project_countermeasure_application_receipt_envelope(
    value: object,
) -> dict | None:
    if (
        not isinstance(value, dict)
        or value.get('version') not in {
            _COUNTERMEASURE_APPLICATION_RECEIPT_LEGACY_VERSION,
            _COUNTERMEASURE_APPLICATION_RECEIPT_DUAL_VERSION,
            _COUNTERMEASURE_APPLICATION_RECEIPT_VERSION,
        }
    ):
        return None
    receipt_version = int(value['version'])
    review_id = _positive_int(value.get('observer_review_id'))
    raw_offset = value.get('event_index_offset')
    raw_reviews = value.get('reviews')
    if (
        review_id is None
        or isinstance(raw_offset, bool)
        or not isinstance(raw_offset, int)
        or raw_offset < 0
        or not isinstance(raw_reviews, list)
    ):
        return None
    reviews, projected_version = _project_countermeasure_application_receipts(
        raw_reviews,
        expected_version=receipt_version,
    )
    if projected_version is None or (raw_reviews and not reviews):
        return None
    return {
        'version': projected_version,
        'observer_review_id': review_id,
        'event_index_offset': raw_offset,
        'reviews': reviews,
    }


def shift_countermeasure_application_receipts(
    value: object,
    *,
    event_index_offset: int,
) -> dict | None:
    envelope = project_countermeasure_application_receipt_envelope(value)
    if envelope is None:
        return None
    projected = envelope['reviews']
    offset = max(0, _coerce_int(event_index_offset))
    for review in projected:
        review['evidence_event_indexes'] = [
            index + offset for index in review['evidence_event_indexes']
        ]
        for application in review['knowledge_applications']:
            if (
                envelope['version']
                >= _COUNTERMEASURE_APPLICATION_RECEIPT_DUAL_VERSION
            ):
                application['matching_exposure_event_indexes'] = [
                    index + offset
                    for index in application[
                        'matching_exposure_event_indexes'
                    ]
                ]
                application['response_event_indexes'] = [
                    index + offset
                    for index in application['response_event_indexes']
                ]
            else:
                application['evidence_event_indexes'] = [
                    index + offset
                    for index in application['evidence_event_indexes']
                ]
    envelope['event_index_offset'] = int(
        envelope['event_index_offset'],
    ) + offset
    return envelope


def countermeasure_application_receipt_envelope_is_valid(
    value: object,
    *,
    expedition_log_id: int,
    full_log: dict | None,
    require_zero_offset: bool = False,
) -> bool:
    """Verify checkpoint provenance, frozen identities and exact source versions."""
    envelope = project_countermeasure_application_receipt_envelope(value)
    if envelope is None or not isinstance(full_log, dict):
        return False
    if require_zero_offset and int(envelope['event_index_offset']) != 0:
        return False
    observer_review = (
        db_manager.query(AdventurerS1ExpeditionObserverReview)
        .filter_by(
            id=int(envelope['observer_review_id']),
            expedition_log_id=int(expedition_log_id),
            status='completed',
        )
        .first()
    )
    persisted_result = (
        observer_review.result
        if observer_review and isinstance(observer_review.result, dict)
        else {}
    )
    persisted_envelope = build_countermeasure_application_receipt_envelope(
        persisted_result.get('countermeasure_opportunity_reviews') or [],
        observer_review_id=int(envelope['observer_review_id']),
        receipt_version=int(envelope['version']),
    )
    persisted_envelope = shift_countermeasure_application_receipts(
        persisted_envelope,
        event_index_offset=int(envelope['event_index_offset']),
    )
    if (
        persisted_envelope is None
        or persisted_envelope['reviews'] != envelope['reviews']
    ):
        return False
    actor_revisions = objective_actor_revisions(full_log.get('objective_outline'))
    expected_target_keys = [
        (actor_id, actor_revisions[actor_id])
        for actor_id in sorted(actor_revisions)
    ]
    if envelope['reviews'] and all(
        review['decision'] == 'applications_reviewed'
        for review in envelope['reviews']
    ):
        frozen_refs = frozen_countermeasure_knowledge_refs(full_log)
        expected_target_keys = [key for key in expected_target_keys if frozen_refs.get(key)]
    actual_target_keys = [
        (
            int(review['observer_world_actor_id']),
            int(review['observer_capability_revision']),
        )
        for review in envelope['reviews']
    ]
    if actual_target_keys != expected_target_keys:
        return False
    event_count = len(full_log.get('events') or [])
    event_index_offset = int(envelope['event_index_offset'])
    for review in envelope['reviews']:
        all_evidence = [review['evidence_event_indexes']]
        if (
            envelope['version']
            >= _COUNTERMEASURE_APPLICATION_RECEIPT_DUAL_VERSION
        ):
            for application in review['knowledge_applications']:
                all_evidence.extend((
                    application['matching_exposure_event_indexes'],
                    application['response_event_indexes'],
                ))
                if envelope['version'] == _COUNTERMEASURE_APPLICATION_RECEIPT_VERSION:
                    for index_field, quote_field in (
                        ('matching_exposure_event_indexes', 'matching_exposure_event_quotes'),
                        ('response_event_indexes', 'response_event_quotes'),
                    ):
                        _, error = bind_final_event_evidence(
                            application[index_field], application[quote_field], full_log.get('events'),
                        )
                        if error:
                            return False
        else:
            all_evidence.extend(
                application['evidence_event_indexes']
                for application in review['knowledge_applications']
            )
        if any(
            index < event_index_offset or index >= event_count
            for evidence in all_evidence
            for index in evidence
        ):
            return False
    frozen_refs = frozen_countermeasure_knowledge_refs(full_log)
    for review, target_key in zip(envelope['reviews'], actual_target_keys):
        expected_sources = [
            (
                int(ref['knowledge_id']),
                int(ref['knowledge_source_expedition_log_id']),
            )
            for ref in frozen_refs.get(target_key, [])
        ]
        actual_sources = [
            (
                int(application['knowledge_id']),
                int(application['knowledge_source_expedition_log_id']),
            )
            for application in review['knowledge_applications']
        ]
        if actual_sources != expected_sources:
            return False
    return True


def persist_countermeasure_applications_for_settlement(
    *,
    expedition_log_id: int,
    final_result_data: dict | None,
) -> dict:
    """Persist actual behavior use only from backend-owned, frozen Observer receipts."""
    if not isinstance(final_result_data, dict):
        return {'applied': 0, 'already_applied': 0, 'status': 'no_receipts'}
    raw_receipts = final_result_data.get(
        COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD,
    )
    if raw_receipts in (None, []):
        return {'applied': 0, 'already_applied': 0, 'status': 'no_receipts'}
    envelope = project_countermeasure_application_receipt_envelope(raw_receipts)
    if envelope is None:
        return {
            'applied': 0,
            'already_applied': 0,
            'status': 'quarantined',
            'errors': ['application_receipts_invalid'],
        }
    if not countermeasure_application_receipt_envelope_is_valid(
        envelope,
        expedition_log_id=int(expedition_log_id),
        full_log=final_result_data,
    ):
        return {
            'applied': 0,
            'already_applied': 0,
            'status': 'quarantined',
            'errors': ['application_receipts_scope_or_provenance_mismatch'],
        }
    if (
        envelope['version']
        != _COUNTERMEASURE_APPLICATION_RECEIPT_VERSION
        and any(
            application['decision'] == 'behavior_informed'
            for review in envelope['reviews']
            for application in review['knowledge_applications']
        )
    ):
        return {
            'applied': 0,
            'already_applied': 0,
            'status': 'quarantined',
            'errors': ['application_receipts_legacy_evidence_unverified'],
        }
    receipts = envelope['reviews']
    allowed_refs = frozen_countermeasure_knowledge_refs(final_result_data)
    event_count = len(final_result_data.get('events') or [])
    requested: dict[int, tuple[int, int, int]] = {}
    errors: list[str] = []
    for review in receipts:
        target = (
            int(review['observer_world_actor_id']),
            int(review['observer_capability_revision']),
        )
        allowed_sources = {
            int(ref['knowledge_id']): int(
                ref['knowledge_source_expedition_log_id'],
            )
            for ref in allowed_refs.get(target, [])
        }
        for application in review['knowledge_applications']:
            if application['decision'] != 'behavior_informed':
                continue
            knowledge_id = int(application['knowledge_id'])
            source_log_id = int(
                application['knowledge_source_expedition_log_id'],
            )
            matching_exposure = application[
                'matching_exposure_event_indexes'
            ]
            response_evidence = application['response_event_indexes']
            if allowed_sources.get(knowledge_id) != source_log_id:
                errors.append(f'knowledge_id={knowledge_id}:not_frozen_for_target')
            elif (
                not matching_exposure
                or not response_evidence
                or any(
                    index >= event_count
                    for index in matching_exposure + response_evidence
                )
                or min(response_evidence) < min(matching_exposure)
            ):
                errors.append(f'knowledge_id={knowledge_id}:evidence_out_of_range')
            elif knowledge_id in requested:
                errors.append(f'knowledge_id={knowledge_id}:duplicate_application')
            else:
                requested[knowledge_id] = (*target, source_log_id)
    if not requested:
        return {
            'applied': 0,
            'already_applied': 0,
            'status': 'quarantined' if errors else 'reviewed',
            **({'errors': errors[:10]} if errors else {}),
        }
    rows = (
        db_manager.query(AdventurerS1WorldCountermeasureKnowledge)
        .filter(AdventurerS1WorldCountermeasureKnowledge.id.in_(requested))
        .order_by(
            AdventurerS1WorldCountermeasureKnowledge.audience_key,
            AdventurerS1WorldCountermeasureKnowledge.learning_fingerprint,
            AdventurerS1WorldCountermeasureKnowledge.id,
        )
        .with_for_update()
        .populate_existing()
        .all()
    )
    rows_by_id = {int(row.id): row for row in rows}
    log_id = int(expedition_log_id)
    applicable: dict[int, tuple[int, int, int]] = {}
    for knowledge_id, (actor_id, revision, source_log_id) in requested.items():
        row = rows_by_id.get(knowledge_id)
        if (
            row is None
            or row.audience_type != 'actor'
            or row.audience_key != f'actor:{actor_id}'
            or int(row.observer_world_actor_id or 0) != actor_id
            or int(row.observer_capability_revision or 0) <= 0
            or int(row.observer_capability_revision or 0) > revision
            or int(row.last_opportunity_expedition_log_id or 0) != log_id
            or int(row.last_opportunity_source_expedition_log_id or 0)
            != source_log_id
        ):
            errors.append(f'knowledge_id={knowledge_id}:identity_or_opportunity_mismatch')
        elif int(row.last_source_expedition_log_id or 0) != source_log_id:
            errors.append(f'knowledge_id={knowledge_id}:source_advanced')
        else:
            applicable[knowledge_id] = (actor_id, revision, source_log_id)
    applied = 0
    already_applied = 0
    for knowledge_id in sorted(applicable):
        row = rows_by_id[knowledge_id]
        row.observer_capability_revision = applicable[knowledge_id][1]
        if int(row.last_application_expedition_log_id or 0) == log_id:
            already_applied += 1
            continue
        row.last_application_expedition_log_id = log_id
        row.last_applied_source_expedition_log_id = applicable[knowledge_id][2]
        row.application_count = int(row.application_count or 0) + 1
        applied += 1
    return {
        'applied': applied,
        'already_applied': already_applied,
        'status': 'applied_with_skips' if errors else 'applied',
        **({'errors': errors[:10]} if errors else {}),
    }


def _countermeasure_knowledge_prompt_ref(
    row: AdventurerS1WorldCountermeasureKnowledge,
) -> dict:
    return {
        'knowledge_id': int(row.id),
        'knowledge_source_expedition_log_id': int(
            row.last_source_expedition_log_id,
        ),
        'knowledge_level': str(row.knowledge_level or ''),
        'audience_type': str(row.audience_type or ''),
        'audience_key': str(row.audience_key or ''),
        'faction_key': str(row.faction_key or ''),
        'observer_world_actor_id': int(row.observer_world_actor_id or 0),
        'observer_capability_revision': int(
            row.observer_capability_revision or 0,
        ),
        'encounter_count': int(row.encounter_count or 1),
        'encountered_method': str(row.encountered_method or ''),
        'unresolved_reason': str(row.unresolved_reason or ''),
        'observed_effect': str(row.observed_effect or ''),
        'learned_boundary': str(row.learned_boundary or ''),
        'countermeasure_direction': str(
            row.countermeasure_direction or '',
        ),
    }


def load_countermeasure_prompt_candidates(
    actor: AdventurerS1WorldActor,
    faction_relations: list[AdventurerS1WorldActorFactionRelation],
) -> list[dict]:
    """Load the full bounded, eligible knowledge set in fair fallback order."""
    if not current_app.config.get(
        'ADVENTURER_S1_COUNTERMEASURE_LEARNING_ENABLED', True,
    ):
        return []
    actor_audience_key = f'actor:{int(actor.id)}'
    faction_audience_keys = [
        f'faction:{relation.faction_key}'
        for relation in faction_relations
        if str(relation.faction_key or '').strip()
    ]
    scan_limit = max(1, _coerce_int(current_app.config.get(
        'ADVENTURER_S1_COUNTERMEASURE_LEARNING_SCAN_LIMIT', 96,
    ), 96))
    audience_filters = [and_(
        AdventurerS1WorldCountermeasureKnowledge.audience_type == 'actor',
        AdventurerS1WorldCountermeasureKnowledge.audience_key
        == actor_audience_key,
        AdventurerS1WorldCountermeasureKnowledge.observer_world_actor_id
        == int(actor.id),
        AdventurerS1WorldCountermeasureKnowledge.observer_capability_revision
        > 0,
        AdventurerS1WorldCountermeasureKnowledge.observer_capability_revision
        <= int(actor.capability_revision or 0),
    )]
    if faction_audience_keys:
        audience_filters.append(and_(
            AdventurerS1WorldCountermeasureKnowledge.audience_type == 'faction',
            AdventurerS1WorldCountermeasureKnowledge.audience_key.in_(
                faction_audience_keys,
            ),
        ))
    knowledge_model = AdventurerS1WorldCountermeasureKnowledge
    personal_outstanding = and_(
        knowledge_model.audience_type == 'actor',
        or_(
            knowledge_model.observer_capability_revision
            < int(actor.capability_revision or 0),
            knowledge_model.last_applied_source_expedition_log_id.is_(None),
            knowledge_model.last_applied_source_expedition_log_id
            != knowledge_model.last_source_expedition_log_id,
        ),
    )
    personal_current_source_unoffered = and_(
        knowledge_model.audience_type == 'actor',
        or_(
            knowledge_model.observer_capability_revision
            < int(actor.capability_revision or 0),
            knowledge_model.last_opportunity_source_expedition_log_id.is_(None),
            knowledge_model.last_opportunity_source_expedition_log_id
            != knowledge_model.last_source_expedition_log_id,
        ),
    )
    rows = (
        db_manager.query(knowledge_model)
        .filter(or_(*audience_filters))
        .order_by(
            case((personal_outstanding, 0), else_=1),
            case((personal_current_source_unoffered, 0), else_=1),
            knowledge_model.last_opportunity_at.asc(),
            knowledge_model.last_source_expedition_log_id.desc(),
            knowledge_model.id.desc(),
        )
        .limit(scan_limit)
        .all()
    )
    selected: dict[str, AdventurerS1WorldCountermeasureKnowledge] = {}
    for row in rows:
        if row.audience_type == 'actor' and (
            int(row.observer_world_actor_id or 0) != int(actor.id)
            or int(row.observer_capability_revision or 0) <= 0
            or int(row.observer_capability_revision or 0)
            > int(actor.capability_revision or 0)
        ):
            continue
        current = selected.get(row.learning_fingerprint)
        if current is None or (
            row.audience_type == 'actor',
            _KNOWLEDGE_LEVEL_RANK.get(row.knowledge_level, 0),
            int(row.encounter_count or 0),
        ) > (
            current.audience_type == 'actor',
            _KNOWLEDGE_LEVEL_RANK.get(current.knowledge_level, 0),
            int(current.encounter_count or 0),
        ):
            selected[row.learning_fingerprint] = row
    ordered = sorted(
        selected.values(),
        key=lambda row: (
            0 if (
                row.audience_type == 'actor'
                and (
                    row.last_applied_source_expedition_log_id is None
                    or int(row.last_applied_source_expedition_log_id)
                    != int(row.last_source_expedition_log_id)
                )
            ) else 1,
            0 if (
                row.audience_type == 'actor'
                and int(row.last_opportunity_source_expedition_log_id or 0)
                != int(row.last_source_expedition_log_id or 0)
            ) else 1,
            row.last_opportunity_at or datetime.min,
            -_KNOWLEDGE_LEVEL_RANK.get(row.knowledge_level, 0),
            -int(row.encounter_count or 0),
            -int(row.id),
        ),
    )
    return [_countermeasure_knowledge_prompt_ref(row) for row in ordered]


def _countermeasure_prompt_header(candidate_refs: list[dict]) -> str:
    # Reserve the same source guidance before and after routing. A later
    # rotation item must not increase overhead and evict an AI-selected item.
    lines = [
        '已保存的遭遇经验（持有者：本段世界主体；“对方”指它观察过的一方）：',
        '本体项记录它当前已取得的经验与理解，势力项是它获知的原观察者经验。冒险者依各自经历、感知与已获情报行动。',
        '知识用于识别对方手段、判断成立条件并形成针对性应对；主体自身能力与状态以本段权威画像为准。',
        'knowledge_id 绑定完整机制身份；observed=已见反馈，understood=已有理解，prepared=已有准备；当前行动仍依本段主体的能力与条件。',
        '知识只传播经验，能力由持有者自身画像确定；当前角色在本职内使用这些资料，实际暴露、响应与结果由本场交互成立。',
    ]
    if any(ref.get('audience_type') == 'faction' for ref in candidate_refs):
        lines.append(
            '势力项的层级、认知与准备属于原观察者的经历；本段主体只是获知这份经验，'
            '须以自己的能力、资源和实际行动形成自己的准备。'
        )
    return '\n'.join(lines)


def _countermeasure_prompt_boundary(ref: dict) -> str:
    """Unwrap only the archive's exact field concatenation for display.

    The complete reference stays unchanged for identity, review and persistence.
    Legacy prose that does not have this exact prefix is not semantically edited.
    """
    boundary = str(ref.get('learned_boundary') or '')
    history_prefix = '\n'.join(
        str(ref[key]) for key in (
            'encountered_method', 'observed_effect', 'unresolved_reason',
        ) if ref.get(key)
    )
    if history_prefix and (
        boundary == history_prefix or boundary.startswith(history_prefix + '\n')
    ):
        boundary = boundary[len(history_prefix):].removeprefix('\n')
    direction = str(ref.get('countermeasure_direction') or '')
    if direction and boundary == direction:
        return ''
    if direction and boundary.endswith('\n' + direction):
        boundary = boundary[:-len(direction) - 1]
    return boundary


def _countermeasure_prompt_line(ref: dict) -> str:
    experience_owner = '持有者' if ref.get('audience_type') == 'actor' else '原观察者'
    audience = (
        '本体'
        if ref.get('audience_type') == 'actor'
        else f"势力:{ref.get('faction_key') or ''}"
    )
    current_knowledge = []
    if ref.get('countermeasure_direction'):
        current_knowledge.append(
            f"{experience_owner}应对={ref['countermeasure_direction']}",
        )
    boundary = _countermeasure_prompt_boundary(ref)
    if boundary:
        label = (
            '当时观察（尚未形成理解）'
            if ref.get('knowledge_level') == 'observed'
            else '对对方手段的认识'
        )
        current_knowledge.append(f'{experience_owner}{label}={boundary}')
    current_knowledge.append(f"曾遇到的对方做法={ref.get('encountered_method') or ''}")
    # Keep historical outcomes in the frozen reference for identity/audit, not
    # as a repeated self-vulnerability account in generation input.
    return (
        f"- [knowledge_id:{_coerce_int(ref.get('knowledge_id'))}][{audience}]"
        f"[{ref.get('knowledge_level') or ''}]"
        f"[遭遇:{max(1, _coerce_int(ref.get('encounter_count'), 1))}] "
        + '；'.join(current_knowledge)
    )


def build_countermeasure_prompt_budget(
    candidate_refs: list[dict] | None,
    *,
    max_chars: int | None = None,
) -> dict:
    """Measure the actual renderer, including source guidance and separators."""
    candidates = [ref for ref in (candidate_refs or []) if isinstance(ref, dict)
                  and _coerce_int(ref.get('knowledge_id')) > 0]
    prompt_limit = max(0, _coerce_int(current_app.config.get(
        'ADVENTURER_S1_COUNTERMEASURE_LEARNING_PROMPT_LIMIT', 32,
    ), 32))
    configured_max_chars = max(0, _coerce_int(current_app.config.get(
        'ADVENTURER_S1_COUNTERMEASURE_LEARNING_PROMPT_MAX_CHARS', 8000,
    ), 8000))
    effective_max_chars = configured_max_chars if max_chars is None else max(
        0, min(int(max_chars), configured_max_chars),
    )
    return {
        'selection_limit': min(prompt_limit, len(candidates)),
        'selection_budget_chars': max(
            0, effective_max_chars - len(_countermeasure_prompt_header(candidates)),
        ),
        'knowledge_prompt_chars': {
            _coerce_int(ref.get('knowledge_id')): len(_countermeasure_prompt_line(ref)) + 1
            for ref in candidates
        },
    }


def render_countermeasure_prompt_block(
    candidate_refs: list[dict] | None,
    *,
    max_chars: int | None = None,
    preferred_knowledge_ids: list[int] | None = None,
    knowledge_ids_out: list[int] | None = None,
    knowledge_refs_out: list[dict] | None = None,
) -> str:
    """Render candidates with validated semantic preferences ahead of fair rotation."""
    candidates = [
        dict(ref) for ref in (candidate_refs or [])
        if isinstance(ref, dict)
        and _coerce_int(ref.get('knowledge_id')) > 0
    ]
    budget = build_countermeasure_prompt_budget(candidates, max_chars=max_chars)
    prompt_limit = budget['selection_limit']
    remaining_chars = budget['selection_budget_chars']
    if prompt_limit <= 0 or remaining_chars <= 0:
        return ''
    candidate_by_id = {
        _coerce_int(ref.get('knowledge_id')): ref
        for ref in candidates
    }
    preferred_ids = []
    for raw_id in preferred_knowledge_ids or []:
        knowledge_id = _coerce_int(raw_id)
        if (
            knowledge_id > 0
            and knowledge_id in candidate_by_id
            and knowledge_id not in preferred_ids
        ):
            preferred_ids.append(knowledge_id)
    preferred_id_set = set(preferred_ids)
    ordered = [candidate_by_id[knowledge_id] for knowledge_id in preferred_ids]
    ordered.extend(
        ref for ref in candidates
        if _coerce_int(ref.get('knowledge_id')) not in preferred_id_set
    )
    lines = [_countermeasure_prompt_header(candidates)]
    injected_count = 0
    for ref in ordered:
        if injected_count >= prompt_limit:
            break
        knowledge_id = _coerce_int(ref.get('knowledge_id'))
        cost = budget['knowledge_prompt_chars'][knowledge_id]
        if cost > remaining_chars:
            continue
        lines.append(_countermeasure_prompt_line(ref))
        remaining_chars -= cost
        injected_count += 1
        if isinstance(knowledge_ids_out, list):
            knowledge_ids_out.append(knowledge_id)
        if isinstance(knowledge_refs_out, list):
            knowledge_refs_out.append({
                key: ref.get(key)
                for key in (
                    'knowledge_id',
                    'knowledge_source_expedition_log_id',
                    'knowledge_level',
                    'audience_type',
                    'audience_key',
                    'faction_key',
                    'observer_world_actor_id',
                    'observer_capability_revision',
                    'encountered_method',
                    'unresolved_reason',
                    'observed_effect',
                    'learned_boundary',
                    'countermeasure_direction',
                )
            })
    return '\n'.join(lines)


def build_countermeasure_prompt_block(
    actor: AdventurerS1WorldActor,
    faction_relations: list[AdventurerS1WorldActorFactionRelation],
    *,
    max_chars: int | None = None,
    preferred_knowledge_ids: list[int] | None = None,
    knowledge_ids_out: list[int] | None = None,
    knowledge_refs_out: list[dict] | None = None,
    knowledge_candidates_out: list[dict] | None = None,
) -> str:
    """Build bounded working memory without exposing player identity."""
    candidates = load_countermeasure_prompt_candidates(actor, faction_relations)
    if isinstance(knowledge_candidates_out, list):
        knowledge_candidates_out.extend(dict(ref) for ref in candidates)
    return render_countermeasure_prompt_block(
        candidates,
        max_chars=max_chars,
        preferred_knowledge_ids=preferred_knowledge_ids,
        knowledge_ids_out=knowledge_ids_out,
        knowledge_refs_out=knowledge_refs_out,
    )
