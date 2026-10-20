"""最终命途永久删除的冻结审核与结算协议。

主探险 AI 的 ``removed_traits`` / ``removed_skills`` 只是删除提案。只有同一份
玩家可见 events 经 Observer 对冻结动作逐项裁决，并在最终事务中仍能匹配出发资产
ID 与状态哈希时，提案才取得写权限。
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Iterable

from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)


FINAL_FATE_DESTRUCTIVE_PROTOCOL_VERSION = 1
FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD = (
    '_final_fate_destructive_change_context'
)
FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD = (
    '_final_fate_destructive_change_resolution'
)
FINAL_FATE_DESTRUCTIVE_REVIEW_FIELD = (
    'final_fate_destructive_change_review'
)
FINAL_FATE_DESTRUCTIVE_QUARANTINE_FIELD = (
    '_final_fate_destructive_change_quarantine'
)

FINAL_FATE_DESTRUCTIVE_DECISIONS = frozenset({
    'preserve_existing',
    'retire_without_successor',
    'replace_with_successors',
})

_REMOVAL_FIELDS = (
    ('removed_traits', 'trait', 'new_traits'),
    ('removed_skills', 'skill', 'new_skills'),
)


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _positive_int(value: object) -> int | None:
    normalized, _ = normalize_ascii_integer(value, minimum=1)
    return normalized if type(normalized) is int and normalized > 0 else None


def _strict_name_list(
    value: object,
    *,
    path: str,
) -> tuple[list[str], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], [f'{path}_not_list']
    normalized: list[str] = []
    errors: list[str] = []
    seen: set[str] = set()
    for index, raw_name in enumerate(value):
        item_path = f'{path}[{index}]'
        if not isinstance(raw_name, str):
            errors.append(f'{item_path}_not_string')
            continue
        name = raw_name.strip()
        if not name:
            errors.append(f'{item_path}_empty')
            continue
        if len(name) > 100:
            errors.append(f'{item_path}_too_long')
            continue
        if name in seen:
            errors.append(f'{item_path}_duplicate')
            continue
        seen.add(name)
        normalized.append(name)
    return normalized, errors


def _named_successors(value: object) -> list[dict]:
    rows: list[dict] = []
    if isinstance(value, dict):
        iterable: Iterable[tuple[object, object]] = value.items()
        for raw_name, raw_desc in iterable:
            name = str(raw_name or '').strip()
            if not name:
                continue
            if isinstance(raw_desc, dict):
                desc = raw_desc.get('description', raw_desc.get('desc', ''))
            else:
                desc = raw_desc
            rows.append({
                'name': name,
                'description': str(desc or '').strip(),
            })
        return rows
    if not isinstance(value, list):
        return rows
    for raw_entry in value:
        if isinstance(raw_entry, dict):
            name = str(raw_entry.get('name') or '').strip()
            desc = raw_entry.get(
                'description',
                raw_entry.get('desc', ''),
            )
        else:
            name = str(raw_entry or '').strip()
            desc = ''
        if name:
            rows.append({
                'name': name,
                'description': str(desc or '').strip(),
            })
    return rows


def _snapshot_asset_state(
    asset_kind: str,
    adventurer_id: int,
    row: dict,
) -> dict | None:
    asset_id = _positive_int(row.get('asset_id', row.get('id')))
    name = str(row.get('name') or '').strip()
    if asset_id is None or not name:
        return None
    if asset_kind == 'trait':
        return {
            'asset_kind': 'trait',
            'asset_id': asset_id,
            'adventurer_id': adventurer_id,
            'name': name,
            'description': str(row.get('description') or ''),
            'description_ai': str(row.get('description_ai') or ''),
            'is_archived': bool(row.get('is_archived', False)),
            'recovery_policy': str(row.get('recovery_policy') or 'none'),
            'recovery_stage': row.get('recovery_stage'),
            'recovery_progress': row.get('recovery_progress'),
            'recovery_required': row.get('recovery_required'),
        }
    return {
        'asset_kind': 'skill',
        'asset_id': asset_id,
        'adventurer_id': adventurer_id,
        'name': name,
        'description': str(row.get('description') or ''),
        'equipped': bool(row.get('equipped', True)),
    }


def _current_asset_state(
    asset_kind: str,
    adventurer_id: int,
    row: object,
) -> dict | None:
    asset_id = _positive_int(getattr(row, 'id', None))
    name = str(getattr(row, 'name', '') or '').strip()
    if asset_id is None or not name:
        return None
    if asset_kind == 'trait':
        return {
            'asset_kind': 'trait',
            'asset_id': asset_id,
            'adventurer_id': adventurer_id,
            'name': name,
            'description': str(getattr(row, 'description', '') or ''),
            'description_ai': str(getattr(row, 'description_ai', '') or ''),
            'is_archived': bool(getattr(row, 'is_archived', False)),
            'recovery_policy': str(
                getattr(row, 'recovery_policy', 'none') or 'none'
            ),
            'recovery_stage': getattr(row, 'recovery_stage', None),
            'recovery_progress': getattr(row, 'recovery_progress', None),
            'recovery_required': getattr(row, 'recovery_required', None),
        }
    return {
        'asset_kind': 'skill',
        'asset_id': asset_id,
        'adventurer_id': adventurer_id,
        'name': name,
        'description': str(getattr(row, 'description', '') or ''),
        'equipped': bool(getattr(row, 'equipped', True)),
    }


def _build_snapshot_assets(
    audit_member_snapshot: object,
) -> dict[tuple[int, str, str], list[dict]]:
    assets: dict[tuple[int, str, str], list[dict]] = {}
    if not isinstance(audit_member_snapshot, list):
        return assets
    for member in audit_member_snapshot:
        if not isinstance(member, dict):
            continue
        adventurer_id = _positive_int(member.get('adventurer_id'))
        if adventurer_id is None:
            continue
        for asset_kind, field in (('trait', 'traits'), ('skill', 'skills')):
            for row in member.get(field) or []:
                if not isinstance(row, dict):
                    continue
                name = str(row.get('name') or '').strip()
                if not name:
                    continue
                state = _snapshot_asset_state(
                    asset_kind,
                    adventurer_id,
                    row,
                )
                predecessor = {
                    'asset_id': (
                        state.get('asset_id') if isinstance(state, dict) else None
                    ),
                    'name': name,
                    'description': str(row.get('description') or ''),
                    'state_hash': (
                        _canonical_digest(state)
                        if isinstance(state, dict)
                        else None
                    ),
                }
                assets.setdefault(
                    (adventurer_id, asset_kind, name),
                    [],
                ).append(predecessor)
    return assets


def _build_successor_candidates(
    result_data: object,
    *,
    final_attempt_id: str,
    expedition_log_id: int,
) -> list[dict]:
    successors: list[dict] = []
    if not isinstance(result_data, dict):
        return successors
    updates = result_data.get('member_updates')
    if not isinstance(updates, list):
        return successors
    for update in updates:
        if not isinstance(update, dict):
            continue
        adventurer_id = _positive_int(update.get('id'))
        if adventurer_id is None:
            continue
        for new_field, asset_kind in (
            ('new_traits', 'trait'),
            ('new_skills', 'skill'),
        ):
            seen_successor_names: set[str] = set()
            for source_index, row in enumerate(
                _named_successors(update.get(new_field))
            ):
                name = row['name']
                if name in seen_successor_names:
                    continue
                seen_successor_names.add(name)
                successor_body = {
                    'adventurer_id': adventurer_id,
                    'asset_kind': asset_kind,
                    'source_field': new_field,
                    'source_index': source_index,
                    'name': name,
                    'description': row['description'],
                    'final_attempt_id': final_attempt_id,
                    'expedition_log_id': int(expedition_log_id),
                }
                successors.append({
                    'successor_ref': _canonical_digest(successor_body)[:24],
                    **successor_body,
                })
    return successors


def _refresh_existing_final_fate_context(
    context: object,
    result_data: dict,
    *,
    expedition_log_id: int,
) -> tuple[dict | None, str | None]:
    context_error = _context_integrity_error(context)
    if context_error:
        return None, context_error
    assert isinstance(context, dict)
    fate_context = result_data.get('fate_journey_context')
    if (
        not isinstance(fate_context, dict)
        or fate_context.get('event_type') != 'final'
    ):
        return None, 'fate_context_not_final'
    final_attempt_id = str(
        fate_context.get('final_attempt_id') or ''
    ).strip()
    focus_adventurer_id = _positive_int(
        fate_context.get('adventurer_id')
    )
    if (
        not final_attempt_id
        or focus_adventurer_id is None
        or context.get('expedition_log_id') != int(expedition_log_id)
        or context.get('final_attempt_id') != final_attempt_id
        or context.get('focus_adventurer_id') != focus_adventurer_id
    ):
        return None, 'fate_context_identity_changed'

    events = result_data.get('events')
    event_rows = events if isinstance(events, list) else []
    refreshed_body = copy.deepcopy(_context_without_id(context))
    refreshed_body['events_hash'] = _canonical_digest(event_rows)
    refreshed_body['event_count'] = len(event_rows)
    refreshed_body['successor_candidates'] = _build_successor_candidates(
        result_data,
        final_attempt_id=final_attempt_id,
        expedition_log_id=expedition_log_id,
    )
    return {
        'context_id': _canonical_digest(refreshed_body),
        **refreshed_body,
    }, None


def _sanitize_removals_against_existing_context(
    result_data: dict,
    context: dict,
) -> list[str]:
    allowed_by_path: dict[tuple[int, str], set[str]] = {}
    for action in context.get('actions') or []:
        if not isinstance(action, dict):
            continue
        adventurer_id = _positive_int(action.get('adventurer_id'))
        source_field = action.get('source_field')
        name = str(action.get('name') or '').strip()
        if (
            adventurer_id is not None
            and source_field in {'removed_traits', 'removed_skills'}
            and name
        ):
            allowed_by_path.setdefault(
                (adventurer_id, str(source_field)),
                set(),
            ).add(name)

    errors: list[str] = []
    for update_index, update in enumerate(
        result_data.get('member_updates') or []
    ):
        if not isinstance(update, dict):
            continue
        adventurer_id = _positive_int(update.get('id'))
        if adventurer_id is None:
            continue
        for removal_field, _, _ in _REMOVAL_FIELDS:
            if removal_field not in update:
                continue
            names, field_errors = _strict_name_list(
                update.get(removal_field),
                path=(
                    f'member_updates[{update_index}].{removal_field}'
                ),
            )
            if field_errors:
                update[removal_field] = []
                errors.extend(field_errors)
                continue
            allowed_names = allowed_by_path.get(
                (adventurer_id, removal_field),
                set(),
            )
            filtered = [name for name in names if name in allowed_names]
            if filtered != names:
                errors.append(
                    'existing_final_fate_context_unowned_removal_discarded:'
                    f'{adventurer_id}:{removal_field}'
                )
                update[removal_field] = filtered
            else:
                update[removal_field] = names
    return errors


def _context_without_id(context: dict) -> dict:
    return {
        key: value
        for key, value in context.items()
        if key != 'context_id'
    }


def _context_integrity_error(context: object) -> str | None:
    if not isinstance(context, dict):
        return 'context_not_object'
    context_id = context.get('context_id')
    if not isinstance(context_id, str) or not context_id:
        return 'context_id_missing'
    if context_id != _canonical_digest(_context_without_id(context)):
        return 'context_id_mismatch'
    if context.get('protocol_version') != FINAL_FATE_DESTRUCTIVE_PROTOCOL_VERSION:
        return 'protocol_version_mismatch'
    if not isinstance(context.get('actions'), list):
        return 'actions_not_list'
    if not isinstance(context.get('successor_candidates'), list):
        return 'successor_candidates_not_list'
    return None


def build_final_fate_destructive_context(
    result_data: object,
    audit_member_snapshot: object,
    *,
    expedition_log_id: int,
) -> tuple[dict | None, list[str]]:
    """Freeze every final-fate trait/skill removal proposal into stable actions."""
    if not isinstance(result_data, dict):
        return None, []
    fate_context = result_data.get('fate_journey_context')
    if (
        not isinstance(fate_context, dict)
        or fate_context.get('event_type') != 'final'
    ):
        return None, []
    final_attempt_id = str(
        fate_context.get('final_attempt_id') or ''
    ).strip()
    focus_adventurer_id = _positive_int(
        fate_context.get('adventurer_id')
    )
    if not final_attempt_id or focus_adventurer_id is None:
        return None, ['final_fate_context_identity_invalid']

    updates = result_data.get('member_updates')
    if not isinstance(updates, list):
        return None, []

    events = result_data.get('events')
    event_rows = events if isinstance(events, list) else []
    events_hash = _canonical_digest(event_rows)
    frozen_assets = _build_snapshot_assets(audit_member_snapshot)
    errors: list[str] = []
    actions: list[dict] = []
    successors = _build_successor_candidates(
        result_data,
        final_attempt_id=final_attempt_id,
        expedition_log_id=expedition_log_id,
    )

    for update_index, update in enumerate(updates):
        if not isinstance(update, dict):
            continue
        adventurer_id = _positive_int(update.get('id'))
        if adventurer_id is None:
            continue

        for removal_field, asset_kind, _ in _REMOVAL_FIELDS:
            if removal_field not in update:
                continue
            names, field_errors = _strict_name_list(
                update.get(removal_field),
                path=(
                    f'member_updates[{update_index}].{removal_field}'
                ),
            )
            errors.extend(field_errors)
            for source_index, name in enumerate(names):
                matches = frozen_assets.get(
                    (adventurer_id, asset_kind, name),
                    [],
                )
                predecessor = matches[0] if len(matches) == 1 else {
                    'asset_id': None,
                    'name': name,
                    'description': (
                        matches[0].get('description', '')
                        if matches else ''
                    ),
                    'state_hash': None,
                }
                action_body = {
                    'adventurer_id': adventurer_id,
                    'asset_kind': asset_kind,
                    'source_field': removal_field,
                    'source_index': source_index,
                    'name': name,
                    'predecessor': predecessor,
                    'final_attempt_id': final_attempt_id,
                    'expedition_log_id': int(expedition_log_id),
                }
                actions.append({
                    'action_ref': _canonical_digest(action_body)[:24],
                    **action_body,
                })

    if not actions and not errors:
        return None, []

    context_body = {
        'protocol_version': FINAL_FATE_DESTRUCTIVE_PROTOCOL_VERSION,
        'expedition_log_id': int(expedition_log_id),
        'final_attempt_id': final_attempt_id,
        'focus_adventurer_id': focus_adventurer_id,
        'events_hash': events_hash,
        'event_count': len(event_rows),
        'actions': actions,
        'successor_candidates': successors,
    }
    return {
        'context_id': _canonical_digest(context_body),
        **context_body,
    }, errors


def prepare_final_fate_destructive_context(
    result_data: dict,
    audit_member_snapshot: object,
    *,
    expedition_log_id: int,
) -> tuple[dict, dict | None, list[str]]:
    """Replace model-supplied internals with a server-derived frozen context."""
    candidate = copy.deepcopy(result_data)
    previous_context = candidate.get(
        FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD
    )
    previous_resolution = candidate.pop(
        FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD,
        None,
    )
    candidate.pop(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD, None)

    context, refresh_error = _refresh_existing_final_fate_context(
        previous_context,
        candidate,
        expedition_log_id=expedition_log_id,
    )
    reused_existing_context = context is not None
    errors: list[str] = []
    if context is not None:
        errors.extend(
            _sanitize_removals_against_existing_context(
                candidate,
                context,
            )
        )
    else:
        context, errors = build_final_fate_destructive_context(
            candidate,
            audit_member_snapshot,
            expedition_log_id=expedition_log_id,
        )
        if previous_context not in (None, {}) and refresh_error:
            errors.append(
                'existing_final_fate_context_discarded:'
                f'{refresh_error}'
            )
    if errors:
        # Invalid destructive payloads never inherit write permission. Valid
        # sibling fields remain available for normal settlement.
        for update in candidate.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            for removal_field, _, _ in _REMOVAL_FIELDS:
                if removal_field not in update:
                    continue
                _, field_errors = _strict_name_list(
                    update.get(removal_field),
                    path=removal_field,
                )
                if field_errors:
                    update[removal_field] = []
        if not reused_existing_context:
            context, _ = build_final_fate_destructive_context(
                candidate,
                audit_member_snapshot,
                expedition_log_id=expedition_log_id,
            )

    if context is None:
        candidate.pop(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD, None)
        candidate.pop(FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD, None)
        return candidate, None, errors

    candidate[FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD] = context
    normalized_resolution, resolution_error = (
        validate_final_fate_destructive_review(
            previous_resolution,
            context,
            needs_regeneration=False,
        )
    )
    if resolution_error is None and normalized_resolution is not None:
        # The receipt, not a downstream Guard/checkpoint copy of the removal
        # arrays, owns the executable subset. Re-derive it on every resume so
        # clearing, expansion, or replacement cannot create a permanent
        # settlement mismatch or bypass the reviewed decisions.
        candidate, _ = apply_final_fate_destructive_review(
            candidate,
            context,
            normalized_resolution,
        )
    return candidate, context, errors


def project_final_fate_destructive_context_for_observer(
    context: object,
) -> dict:
    """Whitelist the frozen review input without exposing DB ids or state hashes."""
    if _context_integrity_error(context):
        return {}
    assert isinstance(context, dict)
    successors_by_kind: dict[tuple[int, str], list[dict]] = {}
    for row in context.get('successor_candidates') or []:
        if not isinstance(row, dict):
            continue
        adventurer_id = _positive_int(row.get('adventurer_id'))
        asset_kind = row.get('asset_kind')
        successor_ref = row.get('successor_ref')
        name = row.get('name')
        if (
            adventurer_id is None
            or asset_kind not in {'trait', 'skill'}
            or not isinstance(successor_ref, str)
            or not isinstance(name, str)
        ):
            continue
        successors_by_kind.setdefault(
            (adventurer_id, asset_kind),
            [],
        ).append({
            'successor_ref': successor_ref,
            'name': name,
            'description': str(row.get('description') or ''),
        })

    actions: list[dict] = []
    for row in context.get('actions') or []:
        if not isinstance(row, dict):
            continue
        predecessor = (
            row.get('predecessor')
            if isinstance(row.get('predecessor'), dict)
            else {}
        )
        adventurer_id = _positive_int(row.get('adventurer_id'))
        asset_kind = row.get('asset_kind')
        if adventurer_id is None or asset_kind not in {'trait', 'skill'}:
            continue
        actions.append({
            'action_ref': row.get('action_ref'),
            'adventurer_id': adventurer_id,
            'field': row.get('source_field'),
            'name': row.get('name'),
            'current_description': str(
                predecessor.get('description') or ''
            ),
            'predecessor_bound': bool(
                _positive_int(predecessor.get('asset_id'))
                and predecessor.get('state_hash')
            ),
            'successor_candidates': successors_by_kind.get(
                (adventurer_id, asset_kind),
                [],
            ),
        })
    return {
        'context_id': context.get('context_id'),
        'final_attempt_id': context.get('final_attempt_id'),
        'focus_adventurer_id': context.get('focus_adventurer_id'),
        'event_count': context.get('event_count'),
        'actions': actions,
    }


def _removed_override_paths(
    override_fields: object,
) -> set[tuple[int, str]]:
    paths: set[tuple[int, str]] = set()
    if not isinstance(override_fields, dict):
        return paths
    rows = override_fields.get('member_updates')
    if not isinstance(rows, list):
        return paths
    for row in rows:
        if not isinstance(row, dict):
            continue
        adventurer_id = _positive_int(row.get('id'))
        if adventurer_id is None:
            continue
        for removal_field, _, _ in _REMOVAL_FIELDS:
            if removal_field in row:
                paths.add((adventurer_id, removal_field))
    return paths


def validate_final_fate_destructive_review(
    value: object,
    context: object,
    *,
    needs_regeneration: bool,
    override_fields: object = None,
) -> tuple[dict | None, str | None]:
    """Validate exact action coverage and the monotonic destructive decisions."""
    # No frozen context means there is no consumer and therefore no authority
    # for this sibling field. Treat any model-authored value as an inert extra.
    if context is None:
        return None, None
    context_error = _context_integrity_error(context)
    if context_error:
        return None, f'context_invalid:{context_error}'
    assert isinstance(context, dict)

    if not isinstance(value, dict):
        return None, 'review_not_object'
    from services.tavern_ai_stage_authority import verified_fate
    policy = verified_fate(value, context)
    if policy is not None and not needs_regeneration:
        return policy, None
    projected = {
        key: value[key]
        for key in ('context_id', 'items')
        if key in value
    }
    if projected.get('context_id') != context.get('context_id'):
        return None, 'context_id_mismatch'
    raw_items = projected.get('items')
    if not isinstance(raw_items, list):
        return None, 'items_not_list'
    if needs_regeneration:
        if raw_items:
            return None, 'regeneration_items_must_be_empty'
        return {
            'context_id': context['context_id'],
            'items': [],
        }, None

    action_rows = [
        row for row in context.get('actions') or []
        if isinstance(row, dict)
    ]
    action_by_ref = {
        row.get('action_ref'): row
        for row in action_rows
        if isinstance(row.get('action_ref'), str)
    }
    if len(action_by_ref) != len(action_rows):
        return None, 'context_action_refs_invalid'

    owned_override_paths = {
        (
            int(row['adventurer_id']),
            str(row['source_field']),
        )
        for row in action_rows
        if _positive_int(row.get('adventurer_id')) is not None
        and row.get('source_field') in {
            'removed_traits',
            'removed_skills',
        }
    }
    if _removed_override_paths(override_fields) & owned_override_paths:
        return None, 'removed_fields_must_not_be_duplicated_in_overrides'

    successor_by_ref = {
        row.get('successor_ref'): row
        for row in context.get('successor_candidates') or []
        if isinstance(row, dict)
        and isinstance(row.get('successor_ref'), str)
    }
    normalized_by_ref: dict[str, dict] = {}
    for index, raw_item in enumerate(raw_items):
        item_path = f'items[{index}]'
        if not isinstance(raw_item, dict):
            return None, f'{item_path}_not_object'
        item = {
            key: raw_item[key]
            for key in (
                'action_ref',
                'decision',
                'successor_refs',
                'evidence_event_indexes',
            )
            if key in raw_item
        }
        action_ref = item.get('action_ref')
        if (
            not isinstance(action_ref, str)
            or action_ref not in action_by_ref
            or action_ref in normalized_by_ref
        ):
            return None, f'{item_path}.action_ref_invalid'
        decision = item.get('decision')
        if decision not in FINAL_FATE_DESTRUCTIVE_DECISIONS:
            return None, f'{item_path}.decision_invalid'
        raw_successor_refs = item.get('successor_refs')
        raw_evidence_indexes = item.get('evidence_event_indexes')
        if not isinstance(raw_successor_refs, list):
            return None, f'{item_path}.successor_refs_not_list'
        if not isinstance(raw_evidence_indexes, list):
            return None, f'{item_path}.evidence_event_indexes_not_list'

        successor_refs: list[str] = []
        for successor_ref in raw_successor_refs:
            if (
                not isinstance(successor_ref, str)
                or successor_ref in successor_refs
            ):
                return None, f'{item_path}.successor_refs_invalid'
            successor = successor_by_ref.get(successor_ref)
            action = action_by_ref[action_ref]
            if (
                not isinstance(successor, dict)
                or successor.get('adventurer_id')
                != action.get('adventurer_id')
                or successor.get('asset_kind')
                != action.get('asset_kind')
            ):
                return None, f'{item_path}.successor_refs_out_of_scope'
            successor_refs.append(successor_ref)

        evidence_indexes: list[int] = []
        for raw_index in raw_evidence_indexes:
            normalized_index, _ = normalize_ascii_integer(
                raw_index,
                minimum=0,
            )
            if (
                type(normalized_index) is not int
                or normalized_index < 0
                or normalized_index >= int(context.get('event_count') or 0)
                or normalized_index in evidence_indexes
            ):
                return None, (
                    f'{item_path}.evidence_event_indexes_invalid'
                )
            evidence_indexes.append(normalized_index)

        predecessor = action_by_ref[action_ref].get('predecessor')
        predecessor_bound = bool(
            isinstance(predecessor, dict)
            and _positive_int(predecessor.get('asset_id'))
            and predecessor.get('state_hash')
        )
        if not predecessor_bound and decision != 'preserve_existing':
            return None, f'{item_path}.unbound_predecessor_must_preserve'
        if decision == 'preserve_existing':
            if successor_refs or evidence_indexes:
                return None, f'{item_path}.preserve_payload_must_be_empty'
        elif decision == 'retire_without_successor':
            if successor_refs or not evidence_indexes:
                return None, f'{item_path}.retire_payload_invalid'
        elif not successor_refs or not evidence_indexes:
            return None, f'{item_path}.replacement_payload_invalid'

        normalized_by_ref[action_ref] = {
            'action_ref': action_ref,
            'decision': decision,
            'successor_refs': successor_refs,
            'evidence_event_indexes': evidence_indexes,
        }

    if set(normalized_by_ref) != set(action_by_ref):
        return None, 'items_must_cover_each_action_exactly_once'
    return {
        'context_id': context['context_id'],
        'items': [
            normalized_by_ref[str(row['action_ref'])]
            for row in action_rows
        ],
    }, None


def apply_final_fate_destructive_review(
    result_data: dict,
    context: dict,
    review: dict,
) -> tuple[dict, list[str]]:
    """Derive final removal lists from the validated independent receipt."""
    normalized, error = validate_final_fate_destructive_review(
        review,
        context,
        needs_regeneration=False,
    )
    if error or normalized is None:
        raise ValueError(
            'final_fate_destructive_review_invalid:'
            f'{error or "unknown"}'
        )
    updated = copy.deepcopy(result_data)
    updates_by_id = {
        _positive_int(row.get('id')): row
        for row in updated.get('member_updates') or []
        if isinstance(row, dict)
        and _positive_int(row.get('id')) is not None
    }
    item_by_ref = {
        row['action_ref']: row
        for row in normalized['items']
    }
    approved_by_path: dict[tuple[int, str], list[str]] = {}
    owned_paths: list[tuple[int, str]] = []
    for action in context.get('actions') or []:
        if not isinstance(action, dict):
            continue
        adventurer_id = _positive_int(action.get('adventurer_id'))
        source_field = action.get('source_field')
        action_ref = action.get('action_ref')
        if (
            adventurer_id is None
            or source_field not in {'removed_traits', 'removed_skills'}
            or not isinstance(action_ref, str)
        ):
            continue
        path = (adventurer_id, source_field)
        if path not in owned_paths:
            owned_paths.append(path)
        decision = (item_by_ref.get(action_ref) or {}).get('decision')
        if decision in {
            'retire_without_successor',
            'replace_with_successors',
            'admin_original_proposal',
        }:
            approved_by_path.setdefault(path, []).append(
                str(action.get('name') or '')
            )

    changed: list[str] = []
    for adventurer_id, source_field in owned_paths:
        update = updates_by_id.get(adventurer_id)
        if not isinstance(update, dict):
            continue
        approved_names = approved_by_path.get(
            (adventurer_id, source_field),
            [],
        )
        if update.get(source_field) != approved_names:
            update[source_field] = approved_names
            changed.append(
                f'member_updates.{adventurer_id}.{source_field}'
            )
    updated[FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD] = copy.deepcopy(context)
    updated[FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD] = {
        'protocol_version': FINAL_FATE_DESTRUCTIVE_PROTOCOL_VERSION,
        **normalized,
    }
    return updated, changed


def final_fate_destructive_change_requires_review(
    result_data: object,
) -> bool:
    if not isinstance(result_data, dict):
        return False
    fate_context = result_data.get('fate_journey_context')
    if (
        not isinstance(fate_context, dict)
        or fate_context.get('event_type') != 'final'
    ):
        return False
    context = result_data.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD)
    if isinstance(context, dict):
        normalized, error = validate_final_fate_destructive_review(
            result_data.get(FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD),
            context,
            needs_regeneration=False,
        )
        return error is not None or normalized is None
    for row in result_data.get('member_updates') or []:
        if not isinstance(row, dict):
            continue
        if row.get('removed_traits') or row.get('removed_skills'):
            return True
    return False


def _approved_actions(
    context: dict,
    resolution: dict,
) -> list[tuple[dict, dict]]:
    item_by_ref = {
        row.get('action_ref'): row
        for row in resolution.get('items') or []
        if isinstance(row, dict)
    }
    approved: list[tuple[dict, dict]] = []
    for action in context.get('actions') or []:
        if not isinstance(action, dict):
            continue
        item = item_by_ref.get(action.get('action_ref'))
        if (
            isinstance(item, dict)
            and item.get('decision') in {
                'retire_without_successor',
                'replace_with_successors',
                'admin_original_proposal',
            }
        ):
            approved.append((action, item))
    return approved


def final_fate_destructive_predecessor_ids(
    full_log: object,
) -> tuple[list[int], list[int]]:
    """Return stable child-row ids that must be locked before final validation."""
    if not isinstance(full_log, dict):
        return [], []
    context = full_log.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD)
    if _context_integrity_error(context):
        return [], []
    assert isinstance(context, dict)
    trait_ids: set[int] = set()
    skill_ids: set[int] = set()
    for action in context.get('actions') or []:
        if not isinstance(action, dict):
            continue
        predecessor = action.get('predecessor')
        if not isinstance(predecessor, dict):
            continue
        asset_id = _positive_int(predecessor.get('asset_id'))
        if asset_id is None:
            continue
        if action.get('asset_kind') == 'trait':
            trait_ids.add(asset_id)
        elif action.get('asset_kind') == 'skill':
            skill_ids.add(asset_id)
    return sorted(trait_ids), sorted(skill_ids)


def final_fate_destructive_approved_asset_ids(
    full_log: object,
) -> dict[int, dict[str, set[int]]]:
    """Return the exact approved predecessor ids grouped for the final writer."""
    if not isinstance(full_log, dict):
        return {}
    context = full_log.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD)
    if _context_integrity_error(context):
        return {}
    assert isinstance(context, dict)
    normalized, error = validate_final_fate_destructive_review(
        full_log.get(FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD),
        context,
        needs_regeneration=False,
    )
    if error or normalized is None:
        return {}
    approved_ids: dict[int, dict[str, set[int]]] = {}
    for action, _ in _approved_actions(context, normalized):
        adventurer_id = _positive_int(action.get('adventurer_id'))
        predecessor = action.get('predecessor')
        asset_id = (
            _positive_int(predecessor.get('asset_id'))
            if isinstance(predecessor, dict)
            else None
        )
        asset_kind = action.get('asset_kind')
        if (
            adventurer_id is None
            or asset_id is None
            or asset_kind not in {'trait', 'skill'}
        ):
            continue
        approved_ids.setdefault(adventurer_id, {
            'trait': set(),
            'skill': set(),
        })[str(asset_kind)].add(asset_id)
    return approved_ids


def final_fate_destructive_owned_paths(
    full_log: object,
) -> set[tuple[int, str]]:
    if not isinstance(full_log, dict):
        return set()
    context = full_log.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD)
    if _context_integrity_error(context):
        return set()
    assert isinstance(context, dict)
    return {
        (int(action['adventurer_id']), str(action['source_field']))
        for action in context.get('actions') or []
        if isinstance(action, dict)
        and _positive_int(action.get('adventurer_id')) is not None
        and action.get('source_field') in {
            'removed_traits',
            'removed_skills',
        }
    }


def _clear_all_final_fate_removals(result_data: dict) -> None:
    for update in result_data.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        for removal_field, _, _ in _REMOVAL_FIELDS:
            if removal_field in update:
                update[removal_field] = []


def quarantine_unexecutable_final_fate_destructive_changes(
    full_log: object,
    locked_members: object,
    *,
    locked_traits: object = None,
    locked_skills: object = None,
) -> tuple[dict, list[dict]]:
    """Monotonically revoke stale deletion permissions without blocking settlement."""
    if not isinstance(full_log, dict):
        return {}, []
    updated = copy.deepcopy(full_log)
    fate_context = updated.get('fate_journey_context')
    if (
        not isinstance(fate_context, dict)
        or fate_context.get('event_type') != 'final'
    ):
        return updated, []

    context = updated.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD)
    context_error = _context_integrity_error(context)
    normalized: dict | None = None
    resolution_error: str | None = None
    if context_error is None:
        assert isinstance(context, dict)
        normalized, resolution_error = validate_final_fate_destructive_review(
            updated.get(FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD),
            context,
            needs_regeneration=False,
        )
    if context_error or resolution_error or normalized is None:
        had_removals = any(
            bool(update.get('removed_traits') or update.get('removed_skills'))
            for update in updated.get('member_updates') or []
            if isinstance(update, dict)
        )
        if not had_removals and context in (None, {}):
            return updated, []
        _clear_all_final_fate_removals(updated)
        updated.pop(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD, None)
        updated.pop(FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD, None)
        audit = [{
            'action_ref': None,
            'reason_code': (
                'legacy_unreviewed_removals'
                if context in (None, {})
                else 'active_context_invalid'
            ),
        }]
        updated[FINAL_FATE_DESTRUCTIVE_QUARANTINE_FIELD] = [
            *(
                updated.get(FINAL_FATE_DESTRUCTIVE_QUARANTINE_FIELD)
                if isinstance(
                    updated.get(FINAL_FATE_DESTRUCTIVE_QUARANTINE_FIELD),
                    list,
                )
                else []
            ),
            *audit,
        ]
        return updated, audit

    assert isinstance(context, dict)
    approved = _approved_actions(context, normalized)
    if not approved:
        normalized_result, _ = apply_final_fate_destructive_review(
            updated,
            context,
            normalized,
        )
        return normalized_result, []

    stale_by_ref: dict[str, str] = {}
    event_rows = (
        updated.get('events')
        if isinstance(updated.get('events'), list)
        else []
    )
    if (
        len(event_rows) != context.get('event_count')
        or _canonical_digest(event_rows) != context.get('events_hash')
    ):
        for action, _ in approved:
            stale_by_ref[str(action.get('action_ref'))] = (
                'final_events_changed'
            )

    current_successor_refs = {
        row.get('successor_ref')
        for row in _build_successor_candidates(
            updated,
            final_attempt_id=str(context.get('final_attempt_id') or ''),
            expedition_log_id=int(context.get('expedition_log_id') or 0),
        )
        if isinstance(row.get('successor_ref'), str)
    }
    member_map = {
        _positive_int(getattr(member, 'id', None)): member
        for member in (locked_members or [])
        if _positive_int(getattr(member, 'id', None)) is not None
    }
    explicit_assets: dict[tuple[str, int], object] | None = None
    if locked_traits is not None or locked_skills is not None:
        explicit_assets = {}
        for row in locked_traits or []:
            asset_id = _positive_int(getattr(row, 'id', None))
            if asset_id is not None:
                explicit_assets[('trait', asset_id)] = row
        for row in locked_skills or []:
            asset_id = _positive_int(getattr(row, 'id', None))
            if asset_id is not None:
                explicit_assets[('skill', asset_id)] = row

    actual_by_path: dict[tuple[int, str], list[str]] = {}
    for row in updated.get('member_updates') or []:
        if not isinstance(row, dict):
            continue
        adventurer_id = _positive_int(row.get('id'))
        if adventurer_id is None:
            continue
        for removal_field, _, _ in _REMOVAL_FIELDS:
            names, list_errors = _strict_name_list(
                row.get(removal_field),
                path=removal_field,
            )
            if list_errors:
                names = []
            actual_by_path[(adventurer_id, removal_field)] = names

    for action, item in approved:
        action_ref = str(action.get('action_ref') or '')
        adventurer_id = _positive_int(action.get('adventurer_id'))
        source_field = str(action.get('source_field') or '')
        if (
            adventurer_id is None
            or str(action.get('name') or '')
            not in actual_by_path.get((adventurer_id, source_field), [])
        ):
            stale_by_ref[action_ref] = 'approved_removal_missing'
        if item.get('decision') == 'replace_with_successors' and any(
            successor_ref not in current_successor_refs
            for successor_ref in item.get('successor_refs') or []
        ):
            stale_by_ref[action_ref] = 'approved_successor_changed'

        predecessor = action.get('predecessor')
        asset_id = (
            _positive_int(predecessor.get('asset_id'))
            if isinstance(predecessor, dict)
            else None
        )
        state_hash = (
            predecessor.get('state_hash')
            if isinstance(predecessor, dict)
            else None
        )
        member = member_map.get(adventurer_id)
        asset_kind = str(action.get('asset_kind') or '')
        if explicit_assets is not None:
            current = (
                explicit_assets.get((asset_kind, asset_id))
                if asset_id is not None
                else None
            )
        else:
            collection = (
                getattr(member, 'traits', None)
                if asset_kind == 'trait'
                else getattr(member, 'skill_items', None)
            )
            current = next(
                (
                    row for row in (collection or [])
                    if _positive_int(getattr(row, 'id', None)) == asset_id
                ),
                None,
            )
        current_state = (
            _current_asset_state(asset_kind, adventurer_id, current)
            if current is not None and adventurer_id is not None
            else None
        )
        current_owner_id = (
            _positive_int(getattr(current, 'adventurer_id', None))
            if current is not None
            else None
        )
        if (
            asset_id is None
            or not isinstance(state_hash, str)
            or not state_hash
            or member is None
            or current_state is None
            or (
                current_owner_id is not None
                and current_owner_id != adventurer_id
            )
            or _canonical_digest(current_state) != state_hash
        ):
            stale_by_ref[action_ref] = 'predecessor_state_changed'

    if not stale_by_ref:
        normalized_result, _ = apply_final_fate_destructive_review(
            updated,
            context,
            normalized,
        )
        return normalized_result, []

    downgraded_review = copy.deepcopy(normalized)
    audit: list[dict] = []
    action_by_ref = {
        str(action.get('action_ref')): action
        for action in context.get('actions') or []
        if isinstance(action, dict)
    }
    for item in downgraded_review.get('items') or []:
        if not isinstance(item, dict):
            continue
        action_ref = str(item.get('action_ref') or '')
        reason_code = stale_by_ref.get(action_ref)
        if not reason_code:
            continue
        item['decision'] = 'preserve_existing'
        item['successor_refs'] = []
        item['evidence_event_indexes'] = []
        action = action_by_ref.get(action_ref) or {}
        predecessor = action.get('predecessor')
        audit.append({
            'action_ref': action_ref,
            'reason_code': reason_code,
            'adventurer_id': action.get('adventurer_id'),
            'asset_kind': action.get('asset_kind'),
            'asset_id': (
                predecessor.get('asset_id')
                if isinstance(predecessor, dict)
                else None
            ),
            'name': action.get('name'),
        })
    updated, _ = apply_final_fate_destructive_review(
        updated,
        context,
        downgraded_review,
    )
    updated[FINAL_FATE_DESTRUCTIVE_QUARANTINE_FIELD] = [
        *(
            updated.get(FINAL_FATE_DESTRUCTIVE_QUARANTINE_FIELD)
            if isinstance(
                updated.get(FINAL_FATE_DESTRUCTIVE_QUARANTINE_FIELD),
                list,
            )
            else []
        ),
        *audit,
    ]
    return updated, audit


def validate_final_fate_destructive_projection(full_log: object) -> list[str]:
    """Verify the exact frozen action package against final JSON, before asset locks."""
    if not isinstance(full_log, dict):
        return []
    fate_context = full_log.get('fate_journey_context')
    if (
        not isinstance(fate_context, dict)
        or fate_context.get('event_type') != 'final'
    ):
        return []

    final_removals: dict[tuple[int, str], list[str]] = {}
    raw_removal_present = False
    for row_index, row in enumerate(full_log.get('member_updates') or []):
        if not isinstance(row, dict):
            continue
        adventurer_id = _positive_int(row.get('id'))
        if adventurer_id is None:
            continue
        for removal_field, _, _ in _REMOVAL_FIELDS:
            if removal_field not in row:
                continue
            names, errors = _strict_name_list(
                row.get(removal_field),
                path=(
                    f'member_updates[{row_index}].{removal_field}'
                ),
            )
            if errors:
                return errors
            final_removals[(adventurer_id, removal_field)] = names
            raw_removal_present = raw_removal_present or bool(names)

    context = full_log.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD)
    resolution = full_log.get(FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD)
    if not isinstance(context, dict):
        return (
            ['final_fate_destructive_context_required']
            if raw_removal_present
            else []
        )
    context_error = _context_integrity_error(context)
    if context_error:
        return [f'final_fate_destructive_context_invalid:{context_error}']
    normalized, resolution_error = validate_final_fate_destructive_review(
        resolution,
        context,
        needs_regeneration=False,
    )
    if resolution_error or normalized is None:
        return [
            'final_fate_destructive_resolution_invalid:'
            f'{resolution_error or "missing"}'
        ]

    approved = _approved_actions(context, normalized)
    if approved:
        event_rows = (
            full_log.get('events')
            if isinstance(full_log.get('events'), list)
            else []
        )
        if (
            len(event_rows) != context.get('event_count')
            or _canonical_digest(event_rows) != context.get('events_hash')
        ):
            return ['final_fate_destructive_events_changed']
        current_successor_refs = {
            row.get('successor_ref')
            for row in _build_successor_candidates(
                full_log,
                final_attempt_id=str(
                    context.get('final_attempt_id') or ''
                ),
                expedition_log_id=int(
                    context.get('expedition_log_id') or 0
                ),
            )
            if isinstance(row.get('successor_ref'), str)
        }
        for _, item in approved:
            if (
                item.get('decision') == 'replace_with_successors'
                and any(
                    successor_ref not in current_successor_refs
                    for successor_ref in item.get('successor_refs') or []
                )
            ):
                return ['final_fate_destructive_successor_state_changed']
    expected_by_path: dict[tuple[int, str], list[str]] = {}
    for action, _ in approved:
        adventurer_id = _positive_int(action.get('adventurer_id'))
        source_field = action.get('source_field')
        if (
            adventurer_id is not None
            and source_field in {'removed_traits', 'removed_skills'}
        ):
            expected_by_path.setdefault(
                (adventurer_id, source_field),
                [],
            ).append(str(action.get('name') or ''))
    owned_paths = {
        (
            int(action['adventurer_id']),
            str(action['source_field']),
        )
        for action in context.get('actions') or []
        if isinstance(action, dict)
        and _positive_int(action.get('adventurer_id')) is not None
        and action.get('source_field') in {
            'removed_traits',
            'removed_skills',
        }
    }
    for path in owned_paths | set(final_removals):
        if final_removals.get(path, []) != expected_by_path.get(path, []):
            return [
                'final_fate_destructive_removed_list_mismatch:'
                f'{path[0]}:{path[1]}'
            ]
    return []


def validate_final_fate_destructive_settlement(
    full_log: object,
    locked_members: object,
    *,
    locked_traits: object = None,
    locked_skills: object = None,
) -> list[str]:
    """Verify the receipt against final JSON and current locked asset rows."""
    errors = validate_final_fate_destructive_projection(full_log)
    if errors:
        return errors
    fate_context = full_log.get('fate_journey_context') if isinstance(full_log, dict) else None
    if not isinstance(fate_context, dict) or fate_context.get('event_type') != 'final':
        return []
    context = full_log.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD)
    if not isinstance(context, dict):
        return []
    normalized, _ = validate_final_fate_destructive_review(
        full_log.get(FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD), context,
        needs_regeneration=False,
    )
    approved = _approved_actions(context, normalized)
    current_successor_refs = {
        row.get('successor_ref') for row in _build_successor_candidates(
            full_log, final_attempt_id=str(context.get('final_attempt_id') or ''),
            expedition_log_id=int(context.get('expedition_log_id') or 0),
        )
    }

    member_map = {
        _positive_int(getattr(member, 'id', None)): member
        for member in (locked_members or [])
        if _positive_int(getattr(member, 'id', None)) is not None
    }
    explicitly_locked_assets: dict[tuple[str, int], object] | None = None
    if locked_traits is not None or locked_skills is not None:
        explicitly_locked_assets = {}
        for row in locked_traits or []:
            asset_id = _positive_int(getattr(row, 'id', None))
            if asset_id is not None:
                explicitly_locked_assets[('trait', asset_id)] = row
        for row in locked_skills or []:
            asset_id = _positive_int(getattr(row, 'id', None))
            if asset_id is not None:
                explicitly_locked_assets[('skill', asset_id)] = row
    for action, item in approved:
        adventurer_id = _positive_int(action.get('adventurer_id'))
        predecessor = action.get('predecessor')
        if adventurer_id is None or not isinstance(predecessor, dict):
            return ['final_fate_destructive_predecessor_missing']
        asset_id = _positive_int(predecessor.get('asset_id'))
        state_hash = predecessor.get('state_hash')
        member = member_map.get(adventurer_id)
        if (
            asset_id is None
            or not isinstance(state_hash, str)
            or not state_hash
            or member is None
        ):
            return [
                'final_fate_destructive_predecessor_unbound:'
                f'{adventurer_id}:{action.get("name")}'
            ]
        asset_kind = action.get('asset_kind')
        if explicitly_locked_assets is not None:
            current = explicitly_locked_assets.get(
                (str(asset_kind), asset_id),
            )
        else:
            collection = (
                getattr(member, 'traits', None)
                if asset_kind == 'trait'
                else getattr(member, 'skill_items', None)
            )
            current = next(
                (
                    row for row in (collection or [])
                    if _positive_int(getattr(row, 'id', None)) == asset_id
                ),
                None,
            )
        current_owner_id = (
            _positive_int(getattr(current, 'adventurer_id', None))
            if current is not None
            else None
        )
        current_state = (
            _current_asset_state(asset_kind, adventurer_id, current)
            if current is not None
            else None
        )
        if (
            current_state is None
            or (
                current_owner_id is not None
                and current_owner_id != adventurer_id
            )
            or _canonical_digest(current_state) != state_hash
        ):
            return [
                'final_fate_destructive_predecessor_state_changed:'
                f'{adventurer_id}:{asset_id}'
            ]

        if item.get('decision') != 'replace_with_successors':
            continue
        for successor_ref in item.get('successor_refs') or []:
            if successor_ref not in current_successor_refs:
                return [
                    'final_fate_destructive_successor_missing:'
                    f'{adventurer_id}:{successor_ref}'
                ]
    return []
