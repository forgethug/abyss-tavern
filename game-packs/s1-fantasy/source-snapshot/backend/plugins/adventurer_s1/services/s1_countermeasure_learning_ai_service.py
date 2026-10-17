"""Review mechanisms and sources, then design adaptation from perceived feedback.

Learning and optional growth review share a frozen route. Historical sealed experiences retain their
archive-only recovery; settlement remains the only database writer.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from typing import Callable

import requests
from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    S1AiResponseContractResult,
    parse_s1_ai_http_json_response,
)
from plugins.adventurer_s1.services.s1_ai_player_name_service import (
    post_s1_ai_request_with_player_aliases as post_s1_ai_request,
)
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
    AIProfileAssignmentError,
    get_s1_ai_response_metadata,
    resolve_s1_ai_profile_snapshot,
)
from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import (
    COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD,
    _contract_limits,
    _RESOURCE_SNAPSHOT_FIELDS,
    _RESOURCE_UPDATE_FIELDS,
    _safe_json_evidence,
    frozen_countermeasure_knowledge_refs,
    objective_actor_revisions,
    prepare_countermeasure_learning_for_settlement,
)
from plugins.adventurer_s1.services.s1_countermeasure_learning_contract import (
    COUNTERMEASURE_LEARNING_OWNER_LOG_FIELD as RECEIPT_FIELD,
    COUNTERMEASURE_LEARNING_OWNER_SCHEMA_VERSION as SCHEMA_VERSION,
    COUNTERMEASURE_LEARNING_FROZEN_FIELDS,
    _normalize_source_refs,
    _positive_int,
    COUNTERMEASURE_KNOWLEDGE_LEVELS,
)
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage


FEATURE = 'expedition_countermeasure_learning'
_EXPERIENCE_FACT_FIELDS = ('encountered_method', 'observed_effect', 'unresolved_reason')
_EXPERIENCE_TEXT_FIELDS = (*_EXPERIENCE_FACT_FIELDS, 'learned_boundary', 'countermeasure_direction')
_EXPERIENCE_SEMANTIC_FIELDS = (*_EXPERIENCE_FACT_FIELDS, 'knowledge_level', 'countermeasure_direction')
_EXPERIENCE_FORMAT = 'opponent_method_learning_v3'
_RESUMABLE_EXPERIENCE_FORMATS = frozenset({
    _EXPERIENCE_FORMAT, 'backend_bound_learning_v2', 'current_fact_fields_v1',
})

ARCHIVE_SYSTEM_PROMPT = """你负责深渊酒馆 S1 已形成经验的来源归档。experiences 是经验作者已经判定的对方手段知识，memory 是后端封存的再遭遇记忆，交互反馈字段只协助绑定来源。你的职责是将每项经验绑定到本场成员来源；经验文本、知识等级、应对方向和旧知识 ID 原样保留，学习结论由经验作者负责。

每个 experience_index 在 records 或 deferred 中恰好出现一次。records 每项只包含 experience_index、reason、source_refs；只有来源身份无法确定时才使用 deferred，每项包含 experience_index、reason。reason 简述来源映射或无法映射的具体原因。actors 是观察主体的冻结身份，source_members 是正文成员名字到来源冒险者 ID 的冻结对应；结合经验内容和完整 final_events 定位来源。经验以整场经历为上下文，归档不要求逐字引文、段落下标或正文显式宣告学习。旧知识匹配已经封存在 experiences 中，归档只处理本次作用的成员来源。

source_refs 完整绑定共同成立这一独立作用的本次成员来源，每项包含 adventurer_id、source_kind、source_name、source_asset_id。source_kind 只取 equipment、skill、trait、state、event。从 source_catalog 精确复制同成员同类型的资产身份；目录不是已使用的证明。已有资产 ID 不能以 event/state 绕过；未提供 ID 或真实的非资产事件/状态才填 null。来源归档可认出主体不知道的正式资产身份，但这些后台资料不能扩写已封存经验。来源无法唯一绑定时仅延期该项，其余项正常归档。

来源绑定同时确认作用发起者：经验所述手段必须确实来自被引用的冒险者。event/state 同样绑定该成员实际发生的作用；学习主体自身施用的能力不能归到冒险者名下，无法形成合法成员来源的条目放入 deferred。

记录与每项来源的正常目标分别是 normal_targets.records、normal_targets.sources；合法独立记录或共同来源超过目标时完整保留，不截断。来源按同成员同类型资产 ID 去重，没有 ID 时按精确原名去重。只输出 JSON 对象，包含 records 数组与 deferred 数组。
"""


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
    ).encode('utf-8')).hexdigest()


def _event_text(event: object) -> str:
    return str(event.get('text') or '') if isinstance(event, dict) else str(event or '')


def _store_diagnostics(receipt: dict, field: str, records: list[dict]) -> None:
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    key = 'ADVENTURER_S1_AUXILIARY_DIAGNOSTIC_EVIDENCE_MAX_RECORDS'
    limit = max(0, int(current_app.config.get(key, AdventurerS1Plugin.default_config[key])))
    receipt[field] = records[:limit]
    receipt[f'{field}_count'] = len(records)
    receipt[f'{field}_omitted'] = max(0, len(records) - limit)


def build_learning_input(final_result: dict, frozen: dict) -> dict:
    """Project frozen evidence, never query live state or include proposed learning."""
    full = {**frozen, **final_result}
    for key in COUNTERMEASURE_LEARNING_FROZEN_FIELDS:
        if key in frozen:
            full[key] = frozen[key]
    revisions = objective_actor_revisions(full.get('objective_outline'))
    world = full.get('objective_world_intel_injection') or {}
    generation = full.get('generation_world_intel_injection') or {}
    actor_fields = (
        'id', 'canonical_name', 'identity_summary', 'capability_revision',
        'capability_profile', 'power_boundary', 'current_state', 'current_agenda',
    )
    actors = []
    for actor in world.get('world_actors') or world.get('world_actor_states') or []:
        if not isinstance(actor, dict):
            continue
        actor_id = _positive_int(actor.get('id'))
        if actor_id not in revisions or actor.get('capability_revision') != revisions[actor_id]:
            continue
        projected = {key: copy.deepcopy(actor[key]) for key in actor_fields if key in actor}
        projected['id'] = actor_id
        projected['capability_revision'] = revisions[actor_id]
        actors.append(projected)
    # The generation entry is the exact actor text actually supplied to the story.
    actor_by_id = {actor['id']: actor for actor in actors}
    for entry in generation.get('world_actor_prompt_entries') or []:
        if not isinstance(entry, dict):
            continue
        actor_id = _positive_int(entry.get('world_actor_id'))
        if actor_id is None or actor_id not in revisions or revisions[actor_id] != entry.get('capability_revision'):
            continue
        actor = actor_by_id.setdefault(actor_id, {
            'id': actor_id, 'capability_revision': revisions[actor_id],
        })
        actor['frozen_prompt_block'] = entry.get('prompt_block') or ''
    refs = frozen_countermeasure_knowledge_refs(full, personal_only=False)
    existing = [
        {
            'world_actor_id': actor_id, 'capability_revision': revision,
            'countermeasure_knowledge': copy.deepcopy(refs.get((actor_id, revision), [])),
        }
        for actor_id, revision in sorted(revisions.items())
    ]
    from plugins.adventurer_s1.services.s1_item_use_condition_service import (
        EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD,
    )

    catalog = []
    source_members = []
    member_names = full.get('_member_id_to_name') or {}
    for member in full.get('audit_member_snapshot') or []:
        if not isinstance(member, dict):
            continue
        member_id = _positive_int(member.get('id') or member.get('adventurer_id'))
        if member_id is None:
            continue
        name = member_names.get(str(member_id)) if isinstance(member_names, dict) else None
        name = name or member.get('adventurer_name') or member.get('name')
        if isinstance(name, str) and name.strip():
            source_members.append({'adventurer_id': member_id, 'name': name})
        for kind, fields in _RESOURCE_SNAPSHOT_FIELDS.items():
            for field in fields:
                for asset in member.get(field) or []:
                    if not isinstance(asset, dict) or not asset.get('name'):
                        continue
                    if (asset.get(EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD) is True
                            or asset.get('equipped') is False
                            or asset.get('is_archived') is True):
                        continue
                    ref = {
                        'adventurer_id': member_id, 'source_kind': kind,
                        'source_name': str(asset['name']),
                        'source_asset_id': _positive_int(asset.get('asset_id')),
                    }
                    description = asset.get('description') or asset.get('desc') or asset.get('custom_desc')
                    if isinstance(description, str) and description.strip():
                        ref['description'] = description
                    if ref not in catalog:
                        catalog.append(ref)
    for member in full.get('member_updates') or []:
        if not isinstance(member, dict):
            continue
        member_id = _positive_int(member.get('id'))
        if member_id is None:
            continue
        for kind, fields in _RESOURCE_UPDATE_FIELDS.items():
            for field in fields:
                for asset in member.get(field) or []:
                    name = asset.get('name') if isinstance(asset, dict) else asset
                    if isinstance(name, str) and name.strip():
                        ref = {'adventurer_id': member_id, 'source_kind': kind,
                               'source_name': name.strip(), 'source_asset_id': None}
                        if not any(all(row[key] == ref[key] for key in ('adventurer_id', 'source_kind', 'source_name')) for row in catalog):
                            catalog.append(ref)
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin
    memory_limit_key = 'ADVENTURER_S1_COUNTERMEASURE_LEARNING_MEMORY_MAX_CHARS'
    return {
        'actors': [actor_by_id[key] for key in sorted(actor_by_id)],
        'existing_knowledge': existing,
        'final_events': [
            {'event_index': index, 'text': _event_text(event)}
            for index, event in enumerate(full.get('events') or [])
        ],
        'source_catalog': catalog,
        'source_members': source_members,
        'context_missing_actor_ids': sorted(set(revisions) - set(actor_by_id)),
        'memory_max_chars': min(_contract_limits()[1], int(current_app.config.get(
            memory_limit_key, AdventurerS1Plugin.default_config[memory_limit_key],
        ))),
        'normal_targets': {
            name: int(current_app.config.get(key, AdventurerS1Plugin.default_config[key]))
            for name, key in (
                ('records', 'ADVENTURER_S1_COUNTERMEASURE_LEARNING_MAX_RECORDS'),
                ('sources', 'ADVENTURER_S1_COUNTERMEASURE_LEARNING_SOURCE_TARGET'),
                ('evidence', 'ADVENTURER_S1_COUNTERMEASURE_LEARNING_EVIDENCE_TARGET'),
            )
        },
    }


def _learning_ai_view(learning_input: dict) -> dict:
    """Hide historical revision echoes without changing the frozen checkpoint base."""
    payload = copy.deepcopy(learning_input)
    payload['normal_targets'].pop('evidence', None)
    for target in payload['existing_knowledge']:
        for knowledge in target['countermeasure_knowledge']:
            knowledge.pop('observer_capability_revision', None)
    return payload


def build_learning_archive_ai_view(learning_input: dict, experiences: list[dict]) -> dict:
    """Bind sources from current evidence; keep learning authority in the full input."""
    payload = _learning_ai_view(learning_input)
    payload['experiences'] = copy.deepcopy(experiences)
    payload['actors'] = []
    for actor in learning_input['actors']:
        identity = {key: copy.deepcopy(actor[key]) for key in (
            'id', 'capability_revision', 'canonical_name', 'identity_summary',
        ) if key in actor}
        # Some older frozen entries only have an ID and their rendered text.
        # Preserve that identity evidence instead of guessing an actor name.
        if not identity.get('canonical_name') and actor.get('frozen_prompt_block'):
            identity['frozen_prompt_block'] = actor['frozen_prompt_block']
        payload['actors'].append(identity)
    payload['source_catalog'] = [
        {key: source[key] for key in (
            'adventurer_id', 'source_kind', 'source_name', 'source_asset_id',
        )}
        for source in learning_input['source_catalog']
    ]
    payload.pop('memory_max_chars', None)
    payload.pop('existing_knowledge', None)
    return payload


def normalize_experiences(value: object, learning_input: dict) -> tuple[list[dict], list[dict]]:
    """Accept authored learning; bind its actor revision from the frozen context."""
    if not isinstance(value, dict) or not isinstance(value.get('experiences'), list):
        raise ValueError('experiences_required')
    targets = {(actor['id'], actor['capability_revision']) for actor in learning_input['actors']}

    def frozen_revision(actor_id):
        matches = {revision for identity, revision in targets if identity == actor_id}
        return next(iter(matches)) if len(matches) == 1 else None

    known = {
        (target['world_actor_id'], target['capability_revision'], ref['knowledge_id']): ref
        for target in learning_input['existing_knowledge']
        for ref in target['countermeasure_knowledge']
    }
    matched_counts = {}
    for raw in value['experiences']:
        if isinstance(raw, dict) and raw.get('matched_knowledge_id') is not None:
            actor_id = _positive_int(raw.get('observer_world_actor_id'))
            key = (actor_id, frozen_revision(actor_id), _positive_int(raw.get('matched_knowledge_id')))
            matched_counts[key] = matched_counts.get(key, 0) + 1
    accepted, rejected = [], []
    for index, raw in enumerate(value['experiences']):
        error = None
        if not isinstance(raw, dict):
            error = 'experience_not_object'
        else:
            actor_id = _positive_int(raw.get('observer_world_actor_id'))
            revision = frozen_revision(actor_id)
            text_values = {field: raw.get(field) for field in _EXPERIENCE_TEXT_FIELDS}
            invalid_text = next((field for field, text in text_values.items() if not isinstance(text, str)), None)
            texts = {field: text.strip() for field, text in text_values.items()} if invalid_text is None else {}
            authored_text = '\n'.join(text for text in texts.values() if text)
            memory = '\n'.join(texts.get(field, '') for field in (
                'learned_boundary', 'countermeasure_direction',
            ) if texts.get(field))
            matched_id = _positive_int(raw.get('matched_knowledge_id'))
            match_key = (actor_id, revision, matched_id)
            if revision is None:
                error = 'experience_identity_revision_mismatch'
            elif 'matched_knowledge_id' not in raw or (
                raw.get('matched_knowledge_id') is not None and match_key not in known
            ):
                error = 'experience_knowledge_not_injected'
            elif matched_id is not None and matched_counts.get(match_key, 0) != 1:
                error = 'experience_duplicate_matched_update'
            elif invalid_text:
                error = f'experience_{invalid_text}_invalid'
            elif not texts['encountered_method'] or not texts['observed_effect']:
                error = 'experience_current_observation_required'
            elif not texts['learned_boundary']:
                error = 'experience_opponent_knowledge_required'
            elif not memory or len(authored_text) > learning_input['memory_max_chars']:
                error = 'experience_memory_invalid'
            else:
                level = raw.get('knowledge_level')
                direction = texts['countermeasure_direction']
                if not isinstance(level, str) or level not in COUNTERMEASURE_KNOWLEDGE_LEVELS:
                    error = 'experience_level_invalid'
                elif (level == 'observed' and direction) or (level == 'prepared' and not direction):
                    error = 'experience_level_direction_mismatch'
                else:
                    accepted.append({
                        'experience_index': index,
                        'observer_world_actor_id': actor_id,
                        'observer_capability_revision': revision,
                        'matched_knowledge_id': matched_id,
                        'memory': memory.strip(),
                        'memory_format': _EXPERIENCE_FORMAT,
                        **texts, 'knowledge_level': level,
                    })
        if error:
            rejected.append({'experience_index': index, 'reason': error,
                             'raw_record': _safe_json_evidence(raw)})
    return accepted, rejected


def seal_learning_archive(value: object, experiences: list[dict], learning_input: dict, *, source_repairs_out: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Bind sources without granting the archive semantic authorship."""
    if not isinstance(value, dict) or not isinstance(value.get('records'), list) or not isinstance(value.get('deferred'), list):
        raise ValueError('learning_archive_arrays_required')
    by_index = {row['experience_index']: row for row in experiences}
    knowledge = {
        (target['world_actor_id'], target['capability_revision'], ref['knowledge_id']): ref
        for target in learning_input['existing_knowledge']
        for ref in target['countermeasure_knowledge']
    }
    occurrences: dict[int, list[tuple[str, dict]]] = {}
    records, rejected = [], []
    for field in ('records', 'deferred'):
        for raw in value[field]:
            if not isinstance(raw, dict) or type(raw.get('experience_index')) is not int or raw['experience_index'] not in by_index:
                rejected.append({'reason': 'learning_archive_unknown_experience',
                                 'raw_record': _safe_json_evidence(raw)})
                continue
            occurrences.setdefault(raw['experience_index'], []).append((field, raw))
    for index, experience in by_index.items():
        entries = occurrences.get(index, [])
        source_repairs = []
        error = None
        if len(entries) != 1:
            error = 'learning_archive_missing_or_duplicate_experience'
        elif entries[0][0] == 'deferred':
            reason = entries[0][1].get('reason')
            error = reason.strip() if isinstance(reason, str) and reason.strip() else 'learning_archive_deferred'
        else:
            raw = entries[0][1]
            reason = raw.get('reason')
            matched_id = experience['matched_knowledge_id']
            matched = knowledge.get((
                experience['observer_world_actor_id'], experience['observer_capability_revision'],
                _positive_int(matched_id),
            )) if matched_id is not None else None
            sources, source_error = _normalize_source_refs(raw.get('source_refs'))
            level = experience.get('knowledge_level')
            memory = experience['memory']
            direction = experience.get('countermeasure_direction')
            if not isinstance(reason, str) or not reason.strip():
                error = 'learning_archive_acceptance_reason_required'
            elif matched_id is not None and matched is None:
                error = 'learning_archive_knowledge_not_injected'
            elif source_error:
                error = source_error
            elif not isinstance(level, str) or level not in COUNTERMEASURE_KNOWLEDGE_LEVELS:
                error = 'learning_archive_level_invalid'
            elif not isinstance(direction, str) or (direction and direction not in memory):
                error = 'learning_archive_direction_not_in_memory'
            elif (level == 'observed' and direction) or (level == 'prepared' and not direction):
                error = 'learning_archive_level_direction_mismatch'
            else:
                # A supplied frozen asset ID is recovered only from one exact
                # owner/type/name tuple. Explicit wrong IDs never fall back.
                for source_index, source in enumerate(sources):
                    if source['source_kind'] in {'state', 'event'}:
                        continue
                    matches = [row for row in learning_input['source_catalog'] if (
                        row['adventurer_id'] == source['adventurer_id']
                        and row['source_kind'] == source['source_kind']
                        and (
                            row['source_asset_id'] == source.get('source_asset_id')
                            if source.get('source_asset_id') is not None
                            else row['source_name'] == source['source_name']
                        )
                    )]
                    if not matches and source.get('source_asset_id') is None:
                        matches = [row for row in learning_input['source_catalog'] if (
                            row['adventurer_id'] == source['adventurer_id']
                            and row['source_name'] == source['source_name']
                        )]
                    if len(matches) != 1:
                        error = 'learning_archive_source_not_unique'
                        break
                    resolved = {key: matches[0][key] for key in (
                        'adventurer_id', 'source_kind', 'source_name', 'source_asset_id',
                    )}
                    if source != resolved:
                        source_repairs.append({
                            'source_ref_index': source_index,
                            'code': 'source_identity_frozen_catalog_canonicalized',
                            'from': dict(source), 'to': resolved,
                        })
                    source.update(resolved)
                texts = {}
                for field in _EXPERIENCE_FACT_FIELDS:
                    text_value = matched.get(field) if matched is not None else experience.get(field)
                    if (not isinstance(text_value, str)
                            or (field != 'unresolved_reason' and not text_value.strip())
                            or (matched is None
                                and experience.get('memory_format') != _EXPERIENCE_FORMAT
                                and text_value not in memory)):
                        error = f'learning_archive_{field}_not_in_memory'
                        break
                    texts[field] = text_value
                if not error:
                    records.append({
                        'observer_world_actor_id': experience['observer_world_actor_id'],
                        'observer_capability_revision': experience['observer_capability_revision'],
                        'matched_knowledge_id': _positive_int(matched_id),
                        'source_refs': sources, **texts,
                        'learned_boundary': memory,
                        'knowledge_level': level,
                        'countermeasure_direction': direction,
                        # Old sealed checkpoints retain their already validated references.
                        'evidence_event_indexes': [row['event_index'] for row in experience.get('evidence', [])],
                        **({'evidence_event_quotes': [row['quote'] for row in experience['evidence']]}
                           if experience.get('evidence') else {}),
                    })
                    if source_repairs and source_repairs_out is not None:
                        source_repairs_out.append({
                            'experience_index': index,
                            'source_ref_repairs': _safe_json_evidence(source_repairs),
                        })
        if error:
            bounded_reason = _safe_json_evidence(str(error))
            rejection = {'experience_index': index, 'reason': (
                bounded_reason if isinstance(bounded_reason, str) else 'learning_archive_deferred'
            )}
            if not isinstance(bounded_reason, str):
                rejection['reason_evidence'] = bounded_reason
            if entries:
                rejection['raw_record'] = _safe_json_evidence([raw for _field, raw in entries])
            rejected.append(rejection)
    return records, rejected


def _call_phase(phase: str, payload: dict, validator: Callable, *, profile, party_id, expedition_log_id, physical_attempt_guard, player_names):
    """Only provider failures defer. Ownership, database and programming faults propagate."""
    from .s1_countermeasure_review_design_service import REVIEW_SYSTEM_PROMPT, DESIGN_SYSTEM_PROMPT
    from .s1_countermeasure_growth_service import GROWTH_REVIEW_PROMPT
    system_prompt = {'review': REVIEW_SYSTEM_PROMPT, 'experience': DESIGN_SYSTEM_PROMPT,
                     'archive': ARCHIVE_SYSTEM_PROMPT, 'growth_review': GROWTH_REVIEW_PROMPT}[phase]
    metadata: dict = {}
    def response_validator(decoded):
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'invalid_json', failure_stage='response_parse')
        try:
            validator(decoded.json_result.data)
        except ValueError as exc:
            return S1AiResponseContractResult.reject(str(exc), failure_stage='contract_validation')
        return S1AiResponseContractResult.accept()
    db_manager.remove()
    try:
        response = post_s1_ai_request(
            f'{profile.api_base}/chat/completions', feature=FEATURE,
            capability=f'expedition_countermeasure_{phase}_ai',
            player_names=player_names, resolved_profile=profile,
            json_object_mode_if_supported=True, request_metadata_out=metadata,
            headers={'Authorization': f'Bearer {profile.api_key}', 'Content-Type': 'application/json'},
            json={
                'model': profile.model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))},
                ],
                'temperature': 0.0,
            },
            response_validator=response_validator,
            physical_attempt_guard=physical_attempt_guard,
        )
        # The gateway can return a terminal HTTP error response after its own
        # attempts. Decoding checks status and must share the provider boundary.
        decoded = parse_s1_ai_http_json_response(response)
    except requests.RequestException as exc:
        record_token_usage(
            call_type=f'expedition_countermeasure_{phase}_ai', usage=None,
            model=metadata.get('model') or profile.model, party_id=party_id,
            expedition_log_id=expedition_log_id, is_success=False,
            failure_stage='http_or_response', validation_error_code=type(exc).__name__,
            record_without_usage=True, request_metadata=metadata, request_exception=exc,
        )
        return None, f'provider.{type(exc).__name__}', metadata
    metadata.update(get_s1_ai_response_metadata(response))
    metadata['response_evidence'] = _safe_json_evidence(decoded.json_result.data)
    error = decoded.json_result.error_code if not decoded.json_result.ok else None
    failure_stage = 'response_parse' if error else 'contract_validation'
    normalized = None
    if not error:
        try:
            normalized = validator(decoded.json_result.data)
        except ValueError as exc:
            error = str(exc)
    record_token_usage(
        call_type=f'expedition_countermeasure_{phase}_ai', usage=decoded.http_result.usage,
        model=metadata.get('model') or profile.model, party_id=party_id,
        expedition_log_id=expedition_log_id, is_success=error is None,
        failure_stage=failure_stage if error else None,
        validation_error_code=error, request_metadata=metadata,
    )
    return normalized, error, metadata


def finalize_countermeasure_learning(final_result: dict, *, frozen: dict, party_id=None, expedition_log_id=None, physical_attempt_guard=None, checkpoint=None, player_names=()) -> dict:
    """The sole new-generation inlet, after story gate, before ordinary settlement.

    Checkpoints reuse the existing approved-candidate claim and transaction. A
    process restart reuses a sealed experience and never asks the archive to
    recreate it. Failed auxiliary work is retained as an explicit disposition.
    """
    from . import s1_countermeasure_review_design_service as D
    result = copy.deepcopy(final_result)
    for key in COUNTERMEASURE_LEARNING_FROZEN_FIELDS:
        result.pop(key, None)
    learning_input = build_learning_input(result, frozen)
    new_subjects = [row['subject'] for row in (frozen.get('objective_outline') or {}).get(
        'encounter_capability_profiles', []) if isinstance(row, dict)
        and row.get('scope') == 'individual' and not row.get('world_actor_id')
        and isinstance(row.get('subject'), str) and row['subject'].strip()]

    def track_admissions(receipt):
        # Existing-actor learning and first admission can coexist in one scene.
        # An unchanged terminal result may also predate the context handoff fix.
        if new_subjects and receipt.get('admission_status') != 'completed':
            receipt['pending_admission_subjects'] = list(new_subjects)
            receipt['admission_status'] = 'pending'
            if receipt.get('status') == 'not_applicable':
                receipt['status'] = 'awaiting_admission'
    fingerprint = _digest(learning_input)
    prior = result.get(RECEIPT_FIELD)
    checkpoint_error = None
    terminal_statuses = {'completed', 'deferred', 'disabled', 'not_applicable', 'awaiting_admission'}
    if isinstance(prior, dict):
        receipt = copy.deepcopy(prior)
        same_schema = receipt.get('schema_version') == SCHEMA_VERSION
        same_input = same_schema and receipt.get('input_fingerprint') == fingerprint
        legacy_input = same_schema and receipt.get('input_fingerprint') == _digest({
            key: value for key, value in learning_input.items() if key != 'source_members'
        })
        terminal = receipt.get('status') in terminal_statuses
        from .s1_countermeasure_growth_service import valid_receipt
        growth = receipt.get('capability_growth')
        growth_valid = not isinstance(growth, dict) or growth.get('status') != 'reviewed' or valid_receipt(growth)
        if (terminal and (same_input or legacy_input)
                and growth_valid
                and receipt.get('output_fingerprint') == _digest(result.get('countermeasure_learning'))):
            previous = copy.deepcopy(receipt)
            track_admissions(receipt)
            result[RECEIPT_FIELD] = receipt
            if receipt != previous and checkpoint is not None:
                checkpoint(copy.deepcopy(result))
            return result
        sealed = receipt.get('status') in {'reviewed', 'experienced'} or 'experiences' in receipt or 'reviewed' in receipt
        if sealed or terminal:
            if not growth_valid:
                checkpoint_error = 'learning_growth_receipt_changed'
            elif not same_schema:
                checkpoint_error = 'learning_checkpoint_schema_mismatch'
            elif legacy_input and not same_input:
                checkpoint_error = 'learning_experience_semantics_missing' if sealed else 'learning_checkpoint_output_mismatch'
            elif not same_input:
                checkpoint_error = 'learning_input_changed_after_experience'
            elif terminal:
                checkpoint_error = 'learning_checkpoint_output_mismatch'
            elif receipt.get('status') == 'reviewed' and 'reviewed' not in receipt:
                checkpoint_error = 'learning_sealed_review_invalid'
            elif receipt.get('status') == 'experienced' and 'experiences' not in receipt:
                checkpoint_error = 'learning_experience_semantics_missing'
            elif 'reviewed' in receipt and (
                    receipt.get('review_format') != D.REVIEW_FORMAT
                    or not isinstance(receipt['reviewed'], list)
                    or receipt.get('review_fingerprint') != _digest(receipt['reviewed'])):
                checkpoint_error = 'learning_sealed_review_invalid'
            elif 'experiences' in receipt and (not isinstance(receipt.get('experiences'), list)
                  or any(not isinstance(row, dict) or any(field not in row for field in _EXPERIENCE_SEMANTIC_FIELDS)
                         for row in receipt['experiences'])):
                checkpoint_error = 'learning_experience_semantics_missing'
            elif 'experiences' in receipt and receipt.get('experience_format') not in _RESUMABLE_EXPERIENCE_FORMATS:
                checkpoint_error = 'learning_experience_format_changed'
        else:
            receipt = {'schema_version': SCHEMA_VERSION, 'input_fingerprint': fingerprint, 'status': 'pending'}
    else:
        receipt = {'schema_version': SCHEMA_VERSION, 'input_fingerprint': fingerprint, 'status': 'pending'}
    # Main/Observer output never supplies new learning authority.
    if checkpoint_error and result.get('countermeasure_learning'):
        receipt['incompatible_output_evidence'] = _safe_json_evidence(result['countermeasure_learning'])
        receipt.setdefault('prior_output_fingerprint', receipt.get('output_fingerprint'))
    result['countermeasure_learning'] = []
    result[RECEIPT_FIELD] = receipt

    def save():
        track_admissions(receipt)
        receipt['output_fingerprint'] = _digest(result.get('countermeasure_learning'))
        if checkpoint is not None:
            checkpoint(copy.deepcopy(result))

    def defer(error):
        receipt.update(status='deferred', error_code=error)
        result[COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD] = {
            'status': 'deferred_unconsumed', 'reason_code': error,
            'total_records': len(receipt.get('experiences') or []),
            'accepted_records': 0,
        }
        save()
        return result

    if checkpoint_error:
        return defer(checkpoint_error)
    if not current_app.config.get('ADVENTURER_S1_COUNTERMEASURE_LEARNING_ENABLED', True):
        receipt['status'] = 'disabled'
        save()
        return result
    receipt['context_missing_actor_ids'] = learning_input['context_missing_actor_ids']
    if learning_input['context_missing_actor_ids'] and not learning_input['actors']:
        return defer('learning_actor_context_missing')
    if not learning_input['actors']:
        receipt['status'] = 'awaiting_admission' if new_subjects else 'not_applicable'
        save()
        return result
    try:
        profile = resolve_s1_ai_profile_snapshot(FEATURE)
    except AIProfileAssignmentError as exc:
        return defer(f'learning_route.{type(exc).__name__}')
    profile_value = dataclasses.asdict(profile) if dataclasses.is_dataclass(profile) else vars(profile)
    profile_fingerprint = _digest(profile_value)
    if receipt.get('profile_fingerprint') not in {None, profile_fingerprint}:
        return defer('learning_route_changed_after_experience')
    if ('experiences' in receipt or 'reviewed' in receipt) and not receipt.get('profile_fingerprint'):
        return defer('learning_frozen_route_missing')
    receipt['profile_fingerprint'] = profile_fingerprint
    call_args = dict(profile=profile, party_id=party_id, expedition_log_id=expedition_log_id, physical_attempt_guard=physical_attempt_guard, player_names=player_names)
    if 'experiences' not in receipt:
        if 'reviewed' not in receipt:
            normalized, error, metadata = _call_phase('review', D.build_review_view(learning_input),
                lambda value: D.normalize_review(value, learning_input), **call_args)
            receipt['review_metadata'] = metadata
            if error:
                return defer(error)
            reviewed, rejected = normalized
            receipt.update(status='reviewed', reviewed=reviewed, review_format=D.REVIEW_FORMAT,
                           review_fingerprint=_digest(reviewed))
            _store_diagnostics(receipt, 'rejected_review', rejected)
            save()
        reviewed = receipt['reviewed']
        experiences, rejected = [], []
        if reviewed:
            normalized, error, metadata = _call_phase('experience', D.build_design_view(learning_input, reviewed),
                lambda value: D.normalize_design(value, reviewed, learning_input, sparse_updates=True), **call_args)
            receipt['experience_metadata'] = metadata
            if error:
                return defer(error)
            experiences, rejected = normalized
        receipt.update(status='experienced', experiences=experiences, experience_format=_EXPERIENCE_FORMAT)
        _store_diagnostics(receipt, 'rejected_experiences', rejected)
        save()
    experiences = receipt['experiences']
    if 'capability_growth' not in receipt and any('_capability_growth' in row for row in experiences):
        from .s1_countermeasure_growth_service import review_growth
        receipt['capability_growth'] = review_growth(
            learning_input, receipt.get('reviewed', []), experiences, call_args)
        save()
    rows, rejected_archive, source_repairs = [], [], []
    if 'reviewed' in receipt:
        source_repairs = [repair for row in receipt['reviewed'] for repair in row.get('source_identity_repairs', [])]
    if experiences and 'reviewed' in receipt:
        try:
            rows, rejected_archive = D.seal_review_sources(receipt['reviewed'], experiences, learning_input)
        except ValueError as exc:
            return defer(str(exc))
    elif experiences:
        payload = build_learning_archive_ai_view(learning_input, experiences)
        def validate_archive(value):
            repairs = []
            rows, rejected = seal_learning_archive(
                value, experiences, learning_input, source_repairs_out=repairs,
            )
            return rows, rejected, repairs

        normalized, error, metadata = _call_phase('archive', payload, validate_archive, **call_args)
        receipt['archive_metadata'] = metadata
        if error:
            return defer(error)
        rows, rejected_archive, source_repairs = normalized
    # This existing validator has a bounded administrator-only raw-evidence outlet.
    result['countermeasure_learning'] = rows
    prepared, disposition = prepare_countermeasure_learning_for_settlement(
        final_result_data=result, cached_full_log=frozen,
    )
    result['countermeasure_learning'] = prepared
    receipt.update(status='completed', accepted_records=len(prepared))
    _store_diagnostics(receipt, 'rejected_archive', rejected_archive)
    _store_diagnostics(receipt, 'source_identity_repairs', source_repairs)
    if disposition is not None:
        result[COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD] = disposition
    elif rejected_archive or receipt.get('rejected_experiences_count') or receipt.get('rejected_review_count') or learning_input['context_missing_actor_ids']:
        result[COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD] = {
            'status': 'partially_quarantined' if prepared else 'quarantined',
            'reason_code': 'learning_owner_records_quarantined',
            'accepted_records': len(prepared),
            'quarantined_records': len(rejected_archive) + receipt.get('rejected_experiences_count', 0) + receipt.get('rejected_review_count', 0),
            'context_missing_actor_ids': learning_input['context_missing_actor_ids'],
        }
    else:
        result.pop(COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD, None)
    save()
    return result
