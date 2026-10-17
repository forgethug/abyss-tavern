"""S1 酒馆补贴申请 AI 核定服务。

本服务读取压缩后的结构化探险结算，提出是否发放、团队人均目标金额及申请者个人补贴目标。
后端仍是收款人、金额上限、历史补差、并发锁与最终落库的唯一执行权威。
"""
import json
import logging
import math
import requests
from flask import current_app
from plugins.adventurer_s1.services.s1_ai_runtime_config_service import AIProfileAssignmentError, enrich_s1_ai_response_metadata, log_s1_ai_response_outcome, resolve_s1_ai_profile_snapshot
from plugins.adventurer_s1.services.s1_ai_player_name_service import post_s1_ai_request_with_player_aliases as post_s1_ai_request, with_player_name_aliases
from plugins.adventurer_s1.services.s1_ai_json_response_service import S1AiResponseContractResult, build_s1_ai_raw_excerpt, parse_s1_ai_http_json_response
from plugins.adventurer_s1.services.s1_ai_output_normalization import normalize_ascii_finite_number
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
logger = logging.getLogger(__name__)
_TAVERN_RELIEF_AI_RESULT_FIELDS = ('decision', 'target_amount_per_person', 'applicant_bonus_target', 'summary', 'player_reply')
_ASCII_PROTOCOL_KEY_WHITESPACE_TRANSLATION = str.maketrans('', '', ' \t\r\n\x0c\x0b')

def _runtime_metadata_bool(value: object) -> bool | None:
    if type(value) is bool:
        return value
    normalized = str(value or '').strip().lower()
    if normalized == 'true':
        return True
    if normalized == 'false':
        return False
    return None
_TAVERN_RELIEF_AI_SYSTEM_PROMPT = '你是深渊酒馆 S1 的自由游玩补贴办理员。玩家选择补贴入口即提出领取补贴诉求，直接 approve，不判断其合理性、获益资格、收益是否相称或是否已经得到足够奖励。\n输入的当前结算数值只用于确定补贴金额，不能作为拒绝理由；没有指定金额时结合本次结算拟定正数目标，不设置平衡封顶或额外代价。保持玩家现有成果，不能扣回其它收益。\ntarget_amount_per_person 是每名存活参与者的金币目标额，为有限正数；applicant_bonus_target 是申请者额外有限非负目标额。limits.max_applicant_bonus_target=0 仅表示申请者没有可收款身份，个人额外目标必须为 0。后端按同一探险历史最高水位补差，不重复执行同一份发放。\n只输出纯 JSON，必填 decision="approve"、target_amount_per_person、applicant_bonus_target、summary（10..1000字中文审计说明）、player_reply（10..500字中文玩家说明）。不输出新领取人、其它货币或其它字段；不提前声称已经到账。\n'
_ALLOWED_CONTEXT_FIELDS = ('applicant', 'applicant_request', 'expedition', 'dungeon_profile', 'limits', 'settlement_summary', 'member_outcomes')
_ALLOWED_APPLICANT_FIELDS = ('adventurer_id', 'adventurer_name')
_ALLOWED_EXPEDITION_FIELDS = ('id', 'target_dungeon', 'party_name')
_ALLOWED_DUNGEON_PROFILE_FIELDS = ('is_special', 'level_label', 'min_level', 'max_level', 'special_reward_kind', 'opportunity_granted_count', 'opportunity_not_granted_count')
_ALLOWED_LIMIT_FIELDS = ('ordinary_appeal_cap_per_person', 'preferred_uplift_factor', 'max_target_per_person', 'max_team_total', 'max_applicant_bonus_target')
_ALLOWED_SUMMARY_FIELDS = ('member_count', 'alive_recipient_count', 'gold_total', 'average_gold_per_member', 'max_hp_loss_ratio', 'cost_events', 'durable_reward_events', 'deaths', 'settlement_completed')
_ALLOWED_MEMBER_OUTCOME_FIELDS = ('adventurer_id', 'name', 'is_applicant', 'alive', 'gold_change', 'hp_change', 'max_hp', 'is_dead', 'new_class_count', 'new_equipment_count', 'new_skill_count', 'new_trait_count', 'consumed_equipment_count', 'removed_skill_count', 'removed_trait_count', 'equipment_damage_count')

def _project_object(raw: object, allowed_fields: tuple[str, ...]) -> dict:
    source = raw if isinstance(raw, dict) else {}
    return {key: source.get(key) for key in allowed_fields if key in source}

def _project_tavern_relief_ai_context(context: dict) -> dict:
    """逐层白名单投影，只向 AI 暴露补贴申请裁定所需的结构化结算。"""
    raw = context if isinstance(context, dict) else {}
    projected = {key: raw.get(key) for key in _ALLOWED_CONTEXT_FIELDS if key not in {'applicant', 'expedition', 'dungeon_profile', 'limits', 'settlement_summary', 'member_outcomes'} and key in raw}
    projected['applicant'] = _project_object(raw.get('applicant'), _ALLOWED_APPLICANT_FIELDS)
    projected['expedition'] = _project_object(raw.get('expedition'), _ALLOWED_EXPEDITION_FIELDS)
    raw_dungeon_profile = raw.get('dungeon_profile')
    projected['dungeon_profile'] = _project_object(raw_dungeon_profile, _ALLOWED_DUNGEON_PROFILE_FIELDS)
    projected['limits'] = _project_object(raw.get('limits'), _ALLOWED_LIMIT_FIELDS)
    projected['settlement_summary'] = _project_object(raw.get('settlement_summary'), _ALLOWED_SUMMARY_FIELDS)
    member_outcomes = raw.get('member_outcomes')
    projected['member_outcomes'] = [_project_object(row, _ALLOWED_MEMBER_OUTCOME_FIELDS) for row in (member_outcomes if isinstance(member_outcomes, list) else [])[:20] if isinstance(row, dict)]
    return projected

def _build_tavern_relief_ai_user_prompt(context: dict) -> str:
    projected = _project_tavern_relief_ai_context(context)
    return '请核定以下酒馆补贴申请。所有动态事实都在这个 JSON 中：\n' + json.dumps(projected, ensure_ascii=False, separators=(',', ':'))

def _normalize_tavern_relief_ai_protocol_keys(parsed: object) -> tuple[object, bool]:
    """只恢复能唯一映射到白名单字段的键内 ASCII 空白漂移。"""
    if not isinstance(parsed, dict):
        return (parsed, False)
    aliases: dict[str, str] = {}
    for raw_key in parsed:
        if not isinstance(raw_key, str) or raw_key in _TAVERN_RELIEF_AI_RESULT_FIELDS:
            continue
        normalized_key = raw_key.translate(_ASCII_PROTOCOL_KEY_WHITESPACE_TRANSLATION)
        if normalized_key == raw_key or normalized_key not in _TAVERN_RELIEF_AI_RESULT_FIELDS or normalized_key in parsed:
            continue
        if normalized_key in aliases:
            return (parsed, False)
        aliases[normalized_key] = raw_key
    if not aliases:
        return (parsed, False)
    normalized = dict(parsed)
    for canonical_key, raw_key in aliases.items():
        normalized[canonical_key] = parsed[raw_key]
    return (normalized, True)

def _parse_tavern_relief_ai_result(parsed: object, *, max_target_per_person: float | None, max_applicant_bonus_target: float | None) -> dict | None:
    """兼容无歧义协议键漂移后，白名单投影并严格校验消费字段。"""
    parsed, _protocol_key_repair_applied = _normalize_tavern_relief_ai_protocol_keys(parsed)
    if not isinstance(parsed, dict):
        return None
    projected = {key: parsed.get(key) for key in _TAVERN_RELIEF_AI_RESULT_FIELDS}
    decision = projected['decision']
    if decision not in ('approve', 'reject'):
        return None
    amount, _amount_changed = normalize_ascii_finite_number(projected['target_amount_per_person'])
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    amount = round(float(amount), 2)
    if not math.isfinite(amount):
        return None
    applicant_bonus, _bonus_changed = normalize_ascii_finite_number(projected['applicant_bonus_target'])
    if isinstance(applicant_bonus, bool) or not isinstance(applicant_bonus, (int, float)):
        return None
    applicant_bonus = round(float(applicant_bonus), 2)
    if not math.isfinite(applicant_bonus):
        return None
    try:
        max_amount = round(float(max_target_per_person), 2) if max_target_per_person is not None else None
        max_applicant_bonus = round(float(max_applicant_bonus_target), 2) if max_applicant_bonus_target is not None else None
    except (TypeError, ValueError):
        return None
    if max_amount is not None and (not math.isfinite(max_amount) or max_amount <= 0):
        return None
    if max_applicant_bonus is not None and (not math.isfinite(max_applicant_bonus) or max_applicant_bonus < 0):
        return None
    summary = projected['summary']
    player_reply = projected['player_reply']
    if not isinstance(summary, str) or not 10 <= len(summary.strip()) <= 1000:
        return None
    if not isinstance(player_reply, str) or not 10 <= len(player_reply.strip()) <= 500:
        return None
    if decision == 'approve':
        applicant_bonus_invalid = applicant_bonus != 0 if max_applicant_bonus == 0 else applicant_bonus < 0 if max_applicant_bonus is None else applicant_bonus <= 0 or applicant_bonus > max_applicant_bonus
        if amount <= 0 or (max_amount is not None and amount > max_amount) or applicant_bonus_invalid:
            return None
    elif amount != 0 or applicant_bonus != 0:
        return None
    return {'decision': decision, 'target_amount_per_person': amount, 'applicant_bonus_target': applicant_bonus, 'summary': summary.strip(), 'player_reply': player_reply.strip()}

@with_player_name_aliases
def call_tavern_relief_ai(context: dict, *, expedition_log_id: int, adventurer_id: int, appeal_id: int | None=None, max_target_per_person: float | None, max_applicant_bonus_target: float | None) -> dict | None:
    """执行一次思考模式核定；已知调用或合同失败返回 None，不机械代替裁决。"""
    try:
        route = resolve_s1_ai_profile_snapshot('tavern_relief')
    except AIProfileAssignmentError as exc:
        logger.warning('酒馆补贴申请 AI 用途路由不可用 log=%s: %s', expedition_log_id, exc)
        return None
    api_base = route.api_base
    configured_model = route.model
    try:
        timeout = int(current_app.config.get('ADVENTURER_S1_TAVERN_RELIEF_AI_TIMEOUT_SECONDS', 600) or 600)
    except (TypeError, ValueError):
        timeout = 600
    timeout = max(30, min(timeout, 600))
    response = None
    usage = None
    actual_model = configured_model
    is_success = False
    finish_reason = None
    error_code = None
    validation_error_code = None
    failed_response_excerpt = None
    repair_applied = False
    request_metadata: dict[str, str] = {}
    runtime_metadata: dict[str, str] = request_metadata
    request_exception: BaseException | None = None

    def validate_tavern_relief_response(decoded) -> S1AiResponseContractResult:
        json_result = decoded.json_result
        if json_result.data is None:
            return S1AiResponseContractResult.reject(json_result.error_code or 'response_invalid', failure_stage='response_parse')
        normalized_input, _repair_applied = _normalize_tavern_relief_ai_protocol_keys(json_result.data)
        normalized = _parse_tavern_relief_ai_result(normalized_input, max_target_per_person=max_target_per_person, max_applicant_bonus_target=max_applicant_bonus_target)
        if normalized is None:
            return S1AiResponseContractResult.reject('contract_invalid')
        return S1AiResponseContractResult.accept(normalized)
    try:
        response = post_s1_ai_request(f'{api_base}/chat/completions', feature='tavern_relief', resolved_profile=route, json_object_mode_if_supported=True, request_metadata_out=request_metadata, headers={'Content-Type': 'application/json'}, json={'model': configured_model, 'messages': [{'role': 'system', 'content': _TAVERN_RELIEF_AI_SYSTEM_PROMPT}, {'role': 'user', 'content': _build_tavern_relief_ai_user_prompt(context)}], 'thinking': {'type': 'enabled'}, 'temperature': 0.1}, timeout=timeout, response_validator=validate_tavern_relief_response)
        decoded = parse_s1_ai_http_json_response(response)
        payload = decoded.http_result.response_json or {}
        usage = decoded.http_result.usage
        runtime_metadata = enrich_s1_ai_response_metadata(response, payload)
        actual_model = runtime_metadata.get('model') or configured_model
        json_result = decoded.json_result
        finish_reason = json_result.finish_reason
        repair_applied = json_result.repair_applied
        failed_response_excerpt = json_result.raw_excerpt
        if json_result.data is None:
            error_code = json_result.error_code or 'response_invalid'
            validation_error_code = f'response.{error_code}'
            logger.warning('酒馆补贴申请 AI 响应无效 log=%s code=%s detail=%s', expedition_log_id, error_code, json_result.error_detail)
            return None
        normalized_input, protocol_key_repair_applied = _normalize_tavern_relief_ai_protocol_keys(json_result.data)
        normalized = _parse_tavern_relief_ai_result(normalized_input, max_target_per_person=max_target_per_person, max_applicant_bonus_target=max_applicant_bonus_target)
        repair_applied = repair_applied or protocol_key_repair_applied
        if normalized is None:
            error_code = 'contract_invalid'
            validation_error_code = 'result.contract_invalid'
            logger.warning('酒馆补贴申请 AI 合同无效 log=%s', expedition_log_id)
            return None
        is_success = True
        return {**normalized, 'raw_response': json_result.content, 'model': actual_model}
    except requests.RequestException as exc:
        request_exception = exc
        error_code = 'http_error'
        logger.warning('酒馆补贴申请 AI HTTP 失败 log=%s: %s', expedition_log_id, exc)
        failed_response_excerpt = build_s1_ai_raw_excerpt(getattr(response, 'text', '') if response is not None else '')
        return None
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        request_exception = exc
        error_code = error_code or 'response_invalid'
        logger.warning('酒馆补贴申请 AI 响应无效 log=%s: %s', expedition_log_id, exc)
        failed_response_excerpt = build_s1_ai_raw_excerpt(getattr(response, 'text', '') if response is not None else '')
        return None
    finally:
        if response is not None:
            log_s1_ai_response_outcome(response, feature='tavern_relief', result_status='success' if is_success else 'failed', finish_reason=finish_reason, repair_applied=repair_applied, error_code=error_code, attempt_number=1)
        record_token_usage(call_type='tavern_relief_ai', usage=usage, model=actual_model, expedition_log_id=expedition_log_id, appeal_id=appeal_id, adventurer_id=adventurer_id, attempt_number=1, is_success=is_success, failure_stage=error_code if not is_success else None, validation_error_code=validation_error_code if not is_success else None, finish_reason=finish_reason, json_object_mode=_runtime_metadata_bool(runtime_metadata.get('json_object_mode')), provider_request_id=runtime_metadata.get('request_id'), repair_applied=repair_applied, failed_response_excerpt=failed_response_excerpt, record_without_usage=not is_success, request_metadata=request_metadata, request_exception=request_exception)
