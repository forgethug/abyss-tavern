"""S1 AI 调用 token 用量统计服务。

提供 record_token_usage 记录每次 AI 调用的 token 消耗，
以及多种维度的聚合查询供管理员查看。
"""
from runtime_core.time import utc_now as _portable_utc_now
from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
import json

from flask import current_app
from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.orm import undefer

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1TokenUsage
from plugins.adventurer_s1.services.s1_ai_input_budget import S1AiInputBudgetError
from plugins.adventurer_s1.services.s1_ai_failure_evidence import (
    S1_AI_FAILURE_EVIDENCE_KEY,
    get_s1_ai_failure_evidence,
)
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY,
    S1_AI_REASONING_FIRST_SENTENCE_LIMIT,
    S1_AI_ROUTE_METADATA_KEY,
    S1_AI_VISIBLE_REASONING_CHARS_KEY,
    merge_s1_ai_attempt_receipts_into_usage,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_exact_boolean,
)
from utils.cache_manager import CacheManager
from utils.time_utils import to_utc_iso

# 申诉 process_ai 入队 / 执行原因（写入 token 行 invoke_reason + 结构化日志）
APPEAL_INVOKE_REASON_LABELS: dict[str, str] = {
    'initial': '玩家提交首次处理',
    'requeue_stuck': '定时补投卡住任务',
    'admin_retry': '管理员手动重试 AI',
    'duplicate_concurrent': '并发重复执行',
    'unknown': '未知来源',
}

_appeal_invoke_reason: ContextVar[str | None] = ContextVar('s1_appeal_invoke_reason', default=None)


def set_appeal_invoke_reason(reason: str | None):
    """在 process_ai 周期内设置当前申诉 AI 调用原因，供 record_token_usage 写入。"""
    return _appeal_invoke_reason.set(reason)


def reset_appeal_invoke_reason(token) -> None:
    _appeal_invoke_reason.reset(token)


def get_appeal_invoke_reason() -> str | None:
    return _appeal_invoke_reason.get()


def get_invoke_reason_label(reason: str | None) -> str:
    if not reason:
        return ''
    return APPEAL_INVOKE_REASON_LABELS.get(reason, reason)

S1_CALL_TYPE_LABELS: dict[str, str] = {
    'expedition_ai': '探险主剧情生成',
    'expedition_objective_outline_ai': '探险客观局势大纲',
    'expedition_resolution_ai': '探险高危主剧情因果底稿',
    'expedition_death_resource_review_ai': '探险生命资源一致性复核',
    'expedition_settlement_guard_ai': '探险结算护栏 AI',
    'healing_house_ai': '治疗所 AI',
    'blacksmith_ai': '铁匠铺 AI',
    'tailor_workshop_ai': '绮罗裁坊 AI',
    'alchemy_workshop_ai': '炼金工房 AI',
    'ambient_npc_gift_ai': '环境伙伴赠礼反应 AI',
    'item_assembly_workshop_ai': '构装工坊 AI',
    'item_assembly_authority_review_ai': '构装资格判定 AI',
    'item_assembly_workshop_review_ai': '构装工坊结果审核 AI',
    'ember_kitchen_ai': '余烬灶坊烹饪 AI',
    'skill_training_ai': '技能书阁研修 AI',
    'skill_training_content_review_ai': '技能书阁研修内容复核 AI',
    'data_appeal_ai': '数据申诉 AI',
    'data_appeal_fact_sheet_ai': '数据申诉副本事实卡 AI',
    'data_appeal_review_ai': '数据申诉二次复核 AI',
    'data_appeal_review_retry_ai': '数据申诉二次复核结构重试 AI',
    'tavern_relief_ai': '酒馆补贴申请 AI 核定',
    'expedition_audit_ai': '探险结算审核 AI（历史）',
    'expedition_observer_review_ai': '探险剧情审核',
    'expedition_observer_verdict_ai': '探险审核输出轻量裁决 AI',
    'expedition_observer_resource_ecology_ai': '探险资源生态审核 AI',
    'expedition_observer_decisive_repair_ai': '探险决定性能力回执修复 AI',
    'expedition_observer_countermeasure_repair_ai': '探险知识应用回执复核 AI',
    'expedition_observer_consumed_asset_repair_ai': '探险整件资产连续性回执修复 AI',
    'expedition_content_safety_review_ai': '探险存量内容处置复核 AI',
    'fate_historical_review_ai': '历史命途删除复核 AI',
    'companion_historical_review_ai': '历史伙伴连续性复核 AI',
    'trait_recovery_ai': '特质恢复 AI',
    'trait_compress_ai': '特质压缩 AI',
    'trait_compress_review_ai': '特质压缩语义审核 AI',
    'trait_context_route_ai': '短特质机制与分类 AI',
    'expedition_countermeasure_context_routing_ai': '探险反制知识上下文路由',
    'expedition_countermeasure_review_ai': '长期主体遭遇与来源审核',
    'expedition_countermeasure_experience_ai': '长期主体应对设计',
    'expedition_countermeasure_archive_ai': '长期主体经验归档',
    'description_compress_ai': '资产描述压缩 AI',
    'asset_description_update_ai': '既有资产描述修改 AI',
    'asset_description_update_review_ai': '既有资产描述修改复核 AI',
    'description_compress_review_ai': '资产描述压缩语义审核 AI',
    'description_summary_final_audit_ai': '描述摘要终稿忠实度审计 AI',
    'description_summary_sample_audit_ai': '描述摘要逐条激活审核 AI',
    'trait_context_route_review_ai': '特质上下文路由独立复审 AI',
    'desc_backfill_ai': '描述补全 AI',
    'commission_generation': '悬赏委托生成 AI',
    'tavern_activity_ai': '酒馆活动剧情 AI',
    'tavern_activity_review_ai': '酒馆活动剧情审核 AI',
    'clue_bureau_boss_generation': '线索追查 Boss 生成 AI',
    'world_outline_maintenance_ai': '世界线维护 AI',
    'campaign_continuation_plan_ai': '战役阶段衔接规划 AI',
    'world_encounter_preparation_ai': '后续实体准备 AI',
    'world_encounter_preparation_review_ai': '后续实体准备复核 AI',
    'campaign_continuation_review_ai': '战役阶段衔接审核 AI',
    'campaign_evolution_plan_ai': '战役时间演进 AI',
    'campaign_evolution_review_ai': '战役时间演进审核 AI',
    'world_actor_profile_backfill_ai': '世界角色画像补全 AI',
    'world_intel_generation_ai': '世界情报生成 AI',
    # 历史/测试遗留 call_type
    'ai_healing_house': '治疗所 AI（旧标识）',
    'ai_blacksmith': '铁匠铺 AI（旧标识）',
}

# 数据申诉相关 call_type（普通申诉链 + 酒馆补贴申请压缩结算核定）
APPEAL_CALL_TYPES: tuple[str, ...] = (
    'data_appeal_ai',
    'data_appeal_review_ai',
    'data_appeal_review_retry_ai',
    'tavern_relief_ai',
)

# 历史 token 行补 appeal_id 时：token 时间落在工单处理期内，取最近创建的工单
APPEAL_TOKEN_BACKFILL_GRACE_MINUTES = 15

# 管理端按冒险者申诉 token 统计（LATERAL 较重，60s 缓存 + 单次拉行 Python 聚合）
_APPEAL_STATS_BY_ADV_CACHE_TTL = 60
_appeal_stats_by_adv_cache = CacheManager(
    ttl_seconds=_APPEAL_STATS_BY_ADV_CACHE_TTL, max_size=32,
)

TOKEN_USAGE_REPORTING_UTC_OFFSET = timedelta(hours=8)


def get_call_type_label(call_type: str | None) -> str:
    if not call_type:
        return '未知'
    return S1_CALL_TYPE_LABELS.get(call_type, call_type)


def list_call_types() -> list[dict]:
    """返回全部已注册的 call_type 及标签，供管理端筛选下拉使用。"""
    return [
        {'call_type': k, 'label': v}
        for k, v in S1_CALL_TYPE_LABELS.items()
    ]


def appeal_counts_toward_daily_quota(appeal_model):
    """申诉每日额度的统一 SQL 判定。

    处理中工单先占用额度，避免玩家在 AI 返回前并发超额提交；只有已经终结为
    rejected、主申诉 AI 明确 failed，且该工单没有任何正 token 用量的技术失败
    才返还额度。业务驳回、解析/校验失败但已经产生 token 的调用仍正常计次。
    """
    positive_usage_exists = (
        select(AdventurerS1TokenUsage.id)
        .where(
            AdventurerS1TokenUsage.appeal_id == appeal_model.id,
            AdventurerS1TokenUsage.total_tokens > 0,
        )
        .correlate(appeal_model)
        .exists()
    )
    return or_(
        appeal_model.status != 'rejected',
        appeal_model.ai_status != 'failed',
        positive_usage_exists,
    )


_POSITIVE_TOKEN_USAGE_FIELDS: tuple[str, ...] = (
    'total_tokens',
    'prompt_tokens',
    'completion_tokens',
    'prompt_cache_hit_tokens',
    'prompt_cache_miss_tokens',
)


def _is_positive_token_count(value: object) -> bool:
    if type(value) is int:
        return value > 0
    if isinstance(value, str):
        stripped = value.strip()
        return bool(
            stripped
            and stripped.isascii()
            and stripped.isdecimal()
            and int(stripped) > 0
        )
    return False


def _usage_payload_has_positive_tokens(usage: object) -> bool:
    if not isinstance(usage, dict):
        return False
    if any(
        _is_positive_token_count(usage.get(field))
        for field in _POSITIVE_TOKEN_USAGE_FIELDS
    ):
        return True
    prompt_details = usage.get('prompt_tokens_details')
    if (
        isinstance(prompt_details, dict)
        and _is_positive_token_count(prompt_details.get('cached_tokens'))
    ):
        return True
    return False


def s1_ai_usage_has_positive_tokens(
    usage: dict | None,
    *,
    request_metadata: dict | None = None,
    request_exception: BaseException | None = None,
) -> bool:
    """Whether authoritative provider accounting proves any positive token use.

    The unified gateway can fail after one or more physical provider attempts.
    Those attempts are carried as receipts on usage, request metadata, or the
    terminal exception, so retry/CD accounting must inspect the same merged
    evidence that ``record_token_usage`` persists.
    """
    merged_usage = merge_s1_ai_attempt_receipts_into_usage(
        usage,
        request_metadata=request_metadata,
        request_exception=request_exception,
    )
    if _usage_payload_has_positive_tokens(merged_usage):
        return True
    raw_receipts = (
        merged_usage.get(S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY)
        if isinstance(merged_usage, dict)
        else None
    )
    return bool(
        isinstance(raw_receipts, list)
        and any(
            _usage_payload_has_positive_tokens(receipt.get('usage'))
            for receipt in raw_receipts[:12]
            if isinstance(receipt, dict)
        )
    )


def record_token_usage(
    call_type: str,
    usage: dict | None,
    model: str | None = None,
    party_id: int | None = None,
    expedition_log_id: int | None = None,
    appeal_id: int | None = None,
    adventurer_id: int | None = None,
    correlation_id: str | None = None,
    attempt_number: int | None = None,
    is_success: bool = True,
    failure_stage: str | None = None,
    validation_error_code: str | None = None,
    finish_reason: str | None = None,
    json_object_mode: bool | str | None = None,
    provider_request_id: str | None = None,
    repair_applied: bool = False,
    reasoning_first_sentence: str | None = None,
    failed_response_excerpt: str | None = None,
    record_without_usage: bool = False,
    profile_id: str | None = None,
    provider: str | None = None,
    request_metadata: dict | None = None,
    request_exception: BaseException | None = None,
) -> None:
    """从 API 响应的 usage 字段提取 token 统计并写入数据库。

    本函数自行管理事务（add + commit），调用方无需包裹事务。
    写入失败仅 warning 日志，不影响主流程。
    统一网关附带的物理尝试回执会逐条写为失败行，不合并到最终响应。
    terminal_current 回执已经代表最终响应，同一份 usage 不再另写业务层统计行。

    Args:
        call_type: 调用类型标识，如 'expedition_ai'
        usage: API 响应中的 usage 对象，包含 prompt_tokens / completion_tokens / prompt_tokens_details 等
        model: 实际使用的模型名
        profile_id: 实际路由配置 ID（不含密钥与 URL）
        provider: 实际 provider 标识
        party_id: 关联的小队 ID（可选）
        expedition_log_id: 关联的探险日志 ID（可选）
        appeal_id: 关联的数据申诉 ID（可选）
        adventurer_id: 关联的冒险者 ID（可选，申诉统计用）
        correlation_id: 跨业务表稳定关联键（可选）
        attempt_number: 第几次尝试（重试场景）
        is_success: 本次 AI 调用是否成功返回了可用结果
        failure_stage: 稳定失败阶段；成功调用留空
        validation_error_code: parser/validator 的稳定错误码
        finish_reason: provider 返回的 finish_reason
        json_object_mode: 最终路由是否实际启用 JSON object mode
        provider_request_id: provider 返回的非敏感请求 ID
        repair_applied: 本次响应是否经过语法、字段名兼容或同阶段结构修复
        reasoning_first_sentence: 主探险 reasoning_content 的有界首句，仅管理员查账
        failed_response_excerpt: 失败响应的有界摘录，仅管理员查账
        record_without_usage: provider 未返回 usage 时仍记录零 token 失败行
        request_metadata: 统一网关元数据，用于回收终止异常前的物理尝试回执
        request_exception: 统一网关或解码器异常，用于回收其物理尝试回执
    """
    # Budget errors originate locally at the shared gateway, before transport.
    # Preserve any earlier physical-attempt receipts under their original codes.
    budget_code = (request_exception.code
                   if isinstance(request_exception, S1AiInputBudgetError) else None)
    if not budget_code and isinstance(request_metadata, dict):
        if request_metadata.get('gateway_failure_stage') == 'input_budget':
            candidate = request_metadata.get('validation_error_code')
            if candidate in ('s1_ai_input_budget_exceeded', 's1_ai_input_budget_invalid'):
                budget_code = candidate
    if budget_code:
        is_success, record_without_usage = False, True
        failure_stage, validation_error_code = 'input_budget', budget_code
    usage = merge_s1_ai_attempt_receipts_into_usage(
        usage,
        request_metadata=request_metadata,
        request_exception=request_exception,
    )
    failure_evidence = get_s1_ai_failure_evidence(usage)
    retain_evidence = failure_evidence is not None and (
        is_success is not True or bool(failure_evidence.field_name_compatibility)
    )
    if failure_evidence is not None and any(
        detail.get('repairs') for detail in failure_evidence.field_name_compatibility
    ):
        repair_applied = True
    usage_route_metadata = (
        usage.get(S1_AI_ROUTE_METADATA_KEY)
        if isinstance(usage, dict)
        and isinstance(usage.get(S1_AI_ROUTE_METADATA_KEY), dict)
        else {}
    )
    request_route_metadata = (
        request_metadata if isinstance(request_metadata, dict) else {}
    )
    route_metadata = {
        **usage_route_metadata,
        **request_route_metadata,
    }
    if json_object_mode is None:
        json_object_mode = route_metadata.get('json_object_mode')
    profile_id = profile_id or route_metadata.get('profile_id')
    provider = provider or route_metadata.get('provider')
    attempt_receipts: list[dict] = []
    terminal_receipt_present = False
    if isinstance(usage, dict):
        raw_receipts = usage.get(S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY)
        if isinstance(raw_receipts, list):
            attempt_receipts = [
                dict(receipt)
                for receipt in raw_receipts[:12]
                if isinstance(receipt, dict)
            ]
        usage = {
            key: value
            for key, value in usage.items()
            if key not in {
                S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY,
                S1_AI_ROUTE_METADATA_KEY,
                S1_AI_FAILURE_EVIDENCE_KEY,
            }
        }

    for receipt in attempt_receipts:
        terminal_receipt_present = (
            terminal_receipt_present
            or receipt.get('terminal_current') is True
        )
        receipt_usage = receipt.get('usage')
        normalized_receipt_usage = (
            {
                key: value
                for key, value in receipt_usage.items()
                if key != S1_AI_PHYSICAL_ATTEMPT_RECEIPTS_KEY
            }
            if isinstance(receipt_usage, dict)
            else None
        )
        record_token_usage(
            call_type=call_type,
            usage=normalized_receipt_usage,
            model=str(receipt.get('model') or model or '').strip() or None,
            profile_id=(
                str(receipt.get('profile_id') or '').strip() or profile_id
            ),
            provider=(
                str(receipt.get('provider') or '').strip() or provider
            ),
            party_id=party_id,
            expedition_log_id=expedition_log_id,
            appeal_id=appeal_id,
            adventurer_id=adventurer_id,
            correlation_id=correlation_id,
            attempt_number=attempt_number,
            is_success=False,
            failure_stage=str(
                receipt.get('failure_stage') or 'provider_failover'
            ),
            validation_error_code=str(
                receipt.get('validation_error_code')
                or 'provider_attempt_failed'
            ),
            finish_reason=(
                str(receipt.get('finish_reason'))
                if receipt.get('finish_reason') is not None
                else None
            ),
            json_object_mode=(
                receipt.get('json_object_mode')
                if receipt.get('json_object_mode') is not None
                else json_object_mode
            ),
            provider_request_id=(
                str(receipt.get('provider_request_id'))
                if receipt.get('provider_request_id') is not None
                else None
            ),
            repair_applied=False,
            failed_response_excerpt=(
                str(receipt.get('failed_response_excerpt'))
                if receipt.get('failed_response_excerpt') is not None
                else None
            ),
            record_without_usage=True,
            request_metadata=receipt,
        )

    if terminal_receipt_present:
        # 最终物理响应已由回执记录；业务层再次传回的 usage 仍属于同一次请求，
        # 无论是否含 token 都不能再记一行。此前失败与后续成功的独立尝试照常分开。
        return

    if not usage or not isinstance(usage, dict):
        if not record_without_usage and not retain_evidence:
            return
        usage = {}

    def _bounded_text(value: object, limit: int) -> str | None:
        if value is None:
            return None
        text_value = str(value).strip()
        return text_value[:limit] if text_value else None

    def _token_count(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    def _optional_non_negative_int(value) -> int | None:
        parsed = None
        if type(value) is int:
            parsed = value
        elif isinstance(value, str):
            stripped = value.strip()
            if stripped and stripped.isascii() and stripped.isdecimal():
                parsed = int(stripped)
        if parsed is None or not 0 <= parsed <= 2_147_483_647:
            return None
        return parsed

    prompt_tokens_total = _token_count(usage.get('prompt_tokens'))
    completion_tokens = _token_count(usage.get('completion_tokens'))
    total_tokens = _token_count(usage.get('total_tokens'))
    visible_reasoning_chars = _optional_non_negative_int(
        usage.get(S1_AI_VISIBLE_REASONING_CHARS_KEY),
    )

    # DeepSeek completion_tokens 包含推理和最终内容；只有 provider 明确给出
    # 合法 breakdown 时才拆分。旧记录或缺失/异常字段保持 NULL，避免把未知
    # 推理量误记为 0。
    completion_tokens_details = usage.get('completion_tokens_details', {}) or {}
    completion_reasoning_tokens = None
    if isinstance(completion_tokens_details, dict):
        raw_reasoning_tokens = completion_tokens_details.get('reasoning_tokens')
        parsed_reasoning_tokens = None
        if type(raw_reasoning_tokens) is int:
            parsed_reasoning_tokens = raw_reasoning_tokens
        elif isinstance(raw_reasoning_tokens, str):
            stripped_reasoning_tokens = raw_reasoning_tokens.strip()
            if (
                stripped_reasoning_tokens
                and stripped_reasoning_tokens.isascii()
                and stripped_reasoning_tokens.isdecimal()
            ):
                parsed_reasoning_tokens = int(stripped_reasoning_tokens)
        if (
            parsed_reasoning_tokens is not None
            and 0 <= parsed_reasoning_tokens <= completion_tokens
        ):
            completion_reasoning_tokens = parsed_reasoning_tokens

    # DeepSeek 文档字段优先；兼容既有 OpenAI-style prompt_tokens_details。
    prompt_tokens_details = usage.get('prompt_tokens_details', {}) or {}
    if not isinstance(prompt_tokens_details, dict):
        prompt_tokens_details = {}
    documented_cache_hit = usage.get('prompt_cache_hit_tokens')
    documented_cache_miss = usage.get('prompt_cache_miss_tokens')
    cache_hit = _token_count(
        documented_cache_hit
        if documented_cache_hit is not None
        else prompt_tokens_details.get('cached_tokens')
    )
    cache_miss = _token_count(
        documented_cache_miss
        if documented_cache_miss is not None
        else max(0, prompt_tokens_total - cache_hit)
    )
    if prompt_tokens_total <= 0 and cache_hit + cache_miss > 0:
        prompt_tokens_total = cache_hit + cache_miss

    # total_tokens 兜底
    if total_tokens <= 0:
        total_tokens = prompt_tokens_total + completion_tokens

    if appeal_id and not adventurer_id:
        from plugins.adventurer_s1.models import AdventurerS1DataAppeal
        row = (
            AdventurerS1DataAppeal.query.with_entities(AdventurerS1DataAppeal.adventurer_id)
            .filter_by(id=int(appeal_id))
            .first()
        )
        if row and row.adventurer_id:
            adventurer_id = int(row.adventurer_id)

    invoke_reason = None
    if call_type in APPEAL_CALL_TYPES:
        invoke_reason = get_appeal_invoke_reason()

    normalized_json_object_mode, _ = normalize_exact_boolean(json_object_mode)

    try:
        # 独立写入：与调用方事务/长 HTTP 等待解耦，避免脏 session 导致 commit 失败
        db_manager.remove()
        failure_detail = None
        if retain_evidence:
            try:
                if request_exception is not None:
                    failure_evidence.capture_exception(request_exception)
                failure_detail = failure_evidence.serialize(
                    failure_stage=failure_stage,
                    validation_error_code=validation_error_code,
                    error_excerpt=failed_response_excerpt,
                )
            except Exception as exc:
                current_app.logger.warning('S1 AI 失败全文序列化失败 type=%s', type(exc).__name__)
        record = AdventurerS1TokenUsage(
            call_type=call_type,
            model=model,
            profile_id=_bounded_text(profile_id, 100),
            provider=_bounded_text(provider, 80),
            party_id=party_id,
            expedition_log_id=expedition_log_id,
            appeal_id=appeal_id,
            adventurer_id=adventurer_id,
            correlation_id=_bounded_text(correlation_id, 64),
            prompt_tokens_total=prompt_tokens_total,
            prompt_cache_hit_tokens=cache_hit,
            prompt_cache_miss_tokens=cache_miss,
            completion_tokens=completion_tokens,
            completion_reasoning_tokens=completion_reasoning_tokens,
            visible_reasoning_chars=visible_reasoning_chars,
            total_tokens=total_tokens,
            attempt_number=attempt_number,
            is_success=is_success,
            failure_stage=_bounded_text(failure_stage, 80),
            validation_error_code=_bounded_text(validation_error_code, 160),
            finish_reason=_bounded_text(finish_reason, 40),
            json_object_mode=(
                normalized_json_object_mode
                if type(normalized_json_object_mode) is bool
                else None
            ),
            provider_request_id=_bounded_text(provider_request_id, 160),
            api_key_slot=_bounded_text(
                route_metadata.get('api_key_slot'),
                24,
            ),
            api_endpoint_slot=_bounded_text(
                route_metadata.get('api_endpoint_slot'),
                24,
            ),
            endpoint_host=_bounded_text(
                route_metadata.get('endpoint_host'),
                253,
            ),
            transport_attempt=_optional_non_negative_int(
                route_metadata.get('transport_attempt')
            ),
            failover_reason=_bounded_text(
                route_metadata.get('failover_reason'),
                160,
            ),
            failover_scope=_bounded_text(
                route_metadata.get('failover_scope'),
                40,
            ),
            failover_attempt_count=_optional_non_negative_int(
                route_metadata.get('failover_attempt_count')
            ),
            repair_applied=repair_applied is True,
            reasoning_first_sentence=(
                _bounded_text(
                    reasoning_first_sentence,
                    S1_AI_REASONING_FIRST_SENTENCE_LIMIT,
                )
                if call_type == 'expedition_ai'
                else None
            ),
            failed_response_excerpt=(
                _bounded_text(failed_response_excerpt, 2000)
                if is_success is not True
                else None
            ),
            invoke_reason=invoke_reason,
            failure_detail=failure_detail,
            has_failure_detail=failure_detail is not None,
        )
        ok, err = db_manager.add(record, flush=False)
        if not ok:
            current_app.logger.warning('S1 token usage add 失败: %s', err)
            return
        ok, err = db_manager.commit()
        if not ok:
            current_app.logger.warning('S1 token usage commit 失败: %s', err)
    except Exception as e:
        current_app.logger.warning('S1 token usage 记录失败: %s', e)


def _summary_columns():
    success_case = case(
        (AdventurerS1TokenUsage.is_success == True, 1),
        else_=0,
    )
    split_known_case = case(
        (AdventurerS1TokenUsage.completion_reasoning_tokens.isnot(None), 1),
        else_=0,
    )
    visible_reasoning_known_case = case(
        (AdventurerS1TokenUsage.visible_reasoning_chars.isnot(None), 1),
        else_=0,
    )
    content_tokens_case = case(
        (
            AdventurerS1TokenUsage.completion_reasoning_tokens.isnot(None),
            AdventurerS1TokenUsage.completion_tokens
            - AdventurerS1TokenUsage.completion_reasoning_tokens,
        ),
        else_=0,
    )
    unattributed_tokens_case = case(
        (
            AdventurerS1TokenUsage.completion_reasoning_tokens.is_(None),
            AdventurerS1TokenUsage.completion_tokens,
        ),
        else_=0,
    )
    return [
        func.count(AdventurerS1TokenUsage.id).label('total_calls'),
        func.sum(success_case).label('success_calls'),
        func.coalesce(func.sum(AdventurerS1TokenUsage.prompt_tokens_total), 0).label('prompt_tokens_total'),
        func.coalesce(func.sum(AdventurerS1TokenUsage.prompt_cache_hit_tokens), 0).label('prompt_cache_hit_tokens'),
        func.coalesce(func.sum(AdventurerS1TokenUsage.prompt_cache_miss_tokens), 0).label('prompt_cache_miss_tokens'),
        func.coalesce(func.sum(AdventurerS1TokenUsage.completion_tokens), 0).label('completion_tokens'),
        func.coalesce(func.sum(AdventurerS1TokenUsage.completion_reasoning_tokens), 0).label('completion_reasoning_tokens'),
        func.coalesce(func.sum(AdventurerS1TokenUsage.visible_reasoning_chars), 0).label('visible_reasoning_chars'),
        func.coalesce(func.sum(visible_reasoning_known_case), 0).label('visible_reasoning_known_calls'),
        func.coalesce(func.sum(content_tokens_case), 0).label('completion_content_tokens'),
        func.coalesce(func.sum(unattributed_tokens_case), 0).label('completion_unattributed_tokens'),
        func.coalesce(func.sum(split_known_case), 0).label('completion_split_known_calls'),
        func.coalesce(func.sum(AdventurerS1TokenUsage.total_tokens), 0).label('total_tokens'),
    ]


def _summary_from_row(row) -> dict:
    total_calls = int(row.total_calls or 0)
    success_calls = int(row.success_calls or 0)
    prompt_total = int(row.prompt_tokens_total or 0)
    cache_hit = int(row.prompt_cache_hit_tokens or 0)
    cache_miss = int(row.prompt_cache_miss_tokens or 0)
    comp_total = int(row.completion_tokens or 0)
    reasoning_total = int(row.completion_reasoning_tokens or 0)
    visible_reasoning_chars = int(row.visible_reasoning_chars or 0)
    visible_reasoning_known_calls = int(row.visible_reasoning_known_calls or 0)
    content_total = int(row.completion_content_tokens or 0)
    unattributed_total = int(row.completion_unattributed_tokens or 0)
    split_known_calls = int(row.completion_split_known_calls or 0)
    grand_total = int(row.total_tokens or 0)
    hit_rate = cache_hit / prompt_total if prompt_total > 0 else 0.0
    return {
        'total_calls': total_calls,
        'success_calls': success_calls,
        'failed_calls': total_calls - success_calls,
        'prompt_tokens_total': prompt_total,
        'prompt_cache_hit_tokens': cache_hit,
        'prompt_cache_miss_tokens': cache_miss,
        'completion_tokens': comp_total,
        'completion_reasoning_tokens': reasoning_total,
        'visible_reasoning_chars': visible_reasoning_chars,
        'visible_reasoning_known_calls': visible_reasoning_known_calls,
        'visible_reasoning_unknown_calls': total_calls - visible_reasoning_known_calls,
        'completion_content_tokens': content_total,
        'completion_unattributed_tokens': unattributed_total,
        'completion_split_known_calls': split_known_calls,
        'completion_split_unknown_calls': total_calls - split_known_calls,
        'total_tokens': grand_total,
        'cache_hit_rate': round(hit_rate, 4),
    }


def _day_key(value) -> str:
    return value.isoformat() if hasattr(value, 'isoformat') else str(value)


def _empty_stats():
    return {
        'metric_scope': 'physical_attempt',
        'total_calls': 0, 'success_calls': 0, 'failed_calls': 0,
        'prompt_tokens_total': 0, 'prompt_cache_hit_tokens': 0,
        'prompt_cache_miss_tokens': 0, 'completion_tokens': 0,
        'completion_reasoning_tokens': 0, 'completion_content_tokens': 0,
        'visible_reasoning_chars': 0,
        'visible_reasoning_known_calls': 0, 'visible_reasoning_unknown_calls': 0,
        'completion_unattributed_tokens': 0,
        'completion_split_known_calls': 0, 'completion_split_unknown_calls': 0,
        'total_tokens': 0, 'cache_hit_rate': 0.0,
        'daily': [],
    }


def _resolve_usage_since(
    days: int | None,
    start_at: datetime | None,
) -> datetime | None:
    """返回 UTC naive 统计起点；精确时间优先于相对天数。"""
    if start_at is not None:
        if start_at.tzinfo is not None:
            return start_at.astimezone(timezone.utc).replace(tzinfo=None)
        return start_at
    if days is None:
        return None
    return _portable_utc_now() - timedelta(days=days)


def _utc8_day_expr(column):
    """按 UTC+8 日界线生成可分组日期，兼容生产 PostgreSQL 与测试 SQLite。"""
    from extensions import db

    if db.session.get_bind().dialect.name == 'sqlite':
        return func.date(column, '+8 hours')
    return func.date(column + text("INTERVAL '8 hours'"))


def _utc8_date_conditions(column, value: str) -> tuple:
    """把 UTC+8 单日转换为可走 created_at 索引的 UTC 半开区间。"""
    try:
        local_start = datetime.strptime(value, '%Y-%m-%d')
    except (TypeError, ValueError):
        return (func.date(column) == value,)
    utc_start = local_start - TOKEN_USAGE_REPORTING_UTC_OFFSET
    return (
        column >= utc_start,
        column < utc_start + timedelta(days=1),
    )


def get_stats(
    days: int = 7,
    call_type: str | None = None,
    start_at: datetime | None = None,
) -> dict:
    """聚合统计指定起点至今的 token 用量，默认最近 N 天。"""
    since = _resolve_usage_since(days, start_at)

    query = AdventurerS1TokenUsage.query
    if since is not None:
        query = query.filter(AdventurerS1TokenUsage.created_at >= since)
    if call_type:
        query = query.filter_by(call_type=call_type)

    summary = query.with_entities(*_summary_columns()).first()
    if not summary or int(summary.total_calls or 0) == 0:
        return _empty_stats()

    result = _summary_from_row(summary)
    result['metric_scope'] = 'physical_attempt'

    day_expr = _utc8_day_expr(AdventurerS1TokenUsage.created_at)
    daily_rows = (
        query.with_entities(day_expr.label('day'), *_summary_columns())
        .group_by(day_expr)
        .order_by(day_expr)
        .all()
    )
    result['daily'] = [
        {
            'date': _day_key(r.day),
            'calls': int(r.total_calls or 0),
            'success_calls': int(r.success_calls or 0),
            'failed_calls': int(r.total_calls or 0) - int(r.success_calls or 0),
            'prompt_tokens_total': int(r.prompt_tokens_total or 0),
            'prompt_cache_hit_tokens': int(r.prompt_cache_hit_tokens or 0),
            'prompt_cache_miss_tokens': int(r.prompt_cache_miss_tokens or 0),
            'completion_tokens': int(r.completion_tokens or 0),
            'completion_reasoning_tokens': int(r.completion_reasoning_tokens or 0),
            'visible_reasoning_chars': int(r.visible_reasoning_chars or 0),
            'visible_reasoning_known_calls': int(r.visible_reasoning_known_calls or 0),
            'visible_reasoning_unknown_calls': (
                int(r.total_calls or 0) - int(r.visible_reasoning_known_calls or 0)
            ),
            'completion_content_tokens': int(r.completion_content_tokens or 0),
            'completion_unattributed_tokens': int(r.completion_unattributed_tokens or 0),
            'completion_split_known_calls': int(r.completion_split_known_calls or 0),
            'completion_split_unknown_calls': (
                int(r.total_calls or 0) - int(r.completion_split_known_calls or 0)
            ),
            'total_tokens': int(r.total_tokens or 0),
        }
        for r in daily_rows
    ]
    return result


def _logical_chain_stats_by_call_type(query) -> dict[str, dict]:
    """只按明确业务关联键聚合；无关联旧行逐条保留。"""
    from extensions import db

    normalized_correlation = func.nullif(
        func.trim(AdventurerS1TokenUsage.correlation_id),
        '',
    )
    fallback_record_id = case(
        (
            normalized_correlation.is_(None),
            AdventurerS1TokenUsage.id,
        ),
        else_=None,
    )
    logical_chains = (
        query.with_entities(
            AdventurerS1TokenUsage.call_type.label('call_type'),
            normalized_correlation.label('correlation_id'),
            fallback_record_id.label('fallback_record_id'),
            func.max(case(
                (AdventurerS1TokenUsage.is_success.is_(True), 1),
                else_=0,
            )).label('logical_success'),
        )
        .group_by(
            AdventurerS1TokenUsage.call_type,
            normalized_correlation,
            fallback_record_id,
        )
        .subquery()
    )
    rows = (
        db.session.query(
            logical_chains.c.call_type,
            func.count(logical_chains.c.call_type).label(
                'logical_total_calls'
            ),
            func.coalesce(
                func.sum(logical_chains.c.logical_success),
                0,
            ).label('logical_success_calls'),
            func.coalesce(func.sum(case(
                (logical_chains.c.correlation_id.isnot(None), 1),
                else_=0,
            )), 0).label('logical_correlated_calls'),
            func.coalesce(func.sum(case(
                (logical_chains.c.correlation_id.is_(None), 1),
                else_=0,
            )), 0).label('logical_uncorrelated_calls'),
        )
        .group_by(logical_chains.c.call_type)
        .all()
    )
    result: dict[str, dict] = {}
    for row in rows:
        total_calls = int(row.logical_total_calls or 0)
        success_calls = int(row.logical_success_calls or 0)
        result[row.call_type] = {
            'logical_total_calls': total_calls,
            'logical_success_calls': success_calls,
            'logical_failed_calls': total_calls - success_calls,
            'logical_correlated_calls': int(
                row.logical_correlated_calls or 0
            ),
            'logical_uncorrelated_calls': int(
                row.logical_uncorrelated_calls or 0
            ),
        }
    return result


def get_stats_by_call_type(
    days: int = 7,
    start_at: datetime | None = None,
) -> list[dict]:
    """按 call_type 和实际模型分组统计指定起点至今的 token 用量。

    返回: [{'call_type': str, 'model': str | None, ...聚合字段..., 'daily': [...]}]
    """
    since = _resolve_usage_since(days, start_at)
    query = AdventurerS1TokenUsage.query
    if since is not None:
        query = query.filter(AdventurerS1TokenUsage.created_at >= since)
    summary_rows = (
        query.with_entities(
            AdventurerS1TokenUsage.call_type,
            AdventurerS1TokenUsage.model,
            func.max(AdventurerS1TokenUsage.created_at).label(
                'last_called_at'
            ),
            *_summary_columns(),
        )
        .group_by(AdventurerS1TokenUsage.call_type, AdventurerS1TokenUsage.model)
        .order_by(AdventurerS1TokenUsage.call_type, AdventurerS1TokenUsage.model)
        .all()
    )
    if not summary_rows:
        return []

    logical_by_call_type = _logical_chain_stats_by_call_type(query)

    day_expr = _utc8_day_expr(AdventurerS1TokenUsage.created_at)
    daily_rows = (
        query.with_entities(
            AdventurerS1TokenUsage.call_type,
            AdventurerS1TokenUsage.model,
            day_expr.label('day'),
            func.count(AdventurerS1TokenUsage.id).label('calls'),
            func.coalesce(func.sum(AdventurerS1TokenUsage.total_tokens), 0).label('total_tokens'),
            func.coalesce(func.sum(AdventurerS1TokenUsage.prompt_tokens_total), 0).label('prompt_tokens_total'),
            func.coalesce(func.sum(AdventurerS1TokenUsage.completion_tokens), 0).label('completion_tokens'),
            func.coalesce(func.sum(AdventurerS1TokenUsage.completion_reasoning_tokens), 0).label('completion_reasoning_tokens'),
            func.coalesce(func.sum(AdventurerS1TokenUsage.visible_reasoning_chars), 0).label('visible_reasoning_chars'),
            func.coalesce(func.sum(case(
                (AdventurerS1TokenUsage.visible_reasoning_chars.isnot(None), 1),
                else_=0,
            )), 0).label('visible_reasoning_known_calls'),
            func.coalesce(func.sum(case(
                (
                    AdventurerS1TokenUsage.completion_reasoning_tokens.isnot(None),
                    AdventurerS1TokenUsage.completion_tokens
                    - AdventurerS1TokenUsage.completion_reasoning_tokens,
                ),
                else_=0,
            )), 0).label('completion_content_tokens'),
            func.coalesce(func.sum(case(
                (
                    AdventurerS1TokenUsage.completion_reasoning_tokens.is_(None),
                    AdventurerS1TokenUsage.completion_tokens,
                ),
                else_=0,
            )), 0).label('completion_unattributed_tokens'),
            func.coalesce(func.sum(case(
                (AdventurerS1TokenUsage.completion_reasoning_tokens.isnot(None), 1),
                else_=0,
            )), 0).label('completion_split_known_calls'),
        )
        .group_by(AdventurerS1TokenUsage.call_type, AdventurerS1TokenUsage.model, day_expr)
        .order_by(AdventurerS1TokenUsage.call_type, AdventurerS1TokenUsage.model, day_expr)
        .all()
    )
    daily_by_type_model: dict[tuple[str, str | None], list] = {}
    for r in daily_rows:
        daily_by_type_model.setdefault((r.call_type, r.model), []).append({
            'date': _day_key(r.day),
            'calls': int(r.calls or 0),
            'total_tokens': int(r.total_tokens or 0),
            'prompt_tokens_total': int(r.prompt_tokens_total or 0),
            'completion_tokens': int(r.completion_tokens or 0),
            'completion_reasoning_tokens': int(r.completion_reasoning_tokens or 0),
            'visible_reasoning_chars': int(r.visible_reasoning_chars or 0),
            'visible_reasoning_known_calls': int(r.visible_reasoning_known_calls or 0),
            'visible_reasoning_unknown_calls': (
                int(r.calls or 0) - int(r.visible_reasoning_known_calls or 0)
            ),
            'completion_content_tokens': int(r.completion_content_tokens or 0),
            'completion_unattributed_tokens': int(r.completion_unattributed_tokens or 0),
            'completion_split_known_calls': int(r.completion_split_known_calls or 0),
            'completion_split_unknown_calls': (
                int(r.calls or 0) - int(r.completion_split_known_calls or 0)
            ),
        })

    result = []
    for row in summary_rows:
        ct = row.call_type
        entry = _summary_from_row(row)
        entry['call_type'] = ct
        entry['call_type_label'] = get_call_type_label(ct)
        entry['model'] = row.model
        entry['metric_scope'] = 'physical_attempt'
        entry['last_called_at'] = (
            to_utc_iso(row.last_called_at) if row.last_called_at else None
        )
        entry.update(logical_by_call_type.get(ct, {
            'logical_total_calls': 0,
            'logical_success_calls': 0,
            'logical_failed_calls': 0,
            'logical_correlated_calls': 0,
            'logical_uncorrelated_calls': 0,
        }))
        entry['daily'] = daily_by_type_model.get((ct, row.model), [])
        result.append(entry)

    return result


def get_stats_by_party(
    days: int = 7,
    call_type: str | None = None,
    start_at: datetime | None = None,
) -> list[dict]:
    """按 party_id 分组统计，附带小队和成员名称信息。

    返回: [{'party_id': int, 'party_name': str, 'members': str, ...聚合字段...}]
    """
    since = _resolve_usage_since(days, start_at)
    query = AdventurerS1TokenUsage.query
    if since is not None:
        query = query.filter(AdventurerS1TokenUsage.created_at >= since)
    if call_type:
        query = query.filter_by(call_type=call_type)

    summary_rows = (
        query.with_entities(AdventurerS1TokenUsage.party_id, *_summary_columns())
        .group_by(AdventurerS1TokenUsage.party_id)
        .order_by(AdventurerS1TokenUsage.party_id.is_(None), AdventurerS1TokenUsage.party_id)
        .all()
    )
    if not summary_rows:
        return []

    # 收集涉及的 party_id
    party_ids = [r.party_id for r in summary_rows if r.party_id]
    party_info = {}
    if party_ids:
        from plugins.adventurer_s1.models import AdventurerS1Party
        parties = AdventurerS1Party.query.filter(AdventurerS1Party.id.in_(party_ids)).all()
        for p in parties:
            party_info[p.id] = {
                'name': p.name or f'队伍#{p.id}',
                'members': ', '.join(m.name for m in p.members if m.name) if p.members else '',
            }

    result = []
    for row in summary_rows:
        pid = row.party_id
        entry = _summary_from_row(row)
        entry['party_id'] = pid
        info = party_info.get(pid, {})
        entry['party_name'] = info.get('name', f'队伍#{pid}' if pid else '无')
        entry['members'] = info.get('members', '')
        result.append(entry)

    return result


def get_stats_by_expedition(
    days: int = 7,
    call_type: str | None = None,
    start_at: datetime | None = None,
) -> list[dict]:
    """按 expedition_log_id 分组统计。

    返回: [{'expedition_log_id': int, ...聚合字段...}]
    """
    since = _resolve_usage_since(days, start_at)
    query = AdventurerS1TokenUsage.query
    if since is not None:
        query = query.filter(AdventurerS1TokenUsage.created_at >= since)
    if call_type:
        query = query.filter_by(call_type=call_type)

    summary_rows = (
        query.with_entities(AdventurerS1TokenUsage.expedition_log_id, *_summary_columns())
        .group_by(AdventurerS1TokenUsage.expedition_log_id)
        .order_by(
            AdventurerS1TokenUsage.expedition_log_id.is_(None),
            AdventurerS1TokenUsage.expedition_log_id,
        )
        .all()
    )
    if not summary_rows:
        return []

    result = []
    for row in summary_rows:
        eid = row.expedition_log_id
        entry = _summary_from_row(row)
        entry['expedition_log_id'] = eid
        result.append(entry)

    return result


def get_failure_detail(record_id: int) -> dict | None:
    """One admin-selected row, in the current S1 runtime's database."""
    record = AdventurerS1TokenUsage.query.options(
        undefer(AdventurerS1TokenUsage.failure_detail),
    ).filter_by(id=record_id).first()
    if record is None or not record.has_failure_detail or not record.failure_detail:
        return None
    return {'record': record.to_dict(), 'detail': json.loads(record.failure_detail)}


def clear_failure_details(record_ids: list[int]) -> dict:
    """Clear only explicitly selected evidence; retain usage and error summaries."""
    if (not isinstance(record_ids, list) or not 1 <= len(record_ids) <= 200
            or any(type(value) is not int or not 1 <= value <= 2_147_483_647
                   for value in record_ids)):
        raise ValueError('record_ids 必须是 1 到 200 个正整数 ID')
    ids = sorted(set(record_ids))
    with db_manager.transaction():
        result, error = db_manager.execute_statement(
            update(AdventurerS1TokenUsage)
            .where(AdventurerS1TokenUsage.id.in_(ids),
                   AdventurerS1TokenUsage.is_success.is_(False),
                   AdventurerS1TokenUsage.has_failure_detail.is_(True))
            .values(failure_detail=None, has_failure_detail=False),
        )
        if error:
            raise RuntimeError('清理 AI 失败全文失败')
        cleared = result.rowcount
    return {'record_ids': ids, 'cleared': cleared}


def cleanup_expired_failure_details_job(*, now: datetime | None = None) -> dict:
    """Expire one bounded batch in this runtime, preserving all usage metadata."""
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin

    def positive_config(key):
        value = current_app.config.get(key, AdventurerS1Plugin.default_config[key])
        if isinstance(value, bool):
            raise ValueError(f'{key} 必须是正整数')
        number = int(value)
        if number <= 0 or str(number) != str(value).strip():
            raise ValueError(f'{key} 必须是正整数')
        return number

    try:
        days = positive_config('ADVENTURER_S1_AI_FAILURE_DETAIL_RETENTION_DAYS')
        batch_size = positive_config('ADVENTURER_S1_AI_FAILURE_DETAIL_CLEANUP_BATCH_SIZE')
        timestamp = now if now is not None else _portable_utc_now()
        if timestamp.tzinfo is not None:
            timestamp = timestamp.astimezone(timezone.utc).replace(tzinfo=None)
        cutoff = timestamp - timedelta(days=days)
        expired = (
            AdventurerS1TokenUsage.has_failure_detail.is_(True),
            AdventurerS1TokenUsage.created_at < cutoff,
        )
        # Select IDs only. PostgreSQL workers skip each other's locked batches;
        # the outer predicates also make concurrent/manual clearing idempotent.
        candidates = (
            select(AdventurerS1TokenUsage.id)
            .where(*expired)
            .order_by(AdventurerS1TokenUsage.created_at, AdventurerS1TokenUsage.id)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        with db_manager.transaction():
            result, error = db_manager.execute_statement(
                update(AdventurerS1TokenUsage)
                .where(AdventurerS1TokenUsage.id.in_(candidates), *expired)
                .values(failure_detail=None, has_failure_detail=False)
                .execution_options(synchronize_session=False),
            )
            if error:
                raise RuntimeError(f'清理过期 AI 失败全文失败: {error}')
            cleared = result.rowcount
        if cleared:
            current_app.logger.info(
                'S1 AI 失败全文过期清理 cleared=%s cutoff=%s',
                cleared, cutoff.isoformat(),
            )
        return {'cleared': cleared, 'cutoff': cutoff.isoformat()}
    finally:
        # transaction() explicitly commits or rolls back before release.
        db_manager.remove()


def get_records(
    page: int = 1,
    per_page: int = 50,
    call_type: str | None = None,
    party_id: int | None = None,
    expedition_log_id: int | None = None,
    correlation_id: str | None = None,
    days: int | None = None,
    start_at: datetime | None = None,
    is_success: bool | None = None,
    sort_by: str = 'created_at',
    sort_order: str = 'desc',
) -> dict:
    """分页查询 token 用量原始记录，附带小队/成员名。

    返回: {
        'records': [...],
        'total': int,
        'page': int,
        'per_page': int,
        'pages': int,
    }
    """
    query = AdventurerS1TokenUsage.query

    if call_type:
        query = query.filter_by(call_type=call_type)
    if party_id is not None:
        query = query.filter_by(party_id=party_id)
    if expedition_log_id is not None:
        query = query.filter_by(expedition_log_id=expedition_log_id)
    normalized_correlation_id = str(correlation_id or '').strip()
    if normalized_correlation_id:
        query = query.filter_by(correlation_id=normalized_correlation_id)
    if is_success is not None:
        query = query.filter_by(is_success=is_success)
    since = _resolve_usage_since(days, start_at)
    if since is not None:
        query = query.filter(AdventurerS1TokenUsage.created_at >= since)

    total = query.with_entities(func.count(AdventurerS1TokenUsage.id)).scalar()
    per_page = max(1, min(int(per_page or 50), 200))
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))

    sort_columns = {
        'id': AdventurerS1TokenUsage.id,
        'created_at': AdventurerS1TokenUsage.created_at,
        'call_type': AdventurerS1TokenUsage.call_type,
        'model': AdventurerS1TokenUsage.model,
        'profile_id': AdventurerS1TokenUsage.profile_id,
        'provider': AdventurerS1TokenUsage.provider,
        'party_id': AdventurerS1TokenUsage.party_id,
        'expedition_log_id': AdventurerS1TokenUsage.expedition_log_id,
        'correlation_id': AdventurerS1TokenUsage.correlation_id,
        'prompt_tokens_total': AdventurerS1TokenUsage.prompt_tokens_total,
        'prompt_cache_hit_tokens': AdventurerS1TokenUsage.prompt_cache_hit_tokens,
        'prompt_cache_miss_tokens': AdventurerS1TokenUsage.prompt_cache_miss_tokens,
        'completion_tokens': AdventurerS1TokenUsage.completion_tokens,
        'completion_reasoning_tokens': AdventurerS1TokenUsage.completion_reasoning_tokens,
        'visible_reasoning_chars': AdventurerS1TokenUsage.visible_reasoning_chars,
        'total_tokens': AdventurerS1TokenUsage.total_tokens,
        'is_success': AdventurerS1TokenUsage.is_success,
        'invoke_reason': AdventurerS1TokenUsage.invoke_reason,
    }
    normalized_sort_by = sort_by if sort_by in sort_columns else 'created_at'
    normalized_sort_order = 'asc' if sort_order == 'asc' else 'desc'
    sort_column = sort_columns[normalized_sort_by]
    primary_order = (
        sort_column.asc()
        if normalized_sort_order == 'asc'
        else sort_column.desc()
    )
    order_clauses = [sort_column.is_(None), primary_order]
    if normalized_sort_by != 'id':
        id_order = (
            AdventurerS1TokenUsage.id.asc()
            if normalized_sort_order == 'asc'
            else AdventurerS1TokenUsage.id.desc()
        )
        order_clauses.append(id_order)
    query = query.order_by(*order_clauses)

    rows = query.offset((page - 1) * per_page).limit(per_page).all()

    # 批量查 party 信息
    party_ids = list({r.party_id for r in rows if r.party_id})
    party_info = {}
    if party_ids:
        from plugins.adventurer_s1.models import AdventurerS1Party
        parties = AdventurerS1Party.query.filter(AdventurerS1Party.id.in_(party_ids)).all()
        for p in parties:
            party_info[p.id] = {
                'name': p.name or f'队伍#{p.id}',
                'members': ', '.join(m.name for m in p.members if m.name) if p.members else '',
            }

    records = []
    for r in rows:
        info = party_info.get(r.party_id, {})
        records.append({
            'id': r.id,
            'call_type': r.call_type,
            'call_type_label': get_call_type_label(r.call_type),
            'model': r.model,
            'profile_id': r.profile_id,
            'provider': r.provider,
            'party_id': r.party_id,
            'party_name': info.get('name', f'队伍#{r.party_id}' if r.party_id else None),
            'members': info.get('members', ''),
            'expedition_log_id': r.expedition_log_id,
            'correlation_id': r.correlation_id,
            'prompt_tokens_total': r.prompt_tokens_total,
            'prompt_cache_hit_tokens': r.prompt_cache_hit_tokens,
            'prompt_cache_miss_tokens': r.prompt_cache_miss_tokens,
            'completion_tokens': r.completion_tokens,
            'completion_reasoning_tokens': r.completion_reasoning_tokens,
            'visible_reasoning_chars': r.visible_reasoning_chars,
            'completion_content_tokens': (
                max(
                    0,
                    int(r.completion_tokens or 0)
                    - int(r.completion_reasoning_tokens),
                )
                if r.completion_reasoning_tokens is not None
                else None
            ),
            'completion_unattributed_tokens': (
                0
                if r.completion_reasoning_tokens is not None
                else r.completion_tokens
            ),
            'total_tokens': r.total_tokens,
            'attempt_number': r.attempt_number,
            'is_success': r.is_success,
            'failure_stage': r.failure_stage,
            'validation_error_code': r.validation_error_code,
            'finish_reason': r.finish_reason,
            'json_object_mode': r.json_object_mode,
            'provider_request_id': r.provider_request_id,
            'api_key_slot': r.api_key_slot,
            'api_endpoint_slot': r.api_endpoint_slot,
            'endpoint_host': r.endpoint_host,
            'transport_attempt': r.transport_attempt,
            'failover_reason': r.failover_reason,
            'failover_scope': r.failover_scope,
            'failover_attempt_count': r.failover_attempt_count,
            'repair_applied': bool(r.repair_applied),
            'reasoning_first_sentence': r.reasoning_first_sentence,
            'failed_response_excerpt': r.failed_response_excerpt,
            'has_failure_detail': bool(r.has_failure_detail),
            'invoke_reason': r.invoke_reason,
            'invoke_reason_label': get_invoke_reason_label(r.invoke_reason),
            'created_at': r.created_at.strftime('%Y-%m-%d %H:%M:%S'),
        })

    return {
        'records': records,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': pages,
        'sort_by': normalized_sort_by,
        'sort_order': normalized_sort_order,
    }


def _avg_tokens_per_call(total_tokens: int, total_calls: int) -> float:
    if total_calls <= 0:
        return 0.0
    return round(total_tokens / total_calls, 1)


def _appeal_token_grace_end_expr(updated_at_col):
    """申诉工单 updated_at + 处理宽限期（与回填 SQL 口径一致）。"""
    return updated_at_col + text(
        f"INTERVAL '{APPEAL_TOKEN_BACKFILL_GRACE_MINUTES} minutes'",
    )


def _build_appeal_token_effective_subquery(
    days: int | None = None,
    date: str | None = None,
    start_at: datetime | None = None,
):
    """申诉 token 行 + 解析后的 effective id（JOIN + LATERAL，避免 GROUP BY 内相关子查询超时）。"""
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal
    from sqlalchemy.orm import aliased

    token = AdventurerS1TokenUsage
    linked_appeal = aliased(AdventurerS1DataAppeal)
    grace_end = _appeal_token_grace_end_expr(AdventurerS1DataAppeal.updated_at)

    inferred = (
        select(
            AdventurerS1DataAppeal.id.label('inferred_appeal_id'),
            AdventurerS1DataAppeal.adventurer_id.label('inferred_adventurer_id'),
        )
        .where(
            token.appeal_id.is_(None),
            token.created_at >= AdventurerS1DataAppeal.created_at,
            token.created_at <= grace_end,
        )
        .order_by(AdventurerS1DataAppeal.created_at.desc())
        .limit(1)
        .correlate(token)
        .lateral()
    )

    stmt = (
        select(
            token.id,
            token.call_type,
            token.created_at,
            token.is_success,
            token.appeal_id,
            token.adventurer_id,
            token.prompt_tokens_total,
            token.prompt_cache_hit_tokens,
            token.prompt_cache_miss_tokens,
            token.completion_tokens,
            token.completion_reasoning_tokens,
            token.visible_reasoning_chars,
            token.total_tokens,
            func.coalesce(token.appeal_id, inferred.c.inferred_appeal_id).label('effective_appeal_id'),
            func.coalesce(
                token.adventurer_id,
                linked_appeal.adventurer_id,
                inferred.c.inferred_adventurer_id,
            ).label('effective_adventurer_id'),
        )
        .select_from(token)
        .outerjoin(linked_appeal, linked_appeal.id == token.appeal_id)
        .outerjoin(inferred, token.appeal_id.is_(None))
        .where(token.call_type.in_(APPEAL_CALL_TYPES))
    )
    since = None if date else _resolve_usage_since(days, start_at)
    if date:
        stmt = stmt.where(*_utc8_date_conditions(token.created_at, date))
    elif since is not None:
        stmt = stmt.where(token.created_at >= since)
    return stmt.subquery('appeal_token_effective')


def _fetch_appeal_token_effective_rows(
    days: int | None = None,
    date: str | None = None,
    start_at: datetime | None = None,
) -> list:
    """一次 SQL 拉取 effective 行（LATERAL 只执行一遍），后续在内存聚合。"""
    from extensions import db

    effective = _build_appeal_token_effective_subquery(
        days=days,
        date=date,
        start_at=start_at,
    )
    return db.session.execute(select(effective)).fetchall()


def _build_appeal_token_linked_subquery(
    days: int | None = None,
    date: str | None = None,
    start_at: datetime | None = None,
):
    """申诉 token 行 + 已持久化关联信息。

    管理页热路径只统计 token 自身的 adventurer_id 或已写入 appeal_id 的工单归属；
    历史未关联行交给回填按钮处理，避免每次页面加载做昂贵的时间窗推断。
    """
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal

    token = AdventurerS1TokenUsage
    stmt = (
        select(
            token.id.label('id'),
            token.call_type.label('call_type'),
            token.created_at.label('created_at'),
            token.is_success.label('is_success'),
            token.appeal_id.label('appeal_id'),
            token.adventurer_id.label('adventurer_id'),
            token.prompt_tokens_total.label('prompt_tokens_total'),
            token.prompt_cache_hit_tokens.label('prompt_cache_hit_tokens'),
            token.prompt_cache_miss_tokens.label('prompt_cache_miss_tokens'),
            token.completion_tokens.label('completion_tokens'),
            token.completion_reasoning_tokens.label('completion_reasoning_tokens'),
            token.visible_reasoning_chars.label('visible_reasoning_chars'),
            token.total_tokens.label('total_tokens'),
            token.appeal_id.label('effective_appeal_id'),
            func.coalesce(
                token.adventurer_id,
                AdventurerS1DataAppeal.adventurer_id,
            ).label('effective_adventurer_id'),
        )
        .select_from(token)
        .outerjoin(AdventurerS1DataAppeal, AdventurerS1DataAppeal.id == token.appeal_id)
        .where(token.call_type.in_(APPEAL_CALL_TYPES))
    )
    since = None if date else _resolve_usage_since(days, start_at)
    if date:
        stmt = stmt.where(*_utc8_date_conditions(token.created_at, date))
    elif since is not None:
        stmt = stmt.where(token.created_at >= since)
    return stmt.subquery('appeal_token_linked')


def _summary_select_columns(source) -> list:
    success_case = case((source.c.is_success == True, 1), else_=0)
    split_known_case = case(
        (source.c.completion_reasoning_tokens.isnot(None), 1),
        else_=0,
    )
    visible_reasoning_known_case = case(
        (source.c.visible_reasoning_chars.isnot(None), 1),
        else_=0,
    )
    content_tokens_case = case(
        (
            source.c.completion_reasoning_tokens.isnot(None),
            source.c.completion_tokens - source.c.completion_reasoning_tokens,
        ),
        else_=0,
    )
    unattributed_tokens_case = case(
        (
            source.c.completion_reasoning_tokens.is_(None),
            source.c.completion_tokens,
        ),
        else_=0,
    )
    return [
        func.count(source.c.id).label('total_calls'),
        func.coalesce(func.sum(success_case), 0).label('success_calls'),
        func.coalesce(func.sum(source.c.prompt_tokens_total), 0).label('prompt_tokens_total'),
        func.coalesce(func.sum(source.c.prompt_cache_hit_tokens), 0).label('prompt_cache_hit_tokens'),
        func.coalesce(func.sum(source.c.prompt_cache_miss_tokens), 0).label('prompt_cache_miss_tokens'),
        func.coalesce(func.sum(source.c.completion_tokens), 0).label('completion_tokens'),
        func.coalesce(func.sum(source.c.completion_reasoning_tokens), 0).label('completion_reasoning_tokens'),
        func.coalesce(func.sum(source.c.visible_reasoning_chars), 0).label('visible_reasoning_chars'),
        func.coalesce(func.sum(visible_reasoning_known_case), 0).label('visible_reasoning_known_calls'),
        func.coalesce(func.sum(content_tokens_case), 0).label('completion_content_tokens'),
        func.coalesce(func.sum(unattributed_tokens_case), 0).label('completion_unattributed_tokens'),
        func.coalesce(func.sum(split_known_case), 0).label('completion_split_known_calls'),
        func.coalesce(func.sum(source.c.total_tokens), 0).label('total_tokens'),
    ]


def _call_type_metric_columns(source) -> list:
    return [
        func.coalesce(func.sum(case((source.c.call_type == ct, 1), else_=0)), 0).label(f'{ct}_calls')
        for ct in APPEAL_CALL_TYPES
    ]


def _summary_from_mapping(row) -> dict:
    if not row:
        return _empty_stats()
    return _summary_from_row(row)


def _row_created_day(row) -> str:
    created_at = row.created_at
    if created_at is None:
        return ''
    day = created_at.date() if hasattr(created_at, 'date') else created_at
    return _day_key(day)


def _rollup_token_rows(rows) -> dict:
    """将 effective 行列表 rollup 为与 _summary_from_row 相同口径的 dict。"""
    total_calls = len(rows)
    success_calls = sum(1 for r in rows if r.is_success)
    prompt_total = sum(int(r.prompt_tokens_total or 0) for r in rows)
    cache_hit = sum(int(r.prompt_cache_hit_tokens or 0) for r in rows)
    cache_miss = sum(int(r.prompt_cache_miss_tokens or 0) for r in rows)
    comp_total = sum(int(r.completion_tokens or 0) for r in rows)
    split_known_rows = [
        r for r in rows if r.completion_reasoning_tokens is not None
    ]
    reasoning_total = sum(
        int(r.completion_reasoning_tokens or 0) for r in split_known_rows
    )
    visible_reasoning_chars = sum(
        int(r.visible_reasoning_chars or 0) for r in rows
    )
    visible_reasoning_known_calls = sum(
        1 for r in rows if r.visible_reasoning_chars is not None
    )
    content_total = sum(
        max(
            0,
            int(r.completion_tokens or 0)
            - int(r.completion_reasoning_tokens or 0),
        )
        for r in split_known_rows
    )
    unattributed_total = sum(
        int(r.completion_tokens or 0)
        for r in rows
        if r.completion_reasoning_tokens is None
    )
    grand_total = sum(int(r.total_tokens or 0) for r in rows)
    hit_rate = cache_hit / prompt_total if prompt_total > 0 else 0.0
    return {
        'total_calls': total_calls,
        'success_calls': success_calls,
        'failed_calls': total_calls - success_calls,
        'prompt_tokens_total': prompt_total,
        'prompt_cache_hit_tokens': cache_hit,
        'prompt_cache_miss_tokens': cache_miss,
        'completion_tokens': comp_total,
        'completion_reasoning_tokens': reasoning_total,
        'visible_reasoning_chars': visible_reasoning_chars,
        'visible_reasoning_known_calls': visible_reasoning_known_calls,
        'visible_reasoning_unknown_calls': (
            total_calls - visible_reasoning_known_calls
        ),
        'completion_content_tokens': content_total,
        'completion_unattributed_tokens': unattributed_total,
        'completion_split_known_calls': len(split_known_rows),
        'completion_split_unknown_calls': total_calls - len(split_known_rows),
        'total_tokens': grand_total,
        'cache_hit_rate': round(hit_rate, 4),
    }


def _link_stats_from_effective_rows(rows) -> dict:
    total = len(rows)
    linked = sum(1 for r in rows if r.appeal_id is not None)
    pending_backfill = total - linked
    with_adventurer = sum(1 for r in rows if r.effective_adventurer_id is not None)
    still_unlinked = max(0, total - with_adventurer)
    return {
        'total_records': total,
        'linked': linked,
        'pending_backfill': pending_backfill,
        'still_unlinked': still_unlinked,
    }


def _call_type_breakdown_from_rows(rows) -> dict[str, int]:
    breakdown = {ct: 0 for ct in APPEAL_CALL_TYPES}
    for row in rows:
        if row.call_type in breakdown:
            breakdown[row.call_type] += 1
    return breakdown


def invalidate_appeal_stats_by_adventurer_cache() -> None:
    _appeal_stats_by_adv_cache.clear()


def _appeal_base_query(
    days: int | None = None,
    date: str | None = None,
    start_at: datetime | None = None,
):
    query = AdventurerS1TokenUsage.query.filter(
        AdventurerS1TokenUsage.call_type.in_(APPEAL_CALL_TYPES),
    )
    since = None if date else _resolve_usage_since(days, start_at)
    if date:
        query = query.filter(*_utc8_date_conditions(
            AdventurerS1TokenUsage.created_at,
            date,
        ))
    elif since is not None:
        query = query.filter(AdventurerS1TokenUsage.created_at >= since)
    return query


def _appeal_link_stats(query) -> dict:
    """统计申诉 token 行：已关联工单 / 待回填 / 无法关联（轻量，不做时间窗推断）。"""
    total = int(query.with_entities(func.count(AdventurerS1TokenUsage.id)).scalar() or 0)
    linked = int(
        query.filter(AdventurerS1TokenUsage.appeal_id.isnot(None))
        .with_entities(func.count(AdventurerS1TokenUsage.id))
        .scalar() or 0
    )
    pending_backfill = int(
        query.filter(AdventurerS1TokenUsage.appeal_id.is_(None))
        .with_entities(func.count(AdventurerS1TokenUsage.id))
        .scalar() or 0
    )
    with_adventurer = int(
        query.filter(
            or_(
                AdventurerS1TokenUsage.adventurer_id.isnot(None),
                AdventurerS1TokenUsage.appeal_id.isnot(None),
            ),
        )
        .with_entities(func.count(AdventurerS1TokenUsage.id))
        .scalar() or 0
    )
    still_unlinked = max(0, total - with_adventurer)
    return {
        'total_records': total,
        'linked': linked,
        'pending_backfill': pending_backfill,
        'still_unlinked': still_unlinked,
    }


def preview_appeal_token_link_backfill() -> dict:
    """预览：可将多少条历史申诉 token 行回填 appeal_id（冒险者取自工单 adventurer_id）。"""
    pending = int(
        _appeal_base_query()
        .filter(AdventurerS1TokenUsage.appeal_id.is_(None))
        .with_entities(func.count(AdventurerS1TokenUsage.id))
        .scalar() or 0
    )
    return {
        'pending_backfill': pending,
        'would_update': pending,
        'note': (
            '为历史 token 行补写 appeal_id，adventurer_id 直接复制自申诉工单的 adventurer_id'
        ),
    }


def execute_appeal_token_link_backfill() -> tuple[dict | None, str | None]:
    """历史行补 appeal_id + 工单 adventurer_id（幂等，仅更新 appeal_id 为空的行）。"""
    from extensions import db

    grace = APPEAL_TOKEN_BACKFILL_GRACE_MINUTES
    sql = text(f"""
        UPDATE adventurer_s1_token_usages AS t
        SET
            appeal_id = sub.appeal_id,
            adventurer_id = sub.adventurer_id
        FROM (
            SELECT tid, appeal_id, adventurer_id
            FROM (
                SELECT
                    t.id AS tid,
                    a.id AS appeal_id,
                    a.adventurer_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY t.id
                        ORDER BY a.created_at DESC
                    ) AS rn
                FROM adventurer_s1_token_usages t
                JOIN adventurer_s1_data_appeals a
                  ON t.created_at >= a.created_at
                 AND t.created_at <= a.updated_at + INTERVAL '{grace} minutes'
                WHERE t.call_type IN (
                    'data_appeal_ai',
                    'data_appeal_review_ai',
                    'data_appeal_review_retry_ai'
                )
                  AND t.appeal_id IS NULL
            ) ranked
            WHERE rn = 1
        ) AS sub
        WHERE t.id = sub.tid
          AND t.appeal_id IS NULL
    """)

    try:
        result = db.session.execute(sql)
        updated = int(result.rowcount or 0)
        ok, err = db_manager.commit()
        if not ok:
            return None, err or 'commit 失败'
        invalidate_appeal_stats_by_adventurer_cache()
        link_stats = _appeal_link_stats(_appeal_base_query())
        return {
            'updated_rows': updated,
            **link_stats,
        }, None
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning('申诉 token 关联回填失败: %s', exc)
        return None, str(exc)


def _fetch_submitted_appeal_counts(
    days: int | None = None,
    date: str | None = None,
    start_at: datetime | None = None,
) -> tuple[dict[int, int], dict[int, dict[str, int]]]:
    """从申诉工单表汇总两个额度池的提交次数；技术失败返还口径与配额一致。"""
    from plugins.adventurer_s1.models import AdventurerS1DataAppeal

    q = db_manager.query(AdventurerS1DataAppeal).filter(
        appeal_counts_toward_daily_quota(AdventurerS1DataAppeal),
    )
    since = None if date else _resolve_usage_since(days, start_at)
    if date:
        q = q.filter(*_utc8_date_conditions(AdventurerS1DataAppeal.created_at, date))
    elif since is not None:
        q = q.filter(AdventurerS1DataAppeal.created_at >= since)

    day_expr = _utc8_day_expr(AdventurerS1DataAppeal.created_at)
    daily_rows = (
        q.with_entities(
            AdventurerS1DataAppeal.adventurer_id,
            day_expr.label('day'),
            func.count(AdventurerS1DataAppeal.id).label('submitted'),
        )
        .group_by(AdventurerS1DataAppeal.adventurer_id, day_expr)
        .all()
    )
    by_adv_day: dict[int, dict[str, int]] = {}
    by_adv: dict[int, int] = {}
    for r in daily_rows:
        if not r.adventurer_id:
            continue
        adv_id = int(r.adventurer_id)
        submitted = int(r.submitted or 0)
        by_adv_day.setdefault(adv_id, {})[_day_key(r.day)] = submitted
        by_adv[adv_id] = by_adv.get(adv_id, 0) + submitted
    return by_adv, by_adv_day


def get_appeal_stats_by_adventurer(
    days: int = 7,
    sort_by: str = 'total_tokens',
    sort_order: str = 'desc',
    date: str | None = None,
    start_at: datetime | None = None,
) -> dict:
    """按冒险者聚合申诉 AI token 用量，含每日明细与均值。

    历史行若未写 appeal_id/adventurer_id，统计时经工单处理时间窗推断（与回填口径一致）。

    Args:
        days: 统计最近 N 天（date 指定时忽略）
        sort_by: total_tokens | avg_tokens | total_calls
        sort_order: desc | asc
        date: 可选 YYYY-MM-DD，只看单日
        start_at: 可选 UTC 统计起点（date 指定时忽略，优先于 days）
    """
    normalized_start_at = None if date else _resolve_usage_since(None, start_at)
    cache_key = (days, date, normalized_start_at, sort_by, sort_order)
    return _appeal_stats_by_adv_cache.get_or_set(
        cache_key,
        lambda: _compute_appeal_stats_by_adventurer(
            days,
            sort_by,
            sort_order,
            date,
            normalized_start_at,
        ),
    )


def _compute_appeal_stats_by_adventurer(
    days: int,
    sort_by: str,
    sort_order: str,
    date: str | None,
    start_at: datetime | None,
) -> dict:
    eff_days = days if not date else None
    eff_start_at = start_at if not date else None
    effective = _build_appeal_token_linked_subquery(
        days=eff_days,
        date=date,
        start_at=eff_start_at,
    )
    link_stats = _appeal_link_stats(_appeal_base_query(
        days=eff_days,
        date=date,
        start_at=eff_start_at,
    ))

    from extensions import db
    summary_row = db.session.execute(select(*_summary_select_columns(effective))).first()
    total_calls = int(summary_row.total_calls or 0) if summary_row else 0

    if total_calls <= 0:
        return {
            'summary': _empty_stats(),
            'call_type_breakdown': {ct: 0 for ct in APPEAL_CALL_TYPES},
            'adventurers': [],
            'days': days,
            'date': date,
            'start_at': start_at.isoformat() + 'Z' if start_at else None,
            'sort_by': sort_by,
            'sort_order': sort_order,
            'link_stats': link_stats,
        }

    summary = _summary_from_mapping(summary_row)
    summary['avg_tokens_per_call'] = _avg_tokens_per_call(
        summary['total_tokens'], summary['total_calls'],
    )
    call_type_row = db.session.execute(select(*_call_type_metric_columns(effective))).first()
    call_type_breakdown = {
        ct: int(getattr(call_type_row, f'{ct}_calls', 0) or 0)
        for ct in APPEAL_CALL_TYPES
    }
    submitted_by_adv, submitted_by_adv_day = _fetch_submitted_appeal_counts(
        days=eff_days,
        date=date,
        start_at=eff_start_at,
    )

    day_expr = _utc8_day_expr(effective.c.created_at)
    daily_rows = db.session.execute(
        select(
            day_expr.label('day'),
            *_summary_select_columns(effective),
        )
        .group_by(day_expr)
        .order_by(day_expr)
    ).fetchall()

    adv_rows = db.session.execute(
        select(
            effective.c.effective_adventurer_id.label('adventurer_id'),
            func.count(func.distinct(effective.c.effective_appeal_id)).label('linked_appeals'),
            *_summary_select_columns(effective),
        )
        .group_by(effective.c.effective_adventurer_id)
    ).fetchall()

    adv_type_rows = db.session.execute(
        select(
            effective.c.effective_adventurer_id.label('adventurer_id'),
            effective.c.call_type.label('call_type'),
            func.count(effective.c.id).label('calls'),
            func.coalesce(func.sum(effective.c.total_tokens), 0).label('total_tokens'),
        )
        .group_by(effective.c.effective_adventurer_id, effective.c.call_type)
    ).fetchall()
    by_adv_type: dict[tuple[int | None, str], dict] = {
        (row.adventurer_id, row.call_type): {
            'calls': int(row.calls or 0),
            'total_tokens': int(row.total_tokens or 0),
            'avg_tokens_per_call': _avg_tokens_per_call(int(row.total_tokens or 0), int(row.calls or 0)),
        }
        for row in adv_type_rows
    }

    adv_daily_rows = db.session.execute(
        select(
            effective.c.effective_adventurer_id.label('adventurer_id'),
            day_expr.label('day'),
            func.count(func.distinct(effective.c.effective_appeal_id)).label('linked_appeals'),
            *_summary_select_columns(effective),
        )
        .group_by(effective.c.effective_adventurer_id, day_expr)
        .order_by(effective.c.effective_adventurer_id, day_expr)
    ).fetchall()
    daily_by_adv: dict[int | None, list] = defaultdict(list)
    for row in adv_daily_rows:
        day_key = _day_key(row.day)
        calls = int(row.total_calls or 0)
        total = int(row.total_tokens or 0)
        daily_by_adv[row.adventurer_id].append({
            'date': day_key,
            'calls': calls,
            'linked_appeals': int(row.linked_appeals or 0),
            'submitted_appeals': (submitted_by_adv_day.get(row.adventurer_id) or {}).get(day_key, 0),
            'total_tokens': total,
            'prompt_tokens_total': int(row.prompt_tokens_total or 0),
            'completion_tokens': int(row.completion_tokens or 0),
            'visible_reasoning_chars': int(row.visible_reasoning_chars or 0),
            'visible_reasoning_known_calls': int(row.visible_reasoning_known_calls or 0),
            'visible_reasoning_unknown_calls': (
                calls - int(row.visible_reasoning_known_calls or 0)
            ),
            'avg_tokens_per_call': _avg_tokens_per_call(total, calls),
        })

    adv_ids = [row.adventurer_id for row in adv_rows if row.adventurer_id]
    adv_names: dict[int, str] = {}
    if adv_ids:
        from plugins.adventurer_s1.models import AdventurerS1
        name_rows = (
            AdventurerS1.query.with_entities(AdventurerS1.id, AdventurerS1.name)
            .filter(AdventurerS1.id.in_(adv_ids))
            .all()
        )
        adv_names = {row.id: row.name or f'冒险者#{row.id}' for row in name_rows}

    adventurers = []
    for row in adv_rows:
        adv_id = row.adventurer_id
        entry = _summary_from_mapping(row)
        total_calls = entry['total_calls']
        total_tokens = entry['total_tokens']
        entry['adventurer_id'] = adv_id
        entry['adventurer_name'] = (
            adv_names.get(adv_id, f'冒险者#{adv_id}' if adv_id else '未关联冒险者')
        )
        entry['submitted_appeals'] = submitted_by_adv.get(adv_id, 0) if adv_id else 0
        entry['linked_appeals'] = int(row.linked_appeals or 0)
        entry['avg_tokens_per_call'] = _avg_tokens_per_call(total_tokens, total_calls)
        entry['daily'] = daily_by_adv.get(adv_id, [])
        entry['by_call_type'] = {}
        for ct in APPEAL_CALL_TYPES:
            entry['by_call_type'][ct] = by_adv_type.get(
                (adv_id, ct),
                {'calls': 0, 'total_tokens': 0, 'avg_tokens_per_call': 0.0},
            )
        adventurers.append(entry)

    reverse = sort_order.lower() != 'asc'
    if sort_by == 'avg_tokens':
        adventurers.sort(key=lambda x: x['avg_tokens_per_call'], reverse=reverse)
    elif sort_by == 'total_calls':
        adventurers.sort(key=lambda x: x['total_calls'], reverse=reverse)
    else:
        adventurers.sort(key=lambda x: x['total_tokens'], reverse=reverse)

    summary['daily'] = []
    for row in daily_rows:
        calls = int(row.total_calls or 0)
        total = int(row.total_tokens or 0)
        summary['daily'].append({
            'date': _day_key(row.day),
            'calls': calls,
            'total_tokens': total,
            'visible_reasoning_chars': int(row.visible_reasoning_chars or 0),
            'visible_reasoning_known_calls': int(row.visible_reasoning_known_calls or 0),
            'visible_reasoning_unknown_calls': (
                calls - int(row.visible_reasoning_known_calls or 0)
            ),
            'avg_tokens_per_call': _avg_tokens_per_call(total, calls),
        })

    return {
        'summary': summary,
        'call_type_breakdown': call_type_breakdown,
        'adventurers': adventurers,
        'days': days,
        'date': date,
        'start_at': start_at.isoformat() + 'Z' if start_at else None,
        'sort_by': sort_by,
        'sort_order': sort_order,
        'link_stats': link_stats,
    }
