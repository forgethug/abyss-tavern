"""S1 世界事实候选、DM 接力便签与公共世界情报维护。

主探险 AI 产出候选；异步大纲维护 AI 长期自我迭代公共情报。
管理员后台只用于查看、纠错和偶发人工覆盖，不承担日常维护。
"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
import logging
import random
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

import requests
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
    get_s1_ai_response_metadata,
    resolve_s1_ai_profile_snapshot,
)
from plugins.adventurer_s1.services.s1_ai_player_name_service import (
    frozen_player_names,
    post_s1_ai_request_with_player_aliases as post_s1_ai_request,
)
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    S1AiResponseContractResult,
    parse_s1_ai_http_json_response,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)
from sqlalchemy import and_, or_, text

from db_repository import db_manager
from db_repository.s1_campaign_world_repository import S1CampaignWorldRepository
from plugins.adventurer_s1.services.s1_campaign_world_service import campaign_world
from db_repository.s1_campaign_dispatch_repository import (
    CAMPAIGN_PUBLICATION_RECOVERY_KIND,
    S1CampaignDispatchRepository,
)
from db_repository.s1_world_outline_lease_repository import maintenance_claim_exists
from plugins.adventurer_s1.services.s1_world_outline_lease_service import (
    maintenance_heartbeat,
    maintenance_lease_settings,
)
from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1DmHandoffState,
    AdventurerS1ExpeditionLog,
    AdventurerS1WorldFactCandidate,
    AdventurerS1WorldActor,
    AdventurerS1WorldOutlineDungeonState,
    AdventurerS1WorldStoryThread,
    AdventurerS1WorldStoryThreadActor,
    AdventurerS1FactionProfile,
    AdventurerS1FactionProfileThread,
)
from plugins.adventurer_s1.services.adventurer_s1_prompt_service import (
    build_codename_to_real_from_snapshot,
    codename_for_member_index,
)
from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
    restore_names_in_narrative,
)
from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_ACTIVE_DUNGEON_NAMES,
)
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
from plugins.adventurer_s1.services.s1_first_encounter_learning_service import (
    FirstEncounterLearningError,
)
from plugins.adventurer_s1.services.s1_expedition_locus_service import (
    extract_location_scope_keys,
    location_scope_key,
    resolve_expedition_locus_scope_from_full_log,
    resolve_persistent_story_scope_from_full_log,
    sanitize_story_location_scope_keys,
    story_scope_matches_expedition_locus,
)
from plugins.adventurer_s1.services.s1_world_outline_contract import (
    FACTION_PRESENCE_RECEIPTS_FIELD,
    MAX_WORLD_FACT_CONTAINER_ITEMS,
    WORLD_INTEL_ALL_DUNGEONS_SCOPE,
    WORLD_LOCATION_MAINTENANCE_RULE,
    WORLD_LOCATION_MAINTENANCE_READ_ONLY_RULE,
    normalize_dm_handoff_note,
    normalize_faction_profile_updates,
    normalize_maintenance_result_with_status,
    normalize_world_fact_updates,
)

logger = logging.getLogger(__name__)

DM_HANDOFF_NOTE_LIMIT = 1000
MAX_CANDIDATE_NOTE_CHARS = 2000
WORLD_STORY_RESEED_MIN_WEIGHT = 50
WORLD_STORY_MAINTENANCE_CONTEXT_LIMIT = 20
WORLD_INTEL_THREAD_TYPES = ('entity', 'event', 'faction', 'location', 'omen', 'artifact')
ECOLOGY_CONTINUITY_DEBT_RECEIPT_ERROR = (
    '资源生态连续性债务回执必须逐日志绑定世界线或非空 DM 接力便签'
)
ECOLOGY_CONTINUITY_DEBT_HANDOFF_FALLBACK_DIAGNOSTIC = (
    '资源生态连续性债务回执缺失或无效，已按精确来源日志转入 DM 接力便签'
)
ECOLOGY_CONTINUITY_DEBT_CONSUMER_FALLBACK_DIAGNOSTIC = (
    '资源生态连续性债务的世界线出口未实际落库，已按精确来源转入 DM 接力便签'
)
WORLD_OUTLINE_MAINTENANCE_RECEIPT_SCHEMA_VERSION = 1
WORLD_OUTLINE_ECOLOGY_RECOVERY_KIND = 'ecology_debt_handoff'
WORLD_OUTLINE_ECOLOGY_RECOVERY_INTERRUPTED_ISSUE_CODE = (
    'ecology_debt_recovery_interrupted'
)
WORLD_LOCATION_MAINTENANCE_REJECTED = 'world_location_maintenance_rejected'
WORLD_OUTLINE_RECOVERABLE_ECOLOGY_ISSUE_CODES = frozenset((
    'maintenance_request_failed',
    'maintenance_output_contract_failed',
    'maintenance_unexpected_failed',
    'consumer_transaction_failed',
    WORLD_LOCATION_MAINTENANCE_REJECTED,
))


@dataclass(frozen=True)
class _WorldLocationMaintenanceRejection:
    """A valid review disposition with no candidate content safe to consume."""

    diagnostics: tuple[str, ...]


def _new_world_story_upsert_result(raw_threads) -> dict:
    """Create one non-persistent accumulator for the authoritative consumer."""
    return {
        'proposed_count': len(raw_threads) if isinstance(raw_threads, list) else 0,
        'applied_indexes': set(),
        'applied': [],
        'rejected': [],
        'warnings': [],
        'applied_cycle_reseed_keys': set(),
    }


def _record_world_story_rejection(
    result: dict,
    *,
    index: int,
    reason_code: str,
    raw: object,
) -> None:
    """Record exactly one stable rejection reason for one proposed row."""
    if any(item.get('index') == index for item in result['rejected']):
        return
    row = raw if isinstance(raw, dict) else {}
    result['rejected'].append({
        'index': int(index),
        'reason_code': str(reason_code)[:64],
        'source_world_story_thread_id': _coerce_int(
            row.get('source_world_story_thread_id'), 0,
        ),
        'source_revision': _coerce_int(row.get('source_revision'), 0),
        'change_kind': _clip_text(row.get('change_kind'), 24),
        'title': _clip_text(row.get('title'), 120),
    })


def _world_story_upsert_result_after_rollback(
    raw_threads: object,
    *,
    reason_code: str,
    previous_result: dict | None = None,
) -> dict:
    """Describe the committed outcome after the surrounding transaction rolled back."""
    result = _new_world_story_upsert_result(raw_threads)
    prior_rejections = {
        row['index']: row['reason_code']
        for row in (previous_result or {}).get('rejected', [])
    }
    for index, raw in enumerate(raw_threads if isinstance(raw_threads, list) else []):
        _record_world_story_rejection(
            result,
            index=index,
            reason_code=prior_rejections.get(index, reason_code),
            raw=raw,
        )
    return result


def _classify_world_outline_maintenance_failure(exc: BaseException) -> str:
    if isinstance(exc, FirstEncounterLearningError) and exc.error_code:
        return (
            'maintenance_request_failed' if exc.error_code.startswith('provider.')
            else 'maintenance_output_contract_failed'
        )
    if isinstance(exc, requests.RequestException):
        return 'maintenance_request_failed'
    if isinstance(exc, ValueError):
        return 'maintenance_output_contract_failed'
    return 'maintenance_unexpected_failed'


def _root_world_outline_exception(exc: BaseException) -> BaseException:
    """Unwrap repository transaction errors for a searchable root diagnosis."""
    current = exc
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        original = getattr(current, 'original_error', None)
        if not isinstance(original, BaseException):
            break
        current = original
    return current


def _build_world_outline_maintenance_receipt(
    *,
    batch_id: str,
    dungeon: str,
    candidate_ids: list[int],
    expedition_log_ids: list[int],
    status: str,
    stage: str,
    upsert_result: dict | None = None,
    issue_codes: list[str] | set[str] | tuple[str, ...] | None = None,
    diagnostics: list | None = None,
    ecology_debt_outcomes: list[dict] | None = None,
    ecology_debt_expected: bool = False,
    recovery_sources: list[dict] | None = None,
    error: BaseException | None = None,
) -> dict:
    """Build a JSON-safe, bounded receipt after a real consumer decision."""
    result = upsert_result if isinstance(upsert_result, dict) else {}
    rejected = result.get('rejected') if isinstance(result.get('rejected'), list) else []
    applied = result.get('applied') if isinstance(result.get('applied'), list) else []
    consumer_warnings = (
        result.get('warnings')
        if isinstance(result.get('warnings'), list) else []
    )
    stable_codes = {
        str(value)[:64]
        for value in (issue_codes or [])
        if str(value or '').strip()
    }
    stable_codes.update(
        str(item.get('reason_code'))[:64]
        for item in rejected
        if isinstance(item, dict) and item.get('reason_code')
    )
    stable_codes.update(
        str(item.get('reason_code'))[:64]
        for item in consumer_warnings
        if isinstance(item, dict) and item.get('reason_code')
    )
    receipt = {
        'schema_version': WORLD_OUTLINE_MAINTENANCE_RECEIPT_SCHEMA_VERSION,
        'batch_id': str(batch_id)[:64],
        'target_dungeon': str(dungeon or '')[:100],
        'candidate_ids': sorted({int(value) for value in candidate_ids}),
        'expedition_log_ids': sorted({int(value) for value in expedition_log_ids}),
        'status': str(status)[:32],
        'stage': str(stage)[:64],
        'proposed_world_story_thread_count': int(
            result.get('proposed_count') or 0
        ),
        'applied_world_story_thread_count': len(applied),
        'rejected_world_story_thread_count': len(rejected),
        'applied_world_story_threads': [dict(item) for item in applied[:50]],
        'rejected_world_story_threads': [dict(item) for item in rejected[:50]],
        'world_story_consumer_warnings': [
            dict(item) for item in consumer_warnings[:50]
        ],
        'issue_codes': sorted(stable_codes),
        'diagnostics': [str(item)[:500] for item in (diagnostics or [])[:20]],
        'ecology_debt_outcomes': [
            dict(item) for item in (ecology_debt_outcomes or [])[:50]
        ],
        'ecology_debt_expected': bool(ecology_debt_expected),
        'recovery_sources': [
            dict(item) for item in (recovery_sources or [])[:50]
        ],
        'recorded_at': _portable_utc_now().isoformat(),
    }
    if error is not None:
        receipt['error'] = {
            'type': type(error).__name__[:120],
            'message': str(error)[:1000],
        }
        if isinstance(error, FirstEncounterLearningError) and error.error_code:
            receipt['error']['validation_error_code'] = error.error_code[:160]
            if error.upstream_failure:
                receipt['error']['upstream_failure'] = dict(error.upstream_failure)
    return receipt


def _primary_world_outline_issue_code(
    *,
    receipt_status: str,
    upsert_result: dict,
    diagnostics: list,
    ecology_degraded: bool,
    encounter_preparation: dict | None = None,
) -> str | None:
    if ecology_degraded:
        return 'ecology_debt_degraded_to_handoff'
    rejected_count = len(upsert_result.get('rejected') or [])
    applied_count = len(upsert_result.get('applied') or [])
    if rejected_count:
        return (
            'world_story_consumer_partial'
            if applied_count else 'world_story_consumer_rejected'
        )
    if upsert_result.get('warnings'):
        return 'world_story_consumer_warning'
    if receipt_status == 'degraded':
        return 'ecology_debt_recovered_to_handoff'
    preparation_issue = _encounter_preparation_issue_code(encounter_preparation)
    if preparation_issue:
        return preparation_issue
    if diagnostics:
        return 'maintenance_diagnostic'
    return None


def _encounter_preparation_issue_code(receipt: dict | None) -> str | None:
    status = (receipt or {}).get('status')
    if status in {'failed', 'rejected', 'deferred', 'stale'}:
        return 'encounter_preparation_' + status
    return None


def _log_world_outline_maintenance_receipt(receipt: dict) -> None:
    issue_codes = receipt.get('issue_codes') or []
    expected_rejection = (
        receipt.get('status') == 'rejected'
        and receipt.get('stage') == 'maintenance_review'
    )
    log_method = logger.warning if issue_codes and not expected_rejection else logger.info
    log_method(
        'S1_WORLD_OUTLINE_MAINTENANCE_RECEIPT '
        'batch=%s dungeon=%s candidates=%s logs=%s status=%s stage=%s '
        'proposed=%s applied=%s rejected=%s issue_codes=%s',
        receipt.get('batch_id'),
        receipt.get('target_dungeon'),
        receipt.get('candidate_ids'),
        receipt.get('expedition_log_ids'),
        receipt.get('status'),
        receipt.get('stage'),
        receipt.get('proposed_world_story_thread_count'),
        receipt.get('applied_world_story_thread_count'),
        receipt.get('rejected_world_story_thread_count'),
        issue_codes,
    )


class _DeferredWorldOutlineTokenUsage:
    """Record one usage-bearing response exactly once after consumption."""

    def __init__(
        self,
        *,
        call_type: str,
        usage: dict | None,
        model: str,
        attempt_number: int,
        usage_context: dict | None = None,
        finish_reason: str | None = None,
        response_error_code: str | None = None,
        repair_applied: bool = False,
    ) -> None:
        self.call_type = call_type
        self.usage = usage
        self.model = model
        self.attempt_number = attempt_number
        context = usage_context or {}
        self.correlation_id = context.get('correlation_id')
        self.expedition_log_id = context.get('expedition_log_id')
        self.finish_reason = finish_reason
        self.response_error_code = response_error_code
        self.repair_applied = repair_applied
        self.finalized = False
        self.contract_valid = False

    def mark_contract_valid(self, is_valid: bool) -> None:
        if not self.finalized:
            self.contract_valid = bool(is_valid)

    def finalize(
        self,
        is_success: bool,
        *,
        failure_stage: str | None = None,
        validation_error_code: str | None = None,
    ) -> None:
        if self.finalized or not self.usage:
            return
        self.finalized = True
        succeeded = bool(is_success and self.contract_valid)
        if not succeeded and not failure_stage:
            if self.response_error_code:
                failure_stage = 'response_parse'
                validation_error_code = self.response_error_code
            elif not self.contract_valid:
                failure_stage = 'contract_validation'
                validation_error_code = 'world_outline_contract_invalid'
            else:
                failure_stage = 'consumer'
                validation_error_code = 'world_outline_consumer_failed'
        record_token_usage(
            call_type=self.call_type,
            usage=self.usage,
            model=self.model,
            attempt_number=self.attempt_number,
            is_success=succeeded,
            correlation_id=self.correlation_id,
            expedition_log_id=self.expedition_log_id,
            failure_stage=None if succeeded else failure_stage,
            validation_error_code=None if succeeded else validation_error_code,
            finish_reason=self.finish_reason,
            repair_applied=self.repair_applied,
        )


class _WorldOutlineConsumerUsageOutcome:
    """Carries the explicit consumer result across transaction commit."""

    def __init__(self) -> None:
        self.is_success = False
        self.error: BaseException | None = None
        self.receipt: dict | None = None


def _finalize_world_outline_usage_handles(
    usage_handles: list[_DeferredWorldOutlineTokenUsage],
    is_success: bool,
    *,
    receipt: dict | None = None,
    failure_stage: str | None = None,
    validation_error_code: str | None = None,
) -> None:
    if not is_success and receipt is not None and not failure_stage:
        stage = receipt.get('stage')
        failure_stage = (
            {
                'partial': 'consumer_partial',
                'degraded': 'consumer_degraded',
                'rejected': 'consumer_rejected',
                'applied_with_warnings': 'consumer_warning',
            }.get(receipt.get('status'), 'consumer')
            if stage == 'consumer' else stage or 'maintenance_ai'
        )
        # The disposition and its concrete cause are separate diagnostics.
        causes = [
            row.get('reason_code')
            for field in (
                'rejected_world_story_threads', 'world_story_consumer_warnings',
            )
            for row in receipt.get(field, [])
            if isinstance(row, dict) and row.get('reason_code')
        ]
        causes.extend(receipt.get('issue_codes') or [])
        validation_error_code = (
            causes[0] if causes else 'world_outline_consumer_failed'
        )
    for usage_handle in usage_handles:
        usage_handle.finalize(
            is_success,
            failure_stage=failure_stage,
            validation_error_code=validation_error_code,
        )


@contextmanager
def _world_outline_consumer_transaction(
    usage_handles: list[_DeferredWorldOutlineTokenUsage],
):
    """Finalize provider usage only after the authoritative transaction commits."""
    outcome = _WorldOutlineConsumerUsageOutcome()
    committed = False
    try:
        with db_manager.transaction():
            yield outcome
        committed = True
    except Exception as exc:  # noqa: BLE001 - caller persists a failure receipt
        outcome.error = _root_world_outline_exception(exc)
    finally:
        _finalize_world_outline_usage_handles(
            usage_handles,
            bool(committed and outcome.is_success),
            receipt=outcome.receipt if committed else None,
            failure_stage=None if committed else 'consumer_transaction',
            validation_error_code=(
                None if committed else 'consumer_transaction_failed'
            ),
        )


def _world_outline_ai_retry_config() -> tuple[int, float, float]:
    from flask import current_app

    max_attempts = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_OUTLINE_AI_MAX_ATTEMPTS'), 3,
    ))
    min_wait = max(0.0, float(current_app.config.get(
        'ADVENTURER_S1_WORLD_OUTLINE_AI_RETRY_MIN_WAIT_SECONDS', 1,
    )))
    max_wait = max(min_wait, float(current_app.config.get(
        'ADVENTURER_S1_WORLD_OUTLINE_AI_RETRY_MAX_WAIT_SECONDS', 8,
    )))
    return max_attempts, min_wait, max_wait


def _is_retryable_world_outline_request_error(exc: BaseException) -> bool:
    if isinstance(exc, (
        requests.exceptions.ConnectionError,
        requests.exceptions.Timeout,
        requests.exceptions.SSLError,
    )):
        return True
    if not isinstance(exc, requests.exceptions.HTTPError):
        return False
    status_code = getattr(getattr(exc, 'response', None), 'status_code', None)
    return status_code == 429 or (isinstance(status_code, int) and 500 <= status_code < 600)


def _post_world_outline_ai_request(
    url: str,
    *,
    stage: str,
    headers: dict,
    json_payload: dict,
    timeout: int,
    response_validator=None,
    player_names: tuple[str, ...] = (),
    usage_context: dict | None = None,
) -> requests.Response:
    max_attempts, min_wait, max_wait = _world_outline_ai_retry_config()

    def _log_before_sleep(retry_state) -> None:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        sleep_seconds = retry_state.next_action.sleep if retry_state.next_action else 0
        logger.warning(
            'S1 大纲 AI 网络请求失败，准备重试 stage=%s attempt=%s/%s sleep=%.2fs error=%s detail=%s',
            stage,
            retry_state.attempt_number,
            max_attempts,
            sleep_seconds,
            type(exc).__name__ if exc else 'unknown',
            str(exc)[:500] if exc else '',
        )

    retryer = Retrying(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential_jitter(initial=min_wait, max=max_wait, jitter=min_wait),
        retry=retry_if_exception(_is_retryable_world_outline_request_error),
        before_sleep=_log_before_sleep,
        reraise=True,
    )

    def validate_world_outline_json_response(decoded):
        json_result = decoded.json_result
        if json_result.ok and isinstance(json_result.data, dict):
            return S1AiResponseContractResult.accept(json_result.data)
        return S1AiResponseContractResult.reject(
            json_result.error_code or 'world_outline_response_invalid',
            failure_stage='response_parse',
        )

    for attempt in retryer:
        with attempt:
            request_metadata: dict[str, object] = {}
            try:
                response = post_s1_ai_request(
                    url,
                    feature='world_outline',
                    player_names=player_names,
                    request_metadata_out=request_metadata,
                    headers=headers,
                    json=json_payload,
                    timeout=timeout,
                    response_validator=(
                        response_validator
                        or validate_world_outline_json_response
                    ),
                )
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                call_type = (
                    'world_outline_cycle_reseed_review_ai'
                    if stage == 'cycle_reseed_review'
                    else 'world_outline_maintenance_ai'
                )
                record_token_usage(
                    call_type=call_type,
                    usage=None,
                    model=str(
                        request_metadata.get('model')
                        or json_payload.get('model')
                        or ''
                    ),
                    attempt_number=attempt.retry_state.attempt_number,
                    is_success=False,
                    correlation_id=(usage_context or {}).get('correlation_id'),
                    expedition_log_id=(usage_context or {}).get('expedition_log_id'),
                    failure_stage='http_or_response',
                    validation_error_code=f'world_outline_{stage}_request_error',
                    record_without_usage=True,
                    request_metadata=request_metadata,
                    request_exception=exc,
                )
                raise
    raise RuntimeError('S1 大纲 AI 网络重试未返回结果')
WORLD_INTEL_STATUSES = ('seeded', 'active', 'escalated', 'closing', 'dormant', 'resolved')
WORLD_INTEL_MUTABLE_STATUSES = frozenset(('seeded', 'active', 'escalated', 'closing'))
WORLD_INTEL_TERMINAL_STATUSES = frozenset(('dormant', 'resolved'))
ABYSS_TAVERN_FACTION_SCOPE_KEY = 'abyss_tavern'
WORLD_CAMPAIGN_SPINE_ROLE = 'spine'
WORLD_CAMPAIGN_FRONT_ROLES = frozenset(('faction_front', 'dungeon_front'))
_REQUIRED_TAVERN_INTERACTION_TRANSIENT_ATTR = (
    '_s1_required_tavern_faction_interaction'
)


def _world_story_cadence_threshold() -> int:
    from flask import current_app

    return max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_STORY_CLOSURE_DUE_AFTER_RUNS'),
        6,
    ))


def _is_tavern_faction_relation(row: AdventurerS1WorldStoryThread) -> bool:
    scope_keys = {
        str(value).strip()
        for value in (getattr(row, 'scope_keys', None) or [])
        if str(value or '').strip()
    }
    return bool(
        getattr(row, 'thread_type', None) == 'faction'
        and ABYSS_TAVERN_FACTION_SCOPE_KEY in scope_keys
    )


def _expedition_effective_dungeon_clause(dungeon: str):
    """Match the narrative/world-state dungeon while preserving legacy ordinary logs."""
    return or_(
        AdventurerS1ExpeditionLog.hidden_dungeon == dungeon,
        and_(
            AdventurerS1ExpeditionLog.hidden_dungeon.is_(None),
            AdventurerS1ExpeditionLog.target_dungeon == dungeon,
        ),
    )


def _world_story_capacity_snapshot(dungeon: str) -> dict:
    """Return the adaptive soft capacity for one dungeon without mutating story data."""
    from flask import current_app

    dungeon = str(dungeon or '').strip()
    minimum = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_MIN_PER_DUNGEON'), 4,
    ))
    base = max(minimum, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_BASE_PER_DUNGEON'), 6,
    ))
    normal_max = max(base, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_MAX_PER_DUNGEON'), 18,
    ))
    burst_max = max(normal_max, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_BURST_MAX_PER_DUNGEON'), 30,
    ))
    window_days = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_RECENT_WINDOW_DAYS'), 14,
    ))
    expeditions_per_head = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_RECENT_EXPEDITIONS_PER_HEAD'), 8,
    ))
    burst_reserve = max(0, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_BURST_RESERVE'), 2,
    ))
    recent_count = 0
    active_count = 0
    if dungeon:
        cutoff = _portable_utc_now() - timedelta(days=window_days)
        recent_count = int(AdventurerS1ExpeditionLog.query.filter(
            _expedition_effective_dungeon_clause(dungeon),
            AdventurerS1ExpeditionLog.created_at >= cutoff,
        ).count())
        active_count = int(AdventurerS1ExpeditionLog.query.filter(
            _expedition_effective_dungeon_clause(dungeon),
            AdventurerS1ExpeditionLog.status.in_(('generating', 'in_progress')),
        ).count())
    history_bonus = (recent_count + expeditions_per_head - 1) // expeditions_per_head
    normal_limit = max(minimum, min(normal_max, base + history_bonus))
    effective_limit = min(burst_max, max(normal_limit, active_count + burst_reserve))
    return {
        'target_dungeon': dungeon,
        'normal_limit': normal_limit,
        'effective_limit': effective_limit,
        'recent_expeditions': recent_count,
        'active_expeditions': active_count,
        'window_days': window_days,
        'burst_max': burst_max,
    }


def _ensure_world_outline_dungeon_state(dungeon: str) -> AdventurerS1WorldOutlineDungeonState:
    """Create the aggregate row idempotently on PostgreSQL and SQLite, then lock it."""
    now = _portable_utc_now()
    db.session.execute(text(
        'INSERT INTO adventurer_s1_world_outline_dungeon_states '
        '(target_dungeon, outline_revision, created_at, updated_at) '
        'VALUES (:dungeon, 0, :now, :now) '
        'ON CONFLICT (target_dungeon) DO NOTHING'
    ), {'dungeon': dungeon, 'now': now})
    return (
        AdventurerS1WorldOutlineDungeonState.query
        .filter_by(target_dungeon=dungeon)
        .with_for_update()
        .populate_existing()
        .one()
    )


def _record_cycle_reseed_result(
    state: AdventurerS1WorldOutlineDungeonState,
    status: str,
    *,
    attempted_at: datetime,
    stage: str,
    reason: str | None = None,
    diagnostics: list | None = None,
    source: dict | None = None,
    application: dict | None = None,
) -> None:
    """在地牢聚合上保留最近一次循环播种结果，供只读审计区分各阶段。"""
    allowed_statuses = {'claimed', 'rejected', 'failed', 'applied'}
    if status not in allowed_statuses:
        raise ValueError(f'未知循环播种状态: {status}')
    result = {
        'status': status,
        'stage': str(stage or '')[:64],
        'attempted_at': attempted_at.isoformat(),
    }
    if reason:
        result['reason'] = str(reason)[:1000]
    if diagnostics:
        result['diagnostics'] = [str(item)[:500] for item in diagnostics[:10]]
    if source:
        result['source'] = {
            'source_world_story_thread_id': _coerce_int(
                source.get('source_world_story_thread_id'), 0,
            ),
            'source_revision': _coerce_int(source.get('source_revision'), 0),
            'title': _clip_text(source.get('title'), 120),
        }
    if application:
        location = application.get('location_state_application') or {}
        explorability = application.get('dungeon_explorability_application') or {}
        result['application'] = {
            'location_id': _coerce_int(location.get('location_id'), 0),
            'location_name': _clip_text(location.get('location_name'), 120),
            'from_revision': _coerce_int(location.get('from_revision'), 0),
            'to_revision': _coerce_int(location.get('to_revision'), 0),
            'previous_is_explorable': explorability.get('previous_is_explorable'),
            'is_explorable': explorability.get('is_explorable'),
        }
    state.cycle_reseed_last_status = status
    state.cycle_reseed_last_attempted_at = attempted_at
    state.cycle_reseed_last_result = result

def _world_story_head_clause():
    """Legacy NULL rows and explicit heads are both current chain nodes."""
    return or_(
        AdventurerS1WorldStoryThread.is_chain_head.is_(True),
        AdventurerS1WorldStoryThread.is_chain_head.is_(None),
    )


def _effective_world_story_base_dungeon(row: AdventurerS1WorldStoryThread) -> str:
    return str(
        getattr(row, 'base_dungeon', '')
        or getattr(row, '_legacy_base_dungeon', '')
        or ''
    ).strip()


def _world_story_thread_has_exact_dungeon_scope(
    row: AdventurerS1WorldStoryThread,
    dungeon: str | None,
) -> bool:
    """Require an explicit local scope; global eligibility alone is not relevance."""
    target = str(dungeon or '').strip()
    if not target:
        return False
    scope_keys = {
        str(value).strip() for value in (row.scope_keys or [])
        if str(value or '').strip()
    }
    return target == _effective_world_story_base_dungeon(row) or target in scope_keys


def _is_due_tavern_faction_interaction(
    row: AdventurerS1WorldStoryThread,
) -> bool:
    """Return whether an established tavern-facing faction stage reached its cadence window."""
    cadence_debt = max(
        int(getattr(row, 'missed_injection_runs', 0) or 0),
        int(getattr(row, 'closure_debt', 0) or 0),
    )
    return bool(
        _is_tavern_faction_relation(row)
        and getattr(row, 'status', None) in WORLD_INTEL_MUTABLE_STATUSES
        and str(getattr(row, 'next_reveal_hint', None) or '').strip()
        and cadence_debt >= _world_story_cadence_threshold()
    )


def _is_campaign_front(row: AdventurerS1WorldStoryThread) -> bool:
    """Campaign fronts reuse world-story lifecycle; the spine is admin-only."""
    return (
        str(getattr(row, 'campaign_role', '') or '').strip()
        in WORLD_CAMPAIGN_FRONT_ROLES
        and int(getattr(row, 'campaign_root_id', 0) or 0) > 0
    )


def _is_due_campaign_front(row: AdventurerS1WorldStoryThread) -> bool:
    """Give a campaign a bounded cadence without starving ordinary world lines."""
    from flask import current_app

    if not current_app.config.get('ADVENTURER_S1_WORLD_CAMPAIGN_ENABLED', True):
        return False
    if not _is_campaign_front(row) or row.status not in WORLD_INTEL_MUTABLE_STATUSES:
        return False
    due_after = max(1, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_CAMPAIGN_DUE_AFTER_RUNS'),
        2,
    ))
    return (
        int(getattr(row, 'injection_count', 0) or 0) <= 0
        or int(getattr(row, 'missed_injection_runs', 0) or 0) >= due_after
    )


def _tavern_faction_interaction_cooldown_ready(dungeon: str) -> bool:
    """Keep aggregate tavern-faction interactions sparse across multiple owed lines."""
    target_dungeon = str(dungeon or '').strip()
    rows = (
        AdventurerS1WorldStoryThread.query
        .filter(
            AdventurerS1WorldStoryThread.thread_type == 'faction',
            or_(
                AdventurerS1WorldStoryThread.base_dungeon == target_dungeon,
                AdventurerS1WorldStoryThread.base_dungeon
                == WORLD_INTEL_ALL_DUNGEONS_SCOPE,
                AdventurerS1WorldStoryThread.base_dungeon.is_(None),
            ),
        )
        .all()
    )
    _hydrate_legacy_world_story_base_dungeons(rows)
    latest_progress_log_id = max(
        (
            int(row.last_progress_log_id)
            for row in rows
            if _is_tavern_faction_relation(row)
            and _world_story_thread_has_exact_dungeon_scope(row, target_dungeon)
            and row.last_progress_log_id
        ),
        default=0,
    )
    if latest_progress_log_id <= 0:
        return True
    completed_since = (
        AdventurerS1ExpeditionLog.query
        .filter(
            _expedition_effective_dungeon_clause(target_dungeon),
            AdventurerS1ExpeditionLog.status == 'completed',
            AdventurerS1ExpeditionLog.id > latest_progress_log_id,
        )
        .count()
    )
    return completed_since >= _world_story_cadence_threshold()


def _hydrate_legacy_world_story_base_dungeons(rows: list[AdventurerS1WorldStoryThread]) -> None:
    """Resolve legacy ownership from the exact source log in one batch, without rewriting data."""
    legacy_rows = [
        row for row in rows
        if not str(getattr(row, 'base_dungeon', '') or '').strip()
        and getattr(row, 'last_seen_log_id', None)
    ]
    if not legacy_rows:
        return
    log_ids = list(dict.fromkeys(int(row.last_seen_log_id) for row in legacy_rows))
    dungeon_by_log_id = {
        int(log_id): str(hidden_dungeon or target_dungeon or '').strip()
        for log_id, target_dungeon, hidden_dungeon in (
            AdventurerS1ExpeditionLog.query
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.target_dungeon,
                AdventurerS1ExpeditionLog.hidden_dungeon,
            )
            .filter(AdventurerS1ExpeditionLog.id.in_(log_ids))
            .all()
        )
    }
    for row in legacy_rows:
        row._legacy_base_dungeon = dungeon_by_log_id.get(int(row.last_seen_log_id), '')


def _as_context_text(*values) -> str:
    parts: list[str] = []
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            text = value.strip()
        else:
            try:
                text = json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                text = str(value)
        if text:
            parts.append(text)
    return '\n'.join(parts)


def _normalize_context_terms(values) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    terms: list[str] = []
    for value in values:
        term = str(value or '').strip()
        if term:
            terms.append(term)
    return list(dict.fromkeys(terms))


def _build_world_intel_context(target_dungeon: str | None, context_text: str | None = None, context_keywords=None) -> tuple[str, list[str]]:
    dungeon = str(target_dungeon or '').strip()
    keywords = _normalize_context_terms(context_keywords)
    if dungeon:
        keywords.insert(0, dungeon)
    keywords = list(dict.fromkeys(keywords))
    text = _as_context_text(dungeon, context_text, keywords)
    return text, keywords


def _normalize_text_list(value, *, limit: int = 8, item_chars: int = 60) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value[:limit]:
        text = _clip_text(item, item_chars)
        if text:
            out.append(text)
    return out


def _clip_text(value, limit: int) -> str:
    text = str(value or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip()


def _soft_text(value) -> str:
    """Normalize prose fields without truncating; prompt rules carry the soft limit."""
    return str(value or '').strip()


def _strict_ai_text(value, *, limit: int | None = None) -> str:
    """Read one AI text field without turning numbers or booleans into prose."""
    if not isinstance(value, str):
        return ''
    text = value.strip()
    if limit is not None and len(text) > limit:
        return text[:limit].rstrip()
    return text


def _strict_ai_positive_integer(value) -> int:
    normalized, _changed = normalize_ascii_integer(value, minimum=1)
    return normalized if type(normalized) is int and normalized > 0 else 0


def _strict_ai_text_list(
    value,
    *,
    limit: int,
    item_chars: int,
) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:limit]:
        text = _strict_ai_text(item, limit=item_chars)
        if text:
            result.append(text)
    return result


def _normalize_ecology_continuity_debt(value: object) -> dict:
    """Project the backend-owned debt record before maintenance consumption."""
    if not isinstance(value, dict):
        return {}
    if value.get('status') != 'pending_world_outline':
        return {}
    raw_indexes = value.get('evidence_event_indexes')
    if not isinstance(raw_indexes, list) or not raw_indexes or len(raw_indexes) > 64:
        return {}
    indexes: list[int] = []
    for raw_index in raw_indexes:
        if (
            isinstance(raw_index, bool)
            or not isinstance(raw_index, int)
            or raw_index < 0
            or raw_index in indexes
        ):
            return {}
        indexes.append(raw_index)
    raw_ecology = value.get('resource_ecology')
    if not isinstance(raw_ecology, dict):
        return {}
    ecology = {
        key: _clip_text(raw_ecology.get(key), 1200)
        for key in (
            'current_state', 'change_basis', 'availability_constraints',
        )
        if _clip_text(raw_ecology.get(key), 1200)
    }
    if not ecology:
        return {}
    normalized = {
        'status': 'pending_world_outline',
        'reason_code': 'successor_ecology_missing',
        'evidence_event_indexes': indexes,
        'resource_ecology': ecology,
    }
    from plugins.adventurer_s1.services.s1_dungeon_ecology_identity_service import (
        project_ecology_identity_snapshot,
    )
    ecology_identity = project_ecology_identity_snapshot(
        value.get('ecology_identity')
    )
    if ecology_identity:
        normalized['ecology_identity'] = ecology_identity
    return normalized


def resolve_ecology_debt_from_location_state(
    full_log: dict,
    *,
    expedition_log_id: int,
    target_dungeon: str,
) -> dict | None:
    """Finish continuity from a settled local baseline, without inventing a successor.

    Called inside the completion transaction after the approved location batch
    was applied. The receipt and location writes commit or roll back together.
    """
    debt = _normalize_ecology_continuity_debt(
        full_log.get('ecology_continuity_debt')
    )
    if (
        not debt
        or full_log.get('status') != 'completed'
        or full_log.get('world_state_application_errors')
        or full_log.get('_review_failure_quarantine_receipt')
        or 'pending_location_state_changes' in full_log
        or 'pending_dungeon_explorability_change' in full_log
    ):
        return None
    events = full_log.get('events')
    if not isinstance(events, list) or any(
        index >= len(events)
        or not isinstance(events[index], str)
        or not events[index].strip()
        for index in debt['evidence_event_indexes']
    ):
        return None
    from plugins.adventurer_s1.services.s1_expedition_locus_service import (
        EXPEDITION_LOCUS_KIND_KNOWN_LOCATION,
        normalize_expedition_locus_snapshot,
        location_scope_key,
    )
    locus = normalize_expedition_locus_snapshot(
        full_log.get('_expedition_locus_snapshot'),
        expected_target_dungeon=target_dungeon,
        expected_expedition_log_id=expedition_log_id,
    )
    if not locus:
        return None
    scope_state, scope_key = resolve_persistent_story_scope_from_full_log(
        full_log,
        expected_target_dungeon=target_dungeon,
        expected_expedition_log_id=expedition_log_id,
    )
    applications = full_log.get('location_state_application')
    if scope_state != 'stable' or not isinstance(applications, list):
        return None
    if applications:
        # Multiple locations require an explicit semantic binding; do not choose
        # one by name or assume all of them describe the source ecology.
        if len(applications) != 1 or not isinstance(applications[0], dict):
            return None
        applied = applications[0]
        location_id = applied.get('location_id')
        before = applied.get('from_revision')
        revision = applied.get('to_revision')
        if (
            type(location_id) is not int or location_id <= 0
            or location_scope_key(location_id) != scope_key
            or type(before) is not int or before < 0
            or type(revision) is not int or revision != before + 1
            or not _strict_ai_text(applied.get('new_state'))
        ):
            return None
        basis = 'location_state_application'
    else:
        if locus['kind'] != EXPEDITION_LOCUS_KIND_KNOWN_LOCATION:
            return None
        snapshot = full_log.get('location_state_snapshot')
        if (
            not isinstance(snapshot, dict)
            or snapshot.get('target_dungeon') != target_dungeon
            or not isinstance(snapshot.get('locations'), list)
        ):
            return None
        matches = [
            item for item in snapshot['locations']
            if isinstance(item, dict)
            and type(item.get('location_id')) is int
            and item['location_id'] == locus['location_id']
        ]
        if len(matches) != 1:
            return None
        baseline = matches[0]
        location_id = baseline['location_id']
        revision = baseline.get('revision')
        if (
            type(revision) is not int or revision <= 0
            or revision != locus['selected_revision']
            or not _strict_ai_text(baseline.get('current_state'))
        ):
            return None
        basis = 'location_state_snapshot'
    return {
        **debt,
        'status': 'resolved_location_state',
        'resolution': {
            'expedition_log_id': expedition_log_id,
            'location_id': location_id,
            'revision': revision,
            'basis': basis,
        },
    }


def _project_ecology_debt_for_maintenance(full_log: object) -> dict:
    """Attach only the settled events referenced by the exact source debt."""
    if not isinstance(full_log, dict):
        return {}
    debt = _normalize_ecology_continuity_debt(
        full_log.get('ecology_continuity_debt')
    )
    if not debt:
        return {}
    events = full_log.get('events')
    events = events if isinstance(events, list) else []
    debt['evidence_events'] = [
        {'event_index': index, 'text': events[index]}
        for index in debt['evidence_event_indexes']
        if index < len(events)
        and isinstance(events[index], str)
        and events[index].strip()
    ]
    return debt


def _build_world_story_write_authority(
    candidate_payload: list[dict],
    existing_threads: list[dict],
    *,
    source_scope_by_log_id: dict[int, tuple[str, str | None]],
) -> dict:
    """Project the same backend authority later enforced by the consumer."""
    candidate_changes: list[dict] = []
    debt_existing_changes: list[dict] = []
    for thread in existing_threads:
        if not isinstance(thread, dict):
            continue
        source_id = _coerce_int(thread.get('id') or thread.get('thread_id'), 0)
        source_revision = _coerce_int(thread.get('revision'), 0)
        status = str(thread.get('status') or '').strip().lower()
        if source_id <= 0 or source_revision <= 0:
            continue
        if status in {'seeded', 'active', 'escalated'}:
            debt_existing_changes.append({
                'source_world_story_thread_id': source_id,
                'source_revision': source_revision,
                'change_kind': 'advance',
            })
        if status in WORLD_INTEL_TERMINAL_STATUSES:
            debt_existing_changes.append({
                'source_world_story_thread_id': source_id,
                'source_revision': source_revision,
                'change_kind': 'successor',
            })
    ecology_outlets: list[dict] = []
    for candidate in candidate_payload:
        if not isinstance(candidate, dict):
            continue
        candidate_id = _coerce_int(candidate.get('candidate_id'), 0)
        expedition_log_id = _coerce_int(candidate.get('expedition_log_id'), 0)
        if candidate_id <= 0 or expedition_log_id <= 0:
            continue
        for fact in candidate.get('world_fact_updates') or []:
            if not isinstance(fact, dict):
                continue
            change_kind = _strict_ai_text(fact.get('change_kind')).lower()
            if change_kind not in {
                'new', 'advance', 'close', 'resolve', 'successor',
            }:
                continue
            authority = {
                'candidate_id': candidate_id,
                'expedition_log_id': expedition_log_id,
                'change_kind': change_kind,
            }
            if change_kind != 'new':
                authority.update({
                    'source_world_story_thread_id': (
                        _strict_ai_positive_integer(
                            fact.get('source_world_story_thread_id')
                        )
                    ),
                    'source_revision': _strict_ai_positive_integer(
                        fact.get('source_revision')
                    ),
                })
            candidate_changes.append(authority)
        if _normalize_ecology_continuity_debt(
            candidate.get('ecology_continuity_debt')
        ):
            scope_state, scope_key = source_scope_by_log_id.get(
                expedition_log_id, ('unresolved', None),
            )
            ecology_outlets.append({
                'candidate_id': candidate_id,
                'expedition_log_id': expedition_log_id,
                'independent_new_allowed': scope_state in {'stable', 'legacy'},
                'source_scope': {'state': scope_state, 'scope_key': scope_key},
                'requires_resolution_receipt': True,
            })
    return {
        'candidate_changes': candidate_changes,
        'ecology_continuity_debt_allowed_existing_changes': (
            [dict(item) for item in debt_existing_changes]
            if ecology_outlets else []
        ),
        'ecology_continuity_debt_outlets': ecology_outlets,
    }


def _merge_dm_handoff_notes(*values: object) -> str:
    """Combine exact handoff records without interpreting their prose semantics."""
    notes: list[str] = []
    for value in values:
        note = _soft_text(value)
        if note and note not in notes:
            notes.append(note)
    return '\n'.join(notes)


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _validate_cycle_reseed_maintenance_candidate(
    parsed: object,
    maintained: object,
    *,
    existing_threads: list[dict],
    authoritative_location_state_snapshot: dict,
    dungeon: str,
) -> tuple[list[dict], str | None]:
    """Validate the complete cycle-reseed role contract without side effects."""
    contract_error = (
        '循环播种维护原始输出必须且只包含一个绑定准确终态前驱的 '
        'seeded cycle_reseed 后继'
    )
    if not isinstance(parsed, dict) or not isinstance(maintained, dict):
        return [], contract_error

    required_fields = {
        'predecessor_id', 'change_kind',
        'source_world_story_thread_id', 'source_revision',
        'cycle_reseed', 'title', 'thread_type', 'status',
        'scope_keys', 'summary', 'weight',
    }
    optional_fields = {
        'trigger_keywords', 'next_reveal_hint',
        'power_tier', 'power_profile',
    }
    output_fields = required_fields | optional_fields
    raw_rows = parsed.get('world_story_threads')
    raw_row = (
        raw_rows[0]
        if isinstance(raw_rows, list) and len(raw_rows) == 1
        else raw_rows if isinstance(raw_rows, dict) else None
    )
    normalized_rows = maintained.get('world_story_threads', [])
    normalized_row = (
        normalized_rows[0]
        if isinstance(normalized_rows, list)
        and len(normalized_rows) == 1
        and isinstance(normalized_rows[0], dict)
        else None
    )
    raw_transition = parsed.get('cycle_reseed_transition')
    normalized_transition = maintained.get('cycle_reseed_transition')
    location_rows = {
        _coerce_int(row.get('location_id'), 0): row
        for row in (
            authoritative_location_state_snapshot.get('locations') or []
        )
        if isinstance(row, dict)
        and _coerce_int(row.get('location_id'), 0) > 0
    }
    transition_location = (
        normalized_transition.get('location_state_change')
        if isinstance(normalized_transition, dict)
        else None
    )
    transition_explorability = (
        normalized_transition.get('dungeon_explorability_change')
        if isinstance(normalized_transition, dict)
        else None
    )
    location_id = (
        _coerce_int(transition_location.get('location_id'), 0)
        if isinstance(transition_location, dict)
        else 0
    )
    frozen_location = location_rows.get(location_id)
    location_valid = bool(
        isinstance(transition_location, dict)
        and (
            (
                location_id > 0
                and isinstance(frozen_location, dict)
                and _coerce_int(
                    transition_location.get('expected_revision'), -1,
                ) == _coerce_int(frozen_location.get('revision'), -2)
                and (
                    not transition_location.get('location_name')
                    or transition_location.get('location_name')
                    == frozen_location.get('location_name')
                )
            )
            or (
                location_id <= 0
                and _coerce_int(
                    transition_location.get('expected_revision'), -1,
                ) == 0
                and isinstance(
                    transition_location.get('location_name'), str,
                )
                and transition_location['location_name'].strip()
            )
        )
    )
    explorability_valid = bool(
        isinstance(transition_explorability, dict)
        and transition_explorability.get('expected_is_explorable') is False
        and transition_explorability.get('is_explorable') is True
    )
    raw_scope_keys = (
        raw_row.get('scope_keys') if isinstance(raw_row, dict) else None
    )
    raw_trigger_keywords = (
        raw_row.get('trigger_keywords')
        if isinstance(raw_row, dict) and 'trigger_keywords' in raw_row
        else []
    )
    scope_keys_valid = bool(
        isinstance(raw_scope_keys, list)
        and 1 <= len(raw_scope_keys) <= 8
        and all(
            isinstance(value, str) and value.strip()
            for value in raw_scope_keys
        )
        and len({value.strip() for value in raw_scope_keys})
        == len(raw_scope_keys)
        and dungeon in {value.strip() for value in raw_scope_keys}
    )
    trigger_keywords_valid = bool(
        isinstance(raw_trigger_keywords, list)
        and len(raw_trigger_keywords) <= 10
        and all(
            isinstance(value, str) and value.strip()
            for value in raw_trigger_keywords
        )
        and len({value.strip() for value in raw_trigger_keywords})
        == len(raw_trigger_keywords)
    )
    valid_sources = {
        (
            _coerce_int(row.get('id') or row.get('thread_id'), 0),
            _coerce_int(row.get('revision'), 0),
        )
        for row in existing_threads
        if isinstance(row, dict)
        and str(row.get('status') or '').strip().lower()
        in WORLD_INTEL_TERMINAL_STATUSES
    }
    predecessor_id = (
        normalized_row.get('predecessor_id')
        if isinstance(normalized_row, dict)
        else 0
    )
    source_id = (
        normalized_row.get('source_world_story_thread_id')
        if isinstance(normalized_row, dict)
        else 0
    )
    source_revision = (
        normalized_row.get('source_revision')
        if isinstance(normalized_row, dict)
        else 0
    )
    if (
        parsed.get('dm_handoff_note') != ''
        or parsed.get('world_actors') != []
        or parsed.get('ecology_continuity_debt_resolutions') not in (None, [])
        or not isinstance(raw_transition, dict)
        or not isinstance(normalized_transition, dict)
        or not location_valid
        or not explorability_valid
        or not isinstance(raw_row, dict)
        or not required_fields.issubset(raw_row)
        or normalized_row is None
        or normalized_row.get('cycle_reseed') is not True
        or raw_row.get('change_kind') != 'successor'
        or raw_row.get('status') != 'seeded'
        or predecessor_id != source_id
        or source_id <= 0
        or source_revision <= 0
        or (source_id, source_revision) not in valid_sources
        or not isinstance(raw_row.get('title'), str)
        or not raw_row['title'].strip()
        or not isinstance(raw_row.get('summary'), str)
        or not raw_row['summary'].strip()
        or raw_row.get('thread_type') not in {
            'entity', 'event', 'faction', 'location', 'omen', 'artifact',
        }
        or not scope_keys_valid
        or not isinstance(normalized_row.get('weight'), int)
        or isinstance(normalized_row.get('weight'), bool)
        or not 1 <= normalized_row['weight'] <= 100
        or not trigger_keywords_valid
        or any(
            not isinstance(raw_row.get(field), str)
            for field in ('next_reveal_hint', 'power_tier', 'power_profile')
            if field in raw_row
        )
    ):
        return [], contract_error
    return ([{
        key: normalized_row[key]
        for key in output_fields
        if key in normalized_row
    }], None)


def _normalize_maintenance_debt_resolution_contract(
    maintained: dict,
    *,
    payload: list[dict],
    world_story_write_authority: dict,
    ecology_continuity_debt_log_ids: list[int],
    has_ecology_continuity_debt: bool,
    diagnostics: list[str],
) -> tuple[bool, list[dict], dict[int, set[int]]]:
    """Validate debt receipts and apply the existing deterministic note fallback."""
    raw_resolutions = maintained.get(
        'ecology_continuity_debt_resolutions', []
    )
    candidate_id_by_log_id = {
        _coerce_int(row.get('expedition_log_id'), 0): _coerce_int(
            row.get('candidate_id'), 0,
        )
        for row in payload
        if isinstance(row, dict)
        and _normalize_ecology_continuity_debt(
            row.get('ecology_continuity_debt')
        )
    }
    outlet_by_log_id = {
        _coerce_int(outlet.get('expedition_log_id'), 0): outlet
        for outlet in world_story_write_authority.get(
            'ecology_continuity_debt_outlets', []
        )
        if isinstance(outlet, dict)
    }
    allowed_existing_changes = {
        (
            _coerce_int(item.get('source_world_story_thread_id'), 0),
            _coerce_int(item.get('source_revision'), 0),
            str(item.get('change_kind') or ''),
        )
        for item in world_story_write_authority.get(
            'ecology_continuity_debt_allowed_existing_changes', []
        )
        if isinstance(item, dict)
    }
    seen_log_ids: set[int] = set()
    normalized_resolutions: list[dict] = []
    authority_by_index: dict[int, set[int]] = {}
    resolutions_valid = isinstance(raw_resolutions, list)
    for raw_resolution in raw_resolutions or []:
        resolution_fields = {
            'expedition_log_id', 'decision',
            'world_story_thread_indexes',
        }
        if (
            not isinstance(raw_resolution, dict)
            or not resolution_fields.issubset(raw_resolution)
        ):
            resolutions_valid = False
            continue
        resolution = {
            key: raw_resolution.get(key) for key in resolution_fields
        }
        expedition_log_id, log_id_changed = normalize_ascii_integer(
            resolution.get('expedition_log_id'),
            minimum=1,
        )
        decision = resolution.get('decision')
        raw_indexes = resolution.get('world_story_thread_indexes')
        indexes: list[int] = []
        indexes_valid = isinstance(raw_indexes, list)
        if indexes_valid:
            for raw_index in raw_indexes:
                normalized_index, index_changed = normalize_ascii_integer(
                    raw_index,
                    minimum=0,
                )
                if type(normalized_index) is not int:
                    indexes_valid = False
                    break
                indexes.append(normalized_index)
                if index_changed:
                    diagnostics.append(
                        'ecology_continuity_debt_resolutions 的世界线下标'
                        '数字字符串已无损转换为整数'
                    )
        if log_id_changed:
            diagnostics.append(
                'ecology_continuity_debt_resolutions.expedition_log_id '
                '数字字符串已无损转换为整数'
            )
        if (
            type(expedition_log_id) is not int
            or expedition_log_id <= 0
            or expedition_log_id in seen_log_ids
            or expedition_log_id not in ecology_continuity_debt_log_ids
            or decision not in {'world_story_thread', 'dm_handoff_note'}
            or not indexes_valid
            or any(
                index < 0
                or index >= len(maintained.get('world_story_threads') or [])
                for index in indexes
            )
            or len(set(indexes)) != len(indexes)
        ):
            resolutions_valid = False
            continue
        if decision == 'dm_handoff_note':
            if indexes or not maintained.get('dm_handoff_note'):
                resolutions_valid = False
                continue
        else:
            outlet = outlet_by_log_id.get(expedition_log_id) or {}
            if not indexes or any(
                (
                    maintained['world_story_threads'][index].get(
                        'change_kind'
                    ) not in {'new', 'advance', 'successor'}
                    or maintained['world_story_threads'][index].get(
                        'status'
                    ) not in {'seeded', 'active', 'escalated'}
                )
                for index in indexes
            ):
                resolutions_valid = False
                continue
            if any(
                not (
                    (
                        maintained['world_story_threads'][index].get(
                            'change_kind'
                        ) == 'new'
                        and outlet.get('independent_new_allowed') is True
                    )
                    or (
                        (
                            _coerce_int(
                                maintained['world_story_threads'][index].get(
                                    'source_world_story_thread_id'
                                ),
                                0,
                            ),
                            _coerce_int(
                                maintained['world_story_threads'][index].get(
                                    'source_revision'
                                ),
                                0,
                            ),
                            str(
                                maintained['world_story_threads'][index].get(
                                    'change_kind'
                                ) or ''
                            ),
                        ) in allowed_existing_changes
                    )
                )
                for index in indexes
            ):
                resolutions_valid = False
                continue
            candidate_id = candidate_id_by_log_id.get(expedition_log_id, 0)
            if candidate_id <= 0:
                resolutions_valid = False
                continue
            for index in indexes:
                authority_by_index.setdefault(index, set()).add(candidate_id)
        normalized_resolutions.append({
            'expedition_log_id': expedition_log_id,
            'decision': decision,
            'world_story_thread_indexes': indexes,
        })
        seen_log_ids.add(expedition_log_id)
    resolutions_valid = (
        bool(
            resolutions_valid
            and seen_log_ids == set(ecology_continuity_debt_log_ids)
        )
        if has_ecology_continuity_debt
        else bool(resolutions_valid and not raw_resolutions)
    )
    if has_ecology_continuity_debt and not resolutions_valid:
        debt_candidate_notes = [
            row.get('dm_handoff_note_candidate')
            for row in payload
            if _coerce_int(row.get('expedition_log_id'), 0)
            in ecology_continuity_debt_log_ids
        ]
        fallback_note = _merge_dm_handoff_notes(
            maintained.get('dm_handoff_note'),
            *debt_candidate_notes,
        )
        if fallback_note:
            maintained['dm_handoff_note'] = fallback_note
            maintained['ecology_continuity_debt_resolutions'] = [
                {
                    'expedition_log_id': expedition_log_id,
                    'decision': 'dm_handoff_note',
                    'world_story_thread_indexes': [],
                }
                for expedition_log_id in ecology_continuity_debt_log_ids
            ]
            normalized_resolutions = [
                dict(item)
                for item in maintained[
                    'ecology_continuity_debt_resolutions'
                ]
            ]
            authority_by_index = {}
            resolutions_valid = True
            diagnostics.append(
                ECOLOGY_CONTINUITY_DEBT_HANDOFF_FALLBACK_DIAGNOSTIC
            )
    return resolutions_valid, normalized_resolutions, authority_by_index


def _lock_maintenance_world_story_threads(
    maintained: dict,
    *,
    dungeon: str | None = None,
) -> list[int]:
    """Prelock every existing world-thread reference in one deterministic order."""
    if not isinstance(maintained, dict):
        return []
    thread_ids: set[int] = set()
    thread_titles: set[str] = set()

    def add_id(value) -> None:
        thread_id = _strict_ai_positive_integer(value)
        if thread_id > 0:
            thread_ids.add(thread_id)

    def add_title(value) -> None:
        title = _strict_ai_text(value, limit=120)
        if title:
            thread_titles.add(title)

    raw_world_threads = maintained.get('world_story_threads') or []
    for raw_thread in raw_world_threads:
        if not isinstance(raw_thread, dict):
            continue
        for field in (
            'id', 'thread_id', 'predecessor_id',
            'source_world_story_thread_id',
        ):
            add_id(raw_thread.get(field))
        add_title(raw_thread.get('title'))

    for container_name in ('world_actors', 'faction_profile_updates'):
        for raw_item in maintained.get(container_name) or []:
            if not isinstance(raw_item, dict):
                continue
            for raw_link in raw_item.get('thread_links') or []:
                if not isinstance(raw_link, dict):
                    continue
                add_id(
                    raw_link.get('thread_id')
                    or raw_link.get('world_story_thread_id')
                )
                add_title(raw_link.get('thread_title'))

    bootstrap_sources = maintained.get(
        '_faction_profile_bootstrap_source_overrides'
    )
    if isinstance(bootstrap_sources, dict):
        for raw_thread_id in bootstrap_sources:
            add_id(raw_thread_id)

    clauses = []
    if thread_ids:
        clauses.append(AdventurerS1WorldStoryThread.id.in_(thread_ids))
    if thread_titles:
        clauses.append(AdventurerS1WorldStoryThread.title.in_(thread_titles))
    if dungeon and any(
        isinstance(raw_thread, dict)
        and raw_thread.get('cycle_reseed') is True
        for raw_thread in raw_world_threads
    ):
        clauses.append(and_(
            _world_story_head_clause(),
            AdventurerS1WorldStoryThread.base_dungeon == dungeon,
            AdventurerS1WorldStoryThread.status.in_(
                WORLD_INTEL_MUTABLE_STATUSES,
            ),
        ))
    if not clauses:
        return []
    rows = (
        AdventurerS1WorldStoryThread.query
        .filter(or_(*clauses))
        .order_by(AdventurerS1WorldStoryThread.id.asc())
        .with_for_update()
        .populate_existing()
        .all()
    )
    return [int(row.id) for row in rows]


def _restore_codenames_in_value(value, codename_to_real: dict[str, str]):
    if not codename_to_real:
        return value
    if isinstance(value, str):
        return restore_names_in_narrative(value, codename_to_real)
    if isinstance(value, list):
        return [_restore_codenames_in_value(item, codename_to_real) for item in value]
    if isinstance(value, dict):
        return {
            key: _restore_codenames_in_value(raw, codename_to_real)
            for key, raw in value.items()
        }
    return value


def _build_alias_context_from_snapshot(log_id: int, snapshot: list | None, id_to_name: dict[int, str]) -> dict:
    if not isinstance(snapshot, list):
        return {'codename_to_real': {}, 'alias_rows': []}
    codename_to_real = build_codename_to_real_from_snapshot(snapshot, id_to_name)
    alias_rows = []
    for idx, row in enumerate(snapshot):
        if not isinstance(row, dict):
            continue
        adventurer_id = row.get('adventurer_id')
        if not isinstance(adventurer_id, int):
            continue
        real = id_to_name.get(adventurer_id)
        if not real:
            continue
        alias_rows.append({
            'scope': f'expedition_log:{int(log_id)}',
            'alias': codename_for_member_index(idx),
            'bare_alias': f'M{idx + 1}',
            'adventurer_id': adventurer_id,
            'name': real,
        })
    return {'codename_to_real': codename_to_real, 'alias_rows': alias_rows}


def _load_alias_contexts_for_logs(log_ids: list[int]) -> dict[int, dict]:
    ids = [int(log_id) for log_id in dict.fromkeys(log_ids or []) if log_id]
    if not ids:
        return {}
    logs = (
        AdventurerS1ExpeditionLog.query
        .with_entities(AdventurerS1ExpeditionLog.id, AdventurerS1ExpeditionLog.full_log)
        .filter(AdventurerS1ExpeditionLog.id.in_(ids))
        .all()
    )
    snapshots_by_log: dict[int, list] = {}
    frozen_names_by_log: dict[int, dict[int, str]] = {}
    adventurer_ids: set[int] = set()
    for log_id, full_log in logs:
        snapshot = full_log.get('audit_member_snapshot') if isinstance(full_log, dict) else None
        if not isinstance(snapshot, list):
            continue
        snapshots_by_log[int(log_id)] = snapshot
        frozen_names = full_log.get('_member_id_to_name')
        frozen_names = frozen_names if isinstance(frozen_names, dict) else {}
        names_for_log: dict[int, str] = {}
        for row in snapshot:
            if isinstance(row, dict) and isinstance(row.get('adventurer_id'), int):
                adventurer_id = int(row['adventurer_id'])
                name = frozen_names.get(str(adventurer_id), frozen_names.get(adventurer_id))
                if isinstance(name, str) and name:
                    names_for_log[adventurer_id] = name
                else:
                    adventurer_ids.add(adventurer_id)
        frozen_names_by_log[int(log_id)] = names_for_log
    id_to_name = dict(
        AdventurerS1.query
        .with_entities(AdventurerS1.id, AdventurerS1.name)
        .filter(AdventurerS1.id.in_(list(adventurer_ids)))
        .all()
    ) if adventurer_ids else {}
    return {
        log_id: _build_alias_context_from_snapshot(
            log_id, snapshot, {**id_to_name, **frozen_names_by_log[log_id]},
        )
        for log_id, snapshot in snapshots_by_log.items()
    }


def _world_intel_snapshot_items(full_log: dict | None) -> dict[int, dict]:
    """Merge frozen objective/generation snapshots; generation wins on duplicate ids."""
    result: dict[int, dict] = {}
    if not isinstance(full_log, dict):
        return result
    for field in ('objective_world_intel_injection', 'generation_world_intel_injection'):
        snapshot = full_log.get(field)
        for item in (snapshot.get('items') if isinstance(snapshot, dict) else []) or []:
            if not isinstance(item, dict):
                continue
            thread_id = _coerce_int(item.get('id') or item.get('world_story_thread_id'), 0)
            if thread_id > 0:
                result[thread_id] = item
    return result


def _world_intel_adoption_modes(full_log: dict | None) -> dict[int, str]:
    """Read exact objective-outline adoption decisions without interpreting prose."""
    if not isinstance(full_log, dict):
        return {}
    result: dict[int, str] = {}
    for item in full_log.get('world_intel_adoptions') or []:
        if not isinstance(item, dict):
            continue
        thread_id = _coerce_int(item.get('world_story_thread_id'), 0)
        usage_mode = str(item.get('usage_mode') or '').strip()
        if thread_id > 0 and usage_mode:
            result[thread_id] = usage_mode
    return result


def _faction_presence_targets(full_log: dict | None) -> dict[tuple[int, int], dict]:
    """Read only exact server-frozen profile/thread targets from injection snapshots."""
    targets: dict[tuple[int, int], dict] = {}
    for thread_id, item in _world_intel_snapshot_items(full_log).items():
        if item.get('required_faction_presence') is not True:
            continue
        target = item.get('faction_presence_target')
        if not isinstance(target, dict):
            continue
        profile_id = _strict_ai_positive_integer(target.get('faction_profile_id'))
        target_thread_id = _strict_ai_positive_integer(
            target.get('world_story_thread_id')
        )
        profile_revision = _strict_ai_positive_integer(
            target.get('profile_revision')
        )
        source_revision = _strict_ai_positive_integer(target.get('source_revision'))
        if (
            profile_id <= 0
            or target_thread_id != int(thread_id)
            or profile_revision <= 0
            or source_revision <= 0
        ):
            continue
        targets[(profile_id, target_thread_id)] = {
            'faction_profile_id': profile_id,
            'profile_revision': profile_revision,
            'world_story_thread_id': target_thread_id,
            'source_revision': source_revision,
        }
    return targets


def _valid_faction_presence_receipt_pairs(
    full_log: dict | None,
) -> set[tuple[int, int]]:
    """Validate enriched auxiliary receipts mechanically; never infer from prose."""
    if not isinstance(full_log, dict):
        return set()
    targets = _faction_presence_targets(full_log)
    events = full_log.get('events') if isinstance(full_log.get('events'), list) else []
    valid: set[tuple[int, int]] = set()
    for receipt in full_log.get(FACTION_PRESENCE_RECEIPTS_FIELD) or []:
        if not isinstance(receipt, dict):
            continue
        profile_id = _strict_ai_positive_integer(receipt.get('faction_profile_id'))
        thread_id = _strict_ai_positive_integer(
            receipt.get('world_story_thread_id')
        )
        target = targets.get((profile_id, thread_id))
        if not target:
            continue
        if (
            _strict_ai_positive_integer(receipt.get('profile_revision'))
            != target['profile_revision']
            or _strict_ai_positive_integer(receipt.get('source_revision'))
            != target['source_revision']
        ):
            continue
        indexes = receipt.get('evidence_event_indexes')
        if not isinstance(indexes, list) or not indexes:
            continue
        if any(
            type(index) is not int
            or index < 0
            or index >= len(events)
            for index in indexes
        ):
            continue
        if len(indexes) != len(set(indexes)):
            continue
        valid.add((profile_id, thread_id))
    return valid


def _project_faction_presence_story_evidence(
    full_log: dict | None,
) -> list[dict]:
    """Project exact Observer-approved story evidence for normal maintenance."""
    if not isinstance(full_log, dict):
        return []
    targets = _faction_presence_targets(full_log)
    valid_pairs = _valid_faction_presence_receipt_pairs(full_log)
    events = full_log.get('events') if isinstance(full_log.get('events'), list) else []
    usage_modes = _world_intel_adoption_modes(full_log)
    projected: list[dict] = []
    seen_pairs: set[tuple[int, int]] = set()
    for receipt in full_log.get(FACTION_PRESENCE_RECEIPTS_FIELD) or []:
        if not isinstance(receipt, dict):
            continue
        pair = (
            _strict_ai_positive_integer(receipt.get('faction_profile_id')),
            _strict_ai_positive_integer(receipt.get('world_story_thread_id')),
        )
        if pair not in valid_pairs or pair in seen_pairs:
            continue
        target = targets.get(pair)
        if not target:
            continue
        story_events: list[dict] = []
        for event_index in receipt.get('evidence_event_indexes') or []:
            if (
                type(event_index) is not int
                or event_index < 0
                or event_index >= len(events)
                or not isinstance(events[event_index], str)
                or not events[event_index].strip()
            ):
                continue
            story_events.append({
                'event_index': event_index,
                'event_text': events[event_index].strip(),
            })
        if not story_events:
            continue
        seen_pairs.add(pair)
        projected.append({
            **target,
            'usage_mode': usage_modes.get(
                target['world_story_thread_id'], 'background_reference',
            ),
            'story_events': story_events,
        })
        if len(projected) >= 1:
            break
    return projected


def _authorize_world_fact_updates_for_log(
    facts: list[dict],
    full_log: dict | None,
    *,
    expedition_log_id: int,
) -> list[dict]:
    """Bind public-line mutations to exact ids/revisions granted in the departure snapshot."""
    snapshot_items = _world_intel_snapshot_items(full_log)
    adoption_modes = _world_intel_adoption_modes(full_log)
    has_adoption_contract = (
        isinstance(full_log, dict) and 'world_intel_adoptions' in full_log
    )
    authorized: list[dict] = []
    for fact in facts:
        change_kind = _strict_ai_text(fact.get('change_kind')).lower()
        if change_kind not in {'new', 'advance', 'close', 'resolve', 'successor'}:
            continue
        if change_kind == 'new':
            authorized.append(fact)
            continue
        source_id = _strict_ai_positive_integer(
            fact.get('source_world_story_thread_id')
        )
        item = snapshot_items.get(source_id)
        # 兼容部署前已经发车的快照：缺 access_mode 的旧条目原本就是独占注入。
        access_mode = str((item or {}).get('access_mode') or 'mutable').strip()
        if (
            not item
            or access_mode != 'mutable'
            or (
                has_adoption_contract
                and adoption_modes.get(source_id) != 'exclusive_direct_use'
            )
        ):
            logger.warning(
                'S1 公共事实写入越权，已丢弃 log=%s source_thread=%s change=%s',
                expedition_log_id, source_id, change_kind,
            )
            continue
        bound = dict(fact)
        bound['source_revision'] = max(1, _coerce_int(item.get('revision'), 1))
        authorized.append(bound)
    return authorized


class S1WorldOutlineService:
    @staticmethod
    def create_candidate_from_result(
        *,
        expedition_log_id: int,
        target_dungeon: str,
        result_data: dict | None,
    ) -> AdventurerS1WorldFactCandidate | None:
        """保存主探险 AI 输出的世界事实 / 便签候选。调用方负责事务。"""
        from .adventurer_s1_config_service import is_s1_independent_dungeon
        if is_s1_independent_dungeon(target_dungeon):
            return None
        if not isinstance(result_data, dict):
            return None
        if 'world_fact_updates' in result_data:
            raw_facts = result_data.get('world_fact_updates')
        elif 'world_fact_update' in result_data:
            raw_facts = result_data.get('world_fact_update')
        else:
            raw_facts = result_data.get('world_fact_updates_candidate')
        if 'dm_handoff_note' in result_data:
            raw_note = result_data.get('dm_handoff_note')
        else:
            raw_note = result_data.get('dm_handoff_note_candidate')
        facts, fact_diagnostics = normalize_world_fact_updates(raw_facts)
        note, note_diagnostics = normalize_dm_handoff_note(raw_note)
        ecology_debt = _normalize_ecology_continuity_debt(
            result_data.get('ecology_continuity_debt')
        )
        if ecology_debt:
            debt_note = (
                '旧资源关系已经终结或失效；后续必须承认这一既成结果，'
                '并由仍在持续作用的因果自然形成可供后来者接触的新阶段。'
            )
            if debt_note not in note:
                note = '\n'.join(
                    value for value in (note, debt_note) if value
                )
        diagnostics = fact_diagnostics + note_diagnostics
        if diagnostics:
            logger.warning(
                'S1 世界大纲候选局部丢弃 log=%s diagnostics=%s',
                expedition_log_id,
                diagnostics,
            )
        log = AdventurerS1ExpeditionLog.query.filter_by(id=int(expedition_log_id)).first()
        log_full = dict(log.full_log or {}) if log and isinstance(log.full_log, dict) else {}
        faction_presence_story_evidence = (
            _project_faction_presence_story_evidence(result_data)
            or _project_faction_presence_story_evidence(log_full)
        )
        facts = _authorize_world_fact_updates_for_log(
            facts,
            log_full,
            expedition_log_id=int(expedition_log_id),
        )
        from plugins.adventurer_s1.services.s1_campaign_dispatch_contract import project_dispatch_facts
        facts = project_dispatch_facts(facts, log_full.get('campaign_dispatch'))
        alias_context = _load_alias_contexts_for_logs([int(expedition_log_id)]).get(int(expedition_log_id), {})
        codename_to_real = alias_context.get('codename_to_real') or {}
        if codename_to_real:
            facts = _restore_codenames_in_value(facts, codename_to_real)
            note = _soft_text(
                restore_names_in_narrative(note, codename_to_real),
            )
        existing = AdventurerS1WorldFactCandidate.query.filter_by(
            expedition_log_id=int(expedition_log_id),
        ).first()
        # Location continuity is already reviewed with the final expedition. It
        # remains maintenance input even when no world thread or note was created.
        location_context = any(
            (
                isinstance(source.get('location_state_snapshot'), dict)
                and bool(source['location_state_snapshot'].get('locations'))
            )
            or bool(source.get('location_state_application'))
            for source in (result_data, log_full)
        )
        from flask import current_app
        from plugins.adventurer_s1.services.s1_encounter_preparation_service import eligible
        preparation_context = bool(
            current_app.config.get('ADVENTURER_S1_ENCOUNTER_PREPARATION_ENABLED', True)
            and eligible(log_full)
        )
        if not facts and not note and not faction_presence_story_evidence and not location_context and not preparation_context:
            if existing and existing.status == 'awaiting_completion':
                existing.status = 'discarded'
                existing.world_fact_updates = []
                existing.dm_handoff_note_candidate = ''
            return None
        if existing:
            if existing.status not in {
                'awaiting_completion', 'pending', 'discarded',
            }:
                # A maintenance receipt describes the exact consumed candidate.
                # Idempotent settlement replay must not rewrite that evidence.
                return existing
            existing.target_dungeon = target_dungeon
            existing.world_fact_updates = facts
            existing.dm_handoff_note_candidate = note
            existing.status = 'awaiting_completion'
            existing.outline_error = None
            existing.maintenance_issue_code = None
            existing.maintenance_receipt = None
            existing.maintenance_recovery_kind = None
            existing.maintenance_recovery_attempts = 0
            existing.processed_at = None
            return existing
        candidate = AdventurerS1WorldFactCandidate(
            expedition_log_id=int(expedition_log_id),
            target_dungeon=str(target_dungeon or '').strip() or '未知地牢',
            world_fact_updates=facts,
            dm_handoff_note_candidate=note,
            status='awaiting_completion',
        )
        db.session.add(candidate)
        return candidate

    @staticmethod
    def publish_candidate_for_completed_log(
        *,
        expedition_log_id: int,
        target_dungeon: str,
        final_result_data: dict | None,
    ) -> bool:
        """Refresh the observer-approved draft, record lifecycle debt, then publish atomically."""
        from .adventurer_s1_config_service import is_s1_independent_dungeon
        if is_s1_independent_dungeon(target_dungeon):
            return False
        log = (
            AdventurerS1ExpeditionLog.query
            .filter_by(id=int(expedition_log_id))
            .with_for_update()
            .first()
        )
        if not log or log.status != 'completed':
            return False
        candidate = S1WorldOutlineService.create_candidate_from_result(
            expedition_log_id=int(expedition_log_id),
            target_dungeon=target_dungeon,
            result_data=final_result_data,
        )
        facts = candidate.world_fact_updates if candidate else []
        S1WorldOutlineService._record_completed_world_story_run(
            log,
            target_dungeon=target_dungeon,
            world_fact_updates=facts,
        )
        if not candidate or candidate.status != 'awaiting_completion':
            return False
        candidate.status = 'pending'
        candidate.outline_error = None
        candidate.maintenance_issue_code = None
        candidate.maintenance_receipt = None
        candidate.maintenance_recovery_kind = None
        candidate.maintenance_recovery_attempts = 0
        candidate.processed_at = None
        return True

    @staticmethod
    def _record_completed_world_story_run(
        log: AdventurerS1ExpeditionLog,
        *,
        target_dungeon: str,
        world_fact_updates: list[dict] | None,
    ) -> None:
        """Use completed eligible runs—not generation time—as exposure/closure clock."""
        from flask import current_app

        full_log = dict(log.full_log) if isinstance(log.full_log, dict) else {}
        completion_clock = full_log.get('_world_story_completion_clock')
        if (
            isinstance(completion_clock, dict)
            and _coerce_int(completion_clock.get('recorded_log_id'), 0)
            == int(log.id)
        ):
            return
        items = _world_intel_snapshot_items(full_log)
        injected_ids = set(items)
        adoption_modes = _world_intel_adoption_modes(full_log)
        adopted_ids = {
            thread_id for thread_id, usage_mode in adoption_modes.items()
            if usage_mode != 'ignored'
        }
        progressed_ids = {
            _strict_ai_positive_integer(
                fact.get('source_world_story_thread_id')
            )
            for fact in (world_fact_updates or [])
            if isinstance(fact, dict)
            and _strict_ai_text(fact.get('change_kind')).lower()
            in {'advance', 'close', 'resolve', 'successor'}
        }
        due_after = max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_CLOSURE_DUE_AFTER_RUNS'), 6,
        ))
        force_dormant_after = max(due_after + 1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_CLOSURE_FORCE_DORMANT_AFTER_RUNS'), 12,
        ))
        expedition_locus_state, expedition_locus_scope_key = (
            resolve_expedition_locus_scope_from_full_log(
                full_log,
                expected_target_dungeon=target_dungeon,
                expected_expedition_log_id=int(log.id),
            )
        )
        old_clock_thread_ids = {
            int(row_id)
            for row_id, in (
                AdventurerS1WorldStoryThread.query
                .filter(
                    _world_story_head_clause(),
                    or_(
                        AdventurerS1WorldStoryThread.campaign_role.is_(None),
                        AdventurerS1WorldStoryThread.campaign_role
                        != WORLD_CAMPAIGN_SPINE_ROLE,
                    ),
                    AdventurerS1WorldStoryThread.base_dungeon.in_((
                        str(target_dungeon or '').strip(),
                        WORLD_INTEL_ALL_DUNGEONS_SCOPE,
                    )),
                    AdventurerS1WorldStoryThread.status.in_((
                        'seeded', 'active', 'escalated', 'closing', 'dormant',
                    )),
                )
                .with_entities(AdventurerS1WorldStoryThread.id)
                .all()
            )
        }
        presence_enabled = bool(current_app.config.get(
            'ADVENTURER_S1_FACTION_PRESENCE_ENABLED', True,
        ))
        linked_presence_thread_ids = (
            {
                int(thread_id)
                for thread_id, in (
                    AdventurerS1FactionProfileThread.query
                    .with_entities(
                        AdventurerS1FactionProfileThread.world_story_thread_id,
                    )
                    .distinct()
                    .all()
                )
            }
            if presence_enabled else set()
        )
        presence_candidate_rows = (
            AdventurerS1WorldStoryThread.query
            .filter(
                AdventurerS1WorldStoryThread.id.in_(
                    sorted(linked_presence_thread_ids),
                ),
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.status.in_((
                    'seeded', 'active', 'escalated', 'closing', 'dormant',
                    'resolved',
                )),
            )
            .all()
            if linked_presence_thread_ids else []
        )
        presence_clock_thread_ids = {
            int(row.id)
            for row in presence_candidate_rows
            if _world_story_thread_has_exact_dungeon_scope(row, target_dungeon)
            and not (
                expedition_locus_state == 'unresolved'
                and extract_location_scope_keys(row.scope_keys)
            )
            and not (
                expedition_locus_state == 'valid'
                and not story_scope_matches_expedition_locus(
                    row.scope_keys,
                    expedition_locus_scope_key,
                )
            )
        }
        clock_thread_ids = sorted(
            old_clock_thread_ids | presence_clock_thread_ids
        )
        from db_repository.s1_encounter_preparation_repository import encounter_pool_thread_clause
        pool_thread_ids = {int(row_id) for row_id, in AdventurerS1WorldStoryThread.query.filter(
            AdventurerS1WorldStoryThread.id.in_(clock_thread_ids),
            encounter_pool_thread_clause(),
        ).with_entities(AdventurerS1WorldStoryThread.id).all()} if clock_thread_ids else set()
        rows = (
            AdventurerS1WorldStoryThread.query
            .filter(AdventurerS1WorldStoryThread.id.in_(clock_thread_ids))
            .order_by(AdventurerS1WorldStoryThread.id.asc())
            .populate_existing()
            .with_for_update()
            .all()
            if clock_thread_ids else []
        )
        locked_rows_by_id = {int(row.id): row for row in rows}
        presence_clock_thread_ids = {
            thread_id
            for thread_id in presence_clock_thread_ids
            for row in [locked_rows_by_id.get(thread_id)]
            if row is not None
            and row.is_chain_head in (True, None)
            and row.status in {
                'seeded', 'active', 'escalated', 'closing', 'dormant',
                'resolved',
            }
            and _world_story_thread_has_exact_dungeon_scope(
                row, target_dungeon,
            )
            and not (
                expedition_locus_state == 'unresolved'
                and extract_location_scope_keys(row.scope_keys)
            )
            and not (
                expedition_locus_state == 'valid'
                and not story_scope_matches_expedition_locus(
                    row.scope_keys,
                    expedition_locus_scope_key,
                )
            )
        }
        old_clock_thread_ids = {
            thread_id
            for thread_id in old_clock_thread_ids
            for row in [locked_rows_by_id.get(thread_id)]
            if row is not None
            and row.is_chain_head in (True, None)
            and row.status in {
                'seeded', 'active', 'escalated', 'closing', 'dormant',
            }
            and row.base_dungeon in {
                str(target_dungeon or '').strip(),
                WORLD_INTEL_ALL_DUNGEONS_SCOPE,
            }
        }
        current_thread_revisions: dict[int, int] = {}
        from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository

        unfinished_campaign_roots = S1CampaignDispatchRepository.unfinished_roots({
            int(row.campaign_root_id) for row in rows
            if int(row.id) in old_clock_thread_ids
            and row.campaign_role in WORLD_CAMPAIGN_FRONT_ROLES
            and row.campaign_root_id
        })
        for row in rows:
            thread_id = int(row.id)
            if thread_id in presence_clock_thread_ids:
                current_thread_revisions[thread_id] = max(
                    1, int(row.revision or 1),
                )
            if thread_id not in old_clock_thread_ids:
                # presence-only（包括 resolved）不触碰既有世界线注入/收束时钟。
                continue
            scope_keys = {
                str(value).strip() for value in (row.scope_keys or [])
                if str(value).strip()
            }
            is_global = (
                WORLD_INTEL_ALL_DUNGEONS_SCOPE in scope_keys
                or _effective_world_story_base_dungeon(row) == WORLD_INTEL_ALL_DUNGEONS_SCOPE
            )
            local_scope_keys = extract_location_scope_keys(row.scope_keys)
            if (
                expedition_locus_state == 'unresolved'
                and local_scope_keys
            ):
                # 字段存在但非法不等于旧日志；无法确认局部域时不扩大局部线时钟。
                continue
            if (
                expedition_locus_state == 'valid'
                and not story_scope_matches_expedition_locus(
                    row.scope_keys,
                    expedition_locus_scope_key,
                )
            ):
                # 其它局部域没有获得本次出场机会，不累计漏注入或收束债务。
                continue
            # 全地牢条目只把真实注入本次快照的探险计入时钟，避免其它地牢的每次
            # 结算都替它累积漏出场和收束债务。
            if is_global and thread_id not in injected_ids:
                continue
            if thread_id in pool_thread_ids and thread_id not in injected_ids:
                # An archived living opponent does not expire because other
                # parties drew a different encounter. Real maintenance can still
                # change its state from actual settled facts.
                continue
            if thread_id in injected_ids:
                row.injection_count = int(row.injection_count or 0) + 1
                row.missed_injection_runs = 0
                row.last_injected_log_id = int(log.id)
            else:
                row.missed_injection_runs = int(row.missed_injection_runs or 0) + 1
            if thread_id in pool_thread_ids:
                row.closure_debt = 0
                if thread_id in progressed_ids:
                    row.last_progress_log_id = int(log.id)
            elif thread_id in progressed_ids:
                row.closure_debt = 0
                row.last_progress_log_id = int(log.id)
            elif (row.campaign_role in WORLD_CAMPAIGN_FRONT_ROLES
                  and row.campaign_root_id in unfinished_campaign_roots):
                # Ordinary runs must not retire current or future campaign fronts.
                # Preserve real world outcomes and existing dormant/resolved states;
                # this only disables the automatic closure clock while unfinished.
                row.closure_debt = 0
            elif row.status == 'dormant':
                # dormant 已是稳定余波；背景资格由本场精确地点/主体关联决定。
                row.closure_debt = 0
            elif is_global and int(row.id) not in adopted_ids:
                # 仅进入候选上下文但被客观大纲明确忽略，不算一次剧情推进机会。
                continue
            else:
                row.closure_debt = int(row.closure_debt or 0) + 1
                if row.closure_debt >= force_dormant_after:
                    row.status = 'dormant'
                    row.closure_debt = 0
                    row.next_reveal_hint = (
                        row.next_reveal_hint
                        or '该阶段已转为稳定余波；仅在新事实自然建立明确因果时再唤醒。'
                    )
                elif row.closure_debt >= due_after and row.status != 'dormant':
                    row.status = 'closing'

        presence_targets = _faction_presence_targets(full_log)
        valid_receipt_pairs = _valid_faction_presence_receipt_pairs(full_log)
        if presence_clock_thread_ids:
            profile_ids = sorted({
                int(profile_id)
                for profile_id, in (
                    AdventurerS1FactionProfileThread.query
                    .filter(
                        AdventurerS1FactionProfileThread.world_story_thread_id.in_(
                            sorted(presence_clock_thread_ids),
                        ),
                    )
                    .with_entities(
                        AdventurerS1FactionProfileThread.faction_profile_id,
                    )
                    .distinct()
                    .all()
                )
            })
            # 与势力档案维护写路径保持同一锁序：world_thread -> profile -> link。
            # profile 必须在加锁后重读，旧版本 Observer 回执不能并发清除新版本债务。
            profiles = (
                AdventurerS1FactionProfile.query
                .filter(AdventurerS1FactionProfile.id.in_(profile_ids))
                .order_by(AdventurerS1FactionProfile.id.asc())
                .with_for_update()
                .populate_existing()
                .all()
                if profile_ids else []
            )
            profile_by_id = {int(profile.id): profile for profile in profiles}
            links = (
                AdventurerS1FactionProfileThread.query
                .filter(
                    AdventurerS1FactionProfileThread.world_story_thread_id.in_(
                        sorted(presence_clock_thread_ids),
                    ),
                )
                .order_by(
                    AdventurerS1FactionProfileThread.faction_profile_id.asc(),
                    AdventurerS1FactionProfileThread.world_story_thread_id.asc(),
                )
                .populate_existing()
                .with_for_update()
                .all()
            )
            retry_cooldown_runs = max(0, _coerce_int(
                current_app.config.get(
                    'ADVENTURER_S1_FACTION_PRESENCE_ATTEMPT_COOLDOWN_RUNS',
                ),
                2,
            ))
            for link in links:
                profile_id = int(link.faction_profile_id)
                thread_id = int(link.world_story_thread_id)
                profile = profile_by_id.get(profile_id)
                if not profile or profile.status not in {
                    'rumored', 'active', 'dormant',
                }:
                    continue
                pair = (profile_id, thread_id)
                target = presence_targets.get(pair)
                current_revisions_match = bool(
                    target
                    and int(profile.revision or 1) == target['profile_revision']
                    and current_thread_revisions.get(thread_id)
                    == target['source_revision']
                )
                presence_confirmed = (
                    pair in valid_receipt_pairs
                    and current_revisions_match
                )
                if presence_confirmed:
                    link.presence_debt = 0
                    link.presence_cooldown_runs = 0
                    link.last_presence_log_id = int(log.id)
                    continue
                link.presence_debt = int(link.presence_debt or 0) + 1
                if target:
                    link.presence_cooldown_runs = retry_cooldown_runs
                elif int(link.presence_cooldown_runs or 0) > 0:
                    link.presence_cooldown_runs = max(
                        0, int(link.presence_cooldown_runs or 0) - 1,
                    )

        full_log['_world_story_completion_clock'] = {
            'recorded_log_id': int(log.id),
        }
        log.full_log = full_log

    @staticmethod
    def submit_maintenance_task() -> bool:
        from services.async_task_service import AsyncTaskService

        return AsyncTaskService.submit_task('ai_s1_world_outline_maintenance', {})

    @staticmethod
    def build_dm_handoff_prompt_block(
        target_dungeon: str | None,
        *,
        party_id: int | None = None,
        expedition_log_id: int | None = None,
    ) -> str:
        from .adventurer_s1_config_service import is_s1_independent_dungeon
        if is_s1_independent_dungeon(target_dungeon):
            return ''
        dungeon = str(target_dungeon or '').strip()
        if not dungeon:
            return ''
        state = AdventurerS1DmHandoffState.query.filter_by(target_dungeon=dungeon).first()
        note = _soft_text(getattr(state, 'note', '') or '')
        if not note:
            return ''
        if expedition_log_id:
            from plugins.adventurer_s1.services.s1_narrative_lease_service import (
                acquire_narrative_lease,
            )
            with db_manager.transaction():
                ok = acquire_narrative_lease(
                    target_dungeon=dungeon,
                    plot_signature=f'dm_handoff:{int(state.id)}',
                    lease_type='dm_handoff',
                    party_id=party_id,
                    expedition_log_id=expedition_log_id,
                    dm_handoff_state_id=int(state.id),
                    title=f'{dungeon} 接力便签',
                )
            if not ok:
                return ''
        return (
            '【内部连续性备注（仅供客观大纲筛选）】\n'
            '这是可能与本场无关的自由备注，不是任务清单。只采纳与本次目标、成员身份和现有状态一致的内容；'
            '不得转移个人伏笔、关系或物品，不得补写未提供的后续线索。玩家可见内容只能呈现为世界内事实。\n'
            f'{note}'
        )

    @staticmethod
    def get_motif_cooldown_terms() -> list[str]:
        """后端不再根据自然语言词项执行母题降权。"""
        return []

    @staticmethod
    def build_world_story_threads_prompt_block(
        target_dungeon: str | None,
        *,
        party_id: int | None = None,
        expedition_log_id: int | None = None,
        context_text: str | None = None,
        context_keywords=None,
    ) -> str:
        """Build a tiny soft-injection block for bounded public world intel."""
        snapshot = S1WorldOutlineService.build_world_story_threads_injection_snapshot(
            target_dungeon,
            party_id=party_id,
            expedition_log_id=expedition_log_id,
            context_text=context_text,
            context_keywords=context_keywords,
        )
        return snapshot.get('prompt_block') or ''

    @staticmethod
    def _render_read_only_world_story_entry(item: dict | None) -> str:
        """Project frozen background facts without turning a future hook into a task."""
        if (
            not isinstance(item, dict)
            or item.get('access_mode') != 'read_only'
            or item.get('injection_role') not in ('historical_echo', 'shared_state')
            or any(item.get(key) for key in (
                'required_faction_interaction', 'required_campaign_manifestation',
                'required_faction_presence', 'campaign_dispatch_context', 'campaign_context',
            ))
            or not all(key in item for key in ('id', 'revision', 'title', 'status', 'summary'))
        ):
            # Older snapshots without structured facts retain their original text.
            return ''
        label = (
            '历史回响·只读'
            if item['injection_role'] == 'historical_echo'
            else '公共当前态·只读'
        )
        parts = [
            f"- [{label}] [world_story_thread_id:{item['id']}] "
            f"[source_revision:{item['revision']}] "
            f"标题:{item['title']}；状态:{item['status']}",
        ]
        for key, field_label in (
            ('summary', '已成立公共事实'),
            ('power_profile', '连续性边界'),
        ):
            value = str(item.get(key) or '').strip()
            if value:
                parts.append(f'{field_label}:{value}')
        if extract_location_scope_keys(item.get('scope_keys')):
            parts.append('[作用域:本次局部探索域]')
        predecessor = item.get('predecessor_context')
        if isinstance(predecessor, dict) and predecessor.get('id'):
            parts.append(
                f"阶段来源:[world_story_thread_id:{predecessor['id']}] "
                f"{predecessor.get('title') or ''}: {predecessor.get('summary') or ''}"
            )
        hint = str(item.get('next_reveal_hint') or '').strip()
        if hint:
            # Legacy hints can contain the only copy of a hard state boundary.
            # Preserve the frozen text, but do not promote it to an active objective.
            parts.append(f'旧后续记录（仅供理解历史条件，不构成本场任务或推进授权）:{hint}')
        return '；'.join(parts)

    @staticmethod
    def _render_world_story_prompt_entries(
        entries: list[dict] | None,
        *,
        adopted: bool = False,
        frozen_items: list[dict] | None = None,
    ) -> str:
        """Render structured prompt entries so downstream stages can remove ignored intel by id."""
        facts_by_id = {
            _coerce_int(item.get('id'), 0): item
            for item in frozen_items or []
            if isinstance(item, dict) and _coerce_int(item.get('id'), 0) > 0
        }
        rendered_lines = []
        background_lines = []
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            prompt_line = str((
                item.get('prompt_line')
                if adopted
                else item.get('selection_prompt_line') or item.get('prompt_line')
            ) or '').strip()
            facts = facts_by_id.get(_coerce_int(item.get('world_story_thread_id'), 0))
            if not adopted and isinstance(facts, dict) and (
                facts.get('access_mode') == 'mutable'
                or facts.get('required_faction_presence') is True
            ):
                # Use the same frozen projection as adoption, including a
                # campaign's revealed view. A title/hook cannot replace current facts.
                current_lines = [
                    f"{label}:{facts[key]}"
                    for key, label in (('summary', '已成立当前事实'), ('power_profile', '连续性边界'))
                    if facts.get(key)
                ]
                if current_lines:
                    prompt_line += '\n  ' + '；'.join(current_lines)
            if not adopted or item.get('usage_mode') == 'background_reference':
                background_line = S1WorldOutlineService._render_read_only_world_story_entry(
                    facts_by_id.get(_coerce_int(item.get('world_story_thread_id'), 0)),
                )
                if background_line:
                    prompt_line = background_line
            if not prompt_line:
                continue
            if adopted:
                label = (
                    '已直接采纳'
                    if item.get('usage_mode') == 'exclusive_direct_use'
                    else '背景引用'
                )
                if prompt_line.startswith('- [') and ']' in prompt_line:
                    prompt_line = f'- [{label}]' + prompt_line.split(']', 1)[1]
                else:
                    prompt_line = f'- [{label}] {prompt_line}'
            read_only_background = (
                isinstance(facts, dict)
                and facts.get('access_mode') == 'read_only'
                and facts.get('injection_role') in ('historical_echo', 'shared_state')
                and not any(facts.get(key) for key in (
                    'required_faction_interaction', 'required_campaign_manifestation',
                    'required_faction_presence', 'campaign_dispatch_context', 'campaign_context',
                ))
            )
            if not adopted and read_only_background:
                background_lines.append(prompt_line)
            else:
                rendered_lines.append(prompt_line)
        body = '\n'.join(rendered_lines)
        if not body and not background_lines:
            return ''
        world_story_block = (
            '【本场可参与的公共世界线】\n'
            '以下条目提供本场可申请的推进或势力参与权限，按准确 ID/版本逐项裁决 world_intel_usages；强制议事厅行动保留冻结任务与揭示内容。前驱全文与势力分面在采纳后按权限恢复。\n'
            '从本次目标与模式需要的冲突选择相容的当前行动；这些行动足以承担该冲突时承接，不足时保留其原状，在世界事实允许的空白中另建本场局势。条目的历史事件与后续建议只说明来源和条件，已有事件的最终结果继续成立。采纳、阶段收束与新内容边界服从 system prompt。\n'
            '条目标有“阶段来源”时，按准确前驱引用理解已有阶段；可推进线的已成立现状与仍在持续的行动可形成本场机会，后续线索需与这些事实相容。“本次可推进公共线”只声明可申请 exclusive_direct_use，强制项与只读项分别服从下方自身合同。\n'
            '“本次必须兑现的酒馆势力交互”是后端依据精确 faction 类型、abyss_tavern 势力节点键与既有推进债务选出的可推进线；必须选择 exclusive_direct_use，让该外部势力对深渊酒馆冒险者生态已经成立的行动、机会或阻力沿本次地牢/委托的既有因果进入共同主线并形成可核对阶段结果，不得另开互不相干的平行任务或替换本次目标。不得预选队伍回应、胜负或代价，也不得降为传闻、预兆或无关彩蛋。\n'
            '“本场势力创作锚点·软性”是服务端冻结的低频势力创作授权；允许该势力沿本场既有主线实际参与并补充不与权威事实冲突的新设定，具体边界服从本阶段 system prompt。同一项若还标为强制酒馆势力交互，强合同优先。\n'
            '长期世界角色服从其独立的结构化权威当前态与 world_actor_usages；世界线采纳不自动授权角色本体。\n'
            f'{body}'
        )
        if background_lines:
            background_block = (
                '【只读世界事实边界】\n'
                '以下资料用于核对本场创作与已有世界是否相容。既成结果、持续状态和作用边界照实继承；旧记录中的计划、研究课题与待验证条件仍停留在原阶段，不构成本场正在发生的事件。'
                '本块不授权主体本体出场、唯一物品接触或公共状态推进。'
                '实际引用时回传 background_reference，未引用时回传 ignored；人物知情与本体参与沿各自权限成立。\n'
                + '\n'.join(background_lines)
            )
            world_story_block = '\n\n'.join(filter(None, (
                world_story_block if body else '', background_block,
            )))
        if adopted:
            world_story_block = (
                '【本场已采纳世界连续性】\n'
                '以下内容只对客观大纲已经保留的条目恢复；只读背景保留事实与连续性边界，未采纳内容已物理移除。'
                '恢复正文提供此前已经成立的历史、主体身份、能力作用、限制与当前阶段；草案安排的本场因果角色与后续阶段仍是候选，由正文在当前模式与这些硬事实内取舍。'
                '主剧情沿同一主体和已成立事实继续；已有行动与部署构成本场起点，后续行动由其目标、当时感知、已知经验与实际条件决定，经验应用按【深渊酒馆·既有知识的应用】执行。选择阶段未看到的旧细节以本块正文和准确长期角色的结构化权威当前态为准。'
                'exclusive_direct_use 必须实际推进、收束、完结或形成由本场结果支持的真正后续，并回传对应 world_fact_updates；background_reference 只能解释已有背景与后果。'
                '每条的直接或背景权限已经冻结，使用边界以本块末尾的结构化采纳授权为准。\n'
                f'{body}'
            )
        faction_profile_blocks: list[str] = []
        seen_profile_ids: set[int] = set()
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            for profile_entry in item.get('faction_profile_entries') or []:
                if not isinstance(profile_entry, dict):
                    continue
                profile_id = _coerce_int(
                    profile_entry.get('faction_profile_id'), 0,
                )
                prompt_block = str((
                    profile_entry.get('prompt_block')
                    if adopted
                    else (
                        profile_entry.get('selection_prompt_block')
                        or profile_entry.get('prompt_block')
                    )
                ) or '').strip()
                if profile_id <= 0 or profile_id in seen_profile_ids or not prompt_block:
                    continue
                seen_profile_ids.add(profile_id)
                faction_profile_blocks.append(prompt_block)
        if not faction_profile_blocks:
            return world_story_block
        if adopted:
            return (
                f'{world_story_block}\n\n'
                '【本场已采纳势力当前态】\n'
                '档案按势力自身归属读取：当前议程是该势力的打算，当前态是其已经成立的事实；直接参与权限仍由相应世界线的 usage_mode 决定。\n'
                + '\n\n'.join(faction_profile_blocks)
            )
        return (
            f'{world_story_block}\n\n'
            '【相关势力档案（结构化当前态）】\n'
            '档案只补充已经入选世界线中的势力连续性，不增加相关性；除精确标为“本场势力创作锚点·软性”的一项外，不要求本场提及，'
            '也不授权势力出场、行动、知情、触达、提高风险或推进世界线。创作锚点是例外：它允许该势力的普通成员、代表与组织行动沿既有主线实际参与，'
            '并补充尚未定义且与本场因果相交的稳定设定；不能推翻档案已有事实，也不能冒用另行注入的唯一角色或唯一物品。'
            '公开当前态可作为背景事实；未证实传闻必须保持不确定，不能当成真相。'
            '未注入的 restricted 分面不得由普通剧情自行得知。'
            '方括号中的 faction_profile_id、faction_key 与 profile_revision 只供审计，'
            '玩家可见文本只使用世界内称谓。\n'
            + '\n\n'.join(faction_profile_blocks)
        )

    @staticmethod
    def _pick_due_faction_presence_target(
        dungeon: str,
        *,
        expedition_locus_scope_key: str | None,
        already_selected_thread_ids: set[int],
    ) -> dict | None:
        """Pick at most one exact profile/thread debt as a read-only presence slot."""
        from flask import current_app

        if not current_app.config.get(
            'ADVENTURER_S1_FACTION_PRESENCE_ENABLED', True,
        ):
            return None
        due_after = max(1, _coerce_int(
            current_app.config.get(
                'ADVENTURER_S1_FACTION_PRESENCE_DUE_AFTER_RUNS',
            ),
            6,
        ))
        candidates = (
            db_manager.query(
                AdventurerS1FactionProfileThread,
                AdventurerS1FactionProfile,
                AdventurerS1WorldStoryThread,
            )
            .join(
                AdventurerS1FactionProfile,
                AdventurerS1FactionProfile.id
                == AdventurerS1FactionProfileThread.faction_profile_id,
            )
            .join(
                AdventurerS1WorldStoryThread,
                AdventurerS1WorldStoryThread.id
                == AdventurerS1FactionProfileThread.world_story_thread_id,
            )
            .filter(
                _world_story_head_clause(),
                AdventurerS1FactionProfile.status.in_((
                    'rumored', 'active', 'dormant',
                )),
                AdventurerS1WorldStoryThread.status.in_((
                    'seeded', 'active', 'escalated', 'closing', 'dormant',
                    'resolved',
                )),
                AdventurerS1FactionProfileThread.presence_debt >= due_after,
                AdventurerS1FactionProfileThread.presence_cooldown_runs <= 0,
            )
            .all()
        )
        from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
        allowed_thread_ids = {
            row.id for row in S1CampaignDispatchService.filter_fronts([t for _, _, t in candidates])
        }
        eligible: list[tuple] = []
        for link, profile, thread in candidates:
            if thread.id not in allowed_thread_ids:
                continue
            if not _world_story_thread_has_exact_dungeon_scope(thread, dungeon):
                continue
            if not story_scope_matches_expedition_locus(
                thread.scope_keys,
                expedition_locus_scope_key,
            ):
                continue
            eligible.append((link, profile, thread))
        eligible.sort(key=lambda row: (
            0 if int(row[2].id) in already_selected_thread_ids else 1,
            -int(row[0].presence_debt or 0),
            -int(row[1].importance or 50),
            int(row[1].id),
            int(row[2].id),
        ))
        if not eligible:
            return None
        link, profile, thread = eligible[0]
        return {
            'link_id': int(link.id),
            'faction_profile_id': int(profile.id),
            'profile_revision': max(1, int(profile.revision or 1)),
            'world_story_thread_id': int(thread.id),
            'source_revision': max(1, int(thread.revision or 1)),
            'presence_debt': int(link.presence_debt or 0),
            'thread': thread,
        }

    @staticmethod
    def build_world_story_threads_injection_snapshot(
        target_dungeon: str | None,
        *,
        party_id: int | None = None,
        expedition_log_id: int | None = None,
        context_text: str | None = None,
        context_keywords=None,
        authority_reference_texts: list[str] | None = None,
        expedition_locus_scope_key: str | None = None,
    ) -> dict:
        """Return the exact world-intel prompt block and debug metadata for one AI call."""
        from .adventurer_s1_config_service import is_s1_independent_dungeon
        if is_s1_independent_dungeon(target_dungeon):
            return {
                'target_dungeon': target_dungeon, 'items': [],
                'world_actor_states': [], 'faction_keys': [], 'prompt_block': '',
            }
        from flask import current_app

        dungeon = str(target_dungeon or '').strip()
        search_text, anchor_terms = _build_world_intel_context(dungeon, context_text, context_keywords)
        if not search_text:
            return {
                'target_dungeon': dungeon,
                'context_text': '',
                'context_keywords': [],
                'context_char_count': 0,
                'items': [],
                'prompt_block': '',
                'diagnostic': 'empty_context',
            }
        from plugins.adventurer_s1.services.s1_narrative_lease_service import (
            get_world_actor_ids_leased_by_expedition,
            get_world_actor_ids_leased_by_other_expeditions,
        )
        countermeasure_due_actor_ids: list[int] = []
        countermeasure_available_actor_ids: list[int] = []
        initially_leased_actor_ids: set[int] = set()
        reseeded_world_actor_thread_id = None
        if expedition_log_id and not campaign_world(dungeon):
            from plugins.adventurer_s1.services.s1_countermeasure_knowledge_service import (
                due_countermeasure_actor_ids,
            )
            countermeasure_due_actor_ids = due_countermeasure_actor_ids()
            initially_leased_actor_ids = (
                get_world_actor_ids_leased_by_other_expeditions(
                    expedition_log_id,
                )
            )
            countermeasure_available_actor_ids = [
                actor_id for actor_id in countermeasure_due_actor_ids
                if actor_id not in initially_leased_actor_ids
            ]
            # 真实发车是写路径：当维护任务已经把所有长期角色线冷却为 dormant，
            # 在本次选择前恢复一条已有直接关联和能力画像的角色线。管理员预览
            # 没有 expedition_log_id，不会触发任何状态变化。
            with db_manager.transaction():
                reseeded_world_actor_thread_id = (
                    S1WorldOutlineService._ensure_progressable_world_actor_thread(
                        dungeon=dungeon,
                        expedition_locus_scope_key=expedition_locus_scope_key,
                        preferred_actor_ids=countermeasure_available_actor_ids,
                        excluded_actor_ids=initially_leased_actor_ids,
                    )
                )
        mutable_limit = max(0, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_PROMPT_LIMIT', 1),
            1,
        ))
        shared_limit = max(0, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_SHARED_STATE_LIMIT', 3), 3,
        ))
        ranked = S1WorldOutlineService._rank_world_story_threads_for_context(
            dungeon,
            context_text=search_text,
            context_keywords=anchor_terms,
            expedition_log_id=expedition_log_id,
        )
        from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
        dispatch_context = S1CampaignDispatchService.context(expedition_log_id)
        from plugins.adventurer_s1.services.s1_campaign_revelation_contract import project_revealed_thread
        revealed_thread_id = (dispatch_context.get('thread_id')
                              if 'revelation_context' in dispatch_context else None)
        locus_filtered_thread_ids = {
            int(row.id)
            for _score, row in ranked
            if _world_story_thread_has_exact_dungeon_scope(row, dungeon)
            and not story_scope_matches_expedition_locus(
                row.scope_keys,
                expedition_locus_scope_key,
            )
        }
        eligible_ranked = [
            (score, row)
            for score, row in ranked
            if int(row.id) not in locus_filtered_thread_ids
        ]
        exact_dungeon_hit_count = sum(1 for score, _row in ranked if score > 0)
        global_only_filtered_count = sum(
            1 for _score, row in ranked
            if (
                WORLD_INTEL_ALL_DUNGEONS_SCOPE in {
                    str(value).strip() for value in (row.scope_keys or [])
                    if str(value or '').strip()
                }
                or _effective_world_story_base_dungeon(row) == WORLD_INTEL_ALL_DUNGEONS_SCOPE
            )
            and not _world_story_thread_has_exact_dungeon_scope(row, dungeon)
        )
        scoped_rows = [
            row for _score, row in eligible_ranked
            if _world_story_thread_has_exact_dungeon_scope(row, dungeon)
        ]
        scoped_thread_ids = [int(row.id) for row in scoped_rows]
        actor_ids_by_thread: dict[int, set[int]] = {}
        direct_lease_actor_ids_by_thread: dict[int, set[int]] = {}
        if scoped_thread_ids:
            actor_links = AdventurerS1WorldStoryThreadActor.query.filter(
                AdventurerS1WorldStoryThreadActor.world_story_thread_id.in_(scoped_thread_ids),
            ).all()
            for link in actor_links:
                actor_ids_by_thread.setdefault(int(link.world_story_thread_id), set()).add(int(link.actor_id))
                if link.involvement == 'direct' and bool(link.lease_required):
                    direct_lease_actor_ids_by_thread.setdefault(
                        int(link.world_story_thread_id), set(),
                    ).add(int(link.actor_id))
        countermeasure_preferred_thread_ids: list[int] = []
        for actor_id in countermeasure_available_actor_ids:
            for row in scoped_rows:
                thread_id = int(row.id)
                if (
                    actor_id in direct_lease_actor_ids_by_thread.get(
                        thread_id, set(),
                    )
                    and thread_id not in countermeasure_preferred_thread_ids
                ):
                    countermeasure_preferred_thread_ids.append(thread_id)
        if (
            reseeded_world_actor_thread_id
            and reseeded_world_actor_thread_id
            not in countermeasure_preferred_thread_ids
        ):
            countermeasure_preferred_thread_ids.append(
                int(reseeded_world_actor_thread_id),
            )
        leased_elsewhere_actor_ids = get_world_actor_ids_leased_by_other_expeditions(
            expedition_log_id,
        )
        leased_elsewhere_thread_ids = {
            int(row.id) for row in scoped_rows
            if actor_ids_by_thread.get(int(row.id), set()) & leased_elsewhere_actor_ids
        }
        mutable_picked = S1WorldOutlineService._pick_world_story_threads_for_prompt(
            dungeon,
            limit=mutable_limit,
            excluded_ids=(
                leased_elsewhere_thread_ids | locus_filtered_thread_ids
            ),
            preferred_ids=(
                countermeasure_preferred_thread_ids or None
            ),
            party_id=party_id,
            expedition_log_id=expedition_log_id,
            context_text=search_text,
            context_keywords=anchor_terms,
        )
        # A competing request may acquire an actor lease while this request is
        # blocked inside the mutable picker. Re-read after acquisition attempts
        # so that a losing request cannot retain the same actor as shared/closure
        # material in the prompt assembled below.
        leased_elsewhere_actor_ids = get_world_actor_ids_leased_by_other_expeditions(
            expedition_log_id,
        )
        leased_elsewhere_thread_ids = {
            int(row.id) for row in scoped_rows
            if actor_ids_by_thread.get(int(row.id), set()) & leased_elsewhere_actor_ids
        }
        owned_actor_ids = get_world_actor_ids_leased_by_expedition(expedition_log_id)
        if expedition_log_id:
            active_actor_ids = {
                int(actor_id)
                for actor_id, in AdventurerS1WorldActor.query.filter_by(
                    status='active',
                ).with_entities(AdventurerS1WorldActor.id).all()
            }
            unavailable_actor_ids = active_actor_ids - owned_actor_ids
        else:
            # Admin preview/no-log callers cannot own a runtime lease; keep their
            # historical behavior while still hiding actors leased by live runs.
            unavailable_actor_ids = set(leased_elsewhere_actor_ids)
        # A selected line owns only its direct actors. An indirect historical
        # relation does not require a second actor lease or authorize that actor
        # to appear. Keep all actors leased elsewhere excluded, and keep the
        # stricter active-actor boundary for shared/closure and indirect-only lines.
        owned_direct_thread_ids = {
            int(row.id) for row in mutable_picked
            if direct_lease_actor_ids_by_thread.get(int(row.id), set())
            and direct_lease_actor_ids_by_thread[int(row.id)] <= owned_actor_ids
        }
        unavailable_actor_thread_ids = set(leased_elsewhere_thread_ids)
        for row in scoped_rows:
            thread_id = int(row.id)
            required_actor_ids = (
                direct_lease_actor_ids_by_thread[thread_id]
                if thread_id in owned_direct_thread_ids
                else actor_ids_by_thread.get(thread_id, set())
            )
            if required_actor_ids & unavailable_actor_ids:
                unavailable_actor_thread_ids.add(thread_id)
        filtered_mutable_thread_ids = {
            int(row.id) for row in mutable_picked
            if int(row.id) in unavailable_actor_thread_ids
        }
        if filtered_mutable_thread_ids:
            mutable_picked = [
                row for row in mutable_picked
                if int(row.id) not in filtered_mutable_thread_ids
            ]
            if expedition_log_id:
                from plugins.adventurer_s1.services.s1_narrative_lease_service import (
                    release_unadopted_world_story_leases,
                )
                with db_manager.transaction():
                    release_unadopted_world_story_leases(
                        expedition_log_id,
                        adopted_world_story_thread_ids={
                            int(row.id) for row in mutable_picked
                        },
                    )
        selected_direct_actor_ids = {
            actor_id for row in mutable_picked
            for actor_id in direct_lease_actor_ids_by_thread.get(int(row.id), set())
        }
        dormant_context_filtered_ids = {
            int(row.id) for _score, row in eligible_ranked
            if row.status == 'dormant'
            and not S1WorldOutlineService._dormant_world_story_matches_current_context(
                row.scope_keys,
                expedition_locus_scope_key=expedition_locus_scope_key,
                linked_actor_ids=actor_ids_by_thread.get(int(row.id), set()),
                selected_actor_ids=selected_direct_actor_ids,
            )
        }
        # Apply eligibility before the bounded slice: unrelated history must not
        # occupy a slot and hide a lower-ranked, relevant current fact.
        shared_picked = [
            row for score, row in eligible_ranked
            if score > 0
            and int(row.id) not in unavailable_actor_thread_ids
            and int(row.id) not in dormant_context_filtered_ids
            and not _is_campaign_front(row)
        ][:shared_limit]
        closure_picked = S1WorldOutlineService._pick_world_story_threads_for_closure(
            dungeon,
            # 当前日志未持有的 active 角色不以收束/历史回响形式进入本次 prompt。
            excluded_ids=(
                unavailable_actor_thread_ids
                | locus_filtered_thread_ids
                | {int(row.id) for row in mutable_picked}
            ),
            party_id=party_id,
            expedition_log_id=expedition_log_id,
            context_text=search_text,
            context_keywords=anchor_terms,
        )
        normal_selected_thread_ids = {
            int(row.id)
            for row in (mutable_picked + closure_picked + shared_picked)
        }
        required_campaign_manifestation_ids = {
            int(row.id)
            for row in mutable_picked
            if _is_campaign_front(row)
            and not getattr(
                row,
                _REQUIRED_TAVERN_INTERACTION_TRANSIENT_ATTR,
                False,
            )
        }
        faction_presence_target = (
            S1WorldOutlineService._pick_due_faction_presence_target(
                dungeon,
                expedition_locus_scope_key=expedition_locus_scope_key,
                already_selected_thread_ids=normal_selected_thread_ids,
            )
            if not required_campaign_manifestation_ids
            else None
        )
        if (
            faction_presence_target
            and int(faction_presence_target['world_story_thread_id'])
            not in normal_selected_thread_ids
            and shared_limit <= 0
        ):
            # 配置关闭共享上下文容量时，不为 presence-only 额外扩张 prompt。
            faction_presence_target = None
        presence_picked: list[AdventurerS1WorldStoryThread] = []
        presence_only_ids: set[int] = set()
        if faction_presence_target:
            presence_row = faction_presence_target['thread']
            presence_thread_id = int(presence_row.id)
            presence_picked = [presence_row]
            if presence_thread_id not in normal_selected_thread_ids:
                presence_only_ids.add(presence_thread_id)
                # 势力痕迹占用一个既有共享上下文名额，避免额外扩大常规 prompt。
                if shared_picked:
                    shared_picked = shared_picked[:-1]
        hot_terms = set(S1WorldOutlineService.get_motif_cooldown_terms())
        items = []
        picked_by_priority = (
            mutable_picked + presence_picked + closure_picked + shared_picked
        )
        all_picked = []
        selected_thread_ids: set[int] = set()
        selected_actor_ids: set[int] = set()
        actor_deduplicated_thread_ids: list[int] = []
        for row in picked_by_priority:
            thread_id = int(row.id)
            if thread_id in selected_thread_ids:
                continue
            row_actor_ids = (
                set()
                if thread_id in presence_only_ids
                else actor_ids_by_thread.get(thread_id, set())
            )
            if row_actor_ids & selected_actor_ids:
                if thread_id not in actor_deduplicated_thread_ids:
                    actor_deduplicated_thread_ids.append(thread_id)
                continue
            all_picked.append(row)
            selected_thread_ids.add(thread_id)
            selected_actor_ids.update(row_actor_ids)
        selected_ids = {int(row.id) for row in all_picked}
        required_campaign_manifestation_ids.intersection_update(selected_ids)
        closure_ids = {int(row.id) for row in closure_picked} & selected_ids
        mutable_ids = {int(row.id) for row in mutable_picked} & selected_ids
        from plugins.adventurer_s1.services.s1_faction_profile_service import (
            build_prompt_entries as build_faction_profile_prompt_entries,
        )
        faction_profile_snapshot = build_faction_profile_prompt_entries(
            [int(row.id) for row in all_picked],
            priority_profile_ids=(
                {int(faction_presence_target['faction_profile_id'])}
                if faction_presence_target else None
            ),
        )
        faction_profile_entries_by_thread = (
            faction_profile_snapshot.get('entries_by_thread_id') or {}
        )
        campaign_context_by_thread: dict[int, dict] = {}
        campaign_rows = [row for row in all_picked
                         if _is_campaign_front(row) and row.id != revealed_thread_id]
        campaign_root_ids = {
            int(row.campaign_root_id)
            for row in campaign_rows
            if int(row.campaign_root_id or 0) > 0
        }
        if campaign_root_ids:
            campaign_records = AdventurerS1WorldStoryThread.query.filter(or_(
                AdventurerS1WorldStoryThread.id.in_(campaign_root_ids),
                AdventurerS1WorldStoryThread.campaign_root_id.in_(campaign_root_ids),
            )).all()
            campaign_root_by_id = {
                int(row.id): row
                for row in campaign_records
                if int(row.id) in campaign_root_ids
            }
            campaign_members_by_root: dict[int, list[AdventurerS1WorldStoryThread]] = {}
            for campaign_record in campaign_records:
                root_id = int(campaign_record.campaign_root_id or 0)
                if (
                    root_id <= 0
                    or campaign_record.campaign_role not in WORLD_CAMPAIGN_FRONT_ROLES
                    or campaign_record.is_chain_head is False
                ):
                    continue
                campaign_members_by_root.setdefault(root_id, []).append(campaign_record)
            for campaign_row in campaign_rows:
                root_id = int(campaign_row.campaign_root_id or 0)
                campaign_root = campaign_root_by_id.get(root_id)
                if not campaign_root:
                    continue
                related_fronts = sorted(
                    campaign_members_by_root.get(root_id, []),
                    key=lambda member: (
                        str(member.base_dungeon or ''),
                        str(member.campaign_role or ''),
                        int(member.id or 0),
                    ),
                )
                campaign_context_by_thread[int(campaign_row.id)] = {
                    'campaign_root_id': root_id,
                    'campaign_title': _clip_text(campaign_root.title, 120),
                    'campaign_summary': _clip_text(campaign_root.summary, 420),
                    'campaign_agenda': _clip_text(campaign_root.campaign_agenda, 320),
                    'front_agenda': _clip_text(campaign_row.campaign_agenda, 320),
                    'related_fronts': [
                        {
                            'world_story_thread_id': int(member.id),
                            'campaign_role': member.campaign_role or '',
                            'base_dungeon': member.base_dungeon or '',
                            'status': member.status or '',
                            'title': _clip_text(member.title, 120),
                            'summary': _clip_text(member.summary, 240),
                            'campaign_agenda': _clip_text(
                                member.campaign_agenda,
                                200,
                            ),
                        }
                        for member in related_fronts[:12]
                    ],
                }
        required_faction_interaction_candidates = [
            int(row.id)
            for row in mutable_picked
            if int(row.id) in selected_ids
            and getattr(
                row,
                _REQUIRED_TAVERN_INTERACTION_TRANSIENT_ATTR,
                False,
            ) is True
        ]
        required_faction_interaction_ids = set(
            required_faction_interaction_candidates[:1],
        )
        deferred_faction_interaction_ids = [
            int(row.id)
            for row in mutable_picked
            if int(row.id) in selected_ids
            and int(row.id) not in required_faction_interaction_ids
            and _is_due_tavern_faction_interaction(row)
        ]
        for row in all_picked:
            item = row.to_admin_dict()
            if row.id == revealed_thread_id:
                item = project_revealed_thread(item, dispatch_context)
            else:
                item.update(S1WorldOutlineService._explain_world_story_thread_match(
                    row,
                    dungeon,
                    search_text,
                    anchor_terms,
                    hot_terms,
                ))
            item['closure_candidate'] = int(row.id) in closure_ids
            item['required_faction_interaction'] = (
                int(row.id) in required_faction_interaction_ids
            )
            item['required_campaign_manifestation'] = (
                int(row.id) in required_campaign_manifestation_ids
            )
            if row.id == dispatch_context.get('thread_id'):
                item['campaign_dispatch_context'] = dict(dispatch_context)
            if int(row.id) in campaign_context_by_thread:
                item['campaign_context'] = dict(
                    campaign_context_by_thread[int(row.id)]
                )
            item['required_faction_presence'] = bool(
                faction_presence_target
                and int(row.id)
                == int(faction_presence_target['world_story_thread_id'])
            )
            if item['required_faction_presence']:
                item['faction_presence_target'] = {
                    key: faction_presence_target[key]
                    for key in (
                        'faction_profile_id', 'profile_revision',
                        'world_story_thread_id', 'source_revision',
                    )
                }
            if int(row.id) in mutable_ids:
                item['access_mode'] = 'mutable'
                item['injection_role'] = 'mutable_thread'
            elif int(row.id) in presence_only_ids:
                item['access_mode'] = 'read_only'
                item['injection_role'] = 'faction_presence'
            elif int(row.id) in closure_ids:
                item['access_mode'] = 'read_only'
                item['injection_role'] = 'historical_echo'
            else:
                item['access_mode'] = 'read_only'
                item['injection_role'] = 'shared_state'
            items.append(item)
        labels = {
            'mutable': '本次可推进公共线',
            'required_faction': '本次必须兑现的酒馆势力交互',
            'required_campaign': '本次必须兑现的共同战役前线',
            'faction_presence': '本场势力创作锚点·软性',
            'closure': '历史回响·只读',
            'shared': '公共当前态·只读',
        }
        prompt_entries = []
        predecessor_ids = {
            int(row.predecessor_id)
            for row in all_picked
            if int(row.predecessor_id or 0) > 0 and row.id != revealed_thread_id
        }
        predecessor_by_id = {
            int(row.id): row
            for row in (
                AdventurerS1WorldStoryThread.query
                .filter(AdventurerS1WorldStoryThread.id.in_(predecessor_ids),
                        S1CampaignWorldRepository.thread_scope(dungeon))
                .all()
                if predecessor_ids else []
            )
        }
        for row in all_picked:
            revealed_item = (project_revealed_thread(row.to_admin_dict(), dispatch_context)
                             if row.id == revealed_thread_id else None)
            role = (
                'required_faction'
                if int(row.id) in required_faction_interaction_ids
                else 'required_campaign'
                if int(row.id) in required_campaign_manifestation_ids
                else 'faction_presence'
                if (
                    faction_presence_target
                    and int(row.id)
                    == int(faction_presence_target['world_story_thread_id'])
                )
                else 'mutable'
                if int(row.id) in mutable_ids
                else 'closure'
                if int(row.id) in closure_ids
                else 'shared'
            )
            prompt_line = (
                f"- [{labels[role]}] [world_story_thread_id:{row.id}] "
                f"[source_revision:{int(row.revision or 1)}] "
                + (f"{revealed_item['title']}: {revealed_item['summary']}" if revealed_item else row.to_prompt_line())
            )
            selection_prompt_line = (
                f"- [{labels[role]}] [world_story_thread_id:{row.id}] "
                f"[source_revision:{int(row.revision or 1)}] "
                f"标题:{revealed_item['title'] if revealed_item else _clip_text(row.title, 120)}；状态:{row.status or ''}"
            )
            current_direction = '' if revealed_item else _clip_text(row.next_reveal_hint, 320)
            if current_direction:
                selection_prompt_line = (
                    f'{selection_prompt_line}；后续线索（须与当前事实相容）:{current_direction}'
                )
            if extract_location_scope_keys(row.scope_keys):
                prompt_line = f'{prompt_line}；[作用域:本次局部探索域]'
                selection_prompt_line = (
                    f'{selection_prompt_line}；[作用域:本次局部探索域]'
                )
            predecessor = (None if revealed_item else predecessor_by_id.get(int(row.predecessor_id or 0)))
            if predecessor:
                prompt_line = (
                    f'{prompt_line}；阶段来源: {predecessor.title}: '
                    f'{predecessor.summary or ""}'
                )
                selection_prompt_line = (
                    f'{selection_prompt_line}；阶段来源:'
                    f'[world_story_thread_id:{int(predecessor.id)}] '
                    f'标题:{_clip_text(predecessor.title, 120)}；'
                    f'状态:{predecessor.status or ""}'
                )
                item = next(
                    (
                        candidate
                        for candidate in items
                        if int(candidate.get('id') or 0) == int(row.id)
                    ),
                    None,
                )
                if item is not None:
                    item['predecessor_context'] = {
                        'id': int(predecessor.id),
                        'title': predecessor.title or '',
                        'summary': predecessor.summary or '',
                    }
            campaign_context = campaign_context_by_thread.get(int(row.id))
            if campaign_context:
                related_front_text = '；'.join(
                    f"{front['base_dungeon']}:{front['title']}({front['status']})"
                    for front in campaign_context.get('related_fronts') or []
                    if int(front.get('world_story_thread_id') or 0) != int(row.id)
                )
                prompt_line = (
                    f"{prompt_line}；[共同战役主轴:{campaign_context['campaign_title']}] "
                    f"{campaign_context['campaign_summary']}；"
                    f"主轴目标:{campaign_context['campaign_agenda']}；"
                    f"本前线目标:{campaign_context['front_agenda']}"
                )
                selection_prompt_line = (
                    f"{selection_prompt_line}；[共同战役主轴 "
                    f"world_story_thread_id:{campaign_context['campaign_root_id']}] "
                    f"标题:{campaign_context['campaign_title']}；"
                    f"主轴推进方向:{campaign_context['campaign_agenda']}；"
                    f"本前线推进方向:{campaign_context['front_agenda']}"
                )
                if related_front_text:
                    prompt_line = (
                        f'{prompt_line}；相关前线只读当前态:{related_front_text}'
                    )
            if row.id == dispatch_context.get('thread_id'):
                from plugins.adventurer_s1.services.s1_campaign_dispatch_contract import dispatch_generation_rule
                dispatch_text = json.dumps(dispatch_context, ensure_ascii=False)
                prompt_line += (
                    '\n【议事厅当前行动】campaign_dispatch_context=' + dispatch_text
                    + '\n' + dispatch_generation_rule(dispatch_context)
                )
                selection_prompt_line += (
                    '\n【议事厅当前行动】campaign_dispatch_context=' + dispatch_text
                    + '\n这是本次必须兑现的同源共同战役：首次发现时呈现可观察的事件迹象；'
                    '其余情况围绕当前 objective 建立可行动的局势。必须采用 exclusive_direct_use，'
                    'world_state_change_allowed=true；只规划本场行动机会，不预写成败或后续结局。'
                )
                if revealed_item:
                    from plugins.adventurer_s1.services.s1_campaign_revelation_contract import REVELATION_GENERATION_RULE
                    selection_prompt_line += '\n' + REVELATION_GENERATION_RULE
                if 'challenge_context' in dispatch_context:
                    from plugins.adventurer_s1.services.s1_campaign_challenge_contract import CHALLENGE_GENERATION_RULE
                    selection_prompt_line += '\n' + CHALLENGE_GENERATION_RULE
                if dispatch_context.get('closed_phase') is True:
                    from plugins.adventurer_s1.services.s1_campaign_dispatch_contract import CLOSED_PHASE_OUTLINE_RULE
                    selection_prompt_line += '\n' + CLOSED_PHASE_OUTLINE_RULE
            prompt_entry = {
                'world_story_thread_id': int(row.id),
                'prompt_line': prompt_line,
                'selection_prompt_line': selection_prompt_line,
            }
            if campaign_context:
                prompt_entry['campaign_context'] = dict(campaign_context)
            if (
                faction_presence_target
                and int(row.id)
                == int(faction_presence_target['world_story_thread_id'])
            ):
                prompt_entry['required_faction_presence'] = True
                prompt_entry['faction_presence_target'] = {
                    key: faction_presence_target[key]
                    for key in (
                        'faction_profile_id', 'profile_revision',
                        'world_story_thread_id', 'source_revision',
                    )
                }
            linked_profile_entries = faction_profile_entries_by_thread.get(
                int(row.id),
            ) or []
            if linked_profile_entries:
                prompt_entry['faction_profile_entries'] = [
                    dict(entry) for entry in linked_profile_entries
                    if isinstance(entry, dict)
                ]
            prompt_entries.append(prompt_entry)
        from plugins.adventurer_s1.services.s1_world_actor_service import build_actor_prompt_block
        actor_reference_texts = [search_text]
        actor_reference_texts.extend(
            str(value).strip() for value in (authority_reference_texts or [])
            if str(value or '').strip()
        )
        actor_reference_texts.extend(
            f"{item['title']}: {item['summary']}"
            if item['id'] == revealed_thread_id else row.to_prompt_line()
            for row, item in zip(all_picked, items)
        )
        actor_prompt_snapshot = {}
        from db_repository.s1_world_identity_repository import load_dungeon_world_identity_guards
        from plugins.adventurer_s1.services.s1_world_identity_service import render_world_identity_guards
        world_identity_guards = load_dungeon_world_identity_guards(
            dungeon,
            limit=max(0, _coerce_int(current_app.config.get('ADVENTURER_S1_WORLD_IDENTITY_GUARD_LIMIT'), 0)),
        )
        actor_prompt_thread_ids = [
            int(row.id) for row in all_picked
            if int(row.id) not in presence_only_ids
        ]
        actor_prompt_block, actor_ids, faction_keys, actor_states = build_actor_prompt_block(
            actor_prompt_thread_ids,
            mutable_thread_ids=mutable_ids,
            priority_actor_ids=countermeasure_available_actor_ids,
            excluded_actor_ids=unavailable_actor_ids,
            authority_reference_texts=[] if campaign_world(dungeon) else actor_reference_texts,
            prompt_snapshot_out=actor_prompt_snapshot,
            activity_dungeons=(campaign_world(dungeon) or {}).get('dungeons'),
        )
        if expedition_log_id and actor_ids:
            from plugins.adventurer_s1.services.s1_world_actor_profile_backfill_ai_service import (
                submit_missing_world_actor_profiles,
            )
            submit_missing_world_actor_profiles(actor_ids)
        actor_entry_rows = actor_prompt_snapshot.get('entries') or []
        exact_actor_injection_count = sum(
            1 for item in actor_entry_rows
            if isinstance(item, dict) and item.get('exact_reference_match') is True
        )
        selection_diagnostics = {
            'exact_dungeon_hit_count': exact_dungeon_hit_count,
            'global_only_filtered_count': global_only_filtered_count,
            'exact_actor_injection_count': exact_actor_injection_count,
        }
        if dormant_context_filtered_ids:
            selection_diagnostics['unrelated_dormant_thread_ids'] = sorted(
                dormant_context_filtered_ids,
            )
        if locus_filtered_thread_ids:
            selection_diagnostics['locus_filtered_thread_ids'] = sorted(
                locus_filtered_thread_ids,
            )
        if leased_elsewhere_thread_ids:
            selection_diagnostics['leased_elsewhere_thread_ids'] = sorted(
                leased_elsewhere_thread_ids,
            )
        if leased_elsewhere_actor_ids:
            selection_diagnostics['leased_elsewhere_actor_ids'] = sorted(
                leased_elsewhere_actor_ids,
            )
        if unavailable_actor_thread_ids:
            selection_diagnostics['unavailable_active_actor_thread_ids'] = sorted(
                unavailable_actor_thread_ids,
            )
        if unavailable_actor_ids:
            selection_diagnostics['unavailable_active_actor_ids'] = sorted(
                unavailable_actor_ids,
            )
        if owned_actor_ids:
            selection_diagnostics['owned_world_actor_lease_ids'] = sorted(owned_actor_ids)
        if actor_deduplicated_thread_ids:
            selection_diagnostics['actor_deduplicated_thread_ids'] = actor_deduplicated_thread_ids
        if required_faction_interaction_ids:
            selection_diagnostics['required_faction_interaction_thread_ids'] = sorted(
                required_faction_interaction_ids,
            )
        if required_campaign_manifestation_ids:
            selection_diagnostics['required_campaign_manifestation_thread_ids'] = sorted(
                required_campaign_manifestation_ids,
            )
        if reseeded_world_actor_thread_id:
            selection_diagnostics['reseeded_world_actor_thread_id'] = int(
                reseeded_world_actor_thread_id,
            )
        if deferred_faction_interaction_ids:
            selection_diagnostics['deferred_faction_interaction_thread_ids'] = (
                deferred_faction_interaction_ids
            )
        if countermeasure_due_actor_ids:
            selection_diagnostics['countermeasure_due_actor_ids'] = list(
                countermeasure_due_actor_ids,
            )
        if countermeasure_preferred_thread_ids:
            selection_diagnostics[
                'countermeasure_preferred_thread_ids'
            ] = list(countermeasure_preferred_thread_ids)
        countermeasure_prompt_actor_ids = [
            actor_id for actor_id in actor_ids
            if actor_id in set(countermeasure_available_actor_ids)
        ]
        if countermeasure_prompt_actor_ids:
            selection_diagnostics['countermeasure_prompt_actor_ids'] = (
                countermeasure_prompt_actor_ids
            )
        if faction_presence_target:
            selection_diagnostics['required_faction_presence_target'] = {
                key: faction_presence_target[key]
                for key in (
                    'faction_profile_id', 'profile_revision',
                    'world_story_thread_id', 'source_revision', 'presence_debt',
                )
            }
        world_story_block = S1WorldOutlineService._render_world_story_prompt_entries(
            prompt_entries, frozen_items=items,
        )
        prompt_block = world_story_block
        if actor_prompt_block:
            prompt_block = f'{prompt_block}\n\n{actor_prompt_block}' if prompt_block else actor_prompt_block
        identity_guard_block = render_world_identity_guards(
            world_identity_guards,
            actor_ids={
                row['id'] for row in actor_states
                if isinstance(row, dict) and type(row.get('id')) is int
            },
            location_ids=set(),
        )
        if identity_guard_block:
            prompt_block = '\n\n'.join(filter(None, (prompt_block, identity_guard_block)))
        if not prompt_block:
            return {
                'target_dungeon': dungeon,
                'context_text': search_text,
                'context_keywords': anchor_terms,
                'context_char_count': len(search_text),
                'items': [],
                'world_actor_ids': [],
                'world_actor_states': [],
                'faction_keys': [],
                'faction_profile_ids': [],
                'faction_profile_states': [],
                'world_identity_guards': world_identity_guards,
                'selection_diagnostics': selection_diagnostics,
                'prompt_block': '',
                'diagnostic': 'no_matching_world_story_thread',
            }
        return {
            'target_dungeon': dungeon,
            'context_text': search_text,
            'context_keywords': anchor_terms,
            'context_char_count': len(search_text),
            'items': items,
            'required_faction_interaction_thread_ids': sorted(
                required_faction_interaction_ids,
            ),
            'required_campaign_manifestation_thread_ids': sorted(
                required_campaign_manifestation_ids,
            ),
            'required_faction_presence_targets': [
                {
                    key: faction_presence_target[key]
                    for key in (
                        'faction_profile_id', 'profile_revision',
                        'world_story_thread_id', 'source_revision',
                    )
                }
            ] if faction_presence_target else [],
            'world_actor_ids': actor_ids,
            'world_actor_states': actor_states,
            'faction_keys': faction_keys,
            'faction_profile_ids': faction_profile_snapshot.get('profile_ids') or [],
            'faction_profile_states': faction_profile_snapshot.get('states') or [],
            'world_story_prompt_entries': prompt_entries,
            'world_actor_prompt_block': actor_prompt_block,
            'world_actor_prompt_header': actor_prompt_snapshot.get('header') or '',
            'world_actor_prompt_entries': actor_prompt_snapshot.get('entries') or [],
            'world_identity_guards': world_identity_guards,
            'selection_diagnostics': selection_diagnostics,
            'prompt_block': prompt_block,
            'diagnostic': 'matched',
        }

    @staticmethod
    def _dormant_world_story_matches_current_context(
        scope_keys,
        *,
        expedition_locus_scope_key: str | None,
        linked_actor_ids: set[int],
        selected_actor_ids: set[int],
    ) -> bool:
        """Dormant facts need an exact local or selected-subject anchor, not age."""
        local_refs = extract_location_scope_keys(scope_keys)
        locus = str(expedition_locus_scope_key or '').strip()
        if local_refs:
            # An actor link cannot bridge an incompatible or unknown location.
            return (
                locus in local_refs
                and location_scope_key(locus.removeprefix('s1_location_state:')) == locus
            )
        return bool(linked_actor_ids & selected_actor_ids)

    @staticmethod
    def _pick_world_story_threads_for_closure(
        dungeon: str,
        *,
        excluded_ids: set[int] | None = None,
        party_id: int | None = None,
        expedition_log_id: int | None = None,
        context_text: str | None = None,
        context_keywords=None,
    ) -> list[AdventurerS1WorldStoryThread]:
        """Pick an overdue, identity-anchored public line without competing with normal injection."""
        from flask import current_app

        limit = max(0, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_CLOSURE_PROMPT_LIMIT'), 1,
        ))
        if limit <= 0:
            return []
        stale_days = max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_CLOSURE_STALE_DAYS'), 7,
        ))
        cutoff = _portable_utc_now() - timedelta(days=stale_days)
        search_text, anchor_terms = _build_world_intel_context(dungeon, context_text, context_keywords)
        excluded = set(excluded_ids or set())
        rows = (
            AdventurerS1WorldStoryThread.query
            .filter(
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.status.in_(('seeded', 'active', 'escalated', 'closing')),
                AdventurerS1WorldStoryThread.campaign_role.is_(None),
                S1CampaignWorldRepository.thread_scope(dungeon),
            )
            .order_by(
                AdventurerS1WorldStoryThread.closure_debt.desc(),
                AdventurerS1WorldStoryThread.missed_injection_runs.desc(),
                AdventurerS1WorldStoryThread.updated_at.asc(),
            )
            .all()
        )
        _hydrate_legacy_world_story_base_dungeons(rows)
        picked = []
        for row in rows:
            if int(row.id) in excluded:
                continue
            if not _world_story_thread_has_exact_dungeon_scope(row, dungeon):
                continue
            overdue = bool(
                row.status == 'closing'
                or int(row.closure_debt or 0) > 0
                or int(row.missed_injection_runs or 0) > 0
                or (row.updated_at and row.updated_at <= cutoff)
            )
            if not overdue:
                continue
            picked.append(row)
            if len(picked) >= limit:
                break
        return picked

    @staticmethod
    def persist_world_intel_injection_snapshot(
        expedition_log_id: int | None,
        field: str,
        snapshot: dict | None,
    ) -> None:
        if not expedition_log_id or not field or not isinstance(snapshot, dict):
            return
        if field not in ('objective_world_intel_injection', 'generation_world_intel_injection'):
            return
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=int(expedition_log_id)).with_for_update().first()
            if not log:
                return
            full_log = dict(log.full_log or {}) if isinstance(log.full_log, dict) else {}
            full_log[field] = dict(snapshot)
            log.full_log = full_log

    @staticmethod
    def persist_world_intel_adoptions(
        expedition_log_id: int | None,
        adoptions: list[dict] | None,
    ) -> None:
        """Persist the validated objective-outline adoption decisions for later stages."""
        if not expedition_log_id:
            return
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=int(expedition_log_id)).with_for_update().first()
            if not log:
                return
            full_log = dict(log.full_log or {}) if isinstance(log.full_log, dict) else {}
            full_log['world_intel_adoptions'] = [dict(item) for item in (adoptions or [])]
            log.full_log = full_log

    @staticmethod
    def load_generation_world_intel_snapshot(
        expedition_log_id: int | None,
        *,
        target_dungeon: str,
    ) -> dict | None:
        """Reuse the server-owned world input already consumed by this expedition."""
        if not expedition_log_id:
            return None
        log = AdventurerS1ExpeditionLog.query.filter_by(id=int(expedition_log_id)).first()
        full_log = log.full_log if log and isinstance(log.full_log, dict) else {}
        snapshot = full_log.get('generation_world_intel_injection')
        if not isinstance(snapshot, dict):
            return None
        snapshot_dungeon = str(snapshot.get('target_dungeon') or '').strip()
        if snapshot_dungeon and snapshot_dungeon != str(target_dungeon or '').strip():
            return None
        return dict(snapshot)

    @staticmethod
    def load_objective_world_intel_context(expedition_log_id: int | None) -> tuple[dict | None, list[dict]]:
        """Load the frozen objective snapshot and its validated adoption decisions."""
        if not expedition_log_id:
            return None, []
        log = AdventurerS1ExpeditionLog.query.filter_by(id=int(expedition_log_id)).first()
        full_log = log.full_log if log and isinstance(log.full_log, dict) else {}
        snapshot = full_log.get('objective_world_intel_injection')
        adoptions = full_log.get('world_intel_adoptions')
        return (
            dict(snapshot) if isinstance(snapshot, dict) else None,
            [dict(item) for item in adoptions if isinstance(item, dict)]
            if isinstance(adoptions, list) else [],
        )

    @staticmethod
    def apply_world_intel_adoptions_to_snapshot(
        snapshot: dict | None,
        adoptions: list[dict] | None,
        actor_adoptions: list[dict] | None = None,
    ) -> dict:
        """Remove ignored intel, then attach the validated authorization for retained entries."""
        if not isinstance(snapshot, dict):
            return {}
        result = dict(snapshot)
        normalized_actor_usages: list[dict] | None = None
        if actor_adoptions is not None:
            normalized_actor_usages = []
            seen_actor_ids: set[int] = set()
            for raw_usage in actor_adoptions:
                if not isinstance(raw_usage, dict):
                    continue
                if isinstance(
                    raw_usage.get('world_actor_id'), bool,
                ) or isinstance(raw_usage.get('capability_revision'), bool):
                    continue
                actor_id = _coerce_int(raw_usage.get('world_actor_id'), 0)
                capability_revision = _coerce_int(
                    raw_usage.get('capability_revision'), 0,
                )
                usage_mode = str(raw_usage.get('usage_mode') or '')
                if (
                    actor_id <= 0
                    or capability_revision <= 0
                    or actor_id in seen_actor_ids
                    or usage_mode not in {
                        'ignored', 'background_reference', 'exclusive_direct_use',
                    }
                ):
                    continue
                seen_actor_ids.add(actor_id)
                normalized_actor_usages.append({
                    'world_actor_id': actor_id,
                    'capability_revision': capability_revision,
                    'usage_mode': usage_mode,
                })
            result['world_actor_usages'] = normalized_actor_usages
        normalized = [dict(item) for item in (adoptions or []) if isinstance(item, dict)]
        frozen_items = [
            item for item in (result.get('items') or [])
            if isinstance(item, dict)
        ]
        presence_items_by_id = {
            _coerce_int(item.get('id') or item.get('world_story_thread_id'), 0): item
            for item in frozen_items
            if item.get('required_faction_presence') is True
            and _coerce_int(
                item.get('id') or item.get('world_story_thread_id'), 0,
            ) > 0
        }
        normalized_by_id = {
            _coerce_int(item.get('world_story_thread_id'), 0): item
            for item in normalized
            if _coerce_int(item.get('world_story_thread_id'), 0) > 0
        }
        # 软存在感是服务端冻结的 background 下限；客观大纲失败或选择 ignored
        # 都只做确定性降权采纳，不制造合同错误、AI 重试或世界线写权限。
        for thread_id, frozen_item in presence_items_by_id.items():
            frozen_presence_target = frozen_item.get('faction_presence_target')
            if not isinstance(frozen_presence_target, dict):
                frozen_presence_target = {}
            usage = normalized_by_id.get(thread_id)
            if usage is None:
                usage = {
                    'world_story_thread_id': thread_id,
                    'source_revision': max(
                        1,
                        _coerce_int(
                            frozen_presence_target.get('source_revision'),
                            _coerce_int(frozen_item.get('revision'), 1),
                        ),
                    ),
                    'usage_mode': 'background_reference',
                }
                normalized.append(usage)
                normalized_by_id[thread_id] = usage
            elif usage.get('usage_mode') == 'ignored':
                usage['usage_mode'] = 'background_reference'
        result['world_intel_usages'] = normalized
        direct_ids = [
            int(item.get('world_story_thread_id') or 0)
            for item in normalized
            if item.get('usage_mode') == 'exclusive_direct_use'
        ]
        direct_id_set = {thread_id for thread_id in direct_ids if thread_id > 0}
        background_ids = [
            int(item.get('world_story_thread_id') or 0)
            for item in normalized
            if item.get('usage_mode') == 'background_reference'
        ]
        ignored_ids = [
            int(item.get('world_story_thread_id') or 0)
            for item in normalized
            if item.get('usage_mode') == 'ignored'
        ]
        snapshot_items = []
        for raw_item in frozen_items:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            thread_id = int(item.get('id') or item.get('world_story_thread_id') or 0)
            if (
                item.get('required_faction_interaction') is True
                and thread_id not in direct_id_set
            ):
                item['required_faction_interaction'] = False
            if (
                item.get('required_campaign_manifestation') is True
                and thread_id not in direct_id_set
            ):
                item['required_campaign_manifestation'] = False
            snapshot_items.append(item)
        result['items'] = snapshot_items
        required_faction_interaction_ids = sorted({
            int(item.get('id') or item.get('world_story_thread_id') or 0)
            for item in snapshot_items
            if item.get('required_faction_interaction') is True
            and int(item.get('id') or item.get('world_story_thread_id') or 0) > 0
        })
        result['required_faction_interaction_thread_ids'] = (
            required_faction_interaction_ids
        )
        required_campaign_manifestation_ids = sorted({
            int(item.get('id') or item.get('world_story_thread_id') or 0)
            for item in snapshot_items
            if item.get('required_campaign_manifestation') is True
            and int(item.get('id') or item.get('world_story_thread_id') or 0) > 0
        })[:1]
        result['required_campaign_manifestation_thread_ids'] = (
            required_campaign_manifestation_ids
        )
        required_faction_presence_targets = [
            dict(item.get('faction_presence_target'))
            for item in snapshot_items
            if item.get('required_faction_presence') is True
            and isinstance(item.get('faction_presence_target'), dict)
            and int(item.get('id') or item.get('world_story_thread_id') or 0)
            in (set(direct_ids) | set(background_ids))
        ][:1]
        result['required_faction_presence_targets'] = (
            required_faction_presence_targets
        )
        authorization_block = (
            '【本次世界情报采纳授权（结构化硬约束）】\n'
            f'exclusive_direct_use={direct_ids}；background_reference={background_ids}\n'
            '只有 exclusive_direct_use 中的准确 thread_id 可让该情报的唯一主体直接现身、行动、说话或互动，'
            '可让唯一物品本体被接触/取得，或让唯一事件、势力行动、公共地点状态产生本次实质推进。\n'
            'background_reference 只允许已有公开事实、传闻、历史、静态地点现状与既成后果；'
            '不得让其长期主体本体出场，不得复制唯一物品或推进唯一事件。\n'
            '若 exclusive_direct_use 非空，正文必须实际兑现对应采纳；不得把它降回一句无关彩蛋。'
        )
        if required_faction_interaction_ids:
            authorization_block += (
                '\n【酒馆势力交互强制闭环】'
                f'required_faction_interaction_thread_ids={required_faction_interaction_ids}。'
                '该后端派生数组固定为 1 项；'
                '这些准确 thread_id 必须位于 exclusive_direct_use；玩家可见 events 必须让对应'
                '外部势力对深渊酒馆冒险者生态已经成立的行动、机会或阻力沿本次既有目标'
                '因果实际进入共同主线，不得另开互不相干的平行任务或替换本次目标，'
                '并形成可核对阶段结果。每个 id 都必须以同一 source_revision 在 '
                'world_fact_updates 中输出 advance|close|resolve|successor；'
                '不得只写传闻、预兆、态度说明或未来可能性。'
            )
        if required_campaign_manifestation_ids:
            authorization_block += (
                '\n【共同战役前线强制闭环】'
                'required_campaign_manifestation_thread_ids='
                f'{required_campaign_manifestation_ids}。该后端派生数组固定为 1 项；'
                '准确前线必须位于 exclusive_direct_use，并沿本次目标地牢、委托、特殊事件'
                '或隐藏Boss已经成立的同一因果进入共同主线。玩家可见 events 必须让本前线'
                '运营目标形成可核对的阶段结果；相关其它前线只读，不得替它们行动或推进。'
                '同一 id/source_revision 必须在 world_fact_updates 输出 '
                'advance|close|resolve|successor；不得另开平行任务、替换本次目标，'
                '也不得只写传闻、预兆或未来可能性。'
            )
        if required_faction_presence_targets:
            presence_ids = [
                int(item['world_story_thread_id'])
                for item in required_faction_presence_targets
            ]
            authorization_block += (
                '\n【势力创作锚点软闭环】'
                f'required_faction_presence_thread_ids={presence_ids}。'
                '该后端派生数组固定为 1 项；对应世界线至少按 background_reference 进入本场，'
                '并取得本阶段 system prompt 定义的势力创作授权。准确档案当前态是创作锚点，'
                '不是固定分面清单；旧 world_story_thread 仍无直接写权限。若自然叙事没有合适空间，'
                '故事仍须正常完成；该软缺口不得触发重写、结算失败或额外 AI 调用。若同一 id 同时位于 '
                'exclusive_direct_use，则直接采纳/强交互合同优先，本软闭环不另加剧情负担。'
            )
        prompt_entries = result.get('world_story_prompt_entries')
        if isinstance(prompt_entries, list):
            retained_ids = set(direct_ids) | set(background_ids)
            profile_states = result.get('faction_profile_states')
            retained_entries = []
            for raw_entry in prompt_entries:
                if not isinstance(raw_entry, dict):
                    continue
                thread_id = _coerce_int(
                    raw_entry.get('world_story_thread_id'), 0,
                )
                if thread_id not in retained_ids:
                    continue
                usage_mode = (
                    'exclusive_direct_use'
                    if thread_id in direct_id_set else 'background_reference'
                )
                retained_entry = dict(raw_entry)
                retained_entry['usage_mode'] = usage_mode
                retained_entries.append(retained_entry)
            result['world_story_prompt_entries'] = retained_entries
            retained_faction_profile_ids = {
                _coerce_int(profile_entry.get('faction_profile_id'), 0)
                for item in retained_entries
                for profile_entry in (item.get('faction_profile_entries') or [])
                if isinstance(profile_entry, dict)
                and _coerce_int(profile_entry.get('faction_profile_id'), 0) > 0
            }
            result['faction_profile_ids'] = sorted(retained_faction_profile_ids)
            if isinstance(profile_states, list):
                result['faction_profile_states'] = [
                    dict(item) for item in profile_states
                    if isinstance(item, dict)
                    and _coerce_int(item.get('id'), 0) in retained_faction_profile_ids
                ]
            world_story_block = S1WorldOutlineService._render_world_story_prompt_entries(
                retained_entries,
                adopted=True,
                frozen_items=snapshot_items,
            )
            actor_prompt_entries = result.get('world_actor_prompt_entries')
            if isinstance(actor_prompt_entries, list):
                actor_usage_by_id = {
                    int(item.get('world_actor_id') or 0): str(item.get('usage_mode') or '')
                    for item in (normalized_actor_usages or [])
                    if isinstance(item, dict) and int(item.get('world_actor_id') or 0) > 0
                }
                actor_state_by_id = {
                    _coerce_int(item.get('id'), 0): dict(item)
                    for item in (result.get('world_actor_states') or [])
                    if isinstance(item, dict)
                    and _coerce_int(item.get('id'), 0) > 0
                }
                candidate_stage_prefixes = (
                    '本次 usage_mode 允许值：',
                    '本体直接使用组合：',
                )
                retained_actor_entries = []
                for item in actor_prompt_entries:
                    if not isinstance(item, dict):
                        continue
                    actor_id = int(item.get('world_actor_id') or 0)
                    item_status = str(item.get('status') or '')
                    is_terminal = item_status in ('dead', 'retired')
                    usage_mode = actor_usage_by_id.get(actor_id, '')
                    if not (
                        is_terminal
                        or actor_adoptions is None
                        or usage_mode
                        in ('background_reference', 'exclusive_direct_use')
                    ):
                        continue
                    retained_entry = dict(item)
                    if actor_adoptions is not None:
                        retained_entry['usage_mode'] = (
                            'background_reference'
                            if is_terminal
                            else usage_mode
                        )
                    effective_usage_mode = str(
                        retained_entry.get('usage_mode') or '',
                    )
                    actor_state = actor_state_by_id.get(actor_id)
                    if actor_state and (
                        is_terminal
                        or effective_usage_mode == 'background_reference'
                    ):
                        actor_status = str(
                            actor_state.get('status')
                            or item_status
                        )
                        retained_entry['prompt_block'] = (
                            '[权威终态·只读]\n'
                            if actor_status in ('dead', 'retired')
                            else '[背景引用]\n'
                        ) + json.dumps({
                            'world_actor_id': actor_id,
                            'capability_revision': _coerce_int(
                                item.get('capability_revision'), 0,
                            ),
                            'usage_mode': 'background_reference',
                            'status': actor_status,
                            'canonical_name': actor_state.get('canonical_name') or '',
                            'identity_summary': actor_state.get('identity_summary') or '',
                            'current_state': actor_state.get('current_state') or '',
                        }, ensure_ascii=False, separators=(',', ':'))
                        retained_entry['countermeasure_knowledge_ids'] = []
                        retained_entry['countermeasure_knowledge_refs'] = []
                    elif actor_adoptions is not None:
                        adopted_lines = [
                            line
                            for line in str(
                                retained_entry.get('prompt_block') or '',
                            ).splitlines()
                            if not line.startswith(candidate_stage_prefixes)
                        ]
                        adopted_block = '\n'.join(adopted_lines).strip()
                        if effective_usage_mode == 'exclusive_direct_use':
                            if '[可直接推进]' in adopted_block:
                                adopted_block = adopted_block.replace(
                                    '[可直接推进]', '[已直接采纳]', 1,
                                )
                            elif adopted_block:
                                adopted_block = '[已直接采纳]\n' + adopted_block
                        elif adopted_block:
                            adopted_block = (
                                f'[usage_mode:{effective_usage_mode}]\n'
                                + adopted_block
                            )
                        retained_entry['prompt_block'] = adopted_block
                    retained_actor_entries.append(retained_entry)
                result['world_actor_prompt_entries'] = retained_actor_entries
                actor_body = '\n\n'.join(
                    str(item.get('prompt_block') or '').strip()
                    for item in retained_actor_entries
                    if str(item.get('prompt_block') or '').strip()
                )
                actor_header = (
                    '【本场已采纳长期世界角色】'
                    if actor_adoptions is not None
                    else str(result.get('world_actor_prompt_header') or '').strip()
                )
                actor_prompt_block = (
                    f'{actor_header}\n{actor_body}' if actor_header and actor_body else actor_body
                )
                result['world_actor_prompt_header'] = actor_header
                result['world_actor_prompt_block'] = actor_prompt_block
            else:
                actor_prompt_block = str(result.get('world_actor_prompt_block') or '').strip()
            prompt_block = (
                f'{world_story_block}\n\n{actor_prompt_block}'
                if world_story_block and actor_prompt_block
                else world_story_block or actor_prompt_block
            )
        else:
            # 兼容部署前已冻结的在途快照：它没有逐条 prompt provenance，不能安全拆除正文。
            # 保留旧块并明确列出 ignored，避免错误授权；新快照不会走此分支。
            prompt_block = str(result.get('prompt_block') or '').strip()
            if ignored_ids:
                authorization_block += f'\n兼容旧快照：ignored={ignored_ids}，这些条目不得进入本次剧情。'
        result['prompt_block'] = (
            f'{prompt_block}\n\n{authorization_block}' if prompt_block else authorization_block
        )
        from plugins.adventurer_s1.services.s1_world_identity_service import render_world_identity_guards
        identity_guard_block = render_world_identity_guards(
            result.get('world_identity_guards'),
            actor_ids={
                row['id'] for row in result.get('world_actor_states') or []
                if isinstance(row, dict) and type(row.get('id')) is int
            },
            location_ids=set(),
        )
        if identity_guard_block and identity_guard_block not in result['prompt_block']:
            result['prompt_block'] += '\n\n' + identity_guard_block
        return result

    @staticmethod
    def _validate_required_story_progression_result(
        final_result_data: dict | None,
        cached_full_log: dict | None,
        *,
        outline_field: str,
        snapshot_flag: str,
        error_label: str,
    ) -> list[str]:
        """Validate one server-frozen direct-use progression contract."""
        result_data = final_result_data if isinstance(final_result_data, dict) else {}
        full_log = cached_full_log if isinstance(cached_full_log, dict) else {}
        outline = (
            result_data.get('objective_outline')
            if isinstance(result_data.get('objective_outline'), dict)
            else full_log.get('objective_outline')
        )
        merged_log = dict(full_log)
        for key in (
            'objective_world_intel_injection',
            'generation_world_intel_injection',
        ):
            if key in result_data:
                merged_log[key] = result_data[key]
        snapshot_items = _world_intel_snapshot_items(merged_log)
        required_ids = {
            _coerce_int(value, 0)
            for value in (
                (outline or {}).get(outline_field) or []
            )
            if _coerce_int(value, 0) > 0
        } | {
            thread_id
            for thread_id, item in snapshot_items.items()
            if item.get(snapshot_flag) is True
        }
        if not required_ids:
            return []
        facts = result_data.get('world_fact_updates')
        if not isinstance(facts, list):
            facts = []
        errors: list[str] = []
        for thread_id in sorted(required_ids):
            item = snapshot_items.get(thread_id)
            if not item or item.get(snapshot_flag) is not True:
                errors.append(
                    f'{error_label}: '
                    f'thread_id={thread_id} 不在同一冻结快照的强制交互白名单中'
                )
                continue
            expected_revision = max(1, _coerce_int(item.get('revision'), 1))
            updates = [
                fact
                for fact in facts
                if isinstance(fact, dict)
                and _strict_ai_positive_integer(
                    fact.get('source_world_story_thread_id')
                ) == thread_id
                and _strict_ai_text(fact.get('change_kind')).lower()
                in {'advance', 'close', 'resolve', 'successor'}
            ]
            if not updates:
                errors.append(
                    f'{error_label}: '
                    f'thread_id={thread_id} 缺少同源阶段 world_fact_updates'
                )
                continue
            invalid_revisions = sorted({
                _strict_ai_positive_integer(fact.get('source_revision'))
                for fact in updates
                if _strict_ai_positive_integer(
                    fact.get('source_revision')
                ) != expected_revision
            })
            if invalid_revisions:
                errors.append(
                    f'{error_label}: '
                    f'thread_id={thread_id} 必须原样回传 source_revision='
                    f'{expected_revision}，实际={invalid_revisions}'
                )
        return errors

    @staticmethod
    def validate_required_faction_interaction_result(
        final_result_data: dict | None,
        cached_full_log: dict | None,
    ) -> list[str]:
        """Mechanically close every server-selected tavern-faction interaction."""
        return S1WorldOutlineService._validate_required_story_progression_result(
            final_result_data,
            cached_full_log,
            outline_field='required_faction_interaction_thread_ids',
            snapshot_flag='required_faction_interaction',
            error_label='required_faction_interaction',
        )

    @staticmethod
    def validate_required_campaign_manifestation_result(
        final_result_data: dict | None,
        cached_full_log: dict | None,
    ) -> list[str]:
        """Mechanically close the one server-selected campaign front."""
        return S1WorldOutlineService._validate_required_story_progression_result(
            final_result_data,
            cached_full_log,
            outline_field='required_campaign_manifestation_thread_ids',
            snapshot_flag='required_campaign_manifestation',
            error_label='required_campaign_manifestation',
        )

    @staticmethod
    def _score_world_story_thread_for_context(
        row: AdventurerS1WorldStoryThread,
        dungeon: str,
        context_text: str,
        anchor_terms: list[str],
        hot_terms: set[str],
    ) -> int:
        from flask import current_app

        if not _world_story_thread_has_exact_dungeon_scope(row, dungeon):
            return 0
        weight = int(getattr(row, 'weight', 0) or 0)
        injection_count = int(getattr(row, 'injection_count', 0) or 0)
        missed_runs = int(getattr(row, 'missed_injection_runs', 0) or 0)
        closure_debt = int(getattr(row, 'closure_debt', 0) or 0)
        new_line_boost = max(0, 24 - injection_count * 4)
        return_boost = min(32, missed_runs * 8)
        closure_boost = min(90, closure_debt * 15)
        local_bonus = 10
        type_bonus = 0
        status = getattr(row, 'status', '')
        if status == 'closing':
            status_penalty = -40
        elif status == 'escalated':
            status_penalty = 10
        elif status == 'dormant':
            status_penalty = 20
        else:
            status_penalty = 0
        return weight + local_bonus + type_bonus + new_line_boost + return_boost + closure_boost - status_penalty

    @staticmethod
    def _rank_world_story_threads_for_context(
        dungeon: str,
        *,
        context_text: str | None = None,
        context_keywords=None,
        expedition_log_id: int | None = None,
    ) -> list[tuple[int, AdventurerS1WorldStoryThread]]:
        hot_terms = set() if campaign_world(dungeon) else set(S1WorldOutlineService.get_motif_cooldown_terms())
        search_text, anchor_terms = _build_world_intel_context(dungeon, context_text, context_keywords)
        rows = (
            AdventurerS1WorldStoryThread.query
            .filter(
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.status.in_(('seeded', 'active', 'escalated', 'closing', 'dormant')),
                S1CampaignWorldRepository.thread_scope(dungeon),
                or_(
                    AdventurerS1WorldStoryThread.campaign_role.is_(None),
                    AdventurerS1WorldStoryThread.campaign_role
                    != WORLD_CAMPAIGN_SPINE_ROLE,
                ),
            )
            .all()
        )
        _hydrate_legacy_world_story_base_dungeons(rows)
        from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
        rows = S1CampaignDispatchService.filter_fronts(rows, expedition_log_id)
        scored = [
            (S1WorldOutlineService._score_world_story_thread_for_context(
                row, dungeon, search_text, anchor_terms, hot_terms,
            ), row)
            for row in rows
        ]
        scored.sort(
            key=lambda item: (
                item[0],
                -(int(item[1].injection_count or 0)),
                getattr(item[1], 'updated_at', None) or datetime.min,
                -int(item[1].id or 0),
            ),
            reverse=True,
        )
        return scored

    @staticmethod
    def _explain_world_story_thread_match(
        row: AdventurerS1WorldStoryThread,
        dungeon: str,
        context_text: str,
        anchor_terms: list[str],
        hot_terms: set[str],
    ) -> dict:
        keys = [str(k).strip() for k in (row.scope_keys or []) if str(k).strip()]
        matched_scope_keys = [
            key for key in keys
            if dungeon and key == dungeon
        ]
        matched_base_dungeon = bool(
            dungeon and _effective_world_story_base_dungeon(row) == dungeon
        )
        return {
            'match_score': S1WorldOutlineService._score_world_story_thread_for_context(
                row, dungeon, context_text, anchor_terms, hot_terms,
            ),
            'matched_title': False,
            'matched_scope_keys': matched_scope_keys,
            'matched_base_dungeon': matched_base_dungeon,
            'matched_trigger_keywords': [],
            'matched_context_terms': matched_scope_keys,
            'matched_text_tokens': [],
        }

    @staticmethod
    def _pick_world_story_threads_for_prompt(
        dungeon: str,
        *,
        limit: int,
        excluded_ids: set[int] | None = None,
        preferred_ids: list[int] | set[int] | None = None,
        party_id: int | None = None,
        expedition_log_id: int | None = None,
        context_text: str | None = None,
        context_keywords=None,
    ) -> list[AdventurerS1WorldStoryThread]:
        scored = S1WorldOutlineService._rank_world_story_threads_for_context(
            dungeon,
            context_text=context_text,
            context_keywords=context_keywords,
            expedition_log_id=expedition_log_id,
        )
        from db_repository.s1_encounter_preparation_repository import encounter_pool_thread_clause
        pool_ids = {int(row_id) for row_id, in AdventurerS1WorldStoryThread.query.filter(
            AdventurerS1WorldStoryThread.id.in_([row.id for _, row in scored]),
            encounter_pool_thread_clause(),
        ).with_entities(AdventurerS1WorldStoryThread.id).all()}
        live_pool_ids = {int(row_id) for row_id, in AdventurerS1WorldStoryThread.query.filter(
            AdventurerS1WorldStoryThread.id.in_(pool_ids),
            encounter_pool_thread_clause(active_only=True),
        ).with_entities(AdventurerS1WorldStoryThread.id).all()} if pool_ids else set()
        scored = [(score, row) for score, row in scored
                  if int(row.id) not in pool_ids or int(row.id) in live_pool_ids]
        # Shuffle only equal-relevance pool slots; campaigns, due learning,
        # scope filters and the existing lease remain the selection authorities.
        if expedition_log_id and live_pool_ids:
            rng = random.Random(f'encounter:{dungeon}:{expedition_log_id}')
            for score in {score for score, row in scored if int(row.id) in live_pool_ids}:
                slots = [i for i, (value, row) in enumerate(scored)
                         if value == score and int(row.id) in live_pool_ids]
                choices = [scored[i] for i in slots]
                rng.shuffle(choices)
                for index, choice in zip(slots, choices):
                    scored[index] = choice
        from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
        dispatch_context = S1CampaignDispatchService.context(expedition_log_id)
        dispatch_thread_id = dispatch_context.get('thread_id')
        for _, row in scored:
            setattr(row, _REQUIRED_TAVERN_INTERACTION_TRANSIENT_ATTR, False)
        excluded = {int(value) for value in (excluded_ids or set())}
        interaction_window_open = (
            not dispatch_thread_id
            and any(
                score > 0 and int(row.id) not in excluded
                and _is_due_tavern_faction_interaction(row)
                for score, row in scored
            )
            and _tavern_faction_interaction_cooldown_ready(dungeon)
        )
        campaign_window_open = (
            not interaction_window_open
            and any(_is_due_campaign_front(row) for _, row in scored)
        )
        raw_preferred = preferred_ids or []
        if isinstance(raw_preferred, set):
            raw_preferred = sorted(raw_preferred)
        preferred_rank = {
            int(value): rank
            for rank, value in enumerate(raw_preferred)
            if int(value or 0) > 0
        }
        # Preserve the existing score order within each group, but do not let a
        # tavern-facing faction interaction whose aggregate cadence debt has matured
        # lose the sole mutable slot. A newly established or recently progressed
        # relation remains a normal optional candidate.
        scored.sort(
            key=lambda item: (
                item[1].id == dispatch_thread_id,
                interaction_window_open
                and _is_due_tavern_faction_interaction(item[1]),
                campaign_window_open and _is_due_campaign_front(item[1]),
                int(item[1].id) in preferred_rank,
                -preferred_rank.get(int(item[1].id), len(preferred_rank)),
            ),
            reverse=True,
        )
        picked = []
        interaction_window_attempted = False
        campaign_front_selected = False
        for score, row in scored:
            if score <= 0:
                continue
            closed_phase_task = (row.id == dispatch_thread_id and row.status == 'dormant'
                                 and dispatch_context.get('closed_phase') is True)
            if row.status not in WORLD_INTEL_MUTABLE_STATUSES and not closed_phase_task:
                continue
            if int(row.id) in excluded:
                continue
            if (
                _is_campaign_front(row)
                and (
                    campaign_front_selected
                    or (
                        interaction_window_open
                        and not _is_due_tavern_faction_interaction(row)
                    )
                )
            ):
                # One expedition advances at most one campaign front. An overdue
                # tavern relation keeps its older, stronger cadence authority.
                continue
            interaction_window_candidate = bool(
                interaction_window_open
                and not interaction_window_attempted
                and _is_due_tavern_faction_interaction(row)
            )
            if interaction_window_candidate:
                # A concurrent expedition that loses this exact lease must not
                # fall through and force a second overdue faction line.
                interaction_window_attempted = True
            if expedition_log_id:
                from plugins.adventurer_s1.services.s1_narrative_lease_service import (
                    acquire_world_story_bundle_lease,
                )
                with db_manager.transaction():
                    ok = acquire_world_story_bundle_lease(
                        target_dungeon=dungeon,
                        world_story_thread_id=int(row.id),
                        party_id=party_id,
                        expedition_log_id=expedition_log_id,
                        source_log_id=getattr(row, 'last_seen_log_id', None),
                        title=getattr(row, 'title', None),
                    )
                if not ok:
                    continue
            setattr(
                row,
                _REQUIRED_TAVERN_INTERACTION_TRANSIENT_ATTR,
                interaction_window_candidate,
            )
            picked.append(row)
            if _is_campaign_front(row):
                campaign_front_selected = True
            if len(picked) >= limit:
                break
        return picked

    @staticmethod
    def list_world_intel(
        *,
        page: int = 1,
        per_page: int = 20,
        status: str | None = None,
        thread_type: str | None = None,
        keyword: str | None = None,
    ) -> dict:
        page = max(1, int(page or 1))
        per_page = min(100, max(1, int(per_page or 20)))
        query = AdventurerS1WorldStoryThread.query
        status = str(status or '').strip()
        thread_type = str(thread_type or '').strip()
        keyword = str(keyword or '').strip()
        if status:
            query = query.filter(AdventurerS1WorldStoryThread.status == status)
        if thread_type:
            query = query.filter(AdventurerS1WorldStoryThread.thread_type == thread_type)
        if keyword:
            like = f'%{keyword}%'
            query = query.filter(
                or_(
                    AdventurerS1WorldStoryThread.title.ilike(like),
                    AdventurerS1WorldStoryThread.base_dungeon.ilike(like),
                    AdventurerS1WorldStoryThread.summary.ilike(like),
                    AdventurerS1WorldStoryThread.next_reveal_hint.ilike(like),
                    AdventurerS1WorldStoryThread.power_profile.ilike(like),
                )
            )
        total = query.count()
        rows = (
            query.order_by(
                AdventurerS1WorldStoryThread.weight.desc(),
                AdventurerS1WorldStoryThread.updated_at.desc(),
            )
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return {
            'items': [row.to_admin_dict() for row in rows],
            'total': total,
            'page': page,
            'per_page': per_page,
            'max_total': S1WorldOutlineService._world_intel_max_total(),
            'thread_types': list(WORLD_INTEL_THREAD_TYPES),
            'statuses': list(WORLD_INTEL_STATUSES),
        }

    @staticmethod
    def _world_intel_max_total(dungeon: str | None = None) -> int:
        from flask import current_app

        dungeon = str(dungeon or '').strip()
        if dungeon:
            return int(_world_story_capacity_snapshot(dungeon)['effective_limit'])
        return max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_BURST_MAX_PER_DUNGEON'),
            30,
        ))

    @staticmethod
    def upsert_world_intel(payload: dict, *, thread_id: int | None = None) -> tuple[dict | None, str | None]:
        title = _clip_text((payload or {}).get('title'), 120)
        summary = _soft_text((payload or {}).get('summary'))
        if not title:
            return None, '缺少情报标题'
        if not summary:
            return None, '缺少情报摘要'
        status = str((payload or {}).get('status') or 'active').strip()
        if status not in WORLD_INTEL_STATUSES:
            return None, '情报状态无效'
        thread_type = str((payload or {}).get('thread_type') or (payload or {}).get('type') or 'event').strip()
        if thread_type not in WORLD_INTEL_THREAD_TYPES:
            return None, '情报类型无效'
        scope_keys = _normalize_text_list((payload or {}).get('scope_keys') or [], limit=8, item_chars=80)
        base_dungeon = _clip_text((payload or {}).get('base_dungeon'), 100)
        if WORLD_INTEL_ALL_DUNGEONS_SCOPE in scope_keys or base_dungeon == WORLD_INTEL_ALL_DUNGEONS_SCOPE:
            scope_keys = [
                WORLD_INTEL_ALL_DUNGEONS_SCOPE,
                *(key for key in scope_keys if key != WORLD_INTEL_ALL_DUNGEONS_SCOPE),
            ][:8]
            base_dungeon = WORLD_INTEL_ALL_DUNGEONS_SCOPE
        trigger_keywords = _normalize_text_list((payload or {}).get('trigger_keywords') or [], limit=10, item_chars=50)
        weight = max(1, min(100, _coerce_int((payload or {}).get('weight'), 50)))
        with db_manager.transaction():
            row = None
            if thread_id:
                row = AdventurerS1WorldStoryThread.query.filter_by(id=int(thread_id)).with_for_update().first()
                if not row:
                    return None, '情报不存在'
                if int(row.campaign_root_id or 0) > 0 or row.campaign_role:
                    return None, '共同战役成员只能在共同战役管理入口修改议程'
            else:
                capacity_dungeon = base_dungeon or (scope_keys[0] if scope_keys else '')
                if capacity_dungeon:
                    max_total = S1WorldOutlineService._world_intel_max_total(capacity_dungeon)
                    if AdventurerS1WorldStoryThread.query.filter(
                        _world_story_head_clause(),
                        AdventurerS1WorldStoryThread.base_dungeon == capacity_dungeon,
                        AdventurerS1WorldStoryThread.status.in_(('seeded', 'active', 'escalated', 'closing', 'dormant')),
                    ).count() >= max_total:
                        return None, (
                            f'{capacity_dungeon} 可触发世界剧情链已达动态入场额度 {max_total} 条，'
                            '请优先收束现有剧情链或建立明确后继关系'
                        )
                row = AdventurerS1WorldStoryThread.query.filter_by(title=title).with_for_update().first()
                if row:
                    return None, '同名情报已存在，请编辑原条目'
                row = AdventurerS1WorldStoryThread(title=title)
                db.session.add(row)
            if row.title != title:
                exists = AdventurerS1WorldStoryThread.query.filter(
                    AdventurerS1WorldStoryThread.title == title,
                    AdventurerS1WorldStoryThread.id != row.id,
                ).first()
                if exists:
                    return None, '同名情报已存在'
            row.title = title
            row.thread_type = thread_type
            row.status = status
            row.base_dungeon = base_dungeon or None
            row.scope_keys = scope_keys
            row.trigger_keywords = trigger_keywords
            row.summary = summary
            row.next_reveal_hint = _soft_text((payload or {}).get('next_reveal_hint'))
            row.power_tier = _clip_text((payload or {}).get('power_tier'), 30)
            row.power_profile = _soft_text((payload or {}).get('power_profile'))
            row.weight = weight
            db.session.flush()
            S1WorldOutlineService._prune_world_story_threads(dungeon=row.base_dungeon)
            return row.to_admin_dict(), None

    @staticmethod
    def delete_world_intel(thread_id: int) -> tuple[dict | None, str | None]:
        thread_id = int(thread_id or 0)
        if thread_id <= 0:
            return None, '情报不存在'
        with db_manager.transaction():
            row = AdventurerS1WorldStoryThread.query.filter_by(id=thread_id).with_for_update().first()
            if not row:
                return None, '情报不存在'
            if int(row.campaign_root_id or 0) > 0 or row.campaign_role:
                return None, '共同战役成员不可从普通世界情报入口删除'
            data = {'deleted': True, 'id': row.id, 'title': row.title}
            db.session.delete(row)
            return data, None

    @staticmethod
    def preview_world_intel_injection(
        target_dungeon: str,
        *,
        limit: int | None = None,
        context_text: str | None = None,
        context_keywords=None,
    ) -> dict:
        dungeon = str(target_dungeon or '').strip()
        search_text, anchor_terms = _build_world_intel_context(dungeon, context_text, context_keywords)
        if not search_text:
            return {'target_dungeon': '', 'items': [], 'prompt_block': ''}
        if limit is None:
            from flask import current_app

            limit = max(0, _coerce_int(
                current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_PROMPT_LIMIT', 1),
                1,
            ))
        picked = S1WorldOutlineService._pick_world_story_threads_for_prompt(
            dungeon,
            limit=max(0, int(limit or 0)),
            context_text=search_text,
            context_keywords=anchor_terms,
        )
        prompt_block = S1WorldOutlineService.build_world_story_threads_prompt_block(
            dungeon,
            context_text=search_text,
            context_keywords=anchor_terms,
        ) if picked else ''
        hot_terms = set(S1WorldOutlineService.get_motif_cooldown_terms())
        items = []
        for row in picked:
            data = row.to_admin_dict()
            data.update(S1WorldOutlineService._explain_world_story_thread_match(
                row,
                dungeon,
                search_text,
                anchor_terms,
                hot_terms,
            ))
            items.append(data)
        return {
            'target_dungeon': dungeon,
            'items': items,
            'prompt_block': prompt_block,
            'limit': int(limit or 0),
            'context_keywords': anchor_terms,
            'context_text': search_text,
        }

    @staticmethod
    def list_candidates(
        *,
        page: int = 1,
        per_page: int = 20,
        status: str | None = None,
        target_dungeon: str | None = None,
        issue_code: str | None = None,
    ) -> dict:
        page = max(1, int(page or 1))
        per_page = min(100, max(1, int(per_page or 20)))
        query = AdventurerS1WorldFactCandidate.query
        if status:
            query = query.filter(AdventurerS1WorldFactCandidate.status == str(status).strip())
        if target_dungeon:
            query = query.filter(AdventurerS1WorldFactCandidate.target_dungeon == str(target_dungeon).strip())
        if issue_code:
            query = query.filter(
                AdventurerS1WorldFactCandidate.maintenance_issue_code
                == str(issue_code).strip()
            )
        total = query.count()
        rows = (
            query.order_by(AdventurerS1WorldFactCandidate.created_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return {
            'items': [row.to_admin_dict() for row in rows],
            'total': total,
            'page': page,
            'per_page': per_page,
        }

    @staticmethod
    def list_handoff_states(*, page: int = 1, per_page: int = 20) -> dict:
        page = max(1, int(page or 1))
        per_page = min(100, max(1, int(per_page or 20)))
        query = AdventurerS1DmHandoffState.query
        total = query.count()
        rows = (
            query.order_by(AdventurerS1DmHandoffState.updated_at.desc())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )
        return {
            'items': [row.to_admin_dict() for row in rows],
            'total': total,
            'page': page,
            'per_page': per_page,
        }

    @staticmethod
    def delete_candidate(candidate_id: int) -> tuple[dict | None, str | None]:
        candidate_id = int(candidate_id or 0)
        if candidate_id <= 0:
            return None, '候选不存在'
        with db_manager.transaction():
            row = AdventurerS1WorldFactCandidate.query.filter_by(id=candidate_id).with_for_update().first()
            if not row:
                return None, '候选不存在'
            from db_repository.s1_campaign_dispatch_repository import S1CampaignDispatchRepository
            if S1CampaignDispatchRepository.log_is_occupied(row.expedition_log_id):
                return None, '这份战报仍在推进共同任务，请重试世界维护后再处理'
            data = {'deleted': True, 'id': row.id, 'target_dungeon': row.target_dungeon}
            db.session.delete(row)
            return data, None

    @staticmethod
    def delete_handoff_state(state_id: int) -> tuple[dict | None, str | None]:
        state_id = int(state_id or 0)
        if state_id <= 0:
            return None, '接力便签不存在'
        with db_manager.transaction():
            row = AdventurerS1DmHandoffState.query.filter_by(id=state_id).with_for_update().first()
            if not row:
                return None, '接力便签不存在'
            data = {'deleted': True, 'id': row.id, 'target_dungeon': row.target_dungeon}
            db.session.delete(row)
            return data, None

    @staticmethod
    def _claim_closed_dungeon_reseed_scans(*, limit: int, now: datetime) -> list[str]:
        """领取确定不可探索且没有待处理连续性工作的地牢；本步骤不调用 AI。"""
        from flask import current_app

        states = (
            AdventurerS1WorldOutlineDungeonState.query
            .filter(
                AdventurerS1WorldOutlineDungeonState.target_dungeon.in_(
                    tuple(S1_ACTIVE_DUNGEON_NAMES),
                ),
                AdventurerS1WorldOutlineDungeonState.is_explorable.is_(False),
                or_(
                    AdventurerS1WorldOutlineDungeonState.cycle_reseed_retry_after.is_(None),
                    AdventurerS1WorldOutlineDungeonState.cycle_reseed_retry_after <= now,
                ),
                or_(
                    AdventurerS1WorldOutlineDungeonState.maintenance_claim_token.is_(None),
                    AdventurerS1WorldOutlineDungeonState.maintenance_claim_expires_at.is_(None),
                    AdventurerS1WorldOutlineDungeonState.maintenance_claim_expires_at <= now,
                ),
            )
            .order_by(
                AdventurerS1WorldOutlineDungeonState.updated_at.asc(),
                AdventurerS1WorldOutlineDungeonState.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(max(1, int(limit or 1)))
            .all()
        )
        if not states:
            return []
        dungeons = [row.target_dungeon for row in states]
        heads = (
            AdventurerS1WorldStoryThread.query
            .filter(
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.base_dungeon.in_(dungeons),
            )
            .all()
        )
        head_statuses: dict[str, set[str]] = {dungeon: set() for dungeon in dungeons}
        for head in heads:
            base_dungeon = _effective_world_story_base_dungeon(head)
            if base_dungeon in head_statuses:
                head_statuses[base_dungeon].add(str(head.status or '').strip())
        busy_dungeons = {
            str(value)
            for (value,) in (
                db.session.query(AdventurerS1WorldFactCandidate.target_dungeon)
                .filter(
                    AdventurerS1WorldFactCandidate.target_dungeon.in_(dungeons),
                    AdventurerS1WorldFactCandidate.status.in_((
                        'awaiting_completion', 'pending', 'processing',
                    )),
                )
                .distinct()
                .all()
            )
        }
        eligible = [
            row
            for row in states
            if (
                row.target_dungeon not in busy_dungeons
                and (
                    head_statuses.get(row.target_dungeon, set())
                    & WORLD_INTEL_TERMINAL_STATUSES
                )
                and not (
                    head_statuses.get(row.target_dungeon, set())
                    & WORLD_INTEL_MUTABLE_STATUSES
                )
            )
        ]
        retry_hours = max(1, int(current_app.config.get(
            'ADVENTURER_S1_WORLD_CYCLE_RESEED_RETRY_HOURS', 24,
        ) or 24))
        for row in eligible:
            row.cycle_reseed_retry_after = now + timedelta(hours=retry_hours)
            row.updated_at = now
            _record_cycle_reseed_result(
                row,
                'claimed',
                attempted_at=now,
                stage='claim',
            )
        return [row.target_dungeon for row in eligible]

    @staticmethod
    def _build_ecology_debt_handoff_fallback(
        dungeon: str,
        candidate_ids: list[int],
    ) -> dict:
        """Recover an explicitly marked ecology failure without another AI call."""
        candidates = (
            AdventurerS1WorldFactCandidate.query
            .filter(AdventurerS1WorldFactCandidate.id.in_(candidate_ids))
            .order_by(AdventurerS1WorldFactCandidate.created_at.asc())
            .all()
        )
        if len(candidates) != len(candidate_ids):
            raise ValueError('生态债务回执恢复候选不完整')
        log_ids = [int(row.expedition_log_id) for row in candidates]
        logs = (
            AdventurerS1ExpeditionLog.query
            .with_entities(AdventurerS1ExpeditionLog.id, AdventurerS1ExpeditionLog.full_log)
            .filter(AdventurerS1ExpeditionLog.id.in_(log_ids))
            .all()
        )
        logs_by_id = {int(log_id): full_log for log_id, full_log in logs}
        debt_notes: list[str] = []
        for candidate in candidates:
            full_log = logs_by_id.get(int(candidate.expedition_log_id))
            debt = _normalize_ecology_continuity_debt(
                full_log.get('ecology_continuity_debt')
                if isinstance(full_log, dict) else None
            )
            note = _soft_text(candidate.dm_handoff_note_candidate)
            if not debt or not note:
                raise ValueError('生态债务回执恢复缺少精确债务或非空接力便签')
            debt_notes.append(note)
        current_state = AdventurerS1DmHandoffState.query.filter_by(
            target_dungeon=dungeon,
        ).first()
        note = _merge_dm_handoff_notes(
            getattr(current_state, 'note', ''),
            *debt_notes,
        )
        if not note:
            raise ValueError('生态债务回执恢复未形成非空接力便签')
        return {
            'dm_handoff_note': note,
            'world_story_threads': [],
            'world_actors': [],
            '_diagnostics': [
                ECOLOGY_CONTINUITY_DEBT_HANDOFF_FALLBACK_DIAGNOSTIC,
            ],
        }

    @staticmethod
    def process_pending_candidates(
        limit: int | None = None,
        *,
        restart_recovered: bool = False,
    ) -> dict:
        """异步批量维护入口。失败保存回执并释放战役占用，不影响探险结算。"""
        from flask import current_app

        batch_limit = max(1, int(limit or current_app.config.get('ADVENTURER_S1_WORLD_OUTLINE_BATCH_SIZE', 10)))
        ai_timeout = max(60, int(current_app.config.get(
            'ADVENTURER_S1_WORLD_OUTLINE_AI_TIMEOUT_SECONDS', 600,
        )))
        configured_stale_seconds = max(60, int(current_app.config.get(
            'ADVENTURER_S1_WORLD_OUTLINE_PROCESSING_STALE_SECONDS', 1800,
        )))
        ecology_recovery_max_attempts = max(0, int(current_app.config.get(
            'ADVENTURER_S1_WORLD_OUTLINE_ECOLOGY_RECOVERY_MAX_ATTEMPTS', 1,
        ) or 0))
        max_ai_attempts, _, retry_max_wait = _world_outline_ai_retry_config()
        # 一次维护可能包含主调用 + 一次结构修复，两段请求都允许有限网络重试；
        # 租约不得在合法调用或退避仍进行时被另一 worker 当成陈旧任务抢走。
        retry_wait_budget = retry_max_wait * max(0, max_ai_attempts - 1) * 2
        stale_seconds = max(
            configured_stale_seconds,
            ai_timeout * max_ai_attempts * 2 + retry_wait_budget + 120,
        )
        claim_time = _portable_utc_now()
        heartbeat_seconds, lease_seconds = maintenance_lease_settings()
        claim_token = f'__lease__:{uuid.uuid4().hex}'
        stale_before = claim_time - timedelta(seconds=stale_seconds)
        # Reconcile terminal failures left by older deployments. This short
        # transaction also runs when there are no pending maintenance candidates.
        with db_manager.transaction():
            campaign_publication_released = S1CampaignDispatchRepository.release_failed_publications(
                limit=batch_limit, now=claim_time,
            )
        with db_manager.transaction():
            processing_query = AdventurerS1WorldFactCandidate.query.filter(
                AdventurerS1WorldFactCandidate.status == 'processing',
                # A restart hint must not invalidate an active dungeon owner.
                # This also protects old-format leases during a rolling update.
                ~maintenance_claim_exists(claim_time),
            )
            if not restart_recovered:
                processing_query = processing_query.filter(
                    or_(
                        AdventurerS1WorldFactCandidate.updated_at < stale_before,
                        maintenance_claim_exists(claim_time, expired=True),
                    ),
                )
            stale_rows = (
                processing_query
                .order_by(AdventurerS1WorldFactCandidate.updated_at.asc())
                .with_for_update(skip_locked=True)
                .limit(batch_limit)
                .all()
            )
            stale_requeued_count = 0
            stale_recovery_interrupted_count = 0
            for row in stale_rows:
                row.updated_at = claim_time
                if (
                    row.maintenance_recovery_kind
                    == WORLD_OUTLINE_ECOLOGY_RECOVERY_KIND
                    and int(row.maintenance_recovery_attempts or 0) > 0
                ):
                    previous_receipt = (
                        dict(row.maintenance_receipt)
                        if isinstance(row.maintenance_receipt, dict) else {}
                    )
                    previous_receipt['recovery_interruption'] = {
                        'issue_code': (
                            WORLD_OUTLINE_ECOLOGY_RECOVERY_INTERRUPTED_ISSUE_CODE
                        ),
                        'recorded_at': claim_time.isoformat(),
                        'attempt_number': int(
                            row.maintenance_recovery_attempts or 0
                        ),
                    }
                    row.status = 'failed'
                    row.processed_at = claim_time
                    row.maintenance_issue_code = (
                        WORLD_OUTLINE_ECOLOGY_RECOVERY_INTERRUPTED_ISSUE_CODE
                    )
                    row.maintenance_receipt = previous_receipt
                    row.maintenance_recovery_kind = None
                    row.outline_error = (
                        '[ecology_debt_recovery_interrupted] '
                        '确定性生态债务恢复租约过期，未重新调用维护 AI'
                    )
                    stale_recovery_interrupted_count += 1
                    logger.warning(
                        'S1_WORLD_OUTLINE_ECOLOGY_RECOVERY_INTERRUPTED '
                        'candidate=%s log=%s dungeon=%s attempt=%s '
                        'source_batch=%s',
                        row.id,
                        row.expedition_log_id,
                        row.target_dungeon,
                        row.maintenance_recovery_attempts,
                        previous_receipt.get('batch_id'),
                    )
                else:
                    row.status = 'pending'
                    row.processed_at = None
                    row.outline_error = (
                        '上次维护进程重启中断，已自动重新排队'
                        if restart_recovered
                        else '上次维护进程超时，已自动重新排队'
                    )
                    stale_requeued_count += 1
            receipt_fallback_rows: list[AdventurerS1WorldFactCandidate] = []
            failed_receipt_rows = (
                AdventurerS1WorldFactCandidate.query
                .filter(
                    AdventurerS1WorldFactCandidate.status == 'failed',
                    AdventurerS1WorldFactCandidate.maintenance_recovery_attempts
                    < ecology_recovery_max_attempts,
                    AdventurerS1WorldFactCandidate.dm_handoff_note_candidate.isnot(None),
                    AdventurerS1WorldFactCandidate.dm_handoff_note_candidate != '',
                    or_(
                        AdventurerS1WorldFactCandidate.outline_error
                        == ECOLOGY_CONTINUITY_DEBT_RECEIPT_ERROR,
                        and_(
                            AdventurerS1WorldFactCandidate.maintenance_recovery_kind
                            == WORLD_OUTLINE_ECOLOGY_RECOVERY_KIND,
                            AdventurerS1WorldFactCandidate.maintenance_receipt.isnot(None),
                            AdventurerS1WorldFactCandidate.maintenance_issue_code.in_(
                                WORLD_OUTLINE_RECOVERABLE_ECOLOGY_ISSUE_CODES,
                            ),
                        ),
                    ),
                )
                .order_by(AdventurerS1WorldFactCandidate.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(batch_limit)
                .all()
            )
            failed_log_ids = [
                int(row.expedition_log_id) for row in failed_receipt_rows
            ]
            failed_logs = (
                AdventurerS1ExpeditionLog.query
                .with_entities(
                    AdventurerS1ExpeditionLog.id,
                    AdventurerS1ExpeditionLog.full_log,
                )
                .filter(AdventurerS1ExpeditionLog.id.in_(failed_log_ids))
                .all()
                if failed_log_ids else []
            )
            failed_logs_by_id = {
                int(log_id): full_log for log_id, full_log in failed_logs
            }
            receipt_fallback_rows = [
                row for row in failed_receipt_rows
                if (
                    row.outline_error == ECOLOGY_CONTINUITY_DEBT_RECEIPT_ERROR
                    or (
                        isinstance(row.maintenance_receipt, dict)
                        and _coerce_int(
                            row.maintenance_receipt.get('schema_version'), 0,
                        ) == WORLD_OUTLINE_MAINTENANCE_RECEIPT_SCHEMA_VERSION
                        and row.maintenance_recovery_kind
                        == WORLD_OUTLINE_ECOLOGY_RECOVERY_KIND
                        and row.maintenance_issue_code
                        in WORLD_OUTLINE_RECOVERABLE_ECOLOGY_ISSUE_CODES
                    )
                )
                and (
                    _normalize_ecology_continuity_debt(
                        failed_logs_by_id[int(row.expedition_log_id)].get(
                            'ecology_continuity_debt'
                        )
                    )
                    if isinstance(
                        failed_logs_by_id.get(int(row.expedition_log_id)),
                        dict,
                    )
                    else {}
                )
                and bool(_soft_text(row.dm_handoff_note_candidate))
            ]
            receipt_fallback_original_errors = {
                int(row.id): row.outline_error for row in receipt_fallback_rows
            }
            receipt_fallback_sources = []
            for row in receipt_fallback_rows:
                source_receipt = (
                    row.maintenance_receipt
                    if isinstance(row.maintenance_receipt, dict) else {}
                )
                source_error = source_receipt.get('error')
                source_issue_codes = source_receipt.get('issue_codes')
                if not isinstance(source_issue_codes, list):
                    source_issue_codes = []
                source_rejections = source_receipt.get(
                    'rejected_world_story_threads'
                )
                if not isinstance(source_rejections, list):
                    source_rejections = []
                receipt_fallback_sources.append({
                    'candidate_id': int(row.id),
                    'source_batch_id': _clip_text(
                        source_receipt.get('batch_id'), 64,
                    ),
                    'source_issue_code': _clip_text(
                        row.maintenance_issue_code, 64,
                    ),
                    'source_issue_codes': [
                        _clip_text(value, 64)
                        for value in source_issue_codes[:20]
                    ],
                    'source_status': _clip_text(
                        source_receipt.get('status'), 32,
                    ),
                    'source_stage': _clip_text(
                        source_receipt.get('stage'), 64,
                    ),
                    'source_proposed_world_story_thread_count': _coerce_int(
                        source_receipt.get(
                            'proposed_world_story_thread_count'
                        ),
                        0,
                    ),
                    'source_applied_world_story_thread_count': _coerce_int(
                        source_receipt.get(
                            'applied_world_story_thread_count'
                        ),
                        0,
                    ),
                    'source_rejected_world_story_thread_count': _coerce_int(
                        source_receipt.get(
                            'rejected_world_story_thread_count'
                        ),
                        0,
                    ),
                    'source_rejected_world_story_threads': [
                        dict(item)
                        for item in source_rejections[:50]
                        if isinstance(item, dict)
                    ],
                    'source_error': (
                        {
                            'type': _clip_text(source_error.get('type'), 120),
                            'message': _clip_text(
                                source_error.get('message'), 1000,
                            ),
                        }
                        if isinstance(source_error, dict) else None
                    ),
                    'source_outline_error': _clip_text(
                        row.outline_error, 1000,
                    ),
                })
            receipt_fallback_dungeons = {
                str(row.target_dungeon) for row in receipt_fallback_rows
            }
            pending_capacity = max(0, batch_limit - len(receipt_fallback_rows))
            pending_query = AdventurerS1WorldFactCandidate.query.filter_by(
                status='pending'
            )
            if receipt_fallback_dungeons:
                # A deterministic debt recovery must never be folded into a
                # normal AI batch for the same dungeon.
                pending_query = pending_query.filter(
                    AdventurerS1WorldFactCandidate.target_dungeon.notin_(
                        receipt_fallback_dungeons
                    )
                )
            pending_rows = (
                pending_query
                .order_by(AdventurerS1WorldFactCandidate.created_at.asc())
                .with_for_update(skip_locked=True)
                .limit(pending_capacity)
                .all()
                if pending_capacity else []
            )
            rows = [*receipt_fallback_rows, *pending_rows]
            for row in pending_rows:
                if row.maintenance_recovery_kind != CAMPAIGN_PUBLICATION_RECOVERY_KIND:
                    continue
                source_receipt = row.maintenance_receipt or {}
                receipt_fallback_sources.append({
                    'candidate_id': int(row.id),
                    'source_batch_id': source_receipt.get('batch_id'),
                    'source_issue_code': row.maintenance_issue_code,
                    'source_stage': source_receipt.get('stage'),
                    'source_status': source_receipt.get('status'),
                    'source_error': source_receipt.get('error'),
                    'recovery_kind': CAMPAIGN_PUBLICATION_RECOVERY_KIND,
                    'attempt_number': int(row.maintenance_recovery_attempts or 0),
                })
            claimed_rows = [
                (int(row.id), row.target_dungeon, int(row.expedition_log_id))
                for row in rows
            ]
            receipt_fallback_candidate_ids = {
                int(row.id) for row in receipt_fallback_rows
            }
            claimed_log_ids = {
                int(expedition_log_id)
                for _candidate_id, _dungeon, expedition_log_id in claimed_rows
            }
            claimed_log_rows = (
                AdventurerS1ExpeditionLog.query
                .with_entities(
                    AdventurerS1ExpeditionLog.id,
                    AdventurerS1ExpeditionLog.full_log,
                )
                .filter(AdventurerS1ExpeditionLog.id.in_(claimed_log_ids))
                .all()
                if claimed_log_ids else []
            )
            claimed_full_logs_by_id = {
                int(log_id): full_log for log_id, full_log in claimed_log_rows
            }
            ecology_debt_candidate_ids = {
                int(candidate_id)
                for candidate_id, _dungeon, expedition_log_id in claimed_rows
                if (
                    _normalize_ecology_continuity_debt(
                        claimed_full_logs_by_id[int(expedition_log_id)].get(
                            'ecology_continuity_debt'
                        )
                    )
                    if isinstance(
                        claimed_full_logs_by_id.get(int(expedition_log_id)),
                        dict,
                    )
                    else {}
                )
            }
            for row in rows:
                row.status = 'processing'
                row.processed_at = None
                row.updated_at = claim_time
                row.outline_error = claim_token
                if int(row.id) in receipt_fallback_candidate_ids:
                    row.maintenance_recovery_attempts = int(
                        row.maintenance_recovery_attempts or 0
                    ) + 1
            cycle_reseed_dungeons = S1WorldOutlineService._claim_closed_dungeon_reseed_scans(
                limit=batch_limit,
                now=claim_time,
            )

        successful_ecology_recoveries = 0
        empty_result = {
            'processed': 0,
            'processed_groups': 0,
            'updated_notes': 0,
            'failed': stale_recovery_interrupted_count,
            'rejected': 0,
            'warnings': stale_recovery_interrupted_count,
            'recovered': stale_requeued_count,
            'campaign_publication_released': campaign_publication_released,
            'lease_lost': 0,
            'deferred': 0,
            'world_actors': {'created': 0, 'updated': 0, 'interactions': 0},
            'faction_profiles': {
                'created': 0, 'updated': 0, 'linked': 0,
                'history_rows': 0, 'skipped': 0,
            },
            'world_story_threads': {
                'proposed': 0, 'applied': 0, 'rejected': 0,
            },
            'cycle_reseed_scanned': len(cycle_reseed_dungeons),
        }
        if not claimed_rows and not cycle_reseed_dungeons:
            return empty_result

        grouped_ids: dict[str, list[int]] = {}
        last_log_by_dungeon: dict[str, int | None] = {}
        expedition_log_by_candidate_id: dict[int, int] = {}
        for candidate_id, dungeon, expedition_log_id in claimed_rows:
            grouped_ids.setdefault(dungeon, []).append(candidate_id)
            last_log_by_dungeon[dungeon] = expedition_log_id
            expedition_log_by_candidate_id[candidate_id] = expedition_log_id
        cycle_reseed_only_dungeons = {
            dungeon
            for dungeon in cycle_reseed_dungeons
            if dungeon not in grouped_ids
        }
        for dungeon in cycle_reseed_only_dungeons:
            grouped_ids[dungeon] = []
            last_log_by_dungeon[dungeon] = None
        # 释放连接；后续 AI 调用可能耗时数分钟，不能占用 checkout 中的 PG 连接。
        db_manager.remove()

        processed = 0
        processed_groups = 0
        failed = stale_recovery_interrupted_count
        rejected = 0
        updated_notes = 0
        warning_count = stale_recovery_interrupted_count
        lease_lost = 0
        deferred = 0
        actor_updates = {'created': 0, 'updated': 0, 'interactions': 0}
        faction_profile_updates = {
            'created': 0, 'updated': 0, 'linked': 0,
            'history_rows': 0, 'skipped': 0,
        }
        world_story_updates = {'proposed': 0, 'applied': 0, 'rejected': 0}
        missing_profile_actor_ids: list[int] = []
        for dungeon, candidate_ids in grouped_ids.items():
            usage_handles: list[_DeferredWorldOutlineTokenUsage] = []
            maintenance_batch_id = uuid.uuid4().hex
            batch_expedition_log_ids = [
                expedition_log_by_candidate_id[candidate_id]
                for candidate_id in candidate_ids
                if candidate_id in expedition_log_by_candidate_id
            ]
            batch_has_ecology_debt = any(
                candidate_id in ecology_debt_candidate_ids
                for candidate_id in candidate_ids
            )
            is_ecology_recovery = bool(
                candidate_ids
                and set(candidate_ids).issubset(
                    receipt_fallback_candidate_ids
                )
            )
            batch_recovery_sources = [
                dict(item) for item in receipt_fallback_sources
                if _coerce_int(item.get('candidate_id'), 0) in candidate_ids
            ]
            dungeon_claim_token = claim_token
            dungeon_revision = 0
            with db_manager.transaction():
                state = _ensure_world_outline_dungeon_state(dungeon)
                now = _portable_utc_now()
                locked = (
                    AdventurerS1WorldFactCandidate.query.filter(
                        AdventurerS1WorldFactCandidate.id.in_(candidate_ids),
                        AdventurerS1WorldFactCandidate.target_dungeon == dungeon,
                        AdventurerS1WorldFactCandidate.status == 'processing',
                        AdventurerS1WorldFactCandidate.outline_error == claim_token,
                    ).with_for_update().populate_existing().all()
                    if candidate_ids else []
                )
                if len(locked) != len(candidate_ids):
                    # A preceding dungeon may have taken a long time. A batch
                    # that lost candidates must not create an orphan map lease
                    # or spend another AI request on those candidates.
                    for row in locked:
                        is_recovery_row = int(row.id) in receipt_fallback_candidate_ids
                        row.status = 'failed' if is_recovery_row else 'pending'
                        row.processed_at = None
                        row.updated_at = now
                        if is_recovery_row:
                            row.maintenance_recovery_attempts = max(
                                0, int(row.maintenance_recovery_attempts or 0) - 1,
                            )
                            row.outline_error = receipt_fallback_original_errors.get(int(row.id))
                        else:
                            row.outline_error = '维护领取已失效，剩余候选已自动重新排队'
                    lease_lost += len(candidate_ids)
                    continue
                if dungeon in cycle_reseed_only_dungeons:
                    head_statuses = {
                        str(value)
                        for (value,) in (
                            db.session.query(AdventurerS1WorldStoryThread.status)
                            .filter(
                                _world_story_head_clause(),
                                AdventurerS1WorldStoryThread.base_dungeon == dungeon,
                            )
                            .all()
                        )
                    }
                    has_busy_candidate = (
                        AdventurerS1WorldFactCandidate.query
                        .filter(
                            AdventurerS1WorldFactCandidate.target_dungeon == dungeon,
                            AdventurerS1WorldFactCandidate.status.in_((
                                'awaiting_completion', 'pending', 'processing',
                            )),
                        )
                        .first()
                        is not None
                    )
                    if (
                        state.is_explorable is not False
                        or not (head_statuses & WORLD_INTEL_TERMINAL_STATUSES)
                        or head_statuses & WORLD_INTEL_MUTABLE_STATUSES
                        or has_busy_candidate
                    ):
                        continue
                claim_is_live = bool(
                    state.maintenance_claim_token
                    and state.maintenance_claim_expires_at
                    and state.maintenance_claim_expires_at > now
                )
                if claim_is_live:
                    for row in locked:
                        is_recovery_row = int(row.id) in (
                            receipt_fallback_candidate_ids
                        )
                        row.status = 'failed' if is_recovery_row else 'pending'
                        row.processed_at = None
                        row.updated_at = now
                        if is_recovery_row:
                            row.maintenance_recovery_attempts = max(
                                0,
                                int(row.maintenance_recovery_attempts or 0) - 1,
                            )
                            row.outline_error = (
                                receipt_fallback_original_errors.get(int(row.id))
                                or '生态债务确定性恢复因同地牢维护运行而延期'
                            )
                        else:
                            row.outline_error = '同地牢已有世界大纲维护运行，已自动重新排队'
                    deferred += len(locked)
                    continue
                state.maintenance_claim_token = dungeon_claim_token
                state.maintenance_claim_expires_at = now + timedelta(seconds=lease_seconds)
                state.updated_at = now
                dungeon_revision = int(state.outline_revision or 0)
                for row in locked:
                    row.updated_at = now
            maintenance_error = None
            maintained = None
            try:
                with maintenance_heartbeat(
                    heartbeat_seconds=heartbeat_seconds,
                    dungeon=dungeon, candidate_ids=candidate_ids,
                    candidate_token=claim_token, dungeon_token=dungeon_claim_token,
                    outline_revision=dungeon_revision, lease_seconds=lease_seconds,
                ):
                    if is_ecology_recovery:
                        maintained = (
                            S1WorldOutlineService._build_ecology_debt_handoff_fallback(
                                dungeon,
                                candidate_ids,
                            )
                        )
                        db_manager.remove()
                    else:
                        maintained = S1WorldOutlineService._maintain_one_dungeon(
                            dungeon,
                            candidate_ids,
                            cycle_reseed_only=dungeon in cycle_reseed_only_dungeons,
                            usage_handles=usage_handles,
                            usage_context={
                                'correlation_id': maintenance_batch_id,
                                'expedition_log_id': (
                                    batch_expedition_log_ids[0]
                                    if len(batch_expedition_log_ids) == 1 else None
                                ),
                            },
                        )
            except Exception as exc:  # noqa: BLE001 - AI failure must not break scheduler
                maintenance_error = exc
            learning_failure = (
                maintenance_error
                if isinstance(maintenance_error, FirstEncounterLearningError) else None
            )
            review_rejected = (
                maintenance_error is None
                and isinstance(maintained, _WorldLocationMaintenanceRejection)
            )
            if maintenance_error is not None or review_rejected:
                issue_code = (
                    WORLD_LOCATION_MAINTENANCE_REJECTED if review_rejected else (
                        'ecology_debt_recovery_failed' if is_ecology_recovery
                        else _classify_world_outline_maintenance_failure(maintenance_error)
                    )
                )
                diagnostics = list(maintained.diagnostics) if review_rejected else []
                receipt = _build_world_outline_maintenance_receipt(
                    batch_id=maintenance_batch_id,
                    dungeon=dungeon,
                    candidate_ids=candidate_ids,
                    expedition_log_ids=batch_expedition_log_ids,
                    status='rejected' if review_rejected else 'failed',
                    stage=(
                        'maintenance_review' if review_rejected else (
                            'ecology_handoff_recovery'
                            if is_ecology_recovery else (
                                learning_failure.failure_stage
                                if learning_failure and learning_failure.failure_stage
                                else 'maintenance_ai'
                            )
                        )
                    ),
                    issue_codes=[issue_code],
                    diagnostics=diagnostics,
                    ecology_debt_expected=batch_has_ecology_debt,
                    recovery_sources=batch_recovery_sources,
                    error=maintenance_error,
                )
                if maintenance_error is not None:
                    logger.warning(
                        'S1 大纲维护失败 batch=%s dungeon=%s code=%s '
                        'stage=%s upstream_code=%s: %s',
                        maintenance_batch_id,
                        dungeon,
                        issue_code,
                        receipt['stage'],
                        (
                            learning_failure.upstream_failure.get('validation_error_code', 'none')
                            if learning_failure else 'none'
                        ),
                        maintenance_error,
                        exc_info=(
                            None if learning_failure and learning_failure.error_code else (
                                type(maintenance_error), maintenance_error,
                                maintenance_error.__traceback__,
                            )
                        ),
                    )
                receipt_persisted = False
                try:
                    failed_row_count = 0
                    with db_manager.transaction():
                        state = AdventurerS1WorldOutlineDungeonState.query.filter_by(
                            target_dungeon=dungeon,
                        ).with_for_update().first()
                        if state and state.maintenance_claim_token == dungeon_claim_token:
                            state.maintenance_claim_token = None
                            state.maintenance_claim_expires_at = None
                            if dungeon in cycle_reseed_only_dungeons:
                                _record_cycle_reseed_result(
                                    state,
                                    'failed',
                                    attempted_at=state.cycle_reseed_last_attempted_at or claim_time,
                                    stage='maintenance_or_review',
                                    reason=f'{type(maintenance_error).__name__}:{str(maintenance_error)[:900]}',
                                )
                        locked = AdventurerS1WorldFactCandidate.query.filter(
                            AdventurerS1WorldFactCandidate.id.in_(candidate_ids),
                            AdventurerS1WorldFactCandidate.status == 'processing',
                            AdventurerS1WorldFactCandidate.outline_error == claim_token,
                        ).with_for_update().all()
                        failed_row_count = len(locked)
                        for row in locked:
                            # Keep the existing terminal candidate status so publication
                            # release and settled ecology-debt recovery retain ownership.
                            row.status = 'failed'
                            row.maintenance_issue_code = issue_code
                            row.maintenance_receipt = dict(receipt)
                            row.maintenance_recovery_kind = (
                                WORLD_OUTLINE_ECOLOGY_RECOVERY_KIND
                                if (
                                    not is_ecology_recovery
                                    and int(row.id) in ecology_debt_candidate_ids
                                )
                                else None
                            )
                            row.outline_error = (
                                f'[{issue_code}] receipt={maintenance_batch_id}: '
                                f'{("; ".join(diagnostics) if review_rejected else str(maintenance_error))[:850]}'
                            )
                            row.processed_at = _portable_utc_now()
                        released_count = S1CampaignDispatchRepository.release_failed_publications(
                            candidate_ids=[row.id for row in locked],
                            limit=batch_limit, now=_portable_utc_now(),
                        )
                    receipt_persisted = True
                    campaign_publication_released += released_count
                    if review_rejected:
                        rejected += failed_row_count
                    else:
                        failed += failed_row_count
                    _log_world_outline_maintenance_receipt(receipt)
                except Exception as record_exc:  # noqa: BLE001 - leave stale recovery available
                    logger.error(
                        'S1_WORLD_OUTLINE_MAINTENANCE_RECEIPT_PERSIST_FAILED '
                        'batch=%s dungeon=%s candidates=%s logs=%s '
                        'original_code=%s error=%s',
                        maintenance_batch_id,
                        dungeon,
                        candidate_ids,
                        batch_expedition_log_ids,
                        issue_code,
                        record_exc,
                        exc_info=True,
                    )
                    db_manager.remove()
                _finalize_world_outline_usage_handles(
                    usage_handles, False,
                    receipt=receipt if receipt_persisted else None,
                    failure_stage=None if receipt_persisted else 'consumer_transaction',
                    validation_error_code=None if receipt_persisted else 'consumer_transaction_failed',
                )
                continue
            committed_receipt: dict | None = None
            processed_before_consumer = processed
            processed_groups_before_consumer = processed_groups
            failed_before_consumer = failed
            updated_notes_before_consumer = updated_notes
            warning_count_before_consumer = warning_count
            actor_updates_before_consumer = dict(actor_updates)
            faction_updates_before_consumer = dict(faction_profile_updates)
            world_story_updates_before_consumer = dict(world_story_updates)
            missing_profile_count_before_consumer = len(
                missing_profile_actor_ids
            )
            upsert_result = _new_world_story_upsert_result([])
            diagnostics: list = []
            with _world_outline_consumer_transaction(usage_handles) as usage_outcome:
                aggregate_state = AdventurerS1WorldOutlineDungeonState.query.filter_by(
                    target_dungeon=dungeon,
                ).with_for_update().populate_existing().first()
                locked = AdventurerS1WorldFactCandidate.query.filter(
                    AdventurerS1WorldFactCandidate.id.in_(candidate_ids),
                    AdventurerS1WorldFactCandidate.status == 'processing',
                    AdventurerS1WorldFactCandidate.outline_error == claim_token,
                ).with_for_update().all()
                aggregate_lease_valid = bool(
                    aggregate_state
                    and aggregate_state.maintenance_claim_token == dungeon_claim_token
                    and int(aggregate_state.outline_revision or 0) == dungeon_revision
                    and aggregate_state.maintenance_claim_expires_at
                    and aggregate_state.maintenance_claim_expires_at > _portable_utc_now()
                )
                if len(locked) != len(candidate_ids) or not aggregate_lease_valid:
                    logger.warning(
                        'S1 大纲维护结果因领取租约或地牢版本已失效而重新排队 dungeon=%s candidates=%s',
                        dungeon,
                        candidate_ids,
                    )
                    now = _portable_utc_now()
                    for row in locked:
                        row.status = 'failed' if is_ecology_recovery else 'pending'
                        row.processed_at = None
                        row.updated_at = now
                        if is_ecology_recovery:
                            row.maintenance_recovery_attempts = max(
                                0,
                                int(row.maintenance_recovery_attempts or 0) - 1,
                            )
                            row.outline_error = (
                                receipt_fallback_original_errors.get(int(row.id))
                                or '生态债务确定性恢复因维护版本失效而延期'
                            )
                        else:
                            row.outline_error = '维护结果版本已过期，已自动重新排队'
                    if aggregate_state and aggregate_state.maintenance_claim_token == dungeon_claim_token:
                        aggregate_state.maintenance_claim_token = None
                        aggregate_state.maintenance_claim_expires_at = None
                    # The whole claimed group lost ownership, even when another worker has
                    # already replaced every per-candidate token and ``locked`` is empty.
                    lease_lost += len(candidate_ids)
                    # Commit before the control-flow jump so a commit failure is
                    # raised inside the yielded body and reaches the receipt path.
                    db.session.commit()
                    continue
                cycle_reseed_only = dungeon in cycle_reseed_only_dungeons
                if isinstance(maintained, dict):
                    activity = campaign_world(dungeon)
                    if activity:
                        S1CampaignWorldRepository.assert_owned_updates(maintained, activity)
                    _lock_maintenance_world_story_threads(
                        maintained,
                        dungeon=dungeon,
                    )
                preparation_context_verified = False
                preparation = maintained.get('_encounter_preparation') if isinstance(maintained, dict) else None
                if isinstance(preparation, dict) and preparation.get('status') == 'approved':
                    from db_repository.s1_encounter_preparation_repository import (
                        validate_context, project_maintenance_plan,
                    )
                    frozen = preparation.get('_snapshot') or {}
                    stale_reason = (
                        'maintenance_plan_changed'
                        if frozen.get('maintenance_plan') != project_maintenance_plan(maintained)
                        else validate_context(frozen)
                    )
                    preparation_context_verified = stale_reason is None
                    if stale_reason:
                        preparation.update(status='stale', reason_code=stale_reason)
                note = _soft_text(
                    maintained.get('dm_handoff_note')
                    if isinstance(maintained, dict) and not cycle_reseed_only
                    else maintained
                    if isinstance(maintained, str) and not cycle_reseed_only
                    else ''
                )
                diagnostics = maintained.get('_diagnostics') if isinstance(maintained, dict) else []
                state = (
                    AdventurerS1DmHandoffState.query
                    .filter_by(target_dungeon=dungeon)
                    .with_for_update()
                    .first()
                    if not cycle_reseed_only else None
                )
                note_changed = False
                delete_empty_state = bool(state and not note)
                if not state and note:
                    state = AdventurerS1DmHandoffState(target_dungeon=dungeon)
                    db.session.add(state)
                    note_changed = True
                if state and note:
                    if state.note != note:
                        state.note = note
                        note_changed = True
                    source_ids = list(dict.fromkeys((state.source_candidate_ids or []) + candidate_ids))
                    state.source_candidate_ids = source_ids[-50:]
                upsert_result = _new_world_story_upsert_result([])
                ecology_debt_outcomes: list[dict] = []
                location_state_applications: list[dict] = []
                ecology_degraded = False
                if is_ecology_recovery:
                    ecology_debt_outcomes = [
                        {
                            'candidate_id': int(candidate_id),
                            'expedition_log_id': int(
                                expedition_log_by_candidate_id[candidate_id]
                            ),
                            'requested_decision': 'dm_handoff_note',
                            'actual_decision': 'dm_handoff_note',
                            'world_story_thread_indexes': [],
                            'applied_world_story_thread_indexes': [],
                            'rejected_world_story_thread_indexes': [],
                            'consumed': True,
                            'recovery': True,
                        }
                        for candidate_id in candidate_ids
                    ]
                if cycle_reseed_only:
                    cycle_rows = (
                        maintained.get('world_story_threads')
                        if isinstance(maintained, dict) else []
                    ) or []
                    cycle_transition = (
                        maintained.get('cycle_reseed_transition')
                        if isinstance(maintained, dict) else None
                    )
                    cycle_result_status = None
                    cycle_result_source = (
                        cycle_rows[0]
                        if len(cycle_rows) == 1 and isinstance(cycle_rows[0], dict)
                        else None
                    )
                    cycle_result_application = None
                    if not cycle_rows and not cycle_transition:
                        cycle_result_status = 'rejected'
                    else:
                        cycle_savepoint = db_manager.begin_nested()
                        try:
                            upsert_result = S1WorldOutlineService._upsert_world_story_threads(
                                cycle_rows,
                                dungeon=dungeon,
                                candidate_ids=candidate_ids,
                                last_seen_log_id=last_log_by_dungeon.get(dungeon),
                                cycle_reseed_only=True,
                            )
                            expected_cycle_key = (
                                (
                                    _coerce_int(cycle_rows[0].get('source_world_story_thread_id'), 0),
                                    _coerce_int(cycle_rows[0].get('source_revision'), 0),
                                    _clip_text(cycle_rows[0].get('title'), 120),
                                )
                                if len(cycle_rows) == 1
                                and isinstance(cycle_rows[0], dict)
                                else None
                            )
                            if (
                                expected_cycle_key is None
                                or expected_cycle_key not in upsert_result[
                                    'applied_cycle_reseed_keys'
                                ]
                                or not cycle_transition
                            ):
                                raise ValueError('循环播种后继未完整落入权威状态')
                            from plugins.adventurer_s1.services.s1_world_location_state_service import (
                                S1WorldLocationStateService,
                            )
                            cycle_result_application = (
                                S1WorldLocationStateService.apply_cycle_reseed_transition(
                                    dungeon,
                                    cycle_transition,
                                    expected_outline_revision=dungeon_revision,
                                )
                            )
                            cycle_savepoint.commit()
                            cycle_result_status = 'applied'
                        except Exception as exc:  # noqa: BLE001 - maintenance remains failure-open
                            if cycle_savepoint.is_active:
                                cycle_savepoint.rollback()
                            diagnostics = list(diagnostics or [])
                            diagnostics.append(
                                '循环播种权威状态落库失败:'
                                f'{type(exc).__name__}:{str(exc)[:500]}'
                            )
                            aggregate_state = (
                                AdventurerS1WorldOutlineDungeonState.query
                                .filter_by(target_dungeon=dungeon)
                                .with_for_update()
                                .populate_existing()
                                .first()
                            )
                            if (
                                aggregate_state
                                and aggregate_state.maintenance_claim_token == dungeon_claim_token
                            ):
                                aggregate_state.maintenance_claim_token = None
                                aggregate_state.maintenance_claim_expires_at = None
                                aggregate_state.updated_at = _portable_utc_now()
                                _record_cycle_reseed_result(
                                    aggregate_state,
                                    'failed',
                                    attempted_at=(
                                        aggregate_state.cycle_reseed_last_attempted_at
                                        or claim_time
                                    ),
                                    stage='apply',
                                    reason=f'{type(exc).__name__}:{str(exc)[:900]}',
                                    diagnostics=diagnostics,
                                    source=cycle_result_source,
                                )
                            failed += 1
                            warning_count += len(diagnostics)
                            logger.warning(
                                'S1 循环播种权威状态落库失败 dungeon=%s: %s',
                                dungeon,
                                exc,
                                exc_info=True,
                            )
                            # See the lease-loss branch above: never let a
                            # context-exit commit error disappear behind continue.
                            db.session.commit()
                            continue
                else:
                    location_changes = maintained.get('location_state_changes', []) if isinstance(maintained, dict) else []
                    if location_changes:
                        if maintained.get('_location_state_review') != 'allow':
                            raise ValueError('world_location_maintenance_review_missing')
                        from plugins.adventurer_s1.services.s1_world_location_state_service import (
                            S1WorldLocationStateService,
                        )
                        location_state_applications = S1WorldLocationStateService.apply_maintenance_location_changes(
                            dungeon, location_changes,
                            snapshot=maintained.get('_maintenance_location_snapshot'),
                            expected_outline_revision=dungeon_revision,
                        )
                    upsert_result = S1WorldOutlineService._upsert_world_story_threads(
                        maintained.get('world_story_threads')
                        if isinstance(maintained, dict) else [],
                        dungeon=dungeon,
                        candidate_ids=candidate_ids,
                        last_seen_log_id=last_log_by_dungeon.get(dungeon),
                        cycle_reseed_only=False,
                        continuity_revival_target=(
                            maintained.get('_continuity_revival_target')
                            if isinstance(maintained, dict) else None
                        ),
                        ecology_debt_authority_by_index=(
                            maintained.get(
                                '_ecology_debt_world_story_authority_by_index'
                            )
                            if isinstance(maintained, dict) else None
                        ),
                    )
                    applied_indexes = set(upsert_result.get('applied_indexes') or [])
                    fallback_candidate_ids: list[int] = []
                    fallback_notes: list[str] = []
                    for resolution in (
                        maintained.get(
                            '_ecology_continuity_debt_resolutions', []
                        )
                        if isinstance(maintained, dict) else []
                    ):
                        if not isinstance(resolution, dict):
                            continue
                        expedition_log_id = _coerce_int(
                            resolution.get('expedition_log_id'), 0,
                        )
                        candidate_id = _coerce_int(
                            (
                                maintained.get(
                                    '_ecology_debt_candidate_id_by_log_id',
                                    {},
                                ) or {}
                            ).get(expedition_log_id),
                            0,
                        )
                        if expedition_log_id <= 0 or candidate_id <= 0:
                            raise ValueError(
                                '资源生态连续性债务消费缺少精确来源绑定'
                            )
                        requested_decision = str(
                            resolution.get('decision') or ''
                        )
                        indexes = [
                            _coerce_int(value, -1)
                            for value in (
                                resolution.get('world_story_thread_indexes')
                                or []
                            )
                        ]
                        if requested_decision not in {
                            'world_story_thread', 'dm_handoff_note',
                        }:
                            raise ValueError(
                                '资源生态连续性债务消费决策越界'
                            )
                        rejected_indexes = sorted(
                            index for index in indexes
                            if index not in applied_indexes
                        )
                        actual_decision = requested_decision
                        if (
                            requested_decision == 'world_story_thread'
                            and rejected_indexes
                        ):
                            actual_decision = 'dm_handoff_note'
                            fallback_candidate_ids.append(candidate_id)
                            fallback_note = _soft_text((
                                maintained.get(
                                    '_ecology_debt_handoff_note_by_log_id',
                                    {},
                                ) or {}
                            ).get(expedition_log_id))
                            if fallback_note:
                                fallback_notes.append(fallback_note)
                        elif requested_decision == 'dm_handoff_note' and not (
                            state and _soft_text(state.note)
                        ):
                            raise ValueError(
                                '资源生态连续性债务 DM 接力出口未实际落库'
                            )
                        ecology_debt_outcomes.append({
                            'candidate_id': candidate_id,
                            'expedition_log_id': expedition_log_id,
                            'requested_decision': requested_decision,
                            'actual_decision': actual_decision,
                            'world_story_thread_indexes': indexes,
                            'applied_world_story_thread_indexes': sorted(
                                index for index in indexes
                                if index in applied_indexes
                            ),
                            'rejected_world_story_thread_indexes': (
                                rejected_indexes
                            ),
                            'consumed': True,
                        })
                    fallback_candidate_ids = [
                        candidate_id for candidate_id in fallback_candidate_ids
                        if candidate_id > 0
                    ]
                    if fallback_candidate_ids:
                        fallback_note = _merge_dm_handoff_notes(
                            getattr(state, 'note', ''),
                            note,
                            *fallback_notes,
                        )
                        if not fallback_note:
                            raise ValueError(
                                '资源生态连续性债务降级未形成非空接力便签'
                            )
                        if not state:
                            state = AdventurerS1DmHandoffState(
                                target_dungeon=dungeon,
                            )
                            db.session.add(state)
                        if state.note != fallback_note:
                            state.note = fallback_note
                            note_changed = True
                        source_ids = list(dict.fromkeys(
                            (state.source_candidate_ids or [])
                            + fallback_candidate_ids
                        ))
                        state.source_candidate_ids = source_ids[-50:]
                        note = fallback_note
                        delete_empty_state = False
                        ecology_degraded = True
                        diagnostics = list(diagnostics or [])
                        diagnostics.append(
                            ECOLOGY_CONTINUITY_DEBT_CONSUMER_FALLBACK_DIAGNOSTIC
                        )
                if state and delete_empty_state and not note:
                    db.session.delete(state)
                    note_changed = True
                if isinstance(maintained, dict) and not cycle_reseed_only:
                    from plugins.adventurer_s1.services.s1_world_actor_service import apply_maintenance_updates
                    applied = apply_maintenance_updates(
                        maintained.get('world_actors'),
                        candidate_ids=candidate_ids,
                        last_seen_log_id=last_log_by_dungeon.get(dungeon),
                        capability_bases=maintained.get('_world_actor_capability_bases') or {},
                    )
                    for key in actor_updates:
                        actor_updates[key] += int(applied.get(key, 0))
                    from plugins.adventurer_s1.services.s1_first_encounter_learning_service import persist as persist_first_learning
                    for learning in maintained.get('_first_encounter_learning') or []:
                        actor_id = applied.get('created_actor_ids', {}).get(learning['maintenance_actor_index'])
                        if actor_id is None:
                            raise ValueError('first_encounter_learning.admission_changed')
                        from plugins.adventurer_s1.models import AdventurerS1WorldActor
                        actor = AdventurerS1WorldActor.query.filter_by(id=actor_id).first()
                        persist_first_learning(learning, actor, dungeon=dungeon, candidate_ids=candidate_ids)
                    missing_profile_actor_ids.extend(
                        int(value) for value in (applied.get('missing_profile_actor_ids') or [])
                    )
                    from plugins.adventurer_s1.services.s1_faction_profile_service import (
                        apply_maintenance_updates as apply_faction_profile_updates,
                    )
                    core_flushed, core_flush_error = db_manager.flush()
                    if not core_flushed:
                        raise RuntimeError(
                            f'势力档案子事务前核心维护落库失败: {core_flush_error}'
                        )
                    try:
                        # 即使 flush 失败已把 savepoint 标成 inactive，上下文退出仍会
                        # 完成该层 rollback/close，避免辅助写污染外层核心维护事务。
                        with db_manager.begin_nested():
                            applied_faction_profiles = apply_faction_profile_updates(
                                maintained.get('faction_profile_updates'),
                                candidate_ids=candidate_ids,
                                last_seen_log_id=last_log_by_dungeon.get(dungeon),
                                source_overrides_by_thread_id=(
                                    maintained.get(
                                        '_faction_profile_bootstrap_source_overrides'
                                    )
                                    if isinstance(
                                        maintained.get(
                                            '_faction_profile_bootstrap_source_overrides'
                                        ),
                                        dict,
                                    )
                                    else None
                                ),
                            )
                            faction_flushed, faction_flush_error = db_manager.flush()
                            if not faction_flushed:
                                raise RuntimeError(
                                    faction_flush_error or '未知约束错误'
                                )
                    except Exception as exc:
                        raw_profile_updates = maintained.get('faction_profile_updates')
                        applied_faction_profiles = {
                            'created': 0,
                            'updated': 0,
                            'linked': 0,
                            'history_rows': 0,
                            'skipped': (
                                len(raw_profile_updates)
                                if isinstance(raw_profile_updates, list) else 0
                            ),
                            'diagnostics': [
                                '势力档案辅助落库异常，已回滚该辅助批次且不重试 AI'
                            ],
                        }
                        logger.warning(
                            'S1 势力档案辅助落库失败开放 dungeon=%s: %s',
                            dungeon,
                            exc,
                            exc_info=True,
                        )
                    for key in faction_profile_updates:
                        faction_profile_updates[key] += int(
                            applied_faction_profiles.get(key, 0)
                        )
                    diagnostics = list(diagnostics or [])
                    diagnostics.extend(
                        applied_faction_profiles.get('diagnostics') or []
                    )
                encounter_preparation_receipt = None
                if isinstance(maintained, dict) and not cycle_reseed_only and isinstance(
                    maintained.get('_encounter_preparation'), dict,
                ):
                    from db_repository.s1_encounter_preparation_repository import apply_preparation
                    core_flushed, core_flush_error = db_manager.flush()
                    if not core_flushed:
                        raise RuntimeError(core_flush_error or '实体准备前维护落库失败')
                    # The paired reviewer saw this exact plan. Partial application
                    # is a different world, so this auxiliary proposal is deferred.
                    if preparation.get('status') == 'approved' and (
                        upsert_result.get('rejected') or upsert_result.get('warnings')
                        or ecology_degraded
                        or int(applied.get('created', 0)) + int(applied.get('updated', 0))
                        != len(maintained.get('world_actors') or [])
                        or applied_faction_profiles.get('rejected')
                        or applied_faction_profiles.get('diagnostics')
                    ):
                        preparation.update(status='deferred', reason_code='maintenance_partially_applied')
                    try:
                        with db_manager.begin_nested():
                            encounter_preparation_receipt = apply_preparation(
                                maintained['_encounter_preparation'],
                                dungeon=dungeon, candidate_ids=candidate_ids,
                                context_verified=preparation_context_verified,
                            )
                    except Exception:
                        logger.exception('S1 后续实体辅助落库失败，已回滚该提案 dungeon=%s', dungeon)
                        encounter_preparation_receipt = {'status': 'failed', 'reason_code': 'consumer_failed'}
                    if encounter_preparation_receipt.get('status') == 'persisted':
                        actor_updates['created'] += 1
                rejected_count = len(upsert_result.get('rejected') or [])
                applied_count = len(upsert_result.get('applied') or [])
                consumer_warning_count = len(
                    upsert_result.get('warnings') or []
                )
                proposed_count = int(upsert_result.get('proposed_count') or 0)
                if cycle_reseed_only:
                    receipt_status = (
                        cycle_result_status
                        if cycle_result_status in {'applied', 'rejected'}
                        else 'failed'
                    )
                elif is_ecology_recovery or ecology_degraded:
                    receipt_status = 'degraded'
                elif rejected_count:
                    receipt_status = (
                        'partial' if applied_count or location_state_applications else 'rejected'
                    )
                elif consumer_warning_count:
                    receipt_status = 'applied_with_warnings'
                elif (
                    proposed_count
                    or (encounter_preparation_receipt or {}).get('status') == 'persisted'
                    or location_state_applications
                    or note
                    or (
                        isinstance(maintained, dict)
                        and maintained.get('world_actors')
                    )
                ):
                    receipt_status = 'applied'
                else:
                    receipt_status = 'no_changes'
                preparation_issue_code = _encounter_preparation_issue_code(encounter_preparation_receipt)
                if preparation_issue_code and receipt_status in {'applied', 'no_changes'}:
                    receipt_status = 'applied_with_warnings' if receipt_status == 'applied' else 'rejected'
                primary_issue_code = _primary_world_outline_issue_code(
                    receipt_status=receipt_status,
                    upsert_result=upsert_result,
                    diagnostics=list(diagnostics or []),
                    ecology_degraded=ecology_degraded,
                    encounter_preparation=encounter_preparation_receipt,
                )
                if cycle_reseed_only and receipt_status == 'rejected':
                    primary_issue_code = 'cycle_reseed_rejected'
                receipt_issue_codes = (
                    [primary_issue_code] if primary_issue_code else []
                )
                if preparation_issue_code and preparation_issue_code not in receipt_issue_codes:
                    receipt_issue_codes.append(preparation_issue_code)
                committed_receipt = _build_world_outline_maintenance_receipt(
                    batch_id=maintenance_batch_id,
                    dungeon=dungeon,
                    candidate_ids=candidate_ids,
                    expedition_log_ids=batch_expedition_log_ids,
                    status=receipt_status,
                    stage=(
                        'ecology_handoff_recovery'
                        if is_ecology_recovery else 'consumer'
                    ),
                    upsert_result=upsert_result,
                    issue_codes=receipt_issue_codes,
                    diagnostics=list(diagnostics or []),
                    ecology_debt_outcomes=ecology_debt_outcomes,
                    ecology_debt_expected=batch_has_ecology_debt,
                    recovery_sources=batch_recovery_sources,
                )
                committed_receipt['location_state_applications'] = location_state_applications
                if encounter_preparation_receipt is not None:
                    committed_receipt['encounter_preparation'] = encounter_preparation_receipt
                now = _portable_utc_now()
                if not cycle_reseed_only:
                    from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
                    S1CampaignDispatchService.publish_applied(
                        candidate_ids, maintained.get('world_story_threads') or [], applied_indexes,
                    )
                for row in locked:
                    row.status = 'processed'
                    row.maintenance_issue_code = primary_issue_code
                    row.maintenance_receipt = dict(committed_receipt)
                    row.maintenance_recovery_kind = None
                    warning_text = '; '.join(
                        str(item) for item in (diagnostics or [])
                    )[:650]
                    if primary_issue_code:
                        row.outline_error = (
                            f'警告[{primary_issue_code}] '
                            f'receipt={maintenance_batch_id} '
                            f'proposed={proposed_count} applied={applied_count} '
                            f'rejected={rejected_count}'
                            + (f'; {warning_text}' if warning_text else '')
                        )[:1000]
                    else:
                        row.outline_error = (
                            f'警告: {warning_text}' if warning_text else None
                        )
                    row.processed_at = now
                    processed += 1
                processed_groups += 1
                aggregate_state.outline_revision = dungeon_revision + 1
                aggregate_state.maintenance_claim_token = None
                aggregate_state.maintenance_claim_expires_at = None
                aggregate_state.updated_at = now
                if cycle_reseed_only and cycle_result_status:
                    _record_cycle_reseed_result(
                        aggregate_state,
                        cycle_result_status,
                        attempted_at=(
                            aggregate_state.cycle_reseed_last_attempted_at
                            or claim_time
                        ),
                        stage='review' if cycle_result_status == 'rejected' else 'apply',
                        reason=(
                            '循环播种审核未批准后继'
                            if cycle_result_status == 'rejected'
                            else None
                        ),
                        diagnostics=diagnostics,
                        source=cycle_result_source,
                        application=cycle_result_application,
                    )
                warning_count += (
                    len(diagnostics or [])
                    + rejected_count
                    + consumer_warning_count
                )
                world_story_updates['proposed'] += proposed_count
                world_story_updates['applied'] += applied_count
                world_story_updates['rejected'] += rejected_count
                if note_changed:
                    updated_notes += 1
                usage_outcome.is_success = bool(
                    not rejected_count
                    and not consumer_warning_count
                    and not ecology_degraded
                    and (
                        not cycle_reseed_only
                        or cycle_result_status == 'applied'
                    )
                )
                usage_outcome.receipt = committed_receipt
            if usage_outcome.error is not None:
                processed = processed_before_consumer
                processed_groups = processed_groups_before_consumer
                failed = failed_before_consumer
                updated_notes = updated_notes_before_consumer
                warning_count = warning_count_before_consumer
                actor_updates = actor_updates_before_consumer
                faction_profile_updates = faction_updates_before_consumer
                world_story_updates = world_story_updates_before_consumer
                del missing_profile_actor_ids[
                    missing_profile_count_before_consumer:
                ]
                consumer_error = usage_outcome.error
                issue_code = 'consumer_transaction_failed'
                rolled_back_threads = (
                    maintained.get('world_story_threads')
                    if isinstance(maintained, dict) else []
                )
                upsert_result = _world_story_upsert_result_after_rollback(
                    rolled_back_threads,
                    reason_code='consumer_transaction_rolled_back',
                    previous_result=upsert_result,
                )
                failed_receipt = _build_world_outline_maintenance_receipt(
                    batch_id=maintenance_batch_id,
                    dungeon=dungeon,
                    candidate_ids=candidate_ids,
                    expedition_log_ids=batch_expedition_log_ids,
                    status='failed',
                    stage='consumer_transaction',
                    upsert_result=upsert_result,
                    issue_codes=[issue_code],
                    diagnostics=list(diagnostics or []),
                    ecology_debt_expected=batch_has_ecology_debt,
                    recovery_sources=batch_recovery_sources,
                    error=consumer_error,
                )
                logger.warning(
                    'S1 大纲维护消费事务失败 batch=%s dungeon=%s: %s',
                    maintenance_batch_id,
                    dungeon,
                    consumer_error,
                    exc_info=(
                        type(consumer_error),
                        consumer_error,
                        consumer_error.__traceback__,
                    ),
                )
                try:
                    failed_row_count = 0
                    with db_manager.transaction():
                        aggregate_state = (
                            AdventurerS1WorldOutlineDungeonState.query
                            .filter_by(target_dungeon=dungeon)
                            .with_for_update()
                            .first()
                        )
                        if (
                            aggregate_state
                            and aggregate_state.maintenance_claim_token
                            == dungeon_claim_token
                        ):
                            aggregate_state.maintenance_claim_token = None
                            aggregate_state.maintenance_claim_expires_at = None
                            aggregate_state.updated_at = _portable_utc_now()
                            if dungeon in cycle_reseed_only_dungeons:
                                _record_cycle_reseed_result(
                                    aggregate_state,
                                    'failed',
                                    attempted_at=(
                                        aggregate_state.cycle_reseed_last_attempted_at
                                        or claim_time
                                    ),
                                    stage='consumer_transaction',
                                    reason=(
                                        f'{type(consumer_error).__name__}:'
                                        f'{str(consumer_error)[:900]}'
                                    ),
                                )
                        failed_rows = AdventurerS1WorldFactCandidate.query.filter(
                            AdventurerS1WorldFactCandidate.id.in_(candidate_ids),
                            AdventurerS1WorldFactCandidate.status == 'processing',
                            AdventurerS1WorldFactCandidate.outline_error == claim_token,
                        ).with_for_update().all()
                        failed_row_count = len(failed_rows) or int(
                            dungeon in cycle_reseed_only_dungeons
                        )
                        for row in failed_rows:
                            row.status = 'failed'
                            row.maintenance_issue_code = issue_code
                            row.maintenance_receipt = dict(failed_receipt)
                            row.maintenance_recovery_kind = (
                                WORLD_OUTLINE_ECOLOGY_RECOVERY_KIND
                                if int(row.id) in ecology_debt_candidate_ids
                                else None
                            )
                            row.outline_error = (
                                f'[{issue_code}] receipt={maintenance_batch_id}: '
                                f'{str(consumer_error)[:850]}'
                            )
                            row.processed_at = _portable_utc_now()
                        released_count = S1CampaignDispatchRepository.release_failed_publications(
                            candidate_ids=[row.id for row in failed_rows],
                            limit=batch_limit, now=_portable_utc_now(),
                        )
                    campaign_publication_released += released_count
                    failed += failed_row_count
                    _log_world_outline_maintenance_receipt(failed_receipt)
                except Exception as record_exc:  # noqa: BLE001
                    logger.error(
                        'S1_WORLD_OUTLINE_MAINTENANCE_RECEIPT_PERSIST_FAILED '
                        'batch=%s dungeon=%s candidates=%s logs=%s '
                        'stage=consumer original_code=%s error=%s',
                        maintenance_batch_id,
                        dungeon,
                        candidate_ids,
                        batch_expedition_log_ids,
                        issue_code,
                        record_exc,
                        exc_info=True,
                    )
                    db_manager.remove()
                continue
            if committed_receipt is not None:
                if is_ecology_recovery:
                    successful_ecology_recoveries += len(candidate_ids)
                _log_world_outline_maintenance_receipt(committed_receipt)
        if missing_profile_actor_ids:
            from plugins.adventurer_s1.services.s1_world_actor_profile_backfill_ai_service import (
                submit_missing_world_actor_profiles,
            )
            unique_missing_actor_ids = list(dict.fromkeys(
                missing_profile_actor_ids
            ))
            try:
                submit_missing_world_actor_profiles(unique_missing_actor_ids)
            except Exception as exc:  # noqa: BLE001 - auxiliary submit is failure-open
                warning_count += 1
                logger.warning(
                    'S1_WORLD_OUTLINE_PROFILE_BACKFILL_SUBMIT_FAILED '
                    'actor_ids=%s error=%s',
                    unique_missing_actor_ids,
                    exc,
                    exc_info=True,
                )
                db_manager.remove()
        return {
            'processed': processed,
            'processed_groups': processed_groups,
            'updated_notes': updated_notes,
            'failed': failed,
            'rejected': rejected,
            'warnings': warning_count,
            'recovered': stale_requeued_count + successful_ecology_recoveries,
            'campaign_publication_released': campaign_publication_released,
            'lease_lost': lease_lost,
            'deferred': deferred,
            'world_actors': actor_updates,
            'faction_profiles': faction_profile_updates,
            'world_story_threads': world_story_updates,
            'cycle_reseed_scanned': len(cycle_reseed_dungeons),
        }

    @staticmethod
    def _upsert_world_story_threads(
        raw_threads,
        *,
        dungeon: str,
        candidate_ids: list[int],
        last_seen_log_id: int | None,
        cycle_reseed_only: bool = False,
        continuity_revival_target: dict | None = None,
        ecology_debt_authority_by_index: dict[int, list[int]] | None = None,
    ) -> dict:
        upsert_result = _new_world_story_upsert_result(raw_threads)
        if not isinstance(raw_threads, list):
            return upsert_result
        debt_authority_by_index = {
            int(index): {
                int(candidate_id)
                for candidate_id in candidate_values
                if _coerce_int(candidate_id, 0) > 0
            }
            for index, candidate_values in (
                ecology_debt_authority_by_index or {}
            ).items()
            if isinstance(candidate_values, list)
        }
        legacy_rows = AdventurerS1WorldStoryThread.query.filter(
            AdventurerS1WorldStoryThread.base_dungeon.is_(None),
            AdventurerS1WorldStoryThread.last_seen_log_id.isnot(None),
        ).all()
        _hydrate_legacy_world_story_base_dungeons(legacy_rows)
        candidate_rows = AdventurerS1WorldFactCandidate.query.filter(
            AdventurerS1WorldFactCandidate.id.in_(candidate_ids),
        ).all()
        candidate_log_ids = [
            int(row.expedition_log_id)
            for row in candidate_rows
            if row.expedition_log_id
        ]
        candidate_source_logs = (
            AdventurerS1ExpeditionLog.query
            .with_entities(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.full_log,
            )
            .filter(AdventurerS1ExpeditionLog.id.in_(candidate_log_ids))
            .all()
            if candidate_log_ids else []
        )
        source_full_logs_by_id = {
            int(log_id): source_full_log
            for log_id, source_full_log in candidate_source_logs
        }
        candidate_log_id_by_id = {
            int(candidate.id): int(candidate.expedition_log_id)
            for candidate in candidate_rows
            if candidate.expedition_log_id
        }
        candidate_scope_info = {
            int(candidate.id): (
                resolve_persistent_story_scope_from_full_log(
                    source_full_logs_by_id[int(candidate.expedition_log_id)],
                    expected_target_dungeon=dungeon,
                    expected_expedition_log_id=int(candidate.expedition_log_id),
                )
                if (
                    int(candidate.expedition_log_id) in source_full_logs_by_id
                    and isinstance(
                        source_full_logs_by_id[int(candidate.expedition_log_id)],
                        dict,
                    )
                )
                else ('unresolved', None)
            )
            for candidate in candidate_rows
            if candidate.expedition_log_id
        }
        trusted_without_candidates = not candidate_rows and not cycle_reseed_only
        authorized_change_candidate_ids: dict[tuple[int, int, str], set[int]] = {}
        new_candidate_ids: set[int] = set()
        for candidate in candidate_rows:
            for fact in candidate.world_fact_updates or []:
                if not isinstance(fact, dict):
                    continue
                fact_change_kind = _strict_ai_text(fact.get('change_kind')).lower()
                if fact_change_kind not in {
                    'new', 'advance', 'close', 'resolve', 'successor',
                }:
                    continue
                if fact_change_kind == 'new':
                    new_candidate_ids.add(int(candidate.id))
                    continue
                change_ref = (
                    _strict_ai_positive_integer(
                        fact.get('source_world_story_thread_id')
                    ),
                    _strict_ai_positive_integer(fact.get('source_revision')),
                    fact_change_kind,
                )
                authorized_change_candidate_ids.setdefault(change_ref, set()).add(
                    int(candidate.id),
                )
        authorized_changes = set(authorized_change_candidate_ids)
        has_new_candidate = trusted_without_candidates or bool(new_candidate_ids)

        continuity_target = (
            dict(continuity_revival_target)
            if isinstance(continuity_revival_target, dict)
            else {}
        )
        continuity_target_kind = str(
            continuity_target.get('target_kind') or ''
        ).strip()
        continuity_actor_id = _strict_ai_positive_integer(
            continuity_target.get('world_actor_id')
        )
        continuity_actor_revision = _strict_ai_positive_integer(
            continuity_target.get('capability_revision')
        )
        continuity_predecessor_id = _strict_ai_positive_integer(
            continuity_target.get('predecessor_id')
        )
        continuity_source_revision = _strict_ai_positive_integer(
            continuity_target.get('source_revision')
        )
        if continuity_target_kind not in {
            'thread_successor', 'actor_successor', 'actor_new_front',
        }:
            continuity_target_kind = ''
        if continuity_target_kind.startswith('actor_') and (
            continuity_actor_id <= 0 or continuity_actor_revision <= 0
        ):
            continuity_target_kind = ''
        if continuity_target_kind.endswith('successor') and (
            continuity_predecessor_id <= 0 or continuity_source_revision <= 0
        ):
            continuity_target_kind = ''

        def matches_continuity_target(raw: object) -> bool:
            if not continuity_target_kind or not isinstance(raw, dict):
                return False
            if str(raw.get('status') or '').strip() != 'seeded':
                return False
            scope_keys = raw.get('scope_keys')
            if (
                not isinstance(scope_keys, list)
                or dungeon not in {
                    str(value).strip() for value in scope_keys
                    if isinstance(value, str)
                }
            ):
                return False
            change_kind = str(raw.get('change_kind') or '').strip().lower()
            output_actor_id = _strict_ai_positive_integer(
                raw.get('continuity_actor_id')
            )
            if continuity_target_kind == 'thread_successor':
                return bool(
                    change_kind == 'successor'
                    and output_actor_id <= 0
                    and _strict_ai_positive_integer(raw.get('predecessor_id'))
                    == continuity_predecessor_id
                    and _strict_ai_positive_integer(
                        raw.get('source_world_story_thread_id')
                    ) == continuity_predecessor_id
                    and _strict_ai_positive_integer(raw.get('source_revision'))
                    == continuity_source_revision
                )
            if output_actor_id != continuity_actor_id:
                return False
            if continuity_target_kind == 'actor_successor':
                return bool(
                    change_kind == 'successor'
                    and _strict_ai_positive_integer(raw.get('predecessor_id'))
                    == continuity_predecessor_id
                    and _strict_ai_positive_integer(
                        raw.get('source_world_story_thread_id')
                    ) == continuity_predecessor_id
                    and _strict_ai_positive_integer(raw.get('source_revision'))
                    == continuity_source_revision
                )
            return bool(
                change_kind == 'new'
                and _strict_ai_positive_integer(raw.get('predecessor_id')) <= 0
                and _strict_ai_positive_integer(
                    raw.get('source_world_story_thread_id')
                ) <= 0
                and _strict_ai_positive_integer(raw.get('source_revision')) <= 0
            )

        continuity_match_indexes = [
            index
            for index, raw in enumerate(raw_threads)
            if matches_continuity_target(raw)
        ]
        continuity_match_index = (
            continuity_match_indexes[0]
            if len(continuity_match_indexes) == 1
            else None
        )
        if len(continuity_match_indexes) > 1:
            logger.warning(
                'S1 大纲维护丢弃重复续篇授权提案 dungeon=%s target=%s indexes=%s',
                dungeon,
                continuity_target_kind,
                continuity_match_indexes,
            )
        continuity_actor_row: AdventurerS1WorldActor | None = None
        continuity_actor_checked = False
        continuity_actor_valid = False

        def lock_and_validate_continuity_actor() -> bool:
            nonlocal continuity_actor_row
            nonlocal continuity_actor_checked
            nonlocal continuity_actor_valid
            if continuity_actor_checked:
                return continuity_actor_valid
            continuity_actor_checked = True
            if not continuity_target_kind.startswith('actor_'):
                continuity_actor_valid = True
                return True
            continuity_actor_row = (
                AdventurerS1WorldActor.query
                .filter_by(id=continuity_actor_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if (
                not continuity_actor_row
                or continuity_actor_row.status != 'active'
                or not continuity_actor_row.capability_profile
                or int(continuity_actor_row.capability_revision or 0)
                != continuity_actor_revision
            ):
                return False
            actor_links = (
                AdventurerS1WorldStoryThreadActor.query
                .filter_by(actor_id=continuity_actor_id)
                .order_by(AdventurerS1WorldStoryThreadActor.id.asc())
                .with_for_update()
                .all()
            )
            linked_ids = list(dict.fromkeys(
                int(link.world_story_thread_id) for link in actor_links
            ))
            linked_heads = (
                AdventurerS1WorldStoryThread.query
                .filter(
                    AdventurerS1WorldStoryThread.id.in_(linked_ids),
                    _world_story_head_clause(),
                )
                .all()
                if linked_ids else []
            )
            if any(
                row.status in WORLD_INTEL_MUTABLE_STATUSES
                for row in linked_heads
            ):
                return False
            if (
                continuity_target_kind == 'actor_successor'
                and continuity_predecessor_id not in linked_ids
            ):
                return False
            continuity_actor_valid = True
            return True

        def resolve_candidate_scope(
            supporting_candidate_ids: set[int],
        ) -> tuple[str, str | None]:
            if any(
                candidate_id not in candidate_scope_info
                for candidate_id in supporting_candidate_ids
            ):
                return 'unresolved', None
            scope_info = [
                candidate_scope_info[candidate_id]
                for candidate_id in sorted(supporting_candidate_ids)
            ]
            if not scope_info or all(
                state == 'legacy' for state, _scope_key in scope_info
            ):
                return 'legacy', None
            stable_scope_keys = {
                scope_key
                for state, scope_key in scope_info
                if state == 'stable' and scope_key
            }
            if (
                len(stable_scope_keys) == 1
                and all(state == 'stable' for state, _scope_key in scope_info)
            ):
                return 'stable', next(iter(stable_scope_keys))
            return 'unresolved', None
        # 先应用已有 head 的收束更新，再创建引用 predecessor_id 的后继。
        # AI 数组顺序不是状态机语义；同一响应即使先写后继，也应按依赖顺序落库。
        # MAX_AI_UPDATES 只用于约束 prompt；合法返回项必须全部处理，不能静默截断后再把候选标为 processed。
        ordered_updates = list(enumerate(raw_threads))
        ordered_updates.sort(
            key=lambda item: (
                0
                if (
                    isinstance(item[1], dict)
                    and _strict_ai_positive_integer(
                        item[1].get('id') or item[1].get('thread_id')
                    ) > 0
                    and _strict_ai_positive_integer(
                        item[1].get('predecessor_id')
                    ) <= 0
                )
                else 1,
                item[0],
            ),
        )
        has_cycle_reseed = any(
            isinstance(raw, dict) and raw.get('cycle_reseed') is True
            for raw in raw_threads
        )
        cycle_reseed_state = (
            AdventurerS1WorldOutlineDungeonState.query
            .filter_by(target_dungeon=dungeon)
            .with_for_update()
            .first()
            if has_cycle_reseed else None
        )
        batch_previous_revisions: dict[int, int] = {}
        applied_cycle_reseed_keys: set[tuple[int, int, str]] = upsert_result[
            'applied_cycle_reseed_keys'
        ]
        applied_rows: list[
            tuple[int, AdventurerS1WorldStoryThread, str, list[int]]
        ] = []
        continuity_revival_applied = False
        for original_index, raw in ordered_updates:
            if not isinstance(raw, dict):
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='proposal_not_object',
                    raw=raw,
                )
                continue
            if cycle_reseed_only and raw.get('cycle_reseed') is not True:
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='cycle_reseed_marker_missing',
                    raw=raw,
                )
                continue
            if (
                raw.get('cycle_reseed') is True
                and (
                    not cycle_reseed_state
                    or cycle_reseed_state.is_explorable is not False
                )
            ):
                logger.warning(
                    'S1 大纲维护拒绝可探索状态不为 false 的循环播种 dungeon=%s',
                    dungeon,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='cycle_reseed_dungeon_still_explorable',
                    raw=raw,
                )
                continue
            title = _strict_ai_text(raw.get('title'), limit=120)
            summary = _strict_ai_text(raw.get('summary'))
            if not title or not summary:
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='missing_title_or_summary',
                    raw=raw,
                )
                continue
            predecessor_id = _strict_ai_positive_integer(
                raw.get('predecessor_id')
            )
            requested_thread_id = _strict_ai_positive_integer(
                raw.get('id') or raw.get('thread_id')
            )
            raw_change_kind = raw.get('change_kind')
            change_kind = raw_change_kind.strip().lower() if isinstance(raw_change_kind, str) else ''
            if change_kind not in {
                'new', 'advance', 'close', 'resolve', 'successor',
            }:
                logger.warning(
                    'S1 大纲维护拒绝缺失或非法 change_kind title=%s change=%r',
                    title,
                    raw_change_kind,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='invalid_change_kind',
                    raw=raw,
                )
                continue
            source_thread_id = _strict_ai_positive_integer(
                raw.get('source_world_story_thread_id')
            )
            source_revision = _strict_ai_positive_integer(
                raw.get('source_revision')
            )
            debt_supporting_candidate_ids = set(
                debt_authority_by_index.get(original_index, set())
            )
            ecology_debt_authorized = bool(debt_supporting_candidate_ids)
            continuity_row_matched = bool(
                continuity_match_index is not None
                and original_index == continuity_match_index
            )
            raw_continuity_actor_id = _strict_ai_positive_integer(
                raw.get('continuity_actor_id')
            )
            if raw_continuity_actor_id > 0 and not continuity_row_matched:
                logger.info(
                    'S1 大纲维护软丢弃未匹配冻结目标的角色续篇字段 actor=%s title=%s',
                    raw_continuity_actor_id,
                    title,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='continuity_target_mismatch',
                    raw=raw,
                )
                continue
            continuity_successor_authorized = bool(
                continuity_row_matched
                and continuity_target_kind in {
                    'thread_successor', 'actor_successor',
                }
                and change_kind == 'successor'
            )
            continuity_new_authorized = bool(
                continuity_row_matched
                and continuity_target_kind == 'actor_new_front'
                and change_kind == 'new'
            )
            if not trusted_without_candidates and change_kind != 'new' and (
                source_thread_id, source_revision, change_kind,
            ) not in authorized_changes and (
                not continuity_successor_authorized
                and not ecology_debt_authorized
            ):
                cycle_reseed = raw.get('cycle_reseed') is True
                if change_kind != 'successor' or not cycle_reseed:
                    logger.warning(
                        'S1 大纲维护拒绝候选未授权的因果变更 source=%s revision=%s change=%s',
                        source_thread_id, source_revision, change_kind,
                    )
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='candidate_change_unauthorized',
                        raw=raw,
                    )
                    continue
            if (
                change_kind == 'new'
                and not has_new_candidate
                and not continuity_new_authorized
                and not ecology_debt_authorized
            ):
                logger.warning('S1 大纲维护拒绝无 new 候选支撑的独立新线 title=%s', title)
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='independent_new_unauthorized',
                    raw=raw,
                )
                continue
            if requested_thread_id > 0:
                if change_kind not in ('advance', 'close', 'resolve') or source_thread_id != requested_thread_id:
                    logger.warning(
                        'S1 大纲维护拒绝缺少结构化因果的更新 thread=%s change=%s source=%s',
                        requested_thread_id, change_kind, source_thread_id,
                    )
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='existing_update_causality_invalid',
                        raw=raw,
                    )
                    continue
            elif predecessor_id > 0:
                if change_kind != 'successor' or source_thread_id != predecessor_id:
                    logger.warning(
                        'S1 大纲维护拒绝缺少结构化因果的后继 predecessor=%s change=%s source=%s',
                        predecessor_id, change_kind, source_thread_id,
                    )
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='successor_causality_invalid',
                        raw=raw,
                    )
                    continue
            elif change_kind != 'new':
                logger.warning('S1 大纲维护拒绝未声明 change_kind=new 的独立新线 title=%s', title)
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='independent_change_not_new',
                    raw=raw,
                )
                continue
            row = S1WorldOutlineService._find_world_story_thread_for_update(raw, dungeon=dungeon, title=title)
            if trusted_without_candidates and row and source_revision <= 0:
                source_revision = int(row.revision or 1)
            if requested_thread_id > 0 and not row:
                logger.warning(
                    'S1 大纲维护拒绝更新不存在或非最新的世界线 thread=%s title=%s',
                    requested_thread_id,
                    title,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='target_thread_missing_or_stale',
                    raw=raw,
                )
                continue
            if row and (
                source_thread_id != int(row.id)
                or source_revision != int(row.revision or 1)
            ):
                logger.warning(
                    'S1 大纲维护拒绝版本或身份不匹配 thread=%s expected_revision=%s source=%s/%s',
                    row.id, int(row.revision or 1), source_thread_id, source_revision,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='source_identity_or_revision_mismatch',
                    raw=raw,
                )
                continue
            predecessor = None
            if predecessor_id > 0 and not row:
                title_conflict = (
                    AdventurerS1WorldStoryThread.query
                    .filter_by(title=title)
                    .with_for_update()
                    .first()
                )
                if title_conflict:
                    logger.warning(
                        'S1 大纲维护拒绝以后继关系复用其它节点标题 predecessor=%s title=%s existing=%s',
                        predecessor_id,
                        title,
                        title_conflict.id,
                    )
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='successor_title_conflict',
                        raw=raw,
                    )
                    continue
                predecessor = (
                    AdventurerS1WorldStoryThread.query
                    .filter_by(id=predecessor_id)
                    .with_for_update()
                    .first()
                )
                if trusted_without_candidates and predecessor and source_revision <= 0:
                    source_revision = int(predecessor.revision or 1)
                if not predecessor or predecessor.superseded_by_id or predecessor.is_chain_head is False:
                    logger.warning(
                        'S1 大纲维护拒绝无效或非最新前驱 predecessor=%s title=%s',
                        predecessor_id,
                        title,
                    )
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='predecessor_missing_or_stale',
                        raw=raw,
                    )
                    continue
                if (
                    not trusted_without_candidates
                    and (source_thread_id, source_revision, change_kind) not in authorized_changes
                    and not continuity_successor_authorized
                    and not ecology_debt_authorized
                ):
                    if (
                        raw.get('cycle_reseed') is not True
                        or predecessor.status not in WORLD_INTEL_TERMINAL_STATUSES
                        or _effective_world_story_base_dungeon(predecessor) != dungeon
                    ):
                        logger.warning(
                            'S1 大纲维护拒绝不满足终结阶段条件的循环播种 predecessor=%s status=%s',
                            predecessor_id,
                            predecessor.status,
                        )
                        _record_world_story_rejection(
                            upsert_result,
                            index=original_index,
                            reason_code='cycle_reseed_precondition_failed',
                            raw=raw,
                        )
                        continue
                    competing_head = (
                        AdventurerS1WorldStoryThread.query
                        .filter(
                            _world_story_head_clause(),
                            AdventurerS1WorldStoryThread.base_dungeon == dungeon,
                            AdventurerS1WorldStoryThread.status.in_(
                                WORLD_INTEL_MUTABLE_STATUSES,
                            ),
                        )
                        .with_for_update()
                        .first()
                    )
                    if competing_head:
                        logger.warning(
                            'S1 大纲维护拒绝在已有可继续世界线时循环播种 predecessor=%s competing=%s',
                            predecessor_id,
                            competing_head.id,
                        )
                        _record_world_story_rejection(
                            upsert_result,
                            index=original_index,
                            reason_code='cycle_reseed_competing_head',
                            raw=raw,
                        )
                        continue
                if predecessor.status not in ('dormant', 'resolved'):
                    logger.warning(
                        'S1 大纲维护拒绝从未收束前驱创建后继 predecessor=%s status=%s title=%s',
                        predecessor_id,
                        predecessor.status,
                        title,
                    )
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='successor_predecessor_not_terminal',
                        raw=raw,
                    )
                    continue
                expected_source_revision = batch_previous_revisions.get(
                    int(predecessor.id), int(predecessor.revision or 1),
                )
                if source_revision != expected_source_revision:
                    logger.warning(
                        'S1 大纲维护拒绝前驱版本不匹配 predecessor=%s expected=%s got=%s',
                        predecessor_id, expected_source_revision, source_revision,
                    )
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='predecessor_revision_mismatch',
                        raw=raw,
                    )
                    continue
            if (
                continuity_row_matched
                and continuity_target_kind.startswith('actor_')
                and not lock_and_validate_continuity_actor()
            ):
                logger.info(
                    'S1 大纲维护软丢弃已漂移的角色续篇提案 actor=%s title=%s',
                    continuity_actor_id,
                    title,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='continuity_actor_state_drifted',
                    raw=raw,
                )
                continue
            raw_status = raw.get('status')
            if not isinstance(raw_status, str):
                logger.warning(
                    'S1 大纲维护拒绝缺失或非字符串世界线状态 title=%s status=%r',
                    title,
                    raw_status,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='status_missing_or_not_string',
                    raw=raw,
                )
                continue
            status = raw_status.strip()
            if status not in ('seeded', 'active', 'escalated', 'closing', 'dormant', 'resolved'):
                logger.warning(
                    'S1 大纲维护拒绝非法世界线状态 title=%s status=%r',
                    title,
                    raw_status,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='status_invalid',
                    raw=raw,
                )
                continue
            raw_thread_type = raw.get('thread_type')
            if not isinstance(raw_thread_type, str):
                logger.warning(
                    'S1 大纲维护拒绝缺失或非字符串世界线类型 title=%s thread_type=%r',
                    title,
                    raw_thread_type,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='thread_type_missing_or_not_string',
                    raw=raw,
                )
                continue
            thread_type = raw_thread_type.strip()
            if thread_type not in (
                'entity', 'event', 'faction', 'location', 'omen', 'artifact',
            ):
                logger.warning(
                    'S1 大纲维护拒绝非法世界线类型 title=%s thread_type=%r',
                    title,
                    raw_thread_type,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='thread_type_invalid',
                    raw=raw,
                )
                continue
            requested_weight, _ = normalize_ascii_integer(
                raw.get('weight'),
                minimum=1,
                maximum=100,
            )
            if type(requested_weight) is not int:
                logger.warning(
                    'S1 大纲维护拒绝缺失或非法 weight title=%s weight=%r',
                    title,
                    raw.get('weight'),
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='weight_invalid',
                    raw=raw,
                )
                continue
            if change_kind == 'resolve' and status != 'resolved':
                logger.warning('S1 大纲维护拒绝 resolve 但状态未 resolved thread=%s', requested_thread_id)
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='resolve_status_mismatch',
                    raw=raw,
                )
                continue
            if change_kind == 'close' and status not in ('closing', 'dormant', 'resolved'):
                logger.warning('S1 大纲维护拒绝 close 但状态不可收束 thread=%s status=%s', requested_thread_id, status)
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='close_status_mismatch',
                    raw=raw,
                )
                continue
            if predecessor and status not in ('seeded', 'active', 'escalated'):
                logger.warning(
                    'S1 大纲维护拒绝不可推进的后继状态 predecessor=%s status=%s title=%s',
                    predecessor_id,
                    status,
                    title,
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='successor_status_not_progressable',
                    raw=raw,
                )
                continue
            if 'scope_keys' not in raw:
                logger.warning('S1 大纲维护拒绝缺失 scope_keys title=%s', title)
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='scope_keys_missing',
                    raw=raw,
                )
                continue
            scope_keys = _strict_ai_text_list(
                raw.get('scope_keys'),
                limit=8,
                item_chars=80,
            )
            inherited_location_scope_keys = (
                extract_location_scope_keys(predecessor.scope_keys)
                if predecessor
                else extract_location_scope_keys(row.scope_keys if row else [])
            )
            is_independent_new = bool(
                change_kind == 'new' and row is None and predecessor is None
            )
            continuity_authority_used = bool(
                continuity_successor_authorized
                or continuity_new_authorized
            )
            if continuity_authority_used:
                continuity_source = (
                    predecessor
                    if predecessor is not None
                    else continuity_actor_row
                )
                lineage_candidate_ids = {
                    int(value)
                    for value in (
                        getattr(continuity_source, 'source_candidate_ids', None)
                        or []
                    )
                    if int(value or 0) > 0
                }
            elif ecology_debt_authorized:
                lineage_candidate_ids = set(debt_supporting_candidate_ids)
            else:
                lineage_candidate_ids = (
                    set(new_candidate_ids)
                    if is_independent_new
                    else set(authorized_change_candidate_ids.get(
                        (source_thread_id, source_revision, change_kind),
                        set(),
                    ))
                )
            supporting_candidate_ids = (
                lineage_candidate_ids if is_independent_new else set()
            )
            if continuity_new_authorized:
                candidate_scope_state, candidate_scope_key = 'stable', dungeon
            else:
                candidate_scope_state, candidate_scope_key = resolve_candidate_scope(
                    supporting_candidate_ids,
                )
            if (
                is_independent_new
                and not trusted_without_candidates
                and candidate_scope_state == 'unresolved'
            ):
                logger.warning(
                    'S1 大纲维护延期无法唯一绑定局部来源的世界线 title=%s candidates=%s',
                    title,
                    sorted(supporting_candidate_ids),
                )
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='candidate_scope_unresolved',
                    raw=raw,
                )
                continue
            candidate_allowed_scope_keys = (
                {candidate_scope_key}
                if (
                    is_independent_new
                    and candidate_scope_state == 'stable'
                    and candidate_scope_key
                )
                else set()
            )
            scope_keys, rejected_location_scope_keys = (
                sanitize_story_location_scope_keys(
                    scope_keys,
                    allowed_location_scope_keys=(
                        candidate_allowed_scope_keys
                        | inherited_location_scope_keys
                    ),
                )
            )
            if rejected_location_scope_keys:
                logger.warning(
                    'S1 大纲维护丢弃未授权局部域引用 title=%s refs=%s',
                    title,
                    rejected_location_scope_keys,
                )
                upsert_result['warnings'].append({
                    'index': int(original_index),
                    'reason_code': 'unauthorized_scope_refs_removed',
                    'title': title,
                })
                if is_independent_new:
                    # 无精确旧线身份可承接时，整项延期，避免把局部事实扩大为地牢级。
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='independent_new_scope_unauthorized',
                        raw=raw,
                    )
                    continue
            required_location_scope_keys = (
                inherited_location_scope_keys or candidate_allowed_scope_keys
            )
            if (
                required_location_scope_keys
                and not extract_location_scope_keys(scope_keys)
            ):
                scope_keys = [
                    *sorted(required_location_scope_keys),
                    *[
                        value for value in scope_keys
                        if value not in required_location_scope_keys
                    ],
                ]
            if not row:
                if (
                    not predecessor
                    and not continuity_new_authorized
                    and S1WorldOutlineService._independent_world_story_head_blocked(
                        dungeon,
                    )
                ):
                    logger.info('S1 大纲维护冻结独立新线入场 dungeon=%s title=%s', dungeon, title)
                    _record_world_story_rejection(
                        upsert_result,
                        index=original_index,
                        reason_code='independent_new_capacity_blocked',
                        raw=raw,
                    )
                    continue
                row = AdventurerS1WorldStoryThread(title=title)
                db.session.add(row)
                if predecessor:
                    db.session.flush()
                    row.chain_key = predecessor.chain_key or f'world-story:{int(predecessor.id)}'
                    row.predecessor_id = int(predecessor.id)
                    row.is_chain_head = True
                    predecessor.is_chain_head = False
                    predecessor.superseded_by_id = int(row.id)
            if predecessor and int(predecessor.campaign_root_id or 0) > 0:
                # A successor is a new phase of the same front, not a new campaign.
                # The operator-owned agenda and exact faction identities stay frozen.
                row.campaign_root_id = int(predecessor.campaign_root_id)
                row.campaign_role = predecessor.campaign_role
                row.campaign_agenda = predecessor.campaign_agenda
            if predecessor and (
                int(predecessor.campaign_root_id or 0) > 0
                or continuity_authority_used
            ):
                # A frozen sequel preserves explicit faction identity links. This
                # transfers identity and cadence only; the new seeded phase still
                # needs a later expedition to establish action and outcomes.
                predecessor_links = (
                    AdventurerS1FactionProfileThread.query
                    .filter_by(world_story_thread_id=int(predecessor.id))
                    .order_by(AdventurerS1FactionProfileThread.id.asc())
                    .all()
                )
                existing_profile_ids = {
                    int(profile_id)
                    for profile_id, in (
                        AdventurerS1FactionProfileThread.query
                        .filter_by(world_story_thread_id=int(row.id))
                        .with_entities(
                            AdventurerS1FactionProfileThread.faction_profile_id,
                        )
                        .all()
                    )
                }
                for predecessor_link in predecessor_links:
                    profile_id = int(predecessor_link.faction_profile_id)
                    if profile_id in existing_profile_ids:
                        continue
                    db.session.add(AdventurerS1FactionProfileThread(
                        faction_profile_id=profile_id,
                        world_story_thread_id=int(row.id),
                        relation_role=predecessor_link.relation_role or 'involved',
                        source_log_id=(
                            (
                                predecessor.last_seen_log_id
                                if continuity_authority_used
                                else last_seen_log_id
                            )
                            or predecessor_link.source_log_id
                        ),
                        presence_debt=int(predecessor_link.presence_debt or 0),
                        presence_cooldown_runs=int(
                            predecessor_link.presence_cooldown_runs or 0
                        ),
                        last_presence_log_id=(
                            predecessor_link.last_presence_log_id
                        ),
                    ))
                    existing_profile_ids.add(profile_id)
            if (
                continuity_authority_used
                and continuity_target_kind.startswith('actor_')
                and continuity_actor_row is not None
            ):
                db.session.flush()
                actor_link = (
                    AdventurerS1WorldStoryThreadActor.query
                    .filter_by(
                        world_story_thread_id=int(row.id),
                        actor_id=int(continuity_actor_row.id),
                    )
                    .with_for_update()
                    .first()
                )
                if not actor_link:
                    actor_link = AdventurerS1WorldStoryThreadActor(
                        world_story_thread_id=int(row.id),
                        actor_id=int(continuity_actor_row.id),
                    )
                    db.session.add(actor_link)
                actor_link.involvement = 'direct'
                actor_link.narrative_role = 'continuity_front'
                actor_link.lease_required = True
            # AI 维护按 target_dungeon 分批，主地城归属由后端确定；AI 只能维护影响范围。
            if not row.base_dungeon:
                row.base_dungeon = str(dungeon or '').strip() or None
            row.status = status
            row.thread_type = thread_type
            row.scope_keys = scope_keys[:8]
            row.trigger_keywords = _strict_ai_text_list(
                raw.get('trigger_keywords'),
                limit=10,
                item_chars=50,
            )
            row.summary = summary
            row.next_reveal_hint = _strict_ai_text(raw.get('next_reveal_hint'))
            power_tier = _strict_ai_text(raw.get('power_tier'), limit=30)
            row.power_tier = power_tier or row.power_tier
            row.power_profile = _strict_ai_text(raw.get('power_profile')) or row.power_profile
            row.weight = requested_weight
            row.source_candidate_ids = list(dict.fromkeys(
                (row.source_candidate_ids or [])
                + sorted(lineage_candidate_ids)
            ))[-20:]
            bound_log_ids = [
                candidate_log_id_by_id[candidate_id]
                for candidate_id in lineage_candidate_ids
                if candidate_id in candidate_log_id_by_id
            ]
            row.last_seen_log_id = (
                max(bound_log_ids)
                if bound_log_ids
                else (
                    getattr(
                        predecessor if predecessor is not None else continuity_actor_row,
                        'last_seen_log_id',
                        None,
                    )
                    if continuity_authority_used
                    else last_seen_log_id
                ) or row.last_seen_log_id
            )
            if requested_thread_id > 0:
                batch_previous_revisions[int(row.id)] = int(row.revision or 1)
                row.revision = int(row.revision or 1) + 1
                row.closure_debt = 0
                row.last_progress_log_id = last_seen_log_id or row.last_progress_log_id
            if row.status == 'resolved':
                row.closure_debt = 0
            if raw.get('cycle_reseed') is True:
                applied_cycle_reseed_keys.add((
                    source_thread_id,
                    source_revision,
                    title,
                ))
            upsert_result['applied_indexes'].add(int(original_index))
            applied_rows.append((
                int(original_index),
                row,
                change_kind,
                sorted(lineage_candidate_ids),
            ))
            if continuity_authority_used:
                continuity_revival_applied = True
        rejected_indexes = {
            int(item.get('index'))
            for item in upsert_result['rejected']
            if isinstance(item, dict) and item.get('index') is not None
        }
        for original_index, raw in enumerate(raw_threads):
            if (
                original_index not in upsert_result['applied_indexes']
                and original_index not in rejected_indexes
            ):
                _record_world_story_rejection(
                    upsert_result,
                    index=original_index,
                    reason_code='consumer_unclassified_rejection',
                    raw=raw,
                )
        S1WorldOutlineService._ensure_progressable_world_story_thread(candidate_ids, dungeon=dungeon)
        if not continuity_revival_applied:
            S1WorldOutlineService._ensure_progressable_world_actor_thread(
                dungeon=dungeon,
            )
        S1WorldOutlineService._prune_world_story_threads(dungeon=dungeon)
        db.session.flush()
        upsert_result['applied'] = [
            {
                'index': original_index,
                'world_story_thread_id': int(row.id),
                'change_kind': change_kind,
                'title': _clip_text(row.title, 120),
                'candidate_ids': lineage_candidate_ids,
            }
            for original_index, row, change_kind, lineage_candidate_ids
            in applied_rows
        ]
        return upsert_result

    @staticmethod
    def _independent_world_story_head_blocked(dungeon: str) -> bool:
        """Bound new heads by capacity; closure debt schedules the existing line."""
        from db_repository.s1_encounter_preparation_repository import encounter_pool_thread_clause
        unresolved = AdventurerS1WorldStoryThread.query.filter(
            _world_story_head_clause(),
            ~encounter_pool_thread_clause(),
            AdventurerS1WorldStoryThread.base_dungeon == dungeon,
            AdventurerS1WorldStoryThread.status.in_(('seeded', 'active', 'escalated', 'closing', 'dormant')),
        )
        return unresolved.count() >= int(_world_story_capacity_snapshot(dungeon)['effective_limit'])

    @staticmethod
    def _ensure_progressable_world_story_thread(candidate_ids: list[int] | None, *, dungeon: str) -> None:
        """Keep at least one bounded public line available for future natural pickup.

        Maintenance can legitimately cool old lines to dormant/resolved. If every
        line is cooled down, the public story loop becomes write-only: candidates
        are archived but later expeditions have nothing progressable to consume.
        Only a recently touched dormant line can be re-seeded; resolved lines stay
        resolved.
        """
        from flask import current_app

        progressable = AdventurerS1WorldStoryThread.query.filter(
            _world_story_head_clause(),
            AdventurerS1WorldStoryThread.status.in_(('seeded', 'active', 'escalated', 'closing')),
            AdventurerS1WorldStoryThread.base_dungeon == dungeon,
        ).first()
        if progressable:
            return

        candidate_id_set = {
            int(cid) for cid in (candidate_ids or [])
            if str(cid).isdigit()
        }
        if not candidate_id_set:
            return

        min_weight = max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_RESEED_MIN_WEIGHT'),
            WORLD_STORY_RESEED_MIN_WEIGHT,
        ))
        rows = (
            AdventurerS1WorldStoryThread.query
            .filter(
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.status == 'dormant',
                AdventurerS1WorldStoryThread.base_dungeon == dungeon,
            )
            .order_by(
                AdventurerS1WorldStoryThread.weight.desc(),
                AdventurerS1WorldStoryThread.updated_at.desc(),
            )
            .all()
        )
        for row in rows:
            if _is_campaign_front(row):
                # Campaign owners decide real phase changes. Generic story
                # availability must not revive a just-settled campaign scene.
                continue
            source_ids = {
                int(cid) for cid in (row.source_candidate_ids or [])
                if str(cid).isdigit()
            }
            if not (source_ids & candidate_id_set):
                continue
            if int(row.weight or 0) < min_weight:
                continue
            row.status = 'seeded'
            row.next_reveal_hint = row.next_reveal_hint or '近期候选提供了新的公共抓手，后续只在自然吻合时轻量验证。'
            row.updated_at = _portable_utc_now()
            return

        if S1WorldOutlineService._independent_world_story_head_blocked(dungeon):
            return

        candidates = (
            AdventurerS1WorldFactCandidate.query
            .filter(AdventurerS1WorldFactCandidate.id.in_(candidate_id_set))
            .order_by(AdventurerS1WorldFactCandidate.created_at.desc())
            .all()
        )
        source_log_ids = {
            int(candidate.expedition_log_id)
            for candidate in candidates
            if candidate.expedition_log_id
        }
        source_full_logs = {
            int(log_id): full_log
            for log_id, full_log in (
                AdventurerS1ExpeditionLog.query
                .with_entities(
                    AdventurerS1ExpeditionLog.id,
                    AdventurerS1ExpeditionLog.full_log,
                )
                .filter(AdventurerS1ExpeditionLog.id.in_(source_log_ids))
                .all()
                if source_log_ids else []
            )
        }
        for candidate in candidates:
            updates = candidate.world_fact_updates if isinstance(candidate.world_fact_updates, list) else []
            for update in updates:
                if not isinstance(update, dict):
                    continue
                if _strict_ai_text(update.get('change_kind')).lower() != 'new':
                    continue
                title = _clip_text(update.get('title') or update.get('name'), 120)
                summary = _soft_text(
                    update.get('summary') or update.get('fact') or update.get('entry'),
                )
                if not title or not summary:
                    continue
                source_full_log = source_full_logs.get(
                    int(candidate.expedition_log_id),
                )
                source_scope_state, source_locus_scope_key = (
                    resolve_persistent_story_scope_from_full_log(
                        source_full_log,
                        expected_target_dungeon=candidate.target_dungeon,
                        expected_expedition_log_id=int(candidate.expedition_log_id),
                    )
                    if isinstance(source_full_log, dict)
                    else ('unresolved', None)
                )
                if source_scope_state == 'unresolved':
                    logger.warning(
                        'S1 大纲维护跳过尚未形成唯一稳定地点的补位世界线 candidate=%s title=%s',
                        candidate.id,
                        title,
                    )
                    continue
                fallback = AdventurerS1WorldStoryThread(
                    title=title,
                    thread_type='event',
                    status='seeded',
                    base_dungeon=candidate.target_dungeon,
                    scope_keys=[
                        candidate.target_dungeon,
                        *([source_locus_scope_key] if source_locus_scope_key else []),
                    ],
                    trigger_keywords=_normalize_text_list(update.get('tags'), limit=10, item_chars=50),
                    summary=summary,
                    next_reveal_hint='仅在后续正文与该公共事实自然吻合时轻量验证或推进。',
                    weight=max(min_weight, 50),
                    source_candidate_ids=[int(candidate.id)],
                    last_seen_log_id=int(candidate.expedition_log_id),
                )
                db.session.add(fallback)
                logger.info(
                    'S1 大纲维护从结构化公共事实补位 seeded 世界线 candidate=%s title=%s',
                    candidate.id,
                    title,
                )
                return

    @staticmethod
    def _ensure_progressable_world_actor_thread(
        *,
        dungeon: str,
        expedition_locus_scope_key: str | None = None,
        preferred_actor_ids: list[int] | None = None,
        excluded_actor_ids: set[int] | None = None,
    ) -> int | None:
        """Re-seed one eligible active actor line and return the changed thread id."""

        eligible_actor_threads = (
            AdventurerS1WorldStoryThread.query
            .join(
                AdventurerS1WorldStoryThreadActor,
                AdventurerS1WorldStoryThreadActor.world_story_thread_id
                == AdventurerS1WorldStoryThread.id,
            )
            .join(
                AdventurerS1WorldActor,
                AdventurerS1WorldActor.id
                == AdventurerS1WorldStoryThreadActor.actor_id,
            )
            .filter(
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.base_dungeon == dungeon,
                AdventurerS1WorldStoryThreadActor.involvement == 'direct',
                AdventurerS1WorldStoryThreadActor.lease_required.is_(True),
                AdventurerS1WorldActor.status == 'active',
                AdventurerS1WorldActor.capability_profile.isnot(None),
                AdventurerS1WorldActor.capability_revision > 0,
                S1CampaignWorldRepository.entity_scope(AdventurerS1WorldActor, dungeon),
                or_(AdventurerS1WorldStoryThread.campaign_root_id.is_(None),
                    AdventurerS1WorldStoryThread.campaign_root_id <= 0,
                    AdventurerS1WorldStoryThread.campaign_role.is_(None),
                    ~AdventurerS1WorldStoryThread.campaign_role.in_(WORLD_CAMPAIGN_FRONT_ROLES)),
            )
        )
        # Lock the complete eligible set before the mutable-state recheck. This
        # serializes two simultaneous launches for the same dungeon, so they
        # cannot each wake a different dormant actor line after both observed
        # the pre-lock "no progressable line" state.
        eligible_rows = (
            eligible_actor_threads
            .order_by(
                AdventurerS1WorldStoryThread.weight.desc(),
                AdventurerS1WorldStoryThread.updated_at.asc(),
                AdventurerS1WorldStoryThread.id.asc(),
            )
            .with_for_update()
            .populate_existing()
            .all()
        )
        locus_eligible_rows = [
            row for row in eligible_rows
            if story_scope_matches_expedition_locus(
                row.scope_keys,
                expedition_locus_scope_key,
            )
        ]
        eligible_thread_ids = {int(row.id) for row in locus_eligible_rows}
        actor_ids_by_thread: dict[int, set[int]] = {}
        if eligible_thread_ids:
            actor_links = AdventurerS1WorldStoryThreadActor.query.filter(
                AdventurerS1WorldStoryThreadActor.world_story_thread_id.in_(
                    eligible_thread_ids,
                ),
                AdventurerS1WorldStoryThreadActor.involvement == 'direct',
                AdventurerS1WorldStoryThreadActor.lease_required.is_(True),
            ).all()
            for link in actor_links:
                actor_ids_by_thread.setdefault(
                    int(link.world_story_thread_id), set(),
                ).add(int(link.actor_id))
        excluded = {int(value) for value in (excluded_actor_ids or set())}
        available_locus_rows = [
            row for row in locus_eligible_rows
            if not actor_ids_by_thread.get(int(row.id), set()) & excluded
        ]
        preferred_rank = {
            int(actor_id): rank
            for rank, actor_id in enumerate(preferred_actor_ids or [])
            if int(actor_id or 0) > 0 and int(actor_id) not in excluded
        }
        preferred_rows = [
            row for row in available_locus_rows
            if actor_ids_by_thread.get(int(row.id), set()) & set(preferred_rank)
        ]
        if any(
            row.status in ('seeded', 'active', 'escalated', 'closing')
            for row in preferred_rows
        ):
            return None
        preferred_dormant_rows = [
            row for row in preferred_rows
            if row.status == 'dormant'
            and bool(str(row.next_reveal_hint or '').strip())
        ]
        if preferred_dormant_rows:
            preferred_dormant_rows.sort(key=lambda candidate: (
                min(
                    preferred_rank[actor_id]
                    for actor_id in actor_ids_by_thread.get(
                        int(candidate.id), set(),
                    )
                    if actor_id in preferred_rank
                ),
                -int(candidate.weight or 0),
                candidate.updated_at or datetime.min,
                int(candidate.id),
            ))
            row = preferred_dormant_rows[0]
            row.status = 'seeded'
            row.updated_at = _portable_utc_now()
            logger.info(
                'S1 大纲维护恢复待兑现反制角色公共线 actor_thread=%s dungeon=%s',
                row.id,
                dungeon,
            )
            return int(row.id)
        if any(
            row.status in ('seeded', 'active', 'escalated', 'closing')
            for row in available_locus_rows
        ):
            return None

        row = next((
            candidate
            for candidate in available_locus_rows
            if candidate.status == 'dormant'
            and bool(str(candidate.next_reveal_hint or '').strip())
        ), None)
        if not row:
            return None
        row.status = 'seeded'
        row.updated_at = _portable_utc_now()
        logger.info(
            'S1 大纲维护恢复长期角色公共线 actor_thread=%s dungeon=%s',
            row.id,
            dungeon,
        )
        return int(row.id)

    @staticmethod
    def _find_world_story_thread_for_update(raw: dict, *, dungeon: str, title: str) -> AdventurerS1WorldStoryThread | None:
        predecessor_id = _coerce_int(raw.get('predecessor_id'), 0)
        if predecessor_id > 0:
            existing_successor = AdventurerS1WorldStoryThread.query.filter_by(title=title).with_for_update().first()
            if existing_successor and int(existing_successor.predecessor_id or 0) == predecessor_id:
                return existing_successor
            return None
        thread_id = _coerce_int(raw.get('id') or raw.get('thread_id'), 0)
        if thread_id > 0:
            row = AdventurerS1WorldStoryThread.query.filter_by(id=thread_id).with_for_update().first()
            if row and row.is_chain_head is not False:
                return row

        row = AdventurerS1WorldStoryThread.query.filter(
            AdventurerS1WorldStoryThread.title == title,
            _world_story_head_clause(),
        ).with_for_update().first()
        if row:
            return row

        # 新条目只通过显式 id、predecessor_id 或完全相同标题接续；后端不再从自然语言
        # 相似度、关键词或词元重叠猜测世界线身份。
        return None

    @staticmethod
    def _select_continuity_revival_target(
        dungeon: str,
        *,
        selection_seed: int,
    ) -> dict | None:
        """Rotate one frozen, optional sequel opportunity into ordinary maintenance."""
        from flask import current_app

        if not current_app.config.get(
            'ADVENTURER_S1_WORLD_CONTINUITY_REVIVAL_ENABLED', True,
        ):
            return None
        pool_limit = min(
            MAX_WORLD_FACT_CONTAINER_ITEMS,
            max(1, _coerce_int(
                current_app.config.get(
                    'ADVENTURER_S1_WORLD_CONTINUITY_REVIVAL_CANDIDATE_LIMIT',
                    24,
                ),
                24,
            )),
        )
        min_actor_importance = max(1, min(100, _coerce_int(
            current_app.config.get(
                'ADVENTURER_S1_WORLD_CONTINUITY_REVIVAL_MIN_ACTOR_IMPORTANCE',
                70,
            ),
            70,
        )))
        min_faction_importance = max(1, min(100, _coerce_int(
            current_app.config.get(
                'ADVENTURER_S1_WORLD_CONTINUITY_REVIVAL_MIN_FACTION_IMPORTANCE',
                70,
            ),
            70,
        )))
        min_thread_weight = max(1, min(100, _coerce_int(
            current_app.config.get(
                'ADVENTURER_S1_WORLD_CONTINUITY_REVIVAL_MIN_THREAD_WEIGHT',
                60,
            ),
            60,
        )))
        scan_limit = pool_limit
        revision_row = (
            AdventurerS1WorldOutlineDungeonState.query
            .filter_by(target_dungeon=dungeon)
            .with_entities(AdventurerS1WorldOutlineDungeonState.outline_revision)
            .first()
        )
        selection_value = abs(int(
            revision_row[0]
            if revision_row is not None
            else selection_seed or 0
        ))
        category_sequence = selection_value // 2

        def load_rotating_window(query, *, order_by: tuple) -> list:
            """Read one bounded cyclic window so older eligible rows are reachable."""
            total = int(query.order_by(None).count() or 0)
            if total <= 0:
                return []
            start = category_sequence % total
            rows = (
                query.order_by(*order_by)
                .offset(start)
                .limit(scan_limit)
                .all()
            )
            remaining = min(scan_limit, total) - len(rows)
            if remaining > 0:
                rows.extend(
                    query.order_by(*order_by)
                    .limit(remaining)
                    .all()
                )
            return rows

        actor_query = (
            AdventurerS1WorldActor.query
            .filter(
                AdventurerS1WorldActor.status == 'active',
                AdventurerS1WorldActor.importance >= min_actor_importance,
                AdventurerS1WorldActor.capability_profile.isnot(None),
                AdventurerS1WorldActor.capability_revision > 0,
                S1CampaignWorldRepository.entity_scope(AdventurerS1WorldActor, dungeon),
            )
        )
        actors = load_rotating_window(
            actor_query,
            order_by=(
                AdventurerS1WorldActor.importance.desc(),
                AdventurerS1WorldActor.updated_at.asc(),
                AdventurerS1WorldActor.id.asc(),
            ),
        )
        actor_ids = [int(actor.id) for actor in actors]
        linked_actor_ids = {
            int(actor_id)
            for actor_id, in (
                db.session.query(AdventurerS1WorldStoryThreadActor.actor_id)
                .filter(
                    AdventurerS1WorldStoryThreadActor.actor_id.in_(actor_ids)
                )
                .distinct()
                .all()
            )
        } if actor_ids else set()
        mutable_actor_ids = {
            int(actor_id)
            for actor_id, in (
                db.session.query(AdventurerS1WorldStoryThreadActor.actor_id)
                .join(
                    AdventurerS1WorldStoryThread,
                    AdventurerS1WorldStoryThread.id
                    == AdventurerS1WorldStoryThreadActor.world_story_thread_id,
                )
                .filter(
                    AdventurerS1WorldStoryThreadActor.actor_id.in_(actor_ids),
                    _world_story_head_clause(),
                    AdventurerS1WorldStoryThread.status.in_(
                        WORLD_INTEL_MUTABLE_STATUSES,
                    ),
                )
                .distinct()
                .all()
            )
        } if actor_ids else set()
        terminal_actor_links = (
            db.session.query(
                AdventurerS1WorldStoryThreadActor.actor_id,
                AdventurerS1WorldStoryThread,
            )
            .join(
                AdventurerS1WorldStoryThread,
                AdventurerS1WorldStoryThread.id
                == AdventurerS1WorldStoryThreadActor.world_story_thread_id,
            )
            .filter(
                AdventurerS1WorldStoryThreadActor.actor_id.in_(actor_ids),
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.status.in_(
                    WORLD_INTEL_TERMINAL_STATUSES,
                ),
                AdventurerS1WorldStoryThread.campaign_role.is_(None),
                S1CampaignWorldRepository.thread_scope(dungeon),
            )
            .order_by(
                AdventurerS1WorldStoryThreadActor.actor_id.asc(),
                AdventurerS1WorldStoryThread.updated_at.asc(),
                AdventurerS1WorldStoryThread.id.asc(),
            )
            .limit(max(1, scan_limit * 8))
            .all()
            if actor_ids else []
        )
        actor_thread_rows: dict[int, list[AdventurerS1WorldStoryThread]] = {}
        for actor_id, thread in terminal_actor_links:
            actor_thread_rows.setdefault(int(actor_id), []).append(thread)

        def thread_relevant(row: AdventurerS1WorldStoryThread) -> bool:
            base_dungeon = _effective_world_story_base_dungeon(row)
            scope_keys = {
                str(value).strip()
                for value in (row.scope_keys or [])
                if str(value).strip()
            }
            return bool(
                base_dungeon in {dungeon, WORLD_INTEL_ALL_DUNGEONS_SCOPE}
                or dungeon in scope_keys
                or WORLD_INTEL_ALL_DUNGEONS_SCOPE in scope_keys
            )

        actor_candidates: list[dict] = []
        actor_predecessor_ids: set[int] = set()
        for actor in actors:
            actor_id = int(actor.id)
            if actor_id in mutable_actor_ids:
                continue
            thread_rows = actor_thread_rows.get(int(actor.id), [])
            terminal_rows = [
                row for row in thread_rows
                if thread_relevant(row)
            ]
            terminal_rows.sort(key=lambda row: (
                row.status == 'resolved',
                not bool(str(row.next_reveal_hint or '').strip()),
                -int(row.weight or 0),
                row.updated_at or datetime.min,
                int(row.id),
            ))
            predecessor = terminal_rows[0] if terminal_rows else None
            if predecessor is None and actor_id in linked_actor_ids:
                # A linked actor without a bounded, relevant terminal predecessor
                # is not a safe independent-front target. The transactional
                # consumer still rechecks all links before any write.
                continue
            actor_scope_keys = {
                str(value).strip()
                for value in (actor.scope_keys or [])
                if str(value).strip()
            }
            actor_scope_relevant = bool(
                not actor_scope_keys
                or dungeon in actor_scope_keys
                or WORLD_INTEL_ALL_DUNGEONS_SCOPE in actor_scope_keys
            )
            if predecessor is None and not actor_scope_relevant:
                continue
            target_kind = (
                'actor_successor' if predecessor else 'actor_new_front'
            )
            target = {
                'target_kind': target_kind,
                'world_actor_id': int(actor.id),
                'capability_revision': int(actor.capability_revision or 0),
            }
            if predecessor is not None:
                target.update({
                    'predecessor_id': int(predecessor.id),
                    'source_revision': max(1, int(predecessor.revision or 1)),
                })
                actor_predecessor_ids.add(int(predecessor.id))
            actor_candidates.append(target)

        dormant_query = (
            AdventurerS1WorldStoryThread.query
            .filter(
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.status == 'dormant',
                AdventurerS1WorldStoryThread.campaign_role.is_(None),
                AdventurerS1WorldStoryThread.next_reveal_hint.isnot(None),
                S1CampaignWorldRepository.thread_scope(dungeon),
            )
        )
        dormant_threads = load_rotating_window(
            dormant_query,
            order_by=(
                AdventurerS1WorldStoryThread.weight.desc(),
                AdventurerS1WorldStoryThread.updated_at.asc(),
                AdventurerS1WorldStoryThread.id.asc(),
            ),
        )
        relevant_dormant_threads = [
            row for row in dormant_threads
            if (
                int(row.id) not in actor_predecessor_ids
                and bool(str(row.next_reveal_hint or '').strip())
                and thread_relevant(row)
            )
        ]
        dormant_thread_ids = [int(row.id) for row in relevant_dormant_threads]
        profile_links = (
            AdventurerS1FactionProfileThread.query
            .filter(
                AdventurerS1FactionProfileThread.world_story_thread_id.in_(
                    dormant_thread_ids,
                ),
            )
            .all()
            if dormant_thread_ids else []
        )
        profile_ids = list(dict.fromkeys(
            int(link.faction_profile_id) for link in profile_links
        ))
        profiles = (
            AdventurerS1FactionProfile.query
            .filter(
                AdventurerS1FactionProfile.id.in_(profile_ids),
                AdventurerS1FactionProfile.status != 'dissolved',
                S1CampaignWorldRepository.entity_scope(AdventurerS1FactionProfile, dungeon),
            )
            .all()
            if profile_ids else []
        )
        profile_importance = {
            int(profile.id): int(profile.importance or 0)
            for profile in profiles
        }
        thread_profile_importance: dict[int, int] = {}
        for link in profile_links:
            importance = profile_importance.get(int(link.faction_profile_id), 0)
            if importance <= 0:
                continue
            thread_id = int(link.world_story_thread_id)
            thread_profile_importance[thread_id] = max(
                importance,
                thread_profile_importance.get(thread_id, 0),
            )
        thread_candidates: list[dict] = []
        for row in relevant_dormant_threads:
            linked_importance = thread_profile_importance.get(int(row.id), 0)
            if (
                int(row.weight or 0) < min_thread_weight
                and linked_importance < min_faction_importance
            ):
                continue
            thread_candidates.append({
                'target_kind': 'thread_successor',
                'predecessor_id': int(row.id),
                'source_revision': max(1, int(row.revision or 1)),
            })

        # Even/odd source logs alternate the preferred category. Each category's
        # cyclic window advances only after both turns, so every eligible row can
        # eventually become the first valid target instead of inheriting a modulo
        # bias from the merged candidate count.
        preferred, fallback = (
            (actor_candidates, thread_candidates)
            if selection_value % 2 == 0
            else (thread_candidates, actor_candidates)
        )
        chosen_pool = preferred or fallback
        return dict(chosen_pool[0]) if chosen_pool else None

    @staticmethod
    def _load_world_story_threads_for_maintenance(
        dungeon: str,
        context_text: str | None = None,
        *,
        required_thread_ids: set[int] | None = None,
        mandatory_thread_ids: set[int] | None = None,
    ) -> list[dict]:
        from flask import current_app

        mandatory_ids = {
            _coerce_int(value, 0)
            for value in (mandatory_thread_ids or set())
            if _coerce_int(value, 0) > 0
        }
        required_ids = {
            _coerce_int(value, 0)
            for value in (required_thread_ids or set())
            if _coerce_int(value, 0) > 0
        } | mandatory_ids
        search_text, anchor_terms = _build_world_intel_context(dungeon, context_text)
        context_limit = max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_MAINTENANCE_CONTEXT_LIMIT'),
            WORLD_STORY_MAINTENANCE_CONTEXT_LIMIT,
        ))
        if len(mandatory_ids) > context_limit:
            raise ValueError('campaign_required_world_story_context_over_limit')
        stale_reserve = min(context_limit, max(0, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_MAINTENANCE_STALE_RESERVE'), 4,
        )))
        stale_days = max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_CLOSURE_STALE_DAYS'), 7,
        ))
        stale_cutoff = _portable_utc_now() - timedelta(days=stale_days)
        rows = (
            AdventurerS1WorldStoryThread.query
            .filter(
                _world_story_head_clause(),
                or_(
                    AdventurerS1WorldStoryThread.campaign_role.is_(None),
                    AdventurerS1WorldStoryThread.campaign_role
                    != WORLD_CAMPAIGN_SPINE_ROLE,
                ),
                S1CampaignWorldRepository.thread_scope(dungeon),
            )
            .order_by(AdventurerS1WorldStoryThread.weight.desc(), AdventurerS1WorldStoryThread.updated_at.desc())
            .all()
        )
        _hydrate_legacy_world_story_base_dungeons(rows)
        matched: list[tuple[AdventurerS1WorldStoryThread, dict]] = []
        for row in rows:
            keys = [str(k).strip() for k in (row.scope_keys or []) if str(k).strip()]
            keywords = [str(k).strip() for k in (row.trigger_keywords or []) if str(k).strip()]
            score = S1WorldOutlineService._score_world_story_thread_for_context(
                row,
                dungeon,
                search_text,
                anchor_terms,
                set(),
            )
            if score <= 0 and int(row.id) not in required_ids:
                continue
            matched.append((row, {
                'id': int(row.id),
                'title': row.title or '',
                'thread_type': row.thread_type or 'event',
                'status': row.status or 'active',
                'base_dungeon': _effective_world_story_base_dungeon(row),
                'scope_keys': keys[:8],
                'trigger_keywords': keywords[:10],
                'summary': row.summary or '',
                'next_reveal_hint': row.next_reveal_hint or '',
                'power_tier': row.power_tier or '',
                'power_profile': row.power_profile or '',
                'weight': int(row.weight or 0),
                'revision': int(row.revision or 1),
                'closure_debt': int(row.closure_debt or 0),
                'injection_count': int(row.injection_count or 0),
                'missed_injection_runs': int(row.missed_injection_runs or 0),
                'chain_key': row.chain_key or '',
                'predecessor_id': row.predecessor_id,
                'campaign_root_id': row.campaign_root_id,
                'campaign_role': row.campaign_role or '',
                'campaign_agenda': row.campaign_agenda or '',
                'match_score': score,
                'updated_at': row.updated_at.isoformat() if row.updated_at else None,
            }))
        priority = sorted(
            matched,
            key=lambda item: (
                -int(item[1].get('match_score') or 0),
                -int(item[1].get('weight') or 0),
                -(item[0].updated_at.timestamp() if item[0].updated_at else 0),
            ),
        )
        stale = sorted(
            (
                item for item in matched
                if item[0].status in ('seeded', 'active', 'escalated', 'closing', 'dormant')
                and item[0].updated_at
                and item[0].updated_at <= stale_cutoff
            ),
            key=lambda item: (item[0].updated_at, int(item[0].weight or 0), int(item[0].id)),
        )
        selected = priority[:max(0, context_limit - stale_reserve)]
        selected_ids = {int(item[0].id) for item in selected}
        for item in stale:
            if int(item[0].id) in selected_ids:
                continue
            selected.append(item)
            selected_ids.add(int(item[0].id))
            if len(selected) >= context_limit:
                break
        if len(selected) < context_limit:
            for item in priority:
                if int(item[0].id) in selected_ids:
                    continue
                selected.append(item)
                selected_ids.add(int(item[0].id))
                if len(selected) >= context_limit:
                    break
        forced_ids = mandatory_ids | set(
            sorted(required_ids - mandatory_ids)[:context_limit - len(mandatory_ids)]
        )
        matched_by_id = {int(item[0].id): item for item in matched}
        if mandatory_ids - matched_by_id.keys():
            raise ValueError('campaign_required_world_story_context_missing')
        for required_id in sorted(forced_ids):
            if required_id in selected_ids or required_id not in matched_by_id:
                continue
            if len(selected) >= context_limit:
                replace_index = next((
                    index for index in range(len(selected) - 1, -1, -1)
                    if int(selected[index][0].id) not in forced_ids
                ), None)
                if replace_index is None:
                    continue
                removed = selected.pop(replace_index)
                selected_ids.discard(int(removed[0].id))
            selected.append(matched_by_id[required_id])
            selected_ids.add(required_id)
        return [item[1] for item in selected]

    @staticmethod
    def _load_faction_agenda_catalog_for_maintenance(
        *,
        excluded_thread_ids: set[int] | None = None,
    ) -> list[dict]:
        """加载其它势力线的有界只读当前态，供维护 AI 判断利益是否真实相交。"""
        from flask import current_app

        context_limit = max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_MAINTENANCE_CONTEXT_LIMIT'),
            WORLD_STORY_MAINTENANCE_CONTEXT_LIMIT,
        ))
        excluded = {int(value) for value in (excluded_thread_ids or set())}
        rows = (
            AdventurerS1WorldStoryThread.query
            .filter(
                _world_story_head_clause(),
                AdventurerS1WorldStoryThread.thread_type == 'faction',
                AdventurerS1WorldStoryThread.status.in_((
                    'seeded', 'active', 'escalated', 'closing', 'dormant',
                )),
            )
            .order_by(
                AdventurerS1WorldStoryThread.weight.desc(),
                AdventurerS1WorldStoryThread.updated_at.desc(),
                AdventurerS1WorldStoryThread.id.asc(),
            )
            .limit(context_limit + len(excluded))
            .all()
        )
        _hydrate_legacy_world_story_base_dungeons(rows)
        result = []
        for row in rows:
            if int(row.id) in excluded:
                continue
            result.append({
                'id': int(row.id),
                'revision': int(row.revision or 1),
                'title': row.title or '',
                'status': row.status or 'active',
                'base_dungeon': _effective_world_story_base_dungeon(row),
                'scope_keys': [
                    str(value).strip()
                    for value in (row.scope_keys or [])
                    if str(value).strip()
                ][:8],
                'summary': row.summary or '',
                'next_reveal_hint': row.next_reveal_hint or '',
                'power_tier': row.power_tier or '',
                'power_profile': row.power_profile or '',
                'weight': int(row.weight or 0),
            })
            if len(result) >= context_limit:
                break
        return result

    @staticmethod
    def _prune_world_story_threads(*, dungeon: str | None = None) -> None:
        """Warn on adaptive per-dungeon pressure; never delete or truncate a chain."""
        from db_repository.s1_encounter_preparation_repository import encounter_pool_thread_clause
        rows = AdventurerS1WorldStoryThread.query.filter(
            _world_story_head_clause(),
            ~encounter_pool_thread_clause(),
            AdventurerS1WorldStoryThread.status.in_(('seeded', 'active', 'escalated', 'closing', 'dormant')),
            or_(
                AdventurerS1WorldStoryThread.campaign_role.is_(None),
                AdventurerS1WorldStoryThread.campaign_role
                != WORLD_CAMPAIGN_SPINE_ROLE,
            ),
        ).all()
        _hydrate_legacy_world_story_base_dungeons(rows)
        counts: dict[str, int] = {}
        requested_dungeon = str(dungeon or '').strip()
        for row in rows:
            base_dungeon = _effective_world_story_base_dungeon(row)
            if not base_dungeon or (requested_dungeon and base_dungeon != requested_dungeon):
                continue
            counts[base_dungeon] = counts.get(base_dungeon, 0) + 1
        if requested_dungeon and requested_dungeon not in counts:
            counts[requested_dungeon] = 0
        for base_dungeon, total in counts.items():
            capacity = _world_story_capacity_snapshot(base_dungeon)
            if total <= int(capacity['effective_limit']):
                continue
            logger.warning(
                'S1 地牢可触发世界剧情链 head 超过动态软额度 dungeon=%s total=%s limit=%s '
                'normal=%s active_expeditions=%s recent_expeditions=%s；保留全部旧链，等待维护 AI 公平收束',
                base_dungeon,
                total,
                capacity['effective_limit'],
                capacity['normal_limit'],
                capacity['active_expeditions'],
                capacity['recent_expeditions'],
            )

    @staticmethod
    def _maintain_one_dungeon(
        dungeon: str,
        candidate_ids: list[int],
        *,
        cycle_reseed_only: bool = False,
        usage_handles: list[_DeferredWorldOutlineTokenUsage] | None = None,
        usage_context: dict | None = None,
    ) -> str | dict | _WorldLocationMaintenanceRejection:
        owns_usage_handles = usage_handles is None
        active_usage_handles = usage_handles if usage_handles is not None else []
        consumer_ready = False
        review_rejected = False
        failure_stage = None
        validation_error_code = None
        try:
            result = S1WorldOutlineService._maintain_one_dungeon_impl(
                dungeon,
                candidate_ids,
                cycle_reseed_only=cycle_reseed_only,
                usage_handles=active_usage_handles,
                usage_context=usage_context,
            )
            review_rejected = isinstance(result, _WorldLocationMaintenanceRejection)
            if review_rejected:
                failure_stage = 'maintenance_review'
                validation_error_code = WORLD_LOCATION_MAINTENANCE_REJECTED
            if isinstance(result, dict) and not cycle_reseed_only:
                from plugins.adventurer_s1.services.s1_encounter_preparation_service import prepare
                result['_encounter_preparation'] = prepare(
                    dungeon, candidate_ids, maintenance_plan=result,
                )
                from plugins.adventurer_s1.services.s1_first_encounter_learning_service import prepare_maintenance
                result['_first_encounter_learning'] = prepare_maintenance(dungeon, candidate_ids, result)
            consumer_ready = not review_rejected
            return result
        except FirstEncounterLearningError as exc:
            failure_stage = exc.failure_stage or 'maintenance_ai'
            validation_error_code = _classify_world_outline_maintenance_failure(exc)
            raise
        finally:
            if owns_usage_handles:
                _finalize_world_outline_usage_handles(
                    active_usage_handles,
                    consumer_ready,
                    failure_stage=failure_stage,
                    validation_error_code=validation_error_code,
                )

    @staticmethod
    def _maintain_one_dungeon_impl(
        dungeon: str,
        candidate_ids: list[int],
        *,
        cycle_reseed_only: bool,
        usage_handles: list[_DeferredWorldOutlineTokenUsage],
        usage_context: dict | None = None,
    ) -> str | dict | _WorldLocationMaintenanceRejection:
        from flask import current_app
        from plugins.adventurer_s1.services.s1_campaign_dispatch_contract import (
            DISPATCH_LOG_FIELD, DISPATCH_MAINTENANCE_RULE,
            project_dispatch_facts, project_dispatch_maintenance_change,
            validate_dispatch_maintenance_changes,
        )

        candidates = (
            AdventurerS1WorldFactCandidate.query
            .filter(AdventurerS1WorldFactCandidate.id.in_(candidate_ids))
            .order_by(AdventurerS1WorldFactCandidate.created_at.asc())
            .all()
        )
        if not candidates and not cycle_reseed_only:
            raise ValueError('候选不存在或已被处理')

        current_state = AdventurerS1DmHandoffState.query.filter_by(target_dungeon=dungeon).first()
        current_note = _soft_text(getattr(current_state, 'note', '') or '')
        payload = []
        candidate_log_ids = [int(c.expedition_log_id) for c in candidates]
        alias_contexts = _load_alias_contexts_for_logs(candidate_log_ids)
        source_logs = AdventurerS1ExpeditionLog.query.filter(
            AdventurerS1ExpeditionLog.id.in_(candidate_log_ids),
        ).all()
        source_capability_profiles = {}
        source_encounter_definitions = {}
        source_ecology_debts = {}
        source_scope_by_log_id = {}
        source_faction_presence_story_evidence = {}
        source_campaign_contexts = {}
        campaign_required_changes = []
        source_player_names = []
        for source_log in source_logs:
            full_log = source_log.full_log if isinstance(source_log.full_log, dict) else {}
            source_campaign_contexts[int(source_log.id)] = full_log.get(DISPATCH_LOG_FIELD)
            source_player_names.extend(frozen_player_names(full_log))
            objective_outline = full_log.get('objective_outline')
            from plugins.adventurer_s1.services.s1_capability_profile_contract import (
                project_encounter_definition_for_ai,
            )
            source_encounter_definitions[int(source_log.id)] = (
                project_encounter_definition_for_ai(objective_outline)
            )
            profiles = (
                objective_outline.get('encounter_capability_profiles')
                if isinstance(objective_outline, dict) else []
            )
            source_capability_profiles[int(source_log.id)] = (
                profiles if isinstance(profiles, list) else []
            )
            source_ecology_debts[int(source_log.id)] = (
                _project_ecology_debt_for_maintenance(full_log)
            )
            source_scope_by_log_id[int(source_log.id)] = (
                resolve_persistent_story_scope_from_full_log(
                    full_log,
                    expected_target_dungeon=dungeon,
                    expected_expedition_log_id=int(source_log.id),
                )
                if isinstance(source_log.full_log, dict)
                else ('unresolved', None)
            )
            source_faction_presence_story_evidence[int(source_log.id)] = (
                _project_faction_presence_story_evidence(full_log)
            )
        for c in candidates:
            alias_context = alias_contexts.get(int(c.expedition_log_id), {})
            codename_to_real = alias_context.get('codename_to_real') or {}
            if not cycle_reseed_only:
                required_change = project_dispatch_maintenance_change(
                    c.world_fact_updates,
                    source_campaign_contexts.get(int(c.expedition_log_id)),
                )
                if required_change:
                    campaign_required_changes.append({
                        'candidate_id': int(c.id),
                        'expedition_log_id': int(c.expedition_log_id),
                        **required_change,
                    })
            payload.append({
                'candidate_id': c.id,
                'expedition_log_id': c.expedition_log_id,
                'member_alias_scope': alias_context.get('alias_rows') or [],
                # The maintenance AI owns established world facts. The approved
                # notice and future invitation remain private consumer input.
                'world_fact_updates': _restore_codenames_in_value(
                    project_dispatch_facts(c.world_fact_updates, None), codename_to_real,
                ),
                'settled_encounter_capability_profiles': _restore_codenames_in_value(
                    source_capability_profiles.get(int(c.expedition_log_id), []),
                    codename_to_real,
                ),
                'encounter_definition': source_encounter_definitions.get(
                    int(c.expedition_log_id), {},
                ),
                'ecology_continuity_debt': _restore_codenames_in_value(
                    source_ecology_debts.get(int(c.expedition_log_id), {}),
                    codename_to_real,
                ),
                'faction_presence_story_evidence': _restore_codenames_in_value(
                    source_faction_presence_story_evidence.get(
                        int(c.expedition_log_id), [],
                    ),
                    codename_to_real,
                ),
                'dm_handoff_note_candidate': _soft_text(
                    restore_names_in_narrative(c.dm_handoff_note_candidate or '', codename_to_real),
                ),
            })
        has_ecology_continuity_debt = any(
            isinstance(row.get('ecology_continuity_debt'), dict)
            and bool(row.get('ecology_continuity_debt'))
            for row in payload
        )
        ecology_continuity_debt_log_ids = sorted({
            int(row['expedition_log_id'])
            for row in payload
            if isinstance(row.get('ecology_continuity_debt'), dict)
            and bool(row.get('ecology_continuity_debt'))
        })
        maintenance_context_text = _as_context_text(current_note, payload)
        faction_presence_thread_ids = {
            _coerce_int(evidence.get('world_story_thread_id'), 0)
            for row in payload
            if isinstance(row, dict)
            for evidence in row.get('faction_presence_story_evidence') or []
            if isinstance(evidence, dict)
            and _coerce_int(evidence.get('world_story_thread_id'), 0) > 0
        }
        continuity_revival_target = (
            None
            if cycle_reseed_only
            else S1WorldOutlineService._select_continuity_revival_target(
                dungeon,
                selection_seed=max(candidate_log_ids or candidate_ids or [0]),
            )
        )
        continuity_predecessor_id = _coerce_int(
            continuity_revival_target.get('predecessor_id'), 0,
        ) if isinstance(continuity_revival_target, dict) else 0
        continuity_actor_id = _coerce_int(
            continuity_revival_target.get('world_actor_id'), 0,
        ) if isinstance(continuity_revival_target, dict) else 0
        existing_threads = S1WorldOutlineService._load_world_story_threads_for_maintenance(
            dungeon,
            context_text=maintenance_context_text,
            required_thread_ids={
                *faction_presence_thread_ids,
                *(
                    {continuity_predecessor_id}
                    if continuity_predecessor_id > 0 else set()
                ),
            },
            mandatory_thread_ids={
                row['source_world_story_thread_id'] for row in campaign_required_changes
            },
        )
        world_story_write_authority = _build_world_story_write_authority(
            payload,
            existing_threads,
            source_scope_by_log_id=source_scope_by_log_id,
        )
        from plugins.adventurer_s1.services.s1_world_location_state_service import (
            S1WorldLocationStateService,
        )
        authoritative_location_state_snapshot = S1WorldLocationStateService.build_snapshot(
            dungeon,
            preserve_persisted_explorability=True,
            include_internal_locus_metadata=True,
        )
        authoritative_location_state_snapshot = {
            key: authoritative_location_state_snapshot[key]
            for key in ('target_dungeon', 'is_explorable', 'captured_at', 'locations')
        }
        # Campaign publication persists settled facts. Optional forward
        # simulation must not share its publication gate or global owner lock.
        allow_location_evolution = bool(
            not cycle_reseed_only
            and not campaign_required_changes
            and authoritative_location_state_snapshot.get('is_explorable') is True
            and any(
                row.get('_write_available') is True
                for row in authoritative_location_state_snapshot.get('locations', [])
            )
        )
        faction_agenda_catalog = [] if campaign_world(dungeon) else S1WorldOutlineService._load_faction_agenda_catalog_for_maintenance(
            excluded_thread_ids={
                int(row.get('id'))
                for row in existing_threads
                if isinstance(row, dict) and row.get('id')
            },
        )
        from plugins.adventurer_s1.services.s1_world_actor_service import (
            build_actor_name_registry,
            build_maintenance_actor_context,
        )
        existing_world_actors = build_maintenance_actor_context(
            [
                _coerce_int(row.get('id'), 0)
                for row in existing_threads
                if isinstance(row, dict) and row.get('id')
            ],
            required_actor_ids=(
                [continuity_actor_id] if continuity_actor_id > 0 else []
            ),
            target_dungeon=dungeon,
        )
        world_actor_name_registry = [] if campaign_world(dungeon) else build_actor_name_registry(
            priority_actor_ids=[row['id'] for row in existing_world_actors],
        )
        from plugins.adventurer_s1.services.s1_faction_profile_service import (
            build_bootstrap_plan as build_faction_profile_bootstrap_plan,
            build_maintenance_context as build_faction_profile_maintenance_context,
            build_name_registry as build_faction_profile_name_registry,
        )
        existing_faction_profiles = (
            []
            if cycle_reseed_only
            else build_faction_profile_maintenance_context(sorted({
                *faction_presence_thread_ids,
                *(
                    _coerce_int(row.get('id'), 0)
                    for row in existing_threads
                    if isinstance(row, dict) and row.get('id')
                ),
            }), target_dungeon=dungeon)
        )
        faction_profile_name_registry = (
            [] if cycle_reseed_only or campaign_world(dungeon) else build_faction_profile_name_registry()
        )
        faction_profile_bootstrap_plan = None
        if not cycle_reseed_only:
            try:
                # 冷启动只是辅助上下文。独立 savepoint 保证查询/投影异常不会
                # 污染本轮普通世界维护事务，更不能据此重试维护 AI。
                with db_manager.begin_nested():
                    faction_profile_bootstrap_plan = (
                        build_faction_profile_bootstrap_plan(existing_threads)
                    )
            except Exception as exc:
                logger.warning(
                    'S1 势力档案冷启动上下文构建失败，已降级为空且不重试 AI: %s',
                    exc,
                    exc_info=True,
                )
        capacity = _world_story_capacity_snapshot(dungeon)
        from db_repository.s1_encounter_preparation_repository import encounter_pool_thread_clause
        current_active_heads = int(AdventurerS1WorldStoryThread.query.filter(
            _world_story_head_clause(),
            ~encounter_pool_thread_clause(),
            AdventurerS1WorldStoryThread.base_dungeon == dungeon,
            AdventurerS1WorldStoryThread.status.in_(('seeded', 'active', 'escalated', 'closing', 'dormant')),
        ).count())
        max_ai_updates = max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_STORY_THREADS_MAX_AI_UPDATES'), 12,
        ))
        max_world_actor_updates = min(
            MAX_WORLD_FACT_CONTAINER_ITEMS,
            max(0, _coerce_int(
                current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_MAX_AI_UPDATES'),
                3,
            )),
        )
        max_faction_profile_updates = min(16, max(1, _coerce_int(
            current_app.config.get('ADVENTURER_S1_FACTION_PROFILE_MAX_AI_UPDATES'),
            8,
        )))
        faction_profile_bootstrap_context = None
        if faction_profile_bootstrap_plan:
            bootstrap_limit = min(
                max_faction_profile_updates,
                max(0, _coerce_int(
                    faction_profile_bootstrap_plan['prompt_context'].get(
                        'max_new_profiles'
                    ),
                    0,
                )),
            )
            if bootstrap_limit > 0:
                faction_profile_bootstrap_context = dict(
                    faction_profile_bootstrap_plan['prompt_context']
                )
                faction_profile_bootstrap_context['max_new_profiles'] = (
                    bootstrap_limit
                )
                faction_profile_bootstrap_plan['source_override'][
                    'max_new_profiles'
                ] = bootstrap_limit
            else:
                faction_profile_bootstrap_plan = None

        # 世界大纲使用自身明确分配的路由；请求闸门仍会在每次物理调用前
        # 重新解析，以便管理员变更及时生效。
        route = resolve_s1_ai_profile_snapshot('world_outline')
        api_key = route.api_key
        api_base = route.api_base
        model = route.model
        timeout = _coerce_int(
            current_app.config.get('ADVENTURER_S1_WORLD_OUTLINE_AI_TIMEOUT_SECONDS', 600),
            600,
        )
        tavern_faction_scope_rule = (
            '本活动的势力关系只保存活动内参与方及已经形成的持续关系；冒险者与当地势力的交易或协作仍可成为公共事实，作用域保持活动地图，不建立 abyss_tavern 旧世界节点或跨世界关系。'
            if campaign_world(dungeon) else (
                '深渊酒馆本身是明确的势力节点；玩家冒险者默认都属于或代表深渊酒馆冒险者生态。势力线可以维护某个势力自己的内部故事，也可以维护两个或多个势力之间的大事；不是每条势力线都必须涉及深渊酒馆。'
                '当且仅当已结算候选已经建立某个外部势力与深渊酒馆冒险者生态之间持续有效的行动、机会、阻力、交易、协作、监视、竞争、索取、威胁或其它关系阶段时，该 faction 世界线的 scope_keys 必须加入精确保留键 abyss_tavern；只知晓酒馆存在、在酒馆活动范围附近、可能未来接触或单方面产生尚未行动的想法都不得加入。阶段关系已经终止且后续不再需要酒馆作为作用对象时移除该键。'
                '带 abyss_tavern 的未收束 faction 线若 next_reveal_hint 非空，hint 只写既有因果支持的下一项可验证外部行动或交互条件，不预写酒馆队伍会如何回应、谁胜谁负或最终代价。正面、负面和复杂交换均服从已结算事实，不设置轮流、敌友或冲突配额。'
            )
        )
        system_prompt = (
            '你是深渊酒馆 S1 的大纲维护 AI，把 new_candidates 中本批已结算的变化保存为后续探险可继承的当前事实。先判断这次改变了什么，再选择承载该变化的档案；未变化的旧档案保持原状。'
            'dm_handoff_note 是内部连续性备注；world_story_threads 是公共世界线档案；地点当前态按下方本轮权限处理。'
            '深渊酒馆 S1 是约 200 个冒险者自由组队、反复探索不同副本的群像世界，冒险者死亡后会补充新人；未来进入该副本的通常不是本次队伍。'
            'dm_handoff_note 不是世界事实摘要、战报压缩、工程清单、状态档案、固定接戏指南或任务清单。它可以是任何能让未来 AI 写得更自由、更有味道、更有连续性、更不重复的内部备注。'
            '你可以大幅删改、压缩、重写 current_dm_handoff_note；旧便签不是必须保留的事实来源，过时、重复、档案味太重、只复述客观状态或围绕本队成员个人后续的内容应移除。'
            '便签优先保留抽象创作提醒：表达口味、使用边界、节奏偏好、需要保留的距离感、暂不推进的方向、避免重复的方向或其它不适合写成事实档案的余味。'
            '不要在 dm_handoff_note 中长篇复述客观事实；这类内容应沉淀到 world_story_threads。'
            '不要保留本场战报、成员个人伤势、装备/战利品分配、战术过程、结算建议、个人装备维修或个人物品持有。'
            'dm_handoff_note 只承载公共创作边界与可复用连续性。成员个人持有物继续归属该成员；物品进入公共层时，只记录已经发生的公共状态变化、公开证据流向或公共机制。'
            '便签可以散文化、口语化；不必固定格式，不必每句都能直接接戏，也不要求本次触发。'
            '【世界线换挡协议】公共世界线必须有生命周期：seeded=新方向刚出现；active=局部/区域正在影响；escalated=阶段性高潮；closing=已有收束债务、应优先形成结果；dormant=阶段结束仅作背景；resolved=已明确收束。'
            '本轮以 new_candidates 的已结算变化为工作集，按下方 world_story_write_authority 合同确定实际可写对象。候选为旧线提供真实结果与准确修改授权时，优先处理其收束或推进；其余旧线继承当前态。反复出现的同类母题，可在已授权变化中合并、降权或归档，并在便签中调整后续表达。'
            'user payload 的 world_story_capacity 是按该地牢近期探险热度与当前并发计算的动态额度，不是删除阈值；对独立新 head 是后端硬入场门。独立 new 由候选真实建立的新公共事实与准确授权支持，入场后 current_active_heads 须保持在 effective_limit 内。旧线的 closing/closure_debt 继续影响该线的召回与收束优先级，容量有余时其它有据的新公共变化仍可入场。普通后继仍只在旧阶段已形成结果且候选真实建立新状态时允许。已有历史与未被本批改变的旧线状态继续保留。'
            '候选事实可以改变旧线的关系、权重或阶段，也可以成为新公共方向。只有候选已经改变后续需要继承的公共事实、已知结论、主体行动状态、关系边界、目标状态或处置结果时才算 advance；只重述旧摘要、提高不确定性或加强未来可能性时不更新。旧线收束不是删除历史，而是保留后续仍需知道的阶段结果与余波边界。'
            '若候选明确带回某条旧世界线准确专名及其正文结果，应优先判断主要矛盾是否已经形成可审计结论：足够则更新原 id 为 resolved，并把 summary 改写为最终公共结果；只有阶段结束但仍存在明确未解公共后果时才用 dormant。不得用新谜团、更大威胁或新幕后主体替代已经可以收束的问题。没有正文结果时，不能只因长期未出现就虚构 resolved。'
            '【副本概念连续性】target_dungeon 是允许长期反复探索的主题身份，不与任一阶段的具体表现绑定。既成阶段结果必须继续有效；普通世界线维护只处理本批候选已经建立的当前变化，候选没有建立可独立推进的后继时可以不建立后继。地点当前态另按下方本轮权限处理，不预写完整循环。'
            '【沉寂内容续篇机会·软性】user payload 的 continuity_revival_target 为 null，或是后端从长期沉寂内容中轮换冻结的唯一可选续篇机会。它不属于 new_candidates，不要求本轮使用；省略相关 world_story_threads/world_actors、保持空数组或继续处理其它候选都是合法结果，不得因此触发格式修复、重试或补造内容。'
            'target_kind=thread_successor 表示准确 dormant 前驱仍有 next_reveal_hint；只有当前输入足以形成与旧阶段边界不同、可在 target_dungeon 继续游玩的方向时，才输出至多一个 status=seeded、scope_keys 包含 target_dungeon 的 successor，predecessor_id/source_world_story_thread_id/source_revision 必须精确复制冻结目标，且不得输出 continuity_actor_id。target_kind=actor_successor 表示准确 active 长期角色缺少任何可推进关联线且有一个终态前驱；采用时按同一 successor 合同，并把 continuity_actor_id 写成冻结 world_actor_id 的 JSON 正整数，后端会据此把新线准确绑定给该角色，不要求为此重复输出 world_actors 更新。target_kind=actor_new_front 表示准确 active 长期角色没有可用终态前驱；采用时输出至多一个 status=seeded、scope_keys 包含 target_dungeon 的 change_kind=new 世界线，并同样准确输出 continuity_actor_id，后端会把它绑定为该角色当前议程的新公共入口。除非 new_candidates 另有已结算角色变化，不能只为续篇绑定而改写该角色当前态。'
            '续篇机会只授权把输入中仍成立的前驱结果、角色身份、稳定原则、力量边界、长期目标、当前议程/处境和势力档案向前组织成一个新阶段；不得把 dormant/resolved 旧节点原地改回 active，不得借此推进、关闭或改写其它世界线，也不得把尚未发生的胜负、伤亡、占领、毁灭或不可逆结果写成既成事实。AI 输出后端会在同一事务重新检查目标 ID、revision、角色 active 状态、能力画像、缺少 mutable 关联和精确跨字段绑定；漂移或不完整时只软丢弃该续篇提案。'
            '续篇应提供真正不同且可识别的内容，不只改名复述旧余波。目标涉及重要势力或长期角色时，可在已有身份与能力边界之间尚未定义的空白中，按当前因果主动建立少量具体、可复用的承载者；承载者的具体形态由当前因果决定，不设预置类别、数量、轮换或逐项补齐要求。新长期主体必须按 world_actors 协议建立稳定身份与边界；不得冒用另行冻结的唯一角色/唯一物品，也不得仅为显得强大而越过已有力量和作用范围。'
            '本 Prompt 只处理 cycle_reseed_only=false 的普通维护，world_story_threads 不得输出 cycle_reseed；整个地牢不可探索后的循环播种由独立任务合同处理。'
            '候选已经造成持久资源生态变化时，当前阶段应保留其生成、转化、分布、消耗、恢复与可接触范围的最新公共关系，使后续探险能从变化后的状态继续推导资源来源；成员个人取得与分配只属于结算，不据此扩写公共生态。'
            'new_candidates.ecology_continuity_debt 是后端在 Observer 确认当前故事仍与权威现状兼容、但没有建立后继生态时形成的结构化连续性债务；它不是玩家失败，也不授权复活已经耗尽、迁离或失效的旧来源。'
            '存在该债务时，本轮必须实际消费：结合其 resource_ecology、evidence_events、existing_world_story_threads 与 authoritative_location_state_snapshot，保留既成结果并向前形成至少一个当前可继承出口。evidence_events 每项的 event_index 是同一来源日志最终 events 的原下标，text 是对应完整原文；缺少的原文不补猜，resource_ecology 描述的旧关系不能覆盖正文已经形成的结果。应把输入已经支持、仍在持续作用且可被后来者观察或互动的变化沉淀为准确类型的 seeded/active 世界线；只有会在后来者进入后继续生成、转化、迁移、交换、占据、补给、再分配或响应互动的过程、主体或机制才构成可持续关系，只会随取用单向减少的固定余量仍是有限存量。具体形态只由输入因果决定，不设题材目录或固定轮换。'
            '债务若带 ecology_identity，它是来源探险冻结的副本生态血统：后继须沿其中 identity_anchor、driver、resource_lineage、evolution_mode、interaction_mode 的描述与当前权威事实形成可继承因果；不能只给通用结晶/核心加副本名前缀，也不能复活旧有限存量。画像不是固定素材表、掉落保证、主线目标或成品剧情，profile/ref/fingerprint 只用于内部绑定，不得写入面向玩家的世界事实、便签正文或自然名称。'
            '若本轮证据只够保留演变约束、还不足以安全建立公共世界线，则必须把债务压缩进非空 dm_handoff_note，供下一次主剧情继续建立；不得同时返回空便签和空世界线而把债务静默标记为已处理。债务只保证同名地牢内部生态继续演变，不保证下一场取得资源，也不得让资源目标接管原本独立的探险目标。'
            '顶层 ecology_continuity_debt_resolutions 是债务消费回执数组。没有生态连续性债务时必须输出空数组；存在债务时，必须按 new_candidates 中每个带债务的 expedition_log_id 恰好输出一项，不得遗漏、重复或增加其它日志。每项固定包含 expedition_log_id（JSON 正整数）、decision 和 world_story_thread_indexes（最终 world_story_threads 的零基整数下标数组）。decision 只能是 world_story_thread 或 dm_handoff_note；前者要求索引非空、去重且每项指向 new/advance/successor 的 seeded/active/escalated 世界线；后者要求索引为空且最终 dm_handoff_note 非空。先确定最终世界线数组，再逐来源填写回执；即使同一日志的常规候选只允许 advance，生态独立 new 仍须在同一成品中通过自己的债务回执取得绑定。仅示意协议：来源日志 101 采用最终第 0 项合法世界线时写 {"expedition_log_id":101,"decision":"world_story_thread","world_story_thread_indexes":[0]}；采用非空便签时写 {"expedition_log_id":101,"decision":"dm_handoff_note","world_story_thread_indexes":[]}。实际日志 ID 与下标必须来自本次输入及最终数组。回执只绑定实际出口，不授权补造输入未支持的事实。'
            'user payload 的 world_story_write_authority 是普通维护中候选与生态债务写入 world_story_threads 的唯一白名单。candidate_changes 中每项只授权同一 candidate_id/expedition_log_id 的准确 change_kind；非 new 还必须逐字匹配 source_world_story_thread_id/source_revision。ecology_continuity_debt_outlets 只授权同一日志通过回执索引绑定 independent_new_allowed，或批次共享 ecology_continuity_debt_allowed_existing_changes 中列出的准确旧线变更；requires_resolution_receipt=true 时不得省略该绑定。共享旧线数组只投影本次有界 existing_world_story_threads，不是额外召回。existing_world_story_threads、new_candidates 正文、faction_agenda_catalog、相似标题或其它只读上下文本身均不授予写权限。continuity_revival_target 仍是与此白名单并列、用途受限的独立软性授权。没有匹配授权时省略对应世界线，不要提交让后端保护性拒绝的候选。'
            '生态出口的 source_scope 来自同一日志的持久地点绑定：state=stable 时独立新线的 scope_keys 必须包含准确 scope_key；state=unresolved 时 independent_new_allowed=false，只能在因果支持时使用已授权旧线并继承其范围，否则写非空便签保留本地演变约束，待后续主剧情建立准确地点。state=legacy 表示来源没有局部域记录，沿既有地牢范围处理。不得把未登记地点按名称猜绑、使用临时探险域作为持久地点，或把局部变化扩大成整个地牢状态。独立新线还须符合 world_story_capacity；多个来源共用同一独立新线时，其持久地点范围必须一致。'
            '跨副本联动允许成为后继阶段的因果来源。只有候选或 existing_world_story_threads 提供可审计的地点、势力、实体、物证、事件或关系锚点时，才能让其它副本的变化传入本地；必须写清影响范围与本地落点，同时保留两个副本各自身份，禁止因母题相似就合并，或把另一副本整体搬来替换 target_dungeon。'
            '只有候选真实建立与旧线不同、具有独立公共后果的方向，才可创建或提升一条有名字、有范围、有阶段、有推进方向的 seeded/active 世界线；没有这种候选时保持空缺。'
            'world_story_threads 保存后续队伍能遇到的公共当前阶段，包括局部对象的持续活动、关系与作用边界。记录保持短描述，说明已有的公共事实与范围；形成长期连续性不要求发生地区大事、升级威胁或另开任务。后续正文从该当前态自然发展，维护再依据实际变化更新、沉寂或收束。'
            '活动隐藏Boss属于专属任务机制，沿用 hidden_boss_context 与 hidden_boss_completed；公共世界要素属于日常大纲连续性，沿用 world_fact_updates、dm_handoff_note 与 world_story_threads。'
            '副本默认具有持久性；候选中的本场临时压力、撤离过程、局部后果或一次性痕迹，默认不能升级为后续队伍继承的地点长期状态。只有候选自身明确具备公共、持久、可复现或会改变后续理解的后果时，才可写入 world_story_threads 或作为地点状态保留。'
            'user payload 中 existing_world_story_threads 只包含后端确认 base_dungeon 与本次目标地牢一致，或兼容旧数据时 scope_keys 精确包含目标地牢的公共世界线。base_dungeon 是后端权威归属，不需要也不得在输出中填写或修改；scope_keys 只写剧情实际影响到的地点、区域、势力或实体域。更新旧线时必须沿用其中明确给出的 id 或 thread_id；新建阶段接力则使用 predecessor_id，禁止自行猜测其它旧线身份。'
            'existing_world_story_threads 中的 campaign_root_id、campaign_role、campaign_agenda 是管理员冻结的共同战役归属与运营方向，只读且不属于输出字段。只有本批已结算候选真正改变准确 front 的当前阶段时，才按原 id 更新或从该 front 建立 successor；不得修改归属、把 sibling front 当作本地已发生事实，或让一个 front 代替其它副本/势力前线推进。后端会为合法 successor 继承这些字段。'
            '单次局部探险中新建的独立公共线由后端按候选来源绑定到稳定局部域；已有地牢级或局部世界线继续沿用自身作用域。scope_keys 可以使用本次 source_scope 授权或准确旧线已有的持久地点键，其余范围按输入中的实际地点、区域、势力或实体填写。内部引用只用于结构化绑定，不写入 title、summary 或便签等自然文本。'
            '只有语义、母题、机制或后果类型相似，不构成旧线身份锚点，不得据此更新旧 id；此时根据候选的独立公共价值选择新建或忽略。'
            '正文 AI 后续会根据这些世界线自然发展剧情；同时，新的正文事实也可以改变旧世界线的理解、阶段或权重。'
            '当候选与旧世界线冲突时，优先判断它如何改变既有理解；不要机械保留旧说法，也不要无依据抹掉已沉淀的宏观事实。'
            '世界线与势力档案维护可以更自由、大胆、具体。不要过度使用“某商会、某组织、某人、可能存在、疑似、或许”这类模糊占位；已结算故事中的势力行动、选择、物证、制度作用、知识/技术运用与关系结果，既是可沉淀事实，也是继续补充未定义稳定设定的创作锚点。'
            '大胆不等于改写权威真相。明确命名、定线和设定补充必须由候选事实、已有身份、既有世界线、反复同源证据或地牢生态提供可解释锚点；在这些锚点之间尚未定义的空白可以主动创作，已经冻结的身份、阶段、地点、角色、物品、能力、关系与状态不得推翻。证据不足时可以保留传闻、误判或局部猜测，但不确定性也要有可被未来剧情验证的具体名字、地点、物证或势力抓手。'
            '同一对象、阶段与准确来源的候选可合并到同一条世界线；独立成立的持续公共事实按各自身份与范围保存。名称沿用候选和现有档案中的具体称谓，新阶段名称从已发生的地点、行为或关系中形成。成员个人持有与个人后续继续由成员档案承载。'
            f'{tavern_faction_scope_rule}'
            '势力线不是好感度系统，不要写数值化好感，也不要把关系简化成单一“友好/敌对”。重点记录本次候选已支持的参与方、关系边界、内部差异、当前阶段和可验证的下一步方向。'
            '当候选已经支持势力、势力成员或长期主体之间的利益、旧怨、依赖、约束或内部裂痕时，采用分层并行维护：长期主体档案承载强 Boss、不可战胜个体和关键 NPC 的稳定身份、力量边界、独立行动能力与当前处境；faction 世界线承载势力阶段、结构和影响范围；faction_relations 承载双方实际位置、利益、约束与行动边界。候选已结算事实将关系发展映射为各方可用资源、行动权限、协作条件、冲突方向与代价结构，友好/敌对作为关系结构中的一个维度参与维护。'
            '【势力利益激活】faction_agenda_catalog 是后端有界提供的其它势力当前议程，只用于判断本批候选造成的公共变化是否与既有目标真实相交；目录项已与 existing_world_story_threads 排除重复，不是本次可直接更新的世界线授权，禁止在输出中使用其 id/thread_id。目录中的 next_reveal_hint 只表示既有因果支持的未来验证条件，不证明该势力当前已经知情、决定介入或采取行动。'
            '逐个判断利益是否相交、该势力是否已经能够通过已成立的信息或传播关系得知、其行动范围与能力是否能够抵达或影响、当前目标与约束是否足以承担介入代价；四项共同成立才可让该势力进入实质行动。允许没有任何势力介入，不设置参与数量、轮流登场或多方冲突配额，也不得因题材相近、名字相关或需要热闹就牵入势力。'
            '候选只建立了可被相关势力发现或争取的持久公共机会、但尚未建立该势力已经知情或采取行动时，不得把可能反应写成已发生事实；只有候选自身为 change_kind=new 且已经建立明确公共对象、可达信息关系与持续影响时，才可用独立 seeded event 世界线保存下一项可验证的势力反应条件。候选已经结算了势力行动时，才按其准确授权更新对应 faction 世界线或建立 active 事件线。'
            '新势力只在候选事实已经建立稳定可辨认的群体身份、共同且持续的目标、自主协调行动能力、本场结束后仍然成立的持续性，以及对公共资源、地点、关系或其它势力的实际影响时创建 faction 世界线；缺少任一项时保留为 event、entity、临时联盟或普通公共事实，不得为了未来可能有趣而补造组织、成员、目标或历史。一次候选若已完整建立上述事实可以直接成立，不要求机械重复次数。'
            '【长期世界主体】长期主体不受物种、形态、生命性质、个体或群体结构限制。候选已建立稳定身份、自主行动、队伍离场后仍持续活动，且其自身能力、目标、处境或关系会影响后续互动时，在 world_actors 保存该主体；首次出现也适用。world_story_threads 承载其活动的公共阶段，world_actors 承载同一主体的能力版本与当前态，两者分工不同。持续观察和学习属于主体连续性的一种表现，力量层级与是否人形不决定建档资格。'
            '新主体的持续活动本身就是可承载它的 entity 世界线阶段。首次建档在同一成品中保存 world_actors 及这个公共阶段，用 involvement=direct、lease_required=true 的 thread_links 准确连接；已有承载该活动的世界线时沿用其 id，否则在本批 new 授权与容量内输出该阶段，并用准确 thread_title 连接。世界线负责后续召回入口，主体档案负责能力与当前态，关联使后来的队伍能够遇到同一主体。更新已有主体沿用准确 id，只提交真实变化。只有本场作用、缺少持续自主主体的对象，按其公共后果保留为普通世界事实。玩家、技能或手段名称不构成主体身份；事实未建立的目标、关系或未来行动保持未定义。'
            '势力的公共阶段、结构、影响范围与局势变化由 faction 世界线维护；长期主体档案只维护可持续行动主体的身份、目标、当前处境及其在关系结构中的位置。组织性对象在候选已证明它本身构成统一、持续且可归责的行动主体，并且符合上述持续性标准时，作为 collective 主体保存；主体档案与势力世界线可以关联，分别维护自身职责内的事实。'
            '【势力档案稀疏维护】existing_faction_profiles 是与本批精确世界线关联、且完整落入本轮字符预算的势力当前档案；未列出的档案不授权猜测或更新。faction_profile_name_registry 是有界的全局占用称谓字符串数组，只用于新建名称避让，不是身份匹配依据。势力档案承载跨阶段仍需复用的稳定身份与分面当前态，faction 世界线继续承载本阶段行动、矛盾和公共结果；两者不得复制同一段剧情摘要。'
            'new_candidates[].faction_presence_story_evidence 是后端从已通过 Observer 的最终玩家可见 events、精确势力锚点和规范回执中投影的维护证据。它只授权更新同项 faction_profile_id 的稳定档案，或在故事已经形成公共持久新阶段时沿准确 world_story_thread_id 维护世界线；profile_revision/source_revision 是生成时冻结版本，实际输出仍使用 existing_faction_profiles/existing_world_story_threads 当前提供的 id 与 revision。该证据不把旧 read-only 线程变成原地写权限，resolved 旧阶段只能保持历史或建立真正不同的 successor。story_events 是已结算事实，可以据此沉淀故事明示内容并补充能够解释这些行动的兼容设定；没有该精确证据时不得从普通剧情名称近似猜配档案。'
            'faction_profile_bootstrap 为 null，或包含一个由后端准确选择、当前尚无档案关联的 faction 世界线及本轮新建上限。对象非空时，它只额外授权把该线已经持久化的稳定身份事实稀疏提取为初始档案；不授权推进或激活世界线、改变其当前事实或补造分面。证据足以区分一个或多个稳定群体时优先提议相应新档案，必须把 target_world_story_thread.id 原样写入 thread_links.thread_id，规范 JSON 类型为整数，不得改用 thread_title；并且不得超过 max_new_profiles，一旦超过，后端会把与该目标关联的全部冷启动提案整体丢弃且不重试，不会截断保留。证据不足时保持空数组仍是合法结果。'
            '除上述冷启动提取外，只对 new_candidates 或 existing_world_story_threads 已准确建立身份的势力输出 faction_profile_updates。每项仍是稀疏提案：已存在的分面只在本次确有变化时重写，未输出分面保持原值；但对尚未定义的分面，缺少旧记录本身就是可创作空白，可以依据本次已结算行动及既有身份大胆建立能够解释其行为、并可供后续剧情复用的稳定设定，不要求候选逐字说出档案句子，也不要因旧档案稀疏而退回泛称。规模、所在、活动范围、历史、内部派别、技术、知识、治理、资源、文化、目标与秘密都可自然形成，不设固定补齐清单、数量、对称性或轮换配额，只选择本次因果真正触及且彼此兼容的内容。不得推翻任何既有身份、分面、世界线、角色、地点、能力、关系或状态事实，也不支持清空、删除和按近似名称合并；无身份锚点或没有可解释行为时输出空数组。'
            '新档案必须由证据同时建立稳定可辨认的群体身份、跨越本场的持续性、自主协调行动能力与公共影响，并以 thread_links 精确关联已有或本次新建的 faction 世界线；输出 canonical_name、identity_summary，不填 id/source_revision。identity_summary 只写普通相关者可用的稳定身份边界；status=rumored 时它整体作为未证实传闻，除此之外按公开身份使用。内部真实归属、秘密身份或受限知识不得写入 identity_summary，只能在证据成立时写入 restricted 分面。一次临时结盟、同场聚集、共同受害或同类生物不构成势力。已有档案更新必须原样使用 existing_faction_profiles 的准确 id 与 revision 作为 source_revision；canonical_name 保持不变，禁止按标题、别名、关键词或语义相似猜旧档案。'
            'status 语义固定：rumored=稳定群体身份正在流传但协调能力尚未被公共证据确认；active=已经建立且当前仍有自主协调行动；dormant=身份与结构仍成立但当前没有持续协调行动；dissolved=原结构已不再作为统一主体行动，只保留历史后果。一次同场行动不自动等于 active，暂未登场不自动等于 dormant，一次失败或首领死亡不自动等于 dissolved。status 高于旧 current_agenda：rumored 的议程只能保持 rumor/restricted，dormant/dissolved 不得把旧议程继续写成正在执行的统一行动；后端再注入也会抑制冲突的旧议程。'
            'facet 只可取 scale、territory、operating_range、history、governance、internal_structure、capabilities、knowledge、resources、culture、current_agenda、current_state。每个 facet_updates 项只写一个紧凑当前事实，并带 visibility。'
            'public 表示外部观察、公开制度或已传播证据足以让普通相关者合理知晓；rumor 表示说法已经在世界内传播但尚未被权威证实，后续保持不确定；restricted 表示本次行动与既有身份足以支持的内部机制或受限真相，仅供后置维护继续判断。'
            '可观察行动、选择、物证和制度效果可以支持对其背后稳定设定的作者性归纳；归纳必须能解释本次事实、与既有档案相容，并区分“新补充的稳定设定”和“本次发生的状态变化”。作者觉得有趣或将来可能揭露本身仍不是 public/rumor 的依据；没有行动或身份锚点的秘密直接省略。governance 与 internal_structure 若形成分支、派系、适用条件或未决分歧，紧凑摘要应保留这些差异。'
            'thread_links 每项以准确 thread_id，或本次 world_story_threads 的准确 thread_title 关联 faction 类型世界线；relation_role=core 表示该线直接承载此势力身份，involved 表示该势力参与该线关系，historical 表示只承载已结束阶段。档案关联本身不授权势力出场、知情、触达、行动、提升风险或推进世界线。'
            'faction_profile_updates 每项格式：{"id":已有档案id可选,"source_revision":已有档案必填,"canonical_name":"新档案稳定名称","aliases":["别名"],"status":"rumored|active|dormant|dissolved","identity_summary":"稳定身份边界","scope_keys":["地点/区域/实体域"],"trigger_keywords":["专名/别名/标志物"],"importance":1-100,"facet_updates":[{"facet":"协议分面","visibility":"public|rumor|restricted","summary":"该分面紧凑当前事实"}],"thread_links":[{"thread_id":已有线准确id可选,"thread_title":"本次新线准确标题可选","relation_role":"core|involved|historical"}],"change_summary":"本次有证据变化"}。canonical_name 最多 160 字符；identity_summary、facet summary、change_summary 各最多 600 字符；aliases 最多 8 项、scope_keys 最多 8 项、trigger_keywords 最多 10 项、facet_updates 最多 12 项、thread_links 最多 8 项；这些数组超过各自上限时对应辅助字段会整体忽略，不按模型顺序截断；已有档案的空数组不会清空旧值。同一档案每批最多一个对象，同一分面和同一世界线关联也最多一项。重复目标的全部冲突项都会被丢弃，不按数组先后取舍。'
            '势力档案是失败开放的后置辅助整理：单项、嵌套分面、关联或版本无效时，后端只丢弃对应辅助项并记录诊断，不要求改写其它维护结果，也不触发主剧情、Observer 或维护 AI 重试。因此不要用空洞占位来避免缺字段，宁可省略整项。'
            '长期主体保存的是固定长度当前态，不是经历流水账。每次用新事实重写当前身份边界、长期目标、当前行动与处境；已经失效的事项移出当前态，完整经历由后端另存审计。不得把旧描述与新剧情不断拼接。'
            'faction_relations 维护主体与势力之间持续有效的关系当前态，而不只是主体对势力的态度。应从已结算事实中抽取双方关系结构中的实际位置与方向、公开表现、真实动机、利益依赖、约束范围、行动权限、独立边界和未决变化；不预设谁从属于谁，不要求归入固定类型，也不得压缩成单一立场或数值。没有稳定势力联系也是有效状态，不得为了填充关系字段强行建立关系。'
            'existing_world_actors 只包含与本批候选世界线关联的有界当前态。更新已有主体必须带其 id；只有候选明确支持稳定身份锚点与独立持续性时才新建，canonical_name 应保存可跨剧情确认同一性的稳定称谓。不得因名字、题材或能力相似合并主体；无法确认身份时保留普通 entity 世界情报。'
            'world_actor_name_registry 是从已占用称谓中选取的有界命名提示，优先包含本次相关主体；未列出不代表名称未被占用，后端写入时核对完整历史身份。它只用于新建主体命名避让，不是身份匹配或更新依据。已有主体的 canonical_name 保持不变；更新时沿用 existing_world_actors 提供的 id 与 canonical_name，不得为了改善命名风格而改名。新建主体的 canonical_name 必须形成可直接指称、可长期复用且与登记表清晰区分的独特专名或世界内稳定代号；其语源、构词和节奏应从候选事实、所属文化、档案来源或主体自我指称中自然确定。描述身份、状态或力量的称号放入 aliases，避免用相近的通用意象和同构称号批量替代专名。登记表中的名字相似不证明主体相同，不得据此合并或更新已有主体。'
            'thread_links 用世界线准确 id 或本次 world_story_threads 的准确 title 建立关联。direct 表示本人可行动、表态或改变状态并需要后端独占租约；indirect 只允许代理、势力或既有余波；historical 只作历史背景。'
            '主体维护是失败开放的后置整理，不裁决主剧情是否重试；只整理候选中已经发生并通过结算的事实，不补造关系变化、承诺或行动。'
            '实体与势力的自我发展按完成程度沉淀：目标、尝试与进行中的准备进入 current_agenda/current_state；已结算过程实际改变的本领进入主体 capability_profile 或势力 capabilities，实际改变的势力资源进入 resources。能力画像更新保留其余仍有效的本领、作用条件与限制，势力只更新变化的分面；没有实际变化时继承原值。未来打算与时间经过本身不建立已完成的成长，维护不替正文完成这些行动。'
            'existing_world_actors 的当前能力版本也可包含独立学习流程已经核对并保存的战后成长，这是已完成的当前事实。以该完整当前画像为更新起点；来源战斗的旧画像解释当时交锋，不覆盖较新的成长，也不因旧正文未写成长过程而撤销它。'
            'new_candidates 的 encounter_definition.schema_version=1 表示 settled_encounter_capability_profiles 在交锋前已定义。只有 world_fact_updates 证明对应主体真实出场并具备持续资格时才建档；按稳定 subject 精确对应 individual profile，继承能力基线，用已结算事件造成的真实变化更新其当前状态和能力，不能因为摘要未重复某项本领就删除它。没有标记的历史画像仍只作已采用部分的参照；没有精确对应画像时依据已结算能力事实建档，不猜配。首次建档完整保存 basis_level、typical_profile、current_profile、condition、number_pressure、capabilities、limitations 及准确 direct 世界线关联；已有主体只有真实结算事实改变能力或状态时才更新，地图、未来队伍或叙事需要不重算能力。'
            '公共关系可以围绕任何有叙事价值的对象发生变化，但不要从本规则列举题材；只抽取候选事实中已经出现的关系结构和行动边界。'
            '同一势力内部不必态度一致，但不要因为一次探险就把整个势力永久定性。'
            '如果需要新建 world_story_threads，优先创建有名字、有范围、有阶段、有推进方向的公共剧情线；避免没有抓手的线索。'
            '这些线索可以是任何公共对象或公共状态；类型字段只用于归类，不是题材清单。'
            '公共世界要素可参与后续剧情，参与强度由本次因果、阶段状态与队伍处理能力共同决定；超出队伍处理范围的要素适合形成有限影响或背景压力，并保留已建立的连续性。目标、条件、能力和阶段自然汇合时，才进入实质变化。普通副本仍沿用已确立公共要素的连续性。'
            '每条格式：{"id":已有线id可选,"predecessor_id":仅后继填写,"change_kind":"new|advance|close|resolve|successor","source_world_story_thread_id":非new必填,"source_revision":非new必填,"continuity_actor_id":仅冻结角色续篇填写的JSON正整数,"title":"...","thread_type":"entity|event|faction|location|omen|artifact","status":"seeded|active|escalated|closing|dormant|resolved","scope_keys":["地点/区域/势力/实体域"],"trigger_keywords":["实例名/势力名/别名/标志物/关键词"],"summary":"当前公共事实","next_reveal_hint":"可选后续","power_tier":"可选影响层级","power_profile":"可选连续性边界","weight":1-100}。next_reveal_hint 只记录尚未收束阶段已有因果支持的下一验证条件；resolved 留空，dormant 没有客观再触发条件时也留空，不用它制造未来承诺。'
            '普通维护的每条 world_story_threads 都必须显式输出 change_kind、thread_type、status、scope_keys 与 weight；非 new 还必须输出准确 source_world_story_thread_id/source_revision。后端不会根据是否有 id、现有状态或缺省值替你选择 new、event、active 或 50。'
            'existing_world_story_threads 中未出现在上述输出格式里的字段都是只读上下文元数据；输出每一项时只按上述格式重新构造，不复制这些元数据。'
            '更新旧线必须令 id、source_world_story_thread_id 都等于 existing_world_story_threads 的准确 id，并原样回传其 revision；独立新线只用 change_kind=new 且不填写 source；后继用 change_kind=successor、predecessor_id/source 均指向旧 head，并回传旧 revision。普通维护不得输出 cycle_reseed。后端不接受按地牢、scope、标题近似或关键词推断的旧线更新。'
            '同一阶段的事实、权重或状态变化必须带旧 id 原行更新；只有旧阶段已经形成明确结果、且该结果因果支持一个边界与矛盾均可独立游玩的新阶段时，才新建不同标题的条目并填写 predecessor_id。后端会把旧节点保留为历史且退出触发，只让新节点成为该剧情链 head。不得因题材、母题或机制相似填写 predecessor_id，也不得把 resolved 旧节点原地改回 active。existing_world_story_threads 只提供各剧情链当前 head；没有明确后继关系就继续更新原 id 或建立独立剧情链。'
            '当同一批候选既让旧 head 形成明确结果，又建立后续队伍会继承且可独立推进的新阶段时，world_story_threads 必须成对输出：先带旧 id 将前驱更新为 dormant/resolved 并写清阶段结果，再输出不同标题、状态为 seeded/active/escalated 且 predecessor_id 指向旧 head 的后继。不得只关闭旧 head 而漏掉正文已经建立的可玩后继，也不得把后继内容覆盖回旧 id。若候选只支持收束、没有建立持久且可玩的后继状态，则只关闭旧阶段，不得虚构后继。'
            '带旧 id/thread_id 更新时必须满足上述精确身份锚点；后端会拒绝任何无锚点旧线更新。'
            '重大公共要素若仍会影响后续任意地点、势力、实体或事件链，应保留其名字/事件名/势力名、关联地点、当前阶段与未解影响；'
            '主体能力与风险沿其 capability_profile、地点权威当前态及 public_risk_notice 继承。world_story_threads 的 summary 记录本批真正改变的公共事实；power_profile 仅在本批证据确立了公共作用边界的真实变化时提交，写清其范围与成立条件，省略表示继承已有边界。局部退避、暂时中断或一次失利按正文原来的时间和范围保存，原本仍成立的能力与风险继续有效。'
            '整体采用地点/地区视角，优先沉淀后续需要理解的公共对象、范围、关系、规则、状态边界和跨场景影响。'
            '不要把候选合并成“本次队伍做了什么”的探险行动总结；要抽取成“该地点现在是什么状态、什么机制被揭示、哪个地区会受影响、未来可能发生什么”。'
            '不要把个人伏笔、专属关系、个人物品、个人伤势、个人装备维修或个人研究进度泛化给无关队伍；不要把 local 事件升级成 global 阴谋；'
            '若某条候选只是在说明某个成员做了什么、受了什么伤、拿了什么东西，且没有地牢/地区层面的公共后果，应删除；若它揭示了公共机制，应只保留机制、地点状态或地区影响。'
            '候选里的 M1/M2/【M1】等成员代号只在对应 expedition_log_id 内有效，绝不能跨日志复用或当成同一个人；'
            '若 payload 提供 member_alias_scope，必须按其中 scope 和 name 理解该日志内代号；输出便签/世界线时默认移除成员视角，不要保留裸 M1/M2，除非确有必要追踪某个具名冒险者的非公共长期连续性。'
            'world_actors 每项格式：{"id":已有角色id可选,"canonical_name":"稳定专名","aliases":["别名"],"actor_type":"individual|collective|manifestation|other","status":"rumored|active|withdrawn|missing|retired|dead","identity_summary":"稳定身份","stable_principles":"原则与行为边界","power_tier":"可选层级","power_boundary":"力量连续性边界","long_term_goal":"长期目标","current_agenda":"当前主动行动","current_state":"当前处境","scope_keys":["地点/势力/实体域"],"trigger_keywords":["专名/别名/标志物"],"importance":1-100,"faction_relations":[{"faction_key":"稳定键","faction_name":"势力名","public_stance":"公开立场","private_view":"真实看法","interests":"利益交集","concerns":"顾虑与戒备","dependencies":"依赖与需要","unresolved_matters":"未清事项","behavior_boundary":"行为边界","last_change_summary":"本次变化"}],"thread_links":[{"thread_id":已有线id可选,"thread_title":"准确标题可选","involvement":"direct|indirect|historical","narrative_role":"简短职责","lease_required":true}],"interaction_summary":"本次已结算交集","actor_change_summary":"本次角色变化","relation_change_summary":"本次关系变化"}。'
            '每条 world_actors 都必须显式输出 actor_type 与 status；每条 thread_links 都必须显式输出 involvement 与 lease_required。后端不会代选 individual、active、direct 或 lease_required。'
            'world_actors.trigger_keywords 是可选的去重非空字符串数组，最多 10 项，每项最多 50 字符；仅数量超过上限时整个字段被忽略并保留诊断，不截取前几项、不清空已有关键词，也不单独因此重试，其它合同继续严格验收。'
            '有精确证据时，上述 world_actors 项可额外包含 capability_profile，固定为 {"basis_level":正整数,"typical_profile":{"force":"very_low|low|baseline|high|very_high|exceptional","mobility":"同枚举","cognition":"同枚举","durability":"同枚举","perception":"同枚举"},"current_profile":同结构,"condition":"impaired|normal|enhanced","number_pressure":"none|low|medium|high|overwhelming","capabilities":["非空能力边界"],"limitations":["非空限制"]}；没有精确证据时省略该字段。'
            '删除过时、重复、互相矛盾或模板化的内容。输出纯 JSON：{"dm_handoff_note":"...","world_story_threads":[...],"world_actors":[...],"faction_profile_updates":[...],"ecology_continuity_debt_resolutions":[...]}。'
            '标准输出包含上述五个字段：dm_handoff_note 是字符串，其余四项是数组。faction_profile_updates 是可选辅助字段，缺失、null 与 [] 均等价为空数组且不会触发结构修复；其余核心字段继续遵守固定类型。没有生态连续性债务且没有值得维护的公共内容时返回空字符串与四个空数组，不要为了满足格式制造内容。'
            f'本次 world_story_threads 建议不超过 {max_ai_updates} 项；若同批存在必须成对落库的前驱收束与后继建立，不得为了凑数量漏掉其中一项。'
            f'本次 world_actors 硬上限为 {max_world_actor_updates} 项；超过即合同失败，禁止依赖数组顺序截断。'
            f'本次 faction_profile_updates 正常目标不超过 {max_faction_profile_updates} 个档案，协议硬上限为 16；超过硬上限时整个辅助数组被忽略且不重试。每项只保留证据支持的稀疏变化。'
            f'dm_handoff_note 建议不超过 {DM_HANDOFF_NOTE_LIMIT} 个中文字符；summary、next_reveal_hint 与 power_profile 也应主动压缩，但后端不会替你截断细节，过长内容需在后续维护中自行压缩。'
        )
        if not cycle_reseed_only:
            system_prompt += (
                WORLD_LOCATION_MAINTENANCE_RULE if allow_location_evolution
                else WORLD_LOCATION_MAINTENANCE_READ_ONLY_RULE
            )
        if cycle_reseed_only:
            system_prompt = (
                '你是深渊酒馆 S1 的副本循环播种 AI。'
                '本轮唯一任务是为已经整体不可探索、且没有可继续公共 head 的 target_dungeon 建立当前后继。'
                'user payload 已由后端机械确认 cycle_reseed_only=true、'
                'authoritative_location_state_snapshot.is_explorable 精确为 false，'
                'existing_world_story_threads 至少含一条准确 dormant 或 resolved 终态 head；'
                '输入中的地点状态与世界线 revision 都是只读权威当前态。'
                '本轮取得限定的向前世界模拟权限，不再等待 new_candidates 预先写出 successor。'
                '从输入选择一条与权威当前态因果衔接完整的 dormant 或 resolved 终态 head，'
                '以未被撤销的前驱结果、权威当前态和 target_dungeon 主题身份作为共同起点，'
                '向前推演由该终态自然形成并持续作用的世界内过程或关系。'
                '具体后继及其过渡原因不要求已经作为独立事实预存在输入中，'
                '但不得引入与这些起点没有因果联系的转折。'
                '后继 summary 必须在同一条公共因果中完成四项功能：保留前驱结果，'
                '建立阶段接替机制，说明当前可观察状态，并形成可反复探索与互动的持续基础。'
                '它不能只提供一次性库存、未成立的未来可能性或后台刷新，也不保证下一场实际取得任何成果。'
                '后继必须保持 target_dungeon 的主题身份可辨认；'
                '本轮审核通过的结果会直接成为权威地点当前态，不再等待未来探险选择或兑现世界线。'
                '只输出纯 JSON 对象，顶层字段固定为 dm_handoff_note、world_story_threads、world_actors、cycle_reseed_transition。'
                'dm_handoff_note 必须是空字符串，world_actors 必须是空数组，'
                'world_story_threads 必须且只包含一个对象。'
                '该对象的必填字段固定为 predecessor_id、change_kind、'
                'source_world_story_thread_id、source_revision、cycle_reseed、title、'
                'thread_type、status、scope_keys、summary、weight；'
                '不得输出 id、thread_id 或其它顶层字段。'
                'predecessor_id 与 source_world_story_thread_id 必须同为所选终态 head 的准确 id，'
                'source_revision 原样回传其 revision，change_kind 固定为 successor，'
                'cycle_reseed 固定为 true，status 固定为 seeded。'
                'title 与 summary 必须是非空字符串；'
                'thread_type 只可取 entity、event、faction、location、omen、artifact；'
                'scope_keys 必须是非空去重字符串数组并包含 target_dungeon，最多 8 项，超出视为合同失败；'
                'weight 必须是 1 至 100 的整数。'
                '唯一对象还可以包含 trigger_keywords、next_reveal_hint、power_tier、power_profile；'
                'trigger_keywords 若输出，必须是不凑数的去重字符串数组，最多 10 项，超出视为合同失败。'
                'next_reveal_hint、power_tier、power_profile 若输出，必须是字符串。'
                'cycle_reseed_transition 必须是对象且只包含 location_state_change 与 dungeon_explorability_change。'
                'location_state_change 必须给出一个变化后的完整地点当前态：更新快照已有地点时原样回传其 location_id、location_name 与 revision 到 expected_revision；'
                '建立快照未登记的新地点时提供稳定 location_name、expected_revision 固定为 0，且不输出 location_id。'
                '两种情况都必须输出非空 new_state；已有地点还必须输出非空 change_summary。'
                'new_state 与唯一后继 summary 必须描述同一当前因果，完整承载保留的终态结果、持续接替机制、当前可观察状态和可反复探索互动的基础。'
                'dungeon_explorability_change 必须原样输出 expected_is_explorable=false、is_explorable=true，并以非空 change_summary 概括已经成立的恢复原因。'
                'existing_world_story_threads 与 authoritative_location_state_snapshot 中未列入上述输出字段的内容'
                '只用于判断，不得回显。'
            )
        if cycle_reseed_only:
            cycle_terminal_heads = [
                {
                    key: row.get(key)
                    for key in (
                        'id', 'revision', 'title', 'thread_type',
                        'status', 'scope_keys', 'summary',
                    )
                    if key in row
                }
                for row in existing_threads
                if isinstance(row, dict)
                and not _coerce_int(row.get('campaign_root_id'), 0)
                and str(row.get('status') or '').strip().lower()
                in WORLD_INTEL_TERMINAL_STATUSES
            ]
            user_payload = {
                'target_dungeon': dungeon,
                'existing_world_story_threads': cycle_terminal_heads,
                'authoritative_location_state_snapshot': (
                    authoritative_location_state_snapshot
                ),
                'cycle_reseed_only': True,
                'maintenance_output_contract': {
                    'required_top_level_fields': [
                        'dm_handoff_note', 'world_story_threads', 'world_actors',
                        'cycle_reseed_transition',
                    ],
                    'max_world_actor_updates': max_world_actor_updates,
                },
            }
        else:
            user_payload = {
                'target_dungeon': dungeon,
                'new_candidates': payload,
                'world_story_write_authority': world_story_write_authority,
                'world_story_capacity': {
                    **capacity,
                    'current_active_heads': current_active_heads,
                    'new_head_admission_is_hard': True,
                },
                'current_dm_handoff_note': current_note,
                # Scheduling counters select context in the backend; they are
                # not world facts or additional work for the maintenance AI.
                'existing_world_story_threads': [
                    {
                        key: value for key, value in row.items()
                        if key not in (
                            'closure_debt', 'injection_count',
                            'missed_injection_runs', 'match_score',
                        )
                    }
                    for row in existing_threads
                ],
                'authoritative_location_state_snapshot': (
                    authoritative_location_state_snapshot
                ),
                'faction_agenda_catalog': faction_agenda_catalog,
                'existing_faction_profiles': existing_faction_profiles,
                'faction_profile_name_registry': faction_profile_name_registry,
                'faction_profile_bootstrap': (
                    faction_profile_bootstrap_context
                ),
                'existing_world_actors': existing_world_actors,
                'continuity_revival_target': continuity_revival_target,
                'world_actor_name_registry': world_actor_name_registry,
                'max_world_story_thread_updates': max_ai_updates,
                'max_world_actor_updates': max_world_actor_updates,
                'max_faction_profile_updates': max_faction_profile_updates,
                'maintenance_output_contract': {
                    'required_top_level_fields': [
                        'dm_handoff_note', 'world_story_threads', 'world_actors',
                        'ecology_continuity_debt_resolutions',
                    ],
                    'optional_top_level_fields': (
                        ['faction_profile_updates', 'location_state_changes']
                        if allow_location_evolution else ['faction_profile_updates']
                    ),
                    'max_world_actor_updates': max_world_actor_updates,
                },
                'cycle_reseed_only': False,
            }
            if campaign_required_changes:
                system_prompt += '\n' + DISPATCH_MAINTENANCE_RULE
                user_payload['maintenance_output_contract'][
                    'required_world_story_changes'
                ] = campaign_required_changes
        activity = campaign_world(dungeon)
        if activity:
            from .s1_campaign_world_service import CAMPAIGN_WORLD_MAINTENANCE_RULE
            activity['allowed_location_scopes'] = sorted(S1CampaignWorldRepository.owned_location_scopes(activity['dungeons']))
            system_prompt += '\n' + CAMPAIGN_WORLD_MAINTENANCE_RULE
            user_payload['activity_world'] = activity
        user_prompt = json.dumps(user_payload, ensure_ascii=False)

        def validate_maintenance_response(decoded):
            parsed_candidate = decoded.json_result.data
            (
                maintained_candidate,
                structural_candidate_errors,
                _candidate_diagnostics,
                contract_valid,
            ) = normalize_maintenance_result_with_status(
                parsed_candidate,
                cycle_reseed_only=cycle_reseed_only,
                max_world_actor_updates=max_world_actor_updates,
                location_state_snapshot=authoritative_location_state_snapshot,
                allow_location_evolution=allow_location_evolution,
            )
            if (
                isinstance(parsed_candidate, dict)
                and isinstance(parsed_candidate.get('world_actors'), list)
                and len(parsed_candidate['world_actors'])
                > max_world_actor_updates
            ):
                return S1AiResponseContractResult.reject(
                    'world_actors_over_consumer_limit'
                )
            if maintained_candidate is None or structural_candidate_errors:
                return S1AiResponseContractResult.reject(
                    structural_candidate_errors[0]
                    if structural_candidate_errors
                    else 'world_outline_maintenance_contract_invalid'
                )
            if not contract_valid:
                return S1AiResponseContractResult.reject(
                    'world_outline_maintenance_contract_relaxed'
                )
            if activity:
                from .s1_campaign_world_service import activity_maintenance_errors
                activity_errors = activity_maintenance_errors(
                    maintained_candidate, activity, existing_world_actors, existing_faction_profiles,
                    allowed_location_scopes=activity['allowed_location_scopes'])
                if activity_errors:
                    return S1AiResponseContractResult.reject(activity_errors[0])
            if cycle_reseed_only:
                _, cycle_contract_error = (
                    _validate_cycle_reseed_maintenance_candidate(
                        parsed_candidate,
                        maintained_candidate,
                        existing_threads=existing_threads,
                        authoritative_location_state_snapshot=(
                            authoritative_location_state_snapshot
                        ),
                        dungeon=dungeon,
                    )
                )
                if cycle_contract_error:
                    return S1AiResponseContractResult.reject(
                        cycle_contract_error
                    )
            else:
                if any(
                    isinstance(row, dict) and row.get('cycle_reseed') is True
                    for row in maintained_candidate.get(
                        'world_story_threads', []
                    )
                ):
                    return S1AiResponseContractResult.reject(
                        'ordinary_maintenance_contains_cycle_reseed'
                    )
                debt_contract_valid, _, _ = (
                    _normalize_maintenance_debt_resolution_contract(
                        maintained_candidate,
                        payload=payload,
                        world_story_write_authority=(
                            world_story_write_authority
                        ),
                        ecology_continuity_debt_log_ids=(
                            ecology_continuity_debt_log_ids
                        ),
                        has_ecology_continuity_debt=(
                            has_ecology_continuity_debt
                        ),
                        diagnostics=[],
                    )
                )
                if not debt_contract_valid:
                    return S1AiResponseContractResult.reject(
                        ECOLOGY_CONTINUITY_DEBT_RECEIPT_ERROR
                    )
                campaign_contract_error = validate_dispatch_maintenance_changes(
                    maintained_candidate.get('world_story_threads', []),
                    campaign_required_changes,
                )
                if campaign_contract_error:
                    return S1AiResponseContractResult.reject(campaign_contract_error)
            return S1AiResponseContractResult.accept(maintained_candidate)

        # AI HTTP 可能阻塞数分钟；先释放 DB 连接，落库阶段再重新 checkout。
        db_manager.remove()
        resp = _post_world_outline_ai_request(
            f'{api_base}/chat/completions',
            stage='maintenance',
            player_names=tuple(dict.fromkeys(source_player_names)),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_payload={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': system_prompt},
                    {'role': 'user', 'content': user_prompt},
                ],
                'temperature': 0.3,
            },
            timeout=timeout,
            response_validator=validate_maintenance_response,
            usage_context=usage_context,
        )
        decoded = parse_s1_ai_http_json_response(
            resp,
            check_http_status=False,
        )
        usage = decoded.http_result.usage
        initial_usage_handle = _DeferredWorldOutlineTokenUsage(
            call_type='world_outline_maintenance_ai',
            usage=usage,
            model=get_s1_ai_response_metadata(resp).get('model') or model,
            attempt_number=1,
            usage_context=usage_context,
            finish_reason=decoded.http_result.finish_reason,
            response_error_code=decoded.json_result.error_code,
            repair_applied=decoded.json_result.repair_applied,
        )
        usage_handles.append(initial_usage_handle)
        if decoded.http_result.finish_reason == 'length' or not decoded.http_result.content.strip():
            # 统一网关已按同一冻结请求尝试备用；残缺内容不能补空后冒充完整事实。
            initial_usage_handle.finalize(False)
            raise ValueError(
                'world_outline_maintenance_response_incomplete:'
                + str(decoded.http_result.finish_reason or decoded.json_result.error_code or 'empty')
            )
        content = decoded.http_result.content
        parsed = decoded.json_result.data
        if not cycle_reseed_only and not allow_location_evolution:
            if not isinstance(parsed, dict):
                # Raw malformed text cannot be projected safely. A format
                # repair must not move an unconsumed proposal into core facts.
                initial_usage_handle.finalize(False)
                raise ValueError('world_outline_maintenance_response_not_object')
            # Unconsumed proposals must not become input to format repair and
            # reappear in another field. Rejected older batches are regenerated.
            parsed = {key: value for key, value in parsed.items() if key != 'location_state_changes'}
            content = json.dumps(parsed, ensure_ascii=False)
        original_location_field_present = (
            allow_location_evolution and isinstance(parsed, dict)
            and 'location_state_changes' in parsed
        )
        original_location_changes = parsed.get('location_state_changes') if isinstance(parsed, dict) else None
        original_faction_profile_updates: list[dict] = []
        original_faction_profile_diagnostics: list[str] = []
        original_faction_profile_field_present = bool(
            isinstance(parsed, dict) and 'faction_profile_updates' in parsed
        )
        if original_faction_profile_field_present:
            (
                original_faction_profile_updates,
                original_faction_profile_diagnostics,
            ) = normalize_faction_profile_updates(
                parsed.get('faction_profile_updates'),
            )
        (
            maintained,
            structural_errors,
            diagnostics,
            maintenance_contract_valid,
        ) = normalize_maintenance_result_with_status(
            parsed,
            cycle_reseed_only=cycle_reseed_only,
            max_world_actor_updates=max_world_actor_updates,
            location_state_snapshot=authoritative_location_state_snapshot,
            allow_location_evolution=allow_location_evolution,
        )
        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get('world_actors'), list)
            and len(parsed['world_actors']) > max_world_actor_updates
        ):
            raise ValueError(
                f'world_actors 超过消费上限 {max_world_actor_updates} 项'
            )
        repair_attempted = bool(structural_errors)
        repair_usage = None
        repair_usage_handle: _DeferredWorldOutlineTokenUsage | None = None
        if repair_attempted:
            initial_usage_handle.finalize(False)
        if structural_errors:
            repair_request_metadata: dict[str, str] = {}
            (
                repaired_content,
                repair_usage,
            ) = S1WorldOutlineService._repair_maintenance_result_structure(
                content,
                structural_errors=structural_errors,
                cycle_reseed_only=cycle_reseed_only,
                api_base=api_base,
                api_key=api_key,
                model=model,
                timeout=timeout,
                request_metadata_out=repair_request_metadata,
                usage_context=usage_context,
                player_names=tuple(dict.fromkeys(source_player_names)),
            )
            repair_usage_handle = _DeferredWorldOutlineTokenUsage(
                call_type='world_outline_maintenance_ai',
                usage=repair_usage,
                model=repair_request_metadata.get('model') or model,
                attempt_number=2,
                usage_context=usage_context,
                finish_reason=repair_request_metadata.get('finish_reason'),
                repair_applied=True,
            )
            usage_handles.append(repair_usage_handle)
            parsed = S1WorldOutlineService._parse_json_object(repaired_content)
            # 势力档案是辅助提案，不允许结构修复 AI 改写、补造或删除。
            if isinstance(parsed, dict):
                parsed = dict(parsed)
                parsed.pop('faction_profile_updates', None)
                parsed.pop('location_state_changes', None)
                if original_location_field_present:
                    parsed['location_state_changes'] = original_location_changes
            (
                maintained,
                structural_errors,
                diagnostics,
                maintenance_contract_valid,
            ) = normalize_maintenance_result_with_status(
                parsed,
                cycle_reseed_only=cycle_reseed_only,
                max_world_actor_updates=max_world_actor_updates,
                location_state_snapshot=authoritative_location_state_snapshot,
                allow_location_evolution=allow_location_evolution,
            )
            if (
                isinstance(parsed, dict)
                and isinstance(parsed.get('world_actors'), list)
                and len(parsed['world_actors']) > max_world_actor_updates
            ):
                raise ValueError(
                    f'world_actors 超过消费上限 {max_world_actor_updates} 项'
                )
            if maintained is not None and original_faction_profile_field_present:
                maintained['faction_profile_updates'] = (
                    original_faction_profile_updates
                )
                diagnostics.extend(original_faction_profile_diagnostics)
        if maintained is None or structural_errors:
            final_usage_handle = (
                repair_usage_handle if repair_attempted else initial_usage_handle
            )
            if final_usage_handle is not None:
                final_usage_handle.finalize(False)
            raise ValueError(f'大纲维护 AI 输出合同非法: {structural_errors or diagnostics}')
        final_usage_handle = (
            repair_usage_handle if repair_attempted else initial_usage_handle
        )
        if cycle_reseed_only:
            cycle_reseed_rows, contract_error = (
                _validate_cycle_reseed_maintenance_candidate(
                    parsed,
                    maintained,
                    existing_threads=existing_threads,
                    authoritative_location_state_snapshot=(
                        authoritative_location_state_snapshot
                    ),
                    dungeon=dungeon,
                )
            )
            cycle_contract_valid = contract_error is None
        else:
            raw_ordinary_rows = maintained.get('world_story_threads', [])
            cycle_contract_valid = not any(
                isinstance(row, dict) and row.get('cycle_reseed') is True
                for row in raw_ordinary_rows
            )
            candidate_id_by_debt_log_id = {
                _coerce_int(row.get('expedition_log_id'), 0): _coerce_int(
                    row.get('candidate_id'), 0,
                )
                for row in payload
                if isinstance(row, dict)
                and _normalize_ecology_continuity_debt(
                    row.get('ecology_continuity_debt')
                )
            }
            debt_handoff_note_by_log_id = {
                _coerce_int(row.get('expedition_log_id'), 0): _soft_text(
                    row.get('dm_handoff_note_candidate')
                )
                for row in payload
                if isinstance(row, dict)
                and _normalize_ecology_continuity_debt(
                    row.get('ecology_continuity_debt')
                )
            }
            (
                debt_resolutions_valid,
                normalized_debt_resolutions,
                debt_authority_by_index,
            ) = _normalize_maintenance_debt_resolution_contract(
                maintained,
                payload=payload,
                world_story_write_authority=world_story_write_authority,
                ecology_continuity_debt_log_ids=(
                    ecology_continuity_debt_log_ids
                ),
                has_ecology_continuity_debt=has_ecology_continuity_debt,
                diagnostics=diagnostics,
            )
            if not debt_resolutions_valid:
                cycle_contract_valid = False
                contract_error = ECOLOGY_CONTINUITY_DEBT_RECEIPT_ERROR
            else:
                contract_error = '普通大纲维护不得输出 cycle_reseed'
            cycle_reseed_rows = []
        if not cycle_contract_valid:
            raise ValueError(contract_error)
        if not cycle_reseed_only:
            campaign_contract_error = validate_dispatch_maintenance_changes(
                maintained.get('world_story_threads', []), campaign_required_changes,
            )
            if campaign_contract_error:
                raise ValueError(campaign_contract_error)
        if cycle_reseed_only:
            maintained['dm_handoff_note'] = ''
            maintained['world_actors'] = []
            maintained['world_story_threads'] = cycle_reseed_rows
        else:
            maintained.pop('cycle_reseed_transition', None)
            maintained.pop('ecology_continuity_debt_resolutions', None)
            maintained['world_story_threads'] = [
                {
                    key: value
                    for key, value in row.items()
                    if key != 'cycle_reseed'
                }
                for row in maintained.get('world_story_threads', [])
                if isinstance(row, dict)
            ]
            maintained['_ecology_continuity_debt_resolutions'] = [
                dict(item) for item in normalized_debt_resolutions
            ]
            maintained['_ecology_debt_world_story_authority_by_index'] = {
                int(index): sorted(candidate_ids)
                for index, candidate_ids in debt_authority_by_index.items()
            }
            maintained['_ecology_debt_candidate_id_by_log_id'] = dict(
                candidate_id_by_debt_log_id
            )
            maintained['_ecology_debt_handoff_note_by_log_id'] = dict(
                debt_handoff_note_by_log_id
            )
            if continuity_revival_target:
                # Server-owned authority is never accepted from model output. It
                # accompanies the normalized proposal only for the transactional
                # identity/revision recheck in the existing maintenance consumer.
                maintained['_continuity_revival_target'] = dict(
                    continuity_revival_target,
                )
        if allow_location_evolution and maintained.get('location_state_changes'):
            location_allowed, review_diagnostics = S1WorldOutlineService._review_cycle_reseed_proposals(
                target_dungeon=dungeon, proposals=[], cycle_reseed_transition={},
                existing_world_story_threads=existing_threads,
                authoritative_location_state_snapshot=authoritative_location_state_snapshot,
                api_base=api_base, api_key=api_key, model=model, timeout=timeout,
                usage_handles=usage_handles, usage_context=usage_context,
                player_names=tuple(dict.fromkeys(source_player_names)),
                source_candidates=payload,
                maintenance_candidate={
                    key: maintained[key] for key in (
                        'location_state_changes', 'dm_handoff_note', 'world_story_threads',
                        'world_actors', 'faction_profile_updates',
                    ) if key in maintained
                },
            )
            if location_allowed is False:
                # A rejected future state must not survive via notes or sibling
                # objects. The settled expedition is never regenerated here.
                if final_usage_handle is not None:
                    final_usage_handle.mark_contract_valid(
                        bool(cycle_contract_valid and maintenance_contract_valid)
                    )
                return _WorldLocationMaintenanceRejection(tuple(diagnostics + review_diagnostics))
            if location_allowed is not True:
                raise ValueError('world_location_review_decision_invalid')
            maintained['_location_state_review'] = 'allow'
            maintained['_maintenance_location_snapshot'] = authoritative_location_state_snapshot
        if cycle_reseed_rows:
            approved_cycle_reseeds, review_diagnostics = (
                S1WorldOutlineService._review_cycle_reseed_proposals(
                    target_dungeon=dungeon,
                    proposals=cycle_reseed_rows,
                    cycle_reseed_transition=maintained['cycle_reseed_transition'],
                    existing_world_story_threads=existing_threads,
                    authoritative_location_state_snapshot=authoritative_location_state_snapshot,
                    api_base=api_base,
                    api_key=api_key,
                    model=model,
                    timeout=timeout,
                    usage_handles=usage_handles,
                    usage_context=usage_context,
                    player_names=tuple(dict.fromkeys(source_player_names)),
                )
            )
            diagnostics.extend(review_diagnostics)
            maintained['world_story_threads'] = [
                row
                for row in maintained.get('world_story_threads', [])
                if (
                    not isinstance(row, dict)
                    or row.get('cycle_reseed') is not True
                    or (
                        _coerce_int(row.get('source_world_story_thread_id'), 0),
                        _coerce_int(row.get('source_revision'), 0),
                        _clip_text(row.get('title'), 120),
                    ) in approved_cycle_reseeds
                )
            ]
            if not maintained['world_story_threads']:
                maintained.pop('cycle_reseed_transition', None)
        if faction_profile_bootstrap_plan and not cycle_reseed_only:
            target_thread_id = _coerce_int(
                faction_profile_bootstrap_plan['prompt_context'][
                    'target_world_story_thread'
                ].get('id'),
                0,
            )
            if target_thread_id > 0:
                maintained['_faction_profile_bootstrap_source_overrides'] = {
                    target_thread_id: dict(
                        faction_profile_bootstrap_plan['source_override']
                    ),
                }
        maintained['_diagnostics'] = diagnostics
        maintained['_world_actor_capability_bases'] = {
            str(actor['id']): {key: actor.get(key) for key in (
                'capability_revision', 'capability_profile', 'power_boundary')}
            for actor in existing_world_actors if isinstance(actor, dict) and actor.get('id')
        }
        if final_usage_handle is not None:
            final_usage_handle.mark_contract_valid(
                bool(cycle_contract_valid and maintenance_contract_valid)
            )
        return maintained

    @staticmethod
    def _review_cycle_reseed_proposals(
        *,
        target_dungeon: str,
        proposals: list[dict],
        cycle_reseed_transition: dict,
        existing_world_story_threads: list[dict],
        authoritative_location_state_snapshot: dict,
        api_base: str,
        api_key: str,
        model: str,
        timeout: int,
        usage_handles: list[_DeferredWorldOutlineTokenUsage] | None = None,
        usage_context: dict | None = None,
        player_names: tuple[str, ...] = (),
        maintenance_candidate: dict | None = None,
        source_candidates: list[dict] | None = None,
    ) -> tuple[set[tuple[int, int, str]] | bool, list[str]]:
        owns_usage_handles = usage_handles is None
        active_usage_handles = usage_handles if usage_handles is not None else []
        consumer_ready = False
        try:
            result = S1WorldOutlineService._review_cycle_reseed_proposals_impl(
                target_dungeon=target_dungeon,
                proposals=proposals,
                cycle_reseed_transition=cycle_reseed_transition,
                existing_world_story_threads=existing_world_story_threads,
                authoritative_location_state_snapshot=(
                    authoritative_location_state_snapshot
                ),
                api_base=api_base,
                api_key=api_key,
                model=model,
                timeout=timeout,
                usage_handles=active_usage_handles,
                usage_context=usage_context,
                player_names=player_names,
                maintenance_candidate=maintenance_candidate,
                source_candidates=source_candidates,
            )
            consumer_ready = True
            return result
        finally:
            if owns_usage_handles:
                _finalize_world_outline_usage_handles(
                    active_usage_handles,
                    consumer_ready,
                )

    @staticmethod
    def _review_cycle_reseed_proposals_impl(
        *,
        target_dungeon: str,
        proposals: list[dict],
        cycle_reseed_transition: dict,
        existing_world_story_threads: list[dict],
        authoritative_location_state_snapshot: dict,
        api_base: str,
        api_key: str,
        model: str,
        timeout: int,
        usage_handles: list[_DeferredWorldOutlineTokenUsage],
        usage_context: dict | None = None,
        player_names: tuple[str, ...] = (),
        maintenance_candidate: dict | None = None,
        source_candidates: list[dict] | None = None,
    ) -> tuple[set[tuple[int, int, str]] | bool, list[str]]:
        """Existing world-state reviewer: successor keys or one local package decision."""
        review_prompt = (
            '你是深渊酒馆 S1 公共世界线循环播种审核 AI。'
            '只审核 cycle_reseed 后继与其权威地点状态转换，不改写提案、不补造事实。'
            '维护 AI 已取得从既成终态向前模拟一个当前后继的限定权限；'
            '不得仅因具体后继或过渡原因没有作为独立事实预存在旧输入中而拒绝。'
            '只有以下条件同时成立才 allow：准确 dormant 或 resolved 终态前驱的结果保持有效；'
            'authoritative_location_state_snapshot.is_explorable 精确为 false；'
            '提案从未被撤销的前驱结果、权威当前态与 target_dungeon 主题身份连续推演，'
            '不与公共地点权威现状冲突，也不把旧状态回档；'
            'summary 已建立持续作用的阶段接替机制、当前可观察状态，'
            '以及可反复探索与互动的当前基础；'
            'cycle_reseed_transition.location_state_change 与 summary 描述同一当前因果，'
            '使用快照中的准确地点身份和 revision，或以 revision 0 建立一个尚未登记的稳定地点；'
            '其 new_state 是变化后的完整当前态，而不是一次性发现、未成立计划或等待未来剧情兑现的线索；'
            'dungeon_explorability_change 准确表达 false 到 true，且只有该地点当前态已经让正常探索路径现实成立时才允许；'
            '后续玩家能够从实际痕迹、作用或主体反应理解这条因果桥。'
            '仅靠主题相似、可玩性诉求、一次性库存、后台刷新或未成立的未来可能性均应 reject；'
            '提案自身没有把输入起点到当前状态的因果建立完整时 reject。'
            '对每个输入提案恰好输出一项，原样回传 source_world_story_thread_id、source_revision、title；'
            'decision 只能是 allow 或 reject。'
            '只输出纯 JSON：{"cycle_reseed_reviews":[{"source_world_story_thread_id":1,'
            '"source_revision":1,"title":"<title>","decision":"allow|reject"}]}。'
        )
        if maintenance_candidate is not None:
            review_prompt = (
                '你是深渊酒馆 S1 世界状态维护审核 AI。本轮审核普通维护的局部地点演变，'
                '以下合同约束 maintenance_candidate，不是要求你重新生成地点：'
                + WORLD_LOCATION_MAINTENANCE_RULE
                + '核对完整地点变化包是否由冻结地点基线、实际条件与时间因果支持，'
                '并确认同一候选没有把这些尚待审核的变化另写入便签、世界线、主体或势力。'
                'new_candidates 是本批已结算事实，只用于区分已经成立的普通维护内容和本轮推演；不得把二者混为一项未批准变化。'
                '维护具有上述限定的向前推演权限，具体接替表现不必事先逐字存在于输入；'
                '但不能据资源不足、后续可玩性或玩家能力创造条件，也不能跳过需要的外部投入与主动行为。'
                '全部符合时 allow，任何一项不成立或通过其它对象绕过地点权限时 reject。'
                '只输出纯 JSON 对象 {"decision":"allow"} 或 {"decision":"reject"}；'
                '不修写候选、不裁决已结束剧情、不补发收益。'
            )

        def validate_cycle_reseed_review_response(decoded):
            parsed_candidate = decoded.json_result.data
            if maintenance_candidate is not None:
                decision = parsed_candidate.get('decision') if isinstance(parsed_candidate, dict) else None
                if (
                    decoded.http_result.finish_reason == 'length'
                    or not isinstance(decision, str) or decision not in {'allow', 'reject'}
                ):
                    return S1AiResponseContractResult.reject('world_location_review_decision_invalid')
                return S1AiResponseContractResult.accept(decision == 'allow')
            raw_reviews = (
                parsed_candidate.get('cycle_reseed_reviews')
                if isinstance(parsed_candidate, dict)
                else None
            )
            expected: set[tuple[int, int, str]] = set()
            for row in proposals:
                if not isinstance(row, dict):
                    return S1AiResponseContractResult.reject(
                        'cycle_reseed_review_input_invalid'
                    )
                source_id, _ = normalize_ascii_integer(
                    row.get('source_world_story_thread_id'),
                    minimum=1,
                )
                source_revision, _ = normalize_ascii_integer(
                    row.get('source_revision'),
                    minimum=1,
                )
                title = row.get('title')
                if (
                    type(source_id) is not int
                    or type(source_revision) is not int
                    or not isinstance(title, str)
                    or not title.strip()
                ):
                    return S1AiResponseContractResult.reject(
                        'cycle_reseed_review_input_invalid'
                    )
                expected.add((source_id, source_revision, title))
            if len(expected) != len(proposals) or not isinstance(raw_reviews, list):
                return S1AiResponseContractResult.reject(
                    'cycle_reseed_reviews_invalid'
                )
            decisions: dict[tuple[int, int, str], str] = {}
            allowed_fields = frozenset({
                'source_world_story_thread_id',
                'source_revision',
                'title',
                'decision',
            })
            for raw in raw_reviews:
                if not isinstance(raw, dict):
                    return S1AiResponseContractResult.reject(
                        'cycle_reseed_review_row_invalid'
                    )
                projected = {
                    field: raw[field]
                    for field in allowed_fields
                    if field in raw
                }
                source_id, _ = normalize_ascii_integer(
                    projected.get('source_world_story_thread_id'),
                    minimum=1,
                )
                source_revision, _ = normalize_ascii_integer(
                    projected.get('source_revision'),
                    minimum=1,
                )
                title = projected.get('title')
                decision_raw = projected.get('decision')
                if (
                    type(source_id) is not int
                    or type(source_revision) is not int
                    or not isinstance(title, str)
                    or not title.strip()
                    or not isinstance(decision_raw, str)
                ):
                    return S1AiResponseContractResult.reject(
                        'cycle_reseed_review_row_invalid'
                    )
                key = (source_id, source_revision, title)
                decision = decision_raw.strip().lower()
                if (
                    key not in expected
                    or decision not in {'allow', 'reject'}
                    or key in decisions
                ):
                    return S1AiResponseContractResult.reject(
                        'cycle_reseed_review_decision_invalid'
                    )
                decisions[key] = decision
            if set(decisions) != expected:
                return S1AiResponseContractResult.reject(
                    'cycle_reseed_review_scope_incomplete'
                )
            # 全 reject 也是完整有效的审核结果。
            return S1AiResponseContractResult.accept(decisions)

        response = _post_world_outline_ai_request(
            f'{api_base}/chat/completions',
            stage='cycle_reseed_review',
            player_names=player_names,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_payload={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': review_prompt},
                    {
                        'role': 'user',
                        'content': json.dumps({
                            'target_dungeon': target_dungeon,
                            'existing_world_story_threads': existing_world_story_threads,
                            'authoritative_location_state_snapshot': (
                                authoritative_location_state_snapshot
                            ),
                            **(
                                {
                                    'maintenance_candidate': maintenance_candidate,
                                    'new_candidates': source_candidates or [],
                                }
                                if maintenance_candidate is not None else {
                                    'cycle_reseed_proposals': proposals,
                                    'cycle_reseed_transition': cycle_reseed_transition,
                                }
                            ),
                        }, ensure_ascii=False),
                    },
                ],
                'temperature': 0,
            },
            timeout=timeout,
            response_validator=validate_cycle_reseed_review_response,
            usage_context=usage_context,
        )
        decoded = parse_s1_ai_http_json_response(
            response,
            check_http_status=False,
        )
        usage = decoded.http_result.usage
        usage_handle = _DeferredWorldOutlineTokenUsage(
            call_type='world_outline_cycle_reseed_review_ai',
            usage=usage,
            model=(
                get_s1_ai_response_metadata(response).get('model') or model
            ),
            attempt_number=1,
            usage_context=usage_context,
            finish_reason=decoded.http_result.finish_reason,
            response_error_code=decoded.json_result.error_code,
            repair_applied=decoded.json_result.repair_applied,
        )
        usage_handles.append(usage_handle)
        parsed = decoded.json_result.data
        if maintenance_candidate is not None:
            decision = parsed.get('decision') if isinstance(parsed, dict) else None
            valid = (
                decoded.http_result.finish_reason != 'length'
                and isinstance(decision, str) and decision in {'allow', 'reject'}
            )
            usage_handle.mark_contract_valid(valid)
            if not valid:
                raise ValueError('world_location_review_decision_invalid')
            return decision == 'allow', ([] if decision == 'allow' else ['地点演变候选未获审核通过'])
        raw_reviews = parsed.get('cycle_reseed_reviews') if isinstance(parsed, dict) else None
        expected: set[tuple[int, int, str]] = set()
        expected_structure_valid = True
        for row in proposals:
            if not isinstance(row, dict):
                expected_structure_valid = False
                break
            source_id, _ = normalize_ascii_integer(
                row.get('source_world_story_thread_id'),
                minimum=1,
            )
            source_revision, _ = normalize_ascii_integer(
                row.get('source_revision'),
                minimum=1,
            )
            title = row.get('title')
            if (
                type(source_id) is not int
                or source_id <= 0
                or type(source_revision) is not int
                or source_revision <= 0
                or not isinstance(title, str)
                or not title.strip()
            ):
                expected_structure_valid = False
                break
            expected.add((source_id, source_revision, title))
        decisions: dict[tuple[int, int, str], str] = {}
        valid_structure = (
            expected_structure_valid
            and len(expected) == len(proposals)
            and isinstance(raw_reviews, list)
        )
        allowed_review_fields = frozenset({
            'source_world_story_thread_id',
            'source_revision',
            'title',
            'decision',
        })
        if valid_structure:
            for raw in raw_reviews:
                if not isinstance(raw, dict):
                    valid_structure = False
                    break
                projected = {
                    field: raw[field]
                    for field in allowed_review_fields
                    if field in raw
                }
                source_id, _ = normalize_ascii_integer(
                    projected.get('source_world_story_thread_id'),
                    minimum=1,
                )
                source_revision, _ = normalize_ascii_integer(
                    projected.get('source_revision'),
                    minimum=1,
                )
                title = projected.get('title')
                raw_decision = projected.get('decision')
                if (
                    type(source_id) is not int
                    or source_id <= 0
                    or type(source_revision) is not int
                    or source_revision <= 0
                    or not isinstance(title, str)
                    or not title.strip()
                    or not isinstance(raw_decision, str)
                    or not raw_decision.strip()
                ):
                    valid_structure = False
                    break
                key = (
                    source_id,
                    source_revision,
                    title,
                )
                decision = raw_decision.strip().lower()
                if key not in expected or decision not in {'allow', 'reject'} or key in decisions:
                    valid_structure = False
                    break
                decisions[key] = decision
        valid_structure = valid_structure and set(decisions) == expected
        usage_handle.mark_contract_valid(valid_structure)
        if not valid_structure:
            raise ValueError('循环播种审核输出结构非法')
        rejected_count = sum(
            1 for decision in decisions.values() if decision == 'reject'
        )
        return {
            key for key, decision in decisions.items() if decision == 'allow'
        }, ([f'循环播种审核拒绝 {rejected_count} 项'] if rejected_count else [])

    @staticmethod
    def _repair_maintenance_result_structure(
        content: str,
        *,
        structural_errors: list[str],
        cycle_reseed_only: bool = False,
        api_base: str,
        api_key: str,
        model: str,
        timeout: int,
        request_metadata_out: dict[str, str] | None = None,
        usage_context: dict | None = None,
        player_names: tuple[str, ...] = (),
    ) -> tuple[str, dict | None]:
        """Repair only the maintenance JSON containers; never recast world facts."""
        repair_prompt = (
            '你是深渊酒馆 S1 世界大纲维护输出的 JSON 结构修复器。'
            '只能修复 JSON 语法和固定字段容器类型，禁止改写、扩写、删除或重新判断任何世界事实、世界线状态、主体身份与关系。'
            + (
                '本轮是 cycle_reseed_only；顶层当前字段为 dm_handoff_note、world_story_threads、world_actors、cycle_reseed_transition。'
                '前三者依次必须是字符串、数组、数组；cycle_reseed_transition 必须保持原有对象内容，不得补造、删改或从其它字段推导。'
                if cycle_reseed_only
                else (
                    '顶层核心字段为 dm_handoff_note、world_story_threads、world_actors、'
                    'ecology_continuity_debt_resolutions；前者必须是字符串，后三者必须是数组。'
                    '债务回执的来源日志、decision 与最终世界线下标必须保留原有绑定；'
                    '缺失绑定不从标题或便签推断。'
                    '输出只含上述核心字段，其它字段不属于本次修复范围。'
                )
            )
            + '缺失容器只能补为空字符串或空数组；无法归入合法容器的内容必须原样保留在其原有合法字段内，不得编造。'
            '只输出单个合法 JSON 对象，不要解释，不要使用代码围栏。'
        )
        resp = _post_world_outline_ai_request(
            f'{api_base}/chat/completions',
            stage='structure_repair',
            player_names=player_names,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json_payload={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': repair_prompt},
                    {
                        'role': 'user',
                        'content': (
                            f'结构错误：{json.dumps(structural_errors, ensure_ascii=False)}\n'
                            f'原始输出：\n{content}'
                        ),
                    },
                ],
                'temperature': 0,
            },
            timeout=timeout,
            usage_context=usage_context,
        )
        decoded = parse_s1_ai_http_json_response(
            resp,
            check_http_status=False,
        )
        usage = decoded.http_result.usage
        runtime_metadata = get_s1_ai_response_metadata(resp)
        if request_metadata_out is not None:
            request_metadata_out.update(runtime_metadata)
            if decoded.http_result.finish_reason is not None:
                request_metadata_out['finish_reason'] = decoded.http_result.finish_reason
        runtime_model = runtime_metadata.get('model') or model
        try:
            if not decoded.json_result.ok:
                raise ValueError(
                    'world_outline_structure_repair_response_invalid:'
                    f'{decoded.json_result.error_code}'
                )
            repaired_content = decoded.json_result.content
        except Exception:
            _DeferredWorldOutlineTokenUsage(
                call_type='world_outline_maintenance_ai',
                usage=usage,
                model=runtime_model,
                attempt_number=2,
                usage_context=usage_context,
                finish_reason=decoded.http_result.finish_reason,
                response_error_code=decoded.json_result.error_code,
                repair_applied=True,
            ).finalize(False)
            raise
        return repaired_content, usage

    @staticmethod
    def _parse_json_object(text: str) -> dict | None:
        text = (text or '').strip()
        if '```' in text:
            start = text.find('{')
            end = text.rfind('}')
            if start >= 0 and end > start:
                text = text[start:end + 1]
        try:
            return json.loads(text)
        except Exception:
            try:
                import json_repair
                obj = json_repair.loads(text)
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
