"""Asynchronously build missing authoritative capability profiles for world actors."""

from __future__ import annotations

import json
import logging

import requests
from flask import current_app
from sqlalchemy import or_

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1WorldActor,
    AdventurerS1WorldStoryThread,
    AdventurerS1WorldStoryThreadActor,
)
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    S1AiResponseContractResult,
    apply_s1_ai_field_schema,
    parse_s1_ai_http_json_response,
    parse_s1_ai_json_content,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
    AIProfileAssignmentError,
    enrich_s1_ai_response_metadata,
    log_s1_ai_response_outcome,
    post_s1_ai_request,
    resolve_s1_ai_profile_snapshot,
)
from plugins.adventurer_s1.services.s1_capability_profile_contract import (
    ACTOR_CAPABILITY_FIELD_SCHEMA,
    normalize_actor_capability_profile,
)
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage

logger = logging.getLogger(__name__)

_PROFILE_DETAIL_NORMAL_TARGET = 24

_PROFILE_FIELD_SCHEMA = {
    'type': 'object', 'properties': {
        'world_actor_id': {'type': ['integer', 'string']},
        'capability_profile': ACTOR_CAPABILITY_FIELD_SCHEMA,
    },
}


_SYSTEM_PROMPT = """你是深渊酒馆 S1 的长期世界角色能力画像维护 AI。

【职责】
把一个长期世界角色已经持久化的身份、力量边界、稳定原则、当前状态与绑定世界线，整理成一份可跨副本复用的权威能力画像。输入资料是本次唯一事实来源；描述之间有不确定处时采用能够同时容纳现有事实的保守边界。

【能力画像契约】
- basis_level 是大于等于 1 的 JSON 整数，表示该角色既有能力层级所对应的常规冒险者共同参照。它固定来自角色既有描述与 power_tier，独立于副本等级和队伍强弱。
- typical_profile 表示该角色在既定身份下通常具备的能力结构；current_profile 表示输入所记录当前状态下的实际能力。
- typical_profile 与 current_profile 都完整包含 force、mobility、cognition、durability、perception；每项只能是 very_low、low、baseline、high、very_high、exceptional。
- condition 只能是 impaired、normal、enhanced；typical_profile 与 current_profile 的差异由输入中已经成立的伤势、强化、异常或状态支持。
- number_pressure 只能是 none、low、medium、high、overwhelming，只表达该角色已建立的群体、分身、召唤物或持续覆盖带来的数量压力；单体五维保持角色自身结构。
- capabilities 整理基础五维以外、且由输入已经支持的作用规则；limitations 只整理输入已经支持的真实能力边界。
- capabilities 与 limitations 都是 JSON 字符串数组（string[]）。每个元素只能是一段完整字符串，用该字符串说明对象、条件、效果、范围与边界；不得输出对象、子数组或其他类型。资料没有建立额外能力或限制时，相应数组可以为空。
- capabilities 与 limitations 的正常目标分别不超过 __PROFILE_DETAIL_NORMAL_TARGET__ 项；先对每项 trim，trim 后精确相同的字符串只保留首次，结果按输入资料中的首次出现顺序排列。若互异的独立事实超过正常目标，必须全部保留，不得截断、合并或置空。

【权威边界】
保持角色既有身份、能力家族、作用范围、代价、限制与当前状态。绑定世界线只补充该角色已经成立的公共事实。输出画像止于当前权威能力事实；未来剧情结果、队伍、地牢适配、处理方案与临场弱点均在本次职责范围之外。

【输出】
输出内容是一个独立 JSON 对象。顶层字段固定为 world_actor_id、capability_profile。capability_profile 字段固定为 basis_level、typical_profile、current_profile、condition、number_pressure、capabilities、limitations。

非空数组的纯结构模板为 ["<string>"]；<string> 只是字符串类型占位符，不是应当原样输出的内容。

结构占位：
{"world_actor_id":1,"capability_profile":{"basis_level":1,"typical_profile":{"force":"baseline","mobility":"baseline","cognition":"baseline","durability":"baseline","perception":"baseline"},"current_profile":{"force":"baseline","mobility":"baseline","cognition":"baseline","durability":"baseline","perception":"baseline"},"condition":"normal","number_pressure":"none","capabilities":[],"limitations":[]}}""".replace(
    '__PROFILE_DETAIL_NORMAL_TARGET__',
    str(_PROFILE_DETAIL_NORMAL_TARGET),
)


def _runtime_metadata_bool(value: object) -> bool | None:
    normalized = str(value or '').strip().lower()
    if normalized == 'true':
        return True
    if normalized == 'false':
        return False
    return value if type(value) is bool else None


def _deduplicate_profile_detail_strings(profile: dict) -> dict:
    """Canonicalize exact duplicates after the shared contract trims each string."""
    normalized = dict(profile)
    for key in ('capabilities', 'limitations'):
        normalized[key] = list(dict.fromkeys(profile[key]))
    return normalized


def _parse_profile_payload(
    raw: object,
    *,
    world_actor_id: int,
) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, 'output.not_object'
    raw = {
        key: raw[key] for key in ('world_actor_id', 'capability_profile') if key in raw
    }
    if frozenset(raw) != {'world_actor_id', 'capability_profile'}:
        return None, 'output.required_fields_missing'
    normalized_actor_id, _ = normalize_ascii_integer(
        raw.get('world_actor_id'),
        minimum=1,
    )
    if type(normalized_actor_id) is not int:
        return None, 'world_actor_id.invalid'
    if normalized_actor_id != int(world_actor_id):
        return None, 'world_actor_id.mismatch'
    profile = normalize_actor_capability_profile(raw.get('capability_profile'))
    if profile is None:
        return None, 'capability_profile.invalid'
    return _deduplicate_profile_detail_strings(profile), None


def _parse_profile_response(content: str, *, world_actor_id: int) -> dict | None:
    """Compatibility helper for direct callers and focused contract tests."""
    result = parse_s1_ai_json_content(
        content, repair_mode='lossless_syntax_only',
        field_schema=_PROFILE_FIELD_SCHEMA, field_contract='world_actor_profile',
    )
    if not result.ok:
        return None
    profile, _error_code = _parse_profile_payload(result.data, world_actor_id=world_actor_id)
    return profile


def _build_actor_evidence(actor: AdventurerS1WorldActor) -> dict:
    links = AdventurerS1WorldStoryThreadActor.query.filter_by(actor_id=int(actor.id)).all()
    thread_ids = list(dict.fromkeys(int(link.world_story_thread_id) for link in links))[:8]
    threads = []
    if thread_ids:
        rows = AdventurerS1WorldStoryThread.query.filter(
            AdventurerS1WorldStoryThread.id.in_(thread_ids),
        ).order_by(AdventurerS1WorldStoryThread.id.asc()).all()
        threads = [
            {
                'world_story_thread_id': int(row.id),
                'title': row.title,
                'status': row.status,
                'summary': row.summary,
                'power_tier': row.power_tier,
                'power_profile': row.power_profile,
            }
            for row in rows
        ]
    return {
        'world_actor_id': int(actor.id),
        'canonical_name': actor.canonical_name,
        'actor_type': actor.actor_type,
        'status': actor.status,
        'identity_summary': actor.identity_summary,
        'stable_principles': actor.stable_principles,
        'power_tier': actor.power_tier,
        'power_boundary': actor.power_boundary,
        'long_term_goal': actor.long_term_goal,
        'current_agenda': actor.current_agenda,
        'current_state': actor.current_state,
        'linked_world_story_threads': threads,
    }


def submit_world_actor_profile_backfill(world_actor_id: int) -> bool:
    """Queue one idempotent task; AsyncTaskService dedupes by actor id."""
    from services.async_task_service import AsyncTaskService

    actor_id = int(world_actor_id or 0)
    if actor_id <= 0:
        return False
    return AsyncTaskService.submit_task(
        'ai_s1_world_actor_profile_backfill',
        {'world_actor_id': actor_id},
    )


def submit_missing_world_actor_profiles(actor_ids: list[int] | None = None) -> int:
    """Queue missing active profiles from explicit ids or a bounded safety-net scan."""
    if not current_app.config.get('ADVENTURER_S1_WORLD_ACTOR_PROFILE_BACKFILL_ENABLED', True):
        return 0
    query = AdventurerS1WorldActor.query.filter(
        AdventurerS1WorldActor.status == 'active',
        or_(
            AdventurerS1WorldActor.capability_profile.is_(None),
            AdventurerS1WorldActor.capability_revision <= 0,
        ),
    )
    if actor_ids is not None:
        normalized_ids = []
        for value in actor_ids:
            try:
                actor_id = int(value)
            except (TypeError, ValueError):
                continue
            if actor_id > 0 and actor_id not in normalized_ids:
                normalized_ids.append(actor_id)
        if not normalized_ids:
            return 0
        query = query.filter(AdventurerS1WorldActor.id.in_(normalized_ids))
        limit = len(normalized_ids)
    else:
        limit = max(1, int(current_app.config.get(
            'ADVENTURER_S1_WORLD_ACTOR_PROFILE_BACKFILL_BATCH_SIZE', 3,
        ) or 3))
    rows = query.order_by(
        AdventurerS1WorldActor.importance.desc(),
        AdventurerS1WorldActor.id.asc(),
    ).limit(limit).all()
    return sum(1 for row in rows if submit_world_actor_profile_backfill(int(row.id)))


def run_world_actor_profile_backfill_ai(world_actor_id: int) -> bool:
    """Worker entry: generate, fully validate, then atomically persist one missing profile."""
    actor_id = int(world_actor_id or 0)
    actor = AdventurerS1WorldActor.query.filter_by(id=actor_id).first()
    if not actor or actor.status != 'active':
        return False
    if actor.capability_profile and int(actor.capability_revision or 0) > 0:
        return True
    evidence_updated_at = actor.updated_at
    evidence = _build_actor_evidence(actor)

    try:
        route = resolve_s1_ai_profile_snapshot(
            'world_actor_profile_backfill'
        )
    except AIProfileAssignmentError as exc:
        logger.warning(
            '世界角色画像补全用途路由不可用 actor=%s error=%s',
            actor_id,
            exc,
        )
        return False
    api_key = route.api_key
    api_base = route.api_base
    model = route.model
    timeout = max(30, int(current_app.config.get(
        'ADVENTURER_S1_WORLD_ACTOR_PROFILE_BACKFILL_AI_TIMEOUT_SECONDS', 600,
    ) or 600))
    usage = None
    usage_recorded = False
    consumer_succeeded = False
    profile = None
    failure_stage = 'response_processing'
    failure_code = 'request_not_completed'
    finish_reason = None
    repair_applied = False
    failed_response_excerpt = None
    request_metadata: dict[str, object] = {}
    runtime_metadata: dict[str, object] = {}
    request_exception: BaseException | None = None

    def _record_call_usage() -> None:
        nonlocal usage_recorded
        if usage_recorded:
            return
        usage_recorded = True
        metadata = {**request_metadata, **runtime_metadata}
        record_token_usage(
            call_type='world_actor_profile_backfill_ai',
            usage=usage,
            model=str(metadata.get('model') or model),
            correlation_id=f'world_actor:{actor_id}',
            attempt_number=1,
            is_success=consumer_succeeded,
            failure_stage=None if consumer_succeeded else failure_stage,
            validation_error_code=None if consumer_succeeded else failure_code,
            finish_reason=finish_reason,
            json_object_mode=_runtime_metadata_bool(metadata.get('json_object_mode')),
            provider_request_id=(
                str(metadata['request_id']) if metadata.get('request_id') else None
            ),
            repair_applied=repair_applied,
            failed_response_excerpt=(
                None if consumer_succeeded else failed_response_excerpt
            ),
            record_without_usage=not isinstance(usage, dict) or not usage,
            request_metadata=request_metadata,
            request_exception=request_exception,
        )

    def validate_world_actor_profile_response(decoded) -> S1AiResponseContractResult:
        json_result = apply_s1_ai_field_schema(
            decoded.json_result, field_schema=_PROFILE_FIELD_SCHEMA,
            contract='world_actor_profile',
        )
        if not json_result.ok:
            return S1AiResponseContractResult.reject(
                json_result.error_code or 'response_parse_failed',
                failure_stage=('contract_validation' if json_result.error_code == 'field_name_compatibility_failed'
                               else 'response_parse'),
            )
        if json_result.finish_reason not in (None, 'stop'):
            return S1AiResponseContractResult.reject(
                'incomplete_output',
                failure_stage='response_parse',
            )
        normalized_profile, contract_error = _parse_profile_payload(
            json_result.data,
            world_actor_id=actor_id,
        )
        if normalized_profile is None:
            return S1AiResponseContractResult.reject(
                contract_error or 'capability_profile.invalid'
            )
        return S1AiResponseContractResult.accept(normalized_profile)

    try:
        try:
            db_manager.remove()
            response = post_s1_ai_request(
                f'{api_base}/chat/completions',
                feature='world_actor_profile_backfill',
                resolved_profile=route,
                request_metadata_out=request_metadata,
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={
                    'model': model,
                    'messages': [
                        {'role': 'system', 'content': _SYSTEM_PROMPT},
                        {
                            'role': 'user',
                            'content': '请根据以下权威资料补全该角色能力画像：\n'
                            + json.dumps(evidence, ensure_ascii=False, separators=(',', ':')),
                        },
                    ],
                    'temperature': 0.2,
                },
                timeout=timeout,
                json_object_mode_if_supported=True,
                response_validator=validate_world_actor_profile_response,
            )
            decoded = parse_s1_ai_http_json_response(
                response,
                repair_mode='lossless_syntax_only',
                field_schema=_PROFILE_FIELD_SCHEMA, field_contract='world_actor_profile',
            )
            payload = decoded.http_result.response_json or {}
            usage = decoded.http_result.usage
            response_metadata = enrich_s1_ai_response_metadata(response, payload)
            runtime_metadata.update(request_metadata)
            runtime_metadata.update(response_metadata)
            json_result = decoded.json_result
            finish_reason = json_result.finish_reason
            repair_applied = json_result.repair_applied
            failed_response_excerpt = json_result.raw_excerpt
            failure_stage = ('contract_validation' if json_result.error_code == 'field_name_compatibility_failed'
                             else 'response_parse')
            failure_code = json_result.error_code or 'response_parse_failed'
            if json_result.ok and json_result.finish_reason in (None, 'stop'):
                failure_stage = 'contract_validation'
                profile, contract_error = _parse_profile_payload(
                    json_result.data,
                    world_actor_id=actor_id,
                )
                failure_code = contract_error or 'none'
            elif json_result.ok:
                failure_code = 'incomplete_output'
            if profile is not None:
                failure_stage = 'downstream_consumption'
                failure_code = 'persistence_not_completed'
                failed_response_excerpt = None
            log_s1_ai_response_outcome(
                response,
                feature='world_actor_profile_backfill',
                result_status='usable' if profile is not None else 'unusable',
                finish_reason=json_result.finish_reason,
                error_code=None if profile is not None else failure_code,
                attempt_number=1,
            )
        except requests.RequestException as exc:
            request_exception = exc
            failure_stage = 'provider_request'
            failure_code = 'http_request_failed'
            logger.warning(
                '世界角色画像补全 AI 失败 actor=%s failure_code=%s error=%s',
                actor_id,
                failure_code,
                exc,
            )
        except (KeyError, TypeError, ValueError) as exc:
            request_exception = exc
            failure_stage = 'response_processing'
            failure_code = 'response_processing_failed'
            logger.warning(
                '世界角色画像补全 AI 失败 actor=%s failure_code=%s error=%s',
                actor_id,
                failure_code,
                exc,
            )

        if profile is None:
            logger.info('世界角色画像补全未落库 actor=%s failure_code=%s', actor_id, failure_code)
            return False
        with db_manager.transaction():
            locked = AdventurerS1WorldActor.query.filter_by(id=actor_id).with_for_update().first()
            if not locked:
                failure_code = 'world_actor_missing'
                return False
            if locked.status != 'active':
                failure_code = 'world_actor_not_active'
                return False
            if locked.capability_profile and int(locked.capability_revision or 0) > 0:
                failure_code = 'profile_already_completed'
                return True
            if locked.updated_at != evidence_updated_at:
                failure_code = 'evidence_changed'
                logger.info('世界角色画像补全结果过期，等待下次重试 actor=%s', actor_id)
                return False
            locked.capability_profile = profile
            locked.capability_revision = max(0, int(locked.capability_revision or 0)) + 1
            locked.capability_source_log_id = locked.last_seen_log_id
        consumer_succeeded = True
        failure_stage = None
        failure_code = 'none'
        return True
    finally:
        _record_call_usage()
