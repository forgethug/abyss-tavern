"""One bounded auxiliary entity proposal on the existing world-maintenance lease."""
from __future__ import annotations

from copy import deepcopy
import json
import logging

import requests
from flask import current_app
from db_repository import db_manager
from plugins.adventurer_s1.services.s1_ai_input_budget import S1AiInputBudgetError
from plugins.adventurer_s1.services.s1_encounter_preparation_contract import (
    ELIGIBLE_KINDS, REVIEW_RULE, build_preparation_rule, digest,
    normalize_proposal, normalize_review,
)

logger = logging.getLogger(__name__)


class EncounterPreparationCallError(RuntimeError):
    """Carry the failed role and stable code through the auxiliary boundary."""

    def __init__(self, capability, failure_stage, error_code):
        super().__init__(error_code)
        self.capability = capability
        self.failure_stage = failure_stage
        self.error_code = error_code


def eligible(full_log: object) -> bool:
    from plugins.adventurer_s1.services.adventurer_s1_config_service import resolve_s1_special_event_kind
    if not isinstance(full_log, dict) or full_log.get('campaign_dispatch'):
        return False
    return resolve_s1_special_event_kind(full_log) in ELIGIBLE_KINDS


def source_projection(full_log: dict) -> dict:
    """Reuse the source narrative's frozen ability view, never current live assets."""
    from plugins.adventurer_s1.services.s1_capability_profile_contract import project_encounter_definition_for_ai
    party, ability_status = _source_party_abilities(full_log)
    outline = full_log.get('objective_outline')
    outline = outline if isinstance(outline, dict) else {}
    return {
        'events': deepcopy(full_log.get('events') or []),
        'encounter_profiles': deepcopy(outline.get('encounter_capability_profiles') or []),
        'encounter_definition': project_encounter_definition_for_ai(outline),
        'settlement_receipt_status': (full_log.get('settlement_receipt') or {}).get('status'),
        'source_scope': {key: deepcopy(full_log.get(key)) for key in (
            '_expedition_locus_snapshot', 'location_state_application',
        )},
        'party': party,
        'party_ability_status': ability_status,
        'outcomes': [
            {key: member[key] for key in ('id', 'remaining_hp', 'is_dead') if key in member}
            for member in full_log.get('member_updates') or []
            if isinstance(member, dict)
        ],
        'special_event_kind': full_log.get('special_event_kind'),
        'special_event_type': full_log.get('special_event_type'),
        'special_event_risk_tier': full_log.get('special_event_risk_tier'),
        'has_special_event': full_log.get('has_special_event'),
        'is_extreme_crisis': full_log.get('is_extreme_crisis'),
    }


def _source_party_abilities(full_log: dict) -> tuple[list[dict], str]:
    """Only exact members/assets selected for the original main request participate."""
    import hashlib
    from plugins.adventurer_s1.services.s1_description_ai_service import (
        get_expedition_description_projection, index_frozen_description_projection,
        resolve_frozen_description_projection, description_source_hash,
    )
    receipt = get_expedition_description_projection(full_log)
    frozen = index_frozen_description_projection(receipt)
    party = []
    found = set()
    valid = bool(isinstance(receipt, dict) and receipt.get('schema_version') == 2
                 and isinstance(receipt.get('assets'), list))
    raw_members = full_log.get('audit_member_snapshot') or []
    if not isinstance(raw_members, list):
        return [], 'unavailable'
    for member in raw_members:
        if not isinstance(member, dict):
            valid = False
            continue
        row = {key: deepcopy(member[key]) for key in (
            'adventurer_id', 'adventurer_name', 'character_class', 'level', 'hp',
            'max_hp', 'strength', 'agility', 'intelligence', 'luck',
        ) if key in member}
        for field, default_kind in (('skills', 'skill'), ('equipment', 'item'), ('traits', 'trait')):
            row[field] = []
            assets = member.get(field) or []
            if not isinstance(assets, list):
                valid = False
                continue
            for asset in assets:
                if not isinstance(asset, dict):
                    continue
                kind = ('companion' if default_kind == 'item'
                        and asset.get('type') == 'companion' else default_kind)
                key = (member.get('adventurer_id'), kind, asset.get('asset_id'))
                if type(key[0]) is not int or type(key[2]) is not int:
                    valid = False
                    continue
                if key not in frozen:
                    continue
                entry = frozen[key]
                full = str((asset.get('custom_desc') if default_kind == 'item' else '')
                           or asset.get('description') or asset.get('desc') or '').strip()
                selected = resolve_frozen_description_projection(
                    full, kind=kind, member_id=key[0], asset_id=key[2], frozen_by_key=frozen)
                if (key in found or not isinstance(entry, dict)
                        or entry.get('source_hash') != description_source_hash(full, kind=kind)
                        or entry.get('input_hash') != hashlib.sha256(selected.encode('utf-8')).hexdigest()
                        or entry.get('input_chars') != len(selected)):
                    valid = False
                    continue
                found.add(key)
                projected = {name: deepcopy(asset[name]) for name in (
                    'asset_id', 'name', 'type', 'quantity', 'uses_left', 'recovering',
                    'recovery_minutes', 'asset_dependencies',
                ) if name in asset}
                projected['description'] = selected
                if kind == 'companion':
                    lineage = entry.get('companion_lineage') if 'companion_lineage' in entry else _frozen_companion_abilities(full_log, key[0], key[2])
                    if not isinstance(lineage, list) or any(not isinstance(text, str) for text in lineage):
                        lineage = None
                    if lineage is None:
                        valid = False
                    elif lineage:
                        projected['companion_lineage'] = lineage
                row[field].append(projected)
        party.append(row)
    valid = valid and found == set(frozen) and len(frozen) == len((receipt or {}).get('assets') or [])
    if not valid:
        # Historical/malformed views remain usable for observed archival, but
        # cannot silently certify a new opponent against a partial player kit.
        for member in party:
            for field in ('skills', 'equipment', 'traits'):
                member.pop(field, None)
    return party, 'complete' if valid else 'unavailable'


def _frozen_companion_abilities(full_log: dict, member_id: int, asset_id: int) -> list[str] | None:
    """Reuse the exact departure lineage, including its frozen summary versions."""
    from types import SimpleNamespace
    from plugins.adventurer_s1.services.adventurer_s1_prompt_service import _project_s1_equipment_for_ai_prompt
    checkpoint = full_log.get('_generation_checkpoint')
    frozen = checkpoint.get('member_generation_input') if isinstance(checkpoint, dict) else None
    if isinstance(frozen, dict) and frozen.get('version') == 1:
        members = frozen.get('members')
        if not isinstance(members, list):
            return None
        rows = [row for row in members if isinstance(row, dict) and row.get('id') == member_id]
        if len(rows) != 1 or not isinstance(rows[0].get('equipment_items'), list):
            return None
        assets = [asset for asset in rows[0]['equipment_items']
                  if isinstance(asset, dict) and asset.get('id') == asset_id]
        if len(assets) != 1 or not isinstance(assets[0].get('companion_lineage'), list):
            return None
        lineage = assets[0]['companion_lineage']
        if any(not isinstance(row, dict) or not isinstance(row.get('evolution_chain', []), list)
               for row in lineage):
            return None
        # Only the shared renderer's player-visible mechanism strings are sent.
        item = SimpleNamespace(id=asset_id, name='伙伴', type='companion', quantity=1,
                               description='', companion_lineage=lineage)
        return _project_s1_equipment_for_ai_prompt([item], {}, use_condition_by_name={})[0].get('companion_lineage', [])
    # An explicitly frozen empty list proves absence. Missing legacy evidence
    # does not prove that a companion had no inherited mechanism.
    if full_log.get('_companion_lineage_context') == []:
        return []
    return None


def call(snapshot: dict, proposal: dict | None = None) -> dict:
    from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
        resolve_ai_profile, get_s1_ai_response_metadata,
    )
    from plugins.adventurer_s1.services.s1_ai_player_name_service import post_s1_ai_request_with_player_aliases
    from plugins.adventurer_s1.services.s1_ai_json_response_service import (
        parse_s1_ai_http_json_response, S1AiResponseContractResult,
    )
    from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage

    capability = 'world_encounter_preparation_review' if proposal is not None else 'world_encounter_preparation'
    route = resolve_ai_profile('world_outline')
    payload = {key: snapshot[key] for key in (
        'target_dungeon', 'allow_observed', 'allow_prepare', 'special_event_kind',
        'core_level_reference', 'contract_limits', 'location_snapshot', 'world_context',
        'maintenance_plan', 'world_actor_name_registry', 'party', 'outcomes',
        'encounter_profiles', 'encounter_definition', 'events',
    )}
    payload['observed_location_scope'] = snapshot.get('observed_location_scope', 'legacy')
    payload['party_ability_status'] = snapshot.get('party_ability_status', 'unavailable')
    # Explicit references remove the need for either role to count paragraphs.
    # The frozen source/hash retain the complete original array for writeback.
    payload['events'] = [
        {'event_index': index, 'text': event}
        for index, event in enumerate(snapshot['events'])
    ]
    payload['encounter_profiles'] = [
        {**deepcopy(row), 'profile_index': index}
        for index, row in enumerate(snapshot['encounter_profiles'])
    ]
    # A prepared-only author calibrates from actual events, not old character drafts.
    # Keep the full frozen profiles for observed recovery and independent review.
    if proposal is None and snapshot['allow_observed'] is False:
        payload['encounter_profiles'] = []
        payload['encounter_definition'] = None
    if proposal is not None:
        payload['proposal'] = proposal
    normalize = (lambda raw: normalize_review(raw, snapshot=snapshot, proposal=proposal)) if proposal is not None else (
        lambda raw: normalize_proposal(raw, snapshot))

    def validate(decoded):
        if decoded.http_result.finish_reason == 'length':
            return S1AiResponseContractResult.reject('response_truncated', failure_stage='response_parse')
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(
                decoded.json_result.error_code or 'invalid_json', failure_stage='response_parse')
        try:
            return S1AiResponseContractResult.accept(normalize(decoded.json_result.data))
        except ValueError as exc:
            return S1AiResponseContractResult.reject(str(exc))

    metadata, usage, result, error = {}, None, None, None
    request_exception = None
    failure_stage = 'http_or_response'
    db_manager.remove()
    try:
        response = post_s1_ai_request_with_player_aliases(
            str(route['api_base']).rstrip('/') + '/chat/completions',
            feature='world_outline', capability=capability,
            player_names=tuple(snapshot.get('player_names', [])),
            headers={'Authorization': 'Bearer ' + route['api_key'], 'Content-Type': 'application/json'},
            json={'model': route['model'], 'messages': [
                {'role': 'system', 'content': REVIEW_RULE if proposal is not None else build_preparation_rule(
                    allow_observed=snapshot['allow_observed'],
                )},
                {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]},
            json_object_mode_if_supported=True, response_validator=validate,
            request_metadata_out=metadata,
        )
        decoded = parse_s1_ai_http_json_response(response)
        metadata.update(get_s1_ai_response_metadata(response))
        usage = decoded.http_result.usage
        failure_stage = 'response_parse'
        if not decoded.json_result.ok:
            error = decoded.json_result.error_code or 'response_parse_invalid'
            raise ValueError(error)
        failure_stage = 'contract_validation'
        result = normalize(decoded.json_result.data)
        return result
    except (requests.RequestException, ValueError) as exc:
        request_exception = exc
        if isinstance(exc, S1AiInputBudgetError):
            failure_stage, error = 'input_budget', exc.code
        elif isinstance(exc, requests.RequestException):
            failure_stage, error = 'http_or_response', 'provider.' + type(exc).__name__
        else:
            error = error or str(exc)
        raise EncounterPreparationCallError(capability, failure_stage, error) from exc
    except Exception as exc:
        failure_stage, error = 'consumer', type(exc).__name__
        request_exception = exc
        raise
    finally:
        record_token_usage(
            call_type=capability + '_ai', usage=usage,
            model=metadata.get('model') or route['model'], attempt_number=1,
            expedition_log_id=snapshot['source_log_id'],
            correlation_id='encounter:' + snapshot['source_hash'][:48],
            is_success=result is not None,
            failure_stage=None if result is not None else failure_stage,
            validation_error_code=error, record_without_usage=True,
            request_metadata=metadata, request_exception=request_exception,
        )
        db_manager.remove()


def prepare(dungeon: str, candidate_ids: list[int], *, maintenance_plan: dict | None = None) -> dict:
    """Failures affect only this auxiliary proposal, not maintenance or expedition."""
    if (not current_app.config.get('ADVENTURER_S1_ENCOUNTER_PREPARATION_ENABLED', True)
            or not current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_ENABLED', True)
            or not current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_AUTO_MAINTENANCE_ENABLED', True)):
        return {'status': 'disabled'}
    from db_repository.s1_encounter_preparation_repository import build_snapshot
    from plugins.adventurer_s1.services.s1_first_encounter_learning_service import FirstEncounterLearningError
    snapshot = None
    phase = 'source_snapshot'
    try:
        skip_reason = {}
        snapshot = build_snapshot(dungeon, candidate_ids, maintenance_plan=maintenance_plan,
                                  skip_reason_out=skip_reason)
        db_manager.remove()
        if snapshot is None:
            status = ('deferred' if skip_reason.get('reason_code') in {
                'world_story_capacity', 'no_available_entity_path', 'input_budget_exceeded',
            } else 'not_applicable')
            return {'status': status, **skip_reason}
        phase = 'generation'
        proposal = call(snapshot)
        if proposal['entity'] is None:
            return {'status': 'no_candidate', 'source_log_id': snapshot['source_log_id'],
                    'source_hash': snapshot['source_hash']}
        phase = 'review'
        review = call(snapshot, proposal)
        result = {
            'status': 'approved' if review['approved'] else 'rejected',
            'source_log_id': snapshot['source_log_id'], 'source_hash': snapshot['source_hash'],
            'proposal': proposal, 'proposal_hash': digest(proposal), 'review': review,
        }
        if review['approved']:
            result['_snapshot'] = snapshot
            if proposal['entity']['origin'] == 'observed':
                from plugins.adventurer_s1.models import AdventurerS1ExpeditionLog
                from plugins.adventurer_s1.services.s1_first_encounter_learning_service import extract
                source = AdventurerS1ExpeditionLog.query.filter_by(id=snapshot['source_log_id']).first()
                full = deepcopy(source.full_log)
                db_manager.remove()
                try:
                    result['_first_encounter_learning'] = extract(
                        full, proposal['entity'], source_log_id=snapshot['source_log_id'],
                        source_candidate_id=snapshot['source_candidate_id'])
                except FirstEncounterLearningError:
                    raise
                except Exception as exc:
                    raise FirstEncounterLearningError('first_encounter_learning.not_completed') from exc
        return result
    except FirstEncounterLearningError:
        raise
    except EncounterPreparationCallError as exc:
        return {'status': 'failed', 'source_log_id': snapshot['source_log_id'] if snapshot else None,
                'phase': phase, 'capability': exc.capability,
                'failure_stage': exc.failure_stage, 'reason_code': exc.error_code}
    except Exception as exc:
        logger.exception('S1 后续实体准备失败开放 dungeon=%s', dungeon)
        return {'status': 'failed', 'source_log_id': snapshot['source_log_id'] if snapshot else None,
                'phase': phase, 'failure_stage': 'consumer',
                'reason_code': 'encounter_preparation.' + type(exc).__name__}
    finally:
        db_manager.remove()
