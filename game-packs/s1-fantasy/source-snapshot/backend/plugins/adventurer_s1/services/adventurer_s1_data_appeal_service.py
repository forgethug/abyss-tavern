"""深渊酒馆 S1 玩家数据申诉：双 AI 分析、复核与自动落库。"""
from runtime_core.time import utc_now as _portable_utc_now
from plugins.adventurer_s1.services.s1_player_error_service import player_database_result
import copy
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from types import SimpleNamespace
from flask import current_app
from db_repository import db_manager
from db_repository.decorators import with_transaction
from utils.cache_manager import CacheManager
from utils.backend_boot import get_async_worker_boot_id
from utils.time_utils import parse_iso_date, to_utc_iso
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog, AdventurerS1DataAppealQuota, AdventurerS1CurrencyLog, AdventurerS1ExpeditionLog, AdventurerS1ExpeditionLogMember, AdventurerS1ItemLog, AdventurerS1Item, AdventurerS1MarketListing, AdventurerS1Trait, AdventurerS1Equipment, AdventurerS1WarehouseItem, AdventurerS1Skill
from plugins.adventurer_s1.s1_item_types import S1_CONSUMABLE_ITEM_TYPES
from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import NATURAL_RECOVERY_POLICIES, NON_NATURAL_RECOVERY_POLICIES, maybe_set_recovery_rest_baseline, sanitize_trait_recovery_metadata_for_appeal, trait_has_natural_dorm_recovery
from plugins.adventurer_s1.services.s1_ai_prompt_shared import S1_DATA_APPEAL_REJECTION_RESOLUTION_BASES, S1_DATA_APPEAL_RESOLUTION_BASES, order_s1_data_appeal_expedition_evidence
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_finite_number, normalize_ascii_integer, normalize_exact_boolean
from plugins.adventurer_s1.services.s1_asset_description_merge_service import rebase_s1_concurrent_public_description
from plugins.adventurer_s1.services.s1_asset_recovery_service import recovery_ai_fields
from plugins.adventurer_s1.services.s1_settlement_field_projection import project_s1_member_description_aliases
from plugins.adventurer_s1.services.adventurer_s1_data_appeal_destructive_contract import DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS, destructive_action_source_ids, destructive_action_source_kind, destructive_action_target_kind, project_data_appeal_destructive_actions, strip_uncontracted_trait_skill_deletes
from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import normalize_data_appeal_currency_adjustment
logger = logging.getLogger(__name__)
_NARRATIVE_CREATE_CLAIM_KINDS = {'create_items': 'permanent_item_acquisition', 'create_traits': 'stable_trait_formation', 'create_skills': 'explicit_permanent_learning'}
_APPEAL_AI_ZERO_BASED_EVENT_LIST_FIELDS = frozenset({'evidence_event_indexes', 'verified_evidence_event_indexes', 'recovery_evidence_event_indexes', 'required_event_indexes', 'main_event_indexes', 'evidence_events', 'negated_by_events'})
_APPEAL_TARGETS_CACHE_TTL_SECONDS = 45
_appeal_targets_cache = CacheManager(ttl_seconds=_APPEAL_TARGETS_CACHE_TTL_SECONDS, max_size=500)
_DATA_APPEAL_RECOVERY_LOCK = threading.Lock()
_TAVERN_RELIEF_RECOVERY_LOCK = threading.Lock()
_PLAYER_REVERT_RECOVERY_LOCK = threading.Lock()
_PLAYER_MIMO_RECOVERY_LOCK = threading.Lock()
_data_appeal_recovery_cursor = 0
_tavern_relief_recovery_cursor = 0
_player_revert_recovery_cursor = 0
_player_mimo_recovery_cursor = 0

def _positive_recovery_config_int(key: str, default: int, *, maximum: int) -> int:
    try:
        return min(maximum, max(1, int(current_app.config.get(key, default))))
    except (TypeError, ValueError):
        return default

def _load_id_ordered_recovery_scan(columns: tuple, *, cursor: int, scan_size: int) -> list:
    """按主键窗口有界扫描；调用方负责及时 remove 当前只读 Session。"""
    base_query = db_manager.query(*columns)
    if cursor <= 0:
        return list(reversed(base_query.order_by(AdventurerS1DataAppeal.id.desc()).limit(scan_size).all()))
    rows = base_query.filter(AdventurerS1DataAppeal.id > cursor).order_by(AdventurerS1DataAppeal.id.asc()).limit(scan_size).all()
    if len(rows) < scan_size:
        rows.extend(base_query.filter(AdventurerS1DataAppeal.id <= cursor).order_by(AdventurerS1DataAppeal.id.asc()).limit(scan_size - len(rows)).all())
    return rows

def _load_data_appeal_event_snapshots(appeal_ids: list[int] | set[int]) -> dict[int, SimpleNamespace]:
    """Load the tiny Socket projection, then release the read transaction."""
    normalized_ids = sorted({int(appeal_id) for appeal_id in appeal_ids})
    if not normalized_ids:
        return {}
    try:
        rows = db_manager.query(AdventurerS1DataAppeal.id, AdventurerS1DataAppeal.user_id, AdventurerS1DataAppeal.status, AdventurerS1DataAppeal.ai_status, AdventurerS1DataAppeal.player_review_status, AdventurerS1DataAppeal.revert_offer_status).filter(AdventurerS1DataAppeal.id.in_(normalized_ids)).limit(len(normalized_ids)).all()
        return {int(row[0]): SimpleNamespace(id=int(row[0]), user_id=int(row[1]), status=row[2], ai_status=row[3], player_review_status=row[4], revert_offer_status=row[5]) for row in rows}
    finally:
        db_manager.remove()

def invalidate_appeal_targets_cache(user_id: int | None=None) -> None:
    """申诉弹窗 targets 读路径缓存失效。"""
    if user_id is None:
        _appeal_targets_cache.clear()
    else:
        _appeal_targets_cache.delete(user_id)
_VALID_TARGET_TYPES = frozenset({'trait', 'equipment', 'warehouse', 'skill'})
_MAX_TARGETS = 10
_MAX_RARITY_REVIEW_TARGETS = 5
_MAX_NOTE_LEN = 200
_FULL_TEXT_APPEAL_CATEGORIES = ('other', 'rarity_review')
_APPEAL_QUOTA_POOL_OTHER = 'other'
_APPEAL_QUOTA_POOL_STANDARD = 'standard'
_FULL_TEXT_QUOTA_SOURCE_DAILY = 'daily'
_FULL_TEXT_QUOTA_SOURCE_BALANCE = 'balance'
_FULL_TEXT_BALANCE_ACTIVATION_SETTING = 'data_appeal_other_balance_started_on'
_VALID_APPEAL_QUOTA_POOLS = frozenset({_APPEAL_QUOTA_POOL_OTHER, _APPEAL_QUOTA_POOL_STANDARD})
_APPEAL_QUOTA_POOL_LABELS = {_APPEAL_QUOTA_POOL_OTHER: '全文申诉（其他问题/稀有度复核）', _APPEAL_QUOTA_POOL_STANDARD: '其余申诉'}
_CLIENT_REQUEST_ID_PATTERN = re.compile('^[A-Za-z0-9][A-Za-z0-9_-]{15,127}$')
_DATA_APPEAL_PRIVILEGED_PATCH_KEYS = ('create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'team_pool', 'unassigned_items', 'tavern_subsidy', 'reclaim_items', *DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS)
_EVENT_TEXT_PREVIEW_LEN = 120
_PLAYER_REQUEST_RESOLUTION_DETAILS = {'repair_supported': '该项已获得可执行结论。', 'partially_supported': '该项仅有部分修复完成，其余部分未获得可执行结论。', 'already_satisfied_same_case': '同一事项已有可核对的完成记录，无需重复修改。', 'not_established': '现有权威记录不足以证明该项应当修改。', 'contradicted': '现有权威记录与该项诉求不一致，因此未修改。', 'outside_authority': '该项不在本次数据申诉可执行的修复范围内，因此未修改。', 'lookup_pending': '该项仍需核对相关权威记录，当前未改动。', 'repair_pending': '该项依据尚未被否定，但本轮尚未形成可安全执行的修复，暂未改动。'}
_TRAIT_RECOVERY_PATCH_FIELDS = frozenset({'recovery_policy', 'recovery_stage', 'recovery_progress', 'recovery_required', 'recovery_hours', 'recovered_desc'})
_PATCH_UPDATE_FIELDS = {'trait': frozenset({'name', 'description', 'is_archived', *_TRAIT_RECOVERY_PATCH_FIELDS}), 'equipment': frozenset({'name', 'type', 'rarity', 'custom_desc', 'quantity', 'uses_left', 'damage_note', 'evolution_stage', 'recovery_minutes'}), 'warehouse': frozenset({'name', 'type', 'rarity', 'custom_desc', 'quantity', 'uses_left', 'recovery_minutes'}), 'skill': frozenset({'name', 'description', 'equipped', 'recovery_minutes'})}
_DATA_APPEAL_EXECUTION_RETRY_POLICY_VERSION = 1
_DATA_APPEAL_TERMINAL_OPERATION_ERRORS_KEY = '_backend_terminal_operation_errors'
_DATA_APPEAL_STALE_ASSET_TARGET_CODE = 'stale_asset_target'
_DATA_APPEAL_STALE_ASSET_DESCRIPTION_CODE = 'stale_asset_description'
_ASSET_RECOVERY_PATCH_META_FIELDS = {'equipment': frozenset({'trigger_recovery', 'recovery_evidence_event_indexes'}), 'warehouse': frozenset(), 'skill': frozenset({'trigger_recovery', 'recovery_evidence_event_indexes'})}
_MAX_RECOVERY_EVIDENCE_EVENT_INDEXES = 20
_TABLE_MODEL = {'trait': AdventurerS1Trait, 'equipment': AdventurerS1Equipment, 'warehouse': AdventurerS1WarehouseItem, 'skill': AdventurerS1Skill}
_VALID_RARITIES = frozenset({'common', 'rare', 'epic', 'legendary'})
_RARITY_RANK = {'common': 0, 'rare': 1, 'epic': 2, 'legendary': 3}
_VALID_CREATE_DESTINATIONS = frozenset({'equipment', 'warehouse'})
_APPEAL_INSTRUCTION_INJECTION_MARKERS = frozenset({'/sys', '\\sys', 'system prompt', 'ignore previous', 'role: system', 'developer message', '系统指令', '忽略所有', '隐藏其在信息面板', 'patch中没有有效操作', 'patch 中没有有效操作'})
_TRAIT_RECOVERY_EASE_RANK = {'trace': 0, 'light': 1, 'medium': 2, 'heavy': 3, 'none': 4}
_APPEAL_SAFE_ITEM_TYPE_UPDATES = frozenset({'weapon', 'armor', 'shield', 'accessory', 'material', 'special', 'quest', 'tool', 'document', 'key_item', 'misc', 'clue'})
_APPEAL_ITEM_TYPE_ALIASES = {'staff': 'weapon', 'wand': 'weapon', '法杖': 'weapon', '武器': 'weapon', '防具': 'armor', '护甲': 'armor', '盾': 'shield', '盾牌': 'shield', '饰品': 'accessory', '工具': 'tool'}
_MAX_CREATE_TRAITS = 3
_MAX_CREATE_SKILLS = 3
_RARITY_REVIEW_GUARD_ERROR = '稀有度复核只能修改装备/仓库物品稀有度'
_NARRATIVE_CURRENCY_CAP_DEFAULT = 150
_TAVERN_SUBSIDY_CAP_PER_PERSON_DEFAULT = 500
_TAVERN_RELIEF_DEFAULTS = {'enabled': True, 'cap_per_person': 600.0, 'cap_per_team': 3600.0, 'applicant_bonus_cap': 100.0}

def _normalize_appeal_patch_boolean(value: object) -> bool | None:
    normalized, _ = normalize_exact_boolean(value)
    return normalized if type(normalized) is bool else None

def _normalize_appeal_patch_integer(value: object, *, minimum: int | None=None, maximum: int | None=None) -> int | None:
    normalized, _ = normalize_ascii_integer(value, minimum=minimum, maximum=maximum)
    if type(normalized) is not int:
        return None
    if minimum is not None and normalized < minimum:
        return None
    if maximum is not None and normalized > maximum:
        return None
    return normalized

def _normalize_appeal_patch_number(value: object) -> int | float | None:
    normalized, _ = normalize_ascii_finite_number(value)
    if type(normalized) is int:
        return normalized
    if type(normalized) is float and math.isfinite(normalized):
        return normalized
    return None
_MAX_RECLAIM_ITEMS_DEFAULT = 3
_MAX_RECLAIM_QUANTITY_DEFAULT = 10
_APPEAL_PATCH_MISSING = object()

def _normalize_appeal_patch_text(value: object, *, default: str | None='', nullable: bool=False, required: bool=False, maximum_length: int | None=None) -> tuple[str | None, bool]:
    """Normalize an AI-authored text scalar without stringifying other types.

    Missing optional fields may use their documented backend default. Explicit
    ``None`` is accepted only for columns whose persistence contract is nullable.
    Overlong values are rejected instead of truncating identity text.
    """
    if value is _APPEAL_PATCH_MISSING:
        if required:
            return (None, False)
        return (default, True)
    if value is None:
        if nullable:
            return (None, True)
        return (None, False)
    if type(value) is not str:
        return (None, False)
    normalized = value.strip()
    if required and (not normalized):
        return (None, False)
    if maximum_length is not None and len(normalized) > maximum_length:
        return (None, False)
    return (normalized, True)

class AdventurerS1DataAppealService:

    @staticmethod
    def _compact_item_name_for_cosmetic_compare(name: str) -> str:
        """去掉冗余助词/标点后再比，用于判定「去「的」/统一间隔符」类纯清洗改名。"""
        compact = str(name or '').strip()
        for ch in ('的', '之', '·', '．', '.', '・', '-', '—', ' '):
            compact = compact.replace(ch, '')
        return compact

    @staticmethod
    def _is_cosmetic_item_name_cleanup(original_name, candidate_name) -> bool:
        old_full = str(original_name or '').strip()
        new_name = str(candidate_name or '').strip()
        if not old_full or not new_name or old_full == new_name:
            return False
        if AdventurerS1DataAppealService._compact_item_name_for_cosmetic_compare(old_full) == AdventurerS1DataAppealService._compact_item_name_for_cosmetic_compare(new_name):
            return True
        return old_full.replace('的', '') == new_name.replace('的', '')

    @staticmethod
    def _appeal_allowed_item_name_update(original_name, candidate_name) -> bool:
        """申诉 update 禁止图鉴物换名；纯冗余字/标点清洗（去「的」等）机械放行。"""
        old_full = str(original_name or '').strip()
        new_name = str(candidate_name or '').strip()
        if not new_name or new_name == old_full:
            return True
        return AdventurerS1DataAppealService._is_cosmetic_item_name_cleanup(old_full, new_name)

    @staticmethod
    def _build_patch_from_review_verdicts(patch_raw: dict, review_raw: dict) -> dict | None:
        """按新响应的完整 verdicts 从 raw patch 回填可执行子集。

        历史已落库响应的兼容由终态读取路径负责；当前 AI 响应缺 verdicts 时
        必须 fail closed，不能把整包 decision=allow 当作隐式授权。
        """
        if not isinstance(patch_raw, dict) or not isinstance(review_raw, dict):
            return None
        verdicts = review_raw.get('verdicts')
        if isinstance(verdicts, list) and verdicts:
            filtered, _ = AdventurerS1DataAppealService._filter_patch_by_verdicts(patch_raw, verdicts)
            if AdventurerS1DataAppealService._patch_has_operations(filtered):
                return filtered
            return None
        return None

    @staticmethod
    def _patch_normalization_error_from_summary(ai_summary: str | None) -> str | None:
        text = str(ai_summary or '')
        marker = 'patch 校验未通过：'
        if marker not in text:
            return None
        tail = text.split(marker, 1)[-1].strip()
        if tail.endswith('）'):
            tail = tail[:-1].strip()
        return tail or None

    @staticmethod
    def _appeal_has_executable_patch(normalized_patch: dict | None, supplement_patch: dict | None) -> bool:
        return AdventurerS1DataAppealService._patch_has_operations(normalized_patch) or AdventurerS1DataAppealService._patch_has_operations(supplement_patch)

    @staticmethod
    def _enforce_review_executable_contract(review_raw: dict, *, normalized_patch: dict | None, supplement_patch: dict | None, patch_err: str | None) -> dict:
        """二审语义 allow 但无可执行 patch 时回到有界复核，不伪造业务驳回。"""
        if not isinstance(review_raw, dict):
            return review_raw
        decision = str(review_raw.get('decision') or '').strip().lower()
        if decision != 'allow':
            return review_raw
        if AdventurerS1DataAppealService._appeal_has_executable_patch(normalized_patch, supplement_patch):
            return review_raw
        updated = dict(review_raw)
        updated['decision'] = 'pending'
        updated['player_reason'] = '该项依据尚未被否定，但当前修复尚未形成可安全执行的写入。本轮未对数据做改动，已保留为待处理。'
        norm_detail = (patch_err or '').strip() or '无可执行的数据修复操作'
        issues = list(updated.get('issues') or []) if isinstance(updated.get('issues'), list) else []
        issues.append({'severity': 'high', 'detail': 'executable_patch_pending_after_allow'})
        updated['issues'] = issues
        updated['_backend_resolution_state'] = 'pending'
        updated['_backend_pending_stage'] = 'review_ai'
        updated['_backend_execution_error'] = norm_detail[:500]
        prior_summary = str(updated.get('summary') or '').strip()
        updated['summary'] = f'{prior_summary}（机械护栏：allow 但无可执行 patch，保留待处理）' if prior_summary else '机械护栏：allow 但无可执行 patch，保留待处理'
        logger.warning('数据申诉二审 allow 但无可执行 patch，机械转为 pending: %s', norm_detail)
        return updated

    @staticmethod
    def _enforce_review_supplement_failure_contract(review_raw: dict, *, normalized_patch: dict | None, supplement_patch: dict | None, supplement_err: str | None, snapshot_before: dict | None=None, primary_raw_patch: dict | None=None) -> dict:
        """二审声称 supplement_patch 可补救但后端未接收时，玩家文案必须同步为未落库。"""
        if not isinstance(review_raw, dict):
            return review_raw
        raw_supplement = review_raw.get('supplement_patch')
        raw_had_operations = bool(review_raw.get('_backend_supplement_had_operations'))
        if not raw_had_operations and (not AdventurerS1DataAppealService._patch_has_operations(raw_supplement)):
            return review_raw
        if AdventurerS1DataAppealService._patch_has_operations(supplement_patch):
            return review_raw
        norm_detail = (supplement_err or '').strip() or '补充修复未通过后端校验'
        if norm_detail == 'patch 中没有有效操作':
            satisfied, msg = AdventurerS1DataAppealService._appeal_recovery_metadata_already_satisfied(snapshot_before, primary_raw_patch, raw_supplement if isinstance(raw_supplement, dict) else None)
            if satisfied and msg:
                updated = dict(review_raw)
                updated['decision'] = 'allow'
                updated['player_reason'] = msg
                updated['recovery_already_satisfied'] = True
                for key in ('_backend_resolution_state', '_backend_pending_stage', '_backend_execution_error'):
                    updated.pop(key, None)
                return updated
        updated = dict(review_raw)
        norm_detail = (supplement_err or '').strip() or '补充修复未通过后端校验'
        has_other_executable = AdventurerS1DataAppealService._patch_has_operations(normalized_patch)
        updated['decision'] = 'pending'
        updated['_backend_resolution_state'] = 'pending'
        updated['_backend_pending_stage'] = 'review_ai'
        updated['_backend_execution_error'] = norm_detail[:500]
        if has_other_executable:
            player_reason = '其它独立通过项仍可正常执行；本条补充修复当前未能安全落账，已保留待处理，不视为诉求被驳回。'
        else:
            player_reason = '该项依据尚未被否定，但补充修复当前未能安全落账。本轮未对数据做改动，已保留待处理。'
        updated['player_reason'] = player_reason
        issues = list(updated.get('issues') or []) if isinstance(updated.get('issues'), list) else []
        issues.append({'severity': 'high', 'detail': f'supplement_patch_dropped_by_backend: {norm_detail}'})
        updated['issues'] = issues
        prior_summary = str(updated.get('summary') or '').strip()
        suffix = f'（机械护栏：二审 supplement_patch 未通过后端校验，保留待处理：{norm_detail}）'
        updated['summary'] = f'{prior_summary}{suffix}' if prior_summary else suffix.strip('（）')
        logger.warning('数据申诉二审 supplement_patch 未通过后端校验，已改写玩家文案: %s', norm_detail)
        return updated

    @staticmethod
    def _collect_supplement_patch_name_update_ids(review_raw: dict | None) -> dict[str, set[int]]:
        """二审 supplement_patch 中显式 rename 的 ID（二审已主动写入，机械授权 normalize）。"""
        allowed = {'equipment': set(), 'warehouse': set()}
        if not isinstance(review_raw, dict):
            return allowed
        supp = review_raw.get('supplement_patch')
        if not isinstance(supp, dict):
            return allowed
        for section in ('equipment', 'warehouse'):
            sec = supp.get(section) if isinstance(supp.get(section), dict) else None
            if not sec:
                continue
            for upd in sec.get('update') or []:
                if not isinstance(upd, dict) or upd.get('id') is None or 'name' not in upd:
                    continue
                record_id = _normalize_appeal_patch_integer(upd['id'], minimum=1)
                if record_id is not None:
                    allowed[section].add(record_id)
        return allowed

    @staticmethod
    def _review_operation_manifest_by_id(review_raw: dict | None) -> dict[str, dict]:
        """Return the frozen v2 reviewer manifest keyed by exact operation ID."""
        if not isinstance(review_raw, dict) or review_raw.get('verdict_contract_version') != 2:
            return {}
        manifest = review_raw.get('_backend_operation_manifest')
        if not isinstance(manifest, list):
            return {}
        return {str(row.get('operation_id') or '').strip(): row for row in manifest if isinstance(row, dict) and str(row.get('operation_id') or '').strip()}

    @staticmethod
    def _collect_approved_destructive_action_ids(review_raw: dict | None) -> dict[str, set[int]]:
        """Resolve destructive authorization from v2 operation IDs or legacy IDs."""
        allowed = {section: set() for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS}
        if not isinstance(review_raw, dict):
            return allowed
        verdicts = review_raw.get('verdicts')
        if not isinstance(verdicts, list):
            return allowed
        operation_contract_v2 = review_raw.get('verdict_contract_version') == 2
        operation_by_id = AdventurerS1DataAppealService._review_operation_manifest_by_id(review_raw)
        effective_decisions: dict[str, str] = {}
        if operation_contract_v2:
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions
            effective_decisions = effective_data_appeal_operation_decisions(verdicts)
        for verdict in verdicts:
            if not isinstance(verdict, dict):
                continue
            operation_id = str(verdict.get('operation_id') or '').strip()
            operation = operation_by_id.get(operation_id) if operation_contract_v2 else None
            section = str(operation.get('section') if operation else verdict.get('section') or '').strip()
            raw_ids = [operation.get('target_id')] if operation_contract_v2 and operation else verdict.get('ids')
            decision = effective_decisions.get(operation_id) if operation_contract_v2 else str(verdict.get('decision') or '').strip().lower()
            if section not in allowed or decision != 'allow' or (not isinstance(raw_ids, list)) or (not raw_ids):
                continue
            if operation_contract_v2 and (not operation or operation.get('action') != 'action'):
                continue
            for raw_id in raw_ids:
                record_id = _normalize_appeal_patch_integer(raw_id, minimum=1)
                if record_id is not None:
                    allowed[section].add(record_id)
        return allowed

    @staticmethod
    def _collect_custom_desc_clear_ids(review_raw: dict | None) -> dict[str, set[int]]:
        """二审 clear_fields 或 supplement 显式 custom_desc='' 的 ID。"""
        allowed = {'equipment': set(), 'warehouse': set()}
        if not isinstance(review_raw, dict):
            return allowed
        if review_raw.get('verdict_contract_version') == 2:
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions, iter_data_appeal_clear_operations, iter_data_appeal_patch_operations
            decisions = effective_data_appeal_operation_decisions(review_raw.get('verdicts'))
            allowed_operation_ids = {operation_id for operation_id, decision in decisions.items() if decision == 'allow'}
            for operation in iter_data_appeal_clear_operations(review_raw.get('clear_fields')):
                if operation['operation_id'] in allowed_operation_ids and operation.get('section') in allowed and ('custom_desc' in (operation.get('fields') or [])) and (type(operation.get('target_id')) is int):
                    allowed[operation['section']].add(operation['target_id'])
            for source, patch in (('supplement', review_raw.get('supplement_patch')), ('override', review_raw.get('override_patch'))):
                for operation in iter_data_appeal_patch_operations(patch, source=source):
                    payload = operation.get('_payload') or {}
                    if operation['operation_id'] in allowed_operation_ids and operation.get('section') in allowed and (operation.get('action') == 'update') and ('custom_desc' in payload) and (not str(payload.get('custom_desc') or '').strip()) and (type(operation.get('target_id')) is int):
                        allowed[operation['section']].add(operation['target_id'])
            return allowed
        clear_fields = review_raw.get('clear_fields')
        if isinstance(clear_fields, dict):
            for section in ('equipment', 'warehouse'):
                rows = clear_fields.get(section)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict) or row.get('id') is None:
                        continue
                    fields = row.get('fields') if isinstance(row.get('fields'), list) else []
                    if 'custom_desc' not in fields:
                        continue
                    record_id = _normalize_appeal_patch_integer(row['id'], minimum=1)
                    if record_id is not None:
                        allowed[section].add(record_id)
        supp = review_raw.get('supplement_patch')
        if isinstance(supp, dict):
            for section in ('equipment', 'warehouse'):
                sec = supp.get(section) if isinstance(supp.get(section), dict) else None
                if not sec:
                    continue
                for upd in sec.get('update') or []:
                    if not isinstance(upd, dict) or upd.get('id') is None:
                        continue
                    if 'custom_desc' not in upd:
                        continue
                    if str(upd.get('custom_desc') or '').strip():
                        continue
                    record_id = _normalize_appeal_patch_integer(upd['id'], minimum=1)
                    if record_id is not None:
                        allowed[section].add(record_id)
        return allowed

    @staticmethod
    def _collect_objective_name_update_ids(review_raw: dict | None) -> dict[str, set[int]]:
        """二审结构化授权的实质改名 ID；玩家偏好不算客观依据。"""
        allowed = {'equipment': set(), 'warehouse': set()}
        if not isinstance(review_raw, dict):
            return allowed
        operation_by_id = AdventurerS1DataAppealService._review_operation_manifest_by_id(review_raw)
        operation_contract_v2 = review_raw.get('verdict_contract_version') == 2
        effective_decisions: dict[str, str] = {}
        if operation_contract_v2:
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions
            effective_decisions = effective_data_appeal_operation_decisions(review_raw.get('verdicts'))
        valid_basis = {'expedition_evidence', 'data_inconsistency', 'mechanical_cleanup', 'history_consistency', 'field_misalignment'}
        for verdict in review_raw.get('verdicts') or []:
            if not isinstance(verdict, dict):
                continue
            operation = operation_by_id.get(str(verdict.get('operation_id') or '').strip()) if operation_contract_v2 else None
            section = str(operation.get('section') if operation else verdict.get('section') or '').strip().lower()
            if section not in allowed:
                continue
            if operation_contract_v2 and (not operation or operation.get('action') != 'update' or 'name' not in (operation.get('fields') or [])):
                continue
            operation_id = str(verdict.get('operation_id') or '').strip()
            decision = effective_decisions.get(operation_id) if operation_contract_v2 else str(verdict.get('decision') or '').strip().lower()
            if decision != 'allow':
                continue
            risk_type = str(verdict.get('risk_type') or '').strip().lower()
            if risk_type != 'item_identity_name_change':
                continue
            basis = str(verdict.get('basis') or '').strip().lower()
            if basis not in valid_basis:
                continue
            if verdict.get('not_player_preference') is not True:
                continue
            evidence = str(verdict.get('objective_evidence') or verdict.get('evidence') or verdict.get('reason') or '').strip()
            if len(evidence) < 8:
                continue
            raw_ids = [operation.get('target_id')] if operation_contract_v2 and operation else verdict.get('ids') or []
            for rid in raw_ids:
                record_id = _normalize_appeal_patch_integer(rid, minimum=1)
                if record_id is not None:
                    allowed[section].add(record_id)
        if not operation_contract_v2:
            supplement_ids = AdventurerS1DataAppealService._collect_supplement_patch_name_update_ids(review_raw)
            for section in ('equipment', 'warehouse'):
                allowed[section] |= supplement_ids[section]
        return allowed

    @staticmethod
    def _collect_objective_name_update_ids_from_appeal(appeal: AdventurerS1DataAppeal | None) -> dict[str, set[int]]:
        if not appeal or not getattr(appeal, 'review_ai_raw_response', None):
            return {'equipment': set(), 'warehouse': set()}
        import json
        try:
            parsed = json.loads(appeal.review_ai_raw_response)
        except (TypeError, ValueError):
            return {'equipment': set(), 'warehouse': set()}
        return AdventurerS1DataAppealService._collect_objective_name_update_ids(parsed if isinstance(parsed, dict) else None)

    @staticmethod
    def _collect_objective_type_update_ids(review_raw: dict | None) -> dict[str, set[int]]:
        """二审按权威物品描述授权的消耗品类修正 ID。"""
        allowed = {'equipment': set(), 'warehouse': set()}
        if not isinstance(review_raw, dict):
            return allowed
        operation_by_id = AdventurerS1DataAppealService._review_operation_manifest_by_id(review_raw)
        operation_contract_v2 = review_raw.get('verdict_contract_version') == 2
        effective_decisions: dict[str, str] = {}
        if operation_contract_v2:
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions
            effective_decisions = effective_data_appeal_operation_decisions(review_raw.get('verdicts'))
        for verdict in review_raw.get('verdicts') or []:
            if not isinstance(verdict, dict):
                continue
            operation = operation_by_id.get(str(verdict.get('operation_id') or '').strip()) if operation_contract_v2 else None
            section = str(operation.get('section') if operation else verdict.get('section') or '').strip().lower()
            if section not in allowed:
                continue
            if operation_contract_v2 and (not operation or operation.get('action') != 'update' or 'type' not in (operation.get('fields') or [])):
                continue
            operation_id = str(verdict.get('operation_id') or '').strip()
            decision = effective_decisions.get(operation_id) if operation_contract_v2 else str(verdict.get('decision') or '').strip().lower()
            if decision != 'allow':
                continue
            if str(verdict.get('risk_type') or '').strip().lower() != 'item_consumption_type_correction':
                continue
            if str(verdict.get('basis') or '').strip().lower() != 'authoritative_item_description':
                continue
            if verdict.get('not_player_preference') is not True:
                continue
            evidence = str(verdict.get('objective_evidence') or '').strip()
            if len(evidence) < 8:
                continue
            raw_ids = [operation.get('target_id')] if operation_contract_v2 and operation else verdict.get('ids') or []
            for rid in raw_ids:
                record_id = _normalize_appeal_patch_integer(rid, minimum=1)
                if record_id is not None:
                    allowed[section].add(record_id)
        return allowed

    @staticmethod
    def _collect_objective_type_update_ids_from_appeal(appeal: AdventurerS1DataAppeal | None) -> dict[str, set[int]]:
        if not appeal or not getattr(appeal, 'review_ai_raw_response', None):
            return {'equipment': set(), 'warehouse': set()}
        import json
        try:
            parsed = json.loads(appeal.review_ai_raw_response)
        except (TypeError, ValueError):
            return {'equipment': set(), 'warehouse': set()}
        return AdventurerS1DataAppealService._collect_objective_type_update_ids(parsed if isinstance(parsed, dict) else None)

    @staticmethod
    def _review_authorizes_consumption_restore(appeal: AdventurerS1DataAppeal | None, *, item_name: str | None=None) -> bool:
        """已消失物恢复须由二审对该物品以权威描述明确授权。"""
        if not appeal or not getattr(appeal, 'review_ai_raw_response', None):
            return False
        import json
        try:
            parsed = json.loads(appeal.review_ai_raw_response)
        except (TypeError, ValueError):
            return False
        if not isinstance(parsed, dict):
            return False
        operation_by_id = AdventurerS1DataAppealService._review_operation_manifest_by_id(parsed)
        operation_contract_v2 = parsed.get('verdict_contract_version') == 2
        effective_decisions: dict[str, str] = {}
        if operation_contract_v2:
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions
            effective_decisions = effective_data_appeal_operation_decisions(parsed.get('verdicts'))
        expected_name = str(item_name or '').strip()
        for verdict in parsed.get('verdicts') or []:
            if not isinstance(verdict, dict):
                continue
            operation = operation_by_id.get(str(verdict.get('operation_id') or '').strip()) if operation_contract_v2 else None
            section = str(operation.get('section') if operation else verdict.get('section') or '').strip().lower()
            if section != 'create_items':
                continue
            if operation_contract_v2 and (not operation or operation.get('action') != 'create' or (expected_name and str(operation.get('target_name') or '').strip() != expected_name)):
                continue
            operation_id = str(verdict.get('operation_id') or '').strip()
            decision = effective_decisions.get(operation_id) if operation_contract_v2 else str(verdict.get('decision') or '').strip().lower()
            if decision != 'allow':
                continue
            if str(verdict.get('risk_type') or '').strip().lower() != 'item_consumption_type_correction':
                continue
            if str(verdict.get('basis') or '').strip().lower() != 'authoritative_item_description':
                continue
            if verdict.get('not_player_preference') is not True:
                continue
            if len(str(verdict.get('objective_evidence') or '').strip()) >= 8:
                return True
        return False

    @staticmethod
    def _collect_custom_desc_clear_ids_from_appeal(appeal: AdventurerS1DataAppeal | None) -> dict[str, set[int]]:
        if not appeal or not getattr(appeal, 'review_ai_raw_response', None):
            return {'equipment': set(), 'warehouse': set()}
        import json
        try:
            parsed = json.loads(appeal.review_ai_raw_response)
        except (TypeError, ValueError):
            return {'equipment': set(), 'warehouse': set()}
        return AdventurerS1DataAppealService._collect_custom_desc_clear_ids(parsed if isinstance(parsed, dict) else None)

    @staticmethod
    def _collect_catalog_bound_item_names(record_maps: dict, raw_patch: dict) -> set[str]:
        """批量解析 patch 涉及物品原名是否在图鉴中。"""
        names: set[str] = set()
        for section in ('equipment', 'warehouse'):
            for rec in (record_maps.get(section) or {}).values():
                full = str(rec.get('name') or '').strip()
                if full:
                    names.add(full)
            sec_in = raw_patch.get(section) if isinstance(raw_patch.get(section), dict) else {}
            for item in sec_in.get('update') or []:
                if not isinstance(item, dict) or 'id' not in item:
                    continue
                try:
                    rid = int(item['id'])
                except (TypeError, ValueError):
                    continue
                orig = (record_maps.get(section) or {}).get(rid) or {}
                full = str(orig.get('name') or '').strip()
                if full:
                    names.add(full)
        if not names:
            return set()
        from flask import has_app_context
        if not has_app_context():
            return set()
        rows = db_manager.query(AdventurerS1Item).filter(AdventurerS1Item.name.in_(names)).all()
        return {str(row.name).strip() for row in rows if row and row.name}

    @staticmethod
    def _load_catalog_desc_map(names: set[str] | list[str] | None) -> dict[str, str]:
        """批量加载图鉴通用描述，供申诉 AI 快照区分「custom_desc 空 = 回退图鉴」。"""
        cleaned = {str(n).strip() for n in names or [] if str(n).strip()}
        if not cleaned:
            return {}
        rows = db_manager.query(AdventurerS1Item).filter(AdventurerS1Item.name.in_(list(cleaned))).all()
        return {str(row.name).strip(): str(row.description or '').strip() for row in rows if row and row.name and str(row.description or '').strip()}

    @staticmethod
    def _strip_spurious_custom_desc_mutations(working_item: dict, original: dict, *, catalog_bound_names: set[str], catalog_desc_map: dict[str, str], allow_custom_desc_clear: bool=False) -> None:
        """剔除重复图鉴回填，并禁止误清空实例原文。"""
        if 'custom_desc' not in working_item:
            return
        name = str(original.get('name') or '').strip()
        old_desc = str(original.get('custom_desc') or '').strip()
        new_desc = str(working_item.get('custom_desc') or '').strip()
        catalog_desc = str(catalog_desc_map.get(name) or '').strip()
        if name in catalog_bound_names and catalog_desc and (not old_desc) and (new_desc == catalog_desc):
            working_item.pop('custom_desc', None)
            logger.info('数据申诉 normalize 剔除重复图鉴回填 custom_desc「%s」', name)
            return
        if old_desc and (not new_desc) and (old_desc != catalog_desc) and (not (allow_custom_desc_clear and catalog_desc)):
            working_item.pop('custom_desc', None)
            logger.info('数据申诉 normalize 拦截误清空 custom_desc「%s」', name)

    @staticmethod
    def _sanitize_item_identity_patch_fields(working_item: dict, original: dict, *, catalog_bound_names: set[str], allow_name_update: bool=False) -> None:
        """机械剔除危险身份变更；只放行安全 type 修正和纯清洗改名。"""
        old_full = str(original.get('name') or '').strip()
        if 'type' in working_item:
            old_type = str(original.get('type') or '').strip()
            raw_type = str(working_item.get('type') or '').strip().lower()
            new_type = _APPEAL_ITEM_TYPE_ALIASES.get(raw_type, raw_type)
            if not new_type or new_type == old_type:
                working_item.pop('type', None)
            else:
                working_item['type'] = new_type
        if 'name' not in working_item:
            return
        if not allow_name_update and (not AdventurerS1DataAppealService._appeal_allowed_item_name_update(old_full, working_item.get('name'))):
            working_item.pop('name', None)

    @staticmethod
    def _normalize_patch_section_ids(raw_patch: dict, allowed_ids: dict) -> dict:
        """AI 偶尔把 warehouse/equipment section 写错；id 唯一可定位时机械搬到正确 section。"""
        if not isinstance(raw_patch, dict):
            return raw_patch
        normalized = copy.deepcopy(raw_patch)
        sections = ('traits', 'equipment', 'warehouse', 'skills')

        def resolve_section(current: str, rid: int) -> str:
            if rid in allowed_ids.get(current, set()):
                return current
            matches = [section for section in sections if rid in allowed_ids.get(section, set())]
            return matches[0] if len(matches) == 1 else current
        for section in sections:
            section_in = normalized.get(section) if isinstance(normalized.get(section), dict) else None
            if section_in is None:
                continue
            for op_key in ('update', 'delete'):
                rows = section_in.get(op_key, _APPEAL_PATCH_MISSING)
                if rows is _APPEAL_PATCH_MISSING or not isinstance(rows, list):
                    continue
                kept = []
                for item in rows:
                    raw_id = item.get('id') if isinstance(item, dict) else item
                    rid = _normalize_appeal_patch_integer(raw_id, minimum=1)
                    if rid is None:
                        kept.append(item)
                        continue
                    if isinstance(item, dict):
                        item['id'] = rid
                    else:
                        item = rid
                    target_section = resolve_section(section, rid)
                    if target_section == section:
                        kept.append(item)
                        continue
                    target = normalized.setdefault(target_section, {'update': [], 'delete': []})
                    if not isinstance(target, dict):
                        target = {'update': [], 'delete': []}
                        normalized[target_section] = target
                    target.setdefault(op_key, []).append(item)
                section_in[op_key] = kept
        return normalized

    @staticmethod
    def _patch_has_substantive_item_name_update(raw_patch: dict, snapshot_before: dict) -> bool:
        """实质改名必须进二审；数量归位/纯清洗不算此处高风险。"""
        if not isinstance(raw_patch, dict) or not isinstance(snapshot_before, dict):
            return False
        original_by_section = {section: {int(r['id']): str(r.get('name') or '').strip() for r in snapshot_before.get(section) or [] if isinstance(r, dict) and r.get('id') is not None} for section in ('equipment', 'warehouse')}
        for section in ('equipment', 'warehouse'):
            sec = raw_patch.get(section) if isinstance(raw_patch.get(section), dict) else {}
            section_has_deletes = bool(sec.get('delete'))
            for item in sec.get('update') or []:
                if not isinstance(item, dict) or 'id' not in item or 'name' not in item:
                    continue
                try:
                    rid = int(item['id'])
                except (TypeError, ValueError):
                    continue
                old_name = original_by_section.get(section, {}).get(rid)
                if not old_name:
                    continue
                new_name = str(item.get('name') or '').strip()
                if not new_name or new_name == old_name:
                    continue
                if AdventurerS1DataAppealService._appeal_allowed_item_name_update(old_name, new_name):
                    continue
                if AdventurerS1DataAppealService._is_quantity_field_normalization_update(item, {'name': old_name, 'quantity': 1}, section_has_deletes=section_has_deletes):
                    continue
                return True
        return False

    @staticmethod
    def _patch_changes_item_type(raw_patch: dict, snapshot_before: dict) -> bool:
        """任何装备/仓库 type 变化都强制进入独立二审。"""
        if not isinstance(raw_patch, dict) or not isinstance(snapshot_before, dict):
            return False
        originals: dict[str, dict[int, str]] = {}
        for section in ('equipment', 'warehouse'):
            originals[section] = {}
            for row in snapshot_before.get(section) or []:
                if not isinstance(row, dict) or row.get('id') is None:
                    continue
                try:
                    originals[section][int(row['id'])] = str(row.get('type') or '').strip().lower()
                except (TypeError, ValueError):
                    continue
        for section in ('equipment', 'warehouse'):
            sec = raw_patch.get(section) if isinstance(raw_patch.get(section), dict) else {}
            for row in sec.get('update') or []:
                if not isinstance(row, dict) or row.get('id') is None or 'type' not in row:
                    continue
                try:
                    old_type = originals[section].get(int(row['id']))
                except (TypeError, ValueError):
                    continue
                new_type = str(row.get('type') or '').strip().lower()
                if old_type is not None and new_type and (new_type != old_type):
                    return True
        return False

    @staticmethod
    def _snapshot_item_quantity(quantity) -> int:
        try:
            return max(1, int(quantity or 1))
        except (TypeError, ValueError):
            return 1

    @staticmethod
    def _is_quantity_field_normalization_update(working_item: dict, original: dict, *, section_has_deletes: bool) -> bool:
        """Name/desc dedup quantity fixes are field normalization, not raw gains.

        Uses structural signals only (paired name/custom_desc/delete). Does NOT parse
        name/custom_desc text — 三者一致由陪审团 1 号输出 + 陪审团 2 号裁决。
        """
        if 'quantity' not in working_item:
            return False
        if section_has_deletes:
            return True
        if 'custom_desc' in working_item:
            return True
        if 'name' in working_item:
            new_name = str(working_item.get('name') or '').strip()
            old_name = str(original.get('name') or '').strip()
            if new_name != old_name:
                return True
        return False

    @staticmethod
    def _expedition_evidence_supports_item_quantity_update(original: dict, raw_update: dict, expedition_evidence: dict | None) -> bool:
        """探险 member_update 已记录本人获得该物，允许对已有堆做 quantity 归位（非 create_items）。"""
        if not isinstance(original, dict) or not isinstance(raw_update, dict):
            return False
        if not isinstance(expedition_evidence, dict):
            return False
        name = str(original.get('name') or '').strip()
        if not name:
            return False
        member_update = expedition_evidence.get('member_update') if isinstance(expedition_evidence.get('member_update'), dict) else {}
        names = AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('new_equipments'))
        if name not in names:
            return False
        raw_qty = _normalize_appeal_patch_integer(raw_update.get('quantity'), minimum=0)
        if raw_qty is None:
            return False
        old_qty = AdventurerS1DataAppealService._snapshot_item_quantity(original.get('quantity'))
        return raw_qty > old_qty

    @staticmethod
    def _restore_dropped_item_quantities_in_normalized(raw_patch: dict, normalized: dict, snapshot_before: dict, *, expedition_evidence: dict | None=None) -> None:
        """normalize 反刷守卫丢弃的合法 quantity 增量，从 raw patch 回填到 normalized。"""
        if not isinstance(raw_patch, dict) or not isinstance(normalized, dict):
            return
        for section in ('equipment', 'warehouse'):
            record_map = {int(row['id']): row for row in snapshot_before.get(section) or [] if isinstance(row, dict) and row.get('id') is not None}
            raw_sec = raw_patch.get(section) if isinstance(raw_patch.get(section), dict) else {}
            norm_sec = normalized.get(section)
            if not isinstance(norm_sec, dict):
                norm_sec = {'update': [], 'delete': []}
                normalized[section] = norm_sec
            updates = list(norm_sec.get('update') or [])
            updates_by_id: dict[int, dict] = {}
            for upd in updates:
                if not isinstance(upd, dict) or upd.get('id') is None:
                    continue
                try:
                    updates_by_id[int(upd['id'])] = upd
                except (TypeError, ValueError):
                    continue
            section_has_deletes = bool(raw_sec.get('delete'))
            for raw_upd in raw_sec.get('update') or []:
                if not isinstance(raw_upd, dict) or raw_upd.get('id') is None or 'quantity' not in raw_upd:
                    continue
                rid = _normalize_appeal_patch_integer(raw_upd['id'], minimum=1)
                raw_qty = _normalize_appeal_patch_integer(raw_upd['quantity'], minimum=0)
                if rid is None or raw_qty is None:
                    continue
                original = record_map.get(rid)
                if not original:
                    continue
                old_qty = AdventurerS1DataAppealService._snapshot_item_quantity(original.get('quantity'))
                if raw_qty <= old_qty:
                    continue
                norm_upd = updates_by_id.get(rid)
                identity_ok = AdventurerS1DataAppealService._is_quantity_field_normalization_update(raw_upd, original, section_has_deletes=section_has_deletes)
                evidence_ok = AdventurerS1DataAppealService._expedition_evidence_supports_item_quantity_update(original, raw_upd, expedition_evidence)
                if not identity_ok and (not evidence_ok):
                    continue
                if norm_upd is None:
                    norm_upd = {'id': rid}
                    updates_by_id[rid] = norm_upd
                elif 'quantity' in norm_upd and int(norm_upd.get('quantity') or 0) >= raw_qty:
                    continue
                norm_upd['quantity'] = raw_qty
                if identity_ok:
                    for field in ('name', 'custom_desc'):
                        if field in raw_upd and field not in norm_upd:
                            norm_upd[field] = raw_upd[field]
            norm_sec['update'] = list(updates_by_id.values())

    @staticmethod
    def _find_item_stack_siblings(record_maps: dict, item_name: str, *, preferred_section: str | None=None) -> list[tuple[str, dict]]:
        """按归一化名在 record_maps 中查找已有堆叠（equipment/warehouse）。"""
        from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService
        normalized_name = S1BugReclaimService._normalize_name(item_name)
        if not normalized_name:
            return []
        found: list[tuple[str, dict]] = []
        for section in ('equipment', 'warehouse'):
            for rec in (record_maps.get(section) or {}).values():
                if not isinstance(rec, dict):
                    continue
                if S1BugReclaimService._normalize_name(rec.get('name')) == normalized_name:
                    found.append((section, rec))
        if preferred_section in ('equipment', 'warehouse'):
            preferred = [row for row in found if row[0] == preferred_section]
            if preferred:
                return preferred
        return found

    @staticmethod
    def _merge_folded_quantity_update(normalized: dict, section: str, record_id: int, target_qty: int, recovery_minutes: int=0) -> None:
        sec = normalized.get(section)
        if not isinstance(sec, dict):
            sec = {'update': [], 'delete': []}
            normalized[section] = sec
        updates_by_id: dict[int, dict] = {}
        for upd in sec.get('update') or []:
            if not isinstance(upd, dict) or upd.get('id') is None:
                continue
            try:
                updates_by_id[int(upd['id'])] = upd
            except (TypeError, ValueError):
                continue
        merged = updates_by_id.get(record_id, {'id': record_id})
        if 'quantity' in merged:
            try:
                target_qty = max(int(target_qty), int(merged.get('quantity') or 0))
            except (TypeError, ValueError):
                pass
        merged['quantity'] = int(target_qty)
        if int(recovery_minutes or 0) > 0:
            merged['recovery_minutes'] = int(recovery_minutes)
        updates_by_id[record_id] = merged
        sec['update'] = list(updates_by_id.values())

    @staticmethod
    def _fold_create_items_to_stack_updates(normalized: dict, record_maps: dict, raw_patch: dict | None=None) -> tuple[dict, list[str]]:
        """结算漏发误写 create_items 时，机械 fold 为已有堆叠的 quantity update。"""
        if not isinstance(normalized, dict):
            return (normalized, [])
        from plugins.adventurer_s1.services.s1_appeal_lineage_guard import is_distinct_desc_upgrade
        kept_create: list[dict] = []
        warnings: list[str] = []
        for item in normalized.get('create_items') or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if not name:
                continue
            destination = str(item.get('destination') or 'warehouse').strip().lower()
            siblings = AdventurerS1DataAppealService._find_item_stack_siblings(record_maps, name, preferred_section=destination)
            sibling_records = [rec for _, rec in siblings]
            if not sibling_records:
                kept_create.append(item)
                continue
            if is_distinct_desc_upgrade(item, sibling_records):
                kept_create.append(item)
                continue
            section, target = siblings[0]
            try:
                target_id = int(target['id'])
            except (KeyError, TypeError, ValueError):
                kept_create.append(item)
                continue
            try:
                create_qty = max(1, int(item.get('quantity') or 1))
                old_qty = max(1, int(target.get('quantity') or 1))
            except (TypeError, ValueError):
                kept_create.append(item)
                continue
            raw_section = raw_patch.get(section) if isinstance(raw_patch, dict) and isinstance(raw_patch.get(section), dict) else {}
            has_quantity_update = any((isinstance(upd, dict) and str(upd.get('id')) == str(target_id) and ('quantity' in upd) for upd in raw_section.get('update') or []))
            if has_quantity_update:
                warnings.append(f'物品「{name}」create_items 已丢弃：同一 patch 已对原 {section} id={target_id} 写入 quantity update，禁止重复补差')
                continue
            target_qty = old_qty + create_qty
            AdventurerS1DataAppealService._merge_folded_quantity_update(normalized, section, target_id, target_qty, recovery_minutes=int(item.get('recovery_minutes') or 0))
            warnings.append(f'物品「{name}」create_items 已机械 fold 为 {section} update：当前 {old_qty} + 本次新增 {create_qty} = {target_qty}')
        out = dict(normalized)
        out['create_items'] = kept_create
        return (out, warnings)

    @staticmethod
    def _deleted_companion_stage_floor_for_create(normalized: dict, record_maps: dict, item_name: str) -> int:
        """同 patch 内 delete 后 create 同名 companion：继承被删实例的最高阶，防 0 阶替换。"""
        from plugins.adventurer_s1.s1_item_types import coerce_evolution_stage, companion_allows_duplicate_name_instances
        clean_name = str(item_name or '').strip()
        if not clean_name:
            return 0
        floor = 0
        sec = normalized.get('equipment') if isinstance(normalized.get('equipment'), dict) else {}
        for del_item in sec.get('delete') or []:
            if not isinstance(del_item, dict) or del_item.get('id') is None:
                continue
            try:
                rid = int(del_item['id'])
            except (TypeError, ValueError):
                continue
            rec = (record_maps.get('equipment') or {}).get(rid) or {}
            if not companion_allows_duplicate_name_instances(rec.get('type') or ''):
                continue
            if str(rec.get('name') or '').strip() != clean_name:
                continue
            floor = max(floor, coerce_evolution_stage(rec.get('evolution_stage')))
        return floor

    @staticmethod
    def _build_mechanical_quantity_supplement_patch(raw_patch: dict, snapshot_before: dict, issues: list[dict]) -> dict | None:
        """审计仍发现 quantity 未落库时，机械从 raw patch 提取 supplement（二审/自动 finalize 兜底）。"""
        if not isinstance(raw_patch, dict) or not issues:
            return None
        target_ids: dict[str, set[int]] = {'equipment': set(), 'warehouse': set()}
        for issue in issues:
            section = str(issue.get('section') or '').strip()
            if section not in target_ids:
                continue
            for rid in issue.get('item_ids') or []:
                try:
                    target_ids[section].add(int(rid))
                except (TypeError, ValueError):
                    continue
        if not any(target_ids.values()):
            return None
        supplement: dict = {'traits': {'update': [], 'delete': []}, 'equipment': {'update': [], 'delete': []}, 'warehouse': {'update': [], 'delete': []}, 'skills': {'update': [], 'delete': []}}
        for section in ('equipment', 'warehouse'):
            raw_sec = raw_patch.get(section) if isinstance(raw_patch.get(section), dict) else {}
            record_map = {int(row['id']): row for row in snapshot_before.get(section) or [] if isinstance(row, dict) and row.get('id') is not None}
            for raw_upd in raw_sec.get('update') or []:
                if not isinstance(raw_upd, dict) or raw_upd.get('id') is None or 'quantity' not in raw_upd:
                    continue
                rid = _normalize_appeal_patch_integer(raw_upd['id'], minimum=1)
                if rid is None:
                    continue
                if rid not in target_ids[section]:
                    continue
                original = record_map.get(rid)
                if not original:
                    continue
                raw_qty = _normalize_appeal_patch_integer(raw_upd['quantity'], minimum=0)
                if raw_qty is None:
                    continue
                old_qty = AdventurerS1DataAppealService._snapshot_item_quantity(original.get('quantity'))
                if raw_qty <= old_qty:
                    continue
                cleaned = {'id': rid, 'quantity': raw_qty}
                for field in ('name', 'custom_desc'):
                    if field not in raw_upd:
                        continue
                    new_val = str(raw_upd.get(field) or '').strip()
                    old_val = str(original.get(field) or '').strip()
                    if new_val and new_val != old_val:
                        cleaned[field] = raw_upd[field]
                supplement[section]['update'].append(cleaned)
        if not any((supplement[s].get('update') or [] for s in ('equipment', 'warehouse'))):
            return None
        return supplement

    @staticmethod
    def _merge_data_appeal_patches(base: dict | None, extra: dict | None, *, allow_field_overwrite: bool=False) -> dict:
        """浅合并两个 patch（同 id update 字段并集，delete/create 追加去重）。

        二审 supplement 常用于补缺字段；若它顺手带了空字符串/None，默认不覆盖一审已规范化的非空字段。
        明确纠错请走 override_patch / clear_fields，并以 allow_field_overwrite=True 合并。
        """
        merged = copy.deepcopy(base) if isinstance(base, dict) else {}
        if not isinstance(extra, dict):
            return merged

        def _merge_update_fields(prev: dict, upd: dict) -> dict:
            out = dict(prev)
            for k, v in upd.items():
                if k == 'id':
                    continue
                if not allow_field_overwrite and k in out and (out.get(k) not in (None, '')):
                    continue
                out[k] = v
            return out
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            base_sec = merged.get(section)
            if not isinstance(base_sec, dict):
                base_sec = {'update': [], 'delete': []}
                merged[section] = base_sec
            extra_sec = extra.get(section) if isinstance(extra.get(section), dict) else {}
            upd_map: dict[int, dict] = {}
            for update in base_sec.get('update') or []:
                if not isinstance(update, dict) or update.get('id') is None:
                    continue
                record_id = _normalize_appeal_patch_integer(update['id'], minimum=1)
                if record_id is None:
                    continue
                normalized_update = dict(update)
                normalized_update['id'] = record_id
                upd_map[record_id] = normalized_update
            for upd in extra_sec.get('update') or []:
                if not isinstance(upd, dict) or upd.get('id') is None:
                    continue
                rid = _normalize_appeal_patch_integer(upd['id'], minimum=1)
                if rid is None:
                    continue
                normalized_update = dict(upd)
                normalized_update['id'] = rid
                prev = upd_map.get(rid) or {'id': rid}
                upd_map[rid] = _merge_update_fields(prev, normalized_update)
            base_sec['update'] = list(upd_map.values())
            normalized_deletes: list[dict] = []
            del_ids: set[int] = set()
            for delete in base_sec.get('delete') or []:
                raw_id = delete.get('id') if isinstance(delete, dict) else delete
                record_id = _normalize_appeal_patch_integer(raw_id, minimum=1)
                if record_id is None or record_id in del_ids:
                    continue
                del_ids.add(record_id)
                normalized_deletes.append({'id': record_id})
            base_sec['delete'] = normalized_deletes
            for del_item in extra_sec.get('delete') or []:
                raw_id = del_item.get('id') if isinstance(del_item, dict) else del_item
                rid = _normalize_appeal_patch_integer(raw_id, minimum=1)
                if rid is None:
                    continue
                if rid not in del_ids:
                    base_sec.setdefault('delete', []).append({'id': rid})
                    del_ids.add(rid)
        for key in ('create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'reclaim_items', 'unassigned_items', *DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS):
            if extra.get(key):
                merged[key] = (merged.get(key) or []) + list(extra.get(key) or [])
        for key in ('team_pool', 'tavern_subsidy'):
            if extra.get(key) and (not merged.get(key)):
                merged[key] = extra[key]
        return merged

    @staticmethod
    def _record_patch_only(patch: dict | None) -> dict:
        """只保留 _apply_patch 负责的一次性落库操作，排除独立分发器字段。"""
        out = copy.deepcopy(patch) if isinstance(patch, dict) else {}
        out['team_pool'] = None
        out['tavern_subsidy'] = None
        out['reclaim_items'] = []
        return out

    @staticmethod
    def _build_clear_fields_patch(clear_fields: dict | None) -> dict | None:
        """把二审显式 clear_fields 转成普通 update patch；仅白名单记录字段可清空。"""
        if not isinstance(clear_fields, dict):
            return None
        clearable = {'traits': {'description', 'recovered_desc'}, 'equipment': {'custom_desc', 'damage_note'}, 'warehouse': {'custom_desc'}, 'skills': {'description'}}
        patch: dict = {}
        for section, allowed_fields in clearable.items():
            rows = clear_fields.get(section)
            if not isinstance(rows, list):
                continue
            updates = []
            for row in rows:
                if not isinstance(row, dict) or row.get('id') is None:
                    continue
                rid = _normalize_appeal_patch_integer(row['id'], minimum=1)
                if rid is None:
                    continue
                fields = row.get('fields') if isinstance(row.get('fields'), list) else []
                update = {'id': rid}
                for field in fields:
                    key = str(field or '').strip()
                    if key in allowed_fields:
                        update[key] = ''
                if len(update) > 1:
                    updates.append(update)
            if updates:
                patch[section] = {'update': updates, 'delete': []}
        return patch if AdventurerS1DataAppealService._patch_has_operations(patch) else None

    @staticmethod
    def _patch_has_record_updates_or_creates(patch: dict | None) -> bool:
        """patch 是否含 update/create（不含纯 delete），用于 apply 零 audit 守卫。"""
        if not isinstance(patch, dict):
            return False
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            sec = patch.get(section) if isinstance(patch.get(section), dict) else {}
            if sec.get('update'):
                return True
        return bool(patch.get('create_items') or patch.get('create_traits') or patch.get('create_skills') or patch.get('unassigned_items'))

    @staticmethod
    def _create_item_failure_can_be_isolated(patch: dict | None) -> bool:
        """create_items 失败是否可与同单其它操作安全隔离。

        只放行没有删除、装备/仓库改写或独立分发器的 patch。旧物删除、数量改写、
        追回和破坏性替换都可能与新物补发构成一个原子动作，必须继续整单回滚。
        """
        if not isinstance(patch, dict) or not patch.get('create_items'):
            return False
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            sec = patch.get(section) if isinstance(patch.get(section), dict) else {}
            if sec.get('delete'):
                return False
            if section in ('equipment', 'warehouse') and sec.get('update'):
                return False
        return not any((patch.get(section) for section in ('team_pool', 'tavern_subsidy', 'reclaim_items', 'unassigned_items', *DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS)))

    @staticmethod
    def _patch_is_trait_merge_consolidation(raw_patch: dict | None) -> bool:
        """create_traits + 同单 traits.delete → 玩家整理/合并，非探险补发。"""
        if not isinstance(raw_patch, dict):
            return False
        create_traits = raw_patch.get('create_traits')
        if not isinstance(create_traits, list) or not create_traits:
            return False
        if not any((isinstance(row, dict) and str(row.get('name') or '').strip() for row in create_traits)):
            return False
        return bool(AdventurerS1DataAppealService._patch_section_delete_ids(raw_patch, 'traits'))

    @staticmethod
    def _patch_is_skill_merge_consolidation(raw_patch: dict | None) -> bool:
        """create_skills + 同单 skills.delete → 技能合并整理，非探险补发。"""
        if not isinstance(raw_patch, dict):
            return False
        create_skills = raw_patch.get('create_skills')
        if not isinstance(create_skills, list) or not create_skills:
            return False
        if not any((isinstance(row, dict) and str(row.get('name') or '').strip() for row in create_skills)):
            return False
        return bool(AdventurerS1DataAppealService._patch_section_delete_ids(raw_patch, 'skills'))

    @staticmethod
    def _patch_section_delete_ids(patch: dict | None, section: str) -> set[int]:
        if not isinstance(patch, dict):
            return set()
        sec = patch.get(section) if isinstance(patch.get(section), dict) else {}
        ids: set[int] = set()
        for item in sec.get('delete') or []:
            raw_id = item.get('id') if isinstance(item, dict) else item
            rid = _normalize_appeal_patch_integer(raw_id, minimum=1)
            if rid is not None:
                ids.add(rid)
        return ids

    @staticmethod
    def _patch_has_deletes(patch: dict | None) -> bool:
        if not isinstance(patch, dict):
            return False
        return any((AdventurerS1DataAppealService._patch_section_delete_ids(patch, section) for section in ('traits', 'equipment', 'warehouse', 'skills')))

    @staticmethod
    def _section_has_item_delete_compensation(normalized: dict, section: str) -> bool:
        delete_ids = AdventurerS1DataAppealService._patch_section_delete_ids(normalized, section)
        if not delete_ids:
            return True
        sec = normalized.get(section) if isinstance(normalized.get(section), dict) else {}
        update_ids = {int(upd['id']) for upd in sec.get('update') or [] if isinstance(upd, dict) and upd.get('id') is not None}
        if update_ids - delete_ids:
            return True
        return bool(normalized.get('create_items'))

    @staticmethod
    def _trait_delete_is_duplicate_cleanup(delete_ids: set[int], trait_record_map: dict | None) -> bool:
        """多条特质 delete 且均为同名重复、删后仍至少保留一条 → 机械去重放行。"""
        if len(delete_ids) <= 1 or not trait_record_map:
            return False
        deleted_names: list[str] = []
        for rid in delete_ids:
            rec = trait_record_map.get(rid)
            if not isinstance(rec, dict):
                return False
            name = str(rec.get('name') or '').strip()
            if not name:
                return False
            deleted_names.append(name)
        if len(set(deleted_names)) != 1:
            return False
        target_name = deleted_names[0]
        same_name_count = sum((1 for rec in trait_record_map.values() if isinstance(rec, dict) and str(rec.get('name') or '').strip() == target_name))
        return same_name_count >= len(delete_ids) and same_name_count - len(delete_ids) >= 1

    @staticmethod
    def _traits_delete_compensation_ok(normalized: dict, trait_record_map: dict | None=None, record_maps: dict | None=None) -> bool:
        delete_ids = AdventurerS1DataAppealService._patch_section_delete_ids(normalized, 'traits')
        if not delete_ids:
            return True
        if len(delete_ids) == 1:
            return True
        if AdventurerS1DataAppealService._trait_delete_is_duplicate_cleanup(delete_ids, trait_record_map):
            return True
        if normalized.get('create_traits'):
            return True
        sec = normalized.get('traits') if isinstance(normalized.get('traits'), dict) else {}
        for upd in sec.get('update') or []:
            if not isinstance(upd, dict) or upd.get('id') is None:
                continue
            try:
                rid = int(upd['id'])
            except (TypeError, ValueError):
                continue
            if rid in delete_ids:
                continue
            if not AdventurerS1DataAppealService._trait_update_is_recovery_only(upd):
                return True
        return False

    @staticmethod
    def _try_merge_skill_on_name_collision(adventurer_id: int, source_id: int, source_record: AdventurerS1Skill, safe_upd: dict, allowed: frozenset, audit_cb, *, appeal_id: int | None=None) -> tuple[bool, str | None]:
        """rename 目标名已被同冒险者另一技能占用时：合并字段到已有条目并删除来源条目。"""
        if 'name' not in safe_upd:
            return (False, None)
        new_name = str(safe_upd.get('name') or '').strip()
        old_name = str(source_record.name or '').strip()
        if not new_name or new_name == old_name:
            return (False, None)
        conflict = db_manager.query(AdventurerS1Skill).filter(AdventurerS1Skill.adventurer_id == adventurer_id, AdventurerS1Skill.name == new_name, AdventurerS1Skill.id != source_id).with_for_update().first()
        if not conflict:
            return (False, None)
        merge_upd = dict(safe_upd)
        src_desc = str(source_record.description or '').strip()
        tgt_desc = str(conflict.description or '').strip()
        if src_desc and (not tgt_desc) and ('description' not in merge_upd):
            merge_upd['description'] = src_desc
        before_target = AdventurerS1DataAppealService._full_record_columns(conflict)
        before_source = AdventurerS1DataAppealService._full_record_columns(source_record)
        for k, v in merge_upd.items():
            if k in ('id', 'name') or k not in allowed:
                continue
            if k == 'equipped':
                v = _normalize_appeal_patch_boolean(v)
                if v is None:
                    continue
            elif k == 'description' and isinstance(v, str):
                v = v.strip()
            setattr(conflict, k, v)
        after_target = AdventurerS1DataAppealService._full_record_columns(conflict)
        changed = {k: after_target[k] for k in after_target if before_target.get(k) != after_target[k] and k not in ('id', 'adventurer_id', 'created_at', 'updated_at')}
        if changed:
            audit_cb('update', 'skill', int(conflict.id), before_target, changed)
        ok, del_err = db_manager.delete_instance(source_record)
        if not ok:
            return (True, del_err or f'删除重复技能 id={source_id} 失败')
        audit_cb('delete', 'skill', source_id, before_source, None, note=f"同名技能合并到 id={conflict.id}（appeal #{appeal_id or '-'}）")
        logger.info('数据申诉 apply 同名技能合并 source=%s -> target=%s name=%r appeal=%s', source_id, conflict.id, new_name, appeal_id)
        return (True, None)

    @staticmethod
    def _skills_delete_compensation_ok(normalized: dict) -> bool:
        delete_ids = AdventurerS1DataAppealService._patch_section_delete_ids(normalized, 'skills')
        if not delete_ids:
            return True
        if len(delete_ids) == 1:
            return True
        if normalized.get('create_skills'):
            return True
        sec = normalized.get('skills') if isinstance(normalized.get('skills'), dict) else {}
        update_ids = {int(upd['id']) for upd in sec.get('update') or [] if isinstance(upd, dict) and upd.get('id') is not None}
        return bool(update_ids - delete_ids)

    @staticmethod
    def _validate_delete_compensation(normalized: dict | None, *, trait_record_map: dict | None=None, record_maps: dict | None=None, adventurer_id: int | None=None) -> str | None:
        """apply 前二次校验：禁止只删不补的物品/多删不合并的特质技能。"""
        return None

    @staticmethod
    def _sanitize_uncompensated_deletes(normalized: dict, *, trait_record_map: dict | None=None, record_maps: dict | None=None) -> tuple[dict, list[str]]:
        """normalize 阶段剥离无补偿 delete，避免只删不补落库。"""
        return (copy.deepcopy(normalized), [])

    @staticmethod
    def audit_delete_compensation(*, raw_patch: dict | None, normalized_patch: dict | None) -> list[dict]:
        issues: list[dict] = []
        if not isinstance(raw_patch, dict):
            return issues
        norm = normalized_patch if isinstance(normalized_patch, dict) else {}
        labels = {'equipment': '装备', 'warehouse': '仓库', 'traits': '特质', 'skills': '技能'}
        for section, label in labels.items():
            raw_ids = AdventurerS1DataAppealService._patch_section_delete_ids(raw_patch, section)
            norm_ids = AdventurerS1DataAppealService._patch_section_delete_ids(norm, section)
            stripped = sorted(raw_ids - norm_ids)
            if not stripped:
                continue
            issues.append({'code': 'delete_uncompensated_stripped', 'section': section, 'record_ids': stripped, 'detail': f'{label}删除未通过补偿校验，已拦截 {len(stripped)} 条'})
        return issues

    @staticmethod
    def apply_delete_compensation_audit(raw: dict, issues: list[dict]) -> dict:
        if not issues:
            return raw
        result = dict(raw)
        result['core_request_satisfied'] = False
        warnings = list(result.get('warnings') or [])
        for issue in issues:
            warnings.append(f"delete_compensation_audit:{issue.get('code')}:{issue.get('detail')}")
        if warnings:
            result['warnings'] = warnings
        result['delete_compensation_audit'] = issues
        sections = {str(issue.get('section') or '') for issue in issues}
        if 'equipment' in sections or 'warehouse' in sections:
            msg = '部分物品删除未能执行：合并/去重须同单 update 保留条目或 create_items 合订后再删，请核对背包与装备栏。'
        elif 'traits' in sections:
            msg = '部分特质删除未能执行：多条合并须先 update 保留条目或 create_traits 后再 delete，请核对特质列表。'
        else:
            msg = '部分删除未能执行：须先写入保留条目或 create 补发后再 delete，请核对数据后必要时重提。'
        existing = AdventurerS1DataAppealService._normalize_player_reject_reason(result.get('player_reject_reason') or result.get('reject_reason_for_player'))
        if AdventurerS1DataAppealService._looks_like_success_reason(existing):
            result['player_reject_reason'] = msg
        elif msg not in existing:
            result['player_reject_reason'] = f'{existing} {msg}'.strip() if existing else msg
        return result

    @staticmethod
    def _expedition_evidence_consumed_item_names(evidence: dict | None) -> set[str]:
        """关联探险 member_update 中「记为已消耗」的装备名（结算可能未真正扣物）。"""
        if not isinstance(evidence, dict):
            return set()
        member_update = evidence.get('member_update') if isinstance(evidence.get('member_update'), dict) else {}
        return AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('consumed_equipments'))

    @staticmethod
    def _sanitize_phantom_expedition_deletes(normalized: dict, *, record_maps: dict, expedition_evidence: dict | None=None) -> tuple[dict, list[str]]:
        """禁止「member_update 记 consumed 但玩家仍持有 → 申诉 delete 对齐结算」的误删。

        当前持有优先于 member_update 净变更；member_update 未真正扣物时不得 delete 现存条目。
        若同单已有 merge 补偿（update 保留条 / create_items），仍走 delete 去重路径。
        """
        return (copy.deepcopy(normalized), [])

    @staticmethod
    def audit_phantom_expedition_deletes(*, raw_patch: dict | None, normalized_patch: dict | None, expedition_evidence: dict | None) -> list[dict]:
        if not isinstance(raw_patch, dict) or not expedition_evidence:
            return []
        consumed = AdventurerS1DataAppealService._expedition_evidence_consumed_item_names(expedition_evidence)
        if not consumed:
            return []
        norm = normalized_patch if isinstance(normalized_patch, dict) else {}
        issues: list[dict] = []
        for section in ('equipment', 'warehouse'):
            raw_ids = AdventurerS1DataAppealService._patch_section_delete_ids(raw_patch, section)
            norm_ids = AdventurerS1DataAppealService._patch_section_delete_ids(norm, section)
            stripped = sorted(raw_ids - norm_ids)
            if not stripped:
                continue
            issues.append({'code': 'phantom_expedition_consume_delete_blocked', 'section': section, 'record_ids': stripped, 'consumed_names': sorted(consumed), 'detail': f'关联探险结算记录该物已消耗，但申诉时您仍持有；已拦截 {len(stripped)} 条 delete（不得以结算记录删除现存物品）'})
        return issues

    @staticmethod
    def apply_phantom_expedition_delete_audit(raw: dict, issues: list[dict]) -> dict:
        if not issues:
            return raw
        result = dict(raw)
        result['core_request_satisfied'] = False
        warnings = list(result.get('warnings') or [])
        for issue in issues:
            warnings.append(f"phantom_expedition_delete:{issue.get('code')}:{issue.get('detail')}")
        if warnings:
            result['warnings'] = warnings
        result['phantom_expedition_delete_audit'] = issues
        msg = '部分物品删除未执行：探险结算虽记录为已消耗，但您当前仍持有该物品，不能以结算记录为由删除。若物品确实不应在，请说明是重复条目或合并去重。'
        existing = AdventurerS1DataAppealService._normalize_player_reject_reason(result.get('player_reject_reason') or result.get('reject_reason_for_player'))
        if AdventurerS1DataAppealService._looks_like_success_reason(existing):
            result['player_reject_reason'] = msg
        elif msg not in existing:
            result['player_reject_reason'] = f'{existing} {msg}'.strip() if existing else msg
        return result

    @staticmethod
    def _apply_patch_zero_audit_error() -> str:
        return '修复未能生效：目标记录已失效或相关修改未通过检查，未对数据做任何改动'

    @staticmethod
    def _patch_increases_item_quantity(patch: dict | None, snapshot: dict | None) -> bool:
        """equipment/warehouse update 是否试图提高 quantity（纯 quantity 字段比较）。"""
        if not isinstance(patch, dict) or not isinstance(snapshot, dict):
            return False
        for section in ('equipment', 'warehouse'):
            record_map = {int(rec['id']): rec for rec in snapshot.get(section) or [] if isinstance(rec, dict) and rec.get('id') is not None}
            sec = patch.get(section) if isinstance(patch.get(section), dict) else {}
            for upd in sec.get('update') or []:
                if not isinstance(upd, dict) or 'quantity' not in upd or 'id' not in upd:
                    continue
                rid = _normalize_appeal_patch_integer(upd['id'], minimum=1)
                new_qty = _normalize_appeal_patch_integer(upd['quantity'], minimum=0)
                if rid is None or new_qty is None:
                    continue
                original = record_map.get(rid) or {}
                old_qty = AdventurerS1DataAppealService._snapshot_item_quantity(original.get('quantity'))
                if new_qty > old_qty:
                    return True
        return False

    @staticmethod
    def is_auto_approve_enabled() -> bool:
        """申诉由双 AI 自动闭环；保留方法以兼容旧调用方。"""
        return True

    @staticmethod
    def _normalize_trait_recovery_updates(original: dict, item: dict) -> dict | None:
        """Validate trait recovery metadata updates for data appeals."""
        touched = {k for k in _TRAIT_RECOVERY_PATCH_FIELDS if k in item}
        if not touched:
            return {}
        meta_input = {'policy': item.get('recovery_policy', original.get('recovery_policy', 'none')), 'stage': item.get('recovery_stage', original.get('recovery_stage')), 'required': item.get('recovery_required', original.get('recovery_required')), 'recovered_desc': item.get('recovered_desc', original.get('recovered_desc')), 'recovery_hours': item.get('recovery_hours', original.get('recovery_hours'))}
        for field, meta_field in (('recovery_required', 'required'), ('recovery_hours', 'recovery_hours')):
            if field not in item or item.get(field) is None:
                continue
            normalized_integer = _normalize_appeal_patch_integer(item.get(field))
            if normalized_integer is None:
                return None
            meta_input[meta_field] = normalized_integer
        normalized = sanitize_trait_recovery_metadata_for_appeal(meta_input)
        updates: dict = {}
        metadata_touched = touched - {'recovery_progress'}
        if metadata_touched:
            updates.update({'recovery_policy': normalized['recovery_policy'], 'recovery_stage': normalized['recovery_stage'], 'recovery_required': normalized['recovery_required'], 'recovered_desc': normalized['recovered_desc']})
            if normalized.get('recovery_stage') == 'trace':
                updates['recovery_hours'] = normalized.get('recovery_hours')
                updates['recovery_progress'] = 0
            else:
                updates['recovery_hours'] = None
            if 'recovery_progress' not in item:
                updates['recovery_progress'] = 0
        if 'recovery_progress' in item:
            progress = _normalize_appeal_patch_integer(item['recovery_progress'], minimum=0)
            if progress is None:
                return None
            required = updates.get('recovery_required')
            if required is None:
                required = normalized['recovery_required'] or original.get('recovery_required')
            if required is not None and progress > int(required):
                return None
            original_progress = int(original.get('recovery_progress') or 0)
            updates['recovery_progress'] = progress
        return updates

    @staticmethod
    def _raw_patch_trait_recovery_target_ids(raw_patch: dict | None) -> set[int]:
        ids: set[int] = set()
        if not isinstance(raw_patch, dict):
            return ids
        for upd in (raw_patch.get('traits') or {}).get('update') or []:
            if not isinstance(upd, dict) or upd.get('id') is None:
                continue
            fields = {k for k in upd if k != 'id'}
            if not fields & _TRAIT_RECOVERY_PATCH_FIELDS:
                continue
            record_id = _normalize_appeal_patch_integer(upd['id'], minimum=1)
            if record_id is not None:
                ids.add(record_id)
        return ids

    @staticmethod
    def _raw_recovery_update_conflicts_with_snapshot(record: dict, upd: dict) -> bool:
        """raw patch 是否明确要求与快照不同的恢复策略（不应走零操作通过）。"""
        snap_policy = str(record.get('recovery_policy') or 'none').strip().lower()
        if 'recovery_policy' in upd:
            desired = str(upd.get('recovery_policy') or 'none').strip().lower()
            if desired != snap_policy:
                return True
        if 'recovery_required' in upd and upd.get('recovery_required') is not None:
            if _normalize_appeal_patch_integer(upd['recovery_required'], minimum=0) is None:
                return False
            if snap_policy in NON_NATURAL_RECOVERY_POLICIES:
                return True
        return False

    @staticmethod
    def _trait_record_recovery_configured(record: dict) -> bool:
        policy = str(record.get('recovery_policy') or 'none').strip().lower()
        stage = str(record.get('recovery_stage') or '').strip().lower()
        if policy in NON_NATURAL_RECOVERY_POLICIES:
            return stage == 'heavy'
        if policy in NATURAL_RECOVERY_POLICIES and stage in {'light', 'medium', 'trace'}:
            if stage == 'trace':
                return True
            try:
                return int(record.get('recovery_required') or 0) > 0
            except (TypeError, ValueError):
                return False
        return False

    @staticmethod
    def _appeal_recovery_metadata_already_satisfied(snapshot_before: dict | None, *raw_patches: dict | None) -> tuple[bool, str | None]:
        """patch 规范化后无操作，但 raw patch 仅尝试写 recovery 且快照已正确配置。"""
        record_map = {int(row['id']): row for row in (snapshot_before or {}).get('traits') or [] if row.get('id') is not None}
        update_map: dict[int, dict] = {}
        for raw_patch in raw_patches:
            if not isinstance(raw_patch, dict):
                continue
            for upd in (raw_patch.get('traits') or {}).get('update') or []:
                if not isinstance(upd, dict) or upd.get('id') is None:
                    continue
                fields = {k for k in upd if k != 'id'}
                if not fields & _TRAIT_RECOVERY_PATCH_FIELDS:
                    continue
                rid = _normalize_appeal_patch_integer(upd['id'], minimum=1)
                if rid is None:
                    continue
                update_map.setdefault(rid, {}).update(upd)
        if not update_map:
            return (False, None)
        for rid, upd in update_map.items():
            record = record_map.get(rid)
            if not record:
                return (False, None)
            if AdventurerS1DataAppealService._raw_recovery_update_conflicts_with_snapshot(record, upd):
                return (False, None)
            if not AdventurerS1DataAppealService._trait_record_recovery_configured(record):
                return (False, None)
        policies = {str(record_map[rid].get('recovery_policy') or 'none').strip().lower() for rid in update_map}
        if policies <= NON_NATURAL_RECOVERY_POLICIES:
            return (True, '所选特质恢复标签已正确标记为医疗/净化/特殊处置级（不走宿舍自然恢复周期），数据无需额外修改。')
        return (True, '所选特质恢复标签已正确配置，数据无需额外修改。')

    @staticmethod
    def _collect_appeal_raw_recovery_patches(appeal: AdventurerS1DataAppeal) -> list[dict]:
        patches: list[dict] = []
        raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
        primary = raw.get('patch') if isinstance(raw.get('patch'), dict) else {}
        if primary:
            patches.append(primary)
        review_text = getattr(appeal, 'review_ai_raw_response', None)
        if review_text:
            import json
            try:
                review_raw = json.loads(review_text)
            except (TypeError, ValueError):
                review_raw = None
            if isinstance(review_raw, dict):
                supplement = review_raw.get('supplement_patch')
                if isinstance(supplement, dict):
                    patches.append(supplement)
        return patches

    @staticmethod
    def _mark_appeal_recovery_already_satisfied(appeal: AdventurerS1DataAppeal, message: str) -> None:
        raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
        raw['player_reply_summary'] = message
        raw['core_request_satisfied'] = True
        raw['recovery_already_satisfied'] = True
        appeal.ai_raw_response = json_dumps_safe(raw)
        appeal.core_request_satisfied = True

    @staticmethod
    def _get_owner_adventurer(user_id: int, *, load_items: bool=False):
        q = db_manager.query(AdventurerS1).filter(AdventurerS1.owner_id == user_id, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES))
        if load_items:
            from sqlalchemy.orm import load_only, raiseload, selectinload
            q = q.options(load_only(AdventurerS1.id, AdventurerS1.name), raiseload('*'), selectinload(AdventurerS1.traits).raiseload('*'), selectinload(AdventurerS1.equipment_items).raiseload('*'), selectinload(AdventurerS1.warehouse_items).raiseload('*'), selectinload(AdventurerS1.skill_items).raiseload('*'))
        adv = q.first()
        if not adv:
            return (None, '您没有存活的绑定冒险者')
        return (adv, None)

    @staticmethod
    def _text_locked_item_names_from_instances(equipment_items, warehouse_items) -> set[str]:
        from plugins.adventurer_s1.services.s1_item_use_condition_service import catalog_immutable_item_names
        names: set[str] = set()
        for item in equipment_items:
            if item and item.name:
                names.add(str(item.name).strip())
        for item in warehouse_items:
            if item and item.name:
                names.add(str(item.name).strip())
        return catalog_immutable_item_names(names)

    @staticmethod
    def _text_locked_item_names(adventurer_id: int) -> set[str]:
        """持有实例 + 图鉴 use_condition 判定不可变物品名集合。"""
        from plugins.adventurer_s1.models import AdventurerS1Equipment, AdventurerS1WarehouseItem
        from plugins.adventurer_s1.services.s1_item_use_condition_service import catalog_immutable_item_names
        names: set[str] = set()
        for model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
            rows = db_manager.query(model).filter(model.adventurer_id == adventurer_id).with_entities(model.name).all()
            names.update((str(r[0]).strip() for r in rows if r and r[0]))
        return catalog_immutable_item_names(names)

    @staticmethod
    def _appeal_record_text_locked(record: dict, catalog_locked_names: set[str]) -> bool:
        if not record:
            return False
        if record.get('text_locked'):
            return True
        name = str(record.get('name') or '').strip()
        return bool(name and name in catalog_locked_names)

    @staticmethod
    def _collect_patch_item_names(raw_patch: dict, record_maps: dict) -> set[str]:
        names: set[str] = set()
        for section in ('equipment', 'warehouse'):
            sec_in = raw_patch.get(section) if isinstance(raw_patch.get(section), dict) else {}
            for item in sec_in.get('update') or []:
                if not isinstance(item, dict):
                    continue
                if item.get('name'):
                    names.add(str(item['name']).strip())
                try:
                    rid = int(item['id'])
                except (TypeError, ValueError, KeyError):
                    continue
                orig = (record_maps.get(section) or {}).get(rid) or {}
                if orig.get('name'):
                    names.add(str(orig['name']).strip())
        for item in raw_patch.get('create_items') or []:
            if isinstance(item, dict) and item.get('name'):
                names.add(str(item['name']).strip())
        return {n for n in names if n}

    @staticmethod
    def _log_text_locked_patch_skips(raw_patch: dict, record_maps: dict, catalog_locked_names: set[str], *, appeal_id: int | None=None) -> None:
        """记录被跳过的不可变物品修改（仅日志，不拒整单 patch）。"""
        section_labels = {'equipment': '随身装备', 'warehouse': '仓库物品'}
        for section in ('equipment', 'warehouse'):
            sec_in = raw_patch.get(section) if isinstance(raw_patch.get(section), dict) else {}
            label = section_labels[section]
            for item in sec_in.get('update') or []:
                if not isinstance(item, dict) or 'id' not in item:
                    continue
                try:
                    rid = int(item['id'])
                except (TypeError, ValueError):
                    continue
                original = (record_maps.get(section) or {}).get(rid) or {}
                if not AdventurerS1DataAppealService._appeal_record_text_locked(original, catalog_locked_names):
                    continue
                item_name = str(original.get('name') or '').strip() or f'id={rid}'
                mutating_keys = sorted((k for k in item.keys() if k != 'id'))
                if mutating_keys:
                    logger.warning('数据申诉 normalize 跳过不可变物品修改 %s「%s」fields=%s appeal=%s', label, item_name, mutating_keys, appeal_id)
                if 'name' in item:
                    new_name = str(item.get('name') or '').strip()
                    old_name = str(original.get('name') or '').strip()
                    if new_name != old_name:
                        logger.warning('数据申诉 normalize 跳过不可变物品改名 %s「%s」appeal=%s', label, item_name, appeal_id)
                if 'custom_desc' in item:
                    new_desc = str(item.get('custom_desc') or '').strip()
                    old_desc = str(original.get('custom_desc') or '').strip()
                    if new_desc != old_desc:
                        logger.warning('数据申诉 normalize 跳过不可变物品改描述 %s「%s」appeal=%s', label, item_name, appeal_id)

    @staticmethod
    def _serialize_record(record_type: str, record, *, text_locked_names: set[str] | None=None, catalog_desc_map: dict[str, str] | None=None, include_internal_recovery_state: bool=False) -> dict:
        if record_type == 'trait':
            return {'id': record.id, 'name': record.name, 'description': record.description, 'is_archived': bool(record.is_archived), 'recovery_policy': record.recovery_policy or 'none', 'recovery_stage': record.recovery_stage, 'recovery_progress': int(record.recovery_progress or 0), 'recovery_required': record.recovery_required, 'recovery_hours': record.recovery_hours, 'recovered_desc': record.recovered_desc, 'asset_dependencies': record.asset_dependencies}
        if record_type in ('equipment', 'warehouse'):
            recovery_ends_at = getattr(record, 'recovery_ends_at', None)
            payload = {'id': record.id, 'name': record.name, 'type': record.type or '', 'rarity': record.rarity or 'common', 'custom_desc': record.custom_desc, 'quantity': int(record.quantity or 1), 'uses_left': record.uses_left, 'damage_note': record.damage_note if record_type == 'equipment' else None, **recovery_ai_fields(record)}
            if include_internal_recovery_state:
                recovery_started_at = getattr(record, 'recovery_started_at', None)
                payload['recovery_started_at'] = to_utc_iso(recovery_started_at) if recovery_started_at else None
                payload['recovery_ends_at'] = to_utc_iso(recovery_ends_at) if recovery_ends_at else None
            if record_type == 'equipment':
                payload['evolution_stage'] = max(0, int(getattr(record, 'evolution_stage', 0) or 0))
            if text_locked_names and record.name in text_locked_names:
                payload['text_locked'] = True
            catalog_desc = str((catalog_desc_map or {}).get(str(record.name or '').strip()) or '').strip()
            if catalog_desc:
                payload['catalog_desc'] = catalog_desc
            return payload
        if record_type == 'skill':
            recovery_ends_at = getattr(record, 'recovery_ends_at', None)
            payload = {'id': record.id, 'name': record.name, 'description': record.description, 'equipped': bool(record.equipped), **recovery_ai_fields(record)}
            if include_internal_recovery_state:
                recovery_started_at = getattr(record, 'recovery_started_at', None)
                payload['recovery_started_at'] = to_utc_iso(recovery_started_at) if recovery_started_at else None
                payload['recovery_ends_at'] = to_utc_iso(recovery_ends_at) if recovery_ends_at else None
            return payload
        return {}

    @staticmethod
    def _full_record_columns(record) -> dict:
        """记录的全列 JSON-safe 快照，供审计 before/after 与还原重建使用。"""
        from decimal import Decimal
        out = {}
        for col in record.__table__.columns:
            val = getattr(record, col.name)
            if isinstance(val, datetime):
                val = val.isoformat()
            elif isinstance(val, Decimal):
                val = float(val)
            out[col.name] = val
        return out

    @staticmethod
    def _truncate_event_text(text: str, max_len: int=_EVENT_TEXT_PREVIEW_LEN) -> str:
        cleaned = (text or '').strip()
        if len(cleaned) <= max_len:
            return cleaned
        return cleaned[:max_len] + '…'

    @staticmethod
    def _get_latest_completed_expedition_log(adventurer_id: int):
        return db_manager.query(AdventurerS1ExpeditionLog).join(AdventurerS1ExpeditionLogMember, AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id).filter(AdventurerS1ExpeditionLogMember.adventurer_id == int(adventurer_id), AdventurerS1ExpeditionLog.status == 'completed').order_by(AdventurerS1ExpeditionLog.created_at.desc()).first()

    @staticmethod
    def _get_tavern_relief_owner(user_id: int):
        """酒馆补贴申请只读取绑定冒险者身份标量，避免加载角色全量关系。"""
        row = db_manager.query(AdventurerS1.id, AdventurerS1.name, AdventurerS1.owner_id, AdventurerS1.status).filter(AdventurerS1.owner_id == user_id, AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).first()
        if not row:
            return (None, '您没有存活的绑定冒险者')
        return (row, None)

    @staticmethod
    def _get_tavern_relief_log(adventurer_id: int, *, expedition_log_id: int | None=None, lock: bool=False):
        """只投影目标日志的 member_updates，不读取同列中的剧情 events。"""
        member_updates_expr = AdventurerS1ExpeditionLog.full_log['member_updates'].label('member_updates')
        special_dungeon_context_expr = AdventurerS1ExpeditionLog.full_log['special_dungeon_context'].label('special_dungeon_context')
        query = db_manager.query(AdventurerS1ExpeditionLog.id, AdventurerS1ExpeditionLog.status, AdventurerS1ExpeditionLog.target_dungeon, AdventurerS1ExpeditionLog.party_name, AdventurerS1ExpeditionLog.created_at, member_updates_expr, special_dungeon_context_expr).join(AdventurerS1ExpeditionLogMember, AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id).filter(AdventurerS1ExpeditionLogMember.adventurer_id == int(adventurer_id), AdventurerS1ExpeditionLog.status == 'completed').order_by(AdventurerS1ExpeditionLog.created_at.desc())
        if expedition_log_id is not None:
            query = query.filter(AdventurerS1ExpeditionLog.id == int(expedition_log_id))
        if lock:
            query = query.with_for_update(of=AdventurerS1ExpeditionLog)
        row = query.first()
        if not row:
            return None
        member_updates = row.member_updates if isinstance(row.member_updates, list) else []
        special_dungeon_context = row.special_dungeon_context if isinstance(row.special_dungeon_context, dict) else {}
        return SimpleNamespace(id=row.id, status=row.status, target_dungeon=row.target_dungeon, party_name=row.party_name, created_at=row.created_at, full_log={'member_updates': member_updates, 'special_dungeon_context': special_dungeon_context})

    @staticmethod
    def _tavern_relief_policy() -> dict:
        """读取酒馆补贴申请启用状态与后端金额硬上限。"""
        from flask import has_app_context
        config = current_app.config if has_app_context() else {}

        def _number(key: str, default_key: str) -> float:
            default = float(_TAVERN_RELIEF_DEFAULTS[default_key])
            try:
                return float(config.get(key, default))
            except (TypeError, ValueError):
                return default
        return {'enabled': bool(config.get('ADVENTURER_S1_TAVERN_RELIEF_ENABLED', _TAVERN_RELIEF_DEFAULTS['enabled'])), 'cap_per_person': max(0.0, _number('ADVENTURER_S1_TAVERN_RELIEF_CAP_PER_PERSON', 'cap_per_person')), 'cap_per_team': max(0.0, _number('ADVENTURER_S1_TAVERN_RELIEF_CAP_PER_TEAM', 'cap_per_team')), 'applicant_bonus_cap': max(0.0, _number('ADVENTURER_S1_TAVERN_RELIEF_APPLICANT_BONUS_CAP', 'applicant_bonus_cap'))}

    @staticmethod
    def _tavern_relief_list_count(value) -> int:
        return len(value) if isinstance(value, (list, dict)) else 0

    @staticmethod
    def _project_tavern_relief_member_updates(log) -> list[dict]:
        """仅投影补贴核定所需结算字段，不经过会丢弃装备损坏的玩家日志投影。"""
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        allowed_fields = ('id', 'gold_change', 'hp_change', 'max_hp', 'is_dead', 'new_class', 'new_equipments', 'new_skills', 'new_traits', 'consumed_equipments', 'removed_skills', 'removed_traits', 'equipment_damage_updates')
        return [{key: row[key] for key in allowed_fields if key in row} for row in full_log.get('member_updates') or [] if isinstance(row, dict)]

    @staticmethod
    def _build_tavern_relief_dungeon_profile(log) -> dict:
        """构造补贴申请 AI 所需的最小权威副本画像，不读取剧情正文。"""
        target_dungeon = str(getattr(log, 'target_dungeon', '') or '').strip()
        from plugins.adventurer_s1.services.adventurer_s1_config_service import format_s1_dungeon_level_desc, get_s1_dungeon_info, get_s1_special_dungeon
        special = get_s1_special_dungeon(target_dungeon)
        if special:
            profile = {'is_special': True, 'level_label': str(special.get('level_label') or '特殊副本'), 'special_reward_kind': str(special.get('reward_kind') or '')}
            full_log = getattr(log, 'full_log', None)
            raw_context = full_log.get('special_dungeon_context') if isinstance(full_log, dict) else None
            raw_directives = raw_context.get('directives') if isinstance(raw_context, dict) else None
            if isinstance(raw_directives, dict):
                granted_directives = {'new_class', 'advance_class', 'new_skill', 'remove_trait'}
                granted_count = 0
                for value in raw_directives.values():
                    directive = str(value or '').strip()
                    if directive in granted_directives:
                        granted_count += 1
                member_updates = full_log.get('member_updates') if isinstance(full_log, dict) else None
                member_count = len(member_updates) if isinstance(member_updates, list) else 0
                profile['opportunity_granted_count'] = granted_count
                profile['opportunity_not_granted_count'] = max(member_count - granted_count, 0)
            return profile
        dungeon_info = get_s1_dungeon_info(target_dungeon)
        if str(dungeon_info.get('name') or '') != target_dungeon:
            return {'is_special': False}
        return {'is_special': False, 'level_label': format_s1_dungeon_level_desc(target_dungeon), 'min_level': int(dungeon_info['min_level']), 'max_level': int(dungeon_info['max_level'])}

    @staticmethod
    def _build_tavern_relief_roster(member_updates: list) -> list:
        """补贴申请花名册只查身份标量，不加载冒险者关系或全量档案。"""
        id_to_gold: dict[int, float] = {}
        for update in member_updates:
            if not isinstance(update, dict):
                continue
            adventurer_id = _normalize_appeal_patch_integer(update.get('id'), minimum=1)
            if adventurer_id is None:
                continue
            gold_change = _normalize_appeal_patch_number(update.get('gold_change', 0))
            id_to_gold[adventurer_id] = float(gold_change or 0)
        if not id_to_gold:
            return []
        rows = db_manager.query(AdventurerS1.id, AdventurerS1.name, AdventurerS1.owner_id, AdventurerS1.status).filter(AdventurerS1.id.in_(list(id_to_gold))).all()
        excluded = set(AdventurerS1.TAVERN_EXCLUDED_STATUSES)
        return [{'adventurer_id': row.id, 'name': row.name, 'owner_id': row.owner_id, 'alive': row.status not in excluded, 'gold_change': round(id_to_gold.get(row.id, 0.0), 2)} for row in rows]

    @staticmethod
    def _calculate_tavern_relief(log: AdventurerS1ExpeditionLog | None, roster: list | None=None, *, applicant_id: int | None=None, target_per_person: float | None=None, applicant_bonus_target: float | None=None, decision: str='pending', reason: str | None=None) -> dict:
        """整理结构化结算，并分别计算团队与申请者个人目标的历史补差。"""
        policy = AdventurerS1DataAppealService._tavern_relief_policy()
        base = {'enabled': policy['enabled'], 'eligible': False, 'claimable': False, 'already_covered': False, 'decision': decision, 'target_per_person': 0.0, 'max_target_per_person': 0.0, 'payout_per_person': 0.0, 'recipient_count': 0, 'team_target_total': 0.0, 'team_grant_total': 0.0, 'applicant_bonus_target': 0.0, 'max_applicant_bonus_target': 0.0, 'applicant_bonus_grant': 0.0, 'applicant_bonus': None, 'grant_total': 0.0, 'recipients': [], 'reason': '', 'signals': {}, 'member_outcomes': []}
        if not policy['enabled']:
            return {**base, 'reason': '酒馆补贴申请当前未开放'}
        if not log or log.status != 'completed':
            return {**base, 'reason': '暂无可核定的已完成探险'}
        member_updates = AdventurerS1DataAppealService._project_tavern_relief_member_updates(log)
        if not member_updates:
            return {**base, 'reason': '本次探险缺少可核定的结构化结算'}
        roster = roster if roster is not None else AdventurerS1DataAppealService._build_tavern_relief_roster(member_updates)
        roster_by_id: dict[int, dict] = {}
        for row in roster or []:
            if not isinstance(row, dict):
                continue
            roster_id = _normalize_appeal_patch_integer(row.get('adventurer_id'), minimum=1)
            if roster_id is not None and roster_id not in roster_by_id:
                roster_by_id[roster_id] = row
        recipients = sorted([row for row in roster_by_id.values() if row.get('alive')], key=lambda row: _normalize_appeal_patch_integer(row.get('adventurer_id'), minimum=1) or 0)
        if not recipients:
            return {**base, 'reason': '本次探险当前没有可领取的存活参与者'}
        normalized_member_updates: list[tuple[int, dict]] = []
        seen_member_ids: set[int] = set()
        for update in member_updates:
            member_id = _normalize_appeal_patch_integer(update.get('id'), minimum=1)
            if member_id is None or member_id not in roster_by_id or member_id in seen_member_ids:
                continue
            seen_member_ids.add(member_id)
            normalized_member_updates.append((member_id, update))
        if not normalized_member_updates:
            return {**base, 'reason': '本次探险缺少可核定的结构化结算'}
        gold_total = 0.0
        max_hp_loss_ratio = 0.0
        deaths = 0
        cost_events = 0
        durable_reward_events = 0
        member_outcomes = []
        normalized_applicant_id = _normalize_appeal_patch_integer(applicant_id, minimum=1) or 0
        applicant_is_member = False
        for member_id, update in normalized_member_updates:
            normalized_gold_change = _normalize_appeal_patch_number(update.get('gold_change', 0))
            gold_change = round(normalized_gold_change or 0, 2)
            gold_total += gold_change
            normalized_is_dead = _normalize_appeal_patch_boolean(update.get('is_dead', False))
            is_dead = normalized_is_dead if normalized_is_dead is not None else False
            if is_dead:
                deaths += 1
            max_hp = _normalize_appeal_patch_number(update.get('max_hp', 0)) or 0
            hp_change = _normalize_appeal_patch_number(update.get('hp_change', 0)) or 0
            if max_hp > 0:
                max_hp_loss_ratio = max(max_hp_loss_ratio, max(-hp_change, 0.0) / max_hp)
            consumed = AdventurerS1DataAppealService._tavern_relief_list_count(update.get('consumed_equipments'))
            removed_skill_count = AdventurerS1DataAppealService._tavern_relief_list_count(update.get('removed_skills'))
            damaged = AdventurerS1DataAppealService._tavern_relief_list_count(update.get('equipment_damage_updates'))
            cost_events += consumed + removed_skill_count + damaged
            new_equipment_count = AdventurerS1DataAppealService._tavern_relief_list_count(update.get('new_equipments'))
            new_skill_count = AdventurerS1DataAppealService._tavern_relief_list_count(update.get('new_skills'))
            new_trait_count = AdventurerS1DataAppealService._tavern_relief_list_count(update.get('new_traits'))
            removed_trait_count = AdventurerS1DataAppealService._tavern_relief_list_count(update.get('removed_traits'))
            raw_new_class = update.get('new_class')
            new_class_count = 1 if isinstance(raw_new_class, str) and raw_new_class.strip() else 0
            durable_reward_events += new_class_count + new_equipment_count + new_skill_count + new_trait_count
            roster_row = roster_by_id.get(member_id) or {}
            is_applicant = member_id == normalized_applicant_id
            applicant_is_member = applicant_is_member or is_applicant
            member_outcomes.append({'adventurer_id': member_id, 'name': str(roster_row.get('name') or f'成员{member_id}'), 'is_applicant': is_applicant, 'alive': bool(roster_row.get('alive')), 'gold_change': gold_change, 'hp_change': round(hp_change, 2), 'max_hp': round(max_hp, 2), 'is_dead': is_dead, 'new_class_count': new_class_count, 'new_equipment_count': new_equipment_count, 'new_skill_count': new_skill_count, 'new_trait_count': new_trait_count, 'consumed_equipment_count': consumed, 'removed_skill_count': removed_skill_count, 'removed_trait_count': removed_trait_count, 'equipment_damage_count': damaged})
        average_gold = round(gold_total / len(normalized_member_updates), 2)
        max_target_per_person = math.floor(min(policy['cap_per_person'], policy['cap_per_team'] / len(recipients)) * 100) / 100
        max_target_per_person = None
        if max_target_per_person is not None and max_target_per_person <= 0:
            return {**base, 'recipient_count': len(recipients), 'reason': '酒馆补贴申请当前没有可用发放额度'}
        is_preview = target_per_person is None and applicant_bonus_target is None
        if is_preview:
            applied_target = None
            calculation_target = max_target_per_person or 0.0
        else:
            try:
                requested_target = float(target_per_person)
            except (TypeError, ValueError):
                requested_target = 0.0
            applied_target = math.floor((min(max(requested_target, 0.0), max_target_per_person) if max_target_per_person is not None else max(requested_target, 0.0)) * 100) / 100
            calculation_target = applied_target
        team_maxima = AdventurerS1DataAppealService._tavern_subsidy_team_maxima(log.id)
        individual_maxima = AdventurerS1DataAppealService._tavern_subsidy_individual_maxima(log.id)
        recipient_rows = []
        calculated_team_grant_total = 0.0
        for recipient in recipients:
            adventurer_id = int(recipient['adventurer_id'])
            prior_team = round(float(team_maxima.get(adventurer_id, 0.0)), 2)
            team_target = round(max(prior_team, calculation_target), 2)
            grant_delta = round(max(team_target - prior_team, 0.0), 2)
            calculated_team_grant_total += grant_delta
            recipient_rows.append({'adventurer_id': adventurer_id, 'name': str(recipient.get('name') or '某成员'), 'target_amount': applied_target or 0.0, 'team_target_amount': team_target, 'previously_covered': prior_team, 'grant_amount': grant_delta if applied_target is not None else 0.0})
        max_applicant_bonus_target = None if applicant_is_member and normalized_applicant_id > 0 else 0.0
        if is_preview:
            applied_applicant_bonus = None
            applicant_bonus_calculation_target = max_applicant_bonus_target or 0.0
        else:
            try:
                requested_applicant_bonus = float(applicant_bonus_target or 0)
            except (TypeError, ValueError):
                requested_applicant_bonus = 0.0
            applied_applicant_bonus = math.floor((min(max(requested_applicant_bonus, 0.0), max_applicant_bonus_target) if max_applicant_bonus_target is not None else max(requested_applicant_bonus, 0.0)) * 100) / 100
            applicant_bonus_calculation_target = applied_applicant_bonus
        prior_applicant_bonus = round(float(individual_maxima.get(normalized_applicant_id, 0.0)), 2)
        calculated_applicant_bonus_grant = round(max(applicant_bonus_calculation_target - prior_applicant_bonus, 0.0), 2)
        calculated_team_grant_total = round(calculated_team_grant_total, 2)
        calculated_grant_total = round(calculated_team_grant_total + calculated_applicant_bonus_grant, 2)
        preview_claimable = True
        team_grant_total = calculated_team_grant_total if applied_target is not None else 0.0
        applicant_bonus_grant = calculated_applicant_bonus_grant if applied_applicant_bonus is not None else 0.0
        grant_total = round(team_grant_total + applicant_bonus_grant, 2)
        applicant_row = roster_by_id.get(normalized_applicant_id) or {}
        default_reason = '可使用一次申诉额度交由 AI 根据结构化结算核定酒馆补贴' if preview_claimable else '本次探险可达到的补贴金额已由既有补贴覆盖'
        return {**base, 'eligible': True, 'claimable': preview_claimable if is_preview else False, 'already_covered': not preview_claimable if is_preview else decision == 'approve' and grant_total <= 0, 'decision': decision, 'target_per_person': applied_target or 0.0, 'max_target_per_person': max_target_per_person, 'payout_per_person': applied_target or 0.0, 'recipient_count': len(recipients), 'team_target_total': round((applied_target or 0.0) * len(recipients), 2), 'team_grant_total': team_grant_total, 'applicant_bonus_target': applied_applicant_bonus or 0.0, 'max_applicant_bonus_target': max_applicant_bonus_target, 'applicant_bonus_grant': applicant_bonus_grant, 'applicant_bonus': {'name': str(applicant_row.get('name') or '申请者'), 'target_amount': applied_applicant_bonus or 0.0, 'previously_covered': prior_applicant_bonus, 'grant_amount': applicant_bonus_grant} if normalized_applicant_id > 0 and applicant_is_member else None, 'grant_total': grant_total, 'recipients': recipient_rows, 'reason': str(reason or default_reason), 'signals': {'member_count': len(member_updates), 'alive_recipient_count': len(recipients), 'gold_total': round(gold_total, 2), 'average_gold_per_member': average_gold, 'max_hp_loss_ratio': round(max_hp_loss_ratio, 4), 'cost_events': cost_events, 'durable_reward_events': durable_reward_events, 'deaths': deaths, 'settlement_completed': getattr(log, 'status', None) == 'completed'}, 'member_outcomes': member_outcomes}

    @staticmethod
    def _public_tavern_relief_result(log, calculated: dict) -> dict:
        return {'enabled': bool(calculated.get('enabled')), 'eligible': bool(calculated.get('eligible')), 'claimable': bool(calculated.get('claimable')), 'already_covered': bool(calculated.get('already_covered')), 'decision': str(calculated.get('decision') or 'pending'), 'expedition': {'id': log.id, 'target_dungeon': log.target_dungeon, 'party_name': log.party_name, 'created_at': to_utc_iso(log.created_at) if log.created_at else None} if log else None, 'target_per_person': round(float(calculated.get('target_per_person') or 0), 2), 'max_target_per_person': round(float(calculated.get('max_target_per_person') or 0), 2), 'payout_per_person': round(float(calculated.get('payout_per_person') or 0), 2), 'recipient_count': int(calculated.get('recipient_count') or 0), 'team_target_total': round(float(calculated.get('team_target_total') or 0), 2), 'team_grant_total': round(float(calculated.get('team_grant_total') or 0), 2), 'applicant_bonus_target': round(float(calculated.get('applicant_bonus_target') or 0), 2), 'max_applicant_bonus_target': round(float(calculated.get('max_applicant_bonus_target') or 0), 2), 'applicant_bonus_grant': round(float(calculated.get('applicant_bonus_grant') or 0), 2), 'applicant_bonus': {'name': str(calculated['applicant_bonus'].get('name') or '申请者'), 'target_amount': round(float(calculated['applicant_bonus'].get('target_amount') or 0), 2), 'previously_covered': round(float(calculated['applicant_bonus'].get('previously_covered') or 0), 2), 'grant_amount': round(float(calculated['applicant_bonus'].get('grant_amount') or 0), 2)} if isinstance(calculated.get('applicant_bonus'), dict) else None, 'grant_total': round(float(calculated.get('grant_total') or 0), 2), 'recipients': [{'name': str(row.get('name') or '某成员'), 'target_amount': round(float(row.get('target_amount') or 0), 2), 'previously_covered': round(float(row.get('previously_covered') or 0), 2), 'grant_amount': round(float(row.get('grant_amount') or 0), 2)} for row in calculated.get('recipients') or [] if isinstance(row, dict)], 'message': str(calculated.get('reason') or '')}

    @staticmethod
    def _normalize_client_request_id(value: object) -> tuple[str | None, str | None]:
        if value is None:
            return (None, None)
        request_id = str(value).strip()
        if not _CLIENT_REQUEST_ID_PATTERN.fullmatch(request_id):
            return (None, '请求标识无效，请刷新页面后重试')
        return (request_id, None)

    @staticmethod
    def _get_tavern_relief_request(user_id: int, expedition_log_id: int, client_request_id: str | None):
        """按一次前端提交令牌查找已落库核定；不合并玩家主动发起的下一次申请。"""
        if not client_request_id:
            return None
        return db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.user_id == int(user_id), AdventurerS1DataAppeal.expedition_log_id == int(expedition_log_id), AdventurerS1DataAppeal.appeal_category == 'tavern_relief', AdventurerS1DataAppeal.status.in_(('approved', 'rejected')), AdventurerS1DataAppeal.expedition_evidence['client_request_id'].as_string() == client_request_id).order_by(AdventurerS1DataAppeal.id.asc()).first()

    @staticmethod
    def _reuse_tavern_relief_request_result(log, calculated: dict, appeal, *, user_id: int, request_reused: bool=False) -> dict:
        result = AdventurerS1DataAppealService._public_tavern_relief_result(log, calculated)
        evidence = appeal.expedition_evidence if isinstance(appeal.expedition_evidence, dict) else {}
        snapshot = evidence.get('tavern_relief_result')
        if isinstance(snapshot, dict):
            for key in ('decision', 'target_per_person', 'max_target_per_person', 'payout_per_person', 'recipient_count', 'team_target_total', 'team_grant_total', 'applicant_bonus_target', 'max_applicant_bonus_target', 'applicant_bonus_grant', 'applicant_bonus', 'grant_total', 'already_covered', 'recipients'):
                if key in snapshot:
                    result[key] = copy.deepcopy(snapshot[key])
        result.update({'appeal_id': appeal.id, 'request_status': 'done', 'request_reused': True, 'message': '这次提交已经完成核定，本次未新增申诉记录、额度消耗或到账。', 'quota': AdventurerS1DataAppealService.get_user_quota_player(user_id)})
        return result

    @staticmethod
    def _get_pending_tavern_relief_request(user_id: int, expedition_log_id: int, client_request_id: str | None=None):
        query = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.user_id == int(user_id), AdventurerS1DataAppeal.expedition_log_id == int(expedition_log_id), AdventurerS1DataAppeal.appeal_category == 'tavern_relief', AdventurerS1DataAppeal.status == 'processing')
        if client_request_id:
            query = query.filter(AdventurerS1DataAppeal.expedition_evidence['client_request_id'].as_string() == client_request_id)
        return query.order_by(AdventurerS1DataAppeal.id.asc()).first()

    @staticmethod
    def _get_tavern_relief_request_for_status(user_id: int, expedition_log_id: int | None=None, *, client_request_id: str | None=None, appeal_id: int | None=None):
        query = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.user_id == int(user_id), AdventurerS1DataAppeal.appeal_category == 'tavern_relief', AdventurerS1DataAppeal.status.in_(('processing', 'approved', 'rejected')))
        if appeal_id:
            return query.filter(AdventurerS1DataAppeal.id == int(appeal_id)).first()
        if expedition_log_id is not None:
            query = query.filter(AdventurerS1DataAppeal.expedition_log_id == int(expedition_log_id))
        if client_request_id:
            exact_request = query.filter(AdventurerS1DataAppeal.expedition_evidence['client_request_id'].as_string() == client_request_id).order_by(AdventurerS1DataAppeal.id.desc()).first()
            if exact_request:
                return exact_request
        return query.filter(AdventurerS1DataAppeal.status == 'processing').order_by(AdventurerS1DataAppeal.id.desc()).first()

    @staticmethod
    def _tavern_relief_request_status_result(log, calculated: dict, appeal, *, user_id: int, request_reused: bool=False) -> dict:
        if appeal.status in ('approved', 'rejected') and appeal.ai_status == 'done':
            result = AdventurerS1DataAppealService._reuse_tavern_relief_request_result(log, calculated, appeal, user_id=user_id)
            evidence = appeal.expedition_evidence if isinstance(appeal.expedition_evidence, dict) else {}
            snapshot = evidence.get('tavern_relief_result')
            if isinstance(snapshot, dict) and snapshot.get('message'):
                result['message'] = str(snapshot['message'])
            result['request_reused'] = False
            return result
        result = AdventurerS1DataAppealService._public_tavern_relief_result(log, calculated)
        failed = appeal.status == 'rejected' and appeal.ai_status == 'failed'
        result.update({'appeal_id': appeal.id, 'request_status': 'failed' if failed else 'processing', 'request_reused': request_reused, 'claimable': bool(result.get('claimable')) if failed else False, 'message': appeal.reject_reason or '酒馆核定暂时未完成，请重新提交申请' if failed else '申请已受理，酒馆正在后台核定；您可以关闭页面稍后再查看。', 'quota': AdventurerS1DataAppealService.get_user_quota_player(user_id)})
        return result

    @staticmethod
    def get_tavern_relief_preview(user_id: int, client_request_id: str | None=None, appeal_id: int | None=None) -> tuple:
        client_request_id, request_id_err = AdventurerS1DataAppealService._normalize_client_request_id(client_request_id)
        if request_id_err:
            return (None, request_id_err)
        request_row = None
        if appeal_id or client_request_id:
            request_row = AdventurerS1DataAppealService._get_tavern_relief_request_for_status(user_id, client_request_id=client_request_id, appeal_id=appeal_id)
        if request_row:
            request_log = AdventurerS1DataAppealService._get_tavern_relief_log(int(request_row.adventurer_id), expedition_log_id=int(request_row.expedition_log_id) if request_row.expedition_log_id else None)
            if request_row.status == 'rejected' and request_row.ai_status == 'failed':
                calculated = AdventurerS1DataAppealService._calculate_tavern_relief(request_log, applicant_id=int(request_row.adventurer_id))
            else:
                calculated = AdventurerS1DataAppealService._calculate_tavern_relief(None)
            return (AdventurerS1DataAppealService._tavern_relief_request_status_result(request_log, calculated, request_row, user_id=user_id), None)
        adv, err = AdventurerS1DataAppealService._get_tavern_relief_owner(user_id)
        if err:
            return (None, err)
        log = AdventurerS1DataAppealService._get_tavern_relief_log(adv.id)
        calculated = AdventurerS1DataAppealService._calculate_tavern_relief(log, applicant_id=adv.id)
        if log and (not request_row):
            request_row = AdventurerS1DataAppealService._get_tavern_relief_request_for_status(user_id, log.id)
            if request_row:
                return (AdventurerS1DataAppealService._tavern_relief_request_status_result(log, calculated, request_row, user_id=user_id), None)
        result = AdventurerS1DataAppealService._public_tavern_relief_result(log, calculated)
        result['quota'] = AdventurerS1DataAppealService.get_user_quota_player(user_id)
        return (result, None)

    @staticmethod
    def submit_tavern_relief(user_id: int, client_request_id: str | None=None) -> tuple:
        """持久化酒馆补贴申请并立即入队；HTTP 请求不等待外部 AI。"""
        client_request_id, request_id_err = AdventurerS1DataAppealService._normalize_client_request_id(client_request_id)
        if request_id_err:
            return (None, request_id_err)
        adv, err = AdventurerS1DataAppealService._get_tavern_relief_owner(user_id)
        if err:
            return (None, err)
        candidate_log = AdventurerS1DataAppealService._get_tavern_relief_log(adv.id, lock=True)
        if not candidate_log:
            calculated = AdventurerS1DataAppealService._calculate_tavern_relief(None)
            result = AdventurerS1DataAppealService._public_tavern_relief_result(None, calculated)
            result['quota'] = AdventurerS1DataAppealService.get_user_quota_player(user_id)
            db_manager.rollback()
            return (result, None)
        calculated = AdventurerS1DataAppealService._calculate_tavern_relief(candidate_log, applicant_id=adv.id)
        existing_request = AdventurerS1DataAppealService._get_tavern_relief_request(user_id, candidate_log.id, client_request_id)
        if existing_request:
            result = AdventurerS1DataAppealService._reuse_tavern_relief_request_result(candidate_log, calculated, existing_request, user_id=user_id)
            db_manager.rollback()
            return (result, None)
        pending_request = AdventurerS1DataAppealService._get_pending_tavern_relief_request(user_id, candidate_log.id, client_request_id) or AdventurerS1DataAppealService._get_pending_tavern_relief_request(user_id, candidate_log.id)
        if pending_request:
            result = AdventurerS1DataAppealService._tavern_relief_request_status_result(candidate_log, calculated, pending_request, user_id=user_id, request_reused=True)
            db_manager.rollback()
            return (result, None)
        if not calculated.get('claimable'):
            result = AdventurerS1DataAppealService._public_tavern_relief_result(candidate_log, calculated)
            result['quota'] = AdventurerS1DataAppealService.get_user_quota_player(user_id)
            db_manager.rollback()
            return (result, None)
        quota = AdventurerS1DataAppealService._compute_quota_state(user_id, _APPEAL_QUOTA_POOL_STANDARD)
        if quota['used'] >= quota['limit']:
            db_manager.rollback()
            return (None, AdventurerS1DataAppealService._quota_limit_error(quota))
        evidence = {'expedition_log_id': int(candidate_log.id), 'target_dungeon': candidate_log.target_dungeon, 'party_name': candidate_log.party_name, 'dungeon_profile': AdventurerS1DataAppealService._build_tavern_relief_dungeon_profile(candidate_log), **({'client_request_id': client_request_id} if client_request_id else {})}
        appeal = AdventurerS1DataAppeal(quota_source='unrestricted', user_id=user_id, adventurer_id=int(adv.id), player_note='酒馆补贴申请', appeal_category='tavern_relief', selected_targets=[], snapshot_before={}, expedition_evidence={**(evidence or {})}, expedition_log_id=int(candidate_log.id), ai_status='pending', status='processing')
        ok, add_err = player_database_result(db_manager.add(appeal))
        if not ok:
            db_manager.rollback()
            return (None, add_err)
        ok, commit_err = player_database_result(db_manager.commit())
        if not ok:
            return (None, commit_err)
        from services.async_task_service import AsyncTaskService
        payload = {'appeal_id': int(appeal.id), 'invoke_reason': 'initial'}
        if not AsyncTaskService.submit_task('ai_s1_tavern_relief', payload):
            logger.warning('酒馆补贴 AI 首次入队失败 appeal=%s，等待 recovery', appeal.id)
        result = AdventurerS1DataAppealService._tavern_relief_request_status_result(candidate_log, calculated, appeal, user_id=user_id)
        invalidate_appeal_targets_cache(user_id)
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        return (result, None)

    @staticmethod
    def claim_tavern_relief(user_id: int, client_request_id: str | None=None, *, request_appeal_id: int | None=None, claim_token: str | None=None) -> tuple:
        """一次思考模式 AI 核定酒馆补贴；后端执行上限、历史补差和事务落库。"""
        request_appeal = None
        if request_appeal_id:
            request_appeal = AdventurerS1DataAppealService._load_claimed_appeal(int(request_appeal_id), str(claim_token or ''))
            if not request_appeal or request_appeal.appeal_category != 'tavern_relief' or int(request_appeal.user_id) != int(user_id):
                return (None, '酒馆补贴申请处理权已变化')
            stored_evidence = request_appeal.expedition_evidence if isinstance(request_appeal.expedition_evidence, dict) else {}
            client_request_id = client_request_id or stored_evidence.get('client_request_id')
        client_request_id, request_id_err = AdventurerS1DataAppealService._normalize_client_request_id(client_request_id)
        if request_id_err:
            return (None, request_id_err)
        adv, err = AdventurerS1DataAppealService._get_tavern_relief_owner(user_id)
        if err:
            return (None, err)
        candidate_log = AdventurerS1DataAppealService._get_tavern_relief_log(adv.id, expedition_log_id=int(request_appeal.expedition_log_id) if request_appeal and request_appeal.expedition_log_id else None)
        if not candidate_log:
            calculated = AdventurerS1DataAppealService._calculate_tavern_relief(None)
            result = AdventurerS1DataAppealService._public_tavern_relief_result(None, calculated)
            result['quota'] = AdventurerS1DataAppealService.get_user_quota_player(user_id)
            return (result, None)
        calculated = AdventurerS1DataAppealService._calculate_tavern_relief(candidate_log, applicant_id=adv.id)
        public_result = AdventurerS1DataAppealService._public_tavern_relief_result(candidate_log, calculated)
        existing_request = AdventurerS1DataAppealService._get_tavern_relief_request(user_id, candidate_log.id, client_request_id)
        if existing_request:
            result = AdventurerS1DataAppealService._reuse_tavern_relief_request_result(candidate_log, calculated, existing_request, user_id=user_id)
            db_manager.rollback()
            return (result, None)
        if not calculated.get('claimable'):
            public_result['quota'] = AdventurerS1DataAppealService.get_user_quota_player(user_id)
            db_manager.rollback()
            return (public_result, None)
        quota = AdventurerS1DataAppealService._compute_quota_state(user_id, _APPEAL_QUOTA_POOL_STANDARD)
        reserved_quota = 1 if request_appeal else 0
        if max(0, quota['used'] - reserved_quota) >= quota['limit']:
            db_manager.rollback()
            return (None, AdventurerS1DataAppealService._quota_limit_error(quota))
        from plugins.adventurer_s1.services.s1_tavern_relief_ai_service import call_tavern_relief_ai
        applicant_id = int(adv.id)
        applicant_name = str(adv.name or '您的冒险者')
        expedition_log_id = int(candidate_log.id)
        claimed_request_id = int(request_appeal.id) if request_appeal else None
        ai_context = {'applicant': {'adventurer_id': applicant_id, 'adventurer_name': applicant_name}, 'applicant_request': '请酒馆根据本次探险的结构化结算核定补贴申请', 'expedition': {'id': expedition_log_id, 'target_dungeon': candidate_log.target_dungeon, 'party_name': candidate_log.party_name}, 'dungeon_profile': AdventurerS1DataAppealService._build_tavern_relief_dungeon_profile(candidate_log), 'limits': {'ordinary_appeal_cap_per_person': AdventurerS1DataAppealService._tavern_subsidy_cap_per_person(), 'preferred_uplift_factor': round(float(calculated['max_target_per_person'] or 0) / max(AdventurerS1DataAppealService._tavern_subsidy_cap_per_person(), 0.01), 4), 'max_target_per_person': calculated['max_target_per_person'], 'max_team_total': AdventurerS1DataAppealService._tavern_relief_policy()['cap_per_team'], 'max_applicant_bonus_target': calculated['max_applicant_bonus_target']}, 'settlement_summary': calculated.get('signals'), 'member_outcomes': calculated.get('member_outcomes')}
        ai_context['limits'] = {'max_target_per_person': None, 'max_applicant_bonus_target': calculated['max_applicant_bonus_target']}
        db_manager.rollback()
        ai_result = call_tavern_relief_ai(ai_context, expedition_log_id=expedition_log_id, adventurer_id=applicant_id, appeal_id=claimed_request_id, max_target_per_person=calculated['max_target_per_person'], max_applicant_bonus_target=calculated['max_applicant_bonus_target'])
        if not ai_result:
            db_manager.rollback()
            return (None, '酒馆补贴申请核定未完成，请稍后重试；本次不消耗申诉额度')
        locked_log = AdventurerS1DataAppealService._get_tavern_relief_log(applicant_id, expedition_log_id=expedition_log_id, lock=True)
        if not locked_log:
            db_manager.rollback()
            return (None, '最近一次探险状态已变化，请重新核定')
        current_roster = AdventurerS1DataAppealService._build_tavern_relief_roster(AdventurerS1DataAppealService._project_tavern_relief_member_updates(locked_log))
        locked_applicant = next((row for row in current_roster if int(row.get('adventurer_id') or 0) == applicant_id), None)
        if not locked_applicant or int(locked_applicant.get('owner_id') or 0) != int(user_id) or (not locked_applicant.get('alive')):
            db_manager.rollback()
            return (None, '申请者归属或存活状态已变化，请重新核定')
        calculated = AdventurerS1DataAppealService._calculate_tavern_relief(locked_log, roster=current_roster, applicant_id=applicant_id)
        public_result = AdventurerS1DataAppealService._public_tavern_relief_result(locked_log, calculated)
        existing_request = AdventurerS1DataAppealService._get_tavern_relief_request(user_id, locked_log.id, client_request_id)
        if existing_request:
            result = AdventurerS1DataAppealService._reuse_tavern_relief_request_result(locked_log, calculated, existing_request, user_id=user_id)
            db_manager.rollback()
            return (result, None)
        if not calculated.get('eligible'):
            public_result['quota'] = AdventurerS1DataAppealService.get_user_quota_player(user_id)
            db_manager.rollback()
            return (public_result, None)
        quota = AdventurerS1DataAppealService._compute_quota_state(user_id, _APPEAL_QUOTA_POOL_STANDARD)
        if max(0, quota['used'] - reserved_quota) >= quota['limit']:
            db_manager.rollback()
            return (None, AdventurerS1DataAppealService._quota_limit_error(quota))
        decision = ai_result['decision']
        target_per_person = ai_result['target_amount_per_person'] if decision == 'approve' else 0.0
        applicant_bonus_target = ai_result['applicant_bonus_target'] if decision == 'approve' else 0.0
        calculated = AdventurerS1DataAppealService._calculate_tavern_relief(locked_log, roster=current_roster, applicant_id=applicant_id, target_per_person=target_per_person, applicant_bonus_target=applicant_bonus_target, decision=decision, reason=ai_result['player_reply'])
        public_result = AdventurerS1DataAppealService._public_tavern_relief_result(locked_log, calculated)
        now = _portable_utc_now()
        result_snapshot = {'decision': public_result['decision'], 'target_per_person': public_result['target_per_person'], 'max_target_per_person': public_result['max_target_per_person'], 'payout_per_person': public_result['payout_per_person'], 'recipient_count': public_result['recipient_count'], 'team_target_total': public_result['team_target_total'], 'team_grant_total': public_result['team_grant_total'], 'applicant_bonus_target': public_result['applicant_bonus_target'], 'max_applicant_bonus_target': public_result['max_applicant_bonus_target'], 'applicant_bonus_grant': public_result['applicant_bonus_grant'], 'applicant_bonus': public_result['applicant_bonus'], 'grant_total': public_result['grant_total'], 'already_covered': public_result['already_covered'], 'recipients': public_result['recipients'], 'message': public_result['message']}
        team_subsidy = {'scope': 'team', 'currency_code': 'gold', 'reason': f"酒馆补贴申请核定每人目标 {public_result['payout_per_person']} 金币", 'allocations': [{'adventurer_id': int(row['adventurer_id']), 'amount': round(float(row['team_target_amount']), 2)} for row in calculated.get('recipients') or [] if float(row.get('team_target_amount') or 0) > 0]}
        applicant_subsidy = {'scope': 'individual', 'currency_code': 'gold', 'reason': f"酒馆补贴申请者额外补贴目标 {public_result['applicant_bonus_target']} 金币", 'total_amount': public_result['applicant_bonus_target']}
        subsidies = [team_subsidy, applicant_subsidy]
        approved = decision == 'approve'
        evidence = {'expedition_log_id': locked_log.id, 'target_dungeon': locked_log.target_dungeon, 'party_name': locked_log.party_name, 'dungeon_profile': AdventurerS1DataAppealService._build_tavern_relief_dungeon_profile(locked_log), 'tavern_relief_result': result_snapshot, **({'client_request_id': client_request_id} if client_request_id else {})}
        if claimed_request_id:
            appeal = AdventurerS1DataAppealService._load_claimed_appeal(claimed_request_id, str(claim_token or ''), for_update=True)
            if not appeal:
                db_manager.rollback()
                return (None, '酒馆补贴申请处理权已变化')
            appeal.expedition_evidence = evidence
            appeal.ai_status = 'done'
            appeal.ai_raw_response = ai_result.get('raw_response')
            appeal.ai_summary = ai_result['summary']
            appeal.proposed_patch = {'tavern_subsidy': subsidies} if approved else {}
            appeal.review_ai_status = 'done'
            appeal.review_ai_summary = '本次申请只执行一轮专用 AI 核定，不进入普通申诉二审'
            appeal.status = 'approved' if approved else 'rejected'
            appeal.core_request_satisfied = approved
            appeal.reject_reason = ai_result['player_reply'] if not approved else None
            appeal.reviewed_at = now
            appeal.applied_at = now if approved else None
            AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)
        else:
            appeal = AdventurerS1DataAppeal(quota_source='unrestricted', user_id=user_id, adventurer_id=applicant_id, player_note='酒馆补贴申请', appeal_category='tavern_relief', selected_targets=[], snapshot_before={}, expedition_evidence={**(evidence or {})}, expedition_log_id=locked_log.id, ai_status='done', ai_raw_response=ai_result.get('raw_response'), ai_summary=ai_result['summary'], proposed_patch={'tavern_subsidy': subsidies} if approved else {}, review_ai_status='done', review_ai_summary='本次申请只执行一轮专用 AI 核定，不进入普通申诉二审', status='approved' if approved else 'rejected', core_request_satisfied=approved, reject_reason=ai_result['player_reply'] if not approved else None, reviewed_at=now, applied_at=now if approved else None)
            ok, add_err = player_database_result(db_manager.add(appeal))
            if not ok:
                db_manager.rollback()
                return (None, add_err)
        ok, flush_err = player_database_result(db_manager.flush())
        if not ok:
            db_manager.rollback()
            return (None, flush_err)
        if approved:
            for index, subsidy in enumerate(subsidies):
                applied, apply_err = AdventurerS1DataAppealService._apply_tavern_subsidy(appeal, subsidy)
                if apply_err or not applied:
                    db_manager.rollback()
                    return (None, apply_err or '酒馆补贴申请发放失败')
                if index < len(subsidies) - 1:
                    ok, flush_err = player_database_result(db_manager.flush())
                    if not ok:
                        db_manager.rollback()
                        return (None, flush_err)
        ok, commit_err = player_database_result(db_manager.commit())
        if not ok:
            return (None, commit_err)
        next_calculated = AdventurerS1DataAppealService._calculate_tavern_relief(locked_log, applicant_id=applicant_id)
        public_result.update({'claimable': bool(next_calculated.get('claimable')), 'already_covered': bool(public_result.get('already_covered') or next_calculated.get('already_covered')), 'appeal_id': appeal.id, 'quota': AdventurerS1DataAppealService.get_user_quota_player(user_id)})
        invalidate_appeal_targets_cache(user_id)
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        return (public_result, None)

    @staticmethod
    def _finalize_tavern_relief_failure(appeal_id: int, claim_token: str, reason: str) -> None:
        """只允许当前租约 owner 记录失败并按有界预算重试。"""
        with db_manager.transaction():
            appeal = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.id == int(appeal_id), AdventurerS1DataAppeal.status == 'processing', AdventurerS1DataAppeal.ai_claim_token == claim_token).with_for_update().populate_existing().first()
            if not appeal:
                return
            appeal.ai_status = 'failed'
            appeal.ai_summary = reason[:500]
            appeal.review_ai_status = 'failed'
            appeal.review_ai_summary = '本次核定未完成，未形成业务结论'
            AdventurerS1DataAppealService._mark_appeal_resolution_pending(appeal, '本次核定未完成，申请已保留等待重新处理；未据此驳回您的请求。', stage='primary_ai')
            AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)

    @staticmethod
    def process_tavern_relief(appeal_id: int, *, invoke_reason: str='unknown', requeue_cause: str | None=None, restart_recovered: bool=False) -> None:
        """异步 worker：持租约执行酒馆补贴 AI，并原子发布核定与发放。"""
        from plugins.adventurer_s1.services.s1_token_usage_service import reset_appeal_invoke_reason, set_appeal_invoke_reason
        claim_token = AdventurerS1DataAppealService._try_claim_appeal_ai(appeal_id, restart_recovered=restart_recovered)
        if not claim_token:
            logger.info('酒馆补贴 AI 跳过 appeal=%s：未取得数据库租约 invoke_reason=%s', appeal_id, invoke_reason)
            return
        appeal = AdventurerS1DataAppealService._load_claimed_appeal(appeal_id, claim_token)
        if not appeal or appeal.appeal_category != 'tavern_relief':
            AdventurerS1DataAppealService._release_appeal_ai_claim(appeal_id, claim_token)
            return
        evidence = appeal.expedition_evidence if isinstance(appeal.expedition_evidence, dict) else {}
        client_request_id = evidence.get('client_request_id')
        reason_token = set_appeal_invoke_reason(invoke_reason)
        heartbeat = AdventurerS1DataAppealService._start_appeal_ai_heartbeat(appeal_id, claim_token)
        try:
            result, err = AdventurerS1DataAppealService.claim_tavern_relief(int(appeal.user_id), client_request_id=client_request_id, request_appeal_id=int(appeal.id), claim_token=claim_token)
            still_owned = AdventurerS1DataAppealService._load_claimed_appeal(appeal_id, claim_token)
            if still_owned:
                failure_reason = err or (result or {}).get('message') or '酒馆补贴申请核定未完成，请稍后重新提交'
                AdventurerS1DataAppealService._finalize_tavern_relief_failure(appeal_id, claim_token, str(failure_reason))
        except Exception:
            logger.exception('酒馆补贴异步任务异常 appeal=%s requeue_cause=%s', appeal_id, requeue_cause or '')
        finally:
            db_manager.rollback()
            AdventurerS1DataAppealService._stop_appeal_ai_heartbeat(heartbeat)
            AdventurerS1DataAppealService._release_appeal_ai_claim(appeal_id, claim_token)
            reset_appeal_invoke_reason(reason_token)

    @staticmethod
    def _serialize_latest_expedition_for_selection(log: AdventurerS1ExpeditionLog | None) -> dict | None:
        if not log:
            return None
        from plugins.adventurer_s1.services.adventurer_s1_log_service import build_public_s1_full_log
        from utils.time_utils import to_utc_iso
        public_full_log = build_public_s1_full_log(log.full_log if isinstance(log.full_log, dict) else {})
        events = public_full_log.get('events') or []
        serialized_events = []
        for idx, event in enumerate(events):
            if not isinstance(event, str):
                continue
            text = event.strip()
            if not text:
                continue
            serialized_events.append({'index': idx, 'text': text, 'preview': AdventurerS1DataAppealService._truncate_event_text(text)})
        if not serialized_events:
            return None
        return {'log_id': log.id, 'target_dungeon': log.target_dungeon, 'party_name': log.party_name, 'status': log.status, 'created_at': to_utc_iso(log.created_at) if log.created_at else None, 'full_text_appeal_enabled': AdventurerS1DataAppealService._log_uses_full_text_appeal(log), 'events': serialized_events}

    @staticmethod
    def _log_uses_full_text_appeal(log: AdventurerS1ExpeditionLog | None) -> bool:
        """Only new expedition logs opt in; old logs keep segment-selection behavior."""
        if not log or not isinstance(log.full_log, dict):
            return False
        return bool(log.full_log.get('data_appeal_full_text_default'))

    @staticmethod
    def _record_update_changes(original: dict, update: dict) -> bool:
        for key, value in update.items():
            if key == 'id':
                continue
            if original.get(key) != value:
                return True
        return False

    @staticmethod
    def _compact_member_update_for_evidence(update: dict | None) -> dict | None:
        """仅压缩探险证据里的 member_update：去掉空 list/dict，不碰特质快照/recovery 字段。"""
        if not isinstance(update, dict):
            return None
        compact: dict = {}
        for key, value in update.items():
            if value is None:
                continue
            if isinstance(value, str) and (not value.strip()):
                continue
            if isinstance(value, (list, dict, tuple, set)) and len(value) == 0:
                continue
            compact[key] = value
        return compact or None

    @staticmethod
    def _build_trait_change_conflicts_for_evidence(evidence: dict | None) -> list[dict]:
        """机械标出“出发已有同名特质却又被结算写进 new_traits”的冲突。"""
        if not isinstance(evidence, dict):
            return []
        departure_by_name: dict[str, dict] = {}
        for row in evidence.get('departure_traits') or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get('name') or '').strip()
            if name:
                departure_by_name[name] = row
        if not departure_by_name:
            return []
        member_update = evidence.get('member_update') if isinstance(evidence.get('member_update'), dict) else {}
        raw_new_traits = member_update.get('new_traits')
        lore_by_name: dict[str, str] = {}
        if isinstance(raw_new_traits, dict):
            for raw_name, raw_data in raw_new_traits.items():
                name = str(raw_name or '').strip()
                if not name or not isinstance(raw_data, dict):
                    continue
                lore = raw_data.get('lore_append') or raw_data.get('lore') or raw_data.get('witness')
                if lore:
                    lore_by_name[name] = str(lore).strip()
        try:
            from plugins.adventurer_s1.services.s1_trait_description_ai_service import parse_member_new_trait_updates
            new_traits = parse_member_new_trait_updates(raw_new_traits, {}, member_name='')
        except Exception:
            logger.exception('构建申诉特质冲突摘要失败')
            return []
        conflicts: list[dict] = []
        for entry in new_traits:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get('name') or '').strip()
            if not name or name not in departure_by_name:
                continue
            departure_desc = str(departure_by_name[name].get('description') or '').strip()
            new_desc = str(entry.get('description') or '').strip()
            conflict = {'name': name, 'in_departure_traits': True, 'in_member_update_new_traits': True, 'same_description': bool(departure_desc and new_desc and (departure_desc == new_desc))}
            if departure_desc:
                conflict['departure_description'] = departure_desc
            if new_desc:
                conflict['new_trait_description'] = new_desc
            lore = str(entry.get('lore_append') or lore_by_name.get(name) or '').strip()
            if lore:
                conflict['new_trait_lore_append'] = lore
            conflicts.append(conflict)
        return conflicts
    _EVIDENCE_METADATA_DROP_KEYS = frozenset({'event_text', 'log_status', 'log_created_at', 'party_name', 'target_dungeon'})
    _APPEAL_MEMBER_UPDATE_KEYS = frozenset({'id', 'gold_change', 'new_equipments', 'new_traits', 'removed_traits', 'new_skills', 'removed_skills', 'consumed_equipments', 'hp_change', 'is_dead', 'new_class'})
    _APPEAL_ASSET_MUTATION_STATE_FIELDS = {'trait': ('name', 'desc', 'is_archived'), 'skill': ('name', 'desc', 'equipped'), 'equipment': ('name', 'type', 'rarity', 'desc', 'quantity', 'uses_left', 'evolution_stage')}
    _APPEAL_ASSET_MUTATION_OPERATIONS = frozenset({'created', 'updated'})
    _APPEAL_EQUIPMENT_RARITIES = frozenset({'common', 'rare', 'epic', 'legendary'})

    @staticmethod
    def _minimize_new_equipments_for_ai(raw: object) -> dict:
        """member_update.new_equipments 只保留申诉判断需要的稀有度与数量。"""
        if not isinstance(raw, dict):
            return {}
        out: dict = {}
        for name, data in raw.items():
            if not name:
                continue
            rarity = 'common'
            quantity = 1
            if isinstance(data, dict):
                raw_rarity = data.get('rarity')
                if raw_rarity in AdventurerS1DataAppealService._APPEAL_EQUIPMENT_RARITIES:
                    rarity = raw_rarity
                normalized_quantity, _ = normalize_ascii_integer(data.get('quantity'), minimum=1, maximum=999999)
                if type(normalized_quantity) is int:
                    quantity = normalized_quantity
            out[str(name)] = {'rarity': rarity, 'quantity': quantity}
        return out

    @staticmethod
    def _compact_member_update_for_appeal_ai(update: dict | None) -> dict | None:
        """探险证据里的 member_update：白名单字段 + 压缩 new_equipments。"""
        if not isinstance(update, dict):
            return None
        update = project_s1_member_description_aliases(update)
        compact: dict = {}
        for key in AdventurerS1DataAppealService._APPEAL_MEMBER_UPDATE_KEYS:
            if key not in update:
                continue
            val = update[key]
            if val is None:
                continue
            if isinstance(val, str) and (not val.strip()):
                continue
            if isinstance(val, (list, dict, tuple, set)) and len(val) == 0:
                continue
            if key == 'new_equipments':
                minimized = AdventurerS1DataAppealService._minimize_new_equipments_for_ai(val)
                if minimized:
                    compact[key] = minimized
                continue
            compact[key] = val
        return compact or None

    @staticmethod
    def _pick_asset_mutation_state_for_appeal_ai(asset_type: str, raw: object) -> dict | None:
        if not isinstance(raw, dict):
            return None
        allowed_fields = AdventurerS1DataAppealService._APPEAL_ASSET_MUTATION_STATE_FIELDS.get(asset_type)
        if allowed_fields is None:
            return None
        return {key: raw[key] for key in allowed_fields if key in raw}

    @staticmethod
    def _compact_party_member_update_summary_entry(update: dict | None) -> dict | None:
        """全队摘要单条：adventurer_id/name + new_equipments。"""
        if not isinstance(update, dict):
            return None
        try:
            adventurer_id = int(update.get('id'))
        except (TypeError, ValueError):
            return None
        entry: dict = {'adventurer_id': adventurer_id}
        name = str(update.get('name') or update.get('adventurer_name') or '').strip()
        if name:
            entry['adventurer_name'] = name
        new_equipments = AdventurerS1DataAppealService._minimize_new_equipments_for_ai(update.get('new_equipments'))
        if new_equipments:
            entry['new_equipments'] = new_equipments
        return entry

    @staticmethod
    def _build_party_member_updates_summary(full_log: dict | None) -> list[dict]:
        """全队 member_updates 摘要：每人 new_equipments，供申诉 AI 核对队友结算。"""
        if not isinstance(full_log, dict):
            return []
        from plugins.adventurer_s1.services.adventurer_s1_log_service import build_public_s1_full_log
        public_full_log = build_public_s1_full_log(full_log)
        updates = public_full_log.get('member_updates') or []
        if not updates:
            return []
        adv_ids: list[int] = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                adv_ids.append(int(update.get('id')))
            except (TypeError, ValueError):
                continue
        id_to_name: dict[int, str] = {}
        if adv_ids:
            id_to_name = dict(db_manager.query(AdventurerS1).with_entities(AdventurerS1.id, AdventurerS1.name).filter(AdventurerS1.id.in_(adv_ids)).all())
        summary: list[dict] = []
        for update in updates:
            if not isinstance(update, dict):
                continue
            try:
                adventurer_id = int(update.get('id'))
            except (TypeError, ValueError):
                continue
            row = dict(update)
            if not row.get('name') and adventurer_id in id_to_name:
                row['name'] = id_to_name[adventurer_id]
            entry = AdventurerS1DataAppealService._compact_party_member_update_summary_entry(row)
            if entry:
                summary.append(entry)
        return summary

    @staticmethod
    def _ensure_party_member_updates_summary(evidence: dict) -> None:
        """旧申诉证据若缺全队摘要，按 expedition_log_id 从 log 补建（原地修改）。"""
        if evidence.get('party_member_updates_summary') is not None:
            return
        try:
            log_id = int(evidence.get('expedition_log_id'))
        except (TypeError, ValueError):
            return
        log = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == log_id).first()
        if not log or not isinstance(log.full_log, dict):
            return
        summary = AdventurerS1DataAppealService._build_party_member_updates_summary(log.full_log)
        evidence['party_member_updates_summary'] = summary

    @staticmethod
    def _build_departure_equipment_ownership(full_log: dict | None) -> list[dict]:
        """Build exact pre-expedition equipment ownership facts for appeal guards."""
        if not isinstance(full_log, dict):
            return []
        from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix
        grouped: dict[tuple[int, str], dict] = {}
        for snapshot in full_log.get('audit_member_snapshot') or []:
            if not isinstance(snapshot, dict):
                continue
            try:
                owner_id = int(snapshot.get('adventurer_id'))
            except (TypeError, ValueError):
                continue
            for item in snapshot.get('equipment') or []:
                if not isinstance(item, dict):
                    continue
                raw_name = str(item.get('name') or '').strip()
                name, suffix_quantity = split_stack_suffix(raw_name, 1)
                if not name:
                    continue
                try:
                    quantity = max(1, int(item.get('quantity') or suffix_quantity or 1))
                except (TypeError, ValueError):
                    quantity = max(1, int(suffix_quantity or 1))
                key = (owner_id, name)
                entry = grouped.setdefault(key, {'name': name, 'owner_adventurer_id': owner_id, 'quantity': 0})
                entry['quantity'] += quantity
        return list(grouped.values())

    @staticmethod
    def _build_departure_named_resource_ownership(full_log: dict | None, snapshot_field: str) -> list[dict]:
        """Build exact pre-expedition trait/skill ownership facts for appeal guards."""
        if not isinstance(full_log, dict) or snapshot_field not in ('traits', 'skills'):
            return []
        ownership: list[dict] = []
        seen: set[tuple[int, str]] = set()
        for snapshot in full_log.get('audit_member_snapshot') or []:
            if not isinstance(snapshot, dict):
                continue
            try:
                owner_id = int(snapshot.get('adventurer_id'))
            except (TypeError, ValueError):
                continue
            for resource in snapshot.get(snapshot_field) or []:
                if not isinstance(resource, dict):
                    continue
                name = str(resource.get('name') or '').strip()
                key = (owner_id, name)
                if not name or key in seen:
                    continue
                seen.add(key)
                ownership.append({'asset_id': resource.get('asset_id', resource.get('id')), 'name': name, 'owner_adventurer_id': owner_id})
        return ownership

    @staticmethod
    def _ensure_departure_equipment_ownership(evidence: dict | None) -> None:
        """Backfill frozen ownership facts for old appeal evidence without reading current inventory."""
        ownership_keys = ('departure_equipment_ownership', 'departure_trait_ownership', 'departure_skill_ownership')
        if not isinstance(evidence, dict) or all((evidence.get(key) is not None for key in ownership_keys)):
            return
        try:
            log_id = int(evidence.get('expedition_log_id'))
        except (TypeError, ValueError):
            return
        log = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == log_id).first()
        if not log:
            return
        if evidence.get('departure_equipment_ownership') is None:
            evidence['departure_equipment_ownership'] = AdventurerS1DataAppealService._build_departure_equipment_ownership(log.full_log)
        if evidence.get('departure_trait_ownership') is None:
            evidence['departure_trait_ownership'] = AdventurerS1DataAppealService._build_departure_named_resource_ownership(log.full_log, 'traits')
        if evidence.get('departure_skill_ownership') is None:
            evidence['departure_skill_ownership'] = AdventurerS1DataAppealService._build_departure_named_resource_ownership(log.full_log, 'skills')

    @staticmethod
    def _ensure_departure_consumption_candidates(evidence: dict | None, adventurer_id: int | None=None) -> None:
        """Refresh exact consumed departure rows that older evidence filters could omit."""
        if not isinstance(evidence, dict):
            return
        if adventurer_id is None:
            member_update = evidence.get('member_update')
            if not isinstance(member_update, dict):
                return
            adventurer_id = member_update.get('id')
        try:
            log_id = int(evidence.get('expedition_log_id'))
            owner_id = int(adventurer_id)
        except (TypeError, ValueError):
            return
        log = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == log_id).first()
        if not log:
            return
        evidence['departure_consumption_candidates'] = AdventurerS1DataAppealService._pick_departure_consumption_candidates(log.full_log if isinstance(log.full_log, dict) else {}, owner_id)

    @staticmethod
    def _appeal_expedition_identity_error(adventurer_id: int, expedition_log_id: int | None) -> str | None:
        if not expedition_log_id:
            return None
        try:
            adv_id = int(adventurer_id)
            log_id = int(expedition_log_id)
        except (TypeError, ValueError):
            return '关联探险身份参数无效'
        log = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == log_id).first()
        snapshot_ids = set()
        full_log = log.full_log if log and isinstance(log.full_log, dict) else {}
        for row in full_log.get('audit_member_snapshot') or []:
            if not isinstance(row, dict):
                continue
            try:
                snapshot_id = int(row.get('adventurer_id'))
            except (TypeError, ValueError):
                continue
            if snapshot_id > 0:
                snapshot_ids.add(snapshot_id)
        if snapshot_ids:
            if adv_id in snapshot_ids:
                return None
            return '当前申诉角色不在关联探险的任何冻结快照中；不能把旧角色快照写入新角色'
        member = db_manager.query(AdventurerS1ExpeditionLogMember.id).filter(AdventurerS1ExpeditionLogMember.expedition_log_id == log_id, AdventurerS1ExpeditionLogMember.adventurer_id == adv_id).first()
        if member:
            return None
        return '关联探险不属于当前申诉角色；新角色不能申诉旧角色参加的探险'

    @staticmethod
    def _appeal_effective_expedition_log_id(appeal: AdventurerS1DataAppeal) -> int | None:
        raw_log_id = appeal.expedition_log_id
        if not raw_log_id and isinstance(appeal.expedition_evidence, dict):
            raw_log_id = appeal.expedition_evidence.get('expedition_log_id')
        try:
            log_id = int(raw_log_id)
        except (TypeError, ValueError):
            return None
        return log_id if log_id > 0 else None

    @staticmethod
    def _appeal_identity_error(appeal: AdventurerS1DataAppeal) -> str | None:
        """统一落库门禁：只比较申诉接收角色与冻结快照角色。"""
        return AdventurerS1DataAppealService._appeal_snapshot_identity_error(appeal)

    @staticmethod
    def _trim_record_dict_for_ai(data: dict, record_type: str) -> dict:
        """裁剪 serialize 结果里 AI 用不到的默认值/空字段。"""
        if not isinstance(data, dict):
            return data
        out = dict(data)
        if record_type in ('equipment', 'warehouse', 'skill'):
            out.pop('recovery_started_at', None)
            out.pop('recovery_ends_at', None)
        if record_type == 'trait':
            if not out.get('is_archived'):
                out.pop('is_archived', None)
            policy = str(out.get('recovery_policy') or 'none').strip().lower()
            if policy in ('', 'none'):
                for key in ('recovery_policy', 'recovery_stage', 'recovery_progress', 'recovery_required', 'recovery_hours', 'recovered_desc'):
                    out.pop(key, None)
            elif not out.get('recovery_progress'):
                out.pop('recovery_progress', None)
            if not out.get('recovered_desc'):
                out.pop('recovered_desc', None)
        elif record_type in ('equipment', 'warehouse'):
            if not out.get('uses_left'):
                out.pop('uses_left', None)
            if not out.get('damage_note'):
                out.pop('damage_note', None)
            if record_type == 'equipment' and (not int(out.get('evolution_stage') or 0)):
                out.pop('evolution_stage', None)
        elif record_type == 'skill':
            if not out.get('equipped'):
                out.pop('equipped', None)
        return out

    @staticmethod
    def compact_all_player_records_for_ai(records: dict | None) -> dict | None:
        """all_player_records / selected_records：冒险者只留 id+name，子表裁剪空字段。"""
        if not isinstance(records, dict):
            return records
        out: dict = {}
        adv = records.get('adventurer')
        if isinstance(adv, dict):
            slim: dict = {}
            if adv.get('id') is not None:
                slim['id'] = adv['id']
            name = adv.get('name')
            if name:
                slim['name'] = name
            if slim:
                out['adventurer'] = slim
        for section, record_type in (('traits', 'trait'), ('equipment', 'equipment'), ('warehouse', 'warehouse'), ('skills', 'skill')):
            rows = records.get(section) or []
            trimmed = [AdventurerS1DataAppealService._trim_record_dict_for_ai(row, record_type) for row in rows if isinstance(row, dict)]
            if trimmed:
                out[section] = trimmed
        from plugins.adventurer_s1.services.s1_appeal_evolution_evidence_service import HISTORY_KEY, project_history_for_ai
        if isinstance(records.get(HISTORY_KEY), dict):
            out[HISTORY_KEY] = project_history_for_ai(records[HISTORY_KEY])
        return out or None

    @staticmethod
    def compact_expedition_evidence_payload(evidence: dict | None) -> dict | None:
        """申诉 AI 探险证据：去重复/无用元数据，member_update 走申诉白名单。"""
        if not evidence or not isinstance(evidence, dict):
            return evidence
        evidence_payload = {k: v for k, v in evidence.items() if k not in AdventurerS1DataAppealService._EVIDENCE_METADATA_DROP_KEYS}
        from plugins.adventurer_s1.services.s1_appeal_rarity_restore_service import EVIDENCE_KEY as rarity_restore_key, project_candidates_for_ai
        if rarity_restore_key in evidence_payload:
            evidence_payload[rarity_restore_key] = project_candidates_for_ai(evidence_payload[rarity_restore_key] or [])
        events = evidence_payload.get('all_events')
        if isinstance(events, list):
            indexed_events: list[dict] = []
            for event_index, event in enumerate(events):
                if isinstance(event, dict):
                    indexed_event = {'event_index': event_index, **{key: value for key, value in event.items() if key != 'event_index'}}
                else:
                    indexed_event = {'event_index': event_index, 'text': str(event or '')}
                indexed_events.append(indexed_event)
            evidence_payload['all_events'] = indexed_events
        member_update = evidence_payload.get('member_update')
        if isinstance(member_update, dict):
            compact = AdventurerS1DataAppealService._compact_member_update_for_appeal_ai(member_update)
            if compact:
                evidence_payload['member_update'] = compact
            else:
                evidence_payload.pop('member_update', None)
        traits = evidence_payload.get('departure_traits')
        if isinstance(traits, list):
            slim_traits = []
            for row in traits:
                if not isinstance(row, dict):
                    continue
                name = str(row.get('name') or '').strip()
                if not name:
                    continue
                entry = {'name': name}
                desc = str(row.get('description') or '').strip()
                if desc:
                    entry['description'] = desc
                slim_traits.append(entry)
            if slim_traits:
                evidence_payload['departure_traits'] = slim_traits
            else:
                evidence_payload.pop('departure_traits', None)
        conflicts = AdventurerS1DataAppealService._build_trait_change_conflicts_for_evidence(evidence)
        if conflicts:
            evidence_payload['trait_change_conflicts'] = conflicts
        party_summary = evidence_payload.get('party_member_updates_summary')
        if isinstance(party_summary, list):
            slim_party: list[dict] = []
            for row in party_summary:
                if not isinstance(row, dict):
                    continue
                entry = AdventurerS1DataAppealService._compact_party_member_update_summary_entry({'id': row.get('adventurer_id'), 'name': row.get('adventurer_name'), 'new_equipments': row.get('new_equipments')})
                if entry:
                    slim_party.append(entry)
            if slim_party:
                evidence_payload['party_member_updates_summary'] = slim_party
            else:
                evidence_payload.pop('party_member_updates_summary', None)
        departure_ownership = evidence_payload.get('departure_equipment_ownership')
        if isinstance(departure_ownership, list):
            slim_ownership: list[dict] = []
            for row in departure_ownership:
                if not isinstance(row, dict):
                    continue
                name = str(row.get('name') or '').strip()
                try:
                    owner_id = int(row.get('owner_adventurer_id'))
                    quantity = max(1, int(row.get('quantity') or 1))
                except (TypeError, ValueError):
                    continue
                if name and owner_id > 0:
                    slim_ownership.append({'name': name, 'owner_adventurer_id': owner_id, 'quantity': quantity})
            if slim_ownership:
                evidence_payload['departure_equipment_ownership'] = slim_ownership
            else:
                evidence_payload.pop('departure_equipment_ownership', None)
        for ownership_key in ('departure_trait_ownership', 'departure_skill_ownership'):
            raw_ownership = evidence_payload.get(ownership_key)
            if not isinstance(raw_ownership, list):
                continue
            slim_named_ownership: list[dict] = []
            for row in raw_ownership:
                if not isinstance(row, dict):
                    continue
                name = str(row.get('name') or '').strip()
                try:
                    owner_id = int(row.get('owner_adventurer_id'))
                except (TypeError, ValueError):
                    continue
                if name and owner_id > 0:
                    slim_named_ownership.append({'asset_id': row.get('asset_id'), 'name': name, 'owner_adventurer_id': owner_id})
            if slim_named_ownership:
                evidence_payload[ownership_key] = slim_named_ownership
            else:
                evidence_payload.pop(ownership_key, None)
        return order_s1_data_appeal_expedition_evidence(evidence_payload)

    @staticmethod
    def prepare_appeal_ai_user_payload(payload: dict) -> dict:
        """裁剪申诉上下文；机械查库结果保留明确的否定、数量与匹配记录。"""
        if not isinstance(payload, dict):
            return {}
        out = dict(payload)
        evidence = out.get('expedition_evidence')
        if evidence:
            out['expedition_evidence'] = AdventurerS1DataAppealService.compact_expedition_evidence_payload(evidence)
        for key in ('all_player_records', 'selected_records', 'related_records'):
            block = out.get(key)
            if isinstance(block, dict):
                compact_block = AdventurerS1DataAppealService.compact_all_player_records_for_ai(block)
                if compact_block:
                    out[key] = compact_block
                else:
                    out.pop(key, None)
        compact = AdventurerS1DataAppealService.compact_for_ai_payload(out)
        from plugins.adventurer_s1.services.s1_appeal_evolution_evidence_service import preserve_histories_after_compaction
        compact = preserve_histories_after_compaction(compact, out, record_blocks=('all_player_records', 'selected_records', 'related_records'))
        lookup_results = out.get('lookup_results')
        if isinstance(lookup_results, list) and lookup_results:
            compact['lookup_results'] = copy.deepcopy(lookup_results)
        return compact

    @staticmethod
    def build_slim_primary_ai_response_for_review(raw: dict | None) -> dict:
        """二审 AI 只需一审结论字段，勿重复传整份 raw。"""
        if not isinstance(raw, dict):
            return {}
        slim: dict = {}
        for key in ('summary', 'player_reject_reason', 'player_reply_summary', 'warnings', 'patch', 'core_request_satisfied', 'trait_description_audit', 'item_quantity_audit', 'create_item_description_audit', 'request_checklist', 'ownership_stripped_grants'):
            val = raw.get(key)
            if val is None:
                continue
            if isinstance(val, (list, dict, str)) and (not val):
                continue
            slim[key] = val
        return slim

    @staticmethod
    def _pick_player_member_update(full_log: dict | None, adventurer_id: int) -> dict | None:
        if not isinstance(full_log, dict):
            return None
        from plugins.adventurer_s1.services.adventurer_s1_log_service import build_public_s1_full_log
        public_full_log = build_public_s1_full_log(full_log)
        for update in public_full_log.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            try:
                update_id = int(update.get('id'))
            except (TypeError, ValueError):
                continue
            if update_id == adventurer_id:
                return AdventurerS1DataAppealService._compact_member_update_for_appeal_ai(update)
        return None

    @staticmethod
    def _pick_player_settlement_receipt(full_log: dict | None, adventurer_id: int) -> dict | None:
        """投影本人最终结算回执，作为本次探险实际写回的精确凭证。"""
        if not isinstance(full_log, dict):
            return None
        receipt = full_log.get('settlement_receipt')
        if not isinstance(receipt, dict) or receipt.get('status') not in (None, 'passed'):
            return None
        for member in receipt.get('members') or []:
            if not isinstance(member, dict):
                continue
            try:
                member_id = int(member.get('adventurer_id'))
            except (TypeError, ValueError):
                continue
            if member_id != int(adventurer_id):
                continue
            if member.get('status') != 'passed':
                return None
            checked = [str(value).strip() for value in member.get('checked') or [] if isinstance(value, str) and value.strip()]
            projected = {'adventurer_id': member_id, 'status': 'passed', 'checked': checked}
            schema_version = receipt.get('schema_version')
            if isinstance(schema_version, int) and schema_version > 0:
                projected['schema_version'] = schema_version
            asset_mutations: list[dict] = []
            for mutation in (member.get('asset_mutations') or [])[:30]:
                if not isinstance(mutation, dict):
                    continue
                asset_type = str(mutation.get('asset_type') or '').strip()
                operation = str(mutation.get('operation') or '').strip()
                if asset_type not in AdventurerS1DataAppealService._APPEAL_ASSET_MUTATION_STATE_FIELDS or operation not in AdventurerS1DataAppealService._APPEAL_ASSET_MUTATION_OPERATIONS:
                    continue
                try:
                    asset_id = int(mutation.get('asset_id'))
                except (TypeError, ValueError):
                    continue
                if asset_id <= 0 or not isinstance(mutation.get('after'), dict):
                    continue
                after_state = AdventurerS1DataAppealService._pick_asset_mutation_state_for_appeal_ai(asset_type, mutation.get('after'))
                if after_state is None:
                    continue
                projected_mutation = {'asset_type': asset_type, 'asset_id': asset_id, 'operation': operation}
                if operation == 'created':
                    projected_mutation['after'] = after_state
                else:
                    before_state = AdventurerS1DataAppealService._pick_asset_mutation_state_for_appeal_ai(asset_type, mutation.get('before'))
                    if before_state is None:
                        continue
                    changed_fields = [field for field in AdventurerS1DataAppealService._APPEAL_ASSET_MUTATION_STATE_FIELDS[asset_type] if before_state.get(field) != after_state.get(field)]
                    if not changed_fields:
                        continue
                    asset_name = str(after_state.get('name') or '').strip()
                    if asset_name:
                        projected_mutation['asset_name'] = asset_name
                    projected_mutation['before'] = {field: before_state.get(field) for field in changed_fields}
                    projected_mutation['after'] = {field: after_state.get(field) for field in changed_fields}
                asset_mutations.append(projected_mutation)
            if asset_mutations:
                projected['asset_mutations'] = asset_mutations
            return projected
        return None

    @staticmethod
    def _pick_departure_traits(full_log: dict | None, adventurer_id: int) -> list[dict]:
        """从出发快照中提取该成员出发时的特质列表，供申诉 AI 交叉验证。"""
        if not isinstance(full_log, dict):
            return []
        for snapshot in full_log.get('audit_member_snapshot') or []:
            if not isinstance(snapshot, dict):
                continue
            try:
                if int(snapshot.get('adventurer_id')) != int(adventurer_id):
                    continue
            except (TypeError, ValueError):
                continue
            traits = []
            for t in snapshot.get('traits') or []:
                if not isinstance(t, dict):
                    continue
                name = str(t.get('name') or '').strip()
                if not name:
                    continue
                traits.append({'name': name, 'description': str(t.get('description') or '')})
            return traits
        return []

    @staticmethod
    def _pick_departure_consumption_candidates(full_log: dict | None, adventurer_id: int) -> list[dict]:
        """提取本人被结算消耗的全部精确同名出发物品，语义交给 AI 复核。"""
        if not isinstance(full_log, dict):
            return []
        member_update = AdventurerS1DataAppealService._pick_player_member_update(full_log, adventurer_id) or {}
        consumed_names = AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('consumed_equipments'))
        if not consumed_names:
            return []
        for snapshot in full_log.get('audit_member_snapshot') or []:
            if not isinstance(snapshot, dict):
                continue
            try:
                if int(snapshot.get('adventurer_id')) != int(adventurer_id):
                    continue
            except (TypeError, ValueError):
                continue
            candidates: list[dict] = []
            for item in snapshot.get('equipment') or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get('name') or '').strip()
                item_type = str(item.get('type') or '').strip().lower()
                try:
                    uses_left = int(item.get('uses_left')) if item.get('uses_left') is not None else None
                except (TypeError, ValueError):
                    uses_left = None
                if name not in consumed_names:
                    continue
                candidate = {'name': name, 'current_type': item_type, 'quantity': max(1, int(item.get('quantity') or 1)), 'authoritative_description': str(item.get('description') or '').strip(), 'source': 'departure_equipment_snapshot', 'was_consumed': True, 'destination': 'equipment'}
                if uses_left is not None:
                    candidate['uses_left'] = uses_left
                candidates.append(candidate)
            return candidates
        return []

    @staticmethod
    def _build_equipment_description_inheritance_candidates(full_log: dict | None, adventurer_id: int, snapshot_before: dict | None) -> list[dict]:
        """机械定位“旧物被消耗 + 新名物结算 + 当前选中同名记录”，语义配对交给申诉 AI。"""
        if not isinstance(full_log, dict) or not isinstance(snapshot_before, dict):
            return []
        member_update = None
        for update in full_log.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            try:
                if int(update.get('id')) == int(adventurer_id):
                    member_update = update
                    break
            except (TypeError, ValueError):
                continue
        if not member_update:
            return []
        new_equipments = member_update.get('new_equipments')
        if not isinstance(new_equipments, dict) or not new_equipments:
            return []
        consumed_names = AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('consumed_equipments'))
        if not consumed_names:
            return []
        departure_items: list[dict] = []
        for member_snapshot in full_log.get('audit_member_snapshot') or []:
            if not isinstance(member_snapshot, dict):
                continue
            try:
                if int(member_snapshot.get('adventurer_id')) != int(adventurer_id):
                    continue
            except (TypeError, ValueError):
                continue
            for item in member_snapshot.get('equipment') or []:
                if not isinstance(item, dict):
                    continue
                old_name = str(item.get('name') or '').strip()
                old_desc = str(item.get('description') or item.get('custom_desc') or '').strip()
                if old_name in consumed_names and old_desc:
                    departure_items.append({'name': old_name, 'type': str(item.get('type') or '').strip(), 'rarity': str(item.get('rarity') or '').strip(), 'description': old_desc})
            break
        if not departure_items:
            return []
        candidates: list[dict] = []
        for section in ('equipment', 'warehouse'):
            for current in snapshot_before.get(section) or []:
                if not isinstance(current, dict):
                    continue
                current_name = str(current.get('name') or '').strip()
                if not current_name or current_name not in new_equipments:
                    continue
                try:
                    target_id = int(current.get('id'))
                except (TypeError, ValueError):
                    continue
                settlement_data = new_equipments.get(current_name)
                settlement_data = settlement_data if isinstance(settlement_data, dict) else {}
                settlement_desc = str(settlement_data.get('desc') or settlement_data.get('description') or settlement_data.get('custom_desc') or '').strip()
                candidate = {'target_section': section, 'target_id': target_id, 'current_name': current_name, 'current_type': str(current.get('type') or '').strip(), 'current_rarity': str(current.get('rarity') or '').strip(), 'current_custom_desc': str(current.get('custom_desc') or '').strip(), 'current_catalog_desc': str(current.get('catalog_desc') or '').strip(), 'predecessor_candidates': copy.deepcopy(departure_items), 'source': 'same_member_exact_settlement_and_departure_snapshot'}
                if settlement_desc:
                    candidate['settlement_description'] = settlement_desc
                candidates.append(candidate)
        return candidates

    @staticmethod
    def _attach_equipment_description_inheritance_candidates(evidence: dict | None, adventurer_id: int, snapshot_before: dict | None) -> dict | None:
        """补充装备修复候选；结构映射不替代两位陪审团的语义裁决。"""
        if not isinstance(evidence, dict):
            return evidence
        out = copy.deepcopy(evidence)
        out.pop('equipment_description_inheritance_candidates', None)
        try:
            log_id = int(out.get('expedition_log_id'))
        except (TypeError, ValueError):
            return out
        log = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == log_id).first()
        full_log = log.full_log if log and isinstance(log.full_log, dict) else None
        candidates = AdventurerS1DataAppealService._build_equipment_description_inheritance_candidates(full_log, adventurer_id, snapshot_before)
        if candidates:
            out['equipment_description_inheritance_candidates'] = candidates
        from plugins.adventurer_s1.services.s1_appeal_rarity_restore_service import refresh_candidates
        return refresh_candidates(out, adventurer_id, snapshot_before or {})

    @staticmethod
    def _build_special_dungeon_appeal_context(full_log: dict | None, adventurer_id: int) -> dict | None:
        """只向申诉链暴露本人特殊机缘的结构化判定，不携带其他成员指令。"""
        if not isinstance(full_log, dict):
            return None
        context = full_log.get('special_dungeon_context')
        if not isinstance(context, dict):
            return None
        directives = context.get('directives')
        if not isinstance(directives, dict):
            return None
        directive = directives.get(str(adventurer_id), directives.get(adventurer_id))
        if directive is None:
            return None
        out = {'dungeon': str(context.get('dungeon') or '').strip(), 'reward_kind': str(context.get('reward_kind') or '').strip(), 'directives': {str(int(adventurer_id)): copy.deepcopy(directive)}}
        return {key: value for key, value in out.items() if value}

    @staticmethod
    def _attach_special_dungeon_appeal_context(evidence: dict | None, adventurer_id: int) -> dict | None:
        """为新旧申诉证据补齐本人特殊机缘权威上下文。"""
        if not isinstance(evidence, dict):
            return evidence
        out = copy.deepcopy(evidence)
        existing_context = AdventurerS1DataAppealService._build_special_dungeon_appeal_context({'special_dungeon_context': out.get('special_dungeon_context')}, adventurer_id)
        if existing_context:
            out['special_dungeon_context'] = existing_context
            return out
        try:
            log_id = int(out.get('expedition_log_id'))
        except (TypeError, ValueError):
            return out
        log = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == log_id).first()
        context = AdventurerS1DataAppealService._build_special_dungeon_appeal_context(log.full_log if log and isinstance(log.full_log, dict) else None, adventurer_id)
        if context:
            out['special_dungeon_context'] = context
        else:
            out.pop('special_dungeon_context', None)
        return out

    @staticmethod
    def _special_trait_relief_patch_guard(patch: dict, *, expedition_evidence: dict | None, adventurer_id: int, record_maps: dict) -> str | None:
        """祛厄药庐只授权本人一次根治；申诉不得把副本描述扩张为批量删特质。"""
        return None

    @staticmethod
    def _build_expedition_evidence(adventurer_id: int, log_id: int, event_index: int) -> tuple:
        latest = AdventurerS1DataAppealService._get_latest_completed_expedition_log(adventurer_id)
        if not latest:
            return (None, '暂无可关联的已完成探险')
        if latest.id != int(log_id):
            return (None, '只能关联最近一次已完成的探险')
        log = db_manager.query(AdventurerS1ExpeditionLog).join(AdventurerS1ExpeditionLogMember, AdventurerS1ExpeditionLogMember.expedition_log_id == AdventurerS1ExpeditionLog.id).filter(AdventurerS1ExpeditionLog.id == int(log_id), AdventurerS1ExpeditionLogMember.adventurer_id == int(adventurer_id)).first()
        if not log:
            return (None, '探险日志不存在或无权访问')
        from plugins.adventurer_s1.services.adventurer_s1_log_service import build_public_s1_full_log
        from utils.time_utils import to_utc_iso
        public_full_log = build_public_s1_full_log(log.full_log if isinstance(log.full_log, dict) else {})
        events = [e.strip() for e in public_full_log.get('events') or [] if isinstance(e, str) and e.strip()]
        try:
            idx = int(event_index)
        except (TypeError, ValueError):
            return (None, '段落索引无效')
        if idx < 0 or idx >= len(events):
            return (None, '段落索引超出范围')
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        return (AdventurerS1DataAppealService.prepare_expedition_evidence_for_ai({'expedition_log_id': log.id, 'event_index': idx, 'target_dungeon': log.target_dungeon, 'party_name': log.party_name, 'log_status': log.status, 'log_created_at': to_utc_iso(log.created_at) if log.created_at else None, 'event_text': events[idx], 'all_events': events, 'member_update': AdventurerS1DataAppealService._pick_player_member_update(full_log, adventurer_id), 'settlement_receipt': AdventurerS1DataAppealService._pick_player_settlement_receipt(full_log, adventurer_id), 'departure_traits': AdventurerS1DataAppealService._pick_departure_traits(full_log, adventurer_id), 'departure_consumption_candidates': AdventurerS1DataAppealService._pick_departure_consumption_candidates(full_log, adventurer_id), 'party_member_updates_summary': AdventurerS1DataAppealService._build_party_member_updates_summary(full_log), 'departure_equipment_ownership': AdventurerS1DataAppealService._build_departure_equipment_ownership(full_log), 'departure_trait_ownership': AdventurerS1DataAppealService._build_departure_named_resource_ownership(full_log, 'traits'), 'departure_skill_ownership': AdventurerS1DataAppealService._build_departure_named_resource_ownership(full_log, 'skills'), 'special_dungeon_context': AdventurerS1DataAppealService._build_special_dungeon_appeal_context(full_log, adventurer_id)})[0], None)

    @staticmethod
    def _get_codename_maps_for_log(log_id: int) -> tuple[dict[str, str], dict[int, str]]:
        """按 audit_member_snapshot 顺序构建代号→真名与 id→真名。"""
        log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=int(log_id)).first()
        if not log or not isinstance(log.full_log, dict):
            return ({}, {})
        snap = log.full_log.get('audit_member_snapshot')
        if not isinstance(snap, list) or not snap:
            return ({}, {})
        adv_ids = [s.get('adventurer_id') for s in snap if isinstance(s, dict) and s.get('adventurer_id')]
        if not adv_ids:
            return ({}, {})
        id_to_name = dict(db_manager.query(AdventurerS1).with_entities(AdventurerS1.id, AdventurerS1.name).filter(AdventurerS1.id.in_(adv_ids)).all())
        from plugins.adventurer_s1.services.adventurer_s1_prompt_service import build_codename_to_real_from_snapshot
        mapping = build_codename_to_real_from_snapshot(snap, id_to_name)
        member_id_to_name = {int(aid): id_to_name[int(aid)] for aid in adv_ids if aid in id_to_name}
        return (mapping, member_id_to_name)

    @staticmethod
    def prepare_expedition_evidence_for_ai(evidence: dict | None) -> tuple[dict | None, dict[str, str]]:
        """把探险证据里的代号还原为真名，并附带 member_id_to_name 供 AI 对照。"""
        if not evidence or not isinstance(evidence, dict):
            return (evidence, {})
        out = copy.deepcopy(evidence)
        member_update = out.get('member_update')
        if isinstance(member_update, dict):
            try:
                member_id = int(member_update.get('id'))
            except (TypeError, ValueError):
                member_id = None
            if member_id is not None:
                out = AdventurerS1DataAppealService._attach_special_dungeon_appeal_context(out, member_id)
        AdventurerS1DataAppealService._ensure_party_member_updates_summary(out)
        AdventurerS1DataAppealService._ensure_departure_equipment_ownership(out)
        try:
            log_id = int(out.get('expedition_log_id'))
        except (TypeError, ValueError):
            return (out, {})
        from plugins.adventurer_s1.services.s1_appeal_hidden_drop_guard import HIDDEN_DROP_EVIDENCE_KEY, project_hidden_drop_receipts
        out.pop(HIDDEN_DROP_EVIDENCE_KEY, None)
        reward_log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=log_id).first()
        if reward_log and isinstance(reward_log.full_log, dict):
            out[HIDDEN_DROP_EVIDENCE_KEY] = project_hidden_drop_receipts(reward_log.full_log)
            from plugins.adventurer_s1.services.s1_team_loot_service import S1TeamLootService, team_loot_appeal_source_hash
            out['unassigned_item_status'] = S1TeamLootService.evidence_status(log_id)
            out['_team_loot_source_hash'] = team_loot_appeal_source_hash(reward_log.full_log)
        mapping, member_id_to_name = AdventurerS1DataAppealService._get_codename_maps_for_log(log_id)
        if not mapping:
            return (out, {})
        from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import _restore_codenames_in_result, _restore_codenames_in_reward_fields, restore_names_in_narrative
        if member_id_to_name:
            out['member_id_to_name'] = {str(mid): name for mid, name in member_id_to_name.items() if name}
        events = out.get('all_events')
        if isinstance(events, list):
            out['all_events'] = [restore_names_in_narrative(e, mapping) if isinstance(e, str) else e for e in events]
        if isinstance(out.get('event_text'), str):
            out['event_text'] = restore_names_in_narrative(out['event_text'], mapping)
        mu = out.get('member_update')
        if isinstance(mu, dict):
            restored = _restore_codenames_in_result({'member_updates': [copy.deepcopy(mu)]}, mapping)
            out['member_update'] = restored['member_updates'][0]
        party_summary = out.get('party_member_updates_summary')
        if isinstance(party_summary, list) and mapping:

            def _restore(text: str) -> str:
                return restore_names_in_narrative(text, mapping)
            for entry in party_summary:
                if not isinstance(entry, dict):
                    continue
                fake_update = {'new_equipments': entry.get('new_equipments')}
                _restore_codenames_in_reward_fields(fake_update, _restore)
                if fake_update.get('new_equipments'):
                    entry['new_equipments'] = fake_update['new_equipments']
        return (out, mapping)

    @staticmethod
    def restore_codenames_in_appeal_narrative(text: str | None, mapping: dict[str, str]) -> str:
        if not text or not mapping:
            return text or ''
        from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import restore_names_in_narrative
        return restore_names_in_narrative(text, mapping)

    @staticmethod
    def restore_codenames_in_appeal_ai_response(result: dict | None, mapping: dict[str, str]) -> dict | None:
        """AI 输出里面向玩家的叙事字段顺手还原残留代号。"""
        if not result or not isinstance(result, dict) or (not mapping):
            return result
        for key in ('summary', 'player_reject_reason', 'player_reason'):
            val = result.get(key)
            if isinstance(val, str) and val:
                result[key] = AdventurerS1DataAppealService.restore_codenames_in_appeal_narrative(val, mapping)
        warnings = result.get('warnings')
        if isinstance(warnings, list):
            result['warnings'] = [AdventurerS1DataAppealService.restore_codenames_in_appeal_narrative(w, mapping) if isinstance(w, str) else w for w in warnings]
        return result

    @staticmethod
    def _build_latest_settlement_evidence(adventurer_id: int) -> dict | None:
        """玩家未选择剧情段落时，自动附最近一次完成探险的「全场结算证据」。

        重点：包含全部 events（不只单段）+ 本人 member_update（结算净归属），用于陪审团
        AI 在无玩家指定段落时也能以全局视角判断诉求，避免“单段剧情断章取义”。
        """
        log = AdventurerS1DataAppealService._get_latest_completed_expedition_log(adventurer_id)
        if not log:
            return None
        from plugins.adventurer_s1.services.adventurer_s1_log_service import build_public_s1_full_log
        from utils.time_utils import to_utc_iso
        public_full_log = build_public_s1_full_log(log.full_log if isinstance(log.full_log, dict) else {})
        events = [e.strip() for e in public_full_log.get('events') or [] if isinstance(e, str) and e.strip()]
        if not events:
            return None
        raw = {'expedition_log_id': log.id, 'event_index': None, 'auto_fallback': True, 'target_dungeon': log.target_dungeon, 'party_name': log.party_name, 'log_status': log.status, 'log_created_at': to_utc_iso(log.created_at) if log.created_at else None, 'event_text': None, 'all_events': events, 'member_update': AdventurerS1DataAppealService._pick_player_member_update(log.full_log if isinstance(log.full_log, dict) else {}, adventurer_id), 'settlement_receipt': AdventurerS1DataAppealService._pick_player_settlement_receipt(log.full_log if isinstance(log.full_log, dict) else {}, adventurer_id), 'departure_traits': AdventurerS1DataAppealService._pick_departure_traits(log.full_log if isinstance(log.full_log, dict) else {}, adventurer_id), 'departure_consumption_candidates': AdventurerS1DataAppealService._pick_departure_consumption_candidates(log.full_log if isinstance(log.full_log, dict) else {}, adventurer_id), 'party_member_updates_summary': AdventurerS1DataAppealService._build_party_member_updates_summary(log.full_log if isinstance(log.full_log, dict) else {}), 'departure_equipment_ownership': AdventurerS1DataAppealService._build_departure_equipment_ownership(log.full_log if isinstance(log.full_log, dict) else {}), 'departure_trait_ownership': AdventurerS1DataAppealService._build_departure_named_resource_ownership(log.full_log if isinstance(log.full_log, dict) else {}, 'traits'), 'departure_skill_ownership': AdventurerS1DataAppealService._build_departure_named_resource_ownership(log.full_log if isinstance(log.full_log, dict) else {}, 'skills')}
        return AdventurerS1DataAppealService.prepare_expedition_evidence_for_ai(raw)[0]

    @staticmethod
    def get_appeal_targets(user_id: int) -> tuple:
        """返回玩家可申诉的条目列表（含 id）。"""
        cached = _appeal_targets_cache.get(user_id)
        if cached is not None:
            return (cached, None)
        adv, err = AdventurerS1DataAppealService._get_owner_adventurer(user_id, load_items=True)
        if err:
            return (None, err)
        latest_log = AdventurerS1DataAppealService._get_latest_completed_expedition_log(adv.id)
        rarity_items = [('equipment', int(e.id), str(e.name or '').strip(), int(e.quantity or 1)) for e in adv.equipment_items] + [('warehouse', int(w.id), str(w.name or '').strip(), int(w.quantity or 1)) for w in adv.warehouse_items]
        rarity_ctx = AdventurerS1DataAppealService._make_rarity_review_eligibility_ctx(adv.id, rarity_items, latest_expedition_log=latest_log)
        traits = [AdventurerS1DataAppealService._serialize_record('trait', t) for t in adv.traits if not t.is_archived]
        locked_names = AdventurerS1DataAppealService._text_locked_item_names_from_instances(adv.equipment_items, adv.warehouse_items)
        equipment = []
        for e in adv.equipment_items:
            rec = AdventurerS1DataAppealService._serialize_record('equipment', e, text_locked_names=locked_names)
            eligible, _ = AdventurerS1DataAppealService._eval_rarity_review_eligibility(rarity_ctx, 'equipment', int(e.id), str(e.name or '').strip(), int(e.quantity or 1))
            rec['rarity_review_eligible'] = eligible
            equipment.append(rec)
        warehouse = []
        for w in adv.warehouse_items:
            rec = AdventurerS1DataAppealService._serialize_record('warehouse', w, text_locked_names=locked_names)
            eligible, _ = AdventurerS1DataAppealService._eval_rarity_review_eligibility(rarity_ctx, 'warehouse', int(w.id), str(w.name or '').strip(), int(w.quantity or 1))
            rec['rarity_review_eligible'] = eligible
            warehouse.append(rec)
        skills = [AdventurerS1DataAppealService._serialize_record('skill', s) for s in adv.skill_items]
        payload = {'adventurer_id': adv.id, 'adventurer_name': adv.name, 'traits': traits, 'equipment': equipment, 'warehouse': warehouse, 'skills': skills, 'latest_expedition': AdventurerS1DataAppealService._serialize_latest_expedition_for_selection(latest_log), 'quota': AdventurerS1DataAppealService.get_user_quota_player(user_id)}
        _appeal_targets_cache.set(user_id, payload)
        return (payload, None)

    @staticmethod
    def _fetch_target_record(adventurer_id: int, target_type: str, target_id: int):
        model = _TABLE_MODEL.get(target_type)
        if not model:
            return None
        return db_manager.query(model).filter(model.id == target_id, model.adventurer_id == adventurer_id).first()

    @staticmethod
    def _build_snapshot(adventurer_id: int, targets: list) -> tuple:
        snapshot = {'traits': [], 'equipment': [], 'warehouse': [], 'skills': []}
        locked_names = AdventurerS1DataAppealService._text_locked_item_names(adventurer_id)
        target_records = []
        for t in targets:
            ttype = t.get('type')
            tid = t.get('id')
            if ttype not in _VALID_TARGET_TYPES or not tid:
                return (None, '目标格式无效')
            record = AdventurerS1DataAppealService._fetch_target_record(adventurer_id, ttype, int(tid))
            if not record:
                return (None, f'找不到 {ttype} id={tid} 或无权访问')
            target_records.append((t, ttype, record))
        catalog_desc_map = AdventurerS1DataAppealService._load_catalog_desc_map({str(rec.name).strip() for _, ttype, rec in target_records if ttype in ('equipment', 'warehouse') and str(rec.name or '').strip()})
        for t, ttype, record in target_records:
            if ttype == 'trait':
                key = 'traits'
            elif ttype == 'skill':
                key = 'skills'
            else:
                key = ttype
            snapshot[key].append(AdventurerS1DataAppealService._serialize_record(ttype, record, text_locked_names=locked_names, catalog_desc_map=catalog_desc_map))
        from plugins.adventurer_s1.services.s1_appeal_evolution_evidence_service import HISTORY_KEY, capture_evolution_history
        evolution_history = capture_evolution_history(adventurer_id, snapshot)
        if evolution_history is not None:
            snapshot[HISTORY_KEY] = evolution_history
        return (snapshot, None)

    @staticmethod
    def _build_recent_item_activity(adventurer_id: int, limit: int=30, appealed_expedition_log_id: int | None=None) -> list:
        """最近的非随身获得流水（已剔除 gain 获得记录）。

        consume 表示物品离开随身/仓库；overflow 表示随身栏满后成功转存仓库，并非销毁或流失。
        给陪审团 AI 看 ItemLog 最近若干条相关记录，用于结合当前持有数据核对物品去向。
        gain（获得）对“物品消失”判定无价值且占比最大，故在 SQL 层剔除以省 token。
        market（交易市场）流出额外解析 market_outcome：挂单只是把物品托管，真实结局看挂单
        当前状态——active=仍在挂可随时取回 / cancelled=已退回仓库 / completed=真售出或换出，
        避免把“上架后又取消”误判成“卖了”而错删仍在仓库的物品。
        只回显 AI 判断需要的最小字段；按时间倒序。
        """
        from plugins.adventurer_s1.models import AdventurerS1ItemLog, AdventurerS1MarketListing
        from utils.time_utils import to_utc_iso
        rows = db_manager.query(AdventurerS1ItemLog).filter(AdventurerS1ItemLog.adventurer_id == adventurer_id, AdventurerS1ItemLog.change_type != 'gain').order_by(AdventurerS1ItemLog.created_at.desc()).limit(max(1, min(int(limit or 30), 100))).all()
        listing_ids = set()
        for r in rows:
            if r.source_type == 'market' and r.related_type == 'market_listing':
                try:
                    listing_ids.add(int(r.related_id))
                except (TypeError, ValueError):
                    continue
        listing_status: dict = {}
        if listing_ids:
            for listing in db_manager.query(AdventurerS1MarketListing).filter(AdventurerS1MarketListing.id.in_(listing_ids)).all():
                listing_status[listing.id] = listing.status
        market_outcome_label = {'active': '仍在挂单中·未售出·可随时取消取回（物品未失去，勿据此删物或判定已卖）', 'completed': '挂单已成交·真实售出或换出（物品确已离开本人）', 'cancelled': '挂单已取消·物品已退回仓库（未失去，去 all_player_records 仓库核对，勿当作已卖）'}
        result = []
        for r in rows:
            item = {'item_name': r.item_name, 'item_rarity': r.item_rarity, 'change_type': r.change_type, 'quantity': int(r.quantity or 1), 'source_type': r.source_type, 'description': r.description, 'created_at': to_utc_iso(r.created_at) if r.created_at else None}
            if r.change_type == 'overflow':
                item['storage_outcome'] = '已成功转存仓库·物品未丢失（不得视为销毁或流出）'
            if r.source_type == 'expedition':
                source_expedition_log_id = None
                if r.related_type == 'expedition_log':
                    try:
                        source_expedition_log_id = int(r.related_id)
                    except (TypeError, ValueError):
                        source_expedition_log_id = None
                if source_expedition_log_id is None:
                    relation = 'unattributed'
                elif appealed_expedition_log_id is not None and source_expedition_log_id == int(appealed_expedition_log_id):
                    relation = 'same_appealed_expedition'
                else:
                    relation = 'different_expedition'
                item['expedition_provenance'] = {'relation': relation, 'expedition_log_id': source_expedition_log_id}
            if r.source_type == 'market' and r.related_type == 'market_listing':
                try:
                    lid = int(r.related_id)
                except (TypeError, ValueError):
                    lid = None
                item['market_outcome'] = market_outcome_label.get(listing_status.get(lid), '挂单状态未知·无法确认是否真售出（勿据此删物或判定已卖）')
            result.append(item)
        return result

    @staticmethod
    def _rarity_review_item_details(adventurer_id: int, targets: list[dict]) -> tuple:
        """构建稀有度复核 AI 输入；只包含当前物品详情，不含玩家描述。"""
        details = []
        for target in targets:
            ttype = str(target.get('type') or '').strip()
            try:
                tid = int(target.get('id'))
            except (TypeError, ValueError):
                return (None, '目标 id 无效')
            if ttype not in ('equipment', 'warehouse'):
                return (None, '稀有度复核只能选择随身装备或收藏室物品')
            record = AdventurerS1DataAppealService._fetch_target_record(adventurer_id, ttype, tid)
            if not record:
                return (None, f'找不到 {ttype} id={tid} 或无权访问')
            catalog = db_manager.query(AdventurerS1Item).filter(AdventurerS1Item.name == record.name).first()
            details.append({'type': ttype, 'id': tid, 'name': record.name, 'item_type': record.type or '', 'current_rarity': record.rarity or 'common', 'description': record.custom_desc or (catalog.description if catalog else '') or '', 'quantity': int(record.quantity or 1)})
        return (details, None)

    @staticmethod
    def _rarity_review_log_is_disqualifying(log) -> bool:
        if AdventurerS1DataAppealService._rarity_review_is_appeal_restore_item_log(log):
            return False
        change_type = str(getattr(log, 'change_type', '') or '')
        source_type = str(getattr(log, 'source_type', '') or '')
        return change_type in ('consume', 'overflow') or source_type != 'expedition'

    @staticmethod
    def _batch_settlement_restored_lineage_map(adventurer_id: int, item_names: set[str]) -> dict[str, dict]:
        clean_names = {str(n).strip() for n in item_names if str(n).strip()}
        if not clean_names:
            return {}
        rows = db_manager.query(AdventurerS1DataAppealAuditLog, AdventurerS1DataAppeal).join(AdventurerS1DataAppeal, AdventurerS1DataAppeal.id == AdventurerS1DataAppealAuditLog.appeal_id).filter(AdventurerS1DataAppealAuditLog.adventurer_id == int(adventurer_id), AdventurerS1DataAppealAuditLog.op_type == 'create_item', AdventurerS1DataAppeal.status == 'approved', AdventurerS1DataAppeal.expedition_log_id.isnot(None)).order_by(AdventurerS1DataAppealAuditLog.id.desc()).limit(80).all()
        log_ids: set[int] = set()
        for _audit, appeal in rows:
            try:
                log_id = int(appeal.expedition_log_id or 0)
            except (TypeError, ValueError):
                continue
            if log_id > 0:
                log_ids.add(log_id)
        logs_by_id: dict[int, AdventurerS1ExpeditionLog] = {}
        if log_ids:
            for log in db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id.in_(log_ids)).all():
                logs_by_id[log.id] = log
        result: dict[str, dict] = {}
        for audit, appeal in rows:
            after = audit.after_json if isinstance(audit.after_json, dict) else {}
            clean_name = str(after.get('name') or '').strip()
            if not clean_name or clean_name not in clean_names or clean_name in result:
                continue
            try:
                log_id = int(appeal.expedition_log_id or 0)
            except (TypeError, ValueError):
                continue
            if log_id <= 0:
                continue
            meta = AdventurerS1DataAppealService._lookup_expedition_settlement_item_meta_from_log(logs_by_id.get(log_id), int(adventurer_id), clean_name)
            if not meta:
                continue
            result[clean_name] = {'appeal_id': int(appeal.id), 'expedition_log_id': log_id, 'settlement_meta': meta, 'granted_at': AdventurerS1DataAppealService._appeal_effective_apply_time(appeal)}
        return result

    @staticmethod
    def _make_rarity_review_eligibility_ctx(adventurer_id: int, items: list[tuple[str, int, str, int]], *, latest_expedition_log=None) -> dict:
        """批量预取稀有度复核 eligibility 所需数据，避免 get_appeal_targets 循环 N+1。"""
        from sqlalchemy import and_, or_
        valid_items: list[tuple[str, int, str, int]] = []
        for target_type, target_id, item_name, quantity in items:
            if target_type in ('equipment', 'warehouse'):
                valid_items.append((target_type, int(target_id), str(item_name or '').strip(), int(quantity or 1)))
        eq_ids = [tid for t, tid, _, _ in valid_items if t == 'equipment']
        wh_ids = [tid for t, tid, _, _ in valid_items if t == 'warehouse']
        names = {name for _, _, name, _ in valid_items if name}
        prior_keys: set[tuple[str, int]] = set()
        audit_filters = []
        if eq_ids:
            audit_filters.append(and_(AdventurerS1DataAppealAuditLog.target_type == 'equipment', AdventurerS1DataAppealAuditLog.target_id.in_(eq_ids)))
        if wh_ids:
            audit_filters.append(and_(AdventurerS1DataAppealAuditLog.target_type == 'warehouse', AdventurerS1DataAppealAuditLog.target_id.in_(wh_ids)))
        if audit_filters:
            for row in db_manager.query(AdventurerS1DataAppealAuditLog.target_type, AdventurerS1DataAppealAuditLog.target_id).filter(or_(*audit_filters)).all():
                prior_keys.add((str(row[0]), int(row[1])))
        gain_by_name: dict[str, AdventurerS1ItemLog] = {}
        logs_by_name: dict[str, list] = defaultdict(list)
        if names:
            all_logs = db_manager.query(AdventurerS1ItemLog).filter(AdventurerS1ItemLog.adventurer_id == int(adventurer_id), AdventurerS1ItemLog.item_name.in_(list(names))).order_by(AdventurerS1ItemLog.created_at.asc()).all()
            for log in all_logs:
                logs_by_name[log.item_name].append(log)
            for log in reversed(all_logs):
                if log.item_name not in gain_by_name and log.change_type in ('gain', 'overflow') and (log.source_type == 'expedition'):
                    gain_by_name[log.item_name] = log
        lineage_by_name = AdventurerS1DataAppealService._batch_settlement_restored_lineage_map(adventurer_id, names)
        if latest_expedition_log is None:
            latest_expedition_log = AdventurerS1DataAppealService._get_latest_completed_expedition_log(adventurer_id)
        latest_since = latest_expedition_log.created_at if latest_expedition_log else None
        return {'prior_keys': prior_keys, 'gain_by_name': gain_by_name, 'logs_by_name': logs_by_name, 'lineage_by_name': lineage_by_name, 'latest_since': latest_since}

    @staticmethod
    def _eval_rarity_review_eligibility(ctx: dict, target_type: str, target_id: int, item_name: str, quantity: int) -> tuple[bool, str | None]:
        """基于批量预取上下文判定单条物品记录的稀有度复核 eligibility。"""
        if target_type not in ('equipment', 'warehouse'):
            return (False, '稀有度复核只能选择随身装备或收藏室物品')
        if int(quantity or 1) < 1:
            return (False, '物品数量无效')
        key = (target_type, int(target_id))
        if key in ctx['prior_keys']:
            return (False, '已被申诉或后台修复变更过')
        clean_name = str(item_name or '').strip()
        logs = ctx['logs_by_name'].get(clean_name, [])
        gain = ctx['gain_by_name'].get(clean_name)
        if gain:
            for log in logs:
                if log.id != gain.id and log.created_at > gain.created_at:
                    return (False, '获得后已有交易/锻造/消耗等变动')
            return (True, None)
        lineage = ctx['lineage_by_name'].get(clean_name)
        if lineage:
            granted_at = lineage.get('granted_at')
            if granted_at:
                for log in logs:
                    if log.created_at <= granted_at:
                        continue
                    if AdventurerS1DataAppealService._rarity_review_log_is_disqualifying(log):
                        return (False, '申诉补回后已有交易/锻造/消耗等变动')
            return (True, None)
        if target_type == 'equipment':
            since = ctx['latest_since']
            for log in logs:
                if since is not None and log.created_at <= since:
                    continue
                if AdventurerS1DataAppealService._rarity_review_log_is_disqualifying(log):
                    return (False, '最近探险后已有交易/锻造/消耗等变动')
            return (True, None)
        return (False, '没有副本掉落获得记录')

    @staticmethod
    def _rarity_review_item_has_prior_audit(target_type: str, target_id: int) -> bool:
        return db_manager.query(AdventurerS1DataAppealAuditLog.id).filter(AdventurerS1DataAppealAuditLog.target_type == target_type, AdventurerS1DataAppealAuditLog.target_id == int(target_id)).first() is not None

    @staticmethod
    def _rarity_review_find_expedition_gain(adventurer_id: int, item_name: str):
        return db_manager.query(AdventurerS1ItemLog).filter(AdventurerS1ItemLog.adventurer_id == adventurer_id, AdventurerS1ItemLog.item_name == item_name, AdventurerS1ItemLog.change_type.in_(('gain', 'overflow')), AdventurerS1ItemLog.source_type == 'expedition').order_by(AdventurerS1ItemLog.created_at.desc()).first()

    @staticmethod
    def _rarity_review_has_flow_after_gain(adventurer_id: int, item_name: str, gain: AdventurerS1ItemLog) -> bool:
        return db_manager.query(AdventurerS1ItemLog.id).filter(AdventurerS1ItemLog.adventurer_id == adventurer_id, AdventurerS1ItemLog.item_name == item_name, AdventurerS1ItemLog.created_at > gain.created_at, AdventurerS1ItemLog.id != gain.id).first() is not None

    @staticmethod
    def _rarity_review_is_appeal_restore_item_log(log) -> bool:
        return str(getattr(log, 'source_type', '') or '') in ('data_appeal_ai', 'data_appeal_reclaim') and str(getattr(log, 'change_type', '') or '') in ('gain', 'overflow')

    @staticmethod
    def _find_settlement_restored_lineage(adventurer_id: int, item_name: str) -> dict | None:
        """approved 申诉 create_item 补回 + full_log 有结算元数据 → 可认定副本结算来源。"""
        clean_name = str(item_name or '').strip()
        if not clean_name:
            return None
        return AdventurerS1DataAppealService._batch_settlement_restored_lineage_map(adventurer_id, {clean_name}).get(clean_name)

    @staticmethod
    def _rarity_review_has_disqualifying_activity_since(adventurer_id: int, item_name: str, since: datetime | None) -> bool:
        q = db_manager.query(AdventurerS1ItemLog).filter(AdventurerS1ItemLog.adventurer_id == adventurer_id, AdventurerS1ItemLog.item_name == item_name)
        if since is not None:
            q = q.filter(AdventurerS1ItemLog.created_at > since)
        from sqlalchemy import and_, not_, or_
        appeal_restore = and_(AdventurerS1ItemLog.source_type.in_(('data_appeal_ai', 'data_appeal_reclaim')), AdventurerS1ItemLog.change_type.in_(('gain', 'overflow')))
        return q.filter(not_(appeal_restore), or_(AdventurerS1ItemLog.change_type.in_(('consume', 'overflow')), AdventurerS1ItemLog.source_type != 'expedition')).first() is not None

    @staticmethod
    def _check_rarity_review_item_eligible(adventurer_id: int, target_type: str, target_id: int, *, item_name: str, quantity: int) -> tuple[bool, str | None]:
        """判定单件物品是否可提交稀有度复核（与所选探险段落并不要求同一次掉落）。"""
        ctx = AdventurerS1DataAppealService._make_rarity_review_eligibility_ctx(adventurer_id, [(target_type, int(target_id), str(item_name or '').strip(), int(quantity or 1))])
        return AdventurerS1DataAppealService._eval_rarity_review_eligibility(ctx, target_type, int(target_id), str(item_name or '').strip(), int(quantity or 1))

    @staticmethod
    def _validate_rarity_review_targets(adventurer_id: int, targets: list[dict], expedition_log_id: int) -> tuple:
        """校验复核物品为未改动的副本掉落物，或副本内强化后稀有度异常的随身装备。"""
        if not targets or not isinstance(targets, list):
            return (None, '请至少选择一件可复核物品')
        if len(targets) > _MAX_RARITY_REVIEW_TARGETS:
            return (None, f'单次最多复核 {_MAX_RARITY_REVIEW_TARGETS} 件物品')
        return AdventurerS1DataAppealService._rarity_review_item_details(adventurer_id, targets)

    @staticmethod
    def _build_rarity_review_patch(ai_raw: dict, valid_items: list[dict]) -> tuple:
        valid_by_key = {(i['type'], int(i['id'])): i for i in valid_items}
        updates = {'equipment': [], 'warehouse': []}
        checklist = []
        for row in ai_raw.get('items') or []:
            if not isinstance(row, dict):
                continue
            ttype = str(row.get('type') or '').strip()
            try:
                tid = int(row.get('id'))
            except (TypeError, ValueError):
                continue
            item = valid_by_key.get((ttype, tid))
            if not item:
                continue
            rarity = str(row.get('rarity') or 'common').strip().lower()
            if rarity not in _VALID_RARITIES:
                rarity = 'common'
            reason = str(row.get('reason') or '').strip()[:160]
            current = str(item.get('current_rarity') or 'common').strip().lower()
            if rarity != current:
                updates[ttype].append({'id': tid, 'rarity': rarity})
                status = 'done'
                detail = f'鉴定为 {rarity}（原 {current}）' + (f'：{reason}' if reason else '')
            else:
                status = 'rejected'
                detail = reason or '与当前稀有度一致，无需修正'
            checklist.append({'label': f"复核「{item['name']}」稀有度", 'category': 'item', 'status': status, 'detail': detail})
        patch = {'equipment': {'update': updates['equipment'], 'delete': []}, 'warehouse': {'update': updates['warehouse'], 'delete': []}}
        patch = {k: v for k, v in patch.items() if v.get('update')}
        return (patch, checklist)

    @staticmethod
    def _rarity_review_patch_guard(appeal: AdventurerS1DataAppeal, patch: dict) -> tuple:
        if not isinstance(patch, dict):
            return (None, '稀有度复核 patch 格式无效')
        forbidden = []
        for k, v in patch.items():
            if k in ('equipment', 'warehouse'):
                continue
            if k in ('traits', 'skills') and isinstance(v, dict):
                if not v.get('update') and (not v.get('delete')):
                    continue
            elif k in ('create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'reclaim_items', 'trait_destructive_actions', 'skill_destructive_actions'):
                if not v:
                    continue
            elif k in ('team_pool', 'tavern_subsidy'):
                if not v:
                    continue
            forbidden.append(k)
        if forbidden:
            return (None, _RARITY_REVIEW_GUARD_ERROR)
        snapshot_maps = {'equipment': {int(r['id']): r for r in (appeal.snapshot_before or {}).get('equipment') or [] if r.get('id') is not None}, 'warehouse': {int(r['id']): r for r in (appeal.snapshot_before or {}).get('warehouse') or [] if r.get('id') is not None}}
        selected = set()
        for t in appeal.selected_targets or []:
            if not isinstance(t, dict):
                continue
            ttype = str(t.get('type') or '').strip()
            if ttype not in ('equipment', 'warehouse'):
                continue
            try:
                selected.add((ttype, int(t.get('id'))))
            except (TypeError, ValueError):
                continue
        for section in ('equipment', 'warehouse'):
            sec = patch.get(section) or {}
            if sec.get('delete'):
                return (None, '稀有度复核不能删除物品')
            for upd in sec.get('update') or []:
                if set(upd.keys()) - {'id', 'rarity'}:
                    return (None, '稀有度复核只能修改 rarity 字段')
                try:
                    tid = int(upd.get('id'))
                except (TypeError, ValueError):
                    return (None, '稀有度复核目标 id 无效')
                if (section, tid) not in selected:
                    return (None, '稀有度复核不能修改未选中的物品')
                rarity = str(upd.get('rarity') or '').strip().lower()
                if rarity not in _VALID_RARITIES:
                    return (None, '稀有度复核目标稀有度无效')
                original = snapshot_maps.get(section, {}).get(tid) or {}
                current = str(original.get('rarity') or 'common').strip().lower()
                if rarity == current:
                    return (None, '稀有度复核未改变稀有度')
        _, purity_err = AdventurerS1DataAppealService._validate_rarity_review_targets(appeal.adventurer_id, appeal.selected_targets or [], int(appeal.expedition_log_id or 0))
        if purity_err:
            return (None, purity_err)
        return (patch, None)

    @staticmethod
    def _process_rarity_review_ai_body(appeal_id: int, appeal: AdventurerS1DataAppeal, snapshot: dict, evidence: dict, appeal_adventurer: dict, claim_token: str | None=None) -> None:
        try:
            rarity_ai_failed = False
            valid_items, purity_err = AdventurerS1DataAppealService._validate_rarity_review_targets(appeal.adventurer_id, appeal.selected_targets or [], int(evidence.get('expedition_log_id') or 0))
            if purity_err:
                raw = {'summary': f'稀有度复核未执行：{purity_err}', 'player_reject_reason': purity_err, 'core_request_satisfied': False, 'request_checklist': [{'label': '副本掉落稀有度复核', 'category': 'item', 'status': 'rejected', 'detail': purity_err}], 'patch': {}}
            else:
                from plugins.adventurer_s1.services.adventurer_s1_data_appeal_ai_service import call_rarity_review_ai
                raw = call_rarity_review_ai(appeal_id, items=valid_items, expedition_evidence=evidence, appeal_adventurer=appeal_adventurer, player_note=appeal.player_note or '')
                if not raw or not isinstance(raw, dict):
                    rarity_ai_failed = True
                    raw = {'summary': '稀有度复核 AI 未返回有效结果', 'player_reject_reason': '复核暂时失败，请稍后重试', 'core_request_satisfied': False, 'request_checklist': [{'label': '副本掉落稀有度复核', 'category': 'item', 'status': 'rejected', 'detail': '复核暂时失败'}], 'patch': {}}
                patch, checklist = AdventurerS1DataAppealService._build_rarity_review_patch(raw, valid_items)
                raw['patch'] = patch
                raw['request_checklist'] = checklist
                raw['core_request_satisfied'] = bool(patch)
                if patch:
                    raw['player_reply_summary'] = '已完成副本掉落稀有度复核，符合条件的物品会自动修正稀有度。'
                else:
                    item_reasons = [f"{row.get('label')}：{row.get('detail')}" for row in checklist if isinstance(row, dict) and row.get('label') and row.get('detail')]
                    reason = '；'.join(item_reasons) or '所选物品的鉴定稀有度与当前值一致，无需修正'
                    raw['summary'] = f'稀有度复核已完成：{reason}'
                    raw['player_reject_reason'] = reason
            appeal = AdventurerS1DataAppealService._reload_appeal_for_ai_write(appeal_id, claim_token, for_update=True)
            if not appeal:
                logger.info('稀有度复核中止 appeal=%s: 租约或工单状态已变化', appeal_id)
                return
            patch_raw = raw.get('patch') if isinstance(raw.get('patch'), dict) else {}
            normalized, patch_err = AdventurerS1DataAppealService.normalize_patch(patch_raw, snapshot, allow_rarity_increase=True, expedition_evidence=evidence, adventurer_id=appeal.adventurer_id, appeal_id=appeal_id)
            patch_error_code = 'rarity_review_patch_normalize_failed' if patch_err else None
            if normalized:
                guarded, guard_err = AdventurerS1DataAppealService._rarity_review_patch_guard(appeal, normalized)
                if guard_err:
                    normalized = None
                    patch_err = guard_err
                    patch_error_code = 'rarity_review_patch_guard_failed'
                else:
                    normalized = guarded
            appeal.ai_raw_response = json_dumps_safe(raw)
            appeal.ai_summary = str(raw.get('summary') or '').strip() or '稀有度复核已完成'
            if normalized:
                appeal.proposed_patch = normalized
                appeal.ai_status = 'done'
                appeal.review_ai_status = 'done'
                appeal.review_ai_summary = '稀有度复核只修改 rarity 字段，已通过机械护栏'
                appeal.review_ai_raw_response = json_dumps_safe({'decision': 'allow', 'summary': '稀有度复核机械放行', 'skipped_review': True})
            elif rarity_ai_failed:
                appeal.ai_status = 'failed'
                appeal.ai_summary = f"{appeal.ai_summary}（未生成可执行修复：{patch_err or '无稀有度变化'}）"
                appeal.review_ai_status = 'failed'
                appeal.review_ai_summary = '稀有度复核 AI 未返回有效结果，未执行机械护栏'
            elif patch_err and patch_raw:
                appeal.ai_status = 'failed'
                appeal.ai_summary = f'{appeal.ai_summary}（但 patch 校验未通过：{patch_err}）'
                appeal.review_ai_status = 'failed'
                appeal.review_ai_summary = f'稀有度复核机械护栏未通过：{patch_err}'
                appeal.review_ai_raw_response = json_dumps_safe({'decision': 'reject', 'summary': '稀有度复核机械护栏未通过', 'skipped_review': True, 'error_code': patch_error_code or 'rarity_review_patch_validation_failed'})
            else:
                appeal.ai_status = 'done'
                appeal.review_ai_status = 'done'
                appeal.review_ai_summary = '稀有度复核无需二次 AI，机械护栏确认无稀有度变化'
                appeal.review_ai_raw_response = json_dumps_safe({'decision': 'allow', 'summary': '稀有度复核完成，无需修改', 'skipped_review': True})
            appeal.status = 'processing'
            ok, err = db_manager.commit()
            if not ok:
                logger.error('稀有度复核结果落库错误: %s', err)
                return
            AdventurerS1DataAppealService._complete_ai_review(appeal_id, claim_token=claim_token)
        except Exception:
            logger.exception('稀有度复核 AI 异常 appeal=%s', appeal_id)
            AdventurerS1DataAppealService._mark_ai_interrupted(appeal_id, '稀有度复核异常中断，未生成可执行结论', claim_token=claim_token)

    @staticmethod
    def _build_all_player_records(adventurer_id: int, *, include_internal_recovery_state: bool=False) -> dict:
        """构建当前全量 S1 记录，不扩大可改 ID；内部时间仅供执行幂等核对。"""
        adventurer = db_manager.query(AdventurerS1).filter(AdventurerS1.id == adventurer_id).first()
        traits = db_manager.query(AdventurerS1Trait).filter(AdventurerS1Trait.adventurer_id == adventurer_id).order_by(AdventurerS1Trait.id.asc()).all()
        equipment = db_manager.query(AdventurerS1Equipment).filter(AdventurerS1Equipment.adventurer_id == adventurer_id).order_by(AdventurerS1Equipment.id.asc()).all()
        warehouse = db_manager.query(AdventurerS1WarehouseItem).filter(AdventurerS1WarehouseItem.adventurer_id == adventurer_id).order_by(AdventurerS1WarehouseItem.id.asc()).all()
        skills = db_manager.query(AdventurerS1Skill).filter(AdventurerS1Skill.adventurer_id == adventurer_id).order_by(AdventurerS1Skill.id.asc()).all()
        locked_names = AdventurerS1DataAppealService._text_locked_item_names(adventurer_id)
        item_names = {str(e.name).strip() for e in equipment if str(e.name or '').strip()} | {str(w.name).strip() for w in warehouse if str(w.name or '').strip()}
        catalog_desc_map = AdventurerS1DataAppealService._load_catalog_desc_map(item_names)
        return {'adventurer': {'id': adventurer.id, 'name': adventurer.name, 'status': adventurer.status} if adventurer else None, 'traits': [AdventurerS1DataAppealService._serialize_record('trait', t) for t in traits], 'equipment': [AdventurerS1DataAppealService._serialize_record('equipment', e, text_locked_names=locked_names, catalog_desc_map=catalog_desc_map, include_internal_recovery_state=include_internal_recovery_state) for e in equipment], 'warehouse': [AdventurerS1DataAppealService._serialize_record('warehouse', w, text_locked_names=locked_names, catalog_desc_map=catalog_desc_map, include_internal_recovery_state=include_internal_recovery_state) for w in warehouse], 'skills': [AdventurerS1DataAppealService._serialize_record('skill', s, include_internal_recovery_state=include_internal_recovery_state) for s in skills]}
    _APPEAL_RECORD_SECTIONS = ('traits', 'equipment', 'warehouse', 'skills')
    _APPEAL_SECTION_RECORD_TYPE = {'traits': 'trait', 'equipment': 'equipment', 'warehouse': 'warehouse', 'skills': 'skill'}

    @staticmethod
    def _collect_focus_ref_names(focus_refs: dict | None) -> dict[str, set[str]]:
        names = {s: set() for s in AdventurerS1DataAppealService._APPEAL_RECORD_SECTIONS}
        if not isinstance(focus_refs, dict):
            return names
        for section in AdventurerS1DataAppealService._APPEAL_RECORD_SECTIONS:
            for entry in focus_refs.get(section) or []:
                if isinstance(entry, str):
                    n = entry.strip()
                    if n:
                        names[section].add(n)
                elif isinstance(entry, dict):
                    n = str(entry.get('name') or '').strip()
                    if n:
                        names[section].add(n)
        return names

    @staticmethod
    def _collect_patch_record_ids(patch: dict | None) -> dict[str, set[int]]:
        ids = {s: set() for s in AdventurerS1DataAppealService._APPEAL_RECORD_SECTIONS}
        if not isinstance(patch, dict):
            return ids
        for section in AdventurerS1DataAppealService._APPEAL_RECORD_SECTIONS:
            sec = patch.get(section) if isinstance(patch.get(section), dict) else {}
            for upd in sec.get('update') or []:
                if isinstance(upd, dict) and upd.get('id') is not None:
                    record_id = _normalize_appeal_patch_integer(upd['id'], minimum=1)
                    if record_id is not None:
                        ids[section].add(record_id)
            for del_item in sec.get('delete') or []:
                if isinstance(del_item, dict) and del_item.get('id') is not None:
                    record_id = _normalize_appeal_patch_integer(del_item['id'], minimum=1)
                    if record_id is not None:
                        ids[section].add(record_id)
        ids['traits'].update(destructive_action_source_ids(patch, 'trait_destructive_actions'))
        ids['skills'].update(destructive_action_source_ids(patch, 'skill_destructive_actions'))
        return ids

    @staticmethod
    def _patch_references_outside_snapshot(patch: dict, snapshot_before: dict | None) -> bool:
        if not isinstance(patch, dict):
            return False
        snapshot_before = snapshot_before or {}
        patch_ids = AdventurerS1DataAppealService._collect_patch_record_ids(patch)
        for section, ids in patch_ids.items():
            selected_ids: set[int] = set()
            for row in snapshot_before.get(section) or []:
                if not isinstance(row, dict) or row.get('id') is None:
                    continue
                try:
                    selected_ids.add(int(row['id']))
                except (TypeError, ValueError):
                    pass
            if any((rid not in selected_ids for rid in ids)):
                return True
        return False

    @staticmethod
    def build_related_records_for_appeal_review(adventurer_id: int, *, snapshot_before: dict | None=None, focus_refs: dict | None=None, patch: dict | None=None) -> dict:
        """一审 focus_refs + selected + patch 引用 id → 二审 related_records 子集。"""
        full = AdventurerS1DataAppealService._build_all_player_records(adventurer_id)
        name_hints = AdventurerS1DataAppealService._collect_focus_ref_names(focus_refs)
        id_hints = AdventurerS1DataAppealService._collect_patch_record_ids(patch)
        if snapshot_before:
            for section in AdventurerS1DataAppealService._APPEAL_RECORD_SECTIONS:
                for row in snapshot_before.get(section) or []:
                    if not isinstance(row, dict):
                        continue
                    if row.get('id') is not None:
                        try:
                            id_hints[section].add(int(row['id']))
                        except (TypeError, ValueError):
                            pass
                    n = str(row.get('name') or '').strip()
                    if n:
                        name_hints[section].add(n)
        related: dict = {}
        adv = full.get('adventurer')
        if isinstance(adv, dict) and adv.get('id') is not None:
            slim_adv = {'id': adv['id']}
            if adv.get('name'):
                slim_adv['name'] = adv['name']
            related['adventurer'] = slim_adv
        for section in AdventurerS1DataAppealService._APPEAL_RECORD_SECTIONS:
            record_type = AdventurerS1DataAppealService._APPEAL_SECTION_RECORD_TYPE[section]
            picked: list[dict] = []
            seen_ids: set[int] = set()
            for row in full.get(section) or []:
                if not isinstance(row, dict):
                    continue
                try:
                    rid = int(row['id'])
                except (KeyError, TypeError, ValueError):
                    continue
                name = str(row.get('name') or '').strip()
                if rid not in id_hints[section] and name not in name_hints[section]:
                    continue
                if rid in seen_ids:
                    continue
                seen_ids.add(rid)
                picked.append(AdventurerS1DataAppealService._trim_record_dict_for_ai(row, record_type))
            if picked:
                related[section] = picked
        return related

    @staticmethod
    def resolve_lookup_queries(adventurer_id: int, lookup_queries: list | None) -> list[dict] | None:
        if not isinstance(lookup_queries, list) or not lookup_queries:
            return None
        full = AdventurerS1DataAppealService._build_all_player_records(adventurer_id)

        def _current_quantity(row: dict) -> int:
            try:
                return max(0, int(row.get('quantity', 1)))
            except (TypeError, ValueError):
                return 1
        results: list[dict] = []
        for q in lookup_queries[:24]:
            if not isinstance(q, dict):
                continue
            qtype = str(q.get('type') or '').strip()
            if qtype not in AdventurerS1DataAppealService._APPEAL_RECORD_SECTIONS:
                continue
            name = str(q.get('name') or '').strip()
            if not name:
                continue
            matches = []
            for row in full.get(qtype) or []:
                if not isinstance(row, dict):
                    continue
                if str(row.get('name') or '').strip() != name:
                    continue
                entry = {'id': row.get('id'), 'name': row.get('name')}
                if qtype in ('equipment', 'warehouse') and row.get('quantity') is not None:
                    entry['quantity'] = row.get('quantity')
                if qtype == 'skills' and row.get('equipped'):
                    entry['equipped'] = True
                matches.append(entry)
            results.append({'type': qtype, 'name': name, 'found': bool(matches), 'match_count': len(matches), 'matches': matches[:5], 'state_scope': 'current_state_only', 'current_total_quantity': sum((_current_quantity(row) for row in matches)) if qtype in ('equipment', 'warehouse') else len(matches)})
        return results or None

    @staticmethod
    def _narrative_currency_cap_for_evidence(evidence: dict | None) -> float | None:
        """叙述货币折算上限。

        member_update 已有正数 gold_change（结算据实落库）→ 返回 None，不夹紧，按 gold_change 据实补发。
        否则属于"叙述货币漏折现"，按初级委托量级封顶，防止把"一袋银币"夸张成几百金币。
        """
        from flask import current_app, has_app_context
        return None

    @staticmethod
    def _tavern_subsidy_cap_per_person() -> float:
        """酒馆补贴每人上限（individual 直接封顶；team 按人均封顶，总额=人均×存活人数）。"""
        from flask import current_app, has_app_context
        if has_app_context():
            try:
                return max(0.0, float(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_TAVERN_SUBSIDY_CAP_PER_PERSON', _TAVERN_SUBSIDY_CAP_PER_PERSON_DEFAULT)))
            except (TypeError, ValueError):
                return float(_TAVERN_SUBSIDY_CAP_PER_PERSON_DEFAULT)
        return float(_TAVERN_SUBSIDY_CAP_PER_PERSON_DEFAULT)

    @staticmethod
    def _is_empty_for_ai_payload(value) -> bool:
        """判断申诉 AI user payload 中是否可省略该值（未传 = 空）。"""
        if value is None:
            return True
        if isinstance(value, bool):
            return value is False
        if isinstance(value, str):
            return value == ''
        if isinstance(value, (int, float)):
            return value == 0 or value == 0.0
        if isinstance(value, dict):
            return len(value) == 0
        if isinstance(value, list):
            return len(value) == 0
        return False

    @staticmethod
    def compact_for_ai_payload(value):
        """递归剔除空字段/空容器，供申诉 AI user JSON 省 token。"""
        if isinstance(value, dict):
            out = {}
            mutation_type = value.get('asset_type')
            preserve_changed_state = value.get('operation') == 'updated' and mutation_type in AdventurerS1DataAppealService._APPEAL_ASSET_MUTATION_STATE_FIELDS and isinstance(value.get('before'), dict) and isinstance(value.get('after'), dict)
            for key, raw in value.items():
                preserve_zero_event_reference = key == 'event_index' or key in _APPEAL_AI_ZERO_BASED_EVENT_LIST_FIELDS
                if preserve_changed_state and key in ('before', 'after'):
                    allowed_fields = AdventurerS1DataAppealService._APPEAL_ASSET_MUTATION_STATE_FIELDS[mutation_type]
                    compacted = {field: raw[field] for field in allowed_fields if field in raw}
                elif preserve_zero_event_reference and isinstance(raw, list):
                    compacted = []
                    for item in raw:
                        compacted_item = AdventurerS1DataAppealService.compact_for_ai_payload(item)
                        is_zero_reference = isinstance(compacted_item, (int, float)) and (not isinstance(compacted_item, bool)) and (compacted_item == 0)
                        if is_zero_reference or not AdventurerS1DataAppealService._is_empty_for_ai_payload(compacted_item):
                            compacted.append(compacted_item)
                else:
                    compacted = AdventurerS1DataAppealService.compact_for_ai_payload(raw)
                is_zero_event_reference = preserve_zero_event_reference and isinstance(compacted, (int, float)) and (not isinstance(compacted, bool)) and (compacted == 0)
                if not is_zero_event_reference and AdventurerS1DataAppealService._is_empty_for_ai_payload(compacted):
                    continue
                out[key] = compacted
            return out
        if isinstance(value, list):
            out = []
            for item in value:
                compacted = AdventurerS1DataAppealService.compact_for_ai_payload(item)
                if AdventurerS1DataAppealService._is_empty_for_ai_payload(compacted):
                    continue
                out.append(compacted)
            return out
        return value

    @staticmethod
    def _patch_text_mentions_other_party_member(item: dict, *, appeal_adventurer_id: int, party_roster: list | None, text_keys: tuple[str, ...]=('reason', 'description')) -> tuple[bool, str | None]:
        """Detect privileged self-only creates that explicitly describe a teammate as the target."""
        if not isinstance(item, dict):
            return (False, None)
        has_explicit_target = False
        for target_key in ('owner_adventurer_id', 'target_adventurer_id'):
            if target_key not in item:
                continue
            has_explicit_target = True
            explicit_target = item[target_key]
            target_id = _normalize_appeal_patch_integer(explicit_target, minimum=1)
            if target_id is None:
                return (True, f'{target_key}=invalid')
            if target_id != int(appeal_adventurer_id):
                return (True, f'adventurer_id={target_id}')
        if has_explicit_target or not party_roster:
            return (False, None)
        haystack_parts = []
        for key in text_keys:
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                haystack_parts.append(val)
        haystack = '\n'.join(haystack_parts)
        if not haystack:
            return (False, None)
        appeal_name = ''
        for member in party_roster:
            if not isinstance(member, dict):
                continue
            try:
                member_id = int(member.get('adventurer_id'))
            except (TypeError, ValueError):
                continue
            if member_id == int(appeal_adventurer_id):
                appeal_name = str(member.get('name') or '').strip()
                break
        if appeal_name and appeal_name in haystack:
            return (False, None)
        for member in party_roster:
            if not isinstance(member, dict):
                continue
            try:
                member_id = int(member.get('adventurer_id'))
            except (TypeError, ValueError):
                continue
            if member_id == int(appeal_adventurer_id):
                continue
            name = str(member.get('name') or '').strip()
            if name and name in haystack:
                return (True, name)
        return (False, None)

    @staticmethod
    def strip_cross_adventurer_self_grants(raw_patch: dict, *, appeal_adventurer_id: int, party_roster: list | None) -> tuple[dict, list[dict]]:
        """Remove self-only grants with conflicting beneficiary assertions.

        create_items/create_traits/create_skills/currency_adjustments have no target id in the
        execution schema; applying them always affects appeal.adventurer_id. Currency grants
        use explicit target assertions here and leave narrative ownership to paired review.
        Asset creates retain their existing text guard.
        """
        if not isinstance(raw_patch, dict):
            return (raw_patch, [])
        cleaned = copy.deepcopy(raw_patch)
        removed: list[dict] = []
        trait_merge = AdventurerS1DataAppealService._patch_is_trait_merge_consolidation(cleaned)
        skill_merge = AdventurerS1DataAppealService._patch_is_skill_merge_consolidation(cleaned)
        sections = {'create_items': ('reason', 'description'), 'create_traits': ('reason', 'description'), 'create_skills': ('reason', 'description'), 'currency_adjustments': ()}
        for section, text_keys in sections.items():
            rows = cleaned.get(section)
            if not isinstance(rows, list):
                continue
            if section == 'create_traits' and trait_merge:
                continue
            if section == 'create_skills' and skill_merge:
                continue
            kept = []
            for row in rows:
                mentions_other, target = AdventurerS1DataAppealService._patch_text_mentions_other_party_member(row, appeal_adventurer_id=appeal_adventurer_id, party_roster=party_roster, text_keys=text_keys)
                if mentions_other:
                    removed.append({'section': section, 'name': row.get('name') if isinstance(row, dict) else None, 'target': target})
                    continue
                kept.append(row)
            cleaned[section] = kept
        return (cleaned, removed)

    @staticmethod
    def _append_cross_adventurer_grant_warnings(raw: dict, removed: list[dict]) -> None:
        if not isinstance(raw, dict) or not removed:
            return
        warnings = raw.get('warnings') if isinstance(raw.get('warnings'), list) else []
        for item in removed:
            section = item.get('section') or 'patch'
            name = item.get('name') or '未命名条目'
            target = item.get('target') or '其它队员'
            warnings.append(f'后端已剔除 {section}「{name}」：该普通补发指向{target}，但本类操作只能作用于申诉者本人。')
        raw['warnings'] = warnings

    @staticmethod
    def _appeal_patch_name_set(raw) -> set[str]:
        names: set[str] = set()
        if isinstance(raw, dict):
            for key in raw.keys():
                name = str(key or '').strip()
                if name:
                    names.add(name)
        elif isinstance(raw, list):
            for entry in raw:
                if isinstance(entry, str):
                    name = entry.strip()
                elif isinstance(entry, dict):
                    name = str(entry.get('name') or '').strip()
                else:
                    continue
                if name:
                    names.add(name)
        return names

    @staticmethod
    def _appeal_patch_structured_ownership_supports_self(item: dict, *, section: str, appeal_adventurer_id: int, expedition_evidence: dict, recent_activity: list | None) -> bool:
        """Validate typed create ownership without interpreting natural-language reasons."""
        if not isinstance(item, dict):
            return False
        owner_adventurer_id = _normalize_appeal_patch_integer(item.get('owner_adventurer_id'), minimum=1)
        if owner_adventurer_id is None:
            return False
        if owner_adventurer_id != int(appeal_adventurer_id):
            return False
        ownership_source = str(item.get('ownership_source') or '').strip()
        if ownership_source == 'narrative_event':
            expected_claim_kind = _NARRATIVE_CREATE_CLAIM_KINDS.get(section)
            if str(item.get('narrative_claim_kind') or '').strip() != expected_claim_kind:
                return False
            events = expedition_evidence.get('all_events')
            indexes = item.get('evidence_event_indexes')
            if not isinstance(events, list) or not isinstance(indexes, list) or (not indexes):
                return False
            normalized_indexes = [_normalize_appeal_patch_integer(index, minimum=0) for index in indexes]
            return all((index is not None and index < len(events) for index in normalized_indexes))
        if ownership_source == 'recent_activity':
            item_name = str(item.get('name') or '').strip()
            if not item_name or not isinstance(recent_activity, list):
                return False
            return any((isinstance(row, dict) and str(row.get('item_name') or '').strip() == item_name and (str(row.get('change_type') or '').strip() == 'consume') and (str(row.get('source_type') or '').strip() == 'data_appeal_ai') for row in recent_activity))
        return False

    @staticmethod
    def _normalize_create_ownership_claim(item: dict, *, section: str) -> dict:
        """Preserve a complete typed ownership claim through backend normalization."""
        if not isinstance(item, dict):
            return {}
        owner_id = _normalize_appeal_patch_integer(item.get('owner_adventurer_id'), minimum=1)
        if owner_id is None:
            return {}
        source = item.get('ownership_source')
        if type(source) is not str:
            return {}
        source = source.strip()
        if owner_id <= 0 or source not in ('member_update', 'narrative_event', 'recent_activity'):
            return {}
        if 'evidence_event_indexes' not in item:
            if source == 'narrative_event':
                return {}
            raw_indexes = []
        else:
            raw_indexes = item.get('evidence_event_indexes')
            if not isinstance(raw_indexes, list):
                return {}
        indexes = []
        for index in raw_indexes:
            index = _normalize_appeal_patch_integer(index, minimum=0)
            if index is None:
                return {}
            if index not in indexes:
                indexes.append(index)
        if len(indexes) > 16:
            return {}
        if source == 'narrative_event' and (not indexes):
            return {}
        normalized = {'owner_adventurer_id': owner_id, 'ownership_source': source, 'evidence_event_indexes': indexes}
        if source == 'narrative_event':
            expected_claim_kind = _NARRATIVE_CREATE_CLAIM_KINDS.get(section)
            claim_kind = item.get('narrative_claim_kind')
            if type(claim_kind) is not str:
                return {}
            claim_kind = claim_kind.strip()
            if not expected_claim_kind or claim_kind != expected_claim_kind:
                return {}
            normalized['narrative_claim_kind'] = claim_kind
        return normalized

    @staticmethod
    def _appeal_party_member_names(party_roster: list | None, expedition_evidence: dict | None) -> set[str]:
        names: set[str] = set()
        for row in party_roster or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get('name') or '').strip()
            if name:
                names.add(name)
        if isinstance(expedition_evidence, dict):
            mapping = expedition_evidence.get('member_id_to_name')
            if isinstance(mapping, dict):
                for raw_name in mapping.values():
                    name = str(raw_name or '').strip()
                    if name:
                        names.add(name)
        return names

    @staticmethod
    def _create_item_is_class_scroll_compensation(item: dict, evidence: dict) -> bool:
        if not isinstance(item, dict) or not isinstance(evidence, dict):
            return False
        item_type = str(item.get('type') or '').strip()
        item_name = str(item.get('name') or '').strip()
        if item_type != 'scroll' or '技能卷轴' not in item_name:
            return False
        member_update = evidence.get('member_update')
        if not isinstance(member_update, dict):
            return False
        new_class = str(member_update.get('new_class') or '').strip()
        if not new_class:
            return False
        new_skills = member_update.get('new_skills')
        if isinstance(new_skills, dict) and new_skills:
            return False
        return True

    @staticmethod
    def _create_item_ai_desc(item: dict) -> str:
        for field in ('description', 'custom_desc'):
            value = item.get(field)
            if type(value) is str and value.strip():
                return value.strip()
        return ''

    @staticmethod
    def _resolve_expedition_log_id_for_appeal(appeal_row, expedition_evidence: dict | None) -> int | None:
        if appeal_row is not None:
            try:
                log_id = int(appeal_row.expedition_log_id or 0)
                if log_id > 0:
                    return log_id
            except (TypeError, ValueError):
                pass
        if isinstance(expedition_evidence, dict):
            try:
                log_id = int(expedition_evidence.get('expedition_log_id') or 0)
                if log_id > 0:
                    return log_id
            except (TypeError, ValueError):
                pass
        return None

    @staticmethod
    def _lookup_expedition_settlement_item_meta_from_log(log, adventurer_id: int, item_name: str) -> dict | None:
        clean_name = str(item_name or '').strip()
        if not clean_name or not log or (not isinstance(log.full_log, dict)):
            return None
        from plugins.adventurer_s1.services.adventurer_s1_parser_service import normalize_s1_new_equipment_payload
        from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix
        lookup_name, _ = split_stack_suffix(clean_name, 1)
        for update in log.full_log.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            try:
                if int(update.get('id')) != int(adventurer_id):
                    continue
            except (TypeError, ValueError):
                continue
            new_eqs = normalize_s1_new_equipment_payload(update.get('new_equipments')) or {}
            if lookup_name in new_eqs:
                return dict(new_eqs[lookup_name])
            if clean_name in new_eqs:
                return dict(new_eqs[clean_name])
        return None

    @staticmethod
    def _lookup_expedition_settlement_item_meta(adventurer_id: int, expedition_log_id: int | None, item_name: str) -> dict | None:
        """从探险 full_log 读取 member_update.new_equipments 完整元数据。

        申诉工单 expedition_evidence 为省 token 已压缩 new_equipments（仅 rarity），
        结算漏发 create_items 落库时必须回到 full_log 取 type/desc。
        """
        clean_name = str(item_name or '').strip()
        if not clean_name or not expedition_log_id:
            return None
        log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=int(expedition_log_id)).first()
        return AdventurerS1DataAppealService._lookup_expedition_settlement_item_meta_from_log(log, int(adventurer_id), clean_name)

    @staticmethod
    def _lookup_consumed_departure_item_meta_from_log(log, adventurer_id: int, item_name: str) -> dict | None:
        """Return the frozen pre-expedition row for an item settlement consumed.

        A restore must recreate the player's old instance, including a distinct
        custom description.  The create-items AI description is only a proposal
        and must not replace this earlier authoritative snapshot.
        """
        clean_name = str(item_name or '').strip()
        if not clean_name or not log or (not isinstance(log.full_log, dict)):
            return None
        from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix
        lookup_name, _ = split_stack_suffix(clean_name, 1)
        member_update = AdventurerS1DataAppealService._pick_player_member_update(log.full_log, int(adventurer_id)) or {}
        consumed_names = AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('consumed_equipments'))
        if clean_name not in consumed_names and lookup_name not in consumed_names:
            return None
        for snapshot in log.full_log.get('audit_member_snapshot') or []:
            if not isinstance(snapshot, dict):
                continue
            try:
                if int(snapshot.get('adventurer_id')) != int(adventurer_id):
                    continue
            except (TypeError, ValueError):
                continue
            for item in snapshot.get('equipment') or []:
                if not isinstance(item, dict):
                    continue
                snapshot_name, _ = split_stack_suffix(str(item.get('name') or ''), 1)
                if snapshot_name not in (clean_name, lookup_name):
                    continue
                return dict(item)
            return None
        return None

    @staticmethod
    def _lookup_expedition_settlement_item_quantity(adventurer_id: int, expedition_log_id: int | None, item_name: str) -> int | None:
        """Return how many matching items this member actually received in settlement."""
        clean_name = str(item_name or '').strip()
        if not clean_name or not expedition_log_id:
            return None
        from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix
        log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=int(expedition_log_id)).first()
        if not log or not isinstance(log.full_log, dict):
            return None
        lookup_name, _ = split_stack_suffix(clean_name, 1)
        total = 0

        def _matches(raw_name: object, raw_quantity: object=1) -> int:
            name, quantity = split_stack_suffix(str(raw_name or ''), raw_quantity)
            if name and name in (clean_name, lookup_name):
                return int(quantity or 1)
            return 0
        for update in log.full_log.get('member_updates') or []:
            if not isinstance(update, dict):
                continue
            try:
                if int(update.get('id')) != int(adventurer_id):
                    continue
            except (TypeError, ValueError):
                continue
            new_equipments = update.get('new_equipments')
            if isinstance(new_equipments, dict):
                for raw_name, data in new_equipments.items():
                    raw_quantity = data.get('quantity', 1) if isinstance(data, dict) else 1
                    total += _matches(raw_name, raw_quantity)
            break
        return total

    @staticmethod
    def _should_allow_expedition_rarity_increase(raw_patch: dict | None, snapshot_before: dict, adventurer_id: int | None, expedition_log_id: int | None) -> bool:
        """patch 拟升稀有度且探险 full_log 有更高稀有度结算记录时放行。"""
        if not isinstance(raw_patch, dict) or not adventurer_id or (not expedition_log_id):
            return False
        if not isinstance(snapshot_before, dict):
            return False
        record_maps: dict[tuple[str, int], dict] = {}
        for section in ('equipment', 'warehouse'):
            for rec in snapshot_before.get(section) or []:
                if not isinstance(rec, dict) or rec.get('id') is None:
                    continue
                try:
                    record_maps[section, int(rec['id'])] = rec
                except (TypeError, ValueError):
                    continue
        for section in ('equipment', 'warehouse'):
            sec = raw_patch.get(section)
            if not isinstance(sec, dict):
                continue
            for upd in sec.get('update') or []:
                if not isinstance(upd, dict) or 'rarity' not in upd or upd.get('id') is None:
                    continue
                rid = _normalize_appeal_patch_integer(upd['id'], minimum=1)
                if rid is None:
                    continue
                original = record_maps.get((section, rid))
                if not original:
                    continue
                item_name = str(original.get('name') or '').strip()
                if not item_name:
                    continue
                meta = AdventurerS1DataAppealService._lookup_expedition_settlement_item_meta(int(adventurer_id), int(expedition_log_id), item_name)
                if not meta:
                    continue
                meta_r = str(meta.get('rarity') or '').strip().lower()
                target_r = str(upd.get('rarity') or meta_r or '').strip().lower()
                old_r = str(original.get('rarity') or 'common').strip().lower()
                if meta_r in _VALID_RARITIES and _RARITY_RANK.get(meta_r, 0) > _RARITY_RANK.get(old_r, 0):
                    if target_r in _VALID_RARITIES and _RARITY_RANK.get(target_r, 0) >= _RARITY_RANK.get(meta_r, 0):
                        return True
        for item in raw_patch.get('create_items') or []:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get('name') or '').strip()
            if not item_name:
                continue
            meta = AdventurerS1DataAppealService._lookup_expedition_settlement_item_meta(int(adventurer_id), int(expedition_log_id), item_name)
            if not meta:
                continue
            meta_r = str(meta.get('rarity') or '').strip().lower()
            if meta_r not in _VALID_RARITIES:
                continue
            siblings = [rec for section in ('equipment', 'warehouse') for rec in snapshot_before.get(section) or [] if isinstance(rec, dict) and str(rec.get('name') or '').strip() == item_name]
            if siblings and max((_RARITY_RANK.get(str(rec.get('rarity') or 'common').strip().lower(), 0) for rec in siblings)) < _RARITY_RANK.get(meta_r, 0):
                return True
        return False

    @staticmethod
    def _patch_has_item_record_benefit(patch: dict | None) -> bool:
        """patch 是否包含物品补发/稀有度或数量归位（不含纯描述整理）。"""
        if not isinstance(patch, dict):
            return False
        if patch.get('create_items') or patch.get('reclaim_items'):
            return True
        for section in ('equipment', 'warehouse'):
            sec = patch.get(section)
            if not isinstance(sec, dict):
                continue
            for upd in sec.get('update') or []:
                if isinstance(upd, dict) and any((k in upd for k in ('rarity', 'quantity'))):
                    return True
        return False

    @staticmethod
    def _build_mechanical_rarity_upgrade_supplement_patch(snapshot_before: dict, *, adventurer_id: int, expedition_log_id: int | None) -> dict | None:
        """探险结算漏升稀有度：快照已有同名物，full_log 记录更高稀有度 → update。"""
        if not expedition_log_id or not isinstance(snapshot_before, dict):
            return None
        supplement: dict = {'traits': {'update': [], 'delete': []}, 'equipment': {'update': [], 'delete': []}, 'warehouse': {'update': [], 'delete': []}, 'skills': {'update': [], 'delete': []}}
        for section in ('equipment', 'warehouse'):
            for rec in snapshot_before.get(section) or []:
                if not isinstance(rec, dict) or rec.get('id') is None:
                    continue
                name = str(rec.get('name') or '').strip()
                if not name:
                    continue
                meta = AdventurerS1DataAppealService._lookup_expedition_settlement_item_meta(int(adventurer_id), int(expedition_log_id), name)
                if not meta:
                    continue
                new_r = str(meta.get('rarity') or '').strip().lower()
                old_r = str(rec.get('rarity') or 'common').strip().lower()
                if new_r not in _VALID_RARITIES or _RARITY_RANK.get(new_r, 0) <= _RARITY_RANK.get(old_r, 0):
                    continue
                try:
                    rid = int(rec['id'])
                except (TypeError, ValueError):
                    continue
                upd: dict = {'id': rid, 'rarity': new_r}
                meta_desc = str(meta.get('desc') or meta.get('description') or '').strip()
                old_desc = str(rec.get('custom_desc') or rec.get('description') or '').strip()
                if meta_desc and meta_desc != old_desc:
                    upd['custom_desc'] = meta_desc
                supplement[section]['update'].append(upd)
        if not any((supplement[s].get('update') or [] for s in ('equipment', 'warehouse'))):
            return None
        return supplement

    @staticmethod
    def _catalog_is_appeal_empty_shell(catalog) -> bool:
        """申诉误建的图鉴空壳：common 且无通用描述。"""
        if catalog is None:
            return False
        catalog_rarity = str(getattr(catalog, 'rarity', None) or 'common').strip().lower()
        catalog_desc = str(getattr(catalog, 'description', None) or '').strip()
        return catalog_rarity == 'common' and (not catalog_desc)

    @staticmethod
    def _resolve_create_item_grant_fields(item: dict, *, settlement_meta: dict | None, catalog, use_ai_desc: bool) -> tuple[str, str, str, bool]:
        """合并 patch / 探险结算 / 图鉴，得到 grant_type/rarity/desc 与是否图鉴溯源。"""
        ai_desc = AdventurerS1DataAppealService._create_item_ai_desc(item)
        return (str(item.get('type') or getattr(catalog, 'type', '') or '').strip(), str(item.get('rarity') or getattr(catalog, 'rarity', 'common') or 'common').strip(), ai_desc or str(getattr(catalog, 'description', '') or '').strip(), False)

    @staticmethod
    def _create_item_grant_will_have_visible_description(item: dict, *, catalog, settlement_meta: dict | None, use_ai_desc: bool) -> bool:
        """补发落库后玩家是否能看到非空描述（custom_desc 或图鉴通用描述）。"""
        _, _, grant_desc, _ = AdventurerS1DataAppealService._resolve_create_item_grant_fields(item, settlement_meta=settlement_meta, catalog=catalog, use_ai_desc=use_ai_desc)
        if grant_desc:
            return True
        if catalog is not None:
            catalog_desc = str(getattr(catalog, 'description', None) or '').strip()
            if catalog_desc and (not AdventurerS1DataAppealService._catalog_is_appeal_empty_shell(catalog)):
                return True
        return False

    @staticmethod
    def audit_create_item_description_landing(normalized_patch: dict, *, adventurer_id: int, expedition_evidence: dict | None, snapshot_before: dict | None, appeal_row=None) -> list[dict]:
        """create_items 补发后是否仍有可展示描述（交二审 supplement 或落库前拦截）。"""
        if not isinstance(normalized_patch, dict):
            return []
        issues: list[dict] = []
        seen_names: set[str] = set()
        expedition_log_id = AdventurerS1DataAppealService._resolve_expedition_log_id_for_appeal(appeal_row, expedition_evidence)
        for item_index, item in enumerate(normalized_patch.get('create_items') or []):
            if not isinstance(item, dict):
                continue
            name = str(item.get('name') or '').strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            catalog = db_manager.query(AdventurerS1Item).filter(AdventurerS1Item.name == name).first()
            settlement_meta = AdventurerS1DataAppealService._lookup_expedition_settlement_item_meta(adventurer_id, expedition_log_id, name)
            use_ai_desc = AdventurerS1DataAppealService._should_apply_ai_custom_desc_for_catalog_create(item, catalog, snapshot_before=snapshot_before, expedition_evidence=expedition_evidence)
            if AdventurerS1DataAppealService._create_item_grant_will_have_visible_description(item, catalog=catalog, settlement_meta=settlement_meta, use_ai_desc=use_ai_desc):
                continue
            issues.append({'code': 'create_item_missing_resolvable_description', 'section': 'create_items', 'item_index': item_index, 'field': 'description', 'name': name, 'detail': f'create_items「{name}」补发后仍无可展示描述（非图鉴还原且无法从探险结算回填）'})
        return issues

    @staticmethod
    def apply_create_item_description_audit(ai_raw: dict, issues: list[dict]) -> dict:
        if not issues:
            return ai_raw
        result = dict(ai_raw)
        result['core_request_satisfied'] = False
        warnings = list(result.get('warnings') or [])
        for issue in issues:
            warnings.append(f"create_item_description_audit:{issue.get('code')}:{issue.get('detail')}")
        if warnings:
            result['warnings'] = warnings
        result['create_item_description_audit'] = issues
        msg = '拟补发的物品缺少可展示描述（非图鉴已有物还原，且无法从探险结算自动回填）；需补写物品说明后才能落库。'
        existing = AdventurerS1DataAppealService._normalize_player_reject_reason(result.get('player_reject_reason') or result.get('reject_reason_for_player'))
        if AdventurerS1DataAppealService._looks_like_success_reason(existing):
            result['player_reject_reason'] = msg
        elif msg not in existing:
            result['player_reject_reason'] = f'{existing} {msg}'.strip() if existing else msg
        return result

    @staticmethod
    def _strip_unlandable_create_items(patch: dict | None, *, appeal: AdventurerS1DataAppeal) -> tuple[dict, list[dict]]:
        """执行前剔除已被结构化审计确认无法落库的 create_items。

        这里只消费 ``audit_create_item_description_landing`` 的结构化结果，不读取
        剧情/摘要关键词。其它独立 section 继续执行，避免一个空描述物品连带回滚
        已获准的补贴或普通修复。
        """
        cleaned = copy.deepcopy(patch) if isinstance(patch, dict) else {}
        issues = AdventurerS1DataAppealService.audit_create_item_description_landing(cleaned, adventurer_id=appeal.adventurer_id, expedition_evidence=appeal.expedition_evidence if isinstance(appeal.expedition_evidence, dict) else None, snapshot_before=appeal.snapshot_before if isinstance(appeal.snapshot_before, dict) else {}, appeal_row=appeal)
        if not issues:
            return (cleaned, [])
        blocked_names = {str(issue.get('name') or '').strip() for issue in issues if str(issue.get('name') or '').strip()}
        kept = [item for item in cleaned.get('create_items') or [] if not isinstance(item, dict) or str(item.get('name') or '').strip() not in blocked_names]
        if kept:
            cleaned['create_items'] = kept
        else:
            cleaned.pop('create_items', None)
        return (cleaned, issues)

    @staticmethod
    def _should_apply_ai_custom_desc_for_catalog_create(item: dict, catalog, *, snapshot_before: dict | None, expedition_evidence: dict | None) -> bool:
        """图鉴物 create_items：纯还原用图鉴描述；同名升级/进化且 AI 描述不同则用 AI custom_desc。"""
        ai_desc = AdventurerS1DataAppealService._create_item_ai_desc(item)
        if not ai_desc or catalog is None:
            return False
        catalog_desc = str(getattr(catalog, 'description', None) or '').strip()
        if ai_desc == catalog_desc:
            return False
        if AdventurerS1DataAppealService._create_item_is_self_upgrade_replacement(item, expedition_evidence or {}):
            return True
        from plugins.adventurer_s1.services.s1_appeal_lineage_guard import _snapshot_siblings_for_guard, is_distinct_desc_upgrade
        name = str(item.get('name') or '').strip()
        if not name or not isinstance(snapshot_before, dict):
            return False
        siblings = _snapshot_siblings_for_guard(snapshot_before, name)
        return is_distinct_desc_upgrade(item, siblings)

    @staticmethod
    def _create_item_is_self_upgrade_replacement(item: dict, evidence: dict) -> bool:
        if not isinstance(item, dict) or not isinstance(evidence, dict):
            return False
        item_name = str(item.get('name') or '').strip()
        if not item_name:
            return False
        member_update = evidence.get('member_update')
        if isinstance(member_update, dict):
            consumed_names = AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('consumed_equipments'))
            if item_name in consumed_names:
                return True
        return False

    @staticmethod
    def _expedition_evidence_self_supports_create(section: str, item: dict, evidence: dict | None) -> bool:
        if not isinstance(item, dict) or not isinstance(evidence, dict):
            return False
        name = str(item.get('name') or '').strip()
        if not name:
            return False
        member_update = evidence.get('member_update') if isinstance(evidence.get('member_update'), dict) else {}
        if section == 'create_items':
            names = AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('new_equipments'))
            names.update(AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('consumed_equipments')))
            return name in names
        if section == 'create_traits':
            return name in AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('new_traits'))
        if section == 'create_skills':
            return name in AdventurerS1DataAppealService._appeal_patch_name_set(member_update.get('new_skills'))
        return False

    @staticmethod
    def _expedition_evidence_other_member_has_item(item_name: str, evidence: dict | None, appeal_adventurer_id: int) -> bool:
        if not item_name or not isinstance(evidence, dict):
            return False
        summary = evidence.get('party_member_updates_summary')
        if not isinstance(summary, list):
            return False
        for row in summary:
            if not isinstance(row, dict):
                continue
            try:
                aid = int(row.get('adventurer_id'))
            except (TypeError, ValueError):
                continue
            if aid == int(appeal_adventurer_id):
                continue
            names = AdventurerS1DataAppealService._appeal_patch_name_set(row.get('new_equipments'))
            if item_name in names:
                return True
        return False

    @staticmethod
    def _departure_resource_owner_ids(resource_name: str, evidence: dict | None, *, ownership_key: str, normalize_stack_name: bool=False) -> set[int]:
        """Return frozen departure owners for one exact equipment, trait, or skill name."""
        clean_name = str(resource_name or '').strip()
        if not clean_name or not isinstance(evidence, dict):
            return set()
        AdventurerS1DataAppealService._ensure_departure_equipment_ownership(evidence)
        lookup_name = clean_name
        split_stack_suffix = None
        if normalize_stack_name:
            from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix
            lookup_name, _ = split_stack_suffix(clean_name, 1)
        owner_ids: set[int] = set()
        for row in evidence.get(ownership_key) or []:
            if not isinstance(row, dict):
                continue
            row_name = str(row.get('name') or '').strip()
            if split_stack_suffix is not None:
                row_name, _ = split_stack_suffix(row_name, 1)
            if row_name != lookup_name:
                continue
            try:
                owner_ids.add(int(row.get('owner_adventurer_id')))
            except (TypeError, ValueError):
                continue
        return owner_ids

    @staticmethod
    def _strip_teammate_departure_resource_creates(raw_patch: dict, *, appeal_adventurer_id: int, expedition_evidence: dict | None) -> tuple[dict, list[dict]]:
        """Prevent narrative claims from cloning resources frozen under a teammate's ownership.

        A permanent ownership correction must use reclaim_items, whose executor removes the source
        equipment before granting the appellant. Skills and traits cannot be transferred at all.
        """
        if not isinstance(raw_patch, dict) or not isinstance(expedition_evidence, dict):
            return (raw_patch, [])
        return (raw_patch, [])

    @staticmethod
    def strip_unsupported_expedition_self_grants(raw_patch: dict, *, appeal_adventurer: dict | None, expedition_evidence: dict | None, party_roster: list | None=None, recent_activity: list | None=None) -> tuple[dict, list[dict]]:
        """Require expedition create_* grants to be attributable to the appellant.

        Mechanical support is either an appellant member_update fact or typed ownership evidence
        pointing to the appellant. Natural-language name occurrence is never ownership evidence.
        If a requested item name exactly equals any party member name, narrative-only ownership is
        rejected; only an actual settlement fact or a verified prior appeal removal can establish
        that a separate same-named asset exists. Stripped rows are recorded for review diagnostics.
        """
        return (raw_patch, [])

    @staticmethod
    def _append_unsupported_expedition_grant_warnings(raw: dict, removed: list[dict]) -> None:
        if not isinstance(raw, dict) or not removed:
            return
        warnings = raw.get('warnings') if isinstance(raw.get('warnings'), list) else []
        stripped_grants: list[dict] = []
        for item in removed:
            section = item.get('section') or 'patch'
            name = item.get('name') or '未命名条目'
            code = item.get('reason') or 'missing_appellant_ownership_evidence'
            warnings.append(f'后端已剔除 {section}「{name}」：探险普通补发缺少申诉者本人归属证明（{code}）。')
            stripped_grants.append({'section': section, 'name': name, 'code': code})
        raw['warnings'] = warnings
        if stripped_grants:
            raw['ownership_stripped_grants'] = stripped_grants

    @staticmethod
    def _summarize_team_pool_for_grant(raw) -> dict | None:
        if not isinstance(raw, dict):
            return None
        try:
            total = round(float(raw.get('total_amount')), 2)
        except (TypeError, ValueError):
            return None
        if total <= 0:
            return None
        out = {'total_amount': total}
        reason = str(raw.get('reason') or '').strip()
        if reason:
            out['reason'] = reason
        return out

    @staticmethod
    def _iter_tavern_subsidies(raw) -> list[dict]:
        """兼容旧单对象与新版 team/individual 双对象数组。"""
        if isinstance(raw, dict):
            return [raw]
        if isinstance(raw, list):
            return [row for row in raw if isinstance(row, dict)]
        return []

    @staticmethod
    def _upsert_tavern_subsidy_summary(existing, summary: dict):
        """按 scope 保留一份摘要，避免历史增量审计制造重复 scope。"""
        rows = list(existing) if isinstance(existing, list) else [existing] if isinstance(existing, dict) else []
        scope = summary.get('scope')
        for index, row in enumerate(rows):
            if isinstance(row, dict) and row.get('scope') == scope:
                rows[index] = summary
                break
        else:
            rows.append(summary)
        return rows[0] if len(rows) == 1 else rows

    @staticmethod
    def _summarize_tavern_subsidy_for_grant(raw) -> dict | list | None:
        if isinstance(raw, list):
            summaries = [AdventurerS1DataAppealService._summarize_tavern_subsidy_for_grant(row) for row in raw if isinstance(row, dict)]
            summaries = [row for row in summaries if isinstance(row, dict)]
            if not summaries:
                return None
            return summaries[0] if len(summaries) == 1 else summaries
        if not isinstance(raw, dict):
            return None
        scope = str(raw.get('scope') or 'individual').strip().lower()
        scope = 'team' if scope == 'team' else 'individual'
        reason = str(raw.get('reason') or '').strip()
        raw_allocs = raw.get('allocations')
        if scope == 'team' and isinstance(raw_allocs, list) and raw_allocs:
            allocations = []
            for alloc in raw_allocs:
                if not isinstance(alloc, dict):
                    continue
                try:
                    aid = int(alloc.get('adventurer_id'))
                    amt = round(float(alloc.get('amount')), 2)
                except (TypeError, ValueError):
                    continue
                if amt <= 0:
                    continue
                item = {'adventurer_id': aid, 'amount': amt}
                alloc_reason = str(alloc.get('reason') or '').strip()
                if alloc_reason:
                    item['reason'] = alloc_reason
                allocations.append(item)
            if allocations:
                out = {'scope': 'team', 'allocations': allocations}
                if reason:
                    out['reason'] = reason
                return out
        total = raw.get('total_amount')
        if total is None:
            total = raw.get('gold')
        if total is None:
            total = raw.get('amount')
        try:
            total_amount = round(float(total), 2)
        except (TypeError, ValueError):
            return None
        if total_amount <= 0:
            return None
        out = {'scope': scope, 'total_amount': total_amount}
        if reason:
            out['reason'] = reason
        return out

    @staticmethod
    def _build_tavern_subsidy_status(expedition_log_id) -> dict:
        """同探险酒馆补贴的历史最高目标额（团队与个人独立）。"""
        if not expedition_log_id:
            return {}
        log_id = int(expedition_log_id)
        team_max = AdventurerS1DataAppealService._tavern_subsidy_team_maxima(log_id)
        individual_max = AdventurerS1DataAppealService._tavern_subsidy_individual_maxima(log_id)
        out = {}
        if team_max:
            out['team_max_amount_by_adventurer_id'] = team_max
        if individual_max:
            out['individual_max_amount_by_adventurer_id'] = individual_max
        return out

    @staticmethod
    def _build_prior_appeal_grants(expedition_log_id, exclude_appeal_id) -> list:
        """同一次探险（expedition_log_id）下，其他成员既往申诉已判给的收益清单（软参考）。

        - approved 取审计台账（权威、已落库、未还原）；processing / 历史
          awaiting_review 取 proposed_patch（在途待定）。
        - 为兼容现有 AI 输入合同，两种在途存储状态都投影为
          ``awaiting_review``；这只表示未终局，不代表进入管理员审核。
        - 仅回显去重所需最小字段；纯内部上下文，绝不外露给玩家。
        - 批量查询（兄弟单 / 冒险者名 / 审计）避免 N+1。
        """
        if not expedition_log_id:
            return []
        try:
            log_id = int(expedition_log_id)
            exclude_id = int(exclude_appeal_id)
        except (TypeError, ValueError):
            return []
        siblings = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.expedition_log_id == log_id, AdventurerS1DataAppeal.id != exclude_id, AdventurerS1DataAppeal.status.in_(('approved', 'processing', 'awaiting_review'))).order_by(AdventurerS1DataAppeal.id.asc()).all()
        if not siblings:
            return []
        adv_ids = {s.adventurer_id for s in siblings}
        adv_name_map = {a.id: a.name for a in db_manager.query(AdventurerS1).filter(AdventurerS1.id.in_(adv_ids)).all()} if adv_ids else {}
        approved_ids = [s.id for s in siblings if s.status == 'approved']
        audit_by_appeal: dict = {}
        if approved_ids:
            rows = db_manager.query(AdventurerS1DataAppealAuditLog).filter(AdventurerS1DataAppealAuditLog.appeal_id.in_(approved_ids), AdventurerS1DataAppealAuditLog.reverted.is_(False), AdventurerS1DataAppealAuditLog.op_type.in_(('create_item', 'create_trait', 'create_skill', 'currency', 'team_pool', 'tavern_subsidy_team', 'tavern_subsidy_self'))).all()
            for r in rows:
                bucket = audit_by_appeal.setdefault(r.appeal_id, {'items': [], 'traits': [], 'skills': [], 'gold': 0.0, 'team_pool': None, 'tavern_subsidy': None})
                after = r.after_json if isinstance(r.after_json, dict) else {}
                name = str(after.get('name') or '').strip()
                if r.op_type == 'create_item' and name:
                    bucket['items'].append({'name': name, 'quantity': int(after.get('quantity') or 1)})
                elif r.op_type == 'create_trait' and name:
                    bucket['traits'].append(name)
                elif r.op_type == 'create_skill' and name:
                    bucket['skills'].append(name)
                elif r.op_type == 'team_pool':
                    bucket['team_pool'] = {'total_amount': round(float(after.get('total_amount') or 0), 2), 'per_member': round(float(after.get('per_member') or 0), 2)}
                elif r.op_type == 'tavern_subsidy_team':
                    summary = {'scope': 'team', 'total_amount': round(float(after.get('total_amount') or 0), 2)}
                    mode = str(after.get('mode') or '').strip()
                    if mode:
                        summary['mode'] = mode
                    allocations = after.get('allocations')
                    if isinstance(allocations, list) and allocations:
                        summary['allocations'] = allocations
                    bucket['tavern_subsidy'] = AdventurerS1DataAppealService._upsert_tavern_subsidy_summary(bucket.get('tavern_subsidy'), summary)
                elif r.op_type == 'tavern_subsidy_self':
                    summary = {'scope': 'individual', 'total_amount': round(float(after.get('total_amount') or 0), 2)}
                    bucket['tavern_subsidy'] = AdventurerS1DataAppealService._upsert_tavern_subsidy_summary(bucket.get('tavern_subsidy'), summary)
                elif r.op_type == 'currency':
                    if after.get('tavern_subsidy') or after.get('team_pool'):
                        continue
                    try:
                        bucket['gold'] += float(after.get('amount') or 0)
                    except (TypeError, ValueError):
                        pass

        def _grants_from_patch(patch) -> dict:
            grants = {'items': [], 'traits': [], 'skills': [], 'gold': 0.0, 'team_pool': None, 'tavern_subsidy': None}
            if not isinstance(patch, dict):
                return grants
            for it in patch.get('create_items') or []:
                name = str((it or {}).get('name') or '').strip()
                if name:
                    grants['items'].append({'name': name, 'quantity': int((it or {}).get('quantity') or 1)})
            for it in patch.get('create_traits') or []:
                name = str((it or {}).get('name') or '').strip()
                if name:
                    grants['traits'].append(name)
            for it in patch.get('create_skills') or []:
                name = str((it or {}).get('name') or '').strip()
                if name:
                    grants['skills'].append(name)
            for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
                for action in patch.get(section) or []:
                    if not isinstance(action, dict):
                        continue
                    target = action.get('target')
                    if not isinstance(target, dict) or target.get('id') is not None:
                        continue
                    name = str(target.get('name') or '').strip()
                    target_kind = destructive_action_target_kind(str(action.get('mode') or ''))
                    if name and target_kind == 'trait':
                        grants['traits'].append(name)
                    elif name and target_kind == 'skill':
                        grants['skills'].append(name)
            for it in patch.get('currency_adjustments') or []:
                try:
                    grants['gold'] += float((it or {}).get('amount') or 0)
                except (TypeError, ValueError):
                    pass
            grants['team_pool'] = AdventurerS1DataAppealService._summarize_team_pool_for_grant(patch.get('team_pool'))
            grants['tavern_subsidy'] = AdventurerS1DataAppealService._summarize_tavern_subsidy_for_grant(patch.get('tavern_subsidy'))
            return grants
        out = []
        for s in siblings:
            if s.status == 'approved':
                grants = audit_by_appeal.get(s.id, {'items': [], 'traits': [], 'skills': [], 'gold': 0.0, 'team_pool': None, 'tavern_subsidy': None})
            else:
                grants = _grants_from_patch(s.proposed_patch)
            if not (grants['items'] or grants['traits'] or grants['skills'] or grants['gold'] or grants.get('team_pool') or grants.get('tavern_subsidy')):
                continue
            entry = {'adventurer_name': adv_name_map.get(s.adventurer_id, '某成员'), 'status': 'approved' if s.status == 'approved' else 'awaiting_review'}
            if grants['items']:
                entry['granted_items'] = grants['items']
            if grants['traits']:
                entry['granted_traits'] = grants['traits']
            if grants['skills']:
                entry['granted_skills'] = grants['skills']
            if grants['gold']:
                entry['granted_gold'] = round(grants['gold'], 2)
            if grants.get('team_pool'):
                entry['granted_team_pool'] = grants['team_pool']
            if grants.get('tavern_subsidy'):
                entry['granted_tavern_subsidy'] = grants['tavern_subsidy']
            out.append(entry)
        return out

    @staticmethod
    def _prior_grants_without_tavern_subsidy(rows: list) -> list:
        """当前普通申诉 AI 不再裁定酒馆补贴，避免把历史补贴账本继续注入模型。"""
        return [{key: value for key, value in row.items() if key != 'granted_tavern_subsidy'} for row in rows or [] if isinstance(row, dict)]

    @staticmethod
    def _build_party_roster(expedition_log_id) -> list:
        """探险参与者花名册（按 log 的 member_updates）：用于团队池均分与 AI 上下文。

        返回 [{adventurer_id, name, owner_id, alive, gold_change}]；alive 排除 dead/archived。
        """
        if not expedition_log_id:
            return []
        log = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == int(expedition_log_id)).first()
        if not log:
            return []
        from plugins.adventurer_s1.services.adventurer_s1_log_service import build_public_s1_full_log
        public_full_log = build_public_s1_full_log(log.full_log if isinstance(log.full_log, dict) else {})
        id_to_gold: dict = {}
        for mu in public_full_log.get('member_updates') or []:
            if not isinstance(mu, dict):
                continue
            try:
                aid = int(mu.get('id'))
            except (TypeError, ValueError):
                continue
            try:
                id_to_gold[aid] = float(mu.get('gold_change') or 0)
            except (TypeError, ValueError):
                id_to_gold[aid] = 0.0
        if not id_to_gold:
            return []
        excluded = set(AdventurerS1.TAVERN_EXCLUDED_STATUSES)
        advs = db_manager.query(AdventurerS1).filter(AdventurerS1.id.in_(list(id_to_gold.keys()))).all()
        return [{'adventurer_id': a.id, 'name': a.name, 'owner_id': a.owner_id, 'alive': a.status not in excluded, 'gold_change': round(id_to_gold.get(a.id, 0.0), 2)} for a in advs]

    @staticmethod
    def _is_team_pool_settled(expedition_log_id) -> bool:
        """同一探险是否已分发过团队池（存在任一未还原的 team_pool 标记）。"""
        if not expedition_log_id:
            return False
        row = db_manager.query(AdventurerS1DataAppealAuditLog.id).join(AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id).filter(AdventurerS1DataAppeal.expedition_log_id == int(expedition_log_id), AdventurerS1DataAppealAuditLog.op_type == 'team_pool', AdventurerS1DataAppealAuditLog.reverted.is_(False)).first()
        return row is not None

    @staticmethod
    def _apply_team_pool_distribution(appeal: AdventurerS1DataAppeal, pool: dict | None=None) -> tuple:
        """把 team_pool 按存活参与者人均分发（只加不没收），逐人写审计 + 团队池标记。

        pool 为 None 时回退读 proposed_patch.team_pool（向后兼容 approve 路径）；显式传入时
        用于陪审团 2 号补充来源。幂等：同探险已结算则跳过；无 log / 无存活参与者 / 人均≤0 均安全跳过。
        必须在 finalize / approve 的事务内调用，与 patch 同事务提交。
        """
        if pool is None:
            pool = (appeal.proposed_patch or {}).get('team_pool')
        if not isinstance(pool, dict):
            return (True, None)
        log_id = appeal.expedition_log_id
        if not log_id:
            return (True, None)
        if AdventurerS1DataAppealService._is_team_pool_settled(log_id):
            return (True, None)
        try:
            total = round(float(pool.get('total_amount')), 2)
        except (TypeError, ValueError):
            return (True, None)
        if total <= 0:
            return (True, None)
        roster = AdventurerS1DataAppealService._build_party_roster(log_id)
        alive = [m for m in roster if m.get('alive')]
        if not alive:
            return (True, None)
        per_member = round(total / len(alive), 2)
        if per_member <= 0:
            return (True, None)
        from sqlalchemy import func
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        base_seq = db_manager.query(func.max(AdventurerS1DataAppealAuditLog.op_seq)).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal.id).scalar()
        base_seq = (base_seq if base_seq is not None else -1) + 1
        audit_rows = []
        recipient_ids = []
        reason = str(pool.get('reason') or '').strip() or '团队池均分补偿'
        for offset, member in enumerate(alive):
            split_result = {}
            new_balance, currency_err = S1CurrencyService.add(member['adventurer_id'], 'gold', per_member, source_type='data_appeal_team_pool', related_id=appeal.id, related_type='data_appeal', description=f'随队探险补偿：{reason}', auto_commit=False, split_result=split_result)
            if currency_err:
                return (None, currency_err)
            audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=member['adventurer_id'], op_seq=base_seq + offset, op_type='currency', target_type='currency', target_id=None, before_json=None, after_json={'currency_code': 'gold', 'amount': per_member, 'balance_after': new_balance, 'gold_credited': split_result.get('gold_credited', per_member), 'low_consumption_credited': split_result.get('low_consumption_credited', 0.0), 'team_pool': True}, note='团队池均分'))
            recipient_ids.append(member['adventurer_id'])
        audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=appeal.adventurer_id, op_seq=base_seq + len(alive), op_type='team_pool', target_type='expedition', target_id=int(log_id), before_json=None, after_json={'expedition_log_id': int(log_id), 'total_amount': total, 'per_member': per_member, 'recipient_ids': recipient_ids}, note=f'团队池结算：总额 {total} / {len(alive)} 人 / 人均 {per_member}'))
        ok, audit_err = db_manager.add_all(audit_rows)
        if not ok:
            return (None, audit_err or '团队池审计写入失败')
        AdventurerS1DataAppealService._notify_team_pool_recipients(appeal, alive, per_member)
        return (True, None)

    @staticmethod
    def _notify_team_pool_recipients(appeal: AdventurerS1DataAppeal, recipients: list, per_member: float) -> None:
        """给非申诉发起人的受益队友发补偿到账通知（申诉者本人走常规审核通知）。"""
        from models.mailbox import Mailbox
        mails = []
        for member in recipients:
            owner_id = member.get('owner_id')
            if not owner_id or owner_id == appeal.user_id:
                continue
            mails.append(Mailbox(user_id=owner_id, title='深渊酒馆 · 随队探险补偿到账', content=f"您的冒险者「{member.get('name') or '队员'}」所在队伍的一次探险收益经核定后按队伍均分补发，本次到账 {per_member} 金币。", sender_id=None, sender_name='深渊酒馆'))
        if mails:
            db_manager.add_all(mails)

    @staticmethod
    def _next_audit_seq(appeal_id: int) -> int:
        """该申诉当前审计台账的下一个 op_seq（续号，支持同事务多次写入不冲突）。"""
        from sqlalchemy import func
        base_seq = db_manager.query(func.max(AdventurerS1DataAppealAuditLog.op_seq)).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal_id).scalar()
        return (base_seq if base_seq is not None else -1) + 1

    @staticmethod
    def _is_tavern_subsidy_settled_team(expedition_log_id) -> bool:
        """兼容旧调用：同一探险是否存在未撤回的团队补贴水位。"""
        return bool(AdventurerS1DataAppealService._tavern_subsidy_team_maxima(expedition_log_id))

    @staticmethod
    def _tavern_subsidy_marker_rows(expedition_log_id, op_type: str) -> list:
        if not expedition_log_id:
            return []
        return db_manager.query(AdventurerS1DataAppealAuditLog.adventurer_id, AdventurerS1DataAppealAuditLog.after_json).join(AdventurerS1DataAppeal, AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id).filter(AdventurerS1DataAppeal.expedition_log_id == int(expedition_log_id), AdventurerS1DataAppealAuditLog.op_type == op_type, AdventurerS1DataAppealAuditLog.reverted.is_(False)).all()

    @staticmethod
    def _tavern_subsidy_team_maxima(expedition_log_id) -> dict[int, float]:
        maxima: dict[int, float] = {}
        for _, after in AdventurerS1DataAppealService._tavern_subsidy_marker_rows(expedition_log_id, 'tavern_subsidy_team'):
            if not isinstance(after, dict):
                continue
            for allocation in after.get('allocations') or []:
                if not isinstance(allocation, dict):
                    continue
                try:
                    adventurer_id = int(allocation.get('adventurer_id'))
                    target_amount = round(float(allocation.get('target_amount', allocation.get('amount'))), 2)
                except (TypeError, ValueError):
                    continue
                if target_amount > maxima.get(adventurer_id, 0.0):
                    maxima[adventurer_id] = target_amount
        return maxima

    @staticmethod
    def _tavern_subsidy_individual_maxima(expedition_log_id) -> dict[int, float]:
        maxima: dict[int, float] = {}
        for adventurer_id, after in AdventurerS1DataAppealService._tavern_subsidy_marker_rows(expedition_log_id, 'tavern_subsidy_self'):
            if adventurer_id is None or not isinstance(after, dict):
                continue
            try:
                aid = int(adventurer_id)
                target_amount = round(float(after.get('target_amount', after.get('total_amount'))), 2)
            except (TypeError, ValueError):
                continue
            if target_amount > maxima.get(aid, 0.0):
                maxima[aid] = target_amount
        return maxima

    @staticmethod
    def _is_tavern_subsidy_settled_self(expedition_log_id, adventurer_id) -> bool:
        """兼容旧调用：该冒险者是否存在未撤回的个人补贴水位。"""
        if not adventurer_id:
            return False
        return int(adventurer_id) in AdventurerS1DataAppealService._tavern_subsidy_individual_maxima(expedition_log_id)

    @staticmethod
    def _apply_tavern_subsidy(appeal: AdventurerS1DataAppeal, subsidy: dict | None) -> tuple:
        """酒馆补贴落库：团队与个人各自按历史最高目标额补差。

        幂等：同类别的新目标额不高于历史最高额时不发；更高时只发差额。
        与 patch 同事务调用。补贴是酒馆出资的凭空补偿（非补打结算漏折现）。
        """
        if not isinstance(subsidy, dict):
            return (True, None)
        log_id = appeal.expedition_log_id
        if not log_id:
            return (True, None)
        locked_log = db_manager.query(AdventurerS1ExpeditionLog.id).filter(AdventurerS1ExpeditionLog.id == int(log_id)).with_for_update().first()
        if not locked_log:
            return (True, None)
        scope = str(subsidy.get('scope') or 'individual').strip().lower()
        reason = str(subsidy.get('reason') or '').strip() or '酒馆补贴'
        allocations = subsidy.get('allocations')
        has_allocations = scope == 'team' and isinstance(allocations, list) and bool(allocations)
        if has_allocations:
            total = 0.0
        else:
            try:
                total = round(float(subsidy.get('total_amount')), 2)
            except (TypeError, ValueError):
                return (True, None)
            if total <= 0:
                return (True, None)
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        is_relief = (appeal.appeal_category or '') == 'tavern_relief'
        currency_source_type = 's1_tavern_relief' if is_relief else 'data_appeal_tavern_subsidy'
        subsidy_label = '酒馆补贴申请' if is_relief else '酒馆补贴'
        if scope == 'team':
            if is_relief and has_allocations:
                roster = AdventurerS1DataAppealService._build_tavern_relief_roster([{'id': allocation.get('adventurer_id'), 'gold_change': 0} for allocation in allocations if isinstance(allocation, dict)])
            else:
                roster = AdventurerS1DataAppealService._build_party_roster(log_id)
            alive = [m for m in roster if m.get('alive')]
            if not alive:
                return (True, None)
            cap_pp = AdventurerS1DataAppealService._tavern_relief_policy()['cap_per_person'] if is_relief else AdventurerS1DataAppealService._tavern_subsidy_cap_per_person()
            plan = []
            if has_allocations:
                alive_by_id = {m['adventurer_id']: m for m in alive}
                seen_ids = set()
                for alloc in allocations:
                    if not isinstance(alloc, dict):
                        continue
                    try:
                        aid = int(alloc.get('adventurer_id'))
                    except (TypeError, ValueError):
                        continue
                    if aid in seen_ids:
                        continue
                    member = alive_by_id.get(aid)
                    if not member:
                        continue
                    try:
                        amt = round(float(alloc.get('amount') or 0), 2)
                    except (TypeError, ValueError):
                        continue
                    if amt <= 0:
                        continue
                    seen_ids.add(aid)
                    plan.append((member, amt))
            else:
                per_member = round(total / len(alive), 2)
                if per_member <= 0:
                    return (True, None)
                plan = [(m, per_member) for m in alive]
            if is_relief and False:
                cap_team = AdventurerS1DataAppealService._tavern_relief_policy()['cap_per_team']
                if round(sum((amount for _, amount in plan)), 2) > round(cap_team, 2):
                    return (None, '酒馆补贴申请团队目标金额超过后端上限')
            historical_maxima = AdventurerS1DataAppealService._tavern_subsidy_team_maxima(log_id)
            delta_plan = []
            for member, target_amount in plan:
                previous_max = historical_maxima.get(member['adventurer_id'], 0.0)
                delta = round(max(target_amount - previous_max, 0.0), 2)
                if delta > 0:
                    delta_plan.append((member, target_amount, previous_max, delta))
            if not delta_plan:
                return (True, None)
            base_seq = AdventurerS1DataAppealService._next_audit_seq(appeal.id)
            audit_rows = []
            recipients = []
            alloc_detail = []
            total_distributed = 0.0
            target_detail = []
            for offset, (member, target_amount, previous_max, delta) in enumerate(delta_plan):
                split_result = {}
                new_balance, currency_err = S1CurrencyService.add(member['adventurer_id'], 'gold', delta, source_type=currency_source_type, related_id=appeal.id, related_type='data_appeal', description=f'{subsidy_label}（随队）：{reason}', auto_commit=False, split_result=split_result)
                if currency_err:
                    return (None, currency_err)
                audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=member['adventurer_id'], op_seq=base_seq + offset, op_type='currency', target_type='currency', target_id=None, before_json=None, after_json={'currency_code': 'gold', 'amount': delta, 'balance_after': new_balance, 'gold_credited': split_result.get('gold_credited', delta), 'low_consumption_credited': split_result.get('low_consumption_credited', 0.0), 'tavern_subsidy': True, 'target_amount': target_amount, 'previous_max_amount': previous_max}, note=f'{subsidy_label}倾斜分配' if has_allocations else f'{subsidy_label}均分'))
                recipients.append({**member, '_amount': delta})
                alloc_detail.append({'adventurer_id': member['adventurer_id'], 'amount': delta, 'target_amount': target_amount, 'previous_max_amount': previous_max})
                target_detail.append({'adventurer_id': member['adventurer_id'], 'amount': target_amount})
                total_distributed += delta
            audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=appeal.adventurer_id, op_seq=base_seq + len(plan), op_type='tavern_subsidy_team', target_type='expedition', target_id=int(log_id), before_json=None, after_json={'expedition_log_id': int(log_id), 'total_amount': round(total_distributed, 2), 'target_total_amount': round(sum((x['amount'] for x in target_detail)), 2), 'mode': 'weighted' if has_allocations else 'even', 'allocations': alloc_detail, 'target_allocations': target_detail, 'recipient_ids': [m['adventurer_id'] for m, _, _, _ in delta_plan], 'reason': reason}, note=f"{subsidy_label}（团队·{('倾斜' if has_allocations else '均分')}）：补差 {round(total_distributed, 2)} / {len(delta_plan)} 人"))
            ok, audit_err = db_manager.add_all(audit_rows)
            if not ok:
                return (None, audit_err or '酒馆补贴审计写入失败')
            AdventurerS1DataAppealService._notify_tavern_subsidy_recipients(appeal, recipients)
            return (True, None)
        cap_pp = AdventurerS1DataAppealService._tavern_relief_policy()['applicant_bonus_cap'] if is_relief else AdventurerS1DataAppealService._tavern_subsidy_cap_per_person()
        if total <= 0:
            return (True, None)
        previous_max = AdventurerS1DataAppealService._tavern_subsidy_individual_maxima(log_id).get(int(appeal.adventurer_id), 0.0)
        delta = round(max(total - previous_max, 0.0), 2)
        if delta <= 0:
            return (True, None)
        split_result = {}
        new_balance, currency_err = S1CurrencyService.add(appeal.adventurer_id, 'gold', delta, source_type=currency_source_type, related_id=appeal.id, related_type='data_appeal', description=f'{subsidy_label}：{reason}', auto_commit=False, split_result=split_result)
        if currency_err:
            return (None, currency_err)
        base_seq = AdventurerS1DataAppealService._next_audit_seq(appeal.id)
        audit_rows = [AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=appeal.adventurer_id, op_seq=base_seq, op_type='currency', target_type='currency', target_id=None, before_json=None, after_json={'currency_code': 'gold', 'amount': delta, 'balance_after': new_balance, 'gold_credited': split_result.get('gold_credited', delta), 'low_consumption_credited': split_result.get('low_consumption_credited', 0.0), 'tavern_subsidy': True, 'target_amount': total, 'previous_max_amount': previous_max}, note=subsidy_label), AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=appeal.adventurer_id, op_seq=base_seq + 1, op_type='tavern_subsidy_self', target_type='expedition', target_id=int(log_id), before_json=None, after_json={'expedition_log_id': int(log_id), 'total_amount': total, 'target_amount': total, 'previous_max_amount': previous_max, 'granted_delta': delta, 'reason': reason}, note=f'{subsidy_label}（个人补差）：{delta}，目标额度 {total}')]
        ok, audit_err = db_manager.add_all(audit_rows)
        if not ok:
            return (None, audit_err or '酒馆补贴审计写入失败')
        return (True, None)

    @staticmethod
    def _notify_tavern_subsidy_recipients(appeal: AdventurerS1DataAppeal, recipients: list) -> None:
        """给非申诉发起人的受益队友发酒馆补贴到账通知。每个 recipient 用 _amount 指定个人到账额。"""
        from models.mailbox import Mailbox
        is_relief = (appeal.appeal_category or '') == 'tavern_relief'
        title = '深渊酒馆 · 酒馆补贴到账'
        body_label = '酒馆补贴申请' if is_relief else '酒馆补贴'
        mails = []
        for member in recipients:
            owner_id = member.get('owner_id')
            if not owner_id or owner_id == appeal.user_id:
                continue
            amount = member.get('_amount')
            if amount is None or amount <= 0:
                continue
            mails.append(Mailbox(user_id=owner_id, title=title, content=f"您的冒险者「{member.get('name') or '队员'}」所在队伍的一次探险，经结构化结算核定后获得{body_label}，本次到账 {amount} 金币。", sender_id=None, sender_name='深渊酒馆'))
        if mails:
            db_manager.add_all(mails)

    @staticmethod
    def _is_reclaim_item_applied(appeal_id: int, item_name: str, from_adventurer_id: int) -> bool:
        rows = db_manager.query(AdventurerS1DataAppealAuditLog).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal_id, AdventurerS1DataAppealAuditLog.op_type == 'reclaim_item', AdventurerS1DataAppealAuditLog.reverted.is_(False)).all()
        for row in rows:
            after = row.after_json if isinstance(row.after_json, dict) else {}
            if str(after.get('name') or '').strip() == item_name and int(after.get('from_adventurer_id') or 0) == from_adventurer_id:
                return True
        return False

    @staticmethod
    def _notify_reclaim_teammate(appeal: AdventurerS1DataAppeal, *, owner_id: int, teammate_name: str, item_name: str, removed_qty: int, compensated_gold: float) -> None:
        from models.mailbox import Mailbox
        if not owner_id or owner_id == appeal.user_id:
            return
        parts = [f"您的冒险者「{teammate_name or '队员'}」在随队探险结算修正中，「{item_name}」×{removed_qty} 已自其背包/仓库追回并归还申诉方。"]
        if compensated_gold > 0:
            parts.append(f'其中 {compensated_gold} 金币已按收购价从其账户折价扣除（物品已损毁或不在持有中）。')
        db_manager.add(Mailbox(user_id=owner_id, title='深渊酒馆 · 探险结算物品追回', content=''.join(parts), sender_id=None, sender_name='深渊酒馆'))

    @staticmethod
    def _remove_reclaim_quantity_from_adventurer(from_adv_id: int, owner_id: int, item_name: str, quantity: int, *, appeal_id: int) -> tuple[int, list]:
        """从队友随身/仓库追回 quantity 件；市场 active 挂单会先下架退回仓库。

        返回 (实际移除数量, audit_rows)。
        """
        from plugins.adventurer_s1.services.adventurer_s1_market_service import S1MarketService
        from plugins.adventurer_s1.services.s1_item_instance_service import consume_item_stack
        from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of
        remaining = int(quantity)
        removed_total = 0
        audit_rows: list = []
        if remaining <= 0:
            return (0, audit_rows)
        while remaining > 0:
            listing = db_manager.query(AdventurerS1MarketListing).filter_by(seller_adventurer_id=from_adv_id, item_name=item_name, status='active').order_by(AdventurerS1MarketListing.id.asc()).first()
            if listing:
                _, cancel_err = S1MarketService.cancel_listing(owner_id, listing.id)
                if cancel_err:
                    logger.warning('数据申诉追回下架失败 appeal=%s adv=%s item=%s listing=%s: %s', appeal_id, from_adv_id, item_name, listing.id, cancel_err)
                    break
            removed_this_round = 0
            for model, target_type in ((AdventurerS1Equipment, 'equipment'), (AdventurerS1WarehouseItem, 'warehouse')):
                if remaining <= 0:
                    break
                record = db_manager.query(model).filter_by(adventurer_id=from_adv_id, name=item_name).order_by(model.id.asc()).with_for_update().first()
                if not record:
                    continue
                before_full = AdventurerS1DataAppealService._full_record_columns(record)
                take = min(remaining, item_quantity_of(record))
                _, consume_err = consume_item_stack(record, take, source_type='data_appeal_reclaim', description=f'数据申诉追回物品（#{appeal_id}）', hidden_from_player=False)
                if consume_err:
                    raise RuntimeError(consume_err)
                audit_rows.append({'target_type': target_type, 'target_id': before_full.get('id'), 'before_json': before_full, 'removed_qty': take})
                removed_this_round += take
                remaining -= take
            if removed_this_round <= 0:
                break
            removed_total += removed_this_round
        return (removed_total, audit_rows)

    @staticmethod
    def _get_expedition_member_identity_map(expedition_log_id: int) -> dict[int, int]:
        """读取出发时冻结的成员与 owner 映射，作为申诉追回的身份硬边界。"""
        log = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id == int(expedition_log_id)).first()
        if not log:
            return {}
        full_log = log.full_log if isinstance(log.full_log, dict) else {}
        snapshot = full_log.get('audit_member_snapshot')
        if not isinstance(snapshot, list):
            return {}
        identity_map: dict[int, int] = {}
        for row in snapshot:
            if not isinstance(row, dict):
                continue
            try:
                adventurer_id = int(row.get('adventurer_id'))
                owner_id = int(row.get('owner_id'))
            except (TypeError, ValueError):
                continue
            if adventurer_id > 0 and owner_id > 0:
                identity_map[adventurer_id] = owner_id
        return identity_map

    @staticmethod
    def _apply_reclaim_items(appeal: AdventurerS1DataAppeal, reclaims: list | None, *, execution_status_out: dict | None=None) -> tuple:
        """追回与补发同组保护双方携带资产；拒绝时连同补偿和审计完整回滚。"""
        from plugins.adventurer_s1.services.s1_appeal_departure_guard import capture_departure_asset_states, departure_asset_change_error, load_departure_asset_ids_for_members
        if not isinstance(reclaims, list) or not reclaims:
            return (True, None)
        member_ids = {int(appeal.adventurer_id)}
        member_ids.update((source_id for row in reclaims if isinstance(row, dict) for source_id in [_normalize_appeal_patch_integer(row.get('from_adventurer_id'), minimum=1)] if source_id is not None))
        savepoint = db_manager.begin_nested()
        try:
            db_manager.query(AdventurerS1.id).filter(AdventurerS1.id.in_(sorted(member_ids))).order_by(AdventurerS1.id.asc()).with_for_update().all()
            transfer_rows = [row for row in reclaims if row.get('asset_kind') in {'skill', 'trait'}]
            item_reclaims = [row for row in reclaims if row.get('asset_kind') not in {'skill', 'trait'}]
            hidden_drop_context = {}
            issue = AdventurerS1DataAppealService._hidden_drop_reclaim_issue(appeal, item_reclaims, execution_context=hidden_drop_context)
            if issue:
                if isinstance(execution_status_out, dict):
                    execution_status_out['terminal_operation_error'] = issue
                savepoint.rollback()
                return (None, issue['detail'])
            protected = load_departure_asset_ids_for_members(sorted(member_ids))
            before = capture_departure_asset_states(protected)
            if transfer_rows:
                try:
                    AdventurerS1DataAppealService._apply_reclaim_abilities(appeal, transfer_rows)
                except ValueError as exc:
                    savepoint.rollback()
                    return (None, str(exc))
            if hidden_drop_context:
                result = AdventurerS1DataAppealService._apply_reclaim_items_impl(appeal, item_reclaims, hidden_drop_context=hidden_drop_context)
            else:
                result = AdventurerS1DataAppealService._apply_reclaim_items_impl(appeal, item_reclaims)
            if not result[1]:
                db_manager.flush()
                after = capture_departure_asset_states(protected)
                for mid in sorted(member_ids):
                    issue = departure_asset_change_error(before[mid], after[mid])
                    if issue:
                        if isinstance(execution_status_out, dict):
                            execution_status_out['terminal_operation_error'] = issue
                        result = (None, issue['detail'])
                        break
            if result[1]:
                savepoint.rollback()
            else:
                savepoint.commit()
            return result
        except Exception:
            savepoint.rollback()
            raise

    @staticmethod
    def _apply_reclaim_abilities(appeal, transfers):
        from plugins.adventurer_s1.services.s1_asset_transfer_service import apply_asset_transfers
        identities = AdventurerS1DataAppealService._get_expedition_member_identity_map(int(appeal.expedition_log_id))
        log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=appeal.expedition_log_id).first()
        if not log:
            raise ValueError('转移缺少权威探险日志')
        full_log = log.full_log or {}
        frozen = {}
        for member in full_log.get('audit_member_snapshot') or []:
            for kind, field in (('skill', 'skills'), ('trait', 'traits')):
                for row in member.get(field) or []:
                    frozen[member.get('adventurer_id'), kind, row.get('asset_id', row.get('id'))] = row
        previous = db_manager.query(AdventurerS1DataAppealAuditLog).filter_by(appeal_id=appeal.id, op_type='transfer_asset', reverted=False).all()
        applied = {(row.after_json['asset_kind'], row.after_json['asset_id']) for row in previous if isinstance(row.after_json, dict)}
        planned = [{**row, 'to_adventurer_id': appeal.adventurer_id} for row in transfers if (row['asset_kind'], row['asset_id']) not in applied]
        receipts = apply_asset_transfers(planned, allowed_member_ids=identities, frozen_assets=frozen)
        seq = AdventurerS1DataAppealService._next_audit_seq(appeal.id)
        for offset, receipt in enumerate(receipts):
            db_manager.add(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=appeal.adventurer_id, op_seq=seq + offset, op_type='transfer_asset', target_type=receipt['asset_kind'], target_id=receipt['asset_id'], before_json=receipt['before'], after_json=receipt, note=str(receipt.get('reason') or '')))

    @staticmethod
    def _hidden_drop_reclaim_issue(appeal: AdventurerS1DataAppeal, reclaims: list, *, execution_context: dict) -> dict | None:
        """Read authoritative grants and prior transfers under sorted member locks."""
        from plugins.adventurer_s1.services.s1_appeal_hidden_drop_guard import hidden_drop_reclaim_error, project_hidden_drop_receipts
        log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=appeal.expedition_log_id).populate_existing().first()
        try:
            if log is None or not isinstance(log.full_log, dict):
                raise ValueError('hidden_drop_log_unavailable')
            if not project_hidden_drop_receipts(log.full_log):
                return None
            from plugins.adventurer_s1.plugin import AdventurerS1Plugin
            batch_key = 'ADVENTURER_S1_APPEAL_PROVENANCE_BATCH_SIZE'
            batch_size = int(current_app.config.get(batch_key, AdventurerS1Plugin.default_config[batch_key]))
            if batch_size <= 0:
                raise ValueError('hidden_drop_provenance_batch_size_invalid')
            audits = db_manager.query(AdventurerS1DataAppealAuditLog.appeal_id, AdventurerS1DataAppealAuditLog.after_json).filter(AdventurerS1DataAppealAuditLog.target_id == appeal.expedition_log_id, AdventurerS1DataAppealAuditLog.target_type == 'expedition', AdventurerS1DataAppealAuditLog.op_type == 'reclaim_item', AdventurerS1DataAppealAuditLog.reverted.is_(False)).yield_per(batch_size)
            previous = {}
            applied = set()
            for audit in audits:
                row = audit.after_json
                if not isinstance(row, dict):
                    raise ValueError('hidden_drop_prior_reclaim_invalid')
                key = (row.get('from_adventurer_id'), row.get('name'))
                quantity = row.get('reclaim_quantity', row.get('quantity'))
                if type(quantity) is not int or quantity <= 0:
                    raise ValueError('hidden_drop_prior_reclaim_quantity_invalid')
                previous[key] = previous.get(key, 0) + quantity
                if audit.appeal_id == appeal.id:
                    applied.add(key)
            pending = [row for row in reclaims if isinstance(row, dict) and (row.get('from_adventurer_id'), row.get('name')) not in applied]
            return hidden_drop_reclaim_error(log.full_log, pending, [dict(from_adventurer_id=mid, name=name, reclaim_quantity=quantity) for (mid, name), quantity in previous.items()], execution_context=execution_context)
        except ValueError as exc:
            return {'code': 'hidden_dungeon_drop_evidence_invalid', 'detail': '隐藏副本掉落来源回执无法确认，拒绝执行物品追回。', 'evidence_error': str(exc)}

    @staticmethod
    def _apply_reclaim_items_impl(appeal: AdventurerS1DataAppeal, reclaims: list | None, *, hidden_drop_context: dict | None=None) -> tuple:
        """归属错配修复：按错收量向队友追回，并独立补足申诉者剧情应得差额。"""
        if not isinstance(reclaims, list) or not reclaims:
            return (True, None)
        log_id = appeal.expedition_log_id
        if not log_id:
            return (None, '缺少探险日志，无法追回物品')
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        from plugins.adventurer_s1.services.s1_buyback_service import S1BuybackService
        from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
        identity_map = AdventurerS1DataAppealService._get_expedition_member_identity_map(int(log_id))
        allowed_member_ids = set(identity_map)
        if not allowed_member_ids:
            return (None, '无法确认本次探险小队成员，拒绝追回物品')
        current_identities = {int(adv.id): int(adv.owner_id or 0) for adv in db_manager.query(AdventurerS1).filter(AdventurerS1.id.in_(allowed_member_ids)).all()}
        changed_ids = sorted((adventurer_id for adventurer_id, owner_id in identity_map.items() if current_identities.get(adventurer_id) != owner_id))
        if changed_ids:
            return (None, f'参战冒险者身份已变化，拒绝追回物品: {changed_ids}')
        roster = {m['adventurer_id']: m for m in AdventurerS1DataAppealService._build_party_roster(log_id)}
        appellant = db_manager.query(AdventurerS1).filter(AdventurerS1.id == appeal.adventurer_id).with_for_update().first()
        if not appellant:
            return (None, '申诉冒险者不存在')
        base_seq = AdventurerS1DataAppealService._next_audit_seq(appeal.id)
        audit_rows: list = []
        seq_offset = 0
        for reclaim in reclaims:
            if not isinstance(reclaim, dict):
                continue
            item_name = str(reclaim.get('name') or '').strip()
            if not item_name:
                continue
            try:
                from_id = int(reclaim.get('from_adventurer_id'))
            except (TypeError, ValueError):
                continue
            if from_id <= 0 or from_id == appeal.adventurer_id:
                continue
            if from_id not in allowed_member_ids:
                return (None, f'追回来源冒险者 #{from_id} 不属于本次探险小队')
            if AdventurerS1DataAppealService._is_reclaim_item_applied(appeal.id, item_name, from_id):
                continue
            try:
                quantity = max(1, int(reclaim.get('quantity') or 1))
            except (TypeError, ValueError):
                quantity = 1
            appellant_settlement_quantity = AdventurerS1DataAppealService._lookup_expedition_settlement_item_quantity(appeal.adventurer_id, log_id, item_name)
            if appellant_settlement_quantity is None:
                appellant_settlement_quantity = 0
            protected_quantities = (hidden_drop_context or {}).get('protected_quantities', {})
            from plugins.adventurer_s1.services.s1_item_quantity_service import split_stack_suffix
            canonical_item_name, _ = split_stack_suffix(item_name, 1)
            appellant_settlement_quantity = max(0, appellant_settlement_quantity - protected_quantities.get((appeal.adventurer_id, canonical_item_name), 0))
            grant_quantity = max(0, quantity - appellant_settlement_quantity)
            if grant_quantity <= 0:
                continue
            destination = str(reclaim.get('destination') or 'warehouse').strip().lower()
            if destination not in _VALID_CREATE_DESTINATIONS:
                destination = 'warehouse'
            reason = str(reclaim.get('reason') or '').strip() or '剧情归属与实际结算不一致，补发申诉者差额'
            source_settlement_quantity = AdventurerS1DataAppealService._lookup_expedition_settlement_item_quantity(from_id, log_id, item_name)
            if source_settlement_quantity is None or source_settlement_quantity <= 0:
                return (None, f'来源队友本次结算未实际获得「{item_name}」，拒绝向其追回')
            reclaim_quantity = min(quantity, source_settlement_quantity)
            reclaim_limits = (hidden_drop_context or {}).get('reclaim_quantities', {})
            if (from_id, canonical_item_name) in reclaim_limits:
                reclaim_quantity = reclaim_limits[from_id, canonical_item_name]
            from_adv = db_manager.query(AdventurerS1).filter_by(id=from_id).with_for_update().first()
            if not from_adv or int(from_adv.owner_id or 0) != identity_map.get(from_id):
                return (None, f'追回来源冒险者 #{from_id} 身份已变化')
            owner_id = int(from_adv.owner_id or 0)
            catalog = db_manager.query(AdventurerS1Item).filter(AdventurerS1Item.name == item_name).first()
            removed_qty, remove_audits = AdventurerS1DataAppealService._remove_reclaim_quantity_from_adventurer(from_id, owner_id, item_name, reclaim_quantity, appeal_id=appeal.id)
            shortage = max(0, reclaim_quantity - removed_qty)
            compensated_gold = 0.0
            for remove_audit in remove_audits:
                audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=from_id, op_seq=base_seq + seq_offset, op_type='reclaim_remove', target_type=remove_audit['target_type'], target_id=remove_audit.get('target_id'), before_json=remove_audit.get('before_json'), after_json={'name': item_name, 'removed_qty': remove_audit.get('removed_qty'), 'from_adventurer_id': from_id}, note=reason))
                seq_offset += 1
            if shortage > 0:
                item_type = catalog.type if catalog else ''
                rarity = catalog.rarity if catalog else 'common'
                unit_price = S1BuybackService.compute_recycle_price(item_name, item_type, rarity)
                compensated_gold = round(unit_price * shortage, 2)
                if compensated_gold > 0:
                    _balance, deduct_err = S1CurrencyService.deduct(from_id, 'gold', compensated_gold, source_type='data_appeal_reclaim_comp', related_id=appeal.id, related_type='data_appeal', description=f'数据申诉追回缺失物品「{item_name}」×{shortage}', auto_commit=False, allow_negative=True)
                    if deduct_err:
                        return (None, deduct_err)
                    audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=from_id, op_seq=base_seq + seq_offset, op_type='reclaim_compensation', target_type='currency', target_id=None, before_json=None, after_json={'currency_code': 'gold', 'amount': compensated_gold, 'balance_after': _balance, 'item_name': item_name, 'shortage_qty': shortage}, note=reason))
                    seq_offset += 1
            settlement_meta = AdventurerS1DataAppealService._lookup_expedition_settlement_item_meta(appeal.adventurer_id, log_id, item_name)
            if catalog is not None:
                grant_type, grant_rarity, grant_desc, _ = AdventurerS1DataAppealService._resolve_create_item_grant_fields({'name': item_name}, settlement_meta=settlement_meta, catalog=catalog, use_ai_desc=False)
            else:
                grant_type, grant_rarity, grant_desc, _ = AdventurerS1DataAppealService._resolve_create_item_grant_fields({'name': item_name}, settlement_meta=settlement_meta, catalog=None, use_ai_desc=False)
            grant_payload = {'type': grant_type, 'rarity': grant_rarity, 'quantity': grant_quantity, 'desc': grant_desc}
            actual_destination, grant_err = grant_raw_item(appellant, item_name, grant_payload, source_type='data_appeal_reclaim', description=reason, force_destination=destination, related_id=str(log_id), related_type='expedition_log')
            if grant_err:
                return (None, grant_err)
            audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=appeal.adventurer_id, op_seq=base_seq + seq_offset, op_type='create_item', target_type='item', target_id=None, before_json=None, after_json={'destination': actual_destination or destination, 'requested_destination': destination, 'name': item_name, 'type': grant_type, 'rarity': grant_rarity, 'quantity': grant_quantity, 'entitled_quantity': quantity, 'already_settled_to_appellant': appellant_settlement_quantity, 'sourced_from_catalog': catalog is not None, 'reclaim_grant': True, 'from_adventurer_id': from_id, 'source_settlement_quantity': source_settlement_quantity, 'reclaim_quantity': reclaim_quantity, 'removed_from_teammate': removed_qty, 'compensated_gold': compensated_gold, 'reason': reason}, note=reason))
            seq_offset += 1
            audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal.id, adventurer_id=appeal.adventurer_id, op_seq=base_seq + seq_offset, op_type='reclaim_item', target_type='expedition', target_id=int(log_id), before_json=None, after_json={'name': item_name, 'from_adventurer_id': from_id, 'quantity': grant_quantity, 'entitled_quantity': quantity, 'already_settled_to_appellant': appellant_settlement_quantity, 'source_settlement_quantity': source_settlement_quantity, 'reclaim_quantity': reclaim_quantity, 'removed_qty': removed_qty, 'compensated_gold': compensated_gold, 'destination': actual_destination or destination}, note=reason))
            seq_offset += 1
            member = roster.get(from_id) or {}
            AdventurerS1DataAppealService._notify_reclaim_teammate(appeal, owner_id=owner_id, teammate_name=member.get('name') or from_adv.name, item_name=item_name, removed_qty=removed_qty, compensated_gold=compensated_gold)
        if audit_rows:
            ok, audit_err = db_manager.add_all(audit_rows)
            if not ok:
                return (None, audit_err or '追回物品审计写入失败')
        return (True, None)

    @staticmethod
    def _submit_description_restore_appeal(user_id: int, adv: AdventurerS1, normalized_targets: list[dict], snapshot: dict, expedition_evidence: dict | None) -> tuple:
        """机械还原同名替换前的冻结描述，并写入普通申诉审计链。"""
        if len(normalized_targets) != 1:
            return (None, '装备描述还原每次只能选择一件装备')
        target = normalized_targets[0]
        target_type = target.get('type')
        if target_type not in ('equipment', 'warehouse'):
            return (None, '装备描述还原只支持随身装备或收藏室物品')
        if not isinstance(expedition_evidence, dict):
            return (None, '请选择发生描述丢失的历史探险记录')
        try:
            expedition_log_id = int(expedition_evidence.get('expedition_log_id'))
            target_id = int(target.get('id'))
        except (TypeError, ValueError):
            return (None, '历史探险或装备参数无效')
        from plugins.adventurer_s1.services.s1_same_name_replacement_desc_repair_service import S1SameNameReplacementDescRepairService
        candidate = S1SameNameReplacementDescRepairService.get_player_candidate(adv.id, expedition_log_id, target_type, target_id)
        if not candidate:
            return (None, '该历史探险没有可安全还原的装备描述，或装备之后已再次变化')
        model = _TABLE_MODEL[target_type]
        now = _portable_utc_now()
        section = target_type
        repair_desc = str(candidate.get('repair_custom_desc') or '').strip()
        if not repair_desc:
            return (None, '历史快照没有可还原的装备描述')
        try:
            with db_manager.transaction():
                locked_adv = db_manager.query(AdventurerS1).filter_by(id=int(adv.id), owner_id=int(user_id)).with_for_update().first()
                if not locked_adv:
                    return (None, '冒险者不存在或无权操作')
                active = db_manager.query(AdventurerS1DataAppeal.id).filter(AdventurerS1DataAppeal.user_id == int(user_id), AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review')), AdventurerS1DataAppeal.appeal_category != 'revert_request').first()
                if active:
                    return (None, '您有一条申诉正在处理中，请等待完成后再提交')
                row = db_manager.query(model).filter_by(id=target_id, adventurer_id=int(adv.id)).with_for_update().first()
                if not row or row.custom_desc != candidate.get('expected_custom_desc'):
                    return (None, '装备描述已发生变化，请刷新后重新选择')
                before_full = AdventurerS1DataAppealService._full_record_columns(row)
                evidence_snapshot = {'expedition_log_id': expedition_log_id, 'event_index': None, 'target_dungeon': candidate.get('target_dungeon'), 'party_name': candidate.get('party_name'), 'log_created_at': candidate.get('log_created_at'), 'description_restore': {'target_type': target_type, 'target_id': target_id, 'item_name': candidate.get('item_name'), 'source': 'departure_equipment_snapshot', 'expected_custom_desc': candidate.get('expected_custom_desc'), 'repair_custom_desc': repair_desc}}
                result_text = '已按该次探险出发前的冻结快照还原装备描述。'
                checklist = [{'label': f"还原「{candidate.get('item_name') or row.name}」的历史描述", 'status': 'done', 'category': 'data_fix', 'detail': '仅还原描述，未改动装备名称、稀有度、数量或其他奖励'}]
                patch = {section: {'update': [{'id': target_id, 'custom_desc': repair_desc}], 'delete': []}}
                appeal = AdventurerS1DataAppeal(quota_source='unrestricted', user_id=int(user_id), adventurer_id=int(adv.id), player_note='历史探险同名替换导致装备描述丢失，申请还原出发前描述', appeal_category='description_restore', selected_targets=normalized_targets, snapshot_before=snapshot, expedition_evidence={**(evidence_snapshot or {})}, expedition_log_id=expedition_log_id, ai_status='done', ai_raw_response=json.dumps({'summary': result_text, 'player_reply_summary': result_text, 'request_checklist': checklist}, ensure_ascii=False), ai_summary=result_text, proposed_patch=patch, review_ai_status='done', review_ai_model='deterministic_snapshot_restore_v1', review_ai_summary='机械证据校验通过，无需模型裁量', status='approved', core_request_satisfied=True, reviewed_at=now, applied_at=now)
                ok, add_err = db_manager.add(appeal)
                if not ok:
                    raise RuntimeError(add_err or '申诉记录写入失败')
                row.custom_desc = repair_desc
                audit = AdventurerS1DataAppealAuditLog(appeal_id=int(appeal.id), adventurer_id=int(adv.id), op_seq=1, op_type='update', target_type=target_type, target_id=target_id, before_json=before_full, after_json={'custom_desc': repair_desc}, note='历史探险同名替换描述机械还原')
                ok, add_err = db_manager.add(audit)
                if not ok:
                    raise RuntimeError(add_err or '申诉审计写入失败')
        except Exception as exc:
            logger.exception('玩家历史探险装备描述还原失败')
            return (None, f'装备描述还原失败：{exc}')
        invalidate_appeal_targets_cache(user_id)
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        return ({'id': appeal.id, 'status': appeal.status, 'message': '装备描述已按历史快照还原'}, None)

    @staticmethod
    def submit_appeal(user_id: int, targets: list, player_note: str, expedition_evidence: dict | None=None, appeal_category: str='other') -> tuple:
        note = (player_note or '').strip()
        category = str(appeal_category or 'other').strip()
        if category == 'description_restore':
            return (None, '旧描述还原已下线，请选择「其他问题」并关联探险，让陪审团合并旧描述与本次状态变化')
        if category not in ('data_fix', 'other', 'rarity_review'):
            category = 'other'
        if not note:
            return (None, '请描述遇到的问题')
        if len(note) > _MAX_NOTE_LEN:
            return (None, f'描述不能超过 {_MAX_NOTE_LEN} 字')
        if not isinstance(targets, list):
            return (None, '目标格式无效')
        if len(targets) == 0 and category != 'other' and False:
            return (None, '请至少选择一条需要处理的数据')
        if len(targets) > _MAX_TARGETS:
            return (None, f'单次最多选择 {_MAX_TARGETS} 条')
        if category == 'rarity_review' and len(targets) > _MAX_RARITY_REVIEW_TARGETS:
            return (None, f'单次最多复核 {_MAX_RARITY_REVIEW_TARGETS} 件物品')
        adv, err = AdventurerS1DataAppealService._get_owner_adventurer(user_id)
        if err:
            return (None, err)
        from flask import current_app
        quota_pool = AdventurerS1DataAppealService._quota_pool_for_category(category)
        quota = AdventurerS1DataAppealService._compute_quota_state(user_id, quota_pool)
        if quota['remaining'] <= 0:
            return (None, AdventurerS1DataAppealService._quota_limit_error(quota))
        active = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.user_id == user_id, AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review')), AdventurerS1DataAppeal.appeal_category != 'revert_request').first()
        if active:
            return (None, '您有一条申诉正在处理中，请等待完成后再提交')
        normalized_targets = []
        seen = set()
        for t in targets:
            ttype = str(t.get('type', '')).strip()
            try:
                tid = int(t.get('id'))
            except (TypeError, ValueError):
                return (None, '目标 id 无效')
            key = (ttype, tid)
            if key in seen:
                continue
            seen.add(key)
            if ttype not in _VALID_TARGET_TYPES:
                return (None, f'不支持的目标类型: {ttype}')
            normalized_targets.append({'type': ttype, 'id': tid})
        snapshot, snap_err = AdventurerS1DataAppealService._build_snapshot(adv.id, normalized_targets)
        if snap_err:
            return (None, snap_err)
        lineage_block = AdventurerS1DataAppealService._submission_lineage_block_reason(adv.id, snapshot, note)
        if lineage_block:
            return (None, lineage_block)
        evidence_snapshot = None
        if expedition_evidence:
            if not isinstance(expedition_evidence, dict):
                return (None, '探险证据格式无效')
            try:
                evidence_log_id = int(expedition_evidence.get('expedition_log_id'))
                evidence_event_index = int(expedition_evidence.get('event_index'))
            except (TypeError, ValueError):
                return (None, '探险证据参数无效')
            evidence_snapshot, evidence_err = AdventurerS1DataAppealService._build_expedition_evidence(adv.id, evidence_log_id, evidence_event_index)
            if evidence_err:
                return (None, evidence_err)
        elif category in ('other', 'rarity_review'):
            latest_log = AdventurerS1DataAppealService._get_latest_completed_expedition_log(adv.id)
            if AdventurerS1DataAppealService._log_uses_full_text_appeal(latest_log):
                evidence_snapshot = AdventurerS1DataAppealService._build_latest_settlement_evidence(adv.id)
        if category == 'rarity_review':
            details, purity_err = AdventurerS1DataAppealService._validate_rarity_review_targets(adv.id, normalized_targets, int((evidence_snapshot or {}).get('expedition_log_id') or 0))
            if purity_err:
                return (None, purity_err)
            if not details:
                return (None, '没有可复核的物品')
        relevant_log_id = (evidence_snapshot or {}).get('expedition_log_id')
        if not relevant_log_id:
            latest_log = AdventurerS1DataAppealService._get_latest_completed_expedition_log(adv.id)
            relevant_log_id = latest_log.id if latest_log else None
        if relevant_log_id:
            identity_err = AdventurerS1DataAppealService._appeal_expedition_identity_error(adv.id, relevant_log_id)
            if identity_err:
                return (None, identity_err)
            if category != 'data_fix':
                other_active = db_manager.query(AdventurerS1DataAppeal.id).filter(AdventurerS1DataAppeal.expedition_log_id == int(relevant_log_id), AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review')), AdventurerS1DataAppeal.appeal_category != 'data_fix', AdventurerS1DataAppeal.user_id != user_id).first()
                if other_active:
                    return (None, '同队有成员正在对这次探险申诉处理中，请稍后再试')
        locked_adv = db_manager.query(AdventurerS1).filter(AdventurerS1.id == int(adv.id), AdventurerS1.owner_id == int(user_id), AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).with_for_update().populate_existing().first()
        if not locked_adv:
            db_manager.rollback()
            return (None, '您没有存活的绑定冒险者')
        quota = AdventurerS1DataAppealService._compute_quota_state(user_id, quota_pool, materialize_full_text=quota_pool == _APPEAL_QUOTA_POOL_OTHER)
        if quota['remaining'] <= 0:
            db_manager.rollback()
            return (None, AdventurerS1DataAppealService._quota_limit_error(quota))
        active = db_manager.query(AdventurerS1DataAppeal.id).filter(AdventurerS1DataAppeal.user_id == user_id, AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review')), AdventurerS1DataAppeal.appeal_category != 'revert_request').first()
        if active:
            db_manager.rollback()
            return (None, '您有一条申诉正在处理中，请等待完成后再提交')
        adv = locked_adv
        appeal = AdventurerS1DataAppeal(user_id=user_id, adventurer_id=adv.id, player_note=note, appeal_category=category, quota_bucket_date=quota.get('_charge_date') if quota_pool == _APPEAL_QUOTA_POOL_OTHER else None, quota_source=quota.get('_next_quota_source') if quota_pool == _APPEAL_QUOTA_POOL_OTHER or quota.get('unlimited') else None, selected_targets=normalized_targets, snapshot_before=snapshot, expedition_evidence={**(evidence_snapshot or {})}, expedition_log_id=relevant_log_id, ai_status='pending', status='processing')
        ok, err = player_database_result(db_manager.add(appeal))
        if not ok:
            return (None, err)
        ok, err = player_database_result(db_manager.commit())
        if not ok:
            return (None, err)
        from services.async_task_service import AsyncTaskService
        submitted = AsyncTaskService.submit_task('ai_s1_data_appeal', {'appeal_id': appeal.id, 'invoke_reason': 'initial'})
        if not submitted:
            logger.warning('数据申诉 AI 任务入队失败 appeal=%s', appeal.id)
        else:
            logger.info('数据申诉 AI 入队 appeal=%s invoke_reason=initial adventurer_id=%s', appeal.id, appeal.adventurer_id)
        AdventurerS1DataAppealService._notify_admin_new_appeal(appeal, adv.name)
        invalidate_appeal_targets_cache(user_id)
        return ({'id': appeal.id, 'status': appeal.status, 'message': '申诉已提交，AI 正在分析，完成后将自动处理'}, None)

    @staticmethod
    def _notify_admin_new_appeal(appeal: AdventurerS1DataAppeal, adventurer_name: str) -> None:
        """双 AI 自动闭环后不再发送人工待审通知。"""
        return

    @staticmethod
    def list_my_appeals(user_id: int, limit: int=20) -> tuple:
        limit = max(1, min(int(limit or 20), 50))
        rows = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.user_id == user_id, AdventurerS1DataAppeal.appeal_category != 'revert_request').order_by(AdventurerS1DataAppeal.created_at.desc()).limit(limit).all()
        if not rows:
            return ([], None)
        from extensions import db
        adv_ids = {r.adventurer_id for r in rows if r.adventurer_id}
        adv_name_map = dict(db.session.query(AdventurerS1.id, AdventurerS1.name).filter(AdventurerS1.id.in_(adv_ids)).all()) if adv_ids else {}
        revert_candidate_ids = {r.id for r in rows if r.id and (r.status or '') in AdventurerS1DataAppealService._REVERT_ELIGIBLE_STATUSES}
        revertible_appeal_ids = AdventurerS1DataAppealService._appeal_ids_with_revertible_audits(revert_candidate_ids)
        from plugins.adventurer_s1.services.s1_ai_feedback_service import S1AiFeedbackService
        feedback_targets = S1AiFeedbackService.get_appeal_targets(user_id, rows)
        items = []
        for r in rows:
            row_data = {**r.to_dict(adv_name_map=adv_name_map), 'ai_feedback': feedback_targets.get(r.id, {}), 'can_request_player_revert': AdventurerS1DataAppealService._can_request_player_revert(r, has_revertible_audits=r.id in revertible_appeal_ids)}
            items.append(AdventurerS1DataAppealService._enrich_cancellation_display(r, row_data))
        return (items, None)

    @staticmethod
    def player_mimo_review_request(user_id: int, appeal_id: int, player_request: str) -> tuple:
        """玩家发起独立复核：校验后提交异步任务，立即返回。"""
        request_text = (player_request or '').strip()
        if not request_text:
            return (None, '请输入你的诉求')
        if len(request_text) > _MAX_NOTE_LEN:
            return (None, f'诉求不能超过 {_MAX_NOTE_LEN} 字')
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, user_id=user_id).first()
        if not appeal:
            return (None, '申诉不存在')
        if appeal.ai_status in ('pending', 'processing') or not appeal.ai_raw_response:
            return (None, '原申诉 AI 尚未完成处理，暂时不能复核')
        if appeal.player_review_status == 'pending':
            return (None, '已有复核任务在处理中，请稍候')
        if appeal.player_review_status == 'done':
            return (None, '该申诉已进行过独立复核，不可重复提交')
        request_log = list(appeal.player_review_requests or [])
        request_log.append({'request': request_text, 'result': None, 'status': 'pending', 'created_at': _portable_utc_now().isoformat() + 'Z'})
        appeal.player_review_requests = request_log[-20:]
        appeal.player_review_status = 'pending'
        ok, err = db_manager.commit()
        if not ok:
            return (None, '暂时无法开始复核，请稍后重试')
        from services.async_task_service import AsyncTaskService
        submitted = AsyncTaskService.submit_task('ai_s1_data_appeal_mimo_review', {'appeal_id': appeal_id, 'player_request': request_text})
        if not submitted:
            appeal.player_review_status = 'none'
            db_manager.commit()
            return (None, '暂时无法开始复核，请稍后重试')
        return ({'appeal_id': appeal_id, 'status': 'pending', 'message': '已提交，AI 复核中，结果将在完成后自动显示'}, None)

    @staticmethod
    def process_player_mimo_review(appeal_id: int) -> None:
        """异步 worker：执行 MIMO 独立复核并更新工单。"""
        appeal = AdventurerS1DataAppealService._load_appeal(appeal_id)
        if not appeal or appeal.player_review_status != 'pending':
            return
        requests = list(appeal.player_review_requests or [])
        pending_req = None
        for r in reversed(requests):
            if isinstance(r, dict) and r.get('status') == 'pending':
                pending_req = r
                break
        if not pending_req:
            appeal.player_review_status = 'none'
            db_manager.commit()
            return
        player_request = pending_req.get('request', '')
        expedition_evidence = copy.deepcopy(appeal.expedition_evidence) if isinstance(appeal.expedition_evidence, dict) else None
        if (appeal.appeal_category or 'other') != 'data_fix':
            AdventurerS1DataAppealService._ensure_departure_consumption_candidates(expedition_evidence, appeal.adventurer_id)
            expedition_evidence = AdventurerS1DataAppealService._attach_equipment_description_inheritance_candidates(expedition_evidence, appeal.adventurer_id, appeal.snapshot_before or {})
        appeal_fact_sheet = None
        if (appeal.appeal_category or 'other') == 'other' and appeal.expedition_log_id and expedition_evidence and current_app.config.get('ADVENTURER_S1_DATA_APPEAL_FACT_SHEET_ENABLED', False):
            from plugins.adventurer_s1.services.s1_appeal_fact_sheet_ai_service import get_or_create_appeal_fact_sheet
            appeal_fact_sheet = get_or_create_appeal_fact_sheet(appeal_id, expedition_log_id=appeal.expedition_log_id, expedition_evidence=copy.deepcopy(expedition_evidence))
            appeal = AdventurerS1DataAppealService._load_appeal(appeal_id)
            if not appeal or appeal.player_review_status != 'pending':
                logger.info('玩家独立复核事实卡生成后中止 appeal=%s: 工单已不在 pending', appeal_id)
                return
        context = {'id': appeal.id, 'adventurer_id': appeal.adventurer_id, 'status': appeal.status, 'ai_status': appeal.ai_status, 'appeal_category': appeal.appeal_category or 'other', 'player_note': appeal.player_note, 'selected_targets': appeal.selected_targets or [], 'target_labels': appeal._target_labels(), 'snapshot_before': appeal.snapshot_before, 'all_player_records': AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id), 'expedition_evidence': expedition_evidence, 'prior_appeal_grants': AdventurerS1DataAppealService._prior_grants_without_tavern_subsidy(AdventurerS1DataAppealService._build_prior_appeal_grants(appeal.expedition_log_id, appeal.id)), 'team_pool_status': {'settled': AdventurerS1DataAppealService._is_team_pool_settled(appeal.expedition_log_id)}, 'ai_summary': appeal.ai_summary, 'ai_raw_response': appeal.ai_raw_response, 'proposed_patch': appeal.proposed_patch, 'review_ai_status': appeal.review_ai_status, 'review_ai_summary': appeal.review_ai_summary, 'review_ai_raw_response': appeal.review_ai_raw_response, 'reject_reason': appeal.reject_reason}
        if appeal_fact_sheet:
            context['appeal_fact_sheet'] = appeal_fact_sheet
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_review_ai_service import call_player_data_appeal_review_request
        try:
            raw = call_player_data_appeal_review_request(appeal_id, player_request=player_request, appeal_context=context)
        except Exception as exc:
            logger.error('玩家独立复核 MIMO 调用异常 appeal=%s: %s', appeal_id, exc, exc_info=True)
            raw = {'decision': 'error', 'reason': '陪审团长暂时无法到场，请稍后再试'}
        appeal = AdventurerS1DataAppealService._load_appeal(appeal_id)
        if not appeal:
            return
        result_entry = raw if isinstance(raw, dict) else {'decision': 'error', 'reason': '无有效返回'}
        review_decision = str(result_entry.get('decision') or '').strip().lower()
        review_reason = str(result_entry.get('reason') or result_entry.get('message') or '').strip()
        updated_requests = []
        found_pending = False
        for r in reversed(appeal.player_review_requests or []):
            if not found_pending and isinstance(r, dict) and (r.get('status') == 'pending'):
                updated_requests.append({**r, 'result': result_entry, 'status': 'done'})
                found_pending = True
            else:
                updated_requests.append(r)
        updated_requests.reverse()
        appeal.player_review_requests = updated_requests[-20:]
        appeal.player_review_status = 'done'
        if review_decision == 'player_may_be_right':
            if review_reason:
                appeal.reject_reason = f'陪审团长复核后认为与原结论存在分歧：{review_reason}'
        ok, err = db_manager.commit()
        if not ok:
            logger.error('玩家独立复核落库失败 appeal=%s: %s', appeal_id, err)
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)

    @staticmethod
    def _submission_lineage_block_reason(adventurer_id: int, snapshot_before: dict, player_note: str | None) -> str | None:
        """玩家提交前拦截已命中申诉数量/异常收益链路的重复收益诉求。"""
        return None

    @staticmethod
    def normalize_patch(raw_patch: dict, snapshot_before: dict, all_player_records: dict | None=None, narrative_currency_cap: float | None=None, allow_quantity_increase: bool=False, allow_rarity_increase: bool=False, allow_all_player_record_ids: bool=True, expedition_evidence: dict | None=None, allow_objective_name_update_ids: dict | None=None, allow_custom_desc_clear_ids: dict | None=None, adventurer_id: int | None=None, appeal_id: int | None=None, lineage_warnings_out: list | None=None, contract_status_out: dict | None=None, preserve_unchanged_update_ids: dict[str, set[int]] | None=None) -> tuple:
        """校验并规范化 AI patch，剔除非法操作。

        narrative_currency_cap 不为 None 时，对 currency_adjustments 的累计金额夹紧到该上限
        （用于"叙述货币折算"无 member_update.gold_change 背书时的小额封顶）。
        allow_quantity_increase 为 True 时跳过数量只能减少的守卫（用于 supplement_patch 修正性操作）。
        """
        allow_quantity_increase = True
        allow_rarity_increase = True
        narrative_currency_cap = None
        proposed_rows = sum((len(v) for v in raw_patch.values() if isinstance(v, list))) if isinstance(raw_patch, dict) else 0
        max_create_traits = proposed_rows
        max_create_skills = proposed_rows
        contract_valid = True

        def _invalidate_contract() -> None:
            nonlocal contract_valid
            contract_valid = False

        def _finish(result, error):
            if isinstance(contract_status_out, dict):
                contract_status_out.clear()
                contract_status_out['contract_valid'] = contract_valid
            return (result, error)

        def _rows(container: dict, key: str) -> list:
            if key not in container:
                return []
            value = container.get(key)
            if not isinstance(value, list):
                _invalidate_contract()
                return []
            return value
        if not isinstance(raw_patch, dict):
            _invalidate_contract()
            return _finish(None, 'patch 格式无效')
        allowed_ids = {s: set() for s in ('traits', 'equipment', 'warehouse', 'skills')}
        for section in allowed_ids:
            for r in snapshot_before.get(section) or []:
                if not isinstance(r, dict) or r.get('id') is None:
                    continue
                try:
                    allowed_ids[section].add(int(r['id']))
                except (TypeError, ValueError):
                    continue
        if all_player_records and allow_all_player_record_ids:
            for section, key in (('traits', 'traits'), ('equipment', 'equipment'), ('warehouse', 'warehouse'), ('skills', 'skills')):
                for r in all_player_records.get(key) or []:
                    try:
                        allowed_ids[section].add(int(r['id']))
                    except (KeyError, TypeError, ValueError):
                        pass
        for section in ('create_items', 'create_traits', 'create_skills'):
            for row in raw_patch.get(section) or []:
                if not isinstance(row, dict) or 'owner_adventurer_id' not in row:
                    continue
                owner_id = _normalize_appeal_patch_integer(row['owner_adventurer_id'], minimum=1)
                if owner_id is None or adventurer_id is None or owner_id != int(adventurer_id):
                    _invalidate_contract()
                    return _finish(None, '新增资产的受益人必须是本次申诉者')
        raw_patch = AdventurerS1DataAppealService._normalize_patch_section_ids(raw_patch, allowed_ids)
        objective_name_ids = allow_objective_name_update_ids if isinstance(allow_objective_name_update_ids, dict) else {}
        custom_desc_clear_ids = allow_custom_desc_clear_ids if isinstance(allow_custom_desc_clear_ids, dict) else {}
        type_map = {'traits': 'trait', 'equipment': 'equipment', 'warehouse': 'warehouse', 'skills': 'skill'}
        record_maps = {s: {} for s in ('traits', 'equipment', 'warehouse', 'skills')}
        for section in record_maps:
            for r in snapshot_before.get(section) or []:
                if not isinstance(r, dict) or r.get('id') is None:
                    continue
                try:
                    record_maps[section][int(r['id'])] = r
                except (TypeError, ValueError):
                    continue
        if all_player_records:
            for section in ('traits', 'equipment', 'warehouse', 'skills'):
                for r in all_player_records.get(section) or []:
                    try:
                        rid = int(r['id'])
                    except (KeyError, TypeError, ValueError):
                        continue
                    prev = record_maps[section].get(rid) or {}
                    merged = dict(prev)
                    merged.update({k: v for k, v in r.items() if k != 'id'})
                    record_maps[section][rid] = merged
        from plugins.adventurer_s1.services.s1_item_use_condition_service import catalog_immutable_item_names
        catalog_locked_names = catalog_immutable_item_names(AdventurerS1DataAppealService._collect_patch_item_names(raw_patch, record_maps))
        AdventurerS1DataAppealService._log_text_locked_patch_skips(raw_patch, record_maps, catalog_locked_names)
        catalog_bound_names = AdventurerS1DataAppealService._collect_catalog_bound_item_names(record_maps, raw_patch)
        catalog_desc_map = AdventurerS1DataAppealService._load_catalog_desc_map(catalog_bound_names)
        normalized = {}
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            raw_section = raw_patch.get(section, _APPEAL_PATCH_MISSING)
            if raw_section is _APPEAL_PATCH_MISSING:
                section_in = {}
            elif isinstance(raw_section, dict):
                section_in = raw_section
            else:
                _invalidate_contract()
                section_in = {}
            updates = []
            deletes = []
            field_key = type_map[section]
            update_rows = _rows(section_in, 'update')
            delete_rows = _rows(section_in, 'delete')
            section_has_deletes = bool(delete_rows)
            for item in update_rows:
                if not isinstance(item, dict) or 'id' not in item:
                    _invalidate_contract()
                    continue
                rid = _normalize_appeal_patch_integer(item['id'], minimum=1)
                if rid is None:
                    _invalidate_contract()
                    continue
                if rid not in allowed_ids[section]:
                    _invalidate_contract()
                    continue
                cleaned = {'id': rid}
                original = record_maps[section].get(rid) or {}
                allowed_fields = _PATCH_UPDATE_FIELDS[field_key] | _ASSET_RECOVERY_PATCH_META_FIELDS.get(field_key, frozenset())
                skip_keys = _TRAIT_RECOVERY_PATCH_FIELDS if field_key == 'trait' else frozenset()
                working_item = dict(item)
                if field_key in ('equipment', 'warehouse'):
                    if 'description' in working_item and 'custom_desc' not in working_item:
                        working_item['custom_desc'] = working_item.pop('description')
                    elif 'description' in working_item:
                        working_item.pop('description', None)
                        _invalidate_contract()
                if field_key in ('equipment', 'warehouse') and AdventurerS1DataAppealService._appeal_record_text_locked(original, catalog_locked_names):
                    _invalidate_contract()
                    continue
                semantic_quantity = AdventurerS1DataAppealService._snapshot_item_quantity(original.get('quantity'))
                quantity_norm_intent = False
                if field_key in ('equipment', 'warehouse'):
                    pre_sanitize = dict(item)
                    if 'description' in pre_sanitize and 'custom_desc' not in pre_sanitize:
                        pre_sanitize['custom_desc'] = pre_sanitize.pop('description')
                    if 'name' in pre_sanitize and isinstance(pre_sanitize.get('name'), str):
                        pre_sanitize['name'] = pre_sanitize['name'].strip()
                    quantity_norm_intent = AdventurerS1DataAppealService._is_quantity_field_normalization_update(pre_sanitize, original, section_has_deletes=section_has_deletes)
                    identity_fields_before = {key for key in ('name', 'type', 'custom_desc') if key in working_item}
                    AdventurerS1DataAppealService._sanitize_item_identity_patch_fields(working_item, original, catalog_bound_names=catalog_bound_names, allow_name_update=True)
                    AdventurerS1DataAppealService._strip_spurious_custom_desc_mutations(working_item, original, catalog_bound_names=catalog_bound_names, catalog_desc_map=catalog_desc_map, allow_custom_desc_clear=rid in set(custom_desc_clear_ids.get(section) or []))
                    if any((key not in working_item for key in identity_fields_before)):
                        _invalidate_contract()
                for k, v in working_item.items():
                    if k == 'id':
                        continue
                    if k in skip_keys:
                        continue
                    if k not in allowed_fields:
                        continue
                    if k in ('name', 'type', 'description', 'custom_desc', 'damage_note'):
                        nullable = k in ('custom_desc', 'damage_note')
                        required = k in ('name', 'type', 'description')
                        maximum_length = 100 if k == 'name' else 50 if k == 'type' else None
                        v, text_valid = _normalize_appeal_patch_text(v, nullable=nullable, required=required, maximum_length=maximum_length)
                        if not text_valid:
                            _invalidate_contract()
                            continue
                        if k == 'description' and field_key in ('trait', 'skill'):
                            from plugins.adventurer_s1.services.s1_description_repair_contract import is_missing_asset_description
                            if is_missing_asset_description(field_key, v):
                                _invalidate_contract()
                                continue
                    if k == 'rarity':
                        if type(v) is str:
                            v = v.strip().lower()
                            if v == 'uncommon':
                                from plugins.adventurer_s1.services.adventurer_s1_parser_service import repair_illegal_rarity
                                v = repair_illegal_rarity(v)
                        if type(v) is not str or v not in _VALID_RARITIES:
                            _invalidate_contract()
                            continue
                        old_rank = _RARITY_RANK.get(original.get('rarity') or 'common', 0)
                        if _RARITY_RANK.get(v, 0) > old_rank and (not allow_rarity_increase):
                            from plugins.adventurer_s1.services.s1_appeal_rarity_restore_service import permits_restore
                            if not permits_restore(expedition_evidence, section, original, v, adventurer_id):
                                _invalidate_contract()
                                continue
                    if k == 'quantity' and v is None:
                        _invalidate_contract()
                        continue
                    if k in ('quantity', 'uses_left') and v is not None:
                        v = _normalize_appeal_patch_integer(v, minimum=0)
                        if v is None:
                            _invalidate_contract()
                            continue
                        old_value = semantic_quantity if k == 'quantity' else original.get(k)
                        if k == 'quantity' and allow_quantity_increase:
                            pass
                        elif k == 'quantity' and field_key in ('equipment', 'warehouse') and (quantity_norm_intent or AdventurerS1DataAppealService._is_quantity_field_normalization_update(working_item, original, section_has_deletes=section_has_deletes)):
                            pass
                    if k == 'recovery_minutes':
                        v = _normalize_appeal_patch_integer(v, minimum=0)
                        if v is None:
                            _invalidate_contract()
                            continue
                    if k == 'trigger_recovery':
                        v = _normalize_appeal_patch_boolean(v)
                        if v is None:
                            _invalidate_contract()
                            continue
                    if k == 'recovery_evidence_event_indexes':
                        if not isinstance(v, list) or not v:
                            _invalidate_contract()
                            continue
                        all_events = expedition_evidence.get('all_events') if isinstance(expedition_evidence, dict) else None
                        if not isinstance(all_events, list):
                            _invalidate_contract()
                            continue
                        indexes = []
                        seen_indexes = set()
                        invalid_indexes = len(v) > _MAX_RECOVERY_EVIDENCE_EVENT_INDEXES
                        for raw_index in v[:_MAX_RECOVERY_EVIDENCE_EVENT_INDEXES]:
                            event_index = _normalize_appeal_patch_integer(raw_index, minimum=0)
                            if event_index is None or event_index >= len(all_events):
                                invalid_indexes = True
                                continue
                            if event_index not in seen_indexes:
                                seen_indexes.add(event_index)
                                indexes.append(event_index)
                        if invalid_indexes or not indexes:
                            _invalidate_contract()
                            continue
                        v = indexes
                    if k == 'evolution_stage':
                        if v is None:
                            _invalidate_contract()
                            continue
                        v = _normalize_appeal_patch_integer(v)
                        if v is None:
                            _invalidate_contract()
                            continue
                        if v < 0:
                            v = 0
                            _invalidate_contract()
                        from plugins.adventurer_s1.s1_item_types import clamp_evolution_stage
                        if field_key == 'equipment':
                            clamped = clamp_evolution_stage(original.get('evolution_stage'), v)
                            if clamped != v:
                                _invalidate_contract()
                            v = clamped
                    if k == 'is_archived' or k == 'equipped':
                        v = _normalize_appeal_patch_boolean(v)
                        if v is None:
                            _invalidate_contract()
                            continue
                    cleaned[k] = v
                if field_key in ('equipment', 'skill'):
                    trigger_recovery = cleaned.get('trigger_recovery') is True
                    evidence_indexes = cleaned.get('recovery_evidence_event_indexes')
                    if trigger_recovery:
                        effective_minutes = cleaned.get('recovery_minutes', original.get('recovery_minutes'))
                        if type(effective_minutes) is not int or effective_minutes <= 0 or (not isinstance(evidence_indexes, list)) or (not evidence_indexes) or (original.get('recovering') is True) or (original.get('recovery_started_at') is not None) or (original.get('recovery_ends_at') is not None) or ('name' in cleaned and cleaned.get('name') != original.get('name')):
                            _invalidate_contract()
                            cleaned.pop('trigger_recovery', None)
                            cleaned.pop('recovery_evidence_event_indexes', None)
                    else:
                        cleaned.pop('trigger_recovery', None)
                        cleaned.pop('recovery_evidence_event_indexes', None)
                if field_key == 'trait':
                    recovery_text_valid = True
                    for text_field, nullable in (('recovery_policy', False), ('recovery_stage', True), ('recovered_desc', True)):
                        if text_field not in item:
                            continue
                        _, text_valid = _normalize_appeal_patch_text(item.get(text_field), nullable=nullable, required=not nullable)
                        if not text_valid:
                            _invalidate_contract()
                            recovery_text_valid = False
                    if not recovery_text_valid:
                        continue
                    recovery_updates = AdventurerS1DataAppealService._normalize_trait_recovery_updates(original, item)
                    if recovery_updates is None:
                        _invalidate_contract()
                        continue
                    for recovery_field in _TRAIT_RECOVERY_PATCH_FIELDS:
                        if recovery_field not in item:
                            continue
                        if recovery_field in ('recovery_progress', 'recovery_required', 'recovery_hours'):
                            raw_recovery_value = None if item.get(recovery_field) is None else _normalize_appeal_patch_integer(item.get(recovery_field))
                        else:
                            raw_recovery_value, _ = _normalize_appeal_patch_text(item.get(recovery_field), nullable=recovery_field in ('recovery_stage', 'recovered_desc'), required=recovery_field == 'recovery_policy')
                        if raw_recovery_value != recovery_updates.get(recovery_field):
                            _invalidate_contract()
                    cleaned.update(recovery_updates)
                preserve_unchanged = rid in (preserve_unchanged_update_ids or {}).get(section, set())
                if len(cleaned) > 1 and (preserve_unchanged or AdventurerS1DataAppealService._record_update_changes(original, cleaned)):
                    updates.append(cleaned)
                else:
                    _invalidate_contract()
            for item in delete_rows:
                raw_id = item.get('id') if isinstance(item, dict) else item
                rid = _normalize_appeal_patch_integer(raw_id, minimum=1)
                if rid is None or rid not in allowed_ids[section]:
                    _invalidate_contract()
                    continue
                original = record_maps[section].get(rid) or {}
                if field_key in ('equipment', 'warehouse') and AdventurerS1DataAppealService._appeal_record_text_locked(original, catalog_locked_names):
                    logger.warning('数据申诉 normalize 跳过不可变物品删除 %s id=%s appeal=%s', field_key, rid, appeal_id)
                    _invalidate_contract()
                    continue
                deletes.append({'id': rid})
            normalized[section] = {'update': updates, 'delete': deletes}
        destructive_create_counts = {'trait': 0, 'skill': 0}
        for action_section, source_section in (('trait_destructive_actions', 'traits'), ('skill_destructive_actions', 'skills')):
            source_kind = 'trait' if source_section == 'traits' else 'skill'
            if action_section not in raw_patch:
                continue
            actions = project_data_appeal_destructive_actions(action_section, raw_patch.get(action_section))
            if actions is None:
                _invalidate_contract()
                return _finish(None, f'{action_section} 格式无效')
            for raw_action in raw_patch.get(action_section) or []:
                if not isinstance(raw_action, dict):
                    _invalidate_contract()
                    continue
                reason = raw_action.get('reason')
                if type(reason) is not str or not reason.strip() or len(reason.strip()) > 500:
                    _invalidate_contract()
            frozen_source_ids = {int(row['id']) for row in snapshot_before.get(source_section) or [] if isinstance(row, dict) and row.get('id') is not None}
            normalized_actions = []
            for action in actions:
                if action['source_id'] not in frozen_source_ids:
                    _invalidate_contract()
                    continue
                target_kind = destructive_action_target_kind(action['mode'])
                target = copy.deepcopy(action.get('target')) if target_kind else None
                if target_kind:
                    target.setdefault('reason', action['reason'])
                    target_section = 'traits' if target_kind == 'trait' else 'skills'
                    target_id = target.get('id')
                    same_identity_replacement = target_kind == source_kind and (target_id is None or int(target_id) == int(action['source_id']))
                    if target_id is not None and int(target_id) not in {int(row['id']) for row in snapshot_before.get(target_section) or [] if isinstance(row, dict) and row.get('id') is not None}:
                        _invalidate_contract()
                        continue
                    target_boolean = None
                    target_boolean_field = 'equipped' if target_kind == 'skill' else 'is_archived'
                    if target_boolean_field in target:
                        target_boolean = _normalize_appeal_patch_boolean(target.get(target_boolean_field))
                        if target_boolean is None:
                            _invalidate_contract()
                            continue
                    if target_id is None and (not same_identity_replacement):
                        max_targets = max_create_traits if target_kind == 'trait' else max_create_skills
                        if destructive_create_counts[target_kind] >= max_targets:
                            _invalidate_contract()
                            continue
                        destructive_create_counts[target_kind] += 1
                    target_name = target.get('name')
                    target_description = target.get('description')
                    if type(target_name) is not str or not target_name.strip() or len(target_name.strip()) > 100 or (type(target_description) is not str) or (not target_description.strip()):
                        _invalidate_contract()
                        continue
                    normalized_target = {'name': target_name.strip(), 'description': target_description.strip()}
                    if target_id is not None:
                        normalized_target['id'] = int(target_id)
                    if target_kind == 'skill':
                        if target_boolean is not None:
                            normalized_target['equipped'] = target_boolean
                        elif target_id is None and (not same_identity_replacement):
                            normalized_target['equipped'] = True
                        if 'recovery_minutes' in target:
                            recovery_minutes = _normalize_appeal_patch_integer(target.get('recovery_minutes'), minimum=0)
                            if recovery_minutes is None:
                                _invalidate_contract()
                                continue
                            normalized_target['recovery_minutes'] = recovery_minutes
                        elif target_id is None and (not same_identity_replacement):
                            normalized_target['recovery_minutes'] = 0
                    else:
                        recovery = sanitize_trait_recovery_metadata_for_appeal({'policy': target.get('recovery_policy'), 'stage': target.get('recovery_stage'), 'required': target.get('recovery_required'), 'recovery_hours': target.get('recovery_hours'), 'recovered_desc': target.get('recovered_desc')})
                        if target_id is None and (not same_identity_replacement) or any((field in target for field in _TRAIT_RECOVERY_PATCH_FIELDS)):
                            normalized_target.update({'recovery_policy': recovery['recovery_policy'], 'recovery_stage': recovery['recovery_stage'], 'recovery_progress': 0, 'recovery_required': recovery['recovery_required'], 'recovery_hours': recovery.get('recovery_hours'), 'recovered_desc': recovery['recovered_desc']})
                        if target_boolean is not None:
                            normalized_target['is_archived'] = target_boolean
                    if same_identity_replacement:
                        candidate_update = dict(normalized_target)
                        candidate_update['id'] = int(action['source_id'])
                        original = record_maps[source_section].get(candidate_update['id']) or {}
                        original_name = str(original.get('name') or '').strip()
                        target_name = str(candidate_update.get('name') or '').strip()
                        colliding_ids = {int(record_id) for record_id, record in record_maps[target_section].items() if int(record_id) != candidate_update['id'] and str(record.get('name') or '').strip() == target_name}
                        if target_name != original_name and colliding_ids:
                            _invalidate_contract()
                            continue
                        source_updates = normalized[source_section]['update']
                        existing_update = next((row for row in source_updates if isinstance(row, dict) and row.get('id') == candidate_update['id']), None)
                        if existing_update is None:
                            merged_update = candidate_update
                        else:
                            conflicting_fields = {key for key, value in candidate_update.items() if key != 'id' and key in existing_update and (existing_update[key] != value)}
                            if conflicting_fields:
                                _invalidate_contract()
                                continue
                            merged_update = dict(existing_update)
                            merged_update.update(candidate_update)
                        if not AdventurerS1DataAppealService._record_update_changes(original, merged_update):
                            _invalidate_contract()
                            continue
                        if existing_update is None:
                            source_updates.append(merged_update)
                        else:
                            existing_update.clear()
                            existing_update.update(merged_update)
                        continue
                    normalized_target['reason'] = action['reason']
                    action = {**action, 'target': normalized_target}
                normalized_actions.append(action)
            normalized[action_section] = normalized_actions
        from flask import current_app, has_app_context
        max_create_items = max(0, int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_MAX_CREATE_ITEMS', 5) if has_app_context() else 5))
        create_items = []
        raw_create_items = raw_patch.get('create_items', _APPEAL_PATCH_MISSING)
        if isinstance(raw_create_items, list):
            max_create_items = len(raw_create_items)
            if len(raw_create_items) > max_create_items:
                _invalidate_contract()
            for item in raw_create_items[:max_create_items]:
                if not isinstance(item, dict):
                    _invalidate_contract()
                    continue
                name, name_valid = _normalize_appeal_patch_text(item.get('name', _APPEAL_PATCH_MISSING), required=True, maximum_length=100)
                destination, destination_valid = _normalize_appeal_patch_text(item.get('destination', _APPEAL_PATCH_MISSING), default='warehouse', required=False)
                rarity, rarity_valid = _normalize_appeal_patch_text(item.get('rarity', _APPEAL_PATCH_MISSING), default='common', required=False)
                if rarity_valid and rarity == 'uncommon':
                    from plugins.adventurer_s1.services.adventurer_s1_parser_service import repair_illegal_rarity
                    rarity = repair_illegal_rarity(rarity)
                item_type, type_valid = _normalize_appeal_patch_text(item.get('type', _APPEAL_PATCH_MISSING), default='', required=False, maximum_length=50)
                reason, reason_valid = _normalize_appeal_patch_text(item.get('reason', _APPEAL_PATCH_MISSING), default='', required=False)
                description_source = item.get('description') if 'description' in item else item.get('custom_desc', _APPEAL_PATCH_MISSING)
                description, description_valid = _normalize_appeal_patch_text(description_source, default='', required=False)
                if 'description' in item and 'custom_desc' in item:
                    _invalidate_contract()
                if not all((name_valid, destination_valid, rarity_valid, type_valid, reason_valid, description_valid)):
                    _invalidate_contract()
                if not name_valid or name is None:
                    continue
                if not destination_valid or destination not in _VALID_CREATE_DESTINATIONS:
                    _invalidate_contract()
                    continue
                if not rarity_valid or rarity not in _VALID_RARITIES:
                    _invalidate_contract()
                    continue
                raw_quantity = item.get('quantity', _APPEAL_PATCH_MISSING)
                if raw_quantity is _APPEAL_PATCH_MISSING:
                    raw_quantity = 1
                elif raw_quantity is None:
                    raw_quantity = 1
                    _invalidate_contract()
                quantity = _normalize_appeal_patch_integer(raw_quantity, minimum=1, maximum=None)
                if quantity is None:
                    _invalidate_contract()
                    continue
                uses_left = item.get('uses_left')
                if uses_left is not None:
                    uses_left = _normalize_appeal_patch_integer(uses_left, minimum=0)
                    if uses_left is None:
                        _invalidate_contract()
                        continue
                recovery_minutes = _normalize_appeal_patch_integer(item.get('recovery_minutes', 0), minimum=0)
                if recovery_minutes is None:
                    _invalidate_contract()
                    continue
                evolution_stage = item.get('evolution_stage')
                if evolution_stage is not None:
                    evolution_stage = _normalize_appeal_patch_integer(evolution_stage)
                    if evolution_stage is None:
                        _invalidate_contract()
                        continue
                    elif evolution_stage < 0:
                        evolution_stage = 0
                        _invalidate_contract()
                from plugins.adventurer_s1.s1_item_types import clamp_companion_evolution_stage, companion_allows_duplicate_name_instances
                if companion_allows_duplicate_name_instances(item_type):
                    stage_floor = AdventurerS1DataAppealService._deleted_companion_stage_floor_for_create(normalized, record_maps, name)
                    if evolution_stage is not None:
                        clamped = clamp_companion_evolution_stage(stage_floor, evolution_stage)
                        if clamped != evolution_stage:
                            _invalidate_contract()
                        evolution_stage = clamped
                    elif stage_floor > 0:
                        evolution_stage = stage_floor
                        _invalidate_contract()
                ownership = {}
                normalized_item = {'destination': destination, 'name': name, 'type': item_type or '', 'rarity': rarity, 'description': description or '', 'quantity': quantity, 'uses_left': uses_left, 'evolution_stage': evolution_stage, 'recovery_minutes': recovery_minutes, 'reason': reason or ''}
                normalized_item.update(ownership)
                create_items.append(normalized_item)
        elif raw_create_items is not _APPEAL_PATCH_MISSING:
            _invalidate_contract()
        normalized['create_items'] = create_items
        normalized, fold_warnings = AdventurerS1DataAppealService._fold_create_items_to_stack_updates(normalized, record_maps, raw_patch)
        if fold_warnings:
            logger.info('数据申诉 normalize create_items fold appeal=%s: %s', appeal_id, fold_warnings)
        create_traits = []
        folded_trait_updates: list[dict] = []
        raw_create_traits = raw_patch.get('create_traits', _APPEAL_PATCH_MISSING)
        if isinstance(raw_create_traits, list):
            if len(raw_create_traits) > max_create_traits:
                _invalidate_contract()
            existing_trait_name_to_id = {str(rec.get('name') or '').strip(): int(rid) for rid, rec in (record_maps.get('traits') or {}).items() if str(rec.get('name') or '').strip() and int(rid) in allowed_ids['traits']}
            existing_trait_name_to_record = {str(rec.get('name') or '').strip(): rec for rec in (record_maps.get('traits') or {}).values() if str(rec.get('name') or '').strip()}
            seen_trait_names = set()
            for item in raw_create_traits[:max_create_traits]:
                if not isinstance(item, dict):
                    _invalidate_contract()
                    continue
                name, name_valid = _normalize_appeal_patch_text(item.get('name', _APPEAL_PATCH_MISSING), required=True, maximum_length=100)
                description, description_valid = _normalize_appeal_patch_text(item.get('description', _APPEAL_PATCH_MISSING), required=True)
                reason, reason_valid = _normalize_appeal_patch_text(item.get('reason', _APPEAL_PATCH_MISSING), default='')
                if not all((name_valid, description_valid, reason_valid)):
                    _invalidate_contract()
                if not name_valid or not description_valid or name is None or (description is None):
                    continue
                recovery_text_invalid = False
                recovery_text_values: dict[str, str | None] = {}
                for field, nullable in (('recovery_policy', False), ('recovery_stage', True), ('recovered_desc', True)):
                    if field not in item:
                        continue
                    text_value, text_valid = _normalize_appeal_patch_text(item.get(field), nullable=nullable, required=not nullable)
                    if not text_valid:
                        recovery_text_invalid = True
                        _invalidate_contract()
                    else:
                        recovery_text_values[field] = text_value
                recovery_input = {'policy': recovery_text_values.get('recovery_policy'), 'stage': recovery_text_values.get('recovery_stage'), 'required': item.get('recovery_required'), 'recovery_hours': item.get('recovery_hours'), 'recovered_desc': recovery_text_values.get('recovered_desc')}
                invalid_recovery_integer = False
                for field, recovery_field in (('recovery_required', 'required'), ('recovery_hours', 'recovery_hours')):
                    if field not in item or item.get(field) is None:
                        continue
                    recovery_integer = _normalize_appeal_patch_integer(item.get(field))
                    if recovery_integer is None:
                        invalid_recovery_integer = True
                        _invalidate_contract()
                        break
                    recovery_input[recovery_field] = recovery_integer
                if invalid_recovery_integer:
                    continue
                recovery = sanitize_trait_recovery_metadata_for_appeal(recovery_input)
                if recovery_text_invalid:
                    recovery = sanitize_trait_recovery_metadata_for_appeal({})
                recovery_field_map = {'recovery_policy': 'recovery_policy', 'recovery_stage': 'recovery_stage', 'recovery_required': 'recovery_required', 'recovery_hours': 'recovery_hours', 'recovered_desc': 'recovered_desc'}
                for raw_field, normalized_field in recovery_field_map.items():
                    if raw_field not in item:
                        continue
                    if raw_field in ('recovery_required', 'recovery_hours'):
                        raw_value = _normalize_appeal_patch_integer(item.get(raw_field))
                    else:
                        raw_value = recovery_text_values.get(raw_field)
                    if raw_value != recovery.get(normalized_field):
                        _invalidate_contract()
                if name in existing_trait_name_to_id:
                    folded = {'id': existing_trait_name_to_id[name], 'description': description}
                    original_trait = existing_trait_name_to_record.get(name) or {}
                    if recovery['recovery_policy'] != 'none':
                        folded.update({'recovery_policy': recovery['recovery_policy'], 'recovery_stage': recovery['recovery_stage'], 'recovery_progress': 0, 'recovery_required': recovery['recovery_required'], 'recovery_hours': recovery.get('recovery_hours'), 'recovered_desc': recovery['recovered_desc']})
                    if AdventurerS1DataAppealService._record_update_changes(original_trait, folded):
                        folded_trait_updates.append(folded)
                    continue
                if name in seen_trait_names:
                    _invalidate_contract()
                    continue
                seen_trait_names.add(name)
                normalized_trait = {'name': name, 'description': description, 'recovery_policy': recovery['recovery_policy'], 'recovery_stage': recovery['recovery_stage'], 'recovery_progress': 0, 'recovery_required': recovery['recovery_required'], 'recovery_hours': recovery.get('recovery_hours'), 'recovered_desc': recovery['recovered_desc'], 'reason': reason or ''}
                ownership = {}
                normalized_trait.update(ownership)
                create_traits.append(normalized_trait)
        elif raw_create_traits is not _APPEAL_PATCH_MISSING:
            _invalidate_contract()
        normalized['create_traits'] = create_traits
        if folded_trait_updates:
            traits_sec = normalized.setdefault('traits', {'update': [], 'delete': []})
            update_by_id: dict[int, dict] = {}
            for upd in traits_sec.get('update') or []:
                if not isinstance(upd, dict) or upd.get('id') is None:
                    continue
                try:
                    update_by_id[int(upd['id'])] = dict(upd)
                except (TypeError, ValueError):
                    continue
            for folded in folded_trait_updates:
                try:
                    fid = int(folded['id'])
                except (TypeError, ValueError):
                    continue
                if fid in update_by_id:
                    update_by_id[fid].update({k: v for k, v in folded.items() if k != 'id'})
                else:
                    update_by_id[fid] = dict(folded)
            traits_sec['update'] = list(update_by_id.values())
        create_skills = []
        folded_skill_updates: list[dict] = []
        raw_create_skills = raw_patch.get('create_skills', _APPEAL_PATCH_MISSING)
        if isinstance(raw_create_skills, list):
            if len(raw_create_skills) > max_create_skills:
                _invalidate_contract()
            existing_skill_name_to_id = {str(rec.get('name') or '').strip(): int(rid) for rid, rec in (record_maps.get('skills') or {}).items() if str(rec.get('name') or '').strip()}
            seen_skill_names = set()
            for item in raw_create_skills[:max_create_skills]:
                if not isinstance(item, dict):
                    _invalidate_contract()
                    continue
                name, name_valid = _normalize_appeal_patch_text(item.get('name', _APPEAL_PATCH_MISSING), required=True, maximum_length=100)
                description, description_valid = _normalize_appeal_patch_text(item.get('description', _APPEAL_PATCH_MISSING), default='')
                reason, reason_valid = _normalize_appeal_patch_text(item.get('reason', _APPEAL_PATCH_MISSING), default='')
                if not all((name_valid, description_valid, reason_valid)):
                    _invalidate_contract()
                if not name_valid or name is None:
                    continue
                equipped = _normalize_appeal_patch_boolean(item.get('equipped', True))
                if equipped is None:
                    _invalidate_contract()
                    continue
                recovery_minutes = _normalize_appeal_patch_integer(item.get('recovery_minutes', 0), minimum=0)
                if recovery_minutes is None:
                    _invalidate_contract()
                    continue
                if name in existing_skill_name_to_id:
                    folded: dict = {'id': existing_skill_name_to_id[name]}
                    if description:
                        folded['description'] = description
                    if 'equipped' in item:
                        folded['equipped'] = equipped
                    if recovery_minutes > 0:
                        folded['recovery_minutes'] = recovery_minutes
                    folded_skill_updates.append(folded)
                    continue
                if name in seen_skill_names:
                    _invalidate_contract()
                    continue
                seen_skill_names.add(name)
                normalized_skill = {'name': name, 'description': description or '', 'equipped': equipped, 'recovery_minutes': recovery_minutes, 'reason': reason or ''}
                ownership = {}
                normalized_skill.update(ownership)
                create_skills.append(normalized_skill)
        elif raw_create_skills is not _APPEAL_PATCH_MISSING:
            _invalidate_contract()
        normalized['create_skills'] = create_skills
        if folded_skill_updates:
            skills_sec = normalized.setdefault('skills', {'update': [], 'delete': []})
            update_by_id: dict[int, dict] = {}
            for upd in skills_sec.get('update') or []:
                if not isinstance(upd, dict) or upd.get('id') is None:
                    continue
                try:
                    update_by_id[int(upd['id'])] = dict(upd)
                except (TypeError, ValueError):
                    continue
            for folded in folded_skill_updates:
                try:
                    fid = int(folded['id'])
                except (TypeError, ValueError):
                    continue
                if fid in update_by_id:
                    update_by_id[fid].update({k: v for k, v in folded.items() if k != 'id'})
                else:
                    update_by_id[fid] = dict(folded)
            skills_sec['update'] = list(update_by_id.values())
            normalized['skills'] = skills_sec
        max_currency_adjust = max(0.0, float(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_MAX_CURRENCY_ADJUST', 100000) if has_app_context() else 100000))
        currency_cap_remaining = None
        if narrative_currency_cap is not None:
            try:
                currency_cap_remaining = max(0.0, float(narrative_currency_cap))
            except (TypeError, ValueError):
                currency_cap_remaining = None
        currency_adjustments = []
        raw_currency = raw_patch.get('currency_adjustments', _APPEAL_PATCH_MISSING)
        if isinstance(raw_currency, list):
            for item in raw_currency:
                if not isinstance(item, dict):
                    _invalidate_contract()
                    continue
                item, currency_error = normalize_data_appeal_currency_adjustment(item)
                if currency_error:
                    _invalidate_contract()
                    continue
                currency_code, currency_code_valid = _normalize_appeal_patch_text(item.get('currency_code', _APPEAL_PATCH_MISSING), required=True)
                reason, reason_valid = _normalize_appeal_patch_text(item.get('reason', _APPEAL_PATCH_MISSING), default='')
                if not currency_code_valid or currency_code is None or currency_code.lower() != 'gold':
                    _invalidate_contract()
                    continue
                amount = _normalize_appeal_patch_number(item.get('amount'))
                if amount is None:
                    _invalidate_contract()
                    continue
                rounded_amount = round(amount, 2)
                if rounded_amount != amount:
                    _invalidate_contract()
                amount = rounded_amount
                if amount <= 0:
                    _invalidate_contract()
                    continue
                if currency_cap_remaining is not None:
                    if currency_cap_remaining <= 0:
                        _invalidate_contract()
                        continue
                    if amount > currency_cap_remaining:
                        amount = round(currency_cap_remaining, 2)
                        _invalidate_contract()
                    currency_cap_remaining -= amount
                if not reason_valid:
                    _invalidate_contract()
                currency_adjustments.append({'currency_code': 'gold', 'amount': amount, 'reason': reason or ''})
        elif raw_currency is not _APPEAL_PATCH_MISSING:
            _invalidate_contract()
        normalized['currency_adjustments'] = currency_adjustments
        team_pool = None
        raw_team_pool = raw_patch.get('team_pool', _APPEAL_PATCH_MISSING)
        if isinstance(raw_team_pool, dict):
            pool_total = _normalize_appeal_patch_number(raw_team_pool.get('total_amount'))
            if pool_total is None:
                _invalidate_contract()
                pool_total = 0.0
            rounded_pool_total = round(pool_total, 2)
            if rounded_pool_total != pool_total:
                _invalidate_contract()
            pool_total = rounded_pool_total
            pool_reason, pool_reason_valid = _normalize_appeal_patch_text(raw_team_pool.get('reason', _APPEAL_PATCH_MISSING), default='')
            if not pool_reason_valid:
                _invalidate_contract()
            team_pool_cap = float(_NARRATIVE_CURRENCY_CAP_DEFAULT)
            if has_app_context():
                try:
                    team_pool_cap = float(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_NARRATIVE_CURRENCY_CAP', _NARRATIVE_CURRENCY_CAP_DEFAULT))
                except (TypeError, ValueError):
                    team_pool_cap = float(_NARRATIVE_CURRENCY_CAP_DEFAULT)
            capped_pool_total = pool_total
            if capped_pool_total != pool_total:
                _invalidate_contract()
            pool_total = capped_pool_total
            if pool_total > 0:
                team_pool = {'currency_code': 'gold', 'total_amount': round(pool_total, 2), 'reason': pool_reason or ''}
            else:
                _invalidate_contract()
        elif raw_team_pool not in (_APPEAL_PATCH_MISSING, None):
            _invalidate_contract()
        normalized['team_pool'] = team_pool
        if 'unassigned_items' in raw_patch:
            from plugins.adventurer_s1.services.s1_team_loot_contract import normalize_team_loot
            unassigned, loot_errors = normalize_team_loot(raw_patch['unassigned_items'], events=(expedition_evidence or {}).get('all_events') or [], path='patch.unassigned_items')
            if loot_errors:
                _invalidate_contract()
                return _finish(None, '共同实物提案无效：' + ';'.join(loot_errors))
            normalized['unassigned_items'] = unassigned
        tavern_subsidies = []
        raw_subsidy = raw_patch.get('tavern_subsidy', _APPEAL_PATCH_MISSING)
        if isinstance(raw_subsidy, list) and any((not isinstance(row, dict) for row in raw_subsidy)):
            _invalidate_contract()
        elif raw_subsidy not in (_APPEAL_PATCH_MISSING, None) and (not isinstance(raw_subsidy, (dict, list))):
            _invalidate_contract()
        raw_subsidy_rows = AdventurerS1DataAppealService._iter_tavern_subsidies(raw_subsidy)
        seen_subsidy_scopes = set()
        for raw_subsidy in raw_subsidy_rows:
            subsidy_cap_pp = AdventurerS1DataAppealService._tavern_subsidy_cap_per_person()
            scope, scope_valid = _normalize_appeal_patch_text(raw_subsidy.get('scope', _APPEAL_PATCH_MISSING), default='individual')
            if not scope_valid or scope is None:
                _invalidate_contract()
                scope = 'individual'
            else:
                scope = scope.lower()
                if scope not in ('individual', 'team'):
                    _invalidate_contract()
                    scope = 'individual'
            if scope in seen_subsidy_scopes:
                _invalidate_contract()
                continue
            subsidy_count_before = len(tavern_subsidies)
            reason, reason_valid = _normalize_appeal_patch_text(raw_subsidy.get('reason', _APPEAL_PATCH_MISSING), default='')
            if not reason_valid:
                _invalidate_contract()
            present_total_fields = [field for field in ('total_amount', 'gold', 'amount') if raw_subsidy.get(field) is not None]
            if len(present_total_fields) > 1:
                _invalidate_contract()
            _subsidy_total = raw_subsidy.get(present_total_fields[0]) if present_total_fields else None
            if scope == 'team':
                allocations = []
                raw_allocs = raw_subsidy.get('allocations')
                if isinstance(raw_allocs, list):
                    seen_ids = set()
                    for alloc in raw_allocs:
                        if not isinstance(alloc, dict):
                            _invalidate_contract()
                            continue
                        aid = _normalize_appeal_patch_integer(alloc.get('adventurer_id'), minimum=1)
                        if aid is None:
                            _invalidate_contract()
                            continue
                        if aid in seen_ids:
                            _invalidate_contract()
                            continue
                        amt = _normalize_appeal_patch_number(alloc.get('amount'))
                        if amt is None:
                            _invalidate_contract()
                            continue
                        rounded_amt = round(amt, 2)
                        if rounded_amt != amt:
                            _invalidate_contract()
                        amt = rounded_amt
                        capped_amt = amt
                        if capped_amt != amt:
                            _invalidate_contract()
                        amt = capped_amt
                        if amt <= 0:
                            _invalidate_contract()
                            continue
                        alloc_reason, alloc_reason_valid = _normalize_appeal_patch_text(alloc.get('reason', _APPEAL_PATCH_MISSING), default='')
                        if not alloc_reason_valid:
                            _invalidate_contract()
                        seen_ids.add(aid)
                        allocations.append({'adventurer_id': aid, 'amount': amt, 'reason': alloc_reason or ''})
                elif raw_allocs is not None:
                    _invalidate_contract()
                if allocations:
                    tavern_subsidies.append({'scope': 'team', 'currency_code': 'gold', 'allocations': allocations, 'reason': reason or ''})
                else:
                    subsidy_amount = _normalize_appeal_patch_number(_subsidy_total)
                    if subsidy_amount is None:
                        _invalidate_contract()
                        subsidy_amount = 0.0
                    rounded_amount = round(subsidy_amount, 2)
                    if rounded_amount != subsidy_amount:
                        _invalidate_contract()
                    subsidy_amount = rounded_amount
                    capped_amount = subsidy_amount
                    if capped_amount != subsidy_amount:
                        _invalidate_contract()
                    subsidy_amount = capped_amount
                    if subsidy_amount > 0:
                        tavern_subsidies.append({'scope': 'team', 'currency_code': 'gold', 'total_amount': round(subsidy_amount, 2), 'reason': reason or ''})
                    else:
                        _invalidate_contract()
            else:
                subsidy_amount = _normalize_appeal_patch_number(_subsidy_total)
                if subsidy_amount is None:
                    _invalidate_contract()
                    subsidy_amount = 0.0
                rounded_amount = round(subsidy_amount, 2)
                if rounded_amount != subsidy_amount:
                    _invalidate_contract()
                subsidy_amount = rounded_amount
                capped_amount = subsidy_amount
                if capped_amount != subsidy_amount:
                    _invalidate_contract()
                subsidy_amount = capped_amount
                if subsidy_amount > 0:
                    tavern_subsidies.append({'scope': 'individual', 'currency_code': 'gold', 'total_amount': round(subsidy_amount, 2), 'reason': reason or ''})
                else:
                    _invalidate_contract()
            if len(tavern_subsidies) > subsidy_count_before:
                seen_subsidy_scopes.add(scope)
        normalized['tavern_subsidy'] = tavern_subsidies[0] if len(tavern_subsidies) == 1 else tavern_subsidies if tavern_subsidies else None
        max_reclaim_items = max(0, int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_MAX_RECLAIM_ITEMS', _MAX_RECLAIM_ITEMS_DEFAULT) if has_app_context() else _MAX_RECLAIM_ITEMS_DEFAULT))
        max_reclaim_qty = max(1, int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_MAX_RECLAIM_QUANTITY', _MAX_RECLAIM_QUANTITY_DEFAULT) if has_app_context() else _MAX_RECLAIM_QUANTITY_DEFAULT))
        reclaim_items = []
        raw_reclaim_items = raw_patch.get('reclaim_items', _APPEAL_PATCH_MISSING)
        if isinstance(raw_reclaim_items, list):
            max_reclaim_items = len(raw_reclaim_items)
            if len(raw_reclaim_items) > max_reclaim_items:
                _invalidate_contract()
            seen_reclaim_keys = set()
            for item in raw_reclaim_items[:max_reclaim_items]:
                if not isinstance(item, dict):
                    _invalidate_contract()
                    continue
                if 'name' in item and 'item_name' in item:
                    raw_name = item.get('name')
                    if item.get('name') != item.get('item_name'):
                        _invalidate_contract()
                elif 'name' in item:
                    raw_name = item.get('name')
                else:
                    raw_name = item.get('item_name', _APPEAL_PATCH_MISSING)
                name, name_valid = _normalize_appeal_patch_text(raw_name, required=True, maximum_length=100)
                if not name_valid or name is None:
                    _invalidate_contract()
                    continue
                from_id = _normalize_appeal_patch_integer(item.get('from_adventurer_id'), minimum=1)
                if from_id is None:
                    _invalidate_contract()
                    continue
                raw_quantity = item.get('quantity', _APPEAL_PATCH_MISSING)
                if raw_quantity is _APPEAL_PATCH_MISSING:
                    raw_quantity = 1
                elif raw_quantity is None:
                    raw_quantity = 1
                    _invalidate_contract()
                quantity = _normalize_appeal_patch_integer(raw_quantity, minimum=1, maximum=None)
                if quantity is None:
                    _invalidate_contract()
                    continue
                destination, destination_valid = _normalize_appeal_patch_text(item.get('destination', _APPEAL_PATCH_MISSING), default='warehouse')
                if not destination_valid or destination is None:
                    _invalidate_contract()
                    destination = 'warehouse'
                else:
                    destination = destination.lower()
                if destination not in _VALID_CREATE_DESTINATIONS:
                    destination = 'warehouse'
                    _invalidate_contract()
                reason, reason_valid = _normalize_appeal_patch_text(item.get('reason', _APPEAL_PATCH_MISSING), default='')
                if not reason_valid:
                    _invalidate_contract()
                asset_kind = item.get('asset_kind', 'equipment')
                asset_id = item.get('asset_id')
                if asset_kind not in {'equipment', 'skill', 'trait'}:
                    _invalidate_contract()
                    continue
                if asset_kind in {'skill', 'trait'}:
                    if type(asset_id) is not int or asset_id <= 0 or quantity != 1:
                        _invalidate_contract()
                        continue
                dedupe_key = (asset_kind, asset_id, name, from_id)
                if dedupe_key in seen_reclaim_keys:
                    _invalidate_contract()
                    continue
                seen_reclaim_keys.add(dedupe_key)
                reclaim_items.append({**({'asset_kind': asset_kind, 'asset_id': asset_id} if asset_kind in {'skill', 'trait'} else {}), 'name': name, 'from_adventurer_id': from_id, 'quantity': quantity, 'destination': destination, 'reason': reason or ''})
        elif raw_reclaim_items is not _APPEAL_PATCH_MISSING:
            _invalidate_contract()
        normalized['reclaim_items'] = reclaim_items
        normalized, delete_strip_warnings = AdventurerS1DataAppealService._sanitize_uncompensated_deletes(normalized, trait_record_map=record_maps.get('traits'), record_maps=record_maps)
        if delete_strip_warnings:
            _invalidate_contract()
            logger.warning('数据申诉 normalize 剥离无补偿 delete: %s', delete_strip_warnings)
        normalized, phantom_strip_warnings = AdventurerS1DataAppealService._sanitize_phantom_expedition_deletes(normalized, record_maps=record_maps, expedition_evidence=expedition_evidence)
        if phantom_strip_warnings:
            _invalidate_contract()
            logger.warning('数据申诉 normalize 拦截 phantom expedition delete: %s', phantom_strip_warnings)
        normalized_before_quantity_restore = copy.deepcopy(normalized)
        AdventurerS1DataAppealService._restore_dropped_item_quantities_in_normalized(raw_patch, normalized, snapshot_before, expedition_evidence=expedition_evidence)
        if normalized != normalized_before_quantity_restore:
            _invalidate_contract()
        for section in ('equipment', 'warehouse'):
            sec = normalized.get(section) or {}
            records = record_maps.get(section) or {}
            filtered_updates = []
            for upd in sec.get('update') or []:
                if not isinstance(upd, dict) or upd.get('id') is None:
                    _invalidate_contract()
                    continue
                try:
                    rid = int(upd.get('id'))
                except (TypeError, ValueError):
                    _invalidate_contract()
                    continue
                if AdventurerS1DataAppealService._appeal_record_text_locked(records.get(rid) or {}, catalog_locked_names):
                    _invalidate_contract()
                    continue
                filtered_updates.append(upd)
            sec['update'] = filtered_updates
            filtered_deletes = []
            for item in sec.get('delete') or []:
                raw_id = item.get('id') if isinstance(item, dict) else item
                try:
                    rid = int(raw_id)
                except (TypeError, ValueError):
                    _invalidate_contract()
                    continue
                if AdventurerS1DataAppealService._appeal_record_text_locked(records.get(rid) or {}, catalog_locked_names):
                    _invalidate_contract()
                    continue
                filtered_deletes.append({'id': rid})
            sec['delete'] = filtered_deletes
            normalized[section] = sec
        destructive_create_remaining = {'trait': max(0, max_create_traits - len(normalized.get('create_traits') or [])), 'skill': max(0, max_create_skills - len(normalized.get('create_skills') or []))}
        for action_section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            kept_actions = []
            for action in normalized.get(action_section) or []:
                target_kind = destructive_action_target_kind(action.get('mode'))
                target = action.get('target') if isinstance(action.get('target'), dict) else None
                if target_kind and target and (target.get('id') is None):
                    if destructive_create_remaining[target_kind] <= 0:
                        _invalidate_contract()
                        continue
                    destructive_create_remaining[target_kind] -= 1
                kept_actions.append(action)
            normalized[action_section] = kept_actions
        has_op = any((normalized[s]['update'] or normalized[s]['delete'] for s in ('traits', 'equipment', 'warehouse', 'skills'))) or bool(normalized.get('create_items') or normalized.get('create_traits') or normalized.get('create_skills') or normalized.get('currency_adjustments') or normalized.get('team_pool') or normalized.get('unassigned_items') or normalized.get('tavern_subsidy') or normalized.get('reclaim_items') or normalized.get('trait_destructive_actions') or normalized.get('skill_destructive_actions'))
        if not has_op:
            return _finish(None, 'patch 中没有有效操作')
        return _finish(normalized, None)

    @staticmethod
    def build_preview(snapshot_before: dict, patch: dict) -> dict:
        """生成管理员预览：before / after / changes 列表。"""
        after = copy.deepcopy(snapshot_before)
        changes = []

        def _apply_section(section: str, record_type: str):
            sec_patch = patch.get(section) or {}
            id_to_idx = {r['id']: i for i, r in enumerate(after.get(section) or [])}
            for upd in sec_patch.get('update') or []:
                rid = upd['id']
                idx = id_to_idx.get(rid)
                if idx is None:
                    continue
                before_rec = after[section][idx]
                merged = copy.deepcopy(before_rec)
                for k, v in upd.items():
                    if k != 'id':
                        merged[k] = v
                for k, v in upd.items():
                    if k == 'id':
                        continue
                    if before_rec.get(k) != v:
                        changes.append({'action': 'update', 'type': record_type, 'id': rid, 'field': k, 'before': before_rec.get(k), 'after': v})
                after[section][idx] = merged
            delete_ids = {d['id'] for d in sec_patch.get('delete') or []}
            if delete_ids:
                for rid in delete_ids:
                    rec = next((r for r in snapshot_before.get(section) or [] if r['id'] == rid), None)
                    if rec:
                        changes.append({'action': 'delete', 'type': record_type, 'id': rid, 'record': rec})
                after[section] = [r for r in after.get(section, []) if r['id'] not in delete_ids]
        _apply_section('traits', 'trait')
        _apply_section('equipment', 'equipment')
        _apply_section('warehouse', 'warehouse')
        _apply_section('skills', 'skill')
        for item in patch.get('create_items') or []:
            changes.append({'action': 'create', 'type': 'item', 'after': item})
        for item in patch.get('create_traits') or []:
            changes.append({'action': 'create', 'type': 'trait', 'after': item})
        for item in patch.get('create_skills') or []:
            changes.append({'action': 'create', 'type': 'skill', 'after': item})
        for item in patch.get('currency_adjustments') or []:
            changes.append({'action': 'add', 'type': 'currency', 'after': item})
        if patch.get('team_pool'):
            changes.append({'action': 'distribute', 'type': 'team_pool', 'after': patch['team_pool']})
        for item in patch.get('unassigned_items') or []:
            changes.append({'action': 'register', 'type': 'team_loot', 'after': item})
        if patch.get('tavern_subsidy'):
            changes.append({'action': 'distribute', 'type': 'tavern_subsidy', 'after': patch['tavern_subsidy']})
        for item in patch.get('reclaim_items') or []:
            changes.append({'action': 'reclaim', 'type': 'item', 'after': item})
        for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            source_type = destructive_action_source_kind(section)
            for item in patch.get(section) or []:
                changes.append({'action': 'destructive', 'type': source_type, 'id': item.get('source_id'), 'mode': item.get('mode'), 'after': item.get('target')})
        return {'before': snapshot_before, 'after': after, 'changes': changes}

    @staticmethod
    def _has_privileged_operations(patch: dict | None) -> bool:
        """Only these high-risk grant/transfer operations trigger the jury-2 AI review."""
        return bool(isinstance(patch, dict) and any((patch.get(key) for key in _DATA_APPEAL_PRIVILEGED_PATCH_KEYS)))

    @staticmethod
    def _has_rejected_expedition_item_request(request_checklist: list | None, expedition_evidence: dict | None) -> bool:
        """探险物品/数量诉求被一审拒绝时仍强制进入独立复核。"""
        if not isinstance(expedition_evidence, dict):
            return False
        try:
            if int(expedition_evidence.get('expedition_log_id')) <= 0:
                return False
        except (TypeError, ValueError):
            return False
        return any((isinstance(row, dict) and str(row.get('status') or '').strip().lower() == 'rejected' and (str(row.get('category') or '').strip().lower() in {'item', 'quantity'}) for row in request_checklist or []))

    @staticmethod
    def _player_note_has_instruction_injection(player_note: str | None) -> bool:
        """Detect player-controlled text that tries to look like model/system instructions."""
        text = str(player_note or '').strip().lower()
        return bool(text and any((marker in text for marker in _APPEAL_INSTRUCTION_INJECTION_MARKERS)))

    @staticmethod
    def _patch_has_trait_archive_updates(patch: dict | None) -> bool:
        if not isinstance(patch, dict):
            return False
        traits = patch.get('traits') if isinstance(patch.get('traits'), dict) else {}
        for update in traits.get('update') or []:
            if isinstance(update, dict) and 'is_archived' in update:
                return True
        return False

    @staticmethod
    def _patch_has_description_rewrite_updates(patch: dict | None) -> bool:
        if not isinstance(patch, dict):
            return False
        checks = (('traits', {'name', 'description', 'recovered_desc'}), ('skills', {'name', 'description'}), ('equipment', {'name', 'custom_desc', 'description', 'damage_note'}), ('warehouse', {'name', 'custom_desc', 'description'}))
        for section, fields in checks:
            block = patch.get(section) if isinstance(patch.get(section), dict) else {}
            for update in block.get('update') or []:
                if isinstance(update, dict) and any((field in update for field in fields)):
                    return True
        return False

    @staticmethod
    def _patch_has_asset_recovery_updates(patch: dict | None) -> bool:
        if not isinstance(patch, dict):
            return False
        for section in ('equipment', 'warehouse', 'skills'):
            block = patch.get(section) if isinstance(patch.get(section), dict) else {}
            for update in block.get('update') or []:
                if isinstance(update, dict) and any((field in update for field in ('recovery_minutes', 'trigger_recovery', 'recovery_evidence_event_indexes'))):
                    return True
        return any((isinstance(row, dict) and 'recovery_minutes' in row for section in ('create_items', 'create_skills') for row in patch.get(section) or []))

    @staticmethod
    def _patch_has_trait_recovery_updates(patch: dict | None) -> bool:
        if not isinstance(patch, dict):
            return False
        traits = patch.get('traits') if isinstance(patch.get('traits'), dict) else {}
        return any((isinstance(update, dict) and any((field in update for field in _TRAIT_RECOVERY_PATCH_FIELDS)) for update in traits.get('update') or []))

    @staticmethod
    def _trait_recovery_ease_rank(record: dict | None) -> int:
        record = record if isinstance(record, dict) else {}
        policy = str(record.get('recovery_policy') or 'none').strip().lower()
        stage = str(record.get('recovery_stage') or '').strip().lower()
        if policy in NON_NATURAL_RECOVERY_POLICIES:
            return _TRAIT_RECOVERY_EASE_RANK['heavy']
        if policy in NATURAL_RECOVERY_POLICIES and stage in ('trace', 'light', 'medium'):
            return _TRAIT_RECOVERY_EASE_RANK[stage]
        return _TRAIT_RECOVERY_EASE_RANK['none']

    @staticmethod
    def _patch_makes_trait_recovery_easier(patch: dict | None, snapshot_before: dict | None) -> bool:
        if not isinstance(patch, dict):
            return False
        original_by_id: dict[int, dict] = {}
        for record in (snapshot_before or {}).get('traits') or []:
            if not isinstance(record, dict) or record.get('id') is None:
                continue
            try:
                original_by_id[int(record['id'])] = record
            except (TypeError, ValueError):
                continue
        traits = patch.get('traits') if isinstance(patch.get('traits'), dict) else {}
        for update in traits.get('update') or []:
            if not isinstance(update, dict) or not _TRAIT_RECOVERY_PATCH_FIELDS & set(update):
                continue
            try:
                rid = int(update.get('id'))
            except (TypeError, ValueError):
                continue
            original = original_by_id.get(rid)
            if not original:
                continue
            merged = dict(original)
            merged.update({k: v for k, v in update.items() if k != 'id'})
            old_rank = AdventurerS1DataAppealService._trait_recovery_ease_rank(original)
            new_rank = AdventurerS1DataAppealService._trait_recovery_ease_rank(merged)
            if new_rank < old_rank:
                return True
            if new_rank == old_rank and new_rank in (_TRAIT_RECOVERY_EASE_RANK['light'], _TRAIT_RECOVERY_EASE_RANK['medium']):
                try:
                    old_required = int(original.get('recovery_required') or 0)
                    new_required = int(merged.get('recovery_required') or 0)
                except (TypeError, ValueError):
                    continue
                if new_required > 0 and old_required > 0 and (new_required < old_required):
                    return True
        return False

    @staticmethod
    def _trait_update_is_recovery_only(update: dict) -> bool:
        if not isinstance(update, dict):
            return False
        fields = {k for k in update if k != 'id'}
        if not fields:
            return False
        allowed = _TRAIT_RECOVERY_PATCH_FIELDS | {'is_archived'}
        return fields <= allowed

    @staticmethod
    def _focus_ref_trait_ids(focus_refs: dict | None, snapshot_before: dict | None) -> set[int]:
        targets: set[int] = set()
        traits = list((snapshot_before or {}).get('traits') or [])
        name_to_id = {str(row.get('name') or '').strip(): int(row['id']) for row in traits if row.get('id') is not None and str(row.get('name') or '').strip()}
        for name in (focus_refs or {}).get('traits') or []:
            key = str(name or '').strip()
            if key in name_to_id:
                targets.add(name_to_id[key])
        return targets

    @staticmethod
    def _normalized_trait_description_changes(patch: dict | None, snapshot_before: dict | None) -> dict[int, str]:
        if not isinstance(patch, dict):
            return {}
        record_map = {int(row['id']): row for row in (snapshot_before or {}).get('traits') or [] if row.get('id') is not None}
        changes: dict[int, str] = {}
        for update in (patch.get('traits') or {}).get('update') or []:
            if not isinstance(update, dict) or 'description' not in update or 'id' not in update:
                continue
            try:
                rid = int(update['id'])
            except (TypeError, ValueError):
                continue
            original = record_map.get(rid) or {}
            new_desc = str(update.get('description') or '').strip()
            old_desc = str(original.get('description') or '').strip()
            if new_desc != old_desc:
                changes[rid] = new_desc
        return changes

    @staticmethod
    def _raw_trait_description_noop_drops(raw_patch: dict | None, normalized_patch: dict | None, snapshot_before: dict | None) -> list[int]:
        if not isinstance(raw_patch, dict):
            return []
        record_map = {int(row['id']): row for row in (snapshot_before or {}).get('traits') or [] if row.get('id') is not None}
        dropped: list[int] = []
        for update in (raw_patch.get('traits') or {}).get('update') or []:
            if not isinstance(update, dict) or 'description' not in update or 'id' not in update:
                continue
            try:
                rid = int(update['id'])
            except (TypeError, ValueError):
                continue
            original = record_map.get(rid) or {}
            if str(original.get('description') or '').strip() == str(update.get('description') or '').strip():
                dropped.append(rid)
        return dropped

    @staticmethod
    def _normalized_skill_description_changes(patch: dict | None, snapshot_before: dict | None) -> dict[int, str]:
        if not isinstance(patch, dict):
            return {}
        record_map = {int(row['id']): row for row in (snapshot_before or {}).get('skills') or [] if row.get('id') is not None}
        changes: dict[int, str] = {}
        for update in (patch.get('skills') or {}).get('update') or []:
            if not isinstance(update, dict) or 'description' not in update or 'id' not in update:
                continue
            try:
                rid = int(update['id'])
            except (TypeError, ValueError):
                continue
            original = record_map.get(rid) or {}
            new_desc = str(update.get('description') or '').strip()
            old_desc = str(original.get('description') or '').strip()
            if new_desc != old_desc:
                changes[rid] = new_desc
        return changes

    @staticmethod
    def _raw_patch_has_trait_description_field(raw_patch: dict | None) -> bool:
        if not isinstance(raw_patch, dict):
            return False
        return any((isinstance(update, dict) and 'description' in update for update in (raw_patch.get('traits') or {}).get('update') or []))

    @staticmethod
    def audit_trait_description_landing(*, raw_patch: dict | None, normalized_patch: dict | None, snapshot_before: dict | None, focus_refs: dict | None=None) -> list[dict]:
        """纯 patch 结构审计：traits.update.description 未真正落库，不解析玩家/AI 自然语言。"""
        normalized_patch = normalized_patch if isinstance(normalized_patch, dict) else {}
        norm_desc_changes = AdventurerS1DataAppealService._normalized_trait_description_changes(normalized_patch, snapshot_before)
        norm_skill_desc_changes = AdventurerS1DataAppealService._normalized_skill_description_changes(normalized_patch, snapshot_before)
        dropped_ids = AdventurerS1DataAppealService._raw_trait_description_noop_drops(raw_patch, normalized_patch, snapshot_before)
        focus_trait_ids = AdventurerS1DataAppealService._focus_ref_trait_ids(focus_refs, snapshot_before)
        raw_trait_desc_field = AdventurerS1DataAppealService._raw_patch_has_trait_description_field(raw_patch)
        issues: list[dict] = []
        if dropped_ids:
            issues.append({'code': 'trait_desc_unchanged_noop', 'trait_ids': dropped_ids, 'detail': 'raw patch 含 traits.update.description 但与快照相同，规范化后未落库'})
            norm_trait_updates = {int(update['id']): update for update in (normalized_patch.get('traits') or {}).get('update') or [] if isinstance(update, dict) and update.get('id') is not None}
            trait_record_map = {int(row['id']): row for row in (snapshot_before or {}).get('traits') or [] if isinstance(row, dict) and row.get('id') is not None}
            recovery_with_noop: list[int] = []
            for rid in dropped_ids:
                update = norm_trait_updates.get(rid)
                if not isinstance(update, dict):
                    continue
                original = trait_record_map.get(rid) or {}
                effective_update = {key: value for key, value in update.items() if key == 'id' or original.get(key) != value}
                if AdventurerS1DataAppealService._trait_update_is_recovery_only(effective_update):
                    recovery_with_noop.append(rid)
            if recovery_with_noop:
                issues.append({'code': 'trait_desc_noop_with_recovery_only', 'trait_ids': recovery_with_noop, 'detail': 'traits.update.description 与快照相同被丢弃，同条仅写了 recovery_*'})
        if norm_skill_desc_changes and (not norm_desc_changes):
            should_flag_wrong_target = raw_trait_desc_field or bool(focus_trait_ids)
            if should_flag_wrong_target:
                unresolved = sorted(focus_trait_ids - set(norm_desc_changes)) if focus_trait_ids else []
                issues.append({'code': 'trait_desc_wrong_target_skill', 'trait_ids': unresolved, 'detail': '规范化 patch 有 skills.update.description 变更，但无 traits.update.description 有效变更'})
        return issues

    @staticmethod
    def _normalized_item_update(normalized_patch: dict | None, section: str, record_id: int) -> dict | None:
        if not isinstance(normalized_patch, dict):
            return None
        sec = normalized_patch.get(section)
        if not isinstance(sec, dict):
            return None
        for update in sec.get('update') or []:
            if not isinstance(update, dict) or update.get('id') is None:
                continue
            update_id = _normalize_appeal_patch_integer(update['id'], minimum=1)
            if update_id == record_id:
                return update
        return None

    @staticmethod
    def _raw_item_update(raw_patch: dict | None, section: str, record_id: int) -> dict | None:
        if not isinstance(raw_patch, dict):
            return None
        sec = raw_patch.get(section)
        if not isinstance(sec, dict):
            return None
        for update in sec.get('update') or []:
            if not isinstance(update, dict) or update.get('id') is None:
                continue
            try:
                if int(update['id']) == int(record_id):
                    return update
            except (TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _collect_item_update_ids(raw_patch: dict | None, normalized_patch: dict | None) -> dict[str, set[int]]:
        ids_by_section: dict[str, set[int]] = {'equipment': set(), 'warehouse': set()}
        for section in ids_by_section:
            for source in (raw_patch, normalized_patch):
                if not isinstance(source, dict):
                    continue
                sec = source.get(section)
                if not isinstance(sec, dict):
                    continue
                for update in sec.get('update') or []:
                    if not isinstance(update, dict) or update.get('id') is None:
                        continue
                    record_id = _normalize_appeal_patch_integer(update['id'], minimum=1)
                    if record_id is not None:
                        ids_by_section[section].add(record_id)
        return ids_by_section

    @staticmethod
    def _best_embedded_quantity_before(original: dict) -> int | None:
        from plugins.adventurer_s1.services.s1_item_quantity_service import parse_embedded_quantity
        best: int | None = None
        for field in ('name', 'custom_desc'):
            _, parsed_qty, found = parse_embedded_quantity(str(original.get(field) or ''))
            if not found or parsed_qty is None or parsed_qty < 1:
                continue
            if best is None or parsed_qty > best:
                best = parsed_qty
        return best

    @staticmethod
    def _item_update_touches_identity(original: dict, raw_update: dict | None, normalized_update: dict | None) -> bool:
        if isinstance(raw_update, dict):
            if any((k in raw_update for k in ('name', 'custom_desc', 'description'))):
                return True
        if not isinstance(normalized_update, dict):
            return False
        if 'name' in normalized_update:
            new_name = str(normalized_update.get('name') or '').strip()
            old_name = str(original.get('name') or '').strip()
            if new_name != old_name:
                return True
        if 'custom_desc' in normalized_update:
            new_desc = str(normalized_update.get('custom_desc') or '').strip()
            old_desc = str(original.get('custom_desc') or '').strip()
            if new_desc != old_desc:
                return True
        return False

    @staticmethod
    def _normalized_item_quantity_value(normalized_update: dict | None, *, fallback: int) -> int:
        if not isinstance(normalized_update, dict) or 'quantity' not in normalized_update:
            return fallback
        try:
            return max(0, int(normalized_update.get('quantity')))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def audit_item_quantity_landing(*, raw_patch: dict | None, normalized_patch: dict | None, snapshot_before: dict | None) -> list[dict]:
        """纯 patch 结构审计：equipment/warehouse 数量归位未随同 name/custom_desc 落库。"""
        normalized_patch = normalized_patch if isinstance(normalized_patch, dict) else {}
        raw_patch = raw_patch if isinstance(raw_patch, dict) else {}
        snapshot_before = snapshot_before if isinstance(snapshot_before, dict) else {}
        issues: list[dict] = []
        seen: set[tuple[str, int, str]] = set()

        def _append(section: str, record_id: int, code: str, detail: str) -> None:
            key = (section, record_id, code)
            if key in seen:
                return
            seen.add(key)
            issues.append({'code': code, 'section': section, 'item_ids': [record_id], 'detail': detail})
        for section in ('equipment', 'warehouse'):
            record_map = {int(row['id']): row for row in snapshot_before.get(section) or [] if isinstance(row, dict) and row.get('id') is not None}
            for record_id in sorted(AdventurerS1DataAppealService._collect_item_update_ids(raw_patch, normalized_patch).get(section) or ()):
                original = record_map.get(record_id)
                if not original:
                    continue
                old_qty = AdventurerS1DataAppealService._snapshot_item_quantity(original.get('quantity'))
                raw_update = AdventurerS1DataAppealService._raw_item_update(raw_patch, section, record_id)
                normalized_update = AdventurerS1DataAppealService._normalized_item_update(normalized_patch, section, record_id)
                if isinstance(raw_update, dict) and 'quantity' in raw_update:
                    raw_qty = _normalize_appeal_patch_integer(raw_update['quantity'], minimum=0)
                    if raw_qty is not None and raw_qty != old_qty:
                        norm_qty = AdventurerS1DataAppealService._normalized_item_quantity_value(normalized_update, fallback=old_qty)
                        if normalized_update is None or norm_qty == old_qty:
                            _append(section, record_id, 'item_qty_raw_intent_not_landed', 'raw patch 写了 quantity 但与快照不同，规范化后未落库')
                if not AdventurerS1DataAppealService._item_update_touches_identity(original, raw_update, normalized_update):
                    continue
                embedded_qty = AdventurerS1DataAppealService._best_embedded_quantity_before(original)
                if embedded_qty is None or embedded_qty == old_qty:
                    continue
                norm_qty = AdventurerS1DataAppealService._normalized_item_quantity_value(normalized_update, fallback=old_qty)
                if norm_qty != old_qty:
                    continue
                _append(section, record_id, 'item_qty_migration_incomplete', 'name/custom_desc 已准备数量归位，但 quantity 字段未随同修正')
        return issues

    @staticmethod
    def apply_item_quantity_audit(ai_raw: dict, issues: list[dict]) -> dict:
        if not issues:
            return ai_raw
        result = dict(ai_raw)
        result['core_request_satisfied'] = False
        warnings = list(result.get('warnings') or [])
        for issue in issues:
            warnings.append(f"item_quantity_audit:{issue.get('code')}:{issue.get('detail')}")
        if warnings:
            result['warnings'] = warnings
        result['item_quantity_audit'] = issues
        code_set = {str(issue.get('code') or '') for issue in issues}
        if 'item_qty_raw_intent_not_landed' in code_set:
            msg = '您申诉的物品数量修正未能在数据中生效：方案里写了数量变更，但实际只改了名称或描述，堆叠数量仍是原值；请核对背包/随身装备后必要时重提。'
        else:
            msg = '您申诉的物品数量归位未完成：名称或描述已调整，但 quantity 字段未同步修正，堆叠数量仍是原值；请核对条目后必要时重提。'
        existing = AdventurerS1DataAppealService._normalize_player_reject_reason(result.get('player_reject_reason') or result.get('reject_reason_for_player'))
        if AdventurerS1DataAppealService._looks_like_success_reason(existing):
            result['player_reject_reason'] = msg
        elif msg not in existing:
            result['player_reject_reason'] = f'{existing} {msg}'.strip() if existing else msg
        return result

    @staticmethod
    def apply_trait_description_audit(ai_raw: dict, issues: list[dict]) -> dict:
        if not issues:
            return ai_raw
        result = dict(ai_raw)
        result['core_request_satisfied'] = False
        warnings = list(result.get('warnings') or [])
        for issue in issues:
            warnings.append(f"trait_description_audit:{issue.get('code')}:{issue.get('detail')}")
        if warnings:
            result['warnings'] = warnings
        result['trait_description_audit'] = issues
        code_set = {str(issue.get('code') or '') for issue in issues}
        if 'trait_desc_wrong_target_skill' in code_set:
            msg = '您要求修改的是特质描述，但本次方案只调整了技能描述，对应特质正文未变更；请打开特质列表核对，必要时重提并指明要改的特质。'
        elif 'trait_desc_unchanged_noop' in code_set:
            msg = '经核对，拟修改的特质描述与当前数据一致，实际未作变更；若仍认为有误，请指明具体词句后重提。'
        elif 'trait_desc_noop_with_recovery_only' in code_set:
            msg = '拟修改的特质描述与当前一致未落库，本次仅调整了恢复标签；若您要改的是描述正文，请重提并给出应写入 traits.update.description 的新文案。'
        else:
            msg = '您申诉的特质描述修改未能在数据中生效，请核对特质列表后必要时重提。'
        existing = AdventurerS1DataAppealService._normalize_player_reject_reason(result.get('player_reject_reason') or result.get('reject_reason_for_player'))
        if AdventurerS1DataAppealService._looks_like_success_reason(existing):
            result['player_reject_reason'] = msg
        elif msg not in existing:
            result['player_reject_reason'] = f'{existing} {msg}'.strip() if existing else msg
        return result

    @staticmethod
    def _patch_only_cosmetic_cleanup(patch: dict | None) -> bool:
        """patch 是否「只做了无收益的整理」：无任何高权限发放、无数量增加，
        仅 update 描述/字段归位 或 delete（去重合并）。用于识别"假通过"。

        有 create_*/currency/team_pool/tavern_subsidy → 实质发放，False。
        update 里出现 quantity 增加 → 收益类，False（此处只看"是否存在收益操作"，
        quantity 是否合规由 normalize_patch 把关）。
        """
        if not isinstance(patch, dict):
            return False
        if AdventurerS1DataAppealService._has_privileged_operations(patch):
            return False
        has_cleanup = False
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            sec = patch.get(section) or {}
            updates = sec.get('update') or []
            deletes = sec.get('delete') or []
            if updates or deletes:
                has_cleanup = True
            for upd in updates:
                if isinstance(upd, dict) and 'quantity' in upd:
                    return False
        return has_cleanup

    @staticmethod
    def _resolve_core_request_satisfied(appeal: AdventurerS1DataAppeal) -> bool | None:
        """判定本次 approved 是否实质满足了玩家核心诉求。

        主判据：1 号自报的 core_request_satisfied（它最清楚玩家诉求是什么、有没有给到）。
        后端兜底：1 号缺报该字段时，若本次最终只做了无收益的整理（纯描述清理/去重）且
        1 号给了 player_reject_reason（说明它驳了某项核心诉求），按"假通过"处理。
        返回 None 表示无法判定（按通过处理，不打扰玩家）。
        """
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        request_results = review_raw.get('request_results')
        if isinstance(request_results, list):
            core_results = [row for row in request_results if isinstance(row, dict) and row.get('is_core') is True]
            if core_results:
                return all((row.get('status') == 'done' for row in core_results))
        if isinstance(review_raw.get('create_item_execution_skips'), list) and any((isinstance(row, dict) for row in review_raw['create_item_execution_skips'])):
            return False
        raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
        applied = appeal.review_supplement_patch or {}
        supplement_grants = AdventurerS1DataAppealService._has_privileged_operations(applied)
        checklist = raw.get('request_checklist') if isinstance(raw.get('request_checklist'), list) else []
        if supplement_grants and checklist and (not any((isinstance(row, dict) and str(row.get('status') or '').strip().lower() == 'rejected' for row in checklist))):
            return True
        trait_audit = raw.get('trait_description_audit')
        if isinstance(trait_audit, list) and trait_audit:
            return False
        item_qty_audit = raw.get('item_quantity_audit')
        if isinstance(item_qty_audit, list) and item_qty_audit:
            return False
        delete_comp_audit = raw.get('delete_compensation_audit')
        if isinstance(delete_comp_audit, list) and delete_comp_audit:
            return False
        phantom_delete_audit = raw.get('phantom_expedition_delete_audit')
        if isinstance(phantom_delete_audit, list) and phantom_delete_audit:
            return False
        flag = raw.get('core_request_satisfied')
        if isinstance(flag, bool):
            return flag
        proposed = appeal.proposed_patch or {}
        if supplement_grants:
            return True
        only_cleanup = AdventurerS1DataAppealService._patch_only_cosmetic_cleanup(proposed) and (not AdventurerS1DataAppealService._has_privileged_operations(proposed))
        has_reject_reason = bool(AdventurerS1DataAppealService._normalize_player_reject_reason(raw.get('player_reject_reason') or raw.get('reject_reason_for_player')))
        if only_cleanup and has_reject_reason:
            return False
        return None

    @staticmethod
    def _strip_privileged_operations(patch: dict | None) -> dict:
        """返回去掉高权限操作（create_items / create_traits / create_skills / currency_adjustments / team_pool / tavern_subsidy / reclaim_items）的 patch 深拷贝。"""
        stripped = copy.deepcopy(patch) if isinstance(patch, dict) else {}
        stripped['create_items'] = []
        stripped['create_traits'] = []
        stripped['create_skills'] = []
        stripped['currency_adjustments'] = []
        stripped['team_pool'] = None
        stripped['unassigned_items'] = []
        stripped['tavern_subsidy'] = None
        stripped['reclaim_items'] = []
        for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            stripped[section] = []
        return stripped

    @staticmethod
    def _patch_has_operations(patch: dict | None) -> bool:
        """patch 是否还有任何可执行操作（普通改删或高权限发放）。"""
        if not isinstance(patch, dict):
            return False
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            sec = patch.get(section) or {}
            if sec.get('update') or sec.get('delete'):
                return True
        return bool(patch.get('create_items') or patch.get('create_traits') or patch.get('create_skills') or patch.get('currency_adjustments') or patch.get('team_pool') or patch.get('unassigned_items') or patch.get('tavern_subsidy') or patch.get('reclaim_items') or patch.get('trait_destructive_actions') or patch.get('skill_destructive_actions'))

    @staticmethod
    def _emit_data_appeal_changed(appeal: AdventurerS1DataAppeal) -> None:
        try:
            from plugins.adventurer_s1.services.s1_socket_service import deployment_s1_realm, emit_s1_to_user
            emit_s1_to_user(deployment_s1_realm(), appeal.user_id, 'data_appeal_changed', {'appeal_id': appeal.id, 'user_id': appeal.user_id, 'status': appeal.status, 'ai_status': appeal.ai_status, 'player_review_status': appeal.player_review_status, 'revert_offer_status': appeal.revert_offer_status or 'none'})
        except Exception:
            pass

    @staticmethod
    def _load_appeal(appeal_id: int, *, refresh: bool=False, for_update: bool=False):
        query = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id)
        if for_update:
            query = query.with_for_update()
        if refresh:
            query = query.populate_existing()
        return query.first()

    @staticmethod
    def _mark_ai_interrupted(appeal_id: int, reason: str, *, claim_token: str | None=None) -> None:
        """AI 中途异常时按退避重试；预算耗尽后进入技术失败终态。"""
        db_manager.rollback()
        appeal = AdventurerS1DataAppealService._reload_appeal_for_ai_write(appeal_id, claim_token, for_update=True)
        if not appeal:
            return
        resume_stage = AdventurerS1DataAppealService._appeal_resume_stage(appeal)
        if resume_stage:
            pending_stage = resume_stage
        elif appeal.review_ai_status == 'done':
            pending_stage = 'execution'
        elif appeal.ai_status == 'done' and appeal.ai_raw_response:
            pending_stage = 'review_ai'
        else:
            pending_stage = 'primary_ai'
        if pending_stage == 'primary_ai':
            appeal.ai_summary = reason
            appeal.review_ai_summary = '主申诉 AI 处理未完成，未进入独立复核'
        elif pending_stage == 'review_ai':
            appeal.review_ai_summary = reason
        AdventurerS1DataAppealService._mark_appeal_resolution_pending(appeal, '本次处理异常中断，申诉仍在处理中，将自动从未完成阶段继续；本轮未对数据做改动，也未据此驳回您的诉求。', stage=pending_stage)
        ok, err = db_manager.commit()
        if not ok:
            logger.error('数据申诉中断落库失败 appeal=%s: %s', appeal_id, err)
            return
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)

    @staticmethod
    def _apply_supplement_and_distributions(appeal: AdventurerS1DataAppeal, *, all_player_records: dict | None, proposed_team_pool: dict | None, proposed_tavern_subsidy: dict | list | None, proposed_reclaim_items: list | None=None, apply_supplement_patch: bool=True) -> tuple:
        """应用陪审团 2 号补充 patch（改删/创造/补金币）+ 团队池/酒馆补贴/物品追回。

        - 2 号补充的普通+高权限操作走 _apply_patch；新合同下已先按自身 operation_id verdict 过滤，旧工单保持兼容。
        - 团队池/酒馆补贴/追回：proposed 来源由调用方按 verdict/复核闸门决定是否传入；supplement 同样只能执行已授权内容。
        - 各分发器自身幂等（团队池每副本一次、补贴按副本/冒险者去重、追回按 appeal+物品+来源去重），proposed 与 supplement 同源不会重复发。
        """
        supplement = appeal.review_supplement_patch if isinstance(appeal.review_supplement_patch, dict) else None
        if apply_supplement_patch and supplement and AdventurerS1DataAppealService._patch_has_operations(supplement):
            _, err = AdventurerS1DataAppealService._apply_patch(appeal.adventurer_id, supplement, appeal.snapshot_before, appeal_id=appeal.id, all_player_records=all_player_records, allow_quantity_increase=True)
            if err:
                return (None, err)
        for pool in (proposed_team_pool, (supplement or {}).get('team_pool')):
            if pool:
                _, err = AdventurerS1DataAppealService._apply_team_pool_distribution(appeal, pool)
                if err:
                    return (None, err)
        for raw_subsidy in (proposed_tavern_subsidy, (supplement or {}).get('tavern_subsidy')):
            for subsidy in AdventurerS1DataAppealService._iter_tavern_subsidies(raw_subsidy):
                _, err = AdventurerS1DataAppealService._apply_tavern_subsidy(appeal, subsidy)
                if err:
                    return (None, err)
        for reclaims in (proposed_reclaim_items, (supplement or {}).get('reclaim_items')):
            if reclaims:
                _, err = AdventurerS1DataAppealService._apply_reclaim_items(appeal, reclaims)
                if err:
                    return (None, err)
        return (True, None)

    @staticmethod
    def _rehydrate_authorized_review_execution_patch(appeal: AdventurerS1DataAppeal, *, all_player_records: dict) -> tuple[dict | None, str | None]:
        """Rebuild missing reviewer writes from frozen authorization, then revalidate.

        This is an execution-stage compatibility path for old appeals whose
        authorized operation was stripped by a backend guard that has since
        been corrected.  Raw reviewer JSON is never replayed directly: only
        still-allowed, not-yet-successful operation IDs are projected and run
        through the current ownership and patch validators again.
        """
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        authored_patches = review_raw.get('_backend_authorized_review_operation_patches')
        authored_operations = review_raw.get('_backend_authorized_review_operations')
        verdicts = review_raw.get('verdicts')
        if review_raw.get('verdict_contract_version') != 2 or not isinstance(authored_patches, dict) or (not isinstance(authored_operations, list)) or (not isinstance(verdicts, list)):
            return (appeal.review_supplement_patch, None)
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions, filter_data_appeal_clear_fields_by_operation_ids, filter_data_appeal_patch_by_operation_ids
        decisions = effective_data_appeal_operation_decisions(verdicts)
        successful_ids = AdventurerS1DataAppealService._successful_operation_receipt_ids(appeal)
        authored_ids = {str(operation.get('operation_id') or '').strip() for operation in authored_operations if isinstance(operation, dict) and str(operation.get('source') or '').strip() in ('supplement', 'override', 'clear') and str(operation.get('operation_id') or '').strip()}
        pending_allowed_ids = {operation_id for operation_id in authored_ids if decisions.get(operation_id) == 'allow' and operation_id not in successful_ids}
        if not pending_allowed_ids:
            return (appeal.review_supplement_patch, None)
        raw_supplement = filter_data_appeal_patch_by_operation_ids(authored_patches.get('supplement'), source='supplement', allowed_operation_ids=pending_allowed_ids)
        raw_override = filter_data_appeal_patch_by_operation_ids(authored_patches.get('override'), source='override', allowed_operation_ids=pending_allowed_ids)
        raw_clear_fields = filter_data_appeal_clear_fields_by_operation_ids(authored_patches.get('clear_fields'), allowed_operation_ids=pending_allowed_ids)
        candidate = raw_supplement if isinstance(raw_supplement, dict) else {}
        if isinstance(raw_override, dict):
            candidate = AdventurerS1DataAppealService._merge_data_appeal_patches(candidate, raw_override, allow_field_overwrite=True)
        clear_patch = AdventurerS1DataAppealService._build_clear_fields_patch(raw_clear_fields)
        if clear_patch:
            candidate = AdventurerS1DataAppealService._merge_data_appeal_patches(candidate, clear_patch, allow_field_overwrite=True)
        if not AdventurerS1DataAppealService._patch_has_operations(candidate):
            return (appeal.review_supplement_patch, None)
        evidence = copy.deepcopy(appeal.expedition_evidence) if isinstance(appeal.expedition_evidence, dict) else None
        expedition_log_id = AdventurerS1DataAppealService._resolve_expedition_log_id_for_appeal(appeal, evidence)
        party_roster = AdventurerS1DataAppealService._build_party_roster(expedition_log_id)
        recent_activity = AdventurerS1DataAppealService._build_recent_item_activity(appeal.adventurer_id, appealed_expedition_log_id=expedition_log_id)
        appeal_adventurer = db_manager.query(AdventurerS1).filter(AdventurerS1.id == appeal.adventurer_id).first()
        if not appeal_adventurer:
            return (appeal.review_supplement_patch, '申诉冒险者不存在，不能重建补救操作')
        appeal_adventurer_projection = {'adventurer_id': appeal_adventurer.id, 'name': appeal_adventurer.name}
        candidate, removed_cross_grants = AdventurerS1DataAppealService.strip_cross_adventurer_self_grants(candidate, appeal_adventurer_id=appeal.adventurer_id, party_roster=party_roster)
        candidate, removed_unsupported_grants = AdventurerS1DataAppealService.strip_unsupported_expedition_self_grants(candidate, appeal_adventurer=appeal_adventurer_projection, expedition_evidence=evidence, party_roster=party_roster, recent_activity=recent_activity)
        candidate, blocked_direct_deletes = strip_uncontracted_trait_skill_deletes(candidate)
        if not AdventurerS1DataAppealService._patch_has_operations(candidate):
            blocked = removed_cross_grants or removed_unsupported_grants or blocked_direct_deletes
            return (appeal.review_supplement_patch, '历史授权操作未通过当前所有权或破坏性操作门禁' if blocked else None)
        narrative_currency_cap = 0 if (appeal.appeal_category or 'other') == 'data_fix' else AdventurerS1DataAppealService._narrative_currency_cap_for_evidence(evidence)
        contract_status: dict = {}
        normalized, normalize_error = AdventurerS1DataAppealService.normalize_patch(candidate, appeal.snapshot_before or {}, all_player_records=all_player_records, narrative_currency_cap=narrative_currency_cap, allow_quantity_increase=True, allow_rarity_increase=AdventurerS1DataAppealService._should_allow_expedition_rarity_increase(candidate, appeal.snapshot_before or {}, appeal.adventurer_id, expedition_log_id), expedition_evidence=evidence, allow_objective_name_update_ids=AdventurerS1DataAppealService._collect_objective_name_update_ids(review_raw), allow_custom_desc_clear_ids=AdventurerS1DataAppealService._collect_custom_desc_clear_ids(review_raw), adventurer_id=appeal.adventurer_id, appeal_id=appeal.id, contract_status_out=contract_status)
        if normalize_error or contract_status.get('contract_valid') is not True or (not AdventurerS1DataAppealService._patch_has_operations(normalized)):
            return (appeal.review_supplement_patch, normalize_error or '历史授权操作未通过当前补丁契约')
        existing = copy.deepcopy(appeal.review_supplement_patch) if isinstance(appeal.review_supplement_patch, dict) else {}
        normalized = copy.deepcopy(normalized)
        for section in ('create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'reclaim_items', *DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS):
            existing_rows = existing.get(section) if isinstance(existing.get(section), list) else []
            candidate_rows = normalized.get(section)
            if isinstance(candidate_rows, list):
                normalized[section] = [row for row in candidate_rows if row not in existing_rows]
        merged = AdventurerS1DataAppealService._merge_data_appeal_patches(existing, normalized, allow_field_overwrite=True)
        return (merged, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def _try_auto_finalize(appeal_id: int, *, claim_token: str | None=None) -> None:
        """AI 完成后按逐项裁决执行；技术失败按预算重试并显式收口。

        裁决表（v2 优先使用 request_verdicts + operation verdicts）：
        1. 无有效 patch → 按逐诉求语义结果通过、驳回或保持待处理。
        2. v2 复核完整 → 按原子组独立执行 allow 操作并回写审计回执；
           reject 操作整行零写入，结构/服务失败则全部零写入并保持待处理。
        3. 复核 done 且无 verdicts（旧格式兼容）：
           - 整单 allow → 执行整单。
           - 整单 reject → 剥离高权限后执行普通改删；无操作则驳回。
        4. 任一版本复核未完成 → 全部零写入并由 AI 自动重试；预算耗尽后
           进入技术失败终态，不伪装成业务审核结论。

        正常 AI worker 必须持有当前数据库租约；``awaiting_review`` 仅兼容
        历史工单和显式人工接管入口，不是普通申诉的中间状态。
        """
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().populate_existing().first()
        if not appeal:
            return
        if claim_token:
            if appeal.status != 'processing' or appeal.ai_claim_token != claim_token:
                return
        elif appeal.status != 'awaiting_review':
            return
        if appeal.review_ai_model == AdventurerS1DataAppealService._MANUAL_TAKEOVER_REVIEW_MODEL:
            logger.info('数据申诉人工接管等待显式批准 appeal=%s', appeal_id)
            return
        identity_err = AdventurerS1DataAppealService._appeal_identity_error(appeal)
        if identity_err:
            appeal.status = 'rejected'
            appeal.reviewed_by = None
            appeal.reviewed_at = _portable_utc_now()
            appeal.reject_reason = identity_err
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=False)
            logger.warning('数据申诉身份门禁自动驳回 appeal=%s: %s', appeal_id, identity_err)
            return

        def _reject(reason: str, *, warn: bool=False) -> None:
            appeal.status = 'rejected'
            appeal.reviewed_by = None
            appeal.reviewed_at = _portable_utc_now()
            appeal.reject_reason = reason
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=False)
            (logger.warning if warn else logger.info)('数据申诉自动驳回 appeal=%s', appeal_id)

        def _request_results() -> list[dict]:
            return AdventurerS1DataAppealService._appeal_request_results(appeal)

        def _has_pending_requests() -> bool:
            return any((row.get('status') == 'pending' for row in _request_results()))

        def _all_requests_done() -> bool:
            rows = _request_results()
            return bool(rows) and all((row.get('status') == 'done' for row in rows))

        def _defer(reason: str, *, stage: str, execution_error: str | None=None, operation_errors: dict[str, str] | None=None, warn: bool=True) -> None:
            AdventurerS1DataAppealService._store_operation_receipts_and_request_results(appeal, execution_error=execution_error, operation_errors=operation_errors)
            AdventurerS1DataAppealService._mark_appeal_resolution_pending(appeal, reason, stage=stage)
            (logger.warning if warn else logger.info)('数据申诉阶段未完成 appeal=%s stage=%s status=%s ai_status=%s', appeal_id, stage, appeal.status, appeal.ai_status)

        def _approve_already_satisfied(message: str) -> None:
            appeal.status = 'approved'
            appeal.reviewed_by = None
            appeal.reviewed_at = _portable_utc_now()
            appeal.applied_at = _portable_utc_now()
            appeal.reject_reason = None
            appeal.core_request_satisfied = True
            resolved_review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
            for key in ('resolution_state', 'pending_stage', '_backend_resolution_state', '_backend_pending_stage', '_backend_execution_error'):
                resolved_review_raw.pop(key, None)
            appeal.review_ai_raw_response = json_dumps_safe(resolved_review_raw)
            AdventurerS1DataAppealService._set_review_execution_note(appeal, message)
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=True, auto=True)
            logger.info('数据申诉同案已满足零写入通过 appeal=%s', appeal_id)

        def _approve_completed_siblings(message: str) -> None:
            """Finalize a mixed result when an earlier retry already landed siblings."""
            appeal.status = 'approved'
            appeal.reviewed_by = None
            appeal.reviewed_at = _portable_utc_now()
            appeal.applied_at = appeal.applied_at or _portable_utc_now()
            appeal.reject_reason = None
            AdventurerS1DataAppealService._set_review_execution_note(appeal, message)
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=True, auto=True)
            logger.info('数据申诉无新写入收口为逐项部分通过 appeal=%s', appeal_id)

        def _cancel_as_withdrawal(reason: str) -> None:
            appeal.status = 'cancelled'
            appeal.reviewed_by = None
            appeal.reviewed_at = _portable_utc_now()
            appeal.reject_reason = reason
            AdventurerS1DataAppealService._notify_player_cancelled(appeal, auto_commit=False)
            logger.info('数据申诉玩家撤回 appeal=%s', appeal_id)

        def _approve_recovery_already_satisfied(message: str) -> None:
            AdventurerS1DataAppealService._mark_appeal_recovery_already_satisfied(appeal, message)
            appeal.status = 'approved'
            appeal.reviewed_by = None
            appeal.reviewed_at = _portable_utc_now()
            appeal.applied_at = _portable_utc_now()
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=True, auto=True)
            logger.info('数据申诉 recovery 已满足零操作通过 appeal=%s', appeal_id)

        def _finalize_empty_patch() -> bool:
            """无有效 patch 时的终态：玩家撤回 → cancelled，否则驳回。返回是否已处理。"""
            if AdventurerS1DataAppealService._is_player_withdrawal_intent(appeal):
                _cancel_as_withdrawal(AdventurerS1DataAppealService._withdrawal_cancel_reason(appeal))
                return True
            raw_patches = AdventurerS1DataAppealService._collect_appeal_raw_recovery_patches(appeal)
            satisfied, msg = AdventurerS1DataAppealService._appeal_recovery_metadata_already_satisfied(appeal.snapshot_before, *raw_patches)
            if satisfied and msg:
                _approve_recovery_already_satisfied(msg)
                return True
            _reject(AdventurerS1DataAppealService.get_suggested_reject_reason(appeal))
            return True
        all_player_records_for_finalize: dict | None = None
        stored_review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        prior_execution_receipts = stored_review_raw.get('operation_receipts') if isinstance(stored_review_raw.get('operation_receipts'), list) else []
        should_rehydrate_review_patch = appeal.review_ai_status == 'done' and stored_review_raw.get('verdict_contract_version') == 2 and any((isinstance(receipt, dict) and receipt.get('decision') == 'allow' and (receipt.get('outcome') in ('conflict', 'not_applied')) for receipt in prior_execution_receipts))
        if should_rehydrate_review_patch:
            all_player_records_for_finalize = AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True)
            rehydrated_patch, rehydrate_error = AdventurerS1DataAppealService._rehydrate_authorized_review_execution_patch(appeal, all_player_records=all_player_records_for_finalize)
            if rehydrate_error:
                logger.info('数据申诉历史二审补丁未重建 appeal=%s: %s', appeal.id, rehydrate_error)
            elif rehydrated_patch != appeal.review_supplement_patch:
                appeal.review_supplement_patch = rehydrated_patch
                logger.info('数据申诉从冻结授权重建待执行二审补丁 appeal=%s', appeal.id)
        has_review_supplement = AdventurerS1DataAppealService._patch_has_operations(appeal.review_supplement_patch)
        if appeal.ai_status != 'done' and (not has_review_supplement):
            _defer('本次处理未形成可核对的逐项结论，申诉已保留等待重新处理；本轮未对数据做改动，也未据此驳回您的诉求。', stage='primary_ai')
            return
        if not appeal.proposed_patch:
            if not AdventurerS1DataAppealService._patch_has_operations(appeal.review_supplement_patch):
                empty_review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
                empty_review_decision = str(empty_review_raw.get('decision') or '').strip().lower()
                primary_raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
                primary_checklist = primary_raw.get('request_checklist')
                primary_checklist = primary_checklist if isinstance(primary_checklist, list) else []
                if primary_checklist and all((isinstance(row, dict) and str(row.get('status') or '').strip().lower() == 'done' and (str(row.get('resolution_basis') or '').strip() == 'already_satisfied_same_case') for row in primary_checklist)):
                    _approve_already_satisfied('同一被申诉事件的精确回执已证明全部诉求完成，本次无需重复写入。')
                    return
                if AdventurerS1DataAppealService._is_player_withdrawal_intent(appeal):
                    _finalize_empty_patch()
                    return
                recovery_patches = AdventurerS1DataAppealService._collect_appeal_raw_recovery_patches(appeal)
                recovery_satisfied, recovery_message = AdventurerS1DataAppealService._appeal_recovery_metadata_already_satisfied(appeal.snapshot_before, *recovery_patches)
                if recovery_satisfied and recovery_message:
                    _approve_recovery_already_satisfied(recovery_message)
                    return
                primary_all_semantically_rejected = bool(primary_checklist) and all((isinstance(row, dict) and str(row.get('status') or '').strip().lower() == 'rejected' for row in primary_checklist))
                if empty_review_raw.get('verdict_contract_version') == 2:
                    if appeal.review_ai_status != 'done' or empty_review_decision not in ('allow', 'reject', 'pending') or (not isinstance(empty_review_raw.get('request_verdicts'), list)) or (not isinstance(empty_review_raw.get('verdicts'), list)) or (not isinstance(empty_review_raw.get('_backend_operation_manifest'), list)):
                        _defer('本次逐项复核未完成，申诉已保留等待重新处理；本轮未对数据做改动，也未据此驳回您的诉求。', stage='review_ai')
                        return
                    AdventurerS1DataAppealService._mark_operation_receipt_execution_start(appeal)
                    AdventurerS1DataAppealService._store_operation_receipts_and_request_results(appeal)
                    if empty_review_decision == 'pending' or _has_pending_requests():
                        _defer(AdventurerS1DataAppealService._get_review_player_reason(appeal) or '该项依据尚未被否定，但当前尚未形成可安全执行的修复；已保留待处理。', stage='review_ai')
                        return
                    if _all_requests_done():
                        _approve_already_satisfied(AdventurerS1DataAppealService._get_review_player_reason(appeal) or '同一被申诉事件的精确回执已证明相关诉求完成，本次无需重复写入。')
                        return
                    if any((row.get('status') in ('done', 'partial') for row in _request_results())):
                        _approve_completed_siblings(AdventurerS1DataAppealService._get_review_player_reason(appeal) or '已完成有明确依据的独立项；其余项按各自证据分别驳回。')
                        return
                    _reject(AdventurerS1DataAppealService._get_review_player_reason(appeal) or AdventurerS1DataAppealService.get_suggested_reject_reason(appeal))
                    return
                if appeal.review_ai_status == 'done' and primary_all_semantically_rejected and (empty_review_decision == 'reject' or ((appeal.appeal_category or '') == 'rarity_review' and empty_review_decision == 'allow' and (empty_review_raw.get('skipped_review') is True))):
                    _finalize_empty_patch()
                    return
                if appeal.review_ai_status != 'done' or empty_review_decision != 'reject':
                    _defer('本次尚未形成可执行修复或明确语义驳回，申诉已保留待处理；本轮未对数据做改动。', stage='review_ai')
                    return
                _finalize_empty_patch()
                return
            AdventurerS1DataAppealService._mark_operation_receipt_execution_start(appeal)
            supp_records = all_player_records_for_finalize or AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True)
            early_review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
            early_verdicts = AdventurerS1DataAppealService._get_review_verdicts(appeal)
            early_v2 = early_review_raw.get('verdict_contract_version') == 2
            early_review_decision = str(early_review_raw.get('decision') or '').strip().lower()
            if early_v2 and (appeal.review_ai_status != 'done' or early_review_decision not in ('allow', 'reject', 'pending') or (not early_verdicts) or (not isinstance(early_review_raw.get('request_verdicts'), list)) or (not isinstance(early_review_raw.get('_backend_operation_manifest'), list)) or any((not isinstance(verdict, dict) or not verdict.get('operation_id') for verdict in early_verdicts))):
                _defer('本次逐项复核未完成，申诉已保留等待重新处理；本轮未对数据做改动，也未据此驳回您的诉求。', stage='review_ai')
                return
            operation_errors: dict[str, str] = {}
            if early_v2:
                successful_ids, operation_errors, _ = AdventurerS1DataAppealService._apply_operation_contract_review_groups(appeal, appeal.review_supplement_patch, early_verdicts or [], all_player_records=supp_records)
                AdventurerS1DataAppealService._store_operation_receipts_and_request_results(appeal, operation_errors=operation_errors)
                successful_ids = AdventurerS1DataAppealService._successful_operation_receipt_ids(appeal)
                supp_err = None if successful_ids else '；'.join(dict.fromkeys(operation_errors.values())) or '补救操作没有可对应的最终写入'
            else:
                _, supp_err = AdventurerS1DataAppealService._apply_supplement_and_distributions(appeal, all_player_records=supp_records, proposed_team_pool=None, proposed_tavern_subsidy=None, proposed_reclaim_items=None)
            if supp_err:
                terminal_errors = AdventurerS1DataAppealService._terminal_operation_errors(appeal)
                terminal_error_ids = set(terminal_errors)
                all_execution_errors_terminal = bool(operation_errors) and set(operation_errors).issubset(terminal_error_ids)
                if early_v2 and (not _has_pending_requests()) and (not operation_errors or all_execution_errors_terminal):
                    _reject((AdventurerS1DataAppealService._terminal_execution_reject_reason(appeal) if terminal_errors else AdventurerS1DataAppealService._get_review_player_reason(appeal)) or '本次逐项复核未通过可执行补救操作。', warn=True)
                    return
                _defer('已确认的补救当前未能落账；该项已保留待处理，不视为诉求被驳回。', stage='execution', execution_error=supp_err if not early_v2 else None, operation_errors=operation_errors if early_v2 else None)
                logger.error('数据申诉 2 号补救执行失败 appeal=%s: %s', appeal_id, supp_err)
                return
            appeal.status = 'approved'
            appeal.reviewed_by = None
            appeal.reviewed_at = _portable_utc_now()
            appeal.applied_at = _portable_utc_now()
            if not early_v2:
                AdventurerS1DataAppealService._store_operation_receipts_and_request_results(appeal, operation_errors=operation_errors)
            if operation_errors:
                AdventurerS1DataAppealService._set_review_execution_note(appeal, '实际执行：部分独立补救操作发生冲突，未写入；其它通过项已分别处理。')
            if early_v2 and _has_pending_requests():
                _defer('本次已完成其它独立通过项；仍有项目尚未落账，已保留待处理，不视为这些项目被驳回。', stage='execution' if operation_errors else 'review_ai', operation_errors=operation_errors)
                return
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=True, auto=True)
            logger.info('数据申诉仅 2 号补救通过 appeal=%s', appeal_id)
            return
        review_decision = AdventurerS1DataAppealService._get_review_ai_decision(appeal)
        review_done = appeal.review_ai_status == 'done' and review_decision in ('allow', 'reject', 'pending')
        verdicts = AdventurerS1DataAppealService._get_review_verdicts(appeal)
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        review_contract_v2 = review_raw.get('verdict_contract_version') == 2
        review_manifest = review_raw.get('_backend_operation_manifest')
        if review_contract_v2 and (not review_done or not isinstance(review_raw.get('request_verdicts'), list) or (not isinstance(review_raw.get('verdicts'), list)) or (not isinstance(review_manifest, list)) or (bool(review_manifest) and (not verdicts)) or any((not isinstance(verdict, dict) or not verdict.get('operation_id') for verdict in verdicts))):
            _defer('本次逐项复核未完成，申诉已保留等待重新处理；本轮未对数据做改动，也未据此驳回您的诉求。', stage='review_ai')
            return
        if not review_contract_v2 and (not review_done):
            _defer('本次逐项复核未完成，申诉已保留等待重新处理；本轮未对数据做改动，也未据此驳回您的诉求。', stage='review_ai')
            return
        all_player_records = all_player_records_for_finalize or AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True)
        if review_done and verdicts:
            patch_to_apply, reject_reasons = AdventurerS1DataAppealService._filter_patch_by_verdicts(appeal.proposed_patch, verdicts)
            operation_contract_v2 = review_contract_v2
            if operation_contract_v2:
                AdventurerS1DataAppealService._mark_operation_receipt_execution_start(appeal)
                has_primary = AdventurerS1DataAppealService._patch_has_operations(patch_to_apply)
                has_supplement = AdventurerS1DataAppealService._patch_has_operations(appeal.review_supplement_patch)
                if not has_primary and (not has_supplement):
                    AdventurerS1DataAppealService._store_operation_receipts_and_request_results(appeal)
                    if _has_pending_requests():
                        _defer(AdventurerS1DataAppealService._get_review_player_reason(appeal) or '该项依据尚未被否定，但当前尚未形成可安全执行的修复；已保留待处理。', stage='review_ai')
                        return
                    detail = '；'.join(reject_reasons) if reject_reasons else '所有操作均未通过逐项复核'
                    _reject(f'陪审团 2 号复核后驳回全部操作：{detail}', warn=True)
                    return
                _, operation_errors, unlandable_items = AdventurerS1DataAppealService._apply_operation_contract_primary_groups(appeal, appeal.proposed_patch, verdicts, all_player_records=all_player_records)
                if has_supplement:
                    _, supplement_errors, supplement_unlandable = AdventurerS1DataAppealService._apply_operation_contract_review_groups(appeal, appeal.review_supplement_patch, verdicts, all_player_records=all_player_records)
                    operation_errors.update(supplement_errors)
                    unlandable_items.extend(supplement_unlandable)
                AdventurerS1DataAppealService._store_operation_receipts_and_request_results(appeal, operation_errors=operation_errors)
                actual_successful_ids = AdventurerS1DataAppealService._successful_operation_receipt_ids(appeal)
                if not actual_successful_ids:
                    errors = '；'.join(dict.fromkeys(operation_errors.values()))
                    detail = errors or '所有通过的操作均未产生审计回执，已按未落账处理'
                    terminal_errors = AdventurerS1DataAppealService._terminal_operation_errors(appeal)
                    all_execution_errors_terminal = bool(operation_errors) and set(operation_errors).issubset(set(terminal_errors))
                    if terminal_errors and all_execution_errors_terminal and (not _has_pending_requests()):
                        _reject((AdventurerS1DataAppealService._terminal_execution_reject_reason(appeal) if terminal_errors else AdventurerS1DataAppealService._get_review_player_reason(appeal)) or '本次通过的操作已不再满足最终写入条件。', warn=True)
                        return
                    _defer('已确认的修复当前未能落账；该项已保留待处理，不视为诉求被驳回。', stage='execution', execution_error=detail if not operation_errors else None, operation_errors=operation_errors)
                    return
                rejected_verdict_reasons = [str(verdict.get('reason') or '').strip() for verdict in verdicts if isinstance(verdict, dict) and str(verdict.get('decision') or '').strip().lower() == 'reject' and str(verdict.get('reason') or '').strip()]
                execution_reasons = [*reject_reasons, *rejected_verdict_reasons, *dict.fromkeys(operation_errors.values())]
                appeal.status = 'approved'
                appeal.reviewed_by = None
                appeal.reviewed_at = _portable_utc_now()
                appeal.applied_at = _portable_utc_now()
                if execution_reasons:
                    appeal.review_ai_summary = (appeal.review_ai_summary or '') + '（部分独立操作未执行：' + '；'.join(dict.fromkeys(execution_reasons)) + '）'
                    terminal_errors = AdventurerS1DataAppealService._terminal_operation_errors(appeal)
                    if terminal_errors:
                        execution_note = AdventurerS1DataAppealService._terminal_execution_reject_reason(appeal, partial=True)
                    else:
                        review_player_reason = AdventurerS1DataAppealService._get_review_player_reason(appeal) or '本次仅执行了有明确通过结论且最终校验成功的项目。'
                        execution_note = review_player_reason + '\n实际执行：每条诉求独立处理；未通过或执行冲突的项目未写入。'
                    AdventurerS1DataAppealService._set_review_execution_note(appeal, execution_note)
                if unlandable_items:
                    names = '、'.join((str(issue.get('name') or '').strip() for issue in unlandable_items if str(issue.get('name') or '').strip()))
                    AdventurerS1DataAppealService._set_review_execution_note(appeal, f"物品「{names or '未命名物品'}」因缺少可展示描述未补发；其它独立通过项不受影响。")
                if _has_pending_requests():
                    _defer('本次已完成其它独立通过项；仍有项目尚未落账，已保留待处理，不视为这些项目被驳回。', stage='execution' if operation_errors else 'review_ai', operation_errors=operation_errors)
                    return
                AdventurerS1DataAppealService._notify_player_result(appeal, approved=True, auto=True)
                logger.info('数据申诉 v2 逐操作完成 appeal=%s success=%s errors=%s', appeal_id, sorted(actual_successful_ids), operation_errors)
                return
            if not AdventurerS1DataAppealService._patch_has_operations(patch_to_apply):
                has_supplement = AdventurerS1DataAppealService._patch_has_operations(appeal.review_supplement_patch)
                if not has_supplement:
                    detail = '；'.join(reject_reasons) if reject_reasons else '所有操作均未通过复核'
                    AdventurerS1DataAppealService._store_operation_receipts_and_request_results(appeal)
                    _reject(f'陪审团 2 号复核后驳回全部操作：{detail}', warn=True)
                    return
            final_record_patch = AdventurerS1DataAppealService._merge_data_appeal_patches(AdventurerS1DataAppealService._record_patch_only(patch_to_apply), AdventurerS1DataAppealService._record_patch_only(appeal.review_supplement_patch), allow_field_overwrite=True)
            final_record_patch, unlandable_items = AdventurerS1DataAppealService._strip_unlandable_create_items(final_record_patch, appeal=appeal)
            if unlandable_items:
                names = '、'.join((str(issue.get('name') or '').strip() for issue in unlandable_items if str(issue.get('name') or '').strip()))
                reject_reasons.append(f"物品补发：{names or '存在物品'}缺少可展示描述，已跳过；其它独立操作继续执行")
            if AdventurerS1DataAppealService._patch_has_operations(final_record_patch):
                _, apply_err = AdventurerS1DataAppealService._apply_patch(appeal.adventurer_id, final_record_patch, appeal.snapshot_before, appeal_id=appeal.id, all_player_records=all_player_records, allow_quantity_increase=True)
                if apply_err:
                    _defer('已确认的修复当前未能落账；该项已保留待处理，不视为诉求被驳回。', stage='execution', execution_error=apply_err)
                    logger.error('数据申诉自动执行失败 appeal=%s: %s', appeal_id, apply_err)
                    return
            proposed_team_pool = None
            proposed_tavern_subsidy = None
            proposed_reclaim_items = None
            operation_contract_v2 = any((isinstance(verdict, dict) and verdict.get('operation_id') for verdict in verdicts))
            if operation_contract_v2:
                proposed_team_pool = patch_to_apply.get('team_pool')
                proposed_tavern_subsidy = patch_to_apply.get('tavern_subsidy')
                proposed_reclaim_items = patch_to_apply.get('reclaim_items')
            else:
                team_pool_verdict = next((v for v in verdicts if str(v.get('section', '')).strip().lower() == 'team_pool'), None)
                if team_pool_verdict and str(team_pool_verdict.get('decision', '')).strip().lower() == 'allow':
                    proposed_team_pool = (appeal.proposed_patch or {}).get('team_pool')
                tavern_subsidy_verdict = next((v for v in verdicts if str(v.get('section', '')).strip().lower() == 'tavern_subsidy'), None)
                if tavern_subsidy_verdict and str(tavern_subsidy_verdict.get('decision', '')).strip().lower() == 'allow':
                    proposed_tavern_subsidy = (appeal.proposed_patch or {}).get('tavern_subsidy')
                reclaim_verdict = next((v for v in verdicts if str(v.get('section', '')).strip().lower() == 'reclaim_items'), None)
                if reclaim_verdict and str(reclaim_verdict.get('decision', '')).strip().lower() == 'allow':
                    proposed_reclaim_items = (appeal.proposed_patch or {}).get('reclaim_items')
            has_distribution = any((proposed_team_pool, proposed_tavern_subsidy, proposed_reclaim_items, (appeal.review_supplement_patch or {}).get('team_pool'), (appeal.review_supplement_patch or {}).get('tavern_subsidy'), (appeal.review_supplement_patch or {}).get('reclaim_items')))
            if unlandable_items and (not AdventurerS1DataAppealService._patch_has_operations(final_record_patch)) and (not has_distribution):
                _defer('拟补发物品当前缺少可展示说明，尚未落账；该项已保留待处理，不视为证据不足或诉求被驳回。', stage='review_ai')
                return
            _, dist_err = AdventurerS1DataAppealService._apply_supplement_and_distributions(appeal, all_player_records=all_player_records, proposed_team_pool=proposed_team_pool, proposed_tavern_subsidy=proposed_tavern_subsidy, proposed_reclaim_items=proposed_reclaim_items, apply_supplement_patch=False)
            if dist_err:
                _defer('已确认的补发或分发当前未能全部落账；未落账项目已保留待处理，不视为诉求被驳回。', stage='execution', execution_error=dist_err)
                logger.error('数据申诉分发失败 appeal=%s: %s', appeal_id, dist_err)
                return
            appeal.status = 'approved'
            appeal.reviewed_by = None
            appeal.reviewed_at = _portable_utc_now()
            appeal.applied_at = _portable_utc_now()
            AdventurerS1DataAppealService._store_operation_receipts_and_request_results(appeal)
            if reject_reasons:
                appeal.review_ai_summary = (appeal.review_ai_summary or '') + '（部分操作已驳回：' + '；'.join(reject_reasons) + '）'
                review_player_reason = AdventurerS1DataAppealService._get_review_player_reason(appeal) or '本次仅执行了有明确通过结论的项目。'
                AdventurerS1DataAppealService._set_review_execution_note(appeal, review_player_reason + '\n实际执行：仅执行有明确通过结论的项目；' + '；'.join(reject_reasons))
            if unlandable_items:
                names = '、'.join((str(issue.get('name') or '').strip() for issue in unlandable_items if str(issue.get('name') or '').strip()))
                AdventurerS1DataAppealService._set_review_execution_note(appeal, f"本次已执行其它通过的修复；物品「{names or '未命名物品'}」因缺少可展示描述未补发，请补充物品说明后重新提交该项诉求。")
                _defer('本次已完成其它独立通过项；仍有物品因缺少可展示说明尚未落账，该项已保留待处理，不视为诉求被驳回。', stage='review_ai')
                return
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=True, auto=True)
            logger.info('数据申诉逐项通过 appeal=%s rejected=%s', appeal_id, reject_reasons)
            return
        if review_done and review_decision == 'allow':
            patch_to_apply = appeal.proposed_patch
        elif review_done and review_decision == 'reject':
            patch_to_apply = AdventurerS1DataAppealService._strip_privileged_operations(appeal.proposed_patch)
            privileged_voided = AdventurerS1DataAppealService._has_privileged_operations(appeal.proposed_patch)
            has_supplement = AdventurerS1DataAppealService._patch_has_operations(appeal.review_supplement_patch)
            if not AdventurerS1DataAppealService._patch_has_operations(patch_to_apply) and (not has_supplement):
                if privileged_voided:
                    _reject('陪审团 2 号驳回，高权限的补发已自动作废，且没有其它可自动执行的普通修复，未对数据做任何改动。', warn=True)
                else:
                    _reject(AdventurerS1DataAppealService.get_suggested_reject_reason(appeal))
                return
        else:
            _defer('本次逐项复核未完成，申诉已保留等待重新处理；本轮未对数据做改动，也未据此驳回您的诉求。', stage='review_ai')
            return
        final_record_patch = AdventurerS1DataAppealService._merge_data_appeal_patches(AdventurerS1DataAppealService._record_patch_only(patch_to_apply), AdventurerS1DataAppealService._record_patch_only(appeal.review_supplement_patch), allow_field_overwrite=True)
        final_record_patch, unlandable_items = AdventurerS1DataAppealService._strip_unlandable_create_items(final_record_patch, appeal=appeal)
        if AdventurerS1DataAppealService._patch_has_operations(final_record_patch):
            _, apply_err = AdventurerS1DataAppealService._apply_patch(appeal.adventurer_id, final_record_patch, appeal.snapshot_before, appeal_id=appeal.id, all_player_records=all_player_records, allow_quantity_increase=True)
            if apply_err:
                _defer('已确认的修复当前未能落账；该项已保留待处理，不视为诉求被驳回。', stage='execution', execution_error=apply_err)
                logger.error('数据申诉自动执行失败 appeal=%s: %s', appeal_id, apply_err)
                return
        proposed_team_pool = (appeal.proposed_patch or {}).get('team_pool') if review_done and review_decision == 'allow' else None
        proposed_tavern_subsidy = (appeal.proposed_patch or {}).get('tavern_subsidy') if review_done and review_decision == 'allow' else None
        proposed_reclaim_items = (appeal.proposed_patch or {}).get('reclaim_items') if review_done and review_decision == 'allow' else None
        has_distribution = any((proposed_team_pool, proposed_tavern_subsidy, proposed_reclaim_items, (appeal.review_supplement_patch or {}).get('team_pool'), (appeal.review_supplement_patch or {}).get('tavern_subsidy'), (appeal.review_supplement_patch or {}).get('reclaim_items')))
        if unlandable_items and (not AdventurerS1DataAppealService._patch_has_operations(final_record_patch)) and (not has_distribution):
            _defer('拟补发物品当前缺少可展示说明，尚未落账；该项已保留待处理，不视为证据不足或诉求被驳回。', stage='review_ai')
            return
        _, dist_err = AdventurerS1DataAppealService._apply_supplement_and_distributions(appeal, all_player_records=all_player_records, proposed_team_pool=proposed_team_pool, proposed_tavern_subsidy=proposed_tavern_subsidy, proposed_reclaim_items=proposed_reclaim_items, apply_supplement_patch=False)
        if dist_err:
            _defer('已确认的补发或分发当前未能全部落账；未落账项目已保留待处理，不视为诉求被驳回。', stage='execution', execution_error=dist_err)
            logger.error('数据申诉分发失败 appeal=%s: %s', appeal_id, dist_err)
            return
        has_any_ops = AdventurerS1DataAppealService._patch_has_operations(patch_to_apply) or AdventurerS1DataAppealService._patch_has_operations(appeal.review_supplement_patch)
        if not has_any_ops:
            _finalize_empty_patch()
            return
        appeal.status = 'approved'
        appeal.reviewed_by = None
        appeal.reviewed_at = _portable_utc_now()
        appeal.applied_at = _portable_utc_now()
        if not (review_done and review_decision == 'allow'):
            appeal.review_ai_summary = (appeal.review_ai_summary or '') + '（未获允许的高风险操作未执行，仅处理了其它独立普通修复）'
        if unlandable_items:
            names = '、'.join((str(issue.get('name') or '').strip() for issue in unlandable_items if str(issue.get('name') or '').strip()))
            appeal.review_ai_summary = (appeal.review_ai_summary or '') + f"（物品补发「{names or '未命名物品'}」因缺少可展示描述未执行；其它独立操作已执行）"
            AdventurerS1DataAppealService._set_review_execution_note(appeal, f"本次已执行其它通过的修复；物品「{names or '未命名物品'}」因缺少可展示描述未补发，请补充物品说明后重新提交该项诉求。")
            _defer('本次已完成其它独立通过项；仍有物品因缺少可展示说明尚未落账，该项已保留待处理，不视为诉求被驳回。', stage='review_ai')
            return
        AdventurerS1DataAppealService._notify_player_result(appeal, approved=True, auto=True)
        logger.info('数据申诉自动通过 appeal=%s', appeal_id)

    @staticmethod
    def _complete_ai_review(appeal_id: int, *, claim_token: str | None=None) -> bool:
        """AI 分析落库后自动完成，不再转管理员业务审核。"""
        appeal = AdventurerS1DataAppealService._load_appeal(appeal_id)
        if not appeal:
            return False
        execution_started = time.monotonic()
        AdventurerS1DataAppealService._try_auto_finalize(appeal_id, claim_token=claim_token)
        execution_ms = round((time.monotonic() - execution_started) * 1000)
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).populate_existing().first()
        if appeal:
            notification = SimpleNamespace(**{field: getattr(appeal, field) for field in ('id', 'user_id', 'status', 'ai_status', 'player_review_status', 'revert_offer_status')})
        else:
            notification = None
        terminal = bool(appeal and appeal.status in ('approved', 'rejected', 'cancelled'))
        db_manager.remove()
        logger.info('数据申诉最终事务完成 appeal=%s execution_ms=%s terminal=%s', appeal_id, execution_ms, terminal)
        if notification is not None:
            AdventurerS1DataAppealService._emit_data_appeal_changed(notification)
        return terminal

    @staticmethod
    def _resolve_appeal_invoke_reason(appeal: AdventurerS1DataAppeal, declared: str | None, prior_token_calls: int) -> str:
        """解析本次 process_ai 的实际执行原因（用于日志与 token.invoke_reason）。"""
        reason = (declared or 'unknown').strip() or 'unknown'
        if reason in ('requeue_stuck', 'admin_retry', 'player_continue'):
            return reason
        if prior_token_calls > 0 or appeal.ai_raw_response:
            return 'duplicate_concurrent'
        return reason

    @staticmethod
    def _appeal_ai_lease_is_stale(appeal: AdventurerS1DataAppeal, *, now: datetime | None=None, stale_minutes: int | None=None) -> bool:
        if not appeal or not appeal.ai_claim_token:
            return True
        owner_boot_id = str(getattr(appeal, 'ai_owner_boot_id', None) or '')
        current_boot_id = str(get_async_worker_boot_id() or '')
        if owner_boot_id and current_boot_id and (owner_boot_id != current_boot_id):
            from services.async_task_service import AsyncTaskService
            if AsyncTaskService.has_exclusive_startup_ownership():
                return True
        if stale_minutes is None:
            try:
                stale_minutes = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AI_STALE_MINUTES', 5))
            except (TypeError, ValueError):
                stale_minutes = 5
        anchor = appeal.ai_heartbeat_at or appeal.ai_claimed_at
        if not anchor:
            return True
        return anchor < (now or _portable_utc_now()) - timedelta(minutes=max(1, stale_minutes))

    @staticmethod
    def _appeal_auto_retry_delay_minutes(attempt: int) -> int:
        """Return the configured exponential delay without an unbounded exponent."""
        try:
            base_minutes = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AUTO_RETRY_BASE_MINUTES', 2))
        except (TypeError, ValueError):
            base_minutes = 2
        try:
            max_minutes = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AUTO_RETRY_MAX_MINUTES', 360))
        except (TypeError, ValueError):
            max_minutes = 360
        base_minutes = max(1, base_minutes)
        max_minutes = max(base_minutes, max_minutes)
        remaining_steps = max(0, int(attempt or 1) - 1)
        delay_minutes = base_minutes
        while remaining_steps and delay_minutes < max_minutes:
            delay_minutes = min(max_minutes, delay_minutes * 2)
            remaining_steps -= 1
        return delay_minutes

    @staticmethod
    def _appeal_auto_retry_max_model_retries() -> int:
        """Bound cross-worker model retries; execution-only retries spend no tokens."""
        try:
            configured = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AUTO_RETRY_MAX_MODEL_RETRIES', 3))
        except (TypeError, ValueError):
            configured = 3
        return max(0, configured)

    @staticmethod
    def _appeal_auto_retry_max_infrastructure_retries() -> int:
        """Bound zero-token outage retries without spending the model budget."""
        return AdventurerS1DataAppealService._appeal_auto_retry_max_model_retries()

    @staticmethod
    def _appeal_auto_retry_max_execution_retries() -> int:
        """Bound repeated consumer/writeback conflicts without spending tokens."""
        try:
            configured = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AUTO_RETRY_MAX_EXECUTION_RETRIES', 2))
        except (TypeError, ValueError):
            configured = 2
        return max(0, configured)

    @staticmethod
    def _appeal_auto_retry_ready(appeal: AdventurerS1DataAppeal, *, now: datetime | None=None) -> bool:
        """Whether a persisted AI-only retry has reached its backoff deadline."""
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        pending_stage = str(review_raw.get('_backend_resume_stage') or review_raw.get('pending_stage') or '').strip()
        policy_version = review_raw.get('_backend_execution_retry_policy_version')
        stage_has_bounded_retry = pending_stage in ('primary_ai', 'review_ai') or (pending_stage == 'execution' and policy_version == _DATA_APPEAL_EXECUTION_RETRY_POLICY_VERSION)
        if stage_has_bounded_retry:
            raw_attempts = review_raw.get('_backend_auto_retry_attempts')
            raw_attempts = raw_attempts if isinstance(raw_attempts, dict) else {}
            retry_count = raw_attempts.get(pending_stage)
            retry_count = retry_count if type(retry_count) is int and retry_count > 0 else 0
            max_retries = AdventurerS1DataAppealService._appeal_auto_retry_max_execution_retries() if pending_stage == 'execution' else AdventurerS1DataAppealService._appeal_auto_retry_max_model_retries()
            if review_raw.get('_backend_auto_retry_exhausted') == pending_stage or retry_count > max_retries:
                return False
        retry_after = parse_iso_date(str(review_raw.get('_backend_auto_retry_after') or '').strip())
        return retry_after is None or retry_after <= (now or _portable_utc_now())

    @staticmethod
    def _appeal_auto_retry_exhausted_stage(appeal: AdventurerS1DataAppeal) -> str | None:
        """Return the exhausted stage recorded by the retry state machine."""
        if not appeal:
            return None
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        exhausted_stage = str(review_raw.get('_backend_auto_retry_exhausted') or '').strip()
        pending_stage = str(review_raw.get('_backend_resume_stage') or review_raw.get('pending_stage') or '').strip()
        stage = exhausted_stage if exhausted_stage in ('primary_ai', 'review_ai', 'execution') else pending_stage
        if stage not in ('primary_ai', 'review_ai', 'execution'):
            return None
        if stage == 'execution' and review_raw.get('_backend_execution_retry_policy_version') != _DATA_APPEAL_EXECUTION_RETRY_POLICY_VERSION:
            return None
        raw_attempts = review_raw.get('_backend_auto_retry_attempts')
        raw_attempts = raw_attempts if isinstance(raw_attempts, dict) else {}
        retry_count = raw_attempts.get(stage)
        retry_count = retry_count if type(retry_count) is int and retry_count > 0 else 0
        max_retries = AdventurerS1DataAppealService._appeal_auto_retry_max_execution_retries() if stage == 'execution' else AdventurerS1DataAppealService._appeal_auto_retry_max_model_retries()
        if exhausted_stage == stage or retry_count > max_retries:
            return stage
        return None

    @staticmethod
    def _finalize_exhausted_appeal_auto_retry(appeal: AdventurerS1DataAppeal) -> bool:
        """Resolve a claimed-free exhausted retry without inventing an AI verdict."""
        exhausted_stage = AdventurerS1DataAppealService._appeal_auto_retry_exhausted_stage(appeal)
        if not exhausted_stage or appeal.status != 'processing' or appeal.ai_status not in ('pending', 'failed') or appeal.ai_claim_token:
            return False
        if exhausted_stage == 'execution':
            player_reason = '系统已自动尝试最终写入多次，但当前记录仍无法安全落账；未完成项目没有新增修改，已完成项目以逐项落账回执为准。本次自动处理已结束，可在原申诉中继续处理。'
        else:
            player_reason = '系统连续多次未能完成当前核查阶段，未形成完整审核结论；系统没有依据未完成的结论新增修改，已完成项目以逐项结果为准。本次自动处理已结束，可在原申诉中继续处理。'
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        request_results = review_raw.get('request_results')
        if isinstance(request_results, list):
            for request_result in request_results:
                if isinstance(request_result, dict) and request_result.get('status') == 'pending':
                    request_result['detail'] = player_reason[:160]
        review_raw['resolution_state'] = 'failed'
        review_raw['pending_stage'] = exhausted_stage
        review_raw['_backend_auto_retry_exhausted'] = exhausted_stage
        review_raw.pop('_backend_auto_retry_after', None)
        review_raw['execution_note'] = player_reason[:800]
        appeal.review_ai_raw_response = json_dumps_safe(review_raw)
        appeal.ai_status = 'failed'
        appeal.status = 'rejected'
        if exhausted_stage == 'execution':
            appeal.review_ai_status = 'done'
            appeal.review_ai_summary = '独立复核已完成，但最终写入达到自动重试上限，自动处理已结束'
            appeal.ai_summary = '最终写入达到自动重试上限，自动处理已结束'
            appeal.core_request_satisfied = None
        else:
            appeal.review_ai_status = 'failed'
            appeal.review_ai_summary = '主申诉分析多次未完成，未进入独立复核' if exhausted_stage == 'primary_ai' else '独立复核多次未完成，未形成审核结论'
        appeal.reviewed_at = _portable_utc_now()
        appeal.reviewed_by = None
        appeal.reject_reason = player_reason
        AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)
        return True

    @staticmethod
    def _finalize_exhausted_appeal_auto_retries(appeal_ids: list[int]) -> list[int]:
        """Lock and resolve exhausted retry states in one transaction."""
        normalized_ids = sorted({int(appeal_id) for appeal_id in appeal_ids})
        if not normalized_ids:
            return []
        finalized_ids: list[int] = []
        with db_manager.transaction():
            appeals = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.id.in_(normalized_ids)).order_by(AdventurerS1DataAppeal.id.asc()).with_for_update().populate_existing().all()
            for appeal in appeals:
                if AdventurerS1DataAppealService._finalize_exhausted_appeal_auto_retry(appeal):
                    finalized_ids.append(int(appeal.id))
        if finalized_ids:
            logger.error('数据申诉 AI 自动重试已耗尽，转技术失败终态 appeals=%s', finalized_ids)
        return finalized_ids

    @staticmethod
    def _appeal_resume_stage(appeal: AdventurerS1DataAppeal) -> str | None:
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        stage = str(review_raw.get('_backend_resume_stage') or '').strip()
        if stage == 'execution' and (not AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal)):
            return 'review_ai'
        return stage if stage in ('review_ai', 'execution') else None

    @staticmethod
    def _appeal_frozen_review_ready_for_execution(appeal: AdventurerS1DataAppeal) -> bool:
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        request_verdicts = review_raw.get('request_verdicts')
        review_decision = str(review_raw.get('decision') or '').strip().lower()
        ready = appeal.review_ai_status == 'done' and review_decision in ('allow', 'reject', 'pending')
        if review_raw.get('verdict_contract_version') == 2:
            ready = bool(ready and isinstance(request_verdicts, list) and isinstance(review_raw.get('verdicts'), list) and isinstance(review_raw.get('_backend_operation_manifest'), list) and (not any((isinstance(row, dict) and str(row.get('decision') or '').strip().lower() == 'pending' for row in request_verdicts))))
        has_executable = AdventurerS1DataAppealService._appeal_has_executable_patch(getattr(appeal, 'proposed_patch', None), getattr(appeal, 'review_supplement_patch', None))
        if ready and (not has_executable):
            terminal_without_write = review_decision == 'reject'
            if isinstance(request_verdicts, list) and request_verdicts:
                terminal_without_write = terminal_without_write and all((isinstance(row, dict) and str(row.get('decision') or '').strip().lower() == 'reject' and (str(row.get('resolution_basis') or '').strip() in S1_DATA_APPEAL_REJECTION_RESOLUTION_BASES) for row in request_verdicts))
            ready = terminal_without_write
        return bool(ready)

    @staticmethod
    def _promote_legacy_non_manual_awaiting(appeal: AdventurerS1DataAppeal) -> bool:
        """Move every non-manual legacy awaiting row back into the AI state machine."""
        if not appeal or appeal.status != 'awaiting_review' or appeal.review_ai_model == AdventurerS1DataAppealService._MANUAL_TAKEOVER_REVIEW_MODEL:
            return False
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        pending_stage = str(review_raw.get('pending_stage') or '').strip() if review_raw.get('resolution_state') == 'pending' else ''
        if (appeal.appeal_category or '') == 'tavern_relief':
            pending_stage = 'primary_ai'
        elif pending_stage not in ('primary_ai', 'review_ai', 'execution'):
            if AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal):
                pending_stage = 'execution'
            elif appeal.ai_status == 'done' and isinstance(appeal._parse_json_text(appeal.ai_raw_response), dict):
                pending_stage = 'review_ai'
            else:
                pending_stage = 'primary_ai'
        elif pending_stage == 'execution' and (not AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal)):
            pending_stage = 'review_ai' if appeal.ai_status == 'done' and isinstance(appeal._parse_json_text(appeal.ai_raw_response), dict) else 'primary_ai'
        review_raw.pop('execution_note', None)
        appeal.review_ai_raw_response = json_dumps_safe(review_raw)
        AdventurerS1DataAppealService._mark_appeal_resolution_pending(appeal, '此前中断的申诉已转回自动处理，不会据此驳回诉求。', stage=pending_stage)
        return True

    @staticmethod
    def _promote_legacy_non_manual_awaiting_by_id(appeal_id: int) -> bool:
        """Lock and recheck an old row before leaving the historical review state."""
        promoted = False
        with db_manager.transaction():
            appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(appeal_id)).with_for_update().populate_existing().first()
            promoted = AdventurerS1DataAppealService._promote_legacy_non_manual_awaiting(appeal)
        return promoted

    @staticmethod
    def _restore_orphaned_completed_appeal_stage(appeal_id: int, *, stale_minutes: int) -> bool:
        """Recover the commit/finalize crash gap under the authoritative row lock."""
        restored = False
        with db_manager.transaction():
            appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(appeal_id)).with_for_update().populate_existing().first()
            if not appeal or appeal.status != 'processing' or appeal.ai_status not in ('done', 'failed') or (appeal.ai_claim_token and (not AdventurerS1DataAppealService._appeal_ai_lease_is_stale(appeal, stale_minutes=stale_minutes))):
                return False
            if appeal.ai_status != 'done':
                orphaned_stage = 'primary_ai'
            elif AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal):
                orphaned_stage = 'execution'
            else:
                orphaned_stage = 'review_ai'
            AdventurerS1DataAppealService._mark_appeal_resolution_pending(appeal, '上一轮处理在完成当前阶段后中断，申诉仍在处理中，将从已持久化的下一阶段自动继续。', stage=orphaned_stage)
            restored = True
        return restored

    @staticmethod
    def _try_claim_appeal_ai(appeal_id: int, *, restart_recovered: bool=False) -> str | None:
        """短事务独占认领；仅接管过期、旧 boot 或精确恢复的租约。"""
        claim_token = uuid.uuid4().hex
        with db_manager.transaction():
            appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().populate_existing().first()
            if not appeal or appeal.status != 'processing' or appeal.ai_status not in ('pending', 'processing'):
                return None
            restart_takeover_allowed = bool(restart_recovered and (not str(getattr(appeal, 'ai_owner_boot_id', None) or '')))
            if appeal.ai_status == 'processing' and appeal.ai_claim_token and (not restart_takeover_allowed) and (not AdventurerS1DataAppealService._appeal_ai_lease_is_stale(appeal)):
                return None
            now = _portable_utc_now()
            appeal.ai_status = 'processing'
            appeal.ai_claim_token = claim_token
            appeal.ai_claimed_at = now
            appeal.ai_heartbeat_at = now
            appeal.ai_owner_boot_id = get_async_worker_boot_id()
        return claim_token

    @staticmethod
    def _load_claimed_appeal(appeal_id: int, claim_token: str, *, for_update: bool=False) -> AdventurerS1DataAppeal | None:
        query = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, status='processing', ai_claim_token=claim_token).populate_existing()
        if for_update:
            query = query.with_for_update()
        return query.first()

    @staticmethod
    def _reload_appeal_for_ai_write(appeal_id: int, claim_token: str | None, *, for_update: bool=False) -> AdventurerS1DataAppeal | None:
        if claim_token:
            return AdventurerS1DataAppealService._load_claimed_appeal(appeal_id, claim_token, for_update=for_update)
        appeal = AdventurerS1DataAppealService._load_appeal(appeal_id, refresh=True, for_update=for_update)
        return appeal if appeal and appeal.status == 'processing' else None

    @staticmethod
    def _clear_appeal_ai_lease_fields(appeal: AdventurerS1DataAppeal) -> None:
        appeal.ai_claim_token = None
        appeal.ai_claimed_at = None
        appeal.ai_heartbeat_at = None
        appeal.ai_owner_boot_id = None

    @staticmethod
    def _touch_appeal_ai_lease(appeal_id: int, claim_token: str) -> bool:
        try:
            updated = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.id == appeal_id, AdventurerS1DataAppeal.status == 'processing', AdventurerS1DataAppeal.ai_status == 'processing', AdventurerS1DataAppeal.ai_claim_token == claim_token).update({AdventurerS1DataAppeal.ai_heartbeat_at: _portable_utc_now(), AdventurerS1DataAppeal.ai_owner_boot_id: get_async_worker_boot_id()}, synchronize_session=False)
            ok, _ = db_manager.commit()
            return bool(updated and ok)
        except Exception:
            db_manager.rollback()
            logger.warning('数据申诉 AI 租约心跳失败 appeal=%s', appeal_id, exc_info=True)
            return False

    @staticmethod
    def _start_appeal_ai_heartbeat(appeal_id: int, claim_token: str) -> tuple[threading.Event, threading.Thread] | None:
        try:
            interval = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AI_HEARTBEAT_SECONDS', 30))
        except (TypeError, ValueError):
            interval = 30
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
                        if not AdventurerS1DataAppealService._touch_appeal_ai_lease(appeal_id, claim_token):
                            return
                    finally:
                        db_manager.remove()
        thread = threading.Thread(target=_heartbeat, name=f's1-appeal-heartbeat-{appeal_id}', daemon=True)
        thread.start()
        return (stop_event, thread)

    @staticmethod
    def _stop_appeal_ai_heartbeat(heartbeat: tuple[threading.Event, threading.Thread] | None) -> None:
        if not heartbeat:
            return
        stop_event, thread = heartbeat
        stop_event.set()
        thread.join(timeout=2)

    @staticmethod
    def _release_appeal_ai_claim(appeal_id: int, claim_token: str) -> None:
        """仅原 owner 可释放自己的租约；过期 worker 不得清掉新 owner。"""
        try:
            updated = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.id == appeal_id, AdventurerS1DataAppeal.ai_claim_token == claim_token).update({AdventurerS1DataAppeal.ai_claim_token: None, AdventurerS1DataAppeal.ai_claimed_at: None, AdventurerS1DataAppeal.ai_heartbeat_at: None, AdventurerS1DataAppeal.ai_owner_boot_id: None}, synchronize_session=False)
            if updated:
                db_manager.commit()
            else:
                db_manager.rollback()
        except Exception:
            db_manager.rollback()
            logger.warning('数据申诉 AI 租约释放失败 appeal=%s', appeal_id, exc_info=True)

    @staticmethod
    def process_ai(appeal_id: int, *, invoke_reason: str='unknown', requeue_cause: str | None=None, resume_stage: str | None=None, restart_recovered: bool=False) -> None:
        """异步 worker 调用：跑 AI 并更新工单。"""
        from plugins.adventurer_s1.models import AdventurerS1TokenUsage
        from plugins.adventurer_s1.services.s1_token_usage_service import reset_appeal_invoke_reason, set_appeal_invoke_reason
        claim_token = AdventurerS1DataAppealService._try_claim_appeal_ai(appeal_id, restart_recovered=restart_recovered)
        if not claim_token:
            logger.info('数据申诉 AI 跳过 appeal=%s: 未取得数据库租约 invoke_reason=%s', appeal_id, invoke_reason)
            return
        appeal = AdventurerS1DataAppealService._load_claimed_appeal(appeal_id, claim_token)
        if not appeal:
            AdventurerS1DataAppealService._release_appeal_ai_claim(appeal_id, claim_token)
            return
        stored_review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        stored_resume_stage = str(stored_review_raw.get('_backend_resume_stage') or '').strip()
        effective_resume_stage = str(resume_stage or stored_resume_stage).strip()
        if effective_resume_stage not in ('review_ai', 'execution'):
            effective_resume_stage = ''
        prior_token_calls = int(AdventurerS1TokenUsage.query.filter_by(appeal_id=appeal_id).count())
        resolved_reason = AdventurerS1DataAppealService._resolve_appeal_invoke_reason(appeal, invoke_reason, prior_token_calls)
        logger.info('数据申诉 AI process appeal=%s invoke_reason=%s declared=%s adventurer_id=%s ai_status=%s prior_token_calls=%s requeue_cause=%s', appeal_id, resolved_reason, invoke_reason, appeal.adventurer_id, appeal.ai_status, prior_token_calls, requeue_cause or '')
        reason_token = set_appeal_invoke_reason(resolved_reason)
        heartbeat = AdventurerS1DataAppealService._start_appeal_ai_heartbeat(appeal_id, claim_token)
        try:
            AdventurerS1DataAppealService._process_ai_body(appeal_id, appeal, claim_token, resume_stage=effective_resume_stage or None)
        finally:
            AdventurerS1DataAppealService._stop_appeal_ai_heartbeat(heartbeat)
            AdventurerS1DataAppealService._release_appeal_ai_claim(appeal_id, claim_token)
            reset_appeal_invoke_reason(reason_token)

    @staticmethod
    def _process_ai_body(appeal_id: int, appeal: AdventurerS1DataAppeal, claim_token: str | None=None, *, resume_stage: str | None=None) -> None:
        """process_ai 主逻辑；二审/执行恢复复用已冻结的前序成品。"""
        appeal = AdventurerS1DataAppealService._reload_appeal_for_ai_write(appeal_id, claim_token, for_update=True)
        if not appeal:
            return
        previous_review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {} if resume_stage == 'review_ai' else None
        identity_err = AdventurerS1DataAppealService._appeal_identity_error(appeal)
        if identity_err:
            appeal.ai_status = 'failed'
            appeal.ai_summary = '关联探险角色身份校验未通过，未调用申诉 AI'
            appeal.review_ai_status = 'failed'
            appeal.review_ai_summary = '后端身份门禁已阻止跨角色世代申诉'
            appeal.status = 'rejected'
            appeal.reviewed_at = _portable_utc_now()
            appeal.reject_reason = identity_err
            AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=False)
            ok, err = db_manager.commit()
            if not ok:
                logger.error('数据申诉身份门禁结果落库失败 appeal=%s: %s', appeal_id, err)
                return
            logger.warning('数据申诉身份门禁中止 AI appeal=%s: %s', appeal_id, identity_err)
            return
        if resume_stage == 'execution':
            review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
            if not AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal):
                AdventurerS1DataAppealService._mark_appeal_resolution_pending(appeal, '已冻结的逐项复核结果不完整，申诉仍在处理中，系统将自动继续核对；本轮未对数据做改动，也未据此驳回您的诉求。', stage='review_ai')
                ok, err = db_manager.commit()
                if not ok:
                    logger.error('数据申诉执行恢复降级落库失败 appeal=%s: %s', appeal_id, err)
                    return
                AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
                return
            for key in ('resolution_state', 'pending_stage', '_backend_resolution_state', '_backend_pending_stage', '_backend_execution_error', '_backend_resume_stage', '_backend_auto_retry_after', '_backend_auto_retry_exhausted'):
                review_raw.pop(key, None)
            appeal.review_ai_raw_response = json_dumps_safe(review_raw)
            appeal.ai_status = 'done'
            ok, err = db_manager.commit()
            if not ok:
                logger.error('数据申诉执行恢复状态落库失败 appeal=%s: %s', appeal_id, err)
                return
            logger.info('数据申诉 appeal=%s 复用冻结裁决，仅重试最终执行', appeal_id)
            AdventurerS1DataAppealService._complete_ai_review(appeal_id, claim_token=claim_token)
            return
        note = appeal.player_note
        snapshot = copy.deepcopy(appeal.snapshot_before) if appeal.snapshot_before else {}
        is_data_fix = (appeal.appeal_category or 'other') == 'data_fix'
        if is_data_fix:
            evidence = None
        else:
            evidence = copy.deepcopy(appeal.expedition_evidence) if appeal.expedition_evidence else None
            if not evidence:
                evidence = AdventurerS1DataAppealService._build_latest_settlement_evidence(appeal.adventurer_id)
            AdventurerS1DataAppealService._ensure_departure_equipment_ownership(evidence)
            AdventurerS1DataAppealService._ensure_departure_consumption_candidates(evidence, appeal.adventurer_id)
            evidence = AdventurerS1DataAppealService._attach_equipment_description_inheritance_candidates(evidence, appeal.adventurer_id, snapshot)
        evidence_log_id = None
        if isinstance(evidence, dict):
            try:
                evidence_log_id = int(evidence.get('expedition_log_id'))
            except (TypeError, ValueError):
                evidence_log_id = None
        recent_activity = AdventurerS1DataAppealService._build_recent_item_activity(appeal.adventurer_id, appealed_expedition_log_id=evidence_log_id)
        identity_err = AdventurerS1DataAppealService._appeal_identity_error(appeal)
        if identity_err:
            appeal.ai_status = 'failed'
            appeal.ai_summary = '关联探险角色身份校验未通过，未调用申诉 AI'
            appeal.review_ai_status = 'failed'
            appeal.review_ai_summary = '后端身份门禁已阻止跨角色世代申诉'
            appeal.status = 'rejected'
            appeal.reviewed_at = _portable_utc_now()
            appeal.reject_reason = identity_err
            AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)
            AdventurerS1DataAppealService._notify_player_result(appeal, approved=False)
            ok, err = db_manager.commit()
            if not ok:
                logger.error('数据申诉证据身份门禁结果落库失败 appeal=%s: %s', appeal_id, err)
                return
            logger.warning('数据申诉证据身份门禁中止 AI appeal=%s: %s', appeal_id, identity_err)
            return
        if evidence_log_id and appeal.expedition_log_id != evidence_log_id:
            appeal.expedition_log_id = evidence_log_id
        if isinstance(evidence, dict):
            appeal.expedition_evidence = copy.deepcopy(evidence)
        if is_data_fix:
            prior_appeal_grants = []
            narrative_currency_cap = 0
            party_roster = []
            team_pool_status = {}
        else:
            prior_appeal_grants = AdventurerS1DataAppealService._prior_grants_without_tavern_subsidy(AdventurerS1DataAppealService._build_prior_appeal_grants(evidence_log_id, appeal.id))
            narrative_currency_cap = AdventurerS1DataAppealService._narrative_currency_cap_for_evidence(evidence)
            _party_roster_full = AdventurerS1DataAppealService._build_party_roster(evidence_log_id)
            party_roster = [{'adventurer_id': m['adventurer_id'], 'name': m['name'], 'alive': m['alive'], 'individual_gold_change': m['gold_change']} for m in _party_roster_full]
            team_pool_status = {'settled': AdventurerS1DataAppealService._is_team_pool_settled(evidence_log_id), 'alive_member_count': sum((1 for m in _party_roster_full if m['alive']))}
        appeal_adv_row = db_manager.query(AdventurerS1.id, AdventurerS1.name).filter(AdventurerS1.id == appeal.adventurer_id).first()
        appeal_adventurer = {'adventurer_id': int(appeal.adventurer_id), 'name': appeal_adv_row.name if appeal_adv_row else ''}
        if isinstance(evidence, dict):
            appeal.expedition_evidence = copy.deepcopy(evidence)
        appeal.ai_status = 'processing'
        ok, err = db_manager.commit()
        if not ok:
            logger.error('数据申诉 AI 状态更新失败: %s', err)
            return
        appeal_fact_sheet = None
        if (appeal.appeal_category or 'other') == 'rarity_review':
            AdventurerS1DataAppealService._process_rarity_review_ai_body(appeal_id, appeal, snapshot, evidence or {}, appeal_adventurer, claim_token=claim_token)
            return
        ai_started = True
        primary_ai_token_usage: dict = {}
        primary_ai_runtime_metadata: dict[str, object] = {}
        primary_ai_contract_candidate = False
        primary_ai_contract_succeeded = False
        primary_ai_consumer_failure_code = None
        review_ai_token_usage = None
        review_ai_contract_candidate = False
        review_ai_contract_succeeded = False
        try:
            from plugins.adventurer_s1.services.adventurer_s1_data_appeal_ai_service import call_data_appeal_ai
            ai_kwargs = {'expedition_evidence': evidence, 'recent_activity': recent_activity, 'prior_appeal_grants': prior_appeal_grants, 'party_roster': party_roster, 'team_pool_status': team_pool_status, 'appeal_category': appeal.appeal_category or 'other'}
            if appeal_fact_sheet:
                ai_kwargs['appeal_fact_sheet'] = appeal_fact_sheet
            import inspect
            ai_params = inspect.signature(call_data_appeal_ai).parameters
            if 'appeal_adventurer' in ai_params:
                ai_kwargs['appeal_adventurer'] = appeal_adventurer
            if 'token_usage_out' in ai_params:
                ai_kwargs['token_usage_out'] = primary_ai_token_usage
            if 'runtime_metadata_out' in ai_params:
                ai_kwargs['runtime_metadata_out'] = primary_ai_runtime_metadata
            if 'all_player_records' in ai_params:
                ai_kwargs['all_player_records'] = AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id)
            if resume_stage == 'review_ai':
                raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal)
                logger.info('数据申诉 appeal=%s 保留一审成品，仅重试独立复核', appeal_id)
            else:
                raw = call_data_appeal_ai(appeal_id, note, snapshot, **ai_kwargs)
            appeal = AdventurerS1DataAppealService._reload_appeal_for_ai_write(appeal_id, claim_token)
            if not appeal:
                logger.info('数据申诉 AI 中止 appeal=%s: 租约或工单状态已变化', appeal_id)
                return
            if isinstance(raw, dict) and raw.get('_ai_call_failed') is True:
                appeal = AdventurerS1DataAppealService._reload_appeal_for_ai_write(appeal_id, claim_token, for_update=True)
                if not appeal:
                    logger.info('数据申诉失败写回中止 appeal=%s: 租约或工单状态已变化', appeal_id)
                    return
                appeal.ai_status = 'failed'
                appeal.ai_summary = str(raw.get('summary') or '一审未完成').strip()
                appeal.ai_raw_response = json_dumps_safe(raw)
                appeal.review_ai_status = 'failed'
                appeal.review_ai_summary = '主申诉分析未完成，未进入独立复核'
                AdventurerS1DataAppealService._mark_appeal_resolution_pending(appeal, '本次初步核对未形成有效结论，申诉仍在处理中，系统将自动重试；本轮未对数据做改动，也未据此驳回您的诉求。', stage='primary_ai', counts_toward_model_retry=primary_ai_runtime_metadata.get('positive_tokens_consumed') if type(primary_ai_runtime_metadata.get('positive_tokens_consumed')) is bool else None)
                ok, err = db_manager.commit()
                if not ok:
                    logger.error('数据申诉 AI 诊断失败落库错误: %s', err)
                    return
                logger.info('数据申诉 AI 未完成 appeal=%s -> %s/%s (stage=%s)', appeal_id, appeal.status, appeal.ai_status, raw.get('failure_stage'))
                AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
                return
            if not raw or not isinstance(raw, dict):
                appeal = AdventurerS1DataAppealService._reload_appeal_for_ai_write(appeal_id, claim_token, for_update=True)
                if not appeal:
                    logger.info('数据申诉空结果写回中止 appeal=%s: 租约或工单状态已变化', appeal_id)
                    return
                appeal.ai_status = 'failed'
                appeal.ai_summary = '一审 AI 分析失败，未生成可执行结论'
                appeal.review_ai_status = 'failed'
                appeal.review_ai_summary = '主申诉 AI 未返回有效结果，未进入独立复核'
                AdventurerS1DataAppealService._mark_appeal_resolution_pending(appeal, '本次初步核对未返回有效结果，申诉仍在处理中，系统将自动重试；本轮未对数据做改动，也未据此驳回您的诉求。', stage='primary_ai', counts_toward_model_retry=primary_ai_runtime_metadata.get('positive_tokens_consumed') if type(primary_ai_runtime_metadata.get('positive_tokens_consumed')) is bool else None)
                ok, err = db_manager.commit()
                if not ok:
                    logger.error('数据申诉 AI 失败落库错误: %s', err)
                    return
                logger.info('数据申诉 AI 未完成 appeal=%s -> %s/%s', appeal_id, appeal.status, appeal.ai_status)
                AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
                return
            raw = AdventurerS1DataAppealService._finalize_ai_raw_response(raw)
            summary = str(raw.get('summary') or '').strip() or 'AI 已完成分析'
            patch_raw = raw.get('patch') if isinstance(raw.get('patch'), dict) else {}
            primary_pre_normalization_contract_valid = True
            patch_raw, removed_cross_grants = AdventurerS1DataAppealService.strip_cross_adventurer_self_grants(patch_raw, appeal_adventurer_id=appeal.adventurer_id, party_roster=party_roster)
            if removed_cross_grants:
                primary_ai_consumer_failure_code = 'consumer.cross_adventurer_grant_stripped'
                primary_pre_normalization_contract_valid = False
                raw['patch'] = patch_raw
                AdventurerS1DataAppealService._append_cross_adventurer_grant_warnings(raw, removed_cross_grants)
                logger.warning('数据申诉 appeal=%s 剔除跨队友普通补发: %s', appeal_id, removed_cross_grants)
            patch_raw, removed_unsupported_grants = AdventurerS1DataAppealService.strip_unsupported_expedition_self_grants(patch_raw, appeal_adventurer=appeal_adventurer, expedition_evidence=evidence, party_roster=party_roster, recent_activity=recent_activity)
            if removed_unsupported_grants:
                primary_ai_consumer_failure_code = 'consumer.ownership_evidence_missing'
                primary_pre_normalization_contract_valid = False
                raw['patch'] = patch_raw
                AdventurerS1DataAppealService._append_unsupported_expedition_grant_warnings(raw, removed_unsupported_grants)
                logger.warning('数据申诉 appeal=%s 剔除缺少本人归属证明的探险普通补发: %s', appeal_id, removed_unsupported_grants)
            patch_raw, blocked_direct_deletes = strip_uncontracted_trait_skill_deletes(patch_raw)
            raw['patch'] = patch_raw
            if blocked_direct_deletes:
                primary_ai_consumer_failure_code = 'consumer.destructive_contract_missing'
                primary_pre_normalization_contract_valid = False
                warnings = raw.get('warnings') if isinstance(raw.get('warnings'), list) else []
                warnings.append('特质/技能删除未使用破坏性动作契约，后端已拒绝这些删除。')
                raw['warnings'] = warnings
                raw['_backend_blocked_direct_deletes'] = blocked_direct_deletes
                logger.warning('数据申诉 appeal=%s 剔除未走破坏性动作契约的特质/技能删除: %s', appeal_id, blocked_direct_deletes)
            focus_refs_raw = raw.get('focus_refs') if isinstance(raw.get('focus_refs'), dict) else None
            related_records = AdventurerS1DataAppealService.build_related_records_for_appeal_review(appeal.adventurer_id, snapshot_before=snapshot, focus_refs=focus_refs_raw, patch=patch_raw)
            lookup_results = AdventurerS1DataAppealService.resolve_lookup_queries(appeal.adventurer_id, raw.get('lookup_queries') if isinstance(raw.get('lookup_queries'), list) else None)
            all_player_records = AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True)
            patch_raw = raw.get('patch') if isinstance(raw.get('patch'), dict) else {}
            lineage_warnings: list[str] = []
            patch_contract_status: dict = {}
            expedition_log_id = AdventurerS1DataAppealService._resolve_expedition_log_id_for_appeal(appeal, evidence)
            allow_rarity_inc = AdventurerS1DataAppealService._should_allow_expedition_rarity_increase(patch_raw, appeal.snapshot_before or {}, appeal.adventurer_id, expedition_log_id)
            normalized, patch_err = AdventurerS1DataAppealService.normalize_patch(patch_raw, appeal.snapshot_before, all_player_records=all_player_records, narrative_currency_cap=narrative_currency_cap, allow_all_player_record_ids=False, expedition_evidence=evidence, allow_rarity_increase=allow_rarity_inc, adventurer_id=appeal.adventurer_id, appeal_id=appeal_id, lineage_warnings_out=lineage_warnings, contract_status_out=patch_contract_status)
            primary_ai_contract_candidate = primary_ai_token_usage.get('envelope_contract_valid', True) is True and primary_pre_normalization_contract_valid and (patch_contract_status.get('contract_valid') is True) and (normalized is not None or patch_err == 'patch 中没有有效操作')
            if patch_contract_status.get('contract_valid') is not True and primary_ai_consumer_failure_code is None:
                primary_ai_consumer_failure_code = 'consumer.patch_contract_invalid'
            elif normalized is None and patch_err != 'patch 中没有有效操作' and (primary_ai_consumer_failure_code is None):
                primary_ai_consumer_failure_code = 'consumer.patch_normalization_failed'
            if lineage_warnings:
                raw['lineage_guard_warnings'] = lineage_warnings
                raw['core_request_satisfied'] = False
            trait_desc_issues = AdventurerS1DataAppealService.audit_trait_description_landing(raw_patch=patch_raw, normalized_patch=normalized or {}, snapshot_before=snapshot, focus_refs=focus_refs_raw)
            if trait_desc_issues:
                raw = AdventurerS1DataAppealService.apply_trait_description_audit(raw, trait_desc_issues)
                logger.info('数据申诉 appeal=%s 特质描述落库审计: %s', appeal_id, [issue.get('code') for issue in trait_desc_issues])
            item_qty_issues = AdventurerS1DataAppealService.audit_item_quantity_landing(raw_patch=patch_raw, normalized_patch=normalized or {}, snapshot_before=snapshot)
            has_substantive_name_update = AdventurerS1DataAppealService._patch_has_substantive_item_name_update(patch_raw, snapshot)
            mechanical_qty_supplement = None
            if item_qty_issues:
                raw = AdventurerS1DataAppealService.apply_item_quantity_audit(raw, item_qty_issues)
                logger.info('数据申诉 appeal=%s 物品数量落库审计: %s', appeal_id, [issue.get('code') for issue in item_qty_issues])
                mech_raw = AdventurerS1DataAppealService._build_mechanical_quantity_supplement_patch(patch_raw, snapshot, item_qty_issues)
                if mech_raw:
                    mechanical_qty_supplement, mech_err = AdventurerS1DataAppealService.normalize_patch(mech_raw, snapshot, all_player_records=all_player_records, allow_quantity_increase=True, allow_all_player_record_ids=False, expedition_evidence=evidence, adventurer_id=appeal.adventurer_id, appeal_id=appeal_id)
                    if mechanical_qty_supplement:
                        logger.info('数据申诉 appeal=%s 机械 quantity supplement 已生成（%s 条 update）', appeal_id, sum((len((mechanical_qty_supplement.get(sec) or {}).get('update') or []) for sec in ('equipment', 'warehouse'))))
                    elif mech_err:
                        logger.info('数据申诉 appeal=%s 机械 quantity supplement 未通过校验: %s', appeal_id, mech_err)
                if mechanical_qty_supplement:
                    normalized = AdventurerS1DataAppealService._merge_data_appeal_patches(normalized or {}, mechanical_qty_supplement, allow_field_overwrite=True)
                    patch_err = None
                    raw['_backend_mechanical_quantity_requires_review'] = True
                    mechanical_qty_supplement = None
            if not AdventurerS1DataAppealService._patch_has_item_record_benefit(normalized):
                mech_rarity_raw = AdventurerS1DataAppealService._build_mechanical_rarity_upgrade_supplement_patch(snapshot, adventurer_id=appeal.adventurer_id, expedition_log_id=expedition_log_id)
                if mech_rarity_raw:
                    mech_rarity_norm, mech_rarity_err = AdventurerS1DataAppealService.normalize_patch(mech_rarity_raw, appeal.snapshot_before or {}, allow_all_player_record_ids=False, expedition_evidence=evidence, allow_rarity_increase=True, adventurer_id=appeal.adventurer_id, appeal_id=appeal_id)
                    if mech_rarity_norm:
                        normalized = AdventurerS1DataAppealService._merge_data_appeal_patches(normalized or {}, mech_rarity_norm)
                        patch_err = None
                        logger.info('数据申诉 appeal=%s 已机械合并探险稀有度升级 patch', appeal_id)
                    elif mech_rarity_err:
                        logger.info('数据申诉 appeal=%s 机械稀有度升级 patch 未通过校验: %s', appeal_id, mech_rarity_err)
            create_item_desc_issues = AdventurerS1DataAppealService.audit_create_item_description_landing(normalized or {}, adventurer_id=appeal.adventurer_id, expedition_evidence=evidence, snapshot_before=snapshot, appeal_row=appeal)
            if create_item_desc_issues:
                raw = AdventurerS1DataAppealService.apply_create_item_description_audit(raw, create_item_desc_issues)
                logger.info('数据申诉 appeal=%s 补发物品描述落库审计: %s', appeal_id, [issue.get('code') for issue in create_item_desc_issues])
            delete_comp_issues = AdventurerS1DataAppealService.audit_delete_compensation(raw_patch=patch_raw, normalized_patch=normalized or {})
            if delete_comp_issues:
                raw = AdventurerS1DataAppealService.apply_delete_compensation_audit(raw, delete_comp_issues)
                logger.info('数据申诉 appeal=%s 删除补偿审计: %s', appeal_id, [issue.get('code') for issue in delete_comp_issues])
            phantom_delete_issues = AdventurerS1DataAppealService.audit_phantom_expedition_deletes(raw_patch=patch_raw, normalized_patch=normalized or {}, expedition_evidence=evidence)
            if phantom_delete_issues:
                raw = AdventurerS1DataAppealService.apply_phantom_expedition_delete_audit(raw, phantom_delete_issues)
                logger.info('数据申诉 appeal=%s 幻影探险消耗 delete 审计: %s', appeal_id, [issue.get('code') for issue in phantom_delete_issues])
            request_checklist = raw.get('request_checklist') if isinstance(raw.get('request_checklist'), list) else []
            has_primary_request_needing_review = bool(request_checklist) and any((not isinstance(row, dict) or str(row.get('status') or '').strip().lower() != 'done' for row in request_checklist))
            force_item_rejection_review = AdventurerS1DataAppealService._has_rejected_expedition_item_request(request_checklist, evidence)
            needs_review = has_primary_request_needing_review or force_item_rejection_review or AdventurerS1DataAppealService._has_privileged_operations(normalized) or AdventurerS1DataAppealService._patch_has_trait_archive_updates(normalized) or AdventurerS1DataAppealService._patch_has_trait_archive_updates(patch_raw) or AdventurerS1DataAppealService._player_note_has_instruction_injection(note) or AdventurerS1DataAppealService._patch_has_description_rewrite_updates(normalized) or AdventurerS1DataAppealService._patch_has_description_rewrite_updates(patch_raw) or AdventurerS1DataAppealService._patch_has_asset_recovery_updates(normalized) or AdventurerS1DataAppealService._patch_has_asset_recovery_updates(patch_raw) or AdventurerS1DataAppealService._patch_has_trait_recovery_updates(normalized) or AdventurerS1DataAppealService._patch_has_trait_recovery_updates(patch_raw) or AdventurerS1DataAppealService._patch_makes_trait_recovery_easier(normalized, snapshot) or AdventurerS1DataAppealService._patch_makes_trait_recovery_easier(patch_raw, snapshot) or bool(patch_err) or bool(trait_desc_issues) or bool(item_qty_issues) or bool(delete_comp_issues) or bool(phantom_delete_issues) or AdventurerS1DataAppealService._patch_has_deletes(normalized or {}) or AdventurerS1DataAppealService._patch_has_deletes(patch_raw) or AdventurerS1DataAppealService._patch_references_outside_snapshot(patch_raw, snapshot) or has_substantive_name_update or AdventurerS1DataAppealService._patch_changes_item_type(normalized or {}, snapshot) or AdventurerS1DataAppealService._patch_changes_item_type(patch_raw, snapshot) or AdventurerS1DataAppealService._patch_increases_item_quantity(normalized, snapshot) or AdventurerS1DataAppealService._patch_increases_item_quantity(patch_raw, snapshot) or bool((evidence or {}).get('equipment_description_inheritance_candidates') if isinstance(evidence, dict) else False) or bool((evidence or {}).get('equipment_rarity_restore_candidates') if isinstance(evidence, dict) else False)
            review_runtime_metadata: dict[str, object] = {}
            if needs_review:
                from plugins.adventurer_s1.services.adventurer_s1_data_appeal_review_ai_service import call_data_appeal_review_ai
                review_model = None
                review_raw = call_data_appeal_review_ai(appeal_id, player_note=note, snapshot_before=snapshot, appeal_adventurer=appeal_adventurer, related_records=related_records, lookup_results=lookup_results, recent_activity=recent_activity, expedition_evidence=evidence, deepseek_response=raw, normalized_patch=normalized, normalization_error=patch_err, prior_appeal_grants=prior_appeal_grants, party_roster=party_roster, team_pool_status=team_pool_status, appeal_fact_sheet=appeal_fact_sheet, appeal_category='data_fix' if is_data_fix else 'other', runtime_metadata_out=review_runtime_metadata)
                review_ai_token_usage = review_runtime_metadata.get('deferred_token_usage')
                review_ai_contract_candidate = review_runtime_metadata.get('contract_valid') is True
                review_model = str(review_runtime_metadata.get('model') or review_model or '').strip() or None
            else:
                review_model = None
                review_raw = {'decision': 'allow', 'summary': '无高权限操作，已机械放行', 'skipped_review': True}
                logger.info('数据申诉 appeal=%s 跳过独立复核（无高权限操作）', appeal_id)
            appeal = AdventurerS1DataAppealService._reload_appeal_for_ai_write(appeal_id, claim_token, for_update=True)
            if not appeal:
                logger.info('数据申诉独立复核中止 appeal=%s: 租约或工单状态已变化', appeal_id)
                return
            if resume_stage == 'review_ai':
                review_raw = AdventurerS1DataAppealService._preserve_completed_review_retry_state(previous_review_raw, review_raw if isinstance(review_raw, dict) else None)
                appeal.review_supplement_patch = None
            appeal.review_ai_model = review_model
            review_decision = None
            supplement_failure_err = None
            objective_name_update_ids = AdventurerS1DataAppealService._collect_objective_name_update_ids(review_raw if isinstance(review_raw, dict) else None)
            custom_desc_clear_ids = AdventurerS1DataAppealService._collect_custom_desc_clear_ids(review_raw if isinstance(review_raw, dict) else None)
            if isinstance(review_raw, dict):
                raw_supplement = review_raw.get('supplement_patch')
                raw_override = review_raw.get('override_patch')
                raw_clear_fields = review_raw.get('clear_fields') if isinstance(review_raw.get('clear_fields'), dict) else None
                if review_raw.get('verdict_contract_version') == 2:
                    from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions, filter_data_appeal_clear_fields_by_operation_ids, filter_data_appeal_patch_by_operation_ids, iter_data_appeal_clear_operations, iter_data_appeal_patch_operations
                    operation_decisions = effective_data_appeal_operation_decisions(review_raw.get('verdicts') if isinstance(review_raw.get('verdicts'), list) else None)
                    allowed_operation_ids = {operation_id for operation_id, decision in operation_decisions.items() if decision == 'allow'}
                    authorized_review_operations = []
                    for source, candidate_patch in (('supplement', raw_supplement), ('override', raw_override)):
                        for operation in iter_data_appeal_patch_operations(candidate_patch, source=source, snapshot_before=appeal.snapshot_before):
                            if operation['operation_id'] not in allowed_operation_ids:
                                continue
                            authorized_review_operations.append({**{key: copy.deepcopy(value) for key, value in operation.items() if key != '_payload'}, 'payload': copy.deepcopy(operation.get('_payload') or {})})
                    for operation in iter_data_appeal_clear_operations(raw_clear_fields):
                        if operation['operation_id'] not in allowed_operation_ids:
                            continue
                        authorized_review_operations.append({**{key: copy.deepcopy(value) for key, value in operation.items() if key != '_payload'}, 'payload': copy.deepcopy(operation.get('_payload') or {})})
                    raw_supplement = filter_data_appeal_patch_by_operation_ids(raw_supplement, source='supplement', allowed_operation_ids=allowed_operation_ids)
                    raw_override = filter_data_appeal_patch_by_operation_ids(raw_override, source='override', allowed_operation_ids=allowed_operation_ids)
                    raw_clear_fields = filter_data_appeal_clear_fields_by_operation_ids(raw_clear_fields, allowed_operation_ids=allowed_operation_ids)
                    if raw_supplement:
                        review_raw['supplement_patch'] = raw_supplement
                    else:
                        review_raw.pop('supplement_patch', None)
                    if raw_override:
                        review_raw['override_patch'] = raw_override
                    else:
                        review_raw.pop('override_patch', None)
                    if raw_clear_fields:
                        review_raw['clear_fields'] = raw_clear_fields
                    else:
                        review_raw.pop('clear_fields', None)
                    review_raw['_backend_authorized_review_operation_patches'] = {'supplement': raw_supplement or {}, 'override': raw_override or {}, 'clear_fields': raw_clear_fields or {}}
                    review_raw['_backend_authorized_review_operations'] = authorized_review_operations
                clear_patch = AdventurerS1DataAppealService._build_clear_fields_patch(raw_clear_fields)
                if AdventurerS1DataAppealService._patch_has_operations(raw_supplement) or AdventurerS1DataAppealService._patch_has_operations(raw_override) or clear_patch:
                    raw_supplement = raw_supplement if isinstance(raw_supplement, dict) else {}
                    if AdventurerS1DataAppealService._patch_has_operations(raw_supplement):
                        review_raw['_backend_supplement_had_operations'] = True
                    if isinstance(raw_override, dict):
                        raw_supplement = AdventurerS1DataAppealService._merge_data_appeal_patches(raw_supplement, raw_override, allow_field_overwrite=True)
                    if clear_patch:
                        raw_supplement = AdventurerS1DataAppealService._merge_data_appeal_patches(raw_supplement, clear_patch, allow_field_overwrite=True)
                    review_raw['supplement_patch'] = raw_supplement
                    raw_supplement, removed_supp_cross_grants = AdventurerS1DataAppealService.strip_cross_adventurer_self_grants(raw_supplement, appeal_adventurer_id=appeal.adventurer_id, party_roster=party_roster)
                    if removed_supp_cross_grants:
                        review_ai_contract_candidate = False
                        review_raw['supplement_patch'] = raw_supplement
                        AdventurerS1DataAppealService._append_cross_adventurer_grant_warnings(review_raw, removed_supp_cross_grants)
                        logger.warning('数据申诉 appeal=%s 剔除二审跨队友普通补发: %s', appeal_id, removed_supp_cross_grants)
                    raw_supplement, removed_supp_unsupported_grants = AdventurerS1DataAppealService.strip_unsupported_expedition_self_grants(raw_supplement, appeal_adventurer=appeal_adventurer, expedition_evidence=evidence, party_roster=party_roster, recent_activity=recent_activity)
                    if removed_supp_unsupported_grants:
                        review_ai_contract_candidate = False
                        review_raw['supplement_patch'] = raw_supplement
                        AdventurerS1DataAppealService._append_unsupported_expedition_grant_warnings(review_raw, removed_supp_unsupported_grants)
                        logger.warning('数据申诉 appeal=%s 剔除二审缺少本人归属证明的探险普通补发: %s', appeal_id, removed_supp_unsupported_grants)
                    raw_supplement, blocked_supp_direct_deletes = strip_uncontracted_trait_skill_deletes(raw_supplement)
                    review_raw['supplement_patch'] = raw_supplement
                    if blocked_supp_direct_deletes:
                        review_ai_contract_candidate = False
                        issues = review_raw.get('issues') if isinstance(review_raw.get('issues'), list) else []
                        issues.append({'severity': 'high', 'detail': '特质/技能删除未使用破坏性动作契约，后端已拒绝这些删除。'})
                        review_raw['issues'] = issues
                    supplement_allow_rarity = AdventurerS1DataAppealService._should_allow_expedition_rarity_increase(raw_supplement, appeal.snapshot_before or {}, appeal.adventurer_id, expedition_log_id)
                    supplement_contract_status: dict = {}
                    supplement_norm, supplement_err = AdventurerS1DataAppealService.normalize_patch(raw_supplement, appeal.snapshot_before or {}, all_player_records=all_player_records, narrative_currency_cap=narrative_currency_cap, allow_quantity_increase=True, allow_rarity_increase=supplement_allow_rarity, expedition_evidence=evidence, allow_objective_name_update_ids=objective_name_update_ids, allow_custom_desc_clear_ids=custom_desc_clear_ids, adventurer_id=appeal.adventurer_id, appeal_id=appeal_id, contract_status_out=supplement_contract_status)
                    if supplement_contract_status.get('contract_valid') is not True:
                        review_ai_contract_candidate = False
                    if supplement_norm:
                        appeal.review_supplement_patch = supplement_norm
                        logger.info('数据申诉 appeal=%s 陪审团 2 号补充 patch 已规范化', appeal_id)
                    else:
                        supplement_failure_err = supplement_err
                        logger.info('数据申诉 appeal=%s 陪审团 2 号补充 patch 未通过校验: %s', appeal_id, supplement_err)
            if not AdventurerS1DataAppealService._patch_has_operations(normalized) and isinstance(review_raw, dict) and patch_raw:
                backfill_raw = AdventurerS1DataAppealService._build_patch_from_review_verdicts(patch_raw, review_raw)
                if backfill_raw:
                    backfill_contract_status: dict = {}
                    backfill_norm, backfill_err = AdventurerS1DataAppealService.normalize_patch(backfill_raw, appeal.snapshot_before or {}, all_player_records=all_player_records, narrative_currency_cap=narrative_currency_cap, allow_quantity_increase=True, allow_all_player_record_ids=False, expedition_evidence=evidence, allow_objective_name_update_ids=objective_name_update_ids, allow_custom_desc_clear_ids=custom_desc_clear_ids, adventurer_id=appeal.adventurer_id, appeal_id=appeal_id, contract_status_out=backfill_contract_status)
                    if backfill_contract_status.get('contract_valid') is not True:
                        review_ai_contract_candidate = False
                    if backfill_norm:
                        normalized = backfill_norm
                        patch_err = None
                        logger.info('数据申诉 appeal=%s 复核 allow 回填 executable patch', appeal_id)
                    elif backfill_err:
                        logger.info('数据申诉 appeal=%s 复核 allow 回填 patch 仍未通过校验: %s', appeal_id, backfill_err)
            if isinstance(review_raw, dict):
                enforced_review_raw = AdventurerS1DataAppealService._enforce_review_executable_contract(review_raw, normalized_patch=normalized, supplement_patch=appeal.review_supplement_patch, patch_err=patch_err)
                if enforced_review_raw is not review_raw:
                    review_ai_contract_candidate = False
                review_raw = enforced_review_raw
                enforced_review_raw = AdventurerS1DataAppealService._enforce_review_supplement_failure_contract(review_raw, normalized_patch=normalized, supplement_patch=appeal.review_supplement_patch, supplement_err=supplement_failure_err, snapshot_before=appeal.snapshot_before or {}, primary_raw_patch=patch_raw)
                if enforced_review_raw is not review_raw:
                    review_ai_contract_candidate = False
                review_raw = enforced_review_raw
            if isinstance(review_raw, dict):
                review_decision = str(review_raw.get('decision') or '').strip().lower()
                appeal.review_ai_raw_response = json_dumps_safe(review_raw)
                appeal.review_ai_summary = str(review_raw.get('summary') or '').strip() or '独立复核已完成'
                if review_decision in ('allow', 'reject', 'pending'):
                    appeal.review_ai_status = 'done'
                else:
                    appeal.review_ai_status = 'failed'
                    retry_budget_consumed = review_runtime_metadata.get('positive_tokens_consumed')
                    review_raw['_backend_counts_toward_model_retry'] = retry_budget_consumed if type(retry_budget_consumed) is bool else True
                    failure_stage = str(review_raw.get('failure_stage') or '').strip()
                    appeal.review_ai_summary = f'独立复核未完成（{failure_stage}），本轮不形成业务驳回' if failure_stage else '独立复核未完成，本轮不形成业务驳回'
            else:
                appeal.review_ai_status = 'failed'
                appeal.review_ai_summary = '独立复核调用失败；本轮不执行修复，也不形成业务驳回'
            review_ai_contract_candidate = review_ai_contract_candidate and appeal.review_ai_status == 'done'
            has_executable = AdventurerS1DataAppealService._appeal_has_executable_patch(normalized, appeal.review_supplement_patch)
            if not has_executable:
                raw['core_request_satisfied'] = False
            raw = AdventurerS1DataAppealService.finalize_player_visible_response(raw, player_note=note, normalized_patch=normalized if AdventurerS1DataAppealService._patch_has_operations(normalized or {}) else None, supplement_patch=appeal.review_supplement_patch, patch_raw=patch_raw, patch_error=patch_err if not has_executable else None)
            appeal.ai_raw_response = json_dumps_safe(raw)
            appeal.ai_summary = summary
            appeal.core_request_satisfied = raw.get('core_request_satisfied') if type(raw.get('core_request_satisfied')) is bool else None
            if normalized:
                appeal.proposed_patch = normalized
                appeal.ai_status = 'done'
            elif patch_err == 'patch 中没有有效操作':
                appeal.ai_status = 'done'
                appeal.proposed_patch = None
            else:
                appeal.ai_status = 'failed'
                appeal.ai_summary = f'{summary}（但 patch 校验未通过：{patch_err}）'
            primary_ai_contract_candidate = primary_ai_contract_candidate and appeal.ai_status == 'done'
            appeal.status = 'processing'
            completed_ai_status = appeal.ai_status
            ok, err = db_manager.commit()
            if not ok:
                logger.error('数据申诉 AI 结果落库错误: %s', err)
                return
            logger.info('数据申诉 AI 完成 appeal=%s -> AI 自动执行 ai_status=%s', appeal_id, completed_ai_status)
            db_manager.remove()
            consumer_terminal = AdventurerS1DataAppealService._complete_ai_review(appeal_id, claim_token=claim_token)
            persisted_appeal = AdventurerS1DataAppealService._load_appeal(appeal_id)
            persisted_review_raw = persisted_appeal._parse_json_text(persisted_appeal.review_ai_raw_response) or {} if persisted_appeal else {}
            consumer_accepted_pending = bool(persisted_appeal and persisted_appeal.status == 'processing' and (persisted_review_raw.get('resolution_state') == 'pending'))
            db_manager.remove()
            if consumer_terminal or consumer_accepted_pending:
                primary_ai_contract_succeeded = primary_ai_contract_candidate
                review_ai_contract_succeeded = review_ai_contract_candidate
            elif primary_ai_contract_candidate:
                primary_ai_consumer_failure_code = 'consumer.finalization_incomplete'
        except Exception:
            logger.exception('数据申诉 AI 异常 appeal=%s', appeal_id)
            if ai_started:
                AdventurerS1DataAppealService._mark_ai_interrupted(appeal_id, 'AI 处理异常中断，未生成可执行结论', claim_token=claim_token)
        finally:
            if review_ai_token_usage is not None:
                try:
                    review_ai_token_usage.finalize(is_success=review_ai_contract_succeeded)
                except Exception:
                    logger.warning('数据申诉二审 token 落账失败 appeal=%s success=%s', appeal_id, review_ai_contract_succeeded, exc_info=True)
            if primary_ai_token_usage:
                if not primary_ai_contract_succeeded:
                    primary_ai_token_usage['consumer_failure_stage'] = 'consumer_contract'
                    primary_ai_token_usage['consumer_validation_error_code'] = primary_ai_consumer_failure_code or 'consumer.contract_failed'
                from plugins.adventurer_s1.services.adventurer_s1_data_appeal_ai_service import record_deferred_data_appeal_ai_usage
                record_deferred_data_appeal_ai_usage(primary_ai_token_usage, is_success=primary_ai_contract_succeeded)

    @staticmethod
    def requeue_stuck_ai(stale_minutes: int=None, *, startup_recovery: bool=False) -> int:
        """有界轮转恢复普通申诉，且不跨队列调用持有数据库连接。"""
        from flask import current_app
        from services.async_task_service import AsyncTaskService
        global _data_appeal_recovery_cursor
        if stale_minutes is None:
            try:
                stale_minutes = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AI_STALE_MINUTES', 5))
            except (TypeError, ValueError):
                stale_minutes = 5
        stale_minutes = max(1, stale_minutes)
        batch_size = _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_BATCH_SIZE', 100, maximum=1000)
        budget_seconds = _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_BUDGET_SECONDS', 10, maximum=120)
        started_at = time.monotonic()
        if not _DATA_APPEAL_RECOVERY_LOCK.acquire(blocking=False):
            logger.info('S1 数据申诉 recovery 已在执行，本轮跳过')
            return 0
        requeued = 0
        inspected = 0
        item_errors = 0
        budget_exhausted = False
        try:
            columns = (AdventurerS1DataAppeal.id, AdventurerS1DataAppeal.status, AdventurerS1DataAppeal.appeal_category, AdventurerS1DataAppeal.review_ai_model, AdventurerS1DataAppeal.ai_status, AdventurerS1DataAppeal.review_ai_raw_response, AdventurerS1DataAppeal.ai_claim_token, AdventurerS1DataAppeal.ai_heartbeat_at, AdventurerS1DataAppeal.ai_claimed_at, AdventurerS1DataAppeal.ai_owner_boot_id, AdventurerS1DataAppeal.review_ai_status, AdventurerS1DataAppeal.proposed_patch, AdventurerS1DataAppeal.review_supplement_patch, AdventurerS1DataAppeal.adventurer_id)
            base_query = db_manager.query(*columns).filter(AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review')), ~AdventurerS1DataAppeal.appeal_category.in_(('revert_request', 'tavern_relief')))
            rows = base_query.filter(AdventurerS1DataAppeal.id > _data_appeal_recovery_cursor).order_by(AdventurerS1DataAppeal.id.asc()).limit(batch_size).all()
            if len(rows) < batch_size:
                rows.extend(base_query.filter(AdventurerS1DataAppeal.id <= _data_appeal_recovery_cursor).order_by(AdventurerS1DataAppeal.id.asc()).limit(batch_size - len(rows)).all())
            appeals = [SimpleNamespace(id=int(row[0]), status=row[1], appeal_category=row[2], review_ai_model=row[3], ai_status=row[4], review_ai_raw_response=row[5], ai_claim_token=row[6], ai_heartbeat_at=row[7], ai_claimed_at=row[8], ai_owner_boot_id=row[9], review_ai_status=row[10], proposed_patch=row[11], review_supplement_patch=row[12], adventurer_id=int(row[13]), _parse_json_text=AdventurerS1DataAppeal._parse_json_text) for row in rows]
            db_manager.remove()
            exhausted_ids = [int(appeal.id) for appeal in appeals if appeal.status == 'processing' and AdventurerS1DataAppealService._appeal_auto_retry_exhausted_stage(appeal)]
            finalized_ids = set(AdventurerS1DataAppealService._finalize_exhausted_appeal_auto_retries(exhausted_ids))
            db_manager.remove()
            finalized_events = _load_data_appeal_event_snapshots(finalized_ids)
            task_type = 'ai_s1_data_appeal'
            for appeal in appeals:
                if inspected and time.monotonic() - started_at >= budget_seconds:
                    budget_exhausted = True
                    break
                inspected += 1
                appeal_id = int(appeal.id)
                _data_appeal_recovery_cursor = appeal_id
                payload = {'appeal_id': appeal_id}
                try:
                    if appeal_id in finalized_ids:
                        finalized = finalized_events.get(appeal_id)
                        if finalized:
                            AdventurerS1DataAppealService._emit_data_appeal_changed(finalized)
                        continue
                    if appeal.status == 'processing' and appeal.ai_status == 'failed' and (AdventurerS1DataAppealService._appeal_auto_retry_exhausted_stage(appeal) == 'execution'):
                        continue
                    if appeal.status == 'awaiting_review':
                        if appeal.review_ai_model == AdventurerS1DataAppealService._MANUAL_TAKEOVER_REVIEW_MODEL:
                            continue
                        promoted = AdventurerS1DataAppealService._promote_legacy_non_manual_awaiting_by_id(appeal_id)
                        db_manager.remove()
                        if not promoted:
                            continue
                        promoted_snapshot = _load_data_appeal_event_snapshots([appeal_id]).get(appeal_id)
                        if promoted_snapshot:
                            if promoted_snapshot.status == 'processing':
                                AsyncTaskService.force_release_task_lock(task_type, payload)
                            AdventurerS1DataAppealService._emit_data_appeal_changed(promoted_snapshot)
                        continue
                    if appeal.ai_status in ('done', 'failed'):
                        restored = AdventurerS1DataAppealService._restore_orphaned_completed_appeal_stage(appeal_id, stale_minutes=stale_minutes)
                        db_manager.remove()
                        if not restored:
                            continue
                        restored_snapshot = _load_data_appeal_event_snapshots([appeal_id]).get(appeal_id)
                        if restored_snapshot:
                            if restored_snapshot.status == 'processing':
                                AsyncTaskService.force_release_task_lock(task_type, payload)
                            AdventurerS1DataAppealService._emit_data_appeal_changed(restored_snapshot)
                        continue
                    task_active = AsyncTaskService.is_task_enqueued_or_running(task_type, payload)
                    if startup_recovery and task_active and AsyncTaskService.has_restart_recovered_queued_task(task_type, payload):
                        continue
                    processing_recoverable = appeal.ai_status == 'processing' and AdventurerS1DataAppealService._appeal_ai_lease_is_stale(appeal, stale_minutes=stale_minutes)
                    resume_stage = AdventurerS1DataAppealService._appeal_resume_stage(appeal)
                    startup_execution_recoverable = startup_recovery and resume_stage == 'execution'
                    pending_recoverable = appeal.ai_status == 'pending' and ((AdventurerS1DataAppealService._appeal_auto_retry_ready(appeal) or startup_execution_recoverable) and (startup_recovery or not task_active))
                    if appeal.ai_status == 'processing' and (not processing_recoverable):
                        logger.info('数据申诉补投跳过 appeal=%s: 数据库租约心跳仍新鲜', appeal_id)
                        continue
                    if not (processing_recoverable or pending_recoverable):
                        continue
                    if task_active and (not startup_recovery) and (not processing_recoverable):
                        continue
                    if processing_recoverable or startup_recovery:
                        AsyncTaskService.force_release_task_lock(task_type, payload)
                    requeue_cause = 'startup_recovery' if startup_recovery else 'expired_lease' if appeal.ai_claim_token else 'unclaimed_processing' if appeal.ai_status == 'processing' else 'pending_missing_task'
                    submit_payload = {'appeal_id': appeal_id, 'invoke_reason': 'requeue_stuck', 'requeue_cause': requeue_cause}
                    if resume_stage:
                        submit_payload['resume_stage'] = resume_stage
                    if AsyncTaskService.submit_task(task_type, submit_payload):
                        logger.info('数据申诉 AI 入队 appeal=%s invoke_reason=requeue_stuck requeue_cause=%s adventurer_id=%s ai_status=%s', appeal_id, requeue_cause, appeal.adventurer_id, appeal.ai_status)
                        requeued += 1
                except Exception:
                    item_errors += 1
                    db_manager.rollback()
                    logger.exception('数据申诉 recovery 单项失败 appeal=%s', appeal_id)
                finally:
                    db_manager.remove()
            if budget_exhausted:
                logger.warning('S1 数据申诉 recovery 达到单轮耗时预算 inspected=%s candidates=%s batch_size=%s budget_seconds=%s cursor=%s', inspected, len(appeals), batch_size, budget_seconds, _data_appeal_recovery_cursor)
            if requeued:
                logger.info('S1 补投数据申诉 AI 任务 %s 条', requeued)
            if item_errors:
                raise RuntimeError(f'S1 data appeal recovery item failures: {item_errors}')
            return requeued
        finally:
            try:
                db_manager.remove()
            finally:
                _DATA_APPEAL_RECOVERY_LOCK.release()

    @staticmethod
    def requeue_stuck_tavern_relief(stale_minutes: int=None, *, startup_recovery: bool=False) -> int:
        """有界轮转恢复酒馆补贴，不跨队列调用持有数据库连接。"""
        from flask import current_app
        from services.async_task_service import AsyncTaskService
        global _tavern_relief_recovery_cursor
        if stale_minutes is None:
            try:
                stale_minutes = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AI_STALE_MINUTES', 5))
            except (TypeError, ValueError):
                stale_minutes = 5
        stale_minutes = max(1, stale_minutes)
        batch_size = _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_BATCH_SIZE', 100, maximum=1000)
        budget_seconds = _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_BUDGET_SECONDS', 10, maximum=120)
        started_at = time.monotonic()
        if not _TAVERN_RELIEF_RECOVERY_LOCK.acquire(blocking=False):
            logger.info('S1 酒馆补贴 recovery 已在执行，本轮跳过')
            return 0
        requeued = 0
        inspected = 0
        item_errors = 0
        budget_exhausted = False
        try:
            columns = (AdventurerS1DataAppeal.id, AdventurerS1DataAppeal.status, AdventurerS1DataAppeal.review_ai_model, AdventurerS1DataAppeal.ai_status, AdventurerS1DataAppeal.review_ai_raw_response, AdventurerS1DataAppeal.ai_claim_token, AdventurerS1DataAppeal.ai_heartbeat_at, AdventurerS1DataAppeal.ai_claimed_at, AdventurerS1DataAppeal.ai_owner_boot_id)
            base_query = db_manager.query(*columns).filter(AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review')), AdventurerS1DataAppeal.appeal_category == 'tavern_relief')
            rows = base_query.filter(AdventurerS1DataAppeal.id > _tavern_relief_recovery_cursor).order_by(AdventurerS1DataAppeal.id.asc()).limit(batch_size).all()
            if len(rows) < batch_size:
                rows.extend(base_query.filter(AdventurerS1DataAppeal.id <= _tavern_relief_recovery_cursor).order_by(AdventurerS1DataAppeal.id.asc()).limit(batch_size - len(rows)).all())
            appeals = [SimpleNamespace(id=int(row[0]), status=row[1], review_ai_model=row[2], ai_status=row[3], review_ai_raw_response=row[4], ai_claim_token=row[5], ai_heartbeat_at=row[6], ai_claimed_at=row[7], ai_owner_boot_id=row[8], _parse_json_text=AdventurerS1DataAppeal._parse_json_text) for row in rows]
            db_manager.remove()
            exhausted_ids = [int(appeal.id) for appeal in appeals if appeal.status == 'processing' and AdventurerS1DataAppealService._appeal_auto_retry_exhausted_stage(appeal)]
            finalized_ids = set(AdventurerS1DataAppealService._finalize_exhausted_appeal_auto_retries(exhausted_ids))
            db_manager.remove()
            finalized_events = _load_data_appeal_event_snapshots(finalized_ids)
            task_type = 'ai_s1_tavern_relief'
            for appeal in appeals:
                if inspected and time.monotonic() - started_at >= budget_seconds:
                    budget_exhausted = True
                    break
                inspected += 1
                appeal_id = int(appeal.id)
                _tavern_relief_recovery_cursor = appeal_id
                payload = {'appeal_id': appeal_id}
                try:
                    if appeal_id in finalized_ids:
                        finalized = finalized_events.get(appeal_id)
                        if finalized:
                            AdventurerS1DataAppealService._emit_data_appeal_changed(finalized)
                        continue
                    if appeal.status == 'awaiting_review':
                        if appeal.review_ai_model == AdventurerS1DataAppealService._MANUAL_TAKEOVER_REVIEW_MODEL:
                            continue
                        promoted = AdventurerS1DataAppealService._promote_legacy_non_manual_awaiting_by_id(appeal_id)
                        db_manager.remove()
                        if not promoted:
                            continue
                        promoted_snapshot = _load_data_appeal_event_snapshots([appeal_id]).get(appeal_id)
                        if promoted_snapshot:
                            if promoted_snapshot.status == 'processing':
                                AsyncTaskService.force_release_task_lock(task_type, payload)
                            AdventurerS1DataAppealService._emit_data_appeal_changed(promoted_snapshot)
                        continue
                    if appeal.ai_status not in ('pending', 'processing'):
                        continue
                    task_active = AsyncTaskService.is_task_enqueued_or_running(task_type, payload)
                    if startup_recovery and task_active and AsyncTaskService.has_restart_recovered_queued_task(task_type, payload):
                        continue
                    processing_recoverable = appeal.ai_status == 'processing' and AdventurerS1DataAppealService._appeal_ai_lease_is_stale(appeal, stale_minutes=stale_minutes)
                    pending_recoverable = appeal.ai_status == 'pending' and (AdventurerS1DataAppealService._appeal_auto_retry_ready(appeal) and (startup_recovery or not task_active))
                    if appeal.ai_status == 'processing' and (not processing_recoverable):
                        continue
                    if not (processing_recoverable or pending_recoverable):
                        continue
                    if task_active and (not startup_recovery) and (not processing_recoverable):
                        continue
                    if processing_recoverable or startup_recovery:
                        AsyncTaskService.force_release_task_lock(task_type, payload)
                    requeue_cause = 'startup_recovery' if startup_recovery else 'expired_lease' if appeal.ai_claim_token else 'unclaimed_processing' if appeal.ai_status == 'processing' else 'pending_missing_task'
                    submit_payload = {'appeal_id': appeal_id, 'invoke_reason': 'requeue_stuck', 'requeue_cause': requeue_cause}
                    if AsyncTaskService.submit_task(task_type, submit_payload):
                        logger.info('酒馆补贴 AI 补投 appeal=%s cause=%s', appeal_id, requeue_cause)
                        requeued += 1
                except Exception:
                    item_errors += 1
                    db_manager.rollback()
                    logger.exception('酒馆补贴 recovery 单项失败 appeal=%s', appeal_id)
                finally:
                    db_manager.remove()
            if budget_exhausted:
                logger.warning('S1 酒馆补贴 recovery 达到单轮耗时预算 inspected=%s candidates=%s batch_size=%s budget_seconds=%s cursor=%s', inspected, len(appeals), batch_size, budget_seconds, _tavern_relief_recovery_cursor)
            if requeued:
                logger.info('S1 补投酒馆补贴 AI 任务 %s 条', requeued)
            if item_errors:
                raise RuntimeError(f'S1 tavern relief recovery item failures: {item_errors}')
            return requeued
        finally:
            try:
                db_manager.remove()
            finally:
                _TAVERN_RELIEF_RECOVERY_LOCK.release()

    @staticmethod
    def requeue_stuck_player_revert_requests(stale_minutes: int=None, *, startup_recovery: bool=False) -> int:
        """有界补投卡住的修复撤回 AI，不跨 Redis 持有数据库连接。"""
        from flask import current_app
        from services.async_task_service import AsyncTaskService
        global _player_revert_recovery_cursor
        if stale_minutes is None:
            try:
                stale_minutes = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AI_STALE_MINUTES', 5))
            except (TypeError, ValueError):
                stale_minutes = 5
        stale_before = _portable_utc_now() - timedelta(minutes=max(1, stale_minutes))
        force_startup_takeover = bool(startup_recovery and AsyncTaskService.has_exclusive_startup_ownership())
        batch_size = _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_BATCH_SIZE', 100, maximum=1000)
        scan_size = max(batch_size, _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_SCAN_SIZE', 2000, maximum=10000))
        budget_seconds = _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_BUDGET_SECONDS', 10, maximum=120)
        started_at = time.monotonic()
        if not _PLAYER_REVERT_RECOVERY_LOCK.acquire(blocking=False):
            logger.info('S1 修复撤回 recovery 已在执行，本轮跳过')
            return 0
        requeued = 0
        scanned = 0
        inspected = 0
        item_errors = 0
        budget_exhausted = False
        try:
            scan_rows = _load_id_ordered_recovery_scan((AdventurerS1DataAppeal.id, AdventurerS1DataAppeal.appeal_category, AdventurerS1DataAppeal.player_revert_request_status, AdventurerS1DataAppeal.player_revert_request_submitted_at, AdventurerS1DataAppeal.updated_at, AdventurerS1DataAppeal.player_revert_request_stub_id), cursor=_player_revert_recovery_cursor, scan_size=scan_size)
            snapshots: dict[int, SimpleNamespace] = {}
            visit_rows = []
            for row in scan_rows:
                visit_rows.append(row)
                anchor = row[3] or row[4]
                if row[1] != 'revert_request' and row[2] == 'processing' and (force_startup_takeover or anchor is None or anchor < stale_before):
                    appeal_id = int(row[0])
                    snapshots[appeal_id] = SimpleNamespace(id=appeal_id, player_revert_request_stub_id=row[5])
                    if len(snapshots) >= batch_size:
                        break
            db_manager.remove()
            task_type = 'ai_s1_data_appeal_revert_request'
            for row in visit_rows:
                if scanned and time.monotonic() - started_at >= budget_seconds:
                    budget_exhausted = True
                    break
                scanned += 1
                appeal_id = int(row[0])
                _player_revert_recovery_cursor = appeal_id
                appeal = snapshots.get(appeal_id)
                if appeal is None:
                    continue
                inspected += 1
                payload = {'target_appeal_id': appeal_id, 'stub_id': appeal.player_revert_request_stub_id}
                try:
                    if force_startup_takeover and AsyncTaskService.has_restart_recovered_queued_task(task_type, payload):
                        continue
                    if not force_startup_takeover and AsyncTaskService.is_task_enqueued_or_running(task_type, payload):
                        continue
                    AsyncTaskService.force_release_task_lock(task_type, payload)
                    if AsyncTaskService.submit_task(task_type, payload):
                        logger.info('修复撤回 AI 入队 target_appeal=%s stub=%s', appeal_id, appeal.player_revert_request_stub_id)
                        requeued += 1
                except Exception:
                    item_errors += 1
                    logger.exception('修复撤回 recovery 单项失败 target_appeal=%s', appeal_id)
            if budget_exhausted:
                logger.warning('S1 修复撤回 recovery 达到单轮耗时预算 scanned=%s inspected=%s candidates=%s scan_size=%s batch_size=%s budget_seconds=%s cursor=%s', scanned, inspected, len(snapshots), scan_size, batch_size, budget_seconds, _player_revert_recovery_cursor)
            if requeued:
                logger.info('S1 补投修复撤回 AI 任务 %s 条', requeued)
            if item_errors:
                raise RuntimeError(f'S1 player revert recovery item failures: {item_errors}')
            return requeued
        finally:
            try:
                db_manager.remove()
            finally:
                _PLAYER_REVERT_RECOVERY_LOCK.release()

    @staticmethod
    def requeue_stuck_player_mimo_reviews(stale_minutes: int=None, *, startup_recovery: bool=False) -> int:
        """有界补投丢失的玩家独立复核，不跨 Redis 持有数据库连接。"""
        from flask import current_app
        from services.async_task_service import AsyncTaskService
        global _player_mimo_recovery_cursor
        if stale_minutes is None:
            try:
                stale_minutes = int(current_app.config.get('ADVENTURER_S1_DATA_APPEAL_AI_STALE_MINUTES', 5))
            except (TypeError, ValueError):
                stale_minutes = 5
        stale_before = _portable_utc_now() - timedelta(minutes=max(1, stale_minutes))
        force_startup_takeover = bool(startup_recovery and AsyncTaskService.has_exclusive_startup_ownership())
        batch_size = _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_BATCH_SIZE', 100, maximum=1000)
        scan_size = max(batch_size, _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_SCAN_SIZE', 2000, maximum=10000))
        budget_seconds = _positive_recovery_config_int('ADVENTURER_S1_DATA_APPEAL_RECOVERY_BUDGET_SECONDS', 10, maximum=120)
        started_at = time.monotonic()
        if not _PLAYER_MIMO_RECOVERY_LOCK.acquire(blocking=False):
            logger.info('S1 玩家独立复核 recovery 已在执行，本轮跳过')
            return 0
        requeued = 0
        scanned = 0
        inspected = 0
        item_errors = 0
        budget_exhausted = False
        try:
            scan_rows = _load_id_ordered_recovery_scan((AdventurerS1DataAppeal.id, AdventurerS1DataAppeal.player_review_status, AdventurerS1DataAppeal.updated_at), cursor=_player_mimo_recovery_cursor, scan_size=scan_size)
            selected_ids = []
            visit_rows = []
            for row in scan_rows:
                visit_rows.append(row)
                if row[1] == 'pending' and (force_startup_takeover or row[2] is None or row[2] < stale_before):
                    selected_ids.append(int(row[0]))
                    if len(selected_ids) >= batch_size:
                        break
            if selected_ids:
                request_rows = db_manager.query(AdventurerS1DataAppeal.id, AdventurerS1DataAppeal.player_review_requests).filter(AdventurerS1DataAppeal.id.in_(selected_ids)).limit(len(selected_ids)).all()
            else:
                request_rows = []
            snapshots = {int(row[0]): SimpleNamespace(id=int(row[0]), player_review_requests=row[1]) for row in request_rows}
            db_manager.remove()
            task_type = 'ai_s1_data_appeal_mimo_review'
            for row in visit_rows:
                if scanned and time.monotonic() - started_at >= budget_seconds:
                    budget_exhausted = True
                    break
                scanned += 1
                appeal_id = int(row[0])
                _player_mimo_recovery_cursor = appeal_id
                appeal = snapshots.get(appeal_id)
                if appeal is None:
                    continue
                inspected += 1
                try:
                    pending_request = next((entry for entry in reversed(appeal.player_review_requests or []) if isinstance(entry, dict) and entry.get('status') == 'pending'), None)
                    player_request = str((pending_request or {}).get('request') or '').strip()
                    if not player_request:
                        logger.warning('玩家独立复核补投跳过 appeal=%s: 缺少 pending 请求正文', appeal_id)
                        continue
                    payload = {'appeal_id': appeal_id, 'player_request': player_request}
                    if force_startup_takeover and AsyncTaskService.has_restart_recovered_queued_task(task_type, payload):
                        continue
                    if not force_startup_takeover and AsyncTaskService.is_task_enqueued_or_running(task_type, payload):
                        continue
                    AsyncTaskService.force_release_task_lock(task_type, payload)
                    if AsyncTaskService.submit_task(task_type, payload):
                        logger.info('玩家独立复核 AI 入队 appeal=%s', appeal_id)
                        requeued += 1
                except Exception:
                    item_errors += 1
                    logger.exception('玩家独立复核 recovery 单项失败 appeal=%s', appeal_id)
            if budget_exhausted:
                logger.warning('S1 玩家独立复核 recovery 达到单轮耗时预算 scanned=%s inspected=%s candidates=%s scan_size=%s batch_size=%s budget_seconds=%s cursor=%s', scanned, inspected, len(snapshots), scan_size, batch_size, budget_seconds, _player_mimo_recovery_cursor)
            if requeued:
                logger.info('S1 补投玩家独立复核 AI 任务 %s 条', requeued)
            if item_errors:
                raise RuntimeError(f'S1 player MIMO recovery item failures: {item_errors}')
            return requeued
        finally:
            try:
                db_manager.remove()
            finally:
                _PLAYER_MIMO_RECOVERY_LOCK.release()

    @staticmethod
    def _release_player_revert_task_lock(target_appeal_id: int, stub_id: int | None=None) -> None:
        from services.async_task_service import AsyncTaskService
        AsyncTaskService.force_release_task_lock('ai_s1_data_appeal_revert_request', {'target_appeal_id': int(target_appeal_id), 'stub_id': stub_id})

    @staticmethod
    def _release_ai_task_lock(appeal_id: int, *, task_type: str='ai_s1_data_appeal') -> None:
        from services.async_task_service import AsyncTaskService
        AsyncTaskService.force_release_task_lock(task_type, {'appeal_id': int(appeal_id)})

    @staticmethod
    def _sync_fields_on_cancel(appeal: AdventurerS1DataAppeal, *, cancelled_by: str) -> None:
        """取消时同步 AI 字段，避免列表/详情残留「分析中」「等待复核」。"""
        ai_never_finished = (appeal.ai_status or '') in ('pending', 'processing') and (not appeal.ai_raw_response)
        if ai_never_finished:
            appeal.ai_status = 'cancelled'
            appeal.ai_summary = '玩家已取消申诉，未执行 AI 分析' if cancelled_by == 'player' else '申诉已由管理员取消，未完成 AI 分析'
        review_never_finished = (appeal.review_ai_status or '') in ('pending', 'processing') and (not appeal.review_ai_raw_response)
        if review_never_finished:
            appeal.review_ai_status = 'cancelled'
            if not appeal.review_ai_summary:
                appeal.review_ai_summary = '申诉已取消，未进入二次复核'
        AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)

    @staticmethod
    def _build_cancellation_display(appeal: AdventurerS1DataAppeal) -> dict:
        """cancelled 工单的展示语义（区分玩家取消 / 管理员取消 / AI 撤回）。"""
        reason = (appeal.reject_reason or '').strip()
        if reason == '玩家已取消':
            kind = 'player'
            status_label = '玩家取消'
            outcome_hint = '玩家在 AI 分析完成前主动取消了本次申诉，未产生任何数据变更。常见原因是发现漏选条目或描述不完整后重新提交。'
            reject_reason_label = '取消说明'
        elif reason == '管理员已取消' or (reason.startswith('管理员') and '取消' in reason):
            kind = 'admin'
            status_label = '管理员取消'
            outcome_hint = '管理员在 AI 完成前取消了本次申诉，未产生数据变更。'
            reject_reason_label = '取消说明'
        elif AdventurerS1DataAppealService._is_player_withdrawal_intent(appeal):
            kind = 'withdrawal'
            status_label = '已撤回'
            outcome_hint = '玩家表达了撤回本次申诉的意图，或 AI 判定无需修改数据。'
            reject_reason_label = '撤回说明'
        else:
            kind = 'other'
            status_label = '已结束'
            outcome_hint = '本次申诉已关闭，未通过审核流程。'
            reject_reason_label = '结束原因'
        data = {'cancellation_kind': kind, 'status_label': status_label, 'outcome_hint': outcome_hint, 'reject_reason_label': reject_reason_label}
        if kind in ('player', 'admin'):
            if (appeal.ai_status or '') in ('pending', 'processing') and (not appeal.ai_raw_response):
                data['ai_status'] = 'cancelled'
                data['ai_summary'] = '玩家已取消申诉，未执行 AI 分析' if kind == 'player' else '申诉已由管理员取消，未完成 AI 分析'
            if (appeal.review_ai_status or '') in ('pending', 'processing') and (not appeal.review_ai_raw_response):
                data['review_ai_status'] = 'cancelled'
                data['review_ai_summary'] = '申诉已取消，未进入二次复核'
        return data

    @staticmethod
    def can_refund_rarity_review_guard_failure(appeal: AdventurerS1DataAppeal) -> bool:
        """历史稀有度复核 guard 误杀单：AI 已给 rarity patch，但后端因空 section 误判失败。"""
        if not appeal:
            return False
        if (appeal.appeal_category or 'other') != 'rarity_review':
            return False
        if (appeal.status or '') != 'rejected':
            return False
        if AdventurerS1DataAppealService._appeal_has_revertible_audits(appeal.id):
            return False
        raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
        if not AdventurerS1DataAppealService._patch_has_operations(raw.get('patch')):
            return False
        summary_text = ' '.join((str(x or '') for x in (appeal.ai_summary, appeal.review_ai_summary, appeal.reject_reason)))
        return _RARITY_REVIEW_GUARD_ERROR in summary_text

    @staticmethod
    def _add_quota_bonus_no_commit(user_id: int, count: int, admin_id: int, quota_pool: str=_APPEAL_QUOTA_POOL_STANDARD) -> tuple:
        from utils.time_utils import get_beijing_today_start_utc
        quota_pool = AdventurerS1DataAppealService._normalize_quota_pool(quota_pool)
        if not quota_pool:
            return (None, '申诉额度类型无效')
        now = _portable_utc_now()
        day_start = get_beijing_today_start_utc()
        _, bonus_field, bonus_at_field = AdventurerS1DataAppealService._quota_adjustment_fields(quota_pool)
        adj = db_manager.query(AdventurerS1DataAppealQuota).filter_by(user_id=user_id).with_for_update().first()
        if not adj:
            values = {'user_id': user_id, 'bonus': 0, 'other_bonus': 0, bonus_field: count, bonus_at_field: now, 'updated_by': admin_id, 'updated_at': now}
            adj = AdventurerS1DataAppealQuota(**values)
            ok, err = db_manager.add(adj)
            if not ok:
                return (None, err or '返还次数失败')
        else:
            current_value = getattr(adj, bonus_field)
            current_at = getattr(adj, bonus_at_field)
            current_bonus = int(current_value) if current_value and current_at and (current_at >= day_start) else 0
            setattr(adj, bonus_field, current_bonus + count)
            setattr(adj, bonus_at_field, now)
            adj.updated_by = admin_id
            adj.updated_at = now
        return (True, None)

    @staticmethod
    def _destructive_target_persisted_payload(target: object) -> dict | None:
        if not isinstance(target, dict):
            return None
        return {key: copy.deepcopy(value) for key, value in target.items() if key != 'reason'}

    @staticmethod
    def _destructive_target_description_equivalence_key(value: str) -> str:
        """Normalize only the proven comma-width representation for comparison.

        The selected reviewed payload is persisted verbatim; this key never
        rewrites player-visible text. Broader Unicode normalization would make
        unrelated compatibility characters equal and is intentionally avoided.
        """
        return value.replace(',', '，')

    @staticmethod
    def _normalize_shared_destructive_target_group_patch(patch: dict) -> tuple[dict | None, str | None]:
        """Coalesce one mechanically monotonic shared-target merge.

        Review-approved actions may repeat the same stable target while
        progressively appending to its description.  Execute those rows with
        the longest already-approved payload so the target update and every
        source deletion remain one atomic operation group.  Divergent target
        versions stay fail-closed; choosing between them would require a new
        semantic review rather than a consumer heuristic.
        """
        normalized = copy.deepcopy(patch) if isinstance(patch, dict) else {}
        for action_section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            actions = normalized.get(action_section)
            if not isinstance(actions, list):
                continue
            shared_targets: dict[tuple[str, int], list[dict]] = defaultdict(list)
            for action in actions:
                if not isinstance(action, dict):
                    continue
                target_kind = destructive_action_target_kind(str(action.get('mode') or '').strip())
                target = action.get('target')
                target = target if isinstance(target, dict) else {}
                target_id = _normalize_appeal_patch_integer(target.get('id'), minimum=1)
                if target_kind is None or target_id is None:
                    continue
                shared_targets[target_kind, target_id].append(action)
            for (target_kind, target_id), target_actions in shared_targets.items():
                payloads = [AdventurerS1DataAppealService._destructive_target_persisted_payload(action.get('target')) for action in target_actions]
                if any((payload is None for payload in payloads)):
                    return (None, '同一合并目标缺少完整最终字段，不能自动执行')
                payloads = [payload for payload in payloads if payload is not None]
                descriptions = [payload.get('description') for payload in payloads]
                if any((not isinstance(description, str) or not description for description in descriptions)):
                    return (None, '同一合并目标存在互不兼容的最终字段，不能自动选择覆盖版本')
                canonical = max(payloads, key=lambda payload: (len(payload.get('description') or ''), sum((payload == candidate for candidate in payloads))))
                canonical = copy.deepcopy(canonical)
                canonical_description = canonical.get('description')
                if not isinstance(canonical_description, str) or not canonical_description:
                    return (None, '同一合并目标存在互不兼容的最终字段，不能自动选择覆盖版本')
                canonical_equivalence = AdventurerS1DataAppealService._destructive_target_description_equivalence_key(canonical_description)
                canonical_non_description = {key: value for key, value in canonical.items() if key != 'description'}
                if any(({key: value for key, value in payload.items() if key != 'description'} != canonical_non_description or not canonical_equivalence.startswith(AdventurerS1DataAppealService._destructive_target_description_equivalence_key(payload['description'])) for payload in payloads)):
                    return (None, '同一合并目标存在互不兼容的最终字段，不能自动选择覆盖版本')
                target_section = 'traits' if target_kind == 'trait' else 'skills'
                target_section_patch = normalized.get(target_section)
                target_updates = target_section_patch.get('update') if isinstance(target_section_patch, dict) and isinstance(target_section_patch.get('update'), list) else []
                target_updates = [update for update in target_updates if isinstance(update, dict) and _normalize_appeal_patch_integer(update.get('id'), minimum=1) == target_id]
                for target_update in target_updates:
                    update_payload = {key: value for key, value in target_update.items() if key not in ('id', 'reason', 'description')}
                    if any((key not in canonical_non_description or canonical_non_description[key] != value for key, value in update_payload.items())):
                        return (None, '合并目标的普通更新与破坏性动作最终字段不一致，不能拆分或自动选择覆盖版本')
                    update_description = target_update.get('description')
                    if update_description is None:
                        continue
                    if not isinstance(update_description, str) or not update_description:
                        return (None, '合并目标的普通更新与破坏性动作最终字段不一致，不能拆分或自动选择覆盖版本')
                    update_equivalence = AdventurerS1DataAppealService._destructive_target_description_equivalence_key(update_description)
                    if not (canonical_equivalence.startswith(update_equivalence) or update_equivalence.startswith(canonical_equivalence)):
                        return (None, '合并目标的普通更新与破坏性动作最终字段不一致，不能拆分或自动选择覆盖版本')
                    if len(update_equivalence) > len(canonical_equivalence):
                        canonical['description'] = update_description
                        canonical_description = update_description
                        canonical_equivalence = update_equivalence
                for action in target_actions:
                    original_target = action.get('target')
                    replacement = copy.deepcopy(canonical)
                    if isinstance(original_target, dict) and 'reason' in original_target:
                        replacement['reason'] = copy.deepcopy(original_target['reason'])
                    action['target'] = replacement
        return (normalized, None)

    @staticmethod
    def _shared_destructive_target_retry_snapshot(appeal: AdventurerS1DataAppeal, *, group_id: tuple, group_patch: dict, previously_successful_ids: set[str], operation_by_id: dict[str, dict], current_records: dict) -> tuple[dict | None, str | None]:
        """Rebase only a proven same-appeal append or comma-width continuation."""
        action_section = str(group_id[1])
        target_kind = str(group_id[2])
        target_id = int(group_id[3])
        canonical_target = next((action.get('target') for action in group_patch.get(action_section) or [] if isinstance(action, dict) and destructive_action_target_kind(str(action.get('mode') or '').strip()) == target_kind and (_normalize_appeal_patch_integer((action.get('target') or {}).get('id') if isinstance(action.get('target'), dict) else None, minimum=1) == target_id)), None)
        canonical_payload = AdventurerS1DataAppealService._destructive_target_persisted_payload(canonical_target)
        if canonical_payload is None:
            return (None, '同一合并目标缺少完整最终字段，不能自动执行')
        previous_operations = [operation_by_id[operation_id] for operation_id in previously_successful_ids if operation_id in operation_by_id]
        previous_payloads = [AdventurerS1DataAppealService._destructive_target_persisted_payload((operation.get('_payload') or {}).get('target') if isinstance(operation.get('_payload'), dict) else None) for operation in previous_operations]
        if any((payload == canonical_payload for payload in previous_payloads)):
            return (appeal.snapshot_before, None)
        target_section = 'traits' if target_kind == 'trait' else 'skills'
        current_target = next((row for row in current_records.get(target_section) or [] if isinstance(row, dict) and row.get('id') == target_id), None)
        prior_step_matches_current = any((AdventurerS1DataAppealService._appeal_operation_already_satisfied(appeal, operation, operation.get('_payload'), current_records) for operation in previous_operations))
        if not current_target or not prior_step_matches_current:
            return (None, '同一申诉已落库回执与当前合并目标不一致，不能自动续写')
        if all((key == 'id' or current_target.get(key) == value for key, value in canonical_payload.items())):
            return (appeal.snapshot_before, None)
        current_description = current_target.get('description')
        canonical_description = canonical_payload.get('description')
        unchanged_fields_match = all((key in ('id', 'description') or current_target.get(key) == value for key, value in canonical_payload.items()))
        current_equivalence = AdventurerS1DataAppealService._destructive_target_description_equivalence_key(current_description) if isinstance(current_description, str) else None
        canonical_equivalence = AdventurerS1DataAppealService._destructive_target_description_equivalence_key(canonical_description) if isinstance(canonical_description, str) else None
        if not isinstance(current_description, str) or not isinstance(canonical_description, str) or (not current_description) or (not canonical_equivalence.startswith(current_equivalence)) or (not unchanged_fields_match):
            return (None, '同一申诉已落库版本与后续目标不是等价或严格追加关系，不能自动覆盖')
        rebased_snapshot = copy.deepcopy(appeal.snapshot_before or {})
        frozen_rows = rebased_snapshot.get(target_section)
        if not isinstance(frozen_rows, list):
            return (None, '冻结快照缺少共享合并目标，不能自动续写')
        replaced = False
        rebased_rows = []
        for row in frozen_rows:
            if isinstance(row, dict) and row.get('id') == target_id:
                rebased_rows.append(copy.deepcopy(current_target))
                replaced = True
            else:
                rebased_rows.append(row)
        if not replaced:
            return (None, '冻结快照缺少共享合并目标，不能自动续写')
        rebased_snapshot[target_section] = rebased_rows
        return (rebased_snapshot, None)

    @staticmethod
    def _item_quantity_merge_identity(row: dict | None) -> tuple[str, str, str] | None:
        """Return the frozen identity used for a mechanically provable item merge."""
        if not isinstance(row, dict):
            return None
        name = str(row.get('name') or '').strip()
        if not name:
            return None
        return (name, str(row.get('type') or '').strip().lower(), str(row.get('rarity') or '').strip().lower())

    @staticmethod
    def _mechanical_item_quantity_merge_groups(snapshot_before: dict | None, operations: list[dict]) -> dict[str, tuple[str, str, int]]:
        """Group exact quantity-conserving item merges without reading reviewer prose.

        One allowed update and every allowed same-identity delete form a group only
        when the target quantity equals the frozen target plus all deleted stacks.
        Explicit reviewer groups are filtered by the caller before this helper.
        """
        snapshot_before = snapshot_before if isinstance(snapshot_before, dict) else {}
        grouped_operation_ids: dict[str, tuple[str, str, int]] = {}
        for section in ('equipment', 'warehouse'):
            frozen = {int(row['id']): row for row in snapshot_before.get(section) or [] if isinstance(row, dict) and _normalize_appeal_patch_integer(row.get('id'), minimum=1) is not None}
            section_operations = [operation for operation in operations if isinstance(operation, dict) and operation.get('section') == section]
            updates = [operation for operation in section_operations if operation.get('action') == 'update']
            deletes = [operation for operation in section_operations if operation.get('action') == 'delete']
            updates_by_identity: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
            deletes_by_identity: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
            for operation in updates:
                target_id = _normalize_appeal_patch_integer(operation.get('target_id'), minimum=1)
                original = frozen.get(target_id) if target_id is not None else None
                identity = AdventurerS1DataAppealService._item_quantity_merge_identity(original)
                payload = operation.get('_payload')
                payload = payload if isinstance(payload, dict) else {}
                if identity is None or 'quantity' not in payload:
                    continue
                identity_changed = any((field in payload and str(payload.get(field) or '').strip().lower() != str(original.get(field) or '').strip().lower() for field in ('name', 'type', 'rarity')))
                if not identity_changed:
                    updates_by_identity[identity].append(operation)
            for operation in deletes:
                target_id = _normalize_appeal_patch_integer(operation.get('target_id'), minimum=1)
                identity = AdventurerS1DataAppealService._item_quantity_merge_identity(frozen.get(target_id) if target_id is not None else None)
                if identity is not None:
                    deletes_by_identity[identity].append(operation)
            for identity, identity_updates in updates_by_identity.items():
                identity_deletes = deletes_by_identity.get(identity) or []
                if len(identity_updates) != 1 or not identity_deletes:
                    continue
                update_operation = identity_updates[0]
                target_id = int(update_operation['target_id'])
                target_before = frozen.get(target_id) or {}
                target_quantity = _normalize_appeal_patch_integer(target_before.get('quantity'), minimum=1)
                proposed_quantity = _normalize_appeal_patch_integer((update_operation.get('_payload') or {}).get('quantity'), minimum=1)
                deleted_quantities = [_normalize_appeal_patch_integer((frozen.get(int(operation['target_id'])) or {}).get('quantity'), minimum=1) for operation in identity_deletes]
                if target_quantity is None or proposed_quantity is None or any((quantity is None for quantity in deleted_quantities)) or (proposed_quantity <= target_quantity) or (proposed_quantity != target_quantity + sum((quantity for quantity in deleted_quantities if quantity is not None))):
                    continue
                group_id = ('item_quantity_merge', section, target_id)
                for operation in (update_operation, *identity_deletes):
                    grouped_operation_ids[operation['operation_id']] = group_id
        return grouped_operation_ids

    @staticmethod
    def _item_quantity_merge_retry_context(appeal: AdventurerS1DataAppeal, *, group_id: tuple, operation_ids: set[str], proven_operation_ids: set[str], operation_by_id: dict[str, dict], current_records: dict) -> tuple[dict[str, set[int]] | None, str | None]:
        """Validate a partially landed item merge and retain its no-op anchor."""
        section = str(group_id[1])
        target_id = int(group_id[2])
        update_operations = [operation_by_id[operation_id] for operation_id in operation_ids if operation_id in operation_by_id and operation_by_id[operation_id].get('section') == section and (operation_by_id[operation_id].get('action') == 'update')]
        if len(update_operations) != 1:
            return (None, '物品合并缺少唯一数量目标，不能自动续跑')
        update_operation = update_operations[0]
        update_id = update_operation['operation_id']
        update_payload = update_operation.get('_payload')
        update_payload = update_payload if isinstance(update_payload, dict) else {}
        frozen_rows = {int(row['id']): row for row in (appeal.snapshot_before or {}).get(section) or [] if isinstance(row, dict) and _normalize_appeal_patch_integer(row.get('id'), minimum=1) is not None}
        current_rows = {int(row['id']): row for row in current_records.get(section) or [] if isinstance(row, dict) and _normalize_appeal_patch_integer(row.get('id'), minimum=1) is not None}
        current_target = current_rows.get(target_id)
        frozen_target = frozen_rows.get(target_id)
        if not current_target or not frozen_target:
            return (None, '物品合并目标已不存在，不能自动续跑')
        preserve_updates: dict[str, set[int]] = {}
        if update_id in proven_operation_ids:
            if not AdventurerS1DataAppealService._appeal_operation_already_satisfied(appeal, update_operation, update_payload, current_records):
                return (None, '已落账数量目标与当前物品不一致，不能自动续跑删除')
            preserve_updates = {section: {target_id}}
        else:
            changed_fields = {key for key in update_payload if key != 'id'}
            if not changed_fields or any((current_target.get(field) != frozen_target.get(field) for field in changed_fields)):
                return (None, '待写入数量目标已偏离冻结状态，不能自动续跑合并')
        for operation_id in operation_ids - proven_operation_ids:
            operation = operation_by_id.get(operation_id) or {}
            if operation.get('action') != 'delete':
                continue
            source_id = _normalize_appeal_patch_integer(operation.get('target_id'), minimum=1)
            frozen_source = frozen_rows.get(source_id) if source_id is not None else None
            current_source = current_rows.get(source_id) if source_id is not None else None
            if not frozen_source or not current_source or AdventurerS1DataAppealService._item_quantity_merge_identity(current_source) != AdventurerS1DataAppealService._item_quantity_merge_identity(frozen_source) or (_normalize_appeal_patch_integer(current_source.get('quantity'), minimum=1) != _normalize_appeal_patch_integer(frozen_source.get('quantity'), minimum=1)):
                return (None, '待删除重复物品已偏离冻结身份或数量，不能自动续跑合并')
        return (preserve_updates, None)

    @staticmethod
    def _frozen_asset_update_terminal_error(appeal: AdventurerS1DataAppeal, operation: dict, current_records: dict | None) -> dict | None:
        """Classify an exact frozen item row that no longer exists for update.

        A submitted equipment/warehouse ID is the only write authority.  Once
        that row leaves the adventurer's current container, a market, trade, or
        other later flow may own the asset.  Names cannot safely relocate it,
        so repeating the same update can never repair the frozen target.
        """
        section = str(operation.get('section') or '')
        if section not in ('equipment', 'warehouse'):
            return None
        if operation.get('action') != 'update':
            return None
        if not isinstance(current_records, dict) or section not in current_records:
            return None
        current_rows = current_records.get(section)
        frozen_rows = (appeal.snapshot_before or {}).get(section) if isinstance(appeal.snapshot_before, dict) else None
        if not isinstance(current_rows, list) or not isinstance(frozen_rows, list):
            return None
        target_id = _normalize_appeal_patch_integer(operation.get('target_id'), minimum=1)
        if target_id is None:
            return None
        frozen_target = next((row for row in frozen_rows if isinstance(row, dict) and _normalize_appeal_patch_integer(row.get('id'), minimum=1) == target_id), None)
        if frozen_target is None or any((isinstance(row, dict) and _normalize_appeal_patch_integer(row.get('id'), minimum=1) == target_id for row in current_rows)):
            return None
        label = '装备' if section == 'equipment' else '仓库物品'
        return {'code': _DATA_APPEAL_STALE_ASSET_TARGET_CODE, 'detail': f'冻结{label}记录 id={target_id} 已不在申诉提交时的位置；后续流转后的当前记录不能按名称替代旧 ID 写入'}

    @staticmethod
    def _record_terminal_operation_errors(appeal: AdventurerS1DataAppeal, errors: dict[str, dict]) -> None:
        if not errors:
            return
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        existing = review_raw.get(_DATA_APPEAL_TERMINAL_OPERATION_ERRORS_KEY)
        existing = copy.deepcopy(existing) if isinstance(existing, dict) else {}
        for operation_id, issue in errors.items():
            if not operation_id or not isinstance(issue, dict):
                continue
            code = str(issue.get('code') or '').strip()
            detail = str(issue.get('detail') or '').strip()
            if not code or not detail:
                continue
            existing[operation_id] = {'code': code[:80], 'detail': detail[:300]}
        if existing:
            review_raw[_DATA_APPEAL_TERMINAL_OPERATION_ERRORS_KEY] = existing
            appeal.review_ai_raw_response = json_dumps_safe(review_raw)

    @staticmethod
    def _terminal_operation_errors(appeal: AdventurerS1DataAppeal) -> dict[str, dict]:
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        raw = review_raw.get(_DATA_APPEAL_TERMINAL_OPERATION_ERRORS_KEY)
        return {str(operation_id): copy.deepcopy(issue) for operation_id, issue in raw.items() if str(operation_id).strip() and isinstance(issue, dict)} if isinstance(raw, dict) else {}

    @staticmethod
    def _terminal_execution_reject_reason(appeal: AdventurerS1DataAppeal, *, partial: bool=False) -> str:
        terminal_errors = AdventurerS1DataAppealService._terminal_operation_errors(appeal)
        if any((issue.get('code') == 'continuation_target_changed' for issue in terminal_errors.values())):
            return '原申诉之后目标已有变化或缺少冻结记录，本项未执行；请基于当前数据重新申诉。'
        from plugins.adventurer_s1.services.s1_appeal_departure_guard import DEPARTURE_ASSET_LOCKED_CODE
        if any((issue.get('code') == DEPARTURE_ASSET_LOCKED_CODE for issue in terminal_errors.values())):
            prefix = '其它独立项目已按落账回执处理；' if partial else ''
            return prefix + '相关内容已随本次探险出发，本项申诉未执行。请在探险结束后在原申诉中继续处理；目标已有变化时需重新申诉。'
        stale_description = any((str(issue.get('code') or '').strip() == _DATA_APPEAL_STALE_ASSET_DESCRIPTION_CODE for issue in terminal_errors.values()))
        if stale_description:
            prefix = '其它独立项目已按落账回执处理；' if partial else ''
            return prefix + '申诉提交后，相关装备或仓库物品的描述又发生了后续修改，系统无法无损合并，本次该项未执行。请基于当前描述重新提交申诉。'
        stale_asset = any((str(issue.get('code') or '').strip() == _DATA_APPEAL_STALE_ASSET_TARGET_CODE for issue in terminal_errors.values()))
        if stale_asset:
            prefix = '其它独立项目已按落账回执处理；' if partial else ''
            return prefix + '申诉提交后，相关装备或仓库记录已发生后续变动，当前已不在当时冻结的位置，系统不能按旧记录安全修改。本次该项未执行，请针对当前持有记录重新提交申诉。'
        return '其它独立项目已按落账回执处理；当前项目的最终写入条件已永久失效。' if partial else '当前项目的最终写入条件已永久失效，本次未执行。'

    @staticmethod
    def _record_terminal_group_operation_error(appeal: AdventurerS1DataAppeal, operation_ids: set[str], issue: dict) -> dict[str, str]:
        """Project one terminal execution issue to an inseparable operation group."""
        code = str(issue.get('code') or '').strip()
        detail = str(issue.get('detail') or '').strip()
        if not code or not detail:
            return {}
        group_issues = {operation_id: {'code': code, 'detail': detail} for operation_id in operation_ids}
        AdventurerS1DataAppealService._record_terminal_operation_errors(appeal, group_issues)
        return {operation_id: detail for operation_id in operation_ids}

    @staticmethod
    def _apply_operation_contract_primary_groups(appeal: AdventurerS1DataAppeal, patch: dict, verdicts: list[dict], *, all_player_records: dict | None) -> tuple[set[str], dict[str, str], list[dict]]:
        """Execute v2 primary operations by effective atomic group.

        Independent operations use their own operation_id as the group, so one
        invalid row cannot roll back an unrelated sibling request.  Explicit
        reviewer groups win; destructive actions that converge on the same
        stable target, plus a compatible ordinary update of that exact target,
        are grouped mechanically as a backstop.
        """
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions, filter_data_appeal_patch_by_operation_ids, iter_data_appeal_patch_operations
        decisions = effective_data_appeal_operation_decisions(verdicts)
        verdict_by_id = {str(row.get('operation_id') or '').strip(): row for row in verdicts if isinstance(row, dict)}
        primary_operations = iter_data_appeal_patch_operations(patch, source='primary', snapshot_before=appeal.snapshot_before)
        operation_by_id = {operation['operation_id']: operation for operation in primary_operations}
        shared_target_group_ids: dict[tuple[str, int], set[tuple[str, str, str, int]]] = defaultdict(set)
        for operation in primary_operations:
            operation_id = operation['operation_id']
            verdict = verdict_by_id.get(operation_id) or {}
            if decisions.get(operation_id) != 'allow' or str(verdict.get('atomic_group_id') or '').strip() or operation.get('section') not in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
                continue
            payload = operation.get('_payload')
            payload = payload if isinstance(payload, dict) else {}
            target = payload.get('target')
            target = target if isinstance(target, dict) else {}
            target_id = _normalize_appeal_patch_integer(target.get('id'), minimum=1)
            target_kind = destructive_action_target_kind(str(payload.get('mode') or '').strip())
            if target_id is None or target_kind is None:
                continue
            shared_target_group_ids[target_kind, target_id].add(('shared_destructive_target', str(operation.get('section') or ''), target_kind, target_id))
        mechanical_item_merge_groups = AdventurerS1DataAppealService._mechanical_item_quantity_merge_groups(appeal.snapshot_before, [operation for operation in primary_operations if decisions.get(operation['operation_id']) == 'allow' and (not str((verdict_by_id.get(operation['operation_id']) or {}).get('atomic_group_id') or '').strip())])
        grouped: dict[object, set[str]] = {}
        for operation in primary_operations:
            operation_id = operation['operation_id']
            if decisions.get(operation_id) != 'allow':
                continue
            verdict = verdict_by_id.get(operation_id) or {}
            explicit_group_id = str(verdict.get('atomic_group_id') or '').strip()
            group_id: object = explicit_group_id or mechanical_item_merge_groups.get(operation_id) or operation_id
            if not explicit_group_id and operation.get('section') in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
                payload = operation.get('_payload')
                payload = payload if isinstance(payload, dict) else {}
                target = payload.get('target')
                target = target if isinstance(target, dict) else {}
                target_id = _normalize_appeal_patch_integer(target.get('id'), minimum=1)
                target_kind = destructive_action_target_kind(str(payload.get('mode') or '').strip())
                if target_id is not None and target_kind is not None:
                    group_id = ('shared_destructive_target', str(operation.get('section') or ''), target_kind, target_id)
            elif not explicit_group_id and operation.get('action') == 'update':
                target_kind = {'traits': 'trait', 'skills': 'skill'}.get(str(operation.get('section') or ''))
                target_id = _normalize_appeal_patch_integer(operation.get('target_id'), minimum=1)
                candidate_groups = shared_target_group_ids.get((target_kind, target_id), set())
                if target_kind is not None and len(candidate_groups) == 1:
                    group_id = next(iter(candidate_groups))
            grouped.setdefault(group_id, set()).add(operation_id)
        successful_ids: set[str] = set()
        operation_errors: dict[str, str] = {}
        unlandable_items: list[dict] = []
        previously_successful_ids = AdventurerS1DataAppealService._successful_operation_receipt_ids(appeal) if getattr(appeal, 'review_ai_raw_response', None) else set()
        for group_id, grouped_operation_ids in grouped.items():
            full_operation_ids = set(grouped_operation_ids)
            previously_successful_group_ids = full_operation_ids & previously_successful_ids
            is_shared_destructive_target = isinstance(group_id, tuple) and bool(group_id) and (group_id[0] == 'shared_destructive_target')
            is_item_quantity_merge = isinstance(group_id, tuple) and bool(group_id) and (group_id[0] == 'item_quantity_merge')
            if previously_successful_group_ids == full_operation_ids:
                successful_ids.update(full_operation_ids)
                continue
            terminal_errors = AdventurerS1DataAppealService._terminal_operation_errors(appeal) if getattr(appeal, 'review_ai_raw_response', None) else {}
            terminal_issue = next((terminal_errors[oid] for oid in sorted(full_operation_ids) if oid in terminal_errors and oid not in previously_successful_group_ids), None)
            if terminal_issue:
                successful_ids.update(previously_successful_group_ids)
                operation_errors.update(AdventurerS1DataAppealService._record_terminal_group_operation_error(appeal, full_operation_ids - previously_successful_group_ids, terminal_issue))
                continue
            operation_ids = full_operation_ids - previously_successful_group_ids
            if previously_successful_group_ids:
                if not (is_shared_destructive_target or is_item_quantity_merge):
                    error = '原子组上一轮仅部分写入成功，为避免重复增量写入已整组停止'
                    operation_errors.update({operation_id: error for operation_id in full_operation_ids})
                    continue
                successful_ids.update(previously_successful_group_ids)
            execution_patch_operation_ids = full_operation_ids if is_item_quantity_merge else operation_ids
            group_patch = filter_data_appeal_patch_by_operation_ids(patch, source='primary', allowed_operation_ids=execution_patch_operation_ids)
            group_patch, group_patch_error = AdventurerS1DataAppealService._normalize_shared_destructive_target_group_patch(group_patch)
            if group_patch_error or group_patch is None:
                error = group_patch_error or '同一合并目标无法形成原子执行补丁'
                operation_errors.update({operation_id: error for operation_id in operation_ids})
                continue
            normalized_group_operations = {operation['operation_id']: operation for operation in iter_data_appeal_patch_operations(group_patch, source='primary', snapshot_before=appeal.snapshot_before)}
            already_satisfied = {operation_id: AdventurerS1DataAppealService._appeal_operation_already_satisfied(appeal, normalized_group_operations.get(operation_id, operation_by_id[operation_id]), normalized_group_operations.get(operation_id, operation_by_id[operation_id]).get('_payload'), all_player_records or {}) for operation_id in operation_ids}
            terminal_issues = {operation_id: issue for operation_id in operation_ids for issue in [AdventurerS1DataAppealService._frozen_asset_update_terminal_error(appeal, operation_by_id.get(operation_id) or {}, all_player_records)] if issue is not None}
            if terminal_issues:
                representative = next(iter(terminal_issues.values()))
                group_terminal_issues = {operation_id: terminal_issues.get(operation_id) or {'code': representative['code'], 'detail': '同一原子组包含已发生后续流转的冻结物品，整组不能再按旧记录执行'} for operation_id in operation_ids}
                AdventurerS1DataAppealService._record_terminal_operation_errors(appeal, group_terminal_issues)
                operation_errors.update({operation_id: str(issue.get('detail') or '') for operation_id, issue in group_terminal_issues.items()})
                continue
            if all(already_satisfied.values()):
                successful_ids.update(operation_ids)
                continue
            if any(already_satisfied.values()):
                satisfied_ids = {operation_id for operation_id, satisfied in already_satisfied.items() if satisfied}
                satisfied_only_target_updates = is_shared_destructive_target and all(((operation_by_id.get(operation_id) or {}).get('action') == 'update' and (operation_by_id.get(operation_id) or {}).get('section') == ('traits' if group_id[2] == 'trait' else 'skills') and (_normalize_appeal_patch_integer((operation_by_id.get(operation_id) or {}).get('target_id'), minimum=1) == int(group_id[3])) for operation_id in satisfied_ids))
                if not (is_item_quantity_merge or satisfied_only_target_updates):
                    error = '原子组当前仅部分已满足，为避免重复写入已整组停止'
                    operation_errors.update({operation_id: error for operation_id in operation_ids})
                    continue
                newly_satisfied_ids = satisfied_ids
                successful_ids.update(newly_satisfied_ids)
                operation_ids -= newly_satisfied_ids
                if not operation_ids:
                    continue
            execution_snapshot = appeal.snapshot_before
            preserve_unchanged_update_ids: dict[str, set[int]] = {}
            if previously_successful_group_ids and is_shared_destructive_target:
                execution_snapshot, snapshot_error = AdventurerS1DataAppealService._shared_destructive_target_retry_snapshot(appeal, group_id=group_id, group_patch=group_patch, previously_successful_ids=previously_successful_group_ids, operation_by_id=operation_by_id, current_records=all_player_records or {})
                if snapshot_error or execution_snapshot is None:
                    error = snapshot_error or '同一合并目标无法安全续写'
                    operation_errors.update({operation_id: error for operation_id in operation_ids})
                    continue
            proven_group_ids = full_operation_ids & successful_ids
            if proven_group_ids and is_item_quantity_merge:
                preserve_unchanged_update_ids, retry_error = AdventurerS1DataAppealService._item_quantity_merge_retry_context(appeal, group_id=group_id, operation_ids=full_operation_ids, proven_operation_ids=proven_group_ids, operation_by_id=operation_by_id, current_records=all_player_records or {})
                if retry_error or preserve_unchanged_update_ids is None:
                    error = retry_error or '物品数量合并无法安全续跑'
                    operation_errors.update({operation_id: error for operation_id in operation_ids})
                    continue
            group_patch, group_unlandable = AdventurerS1DataAppealService._strip_unlandable_create_items(group_patch, appeal=appeal)
            if group_unlandable:
                unlandable_items.extend(group_unlandable)
                names = '、'.join((str(row.get('name') or '').strip() for row in group_unlandable if str(row.get('name') or '').strip()))
                error = f"拟补发物品「{names or '未命名物品'}」缺少可展示描述"
                operation_errors.update({operation_id: error for operation_id in operation_ids})
                continue
            savepoint = db_manager.begin_nested()
            try:
                record_patch = AdventurerS1DataAppealService._record_patch_only(group_patch)
                if AdventurerS1DataAppealService._patch_has_operations(record_patch):
                    execution_status: dict = {}
                    _, error = AdventurerS1DataAppealService._apply_patch(appeal.adventurer_id, record_patch, execution_snapshot, appeal_id=appeal.id, all_player_records=all_player_records, allow_quantity_increase=True, preserve_unchanged_update_ids=preserve_unchanged_update_ids or None, execution_status_out=execution_status)
                    terminal_issue = execution_status.get('terminal_operation_error')
                    if isinstance(terminal_issue, dict):
                        savepoint.rollback()
                        operation_errors.update(AdventurerS1DataAppealService._record_terminal_group_operation_error(appeal, operation_ids, terminal_issue))
                        continue
                    if error:
                        savepoint.rollback()
                        operation_errors.update({operation_id: str(error) for operation_id in operation_ids})
                        continue
                if group_patch.get('team_pool'):
                    _, error = AdventurerS1DataAppealService._apply_team_pool_distribution(appeal, group_patch['team_pool'])
                    if error:
                        savepoint.rollback()
                        operation_errors.update({operation_id: str(error) for operation_id in operation_ids})
                        continue
                if group_patch.get('reclaim_items'):
                    execution_status = {}
                    _, error = AdventurerS1DataAppealService._apply_reclaim_items(appeal, group_patch['reclaim_items'], execution_status_out=execution_status)
                    terminal_issue = execution_status.get('terminal_operation_error')
                    if isinstance(terminal_issue, dict):
                        savepoint.rollback()
                        operation_errors.update(AdventurerS1DataAppealService._record_terminal_group_operation_error(appeal, operation_ids, terminal_issue))
                        continue
                    if error:
                        savepoint.rollback()
                        operation_errors.update({operation_id: str(error) for operation_id in operation_ids})
                        continue
                savepoint.commit()
                successful_ids.update(operation_ids)
            except Exception as exc:
                savepoint.rollback()
                error = f'操作组执行异常：{type(exc).__name__}'
                operation_errors.update({operation_id: error for operation_id in operation_ids})
                logger.exception('数据申诉 v2 独立操作组执行异常 appeal=%s operations=%s', appeal.id, sorted(operation_ids))
        return (successful_ids, operation_errors, unlandable_items)

    @staticmethod
    def _apply_operation_contract_review_groups(appeal: AdventurerS1DataAppeal, patch: dict, verdicts: list[dict], *, all_player_records: dict | None) -> tuple[set[str], dict[str, str], list[dict]]:
        """Execute normalized reviewer-authored writes without coupling siblings.

        The reviewer names supplement/override/clear operations before backend
        normalization.  Normalization may merge those sources into one physical
        row, so this method maps each final row back to its authorized operation
        IDs and executes only the smallest inseparable group.
        """
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions, filter_data_appeal_patch_by_operation_ids, iter_data_appeal_patch_operations
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        previously_successful_ids = AdventurerS1DataAppealService._successful_operation_receipt_ids(appeal) if getattr(appeal, 'review_ai_raw_response', None) else set()
        authored_rows = review_raw.get('_backend_authorized_review_operations')
        authored_rows = authored_rows if isinstance(authored_rows, list) else []
        decisions = effective_data_appeal_operation_decisions(verdicts)
        authored_by_id = {str(row.get('operation_id') or '').strip(): row for row in authored_rows if isinstance(row, dict) and decisions.get(str(row.get('operation_id') or '').strip()) == 'allow'}
        if not authored_by_id:
            return (set(), {}, [])
        verdict_by_id = {str(row.get('operation_id') or '').strip(): row for row in verdicts if isinstance(row, dict)}
        normalized_operations = iter_data_appeal_patch_operations(patch, source='supplement', snapshot_before=appeal.snapshot_before)
        normalized_by_id = {operation['operation_id']: operation for operation in normalized_operations}
        execution_record_maps: dict[str, dict[int, dict]] = {section: {} for section in ('traits', 'equipment', 'warehouse', 'skills')}
        for record_source in (appeal.snapshot_before, all_player_records):
            if not isinstance(record_source, dict):
                continue
            for record_section, record_map in execution_record_maps.items():
                rows = record_source.get(record_section)
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    record_id = _normalize_appeal_patch_integer(row.get('id'), minimum=1)
                    if record_id is None:
                        continue
                    merged = dict(record_map.get(record_id) or {})
                    merged.update(row)
                    record_map[record_id] = merged
        claimed_array_ids: set[str] = set()
        mapped_authored_ids: set[str] = set()
        execution_operations: dict[str, list[dict]] = {}
        grouped: dict[str, dict[str, set[str]]] = {}
        operation_errors: dict[str, str] = {}

        def _record_action_matches(normalized_action: str, authored_action: str) -> bool:
            if authored_action == 'clear':
                return normalized_action == 'update'
            return normalized_action == authored_action
        array_field_aliases = {'custom_desc': 'description', 'item_name': 'name'}
        array_required_fields = {'create_items': {'name', 'destination', 'rarity', 'quantity'}, 'create_traits': {'name', 'description'}, 'create_skills': {'name', 'description', 'equipped', 'recovery_minutes'}, 'reclaim_items': {'name', 'from_adventurer_id', 'quantity', 'destination'}, 'currency_adjustments': {'currency_code', 'amount'}, 'team_pool': {'total_amount'}, 'unassigned_items': {'source_event_index', 'evidence_event_indexes', 'name', 'item'}}
        array_defaults = {'create_items': {'destination': 'warehouse', 'rarity': 'common', 'quantity': 1, 'type': '', 'description': '', 'uses_left': None, 'evolution_stage': None, 'recovery_minutes': 0, 'reason': ''}, 'create_skills': {'description': '', 'equipped': True, 'recovery_minutes': 0, 'reason': ''}, 'reclaim_items': {'quantity': 1, 'destination': 'warehouse', 'reason': ''}, 'currency_adjustments': {'reason': ''}, 'team_pool': {'currency_code': 'gold', 'reason': ''}}
        missing_array_value = object()

        def _array_payload_value(section: str, payload: dict, field: str):
            if field == 'description':
                if 'description' in payload:
                    value = payload.get('description')
                elif 'custom_desc' in payload:
                    value = payload.get('custom_desc')
                else:
                    value = array_defaults.get(section, {}).get(field, missing_array_value)
            elif field == 'name' and section == 'reclaim_items':
                if 'name' in payload:
                    value = payload.get('name')
                elif 'item_name' in payload:
                    value = payload.get('item_name')
                else:
                    value = missing_array_value
            elif field in payload:
                value = payload.get(field)
            else:
                value = array_defaults.get(section, {}).get(field, missing_array_value)
            if value is missing_array_value:
                return value
            if field == 'rarity' and isinstance(value, str):
                rarity = value.strip().lower()
                return 'rare' if rarity == 'uncommon' else rarity
            if field in {'destination', 'currency_code', 'ownership_source', 'narrative_claim_kind', 'recovery_policy'} and isinstance(value, str):
                return value.strip().lower()
            if field in {'quantity', 'from_adventurer_id', 'owner_adventurer_id', 'uses_left', 'evolution_stage', 'recovery_minutes', 'recovery_progress', 'recovery_required', 'recovery_hours'} and value is not None:
                normalized_int = _normalize_appeal_patch_integer(value)
                return value if normalized_int is None else normalized_int
            if field in {'amount', 'total_amount'}:
                normalized_number = _normalize_appeal_patch_number(value)
                return value if normalized_number is None else round(normalized_number, 2)
            if field in {'equipped', 'is_archived'}:
                normalized_bool = _normalize_appeal_patch_boolean(value)
                return value if normalized_bool is None else normalized_bool
            if field in {'evidence_event_indexes', 'recovery_evidence_event_indexes'} and isinstance(value, list):
                return tuple((_normalize_appeal_patch_integer(entry) for entry in value))
            if isinstance(value, str):
                return value.strip()
            return value

        def _array_candidate_score(normalized: dict, authored: dict) -> int | None:
            if normalized.get('section') != authored.get('section'):
                return None
            normalized_payload = normalized.get('_payload') or {}
            authored_payload = authored.get('payload') or {}
            section = str(normalized.get('section') or '')
            normalized_name = str(normalized.get('target_name') or '').strip()
            authored_name = str(authored.get('target_name') or '').strip()
            if section in ('create_items', 'create_traits', 'create_skills', 'reclaim_items'):
                if normalized_name and authored_name and (normalized_name != authored_name):
                    return None
            if section not in array_required_fields:
                return None
            authored_fields = {array_field_aliases.get(str(field), str(field)) for field in authored_payload}
            compared_fields = array_required_fields[section] | authored_fields
            if any((_array_payload_value(section, normalized_payload, field) != _array_payload_value(section, authored_payload, field) for field in compared_fields)):
                return None
            return 100

        def _folded_create_matches(normalized: dict, authored: dict) -> bool:
            """Bind create_* operations folded by normalization to stable rows."""
            normalized_section = str(normalized.get('section') or '')
            authored_section = str(authored.get('section') or '')
            expected_authored_section = {'equipment': 'create_items', 'warehouse': 'create_items', 'traits': 'create_traits', 'skills': 'create_skills'}.get(normalized_section)
            if expected_authored_section != authored_section or normalized.get('action') != 'update':
                return False
            record_id = _normalize_appeal_patch_integer(normalized.get('target_id'), minimum=1)
            if record_id is None:
                return False
            frozen = execution_record_maps.get(normalized_section, {}).get(record_id) or {}
            frozen_name = str(frozen.get('name') or '').strip()
            authored_payload = authored.get('payload') or {}
            authored_name = str(authored_payload.get('name') or authored.get('target_name') or '').strip()
            if not frozen_name or frozen_name != authored_name:
                return False
            normalized_payload = normalized.get('_payload') or {}
            if authored_section == 'create_items':
                grant_quantity = _normalize_appeal_patch_integer(authored_payload.get('quantity', 1), minimum=1)
                target_quantity = _normalize_appeal_patch_integer(normalized_payload.get('quantity'), minimum=0)
                if grant_quantity is None or target_quantity is None:
                    return False
                frozen_quantity = AdventurerS1DataAppealService._snapshot_item_quantity(frozen.get('quantity'))
                if target_quantity != frozen_quantity + grant_quantity:
                    return False
                raw_recovery = _normalize_appeal_patch_integer(authored_payload.get('recovery_minutes', 0), minimum=0)
                normalized_recovery = _normalize_appeal_patch_integer(normalized_payload.get('recovery_minutes', 0), minimum=0)
                return raw_recovery is not None and normalized_recovery == raw_recovery
            persisted_fields = {'description', 'recovery_policy', 'recovery_stage', 'recovery_progress', 'recovery_required', 'recovery_hours', 'recovered_desc'} if authored_section == 'create_traits' else {'description', 'equipped', 'recovery_minutes'}
            fields_to_compare = {field for field in persisted_fields if field in authored_payload}
            if authored_section == 'create_traits':
                fields_to_compare.add('description')
            return bool(fields_to_compare) and all((_array_payload_value(authored_section, normalized_payload, field) == _array_payload_value(authored_section, authored_payload, field) for field in fields_to_compare))
        for normalized in normalized_operations:
            normalized_id = normalized['operation_id']
            section = str(normalized.get('section') or '')
            matches: set[str] = set()
            if section in ('traits', 'equipment', 'warehouse', 'skills'):
                for authored_id, authored in authored_by_id.items():
                    if authored.get('section') == section and authored.get('target_id') == normalized.get('target_id') and _record_action_matches(str(normalized.get('action') or ''), str(authored.get('action') or '')):
                        matches.add(authored_id)
                folded_create_matches = {authored_id for authored_id, authored in authored_by_id.items() if authored_id not in claimed_array_ids and _folded_create_matches(normalized, authored)}
                if len(folded_create_matches) > 1:
                    ambiguous_ids = matches | folded_create_matches
                    error = '规范化后的折叠写入无法唯一对应二审补救操作'
                    operation_errors.update({operation_id: error for operation_id in ambiguous_ids})
                    mapped_authored_ids.update(ambiguous_ids)
                    claimed_array_ids.update(folded_create_matches)
                    continue
                if folded_create_matches:
                    folded_id = next(iter(folded_create_matches))
                    matches.add(folded_id)
                    claimed_array_ids.add(folded_id)
            elif section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
                for authored_id, authored in authored_by_id.items():
                    if authored.get('section') == section and authored.get('target_id') == normalized.get('target_id'):
                        matches.add(authored_id)
            else:
                ranked = sorted(((score, authored_id) for authored_id, authored in authored_by_id.items() if authored_id not in claimed_array_ids for score in [_array_candidate_score(normalized, authored)] if score is not None), key=lambda pair: (-pair[0], pair[1]))
                if ranked:
                    top_score = ranked[0][0]
                    top_ids = {authored_id for score, authored_id in ranked if score == top_score}
                    if len(top_ids) > 1:
                        error = '规范化后的数组写入无法唯一对应二审补救操作'
                        operation_errors.update({operation_id: error for operation_id in top_ids})
                        mapped_authored_ids.update(top_ids)
                        claimed_array_ids.update(top_ids)
                        continue
                    selected_id = next(iter(top_ids))
                    matches.add(selected_id)
                    claimed_array_ids.add(selected_id)
            if not matches:
                continue
            mapped_authored_ids.update(matches)
            projected_normalized = {**{key: copy.deepcopy(value) for key, value in normalized.items() if key != '_payload'}, 'payload': copy.deepcopy(normalized.get('_payload') or {})}
            for authored_id in matches:
                execution_operations.setdefault(authored_id, []).append(projected_normalized)
            atomic_groups = {str((verdict_by_id.get(authored_id) or {}).get('atomic_group_id') or '').strip() for authored_id in matches if str((verdict_by_id.get(authored_id) or {}).get('atomic_group_id') or '').strip()}
            if len(atomic_groups) > 1:
                error = '规范化后的同一写入对应多个冲突的原子组'
                operation_errors.update({authored_id: error for authored_id in matches})
                continue
            group_id = next(iter(atomic_groups)) if atomic_groups else 'authored:' + '|'.join(sorted(matches))
            group = grouped.setdefault(group_id, {'normalized_ids': set(), 'authored_ids': set()})
            group['normalized_ids'].add(normalized_id)
            group['authored_ids'].update(matches)
        review_raw['_backend_execution_operations'] = execution_operations
        appeal.review_ai_raw_response = json_dumps_safe(review_raw)
        missing_authored_ids = set(authored_by_id).difference(mapped_authored_ids).difference(previously_successful_ids)
        operation_errors.update({operation_id: '补救操作规范化后没有可对应的最终写入' for operation_id in missing_authored_ids})
        atomic_members: dict[str, set[str]] = {}
        for operation_id in authored_by_id:
            atomic_group_id = str((verdict_by_id.get(operation_id) or {}).get('atomic_group_id') or '').strip()
            if atomic_group_id:
                atomic_members.setdefault(atomic_group_id, set()).add(operation_id)
        for members in atomic_members.values():
            failed_member = next((operation_id for operation_id in members if operation_id in operation_errors), None)
            if failed_member is None:
                continue
            group_error = operation_errors[failed_member]
            operation_errors.update({operation_id: group_error for operation_id in members})
        successful_ids: set[str] = set()
        unlandable_items: list[dict] = []
        for group in grouped.values():
            authored_ids = group['authored_ids']
            if any((operation_id in operation_errors for operation_id in authored_ids)):
                continue
            previously_successful_group_ids = authored_ids & previously_successful_ids
            if previously_successful_group_ids == authored_ids:
                successful_ids.update(authored_ids)
                continue
            terminal_errors = AdventurerS1DataAppealService._terminal_operation_errors(appeal) if getattr(appeal, 'review_ai_raw_response', None) else {}
            terminal_issue = next((terminal_errors[oid] for oid in sorted(authored_ids) if oid in terminal_errors and oid not in previously_successful_group_ids), None)
            if terminal_issue:
                successful_ids.update(previously_successful_group_ids)
                operation_errors.update(AdventurerS1DataAppealService._record_terminal_group_operation_error(appeal, authored_ids - previously_successful_group_ids, terminal_issue))
                continue
            if previously_successful_group_ids:
                error = '二审原子组上一轮仅部分写入成功，为避免重复增量写入已整组停止'
                operation_errors.update({operation_id: error for operation_id in authored_ids})
                continue
            terminal_issues = [issue for operation_id in group['normalized_ids'] for issue in [AdventurerS1DataAppealService._frozen_asset_update_terminal_error(appeal, normalized_by_id[operation_id], all_player_records)] if issue is not None]
            if terminal_issues:
                operation_errors.update(AdventurerS1DataAppealService._record_terminal_group_operation_error(appeal, authored_ids, terminal_issues[0]))
                continue
            already_satisfied = {operation_id: AdventurerS1DataAppealService._appeal_operation_already_satisfied(appeal, normalized_by_id[operation_id], normalized_by_id[operation_id].get('_payload'), all_player_records or {}) for operation_id in group['normalized_ids']}
            if all(already_satisfied.values()):
                successful_ids.update(authored_ids)
                continue
            if any(already_satisfied.values()):
                error = '二审原子组当前仅部分已满足，为避免重复写入已整组停止'
                operation_errors.update({operation_id: error for operation_id in authored_ids})
                continue
            group_patch = filter_data_appeal_patch_by_operation_ids(patch, source='supplement', allowed_operation_ids=group['normalized_ids'])
            group_patch, group_unlandable = AdventurerS1DataAppealService._strip_unlandable_create_items(group_patch, appeal=appeal)
            if group_unlandable:
                unlandable_items.extend(group_unlandable)
                names = '、'.join((str(row.get('name') or '').strip() for row in group_unlandable if str(row.get('name') or '').strip()))
                error = f"拟补发物品「{names or '未命名物品'}」缺少可展示描述"
                operation_errors.update({operation_id: error for operation_id in authored_ids})
                continue
            savepoint = db_manager.begin_nested()
            try:
                record_patch = AdventurerS1DataAppealService._record_patch_only(group_patch)
                if AdventurerS1DataAppealService._patch_has_operations(record_patch):
                    execution_status: dict = {}
                    _, error = AdventurerS1DataAppealService._apply_patch(appeal.adventurer_id, record_patch, appeal.snapshot_before, appeal_id=appeal.id, all_player_records=all_player_records, allow_quantity_increase=True, execution_status_out=execution_status)
                    terminal_issue = execution_status.get('terminal_operation_error')
                    if isinstance(terminal_issue, dict):
                        savepoint.rollback()
                        operation_errors.update(AdventurerS1DataAppealService._record_terminal_group_operation_error(appeal, authored_ids, terminal_issue))
                        continue
                    if error:
                        savepoint.rollback()
                        operation_errors.update({operation_id: str(error) for operation_id in authored_ids})
                        continue
                if group_patch.get('team_pool'):
                    _, error = AdventurerS1DataAppealService._apply_team_pool_distribution(appeal, group_patch['team_pool'])
                    if error:
                        savepoint.rollback()
                        operation_errors.update({operation_id: str(error) for operation_id in authored_ids})
                        continue
                if group_patch.get('reclaim_items'):
                    execution_status = {}
                    _, error = AdventurerS1DataAppealService._apply_reclaim_items(appeal, group_patch['reclaim_items'], execution_status_out=execution_status)
                    terminal_issue = execution_status.get('terminal_operation_error')
                    if isinstance(terminal_issue, dict):
                        savepoint.rollback()
                        operation_errors.update(AdventurerS1DataAppealService._record_terminal_group_operation_error(appeal, authored_ids, terminal_issue))
                        continue
                    if error:
                        savepoint.rollback()
                        operation_errors.update({operation_id: str(error) for operation_id in authored_ids})
                        continue
                savepoint.commit()
                successful_ids.update(authored_ids)
            except Exception as exc:
                savepoint.rollback()
                error = f'二审补救操作执行异常：{type(exc).__name__}'
                operation_errors.update({operation_id: error for operation_id in authored_ids})
                logger.exception('数据申诉 v2 二审独立操作组异常 appeal=%s operations=%s', appeal.id, sorted(authored_ids))
        return (successful_ids, operation_errors, unlandable_items)

    @staticmethod
    @with_transaction(auto_commit=True)
    def refund_rarity_review_guard_failure(appeal_id: int, admin_id: int, *, notify_player: bool=True) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if not AdventurerS1DataAppealService.can_refund_rarity_review_guard_failure(appeal):
            return (None, '该申诉不属于可返还的历史稀有度复核误杀单')
        _, quota_err = AdventurerS1DataAppealService._add_quota_bonus_no_commit(appeal.user_id, 1, admin_id, _APPEAL_QUOTA_POOL_OTHER)
        if quota_err:
            return (None, quota_err)
        appeal.status = 'cancelled'
        appeal.reviewed_by = admin_id
        appeal.reviewed_at = _portable_utc_now()
        appeal.reject_reason = '历史稀有度复核因系统护栏误判未执行，已返还 1 次全文申诉次数，可重新提交。'
        AdventurerS1DataAppealService._sync_fields_on_cancel(appeal, cancelled_by='admin')
        if notify_player:
            AdventurerS1DataAppealService._notify_player_rarity_review_refunded(appeal, auto_commit=False)
        state = AdventurerS1DataAppealService._compute_quota_state(appeal.user_id, _APPEAL_QUOTA_POOL_OTHER)
        logger.info('管理员 %s 返还稀有度复核误杀单 appeal=%s user=%s', admin_id, appeal.id, appeal.user_id)
        return ({'id': appeal.id, 'status': 'cancelled', 'user_id': appeal.user_id, 'refunded_quota': 1, 'quota': {'used_24h': state['used'], 'limit': state['limit'], 'unlimited': state.get('unlimited', False), 'remaining': state['remaining'], 'bonus': state['bonus']}}, None)

    @staticmethod
    def _summarize_rarity_review_guard_failure(appeal: AdventurerS1DataAppeal) -> dict | None:
        from models.user import User
        if not AdventurerS1DataAppealService.can_refund_rarity_review_guard_failure(appeal):
            return None
        user = db_manager.query(User).filter_by(id=appeal.user_id).first()
        adv = db_manager.query(AdventurerS1).filter_by(id=appeal.adventurer_id).first()
        raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
        patch = raw.get('patch') if isinstance(raw.get('patch'), dict) else {}
        rarity_updates = []
        snapshot_maps = {'equipment': {int(r['id']): r for r in (appeal.snapshot_before or {}).get('equipment') or [] if r.get('id') is not None}, 'warehouse': {int(r['id']): r for r in (appeal.snapshot_before or {}).get('warehouse') or [] if r.get('id') is not None}}
        for section in ('equipment', 'warehouse'):
            for upd in (patch.get(section) or {}).get('update') or []:
                if not isinstance(upd, dict) or 'rarity' not in upd:
                    continue
                try:
                    tid = int(upd.get('id'))
                except (TypeError, ValueError):
                    continue
                original = snapshot_maps.get(section, {}).get(tid) or {}
                rarity_updates.append({'type': section, 'id': tid, 'name': original.get('name') or f'{section} #{tid}', 'from': original.get('rarity') or 'common', 'to': upd.get('rarity')})
        return {'id': appeal.id, 'user_id': appeal.user_id, 'username': getattr(user, 'username', None), 'nickname': getattr(user, 'nickname', None), 'adventurer_id': appeal.adventurer_id, 'adventurer_name': getattr(adv, 'name', None) or '未知', 'created_at': to_utc_iso(appeal.created_at) if appeal.created_at else None, 'ai_summary': appeal.ai_summary, 'rarity_updates': rarity_updates, 'can_refund': True}

    @staticmethod
    def preview_rarity_review_guard_failures(limit: int=50) -> tuple:
        limit = max(1, min(int(limit or 50), 200))
        rows = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.appeal_category == 'rarity_review', AdventurerS1DataAppeal.status == 'rejected').order_by(AdventurerS1DataAppeal.id.asc()).limit(limit * 3).all()
        items = []
        for appeal in rows:
            item = AdventurerS1DataAppealService._summarize_rarity_review_guard_failure(appeal)
            if item:
                items.append(item)
            if len(items) >= limit:
                break
        return ({'items': items, 'summary': {'total_candidates': len(items), 'refundable_count': sum((1 for i in items if i.get('can_refund')))}}, None)

    @staticmethod
    def bulk_refund_rarity_review_guard_failures(admin_id: int, *, appeal_ids: list | None=None, limit: int=50, notify_player: bool=True) -> tuple:
        if appeal_ids is None:
            preview, _ = AdventurerS1DataAppealService.preview_rarity_review_guard_failures(limit=limit)
            appeal_ids = [i['id'] for i in preview.get('items') or []]
        refunded, skipped, errors = ([], [], [])
        for raw_id in appeal_ids:
            try:
                appeal_id = int(raw_id)
            except (TypeError, ValueError):
                errors.append({'id': raw_id, 'error': 'id 无效'})
                continue
            data, err = AdventurerS1DataAppealService.refund_rarity_review_guard_failure(appeal_id, admin_id, notify_player=notify_player)
            if err:
                skipped.append({'id': appeal_id, 'reason': err})
            else:
                refunded.append(data)
        return ({'refunded': refunded, 'skipped': skipped, 'errors': errors}, None)

    @staticmethod
    def _enrich_cancellation_display(appeal: AdventurerS1DataAppeal, data: dict, *, include_admin_actions: bool=False) -> dict:
        outcome = AdventurerS1DataAppealService._continuation_outcome(appeal)
        data['processing_outcome'] = outcome
        data['can_continue'] = bool(outcome) and (not (appeal.ai_claim_token or appeal.player_review_status == 'pending' or appeal.player_revert_request_status in ('processing', 'allowed') or (appeal.revert_offer_status == 'pending')))
        if outcome == 'execution_blocked':
            data['status_label'] = '部分待执行' if appeal.applied_at else '审核通过，待执行'
            data['outcome_hint'] = '部分修改因探险携带锁未执行，不代表申诉依据不成立。探险结束后可在本记录继续处理；已完成项目不会重复发放，有后续变化的目标须重新核查。'
            data['reject_reason_label'] = '未执行说明'
            return data
        exhausted_stage = AdventurerS1DataAppealService._appeal_auto_retry_exhausted_stage(appeal)
        exhausted_status_matches = (appeal.status or '') == 'rejected'
        if exhausted_status_matches and (appeal.ai_status or '') == 'failed' and exhausted_stage:
            data['status_label'] = '处理失败'
            data['outcome_hint'] = '一审与独立复核已经完成，但最终写入连续冲突；未完成项目没有新增修改，已完成项目以逐项落账回执为准。' if exhausted_stage == 'execution' else '系统已自动重试多次，但仍未形成完整审核结论；未完成阶段没有新增修改，已完成项目以逐项结果为准。'
            data['reject_reason_label'] = '失败说明'
            if include_admin_actions:
                data['can_retry_ai'] = True
            return data
        if (appeal.status or '') == 'processing':
            review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
            stage = AdventurerS1DataAppealService._appeal_resume_stage(appeal)
            if not stage:
                raw_stage = str(review_raw.get('pending_stage') or '').strip()
                if raw_stage in ('primary_ai', 'review_ai', 'execution'):
                    stage = 'review_ai' if raw_stage == 'execution' and (not AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal)) else raw_stage
                elif appeal.review_ai_status == 'done':
                    stage = 'execution' if AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal) else 'review_ai'
                elif appeal.ai_status == 'done':
                    stage = 'review_ai'
                else:
                    stage = 'primary_ai'
            if stage == 'execution':
                data['status_label'] = '落账处理中' if appeal.ai_status == 'processing' else '等待落账'
                data['outcome_hint'] = '一审与独立复核已经完成，当前只处理最终写入和审计回执。'
            elif stage == 'review_ai':
                data['status_label'] = '独立复核中' if appeal.ai_status == 'processing' else '等待独立复核'
                data['outcome_hint'] = '一审结果已保留，系统正在补全或重新核对独立复核成品。'
            else:
                data['status_label'] = 'AI 初审中' if appeal.ai_status == 'processing' else '等待 AI 初审'
                data['outcome_hint'] = '系统仍在生成或重试第一阶段核查结果。'
            return data
        if (appeal.status or '') != 'cancelled':
            return data
        display = AdventurerS1DataAppealService._build_cancellation_display(appeal)
        data['cancellation_kind'] = display['cancellation_kind']
        data['status_label'] = display['status_label']
        data['outcome_hint'] = display['outcome_hint']
        data['reject_reason_label'] = display['reject_reason_label']
        for key in ('ai_status', 'ai_summary', 'review_ai_status', 'review_ai_summary'):
            if key in display:
                data[key] = display[key]
        return data

    @staticmethod
    def _continuation_outcome(appeal: AdventurerS1DataAppeal) -> str | None:
        """Pure persisted-state projection; no model text or queries decide eligibility."""
        if appeal.status not in ('approved', 'rejected') or appeal.appeal_category in ('tavern_relief', 'revert_request'):
            return None
        if appeal.status == 'rejected' and appeal.ai_status == 'failed' and AdventurerS1DataAppealService._appeal_auto_retry_exhausted_stage(appeal):
            return 'technical_failure'
        errors = AdventurerS1DataAppealService._terminal_operation_errors(appeal)
        successful = AdventurerS1DataAppealService._successful_operation_receipt_ids(appeal)
        if any((issue.get('code') == 'departure_asset_locked' and oid not in successful for oid, issue in errors.items())):
            if AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal):
                return 'execution_blocked'
        return None

    @staticmethod
    def cancel_by_player(user_id: int, appeal_id: int) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, user_id=user_id).first()
        if not appeal:
            return (None, '申诉不存在')
        if appeal.status != 'processing':
            return (None, '仅 AI 处理中的申诉可取消')
        appeal.status = 'cancelled'
        appeal.reject_reason = '玩家已取消'
        AdventurerS1DataAppealService._sync_fields_on_cancel(appeal, cancelled_by='player')
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '取消失败')
        AdventurerS1DataAppealService._release_ai_task_lock(appeal.id)
        return ({'id': appeal.id, 'status': 'cancelled'}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def cancel_by_admin(appeal_id: int, admin_id: int, reason: str='') -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if appeal.status not in ('processing', 'awaiting_review'):
            return (None, f'当前状态为 {appeal.status}，无法取消')
        appeal.status = 'cancelled'
        appeal.reviewed_by = admin_id
        appeal.reviewed_at = _portable_utc_now()
        appeal.reject_reason = (reason or '').strip() or '管理员已取消'
        AdventurerS1DataAppealService._sync_fields_on_cancel(appeal, cancelled_by='admin')
        AdventurerS1DataAppealService._release_ai_task_lock(appeal.id)
        AdventurerS1DataAppealService._notify_player_cancelled(appeal, auto_commit=False)
        return ({'id': appeal.id, 'status': 'cancelled'}, None)

    @staticmethod
    def retry_ai(appeal_id: int, *, user_id: int | None=None) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().populate_existing().first()
        if not appeal or (user_id is not None and appeal.user_id != user_id):
            return (None, '申诉不存在')
        continuation = AdventurerS1DataAppealService._continuation_outcome(appeal)
        if user_id is not None:
            display = AdventurerS1DataAppealService._enrich_cancellation_display(appeal, {})
            if not display.get('can_continue'):
                return (None, '该申诉当前不能继续处理，请查看逐项结果')
            adventurer = db_manager.query(AdventurerS1).filter_by(id=appeal.adventurer_id, owner_id=user_id).with_for_update().populate_existing().first()
            if not adventurer or adventurer.status == 'dead':
                return (None, '当前角色无法继续此申诉')
            if adventurer.status == 'exploring':
                return (None, '请在探险结束后继续处理')
        terminal_technical_failure = appeal.status == 'rejected' and appeal.ai_status == 'failed' and bool(AdventurerS1DataAppealService._appeal_auto_retry_exhausted_stage(appeal))
        if appeal.status not in ('processing', 'awaiting_review') and (not terminal_technical_failure) and (continuation != 'execution_blocked'):
            return (None, f'当前状态为 {appeal.status}，无法重试 AI')
        from services.async_task_service import AsyncTaskService
        task_type = 'ai_s1_tavern_relief' if (appeal.appeal_category or '') == 'tavern_relief' else 'ai_s1_data_appeal'
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        if continuation == 'execution_blocked':
            errors = AdventurerS1DataAppealService._terminal_operation_errors(appeal)
            blocked = {oid: issue for oid, issue in errors.items() if issue.get('code') == 'departure_asset_locked'}
            review_raw['_backend_continued_departure_errors'] = blocked
            review_raw[_DATA_APPEAL_TERMINAL_OPERATION_ERRORS_KEY] = {oid: issue for oid, issue in errors.items() if oid not in blocked}
            review_raw['pending_stage'] = 'execution'
            review_raw['resolution_state'] = 'pending'
        pending_stage = str(review_raw.get('pending_stage') or '').strip() if review_raw.get('resolution_state') in ('pending', 'failed') else ''
        has_pending_review_verdict = any((isinstance(row, dict) and str(row.get('decision') or '').strip().lower() == 'pending' for row in review_raw.get('request_verdicts') or []))
        resume_execution = pending_stage == 'execution' and (not has_pending_review_verdict) and ((appeal.appeal_category or '') != 'tavern_relief') and AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal)
        resume_review = not resume_execution and (pending_stage == 'review_ai' or pending_stage == 'execution') and ((appeal.appeal_category or '') != 'tavern_relief') and isinstance(appeal._parse_json_text(appeal.ai_raw_response), dict)
        resume_stage = 'execution' if resume_execution else 'review_ai' if resume_review else None
        if resume_execution and (user_id is not None or continuation == 'execution_blocked'):
            review_raw['_backend_player_continuation'] = True
        appeal.status = 'processing'
        appeal.ai_status = 'pending'
        if not resume_stage:
            appeal.ai_summary = None
            appeal.proposed_patch = None
            appeal.ai_raw_response = None
            appeal.review_ai_status = 'pending'
            appeal.review_ai_model = None
            appeal.review_ai_summary = None
            appeal.review_ai_raw_response = None
            appeal.review_supplement_patch = None
        else:
            review_raw['_backend_resume_stage'] = resume_stage
            review_raw.pop('_backend_auto_retry_after', None)
            review_raw.pop('_backend_auto_retry_exhausted', None)
            for retry_state_key in ('_backend_auto_retry_attempts', '_backend_zero_token_retry_attempts'):
                retry_attempts = review_raw.get(retry_state_key)
                if isinstance(retry_attempts, dict):
                    retry_attempts = copy.deepcopy(retry_attempts)
                    retry_attempts.pop(resume_stage, None)
                    if retry_attempts:
                        review_raw[retry_state_key] = retry_attempts
                    else:
                        review_raw.pop(retry_state_key, None)
            appeal.review_ai_raw_response = json_dumps_safe(review_raw)
            if resume_stage == 'review_ai':
                appeal.review_ai_status = 'pending'
                appeal.review_ai_model = None
                appeal.review_ai_summary = None
        appeal.reviewed_by = None
        appeal.reviewed_at = None
        appeal.reject_reason = None
        appeal.core_request_satisfied = None
        AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '状态更新失败')
        appeal_id_value = appeal.id
        adventurer_id_value = appeal.adventurer_id
        invoke_reason = 'player_continue' if user_id is not None else 'admin_retry'
        notification = SimpleNamespace(**{field: getattr(appeal, field) for field in ('id', 'user_id', 'status', 'ai_status', 'player_review_status', 'revert_offer_status')})
        db_manager.remove()
        AdventurerS1DataAppealService._emit_data_appeal_changed(notification)
        if user_id is None:
            AdventurerS1DataAppealService._release_ai_task_lock(appeal_id_value, task_type=task_type)
        if not AsyncTaskService.submit_task(task_type, {'appeal_id': appeal_id_value, 'invoke_reason': invoke_reason, **({'resume_stage': resume_stage} if resume_stage else {})}):
            return (None, '申诉已保存为待处理，任务暂未入队，系统会继续尝试调度')
        logger.info('数据申诉 AI 入队 appeal=%s invoke_reason=%s adventurer_id=%s', appeal_id_value, invoke_reason, adventurer_id_value)
        return ({'id': appeal_id_value, 'status': 'processing', 'ai_status': 'pending', **({'resume_stage': resume_stage} if resume_stage else {})}, None)
    _MANUAL_TAKEOVER_PROTOCOL = 's1_admin_manual_appeal_v1'
    _MANUAL_TAKEOVER_REVIEW_MODEL = 'admin_manual_takeover_v1'
    _MANUAL_TAKEOVER_CATEGORIES = frozenset({'data_fix', 'other'})
    _MANUAL_TAKEOVER_FINAL_CHECKLIST_STATUSES = frozenset({'done', 'partial', 'rejected'})

    @staticmethod
    def _manual_takeover_metadata(appeal: AdventurerS1DataAppeal) -> dict | None:
        raw = appeal._parse_json_text(appeal.ai_raw_response) or {}
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        metadata = raw.get('_manual_takeover')
        review_metadata = review_raw.get('_manual_takeover')
        if not isinstance(metadata, dict) or not isinstance(review_metadata, dict):
            return None
        protocol = AdventurerS1DataAppealService._MANUAL_TAKEOVER_PROTOCOL
        if metadata.get('protocol') != protocol or review_metadata.get('protocol') != protocol:
            return None
        status = str(metadata.get('status') or '').strip()
        if status not in ('pending', 'ready'):
            return None
        if str(review_metadata.get('status') or '').strip() != status:
            return None
        claimed_by = metadata.get('claimed_by')
        claimed_at = metadata.get('claimed_at')
        revision = metadata.get('revision')
        if not isinstance(claimed_by, int) or isinstance(claimed_by, bool) or (not isinstance(claimed_at, str)) or (not claimed_at.strip()) or (not isinstance(revision, int)) or isinstance(revision, bool) or (revision < 0) or (review_metadata.get('claimed_by') != claimed_by) or (review_metadata.get('claimed_at') != claimed_at):
            return None
        if status == 'ready':
            drafted_by = metadata.get('drafted_by')
            drafted_at = metadata.get('drafted_at')
            if not isinstance(drafted_by, int) or isinstance(drafted_by, bool) or (not isinstance(drafted_at, str)) or (not drafted_at.strip()) or (review_metadata.get('drafted_by') != drafted_by) or (review_metadata.get('drafted_at') != drafted_at) or (review_metadata.get('revision') != revision):
                return None
        expected_ai_status = 'processing' if status == 'pending' else 'done'
        expected_review_status = 'manual_pending' if status == 'pending' else 'manual_ready'
        if appeal.ai_status != expected_ai_status or appeal.review_ai_status != expected_review_status or appeal.review_ai_model != AdventurerS1DataAppealService._MANUAL_TAKEOVER_REVIEW_MODEL:
            return None
        return metadata

    @staticmethod
    def _manual_takeover_projection(appeal: AdventurerS1DataAppeal) -> dict | None:
        metadata = AdventurerS1DataAppealService._manual_takeover_metadata(appeal)
        if not metadata:
            return None
        revision = metadata['revision']
        projected = {'status': str(metadata.get('status') or '').strip() or 'pending', 'claimed_by': metadata.get('claimed_by'), 'claimed_at': metadata.get('claimed_at'), 'drafted_by': metadata.get('drafted_by'), 'drafted_at': metadata.get('drafted_at'), 'revision': revision}
        return projected

    @staticmethod
    def _manual_takeover_allowed(appeal: AdventurerS1DataAppeal) -> bool:
        return appeal.status in ('processing', 'awaiting_review') and (not AdventurerS1DataAppealService._appeal_auto_retry_exhausted_stage(appeal)) and ((appeal.appeal_category or 'other') in AdventurerS1DataAppealService._MANUAL_TAKEOVER_CATEGORIES)

    @staticmethod
    def _normalize_manual_takeover_checklist(items) -> tuple[list[dict] | None, str | None]:
        if not isinstance(items, list):
            return (None, 'request_checklist 必须是数组')
        if not 1 <= len(items) <= 8:
            return (None, 'request_checklist 必须包含 1 到 8 条逐项结论')
        normalized: list[dict] = []
        seen_labels: set[str] = set()
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                return (None, f'request_checklist[{index}] 必须是对象')
            label_value = item.get('label')
            if not isinstance(label_value, str):
                return (None, f'request_checklist[{index}].label 必须是字符串')
            label = label_value.strip()
            if not label or len(label) > 120:
                return (None, f'request_checklist[{index}].label 长度必须在 1 到 120 字之间')
            dedupe_key = label.casefold()
            if dedupe_key in seen_labels:
                return (None, f'request_checklist[{index}].label 与前项重复')
            seen_labels.add(dedupe_key)
            status_value = item.get('status')
            if not isinstance(status_value, str):
                return (None, f'request_checklist[{index}].status 必须是字符串')
            status = status_value.strip().lower()
            if status not in AdventurerS1DataAppealService._MANUAL_TAKEOVER_FINAL_CHECKLIST_STATUSES:
                return (None, f'request_checklist[{index}].status 仅支持 done / partial / rejected')
            category_value = item.get('category', 'other')
            if not isinstance(category_value, str):
                return (None, f'request_checklist[{index}].category 必须是字符串')
            category = category_value.strip().lower() or 'other'
            if len(category) > 20:
                return (None, f'request_checklist[{index}].category 不能超过 20 字')
            detail_value = item.get('detail', '')
            if not isinstance(detail_value, str):
                return (None, f'request_checklist[{index}].detail 必须是字符串')
            detail = detail_value.strip()
            if len(detail) > 160:
                return (None, f'request_checklist[{index}].detail 不能超过 160 字')
            is_core_value = item.get('is_core', index == 0)
            if type(is_core_value) is not bool:
                return (None, f'request_checklist[{index}].is_core 必须是布尔值')
            row = {'request_id': f'request_{index + 1}', 'label': label, 'status': status, 'category': category, 'is_core': is_core_value}
            if detail:
                row['detail'] = detail
            normalized.append(row)
        if not any((row['is_core'] for row in normalized)):
            normalized[0]['is_core'] = True
        return (normalized, None)

    @staticmethod
    def claim_manual_takeover(appeal_id: int, admin_id: int) -> tuple:
        """把一条普通申诉切到管理员人工草拟；AI worker 后续写回会因状态复查而退出。"""
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().populate_existing().first()
        if not appeal:
            return (None, '申诉不存在')
        existing = AdventurerS1DataAppealService._manual_takeover_metadata(appeal)
        if existing and str(existing.get('status') or '') in ('pending', 'ready'):
            return AdventurerS1DataAppealService.get_admin_detail(appeal.id)
        if not AdventurerS1DataAppealService._manual_takeover_allowed(appeal):
            if (appeal.appeal_category or 'other') not in AdventurerS1DataAppealService._MANUAL_TAKEOVER_CATEGORIES:
                return (None, '人工接管仅支持普通数据修复与其他问题申诉')
            return (None, f'当前状态为 {appeal.status}，无法人工接管')
        now = _portable_utc_now()
        previous_ai_raw = appeal._parse_json_text(appeal.ai_raw_response)
        previous_review_raw = appeal._parse_json_text(appeal.review_ai_raw_response)
        metadata = {'protocol': AdventurerS1DataAppealService._MANUAL_TAKEOVER_PROTOCOL, 'status': 'pending', 'claimed_by': int(admin_id), 'claimed_at': now.isoformat(timespec='seconds') + 'Z', 'drafted_by': None, 'drafted_at': None, 'revision': 0, 'previous_ai_status': appeal.ai_status, 'previous_ai_summary': appeal.ai_summary, 'previous_ai_response': previous_ai_raw, 'previous_review_status': appeal.review_ai_status, 'previous_review_summary': appeal.review_ai_summary, 'previous_review_response': previous_review_raw}
        appeal.status = 'awaiting_review'
        appeal.ai_status = 'processing'
        appeal.ai_summary = '管理员人工复核处理中'
        appeal.proposed_patch = None
        appeal.review_supplement_patch = None
        appeal.ai_raw_response = json_dumps_safe({'summary': '管理员人工复核处理中', '_manual_takeover': metadata})
        appeal.review_ai_status = 'manual_pending'
        appeal.review_ai_model = AdventurerS1DataAppealService._MANUAL_TAKEOVER_REVIEW_MODEL
        appeal.review_ai_summary = '管理员已接管，等待人工方案'
        appeal.review_ai_raw_response = json_dumps_safe({'decision': 'pending', 'summary': '管理员已接管，等待人工方案', '_manual_takeover': {'protocol': AdventurerS1DataAppealService._MANUAL_TAKEOVER_PROTOCOL, 'status': 'pending', 'claimed_by': int(admin_id), 'claimed_at': metadata['claimed_at']}})
        appeal.core_request_satisfied = None
        appeal.reject_reason = None
        AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '人工接管状态保存失败')
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        logger.info('数据申诉人工接管 appeal=%s admin=%s', appeal.id, admin_id)
        return AdventurerS1DataAppealService.get_admin_detail(appeal.id)

    @staticmethod
    def save_manual_takeover_draft(appeal_id: int, admin_id: int, *, summary, core_request_satisfied, request_checklist, patch) -> tuple:
        """保存人工结构化提案并生成预览；真实写入仍由既有 approve 入口执行。"""
        if not isinstance(summary, str):
            return (None, 'summary 必须是字符串')
        normalized_summary = summary.strip()
        if not normalized_summary or len(normalized_summary) > 500:
            return (None, 'summary 长度必须在 1 到 500 字之间')
        if not isinstance(core_request_satisfied, bool):
            return (None, 'core_request_satisfied 必须是布尔值')
        normalized_checklist, checklist_err = AdventurerS1DataAppealService._normalize_manual_takeover_checklist(request_checklist)
        if checklist_err:
            return (None, checklist_err)
        if not isinstance(patch, dict):
            return (None, 'patch 必须是对象')
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().populate_existing().first()
        if not appeal:
            return (None, '申诉不存在')
        metadata = AdventurerS1DataAppealService._manual_takeover_metadata(appeal)
        if not metadata or str(metadata.get('status') or '') not in ('pending', 'ready') or appeal.status != 'awaiting_review':
            return (None, '请先人工接管这条申诉')
        if (appeal.appeal_category or 'other') not in AdventurerS1DataAppealService._MANUAL_TAKEOVER_CATEGORIES:
            return (None, '人工接管仅支持普通数据修复与其他问题申诉')
        identity_err = AdventurerS1DataAppealService._appeal_identity_error(appeal)
        if identity_err:
            return (None, identity_err)
        evidence = copy.deepcopy(appeal.expedition_evidence) if isinstance(appeal.expedition_evidence, dict) else None
        narrative_currency_cap = 0 if (appeal.appeal_category or 'other') == 'data_fix' else AdventurerS1DataAppealService._narrative_currency_cap_for_evidence(evidence)
        contract_status: dict = {}
        normalized_patch, patch_err = AdventurerS1DataAppealService.normalize_patch(patch, appeal.snapshot_before or {}, all_player_records=AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True), narrative_currency_cap=narrative_currency_cap, allow_quantity_increase=True, allow_rarity_increase=False, allow_all_player_record_ids=False, expedition_evidence=evidence, adventurer_id=appeal.adventurer_id, appeal_id=appeal.id, contract_status_out=contract_status)
        if patch_err:
            return (None, patch_err)
        if contract_status.get('contract_valid') is not True:
            return (None, '人工方案包含无效、越权或被后端规范化丢弃的操作，请修正后重试')
        if not AdventurerS1DataAppealService._patch_has_operations(normalized_patch):
            return (None, '人工方案没有可执行的数据操作；如需驳回请使用驳回入口')
        now = _portable_utc_now()
        metadata = copy.deepcopy(metadata)
        metadata.update({'status': 'ready', 'drafted_by': int(admin_id), 'drafted_at': now.isoformat(timespec='seconds') + 'Z', 'revision': int(metadata.get('revision') or 0) + 1})
        manual_raw = AdventurerS1DataAppealService.finalize_player_visible_response({'summary': normalized_summary, 'core_request_satisfied': core_request_satisfied, 'request_checklist': normalized_checklist, 'patch': normalized_patch, '_manual_takeover': metadata}, player_note=appeal.player_note, normalized_patch=normalized_patch, patch_raw=normalized_patch)
        player_reason = str(manual_raw.get('player_reply_summary') or manual_raw.get('player_reject_reason') or '').strip()
        appeal.proposed_patch = normalized_patch
        appeal.review_supplement_patch = None
        appeal.ai_status = 'done'
        appeal.ai_summary = normalized_summary
        appeal.ai_raw_response = json_dumps_safe(manual_raw)
        appeal.review_ai_status = 'manual_ready'
        appeal.review_ai_model = AdventurerS1DataAppealService._MANUAL_TAKEOVER_REVIEW_MODEL
        appeal.review_ai_summary = normalized_summary
        appeal.review_ai_raw_response = json_dumps_safe({'decision': 'allow', 'summary': normalized_summary, 'player_reason': player_reason, 'execution_note': player_reason, '_manual_takeover': {'protocol': AdventurerS1DataAppealService._MANUAL_TAKEOVER_PROTOCOL, 'status': 'ready', 'claimed_by': metadata.get('claimed_by'), 'claimed_at': metadata.get('claimed_at'), 'drafted_by': int(admin_id), 'drafted_at': metadata['drafted_at'], 'revision': metadata['revision']}})
        appeal.core_request_satisfied = core_request_satisfied
        appeal.reject_reason = None
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '人工方案保存失败')
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        logger.info('数据申诉人工方案已保存 appeal=%s admin=%s revision=%s', appeal.id, admin_id, metadata['revision'])
        return AdventurerS1DataAppealService.get_admin_detail(appeal.id)

    @staticmethod
    def _notify_player_cancelled(appeal: AdventurerS1DataAppeal, *, auto_commit: bool=True):
        from models.mailbox import Mailbox
        mail = Mailbox(user_id=appeal.user_id, title=f'数据申诉已取消（#{appeal.id}）', content=f'您的数据异常申诉（#{appeal.id}）已被取消。\n\n说明：{appeal.reject_reason}', sender_id=None, sender_name='深渊酒馆')
        db_manager.add(mail)
        if auto_commit:
            db_manager.commit()

    @staticmethod
    def _notify_player_rarity_review_refunded(appeal: AdventurerS1DataAppeal, *, auto_commit: bool=True):
        from models.mailbox import Mailbox
        mail = Mailbox(user_id=appeal.user_id, title=f'稀有度复核次数已返还（#{appeal.id}）', content=f'你的副本掉落稀有度复核申诉（#{appeal.id}）此前因系统护栏误判未能成功执行。\n\n管理员已关闭这条旧申诉，并返还 1 次全文申诉次数。你现在可以重新提交稀有度复核。', sender_id=None, sender_name='深渊酒馆', mail_type='s1_rarity_review_refund', reference_id=appeal.id)
        db_manager.add(mail)
        if auto_commit:
            db_manager.commit()

    @staticmethod
    def _notify_player_submitted(appeal: AdventurerS1DataAppeal):
        from models.mailbox import Mailbox
        mail = Mailbox(user_id=appeal.user_id, title='数据申诉已进入审核', content=f'您提交的数据异常申诉（#{appeal.id}）已完成 AI 分析，自动审核尚未完成，系统会继续处理。', sender_id=None, sender_name='深渊酒馆')
        db_manager.add(mail)
        db_manager.commit()

    @staticmethod
    def _admin_list_search_filter(search: str):
        keyword = (search or '').strip()
        if not keyword:
            return None
        from models.user import User
        from sqlalchemy import or_
        like = f'%{keyword}%'
        clauses = [AdventurerS1DataAppeal.player_note.ilike(like)]
        if keyword.isdigit():
            kid = int(keyword)
            clauses.extend([AdventurerS1DataAppeal.id == kid, AdventurerS1DataAppeal.adventurer_id == kid, AdventurerS1DataAppeal.user_id == kid])
        user_ids = [uid for uid, in db_manager.query(User.id).filter(or_(User.username.ilike(like), User.nickname.ilike(like))).limit(200).all()]
        if user_ids:
            clauses.append(AdventurerS1DataAppeal.user_id.in_(user_ids))
        adv_ids = [aid for aid, in db_manager.query(AdventurerS1.id).filter(AdventurerS1.name.ilike(like)).limit(200).all()]
        if adv_ids:
            clauses.append(AdventurerS1DataAppeal.adventurer_id.in_(adv_ids))
        return or_(*clauses)

    @staticmethod
    def list_admin(status: str=None, page: int=1, per_page: int=20, search: str=None) -> tuple:
        q = db_manager.query(AdventurerS1DataAppeal)
        if status:
            q = q.filter(AdventurerS1DataAppeal.status == status)
        search_filter = AdventurerS1DataAppealService._admin_list_search_filter(search)
        if search_filter is not None:
            q = q.filter(search_filter)
        q = q.order_by(AdventurerS1DataAppeal.created_at.desc())
        total = q.count()
        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or 20), 100))
        rows = q.offset((page - 1) * per_page).limit(per_page).all()
        user_ids = {r.user_id for r in rows}
        adv_ids = {r.adventurer_id for r in rows}
        from models.user import User
        users = db_manager.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []
        user_map = {u.id: u for u in users}
        advs = db_manager.query(AdventurerS1).filter(AdventurerS1.id.in_(adv_ids)).all() if adv_ids else []
        adv_name_map = {a.id: a.name for a in advs}
        items = []
        for r in rows:
            row_data = r.to_dict(is_admin=True, user_map=user_map, adv_name_map=adv_name_map)
            row_data['manual_takeover'] = AdventurerS1DataAppealService._manual_takeover_projection(r)
            items.append(AdventurerS1DataAppealService._enrich_cancellation_display(r, row_data, include_admin_actions=True))
        return ({'items': items, 'total': total, 'page': page, 'per_page': per_page}, None)

    @staticmethod
    def _cross_character_appeal_candidate_query():
        """仍有待阻止或待撤销内容的工单；快照身份在 Python 解压后精确过滤。"""
        from sqlalchemy import and_, exists, or_
        active_audit_exists = exists().where(and_(AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id, AdventurerS1DataAppealAuditLog.reverted.is_(False)))
        return db_manager.query(AdventurerS1DataAppeal).filter(or_(AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review')), active_audit_exists))

    @staticmethod
    def _appeal_snapshot_adventurer_ids(appeal: AdventurerS1DataAppeal, *, log_map: dict[int, AdventurerS1ExpeditionLog] | None=None) -> list[int]:
        """读取冻结快照身份；不根据账号世代、角色状态或玩家文本猜测。"""
        log_id = AdventurerS1DataAppealService._appeal_effective_expedition_log_id(appeal)
        log = log_map.get(log_id) if log_id and log_map is not None else None
        if log_id and log_map is None:
            log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=log_id).first()
        full_log = log.full_log if log and isinstance(log.full_log, dict) else {}
        ids = set()
        for row in full_log.get('audit_member_snapshot') or []:
            if not isinstance(row, dict):
                continue
            try:
                snapshot_id = int(row.get('adventurer_id'))
            except (TypeError, ValueError):
                continue
            if snapshot_id > 0:
                ids.add(snapshot_id)
        if ids:
            return sorted(ids)
        evidence = appeal.expedition_evidence if isinstance(appeal.expedition_evidence, dict) else {}
        member_update = evidence.get('member_update')
        if isinstance(member_update, dict):
            try:
                member_id = int(member_update.get('id'))
            except (TypeError, ValueError):
                member_id = 0
            if member_id > 0:
                return [member_id]
        return []

    @staticmethod
    def _appeal_snapshot_identity_error(appeal: AdventurerS1DataAppeal, *, log_map: dict[int, AdventurerS1ExpeditionLog] | None=None) -> str | None:
        snapshot_ids = AdventurerS1DataAppealService._appeal_snapshot_adventurer_ids(appeal, log_map=log_map)
        if snapshot_ids and int(appeal.adventurer_id) not in snapshot_ids:
            return '申诉接收角色与冻结快照角色不一致；旧角色快照不能写入新角色'
        return None

    @staticmethod
    def _is_cross_character_appeal_candidate(appeal_id: int) -> bool:
        """执行前按与预览相同的冻结快照身份复校验，避免过期预览误操作。"""
        appeal = AdventurerS1DataAppealService._cross_character_appeal_candidate_query().filter(AdventurerS1DataAppeal.id == int(appeal_id)).first()
        return bool(appeal and AdventurerS1DataAppealService._appeal_snapshot_identity_error(appeal))

    @staticmethod
    def preview_cross_character_appeals(limit: int=100) -> tuple:
        """管理员预览：新角色关联旧角色探险的存量申诉及可撤销内容。"""
        from sqlalchemy import func
        from models.user import User
        limit = max(1, min(int(limit or 100), 200))
        rows: list[AdventurerS1DataAppeal] = []
        selected_log_map: dict[int, AdventurerS1ExpeditionLog] = {}
        total = 0
        after_id = 0
        while True:
            batch = AdventurerS1DataAppealService._cross_character_appeal_candidate_query().filter(AdventurerS1DataAppeal.id > after_id).order_by(AdventurerS1DataAppeal.id.asc()).limit(500).all()
            if not batch:
                break
            after_id = int(batch[-1].id)
            batch_log_ids = {log_id for appeal in batch if (log_id := AdventurerS1DataAppealService._appeal_effective_expedition_log_id(appeal))}
            batch_logs = db_manager.query(AdventurerS1ExpeditionLog).filter(AdventurerS1ExpeditionLog.id.in_(batch_log_ids)).all() if batch_log_ids else []
            batch_log_map = {int(log.id): log for log in batch_logs}
            for appeal in batch:
                if not AdventurerS1DataAppealService._appeal_snapshot_identity_error(appeal, log_map=batch_log_map):
                    continue
                total += 1
                if len(rows) < limit:
                    rows.append(appeal)
                    log_id = AdventurerS1DataAppealService._appeal_effective_expedition_log_id(appeal)
                    if log_id and log_id in batch_log_map:
                        selected_log_map[log_id] = batch_log_map[log_id]
        if not rows:
            return ({'items': [], 'summary': {'total_candidates': total, 'shown': 0, 'pending_to_cancel': 0, 'applied_to_revert': 0}}, None)
        appeal_ids = [int(row.id) for row in rows]
        user_ids = {int(row.user_id) for row in rows}
        snapshot_ids_by_appeal = {int(row.id): AdventurerS1DataAppealService._appeal_snapshot_adventurer_ids(row, log_map=selected_log_map) for row in rows}
        adv_ids = {int(row.adventurer_id) for row in rows} | {snapshot_id for snapshot_ids in snapshot_ids_by_appeal.values() for snapshot_id in snapshot_ids}
        audit_counts = dict(db_manager.query(AdventurerS1DataAppealAuditLog.appeal_id, func.count(AdventurerS1DataAppealAuditLog.id)).filter(AdventurerS1DataAppealAuditLog.appeal_id.in_(appeal_ids), AdventurerS1DataAppealAuditLog.reverted.is_(False)).group_by(AdventurerS1DataAppealAuditLog.appeal_id).all())
        all_adv_ids = adv_ids
        adv_rows = db_manager.query(AdventurerS1.id, AdventurerS1.name, AdventurerS1.status, AdventurerS1.owner_id).filter(AdventurerS1.id.in_(all_adv_ids)).all() if all_adv_ids else []
        adv_map = {int(adv_id): {'name': name, 'status': status, 'owner_id': owner_id} for adv_id, name, status, owner_id in adv_rows}
        users = db_manager.query(User).filter(User.id.in_(user_ids)).all() if user_ids else []
        user_map = {int(user.id): user for user in users}
        items = []
        for appeal in rows:
            active_audits = int(audit_counts.get(appeal.id, 0) or 0)
            log_id = int(appeal.expedition_log_id) if appeal.expedition_log_id else None
            appealed_adv = adv_map.get(int(appeal.adventurer_id), {})
            member_ids = snapshot_ids_by_appeal[int(appeal.id)]
            user = user_map.get(int(appeal.user_id))
            items.append({'id': int(appeal.id), 'user_id': int(appeal.user_id), 'username': user.username if user else '未知', 'nickname': user.nickname if user else None, 'adventurer_id': int(appeal.adventurer_id), 'adventurer_name': appealed_adv.get('name') or '未知', 'adventurer_status': appealed_adv.get('status'), 'expedition_log_id': log_id, 'expedition_member_ids': member_ids, 'expedition_members': [{'adventurer_id': member_id, 'name': (adv_map.get(member_id) or {}).get('name') or '未知', 'status': (adv_map.get(member_id) or {}).get('status')} for member_id in member_ids], 'status': appeal.status, 'player_note': appeal.player_note, 'active_audit_count': active_audits, 'action': 'revert' if active_audits else 'cancel', 'created_at': to_utc_iso(appeal.created_at) if appeal.created_at else None})
        return ({'items': items, 'summary': {'total_candidates': total, 'shown': len(items), 'pending_to_cancel': sum((1 for item in items if item['action'] == 'cancel')), 'applied_to_revert': sum((1 for item in items if item['action'] == 'revert'))}}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def _cancel_cross_character_appeal(appeal_id: int, admin_id: int) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(appeal_id)).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if not AdventurerS1DataAppealService._is_cross_character_appeal_candidate(appeal.id):
            return (None, '该申诉的接收角色与冻结快照身份一致，不属于跨角色异常')
        if appeal.status not in ('processing', 'awaiting_review'):
            return (None, f'当前状态为 {appeal.status}，无法按未落库异常取消')
        if AdventurerS1DataAppealService._appeal_has_revertible_audits(appeal.id):
            return (None, '该申诉已有落库变更，必须走审计台账还原')
        now = _portable_utc_now()
        appeal.status = 'cancelled'
        appeal.ai_status = 'failed'
        appeal.ai_summary = '管理员已阻止接收角色与冻结快照不一致的申诉，未继续执行'
        appeal.review_ai_status = 'failed'
        appeal.review_ai_summary = '后端身份门禁已阻止跨角色世代申诉'
        appeal.reviewed_by = admin_id
        appeal.reviewed_at = now
        appeal.reject_reason = '管理员已撤销接收角色与冻结快照不一致的申诉'
        AdventurerS1DataAppealService._notify_player_cancelled(appeal, auto_commit=False)
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        return ({'id': appeal.id, 'status': appeal.status, 'action': 'cancelled'}, None)

    @staticmethod
    def _revert_exploring_adventurer_ids(appeal_id: int, *, lock_rows: bool=False) -> list[int]:
        """还原涉及的任一角色正在探险时必须延后，避免被冻结结算写回覆盖。"""
        from db_repository.s1_asset_transfer_repository import appeal_affected_member_ids
        affected_ids = appeal_affected_member_ids(int(appeal_id))
        if not affected_ids:
            return []
        query = db_manager.query(AdventurerS1).filter(AdventurerS1.id.in_(affected_ids)).order_by(AdventurerS1.id.asc())
        if lock_rows:
            query = query.with_for_update()
        return sorted((int(adv.id) for adv in query.all() if (adv.status or '') == 'exploring'))

    @staticmethod
    def pending_cross_character_revert_adventurer_ids(adventurer_ids: list[int] | set[int] | tuple[int, ...]) -> list[int]:
        """返回仍有跨角色申诉待撤销审计的角色，供发车硬门禁批量检查。"""
        normalized_ids = sorted({int(adventurer_id) for adventurer_id in adventurer_ids or () if int(adventurer_id) > 0})
        if not normalized_ids:
            return []
        from db_repository.s1_asset_transfer_repository import pending_revert_member_ids
        return pending_revert_member_ids(normalized_ids)

    @staticmethod
    @with_transaction(auto_commit=True)
    def _queue_cross_character_revert(appeal_id: int, admin_id: int) -> tuple:
        """将正在探险的跨角色违规申诉登记为出本后自动撤销。"""
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(appeal_id)).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if not AdventurerS1DataAppealService._is_cross_character_appeal_candidate(appeal.id):
            return (None, '该申诉的接收角色与冻结快照身份一致，不属于跨角色异常')
        if not AdventurerS1DataAppealService._appeal_has_revertible_audits(appeal.id):
            return (None, '没有未撤销的落库内容')
        exploring_ids = AdventurerS1DataAppealService._revert_exploring_adventurer_ids(appeal.id, lock_rows=True)
        if not exploring_ids:
            return ({'id': appeal.id, 'queued': False, 'exploring_adventurer_ids': []}, None)
        now = _portable_utc_now()
        appeal.cross_character_revert_status = 'pending'
        appeal.cross_character_revert_requested_at = appeal.cross_character_revert_requested_at or now
        appeal.cross_character_revert_requested_by = admin_id
        appeal.cross_character_revert_last_error = None
        return ({'id': appeal.id, 'status': appeal.status, 'action': 'deferred_until_expedition_exit', 'queued': True, 'exploring_adventurer_ids': exploring_ids}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def _record_pending_cross_character_revert_error(appeal_id: int, error: str) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(appeal_id)).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if (appeal.cross_character_revert_status or 'none') != 'pending':
            return ({'id': appeal.id, 'recorded': False}, None)
        appeal.cross_character_revert_last_error = str(error or '未知错误')[:500]
        return ({'id': appeal.id, 'recorded': True}, None)

    @staticmethod
    def execute_cross_character_appeal_reverts(admin_id: int, appeal_ids: list[int]) -> tuple:
        """逐单短事务撤销已落库内容或取消未落库工单；每单执行前重新校验身份。"""
        normalized_ids = []
        seen = set()
        for raw_id in appeal_ids or []:
            try:
                appeal_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if appeal_id > 0 and appeal_id not in seen:
                seen.add(appeal_id)
                normalized_ids.append(appeal_id)
        if not normalized_ids:
            return (None, '请先预览并选择要撤销的跨角色申诉')
        if len(normalized_ids) > 200:
            return (None, '单次最多处理 200 条申诉')
        reverted = []
        cancelled = []
        deferred = []
        skipped = []
        errors = []
        for appeal_id in normalized_ids:
            appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).first()
            if not appeal:
                skipped.append({'id': appeal_id, 'reason': '申诉不存在'})
                continue
            if not AdventurerS1DataAppealService._is_cross_character_appeal_candidate(appeal_id):
                skipped.append({'id': appeal_id, 'reason': '接收角色与冻结快照身份一致'})
                continue
            has_audits = AdventurerS1DataAppealService._appeal_has_revertible_audits(appeal_id)
            if has_audits:
                queued, queue_err = AdventurerS1DataAppealService._queue_cross_character_revert(appeal_id, admin_id)
                if queue_err:
                    errors.append({'id': appeal_id, 'error': queue_err})
                    continue
                if queued and queued.get('queued'):
                    deferred.append(queued)
                    continue
                data, err = AdventurerS1DataAppealService.revert_appeal(appeal_id, admin_id=admin_id)
                if err:
                    errors.append({'id': appeal_id, 'error': err})
                else:
                    reverted.append(data)
                continue
            if appeal.status in ('processing', 'awaiting_review'):
                data, err = AdventurerS1DataAppealService._cancel_cross_character_appeal(appeal_id, admin_id)
                if err:
                    errors.append({'id': appeal_id, 'error': err})
                else:
                    cancelled.append(data)
                continue
            skipped.append({'id': appeal_id, 'reason': '没有未撤销的落库内容'})
        return ({'reverted': reverted, 'cancelled': cancelled, 'deferred': deferred, 'skipped': skipped, 'errors': errors}, None)

    @staticmethod
    def process_pending_cross_character_reverts(limit: int=100) -> tuple:
        """定时任务：受影响角色全部出本后，逐单短事务执行延后撤销。"""
        limit = max(1, min(int(limit or 100), 200))
        pending_rows = db_manager.query(AdventurerS1DataAppeal.id, AdventurerS1DataAppeal.cross_character_revert_requested_by).filter(AdventurerS1DataAppeal.cross_character_revert_status == 'pending').order_by(AdventurerS1DataAppeal.cross_character_revert_requested_at.asc()).limit(limit).all()
        reverted = []
        waiting = []
        errors = []
        for raw_appeal_id, requested_by in pending_rows:
            appeal_id = int(raw_appeal_id)
            exploring_ids = AdventurerS1DataAppealService._revert_exploring_adventurer_ids(appeal_id)
            if exploring_ids:
                waiting.append({'id': appeal_id, 'exploring_adventurer_ids': exploring_ids})
                continue
            data, err = AdventurerS1DataAppealService.revert_appeal(appeal_id, admin_id=int(requested_by or 0))
            if err:
                AdventurerS1DataAppealService._record_pending_cross_character_revert_error(appeal_id, err)
                errors.append({'id': appeal_id, 'error': err})
            elif data:
                reverted.append(data)
        return ({'reverted': reverted, 'waiting': waiting, 'errors': errors}, None)

    @staticmethod
    def get_admin_detail(appeal_id: int) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).first()
        if not appeal:
            return (None, '申诉不存在')
        data = appeal.to_dict(include_patch=True, include_ai_fields=True)
        preview_patch = copy.deepcopy(appeal.proposed_patch) if isinstance(appeal.proposed_patch, dict) else {}
        supplement = appeal.review_supplement_patch if isinstance(appeal.review_supplement_patch, dict) else {}
        if AdventurerS1DataAppealService._patch_has_operations(supplement):
            for section in ('traits', 'equipment', 'warehouse', 'skills'):
                preview_patch.setdefault(section, {'update': [], 'delete': []})
                sec = supplement.get(section) or {}
                preview_patch[section].setdefault('update', []).extend(sec.get('update') or [])
                preview_patch[section].setdefault('delete', []).extend(sec.get('delete') or [])
            for key in ('create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'reclaim_items'):
                preview_patch.setdefault(key, [])
                preview_patch[key].extend(supplement.get(key) or [])
            for key in ('team_pool', 'tavern_subsidy'):
                if supplement.get(key):
                    preview_patch[key] = supplement[key]
        if AdventurerS1DataAppealService._patch_has_operations(preview_patch):
            data['preview'] = AdventurerS1DataAppealService.build_preview(appeal.snapshot_before, preview_patch)
        else:
            data['preview'] = {'before': appeal.snapshot_before, 'after': appeal.snapshot_before, 'changes': []}
        data['suggested_reject_reason'] = AdventurerS1DataAppealService.get_suggested_reject_reason(appeal)
        audit_rows = db_manager.query(AdventurerS1DataAppealAuditLog).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal_id).order_by(AdventurerS1DataAppealAuditLog.op_seq.asc()).all()
        data['audit_logs'] = [r.to_admin_dict() for r in audit_rows]
        active_audit_count = sum((1 for row in audit_rows if not row.reverted))
        if not audit_rows:
            execution_state = 'not_applied'
        elif active_audit_count == 0:
            execution_state = 'reverted'
        elif appeal.status == 'approved':
            execution_state = 'applied'
        else:
            execution_state = 'partially_applied'
        data['execution_state'] = execution_state
        data['applied_change_count'] = active_audit_count
        data['can_revert'] = AdventurerS1DataAppealService._appeal_can_revert(appeal)
        data['can_send_revert_offer'] = AdventurerS1DataAppealService._appeal_can_send_revert_offer(appeal)
        data['revert_offer_status'] = appeal.revert_offer_status or 'none'
        data['revert_offer_sent_at'] = to_utc_iso(appeal.revert_offer_sent_at) if appeal.revert_offer_sent_at else None
        data['can_fix_misclassified_withdrawal'] = AdventurerS1DataAppealService.can_fix_misclassified_withdrawal(appeal)
        data['can_retry_player_revert_request'] = AdventurerS1DataAppealService.can_retry_player_revert_request(appeal)
        data['can_reset_player_revert_request'] = AdventurerS1DataAppealService.can_reset_player_revert_request(appeal)
        data['can_refund_rarity_review_guard_failure'] = AdventurerS1DataAppealService.can_refund_rarity_review_guard_failure(appeal)
        manual_takeover = AdventurerS1DataAppealService._manual_takeover_projection(appeal)
        data['manual_takeover'] = manual_takeover
        data['can_manual_takeover'] = manual_takeover is None and AdventurerS1DataAppealService._manual_takeover_allowed(appeal)
        data['can_save_manual_draft'] = bool(manual_takeover and manual_takeover.get('status') in ('pending', 'ready') and (appeal.status == 'awaiting_review'))
        data = AdventurerS1DataAppealService._enrich_cancellation_display(appeal, data, include_admin_actions=True)
        return (data, None)
    _REVERT_ELIGIBLE_STATUSES = frozenset({'approved', 'rejected'})

    @staticmethod
    def _appeal_ids_with_revertible_audits(appeal_ids) -> set[int]:
        normalized_ids = {int(appeal_id) for appeal_id in appeal_ids or set() if appeal_id}
        if not normalized_ids:
            return set()
        rows = db_manager.query(AdventurerS1DataAppealAuditLog.appeal_id).filter(AdventurerS1DataAppealAuditLog.appeal_id.in_(normalized_ids), AdventurerS1DataAppealAuditLog.reverted.is_(False)).distinct().all()
        return {int(appeal_id) for appeal_id, in rows}

    @staticmethod
    def _appeal_has_revertible_audits(appeal_id: int) -> bool:
        return bool(db_manager.query(AdventurerS1DataAppealAuditLog.id).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal_id, AdventurerS1DataAppealAuditLog.reverted.is_(False)).limit(1).first())

    @staticmethod
    def _appeal_effective_apply_time(appeal: AdventurerS1DataAppeal):
        """申诉落库时间锚点：已通过用 applied_at；已驳回但部分落库用 reviewed_at 或首条审计。"""
        if appeal.applied_at:
            return appeal.applied_at
        if appeal.reviewed_at:
            return appeal.reviewed_at
        if not appeal.id:
            return None
        from sqlalchemy import func
        return db_manager.query(func.min(AdventurerS1DataAppealAuditLog.created_at)).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal.id).scalar()

    @staticmethod
    def _appeal_can_revert(appeal: AdventurerS1DataAppeal, *, has_revertible_audits: bool | None=None) -> bool:
        if (appeal.status or '') not in AdventurerS1DataAppealService._REVERT_ELIGIBLE_STATUSES:
            return False
        if has_revertible_audits is not None:
            return has_revertible_audits
        return AdventurerS1DataAppealService._appeal_has_revertible_audits(appeal.id)

    @staticmethod
    def _appeal_can_send_revert_offer(appeal: AdventurerS1DataAppeal) -> bool:
        if not AdventurerS1DataAppealService._appeal_can_revert(appeal):
            return False
        return (appeal.revert_offer_status or 'none') != 'pending'

    @staticmethod
    def _fixed_buyback_sale_quantity(description: str, item_name: str) -> int | None:
        """解析本项目收购流水的固定尾部格式：出售「名称」或出售「名称」×N。"""
        marker = f'出售「{item_name}」'
        text = str(description or '').strip()
        marker_pos = text.rfind(marker)
        if marker_pos < 0:
            return None
        suffix = text[marker_pos + len(marker):]
        if not suffix:
            return 1
        if suffix.startswith('×') and suffix[1:].isdigit():
            return max(1, int(suffix[1:]))
        return None

    @staticmethod
    def _created_item_sale_clawback(adventurer_id: int, row: AdventurerS1DataAppealAuditLog, missing_quantity: int, *, allow_partial: bool=False) -> dict | None:
        """按申诉审计之后的结构化出售流水，计算缺失补发物的实际到账扣回额。"""
        missing_quantity = max(0, int(missing_quantity or 0))
        if missing_quantity <= 0:
            return {'quantity': 0, 'gold': 0.0, 'sources': []}
        after = row.after_json if isinstance(row.after_json, dict) else {}
        item_name = str(after.get('name') or '').strip()
        if not item_name:
            return None
        sale_events: list[dict] = []
        buyback_q = db_manager.query(AdventurerS1CurrencyLog).filter(AdventurerS1CurrencyLog.adventurer_id == adventurer_id, AdventurerS1CurrencyLog.currency_code == 'gold', AdventurerS1CurrencyLog.source_type == 'buyback_sell', AdventurerS1CurrencyLog.change_amount > 0, AdventurerS1CurrencyLog.description.contains(f'「{item_name}」'))
        if row.created_at:
            buyback_q = buyback_q.filter(AdventurerS1CurrencyLog.created_at >= row.created_at)
        if row.target_id:
            buyback_q = buyback_q.filter(AdventurerS1CurrencyLog.related_id == str(row.target_id))
        for log in buyback_q.order_by(AdventurerS1CurrencyLog.created_at.asc()).all():
            sold_qty = AdventurerS1DataAppealService._fixed_buyback_sale_quantity(log.description, item_name)
            amount = round(float(log.change_amount or 0), 2)
            if sold_qty and amount > 0:
                sale_events.append({'kind': 'buyback_sell', 'id': int(log.id), 'quantity': sold_qty, 'gold': amount, 'created_at': log.created_at})
        listing_q = db_manager.query(AdventurerS1MarketListing).filter(AdventurerS1MarketListing.seller_adventurer_id == adventurer_id, AdventurerS1MarketListing.item_name == item_name, AdventurerS1MarketListing.listing_type == 'sell', AdventurerS1MarketListing.status == 'completed')
        if row.created_at:
            listing_q = listing_q.filter(AdventurerS1MarketListing.created_at >= row.created_at)
        listings = listing_q.order_by(AdventurerS1MarketListing.created_at.asc()).all()
        expected_type = str(after.get('type') or '').strip()
        expected_rarity = str(after.get('rarity') or '').strip().lower()
        expected_desc = str(after.get('custom_desc') or '').strip()
        listings = [listing for listing in listings if (not expected_type or str(listing.item_type or '').strip() == expected_type) and (not expected_rarity or str(listing.rarity or '').strip().lower() == expected_rarity) and (not expected_desc or str(listing.custom_desc or '').strip() == expected_desc)]
        listing_ids = [int(listing.id) for listing in listings]
        if listing_ids:
            listing_id_texts = [str(listing_id) for listing_id in listing_ids]
            movement_rows = db_manager.query(AdventurerS1ItemLog).filter(AdventurerS1ItemLog.adventurer_id == adventurer_id, AdventurerS1ItemLog.item_name == item_name, AdventurerS1ItemLog.change_type == 'consume', AdventurerS1ItemLog.source_type == 'market', AdventurerS1ItemLog.related_type == 'market_listing', AdventurerS1ItemLog.related_id.in_(listing_id_texts)).all()
            income_rows = db_manager.query(AdventurerS1CurrencyLog).filter(AdventurerS1CurrencyLog.adventurer_id == adventurer_id, AdventurerS1CurrencyLog.currency_code == 'gold', AdventurerS1CurrencyLog.source_type == 'market_sell', AdventurerS1CurrencyLog.related_type == 'market_listing', AdventurerS1CurrencyLog.related_id.in_(listing_id_texts), AdventurerS1CurrencyLog.change_amount > 0).all()
            quantity_by_listing: dict[str, int] = defaultdict(int)
            income_by_listing: dict[str, float] = defaultdict(float)
            for movement in movement_rows:
                quantity_by_listing[str(movement.related_id)] += max(1, int(movement.quantity or 1))
            for income in income_rows:
                income_by_listing[str(income.related_id)] += float(income.change_amount or 0)
            for listing in listings:
                listing_key = str(listing.id)
                sold_qty = int(quantity_by_listing.get(listing_key, 0) or 0)
                amount = round(float(income_by_listing.get(listing_key, 0.0) or 0.0), 2)
                if sold_qty > 0 and amount > 0:
                    sale_events.append({'kind': 'market_sell', 'id': int(listing.id), 'quantity': sold_qty, 'gold': amount, 'created_at': listing.created_at})
        sale_events.sort(key=lambda event: (event.get('created_at') or datetime.min, event['kind'], event['id']))
        remaining = missing_quantity
        clawback_gold = 0.0
        used_sources = []
        for event in sale_events:
            if remaining <= 0:
                break
            event_qty = max(1, int(event['quantity']))
            used_qty = min(remaining, event_qty)
            used_gold = float(event['gold']) if used_qty == event_qty else float(event['gold']) * used_qty / event_qty
            used_gold = round(used_gold, 2)
            clawback_gold = round(clawback_gold + used_gold, 2)
            used_sources.append({'kind': event['kind'], 'id': event['id'], 'quantity': used_qty, 'gold': used_gold})
            remaining -= used_qty
        traced_quantity = missing_quantity - remaining
        if clawback_gold <= 0 or traced_quantity <= 0:
            return None
        if remaining > 0 and (not allow_partial):
            return None
        return {'quantity': traced_quantity, 'gold': clawback_gold, 'sources': used_sources, 'complete': remaining == 0}

    @staticmethod
    def _appeal_traced_sale_unit_price(appeal_id: int) -> float | None:
        """同一申诉内已能追溯的实际出售收入，按数量加权得到统一兜底单价。"""
        rows = db_manager.query(AdventurerS1DataAppealAuditLog).filter(AdventurerS1DataAppealAuditLog.appeal_id == int(appeal_id), AdventurerS1DataAppealAuditLog.op_type == 'create_item', AdventurerS1DataAppealAuditLog.reverted.is_(False)).order_by(AdventurerS1DataAppealAuditLog.op_seq.asc()).all()
        traced_quantity = 0
        traced_gold = 0.0
        for row in rows:
            after = row.after_json if isinstance(row.after_json, dict) else {}
            try:
                granted_quantity = max(1, int(after.get('quantity') or 1))
            except (TypeError, ValueError):
                granted_quantity = 1
            plan = AdventurerS1DataAppealService._created_item_sale_clawback(int(row.adventurer_id), row, granted_quantity, allow_partial=True)
            if not plan:
                continue
            traced_quantity += int(plan['quantity'])
            traced_gold = round(traced_gold + float(plan['gold']), 2)
        if traced_quantity <= 0 or traced_gold <= 0:
            return None
        return round(traced_gold / traced_quantity, 2)

    @staticmethod
    def _created_item_expedition_consumed_quantity(adventurer_id: int, row: AdventurerS1DataAppealAuditLog, missing_quantity: int) -> int:
        """只认申诉落库后的结构化探险消耗流水，不从剧情文本猜用途。"""
        after = row.after_json if isinstance(row.after_json, dict) else {}
        item_name = str(after.get('name') or '').strip()
        if not item_name or missing_quantity <= 0:
            return 0
        query = db_manager.query(AdventurerS1ItemLog).filter(AdventurerS1ItemLog.adventurer_id == int(adventurer_id), AdventurerS1ItemLog.item_name == item_name, AdventurerS1ItemLog.change_type == 'consume', AdventurerS1ItemLog.source_type == 'expedition')
        if row.created_at:
            query = query.filter(AdventurerS1ItemLog.created_at >= row.created_at)
        expected_type = str(after.get('type') or '').strip()
        expected_rarity = str(after.get('rarity') or '').strip().lower()
        if expected_type:
            query = query.filter(AdventurerS1ItemLog.item_type == expected_type)
        if expected_rarity:
            query = query.filter(AdventurerS1ItemLog.item_rarity == expected_rarity)
        consumed = sum((max(1, int(log.quantity or 1)) for log in query.all()))
        return min(max(0, int(missing_quantity)), consumed)

    @staticmethod
    def _created_item_missing_disposition(adventurer_id: int, row: AdventurerS1DataAppealAuditLog, missing_quantity: int, *, fallback_unit_price: float | None=None) -> dict | None:
        """缺失补发物必须由探险消耗或实际出售流水完整覆盖。"""
        missing_quantity = max(0, int(missing_quantity or 0))
        consumed_quantity = AdventurerS1DataAppealService._created_item_expedition_consumed_quantity(adventurer_id, row, missing_quantity)
        sale_quantity = missing_quantity - consumed_quantity
        sale_plan = None
        if sale_quantity > 0:
            sale_plan = AdventurerS1DataAppealService._created_item_sale_clawback(adventurer_id, row, sale_quantity)
            if sale_plan is None:
                try:
                    unit_price = round(float(fallback_unit_price or 0), 2)
                except (TypeError, ValueError):
                    unit_price = 0.0
                if unit_price <= 0:
                    return None
                sale_plan = {'quantity': sale_quantity, 'gold': round(unit_price * sale_quantity, 2), 'sources': [], 'complete': True, 'fallback_unit_price': unit_price}
        return {'quantity': missing_quantity, 'expedition_consumed_quantity': consumed_quantity, 'sale': sale_plan}

    @staticmethod
    def _deleted_skill_restore_state(adventurer_id: int, before: dict, *, lock_rows: bool=False) -> tuple[str, AdventurerS1Skill | None]:
        """判断被删技能是否已由其他确定性恢复路径补回。"""
        name = str(before.get('name') or '').strip()
        if not name:
            return ('invalid', None)
        query = db_manager.query(AdventurerS1Skill).filter(AdventurerS1Skill.adventurer_id == int(adventurer_id), AdventurerS1Skill.name == name)
        if lock_rows:
            query = query.with_for_update()
        existing = query.first()
        if existing is None:
            return ('missing', None)
        expected_description = str(before.get('description') or '')
        if str(existing.description or '') == expected_description:
            return ('equivalent', existing)
        return ('conflict', existing)

    @staticmethod
    def _validate_revert_requirements(appeal_id: int, *, lock_rows: bool=False) -> tuple[bool, str | None]:
        """撤销前校验：玩家仍需持有本次修复补发/增加的资产。"""
        rows = db_manager.query(AdventurerS1DataAppealAuditLog).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal_id, AdventurerS1DataAppealAuditLog.reverted.is_(False)).order_by(AdventurerS1DataAppealAuditLog.op_seq.asc()).all()
        if not rows:
            return (False, '本次申诉没有可以撤回的修改')
        transfer_rows = [row for row in rows if row.op_type == 'transfer_asset']
        if transfer_rows:
            from db_repository.s1_asset_transfer_repository import lock_transfer_state, appeal_affected_member_ids
            from plugins.adventurer_s1.services.s1_asset_transfer_service import validate_transfer_dependencies
            _, assets = lock_transfer_state(appeal_affected_member_ids(appeal_id), lock_rows=lock_rows)
            destinations = {(row.after_json['asset_kind'], row.after_json['asset_id']): row.after_json['from_adventurer_id'] for row in transfer_rows}
            try:
                validate_transfer_dependencies(assets, destinations)
            except ValueError as error:
                return (False, f'无法精确还原：{error}')
        fallback_unit_price = AdventurerS1DataAppealService._appeal_traced_sale_unit_price(appeal_id)
        problems: list[str] = []
        for row in rows:
            after = row.after_json if isinstance(row.after_json, dict) else {}
            adventurer_id = row.adventurer_id
            if row.op_type == 'create_item':
                destination = after.get('destination')
                name = str(after.get('name') or '').strip()
                try:
                    qty = max(1, int(after.get('quantity') or 1))
                except (TypeError, ValueError):
                    qty = 1
                model = {'equipment': AdventurerS1Equipment, 'warehouse': AdventurerS1WarehouseItem}.get(destination)
                if model is None or not name:
                    problems.append('补发物品记录信息不全')
                    continue
                q = db_manager.query(model).filter(model.adventurer_id == adventurer_id, model.name == name).order_by(model.id.desc())
                if lock_rows:
                    q = q.with_for_update()
                record = q.first()
                current = int(record.quantity or 1) if record else 0
                if current < qty:
                    disposition = AdventurerS1DataAppealService._created_item_missing_disposition(adventurer_id, row, qty - current, fallback_unit_price=fallback_unit_price)
                    if disposition is None:
                        problems.append(f'物品「{name}」不足，需保留 {qty}，当前 {current}，且未找到足量的探险消耗或出售流水')
            elif row.op_type == 'create_trait':
                name = str(after.get('name') or '').strip()
                if not name:
                    problems.append('补发特质记录信息不全')
                    continue
                q = db_manager.query(AdventurerS1Trait).filter(AdventurerS1Trait.adventurer_id == adventurer_id, AdventurerS1Trait.name == name).order_by(AdventurerS1Trait.id.desc())
                if lock_rows:
                    q = q.with_for_update()
                if not q.first():
                    problems.append(f'特质「{name}」已不存在')
            elif row.op_type == 'create_skill':
                name = str(after.get('name') or '').strip()
                if not name:
                    problems.append('补发技能记录信息不全')
                    continue
                q = db_manager.query(AdventurerS1Skill).filter(AdventurerS1Skill.adventurer_id == adventurer_id, AdventurerS1Skill.name == name).order_by(AdventurerS1Skill.id.desc())
                if lock_rows:
                    q = q.with_for_update()
                if not q.first():
                    problems.append(f'技能「{name}」已不存在')
            elif row.op_type == 'delete' and row.target_type == 'skill':
                before = row.before_json if isinstance(row.before_json, dict) else {}
                state, _ = AdventurerS1DataAppealService._deleted_skill_restore_state(adventurer_id, before, lock_rows=lock_rows)
                name = str(before.get('name') or '').strip()
                if state == 'invalid':
                    problems.append('被删技能记录信息不全')
                elif state == 'conflict':
                    problems.append(f'技能「{name}」已有同名但描述不同的当前记录，无法精确还原')
        if problems:
            return (False, '当前已不满足撤销条件：' + '；'.join(problems[:5]))
        return (True, None)

    @staticmethod
    def _revert_offer_candidate_query(*, appeal_ids: list[int] | None=None, only_likely_mistake: bool=True):
        """筛选可发送还原确认的申诉候选。"""
        q = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.status == 'approved').filter(AdventurerS1DataAppeal.revert_offer_status.notin_(['pending', 'accepted']))
        if appeal_ids:
            q = q.filter(AdventurerS1DataAppeal.id.in_(appeal_ids))
        q = q.filter(db_manager.query(AdventurerS1DataAppealAuditLog.id).filter(AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id, AdventurerS1DataAppealAuditLog.reverted.is_(False)).exists())
        if only_likely_mistake:
            from sqlalchemy import or_, and_
            item_delete_exists = db_manager.query(AdventurerS1DataAppealAuditLog.id).filter(AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id, AdventurerS1DataAppealAuditLog.op_type == 'delete', AdventurerS1DataAppealAuditLog.target_type.in_(('equipment', 'warehouse')), AdventurerS1DataAppealAuditLog.reverted.is_(False)).exists()
            trait_or_skill_delete_exists = db_manager.query(AdventurerS1DataAppealAuditLog.id).filter(AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id, AdventurerS1DataAppealAuditLog.op_type == 'delete', AdventurerS1DataAppealAuditLog.target_type.in_(('trait', 'skill')), AdventurerS1DataAppealAuditLog.reverted.is_(False)).exists()
            no_create_comp = ~db_manager.query(AdventurerS1DataAppealAuditLog.id).filter(AdventurerS1DataAppealAuditLog.appeal_id == AdventurerS1DataAppeal.id, AdventurerS1DataAppealAuditLog.op_type.in_(('create_item', 'create_trait', 'create_skill')), AdventurerS1DataAppealAuditLog.reverted.is_(False)).exists()
            q = q.filter(no_create_comp, or_(item_delete_exists, trait_or_skill_delete_exists))
        return q
    _REVERT_OFFER_MISTAKE_LABELS = {'phantom_expedition_delete': '探险幻影消耗误删', 'core_request_unsatisfied': '核心诉求未满足仍删物', 'expedition_multi_item_delete': '探险关联多物品删除', 'trait_delete_without_compensation': '特质删除未补合并', 'skill_delete_without_compensation': '技能删除未补合并'}

    @staticmethod
    def _appeal_proposed_patch(appeal: AdventurerS1DataAppeal) -> dict:
        patch = appeal.proposed_patch
        return patch if isinstance(patch, dict) else {}

    @staticmethod
    def _trait_delete_missing_compensation(appeal: AdventurerS1DataAppeal, trait_deletes: list, *, has_create_trait_audit: bool) -> bool:
        if not trait_deletes or has_create_trait_audit:
            return False
        count = len(trait_deletes)
        patch = AdventurerS1DataAppealService._appeal_proposed_patch(appeal)
        proposed_merges = patch.get('create_traits') or []
        if count >= 2 and proposed_merges:
            return True
        if count >= 2 and appeal.core_request_satisfied is False:
            return True
        if count >= 3:
            return True
        if count >= 1 and proposed_merges:
            return True
        return False

    @staticmethod
    def _skill_delete_missing_compensation(appeal: AdventurerS1DataAppeal, skill_deletes: list, *, has_create_skill_audit: bool) -> bool:
        if not skill_deletes or has_create_skill_audit:
            return False
        count = len(skill_deletes)
        patch = AdventurerS1DataAppealService._appeal_proposed_patch(appeal)
        proposed_merges = patch.get('create_skills') or []
        if count >= 2 and proposed_merges:
            return True
        if count >= 2 and appeal.core_request_satisfied is False:
            return True
        if count >= 3:
            return True
        if count >= 1 and proposed_merges:
            return True
        return False

    @staticmethod
    def _assess_revert_offer_mistake(appeal: AdventurerS1DataAppeal, audit_rows: list) -> tuple[bool, str | None, list[str]]:
        """判断是否为「疑似误删、值得推送还原确认」的申诉。"""
        if any((r.op_type in ('create_item', 'create_trait', 'create_skill') for r in audit_rows)):
            return (False, None, [])
        item_deletes = [r for r in audit_rows if r.op_type == 'delete' and r.target_type in ('equipment', 'warehouse')]
        trait_deletes = [r for r in audit_rows if r.op_type == 'delete' and r.target_type == 'trait']
        skill_deletes = [r for r in audit_rows if r.op_type == 'delete' and r.target_type == 'skill']
        if not item_deletes and (not trait_deletes) and (not skill_deletes):
            return (False, None, [])
        signals: list[str] = []
        if AdventurerS1DataAppealService._trait_delete_missing_compensation(appeal, trait_deletes, has_create_trait_audit=False):
            signals.append('trait_delete_without_compensation')
        if AdventurerS1DataAppealService._skill_delete_missing_compensation(appeal, skill_deletes, has_create_skill_audit=False):
            signals.append('skill_delete_without_compensation')
        if item_deletes:
            consumed = AdventurerS1DataAppealService._expedition_evidence_consumed_item_names(appeal.expedition_evidence if isinstance(appeal.expedition_evidence, dict) else None)
            for row in item_deletes:
                before = row.before_json if isinstance(row.before_json, dict) else {}
                name = str(before.get('name') or '').strip()
                if name and name in consumed:
                    signals.append('phantom_expedition_delete')
                    break
            if appeal.core_request_satisfied is False:
                signals.append('core_request_unsatisfied')
            has_expedition = isinstance(appeal.expedition_evidence, dict) and bool(appeal.expedition_evidence)
            if has_expedition and len(item_deletes) >= 2:
                signals.append('expedition_multi_item_delete')
        if not signals:
            return (False, None, [])
        return (True, signals[0], signals)

    @staticmethod
    def _load_appeal_audit_rows(appeal_id: int) -> list:
        return db_manager.query(AdventurerS1DataAppealAuditLog).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal_id, AdventurerS1DataAppealAuditLog.reverted.is_(False)).order_by(AdventurerS1DataAppealAuditLog.op_seq.asc()).all()

    @staticmethod
    def _collect_revert_offer_preview_items(*, appeal_ids: list[int] | None=None, only_likely_mistake: bool=True, limit: int=50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        if appeal_ids:
            appeals = AdventurerS1DataAppealService._revert_offer_candidate_query(appeal_ids=appeal_ids, only_likely_mistake=False).order_by(AdventurerS1DataAppeal.id.desc()).all()
            items = [AdventurerS1DataAppealService._summarize_revert_offer_candidate(a) for a in appeals]
            if only_likely_mistake:
                items = [i for i in items if i.get('likely_mistake')]
            return items[:limit]
        items: list[dict] = []
        offset = 0
        batch_size = max(limit * 5, 50)
        max_scan = 800
        scanned = 0
        while len(items) < limit and scanned < max_scan:
            batch = AdventurerS1DataAppealService._revert_offer_candidate_query(only_likely_mistake=True).order_by(AdventurerS1DataAppeal.applied_at.desc()).offset(offset).limit(batch_size).all()
            if not batch:
                break
            scanned += len(batch)
            offset += batch_size
            for appeal in batch:
                summary = AdventurerS1DataAppealService._summarize_revert_offer_candidate(appeal)
                if only_likely_mistake and (not summary.get('likely_mistake')):
                    continue
                items.append(summary)
                if len(items) >= limit:
                    break
        return items

    @staticmethod
    def _summarize_revert_offer_candidate(appeal: AdventurerS1DataAppeal) -> dict:
        from models.user import User
        audit_rows = AdventurerS1DataAppealService._load_appeal_audit_rows(appeal.id)
        user = db_manager.query(User).filter_by(id=appeal.user_id).first()
        adv = db_manager.query(AdventurerS1).filter_by(id=appeal.adventurer_id).first()
        delete_names: list[str] = []
        item_delete_count = 0
        trait_delete_count = 0
        skill_delete_count = 0
        for row in audit_rows:
            if row.op_type != 'delete':
                continue
            if row.target_type in ('equipment', 'warehouse'):
                item_delete_count += 1
            elif row.target_type == 'trait':
                trait_delete_count += 1
            elif row.target_type == 'skill':
                skill_delete_count += 1
            before = row.before_json if isinstance(row.before_json, dict) else {}
            name = str(before.get('name') or '').strip()
            if name and name not in delete_names:
                delete_names.append(name[:40])
        likely_mistake, primary_signal, mistake_signals = AdventurerS1DataAppealService._assess_revert_offer_mistake(appeal, audit_rows)
        can_send = AdventurerS1DataAppealService._appeal_can_send_revert_offer(appeal)
        skip_reason = None
        if not can_send:
            if appeal.revert_offer_status == 'pending':
                skip_reason = '已有待确认邀请'
            elif not AdventurerS1DataAppealService._appeal_has_revertible_audits(appeal.id):
                skip_reason = '无可还原审计'
            else:
                skip_reason = '不可发送'
        elif not likely_mistake:
            skip_reason = '未命中误删特征（多为正常整理）'
        return {'id': appeal.id, 'user_id': appeal.user_id, 'username': user.username if user else '未知', 'nickname': user.nickname if user else None, 'adventurer_id': appeal.adventurer_id, 'adventurer_name': adv.name if adv else '未知', 'player_note': (appeal.player_note or '')[:120], 'applied_at': to_utc_iso(appeal.applied_at) if appeal.applied_at else None, 'revert_offer_status': appeal.revert_offer_status or 'none', 'core_request_satisfied': appeal.core_request_satisfied, 'delete_count': sum((1 for r in audit_rows if r.op_type == 'delete')), 'item_delete_count': item_delete_count, 'trait_delete_count': trait_delete_count, 'skill_delete_count': skill_delete_count, 'create_compensation_count': sum((1 for r in audit_rows if r.op_type in ('create_item', 'create_trait', 'create_skill'))), 'deleted_item_names': delete_names[:8], 'likely_mistake': likely_mistake, 'mistake_signal': primary_signal, 'mistake_signal_label': AdventurerS1DataAppealService._REVERT_OFFER_MISTAKE_LABELS.get(primary_signal or '', '') or None, 'mistake_signals': mistake_signals, 'can_send': can_send and likely_mistake, 'skip_reason': skip_reason}

    @staticmethod
    def preview_revert_offers(*, appeal_ids: list[int] | None=None, only_likely_mistake: bool=True, limit: int=50) -> tuple:
        """预览可发送还原确认的申诉（默认：命中误删特征的高风险单）。"""
        limit = max(1, min(int(limit or 50), 200))
        items = AdventurerS1DataAppealService._collect_revert_offer_preview_items(appeal_ids=appeal_ids, only_likely_mistake=only_likely_mistake, limit=limit)
        sendable = [i for i in items if i['can_send']]
        return ({'summary': {'total_candidates': len(items), 'sendable_count': len(sendable), 'only_likely_mistake': only_likely_mistake, 'limit': limit}, 'items': items}, None)

    @staticmethod
    def list_pending_revert_offers(user_id: int, limit: int=20) -> tuple:
        """玩家个人页：待确认的还原/撤回列表（管理员邀请 + 玩家申请 AI 已通过）。"""
        limit = max(1, min(int(limit or 20), 50))
        from sqlalchemy import or_, and_
        rows = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.user_id == user_id, AdventurerS1DataAppeal.appeal_category != 'revert_request', or_(and_(AdventurerS1DataAppeal.status == 'approved', AdventurerS1DataAppeal.revert_offer_status == 'pending'), AdventurerS1DataAppeal.player_revert_request_status == 'allowed')).order_by(AdventurerS1DataAppeal.updated_at.desc()).limit(limit).all()
        if not rows:
            return ([], None)
        from models.user import User
        user = db_manager.query(User).filter_by(id=user_id).first()
        user_map = {user_id: user} if user else {}
        adv_ids = {r.adventurer_id for r in rows if r.adventurer_id}
        adv_name_map = dict(db_manager.query(AdventurerS1.id, AdventurerS1.name).filter(AdventurerS1.id.in_(adv_ids)).all()) if adv_ids else {}
        out = []
        for r in rows:
            if (r.revert_offer_status or 'none') == 'pending':
                out.append({'id': r.id, 'kind': 'admin_offer', 'adventurer_id': r.adventurer_id, 'adventurer_name': adv_name_map.get(r.adventurer_id) or '未知', 'player_note': (r.player_note or '')[:120], 'revert_offer_note': (r.revert_offer_note or '').strip()[:500] or None, 'revert_offer_sent_at': to_utc_iso(r.revert_offer_sent_at) if r.revert_offer_sent_at else None})
            elif (r.player_revert_request_status or 'none') == 'allowed':
                out.append({'id': r.id, 'kind': 'player_request', 'adventurer_id': r.adventurer_id, 'adventurer_name': adv_name_map.get(r.adventurer_id) or '未知', 'player_note': (r.player_note or '')[:120], 'revert_offer_note': (r.player_revert_request_ai_summary or '').strip()[:500] or None, 'revert_offer_sent_at': to_utc_iso(r.player_revert_request_responded_at) if r.player_revert_request_responded_at else None})
        return (out, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def send_revert_offer(appeal_id: int, admin_id: int, admin_note: str | None=None) -> tuple:
        """管理员向玩家推送「同意则还原申诉修复」确认（仅个人页展示，不发邮件）。"""
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if not AdventurerS1DataAppealService._appeal_can_revert(appeal):
            return (None, '仅存在可还原审计记录的已通过或部分落库驳回申诉可发送')
        ok, err = AdventurerS1DataAppealService._validate_revert_requirements(appeal.id)
        if not ok:
            return (None, err or '当前已不满足撤销条件')
        if (appeal.revert_offer_status or 'none') == 'pending':
            return (None, '已有待确认的还原邀请，请勿重复发送')
        now = _portable_utc_now()
        note = str(admin_note or '').strip() or None
        appeal.revert_offer_status = 'pending'
        appeal.revert_offer_sent_at = now
        appeal.revert_offer_responded_at = None
        appeal.revert_offer_note = note
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        return ({'id': appeal.id, 'revert_offer_status': 'pending'}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def bulk_send_revert_offers(admin_id: int, *, appeal_ids: list[int] | None=None, only_likely_mistake: bool=True, only_with_deletes: bool | None=None, limit: int=50, admin_note: str | None=None) -> tuple:
        """批量向已通过申诉的玩家推送还原确认（默认：只删不补的疑似误操作）。"""
        if only_with_deletes is not None:
            only_likely_mistake = bool(only_with_deletes)
        limit = max(1, min(int(limit or 50), 200))
        ids = [int(i) for i in appeal_ids or [] if i is not None]
        sent: list[dict] = []
        skipped: list[dict] = []
        errors: list[dict] = []
        if ids:
            summaries = AdventurerS1DataAppealService._collect_revert_offer_preview_items(appeal_ids=ids, only_likely_mistake=only_likely_mistake, limit=len(ids))
            id_to_summary = {i['id']: i for i in summaries}
            for appeal_id in ids:
                summary = id_to_summary.get(appeal_id)
                if not summary or not summary.get('can_send'):
                    reason = (summary or {}).get('skip_reason') or '不可发送'
                    skipped.append({'id': appeal_id, 'reason': reason})
                    continue
                data, err = AdventurerS1DataAppealService.send_revert_offer(appeal_id, admin_id, admin_note=admin_note)
                if err:
                    errors.append({'id': appeal_id, 'error': err})
                else:
                    sent.append(data)
            return ({'sent': sent, 'skipped': skipped, 'errors': errors}, None)
        preview_items = AdventurerS1DataAppealService._collect_revert_offer_preview_items(only_likely_mistake=only_likely_mistake, limit=limit)
        for summary in preview_items:
            if not summary.get('can_send'):
                skipped.append({'id': summary['id'], 'reason': summary.get('skip_reason') or '不可发送'})
                continue
            data, err = AdventurerS1DataAppealService.send_revert_offer(summary['id'], admin_id, admin_note=admin_note)
            if err:
                errors.append({'id': summary['id'], 'error': err})
            else:
                sent.append(data)
        return ({'sent': sent, 'skipped': skipped, 'errors': errors}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def accept_revert_offer(appeal_id: int, user_id: int) -> tuple:
        """玩家同意撤销本次申诉的数据修复，执行台账还原。"""
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, user_id=user_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if (appeal.revert_offer_status or 'none') != 'pending':
            return (None, '当前没有待确认的还原邀请')
        return AdventurerS1DataAppealService.revert_appeal(appeal_id, user_id, by_player=True)

    @staticmethod
    @with_transaction(auto_commit=True)
    def decline_revert_offer(appeal_id: int, user_id: int) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, user_id=user_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if (appeal.revert_offer_status or 'none') != 'pending':
            return (None, '当前没有待确认的还原邀请')
        now = _portable_utc_now()
        appeal.revert_offer_status = 'declined'
        appeal.revert_offer_responded_at = now
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        return ({'id': appeal.id, 'revert_offer_status': 'declined'}, None)

    @staticmethod
    def get_revert_offer_status(appeal_id: int, user_id: int) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, user_id=user_id).first()
        if not appeal:
            return (None, '申诉不存在')
        return ({'id': appeal.id, 'status': appeal.status, 'revert_offer_status': appeal.revert_offer_status or 'none'}, None)

    @staticmethod
    def _can_request_player_revert(appeal: AdventurerS1DataAppeal, *, has_revertible_audits: bool | None=None) -> bool:
        if not appeal or (appeal.appeal_category or '') in ('revert_request', 'tavern_relief'):
            return False
        if (appeal.revert_offer_status or 'none') == 'pending':
            return False
        prs = appeal.player_revert_request_status or 'none'
        if prs in ('processing', 'allowed'):
            return False
        if not AdventurerS1DataAppealService._appeal_can_revert(appeal, has_revertible_audits=has_revertible_audits):
            return False
        return True

    @staticmethod
    def _audit_rows_for_revert_summary(appeal_id: int) -> list[AdventurerS1DataAppealAuditLog]:
        return db_manager.query(AdventurerS1DataAppealAuditLog).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal_id).order_by(AdventurerS1DataAppealAuditLog.op_seq.asc()).all()

    @staticmethod
    def _summarize_audit_for_revert_ai(rows: list) -> list[dict]:
        out = []
        for row in rows or []:
            before = row.before_json if isinstance(row.before_json, dict) else {}
            after = row.after_json if isinstance(row.after_json, dict) else {}
            name = before.get('name') or after.get('name') or after.get('item_name')
            entry = {'op_type': row.op_type, 'target_type': row.target_type, 'target_id': row.target_id, 'name': name, 'reverted': bool(row.reverted)}
            if row.op_type == 'delete' and before:
                entry['deleted'] = {k: before.get(k) for k in ('name', 'type', 'rarity', 'quantity', 'custom_desc', 'description') if before.get(k) not in (None, '')}
            elif row.op_type == 'update' and after:
                entry['changed_fields'] = list(after.keys())
            elif row.op_type == 'create_item' and after:
                entry['created'] = {k: after.get(k) for k in ('name', 'destination', 'quantity', 'type', 'rarity') if after.get(k) not in (None, '')}
            out.append(entry)
        return out

    @staticmethod
    def _find_revert_request_conflicts(appeal: AdventurerS1DataAppeal) -> list[dict]:
        anchor = AdventurerS1DataAppealService._appeal_effective_apply_time(appeal)
        if not appeal or not anchor:
            return []
        rows = AdventurerS1DataAppealService._audit_rows_for_revert_summary(appeal.id)
        keys = {(r.target_type, int(r.target_id)) for r in rows if r.target_id and (not r.reverted)}
        if not keys:
            return []
        later_appeals = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.adventurer_id == appeal.adventurer_id, AdventurerS1DataAppeal.status.in_(('approved', 'rejected')), AdventurerS1DataAppeal.id != appeal.id).order_by(AdventurerS1DataAppeal.id.asc()).all()
        conflicts = []
        for la in later_appeals:
            la_anchor = AdventurerS1DataAppealService._appeal_effective_apply_time(la)
            if not la_anchor or la_anchor <= anchor:
                continue
            la_rows = AdventurerS1DataAppealService._audit_rows_for_revert_summary(la.id)
            for r in la_rows:
                if r.target_id and (r.target_type, int(r.target_id)) in keys:
                    conflicts.append({'appeal_id': la.id, 'target_type': r.target_type, 'target_id': r.target_id, 'op_type': r.op_type})
        return conflicts

    @staticmethod
    @with_transaction(auto_commit=True)
    def request_player_revert(appeal_id: int, user_id: int, player_note: str) -> tuple:
        """玩家申请撤回某次申诉的数据修复/已落库修改（消耗 1 次申诉额度，AI 审核）。"""
        note = (player_note or '').strip()
        if not note:
            return (None, '请说明为何要撤回此次修复')
        if len(note) > _MAX_NOTE_LEN:
            return (None, f'说明不能超过 {_MAX_NOTE_LEN} 字')
        target = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, user_id=user_id).with_for_update().first()
        if not target:
            return (None, '申诉不存在')
        if not AdventurerS1DataAppealService._can_request_player_revert(target):
            prs = target.player_revert_request_status or 'none'
            if prs == 'processing':
                return (None, '撤回申请审核中，请稍候')
            if prs == 'allowed':
                return (None, '撤回申请已通过，请在申诉记录中点击确认')
            if target.status not in AdventurerS1DataAppealService._REVERT_ELIGIBLE_STATUSES:
                return (None, '仅已处理且产生修改的申诉可以申请撤回')
            if not AdventurerS1DataAppealService._appeal_has_revertible_audits(target.id):
                return (None, '本次申诉没有可以撤回的修改')
            return (None, '当前不可申请撤回此次修复')
        ok, err = AdventurerS1DataAppealService._validate_revert_requirements(target.id)
        if not ok:
            return (None, err or '当前已不满足撤销条件')
        quota = AdventurerS1DataAppealService._compute_quota_state(user_id, _APPEAL_QUOTA_POOL_STANDARD)
        if quota['used'] >= quota['limit']:
            return (None, AdventurerS1DataAppealService._quota_limit_error(quota))
        now = _portable_utc_now()
        stub = AdventurerS1DataAppeal(quota_source='unrestricted', user_id=user_id, adventurer_id=target.adventurer_id, player_note=note, appeal_category='revert_request', revert_target_appeal_id=appeal_id, selected_targets=[], snapshot_before={}, status='processing', ai_status='pending')
        ok, err = db_manager.add(stub)
        if not ok:
            return (None, err or '提交失败')
        target.player_revert_request_status = 'processing'
        target.player_revert_request_note = note
        target.player_revert_request_ai_summary = None
        target.player_revert_request_stub_id = stub.id
        target.player_revert_request_submitted_at = now
        target.player_revert_request_responded_at = None
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '提交失败')
        from services.async_task_service import AsyncTaskService
        submitted = AsyncTaskService.submit_task('ai_s1_data_appeal_revert_request', {'target_appeal_id': appeal_id, 'stub_id': stub.id})
        if not submitted:
            logger.warning('修复撤回 AI 首次入队失败 target_appeal=%s stub=%s，等待 recovery', appeal_id, stub.id)
        AdventurerS1DataAppealService._emit_data_appeal_changed(target)
        return ({'id': appeal_id, 'player_revert_request_status': 'processing', 'message': '撤回申请已提交，正在核查'}, None)

    @staticmethod
    def process_player_revert_request(target_appeal_id: int, stub_id: int | None=None) -> None:
        """异步 worker：AI 裁决玩家撤回申请。"""
        target = AdventurerS1DataAppealService._load_appeal(target_appeal_id)
        if not target or (target.player_revert_request_status or 'none') != 'processing':
            return
        stub = None
        if stub_id:
            stub = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(stub_id)).first()
        if not stub and target.player_revert_request_stub_id:
            stub = db_manager.query(AdventurerS1DataAppeal).filter_by(id=target.player_revert_request_stub_id).first()
        conflicts = AdventurerS1DataAppealService._find_revert_request_conflicts(target)
        if conflicts:
            reason = '后续申诉已修改相关数据，无法安全撤回，请联系管理员处理。'
            AdventurerS1DataAppealService._finalize_player_revert_request(target, stub, decision='reject', reason=reason)
            return
        ok, err = AdventurerS1DataAppealService._validate_revert_requirements(target.id)
        if not ok:
            AdventurerS1DataAppealService._finalize_player_revert_request(target, stub, decision='reject', reason=(err or '当前已不满足撤销条件')[:500])
            return
        audit_rows = AdventurerS1DataAppealService._audit_rows_for_revert_summary(target.id)
        audit_summary = AdventurerS1DataAppealService._summarize_audit_for_revert_ai(audit_rows)
        context = {'id': target.id, 'adventurer_id': target.adventurer_id, 'status': target.status, 'player_note': target.player_note, 'ai_summary': target.ai_summary, 'target_labels': target._target_labels(), 'expedition_evidence': target.expedition_evidence, 'applied_at': to_utc_iso(target.applied_at) if target.applied_at else None, 'reject_reason': target.reject_reason}
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_review_ai_service import call_data_appeal_revert_request_ai
        try:
            raw = call_data_appeal_revert_request_ai(target.id, player_revert_note=target.player_revert_request_note or '', appeal_context=context, audit_changes=audit_summary, later_conflicts=conflicts)
        except Exception as exc:
            logger.error('玩家撤回申诉 AI 异常 appeal=%s: %s', target_appeal_id, exc, exc_info=True)
            raw = {'decision': 'error', 'reason': '撤回审核暂时不可用，请稍后再试'}
        target = AdventurerS1DataAppealService._load_appeal(target_appeal_id)
        if not target or (target.player_revert_request_status or 'none') != 'processing':
            return
        if stub_id:
            stub = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(stub_id)).first()
        decision = str((raw or {}).get('decision') or '').strip().lower()
        reason = str((raw or {}).get('reason') or (raw or {}).get('summary') or '').strip()
        if decision not in ('allow', 'reject'):
            decision = 'reject'
            reason = reason or '撤回审核未通过，请补充说明或联系管理员'
        AdventurerS1DataAppealService._finalize_player_revert_request(target, stub, decision=decision, reason=reason[:500])

    @staticmethod
    @with_transaction(auto_commit=True)
    def _finalize_player_revert_request(target: AdventurerS1DataAppeal, stub: AdventurerS1DataAppeal | None, *, decision: str, reason: str) -> None:
        now = _portable_utc_now()
        if decision == 'allow':
            target.player_revert_request_status = 'allowed'
            if stub:
                stub.status = 'approved'
        else:
            target.player_revert_request_status = 'rejected'
            if stub:
                stub.status = 'rejected'
        target.player_revert_request_ai_summary = reason
        target.player_revert_request_responded_at = now
        if stub:
            stub.ai_status = 'done'
            stub.ai_summary = reason
            stub.reject_reason = reason if decision != 'allow' else None
        db_manager.commit()
        AdventurerS1DataAppealService._emit_data_appeal_changed(target)

    @staticmethod
    def can_retry_player_revert_request(appeal: AdventurerS1DataAppeal) -> bool:
        if not appeal or (appeal.appeal_category or '') == 'revert_request':
            return False
        return (appeal.player_revert_request_status or 'none') == 'processing'

    @staticmethod
    def can_reset_player_revert_request(appeal: AdventurerS1DataAppeal) -> bool:
        if not appeal or (appeal.appeal_category or '') == 'revert_request':
            return False
        return (appeal.player_revert_request_status or 'none') == 'processing'

    @staticmethod
    def _summarize_stuck_player_revert_request(appeal: AdventurerS1DataAppeal) -> dict:
        from models.user import User
        user = db_manager.query(User).filter_by(id=appeal.user_id).first()
        adv = db_manager.query(AdventurerS1).filter_by(id=appeal.adventurer_id).first()
        stub = None
        if appeal.player_revert_request_stub_id:
            stub = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal.player_revert_request_stub_id).first()
        stub_misprocessed = bool(stub and (stub.appeal_category or '') == 'revert_request' and ((stub.status or '') in ('rejected', 'awaiting_review')) and (stub.id != appeal.id))
        can_retry = AdventurerS1DataAppealService.can_retry_player_revert_request(appeal)
        can_reset = AdventurerS1DataAppealService.can_reset_player_revert_request(appeal)
        return {'id': appeal.id, 'user_id': appeal.user_id, 'username': getattr(user, 'username', None), 'nickname': getattr(user, 'nickname', None), 'adventurer_id': appeal.adventurer_id, 'adventurer_name': getattr(adv, 'name', None) or '未知', 'status': appeal.status, 'player_note': (appeal.player_note or '')[:120], 'player_revert_request_note': (appeal.player_revert_request_note or '')[:200], 'player_revert_request_submitted_at': to_utc_iso(appeal.player_revert_request_submitted_at) if appeal.player_revert_request_submitted_at else None, 'stub_id': stub.id if stub else None, 'stub_status': stub.status if stub else None, 'stub_misprocessed': stub_misprocessed, 'can_retry': can_retry, 'can_reset': can_reset}

    @staticmethod
    def preview_stuck_player_revert_requests(limit: int=50) -> tuple:
        limit = max(1, min(int(limit or 50), 200))
        rows = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.appeal_category != 'revert_request', AdventurerS1DataAppeal.player_revert_request_status == 'processing').order_by(AdventurerS1DataAppeal.player_revert_request_submitted_at.desc()).limit(limit).all()
        items = [AdventurerS1DataAppealService._summarize_stuck_player_revert_request(r) for r in rows]
        return ({'summary': {'total_candidates': len(items), 'retryable_count': sum((1 for i in items if i.get('can_retry')))}, 'items': items}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def admin_retry_player_revert_request(appeal_id: int, admin_id: int) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(appeal_id)).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if not AdventurerS1DataAppealService.can_retry_player_revert_request(appeal):
            return (None, '当前没有可重试的修复撤回申请')
        from services.async_task_service import AsyncTaskService
        stub_id = appeal.player_revert_request_stub_id
        AdventurerS1DataAppealService._release_player_revert_task_lock(appeal.id, stub_id)
        payload = {'target_appeal_id': int(appeal.id), 'stub_id': stub_id}
        if not AsyncTaskService.submit_task('ai_s1_data_appeal_revert_request', payload):
            return (None, '任务入队失败，请稍后重试')
        logger.info('管理员重试修复撤回 AI target=%s stub=%s admin=%s', appeal.id, stub_id, admin_id)
        return ({'id': appeal.id, 'player_revert_request_status': 'processing', 'message': '已重新提交修复撤回 AI'}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def admin_reset_stuck_player_revert_request(appeal_id: int, admin_id: int) -> tuple:
        """解除 processing 卡住状态，允许玩家重新申请撤回修复。"""
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(appeal_id)).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if not AdventurerS1DataAppealService.can_reset_player_revert_request(appeal):
            return (None, '当前不可重置修复撤回申请')
        stub_id = appeal.player_revert_request_stub_id
        stub = None
        if stub_id:
            stub = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(stub_id)).with_for_update().first()
        AdventurerS1DataAppealService._release_player_revert_task_lock(appeal.id, stub_id)
        appeal.player_revert_request_status = 'none'
        appeal.player_revert_request_note = None
        appeal.player_revert_request_ai_summary = None
        appeal.player_revert_request_stub_id = None
        appeal.player_revert_request_submitted_at = None
        appeal.player_revert_request_responded_at = None
        if stub and (stub.appeal_category or '') == 'revert_request':
            stub.status = 'cancelled'
            stub.reject_reason = (stub.reject_reason or '').strip() or '管理员已重置卡住的修复撤回申请'
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        logger.info('管理员重置修复撤回 target=%s stub=%s admin=%s', appeal.id, stub_id, admin_id)
        return ({'id': appeal.id, 'player_revert_request_status': 'none'}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def confirm_player_revert_request(appeal_id: int, user_id: int) -> tuple:
        """玩家确认执行 AI 已同意的撤回（还原台账）。"""
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, user_id=user_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if (appeal.player_revert_request_status or 'none') != 'allowed':
            return (None, '当前没有待确认的撤回申请')
        return AdventurerS1DataAppealService.revert_appeal(appeal_id, user_id, by_player=True, via_player_revert_request=True)

    @staticmethod
    @with_transaction(auto_commit=True)
    def decline_player_revert_request(appeal_id: int, user_id: int) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id, user_id=user_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if (appeal.player_revert_request_status or 'none') != 'allowed':
            return (None, '当前没有待确认的撤回申请')
        now = _portable_utc_now()
        appeal.player_revert_request_status = 'declined'
        appeal.player_revert_request_responded_at = now
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '放弃撤回失败')
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        return ({'id': appeal.id, 'player_revert_request_status': 'declined'}, None)

    @staticmethod
    def _normalize_quota_pool(quota_pool: str | None) -> str | None:
        value = str(quota_pool or '').strip()
        return value if value in _VALID_APPEAL_QUOTA_POOLS else None

    @staticmethod
    def _quota_pool_for_category(appeal_category: str | None) -> str:
        return _APPEAL_QUOTA_POOL_OTHER if str(appeal_category or '').strip() in _FULL_TEXT_APPEAL_CATEGORIES else _APPEAL_QUOTA_POOL_STANDARD

    @staticmethod
    def _quota_adjustment_fields(quota_pool: str) -> tuple[str, str, str]:
        if quota_pool == _APPEAL_QUOTA_POOL_OTHER:
            return ('other_reset_at', 'other_bonus', 'other_bonus_at')
        return ('reset_at', 'bonus', 'bonus_at')

    @staticmethod
    def _quota_limit_error(state: dict) -> str:
        if state['quota_pool'] == _APPEAL_QUOTA_POOL_OTHER and state.get('quota_mode') == 'daily_first_balance_pool':
            return '今日全文申诉次数与余额池次数均已用完'
        label = _APPEAL_QUOTA_POOL_LABELS[state['quota_pool']]
        return f"今日{label}最多提交 {state['limit']} 次"

    @staticmethod
    def _get_base_daily_limit(quota_pool: str=_APPEAL_QUOTA_POOL_STANDARD) -> int:
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        setting_key = 'data_appeal_other_daily_limit' if quota_pool == _APPEAL_QUOTA_POOL_OTHER else 'data_appeal_daily_limit'
        return max(1, int(S1SettingsService.get(setting_key) or 5))

    @staticmethod
    def _get_full_text_balance_cap() -> int:
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        value = S1SettingsService.get('data_appeal_other_balance_cap')
        return max(0, int(5 if value is None else value))

    @staticmethod
    def _get_full_text_balance_started_on(user_id: int):
        """余额池从部署启用日和玩家进入 S1 日二者较晚者开始累计。"""
        from sqlalchemy import func
        from utils.time_utils import get_beijing_time
        from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
        today = get_beijing_time().date()
        raw = S1SettingsService.get(_FULL_TEXT_BALANCE_ACTIVATION_SETTING)
        try:
            started_on = datetime.strptime(str(raw), '%Y-%m-%d').date()
        except (TypeError, ValueError):
            started_on = today
        first_adventurer_at = db_manager.query(func.min(AdventurerS1.created_at)).filter(AdventurerS1.owner_id == int(user_id)).scalar()
        if first_adventurer_at:
            player_started_on = (first_adventurer_at + timedelta(hours=8)).date()
            started_on = max(started_on, player_started_on)
        return min(started_on, today)

    @staticmethod
    def get_user_quota_player(user_id: int) -> dict:
        """玩家侧两个申诉额度池（保留顶层其余申诉字段兼容旧客户端）。"""
        states = {quota_pool: AdventurerS1DataAppealService._compute_quota_state(user_id, quota_pool) for quota_pool in (_APPEAL_QUOTA_POOL_OTHER, _APPEAL_QUOTA_POOL_STANDARD)}
        public_pools = {}
        for quota_pool, state in states.items():
            public_state = {'unlimited': state.get('unlimited', False), 'remaining': state['remaining'], 'limit': state['limit'], 'used_24h': state['used']}
            if state.get('quota_mode') == 'daily_first_balance_pool':
                public_state.update({'quota_mode': state['quota_mode'], 'today_remaining': state['today_remaining'], 'balance_remaining': state['balance_remaining'], 'balance_cap': state['balance_cap'], 'next_balance': state['next_balance']})
            public_pools[quota_pool] = public_state
        standard = public_pools[_APPEAL_QUOTA_POOL_STANDARD]
        return {**public_pools, 'unlimited': standard.get('unlimited', False), 'remaining': standard['remaining'], 'limit': standard['limit'], 'used_24h': standard['used_24h']}

    @staticmethod
    def _compute_quota_state(user_id: int, quota_pool: str=_APPEAL_QUOTA_POOL_STANDARD, *, materialize_full_text: bool=False) -> dict:
        """玩家当前北京日历日指定申诉池的额度状态。

        统计起点为北京时间 0 点（UTC naive）；reset_at 若在本日内则把起点抬到
        reset_at（清零已用）；bonus 仅当 bonus_at 仍在本日内才生效，跨日自动失效。
        """
        from utils.time_utils import get_beijing_today_start_utc
        from plugins.adventurer_s1.services.s1_token_usage_service import appeal_counts_toward_daily_quota
        quota_pool = AdventurerS1DataAppealService._normalize_quota_pool(quota_pool)
        if not quota_pool:
            raise ValueError('invalid appeal quota pool')
        return {'quota_pool': quota_pool, 'unlimited': True, '_next_quota_source': 'unrestricted', 'limit': 1, 'used': 0, 'remaining': 1, 'base_limit': 0, 'bonus': 0, 'reset_at': None}

    @staticmethod
    def _compute_full_text_quota_state(*, user_id: int, day_start: datetime, base_limit: int, bonus: int, reset_at: datetime | None, count_reset_at: datetime | None, window_start: datetime, adjustment, materialize: bool) -> dict:
        """全文申诉先用当日次数，未用基础次数次日存入持久余额池。"""
        from sqlalchemy import and_, case, func, or_
        from utils.time_utils import get_beijing_time
        from plugins.adventurer_s1.services.s1_token_usage_service import appeal_counts_toward_daily_quota
        today = get_beijing_time().date()
        tomorrow_start = day_start + timedelta(days=1)
        balance_cap = AdventurerS1DataAppealService._get_full_text_balance_cap()

        def usage_counts(charge_date, charge_day_start: datetime, charge_day_end: datetime, count_from: datetime) -> dict:
            explicit_charge = and_(AdventurerS1DataAppeal.quota_bucket_date == charge_date, AdventurerS1DataAppeal.quota_source.in_((_FULL_TEXT_QUOTA_SOURCE_DAILY, _FULL_TEXT_QUOTA_SOURCE_BALANCE)))
            legacy_charge = and_(AdventurerS1DataAppeal.quota_source.is_(None), AdventurerS1DataAppeal.created_at >= charge_day_start, AdventurerS1DataAppeal.created_at < charge_day_end)
            charged_on_date = or_(explicit_charge, legacy_charge)
            daily_charge = and_(charged_on_date, or_(AdventurerS1DataAppeal.quota_source == _FULL_TEXT_QUOTA_SOURCE_DAILY, AdventurerS1DataAppeal.quota_source.is_(None)))
            balance_charge = and_(charged_on_date, AdventurerS1DataAppeal.quota_source == _FULL_TEXT_QUOTA_SOURCE_BALANCE)
            row = db_manager.query(func.coalesce(func.sum(case((daily_charge, 1), else_=0)), 0), func.coalesce(func.sum(case((balance_charge, 1), else_=0)), 0)).filter(AdventurerS1DataAppeal.user_id == user_id, AdventurerS1DataAppeal.appeal_category.in_(_FULL_TEXT_APPEAL_CATEGORIES), charged_on_date, AdventurerS1DataAppeal.created_at >= count_from, AdventurerS1DataAppeal.created_at < charge_day_end, appeal_counts_toward_daily_quota(AdventurerS1DataAppeal)).first()
            return {'daily': int(row[0] or 0), 'balance': int(row[1] or 0)}
        stored_balance = max(0, int(getattr(adjustment, 'other_balance', 0) or 0))
        state_date = getattr(adjustment, 'other_balance_date', None)
        state_limit = max(1, int(getattr(adjustment, 'other_daily_limit_snapshot', 0) or base_limit))
        if state_date and state_date > today:
            state_date = today
        if state_date is None:
            started_on = AdventurerS1DataAppealService._get_full_text_balance_started_on(user_id)
            elapsed_days = max(0, (today - started_on).days)
            balance_at_day_start = min(balance_cap, elapsed_days * base_limit)
        elif state_date < today:
            elapsed_days = (today - state_date).days
            state_day_start = day_start - timedelta(days=elapsed_days)
            state_day_end = state_day_start + timedelta(days=1)
            state_count_from = state_day_start
            if count_reset_at and state_day_start <= count_reset_at < state_day_end:
                state_count_from = count_reset_at
            previous_counts = usage_counts(state_date, state_day_start, state_day_end, state_count_from)
            previous_balance = max(0, stored_balance - previous_counts['balance'])
            unused_previous_daily = max(0, state_limit - previous_counts['daily'])
            untouched_days = max(0, elapsed_days - 1)
            balance_at_day_start = min(balance_cap, previous_balance + unused_previous_daily + untouched_days * base_limit)
        else:
            balance_at_day_start = min(balance_cap, stored_balance)
        if materialize:
            if adjustment is None:
                adjustment = AdventurerS1DataAppealQuota(user_id=user_id, bonus=0, other_bonus=0)
                ok, err = db_manager.add(adjustment)
                if not ok:
                    raise RuntimeError(err or '全文申诉余额池初始化失败')
            adjustment.other_balance = balance_at_day_start
            adjustment.other_balance_date = today
            adjustment.other_daily_limit_snapshot = base_limit
        today_counts = usage_counts(today, day_start, tomorrow_start, window_start)
        today_remaining = max(0, base_limit + bonus - today_counts['daily'])
        balance_remaining = max(0, balance_at_day_start - today_counts['balance'])
        next_source = None
        if today_remaining > 0:
            next_source = _FULL_TEXT_QUOTA_SOURCE_DAILY
        elif balance_remaining > 0:
            next_source = _FULL_TEXT_QUOTA_SOURCE_BALANCE
        used = today_counts['daily'] + today_counts['balance']
        remaining = today_remaining + balance_remaining
        next_balance = min(balance_cap, balance_remaining + max(0, base_limit - today_counts['daily']))
        return {'quota_pool': _APPEAL_QUOTA_POOL_OTHER, 'quota_mode': 'daily_first_balance_pool', 'base_limit': base_limit, 'bonus': bonus, 'limit': used + remaining, 'used': used, 'remaining': remaining, 'today_remaining': today_remaining, 'balance_remaining': balance_remaining, 'balance_cap': balance_cap, 'next_balance': next_balance, 'reset_at': reset_at, '_charge_date': today, '_next_quota_source': next_source}

    @staticmethod
    def _resolve_user(user_id=None, username=None):
        from models.user import User
        if user_id not in (None, ''):
            try:
                return db_manager.query(User).filter(User.id == int(user_id)).first()
            except (TypeError, ValueError):
                return None
        uname = str(username or '').strip()
        if uname:
            return db_manager.query(User).filter(User.username == uname).first()
        return None

    @staticmethod
    def get_user_quota_admin(user_id=None, username=None) -> tuple:
        """管理员查看某玩家两个申诉池的额度使用情况。"""
        user = AdventurerS1DataAppealService._resolve_user(user_id=user_id, username=username)
        if not user:
            return (None, '用户不存在')
        states = {quota_pool: AdventurerS1DataAppealService._compute_quota_state(user.id, quota_pool) for quota_pool in (_APPEAL_QUOTA_POOL_OTHER, _APPEAL_QUOTA_POOL_STANDARD)}
        quota_pools = {}
        for quota_pool, state in states.items():
            public_state = {'base_limit': state['base_limit'], 'bonus': state['bonus'], 'limit': state['limit'], 'used_24h': state['used'], 'unlimited': state.get('unlimited', False), 'remaining': state['remaining'], 'reset_at': to_utc_iso(state['reset_at']) if state['reset_at'] else None}
            if state.get('quota_mode') == 'daily_first_balance_pool':
                public_state.update({'quota_mode': state['quota_mode'], 'today_remaining': state['today_remaining'], 'balance_remaining': state['balance_remaining'], 'balance_cap': state['balance_cap'], 'next_balance': state['next_balance']})
            quota_pools[quota_pool] = public_state
        total = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.user_id == user.id).count()
        active = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.user_id == user.id, AdventurerS1DataAppeal.status.in_(('processing', 'awaiting_review'))).count()
        return ({'user_id': user.id, 'username': user.username, 'nickname': user.nickname or None, 'quota_pools': quota_pools, **quota_pools[_APPEAL_QUOTA_POOL_STANDARD], 'total_appeals': total, 'active_appeals': active}, None)

    @staticmethod
    def admin_reset_quota(user_id, admin_id: int, quota_pool: str=_APPEAL_QUOTA_POOL_STANDARD) -> tuple:
        """一键重置该玩家指定申诉池在当前北京日历日内的已用次数。"""
        quota_pool = AdventurerS1DataAppealService._normalize_quota_pool(quota_pool)
        if not quota_pool:
            return (None, '申诉额度类型无效')
        user = AdventurerS1DataAppealService._resolve_user(user_id=user_id)
        if not user:
            return (None, '用户不存在')
        now = _portable_utc_now()
        reset_field, _, _ = AdventurerS1DataAppealService._quota_adjustment_fields(quota_pool)
        adj = db_manager.query(AdventurerS1DataAppealQuota).filter_by(user_id=user.id).first()
        if not adj:
            values = {'user_id': user.id, 'bonus': 0, 'other_bonus': 0, reset_field: now, 'updated_by': admin_id, 'updated_at': now}
            adj = AdventurerS1DataAppealQuota(**values)
            ok, err = db_manager.add(adj)
            if not ok:
                return (None, err or '操作失败')
        else:
            setattr(adj, reset_field, now)
            adj.updated_by = admin_id
            adj.updated_at = now
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '操作失败')
        state = AdventurerS1DataAppealService._compute_quota_state(user.id, quota_pool)
        logger.info('管理员 %s 重置玩家 %s 的 %s 申诉额度', admin_id, user.id, quota_pool)
        return ({'user_id': user.id, 'quota_pool': quota_pool, 'used_24h': state['used'], 'limit': state['limit'], 'remaining': state['remaining']}, None)

    @staticmethod
    def admin_add_quota(user_id, count, admin_id: int, quota_pool: str=_APPEAL_QUOTA_POOL_STANDARD) -> tuple:
        """给指定申诉池增加 N 次一次性额度。"""
        from utils.time_utils import get_beijing_today_start_utc
        quota_pool = AdventurerS1DataAppealService._normalize_quota_pool(quota_pool)
        if not quota_pool:
            return (None, '申诉额度类型无效')
        try:
            count = int(count)
        except (TypeError, ValueError):
            return (None, '次数必须是整数')
        if count <= 0:
            return (None, '增加次数必须为正整数')
        if count > 100:
            return (None, '单次最多增加 100 次')
        user = AdventurerS1DataAppealService._resolve_user(user_id=user_id)
        if not user:
            return (None, '用户不存在')
        now = _portable_utc_now()
        day_start = get_beijing_today_start_utc()
        _, bonus_field, bonus_at_field = AdventurerS1DataAppealService._quota_adjustment_fields(quota_pool)
        adj = db_manager.query(AdventurerS1DataAppealQuota).filter_by(user_id=user.id).first()
        if not adj:
            values = {'user_id': user.id, 'bonus': 0, 'other_bonus': 0, bonus_field: count, bonus_at_field: now, 'updated_by': admin_id, 'updated_at': now}
            adj = AdventurerS1DataAppealQuota(**values)
            ok, err = db_manager.add(adj)
            if not ok:
                return (None, err or '操作失败')
        else:
            current_value = getattr(adj, bonus_field)
            current_at = getattr(adj, bonus_at_field)
            current_bonus = int(current_value) if current_value and current_at and (current_at >= day_start) else 0
            setattr(adj, bonus_field, current_bonus + count)
            setattr(adj, bonus_at_field, now)
            adj.updated_by = admin_id
            adj.updated_at = now
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '操作失败')
        state = AdventurerS1DataAppealService._compute_quota_state(user.id, quota_pool)
        logger.info('管理员 %s 给玩家 %s 的 %s 申诉额度增加 %s 次', admin_id, user.id, quota_pool, count)
        return ({'user_id': user.id, 'quota_pool': quota_pool, 'bonus': state['bonus'], 'limit': state['limit'], 'used_24h': state['used'], 'remaining': state['remaining']}, None)

    @staticmethod
    def admin_reset_all_quota(admin_id: int, quota_pool: str=_APPEAL_QUOTA_POOL_STANDARD) -> tuple:
        """全服一次性重置指定申诉池。

        批量 upsert：已有 quota 行只更新目标池的 reset 字段，缺失的批量 INSERT，
        避免逐人写。
        """
        quota_pool = AdventurerS1DataAppealService._normalize_quota_pool(quota_pool)
        if not quota_pool:
            return (None, '申诉额度类型无效')
        now = _portable_utc_now()
        reset_field, _, _ = AdventurerS1DataAppealService._quota_adjustment_fields(quota_pool)
        owner_rows = db_manager.query(AdventurerS1.owner_id).filter(AdventurerS1.owner_id.isnot(None), AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES)).distinct().all()
        user_ids = [r[0] for r in owner_rows]
        if not user_ids:
            return ({'reset_count': 0, 'quota_pool': quota_pool}, None)
        existing = {r[0] for r in db_manager.query(AdventurerS1DataAppealQuota.user_id).filter(AdventurerS1DataAppealQuota.user_id.in_(user_ids)).all()}
        if existing:
            AdventurerS1DataAppealQuota.query.filter(AdventurerS1DataAppealQuota.user_id.in_(list(existing))).update({reset_field: now, 'updated_by': admin_id, 'updated_at': now}, synchronize_session=False)
        missing = [uid for uid in user_ids if uid not in existing]
        if missing:
            new_rows = [AdventurerS1DataAppealQuota(**{'user_id': uid, 'bonus': 0, 'other_bonus': 0, reset_field: now, 'updated_by': admin_id, 'updated_at': now}) for uid in missing]
            ok, err = db_manager.add_all(new_rows)
            if not ok:
                return (None, err or '操作失败')
        ok, err = db_manager.commit()
        if not ok:
            return (None, err or '操作失败')
        logger.info('管理员 %s 全服重置 %s 申诉额度，影响 %s 名玩家', admin_id, quota_pool, len(user_ids))
        return ({'reset_count': len(user_ids), 'quota_pool': quota_pool}, None)

    @staticmethod
    def _normalize_player_reject_reason(text) -> str:
        return str(text or '').strip()
    _CHECKLIST_STATUS_LABELS = {'done': '✓ 已处理', 'partial': '△ 部分处理', 'rejected': '✗ 未通过', 'pending': '· 待核对'}
    _CHECKLIST_CATEGORY_GOLD = frozenset({'gold', 'subsidy', 'compensation'})
    _CHECKLIST_CATEGORY_QTY = frozenset({'quantity', 'merge', 'stack'})
    _CHECKLIST_CATEGORY_ITEM = frozenset({'item', 'create_item', 'restore'})
    _CHECKLIST_CATEGORY_DESC = frozenset({'desc', 'name', 'text', 'trait_desc', 'skill_desc'})
    _CHECKLIST_CATEGORY_TRAIT = frozenset({'trait', 'skill', 'recovery'})

    @staticmethod
    def _normalize_request_checklist(items, *, player_note: str | None=None) -> list[dict]:
        valid_status = frozenset(AdventurerS1DataAppealService._CHECKLIST_STATUS_LABELS)
        out: list[dict] = []
        if isinstance(items, list):
            for index, item in enumerate(items[:8]):
                if not isinstance(item, dict):
                    continue
                label = str(item.get('label') or item.get('request') or '').strip()[:120]
                if not label:
                    continue
                status = str(item.get('status') or 'pending').strip().lower()
                if status not in valid_status:
                    status = 'pending'
                category = str(item.get('category') or '').strip().lower() or 'other'
                detail = str(item.get('detail') or item.get('reason') or '').strip()[:160]
                row = {'request_id': f'request_{index + 1}', 'label': label, 'status': status, 'category': category, 'is_core': item.get('is_core') is True}
                resolution_basis = item.get('resolution_basis')
                if resolution_basis in S1_DATA_APPEAL_RESOLUTION_BASES:
                    row['resolution_basis'] = resolution_basis
                if detail:
                    row['detail'] = detail
                out.append(row)
        if out:
            if not any((row['is_core'] for row in out)):
                out[0]['is_core'] = True
            return out
        return AdventurerS1DataAppealService._infer_checklist_from_note(player_note)

    @staticmethod
    def _infer_checklist_from_note(note: str | None) -> list[dict]:
        import re
        items: list[dict] = []
        for match in re.finditer('(?:^|\\n)\\s*(\\d+)[\\.、．)\\]]\\s*([^\\n]{4,120})', str(note or '')):
            label = match.group(2).strip()
            items.append({'request_id': f'request_{len(items) + 1}', 'label': label, 'status': 'pending', 'category': 'other', 'is_core': not items})
        if not items:
            for line in str(note or '').splitlines():
                line = line.strip()
                if len(line) < 6:
                    continue
                items.append({'request_id': f'request_{len(items) + 1}', 'label': line[:120], 'status': 'pending', 'category': 'other', 'is_core': not items})
                if len(items) >= 6:
                    break
        for index, row in enumerate(items[:8]):
            row['request_id'] = f'request_{index + 1}'
            row.setdefault('is_core', index == 0)
        return items[:8]

    @staticmethod
    def _sync_request_checklist_with_audits(checklist: list[dict], raw: dict, *, normalized_patch: dict | None, supplement_patch: dict | None=None, patch_raw: dict | None, patch_error: str | None=None) -> list[dict]:
        items = [dict(row) for row in checklist or []]
        if not items:
            return items
        if patch_error:
            detail = '已保留待处理，尚未形成安全写入'
            for row in items:
                if row.get('status') in ('done', 'partial'):
                    row['status'] = 'pending'
                    row['resolution_basis'] = 'repair_pending'
                    if not row.get('detail'):
                        row['detail'] = detail
            return items[:8]

        def _touch(category_keys: frozenset, *, status: str, detail: str, fallback_label: str) -> None:
            touched = False
            for row in items:
                if row.get('category') in category_keys:
                    row['status'] = status
                    if status == 'pending':
                        row['resolution_basis'] = 'repair_pending'
                    if detail and (not row.get('detail')):
                        row['detail'] = detail
                    touched = True
            if not touched:
                items.append({'label': fallback_label, 'category': next(iter(category_keys)), 'status': status, 'detail': detail, **({'resolution_basis': 'repair_pending'} if status == 'pending' else {})})
        qty_issues = raw.get('item_quantity_audit') if isinstance(raw.get('item_quantity_audit'), list) else []
        if qty_issues:
            _touch(AdventurerS1DataAppealService._CHECKLIST_CATEGORY_QTY, status='pending', detail='数量修正尚未形成可安全写入的结果', fallback_label='物品数量修正')
        lineage_warnings = raw.get('lineage_guard_warnings') if isinstance(raw.get('lineage_guard_warnings'), list) else []
        if lineage_warnings:
            detail = str(lineage_warnings[0] or '').strip()[:160] or '该物品已命中历史申诉收益防刷规则'
            _touch(AdventurerS1DataAppealService._CHECKLIST_CATEGORY_QTY | AdventurerS1DataAppealService._CHECKLIST_CATEGORY_ITEM, status='rejected', detail=detail, fallback_label='物品数量/补发诉求')
        delete_comp_issues = raw.get('delete_compensation_audit') if isinstance(raw.get('delete_compensation_audit'), list) else []
        if delete_comp_issues:
            _touch(AdventurerS1DataAppealService._CHECKLIST_CATEGORY_ITEM | AdventurerS1DataAppealService._CHECKLIST_CATEGORY_TRAIT | frozenset({'delete', 'merge'}), status='pending', detail='删除与配套补偿尚未形成安全原子修复', fallback_label='删除/合并操作')
        phantom_delete_issues = raw.get('phantom_expedition_delete_audit') if isinstance(raw.get('phantom_expedition_delete_audit'), list) else []
        if phantom_delete_issues:
            _touch(AdventurerS1DataAppealService._CHECKLIST_CATEGORY_ITEM | frozenset({'delete', 'expedition'}), status='rejected', detail='结算记录已消耗但您仍持有，删除未执行', fallback_label='物品删除')
        trait_issues = raw.get('trait_description_audit') if isinstance(raw.get('trait_description_audit'), list) else []
        if trait_issues:
            _touch(AdventurerS1DataAppealService._CHECKLIST_CATEGORY_DESC | {'trait'}, status='pending', detail='特质/描述修正尚未形成完整可执行结果', fallback_label='特质或描述修正')
        create_item_desc_issues = raw.get('create_item_description_audit') if isinstance(raw.get('create_item_description_audit'), list) else []
        if create_item_desc_issues:
            _touch(AdventurerS1DataAppealService._CHECKLIST_CATEGORY_ITEM, status='pending', detail='补发物品缺少可展示描述，尚未形成可执行结果', fallback_label='补发物品')
        normalized_patch = normalized_patch if isinstance(normalized_patch, dict) else {}
        supplement_patch = supplement_patch if isinstance(supplement_patch, dict) else {}
        effective_patch = AdventurerS1DataAppealService._merge_data_appeal_patches(normalized_patch, supplement_patch)
        patch_raw = patch_raw if isinstance(patch_raw, dict) else {}
        has_cosmetic = AdventurerS1DataAppealService._patch_only_cosmetic_cleanup(effective_patch)
        has_privileged = AdventurerS1DataAppealService._has_privileged_operations(effective_patch) or AdventurerS1DataAppealService._has_privileged_operations(patch_raw)
        has_item_grant = bool(effective_patch.get('create_items') or effective_patch.get('create_traits') or effective_patch.get('create_skills') or effective_patch.get('reclaim_items') or effective_patch.get('unassigned_items')) or AdventurerS1DataAppealService._patch_has_item_record_benefit(effective_patch)
        has_gold_grant = bool(effective_patch.get('currency_adjustments') or effective_patch.get('team_pool') or effective_patch.get('tavern_subsidy'))
        if has_cosmetic:
            for row in items:
                if row.get('category') in AdventurerS1DataAppealService._CHECKLIST_CATEGORY_DESC and row.get('status') == 'pending' and (row.get('resolution_basis') != 'repair_pending'):
                    row['status'] = 'done'
        if has_item_grant or has_gold_grant:
            for row in items:
                cat = str(row.get('category') or '')
                technical_pending = row.get('resolution_basis') == 'repair_pending'
                if not technical_pending and has_item_grant and (cat in AdventurerS1DataAppealService._CHECKLIST_CATEGORY_ITEM | AdventurerS1DataAppealService._CHECKLIST_CATEGORY_TRAIT | {'skill'}):
                    row['status'] = 'done'
                    row.pop('detail', None)
                if not technical_pending and has_gold_grant and (cat in AdventurerS1DataAppealService._CHECKLIST_CATEGORY_GOLD):
                    row['status'] = 'done'
                    row.pop('detail', None)
        if raw.get('core_request_satisfied') is False:
            for row in items:
                cat = str(row.get('category') or '')
                if cat in AdventurerS1DataAppealService._CHECKLIST_CATEGORY_GOLD | AdventurerS1DataAppealService._CHECKLIST_CATEGORY_ITEM:
                    if row.get('status') == 'pending' and row.get('resolution_basis') != 'repair_pending':
                        row['status'] = 'rejected'
                        if not row.get('detail'):
                            row['detail'] = '证据不足或规则不允许'
                elif cat in AdventurerS1DataAppealService._CHECKLIST_CATEGORY_QTY and row.get('status') == 'pending' and (row.get('resolution_basis') != 'repair_pending'):
                    row['status'] = 'rejected'
                    if not row.get('detail'):
                        row['detail'] = '未能写入 quantity 字段'
        if has_gold_grant and raw.get('core_request_satisfied') is not False:
            for row in items:
                if row.get('category') in AdventurerS1DataAppealService._CHECKLIST_CATEGORY_GOLD:
                    if row.get('status') == 'pending' and row.get('resolution_basis') != 'repair_pending':
                        row['status'] = 'done'
                        row.pop('detail', None)
        for index, row in enumerate(items[:8]):
            row['request_id'] = f'request_{index + 1}'
            row.setdefault('is_core', index == 0)
        return items[:8]

    @staticmethod
    def _render_player_reply_from_checklist(checklist: list[dict], *, core_request_satisfied, fallback: str | None) -> str | None:
        if not checklist:
            return fallback
        if core_request_satisfied is False:
            header = '本次已对部分条目做了数据整理，但您的核心诉求未完全满足：'
        elif core_request_satisfied is True:
            header = '经核对，处理结果如下：'
        else:
            header = '处理结果如下：'
        lines = [header]
        for item in checklist:
            status = str(item.get('status') or 'pending')
            prefix = AdventurerS1DataAppealService._CHECKLIST_STATUS_LABELS.get(status, '· 待核对')
            label = str(item.get('label') or '诉求').strip()
            detail = str(item.get('detail') or '').strip()
            line = f'{prefix}：{label}'
            if detail:
                line += f'（{detail}）'
            lines.append(line)
        return '\n'.join(lines)

    @staticmethod
    def _ensure_partial_pass_wording(text: str) -> str:
        cleaned = str(text or '').strip()
        if not cleaned:
            return '本次只完成了部分数据整理，您申诉的核心诉求未完全满足。'
        if '核心诉求未完全满足' in cleaned or '未完全满足' in cleaned:
            return cleaned
        if cleaned.startswith('本次已对部分条目'):
            return cleaned
        return f'本次只对部分条目做了整理，核心诉求未完全满足。\n\n{cleaned}'

    @staticmethod
    def finalize_player_visible_response(raw: dict, *, player_note: str | None=None, normalized_patch: dict | None=None, supplement_patch: dict | None=None, patch_raw: dict | None=None, patch_error: str | None=None) -> dict:
        """机械同步 request_checklist 与 audit，并生成玩家可见的逐条回复。"""
        result = dict(raw)
        if patch_error:
            result['core_request_satisfied'] = None
        checklist = AdventurerS1DataAppealService._normalize_request_checklist(result.get('request_checklist'), player_note=player_note)
        checklist = AdventurerS1DataAppealService._sync_request_checklist_with_audits(checklist, result, normalized_patch=normalized_patch, supplement_patch=supplement_patch, patch_raw=patch_raw, patch_error=patch_error)
        if checklist:
            result['request_checklist'] = checklist
        fallback = AdventurerS1DataAppealService._normalize_player_reject_reason(result.get('player_reject_reason') or result.get('reject_reason_for_player'))
        reply = AdventurerS1DataAppealService._render_player_reply_from_checklist(checklist, core_request_satisfied=result.get('core_request_satisfied'), fallback=fallback)
        if reply:
            if result.get('core_request_satisfied') is False or patch_error:
                reply = AdventurerS1DataAppealService._ensure_partial_pass_wording(reply)
            result['player_reject_reason'] = reply
            result['player_reply_summary'] = reply[:800]
        elif patch_error:
            fallback_reject = f'修复方案未能写入数据：{patch_error}' if patch_error else '修复方案未能写入数据，请核对后重提。'
            result['player_reject_reason'] = fallback_reject
            result['player_reply_summary'] = fallback_reject[:800]
        elif result.get('core_request_satisfied') is False and fallback:
            result['player_reject_reason'] = AdventurerS1DataAppealService._ensure_partial_pass_wording(fallback)
            result['player_reply_summary'] = result['player_reject_reason'][:800]
        return result

    @staticmethod
    def _finalize_ai_raw_response(raw: dict) -> dict:
        result = dict(raw)
        player_reason = AdventurerS1DataAppealService._normalize_player_reject_reason(result.get('player_reject_reason') or result.get('reject_reason_for_player'))
        if player_reason:
            result['player_reject_reason'] = player_reason
        else:
            result.pop('player_reject_reason', None)
            result.pop('reject_reason_for_player', None)
        lookup = result.get('lookup_results')
        if isinstance(lookup, list) and (not lookup):
            result.pop('lookup_results', None)
        return result

    @staticmethod
    def _parse_ai_raw_response(appeal: AdventurerS1DataAppeal) -> dict | None:
        text = getattr(appeal, 'ai_raw_response', None)
        if not text:
            return None
        import json
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _get_review_ai_decision(appeal: AdventurerS1DataAppeal) -> str | None:
        text = getattr(appeal, 'review_ai_raw_response', None)
        if not text:
            return None
        import json
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        decision = str(parsed.get('decision') or '').strip().lower()
        return decision or None

    @staticmethod
    def _get_review_verdicts(appeal: AdventurerS1DataAppeal) -> list[dict] | None:
        """从二审 AI 原始响应中提取 verdicts 数组；旧格式（无 verdicts）返回 None。"""
        text = getattr(appeal, 'review_ai_raw_response', None)
        if not text:
            return None
        import json
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        verdicts = parsed.get('verdicts')
        if not isinstance(verdicts, list) or not verdicts:
            return None
        return verdicts

    @staticmethod
    def _filter_patch_by_verdicts(patch: dict, verdicts: list[dict], *, source: str='primary') -> tuple[dict, list[str]]:
        """按 verdicts 过滤 proposed_patch，只保留 allow 的操作。

        返回 (filtered_patch, reject_reasons)。
        section 级 allow：该 section 全部保留。
        section + ids allow：只保留指定 id。
        无 verdict 覆盖的操作默认 reject。
        """
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions, filter_data_appeal_patch_by_operation_ids, iter_data_appeal_patch_operations
        operation_verdicts = [row for row in verdicts or [] if isinstance(row, dict) and row.get('operation_id')]
        if operation_verdicts:
            decisions = effective_data_appeal_operation_decisions(operation_verdicts)
            allowed_ids = {operation_id for operation_id, decision in decisions.items() if decision == 'allow' and operation_id.startswith(f'{source}:')}
            filtered = filter_data_appeal_patch_by_operation_ids(patch, source=source, allowed_operation_ids=allowed_ids)
            verdict_by_id = {str(row.get('operation_id') or '').strip(): row for row in operation_verdicts}
            reject_reasons = []
            for operation in iter_data_appeal_patch_operations(patch, source=source):
                operation_id = operation['operation_id']
                if operation_id in allowed_ids:
                    continue
                verdict = verdict_by_id.get(operation_id) or {}
                reason = str(verdict.get('reason') or '').strip()
                target = operation.get('target_name') or operation.get('target_id')
                label = f"{operation.get('section')} {target}" if target else str(operation.get('section') or '操作')
                reject_reasons.append(f"{label}：{reason or '没有精确通过结论，已跳过'}")
            return (filtered, reject_reasons)
        import copy
        filtered = {}
        reject_reasons = []

        def _section_label(section: str) -> str:
            return {'traits': '特质记录', 'equipment': '随身物品', 'warehouse': '仓库物品', 'skills': '技能记录', 'create_items': '补发物品', 'create_traits': '新增特质', 'create_skills': '新增技能', 'currency_adjustments': '补发金币', 'team_pool': '团队共享金币', 'tavern_subsidy': '酒馆补贴', 'reclaim_items': '追回误转移物品', 'trait_destructive_actions': '特质破坏性动作', 'skill_destructive_actions': '技能破坏性动作'}.get(section, '数据修复')

        def _has_section_operation(section: str, value) -> bool:
            if section in ('traits', 'equipment', 'warehouse', 'skills'):
                return isinstance(value, dict) and bool(value.get('update') or value.get('delete'))
            if section == 'team_pool':
                return isinstance(value, dict) and bool(value)
            if section == 'tavern_subsidy':
                return isinstance(value, (dict, list)) and bool(value)
            if section == 'reclaim_items':
                return isinstance(value, list) and bool(value)
            if isinstance(value, list):
                return bool(value)
            return bool(value)
        section_verdicts: dict[str, list[dict]] = {}
        for v in verdicts or []:
            sec = str(v.get('section') or '').strip().lower()
            if not sec:
                continue
            section_verdicts.setdefault(sec, []).append(v)
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            sec_data = patch.get(section)
            if not isinstance(sec_data, dict):
                continue
            vs = section_verdicts.get(section, [])
            if not vs:
                if sec_data.get('update') or sec_data.get('delete'):
                    reject_reasons.append(f'{_section_label(section)}：陪审团 2 号没有给出通过结论，已跳过')
                continue
            overall = [v for v in vs if not v.get('ids')]
            id_verdicts = [v for v in vs if v.get('ids')]
            if any((str(v.get('decision', '')).strip().lower() == 'allow' for v in overall)):
                filtered[section] = sec_data
                continue
            allowed_ids = set()
            for v in id_verdicts:
                if str(v.get('decision', '')).strip().lower() == 'allow':
                    for rid in v.get('ids') or []:
                        record_id = _normalize_appeal_patch_integer(rid, minimum=1)
                        if record_id is not None:
                            allowed_ids.add(record_id)
            filtered_updates = [u for u in sec_data.get('update') or [] if u.get('id') in allowed_ids]
            filtered_deletes = [d for d in sec_data.get('delete') or [] if (d.get('id') if isinstance(d, dict) else d) in allowed_ids]
            rejected_ids = []
            for u in sec_data.get('update') or []:
                uid = u.get('id')
                if uid in allowed_ids:
                    continue
                rejected_ids.append(uid)
            for d in sec_data.get('delete') or []:
                did = d.get('id') if isinstance(d, dict) else d
                if did not in allowed_ids:
                    rejected_ids.append(did)
            if rejected_ids:
                reject_reasons.append(f'{_section_label(section)}：部分记录未通过复核，已跳过')
            if filtered_updates or filtered_deletes:
                filtered[section] = {'update': filtered_updates, 'delete': filtered_deletes}
        for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            rows = patch.get(section)
            if not isinstance(rows, list) or not rows:
                continue
            allowed_ids: set[int] = set()
            for verdict in section_verdicts.get(section, []):
                if str(verdict.get('decision') or '').strip().lower() != 'allow':
                    continue
                ids = verdict.get('ids')
                if not isinstance(ids, list) or not ids:
                    continue
                for raw_id in ids:
                    record_id = _normalize_appeal_patch_integer(raw_id, minimum=1)
                    if record_id is not None:
                        allowed_ids.add(record_id)
            allowed_rows = [row for row in rows if isinstance(row, dict) and row.get('source_id') in allowed_ids]
            if len(allowed_rows) != len(rows):
                reject_reasons.append(f'{_section_label(section)}：部分记录没有精确通过结论，已跳过')
            if allowed_rows:
                filtered[section] = allowed_rows
        for section in ('create_items', 'create_traits', 'create_skills', 'currency_adjustments', 'team_pool', 'tavern_subsidy', 'reclaim_items'):
            if section not in patch:
                continue
            if not _has_section_operation(section, patch.get(section)):
                continue
            vs = section_verdicts.get(section, [])
            if not vs:
                reject_reasons.append(f'{_section_label(section)}：陪审团 2 号没有给出通过结论，已跳过')
                continue
            allow = any((str(v.get('decision', '')).strip().lower() == 'allow' for v in vs))
            if allow:
                filtered[section] = patch[section]
            else:
                reasons = [str(v.get('reason') or '') for v in vs if str(v.get('decision', '')).strip().lower() == 'reject']
                reject_reasons.append(f"{_section_label(section)}：{'；'.join((r for r in reasons if r)) or '复核未通过'}")
        return (filtered, reject_reasons)

    @staticmethod
    def _get_review_player_reason(appeal: AdventurerS1DataAppeal) -> str | None:
        text = getattr(appeal, 'review_ai_raw_response', None)
        if not text:
            return None
        import json
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        if not isinstance(parsed, dict):
            return None
        reason = str(parsed.get('player_reason') or parsed.get('reason') or '').strip()
        return reason or None

    @staticmethod
    def _set_review_execution_note(appeal: AdventurerS1DataAppeal, note: str) -> None:
        """记录后端实际执行结果；玩家展示优先于 AI 的执行前承诺文案。"""
        import json
        text = getattr(appeal, 'review_ai_raw_response', None)
        try:
            parsed = json.loads(text) if text else {}
        except (TypeError, ValueError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        new_note = str(note or '').strip()
        previous = str(parsed.get('execution_note') or '').strip()
        if previous and new_note and (new_note not in previous):
            new_note = f'{previous}\n{new_note}'
        parsed['execution_note'] = new_note[:800]
        appeal.review_ai_raw_response = json_dumps_safe(parsed)

    @staticmethod
    def _appeal_request_results(appeal: AdventurerS1DataAppeal) -> list[dict]:
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        rows = review_raw.get('request_results')
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    @staticmethod
    def _preserve_completed_review_retry_state(previous: dict | None, candidate: dict | None) -> dict | None:
        """Freeze already landed sibling requests across an independent AI re-review.

        The reviewer may reconsider only unresolved requests.  Proven receipts and
        completed request verdicts are backend facts: a later model response cannot
        downgrade them or attach a new additive operation to those requests.
        """
        if not isinstance(candidate, dict) or not isinstance(previous, dict):
            return candidate
        merged = copy.deepcopy(candidate)
        previous_receipts = [copy.deepcopy(row) for row in previous.get('operation_receipts') or [] if isinstance(row, dict) and row.get('decision') == 'allow' and (row.get('outcome') in ('executed', 'already_satisfied')) and str(row.get('operation_id') or '').strip()]
        successful_operation_ids = {str(row.get('operation_id') or '').strip() for row in previous_receipts}
        previous_terminal_results = [copy.deepcopy(row) for row in previous.get('request_results') or [] if isinstance(row, dict) and str(row.get('status') or '').strip().lower() in ('done', 'partial', 'rejected') and str(row.get('request_id') or '').strip()]
        completed_request_ids = {str(row.get('request_id') or '').strip() for row in previous_terminal_results}
        previous_verdict_by_id = {str(row.get('operation_id') or '').strip(): copy.deepcopy(row) for row in previous.get('verdicts') or [] if isinstance(row, dict) and str(row.get('operation_id') or '').strip()}
        dropped_candidate_operation_ids: set[str] = set()
        merged_verdicts: list[dict] = []
        for row in merged.get('verdicts') or []:
            if not isinstance(row, dict):
                continue
            operation_id = str(row.get('operation_id') or '').strip()
            request_ids = {str(request_id).strip() for request_id in row.get('request_ids') or [] if str(request_id).strip()}
            if operation_id in successful_operation_ids:
                continue
            if request_ids & completed_request_ids:
                if operation_id:
                    dropped_candidate_operation_ids.add(operation_id)
                continue
            merged_verdicts.append(copy.deepcopy(row))
        for operation_id in sorted(successful_operation_ids):
            previous_verdict = previous_verdict_by_id.get(operation_id)
            if previous_verdict:
                merged_verdicts.append(previous_verdict)
        if isinstance(merged.get('verdicts'), list) or merged_verdicts:
            merged['verdicts'] = merged_verdicts
        previous_manifest_by_id = {str(row.get('operation_id') or '').strip(): copy.deepcopy(row) for row in previous.get('_backend_operation_manifest') or [] if isinstance(row, dict) and str(row.get('operation_id') or '').strip()}
        candidate_manifest_by_id = {str(row.get('operation_id') or '').strip(): copy.deepcopy(row) for row in merged.get('_backend_operation_manifest') or [] if isinstance(row, dict) and str(row.get('operation_id') or '').strip()}
        merged_manifest = [row for operation_id, row in candidate_manifest_by_id.items() if operation_id not in successful_operation_ids and operation_id not in dropped_candidate_operation_ids]
        for operation_id in sorted(successful_operation_ids):
            protected_manifest = previous_manifest_by_id.get(operation_id) or candidate_manifest_by_id.get(operation_id)
            if protected_manifest:
                merged_manifest.append(protected_manifest)
        if isinstance(merged.get('_backend_operation_manifest'), list) or merged_manifest:
            merged['_backend_operation_manifest'] = merged_manifest
        previous_request_verdict_by_id = {str(row.get('request_id') or '').strip(): copy.deepcopy(row) for row in previous.get('request_verdicts') or [] if isinstance(row, dict) and str(row.get('request_id') or '').strip()}
        merged_request_verdicts = [copy.deepcopy(row) for row in merged.get('request_verdicts') or [] if isinstance(row, dict) and str(row.get('request_id') or '').strip() not in completed_request_ids]
        for request_id in sorted(completed_request_ids):
            previous_verdict = previous_request_verdict_by_id.get(request_id)
            if previous_verdict:
                merged_request_verdicts.append(previous_verdict)
        if isinstance(merged.get('request_verdicts'), list) or merged_request_verdicts:
            merged['request_verdicts'] = merged_request_verdicts
        candidate_receipts = [copy.deepcopy(row) for row in merged.get('operation_receipts') or [] if isinstance(row, dict) and str(row.get('operation_id') or '').strip() not in successful_operation_ids]
        if previous_receipts or isinstance(merged.get('operation_receipts'), list):
            merged['operation_receipts'] = candidate_receipts + previous_receipts
        candidate_results = [copy.deepcopy(row) for row in merged.get('request_results') or [] if isinstance(row, dict) and str(row.get('request_id') or '').strip() not in completed_request_ids]
        if previous_terminal_results or isinstance(merged.get('request_results'), list):
            merged['request_results'] = candidate_results + previous_terminal_results
        for retry_state_key in ('_backend_auto_retry_attempts', '_backend_zero_token_retry_attempts'):
            retry_attempts = previous.get(retry_state_key)
            if isinstance(retry_attempts, dict):
                merged[retry_state_key] = copy.deepcopy(retry_attempts)
        return merged

    @staticmethod
    def _mark_appeal_resolution_pending(appeal: AdventurerS1DataAppeal, reason: str, *, stage: str, counts_toward_model_retry: bool | None=None) -> None:
        """Persist retry state; only proven token use spends model attempts."""
        pending_stage = stage if stage in ('primary_ai', 'review_ai', 'execution') else 'execution'
        if pending_stage == 'execution' and (not AdventurerS1DataAppealService._appeal_frozen_review_ready_for_execution(appeal)):
            logger.warning('数据申诉拒绝进入空执行重试，改回二审 appeal=%s', appeal.id)
            pending_stage = 'review_ai'
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        persisted_retry_flag = review_raw.pop('_backend_counts_toward_model_retry', None)
        if counts_toward_model_retry is None and pending_stage == 'review_ai' and (type(persisted_retry_flag) is bool):
            counts_toward_model_retry = persisted_retry_flag
        if counts_toward_model_retry is None:
            counts_toward_model_retry = True
        primary_raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
        checklist = primary_raw.get('request_checklist')
        checklist = checklist if isinstance(checklist, list) else []
        request_verdict_by_id = {str(row.get('request_id') or '').strip(): row for row in review_raw.get('request_verdicts') or [] if isinstance(row, dict) and str(row.get('request_id') or '').strip()}
        existing_results = AdventurerS1DataAppealService._appeal_request_results(appeal)
        results_by_id = {str(row.get('request_id') or '').strip(): row for row in existing_results if str(row.get('request_id') or '').strip()}
        request_results: list[dict] = []
        force_pending = pending_stage == 'primary_ai'
        for index, row in enumerate(checklist[:8]):
            if not isinstance(row, dict):
                continue
            request_id = str(row.get('request_id') or f'request_{index + 1}').strip()
            if not force_pending and request_id in results_by_id:
                request_results.append(results_by_id[request_id])
                continue
            verdict = request_verdict_by_id.get(request_id) or {}
            resolution_basis = str(verdict.get('resolution_basis') or row.get('resolution_basis') or '').strip()
            verdict_decision = str(verdict.get('decision') or '').strip().lower()
            if not force_pending and verdict_decision == 'reject' and (resolution_basis != 'repair_pending'):
                if resolution_basis == 'already_satisfied_same_case':
                    status = 'done'
                    outcome = 'already_satisfied'
                else:
                    status = 'rejected'
                    outcome = 'rejected'
            else:
                status = 'pending'
                outcome = {'primary_ai': 'primary_review_unavailable', 'review_ai': 'review_unavailable', 'execution': 'execution_pending'}[pending_stage]
                resolution_basis = 'repair_pending'
            result = {'request_id': request_id, 'label': str(row.get('label') or '申诉诉求').strip()[:120], 'category': str(row.get('category') or 'other').strip()[:20], 'is_core': row.get('is_core') is True, 'status': status, 'outcome': outcome, 'operation_ids': [str(operation_id) for operation_id in (results_by_id.get(request_id) or {}).get('operation_ids') or [] if str(operation_id).strip()], **({'resolution_basis': resolution_basis} if resolution_basis else {}), 'detail': str(reason or '该项仍待处理。').strip()[:160]}
            request_results.append(result)
        if request_results:
            review_raw['request_results'] = request_results
        review_raw['resolution_state'] = 'pending'
        review_raw['pending_stage'] = pending_stage
        raw_attempts = review_raw.get('_backend_auto_retry_attempts')
        raw_attempts = raw_attempts if isinstance(raw_attempts, dict) else {}
        if pending_stage == 'execution' and review_raw.get('_backend_execution_retry_policy_version') != _DATA_APPEAL_EXECUTION_RETRY_POLICY_VERSION:
            raw_attempts = copy.deepcopy(raw_attempts)
            raw_attempts.pop('execution', None)
            review_raw['_backend_execution_retry_policy_version'] = _DATA_APPEAL_EXECUTION_RETRY_POLICY_VERSION
        retry_attempts = {retry_stage: int(raw_attempts.get(retry_stage) or 0) for retry_stage in ('primary_ai', 'review_ai', 'execution') if type(raw_attempts.get(retry_stage)) is int and int(raw_attempts.get(retry_stage) or 0) > 0}
        raw_zero_token_attempts = review_raw.get('_backend_zero_token_retry_attempts')
        raw_zero_token_attempts = raw_zero_token_attempts if isinstance(raw_zero_token_attempts, dict) else {}
        zero_token_attempts = {retry_stage: int(raw_zero_token_attempts.get(retry_stage) or 0) for retry_stage in ('primary_ai', 'review_ai') if type(raw_zero_token_attempts.get(retry_stage)) is int and int(raw_zero_token_attempts.get(retry_stage) or 0) > 0}
        zero_token_model_failure = pending_stage in ('primary_ai', 'review_ai') and counts_toward_model_retry is False
        zero_token_attempt = zero_token_attempts.get(pending_stage, 0)
        if zero_token_model_failure:
            retry_attempt = retry_attempts.get(pending_stage, 0)
            zero_token_attempt += 1
            zero_token_attempts[pending_stage] = zero_token_attempt
            backoff_attempt = zero_token_attempt
            logger.info('数据申诉 AI 零 token 上游故障不消耗模型重试预算 appeal=%s stage=%s infrastructure_attempts=%s model_attempts=%s', appeal.id, pending_stage, zero_token_attempt, retry_attempt)
        else:
            retry_attempt = retry_attempts.get(pending_stage, 0) + 1
            retry_attempts[pending_stage] = retry_attempt
            zero_token_attempts.pop(pending_stage, None)
            backoff_attempt = retry_attempt
        if retry_attempts:
            review_raw['_backend_auto_retry_attempts'] = retry_attempts
        else:
            review_raw.pop('_backend_auto_retry_attempts', None)
        if zero_token_attempts:
            review_raw['_backend_zero_token_retry_attempts'] = zero_token_attempts
        else:
            review_raw.pop('_backend_zero_token_retry_attempts', None)
        if pending_stage == 'primary_ai':
            review_raw.pop('_backend_resume_stage', None)
        else:
            review_raw['_backend_resume_stage'] = pending_stage
        max_stage_retries = AdventurerS1DataAppealService._appeal_auto_retry_max_execution_retries() if pending_stage == 'execution' else AdventurerS1DataAppealService._appeal_auto_retry_max_model_retries()
        auto_retry_exhausted = pending_stage in ('primary_ai', 'review_ai', 'execution') and (retry_attempt > max_stage_retries or (zero_token_model_failure and zero_token_attempt > AdventurerS1DataAppealService._appeal_auto_retry_max_infrastructure_retries()))
        if auto_retry_exhausted:
            review_raw['_backend_auto_retry_exhausted'] = pending_stage
            review_raw.pop('_backend_auto_retry_after', None)
            if zero_token_model_failure:
                logger.error('数据申诉 AI 零 token 上游故障退避耗尽 appeal=%s stage=%s infrastructure_attempts=%s model_attempts=%s', appeal.id, pending_stage, zero_token_attempt, retry_attempt)
            else:
                logger.error('数据申诉 AI 自动重试预算耗尽 appeal=%s stage=%s attempts=%s', appeal.id, pending_stage, retry_attempt)
            pending_note = '最终写入已达自动重试上限；未完成项目没有新增修改。' if pending_stage == 'execution' else '本阶段已达自动重试上限，未形成审核结论；本轮没有据此修改数据。'
            for request_result in request_results:
                if request_result.get('status') == 'pending':
                    request_result['detail'] = pending_note[:160]
        else:
            review_raw.pop('_backend_auto_retry_exhausted', None)
            review_raw['_backend_auto_retry_after'] = to_utc_iso(_portable_utc_now() + timedelta(minutes=AdventurerS1DataAppealService._appeal_auto_retry_delay_minutes(backoff_attempt)))
            pending_note = str(reason or '该项仍待处理。').strip()
        previous_note = str(review_raw.get('execution_note') or '').strip()
        if previous_note and pending_note not in previous_note:
            pending_note = f'{pending_note}\n{previous_note}' if auto_retry_exhausted else f'{previous_note}\n{pending_note}'
        review_raw['execution_note'] = pending_note[:800]
        appeal.review_ai_raw_response = json_dumps_safe(review_raw)
        appeal.status = 'processing'
        appeal.ai_status = 'pending'
        if pending_stage in ('primary_ai', 'review_ai'):
            appeal.review_ai_status = 'pending'
        if pending_stage == 'primary_ai':
            appeal.review_ai_model = None
            appeal.review_supplement_patch = None
        appeal.reviewed_by = None
        appeal.reviewed_at = None
        appeal.reject_reason = None
        AdventurerS1DataAppealService._clear_appeal_ai_lease_fields(appeal)
        if auto_retry_exhausted:
            AdventurerS1DataAppealService._finalize_exhausted_appeal_auto_retry(appeal)
        core_results = [row for row in request_results if row.get('is_core')]
        if core_results:
            if all((row.get('status') == 'done' for row in core_results)):
                appeal.core_request_satisfied = True
            elif any((row.get('status') == 'pending' for row in core_results)):
                appeal.core_request_satisfied = None
            else:
                appeal.core_request_satisfied = False
        else:
            appeal.core_request_satisfied = None

    @staticmethod
    def _appeal_update_fields_match(section: str, payload: dict, current: dict | None) -> tuple[bool, bool]:
        """严格比较所有允许持久化字段，禁止投影漏字段被静默忽略。"""
        field_key = {'traits': 'trait', 'equipment': 'equipment', 'warehouse': 'warehouse', 'skills': 'skill'}.get(str(section or ''))
        allowed_fields = _PATCH_UPDATE_FIELDS.get(field_key, frozenset())
        expected = {key: value for key, value in (payload or {}).items() if key in allowed_fields}
        if not expected:
            return (False, True)
        if not isinstance(current, dict) or any((key not in current for key in expected)):
            return (True, False)
        return (True, all((current.get(key) == value for key, value in expected.items())))

    @staticmethod
    def _appeal_audit_matches_operation(operation: dict, audit: AdventurerS1DataAppealAuditLog, *, payload: dict | None=None, current_records: dict | None=None) -> bool:
        """Match one persisted audit row to one deterministic operation receipt."""
        section = str(operation.get('section') or '')
        action = str(operation.get('action') or '')
        target_id = operation.get('target_id')
        target_name = str(operation.get('target_name') or '').strip()
        audit_match = operation.get('audit_match') if isinstance(operation.get('audit_match'), dict) else {}
        after = audit.after_json if isinstance(audit.after_json, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        current_records = current_records if isinstance(current_records, dict) else {}
        target_type = {'traits': 'trait', 'equipment': 'equipment', 'warehouse': 'warehouse', 'skills': 'skill'}.get(section)
        if target_type:
            if audit.target_type != target_type or audit.target_id != target_id:
                return False
            if action == 'delete':
                return audit.op_type == 'delete'
            if action == 'clear':
                if audit.op_type != 'update':
                    return False
                current = next((row for row in current_records.get(section) or [] if isinstance(row, dict) and row.get('id') == target_id), None)
                return bool(current) and all((current.get(field) in (None, '') for field in payload.get('fields') or []))
            if action == 'update':
                if audit.op_type != 'update':
                    return False
                current = next((row for row in current_records.get(section) or [] if isinstance(row, dict) and row.get('id') == target_id), None)
                has_fields, fields_match = AdventurerS1DataAppealService._appeal_update_fields_match(section, payload, current)
                trigger_required = payload.get('trigger_recovery') is True
                trigger_satisfied = not trigger_required or bool(current and current.get('recovery_started_at') and current.get('recovery_ends_at'))
                return (has_fields or trigger_required) and fields_match and trigger_satisfied
        if section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            destructive_type = 'trait' if section.startswith('trait_') else 'skill'
            return audit.op_type == 'delete' and audit.target_type == destructive_type and (audit.target_id == target_id)
        if section == 'create_items':
            return audit.op_type == 'create_item' and str(after.get('name') or '').strip() == target_name and (audit_match.get('quantity') is None or after.get('quantity') == audit_match.get('quantity')) and (not audit_match.get('destination') or after.get('requested_destination') == audit_match.get('destination'))
        if section == 'create_traits':
            return audit.op_type == 'create_trait' and str(after.get('name') or '').strip() == target_name
        if section == 'create_skills':
            return audit.op_type == 'create_skill' and str(after.get('name') or '').strip() == target_name
        if section == 'currency_adjustments':
            return audit.op_type == 'currency' and (not after.get('team_pool')) and (not after.get('tavern_subsidy')) and (audit_match.get('currency_code') is None or after.get('currency_code') == audit_match.get('currency_code')) and (audit_match.get('amount') is None or after.get('amount') == audit_match.get('amount'))
        if section == 'team_pool':
            return audit.op_type == 'team_pool' and (audit_match.get('total_amount') is None or after.get('total_amount') == audit_match.get('total_amount'))
        if section == 'unassigned_items':
            return audit.op_type == 'unassigned_item' and after.get('registration_hash') == audit_match.get('registration_hash')
        if section == 'reclaim_items':
            return audit.op_type in ('reclaim_item', 'transfer_asset') and (audit.op_type != 'transfer_asset' or (after.get('asset_kind') == audit_match.get('asset_kind') and after.get('asset_id') == audit_match.get('asset_id'))) and (str(after.get('name') or '').strip() == target_name) and (audit_match.get('from_adventurer_id') is None or after.get('from_adventurer_id') == audit_match.get('from_adventurer_id'))
        return False

    @staticmethod
    def _appeal_operation_already_satisfied(appeal: AdventurerS1DataAppeal, operation: dict, payload: dict | None, current_records: dict) -> bool:
        """Recognize deterministic no-op success without interpreting AI prose."""
        section = str(operation.get('section') or '')
        action = str(operation.get('action') or '')
        target_id = operation.get('target_id')
        payload = payload if isinstance(payload, dict) else {}
        if section in ('traits', 'equipment', 'warehouse', 'skills'):
            current = next((row for row in current_records.get(section) or [] if isinstance(row, dict) and row.get('id') == target_id), None)
            if action == 'delete':
                return current is None
            if action == 'clear':
                return bool(current) and all((current.get(field) in (None, '') for field in payload.get('fields') or []))
            if action == 'update' and current:
                has_fields, fields_match = AdventurerS1DataAppealService._appeal_update_fields_match(section, payload, current)
                trigger_required = payload.get('trigger_recovery') is True
                trigger_satisfied = not trigger_required or bool(current.get('recovery_started_at') and current.get('recovery_ends_at'))
                return (has_fields or trigger_required) and fields_match and trigger_satisfied
        if section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            source_section = 'traits' if section.startswith('trait_') else 'skills'
            source_absent = not any((isinstance(row, dict) and row.get('id') == target_id for row in current_records.get(source_section) or []))
            if not source_absent:
                return False
            mode = str(payload.get('mode') or '').strip()
            target_kind = destructive_action_target_kind(mode)
            if target_kind is None:
                return mode == 'delete_only'
            target = payload.get('target')
            if not isinstance(target, dict):
                return False
            target_section = 'traits' if target_kind == 'trait' else 'skills'
            target_id_value = _normalize_appeal_patch_integer(target.get('id'), minimum=1)
            target_name = str(target.get('name') or '').strip()
            candidates = [row for row in current_records.get(target_section) or [] if isinstance(row, dict) and (row.get('id') == target_id_value if target_id_value is not None else str(row.get('name') or '').strip() == target_name)]
            persisted_fields = {'name', 'description', 'is_archived', 'recovery_policy', 'recovery_stage', 'recovery_required', 'recovery_progress', 'recovery_hours', 'recovered_desc'} if target_kind == 'trait' else {'name', 'description', 'equipped', 'recovery_minutes'}
            comparable = {key: value for key, value in target.items() if key in persisted_fields}
            return bool(comparable) and any((all((candidate.get(key) == value for key, value in comparable.items())) for candidate in candidates))
        if section == 'create_items':
            return False
        if section in ('create_traits', 'create_skills'):
            name = str(payload.get('name') or operation.get('target_name') or '').strip()
            candidate_section = 'traits' if section == 'create_traits' else 'skills'
            if not name or any((isinstance(row, dict) and str(row.get('name') or '').strip() == name for row in (appeal.snapshot_before or {}).get(candidate_section) or [])):
                return False
            persisted_fields = {'name', 'description', 'is_negative', 'recovery_policy', 'recovery_stage', 'recovery_required', 'recovery_progress'} if section == 'create_traits' else {'name', 'description', 'recovery_minutes'}
            comparable = {key: value for key, value in payload.items() if key in persisted_fields}
            return bool(comparable) and any((isinstance(row, dict) and str(row.get('name') or '').strip() == name and all((row.get(key) == value for key, value in comparable.items())) for row in current_records.get(candidate_section) or []))
        if section == 'team_pool':
            return bool(appeal.expedition_log_id and AdventurerS1DataAppealService._is_team_pool_settled(appeal.expedition_log_id))
        if section == 'unassigned_items':
            from plugins.adventurer_s1.services.s1_team_loot_service import S1TeamLootService
            return any((row['status'] != 'cancelled' and row['source_event_index'] == payload.get('source_event_index') and (row['name'] == payload.get('name')) and (row['item'] == payload.get('item')) for row in S1TeamLootService.evidence_status(appeal.expedition_log_id)))
        if section == 'reclaim_items':
            name = str(payload.get('name') or operation.get('target_name') or '').strip()
            try:
                source_id = int(payload.get('from_adventurer_id'))
            except (TypeError, ValueError):
                return False
            return bool(name) and AdventurerS1DataAppealService._is_reclaim_item_applied(appeal.id, name, source_id)
        return False

    @staticmethod
    def _appeal_record_patch_already_satisfied(appeal: AdventurerS1DataAppeal, patch: dict | None, current_records: dict) -> bool:
        """零审计兜底：仅当整份普通改删 patch 已机械满足时视为成功。"""
        if not appeal or not isinstance(patch, dict) or (not isinstance(current_records, dict)):
            return False
        if any((patch.get(key) for key in _DATA_APPEAL_PRIVILEGED_PATCH_KEYS)):
            return False
        operations: list[tuple[dict, dict]] = []
        for section in ('traits', 'equipment', 'warehouse', 'skills'):
            section_patch = patch.get(section) or {}
            if not isinstance(section_patch, dict):
                return False
            for update in section_patch.get('update') or []:
                if not isinstance(update, dict) or update.get('id') is None:
                    return False
                operations.append(({'section': section, 'action': 'update', 'target_id': update.get('id')}, update))
            for delete in section_patch.get('delete') or []:
                target_id = delete.get('id') if isinstance(delete, dict) else delete
                if target_id is None:
                    return False
                operations.append(({'section': section, 'action': 'delete', 'target_id': target_id}, {'id': target_id}))
        return bool(operations) and all((AdventurerS1DataAppealService._appeal_operation_already_satisfied(appeal, operation, payload, current_records) for operation, payload in operations))

    @staticmethod
    def _project_player_request_result_detail(*, status: str, outcome: str, resolution_basis: str, terminal_error_codes: set[str] | None=None) -> str:
        """Project structured outcomes without relaying unverified AI promises."""
        from plugins.adventurer_s1.services.s1_appeal_departure_guard import DEPARTURE_ASSET_LOCKED_CODE
        if status in ('partial', 'rejected') and outcome in ('stale_target', 'partial_stale_target') and (DEPARTURE_ASSET_LOCKED_CODE in (terminal_error_codes or set())):
            prefix = '其它独立修改已处理；' if status == 'partial' else ''
            return prefix + '涉及出发携带资产的修改未执行，请在探险结束后在原申诉中继续处理。' + ('其它未执行原因见逐项回执。' if len(terminal_error_codes) > 1 else '')
        if status == 'done':
            if outcome == 'executed':
                return '该项已按本轮可核对的落账回执完成。'
            if outcome == 'already_satisfied':
                return '同一事项已有可核对的完成记录，无需重复修改。'
            return _PLAYER_REQUEST_RESOLUTION_DETAILS.get(resolution_basis, '该项已有可核对的完成记录。')
        if status == 'pending':
            if outcome == 'conflict':
                return '该项已获得可执行结论，但最终写入条件发生冲突，仍保留待处理。'
            if outcome == 'not_applied':
                return '该项已获得可执行结论，但最终写入没有产生可核对的落账回执，仍保留待处理。'
            if outcome == 'partial_pending':
                return '该项仅有部分写入完成，其余部分仍保留待处理。'
            return _PLAYER_REQUEST_RESOLUTION_DETAILS.get(resolution_basis, '该项本轮尚未形成可安全执行的完整结果，仍保留待处理。')
        if status == 'partial':
            if outcome == 'partial_stale_target':
                return '其它独立修改已处理；该项冻结记录已发生后续变动，不能再按旧记录安全修改。'
            return _PLAYER_REQUEST_RESOLUTION_DETAILS.get(resolution_basis, '该项仅有部分修复完成，其余部分未获得可执行结论。')
        if status == 'rejected':
            if outcome == 'stale_target':
                return '申诉提交后该记录已发生后续变动，当前不在冻结位置，不能按旧记录安全修改。'
            return _PLAYER_REQUEST_RESOLUTION_DETAILS.get(resolution_basis, '现有权威记录不足以支持本轮修改。')
        return ''

    @staticmethod
    def _store_operation_receipts_and_request_results(appeal: AdventurerS1DataAppeal, *, execution_error: str | None=None, operation_errors: dict[str, str] | None=None) -> None:
        """Persist actual per-operation receipts and derive independent request outcomes."""
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        if review_raw.get('verdict_contract_version') != 2:
            return
        verdicts = review_raw.get('verdicts')
        manifest = review_raw.get('_backend_operation_manifest')
        if not isinstance(verdicts, list) or not isinstance(manifest, list):
            return
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import effective_data_appeal_operation_decisions, iter_data_appeal_clear_operations, iter_data_appeal_patch_operations
        payload_by_id = {operation['operation_id']: operation.get('_payload') for operation in iter_data_appeal_patch_operations(appeal.proposed_patch, source='primary')}
        authored = review_raw.get('_backend_authorized_review_operation_patches')
        authored = authored if isinstance(authored, dict) else {}
        authorized_operations = review_raw.get('_backend_authorized_review_operations')
        if isinstance(authorized_operations, list):
            for operation in authorized_operations:
                if not isinstance(operation, dict):
                    continue
                operation_id = str(operation.get('operation_id') or '').strip()
                if operation_id:
                    payload_by_id[operation_id] = operation.get('payload')
        else:
            for source in ('supplement', 'override'):
                for operation in iter_data_appeal_patch_operations(authored.get(source), source=source):
                    payload_by_id[operation['operation_id']] = operation.get('_payload')
            for operation in iter_data_appeal_clear_operations(authored.get('clear_fields')):
                payload_by_id[operation['operation_id']] = operation.get('_payload')
        decisions = effective_data_appeal_operation_decisions(verdicts)
        operation_errors = operation_errors or {}
        terminal_operation_errors = review_raw.get(_DATA_APPEAL_TERMINAL_OPERATION_ERRORS_KEY)
        terminal_operation_errors = terminal_operation_errors if isinstance(terminal_operation_errors, dict) else {}
        verdict_by_id = {str(row.get('operation_id') or '').strip(): row for row in verdicts if isinstance(row, dict)}
        request_verdict_by_id = {str(row.get('request_id') or '').strip(): row for row in review_raw.get('request_verdicts') or [] if isinstance(row, dict) and str(row.get('request_id') or '').strip()}
        previous_successful_receipts = {str(row.get('operation_id') or '').strip(): str(row.get('outcome') or '').strip() for row in review_raw.get('operation_receipts') or [] if isinstance(row, dict) and row.get('decision') == 'allow' and (row.get('outcome') in ('executed', 'already_satisfied')) and str(row.get('operation_id') or '').strip()}
        audit_query = db_manager.query(AdventurerS1DataAppealAuditLog).filter_by(appeal_id=appeal.id)
        audit_seq_start = review_raw.get('_backend_execution_audit_seq_start')
        if type(audit_seq_start) is int and audit_seq_start >= 0:
            audit_query = audit_query.filter(AdventurerS1DataAppealAuditLog.op_seq >= audit_seq_start)
        audits = audit_query.order_by(AdventurerS1DataAppealAuditLog.op_seq.asc()).all()
        current_records = AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True)
        used_audit_ids: set[int] = set()
        receipts: list[dict] = []
        for operation in manifest:
            if not isinstance(operation, dict):
                continue
            operation_id = str(operation.get('operation_id') or '').strip()
            if not operation_id:
                continue
            verdict = verdict_by_id.get(operation_id) or {}
            effective_decision = decisions.get(operation_id, 'reject')
            if operation_id in previous_successful_receipts:
                effective_decision = 'allow'
                outcome = previous_successful_receipts[operation_id]
            elif effective_decision != 'allow':
                outcome = 'rejected'
            else:
                expected_payload = payload_by_id.get(operation_id)
                matched_audit = next((audit for audit in audits if audit.id not in used_audit_ids and AdventurerS1DataAppealService._appeal_audit_matches_operation(operation, audit, payload=expected_payload, current_records=current_records)), None)
                if matched_audit is not None:
                    if matched_audit.id is not None:
                        used_audit_ids.add(matched_audit.id)
                    outcome = 'executed'
                elif operation_id in terminal_operation_errors:
                    outcome = 'stale_target'
                elif operation_id in operation_errors or execution_error:
                    outcome = 'conflict'
                elif AdventurerS1DataAppealService._appeal_operation_already_satisfied(appeal, operation, expected_payload, current_records):
                    outcome = 'already_satisfied'
                else:
                    outcome = 'not_applied'
            receipts.append({'operation_id': operation_id, 'request_ids': [request_id for request_id in verdict.get('request_ids') or [] if isinstance(request_id, str)], 'decision': effective_decision, 'outcome': outcome, **({'atomic_group_id': verdict.get('atomic_group_id')} if verdict.get('atomic_group_id') else {}), **({'reason': str(verdict.get('reason') or '').strip()[:300]} if verdict.get('reason') else {}), **({'error': str((terminal_operation_errors.get(operation_id) or {}).get('detail') or operation_errors.get(operation_id) or execution_error)[:300]} if outcome in ('conflict', 'stale_target') else {})})
        primary_raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
        checklist = primary_raw.get('request_checklist')
        checklist = checklist if isinstance(checklist, list) else []
        request_results: list[dict] = []
        for index, row in enumerate(checklist[:8]):
            if not isinstance(row, dict):
                continue
            request_id = str(row.get('request_id') or f'request_{index + 1}').strip()
            request_verdict = request_verdict_by_id.get(request_id) or {}
            resolution_basis = str(request_verdict.get('resolution_basis') or row.get('resolution_basis') or '').strip()
            linked = [receipt for receipt in receipts if request_id in receipt.get('request_ids', [])]
            if linked:
                successful = [receipt for receipt in linked if receipt['outcome'] in ('executed', 'already_satisfied')]
                failed = [receipt for receipt in linked if receipt not in successful]
                technical_failed = [receipt for receipt in failed if receipt['outcome'] in ('conflict', 'not_applied')]
                terminal_failed = [receipt for receipt in failed if receipt['outcome'] == 'stale_target']
                if successful and technical_failed:
                    status = 'pending'
                    outcome = 'partial_pending'
                elif successful and terminal_failed:
                    status = 'partial'
                    outcome = 'partial_stale_target'
                elif successful and failed:
                    status = 'partial'
                    outcome = 'partially_executed'
                elif successful:
                    status = 'done'
                    outcome = 'already_satisfied' if all((receipt['outcome'] == 'already_satisfied' for receipt in successful)) else 'executed'
                else:
                    technical_outcomes = {receipt['outcome'] for receipt in linked if receipt['outcome'] in ('conflict', 'not_applied')}
                    if terminal_failed:
                        status = 'rejected'
                        outcome = 'stale_target'
                    elif resolution_basis == 'repair_pending':
                        status = 'pending'
                        outcome = 'repair_pending'
                    elif technical_outcomes:
                        status = 'pending'
                        outcome = 'conflict' if 'conflict' in technical_outcomes else 'not_applied'
                    elif resolution_basis == 'already_satisfied_same_case':
                        status = 'done'
                        outcome = 'already_satisfied'
                    else:
                        status = 'rejected'
                        outcome = 'rejected'
            else:
                request_decision = str(request_verdict.get('decision') or '').strip().lower()
                if request_decision == 'pending' or resolution_basis == 'repair_pending':
                    status = 'pending'
                    outcome = 'repair_pending'
                elif request_decision in ('reject', 'partial'):
                    if resolution_basis == 'already_satisfied_same_case':
                        status = 'done'
                        outcome = 'already_satisfied'
                    else:
                        status = 'rejected'
                        outcome = 'rejected'
                else:
                    status = str(row.get('status') or 'pending').strip().lower()
                    if status not in ('done', 'partial', 'rejected', 'pending'):
                        status = 'pending'
                    outcome = {'done': 'already_satisfied', 'partial': 'partially_executed', 'rejected': 'rejected', 'pending': 'review_unavailable'}[status]
            result = {'request_id': request_id, 'label': str(row.get('label') or '申诉诉求').strip()[:120], 'category': str(row.get('category') or 'other').strip()[:20], 'is_core': row.get('is_core') is True, 'status': status, 'outcome': outcome, 'operation_ids': [receipt['operation_id'] for receipt in linked], **({'resolution_basis': resolution_basis} if resolution_basis else {})}
            detail = AdventurerS1DataAppealService._project_player_request_result_detail(status=status, outcome=outcome, resolution_basis=resolution_basis, terminal_error_codes={str((terminal_operation_errors.get(receipt['operation_id']) or {}).get('code') or '') for receipt in linked if receipt['outcome'] == 'stale_target'})
            if detail:
                result['detail'] = detail[:160]
            request_results.append(result)
        core_results = [row for row in request_results if row.get('is_core')]
        if core_results:
            if all((row.get('status') == 'done' for row in core_results)):
                appeal.core_request_satisfied = True
            elif any((row.get('status') == 'pending' for row in core_results)):
                appeal.core_request_satisfied = None
            else:
                appeal.core_request_satisfied = False
        review_raw['operation_receipts'] = receipts
        review_raw['request_results'] = request_results
        appeal.review_ai_raw_response = json_dumps_safe(review_raw)

    @staticmethod
    def _successful_operation_receipt_ids(appeal: AdventurerS1DataAppeal) -> set[str]:
        """Return only operations proven written or already satisfied."""
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        receipts = review_raw.get('operation_receipts')
        if not isinstance(receipts, list):
            return set()
        return {str(receipt.get('operation_id') or '').strip() for receipt in receipts if isinstance(receipt, dict) and receipt.get('decision') == 'allow' and (receipt.get('outcome') in ('executed', 'already_satisfied')) and str(receipt.get('operation_id') or '').strip()}

    @staticmethod
    def _mark_operation_receipt_execution_start(appeal: AdventurerS1DataAppeal) -> None:
        """Freeze the audit boundary for this v2 finalize attempt."""
        review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        if review_raw.get('verdict_contract_version') != 2:
            return
        review_raw['_backend_execution_audit_seq_start'] = AdventurerS1DataAppealService._next_audit_seq(appeal.id)
        appeal.review_ai_raw_response = json_dumps_safe(review_raw)

    @staticmethod
    def _record_create_item_execution_skips(appeal: AdventurerS1DataAppeal, skipped: list[dict]) -> None:
        """记录最终写入阶段未落地的独立物品项，不从错误文本推断语义。"""
        if not skipped:
            return
        parsed = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        existing = parsed.get('create_item_execution_skips') if isinstance(parsed.get('create_item_execution_skips'), list) else []
        merged: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for row in [*existing, *skipped]:
            if not isinstance(row, dict):
                continue
            name = str(row.get('name') or '').strip()
            reason = str(row.get('reason') or '').strip()
            key = (name, reason)
            if key in seen:
                continue
            seen.add(key)
            merged.append({'section': 'create_items', 'name': name[:120], 'code': 'grant_rejected', 'reason': reason[:500]})
        parsed['create_item_execution_skips'] = merged
        appeal.review_ai_raw_response = json_dumps_safe(parsed)
        names = '、'.join((row['name'] for row in merged if row.get('name')))
        AdventurerS1DataAppealService._set_review_execution_note(appeal, f"本次已执行其它通过的修复；物品「{names or '未命名物品'}」因最终发放条件未满足而未补发，请在状态变化后单独重新申诉该项。")

    @staticmethod
    def _looks_like_success_reason(text: str | None) -> bool:
        """兼容入口：不再从自然语言理由推断成功状态。"""
        return False

    @staticmethod
    def _is_player_withdrawal_intent(appeal: AdventurerS1DataAppeal) -> bool:
        """只认一审结构化撤回字段，不从玩家或 AI 文案猜测意图。"""
        if AdventurerS1DataAppealService._patch_has_operations(appeal.proposed_patch):
            return False
        if AdventurerS1DataAppealService._patch_has_operations(appeal.review_supplement_patch):
            return False
        raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
        return raw.get('withdrawal_requested') is True

    @staticmethod
    def _withdrawal_cancel_reason(appeal: AdventurerS1DataAppeal) -> str:
        reason = AdventurerS1DataAppealService._get_review_player_reason(appeal)
        if reason and AdventurerS1DataAppealService._looks_like_success_reason(reason):
            return reason[:500]
        return '已按您的要求撤回本次申诉，未对数据做任何修改。'

    @staticmethod
    def can_fix_misclassified_withdrawal(appeal: AdventurerS1DataAppeal) -> bool:
        """rejected 但实为玩家撤回本次申诉（二审 allow + 空 patch + 无落库审计）。"""
        if not appeal or (appeal.status or '') != 'rejected':
            return False
        if (appeal.appeal_category or '') == 'revert_request':
            return False
        if AdventurerS1DataAppealService._appeal_has_revertible_audits(appeal.id):
            return False
        return AdventurerS1DataAppealService._is_player_withdrawal_intent(appeal)

    @staticmethod
    def _summarize_misclassified_withdrawal_candidate(appeal: AdventurerS1DataAppeal) -> dict:
        from models.user import User
        user = db_manager.query(User).filter_by(id=appeal.user_id).first()
        adv = db_manager.query(AdventurerS1).filter_by(id=appeal.adventurer_id).first()
        can_fix = AdventurerS1DataAppealService.can_fix_misclassified_withdrawal(appeal)
        skip_reason = None
        if not can_fix:
            if (appeal.status or '') != 'rejected':
                skip_reason = f'当前状态为 {appeal.status}'
            elif AdventurerS1DataAppealService._appeal_has_revertible_audits(appeal.id):
                skip_reason = '已有落库审计，非纯撤回'
            elif not AdventurerS1DataAppealService._is_player_withdrawal_intent(appeal):
                skip_reason = '未命中撤回意图'
            else:
                skip_reason = '不可修正'
        return {'id': appeal.id, 'user_id': appeal.user_id, 'username': getattr(user, 'username', None), 'nickname': getattr(user, 'nickname', None), 'adventurer_id': appeal.adventurer_id, 'adventurer_name': getattr(adv, 'name', None) or '未知', 'player_note': (appeal.player_note or '')[:200], 'reject_reason': (appeal.reject_reason or '')[:300], 'review_player_reason': (AdventurerS1DataAppealService._get_review_player_reason(appeal) or '')[:300], 'new_status_reason': AdventurerS1DataAppealService._withdrawal_cancel_reason(appeal)[:300], 'can_fix': can_fix, 'skip_reason': skip_reason}

    @staticmethod
    def _collect_misclassified_withdrawal_preview_items(*, appeal_ids: list[int] | None=None, limit: int=50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        if appeal_ids:
            appeals = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.id.in_([int(i) for i in appeal_ids if i])).order_by(AdventurerS1DataAppeal.id.desc()).all()
            return [AdventurerS1DataAppealService._summarize_misclassified_withdrawal_candidate(a) for a in appeals][:limit]
        items: list[dict] = []
        offset = 0
        batch_size = max(limit * 8, 80)
        max_scan = 1000
        scanned = 0
        while len(items) < limit and scanned < max_scan:
            batch = db_manager.query(AdventurerS1DataAppeal).filter(AdventurerS1DataAppeal.status == 'rejected', AdventurerS1DataAppeal.appeal_category != 'revert_request', AdventurerS1DataAppeal.review_ai_status == 'done').order_by(AdventurerS1DataAppeal.id.desc()).offset(offset).limit(batch_size).all()
            if not batch:
                break
            scanned += len(batch)
            offset += batch_size
            for appeal in batch:
                if not AdventurerS1DataAppealService.can_fix_misclassified_withdrawal(appeal):
                    continue
                items.append(AdventurerS1DataAppealService._summarize_misclassified_withdrawal_candidate(appeal))
                if len(items) >= limit:
                    break
        return items

    @staticmethod
    def preview_misclassified_withdrawals(limit: int=50) -> tuple:
        limit = max(1, min(int(limit or 50), 200))
        items = AdventurerS1DataAppealService._collect_misclassified_withdrawal_preview_items(limit=limit)
        return ({'summary': {'total_candidates': len(items), 'fixable_count': sum((1 for i in items if i.get('can_fix')))}, 'items': items}, None)

    @staticmethod
    def _notify_misclassified_withdrawal_fixed(appeal: AdventurerS1DataAppeal) -> None:
        from models.mailbox import Mailbox
        reason = (appeal.reject_reason or '').strip() or '已按您的要求撤回本次申诉。'
        mail = Mailbox(user_id=appeal.user_id, title=f'数据申诉状态已修正（#{appeal.id}）', content=f'您提交的数据异常申诉（#{appeal.id}）此前状态误显示为「未通过」。\n\n现已修正为「已撤回」：\n{reason}', sender_id=None, sender_name='深渊酒馆')
        db_manager.add(mail)

    @staticmethod
    @with_transaction(auto_commit=True)
    def fix_misclassified_withdrawal(appeal_id: int, admin_id: int, *, notify_player: bool=True) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=int(appeal_id)).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if not AdventurerS1DataAppealService.can_fix_misclassified_withdrawal(appeal):
            return (None, '该工单不符合误标撤回修正条件（需为 rejected + 二审 allow + 空 patch + 无落库）')
        previous_status = appeal.status
        appeal.status = 'cancelled'
        appeal.reject_reason = AdventurerS1DataAppealService._withdrawal_cancel_reason(appeal)
        if notify_player:
            AdventurerS1DataAppealService._notify_misclassified_withdrawal_fixed(appeal)
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        logger.info('数据申诉误标撤回已修正 appeal=%s admin=%s prev=%s', appeal_id, admin_id, previous_status)
        return ({'id': appeal.id, 'status': 'cancelled', 'previous_status': previous_status, 'reject_reason': appeal.reject_reason}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def execute_misclassified_withdrawals(admin_id: int, *, appeal_ids: list[int] | None=None, limit: int=50, notify_player: bool=True) -> tuple:
        limit = max(1, min(int(limit or 50), 200))
        ids = [int(i) for i in appeal_ids or [] if i is not None]
        fixed: list[dict] = []
        skipped: list[dict] = []
        errors: list[dict] = []
        if ids:
            summaries = {i['id']: i for i in AdventurerS1DataAppealService._collect_misclassified_withdrawal_preview_items(appeal_ids=ids, limit=len(ids))}
            for appeal_id in ids:
                summary = summaries.get(appeal_id)
                if not summary or not summary.get('can_fix'):
                    skipped.append({'id': appeal_id, 'reason': (summary or {}).get('skip_reason') or '不可修正'})
                    continue
                data, err = AdventurerS1DataAppealService.fix_misclassified_withdrawal(appeal_id, admin_id, notify_player=notify_player)
                if err:
                    errors.append({'id': appeal_id, 'error': err})
                elif data:
                    fixed.append(data)
            return ({'fixed': fixed, 'skipped': skipped, 'errors': errors}, None)
        preview_items = AdventurerS1DataAppealService._collect_misclassified_withdrawal_preview_items(limit=limit)
        for summary in preview_items:
            if not summary.get('can_fix'):
                skipped.append({'id': summary['id'], 'reason': summary.get('skip_reason') or '不可修正'})
                continue
            data, err = AdventurerS1DataAppealService.fix_misclassified_withdrawal(summary['id'], admin_id, notify_player=notify_player)
            if err:
                errors.append({'id': summary['id'], 'error': err})
            elif data:
                fixed.append(data)
        return ({'fixed': fixed, 'skipped': skipped, 'errors': errors}, None)

    @staticmethod
    def _default_suggested_reject_reason(appeal: AdventurerS1DataAppeal, *, has_valid_patch: bool) -> str:
        if has_valid_patch:
            return '不符合修改条件，请补充说明后重提。'
        if getattr(appeal, 'ai_status', None) == 'failed':
            summary = str(getattr(appeal, 'ai_summary', '') or '')
            if 'AI 分析失败' in summary or '未返回有效结果' in summary or '处理异常' in summary:
                return '本次核查未能完成，请稍后重试。'
            return '未找到可以安全修复的数据异常，请核对条目与描述后重提。'
        return '申诉未通过，请核对后重提。'

    @staticmethod
    def get_suggested_reject_reason(appeal: AdventurerS1DataAppeal) -> str:
        has_executable = AdventurerS1DataAppealService._appeal_has_executable_patch(appeal.proposed_patch, appeal.review_supplement_patch)
        review_decision = AdventurerS1DataAppealService._get_review_ai_decision(appeal)
        if review_decision == 'reject':
            review_reason = AdventurerS1DataAppealService._get_review_player_reason(appeal)
            if review_reason:
                if has_executable:
                    return review_reason
                if not AdventurerS1DataAppealService._looks_like_success_reason(review_reason):
                    return review_reason
        if not has_executable:
            patch_err = AdventurerS1DataAppealService._patch_normalization_error_from_summary(appeal.ai_summary)
            if patch_err:
                return '修复方案未能安全应用，未对数据做任何改动。请核对所选条目与问题描述后重新提交。'
            raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal)
            if raw:
                reason = AdventurerS1DataAppealService._normalize_player_reject_reason(raw.get('player_reject_reason') or raw.get('reject_reason_for_player'))
                if reason:
                    return reason
            if getattr(appeal, 'ai_status', None) == 'failed':
                return AdventurerS1DataAppealService._default_suggested_reject_reason(appeal, has_valid_patch=False)
        raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal)
        if raw and (not has_executable):
            reason = AdventurerS1DataAppealService._normalize_player_reject_reason(raw.get('player_reject_reason') or raw.get('reject_reason_for_player'))
            if reason:
                return reason
        return AdventurerS1DataAppealService._default_suggested_reject_reason(appeal, has_valid_patch=has_executable)

    @staticmethod
    @with_transaction(auto_commit=True)
    def approve(appeal_id: int, admin_id: int) -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if appeal.status != 'awaiting_review':
            return (None, f'当前状态为 {appeal.status}，无法审核')
        identity_err = AdventurerS1DataAppealService._appeal_identity_error(appeal)
        if identity_err:
            return (None, identity_err)
        if not appeal.proposed_patch:
            return (None, '没有可执行的修复方案')
        all_player_records = AdventurerS1DataAppealService._build_all_player_records(appeal.adventurer_id, include_internal_recovery_state=True)
        _, apply_err = AdventurerS1DataAppealService._apply_patch(appeal.adventurer_id, appeal.proposed_patch, appeal.snapshot_before, appeal_id=appeal.id, all_player_records=all_player_records, allow_quantity_increase=True)
        if apply_err:
            return (None, apply_err)
        _, dist_err = AdventurerS1DataAppealService._apply_supplement_and_distributions(appeal, all_player_records=all_player_records, proposed_team_pool=(appeal.proposed_patch or {}).get('team_pool'), proposed_tavern_subsidy=(appeal.proposed_patch or {}).get('tavern_subsidy'), proposed_reclaim_items=(appeal.proposed_patch or {}).get('reclaim_items'))
        if dist_err:
            return (None, dist_err)
        appeal.status = 'approved'
        appeal.reviewed_by = admin_id
        appeal.reviewed_at = _portable_utc_now()
        appeal.applied_at = _portable_utc_now()
        AdventurerS1DataAppealService._notify_player_result(appeal, approved=True)
        return ({'id': appeal.id, 'status': 'approved'}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def reject(appeal_id: int, admin_id: int, reject_reason: str='') -> tuple:
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if appeal.status != 'awaiting_review':
            return (None, f'当前状态为 {appeal.status}，无法驳回')
        appeal.status = 'rejected'
        appeal.reviewed_by = admin_id
        appeal.reviewed_at = _portable_utc_now()
        appeal.reject_reason = (reject_reason or '').strip() or '管理员未通过'
        AdventurerS1DataAppealService._notify_player_result(appeal, approved=False)
        return ({'id': appeal.id, 'status': 'rejected'}, None)

    @staticmethod
    @with_transaction(auto_commit=True)
    def revert_appeal(appeal_id: int, actor_id: int | None=None, *, admin_id: int | None=None, by_player: bool=False, via_player_revert_request: bool=False) -> tuple:
        """还原申诉的数据修复：按审计台账倒序逐操作反向，恢复修复前状态。

        支持 status=approved，以及 status=rejected 但存在未还原审计记录（部分落库后整单驳回）的情形。
        """
        operator_id = actor_id if actor_id is not None else admin_id
        if operator_id is None:
            return (None, '缺少操作者 id')
        appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).with_for_update().first()
        if not appeal:
            return (None, '申诉不存在')
        if (appeal.status or '') not in AdventurerS1DataAppealService._REVERT_ELIGIBLE_STATUSES:
            return (None, f'当前状态为 {appeal.status}，仅已通过或已部分落库的驳回申诉可还原')
        if by_player:
            if appeal.user_id != operator_id:
                return (None, '无权操作该申诉')
            offer_pending = (appeal.revert_offer_status or 'none') == 'pending'
            request_allowed = (appeal.player_revert_request_status or 'none') == 'allowed'
            if via_player_revert_request:
                if not request_allowed:
                    return (None, '当前没有待确认的撤回申请')
            elif not offer_pending:
                return (None, '当前没有待确认的还原邀请')
        rows = db_manager.query(AdventurerS1DataAppealAuditLog).filter(AdventurerS1DataAppealAuditLog.appeal_id == appeal_id, AdventurerS1DataAppealAuditLog.reverted.is_(False)).order_by(AdventurerS1DataAppealAuditLog.op_seq.desc()).all()
        if not rows:
            return (None, '没有可还原的变更记录')
        loot_ids = [row.target_id for row in rows if row.op_type == 'unassigned_item']
        if loot_ids:
            from db_repository.s1_team_loot_repository import S1TeamLootRepository
            S1TeamLootRepository.lock_lots(loot_ids)
        exploring_ids = AdventurerS1DataAppealService._revert_exploring_adventurer_ids(appeal_id, lock_rows=True)
        if exploring_ids:
            return (None, f"受影响角色正在探险，已禁止中途还原；需等待出本后再撤销（角色 ID: {', '.join(map(str, exploring_ids))}）")
        ok, err = AdventurerS1DataAppealService._validate_revert_requirements(appeal_id, lock_rows=True)
        if not ok:
            return (None, err or '当前已不满足撤销条件')
        split_audit_err = AdventurerS1DataAppealService._validate_revert_currency_split_audits(rows)
        if split_audit_err:
            return (None, split_audit_err)
        now = _portable_utc_now()
        fallback_unit_price = AdventurerS1DataAppealService._appeal_traced_sale_unit_price(appeal_id)
        results = []
        for row in rows:
            result_text = AdventurerS1DataAppealService._revert_one(row.adventurer_id, row, appeal_id, fallback_unit_price=fallback_unit_price)
            if AdventurerS1DataAppealService._is_blocking_revert_result(result_text):
                db_manager.rollback()
                return (None, result_text)
            row.reverted = True
            row.reverted_at = now
            row.reverted_by = operator_id
            row.revert_result = result_text[:200]
            results.append({'op_seq': row.op_seq, 'op_type': row.op_type, 'target_type': row.target_type, 'target_id': row.target_id, 'result': result_text})
        appeal.status = 'reverted'
        appeal.reverted_at = now
        appeal.reverted_by = operator_id
        if (appeal.cross_character_revert_status or 'none') == 'pending':
            appeal.cross_character_revert_status = 'completed'
            appeal.cross_character_revert_last_error = None
        if (appeal.revert_offer_status or 'none') == 'pending':
            appeal.revert_offer_status = 'accepted'
            appeal.revert_offer_responded_at = now
        if via_player_revert_request or (appeal.player_revert_request_status or 'none') == 'allowed':
            appeal.player_revert_request_status = 'confirmed'
            appeal.player_revert_request_responded_at = now
        base_reason = (appeal.reject_reason or '').strip()
        if by_player:
            note = '您已确认撤回本次申诉的数据修复' if via_player_revert_request else '您已同意撤销本次申诉的数据修复'
        else:
            note = '管理员已还原本次申诉的数据修复'
        appeal.reject_reason = f'{base_reason}\n{note}' if base_reason else note
        from plugins.adventurer_s1.services.s1_bug_reclaim_service import S1BugReclaimService
        S1BugReclaimService.mark_adventurers_dirty({int(row.adventurer_id) for row in rows})
        AdventurerS1DataAppealService._notify_player_reverted(appeal, by_player=by_player)
        AdventurerS1DataAppealService._emit_data_appeal_changed(appeal)
        return ({'id': appeal.id, 'status': 'reverted', 'results': results}, None)

    @staticmethod
    def _is_blocking_revert_result(result_text: str) -> bool:
        text = result_text or ''
        return any((token in text for token in ('失败', '不足', '无法精确还原')))

    @staticmethod
    def _revert_one(adventurer_id: int, row: AdventurerS1DataAppealAuditLog, appeal_id: int, *, fallback_unit_price: float | None=None) -> str:
        op = row.op_type
        if op == 'update':
            return AdventurerS1DataAppealService._revert_update(adventurer_id, row)
        if op == 'delete':
            return AdventurerS1DataAppealService._revert_delete(adventurer_id, row)
        if op == 'create_item':
            return AdventurerS1DataAppealService._revert_create_item(adventurer_id, row, appeal_id, fallback_unit_price=fallback_unit_price)
        if op == 'create_trait':
            return AdventurerS1DataAppealService._revert_create_trait(adventurer_id, row)
        if op == 'create_skill':
            return AdventurerS1DataAppealService._revert_create_skill(adventurer_id, row)
        if op == 'currency':
            return AdventurerS1DataAppealService._revert_currency(adventurer_id, row, appeal_id)
        if op == 'team_pool':
            return '已解除团队池结算标记（人均补偿按 currency 逐条扣回）'
        if op == 'unassigned_item':
            from plugins.adventurer_s1.services.s1_team_loot_service import S1TeamLootService, TeamLootError
            try:
                S1TeamLootService.cancel_appeal_registration_in_transaction(row.target_id, appeal_id)
                return '已撤销尚未开始分配的共同实物登记'
            except TeamLootError as error:
                return str(error)
        if op in ('tavern_subsidy_team', 'tavern_subsidy_self'):
            return '已解除酒馆补贴结算标记（补贴金额按 currency 逐条扣回）'
        if op == 'reclaim_remove':
            return AdventurerS1DataAppealService._revert_delete(adventurer_id, row)
        if op == 'reclaim_compensation':
            return AdventurerS1DataAppealService._revert_reclaim_compensation(adventurer_id, row, appeal_id)
        if op == 'transfer_asset':
            from plugins.adventurer_s1.services.s1_asset_transfer_service import asset_snapshot
            from db_repository.s1_asset_transfer_repository import lock_transfer_state
            receipt = row.after_json or {}
            before = row.before_json or {}
            members, assets = lock_transfer_state([receipt.get('from_adventurer_id'), receipt.get('to_adventurer_id')])
            asset = next((asset for asset in assets.get(receipt.get('asset_kind'), []) if asset.id == receipt.get('asset_id')), None)
            if asset is None or asset_snapshot(asset) != receipt.get('after'):
                return '无法精确还原：转移资产已经变化'
            source = members.get(receipt.get('from_adventurer_id'))
            if source is None or any((candidate.id != asset.id and candidate.adventurer_id == source.id and (candidate.name == asset.name) for candidate in assets.get(receipt.get('asset_kind'), []))):
                return '无法精确还原：来源成员不存在或已有同名资产'
            if receipt.get('asset_kind') == 'trait':
                from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import rebase_transferred_trait_rest
                rebase_transferred_trait_rest(asset, members[receipt['to_adventurer_id']], source)
            asset.adventurer = source
            asset.adventurer_id = source.id
            equipped_count = sum((1 for candidate in assets.get('skill', []) if candidate.id != asset.id and candidate.adventurer_id == source.id and candidate.equipped))
            if receipt.get('asset_kind') == 'skill':
                asset.equipped = bool(before.get('equipped', False) and equipped_count < int(current_app.config.get('ADVENTURER_S1_MAX_EQUIPPED_SKILLS', 5)))
            return '已将原技能或特质实例归还原持有者'
        if op == 'reclaim_item':
            return '已解除物品追回结算标记（移除/补发/折价按关联审计行逐条还原）'
        return f'未知操作类型 {op}，跳过'

    @staticmethod
    def _coerce_restore_value(model, field, value):
        """JSON 里存的 datetime 是 iso 字符串，写回 DateTime 列前转回 datetime。"""
        if not isinstance(value, str):
            return value
        cols = model.__table__.columns
        if field not in cols:
            return value
        from sqlalchemy import DateTime as SADateTime
        if isinstance(cols[field].type, SADateTime):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return value

    @staticmethod
    def _revert_update(adventurer_id: int, row: AdventurerS1DataAppealAuditLog) -> str:
        model = _TABLE_MODEL.get(row.target_type)
        if model is None:
            return f'未知类型 {row.target_type}，跳过'
        record = db_manager.query(model).filter(model.id == row.target_id, model.adventurer_id == adventurer_id).with_for_update().first()
        if not record:
            return '记录已不存在，跳过还原'
        before = row.before_json if isinstance(row.before_json, dict) else {}
        after = row.after_json if isinstance(row.after_json, dict) else {}
        restored = []
        for field in after.keys():
            if field in ('id', 'adventurer_id', 'created_at', 'updated_at'):
                continue
            if field not in model.__table__.columns:
                continue
            value = AdventurerS1DataAppealService._coerce_restore_value(model, field, before.get(field))
            setattr(record, field, value)
            restored.append(field)
        return f"已还原字段：{'、'.join(restored)}" if restored else '无字段需还原'

    @staticmethod
    def _revert_delete(adventurer_id: int, row: AdventurerS1DataAppealAuditLog) -> str:
        model = _TABLE_MODEL.get(row.target_type)
        if model is None:
            return f'未知类型 {row.target_type}，跳过'
        before = row.before_json if isinstance(row.before_json, dict) else {}
        if not before:
            return '缺少原始快照，无法重建'
        if row.target_type == 'skill':
            state, existing = AdventurerS1DataAppealService._deleted_skill_restore_state(adventurer_id, before, lock_rows=True)
            name = str(before.get('name') or '').strip()
            if state == 'equivalent':
                return f'同名同描述技能「{name}」已存在（当前 id={existing.id}），视为已还原并保留当前携带状态'
            if state == 'conflict':
                return f'技能「{name}」已有同名但描述不同的当前记录，无法精确还原'
        col_names = {c.name for c in model.__table__.columns}
        kwargs = {}
        for field, value in before.items():
            if field == 'id' or field not in col_names:
                continue
            kwargs[field] = AdventurerS1DataAppealService._coerce_restore_value(model, field, value)
        kwargs['adventurer_id'] = adventurer_id
        new_record = model(**kwargs)
        ok, err = db_manager.add(new_record)
        if not ok:
            return f'重建失败：{err}'
        return f'已重建被删记录（新 id={new_record.id}）'

    @staticmethod
    def _revert_create_item(adventurer_id: int, row: AdventurerS1DataAppealAuditLog, appeal_id: int, *, fallback_unit_price: float | None=None) -> str:
        after = row.after_json if isinstance(row.after_json, dict) else {}
        destination = after.get('destination')
        name = after.get('name')
        try:
            qty = max(1, int(after.get('quantity') or 1))
        except (TypeError, ValueError):
            qty = 1
        model = {'equipment': AdventurerS1Equipment, 'warehouse': AdventurerS1WarehouseItem}.get(destination)
        if model is None or not name:
            return '补发记录信息不全，无法还原'
        record = db_manager.query(model).filter(model.adventurer_id == adventurer_id, model.name == name).order_by(model.id.desc()).with_for_update().first()
        current = int(record.quantity or 1) if record else 0
        removable_qty = min(current, qty)
        missing_qty = qty - removable_qty
        disposition = None
        if missing_qty > 0:
            disposition = AdventurerS1DataAppealService._created_item_missing_disposition(adventurer_id, row, missing_qty, fallback_unit_price=fallback_unit_price)
            if disposition is None:
                return f'撤销失败：物品「{name}」不足，且未找到足量的探险消耗或出售流水'
        messages = []
        if removable_qty > 0 and record is not None:
            remaining = current - removable_qty
            if remaining > 0:
                record.quantity = remaining
                messages.append(f'移除现存 {removable_qty}（剩余 {remaining}）')
            else:
                ok, del_err = db_manager.delete_instance(record)
                if not ok:
                    return f'删除「{name}」失败：{del_err}'
                messages.append(f'移除现存 {removable_qty}')
        if disposition is not None and disposition['expedition_consumed_quantity'] > 0:
            messages.append(f"探险中已消耗 {disposition['expedition_consumed_quantity']}")
        sale_plan = disposition.get('sale') if disposition is not None else None
        if sale_plan is not None:
            from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
            clawback_gold = round(float(sale_plan['gold']), 2)
            _, deduct_err = S1CurrencyService.deduct(adventurer_id, 'gold', clawback_gold, source_type='data_appeal_revert_sale', related_id=row.id or appeal_id, related_type='data_appeal_audit', description=f'数据申诉还原：扣回已出售补发物「{name}」的实际到账', auto_commit=False, allow_negative=True)
            if deduct_err:
                return f'扣回已出售物品「{name}」的金币失败：{deduct_err}'
            fallback_price = sale_plan.get('fallback_unit_price')
            if fallback_price:
                messages.append(f"去向不可追溯 {sale_plan['quantity']}，按同申诉已追溯统一单价 {fallback_price} 扣回 {clawback_gold} 金币")
            else:
                messages.append(f"已售 {sale_plan['quantity']}，按实际到账扣回 {clawback_gold} 金币")
        return f"已撤销补发「{name}」×{qty}（{'；'.join(messages)}）"

    @staticmethod
    def _revert_create_trait(adventurer_id: int, row: AdventurerS1DataAppealAuditLog) -> str:
        after = row.after_json if isinstance(row.after_json, dict) else {}
        name = after.get('name')
        if not name:
            return '补发特质记录信息不全，无法还原'
        record = db_manager.query(AdventurerS1Trait).filter(AdventurerS1Trait.adventurer_id == adventurer_id, AdventurerS1Trait.name == name).order_by(AdventurerS1Trait.id.desc()).with_for_update().first()
        if not record:
            return f'未找到补发特质「{name}」（可能已被删除），无法精确还原'
        ok, del_err = db_manager.delete_instance(record)
        if not ok:
            return f'删除特质「{name}」失败：{del_err}'
        return f'已撤销补发特质「{name}」'

    @staticmethod
    def _revert_create_skill(adventurer_id: int, row: AdventurerS1DataAppealAuditLog) -> str:
        after = row.after_json if isinstance(row.after_json, dict) else {}
        name = after.get('name')
        if not name:
            return '补发技能记录信息不全，无法还原'
        record = db_manager.query(AdventurerS1Skill).filter(AdventurerS1Skill.adventurer_id == adventurer_id, AdventurerS1Skill.name == name).order_by(AdventurerS1Skill.id.desc()).with_for_update().first()
        if not record:
            return f'未找到补发技能「{name}」（可能已被删除），无法精确还原'
        ok, del_err = db_manager.delete_instance(record)
        if not ok:
            return f'删除技能「{name}」失败：{del_err}'
        return f'已撤销补发技能「{name}」'

    @staticmethod
    def _revert_reclaim_compensation(adventurer_id: int, row: AdventurerS1DataAppealAuditLog, appeal_id: int) -> str:
        after = row.after_json if isinstance(row.after_json, dict) else {}
        try:
            amount = round(float(after.get('amount')), 2)
        except (TypeError, ValueError):
            return '追回折价扣款无效，跳过'
        if amount <= 0:
            return '追回折价扣款非正，跳过'
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        _, err = S1CurrencyService.add(adventurer_id, 'gold', amount, source_type='data_appeal_revert', related_id=appeal_id, related_type='data_appeal_revert', description='数据申诉还原：退回追回折价扣款', auto_commit=False)
        if err:
            return f'退回折价扣款 {amount} 失败：{err}'
        return f'已退回追回折价扣款 {amount} 金币'

    @staticmethod
    def _validate_revert_currency_split_audits(rows: list) -> str | None:
        """在写入前校验金币分流审计，旧审计无法唯一还原时 fail closed。"""
        legacy_gold_rows = []
        for row in rows:
            if row.op_type != 'currency':
                continue
            after = row.after_json if isinstance(row.after_json, dict) else {}
            code = after.get('currency_code') or 'gold'
            if code != 'gold':
                continue
            if 'low_consumption_credited' not in after:
                legacy_gold_rows.append(row)
                continue
            try:
                amount = round(float(after.get('amount')), 2)
                temporary_credited = round(float(after.get('low_consumption_credited') or 0), 2)
                gold_credited = round(float(after.get('gold_credited', amount - temporary_credited)), 2)
            except (TypeError, ValueError):
                return f'审计 #{row.id} 的补发分流金额无效，无法精确还原'
            if amount <= 0 or temporary_credited < 0 or temporary_credited > amount + 0.001 or (round(gold_credited + temporary_credited, 2) != amount):
                return f'审计 #{row.id} 的补发分流金额不守恒，无法精确还原'
        if not legacy_gold_rows:
            return None
        legacy_adventurer_ids = sorted({int(row.adventurer_id) for row in legacy_gold_rows})
        legacy_appeal_ids = sorted({str(row.appeal_id) for row in legacy_gold_rows})
        split_logs = db_manager.query(AdventurerS1CurrencyLog).filter(AdventurerS1CurrencyLog.adventurer_id.in_(legacy_adventurer_ids), AdventurerS1CurrencyLog.currency_code == 'low_consumption_gold', AdventurerS1CurrencyLog.source_type == 'low_consumption_split', AdventurerS1CurrencyLog.related_type == 'data_appeal', AdventurerS1CurrencyLog.related_id.in_(legacy_appeal_ids), AdventurerS1CurrencyLog.change_amount > 0).order_by(AdventurerS1CurrencyLog.id.asc()).all()
        split_pairs = {(str(log.related_id), int(log.adventurer_id)) for log in split_logs}
        ambiguous_rows = [row for row in legacy_gold_rows if (str(row.appeal_id), int(row.adventurer_id)) in split_pairs]
        if ambiguous_rows:
            audit_ids = '、'.join((str(row.id) for row in ambiguous_rows))
            return f'旧版金币审计 #{audit_ids} 缺少金币/临时金分流明细，且同申诉角色存在临时金分流流水，无法逐条唯一对应；为避免多扣，禁止自动还原'
        return None

    @staticmethod
    def _revert_currency(adventurer_id: int, row: AdventurerS1DataAppealAuditLog, appeal_id: int) -> str:
        after = row.after_json if isinstance(row.after_json, dict) else {}
        code = after.get('currency_code') or 'gold'
        try:
            amount = round(float(after.get('amount')), 2)
        except (TypeError, ValueError):
            return '补发金额无效，跳过'
        if amount <= 0:
            return '补发金额非正，跳过'
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        if code == 'gold' and 'low_consumption_credited' in after:
            try:
                temporary_credited = round(float(after.get('low_consumption_credited') or 0), 2)
                recorded_gold_credited = round(float(after.get('gold_credited', amount - temporary_credited)), 2)
            except (TypeError, ValueError):
                return '补发分流审计金额无效，无法精确还原'
            if temporary_credited < 0 or temporary_credited > amount + 0.001 or round(recorded_gold_credited + temporary_credited, 2) != amount:
                return '补发分流审计金额不守恒，无法精确还原'
            reclaimed, reclaim_err = S1CurrencyService.reclaim_low_consumption_split_in_transaction(adventurer_id, amount, temporary_credited, source_type='data_appeal_revert', related_id=appeal_id, related_type='data_appeal_revert', description='数据申诉还原：按原始金币/临时金分流扣回补发')
            if reclaim_err:
                return f'扣回 gold {amount} 失败：{reclaim_err}'
            return f"已扣回补发 gold {amount}（金币 {reclaimed['gold_reclaimed']}，临时金 {reclaimed['low_consumption_reclaimed']}）"
        _, err = S1CurrencyService.deduct(adventurer_id, code, amount, source_type='data_appeal_revert', related_id=appeal_id, related_type='data_appeal_revert', description='数据申诉还原：扣回补发金币', auto_commit=False, allow_negative=True)
        if err:
            return f'扣回 {code} {amount} 失败：{err}'
        return f'已扣回补发 {code} {amount}'

    @staticmethod
    def _notify_player_reverted(appeal: AdventurerS1DataAppeal, *, by_player: bool=False):
        from models.mailbox import Mailbox
        if by_player:
            content = f'您已确认撤销数据异常申诉（#{appeal.id}）的数据修复，相关条目已恢复到修复前状态。如有疑问可重新提交申诉说明情况。'
        else:
            content = f'管理员已撤销并还原您此前通过的数据异常申诉（#{appeal.id}）所做的数据修复，相关条目已恢复到修复前状态。如有疑问可重新提交申诉说明情况。'
        mail = Mailbox(user_id=appeal.user_id, title=f'数据申诉修复已还原（#{appeal.id}）', content=content, sender_id=None, sender_name='深渊酒馆')
        db_manager.add(mail)

    @staticmethod
    def _expand_approved_destructive_actions(*, adventurer_id: int, patch: dict, snapshot_before: dict, appeal_row: AdventurerS1DataAppeal | None) -> dict:
        """把逐 ID 复核且冻结状态未漂移的破坏性动作展开为普通原子写入。"""
        cleaned, blocked_direct = strip_uncontracted_trait_skill_deletes(patch)
        if blocked_direct:
            logger.warning('数据申诉 appeal=%s 最终写入层拒绝未契约化特质/技能删除: %s', getattr(appeal_row, 'id', None), blocked_direct)
        if not appeal_row or appeal_row.review_ai_status != 'done':
            for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
                cleaned.pop(section, None)
            return cleaned
        review_raw = appeal_row._parse_json_text(appeal_row.review_ai_raw_response) or {}
        allowed_by_section = AdventurerS1DataAppealService._collect_approved_destructive_action_ids(review_raw)
        source_section_by_kind = {'trait': 'traits', 'skill': 'skills'}
        model_by_kind = {'trait': AdventurerS1Trait, 'skill': AdventurerS1Skill}
        create_section_by_kind = {'trait': 'create_traits', 'skill': 'create_skills'}
        frozen_fields_by_kind = {'trait': tuple(_PATCH_UPDATE_FIELDS['trait']), 'skill': tuple(_PATCH_UPDATE_FIELDS['skill'])}
        frozen_record_maps = {section: {int(row['id']): row for row in snapshot_before.get(section) or [] if isinstance(row, dict) and row.get('id') is not None} for section in ('traits', 'skills')}
        reserved_targets: set[tuple[str, str]] = set()
        reserved_target_ids: set[tuple[str, int]] = set()
        reserved_target_payloads: dict[tuple[str, int], dict] = {}
        skipped_actions: list[dict] = []
        destructive_source_keys = {(destructive_action_source_kind(section), source_id) for section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS for source_id in destructive_action_source_ids(cleaned, section)}

        def _skip(action_section: str, source_id: int | None, code: str) -> None:
            skipped_actions.append({'section': action_section, 'source_id': source_id, 'code': code})
        actions_by_section: dict[str, list[dict]] = {}
        lock_ids_by_kind: dict[str, set[int]] = {'trait': set(), 'skill': set()}
        target_names_by_kind: dict[str, set[str]] = {'trait': set(), 'skill': set()}
        for action_section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            actions = project_data_appeal_destructive_actions(action_section, cleaned.get(action_section, []))
            cleaned.pop(action_section, None)
            if actions is None:
                logger.warning('数据申诉 appeal=%s 最终写入层拒绝非法破坏性动作 section=%s', appeal_row.id, action_section)
                _skip(action_section, None, 'contract_invalid')
                actions = []
            actions_by_section[action_section] = actions
            source_kind = destructive_action_source_kind(action_section)
            for action in actions:
                lock_ids_by_kind[source_kind].add(action['source_id'])
                target_kind = destructive_action_target_kind(action['mode'])
                target = action.get('target') if target_kind else None
                if not isinstance(target, dict):
                    continue
                target_name = str(target.get('name') or '').strip()
                if target_name:
                    target_names_by_kind[target_kind].add(target_name)
                if target.get('id') is not None:
                    lock_ids_by_kind[target_kind].add(int(target['id']))
        records_by_kind_id: dict[str, dict[int, object]] = {'trait': {}, 'skill': {}}
        records_by_kind_name: dict[str, dict[str, list[object]]] = {'trait': defaultdict(list), 'skill': defaultdict(list)}
        for kind, model in model_by_kind.items():
            ids = lock_ids_by_kind[kind]
            if ids:
                locked_rows = db_manager.query(model).filter(model.adventurer_id == int(adventurer_id), model.id.in_(ids)).with_for_update().all()
                records_by_kind_id[kind] = {int(row.id): row for row in locked_rows}
            names = target_names_by_kind[kind]
            if names:
                named_rows = db_manager.query(model).filter(model.adventurer_id == int(adventurer_id), model.name.in_(names)).with_for_update().all()
                for row in named_rows:
                    records_by_kind_name[kind][str(row.name or '').strip()].append(row)
        for kind, create_section in create_section_by_kind.items():
            for row in cleaned.get(create_section) or []:
                if isinstance(row, dict):
                    name = str(row.get('name') or '').strip()
                    if name:
                        reserved_targets.add((kind, name))
        for action_section in DATA_APPEAL_DESTRUCTIVE_ACTION_SECTIONS:
            actions = actions_by_section[action_section]
            source_kind = destructive_action_source_kind(action_section)
            source_section = source_section_by_kind[source_kind]
            frozen_map = frozen_record_maps[source_section]
            for action in actions:
                source_id = action['source_id']
                if source_id not in allowed_by_section[action_section]:
                    continue
                source_patch = cleaned.setdefault(source_section, {'update': [], 'delete': []})
                source_patch['update'] = [row for row in source_patch.get('update') or [] if not isinstance(row, dict) or row.get('id') != source_id]
                source_patch['delete'] = [row for row in source_patch.get('delete') or [] if (row.get('id') if isinstance(row, dict) else row) != source_id]
                frozen = frozen_map.get(source_id)
                if not frozen:
                    _skip(action_section, source_id, 'source_not_frozen')
                    continue
                source = records_by_kind_id[source_kind].get(source_id)
                if not source:
                    _skip(action_section, source_id, 'source_missing')
                    continue
                if any((field in frozen and getattr(source, field, None) != frozen.get(field) for field in frozen_fields_by_kind[source_kind])):
                    logger.info('数据申诉 appeal=%s 破坏性动作源记录漂移，跳过 %s:%s', appeal_row.id, source_kind, source_id)
                    _skip(action_section, source_id, 'source_state_drifted')
                    continue
                target_kind = destructive_action_target_kind(action['mode'])
                target = action.get('target') if target_kind else None
                reuse_reserved_target = False
                if target_kind:
                    target_name = str(target.get('name') or '').strip()
                    target_key = (target_kind, target_name)
                    target_id = target.get('id')
                    if not target_name:
                        _skip(action_section, source_id, 'target_invalid')
                        continue
                    same_identity_replacement = target_kind == source_kind and (target_id is None or int(target_id) == source_id)
                    if same_identity_replacement:
                        collision_ids = {int(row.id) for row in records_by_kind_name[target_kind].get(target_name, [])}
                        collision_ids.discard(source_id)
                        source_name = str(getattr(source, 'name', '') or '').strip()
                        if target_key in reserved_targets or (target_name != source_name and collision_ids):
                            _skip(action_section, source_id, 'target_conflict')
                            continue
                        reserved_targets.add(target_key)
                        in_place_update = {key: value for key, value in target.items() if key != 'reason'}
                        in_place_update['id'] = source_id
                        source_patch['update'].append(in_place_update)
                        continue
                    if target_id is not None:
                        target_id = int(target_id)
                        target_id_key = (target_kind, target_id)
                        target_payload = {key: value for key, value in target.items() if key != 'reason'}
                        if target_id_key in reserved_target_ids:
                            if reserved_target_payloads[target_id_key] != target_payload:
                                _skip(action_section, source_id, 'target_payload_conflict')
                                continue
                            reuse_reserved_target = True
                        else:
                            target_section = source_section_by_kind[target_kind]
                            target_patch = cleaned.setdefault(target_section, {'update': [], 'delete': []})
                            target_patch['update'] = [row for row in target_patch.get('update') or [] if not isinstance(row, dict) or row.get('id') != target_id]
                            target_patch['delete'] = [row for row in target_patch.get('delete') or [] if (row.get('id') if isinstance(row, dict) else row) != target_id]
                            target_frozen = frozen_record_maps[target_section].get(target_id)
                            if target_id_key in destructive_source_keys or (target_kind == source_kind and target_id == source_id) or (not target_frozen):
                                _skip(action_section, source_id, 'target_not_frozen')
                                continue
                            target_record = records_by_kind_id[target_kind].get(target_id)
                            target_drifted = bool(target_record and any((field in target_frozen and getattr(target_record, field, None) != target_frozen.get(field) for field in frozen_fields_by_kind[target_kind])))
                            target_already_matches = bool(target_record and target_drifted and all((field not in target_payload or getattr(target_record, field, None) == target_payload[field] for field in frozen_fields_by_kind[target_kind])))
                            if not target_record or (target_drifted and (not target_already_matches)):
                                _skip(action_section, source_id, 'target_state_drifted')
                                continue
                    elif target_key in reserved_targets:
                        _skip(action_section, source_id, 'target_conflict')
                        continue
                    collision_ids = {int(row.id) for row in records_by_kind_name[target_kind].get(target_name, [])}
                    if target_id is not None:
                        collision_ids.discard(target_id)
                    if target_kind == source_kind:
                        collision_ids.discard(source_id)
                    if collision_ids:
                        _skip(action_section, source_id, 'target_conflict')
                        continue
                    if target_id is not None:
                        if not reuse_reserved_target:
                            reserved_target_ids.add(target_id_key)
                            reserved_target_payloads[target_id_key] = target_payload
                    else:
                        reserved_targets.add(target_key)
                source_patch['delete'].append({'id': source_id})
                if target_kind and (not reuse_reserved_target):
                    if target.get('id') is not None:
                        target_section = source_section_by_kind[target_kind]
                        target_patch = cleaned.setdefault(target_section, {'update': [], 'delete': []})
                        target_id = target['id']
                        target_patch['update'].append(target)
                    else:
                        cleaned.setdefault(create_section_by_kind[target_kind], []).append(target)
        if skipped_actions:
            try:
                review_raw = json.loads(appeal_row.review_ai_raw_response or '{}')
            except (TypeError, ValueError):
                review_raw = {}
            if not isinstance(review_raw, dict):
                review_raw = {}
            review_raw['destructive_execution_skips'] = skipped_actions
            appeal_row.review_ai_raw_response = json_dumps_safe(review_raw)
            AdventurerS1DataAppealService._set_review_execution_note(appeal_row, f'本次有 {len(skipped_actions)} 项特质或技能清理因提交后数据状态变化、目标冲突或最终写入条件不完整而未执行；其它独立通过项不受影响。')
        return cleaned

    @staticmethod
    def _apply_patch(adventurer_id: int, patch: dict, snapshot_before: dict, *, appeal_id: int | None=None, all_player_records: dict | None=None, allow_quantity_increase: bool=False, preserve_unchanged_update_ids: dict[str, set[int]] | None=None, execution_status_out: dict | None=None) -> tuple:
        """以 savepoint 包住整份修复，任何返回错误都不会留下半套写入。"""
        from plugins.adventurer_s1.services.s1_appeal_departure_guard import capture_departure_asset_state, departure_asset_change_error, load_departure_asset_ids
        savepoint = db_manager.begin_nested()
        try:
            locked_adventurer = db_manager.query(AdventurerS1.id).filter(AdventurerS1.id == int(adventurer_id)).with_for_update().first()
            if not locked_adventurer:
                savepoint.rollback()
                return (None, '冒险者不存在')
            protected = load_departure_asset_ids(int(adventurer_id))
            before = capture_departure_asset_state(int(adventurer_id), protected)
            appeal = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).first() if appeal_id is not None else None
            raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {} if appeal else {}
            continuation_before = AdventurerS1DataAppealService._continuation_asset_states(adventurer_id, lock=True) if raw.get('_backend_player_continuation') else None
            result = AdventurerS1DataAppealService._apply_patch_impl(adventurer_id, patch, snapshot_before, appeal_id=appeal_id, all_player_records=all_player_records, allow_quantity_increase=allow_quantity_increase, preserve_unchanged_update_ids=preserve_unchanged_update_ids, execution_status_out=execution_status_out)
            if not result[1] and continuation_before is not None:
                db_manager.flush()
                issue = AdventurerS1DataAppealService._continuation_patch_error(snapshot_before, continuation_before, AdventurerS1DataAppealService._continuation_asset_states(adventurer_id, lock=False))
                if issue:
                    if isinstance(execution_status_out, dict):
                        execution_status_out['terminal_operation_error'] = issue
                    result = (None, issue['detail'])
            if not result[1] and any(protected.values()):
                db_manager.flush()
                issue = departure_asset_change_error(before, capture_departure_asset_state(int(adventurer_id), protected))
                if issue:
                    if isinstance(execution_status_out, dict):
                        execution_status_out['terminal_operation_error'] = issue
                    result = (None, issue['detail'])
            if result[1]:
                savepoint.rollback()
            else:
                savepoint.commit()
            return result
        except Exception:
            savepoint.rollback()
            raise

    @staticmethod
    def _continuation_asset_states(adventurer_id: int, *, lock: bool) -> dict:
        states = {}
        for section, model in (('traits', AdventurerS1Trait), ('skills', AdventurerS1Skill), ('equipment', AdventurerS1Equipment), ('warehouse', AdventurerS1WarehouseItem)):
            query = db_manager.query(model).filter_by(adventurer_id=adventurer_id).order_by(model.id)
            if lock:
                query = query.with_for_update()
            states[section] = {row.id: {key: value for key, value in AdventurerS1DataAppealService._serialize_record({'traits': 'trait', 'skills': 'skill'}.get(section, section), row).items() if key != 'recovering'} for row in query.populate_existing().all()}
        return states

    @staticmethod
    def _continuation_patch_error(snapshot: dict, before: dict, after: dict) -> dict | None:
        for section, previous in before.items():
            frozen = {int(row['id']): row for row in snapshot.get(section, [])}
            for rid, original in previous.items():
                current = after.get(section, {}).get(rid)
                comparable = set(original) - {'created_at', 'updated_at'}
                if current is not None and all((original[k] == current.get(k) for k in comparable)):
                    continue
                baseline = frozen.get(rid) or {}
                fields = (set(baseline) & comparable) - {'id', 'adventurer_id'}
                changed = {k for k in comparable - {'id', 'adventurer_id'} if current is None or original[k] != current.get(k)}
                if not fields or (current is not None and (not changed.issubset(baseline))) or any((baseline[k] != original[k] for k in fields)):
                    return {'code': 'continuation_target_changed', 'detail': '目标在原申诉之后已有变化或缺少可核对的冻结记录；本项未执行，请基于当前数据重新申诉。'}
        return None

    @staticmethod
    def _apply_patch_impl(adventurer_id: int, patch: dict, snapshot_before: dict, *, appeal_id: int | None=None, all_player_records: dict | None=None, allow_quantity_increase: bool=False, preserve_unchanged_update_ids: dict[str, set[int]] | None=None, execution_status_out: dict | None=None) -> tuple:
        expedition_evidence = None
        appeal_row = None
        if appeal_id is not None:
            appeal_row = db_manager.query(AdventurerS1DataAppeal).filter_by(id=appeal_id).first()
            if appeal_row and isinstance(appeal_row.expedition_evidence, dict):
                expedition_evidence = copy.deepcopy(appeal_row.expedition_evidence)
        if appeal_row:
            identity_err = AdventurerS1DataAppealService._appeal_snapshot_identity_error(appeal_row)
            if identity_err:
                return (None, identity_err)
        objective_type_update_ids = AdventurerS1DataAppealService._collect_objective_type_update_ids_from_appeal(appeal_row)
        objective_name_update_ids = AdventurerS1DataAppealService._collect_objective_name_update_ids_from_appeal(appeal_row)
        if appeal_row and (appeal_row.appeal_category or 'other') == 'rarity_review':
            patch, guard_err = AdventurerS1DataAppealService._rarity_review_patch_guard(appeal_row, patch)
            if guard_err:
                return (None, guard_err)
        exp_log_id = AdventurerS1DataAppealService._resolve_expedition_log_id_for_appeal(appeal_row, expedition_evidence)
        locked_adventurer = db_manager.query(AdventurerS1.id).filter(AdventurerS1.id == int(adventurer_id)).with_for_update().first()
        if not locked_adventurer:
            return (None, '冒险者不存在')
        if exp_log_id is not None:
            locked_source = db_manager.query(AdventurerS1ExpeditionLog.id).filter(AdventurerS1ExpeditionLog.id == int(exp_log_id)).with_for_update().first()
            if not locked_source:
                return (None, '申诉对应的探险源头不存在')
        expedition_evidence = AdventurerS1DataAppealService._attach_special_dungeon_appeal_context(expedition_evidence, int(adventurer_id))
        patch, blocked_departure_grants = AdventurerS1DataAppealService._strip_teammate_departure_resource_creates(patch, appeal_adventurer_id=int(adventurer_id), expedition_evidence=expedition_evidence)
        if blocked_departure_grants:
            names = '、'.join((str(row.get('name') or '').strip() for row in blocked_departure_grants if str(row.get('name') or '').strip()))
            blocked_nontransferable = any((row.get('section') in ('create_traits', 'create_skills') for row in blocked_departure_grants))
            if blocked_nontransferable:
                return (None, f"技能或特质「{names or '未命名资源'}」出发时属于队友，剧情中的借用、误用或表现不能补成申诉者的永久数据")
            return (None, f"物品「{names or '未命名物品'}」出发时属于队友，普通补发不能复制该资产；若剧情确有永久转移，必须使用会同步处理来源资产的 reclaim_items")
        allow_rarity_inc = bool(appeal_row and (appeal_row.appeal_category or 'other') == 'rarity_review') or AdventurerS1DataAppealService._should_allow_expedition_rarity_increase(patch, snapshot_before, int(adventurer_id), exp_log_id)
        pre_normalize_record_maps = {section: {int(row['id']): row for row in snapshot_before.get(section) or [] if isinstance(row, dict) and row.get('id') is not None} for section in ('traits', 'equipment', 'warehouse', 'skills')}
        frozen_item_names = {section: {rid: str(row.get('name') or '').strip() for rid, row in pre_normalize_record_maps[section].items()} for section in ('equipment', 'warehouse')}
        frozen_item_custom_descs = {section: {rid: str(row.get('custom_desc') or '') for rid, row in pre_normalize_record_maps[section].items()} for section in ('equipment', 'warehouse')}
        if all_player_records:
            for section in ('traits', 'equipment', 'warehouse', 'skills'):
                for row in all_player_records.get(section) or []:
                    if not isinstance(row, dict) or row.get('id') is None:
                        continue
                    try:
                        record_id = int(row['id'])
                    except (TypeError, ValueError):
                        continue
                    merged = dict(pre_normalize_record_maps[section].get(record_id) or {})
                    merged.update(row)
                    pre_normalize_record_maps[section][record_id] = merged
        special_guard_err = AdventurerS1DataAppealService._special_trait_relief_patch_guard(patch, expedition_evidence=expedition_evidence, adventurer_id=int(adventurer_id), record_maps=pre_normalize_record_maps)
        if special_guard_err:
            return (None, special_guard_err)
        final_patch_contract_status: dict = {}
        normalized, err = AdventurerS1DataAppealService.normalize_patch(patch, snapshot_before, all_player_records=all_player_records, allow_quantity_increase=allow_quantity_increase, allow_rarity_increase=allow_rarity_inc, expedition_evidence=expedition_evidence, allow_objective_name_update_ids=objective_name_update_ids, allow_custom_desc_clear_ids=AdventurerS1DataAppealService._collect_custom_desc_clear_ids_from_appeal(appeal_row), adventurer_id=int(adventurer_id), appeal_id=appeal_id, preserve_unchanged_update_ids=preserve_unchanged_update_ids, contract_status_out=final_patch_contract_status)
        if err:
            return (None, err)
        from plugins.adventurer_s1.services.s1_appeal_rarity_restore_service import EVIDENCE_KEY as rarity_restore_key, has_restore_verdict, permits_restore, refresh_candidates as refresh_rarity_restore_candidates
        if final_patch_contract_status.get('contract_valid') is not True and any((isinstance(update, dict) and 'rarity' in update and (_normalize_appeal_patch_integer(update.get('id'), minimum=1) == candidate.get('target_id')) for candidate in (expedition_evidence or {}).get(rarity_restore_key) or [] if isinstance(candidate, dict) for update in (patch.get(candidate.get('target_type')) or {}).get('update') or [])):
            return (None, '原稀有度恢复操作未完整通过当前状态校验，未执行剩余字段')
        rarity_restores = []
        for section in ('equipment', 'warehouse'):
            frozen_records = {row.get('id'): row for row in snapshot_before.get(section) or [] if isinstance(row, dict)}
            for update in (normalized.get(section) or {}).get('update') or []:
                if not isinstance(update, dict):
                    continue
                frozen = frozen_records.get(update.get('id')) or {}
                if permits_restore(expedition_evidence, section, frozen, update.get('rarity'), int(adventurer_id)):
                    rarity_restores.append((section, frozen, update['rarity']))
        if rarity_restores:
            current_restore_record_ids: dict[str, set[int]] = {}
            fresh_evidence = refresh_rarity_restore_candidates(expedition_evidence, int(adventurer_id), snapshot_before, lock=True, current_record_ids_out=current_restore_record_ids)
            frozen_candidates = (expedition_evidence or {}).get(rarity_restore_key) or []
            fresh_candidates = (fresh_evidence or {}).get(rarity_restore_key) or []
            for section, frozen, rarity in rarity_restores:
                if not has_restore_verdict(appeal_row, section, frozen['id'], rarity):
                    return (None, '恢复原稀有度尚未取得对应操作的独立复核授权')
                if section in current_restore_record_ids and frozen['id'] not in current_restore_record_ids[section]:
                    detail = '恢复原稀有度的冻结物品已不在提交时的位置，需针对当前持有记录重新申诉'
                    if isinstance(execution_status_out, dict):
                        execution_status_out['terminal_operation_error'] = {'code': _DATA_APPEAL_STALE_ASSET_TARGET_CODE, 'detail': detail}
                    return (None, detail)
                if not permits_restore(fresh_evidence, section, frozen, rarity, int(adventurer_id)) or any((candidate not in fresh_candidates for candidate in frozen_candidates if candidate.get('target_type') == section and candidate.get('target_id') == frozen['id'])):
                    return (None, '恢复原稀有度的来源或当前物品已变化，未按旧证据写入')
            expedition_evidence = fresh_evidence
        if appeal_row:
            normalized = AdventurerS1DataAppealService._expand_approved_destructive_actions(adventurer_id=int(adventurer_id), patch=normalized, snapshot_before=snapshot_before, appeal_row=appeal_row)
            if not AdventurerS1DataAppealService._patch_has_operations(normalized):
                return (None, 'patch 中没有有效操作')
        trait_record_map = {r['id']: r for r in snapshot_before.get('traits') or [] if isinstance(r, dict) and r.get('id') is not None}
        record_maps = {section: {r['id']: r for r in snapshot_before.get(section) or [] if isinstance(r, dict) and r.get('id') is not None} for section in ('traits', 'equipment', 'warehouse', 'skills')}
        if all_player_records:
            for section in ('traits', 'equipment', 'warehouse', 'skills'):
                for r in all_player_records.get(section) or []:
                    if not isinstance(r, dict) or r.get('id') is None:
                        continue
                    try:
                        rid = int(r['id'])
                    except (TypeError, ValueError):
                        continue
                    prev = record_maps[section].get(rid) or {}
                    merged = dict(prev)
                    merged.update({k: v for k, v in r.items() if k != 'id'})
                    record_maps[section][rid] = merged
        appeal_item_names = {str(row.get('name') or '').strip() for section in ('equipment', 'warehouse') for row in record_maps[section].values() if str(row.get('name') or '').strip()}
        for section in ('equipment', 'warehouse'):
            for update in (normalized.get(section) or {}).get('update') or []:
                if not isinstance(update, dict):
                    continue
                update_name = str(update.get('name') or '').strip()
                if update_name:
                    appeal_item_names.add(update_name)
        appeal_item_catalog_desc_map = AdventurerS1DataAppealService._load_catalog_desc_map(appeal_item_names)
        special_guard_err = AdventurerS1DataAppealService._special_trait_relief_patch_guard(normalized, expedition_evidence=expedition_evidence, adventurer_id=int(adventurer_id), record_maps=record_maps)
        if special_guard_err:
            return (None, special_guard_err)
        comp_err = AdventurerS1DataAppealService._validate_delete_compensation(normalized, trait_record_map=trait_record_map, record_maps=record_maps, adventurer_id=adventurer_id)
        if comp_err:
            return (None, comp_err)
        audit_rows: list = []
        seq_base = AdventurerS1DataAppealService._next_audit_seq(appeal_id) if appeal_id is not None else 0

        def _audit(op_type, target_type, target_id, before, after, note=None):
            if appeal_id is None:
                return
            audit_rows.append(AdventurerS1DataAppealAuditLog(appeal_id=appeal_id, adventurer_id=adventurer_id, op_seq=seq_base + len(audit_rows), op_type=op_type, target_type=target_type, target_id=target_id, before_json=before, after_json=after, note=str(note)[:200] if note else None))
        type_map = {'traits': ('trait', AdventurerS1Trait), 'equipment': ('equipment', AdventurerS1Equipment), 'warehouse': ('warehouse', AdventurerS1WarehouseItem), 'skills': ('skill', AdventurerS1Skill)}
        adventurer_for_recovery_baseline = None
        trait_asset_rename_changes: list[dict] = []
        source_expedition_log = db_manager.query(AdventurerS1ExpeditionLog).filter_by(id=int(exp_log_id)).first() if exp_log_id is not None else None
        recovery_trigger_time = None
        if source_expedition_log is not None and source_expedition_log.status == 'completed':
            recovery_trigger_time = source_expedition_log.updated_at or source_expedition_log.created_at
        for section, (rtype, model) in type_map.items():
            sec = normalized.get(section) or {}

            def _process_deletes() -> str | None:
                for del_item in sec.get('delete') or []:
                    raw_id = del_item.get('id') if isinstance(del_item, dict) else del_item
                    if raw_id is None:
                        logger.warning('数据申诉 apply 跳过无效 delete %s（缺少 id）appeal=%s', rtype, appeal_id)
                        continue
                    try:
                        rid = int(raw_id)
                    except (TypeError, ValueError):
                        logger.warning('数据申诉 apply 跳过无效 delete %s id=%r appeal=%s', rtype, raw_id, appeal_id)
                        continue
                    record = db_manager.query(model).filter(model.id == rid, model.adventurer_id == adventurer_id).with_for_update().first()
                    if not record:
                        logger.info('数据申诉 apply 跳过失效删除 %s id=%s（记录已不存在）appeal=%s', rtype, rid, appeal_id)
                        continue
                    before_full = AdventurerS1DataAppealService._full_record_columns(record)
                    del_err = None
                    if rtype in ('equipment', 'warehouse'):
                        from plugins.adventurer_s1.services.s1_item_use_condition_service import build_use_condition_map, is_catalog_immutable_item
                        item_name = str(record.name or '').strip()
                        cond = build_use_condition_map({item_name}).get(item_name) if item_name else None
                        if is_catalog_immutable_item(cond):
                            logger.warning('数据申诉 apply 拦截不可变物品删除 %s id=%s appeal=%s', rtype, rid, appeal_id)
                            continue
                        from plugins.adventurer_s1.services.s1_item_instance_service import remove_item_row
                        _, del_err = remove_item_row(record, source_type='data_appeal_ai', description=f"数据申诉删除（#{appeal_id or '-'}）", hidden_from_player=False)
                    else:
                        ok, del_err = db_manager.delete_instance(record)
                        if not ok and (not del_err):
                            del_err = f'删除 {rtype} id={rid} 失败'
                    if del_err:
                        return del_err
                    _audit('delete', rtype, rid, before_full, None)
                return None
            if section == 'skills':
                del_err = _process_deletes()
                if del_err:
                    return (None, del_err)
            for upd in sec.get('update') or []:
                if not isinstance(upd, dict) or upd.get('id') is None:
                    logger.warning('数据申诉 apply 跳过无效 update %s（缺少 id）appeal=%s', rtype, appeal_id)
                    continue
                try:
                    rid = int(upd['id'])
                except (TypeError, ValueError):
                    logger.warning('数据申诉 apply 跳过无效 update %s id=%r appeal=%s', rtype, upd.get('id'), appeal_id)
                    continue
                record = db_manager.query(model).filter(model.id == rid, model.adventurer_id == adventurer_id).with_for_update().first()
                if not record:
                    frozen_target = any((isinstance(row, dict) and _normalize_appeal_patch_integer(row.get('id'), minimum=1) == rid for row in snapshot_before.get(section) or []))
                    if isinstance(execution_status_out, dict) and rtype in ('equipment', 'warehouse') and frozen_target:
                        label = '装备' if rtype == 'equipment' else '仓库物品'
                        issue = {'code': _DATA_APPEAL_STALE_ASSET_TARGET_CODE, 'detail': f'冻结{label}记录 id={rid} 已不在申诉提交时的位置；后续流转后的当前记录不能按名称替代旧 ID 写入'}
                        execution_status_out['terminal_operation_error'] = issue
                        return (None, issue['detail'])
                    logger.info('数据申诉 apply 跳过失效更新 %s id=%s（记录已不存在）appeal=%s', rtype, rid, appeal_id)
                    continue
                before_full = AdventurerS1DataAppealService._full_record_columns(record)
                old_record_name = str(getattr(record, 'name', '') or '').strip()
                field_key = rtype
                allowed = _PATCH_UPDATE_FIELDS[field_key]
                safe_upd = dict(upd)
                if field_key in ('equipment', 'warehouse'):
                    from plugins.adventurer_s1.services.s1_item_use_condition_service import build_use_condition_map, is_catalog_immutable_item
                    item_name = str(record.name or '').strip()
                    cond = build_use_condition_map({item_name}).get(item_name) if item_name else None
                    if is_catalog_immutable_item(cond):
                        blocked = [k for k in safe_upd if k != 'id']
                        if blocked:
                            logger.warning('数据申诉 apply 拦截不可变物品字段 %s %s id=%s appeal=%s', rtype, blocked, rid, appeal_id)
                        safe_upd = {'id': rid}
                    if 'name' in safe_upd:
                        catalog_row = db_manager.query(AdventurerS1Item).filter(AdventurerS1Item.name == item_name).first()
                        cosmetic_update = AdventurerS1DataAppealService._appeal_allowed_item_name_update(record.name, safe_upd.get('name'))
                        objective_authorized = True
                        frozen_name = frozen_item_names.get(rtype, {}).get(rid, '')
                        frozen_matches_current = bool(frozen_name and frozen_name == item_name)
                        stale_objective_update = not cosmetic_update and objective_authorized and (not frozen_matches_current)
                        unauthorized_catalog_update = not cosmetic_update and bool(catalog_row) and (not objective_authorized)
                        if stale_objective_update or unauthorized_catalog_update:
                            logger.warning('数据申诉 apply 拦截物品实质改名 %s id=%s appeal=%s objective_authorized=%s frozen_matches_current=%s catalog_bound=%s', rtype, rid, appeal_id, objective_authorized, frozen_matches_current, bool(catalog_row))
                            safe_upd.pop('name', None)
                    if 'custom_desc' in safe_upd:
                        frozen_custom_desc = frozen_item_custom_descs.get(rtype, {}).get(rid, '')
                        current_custom_desc = str(record.custom_desc or '')
                        if current_custom_desc != frozen_custom_desc:
                            rebased_description = rebase_s1_concurrent_public_description(frozen_custom_desc, current_custom_desc, safe_upd.get('custom_desc'))
                            if rebased_description is None:
                                label = '装备' if rtype == 'equipment' else '仓库物品'
                                issue = {'code': _DATA_APPEAL_STALE_ASSET_DESCRIPTION_CODE, 'detail': f'冻结{label}记录 id={rid} 的描述已发生后续修改；当前内容无法与本次申诉修改无损合并'}
                                if isinstance(execution_status_out, dict):
                                    execution_status_out['terminal_operation_error'] = issue
                                return (None, issue['detail'])
                            safe_upd['custom_desc'] = rebased_description
                            logger.info('数据申诉已合并并发物品描述更新 %s id=%s appeal=%s', rtype, rid, appeal_id)
                if rid in (preserve_unchanged_update_ids or {}).get(section, set()):
                    comparable_fields = {key: value for key, value in safe_upd.items() if key != 'id' and key in allowed}
                    if not comparable_fields or any((before_full.get(key) != value for key, value in comparable_fields.items())):
                        return (None, f'已落账合并目标 {rtype} id={rid} 与回执最终值不一致，不能把它作为删除补偿锚点')
                trigger_recovery = safe_upd.pop('trigger_recovery', False) is True
                recovery_evidence_indexes = safe_upd.pop('recovery_evidence_event_indexes', [])
                if len(safe_upd) <= 1 and (not trigger_recovery):
                    continue
                if field_key == 'skill' and 'name' in safe_upd:
                    merged, merge_err = AdventurerS1DataAppealService._try_merge_skill_on_name_collision(adventurer_id, rid, record, safe_upd, allowed, _audit, appeal_id=appeal_id)
                    if merge_err:
                        return (None, merge_err)
                    if merged:
                        continue
                trait_description_update_present = bool(field_key == 'trait' and 'description' in safe_upd)
                trait_description_update = safe_upd.get('description') if trait_description_update_present else None
                for k, v in safe_upd.items():
                    if k == 'id' or k not in allowed:
                        continue
                    if field_key == 'trait' and k == 'description':
                        continue
                    if k == 'evolution_stage':
                        from plugins.adventurer_s1.s1_item_types import clamp_evolution_stage
                        proposed = max(0, int(v))
                        clamped = clamp_evolution_stage(getattr(record, 'evolution_stage', 0), proposed)
                        if clamped != proposed:
                            logger.info('数据申诉 apply 拦截装备降阶 %s id=%s %s→%s appeal=%s', rtype, rid, proposed, clamped, appeal_id)
                        v = clamped
                    setattr(record, k, v)
                if trigger_recovery and field_key in ('equipment', 'skill'):
                    if recovery_trigger_time is not None and isinstance(recovery_evidence_indexes, list) and recovery_evidence_indexes and (int(getattr(record, 'recovery_minutes', 0) or 0) > 0) and (getattr(record, 'recovery_started_at', None) is None) and (getattr(record, 'recovery_ends_at', None) is None):
                        from plugins.adventurer_s1.services.s1_asset_recovery_service import start_asset_recovery
                        start_asset_recovery(record, now=recovery_trigger_time)
                    else:
                        logger.warning('数据申诉跳过不满足冻结条件的恢复触发 %s id=%s appeal=%s', rtype, rid, appeal_id)
                if field_key in ('equipment', 'warehouse') and 'custom_desc' in safe_upd and (not (record.custom_desc or '').strip()):
                    from plugins.adventurer_s1.services.s1_item_use_condition_service import build_use_condition_map, is_catalog_immutable_item
                    item_name = str(record.name or '').strip()
                    cond = build_use_condition_map({item_name}).get(item_name)
                    if appeal_item_catalog_desc_map.get(item_name):
                        record.custom_desc = None
                    elif not is_catalog_immutable_item(cond):
                        record.custom_desc = record.name or ''
                if field_key == 'trait':
                    AdventurerS1DataAppealService._sync_trait_recovery_expiry(record)
                    if adventurer_for_recovery_baseline is None:
                        adventurer_for_recovery_baseline = db_manager.query(AdventurerS1).filter(AdventurerS1.id == adventurer_id).with_for_update().first()
                    if adventurer_for_recovery_baseline is not None:
                        maybe_set_recovery_rest_baseline(record, adventurer_for_recovery_baseline, had_natural_dorm_recovery=trait_has_natural_dorm_recovery(before_full))
                    if trait_description_update_present:
                        from plugins.adventurer_s1.services.s1_trait_description_ai_service import sync_trait_description_fields
                        sync_trait_description_fields(record, trait_description_update or '')
                new_record_name = str(getattr(record, 'name', '') or '').strip()
                if field_key in ('equipment', 'warehouse', 'skill') and old_record_name and new_record_name and (old_record_name != new_record_name):
                    trait_asset_rename_changes.append({'asset_kind': 'skill' if field_key == 'skill' else 'equipment', 'asset_id': rid, 'asset_location': field_key, 'old_name': old_record_name, 'new_name': new_record_name})
                after_full = AdventurerS1DataAppealService._full_record_columns(record)
                changed = {k: after_full[k] for k in after_full if before_full.get(k) != after_full[k] and k not in ('id', 'adventurer_id', 'created_at', 'updated_at')}
                if changed:
                    _audit('update', rtype, rid, before_full, changed)
            if section != 'skills':
                del_err = _process_deletes()
                if del_err:
                    return (None, del_err)
        if trait_asset_rename_changes:
            from plugins.adventurer_s1.services.s1_trait_asset_dependency_service import sync_trait_asset_renames
            sync_trait_asset_renames(adventurer_id, trait_asset_rename_changes)
        adventurer = db_manager.query(AdventurerS1).filter(AdventurerS1.id == adventurer_id).with_for_update().first()
        if not adventurer:
            return (None, '冒险者不存在')
        from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
        _dest_model = {'equipment': AdventurerS1Equipment, 'warehouse': AdventurerS1WarehouseItem}
        expedition_log_id = exp_log_id
        consumption_candidates = {str(row.get('name') or '').strip(): row for row in (expedition_evidence or {}).get('departure_consumption_candidates') or [] if isinstance(row, dict) and str(row.get('name') or '').strip()}
        appeal_has_coupled_distribution = bool(appeal_row and any((isinstance(source, dict) and any((source.get(key) for key in ('team_pool', 'tavern_subsidy', 'reclaim_items'))) for source in (appeal_row.proposed_patch, appeal_row.review_supplement_patch))))
        isolate_create_item_failure = bool(appeal_row and (not appeal_has_coupled_distribution) and AdventurerS1DataAppealService._create_item_failure_can_be_isolated(normalized))
        create_item_execution_skips: list[dict] = []
        for item in normalized.get('create_items') or []:
            settlement_meta = AdventurerS1DataAppealService._lookup_expedition_settlement_item_meta_from_log(source_expedition_log, adventurer_id, item['name'])
            departure_restore_meta = None
            catalog = db_manager.query(AdventurerS1Item).filter(AdventurerS1Item.name == item['name']).first()
            ai_desc = AdventurerS1DataAppealService._create_item_ai_desc(item)
            use_ai_desc = AdventurerS1DataAppealService._should_apply_ai_custom_desc_for_catalog_create(item, catalog, snapshot_before=snapshot_before, expedition_evidence=expedition_evidence)
            grant_type, grant_rarity, grant_desc, sourced_from_catalog = AdventurerS1DataAppealService._resolve_create_item_grant_fields(item, settlement_meta=settlement_meta, catalog=catalog, use_ai_desc=use_ai_desc)
            if departure_restore_meta:
                departure_type = str(departure_restore_meta.get('type') or '').strip()
                departure_rarity = str(departure_restore_meta.get('rarity') or '').strip().lower()
                departure_desc = str(departure_restore_meta.get('description') or departure_restore_meta.get('custom_desc') or '').strip()
                if departure_type:
                    grant_type = departure_type
                if departure_rarity in _VALID_RARITIES:
                    grant_rarity = departure_rarity
                catalog_desc = str(getattr(catalog, 'description', None) or '').strip() if catalog is not None else ''
                grant_desc = '' if departure_desc == catalog_desc else departure_desc
            grant_uses_left = departure_restore_meta.get('uses_left') if departure_restore_meta and 'uses_left' in departure_restore_meta else item.get('uses_left')
            grant_evolution_stage = departure_restore_meta.get('evolution_stage') if departure_restore_meta and 'evolution_stage' in departure_restore_meta else item.get('evolution_stage')
            grant_recovery_minutes = max(0, int(item.get('recovery_minutes') or 0), 0)
            candidate = consumption_candidates.get(str(item.get('name') or '').strip())
            proposed_type = str(item.get('type') or '').strip().lower()
            try:
                proposed_quantity = int(item.get('quantity') or 1)
            except (TypeError, ValueError):
                proposed_quantity = 0
            if candidate and AdventurerS1DataAppealService._review_authorizes_consumption_restore(appeal_row, item_name=item['name']):
                old_type = str(candidate.get('current_type') or '').strip().lower()
                candidate_desc = str(candidate.get('authoritative_description') or '').strip()
                try:
                    candidate_uses = int(candidate.get('uses_left')) if candidate.get('uses_left') is not None else None
                except (TypeError, ValueError):
                    candidate_uses = None
                if bool(candidate.get('was_consumed')) and candidate_desc and (proposed_quantity == 1) and (proposed_type in _APPEAL_SAFE_ITEM_TYPE_UPDATES) and (old_type in S1_CONSUMABLE_ITEM_TYPES or (candidate_uses is not None and candidate_uses > 0)):
                    grant_type = proposed_type
            catalog_visible_desc = str(getattr(catalog, 'description', None) or '').strip() if catalog is not None else ''
            if not grant_desc and (not catalog_visible_desc):
                return (None, f"物品「{item['name']}」补发缺少可展示描述（非图鉴还原且无法从探险结算回填）；请补写 description 后重试")
            existing_by_destination: dict[str, object | None] = {}
            for destination, dest_model in _dest_model.items():
                existing_by_destination[destination] = db_manager.query(dest_model).filter(dest_model.adventurer_id == adventurer_id, dest_model.name == item['name']).first()
            dest = str(item.get('destination') or 'warehouse').strip().lower()
            existing_in_dest = existing_by_destination.get(dest)
            if existing_in_dest is not None:
                from plugins.adventurer_s1.s1_item_types import companion_allows_duplicate_name_instances
                if not companion_allows_duplicate_name_instances(grant_type) and departure_restore_meta is None and use_ai_desc and ai_desc:
                    before_full = AdventurerS1DataAppealService._full_record_columns(existing_in_dest)
                    existing_in_dest.custom_desc = ai_desc
                    existing_in_dest.recovery_minutes = max(int(existing_in_dest.recovery_minutes or 0), grant_recovery_minutes)
                    _audit('update', dest, int(existing_in_dest.id), before_full, AdventurerS1DataAppealService._full_record_columns(existing_in_dest), note=item.get('reason') or '同名升级：以 AI 描述覆盖')
                    continue
            grant_payload = {'type': grant_type, 'rarity': grant_rarity, 'quantity': item.get('quantity') or 1, 'uses_left': grant_uses_left, 'desc': grant_desc, 'recovery_minutes': grant_recovery_minutes}
            if grant_evolution_stage is not None:
                grant_payload['evolution_stage'] = grant_evolution_stage
            item_savepoint = db_manager.begin_nested() if isolate_create_item_failure else None
            try:
                actual_destination, grant_err = grant_raw_item(adventurer, item['name'], grant_payload, source_type='data_appeal_ai', description=item.get('reason') or f"数据申诉补发（#{appeal_id or '-'}）", force_destination=item['destination'], related_id=str(expedition_log_id) if expedition_log_id is not None else None, related_type='expedition_log' if expedition_log_id is not None else None)
                if item_savepoint is not None:
                    if grant_err:
                        item_savepoint.rollback()
                    else:
                        item_savepoint.commit()
            except Exception:
                if item_savepoint is not None:
                    item_savepoint.rollback()
                raise
            if grant_err:
                if isolate_create_item_failure:
                    create_item_execution_skips.append({'name': str(item.get('name') or '').strip(), 'reason': str(grant_err)})
                    continue
                return (None, grant_err)
            actual_destination = actual_destination or item['destination']
            was_new = existing_by_destination.get(actual_destination) is None
            granted_model = _dest_model.get(actual_destination)
            granted_record = db_manager.query(granted_model).filter(granted_model.adventurer_id == adventurer_id, granted_model.name == item['name']).order_by(granted_model.id.desc()).first() if granted_model is not None else None
            _audit('create_item', 'item', int(granted_record.id) if granted_record else None, None, {'destination': actual_destination, 'requested_destination': item['destination'], 'name': item['name'], 'type': grant_type, 'rarity': grant_rarity, 'quantity': item.get('quantity') or 1, 'uses_left': grant_uses_left, 'evolution_stage': grant_evolution_stage, 'recovery_minutes': grant_recovery_minutes, 'sourced_from_catalog': sourced_from_catalog, 'custom_desc': grant_desc or None, 'reason': item.get('reason') or '', 'was_new': was_new}, note=item.get('reason'))
        for trait in normalized.get('create_traits') or []:
            existing = db_manager.query(AdventurerS1Trait).filter(AdventurerS1Trait.adventurer_id == adventurer_id, AdventurerS1Trait.name == trait['name']).with_for_update().first()
            if existing:
                before_full = AdventurerS1DataAppealService._full_record_columns(existing)
                updated_description = trait.get('description') or existing.description or ''
                existing.is_archived = False
                existing.recovery_policy = trait.get('recovery_policy') or 'none'
                existing.recovery_stage = trait.get('recovery_stage')
                existing.recovery_progress = 0
                existing.recovery_required = trait.get('recovery_required')
                existing.recovery_hours = trait.get('recovery_hours')
                existing.recovered_desc = trait.get('recovered_desc')
                AdventurerS1DataAppealService._sync_trait_recovery_expiry(existing)
                if adventurer_for_recovery_baseline is None:
                    adventurer_for_recovery_baseline = db_manager.query(AdventurerS1).filter(AdventurerS1.id == adventurer_id).with_for_update().first()
                if adventurer_for_recovery_baseline is not None:
                    maybe_set_recovery_rest_baseline(existing, adventurer_for_recovery_baseline, had_natural_dorm_recovery=trait_has_natural_dorm_recovery(before_full))
                from plugins.adventurer_s1.services.s1_trait_description_ai_service import sync_trait_description_fields
                sync_trait_description_fields(existing, updated_description)
                after_full = AdventurerS1DataAppealService._full_record_columns(existing)
                changed = {k: after_full[k] for k in after_full if before_full.get(k) != after_full[k] and k not in ('id', 'adventurer_id', 'created_at', 'updated_at')}
                if changed:
                    _audit('update', 'trait', int(existing.id), before_full, changed, note=trait.get('reason') or '同名特质补发合并为更新')
                continue
            record = AdventurerS1Trait(adventurer_id=adventurer_id, name=trait['name'], description=trait.get('description') or '', is_archived=False, recovery_policy=trait.get('recovery_policy') or 'none', recovery_stage=trait.get('recovery_stage'), recovery_progress=0, recovery_required=trait.get('recovery_required'), recovery_hours=trait.get('recovery_hours'), recovered_desc=trait.get('recovered_desc'))
            AdventurerS1DataAppealService._sync_trait_recovery_expiry(record)
            if adventurer_for_recovery_baseline is None:
                adventurer_for_recovery_baseline = db_manager.query(AdventurerS1).filter(AdventurerS1.id == adventurer_id).with_for_update().first()
            if adventurer_for_recovery_baseline is not None:
                maybe_set_recovery_rest_baseline(record, adventurer_for_recovery_baseline, had_natural_dorm_recovery=False)
            ok, add_err = db_manager.add(record)
            if not ok:
                return (None, add_err or f"补发特质「{trait['name']}」失败")
            from plugins.adventurer_s1.services.s1_trait_description_ai_service import sync_trait_description_fields
            sync_trait_description_fields(record, record.description or '')
            _audit('create_trait', 'trait', None, None, {'name': trait['name'], 'description': trait.get('description') or '', 'is_archived': False, 'recovery_policy': record.recovery_policy or 'none', 'recovery_stage': record.recovery_stage, 'recovery_progress': int(record.recovery_progress or 0), 'recovery_required': record.recovery_required, 'recovery_hours': record.recovery_hours, 'recovery_expires_at': record.recovery_expires_at.isoformat() if record.recovery_expires_at else None, 'recovered_desc': record.recovered_desc, 'reason': trait.get('reason') or ''}, note=trait.get('reason'))
        for skill in normalized.get('create_skills') or []:
            equipped = _normalize_appeal_patch_boolean(skill.get('equipped', True))
            if equipped is None:
                return (None, f"补发技能「{skill.get('name') or ''}」的 equipped 格式无效")
            existing = db_manager.query(AdventurerS1Skill).filter(AdventurerS1Skill.adventurer_id == adventurer_id, AdventurerS1Skill.name == skill['name']).with_for_update().first()
            if existing:
                before_full = AdventurerS1DataAppealService._full_record_columns(existing)
                if skill.get('description'):
                    existing.description = skill.get('description') or ''
                if 'equipped' in skill:
                    existing.equipped = equipped
                existing.recovery_minutes = max(int(existing.recovery_minutes or 0), int(skill.get('recovery_minutes') or 0))
                after_full = AdventurerS1DataAppealService._full_record_columns(existing)
                changed = {k: after_full[k] for k in after_full if before_full.get(k) != after_full[k] and k not in ('id', 'adventurer_id', 'created_at', 'updated_at')}
                if changed:
                    _audit('update', 'skill', int(existing.id), before_full, changed, note=skill.get('reason') or '同名技能补发合并为更新')
                continue
            record = AdventurerS1Skill(adventurer_id=adventurer_id, name=skill['name'], description=skill.get('description') or '', equipped=equipped, recovery_minutes=max(0, int(skill.get('recovery_minutes') or 0)))
            ok, add_err = db_manager.add(record)
            if not ok:
                return (None, add_err or f"补发技能「{skill['name']}」失败")
            _audit('create_skill', 'skill', None, None, {'name': skill['name'], 'description': skill.get('description') or '', 'equipped': equipped, 'recovery_minutes': max(0, int(skill.get('recovery_minutes') or 0)), 'reason': skill.get('reason') or ''}, note=skill.get('reason'))
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        for adjustment in normalized.get('currency_adjustments') or []:
            split_result = {}
            new_balance, currency_err = S1CurrencyService.add(adventurer_id, adjustment['currency_code'], adjustment['amount'], source_type='data_appeal_ai', related_id=appeal_id, related_type='data_appeal', description=adjustment.get('reason') or '数据申诉补发', auto_commit=False, split_result=split_result)
            if currency_err:
                return (None, currency_err)
            _audit('currency', 'currency', None, None, {'currency_code': adjustment['currency_code'], 'amount': adjustment['amount'], 'balance_after': new_balance, 'gold_credited': split_result.get('gold_credited', 0.0), 'low_consumption_credited': split_result.get('low_consumption_credited', 0.0)}, note=adjustment.get('reason'))
        if normalized.get('unassigned_items'):
            from plugins.adventurer_s1.services.s1_team_loot_service import S1TeamLootService, TeamLootError
            from plugins.adventurer_s1.services.s1_team_loot_contract import team_loot_registration_hash
            if appeal_row is None:
                return (None, '共同实物缺少申诉与独立复核来源')
            try:
                loot_ids = S1TeamLootService.register_appeal_in_transaction(appeal_row, normalized['unassigned_items'])
            except TeamLootError as error:
                return (None, str(error))
            for row, lot_id in zip(normalized['unassigned_items'], loot_ids):
                _audit('unassigned_item', 'team_loot_lot', lot_id, None, {'name': row['name'], 'expedition_log_id': appeal_row.expedition_log_id, 'registration_hash': team_loot_registration_hash(row), 'source_event_index': row['source_event_index'], 'quantity': row['item']['quantity']}, note='共同实物进入本次出发小队待分配池')
        if create_item_execution_skips:
            if not audit_rows:
                return (None, create_item_execution_skips[0]['reason'])
            AdventurerS1DataAppealService._record_create_item_execution_skips(appeal_row, create_item_execution_skips)
        if audit_rows:
            ok, audit_err = db_manager.add_all(audit_rows)
            if not ok:
                return (None, audit_err or '审计记录写入失败')
        if appeal_id is not None and AdventurerS1DataAppealService._patch_has_record_updates_or_creates(normalized) and (len(audit_rows) == 0):
            pre_apply_satisfied = appeal_row and isinstance(all_player_records, dict) and AdventurerS1DataAppealService._appeal_record_patch_already_satisfied(appeal_row, normalized, all_player_records)
            if pre_apply_satisfied:
                post_apply_records = AdventurerS1DataAppealService._build_all_player_records(adventurer_id, include_internal_recovery_state=True)
                if AdventurerS1DataAppealService._appeal_record_patch_already_satisfied(appeal_row, normalized, post_apply_records):
                    logger.info('数据申诉 patch 在执行前后均已由权威状态完整满足，零审计幂等收口 appeal=%s', appeal_id)
                    return (0, None)
            return (None, AdventurerS1DataAppealService._apply_patch_zero_audit_error())
        return (len(audit_rows), None)

    @staticmethod
    def _sync_trait_recovery_expiry(trait: AdventurerS1Trait) -> None:
        from plugins.adventurer_s1.services.adventurer_s1_trait_recovery_service import NATURAL_RECOVERY_POLICIES, _coerce_trace_hours
        stage = (trait.recovery_stage or '').strip().lower()
        policy = (trait.recovery_policy or 'none').strip().lower()
        if policy in NATURAL_RECOVERY_POLICIES and stage == 'trace':
            hours = _coerce_trace_hours(trait.recovery_hours or 2)
            trait.recovery_hours = hours
            trait.recovery_expires_at = _portable_utc_now() + timedelta(hours=hours)
            trait.recovery_progress = 0
            trait.recovery_required = None
            trait.recovered_desc = None
        else:
            trait.recovery_hours = None
            trait.recovery_expires_at = None

    @staticmethod
    def _notify_player_result(appeal: AdventurerS1DataAppeal, *, approved: bool, auto: bool=False):
        from models.mailbox import Mailbox
        if approved:
            core_satisfied = AdventurerS1DataAppealService._resolve_core_request_satisfied(appeal)
            appeal.core_request_satisfied = core_satisfied
            review_raw = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
            request_results = review_raw.get('request_results')
            has_incomplete_request = bool(isinstance(request_results, list) and any((isinstance(row, dict) and str(row.get('status') or '').strip().lower() != 'done' for row in request_results)))
            if core_satisfied is False:
                title = f'数据申诉已处理（#{appeal.id}）'
                reason = appeal._player_final_reason() or AdventurerS1DataAppealService.get_suggested_reject_reason(appeal)
                content = f'您提交的数据异常申诉已处理：本次只对部分数据做了整理，您申诉的核心诉求未完全满足。\n\n{reason}'
            elif has_incomplete_request:
                title = f'数据申诉已处理（#{appeal.id}）'
                reason = appeal._player_final_reason() or '请在申诉记录中查看每条诉求的独立结果。'
                content = f'您提交的数据异常申诉已逐条处理：部分诉求已完成，其它诉求未通过或在最终写入时发生冲突。\n\n{reason}'
            else:
                title = f'数据申诉已通过（#{appeal.id}）'
                raw = AdventurerS1DataAppealService._parse_ai_raw_response(appeal) or {}
                custom = appeal._player_final_reason()
                if custom and raw.get('recovery_already_satisfied'):
                    content = f'您提交的数据异常申诉已通过陪审团复核。\n\n{custom}'
                elif custom:
                    content = f'您提交的数据异常申诉已通过陪审团复核并完成修复。\n\n{custom}'
                elif auto:
                    content = '您提交的数据异常申诉已通过陪审团复核并完成修复。'
                else:
                    content = '您提交的数据异常申诉已通过审核并完成修复。'
        else:
            display = AdventurerS1DataAppealService._enrich_cancellation_display(appeal, {})
            if display.get('processing_outcome'):
                title = f"数据申诉{display['status_label']}（#{appeal.id}）"
                content = display['outcome_hint'] + '\n\n请在申诉记录中查看逐项结果并继续处理。'
            else:
                title = f'数据申诉未通过（#{appeal.id}）'
                content = f'您提交的数据异常申诉未通过审核。\n\n原因：{appeal.reject_reason}'
        mail = Mailbox(user_id=appeal.user_id, title=title, content=content, sender_id=None, sender_name='深渊酒馆')
        db_manager.add(mail)

def json_dumps_safe(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)
