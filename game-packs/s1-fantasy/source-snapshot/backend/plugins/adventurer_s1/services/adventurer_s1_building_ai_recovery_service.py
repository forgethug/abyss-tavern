"""S1 建筑异步 AI recovery：重启 / worker 崩溃后补投，超时则中止并退还费用 + CD。"""
from runtime_core.time import utc_now as _portable_utc_now

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingAiFailureAudit,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
    AdventurerS1BuildingUsageLog,
    AdventurerS1CurrencyLog,
    AdventurerS1ItemLog,
)
from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
    AdventurerS1StateTransitionCoordinator,
)
from plugins.adventurer_s1.services.s1_building_ai_ownership_service import (
    building_ai_owner_is_current,
    stamp_current_building_ai_owner,
)

logger = logging.getLogger(__name__)
_BUILDING_AI_RECOVERY_LOCK = threading.Lock()
_building_ai_recovery_cursor = 0

_ITEM_ASSEMBLY_REQUEST_AUDIT_KEYS = (
    'request_id', 'request_hash', 'prompt_hash', 'system_prompt_hash',
    'contract_hash', 'model', 'provider', 'profile_id', 'thinking_mode',
    'json_object_requested', 'json_object_mode', 'finish_reason',
)

_PET_EVOLUTION_NAME_CONFLICT_REASON_CODES = frozenset({
    'catalog_identity_conflict',
    'world_unique_conflict',
})
_PET_EVOLUTION_NAME_CONFLICT_STAGES = frozenset({
    'preflight',
    'transaction_recheck',
})
_PET_EVOLUTION_NAME_RETRY_AUDIT_HARD_LIMIT = 1
_PET_EVOLUTION_NAME_CONFLICT_AUDIT_LIMIT = 2


def _project_item_assembly_request_audit(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {
        key: str(raw[key])
        for key in _ITEM_ASSEMBLY_REQUEST_AUDIT_KEYS
        if raw.get(key) not in (None, '')
    }


def _build_item_assembly_failure_diagnostics(extra: dict) -> dict:
    """保留终止前的非敏感 checkpoint，不持久化原始 Prompt 或模型正文。"""
    diagnostics = {
        'input_snapshot_version': extra.get('input_snapshot_version'),
        'input_snapshot_hash': str(extra.get('input_snapshot_hash') or ''),
    }
    raw_checkpoint = extra.get('assembly_ai_checkpoint')
    if not isinstance(raw_checkpoint, dict):
        diagnostics['checkpoint_present'] = False
        return diagnostics

    checkpoint = {
        'version': raw_checkpoint.get('version'),
        'revision': raw_checkpoint.get('revision'),
        'stage': str(raw_checkpoint.get('stage') or ''),
        'generation_attempt': raw_checkpoint.get('generation_attempt'),
        'generation_request': _project_item_assembly_request_audit(
            raw_checkpoint.get('generation_request'),
        ),
    }
    if raw_checkpoint.get('version') == 3:
        checkpoint['review_attempt'] = raw_checkpoint.get('review_attempt')
        checkpoint['review_request'] = _project_item_assembly_request_audit(
            raw_checkpoint.get('review_request'),
        )
    retry_reason_code = raw_checkpoint.get('retry_reason_code')
    if retry_reason_code:
        checkpoint['retry_reason_code'] = str(retry_reason_code)[:100]

    attempt_history = []
    for raw_entry in raw_checkpoint.get('attempt_history') or []:
        if not isinstance(raw_entry, dict):
            continue
        entry = {
            'stage': str(raw_entry.get('stage') or ''),
            'attempt': raw_entry.get('attempt'),
            'outcome': str(raw_entry.get('outcome') or ''),
            'request': _project_item_assembly_request_audit(raw_entry.get('request')),
        }
        if raw_entry.get('failure_code'):
            entry['failure_code'] = str(raw_entry['failure_code'])[:100]
        if raw_entry.get('reason_code'):
            entry['reason_code'] = str(raw_entry['reason_code'])[:100]
        attempt_history.append(entry)
    checkpoint['attempt_history'] = attempt_history

    if checkpoint['stage'] == 'approved' and isinstance(raw_checkpoint.get('candidate'), dict):
        candidate = raw_checkpoint['candidate']
        checkpoint['approved_candidate'] = {
            key: candidate.get(key)
            for key in (
                'success', 'new_name', 'new_type', 'new_desc', 'new_rarity',
                'narrative', 'consumed_slots', 'unreasonable_input',
            )
        }
    if (
        raw_checkpoint.get('version') == 3
        and checkpoint['stage'] == 'approved'
        and isinstance(raw_checkpoint.get('review'), dict)
    ):
        checkpoint['review'] = {
            'decision': str(raw_checkpoint['review'].get('decision') or ''),
            'reason_code': str(raw_checkpoint['review'].get('reason_code') or ''),
        }
    authority = raw_checkpoint.get('authority_review')
    if isinstance(authority, dict):
        payload = authority.get('payload') if isinstance(authority.get('payload'), dict) else {}
        review = authority.get('review') if isinstance(authority.get('review'), dict) else {}
        checkpoint['authority_review'] = {
            'version': authority.get('version'),
            'input_hash': payload.get('input_hash'),
            'decision': review.get('decision'),
            'attempts': [{
                'outcome': str(row.get('outcome') or ''),
                'failure_code': str(row.get('failure_code') or '')[:100],
                'request': _project_item_assembly_request_audit(row.get('request')),
            } for row in authority.get('attempts') or [] if isinstance(row, dict)],
        }
    diagnostics['checkpoint_present'] = True
    diagnostics['checkpoint'] = checkpoint
    return diagnostics


def _build_ember_kitchen_failure_diagnostics(extra: dict) -> dict:
    """保留烹饪技术中止的最小冻结回执，不落原始 Prompt 或候选正文。"""
    from plugins.adventurer_s1.services.s1_item_instance_service import (
        item_refs_total,
    )

    return {
        'input_snapshot_version': extra.get('input_snapshot_version'),
        'input_snapshot_hash': str(extra.get('input_snapshot_hash') or ''),
        'cookware_code': str(extra.get('cookware_code') or ''),
        'ingredient_count': item_refs_total(extra.get('item_refs') or []),
    }


def _project_pet_evolution_failure_diagnostics(raw: object) -> dict:
    """只保留唯一名冲突的最小内部证据，不把原始 Prompt 或模型正文写入审计。"""
    source = raw if isinstance(raw, dict) else {}
    try:
        retry_limit = int(source.get('retry_limit') or 0)
    except (TypeError, ValueError):
        retry_limit = 0
    diagnostics = {
        'failure_kind': 'pet_evolution_name_conflict',
        'retry_attempted': source.get('retry_attempted') is True,
        'retry_limit': max(0, min(_PET_EVOLUTION_NAME_RETRY_AUDIT_HARD_LIMIT, retry_limit)),
        'blocked_candidates': [],
    }
    raw_candidates = source.get('blocked_candidates')
    if not isinstance(raw_candidates, list):
        return diagnostics
    for raw_candidate in raw_candidates[:_PET_EVOLUTION_NAME_CONFLICT_AUDIT_LIMIT]:
        if not isinstance(raw_candidate, dict):
            continue
        proposed_name = str(raw_candidate.get('proposed_name') or '').strip()[:100]
        reason_code = str(raw_candidate.get('reason_code') or '').strip()
        source_stage = str(raw_candidate.get('source_stage') or '').strip()
        try:
            attempt = int(raw_candidate.get('attempt') or 0)
        except (TypeError, ValueError):
            attempt = 0
        if (
            not proposed_name
            or attempt not in (1, 2)
            or reason_code not in _PET_EVOLUTION_NAME_CONFLICT_REASON_CODES
            or source_stage not in _PET_EVOLUTION_NAME_CONFLICT_STAGES
        ):
            continue
        diagnostics['blocked_candidates'].append({
            'attempt': attempt,
            'proposed_name': proposed_name,
            'reason_code': reason_code,
            'source_stage': source_stage,
        })
    return diagnostics

# handler_key → AsyncTaskService task_type
_ASYNC_HANDLER_TASK_TYPES = {
    'blacksmith': 'ai_blacksmith',
    'tailor_workshop': 'ai_tailor_workshop',
    'alchemy_workshop': 'ai_alchemy_workshop',
    'item_assembly_workshop': 'ai_item_assembly_workshop',
    'ember_kitchen': 'ai_ember_kitchen',
    'healing_house': 'ai_healing_house',
    'chapel': 'ai_chapel',
    'pet_evolution': 'ai_pet_evolution',
    'skill_school': 'ai_skill_training',
}


class AdventurerS1BuildingAiRecoveryService:
    """补投 / 安全中止卡在 building 态的异步建筑 AI 会话。"""

    @staticmethod
    def requeue_stuck_building_ai(
        stale_minutes: int = None,
        abort_minutes: int = None,
        *,
        startup_recovery: bool = False,
    ) -> dict:
        """有界轮转恢复建筑 AI；批量预载且不跨 Redis 持有 DB。"""
        from flask import current_app
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        from sqlalchemy.orm import load_only, raiseload, selectinload

        global _building_ai_recovery_cursor

        empty_stats = {'requeued': 0, 'completed': 0, 'released': 0, 'aborted': 0}
        if stale_minutes is None:
            try:
                stale_minutes = int(
                    current_app.config.get('ADVENTURER_S1_BUILDING_AI_STALE_MINUTES', 3)
                )
            except (TypeError, ValueError):
                stale_minutes = 3
        if abort_minutes is None:
            try:
                abort_minutes = int(
                    current_app.config.get('ADVENTURER_S1_BUILDING_AI_ABORT_MINUTES', 30)
                )
            except (TypeError, ValueError):
                abort_minutes = 30

        stale_minutes = max(1, stale_minutes)
        abort_minutes = max(1, abort_minutes)
        try:
            batch_size = min(1000, max(1, int(current_app.config.get(
                'ADVENTURER_S1_BUILDING_AI_RECOVERY_BATCH_SIZE',
                100,
            ))))
        except (TypeError, ValueError):
            batch_size = 100
        try:
            budget_seconds = min(120, max(1, int(current_app.config.get(
                'ADVENTURER_S1_BUILDING_AI_RECOVERY_BUDGET_SECONDS',
                20,
            ))))
        except (TypeError, ValueError):
            budget_seconds = 20

        now = _portable_utc_now()
        started_at = time.monotonic()
        if not _BUILDING_AI_RECOVERY_LOCK.acquire(blocking=False):
            logger.info('S1 建筑 AI recovery 已在执行，本轮跳过')
            return empty_stats
        stats = dict(empty_stats)
        inspected = 0
        item_errors = 0
        budget_exhausted = False
        try:
            candidate_base = (
                db_manager.query(AdventurerS1.id)
                .join(
                    AdventurerS1BuildingInstance,
                    AdventurerS1.building_instance_id
                    == AdventurerS1BuildingInstance.id,
                )
                .filter(
                    AdventurerS1.status == 'building',
                    AdventurerS1.building_instance_id.isnot(None),
                    AdventurerS1BuildingInstance.status == 'built',
                )
            )
            candidate_rows = candidate_base.filter(
                AdventurerS1.id > _building_ai_recovery_cursor,
            ).order_by(AdventurerS1.id.asc()).limit(batch_size).all()
            if len(candidate_rows) < batch_size:
                candidate_rows.extend(
                    candidate_base.filter(
                        AdventurerS1.id <= _building_ai_recovery_cursor,
                    ).order_by(AdventurerS1.id.asc()).limit(
                        batch_size - len(candidate_rows),
                    ).all()
                )
            candidate_ids = [int(row[0]) for row in candidate_rows]

            if candidate_ids:
                instance_loader = (
                    selectinload(AdventurerS1.building_instance)
                    .load_only(
                        AdventurerS1BuildingInstance.id,
                        AdventurerS1BuildingInstance.template_id,
                        AdventurerS1BuildingInstance.status,
                    )
                    .selectinload(AdventurerS1BuildingInstance.template)
                    .load_only(
                        AdventurerS1BuildingTemplate.id,
                        AdventurerS1BuildingTemplate.handler_key,
                        AdventurerS1BuildingTemplate.category,
                        AdventurerS1BuildingTemplate.settlement_config,
                    )
                )
                loaded_rows = (
                    AdventurerS1.query
                    .filter(AdventurerS1.id.in_(candidate_ids))
                    .options(
                        # load_only 不会关闭默认 selectin；扫描不应顺带加载资产、
                        # 宿舍住客或模板下全部实例/帮工。只保留下方显式建筑链。
                        raiseload('*'),
                        load_only(
                            AdventurerS1.id,
                            AdventurerS1.status,
                            AdventurerS1.building_instance_id,
                            AdventurerS1.building_assigned_at,
                            AdventurerS1.building_usage_extra,
                            AdventurerS1.hp,
                            AdventurerS1.max_hp,
                            AdventurerS1.hp_updated_at,
                            AdventurerS1.dorm_room_id,
                        ),
                        instance_loader,
                    )
                    .limit(len(candidate_ids))
                    .all()
                )
                rows_by_id = {int(row.id): row for row in loaded_rows}
                rows = [
                    rows_by_id[adventurer_id]
                    for adventurer_id in candidate_ids
                    if adventurer_id in rows_by_id
                ]
            else:
                rows = []
            # All required relationships and scalar fields are loaded in a
            # fixed number of queries; per-item Redis/settlement starts detached.
            db_manager.remove()

            for adv in rows:
                if inspected and time.monotonic() - started_at >= budget_seconds:
                    budget_exhausted = True
                    break
                inspected += 1
                adventurer_id = int(adv.id)
                _building_ai_recovery_cursor = adventurer_id
                try:
                    building_instance_id = adv.building_instance_id
                    inst = adv.building_instance
                    if (
                        adv.status != 'building'
                        or building_instance_id is None
                        or inst is None
                        or int(inst.id) != int(building_instance_id)
                        or inst.status != 'built'
                        or inst.template is None
                    ):
                        continue
                    handler_key = inst.template.handler_key
                    task_type = _ASYNC_HANDLER_TASK_TYPES.get(handler_key)
                    if not task_type:
                        continue
                    extra = (
                        dict(adv.building_usage_extra)
                        if isinstance(adv.building_usage_extra, dict)
                        else {}
                    )
                    if handler_key == 'skill_school':
                        from plugins.adventurer_s1.services.s1_skill_school_service import (
                            S1SkillSchoolService,
                        )
                        # 经典目录传授仍是同步 handler，由通用自动结算负责。
                        if not S1SkillSchoolService.is_custom_training_extra(extra):
                            continue
                    if not adv.building_assigned_at:
                        continue

                    from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import (
                        BuildingSettlementService,
                    )
                    config = BuildingSettlementService.get_reward_config(inst)
                    duration_minutes = (
                        AdventurerS1Service._effective_usage_duration_minutes(
                            adv,
                            config,
                        )
                    )
                    assigned_at = adv.building_assigned_at
                    due_at = assigned_at + timedelta(
                        minutes=max(0, duration_minutes),
                    )
                    if now < due_at:
                        continue

                    stale_at = due_at + timedelta(minutes=stale_minutes)
                    abort_at = due_at + timedelta(minutes=abort_minutes)
                    if (
                        now < stale_at
                        and (
                            not startup_recovery
                            or building_ai_owner_is_current(extra)
                        )
                    ):
                        continue
                    usage_session_id = str(
                        extra.get('usage_session_id') or ''
                    ) or None
                    claimed_extra, owner_changed = (
                        AdventurerS1BuildingAiRecoveryService
                        ._claim_session_for_current_boot(
                            adventurer_id,
                            int(inst.id),
                            usage_session_id=usage_session_id,
                        )
                    )
                    db_manager.remove()
                    if claimed_extra is None:
                        continue
                    extra = claimed_extra
                    usage_results = (
                        db_manager.query(AdventurerS1BuildingUsageLog.result)
                        .filter(
                            AdventurerS1BuildingUsageLog.adventurer_id
                            == adventurer_id,
                            AdventurerS1BuildingUsageLog.building_instance_id
                            == int(inst.id),
                            AdventurerS1BuildingUsageLog.used_at >= assigned_at,
                        )
                        .order_by(AdventurerS1BuildingUsageLog.used_at.desc())
                        .limit(20)
                        .all()
                    )
                    db_manager.remove()
                    if usage_session_id is not None:
                        usage_exists = any(
                            isinstance(row[0], dict)
                            and str(row[0].get('usage_session_id') or '')
                            == usage_session_id
                            for row in usage_results
                        )
                    else:
                        usage_exists = bool(usage_results)
                    if usage_exists:
                        if AdventurerS1BuildingAiRecoveryService._release_only(
                            adventurer_id,
                            int(inst.id),
                            usage_session_id=usage_session_id,
                        ):
                            stats['released'] += 1
                        db_manager.remove()
                        continue

                    payload = AdventurerS1BuildingAiRecoveryService._build_task_payload(
                        handler_key,
                        adventurer_id,
                        int(inst.id),
                        extra,
                        config,
                    )
                    # 旧宠物会话缺少 evolution_stage 时，payload 重建会补查装备表。
                    # Redis 队列探测/补投前必须释放这次兜底查询占用的 scoped session。
                    db_manager.remove()
                    if now >= abort_at:
                        if AdventurerS1BuildingAiRecoveryService.abort_stuck_session(
                            adventurer_id,
                            int(inst.id),
                            handler_key,
                            reason='building_ai_timeout',
                            usage_session_id=usage_session_id,
                        ):
                            stats['aborted'] += 1
                        db_manager.remove()
                        continue

                    from services.async_task_service import AsyncTaskService

                    if (
                        payload
                        and startup_recovery
                        and AsyncTaskService.has_restart_recovered_queued_task(
                            task_type,
                            payload,
                        )
                    ):
                        # The exact interrupted delivery remains authoritative;
                        # the owner stamp above only fences the old process.
                        continue
                    if payload and owner_changed:
                        # 仅跨 boot 行锁接管者可以抢删旧性能锁。
                        AsyncTaskService.force_release_task_lock(task_type, payload)
                    if (
                        payload
                        and AsyncTaskService.is_task_enqueued_or_running(
                            task_type,
                            payload,
                        )
                    ):
                        continue
                    if payload and AsyncTaskService.submit_task(task_type, payload):
                        stats['requeued'] += 1
                        logger.info(
                            '建筑 AI 补投 adv=%s inst=%s handler=%s',
                            adventurer_id,
                            inst.id,
                            handler_key,
                        )
                        continue

                    _, err = AdventurerS1Service.complete_building_usage(
                        adventurer_id=adventurer_id,
                        building_instance_id=int(inst.id),
                        is_admin=True,
                        extra_params=extra or None,
                    )
                    db_manager.remove()
                    if not err:
                        stats['completed'] += 1
                        logger.info(
                            '建筑 AI recovery 触发 complete '
                            'adv=%s inst=%s handler=%s',
                            adventurer_id,
                            inst.id,
                            handler_key,
                        )
                        continue
                    if now >= abort_at:
                        if AdventurerS1BuildingAiRecoveryService.abort_stuck_session(
                            adventurer_id,
                            int(inst.id),
                            handler_key,
                            reason='building_ai_timeout',
                            usage_session_id=usage_session_id,
                        ):
                            stats['aborted'] += 1
                            logger.warning(
                                '建筑 AI 超时中止 adv=%s inst=%s '
                                'handler=%s complete_err=%s',
                                adventurer_id,
                                inst.id,
                                handler_key,
                                err,
                            )
                        db_manager.remove()
                    else:
                        logger.warning(
                            '建筑 AI recovery 暂无法处理 '
                            'adv=%s inst=%s handler=%s err=%s',
                            adventurer_id,
                            inst.id,
                            handler_key,
                            err,
                        )
                except Exception:
                    item_errors += 1
                    db_manager.rollback()
                    logger.exception(
                        '建筑 AI recovery 单项失败 adv=%s',
                        adventurer_id,
                    )
                finally:
                    db_manager.remove()

            if budget_exhausted:
                logger.warning(
                    'S1 建筑 AI recovery 达到单轮耗时预算 '
                    'inspected=%s candidates=%s batch_size=%s '
                    'budget_seconds=%s cursor=%s',
                    inspected,
                    len(rows),
                    batch_size,
                    budget_seconds,
                    _building_ai_recovery_cursor,
                )
            total = sum(stats.values())
            if total:
                logger.info('S1 建筑 AI recovery: %s', stats)
            if item_errors:
                raise RuntimeError(
                    f'S1 building AI recovery item failures: {item_errors}'
                )
            return stats
        finally:
            try:
                db_manager.remove()
            finally:
                _BUILDING_AI_RECOVERY_LOCK.release()

    @staticmethod
    def _build_task_payload(
        handler_key: str,
        adv_id: int,
        building_instance_id: int,
        extra: dict,
        config: dict,
    ) -> Optional[dict]:
        """从 building_usage_extra 重建 AsyncTask payload；参数不足时返回 None。"""
        if handler_key == 'blacksmith':
            from plugins.adventurer_s1.services.s1_blacksmith_forge_intent import (
                normalize_blacksmith_forge_intent,
            )
            item_refs = extra.get('item_refs') or []
            if not isinstance(item_refs, list) or len(item_refs) != 2:
                return None
            forge_intent, forge_intent_err = normalize_blacksmith_forge_intent(
                extra.get('forge_intent'),
            )
            if forge_intent_err:
                return None
            return {
                'adventurer_id': adv_id,
                'building_instance_id': building_instance_id,
                'item_refs': item_refs,
                'forge_intent': forge_intent,
                'usage_session_id': extra.get('usage_session_id') or None,
            }

        if handler_key == 'tailor_workshop':
            from plugins.adventurer_s1.services.s1_tailor_workshop_contract import (
                normalize_tailor_workshop_operation,
            )
            item_refs = extra.get('item_refs') or []
            if not isinstance(item_refs, list) or len(item_refs) != 2:
                return None
            tailor_operation, operation_err = normalize_tailor_workshop_operation(
                extra.get('tailor_operation'),
            )
            if operation_err:
                return None
            return {
                'adventurer_id': adv_id,
                'building_instance_id': building_instance_id,
                'item_refs': item_refs,
                'tailor_operation': tailor_operation,
                'usage_session_id': extra.get('usage_session_id') or None,
            }

        if handler_key == 'alchemy_workshop':
            from plugins.adventurer_s1.services.s1_item_instance_service import alchemy_item_refs_total
            item_refs = extra.get('item_refs') or []
            if not isinstance(item_refs, list) or not (1 <= alchemy_item_refs_total(item_refs) <= 5):
                return None
            return {
                'adventurer_id': adv_id,
                'building_instance_id': building_instance_id,
                'item_refs': item_refs,
                'usage_session_id': extra.get('usage_session_id') or None,
            }

        if handler_key == 'item_assembly_workshop':
            from plugins.adventurer_s1.services.s1_item_instance_service import alchemy_item_refs_total
            item_refs = extra.get('item_refs') or []
            if not isinstance(item_refs, list) or not (2 <= alchemy_item_refs_total(item_refs) <= 5):
                return None
            return {
                'adventurer_id': adv_id,
                'building_instance_id': building_instance_id,
                'item_refs': item_refs,
                'layout_mode': extra.get('layout_mode') or 'vertical_line',
                'layout_slots': extra.get('layout_slots') or [],
                'desired_direction': extra.get('desired_direction') or '',
                'usage_session_id': extra.get('usage_session_id') or None,
                'item_snapshots': extra.get('item_snapshots') or [],
                'input_slots': extra.get('input_slots') or [],
                'input_snapshot_version': extra.get('input_snapshot_version'),
                'input_snapshot_hash': extra.get('input_snapshot_hash') or '',
            }

        if handler_key == 'ember_kitchen':
            from plugins.adventurer_s1.services.s1_ember_kitchen_contract import (
                EMBER_KITCHEN_COOKWARE,
                EMBER_KITCHEN_INPUT_CONTRACT_VERSION,
            )
            from plugins.adventurer_s1.services.s1_item_instance_service import (
                item_refs_total,
            )

            item_refs = extra.get('item_refs') or []
            input_slots = extra.get('input_slots') or []
            cookware_code = str(extra.get('cookware_code') or '').strip()
            usage_session_id = str(extra.get('usage_session_id') or '').strip()
            total = item_refs_total(item_refs)
            if (
                not isinstance(item_refs, list)
                or not (1 <= total <= 5)
                or cookware_code not in EMBER_KITCHEN_COOKWARE
                or not usage_session_id
                or not isinstance(input_slots, list)
                or len(input_slots) != total
                or extra.get('input_snapshot_version')
                != EMBER_KITCHEN_INPUT_CONTRACT_VERSION
                or not str(extra.get('input_snapshot_hash') or '').strip()
            ):
                return None
            return {
                'adventurer_id': adv_id,
                'building_instance_id': building_instance_id,
                'item_refs': item_refs,
                'cookware_code': cookware_code,
                'usage_session_id': usage_session_id,
                'item_snapshots': extra.get('item_snapshots') or [],
                'input_slots': input_slots,
                'input_snapshot_version': extra.get('input_snapshot_version'),
                'input_snapshot_hash': extra.get('input_snapshot_hash') or '',
            }

        if handler_key in ('healing_house', 'chapel'):
            raw_traits = extra.get('target_traits') or []
            target_traits = list(dict.fromkeys(str(t).strip() for t in raw_traits if t))
            if not target_traits:
                return None
            payload = {
                'adventurer_id': adv_id,
                'building_instance_id': building_instance_id,
                'target_traits': target_traits,
                'usage_session_id': extra.get('usage_session_id') or None,
            }
            item_ref = extra.get('item_ref')
            if handler_key == 'healing_house' and item_ref:
                payload['item_ref'] = item_ref
            return payload

        if handler_key == 'pet_evolution':
            companion_id = extra.get('companion_id')
            if companion_id is None:
                return None
            try:
                evolution_stage_at_start = max(0, int(extra.get('evolution_stage')))
            except (TypeError, ValueError):
                evolution_stage_at_start = None
            if evolution_stage_at_start is None:
                from plugins.adventurer_s1.models import AdventurerS1Equipment
                companion = AdventurerS1Equipment.query.filter_by(
                    id=int(companion_id), adventurer_id=adv_id,
                ).first()
                evolution_stage_at_start = max(0, int(companion.evolution_stage or 0)) if companion else 0
            return {
                'adventurer_id': adv_id,
                'building_instance_id': building_instance_id,
                'companion_id': int(companion_id),
                'charged_gold_cost': float(extra.get('gold_cost') or config.get('gold_cost') or 0),
                'auxiliary_material': extra.get('auxiliary_material'),
                'evolution_stage_at_start': evolution_stage_at_start,
                'usage_session_id': extra.get('usage_session_id') or None,
            }

        if handler_key == 'skill_school':
            from plugins.adventurer_s1.services.s1_skill_school_service import (
                S1SkillSchoolService,
            )
            if not S1SkillSchoolService.is_custom_training_extra(extra):
                return None
            operation = str(extra.get('operation') or '')
            direction = str(extra.get('direction') or '').strip()
            usage_session_id = str(extra.get('usage_session_id') or '').strip()
            if not direction or not usage_session_id:
                return None
            if operation == 'advance':
                try:
                    int(extra.get('target_skill_id'))
                except (TypeError, ValueError):
                    return None
            return {
                'adventurer_id': adv_id,
                'building_instance_id': building_instance_id,
                'usage_session_id': usage_session_id,
            }

        return None

    @staticmethod
    def _claim_session_for_current_boot(
        adventurer_id: int,
        building_instance_id: int,
        *,
        usage_session_id: Optional[str],
    ) -> tuple[Optional[dict], bool]:
        """在行锁内接管同一建筑会话，防止多个 recovery 同时抢占。"""
        try:
            with db_manager.transaction():
                adv = (
                    AdventurerS1.query
                    .filter_by(id=adventurer_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if (
                    not adv
                    or adv.status != 'building'
                    or adv.building_instance_id != building_instance_id
                ):
                    return None, False
                extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
                if str(extra.get('usage_session_id') or '') != str(usage_session_id or ''):
                    return None, False
                owned, owner_changed = stamp_current_building_ai_owner(extra)
                if owner_changed:
                    adv.building_usage_extra = owned
                return owned, owner_changed
        except Exception:
            logger.exception(
                '建筑 AI recovery 接管失败 adv=%s inst=%s',
                adventurer_id,
                building_instance_id,
            )
            return None, False

    @staticmethod
    def _release_only(
        adventurer_id: int,
        building_instance_id: int,
        usage_session_id: Optional[str] = None,
    ) -> bool:
        """AI 已写 usage_log 但未释放：仅释放占位，不退费（结果已落地）。"""
        try:
            with db_manager.transaction():
                adv = (
                    AdventurerS1.query
                    .filter_by(id=adventurer_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not adv:
                    return False
                if adv.status != 'building' or adv.building_instance_id != building_instance_id:
                    return False
                extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
                if not building_ai_owner_is_current(extra):
                    return False
                current_session_id = str(extra.get('usage_session_id') or '')
                expected_session_id = str(usage_session_id or '')
                if current_session_id != expected_session_id:
                    logger.info(
                        '忽略过期建筑 AI 会话释放 adv=%s expected=%s current=%s',
                        adventurer_id, expected_session_id, current_session_id,
                    )
                    return False
                AdventurerS1StateTransitionCoordinator.return_from_building(adv)
            db_manager.remove()
            from plugins.adventurer_s1.services.s1_building_usage_queue_service import (
                S1BuildingUsageQueueService,
            )
            S1BuildingUsageQueueService.promote_waiting_for_building(
                building_instance_id,
            )
            db_manager.remove()
            AdventurerS1BuildingAiRecoveryService._emit_recovered(adventurer_id, 'released')
            return True
        except Exception:
            logger.exception(
                '建筑 AI recovery 释放失败 adv=%s inst=%s', adventurer_id, building_instance_id,
            )
            return False
        finally:
            db_manager.remove()

    @staticmethod
    def _refund_pet_evolution_auxiliary_material(
        adventurer: AdventurerS1,
        extra: dict,
        usage_session_id: str,
        *,
        refund_source_type: str = 'pet_evolution_abort_refund',
        refund_description: str = '嬗变祭坛技术中止，退还辅助素材',
    ) -> Optional[dict]:
        """按会话流水补回嬗变辅助原物；旧会话缺证据时保持 fail-closed。"""
        snapshot = extra.get('auxiliary_material_refund_snapshot')
        if not isinstance(snapshot, dict) or not usage_session_id:
            return None
        item_name = str(snapshot.get('name') or '').strip()
        item_type = str(snapshot.get('type') or '')
        item_rarity = str(snapshot.get('rarity') or 'common')
        try:
            snapshot_quantity = int(snapshot.get('quantity') or 0)
        except (TypeError, ValueError):
            return None
        if not item_name or snapshot_quantity <= 0:
            return None

        logs = (
            AdventurerS1ItemLog.query
            .with_entities(
                AdventurerS1ItemLog.change_type,
                AdventurerS1ItemLog.source_type,
                AdventurerS1ItemLog.quantity,
            )
            .filter(
                AdventurerS1ItemLog.adventurer_id == adventurer.id,
                AdventurerS1ItemLog.related_id == usage_session_id,
                AdventurerS1ItemLog.related_type == 'building_usage_session',
                AdventurerS1ItemLog.item_name == item_name,
                AdventurerS1ItemLog.item_type == item_type,
                AdventurerS1ItemLog.item_rarity == item_rarity,
                AdventurerS1ItemLog.source_type.in_((
                    'pet_evolution',
                    'pet_evolution_abort_refund',
                    'pet_evolution_queue_refund',
                )),
            )
            .all()
        )
        consumed = sum(
            int(quantity or 0)
            for change_type, source_type, quantity in logs
            if change_type == 'consume' and source_type == 'pet_evolution'
        )
        refunded = sum(
            int(quantity or 0)
            for change_type, source_type, quantity in logs
            if change_type in ('gain', 'overflow')
            and source_type in (
                'pet_evolution_abort_refund',
                'pet_evolution_queue_refund',
            )
        )
        refund_quantity = min(snapshot_quantity, max(0, consumed - refunded))
        if refund_quantity <= 0:
            return None

        source = str(snapshot.get('source') or '')
        force_destination = {
            'carry': 'equipment',
            'warehouse': 'warehouse',
        }.get(source)
        if force_destination is None:
            raise RuntimeError('嬗变辅助素材恢复位置无效')
        from plugins.adventurer_s1.services.s1_item_grant_service import (
            grant_raw_item,
        )
        destination, restore_err = grant_raw_item(
            adventurer,
            item_name,
            {
                'type': item_type,
                'rarity': item_rarity,
                'quantity': refund_quantity,
                'uses_left': snapshot.get('uses_left'),
                'desc': snapshot.get('custom_desc'),
                'evolution_stage': int(snapshot.get('evolution_stage') or 0),
                **(
                    {
                        'damage_stage': snapshot.get('damage_stage'),
                        'damage_note': snapshot.get('damage_note'),
                    }
                    if source == 'carry' else {}
                ),
            },
            source_type=refund_source_type,
            description=refund_description,
            force_destination=force_destination,
            equipment_first=source == 'carry',
            allow_equipment_overflow=source != 'carry',
            register_catalog=False,
            related_id=usage_session_id,
            related_type='building_usage_session',
            allow_same_name_stack_merge=True,
        )
        if restore_err or destination is None:
            raise RuntimeError(restore_err or '嬗变辅助素材退还失败')
        return {
            'name': item_name,
            'quantity': refund_quantity,
            'destination': destination,
        }

    @staticmethod
    def abort_stuck_session(
        adventurer_id: int,
        building_instance_id: int,
        handler_key: str,
        reason: str = 'building_ai_abort',
        usage_session_id: Optional[str] = None,
        failure_diagnostics: Optional[dict] = None,
    ) -> bool:
        """中止未完成的异步建筑会话：退还金币/精力，清除 CD（删 usage_log），释放冒险者。"""
        from plugins.adventurer_s1.services.adventurer_s1_building_settlement_service import (
            BuildingSettlementService,
        )
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        from services.async_task_service import AsyncTaskService

        task_type = _ASYNC_HANDLER_TASK_TYPES.get(handler_key)
        gold_refund = 0.0
        stamina_cost = 0
        extra_snapshot: dict = {}
        auxiliary_material_refund: Optional[dict] = None
        skill_training_retry_preserved = False

        try:
            inst = AdventurerS1BuildingInstance.query.session.get(AdventurerS1BuildingInstance, building_instance_id)
            if not inst:
                return False
            config = BuildingSettlementService.get_reward_config(inst)
            with db_manager.transaction():
                adv = (
                    AdventurerS1.query
                    .filter_by(id=adventurer_id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if not adv:
                    return False
                if adv.status != 'building' or adv.building_instance_id != building_instance_id:
                    return False

                assigned_at = adv.building_assigned_at or _portable_utc_now()
                extra = adv.building_usage_extra if isinstance(adv.building_usage_extra, dict) else {}
                if not building_ai_owner_is_current(extra):
                    logger.info(
                        '忽略旧后端启动版本的建筑 AI 中止 adv=%s session=%s',
                        adventurer_id,
                        usage_session_id,
                    )
                    return False
                current_session_id = str(extra.get('usage_session_id') or '')
                expected_session_id = str(usage_session_id or '')
                if current_session_id != expected_session_id:
                    logger.info(
                        '忽略过期建筑 AI 会话中止 adv=%s expected=%s current=%s',
                        adventurer_id, expected_session_id, current_session_id,
                    )
                    return False
                extra_snapshot = dict(extra)

                if handler_key == 'skill_school':
                    from plugins.adventurer_s1.services.s1_skill_school_service import (
                        S1SkillSchoolService,
                    )
                    retry_snapshot, retry_err = (
                        S1SkillSchoolService
                        .ensure_retry_opportunity_for_active_lesson(
                            adv,
                            building_instance_id,
                            current_session_id,
                        )
                    )
                    if retry_err:
                        raise RuntimeError(
                            f'导师课堂中止前无法保留补课: {retry_err}'
                        )
                    skill_training_retry_preserved = retry_snapshot is not None

                gold_refund = AdventurerS1BuildingAiRecoveryService._resolve_start_gold_refund(
                    adventurer_id, building_instance_id, assigned_at,
                )
                if gold_refund <= 0:
                    gold_refund = float(extra.get('gold_cost') or config.get('gold_cost') or 0)

                if gold_refund > 0:
                    from plugins.adventurer_s1.services.adventurer_s1_currency_service import (
                        S1CurrencyService,
                    )
                    building_name = inst.template.name if inst.template else '建筑'
                    refund_description = (
                        '百艺阁授艺未成，退回束脩'
                        if handler_key == 'skill_school'
                        else f'「{building_name}」本次作业未能完成，退还使用费用'
                    )
                    _, refund_err = S1CurrencyService.add(
                        adventurer_id,
                        'gold',
                        float(gold_refund),
                        source_type='building_usage_abort_refund',
                        related_id=building_instance_id,
                        related_type='building_instance',
                        description=refund_description,
                        auto_commit=False,
                    )
                    if refund_err:
                        raise RuntimeError(refund_err)

                stamina_cost = int(extra.get('stamina_cost') or config.get('usage_stamina_cost') or 0)
                if stamina_cost > 0:
                    from flask import current_app
                    max_stamina = int(current_app.config.get('ADVENTURER_S1_STAMINA_MAX', 100))
                    adv.stamina = min(max_stamina, int(adv.stamina or 0) + stamina_cost)

                AdventurerS1Service._refund_building_usage_input_materials(
                    adventurer_id,
                    building_instance_id,
                    assigned_at,
                    config,
                    description=f'「{inst.template.name if inst.template else "建筑"}」本次作业未能完成，退还投入材料',
                )

                if handler_key == 'pet_evolution':
                    auxiliary_material_refund = (
                        AdventurerS1BuildingAiRecoveryService
                        ._refund_pet_evolution_auxiliary_material(
                            adv,
                            extra,
                            current_session_id,
                        )
                    )

                AdventurerS1BuildingUsageLog.query.filter(
                    AdventurerS1BuildingUsageLog.adventurer_id == adventurer_id,
                    AdventurerS1BuildingUsageLog.building_instance_id == building_instance_id,
                    AdventurerS1BuildingUsageLog.used_at >= assigned_at,
                ).delete(synchronize_session=False)

                audit_diagnostics = None
                if handler_key == 'item_assembly_workshop':
                    audit_diagnostics = _build_item_assembly_failure_diagnostics(extra_snapshot)
                elif handler_key == 'ember_kitchen':
                    audit_diagnostics = _build_ember_kitchen_failure_diagnostics(
                        extra_snapshot,
                    )
                elif handler_key == 'pet_evolution' and failure_diagnostics is not None:
                    audit_diagnostics = _project_pet_evolution_failure_diagnostics(
                        failure_diagnostics,
                    )
                if audit_diagnostics is not None:
                    audit = AdventurerS1BuildingAiFailureAudit(
                        adventurer_id=adventurer_id,
                        building_instance_id=building_instance_id,
                        handler_key=handler_key,
                        usage_session_id=str(usage_session_id) if usage_session_id else None,
                        failure_code=str(reason or 'building_ai_abort')[:100],
                        diagnostics=audit_diagnostics,
                    )
                    audit_ok, audit_err = db_manager.add(audit)
                    if not audit_ok:
                        raise RuntimeError(audit_err or '建筑 AI 终止审计写入失败')

                AdventurerS1StateTransitionCoordinator.return_from_building(adv)

            db_manager.remove()
            if handler_key == 'skill_school':
                from plugins.adventurer_s1.services.s1_skill_school_service import (
                    S1SkillSchoolService,
                )
                S1SkillSchoolService.promote_next_waiting(building_instance_id)
            else:
                from plugins.adventurer_s1.services.s1_building_usage_queue_service import (
                    S1BuildingUsageQueueService,
                )
                S1BuildingUsageQueueService.promote_waiting_for_building(
                    building_instance_id,
                )
            db_manager.remove()

            if task_type:
                payload = AdventurerS1BuildingAiRecoveryService._build_task_payload(
                    handler_key, adventurer_id, building_instance_id, extra_snapshot, config,
                )
                db_manager.remove()
                if payload:
                    AsyncTaskService.force_release_task_lock(task_type, payload)
                else:
                    AsyncTaskService.force_release_task_lock(
                        task_type, {'adventurer_id': adventurer_id},
                    )

            narrative = '本次作业未能完成，费用已退还，可重新使用。'
            if handler_key in (
                'blacksmith', 'tailor_workshop', 'alchemy_workshop',
                'item_assembly_workshop',
            ):
                narrative = '本次作业未能完成，费用与投入物品均已退还，可重新使用。'
            elif handler_key == 'ember_kitchen':
                narrative = '本次烹饪未能完成，费用已退还，投入食材仍原样保留。'
            elif handler_key == 'pet_evolution' and auxiliary_material_refund:
                narrative = '本次嬗变因技术异常中止，费用与辅助素材均已原样退还，可重新使用。'
            elif handler_key == 'skill_school':
                narrative = (
                    '导师试着替你收了收路子，最终还是没能把这门本事授成。'
                    '这堂课不作数，束脩已退，耗去的精力也已养回。'
                    '原课堂已经留作补课；下次来百艺阁可直接继续，'
                    '不必重新候场或再等一堂课。'
                )

            result_type = 'skill_training' if handler_key == 'skill_school' else handler_key
            recovery_result = {
                'type': result_type,
                'success': False,
                'system_abort': True,
                'gold_refunded': round(float(gold_refund), 2) if gold_refund > 0 else 0,
                'stamina_refunded': stamina_cost,
                'cooldown_cleared': True,
                'items_preserved': (
                    handler_key in (
                        'blacksmith', 'tailor_workshop', 'alchemy_workshop',
                        'item_assembly_workshop', 'ember_kitchen',
                    )
                    or auxiliary_material_refund is not None
                ),
                'narrative': narrative,
            }
            if handler_key == 'pet_evolution':
                recovery_result['auxiliary_material_refunded'] = bool(
                    auxiliary_material_refund
                )
                if auxiliary_material_refund:
                    recovery_result['auxiliary_material_refund'] = auxiliary_material_refund
            if handler_key == 'skill_school':
                recovery_result['operation'] = extra_snapshot.get('operation')
                recovery_result['retry_preserved'] = (
                    skill_training_retry_preserved
                )
            db_manager.remove()
            AdventurerS1BuildingAiRecoveryService._emit_recovered(
                adventurer_id,
                'aborted',
                recovery_result,
            )
            return True
        except Exception:
            logger.exception(
                '建筑 AI 中止失败 adv=%s inst=%s handler=%s',
                adventurer_id, building_instance_id, handler_key,
            )
            return False
        finally:
            db_manager.remove()

    @staticmethod
    def _resolve_start_gold_refund(
        adventurer_id: int,
        building_instance_id: int,
        since: datetime,
    ) -> float:
        """按本次会话 start 时的扣费流水计算应退金币。"""
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService

        related_id = S1CurrencyService.normalize_related_id(building_instance_id)
        usage_cost_types = (
            'building_usage_cost',
            'blacksmith_usage_cost',
            'tailor_workshop_usage_cost',
            'alchemy_workshop_usage_cost',
            'assembly_workshop_usage_cost',
            'ember_kitchen_usage_cost',
            'skill_school_custom_learn',
            'skill_school_custom_advance',
        )
        logs = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
                AdventurerS1CurrencyLog.related_type == 'building_instance',
                AdventurerS1CurrencyLog.related_id == related_id,
                AdventurerS1CurrencyLog.source_type.in_(usage_cost_types),
                AdventurerS1CurrencyLog.created_at >= since,
            )
            .all()
        )
        total = 0.0
        for log in logs:
            amount = float(log.change_amount or 0)
            if amount < 0:
                total += abs(amount)
        return round(total, 2)

    @staticmethod
    def _emit_recovered(adventurer_id: int, action: str, result: dict = None):
        try:
            from plugins.adventurer_s1.services.adventurer_s1_service import (
                invalidate_adventurer_detail_cache,
                invalidate_buildings_list_cache,
                invalidate_my_activity_cache,
            )
            invalidate_adventurer_detail_cache(adventurer_id)
            invalidate_buildings_list_cache()
            invalidate_my_activity_cache()
        except Exception:
            pass
        try:
            from plugins.adventurer_s1.services.s1_socket_service import emit_s1_building_result
            payload = {
                'action': 'building_ai_complete',
                'reason': f'building_ai_{action}',
                'ids': [adventurer_id],
            }
            if result is not None:
                payload['data'] = result
            emit_s1_building_result(adventurer_id, payload)
        except Exception:
            pass
