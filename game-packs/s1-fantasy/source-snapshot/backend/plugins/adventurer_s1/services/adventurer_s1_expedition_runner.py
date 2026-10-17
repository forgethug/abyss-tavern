"""深渊酒馆 S1 探险引擎：主 AI 跑团 + 子表结算写回 + 职业授予 + 日志分批推进。

跳过①行囊整理与②商店购物：成员带现有背包直接进主 AI 跑团。

与原版最大的不同是**结算写回**：原版把 traits/skills/equipment 存成 Adventurer 行内
JSON 直接改；S1 是规范化子表（`AdventurerS1Trait/Skill/Equipment/WarehouseItem`），
这里把写回重写为子表的增删改。AI 返回字段的 normalize 复用原版纯函数。

已对齐 legacy 的两段机制（2026-06-10），并在 S1 层做专属增强：
- **特殊事件**：出发时按 S1 概率曲线掷骰；极端危机细分 abyss/stranger/unbeatable_horror
  沿用 legacy 分布；存量字段继续兼容，S1 AI 只接收统一的特殊事件类别，额外注入
  风险档与本地生态/世界事实联动规则，经验倍率按 S1 风险档重标定。
- **分批推进**：`progress_s1_expedition` 每次只放出一批事件（步长公式同 legacy：
  剩余事件/剩余推送次数 向上取整，最多 `ADVENTURER_S1_LOG_MAX_PROGRESS_PUSHES` 批），
  推完最后一批才结算；`progress_active_expeditions` 由 scheduler 周期驱动。

危险度分级仍用 S1 简化版（等级差定档，见 prompt service）。经验结算累计并自动升级
（上限 `ADVENTURER_S1_MAX_LEVEL`），地牢档位用 S1 专属的 `S1_DUNGEONS`。
"""
from runtime_core.time import utc_now as _portable_utc_now
import json
import logging
import random
import re
import requests
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timedelta
from enum import Enum
from hashlib import sha256
from types import SimpleNamespace
from typing import Callable, Literal
from uuid import uuid4
from flask import current_app
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tenacity import Retrying, RetryError, stop_after_attempt, wait_exponential_jitter, retry_if_exception
from db_repository import ConcurrencyError, db_manager
from db_repository.decorators import monitor_performance
from db_repository.postgresql_advisory_lock import acquire_postgresql_advisory_lock
from utils.backend_boot import get_async_worker_boot_id
from plugins.adventurer_s1.services.s1_campaign_world_service import campaign_world
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import AIProfileAssignmentError, S1AiCallBlockedError, S1AiRequestDeadlineExceeded, enrich_s1_ai_response_metadata, get_s1_ai_response_metadata, is_s1_ai_retryable_request_exception, log_s1_ai_response_outcome, post_s1_ai_request, resolve_s1_ai_profile_snapshot
from plugins.adventurer_s1.services.s1_settlement_field_projection import project_s1_member_settlement_fields, project_s1_settlement_fields
from plugins.adventurer_s1.services.s1_expedition_ai_timeout_contract import S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS
from plugins.adventurer_s1.services.s1_expedition_resolution_contract import S1_EXPEDITION_RESOLUTION_LEDGER_HARD_LIMIT, S1_EXPEDITION_RESOLUTION_MAX_ALLOCATIONS_PER_CHANGE, S1_EXPEDITION_RESOLUTION_MAX_CHANGES_PER_MEMBER, S1_EXPEDITION_RESOLUTION_MAX_ESTABLISHED_SOURCES, S1_EXPEDITION_RESOLUTION_MAX_EVENT_FACTS, S1_EXPEDITION_RESOLUTION_MAX_MEMBERS
from plugins.adventurer_s1.services.s1_ai_failure_evidence import get_s1_ai_failure_evidence
from plugins.adventurer_s1.services.s1_ai_input_budget import S1AiInputBudgetError
from plugins.adventurer_s1.services.s1_ai_json_response_service import S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY, S1AiJsonResult, S1AiResponseContractResult, apply_s1_ai_field_schema, build_s1_ai_raw_excerpt, decode_s1_ai_http_response, extract_s1_ai_reasoning_first_sentence, extract_s1_ai_structured_refusal, parse_s1_ai_http_json_response, parse_s1_ai_json_content
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_integer, normalize_exact_boolean
from plugins.adventurer_s1.services.s1_capability_profile_contract import ACTOR_CAPABILITY_FIELD_SCHEMA, CAPABILITY_AXIS_KEYS, CAPABILITY_CONDITIONS, CAPABILITY_NUMBER_PRESSURES, CAPABILITY_RANKS, normalize_actor_capability_profile
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Party, AdventurerS1ExpeditionLog, AdventurerS1Item, AdventurerS1Trait, AdventurerS1Skill, AdventurerS1Equipment, AdventurerS1WarehouseItem, AdventurerS1ItemLog, AdventurerS1Relationship, AdventurerS1PartyMember
logger = logging.getLogger(__name__)
_S1_EXPEDITION_PROGRESS_JOB_LOCK = threading.Lock()
_s1_expedition_progress_cursor = 0
_S1_EXPEDITION_RECOVERY_JOB_LOCK = threading.Lock()
_s1_expedition_recovery_cursor = 0
from plugins.adventurer_s1.services.s1_item_quantity_service import coerce_item_quantity, item_quantity_of, split_stack_suffix
from plugins.adventurer_s1.services import s1_consumed_asset_continuity_contract
from plugins.adventurer_s1.services.adventurer_s1_prompt_service import anonymize_s1_prompt_member_names, build_codename_to_real_from_alias, build_s1_expedition_death_resource_structure_repair_system_prompt, build_s1_expedition_death_resource_review_system_prompt, build_s1_expedition_event_resolution_system_prompt, build_s1_expedition_narrative_seed, build_s1_expedition_prompt_receipt, build_s1_expedition_prompts, build_s1_expedition_resource_review_input_prompt, build_s1_expedition_resolution_input_prompt, build_s1_expedition_outline_prompts
from plugins.adventurer_s1.services.adventurer_s1_expedition_log_member_service import refresh_expedition_log_members_for_log
from plugins.adventurer_s1.services.s1_trait_description_ai_service import parse_member_removed_trait_names, parse_member_new_trait_updates, resolve_removed_trait_db_name, sync_trait_description_fields
from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import normalize_persisted_trait_asset_dependencies, resolve_trait_asset_dependencies_for_write, sync_trait_asset_renames
from plugins.adventurer_s1.services.s1_description_ai_service import build_member_description_ai_context, build_frozen_member_description_projection_receipt, description_source_hash, enqueue_expedition_result_description_compressions, enqueue_member_description_prewarm, get_expedition_description_projection, refresh_frozen_member_description_ai_context, restore_frozen_member_description_projection
from plugins.adventurer_s1.services.s1_asset_description_update_ai_service import S1_ASSET_DESCRIPTION_UPDATE_RECEIPT_FIELD, finalize_s1_existing_asset_descriptions, persist_s1_asset_description_update_summaries, resolve_s1_finalized_asset_description
from plugins.adventurer_s1.services.s1_asset_description_merge_service import rebase_s1_concurrent_public_description
from plugins.adventurer_s1.services.s1_fate_destructive_change_contract import FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD, FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD, final_fate_destructive_approved_asset_ids, final_fate_destructive_owned_paths, final_fate_destructive_predecessor_ids, final_fate_destructive_change_requires_review, prepare_final_fate_destructive_context, quarantine_unexecutable_final_fate_destructive_changes, validate_final_fate_destructive_settlement
from plugins.adventurer_s1.services.s1_persistent_content_safety_service import PERSISTENT_CONTENT_REGENERATION_BASIS, apply_persistent_content_disposition, build_persistent_asset_state, persistent_asset_state_hash, persistent_content_exclusion_refs
from plugins.adventurer_s1.services.s1_observer_retry_policy import S1ObserverRegenerationTarget, decide_s1_observer_regeneration
from plugins.adventurer_s1.services.adventurer_s1_parser_service import S1_NEW_EQUIPMENT_LIST_REPAIR_CODE, S1_MEMBER_CODENAME_ANCHOR_ERROR_MARKER, S1_MEMBER_HISTORY_ANCHOR_ERROR_MARKER, matching_s1_history_anchor_repairs, unresolved_s1_history_anchor_errors, diagnose_s1_expedition_response_parse_failure, get_s1_expedition_parse_retry_feedback, normalize_s1_expedition_response_format, parse_s1_expedition_response, validate_s1_expedition_codename_anchors, validate_s1_expedition_result, build_s1_retry_validation_feedback, _normalize_member_used_name_list, clamp_s1_soft_fields, get_s1_new_equipment_quantity_repair_errors, get_s1_observer_repair_errors, normalize_s1_member_updates_by_id, normalize_s1_consumed_equipment_payload, normalize_s1_new_equipment_payload
from plugins.adventurer_s1.services.s1_gold_settlement_contract import S1_GOLD_SETTLEMENT_COMPONENTS_FIELD, S1_GOLD_SETTLEMENT_CONTRACT_VERSION, S1_GOLD_SETTLEMENT_FIELD, S1_GOLD_SETTLEMENT_VERSION_FIELD, materialize_s1_gold_settlement, normalize_s1_gold_settlement, normalize_s1_gold_settlement_value, rebase_s1_gold_settlement_for_log
from plugins.adventurer.services.adventurer_config_service import calculate_expedition_exp_gain, get_adventurer_exp_required
from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_DUNGEONS, get_s1_dungeon_level_range, get_s1_dungeon_info, get_s1_special_dungeon, is_s1_special_dungeon, is_s1_independent_dungeon, compute_s1_special_event_probability, classify_s1_special_event_risk, get_s1_special_event_exp_multiplier, resolve_s1_special_event_kind, resolve_s1_special_event_theme, get_s1_special_event_warning, requires_s1_high_risk_adjudication, get_s1_special_core_level_reference
from plugins.adventurer_s1.services.s1_hidden_boss_service import S1HiddenBossService
from plugins.adventurer_s1.services.s1_hidden_dungeon_service import HIDDEN_DUNGEON_REWARD_PLAN_KEY, build_hidden_dungeon_prompt_block, roll_hidden_dungeon, settle_hidden_dungeon_rewards
from plugins.adventurer_s1.services.s1_commission_service import S1CommissionService
from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import objective_actor_revisions
from plugins.adventurer_s1.services.s1_world_outline_contract import FACTION_PRESENCE_RECEIPTS_FIELD
from plugins.adventurer.services.adventurer_parser_service import _normalize_new_skills_payload, _normalize_removed_name_list_payload, _normalize_member_update_payload, _split_structured_sections, _strip_markdown_code_fence
from plugins.adventurer.services.adventurer_log_service import _normalize_member_new_traits_payload

class _S1ExpeditionGenerationClaimLost(ConcurrencyError):
    """The worker no longer owns the persisted expedition-generation lease."""

class S1RetryAction(str, Enum):
    """稳定的探险 AI 失败处置分类，供管理员审计与日志聚合。"""
    RETRY_SAME_STAGE = 'retry_same_stage'
    REPAIR_LOCAL = 'repair_local'
    REPAIR_BY_OBSERVER = 'repair_by_observer'
    RETRY_OBSERVER = 'retry_observer'
    RETRY_GUARD = 'retry_guard'
    RETRY_LOCATION_REVIEW = 'retry_location_review'
    RETRY_DEATH_RESOURCE_REVIEW = 'retry_death_resource_review'
    DEFER_AUXILIARY_STATE = 'defer_auxiliary_state'
    REGENERATE_RESOLUTION = 'regenerate_resolution'
    REGENERATE_MAIN = 'regenerate_main'
    REJECT_REGENERATION = 'reject_regeneration'
    QUARANTINE_UNREVIEWED_STATE = 'quarantine_unreviewed_state'
    VALIDATE_ORIGINAL_AFTER_REVIEW_FAILURE = 'validate_original_after_review_failure'
    RELEASE_STORY_AFTER_REVIEW_FAILURE = 'release_story_after_review_failure'
    FAIL_AND_DISBAND = 'fail_and_disband'
    RETURN_STAGE_FAILURE = 'return_stage_failure'
_S1_RETRY_TRACE_MAX_ENTRIES = 50
_S1_REVIEW_FAILURE_QUARANTINE_REASON_CODE = 'required_semantic_review_infra_exhausted_quarantined'
_S1_REVIEW_FAILURE_RESPONSE_PARSE_QUARANTINE_REASON_CODE = 'required_semantic_review_response_parse_repair_exhausted_quarantined'
_S1_OBSERVER_RELEASE_AUTHORIZED_QUARANTINE_REASON_CODE = 'observer_release_authorized_quarantined'
_S1_OBSERVER_SEMANTIC_REJECTION_REASON_CODE = 'observer_semantic_verdict_cannot_pass'
_S1_NEW_EQUIPMENT_QUANTITY_REPAIR_QUARANTINE_REASON_CODE = 'new_equipment_quantity_repair_exhausted_quarantined'
_S1_REVIEW_FAILURE_QUARANTINE_RECEIPT_FIELD = '_review_failure_quarantine_receipt'
_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD = '_same_name_gain_review_receipt'
_S1_REVIEW_FAILURE_RELEASE_REASON_CODES = {'observer_error_original_result_pending_validation': 'observer_error_original_result_released', 'settlement_guard_error_original_result_pending_validation': 'settlement_guard_error_original_result_released', _S1_OBSERVER_RELEASE_AUTHORIZED_QUARANTINE_REASON_CODE: 'observer_release_authorized', _S1_REVIEW_FAILURE_QUARANTINE_REASON_CODE: 'required_semantic_review_infra_exhausted', _S1_REVIEW_FAILURE_RESPONSE_PARSE_QUARANTINE_REASON_CODE: 'required_semantic_review_response_parse_repair_exhausted', _S1_NEW_EQUIPMENT_QUANTITY_REPAIR_QUARANTINE_REASON_CODE: 'new_equipment_quantity_repair_exhausted'}
_S1_ORDINARY_PHYSICAL_LOOT_RELEASE_REASON_CODE = 'ordinary_physical_loot_preserved_after_observer_failure'
_S1_UNEXPECTED_CHECKPOINT_CONSUMER_STAGE = 'expedition_checkpoint_consumer'
_S1_UNEXPECTED_CHECKPOINT_CONSUMER_REASON_PREFIX = 'unexpected_consumer_exception'
_S1_GENERATION_INFRASTRUCTURE_STAGE = 'expedition_generation_infrastructure'
_S1_GENERATION_INFRASTRUCTURE_REASON_PREFIX = 'generation_infrastructure_exception'
_WAITING_NPC_REVIEW_DISPOSITION_LOG_FIELD = '_waiting_npc_review_disposition'
_S1_GENERATION_INFRASTRUCTURE_RESUME_STAGES = frozenset({'objective_outline_ai', 'event_resolution_ai', 'main_ai'})
_S1_RETRY_FAILURE_CLASSIFICATION_BY_REASON = {'s1_ai_input_budget_exceeded': ('input_budget', 's1_ai_input_budget_exceeded'), 's1_ai_input_budget_invalid': ('input_budget', 's1_ai_input_budget_invalid'), 'expedition_input_budget_exceeded': ('input_budget', 'expedition_input_budget_exceeded'), 'expedition_input_budget_invalid': ('input_budget', 'expedition_input_budget_invalid'), 'review_attempts_exhausted': ('pipeline_exhausted', 'pipeline.review_attempts_exhausted'), 'required_semantic_review_infra_failed': ('observer_infrastructure', 'observer.required_semantic_review_infrastructure_unavailable'), 'observer_infrastructure_unavailable': ('observer_infrastructure', 'observer.infrastructure_unavailable'), 'observer_infra_failed_with_hard_errors': ('observer_infrastructure', 'observer.infrastructure_unavailable_with_hard_errors'), 'required_world_story_review_unavailable': ('observer_infrastructure', 'observer.required_world_story_review_unavailable'), _S1_REVIEW_FAILURE_QUARANTINE_REASON_CODE: ('observer_infrastructure', 'observer.required_semantic_review_infrastructure_quarantined'), _S1_REVIEW_FAILURE_RESPONSE_PARSE_QUARANTINE_REASON_CODE: ('observer_response_parse', 'observer.response_parse_repair_exhausted_quarantined'), _S1_OBSERVER_RELEASE_AUTHORIZED_QUARANTINE_REASON_CODE: ('observer_semantic_verdict', 'observer.story_release_authorized_quarantined'), _S1_OBSERVER_SEMANTIC_REJECTION_REASON_CODE: ('observer_semantic_verdict', 'observer.semantic_verdict.cannot_pass'), _S1_NEW_EQUIPMENT_QUANTITY_REPAIR_QUARANTINE_REASON_CODE: ('observer_contract', 'observer.new_equipment_quantity_repair_exhausted_quarantined')}
_S1_RESPONSE_PARSE_REASON_CODES = frozenset({'empty_content', 'incomplete_response', 'incomplete_structured_content', 'invalid_json', 'invalid_response_json', 'response_json_invalid', 'response_structure_error', 'truncated_output', 'truncated_response', 'unusable_response'})
_S1_GENERATION_STAGE_NAMES = frozenset({'objective_outline', 'expedition_resolution', 'expedition_main', 'expedition_main_validation', 'post_validation', 'settlement_item_grant_feasibility'})

def _classify_s1_retry_failure(*, stage: str, reason_code: str, action: S1RetryAction) -> tuple[str, str]:
    """把内部阶段/动作码机械映射为可聚合故障类别，不解析 AI 文本。"""
    normalized_stage = str(stage or 'unknown')[:80]
    normalized_reason = str(reason_code or 'unknown')[:120]
    exact = _S1_RETRY_FAILURE_CLASSIFICATION_BY_REASON.get(normalized_reason)
    if exact is not None:
        return exact
    lower_reason = normalized_reason.lower()
    if lower_reason in _S1_RESPONSE_PARSE_REASON_CODES:
        category = 'observer_response_parse' if normalized_stage == 'expedition_observer' else 'response_parse'
    elif lower_reason == 'provider_context_too_large':
        category = 'provider_context'
    elif lower_reason == 'provider_refusal':
        category = 'provider_refusal'
    elif lower_reason.startswith(('call_', 'http_')):
        category = 'generation_transport' if normalized_stage in _S1_GENERATION_STAGE_NAMES else 'review_transport'
    elif normalized_reason.startswith(_S1_GENERATION_INFRASTRUCTURE_REASON_PREFIX):
        category = 'generation_infrastructure'
    elif normalized_reason.startswith(_S1_UNEXPECTED_CHECKPOINT_CONSUMER_REASON_PREFIX):
        category = 'consumer_exception'
    elif action is S1RetryAction.DEFER_AUXILIARY_STATE:
        category = 'auxiliary_deferred'
    elif action in {S1RetryAction.RETRY_OBSERVER, S1RetryAction.REPAIR_BY_OBSERVER}:
        category = 'observer_contract'
    elif action is S1RetryAction.RETRY_GUARD:
        category = 'settlement_contract'
    elif action is S1RetryAction.RETRY_LOCATION_REVIEW:
        category = 'location_concurrency'
    elif action is S1RetryAction.RETRY_DEATH_RESOURCE_REVIEW:
        category = 'death_review_contract'
    elif action is S1RetryAction.REGENERATE_RESOLUTION:
        category = 'resolution_contract'
    elif action is S1RetryAction.REGENERATE_MAIN:
        category = 'generation_contract'
    elif action is S1RetryAction.REJECT_REGENERATION:
        category = 'reviewer_authority_rejected'
    elif action in {S1RetryAction.FAIL_AND_DISBAND, S1RetryAction.RETURN_STAGE_FAILURE}:
        if normalized_stage == 'objective_outline':
            category = 'objective_contract'
        elif normalized_stage == 'expedition_resolution':
            category = 'resolution_contract'
        elif normalized_stage == 'expedition_death_resource_review':
            category = 'death_review_contract'
        else:
            category = 'pipeline_terminal'
    elif action is S1RetryAction.REPAIR_LOCAL:
        category = 'contract_repair'
    elif action is S1RetryAction.RETRY_SAME_STAGE:
        category = 'generation_contract' if normalized_stage in _S1_GENERATION_STAGE_NAMES else 'stage_retry'
    elif action in {S1RetryAction.QUARANTINE_UNREVIEWED_STATE, S1RetryAction.VALIDATE_ORIGINAL_AFTER_REVIEW_FAILURE, S1RetryAction.RELEASE_STORY_AFTER_REVIEW_FAILURE}:
        category = 'review_failure_degraded'
    else:
        category = 'pipeline_decision'
    return (category, f'{category}.{normalized_stage}.{normalized_reason}'[:320])

def _terminal_s1_failure_from_retry_trace(trace: object) -> tuple[str, str] | None:
    """返回最终失败写回所用的稳定聚合类别与错误码。"""
    entries = trace if isinstance(trace, list) else []
    generic_fallback: tuple[str, str] | None = None
    non_terminal_categories = {'auxiliary_deferred', 'contract_repair', 'pipeline_decision', 'review_failure_degraded'}
    for entry in reversed(entries):
        if not isinstance(entry, dict):
            continue
        category = str(entry.get('failure_category') or '').strip()
        code = str(entry.get('failure_code') or '').strip()
        classified: tuple[str, str] | None = None
        if category and code:
            classified = (category[:80], code[:320])
        else:
            raw_action = str(entry.get('action') or '')
            try:
                action = S1RetryAction(raw_action)
            except ValueError:
                continue
            classified = _classify_s1_retry_failure(stage=str(entry.get('stage') or 'unknown'), reason_code=str(entry.get('reason_code') or 'unknown'), action=action)
        if classified[0] in non_terminal_categories:
            continue
        if classified[0] in {'pipeline_exhausted', 'pipeline_terminal'}:
            if generic_fallback is None:
                generic_fallback = classified
            continue
        return classified
    return generic_fallback

def _bounded_auxiliary_diagnostic_evidence(value: object) -> object:
    """Keep admin-only raw evidence useful without duplicating unbounded JSON."""
    max_chars = max(0, _coerce_int(current_app.config.get('ADVENTURER_S1_AUXILIARY_DIAGNOSTIC_EVIDENCE_MAX_CHARS')))
    if max_chars <= 0:
        return {'evidence_omitted': 'diagnostic_limit_disabled'}
    try:
        raw_json = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError, OverflowError):
        return {'unserializable_excerpt': str(value)[:max_chars]}
    if len(raw_json) > max_chars:
        return {'truncated_json_excerpt': raw_json[:max_chars], 'original_json_chars': len(raw_json)}
    return json.loads(raw_json)

def _defer_waiting_npc_updates_with_disposition(candidate: dict, *, full_log: dict, verdict: dict, quarantine_receipt_entries: list[dict] | None=None) -> tuple[dict, str, str]:
    """Drop an unreviewed future NPC update while retaining bounded evidence."""
    deferred_candidate = dict(candidate)
    raw_updates = deferred_candidate.get('waiting_npc_updates')
    if quarantine_receipt_entries is not None and raw_updates not in (None, '', [], {}):
        quarantine_receipt_entries.append({'path': 'waiting_npc_updates', 'value': deepcopy(raw_updates)})
    deferred_candidate['waiting_npc_updates'] = []
    waiting_snapshot = full_log.get('waiting_player_npc_injection')
    waiting_snapshot_candidates = waiting_snapshot.get('candidates') if isinstance(waiting_snapshot, dict) else []
    waiting_candidates = [row for row in waiting_snapshot_candidates or [] if isinstance(row, dict)]
    actor_refs = [{'actor_id': int(row['actor_id']), 'incarnation': int(row['incarnation'])} for row in waiting_candidates if isinstance(row.get('actor_id'), int) and (not isinstance(row.get('actor_id'), bool)) and isinstance(row.get('incarnation'), int) and (not isinstance(row.get('incarnation'), bool))]
    diagnostic_char_limit = max(0, _coerce_int(current_app.config.get('ADVENTURER_S1_AUXILIARY_DIAGNOSTIC_EVIDENCE_MAX_CHARS')))
    diagnostic_record_limit = max(0, _coerce_int(current_app.config.get('ADVENTURER_S1_AUXILIARY_DIAGNOSTIC_EVIDENCE_MAX_RECORDS')))
    contract_error = str(verdict.get('contract_error') or '').strip()
    reason_code = 'observer_output_contract_invalid' if contract_error else 'observer_semantic_review_unavailable'
    raw_detail = contract_error or 'required_waiting_npc_semantic_review_unavailable'
    detail = raw_detail[:diagnostic_char_limit] if diagnostic_char_limit > 0 else reason_code
    raw_update_rows = raw_updates if isinstance(raw_updates, list) else []
    disposition = {'status': 'deferred_unconsumed', 'reason_code': reason_code, 'contract_error': detail, 'actor_refs': actor_refs[:diagnostic_record_limit], 'actor_refs_omitted': max(0, len(actor_refs) - diagnostic_record_limit), 'candidate_updates': [_bounded_auxiliary_diagnostic_evidence(update) for update in raw_update_rows[:diagnostic_record_limit]], 'candidate_updates_omitted': max(0, len(raw_update_rows) - diagnostic_record_limit), 'event_index_basis': 'candidate_final_events_zero_based'}
    if raw_updates is not None and (not isinstance(raw_updates, list)):
        disposition['invalid_candidate_updates'] = _bounded_auxiliary_diagnostic_evidence(raw_updates)
    observer_review_id = verdict.get('observer_review_id')
    if isinstance(observer_review_id, int) and (not isinstance(observer_review_id, bool)) and (observer_review_id > 0):
        disposition['observer_review_id'] = observer_review_id
    deferred_candidate[_WAITING_NPC_REVIEW_DISPOSITION_LOG_FIELD] = disposition
    return (deferred_candidate, reason_code, detail)

def _has_s1_retained_physical_loot_settlement(result_data: object) -> bool:
    """Return whether the candidate would persist any unliquidated new item."""
    if not isinstance(result_data, dict):
        return False
    return bool(result_data.get('unassigned_items')) or any((isinstance(update, dict) and isinstance(update.get('new_equipments'), dict) and bool(update.get('new_equipments')) for update in result_data.get('member_updates') or []))

def _allow_s1_story_release_after_observer_failure(*, faction_interaction_requires_review: bool, persistent_content_review_required: bool, high_risk_life_review_requested: bool, class_transfer_review_required: bool=False, destructive_asset_review_required: bool=False, dm_instruction_review_required: bool=False) -> bool:
    """Observer infrastructure failure never acquires authority over a valid story.

    An unavailable reviewer does not revoke the original settlement proposal.
    The caller still runs the final mechanical contracts before publication.
    """
    return True

def _should_release_s1_story_after_observer_failure(*, story_release_authorized: bool, semantic_verdict_cannot_pass: bool, fallback_scope_allowed: bool, response_parse_repair_exhausted: bool, review_infra_exhausted: bool) -> bool:
    """Release after bounded infrastructure exhaustion, never after a real rejection."""
    if semantic_verdict_cannot_pass:
        return False
    if not fallback_scope_allowed:
        return False
    if story_release_authorized:
        return True
    return bool(response_parse_repair_exhausted or review_infra_exhausted)

def _validate_s1_required_world_story_result(candidate: dict, full_log: dict) -> list[str]:
    """同一冻结强制线与战报合同用于隔离候选预检和最终发布复校验。"""
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService
    from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
    from plugins.adventurer_s1.services.s1_temporary_location_service import validate_temporary_location_result
    from plugins.adventurer_s1.services.s1_campaign_world_service import activity_scope_errors
    return S1WorldOutlineService.validate_required_faction_interaction_result(candidate, full_log) + S1WorldOutlineService.validate_required_campaign_manifestation_result(candidate, full_log) + S1CampaignDispatchService.validate_result(candidate, full_log) + validate_temporary_location_result(candidate, full_log) + activity_scope_errors(candidate, full_log)

def _quarantine_s1_invalid_new_equipment_quantities(result_data: dict, *, quarantine_receipt_entries: list[dict] | None=None) -> tuple[dict, list[str]]:
    """只移除数量不能机械规范化的物品条目，不从正文猜测数量。"""
    candidate = json.loads(json.dumps(result_data, ensure_ascii=False))
    quarantined_paths: list[str] = []
    for index, update in enumerate(candidate.get('member_updates') or []):
        if not isinstance(update, dict):
            continue
        new_equipments = update.get('new_equipments')
        if new_equipments is not None and (not isinstance(new_equipments, dict)):
            if quarantine_receipt_entries is not None:
                quarantine_receipt_entries.append({'path': f'member_updates[{index}].new_equipments', 'value': deepcopy(new_equipments)})
            update.pop('new_equipments', None)
            quarantined_paths.append(f'member_updates[{index}].new_equipments')
            continue
        if not isinstance(new_equipments, dict):
            continue
        for raw_name, raw_data in list(new_equipments.items()):
            quantity = None
            if isinstance(raw_data, dict) and 'quantity' in raw_data:
                quantity, _ = normalize_ascii_integer(raw_data.get('quantity'), minimum=1, maximum=999999)
            if type(quantity) is int and 1 <= quantity <= 999999:
                raw_data['quantity'] = quantity
                continue
            if quarantine_receipt_entries is not None:
                quarantine_receipt_entries.append({'path': f'member_updates[{index}].new_equipments[{str(raw_name)!r}]', 'value': deepcopy(raw_data)})
            new_equipments.pop(raw_name, None)
            quarantined_paths.append(f'member_updates[{index}].new_equipments[{str(raw_name)!r}]')
    return (candidate, quarantined_paths[:100])

def _prepare_s1_review_failure_release(result_data: dict, *, quarantine_reason_code: str | None=None, quarantine_failure_stage: str | None=None, precollected_receipt_entries: list[dict] | None=None, preserve_verified_waiting_npc_updates: bool=False) -> tuple[dict, list[str]]:
    """审核故障保留主结果；仅处理非法数量及独立 NPC 辅助合同。"""
    candidate = json.loads(json.dumps(result_data, ensure_ascii=False))
    quarantined_paths: list[str] = []
    existing_receipt = candidate.get(_S1_REVIEW_FAILURE_QUARANTINE_RECEIPT_FIELD)
    quarantine_receipt_entries = [deepcopy(entry) for entry in [*(existing_receipt.get('entries') if isinstance(existing_receipt, dict) and isinstance(existing_receipt.get('entries'), list) else []), *(precollected_receipt_entries or [])] if isinstance(entry, dict) and isinstance(entry.get('path'), str) and ('value' in entry)]
    if not preserve_verified_waiting_npc_updates:
        if candidate.get('waiting_npc_updates'):
            quarantine_receipt_entries.append({'path': 'waiting_npc_updates', 'value': deepcopy(candidate.get('waiting_npc_updates'))})
            quarantined_paths.append('waiting_npc_updates')
        candidate['waiting_npc_updates'] = []
    candidate, invalid_quantity_paths = _quarantine_s1_invalid_new_equipment_quantities(candidate, quarantine_receipt_entries=quarantine_receipt_entries)
    quarantined_paths.extend(invalid_quantity_paths)
    if quarantine_receipt_entries:
        candidate[_S1_REVIEW_FAILURE_QUARANTINE_RECEIPT_FIELD] = {'schema_version': 1, 'status': 'quarantined_unconsumed', 'reason_code': str(quarantine_reason_code or (existing_receipt.get('reason_code') if isinstance(existing_receipt, dict) else '') or 'observer_review_unavailable')[:160], 'failure_stage': str(quarantine_failure_stage or (existing_receipt.get('failure_stage') if isinstance(existing_receipt, dict) else '') or 'unknown')[:80], 'entries': quarantine_receipt_entries}
    return (candidate, quarantined_paths[:100])

def _quarantine_s1_settlement_guard_state(result_data: dict, *, reason_code: str, guard_scope: dict[int, dict[str, object]]) -> tuple[dict, list[str]]:
    """Keep story/life facts while withholding only exact Guard-owned writes."""
    candidate = json.loads(json.dumps(result_data, ensure_ascii=False))
    existing_receipt = candidate.get(_S1_REVIEW_FAILURE_QUARANTINE_RECEIPT_FIELD)
    entries = [deepcopy(entry) for entry in (existing_receipt.get('entries') if isinstance(existing_receipt, dict) and isinstance(existing_receipt.get('entries'), list) else []) if isinstance(entry, dict) and isinstance(entry.get('path'), str) and ('value' in entry)]
    quarantined_paths: list[str] = []
    for index, update in enumerate(candidate.get('member_updates') or []):
        if not isinstance(update, dict):
            continue
        try:
            member_id = int(update.get('id'))
        except (TypeError, ValueError):
            continue
        member_scope = guard_scope.get(member_id)
        if not isinstance(member_scope, dict):
            continue
        member_fields = {str(field_name) for field_name in member_scope.get('member_fields') or () if str(field_name) in {'new_class', 'character_class', 'new_skills'}}
        for field_name in sorted(member_fields):
            if field_name not in update:
                continue
            path = f'member_updates[{index}].{field_name}'
            entries.append({'path': path, 'value': deepcopy(update.get(field_name))})
            update.pop(field_name, None)
            quarantined_paths.append(path)
        consumed_names = {str(name).strip() for name in member_scope.get('consumed_equipment_names') or () if str(name).strip()}
        raw_consumed = update.get('consumed_equipments')
        if consumed_names and raw_consumed is not None:
            consumed_entries = raw_consumed if isinstance(raw_consumed, list) else [raw_consumed]
            retained_entries = []
            removed_any = False
            for consumed_index, entry in enumerate(consumed_entries):
                entry_name = str(entry.get('name') or '').strip() if isinstance(entry, dict) else str(entry or '').strip()
                if entry_name not in consumed_names:
                    retained_entries.append(entry)
                    continue
                path = f'member_updates[{index}].consumed_equipments[{consumed_index}]'
                entries.append({'path': path, 'value': deepcopy(entry)})
                quarantined_paths.append(path)
                removed_any = True
            if removed_any:
                if retained_entries:
                    update['consumed_equipments'] = retained_entries
                else:
                    update.pop('consumed_equipments', None)
    if entries:
        candidate[_S1_REVIEW_FAILURE_QUARANTINE_RECEIPT_FIELD] = {'schema_version': 1, 'status': 'quarantined_unconsumed', 'reason_code': str(reason_code or 'settlement_guard_unavailable')[:160], 'failure_stage': 'settlement_guard', 'entries': entries}
    return (candidate, quarantined_paths[:100])

def _has_s1_review_failure_story_release(trace: object) -> bool:
    """识别内部 checkpoint 中已经完成机械复校验的故障降级发布。"""
    entries = trace if isinstance(trace, list) else []
    return any((isinstance(entry, dict) and entry.get('action') == S1RetryAction.RELEASE_STORY_AFTER_REVIEW_FAILURE.value for entry in entries))

def _pending_s1_review_failure_story_release(trace: object) -> tuple[str, str] | None:
    """返回故障后待最终复校验的放行原因；兼容历史隔离 checkpoint。"""
    entries = trace if isinstance(trace, list) else []
    if _has_s1_review_failure_story_release(entries):
        return None
    pending = None
    for entry in reversed(entries):
        if isinstance(entry, dict) and entry.get('action') in {S1RetryAction.REGENERATE_MAIN.value, S1RetryAction.REGENERATE_RESOLUTION.value, S1RetryAction.FAIL_AND_DISBAND.value}:
            break
        if isinstance(entry, dict):
            release_reason_code = _S1_REVIEW_FAILURE_RELEASE_REASON_CODES.get(entry.get('reason_code'))
            if release_reason_code:
                matched = (release_reason_code, str(entry.get('detail') or '')[:2000])
                if release_reason_code == 'observer_error_original_result_released':
                    return matched
                if pending is None:
                    pending = matched
    return pending

def _record_s1_retry_decision(trace: list[dict] | None, *, stage: str, reason_code: str, action: S1RetryAction, attempt: int, max_attempts: int, preserves_main_result: bool | None, detail: str | None=None, correlation_id: str | None=None) -> dict:
    """记录稳定 reason/action，并同步输出单行后端日志；轨迹始终有界。"""
    failure_category, failure_code = _classify_s1_retry_failure(stage=stage, reason_code=reason_code, action=action)
    entry = {'stage': str(stage or 'unknown')[:80], 'reason_code': str(reason_code or 'unknown')[:120], 'failure_category': failure_category, 'failure_code': failure_code, 'action': action.value, 'attempt': max(1, int(attempt or 1)), 'max_attempts': max(1, int(max_attempts or 1)), 'preserves_main_result': preserves_main_result, 'recorded_at': _portable_utc_now().isoformat()}
    if detail:
        entry['detail'] = str(detail)[:1000]
    if trace is not None:
        trace.append(entry)
        if len(trace) > _S1_RETRY_TRACE_MAX_ENTRIES:
            del trace[:-_S1_RETRY_TRACE_MAX_ENTRIES]
    log_retry_decision = current_app.logger.info if action is S1RetryAction.DEFER_AUXILIARY_STATE else current_app.logger.warning
    normalized_correlation_id = str(correlation_id or '').strip()[:64]
    correlation_fragment = f' correlation_id={normalized_correlation_id}' if normalized_correlation_id else ''
    log_retry_decision('S1 retry_decision stage=%s reason_code=%s failure_category=%s failure_code=%s action=%s attempt=%s/%s preserves_main_result=%s%s detail=%s', entry['stage'], entry['reason_code'], entry['failure_category'], entry['failure_code'], entry['action'], entry['attempt'], entry['max_attempts'], preserves_main_result, correlation_fragment, entry.get('detail', ''))
    return entry
_S1_OBSERVER_FEEDBACK_MAX_CHARS = 12000

def _build_s1_observer_regeneration_feedback(regeneration_basis: str | None, *, verdict: dict | None=None, result_data: dict | None=None) -> str:
    """Continue incomplete stories; isolate causal rejections from new attempts."""
    if regeneration_basis == 'narrative_incomplete':
        fallback = '上一次玩家可见正文尚未形成完整冒险叙事。请完整呈现起因、推进、决定性转折与代价、结果以及离场收束，并让这些内容在 events 中成立。按【深渊酒馆·单次探险的故事边界】完成本场行动及返程，不以新行动的起点结束，也不改变已经成立的死亡或无法归队结果。'
        events = result_data.get('events') if isinstance(result_data, dict) else None
        if not (isinstance(events, list) and events and all((isinstance(event, str) for event in events))):
            return fallback
        from .adventurer_s1_prompt_service import S1_NARRATIVE_REPAIR_TASK
        verdict = verdict if isinstance(verdict, dict) else {}
        reason = verdict.get('reason')
        evidence = verdict.get('regeneration_evidence')
        indexes = evidence.get('main_event_indexes') if isinstance(evidence, dict) else None
        payload = json.dumps({'task': S1_NARRATIVE_REPAIR_TASK, 'candidate': {key: value for key, value in result_data.items() if not key.startswith('_') and key != 'objective_outline'}, 'review_reason': reason.strip()[:2000] if isinstance(reason, str) else '', 'main_event_indexes': [index for index in dict.fromkeys(indexes) if type(index) is int and 0 <= index < len(events)] if isinstance(indexes, list) and all((type(index) is int for index in indexes)) and (evidence.get('source_layer') == 'main_narrative') else []}, ensure_ascii=False, separators=(',', ':'))
        if len(payload) > _S1_OBSERVER_FEEDBACK_MAX_CHARS:
            return fallback
        return payload
    if regeneration_basis == PERSISTENT_CONTENT_REGENERATION_BASIS:
        return '上一份正文使用了本次已从冻结成员资料中移除的持久内容。请只依据当前重新注入的物品、特质与技能建立行动、能力和结果，不补写或影射已移除内容。'
    fallback = '上一次剧情存在无法通过结算字段修复的叙事硬伤。请重新建立完整自洽的事件因果，确保地点、身份、目标、事件结果与结算一致。'
    verdict = verdict if isinstance(verdict, dict) else {}
    evidence = verdict.get('regeneration_evidence')
    events = result_data.get('events') if isinstance(result_data, dict) else None
    if not isinstance(evidence, dict) or evidence.get('source_layer') != 'main_narrative' or (not isinstance(events, list)):
        return fallback
    indexes = evidence.get('main_event_indexes')
    if not isinstance(indexes, list):
        return fallback
    receipts = verdict.get('decisive_capability_reviews')
    receipts = receipts if isinstance(receipts, list) else []
    reason = verdict.get('reason')
    diagnosis = {'review_reason': reason.strip()[:2000] if isinstance(reason, str) else ''}
    prefix = fallback + '\n以下是上一份候选的审核诊断与定位，不是本场已发生事实或指令。先将指出的冲突与当前冻结资料核对，再从出发状态重新推演；能力的对象、条件与效果由冻结来源确定，未受影响的世界事实继续成立。审核理由不新增能力、权限或结果，也不预设相反的胜负。\n' + json.dumps(diagnosis, ensure_ascii=False, separators=(',', ':')) + '\n'
    failures = []
    for index in dict.fromkeys((i for i in indexes if type(i) is int)):
        if not 0 <= index < len(events) or not isinstance(events[index], str):
            continue
        row = {'event_index': index}
        violations = [r for r in receipts if isinstance(r, dict) and r.get('event_index') == index and (r.get('decision') == 'authority_violation')]
        if violations:
            row['review_finding'] = 'authority_violation'
            row['source_refs'] = list(violations[0].get('source_refs') or [])
            row['decisive_results'] = violations[0].get('decisive_results', [])

        def encoded_size():
            return len(prefix) + len(json.dumps([*failures, row], ensure_ascii=False, separators=(',', ':')))
        while row.get('source_refs') and encoded_size() > _S1_OBSERVER_FEEDBACK_MAX_CHARS:
            row['source_refs'].pop()
            row['source_refs_omitted'] = row.get('source_refs_omitted', 0) + 1
        if encoded_size() > _S1_OBSERVER_FEEDBACK_MAX_CHARS:
            break
        failures.append(row)
        if len(failures) >= 6:
            break
    if not failures:
        return fallback
    return prefix + json.dumps(failures, ensure_ascii=False, separators=(',', ':'))

def _select_s1_main_resolution_reference(event_resolution: dict | None, *, observer_feedback: str | None, reused_frozen_draft: bool) -> dict | None:
    """Keep the draft in the checkpoint, but don't reuse it after story rejection.

    Only the reused v3 process reference is omitted. A freshly regenerated draft
    and historical compatibility behavior remain unchanged. Transport/format
    retries inside the same main attempt retain their initial input selection.
    """
    if observer_feedback and reused_frozen_draft and isinstance(event_resolution, dict) and (event_resolution.get('resolution_contract_version') == 3):
        return None
    return event_resolution

def _build_s1_event_resolution_regeneration_feedback(regeneration_basis: str | None) -> str:
    """Translate a structured upstream rejection without injecting Observer prose."""
    if regeneration_basis != 'event_resolution_invalid':
        return ''
    return '【主剧情因果底稿定向重做】上一份底稿的关键能力或生命因果未通过审核。请按当前冻结客观大纲重新裁决，并让每项决定性减免、耐久绕过与豁免都由已成立来源的对象、范围、时机、持续和容量完整支持。角色的强度判断、撤离与救援取舍只使用决定前已经进入本人认知的信息；信息尚不足时仍按性格、状态和当下目标自然行动，取证不暂停威胁，实际形成的交战、救援、接触与生死结果照实裁决。威胁已经显露的直接作用可作为判断依据。'

def _coerce_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _coerce_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ('1', 'true', 'yes', 'on', 'y'):
            return True
        if low in ('0', 'false', 'no', 'off', 'n'):
            return False
    return default

def _coerce_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _gold_change_cents(value) -> int:
    """把金币变动无损归一到分，非法或非有限值按 0 处理。"""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return 0
    if not amount.is_finite():
        return 0
    amount = amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    return int(amount * 100)

def _gold_change_from_cents(cents: int) -> int | float:
    """保持整金币使用 int，其余使用两位小数，避免日志出现多余浮点尾数。"""
    if cents % 100 == 0:
        return cents // 100
    return float(Decimal(cents) / Decimal(100))

def _redistribute_expedition_gold_rewards(member_updates: list, member_ids: list[int], *, remainder_offset: int=0) -> dict[int, int | float]:
    """均分探险 AI/Observer 最终给出的正向金币收益。

    只汇总正数 ``gold_change``，并仅在最终存活成员之间均分；死亡成员不分正向收益。
    负数仍是对应成员的个人损失，并在存活成员的均分收益后抵扣。
    有存活成员时，分币余数按 ``remainder_offset`` 轮转，保证总额不增不减且避免固定成员长期多拿一分；
    全员死亡时不发放任何正向金币。
    函数原地更新 member_updates，并返回逐成员最终金币变动，便于日志与测试核对。
    """
    if not isinstance(member_updates, list):
        return {}
    updates_by_id: dict[int, dict] = {}
    for update in member_updates:
        if not isinstance(update, dict) or update.get('id') is None:
            continue
        member_id = _coerce_int(update.get('id'), -1)
        if member_id >= 0:
            updates_by_id[member_id] = update
    ordered_ids: list[int] = []
    seen_ids: set[int] = set()
    for raw_member_id in member_ids:
        member_id = _coerce_int(raw_member_id, -1)
        if member_id < 0 or member_id in seen_ids or member_id not in updates_by_id:
            continue
        seen_ids.add(member_id)
        ordered_ids.append(member_id)
    if not ordered_ids:
        return {}
    original_cents = {member_id: _gold_change_cents(updates_by_id[member_id].get('gold_change', 0)) for member_id in ordered_ids}
    positive_total_cents = sum((max(0, cents) for cents in original_cents.values()))
    if positive_total_cents <= 0:
        return {}
    eligible_ids = [member_id for member_id in ordered_ids if not (updates_by_id[member_id].get('is_dead') is True or ('remaining_hp' in updates_by_id[member_id] and _coerce_int(updates_by_id[member_id].get('remaining_hp'), 0) <= 0))]
    base_share, remainder = divmod(positive_total_cents, len(eligible_ids)) if eligible_ids else (0, 0)
    remainder_start = remainder_offset % len(eligible_ids) if eligible_ids else 0
    remainder_indexes = {(remainder_start + index) % len(eligible_ids) for index in range(remainder)}
    redistributed: dict[int, int | float] = {}
    eligible_index = {member_id: index for index, member_id in enumerate(eligible_ids)}
    for member_id in ordered_ids:
        personal_loss = min(0, original_cents[member_id])
        index = eligible_index.get(member_id)
        shared_reward = base_share + (1 if index in remainder_indexes else 0) if index is not None else 0
        final_cents = shared_reward + personal_loss
        final_change = _gold_change_from_cents(final_cents)
        updates_by_id[member_id]['gold_change'] = final_change
        redistributed[member_id] = final_change
    return redistributed

def _materialize_expedition_gold_changes(full_log: dict, member_ids: list[int], *, remainder_offset: int=0) -> dict[int, int | float]:
    """消费当前团队/个人合同；没有合同标记的在途旧日志沿用旧规则。"""
    if not isinstance(full_log, dict):
        return {}
    current_contract = S1_GOLD_SETTLEMENT_FIELD in full_log or full_log.get(S1_GOLD_SETTLEMENT_VERSION_FIELD) == S1_GOLD_SETTLEMENT_CONTRACT_VERSION
    if not current_contract:
        return _redistribute_expedition_gold_rewards(full_log.get('member_updates') or [], member_ids, remainder_offset=remainder_offset)
    if S1_GOLD_SETTLEMENT_COMPONENTS_FIELD in full_log:
        for update in full_log.get('member_updates') or []:
            if isinstance(update, dict):
                update.pop('gold_change', None)
    errors = normalize_s1_gold_settlement(full_log, member_ids=member_ids, event_count=len(full_log.get('events') or []), require_current_contract=True)
    if errors:
        raise ValueError('; '.join(errors[:10]))
    changes, components = materialize_s1_gold_settlement(full_log.get('member_updates') or [], member_ids, full_log[S1_GOLD_SETTLEMENT_FIELD], remainder_offset=remainder_offset)
    full_log[S1_GOLD_SETTLEMENT_COMPONENTS_FIELD] = components
    return changes

@dataclass
class S1ExpeditionMemberSnapshot:
    """切断 ORM Session 后的成员快照，供主探险 AI prompt / 校验使用。"""
    id: int
    owner_id: int
    name: str
    gender: str
    character_class: str
    level: int
    hp: int
    max_hp: int
    strength: int
    agility: int
    intelligence: int
    luck: int
    personality: str
    gold: float
    history_story: str = ''
    traits: list = field(default_factory=list)
    skill_items: list = field(default_factory=list)
    equipment_items: list = field(default_factory=list)

def restore_names_in_narrative(text: str, codename_to_real: dict[str, str]) -> str:
    """把 AI 输出的叙事文本中的代号还原为真实冒险者名字。

    codename_to_real: {'【M1】': '张三', 'M1': '张三', ...}。
    完整 token 与带边界的 bare ``M1`` 单轮替换，避免再次处理还原后的真实名字。
    """
    if not text or not codename_to_real:
        return text
    bare_m = re.compile('^M\\d+$')
    full_tokens = {k: v for k, v in codename_to_real.items() if k and v and (k != v) and (not bare_m.fullmatch(k))}
    bare_map = {k: v for k, v in codename_to_real.items() if k and v and (k != v) and bare_m.fullmatch(k)}
    patterns = [re.escape(key) for key in sorted(full_tokens, key=len, reverse=True)]
    if bare_map:
        patterns.append('(?<![A-Za-z【])M\\d+(?!】|\\d)')
    if not patterns:
        return text
    mapping = {**full_tokens, **bare_map}
    return re.sub('|'.join(patterns), lambda match: mapping.get(match.group(0), match.group(0)), text)

def _restore_codenames_in_named_collection(value, restore_fn):
    """还原 ``{name: desc}`` dict 或 ``[{name, description}]`` list 内的名称(含 dict key)
    与描述文本里的代号。restore_fn: (str) -> str。返回还原后的结构（保持原类型）。

    覆盖 new_traits / new_skills / new_equipments 三类奖励字段：主 AI 会在物品名、特质名、
    技能名及其描述里引用队友代号（如「【M1】所赠的佩剑」），这些字段不属于叙事正文，
    早期还原只扫 summary/events/传记，导致特质/技能/装备落库后仍残留代号。
    """
    if isinstance(value, dict):
        rebuilt = {}
        for name, desc in value.items():
            new_name = restore_fn(name) if isinstance(name, str) else name
            if isinstance(desc, str):
                desc = restore_fn(desc)
            elif isinstance(desc, dict):
                for dk in ('desc', 'description'):
                    if isinstance(desc.get(dk), str):
                        desc[dk] = restore_fn(desc[dk])
            rebuilt[new_name] = desc
        return rebuilt
    if isinstance(value, list):
        for idx, item in enumerate(value):
            if isinstance(item, dict):
                if isinstance(item.get('name'), str):
                    item['name'] = restore_fn(item['name'])
                for dk in ('description', 'desc'):
                    if isinstance(item.get(dk), str):
                        item[dk] = restore_fn(item[dk])
                for dependency in item.get('asset_dependencies') or []:
                    if isinstance(dependency, dict) and isinstance(dependency.get('new_asset_name'), str):
                        dependency['new_asset_name'] = restore_fn(dependency['new_asset_name'])
            elif isinstance(item, str):
                value[idx] = restore_fn(item)
        return value
    return value

def _restore_codenames_in_contract_payload(value, codename_to_real: dict[str, str]):
    """递归还原已由 parser 白名单投影的 AI 契约值，供严格校验与消费使用。"""
    if isinstance(value, str):
        return restore_names_in_narrative(value, codename_to_real)
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _restore_codenames_in_contract_payload(item, codename_to_real)
        return value
    if isinstance(value, dict):
        rebuilt = {}
        for key, item in value.items():
            restored_key = restore_names_in_narrative(key, codename_to_real) if isinstance(key, str) else key
            rebuilt[restored_key] = _restore_codenames_in_contract_payload(item, codename_to_real)
        value.clear()
        value.update(rebuilt)
    return value

def _restore_codenames_in_ai_json_result(result: S1AiJsonResult, codename_to_real: dict[str, str]) -> S1AiJsonResult:
    """保留冻结解析结果的诊断元数据，并以新实例承载还原后的契约值。"""
    if result.data is None or not codename_to_real:
        return result
    restored_data = _restore_codenames_in_contract_payload(deepcopy(result.data), codename_to_real)
    return replace(result, data=restored_data)

def _restore_codenames_in_reward_fields(update: dict, restore_fn) -> None:
    """原地还原 member_update 内 new_traits / new_skills / new_equipments 的名称与描述。"""
    for field_name in ('new_traits', 'new_skills', 'new_equipments'):
        if update.get(field_name) is not None:
            update[field_name] = _restore_codenames_in_named_collection(update[field_name], restore_fn)

def _restore_codenames_in_result(result_data: dict, codename_to_real: dict[str, str]) -> dict:
    """把解析后的 result_data 中所有玩家可见叙事文本里的代号还原为真名。"""
    from .s1_team_loot_service import capture_team_loot_review, refresh_team_loot_review_after_name_mapping
    loot_review = capture_team_loot_review(result_data)
    if result_data.get('summary'):
        result_data['summary'] = restore_names_in_narrative(result_data['summary'], codename_to_real)
    for idx, evt in enumerate(result_data.get('events') or []):
        if isinstance(evt, dict):
            for key in ('text', 'description', 'content'):
                if evt.get(key) and isinstance(evt[key], str):
                    evt[key] = restore_names_in_narrative(evt[key], codename_to_real)
        elif isinstance(evt, str):
            result_data['events'][idx] = restore_names_in_narrative(evt, codename_to_real)

    def _restore(text: str) -> str:
        return restore_names_in_narrative(text, codename_to_real)
    for update in result_data.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        if update.get('death_reason'):
            update['death_reason'] = _restore(update['death_reason'])
        if update.get('history_story_append'):
            update['history_story_append'] = _restore(update['history_story_append'])
        _restore_codenames_in_reward_fields(update, _restore)
    _restore_waiting_npc_visible_fields(result_data, _restore)
    _restore_team_loot_visible_fields(result_data, _restore)
    refresh_team_loot_review_after_name_mapping(result_data, loot_review)
    return result_data

def _restore_team_loot_visible_fields(result_data: dict, transform) -> None:
    for row in result_data.get('unassigned_items') or []:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get('name'), str):
            row['name'] = transform(row['name'])
        item = row.get('item')
        if isinstance(item, dict):
            for field in ('desc', 'damage_note'):
                if isinstance(item.get(field), str):
                    item[field] = transform(item[field])

def _restore_waiting_npc_visible_fields(result_data: dict, transform) -> None:
    """同步变换 NPC 原文证据与未来会展示给玩家的有界档案文本。"""
    for update in result_data.get('waiting_npc_updates') or []:
        if not isinstance(update, dict):
            continue
        for record in update.get('records') or []:
            if isinstance(record, dict) and isinstance(record.get('excerpt'), str):
                record['excerpt'] = transform(record['excerpt'])
        profile = update.get('profile')
        if not isinstance(profile, dict):
            continue
        if isinstance(profile.get('character_class'), str):
            profile['character_class'] = transform(profile['character_class'])
        for field_name in ('equipments', 'skills', 'traits'):
            for row in profile.get(field_name) or []:
                if not isinstance(row, dict):
                    continue
                for key in ('name', 'desc'):
                    if isinstance(row.get(key), str):
                        row[key] = transform(row[key])

def _restore_codenames_in_event_resolution(resolution: dict, codename_to_real: dict[str, str]) -> dict:
    """还原前置裁决的完整契约值，便于严格校验、持久化与 Observer 核对。"""
    restored = json.loads(json.dumps(resolution, ensure_ascii=False))
    return _restore_codenames_in_contract_payload(restored, codename_to_real)
_HASH_ID_PATTERN = re.compile('#(\\d+)')

def _fix_hash_id_names_in_result(result_data: dict, id_to_name: dict[int, str]) -> dict:
    """兜底替换事件/摘要中残留的 #id 格式为真实冒险者名字。

    AI 有时忽略代号，直接用 #57 / #60 等 ID 格式引用成员。本函数在代号还原之后
    运行，捕获仍残留的 #id 并替换。id_to_name: {57: '张三', ...}。
    """
    if not id_to_name:
        return result_data
    from .s1_team_loot_service import capture_team_loot_review, refresh_team_loot_review_after_name_mapping
    loot_review = capture_team_loot_review(result_data)

    def _replace(text: str) -> str:

        def _sub(m):
            mid = int(m.group(1))
            return id_to_name.get(mid, m.group(0))
        return _HASH_ID_PATTERN.sub(_sub, text)
    if result_data.get('summary') and isinstance(result_data['summary'], str):
        result_data['summary'] = _replace(result_data['summary'])
    for idx, evt in enumerate(result_data.get('events') or []):
        if isinstance(evt, str):
            result_data['events'][idx] = _replace(evt)
        elif isinstance(evt, dict):
            for key in ('text', 'description', 'content'):
                if isinstance(evt.get(key), str):
                    evt[key] = _replace(evt[key])
    for update in result_data.get('member_updates') or []:
        if isinstance(update, dict):
            if isinstance(update.get('death_reason'), str):
                update['death_reason'] = _replace(update['death_reason'])
            if isinstance(update.get('history_story_append'), str):
                update['history_story_append'] = _replace(update['history_story_append'])
            _restore_codenames_in_reward_fields(update, _replace)
    _restore_waiting_npc_visible_fields(result_data, _replace)
    _restore_team_loot_visible_fields(result_data, _replace)
    refresh_team_loot_review_after_name_mapping(result_data, loot_review)
    return result_data

def _name_appears_in_text(name: str, text: str) -> bool:
    """灵活匹配名字是否出现在文本中。

    直接精确匹配；若失败，对 ≥4 字的名字取后半部分（≥2 字）再匹配，
    兼容 AI 用简称（如「倚星星」指「魔法少女倚星星」）。
    """
    if not name or not text:
        return False
    if name in text:
        return True
    if len(name) >= 4:
        core = name[len(name) // 2:]
        if len(core) >= 2 and core in text:
            return True
    return False

def _detect_missing_name_mentions(result_data: dict, members) -> dict[int, str]:
    """检测 summary + events 里完全未出现真名的成员，返回 {id: name} 缺失映射。

    用于代号还原 / #id 兜底后的人名覆盖率检查：若某成员在所有玩家可见叙事中
    完全没有被提到真名，说明 AI 使用了未知的别名格式或完全遗漏了该成员。
    """
    if not members:
        return {}
    parts: list[str] = []
    summary = result_data.get('summary')
    if isinstance(summary, str):
        parts.append(summary)
    for evt in result_data.get('events') or []:
        if isinstance(evt, str):
            parts.append(evt)
        elif isinstance(evt, dict):
            for key in ('text', 'description', 'content'):
                if isinstance(evt.get(key), str):
                    parts.append(evt[key])
    combined = ''.join(parts)
    missing: dict[int, str] = {}
    for m in members:
        name = getattr(m, 'name', None) or ''
        if name and (not _name_appears_in_text(name, combined)):
            missing[int(m.id)] = name
    return missing

def _scan_named_collection_for_codenames(value, where: str, scan) -> None:
    """遍历 {name: desc} 或 [{name, description}] 的名称(含 dict key)与描述，逐项调 scan(text, where)。"""
    if isinstance(value, dict):
        for name, desc in value.items():
            scan(name, f'{where}.name')
            if isinstance(desc, str):
                scan(desc, f'{where}.desc')
            elif isinstance(desc, dict):
                for dk in ('desc', 'description'):
                    scan(desc.get(dk), f'{where}.desc')
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                scan(item.get('name'), f'{where}.name')
                for dk in ('description', 'desc'):
                    scan(item.get(dk), f'{where}.desc')
            elif isinstance(item, str):
                scan(item, where)

def _detect_residual_codenames_in_result(result_data: dict) -> list[str]:
    """后端最终裁决：确定性扫描结算结果全部玩家可见字段 + 特质/技能/装备名称与描述里
    残留的探险代号（【Mx】/ bare Mx / 冒险者甲乙）。

    可映射代号应已在 _restore_codenames_in_result、#id 兜底与 observer name_corrections
    阶段还原；此处仍扫到，说明代号未能映射到真名（多为 AI 引用了超出队伍人数的编号或
    不存在的队友），由调用方据此打回重写。返回带位置标注的残留列表，空列表表示干净。
    """
    from plugins.adventurer_s1.services.adventurer_s1_prompt_service import RESIDUAL_CODENAME_PATTERN
    if not isinstance(result_data, dict):
        return []
    hits: list[str] = []

    def _scan(text, where: str) -> None:
        if not isinstance(text, str) or not text:
            return
        found = RESIDUAL_CODENAME_PATTERN.findall(text)
        if found:
            uniq = list(dict.fromkeys(found))
            hits.append(f"{where}: {', '.join(uniq[:4])}")
    _scan(result_data.get('summary'), 'summary')
    for idx, evt in enumerate(result_data.get('events') or []):
        if isinstance(evt, str):
            _scan(evt, f'events[{idx}]')
        elif isinstance(evt, dict):
            for key in ('text', 'description', 'content'):
                _scan(evt.get(key), f'events[{idx}].{key}')
    for idx, update in enumerate(result_data.get('member_updates') or []):
        if not isinstance(update, dict):
            continue
        _scan(update.get('death_reason'), f'member_updates[{idx}].death_reason')
        _scan(update.get('history_story_append'), f'member_updates[{idx}].history_story_append')
        for field_name in ('new_traits', 'new_skills', 'new_equipments'):
            _scan_named_collection_for_codenames(update.get(field_name), f'member_updates[{idx}].{field_name}', _scan)
    for idx, row in enumerate(result_data.get('unassigned_items') or []):
        if not isinstance(row, dict):
            continue
        _scan(row.get('name'), f'unassigned_items[{idx}].name')
        item = row.get('item') or {}
        for field in ('desc', 'damage_note'):
            _scan(item.get(field), f'unassigned_items[{idx}].item.{field}')
    for idx, update in enumerate(result_data.get('waiting_npc_updates') or []):
        if not isinstance(update, dict):
            continue
        profile = update.get('profile')
        if not isinstance(profile, dict):
            continue
        _scan(profile.get('character_class'), f'waiting_npc_updates[{idx}].profile.character_class')
        for field_name in ('equipments', 'skills', 'traits'):
            _scan_named_collection_for_codenames(profile.get(field_name), f'waiting_npc_updates[{idx}].profile.{field_name}', _scan)
    return hits

def _try_restore_codenames_on_result(result_data: dict, name_alias: dict[int, str] | None, member_snapshots) -> dict:
    """Guard/审核后残留代号时，再跑一轮确定性代号还原，避免无谓整段重生成。"""
    if not isinstance(result_data, dict) or not name_alias:
        return result_data
    from plugins.adventurer_s1.services.adventurer_s1_prompt_service import build_codename_to_real_from_alias
    id_to_name = {m.id: m.name for m in member_snapshots or [] if getattr(m, 'name', None)}
    mapping = build_codename_to_real_from_alias(name_alias, id_to_name)
    restored = _restore_codenames_in_result(result_data, mapping)
    return _fix_hash_id_names_in_result(restored, id_to_name)

def _build_s1_expedition_member_snapshots(members, *, description_ai_context: dict | None=None) -> tuple[list[S1ExpeditionMemberSnapshot], dict]:
    """把 ORM 成员转成纯快照，并记录用餐加成的出发/快照 HP 对照（供结算映射）。"""
    from plugins.adventurer_s1.services.s1_dining_service import S1DiningService
    from plugins.adventurer_s1.services.s1_companion_historical_repair_service import S1CompanionHistoricalRepairService
    from plugins.adventurer_s1.services.s1_asset_recovery_service import is_asset_recovering, normalize_recovery_minutes
    member_rows = list(members or [])
    member_ids = []
    for member in member_rows:
        try:
            member_ids.append(int(member.id))
        except (TypeError, ValueError):
            continue
    description_ai_context = description_ai_context if isinstance(description_ai_context, dict) else {}
    lineage_by_adventurer = description_ai_context.get('lineage_by_adventurer')
    if not isinstance(lineage_by_adventurer, dict) or not lineage_by_adventurer:
        lineage_by_adventurer = S1CompanionHistoricalRepairService.load_active_lineage_context(member_ids)
    equipment_desc_ai = description_ai_context.get('equipment_by_id') or {}
    equipment_full_desc = description_ai_context.get('equipment_full_by_id') or {}
    skill_desc_ai = description_ai_context.get('skill_by_id') or {}
    trait_desc_ai = description_ai_context.get('trait_by_id') or {}
    trait_context_rules = description_ai_context.get('trait_context_rule_by_id') or {}
    asset_dependent_trait_ids = description_ai_context.get('asset_dependent_trait_ids')
    active_trait_ids = description_ai_context.get('active_trait_ids')
    if not isinstance(active_trait_ids, set) or not isinstance(asset_dependent_trait_ids, set):
        from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import trait_asset_dependency_states_for_members
        active_trait_ids, asset_dependent_trait_ids = trait_asset_dependency_states_for_members(member_rows)
    snapshots: list[S1ExpeditionMemberSnapshot] = []
    dining_context: dict[str, dict] = {}
    recovery_now = _portable_utc_now()
    for member in member_rows:
        try:
            member_id = int(member.id)
        except (TypeError, ValueError):
            continue
        base_hp = _coerce_int(getattr(member, 'hp', 0), 0)
        base_max_hp = _coerce_int(getattr(member, 'max_hp', 1), 1)
        strength, agility, intelligence, luck, hp, max_hp = S1DiningService.apply_buffs_to_snapshot_values(_coerce_int(getattr(member, 'strength', 0), 0), _coerce_int(getattr(member, 'agility', 0), 0), _coerce_int(getattr(member, 'intelligence', 0), 0), _coerce_int(getattr(member, 'luck', 0), 0), base_hp, base_max_hp, getattr(member, 'expedition_buff', None))
        dining_context[str(member_id)] = S1DiningService.build_dining_context_entry(member_id, base_hp, base_max_hp, hp, max_hp, getattr(member, 'expedition_buff', None))
        snapshots.append(S1ExpeditionMemberSnapshot(id=member_id, owner_id=_coerce_int(getattr(member, 'owner_id', 0), 0), name=str(getattr(member, 'name', '') or ''), gender=str(getattr(member, 'gender', '') or ''), character_class=str(getattr(member, 'character_class', '') or ''), level=_coerce_int(getattr(member, 'level', 1), 1), hp=hp, max_hp=max_hp, strength=strength, agility=agility, intelligence=intelligence, luck=luck, personality=str(getattr(member, 'personality', '') or ''), gold=float(getattr(member, 'gold', 0) or 0), history_story=str(getattr(member, 'history_story', '') or ''), traits=[SimpleNamespace(id=_coerce_int(getattr(t, 'id', 0), 0), name=t.name, description=getattr(t, 'description', '') or '', description_ai=trait_desc_ai.get(_coerce_int(getattr(t, 'id', 0), 0)), description_ai_source_hash=description_source_hash(getattr(t, 'description', None), kind='trait') if _coerce_int(getattr(t, 'id', 0), 0) in (description_ai_context.get('trait_summary_ids') or set()) else None, trait_context_rule=trait_context_rules.get(_coerce_int(getattr(t, 'id', 0), 0)), has_asset_dependencies=_coerce_int(getattr(t, 'id', 0), 0) in asset_dependent_trait_ids, is_archived=bool(getattr(t, 'is_archived', False)), recovery_policy=getattr(t, 'recovery_policy', 'none') or 'none', recovery_stage=getattr(t, 'recovery_stage', None), recovery_progress=_coerce_int(getattr(t, 'recovery_progress', 0), 0), recovery_required=getattr(t, 'recovery_required', None), asset_dependencies=getattr(t, 'asset_dependencies', None), _persistent_asset_state=build_persistent_asset_state('trait', member_id, t)) for t in getattr(member, 'traits', None) or [] if _coerce_int(getattr(t, 'id', 0), 0) in active_trait_ids], skill_items=[SimpleNamespace(id=_coerce_int(getattr(s, 'id', 0), 0), name=s.name, description=getattr(s, 'description', '') or '', description_ai=skill_desc_ai.get(_coerce_int(getattr(s, 'id', 0), 0)), description_ai_source_hash=description_source_hash(getattr(s, 'description', None), kind='skill') if _coerce_int(getattr(s, 'id', 0), 0) in (description_ai_context.get('skill_summary_ids') or set()) else None, equipped=bool(getattr(s, 'equipped', True)), recovery_minutes=normalize_recovery_minutes(getattr(s, 'recovery_minutes', 0)), recovering=is_asset_recovering(s, now=recovery_now), recovery_started_at=getattr(s, 'recovery_started_at', None), recovery_ends_at=getattr(s, 'recovery_ends_at', None), _persistent_asset_state=build_persistent_asset_state('skill', member_id, s)) for s in getattr(member, 'skill_items', None) or []], equipment_items=[SimpleNamespace(id=_coerce_int(getattr(e, 'id', 0), 0), name=e.name, type=getattr(e, 'type', '') or '', rarity=getattr(e, 'rarity', 'common') or 'common', custom_desc=getattr(e, 'custom_desc', '') or '', description=getattr(e, 'custom_desc', '') or equipment_full_desc.get(_coerce_int(getattr(e, 'id', 0), 0), ''), custom_desc_ai=equipment_desc_ai.get(_coerce_int(getattr(e, 'id', 0), 0)), custom_desc_ai_source_hash=description_source_hash(getattr(e, 'custom_desc', '') or equipment_full_desc.get(_coerce_int(getattr(e, 'id', 0), 0), ''), kind='companion' if str(getattr(e, 'type', '')).lower() == 'companion' else 'item') if _coerce_int(getattr(e, 'id', 0), 0) in (description_ai_context.get('equipment_summary_ids') or set()) else None, uses_left=getattr(e, 'uses_left', None), evolution_stage=_coerce_int(getattr(e, 'evolution_stage', 0), 0), quantity=item_quantity_of(e), recovery_minutes=normalize_recovery_minutes(getattr(e, 'recovery_minutes', 0)), recovering=is_asset_recovering(e, now=recovery_now), recovery_started_at=getattr(e, 'recovery_started_at', None), recovery_ends_at=getattr(e, 'recovery_ends_at', None), _persistent_asset_state=build_persistent_asset_state('equipment', member_id, e), companion_lineage=[context for context in lineage_by_adventurer.get(member_id, []) if (context.get('current_target') or {}).get('source') == 'equipment' and _coerce_int((context.get('current_target') or {}).get('target_id'), 0) == _coerce_int(getattr(e, 'id', 0), 0)]) for e in getattr(member, 'equipment_items', None) or []]))
    return (snapshots, dining_context)
_S1_MEMBER_SNAPSHOT_ASSET_FIELDS = ('traits', 'skill_items', 'equipment_items')
_S1_MEMBER_SNAPSHOT_DATE_FIELDS = ('recovery_started_at', 'recovery_ends_at')

def _freeze_s1_member_generation_input(member_snapshots, *, item_dict, item_prompt_dict, use_condition_by_name, dining_context, name_alias, source='departure') -> dict:
    """Persist the exact in-memory main/validator inputs before consuming buffs."""
    rows = []
    for member in member_snapshots:
        row = {field.name: deepcopy(getattr(member, field.name)) for field in fields(S1ExpeditionMemberSnapshot) if field.name not in _S1_MEMBER_SNAPSHOT_ASSET_FIELDS}
        for key in _S1_MEMBER_SNAPSHOT_ASSET_FIELDS:
            row[key] = []
            for asset in getattr(member, key):
                asset_row = deepcopy(vars(asset))
                for date_key in _S1_MEMBER_SNAPSHOT_DATE_FIELDS:
                    value = asset_row.get(date_key)
                    if isinstance(value, datetime):
                        asset_row[date_key] = value.isoformat()
                row[key].append(asset_row)
        rows.append(row)
    return _clone_s1_generation_value({'version': 1, 'source': source, 'members': rows, 'item_dict': item_dict, 'item_prompt_dict': item_prompt_dict, 'use_condition_by_name': use_condition_by_name, 'dining_context': dining_context, 'name_alias': {str(key): value for key, value in name_alias.items()}})

def _restore_s1_member_generation_input(value: dict, *, member_ids: list[int]):
    """A malformed new snapshot is a technical failure, never a live-input retry."""
    frozen = _clone_s1_generation_value(value)
    if not isinstance(frozen, dict) or frozen.get('version') != 1 or (not isinstance(frozen.get('members'), list)) or ([row.get('id') for row in frozen['members'] if isinstance(row, dict)] != member_ids) or (len(frozen['members']) != len(member_ids)) or any((not isinstance(frozen.get(key), dict) for key in ('item_dict', 'item_prompt_dict', 'use_condition_by_name', 'dining_context', 'name_alias'))) or (set(frozen['name_alias']) != {str(member_id) for member_id in member_ids}):
        raise ValueError('member_generation_input_invalid')
    members = []
    for row in frozen['members']:
        row.pop('dm_instruction_revision', None)
        row.pop('dm_instruction', None)
        if set(row) != {field.name for field in fields(S1ExpeditionMemberSnapshot)}:
            raise ValueError('member_generation_fields_invalid')
        for key in _S1_MEMBER_SNAPSHOT_ASSET_FIELDS:
            if not isinstance(row.get(key), list):
                raise ValueError('member_generation_assets_invalid')
            assets = []
            for asset in row[key]:
                if not isinstance(asset, dict):
                    raise ValueError('member_generation_asset_invalid')
                for date_key in _S1_MEMBER_SNAPSHOT_DATE_FIELDS:
                    if isinstance(asset.get(date_key), str):
                        asset[date_key] = datetime.fromisoformat(asset[date_key])
                assets.append(SimpleNamespace(**asset))
            row[key] = assets
        members.append(S1ExpeditionMemberSnapshot(**row))
    return (members, frozen['item_dict'], frozen['item_prompt_dict'], frozen['use_condition_by_name'], frozen['dining_context'], {int(key): value for key, value in frozen['name_alias'].items()})

def _build_s1_companion_lineage_context(member_snapshots: list[S1ExpeditionMemberSnapshot]) -> list[dict]:
    """Freeze reviewed lineage separately from player-visible member snapshots."""
    result = []
    for member in member_snapshots or []:
        for item in getattr(member, 'equipment_items', []) or []:
            for context in getattr(item, 'companion_lineage', []) or []:
                if isinstance(context, dict):
                    frozen = _clone_s1_generation_value(context)
                    for key in ('predecessor', 'source_successor'):
                        row = frozen.get(key)
                        if isinstance(row, dict):
                            row.pop('description_ai', None)
                    for evolution in frozen.get('evolution_chain') or []:
                        if isinstance(evolution, dict):
                            evolution.pop('after_description_ai', None)
                    result.append(frozen)
    return result

def _filter_s1_member_snapshot_assets(member_snapshots: list[S1ExpeditionMemberSnapshot], excluded_refs: list[dict] | None) -> tuple[int, set[str]]:
    """按成员 + 类型 + 资产 ID 隔离，绝不按名称扩大到同名 sibling。"""
    excluded = {(_coerce_int(row.get('adventurer_id'), 0), str(row.get('asset_kind') or ''), _coerce_int(row.get('asset_id'), 0)) for row in excluded_refs or [] if isinstance(row, dict)}
    if not excluded:
        return (0, set())
    removed_count = 0
    removed_equipment_names: set[str] = set()
    for member in member_snapshots or []:
        member_id = _coerce_int(getattr(member, 'id', 0), 0)
        for field, asset_kind in (('equipment_items', 'equipment'), ('traits', 'trait'), ('skill_items', 'skill')):
            kept_rows = []
            for row in list(getattr(member, field, []) or []):
                key = (member_id, asset_kind, _coerce_int(getattr(row, 'id', 0), 0))
                if key in excluded:
                    removed_count += 1
                    if asset_kind == 'equipment':
                        name = str(getattr(row, 'name', '') or '').strip()
                        if name:
                            removed_equipment_names.add(name)
                    continue
                kept_rows.append(row)
            setattr(member, field, kept_rows)
    remaining_equipment_names = {str(getattr(row, 'name', '') or '').strip() for member in member_snapshots or [] for row in getattr(member, 'equipment_items', []) or [] if str(getattr(row, 'name', '') or '').strip()}
    return (removed_count, removed_equipment_names - remaining_equipment_names)

def _build_s1_audit_member_snapshot(member_snapshots: list[S1ExpeditionMemberSnapshot], item_dict: dict, *, use_condition_by_name: dict[str, dict | None] | None=None, name_alias: dict[int, str] | None=None) -> list[dict]:
    """冻结审核所需的出发成员资料；后续审核禁止回查成员最新状态。"""
    from plugins.adventurer_s1.services.s1_item_use_condition_service import EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD, is_equipment_hidden_from_expedition_ai, is_catalog_immutable_item
    cond_map = use_condition_by_name or {}
    result = []
    for member in member_snapshots or []:
        equipment = []
        for item in getattr(member, 'equipment_items', []) or []:
            name = str(getattr(item, 'name', '') or '').strip()
            if not name:
                continue
            entry = {'asset_id': _coerce_int(getattr(item, 'id', 0), 0), 'name': name, 'type': str(getattr(item, 'type', '') or ''), 'rarity': str(getattr(item, 'rarity', 'common') or 'common'), 'quantity': item_quantity_of(item), 'custom_desc': str(getattr(item, 'custom_desc', '') or ''), 'description': str(getattr(item, 'custom_desc', '') or item_dict.get(name, '') or ''), 'recovery_minutes': max(0, _coerce_int(getattr(item, 'recovery_minutes', 0), 0)), 'recovering': bool(getattr(item, 'recovering', False)), '_recovery_started_at': getattr(item, 'recovery_started_at').isoformat() if getattr(item, 'recovery_started_at', None) else None, '_recovery_ends_at': getattr(item, 'recovery_ends_at').isoformat() if getattr(item, 'recovery_ends_at', None) else None}
            if use_condition_by_name is not None:
                entry[EXPEDITION_AI_HIDDEN_EQUIPMENT_FIELD] = is_equipment_hidden_from_expedition_ai(cond_map.get(name))
                if is_catalog_immutable_item(cond_map.get(name)):
                    entry['immutable'] = True
            uses_left = getattr(item, 'uses_left', None)
            if uses_left is not None:
                entry['uses_left'] = uses_left
            evolution_stage = _coerce_int(getattr(item, 'evolution_stage', 0), 0)
            if evolution_stage > 0:
                entry['evolution_stage'] = evolution_stage
            entry['asset_state_hash'] = persistent_asset_state_hash('equipment', int(member.id), item)
            equipment.append(entry)
        skills = []
        for skill in getattr(member, 'skill_items', []) or []:
            if not bool(getattr(skill, 'equipped', True)):
                continue
            name = str(getattr(skill, 'name', '') or '').strip()
            if name:
                skill_entry = {'asset_id': _coerce_int(getattr(skill, 'id', 0), 0), 'name': name, 'description': str(getattr(skill, 'description', '') or ''), 'equipped': bool(getattr(skill, 'equipped', True)), 'recovery_minutes': max(0, _coerce_int(getattr(skill, 'recovery_minutes', 0), 0)), 'recovering': bool(getattr(skill, 'recovering', False)), '_recovery_started_at': getattr(skill, 'recovery_started_at').isoformat() if getattr(skill, 'recovery_started_at', None) else None, '_recovery_ends_at': getattr(skill, 'recovery_ends_at').isoformat() if getattr(skill, 'recovery_ends_at', None) else None}
                skill_entry['asset_state_hash'] = persistent_asset_state_hash('skill', int(member.id), skill)
                skills.append(skill_entry)
        traits = []
        for trait in getattr(member, 'traits', []) or []:
            if bool(getattr(trait, 'is_archived', False)) or not str(getattr(trait, 'name', '') or '').strip():
                continue
            trait_entry = {'asset_id': _coerce_int(getattr(trait, 'id', 0), 0), 'name': str(getattr(trait, 'name', '') or ''), 'description': str(getattr(trait, 'description', '') or ''), 'description_ai': str(getattr(trait, 'description_ai', '') or ''), 'is_archived': bool(getattr(trait, 'is_archived', False)), 'recovery_policy': str(getattr(trait, 'recovery_policy', 'none') or 'none'), 'recovery_stage': getattr(trait, 'recovery_stage', None), 'recovery_progress': _coerce_int(getattr(trait, 'recovery_progress', 0), 0), 'recovery_required': getattr(trait, 'recovery_required', None), 'asset_dependencies': getattr(trait, 'asset_dependencies', None)}
            trait_entry['asset_state_hash'] = persistent_asset_state_hash('trait', int(member.id), trait)
            traits.append(trait_entry)
        result.append({'adventurer_id': int(member.id), 'owner_id': int(member.owner_id), 'adventurer_name': (name_alias or {}).get(member.id, member.name), 'gender': member.gender, 'character_class': member.character_class, 'level': int(member.level), 'hp': int(member.hp), 'max_hp': int(member.max_hp), 'strength': int(member.strength), 'agility': int(member.agility), 'intelligence': int(member.intelligence), 'luck': int(member.luck), 'personality': member.personality, 'gold': member.gold, 'traits': traits, 'equipment': equipment, 'skills': skills})
    return result

def _load_s1_party_members_ordered(party_id: int) -> list:
    """按入队关联行固定顺序读取成员，保证 M1/M2 在同一小队内稳定。"""
    from sqlalchemy.orm import selectinload
    return AdventurerS1.query.join(AdventurerS1PartyMember, AdventurerS1PartyMember.adventurer_id == AdventurerS1.id).filter(AdventurerS1PartyMember.party_id == party_id).options(selectinload(AdventurerS1.traits), selectinload(AdventurerS1.equipment_items), selectinload(AdventurerS1.skill_items)).order_by(AdventurerS1PartyMember.created_at.asc(), AdventurerS1PartyMember.id.asc()).all()

def _reload_s1_party_members(party_id: int) -> list:
    return _load_s1_party_members_ordered(party_id)

def _build_item_catalog_context_for_members(members) -> tuple[dict, dict]:
    """批量预取随身物品的图鉴描述与使用条件，避免结算逐成员查库。"""
    names = set()
    for adv in members:
        for e in adv.equipment_items:
            names.add(e.name)
    if not names:
        return ({}, {})
    items = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(names)).all()
    return ({it.name: it.description for it in items}, {it.name: it.use_condition for it in items if it.use_condition})

def _build_item_dict_for_members(members) -> dict:
    """批量预取成员随身装备涉及的图鉴通用描述，避免 N+1。"""
    item_dict, _ = _build_item_catalog_context_for_members(members)
    return item_dict

def _ensure_s1_item_catalog(name: str, data: dict, existing_names: set | None=None) -> None:
    """把探险新产出的装备/道具登记进 S1 图鉴（若不存在）。本期不建商店条目。

    existing_names：调用方批量预取的图鉴已存在名集合（避免循环里单查）。
    若图鉴已有该物品且 data 中无 desc，会把图鉴的 description 回填到 data['desc']。
    """
    from plugins.adventurer_s1.services.s1_item_world_unique_service import infer_world_unique
    if existing_names is not None:
        if name in existing_names:
            if not data.get('desc'):
                existing_item = AdventurerS1Item.query.filter_by(name=name).first()
                if existing_item and existing_item.description:
                    data['desc'] = existing_item.description
            return
        existing_names.add(name)
    else:
        existing_item = AdventurerS1Item.query.filter_by(name=name).first()
        if existing_item:
            if not data.get('desc') and existing_item.description:
                data['desc'] = existing_item.description
            return
    db_manager.add(AdventurerS1Item(name=name, type=data.get('type', 'consumable'), description=data.get('desc', ''), rarity=data.get('rarity', 'common'), recovery_minutes=max(0, _coerce_int(data.get('recovery_minutes', 0), 0)), is_starter=False, world_unique=infer_world_unique(name, data.get('type', ''), data.get('desc', ''))))

def _build_s1_member_equipment_snapshot(member) -> dict[str, dict]:
    """出发时随身装备快照，供 consumed/used normalize 与次数型消耗迁移。"""
    snapshot: dict[str, dict] = {}
    for item in getattr(member, 'equipment_items', []) or []:
        name = getattr(item, 'name', None)
        if not name or name in snapshot:
            continue
        snapshot[name] = {'type': getattr(item, 'type', '') or '', 'uses_left': getattr(item, 'uses_left', None)}
    return snapshot

def _extend_s1_equipment_snapshot_with_new_items(snapshot: dict[str, dict], new_equipments) -> None:
    """把同条 member_update 的新增资产加入 consumed 稳定名称映射候选。"""
    for raw_name, raw_data in (normalize_s1_new_equipment_payload(new_equipments) or {}).items():
        data = raw_data if isinstance(raw_data, dict) else {}
        name, quantity = split_stack_suffix(raw_name, data.get('quantity', 1))
        clean_name = str(name or '').strip()
        if not clean_name or clean_name in snapshot:
            continue
        snapshot[clean_name] = {'type': str(data.get('type') or '').strip(), 'uses_left': data.get('uses_left'), 'quantity': quantity}

def _filter_misplaced_skill_names_from_new_traits(update: dict, skill_names: set[str]) -> None:
    """AI 偶发把已携带技能名误写进 new_traits，改记 used_skills 并剥离。"""
    if not skill_names:
        return
    raw_new = update.get('new_traits')
    if raw_new in (None, {}, []):
        return
    used_skills = list(update.get('used_skills') or [])
    if isinstance(raw_new, dict):
        remaining = {}
        for name, value in raw_new.items():
            clean_name = str(name or '').strip()
            if clean_name in skill_names:
                if clean_name not in used_skills:
                    used_skills.append(clean_name)
                continue
            remaining[name] = value
        update['new_traits'] = remaining
    elif isinstance(raw_new, list):
        remaining = []
        for entry in raw_new:
            if isinstance(entry, dict):
                clean_name = str(entry.get('name') or '').strip()
                if clean_name in skill_names:
                    if clean_name not in used_skills:
                        used_skills.append(clean_name)
                    continue
            elif isinstance(entry, str):
                clean_name = entry.strip()
                if clean_name in skill_names:
                    if clean_name not in used_skills:
                        used_skills.append(clean_name)
                    continue
            remaining.append(entry)
        update['new_traits'] = remaining
    if used_skills:
        update['used_skills'] = used_skills

def _apply_traits_to_s1(adv, update, *, exact_removed_rows: list[object] | None=None, frozen_member_snapshot: dict | None=None, asset_description_update_receipt: dict | None=None) -> list[AdventurerS1Trait]:
    equipped_skill_names = {s.name for s in getattr(adv, 'skill_items', []) or [] if getattr(s, 'name', None) and getattr(s, 'equipped', True)}
    _filter_misplaced_skill_names_from_new_traits(update, equipped_skill_names)
    removed = set(parse_member_removed_trait_names(update.get('removed_traits')))
    raw_new = update.get('new_traits')
    trait_updates = parse_member_new_trait_updates(raw_new, {}, member_name=adv.name) if raw_new else []
    exact_removed_ids = {int(row.id) for row in exact_removed_rows if getattr(row, 'id', None) is not None and getattr(row, 'adventurer_id', None) == adv.id} if exact_removed_rows is not None else None
    traits_by_name = {t.name: t for t in adv.traits if exact_removed_ids is None or getattr(t, 'id', None) not in exact_removed_ids}
    frozen_traits_by_name = {str(row.get('name') or '').strip(): row for row in (frozen_member_snapshot or {}).get('traits') or [] if isinstance(row, dict) and _coerce_int(row.get('asset_id'), 0) > 0 and str(row.get('name') or '').strip()}
    if exact_removed_ids is not None:
        for trait in exact_removed_rows or []:
            if getattr(trait, 'id', None) in exact_removed_ids:
                db_manager.delete_instance(trait)
    else:
        for name in removed:
            if any((u['name'] == name for u in trait_updates)):
                continue
            resolved = resolve_removed_trait_db_name(name, traits_by_name)
            if not resolved:
                continue
            t = traits_by_name.get(resolved)
            if t and (not t.is_archived):
                db_manager.delete_instance(t)
                traits_by_name.pop(resolved, None)
    for entry in trait_updates:
        name = entry['name']
        desc = entry.get('description') or ''
        t = traits_by_name.get(name)
        dependency_declared = bool(entry.get('asset_dependencies_declared'))
        resolved_dependencies = resolve_trait_asset_dependencies_for_write(entry.get('asset_dependencies'), adventurer_id=int(adv.id), equipment_items=getattr(adv, 'equipment_items', None) or [], skill_items=getattr(adv, 'skill_items', None) or [], frozen_member_snapshot=frozen_member_snapshot) if dependency_declared else None
        if t:
            current_desc = str(getattr(t, 'description', '') or '').strip()
            frozen_trait = frozen_traits_by_name.get(name) or {}
            desc, _description_ai = resolve_s1_finalized_asset_description(member_id=int(adv.id), asset_kind='trait', asset_id=_coerce_int(frozen_trait.get('asset_id') or getattr(t, 'id', 0), 0), frozen_full_description=frozen_trait.get('description') or current_desc, proposed_description=desc, receipt=asset_description_update_receipt)
            lore_append = str(entry.get('lore_append') or '').strip()
            dependency_changed = dependency_declared and resolved_dependencies != normalize_persisted_trait_asset_dependencies(getattr(t, 'asset_dependencies', None))
            if desc.strip() == current_desc and (not lore_append) and (not dependency_changed):
                current_app.logger.warning('S1 结算跳过同名同描述特质伪更新 adv=%s trait=%s', adv.id, name)
                continue
            sync_trait_description_fields(t, desc, lore_append=entry.get('lore_append'))
            if dependency_declared:
                t.asset_dependencies = resolved_dependencies
            t.is_archived = False
        else:
            t = AdventurerS1Trait(adventurer_id=adv.id, name=name, description='', asset_dependencies=resolved_dependencies or [])
            sync_trait_description_fields(t, desc, lore_append=entry.get('lore_append'))
            db_manager.add(t)
            traits_by_name[name] = t
    return list(traits_by_name.values())

def _collect_s1_asset_rename_changes(update: dict, frozen_member_snapshot: dict | None) -> list[dict]:
    """从稳定 ID 更新合同中提取确定性的同实例改名。"""
    frozen = frozen_member_snapshot if isinstance(frozen_member_snapshot, dict) else {}
    changes: list[dict] = []
    for update_field, snapshot_field, kind in (('equipment_updates', 'equipment', 'equipment'), ('skill_updates', 'skills', 'skill')):
        old_by_id = {row.get('asset_id'): str(row.get('name') or '').strip() for row in frozen.get(snapshot_field) or [] if isinstance(row, dict) and type(row.get('asset_id')) is int}
        id_field = 'equipment_id' if kind == 'equipment' else 'skill_id'
        for row in update.get(update_field) or []:
            if not isinstance(row, dict):
                continue
            asset_id = row.get(id_field)
            old_name = old_by_id.get(asset_id, '')
            new_name = str(row.get('name') or '').strip()
            if old_name and new_name and (old_name != new_name):
                changes.append({'asset_kind': kind, 'asset_id': asset_id, 'asset_location': 'equipment' if kind == 'equipment' else 'skill', 'old_name': old_name, 'new_name': new_name})
    return changes

def _apply_skills_to_s1(adv, update, *, exact_removed_rows: list[object] | None=None, frozen_member_snapshot: dict | None=None, asset_description_update_receipt: dict | None=None) -> None:
    removed = set(_normalize_removed_name_list_payload(update.get('removed_skills')) or [])
    raw_new_skills = update.get('new_skills')
    recovery_by_skill_name = {str(row.get('name') or '').strip(): max(0, _coerce_int(row.get('recovery_minutes'), 0)) for row in raw_new_skills or [] if isinstance(row, dict) and str(row.get('name') or '').strip()} if isinstance(raw_new_skills, list) else {}
    new_skills = _normalize_new_skills_payload(raw_new_skills, split_semantic_name_desc=False) or []
    for skill in new_skills:
        if isinstance(skill, dict):
            skill['recovery_minutes'] = recovery_by_skill_name.get(str(skill.get('name') or '').strip(), 0)
    exact_removed_ids = {int(row.id) for row in exact_removed_rows if getattr(row, 'id', None) is not None and getattr(row, 'adventurer_id', None) == adv.id} if exact_removed_rows is not None else None
    locked_skill_rows = _apply_existing_skill_updates_to_s1(adv, update, frozen_member_snapshot=frozen_member_snapshot, forbidden_skill_ids=exact_removed_ids or set(), asset_description_update_receipt=asset_description_update_receipt)
    if exact_removed_ids is not None:
        for skill in exact_removed_rows or []:
            if getattr(skill, 'id', None) in exact_removed_ids:
                db_manager.delete_instance(skill)
    if locked_skill_rows is not None:
        skills_by_name = {s.name: s for s in locked_skill_rows if exact_removed_ids is None or getattr(s, 'id', None) not in exact_removed_ids}
    else:
        skills_by_name = {s.name: s for s in adv.skill_items if exact_removed_ids is None or getattr(s, 'id', None) not in exact_removed_ids}
        try:
            db_skills = AdventurerS1Skill.query.filter_by(adventurer_id=adv.id).all()
            for ds in db_skills:
                if exact_removed_ids is not None and getattr(ds, 'id', None) in exact_removed_ids:
                    continue
                if ds.name not in skills_by_name:
                    skills_by_name[ds.name] = ds
        except Exception:
            pass
    equipped_count = sum((1 for s in skills_by_name.values() if s and s.equipped))
    new_names = {s.get('name') for s in new_skills if isinstance(s, dict) and s.get('name')}
    if exact_removed_ids is None:
        for name in removed:
            if name in new_names:
                continue
            s = skills_by_name.get(name)
            if s:
                db_manager.delete_instance(s)
                if s.equipped:
                    equipped_count -= 1
                skills_by_name.pop(name, None)
    from flask import current_app
    max_equipped = int(current_app.config.get('ADVENTURER_S1_MAX_EQUIPPED_SKILLS', 5))
    for skill in new_skills:
        if not isinstance(skill, dict) or not skill.get('name'):
            continue
        name = skill['name']
        desc = str(skill.get('desc') or skill.get('description') or '').strip()
        s = skills_by_name.get(name)
        if s:
            if name in removed and desc:
                s.description = desc
                s.recovery_minutes = max(0, _coerce_int(skill.get('recovery_minutes'), 0))
        else:
            equipped = equipped_count < max_equipped
            new_s = AdventurerS1Skill(adventurer_id=adv.id, name=name, description=desc or '技能效果待探索', equipped=equipped, recovery_minutes=max(0, _coerce_int(skill.get('recovery_minutes'), 0)))
            db_manager.add(new_s)
            skills_by_name[name] = new_s
            if equipped:
                equipped_count += 1

def _apply_existing_skill_updates_to_s1(adv, update: dict, *, frozen_member_snapshot: dict | None, forbidden_skill_ids: set[int], asset_description_update_receipt: dict | None=None) -> list | None:
    """按冻结 ID 原位修改既有技能；最终写回前锁行并拒绝状态漂移。"""
    raw_updates = update.get('skill_updates')
    if not raw_updates:
        return None
    if not isinstance(raw_updates, list) or not isinstance(frozen_member_snapshot, dict):
        raise RuntimeError(f'S1 技能专用修改缺少冻结上下文 adv={adv.id}')
    frozen_by_id = {_coerce_int(row.get('asset_id'), 0): row for row in frozen_member_snapshot.get('skills') or [] if isinstance(row, dict) and _coerce_int(row.get('asset_id'), 0) > 0}
    update_ids = [_coerce_int(row.get('skill_id'), 0) for row in raw_updates if isinstance(row, dict)]
    if len(update_ids) != len(raw_updates) or any((skill_id <= 0 for skill_id in update_ids)) or len(set(update_ids)) != len(update_ids):
        raise RuntimeError(f'S1 技能专用修改 ID 非法 adv={adv.id}')
    conflict_ids = set(update_ids) & set(forbidden_skill_ids)
    if conflict_ids:
        raise RuntimeError(f'S1 技能专用修改与永久删除冲突 adv={adv.id} ids={sorted(conflict_ids)}')
    locked_rows = AdventurerS1Skill.query.filter_by(adventurer_id=adv.id).order_by(AdventurerS1Skill.id.asc()).populate_existing().with_for_update().all()
    locked_by_id = {int(row.id): row for row in locked_rows}
    final_names = {_coerce_int(entry.get('skill_id'), 0): str(entry.get('name') or '').strip() for entry in raw_updates if isinstance(entry, dict)}
    if any((not name for name in final_names.values())) or len(set(final_names.values())) != len(final_names):
        raise RuntimeError(f'S1 技能专用修改名称非法或重复 adv={adv.id}')
    for skill_id, final_name in final_names.items():
        for other in locked_rows:
            if int(other.id) != skill_id and str(other.name or '').strip() == final_name:
                raise RuntimeError(f'S1 技能专用修改名称冲突 adv={adv.id} skill_id={skill_id}')
    effective_updates = []
    for entry in raw_updates:
        skill_id = _coerce_int(entry.get('skill_id'), 0)
        frozen = frozen_by_id.get(skill_id)
        current = locked_by_id.get(skill_id)
        if frozen is None or current is None:
            raise RuntimeError(f'S1 技能专用修改目标不存在或不在出发快照 adv={adv.id} skill_id={skill_id}')
        if str(entry.get('name') or '').strip() == str(frozen.get('name') or '').strip() and str(entry.get('desc') or '').strip() == str(frozen.get('description') or '').strip() and (max(0, _coerce_int(entry.get('recovery_minutes'), 0)) == max(0, _coerce_int(frozen.get('recovery_minutes'), 0))):
            continue
        frozen_state = (str(frozen.get('name') or '').strip(), str(frozen.get('description') or '').strip(), bool(frozen.get('equipped', True)), max(0, _coerce_int(frozen.get('recovery_minutes', 0), 0)), frozen.get('_recovery_started_at'), frozen.get('_recovery_ends_at'))
        current_state = (str(current.name or '').strip(), str(current.description or '').strip(), bool(current.equipped), max(0, _coerce_int(current.recovery_minutes, 0)), current.recovery_started_at.isoformat() if current.recovery_started_at else None, current.recovery_ends_at.isoformat() if current.recovery_ends_at else None)
        if current_state != frozen_state:
            raise RuntimeError(f'S1 技能专用修改冻结状态已漂移 adv={adv.id} skill_id={skill_id}')
        full_description, _description_ai = resolve_s1_finalized_asset_description(member_id=int(adv.id), asset_kind='skill', asset_id=skill_id, frozen_full_description=frozen.get('description'), proposed_description=entry.get('desc'), receipt=asset_description_update_receipt)
        current.name = str(entry.get('name') or '').strip()
        current.description = full_description
        current.recovery_minutes = max(0, _coerce_int(entry.get('recovery_minutes'), 0))
        entry['desc'] = full_description
        effective_updates.append(entry)
    update['skill_updates'] = effective_updates
    return locked_rows

def _companion_consumed_stage_floors(eq_items, consumed_names: list[str]) -> dict[str, int]:
    """预读本次 consumed 将扣到的 companion 阶数（FIFO 模拟，不删行）。"""
    from plugins.adventurer_s1.s1_item_types import coerce_evolution_stage, companion_allows_duplicate_name_instances
    floors: dict[str, int] = {}
    pool = list(eq_items)
    for name in consumed_names:
        candidates = sorted([e for e in pool if e.name == name], key=lambda e: e.id)
        if not candidates:
            continue
        target = candidates[0]
        if not companion_allows_duplicate_name_instances(target.type or ''):
            continue
        clean = str(name or '').strip()
        if not clean:
            continue
        stage = coerce_evolution_stage(target.evolution_stage)
        floors[clean] = max(floors.get(clean, 0), stage)
        pool.remove(target)
    return floors

def _resolve_equipment_for_settlement_action(eq_items, name: str, *, exclude_object_ids: set[int] | None=None, prefer_exclude_object_ids: set[int] | None=None):
    """结算资产扣减：硬排除受保护实例，并优先扣 settlement 前已有的行。"""
    hard_exclude_object_ids = exclude_object_ids or set()
    soft_exclude_object_ids = prefer_exclude_object_ids or set()
    candidates = [e for e in eq_items if e.name == name and id(e) not in hard_exclude_object_ids]
    preferred_candidates = [e for e in candidates if id(e) not in soft_exclude_object_ids]
    if preferred_candidates:
        candidates = preferred_candidates
    if not candidates:
        return None
    return min(candidates, key=lambda e: e.id)

def _apply_existing_equipment_updates_to_s1(adv, update: dict, item_dict: dict, *, frozen_member_snapshot: dict | None, catalog_by_name: dict | None, asset_description_update_receipt: dict | None=None) -> set[int]:
    """按冻结 ID 原位修改既有装备；数量仍由独立消耗/发放协议管理。"""
    raw_updates = update.get('equipment_updates')
    if not raw_updates:
        return set()
    if not isinstance(raw_updates, list) or not isinstance(frozen_member_snapshot, dict):
        raise RuntimeError(f'S1 装备专用修改缺少冻结上下文 adv={adv.id}')
    frozen_by_id = {_coerce_int(row.get('asset_id'), 0): row for row in frozen_member_snapshot.get('equipment') or [] if isinstance(row, dict) and _coerce_int(row.get('asset_id'), 0) > 0}
    update_ids = [_coerce_int(row.get('equipment_id'), 0) for row in raw_updates if isinstance(row, dict)]
    if len(update_ids) != len(raw_updates) or any((equipment_id <= 0 for equipment_id in update_ids)) or len(set(update_ids)) != len(update_ids):
        raise RuntimeError(f'S1 装备专用修改 ID 非法 adv={adv.id}')
    from plugins.adventurer_s1.services.s1_item_use_condition_service import catalog_immutable_item_names
    immutable_names = catalog_immutable_item_names({str(frozen_by_id[equipment_id].get('name') or '').strip() for equipment_id in update_ids if equipment_id in frozen_by_id})
    immutable_ids = {equipment_id for equipment_id, frozen in frozen_by_id.items() if frozen.get('immutable') is True or str(frozen.get('name') or '').strip() in immutable_names}
    raw_updates = [entry for entry in raw_updates if _coerce_int(entry.get('equipment_id'), 0) not in immutable_ids]
    update['equipment_updates'] = raw_updates
    update_ids = [_coerce_int(entry.get('equipment_id'), 0) for entry in raw_updates]
    if not raw_updates:
        return set()
    locked_rows = AdventurerS1Equipment.query.filter(AdventurerS1Equipment.adventurer_id == adv.id).order_by(AdventurerS1Equipment.id.asc()).populate_existing().with_for_update().all()
    locked_by_id = {int(row.id): row for row in locked_rows}
    final_names = {_coerce_int(entry.get('equipment_id'), 0): str(entry.get('name') or '').strip() for entry in raw_updates if isinstance(entry, dict)}
    if any((not name for name in final_names.values())) or len(set(final_names.values())) != len(final_names):
        raise RuntimeError(f'S1 装备专用修改名称非法或重复 adv={adv.id}')
    for equipment_id, final_name in final_names.items():
        for other in locked_rows:
            if int(other.id) != equipment_id and str(other.name or '').strip() == final_name:
                raise RuntimeError(f'S1 装备专用修改名称冲突 adv={adv.id} equipment_id={equipment_id}')
    proposed_names = set(final_names.values())
    catalog_existing = set(catalog_by_name or {}) & proposed_names
    catalog_missing = proposed_names - catalog_existing
    if catalog_missing:
        catalog_existing.update((row[0] for row in AdventurerS1Item.query.with_entities(AdventurerS1Item.name).filter(AdventurerS1Item.name.in_(sorted(catalog_missing))).all()))
    for entry in raw_updates:
        equipment_id = _coerce_int(entry.get('equipment_id'), 0)
        frozen = frozen_by_id.get(equipment_id)
        current = locked_by_id.get(equipment_id)
        if frozen is None or current is None:
            raise RuntimeError(f'S1 装备专用修改目标不存在或不在出发快照 adv={adv.id} equipment_id={equipment_id}')
        frozen_state = (str(frozen.get('name') or '').strip(), str(frozen.get('type') or '').strip(), str(frozen.get('rarity') or 'common').strip(), frozen.get('uses_left'), _coerce_int(frozen.get('evolution_stage', 0), 0), max(1, _coerce_int(frozen.get('quantity', 1), 1)), max(0, _coerce_int(frozen.get('recovery_minutes', 0), 0)), frozen.get('_recovery_started_at'), frozen.get('_recovery_ends_at'))
        current_state = (str(current.name or '').strip(), str(current.type or '').strip(), str(current.rarity or 'common').strip(), current.uses_left, _coerce_int(current.evolution_stage, 0), item_quantity_of(current), max(0, _coerce_int(current.recovery_minutes, 0)), current.recovery_started_at.isoformat() if current.recovery_started_at else None, current.recovery_ends_at.isoformat() if current.recovery_ends_at else None)
        if current_state != frozen_state:
            raise RuntimeError(f'S1 装备专用修改冻结状态已漂移 adv={adv.id} equipment_id={equipment_id}')
        name = str(entry.get('name') or '').strip()
        frozen_description = frozen.get('description') or frozen.get('custom_desc')
        full_description, _description_ai = resolve_s1_finalized_asset_description(member_id=int(adv.id), asset_kind='equipment', asset_id=equipment_id, frozen_full_description=frozen_description, proposed_description=entry.get('desc'), receipt=asset_description_update_receipt)
        frozen_custom_desc = str(frozen.get('custom_desc') or '')
        current_custom_desc = str(current.custom_desc or '')
        if current_custom_desc != frozen_custom_desc:
            rebased_description = rebase_s1_concurrent_public_description(frozen_description, current_custom_desc, full_description)
            if rebased_description is None:
                raise RuntimeError(f'S1 装备专用修改冻结状态已漂移 adv={adv.id} equipment_id={equipment_id}')
            full_description = rebased_description
            logger.info('S1 装备专用修改已合并并发描述更新 adv=%s equipment_id=%s', adv.id, equipment_id)
        data = {'type': str(current.type or '').strip(), 'rarity': str(entry.get('rarity') or '').strip(), 'desc': full_description, 'recovery_minutes': max(0, _coerce_int(entry.get('recovery_minutes'), 0))}
        proposed_stage = _coerce_int(current.evolution_stage, 0)
        _ensure_s1_item_catalog(name, data, catalog_existing)
        current.name = name
        current.type = data['type']
        current.rarity = data['rarity']
        current.custom_desc = data['desc']
        current.uses_left = entry.get('uses_left')
        current.evolution_stage = proposed_stage
        current.recovery_minutes = data['recovery_minutes']
        entry['type'] = data['type']
        entry['desc'] = data['desc']
        entry['evolution_stage'] = proposed_stage
        item_dict[name] = data['desc']
    return set(update_ids)

def _apply_asset_recovery_triggers_to_s1(adv, update: dict, *, frozen_member_snapshot: dict | None) -> None:
    """Start all validated asset timers in parallel under final row locks."""
    raw_triggers = update.get('recovery_triggers')
    if not raw_triggers:
        return
    if not isinstance(raw_triggers, list) or not isinstance(frozen_member_snapshot, dict):
        raise RuntimeError(f'S1 恢复触发缺少冻结上下文 adv={adv.id}')
    from plugins.adventurer_s1.services.s1_asset_recovery_service import start_asset_recovery
    frozen_by_kind = {'skill': {_coerce_int(row.get('asset_id'), 0): row for row in frozen_member_snapshot.get('skills') or [] if isinstance(row, dict) and _coerce_int(row.get('asset_id'), 0) > 0}, 'equipment': {_coerce_int(row.get('asset_id'), 0): row for row in frozen_member_snapshot.get('equipment') or [] if isinstance(row, dict) and _coerce_int(row.get('asset_id'), 0) > 0}}
    trigger_ids = {'skill': set(), 'equipment': set()}
    for trigger in raw_triggers:
        if not isinstance(trigger, dict) or trigger.get('asset_kind') not in trigger_ids:
            raise RuntimeError(f'S1 恢复触发结构非法 adv={adv.id}')
        asset_id = _coerce_int(trigger.get('asset_id'), 0)
        if asset_id <= 0 or asset_id in trigger_ids[trigger['asset_kind']]:
            raise RuntimeError(f'S1 恢复触发资产 ID 非法或重复 adv={adv.id}')
        trigger_ids[trigger['asset_kind']].add(asset_id)
    locked_by_kind: dict[str, dict[int, object]] = {'skill': {}, 'equipment': {}}
    if trigger_ids['skill']:
        rows = AdventurerS1Skill.query.filter(AdventurerS1Skill.adventurer_id == adv.id, AdventurerS1Skill.id.in_(sorted(trigger_ids['skill']))).order_by(AdventurerS1Skill.id.asc()).populate_existing().with_for_update().all()
        locked_by_kind['skill'] = {int(row.id): row for row in rows}
    if trigger_ids['equipment']:
        rows = AdventurerS1Equipment.query.filter(AdventurerS1Equipment.adventurer_id == adv.id, AdventurerS1Equipment.id.in_(sorted(trigger_ids['equipment']))).order_by(AdventurerS1Equipment.id.asc()).populate_existing().with_for_update().all()
        locked_by_kind['equipment'] = {int(row.id): row for row in rows}
    updated_minutes = {('skill', _coerce_int(row.get('skill_id'), 0)): max(0, _coerce_int(row.get('recovery_minutes'), 0)) for row in update.get('skill_updates') or [] if isinstance(row, dict)}
    updated_minutes.update({('equipment', _coerce_int(row.get('equipment_id'), 0)): max(0, _coerce_int(row.get('recovery_minutes'), 0)) for row in update.get('equipment_updates') or [] if isinstance(row, dict)})
    recovery_now = _portable_utc_now()
    for trigger in raw_triggers:
        asset_kind = trigger['asset_kind']
        asset_id = _coerce_int(trigger.get('asset_id'), 0)
        frozen = frozen_by_kind[asset_kind].get(asset_id)
        current = locked_by_kind[asset_kind].get(asset_id)
        if frozen is None:
            raise RuntimeError(f'S1 恢复触发目标不在出发快照 adv={adv.id} kind={asset_kind} asset_id={asset_id}')
        if current is None:
            continue
        frozen_minutes = max(0, _coerce_int(frozen.get('recovery_minutes'), 0))
        effective_minutes = updated_minutes.get((asset_kind, asset_id), frozen_minutes)
        if effective_minutes <= 0 or frozen.get('recovering') is True:
            raise RuntimeError(f'S1 恢复触发与冻结可用性冲突 adv={adv.id} kind={asset_kind} asset_id={asset_id}')
        expected_current_minutes = effective_minutes
        current_started = current.recovery_started_at.isoformat() if current.recovery_started_at else None
        current_ends = current.recovery_ends_at.isoformat() if current.recovery_ends_at else None
        if max(0, _coerce_int(current.recovery_minutes, 0)) != expected_current_minutes or current_started != frozen.get('_recovery_started_at') or current_ends != frozen.get('_recovery_ends_at'):
            raise RuntimeError(f'S1 恢复触发冻结状态已漂移 adv={adv.id} kind={asset_kind} asset_id={asset_id}')
        start_asset_recovery(current, recovery_minutes=effective_minutes, now=recovery_now)

def _apply_equipment_to_s1(adv, update, item_dict, *, party_transfers: dict[str, tuple] | None=None, party_holders_map: dict[str, set[int]] | None=None, catalog_by_name: dict | None=None, holder_custom_desc_flags: dict[tuple[int, str], bool] | None=None, holders_map: dict[str, set[int]] | None=None, expedition_log_id: int | None=None, item_use_conditions: dict[str, dict] | None=None, previous_class: str | None=None, frozen_member_snapshot: dict | None=None, asset_description_update_receipt: dict | None=None) -> None:
    from plugins.adventurer_s1.services.s1_item_grant_service import apply_missing_raw_item_uses_left_default
    eq_items = list(adv.equipment_items)
    updated_equipment_ids = _apply_existing_equipment_updates_to_s1(adv, update, item_dict, frozen_member_snapshot=frozen_member_snapshot, catalog_by_name=catalog_by_name, asset_description_update_receipt=asset_description_update_receipt)
    updated_equipment_object_ids = {id(e) for e in eq_items if _coerce_int(getattr(e, 'id', None), 0) in updated_equipment_ids}
    preexisting_equipment_ids = {int(e.id) for e in eq_items if getattr(e, 'id', None) is not None}
    current_snapshot = {}
    expedition_related_id = str(int(expedition_log_id)) if expedition_log_id is not None else None
    expedition_related_type = 'expedition_log' if expedition_related_id is not None else None
    for e in eq_items:
        if e.name not in current_snapshot:
            current_snapshot[e.name] = {'type': e.type, 'uses_left': e.uses_left, 'quantity': item_quantity_of(e)}

    def _remove_equipped(e):
        if e in eq_items:
            eq_items.remove(e)

    def _consume_once(e):
        """对带次数的物品扣 1 次，耗尽则删除；无次数的消耗品直接删除。返回是否已删除。"""
        quantity = item_quantity_of(e)
        if quantity > 1:
            e.quantity = quantity - 1
            db_manager.add(AdventurerS1ItemLog(adventurer_id=adv.id, item_name=e.name, item_type=e.type or '', item_rarity=e.rarity or 'common', change_type='consume', quantity=1, source_type='expedition', description=f'探险消耗「{e.name}」', related_id=expedition_related_id, related_type=expedition_related_type))
            return False
        if e.uses_left is not None:
            e.uses_left = _coerce_int(e.uses_left, 0) - 1
            if e.uses_left <= 0:
                db_manager.delete_instance(e)
                _remove_equipped(e)
                db_manager.add(AdventurerS1ItemLog(adventurer_id=adv.id, item_name=e.name, item_type=e.type or '', item_rarity=e.rarity or 'common', change_type='consume', quantity=1, source_type='expedition', description=f'探险消耗「{e.name}」', related_id=expedition_related_id, related_type=expedition_related_type))
                return True
            return False
        db_manager.delete_instance(e)
        _remove_equipped(e)
        db_manager.add(AdventurerS1ItemLog(adventurer_id=adv.id, item_name=e.name, item_type=e.type or '', item_rarity=e.rarity or 'common', change_type='consume', quantity=1, source_type='expedition', description=f'探险消耗「{e.name}」', related_id=expedition_related_id, related_type=expedition_related_type))
        return True

    def _consume_declared_quantity(name: str, requested_quantity: int) -> int:
        """Apply one structured asset deduction and return the unmet quantity."""
        remaining = max(1, min(_coerce_int(requested_quantity, 1), 999999))
        while remaining > 0:
            e = _resolve_equipment_for_settlement_action(eq_items, name, exclude_object_ids=updated_equipment_object_ids, prefer_exclude_object_ids=newly_granted_item_object_ids)
            if not e or e not in eq_items:
                return remaining
            stack_quantity = item_quantity_of(e)
            if stack_quantity > 1:
                deducted = min(stack_quantity, remaining)
                if deducted < stack_quantity:
                    e.quantity = stack_quantity - deducted
                else:
                    db_manager.delete_instance(e)
                    _remove_equipped(e)
            elif e.uses_left is not None:
                uses_left = max(1, _coerce_int(e.uses_left, 1))
                deducted = min(uses_left, remaining)
                if deducted < uses_left:
                    e.uses_left = uses_left - deducted
                else:
                    db_manager.delete_instance(e)
                    _remove_equipped(e)
            else:
                deducted = 1
                db_manager.delete_instance(e)
                _remove_equipped(e)
            db_manager.add(AdventurerS1ItemLog(adventurer_id=adv.id, item_name=e.name, item_type=e.type or '', item_rarity=e.rarity or 'common', change_type='consume', quantity=deducted, source_type='expedition', description=f'探险消耗「{e.name}」', related_id=expedition_related_id, related_type=expedition_related_type))
            remaining -= deducted
        return remaining

    def _track_snapshot(e):
        snap = {'type': e.type, 'uses_left': e.uses_left, 'quantity': item_quantity_of(e)}
        if e.name not in current_snapshot:
            current_snapshot[e.name] = snap
        else:
            current_snapshot[e.name].update(snap)
    new_eqs = normalize_s1_new_equipment_payload(update.get('new_equipments')) or {}
    normalized_new_eqs = {}
    for raw_name, data in new_eqs.items():
        if not isinstance(data, dict):
            data = {}
        clean_name, quantity = split_stack_suffix(raw_name, data.get('quantity', 1))
        data = dict(data)
        data['quantity'] = quantity
        catalog = (catalog_by_name or {}).get(clean_name)
        data = apply_missing_raw_item_uses_left_default(clean_name, data, catalog_use_condition=catalog.use_condition if catalog is not None and isinstance(catalog.use_condition, dict) else None)
        normalized_new_eqs[clean_name] = data
    new_eqs = normalized_new_eqs
    _extend_s1_equipment_snapshot_with_new_items(current_snapshot, new_eqs)
    limit = _coerce_int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5), 5)
    raw_consumed_equipments = update.get('consumed_equipments', [])
    consumed_entries = normalize_s1_consumed_equipment_payload(raw_consumed_equipments, current_snapshot)
    legacy_full_quantities: dict[str, int] = {}
    legacy_pool = list(eq_items)
    raw_legacy_entries = raw_consumed_equipments if isinstance(raw_consumed_equipments, list) else [raw_consumed_equipments]
    for raw_entry in raw_legacy_entries[:100]:
        if not isinstance(raw_entry, str):
            continue
        raw_name = raw_entry.strip()
        if not raw_name or raw_name[0] in ('{', '['):
            continue
        base_name, explicit_quantity = split_stack_suffix(raw_name, 1)
        if base_name != raw_name or explicit_quantity != 1:
            continue
        normalized_legacy = normalize_s1_consumed_equipment_payload([raw_name], current_snapshot)
        if not normalized_legacy:
            continue
        stable_name = normalized_legacy[0]['name']
        target = _resolve_equipment_for_settlement_action(legacy_pool, stable_name, exclude_object_ids=updated_equipment_object_ids)
        if target is None:
            continue
        available = item_quantity_of(target)
        if available <= 1 and target.uses_left is not None:
            available = max(1, _coerce_int(target.uses_left, 1))
        legacy_full_quantities[stable_name] = legacy_full_quantities.get(stable_name, 0) + max(1, available)
        legacy_pool.remove(target)
    if legacy_full_quantities:
        for entry in consumed_entries:
            legacy_quantity = legacy_full_quantities.get(entry['name'])
            if legacy_quantity is not None:
                entry['quantity'] = max(entry['quantity'], legacy_quantity)
    from plugins.adventurer_s1.services.s1_item_use_condition_service import is_class_transfer_invitation
    new_class = _normalize_new_class(update.get('new_class'))
    guarded_consumed_entries = []
    for entry in consumed_entries:
        name = entry['name']
        use_condition = (item_use_conditions or {}).get(name)
        is_transfer_invitation = is_class_transfer_invitation(use_condition)
        party_transfer = (party_transfers or {}).get(name)
        is_party_transfer_source = bool(party_transfer and int(party_transfer[0]) == int(adv.id))
        if is_transfer_invitation and (not new_class) and (not is_party_transfer_source):
            current_app.logger.warning('S1 结算跳过无 new_class 的孤立转职凭证消耗 adv=%s item=%s', adv.id, name)
            continue
        if is_transfer_invitation:
            entry = {'name': name, 'quantity': 1}
        guarded_consumed_entries.append(entry)
    consumed_entries = guarded_consumed_entries
    consumed_names = [entry['name'] for entry in consumed_entries]
    consumed_name_set = set(consumed_names)
    update.pop('used_equipments', None)
    update['consumed_equipments'] = consumed_entries
    companion_consumed_names = []
    for entry in consumed_entries:
        matching_count = sum((1 for item in eq_items if item.name == entry['name'] and id(item) not in updated_equipment_object_ids))
        companion_consumed_names.extend([entry['name']] * min(entry['quantity'], matching_count))
    companion_consumed_stage_floors = _companion_consumed_stage_floors([item for item in eq_items if id(item) not in updated_equipment_object_ids], companion_consumed_names)
    inherited_replacement_descs: dict[str, str] = {}
    for name in consumed_name_set & set(new_eqs):
        data = new_eqs.get(name)
        data = data if isinstance(data, dict) else {}
        if str(data.get('desc') or data.get('description') or '').strip():
            continue
        replaced_item = _resolve_equipment_for_settlement_action(eq_items, name, exclude_object_ids=updated_equipment_object_ids)
        inherited_desc = str(getattr(replaced_item, 'custom_desc', '') or item_dict.get(name, '') or '').strip()
        if inherited_desc:
            inherited_replacement_descs[name] = inherited_desc
    newly_granted_item_object_ids: set[int] = set()
    post_grant_consumptions: list[dict] = []

    def _consume_party_transfer_source(entry: dict) -> bool:
        transfer = (party_transfers or {}).get(entry['name'])
        if not transfer or int(transfer[0]) != int(adv.id):
            return False
        source_id = int(transfer[2])
        expected_quantity = int(transfer[3])
        if source_id in updated_equipment_ids:
            raise RuntimeError(f"S1 结算禁止转交本轮专用修改的装备 adv={adv.id} item={entry['name']} source_id={source_id}")
        if int(entry['quantity']) != expected_quantity:
            raise RuntimeError(f"队内转交数量已变化 adv={adv.id} item={entry['name']}")
        source = AdventurerS1Equipment.query.filter_by(id=source_id, adventurer_id=adv.id, name=entry['name']).populate_existing().with_for_update().first()
        if source is None or item_quantity_of(source) != expected_quantity:
            raise RuntimeError(f"队内转交来源已变化 adv={adv.id} item={entry['name']} source_id={source_id}")
        deleted, delete_error = db_manager.delete_instance(source)
        if not deleted:
            raise RuntimeError(f'队内转交来源删除失败 source_id={source_id}: {delete_error}')
        _remove_equipped(source)
        db_manager.add(AdventurerS1ItemLog(adventurer_id=adv.id, item_name=source.name, item_type=source.type or '', item_rarity=source.rarity or 'common', change_type='consume', quantity=expected_quantity, source_type='expedition', description=f'探险中转交「{source.name}」', related_id=expedition_related_id, related_type=expedition_related_type))
        return True
    for entry in consumed_entries:
        if _consume_party_transfer_source(entry):
            continue
        remaining = _consume_declared_quantity(entry['name'], entry['quantity'])
        if remaining > 0:
            post_grant_consumptions.append({'name': entry['name'], 'quantity': remaining})
    count = len(eq_items)
    granted_equipment_names: set[str] = set()
    granted_warehouse_names: set[str] = set()
    settled_new_eqs: dict[str, dict] = {}
    discarded_equipments: list[dict] = []
    from plugins.adventurer_s1.seed import is_herbalist_item
    herb_limit = _coerce_int(current_app.config.get('ADVENTURER_S1_EQUIPMENT_LIMIT', 5), 3)
    herb_count = sum((1 for e in eq_items if is_herbalist_item(e.name)))
    catalog_existing: set = set()
    if new_eqs:
        catalog_existing = {row[0] for row in AdventurerS1Item.query.with_entities(AdventurerS1Item.name).filter(AdventurerS1Item.name.in_(list(new_eqs.keys()))).all()}
    from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item, is_warehouse_capacity_error
    from plugins.adventurer_s1.s1_item_types import clamp_companion_evolution_stage, coerce_evolution_stage, companion_allows_duplicate_name_instances
    warehouse_quantities_before_grant: dict[int, int] = {}
    if new_eqs:
        warehouse_quantities_before_grant = {int(item.id): item_quantity_of(item) for item in AdventurerS1WarehouseItem.query.filter(AdventurerS1WarehouseItem.adventurer_id == adv.id, AdventurerS1WarehouseItem.name.in_(list(new_eqs.keys()))).all()}
    for name, data in new_eqs.items():
        if not isinstance(data, dict):
            data = {}
        else:
            data = dict(data)
        transfer = (party_transfers or {}).get(name)
        transfer_from_id = int(transfer[0]) if transfer and int(transfer[1]) == int(adv.id) else None
        transfer_source_item_id = int(transfer[2]) if transfer_from_id is not None else None
        transfer_quantity = int(transfer[3]) if transfer_from_id is not None else None
        if transfer_from_id is not None and len(transfer) > 4 and isinstance(transfer[4], dict):
            source_snapshot = transfer[4]
            for field in ('type', 'rarity', 'quantity', 'uses_left', 'evolution_stage', 'recovery_minutes', 'recovery_started_at', 'recovery_ends_at'):
                data[field] = source_snapshot.get(field)
            if source_snapshot.get('recovery_triggered') is True:
                transfer_recovery_started_at = _portable_utc_now()
                data['recovery_started_at'] = transfer_recovery_started_at
                data['recovery_ends_at'] = transfer_recovery_started_at + timedelta(minutes=max(0, _coerce_int(source_snapshot.get('recovery_minutes'), 0)))
        inherited_desc = inherited_replacement_descs.get(name)
        if inherited_desc and (not str(data.get('desc') or data.get('description') or '').strip()):
            data['desc'] = inherited_desc
            new_eqs[name] = data
        item_type = str(data.get('type') or '').strip()
        stage_floor = companion_consumed_stage_floors.get(name, 0)
        if companion_allows_duplicate_name_instances(item_type) and stage_floor > 0:
            proposed = coerce_evolution_stage(data['evolution_stage']) if 'evolution_stage' in data else 0
            data['evolution_stage'] = clamp_companion_evolution_stage(stage_floor, proposed)
        settled_new_eqs[name] = data
        is_herb = is_herbalist_item(name)
        if count < limit and (not (is_herb and herb_count >= herb_limit)):
            dest = 'equipment'
        else:
            dest = 'warehouse'
        granted_destination, grant_err = grant_raw_item(adv, name, data, source_type='expedition', description=f'探险获得「{name}」', related_id=expedition_related_id, related_type=expedition_related_type, force_destination=dest, allow_same_name_stack_merge=True, world_unique_transfer_from_adventurer_id=transfer_from_id, world_unique_transfer_source_item_id=transfer_source_item_id, world_unique_transfer_quantity=transfer_quantity)
        if grant_err:
            if is_warehouse_capacity_error(adv.id, grant_err):
                settled_new_eqs.pop(name, None)
                discarded_equipments.append({'name': name, 'quantity': max(1, _coerce_int(data.get('quantity', 1), 1)), 'reason': 'warehouse_full', 'message': f'仓库已满，未能存入「{name}」，该物品已丢弃'})
                current_app.logger.warning('S1 结算仓库已满，物品已丢弃 adv=%s name=%s quantity=%s', adv.id, name, discarded_equipments[-1]['quantity'])
                continue
            current_app.logger.warning('S1 结算发放物品失败 adv=%s name=%s: %s', adv.id, name, grant_err)
            continue
        if granted_destination == 'equipment':
            granted_equipment_names.add(name)
        elif granted_destination == 'warehouse':
            granted_warehouse_names.add(name)
    if new_eqs:
        update['new_equipments'] = settled_new_eqs
    if discarded_equipments:
        update['discarded_equipments'] = discarded_equipments
    else:
        update.pop('discarded_equipments', None)
    if granted_equipment_names:
        refreshed_items = AdventurerS1Equipment.query.filter(AdventurerS1Equipment.adventurer_id == adv.id, AdventurerS1Equipment.name.in_(granted_equipment_names)).all()
        for item in refreshed_items:
            _track_snapshot(item)
            item_id = int(item.id)
            if item_id not in preexisting_equipment_ids:
                newly_granted_item_object_ids.add(id(item))
            if item not in eq_items:
                eq_items.append(item)
    if granted_warehouse_names:
        refreshed_warehouse_items = AdventurerS1WarehouseItem.query.filter(AdventurerS1WarehouseItem.adventurer_id == adv.id, AdventurerS1WarehouseItem.name.in_(granted_warehouse_names)).all()
        for item in refreshed_warehouse_items:
            before_quantity = warehouse_quantities_before_grant.get(int(item.id))
            if before_quantity is not None and item_quantity_of(item) <= before_quantity:
                continue
            newly_granted_item_object_ids.add(id(item))
            if item not in eq_items:
                eq_items.append(item)
    for entry in post_grant_consumptions:
        remaining = _consume_declared_quantity(entry['name'], entry['quantity'])
        conflicting_updated_ids = sorted((_coerce_int(getattr(item, 'id', None), 0) for item in eq_items if id(item) in updated_equipment_object_ids and item.name == entry['name']))
        if remaining > 0 and conflicting_updated_ids:
            raise RuntimeError(f"S1 结算禁止消耗本轮专用修改的装备 adv={adv.id} item={entry['name']} equipment_ids={conflicting_updated_ids}")

def _append_s1_settlement_discard_events(full_log: dict, updates: dict[int, dict], members: list) -> list[str]:
    """把后端确定的未到账结果写成最终系统事件，供提示与申诉证据复用。"""
    member_names = {int(member.id): str(member.name or f'冒险者 #{member.id}') for member in members if getattr(member, 'id', None) is not None}
    existing_events = list(full_log.get('events') or [])
    existing_event_set = set(existing_events)
    notices: list[str] = []
    for member_id in sorted(updates):
        update = updates.get(member_id) or {}
        discarded = update.get('discarded_equipments')
        if not isinstance(discarded, list):
            continue
        for entry in discarded[:100]:
            if not isinstance(entry, dict):
                continue
            reason = entry.get('reason')
            if reason not in ('warehouse_full', 'world_unique_conflict'):
                continue
            item_name = str(entry.get('name') or '').strip()
            if not item_name:
                continue
            quantity = max(1, _coerce_int(entry.get('quantity', 1), 1))
            quantity_text = f' ×{quantity}' if quantity > 1 else ''
            member_name = member_names.get(member_id, f'冒险者 #{member_id}')
            if reason == 'world_unique_conflict':
                notice = f'【结算提示】全服唯一物品「{item_name}」{quantity_text}当前已无法发放给{member_name}，本次未能到账。'
            else:
                notice = f'【结算提示】{member_name}的仓库已满，未能存入「{item_name}」{quantity_text}，该物品已丢弃。'
            if notice not in existing_event_set:
                notices.append(notice)
                existing_event_set.add(notice)
    if notices:
        full_log['events'] = existing_events + notices
    return notices

def _normalize_new_class(value) -> str | None:
    """清洗 AI 返回的 new_class 职业名。

    仅接受非空字符串；压缩空白；超过 20 字符视为 AI 把描述塞进了字段，整体拒绝
    （prompt 约定职业名 2~6 个字）。
    """
    if not isinstance(value, str):
        return None
    name = ' '.join(value.split())
    if not name or len(name) > 20:
        return None
    return name

def _fallback_history_story(adv, update: dict) -> str:
    """从结算数据机械拼一句简短传记，作为主 AI 漏写 history_story_append 时的兜底。

    正常情况下主 AI 会按 prompt 输出 1~2 句剧情浓缩，runner 优先采用；仅当其为空
    （漏写）时才回落到本兜底拼接。audit fix 显式注入 update 的传记（path A）仍优先生效。
    """
    parts: list[str] = []
    name = adv.name or '冒险者'
    hp_change = _coerce_int(update.get('hp_change', 0), 0)
    remaining_hp = _coerce_int(update.get('remaining_hp', -1), -1)
    if remaining_hp <= 0:
        parts.append(f'{name}在本次探险中不幸阵亡')
    elif hp_change < 0:
        parts.append(f'{name}负伤归来')
    elif hp_change > 0:
        parts.append(f'{name}安然无恙')
    gold = round(_coerce_float(update.get('gold_change', 0), 0), 2)
    if gold > 0:
        parts.append(f'获得了{gold}金币')
    elif gold < 0:
        parts.append(f'损失了{abs(gold)}金币')
    new_eq = update.get('new_equipments') or {}
    if isinstance(new_eq, dict) and new_eq:
        names = list(new_eq.keys())
        if len(names) == 1:
            parts.append(f'获得了{names[0]}')
        else:
            parts.append(f'获得了{names[0]}等{len(names)}件物品')
    consumed = update.get('consumed_equipments') or []
    if isinstance(consumed, list) and consumed:
        first = consumed[0]
        first_name = str(first.get('name') or '') if isinstance(first, dict) else str(first)
        total_quantity = sum((max(1, _coerce_int(item.get('quantity', 1), 1)) if isinstance(item, dict) else 1 for item in consumed))
        parts.append(f'消耗了{first_name}' if total_quantity == 1 else f'消耗了{total_quantity}份物品')
    new_traits = update.get('new_traits')
    if isinstance(new_traits, dict) and new_traits:
        parts.append(f'获得了"{list(new_traits.keys())[0]}"特质')
    elif isinstance(new_traits, list) and new_traits:
        t_name = new_traits[0].get('name') if isinstance(new_traits[0], dict) else str(new_traits[0])
        if t_name:
            parts.append(f'获得了"{t_name}"特质')
    if parts:
        return '，'.join(parts) + '。'
    return f'{name}平安完成了本次探险。'

def apply_update_to_s1_adventurer(adv, update, item_dict, dining_context: dict | None=None, *, party_transfers: dict[str, tuple] | None=None, party_holders_map: dict[str, set[int]] | None=None, catalog_by_name: dict | None=None, holder_custom_desc_flags: dict[tuple[int, str], bool] | None=None, holders_map: dict[str, set[int]] | None=None, expedition_log_id: int | None=None, item_use_conditions: dict[str, dict] | None=None, exact_removed_traits: list[object] | None=None, exact_removed_skills: list[object] | None=None, frozen_member_snapshot: dict | None=None, asset_description_update_receipt: dict | None=None, record_history: bool=True) -> float | None:
    """应用单成员 update；有金币变动时返回货币服务确认的最终余额。

    须在事务内调用；死亡资产清理由整队结算在到账回执通过后统一执行。
    """
    from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
    from plugins.adventurer_s1.services.s1_dining_service import S1DiningService
    remaining_hp = update.get('remaining_hp')
    if remaining_hp is not None:
        adv.hp = S1DiningService.resolve_post_expedition_hp(_coerce_int(remaining_hp, adv.hp), dining_context, int(adv.max_hp or 100))
    gold_change = round(_coerce_float(update.get('gold_change', 0), 0.0), 2)
    gold_balance_after: float | None = None
    if gold_change > 0:
        gold_balance_after, gold_err = S1CurrencyService.add(adv.id, 'gold', gold_change, source_type='expedition_reward', description='探险结算收益', auto_commit=False)
        if gold_err:
            raise RuntimeError(f'探险金币入账失败 adv={adv.id}: {gold_err}')
    elif gold_change < 0:
        gold_balance_after, gold_err = S1CurrencyService.deduct(adv.id, 'gold', abs(gold_change), source_type='expedition_loss', description='探险结算损失', auto_commit=False, allow_negative=True)
        if gold_err:
            raise RuntimeError(f'探险金币扣减失败 adv={adv.id}: {gold_err}')
    if update.get('score') is not None:
        adv.latest_score = _coerce_float(update.get('score'), adv.latest_score or 0)
    from plugins.adventurer_s1.services.s1_adventurer_stat_service import add_s1_attr, add_s1_max_hp, clamp_s1_adventurer_stats
    add_s1_attr(adv, 'strength', _coerce_int(update.get('strength_increase', 0), 0))
    add_s1_attr(adv, 'agility', _coerce_int(update.get('agility_increase', 0), 0))
    add_s1_attr(adv, 'intelligence', _coerce_int(update.get('intelligence_increase', 0), 0))
    add_s1_attr(adv, 'luck', _coerce_int(update.get('luck_increase', 0), 0))
    mhp_inc = _coerce_int(update.get('max_hp_increase', 0), 0)
    if mhp_inc:
        add_s1_max_hp(adv, mhp_inc, sync_hp=int(adv.hp or 0) > 0)
    clamp_s1_adventurer_stats(adv)
    if adv.hp <= 0:
        adv.status = 'dead'
        adv.hp = 0
        from plugins.adventurer_s1.services.s1_tombstone_service import S1TombstoneService
        raw_reason = update.get('death_reason')
        S1TombstoneService.record_death(adv, raw_reason if isinstance(raw_reason, str) else None, expedition_log_id=expedition_log_id)
    else:
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        AdventurerS1Service.enter_rest(adv)
    previous_class = str(adv.character_class or '').strip()
    new_class = _normalize_new_class(update.get('new_class'))
    if new_class:
        adv.character_class = new_class
    if record_history:
        history_append = update.get('history_story_append')
        if not history_append:
            history_append = _fallback_history_story(adv, update)
        if history_append:
            adv.history_story = adv.history_story + '\n\n' + history_append if adv.history_story else history_append
    asset_rename_changes = _collect_s1_asset_rename_changes(update, frozen_member_snapshot)
    _apply_skills_to_s1(adv, update, exact_removed_rows=exact_removed_skills, frozen_member_snapshot=frozen_member_snapshot, asset_description_update_receipt=asset_description_update_receipt)
    _apply_equipment_to_s1(adv, update, item_dict, party_transfers=party_transfers, party_holders_map=party_holders_map or holders_map, catalog_by_name=catalog_by_name, holder_custom_desc_flags=holder_custom_desc_flags, expedition_log_id=expedition_log_id, item_use_conditions=item_use_conditions, previous_class=previous_class, frozen_member_snapshot=frozen_member_snapshot, asset_description_update_receipt=asset_description_update_receipt)
    current_traits = _apply_traits_to_s1(adv, update, exact_removed_rows=exact_removed_traits, frozen_member_snapshot=frozen_member_snapshot, asset_description_update_receipt=asset_description_update_receipt)
    sync_trait_asset_renames(int(adv.id), asset_rename_changes, traits=current_traits)
    _apply_asset_recovery_triggers_to_s1(adv, update, frozen_member_snapshot=frozen_member_snapshot)
    return gold_balance_after
_S1_SPECIAL_EVENT_RISK_LABELS = {'local_anomaly': '局部异变', 'regional_incident': '区域事件', 'world_fact_catalyst': '世界事实触媒', 'extreme_crisis': '极端危机', 'stranger': '异界存在', 'abyss': '深渊异变', 'unbeatable_horror': '绝对死境'}

def _roll_special_event(dungeon_min_level: int) -> tuple[bool, bool, str | None, str | None]:
    """出发时掷一次特殊事件，返回 (has_special_event, is_extreme_crisis, special_event_type, risk_tier)。

    使用 S1 专属概率曲线（按当前开放最高常规档 Lv.13-15 平滑抬升）；命中后按
    `ADVENTURER_S1_EXTREME_CRISIS_PROB` 掷极端危机，极端危机内部
    40% abyss / 40% stranger / 20% unbeatable_horror。
    """
    special_prob = compute_s1_special_event_probability(dungeon_min_level)
    if random.random() >= special_prob:
        return (False, False, None, None)
    extreme_prob = float(current_app.config.get('ADVENTURER_S1_EXTREME_CRISIS_PROB', 0.1))
    if random.random() >= extreme_prob:
        return (True, False, None, classify_s1_special_event_risk(dungeon_min_level))
    rand_val = random.random()
    if rand_val < 0.4:
        special_event_type = 'abyss'
    elif rand_val < 0.8:
        special_event_type = 'stranger'
    else:
        special_event_type = 'unbeatable_horror'
    return (True, True, special_event_type, classify_s1_special_event_risk(dungeon_min_level, is_extreme_crisis=True, special_event_type=special_event_type))

def _build_s1_special_event_mode_note(special_event_kind: str, *, target_dungeon: str | None) -> str:
    """Supply current facts; stable mode rules belong to the shared system owners."""
    kind = str(special_event_kind or 'normal').strip() or 'normal'
    if kind == 'normal':
        return '【当前特殊事件类别】：normal'
    return f"【当前特殊事件类别】：{kind}\n【本次特殊事件落点】：{target_dungeon or '当前地牢'}"

def _build_s1_special_event_prompt_context(trigger_special_event: bool, is_extreme_crisis: bool, special_event_type: str | None, *, risk_tier: str | None=None) -> tuple[str, str]:
    """Return the player warning plus one canonical current AI-facing category."""
    kind = resolve_s1_special_event_kind({'has_special_event': trigger_special_event, 'is_extreme_crisis': is_extreme_crisis, 'special_event_type': special_event_type, 'special_event_risk_tier': risk_tier})
    if not trigger_special_event:
        return ('', kind)
    return (get_s1_special_event_warning(kind), kind)

def _apply_exp_and_level_ups(adv, update, dungeon_min_level: int, dungeon_max_level: int, has_special_event: bool=False, is_extreme_crisis: bool=False, special_event_risk_tier: str | None=None, dining_context: dict | None=None) -> None:
    """按地牢档位与本次评分给冒险者累计经验并连续升级。须在事务内、且在
    `apply_update_to_s1_adventurer`（已定生死/状态）之后调用。

    经验计算与升级所需经验复用原版纯函数；上限取 `ADVENTURER_S1_MAX_LEVEL`（暂 15），
    允许一次结算连升多级。每升一级：max_hp +15~25（存活时同步加 hp），随机 1~3 项基础
    属性各 +2~4。S1 特殊事件按风险档重标定经验倍率；
    食堂经验餐在特殊事件倍率之后叠加；狂暴已下线。
    """
    exp_gain = calculate_expedition_exp_gain(adventurer_level=adv.level, dungeon_min_level=dungeon_min_level, dungeon_max_level=dungeon_max_level, score=update.get('score'), has_special_event=has_special_event, is_extreme_crisis=is_extreme_crisis)
    if has_special_event and special_event_risk_tier:
        legacy_multiplier = 3.0 if is_extreme_crisis else 2.0
        s1_multiplier = get_s1_special_event_exp_multiplier(special_event_risk_tier)
        exp_gain = int(exp_gain / legacy_multiplier * s1_multiplier)
    if exp_gain > 0 and isinstance(dining_context, dict):
        from plugins.adventurer_s1.services.s1_dining_service import S1DiningService
        exp_bonus_pct = S1DiningService.get_expedition_exp_bonus_pct(dining_context)
        if exp_bonus_pct > 0:
            exp_gain = int(exp_gain * (1.0 + exp_bonus_pct / 100.0))
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    max_level = _coerce_int(S1SettingsService.get('adventurer_max_level'), 15)
    if exp_gain > 0:
        if adv.level >= max_level:
            adv.exp = 0
        else:
            adv.exp = _coerce_int(getattr(adv, 'exp', 0), 0) + exp_gain
    from plugins.adventurer_s1.services.s1_adventurer_stat_service import add_s1_attr, add_s1_max_hp
    while adv.level < max_level and adv.exp >= get_adventurer_exp_required(adv.level):
        adv.exp -= get_adventurer_exp_required(adv.level)
        adv.level += 1
        max_hp_inc = random.randint(15, 25)
        add_s1_max_hp(adv, max_hp_inc, sync_hp=adv.status != 'dead')
        for attr in random.sample(['strength', 'agility', 'intelligence', 'luck'], random.randint(1, 3)):
            add_s1_attr(adv, attr, random.randint(2, 4))
    if adv.level >= max_level:
        adv.exp = 0

def _apply_expedition_relationships(members, full_log):
    """探险结算时更新好感度。

    基础同队不再自动加好感，所有好感度变化完全由 AI 事件驱动。
    解析 AI 返回的 relationship_changes 并应用；缺少的 pair 记录即时创建。

    所有相关 relationship 一次性 IN 预取到 rel_map，循环内只用 dict 命中；
    缺失的 pair 即时建表并加入 map，避免 O(N²) 单查。
    """
    now = _portable_utc_now()
    member_ids = [m.id for m in members]
    if len(member_ids) < 2:
        return
    member_id_set = set(member_ids)
    existing_rels = AdventurerS1Relationship.query.filter(AdventurerS1Relationship.adventurer_a_id.in_(member_id_set), AdventurerS1Relationship.adventurer_b_id.in_(member_id_set)).all()
    rel_map = {(r.adventurer_a_id, r.adventurer_b_id): r for r in existing_rels}

    def _resolve(a, b):
        a_id, b_id = (min(a, b), max(a, b))
        rel = rel_map.get((a_id, b_id))
        if not rel:
            rel = AdventurerS1Relationship(adventurer_a_id=a_id, adventurer_b_id=b_id, score=0, interaction_count=0)
            db_manager.add(rel)
            rel_map[a_id, b_id] = rel
        return rel
    for i in range(len(members)):
        for j in range(i + 1, len(members)):
            rel = _resolve(members[i].id, members[j].id)
            rel.interaction_count += 1
            rel.last_interaction_at = now
    updates = full_log.get('member_updates', [])
    if not isinstance(updates, list):
        return
    for update in updates:
        if not isinstance(update, dict):
            continue
        changes = update.get('relationship_changes')
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            target_id = _coerce_int(change.get('target_id'), -1)
            delta = _coerce_int(change.get('delta'), 0)
            if target_id not in member_id_set or delta == 0:
                continue
            source_id = _coerce_int(update.get('id'), -1)
            if source_id == target_id:
                continue
            rel = _resolve(source_id, target_id)
            rel.score = max(-100, min(100, rel.score + delta))
            rel.last_interaction_at = now

def _build_s1_member_validation_context(members, target_dungeon: str | None=None, use_condition_by_name: dict[str, dict | None] | None=None, item_dict: dict[str, str] | None=None) -> dict[int, dict]:
    """给 AI 结果校验使用的成员快照，避免校验层反查数据库。"""
    from plugins.adventurer_s1.services.s1_item_use_condition_service import filter_equipment_for_expedition_ai, is_catalog_immutable_item
    from plugins.adventurer_s1.services.s1_ai_asset_reference_service import build_s1_ai_asset_reference_context
    context: dict[int, dict] = {}
    for member in members or []:
        try:
            member_id = int(member.id)
        except (TypeError, ValueError):
            continue
        visible_equipment = filter_equipment_for_expedition_ai(getattr(member, 'equipment_items', []) or [], target_dungeon, use_condition_by_name)
        equipped_skills = [skill for skill in getattr(member, 'skill_items', []) or [] if getattr(skill, 'equipped', True)]
        asset_reference_context = build_s1_ai_asset_reference_context(equipped_skills, visible_equipment)
        catalog_descriptions = item_dict or {}
        equipment_assets_by_id = {int(item.id): {'name': str(getattr(item, 'name', '') or '').strip(), 'type': str(getattr(item, 'type', '') or '').strip(), 'rarity': str(getattr(item, 'rarity', 'common') or 'common').strip(), 'immutable': is_catalog_immutable_item((use_condition_by_name or {}).get(str(getattr(item, 'name', '') or '').strip())), 'desc': str(getattr(item, 'custom_desc', '') or catalog_descriptions.get(str(getattr(item, 'name', '') or ''), '') or '').strip(), 'uses_left': getattr(item, 'uses_left', None), 'evolution_stage': _coerce_int(getattr(item, 'evolution_stage', 0), 0), 'quantity': item_quantity_of(item), 'recovery_minutes': max(0, _coerce_int(getattr(item, 'recovery_minutes', 0), 0)), 'recovering': bool(getattr(item, 'recovering', False))} for item in visible_equipment if getattr(item, 'id', None) is not None and str(getattr(item, 'name', '') or '').strip()}
        skill_assets_by_id = {int(skill.id): {'name': str(getattr(skill, 'name', '') or '').strip(), 'desc': str(getattr(skill, 'description', '') or '').strip(), 'recovery_minutes': max(0, _coerce_int(getattr(skill, 'recovery_minutes', 0), 0)), 'recovering': bool(getattr(skill, 'recovering', False))} for skill in equipped_skills if getattr(skill, 'id', None) is not None and getattr(skill, 'equipped', True) and str(getattr(skill, 'name', '') or '').strip()}
        context[member_id] = {'name': getattr(member, 'name', None), 'hp_before': _coerce_int(getattr(member, 'hp', 0), 0), 'equipment_names': _validation_equipment_names_for_member(visible_equipment), 'equipment_available_quantities': _validation_equipment_quantities_for_member(member), 'skill_names': {s.name for s in getattr(member, 'skill_items', []) or [] if getattr(s, 'name', None) and getattr(s, 'equipped', True)}, 'equipment_assets_by_id': equipment_assets_by_id, 'skill_assets_by_id': skill_assets_by_id, **asset_reference_context, 'trait_assets_by_id': {int(trait.id): {'name': str(getattr(trait, 'name', '') or '').strip()} for trait in getattr(member, 'traits', []) or [] if getattr(trait, 'id', None) is not None and getattr(trait, 'name', None) and (not bool(getattr(trait, 'is_archived', False)))}, 'trait_names': {t.name for t in getattr(member, 'traits', []) or [] if getattr(t, 'name', None) and (not bool(getattr(t, 'is_archived', False)))}}
    return context

def _validation_equipment_quantities_for_member(member) -> dict[str, int]:
    """Return exact consumable units/uses available at departure, grouped by stable name."""
    quantities: dict[str, int] = {}
    for item in getattr(member, 'equipment_items', []) or []:
        name = str(getattr(item, 'name', None) or '').strip()
        if not name:
            continue
        stack_quantity = item_quantity_of(item)
        uses_left = getattr(item, 'uses_left', None)
        available = max(0, _coerce_int(uses_left, 0)) if stack_quantity <= 1 and uses_left is not None else stack_quantity
        quantities[name] = quantities.get(name, 0) + available
    return quantities

def _validation_equipment_names_for_member(visible_equipment) -> set[str]:
    """校验白名单与 AI 可见装备严格同源；后端托管物品在校验前剥离。"""
    return {e.name for e in visible_equipment if getattr(e, 'name', None)}

def _strip_unknown_used_skills(update: dict, known_skill_names: set[str]) -> list[str]:
    """剥离 used_skills 里该成员并未携带（也非本次新学）的技能名，返回被剥离的名字。

    used_skills 不参与结算（`_apply_skills_to_s1` 只认 new_skills/removed_skills），仅是叙事标记；
    且这类「未携带技能」绝大多数是 AI 从装备描述里捞出的能力名（如某杖描述写「可施展X」）。
    校验层已不再为它阻断探险（见 parser `_validate_member_owned_names`），这里在写入日志前
    清掉越界项，保持结构化数据干净；剧情正文不动。
    """
    names = _normalize_member_used_name_list(update.get('used_skills'))
    if not names:
        return []
    kept = [n for n in names if n in known_skill_names]
    removed = [n for n in names if n not in known_skill_names]
    if removed:
        update['used_skills'] = kept
    return removed

def _prepare_s1_member_updates_for_log(result_data: dict, members) -> list[dict]:
    """规范化日志里的成员结算展示字段。

    AI 返回 `remaining_hp` 作为最终剩余 HP；后端基于出发前 HP 自行计算
    `hp_change`，前端只展示变化量。`remaining_hp` 做非负夹取，避免玩家侧看到
    负剩余血。
    """
    from plugins.adventurer_s1.services.s1_item_use_condition_service import strip_backend_managed_equipment_refs
    strip_backend_managed_equipment_refs(result_data)
    context = _build_s1_member_validation_context(members)
    member_by_id: dict[int, object] = {}
    for member in members or []:
        try:
            member_by_id[int(member.id)] = member
        except (TypeError, ValueError):
            continue
    uses_current_gold_contract = S1_GOLD_SETTLEMENT_FIELD in result_data or result_data.get(S1_GOLD_SETTLEMENT_VERSION_FIELD) == S1_GOLD_SETTLEMENT_CONTRACT_VERSION
    prepared: list[dict] = []
    for raw in result_data.get('member_updates', []) or []:
        if not isinstance(raw, dict):
            continue
        raw = project_s1_member_settlement_fields(raw)
        raw_new_equipments = raw.get('new_equipments')
        raw_consumed_equipments = raw.get('consumed_equipments')
        raw_new_skills = raw.get('new_skills')
        raw_recovery_triggers = raw.get('recovery_triggers')
        update = _normalize_member_update_payload(raw, split_semantic_skill_name_desc=False)
        if uses_current_gold_contract:
            update.pop('gold_change', None)
        update['new_equipments'] = normalize_s1_new_equipment_payload(raw_new_equipments) or {}
        update['received_assets'] = deepcopy(raw.get('received_assets') or [])
        update['recovery_triggers'] = list(raw_recovery_triggers) if isinstance(raw_recovery_triggers, list) else []
        if isinstance(raw_new_skills, list) and isinstance(update.get('new_skills'), list):
            recovery_by_skill_name = {str(row.get('name') or '').strip(): max(0, _coerce_int(row.get('recovery_minutes'), 0)) for row in raw_new_skills if isinstance(row, dict) and str(row.get('name') or '').strip()}
            for skill in update['new_skills']:
                if isinstance(skill, dict):
                    skill['recovery_minutes'] = recovery_by_skill_name.get(str(skill.get('name') or '').strip(), 0)
        update.pop('stored_to_warehouse', None)
        member_id = _coerce_int(update.get('id'), -1)
        hp_before = context.get(member_id, {}).get('hp_before', 0)
        remaining_hp = max(0, _coerce_int(update.get('remaining_hp'), hp_before))
        update['remaining_hp'] = remaining_hp
        update['hp_change'] = remaining_hp - hp_before
        update['hp_before'] = hp_before
        if remaining_hp <= 0:
            update['is_dead'] = True
        member = member_by_id.get(member_id)
        if member is not None:
            update['max_hp'] = int(getattr(member, 'max_hp', 100) or 100)
            equip_snapshot = _build_s1_member_equipment_snapshot(member)
            _extend_s1_equipment_snapshot_with_new_items(equip_snapshot, update.get('new_equipments'))
            update['consumed_equipments'] = normalize_s1_consumed_equipment_payload(raw_consumed_equipments, equip_snapshot)
            update.pop('used_equipments', None)
            skill_names = set(context.get(member_id, {}).get('skill_names') or [])
            _filter_misplaced_skill_names_from_new_traits(update, skill_names)
            known_skills = set(skill_names)
            for new_skill in _normalize_new_skills_payload(update.get('new_skills'), split_semantic_name_desc=False) or []:
                if isinstance(new_skill, dict) and new_skill.get('name'):
                    known_skills.add(str(new_skill['name']).strip())
            stripped_skills = _strip_unknown_used_skills(update, known_skills)
            if stripped_skills:
                member_name = context.get(member_id, {}).get('name') or f'#{member_id}'
                current_app.logger.warning('S1 结算剥离未携带 used_skills（多为装备描述里的能力名）: %s 的 %s', member_name, '、'.join(stripped_skills))
        prepared.append(update)
    return prepared

def _build_s1_same_name_gain_review_context(result_data: dict, audit_member_snapshot: list[dict] | None) -> list[dict]:
    """构建同名新增与冻结资产/稳定 ID 更新的冲突，交由全文审核裁决。"""
    held_by_id: dict[int, dict[str, list[dict]]] = {}
    held_asset_by_id: dict[int, dict[int, dict]] = {}
    for member in audit_member_snapshot or []:
        if not isinstance(member, dict):
            continue
        try:
            member_id = int(member.get('adventurer_id'))
        except (TypeError, ValueError):
            continue
        rows: dict[str, list[dict]] = {}
        assets: dict[int, dict] = {}
        for item in member.get('equipment') or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            asset_id = _coerce_int(item.get('asset_id'), 0)
            instance = {'location': 'equipment', 'type': item.get('type'), 'rarity': item.get('rarity'), 'quantity': item.get('quantity'), 'desc': item.get('description') or item.get('desc')}
            if asset_id > 0:
                instance['equipment_id'] = asset_id
                assets[asset_id] = {**instance, 'name': name}
            rows.setdefault(name, []).append(instance)
        held_by_id[member_id] = rows
        held_asset_by_id[member_id] = assets
    collisions: list[dict] = []
    for update in result_data.get('member_updates') or []:
        if not isinstance(update, dict):
            continue
        member_id = _coerce_int(update.get('id'), -1)
        new_items = update.get('new_equipments') or {}
        if not isinstance(new_items, dict):
            continue
        updated_by_final_name: dict[str, list[dict]] = {}
        frozen_assets = held_asset_by_id.get(member_id, {})
        for equipment_update in update.get('equipment_updates') or []:
            if not isinstance(equipment_update, dict):
                continue
            equipment_id = _coerce_int(equipment_update.get('equipment_id'), 0)
            frozen = frozen_assets.get(equipment_id)
            final_name = str(equipment_update.get('name') or '').strip()
            if equipment_id <= 0 or frozen is None or (not final_name):
                continue
            updated_state = {field: equipment_update[field] for field in ('name', 'rarity', 'desc', 'uses_left', 'recovery_minutes', 'evidence_event_indexes') if field in equipment_update}
            updated_by_final_name.setdefault(final_name, []).append({'equipment_id': equipment_id, 'source_name': frozen['name'], 'source_type': frozen.get('type'), 'source_rarity': frozen.get('rarity'), 'source_quantity': frozen.get('quantity'), 'source_desc': frozen.get('desc'), 'updated_state': updated_state})
        for name, proposed in new_items.items():
            clean_name = str(name or '').strip()
            existing = held_by_id.get(member_id, {}).get(clean_name) or []
            updated_existing = updated_by_final_name.get(clean_name) or []
            if existing or updated_existing:
                collisions.append({'adventurer_id': member_id, 'name': clean_name, 'conflict_kind': 'equipment_update_final_name' if updated_existing else 'departure_same_name', 'existing_instances': existing, 'equipment_update_instances': updated_existing, 'proposed_gain': proposed})
    return collisions

def _find_unreviewed_s1_same_name_gain_candidates(reviewed_context: list[dict] | None, final_context: list[dict] | None) -> list[dict]:
    """审核覆盖可消除冲突；新增同名项或稳定实例必须用最终候选续审。"""
    reviewed_ids_by_key = {(row['adventurer_id'], row['name']): {instance['equipment_id'] for instance in row.get('equipment_update_instances') or []} for row in reviewed_context or []}
    unreviewed = []
    for row in final_context or []:
        key = (row['adventurer_id'], row['name'])
        final_ids = {instance['equipment_id'] for instance in row.get('equipment_update_instances') or []}
        if key not in reviewed_ids_by_key or not final_ids.issubset(reviewed_ids_by_key[key]):
            unreviewed.append(row)
    return unreviewed

def _validate_s1_same_name_gain_review_receipt(full_log: dict) -> list[str]:
    """核对同名并存裁决或服务端故障放行凭据；两者均绑定冻结身份。"""
    raw_overlap_keys: set[tuple[int, str, int]] = set()
    for member_update in full_log.get('member_updates') or []:
        if not isinstance(member_update, dict):
            continue
        member_id = _coerce_int(member_update.get('id'), -1)
        new_items = member_update.get('new_equipments') or {}
        if member_id <= 0 or not isinstance(new_items, dict):
            continue
        new_names = {str(name or '').strip() for name in new_items}
        for equipment_update in member_update.get('equipment_updates') or []:
            if not isinstance(equipment_update, dict):
                continue
            equipment_id = _coerce_int(equipment_update.get('equipment_id'), 0)
            final_name = str(equipment_update.get('name') or '').strip()
            if equipment_id > 0 and final_name and (final_name in new_names):
                raw_overlap_keys.add((member_id, final_name, equipment_id))
    if not raw_overlap_keys:
        return []
    conflicts = [row for row in _build_s1_same_name_gain_review_context(full_log, full_log.get('audit_member_snapshot')) if row.get('conflict_kind') == 'equipment_update_final_name']
    frozen_overlap_keys = {(int(conflict['adventurer_id']), str(conflict['name']), _coerce_int(instance.get('equipment_id'), 0)) for conflict in conflicts for instance in conflict.get('equipment_update_instances') or [] if isinstance(instance, dict)}
    if frozen_overlap_keys != raw_overlap_keys:
        return ['稳定 ID 更新后的同名新发放无法由冻结装备身份唯一核对']
    receipt = full_log.get(_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD)
    expected_context_digest = _s1_same_name_gain_review_context_digest(conflicts)
    unavailable_preserved = bool(isinstance(receipt, dict) and receipt.get('status') == 'review_unavailable_original_preserved' and (receipt.get('source') == 'runner_final_mechanical_validation') and (receipt.get('reason_code') == 'observer_error_original_result_released') and (receipt.get('reviews') == []))
    if not (isinstance(receipt, dict) and receipt.get('schema_version') == 2 and (receipt.get('status') == 'passed' or unavailable_preserved)):
        return ['稳定 ID 更新后的同名新发放缺少 Observer 结构化裁决回执']
    if receipt.get('context_digest') != expected_context_digest:
        return ['稳定 ID 更新后的同名新发放裁决回执与最终结算上下文不一致']
    reviews = receipt.get('reviews')
    if not isinstance(reviews, list):
        return ['稳定 ID 更新后的同名新发放缺少 Observer 结构化裁决回执']
    review_by_key: dict[tuple[int, str], dict] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        member_id = _coerce_int(review.get('adventurer_id'), -1)
        name = str(review.get('name') or '').strip()
        key = (member_id, name)
        if member_id <= 0 or not name or key in review_by_key:
            return ['稳定 ID 更新后的同名新发放裁决回执存在重复或非法身份']
        review_by_key[key] = review
    member_updates = {_coerce_int(update.get('id'), -1): update for update in full_log.get('member_updates') or [] if isinstance(update, dict)}
    event_count = len(full_log.get('events') or [])
    errors: list[str] = []
    for conflict in conflicts:
        member_id = int(conflict['adventurer_id'])
        name = str(conflict['name'])
        review = review_by_key.get((member_id, name))
        decision = review.get('decision') if isinstance(review, dict) else None
        if not unavailable_preserved and decision not in {'keep_independent_gain', 'transfer'}:
            errors.append(f'冒险者 #{member_id} 的 {name} 同名并存没有保留双实例的有效裁决')
            continue
        evidence_indexes = review.get('evidence_event_indexes') if isinstance(review, dict) else None
        if not unavailable_preserved and (not isinstance(evidence_indexes, list) or not evidence_indexes or any((type(index) is not int or index < 0 or index >= event_count for index in evidence_indexes)) or (len(set(evidence_indexes)) != len(evidence_indexes))):
            errors.append(f'冒险者 #{member_id} 的 {name} 同名并存裁决缺少有效正文证据')
            continue
        update = member_updates.get(member_id) or {}
        if decision == 'transfer':
            source_id = _coerce_int(review.get('source_adventurer_id'), -1)
            source_update = member_updates.get(source_id) or {}
            source_consumed = {str(row.get('name') or '').strip() for row in source_update.get('consumed_equipments') or [] if isinstance(row, dict)}
            if source_id <= 0 or source_id == member_id or name not in source_consumed:
                errors.append(f'冒险者 #{member_id} 的 {name} 转移裁决没有来源成员消耗闭环')
                continue
        raw_traits = update.get('new_traits') or []
        trait_rows = list(raw_traits.values()) if isinstance(raw_traits, dict) else raw_traits if isinstance(raw_traits, list) else []
        ambiguous_dependency = any((isinstance(dependency, dict) and str(dependency.get('asset_kind') or '').strip() == 'equipment' and (str(dependency.get('new_asset_name') or '').strip() == name) for trait in trait_rows if isinstance(trait, dict) for dependency in trait.get('asset_dependencies') or []))
        if ambiguous_dependency:
            errors.append(f'冒险者 #{member_id} 的 {name} 同名并存仍有无法唯一绑定的特质依赖')
    return errors

def _s1_same_name_gain_review_context_digest(context: list[dict]) -> str:
    """绑定 Observer 已审核的精确稳定 ID、候选更新与同名新增状态。"""
    payload = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return sha256(payload.encode('utf-8')).hexdigest()

def _rebind_s1_same_name_gain_receipt_after_description_finalization(reviewed_result: dict, finalized_result: dict, audit_member_snapshot: list[dict] | None) -> None:
    """仅承接描述 owner 已核验的原实例全文变化，不扩张同名所得裁决。"""
    receipt = reviewed_result.get(_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD)
    if not isinstance(receipt, dict) or _validate_s1_same_name_gain_review_receipt({**reviewed_result, 'audit_member_snapshot': audit_member_snapshot}):
        return
    if reviewed_result.get('events') != finalized_result.get('events'):
        return
    contexts = [[row for row in _build_s1_same_name_gain_review_context(value, audit_member_snapshot) if row.get('conflict_kind') == 'equipment_update_final_name'] for value in (reviewed_result, finalized_result)]
    reviewed_context, final_context = contexts
    if not reviewed_context or len(reviewed_context) != len(final_context):
        return
    expected_context = deepcopy(reviewed_context)
    for expected, final in zip(expected_context, final_context):
        old_instances = expected['equipment_update_instances']
        final_instances = final['equipment_update_instances']
        if len(old_instances) != len(final_instances):
            return
        for old_instance, final_instance in zip(old_instances, final_instances):
            old_state = old_instance['updated_state']
            final_state = final_instance['updated_state']
            if old_state.get('desc') == final_state.get('desc'):
                continue
            _, verified_summary = resolve_s1_finalized_asset_description(member_id=expected['adventurer_id'], asset_kind='equipment', asset_id=old_instance['equipment_id'], frozen_full_description=old_instance.get('source_desc'), proposed_description=final_state.get('desc'), receipt=finalized_result.get(S1_ASSET_DESCRIPTION_UPDATE_RECEIPT_FIELD))
            if verified_summary is None:
                return
            old_state['desc'] = final_state['desc']
    if expected_context != final_context:
        return
    rebound_receipt = {**deepcopy(receipt), 'context_digest': _s1_same_name_gain_review_context_digest(final_context)}
    if _validate_s1_same_name_gain_review_receipt({**finalized_result, 'audit_member_snapshot': audit_member_snapshot, _S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD: rebound_receipt}):
        return
    finalized_result[_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD] = rebound_receipt

def _copy_s1_same_name_gain_review_receipt_to_log(full_log: dict, result_data: dict, *, event_index_offset: int=0) -> None:
    """把内部语义裁决绑定到规范化后的日志成品，供最终结算复核。"""
    full_log.pop(_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD, None)
    receipt = result_data.get(_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD)
    if not isinstance(receipt, dict):
        return
    frozen_snapshot = full_log.get('audit_member_snapshot')
    reviewed_conflicts = [row for row in _build_s1_same_name_gain_review_context(result_data, frozen_snapshot) if row.get('conflict_kind') == 'equipment_update_final_name']
    persisted_conflicts = [row for row in _build_s1_same_name_gain_review_context(full_log, frozen_snapshot) if row.get('conflict_kind') == 'equipment_update_final_name']
    receipt_digest = str(receipt.get('context_digest') or '').strip()
    if not reviewed_conflicts or not persisted_conflicts or receipt_digest != _s1_same_name_gain_review_context_digest(reviewed_conflicts) or (receipt_digest != _s1_same_name_gain_review_context_digest(persisted_conflicts)):
        return
    copied_receipt = deepcopy(receipt)
    offset = max(0, _coerce_int(event_index_offset, 0))
    for review in copied_receipt.get('reviews') or []:
        if not isinstance(review, dict):
            continue
        indexes = review.get('evidence_event_indexes')
        if isinstance(indexes, list):
            review['evidence_event_indexes'] = [index + offset for index in indexes if type(index) is int and index >= 0]
    full_log[_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD] = copied_receipt

def _build_s1_consumed_asset_continuity_review_context(result_data: dict, audit_member_snapshot: list[dict] | None) -> list[dict]:
    """兼容旧调用名；候选构建由共享连续性合同单点持有。"""
    return s1_consumed_asset_continuity_contract.build_consumed_asset_continuity_review_context(result_data, audit_member_snapshot)

def _find_unreviewed_s1_consumed_asset_continuity_candidates(reviewed_context: list[dict] | None, final_context: list[dict] | None) -> list[dict]:
    """Return final destructive candidates not covered by the prior review input.

    A valid ``retained_unchanged`` decision removes a candidate, so contraction is
    allowed. New candidates, changed full-consumption quantities, or extra
    possible survivor names must be reviewed against the final candidate before
    settlement can continue.
    """
    reviewed_by_key: dict[tuple[int, str], dict] = {}
    for row in reviewed_context or []:
        if not isinstance(row, dict):
            continue
        key = (_coerce_int(row.get('adventurer_id'), -1), str(row.get('consumed_name') or '').strip())
        if key[0] <= 0 or not key[1] or key in reviewed_by_key:
            continue
        reviewed_by_key[key] = row
    unreviewed: list[dict] = []
    for row in final_context or []:
        if not isinstance(row, dict):
            continue
        key = (_coerce_int(row.get('adventurer_id'), -1), str(row.get('consumed_name') or '').strip())
        reviewed = reviewed_by_key.get(key)
        if reviewed is None:
            unreviewed.append(row)
            continue
        if _coerce_int(row.get('consumed_quantity'), -1) != _coerce_int(reviewed.get('consumed_quantity'), -1):
            unreviewed.append(row)
            continue
        reviewed_new_names = {str(name or '').strip() for name in reviewed.get('proposed_new_names') or [] if str(name or '').strip()}
        final_new_names = {str(name or '').strip() for name in row.get('proposed_new_names') or [] if str(name or '').strip()}
        if not final_new_names.issubset(reviewed_new_names):
            unreviewed.append(row)
    return unreviewed
_REVIEWED_COMPANION_STAGE_FLOORS_KEY = '_reviewed_companion_stage_floors'

def _reviewed_companion_survivor_snapshot(value: dict, *, stage_floor: int) -> dict:
    snapshot = deepcopy(value)
    snapshot['evolution_stage'] = stage_floor
    snapshot.setdefault('uses_left', None)
    return snapshot

def _apply_reviewed_companion_stage_floors(result_data: dict, continuity_context: list[dict] | None, continuity_reviews: list[dict] | None) -> list[str]:
    """把 Observer 已确认的实体延续转换为后端托管的伙伴阶级下限。"""
    from plugins.adventurer_s1.s1_item_types import coerce_evolution_stage, companion_allows_duplicate_name_instances
    context_by_key: dict[tuple[int, str], dict] = {}
    for row in continuity_context or []:
        if not isinstance(row, dict):
            continue
        key = (_coerce_int(row.get('adventurer_id'), -1), str(row.get('consumed_name') or '').strip())
        if key[0] <= 0 or not key[1] or key in context_by_key:
            continue
        context_by_key[key] = row
    updates_by_id = {_coerce_int(update.get('id'), -1): update for update in result_data.get('member_updates') or [] if isinstance(update, dict)}
    errors: list[str] = []
    bindings: list[dict] = []
    for review in continuity_reviews or []:
        if not isinstance(review, dict) or review.get('decision') != 'whole_replacement':
            continue
        adventurer_id = _coerce_int(review.get('adventurer_id'), -1)
        consumed_name = str(review.get('consumed_name') or '').strip()
        context = context_by_key.get((adventurer_id, consumed_name))
        if context is None:
            errors.append(f'companion_stage_lineage.context_missing:{adventurer_id}:{consumed_name}')
            continue
        source_stages = [coerce_evolution_stage(instance.get('evolution_stage')) for instance in context.get('departure_instances') or [] if isinstance(instance, dict) and companion_allows_duplicate_name_instances(str(instance.get('type') or ''))]
        if not source_stages:
            continue
        stage_floor = max(source_stages)
        update = updates_by_id.get(adventurer_id)
        new_equipments = update.get('new_equipments') if isinstance(update, dict) else None
        if not isinstance(new_equipments, dict):
            errors.append(f'companion_stage_lineage.new_equipments_missing:{adventurer_id}')
            continue
        for surviving_name in review.get('surviving_new_names') or []:
            clean_name = str(surviving_name or '').strip()
            data = new_equipments.get(clean_name)
            if not isinstance(data, dict):
                errors.append(f'companion_stage_lineage.survivor_missing:{adventurer_id}:{clean_name}')
                continue
            if not companion_allows_duplicate_name_instances(str(data.get('type') or '')):
                errors.append(f'companion_stage_lineage.survivor_type_mismatch:{adventurer_id}:{clean_name}')
                continue
            normalized_data = _reviewed_companion_survivor_snapshot(data, stage_floor=stage_floor)
            new_equipments[clean_name] = normalized_data
            bindings.append({'adventurer_id': adventurer_id, 'consumed_name': consumed_name, 'surviving_name': clean_name, 'stage_floor': stage_floor, 'surviving_snapshot': deepcopy(normalized_data)})
    if not errors:
        result_data[_REVIEWED_COMPANION_STAGE_FLOORS_KEY] = bindings
    return errors

def _enforce_reviewed_companion_stage_floors(result_data: dict, continuity_context: list[dict] | None) -> list[str]:
    """Guard/恢复阶段后再次机械执行已冻结的阶级下限。"""
    from plugins.adventurer_s1.s1_item_types import companion_allows_duplicate_name_instances
    continuity_keys = {(_coerce_int(row.get('adventurer_id'), -1), str(row.get('consumed_name') or '').strip()) for row in continuity_context or [] if isinstance(row, dict)}
    updates_by_id = {_coerce_int(update.get('id'), -1): update for update in result_data.get('member_updates') or [] if isinstance(update, dict)}
    errors: list[str] = []
    for binding in result_data.get(_REVIEWED_COMPANION_STAGE_FLOORS_KEY) or []:
        if not isinstance(binding, dict):
            errors.append('companion_stage_lineage.binding_invalid')
            continue
        adventurer_id = _coerce_int(binding.get('adventurer_id'), -1)
        consumed_name = str(binding.get('consumed_name') or '').strip()
        surviving_name = str(binding.get('surviving_name') or '').strip()
        stage_floor = max(0, _coerce_int(binding.get('stage_floor'), 0))
        if (adventurer_id, consumed_name) not in continuity_keys:
            errors.append(f'companion_stage_lineage.consumption_stale:{adventurer_id}:{consumed_name}')
            continue
        update = updates_by_id.get(adventurer_id)
        new_equipments = update.get('new_equipments') if isinstance(update, dict) else None
        data = new_equipments.get(surviving_name) if isinstance(new_equipments, dict) else None
        if not isinstance(data, dict) or not companion_allows_duplicate_name_instances(str(data.get('type') or '')):
            errors.append(f'companion_stage_lineage.binding_stale:{adventurer_id}:{surviving_name}')
            continue
        reviewed_snapshot = binding.get('surviving_snapshot')
        if not isinstance(reviewed_snapshot, dict):
            errors.append(f'companion_stage_lineage.snapshot_invalid:{adventurer_id}:{surviving_name}')
            continue
        normalized_data = _reviewed_companion_survivor_snapshot(data, stage_floor=stage_floor)
        normalized_reviewed_snapshot = _reviewed_companion_survivor_snapshot(reviewed_snapshot, stage_floor=stage_floor)
        if normalized_data != normalized_reviewed_snapshot:
            errors.append(f'companion_stage_lineage.survivor_metadata_changed:{adventurer_id}:{surviving_name}')
            continue
        new_equipments[surviving_name] = normalized_data
    return errors

class _S1ExpeditionRetryable(Exception):
    """触发 tenacity 重试的内部信号：HTTP 异常 / AI 响应结构异常 / 业务校验失败。

    用单一异常类型把"业务上希望重试的失败"和"代码 bug 不该重试"明确分开 —— 后者
    会自然冒泡，不会被默默重试浪费 AI 调用。
    """

def _build_s1_outline_party_profile(members, *, target_dungeon: str, special_event_kind: str='normal', has_hidden_boss: bool=False) -> dict[str, object]:
    """新核心的等级参照；真实能力由同次冻结的成员机制投影另行提供。"""
    levels = [_coerce_int(getattr(m, 'level', 1), 1) for m in members or []]
    avg_level = round(sum(levels) / len(levels), 1) if levels else 1
    highest_level = max(levels) if levels else 1
    dungeon_min_level, _ = get_s1_dungeon_level_range(target_dungeon)
    core_reference = get_s1_special_core_level_reference(dungeon_min_level, special_event_kind=special_event_kind, has_hidden_boss=has_hidden_boss, party_highest_level=highest_level)
    return {'member_count': len(levels), 'avg_level': avg_level, 'highest_level': highest_level, 'special_core_basis_level': core_reference['basis_level'], 'core_level_reference': core_reference}

def _build_s1_outline_member_capabilities(members, *, item_dict: dict, target_dungeon: str, use_condition_by_name: dict | None) -> list[dict]:
    """Reuse the generation asset filters and verified compressed mechanisms.

    Keep each member's capacity separate without player names or asset IDs.
    Creation receives skills and carried equipment, not the trait catalogue.
    The shared selector keeps traits for knowledge routing and other consumers.
    Knowledge embedded in a supplied skill/item remains part of that description;
    this projection does not attempt semantic rewriting of individual assets.
    """
    from plugins.adventurer_s1.services.s1_countermeasure_context_selection_ai_service import build_party_capability_mechanism_snapshot
    return [{'member_index': index, 'level': _coerce_int(getattr(member, 'level', 1), 1), 'current_hp': _coerce_int(getattr(member, 'hp', 0)), 'max_hp': _coerce_int(getattr(member, 'max_hp', 0)), 'attributes': {key: _coerce_int(getattr(member, key, 0)) for key in ('strength', 'agility', 'intelligence', 'luck')}, 'capabilities': build_party_capability_mechanism_snapshot([member], item_dict, target_dungeon=target_dungeon, use_condition_by_name=use_condition_by_name, deduplicate=False, include_traits=False)} for index, member in enumerate(members or [], start=1)]

def _should_build_s1_objective_outline() -> bool:
    if not _coerce_bool(current_app.config.get('ADVENTURER_S1_EXPEDITION_OBJECTIVE_OUTLINE_AI_ENABLED', True), True):
        return False
    return True
_S1_SPECIAL_EVENT_DELTA_FIELD = 'special_event_delta'
_S1_SPECIAL_EVENT_DELTA_KEYS = frozenset({'present', 'ordinary_baseline', 'material_delta', 'causal_entry', 'player_visible_signature'})
_S1_OBJECTIVE_OUTLINE_LIST_FIELDS = frozenset({'commission_objectives', 'lethal_pressure'})
_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD = 'encounter_capability_profiles'
_S1_ENCOUNTER_CAPABILITY_PROFILE_KEYS = frozenset({'subject', 'scope', 'basis_level', 'typical_profile', 'current_profile', 'condition', 'number_pressure', 'capabilities', 'limitations', 'world_actor_id', 'capability_revision', 'identity_summary', 'current_agenda'})
_S1_ENCOUNTER_CAPABILITY_PROFILE_LEGACY_KEYS = _S1_ENCOUNTER_CAPABILITY_PROFILE_KEYS - {'world_actor_id', 'capability_revision', 'identity_summary', 'current_agenda'}
_S1_ENCOUNTER_CAPABILITY_AXIS_KEYS = CAPABILITY_AXIS_KEYS
_S1_ENCOUNTER_CAPABILITY_RANKS = CAPABILITY_RANKS
_S1_ENCOUNTER_CAPABILITY_SCOPES = frozenset({'group', 'individual'})
_S1_ENCOUNTER_CAPABILITY_CONDITIONS = CAPABILITY_CONDITIONS
_S1_ENCOUNTER_NUMBER_PRESSURES = CAPABILITY_NUMBER_PRESSURES
_S1_LETHAL_PRESSURE_STRUCTURE_FIELD = 'lethal_pressure_structures'
_S1_LETHAL_PRESSURE_STRUCTURE_KEYS = frozenset({'pressure_index', 'coverage_mode', 'contact_rule', 'local_change_limit', 'residual_rule', 'resolution_paths', 'capability_refs', 'resolution_modes'})
_S1_LETHAL_PRESSURE_RESOLUTION_PATH_KEYS = frozenset({'mode', 'trigger', 'effect_boundary', 'prevention_boundary'})
_S1_LETHAL_PRESSURE_RESOLUTION_PATH_MODES = frozenset({'hp_damage', 'durability_bypass'})
_S1_LETHAL_PRESSURE_COVERAGE_MODES = frozenset({'shared', 'individual', 'linked', 'propagating'})
_S1_WORLD_INTEL_USAGE_FIELD = 'world_intel_usages'
_S1_WORLD_INTEL_USAGE_KEYS = frozenset({'world_story_thread_id', 'source_revision', 'usage_mode'})
_S1_WORLD_INTEL_USAGE_MODES = frozenset({'ignored', 'background_reference', 'exclusive_direct_use'})
_S1_WORLD_ACTOR_USAGE_FIELD = 'world_actor_usages'
_S1_WORLD_ACTOR_USAGE_KEYS = frozenset({'world_actor_id', 'capability_revision', 'usage_mode'})
_S1_WORLD_ACTOR_USAGE_MODES = _S1_WORLD_INTEL_USAGE_MODES
_S1_ACTIVE_FACTION_KEYS_FIELD = 'active_faction_keys'
_S1_TRAIT_CONTEXT_TAGS_FIELD = 'trait_context_tags'

class _S1WorldIntelUsageModel(BaseModel):
    """新增情报采纳字段的纯结构契约；运行时授权仍由冻结快照校验。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    world_story_thread_id: int = Field(gt=0)
    source_revision: int = Field(gt=0)
    usage_mode: Literal['ignored', 'background_reference', 'exclusive_direct_use']

class _S1WorldActorUsageModel(BaseModel):
    """长期角色的精确身份与能力版本采纳契约。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    world_actor_id: int = Field(gt=0)
    capability_revision: int = Field(ge=0)
    usage_mode: Literal['ignored', 'background_reference', 'exclusive_direct_use']

class _S1ExpeditionMitigationSourceModel(BaseModel):
    """单步减免对应的稳定能力来源与成立边界。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    ledger_id: str | None = Field(default=None, min_length=1, max_length=40)
    source_ref_id: str | None = Field(default=None, min_length=1, max_length=80)
    source_owner_id: int | None = Field(default=None, gt=0)
    source_kind: Literal['equipment', 'skill', 'trait', 'state'] | None = None
    source_name: str | None = Field(default=None, min_length=1)
    source_origin: Literal['member_snapshot', 'event_fact']
    established_event_fact_index: int | None = Field(default=None, ge=0)
    mitigation: int = Field(gt=0)
    effect_boundary: str = Field(min_length=1)

class _S1ExpeditionMitigationLedgerEntryModel(BaseModel):
    """一次防护建立后可被多个对象或接触共同占用的全局容量。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    ledger_id: str = Field(min_length=1, max_length=40)
    source_ref_id: str | None = Field(default=None, min_length=1, max_length=80)
    source_owner_id: int | None = Field(default=None, gt=0)
    source_kind: Literal['equipment', 'skill', 'trait', 'state'] | None = None
    source_name: str | None = Field(default=None, min_length=1)
    source_origin: Literal['member_snapshot', 'event_fact']
    established_event_fact_index: int | None = Field(default=None, ge=0)
    capacity: int = Field(gt=0)
    effect_boundary: str = Field(min_length=1)

class _S1ExpeditionMitigationAllocationModel(BaseModel):
    """单次作用对一个已声明防护账本项的实际占用。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    ledger_id: str = Field(min_length=1, max_length=40)
    mitigation: int = Field(gt=0)

class _S1ExpeditionHpChangeModel(BaseModel):
    """与事件事实绑定的单步普通 HP 变化。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    event_fact_index: int = Field(ge=0)
    pressure_index: int | None = Field(default=None, ge=0)
    raw_damage: int = Field(ge=0)
    mitigation: int = Field(ge=0)
    mitigation_sources: list[_S1ExpeditionMitigationSourceModel] = Field(default_factory=list, max_length=S1_EXPEDITION_RESOLUTION_MAX_ALLOCATIONS_PER_CHANGE)
    damage_taken: int = Field(ge=0)
    healing_received: int = Field(ge=0)

class _S1ExpeditionImpactChangeModel(BaseModel):
    """主剧情因果底稿只提交作用事实；扣血与终态由后端派生。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    event_fact_index: int = Field(ge=0)
    pressure_index: int | None = Field(default=None, ge=0)
    raw_damage: int = Field(ge=0)
    mitigation: int = Field(ge=0)
    mitigation_sources: list[_S1ExpeditionMitigationAllocationModel] = Field(max_length=S1_EXPEDITION_RESOLUTION_MAX_ALLOCATIONS_PER_CHANGE)
    healing_received: int = Field(ge=0)

class _S1ExpeditionMemberImpactModel(BaseModel):
    """不读取当前 HP 的逐成员作用候选。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    id: int = Field(gt=0)
    changes: list[_S1ExpeditionImpactChangeModel] = Field(max_length=S1_EXPEDITION_RESOLUTION_MAX_CHANGES_PER_MEMBER)
    direct_terminal_state: Literal['none', 'durability_bypass', 'unable_to_return']
    direct_terminal_event_fact_index: int | None = Field(ge=0)
    direct_terminal_pressure_index: int | None = Field(default=None, ge=0)

class _S1ExpeditionHpTraceModel(BaseModel):
    """主剧情因果底稿内部可机械复算的 HP 继承链。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    starting_hp: int = Field(gt=0)
    changes: list[_S1ExpeditionHpChangeModel] = Field(max_length=S1_EXPEDITION_RESOLUTION_MAX_CHANGES_PER_MEMBER)
    durability_bypass_triggered: bool
    life_state_basis: Literal['survived', 'ordinary_hp_exhausted', 'durability_bypass', 'unable_to_return']

class _S1ExpeditionEstablishedSourceModel(BaseModel):
    """事件事实中新成立、可被后续生命裁决引用的来源。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    event_fact_index: int = Field(ge=0)
    source_owner_id: int = Field(gt=0)
    source_kind: Literal['equipment', 'skill', 'trait', 'state']
    source_name: str = Field(min_length=1)

class _S1ExpeditionLifeEffectSourceModel(BaseModel):
    """主剧情追加恢复或复活所依赖的稳定来源。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    source_owner_id: int = Field(gt=0)
    source_kind: Literal['equipment', 'skill', 'trait', 'state']
    source_name: str = Field(min_length=1)
    source_origin: Literal['member_snapshot', 'event']
    established_event_index: int | None = Field(default=None, ge=0)
    effect_boundary: str = Field(min_length=1)

class _S1ExpeditionLifeAdjustmentChangeModel(BaseModel):
    """主剧情相对前置生命底稿新增的单步 HP 变化。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    event_index: int = Field(ge=0)
    damage_taken: int = Field(ge=0)
    healing_received: int = Field(ge=0)
    revival_triggered: bool
    effect_sources: list[_S1ExpeditionLifeEffectSourceModel] = Field(default_factory=list, max_length=16)

class _S1ExpeditionLifeAdjustmentModel(BaseModel):
    """主剧情改变生命结果时必须提交的可复算追加链。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    id: int = Field(gt=0)
    starting_hp: int = Field(ge=0)
    changes: list[_S1ExpeditionLifeAdjustmentChangeModel] = Field(max_length=32)
    terminal_event_index: int = Field(ge=0)
    durability_bypass_triggered: bool
    final_state_basis: Literal['survived', 'ordinary_hp_exhausted', 'durability_bypass', 'unable_to_return']

class _S1ExpeditionResolvedMemberOutcomeModel(BaseModel):
    """主剧情因果底稿经后端派生的逐成员生命结果与 HP 继承链。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    id: int = Field(gt=0)
    remaining_hp: int = Field(ge=0)
    is_dead: bool
    death_reason: str
    direct_terminal_pressure_index: int | None = Field(default=None, ge=0)
    hp_trace: _S1ExpeditionHpTraceModel

class _S1ExpeditionResolutionCompletionModel(BaseModel):
    """主剧情因果底稿的核心局势与离场结果。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    core_resolved: bool
    exit_resolved: bool
    ending_type: str = Field(min_length=1)

class _S1ExpeditionEventResolutionProposalModel(BaseModel):
    """主剧情因果底稿的当前正向输出契约，不包含 HP 派生终态。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    resolution_contract_version: Literal[2] = 2
    event_facts: list[str] = Field(min_length=1, max_length=S1_EXPEDITION_RESOLUTION_MAX_EVENT_FACTS)
    established_sources: list[_S1ExpeditionEstablishedSourceModel] = Field(max_length=S1_EXPEDITION_RESOLUTION_MAX_ESTABLISHED_SOURCES)
    mitigation_ledger: list[_S1ExpeditionMitigationLedgerEntryModel] = Field(max_length=S1_EXPEDITION_RESOLUTION_LEDGER_HARD_LIMIT)
    member_impacts: list[_S1ExpeditionMemberImpactModel] = Field(min_length=1, max_length=S1_EXPEDITION_RESOLUTION_MAX_MEMBERS)
    completion_check: _S1ExpeditionResolutionCompletionModel

class _S1ExpeditionEventResolutionModel(BaseModel):
    """主剧情写作前的内部事件事实契约。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    resolution_contract_version: int = Field(default=1, ge=1)
    event_facts: list[str] = Field(min_length=1)
    established_sources: list[_S1ExpeditionEstablishedSourceModel] = Field(default_factory=list, max_length=S1_EXPEDITION_RESOLUTION_MAX_ESTABLISHED_SOURCES)
    mitigation_ledger: list[_S1ExpeditionMitigationLedgerEntryModel] = Field(default_factory=list, max_length=S1_EXPEDITION_RESOLUTION_LEDGER_HARD_LIMIT)
    member_outcomes: list[_S1ExpeditionResolvedMemberOutcomeModel] = Field(min_length=1)
    completion_check: _S1ExpeditionResolutionCompletionModel

class _S1ExpeditionDeathResourceReviewModel(BaseModel):
    """不可逆终态与共享防护的一致性复核；无直接改写权限。"""
    model_config = ConfigDict(extra='ignore', strict=True)
    verdict: Literal['confirmed', 'retry_causal_draft']
    reviewed_member_ids: list[int] = Field(min_length=1, max_length=S1_EXPEDITION_RESOLUTION_MAX_MEMBERS)
    reviewed_ledger_ids: list[str] = Field(max_length=S1_EXPEDITION_RESOLUTION_LEDGER_HARD_LIMIT)
    issue_event_fact_indexes: list[int] = Field(max_length=S1_EXPEDITION_RESOLUTION_MAX_EVENT_FACTS)
    reason: str = ''
_S1_OBJECTIVE_OUTLINE_REQUIRED_FIELDS = frozenset({'target_dungeon', 'objective_situation', 'world_state_change_allowed', _S1_SPECIAL_EVENT_DELTA_FIELD}) | _S1_OBJECTIVE_OUTLINE_LIST_FIELDS | {_S1_LETHAL_PRESSURE_STRUCTURE_FIELD}
_S1_OBJECTIVE_OUTLINE_ALLOWED_FIELDS = _S1_OBJECTIVE_OUTLINE_REQUIRED_FIELDS | {'temporary_location', _S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD, _S1_LETHAL_PRESSURE_STRUCTURE_FIELD, _S1_WORLD_INTEL_USAGE_FIELD, _S1_WORLD_ACTOR_USAGE_FIELD, _S1_ACTIVE_FACTION_KEYS_FIELD, _S1_TRAIT_CONTEXT_TAGS_FIELD}
_S1_OBJECTIVE_OUTLINE_FIELD_SCHEMA = {'type': 'object', 'properties': {**{key: {'type': 'string'} for key in _S1_OBJECTIVE_OUTLINE_ALLOWED_FIELDS}, **{key: {'type': ['array', 'string'], 'items': {'type': 'string'}} for key in _S1_OBJECTIVE_OUTLINE_LIST_FIELDS}, 'world_state_change_allowed': {'type': ['boolean', 'string']}, 'temporary_location': {'type': ['object', 'array', 'string', 'null'], 'properties': {'location_name': {'type': 'string'}, 'entry_basis': {'type': 'string'}}, 'items': {'type': 'object', 'properties': {'location_name': {'type': 'string'}, 'entry_basis': {'type': 'string'}}}}, _S1_SPECIAL_EVENT_DELTA_FIELD: {'type': 'object', 'properties': {**{key: {'type': 'string'} for key in _S1_SPECIAL_EVENT_DELTA_KEYS}, 'present': {'type': ['boolean', 'string']}}}, _S1_LETHAL_PRESSURE_STRUCTURE_FIELD: {'type': 'array', 'items': {'type': 'object', 'properties': {**{key: {'type': 'string'} for key in _S1_LETHAL_PRESSURE_STRUCTURE_KEYS}, 'pressure_index': {'type': ['integer', 'string']}, 'capability_refs': {'type': 'array', 'items': {'type': 'object', 'properties': {'profile_index': {'type': ['integer', 'string']}, 'world_actor_id': {'type': ['integer', 'string']}, 'capability_indexes': {'type': 'array', 'items': {'type': ['integer', 'string']}}}}}, 'resolution_modes': {'type': 'array', 'items': {'type': 'string'}}, 'resolution_paths': {'type': 'array', 'items': {'type': 'object', 'properties': {key: {'type': 'string'} for key in _S1_LETHAL_PRESSURE_RESOLUTION_PATH_KEYS}}}}}}, _S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD: {'type': 'array', 'items': {'type': 'object', 'properties': {**{key: {'type': 'string'} for key in _S1_ENCOUNTER_CAPABILITY_PROFILE_KEYS}, **ACTOR_CAPABILITY_FIELD_SCHEMA['properties'], **{key: {'type': ['integer', 'string', 'null']} for key in ('world_actor_id', 'capability_revision')}}}}, _S1_WORLD_INTEL_USAGE_FIELD: {'type': 'array', 'items': {'type': 'object', 'properties': {key: {'type': 'string' if key == 'usage_mode' else ['integer', 'string']} for key in _S1_WORLD_INTEL_USAGE_KEYS}}}, _S1_WORLD_ACTOR_USAGE_FIELD: {'type': 'array', 'items': {'type': 'object', 'properties': {key: {'type': 'string' if key == 'usage_mode' else ['integer', 'string']} for key in _S1_WORLD_ACTOR_USAGE_KEYS}}}, _S1_ACTIVE_FACTION_KEYS_FIELD: {'type': 'array', 'items': {'type': 'string'}}, _S1_TRAIT_CONTEXT_TAGS_FIELD: {'type': ['array', 'null'], 'items': {'type': 'string'}}}}

def _build_s1_objective_outline_raw_excerpt(text: str, limit: int=2000) -> str:
    """保留响应首尾，避免长 JSON 的管理端摘录看起来像在中途被截断。"""
    return build_s1_ai_raw_excerpt(text, limit=limit)

def _normalize_s1_outline_ascii_integer(value: object, *, minimum: int) -> tuple[object, bool]:
    """Normalize only a complete ASCII decimal string; never default or truncate."""
    return normalize_ascii_integer(value, minimum=minimum)

def _normalize_s1_outline_exact_boolean(value: object) -> tuple[object, bool]:
    """Normalize only the explicit JSON boolean spellings encoded as strings."""
    return normalize_exact_boolean(value)

def _is_s1_encounter_capability_axes(value: object) -> bool:
    return isinstance(value, dict) and _S1_ENCOUNTER_CAPABILITY_AXIS_KEYS.issubset(value) and all((value[key] in _S1_ENCOUNTER_CAPABILITY_RANKS for key in _S1_ENCOUNTER_CAPABILITY_AXIS_KEYS))

def _normalize_s1_encounter_capability_profile(value: object) -> object:
    """只规范化无歧义的 JSON 表达差异，不替 AI 选择能力等级或改写事实。"""
    if not isinstance(value, dict):
        return value
    normalized = {key: item for key, item in value.items() if key in _S1_ENCOUNTER_CAPABILITY_PROFILE_KEYS}
    for field in ('typical_profile', 'current_profile'):
        axes = normalized.get(field)
        if isinstance(axes, dict):
            normalized[field] = {key: item for key, item in axes.items() if key in _S1_ENCOUNTER_CAPABILITY_AXIS_KEYS}
    basis_level = normalized.get('basis_level')
    if isinstance(basis_level, str):
        match = re.fullmatch('\\s*(?:Lv\\.?\\s*)?(\\d+)\\s*', basis_level, flags=re.IGNORECASE)
        if match:
            try:
                normalized['basis_level'] = int(match.group(1))
            except ValueError:
                pass
    for field in ('world_actor_id', 'capability_revision'):
        normalized_value, changed = _normalize_s1_outline_ascii_integer(normalized.get(field), minimum=1)
        if changed:
            normalized[field] = normalized_value
    for key in ('capabilities', 'limitations'):
        item = normalized.get(key)
        if isinstance(item, str):
            clean_item = item.strip()
            normalized[key] = [clean_item] if clean_item else []
    return normalized

def _get_s1_encounter_capability_profile_error(value: object) -> str | None:
    if not isinstance(value, dict) or not _S1_ENCOUNTER_CAPABILITY_PROFILE_LEGACY_KEYS.issubset(value):
        return 'fields'
    if not isinstance(value.get('subject'), str) or not value['subject'].strip():
        return 'subject'
    if value.get('world_actor_id') is None:
        for key in ('identity_summary', 'current_agenda'):
            if key in value and (not isinstance(value[key], str) or not value[key].strip()):
                return key
    if value.get('scope') not in _S1_ENCOUNTER_CAPABILITY_SCOPES:
        return 'scope'
    if type(value.get('basis_level')) is not int or value['basis_level'] < 1:
        return 'basis_level'
    if not _is_s1_encounter_capability_axes(value.get('typical_profile')):
        return 'typical_profile'
    if not _is_s1_encounter_capability_axes(value.get('current_profile')):
        return 'current_profile'
    if value.get('condition') not in _S1_ENCOUNTER_CAPABILITY_CONDITIONS:
        return 'condition'
    if value.get('number_pressure') not in _S1_ENCOUNTER_NUMBER_PRESSURES:
        return 'number_pressure'
    world_actor_id = value.get('world_actor_id')
    capability_revision = value.get('capability_revision')
    if world_actor_id is None and capability_revision is not None:
        return 'capability_revision'
    if world_actor_id is not None and (type(world_actor_id) is not int or world_actor_id < 1 or type(capability_revision) is not int or (capability_revision < 1) or (value.get('scope') != 'individual')):
        return 'world_actor_id'
    for key in ('capabilities', 'limitations'):
        if not isinstance(value.get(key), list) or any((not isinstance(item, str) or not item.strip() for item in value[key])):
            return key
    return None

def _get_s1_lethal_pressure_structure_error(value: object) -> str | None:
    from plugins.adventurer_s1.services.s1_outline_pressure_contract import normalize_pressure_structure
    return normalize_pressure_structure(value)[1]

def _format_s1_objective_outline_reject_meta(meta: dict | None) -> str:
    if not isinstance(meta, dict):
        return 'unknown_contract_error'
    parts = []
    for key in ('reason', 'path', 'expected', 'actual_type', 'actual', 'missing_fields', 'unexpected_fields', 'invalid_field', 'invalid_index', 'actor_usage_mode', 'required_world_story_thread_ids'):
        if key not in meta:
            continue
        encoded = json.dumps(meta[key], ensure_ascii=False, default=str, separators=(',', ':'))
        parts.append(f'{key}={encoded[:240]}')
    return ' '.join(parts)[:1000] or 'unknown_contract_error'
_S1_OBJECTIVE_OUTLINE_RETRY_CONTRACTS = {'invalid_pressure_capability_reference': '每项压力引用实际存在的能力位置。新画像按本次输出的 profile_index 引用；已有角色按获准直接参与的准确 world_actor_id 及冻结 capabilities 位置引用。', 'special_event_lethal_pressure_required': '本次类别要求当前核心具有实质致命压力。沿相容的开场局势建立核心本领，输出非空 lethal_pressure 及逐索引对应的 lethal_pressure_structures；旧来源的终结与已冻结主体能力继续有效，成员应对和结果留给正文。', 'unknown_world_actor_id': 'world_actor_usages 只逐项回传“相关长期世界角色（结构化权威当前态）”中明确提供的准确 world_actor_id 与 capability_revision；输入不含该角色块时省略字段。', 'world_actor_direct_use_not_authorized': '长期角色仅从输入列出的 usage_mode 允许值中选择；只读角色使用 ignored 或 background_reference，并保持无绑定能力画像。', 'world_actor_direct_use_terminal_state': '权威终态角色使用 ignored 或 background_reference，并保持无绑定能力画像。', 'world_actor_direct_use_authority_profile_missing': '具备正 capability_revision 与权威能力画像的角色才采用完整本体直接使用组合；其余角色使用 ignored 或 background_reference，并保持无绑定能力画像。', 'world_actor_direct_use_story_thread_not_adopted': '长期角色选择 exclusive_direct_use 时，其输入列出的本体直接使用组合中至少一个 world_story_thread_id 也在 world_intel_usages 选择 exclusive_direct_use；其余使用模式保持角色不绑定能力画像。', 'world_actor_stable_identity_requires_direct_use': '使用长期角色正式名或别名的能力画像属于该稳定身份。输入允许 exclusive_direct_use 时，它只在角色、关联公共线和准确 ID/版本共同组成的完整本体直接使用组合中出现；输入仅允许背景模式时，局部衍生主体使用不同的稳定 subject，并将两个身份字段设为 null。', 'world_actor_identity_binding_missing': '已选择 exclusive_direct_use 的长期角色画像填写准确 world_actor_id 与 capability_revision。', 'world_actor_capability_profile_requires_direct_use': '绑定 world_actor_id 的能力画像只对应同一角色的 exclusive_direct_use；background_reference 与 ignored 保持无绑定画像。', 'duplicate_world_actor_capability_profile': '每个 exclusive_direct_use 长期角色只绑定一个 individual 权威能力画像。', 'invalid_active_faction_keys': 'active_faction_keys 输出 JSON 字符串数组；没有势力直接参与本场主线时输出 []。', 'unknown_active_faction_key': 'active_faction_keys 只能选择输入中随本次 exclusive_direct_use 公共世界线提供的精确 faction_key；背景提及、ignored 与未被本场直接推进的势力不进入该数组。', 'required_faction_interaction_not_adopted': '输入中每条标为“本次必须兑现的酒馆势力交互”的准确 world_story_thread_id 都必须在 world_intel_usages 选择 exclusive_direct_use，并在 objective_situation 建立外部势力对深渊酒馆冒险者生态已经成立的行动、机会或阻力，后续交互与阶段变化由正文裁决。', 'required_faction_interaction_world_change_disabled': '存在“本次必须兑现的酒馆势力交互”时，world_state_change_allowed 必须为 true，以允许正文结果按同一 thread_id/source_revision 回写公共阶段。'}

def _build_s1_objective_outline_retry_feedback(meta: dict | None) -> str:
    """只把稳定业务错误码与正向契约交给同阶段复核，不回灌原始输出。"""
    reason = str((meta or {}).get('reason') or 'unknown_contract_error')
    contract = _S1_OBJECTIVE_OUTLINE_RETRY_CONTRACTS.get(reason, '按原 system prompt 的字段类型、实体身份、版本与跨字段契约重新完成本次输出。')
    if reason == 'invalid_lethal_pressure_structure':
        from plugins.adventurer_s1.services.s1_outline_pressure_contract import LEGACY_KEYS, REFERENCE_KEYS, PRESSURE_STRUCTURE_OUTPUT_SHAPE
        contract = PRESSURE_STRUCTURE_OUTPUT_SHAPE
        fields = LEGACY_KEYS | REFERENCE_KEYS
        missing = (meta or {}).get('missing_fields')
        if isinstance(missing, list):
            missing = sorted({key for key in missing if isinstance(key, str) and key in fields})
            if missing:
                contract += '\n缺失必填字段：' + '、'.join(missing) + '。'
        invalid = (meta or {}).get('invalid_field')
        if isinstance(invalid, str) and invalid in fields:
            contract += '\n需按上述类型修正字段：' + invalid + '。'
    if reason == 'special_event_persistent_actor_missing':
        contract = '本次红黑事件至少由一个长期实体实际参与：使用获准直接参与的准确 world_actor 身份，或在 encounter_capability_profiles 提供 individual 主体及非空 identity_summary、current_agenda。沿用输入权限与既有身份，新主体保持相容的独立身份。'
    if reason in {'temporary_location_not_object', 'temporary_location_cardinality_invalid', 'temporary_location_name_invalid', 'temporary_location_entry_basis_missing', 'temporary_location_not_allowed', 'temporary_location_is_configured_dungeon'}:
        from plugins.adventurer_s1.services.s1_temporary_location_service import TEMPORARY_LOCATION_OUTLINE_RULE
        contract = TEMPORARY_LOCATION_OUTLINE_RULE
    return f'【同阶段输出契约复核】\n上次输出未通过业务契约，稳定错误码：{reason}\n本次复核契约：{contract}\n保持原始输入事实与裁决职责不变，重新输出一份完整单行 JSON。'

def _parse_s1_objective_outline_with_meta(content: str | S1AiJsonResult | None, *, expected_target_dungeon: str | None=None, expected_special_event_kind: str | None=None, expected_world_intel_items: list[dict] | None=None, expected_world_actor_states: list[dict] | None=None, expected_world_identity_guards: dict | None=None, expected_faction_keys: list[str] | None=None, temporary_location_allowed: bool=True) -> tuple[dict | None, dict | None]:
    json_result = content if isinstance(content, S1AiJsonResult) else parse_s1_ai_json_content(content, repair_mode='syntax_only')
    json_result = apply_s1_ai_field_schema(json_result, field_schema=_S1_OBJECTIVE_OUTLINE_FIELD_SCHEMA, contract='objective_outline')
    outline, meta = _validate_s1_objective_outline_with_meta(json_result, expected_target_dungeon=expected_target_dungeon, expected_special_event_kind=expected_special_event_kind, expected_world_intel_items=expected_world_intel_items, expected_world_actor_states=expected_world_actor_states, expected_world_identity_guards=expected_world_identity_guards, expected_faction_keys=expected_faction_keys, temporary_location_allowed=temporary_location_allowed)
    if json_result.field_name_diagnostics:
        meta = {**(meta or {}), 'field_name_compatibility': json_result.field_name_diagnostics}
        if json_result.evidence is not None:
            json_result.evidence.contract_diagnostics.update({'objective_outline': {'accepted': outline is not None, 'details': meta}})
    return (outline, meta)

def _validate_s1_objective_outline_with_meta(content: S1AiJsonResult, *, expected_target_dungeon: str | None=None, expected_special_event_kind: str | None=None, expected_world_intel_items: list[dict] | None=None, expected_world_actor_states: list[dict] | None=None, expected_world_identity_guards: dict | None=None, expected_faction_keys: list[str] | None=None, temporary_location_allowed: bool=True) -> tuple[dict | None, dict | None]:
    from plugins.adventurer_s1.services.s1_world_identity_service import exact_guarded_actor_ids
    json_result = content if isinstance(content, S1AiJsonResult) else parse_s1_ai_json_content(content, repair_mode='syntax_only')
    if json_result.error_code:
        return (None, {'reason': json_result.error_code, 'path': '$', 'expected': 'valid_json_object', 'actual': json_result.error_detail, 'finish_reason': json_result.finish_reason, 'raw_excerpt': json_result.raw_excerpt})
    parsed = json_result.data
    raw_excerpt = json_result.raw_excerpt
    json_repair_applied = json_result.repair_applied
    if not isinstance(parsed, dict):
        return (None, {'reason': 'not_object', 'path': '$', 'expected': 'object', 'actual_type': type(parsed).__name__, 'raw_excerpt': raw_excerpt})
    parsed.setdefault(_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD, [])
    parsed.setdefault(_S1_WORLD_INTEL_USAGE_FIELD, [])
    parsed.setdefault(_S1_WORLD_ACTOR_USAGE_FIELD, [])
    parsed.setdefault(_S1_ACTIVE_FACTION_KEYS_FIELD, [])
    if expected_special_event_kind in (None, 'normal'):
        parsed.setdefault(_S1_SPECIAL_EVENT_DELTA_FIELD, {'present': False, 'ordinary_baseline': '', 'material_delta': '', 'causal_entry': '', 'player_visible_signature': ''})
    parsed = {key: value for key, value in parsed.items() if key in _S1_OBJECTIVE_OUTLINE_ALLOWED_FIELDS}
    compatibility_warnings: list[dict] = []
    from plugins.adventurer_s1.services.s1_temporary_location_service import normalize_temporary_location_proposal
    try:
        raw_temporary_location = parsed.get('temporary_location')
        temporary_location = normalize_temporary_location_proposal(raw_temporary_location)
        if temporary_location and (not temporary_location_allowed):
            raise ValueError('temporary_location_not_allowed')
        if temporary_location and temporary_location['location_name'] in {str(expected_target_dungeon or '').strip(), *(row['name'] for row in S1_DUNGEONS)}:
            raise ValueError('temporary_location_is_configured_dungeon')
        if 'temporary_location' in parsed:
            parsed['temporary_location'] = temporary_location
            if raw_temporary_location != temporary_location:
                compatibility_warnings.append({'field': 'temporary_location', 'issue': 'temporary_location_normalized'})
    except ValueError as exc:
        return (None, {'reason': str(exc), 'path': 'temporary_location', 'raw_excerpt': raw_excerpt})
    required_fields = set(_S1_OBJECTIVE_OUTLINE_REQUIRED_FIELDS)
    missing_fields = sorted(required_fields.difference(parsed))
    if missing_fields:
        return (None, {'reason': 'missing_required_fields', 'path': '$', 'missing_fields': missing_fields, 'raw_excerpt': raw_excerpt})
    target_dungeon = parsed.get('target_dungeon')
    if not isinstance(target_dungeon, str) or not target_dungeon.strip():
        return (None, {'reason': 'invalid_target_dungeon', 'path': 'target_dungeon', 'expected': 'non_empty_string', 'actual_type': type(target_dungeon).__name__, 'raw_excerpt': raw_excerpt})
    if expected_target_dungeon and target_dungeon.strip() != str(expected_target_dungeon).strip():
        return (None, {'reason': 'target_dungeon_mismatch', 'path': 'target_dungeon', 'expected': str(expected_target_dungeon).strip(), 'actual': target_dungeon.strip(), 'raw_excerpt': raw_excerpt})
    if not isinstance(parsed.get('objective_situation'), str) or not parsed['objective_situation'].strip():
        value = parsed.get('objective_situation')
        return (None, {'reason': 'invalid_objective_situation', 'path': 'objective_situation', 'expected': 'non_empty_string', 'actual_type': type(value).__name__, 'raw_excerpt': raw_excerpt})
    world_state_change_allowed, normalized_boolean = _normalize_s1_outline_exact_boolean(parsed.get('world_state_change_allowed'))
    if normalized_boolean:
        parsed['world_state_change_allowed'] = world_state_change_allowed
        compatibility_warnings.append({'field': 'world_state_change_allowed', 'issue': 'boolean_string_coerced_to_boolean', 'action': 'accepted_after_full_contract_validation'})
    if not isinstance(parsed.get('world_state_change_allowed'), bool):
        value = parsed.get('world_state_change_allowed')
        return (None, {'reason': 'invalid_world_state_change_allowed', 'path': 'world_state_change_allowed', 'expected': 'boolean', 'actual_type': type(value).__name__, 'raw_excerpt': raw_excerpt})
    special_event_delta = parsed.get(_S1_SPECIAL_EVENT_DELTA_FIELD)
    if not isinstance(special_event_delta, dict) or not _S1_SPECIAL_EVENT_DELTA_KEYS.issubset(special_event_delta):
        return (None, {'reason': 'invalid_special_event_delta', 'path': _S1_SPECIAL_EVENT_DELTA_FIELD, 'expected': sorted(_S1_SPECIAL_EVENT_DELTA_KEYS), 'actual': sorted(map(str, special_event_delta)) if isinstance(special_event_delta, dict) else None, 'raw_excerpt': raw_excerpt})
    special_event_delta = {key: value for key, value in special_event_delta.items() if key in _S1_SPECIAL_EVENT_DELTA_KEYS}
    delta_present, normalized_boolean = _normalize_s1_outline_exact_boolean(special_event_delta.get('present'))
    if normalized_boolean:
        special_event_delta['present'] = delta_present
        compatibility_warnings.append({'field': f'{_S1_SPECIAL_EVENT_DELTA_FIELD}.present', 'issue': 'boolean_string_coerced_to_boolean', 'action': 'accepted_after_full_contract_validation'})
    delta_present = special_event_delta.get('present')
    delta_text_fields = ('ordinary_baseline', 'material_delta', 'causal_entry', 'player_visible_signature')
    delta_texts = [special_event_delta.get(field) for field in delta_text_fields]
    expected_delta_present = bool(expected_special_event_kind and expected_special_event_kind != 'normal')
    if expected_special_event_kind == 'normal' and delta_present is False and all((isinstance(value, str) for value in delta_texts)) and any((value.strip() for value in delta_texts)):
        compatibility_warnings.append({'field': _S1_SPECIAL_EVENT_DELTA_FIELD, 'issue': 'inactive_special_event_texts_cleared', 'fields': [field for field in delta_text_fields if special_event_delta[field].strip()], 'action': 'accepted_after_full_contract_validation'})
        special_event_delta.update(dict.fromkeys(delta_text_fields, ''))
        delta_texts = [special_event_delta[field] for field in delta_text_fields]
    valid_delta_contract = delta_present is expected_delta_present and all((isinstance(value, str) for value in delta_texts)) and (all((value.strip() for value in delta_texts)) if expected_delta_present else all((not value.strip() for value in delta_texts)))
    if not valid_delta_contract:
        return (None, {'reason': 'invalid_special_event_delta', 'path': _S1_SPECIAL_EVENT_DELTA_FIELD, 'expected': 'present_true_with_four_non_empty_strings' if expected_delta_present else 'present_false_with_four_empty_strings', 'actual': special_event_delta, 'raw_excerpt': raw_excerpt})
    parsed[_S1_SPECIAL_EVENT_DELTA_FIELD] = {'present': delta_present, **{field: special_event_delta[field].strip() for field in delta_text_fields}}
    for key in _S1_OBJECTIVE_OUTLINE_LIST_FIELDS:
        value = parsed.get(key)
        if isinstance(value, str):
            clean_value = value.strip()
            parsed[key] = [clean_value] if clean_value else []
    for key in _S1_OBJECTIVE_OUTLINE_LIST_FIELDS:
        if not isinstance(parsed.get(key), list):
            return (None, {'reason': 'invalid_list_field', 'path': key, 'expected': 'array_of_non_empty_strings', 'actual_type': type(parsed.get(key)).__name__, 'raw_excerpt': raw_excerpt})
    for key in _S1_OBJECTIVE_OUTLINE_LIST_FIELDS:
        for index, item in enumerate(parsed[key]):
            if not isinstance(item, str) or not item.strip():
                return (None, {'reason': 'invalid_list_item', 'path': f'{key}[{index}]', 'expected': 'non_empty_string', 'actual_type': type(item).__name__, 'invalid_field': key, 'invalid_index': index, 'raw_excerpt': raw_excerpt})
    if expected_special_event_kind in {'local_anomaly', 'regional_incident', 'world_fact_catalyst', 'extreme_crisis', 'abyss', 'unbeatable_horror'} and (not parsed['lethal_pressure']):
        return (None, {'reason': 'special_event_lethal_pressure_required', 'path': 'lethal_pressure', 'expected': 'non_empty_array_for_current_special_event_core', 'special_event_kind': expected_special_event_kind, 'raw_excerpt': raw_excerpt})
    pressure_structures = parsed.get(_S1_LETHAL_PRESSURE_STRUCTURE_FIELD)
    if not isinstance(pressure_structures, list):
        return (None, {'reason': 'invalid_lethal_pressure_structures', 'path': _S1_LETHAL_PRESSURE_STRUCTURE_FIELD, 'expected': 'array', 'actual_type': type(pressure_structures).__name__, 'raw_excerpt': raw_excerpt})
    normalized_pressure_structures: list[dict] = []
    seen_pressure_indices: set[int] = set()
    from plugins.adventurer_s1.services.s1_outline_pressure_contract import LEGACY_KEYS, REFERENCE_KEYS, normalize_pressure_structure
    for index, structure in enumerate(pressure_structures):
        path = f'{_S1_LETHAL_PRESSURE_STRUCTURE_FIELD}[{index}]'
        if isinstance(structure, dict):
            structure = {key: value for key, value in structure.items() if key in _S1_LETHAL_PRESSURE_STRUCTURE_KEYS}
            normalized_value, normalized_integer = _normalize_s1_outline_ascii_integer(structure.get('pressure_index'), minimum=0)
            if normalized_integer:
                structure['pressure_index'] = normalized_value
                compatibility_warnings.append({'field': f'{path}.pressure_index', 'issue': 'numeric_string_coerced_to_integer', 'action': 'accepted_after_full_contract_validation'})
        expected_structure_keys = REFERENCE_KEYS if isinstance(structure, dict) and ('capability_refs' in structure or 'resolution_modes' in structure) else LEGACY_KEYS
        missing_structure_fields = sorted(expected_structure_keys.difference(structure) if isinstance(structure, dict) else expected_structure_keys)
        singleton_capability_ref = isinstance(structure, dict) and isinstance(structure.get('capability_refs'), dict)
        structure, structure_error = normalize_pressure_structure(structure)
        if structure_error:
            return (None, {'reason': 'invalid_lethal_pressure_structure', 'path': path, 'expected': sorted(expected_structure_keys), 'invalid_field': structure_error, 'missing_fields': missing_structure_fields, 'raw_excerpt': raw_excerpt})
        if singleton_capability_ref:
            compatibility_warnings.append({'field': f'{path}.capability_refs', 'issue': 'singleton_object_coerced_to_array', 'action': 'accepted_after_full_contract_validation'})
        pressure_index = structure['pressure_index']
        if pressure_index >= len(parsed['lethal_pressure']) or pressure_index in seen_pressure_indices:
            return (None, {'reason': 'invalid_lethal_pressure_structure_index', 'path': f'{path}.pressure_index', 'expected': list(range(len(parsed['lethal_pressure']))), 'actual': pressure_index, 'raw_excerpt': raw_excerpt})
        seen_pressure_indices.add(pressure_index)
        normalized_pressure_structures.append(structure)
    if pressure_structures and seen_pressure_indices != set(range(len(parsed['lethal_pressure']))):
        return (None, {'reason': 'lethal_pressure_structure_coverage_mismatch', 'path': _S1_LETHAL_PRESSURE_STRUCTURE_FIELD, 'expected': list(range(len(parsed['lethal_pressure']))), 'actual': sorted(seen_pressure_indices), 'raw_excerpt': raw_excerpt})
    if parsed['lethal_pressure'] and (not pressure_structures):
        return (None, {'reason': 'lethal_pressure_structure_coverage_mismatch', 'path': _S1_LETHAL_PRESSURE_STRUCTURE_FIELD, 'expected': list(range(len(parsed['lethal_pressure']))), 'actual': [], 'raw_excerpt': raw_excerpt})
    parsed[_S1_LETHAL_PRESSURE_STRUCTURE_FIELD] = normalized_pressure_structures
    if _S1_TRAIT_CONTEXT_TAGS_FIELD in parsed:
        from plugins.adventurer_s1.services.s1_trait_context_selection_service import project_outline_trait_context_scope
        trait_context_tags, scope_status, trait_context_errors = project_outline_trait_context_scope(parsed)
        if scope_status == 'invalid_tags':
            compatibility_warnings.append({'field': _S1_TRAIT_CONTEXT_TAGS_FIELD, 'issue': 'invalid_auxiliary_trait_context_tags', 'errors': list(trait_context_errors), 'action': 'invalid_marker_and_trait_selection_fail_open'})
            parsed[_S1_TRAIT_CONTEXT_TAGS_FIELD] = []
        elif scope_status == 'tagged':
            parsed[_S1_TRAIT_CONTEXT_TAGS_FIELD] = list(trait_context_tags)
    world_intel_usages = parsed.get(_S1_WORLD_INTEL_USAGE_FIELD)
    if not isinstance(world_intel_usages, list):
        return (None, {'reason': 'invalid_world_intel_usages', 'path': _S1_WORLD_INTEL_USAGE_FIELD, 'expected': 'array', 'actual_type': type(world_intel_usages).__name__, 'raw_excerpt': raw_excerpt})
    expected_items_by_id: dict[int, dict] = {}
    if expected_world_intel_items is not None:
        for item in expected_world_intel_items:
            if not isinstance(item, dict):
                continue
            thread_id = _coerce_int(item.get('id') or item.get('world_story_thread_id'), 0)
            if thread_id > 0:
                expected_items_by_id[thread_id] = item
    seen_usage_ids: set[int] = set()
    normalized_usages: list[dict] = []
    for index, usage in enumerate(world_intel_usages):
        path = f'{_S1_WORLD_INTEL_USAGE_FIELD}[{index}]'
        if not isinstance(usage, dict) or not _S1_WORLD_INTEL_USAGE_KEYS.issubset(usage):
            return (None, {'reason': 'invalid_world_intel_usage', 'path': path, 'expected': sorted(_S1_WORLD_INTEL_USAGE_KEYS), 'actual': sorted(map(str, usage)) if isinstance(usage, dict) else None, 'raw_excerpt': raw_excerpt})
        normalized_usage = {key: value for key, value in usage.items() if key in _S1_WORLD_INTEL_USAGE_KEYS}
        for field in ('world_story_thread_id', 'source_revision'):
            normalized_value, normalized_integer = _normalize_s1_outline_ascii_integer(normalized_usage.get(field), minimum=1)
            if normalized_integer:
                normalized_usage[field] = normalized_value
                compatibility_warnings.append({'field': f'{path}.{field}', 'issue': 'numeric_string_coerced_to_integer', 'action': 'accepted_after_full_contract_validation'})
        thread_id = normalized_usage.get('world_story_thread_id')
        source_revision = normalized_usage.get('source_revision')
        usage_mode = usage.get('usage_mode')
        if type(thread_id) is not int or thread_id <= 0 or thread_id in seen_usage_ids:
            return (None, {'reason': 'invalid_world_intel_usage_id', 'path': f'{path}.world_story_thread_id', 'actual': thread_id, 'raw_excerpt': raw_excerpt})
        if type(source_revision) is not int or source_revision <= 0:
            return (None, {'reason': 'invalid_world_intel_usage_revision', 'path': f'{path}.source_revision', 'actual': source_revision, 'raw_excerpt': raw_excerpt})
        if usage_mode not in _S1_WORLD_INTEL_USAGE_MODES:
            return (None, {'reason': 'invalid_world_intel_usage_mode', 'path': f'{path}.usage_mode', 'expected': sorted(_S1_WORLD_INTEL_USAGE_MODES), 'actual': usage_mode, 'raw_excerpt': raw_excerpt})
        try:
            validated_usage = _S1WorldIntelUsageModel.model_validate(normalized_usage).model_dump()
        except ValidationError as exc:
            return (None, {'reason': 'invalid_world_intel_usage', 'path': path, 'expected': 'strict_world_intel_usage_object', 'actual': exc.errors(include_url=False)[:3], 'raw_excerpt': raw_excerpt})
        thread_id = validated_usage['world_story_thread_id']
        source_revision = validated_usage['source_revision']
        usage_mode = validated_usage['usage_mode']
        expected_item = expected_items_by_id.get(thread_id)
        if expected_world_intel_items is not None and (not expected_item):
            return (None, {'reason': 'unknown_world_intel_usage_id', 'path': f'{path}.world_story_thread_id', 'actual': thread_id, 'raw_excerpt': raw_excerpt})
        if expected_item:
            expected_revision = max(1, _coerce_int(expected_item.get('revision'), 1))
            if source_revision != expected_revision:
                return (None, {'reason': 'world_intel_usage_revision_mismatch', 'path': f'{path}.source_revision', 'expected': expected_revision, 'actual': source_revision, 'raw_excerpt': raw_excerpt})
            if expected_item.get('required_faction_presence') is True and (usage_mode == 'ignored' or (usage_mode == 'exclusive_direct_use' and str(expected_item.get('access_mode') or '').strip() != 'mutable')):
                usage_mode = 'background_reference'
                validated_usage['usage_mode'] = usage_mode
                compatibility_warnings.append({'field': f'{path}.usage_mode', 'issue': 'faction_presence_soft_floor_applied', 'action': 'normalized_to_background_reference_without_retry'})
            if usage_mode == 'exclusive_direct_use' and str(expected_item.get('access_mode') or '').strip() != 'mutable':
                return (None, {'reason': 'world_intel_direct_use_not_authorized', 'path': f'{path}.usage_mode', 'expected': 'mutable_access_required', 'actual': expected_item.get('access_mode'), 'raw_excerpt': raw_excerpt})
        seen_usage_ids.add(thread_id)
        normalized_usages.append({'world_story_thread_id': thread_id, 'source_revision': source_revision, 'usage_mode': usage_mode})
    if expected_world_intel_items is not None:
        for thread_id, expected_item in expected_items_by_id.items():
            if thread_id in seen_usage_ids:
                continue
            if expected_item.get('required_faction_interaction') is True or expected_item.get('required_campaign_manifestation') is True:
                continue
            soft_presence = expected_item.get('required_faction_presence') is True
            normalized_usages.append({'world_story_thread_id': thread_id, 'source_revision': max(1, _coerce_int(expected_item.get('revision'), 1)), 'usage_mode': 'background_reference' if soft_presence else 'ignored'})
            seen_usage_ids.add(thread_id)
            compatibility_warnings.append({'field': _S1_WORLD_INTEL_USAGE_FIELD, 'issue': 'faction_presence_usage_missing_soft_filled' if soft_presence else 'world_intel_usage_missing_defaulted', 'action': 'filled_as_background_reference_without_retry' if soft_presence else 'filled_as_ignored_without_retry'})
    if expected_world_intel_items is not None and seen_usage_ids != set(expected_items_by_id):
        return (None, {'reason': 'world_intel_usage_items_mismatch', 'path': _S1_WORLD_INTEL_USAGE_FIELD, 'expected': sorted(expected_items_by_id), 'actual': sorted(seen_usage_ids), 'raw_excerpt': raw_excerpt})
    parsed[_S1_WORLD_INTEL_USAGE_FIELD] = normalized_usages
    direct_world_story_thread_ids = {int(item['world_story_thread_id']) for item in normalized_usages if item.get('usage_mode') == 'exclusive_direct_use'}
    required_faction_interaction_thread_ids = {thread_id for thread_id, item in expected_items_by_id.items() if item.get('required_faction_interaction') is True}
    missing_required_faction_interactions = sorted(required_faction_interaction_thread_ids.difference(direct_world_story_thread_ids))
    if missing_required_faction_interactions:
        return (None, {'reason': 'required_faction_interaction_not_adopted', 'path': _S1_WORLD_INTEL_USAGE_FIELD, 'expected': sorted(required_faction_interaction_thread_ids), 'actual': sorted(direct_world_story_thread_ids), 'raw_excerpt': raw_excerpt})
    if required_faction_interaction_thread_ids and parsed.get('world_state_change_allowed') is not True:
        return (None, {'reason': 'required_faction_interaction_world_change_disabled', 'path': 'world_state_change_allowed', 'expected': True, 'actual': parsed.get('world_state_change_allowed'), 'raw_excerpt': raw_excerpt})
    required_campaign_manifestation_thread_ids = {thread_id for thread_id, item in expected_items_by_id.items() if item.get('required_campaign_manifestation') is True}
    missing_required_campaign_manifestations = sorted(required_campaign_manifestation_thread_ids.difference(direct_world_story_thread_ids))
    if missing_required_campaign_manifestations:
        return (None, {'reason': 'required_campaign_manifestation_not_adopted', 'path': _S1_WORLD_INTEL_USAGE_FIELD, 'expected': sorted(required_campaign_manifestation_thread_ids), 'actual': sorted(direct_world_story_thread_ids), 'raw_excerpt': raw_excerpt})
    if required_campaign_manifestation_thread_ids and parsed.get('world_state_change_allowed') is not True:
        return (None, {'reason': 'required_campaign_manifestation_world_change_disabled', 'path': 'world_state_change_allowed', 'expected': True, 'actual': parsed.get('world_state_change_allowed'), 'raw_excerpt': raw_excerpt})
    raw_actor_usages = parsed.get(_S1_WORLD_ACTOR_USAGE_FIELD)
    if not isinstance(raw_actor_usages, list):
        return (None, {'reason': 'invalid_world_actor_usages', 'path': _S1_WORLD_ACTOR_USAGE_FIELD, 'expected': 'array', 'actual_type': type(raw_actor_usages).__name__, 'raw_excerpt': raw_excerpt})
    expected_actors_by_id = {int(item['id']): item for item in expected_world_actor_states or [] if isinstance(item, dict) and type(item.get('id')) is int and (item['id'] > 0)}
    normalized_actor_usages = []
    seen_actor_ids: set[int] = set()
    direct_actor_ids: set[int] = set()
    discarded_ignored_actor_ids: set[int] = set()
    discarded_ignored_actor_usage_count = 0
    for index, raw_usage in enumerate(raw_actor_usages):
        path = f'{_S1_WORLD_ACTOR_USAGE_FIELD}[{index}]'
        normalized_usage = raw_usage
        if isinstance(raw_usage, dict):
            normalized_usage = {key: value for key, value in raw_usage.items() if key in _S1_WORLD_ACTOR_USAGE_KEYS}
            for field, minimum in (('world_actor_id', 1), ('capability_revision', 0)):
                normalized_value, normalized_integer = _normalize_s1_outline_ascii_integer(normalized_usage.get(field), minimum=minimum)
                if normalized_integer:
                    normalized_usage[field] = normalized_value
                    compatibility_warnings.append({'field': f'{path}.{field}', 'issue': 'numeric_string_coerced_to_integer', 'action': 'accepted_after_full_contract_validation'})
        try:
            usage = _S1WorldActorUsageModel.model_validate(normalized_usage)
        except ValidationError as exc:
            return (None, {'reason': 'invalid_world_actor_usage', 'path': path, 'expected': 'strict_world_actor_usage_object', 'actual': exc.errors(include_url=False), 'raw_excerpt': raw_excerpt})
        actor_id = usage.world_actor_id
        if expected_world_actor_states is not None and (not expected_actors_by_id) and (usage.usage_mode == 'ignored'):
            discarded_ignored_actor_ids.add(actor_id)
            discarded_ignored_actor_usage_count += 1
            continue
        if actor_id in seen_actor_ids:
            return (None, {'reason': 'duplicate_world_actor_usage', 'path': f'{path}.world_actor_id', 'actual': actor_id, 'raw_excerpt': raw_excerpt})
        expected_actor = expected_actors_by_id.get(actor_id)
        if expected_actor is None:
            return (None, {'reason': 'unknown_world_actor_id', 'path': f'{path}.world_actor_id', 'actual': actor_id, 'raw_excerpt': raw_excerpt})
        expected_revision = int(expected_actor.get('capability_revision') or 0)
        if usage.capability_revision != expected_revision:
            return (None, {'reason': 'world_actor_capability_revision_mismatch', 'path': f'{path}.capability_revision', 'expected': expected_revision, 'actual': usage.capability_revision, 'raw_excerpt': raw_excerpt})
        if usage.usage_mode == 'exclusive_direct_use':
            authority_profile = normalize_actor_capability_profile(expected_actor.get('capability_profile'))
            if expected_actor.get('access_mode') != '可直接推进':
                return (None, {'reason': 'world_actor_direct_use_not_authorized', 'path': f'{path}.usage_mode', 'expected': '可直接推进', 'actual': expected_actor.get('access_mode'), 'raw_excerpt': raw_excerpt})
            if expected_actor.get('status') in {'dead', 'retired'}:
                return (None, {'reason': 'world_actor_direct_use_terminal_state', 'path': f'{path}.usage_mode', 'expected': 'active_non_terminal_actor', 'actual': expected_actor.get('status'), 'raw_excerpt': raw_excerpt})
            if expected_revision < 1 or authority_profile is None:
                return (None, {'reason': 'world_actor_direct_use_authority_profile_missing', 'path': f'{path}.usage_mode', 'expected': 'positive_revision_with_authoritative_capability_profile', 'actual': expected_revision, 'raw_excerpt': raw_excerpt})
            required_thread_ids = {int(value) for value in expected_actor.get('direct_world_story_thread_ids') or [] if type(value) is int}
            if not direct_world_story_thread_ids.intersection(required_thread_ids):
                return (None, {'reason': 'world_actor_direct_use_story_thread_not_adopted', 'path': f'{path}.usage_mode', 'expected': 'at_least_one_linked_thread_exclusive_direct_use', 'actual': sorted(direct_world_story_thread_ids), 'required_world_story_thread_ids': sorted(required_thread_ids), 'raw_excerpt': raw_excerpt})
            direct_actor_ids.add(actor_id)
        seen_actor_ids.add(actor_id)
        normalized_actor_usages.append(usage.model_dump())
    if discarded_ignored_actor_usage_count:
        compatibility_warnings.append({'field': _S1_WORLD_ACTOR_USAGE_FIELD, 'issue': 'ignored_usage_without_actor_authority', 'action': 'normalized_to_empty_list', 'count': discarded_ignored_actor_usage_count, 'world_actor_ids': sorted(discarded_ignored_actor_ids)})
    if expected_world_actor_states is not None:
        missing_actor_ids = sorted(set(expected_actors_by_id).difference(seen_actor_ids))
        for actor_id in missing_actor_ids:
            normalized_actor_usages.append({'world_actor_id': actor_id, 'capability_revision': int(expected_actors_by_id[actor_id].get('capability_revision') or 0), 'usage_mode': 'ignored'})
        if missing_actor_ids:
            compatibility_warnings.append({'field': _S1_WORLD_ACTOR_USAGE_FIELD, 'issue': 'world_actor_usage_missing_defaulted', 'action': 'filled_as_ignored_without_retry', 'world_actor_ids': missing_actor_ids})
    parsed[_S1_WORLD_ACTOR_USAGE_FIELD] = normalized_actor_usages
    raw_active_faction_keys = parsed.get(_S1_ACTIVE_FACTION_KEYS_FIELD)
    if not isinstance(raw_active_faction_keys, list) or len(raw_active_faction_keys) > 8 or any((not isinstance(value, str) or not value.strip() for value in raw_active_faction_keys)):
        return (None, {'reason': 'invalid_active_faction_keys', 'path': _S1_ACTIVE_FACTION_KEYS_FIELD, 'expected': 'array_of_1_to_8_non_empty_strings_or_empty_array', 'actual_type': type(raw_active_faction_keys).__name__, 'raw_excerpt': raw_excerpt})
    normalized_active_faction_keys = [value.strip() for value in raw_active_faction_keys]
    if len(set(normalized_active_faction_keys)) != len(normalized_active_faction_keys):
        return (None, {'reason': 'invalid_active_faction_keys', 'path': _S1_ACTIVE_FACTION_KEYS_FIELD, 'expected': 'unique_exact_faction_keys', 'actual': normalized_active_faction_keys, 'raw_excerpt': raw_excerpt})
    authorized_faction_keys = {str(value).strip() for actor in expected_world_actor_states or [] if isinstance(actor, dict) and direct_world_story_thread_ids.intersection({int(thread_id) for thread_id in actor.get('direct_world_story_thread_ids') or [] if type(thread_id) is int}) for value in actor.get('faction_keys') or [] if isinstance(value, str) and value.strip()}
    if expected_faction_keys is not None:
        authorized_faction_keys.intersection_update({str(value).strip() for value in expected_faction_keys if isinstance(value, str) and value.strip()})
    unknown_active_faction_keys = sorted(set(normalized_active_faction_keys).difference(authorized_faction_keys))
    if unknown_active_faction_keys:
        return (None, {'reason': 'unknown_active_faction_key', 'path': _S1_ACTIVE_FACTION_KEYS_FIELD, 'expected': sorted(authorized_faction_keys), 'actual': unknown_active_faction_keys, 'raw_excerpt': raw_excerpt})
    parsed[_S1_ACTIVE_FACTION_KEYS_FIELD] = normalized_active_faction_keys
    capability_profiles = parsed.get(_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD)
    validation_warnings = compatibility_warnings
    if json_repair_applied:
        validation_warnings.append({'field': '$', 'issue': 'json_repair_applied', 'action': 'accepted_after_full_contract_validation'})
    if not isinstance(capability_profiles, list):
        validation_warnings.append({'field': _S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD, 'issue': 'invalid_container', 'action': 'replaced_with_empty_list'})
        capability_profiles = []
    valid_capability_profiles = []
    original_profile_to_valid = {}
    capability_profile_actor_ids: set[int] = set()
    actor_usage_mode_by_id = {int(item['world_actor_id']): item.get('usage_mode') for item in normalized_actor_usages if isinstance(item, dict) and type(item.get('world_actor_id')) is int}
    actor_ids_by_exact_name: dict[str, set[int]] = {}
    for expected_actor_id, expected_actor in expected_actors_by_id.items():
        actor_names = [expected_actor.get('canonical_name'), *(expected_actor.get('aliases') or [])]
        for actor_name in actor_names:
            normalized_name = str(actor_name or '').strip().casefold()
            if normalized_name:
                actor_ids_by_exact_name.setdefault(normalized_name, set()).add(expected_actor_id)
    for index, profile in enumerate(capability_profiles):
        normalized_profile = _normalize_s1_encounter_capability_profile(profile)
        if isinstance(profile, dict) and isinstance(normalized_profile, dict):
            for field in ('world_actor_id', 'capability_revision'):
                if isinstance(profile.get(field), str) and type(normalized_profile.get(field)) is int:
                    validation_warnings.append({'field': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}].{field}', 'issue': 'numeric_string_coerced_to_integer', 'action': 'accepted_after_full_contract_validation'})
        profile_subject = str(normalized_profile.get('subject') or '').strip().casefold() if isinstance(normalized_profile, dict) else ''
        exact_actor_ids = actor_ids_by_exact_name.get(profile_subject, set())
        guarded_actor_ids = exact_guarded_actor_ids(profile_subject, expected_world_identity_guards)
        if guarded_actor_ids and (not guarded_actor_ids.issubset(expected_actors_by_id)):
            return (None, {'reason': 'world_actor_identity_not_in_frozen_authority', 'path': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}].world_actor_id', 'expected': sorted(guarded_actor_ids), 'actual': None, 'raw_excerpt': raw_excerpt})
        declared_actor_id = normalized_profile.get('world_actor_id') if isinstance(normalized_profile, dict) else None
        if exact_actor_ids and declared_actor_id is None:
            exact_actor_usage_modes = {actor_id: actor_usage_mode_by_id.get(actor_id) for actor_id in sorted(exact_actor_ids)}
            if any((mode != 'exclusive_direct_use' for mode in exact_actor_usage_modes.values())):
                return (None, {'reason': 'world_actor_stable_identity_requires_direct_use', 'path': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}].world_actor_id', 'expected': 'exclusive_direct_use_with_exact_identity_binding', 'actual': None, 'actor_usage_mode': exact_actor_usage_modes, 'subject': normalized_profile.get('subject'), 'raw_excerpt': raw_excerpt})
            if len(exact_actor_ids) == 1:
                actor_id = next(iter(exact_actor_ids))
                expected_actor = expected_actors_by_id[actor_id]
                normalized_profile = {**normalized_profile, 'world_actor_id': actor_id, 'capability_revision': int(expected_actor.get('capability_revision') or 0)}
                declared_actor_id = actor_id
                validation_warnings.append({'field': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}].world_actor_id', 'issue': 'unique_frozen_world_actor_identity_binding_filled', 'action': 'accepted_after_full_contract_validation'})
            else:
                return (None, {'reason': 'world_actor_identity_binding_missing', 'path': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}].world_actor_id', 'expected': sorted(exact_actor_ids), 'actual': None, 'subject': normalized_profile.get('subject'), 'raw_excerpt': raw_excerpt})
        if exact_actor_ids and declared_actor_id not in exact_actor_ids:
            return (None, {'reason': 'world_actor_identity_binding_mismatch', 'path': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}].world_actor_id', 'expected': sorted(exact_actor_ids), 'actual': declared_actor_id, 'subject': normalized_profile.get('subject'), 'raw_excerpt': raw_excerpt})
        if isinstance(normalized_profile, dict) and normalized_profile.get('world_actor_id') is None:
            for field in ('identity_summary', 'current_agenda'):
                if field in normalized_profile and (not isinstance(normalized_profile[field], str) or not normalized_profile[field].strip()):
                    normalized_profile.pop(field)
                    validation_warnings.append({'profile_index': index, 'field': field, 'issue': 'invalid_entity_context_field', 'action': 'omitted_entity_context_field'})
        profile_error = _get_s1_encounter_capability_profile_error(normalized_profile)
        if profile_error:
            if isinstance(normalized_profile, dict) and normalized_profile.get('world_actor_id') is not None:
                return (None, {'reason': 'invalid_world_actor_capability_profile', 'path': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}]', 'invalid_field': profile_error, 'raw_excerpt': raw_excerpt})
            validation_warnings.append({'profile_index': index, 'field': profile_error, 'issue': 'invalid_profile_field', 'action': 'dropped_profile'})
            continue
        actor_id = normalized_profile.get('world_actor_id')
        if actor_id is not None:
            if actor_id not in direct_actor_ids:
                return (None, {'reason': 'world_actor_capability_profile_requires_direct_use', 'path': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}].world_actor_id', 'expected': 'exclusive_direct_use', 'actual': actor_usage_mode_by_id.get(actor_id), 'actor_usage_mode': actor_usage_mode_by_id.get(actor_id), 'raw_excerpt': raw_excerpt})
            if actor_id in capability_profile_actor_ids:
                return (None, {'reason': 'duplicate_world_actor_capability_profile', 'path': f'{_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD}[{index}].world_actor_id', 'expected': 'one_profile_per_direct_world_actor', 'actual': actor_id, 'raw_excerpt': raw_excerpt})
            expected_actor = expected_actors_by_id[actor_id]
            authority_profile = normalize_actor_capability_profile(expected_actor.get('capability_profile'))
            normalized_profile = {'subject': expected_actor.get('canonical_name') or normalized_profile['subject'], 'scope': 'individual', **authority_profile, **{key: expected_actor[key] for key in ('identity_summary', 'current_agenda') if isinstance(expected_actor.get(key), str) and expected_actor[key].strip()}, 'world_actor_id': actor_id, 'capability_revision': int(expected_actor['capability_revision'])}
            capability_profile_actor_ids.add(actor_id)
        original_profile_to_valid[index] = len(valid_capability_profiles)
        valid_capability_profiles.append(normalized_profile)
    for actor_id in sorted(direct_actor_ids - capability_profile_actor_ids):
        expected_actor = expected_actors_by_id[actor_id]
        authority_profile = normalize_actor_capability_profile(expected_actor.get('capability_profile'))
        valid_capability_profiles.append({'subject': expected_actor['canonical_name'], 'scope': 'individual', **authority_profile, **{key: expected_actor[key] for key in ('identity_summary', 'current_agenda') if isinstance(expected_actor.get(key), str) and expected_actor[key].strip()}, 'world_actor_id': actor_id, 'capability_revision': int(expected_actor['capability_revision'])})
    from plugins.adventurer_s1.services.s1_ai_prompt_shared import S1_RED_SPECIAL_EVENT_KINDS
    if expected_special_event_kind in S1_RED_SPECIAL_EVENT_KINDS | {'unbeatable_horror'}:
        if not any((profile.get('scope') == 'individual' and (profile.get('world_actor_id') in direct_actor_ids or all((isinstance(profile.get(key), str) and profile[key].strip() for key in ('identity_summary', 'current_agenda')))) for profile in valid_capability_profiles)):
            return (None, {'reason': 'special_event_persistent_actor_missing', 'path': _S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD, 'expected': 'direct_world_actor_or_defined_individual', 'raw_excerpt': raw_excerpt})
    from plugins.adventurer_s1.services.s1_outline_pressure_contract import bind_pressure_capability_refs
    bound_pressures, pressure_reference_error = bind_pressure_capability_refs(normalized_pressure_structures, valid_capability_profiles, original_profile_to_valid)
    if pressure_reference_error:
        return (None, {'reason': 'invalid_pressure_capability_reference', 'path': pressure_reference_error, 'expected': 'existing_capability_of_valid_new_profile_or_authorized_direct_actor', 'raw_excerpt': raw_excerpt})
    parsed[_S1_LETHAL_PRESSURE_STRUCTURE_FIELD] = bound_pressures
    parsed[_S1_ENCOUNTER_CAPABILITY_PROFILE_FIELD] = valid_capability_profiles
    outline = {key: parsed[key] for key in _S1_OBJECTIVE_OUTLINE_ALLOWED_FIELDS if key in parsed}
    if validation_warnings:
        return (outline, {'non_fatal_warnings': validation_warnings})
    return (outline, None)
_S1_OBJECTIVE_OUTLINE_STRUCTURE_REPAIR_SYSTEM_PROMPT = '你是深渊酒馆 S1 客观局势草案的 JSON 结构修复器。\n你只能修复 JSON 语法、引号、逗号、括号和字段容器类型，禁止改写、扩写、删减或重新裁决任何局势事实。\n所有声明为字符串数组的字段必须输出 JSON 数组；单段字符串只能原样包成单元素数组。\n字符串编码的布尔值只可无损转换为同一真假含义的 JSON boolean，不得改变裁决结果。\n可以补回固定 schema 中缺失的空列表字段，但不得为其生成内容；缺失的事实型字符串或裁决值不得补造。\n不得补造原文不存在的业务事实。只输出单个合法 JSON 对象，不要解释，不要使用代码围栏。'

def _repair_s1_objective_outline_structure(content: str, *, api_base: str, headers: dict, model: str, timeout: int, validation_reason: str | None=None, request_metadata_out: dict[str, str] | None=None, physical_attempt_guard: Callable[[], None] | None=None) -> tuple[str | None, dict | None]:
    """只修复草案 JSON 结构；网络或 HTTP 错误交给外层调用处理。"""

    def validate_outline_structure_repair(decoded) -> S1AiResponseContractResult:
        repaired_result = decoded.json_result
        if repaired_result.ok and isinstance(repaired_result.data, dict):
            return S1AiResponseContractResult.accept(repaired_result.data)
        return S1AiResponseContractResult.reject(repaired_result.error_code or 'outline_structure_repair_invalid', failure_stage='response_parse')
    resp = post_s1_ai_request(f'{api_base}/chat/completions', feature='expedition_outline', request_metadata_out=request_metadata_out, physical_attempt_guard=physical_attempt_guard, headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': _S1_OBJECTIVE_OUTLINE_STRUCTURE_REPAIR_SYSTEM_PROMPT}, {'role': 'user', 'content': f"结构错误代码：{validation_reason or 'unknown'}\n原始输出：\n{content}"}], 'temperature': 0, 'response_format': {'type': 'json_object'}}, timeout=timeout, response_validator=validate_outline_structure_repair)
    decoded = parse_s1_ai_http_json_response(resp)
    repaired_result = decoded.json_result
    return (repaired_result.content if repaired_result.ok else None, decoded.http_result.usage)

def _parse_s1_objective_outline(content: str | None) -> dict | None:
    outline, _meta = _parse_s1_objective_outline_with_meta(content)
    return outline

def _build_s1_objective_outline_special_event_mode(*, special_event_type: str | None=None, special_event_risk_tier: str | None=None, has_special_event: bool=False, hidden_boss_context: dict | None=None) -> str:
    """Use the structured category without a second capability-definition contract."""
    if not has_special_event and (not hidden_boss_context):
        return '【当前特殊事件类别】：normal'
    kind = resolve_s1_special_event_kind({'has_special_event': has_special_event, 'special_event_type': special_event_type, 'special_event_risk_tier': special_event_risk_tier})
    return f'【当前特殊事件类别】：{kind}'

def _persist_objective_outline_to_log(expedition_log_id: int | None, objective_outline: dict | None, reject_meta: dict | None=None, *, generation_claim_token: str | None=None) -> None:
    """把客观局势草案写入 full_log，供管理员查看（玩家侧 API 不下发）。"""
    if not expedition_log_id:
        return
    try:
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).with_for_update().populate_existing().first()
            if not log:
                return
            if generation_claim_token is not None and (log.status != 'generating' or not _owns_expedition_generation(log, generation_claim_token)):
                _reject_lost_expedition_generation_claim(generation_claim_token)
            merged = dict(log.full_log or {})
            if objective_outline:
                merged['objective_outline'] = objective_outline
                merged.pop('objective_outline_rejected_reason', None)
                merged.pop('objective_outline_raw_excerpt', None)
                warnings = reject_meta.get('non_fatal_warnings') if isinstance(reject_meta, dict) else None
                if warnings:
                    merged['objective_outline_validation_warnings'] = warnings
                else:
                    merged.pop('objective_outline_validation_warnings', None)
            else:
                merged.pop('objective_outline', None)
                if isinstance(reject_meta, dict) and reject_meta.get('reason'):
                    reason = str(reject_meta.get('reason') or '')
                    detail_parts = []
                    if reject_meta.get('invalid_field') is not None:
                        detail_parts.append(f"field={reject_meta['invalid_field']}")
                    if reject_meta.get('invalid_index') is not None:
                        detail_parts.append(f"index={reject_meta['invalid_index']}")
                    if reject_meta.get('invalid_profile_index') is not None:
                        detail_parts.append(f"profile={reject_meta['invalid_profile_index']}")
                    if reject_meta.get('invalid_profile_field') is not None:
                        detail_parts.append(f"profile_field={reject_meta['invalid_profile_field']}")
                    if detail_parts:
                        reason = f"{reason} ({', '.join(detail_parts)})"
                    merged['objective_outline_rejected_reason'] = reason[:120]
                    raw_excerpt = str(reject_meta.get('raw_excerpt') or '')[:2000]
                    if raw_excerpt:
                        merged['objective_outline_raw_excerpt'] = raw_excerpt
            log.full_log = merged
    except _S1ExpeditionGenerationClaimLost:
        raise
    except Exception as exc:
        current_app.logger.warning('S1 客观局势草案写回失败 log=%s: %s', expedition_log_id, exc)

def _load_s1_trait_context_selection_receipt(expedition_log_id: int | None) -> dict | None:
    if not expedition_log_id:
        return None
    from plugins.adventurer_s1.services.s1_trait_context_projection_service import TRAIT_CONTEXT_SELECTION_LOG_FIELD
    try:
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).populate_existing().first()
            raw = (log.full_log or {}).get(TRAIT_CONTEXT_SELECTION_LOG_FIELD) if log and isinstance(log.full_log, dict) else None
            raw = _clone_s1_generation_value(raw) if isinstance(raw, dict) else None
        return raw
    except Exception as exc:
        db_manager.rollback()
        current_app.logger.warning('S1 特质上下文冻结回执读取失败，后续将 fail-open log=%s: %s', expedition_log_id, exc)
        return None

def _persist_s1_trait_context_selection_receipt(expedition_log_id: int | None, receipt: dict | None, *, generation_claim_token: str | None=None, expected_receipt: dict | None=None) -> bool:
    """冻结选择及实际采用的摘要版本；写入失败时改用完整特质。"""
    if not expedition_log_id or not isinstance(receipt, dict):
        return False
    from plugins.adventurer_s1.services.s1_trait_context_projection_service import TRAIT_CONTEXT_SELECTION_LOG_FIELD
    try:
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).with_for_update().populate_existing().first()
            if not log:
                return False
            if generation_claim_token is not None and (log.status != 'generating' or not _owns_expedition_generation(log, generation_claim_token)):
                _reject_lost_expedition_generation_claim(generation_claim_token)
            merged = dict(log.full_log or {})
            existing = merged.get(TRAIT_CONTEXT_SELECTION_LOG_FIELD)
            if existing != expected_receipt:
                return existing == receipt
            merged[TRAIT_CONTEXT_SELECTION_LOG_FIELD] = _clone_s1_generation_value(receipt)
            log.full_log = merged
        return True
    except _S1ExpeditionGenerationClaimLost:
        raise
    except Exception as exc:
        db_manager.rollback()
        current_app.logger.warning('S1 特质上下文冻结回执写入失败，安全回退完整特质 log=%s: %s', expedition_log_id, exc)
        return False

def _load_s1_countermeasure_context_selection_receipt(expedition_log_id: int | None) -> tuple[dict | None, bool]:
    if not expedition_log_id:
        return (None, False)
    from plugins.adventurer_s1.services.s1_countermeasure_context_selection_ai_service import COUNTERMEASURE_CONTEXT_SELECTION_LOG_FIELD
    try:
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).populate_existing().first()
            full_log = log.full_log if log and isinstance(log.full_log, dict) else {}
            raw = full_log.get(COUNTERMEASURE_CONTEXT_SELECTION_LOG_FIELD)
            return (_clone_s1_generation_value(raw) if isinstance(raw, dict) else None, isinstance(full_log.get('generation_world_intel_injection'), dict))
    except Exception as exc:
        db_manager.rollback()
        current_app.logger.warning('S1 反制知识上下文冻结回执读取失败，后续保留公平轮转 log=%s: %s', expedition_log_id, exc)
        return (None, False)

def _persist_s1_countermeasure_context_selection_receipt(expedition_log_id: int | None, receipt: dict | None, *, generation_claim_token: str | None=None) -> bool:
    if not expedition_log_id or not isinstance(receipt, dict):
        return False
    from plugins.adventurer_s1.services.s1_countermeasure_context_selection_ai_service import COUNTERMEASURE_CONTEXT_SELECTION_LOG_FIELD
    try:
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).with_for_update().populate_existing().first()
            if not log:
                return False
            if generation_claim_token is not None and (log.status != 'generating' or not _owns_expedition_generation(log, generation_claim_token)):
                _reject_lost_expedition_generation_claim(generation_claim_token)
            merged = dict(log.full_log or {})
            existing = merged.get(COUNTERMEASURE_CONTEXT_SELECTION_LOG_FIELD)
            if isinstance(existing, dict):
                return existing == receipt
            merged[COUNTERMEASURE_CONTEXT_SELECTION_LOG_FIELD] = _clone_s1_generation_value(receipt)
            log.full_log = merged
        return True
    except _S1ExpeditionGenerationClaimLost:
        raise
    except Exception as exc:
        db_manager.rollback()
        current_app.logger.warning('S1 反制知识上下文冻结回执写入失败，后续保留公平轮转 log=%s: %s', expedition_log_id, exc)
        return False

def _resolve_s1_countermeasure_context_selection(members, item_dict: dict, *, target_dungeon: str, use_condition_by_name: dict[str, dict | None] | None, objective_outline: dict | None, party_id: int | None, expedition_log_id: int | None, generation_claim_token: str | None, physical_attempt_guard: Callable[[], None] | None) -> dict | None:
    """Freeze one semantic priority receipt; failures retain existing rotation."""
    if not expedition_log_id:
        return None
    from plugins.adventurer_s1.services.s1_countermeasure_context_selection_ai_service import build_countermeasure_context_selection_input, build_countermeasure_context_selection_receipt, call_countermeasure_context_selection_ai, validate_countermeasure_context_selection_receipt
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService
    objective_world_snapshot, _adoptions = S1WorldOutlineService.load_objective_world_intel_context(expedition_log_id)
    selection_input = build_countermeasure_context_selection_input(members, item_dict, target_dungeon=target_dungeon, use_condition_by_name=use_condition_by_name, objective_outline=objective_outline, objective_world_snapshot=objective_world_snapshot)
    if selection_input is None:
        return None
    frozen_receipt, generation_snapshot_frozen = _load_s1_countermeasure_context_selection_receipt(expedition_log_id)
    if frozen_receipt is not None:
        validated_receipt = validate_countermeasure_context_selection_receipt(frozen_receipt, selection_input=selection_input)
        if validated_receipt is not None:
            return validated_receipt
        current_app.logger.warning('S1 反制知识上下文冻结回执与当前冻结输入不一致，禁止重选并保留公平轮转 party=%s log=%s', party_id, expedition_log_id)
        return None
    if generation_snapshot_frozen:
        return None
    selection, error_code, metadata = call_countermeasure_context_selection_ai(selection_input, party_id=party_id, expedition_log_id=expedition_log_id, physical_attempt_guard=physical_attempt_guard, player_names=tuple((member.name for member in members if member.name)))
    receipt = build_countermeasure_context_selection_receipt(selection_input, selection, status='selected' if selection is not None else 'fallback', model=metadata.get('model') if isinstance(metadata, dict) else None, error_code=error_code)
    if not _persist_s1_countermeasure_context_selection_receipt(expedition_log_id, receipt, generation_claim_token=generation_claim_token):
        concurrent_receipt, _generation_snapshot_frozen = _load_s1_countermeasure_context_selection_receipt(expedition_log_id)
        return validate_countermeasure_context_selection_receipt(concurrent_receipt, selection_input=selection_input)
    return receipt

def _load_s1_recent_ecology_history_entries(target_dungeon: str, *, exclude_expedition_log_id: int | None=None, limit: int) -> list[dict]:
    """旧画像回归与离线诊断用有界历史；新生成链禁止调用。"""
    limit = max(0, min(32, _coerce_int(limit, 0)))
    dungeon = str(target_dungeon or '').strip()
    if not dungeon or limit <= 0:
        return []
    from sqlalchemy import and_, or_
    from plugins.adventurer_s1.services.s1_dungeon_ecology_identity_service import ECOLOGY_IDENTITY_OUTLINE_FIELD, ECOLOGY_IDENTITY_SNAPSHOT_LOG_FIELD, project_ecology_identity_snapshot
    query = AdventurerS1ExpeditionLog.query.filter(AdventurerS1ExpeditionLog.status == 'completed', or_(AdventurerS1ExpeditionLog.hidden_dungeon == dungeon, and_(AdventurerS1ExpeditionLog.hidden_dungeon.is_(None), AdventurerS1ExpeditionLog.target_dungeon == dungeon)))
    if exclude_expedition_log_id:
        query = query.filter(AdventurerS1ExpeditionLog.id != int(exclude_expedition_log_id))
    logs = query.order_by(AdventurerS1ExpeditionLog.id.desc()).limit(limit).all()
    entries: list[dict] = []
    for log in logs:
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        outline = full_log.get('objective_outline') if isinstance(full_log.get('objective_outline'), dict) else {}
        ecology = outline.get('resource_ecology') if isinstance(outline.get('resource_ecology'), dict) else None
        ecology_identity = project_ecology_identity_snapshot(full_log.get(ECOLOGY_IDENTITY_SNAPSHOT_LOG_FIELD) or outline.get(ECOLOGY_IDENTITY_OUTLINE_FIELD), expected_target_dungeon=dungeon)
        location_state_application = full_log.get('location_state_application')
        if not isinstance(location_state_application, list):
            location_state_application = []
        acquired_item_names: set[str] = set()
        for update in full_log.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            equipments = update.get('new_equipments')
            if isinstance(equipments, dict):
                acquired_item_names.update((str(name).strip() for name in equipments if str(name).strip()))
        entries.append({'expedition_log_id': int(log.id), 'summary': str(full_log.get('summary') or '')[:600], 'resource_ecology': ecology, 'ecology_identity': ecology_identity, 'acquired_item_names': sorted(acquired_item_names)[:20], 'location_state_application': list(location_state_application)[:4], 'dungeon_explorability_application': full_log.get('dungeon_explorability_application') if isinstance(full_log.get('dungeon_explorability_application'), dict) else None})
    return entries

def _load_or_create_s1_objective_mode_snapshot_strict(target_dungeon: str, *, expedition_log_id: int | None, authority_sources: list[str]) -> dict | None:
    """按日志冻结一次战斗存在要求，并兼容复用已冻结的 v1 主目标模式。"""
    feature_enabled = _coerce_bool(current_app.config.get('ADVENTURER_S1_COMBAT_PRESENCE_ENABLED', True), True)
    from plugins.adventurer_s1.services.s1_expedition_objective_mode_service import OBJECTIVE_MODE_OUTLINE_FIELD, OBJECTIVE_MODE_SNAPSHOT_LOG_FIELD, project_objective_mode_snapshot, select_combat_presence_snapshot
    dungeon = str(target_dungeon or '').strip()
    presence_weights = current_app.config.get('ADVENTURER_S1_COMBAT_PRESENCE_WEIGHTS')

    def select_snapshot() -> dict | None:
        if not feature_enabled:
            return None
        try:
            return select_combat_presence_snapshot(dungeon, expedition_log_id=expedition_log_id, authority_sources=authority_sources, presence_weights=presence_weights)
        except (TypeError, ValueError) as exc:
            current_app.logger.warning('S1 战斗存在要求不可用 log=%s dungeon=%s error=%s', expedition_log_id, dungeon, str(exc)[:300])
            return None
    if not expedition_log_id:
        return select_snapshot()
    with db_manager.transaction():
        locked_log = AdventurerS1ExpeditionLog.query.filter_by(id=int(expedition_log_id)).with_for_update().populate_existing().first()
        if not locked_log:
            return None
        locked_actual_dungeon = str(locked_log.hidden_dungeon or locked_log.target_dungeon or '').strip()
        if locked_actual_dungeon != dungeon:
            current_app.logger.warning('S1 战斗存在要求目标漂移，已按无要求继续 log=%s expected=%s actual=%s', expedition_log_id, dungeon, locked_actual_dungeon)
            return None
        merged = dict(locked_log.full_log or {})
        outline = merged.get('objective_outline')
        outline = outline if isinstance(outline, dict) else {}
        existing = project_objective_mode_snapshot(merged.get(OBJECTIVE_MODE_SNAPSHOT_LOG_FIELD), expected_target_dungeon=dungeon, expected_expedition_log_id=int(expedition_log_id))
        if not existing:
            existing = project_objective_mode_snapshot(outline.get(OBJECTIVE_MODE_OUTLINE_FIELD), expected_target_dungeon=dungeon, expected_expedition_log_id=int(expedition_log_id))
        if existing:
            return existing
        selected = select_snapshot()
        if selected:
            merged[OBJECTIVE_MODE_SNAPSHOT_LOG_FIELD] = selected
            locked_log.full_log = merged
        return selected

def _load_or_create_s1_objective_mode_snapshot(target_dungeon: str, *, expedition_log_id: int | None, authority_sources: list[str]) -> dict | None:
    """可选战斗分布机制失败时放行原剧情链，不触发大纲或任务重试。"""
    try:
        return _load_or_create_s1_objective_mode_snapshot_strict(target_dungeon, expedition_log_id=expedition_log_id, authority_sources=authority_sources)
    except Exception:
        try:
            db_manager.rollback()
        except Exception:
            current_app.logger.exception('S1 战斗存在要求失败后的 session 回滚失败 log=%s dungeon=%s', expedition_log_id, str(target_dungeon or '').strip())
        current_app.logger.exception('S1 战斗存在要求冻结失败，已按无要求继续 log=%s dungeon=%s', expedition_log_id, str(target_dungeon or '').strip())
        return None
def _should_retry_s1_objective_outline_transport_exception(exc: BaseException) -> bool:
    if isinstance(exc, json.JSONDecodeError):
        return True
    return is_s1_ai_retryable_request_exception(exc)
_S1_OBJECTIVE_MODE_SNAPSHOT_UNSET = object()
def _attach_s1_objective_outline_authority(outline: dict, objective_mode_snapshot: dict | None, world_intel_items: list[dict]) -> dict:
    """Attach server-owned authority after parsing, including checkpoint replays."""
    if objective_mode_snapshot:
        from plugins.adventurer_s1.services.s1_expedition_objective_mode_service import OBJECTIVE_MODE_OUTLINE_FIELD
        outline[OBJECTIVE_MODE_OUTLINE_FIELD] = json.loads(json.dumps(objective_mode_snapshot, ensure_ascii=False))
    outline['required_faction_interaction_thread_ids'] = sorted({int(item.get('id') or item.get('world_story_thread_id') or 0) for item in world_intel_items if item.get('required_faction_interaction') is True and int(item.get('id') or item.get('world_story_thread_id') or 0) > 0})
    outline['required_campaign_manifestation_thread_ids'] = sorted({int(item.get('id') or item.get('world_story_thread_id') or 0) for item in world_intel_items if item.get('required_campaign_manifestation') is True and int(item.get('id') or item.get('world_story_thread_id') or 0) > 0})[:1]
    outline['required_faction_presence_thread_ids'] = sorted({int(item.get('id') or item.get('world_story_thread_id') or 0) for item in world_intel_items if item.get('required_faction_presence') is True and int(item.get('id') or item.get('world_story_thread_id') or 0) > 0})[:1]
    return outline
def _call_s1_objective_outline_ai(members, target_dungeon: str, level_desc: str, special_event_mode: str, *, special_event_type: str | None=None, special_event_risk_tier: str | None=None, has_special_event: bool=False, hidden_boss_context: dict | None=None, commission_context: list | None=None, special_dungeon_context: dict | None=None, party_id: int | None=None, expedition_log_id: int | None=None, hidden_dungeon_context: dict | None=None, name_alias: dict[int, str] | None=None, objective_mode_snapshot: object=_S1_OBJECTIVE_MODE_SNAPSHOT_UNSET, physical_attempt_guard: Callable[[], None] | None=None, generation_claim_token: str | None=None, item_dict: dict | None=None, use_condition_by_name: dict | None=None) -> dict | None:
    """生成客观大纲；失败返回 None，由调用方终止本次探险生成。"""
    if not _should_build_s1_objective_outline():
        return None
    from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
    from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService
    try:
        outline_route = resolve_s1_ai_profile_snapshot('expedition_outline')
    except AIProfileAssignmentError as exc:
        current_app.logger.error('S1 客观局势草案路由不可用，终止本次探险生成 party=%s log=%s error=%s', party_id, expedition_log_id, str(exc)[:500])
        return None
    api_key = outline_route.api_key
    api_base = outline_route.api_base
    model = outline_route.model
    if not api_key or not api_base or (not model):
        current_app.logger.error('S1 客观局势草案路由配置不完整，终止本次探险生成 party=%s log=%s profile=%s', party_id, expedition_log_id, outline_route.profile_id)
        return None
    timeout_floor = _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_AI_TIMEOUT_FLOOR_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS)
    timeout = max(S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS, timeout_floor, _coerce_int(current_app.config.get('ADVENTURER_S1_AI_DEFAULT_TIMEOUT_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_OBJECTIVE_OUTLINE_AI_TIMEOUT_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS))
    transport_max_attempts = max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_OBJECTIVE_OUTLINE_TRANSPORT_MAX_ATTEMPTS', 2), 2))
    dm_handoff_block = S1WorldOutlineService.build_dm_handoff_prompt_block(target_dungeon, party_id=party_id, expedition_log_id=expedition_log_id)
    from plugins.adventurer_s1.services.s1_expedition_locus_service import prepare_expedition_location_snapshot
    expedition_locus, location_snapshot = prepare_expedition_location_snapshot(expedition_log_id, target_dungeon)
    location_block = location_snapshot.get('prompt_block') or ''
    world_story_context = '\n'.join([str(level_desc or ''), str(special_event_mode or ''), json.dumps(hidden_boss_context or {}, ensure_ascii=False), json.dumps(commission_context or [], ensure_ascii=False)])
    world_story_snapshot = S1WorldOutlineService.build_world_story_threads_injection_snapshot(target_dungeon, party_id=party_id, expedition_log_id=expedition_log_id, context_text=world_story_context, authority_reference_texts=[dm_handoff_block, location_block], expedition_locus_scope_key=expedition_locus.get('scope_key'))
    world_story_block = world_story_snapshot.get('prompt_block') or ''
    world_intel_items = [dict(item) for item in world_story_snapshot.get('items') or [] if isinstance(item, dict)]
    world_actor_states = [dict(item) for item in world_story_snapshot.get('world_actor_states') or [] if isinstance(item, dict)]
    expected_faction_keys = [str(value).strip() for value in world_story_snapshot.get('faction_keys') or [] if isinstance(value, str) and value.strip()]
    expected_special_event_kind = resolve_s1_special_event_kind({'has_special_event': has_special_event, 'special_event_type': special_event_type, 'special_event_risk_tier': special_event_risk_tier}) if has_special_event else 'normal'
    temporary_location_allowed = not campaign_world(target_dungeon) and (not any((hidden_boss_context, hidden_dungeon_context, special_dungeon_context, commission_context)))
    S1WorldOutlineService.persist_world_intel_injection_snapshot(expedition_log_id, 'objective_world_intel_injection', world_story_snapshot)
    if world_story_block:
        dm_handoff_block = f'{dm_handoff_block}\n\n{world_story_block}' if dm_handoff_block else world_story_block
    if location_block:
        dm_handoff_block = f'{dm_handoff_block}\n\n{location_block}' if dm_handoff_block else location_block
    if objective_mode_snapshot is _S1_OBJECTIVE_MODE_SNAPSHOT_UNSET:
        from plugins.adventurer_s1.services.s1_expedition_objective_mode_service import build_objective_mode_authority_sources
        objective_mode_snapshot = _load_or_create_s1_objective_mode_snapshot(target_dungeon, expedition_log_id=expedition_log_id, authority_sources=build_objective_mode_authority_sources(commission_context=commission_context, hidden_boss_context=hidden_boss_context))
    else:
        from plugins.adventurer_s1.services.s1_expedition_objective_mode_service import project_objective_mode_snapshot
        objective_mode_snapshot = project_objective_mode_snapshot(objective_mode_snapshot, expected_target_dungeon=target_dungeon, expected_expedition_log_id=expedition_log_id)
    from plugins.adventurer_s1.services.s1_item_use_condition_service import build_expedition_equipment_context
    from plugins.adventurer_s1.services.s1_capability_profile_contract import freeze_encounter_definition
    if item_dict is None or use_condition_by_name is None:
        frozen_items, frozen_conditions = build_expedition_equipment_context(members)
        if item_dict is None:
            item_dict = frozen_items
        if use_condition_by_name is None:
            use_condition_by_name = frozen_conditions
    outline_members = refresh_frozen_member_description_ai_context(members)
    outline_description_projection = build_frozen_member_description_projection_receipt(outline_members, item_dict=item_dict, target_dungeon=target_dungeon, use_condition_by_name=use_condition_by_name)
    system_prompt, user_prompt = build_s1_expedition_outline_prompts(target_dungeon=target_dungeon, temporary_location_allowed=temporary_location_allowed, dungeon_level_desc=level_desc, special_event_mode=_build_s1_objective_outline_special_event_mode(special_event_type=special_event_type, special_event_risk_tier=special_event_risk_tier, has_special_event=has_special_event, hidden_boss_context=hidden_boss_context), hidden_boss_context=hidden_boss_context, commission_context=commission_context, special_dungeon_context=special_dungeon_context, dm_handoff_block=dm_handoff_block, has_world_actor_authority=any((type(item.get('id')) is int and item['id'] > 0 for item in world_actor_states)), party_profile=_build_s1_outline_party_profile(members, target_dungeon=target_dungeon, special_event_kind=expected_special_event_kind, has_hidden_boss=bool(hidden_boss_context)), party_capabilities=_build_s1_outline_member_capabilities(outline_members, item_dict=item_dict, target_dungeon=target_dungeon, use_condition_by_name=use_condition_by_name), special_event_type=expected_special_event_kind, objective_mode_snapshot=objective_mode_snapshot, expedition_log_id=expedition_log_id)
    user_prompt = anonymize_s1_prompt_member_names(user_prompt, members, name_alias)
    outline_codename_to_real = build_codename_to_real_from_alias(name_alias, {member.id: member.name for member in members or [] if getattr(member, 'name', None)})
    try:
        db_manager.remove()
        headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
        call_attempt_number = 0

        def call_outline_generation(validation_feedback: str | None=None) -> tuple[S1AiJsonResult, dict | None, dict, int]:
            nonlocal call_attempt_number
            call_attempt_number += 1
            current_attempt_number = call_attempt_number
            request_user_prompt = user_prompt
            if validation_feedback:
                request_user_prompt = f'{user_prompt}\n\n{validation_feedback}'
            request_user_prompt = anonymize_s1_prompt_member_names(request_user_prompt, members, name_alias)
            usage = None
            metadata: dict = {}
            resp = None

            def validate_outline_response(decoded) -> S1AiResponseContractResult:
                generated_candidate = _restore_codenames_in_ai_json_result(decoded.json_result, outline_codename_to_real)
                parsed_candidate, reject_meta = _parse_s1_objective_outline_with_meta(generated_candidate, expected_target_dungeon=target_dungeon, expected_special_event_kind=expected_special_event_kind, expected_world_intel_items=world_intel_items, expected_world_actor_states=world_actor_states, expected_world_identity_guards=world_story_snapshot.get('world_identity_guards'), expected_faction_keys=expected_faction_keys, temporary_location_allowed=temporary_location_allowed)
                if parsed_candidate is not None:
                    return S1AiResponseContractResult.accept()
                reject_reason = reject_meta.get('reason') if isinstance(reject_meta, dict) else None
                return S1AiResponseContractResult.reject(reject_reason or generated_candidate.error_code or 'objective_outline_invalid', diagnostics=reject_meta, failure_stage='response_parse' if generated_candidate.error_code else 'contract_validation')
            try:
                resp = post_s1_ai_request(f'{api_base}/chat/completions', feature='expedition_outline', resolved_profile=outline_route, physical_attempt_guard=physical_attempt_guard, headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': request_user_prompt}], 'temperature': 0.2, 'response_format': {'type': 'json_object'}}, timeout=timeout, request_metadata_out=metadata, response_validator=validate_outline_response)
                decoded = parse_s1_ai_http_json_response(resp)
                resp_json = decoded.http_result.response_json or {}
                usage = decoded.http_result.usage
                enrich_s1_ai_response_metadata(resp, resp_json)
                generated_result = _restore_codenames_in_ai_json_result(decoded.json_result, outline_codename_to_real)
                recovery_mode = decoded.http_result.recovery_mode
                if recovery_mode in {'repaired_envelope', 'direct_body_candidate'}:
                    recovered_outline, _ = _parse_s1_objective_outline_with_meta(generated_result, expected_target_dungeon=target_dungeon, expected_special_event_kind=expected_special_event_kind, expected_world_intel_items=world_intel_items, expected_world_actor_states=world_actor_states, expected_world_identity_guards=world_story_snapshot.get('world_identity_guards'), expected_faction_keys=expected_faction_keys, temporary_location_allowed=temporary_location_allowed)
                    if recovered_outline is None:
                        raise json.JSONDecodeError('nonstandard_response_contract_invalid', decoded.http_result.content, 0)
                    current_app.logger.info('S1 AI 非标准响应已本地恢复 stage=objective_outline_http_body_json_repair party=%s log=%s attempt=%s action=accepted recovery_mode=%s repair_applied=%s', party_id, expedition_log_id, current_attempt_number, recovery_mode, bool(generated_result.repair_applied))
                metadata.update(get_s1_ai_response_metadata(resp))
            except Exception as exc:
                failed_response_excerpt = build_s1_ai_raw_excerpt('exception={}: {}'.format(type(exc).__name__, str(exc)))
                if resp is not None:
                    try:
                        failed_response_excerpt = build_s1_ai_raw_excerpt('exception={}: {}; http_status={}; body={}'.format(type(exc).__name__, str(exc), getattr(resp, 'status_code', None), getattr(resp, 'text', '') or ''))
                    except Exception as diagnostic_exc:
                        failed_response_excerpt = 'response_diagnostic_unavailable={}'.format(type(diagnostic_exc).__name__)
                record_token_usage(call_type='expedition_objective_outline_ai', usage=usage, model=metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=current_attempt_number, is_success=False, failure_stage='http_or_response', validation_error_code=f'http_or_response.{type(exc).__name__}', json_object_mode=_coerce_bool(metadata.get('json_object_mode'), None), provider_request_id=metadata.get('request_id'), failed_response_excerpt=failed_response_excerpt, record_without_usage=True, request_metadata=metadata, request_exception=exc)
                raise
            log_s1_ai_response_outcome(resp, feature='expedition_outline', result_status='json_usable' if generated_result.ok else 'unusable', finish_reason=generated_result.finish_reason, error_code=generated_result.error_code, attempt_number=current_attempt_number, expedition_log_id=expedition_log_id)
            return (generated_result, usage, metadata, current_attempt_number)

        def call_outline_generation_with_transport_retry(validation_feedback: str | None=None) -> tuple[S1AiJsonResult, dict | None, dict, int]:
            for transport_attempt in range(1, transport_max_attempts + 1):
                try:
                    return call_outline_generation(validation_feedback)
                except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
                    if not _should_retry_s1_objective_outline_transport_exception(exc):
                        raise
                    if transport_attempt >= transport_max_attempts:
                        raise
                    current_app.logger.warning('S1 AI 响应异常 stage=objective_outline party=%s log=%s transport_attempt=%s max_attempts=%s action=retry_same_stage_transport error=%s', party_id, expedition_log_id, transport_attempt, transport_max_attempts, type(exc).__name__)
            raise RuntimeError('objective outline transport retry exhausted')

        def parse_and_repair_once(generated_call: tuple[S1AiJsonResult, dict | None, dict, int]) -> tuple[dict | None, dict | None]:
            nonlocal call_attempt_number
            generated_result, generation_usage, generation_metadata, generation_attempt_number = generated_call
            parsed_outline = None
            parsed_reject_meta = None
            try:
                parsed_outline, parsed_reject_meta = _parse_s1_objective_outline_with_meta(generated_result, expected_target_dungeon=target_dungeon, expected_special_event_kind=expected_special_event_kind, expected_world_intel_items=world_intel_items, expected_world_actor_states=world_actor_states, expected_world_identity_guards=world_story_snapshot.get('world_identity_guards'), expected_faction_keys=expected_faction_keys, temporary_location_allowed=temporary_location_allowed)
            finally:
                reject_reason = parsed_reject_meta.get('reason') if isinstance(parsed_reject_meta, dict) else None
                generation_succeeded = parsed_outline is not None
                record_token_usage(call_type='expedition_objective_outline_ai', usage=generation_usage, model=generation_metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=generation_attempt_number, is_success=generation_succeeded, failure_stage=None if generation_succeeded else 'response_parse' if generated_result.error_code else 'contract_validation', validation_error_code=None if generation_succeeded else reject_reason or generated_result.error_code or 'objective_outline_invalid', finish_reason=generated_result.finish_reason, json_object_mode=_coerce_bool(generation_metadata.get('json_object_mode'), None), provider_request_id=generation_metadata.get('request_id'), repair_applied=bool(generated_result.repair_applied), failed_response_excerpt=None if generation_succeeded else generated_result.raw_excerpt, record_without_usage=not bool(generation_usage))
            reject_reason = parsed_reject_meta.get('reason') if parsed_reject_meta else None
            missing_fields = set(parsed_reject_meta.get('missing_fields') or []) if parsed_reject_meta else set()
            can_repair_missing_fields = bool(missing_fields) and missing_fields.issubset(_S1_OBJECTIVE_OUTLINE_LIST_FIELDS)
            can_repair = reject_reason in {'invalid_json', 'repair_failed'} or (reject_reason == 'missing_required_fields' and can_repair_missing_fields)
            if parsed_outline is not None or not can_repair:
                if parsed_outline is not None and parsed_reject_meta and any((item.get('issue') == 'json_repair_applied' for item in parsed_reject_meta.get('non_fatal_warnings') or [] if isinstance(item, dict))):
                    current_app.logger.info('S1 AI 输出结构已本地修复 stage=objective_outline_json_repair party=%s log=%s attempt=%s action=accepted', party_id, expedition_log_id, call_attempt_number)
                return (parsed_outline, parsed_reject_meta)
            try:
                repair_request_metadata: dict[str, str] = {}
                call_attempt_number += 1
                repair_attempt_number = call_attempt_number
                repaired_content, repair_usage = _repair_s1_objective_outline_structure(generated_result.content, api_base=api_base, headers=headers, model=model, timeout=timeout, validation_reason=f"{reject_reason}:{','.join(sorted(missing_fields))}" if missing_fields else reject_reason, request_metadata_out=repair_request_metadata, physical_attempt_guard=physical_attempt_guard)
                canonical_repaired_content = restore_names_in_narrative(repaired_content, outline_codename_to_real)
                canonical_repaired_result = replace(parse_s1_ai_json_content(canonical_repaired_content), evidence=get_s1_ai_failure_evidence(repair_usage))
                parsed_outline, parsed_reject_meta = _parse_s1_objective_outline_with_meta(canonical_repaired_result, expected_target_dungeon=target_dungeon, expected_special_event_kind=expected_special_event_kind, expected_world_intel_items=world_intel_items, expected_world_actor_states=world_actor_states, expected_world_identity_guards=world_story_snapshot.get('world_identity_guards'), expected_faction_keys=expected_faction_keys, temporary_location_allowed=temporary_location_allowed)
                record_token_usage(call_type='expedition_objective_outline_ai', usage=repair_usage, model=repair_request_metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=repair_attempt_number, is_success=parsed_outline is not None, failure_stage=None if parsed_outline is not None else 'contract_validation', validation_error_code=None if parsed_outline is not None else parsed_reject_meta.get('reason') if isinstance(parsed_reject_meta, dict) else 'objective_outline_structure_repair_invalid', json_object_mode=_coerce_bool(repair_request_metadata.get('json_object_mode'), None), provider_request_id=repair_request_metadata.get('request_id'), repair_applied=True, failed_response_excerpt=None if parsed_outline is not None else repaired_content, record_without_usage=not bool(repair_usage))
            except _S1ExpeditionGenerationClaimLost:
                raise
            except Exception as repair_exc:
                record_token_usage(call_type='expedition_objective_outline_ai', usage=None, model=repair_request_metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=repair_attempt_number, is_success=False, failure_stage='http_or_response', validation_error_code=f'objective_outline_structure_repair.{type(repair_exc).__name__}', repair_applied=True, record_without_usage=True, request_metadata=repair_request_metadata, request_exception=repair_exc)
                current_app.logger.warning('S1 AI 输出失败 stage=objective_outline_structure_repair party=%s log=%s attempt=%s error=%s detail=%s', party_id, expedition_log_id, call_attempt_number, type(repair_exc).__name__, str(repair_exc)[:500])
            return (parsed_outline, parsed_reject_meta)
        generated_call = call_outline_generation_with_transport_retry()
        outline, reject_meta = parse_and_repair_once(generated_call)
        if outline is None:
            retry_feedback = _build_s1_objective_outline_retry_feedback(reject_meta)
            current_app.logger.warning('S1 AI 输出契约失败 stage=objective_outline party=%s log=%s attempt=%s action=retry_same_stage_contract error=%s', party_id, expedition_log_id, call_attempt_number, _format_s1_objective_outline_reject_meta(reject_meta))
            generated_call = call_outline_generation_with_transport_retry(retry_feedback)
            outline, reject_meta = parse_and_repair_once(generated_call)
        if isinstance(outline, dict):
            _attach_s1_objective_outline_authority(outline, objective_mode_snapshot, world_intel_items)
            freeze_encounter_definition(outline, member_description_projection=outline_description_projection)
        if outline is None:
            current_app.logger.warning('S1 AI 输出契约失败 stage=objective_outline party=%s log=%s attempt=%s action=fail_generation error=%s', party_id, expedition_log_id, call_attempt_number, _format_s1_objective_outline_reject_meta(reject_meta))
        adoptions = outline.get(_S1_WORLD_INTEL_USAGE_FIELD) or [] if isinstance(outline, dict) else []
        S1WorldOutlineService.persist_world_intel_adoptions(expedition_log_id, adoptions)
        direct_thread_ids = {int(item['world_story_thread_id']) for item in adoptions if isinstance(item, dict) and item.get('usage_mode') == 'exclusive_direct_use' and (type(item.get('world_story_thread_id')) is int)}
        from plugins.adventurer_s1.services.s1_narrative_lease_service import release_unadopted_world_story_leases
        with db_manager.transaction():
            release_unadopted_world_story_leases(expedition_log_id, adopted_world_story_thread_ids=direct_thread_ids)
        if outline is not None and reject_meta:
            _persist_objective_outline_to_log(expedition_log_id, outline, reject_meta, generation_claim_token=generation_claim_token)
        elif reject_meta:
            _persist_objective_outline_to_log(expedition_log_id, None, reject_meta, generation_claim_token=generation_claim_token)
        return outline
    except _S1ExpeditionGenerationClaimLost:
        raise
    except Exception as exc:
        current_app.logger.warning('S1 AI 调用失败 stage=objective_outline party=%s log=%s error=%s detail=%s action=fail_generation', party_id, expedition_log_id, type(exc).__name__, str(exc)[:500])
        try:
            S1WorldOutlineService.persist_world_intel_adoptions(expedition_log_id, [])
            from plugins.adventurer_s1.services.s1_narrative_lease_service import release_unadopted_world_story_leases
            with db_manager.transaction():
                release_unadopted_world_story_leases(expedition_log_id)
        except Exception:
            current_app.logger.warning('S1 世界情报采纳失败后的租约释放异常 log=%s', expedition_log_id, exc_info=True)
        return None
def _s1_event_resolution_index_fields(payload: dict) -> list[tuple[dict, str]]:
    """返回主剧情因果底稿中全部已填写的事件索引字段。"""
    fields: list[tuple[dict, str]] = []
    for source in payload.get('established_sources') or []:
        if isinstance(source, dict) and type(source.get('event_fact_index')) is int:
            fields.append((source, 'event_fact_index'))
    for source in payload.get('mitigation_ledger') or []:
        if isinstance(source, dict) and type(source.get('established_event_fact_index')) is int:
            fields.append((source, 'established_event_fact_index'))
    for row in payload.get('member_impacts') or []:
        if not isinstance(row, dict):
            continue
        if type(row.get('direct_terminal_event_fact_index')) is int:
            fields.append((row, 'direct_terminal_event_fact_index'))
        for change in row.get('changes') or []:
            if not isinstance(change, dict):
                continue
            if type(change.get('event_fact_index')) is int:
                fields.append((change, 'event_fact_index'))
            for source in change.get('mitigation_sources') or []:
                if isinstance(source, dict) and type(source.get('established_event_fact_index')) is int:
                    fields.append((source, 'established_event_fact_index'))
    for row in payload.get('member_outcomes') or []:
        hp_trace = row.get('hp_trace') if isinstance(row, dict) else None
        for change in hp_trace.get('changes') or [] if isinstance(hp_trace, dict) else []:
            if not isinstance(change, dict):
                continue
            if type(change.get('event_fact_index')) is int:
                fields.append((change, 'event_fact_index'))
            for source in change.get('mitigation_sources') or []:
                if isinstance(source, dict) and type(source.get('established_event_fact_index')) is int:
                    fields.append((source, 'established_event_fact_index'))
    return fields
def _canonicalize_s1_expedition_event_resolution_payload(data: object) -> object:
    """投影当前作用事实契约；旧 HP 响应只迁移其非派生事实。"""
    if not isinstance(data, dict):
        return data
    payload = json.loads(json.dumps(data, ensure_ascii=False))
    raw_event_facts = payload.get('event_facts')
    if isinstance(raw_event_facts, list) and raw_event_facts:
        wrapped_facts: list[str] = []
        explicit_indexes: list[int] = []
        all_wrapped = True
        all_indexed = True
        for raw_fact in raw_event_facts:
            if not isinstance(raw_fact, dict):
                all_wrapped = False
                break
            fact = raw_fact.get('fact')
            if not isinstance(fact, str):
                all_wrapped = False
                break
            wrapped_facts.append(fact)
            raw_index = raw_fact.get('event_fact_index', raw_fact.get('index'))
            if type(raw_index) is int:
                explicit_indexes.append(raw_index)
            else:
                all_indexed = False
        if all_wrapped and (not all_indexed or explicit_indexes == list(range(len(wrapped_facts))) or explicit_indexes == list(range(1, len(wrapped_facts) + 1))):
            payload['event_facts'] = wrapped_facts
            if all_indexed:
                index_map = {source_index: event_fact_index for event_fact_index, source_index in enumerate(explicit_indexes)}
                indexed_fields = _s1_event_resolution_index_fields(payload)
                if all((holder[field] in index_map for holder, field in indexed_fields)):
                    for holder, field in indexed_fields:
                        holder[field] = index_map[holder[field]]
    legacy_outcomes = payload.get('member_outcomes')
    is_legacy_payload = not isinstance(payload.get('member_impacts'), list) and isinstance(legacy_outcomes, list)
    if 'established_sources' not in payload:
        payload['established_sources'] = []
    payload.setdefault('resolution_contract_version', 2)
    if not isinstance(payload.get('mitigation_ledger'), list):
        payload['mitigation_ledger'] = []
    if is_legacy_payload:
        if isinstance(legacy_outcomes, list):
            member_impacts: list[object] = []
            for raw_row in legacy_outcomes:
                if not isinstance(raw_row, dict):
                    member_impacts.append(raw_row)
                    continue
                hp_trace = raw_row.get('hp_trace')
                hp_trace = hp_trace if isinstance(hp_trace, dict) else {}
                life_state_basis = hp_trace.get('life_state_basis')
                direct_terminal_state = life_state_basis if life_state_basis in {'durability_bypass', 'unable_to_return'} else 'none'
                direct_terminal_index = raw_row.get('direct_terminal_event_fact_index', hp_trace.get('direct_terminal_event_fact_index'))
                changes: list[object] = []
                for raw_change in hp_trace.get('changes') or []:
                    if not isinstance(raw_change, dict):
                        changes.append(raw_change)
                        continue
                    change = dict(raw_change)
                    change.pop('damage_taken', None)
                    if change.get('mitigation_sources') is None and change.get('mitigation') == 0:
                        change['mitigation_sources'] = []
                    changes.append(change)
                member_impacts.append({'id': raw_row.get('id'), 'changes': changes, 'direct_terminal_state': direct_terminal_state, 'direct_terminal_event_fact_index': direct_terminal_index})
            payload['member_impacts'] = member_impacts
    for row in payload.get('member_impacts') or []:
        if not isinstance(row, dict):
            continue
        if row.get('direct_terminal_state') == 'none' and 'direct_terminal_event_fact_index' not in row:
            row['direct_terminal_event_fact_index'] = None
        changes = row.get('changes')
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            if 'healing_received' not in change and type(change.get('raw_damage')) is int and (type(change.get('mitigation')) is int) and (change['raw_damage'] > 0 or change['mitigation'] > 0):
                change['healing_received'] = 0
            if change.get('mitigation') == 0 and 'mitigation_sources' not in change:
                change['mitigation_sources'] = []
            change.pop('damage_taken', None)
        row['changes'] = [change for change in changes if not (isinstance(change, dict) and all((type(change.get(field)) is int and change[field] == 0 for field in ('raw_damage', 'mitigation', 'healing_received'))) and (change.get('mitigation_sources') in (None, [])))]
    ledger_rows = payload['mitigation_ledger']
    for ledger_row in ledger_rows:
        if isinstance(ledger_row, dict) and type(ledger_row.get('ledger_id')) is int:
            ledger_row['ledger_id'] = str(ledger_row['ledger_id'])
    used_ledger_ids = {row.get('ledger_id') for row in ledger_rows if isinstance(row, dict) and isinstance(row.get('ledger_id'), str)}
    next_ledger_ordinal = 1

    def allocate_legacy_ledger_id() -> str:
        nonlocal next_ledger_ordinal
        while f'L{next_ledger_ordinal}' in used_ledger_ids:
            next_ledger_ordinal += 1
        ledger_id = f'L{next_ledger_ordinal}'
        used_ledger_ids.add(ledger_id)
        next_ledger_ordinal += 1
        return ledger_id
    for row in payload.get('member_impacts') or []:
        if not isinstance(row, dict):
            continue
        for change in row.get('changes') or []:
            if not isinstance(change, dict):
                continue
            migrated_allocations: list[object] = []
            allocation_index_by_ledger_id: dict[str, int] = {}
            for source in change.get('mitigation_sources') or []:
                if not isinstance(source, dict):
                    migrated_allocations.append(source)
                    continue
                ledger_id = source.get('ledger_id')
                if type(ledger_id) is int:
                    ledger_id = str(ledger_id)
                mitigation = source.get('mitigation')
                if type(mitigation) is int and mitigation == 0:
                    continue
                if isinstance(ledger_id, str) and ledger_id:
                    existing_index = allocation_index_by_ledger_id.get(ledger_id)
                    if existing_index is not None and type(mitigation) is int and (mitigation > 0):
                        existing = migrated_allocations[existing_index]
                        if isinstance(existing, dict) and type(existing.get('mitigation')) is int and (existing['mitigation'] > 0):
                            existing['mitigation'] += mitigation
                            continue
                    migrated_allocations.append({'ledger_id': ledger_id, 'mitigation': mitigation})
                    if type(mitigation) is int and mitigation > 0:
                        allocation_index_by_ledger_id[ledger_id] = len(migrated_allocations) - 1
                    continue
                if type(mitigation) is not int or mitigation <= 0:
                    migrated_allocations.append(source)
                    continue
                ledger_id = allocate_legacy_ledger_id()
                ledger_rows.append({key: value for key, value in {'ledger_id': ledger_id, 'source_ref_id': source.get('source_ref_id'), 'source_owner_id': source.get('source_owner_id'), 'source_kind': source.get('source_kind'), 'source_name': source.get('source_name'), 'source_origin': source.get('source_origin'), 'established_event_fact_index': source.get('established_event_fact_index'), 'capacity': mitigation, 'effect_boundary': source.get('effect_boundary')}.items() if value is not None or key == 'established_event_fact_index'})
                migrated_allocations.append({'ledger_id': ledger_id, 'mitigation': mitigation})
            change['mitigation_sources'] = migrated_allocations
    return payload
def _normalize_s1_event_resolution_positional_member_ids(data: object, member_ids: list[int]) -> object:
    """将完整的 1..N 位置编号机械映射为冻结成员 ID；歧义输入保持原样。"""
    if not isinstance(data, dict):
        return data
    expected_ids = [member_id for member_id in member_ids if type(member_id) is int and member_id > 0]
    if not expected_ids or len(expected_ids) != len(member_ids):
        return data
    if len(expected_ids) != len(set(expected_ids)):
        return data
    ordinal_ids = set(range(1, len(expected_ids) + 1))
    expected_id_set = set(expected_ids)
    if ordinal_ids.intersection(expected_id_set):
        return data
    row_key = 'member_impacts' if isinstance(data.get('member_impacts'), list) else 'member_outcomes'
    raw_rows = data.get(row_key)
    if not isinstance(raw_rows, list) or len(raw_rows) != len(expected_ids):
        return data
    actual_ids = [row.get('id') if isinstance(row, dict) else None for row in raw_rows]
    if any((type(member_id) is not int for member_id in actual_ids)) or len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != ordinal_ids:
        return data
    source_owner_ids: list[int] = []
    source_groups: list[object] = [data.get('established_sources') or [], data.get('mitigation_ledger') or []]
    for row in raw_rows:
        if not isinstance(row, dict):
            return data
        raw_changes = row.get('changes') or [] if row_key == 'member_impacts' else (row.get('hp_trace') or {}).get('changes') or []
        for change in raw_changes:
            if isinstance(change, dict):
                source_groups.append(change.get('mitigation_sources') or [])
    for raw_sources in source_groups:
        if not isinstance(raw_sources, list):
            return data
        for source in raw_sources:
            if not isinstance(source, dict):
                return data
            source_owner_id = source.get('source_owner_id')
            if source_owner_id is None and isinstance(source.get('ledger_id'), str) and source['ledger_id']:
                continue
            if source_owner_id is None and isinstance(source.get('source_ref_id'), str) and source['source_ref_id']:
                continue
            if type(source_owner_id) is not int:
                return data
            source_owner_ids.append(source_owner_id)
    source_owner_set = set(source_owner_ids)
    source_ids_are_ordinals = source_owner_set.issubset(ordinal_ids)
    source_ids_are_stable = source_owner_set.issubset(expected_id_set)
    if source_owner_set and source_ids_are_ordinals == source_ids_are_stable:
        return data
    normalized = deepcopy(data)
    ordinal_to_member_id = {ordinal: expected_ids[ordinal - 1] for ordinal in ordinal_ids}
    for row in normalized[row_key]:
        row['id'] = ordinal_to_member_id[row['id']]
    if source_ids_are_ordinals:
        for source in normalized.get('established_sources') or []:
            source['source_owner_id'] = ordinal_to_member_id[source['source_owner_id']]
        for source in normalized.get('mitigation_ledger') or []:
            source_owner_id = source.get('source_owner_id')
            if type(source_owner_id) is int:
                source['source_owner_id'] = ordinal_to_member_id[source_owner_id]
        for row in normalized[row_key]:
            raw_changes = row.get('changes') or [] if row_key == 'member_impacts' else row.get('hp_trace', {}).get('changes') or []
            for change in raw_changes:
                for source in change.get('mitigation_sources') or []:
                    source_owner_id = source.get('source_owner_id')
                    if type(source_owner_id) is int:
                        source['source_owner_id'] = ordinal_to_member_id[source_owner_id]
    return normalized
def _validate_s1_canonical_event_resolution(data: object, member_ids: list[int], member_max_hp_by_id: dict[int, int] | None=None, member_current_hp_by_id: dict[int, int] | None=None, member_source_names_by_id: dict[int, dict[str, set[str]]] | None=None, allowed_pressure_indices: set[int] | None=None) -> tuple[dict | None, str | None]:
    """校验主剧情因果底稿经后端派生的规范生命结果。"""
    try:
        resolution = _S1ExpeditionEventResolutionModel.model_validate(data).model_dump()
    except ValidationError as exc:
        return (None, f'invalid_resolution_schema: {exc.errors(include_url=False)[:5]}')
    expected_ids = [int(member_id) for member_id in member_ids]
    actual_ids = [row['id'] for row in resolution['member_outcomes']]
    if len(actual_ids) != len(set(actual_ids)):
        return (None, 'duplicate_member_outcome_id')
    if set(actual_ids) != set(expected_ids):
        return (None, f'member_outcome_ids_mismatch expected={sorted(expected_ids)} actual={sorted(actual_ids)}')
    if any((not fact.strip() for fact in resolution['event_facts'])):
        return (None, 'empty_event_fact')
    pressure_indices = set(allowed_pressure_indices or set())
    ledger_capacity_by_id: dict[str, int] = {}
    ledger_usage_totals: dict[str, int] = {}
    for ledger_row in resolution['mitigation_ledger']:
        ledger_id = ledger_row['ledger_id'].strip()
        if not ledger_id:
            return (None, 'empty_mitigation_ledger_id')
        if ledger_id in ledger_capacity_by_id:
            return (None, f'duplicate_mitigation_ledger_id ledger_id={ledger_id}')
        ledger_row['ledger_id'] = ledger_id
        ledger_row['effect_boundary'] = ledger_row['effect_boundary'].strip()
        if not ledger_row['effect_boundary']:
            return (None, f'empty_mitigation_ledger_boundary ledger_id={ledger_id}')
        ledger_capacity_by_id[ledger_id] = int(ledger_row['capacity'])
        ledger_usage_totals[ledger_id] = 0
    member_snapshot_source_refs_by_id = _index_s1_event_resolution_member_source_ref_catalog(_build_s1_event_resolution_member_source_ref_catalog(member_source_names_by_id or {}))
    established_source_keys: set[tuple[int, int, str, str]] = set()
    for source in resolution['established_sources']:
        source['source_name'] = source['source_name'].strip()
        if not source['source_name']:
            return (None, 'empty_established_source_name')
        if source['source_owner_id'] not in expected_ids:
            return (None, 'unknown_established_source_owner')
        if source['event_fact_index'] >= len(resolution['event_facts']):
            return (None, 'established_source_event_out_of_range')
        source_key = (source['event_fact_index'], source['source_owner_id'], source['source_kind'], source['source_name'])
        if source_key in established_source_keys:
            return (None, 'duplicate_established_source')
        established_source_keys.add(source_key)
    for row in resolution['member_outcomes']:
        death_reason = row['death_reason'].strip()
        max_hp = _coerce_int((member_max_hp_by_id or {}).get(row['id']), 0)
        current_hp = _coerce_int((member_current_hp_by_id or {}).get(row['id']), 0)
        hp_trace = row['hp_trace']
        direct_pressure_index = row.get('direct_terminal_pressure_index')
        if direct_pressure_index is not None and direct_pressure_index not in pressure_indices:
            return (None, f"member_outcome_direct_terminal_pressure_invalid id={row['id']}")
        if current_hp > 0 and hp_trace['starting_hp'] != current_hp:
            return (None, f"member_outcome_starting_hp_mismatch id={row['id']}")
        if max_hp > 0 and hp_trace['starting_hp'] > max_hp:
            return (None, f"member_outcome_starting_hp_above_max id={row['id']}")
        calculated_hp = hp_trace['starting_hp']
        hp_exhausted = False
        previous_event_fact_index = -1
        for change in hp_trace['changes']:
            event_fact_index = change['event_fact_index']
            pressure_index = change.get('pressure_index')
            if event_fact_index >= len(resolution['event_facts']):
                return (None, f"member_outcome_hp_event_index_out_of_range id={row['id']}")
            if event_fact_index < previous_event_fact_index:
                return (None, f"member_outcome_hp_events_out_of_order id={row['id']}")
            if pressure_index is not None and pressure_index not in pressure_indices:
                return (None, f"member_outcome_pressure_index_invalid id={row['id']}")
            if change['mitigation'] > change['raw_damage']:
                return (None, f"member_outcome_mitigation_above_raw_damage id={row['id']}")
            mitigation_sources = change['mitigation_sources']
            if change['mitigation'] == 0 and mitigation_sources:
                return (None, f"member_outcome_zero_mitigation_has_sources id={row['id']}")
            if change['mitigation'] > 0 and (not mitigation_sources):
                return (None, f"member_outcome_mitigation_missing_sources id={row['id']}")
            source_total = 0
            source_keys: set[tuple] = set()
            for source in mitigation_sources:
                source['effect_boundary'] = source['effect_boundary'].strip()
                if not source['effect_boundary']:
                    return (None, f"member_outcome_empty_mitigation_source id={row['id']}")
                source_ref_id = source.get('source_ref_id')
                ledger_id = source.get('ledger_id')
                if resolution['resolution_contract_version'] >= 2:
                    if ledger_id not in ledger_capacity_by_id:
                        return (None, f"member_outcome_unknown_mitigation_ledger_id id={row['id']}")
                    ledger_usage_totals[ledger_id] += int(source['mitigation'])
                source_origin = source['source_origin']
                supplied_identity = {'source_owner_id': source.get('source_owner_id'), 'source_kind': source.get('source_kind'), 'source_name': source['source_name'].strip() if isinstance(source.get('source_name'), str) else None}
                if source_origin == 'member_snapshot' and source_ref_id:
                    signed_ref = member_snapshot_source_refs_by_id.get(source_ref_id)
                    if signed_ref is None:
                        return (None, f"member_outcome_mitigation_source_ref_not_in_catalog id={row['id']}")
                    if any((value is not None for value in supplied_identity.values())):
                        if supplied_identity != signed_ref:
                            return (None, f"member_outcome_mitigation_source_ref_identity_mismatch id={row['id']}")
                    source.update(signed_ref)
                elif source_ref_id:
                    return (None, f"member_outcome_event_source_ref_id_unsupported id={row['id']}")
                elif any((value is None for value in supplied_identity.values())):
                    return (None, f"member_outcome_mitigation_source_identity_missing id={row['id']}")
                else:
                    source.update(supplied_identity)
                source.pop('source_ref_id', None)
                source_owner_id = int(source['source_owner_id'])
                if source_owner_id not in expected_ids:
                    return (None, f"member_outcome_unknown_mitigation_source_owner id={row['id']}")
                established_index = source['established_event_fact_index']
                snapshot_names = {name.strip() for name in (member_source_names_by_id or {}).get(source_owner_id, {}).get(source['source_kind'], set()) if isinstance(name, str) and name.strip()}
                if source['source_origin'] == 'event_fact' and source['source_kind'] != 'state' and (source['source_name'] in snapshot_names):
                    source['source_origin'] = 'member_snapshot'
                candidate_event_source_key = (established_index, source_owner_id, source['source_kind'], source['source_name'])
                if source['source_origin'] == 'member_snapshot' and source['source_name'] not in snapshot_names and (candidate_event_source_key in established_source_keys):
                    source['source_origin'] = 'event_fact'
                if source['source_origin'] == 'member_snapshot':
                    if source['source_kind'] == 'state':
                        return (None, f"member_outcome_snapshot_state_source_unsupported id={row['id']}")
                    if source['source_name'] not in snapshot_names:
                        return (None, f"member_outcome_unknown_mitigation_source_name id={row['id']}")
                    if established_index is not None and established_index > event_fact_index:
                        return (None, f"member_outcome_mitigation_source_timing_invalid id={row['id']}")
                else:
                    if established_index is None or established_index > event_fact_index:
                        return (None, f"member_outcome_mitigation_source_timing_invalid id={row['id']}")
                    if established_index >= len(resolution['event_facts']):
                        return (None, f"member_outcome_mitigation_source_event_out_of_range id={row['id']}")
                    event_source_key = (established_index, source_owner_id, source['source_kind'], source['source_name'])
                    if event_source_key not in established_source_keys:
                        return (None, f"member_outcome_unestablished_event_source id={row['id']}")
                source_key = (source_owner_id, source['source_kind'], source['source_name'], source['source_origin'], established_index)
                if source_key in source_keys:
                    return (None, f"member_outcome_duplicate_mitigation_source id={row['id']}")
                source_keys.add(source_key)
                source_total += int(source['mitigation'])
            if source_total != change['mitigation']:
                return (None, f"member_outcome_mitigation_source_sum_mismatch id={row['id']}")
            change['damage_taken'] = change['raw_damage'] - change['mitigation']
            if change['raw_damage'] == 0 and change['healing_received'] == 0:
                return (None, f"member_outcome_empty_hp_change id={row['id']}")
            if not hp_exhausted:
                calculated_hp = max(0, calculated_hp - change['damage_taken'])
                hp_exhausted = calculated_hp == 0
            if not hp_exhausted:
                calculated_hp += change['healing_received']
                if max_hp > 0:
                    calculated_hp = min(max_hp, calculated_hp)
            previous_event_fact_index = event_fact_index
        life_state_basis = hp_trace['life_state_basis']
        bypass_triggered = hp_trace['durability_bypass_triggered']
        if hp_exhausted and life_state_basis == 'durability_bypass' and bypass_triggered:
            life_state_basis = 'ordinary_hp_exhausted'
            bypass_triggered = False
            hp_trace['life_state_basis'] = life_state_basis
            hp_trace['durability_bypass_triggered'] = bypass_triggered
        if life_state_basis == 'survived':
            if bypass_triggered or hp_exhausted or calculated_hp < 1:
                return (None, f"invalid_survived_hp_trace id={row['id']}")
            expected_remaining_hp = calculated_hp
            expected_is_dead = False
        elif life_state_basis == 'ordinary_hp_exhausted':
            if bypass_triggered or not hp_exhausted:
                return (None, f"invalid_exhausted_hp_trace id={row['id']}")
            expected_remaining_hp = 0
            expected_is_dead = True
        elif life_state_basis == 'durability_bypass':
            if not bypass_triggered:
                return (None, f"invalid_durability_bypass_hp_trace id={row['id']}")
            expected_remaining_hp = 0
            expected_is_dead = True
        else:
            if bypass_triggered or hp_exhausted:
                return (None, f"invalid_unable_to_return_hp_trace id={row['id']}")
            expected_remaining_hp = 0
            expected_is_dead = True
        if row['is_dead'] != expected_is_dead:
            return (None, f"member_outcome_life_basis_mismatch id={row['id']}")
        if row['remaining_hp'] != expected_remaining_hp:
            return (None, f"member_outcome_remaining_hp_mismatch id={row['id']}")
        if row['is_dead']:
            if row['remaining_hp'] != 0 or not death_reason:
                return (None, f"invalid_dead_member_outcome id={row['id']}")
        elif row['remaining_hp'] < 1 or death_reason:
            return (None, f"invalid_surviving_member_outcome id={row['id']}")
        row['death_reason'] = death_reason
    resolution['completion_check']['ending_type'] = resolution['completion_check']['ending_type'].strip()
    if not resolution['completion_check']['ending_type']:
        return (None, 'empty_resolution_ending_type')
    if resolution['resolution_contract_version'] >= 2:
        for ledger_id, capacity in ledger_capacity_by_id.items():
            allocated = ledger_usage_totals[ledger_id]
            if allocated == 0:
                return (None, f'unused_mitigation_ledger_entry ledger_id={ledger_id}')
            if allocated > capacity:
                return (None, f'mitigation_ledger_capacity_exceeded ledger_id={ledger_id}')
    return (resolution, None)
def _derive_s1_canonical_event_resolution(proposal: dict, member_ids: list[int], member_max_hp_by_id: dict[int, int] | None, member_current_hp_by_id: dict[int, int] | None, allowed_pressure_indices: set[int] | None=None) -> tuple[dict | None, str | None]:
    """用冻结 HP 快照把 AI 的作用事实机械派生为规范生命底稿。"""
    expected_ids = [int(member_id) for member_id in member_ids]
    actual_ids = [row['id'] for row in proposal['member_impacts']]
    if len(actual_ids) != len(set(actual_ids)):
        return (None, 'duplicate_member_impact_id')
    if set(actual_ids) != set(expected_ids):
        return (None, f'member_impact_ids_mismatch expected={sorted(expected_ids)} actual={sorted(actual_ids)}')
    event_facts = proposal['event_facts']
    pressure_indices = set(allowed_pressure_indices or set())
    ledger_by_id: dict[str, dict] = {}
    for ledger_row in proposal['mitigation_ledger']:
        ledger_id = ledger_row['ledger_id'].strip()
        if not ledger_id:
            return (None, 'empty_mitigation_ledger_id')
        if ledger_id in ledger_by_id:
            return (None, f'duplicate_mitigation_ledger_id ledger_id={ledger_id}')
        ledger_row['ledger_id'] = ledger_id
        ledger_row['effect_boundary'] = ledger_row['effect_boundary'].strip()
        if not ledger_row['effect_boundary']:
            return (None, f'empty_mitigation_ledger_boundary ledger_id={ledger_id}')
        ledger_by_id[ledger_id] = ledger_row
    ledger_usage_totals = {ledger_id: 0 for ledger_id in ledger_by_id}
    outcomes: list[dict] = []
    for impact in proposal['member_impacts']:
        member_id = int(impact['id'])
        current_hp = _coerce_int((member_current_hp_by_id or {}).get(member_id), 0)
        max_hp = _coerce_int((member_max_hp_by_id or {}).get(member_id), 0)
        if current_hp < 1:
            return (None, f'member_current_hp_missing id={member_id}')
        if max_hp < 1:
            return (None, f'member_max_hp_missing id={member_id}')
        if current_hp > max_hp:
            return (None, f'member_current_hp_above_max id={member_id}')
        calculated_hp = current_hp
        hp_exhausted = False
        exhaustion_event_fact_index: int | None = None
        canonical_changes: list[dict] = []
        previous_event_fact_index = -1
        for change in impact['changes']:
            event_fact_index = int(change['event_fact_index'])
            pressure_index = change['pressure_index']
            raw_damage = int(change['raw_damage'])
            mitigation = int(change['mitigation'])
            healing_received = int(change['healing_received'])
            if event_fact_index >= len(event_facts):
                return (None, f'member_outcome_hp_event_index_out_of_range id={member_id}')
            if event_fact_index < previous_event_fact_index:
                return (None, f'member_outcome_hp_events_out_of_order id={member_id}')
            if pressure_index is not None and pressure_index not in pressure_indices:
                return (None, f'member_impact_pressure_index_invalid id={member_id}')
            if mitigation > raw_damage:
                return (None, f'member_outcome_mitigation_above_raw_damage id={member_id}')
            if raw_damage == 0 and healing_received == 0:
                return (None, f'member_outcome_empty_hp_change id={member_id}')
            allocation_total = 0
            allocation_ids: set[str] = set()
            canonical_sources: list[dict] = []
            for allocation in change['mitigation_sources']:
                ledger_id = allocation['ledger_id'].strip()
                if ledger_id not in ledger_by_id:
                    return (None, f'member_outcome_unknown_mitigation_ledger_id id={member_id} ledger_id={ledger_id}')
                if ledger_id in allocation_ids:
                    return (None, f'member_outcome_duplicate_mitigation_ledger_allocation id={member_id} ledger_id={ledger_id}')
                allocation_ids.add(ledger_id)
                allocation_value = int(allocation['mitigation'])
                allocation_total += allocation_value
                ledger_usage_totals[ledger_id] += allocation_value
                ledger_row = ledger_by_id[ledger_id]
                canonical_sources.append({**ledger_row, 'ledger_id': ledger_id, 'mitigation': allocation_value})
                canonical_sources[-1].pop('capacity', None)
            if allocation_total != mitigation:
                return (None, f'member_outcome_mitigation_source_sum_mismatch id={member_id}')
            damage_taken = raw_damage - mitigation
            if not hp_exhausted:
                calculated_hp = max(0, calculated_hp - damage_taken)
                hp_exhausted = calculated_hp == 0
                if hp_exhausted:
                    exhaustion_event_fact_index = event_fact_index
            if not hp_exhausted:
                calculated_hp = min(max_hp, calculated_hp + healing_received)
            canonical_changes.append({**change, 'mitigation_sources': canonical_sources, 'damage_taken': damage_taken})
            previous_event_fact_index = event_fact_index
        direct_state = impact['direct_terminal_state']
        direct_event_index = impact['direct_terminal_event_fact_index']
        direct_pressure_index = impact['direct_terminal_pressure_index']
        if hp_exhausted:
            life_state_basis = 'ordinary_hp_exhausted'
            durability_bypass_triggered = False
            remaining_hp = 0
            is_dead = True
            death_reason = event_facts[exhaustion_event_fact_index].strip()
        elif direct_state != 'none':
            if direct_event_index is None:
                return (None, f'member_impact_direct_terminal_event_required id={member_id}')
            if direct_event_index >= len(event_facts):
                return (None, f'member_impact_direct_terminal_event_out_of_range id={member_id}')
            if direct_event_index < previous_event_fact_index:
                return (None, f'member_impact_change_after_direct_terminal id={member_id}')
            if direct_pressure_index is not None and direct_pressure_index not in pressure_indices:
                return (None, f'member_impact_direct_terminal_pressure_invalid id={member_id}')
            life_state_basis = direct_state
            durability_bypass_triggered = direct_state == 'durability_bypass'
            remaining_hp = 0
            is_dead = True
            death_reason = event_facts[direct_event_index].strip()
        else:
            if direct_event_index is not None:
                return (None, f'member_impact_unexpected_direct_terminal_event id={member_id}')
            if direct_pressure_index is not None:
                return (None, f'member_impact_unexpected_direct_terminal_pressure id={member_id}')
            life_state_basis = 'survived'
            durability_bypass_triggered = False
            remaining_hp = calculated_hp
            is_dead = False
            death_reason = ''
        outcomes.append({'id': member_id, 'remaining_hp': remaining_hp, 'is_dead': is_dead, 'death_reason': death_reason, 'direct_terminal_pressure_index': direct_pressure_index if direct_state != 'none' else None, 'hp_trace': {'starting_hp': current_hp, 'changes': canonical_changes, 'durability_bypass_triggered': durability_bypass_triggered, 'life_state_basis': life_state_basis}})
    for ledger_id, ledger_row in ledger_by_id.items():
        allocated = ledger_usage_totals[ledger_id]
        if allocated == 0:
            return (None, f'unused_mitigation_ledger_entry ledger_id={ledger_id}')
        if allocated > int(ledger_row['capacity']):
            return (None, f'mitigation_ledger_capacity_exceeded ledger_id={ledger_id}')
    return ({'resolution_contract_version': proposal['resolution_contract_version'], 'event_facts': event_facts, 'established_sources': proposal['established_sources'], 'mitigation_ledger': proposal['mitigation_ledger'], 'member_outcomes': outcomes, 'completion_check': proposal['completion_check']}, None)
def _parse_s1_expedition_event_resolution(content: str | S1AiJsonResult | None, member_ids: list[int], member_max_hp_by_id: dict[int, int] | None=None, member_current_hp_by_id: dict[int, int] | None=None, member_source_names_by_id: dict[int, dict[str, set[str]]] | None=None, allowed_pressure_indices: set[int] | None=None, *, failure_feedback: dict | None=None) -> tuple[dict | None, str | None]:
    """解析 AI 作用事实，并用后台冻结 HP 派生唯一生命底稿。"""
    json_result = content if isinstance(content, S1AiJsonResult) else parse_s1_ai_json_content(content, repair_mode='syntax_only')
    if json_result.error_code:
        return (None, f"{json_result.error_code}: {json_result.error_detail or ''}".strip())
    if isinstance(json_result.data, dict) and json_result.data.get('resolution_contract_version') == 3:
        from plugins.adventurer_s1.services.s1_encounter_draft_contract import parse_compact_draft
        return parse_compact_draft(json_result.data, member_ids, member_max_hp_by_id or {}, member_current_hp_by_id or {}, failure_feedback=failure_feedback)
    try:
        normalized_data = _canonicalize_s1_expedition_event_resolution_payload(json_result.data)
        normalized_data = _normalize_s1_event_resolution_positional_member_ids(normalized_data, member_ids)
        proposal = _S1ExpeditionEventResolutionProposalModel.model_validate(normalized_data).model_dump()
    except ValidationError as exc:
        return (None, f'invalid_resolution_schema: {exc.errors(include_url=False)[:5]}')
    canonical, error = _derive_s1_canonical_event_resolution(proposal, member_ids, member_max_hp_by_id, member_current_hp_by_id, allowed_pressure_indices)
    if canonical is None:
        return (None, error)
    return _validate_s1_canonical_event_resolution(canonical, member_ids, member_max_hp_by_id, member_current_hp_by_id, member_source_names_by_id, allowed_pressure_indices)
def _parse_s1_expedition_death_resource_review(content: str | S1AiJsonResult | None, review_member_ids: list[int], review_ledger_ids: list[str] | None=None, event_fact_count: int | None=None) -> tuple[dict | None, str | None]:
    """严格校验只读资源一致性复核，不接受任何直接修正。"""
    json_result = content if isinstance(content, S1AiJsonResult) else parse_s1_ai_json_content(content, repair_mode='syntax_only')
    if json_result.error_code:
        return (None, f"{json_result.error_code}: {json_result.error_detail or ''}".strip())
    try:
        review = _S1ExpeditionDeathResourceReviewModel.model_validate(json_result.data).model_dump()
    except ValidationError as exc:
        return (None, f'invalid_death_resource_review_schema: {exc.errors(include_url=False)[:5]}')
    expected_ids = {int(member_id) for member_id in review_member_ids}
    actual_ids = review['reviewed_member_ids']
    if len(actual_ids) != len(set(actual_ids)):
        return (None, 'duplicate_resource_review_member_id')
    if set(actual_ids) != expected_ids:
        return (None, f'resource_review_member_ids_mismatch expected={sorted(expected_ids)} actual={sorted(actual_ids)}')
    expected_ledger_ids = {str(ledger_id).strip() for ledger_id in review_ledger_ids or [] if str(ledger_id).strip()}
    actual_ledger_ids = [ledger_id.strip() for ledger_id in review['reviewed_ledger_ids']]
    if any((not ledger_id for ledger_id in actual_ledger_ids)):
        return (None, 'empty_resource_review_ledger_id')
    if len(actual_ledger_ids) != len(set(actual_ledger_ids)):
        return (None, 'duplicate_resource_review_ledger_id')
    if set(actual_ledger_ids) != expected_ledger_ids:
        return (None, f'resource_review_ledger_ids_mismatch expected={sorted(expected_ledger_ids)} actual={sorted(actual_ledger_ids)}')
    issue_indexes = review['issue_event_fact_indexes']
    if len(issue_indexes) != len(set(issue_indexes)):
        return (None, 'duplicate_resource_review_event_fact_index')
    if any((index < 0 or (event_fact_count is not None and index >= event_fact_count) for index in issue_indexes)):
        return (None, 'resource_review_event_fact_index_out_of_range')
    review['reason'] = review['reason'].strip() or ('固定复核范围内未发现资源一致性硬冲突。' if review['verdict'] == 'confirmed' else '固定复核范围内存在需要重做因果底稿的硬冲突。')
    if review['verdict'] == 'confirmed' and issue_indexes:
        return (None, 'confirmed_resource_review_has_issues')
    if review['verdict'] == 'retry_causal_draft' and (not issue_indexes):
        return (None, 'retry_resource_review_missing_evidence')
    return (review, None)
_S1_DEATH_RESOURCE_REVIEW_CONTRACT_ERROR_CODES = frozenset({'duplicate_resource_review_member_id', 'resource_review_member_ids_mismatch', 'empty_resource_review_ledger_id', 'duplicate_resource_review_ledger_id', 'resource_review_ledger_ids_mismatch', 'duplicate_resource_review_event_fact_index', 'resource_review_event_fact_index_out_of_range', 'confirmed_resource_review_has_issues', 'retry_resource_review_missing_evidence'})
def _get_s1_death_resource_review_contract_error_code(error: str | None) -> str | None:
    """提取后端产生的稳定跨字段错误码，不把动态详情回灌给复核 AI。"""
    error_code = str(error or '').split(':', 1)[0].split(' ', 1)[0]
    return error_code if error_code in _S1_DEATH_RESOURCE_REVIEW_CONTRACT_ERROR_CODES else None
def _discard_s1_model_frozen_life_fields(result_data: dict | None) -> None:
    """旧版冻结裁决路径不让模型误抄的 HP/生死标量参与合并。"""
    if not isinstance(result_data, dict):
        return
    for row in result_data.get('member_updates') or []:
        if not isinstance(row, dict):
            continue
        row.pop('remaining_hp', None)
        row.pop('is_dead', None)

def _validate_s1_expedition_life_adjustments(result_data: dict | None, resolution: dict | None, member_max_hp_by_id: dict[int, int], member_source_names_by_id: dict[int, dict[str, set[str]]] | None=None) -> list[str]:
    """兼容旧版冻结底稿的稀疏追加变化；v2 由正文成品直接拥有终态。"""
    if not isinstance(result_data, dict) or not isinstance(resolution, dict):
        return []
    if resolution.get('resolution_contract_version') == 3:
        from plugins.adventurer_s1.services.s1_encounter_draft_contract import validate_compact_narrative_result
        return validate_compact_narrative_result(result_data, resolution)
    expected_rows = {int(row['id']): row for row in resolution.get('member_outcomes') or [] if isinstance(row, dict) and type(row.get('id')) is int}
    actual_rows = {int(row['id']): row for row in result_data.get('member_updates') or [] if isinstance(row, dict) and type(row.get('id')) is int}
    raw_adjustments = result_data.get('pre_narrative_life_adjustments', [])
    if int(resolution.get('resolution_contract_version') or 1) >= 2:
        result_data.pop('pre_narrative_life_adjustments', None)
        return []
    if not isinstance(raw_adjustments, list):
        return ['pre_narrative_life_adjustments 必须是数组']
    proposed_death_reasons: dict[int, str] = {}
    for member_id, expected in expected_rows.items():
        actual = actual_rows.get(member_id)
        if actual is None:
            continue
        proposed_reason = actual.get('death_reason')
        if isinstance(proposed_reason, str) and proposed_reason.strip():
            proposed_death_reasons[member_id] = proposed_reason.strip()
        actual['remaining_hp'] = expected['remaining_hp']
        actual['is_dead'] = expected['is_dead']
        if expected['is_dead']:
            actual['death_reason'] = str(expected.get('death_reason') or '').strip()
        else:
            actual.pop('death_reason', None)
    normalized: list[dict] = []
    errors: list[str] = []
    seen_ids: set[int] = set()
    for index, raw in enumerate(raw_adjustments):
        raw_for_validation = raw
        raw_member_id = raw.get('id') if isinstance(raw, dict) else None
        if type(raw_member_id) is int and raw_member_id in expected_rows:
            if expected_rows[raw_member_id].get('is_dead') is True:
                errors.append(f'冻结死亡成员 #{raw_member_id} 不得提交生命结果追加链')
                continue
            raw_for_validation = deepcopy(raw)
            raw_for_validation['starting_hp'] = expected_rows[raw_member_id]['remaining_hp']
            for change in raw_for_validation.get('changes') or []:
                if not isinstance(change, dict):
                    continue
                for source in change.get('effect_sources') or []:
                    if isinstance(source, dict) and source.get('source_origin') == 'member_snapshot' and (type(source.get('established_event_index')) is int) and (source['established_event_index'] == -1):
                        source['established_event_index'] = None
        try:
            adjustment = _S1ExpeditionLifeAdjustmentModel.model_validate(raw_for_validation)
        except ValidationError as exc:
            first_error = exc.errors()[0]
            location = '.'.join((str(part) for part in first_error.get('loc', ())))
            detail = first_error.get('msg', 'validation error')
            if location:
                detail = f'{location}: {detail}'
            errors.append(f'pre_narrative_life_adjustments[{index}] 结构非法: {detail}')
            continue
        item = adjustment.model_dump()
        member_id = item['id']
        if member_id not in expected_rows:
            errors.append(f'生命结果追加链成员 #{member_id} 不在前置底稿')
            continue
        if expected_rows[member_id].get('is_dead') is True:
            errors.append(f'冻结死亡成员 #{member_id} 不得提交生命结果追加链')
            continue
        if member_id not in actual_rows:
            errors.append(f'生命结果追加链成员 #{member_id} 缺少 member_update')
            continue
        if member_id in seen_ids:
            errors.append(f'成员 #{member_id} 重复提交生命结果追加链')
            continue
        seen_ids.add(member_id)
        normalized.append(item)
    if errors:
        return errors
    event_count = len(result_data.get('events') or [])
    applied_adjustments: list[dict] = []
    for item in normalized:
        member_id = item['id']
        expected = expected_rows[member_id]
        actual = actual_rows[member_id]
        if item['starting_hp'] != expected['remaining_hp']:
            errors.append(f'成员 #{member_id} 追加链起始 HP 与前置生命底稿不同')
            continue
        max_hp = max(1, _coerce_int(member_max_hp_by_id.get(member_id), 1))
        if item['terminal_event_index'] >= event_count:
            errors.append(f'成员 #{member_id} 追加链终态事件索引越界')
            continue
        calculated_hp = item['starting_hp']
        previous_event_index = -1
        for change in item['changes']:
            event_index = change['event_index']
            if event_index >= event_count:
                errors.append(f'成员 #{member_id} 追加链事件索引越界')
                break
            if event_index < previous_event_index:
                errors.append(f'成员 #{member_id} 追加链事件顺序倒置')
                break
            if change['damage_taken'] == 0 and change['healing_received'] == 0:
                errors.append(f'成员 #{member_id} 追加链包含空变化')
                break
            if calculated_hp == 0 and (not change['revival_triggered']):
                errors.append(f'成员 #{member_id} 在 HP 归零后缺少已成立复活触发')
                break
            effect_sources = change['effect_sources']
            if change['healing_received'] == 0 and (not change['revival_triggered']):
                effect_sources = []
                change['effect_sources'] = effect_sources
            elif not effect_sources:
                errors.append(f'成员 #{member_id} 的恢复或复活缺少实际来源')
                break
            source_keys: set[tuple] = set()
            source_invalid = False
            for source in effect_sources:
                source['source_name'] = source['source_name'].strip()
                source['effect_boundary'] = source['effect_boundary'].strip()
                if not source['source_name'] or not source['effect_boundary']:
                    errors.append(f'成员 #{member_id} 的生命效果来源不完整')
                    source_invalid = True
                    break
                source_owner_id = int(source['source_owner_id'])
                if source_owner_id not in expected_rows:
                    errors.append(f'成员 #{member_id} 的生命效果来源持有人不在本队')
                    source_invalid = True
                    break
                established_index = source['established_event_index']
                snapshot_names = (member_source_names_by_id or {}).get(source_owner_id, {}).get(source['source_kind'], set())
                if source['source_origin'] == 'event' and source['source_kind'] != 'state' and (source['source_name'] in snapshot_names):
                    source['source_origin'] = 'member_snapshot'
                if source['source_origin'] == 'member_snapshot':
                    if source['source_kind'] == 'state' or source['source_name'] not in snapshot_names:
                        errors.append(f'成员 #{member_id} 的生命效果来源无法映射到出发快照')
                        source_invalid = True
                        break
                    if established_index is not None and established_index > event_index:
                        errors.append(f'成员 #{member_id} 的生命效果来源晚于结果')
                        source_invalid = True
                        break
                elif established_index is None or established_index > event_index:
                    errors.append(f'成员 #{member_id} 的事件来源缺少有效成立时序')
                    source_invalid = True
                    break
                elif established_index >= event_count:
                    errors.append(f'成员 #{member_id} 的事件来源索引越界')
                    source_invalid = True
                    break
                source_key = (source_owner_id, source['source_kind'], source['source_name'], source['source_origin'], established_index)
                if source_key in source_keys:
                    errors.append(f'成员 #{member_id} 重复提交生命效果来源')
                    source_invalid = True
                    break
                source_keys.add(source_key)
            if source_invalid:
                break
            damaged_hp = max(0, calculated_hp - change['damage_taken'])
            if change['revival_triggered']:
                if damaged_hp != 0 or change['healing_received'] <= 0:
                    errors.append(f'成员 #{member_id} 的复活触发与 HP 变化不一致')
                    break
                calculated_hp = min(max_hp, change['healing_received'])
            elif damaged_hp == 0:
                if change['healing_received'] > 0:
                    errors.append(f'成员 #{member_id} 用普通治疗从 0 HP 复活')
                    break
                calculated_hp = 0
            else:
                calculated_hp = min(max_hp, damaged_hp + change['healing_received'])
            previous_event_index = event_index
        else:
            if item['terminal_event_index'] < previous_event_index:
                errors.append(f'成员 #{member_id} 追加链终态早于 HP 变化')
                continue
            basis = item['final_state_basis']
            bypass = item['durability_bypass_triggered']
            if basis == 'survived':
                valid_basis = not bypass and calculated_hp > 0
                expected_hp, expected_dead = (calculated_hp, False)
            elif basis == 'ordinary_hp_exhausted':
                valid_basis = not bypass and calculated_hp == 0
                expected_hp, expected_dead = (0, True)
            elif basis == 'durability_bypass':
                valid_basis = bypass
                expected_hp, expected_dead = (0, True)
            else:
                valid_basis = not bypass and calculated_hp > 0
                expected_hp, expected_dead = (0, True)
            if not valid_basis:
                errors.append(f'成员 #{member_id} 追加链终态依据不成立')
                continue
            actual['remaining_hp'] = expected_hp
            actual['is_dead'] = expected_dead
            if expected_dead:
                proposed_reason = proposed_death_reasons.get(member_id)
                if proposed_reason:
                    actual['death_reason'] = proposed_reason
                else:
                    actual.pop('death_reason', None)
            else:
                actual.pop('death_reason', None)
            if expected_hp != expected['remaining_hp'] or expected_dead != expected['is_dead']:
                applied_adjustments.append(item)
    if not errors:
        result_data['pre_narrative_life_adjustments'] = applied_adjustments
    return errors

def _get_s1_expedition_resolution_review_differences(result_data: dict | None, resolution: dict | None) -> list[str]:
    """列出成品与第一阶段草稿差异，作为 Observer 核对线索而非否决。"""
    if not isinstance(resolution, dict):
        return []
    if resolution.get('resolution_contract_version') == 3:
        return []
    if not isinstance(result_data, dict):
        return ['主剧情因果底稿存在，但正文没有合法结算对象']
    expected_rows = {int(row['id']): row for row in resolution.get('member_outcomes') or [] if isinstance(row, dict) and type(row.get('id')) is int}
    actual_rows = {int(row['id']): row for row in result_data.get('member_updates') or [] if isinstance(row, dict) and type(row.get('id')) is int}
    errors: list[str] = []
    for member_id, expected in expected_rows.items():
        actual = actual_rows.get(member_id)
        if actual is None:
            errors.append(f'成员 #{member_id} 缺少与因果底稿对应的 member_update')
            continue
        actual_hp = _coerce_int(actual.get('remaining_hp'), -1)
        actual_dead = bool(actual.get('is_dead', False))
        if actual_hp != expected['remaining_hp']:
            errors.append(f'成员 #{member_id} remaining_hp 与第一阶段因果草稿不同，须按最终 events 核对成品结算')
        if actual_dead != expected['is_dead']:
            errors.append(f'成员 #{member_id} is_dead 与第一阶段因果草稿不同，须按最终 events 核对成品结算')
    expected_completion = resolution.get('completion_check')
    actual_completion = result_data.get('completion_check')
    if isinstance(expected_completion, dict) and isinstance(actual_completion, dict):
        for field in ('core_resolved', 'exit_resolved', 'ending_type'):
            if actual_completion.get(field) != expected_completion.get(field):
                errors.append(f'completion_check.{field} 与第一阶段因果草稿不同，须按最终 events 核对成品结算')
    return errors
def _build_s1_resource_consistency_review_scope(resolution: dict | None) -> tuple[list[int], list[str]]:
    """只选择不可逆成员及其实际使用过的防护与关联来源成员。"""
    if not isinstance(resolution, dict):
        return ([], [])
    irreversible_member_ids: set[int] = set()
    ledger_uses: dict[str, set[tuple[int, int]]] = {}
    ledgers_by_member: dict[int, set[str]] = {}
    ledger_owner_by_id: dict[str, int] = {}
    for ledger in resolution.get('mitigation_ledger') or []:
        if not isinstance(ledger, dict):
            continue
        ledger_id = str(ledger.get('ledger_id') or '').strip()
        owner_id = ledger.get('source_owner_id')
        if ledger_id and type(owner_id) is int and (owner_id > 0):
            ledger_owner_by_id[ledger_id] = owner_id
    for outcome in resolution.get('member_outcomes') or []:
        if not isinstance(outcome, dict) or type(outcome.get('id')) is not int:
            continue
        member_id = int(outcome['id'])
        if outcome.get('is_dead') is True:
            irreversible_member_ids.add(member_id)
        trace = outcome.get('hp_trace')
        changes = trace.get('changes') if isinstance(trace, dict) else []
        for change_index, change in enumerate(changes or []):
            if not isinstance(change, dict):
                continue
            for source in change.get('mitigation_sources') or []:
                if not isinstance(source, dict):
                    continue
                ledger_id = str(source.get('ledger_id') or '').strip()
                if not ledger_id:
                    continue
                ledger_uses.setdefault(ledger_id, set()).add((member_id, change_index))
                ledgers_by_member.setdefault(member_id, set()).add(ledger_id)
    if not irreversible_member_ids:
        return ([], [])
    reviewed_ledger_ids: set[str] = set()
    for member_id in irreversible_member_ids:
        reviewed_ledger_ids.update(ledgers_by_member.get(member_id, set()))
    reviewed_member_ids = set(irreversible_member_ids)
    for ledger_id in reviewed_ledger_ids:
        reviewed_member_ids.update((member_id for member_id, _ in ledger_uses[ledger_id]))
        owner_id = ledger_owner_by_id.get(ledger_id)
        if owner_id is not None:
            reviewed_member_ids.add(owner_id)
    return (sorted(reviewed_member_ids), sorted(reviewed_ledger_ids))
def _project_s1_death_resource_review_resolution(resolution: dict, review_member_ids: list[int], review_ledger_ids: list[str], *, member_ref_by_id: dict[int, str] | None=None) -> dict:
    """保留完整事件脊柱，只投影固定范围的成员结果与共享账本。"""
    member_ids = {member_id for member_id in review_member_ids if type(member_id) is int and member_id > 0}
    ledger_ids = {ledger_id.strip() for ledger_id in review_ledger_ids if isinstance(ledger_id, str) and ledger_id.strip()}
    if resolution.get('resolution_contract_version') == 3:
        summaries = {row['id']: row['summary'] for row in resolution.get('member_summaries', [])}
        refs = member_ref_by_id or {}
        return {'resolution_contract_version': 3, 'event_facts': [{'event_fact_index': index, 'text': deepcopy(fact)} for index, fact in enumerate(resolution.get('event_facts') or [])], 'member_outcomes': [{**{key: deepcopy(row[key]) for key in ('id', 'remaining_hp', 'is_dead', 'hp_trace') if key in row}, **({'member_ref': refs[row['id']]} if row['id'] in refs else {}), **({'summary': summaries[row['id']]} if row['id'] in summaries else {})} for row in resolution.get('member_outcomes') or [] if isinstance(row, dict) and type(row.get('id')) is int and (row['id'] in member_ids)]}
    return {'resolution_contract_version': resolution.get('resolution_contract_version', 2), 'event_facts': deepcopy(resolution.get('event_facts') or []), 'established_sources': deepcopy(resolution.get('established_sources') or []), 'mitigation_ledger': [deepcopy(row) for row in resolution.get('mitigation_ledger') or [] if isinstance(row, dict) and str(row.get('ledger_id') or '').strip() in ledger_ids], 'member_outcomes': [deepcopy(row) for row in resolution.get('member_outcomes') or [] if isinstance(row, dict) and type(row.get('id')) is int and (row['id'] in member_ids)]}
def _call_s1_expedition_death_resource_review_ai(*, user_prompt: str, resolution: dict, review_member_ids: list[int], review_ledger_ids: list[str], api_base: str, headers: dict, model: str, timeout: int, party_id: int | None, expedition_log_id: int | None, attempt_number: int, retry_trace: list[dict] | None=None, members: list | None=None, name_alias: dict[int, str] | None=None, physical_attempt_guard: Callable[[], None] | None=None, special_event_type: str | None=None) -> dict | None:
    """只读复核不可逆结果与共享防护；有问题时要求主因果底稿重做。"""
    from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
    system_prompt = build_s1_expedition_death_resource_review_system_prompt(3, special_event_type=special_event_type) if resolution.get('resolution_contract_version') == 3 else build_s1_expedition_death_resource_review_system_prompt()
    projected_resolution = _project_s1_death_resource_review_resolution(resolution, review_member_ids, review_ledger_ids, member_ref_by_id=name_alias)
    review_user_prompt = f"{user_prompt}\n\n【待复核的主剧情因果底稿（后台内部）】\n{json.dumps(projected_resolution, ensure_ascii=False, separators=(',', ':'))}\n【服务端固定复核范围】\n{json.dumps({'reviewed_member_ids': review_member_ids, 'reviewed_ledger_ids': review_ledger_ids}, ensure_ascii=False, separators=(',', ':'))}\n只核对该范围是否与冻结资源、事件时序及共享容量一致；不得提交修正值。"
    review_user_prompt = anonymize_s1_prompt_member_names(review_user_prompt, members, name_alias)
    codename_to_real = build_codename_to_real_from_alias(name_alias, {member.id: member.name for member in members or [] if getattr(member, 'name', None)})
    structure_retries = max(0, int(current_app.config.get('ADVENTURER_S1_DEATH_RESOURCE_REVIEW_STRUCTURE_RETRIES', 1)))
    contract_retries = max(0, int(current_app.config.get('ADVENTURER_S1_DEATH_RESOURCE_REVIEW_CONTRACT_RETRIES', 1)))
    messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': review_user_prompt}]

    def validate_death_resource_review_response(decoded) -> S1AiResponseContractResult:
        parsed_candidate = _restore_codenames_in_ai_json_result(decoded.json_result, codename_to_real)
        review_candidate, validation_error = _parse_s1_expedition_death_resource_review(parsed_candidate, review_member_ids, review_ledger_ids, len(resolution.get('event_facts') or []))
        if review_candidate is not None:
            return S1AiResponseContractResult.accept()
        return S1AiResponseContractResult.reject(str(validation_error or parsed_candidate.error_code or 'death_resource_review_invalid').split(':', 1)[0], failure_stage='response_parse' if parsed_candidate.error_code else 'contract_validation')
    try:
        structure_attempt = 0
        contract_attempt = 0
        request_metadata: dict[str, object] = {}
        usage = None
        while True:
            request_metadata = {}
            usage = None
            usage_recorded = False
            db_manager.remove()
            resp = post_s1_ai_request(f'{api_base}/chat/completions', feature='expedition_death_resource_review', request_metadata_out=request_metadata, physical_attempt_guard=physical_attempt_guard, headers=headers, json={'model': model, 'messages': messages, 'temperature': 0, 'response_format': {'type': 'json_object'}}, timeout=timeout, response_validator=validate_death_resource_review_response)
            decoded = parse_s1_ai_http_json_response(resp)
            payload = decoded.http_result.response_json or {}
            enrich_s1_ai_response_metadata(resp, payload)
            parsed_json = decoded.json_result
            parsed_json = _restore_codenames_in_ai_json_result(parsed_json, codename_to_real)
            review, error = _parse_s1_expedition_death_resource_review(parsed_json, review_member_ids, review_ledger_ids, len(resolution.get('event_facts') or []))
            usage = decoded.http_result.usage
            metadata = get_s1_ai_response_metadata(resp)
            if review is not None and physical_attempt_guard is not None:
                try:
                    physical_attempt_guard()
                except Exception as claim_exc:
                    record_token_usage(call_type='expedition_death_resource_review_ai', usage=usage, model=metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_number, is_success=False, failure_stage='pipeline_ownership', validation_error_code='stale_owner_unconsumed' if isinstance(claim_exc, _S1ExpeditionGenerationClaimLost) else 'generation_claim_check_failed', finish_reason=parsed_json.finish_reason, json_object_mode=metadata.get('json_object_mode'), provider_request_id=metadata.get('request_id'), repair_applied=parsed_json.repair_applied, failed_response_excerpt=parsed_json.raw_excerpt, record_without_usage=not bool(usage), request_metadata=metadata)
                    usage_recorded = True
                    raise
            if usage:
                record_token_usage(call_type='expedition_death_resource_review_ai', usage=usage, model=metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_number, is_success=review is not None)
                usage_recorded = True
            log_s1_ai_response_outcome(resp, feature='expedition_death_resource_review', result_status=f"json_usable_{review['verdict']}" if review is not None else 'unusable', finish_reason=parsed_json.finish_reason, error_code=parsed_json.error_code or (None if review is not None else 'business_validation_failed'), attempt_number=attempt_number, expedition_log_id=expedition_log_id)
            if review is not None:
                return review
            is_structure_error = parsed_json.finish_reason != 'length' and (parsed_json.error_code in {'invalid_json', 'repair_failed', 'not_object'} or str(error or '').startswith('invalid_death_resource_review_schema:'))
            contract_error_code = _get_s1_death_resource_review_contract_error_code(error)
            if is_structure_error and structure_attempt < structure_retries:
                structure_attempt += 1
                _record_s1_retry_decision(retry_trace, stage='expedition_death_resource_review', reason_code=parsed_json.error_code or 'invalid_death_resource_review_schema', action=S1RetryAction.REPAIR_LOCAL, attempt=structure_attempt, max_attempts=structure_retries, preserves_main_result=None, detail='仅修复资源一致性复核 JSON/schema；主剧情因果底稿保持不变。')
                structure_repair_user_prompt = f'结构校验失败项：{error or parsed_json.error_code}\n原始输出：\n{parsed_json.content}'
                structure_repair_user_prompt = anonymize_s1_prompt_member_names(structure_repair_user_prompt, members, name_alias)
                messages = [{'role': 'system', 'content': build_s1_expedition_death_resource_structure_repair_system_prompt()}, {'role': 'user', 'content': structure_repair_user_prompt}]
                continue
            if contract_error_code and contract_attempt < contract_retries:
                contract_attempt += 1
                retry_contract = json.dumps({'previous_attempt_error': contract_error_code}, ensure_ascii=False, separators=(',', ':'))
                _record_s1_retry_decision(retry_trace, stage='expedition_death_resource_review', reason_code=contract_error_code, action=S1RetryAction.RETRY_DEATH_RESOURCE_REVIEW, attempt=contract_attempt, max_attempts=contract_retries, preserves_main_result=None, detail='跨字段契约矛盾；保留主剧情因果底稿并只重跑一致性复核。')
                messages = [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': f'{review_user_prompt}\n\n【结构化重试信息】\n{retry_contract}'}]
                continue
            if not is_structure_error or structure_attempt >= structure_retries:
                current_app.logger.warning('S1 AI 资源一致性复核失败 stage=expedition_death_resource_review party=%s log=%s attempt=%s structure_repairs=%s contract_retries=%s error=%s', party_id, expedition_log_id, attempt_number, structure_attempt, contract_attempt, error)
                break
    except (AIProfileAssignmentError, requests.exceptions.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
        if not usage_recorded:
            failure_stage = 'route_assignment' if isinstance(exc, AIProfileAssignmentError) else 'http_or_response'
            record_token_usage(call_type='expedition_death_resource_review_ai', usage=usage, model=request_metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_number, is_success=False, failure_stage=failure_stage, validation_error_code=f'{failure_stage}.{type(exc).__name__}', record_without_usage=True, request_metadata=request_metadata, request_exception=exc)
        current_app.logger.warning('S1 AI 资源一致性复核调用失败 stage=expedition_death_resource_review party=%s log=%s attempt=%s error=%s detail=%s', party_id, expedition_log_id, attempt_number, type(exc).__name__, str(exc)[:500])
    return None
_S1_EVENT_RESOLUTION_MEMBER_SOURCE_REF_ID_PREFIX = 'R'
def _s1_event_resolution_member_source_ref_id(catalog_ordinal: int) -> str:
    """Issue one compact request-scoped ID in deterministic catalog order."""
    return f'{_S1_EVENT_RESOLUTION_MEMBER_SOURCE_REF_ID_PREFIX}{catalog_ordinal}'
def _build_s1_event_resolution_member_source_ref_catalog(member_source_names_by_id: dict[int, dict[str, set[str]]]) -> list[dict]:
    """Build the backend-signed catalog for frozen member source identities."""
    catalog: list[dict] = []
    for member_id in sorted(member_source_names_by_id):
        source_names = member_source_names_by_id.get(member_id) or {}
        for source_kind in ('equipment', 'skill', 'trait'):
            names = sorted({name.strip() for name in source_names.get(source_kind) or set() if isinstance(name, str) and name.strip()})
            for source_name in names:
                catalog.append({'source_ref_id': _s1_event_resolution_member_source_ref_id(len(catalog) + 1), 'source_owner_id': int(member_id), 'source_kind': source_kind, 'source_name': source_name})
    return catalog

def _index_s1_event_resolution_member_source_ref_catalog(catalog: list[dict]) -> dict[str, dict]:
    """Index only rows whose opaque IDs match their exact backend identity."""
    indexed: dict[str, dict] = {}
    for catalog_ordinal, row in enumerate(catalog, start=1):
        source_ref_id = row.get('source_ref_id')
        source_owner_id = row.get('source_owner_id')
        source_kind = row.get('source_kind')
        source_name = row.get('source_name')
        if not isinstance(source_ref_id, str) or type(source_owner_id) is not int or source_kind not in {'equipment', 'skill', 'trait'} or (not isinstance(source_name, str)) or (not source_name) or (source_ref_id != _s1_event_resolution_member_source_ref_id(catalog_ordinal)):
            continue
        identity = {'source_owner_id': source_owner_id, 'source_kind': source_kind, 'source_name': source_name}
        if source_ref_id in indexed and indexed[source_ref_id] != identity:
            continue
        indexed[source_ref_id] = identity
    return indexed

def _build_s1_event_resolution_source_catalog_prompt(member_source_names_by_id: dict[int, dict[str, set[str]]]) -> str:
    """Project exact frozen member sources as model-copyable signed IDs."""
    catalog = _build_s1_event_resolution_member_source_ref_catalog(member_source_names_by_id)
    refs_by_identity = {(row['source_owner_id'], row['source_kind'], row['source_name']): row['source_ref_id'] for row in catalog}
    members: list[dict] = []
    for member_id in sorted(member_source_names_by_id):
        source_names = member_source_names_by_id.get(member_id) or {}
        member = {'source_owner_id': int(member_id)}
        for source_kind in ('equipment', 'skill', 'trait'):
            member[source_kind] = {source_name: refs_by_identity[int(member_id), source_kind, source_name] for source_name in sorted({name.strip() for name in source_names.get(source_kind) or set() if isinstance(name, str) and name.strip()})}
        members.append(member)
    payload = json.dumps({'member_snapshot_sources': members}, ensure_ascii=False, separators=(',', ':'))
    return f'【member_snapshot 缓解来源精确白名单（后台权威）】\n{payload}\n`source_origin:"member_snapshot"` 时，只从对应名称右侧逐字复制本次请求内的 `source_ref_id`，不得输出或猜测 `source_owner_id`、`source_kind`、`source_name`；后端从该 ID 恢复唯一冻结身份。未列出的新来源只能先在本次 event_facts 与 established_sources 中精确建立，再在 mitigation_ledger 中使用 `source_origin:"event_fact"`。'
def _build_s1_event_resolution_retry_prompt(user_prompt: str, error: str, contract_version: int=2, *, review_feedback: dict | None=None, contract_feedback: dict | None=None) -> str:
    """Retry from validated formal evidence; never replay raw model reasoning."""
    error_code = str(error or 'business_validation_failed')
    error_code = error_code.split(':', 1)[0].split(' ', 1)[0][:96]
    retry_payload = {'previous_attempt_error': error_code}
    if contract_version == 3:
        retry_payload['required_contract'] = '沿当前 events/fact/impacts 合同完整重新推演；每项作用绑定本事件准确成员 id、防护后实际净损耗 damage、有效 healing 或直接终态；没有生命变化时 impacts 为空，后台从冻结生命顺序扣损耗并计有效恢复，持续防护的来源、范围、次数和耗尽写入同一事件事实。'
        if error_code == 'resource_consistency_review_retry' and review_feedback:
            retry_payload['resource_review_feedback'] = review_feedback
            retry_payload['required_contract'] = '下方是上次未通过的候选过程及生命资源复核指出的具体矛盾，均不新增权威事实。先对照本次冻结能力、风险模式和生命规则定位这些矛盾；与问题无关且符合冻结权威的过程沿用，从被修正的状态重新推演受影响的后续，交付完整 events/fact/impacts 与总结，不输出局部补丁。修正成员生命不改变本场核心身份、能力及模式边界，复核意见不预定胜负或生死。'
        elif error_code in {'compact_draft_effect_after_terminal', 'compact_draft_healing_after_terminal', 'compact_draft_state_hint_mismatch'} and contract_feedback:
            retry_payload['contract_failure_feedback'] = contract_feedback
            correction = 'terminal_conflicts 逐项列出本次候选中各成员首次在终态后继续提交生命变化的冲突；terminal_event_fact_index 是候选记录形成终态的位置，issue_event_fact_index 是终态后仍提交生命变化的位置；同一条作用含致死与恢复时二者相同，candidate_remaining_hp 是此前记录在终态处的余额；这些只说明候选内部矛盾，不证明原作用合法。完整核对所列成员；仅提供单个指定成员时，核对该成员。若此前裁定成立，从形成终态的那一步起停止该成员后续主动行动、恢复与生命扣减，其他主体从当时的真实状态继续；若此前作用有误，按冻结能力修正实际发生阶段，再从修正后的状态重新推演受影响的后续。' if error_code in {'compact_draft_effect_after_terminal', 'compact_draft_healing_after_terminal'} else 'state_hint_conflicts 列出最早矛盾阶段的全部成员；仅提供单个指定成员时核对该成员。先按冻结能力、实际接触与防护确认本步实际净损耗，再核对扣后余额；damage 已经是防护后的损耗，不能再次扣除防护，也不能用希望保留的 hp_after 反算 damage。原作用成立时修正生命备忘并沿真实生命状态推进，包括生命耗尽后的终止；原作用有误时只凭原有交锋依据重新裁决，从修正后的状态推进后续。'
            retry_payload['required_contract'] = '下方只回放上次未通过候选中截至错误位置的事件及其内部生命轨迹，不是已发生的世界事实。' + correction + '交付完整底稿，不只交付补丁，不为通过校验取消已有的有效伤害或保命效果。'
    elif error_code in {'member_impact_direct_terminal_event_required', 'member_impact_direct_terminal_event_out_of_range', 'member_impact_change_after_direct_terminal'}:
        retry_payload['required_contract'] = 'direct_terminal_state非none时必须用有效的direct_terminal_event_fact_index精确引用形成该非HP终态的事件事实，该成员不得在该终态索引之后再提交普通作用变化。'
    elif error_code in {'member_outcome_unknown_mitigation_source_name', 'member_outcome_mitigation_source_ref_not_in_catalog', 'member_outcome_mitigation_source_ref_identity_mismatch', 'member_outcome_mitigation_source_identity_missing'}:
        retry_payload['required_contract'] = 'source_origin为member_snapshot时只提交输入末尾白名单中名称右侧的source_ref_id，不提交source_owner_id、source_kind或source_name；逐字复制ID，不得简称、改写、补全或自造。'
    elif error_code in {'member_outcome_unknown_mitigation_ledger_id', 'member_outcome_duplicate_mitigation_ledger_allocation', 'member_outcome_mitigation_source_sum_mismatch', 'unused_mitigation_ledger_entry', 'mitigation_ledger_capacity_exceeded', 'resource_consistency_review_retry'}:
        retry_payload['required_contract'] = '先在mitigation_ledger中一次声明每个已成立防护的身份、完整容量与作用边界，再由全部成员和接触用ledger_id分配；每个分量必须是正整数，每项变化的mitigation必须精确等于其分量之和，全局分配总量不得超过该项容量。保持威胁行动、raw_damage与生命结果均从重新形成的主剧情因果底稿正向得出。'
    elif error_code in {'member_impact_pressure_index_invalid', 'member_impact_direct_terminal_pressure_invalid'}:
        retry_payload['required_contract'] = 'pressure_index只追踪实际采用的客观大纲压力候选；非null时必须逐字使用user prompt中lethal_pressure_structures对应项的零起始有效索引。由其它硬来源能力边界或更早事件独立建立的作用使用null；索引不授予强度或结果。'
    retry_contract = json.dumps(retry_payload, ensure_ascii=False, separators=(',', ':'))
    return f'{user_prompt}\n\n【结构化重试信息】\n{retry_contract}'
def _call_s1_expedition_event_resolution_ai(*, user_prompt: str, member_ids: list[int], member_max_hp_by_id: dict[int, int], member_current_hp_by_id: dict[int, int], member_source_names_by_id: dict[int, dict[str, set[str]]], allowed_pressure_indices: set[int], api_base: str, headers: dict, model: str, timeout: int, max_retries: int, party_id: int | None, expedition_log_id: int | None, retry_trace: list[dict] | None=None, members: list | None=None, name_alias: dict[int, str] | None=None, resource_review_prompt_factory: Callable[[list[int]], str] | None=None, physical_attempt_guard: Callable[[], None] | None=None, defer_accepted_usage: bool=False, contract_version: int=2, special_event_type: str | None=None) -> dict | None:
    """高危主剧情写作前裁决事件；拟死亡结果经独立资源复核后才冻结。"""
    from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
    system_prompt = build_s1_expedition_event_resolution_system_prompt(contract_version, special_event_type=special_event_type)
    if contract_version == 3:
        base_user_prompt = user_prompt
    else:
        source_catalog_prompt = _build_s1_event_resolution_source_catalog_prompt(member_source_names_by_id)
        base_user_prompt = f'{user_prompt}\n\n{source_catalog_prompt}'
    base_user_prompt = anonymize_s1_prompt_member_names(base_user_prompt, members, name_alias)
    codename_to_real = build_codename_to_real_from_alias(name_alias, {member.id: member.name for member in members or [] if getattr(member, 'name', None)})

    def validate_event_resolution_response(decoded) -> S1AiResponseContractResult:
        parsed_candidate = _restore_codenames_in_ai_json_result(decoded.json_result, codename_to_real)
        resolution_candidate, validation_error = _parse_s1_expedition_event_resolution(parsed_candidate, member_ids, member_max_hp_by_id, member_current_hp_by_id, member_source_names_by_id, allowed_pressure_indices)
        if resolution_candidate is not None and resolution_candidate.get('resolution_contract_version') != contract_version:
            return S1AiResponseContractResult.reject('resolution_contract_version_mismatch', failure_stage='contract_validation')
        if resolution_candidate is not None:
            return S1AiResponseContractResult.accept()
        return S1AiResponseContractResult.reject(str(validation_error or parsed_candidate.error_code or 'event_resolution_invalid').split(':', 1)[0].split(' ', 1)[0], failure_stage='response_parse' if parsed_candidate.error_code else 'contract_validation')
    attempts = min(2, max(1, int(max_retries or 1)))
    validation_error_code: str | None = None
    resource_review_feedback: dict | None = None
    contract_failure_feedback: dict = {}
    for attempt_no in range(1, attempts + 1):
        usage = None
        metadata: dict = {}
        parsed_json: S1AiJsonResult | None = None
        usage_recorded = False
        try:
            attempt_user_prompt = base_user_prompt
            if validation_error_code:
                attempt_user_prompt = _build_s1_event_resolution_retry_prompt(base_user_prompt, validation_error_code, contract_version, review_feedback=resource_review_feedback, contract_feedback=contract_failure_feedback)
            attempt_user_prompt = anonymize_s1_prompt_member_names(attempt_user_prompt, members, name_alias)
            db_manager.remove()
            resp = post_s1_ai_request(f'{api_base}/chat/completions', feature='expedition_resolution', request_metadata_out=metadata, physical_attempt_guard=physical_attempt_guard, headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': attempt_user_prompt}], 'temperature': 0.1, 'response_format': {'type': 'json_object'}}, timeout=timeout, response_validator=validate_event_resolution_response)
            decoded = parse_s1_ai_http_json_response(resp)
            payload = decoded.http_result.response_json or {}
            usage = decoded.http_result.usage
            enrich_s1_ai_response_metadata(resp, payload)
            metadata = get_s1_ai_response_metadata(resp)
            parsed_json = decoded.json_result
            parsed_json = _restore_codenames_in_ai_json_result(parsed_json, codename_to_real)
            resolution, error = _parse_s1_expedition_event_resolution(parsed_json, member_ids, member_max_hp_by_id, member_current_hp_by_id, member_source_names_by_id, allowed_pressure_indices, failure_feedback=contract_failure_feedback)
            if resolution is not None and resolution.get('resolution_contract_version') != contract_version:
                resolution, error = (None, 'resolution_contract_version_mismatch')
            if resolution is not None:
                review_member_ids, review_ledger_ids = _build_s1_resource_consistency_review_scope(resolution)
                review = None
                if review_member_ids:
                    resource_review_user_prompt = base_user_prompt
                    if resource_review_prompt_factory is not None:
                        try:
                            projected_review_prompt = resource_review_prompt_factory(member_ids if contract_version == 3 else review_member_ids)
                            if str(projected_review_prompt or '').strip():
                                resource_review_user_prompt = str(projected_review_prompt)
                        except Exception:
                            current_app.logger.warning('S1 不可逆结果资源复核输入投影失败，保守复用完整因果输入 party=%s log=%s', party_id, expedition_log_id, exc_info=True)
                    review = _call_s1_expedition_death_resource_review_ai(user_prompt=resource_review_user_prompt, resolution=resolution, review_member_ids=review_member_ids, review_ledger_ids=review_ledger_ids, api_base=api_base, headers=headers, model=model, timeout=timeout, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_no, retry_trace=retry_trace, members=members, name_alias=name_alias, physical_attempt_guard=physical_attempt_guard, special_event_type=special_event_type)
                accepted = not review_member_ids or (review is not None and review['verdict'] == 'confirmed')
                review_error_code = None if accepted else 'resource_consistency_review_retry' if review is not None and review['verdict'] == 'retry_causal_draft' else 'resource_consistency_review_unavailable'
                deferred_token_usage = None
                if accepted and defer_accepted_usage and usage:
                    deferred_token_usage = _DeferredMainExpeditionTokenUsage(call_type='expedition_resolution_ai', usage=usage, model=metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_no)
                    deferred_token_usage.set_response_diagnostics(finish_reason=parsed_json.finish_reason, json_object_mode=metadata.get('json_object_mode'), provider_request_id=metadata.get('request_id'), repair_applied=parsed_json.repair_applied, failed_response_excerpt=parsed_json.raw_excerpt)
                    usage_recorded = True
                else:
                    usage_recorded = True
                    record_token_usage(call_type='expedition_resolution_ai', usage=usage, model=metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_no, is_success=accepted, failure_stage=None if accepted else 'downstream_validation', validation_error_code=review_error_code, finish_reason=parsed_json.finish_reason, json_object_mode=metadata.get('json_object_mode'), provider_request_id=metadata.get('request_id'), repair_applied=parsed_json.repair_applied, failed_response_excerpt=None if accepted else parsed_json.raw_excerpt, record_without_usage=not accepted and (not bool(usage)))
                log_s1_ai_response_outcome(resp, feature='expedition_resolution', result_status='json_usable' if accepted else 'rejected_by_resource_consistency_review', finish_reason=parsed_json.finish_reason, error_code=review_error_code, attempt_number=attempt_no, expedition_log_id=expedition_log_id)
                if accepted:
                    if contract_version == 3 and review is not None:
                        resolution['_resource_review'] = deepcopy(review)
                    if deferred_token_usage is not None:
                        return _EventResolutionAIResult(resolution, deferred_token_usage=deferred_token_usage)
                    return resolution
                if review is None:
                    current_app.logger.warning('S1 主剧情因果底稿资源一致性复核不可用 stage=expedition_death_resource_review party=%s log=%s attempt=%s/%s', party_id, expedition_log_id, attempt_no, attempts)
                    _record_s1_retry_decision(retry_trace, stage='expedition_death_resource_review', reason_code='resource_consistency_review_unavailable', action=S1RetryAction.RETURN_STAGE_FAILURE, attempt=attempt_no, max_attempts=attempts, preserves_main_result=None, detail='不可逆或共享资源复核没有形成可消费回执；本阶段返回未确认结果，由调用方按所属失败流程处理，不表示已生成或放行主剧情。')
                    return None
                validation_error_code = 'resource_consistency_review_retry'
                if contract_version == 3:
                    resource_review_feedback = {'events': [{'fact': event['fact'], 'impacts': [{key: impact[key] for key in ('id', 'damage', 'healing', 'terminal') if key in impact} for impact in event['impacts']]} for event in resolution['events']], 'issue_event_fact_indexes': list(review['issue_event_fact_indexes']), 'reviewed_member_ids': list(review['reviewed_member_ids']), 'reason': review['reason']}
                _record_s1_retry_decision(retry_trace, stage='expedition_death_resource_review', reason_code=validation_error_code, action=S1RetryAction.REGENERATE_RESOLUTION if attempt_no < attempts else S1RetryAction.DEFER_AUXILIARY_STATE, attempt=attempt_no, max_attempts=attempts, preserves_main_result=None, detail='复核只要求主剧情因果底稿重做，不直接改写生命终态或减伤数值。')
                if attempt_no >= attempts:
                    return None
                continue
            stable_validation_error_code = str(error or parsed_json.error_code or 'business_validation_failed').split(':', 1)[0].split(' ', 1)[0][:160]
            usage_recorded = True
            record_token_usage(call_type='expedition_resolution_ai', usage=usage, model=metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_no, is_success=False, failure_stage='response_parse' if parsed_json.error_code else 'contract_validation', validation_error_code=stable_validation_error_code, finish_reason=parsed_json.finish_reason, json_object_mode=metadata.get('json_object_mode'), provider_request_id=metadata.get('request_id'), repair_applied=parsed_json.repair_applied, failed_response_excerpt=parsed_json.raw_excerpt, record_without_usage=not bool(usage))
            log_s1_ai_response_outcome(resp, feature='expedition_resolution', result_status='unusable', finish_reason=parsed_json.finish_reason, error_code=parsed_json.error_code or 'business_validation_failed', attempt_number=attempt_no, expedition_log_id=expedition_log_id)
            current_app.logger.warning('S1 AI 主剧情因果底稿失败 stage=expedition_resolution party=%s log=%s attempt=%s/%s error=%s', party_id, expedition_log_id, attempt_no, attempts, error)
            validation_error_code = str(error or 'business_validation_failed')
            _record_s1_retry_decision(retry_trace, stage='expedition_resolution', reason_code=stable_validation_error_code, action=S1RetryAction.DEFER_AUXILIARY_STATE if stable_validation_error_code == 'invalid_resolution_schema' else S1RetryAction.REGENERATE_RESOLUTION, attempt=attempt_no, max_attempts=attempts, preserves_main_result=None, detail=str(error or ''))
            if stable_validation_error_code == 'invalid_resolution_schema':
                return None
        except (AIProfileAssignmentError, requests.exceptions.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            if not usage_recorded:
                failure_stage = 'route_assignment' if isinstance(exc, AIProfileAssignmentError) else 'http_or_response' if isinstance(exc, requests.exceptions.RequestException) else 'response_parse'
                usage_recorded = True
                record_token_usage(call_type='expedition_resolution_ai', usage=usage, model=metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_no, is_success=False, failure_stage=failure_stage, validation_error_code=f'{failure_stage}.{type(exc).__name__}', finish_reason=parsed_json.finish_reason if parsed_json is not None else None, json_object_mode=metadata.get('json_object_mode'), provider_request_id=metadata.get('request_id'), repair_applied=parsed_json.repair_applied if parsed_json is not None else False, failed_response_excerpt=parsed_json.raw_excerpt if parsed_json is not None else None, record_without_usage=not bool(usage), request_metadata=metadata, request_exception=exc)
            current_app.logger.warning('S1 AI 主剧情因果底稿调用失败 stage=expedition_resolution party=%s log=%s attempt=%s/%s error=%s detail=%s', party_id, expedition_log_id, attempt_no, attempts, type(exc).__name__, str(exc)[:500])
            if isinstance(exc, requests.RequestException) and (not is_s1_ai_retryable_request_exception(exc)):
                return None
            _record_s1_retry_decision(retry_trace, stage='expedition_resolution', reason_code=f'call_{type(exc).__name__}', action=S1RetryAction.RETRY_SAME_STAGE, attempt=attempt_no, max_attempts=attempts, preserves_main_result=None, detail=str(exc))
    current_app.logger.warning('S1 AI 主剧情因果底稿耗尽重试，未形成已确认底稿，返回失败结果 party=%s log=%s', party_id, expedition_log_id)
    return None
def _measure_s1_player_visible_story(content: str, result_data: dict | None) -> tuple[int, int]:
    """统计玩家可见正文字符与可推送段落；纯 JSON 兜底直接统计 events。"""
    cleaned = _strip_markdown_code_fence(content)
    header_pattern = re.compile('^[ \\t]*###[ \\t]*(规则判定|剧情描述|状态更新)[ \\t]*(.*?)$', re.MULTILINE)
    header_matches = list(header_pattern.finditer(cleaned))
    raw_story = ''
    for index, match in enumerate(header_matches):
        if match.group(1) != '剧情描述':
            continue
        inline_content = match.group(2).strip()
        start = match.end()
        end = header_matches[index + 1].start() if index + 1 < len(header_matches) else len(cleaned)
        section_text = cleaned[start:end].strip()
        raw_story = f'{inline_content}\n{section_text}'.strip() if inline_content else section_text
        break
    if raw_story.strip():
        raw_paragraphs = [paragraph.strip() for paragraph in re.split('\\n\\s*\\n', raw_story) if paragraph.strip()]
        return (len(re.sub('\\s+', '', raw_story)), len(raw_paragraphs))
    events = result_data.get('events') if isinstance(result_data, dict) and isinstance(result_data.get('events'), list) else []
    visible_events = [event for event in events if isinstance(event, str) and event.strip()]
    return (sum((len(re.sub('\\s+', '', event)) for event in visible_events)), len(visible_events))
class _DeferredMainExpeditionTokenUsage:
    """Delay one locally valid expedition usage row until authoritative consumption."""

    def __init__(self, *, call_type: str='expedition_ai', usage: dict | None, model: str, party_id: int | None, expedition_log_id: int | None, attempt_number: int, request_metadata: dict | None=None) -> None:
        self._call_type = str(call_type or 'expedition_ai')[:50]
        self._usage = usage
        self._model = model
        self._party_id = party_id
        self._expedition_log_id = expedition_log_id
        self._attempt_number = attempt_number
        self._request_metadata = dict(request_metadata) if request_metadata is not None else None
        self._finish_reason = None
        self._json_object_mode = None
        self._provider_request_id = None
        self._repair_applied = False
        self._failed_response_excerpt = None
        self._reasoning_first_sentence = None
        self._downstream_failure_stage: str | None = None
        self._downstream_error_code: str | None = None
        self._finalized = False

    def set_model(self, model: str) -> None:
        if not self._finalized and model:
            self._model = model

    def set_response_diagnostics(self, *, finish_reason: object=None, json_object_mode: object=None, provider_request_id: object=None, repair_applied: bool=False, failed_response_excerpt: object=None, reasoning_first_sentence: object=None, request_metadata: dict | None=None) -> None:
        if self._finalized:
            return
        if finish_reason is not None:
            self._finish_reason = finish_reason
        if isinstance(json_object_mode, bool):
            self._json_object_mode = json_object_mode
        if provider_request_id is not None:
            self._provider_request_id = provider_request_id
        self._repair_applied = bool(repair_applied)
        if failed_response_excerpt is not None:
            self._failed_response_excerpt = failed_response_excerpt
        if reasoning_first_sentence is not None:
            self._reasoning_first_sentence = reasoning_first_sentence
        if request_metadata is not None:
            self._request_metadata = dict(request_metadata)

    def set_downstream_failure_diagnostics(self, *, failure_stage: str, validation_error_code: str) -> None:
        if self._finalized:
            return
        self._downstream_failure_stage = str(failure_stage or '')[:80] or None
        self._downstream_error_code = str(validation_error_code or '')[:160] or None

    def finalize(self, *, is_success: bool, failure_stage: str | None=None, validation_error_code: str | None=None) -> None:
        if self._finalized:
            return
        self._finalized = True
        final_failure_stage = None if is_success else failure_stage or self._downstream_failure_stage or 'downstream_consumption'
        final_error_code = None if is_success else validation_error_code or self._downstream_error_code or 'pipeline_not_consumed'
        from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
        record_token_usage(call_type=self._call_type, usage=self._usage, model=self._model, party_id=self._party_id, expedition_log_id=self._expedition_log_id, attempt_number=self._attempt_number, is_success=is_success is True, failure_stage=final_failure_stage, validation_error_code=final_error_code, finish_reason=self._finish_reason, json_object_mode=self._json_object_mode, provider_request_id=self._provider_request_id, repair_applied=self._repair_applied, reasoning_first_sentence=self._reasoning_first_sentence, failed_response_excerpt=None if is_success else self._failed_response_excerpt, request_metadata=self._request_metadata, record_without_usage=True)
class _MainExpeditionAIResult(dict):
    """Carry server-owned input context and usage outside model JSON."""

    def __init__(self, value: dict, *, deferred_token_usage: _DeferredMainExpeditionTokenUsage | None=None, trait_context_receipt: dict | None=None, event_resolution: dict | None=None) -> None:
        super().__init__(value)
        self.deferred_token_usage = deferred_token_usage
        self.trait_context_receipt = _clone_s1_generation_value(trait_context_receipt)
        self.event_resolution = _clone_s1_generation_value(event_resolution)
class _EventResolutionAIResult(dict):
    """Carry the accepted resolution usage until its fenced checkpoint commits."""

    def __init__(self, value: dict, *, deferred_token_usage: _DeferredMainExpeditionTokenUsage) -> None:
        super().__init__(value)
        self.deferred_token_usage = deferred_token_usage
def _call_main_expedition_ai(members, item_dict, target_dungeon, dungeon_info, special_event_mode: str='【当前特殊事件类别】：normal', party_id: int | None=None, expedition_log_id: int | None=None, special_event_type: str | None=None, special_event_risk_tier: str | None=None, has_special_event: bool=False, is_extreme_crisis: bool=False, hidden_boss_context: dict | None=None, commission_context: list | None=None, use_condition_by_name: dict[str, dict | None] | None=None, initial_validation_feedback: str | None=None, name_alias: dict[int, str] | None=None, special_dungeon_context: dict | None=None, narrative_seed: dict[str, object] | None=None, skip_length_checks: bool=False, allow_observer_equipment_repairs: bool=False, frozen_objective_outline: dict | None=None, frozen_event_resolution: dict | None=None, event_resolution_regeneration_basis: str | None=None, checkpoint_review_attempt: int=1, generation_claim_token: str | None=None, physical_attempt_guard: Callable[[], None] | None=None, hidden_dungeon_context: dict | None=None, retry_trace: list[dict] | None=None, campaign_dispatch: dict | None=None):
    """调用 DeepSeek 主探险 AI，解析+校验，失败重试。返回 ``(result_data, failure_detail)``。

    成功时 ``failure_detail`` 为 ``None``；耗尽重试后 ``result_data`` 为 ``None``，
    ``failure_detail`` 为最后一次 HTTP/校验/可信度失败原因（供管理员落库，不对玩家展示）。

    重试用 `tenacity.Retrying`：HTTP 异常和业务校验失败自动指数退避 + jitter；其他
    异常（如代码 bug）直接抛出不重试。校验失败时把 errors 拼回 validation_feedback
    供下次 prompt 使用。`special_event_mode` 由出发时掷骰决定，整段重试沿用同一面值。

    party_id / expedition_log_id：可选关联上下文，传入后 token 统计会附带这些 ID。
    """
    main_route = resolve_s1_ai_profile_snapshot('expedition_main')
    api_key = main_route.api_key
    api_base = main_route.api_base
    model = main_route.model
    timeout_floor = _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_AI_TIMEOUT_FLOOR_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS)
    timeout = max(S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS, timeout_floor, _coerce_int(current_app.config.get('ADVENTURER_S1_AI_DEFAULT_TIMEOUT_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_AI_TIMEOUT_SECONDS', S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS), S1_EXPEDITION_AI_TIMEOUT_MIN_SECONDS))
    max_retries = max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_AI_MAX_RETRIES', 3), 3))
    configured_max_output_tokens = _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_AI_MAX_OUTPUT_TOKENS', 0), 0)
    max_output_tokens = configured_max_output_tokens if configured_max_output_tokens > 0 else None
    provider_refusal_max_attempts = max(1, min(max_retries, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_PROVIDER_REFUSAL_MAX_ATTEMPTS', 2), 2)))
    if dungeon_info['max_level'] >= 999:
        level_desc = f"Lv.{dungeon_info['min_level']}+"
    else:
        level_desc = f"Lv.{dungeon_info['min_level']}-{dungeon_info['max_level']}"
    id_to_name = {member.id: member.name for member in members if getattr(member, 'name', None)}
    codename_to_real = build_codename_to_real_from_alias(name_alias, id_to_name)
    is_special_opportunity_dungeon = bool(special_dungeon_context) or is_s1_special_dungeon(target_dungeon)
    high_risk_context = {'has_special_event': has_special_event, 'is_extreme_crisis': is_extreme_crisis, 'special_event_type': special_event_type, 'special_event_risk_tier': special_event_risk_tier, 'hidden_boss_context': hidden_boss_context, 'campaign_dispatch': campaign_dispatch}
    high_risk_adjudication_required = requires_s1_high_risk_adjudication(high_risk_context)
    objective_outline = json.loads(json.dumps(frozen_objective_outline, ensure_ascii=False)) if isinstance(frozen_objective_outline, dict) else None
    should_generate_objective_outline = bool(not is_special_opportunity_dungeon or high_risk_adjudication_required)
    objective_mode_snapshot = None
    if should_generate_objective_outline:
        from plugins.adventurer_s1.services.s1_expedition_objective_mode_service import build_objective_mode_authority_sources
        objective_mode_snapshot = _load_or_create_s1_objective_mode_snapshot(target_dungeon, expedition_log_id=expedition_log_id, authority_sources=build_objective_mode_authority_sources(commission_context=commission_context, hidden_boss_context=hidden_boss_context))
    if objective_outline is None and should_generate_objective_outline:
        if party_id and expedition_log_id:
            _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=[m.id for m in members], resume_stage='objective_outline_ai', review_attempt=checkpoint_review_attempt, generation_claim_token=generation_claim_token, candidate=None)
        objective_outline = _call_s1_objective_outline_ai(members, target_dungeon, level_desc, special_event_mode, special_event_type=special_event_type, special_event_risk_tier=special_event_risk_tier, has_special_event=has_special_event, hidden_boss_context=hidden_boss_context, commission_context=commission_context, special_dungeon_context=special_dungeon_context, party_id=party_id, expedition_log_id=expedition_log_id, hidden_dungeon_context=hidden_dungeon_context, name_alias=name_alias, objective_mode_snapshot=objective_mode_snapshot, physical_attempt_guard=physical_attempt_guard, generation_claim_token=generation_claim_token, item_dict=item_dict, use_condition_by_name=use_condition_by_name)
        _persist_objective_outline_to_log(expedition_log_id, objective_outline, generation_claim_token=generation_claim_token)
        if party_id and expedition_log_id and objective_outline:
            _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=[m.id for m in members], resume_stage='main_ai', review_attempt=checkpoint_review_attempt, generation_claim_token=generation_claim_token, objective_outline=objective_outline, candidate=None)
    objective_outline_available = bool(isinstance(objective_outline, dict) and isinstance(objective_outline.get('objective_situation'), str) and objective_outline['objective_situation'].strip())
    if should_generate_objective_outline and (not objective_outline_available):
        failure_detail = '客观大纲生成失败或缺失，已终止本次探险生成'
        current_app.logger.warning('S1 客观大纲不可用，终止本次探险生成 party=%s log=%s special_event_type=%s', party_id, expedition_log_id, special_event_type)
        _record_s1_retry_decision(retry_trace, stage='objective_outline', reason_code='objective_outline_unavailable', action=S1RetryAction.FAIL_AND_DISBAND, attempt=1, max_attempts=1, preserves_main_result=False, detail=failure_detail)
        return (None, failure_detail)
    if objective_outline is None:
        objective_outline = {}
    from plugins.adventurer_s1.services.s1_temporary_location_service import expedition_asset_dungeon, temporary_location_from_context, resolve_outline_temporary_location
    objective_outline = resolve_outline_temporary_location(expedition_log_id, target_dungeon, objective_outline, generation_claim_token=generation_claim_token)
    asset_dungeon = expedition_asset_dungeon(objective_outline, target_dungeon)
    is_temporary_location = temporary_location_from_context(objective_outline) is not None
    member_ids = [m.id for m in members]
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    validation_feedback = initial_validation_feedback or ''
    from plugins.adventurer_s1.services.s1_item_use_condition_service import build_expedition_equipment_context
    if use_condition_by_name is None:
        _, use_condition_by_name = build_expedition_equipment_context(members)
    from plugins.adventurer_s1.services.s1_encounter_draft_contract import restore_compact_draft
    event_resolution = restore_compact_draft(frozen_event_resolution, members)
    reused_frozen_draft = event_resolution is not None and event_resolution_regeneration_basis not in {'event_resolution_invalid', 'persistent_content_violation'}
    from plugins.adventurer_s1.services.s1_trait_context_projection_service import build_s1_trait_context_projection
    frozen_trait_context_receipt = _load_s1_trait_context_selection_receipt(expedition_log_id)
    trait_projection_members = members
    from plugins.adventurer_s1.services.s1_capability_profile_contract import ENCOUNTER_DEFINITION_FIELD, project_encounter_definition_for_ai
    if frozen_trait_context_receipt is not None:
        trait_projection_members = restore_frozen_member_description_projection(members, frozen_trait_context_receipt.get('description_projection'))
    elif project_encounter_definition_for_ai(objective_outline):
        trait_projection_members = restore_frozen_member_description_projection(members, objective_outline[ENCOUNTER_DEFINITION_FIELD].get('member_description_projection'))
    else:
        trait_projection_members = refresh_frozen_member_description_ai_context(members)
        if trait_projection_members is not members:
            current_app.logger.info('S1 客观大纲期间完成的摘要与路由已补入冻结快照 party=%s log=%s', party_id, expedition_log_id)
    trait_projection = build_s1_trait_context_projection(trait_projection_members, objective_outline=objective_outline, target_dungeon=asset_dungeon, independent_location=is_temporary_location or bool(campaign_world(target_dungeon)), frozen_event_resolution=event_resolution, frozen_receipt=frozen_trait_context_receipt)
    try:
        trait_projection.receipt['description_projection'] = build_frozen_member_description_projection_receipt(trait_projection.members, item_dict=item_dict, target_dungeon=asset_dungeon, use_condition_by_name=use_condition_by_name)
    except Exception as exc:
        trait_projection.receipt['description_projection'] = {'schema_version': 1, 'unavailable': type(exc).__name__}
        current_app.logger.warning('S1 成员摘要输入诊断不可用 log=%s error=%s', expedition_log_id, type(exc).__name__)
    if (not trait_projection.reused_frozen_receipt or trait_projection.receipt != frozen_trait_context_receipt) and (not _persist_s1_trait_context_selection_receipt(expedition_log_id, trait_projection.receipt, generation_claim_token=generation_claim_token, expected_receipt=frozen_trait_context_receipt)):
        trait_projection = build_s1_trait_context_projection(trait_projection_members, objective_outline=objective_outline, target_dungeon=asset_dungeon, independent_location=is_temporary_location or bool(campaign_world(target_dungeon)), frozen_event_resolution=event_resolution, mode_override='off')
        trait_projection.receipt['freeze_fallback'] = True
        try:
            trait_projection.receipt['description_projection'] = build_frozen_member_description_projection_receipt(trait_projection.members, item_dict=item_dict, target_dungeon=asset_dungeon, use_condition_by_name=use_condition_by_name)
        except Exception as exc:
            trait_projection.receipt['description_projection'] = {'schema_version': 2, 'unavailable': type(exc).__name__}
    ai_members = trait_projection.members
    countermeasure_context_selection = _resolve_s1_countermeasure_context_selection(ai_members, item_dict, target_dungeon=asset_dungeon, use_condition_by_name=use_condition_by_name, objective_outline=objective_outline, party_id=party_id, expedition_log_id=expedition_log_id, generation_claim_token=generation_claim_token, physical_attempt_guard=physical_attempt_guard)
    member_context = _build_s1_member_validation_context(ai_members, asset_dungeon, use_condition_by_name, item_dict)
    member_source_names_by_id = {int(mid): {kind: set(context.get(field) or ()) for kind, field in (('equipment', 'equipment_names'), ('skill', 'skill_names'), ('trait', 'trait_names'))} for mid, context in member_context.items()}
    description_feedback_catalog = None
    if high_risk_adjudication_required and event_resolution is None:

        def save_resolution_checkpoint(stage, resolution):
            if not party_id or not expedition_log_id:
                return
            if not _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_ids, resume_stage=stage, review_attempt=checkpoint_review_attempt, generation_claim_token=generation_claim_token, objective_outline=objective_outline, event_resolution=resolution, candidate=None, trait_context_receipt=trait_projection.receipt, event_resolution_regeneration_basis=event_resolution_regeneration_basis, retry_trace=retry_trace):
                raise RuntimeError('compact_draft_checkpoint_not_persisted')
        save_resolution_checkpoint('event_resolution_ai', None)
        description_feedback_catalog = {}
        combat_authority_blocks = []
        resolution_prompt = build_s1_expedition_resolution_input_prompt(ai_members, item_dict, target_dungeon, level_desc, dungeon_info['min_level'], dungeon_info['max_level'], description_feedback_catalog=description_feedback_catalog, special_event_mode=special_event_mode, hidden_boss_context=hidden_boss_context, use_condition_by_name=use_condition_by_name, has_special_event=has_special_event, is_extreme_crisis=is_extreme_crisis, special_event_type=special_event_type, commission_context=commission_context, special_event_risk_tier=special_event_risk_tier, name_alias=name_alias, special_dungeon_context=special_dungeon_context, objective_outline=objective_outline, objective_mode_snapshot=objective_mode_snapshot, expedition_log_id=expedition_log_id, countermeasure_context_selection=countermeasure_context_selection, combat_authority_blocks_out=combat_authority_blocks)
        feedback = _build_s1_event_resolution_regeneration_feedback(event_resolution_regeneration_basis)
        if feedback:
            resolution_prompt += '\n\n' + feedback
        resolution_route = resolve_s1_ai_profile_snapshot('expedition_resolution')
        event_resolution = _call_s1_expedition_event_resolution_ai(user_prompt=resolution_prompt, member_ids=member_ids, member_max_hp_by_id={m.id: m.max_hp for m in members}, member_current_hp_by_id={m.id: m.hp for m in members}, member_source_names_by_id=member_source_names_by_id, allowed_pressure_indices=set(), api_base=resolution_route.api_base, headers={'Authorization': f'Bearer {resolution_route.api_key}', 'Content-Type': 'application/json'}, model=resolution_route.model, timeout=timeout, max_retries=max_retries, party_id=party_id, expedition_log_id=expedition_log_id, retry_trace=retry_trace, members=ai_members, name_alias=name_alias, physical_attempt_guard=physical_attempt_guard, contract_version=3, special_event_type=resolve_s1_special_event_kind(high_risk_context), defer_accepted_usage=True, resource_review_prompt_factory=lambda ids: build_s1_expedition_resource_review_input_prompt(ai_members, item_dict, target_dungeon, ids, use_condition_by_name=use_condition_by_name, name_alias=name_alias, combat_authority_blocks=combat_authority_blocks))
        if event_resolution is None:
            return (None, '高危交锋推演未形成已确认结果')
        usage_handle = getattr(event_resolution, 'deferred_token_usage', None)
        try:
            event_resolution = restore_compact_draft(event_resolution, members)
            save_resolution_checkpoint('main_ai', event_resolution)
        except Exception:
            if usage_handle is not None:
                usage_handle.finalize(is_success=False, failure_stage='downstream_validation', validation_error_code='compact_draft_checkpoint_not_persisted')
            raise
        if usage_handle is not None:
            usage_handle.finalize(is_success=True)
    if event_resolution and event_resolution.get('asset_description_feedback'):
        try:
            from plugins.adventurer_s1.services.s1_description_feedback_service import enqueue_description_feedback
            if description_feedback_catalog is None:
                from plugins.adventurer_s1.services.adventurer_s1_prompt_service import build_s1_member_block
                description_feedback_catalog = {}
                for member in ai_members:
                    build_s1_member_block(member, item_dict, target_dungeon=target_dungeon, use_condition_by_name=use_condition_by_name, include_companion_lineage=False, include_settlement_refs=False, description_feedback_catalog=description_feedback_catalog)
            enqueue_description_feedback(event_resolution['asset_description_feedback'], description_feedback_catalog, expedition_log_id=expedition_log_id, member_id_to_name={m.id: m.name for m in ai_members})
        except Exception:
            db_manager.rollback()
            current_app.logger.exception('S1 底稿描述反馈入队失败，继续本次探险 log=%s', expedition_log_id)
    retryer = Retrying(stop=stop_after_attempt(max_retries), wait=wait_exponential_jitter(initial=1, max=10), retry=retry_if_exception(lambda exc: isinstance(exc, _S1ExpeditionRetryable) or is_s1_ai_retryable_request_exception(exc)), reraise=False)
    last_failure_detail: str | None = None
    pending_usage_handles: list[_DeferredMainExpeditionTokenUsage] = []
    provider_refusal_count = 0
    main_resolution_reference = _select_s1_main_resolution_reference(event_resolution, observer_feedback=initial_validation_feedback, reused_frozen_draft=reused_frozen_draft)
    from .adventurer_s1_prompt_service import parse_s1_narrative_repair_context
    narrative_repair_context = parse_s1_narrative_repair_context(initial_validation_feedback, member_ids)

    def validate_main_expedition_route_response(decoded) -> S1AiResponseContractResult:
        extracted_candidate = decoded.http_result.content_result
        if extracted_candidate.error_code:
            return S1AiResponseContractResult.reject(extracted_candidate.error_code, failure_stage='response_parse')
        if extracted_candidate.finish_reason == 'length':
            return S1AiResponseContractResult.reject('truncated_output', failure_stage='response_parse')
        candidate_content = extracted_candidate.content
        if narrative_repair_context is not None:
            from .adventurer_s1_parser_service import materialize_s1_narrative_repair_response
            candidate_content, repair_error = materialize_s1_narrative_repair_response(candidate_content, narrative_repair_context['candidate']['events'])
            if repair_error:
                return S1AiResponseContractResult.reject(repair_error, failure_stage='response_parse')
        candidate_content, _ = normalize_s1_expedition_response_format(candidate_content)
        candidate_result = parse_s1_expedition_response(candidate_content)
        from .s1_independent_dungeon_service import project_independent_dungeon_result
        project_independent_dungeon_result(candidate_result, target_dungeon)
        if candidate_result is None:
            parse_diagnostics: dict = {}
            parse_failure_code = diagnose_s1_expedition_response_parse_failure(candidate_content, diagnostics_out=parse_diagnostics)
            return S1AiResponseContractResult.reject(parse_failure_code, failure_stage='response_parse', diagnostics=parse_diagnostics)
        raw_story_char_count, raw_paragraph_count = _measure_s1_player_visible_story(candidate_content, candidate_result)
        if codename_to_real:
            candidate_result = _restore_codenames_in_contract_payload(candidate_result, codename_to_real)
        life_adjustment_errors: list[str] = []
        if event_resolution:
            if int(event_resolution.get('resolution_contract_version') or 1) < 2:
                _discard_s1_model_frozen_life_fields(candidate_result)
            member_update_errors = normalize_s1_member_updates_by_id(candidate_result, member_ids)
            if not member_update_errors:
                life_adjustment_errors = _validate_s1_expedition_life_adjustments(candidate_result, event_resolution, {int(member.id): max(1, _coerce_int(getattr(member, 'max_hp', 0), 1)) for member in members}, member_source_names_by_id)
        is_valid, errors, _ = validate_s1_expedition_result(candidate_result, member_ids, member_context=member_context, raw_paragraph_count=raw_paragraph_count, raw_story_char_count=raw_story_char_count, skip_length_checks=skip_length_checks, allow_observer_equipment_repairs=allow_observer_equipment_repairs, allow_observer_death_reason_repairs=True, allow_observer_gold_settlement_repairs=True, commission_context=commission_context or [], allow_observer_commission_repairs=True, countermeasure_learning_enabled=False, require_gold_settlement=True, require_member_scores=True, allow_observer_score_repairs=True)
        if event_resolution and life_adjustment_errors:
            if is_valid:
                observer_repairable = set(get_s1_observer_repair_errors(life_adjustment_errors))
                non_repairable = [error for error in life_adjustment_errors if error not in observer_repairable]
                if non_repairable:
                    is_valid = False
                    errors.extend(non_repairable)
            else:
                errors.extend(life_adjustment_errors)
        if is_valid:
            return S1AiResponseContractResult.accept()
        return S1AiResponseContractResult.reject('business_validation_failed', failure_stage='contract_validation', diagnostics={'errors': list(errors)})
    from plugins.adventurer_s1.services.s1_cross_realm_mirror_service import prepare_cross_realm_mirror_injection
    prepare_cross_realm_mirror_injection(expedition_log_id, target_dungeon, eligible=not bool(special_dungeon_context) and (not bool(hidden_dungeon_context)) and (not bool(hidden_boss_context)) and (not has_special_event) and (not is_s1_special_dungeon(target_dungeon)))
    try:
        for attempt_ctx in retryer:
            with attempt_ctx:
                attempt_no = attempt_ctx.retry_state.attempt_number
                system_prompt, user_prompt = build_s1_expedition_prompts(ai_members, item_dict, target_dungeon, level_desc, dungeon_info['min_level'], dungeon_info['max_level'], validation_feedback='' if narrative_repair_context is not None and validation_feedback == initial_validation_feedback else validation_feedback, narrative_repair_context=narrative_repair_context, special_event_mode=special_event_mode, hidden_boss_context=hidden_boss_context, hidden_dungeon_context=hidden_dungeon_context, commission_context=commission_context, use_condition_by_name=use_condition_by_name, has_special_event=has_special_event, is_extreme_crisis=is_extreme_crisis, special_event_type=special_event_type, special_event_risk_tier=special_event_risk_tier, name_alias=name_alias, special_dungeon_context=special_dungeon_context, narrative_seed=narrative_seed, objective_outline=objective_outline, objective_mode_snapshot=objective_mode_snapshot, event_resolution=main_resolution_reference, codename_to_real_out=codename_to_real, countermeasure_context_selection=countermeasure_context_selection, party_id=party_id, expedition_log_id=expedition_log_id, include_narrative_quality_requirements=not skip_length_checks, include_current_hp=True)
                trait_projection.receipt['main_prompt'] = build_s1_expedition_prompt_receipt(system_prompt, user_prompt, attempt=attempt_no)
                resp_usage = None
                deferred_token_usage = None
                resp_metadata: dict[str, str] = {}
                finish_reason: str | None = None
                try:
                    db_manager.remove()
                    resp = post_s1_ai_request(f'{api_base}/chat/completions', feature='expedition_main', resolved_profile=main_route, json_object_mode_if_profile_enabled=True, output_token_limit=max_output_tokens, request_metadata_out=resp_metadata, physical_attempt_guard=physical_attempt_guard, headers=headers, json={'model': model, 'messages': [{'role': 'system', 'content': system_prompt}, {'role': 'user', 'content': user_prompt}], 'temperature': 0.3}, timeout=timeout, response_validator=validate_main_expedition_route_response)
                    decoded = decode_s1_ai_http_response(resp)
                    resp_json = decoded.response_json or {}
                    reasoning_first_sentence = extract_s1_ai_reasoning_first_sentence(resp_json)
                    resp_usage = decoded.usage
                    deferred_token_usage = _DeferredMainExpeditionTokenUsage(usage=resp_usage, model=model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_no, request_metadata=resp_metadata or None)
                    pending_usage_handles.append(deferred_token_usage)
                    enrich_s1_ai_response_metadata(resp, resp_json)
                    resp_metadata = get_s1_ai_response_metadata(resp)
                    if deferred_token_usage is not None:
                        deferred_token_usage.set_model(resp_metadata.get('model') or model)
                        deferred_token_usage.set_response_diagnostics(json_object_mode=resp_metadata.get('json_object_mode'), provider_request_id=resp_metadata.get('request_id'), reasoning_first_sentence=reasoning_first_sentence, request_metadata=resp_metadata)
                    extracted = decoded.content_result
                    finish_reason = extracted.finish_reason
                    structured_refusal = extract_s1_ai_structured_refusal(resp_json)
                    response_excerpt = extracted.raw_excerpt or build_s1_ai_raw_excerpt(structured_refusal or '')
                    if deferred_token_usage is not None:
                        deferred_token_usage.set_response_diagnostics(finish_reason=finish_reason, json_object_mode=resp_metadata.get('json_object_mode'), provider_request_id=resp_metadata.get('request_id'), failed_response_excerpt=response_excerpt)
                    if finish_reason == 'content_filter' or structured_refusal is not None:
                        provider_refusal_count += 1
                        last_failure_detail = '上游模型拒绝生成主探险候选（provider_refusal）'
                        if deferred_token_usage is not None:
                            deferred_token_usage.finalize(is_success=False, failure_stage='provider_response', validation_error_code='provider_refusal')
                        else:
                            from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
                            record_token_usage(call_type='expedition_ai', usage=resp_usage, model=resp_metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_no, is_success=False, failure_stage='provider_response', validation_error_code='provider_refusal', finish_reason=finish_reason, json_object_mode=resp_metadata.get('json_object_mode'), provider_request_id=resp_metadata.get('request_id'), reasoning_first_sentence=reasoning_first_sentence, failed_response_excerpt=response_excerpt, record_without_usage=True, request_metadata=resp_metadata)
                        log_s1_ai_response_outcome(resp, feature='expedition_main', result_status='unusable', finish_reason=finish_reason, error_code='provider_refusal', attempt_number=attempt_no, expedition_log_id=expedition_log_id)
                        should_retry_refusal = resp_metadata.get('api_key_slot') != 'backup' and provider_refusal_count < provider_refusal_max_attempts and (attempt_no < max_retries)
                        refusal_attempt_limit = provider_refusal_max_attempts if resp_metadata.get('api_key_slot') != 'backup' else provider_refusal_count
                        _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code='provider_refusal', action=S1RetryAction.RETRY_SAME_STAGE if should_retry_refusal else S1RetryAction.FAIL_AND_DISBAND, attempt=provider_refusal_count, max_attempts=refusal_attempt_limit, preserves_main_result=False, detail=last_failure_detail)
                        if should_retry_refusal:
                            raise _S1ExpeditionRetryable(last_failure_detail)
                        return (None, last_failure_detail)
                    if extracted.error_code or finish_reason == 'length':
                        error_code = 'truncated_output' if finish_reason == 'length' else extracted.error_code
                        if finish_reason == 'length':
                            completion_tokens = None
                            if isinstance(resp_usage, dict):
                                completion_tokens = resp_usage.get('completion_tokens') if resp_usage.get('completion_tokens') is not None else resp_usage.get('output_tokens')
                            response_model = str(resp_metadata.get('response_model') or resp_json.get('model') or '').strip()
                            last_failure_detail = '; '.join(['主探险 AI 输出被长度上限截断', f"profile_id={resp_metadata.get('profile_id') or 'unknown'}", f"provider={resp_metadata.get('provider') or 'unknown'}", f"route_model={resp_metadata.get('model') or model or 'unknown'}", f"response_model={response_model or 'unknown'}", f"requested_max_tokens={resp_metadata.get('output_token_limit') or max_output_tokens or 'unset'}", f"completion_tokens={(completion_tokens if completion_tokens is not None else 'unknown')}"])
                            current_app.logger.warning('S1 主探险输出截断 party=%s log=%s attempt=%s/%s profile=%s provider=%s route_model=%s response_model=%s requested_max_tokens=%s completion_tokens=%s', party_id, expedition_log_id, attempt_no, max_retries, resp_metadata.get('profile_id') or 'unknown', resp_metadata.get('provider') or 'unknown', resp_metadata.get('model') or model or 'unknown', response_model or 'unknown', resp_metadata.get('output_token_limit') or max_output_tokens or 'unset', completion_tokens if completion_tokens is not None else 'unknown')
                        else:
                            last_failure_detail = f"响应内容不可用: {error_code} {extracted.error_detail or ''}".strip()
                        if deferred_token_usage is not None:
                            deferred_token_usage.finalize(is_success=False, failure_stage='response_parse', validation_error_code=error_code or 'unusable_response')
                        log_s1_ai_response_outcome(resp, feature='expedition_main', result_status='unusable', finish_reason=finish_reason, error_code=error_code, attempt_number=attempt_no, expedition_log_id=expedition_log_id)
                        _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code=error_code or 'unusable_response', action=S1RetryAction.RETRY_SAME_STAGE, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=False, detail=last_failure_detail)
                        raise _S1ExpeditionRetryable(last_failure_detail)
                    content = extracted.content
                except S1AiInputBudgetError as e:
                    from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
                    _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code=e.code, action=S1RetryAction.FAIL_AND_DISBAND, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=False, detail=str(e))
                    record_token_usage(call_type='expedition_ai', usage=None, model=resp_metadata.get('model') or model, expedition_log_id=expedition_log_id, party_id=party_id, is_success=False, failure_stage='input_budget', validation_error_code=e.code, request_metadata=resp_metadata, record_without_usage=True)
                    return (None, str(e))
                except requests.exceptions.RequestException as e:
                    if deferred_token_usage is not None:
                        deferred_token_usage.finalize(is_success=False, failure_stage='http_or_response', validation_error_code=f'http_or_response.{type(e).__name__}')
                    else:
                        from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
                        record_token_usage(call_type='expedition_ai', usage=resp_usage, model=resp_metadata.get('model') or model, party_id=party_id, expedition_log_id=expedition_log_id, attempt_number=attempt_no, is_success=False, failure_stage='http_or_response', validation_error_code=f'http_or_response.{type(e).__name__}', record_without_usage=True, request_metadata=resp_metadata, request_exception=e)
                    last_failure_detail = f'HTTP 请求失败: {e}'
                    current_app.logger.warning('S1 AI 调用失败 stage=expedition_main party=%s log=%s attempt=%s/%s error=%s detail=%s', party_id, expedition_log_id, attempt_no, max_retries, type(e).__name__, str(e)[:500])
                    if attempt_no < max_retries and is_s1_ai_retryable_request_exception(e):
                        _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code=f'http_{type(e).__name__}', action=S1RetryAction.RETRY_SAME_STAGE, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=False, detail=str(e))
                    raise
                except (KeyError, IndexError, TypeError, ValueError) as e:
                    if deferred_token_usage is not None:
                        deferred_token_usage.finalize(is_success=False, failure_stage='response_parse', validation_error_code='response_structure_error')
                    last_failure_detail = f'响应结构异常: {e}'
                    current_app.logger.warning('S1 AI 响应结构失败 stage=expedition_main party=%s log=%s attempt=%s/%s error=%s detail=%s', party_id, expedition_log_id, attempt_no, max_retries, type(e).__name__, str(e)[:500])
                    _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code='response_structure_error', action=S1RetryAction.RETRY_SAME_STAGE, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=False, detail=str(e))
                    raise _S1ExpeditionRetryable(f'响应结构异常: {e}') from e
                if narrative_repair_context is not None:
                    from .adventurer_s1_parser_service import materialize_s1_narrative_repair_response
                    content, repair_error = materialize_s1_narrative_repair_response(content, narrative_repair_context['candidate']['events'])
                    if repair_error:
                        raise _S1ExpeditionRetryable(repair_error)
                content, format_repair_code = normalize_s1_expedition_response_format(content)
                format_repair_applied = format_repair_code is not None
                if format_repair_applied:
                    if deferred_token_usage is not None:
                        deferred_token_usage.set_response_diagnostics(repair_applied=True)
                    _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code=format_repair_code, action=S1RetryAction.REPAIR_LOCAL, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=True, detail='仅规范化分段标题或消费结果唯一的单个 JSON 括号')
                result_data = parse_s1_expedition_response(content)
                from .s1_independent_dungeon_service import project_independent_dungeon_result
                project_independent_dungeon_result(result_data, target_dungeon)
                if result_data is None:
                    parse_failure_code = diagnose_s1_expedition_response_parse_failure(content)
                    parse_retry_feedback = get_s1_expedition_parse_retry_feedback(parse_failure_code)
                    last_failure_detail = f'响应无法解析为完整正文与状态分段或合法状态 JSON：{parse_failure_code}'
                    if deferred_token_usage is not None:
                        deferred_token_usage.finalize(is_success=False, failure_stage='response_parse', validation_error_code=parse_failure_code)
                    log_s1_ai_response_outcome(resp, feature='expedition_main', result_status='unusable', finish_reason=finish_reason, repair_applied=format_repair_applied, error_code=parse_failure_code, attempt_number=attempt_no, expedition_log_id=expedition_log_id)
                    parse_errors = [f'{parse_failure_code}: {parse_retry_feedback}']
                    validation_feedback = build_s1_retry_validation_feedback(parse_errors, member_context)
                    _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code=parse_failure_code, action=S1RetryAction.REGENERATE_MAIN, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=False, detail=last_failure_detail)
                    raise _S1ExpeditionRetryable(last_failure_detail)
                raw_story_char_count, raw_paragraph_count = _measure_s1_player_visible_story(content, result_data)
                codename_anchor_errors = validate_s1_expedition_codename_anchors(result_data, name_alias)
                history_anchor_errors = [error for error in codename_anchor_errors if error.startswith(S1_MEMBER_HISTORY_ANCHOR_ERROR_MARKER + ':')]
                codename_anchor_errors = [error for error in codename_anchor_errors if error not in history_anchor_errors]
                if codename_to_real:
                    result_data = _restore_codenames_in_contract_payload(result_data, codename_to_real)
                contract_repair_paths: list[str] = []
                life_adjustment_errors: list[str] = []
                if event_resolution:
                    if int(event_resolution.get('resolution_contract_version') or 1) < 2:
                        _discard_s1_model_frozen_life_fields(result_data)
                    member_update_normalization_errors = normalize_s1_member_updates_by_id(result_data, member_ids, repair_diagnostics=contract_repair_paths)
                    if not member_update_normalization_errors:
                        life_adjustment_errors = _validate_s1_expedition_life_adjustments(result_data, event_resolution, {int(member.id): max(1, _coerce_int(getattr(member, 'max_hp', 0), 1)) for member in members}, member_source_names_by_id)
                is_valid, errors, soft_errors = validate_s1_expedition_result(result_data, member_ids, member_context=member_context, raw_paragraph_count=raw_paragraph_count, raw_story_char_count=raw_story_char_count, skip_length_checks=skip_length_checks, allow_observer_equipment_repairs=allow_observer_equipment_repairs, allow_observer_death_reason_repairs=True, allow_observer_gold_settlement_repairs=True, commission_context=commission_context or [], allow_observer_commission_repairs=True, countermeasure_learning_enabled=False, require_gold_settlement=True, repair_diagnostics=contract_repair_paths, require_member_scores=True, allow_observer_score_repairs=True)
                if codename_anchor_errors:
                    is_valid = False
                    errors = [*codename_anchor_errors, *errors]
                contract_repair_applied = bool(contract_repair_paths)
                response_repair_applied = format_repair_applied or contract_repair_applied
                if contract_repair_applied:
                    if deferred_token_usage is not None:
                        deferred_token_usage.set_response_diagnostics(repair_applied=True)
                    _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code=S1_NEW_EQUIPMENT_LIST_REPAIR_CODE, action=S1RetryAction.REPAIR_LOCAL, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=True, detail='; '.join(contract_repair_paths[:10]))
                from plugins.adventurer_s1.services.s1_waiting_player_npc_service import load_waiting_player_npc_injection, validate_waiting_npc_updates_strict
                waiting_npc_updates, waiting_npc_errors = validate_waiting_npc_updates_strict(result_data.get('waiting_npc_updates') if isinstance(result_data, dict) else None, snapshot=load_waiting_player_npc_injection(expedition_log_id), events=result_data.get('events') if isinstance(result_data, dict) else None)
                if waiting_npc_errors:
                    soft_errors.extend((f'waiting_npc_updates: {message}' for message in waiting_npc_errors))
                elif isinstance(result_data, dict):
                    result_data['waiting_npc_updates'] = waiting_npc_updates
                resolution_review_errors: list[str] = []
                if event_resolution and life_adjustment_errors:
                    if is_valid:
                        observer_life_errors = get_s1_observer_repair_errors(life_adjustment_errors)
                        non_repairable_life_errors = [error for error in life_adjustment_errors if error not in observer_life_errors]
                        resolution_review_errors.extend(observer_life_errors)
                        if non_repairable_life_errors:
                            is_valid = False
                            errors.extend(non_repairable_life_errors)
                    else:
                        errors.extend(life_adjustment_errors)
                if is_valid and event_resolution:
                    resolution_review_errors.extend(_get_s1_expedition_resolution_review_differences(result_data, event_resolution))
                if is_valid:
                    result_data[S1_GOLD_SETTLEMENT_VERSION_FIELD] = S1_GOLD_SETTLEMENT_CONTRACT_VERSION
                    if deferred_token_usage is None:
                        current_app.logger.warning('S1 探险 AI 响应缺少 usage 字段，无法记录 token (party=%s log=%s attempt=%s/%s)', party_id, expedition_log_id, attempt_no, max_retries)
                    log_s1_ai_response_outcome(resp, feature='expedition_main', result_status='usable', finish_reason=finish_reason, repair_applied=response_repair_applied, attempt_number=attempt_no, expedition_log_id=expedition_log_id)
                    observer_repair_errors = [*get_s1_observer_repair_errors(soft_errors), *resolution_review_errors, *[restore_names_in_narrative(error, codename_to_real) for error in history_anchor_errors]]
                    if observer_repair_errors and isinstance(result_data, dict):
                        _record_s1_retry_decision(retry_trace, stage='expedition_main_validation', reason_code='observer_repair_required', action=S1RetryAction.REPAIR_BY_OBSERVER, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=True, detail='; '.join(observer_repair_errors[:10]))
                        result_data['_observer_repair_errors'] = observer_repair_errors
                    if soft_errors:
                        current_app.logger.info('S1 探险结果有 soft errors，交给剧情审核 AI 补正: %s', '; '.join(soft_errors[:3]))
                    current_app.logger.info('S1 AI 主剧情候选生成完成，待剧情审核 (队伍目标: %s)', target_dungeon)
                    result_data = _fix_hash_id_names_in_result(result_data, id_to_name)
                    missing = _detect_missing_name_mentions(result_data, members)
                    if missing:
                        current_app.logger.info('S1 代号还原后仍有 %s 名成员未出现真名: %s', len(missing), missing)
                    result_data['_missing_name_mentions'] = missing
                    if objective_outline:
                        result_data['objective_outline'] = objective_outline
                    result_data = _MainExpeditionAIResult(result_data, deferred_token_usage=deferred_token_usage, trait_context_receipt=trait_projection.receipt, event_resolution=event_resolution)
                    if deferred_token_usage is not None:
                        pending_usage_handles.remove(deferred_token_usage)
                    return (result_data, None)
                last_failure_detail = '; '.join(errors[:10])
                validation_error_code = S1_MEMBER_CODENAME_ANCHOR_ERROR_MARKER if codename_anchor_errors else 'business_validation_failed'
                if deferred_token_usage is not None:
                    deferred_token_usage.finalize(is_success=False, failure_stage='contract_validation', validation_error_code=validation_error_code)
                log_s1_ai_response_outcome(resp, feature='expedition_main', result_status='unusable', finish_reason=finish_reason, repair_applied=response_repair_applied, error_code=validation_error_code, attempt_number=attempt_no, expedition_log_id=expedition_log_id)
                validation_feedback = build_s1_retry_validation_feedback(errors, member_context)
                current_app.logger.warning('S1 AI 输出契约失败 stage=expedition_main party=%s log=%s attempt=%s/%s action=retry_generation error=%s', party_id, expedition_log_id, attempt_no, max_retries, '; '.join(errors[:3]))
                _record_s1_retry_decision(retry_trace, stage='expedition_main', reason_code=validation_error_code, action=S1RetryAction.REGENERATE_MAIN, attempt=attempt_no, max_attempts=max_retries, preserves_main_result=False, detail='; '.join(errors[:10]))
                raise _S1ExpeditionRetryable('校验失败：' + '; '.join(errors[:3]))
    except RetryError as retry_error:
        if not last_failure_detail:
            last_exc = retry_error.last_attempt.exception()
            if last_exc:
                last_failure_detail = str(last_exc)
    finally:
        for usage_handle in pending_usage_handles:
            usage_handle.finalize(is_success=False)
    if not last_failure_detail:
        last_failure_detail = '主探险 AI 多次重试均失败（无详细校验信息）'
    current_app.logger.error('S1 AI 多次重试失败 stage=expedition_main party=%s log=%s target=%s attempts=%s error=%s', party_id, expedition_log_id, target_dungeon, max_retries, last_failure_detail[:1000])
    return (None, last_failure_detail)
_S1_DEPARTURE_TEMPLATES = ('小队集结完毕，正挤上一辆坐垫下会突然弹起来的牛车，每过一个坑就有人被颠得暂时忘了自己姓什么……', '小队集结完毕，大家坐在一艘耐心漏水的木筏上顺流而下，谁也不敢问这筏子到底还能撑几里地……', '小队集结完毕，正在徒步穿越一片会吸住靴底的沼泽，领队坚称「前面就是硬地」，已经说了第四遍……', '小队集结完毕，队长把地图倒拿着带大家找路，居然真走出城门好一段才有人小声提醒方向反了……', '小队集结完毕，正搭乘地精二手飞艇赶路，起飞前三分钟还算体面，第四分钟开始有人认真考虑跳下去……', '小队集结完毕，大家一边赶路一边争论今晚谁负责生火做饭，香味还没冒出来，队形先散了一半……', '小队集结完毕，有人不知从哪牵来一头温顺到可疑的驴，驴走得比全队讨论地牢难度还慢……', '小队集结完毕，正坐着酒馆老板娘拍胸脯保证「绝对靠谱」的马车出城，车夫全程只回头说「放心放心」……', '小队集结完毕，队伍挤进一辆自称豪华的铁皮车厢，豪华之处大概是窗户真能挡风，门也能勉强关上……', '小队集结完毕，领队带大家翻城墙外的矮墙抄近道，翻过去才发现这条近道比大路还崎岖……', '小队集结完毕，有人背着捆得歪歪扭扭的行囊哼着走调小曲开路，其余人假装没听见，脚步倒是跟得挺齐……', '小队集结完毕，正沿着黑黢黢的地下引水渠摸索前进，油灯亮度刚好够照见下一处水渍和再下一处水渍……', '小队集结完毕，大家租了条听着就不太吉利的水路，果然流水很顺，船和船上的人不怎么顺……', '小队集结完毕，两匹价格可疑的瘦马拖着板车吱呀前行，马的眼神清醒得像早就看破了这趟买卖……', '小队集结完毕，队长把集结口令喊了三遍才凑齐人，第四遍时大家已经在商量要不要回酒馆再来一轮……', '小队集结完毕，有人提议比赛谁先追上先头部队，走了一刻钟才发现先头部队就是刚才掉队的那位……', '小队集结完毕，众人踩着吱呀作响的吊桥过城门，桥索一晃，全队不约而同把背包带勒紧了一格……', '小队集结完毕，正贴着城墙根绕巡逻队，有人差点把水壶磕在石头上，声响吓得大家集体屏息十秒……', '小队集结完毕，晨雾里踩着碎石路出城，队长声称记得每一块石头的位置，其实多半靠运气和嘴硬……', '小队集结完毕，大家挤上吱呀作响的摆渡板车过河，车夫表示「掉下去算你们锻炼身体」，没人敢接话……')
def _build_s1_expedition_departure_msg() -> str:
    """出发时随机一次并写入日志，生成期间固定展示，刷新不变。"""
    return random.choice(_S1_DEPARTURE_TEMPLATES)
_S1_DISPATCH_ADVISORY_LOCK_KEY = 910630151
_LOCAL_S1_DISPATCH_LOCK_HANDLE = object()
def _try_acquire_s1_dispatch_lock() -> object | None:
    """跨 worker 自动发车互斥锁；非 PostgreSQL 测试环境直接放行。"""
    db_uri = str(current_app.config.get('SQLALCHEMY_DATABASE_URI') or '')
    if 'postgresql' not in db_uri:
        return _LOCAL_S1_DISPATCH_LOCK_HANDLE
    try:
        from extensions import db
        return acquire_postgresql_advisory_lock(db.engine.url, _S1_DISPATCH_ADVISORY_LOCK_KEY, engine_options=current_app.config.get('SQLALCHEMY_ENGINE_OPTIONS'))
    except Exception as exc:
        current_app.logger.warning('S1 自动发车锁获取失败，跳过本轮发车: %s', exc)
        return None
def _release_s1_dispatch_lock(lock_handle: object) -> None:
    if lock_handle is _LOCAL_S1_DISPATCH_LOCK_HANDLE:
        return
    try:
        if not lock_handle.release():
            raise RuntimeError('当前连接未持有自动发车 advisory lock')
    except Exception as exc:
        current_app.logger.warning('S1 自动发车锁释放失败: %s', exc)
def _get_generating_gm_retry_minutes() -> int:
    return max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_GENERATING_GM_RETRY_MINUTES', 75), 75))
def _generation_phase_anchor_at(log: AdventurerS1ExpeditionLog) -> datetime | None:
    """generating 阶段心跳锚点：优先 generation_phase_at，旧数据回退 created_at。"""
    if not log:
        return None
    if log.generation_phase_at:
        return log.generation_phase_at
    return log.created_at
def is_s1_generating_stuck(log: AdventurerS1ExpeditionLog, *, now: datetime | None=None) -> bool:
    """generating 且主 AI / 审核 AI 阶段超过 GM 重试阈值，视为卡住待补投或人工重试。"""
    if not log or log.status != 'generating':
        return False
    anchor = _generation_phase_anchor_at(log)
    if not anchor:
        return False
    now = now or _portable_utc_now()
    threshold = timedelta(minutes=_get_generating_gm_retry_minutes())
    return now - anchor >= threshold
def _clear_expedition_generation_phase(log: AdventurerS1ExpeditionLog) -> None:
    log.generation_phase = None
    log.generation_phase_at = None
    log.generation_owner_boot_id = None
def _current_generation_owner_prefix() -> str:
    boot_digest = sha256(str(get_async_worker_boot_id()).encode('utf-8')).hexdigest()[:32]
    return f'claim:{boot_digest}:'
def _new_expedition_generation_claim_token() -> str:
    """Return a bounded per-attempt owner token tied to the current boot."""
    return f'{_current_generation_owner_prefix()}{uuid4().hex}'
def _generation_owner_belongs_to_current_boot(owner: str | None) -> bool:
    """Accept legacy boot-only owners and current per-attempt owner tokens."""
    boot_id = str(get_async_worker_boot_id())
    owner = str(owner or '')
    return owner == boot_id or owner.startswith(_current_generation_owner_prefix())
def _owns_expedition_generation(log: AdventurerS1ExpeditionLog | None, generation_claim_token: str | None) -> bool:
    expected_owner = generation_claim_token or get_async_worker_boot_id()
    return bool(log and log.generation_owner_boot_id == expected_owner)
def _reject_lost_expedition_generation_claim(generation_claim_token: str | None) -> bool:
    if generation_claim_token is not None:
        raise _S1ExpeditionGenerationClaimLost('expedition_generation_claim_lost')
    return False

def _release_expedition_generation_lease(expedition_log_id: int, party_id: int, *, generation_claim_token: str | None=None) -> None:
    """Worker 正常退出且日志仍为 generating 时释放心跳租约，允许补投立即重试。

    成功写回（in_progress）或失败解散（failed）时不应调用；旧 boot 的 finally 不得
    清除新 boot 已接管的心跳。
    """
    from sqlalchemy.orm import load_only
    with db_manager.transaction():
        log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).options(load_only(AdventurerS1ExpeditionLog.id, AdventurerS1ExpeditionLog.party_id, AdventurerS1ExpeditionLog.status, AdventurerS1ExpeditionLog.generation_owner_boot_id, AdventurerS1ExpeditionLog.generation_phase_at, raiseload=True)).with_for_update().populate_existing().first()
        if not log or log.party_id != party_id or log.status != 'generating' or (not _owns_expedition_generation(log, generation_claim_token)):
            return
        log.generation_phase_at = None
        log.generation_owner_boot_id = None

def _can_requeue_expedition_generation(log: AdventurerS1ExpeditionLog) -> bool:
    """是否允许补投 generating AI 任务。

    - 未认领（phase_at 为空）或 worker 已释放租约：内部出错 / 提交失败，可立即补投。
    - owner 属于旧 AI Worker boot：消费者已经重启，可立即补投。
    - 心跳超时：视为卡住，可补投或 GM 重试。
    - 心跳仍新鲜：视为 worker 仍在跑，禁止补投（防重复生成）。
    """
    if not log or log.status != 'generating':
        return False
    if log.generation_phase_at is None:
        return True
    if not _generation_owner_belongs_to_current_boot(log.generation_owner_boot_id):
        return True
    return is_s1_generating_stuck(log)

_S1_GENERATION_CHECKPOINT_KEY = '_generation_checkpoint'
_S1_GENERATION_CHECKPOINT_VERSION = 1
_S1_SAME_BOOT_ACTIVE_TAKEOVER_PHASES = frozenset({'review_ai', 'settlement_guard_ai', 'apply_result'})
def _can_take_over_active_same_boot_generation(log: AdventurerS1ExpeditionLog) -> bool:
    """Only take over an active lock when a full downstream candidate is durable."""
    if not is_s1_generating_stuck(log) or log.generation_phase not in _S1_SAME_BOOT_ACTIVE_TAKEOVER_PHASES:
        return False
    full_log = log.full_log if isinstance(log.full_log, dict) else {}
    checkpoint = full_log.get(_S1_GENERATION_CHECKPOINT_KEY)
    member_ids = checkpoint.get('member_ids') if isinstance(checkpoint, dict) else None
    review_attempt = checkpoint.get('review_attempt') if isinstance(checkpoint, dict) else None
    return bool(isinstance(checkpoint, dict) and checkpoint.get('version') == _S1_GENERATION_CHECKPOINT_VERSION and (checkpoint.get('expedition_log_id') == log.id) and (checkpoint.get('party_id') == log.party_id) and isinstance(member_ids, list) and bool(member_ids) and all((type(member_id) is int and member_id > 0 for member_id in member_ids)) and (type(review_attempt) is int) and (1 <= review_attempt <= _s1_generation_checkpoint_review_attempt_limit()) and (checkpoint.get('resume_stage') == log.generation_phase) and isinstance(checkpoint.get('candidate'), dict))
_S1_GENERATION_CHECKPOINT_UNSET = object()
def _clone_s1_generation_value(value):
    return json.loads(json.dumps(value, ensure_ascii=False))
def _s1_generation_checkpoint_review_attempt_limit() -> int:
    """Keep checkpoint validation aligned with every configured review path."""
    return max(2, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_REVIEW_MAX_ATTEMPTS', 2), 2), _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_REVIEW_INFRA_MAX_ATTEMPTS', 3), 3), _coerce_int(current_app.config.get('ADVENTURER_S1_LOCATION_CONFLICT_REVIEW_MAX_ATTEMPTS', 4), 4))
def _load_s1_expedition_generation_checkpoint(full_log: dict | None, *, expedition_log_id: int, party_id: int, member_ids: list[int], copy_value: bool=True) -> dict | None:
    """读取并机械校验内部恢复点；身份或结构不匹配时绝不续用旧候选稿。"""
    if not isinstance(full_log, dict):
        return None
    checkpoint = full_log.get(_S1_GENERATION_CHECKPOINT_KEY)
    if not isinstance(checkpoint, dict):
        return None
    from plugins.adventurer_s1.models import S1_EXPEDITION_ACTIVE_GENERATION_PHASES
    expected_member_ids = [int(member_id) for member_id in member_ids]
    if checkpoint.get('version') != _S1_GENERATION_CHECKPOINT_VERSION or checkpoint.get('expedition_log_id') != expedition_log_id or checkpoint.get('party_id') != party_id or (checkpoint.get('member_ids') != expected_member_ids) or (checkpoint.get('resume_stage') not in S1_EXPEDITION_ACTIVE_GENERATION_PHASES):
        return None
    attempt = checkpoint.get('review_attempt')
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1 or (attempt > _s1_generation_checkpoint_review_attempt_limit()):
        return None
    for key in ('objective_outline', 'event_resolution', 'candidate', 'review_failure_fallback_candidate'):
        if checkpoint.get(key) is not None and (not isinstance(checkpoint.get(key), dict)):
            return None
    if checkpoint.get('resume_stage') in {'review_ai', 'settlement_guard_ai', 'apply_result'} and (not isinstance(checkpoint.get('candidate'), dict)):
        return None
    if not isinstance(checkpoint.get('consumed_buffs', []), list):
        return None
    return _clone_s1_generation_value(checkpoint) if copy_value else checkpoint
@monitor_performance('s1.persist_generation_checkpoint', context_getter=lambda expedition_log_id, **kwargs: {'expedition_log_id': expedition_log_id, 'party_id': kwargs.get('party_id'), 'phase': kwargs.get('resume_stage')})
def _persist_s1_expedition_generation_checkpoint(expedition_log_id: int, *, party_id: int, member_ids: list[int], resume_stage: str, review_attempt: int, generation_claim_token: str | None=None, objective_outline=_S1_GENERATION_CHECKPOINT_UNSET, event_resolution=_S1_GENERATION_CHECKPOINT_UNSET, candidate=_S1_GENERATION_CHECKPOINT_UNSET, review_failure_fallback_candidate=_S1_GENERATION_CHECKPOINT_UNSET, trait_context_receipt=_S1_GENERATION_CHECKPOINT_UNSET, observer_feedback=_S1_GENERATION_CHECKPOINT_UNSET, event_resolution_regeneration_basis=_S1_GENERATION_CHECKPOINT_UNSET, retry_errors=_S1_GENERATION_CHECKPOINT_UNSET, history_anchor_repairs=_S1_GENERATION_CHECKPOINT_UNSET, retry_trace=_S1_GENERATION_CHECKPOINT_UNSET, consumed_buffs=_S1_GENERATION_CHECKPOINT_UNSET, location_state_snapshot=_S1_GENERATION_CHECKPOINT_UNSET) -> bool:
    """原子保存可恢复输入/成品并刷新阶段心跳。"""
    from plugins.adventurer_s1.models import S1_EXPEDITION_ACTIVE_GENERATION_PHASES
    if resume_stage not in S1_EXPEDITION_ACTIVE_GENERATION_PHASES:
        return False
    checkpoint_review_attempt_limit = _s1_generation_checkpoint_review_attempt_limit()
    from db_repository.s1_expedition_generation_repository import load_generation_checkpoint_for_update, write_generation_checkpoint
    prepared_updates = {}
    for key, value in (('objective_outline', objective_outline), ('event_resolution', event_resolution), ('candidate', candidate), ('review_failure_fallback_candidate', review_failure_fallback_candidate)):
        if value is not _S1_GENERATION_CHECKPOINT_UNSET:
            prepared_updates[key] = _clone_s1_generation_value(value) if isinstance(value, dict) else None
    if retry_trace is not _S1_GENERATION_CHECKPOINT_UNSET:
        prepared_updates['retry_trace'] = _clone_s1_generation_value(list(retry_trace or [])[-50:])
    if consumed_buffs is not _S1_GENERATION_CHECKPOINT_UNSET:
        prepared_updates['consumed_buffs'] = _clone_s1_generation_value(list(consumed_buffs or []))
    receipt_updates = {}
    if isinstance(trait_context_receipt, dict):
        from plugins.adventurer_s1.services.s1_trait_context_projection_service import TRAIT_CONTEXT_SELECTION_LOG_FIELD
        receipt_updates[TRAIT_CONTEXT_SELECTION_LOG_FIELD] = _clone_s1_generation_value(trait_context_receipt)
    if isinstance(location_state_snapshot, dict):
        receipt_updates['location_state_snapshot'] = _clone_s1_generation_value(location_state_snapshot)
    with db_manager.transaction() as session:
        log = load_generation_checkpoint_for_update(session, expedition_log_id, _S1_GENERATION_CHECKPOINT_KEY)
        if not log or log.party_id != party_id or log.status != 'generating':
            return _reject_lost_expedition_generation_claim(generation_claim_token)
        if not _owns_expedition_generation(log, generation_claim_token):
            return _reject_lost_expedition_generation_claim(generation_claim_token)
        merged = dict(log.full_log or {})
        checkpoint = _load_s1_expedition_generation_checkpoint(merged, expedition_log_id=expedition_log_id, party_id=party_id, member_ids=member_ids, copy_value=False) or {'objective_outline': None, 'event_resolution': None, 'candidate': None, 'observer_feedback': None, 'event_resolution_regeneration_basis': None, 'retry_errors': [], 'retry_trace': [], 'consumed_buffs': []}
        checkpoint.update({'version': _S1_GENERATION_CHECKPOINT_VERSION, 'expedition_log_id': expedition_log_id, 'party_id': party_id, 'member_ids': [int(member_id) for member_id in member_ids], 'resume_stage': resume_stage, 'review_attempt': max(1, min(int(review_attempt), checkpoint_review_attempt_limit))})
        checkpoint.update(prepared_updates)
        if observer_feedback is not _S1_GENERATION_CHECKPOINT_UNSET:
            checkpoint['observer_feedback'] = str(observer_feedback)[:_S1_OBSERVER_FEEDBACK_MAX_CHARS] if observer_feedback else None
        if event_resolution_regeneration_basis is not _S1_GENERATION_CHECKPOINT_UNSET:
            checkpoint['event_resolution_regeneration_basis'] = str(event_resolution_regeneration_basis)[:100] if event_resolution_regeneration_basis else None
        if retry_errors is not _S1_GENERATION_CHECKPOINT_UNSET:
            checkpoint['retry_errors'] = [str(error)[:1000] for error in retry_errors or [] if str(error).strip()][:20]
        if resume_stage == 'main_ai':
            checkpoint.pop('history_anchor_repairs', None)
            checkpoint.pop('review_failure_fallback_candidate', None)
        else:
            proofs = checkpoint.get('history_anchor_repairs') or {} if history_anchor_repairs is _S1_GENERATION_CHECKPOINT_UNSET else history_anchor_repairs
            checkpoint['history_anchor_repairs'] = matching_s1_history_anchor_repairs(checkpoint.get('candidate') or {}, proofs)
        merged.update(receipt_updates)
        merged[_S1_GENERATION_CHECKPOINT_KEY] = checkpoint
        if not write_generation_checkpoint(session, log, merged, phase=resume_stage, phase_at=_portable_utc_now(), owner=generation_claim_token or get_async_worker_boot_id()):
            return _reject_lost_expedition_generation_claim(generation_claim_token)
        return True
def _checkpoint_s1_unexpected_consumer_exception(expedition_log_id: int, *, party_id: int, member_ids: list[int], generation_claim_token: str | None=None, error: Exception, max_attempts: int, retry_trace: list[dict]) -> str | None:
    """Persist a bounded same-candidate retry for an unexpected consumer fault.

    The provider response remains in the existing generation checkpoint.  A
    restarted worker therefore resumes the authoritative stage instead of
    calling the main story model again.  Once the shared infrastructure budget
    is exhausted, the caller must use the existing fail-and-disband finalizer.
    """
    db_manager.remove()
    log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).populate_existing().first()
    if not log or log.party_id != party_id or log.status != 'generating' or (not _owns_expedition_generation(log, generation_claim_token)):
        return None
    checkpoint = _load_s1_expedition_generation_checkpoint(dict(log.full_log or {}), expedition_log_id=expedition_log_id, party_id=party_id, member_ids=member_ids)
    if not isinstance(checkpoint, dict) or checkpoint.get('resume_stage') not in {'review_ai', 'settlement_guard_ai', 'apply_result'} or (not isinstance(checkpoint.get('candidate'), dict)):
        return None
    max_attempts = max(1, int(max_attempts or 1))
    persisted_trace = list(checkpoint.get('retry_trace') or [])
    prior_failures = sum((1 for entry in persisted_trace if isinstance(entry, dict) and entry.get('stage') == _S1_UNEXPECTED_CHECKPOINT_CONSUMER_STAGE))
    failure_attempt = min(prior_failures + 1, max_attempts)
    exhausted = prior_failures + 1 >= max_attempts
    reason_code = f'{_S1_UNEXPECTED_CHECKPOINT_CONSUMER_REASON_PREFIX}:{type(error).__name__}'
    error_detail = f'{type(error).__name__}: {error}'[:1000]
    _record_s1_retry_decision(persisted_trace, stage=_S1_UNEXPECTED_CHECKPOINT_CONSUMER_STAGE, reason_code=reason_code, action=S1RetryAction.FAIL_AND_DISBAND if exhausted else S1RetryAction.RETRY_SAME_STAGE, attempt=failure_attempt, max_attempts=max_attempts, preserves_main_result=not exhausted, detail=error_detail)
    current_review_attempt = max(1, int(checkpoint.get('review_attempt') or 1))
    next_review_attempt = current_review_attempt if exhausted else min(_s1_generation_checkpoint_review_attempt_limit(), current_review_attempt + 1)
    if not _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_ids, resume_stage=str(checkpoint['resume_stage']), review_attempt=next_review_attempt, generation_claim_token=generation_claim_token, retry_trace=persisted_trace):
        return None
    retry_trace[:] = persisted_trace
    return 'exhausted' if exhausted else 'retry'
def _checkpoint_s1_generation_infrastructure_exception(expedition_log_id: int, *, party_id: int, member_ids: list[int], generation_claim_token: str | None=None, error: BaseException, max_attempts: int, retry_trace: list[dict]) -> str | None:
    """持久化主生成上游请求的独立跨 worker 中断预算。

    这里只接受尚未形成主剧情 candidate 的生成阶段，以及统一请求层异常或
    跨区倒影领取回执待恢复异常。管理员主动停用 AI 属于有意暂停，不消耗该预算。
    冻结大纲、前置裁决和业务 review_attempt 均保持不变；耗尽后由调用方
    复用既有失败解散与资源恢复事务。
    """
    from plugins.adventurer_s1.services.s1_cross_realm_mirror_service import S1CrossRealmMirrorDeliveryError
    if not isinstance(error, (requests.RequestException, S1CrossRealmMirrorDeliveryError)) or isinstance(error, S1AiCallBlockedError):
        return None
    db_manager.remove()
    log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).populate_existing().first()
    if not log or log.party_id != party_id or log.status != 'generating' or (not _owns_expedition_generation(log, generation_claim_token)):
        return None
    checkpoint = _load_s1_expedition_generation_checkpoint(dict(log.full_log or {}), expedition_log_id=expedition_log_id, party_id=party_id, member_ids=member_ids)
    if not isinstance(checkpoint, dict):
        return None
    resume_stage = checkpoint.get('resume_stage')
    if resume_stage not in _S1_GENERATION_INFRASTRUCTURE_RESUME_STAGES or checkpoint.get('candidate') is not None:
        return None
    max_attempts = max(1, min(100, int(max_attempts or 1)))
    retryable_across_worker = isinstance(error, (S1AiRequestDeadlineExceeded, S1CrossRealmMirrorDeliveryError)) or is_s1_ai_retryable_request_exception(error)
    persisted_trace = list(checkpoint.get('retry_trace') or [])
    current_trace = list(retry_trace or [])
    if current_trace[:len(persisted_trace)] == persisted_trace:
        persisted_trace = current_trace
    prior_failures = sum((1 for entry in persisted_trace if isinstance(entry, dict) and entry.get('stage') == _S1_GENERATION_INFRASTRUCTURE_STAGE))
    failure_attempt = min(prior_failures + 1, max_attempts)
    exhausted = not retryable_across_worker or prior_failures + 1 >= max_attempts
    decision_max_attempts = max_attempts if retryable_across_worker else failure_attempt
    reason_code = f'{_S1_GENERATION_INFRASTRUCTURE_REASON_PREFIX}:{resume_stage}:{type(error).__name__}'
    error_detail = f'resume_stage={resume_stage}; {type(error).__name__}: {error}'[:1000]
    _record_s1_retry_decision(persisted_trace, stage=_S1_GENERATION_INFRASTRUCTURE_STAGE, reason_code=reason_code, action=S1RetryAction.FAIL_AND_DISBAND if exhausted else S1RetryAction.RETRY_SAME_STAGE, attempt=failure_attempt, max_attempts=decision_max_attempts, preserves_main_result=False, detail=error_detail)
    if not _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_ids, resume_stage=str(resume_stage), review_attempt=int(checkpoint['review_attempt']), generation_claim_token=generation_claim_token, retry_trace=persisted_trace):
        return None
    retry_trace[:] = persisted_trace
    return 'exhausted' if exhausted else 'retry'
def _s1_retry_stage_exhausted(retry_trace: list[dict] | None, *, stage: str) -> bool:
    """Return whether the latest decision for one persisted stage fails closed."""
    for entry in reversed(retry_trace or []):
        if isinstance(entry, dict) and entry.get('stage') == stage:
            return entry.get('action') == S1RetryAction.FAIL_AND_DISBAND.value
    return False
def _s1_unexpected_consumer_retry_exhausted(retry_trace: list[dict] | None) -> bool:
    """Return the latest persisted disposition for the shared consumer guard."""
    return _s1_retry_stage_exhausted(retry_trace, stage=_S1_UNEXPECTED_CHECKPOINT_CONSUMER_STAGE)
def _s1_generation_infrastructure_retry_exhausted(retry_trace: list[dict] | None) -> bool:
    """Return whether generation infrastructure recovery exhausted its budget."""
    return _s1_retry_stage_exhausted(retry_trace, stage=_S1_GENERATION_INFRASTRUCTURE_STAGE)

def _touch_expedition_generation_lease(log_id: int, generation_claim_token: str) -> bool:
    """独立单语句续租当前 generation claim，不改写流水线阶段。"""
    from db_repository.s1_expedition_generation_repository import touch_generation_lease
    return touch_generation_lease(log_id, generation_claim_token)

def _require_expedition_generation_claim(log_id: int, generation_claim_token: str) -> None:
    """在一次物理 AI 请求前同步续租；失权时阻止旧 worker 继续调用。"""
    db_manager.remove()
    try:
        if not _touch_expedition_generation_lease(log_id, generation_claim_token):
            raise _S1ExpeditionGenerationClaimLost('expedition_generation_claim_lost')
    finally:
        db_manager.remove()
def _start_expedition_generation_heartbeat(log_id: int, generation_claim_token: str) -> tuple[threading.Event, threading.Thread] | None:
    """在长 AI/审核调用期间周期续租；SQLite 仍靠每次出站前的同步检查。"""
    interval = max(0, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_GENERATION_HEARTBEAT_SECONDS', 30), 30))
    if interval <= 0:
        return None
    from extensions import db
    if db.engine.dialect.name == 'sqlite':
        return None
    app = current_app._get_current_object()
    stop_event = threading.Event()

    def _heartbeat() -> None:
        while not stop_event.wait(interval):
            with app.app_context():
                try:
                    if not _touch_expedition_generation_lease(log_id, generation_claim_token):
                        return
                except Exception:
                    current_app.logger.warning('S1 探险 generation claim 心跳失败 log=%s', log_id, exc_info=True)
                finally:
                    db_manager.remove()
    thread = threading.Thread(target=_heartbeat, name=f's1-expedition-heartbeat-{log_id}', daemon=True)
    thread.start()
    return (stop_event, thread)
def _stop_expedition_generation_heartbeat(heartbeat: tuple[threading.Event, threading.Thread] | None) -> None:
    if not heartbeat:
        return
    stop_event, thread = heartbeat
    stop_event.set()
    thread.join(timeout=2)
def _touch_expedition_generation_phase(log_id: int, phase: str, *, generation_claim_token: str | None=None) -> bool:
    """刷新 generating 流水线阶段与心跳时间；日志已非 generating 时返回 False。"""
    from plugins.adventurer_s1.models import S1_EXPEDITION_ACTIVE_GENERATION_PHASES
    if phase not in S1_EXPEDITION_ACTIVE_GENERATION_PHASES:
        return False
    with db_manager.transaction():
        log = AdventurerS1ExpeditionLog.query.filter_by(id=log_id).with_for_update().populate_existing().first()
        if not log or log.status != 'generating':
            return _reject_lost_expedition_generation_claim(generation_claim_token)
        if not _owns_expedition_generation(log, generation_claim_token):
            return _reject_lost_expedition_generation_claim(generation_claim_token)
        log.generation_phase = phase
        log.generation_phase_at = _portable_utc_now()
        log.generation_owner_boot_id = generation_claim_token or get_async_worker_boot_id()
        return True
def _try_claim_expedition_generation(expedition_log_id: int, *, force_retry: bool=False, generation_claim_token: str | None=None) -> bool:
    """独占认领 generating 日志的 AI 流水线。

    - 首次 worker（generation_phase_at 为空）：认领并写入心跳。
    - owner 属于旧 AI Worker boot：新消费者立即接管。
    - 阶段心跳仍新鲜：视为已有 worker，跳过（防重复生成）。
    - 心跳超过 GM 阈值：允许补投 / 人工重试接管。
    - force_retry=True：仅 GM 人工重试，须已卡住 >= GM 阈值。
    """
    from plugins.adventurer_s1.models import S1_EXPEDITION_ACTIVE_GENERATION_PHASES, S1_EXPEDITION_GENERATION_PHASE_MAIN_AI
    if generation_claim_token is not None and (not generation_claim_token.startswith(_current_generation_owner_prefix())):
        return False
    with db_manager.transaction():
        log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).with_for_update().populate_existing().first()
        if not log or log.status != 'generating':
            return False
        stuck = is_s1_generating_stuck(log)
        owner_is_current_boot = _generation_owner_belongs_to_current_boot(log.generation_owner_boot_id)
        if force_retry:
            if not stuck:
                return False
        elif log.generation_phase_at is not None and (not stuck) and owner_is_current_boot:
            return False
        if log.generation_phase not in S1_EXPEDITION_ACTIVE_GENERATION_PHASES:
            log.generation_phase = S1_EXPEDITION_GENERATION_PHASE_MAIN_AI
        log.generation_phase_at = _portable_utc_now()
        log.generation_owner_boot_id = generation_claim_token or get_async_worker_boot_id()
        return True
def _restore_consumed_buffs(consumed_buffs: list) -> None:
    """AI 生成失败时，把已消耗的食物 buff 还给冒险者（探险未真正开始）。"""
    for entry in consumed_buffs:
        adv_id = entry.get('adventurer_id')
        buff = entry.get('buff')
        if not adv_id or not isinstance(buff, dict):
            continue
        adv = AdventurerS1.query.session.get(AdventurerS1, adv_id)
        if adv:
            adv.expedition_buff = buff
    db_manager.commit()
_SPECIAL_DUNGEON_TICKET_CONSUMPTIONS_KEY = '_special_dungeon_ticket_consumptions'
_SPECIAL_DUNGEON_TICKET_REFUND_KEY = '_special_dungeon_ticket_refund'
_SPECIAL_DUNGEON_TICKET_REFUND_SOURCE = 'special_ticket_refund'

def _resolve_failed_special_dungeon_ticket_consumptions(cached_full_log: dict, members: list, target_dungeon: str) -> tuple[list[dict], str | None]:
    """读取发车时冻结的扣票回执；兼容部署前已在 generating 的特殊副本。"""
    special_context = cached_full_log.get('special_dungeon_context')
    if not isinstance(special_context, dict):
        return ([], None)
    member_ids = {int(member.id) for member in members if getattr(member, 'id', None) is not None}
    frozen = cached_full_log.get(_SPECIAL_DUNGEON_TICKET_CONSUMPTIONS_KEY)
    if frozen is None:
        from plugins.adventurer_s1.services.adventurer_s1_config_service import get_s1_special_dungeon
        dungeon_name = str(special_context.get('dungeon') or target_dungeon or '').strip()
        special = get_s1_special_dungeon(dungeon_name)
        ticket_item = str((special or {}).get('ticket_item') or '').strip()
        if not special or not ticket_item:
            raise RuntimeError(f'S1 特殊副本退票失败：无法确认旧日志门票 dungeon={dungeon_name}')
        return ([{'adventurer_id': adventurer_id, 'ticket_item': ticket_item, 'quantity': 1} for adventurer_id in sorted(member_ids)], 'legacy_member_snapshot')
    if not isinstance(frozen, list):
        raise RuntimeError('S1 特殊副本退票失败：扣票回执格式错误')
    resolved: list[dict] = []
    seen_ids: set[int] = set()
    for raw in frozen:
        if not isinstance(raw, dict):
            raise RuntimeError('S1 特殊副本退票失败：扣票回执条目格式错误')
        try:
            adventurer_id = int(raw.get('adventurer_id'))
            quantity = int(raw.get('quantity'))
        except (TypeError, ValueError):
            raise RuntimeError('S1 特殊副本退票失败：扣票回执缺少有效成员或数量') from None
        ticket_item = str(raw.get('ticket_item') or '').strip()
        if adventurer_id <= 0 or adventurer_id not in member_ids or adventurer_id in seen_ids or (quantity != 1) or (not ticket_item):
            raise RuntimeError(f'S1 特殊副本退票失败：扣票回执不一致 adv={adventurer_id}')
        seen_ids.add(adventurer_id)
        resolved.append({'adventurer_id': adventurer_id, 'ticket_item': ticket_item, 'quantity': 1})
    if member_ids and (not resolved):
        raise RuntimeError('S1 特殊副本退票失败：扣票回执为空')
    return (sorted(resolved, key=lambda item: item['adventurer_id']), 'frozen_consumption_receipt')
def _refund_failed_special_dungeon_tickets(log: AdventurerS1ExpeditionLog, cached_full_log: dict, members: list, target_dungeon: str) -> dict | None:
    """AI 终态生成失败时原子返票；优先随身，满则入仓库并写审计流水。"""
    consumptions, basis = _resolve_failed_special_dungeon_ticket_consumptions(cached_full_log, members, target_dungeon)
    if not consumptions:
        return None
    adventurer_ids = [entry['adventurer_id'] for entry in consumptions]
    adventurers = AdventurerS1.query.filter(AdventurerS1.id.in_(adventurer_ids)).order_by(AdventurerS1.id.asc()).with_for_update().populate_existing().all()
    adventurer_by_id = {int(adventurer.id): adventurer for adventurer in adventurers}
    if set(adventurer_by_id) != set(adventurer_ids):
        raise RuntimeError('S1 特殊副本退票失败：扣票成员已不存在')
    from plugins.adventurer_s1.models import AdventurerS1ItemLog
    from plugins.adventurer_s1.services.s1_item_instance_service import lock_warehouse_aggregates
    from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item
    lock_warehouse_aggregates(adventurer_ids)
    refund_items: list[dict] = []
    for entry in consumptions:
        adventurer_id = entry['adventurer_id']
        ticket_item = entry['ticket_item']
        granted, grant_err = grant_catalog_item(adventurer_by_id[adventurer_id], ticket_item, quantity=1, prefer_equipment=True, allow_warehouse_capacity_overflow=True, source_type=_SPECIAL_DUNGEON_TICKET_REFUND_SOURCE, description=f'【{target_dungeon}】AI 生成失败，返还门票', related_id=str(log.id), related_type='expedition_log')
        if grant_err or granted != [ticket_item]:
            raise RuntimeError(f"S1 特殊副本退票失败 adv={adventurer_id} dungeon={target_dungeon}: {grant_err or '发放结果不一致'}")
        db_manager.flush()
        refund_log = AdventurerS1ItemLog.query.filter(AdventurerS1ItemLog.adventurer_id == adventurer_id, AdventurerS1ItemLog.item_name == ticket_item, AdventurerS1ItemLog.change_type.in_(['gain', 'overflow']), AdventurerS1ItemLog.quantity == 1, AdventurerS1ItemLog.source_type == _SPECIAL_DUNGEON_TICKET_REFUND_SOURCE, AdventurerS1ItemLog.related_id == str(log.id), AdventurerS1ItemLog.related_type == 'expedition_log').order_by(AdventurerS1ItemLog.id.desc()).first()
        if not refund_log:
            raise RuntimeError(f'S1 特殊副本退票失败：发放流水缺失 adv={adventurer_id}')
        destination = 'warehouse' if refund_log.change_type == 'overflow' else 'equipment'
        if destination == 'warehouse':
            refund_log.description = f'【{target_dungeon}】AI 生成失败；随身栏已满，门票退至仓库'
        refund_items.append({'adventurer_id': adventurer_id, 'ticket_item': ticket_item, 'quantity': 1, 'destination': destination, 'change_type': refund_log.change_type, 'item_log_id': int(refund_log.id)})
    return {'basis': basis, 'source_type': _SPECIAL_DUNGEON_TICKET_REFUND_SOURCE, 'related_id': str(log.id), 'related_type': 'expedition_log', 'items': refund_items}
def _apply_expedition_ai_result(log_id: int, party_id: int, members: list, result_data: dict | None, *, pre_events: list, target_dungeon: str, has_special_event: bool, is_extreme_crisis: bool, special_event_type: str | None, special_event_risk_tier: str | None=None, stamina_max: int, stamina_cost: int, consumed_buffs: list | None=None, generation_failure_detail: str | None=None, generation_retry_trace: list[dict] | None=None, generation_claim_token: str | None=None, before_publish: Callable[[], None] | None=None, trait_context_receipt: dict | None=None) -> tuple[int | None, str | None]:
    """将主探险 AI 结果写回日志：成功转 in_progress 并首批推进，失败则叙事兜底并释放成员。"""
    from .s1_independent_dungeon_service import project_independent_dungeon_result
    project_independent_dungeon_result(result_data, target_dungeon)
    from plugins.adventurer_s1.services.s1_trait_context_projection_service import TRAIT_CONTEXT_SELECTION_LOG_FIELD
    if not result_data:
        current_app.logger.warning('S1 探险 AI 返回为空 party=%s log_id=%s，使用叙事兜底文案', party_id, log_id)
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=log_id).with_for_update().populate_existing().first()
            if not log or log.status != 'generating' or (not _owns_expedition_generation(log, generation_claim_token)):
                current_app.logger.info('S1 探险 AI 失败写回跳过：状态或 boot owner 已变化 party=%s log=%s status=%s owner=%s', party_id, log_id, getattr(log, 'status', None), getattr(log, 'generation_owner_boot_id', None))
                return (None, '探险生成已结束')
            cached = dict(log.full_log or {})
            failed_hidden_ctx = cached.get('hidden_boss_context')
            log.status = 'failed'
            _clear_expedition_generation_phase(log)
            fail_reasons = ['冒险者们走到城门口，发现今天去地下城的马车全没了，只好打道回府。', '刚出城就遇上暴雨，道路泥泞不堪，队伍被迫折返。', '冒险者公会临时发布通知：目标区域魔力暴走，今日禁止通行。', '守城卫兵说前方出现不可控异动，公会临时叫停了今天的委托。', '出发时发现补给忘带了，总不能饿着肚子探险吧，只好改天再出发。', '城门口的占卜师说今天不宜出行，冒险者们犹豫再三还是回去了。', '半路上遇到一支败退的冒险队，说前方异常危险，队伍决定先撤。', '刚走到半路，队长发现地图拿错了，只好原路返回。']
            reason = random.choice(fail_reasons)
            failed_full_log = {'status': 'failed', 'summary': reason, 'audit_member_snapshot': cached.get('audit_member_snapshot')}
            if generation_failure_detail:
                failed_full_log['generation_failure_detail'] = str(generation_failure_detail)[:4000]
            if generation_retry_trace:
                failed_full_log['generation_retry_trace'] = list(generation_retry_trace[-_S1_RETRY_TRACE_MAX_ENTRIES:])
                terminal_failure = _terminal_s1_failure_from_retry_trace(generation_retry_trace)
                if terminal_failure is not None:
                    failed_full_log['generation_failure_category'], failed_full_log['generation_failure_code'] = terminal_failure
            for _admin_key in ('has_special_event', 'special_event_type', 'special_event_risk_tier', 'is_extreme_crisis', 'target_dungeon', 'departure_msg', 'hidden_boss_context', 'commission_context', 'special_dungeon_context', 'campaign_dispatch', 'hidden_dungeon_context', '_ecology_identity_snapshot', '_ecology_identity_review', '_objective_mode_snapshot', '_expedition_locus_snapshot', '_campaign_departure_decision', TRAIT_CONTEXT_SELECTION_LOG_FIELD, _SPECIAL_DUNGEON_TICKET_CONSUMPTIONS_KEY, 'narrative_seed', 'dining_context', '_member_id_to_name', '_generation_checkpoint', 'objective_outline_rejected_reason', 'objective_outline_raw_excerpt', 'objective_outline_validation_warnings'):
                if cached.get(_admin_key) is not None:
                    failed_full_log[_admin_key] = cached.get(_admin_key)
            failed_full_log['has_special_event'] = bool(has_special_event)
            failed_full_log['is_extreme_crisis'] = bool(is_extreme_crisis)
            failed_full_log['target_dungeon'] = target_dungeon
            if special_event_type is not None:
                failed_full_log['special_event_type'] = special_event_type
            if special_event_risk_tier is not None:
                failed_full_log['special_event_risk_tier'] = special_event_risk_tier
            ticket_refund = _refund_failed_special_dungeon_tickets(log, cached, members, target_dungeon)
            if ticket_refund:
                failed_full_log[_SPECIAL_DUNGEON_TICKET_REFUND_KEY] = ticket_refund
            from plugins.adventurer_s1.services.s1_waiting_player_npc_service import release_unused_waiting_npc_leases
            release_unused_waiting_npc_leases(int(log.id), cached.get('waiting_player_npc_injection'), set())
            log.full_log = failed_full_log
            log.result_summary = reason
            err = refresh_expedition_log_members_for_log(log, [m.id for m in members])
            if err:
                raise RuntimeError(f'S1 探险失败收尾刷新成员索引失败：{err}')
            from plugins.adventurer_s1.services.adventurer_s1_party_service import finalize_party_disband
            finalize_party_disband(party_id, member_ids=[m.id for m in members], exploring_stamina_refund=stamina_cost)
            from plugins.adventurer_s1.services.s1_narrative_lease_service import release_narrative_leases_for_log
            release_narrative_leases_for_log(log.id)
            S1CommissionService.settle_expedition(failed_full_log.get('commission_context') or [], failed_full_log.get('commission_results') or [], {m.id for m in members if getattr(m, 'status', None) != 'dead'}, expedition_log_id=log.id, hidden_boss_completed=bool(_coerce_int(failed_full_log.get('hidden_boss_completed') or failed_full_log.get('hidden_boss_commission_completed'), 0)), auto_commit=False)
        try:
            S1HiddenBossService.settle_challenge(log.target_dungeon, party_id, log_id, {'hidden_boss_context': failed_hidden_ctx} if failed_hidden_ctx else {}, members=members)
        finally:
            if consumed_buffs:
                _restore_consumed_buffs(consumed_buffs)
        return (None, reason)
    with db_manager.transaction():
        log = AdventurerS1ExpeditionLog.query.filter_by(id=log_id).with_for_update().populate_existing().first()
        if not log or log.status != 'generating' or (not _owns_expedition_generation(log, generation_claim_token)):
            current_app.logger.info('S1 探险 AI 成功写回跳过：状态或 boot owner 已变化 party=%s log=%s status=%s owner=%s', party_id, log_id, getattr(log, 'status', None), getattr(log, 'generation_owner_boot_id', None))
            return (log_id if log and log.status == 'in_progress' else None, None)
        cached = dict(log.full_log or {})
        waiting_npc_updates_for_log: list[dict] = []
        checkpoint = _load_s1_expedition_generation_checkpoint(cached, expedition_log_id=log_id, party_id=party_id, member_ids=[m.id for m in members]) or {}
        server_draft = checkpoint.get('event_resolution')
        if isinstance(server_draft, dict) and server_draft.get('resolution_contract_version') == 3:
            from plugins.adventurer_s1.services.s1_encounter_draft_contract import validate_compact_narrative_result
            draft_errors = validate_compact_narrative_result(result_data, server_draft)
            if draft_errors:
                raise RuntimeError('S1 交锋推演发布结果不一致: ' + '; '.join(draft_errors))
            cached['pre_narrative_resolution'] = deepcopy(server_draft)
        for update in result_data.get('waiting_npc_updates') or []:
            if not isinstance(update, dict):
                continue
            stored_update = dict(update)
            stored_update['records'] = [{**record, 'event_index': int(record.get('event_index') or 0) + len(pre_events)} for record in update.get('records') or [] if isinstance(record, dict)]
            waiting_npc_updates_for_log.append(stored_update)
        waiting_npc_disposition_for_log = result_data.get(_WAITING_NPC_REVIEW_DISPOSITION_LOG_FIELD)
        if isinstance(waiting_npc_disposition_for_log, dict):
            waiting_npc_disposition_for_log = json.loads(json.dumps(waiting_npc_disposition_for_log, ensure_ascii=False))
            waiting_npc_disposition_for_log['event_index_basis'] = 'candidate_final_events_zero_based'
            waiting_npc_disposition_for_log['full_log_event_index_offset'] = len(pre_events)
        else:
            waiting_npc_disposition_for_log = None
        from plugins.adventurer_s1.services.s1_waiting_player_npc_service import hold_waiting_npc_review_quarantine, release_unused_waiting_npc_leases
        waiting_npc_quarantine = result_data.get('waiting_npc_review_quarantine')
        quarantined_actor_ids = {int(row['actor_id']) for row in (waiting_npc_quarantine or {}).get('actor_refs') or [] if isinstance(row, dict) and isinstance(row.get('actor_id'), int) and (not isinstance(row.get('actor_id'), bool))}
        release_unused_waiting_npc_leases(int(log_id), cached.get('waiting_player_npc_injection'), {int(update['actor_id']) for update in waiting_npc_updates_for_log if isinstance(update.get('actor_id'), int) and (not isinstance(update.get('actor_id'), bool))} | quarantined_actor_ids)
        if isinstance(waiting_npc_quarantine, dict) and waiting_npc_quarantine.get('status') == 'quarantined':
            waiting_npc_quarantine = dict(waiting_npc_quarantine)
            waiting_npc_quarantine['actor_refs'] = hold_waiting_npc_review_quarantine(int(log_id), cached.get('waiting_player_npc_injection'))
        faction_presence_receipts_for_log: list[dict] = []
        for receipt in result_data.get(FACTION_PRESENCE_RECEIPTS_FIELD) or []:
            if not isinstance(receipt, dict):
                continue
            projected_receipt = {key: receipt[key] for key in ('world_story_thread_id', 'source_revision', 'faction_profile_id', 'profile_revision') if key in receipt}
            indexes = receipt.get('evidence_event_indexes')
            if not isinstance(indexes, list):
                continue
            projected_receipt['evidence_event_indexes'] = [int(index) + len(pre_events) for index in indexes if type(index) is int and index >= 0]
            faction_presence_receipts_for_log.append(projected_receipt)
        countermeasure_learning_for_log: list[dict] = []
        for raw_learning in result_data.get('countermeasure_learning') or []:
            if not isinstance(raw_learning, dict):
                continue
            projected_learning = json.loads(json.dumps(raw_learning, ensure_ascii=False))
            indexes = projected_learning.get('evidence_event_indexes')
            if isinstance(indexes, list):
                projected_learning['evidence_event_indexes'] = [int(index) + len(pre_events) for index in indexes if type(index) is int and index >= 0]
            countermeasure_learning_for_log.append(projected_learning)
        from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD, COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD, project_countermeasure_learning_disposition_for_log, shift_countermeasure_application_receipts
        countermeasure_application_receipts_for_log = shift_countermeasure_application_receipts(result_data.get(COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD), event_index_offset=len(pre_events))
        countermeasure_learning_disposition_for_log = project_countermeasure_learning_disposition_for_log(result_data.get(COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD), event_index_offset=len(pre_events))
        current_gold_contract_required = cached.get(S1_GOLD_SETTLEMENT_VERSION_FIELD) == S1_GOLD_SETTLEMENT_CONTRACT_VERSION or result_data.get(S1_GOLD_SETTLEMENT_VERSION_FIELD) == S1_GOLD_SETTLEMENT_CONTRACT_VERSION or S1_GOLD_SETTLEMENT_FIELD in result_data
        gold_settlement_for_log = None
        if current_gold_contract_required:
            rebased_gold_settlement = rebase_s1_gold_settlement_for_log(result_data.get(S1_GOLD_SETTLEMENT_FIELD), event_index_offset=len(pre_events))
            gold_settlement_for_log, gold_settlement_errors = normalize_s1_gold_settlement_value(rebased_gold_settlement, member_ids=[int(member.id) for member in members], event_count=len(pre_events) + len(result_data.get('events') or []))
            if gold_settlement_errors or gold_settlement_for_log is None:
                error_details = gold_settlement_errors or ['规范化结果为空']
                raise RuntimeError('S1 金币结算发布投影失败: ' + '；'.join(error_details[:10]))
        published_member_updates = _prepare_s1_member_updates_for_log(result_data, members)
        for member_update in published_member_updates:
            for transfer in member_update.get('received_assets') or []:
                transfer['event_indexes'] = [i + len(pre_events) for i in transfer['event_indexes']]
        full_log = {'status': 'in_progress', 'data_appeal_full_text_default': True, 'summary': result_data.get('summary', ''), 'events': pre_events + list(result_data.get('events', [])), 'member_updates': published_member_updates, 'members': [m.name for m in members], 'current_event_index': len(pre_events), 'progress_push_count': 0, 'target_dungeon': target_dungeon, 'has_special_event': has_special_event, 'is_extreme_crisis': is_extreme_crisis, 'special_event_type': special_event_type, 'special_event_risk_tier': special_event_risk_tier, 'hidden_boss_context': cached.get('hidden_boss_context'), 'hidden_boss_completed': int(result_data.get('hidden_boss_completed') or result_data.get('hidden_boss_commission_completed') or 0), 'commission_context': cached.get('commission_context'), 'campaign_dispatch': cached.get('campaign_dispatch') or {}, 'activity_world': deepcopy(cached.get('activity_world')), 'commission_results': result_data.get('commission_results') or [], 'departure_msg': cached.get('departure_msg'), 'audit_member_snapshot': cached.get('audit_member_snapshot'), '_member_id_to_name': cached.get('_member_id_to_name'), S1_ASSET_DESCRIPTION_UPDATE_RECEIPT_FIELD: result_data.get(S1_ASSET_DESCRIPTION_UPDATE_RECEIPT_FIELD), 'special_dungeon_context': cached.get('special_dungeon_context'), 'hidden_dungeon_context': cached.get('hidden_dungeon_context'), HIDDEN_DUNGEON_REWARD_PLAN_KEY: cached.get(HIDDEN_DUNGEON_REWARD_PLAN_KEY) or [], 'narrative_seed': cached.get('narrative_seed'), 'dining_context': cached.get('dining_context'), 'objective_world_intel_injection': cached.get('objective_world_intel_injection'), 'generation_world_intel_injection': cached.get('generation_world_intel_injection'), '_ecology_identity_snapshot': cached.get('_ecology_identity_snapshot'), '_ecology_identity_review': result_data.get('_ecology_identity_review'), '_objective_mode_snapshot': cached.get('_objective_mode_snapshot'), '_expedition_locus_snapshot': cached.get('_expedition_locus_snapshot'), '_campaign_departure_decision': cached.get('_campaign_departure_decision'), '_temporary_location': cached.get('_temporary_location'), TRAIT_CONTEXT_SELECTION_LOG_FIELD: _clone_s1_generation_value(trait_context_receipt if isinstance(trait_context_receipt, dict) else cached.get(TRAIT_CONTEXT_SELECTION_LOG_FIELD)), 'waiting_player_npc_injection': cached.get('waiting_player_npc_injection'), 'cross_realm_mirror_injection': cached.get('cross_realm_mirror_injection'), 'world_intel_adoptions': cached.get('world_intel_adoptions') or [], 'pre_narrative_resolution': cached.get('pre_narrative_resolution'), 'pre_narrative_life_adjustments': result_data.get('pre_narrative_life_adjustments') or [], 'world_fact_updates': result_data.get('world_fact_updates') or [], FACTION_PRESENCE_RECEIPTS_FIELD: faction_presence_receipts_for_log, 'dm_handoff_note': result_data.get('dm_handoff_note') or '', 'ecology_continuity_debt': result_data.get('ecology_continuity_debt'), 'countermeasure_learning': countermeasure_learning_for_log, 'waiting_npc_updates': waiting_npc_updates_for_log, 'fate_journey_context': cached.get('fate_journey_context'), 'fate_progress': result_data.get('fate_progress'), 'location_state_snapshot': cached.get('location_state_snapshot'), 'pending_location_state_changes': list(result_data.get('location_state_changes') or []), 'pending_dungeon_explorability_change': result_data.get('dungeon_explorability_change'), 'location_state_application': [], 'dungeon_explorability_application': None}
        if is_s1_independent_dungeon(target_dungeon):
            from .s1_independent_dungeon_service import MATERIAL_REWARD_INPUT_KEY, MATERIAL_REWARD_PLAN_KEY, build_material_reward_plan
            frozen_rewards = cached[MATERIAL_REWARD_INPUT_KEY]
            full_log[MATERIAL_REWARD_INPUT_KEY] = frozen_rewards
            full_log[MATERIAL_REWARD_PLAN_KEY] = cached.get(MATERIAL_REWARD_PLAN_KEY) or build_material_reward_plan(frozen_rewards, full_log['member_updates'], [int(member.id) for member in members])
        if gold_settlement_for_log is not None:
            full_log[S1_GOLD_SETTLEMENT_FIELD] = gold_settlement_for_log
            full_log[S1_GOLD_SETTLEMENT_VERSION_FIELD] = S1_GOLD_SETTLEMENT_CONTRACT_VERSION
        from .s1_team_loot_service import copy_team_loot_to_published_log
        copy_team_loot_to_published_log(full_log, result_data, pre_events=pre_events, log_id=log_id)
        if isinstance(server_draft, dict) and server_draft.get('resolution_contract_version') == 3:
            full_log['completion_check'] = deepcopy(result_data['completion_check'])
        if countermeasure_application_receipts_for_log is not None:
            full_log[COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD] = countermeasure_application_receipts_for_log
        from plugins.adventurer_s1.services.s1_countermeasure_learning_ai_service import RECEIPT_FIELD as learning_owner_receipt_field
        if isinstance(result_data.get(learning_owner_receipt_field), dict):
            full_log[learning_owner_receipt_field] = {**deepcopy(result_data[learning_owner_receipt_field]), 'event_index_basis': 'candidate_final_events_zero_based', 'final_event_index_offset': len(pre_events)}
        if countermeasure_learning_disposition_for_log is not None:
            full_log[COUNTERMEASURE_LEARNING_DISPOSITION_LOG_FIELD] = countermeasure_learning_disposition_for_log
        _copy_s1_same_name_gain_review_receipt_to_log(full_log, result_data, event_index_offset=len(pre_events))
        same_name_gain_receipt_errors = _validate_s1_same_name_gain_review_receipt(full_log)
        if same_name_gain_receipt_errors:
            raise RuntimeError('S1 同名装备裁决未闭合，禁止发布: ' + '；'.join(same_name_gain_receipt_errors[:10]))
        for destructive_field in (FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD, FINAL_FATE_DESTRUCTIVE_RESOLUTION_FIELD):
            destructive_value = result_data.get(destructive_field)
            if isinstance(destructive_value, dict):
                full_log[destructive_field] = json.loads(json.dumps(destructive_value, ensure_ascii=False))
        reviewed_companion_stage_floors = result_data.get(_REVIEWED_COMPANION_STAGE_FLOORS_KEY)
        if isinstance(reviewed_companion_stage_floors, list):
            full_log[_REVIEWED_COMPANION_STAGE_FLOORS_KEY] = [dict(row) for row in reviewed_companion_stage_floors if isinstance(row, dict)]
        if waiting_npc_quarantine is not None:
            full_log['waiting_npc_review_quarantine'] = waiting_npc_quarantine
        if waiting_npc_disposition_for_log is not None:
            full_log[_WAITING_NPC_REVIEW_DISPOSITION_LOG_FIELD] = waiting_npc_disposition_for_log
        if generation_retry_trace:
            full_log['generation_retry_trace'] = list(generation_retry_trace[-_S1_RETRY_TRACE_MAX_ENTRIES:])
        review_quarantine_receipt = result_data.get(_S1_REVIEW_FAILURE_QUARANTINE_RECEIPT_FIELD)
        if isinstance(review_quarantine_receipt, dict):
            full_log[_S1_REVIEW_FAILURE_QUARANTINE_RECEIPT_FIELD] = deepcopy(review_quarantine_receipt)
        if cached.get('objective_outline'):
            full_log['objective_outline'] = cached.get('objective_outline')
        for debug_key in ('objective_outline_rejected_reason', 'objective_outline_raw_excerpt', 'objective_outline_validation_warnings'):
            if cached.get(debug_key):
                full_log[debug_key] = cached.get(debug_key)
        from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService
        S1WorldOutlineService.create_candidate_from_result(expedition_log_id=log_id, target_dungeon=target_dungeon, result_data=result_data)
        log.full_log = full_log
        log.status = 'in_progress'
        from plugins.adventurer_s1.services.s1_temporary_location_service import temporary_location_from_context
        destination = temporary_location_from_context(cached)
        log.temporary_location_name = destination['location_name'] if destination else None
        log.result_summary = '探险进行中...'
        _clear_expedition_generation_phase(log)
    if before_publish is not None:
        try:
            before_publish()
        except Exception:
            current_app.logger.warning('S1 探险发布前 token 用量落账失败 party=%s log=%s', party_id, log_id, exc_info=True)
    try:
        from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
        socketio.emit('expedition_log_changed', {'action': 'created', 'reason': 'expedition_started', 'data': {'id': log_id, 'party_id': party_id, 'status': 'in_progress'}}, namespace='/')
        socketio.emit('adventurer_changed', {'action': 'status_changed', 'reason': 'expedition_started', 'ids': [m.id for m in members]}, namespace='/')
    except Exception:
        pass
    try:
        progress_s1_expedition(party_id)
    except Exception as e:
        current_app.logger.warning('S1 首次推进失败 party=%s: %s', party_id, e)
    return (log_id, None)
def _submit_s1_expedition_ai_task(party_id: int, expedition_log_id: int, *, force_retry: bool=False) -> bool:
    """把主探险 AI 投递到 AsyncTaskService（Redis 队列 + worker 线程）。"""
    from services.async_task_service import AsyncTaskService
    payload = {'party_id': int(party_id), 'expedition_log_id': int(expedition_log_id), 'force_retry': bool(force_retry)}
    if force_retry:
        dedupe_key = AsyncTaskService._build_task_dedupe_key('ai_s1_expedition', payload)
        AsyncTaskService._release_task_dedupe_key(dedupe_key)
    elif AsyncTaskService.is_task_enqueued_or_running('ai_s1_expedition', payload):
        current_app.logger.info('S1 探险 AI 任务已在队列中 party=%s log=%s', party_id, expedition_log_id)
        return True
    if not AsyncTaskService.submit_task('ai_s1_expedition', payload):
        current_app.logger.error('S1 探险 AI 任务提交失败 party=%s log=%s', party_id, expedition_log_id)
        return False
    return True

def _enqueue_s1_description_summaries_after_apply(*, applied_log_id: int | None, apply_error: object, approved_result: dict | None, member_id_to_name: dict | None=None) -> int:
    """只在最终结算落库成功后投递单次压缩，任何故障均不影响结算。"""
    if not isinstance(approved_result, dict) or applied_log_id is None or apply_error is not None:
        return 0
    try:
        return enqueue_expedition_result_description_compressions(approved_result, expedition_log_id=int(applied_log_id), member_id_to_name=member_id_to_name)
    except Exception as exc:
        current_app.logger.warning('S1 最终落库资产描述摘要入队失败，保留完整原文 log=%s: %s', applied_log_id, exc)
        return 0

def _do_s1_expedition_ai(party_id: int, expedition_log_id: int, *, force_retry: bool=False) -> None:
    """AsyncTaskService worker：调用主探险 AI 并写回日志（不阻塞 scheduler / HTTP）。"""
    log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).populate_existing().first()
    if not log or log.party_id != party_id:
        current_app.logger.warning('S1 探险 AI 任务跳过：日志不存在或 party 不匹配 party=%s log=%s', party_id, expedition_log_id)
        return
    if log.status != 'generating':
        current_app.logger.info('S1 探险 AI 任务跳过：日志已非 generating party=%s log=%s status=%s', party_id, expedition_log_id, log.status)
        return
    generation_claim_token = _new_expedition_generation_claim_token()
    if not _try_claim_expedition_generation(expedition_log_id, force_retry=force_retry, generation_claim_token=generation_claim_token):
        current_app.logger.info('S1 探险 AI 任务跳过：generating 流水线仍由其他 worker 占用 party=%s log=%s force_retry=%s', party_id, expedition_log_id, force_retry)
        return
    generation_retry_trace: list[dict] = []
    deferred_observer_ai_token_usage_handles: list[object] = []
    deferred_guard_ai_token_usage_handles: list[object] = []
    deferred_main_ai_token_usage_handles: list[object] = []
    pipeline_recovery_context: dict | None = None
    unexpected_consumer_fail_close = False
    generation_infrastructure_fail_close = False
    checkpoint_fail_close = False
    generation_heartbeat: tuple[threading.Event, threading.Thread] | None = None

    def require_current_generation_claim() -> None:
        _require_expedition_generation_claim(expedition_log_id, generation_claim_token)

    def finalize_token_usage_handle_group(handles: list[object], *, is_success: bool) -> None:
        while handles:
            handle = handles.pop()
            finalize = getattr(handle, 'finalize', None)
            if not callable(finalize):
                continue
            try:
                finalize(is_success=is_success)
            except Exception:
                current_app.logger.warning('S1 延迟 token 用量落账失败 party=%s log=%s success=%s', party_id, expedition_log_id, is_success, exc_info=True)

    def finalize_deferred_ai_token_usage(*, is_success: bool, include_main_ai: bool=True, failure_stage: str | None=None, validation_error_code: str | None=None) -> None:
        handle_groups = [deferred_observer_ai_token_usage_handles, deferred_guard_ai_token_usage_handles]
        if include_main_ai:
            handle_groups.append(deferred_main_ai_token_usage_handles)
        if not is_success:
            latest_retry = next((entry for entry in reversed(generation_retry_trace) if isinstance(entry, dict)), {})
            resolved_failure_stage = str(failure_stage or latest_retry.get('stage') or 'downstream_consumption')
            resolved_validation_error_code = str(validation_error_code or latest_retry.get('reason_code') or 'pipeline_not_consumed')
            for handles in handle_groups:
                for handle in handles:
                    set_diagnostics = getattr(handle, 'set_downstream_failure_diagnostics', None)
                    if callable(set_diagnostics):
                        set_diagnostics(failure_stage=resolved_failure_stage, validation_error_code=resolved_validation_error_code)
        for handles in handle_groups:
            finalize_token_usage_handle_group(handles, is_success=is_success)
    try:
        generation_heartbeat = _start_expedition_generation_heartbeat(expedition_log_id, generation_claim_token)
        party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
        if not party:
            current_app.logger.warning('S1 探险 AI 任务跳过：小队不存在 party=%s', party_id)
            return
        members = _load_s1_party_members_ordered(party_id)
        if not members:
            current_app.logger.warning('S1 探险 AI 任务跳过：小队无成员 party=%s', party_id)
            return
        log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).populate_existing().first()
        party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
        members = _load_s1_party_members_ordered(party_id)
        if not log or log.party_id != party_id or log.status != 'generating' or (not _owns_expedition_generation(log, generation_claim_token)) or (not party) or (not members):
            current_app.logger.warning('S1 主剧情准备阶段重新加载失败 party=%s log=%s', party_id, expedition_log_id)
            return
        description_queued = enqueue_member_description_prewarm(members, expedition_log_id=expedition_log_id)
        if description_queued:
            current_app.logger.info('S1 资产描述异步压缩已入队 party=%s log=%s items=%s', party_id, expedition_log_id, description_queued)
        source_dungeon = log.target_dungeon or party.target_dungeon or S1_DUNGEONS[0]['name']
        target_dungeon = log.hidden_dungeon or source_dungeon
        dungeon_info = get_s1_dungeon_info(target_dungeon)
        member_snapshot_ids = [int(member.id) for member in members]
        generation_checkpoint = _load_s1_expedition_generation_checkpoint(dict(log.full_log or {}), expedition_log_id=expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids)
        raw_checkpoint = (log.full_log or {}).get(_S1_GENERATION_CHECKPOINT_KEY)
        unusable_frozen_checkpoint = generation_checkpoint is None and isinstance(raw_checkpoint, dict) and ('member_generation_input' in raw_checkpoint)
        if unusable_frozen_checkpoint:
            generation_checkpoint = deepcopy(raw_checkpoint)
        from plugins.adventurer_s1.services.adventurer_s1_prompt_service import build_codename_alias
        if generation_checkpoint is not None and 'member_generation_input' in generation_checkpoint:
            try:
                if unusable_frozen_checkpoint:
                    raise ValueError('generation_checkpoint_invalid')
                member_snapshots, item_dict, item_prompt_dict, audit_use_condition_by_name, dining_context, name_alias = _restore_s1_member_generation_input(generation_checkpoint['member_generation_input'], member_ids=member_snapshot_ids)
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                cached_input = dict(log.full_log or {})
                raw_buffs = generation_checkpoint.get('consumed_buffs')
                known_buffs = [deepcopy(row) for row in (raw_buffs if isinstance(raw_buffs, list) else []) if isinstance(row, dict) and type(row.get('adventurer_id')) is int and (row['adventurer_id'] in member_snapshot_ids) and isinstance(row.get('buff'), dict)] if generation_checkpoint.get('expedition_log_id') == expedition_log_id and generation_checkpoint.get('party_id') == party_id and (generation_checkpoint.get('member_ids') == member_snapshot_ids) else []
                _apply_expedition_ai_result(expedition_log_id, party_id, members, None, pre_events=list(cached_input.get('pending_pre_events', cached_input.get('events', [])) or []), target_dungeon=target_dungeon, has_special_event=bool(cached_input.get('has_special_event')), is_extreme_crisis=bool(cached_input.get('is_extreme_crisis')), special_event_type=cached_input.get('special_event_type'), special_event_risk_tier=cached_input.get('special_event_risk_tier'), stamina_max=int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100)), stamina_cost=int(current_app.config.get('ADVENTURER_S1_STAMINA_COST_PER_DUNGEON', 80)), consumed_buffs=known_buffs, generation_failure_detail=f'member_generation_input_invalid:{type(exc).__name__}', generation_claim_token=generation_claim_token)
                return
        else:
            item_dict, audit_use_condition_by_name = _build_item_catalog_context_for_members(members)
            description_ai_context = build_member_description_ai_context(members)
            item_prompt_dict = dict(item_dict)
            item_prompt_dict.update(description_ai_context.get('item_prompt_dict') or {})
            member_snapshots, dining_context = _build_s1_expedition_member_snapshots(members, description_ai_context=description_ai_context)
            if generation_checkpoint is not None:
                legacy_full_log = dict(log.full_log or {})
                legacy_by_id = {row.get('adventurer_id'): row for row in legacy_full_log.get('audit_member_snapshot') or [] if isinstance(row, dict)}
                for member in member_snapshots:
                    frozen_member = legacy_by_id.get(member.id) or {}
                    for key in ('hp', 'max_hp', 'strength', 'agility', 'intelligence', 'luck', 'level', 'gold', 'gender', 'character_class', 'personality'):
                        if key in frozen_member:
                            setattr(member, key, deepcopy(frozen_member[key]))
                dining_context = deepcopy(legacy_full_log.get('dining_context') or {})
            name_alias = build_codename_alias(member_snapshots)
        from plugins.adventurer_s1.services.s1_ai_input_policy_service import project_members_for_ai
        if generation_checkpoint is None:
            member_snapshots = project_members_for_ai(member_snapshots)
        restored_content_exclusions = persistent_content_exclusion_refs(dict(log.full_log or {}))
        if restored_content_exclusions:
            restored_count, removed_item_names = _filter_s1_member_snapshot_assets(member_snapshots, restored_content_exclusions)
            for removed_name in removed_item_names:
                item_prompt_dict.pop(removed_name, None)
            current_app.logger.info('S1 探险恢复已应用持久内容隔离 log=%s refs=%s removed=%s', expedition_log_id, len(restored_content_exclusions), restored_count)
        if is_s1_independent_dungeon(target_dungeon) or campaign_world(target_dungeon):
            for member in member_snapshots:
                member.history_story = ''
        member_generation_input = _freeze_s1_member_generation_input(member_snapshots, item_dict=item_dict, item_prompt_dict=item_prompt_dict, use_condition_by_name=audit_use_condition_by_name, dining_context=dining_context, name_alias=name_alias, source='departure' if generation_checkpoint is None else (generation_checkpoint.get('member_generation_input') or {}).get('source', 'legacy_recovery'))
        from plugins.adventurer_s1.services.s1_dining_service import S1DiningService
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).with_for_update().populate_existing().first()
            if not _owns_expedition_generation(log, generation_claim_token):
                _reject_lost_expedition_generation_claim(generation_claim_token)
            merged = dict(log.full_log or {})
            if generation_checkpoint is None:
                merged['dining_context'] = dining_context
                merged['audit_member_snapshot'] = _build_s1_audit_member_snapshot(member_snapshots, item_dict, use_condition_by_name=audit_use_condition_by_name, name_alias=name_alias)
                merged['_companion_lineage_context'] = _build_s1_companion_lineage_context(member_snapshots)
                consumed_buffs = S1DiningService.consume_buffs_for_members(members)
                generation_checkpoint = {'version': _S1_GENERATION_CHECKPOINT_VERSION, 'expedition_log_id': expedition_log_id, 'party_id': party_id, 'member_ids': member_snapshot_ids, 'resume_stage': 'main_ai', 'review_attempt': 1, 'objective_outline': merged.get('objective_outline') if isinstance(merged.get('objective_outline'), dict) else None, 'event_resolution': None, 'candidate': None, 'observer_feedback': None, 'event_resolution_regeneration_basis': None, 'retry_errors': [], 'retry_trace': [], 'consumed_buffs': consumed_buffs}
                merged[_S1_GENERATION_CHECKPOINT_KEY] = _clone_s1_generation_value(generation_checkpoint)
            else:
                consumed_buffs = list(generation_checkpoint.get('consumed_buffs') or [])
            generation_checkpoint['member_generation_input'] = member_generation_input
            merged[_S1_GENERATION_CHECKPOINT_KEY] = _clone_s1_generation_value(generation_checkpoint)
            merged['_member_id_to_name'] = {str(member.id): member.name for member in member_snapshots if member.name}
            log.full_log = merged
            log.generation_phase = generation_checkpoint['resume_stage']
            log.generation_phase_at = _portable_utc_now()
            log.generation_owner_boot_id = generation_claim_token
        if generation_checkpoint is None:
            current_app.logger.warning('S1 探险 AI 无法建立恢复点，中止本轮 party=%s log=%s', party_id, expedition_log_id)
            return
        full_log = dict(log.full_log or {}) if log else {}
        has_special_event = bool(full_log.get('has_special_event'))
        is_extreme_crisis = bool(full_log.get('is_extreme_crisis'))
        special_event_type = full_log.get('special_event_type')
        special_event_risk_tier = full_log.get('special_event_risk_tier')
        if has_special_event and (not special_event_risk_tier):
            special_event_risk_tier = classify_s1_special_event_risk(get_s1_dungeon_info(source_dungeon)['min_level'], is_extreme_crisis=is_extreme_crisis, special_event_type=special_event_type)
        hidden_boss_context = full_log.get('hidden_boss_context') if isinstance(full_log, dict) else None
        commission_context = full_log.get('commission_context') if isinstance(full_log, dict) else None
        special_dungeon_context = full_log.get('special_dungeon_context') if isinstance(full_log, dict) else None
        hidden_dungeon_context = full_log.get('hidden_dungeon_context') if isinstance(full_log, dict) else None
        narrative_seed = full_log.get('narrative_seed') if isinstance(full_log, dict) else None
        narrative_seed = narrative_seed if isinstance(narrative_seed, dict) else None
        skip_length_checks = bool(special_dungeon_context) or is_s1_special_dungeon(target_dungeon)
        _, special_event_mode = _build_s1_special_event_prompt_context(has_special_event, is_extreme_crisis, special_event_type, risk_tier=special_event_risk_tier)
        special_event_mode = _build_s1_special_event_mode_note(special_event_mode, target_dungeon=target_dungeon)
        hidden_dungeon_prompt_block = build_hidden_dungeon_prompt_block(hidden_dungeon_context)
        if hidden_dungeon_prompt_block:
            special_event_mode = f'{special_event_mode}\n\n{hidden_dungeon_prompt_block}'
        if 'pending_pre_events' in full_log:
            pre_events = list(full_log.get('pending_pre_events') or [])
        else:
            pre_events = list(full_log.get('events') or [])
        stamina_max = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
        stamina_cost = int(current_app.config.get('ADVENTURER_S1_STAMINA_COST_PER_DUNGEON', 80))
        party_name = log.party_name if log else None
        db_manager.remove()
        observer_review_enabled = _coerce_bool(current_app.config.get('ADVENTURER_S1_EXPEDITION_OBSERVER_REVIEW_ENABLED', False), False)
        from plugins.adventurer_s1.services.adventurer_s1_expedition_observer_ai_service import CONSUMED_ASSET_CONTINUITY_REVIEWED_CONTEXT_FIELD, COUNTERMEASURE_OPPORTUNITY_REVIEWS_FIELD, DECISIVE_CAPABILITY_REVIEWS_FIELD, DEFERRED_OBSERVER_SUBCONTRACTS_FIELD, OBSERVER_CALL_CORRELATION_ID_FIELD, OBSERVER_SEMANTIC_VERDICT_CAN_PASS_FIELD, OBSERVER_STORY_RELEASE_AUTHORIZED_FIELD, OBSERVER_STORY_RELEASE_BASIS_FIELD, TARGETED_SPECIALIZED_REVIEW_CLASS_TRANSFER, TARGETED_SPECIALIZED_REVIEW_DM_INSTRUCTION, TARGETED_SPECIALIZED_REVIEW_LOCATION_STATE, TARGETED_SPECIALIZED_REVIEW_NEW_EQUIPMENT_QUANTITY, TARGETED_SPECIALIZED_REVIEW_TRAIT_PERSISTENCE, TRAIT_PERSISTENCE_PROGRESS_FIELD, TRAIT_PERSISTENCE_PUBLICATION_BLOCKED_FIELD, requires_decisive_capability_review, review_expedition_result_for_gate
        from plugins.adventurer_s1.services.s1_item_use_condition_service import build_expedition_equipment_context, class_transfer_invitation_member_ids
        configured_review_attempts = max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_REVIEW_MAX_ATTEMPTS', 2), 2))
        max_review_attempts = min(configured_review_attempts, 2)
        review_infra_attempt_limit = max(max_review_attempts, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_REVIEW_INFRA_MAX_ATTEMPTS', 3), 3))
        generation_infra_attempt_limit = max(1, min(100, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_GENERATION_INFRA_MAX_ATTEMPTS', 3), 3)))
        location_conflict_review_attempt_limit = max(max_review_attempts, _coerce_int(current_app.config.get('ADVENTURER_S1_LOCATION_CONFLICT_REVIEW_MAX_ATTEMPTS', 4), 4))
        from plugins.adventurer_s1.services.s1_trait_context_projection_service import TRAIT_CONTEXT_SELECTION_LOG_FIELD
        actual_trait_context_receipt = _clone_s1_generation_value(full_log.get(TRAIT_CONTEXT_SELECTION_LOG_FIELD))
        pipeline_recovery_context = {'member_ids': list(member_snapshot_ids), 'pre_events': list(pre_events), 'target_dungeon': target_dungeon, 'has_special_event': has_special_event, 'is_extreme_crisis': is_extreme_crisis, 'special_event_type': special_event_type, 'special_event_risk_tier': special_event_risk_tier, 'stamina_max': stamina_max, 'stamina_cost': stamina_cost, 'consumed_buffs': list(consumed_buffs or []), 'consumer_max_attempts': review_infra_attempt_limit, 'generation_infra_max_attempts': generation_infra_attempt_limit}
        _, use_condition_by_name = build_expedition_equipment_context(member_snapshots)
        from plugins.adventurer_s1.services.s1_temporary_location_service import expedition_asset_dungeon
        post_member_context = _build_s1_member_validation_context(member_snapshots, expedition_asset_dungeon(full_log, target_dungeon), use_condition_by_name, item_prompt_dict)
        checkpoint_stage = generation_checkpoint.get('resume_stage') if isinstance(generation_checkpoint, dict) else 'main_ai'
        observer_feedback: str | None = generation_checkpoint.get('observer_feedback') if isinstance(generation_checkpoint, dict) else None
        apply_result_checkpoint_candidate: dict | None = project_s1_settlement_fields(_clone_s1_generation_value(generation_checkpoint.get('candidate'))) if checkpoint_stage == 'apply_result' and isinstance(generation_checkpoint.get('candidate'), dict) else None
        apply_result_released_after_review_failure = apply_result_checkpoint_candidate is not None and _has_s1_review_failure_story_release(generation_checkpoint.get('retry_trace'))
        apply_result_requires_original_source_review = bool(apply_result_released_after_review_failure and (not isinstance(generation_checkpoint.get('review_failure_fallback_candidate'), dict)))
        checkpoint_fate_context = full_log.get('fate_journey_context')
        if apply_result_checkpoint_candidate is not None and isinstance(checkpoint_fate_context, dict):
            apply_result_checkpoint_candidate = dict(apply_result_checkpoint_candidate)
            apply_result_checkpoint_candidate['fate_journey_context'] = _clone_s1_generation_value(checkpoint_fate_context)
        apply_result_requires_final_fate_review = bool(apply_result_checkpoint_candidate and final_fate_destructive_change_requires_review(apply_result_checkpoint_candidate))
        frozen_objective_outline: dict | None = generation_checkpoint.get('objective_outline') or (full_log.get('objective_outline') if isinstance(full_log.get('objective_outline'), dict) else None)
        apply_result_requires_countermeasure_review = False
        if apply_result_checkpoint_candidate is not None and objective_actor_revisions(frozen_objective_outline):
            from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD, countermeasure_application_receipt_envelope_is_valid, frozen_countermeasure_knowledge_refs
            checkpoint_countermeasure_context = dict(full_log)
            checkpoint_countermeasure_context.update(apply_result_checkpoint_candidate)
            checkpoint_countermeasure_context['objective_outline'] = frozen_objective_outline
            apply_result_requires_countermeasure_review = any(frozen_countermeasure_knowledge_refs(checkpoint_countermeasure_context).values()) and (not countermeasure_application_receipt_envelope_is_valid(apply_result_checkpoint_candidate.get(COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD), expedition_log_id=int(expedition_log_id), full_log=checkpoint_countermeasure_context, require_zero_offset=True))
        resource_ecology_requires_review = bool(isinstance(frozen_objective_outline, dict) and isinstance(frozen_objective_outline.get('resource_ecology'), dict) and frozen_objective_outline.get('resource_ecology'))
        from plugins.adventurer_s1.services.s1_dungeon_ecology_identity_service import ECOLOGY_IDENTITY_OUTLINE_FIELD, ECOLOGY_IDENTITY_REVIEW_LOG_FIELD
        ecology_identity_review_context, ecology_identity_requires_review = (None, False)
        if not resource_ecology_requires_review:
            ecology_identity_review_context = None
            ecology_identity_requires_review = False
        from .s1_team_loot_service import capture_team_loot_review
        checkpoint_loot_authority = capture_team_loot_review(apply_result_checkpoint_candidate) if apply_result_checkpoint_candidate is not None else None
        apply_result_requires_team_loot_review = bool(apply_result_checkpoint_candidate and apply_result_checkpoint_candidate.get('unassigned_items') and (checkpoint_loot_authority is None or (checkpoint_loot_authority.get('version') == 2 and checkpoint_loot_authority['expedition_log_id'] != expedition_log_id)))
        approved_result: dict | None = apply_result_checkpoint_candidate if not apply_result_requires_original_source_review and (not apply_result_requires_team_loot_review) and (apply_result_released_after_review_failure or (not apply_result_requires_final_fate_review and (not apply_result_requires_countermeasure_review) and (not resource_ecology_requires_review) and (not ecology_identity_requires_review))) else None
        review_rejected_all = False
        generation_failure_detail: str | None = None
        generation_retry_trace = list(generation_checkpoint.get('retry_trace') or [])
        unexpected_consumer_fail_close = _s1_unexpected_consumer_retry_exhausted(generation_retry_trace)
        generation_infrastructure_fail_close = _s1_generation_infrastructure_retry_exhausted(generation_retry_trace)
        checkpoint_fail_close = unexpected_consumer_fail_close or generation_infrastructure_fail_close
        if generation_infrastructure_fail_close:
            approved_result = None
            generation_failure_detail = '探险上游 AI 基础设施中断连续达到上限，已停止自动重试。'
        elif unexpected_consumer_fail_close:
            approved_result = None
            generation_failure_detail = '探险检查点下游消费连续异常，已停止自动重试。'
        location_conflict_count = sum((1 for entry in generation_retry_trace if isinstance(entry, dict) and entry.get('reason_code') == 'location_state_conflict'))
        checkpoint_review_attempt = max(1, int(generation_checkpoint.get('review_attempt') or 1))
        if checkpoint_stage == 'review_ai':
            max_review_attempts = max(max_review_attempts, min(checkpoint_review_attempt, review_infra_attempt_limit))
        if location_conflict_count >= 2:
            max_review_attempts = max(max_review_attempts, checkpoint_review_attempt)
        from plugins.adventurer_s1.services.s1_encounter_draft_contract import restore_compact_draft
        frozen_event_resolution = restore_compact_draft(generation_checkpoint.get('event_resolution'), member_snapshots)
        if frozen_event_resolution is not None:
            full_log['pre_narrative_resolution'] = deepcopy(frozen_event_resolution)
        else:
            full_log.pop('pre_narrative_resolution', None)
        event_resolution_regeneration_basis: str | None = generation_checkpoint.get('event_resolution_regeneration_basis')
        review_retry_candidate: dict | None = _clone_s1_generation_value(generation_checkpoint.get('candidate')) if checkpoint_stage in {'review_ai', 'settlement_guard_ai'} and isinstance(generation_checkpoint.get('candidate'), dict) else apply_result_checkpoint_candidate if apply_result_requires_original_source_review or apply_result_requires_team_loot_review or apply_result_requires_final_fate_review or apply_result_requires_countermeasure_review or resource_ecology_requires_review or ecology_identity_requires_review else None
        review_retry_errors: list[str] = list(generation_checkpoint.get('retry_errors') or [])
        review_failure_fallback_candidate = _clone_s1_generation_value(generation_checkpoint.get('review_failure_fallback_candidate'))
        history_anchor_repair_errors: list[str] = []
        history_anchor_repairs = matching_s1_history_anchor_repairs(review_retry_candidate or {}, generation_checkpoint.get('history_anchor_repairs') or {})
        resume_guard_only = checkpoint_stage == 'settlement_guard_ai'
        guard_resume_reuses_current_observer = False
        first_review_attempt = max(1, min(checkpoint_review_attempt, max_review_attempts))
        if (apply_result_requires_original_source_review or apply_result_requires_final_fate_review or apply_result_requires_countermeasure_review or resource_ecology_requires_review or ecology_identity_requires_review) and location_conflict_count == 0:
            first_review_attempt = 1
        review_attempt_sequence = list(range(first_review_attempt, max_review_attempts + 1) if approved_result is None and (not checkpoint_fail_close) else ())
        location_extra_review_may_regenerate_main = False

        def claim_location_changes(candidate: dict) -> tuple[dict | None, list[str], bool, bool]:
            if is_s1_independent_dungeon(target_dungeon):
                from .s1_independent_dungeon_service import project_independent_dungeon_result
                project_independent_dungeon_result(candidate, target_dungeon)
                return (candidate, [], False, False)
            from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
            snapshot = S1WorldLocationStateService.load_snapshot(expedition_log_id)
            from plugins.adventurer_s1.services.s1_temporary_location_service import temporary_location_from_context
            destination = temporary_location_from_context(snapshot)
            stable_identity_change_ignored = bool(candidate.get('dungeon_explorability_change') and S1WorldLocationStateService.has_persistent_dungeon_identity(target_dungeon))
            explorability_initialization_deferred = bool('is_explorable' in snapshot and snapshot.get('is_explorable') is None and (not candidate.get('dungeon_explorability_change')))
            changes = list(candidate.get('location_state_changes') or [])
            claimed, errors = S1WorldLocationStateService.claim_changes(expedition_log_id, target_dungeon, changes, snapshot)
            if errors:
                return (None, errors, False, False)
            candidate['location_state_changes'] = claimed
            if destination:
                candidate['dungeon_explorability_change'] = None
                return (candidate, [], False, stable_identity_change_ignored)
            if explorability_initialization_deferred:
                candidate['dungeon_explorability_change'] = None
            explorability_change, errors = S1WorldLocationStateService.claim_explorability_change(target_dungeon, candidate.get('dungeon_explorability_change'), snapshot, world_state_change_allowed=bool(isinstance(frozen_objective_outline, dict) and frozen_objective_outline.get('world_state_change_allowed') is True))
            if errors:
                return (None, errors, False, False)
            candidate['dungeon_explorability_change'] = explorability_change
            return (candidate, [], explorability_initialization_deferred, stable_identity_change_ignored)

        def queue_downstream_retry(candidate: dict, errors: list[str], *, resume_stage: str='review_ai') -> None:
            nonlocal review_retry_candidate, review_retry_errors
            nonlocal resume_guard_only, guard_resume_reuses_current_observer
            nonlocal history_anchor_repairs
            history_anchor_repairs = matching_s1_history_anchor_repairs(candidate, history_anchor_repairs)
            review_retry_candidate = json.loads(json.dumps(candidate, ensure_ascii=False))
            review_retry_errors = [str(error)[:1000] for error in dict.fromkeys([*history_anchor_repair_errors, *errors]) if str(error).strip()][:20]
            resume_guard_only = resume_stage == 'settlement_guard_ai'
            guard_resume_reuses_current_observer = resume_guard_only
            if resume_guard_only:
                finalize_token_usage_handle_group(deferred_observer_ai_token_usage_handles, is_success=True)
            _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids, resume_stage=resume_stage, review_attempt=min(review_attempt + 1, max_review_attempts), generation_claim_token=generation_claim_token, objective_outline=frozen_objective_outline, event_resolution=frozen_event_resolution, candidate=review_retry_candidate, review_failure_fallback_candidate=review_failure_fallback_candidate, trait_context_receipt=actual_trait_context_receipt, observer_feedback=observer_feedback, event_resolution_regeneration_basis=event_resolution_regeneration_basis, retry_errors=review_retry_errors, history_anchor_repairs=history_anchor_repairs, retry_trace=generation_retry_trace)

        def checkpoint_approved_result(candidate: dict, review_attempt: int, *, release_unreviewed_ordinary_loot: bool=False) -> None:
            pending_story_release = _pending_s1_review_failure_story_release(generation_retry_trace)
            if candidate.get('unassigned_items') and capture_team_loot_review(candidate) is None:
                from .s1_team_loot_service import authorize_team_loot_original_release, TeamLootError
                original_release = bool(release_unreviewed_ordinary_loot or pending_story_release is not None or _has_s1_review_failure_story_release(generation_retry_trace))
                if not original_release or not isinstance(review_failure_fallback_candidate, dict):
                    raise TeamLootError('共同实物尚未获得可发布的同源授权')
                authorize_team_loot_original_release(candidate, review_failure_fallback_candidate, roster=audit_member_snapshot, log_id=expedition_log_id, attempt=review_attempt)
            if release_unreviewed_ordinary_loot and _has_s1_retained_physical_loot_settlement(candidate) and (pending_story_release is None) and (not _has_s1_review_failure_story_release(generation_retry_trace)):
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=_S1_ORDINARY_PHYSICAL_LOOT_RELEASE_REASON_CODE, action=S1RetryAction.RELEASE_STORY_AFTER_REVIEW_FAILURE, attempt=review_attempt, max_attempts=review_infra_attempt_limit, preserves_main_result=True, detail='Observer 本次不可用；普通实物掉落已通过最终机械校验，保留主剧情候选中的精确数量与逐人归属继续结算。')
            elif pending_story_release is not None:
                release_reason_code, release_detail = pending_story_release
                if release_reason_code == 'observer_error_original_result_released':
                    conflicts = [row for row in _build_s1_same_name_gain_review_context(candidate, audit_member_snapshot) if row.get('conflict_kind') == 'equipment_update_final_name']
                    if conflicts and _validate_s1_same_name_gain_review_receipt({**candidate, 'audit_member_snapshot': audit_member_snapshot}):
                        candidate[_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD] = {'schema_version': 2, 'status': 'review_unavailable_original_preserved', 'source': 'runner_final_mechanical_validation', 'reason_code': release_reason_code, 'context_digest': _s1_same_name_gain_review_context_digest(conflicts), 'reviews': []}
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=release_reason_code, action=S1RetryAction.RELEASE_STORY_AFTER_REVIEW_FAILURE, attempt=review_attempt, max_attempts=review_infra_attempt_limit, preserves_main_result=True, detail=release_detail)
            _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids, resume_stage='apply_result', review_attempt=review_attempt, generation_claim_token=generation_claim_token, objective_outline=frozen_objective_outline, event_resolution=frozen_event_resolution, candidate=candidate, review_failure_fallback_candidate=review_failure_fallback_candidate, trait_context_receipt=actual_trait_context_receipt, observer_feedback=None, event_resolution_regeneration_basis=None, retry_errors=[], history_anchor_repairs=history_anchor_repairs, retry_trace=generation_retry_trace)

        def resolve_location_claim_conflict(candidate: dict, errors: list[str], *, review_attempt: int) -> tuple[dict | None, bool]:
            """刷新快照后复审；确认兼容时只延期辅助写回。"""
            nonlocal location_conflict_count
            nonlocal location_extra_review_may_regenerate_main
            nonlocal max_review_attempts
            from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
            reviewed_snapshot = full_log.get('location_state_snapshot')
            from plugins.adventurer_s1.services.s1_temporary_location_service import temporary_location_from_context
            destination = temporary_location_from_context(reviewed_snapshot)
            refreshed_snapshot = dict(reviewed_snapshot) if destination else S1WorldLocationStateService.build_snapshot(target_dungeon)
            from plugins.adventurer_s1.services.s1_expedition_locus_service import prepare_locus_location_snapshot
            _, refreshed_snapshot = prepare_locus_location_snapshot(expedition_log_id, target_dungeon, refreshed_snapshot, preserve_snapshot=bool(destination))
            refreshed_snapshot = S1WorldLocationStateService.include_proposed_location_states(refreshed_snapshot, [{'location_id': destination['location_id']}] if destination else candidate.get('location_state_changes') or [])
            S1WorldLocationStateService.persist_snapshot(expedition_log_id, refreshed_snapshot)
            persisted_snapshot = S1WorldLocationStateService.load_snapshot(expedition_log_id)
            full_log['location_state_snapshot'] = persisted_snapshot or refreshed_snapshot
            current_snapshot = full_log['location_state_snapshot']

            def state_signature(snapshot: object) -> tuple:
                if not isinstance(snapshot, dict):
                    return ()
                locations = tuple(sorted(((int(row.get('location_id') or 0), str(row.get('location_name') or ''), int(row.get('revision') or 0), str(row.get('current_state') or ''), str(row.get('public_risk_notice') or '')) for row in snapshot.get('locations') or [] if isinstance(row, dict))))
                return (str(snapshot.get('target_dungeon') or ''), snapshot.get('is_explorable'), snapshot.get('identity_is_persistent') is True, locations)
            latest_state_changed = state_signature(reviewed_snapshot) != state_signature(current_snapshot)
            location_conflict_count += 1
            detail = '; '.join(errors[:3])

            def defer_auxiliary_location_write(reason_code: str, log_message: str) -> tuple[dict, bool]:
                deferred_candidate = dict(candidate)
                deferred_candidate['location_state_changes'] = []
                deferred_candidate['dungeon_explorability_change'] = None
                _record_s1_retry_decision(generation_retry_trace, stage='location_state_claim', reason_code=reason_code, action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=detail)
                current_app.logger.warning(log_message, expedition_log_id, review_attempt, location_conflict_count, detail)
                return (deferred_candidate, False)
            if not latest_state_changed:
                return defer_auxiliary_location_write('location_state_write_deferred_unchanged_authority', 'S1 地点写入冲突但权威现状未变化，直接延期辅助写回并发布同一主剧情 log=%s attempt=%s conflicts=%s error=%s')
            if location_conflict_count == 1 and review_attempt < max_review_attempts:
                _record_s1_retry_decision(generation_retry_trace, stage='location_state_claim', reason_code='location_state_conflict', action=S1RetryAction.RETRY_LOCATION_REVIEW, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=detail)
                queue_downstream_retry(candidate, errors)
                current_app.logger.info('S1 地点状态冲突已刷新权威快照，仅复审同一主剧情 log=%s 第 %s/%s 次：%s', expedition_log_id, review_attempt, max_review_attempts, detail)
                return (None, True)
            if location_conflict_count == 2 and latest_state_changed and (max_review_attempts < location_conflict_review_attempt_limit):
                max_review_attempts += 1
                next_attempt = review_attempt + 1
                if next_attempt not in review_attempt_sequence:
                    review_attempt_sequence.append(next_attempt)
                location_extra_review_may_regenerate_main = True
                _record_s1_retry_decision(generation_retry_trace, stage='location_state_claim', reason_code='location_state_conflict', action=S1RetryAction.RETRY_LOCATION_REVIEW, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=f'{detail}; 最新权威状态再次变化，追加一次有界兼容性复审')
                queue_downstream_retry(candidate, errors)
                current_app.logger.info('S1 第二次地点冲突伴随权威状态变化，追加一次最新态复审 log=%s next_attempt=%s error=%s', expedition_log_id, next_attempt, detail)
                return (None, True)
            return defer_auxiliary_location_write('location_state_write_deferred_after_refresh', 'S1 地点状态刷新后仍有并发冲突，丢弃本次辅助地点写回并发布同一主剧情 log=%s attempt=%s conflicts=%s error=%s')
        final_review_attempt = checkpoint_review_attempt
        for review_attempt in review_attempt_sequence:
            final_review_attempt = review_attempt
            finalize_deferred_ai_token_usage(is_success=False, include_main_ai=review_retry_candidate is None)
            if review_attempt > first_review_attempt:
                members = _reload_s1_party_members(party_id)
                if not members:
                    current_app.logger.error('S1 探险重审失败：无法刷新成员 party=%s log=%s attempt=%s', party_id, expedition_log_id, review_attempt)
                    break
            pending_review_errors = review_retry_errors
            review_retry_errors = []
            main_result_generated = review_retry_candidate is None
            if review_retry_candidate is not None:
                result_data = json.loads(json.dumps(review_retry_candidate, ensure_ascii=False))
                review_retry_candidate = None
                main_ai_failure = None
            else:
                from plugins.adventurer_s1.models import S1_EXPEDITION_GENERATION_PHASE_MAIN_AI
                history_anchor_repairs = {}
                review_failure_fallback_candidate = None
                if not _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids, resume_stage=S1_EXPEDITION_GENERATION_PHASE_MAIN_AI, review_attempt=review_attempt, generation_claim_token=generation_claim_token, objective_outline=frozen_objective_outline, event_resolution=frozen_event_resolution, candidate=None, observer_feedback=observer_feedback, event_resolution_regeneration_basis=event_resolution_regeneration_basis, retry_errors=[], retry_trace=generation_retry_trace):
                    current_app.logger.info('S1 探险 AI 主生成中止：日志已离开 generating party=%s log=%s', party_id, expedition_log_id)
                    return
                result_data, main_ai_failure = _call_main_expedition_ai(member_snapshots, item_prompt_dict, target_dungeon, dungeon_info, special_event_mode=special_event_mode, party_id=party_id, expedition_log_id=expedition_log_id, special_event_type=special_event_type, special_event_risk_tier=special_event_risk_tier, has_special_event=has_special_event, is_extreme_crisis=is_extreme_crisis, hidden_boss_context=hidden_boss_context, campaign_dispatch=full_log.get('campaign_dispatch'), commission_context=commission_context, use_condition_by_name=use_condition_by_name, initial_validation_feedback=observer_feedback, name_alias=name_alias, special_dungeon_context=special_dungeon_context, narrative_seed=narrative_seed, skip_length_checks=skip_length_checks, allow_observer_equipment_repairs=True, frozen_objective_outline=frozen_objective_outline, frozen_event_resolution=frozen_event_resolution, event_resolution_regeneration_basis=event_resolution_regeneration_basis, checkpoint_review_attempt=review_attempt, generation_claim_token=generation_claim_token, physical_attempt_guard=require_current_generation_claim, hidden_dungeon_context=hidden_dungeon_context, retry_trace=generation_retry_trace)
                actual_trait_context_receipt = _clone_s1_generation_value(getattr(result_data, 'trait_context_receipt', None))
                if result_data is not None:
                    frozen_event_resolution = restore_compact_draft(getattr(result_data, 'event_resolution', None), member_snapshots)
                main_token_usage = getattr(result_data, 'deferred_token_usage', None)
                if main_token_usage is not None:
                    deferred_main_ai_token_usage_handles.append(main_token_usage)
            if result_data is None:
                generation_failure_detail = main_ai_failure
                latest_failure = generation_retry_trace[-1] if generation_retry_trace else {}
                already_recorded_terminal_failure = latest_failure.get('action') == S1RetryAction.FAIL_AND_DISBAND.value and latest_failure.get('detail') == main_ai_failure
                if not already_recorded_terminal_failure:
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_main', reason_code='generation_attempts_exhausted', action=S1RetryAction.FAIL_AND_DISBAND, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=False, detail=main_ai_failure)
                break
            if frozen_objective_outline is None and isinstance(result_data.get('objective_outline'), dict):
                frozen_objective_outline = json.loads(json.dumps(result_data['objective_outline'], ensure_ascii=False))
            resource_ecology_requires_review = bool(isinstance(frozen_objective_outline, dict) and isinstance(frozen_objective_outline.get('resource_ecology'), dict) and frozen_objective_outline.get('resource_ecology'))
            result_data.pop('pre_narrative_resolution', None)
            result_data.pop('pre_narrative_life_adjustments', None)
            full_log.pop('pre_narrative_resolution', None)
            if frozen_event_resolution is not None:
                result_data['pre_narrative_resolution'] = deepcopy(frozen_event_resolution)
                full_log['pre_narrative_resolution'] = deepcopy(frozen_event_resolution)
            latest_context_log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).populate_existing().first()
            latest_context_full_log = latest_context_log.full_log if latest_context_log and isinstance(latest_context_log.full_log, dict) else {}
            from plugins.adventurer_s1.services.s1_trait_context_projection_service import TRAIT_CONTEXT_SELECTION_LOG_FIELD
            from plugins.adventurer_s1.services.s1_countermeasure_learning_contract import refresh_learning_frozen_context
            refresh_learning_frozen_context(full_log, latest_context_full_log)
            for context_key in ('world_intel_adoptions', '_ecology_identity_snapshot', '_objective_mode_snapshot', '_expedition_locus_snapshot', '_campaign_departure_decision', '_temporary_location', TRAIT_CONTEXT_SELECTION_LOG_FIELD, 'cross_realm_mirror_injection'):
                if latest_context_full_log.get(context_key) is not None:
                    full_log[context_key] = latest_context_full_log[context_key]
            if isinstance(actual_trait_context_receipt, dict):
                full_log[TRAIT_CONTEXT_SELECTION_LOG_FIELD] = actual_trait_context_receipt
            post_member_context = _build_s1_member_validation_context(member_snapshots, expedition_asset_dungeon(full_log, target_dungeon), use_condition_by_name, item_prompt_dict)
            ecology_identity_review_context, ecology_identity_requires_review = (None, False)
            if not resource_ecology_requires_review:
                ecology_identity_review_context = None
                ecology_identity_requires_review = False
            from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
            latest_location_snapshot = S1WorldLocationStateService.load_snapshot(expedition_log_id)
            if latest_location_snapshot:
                full_log['location_state_snapshot'] = latest_location_snapshot
            from plugins.adventurer_s1.services.s1_waiting_player_npc_service import load_waiting_player_npc_injection
            latest_waiting_npc_snapshot = load_waiting_player_npc_injection(expedition_log_id)
            if latest_waiting_npc_snapshot:
                full_log['waiting_player_npc_injection'] = latest_waiting_npc_snapshot
            frozen_fate_context = full_log.get('fate_journey_context')
            if isinstance(frozen_fate_context, dict):
                result_data = dict(result_data)
                result_data['fate_journey_context'] = json.loads(json.dumps(frozen_fate_context, ensure_ascii=False))
            result_data, final_fate_destructive_review_context, final_fate_destructive_context_errors = prepare_final_fate_destructive_context(result_data, full_log.get('audit_member_snapshot'), expedition_log_id=expedition_log_id)
            if final_fate_destructive_context_errors:
                _record_s1_retry_decision(generation_retry_trace, stage='final_fate_destructive_change_freeze', reason_code='invalid_destructive_payload_discarded', action=S1RetryAction.REPAIR_LOCAL, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(final_fate_destructive_context_errors[:10]))
                current_app.logger.warning('S1 最终命途非法永久删除提案已丢弃，保留主剧情与其它结算 log=%s errors=%s', expedition_log_id, final_fate_destructive_context_errors[:10])
            final_fate_destructive_requires_review = final_fate_destructive_change_requires_review(result_data)
            location_state_requires_review = bool(result_data.get('location_state_changes') or (isinstance(full_log.get('location_state_snapshot'), dict) and full_log['location_state_snapshot'].get('locations')))
            same_name_gain_review_context = _build_s1_same_name_gain_review_context(result_data, full_log.get('audit_member_snapshot'))
            same_name_gain_requires_review = bool(same_name_gain_review_context)
            consumed_asset_continuity_review_context = _build_s1_consumed_asset_continuity_review_context(result_data, full_log.get('audit_member_snapshot'))
            consumed_asset_continuity_requires_review = bool(consumed_asset_continuity_review_context)
            trait_persistence_requires_review = any((isinstance(update, dict) and bool(update.get('new_traits')) for update in result_data.get('member_updates') or []))
            class_transfer_review_member_ids = class_transfer_invitation_member_ids(full_log.get('audit_member_snapshot'), expedition_asset_dungeon(full_log, target_dungeon), use_condition_by_name)
            retained_physical_loot_review_requested = _has_s1_retained_physical_loot_settlement(result_data)
            history_anchor_repairs = matching_s1_history_anchor_repairs(result_data, history_anchor_repairs)
            missing_names = result_data.pop('_missing_name_mentions', None) or {}
            observer_repair_errors = [*(result_data.pop('_observer_repair_errors', None) or []), *pending_review_errors]
            history_anchor_repair_errors = [str(error) for error in observer_repair_errors if str(error).startswith(S1_MEMBER_HISTORY_ANCHOR_ERROR_MARKER + ':')]
            history_anchor_repair_requested = bool(history_anchor_repair_errors)
            waiting_npc_repair_required = any(('waiting_npc_updates' in str(error) for error in observer_repair_errors))
            new_equipment_quantity_repair_requested = bool(observer_repair_errors and len(get_s1_new_equipment_quantity_repair_errors(observer_repair_errors)) == len(observer_repair_errors))
            waiting_npc_requires_review = waiting_npc_repair_required or bool(result_data.get('waiting_npc_updates'))
            frozen_waiting_npc_snapshot = full_log.get('waiting_player_npc_injection')
            if not isinstance(frozen_waiting_npc_snapshot, dict):
                frozen_waiting_npc_snapshot = {}
            waiting_npc_review_candidate_present = waiting_npc_requires_review or bool(frozen_waiting_npc_snapshot.get('enabled') is True and frozen_waiting_npc_snapshot.get('candidates'))
            countermeasure_outline = result_data.get('objective_outline') if isinstance(result_data.get('objective_outline'), dict) else full_log.get('objective_outline')
            required_faction_interaction_thread_ids = {_coerce_int(value, 0) for value in (countermeasure_outline or {}).get('required_faction_interaction_thread_ids') or [] if _coerce_int(value, 0) > 0}
            required_campaign_manifestation_thread_ids = {_coerce_int(value, 0) for value in (countermeasure_outline or {}).get('required_campaign_manifestation_thread_ids') or [] if _coerce_int(value, 0) > 0}
            faction_interaction_requires_review = bool(required_faction_interaction_thread_ids or required_campaign_manifestation_thread_ids)
            from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import frozen_countermeasure_knowledge_refs
            knowledge_refs = frozen_countermeasure_knowledge_refs(full_log)
            countermeasure_application_receipt_required = any((knowledge_refs.get((actor_id, revision)) for actor_id, revision in objective_actor_revisions(countermeasure_outline).items()))
            countermeasure_application_requires_review = countermeasure_application_receipt_required
            high_risk_life_review_requested = requires_s1_high_risk_adjudication({'has_special_event': has_special_event, 'is_extreme_crisis': is_extreme_crisis, 'special_event_type': special_event_type, 'special_event_risk_tier': special_event_risk_tier, 'hidden_boss_context': hidden_boss_context, 'campaign_dispatch': full_log.get('campaign_dispatch')})
            hard_specialized_observer_review_requested = bool(any((observer_repair_errors, same_name_gain_requires_review, consumed_asset_continuity_requires_review, trait_persistence_requires_review, class_transfer_review_member_ids, new_equipment_quantity_repair_requested, final_fate_destructive_requires_review, False, faction_interaction_requires_review, countermeasure_application_requires_review, location_state_requires_review, waiting_npc_repair_required, high_risk_life_review_requested, resource_ecology_requires_review, ecology_identity_requires_review)))
            lightweight_observer_review_requested = bool((observer_review_enabled or retained_physical_loot_review_requested) and (not hard_specialized_observer_review_requested))
            targeted_specialized_review_types = [*[], *([TARGETED_SPECIALIZED_REVIEW_LOCATION_STATE] if location_state_requires_review else []), *([TARGETED_SPECIALIZED_REVIEW_NEW_EQUIPMENT_QUANTITY] if new_equipment_quantity_repair_requested else []), *([TARGETED_SPECIALIZED_REVIEW_TRAIT_PERSISTENCE] if trait_persistence_requires_review else []), *([TARGETED_SPECIALIZED_REVIEW_CLASS_TRANSFER] if class_transfer_review_member_ids else [])]
            full_specialized_observer_review_requested = bool(faction_interaction_requires_review)
            decisive_capability_scope_input = dict(full_log)
            decisive_capability_scope_input.update(result_data)
            decisive_capability_review_requires_review = requires_decisive_capability_review(decisive_capability_scope_input, full_review_requested=full_specialized_observer_review_requested)
            _member_id_to_name = {m.id: m.name for m in member_snapshots if m.name}
            guard_countermeasure_envelope = None
            if resume_guard_only and countermeasure_application_receipt_required:
                from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD, countermeasure_application_receipt_envelope_is_valid, project_countermeasure_application_receipt_envelope
                guard_countermeasure_context = dict(full_log)
                guard_countermeasure_context.update(result_data)
                guard_countermeasure_context['objective_outline'] = countermeasure_outline
                raw_guard_countermeasure_envelope = result_data.get(COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD)
                if countermeasure_application_receipt_envelope_is_valid(raw_guard_countermeasure_envelope, expedition_log_id=int(expedition_log_id), full_log=guard_countermeasure_context, require_zero_offset=True):
                    guard_countermeasure_envelope = project_countermeasure_application_receipt_envelope(raw_guard_countermeasure_envelope)
                else:
                    resume_guard_only = False
            if resume_guard_only and (not guard_resume_reuses_current_observer):
                if consumed_asset_continuity_requires_review and _REVIEWED_COMPANION_STAGE_FLOORS_KEY not in result_data:
                    resume_guard_only = False
                if resume_guard_only and final_fate_destructive_requires_review:
                    resume_guard_only = False
                if resume_guard_only and decisive_capability_review_requires_review:
                    resume_guard_only = False
                if resume_guard_only and (resource_ecology_requires_review or ecology_identity_requires_review):
                    resume_guard_only = False
                if resume_guard_only and high_risk_life_review_requested:
                    resume_guard_only = False
                if resume_guard_only and trait_persistence_requires_review:
                    resume_guard_only = False
                if resume_guard_only and class_transfer_review_member_ids:
                    resume_guard_only = False
            if main_result_generated:
                review_failure_fallback_candidate = _clone_s1_generation_value(result_data)
            if resume_guard_only:
                verdict = {'infra_failed': False, 'observer_skipped': True, 'needs_regeneration': False, 'regeneration_basis': 'none', 'reason': '从已持久化的结算守卫阶段继续', 'result_data': result_data, **({COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD: guard_countermeasure_envelope} if guard_countermeasure_envelope is not None else {})}
                resume_guard_only = False
                guard_resume_reuses_current_observer = False
            elif observer_review_enabled or retained_physical_loot_review_requested or hard_specialized_observer_review_requested:
                from plugins.adventurer_s1.models import S1_EXPEDITION_GENERATION_PHASE_REVIEW_AI
                location_review_snapshot = full_log.get('location_state_snapshot')
                if isinstance(location_review_snapshot, dict):
                    from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
                    location_review_snapshot = S1WorldLocationStateService.include_proposed_location_states(location_review_snapshot, result_data.get('location_state_changes') or [], refresh_existing=False)
                    location_review_snapshot = {key: location_review_snapshot.get(key) for key in ('target_dungeon', 'is_explorable', 'identity_is_persistent', 'locations', 'captured_at', '_temporary_location')}
                if not _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids, resume_stage=S1_EXPEDITION_GENERATION_PHASE_REVIEW_AI, review_attempt=review_attempt, generation_claim_token=generation_claim_token, objective_outline=frozen_objective_outline, event_resolution=frozen_event_resolution, candidate=result_data, review_failure_fallback_candidate=review_failure_fallback_candidate, trait_context_receipt=actual_trait_context_receipt, observer_feedback=observer_feedback, event_resolution_regeneration_basis=event_resolution_regeneration_basis, retry_errors=observer_repair_errors, history_anchor_repairs=history_anchor_repairs, retry_trace=generation_retry_trace, location_state_snapshot=location_review_snapshot):
                    current_app.logger.info('S1 探险 AI 审核中止：日志已离开 generating party=%s log=%s', party_id, expedition_log_id)
                    return
                if isinstance(location_review_snapshot, dict):
                    full_log['location_state_snapshot'] = location_review_snapshot
                verdict = review_expedition_result_for_gate(expedition_log_id=expedition_log_id, party_name=party_name, target_dungeon=target_dungeon, result_data=result_data, cached_full_log=full_log, review_attempt=review_attempt, missing_name_mentions=missing_names or None, member_id_to_name=_member_id_to_name or None, same_name_gain_review_context=same_name_gain_review_context or None, consumed_asset_continuity_review_context=consumed_asset_continuity_review_context or None, final_fate_destructive_change_review_context=final_fate_destructive_review_context, structured_validation_errors=observer_repair_errors or None, waiting_npc_repair_required=waiting_npc_repair_required, full_review_requested=full_specialized_observer_review_requested, resource_ecology_review_required=resource_ecology_requires_review, ecology_identity_review_context=ecology_identity_review_context or None, lightweight_review_requested=lightweight_observer_review_requested, targeted_specialized_review_types=targeted_specialized_review_types or None, class_transfer_review_member_ids=class_transfer_review_member_ids or None, deferred_token_usage_handles=deferred_observer_ai_token_usage_handles)
                db_manager.remove()
                _touch_expedition_generation_phase(expedition_log_id, S1_EXPEDITION_GENERATION_PHASE_REVIEW_AI, generation_claim_token=generation_claim_token)
            else:
                verdict = {'infra_failed': False, 'observer_skipped': True, 'needs_regeneration': False, 'regeneration_basis': 'none', 'reason': '剧情审核 AI 已关闭，仅执行后端轻量校验', 'result_data': result_data}
            history_anchor_repairs = matching_s1_history_anchor_repairs(verdict.get('result_data') or result_data, {**history_anchor_repairs, **(verdict.get('history_anchor_repairs') or {})})
            waiting_npc_review_requested = bool(waiting_npc_repair_required or (waiting_npc_review_candidate_present and verdict.get('review_mode') == 'full'))
            countermeasure_review_deferred = bool(lightweight_observer_review_requested or verdict.get('review_mode') == 'targeted_specialized' or verdict.get('observer_skipped') is True)
            observer_skipped = verdict.get('observer_skipped') is True
            if countermeasure_review_deferred:
                verdict = dict(verdict)
                if countermeasure_review_deferred and countermeasure_application_receipt_required:
                    deferred_subcontracts = list(verdict.get(DEFERRED_OBSERVER_SUBCONTRACTS_FIELD) or [])
                    if not any((isinstance(row, dict) and row.get('field') == COUNTERMEASURE_OPPORTUNITY_REVIEWS_FIELD for row in deferred_subcontracts)):
                        deferred_subcontracts.append({'field': COUNTERMEASURE_OPPORTUNITY_REVIEWS_FIELD, 'reason_code': 'outside_lightweight_observer_scope', 'contract_error': 'countermeasure_application_review_not_dispatched'})
                    verdict[DEFERRED_OBSERVER_SUBCONTRACTS_FIELD] = deferred_subcontracts
                if observer_skipped and resource_ecology_requires_review:
                    verdict.setdefault('resource_ecology_review_record', {'status': 'deferred', 'reason_code': 'observer_disabled', 'contract_error': 'resource_ecology_review_not_dispatched'})
                    if ecology_identity_requires_review:
                        verdict.setdefault('ecology_identity_review_record', {'status': 'deferred', 'profile_id': ecology_identity_review_context.get('profile_id'), 'profile_revision': ecology_identity_review_context.get('profile_revision'), 'motif_fingerprint': ecology_identity_review_context.get('motif_fingerprint'), 'reason_code': 'observer_disabled', 'contract_error': 'ecology_identity_review_not_dispatched'})
                elif observer_skipped:
                    verdict.setdefault('resource_ecology_review_record', {'status': 'not_required', 'reason_code': 'resource_ecology_not_present'})
            observer_correlation_id = str(verdict.get(OBSERVER_CALL_CORRELATION_ID_FIELD) or '').strip()[:64] or None
            observer_regeneration = decide_s1_observer_regeneration(verdict, result_data=verdict.get('result_data') or result_data, cached_full_log=full_log)
            regeneration_proposal_rejected = bool(observer_regeneration.requested and (not observer_regeneration.authorized))
            if regeneration_proposal_rejected:
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=observer_regeneration.reason_code, action=S1RetryAction.REJECT_REGENERATION, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=str(verdict.get('reason') or '')[:1000], correlation_id=observer_correlation_id)
                current_app.logger.warning('S1 Observer 打回提案未通过后端重试权限校验，保留主剧情 log=%s attempt=%s basis=%s reason=%s', expedition_log_id, review_attempt, observer_regeneration.basis, observer_regeneration.reason_code)
                if verdict.get(OBSERVER_SEMANTIC_VERDICT_CAN_PASS_FIELD) is False:
                    verdict = {**verdict, 'infra_failed': True, 'needs_regeneration': False, 'failure_stage': 'contract_validation', 'contract_error': 'observer_regeneration_proposal_rejected'}
            if observer_regeneration.authorized:
                regeneration_basis = observer_regeneration.basis
                if regeneration_basis == PERSISTENT_CONTENT_REGENERATION_BASIS:
                    disposition = verdict.get('persistent_content_disposition')
                    application = None
                    application_error = 'persistent_content_disposition_missing'
                    if isinstance(disposition, dict):
                        application, application_error = apply_persistent_content_disposition(expedition_log_id=expedition_log_id, disposition=disposition)
                    if application is None:
                        downstream_error = f"存量内容边界复核尚未形成可执行回执：{application_error or 'unknown'}"
                        queue_downstream_retry(result_data, [downstream_error])
                        _record_s1_retry_decision(generation_retry_trace, stage='expedition_content_safety_review', reason_code='persistent_content_disposition_unavailable', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=downstream_error, correlation_id=observer_correlation_id)
                        current_app.logger.warning('S1 存量内容未先完成没收/隔离，保留同一主剧情并只重试审核 log=%s attempt=%s error=%s', expedition_log_id, review_attempt, application_error)
                        continue
                    excluded_refs = list(application.get('excluded_refs') or [])
                    removed_count, removed_item_names = _filter_s1_member_snapshot_assets(member_snapshots, excluded_refs)
                    for removed_name in removed_item_names:
                        item_prompt_dict.pop(removed_name, None)
                    _, use_condition_by_name = build_expedition_equipment_context(member_snapshots)
                    post_member_context = _build_s1_member_validation_context(member_snapshots, expedition_asset_dungeon(full_log, target_dungeon), use_condition_by_name, item_prompt_dict)
                    persisted_full_log = application.get('full_log')
                    if isinstance(persisted_full_log, dict):
                        full_log = persisted_full_log
                    current_app.logger.warning('S1 存量内容已先完成没收/隔离，随后重生成 log=%s refs=%s removed_from_snapshot=%s confiscated=%s', expedition_log_id, len(excluded_refs), removed_count, len(application.get('confiscated_refs') or []))
                if location_extra_review_may_regenerate_main and review_attempt >= max_review_attempts and (max_review_attempts < location_conflict_review_attempt_limit):
                    max_review_attempts += 1
                    review_attempt_sequence.append(review_attempt + 1)
                    location_extra_review_may_regenerate_main = False
                regenerate_resolution = regeneration_basis in {'event_resolution_invalid', 'persistent_content_violation'}
                if regenerate_resolution:
                    frozen_event_resolution = None
                    full_log.pop('pre_narrative_resolution', None)
                event_resolution_regeneration_basis = regeneration_basis if regenerate_resolution else None
                observer_feedback = _build_s1_observer_regeneration_feedback(regeneration_basis, verdict=verdict, result_data=verdict.get('result_data') or result_data)
                _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids, resume_stage='main_ai', review_attempt=min(review_attempt + 1, max_review_attempts), generation_claim_token=generation_claim_token, objective_outline=frozen_objective_outline, event_resolution=frozen_event_resolution, candidate=None, observer_feedback=observer_feedback, event_resolution_regeneration_basis=event_resolution_regeneration_basis, retry_errors=[], retry_trace=generation_retry_trace)
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=str(regeneration_basis or 'other_unrepairable'), action=S1RetryAction.REGENERATE_RESOLUTION if regenerate_resolution else S1RetryAction.REGENERATE_MAIN, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=False, detail=verdict.get('reason'), correlation_id=observer_correlation_id)
                current_app.logger.warning('S1 剧情审核打回，定向重生成 log=%s 第 %s/%s 次 basis=%s：%s', expedition_log_id, review_attempt, max_review_attempts, regeneration_basis, (observer_feedback or '')[:120])
                continue
            candidate = dict(verdict.get('result_data') or result_data)
            infra_failed = bool(verdict.get('infra_failed'))
            if infra_failed:
                if not isinstance(review_failure_fallback_candidate, dict):
                    original_candidate_error = 'observer_original_candidate_missing: 当前检查点缺少独立保存的主剧情原稿；保留续审候选，只重试 Observer，不使用失败审核覆盖执行故障放行。'
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='observer_original_candidate_missing', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=original_candidate_error, correlation_id=observer_correlation_id)
                    queue_downstream_retry(candidate, [original_candidate_error])
                    continue
                candidate = _clone_s1_generation_value(review_failure_fallback_candidate)
                if S1_GOLD_SETTLEMENT_FIELD in (verdict.get('verified_failed_observer_override_fields') or []) and isinstance((verdict.get('result_data') or {}).get(S1_GOLD_SETTLEMENT_FIELD), dict):
                    candidate[S1_GOLD_SETTLEMENT_FIELD] = _clone_s1_generation_value(verdict['result_data'][S1_GOLD_SETTLEMENT_FIELD])
            candidate.pop('pre_narrative_resolution', None)
            if frozen_event_resolution is not None:
                candidate['pre_narrative_resolution'] = deepcopy(frozen_event_resolution)
            retained_physical_loot_review_requested = retained_physical_loot_review_requested or _has_s1_retained_physical_loot_settlement(candidate)
            if not infra_failed and candidate.get('unassigned_items') and (capture_team_loot_review(candidate) is None):
                loot_review_error = 'unassigned_items_explicit_review_missing'
                queue_downstream_retry(candidate, [loot_review_error])
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=loot_review_error, action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=loot_review_error, correlation_id=observer_correlation_id)
                continue
            if verdict.get(TRAIT_PERSISTENCE_PUBLICATION_BLOCKED_FIELD) is True:
                trait_hold_error = str(verdict.get('contract_error') or 'trait_persistence_action_group_incomplete')
                queue_downstream_retry(candidate, [trait_hold_error])
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=trait_hold_error, action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=trait_hold_error, correlation_id=observer_correlation_id)
                continue
            trait_review_progress = candidate.get(TRAIT_PERSISTENCE_PROGRESS_FIELD)
            if not infra_failed and isinstance(trait_review_progress, dict) and trait_review_progress.get('observer_review_id') and trait_review_progress.get('pending_candidates'):
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='trait_persistence_reviews_pending', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=f"accepted={len(trait_review_progress.get('accepted_reviews') or [])}; pending={len(trait_review_progress['pending_candidates'])}; 仅待审新增特质未取得写权限，原始候选与逐项审核进度已完整留档。", correlation_id=observer_correlation_id)
            semantic_verdict_value = verdict.get(OBSERVER_SEMANTIC_VERDICT_CAN_PASS_FIELD)
            semantic_verdict_cannot_pass = semantic_verdict_value is False
            story_release_authorized = verdict.get(OBSERVER_STORY_RELEASE_AUTHORIZED_FIELD) is True
            story_release_basis = str(verdict.get(OBSERVER_STORY_RELEASE_BASIS_FIELD) or '')[:80]
            deferred_observer_subcontracts = [row for row in verdict.get(DEFERRED_OBSERVER_SUBCONTRACTS_FIELD) or [] if isinstance(row, dict) and row.get('field')]
            waiting_npc_deferred_subcontract = next((row for row in deferred_observer_subcontracts if waiting_npc_review_requested and row.get('field') == 'waiting_npc_updates' and (row.get('reason_code') == 'waiting_npc_contract_deferred_unconsumed')), None)
            if regeneration_proposal_rejected and countermeasure_application_receipt_required and (not any((row.get('field') == COUNTERMEASURE_OPPORTUNITY_REVIEWS_FIELD for row in deferred_observer_subcontracts))):
                deferred_observer_subcontracts.append({'field': COUNTERMEASURE_OPPORTUNITY_REVIEWS_FIELD, 'reason_code': 'regeneration_proposal_rejected', 'contract_error': 'regeneration_response_omitted_auxiliary_receipt'})
            for deferred_subcontract in deferred_observer_subcontracts:
                if deferred_subcontract is waiting_npc_deferred_subcontract:
                    continue
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=str(deferred_subcontract.get('reason_code') or 'auxiliary_receipt_deferred')[:120], action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=f"field={deferred_subcontract.get('field')} error={deferred_subcontract.get('contract_error') or ''}"[:1000], correlation_id=observer_correlation_id)
            deferred_observer_subcontract_fields = {str(row.get('field')) for row in deferred_observer_subcontracts}
            countermeasure_application_review_deferred = bool(countermeasure_review_deferred or COUNTERMEASURE_OPPORTUNITY_REVIEWS_FIELD in deferred_observer_subcontract_fields)
            decisive_capability_review_deferred = bool(countermeasure_review_deferred or DECISIVE_CAPABILITY_REVIEWS_FIELD in deferred_observer_subcontract_fields)
            deferred_member_name_ids = [int(member_id) for member_id in verdict.get('deferred_member_name_ids') or [] if isinstance(member_id, int) and (not isinstance(member_id, bool)) and (member_id > 0)]
            if deferred_member_name_ids:
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='member_name_coverage_deferred', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=','.join((str(mid) for mid in deferred_member_name_ids)), correlation_id=observer_correlation_id)
            from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD, project_countermeasure_application_receipt_envelope
            candidate.pop(COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD, None)
            application_envelope = project_countermeasure_application_receipt_envelope(verdict.get(COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD)) if not infra_failed else None
            if application_envelope is not None:
                candidate[COUNTERMEASURE_APPLICATION_RECEIPTS_LOG_FIELD] = application_envelope
            elif countermeasure_application_receipt_required and (not infra_failed):
                countermeasure_receipt_deferred = any((row.get('field') == COUNTERMEASURE_OPPORTUNITY_REVIEWS_FIELD for row in deferred_observer_subcontracts))
                if not countermeasure_receipt_deferred:
                    receipt_error = 'countermeasure_application_receipt_missing'
                    queue_downstream_retry(candidate, [receipt_error])
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=receipt_error, action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=receipt_error, correlation_id=observer_correlation_id)
                    continue
            resource_ecology_resolution = verdict.get('resource_ecology_resolution')
            if resource_ecology_requires_review and isinstance(resource_ecology_resolution, dict) and (resource_ecology_resolution.get('decision') == 'defer_successor'):
                candidate = dict(candidate)
                candidate['ecology_continuity_debt'] = {'status': 'pending_world_outline', 'reason_code': 'successor_ecology_missing', 'evidence_event_indexes': list(resource_ecology_resolution.get('evidence_event_indexes') or []), 'resource_ecology': json.loads(json.dumps((frozen_objective_outline.get('resource_ecology') if isinstance(frozen_objective_outline, dict) else {}) or {}, ensure_ascii=False)), 'ecology_identity': json.loads(json.dumps((frozen_objective_outline.get(ECOLOGY_IDENTITY_OUTLINE_FIELD) if isinstance(frozen_objective_outline, dict) else {}) or {}, ensure_ascii=False))}
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='resource_ecology_successor_deferred', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='当前故事与权威现状兼容；结算优先继承已保存地点现状，无法确认时再交世界维护。', correlation_id=observer_correlation_id)
            resource_ecology_review_record = verdict.get('resource_ecology_review_record')
            if resource_ecology_requires_review and isinstance(resource_ecology_review_record, dict) and (resource_ecology_review_record.get('status') == 'deferred'):
                ecology_defer_reason = str(resource_ecology_review_record.get('reason_code') or 'resource_ecology_review_deferred')[:120]
                ecology_defer_detail = str(resource_ecology_review_record.get('contract_error') or '')[:1000]
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=ecology_defer_reason, action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=ecology_defer_detail, correlation_id=observer_correlation_id)
                current_app.logger.debug('S1 资源生态审核未形成可消费裁决，保留当前剧情并仅记内部审计 log=%s attempt=%s reason=%s error=%s', expedition_log_id, review_attempt, ecology_defer_reason, ecology_defer_detail[:200])
            ecology_identity_review_record = verdict.get('ecology_identity_review_record')
            if ecology_identity_requires_review:
                if not isinstance(ecology_identity_review_record, dict):
                    ecology_identity_review_record = {'status': 'deferred', 'profile_id': ecology_identity_review_context.get('profile_id'), 'profile_revision': ecology_identity_review_context.get('profile_revision'), 'motif_fingerprint': ecology_identity_review_context.get('motif_fingerprint'), 'reason_code': 'ecology_identity_review_record_missing', 'contract_error': 'observer_result_missing_review_record'}
                candidate = dict(candidate)
                candidate[ECOLOGY_IDENTITY_REVIEW_LOG_FIELD] = json.loads(json.dumps(ecology_identity_review_record, ensure_ascii=False))
                identity_review_status = str(ecology_identity_review_record.get('status') or '')
                identity_alignment = str(ecology_identity_review_record.get('alignment') or '')
                if identity_review_status == 'deferred' or identity_alignment == 'drift_detected':
                    identity_reason_code = 'ecology_identity_drift_detected' if identity_alignment == 'drift_detected' else str(ecology_identity_review_record.get('reason_code') or 'ecology_identity_review_deferred')[:120]
                    identity_detail = str(ecology_identity_review_record.get('diagnostic_summary') or ecology_identity_review_record.get('contract_error') or '')[:1000]
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=identity_reason_code, action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=identity_detail, correlation_id=observer_correlation_id)
                    current_app.logger.debug('S1 副本生态身份诊断只留内部审计并保留当前剧情 log=%s attempt=%s status=%s alignment=%s detail=%s', expedition_log_id, review_attempt, identity_review_status, identity_alignment, identity_detail[:200])
            if final_fate_destructive_requires_review and (not infra_failed) and final_fate_destructive_change_requires_review(candidate):
                destructive_review_error = 'final_fate_destructive_review_not_closed'
                queue_downstream_retry(candidate, [destructive_review_error])
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=destructive_review_error, action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=destructive_review_error, correlation_id=observer_correlation_id)
                continue
            post_observer_continuity_review_context = _build_s1_consumed_asset_continuity_review_context(candidate, full_log.get('audit_member_snapshot'))
            reviewed_continuity_context = verdict.get(CONSUMED_ASSET_CONTINUITY_REVIEWED_CONTEXT_FIELD, consumed_asset_continuity_review_context)
            unreviewed_continuity_candidates = _find_unreviewed_s1_consumed_asset_continuity_candidates(reviewed_continuity_context, post_observer_continuity_review_context)
            if unreviewed_continuity_candidates:
                continuity_drift_error = 'consumed_asset_continuity_candidate_drift_after_override'
                queue_downstream_retry(candidate, [continuity_drift_error])
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=continuity_drift_error, action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=continuity_drift_error, correlation_id=observer_correlation_id)
                continue
            if reviewed_continuity_context and (not infra_failed):
                companion_stage_errors = _apply_reviewed_companion_stage_floors(candidate, reviewed_continuity_context, verdict.get('consumed_asset_continuity_reviews'))
                if companion_stage_errors:
                    queue_downstream_retry(candidate, companion_stage_errors)
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='companion_stage_lineage_invalid', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(companion_stage_errors[:10]), correlation_id=observer_correlation_id)
                    continue
            waiting_npc_contract_error = str(verdict.get('contract_error') or '')
            verified_waiting_npc_updates = verdict.get('verified_waiting_npc_updates')
            waiting_npc_review_resolved_from_partial = infra_failed and waiting_npc_review_requested and bool(waiting_npc_contract_error) and isinstance(verified_waiting_npc_updates, list)
            if waiting_npc_review_resolved_from_partial:
                candidate = dict(candidate)
                candidate['waiting_npc_updates'] = verified_waiting_npc_updates
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='waiting_npc_partial_contract_verified', action=S1RetryAction.REPAIR_LOCAL, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=waiting_npc_contract_error, correlation_id=observer_correlation_id)
            non_waiting_observer_repair_error = next((str(error) for error in observer_repair_errors if 'waiting_npc_updates' not in str(error)), '')
            non_waiting_observer_repair_required = bool(non_waiting_observer_repair_error)
            persistent_content_review_required = bool(verdict.get('persistent_content_review_required'))
            blocking_non_waiting_semantic_review = any((story_release_authorized and (not waiting_npc_review_resolved_from_partial), semantic_verdict_cannot_pass, same_name_gain_requires_review, consumed_asset_continuity_requires_review, trait_persistence_requires_review, class_transfer_review_member_ids, final_fate_destructive_requires_review, False, faction_interaction_requires_review, countermeasure_application_requires_review and (not countermeasure_application_review_deferred), decisive_capability_review_requires_review and (not decisive_capability_review_deferred), persistent_content_review_required, high_risk_life_review_requested))
            waiting_npc_contract_deferred_by_observer = bool(waiting_npc_deferred_subcontract is not None)
            waiting_npc_contract_failure_deferable = bool(waiting_npc_contract_deferred_by_observer or (infra_failed and waiting_npc_review_requested and (not waiting_npc_review_resolved_from_partial) and (not any((blocking_non_waiting_semantic_review, non_waiting_observer_repair_required)))))
            waiting_npc_review_deferred = False
            if waiting_npc_contract_failure_deferable:
                disposition_verdict = verdict
                if waiting_npc_deferred_subcontract is not None:
                    disposition_verdict = {**verdict, 'contract_error': str(waiting_npc_deferred_subcontract.get('contract_error') or '')}
                candidate, waiting_npc_disposition_reason, waiting_npc_disposition_detail = _defer_waiting_npc_updates_with_disposition(candidate, full_log=full_log, verdict=disposition_verdict)
                waiting_npc_review_deferred = True
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='waiting_npc_contract_deferred_unconsumed', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=waiting_npc_disposition_detail, correlation_id=observer_correlation_id)
                current_app.logger.warning('S1 待抽取玩家 NPC 审核未形成可消费裁决，候选更新已留证但不消费，租约将在写回时释放，主剧情继续 log=%s attempt=%s reason=%s error=%s', expedition_log_id, review_attempt, waiting_npc_disposition_reason, waiting_npc_disposition_detail)
            waiting_npc_review_unresolved = waiting_npc_review_requested and (not waiting_npc_review_deferred) and (not waiting_npc_review_resolved_from_partial)
            non_waiting_observer_repair_failure = not blocking_non_waiting_semantic_review and non_waiting_observer_repair_required
            waiting_npc_hard_failure = waiting_npc_review_unresolved and (not blocking_non_waiting_semantic_review) and (not non_waiting_observer_repair_required)
            if (blocking_non_waiting_semantic_review or non_waiting_observer_repair_failure or waiting_npc_hard_failure) and infra_failed:
                fallback_story_release_allowed = _allow_s1_story_release_after_observer_failure(faction_interaction_requires_review=faction_interaction_requires_review, persistent_content_review_required=persistent_content_review_required, high_risk_life_review_requested=high_risk_life_review_requested, class_transfer_review_required=bool(class_transfer_review_member_ids), destructive_asset_review_required=final_fate_destructive_requires_review, dm_instruction_review_required=False)
                if story_release_authorized and (not semantic_verdict_cannot_pass) and fallback_story_release_allowed:
                    downstream_error = f"Observer 未形成可执行的最终拒绝，按轻量语义裁决边界发布当前剧情；保留主结果结算提案，必须通过最终机械复校验后才能写回。 release_basis={story_release_basis or 'unknown'}"
                elif semantic_verdict_cannot_pass:
                    downstream_error = '轻量语义裁决已确认 Observer 输出明确要求当前剧情不能放行；禁止把解析失败或审核基础设施耗尽改写成故障降级通过。'
                elif faction_interaction_requires_review:
                    downstream_error = '本次存在后端选定的酒馆势力交互强制线，必须由审核 AI 依据玩家可见事件确认外部势力交互与阶段结果均已实际兑现，并校正同源世界事实写回；审核服务本次不可用，不能发布未经确认的剧情。'
                elif persistent_content_review_required:
                    downstream_error = '本次主剧情使用了冻结存量资产并形成内容边界硬伤，必须先由独立复核逐项确认没收或本次隔离，再允许重生成；复核不可用时保留同一主剧情，只重试审核链。'
                elif final_fate_destructive_requires_review:
                    downstream_error = '最终命途存在对出发特质或技能的永久删除提案，必须由审核 AI 逐项绑定玩家可见事件、冻结旧资产与明确后继；审核服务本次不可用，删除不得取得写权限。'
                elif countermeasure_application_requires_review:
                    downstream_error = '本次存在实际注入的长期主体知识，必须核对该主体在合法条件下的真实响应及其证据；应用回执不可用时按已有辅助失败出口处理。'
                elif decisive_capability_review_requires_review:
                    downstream_error = '完整剧情审核必须按最终事件逐项形成决定性能力边界回执，并把任何对象、范围、时机、容量或功能越权与同一事件的重生成证据闭环；审核服务或内部回执留档本次不可用，必须保留同一主剧情并只重试 Observer。'
                elif new_equipment_quantity_repair_requested:
                    downstream_error = '本次存在缺失或非法的实物数量，必须先由审核 AI 尝试依据正文补齐；审核服务持续不可用时不得猜数，只隔离数量非法的物品条目，保留其它机械有效掉落、主剧情与同案申诉证据。'
                elif same_name_gain_requires_review:
                    downstream_error = '同名物品再次获得必须依据剧情全文判定是独立新获得、升级/转赠，还是重复复制；审核服务本次不可用，不能在来源未确认时落库。'
                elif consumed_asset_continuity_requires_review:
                    downstream_error = '冻结快照中的整件资产被完整扣除，必须依据描述与剧情全文裁决其结尾实体连续性；审核服务本次不可用，不能在资产最终状态未确认时落库。'
                elif trait_persistence_requires_review:
                    downstream_error = '本次存在 new_traits 提案，必须由审核 AI 逐项确认离场后仍有稳定持续机制，并把非持久经历从最终特质栏完整移除；审核服务本次不可用，不能把未经确认的经历写成永久特质。'
                elif class_transfer_review_member_ids:
                    downstream_error = '本次有成员携带与目标副本精确匹配的正式转职邀请函，必须由审核 AI 核对玩家可见正文与 new_class 是否一致；审核服务本次不可用，不能发布职业叙事与持久状态可能分离的结果。'
                elif high_risk_life_review_requested:
                    downstream_error = '本次特殊事件或隐藏Boss的前置生命底稿必须完成高危压力语义审核；审核服务本次不可用，不能发布未经确认的生死、核心或离场结果。'
                elif non_waiting_observer_repair_failure:
                    downstream_error = non_waiting_observer_repair_error[:1000]
                else:
                    observer_contract_error = waiting_npc_contract_error
                    if observer_contract_error.startswith('waiting_npc_updates_'):
                        downstream_error = observer_contract_error
                    else:
                        downstream_error = '本次已冻结待抽取玩家 NPC 候选，必须由审核 AI 依据完整事件确认候选是否实际出场；若出场，还要确认其身份、配角边界、原文证据及会在正式抽取时继承的当前档案。审核服务本次不可用，不能发布无法确认是否漏记 NPC 出场的剧情。'
                response_parse_repair_exhausted = verdict.get('failure_stage') == 'response_parse'
                review_infra_exhausted = review_attempt >= review_infra_attempt_limit
                release_story_after_observer_failure = _should_release_s1_story_after_observer_failure(story_release_authorized=story_release_authorized, semantic_verdict_cannot_pass=semantic_verdict_cannot_pass, fallback_scope_allowed=fallback_story_release_allowed, response_parse_repair_exhausted=response_parse_repair_exhausted, review_infra_exhausted=review_infra_exhausted)
                if release_story_after_observer_failure:
                    review_source_candidate = deepcopy(candidate)
                    downstream_error = 'Observer 在有界重试内没有形成可消费裁决，且没有可信的明确拒绝；保留原主剧情及其结算提案，统一通过最终机械复校验后发布和写回。'
                    precollected_quarantine_receipt_entries: list[dict] = []
                    if waiting_npc_review_unresolved:
                        candidate, waiting_npc_disposition_reason, waiting_npc_disposition_detail = _defer_waiting_npc_updates_with_disposition(candidate, full_log=full_log, verdict=verdict, quarantine_receipt_entries=precollected_quarantine_receipt_entries)
                        _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='waiting_npc_contract_deferred_unconsumed', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=review_infra_attempt_limit, preserves_main_result=True, detail=waiting_npc_disposition_detail, correlation_id=observer_correlation_id)
                        current_app.logger.warning('S1 待抽取玩家 NPC 完整审核随混合合同故障耗尽，候选更新已留证但不消费，租约将在写回时释放 log=%s attempt=%s reason=%s error=%s', expedition_log_id, review_attempt, waiting_npc_disposition_reason, waiting_npc_disposition_detail)
                    quarantine_reason_code = _S1_OBSERVER_RELEASE_AUTHORIZED_QUARANTINE_REASON_CODE if story_release_authorized else _S1_REVIEW_FAILURE_RESPONSE_PARSE_QUARANTINE_REASON_CODE if response_parse_repair_exhausted else _S1_REVIEW_FAILURE_QUARANTINE_REASON_CODE
                    candidate, quarantined_paths = _prepare_s1_review_failure_release(candidate, quarantine_reason_code=quarantine_reason_code, quarantine_failure_stage=str(verdict.get('failure_stage') or 'unknown'), precollected_receipt_entries=precollected_quarantine_receipt_entries, preserve_verified_waiting_npc_updates=waiting_npc_review_resolved_from_partial)
                    if _validate_s1_required_world_story_result(candidate, full_log):
                        required_review_error = 'required_world_story_review_unavailable: 公共剧情与共同任务战报尚未取得可消费审核结果；请基于同一冻结输入重新核验原候选。'
                        _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='required_world_story_review_unavailable', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=f"{required_review_error} failure_stage={verdict.get('failure_stage') or 'unknown'}; contract_error={verdict.get('contract_error') or 'unknown'}", correlation_id=observer_correlation_id)
                        queue_downstream_retry(review_source_candidate, [required_review_error])
                        continue
                    quarantine_detail = f'{downstream_error}; invalid_or_auxiliary_paths=' + ','.join(quarantined_paths[:30])
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='observer_error_original_result_pending_validation', action=S1RetryAction.VALIDATE_ORIGINAL_AFTER_REVIEW_FAILURE, attempt=review_attempt, max_attempts=review_infra_attempt_limit, preserves_main_result=True, detail=quarantine_detail, correlation_id=observer_correlation_id)
                    if quarantined_paths:
                        _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=quarantine_reason_code, action=S1RetryAction.QUARANTINE_UNREVIEWED_STATE, attempt=review_attempt, max_attempts=review_infra_attempt_limit, preserves_main_result=True, detail=','.join(quarantined_paths), correlation_id=observer_correlation_id)
                    if story_release_authorized:
                        current_app.logger.info('S1 Observer 输出未形成最终拒绝，按轻量语义裁决边界保留原始结算进入最终机械复校验并发布 log=%s attempt=%s basis=%s quarantined=%s', expedition_log_id, review_attempt, story_release_basis or 'unknown', quarantined_paths[:30])
                    elif response_parse_repair_exhausted:
                        current_app.logger.error('S1 Observer 响应解析及结构修复已耗尽，保留原始结算进入最终机械复校验并尝试发布 log=%s attempt=%s configured_infra_attempts=%s quarantined=%s', expedition_log_id, review_attempt, review_infra_attempt_limit, quarantined_paths[:30])
                    else:
                        current_app.logger.error('S1 强制语义审核连续故障已达上限，保留原始结算后进入最终机械复校验并尝试发布原剧情 log=%s attempts=%s quarantined=%s', expedition_log_id, review_infra_attempt_limit, quarantined_paths[:30])
                else:
                    if review_attempt >= max_review_attempts and max_review_attempts < review_infra_attempt_limit:
                        max_review_attempts += 1
                        review_attempt_sequence.append(review_attempt + 1)
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=_S1_OBSERVER_SEMANTIC_REJECTION_REASON_CODE if semantic_verdict_cannot_pass else 'required_semantic_review_infra_failed', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=downstream_error, correlation_id=observer_correlation_id)
                    queue_downstream_retry(candidate, get_s1_new_equipment_quantity_repair_errors(observer_repair_errors) if new_equipment_quantity_repair_requested else [downstream_error])
                    current_app.logger.warning('S1 强制语义审核故障，仅重试下游审核 log=%s 第 %s/%s 次 dm=%s same_name=%s consumed_continuity=%s trait_persistence=%s class_transfer=%s fate_destructive=%s faction_interaction=%s countermeasure=%s observer_repair=%s waiting_npc_blocking=%s resource_ecology=%s', expedition_log_id, review_attempt, max_review_attempts, False, same_name_gain_requires_review, consumed_asset_continuity_requires_review, trait_persistence_requires_review, bool(class_transfer_review_member_ids), final_fate_destructive_requires_review, faction_interaction_requires_review, countermeasure_application_requires_review, non_waiting_observer_repair_failure, waiting_npc_hard_failure, resource_ecology_requires_review)
                    continue
            from plugins.adventurer_s1.services.adventurer_s1_expedition_settlement_guard_ai_service import apply_settlement_guard_patch, build_settlement_guard_quarantine_scope, has_unresolved_class_transfer_consumption, needs_career_consumption_review, resolve_settlement_guard
            audit_member_snapshot = full_log.get('audit_member_snapshot') or []
            career_dungeon = expedition_asset_dungeon(full_log, target_dungeon)
            if needs_career_consumption_review(candidate, audit_member_snapshot, career_dungeon):
                guard_quarantine_scope = build_settlement_guard_quarantine_scope(candidate, audit_member_snapshot, career_dungeon)
                from plugins.adventurer_s1.models import S1_EXPEDITION_GENERATION_PHASE_SETTLEMENT_GUARD_AI
                if not _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids, resume_stage=S1_EXPEDITION_GENERATION_PHASE_SETTLEMENT_GUARD_AI, review_attempt=review_attempt, generation_claim_token=generation_claim_token, objective_outline=frozen_objective_outline, event_resolution=frozen_event_resolution, candidate=candidate, review_failure_fallback_candidate=review_failure_fallback_candidate, trait_context_receipt=actual_trait_context_receipt, observer_feedback=observer_feedback, event_resolution_regeneration_basis=event_resolution_regeneration_basis, retry_errors=observer_repair_errors, history_anchor_repairs=history_anchor_repairs, retry_trace=generation_retry_trace):
                    current_app.logger.info('S1 职业消耗闭环中止：日志已离开 generating party=%s log=%s', party_id, expedition_log_id)
                    return
                guard_result = resolve_settlement_guard(expedition_log_id=expedition_log_id, party_id=party_id, result_data=candidate, target_dungeon=career_dungeon, member_snapshot=audit_member_snapshot, context={'member_id_to_name': {member.id: member.name for member in member_snapshots if member.name}, 'has_special_event': has_special_event, 'is_extreme_crisis': is_extreme_crisis, 'special_event_type': special_event_type, 'special_event_risk_tier': special_event_risk_tier, 'hidden_boss_context': hidden_boss_context, 'commission_context': commission_context, 'special_dungeon_context': special_dungeon_context}, attempt_number=review_attempt)
                _touch_expedition_generation_phase(expedition_log_id, S1_EXPEDITION_GENERATION_PHASE_SETTLEMENT_GUARD_AI, generation_claim_token=generation_claim_token)
                if not guard_result:
                    guard_result = {'decision': 'unavailable_preserved'}
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_settlement_guard', reason_code='settlement_guard_error_original_result_pending_validation', action=S1RetryAction.VALIDATE_ORIGINAL_AFTER_REVIEW_FAILURE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='职业消耗审核不可用，保留原职业、技能和消耗提案进入最终机械复校验。')
                guard_token_usage = getattr(guard_result, 'deferred_token_usage', None)
                if guard_token_usage is not None:
                    deferred_guard_ai_token_usage_handles.append(guard_token_usage)
                guard_decision = guard_result.get('decision')
                if guard_decision == 'regenerate':
                    guard_error = guard_result.get('retry_feedback') or '职业凭证结算存在无法安全修正的问题。'
                    candidate, guard_quarantined_paths = _quarantine_s1_settlement_guard_state(candidate, reason_code='settlement_guard_cannot_patch', guard_scope=guard_quarantine_scope)
                    guard_decision = 'quarantined'
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_settlement_guard', reason_code='settlement_guard_cannot_patch', action=S1RetryAction.QUARANTINE_UNREVIEWED_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=f'{guard_error}; quarantined=' + ','.join(guard_quarantined_paths[:30]))
                    current_app.logger.warning('S1 职业消耗 Guard 无法局部修正，隔离职业相关写入后继续主剧情 log=%s attempt=%s quarantined=%s', expedition_log_id, review_attempt, guard_quarantined_paths[:30])
                if guard_decision == 'patched':
                    guard_touched_new_equipments = any((isinstance(row, dict) and 'new_equipments' in row for row in guard_result.get('member_updates_patch') or []))
                    candidate = apply_settlement_guard_patch(candidate, guard_result, member_snapshot_ids)
                    if guard_touched_new_equipments:
                        _record_s1_retry_decision(generation_retry_trace, stage='expedition_settlement_guard', reason_code='guard_new_equipment_entitlement_fields_frozen', action=S1RetryAction.REPAIR_LOCAL, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='Guard 只保留同成员同名实物的非权益元数据修正；物品集合、所有者与 quantity 继承 Observer 成品，不增加 Observer 调用。')
                if has_unresolved_class_transfer_consumption(candidate, audit_member_snapshot, career_dungeon):
                    guard_error = '正式转职邀请函消费后置条件仍未闭合：模型扣减尚未完全剥离，或非空 new_class 对应的本副本邀请函未按后端权威结果补扣；职业、新技能与对应消费写入已隔离，正文继续。'
                    candidate, guard_quarantined_paths = _quarantine_s1_settlement_guard_state(candidate, reason_code='class_transfer_consumption_unresolved', guard_scope=guard_quarantine_scope)
                    _record_s1_retry_decision(generation_retry_trace, stage='expedition_settlement_guard', reason_code='class_transfer_consumption_unresolved', action=S1RetryAction.QUARANTINE_UNREVIEWED_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=f'{guard_error} quarantined=' + ','.join(guard_quarantined_paths[:30]))
                    current_app.logger.warning('S1 正式转职邀请函补扣后置条件失败，隔离职业相关写入后继续主剧情 log=%s attempt=%s quarantined=%s', expedition_log_id, review_attempt, guard_quarantined_paths[:30])
            post_guard_continuity_review_context = _build_s1_consumed_asset_continuity_review_context(candidate, audit_member_snapshot)
            unreviewed_guard_continuity_candidates = _find_unreviewed_s1_consumed_asset_continuity_candidates(post_observer_continuity_review_context, post_guard_continuity_review_context)
            if unreviewed_guard_continuity_candidates:
                continuity_guard_drift_error = 'consumed_asset_continuity_candidate_drift_after_guard'
                queue_downstream_retry(candidate, [continuity_guard_drift_error])
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=continuity_guard_drift_error, action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=continuity_guard_drift_error, correlation_id=observer_correlation_id)
                continue
            destructive_settlement_errors = validate_final_fate_destructive_settlement(candidate, member_snapshots)
            if destructive_settlement_errors:
                queue_downstream_retry(candidate, destructive_settlement_errors)
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='final_fate_destructive_receipt_invalid', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(destructive_settlement_errors[:10]), correlation_id=observer_correlation_id)
                continue
            companion_stage_errors = _enforce_reviewed_companion_stage_floors(candidate, post_guard_continuity_review_context)
            if companion_stage_errors:
                queue_downstream_retry(candidate, companion_stage_errors)
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='companion_stage_lineage_stale_after_guard', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(companion_stage_errors[:10]), correlation_id=observer_correlation_id)
                continue
            _, post_hard_errors, post_soft_errors = validate_s1_expedition_result(candidate, member_snapshot_ids, member_context=post_member_context, commission_context=commission_context or [], raw_story_char_count=sum((len(re.sub('\\s+', '', str(event))) for event in (candidate or {}).get('events') or [] if isinstance(event, str))), skip_length_checks=skip_length_checks, countermeasure_learning_enabled=False, require_gold_settlement=isinstance(candidate, dict) and (S1_GOLD_SETTLEMENT_FIELD in candidate or candidate.get(S1_GOLD_SETTLEMENT_VERSION_FIELD) == S1_GOLD_SETTLEMENT_CONTRACT_VERSION), require_member_scores=True)
            if history_anchor_repair_requested:
                history_anchor_repairs = matching_s1_history_anchor_repairs(candidate, history_anchor_repairs)
                post_hard_errors.extend(unresolved_s1_history_anchor_errors(history_anchor_repair_errors, history_anchor_repairs))
            post_hard_errors.extend(_validate_s1_expedition_life_adjustments(candidate, frozen_event_resolution, {int(member.id): max(1, _coerce_int(getattr(member, 'max_hp', 0), 1)) for member in member_snapshots}, {int(member_id): {'equipment': set(context.get('equipment_names') or set()), 'skill': set(context.get('skill_names') or set()), 'trait': set(context.get('trait_names') or set())} for member_id, context in post_member_context.items()}))
            from plugins.adventurer_s1.services.s1_waiting_player_npc_service import load_waiting_player_npc_injection, validate_waiting_npc_updates_strict
            waiting_npc_updates, waiting_npc_errors = validate_waiting_npc_updates_strict(candidate.get('waiting_npc_updates') if isinstance(candidate, dict) else None, snapshot=load_waiting_player_npc_injection(expedition_log_id), events=candidate.get('events') if isinstance(candidate, dict) else None)
            if waiting_npc_errors:
                post_hard_errors.extend((f'waiting_npc_updates: {message}' for message in waiting_npc_errors))
            elif isinstance(candidate, dict):
                candidate['waiting_npc_updates'] = waiting_npc_updates
            required_world_story_errors = _validate_s1_required_world_story_result(candidate, full_log)
            if required_world_story_errors:
                queue_downstream_retry(candidate, required_world_story_errors)
                _record_s1_retry_decision(generation_retry_trace, stage='required_world_story_validation', reason_code='required_world_story_requires_observer_repair', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(required_world_story_errors[:10]))
                current_app.logger.warning('S1 必选公共剧情闭环未通过，保留正文重试下游审核 log=%s 第 %s/%s 次：%s', expedition_log_id, review_attempt, max_review_attempts, '; '.join(required_world_story_errors[:3]))
                continue
            residual_codenames = _detect_residual_codenames_in_result(candidate)
            if residual_codenames:
                candidate = _try_restore_codenames_on_result(candidate, name_alias, member_snapshots)
                residual_codenames = _detect_residual_codenames_in_result(candidate)
            if residual_codenames:
                codename_error = '上次输出残留无法映射到真实队员的代号：' + '；'.join(residual_codenames[:5]) + '。请只对现有正文执行名称归属与局部校正，不改变事件事实。'
                queue_downstream_retry(candidate, [codename_error])
                _record_s1_retry_decision(generation_retry_trace, stage='post_validation', reason_code='residual_codename', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='；'.join(residual_codenames[:5]))
                current_app.logger.warning('S1 后端残留代号裁决仅重试下游审核 log=%s 第 %s/%s 次：%s', expedition_log_id, review_attempt, max_review_attempts, '；'.join(residual_codenames[:3]))
                continue
            final_continuity_review_context = _build_s1_consumed_asset_continuity_review_context(candidate, audit_member_snapshot)
            unreviewed_final_continuity_candidates = _find_unreviewed_s1_consumed_asset_continuity_candidates(post_guard_continuity_review_context, final_continuity_review_context)
            if unreviewed_final_continuity_candidates:
                continuity_post_processing_error = 'consumed_asset_continuity_candidate_drift_after_post_processing'
                queue_downstream_retry(candidate, [continuity_post_processing_error])
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code=continuity_post_processing_error, action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=continuity_post_processing_error, correlation_id=observer_correlation_id)
                continue
            final_companion_stage_errors = _enforce_reviewed_companion_stage_floors(candidate, final_continuity_review_context)
            if final_companion_stage_errors:
                queue_downstream_retry(candidate, final_companion_stage_errors)
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='companion_stage_lineage_stale_after_post_processing', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(final_companion_stage_errors[:10]), correlation_id=observer_correlation_id)
                continue
            from plugins.adventurer_s1.services.s1_item_world_unique_service import validate_expedition_item_grant_feasibility
            item_grant_errors = validate_expedition_item_grant_feasibility(candidate.get('member_updates') or [], member_snapshot_ids)
            if item_grant_errors:
                event_resolution_regeneration_basis = None
                observer_feedback = '上轮输出未通过 settlement_item_grant_feasibility 校验。请按【深渊酒馆·物品防复制与唯一名称】重新生成完整正文与结算；为冲突的唯一成果建立新的稳定名称，或在正文一致的前提下折现，不要再次提交同一冲突名称。'
                _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids, resume_stage='main_ai', review_attempt=min(review_attempt + 1, max_review_attempts), generation_claim_token=generation_claim_token, objective_outline=frozen_objective_outline, event_resolution=frozen_event_resolution, candidate=None, observer_feedback=observer_feedback, event_resolution_regeneration_basis=None, retry_errors=[], retry_trace=generation_retry_trace)
                _record_s1_retry_decision(generation_retry_trace, stage='settlement_item_grant_feasibility', reason_code='settlement_item_grant_infeasible', action=S1RetryAction.REGENERATE_MAIN, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=False, detail='; '.join(item_grant_errors[:10]))
                current_app.logger.warning('S1 结算候选存在不可到账物品，发布前打回重生成 log=%s 第 %s/%s 次：%s', expedition_log_id, review_attempt, max_review_attempts, '; '.join(item_grant_errors[:3]))
                continue
            final_same_name_gain_review_context = _build_s1_same_name_gain_review_context(candidate, audit_member_snapshot)
            unreviewed_same_name_gains = _find_unreviewed_s1_same_name_gain_candidates(same_name_gain_review_context, final_same_name_gain_review_context)
            if unreviewed_same_name_gains:
                same_name_gain_error = 'same_name_gain_candidate_drift_after_observer: 最终结算新增了未经裁决的同名装备冲突；请依据最终候选与冻结装备身份完成 same_name_gain_reviews 及对应完整字段覆盖。'
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_observer', reason_code='same_name_gain_candidate_drift_after_observer', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail=same_name_gain_error, correlation_id=observer_correlation_id)
                queue_downstream_retry(candidate, [same_name_gain_error])
                current_app.logger.warning('S1 审核后新增同名装备冲突，保留正文续审 log=%s attempt=%s/%s', expedition_log_id, review_attempt, max_review_attempts)
                continue
            if infra_failed:
                if not post_hard_errors:
                    if post_soft_errors:
                        candidate = clamp_s1_soft_fields(candidate)
                        _record_s1_retry_decision(generation_retry_trace, stage='post_validation', reason_code='soft_fields_clamped', action=S1RetryAction.REPAIR_LOCAL, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(post_soft_errors[:10]))
                    claimed_candidate, location_errors, initialization_deferred, stable_identity_change_ignored = claim_location_changes(candidate)
                    if not location_errors:
                        if stable_identity_change_ignored:
                            _record_s1_retry_decision(generation_retry_trace, stage='location_state_claim', reason_code='persistent_dungeon_explorability_change_ignored', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='常规副本身份固定保留；整体关闭提案已丢弃，地点与资源生态结果继续正常结算。')
                        if initialization_deferred:
                            _record_s1_retry_decision(generation_retry_trace, stage='location_state_claim', reason_code='dungeon_explorability_initialization_deferred', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='Observer 基础设施未完成首次可探索性判断；保留 null 未知态并发布原主剧情。')
                        checkpoint_approved_result(claimed_candidate, review_attempt, release_unreviewed_ordinary_loot=retained_physical_loot_review_requested)
                        approved_result = claimed_candidate
                        break
                    deferred_candidate, should_retry = resolve_location_claim_conflict(candidate, location_errors, review_attempt=review_attempt)
                    if should_retry:
                        continue
                    checkpoint_approved_result(deferred_candidate, review_attempt, release_unreviewed_ordinary_loot=retained_physical_loot_review_requested)
                    approved_result = deferred_candidate
                    break
                queue_downstream_retry(candidate, post_hard_errors + post_soft_errors)
                _record_s1_retry_decision(generation_retry_trace, stage='post_validation', reason_code='observer_infra_failed_with_hard_errors', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(post_hard_errors[:10]))
                current_app.logger.warning('S1 剧情审核故障放行后复校验仍有硬残留，仅重试下游审核 log=%s 第 %s/%s 次：%s', expedition_log_id, review_attempt, max_review_attempts, '; '.join(post_hard_errors[:3]))
                continue
            quantity_repair_errors = get_s1_new_equipment_quantity_repair_errors(post_hard_errors)
            if post_hard_errors and len(quantity_repair_errors) == len(post_hard_errors) and (review_attempt >= max_review_attempts):
                quantity_repaired_candidate, quarantined_paths = _quarantine_s1_invalid_new_equipment_quantities(candidate)
                if quarantined_paths:
                    candidate = quantity_repaired_candidate
                    post_hard_errors = []
                    _record_s1_retry_decision(generation_retry_trace, stage='post_validation', reason_code=_S1_NEW_EQUIPMENT_QUANTITY_REPAIR_QUARANTINE_REASON_CODE, action=S1RetryAction.QUARANTINE_UNREVIEWED_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='Observer 已用尽数量局部修复次数；未猜测数量，仅隔离非法物品条目：' + ','.join(quarantined_paths[:30]))
                    current_app.logger.warning('S1 实物数量局部修复耗尽，仅隔离非法条目并发布原剧情 log=%s attempt=%s/%s quarantined=%s', expedition_log_id, review_attempt, max_review_attempts, quarantined_paths[:30])
            if post_hard_errors:
                observer_fixable_errors = get_s1_observer_repair_errors(post_hard_errors)
                if len(observer_fixable_errors) == len(post_hard_errors):
                    for usage_handle in deferred_observer_ai_token_usage_handles:
                        set_failure_diagnostics = getattr(usage_handle, 'set_downstream_failure_diagnostics', None)
                        if callable(set_failure_diagnostics):
                            set_failure_diagnostics(failure_stage='downstream_validation', validation_error_code='observer_repair_incomplete')
                    queue_downstream_retry(candidate, observer_fixable_errors)
                    _record_s1_retry_decision(generation_retry_trace, stage='post_validation', reason_code='observer_repair_incomplete', action=S1RetryAction.RETRY_OBSERVER, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(observer_fixable_errors[:10]))
                    current_app.logger.warning('S1 探险复校验仅剩 Observer 职责字段，保留正文重试下游审核 log=%s 第 %s/%s 次：%s', expedition_log_id, review_attempt, max_review_attempts, '; '.join(observer_fixable_errors[:3]))
                    continue
                event_resolution_regeneration_basis = None
                observer_feedback = build_s1_retry_validation_feedback(post_hard_errors + post_soft_errors, post_member_context)
                _persist_s1_expedition_generation_checkpoint(expedition_log_id, party_id=party_id, member_ids=member_snapshot_ids, resume_stage='main_ai', review_attempt=min(review_attempt + 1, max_review_attempts), generation_claim_token=generation_claim_token, objective_outline=frozen_objective_outline, event_resolution=frozen_event_resolution, candidate=None, observer_feedback=observer_feedback, event_resolution_regeneration_basis=None, retry_errors=[], retry_trace=generation_retry_trace)
                _record_s1_retry_decision(generation_retry_trace, stage='post_validation', reason_code='hard_validation_error', action=S1RetryAction.REGENERATE_MAIN, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=False, detail='; '.join(post_hard_errors[:10]))
                current_app.logger.warning('S1 探险生成复校验发现硬错误，打回重生成 log=%s 第 %s/%s 次：%s', expedition_log_id, review_attempt, max_review_attempts, '; '.join(post_hard_errors[:3]))
                continue
            if post_soft_errors:
                candidate = clamp_s1_soft_fields(candidate)
                _record_s1_retry_decision(generation_retry_trace, stage='post_validation', reason_code='soft_fields_clamped', action=S1RetryAction.REPAIR_LOCAL, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='; '.join(post_soft_errors[:10]))
                current_app.logger.info('S1 探险生成存在软错误，后端确定性兜底修正后放行 log=%s：%s', expedition_log_id, '; '.join(post_soft_errors[:3]))
            candidate.pop(_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD, None)
            if same_name_gain_review_context and (not infra_failed):
                same_name_gain_reviews = verdict.get('same_name_gain_reviews')
                retained_update_conflicts = [row for row in _build_s1_same_name_gain_review_context(candidate, full_log.get('audit_member_snapshot')) if row.get('conflict_kind') == 'equipment_update_final_name']
                if isinstance(same_name_gain_reviews, list) and retained_update_conflicts:
                    candidate[_S1_SAME_NAME_GAIN_REVIEW_RECEIPT_FIELD] = {'schema_version': 2, 'status': 'passed', 'context_digest': _s1_same_name_gain_review_context_digest(retained_update_conflicts), 'reviews': deepcopy(same_name_gain_reviews)}
            claimed_candidate, location_errors, initialization_deferred, stable_identity_change_ignored = claim_location_changes(candidate)
            if location_errors:
                deferred_candidate, should_retry = resolve_location_claim_conflict(candidate, location_errors, review_attempt=review_attempt)
                if should_retry:
                    continue
                checkpoint_approved_result(deferred_candidate, review_attempt)
                approved_result = deferred_candidate
                break
            if stable_identity_change_ignored:
                _record_s1_retry_decision(generation_retry_trace, stage='location_state_claim', reason_code='persistent_dungeon_explorability_change_ignored', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='常规副本身份固定保留；整体关闭提案已丢弃，地点与资源生态结果继续正常结算。')
            if initialization_deferred:
                _record_s1_retry_decision(generation_retry_trace, stage='location_state_claim', reason_code='dungeon_explorability_initialization_deferred', action=S1RetryAction.DEFER_AUXILIARY_STATE, attempt=review_attempt, max_attempts=max_review_attempts, preserves_main_result=True, detail='主 AI 未提供首次可探索性投影；保留 null 未知态并发布原主剧情。')
            checkpoint_approved_result(claimed_candidate, review_attempt)
            approved_result = claimed_candidate
            break
        else:
            if approved_result is None and (not checkpoint_fail_close):
                review_rejected_all = True
                _record_s1_retry_decision(generation_retry_trace, stage='expedition_pipeline', reason_code='review_attempts_exhausted', action=S1RetryAction.FAIL_AND_DISBAND, attempt=max_review_attempts, max_attempts=max_review_attempts, preserves_main_result=bool(review_retry_candidate), detail=review_retry_errors[0] if review_retry_errors else observer_feedback)
        if approved_result is not None:
            from plugins.adventurer_s1.services.s1_countermeasure_learning_ai_service import finalize_countermeasure_learning
            from plugins.adventurer_s1.services.s1_team_loot_service import restore_team_loot_authority_after_postprocessing
            db_manager.remove()
            result_before_learning_finalization = approved_result
            approved_result = finalize_countermeasure_learning(approved_result, frozen=full_log, party_id=party_id, expedition_log_id=expedition_log_id, physical_attempt_guard=require_current_generation_claim, checkpoint=lambda value: checkpoint_approved_result(value, final_review_attempt), player_names=tuple((name for name in (full_log.get('_member_id_to_name') or {}).values() if isinstance(name, str) and name)))
            restore_team_loot_authority_after_postprocessing(approved_result, result_before_learning_finalization, roster=full_log.get('audit_member_snapshot') or [])
            db_manager.remove()
            result_before_description_finalization = approved_result
            approved_result, descriptions_finalized = finalize_s1_existing_asset_descriptions(approved_result, audit_member_snapshot=full_log.get('audit_member_snapshot'), description_projection=get_expedition_description_projection(full_log), expedition_log_id=expedition_log_id, party_id=party_id, member_id_to_name=full_log.get('_member_id_to_name'))
            restore_team_loot_authority_after_postprocessing(approved_result, result_before_description_finalization, roster=full_log.get('audit_member_snapshot') or [])
            if descriptions_finalized:
                _rebind_s1_same_name_gain_receipt_after_description_finalization(result_before_description_finalization, approved_result, full_log.get('audit_member_snapshot'))
                checkpoint_approved_result(approved_result, final_review_attempt)
        db_manager.remove()
        live_members = _reload_s1_party_members(party_id)
        if not live_members:
            current_app.logger.error('S1 探险 AI 写回失败：无法加载成员 party=%s log=%s', party_id, expedition_log_id)
            return
        if approved_result is None and review_rejected_all:
            current_app.logger.error('S1 剧情审核连续 %s 次打回，解散队伍 party=%s log=%s', max_review_attempts, party_id, expedition_log_id)
            review_detail = f'剧情审核连续 {max_review_attempts} 次打回'
            if observer_feedback:
                review_detail = f'{review_detail}；最后反馈：{observer_feedback[:500]}'
            generation_failure_detail = generation_failure_detail or review_detail
        applied_log_id, apply_error = _apply_expedition_ai_result(expedition_log_id, party_id, live_members, approved_result, pre_events=pre_events, target_dungeon=target_dungeon, has_special_event=has_special_event, is_extreme_crisis=is_extreme_crisis, special_event_type=special_event_type, special_event_risk_tier=special_event_risk_tier, stamina_max=stamina_max, stamina_cost=stamina_cost, consumed_buffs=consumed_buffs, generation_failure_detail=generation_failure_detail, generation_retry_trace=generation_retry_trace, generation_claim_token=generation_claim_token, before_publish=lambda: finalize_deferred_ai_token_usage(is_success=True), trait_context_receipt=actual_trait_context_receipt)
        queued_summaries = _enqueue_s1_description_summaries_after_apply(applied_log_id=applied_log_id, apply_error=apply_error, approved_result=approved_result, member_id_to_name=full_log.get('_member_id_to_name'))
        if queued_summaries:
            current_app.logger.info('S1 最终落库资产长描述压缩已异步入队 log=%s items=%s', applied_log_id, queued_summaries)
        finalize_deferred_ai_token_usage(is_success=approved_result is not None and applied_log_id is not None and (apply_error is None))
    except _S1ExpeditionGenerationClaimLost:
        current_app.logger.info('S1 探险旧 worker 已失去 generation claim，停止迟到写回 party=%s log=%s', party_id, expedition_log_id)
        finalize_deferred_ai_token_usage(is_success=False, failure_stage='pipeline_ownership', validation_error_code='stale_owner_unconsumed')
        return
    except Exception as exc:
        current_app.logger.exception('S1 探险流水线执行异常 party=%s log=%s error=%s', party_id, expedition_log_id, type(exc).__name__)
        recovery_context = pipeline_recovery_context
        if checkpoint_fail_close or not isinstance(recovery_context, dict):
            raise
        recovery_kind: Literal['generation_infrastructure', 'downstream_consumer'] | None = None
        recovery_disposition: str | None = None
        recovery_disposition = _checkpoint_s1_generation_infrastructure_exception(expedition_log_id, party_id=party_id, member_ids=list(recovery_context['member_ids']), generation_claim_token=generation_claim_token, error=exc, max_attempts=int(recovery_context['generation_infra_max_attempts']), retry_trace=generation_retry_trace)
        if recovery_disposition is not None:
            recovery_kind = 'generation_infrastructure'
        if recovery_disposition is None:
            recovery_disposition = _checkpoint_s1_unexpected_consumer_exception(expedition_log_id, party_id=party_id, member_ids=list(recovery_context['member_ids']), generation_claim_token=generation_claim_token, error=exc, max_attempts=int(recovery_context['consumer_max_attempts']), retry_trace=generation_retry_trace)
            if recovery_disposition is not None:
                recovery_kind = 'downstream_consumer'
        if recovery_disposition == 'retry':
            log_message = 'S1 探险上游 AI 基础设施异常已保存独立恢复预算，等待自动补投 ' if recovery_kind == 'generation_infrastructure' else 'S1 探险下游消费异常已保存同阶段恢复点，等待自动补投 '
            current_app.logger.warning(f'{log_message}party=%s log=%s stage=%s', party_id, expedition_log_id, generation_retry_trace[-1].get('stage'))
            return
        if recovery_disposition != 'exhausted':
            raise
        db_manager.remove()
        live_members = _reload_s1_party_members(party_id)
        if not live_members:
            current_app.logger.error('S1 探险恢复预算已达上限，但无法加载成员完成失败收尾 party=%s log=%s kind=%s', party_id, expedition_log_id, recovery_kind)
            raise
        failure_scope = '探险上游 AI 基础设施中断' if recovery_kind == 'generation_infrastructure' else '探险检查点下游消费异常'
        failure_detail = f'{failure_scope}连续达到上限，已停止自动重试；最后错误类型：{type(exc).__name__}'
        _apply_expedition_ai_result(expedition_log_id, party_id, live_members, None, pre_events=list(recovery_context['pre_events']), target_dungeon=str(recovery_context['target_dungeon']), has_special_event=bool(recovery_context['has_special_event']), is_extreme_crisis=bool(recovery_context['is_extreme_crisis']), special_event_type=recovery_context['special_event_type'], special_event_risk_tier=recovery_context['special_event_risk_tier'], stamina_max=int(recovery_context['stamina_max']), stamina_cost=int(recovery_context['stamina_cost']), consumed_buffs=list(recovery_context['consumed_buffs']), generation_failure_detail=failure_detail, generation_retry_trace=generation_retry_trace, generation_claim_token=generation_claim_token)
        current_app.logger.error('S1 探险恢复预算连续达到上限，已按既有失败流程收尾 party=%s log=%s kind=%s attempts=%s', party_id, expedition_log_id, recovery_kind, recovery_context['generation_infra_max_attempts'] if recovery_kind == 'generation_infrastructure' else recovery_context['consumer_max_attempts'])
    finally:
        _stop_expedition_generation_heartbeat(generation_heartbeat)
        finalize_deferred_ai_token_usage(is_success=False)
        _release_expedition_generation_lease(expedition_log_id, party_id, generation_claim_token=generation_claim_token)
def _requeue_pending_s1_expedition_ai(*, startup_recovery: bool=False) -> int:
    """有界轮转补投 generating AI 任务，不跨 Redis 调用持有数据库连接。"""
    from services.async_task_service import AsyncTaskService
    from plugins.adventurer_s1.services.s1_narrative_lease_service import release_inactive_expedition_narrative_leases
    global _s1_expedition_recovery_cursor
    if not _S1_EXPEDITION_RECOVERY_JOB_LOCK.acquire(blocking=False):
        current_app.logger.info('S1 generating 探险 AI recovery 已在执行，本轮跳过')
        return 0
    batch_size = min(1000, max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_RECOVERY_BATCH_SIZE', 200), 200)))
    budget_seconds = min(120, max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_RECOVERY_BUDGET_SECONDS', 20), 20)))
    started_at = time.monotonic()
    requeued = 0
    requeue_failed = 0
    released_narrative_leases = 0
    inspected = 0
    budget_exhausted = False
    try:
        with db_manager.transaction():
            cleaned_narrative_leases = release_inactive_expedition_narrative_leases()
        db_manager.remove()
        if cleaned_narrative_leases:
            current_app.logger.info('S1 recovery 清理终态、孤儿或过期叙事租约 %s 条', cleaned_narrative_leases)
        columns = (AdventurerS1ExpeditionLog.id, AdventurerS1ExpeditionLog.party_id, AdventurerS1ExpeditionLog.status, AdventurerS1ExpeditionLog.generation_phase, AdventurerS1ExpeditionLog.generation_phase_at, AdventurerS1ExpeditionLog.generation_owner_boot_id, AdventurerS1ExpeditionLog.created_at)
        base_query = AdventurerS1ExpeditionLog.query.filter_by(status='generating').with_entities(*columns)
        rows = base_query.filter(AdventurerS1ExpeditionLog.id > _s1_expedition_recovery_cursor).order_by(AdventurerS1ExpeditionLog.id.asc()).limit(batch_size).all()
        if len(rows) < batch_size:
            rows.extend(base_query.filter(AdventurerS1ExpeditionLog.id <= _s1_expedition_recovery_cursor).order_by(AdventurerS1ExpeditionLog.id.asc()).limit(batch_size - len(rows)).all())
        snapshots = [SimpleNamespace(id=int(row[0]), party_id=int(row[1]), status=row[2], generation_phase=row[3], generation_phase_at=row[4], generation_owner_boot_id=row[5], created_at=row[6], full_log={}) for row in rows]
        db_manager.remove()
        for log in snapshots:
            if inspected and time.monotonic() - started_at >= budget_seconds:
                budget_exhausted = True
                break
            inspected += 1
            log_id = int(log.id)
            party_id = int(log.party_id)
            _s1_expedition_recovery_cursor = log_id
            if not _can_requeue_expedition_generation(log):
                continue
            payload = {'party_id': party_id, 'expedition_log_id': log_id}
            owner_changed = not _generation_owner_belongs_to_current_boot(log.generation_owner_boot_id)
            delivery_state = AsyncTaskService.get_task_delivery_state('ai_s1_expedition', payload)
            task_is_active = delivery_state != 'missing'
            if startup_recovery and task_is_active and AsyncTaskService.has_restart_recovered_queued_task('ai_s1_expedition', payload):
                continue
            if log.generation_phase_at is None and delivery_state in {'queued', 'reserved'}:
                continue
            if task_is_active and (not owner_changed) and (log.generation_phase in _S1_SAME_BOOT_ACTIVE_TAKEOVER_PHASES):
                current = AdventurerS1ExpeditionLog.query.filter_by(id=log_id, status='generating').first()
                if current is None:
                    db_manager.remove()
                    continue
                log = SimpleNamespace(id=int(current.id), party_id=int(current.party_id), status=current.status, generation_phase=current.generation_phase, generation_phase_at=current.generation_phase_at, generation_owner_boot_id=current.generation_owner_boot_id, created_at=current.created_at, full_log=deepcopy(current.full_log or {}))
                db_manager.remove()
                if not _can_requeue_expedition_generation(log):
                    continue
                owner_changed = not _generation_owner_belongs_to_current_boot(log.generation_owner_boot_id)
            active_same_boot_takeover = bool(task_is_active and (not owner_changed) and _can_take_over_active_same_boot_generation(log))
            if active_same_boot_takeover:
                current_app.logger.warning('S1 generating 下游心跳已超时但异步锁仍活跃，轮换 attempt 接管 log=%s party=%s phase=%s owner=%s', log_id, party_id, log.generation_phase, log.generation_owner_boot_id)
            if log.generation_phase_at is None or owner_changed or active_same_boot_takeover:
                AsyncTaskService.force_release_task_lock('ai_s1_expedition', payload)
            elif task_is_active:
                continue
            if log.generation_phase_at is None or owner_changed or (not task_is_active):
                from plugins.adventurer_s1.services.s1_narrative_lease_service import release_narrative_leases_for_log
                with db_manager.transaction():
                    released_narrative_leases += release_narrative_leases_for_log(log_id)
                db_manager.remove()
            if AsyncTaskService.submit_task('ai_s1_expedition', payload):
                requeued += 1
            else:
                requeue_failed += 1
                current_app.logger.warning('S1 补投 generating 探险 AI 任务失败 log=%s party=%s phase=%s owner=%s startup_recovery=%s', log_id, party_id, log.generation_phase, log.generation_owner_boot_id, startup_recovery)
        if budget_exhausted:
            current_app.logger.warning('S1 generating 探险 AI recovery 达到单轮耗时预算 inspected=%s candidates=%s batch_size=%s budget_seconds=%s cursor=%s', inspected, len(snapshots), batch_size, budget_seconds, _s1_expedition_recovery_cursor)
        if requeued:
            current_app.logger.info('S1 补投 generating 探险 AI 任务 %s 条', requeued)
        if released_narrative_leases:
            current_app.logger.info('S1 补投前释放无活动 worker 的叙事租约 %s 条', released_narrative_leases)
        if requeue_failed:
            current_app.logger.warning('S1 generating 探险 AI 补投失败共 %s 条', requeue_failed)
        return requeued
    finally:
        try:
            db_manager.remove()
        finally:
            _S1_EXPEDITION_RECOVERY_JOB_LOCK.release()
def _roll_special_dungeon_directives(special: dict, members) -> dict[int, str | dict]:
    """门票制特殊副本：后端逐成员静默掷骰，返回 {member_id: directive}。

    转职试炼场 / 祛厄药庐：每名成员均有一条判定（成功或失败）。
    directive 为 str（class/skill/trait 机缘或 *_failed）。
    """
    directives: dict[int, str | dict] = {}
    kind = special.get('reward_kind')
    for m in members:
        if kind == 'class':
            has_class = bool((getattr(m, 'character_class', '') or '').strip())
            if has_class:
                if random.random() <= float(special.get('advance_class_prob', 0) or 0):
                    directives[m.id] = 'advance_class'
                else:
                    directives[m.id] = 'advance_class_failed'
            elif random.random() <= float(special.get('new_class_prob', 0) or 0):
                directives[m.id] = 'new_class'
            else:
                directives[m.id] = 'new_class_failed'
        elif kind == 'skill':
            if random.random() <= float(special.get('new_skill_prob', 0) or 0):
                directives[m.id] = 'new_skill'
        elif kind == 'trait_relief':
            from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import list_trait_relief_candidates
            if list_trait_relief_candidates(m):
                directives[m.id] = 'remove_trait'
            else:
                directives[m.id] = 'remove_trait_failed'
    return directives
def run_s1_expedition(party_id, *, require_full: bool=False):
    """对一个已满员/出发的小队发起探险：同步写 generating 占位日志，AI 剧本异步生成。

    返回 (log_id, error)。log_id 对应 status=generating 的占位日志；AI 完成后由
    AsyncTaskService worker 转为 in_progress 并首批推进。自动调度传
    ``require_full=True``，管理员强制发车仍可显式跳过满员要求。
    """
    from .s1_campaign_task_admission_service import S1CampaignTaskAdmissionService as TaskAdmission, ADMISSION_CANCEL_NOTICE, notify_task_cancellations_after_commit, timeout_notice
    party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
    if not party:
        return (None, '小队不存在')
    members = _load_s1_party_members_ordered(party_id)
    if not members:
        return (None, '小队没有成员')
    target_dungeon = party.target_dungeon or S1_DUNGEONS[0]['name']
    independent_run = is_s1_independent_dungeon(target_dungeon)
    activity = campaign_world(target_dungeon)
    entry = get_s1_special_dungeon(target_dungeon)
    if entry and (not entry.get('entry_enabled', True)):
        return (None, '该副本尚未开放')
    from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
    if not S1WorldLocationStateService.is_dungeon_dispatchable(target_dungeon):
        existing_log = AdventurerS1ExpeditionLog.query.filter(AdventurerS1ExpeditionLog.party_id == party.id).order_by(AdventurerS1ExpeditionLog.created_at.desc(), AdventurerS1ExpeditionLog.id.desc()).first()
        if existing_log:
            return (existing_log.id, None)
        if activity:
            cancellation_notice = TaskAdmission.cancel_closed_activity_party(party.id)
            if cancellation_notice:
                return (None, cancellation_notice)
        return (None, f'地牢【{target_dungeon}】当前没有可正常进入的探索路径')
    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_service import AdventurerS1DataAppealService
    pending_revert_ids = AdventurerS1DataAppealService.pending_cross_character_revert_adventurer_ids([int(member.id) for member in members])
    if pending_revert_ids:
        return (None, f"队员存在待完成的违规申诉资产撤销，暂不能再次出发（角色 ID: {', '.join(map(str, pending_revert_ids))}）")
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
    departure_trim_events: list[str] = []
    trimmed_any = False
    for m in members:
        summary, trim_err = AdventurerS1Service.enforce_departure_carry_limits(m.id)
        if trim_err and (not summary):
            return (None, trim_err)
        if not summary:
            continue
        trimmed_any = True
        parts: list[str] = []
        moved = summary.get('equipment_moved') or []
        unequipped = summary.get('skills_unequipped') or []
        if moved:
            parts.append(f'{len(moved)} 件装备已收入仓库')
        if unequipped:
            parts.append(f'{len(unequipped)} 个技能已放回技能池')
        if parts:
            departure_trim_events.append(f"📦 {m.name} 出发前行囊整理：{'，'.join(parts)}。")
        if trim_err:
            current_app.logger.warning('S1 发车行囊整理部分失败 adv=%s: %s', m.id, trim_err)
    if trimmed_any:
        members = _reload_s1_party_members(party_id)
    dungeon_info = get_s1_dungeon_info(target_dungeon)
    special_dungeon = get_s1_special_dungeon(target_dungeon)
    if special_dungeon:
        ticket_name = special_dungeon['ticket_item']
        member_ids = [m.id for m in members]
        carried_ids = {row[0] for row in AdventurerS1Equipment.query.with_entities(AdventurerS1Equipment.adventurer_id).filter(AdventurerS1Equipment.adventurer_id.in_(member_ids), AdventurerS1Equipment.name == ticket_name).all()}
        missing_names = [m.name for m in members if m.id not in carried_ids]
        if missing_names:
            return (None, f"特殊副本【{target_dungeon}】发车失败：{'、'.join(missing_names)}未随身携带「{ticket_name}」")
    special_directives_roll = _roll_special_dungeon_directives(special_dungeon, members) if special_dungeon else {}
    hidden_dungeon_context, hidden_dungeon_reward_plan = roll_hidden_dungeon(target_dungeon, members) if not independent_run and (not activity) else (None, None)
    has_special_event, is_extreme_crisis, special_event_type, special_event_risk_tier = _roll_special_event(dungeon_info['min_level']) if not independent_run and (not activity) else (False, False, None, None)
    warning_title, special_event_mode = _build_s1_special_event_prompt_context(has_special_event, is_extreme_crisis, special_event_type, risk_tier=special_event_risk_tier)
    special_event_mode = _build_s1_special_event_mode_note(special_event_mode, target_dungeon=target_dungeon)
    pre_events = list(departure_trim_events)
    special_event_event_index = len(pre_events) if warning_title else None
    if warning_title:
        pre_events.append(warning_title)
    stamina_max = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
    stamina_cost = int(current_app.config.get('ADVENTURER_S1_STAMINA_COST_PER_DUNGEON', 80))
    stamina_min = int(current_app.config.get('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80))
    recovery_rate = int(current_app.config.get('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1))
    insufficient = []
    for m in members:
        cur = m.get_current_stamina(max_stamina=stamina_max, recovery_per_minute=recovery_rate)
        if cur <= stamina_min:
            insufficient.append(f'{m.name}(精力{cur})')
    if insufficient:
        return (None, f"以下冒险者精力不足，无法出发：{', '.join(insufficient)}")
    from plugins.adventurer_s1.services.s1_dining_service import S1DiningService
    dining_events = S1DiningService.build_departure_buff_events(members)
    if dining_events:
        pre_events = list(dining_events) + list(pre_events)
    hidden_boss_context = S1HiddenBossService.resolve_clue_hidden_boss_for_party(target_dungeon, party.id, members) if not independent_run and (not activity) else None
    if not independent_run and (not activity) and (not hidden_boss_context):
        hidden_boss_context = S1HiddenBossService.resolve_task_hidden_boss_for_party(target_dungeon, party.id, members)
    if hidden_boss_context:
        boss_name = str(hidden_boss_context.get('name') or '隐藏Boss')
        if hidden_boss_context.get('trigger_mode') == 'clue':
            pre_events.append(f'👑 队伍集结四条线索，指向{target_dungeon}的隐藏Boss「{boss_name}」。')
        else:
            pre_events.append(f'👑 本次委托探险遭遇{target_dungeon}的地图唯一隐藏Boss「{boss_name}」。')
    commission_context = S1CommissionService.roll_encounters_for_party(target_dungeon, [m.id for m in members]) if not independent_run and (not activity) else []
    narrative_seed = build_s1_expedition_narrative_seed()
    locus_target_dungeon = str((hidden_dungeon_context or {}).get('dungeon') or target_dungeon).strip()
    departure_continuation_location_ids = set()
    try:
        from plugins.adventurer_s1.services.s1_expedition_locus_service import load_continuation_location_ids
        departure_continuation_location_ids = load_continuation_location_ids(locus_target_dungeon)
    except Exception as exc:
        current_app.logger.warning('S1 发车读取续篇地点偏好失败，沿用普通落点选择 dungeon=%s error=%s', locus_target_dungeon, str(exc)[:300])
    try:
        departure_locus_location_snapshot = S1WorldLocationStateService.build_snapshot(locus_target_dungeon, include_internal_locus_metadata=True, preferred_location_ids=departure_continuation_location_ids)
    except Exception as exc:
        current_app.logger.warning('S1 发车读取局部探索域候选失败，改用未登记局部域 dungeon=%s error=%s', locus_target_dungeon, str(exc)[:300])
        departure_locus_location_snapshot = {'target_dungeon': locus_target_dungeon, 'locations': []}
    departure_msg = _build_s1_expedition_departure_msg()
    base_full_log = {'status': 'generating', 'data_appeal_full_text_default': True, S1_GOLD_SETTLEMENT_VERSION_FIELD: S1_GOLD_SETTLEMENT_CONTRACT_VERSION, 'events': [], 'pending_pre_events': list(pre_events), 'departure_msg': departure_msg, 'has_special_event': has_special_event, 'is_extreme_crisis': is_extreme_crisis, 'special_event_type': special_event_type, 'special_event_risk_tier': special_event_risk_tier, 'special_event_event_index': special_event_event_index, 'hidden_boss_context': hidden_boss_context, 'commission_context': commission_context, 'hidden_dungeon_context': hidden_dungeon_context, HIDDEN_DUNGEON_REWARD_PLAN_KEY: hidden_dungeon_reward_plan, 'narrative_seed': narrative_seed}
    if independent_run:
        from .s1_independent_dungeon_service import MATERIAL_REWARD_INPUT_KEY, freeze_material_reward_input
        base_full_log[MATERIAL_REWARD_INPUT_KEY] = freeze_material_reward_input()
    expected_member_ids = sorted((int(member.id) for member in members))
    log_id = None
    with notify_task_cancellations_after_commit() as cancellation_changed, db_manager.transaction():
        from plugins.adventurer_s1.models import S1_EXPEDITION_GENERATION_PHASE_MAIN_AI
        locked_members = AdventurerS1.query.populate_existing().filter(AdventurerS1.id.in_(expected_member_ids)).order_by(AdventurerS1.id.asc()).with_for_update().all()
        if [int(member.id) for member in locked_members] != expected_member_ids:
            return (None, '小队成员已变化，已取消本次发车')
        locked_party = AdventurerS1Party.query.populate_existing().filter_by(id=party_id).with_for_update().first()
        if not locked_party:
            return (None, '小队不存在')
        existing_log = AdventurerS1ExpeditionLog.query.filter(AdventurerS1ExpeditionLog.party_id == locked_party.id).order_by(AdventurerS1ExpeditionLog.created_at.desc(), AdventurerS1ExpeditionLog.id.desc()).first()
        if existing_log:
            return (existing_log.id, None)
        if locked_party.status != 'forming':
            return (None, f'小队当前状态为 {locked_party.status}，已被其他调度处理')
        current_member_ids = sorted((row.adventurer_id for row in AdventurerS1PartyMember.query.filter_by(party_id=locked_party.id).order_by(AdventurerS1PartyMember.adventurer_id.asc()).all()))
        if current_member_ids != expected_member_ids:
            return (None, '小队成员已变化，已取消本次发车')
        if TaskAdmission.cancel_locked_party(locked_party, now=_portable_utc_now()):
            cancellation_changed[0] = True
            return (None, timeout_notice())
        if require_full and len(current_member_ids) < int(locked_party.target_size or 0):
            return (None, '小队人数不足，已取消本次自动发车')
        members = locked_members
        if not S1WorldLocationStateService.lock_dungeon_dispatchable(target_dungeon):
            if activity and TaskAdmission.cancel_locked_party(locked_party, now=_portable_utc_now(), expired_only=False):
                cancellation_changed[0] = True
                return (None, ADMISSION_CANCEL_NOTICE)
            return (None, f'地牢【{target_dungeon}】当前没有可正常进入的探索路径')
        member_ids = current_member_ids
        pending_revert_ids = AdventurerS1DataAppealService.pending_cross_character_revert_adventurer_ids(member_ids)
        if pending_revert_ids:
            return (None, f"队员存在待完成的违规申诉资产撤销，暂不能再次出发（角色 ID: {', '.join(map(str, pending_revert_ids))}）")
        log = AdventurerS1ExpeditionLog(party_id=locked_party.id, party_name=locked_party.name, target_dungeon=target_dungeon, hidden_dungeon=(hidden_dungeon_context or {}).get('dungeon'), status='generating', special_event_theme=resolve_s1_special_event_theme(base_full_log) or '', generation_phase=S1_EXPEDITION_GENERATION_PHASE_MAIN_AI, generation_phase_at=None, full_log={**base_full_log})
        ok, err = db_manager.add(log)
        if not ok:
            return (None, err)
        db_manager.flush()
        log_id = log.id
        try:
            from plugins.adventurer_s1.services.s1_expedition_locus_service import EXPEDITION_LOCUS_LOG_FIELD, build_expedition_locus_snapshot
            expedition_locus = build_expedition_locus_snapshot(int(log.id), locus_target_dungeon, departure_locus_location_snapshot, new_frontier_percent=int(current_app.config.get('ADVENTURER_S1_EXPEDITION_LOCUS_NEW_FRONTIER_PERCENT', 40) or 0), preferred_location_ids=departure_continuation_location_ids)
            if expedition_locus:
                frozen_full_log = dict(log.full_log or {})
                frozen_full_log[EXPEDITION_LOCUS_LOG_FIELD] = expedition_locus
                log.full_log = frozen_full_log
        except Exception as exc:
            current_app.logger.warning('S1 发车冻结局部探索域失败，继续使用地牢级上下文 log=%s dungeon=%s error=%s', log.id, locus_target_dungeon, str(exc)[:300])
        from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
        from plugins.adventurer_s1.services.s1_expedition_locus_service import expedition_locus_scope_key_from_full_log
        dispatch_context = S1CampaignDispatchService.freeze_departure(log, locked_party, locus_scope=expedition_locus_scope_key_from_full_log(log.full_log, expected_target_dungeon=locus_target_dungeon, expected_expedition_log_id=log.id))
        if activity and (not (log.full_log or {}).get('activity_world')):
            deleted, delete_error = db_manager.delete_instance(log)
            if not deleted:
                raise RuntimeError(delete_error or '活动发车占位回收失败')
            db_manager.flush()
            if not TaskAdmission.cancel_locked_party(locked_party, now=_portable_utc_now(), expired_only=False):
                raise RuntimeError('未出发的活动小队取消失败')
            cancellation_changed[0] = True
            return (None, ADMISSION_CANCEL_NOTICE)
        err = refresh_expedition_log_members_for_log(log, [m.id for m in members])
        if err:
            return (None, err)
        from plugins.adventurer_s1.services.adventurer_s1_party_service import _clear_departure_votes
        from plugins.adventurer_s1.services.adventurer_s1_tavern_service import invalidate_parties_board_cache
        _clear_departure_votes(party_id)
        locked_party.status = 'exploring'
        if special_dungeon:
            from plugins.adventurer_s1.models import AdventurerS1SpecialPartySlot
            db_manager.query(AdventurerS1SpecialPartySlot).filter_by(party_id=locked_party.id).delete(synchronize_session=False)
        invalidate_parties_board_cache()
        now = _portable_utc_now()
        ticket_consumed_ids: list[int] = []
        ticket_consumptions: list[dict] = []
        for m in members:
            locked_m = AdventurerS1.query.filter_by(id=m.id).with_for_update().first()
            if not locked_m:
                current_app.logger.warning('S1 发车跳过：成员不存在 id=%s', m.id)
                continue
            if locked_m.status not in ('in_party', 'resting', 'idle'):
                current_app.logger.warning('S1 发车跳过：成员状态异常 id=%s status=%s', m.id, locked_m.status)
                continue
            if special_dungeon:
                from plugins.adventurer_s1.services.s1_item_instance_service import consume_one_by_name_carry_only
                ok_ticket, ticket_err = consume_one_by_name_carry_only(locked_m.id, special_dungeon['ticket_item'], source_type='special_dungeon_entry', description=f'进入【{target_dungeon}】消耗门票')
                if ok_ticket:
                    ticket_consumed_ids.append(locked_m.id)
                    ticket_consumptions.append({'adventurer_id': int(locked_m.id), 'ticket_item': str(special_dungeon['ticket_item']), 'quantity': 1})
                else:
                    raise RuntimeError(f'S1 特殊副本发车扣票失败 adv={locked_m.id} dungeon={target_dungeon}: {ticket_err}')
            locked_m.status = 'exploring'
            cur = locked_m.get_current_stamina(max_stamina=stamina_max, recovery_per_minute=recovery_rate)
            locked_m.stamina = max(0, int(cur) - stamina_cost)
            locked_m.stamina_updated_at = now
        if special_dungeon:
            directives = {str(adv_id): special_directives_roll[adv_id] for adv_id in ticket_consumed_ids if adv_id in special_directives_roll}
            merged = dict(log.full_log or {})
            merged['special_dungeon_context'] = {'dungeon': target_dungeon, 'reward_kind': special_dungeon.get('reward_kind'), 'directives': directives}
            merged[_SPECIAL_DUNGEON_TICKET_CONSUMPTIONS_KEY] = ticket_consumptions
            log.full_log = merged
        from plugins.adventurer_s1.services.s1_appeal_departure_guard import DEPARTURE_ASSET_IDS_KEY, freeze_departure_asset_ids
        merged = dict(log.full_log or {})
        merged[DEPARTURE_ASSET_IDS_KEY] = freeze_departure_asset_ids(member_ids)
        log.full_log = merged
    if hidden_boss_context:
        S1HiddenBossService.attach_log(target_dungeon, party.id, log_id, clue_bureau_boss_id=hidden_boss_context.get('clue_bureau_boss_id'))
    if not _submit_s1_expedition_ai_task(party_id, log_id):
        return (log_id, '探险暂时未能启程，系统将自动重试')
    return (log_id, None)
def _calculate_s1_progress_step(full_log: dict) -> int:
    """计算本次推进应放出的事件数：剩余事件 / 剩余推送次数 向上取整。

    公式与 legacy `_calculate_expedition_progress_step` 一致，仅 config key 换成
    S1 专属 `ADVENTURER_S1_LOG_MAX_PROGRESS_PUSHES`，保证单条日志最多 N 批推完。
    """
    events = full_log.get('events', [])
    current_index = _coerce_int(full_log.get('current_event_index', 0), 0)
    remaining_events = max(0, len(events) - current_index)
    if remaining_events <= 0:
        return 0
    max_pushes = max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_LOG_MAX_PROGRESS_PUSHES', 8), 8))
    progress_push_count = max(0, _coerce_int(full_log.get('progress_push_count', 0), 0))
    remaining_pushes = max(1, max_pushes - progress_push_count)
    return max(1, (remaining_events + remaining_pushes - 1) // remaining_pushes)
def _enqueue_s1_trait_recovery_ai(log_id: int, party_id: int, member_updates: list) -> None:
    """探险结算后投递恢复 AI 任务：若本次有新增/修改特质，异步为其回填恢复剧本。

    探险 AI 不再产出恢复字段（policy/stage/required/recovered_desc），改由独立的恢复 AI
    在此异步补写。submit_task 自带去重，同一 expedition_log 的重复投递会被跳过。
    """
    has_new_traits = any((isinstance(u, dict) and u.get('new_traits') for u in member_updates or []))
    if not has_new_traits:
        return
    from services.async_task_service import AsyncTaskService
    AsyncTaskService.submit_task('ai_s1_trait_recovery', {'expedition_log_id': int(log_id), 'party_id': int(party_id)})
def _enqueue_s1_desc_backfill_ai(log_id: int, member_updates: list) -> None:
    """探险结算后投递描述补全 AI 任务：若本次有 new_traits 或 new_skills，异步为空描述条目补全。

    与 trait_recovery_ai 并列投递、互不依赖。submit_task 自带去重。
    """
    has_desc_candidates = any((isinstance(u, dict) and (u.get('new_traits') or u.get('new_skills')) for u in member_updates or []))
    if not has_desc_candidates:
        return
    from services.async_task_service import AsyncTaskService
    AsyncTaskService.submit_task('ai_s1_desc_backfill', {'expedition_log_id': int(log_id)})
def progress_s1_expedition(party_id):
    """推进小队探险日志一批事件；推完最后一批才结算写回。返回 (status, error)。

    status：'generating'（AI 未返回，跳过）/ 'in_progress'（本批已推进，未完）/
    'completed'（本批推完并已结算）。由 progress tick 周期调用，admin 路由可手动单步。
    """
    party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
    if not party:
        return (None, '小队不存在')
    log = AdventurerS1ExpeditionLog.query.filter_by(party_id=party.id).order_by(AdventurerS1ExpeditionLog.created_at.desc()).first()
    if not log or log.status not in ('in_progress', 'generating'):
        return (None, '没有可推进的探险日志')
    if log.status == 'generating':
        return ('generating', None)
    members = _load_s1_party_members_ordered(party_id)
    settle_now = False
    outline_candidate_published = False
    clue_boss_settled_in_transaction = False
    hidden_boss_post_commit_callbacks = []
    with db_manager.transaction():
        log = AdventurerS1ExpeditionLog.query.filter_by(id=log.id).with_for_update().first()
        if not log or log.status not in ('in_progress',):
            return (None, '日志状态已变更')
        full_log = dict(log.full_log)
        effective_dungeon = log.hidden_dungeon or log.target_dungeon
        from .s1_independent_dungeon_service import project_independent_dungeon_result
        project_independent_dungeon_result(full_log, effective_dungeon)
        events = full_log.get('events', [])
        current_index = _coerce_int(full_log.get('current_event_index', 0), 0)
        step = _calculate_s1_progress_step(full_log)
        current_index = min(len(events), current_index + step) if step > 0 else len(events)
        full_log['current_event_index'] = current_index
        full_log['progress_push_count'] = _coerce_int(full_log.get('progress_push_count', 0), 0) + 1
        if current_index >= len(events):
            settle_now = True
            full_log['status'] = 'completed'
        if settle_now:
            draft = full_log.get('pre_narrative_resolution')
            if isinstance(draft, dict) and draft.get('resolution_contract_version') == 3:
                from plugins.adventurer_s1.services.s1_encounter_draft_contract import validate_compact_narrative_result
                draft_errors = validate_compact_narrative_result(full_log, draft)
                if draft_errors:
                    raise RuntimeError('S1 交锋推演结算结果不一致: ' + '; '.join(draft_errors))
            has_pending_world_state_protocol = 'pending_location_state_changes' in full_log or 'pending_dungeon_explorability_change' in full_log
            if has_pending_world_state_protocol:
                from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
                world_state_outcome = S1WorldLocationStateService.apply_pending_changes_at_completion(int(log.id), effective_dungeon, list(full_log.pop('pending_location_state_changes', []) or []), full_log.pop('pending_dungeon_explorability_change', None))
                full_log['location_state_application'] = world_state_outcome['location_state_application']
                full_log['dungeon_explorability_application'] = world_state_outcome['dungeon_explorability_application']
                if world_state_outcome['errors']:
                    full_log['world_state_application_errors'] = world_state_outcome['errors']
                    current_app.logger.warning('S1 世界状态在剧情完成后落地失败但不阻塞玩家结算 log=%s errors=%s', log.id, world_state_outcome['errors'])
                else:
                    full_log.pop('world_state_application_errors', None)
                from plugins.adventurer_s1.services.s1_world_outline_service import resolve_ecology_debt_from_location_state
                inherited_ecology = resolve_ecology_debt_from_location_state(full_log, expedition_log_id=int(log.id), target_dungeon=effective_dungeon)
                if inherited_ecology is not None:
                    full_log['ecology_continuity_debt'] = inherited_ecology
            same_name_gain_receipt_errors = _validate_s1_same_name_gain_review_receipt(full_log)
            if same_name_gain_receipt_errors:
                raise RuntimeError('S1 同名装备语义裁决回执非法: ' + '；'.join(same_name_gain_receipt_errors))
            member_update_errors = normalize_s1_member_updates_by_id(full_log, [member.id for member in members])
            if member_update_errors:
                raise RuntimeError('S1 结算成员更新非法: ' + '；'.join(member_update_errors))
            member_ids = [m.id for m in members]
            if member_ids:
                locked_members = AdventurerS1.query.filter(AdventurerS1.id.in_(member_ids)).order_by(AdventurerS1.id.asc()).populate_existing().with_for_update().all()
                locked_map = {m.id: m for m in locked_members}
                members = [locked_map.get(m.id) or m for m in members]
                from plugins.adventurer_s1.services.s1_item_instance_service import lock_warehouse_aggregates
                lock_warehouse_aggregates(member_ids)
            destructive_trait_ids, destructive_skill_ids = final_fate_destructive_predecessor_ids(full_log)
            locked_destructive_traits = AdventurerS1Trait.query.filter(AdventurerS1Trait.id.in_(destructive_trait_ids)).order_by(AdventurerS1Trait.id.asc()).with_for_update().populate_existing().all() if destructive_trait_ids else []
            locked_destructive_skills = AdventurerS1Skill.query.filter(AdventurerS1Skill.id.in_(destructive_skill_ids)).order_by(AdventurerS1Skill.id.asc()).with_for_update().populate_existing().all() if destructive_skill_ids else []
            full_log, destructive_quarantine = quarantine_unexecutable_final_fate_destructive_changes(full_log, members, locked_traits=locked_destructive_traits, locked_skills=locked_destructive_skills)
            if destructive_quarantine:
                current_app.logger.warning('S1 最终命途永久删除权限因最终状态漂移被单调撤销 log=%s actions=%s', log.id, destructive_quarantine)
            destructive_settlement_errors = validate_final_fate_destructive_settlement(full_log, members, locked_traits=locked_destructive_traits, locked_skills=locked_destructive_skills)
            if destructive_settlement_errors:
                raise RuntimeError('S1 最终命途永久删除凭证非法: ' + '；'.join(destructive_settlement_errors))
            destructive_ids_by_member = final_fate_destructive_approved_asset_ids(full_log)
            destructive_owned_paths = final_fate_destructive_owned_paths(full_log)
            locked_trait_by_id = {int(row.id): row for row in locked_destructive_traits if getattr(row, 'id', None) is not None}
            locked_skill_by_id = {int(row.id): row for row in locked_destructive_skills if getattr(row, 'id', None) is not None}
            exact_asset_names: dict[int, dict[str, set[str]]] = {}
            for adventurer_id, by_kind in destructive_ids_by_member.items():
                exact_asset_names[adventurer_id] = {'trait': {str(locked_trait_by_id[asset_id].name) for asset_id in by_kind.get('trait', set()) if asset_id in locked_trait_by_id}, 'skill': {str(locked_skill_by_id[asset_id].name) for asset_id in by_kind.get('skill', set()) if asset_id in locked_skill_by_id}}
            settled_gold = _materialize_expedition_gold_changes(full_log, [member.id for member in members], remainder_offset=log.id)
            if settled_gold:
                current_app.logger.info('S1 探险金币结算已派生 log=%s member_changes=%s', log.id, settled_gold)
            log.full_log = full_log
            item_dict, item_use_conditions = _build_item_catalog_context_for_members(members)
            updates = {_coerce_int(u.get('id'), -1): u for u in full_log.get('member_updates', []) if isinstance(u, dict) and u.get('id') is not None}
            stored_asset_description_summaries = persist_s1_asset_description_update_summaries(full_log, receipt=full_log.get(S1_ASSET_DESCRIPTION_UPDATE_RECEIPT_FIELD))
            if stored_asset_description_summaries:
                current_app.logger.info('S1 既有资产修改摘要已随结算批量激活 log=%s items=%s', log.id, stored_asset_description_summaries)
            from plugins.adventurer_s1.services.s1_item_world_unique_service import reconcile_expedition_item_grants_at_settlement
            reconciled_item_grants, unreconciled_item_grant_errors = reconcile_expedition_item_grants_at_settlement(list(updates.values()), members)
            if unreconciled_item_grant_errors:
                raise RuntimeError('S1 最终结算物品发放复核失败: ' + '；'.join(unreconciled_item_grant_errors))
            if reconciled_item_grants:
                full_log['settlement_item_grant_reconciliation'] = reconciled_item_grants
                current_app.logger.warning('S1 最终结算撤销无法到账的唯一物品 log=%s items=%s', log.id, reconciled_item_grants)
            dining_context_map = full_log.get('dining_context') or {}
            audit_member_snapshot_by_id = {_coerce_int(row.get('adventurer_id'), 0): row for row in full_log.get('audit_member_snapshot') or [] if isinstance(row, dict) and _coerce_int(row.get('adventurer_id'), 0) > 0}
            dungeon_min, dungeon_max = get_s1_dungeon_level_range(effective_dungeon)
            has_special_event = bool(full_log.get('has_special_event', False))
            is_extreme_crisis = bool(full_log.get('is_extreme_crisis', False))
            special_event_type = full_log.get('special_event_type')
            special_event_risk_tier = full_log.get('special_event_risk_tier')
            if has_special_event and (not special_event_risk_tier):
                special_event_risk_tier = classify_s1_special_event_risk(dungeon_min, is_extreme_crisis=is_extreme_crisis, special_event_type=special_event_type)
            from plugins.adventurer_s1.services.s1_fate_journey_service import resolve_fate_reward_level_range
            reward_min, reward_max = resolve_fate_reward_level_range(full_log.get('fate_journey_context'), dungeon_min, dungeon_max)
            from plugins.adventurer_s1.services.s1_item_world_unique_service import _build_party_guard_context, build_party_equipment_transfers, build_party_item_holders_map
            catalog_names: list[str] = []
            for update in updates.values():
                raw = (update or {}).get('new_equipments') or {}
                if isinstance(raw, dict):
                    catalog_names.extend((str(n).strip() for n in raw.keys() if str(n).strip()))
                raw_updates = (update or {}).get('equipment_updates') or []
                if isinstance(raw_updates, list):
                    catalog_names.extend((str(row.get('name') or '').strip() for row in raw_updates if isinstance(row, dict) and str(row.get('name') or '').strip()))
            party_holders_map, catalog_by_name, holder_custom_desc_flags = _build_party_guard_context(members, catalog_names)
            party_transfers = build_party_equipment_transfers(list(updates.values()), party_holders_map=party_holders_map, lock_sources=True)
            from plugins.adventurer_s1.services.s1_expedition_settlement_receipt_service import capture_s1_settlement_state, verify_s1_settlement_receipt
            settlement_state_before = capture_s1_settlement_state(list(updates), include_gold=False)
            settlement_gold_balances: dict[int, float] = {}
            insurance_contexts: list[dict] = []
            for adv in members:
                update = updates.get(adv.id)
                if update:
                    approved_asset_ids = destructive_ids_by_member.get(adv.id, {})
                    exact_removed_traits = [locked_trait_by_id[asset_id] for asset_id in approved_asset_ids.get('trait', set()) if asset_id in locked_trait_by_id] if (adv.id, 'removed_traits') in destructive_owned_paths else None
                    exact_removed_skills = [locked_skill_by_id[asset_id] for asset_id in approved_asset_ids.get('skill', set()) if asset_id in locked_skill_by_id] if (adv.id, 'removed_skills') in destructive_owned_paths else None
                    hp_before = int(adv.hp or 0)
                    level_before = int(adv.level or 1)
                    member_dining_ctx = dining_context_map.get(str(adv.id))
                    gold_balance_after = apply_update_to_s1_adventurer(adv, update, item_dict, dining_context=member_dining_ctx, party_transfers=party_transfers, party_holders_map=party_holders_map, catalog_by_name=catalog_by_name, holder_custom_desc_flags=holder_custom_desc_flags, expedition_log_id=log.id, record_history=not is_s1_independent_dungeon(effective_dungeon), item_use_conditions=item_use_conditions, exact_removed_traits=exact_removed_traits, exact_removed_skills=exact_removed_skills, frozen_member_snapshot=audit_member_snapshot_by_id.get(int(adv.id)), asset_description_update_receipt=full_log.get(S1_ASSET_DESCRIPTION_UPDATE_RECEIPT_FIELD))
                    if gold_balance_after is not None:
                        settlement_gold_balances[adv.id] = gold_balance_after
                    _apply_exp_and_level_ups(adv, update, reward_min, reward_max, has_special_event=has_special_event, is_extreme_crisis=is_extreme_crisis, special_event_risk_tier=special_event_risk_tier, dining_context=member_dining_ctx)
                    insurance_contexts.append({'adventurer': adv, 'member_update': update, 'hp_before': hp_before, 'level_before': level_before, 'level_after': int(adv.level or 1)})
                elif adv.status in ('exploring', 'in_party'):
                    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
                    AdventurerS1Service.enter_rest(adv)
            for reconciliation in reconciled_item_grants:
                update = updates.get(int(reconciliation['adventurer_id']))
                if update is None:
                    continue
                discarded = update.setdefault('discarded_equipments', [])
                discarded.append({'name': reconciliation['name'], 'quantity': reconciliation['quantity'], 'reason': reconciliation['reason'], 'message': reconciliation['message']})
            log.full_log = full_log
            _, hidden_reward_err = settle_hidden_dungeon_rewards(log, {int(member.id): member for member in members}, updates)
            if hidden_reward_err:
                raise RuntimeError(f'隐藏副本掉落结算失败: {hidden_reward_err}')
            full_log = dict(log.full_log or {})
            from .s1_independent_dungeon_service import settle_material_rewards
            settle_material_rewards(log, {int(member.id): member for member in members}, updates)
            full_log = dict(log.full_log or {})
            _append_s1_settlement_discard_events(full_log, updates, members)
            from .s1_team_loot_service import S1TeamLootService
            team_loot_ids = S1TeamLootService.register_settlement_in_transaction(log, full_log, {int(member.id): member for member in members})
            settlement_receipt = verify_s1_settlement_receipt(settlement_state_before, updates, settlement_gold_balances, exact_asset_names=exact_asset_names, party_transfers=party_transfers)
            from plugins.adventurer_s1.services.s1_asset_transfer_service import expedition_asset_transfers
            settlement_receipt['asset_transfers'] = expedition_asset_transfers(full_log)
            exact_flush_ok, exact_flush_error = db_manager.flush()
            if not exact_flush_ok:
                raise RuntimeError(f'S1 最终命途永久删除 ID 回执 flush 失败: {exact_flush_error}')
            remaining_exact_trait_ids = {int(row.id) for row in AdventurerS1Trait.query.filter(AdventurerS1Trait.id.in_(destructive_trait_ids)).all()} if destructive_trait_ids else set()
            remaining_exact_skill_ids = {int(row.id) for row in AdventurerS1Skill.query.filter(AdventurerS1Skill.id.in_(destructive_skill_ids)).all()} if destructive_skill_ids else set()
            expected_deleted_trait_ids = {asset_id for by_kind in destructive_ids_by_member.values() for asset_id in by_kind.get('trait', set())}
            expected_deleted_skill_ids = {asset_id for by_kind in destructive_ids_by_member.values() for asset_id in by_kind.get('skill', set())}
            initially_locked_trait_ids = set(locked_trait_by_id)
            initially_locked_skill_ids = set(locked_skill_by_id)
            actual_deleted_trait_ids = initially_locked_trait_ids - remaining_exact_trait_ids
            actual_deleted_skill_ids = initially_locked_skill_ids - remaining_exact_skill_ids
            if actual_deleted_trait_ids != expected_deleted_trait_ids or actual_deleted_skill_ids != expected_deleted_skill_ids:
                raise RuntimeError(f'S1 最终命途永久删除 ID 回执不一致: expected_traits={sorted(expected_deleted_trait_ids)};actual_traits={sorted(actual_deleted_trait_ids)};expected_skills={sorted(expected_deleted_skill_ids)};actual_skills={sorted(actual_deleted_skill_ids)}')
            settlement_receipt['final_fate_destructive_ids'] = {'status': 'passed', 'context_id': full_log.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD, {}).get('context_id') if isinstance(full_log.get(FINAL_FATE_DESTRUCTIVE_CONTEXT_FIELD), dict) else None, 'deleted_trait_ids': sorted(actual_deleted_trait_ids), 'deleted_skill_ids': sorted(actual_deleted_skill_ids)}
            full_log = dict(full_log)
            full_log['settlement_receipt'] = settlement_receipt
            if team_loot_ids:
                settlement_receipt['unassigned_items'] = {'status': 'registered', 'lot_ids': team_loot_ids}
            from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
            for adv in members:
                if adv.id in updates and adv.status == 'dead':
                    AdventurerS1Service.handle_death(adv)
            from plugins.adventurer_s1.services.s1_insurance_service import S1InsuranceService
            insurance_receipts = {row['adventurer_id']: row for row in settlement_receipt['members']}
            for context in insurance_contexts:
                insured_adv = context['adventurer']
                teammate_died = any((member.id != insured_adv.id and getattr(member, 'status', None) == 'dead' for member in members))
                _, insurance_err = S1InsuranceService.settle_insurance(insured_adv, context['hp_before'], int(insured_adv.hp or 0), member_update=context['member_update'], level_before=context['level_before'], level_after=context['level_after'], teammate_died=teammate_died, target_dungeon=log.target_dungeon, expedition_log_id=log.id, settlement_member_receipt=insurance_receipts.get(insured_adv.id))
                if insurance_err:
                    raise RuntimeError(f'保险结算失败: {insurance_err}')
            member_mental_states: dict[str, str] = {}
            for u in full_log.get('member_updates', []):
                if not isinstance(u, dict):
                    continue
                mid = u.get('id')
                ms = u.get('mental_state')
                if mid is not None and ms and isinstance(ms, str):
                    member_mental_states[str(mid)] = ms.strip()[:20]
            if member_mental_states:
                full_log['member_mental_states'] = member_mental_states
            if not is_s1_independent_dungeon(effective_dungeon):
                _apply_expedition_relationships(members, full_log)
            from plugins.adventurer_s1.services.s1_story_thread_service import apply_s1_story_thread_updates
            apply_s1_story_thread_updates(members, full_log, source_log_id=log.id)
            from plugins.adventurer_s1.services.s1_fate_journey_service import S1FateJourneyService
            S1FateJourneyService.settle_expedition(members, log, full_log)
            log.status = 'completed'
            log.result_summary = full_log.get('summary', '探险结束')
            from plugins.adventurer_s1.services.adventurer_s1_party_service import finalize_party_disband
            finalize_party_disband(party.id, member_ids=member_ids)
            from plugins.adventurer_s1.services.s1_narrative_lease_service import release_narrative_leases_for_log
            release_narrative_leases_for_log(log.id)
            S1CommissionService.settle_expedition(full_log.get('commission_context') or [], full_log.get('commission_results') or [], {m.id for m in members if getattr(m, 'status', None) != 'dead'}, expedition_log_id=log.id, hidden_boss_completed=bool(_coerce_int(full_log.get('hidden_boss_completed') or full_log.get('hidden_boss_commission_completed'), 0)), auto_commit=False)
            from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import mark_countermeasure_opportunities_from_snapshot, persist_countermeasure_applications_for_settlement, persist_completed_learning_for_settlement
            full_log['countermeasure_opportunity_persistence'] = mark_countermeasure_opportunities_from_snapshot(expedition_log_id=int(log.id), snapshot=full_log.get('generation_world_intel_injection'))
            full_log['countermeasure_application_persistence'] = persist_countermeasure_applications_for_settlement(expedition_log_id=int(log.id), final_result_data=full_log)
            full_log['countermeasure_learning_persistence'] = persist_completed_learning_for_settlement(expedition_log_id=int(log.id), final_result_data=full_log)
            from plugins.adventurer_s1.services.s1_waiting_player_npc_service import persist_completed_waiting_npc_updates
            full_log['waiting_npc_persistence'] = persist_completed_waiting_npc_updates(int(log.id), full_log)
            hidden_boss_context = full_log.get('hidden_boss_context')
            if isinstance(hidden_boss_context, dict) and hidden_boss_context.get('clue_bureau_boss_id'):
                S1HiddenBossService.settle_challenge(log.target_dungeon, party_id, log.id, full_log, members=members, auto_commit=False, post_commit_callbacks=hidden_boss_post_commit_callbacks)
                clue_boss_settled_in_transaction = True
        log.full_log = full_log
        if settle_now:
            from plugins.adventurer_s1.services.s1_world_outline_service import S1WorldOutlineService
            outline_candidate_published = S1WorldOutlineService.publish_candidate_for_completed_log(expedition_log_id=int(log.id), target_dungeon=log.hidden_dungeon or log.target_dungeon, final_result_data=full_log)
        err = refresh_expedition_log_members_for_log(log, [m.id for m in members])
        if err:
            return (None, err)
    if settle_now:
        for callback in hidden_boss_post_commit_callbacks:
            try:
                callback()
            except Exception as exc:
                current_app.logger.warning('S1 线索 Boss 提交后刷新失败 log=%s: %s', log.id, exc)
        if outline_candidate_published:
            try:
                S1WorldOutlineService.submit_maintenance_task()
            except Exception as exc:
                current_app.logger.warning('S1 世界大纲维护任务提交失败 log=%s: %s', log.id, exc)
        from plugins.adventurer_s1.services.adventurer_s1_task_service import AdventurerS1TaskService
        member_updates = (log.full_log or {}).get('member_updates', [])
        if not clue_boss_settled_in_transaction:
            S1HiddenBossService.settle_challenge(log.target_dungeon, party_id, log.id, log.full_log or {}, members=members)
        AdventurerS1TaskService.settle_expedition_tasks([m.id for m in members], log.target_dungeon, member_updates=member_updates, result_data=log.full_log or {}, expedition_log_id=log.id)
        _enqueue_s1_trait_recovery_ai(log.id, party_id, member_updates)
        _enqueue_s1_desc_backfill_ai(log.id, member_updates)
    try:
        from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
        if settle_now:
            socketio.emit('expedition_log_changed', {'action': 'completed', 'reason': 'expedition_finished', 'data': {'id': log.id, 'party_id': party_id, 'status': 'completed', 'team_loot_registered': bool(team_loot_ids)}}, namespace='/')
            socketio.emit('adventurer_changed', {'action': 'status_changed', 'reason': 'expedition_finished', 'ids': [m.id for m in members]}, namespace='/')
        else:
            socketio.emit('expedition_log_changed', {'action': 'progressed', 'reason': 'expedition_progressed', 'data': {'id': log.id, 'party_id': party_id, 'status': 'in_progress'}}, namespace='/')
    except Exception:
        pass
    return ('completed' if settle_now else 'in_progress', None)
def _get_latest_s1_log_for_party(party_id: int):
    return AdventurerS1ExpeditionLog.query.filter_by(party_id=party_id).order_by(AdventurerS1ExpeditionLog.created_at.desc()).first()
def _validate_admin_log_action(log_id: int, allowed_statuses: set[str]):
    log = AdventurerS1ExpeditionLog.query.session.get(AdventurerS1ExpeditionLog, log_id)
    if not log:
        return (None, '日志不存在')
    if not log.party_id:
        return (None, '该日志没有关联小队，无法执行操作')
    if log.status not in allowed_statuses:
        allowed = '、'.join(sorted(allowed_statuses))
        return (None, f'该日志状态为 {log.status}，只允许 {allowed}')
    latest = _get_latest_s1_log_for_party(log.party_id)
    if latest and latest.id != log.id:
        return (None, '该日志不是该小队的最新日志，无法操作')
    return (log, None)
def step_s1_expedition_log(log_id: int):
    """管理员按日志 id 手动推进一批事件。"""
    log, err = _validate_admin_log_action(log_id, {'in_progress'})
    if err:
        return (None, err)
    status, err = progress_s1_expedition(log.party_id)
    if err:
        return (None, err)
    return ({'status': status, 'log_id': log_id}, None)
def force_complete_s1_expedition_log(log_id: int):
    """管理员直接释放剩余事件并触发结算。"""
    log, err = _validate_admin_log_action(log_id, {'in_progress'})
    if err:
        return (None, err)
    with db_manager.transaction():
        locked_log = AdventurerS1ExpeditionLog.query.session.get(AdventurerS1ExpeditionLog, log_id, with_for_update=True)
        if not locked_log or locked_log.status != 'in_progress':
            raise RuntimeError('日志状态已变化，无法直接结算')
        full_log = dict(locked_log.full_log or {})
        events = full_log.get('events', []) or []
        full_log['current_event_index'] = len(events)
        locked_log.full_log = full_log
    status, err = progress_s1_expedition(log.party_id)
    if err:
        return (None, err)
    return ({'status': status, 'log_id': log_id}, None)
def retry_generating_s1_expedition_log(log_id: int):
    """管理员重试超时卡在 generating 的探险日志（沿用原日志，不重复扣精力）。"""
    log, err = _validate_admin_log_action(log_id, {'generating'})
    if err:
        return (None, err)
    if not is_s1_generating_stuck(log):
        minutes = _get_generating_gm_retry_minutes()
        return (None, f'生成尚未超时（需卡住 {minutes} 分钟后才可人工重试）')
    party = AdventurerS1Party.query.session.get(AdventurerS1Party, log.party_id)
    if not party:
        return (None, '小队不存在')
    if not _load_s1_party_members_ordered(party.id):
        return (None, '小队没有成员')
    if not _submit_s1_expedition_ai_task(party.id, log_id, force_retry=True):
        return (None, '探险 AI 任务提交失败')
    return (log_id, None)
def regenerate_s1_expedition_log(log_id: int):
    """管理员重新生成已失败（解散）的探险日志（重新发车，新建日志）。"""
    log, err = _validate_admin_log_action(log_id, {'failed'})
    if err:
        return (None, err)
    new_log_id, err = run_s1_expedition(log.party_id)
    if err:
        return (None, err)
    return ({'log_id': new_log_id, 'old_log_id': log_id}, None)
def get_daily_expedition_quota_state() -> tuple[int, int]:
    """返回 (今日主叙事已占用数, 每日配额)。

    普通探险与酒馆叙事活动共享同一硬闸门，避免活动额外抬高每日主剧情
    token 预算。失败的探险日志与已投递的活动组同样占用额度；0 表示不限制。
    """
    from plugins.adventurer_s1.services.s1_tavern_activity_service import get_daily_narrative_quota_state
    return get_daily_narrative_quota_state()
def _dispatch_departed_expeditions_locked(*, manual: bool=False, match_queued: bool=False) -> dict:
    """扫已到 departure_time 的 forming 队，逐支写 generating 日志并入队 AI 任务。

    供 plugin scheduler 周期调用；主探险 AI 由 AsyncTaskService worker 异步执行，
    本 tick 仅做同步准备（扣精力、占位日志），不阻塞 scheduler 线程。
    `get_departed_parties()` 过滤 `status='forming'`，重复 tick 不会对同一支队重复发车。

    每日配额（`ADVENTURER_S1_DAILY_EXPEDITION_QUOTA`）用尽时不发车：满员队保留
    forming 状态，配额恢复（次日）后自动补发。

    返回简短统计，便于日志观察。
    """
    from plugins.adventurer_s1.services.adventurer_s1_party_service import get_departed_parties, is_auto_dispatch_on_full_enabled, is_expedition_dispatch_paused
    if is_expedition_dispatch_paused():
        current_app.logger.info('S1 探险出发已暂停，跳过本轮自动发车')
        return {'dispatched': 0, 'failed': 0, 'party_ids': [], 'dispatch_paused': True}
    from plugins.adventurer_s1.services.s1_ai_runtime_config_service import get_ai_access_state
    ai_access = get_ai_access_state()
    if not ai_access['allowed']:
        current_app.logger.info('S1 AI 调用受控，跳过本轮发车：%s', ai_access['reason'])
        return {'dispatched': 0, 'failed': 0, 'party_ids': [], 'ai_blocked': True, 'ai_blocked_reason': ai_access['reason']}
    if not manual and (not is_auto_dispatch_on_full_enabled()):
        current_app.logger.info('S1 ADVENTURER_S1_AUTO_DISPATCH_ON_FULL=false，跳过本轮自动发车（管理员可手动触发）')
        return {'dispatched': 0, 'failed': 0, 'party_ids': [], 'auto_dispatch_disabled': True}
    departed_parties = get_departed_parties()
    if not departed_parties and match_queued:
        from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import AdventurerS1MatchmakingService
        AdventurerS1MatchmakingService.tick_matchmaking(enqueue_automatic=False, dispatch_immediately=False)
        departed_parties = get_departed_parties()
    from plugins.adventurer_s1.services.s1_world_location_state_service import S1WorldLocationStateService
    closed_dungeons = S1WorldLocationStateService.closed_dungeon_names((party.target_dungeon for party in departed_parties))
    blocked_party_ids = [party.id for party in departed_parties if party.target_dungeon in closed_dungeons]
    party_ids = [party.id for party in departed_parties if party.target_dungeon not in closed_dungeons]
    if not party_ids:
        result = {'dispatched': 0, 'failed': 0, 'party_ids': []}
        if blocked_party_ids:
            result['closed_dungeon_blocked'] = len(blocked_party_ids)
        return result
    cap = 1
    used, quota = get_daily_expedition_quota_state()
    if quota > 0:
        from plugins.adventurer_s1.services.adventurer_s1_expedition_throttle_service import compute_dispatch_allowance, minutes_since_last_departure
        remaining = max(0, quota - used)
        if remaining <= 0:
            current_app.logger.info('S1 每日探险配额已用尽（%s/%s），%s 支待发队推迟', used, quota, len(party_ids))
            return {'dispatched': 0, 'failed': 0, 'party_ids': [], 'quota_blocked': len(party_ids)}
        else:
            since_last = minutes_since_last_departure()
            allowance = compute_dispatch_allowance(used, quota, since_last)
            cap = min(remaining, allowance)
            if cap <= 0:
                current_app.logger.info('S1 动态发车间隔未到（used=%s/%s，距上次 %.2f 分钟），%s 支待发队推迟', used, quota, since_last, len(party_ids))
                return {'dispatched': 0, 'failed': 0, 'party_ids': [], 'pace_throttled': len(party_ids)}
    dispatched: list[int] = []
    failed: list[int] = []
    for pid in party_ids:
        try:
            log_id, err = run_s1_expedition(pid, require_full=True)
            if err or not log_id:
                failed.append(pid)
                current_app.logger.warning('S1 调度发车失败 party=%s err=%s', pid, err)
                continue
            dispatched.append(pid)
            if len(dispatched) >= cap:
                break
        except Exception as e:
            failed.append(pid)
            current_app.logger.exception('S1 调度发车异常 party=%s: %s', pid, e)
    result = {'dispatched': len(dispatched), 'failed': len(failed), 'party_ids': dispatched}
    if blocked_party_ids:
        result['closed_dungeon_blocked'] = len(blocked_party_ids)
    return result
def dispatch_departed_expeditions(*, manual: bool=False, match_queued: bool=False) -> dict:
    """集群互斥的自动发车入口，避免多 worker 同一刻度重复/并行放车。

    manual=True 时供管理员手动触发，不受 ADVENTURER_S1_AUTO_DISPATCH_ON_FULL 限制。
    """
    lock_handle = _try_acquire_s1_dispatch_lock()
    if lock_handle is None:
        current_app.logger.info('S1 自动发车已有其他 worker 执行，本轮跳过')
        return {'dispatched': 0, 'failed': 0, 'party_ids': [], 'lock_busy': True}
    try:
        return _dispatch_departed_expeditions_locked(manual=manual, match_queued=match_queued)
    finally:
        _release_s1_dispatch_lock(lock_handle)
def dispatch_queued_expeditions() -> dict:
    """定时发车先处理已成团小队；无待发队时消费现有匹配票。"""
    return dispatch_departed_expeditions(match_queued=True)
def force_dispatch_party(party_id: int) -> tuple[int | None, str | None]:
    """管理员强制出发：跳过 departure_time / 满员要求，直接发起探险。

    仍要求：小队 forming、至少 1 名成员、成员精力达标（由 run_s1_expedition 校验）。
    返回 (log_id, err)。
    """
    from plugins.adventurer_s1.services.adventurer_s1_party_service import is_expedition_dispatch_paused
    if is_expedition_dispatch_paused():
        return (None, '探险出发已暂停，请先恢复后再强制出发')
    party = AdventurerS1Party.query.session.get(AdventurerS1Party, party_id)
    if not party:
        return (None, '小队不存在')
    if party.status != 'forming':
        return (None, f'小队当前状态为 {party.status}，只有 forming 状态可强制出发')
    if not _load_s1_party_members_ordered(party_id):
        return (None, '小队没有成员，无法出发')
    return run_s1_expedition(party_id)
def progress_active_expeditions() -> dict:
    """按稳定 ID 公平轮转推进 exploring 小队，并限制单轮批量与墙钟耗时。"""
    global _s1_expedition_progress_cursor
    empty_result = {'progressed': 0, 'completed': 0, 'failed': 0}
    if not _S1_EXPEDITION_PROGRESS_JOB_LOCK.acquire(blocking=False):
        current_app.logger.info('S1 探险推进 tick 已在执行，本轮跳过')
        return empty_result
    batch_size = min(1000, max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_PROGRESS_BATCH_SIZE', 200), 200)))
    budget_seconds = min(240, max(1, _coerce_int(current_app.config.get('ADVENTURER_S1_EXPEDITION_PROGRESS_BUDGET_SECONDS', 45), 45)))
    started_at = time.monotonic()
    processed = 0
    progressed = 0
    completed = 0
    failed = 0
    budget_exhausted = False
    try:
        base_query = AdventurerS1Party.query.with_entities(AdventurerS1Party.id).filter_by(status='exploring')
        rows = base_query.filter(AdventurerS1Party.id > _s1_expedition_progress_cursor).order_by(AdventurerS1Party.id.asc()).limit(batch_size).all()
        if len(rows) < batch_size:
            rows.extend(base_query.filter(AdventurerS1Party.id <= _s1_expedition_progress_cursor).order_by(AdventurerS1Party.id.asc()).limit(batch_size - len(rows)).all())
        party_ids = [int(row[0]) for row in rows]
        db_manager.remove()
        for party_id in party_ids:
            if processed and time.monotonic() - started_at >= budget_seconds:
                budget_exhausted = True
                break
            processed += 1
            _s1_expedition_progress_cursor = party_id
            try:
                status, err = progress_s1_expedition(party_id)
                if err:
                    failed += 1
                    db_manager.rollback()
                    current_app.logger.warning('S1 推进失败 party=%s err=%s', party_id, err)
                elif status == 'completed':
                    completed += 1
                elif status == 'in_progress':
                    progressed += 1
            except Exception as exc:
                failed += 1
                db_manager.rollback()
                current_app.logger.exception('S1 推进异常 party=%s: %s', party_id, exc)
            finally:
                db_manager.remove()
        if budget_exhausted:
            current_app.logger.warning('S1 探险推进达到单轮耗时预算 processed=%s candidates=%s batch_size=%s budget_seconds=%s cursor=%s', processed, len(party_ids), batch_size, budget_seconds, _s1_expedition_progress_cursor)
        return {'progressed': progressed, 'completed': completed, 'failed': failed}
    finally:
        try:
            db_manager.remove()
        finally:
            _S1_EXPEDITION_PROGRESS_JOB_LOCK.release()
