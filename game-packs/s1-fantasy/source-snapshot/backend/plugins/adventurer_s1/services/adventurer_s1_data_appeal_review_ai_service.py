"""MIMO second-pass review for DeepSeek data-appeal patches."""
import copy
import json
from plugins.adventurer_s1.services.s1_appeal_hidden_drop_guard import HIDDEN_DROP_APPEAL_RULE
from plugins.adventurer_s1.services.s1_description_repair_contract import S1_MISSING_DESCRIPTION_REPAIR_RULES
import logging
import re
import requests
from flask import current_app
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import AIProfileAssignmentError, enrich_s1_ai_response_metadata, log_s1_ai_response_outcome
from db_repository import db_manager
from plugins.adventurer_s1.services.s1_mimo_client_config import get_mimo_compatible_client_config
from plugins.adventurer_s1.services.s1_data_appeal_input_service import S1_APPEAL_DESCRIPTION_INPUT_RULE, pack_appeal_description_inputs, decode_appeal_response_evidence, build_appeal_record_comparisons
from plugins.adventurer_s1.services.s1_ai_prompt_shared import S1_DATA_APPEAL_EVENT_NUMBERING_RULES, S1_DATA_APPEAL_PLAYER_DIRECTED_CONTENT_RULES, S1_DATA_APPEAL_REJECTION_RESOLUTION_BASES, S1_DATA_APPEAL_REVIEW_CONTENT_OUTLET, S1_DATA_APPEAL_RESPONSIBILITY_CONTRACT, S1_DATA_APPEAL_DATA_FIX_SCOPE, S1_DATA_APPEAL_FACT_SHEET_INPUT_RULE, S1_DATA_APPEAL_TRAIT_CONSOLIDATION_RULES, S1_DATA_APPEAL_SHARED_CACHE_PREFIX, S1_DATA_APPEAL_SHARED_EVIDENCE_PREFIX_CONFIG, S1_ECONOMIC_RESOURCE_SOURCE_RULES, S1_PLAYER_DIRECTED_CREATIVE_CONTENT_BOUNDARY, S1_SKILL_RESOURCE_AUTHORITY_BOUNDARY, build_s1_data_appeal_messages, log_s1_data_appeal_cache_diagnostics
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage, s1_ai_usage_has_positive_tokens
from plugins.adventurer_s1.services.s1_ai_player_name_service import post_s1_ai_request_with_player_aliases as post_s1_ai_request, with_player_name_aliases
from plugins.adventurer_s1.services.s1_ai_json_response_service import S1AiJsonResult, S1AiResponseContractResult, build_s1_ai_raw_excerpt, extract_s1_ai_response_content, extract_s1_ai_reasoning_content, parse_s1_ai_http_json_response, parse_s1_ai_json_response
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_integer, normalize_exact_boolean
from plugins.adventurer_s1.services.adventurer_s1_data_appeal_destructive_contract import DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS, get_data_appeal_destructive_action_max
from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import DATA_APPEAL_OPERATION_CONTRACT_VERSION, build_data_appeal_operation_manifest, normalize_data_appeal_number
logger = logging.getLogger(__name__)

class _DeferredDataAppealReviewTokenUsage:
    """Delay one valid review attempt until its persisted consumer finishes."""

    def __init__(self, *, call_type: str, usage: dict, model: str, appeal_id: int, adventurer_id: int | None, attempt_number: int, finish_reason: str | None=None, json_object_mode: bool | None=None, provider_request_id: str | None=None, repair_applied: bool=False, failed_response_excerpt: str | None=None) -> None:
        self._call_type = call_type
        self._usage = usage
        self._model = model
        self._appeal_id = appeal_id
        self._adventurer_id = adventurer_id
        self._attempt_number = attempt_number
        self._finish_reason = finish_reason
        self._json_object_mode = json_object_mode
        self._provider_request_id = provider_request_id
        self._repair_applied = repair_applied
        self._failed_response_excerpt = failed_response_excerpt
        self._finalized = False

    def finalize(self, *, is_success: bool) -> None:
        if self._finalized:
            return
        self._finalized = True
        record_token_usage(call_type=self._call_type, usage=self._usage, model=self._model, appeal_id=self._appeal_id, adventurer_id=self._adventurer_id, attempt_number=self._attempt_number, is_success=is_success is True, failure_stage='consumer_contract' if is_success is not True else None, validation_error_code='consumer.contract_failed' if is_success is not True else None, finish_reason=self._finish_reason, json_object_mode=self._json_object_mode, provider_request_id=self._provider_request_id, repair_applied=self._repair_applied, failed_response_excerpt=self._failed_response_excerpt)
_CHINESE_TEXT_RE = re.compile('[\\u3400-\\u9fff]')
from plugins.adventurer_s1.services.s1_team_loot_contract import TEAM_LOOT_APPEAL_RULE, TEAM_LOOT_STRUCTURE_RULE
_REVIEW_SECTIONS = frozenset({'traits', 'equipment', 'warehouse', 'skills', 'create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'team_pool', 'reclaim_items', 'unassigned_items', *DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS})
_REVIEW_RECORD_ID_SECTIONS = frozenset({'traits', 'equipment', 'warehouse', 'skills', *DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS})
_REVIEW_EXACT_ID_SECTIONS = frozenset(DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS)
_REVIEW_SECTION_SCOPED_SECTIONS = _REVIEW_SECTIONS - _REVIEW_RECORD_ID_SECTIONS
_REVIEW_LEGACY_CREATE_NAME_SECTIONS = frozenset({'create_items', 'create_traits', 'create_skills'})
_REVIEW_RESULT_ALLOWED_FIELDS = frozenset({'verdict_contract_version', 'decision', 'request_verdicts', 'verdicts', 'supplement_patch', 'override_patch', 'clear_fields', 'summary', 'player_reason', 'issues'})
_REVIEW_RESULT_REQUIRED_FIELDS = frozenset({'decision', 'summary', 'player_reason'})
_REVIEW_RETRYABLE_STRUCTURE_FAILURES = frozenset({'empty_response', 'truncated_response', 'json_parse_failed', 'result_validation_failed'})
_REVIEW_NON_REPAIRABLE_VALIDATION_SUFFIXES = frozenset({'ids_create_full_coverage_required', 'ids_section_scoped_forbidden', 'duplicate_section_scoped_verdict', 'ids_invalid', 'ids_overlap', 'ids_outside_patch', 'ids_required', 'mixed_record_scope', 'operation_manifest_invalid', 'operation_id_invalid', 'operation_id_duplicate', 'verdicts_outside_manifest', 'request_scope_missing', 'request_id_invalid', 'request_ids_invalid', 'atomic_group_contract_conflict', 'atomic_group_cross_source', 'risk_authorization_required', 'risk_authorization_split_required', 'risk_fields_incomplete', 'risk_basis_invalid', 'objective_evidence_invalid', 'not_player_preference_invalid'})
_REVIEW_ITEM_EVIDENCE_COMPLETION_SUFFIXES = frozenset({'quantity_evidence_invalid', 'quantity_exact_evidence_required', 'quantity_evidence_event_indexes_invalid', 'quantity_evidence_event_indexes_forbidden', 'quantity_evidence_value_invalid', 'quantity_evidence_value_mismatch', 'narrative_evidence_verification_required', 'narrative_evidence_verification_invalid', 'narrative_evidence_verification_mismatch', 'narrative_evidence_verification_forbidden'})
_REVIEW_PRIMARY_SUPPORT_RECONSIDERATION_SUFFIX = 'primary_support_rejected'
from plugins.adventurer_s1.services.adventurer_s1_data_appeal_ai_service import DATA_APPEAL_CURRENCY_ADJUSTMENT_STRUCTURE_RULE, DATA_APPEAL_PATCH_RECORD_SECTION_STRUCTURE_RULE, DATA_APPEAL_RECORD_SECTIONS, _APPEAL_CODENAME_ADDENDUM, _EQUIPMENT_DESCRIPTION_INHERITANCE_EVIDENCE_RULES, _EQUIPMENT_RARITY_RESTORE_EVIDENCE_RULES, _RECOVERY_AUTHORITY_EVIDENCE_RULES, _SETTLEMENT_ASSET_MUTATION_EVIDENCE_RULES, _validate_data_appeal_patch

def _runtime_metadata_bool(value: object) -> bool | None:
    normalized = str(value or '').strip().lower()
    if normalized == 'true':
        return True
    if normalized == 'false':
        return False
    return value if type(value) is bool else None
_REVIEW_SYSTEM_PROMPT = ''
_REVIEW_SYSTEM_PROMPT += ''
_REVIEW_RECORD_SYSTEM_PROMPT = ''
_REVIEW_SYSTEM_PROMPT += ''
_REVIEW_SYSTEM_PROMPT += ''
_REVIEW_SYSTEM_PROMPT += ''
_REVIEW_SYSTEM_PROMPT += ''
_REVIEW_COMMON_TAIL = ''
_REVIEW_COMMON_TAIL += ''
_REVIEW_SYSTEM_PROMPT += ''
_REVIEW_SYSTEM_PROMPT += ''
_DATA_FIX_REVIEW_ADDENDUM = ''
_REVIEW_DATA_FIX_SYSTEM_PROMPT = ''
_EQUIPMENT_DESCRIPTION_INHERITANCE_REVIEW_ADDENDUM = ''
_PLAYER_REVIEW_SYSTEM_PROMPT = ''
_PLAYER_REVIEW_SYSTEM_PROMPT += ''
_PLAYER_REVIEW_SYSTEM_PROMPT += ''
_PLAYER_REVIEW_SYSTEM_PROMPT += ''
_PLAYER_REVIEW_SYSTEM_PROMPT += ''
_PLAYER_REVIEW_SYSTEM_PROMPT += ''
_PLAYER_REVIEW_SYSTEM_PROMPT += ''
_PLAYER_REVIEW_SYSTEM_PROMPT += ''
_PLAYER_REVIEW_SYSTEM_PROMPT += ''
from plugins.adventurer_s1.services.s1_free_appeal_contract import FREE_APPEAL_COMMON, FREE_APPEAL_REVIEW, FREE_APPEAL_PLAYER_REVIEW, FREE_APPEAL_REVERT
_REVIEW_SYSTEM_PROMPT += FREE_APPEAL_REVIEW
_REVIEW_DATA_FIX_SYSTEM_PROMPT += FREE_APPEAL_REVIEW
_PLAYER_REVIEW_SYSTEM_PROMPT += FREE_APPEAL_PLAYER_REVIEW + S1_DATA_APPEAL_PLAYER_DIRECTED_CONTENT_RULES

def _parse_json(content: str) -> dict | None:
    text = (content or '').strip()
    if '```' in text:
        start = text.find('{')
        end = text.rfind('}')
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None

def _parse_review_json_response(response_json: object) -> S1AiJsonResult:
    """Keep the legacy review parser authoritative, then add local repair."""
    extracted = extract_s1_ai_response_content(response_json)
    if not extracted.content:
        return extracted
    legacy_parsed = _parse_json(extracted.content)
    if legacy_parsed is not None:
        return S1AiJsonResult(data=legacy_parsed, content=extracted.content, raw_excerpt=extracted.raw_excerpt, finish_reason=extracted.finish_reason)
    return parse_s1_ai_json_response(response_json, repair_mode='lossless_syntax_only')

def _validate_review_patch(value: object, *, ordinary_only: bool=False) -> dict | None:
    if not isinstance(value, dict):
        return None
    expanded = dict(value)
    for section in DATA_APPEAL_RECORD_SECTIONS:
        if section not in expanded:
            continue
        section_value = expanded[section]
        if not isinstance(section_value, dict):
            return None
        expanded[section] = {'update': section_value.get('update', []), 'delete': section_value.get('delete', []), **{key: item for key, item in section_value.items() if key not in {'update', 'delete'}}}
    validated = _validate_data_appeal_patch(expanded)
    if validated is None:
        return None
    if ordinary_only and any((key not in {'traits', 'equipment', 'warehouse', 'skills'} for key in validated)):
        return None
    return validated

def _validate_clear_fields(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    value = {key: section_value for key, section_value in value.items() if key in {'traits', 'equipment', 'warehouse', 'skills'}}
    allowed_fields = {'traits': {'description', 'recovered_desc'}, 'equipment': {'custom_desc', 'damage_note'}, 'warehouse': {'custom_desc'}, 'skills': {'description'}}
    normalized: dict[str, list[dict]] = {}
    for section, rows in value.items():
        if not isinstance(rows, list):
            return None
        normalized_rows: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                return None
            row = {key: row[key] for key in ('id', 'fields', 'reason') if key in row}
            if frozenset(row) != frozenset({'id', 'fields', 'reason'}):
                return None
            raw_id = row.get('id')
            fields = row.get('fields')
            reason = row.get('reason')
            record_id, _ = normalize_ascii_integer(raw_id, minimum=1)
            if type(record_id) is not int:
                return None
            if record_id <= 0 or not isinstance(fields, list) or (not fields) or any((field not in allowed_fields[section] for field in fields)) or (not isinstance(reason, str)) or (not reason.strip()):
                return None
            normalized_rows.append({'id': record_id, 'fields': fields, 'reason': reason.strip()})
        normalized[section] = normalized_rows
    return normalized

def _collect_review_operation_requirements(*patches: dict | None, clear_fields: dict | None=None) -> tuple[dict[str, set[int]], set[str]] | None:
    """Derive the exact verdict scope from every operation the response can execute."""
    record_ids: dict[str, set[int]] = {}
    section_scoped: set[str] = set()
    for patch in patches:
        if not isinstance(patch, dict):
            continue
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            section_value = patch.get(section)
            if not isinstance(section_value, dict):
                continue
            ids = record_ids.setdefault(section, set())
            for operation in ('update', 'delete'):
                rows = section_value.get(operation) or []
                if not isinstance(rows, list):
                    return None
                for row in rows:
                    raw_id = row.get('id') if isinstance(row, dict) else row
                    record_id, _ = normalize_ascii_integer(raw_id, minimum=1)
                    if type(record_id) is not int or record_id <= 0:
                        return None
                    ids.add(record_id)
            if not ids:
                record_ids.pop(section, None)
        for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            rows = patch.get(section) or []
            if not isinstance(rows, list):
                return None
            ids = record_ids.setdefault(section, set())
            for row in rows:
                if not isinstance(row, dict):
                    return None
                source_id, _ = normalize_ascii_integer(row.get('source_id'), minimum=1)
                if type(source_id) is not int or source_id <= 0:
                    return None
                ids.add(source_id)
            if not ids:
                record_ids.pop(section, None)
        for section in _REVIEW_SECTION_SCOPED_SECTIONS:
            value = patch.get(section)
            has_operation = bool(value) if section != 'team_pool' else isinstance(value, dict) and bool(value)
            if has_operation:
                section_scoped.add(section)
    if isinstance(clear_fields, dict):
        for section, rows in clear_fields.items():
            if section not in _REVIEW_RECORD_ID_SECTIONS:
                return None
            if not isinstance(rows, list):
                return None
            ids = record_ids.setdefault(section, set())
            for row in rows:
                if not isinstance(row, dict):
                    return None
                record_id, _ = normalize_ascii_integer(row.get('id'), minimum=1)
                if type(record_id) is not int or record_id <= 0:
                    return None
                ids.add(record_id)
            if not ids:
                record_ids.pop(section, None)
    return (record_ids, section_scoped)

def _review_verdicts_cover_requirements(verdicts: list[dict], *, record_ids: dict[str, set[int]], section_scoped: set[str]) -> bool:
    by_section: dict[str, list[dict]] = {}
    for verdict in verdicts:
        section = verdict.get('section') if isinstance(verdict, dict) else None
        if type(section) is str:
            by_section.setdefault(section, []).append(verdict)
    for section in section_scoped:
        if not by_section.get(section):
            return False
    for section, required_ids in record_ids.items():
        section_verdicts = by_section.get(section) or []
        if not section_verdicts:
            return False
        if section not in _REVIEW_EXACT_ID_SECTIONS and any(('ids' not in verdict for verdict in section_verdicts)):
            continue
        covered_ids = {record_id for verdict in section_verdicts for record_id in verdict.get('ids') or [] if type(record_id) is int and record_id > 0}
        if not required_ids.issubset(covered_ids):
            return False
    return True

def _legacy_create_ids_cover_full_section(*, section: str, ids: object, normalized_patch: dict | None) -> bool:
    """兼容旧输出中的创建项名称 ids；只接受无歧义的全 section 覆盖。"""
    if section not in _REVIEW_LEGACY_CREATE_NAME_SECTIONS:
        return False
    if not isinstance(ids, list) or not ids:
        return False
    legacy_names: list[str] = []
    for raw_name in ids:
        if not isinstance(raw_name, str) or not raw_name.strip():
            return False
        name = raw_name.strip()
        if name in legacy_names:
            return False
        legacy_names.append(name)
    patch_rows = normalized_patch.get(section) if isinstance(normalized_patch, dict) else None
    if not isinstance(patch_rows, list) or not patch_rows:
        return False
    patch_names: list[str] = []
    for row in patch_rows:
        if not isinstance(row, dict):
            return False
        raw_name = row.get('name')
        if not isinstance(raw_name, str) or not raw_name.strip():
            return False
        name = raw_name.strip()
        if name in patch_names:
            return False
        patch_names.append(name)
    return len(legacy_names) == len(patch_names) and set(legacy_names) == set(patch_names)
_OPERATION_ID_RE = re.compile('^(primary|supplement|override|clear):[a-z_]+:[a-z_]+:[0-9]+$')
_ATOMIC_GROUP_ID_RE = re.compile('^[A-Za-z0-9:_-]{1,80}$')
_QUANTITY_EVIDENCE_SOURCES = frozenset({'encoded_existing_field', 'structured_settlement', 'explicit_narrative'})

def _collect_primary_supported_request_ids(primary_request_checklist: list[dict] | None) -> set[str]:
    """Return request IDs for primary conclusions that proposed real repair."""
    return {str(row.get('request_id') or '').strip() for row in primary_request_checklist or [] if isinstance(row, dict) and (str(row.get('status') or '').strip().lower() == 'done' and str(row.get('resolution_basis') or '').strip() == 'repair_supported' or (str(row.get('status') or '').strip().lower() == 'partial' and str(row.get('resolution_basis') or '').strip() == 'partially_supported'))}

def _validate_operation_contract_review_result(result: dict, *, decision: str, summary: str, player_reason: str, supplement: dict | None, override: dict | None, clear_fields: dict | None, primary_operation_manifest: list[dict] | None, primary_request_checklist: list[dict] | None, enforce_primary_support_consensus: bool, valid_request_ids: set[str] | None, snapshot_before: dict | None, narrative_event_count: int | None, consumption_restore_candidate_names: set[str] | None, diagnostics_out: dict | None=None) -> tuple[dict | None, str | None]:
    """Validate v2 exact-operation verdicts while preserving valid siblings."""
    expected_operations: list[dict] = [row for row in primary_operation_manifest or [] if isinstance(row, dict)]
    expected_operations.extend(build_data_appeal_operation_manifest(supplement, source='supplement', snapshot_before=snapshot_before, consumption_restore_candidate_names=consumption_restore_candidate_names))
    expected_operations.extend(build_data_appeal_operation_manifest(override, source='override', snapshot_before=snapshot_before, consumption_restore_candidate_names=consumption_restore_candidate_names))
    expected_operations.extend(build_data_appeal_operation_manifest(None, source='clear', snapshot_before=snapshot_before, clear_fields=clear_fields))
    expected_by_id = {str(row.get('operation_id') or ''): row for row in expected_operations if str(row.get('operation_id') or '')}
    if len(expected_by_id) != len(expected_operations):
        return (None, 'result.operation_manifest_invalid')
    verdicts = result.get('verdicts')
    if not isinstance(verdicts, list):
        return (None, 'result.verdicts_not_array')
    if expected_by_id and (not verdicts):
        return (None, 'result.verdicts_missing_for_operations')
    if verdicts and (not expected_by_id):
        return (None, 'result.verdicts_outside_manifest')
    normalized_verdicts: list[dict] = []
    seen_operation_ids: set[str] = set()
    group_contract: dict[str, tuple[str, tuple[str, ...]]] = {}
    group_source_family: dict[str, str] = {}
    verdict_allowed = {'operation_id', 'request_ids', 'decision', 'reason', 'atomic_group_id', 'quantity_evidence', 'risk_type', 'basis', 'objective_evidence', 'not_player_preference', 'verified_evidence_event_indexes'}
    for row_index, raw_row in enumerate(verdicts):
        error_prefix = f'result.verdicts[{row_index}]'
        if not isinstance(raw_row, dict):
            return (None, f'{error_prefix}.not_object')
        row = {key: raw_row[key] for key in verdict_allowed if key in raw_row}
        if not {'operation_id', 'request_ids', 'decision', 'reason'}.issubset(row):
            return (None, f'{error_prefix}.required_fields_missing')
        operation_id = str(row.get('operation_id') or '').strip()
        if not _OPERATION_ID_RE.fullmatch(operation_id) or operation_id not in expected_by_id:
            return (None, f'{error_prefix}.operation_id_invalid')
        if operation_id in seen_operation_ids:
            return (None, f'{error_prefix}.operation_id_duplicate')
        seen_operation_ids.add(operation_id)
        row_decision = str(row.get('decision') or '').strip().lower()
        reason = row.get('reason')
        if row_decision not in ('allow', 'reject'):
            return (None, f'{error_prefix}.decision_invalid')
        if not isinstance(reason, str) or not reason.strip():
            return (None, f'{error_prefix}.reason_invalid')
        raw_request_ids = row.get('request_ids')
        if not isinstance(raw_request_ids, list) or not raw_request_ids:
            return (None, f'{error_prefix}.request_ids_invalid')
        request_ids: list[str] = []
        for raw_request_id in raw_request_ids:
            request_id = str(raw_request_id or '').strip()
            if not re.fullmatch('request_[1-8]', request_id) or request_id in request_ids or (valid_request_ids is not None and request_id not in valid_request_ids):
                return (None, f'{error_prefix}.request_ids_invalid')
            request_ids.append(request_id)
        normalized = {'operation_id': operation_id, 'request_ids': request_ids, 'decision': row_decision, 'reason': reason.strip()}
        atomic_group_id = str(row.get('atomic_group_id') or '').strip()
        if atomic_group_id:
            if not _ATOMIC_GROUP_ID_RE.fullmatch(atomic_group_id):
                return (None, f'{error_prefix}.atomic_group_id_invalid')
            contract = (row_decision, tuple(sorted(request_ids)))
            if atomic_group_id in group_contract and group_contract[atomic_group_id] != contract:
                return (None, f'{error_prefix}.atomic_group_contract_conflict')
            group_contract[atomic_group_id] = contract
            source_family = 'primary' if expected_by_id[operation_id].get('source') == 'primary' else 'review_authored'
            if atomic_group_id in group_source_family and group_source_family[atomic_group_id] != source_family:
                return (None, f'{error_prefix}.atomic_group_cross_source')
            group_source_family[atomic_group_id] = source_family
            normalized['atomic_group_id'] = atomic_group_id
        normalized_verdicts.append(normalized)
        continue
    if seen_operation_ids != set(expected_by_id):
        return (None, 'result.verdicts_coverage_incomplete')
    review_record_targets: dict[tuple[str, int], list[dict]] = {}
    for verdict in normalized_verdicts:
        if verdict.get('decision') != 'allow':
            continue
        operation = expected_by_id[verdict['operation_id']]
        if operation.get('source') == 'primary':
            continue
        section = str(operation.get('section') or '')
        target_id = operation.get('target_id')
        if section not in ('traits', 'equipment', 'warehouse', 'skills'):
            continue
        if type(target_id) is not int:
            continue
        review_record_targets.setdefault((section, target_id), []).append(verdict)
    for target_verdicts in review_record_targets.values():
        if len(target_verdicts) <= 1:
            continue
        atomic_group_ids = {str(verdict.get('atomic_group_id') or '').strip() for verdict in target_verdicts}
        if len(atomic_group_ids) != 1 or not next(iter(atomic_group_ids)):
            return (None, 'result.review_authored_merge_requires_atomic_group')
    expected_request_ids = set(valid_request_ids or set())
    if not expected_request_ids:
        return (None, 'result.request_scope_missing')
    primary_supported_request_ids = _collect_primary_supported_request_ids(primary_request_checklist)
    raw_request_verdicts = result.get('request_verdicts')
    if not isinstance(raw_request_verdicts, list) or not raw_request_verdicts:
        return (None, 'result.request_verdicts_missing')
    normalized_request_verdicts: list[dict] = []
    seen_request_ids: set[str] = set()
    primary_support_conflict_error: str | None = None
    linked_decisions: dict[str, list[str]] = {request_id: [] for request_id in expected_request_ids}
    for operation_verdict in normalized_verdicts:
        for request_id in operation_verdict['request_ids']:
            linked_decisions[request_id].append(operation_verdict['decision'])
    for row_index, raw_row in enumerate(raw_request_verdicts):
        error_prefix = f'result.request_verdicts[{row_index}]'
        if not isinstance(raw_row, dict):
            return (None, f'{error_prefix}.not_object')
        row = {key: raw_row[key] for key in ('request_id', 'decision', 'resolution_basis', 'reason') if key in raw_row}
        required_request_verdict_fields = {'request_id', 'decision', 'resolution_basis', 'reason'}
        if 'resolution_basis' not in row and {'request_id', 'decision', 'reason'}.issubset(row):
            return (None, f'{error_prefix}.resolution_basis_required')
        if frozenset(row) != frozenset(required_request_verdict_fields):
            return (None, f'{error_prefix}.required_fields_missing')
        request_id = str(row.get('request_id') or '').strip()
        request_decision = str(row.get('decision') or '').strip().lower()
        resolution_basis = str(row.get('resolution_basis') or '').strip()
        reason = row.get('reason')
        if request_id not in expected_request_ids or request_id in seen_request_ids:
            return (None, f'{error_prefix}.request_id_invalid')
        if request_decision not in ('allow', 'partial', 'reject', 'pending'):
            return (None, f'{error_prefix}.decision_invalid')
        if request_decision == 'allow' and resolution_basis != 'repair_supported' or (request_decision == 'partial' and resolution_basis != 'partially_supported') or (request_decision == 'reject' and resolution_basis not in S1_DATA_APPEAL_REJECTION_RESOLUTION_BASES) or (request_decision == 'pending' and resolution_basis != 'repair_pending'):
            return (None, f'{error_prefix}.resolution_basis_invalid')
        if not isinstance(reason, str) or not reason.strip():
            return (None, f'{error_prefix}.reason_invalid')
        operation_decisions = linked_decisions[request_id]
        has_allow = 'allow' in operation_decisions
        has_reject = 'reject' in operation_decisions
        if request_decision == 'allow' and (not has_allow or has_reject):
            return (None, f'{error_prefix}.decision_operation_mismatch')
        if request_decision == 'partial' and (not (has_allow and has_reject)):
            return (None, f'{error_prefix}.decision_operation_mismatch')
        if request_decision == 'reject' and has_allow:
            return (None, f'{error_prefix}.decision_operation_mismatch')
        if request_decision == 'pending' and has_allow:
            return (None, f'{error_prefix}.decision_operation_mismatch')
        if enforce_primary_support_consensus and request_decision == 'reject' and (request_id in primary_supported_request_ids) and (primary_support_conflict_error is None):
            primary_support_conflict_error = f'{error_prefix}.{_REVIEW_PRIMARY_SUPPORT_RECONSIDERATION_SUFFIX}'
        seen_request_ids.add(request_id)
        normalized_request_verdicts.append({'request_id': request_id, 'decision': request_decision, 'resolution_basis': resolution_basis, 'reason': reason.strip()})
    if seen_request_ids != expected_request_ids:
        return (None, 'result.request_verdicts_coverage_incomplete')
    if any((row['decision'] == 'pending' for row in normalized_request_verdicts)):
        decision = 'pending'
    elif all((row['decision'] == 'allow' for row in normalized_verdicts)) and all((row['decision'] == 'allow' for row in normalized_request_verdicts)):
        decision = 'allow'
    else:
        decision = 'reject'
    issues = result.get('issues', [])
    if not isinstance(issues, list):
        return (None, 'result.issues_not_array')
    normalized_issues: list[dict] = []
    for row_index, row in enumerate(issues):
        error_prefix = f'result.issues[{row_index}]'
        if not isinstance(row, dict):
            return (None, f'{error_prefix}.not_object')
        row = {key: row[key] for key in ('severity', 'detail') if key in row}
        if frozenset(row) != frozenset({'severity', 'detail'}):
            return (None, f'{error_prefix}.required_fields_missing')
        if row.get('severity') not in ('high', 'medium', 'low'):
            return (None, f'{error_prefix}.severity_invalid')
        if not isinstance(row.get('detail'), str) or not row['detail'].strip():
            return (None, f'{error_prefix}.detail_invalid')
        normalized_issues.append({'severity': row['severity'], 'detail': row['detail'].strip()})
    normalized = {'verdict_contract_version': DATA_APPEAL_OPERATION_CONTRACT_VERSION, 'decision': decision, 'request_verdicts': normalized_request_verdicts, 'verdicts': normalized_verdicts, 'summary': summary.strip(), 'player_reason': player_reason.strip(), 'issues': normalized_issues}
    if supplement is not None:
        normalized['supplement_patch'] = supplement
    if override is not None:
        normalized['override_patch'] = override
    if clear_fields is not None:
        normalized['clear_fields'] = clear_fields
    if primary_support_conflict_error is not None:
        return (None, primary_support_conflict_error)
    return (normalized, None)

def _validate_data_appeal_review_result_with_error(result: object, *, normalized_patch: dict | None=None, primary_operation_manifest: list[dict] | None=None, primary_request_checklist: list[dict] | None=None, enforce_primary_support_consensus: bool=False, valid_request_ids: set[str] | None=None, snapshot_before: dict | None=None, narrative_event_count: int | None=None, consumption_restore_candidate_names: set[str] | None=None, diagnostics_out: dict | None=None) -> tuple[dict | None, str | None]:
    if diagnostics_out is not None:
        diagnostics_out.clear()
    if not isinstance(result, dict):
        return (None, 'result.not_object')
    result = {str(key): value for key, value in result.items() if str(key) in _REVIEW_RESULT_ALLOWED_FIELDS}
    if not _REVIEW_RESULT_REQUIRED_FIELDS.issubset(result):
        return (None, 'result.required_fields_missing')
    decision = result.get('decision')
    summary = result.get('summary')
    player_reason = result.get('player_reason')
    if decision not in ('allow', 'reject', 'pending'):
        return (None, 'result.decision_invalid')
    if not isinstance(summary, str) or not summary.strip() or (not _CHINESE_TEXT_RE.search(summary)):
        return (None, 'result.summary_invalid')
    if not isinstance(player_reason, str) or not player_reason.strip() or (not _CHINESE_TEXT_RE.search(player_reason)):
        return (None, 'result.player_reason_invalid')
    supplement = None
    if 'supplement_patch' in result:
        supplement = _validate_review_patch(result['supplement_patch'])
        if supplement is None:
            return (None, 'result.supplement_patch_invalid')
    override = None
    if 'override_patch' in result:
        override = _validate_review_patch(result['override_patch'], ordinary_only=True)
        if override is None:
            return (None, 'result.override_patch_invalid')
    clear_fields = None
    if 'clear_fields' in result:
        clear_fields = _validate_clear_fields(result['clear_fields'])
        if clear_fields is None:
            return (None, 'result.clear_fields_invalid')
    raw_contract_version = result.get('verdict_contract_version')
    contract_version, _ = normalize_ascii_integer(raw_contract_version, minimum=1)
    if contract_version != DATA_APPEAL_OPERATION_CONTRACT_VERSION and any((patch and patch.get('unassigned_items') for patch in (normalized_patch, supplement, override))):
        return (None, 'result.team_loot_operation_contract_required')
    if raw_contract_version is None and (primary_operation_manifest is not None or valid_request_ids is not None):
        return (None, 'result.verdict_contract_version_required')
    if raw_contract_version is not None and contract_version != DATA_APPEAL_OPERATION_CONTRACT_VERSION:
        return (None, 'result.verdict_contract_version_invalid')
    if contract_version == DATA_APPEAL_OPERATION_CONTRACT_VERSION:
        if primary_operation_manifest is None:
            primary_operation_manifest = build_data_appeal_operation_manifest(normalized_patch, source='primary', snapshot_before=snapshot_before, consumption_restore_candidate_names=consumption_restore_candidate_names)
        return _validate_operation_contract_review_result(result, decision=decision, summary=summary, player_reason=player_reason, supplement=supplement, override=override, clear_fields=clear_fields, primary_operation_manifest=primary_operation_manifest, primary_request_checklist=primary_request_checklist, enforce_primary_support_consensus=enforce_primary_support_consensus, valid_request_ids=valid_request_ids, snapshot_before=snapshot_before, narrative_event_count=narrative_event_count, consumption_restore_candidate_names=consumption_restore_candidate_names, diagnostics_out=diagnostics_out)
    operation_requirements = _collect_review_operation_requirements(normalized_patch, supplement, override, clear_fields=clear_fields)
    if operation_requirements is None:
        return (None, 'result.operation_scope_invalid')
    required_record_ids, required_section_scoped = operation_requirements
    review_authored_requirements = _collect_review_operation_requirements(None, supplement, override, clear_fields=clear_fields)
    if review_authored_requirements is None:
        return (None, 'result.operation_scope_invalid')
    review_authored_record_ids, review_authored_section_scoped = review_authored_requirements
    verdicts = result.get('verdicts', [])
    if not isinstance(verdicts, list):
        return (None, 'result.verdicts_not_array')
    normalized_verdicts: list[dict] = []
    seen_section_scoped: set[str] = set()
    record_overall_sections: set[str] = set()
    record_seen_ids: dict[str, set[int]] = {}
    verdict_allowed = {'section', 'ids', 'decision', 'reason', 'risk_type', 'basis', 'objective_evidence', 'not_player_preference'}
    for row_index, row in enumerate(verdicts):
        error_prefix = f'result.verdicts[{row_index}]'
        if not isinstance(row, dict):
            return (None, f'{error_prefix}.not_object')
        row = {key: row[key] for key in verdict_allowed if key in row}
        if not {'section', 'decision', 'reason'}.issubset(row):
            return (None, f'{error_prefix}.required_fields_missing')
        section = row.get('section')
        row_decision = row.get('decision')
        reason = row.get('reason')
        if section not in _REVIEW_SECTIONS:
            return (None, f'{error_prefix}.section_invalid')
        if row_decision not in ('allow', 'reject'):
            return (None, f'{error_prefix}.decision_invalid')
        if not isinstance(reason, str) or not reason.strip():
            return (None, f'{error_prefix}.reason_invalid')
        if section in _REVIEW_SECTION_SCOPED_SECTIONS:
            if section in seen_section_scoped:
                return (None, f'{error_prefix}.duplicate_section_scoped_verdict')
            seen_section_scoped.add(section)
        normalized = {'section': section, 'decision': row_decision, 'reason': reason.strip()}
        if 'ids' in row and section in _REVIEW_RECORD_ID_SECTIONS:
            if section in record_overall_sections:
                return (None, f'{error_prefix}.mixed_record_scope')
            ids = row.get('ids')
            if not isinstance(ids, list):
                singleton_id, _ = normalize_ascii_integer(ids, minimum=1)
                if type(singleton_id) is not int or singleton_id <= 0:
                    return (None, f'{error_prefix}.ids_not_array')
                ids = [singleton_id]
            if not ids:
                return (None, f'{error_prefix}.ids_not_array')
            normalized_ids: list[int] = []
            for raw_id in ids:
                record_id, _ = normalize_ascii_integer(raw_id, minimum=1)
                if type(record_id) is not int or record_id <= 0:
                    return (None, f'{error_prefix}.ids_invalid')
                if record_id in normalized_ids:
                    continue
                normalized_ids.append(record_id)
            seen_ids = record_seen_ids.setdefault(section, set())
            if seen_ids.intersection(normalized_ids):
                return (None, f'{error_prefix}.ids_overlap')
            seen_ids.update(normalized_ids)
            if section in _REVIEW_EXACT_ID_SECTIONS and any((record_id not in required_record_ids.get(section, set()) for record_id in normalized_ids)):
                return (None, f'{error_prefix}.ids_outside_patch')
            normalized['ids'] = normalized_ids
        elif 'ids' in row:
            if not _legacy_create_ids_cover_full_section(section=section, ids=row.get('ids'), normalized_patch=normalized_patch):
                error_suffix = 'ids_create_full_coverage_required' if section in _REVIEW_LEGACY_CREATE_NAME_SECTIONS else 'ids_section_scoped_forbidden'
                return (None, f'{error_prefix}.{error_suffix}')
        elif section in _REVIEW_EXACT_ID_SECTIONS:
            return (None, f'{error_prefix}.ids_required')
        elif section in _REVIEW_RECORD_ID_SECTIONS:
            if section in record_overall_sections or record_seen_ids.get(section):
                return (None, f'{error_prefix}.mixed_record_scope')
            record_overall_sections.add(section)
        risk_fields = {'risk_type', 'basis', 'objective_evidence', 'not_player_preference'}
        present_risk_fields = risk_fields.intersection(row)
        if present_risk_fields:
            if present_risk_fields != risk_fields:
                return (None, f'{error_prefix}.risk_fields_incomplete')
            risk_type = row.get('risk_type')
            basis = row.get('basis')
            if risk_type == 'item_identity_name_change':
                if basis not in {'expedition_evidence', 'data_inconsistency', 'mechanical_cleanup', 'history_consistency', 'field_misalignment'}:
                    return (None, f'{error_prefix}.risk_basis_invalid')
            elif risk_type == 'item_consumption_type_correction':
                if basis != 'authoritative_item_description':
                    return (None, f'{error_prefix}.risk_basis_invalid')
            else:
                return (None, f'{error_prefix}.risk_type_invalid')
            if not isinstance(row.get('objective_evidence'), str) or not row['objective_evidence'].strip():
                return (None, f'{error_prefix}.objective_evidence_invalid')
            not_player_preference, _ = normalize_exact_boolean(row.get('not_player_preference'))
            if not_player_preference is not True:
                return (None, f'{error_prefix}.not_player_preference_invalid')
            row['not_player_preference'] = True
            normalized.update({field: row[field] for field in risk_fields})
        normalized_verdicts.append(normalized)
    has_operations_requiring_verdicts = bool(required_record_ids or required_section_scoped)
    if has_operations_requiring_verdicts and (not normalized_verdicts):
        return (None, 'result.verdicts_missing_for_operations')
    if (review_authored_record_ids or review_authored_section_scoped) and (not _review_verdicts_cover_requirements(normalized_verdicts, record_ids=review_authored_record_ids, section_scoped=review_authored_section_scoped)):
        return (None, 'result.verdicts_coverage_incomplete')
    verdict_coverage_complete = not has_operations_requiring_verdicts or _review_verdicts_cover_requirements(normalized_verdicts, record_ids=required_record_ids, section_scoped=required_section_scoped)
    if normalized_verdicts:
        decision = 'reject' if not verdict_coverage_complete or any((row['decision'] == 'reject' for row in normalized_verdicts)) else 'allow'
    issues = result.get('issues', [])
    if not isinstance(issues, list):
        return (None, 'result.issues_not_array')
    normalized_issues: list[dict] = []
    for row_index, row in enumerate(issues):
        error_prefix = f'result.issues[{row_index}]'
        if not isinstance(row, dict):
            return (None, f'{error_prefix}.not_object')
        row = {key: row[key] for key in ('severity', 'detail') if key in row}
        if frozenset(row) != frozenset({'severity', 'detail'}):
            return (None, f'{error_prefix}.required_fields_missing')
        if row.get('severity') not in ('high', 'medium', 'low'):
            return (None, f'{error_prefix}.severity_invalid')
        if not isinstance(row.get('detail'), str) or not row['detail'].strip():
            return (None, f'{error_prefix}.detail_invalid')
        normalized_issues.append({'severity': row['severity'], 'detail': row['detail'].strip()})
    normalized = {'decision': decision, 'verdicts': normalized_verdicts, 'summary': summary.strip(), 'player_reason': player_reason.strip(), 'issues': normalized_issues}
    if supplement is not None:
        normalized['supplement_patch'] = supplement
    if override is not None:
        normalized['override_patch'] = override
    if clear_fields is not None:
        normalized['clear_fields'] = clear_fields
    return (normalized, None)

def _validate_data_appeal_review_result(result: object, *, normalized_patch: dict | None=None) -> dict | None:
    normalized, _ = _validate_data_appeal_review_result_with_error(result, normalized_patch=normalized_patch)
    return normalized

def _build_review_structure_repair_rule(error_code: str) -> str:
    return '沿当前自由申诉合同，只修复错误路径涉及的结构、完整覆盖和操作关联；保留已明确的玩家目标与独立操作，并按逐项结果重算顶层裁决。不增加历史证据或合理性审核要求。'

def _build_review_item_evidence_completion_rule(error_code: str) -> str:
    """Build one-operation evidence completion without granting new authority."""
    suffix = str(error_code or '').rsplit('.', 1)[-1]
    if suffix.startswith('quantity_'):
        return '只重新核对错误路径指向的一个 operation verdict。若冻结 operation_manifest.numeric_target 与权威输入能直接证明同一精确值，补齐或修正该 verdict 的 quantity_evidence：certainty 使用 exact，value 必须等于 numeric_target，source 与 evidence 必须指向实际权威位置；若没有精确总数、但该 operation 与权威历史正文满足 system 中完整的 bounded 物品最低补偿合同，则 certainty 保持 bounded，绝不能伪装成 exact；create/reclaim 的 value 使用 entitlement，已有堆叠 update 的 value 使用当前快照加短缺后的绝对目标。source=explicit_narrative 时回到完整 all_events 核对对应原事件，给出有效且去重的零基索引；事实卡只用于定位，不替代原文，也不按摘要位置重编号。其它 source 不得带事件索引。若无法直接证明，必须只把该 operation 改为 reject；若它有 atomic_group_id，则同步 reject 同一原子组。同时移除被 reject verdict 的 allow-only 证据字段；不得编造或借用独立兄弟项证据'
    return '只重新核对错误路径指向的一个 operation verdict。若该 operation manifest 声明的正文证据索引能由 all_events 独立确认，verified_evidence_event_indexes 必须与 manifest 的稳定索引完全一致；若无法确认，必须只把该 operation 改为 reject；若它有 atomic_group_id，则同步 reject 同一原子组。同时移除被 reject verdict 的 allow-only 证据字段，不得编造、替换或借用独立兄弟项证据'

def _build_review_primary_support_reconsideration_rule() -> str:
    """Resolve bounded jury disagreements without making jury 1 authoritative."""
    return '重新核对一审有方案而二审整项拒绝的诉求：玩家要求即本次修改依据，不需要历史证明。按当前自由申诉合同核对精确目标和可执行字段，只重新裁决已有操作，方案可执行则 allow；保持所有 patch 与 operation_id 集合不变。还需新增操作、缺准确目标或表达方式时 pending + repair_pending；同案已完整执行则 reject + already_satisfied_same_case；执行器不支持则 reject + outside_authority。保持独立诉求与操作关联完整。'

def _project_review_item_evidence_completion_candidate(base: object, repaired: object, *, error_code: str) -> tuple[dict | None, str | None]:
    """Keep every independent sibling frozen during one evidence completion."""
    if not isinstance(base, dict) or not isinstance(repaired, dict):
        return (None, 'result.item_evidence_completion_not_object')
    match = re.match('^result\\.verdicts\\[(\\d+)\\]\\.', str(error_code or ''))
    base_verdicts = base.get('verdicts')
    repaired_verdicts = repaired.get('verdicts')
    if match is None or not isinstance(base_verdicts, list) or (not isinstance(repaired_verdicts, list)):
        return (None, 'result.item_evidence_completion_scope_invalid')
    target_index = int(match.group(1))
    if target_index >= len(base_verdicts):
        return (None, 'result.item_evidence_completion_scope_invalid')
    target_row = base_verdicts[target_index]
    if not isinstance(target_row, dict):
        return (None, 'result.item_evidence_completion_scope_invalid')
    target_operation_id = str(target_row.get('operation_id') or '').strip()
    if not target_operation_id:
        return (None, 'result.item_evidence_completion_scope_invalid')
    target_group_id = str(target_row.get('atomic_group_id') or '').strip()
    target_operation_ids = {target_operation_id}
    if target_group_id:
        target_operation_ids = {str(row.get('operation_id') or '').strip() for row in base_verdicts if isinstance(row, dict) and str(row.get('atomic_group_id') or '').strip() == target_group_id}
    repaired_by_operation_id: dict[str, dict] = {}
    for row in repaired_verdicts:
        if not isinstance(row, dict):
            continue
        operation_id = str(row.get('operation_id') or '').strip()
        if not operation_id or operation_id in repaired_by_operation_id:
            return (None, 'result.item_evidence_completion_scope_invalid')
        repaired_by_operation_id[operation_id] = row
    if not target_operation_ids.issubset(repaired_by_operation_id):
        return (None, 'result.item_evidence_completion_scope_invalid')
    projected = copy.deepcopy(base)
    projected_verdicts: list[object] = []
    affected_request_ids: set[str] = set()
    for base_row in base_verdicts:
        if not isinstance(base_row, dict):
            projected_verdicts.append(copy.deepcopy(base_row))
            continue
        operation_id = str(base_row.get('operation_id') or '').strip()
        if operation_id not in target_operation_ids:
            projected_verdicts.append(copy.deepcopy(base_row))
            continue
        repair_row = repaired_by_operation_id[operation_id]
        replacement = copy.deepcopy(base_row)
        for field in ('decision', 'reason'):
            if field in repair_row:
                replacement[field] = copy.deepcopy(repair_row[field])
        for field in ('quantity_evidence', 'verified_evidence_event_indexes'):
            if field in repair_row:
                replacement[field] = copy.deepcopy(repair_row[field])
            elif str(replacement.get('decision') or '').strip().lower() == 'reject':
                replacement.pop(field, None)
        projected_verdicts.append(replacement)
        affected_request_ids.update((str(request_id or '').strip() for request_id in base_row.get('request_ids') or []))
    projected['verdicts'] = projected_verdicts
    base_request_verdicts = base.get('request_verdicts')
    repaired_request_verdicts = repaired.get('request_verdicts')
    if not isinstance(base_request_verdicts, list) or not isinstance(repaired_request_verdicts, list):
        return (None, 'result.item_evidence_completion_scope_invalid')
    repaired_by_request_id: dict[str, dict] = {}
    for row in repaired_request_verdicts:
        if not isinstance(row, dict):
            continue
        request_id = str(row.get('request_id') or '').strip()
        if not request_id or request_id in repaired_by_request_id:
            return (None, 'result.item_evidence_completion_scope_invalid')
        repaired_by_request_id[request_id] = row
    if not affected_request_ids.issubset(repaired_by_request_id):
        return (None, 'result.item_evidence_completion_scope_invalid')
    projected_request_verdicts: list[object] = []
    for base_row in base_request_verdicts:
        if not isinstance(base_row, dict):
            projected_request_verdicts.append(copy.deepcopy(base_row))
            continue
        request_id = str(base_row.get('request_id') or '').strip()
        if request_id not in affected_request_ids:
            projected_request_verdicts.append(copy.deepcopy(base_row))
            continue
        repair_row = repaired_by_request_id[request_id]
        replacement = copy.deepcopy(base_row)
        for field in ('decision', 'resolution_basis', 'reason'):
            if field in repair_row:
                replacement[field] = copy.deepcopy(repair_row[field])
        projected_request_verdicts.append(replacement)
    projected['request_verdicts'] = projected_request_verdicts
    for field in ('decision', 'summary', 'player_reason'):
        if field in repaired:
            projected[field] = copy.deepcopy(repaired[field])
    return (projected, None)

def _project_review_primary_support_reconsideration_candidate(base: object, repaired: object, *, error_code: str, primary_request_checklist: list[dict] | None) -> tuple[dict | None, str | None]:
    """Freeze unrelated siblings while jury disagreements are reconsidered."""
    if not isinstance(base, dict) or not isinstance(repaired, dict):
        return (None, 'result.primary_support_reconsideration_not_object')
    match = re.match('^result\\.request_verdicts\\[(\\d+)\\]\\.primary_support_rejected$', str(error_code or ''))
    base_request_verdicts = base.get('request_verdicts')
    repaired_request_verdicts = repaired.get('request_verdicts')
    base_verdicts = base.get('verdicts')
    repaired_verdicts = repaired.get('verdicts')
    if match is None or not isinstance(base_request_verdicts, list) or (not isinstance(repaired_request_verdicts, list)) or (not isinstance(base_verdicts, list)) or (not isinstance(repaired_verdicts, list)):
        return (None, 'result.primary_support_reconsideration_scope_invalid')
    target_index = int(match.group(1))
    if target_index >= len(base_request_verdicts):
        return (None, 'result.primary_support_reconsideration_scope_invalid')
    target_request_row = base_request_verdicts[target_index]
    if not isinstance(target_request_row, dict):
        return (None, 'result.primary_support_reconsideration_scope_invalid')
    target_request_id = str(target_request_row.get('request_id') or '').strip()
    if not target_request_id:
        return (None, 'result.primary_support_reconsideration_scope_invalid')
    primary_supported_request_ids = _collect_primary_supported_request_ids(primary_request_checklist)
    reconsidered_request_ids = {str(row.get('request_id') or '').strip() for row in base_request_verdicts if isinstance(row, dict) and str(row.get('decision') or '').strip().lower() == 'reject' and (str(row.get('request_id') or '').strip() in primary_supported_request_ids)}
    if target_request_id not in reconsidered_request_ids:
        return (None, 'result.primary_support_reconsideration_scope_invalid')
    target_operation_ids = {str(row.get('operation_id') or '').strip() for row in base_verdicts if isinstance(row, dict) and reconsidered_request_ids.intersection({str(request_id or '').strip() for request_id in row.get('request_ids') or []})}
    target_group_ids = {str(row.get('atomic_group_id') or '').strip() for row in base_verdicts if isinstance(row, dict) and str(row.get('operation_id') or '').strip() in target_operation_ids and str(row.get('atomic_group_id') or '').strip()}
    if target_group_ids:
        target_operation_ids.update({str(row.get('operation_id') or '').strip() for row in base_verdicts if isinstance(row, dict) and str(row.get('atomic_group_id') or '').strip() in target_group_ids})
    affected_request_ids = set(reconsidered_request_ids)
    for row in base_verdicts:
        if not isinstance(row, dict) or str(row.get('operation_id') or '').strip() not in target_operation_ids:
            continue
        affected_request_ids.update((str(request_id or '').strip() for request_id in row.get('request_ids') or [] if str(request_id or '').strip()))
    repaired_by_operation_id: dict[str, dict] = {}
    for row in repaired_verdicts:
        if not isinstance(row, dict):
            continue
        operation_id = str(row.get('operation_id') or '').strip()
        if not operation_id or operation_id in repaired_by_operation_id:
            return (None, 'result.primary_support_reconsideration_scope_invalid')
        repaired_by_operation_id[operation_id] = row
    base_operation_ids = {str(row.get('operation_id') or '').strip() for row in base_verdicts if isinstance(row, dict)}
    if set(repaired_by_operation_id) != base_operation_ids:
        return (None, 'result.primary_support_reconsideration_scope_invalid')
    projected = copy.deepcopy(base)
    projected_verdicts: list[object] = []
    mutable_operation_fields = ('decision', 'reason', 'quantity_evidence', 'verified_evidence_event_indexes', 'risk_type', 'basis', 'objective_evidence', 'not_player_preference')
    for base_row in base_verdicts:
        if not isinstance(base_row, dict):
            projected_verdicts.append(copy.deepcopy(base_row))
            continue
        operation_id = str(base_row.get('operation_id') or '').strip()
        if operation_id not in target_operation_ids:
            projected_verdicts.append(copy.deepcopy(base_row))
            continue
        repair_row = repaired_by_operation_id[operation_id]
        replacement = copy.deepcopy(base_row)
        for field in mutable_operation_fields:
            replacement.pop(field, None)
            if field in repair_row:
                replacement[field] = copy.deepcopy(repair_row[field])
        projected_verdicts.append(replacement)
    projected['verdicts'] = projected_verdicts
    repaired_by_request_id: dict[str, dict] = {}
    for row in repaired_request_verdicts:
        if not isinstance(row, dict):
            continue
        request_id = str(row.get('request_id') or '').strip()
        if not request_id or request_id in repaired_by_request_id:
            return (None, 'result.primary_support_reconsideration_scope_invalid')
        repaired_by_request_id[request_id] = row
    base_request_ids = {str(row.get('request_id') or '').strip() for row in base_request_verdicts if isinstance(row, dict)}
    if set(repaired_by_request_id) != base_request_ids:
        return (None, 'result.primary_support_reconsideration_scope_invalid')
    projected_request_verdicts: list[object] = []
    for base_row in base_request_verdicts:
        if not isinstance(base_row, dict):
            projected_request_verdicts.append(copy.deepcopy(base_row))
            continue
        request_id = str(base_row.get('request_id') or '').strip()
        if request_id not in affected_request_ids:
            projected_request_verdicts.append(copy.deepcopy(base_row))
            continue
        repair_row = repaired_by_request_id[request_id]
        replacement = copy.deepcopy(base_row)
        for field in ('decision', 'resolution_basis', 'reason'):
            if field in repair_row:
                replacement[field] = copy.deepcopy(repair_row[field])
        projected_request_verdicts.append(replacement)
    projected['request_verdicts'] = projected_request_verdicts
    for field in ('decision', 'summary', 'player_reason'):
        if field in repaired:
            projected[field] = copy.deepcopy(repaired[field])
    return (projected, None)

def _review_validation_retry_mode(error_code: str | None) -> str | None:
    if not error_code:
        return 'structure'
    suffix = str(error_code).rsplit('.', 1)[-1]
    if suffix == _REVIEW_PRIMARY_SUPPORT_RECONSIDERATION_SUFFIX:
        return 'primary_support_reconsideration'
    if suffix in _REVIEW_ITEM_EVIDENCE_COMPLETION_SUFFIXES:
        return 'item_evidence_completion'
    if suffix in _REVIEW_NON_REPAIRABLE_VALIDATION_SUFFIXES:
        return None
    return 'structure'

@with_player_name_aliases
def _call_data_appeal_review_with_structure_retry(*, api_key: str, api_base: str, model: str, timeout: int, messages: list[dict], appeal_id: int, adventurer_id: int | None, normalized_patch: dict | None=None, primary_operation_manifest: list[dict] | None=None, primary_request_checklist: list[dict] | None=None, valid_request_ids: set[str] | None=None, snapshot_before: dict | None=None, narrative_event_count: int | None=None, consumption_restore_candidate_names: set[str] | None=None, runtime_metadata_out: dict[str, object] | None=None) -> tuple[dict | None, dict]:
    """调用二审；有界修复结构、单项证据或一审支持分歧。"""
    try:
        retry_limit = max(0, min(1, int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_REVIEW_STRUCTURE_RETRIES', 1))))
    except (TypeError, ValueError):
        retry_limit = 1
    request_messages = list(messages)
    item_evidence_completion_base: dict | None = None
    item_evidence_completion_error_code = ''
    primary_support_reconsideration_base: dict | None = None
    primary_support_reconsideration_error_code = ''
    attempts: list[dict] = []
    final_error_code = 'response_not_received'
    for response_attempt in range(retry_limit + 1):
        request_metadata: dict[str, str] = {}
        response = None
        usage = None
        attempt_number = response_attempt + 1
        runtime_model = str(request_metadata.get('model') or model)

        def validate_data_appeal_review_response(decoded) -> S1AiResponseContractResult:
            parsed_candidate = decoded.json_result.data
            projection_error = None
            if item_evidence_completion_base is not None and isinstance(parsed_candidate, dict):
                parsed_candidate, projection_error = _project_review_item_evidence_completion_candidate(item_evidence_completion_base, parsed_candidate, error_code=item_evidence_completion_error_code)
            elif primary_support_reconsideration_base is not None and isinstance(parsed_candidate, dict):
                parsed_candidate, projection_error = _project_review_primary_support_reconsideration_candidate(primary_support_reconsideration_base, parsed_candidate, error_code=primary_support_reconsideration_error_code, primary_request_checklist=primary_request_checklist)
            if projection_error is not None:
                return S1AiResponseContractResult.reject(projection_error)
            if parsed_candidate is None:
                return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'response_not_parseable', failure_stage='response_parse')
            validation_diagnostics: dict = {}
            result_candidate, validation_error = _validate_data_appeal_review_result_with_error(parsed_candidate, normalized_patch=normalized_patch, primary_operation_manifest=primary_operation_manifest, primary_request_checklist=primary_request_checklist, enforce_primary_support_consensus=primary_support_reconsideration_base is None, valid_request_ids=valid_request_ids, snapshot_before=snapshot_before, narrative_event_count=narrative_event_count, diagnostics_out=validation_diagnostics, consumption_restore_candidate_names=consumption_restore_candidate_names)
            if result_candidate is None:
                return S1AiResponseContractResult.reject(validation_error or 'result.review_contract_invalid', diagnostics=validation_diagnostics)
            return S1AiResponseContractResult.accept(result_candidate)
        try:
            response = post_s1_ai_request(f'{api_base}/chat/completions', feature='data_appeal_review', json_object_mode_if_supported=True, request_metadata_out=request_metadata, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': request_messages, 'temperature': 0.1}, timeout=timeout, response_validator=validate_data_appeal_review_response)
            decoded = parse_s1_ai_http_json_response(response, repair_mode='lossless_syntax_only')
            usage = decoded.http_result.usage
            decoded_content = decoded.json_result.content
            if runtime_metadata_out is not None and isinstance(decoded_content, str) and decoded_content.strip():
                runtime_metadata_out['positive_tokens_consumed'] = True
            if runtime_metadata_out is not None and runtime_metadata_out.get('positive_tokens_consumed') is not True and s1_ai_usage_has_positive_tokens(usage, request_metadata=request_metadata):
                runtime_metadata_out['positive_tokens_consumed'] = True
        except Exception as exc:
            if runtime_metadata_out is not None and runtime_metadata_out.get('positive_tokens_consumed') is not True and s1_ai_usage_has_positive_tokens(usage, request_metadata=request_metadata, request_exception=exc):
                runtime_metadata_out['positive_tokens_consumed'] = True
            record_token_usage(call_type='data_appeal_review_ai' if response_attempt == 0 else 'data_appeal_review_retry_ai', usage=usage, model=request_metadata.get('model') or model, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=attempt_number, is_success=False, failure_stage='http_or_response', validation_error_code=f'http_or_response.{type(exc).__name__}', record_without_usage=True, request_metadata=request_metadata, request_exception=exc)
            raise
        response_json = decoded.http_result.response_json or {}
        runtime_model = str(request_metadata.get('model') or model)
        runtime_metadata: dict[str, str] = dict(request_metadata)
        finish_reason = None
        content = ''
        diagnostic_content = ''
        validation_error_code = None
        error_code = 'response_processing_failed'
        repair_applied = response_attempt > 0
        try:
            runtime_metadata = enrich_s1_ai_response_metadata(response, response_json)
            runtime_model = str(runtime_metadata.get('model') or runtime_model)
            reasoning_content = extract_s1_ai_reasoning_content(response_json) or ''
            json_result = decoded.json_result
            content = json_result.content
            diagnostic_content = json_result.raw_excerpt if json_result.error_code == 'truncated_output' else content
            finish_reason = json_result.finish_reason
            repair_applied = json_result.repair_applied or response_attempt > 0
            parsed = json_result.data
            projection_error = None
            if item_evidence_completion_base is not None and isinstance(parsed, dict):
                parsed, projection_error = _project_review_item_evidence_completion_candidate(item_evidence_completion_base, parsed, error_code=item_evidence_completion_error_code)
            elif primary_support_reconsideration_base is not None and isinstance(parsed, dict):
                parsed, projection_error = _project_review_primary_support_reconsideration_candidate(primary_support_reconsideration_base, parsed, error_code=primary_support_reconsideration_error_code, primary_request_checklist=primary_request_checklist)
            result, validation_error_code = (None, projection_error) if projection_error is not None else _validate_data_appeal_review_result_with_error(parsed, normalized_patch=normalized_patch, primary_operation_manifest=primary_operation_manifest, primary_request_checklist=primary_request_checklist, enforce_primary_support_consensus=primary_support_reconsideration_base is None, valid_request_ids=valid_request_ids, snapshot_before=snapshot_before, narrative_event_count=narrative_event_count, consumption_restore_candidate_names=consumption_restore_candidate_names) if parsed is not None else (None, f"response.{json_result.error_code or 'invalid'}")
            if result is not None:
                error_code = None
            elif finish_reason == 'length':
                error_code = 'truncated_response'
            elif not content:
                error_code = 'empty_response'
            elif parsed is None and projection_error is None:
                error_code = 'json_parse_failed'
            else:
                error_code = 'result_validation_failed'
            attempts.append({'attempt_number': attempt_number, 'failure_stage': error_code, 'validation_error_code': validation_error_code, 'finish_reason': finish_reason, 'repair_applied': repair_applied, 'raw_content': diagnostic_content[:12000], 'reasoning_content': reasoning_content[:12000], 'runtime_metadata': runtime_metadata})
        except Exception as exc:
            record_token_usage(call_type='data_appeal_review_ai' if response_attempt == 0 else 'data_appeal_review_retry_ai', usage=usage, model=runtime_model, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=attempt_number, is_success=False, failure_stage='response_processing_failed', finish_reason=finish_reason, json_object_mode=_runtime_metadata_bool(runtime_metadata.get('json_object_mode')), provider_request_id=runtime_metadata.get('request_id'), repair_applied=repair_applied, failed_response_excerpt=build_s1_ai_raw_excerpt(diagnostic_content), record_without_usage=True, request_metadata=request_metadata, request_exception=exc)
            raise
        deferred_token_usage = None
        call_type = 'data_appeal_review_ai' if response_attempt == 0 else 'data_appeal_review_retry_ai'
        if result is not None:
            if usage:
                deferred_token_usage = _DeferredDataAppealReviewTokenUsage(call_type=call_type, usage=usage, model=runtime_model, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=attempt_number, finish_reason=finish_reason, json_object_mode=_runtime_metadata_bool(runtime_metadata.get('json_object_mode')), provider_request_id=runtime_metadata.get('request_id'), repair_applied=repair_applied, failed_response_excerpt=build_s1_ai_raw_excerpt(diagnostic_content))
        else:
            record_token_usage(call_type=call_type, usage=usage, model=runtime_model, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=attempt_number, is_success=False, failure_stage=error_code, validation_error_code=validation_error_code, finish_reason=finish_reason, json_object_mode=_runtime_metadata_bool(runtime_metadata.get('json_object_mode')), provider_request_id=runtime_metadata.get('request_id'), repair_applied=repair_applied, failed_response_excerpt=build_s1_ai_raw_excerpt(diagnostic_content), record_without_usage=True)
        log_s1_ai_response_outcome(response, feature='data_appeal_review', result_status='success' if result is not None else 'invalid_output', finish_reason=finish_reason, repair_applied=repair_applied, error_code=validation_error_code or error_code, attempt_number=attempt_number)
        if result is not None:
            return (result, {'attempts': attempts, 'contract_valid': True, 'deferred_token_usage': deferred_token_usage})
        final_error_code = error_code or 'result_validation_failed'
        retry_mode = 'structure' if finish_reason == 'length' else _review_validation_retry_mode(validation_error_code)
        if response_attempt >= retry_limit or final_error_code not in _REVIEW_RETRYABLE_STRUCTURE_FAILURES or retry_mode is None:
            break
        repair_reason_code = validation_error_code or final_error_code
        if retry_mode == 'item_evidence_completion':
            repair_rule = _build_review_item_evidence_completion_rule(repair_reason_code)
        elif retry_mode == 'primary_support_reconsideration':
            repair_rule = _build_review_primary_support_reconsideration_rule()
        else:
            repair_rule = _build_review_structure_repair_rule(repair_reason_code)
        if retry_mode == 'item_evidence_completion' and isinstance(parsed, dict):
            item_evidence_completion_base = copy.deepcopy(parsed)
            item_evidence_completion_error_code = repair_reason_code
        elif retry_mode == 'primary_support_reconsideration' and isinstance(parsed, dict):
            primary_support_reconsideration_base = copy.deepcopy(parsed)
            primary_support_reconsideration_error_code = repair_reason_code
        request_messages = list(messages)
        if content and finish_reason != 'length':
            request_messages.append({'role': 'assistant', 'content': content})
        if retry_mode == 'item_evidence_completion':
            repair_intro = '上一份输出的一条 operation 证据合同未闭合。'
            repair_preservation_rule = '除错误路径指向的 verdict 及其同一 atomic_group 外，保持所有 operation_id、request_ids、逐项裁决、证据字段与全部 patch 原样不变；只机械重算受这些 operation 影响的 request_verdict 和顶层 decision，summary/player_reason 仅在汇总结果改变时作最小一致性修正。'
        elif retry_mode == 'primary_support_reconsideration':
            repair_intro = '上一份输出与一审的可执行支持结论发生了整项分歧。'
            repair_preservation_rule = '只允许重核上一份输出中的全部此类 request、直接关联 operation 及其 atomic_group；所有 operation_id、request_ids、atomic_group_id 与全部 patch 冻结不变，其它独立 request、operation、证据字段和逐项裁决保持原样；按重核结果机械更新受影响 request_verdict 与顶层 decision，summary/player_reason 仅作最小一致性修正。'
        else:
            repair_intro = '上一份输出未通过结构校验。'
            repair_preservation_rule = '保持冻结输入、证据判断、各操作语义和逐项 allow/reject 不变；顶层 decision 只按 request_verdicts 与 verdicts 的协议不变量机械重算。'
        request_messages.append({'role': 'user', 'content': f'{repair_intro}reason_code={repair_reason_code}。repair_rule={repair_rule}。{repair_preservation_rule}按既定 JSON 协议重新输出完整对象。'})
    return (None, {'failure_stage': final_error_code, 'validation_error_code': validation_error_code, 'attempts': attempts})

def _validate_player_review_result(result: object) -> dict | None:
    if not isinstance(result, dict):
        return None
    result = {key: result[key] for key in ('decision', 'reason') if key in result}
    if frozenset(result) != frozenset({'decision', 'reason'}):
        return None
    decision = result.get('decision')
    reason = result.get('reason')
    if decision not in ('original_ai_correct', 'player_may_be_right'):
        return None
    if not isinstance(reason, str) or not reason.strip() or (not _CHINESE_TEXT_RE.search(reason)) or (len(reason.strip()) > 200):
        return None
    return {'decision': decision, 'reason': reason.strip()}

def _validate_revert_review_result(result: object) -> dict | None:
    if not isinstance(result, dict):
        return None
    result = {key: result[key] for key in ('decision', 'reason') if key in result}
    if frozenset(result) != frozenset({'decision', 'reason'}):
        return None
    decision = result.get('decision')
    reason = result.get('reason')
    if decision not in ('allow', 'reject'):
        return None
    if not isinstance(reason, str) or not reason.strip() or (not _CHINESE_TEXT_RE.search(reason)) or (len(reason.strip()) > 120):
        return None
    return {'decision': decision, 'reason': reason.strip()}

@with_player_name_aliases
def call_player_data_appeal_review_request(appeal_id: int, *, player_request: str, appeal_context: dict) -> dict | None:
    """Ask MIMO to review a player's follow-up request; never expose model details to callers."""
    try:
        api_key, api_base, model, timeout = get_mimo_compatible_client_config('data_appeal_player_review')
    except AIProfileAssignmentError as exc:
        logger.warning('玩家数据申诉独立复核用途路由不可用: %s', exc)
        return {'decision': 'error', 'reason': '独立复核服务暂时不可用，请稍后再试。', 'internal_error': str(exc)}
    if not api_key or not api_base:
        logger.warning('玩家数据申诉独立复核跳过：API key 或 base URL 未配置')
        return {'decision': 'error', 'reason': '独立复核服务暂时不可用，请稍后再试。', 'internal_error': '独立复核 API key 或 base URL 未配置'}
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    from plugins.adventurer_s1.services.s1_appeal_rarity_restore_service import EVIDENCE_KEY as rarity_restore_key, project_candidates_for_ai
    context_for_ai = copy.deepcopy(appeal_context)
    context_evidence = context_for_ai.get('expedition_evidence')
    if isinstance(context_evidence, dict):
        context_evidence, _ = AdventurerS1DataAppealService.prepare_expedition_evidence_for_ai(context_evidence)
        context_for_ai['expedition_evidence'] = context_evidence
    if isinstance(context_evidence, dict) and rarity_restore_key in context_evidence:
        context_evidence[rarity_restore_key] = project_candidates_for_ai(context_evidence[rarity_restore_key] or [])
    context_for_ai = AdventurerS1DataAppealService.compact_for_ai_payload(context_for_ai)
    from plugins.adventurer_s1.services.s1_appeal_evolution_evidence_service import preserve_histories_after_compaction
    context_for_ai = preserve_histories_after_compaction(context_for_ai, appeal_context, record_blocks=('snapshot_before', 'all_player_records'))
    for key in ('ai_raw_response', 'review_ai_raw_response'):
        if key in context_for_ai:
            context_for_ai[key] = decode_appeal_response_evidence(context_for_ai[key])
    stable_payload = pack_appeal_description_inputs({'appeal': context_for_ai})
    player_review_prompt = _PLAYER_REVIEW_SYSTEM_PROMPT
    if context_for_ai.get('appeal_category') == 'data_fix':
        player_review_prompt += _DATA_FIX_REVIEW_ADDENDUM
    task_payload = {'player_request': player_request}
    usage = None
    runtime_model = model
    runtime_metadata: dict[str, str] = {}
    request_metadata: dict[str, str] = {}
    request_exception: BaseException | None = None
    adventurer_id = None
    is_success = False
    failure_stage = 'response_not_received'
    validation_error_code = None
    finish_reason = None
    repair_applied = False

    def validate_player_review_response(decoded) -> S1AiResponseContractResult:
        result = _validate_player_review_result(decoded.json_result.data)
        if result is None:
            return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'result.player_review_contract_invalid')
        return S1AiResponseContractResult.accept(result)
    try:
        db_manager.remove()
        response = post_s1_ai_request(f'{api_base}/chat/completions', feature='data_appeal_player_review', capability='data_appeal_player_review', json_object_mode_if_supported=True, request_metadata_out=request_metadata, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'system', 'content': player_review_prompt}, {'role': 'user', 'content': json.dumps(stable_payload, ensure_ascii=False, separators=(',', ':'))}, {'role': 'user', 'content': json.dumps(task_payload, ensure_ascii=False, separators=(',', ':'))}], 'temperature': 0.1}, timeout=timeout, response_validator=validate_player_review_response)
        decoded = parse_s1_ai_http_json_response(response, repair_mode='lossless_syntax_only')
        response_json = decoded.http_result.response_json or {}
        usage = decoded.http_result.usage
        runtime_metadata = enrich_s1_ai_response_metadata(response, response_json)
        runtime_model = str(runtime_metadata.get('model') or model)
        json_result = decoded.json_result
        finish_reason = json_result.finish_reason
        repair_applied = json_result.repair_applied
        if json_result.data is None:
            failure_stage = 'json_parse_failed'
            validation_error_code = f"response.{json_result.error_code or 'invalid'}"
            raise ValueError('MIMO 未返回有效 JSON')
        result = _validate_player_review_result(json_result.data)
        if result is None:
            failure_stage = 'result_validation_failed'
            validation_error_code = 'result.player_review_contract_invalid'
            raise ValueError('MIMO 未返回有效 JSON')
        if isinstance(appeal_context, dict):
            raw_adv_id = appeal_context.get('adventurer_id')
            if raw_adv_id is not None:
                adventurer_id = int(raw_adv_id)
        is_success = True
        failure_stage = None
        return result
    except Exception as exc:
        request_exception = exc
        if failure_stage == 'response_not_received':
            failure_stage = 'http_or_response'
        logger.warning('玩家数据申诉 MIMO 复核失败 appeal=%s: %s', appeal_id, exc)
        return {'decision': 'error', 'reason': '独立复核服务暂时不可用，请稍后再试。', 'internal_error': str(exc).replace(api_base, '[独立复核接口]').replace(model, '[独立复核模型]')[:500]}
    finally:
        if usage or request_exception is not None:
            record_token_usage(call_type='data_appeal_review_ai', usage=usage, model=runtime_model, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=1, is_success=is_success, failure_stage=failure_stage, validation_error_code=validation_error_code, finish_reason=finish_reason, json_object_mode=_runtime_metadata_bool(runtime_metadata.get('json_object_mode')), provider_request_id=runtime_metadata.get('request_id'), repair_applied=repair_applied, record_without_usage=request_exception is not None, request_metadata=request_metadata, request_exception=request_exception)

@with_player_name_aliases
def call_data_appeal_review_ai(appeal_id: int, *, player_note: str, snapshot_before: dict, appeal_adventurer: dict | None=None, related_records: dict | None=None, lookup_results: list | None=None, recent_activity: list | None=None, expedition_evidence: dict | None, deepseek_response: dict, normalized_patch: dict | None, normalization_error: str | None, prior_appeal_grants: list | None=None, party_roster: list | None=None, team_pool_status: dict | None=None, appeal_fact_sheet: dict | None=None, appeal_category: str='other', runtime_metadata_out: dict[str, object] | None=None) -> dict | None:
    """Review the DS result with MIMO; return parsed JSON or None on any failure."""
    if runtime_metadata_out is not None:
        runtime_metadata_out['positive_tokens_consumed'] = False
    try:
        api_key, api_base, model, timeout = get_mimo_compatible_client_config('data_appeal_review')
    except AIProfileAssignmentError as exc:
        logger.warning('数据申诉独立复核用途路由不可用: %s', exc)
        return {'verdict_contract_version': DATA_APPEAL_OPERATION_CONTRACT_VERSION, 'decision': 'error', 'summary': '独立复核 AI 路由未配置', 'internal_error': str(exc)}
    if not api_key or not api_base:
        logger.warning('数据申诉独立复核跳过：API key 或 base URL 未配置')
        return {'verdict_contract_version': DATA_APPEAL_OPERATION_CONTRACT_VERSION, 'decision': 'error', 'summary': '独立复核 AI 配置缺失', 'internal_error': '独立复核 API key 或 base URL 未配置'}
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    codename_mapping: dict[str, str] = {}
    if expedition_evidence:
        expedition_evidence, codename_mapping = AdventurerS1DataAppealService.prepare_expedition_evidence_for_ai(expedition_evidence)
        if codename_mapping:
            player_note = AdventurerS1DataAppealService.restore_codenames_in_appeal_narrative(player_note, codename_mapping)
    narrative_event_count = len(expedition_evidence.get('all_events') or []) if isinstance(expedition_evidence, dict) else 0
    evidence_payload = expedition_evidence
    payload = {}
    split_evidence_payload = {}
    if appeal_fact_sheet:
        split_evidence_payload['appeal_fact_sheet'] = appeal_fact_sheet
    if party_roster:
        split_evidence_payload['party_roster'] = party_roster
    if evidence_payload:
        split_evidence_payload['expedition_evidence'] = evidence_payload
    if appeal_adventurer:
        payload['appeal_adventurer'] = appeal_adventurer
    payload['appeal_category'] = appeal_category
    payload['selected_records'] = snapshot_before
    payload['related_records'] = related_records or snapshot_before
    if lookup_results:
        payload['lookup_results'] = lookup_results
    if prior_appeal_grants:
        payload['prior_appeal_grants'] = prior_appeal_grants
    if recent_activity:
        payload['recent_activity'] = recent_activity
    if team_pool_status:
        pool_status = dict(team_pool_status)
        if pool_status.get('settled'):
            pool_status = {'settled': True, **{k: v for k, v in pool_status.items() if k != 'settled' and v}}
        else:
            pool_status = {k: v for k, v in pool_status.items() if k != 'settled' and v}
        if pool_status:
            payload['team_pool_status'] = pool_status
    payload['player_note'] = player_note
    slim_primary_response = AdventurerS1DataAppealService.build_slim_primary_ai_response_for_review(deepseek_response)
    payload['deepseek_response'] = slim_primary_response
    payload['backend_normalized_patch'] = normalized_patch
    consumption_restore_candidate_names = {str(row.get('name') or '').strip() for row in (evidence_payload or {}).get('departure_consumption_candidates') or [] if isinstance(row, dict) and str(row.get('name') or '').strip()}
    primary_operation_manifest = build_data_appeal_operation_manifest(normalized_patch, source='primary', snapshot_before=snapshot_before, consumption_restore_candidate_names=consumption_restore_candidate_names)
    payload['operation_manifest'] = primary_operation_manifest
    valid_request_ids = {str(row.get('request_id') or '').strip() for row in slim_primary_response.get('request_checklist') or [] if isinstance(row, dict) and re.fullmatch('request_[1-8]', str(row.get('request_id') or '').strip())}
    if normalization_error:
        payload['backend_normalization_error'] = normalization_error
    payload = AdventurerS1DataAppealService.prepare_appeal_ai_user_payload(payload)
    if split_evidence_payload:
        split_evidence_payload = AdventurerS1DataAppealService.prepare_appeal_ai_user_payload(split_evidence_payload)
    payload['record_comparisons'] = build_appeal_record_comparisons(normalized_patch, snapshot_before)
    destructive_action_max = get_data_appeal_destructive_action_max()
    task_prompt = (_REVIEW_DATA_FIX_SYSTEM_PROMPT if appeal_category == 'data_fix' else _REVIEW_SYSTEM_PROMPT).replace('<DATA_APPEAL_DESTRUCTIVE_ACTION_MAX>', str(destructive_action_max))
    if appeal_category != 'data_fix':
        task_prompt += _EQUIPMENT_DESCRIPTION_INHERITANCE_REVIEW_ADDENDUM
    shared_system_prompt = FREE_APPEAL_COMMON + S1_DATA_APPEAL_PLAYER_DIRECTED_CONTENT_RULES + _APPEAL_CODENAME_ADDENDUM + S1_APPEAL_DESCRIPTION_INPUT_RULE + ''
    task_user_prompt = '【本次复核输入】\n' + json.dumps(pack_appeal_description_inputs(payload), ensure_ascii=False, separators=(',', ':'))
    shared_evidence_prefix_enabled = current_app.config.get(S1_DATA_APPEAL_SHARED_EVIDENCE_PREFIX_CONFIG, True) is True
    messages = build_s1_data_appeal_messages(shared_system_prompt=shared_system_prompt, task_system_prompt=task_prompt, shared_evidence_payload=split_evidence_payload, task_user_prompt=task_user_prompt, shared_evidence_prefix_enabled=shared_evidence_prefix_enabled)
    if shared_evidence_prefix_enabled and split_evidence_payload:
        log_s1_data_appeal_cache_diagnostics(logger, appeal_id=appeal_id, stage='second_review', shared_system_prompt=shared_system_prompt, shared_evidence_payload=split_evidence_payload, shared_evidence_content=messages[1]['content'])
    deferred_token_usage = None
    usage_handed_off = False
    try:
        db_manager.remove()
        adventurer_id = None
        if isinstance(appeal_adventurer, dict):
            raw_adv_id = appeal_adventurer.get('adventurer_id') or appeal_adventurer.get('id')
            if raw_adv_id is not None:
                adventurer_id = int(raw_adv_id)
        result, diagnostics = _call_data_appeal_review_with_structure_retry(api_key=api_key, api_base=api_base, model=model, timeout=timeout, messages=messages, appeal_id=appeal_id, adventurer_id=adventurer_id, normalized_patch=normalized_patch, primary_operation_manifest=primary_operation_manifest, primary_request_checklist=slim_primary_response.get('request_checklist') or [], valid_request_ids=valid_request_ids, snapshot_before=snapshot_before, narrative_event_count=narrative_event_count, consumption_restore_candidate_names=consumption_restore_candidate_names, runtime_metadata_out=runtime_metadata_out)
        deferred_token_usage = diagnostics.get('deferred_token_usage')
        attempts = diagnostics.get('attempts') or []
        if runtime_metadata_out is not None and attempts:
            last_attempt = attempts[-1] if isinstance(attempts[-1], dict) else {}
            runtime_metadata = last_attempt.get('runtime_metadata')
            if isinstance(runtime_metadata, dict):
                runtime_metadata_out.update({key: str(runtime_metadata[key]) for key in ('profile_id', 'provider', 'model') if runtime_metadata.get(key)})
        if result is None:
            return {'verdict_contract_version': DATA_APPEAL_OPERATION_CONTRACT_VERSION, 'decision': 'error', 'summary': '独立复核输出结构无效', 'failure_stage': diagnostics.get('failure_stage'), 'validation_error_code': diagnostics.get('validation_error_code'), 'attempts': diagnostics.get('attempts') or [], 'internal_error': '独立复核在结构修复重试后仍未返回有效结果'}
        if codename_mapping:
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
            AdventurerS1DataAppealService.restore_codenames_in_appeal_ai_response(result, codename_mapping)
        if len(attempts) > 1:
            result['_structure_retry_trace'] = attempts
        if result.get('verdict_contract_version') == DATA_APPEAL_OPERATION_CONTRACT_VERSION:
            result['_backend_operation_manifest'] = [*primary_operation_manifest, *build_data_appeal_operation_manifest(result.get('supplement_patch'), source='supplement', snapshot_before=snapshot_before, consumption_restore_candidate_names=consumption_restore_candidate_names), *build_data_appeal_operation_manifest(result.get('override_patch'), source='override', snapshot_before=snapshot_before, consumption_restore_candidate_names=consumption_restore_candidate_names), *build_data_appeal_operation_manifest(None, source='clear', snapshot_before=snapshot_before, clear_fields=result.get('clear_fields'))]
        if runtime_metadata_out is not None and deferred_token_usage is not None:
            runtime_metadata_out['deferred_token_usage'] = deferred_token_usage
            runtime_metadata_out['contract_valid'] = diagnostics.get('contract_valid') is True
            usage_handed_off = True
        return result
    except Exception as exc:
        logger.warning('数据申诉 MIMO 复核失败 appeal=%s: %s', appeal_id, exc)
        return {'verdict_contract_version': DATA_APPEAL_OPERATION_CONTRACT_VERSION, 'decision': 'error', 'summary': '独立复核 AI 调用失败', 'failure_stage': 'http_or_response', 'internal_error': str(exc).replace(api_base, '[独立复核接口]').replace(model, '[独立复核模型]')[:500]}
    finally:
        if deferred_token_usage is not None and (not usage_handed_off):
            deferred_token_usage.finalize(is_success=False)
_REVERT_REQUEST_SYSTEM_PROMPT = ''
_REVERT_REQUEST_SYSTEM_PROMPT += FREE_APPEAL_REVERT

@with_player_name_aliases
def call_data_appeal_revert_request_ai(target_appeal_id: int, *, player_revert_note: str, appeal_context: dict, audit_changes: list[dict], later_conflicts: list[dict] | None=None) -> dict | None:
    """AI 裁决玩家「撤回此次申诉修复」申请。"""
    try:
        api_key, api_base, model, timeout = get_mimo_compatible_client_config('data_appeal_player_review')
    except AIProfileAssignmentError as exc:
        logger.warning('玩家撤回申诉 AI 用途路由不可用: %s', exc)
        return {'decision': 'error', 'reason': '撤回审核服务暂时不可用，请稍后再试。', 'internal_error': str(exc)}
    if not api_key or not api_base:
        logger.warning('玩家撤回申诉 AI 跳过：API key 或 base URL 未配置')
        return {'decision': 'error', 'reason': '撤回审核服务暂时不可用，请稍后再试。', 'internal_error': '撤回审核 API key 或 base URL 未配置'}
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    stable_payload = {'target_appeal_id': target_appeal_id, 'target_appeal': AdventurerS1DataAppealService.compact_for_ai_payload(appeal_context), 'audit_changes': audit_changes[:30], 'later_conflicts': (later_conflicts or [])[:10]}
    task_payload = {'player_revert_note': player_revert_note}
    usage = None
    runtime_model = model
    runtime_metadata: dict[str, str] = {}
    request_metadata: dict[str, str] = {}
    request_exception: BaseException | None = None
    adventurer_id = None
    is_success = False
    failure_stage = 'response_not_received'
    validation_error_code = None
    finish_reason = None
    repair_applied = False

    def validate_revert_review_response(decoded) -> S1AiResponseContractResult:
        result = _validate_revert_review_result(decoded.json_result.data)
        if result is None:
            return S1AiResponseContractResult.reject(decoded.json_result.error_code or 'result.revert_review_contract_invalid')
        return S1AiResponseContractResult.accept(result)
    try:
        db_manager.remove()
        response = post_s1_ai_request(f'{api_base}/chat/completions', feature='data_appeal_player_review', capability='data_appeal_revert_review', json_object_mode_if_supported=True, request_metadata_out=request_metadata, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json={'model': model, 'messages': [{'role': 'system', 'content': _REVERT_REQUEST_SYSTEM_PROMPT}, {'role': 'user', 'content': json.dumps(stable_payload, ensure_ascii=False, separators=(',', ':'))}, {'role': 'user', 'content': json.dumps(task_payload, ensure_ascii=False, separators=(',', ':'))}], 'temperature': 0.1}, timeout=timeout, response_validator=validate_revert_review_response)
        decoded = parse_s1_ai_http_json_response(response, repair_mode='lossless_syntax_only')
        response_json = decoded.http_result.response_json or {}
        usage = decoded.http_result.usage
        runtime_metadata = enrich_s1_ai_response_metadata(response, response_json)
        runtime_model = str(runtime_metadata.get('model') or model)
        json_result = decoded.json_result
        finish_reason = json_result.finish_reason
        repair_applied = json_result.repair_applied
        if json_result.data is None:
            failure_stage = 'json_parse_failed'
            validation_error_code = f"response.{json_result.error_code or 'invalid'}"
            raise ValueError('MIMO 未返回有效 JSON')
        result = _validate_revert_review_result(json_result.data)
        if result is None:
            failure_stage = 'result_validation_failed'
            validation_error_code = 'result.revert_review_contract_invalid'
            raise ValueError('MIMO 未返回有效 JSON')
        if isinstance(appeal_context, dict):
            raw_adv_id = appeal_context.get('adventurer_id')
            if raw_adv_id is not None:
                adventurer_id = int(raw_adv_id)
        is_success = True
        failure_stage = None
        return result
    except Exception as exc:
        request_exception = exc
        if failure_stage == 'response_not_received':
            failure_stage = 'http_or_response'
        logger.warning('玩家撤回申诉 AI 失败 appeal=%s: %s', target_appeal_id, exc)
        return {'decision': 'error', 'reason': '撤回审核服务暂时不可用，请稍后再试。', 'internal_error': str(exc).replace(api_base, '[撤回审核接口]').replace(model, '[撤回审核模型]')[:500]}
    finally:
        if usage or request_exception is not None:
            record_token_usage(call_type='data_appeal_revert_request_ai', usage=usage, model=runtime_model, appeal_id=target_appeal_id, adventurer_id=adventurer_id, attempt_number=1, is_success=is_success, failure_stage=failure_stage, validation_error_code=validation_error_code, finish_reason=finish_reason, json_object_mode=_runtime_metadata_bool(runtime_metadata.get('json_object_mode')), provider_request_id=runtime_metadata.get('request_id'), repair_applied=repair_applied, record_without_usage=request_exception is not None, request_metadata=request_metadata, request_exception=request_exception)
