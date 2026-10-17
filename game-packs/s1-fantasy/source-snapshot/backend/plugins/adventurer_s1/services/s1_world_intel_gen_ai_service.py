"""管理员世界情报 AI 草稿生成。

本模块只把管理员概述转换为现有 ``world_story_threads`` 写入契约的草稿，
不直接写库。管理员仍需在前端检查并通过既有情报 CRUD 接口保存。
"""

import json
import logging

import requests

from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
    AIProfileAssignmentError,
    post_s1_ai_request,
    resolve_s1_ai_profile_snapshot,
)
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    S1AiResponseContractResult,
    parse_s1_ai_http_json_response,
    parse_s1_ai_json_content,
)
from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
)
from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
from plugins.adventurer_s1.services.s1_world_outline_contract import WORLD_INTEL_ALL_DUNGEONS_SCOPE

logger = logging.getLogger(__name__)

WORLD_INTEL_TYPES = ('entity', 'event', 'faction', 'location', 'omen', 'artifact')

_SYSTEM_PROMPT = """你是深渊酒馆 S1 的世界情报编辑。管理员会给出一段概述，你要把它整理成一条可供现有探险剧情系统持续引用和演化的世界情报草稿。

【职责边界】
- 只创建当前已经成立的初始设定，不预写未来剧情结果，不替未来队伍作决定，也不虚构已经发生过的探险。
- 草稿可能描述势力、强大 NPC、地点、事件、征兆或器物。保留管理员指定的身份、关系、力量与地牢范围；其余细节用于补全可辨识性、当前目标、行动边界和后续可用抓手。
- 势力和其中的关键 NPC 是不同世界主体；本次只输出一条草稿。管理员若概述聚焦 NPC，就以 entity 建档，并在摘要、关键词或范围中保留其所属势力的准确专名。
- scope_keys 是当前可受影响或可出没的地点、区域、势力或实体域，不是永久锁定。后续只有已结算剧情事实才能扩大、缩小或迁移 scope_keys；不要预先安排变化结果。
- base_dungeon 只是初始稳定召回锚点；有初始地牢时原样使用第一项，没有则留空。管理员明确选择全部地牢时，base_dungeon 与 scope_keys 使用固定范围“全部地牢”。它不等于该主体必须在每次探险出场。
- power_profile 写不可随意突破的连续性边界，尤其是强者的能力层级、限制、代价与不能被普通队伍无依据改写的事实；不要写数值概率或后台判定标签。
- next_reveal_hint 只写未来剧情可自然验证或推进的方向，不强制下次出现，不预定成功、失败、死亡或阵营变化。
- 玩家可见设定保持纯世界叙事，不暴露 prompt、schema、系统字段用途或后台规则。

【输出契约】
只输出一个 JSON 对象，不要 Markdown 或解释：
{"title":"专名或情报标题","thread_type":"entity|event|faction|location|omen|artifact","status":"seeded","weight":1,"base_dungeon":"字符串","scope_keys":["字符串"],"trigger_keywords":["字符串"],"summary":"当前公共事实","next_reveal_hint":"可选后续方向","power_tier":"可选影响层级","power_profile":"可选连续性边界"}

title 与 summary 必填；scope_keys 最多 8 项；trigger_keywords 最多 10 项；weight 为 1~100 的整数。
trigger_keywords 必须把 title 的前 50 个字符原样作为一个独立数组项；title 不超过 50 个字符时即使用完整 title。该标题项计入最多 10 项，不得拆分、缩写或改写。"""


def _clip_text(value, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ''


def _clip_ai_text(value: object, limit: int, *, required: bool = False) -> tuple[str, bool]:
    """读取 AI 文本槽；可安全裁剪落地，但不把有损修正计为契约成功。"""
    if value is None and not required:
        return '', True
    if not isinstance(value, str):
        return '', False
    text = value.strip()
    if required and not text:
        return '', False
    if len(text) > limit:
        return text[:limit], False
    return text, True


def _normalize_text_list(value, *, limit: int, item_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _clip_text(item, item_chars)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _normalize_ai_text_list(
    value: object,
    *,
    limit: int,
    item_chars: int,
) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    result: list[str] = []
    contract_valid = len(value) <= limit
    for item in value:
        text, item_valid = _clip_ai_text(item, item_chars, required=True)
        contract_valid = contract_valid and item_valid
        if text and text not in result and len(result) < limit:
            result.append(text)
    return result, contract_valid


def _coerce_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_user_prompt(
    brief: str,
    preferred_type: str,
    initial_dungeons: list[str],
    *,
    all_dungeons: bool = False,
) -> str:
    type_instruction = preferred_type if preferred_type in WORLD_INTEL_TYPES else '由概述判断最合适的类型'
    dungeon_text = (
        '全部地牢（当前已开放及后续新增地牢）'
        if all_dungeons
        else ('、'.join(initial_dungeons) if initial_dungeons else '未指定')
    )
    return (
        f'【管理员概述】\n{brief}\n\n'
        f'【期望类型】\n{type_instruction}\n\n'
        f'【初始出没/影响地牢】\n{dungeon_text}\n\n'
        '请按输出契约生成一条可编辑草稿。管理员明确给出的专名与地牢名必须原样保留。'
    )


def _parse_json_object(content: str) -> dict | None:
    text = str(content or '').strip()
    if '```' in text:
        start, end = text.find('{'), text.rfind('}')
        if start >= 0 and end > start:
            text = text[start:end + 1]
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        pass
    else:
        return parsed if isinstance(parsed, dict) else None
    # 旧格式严格解析失败后，公共修复必须检查完整原文，不能继续裁掉尾部。
    result = parse_s1_ai_json_content(content, repair_mode='lossless_syntax_only')
    return result.data if result.ok else None


def _validate_draft_with_status(
    raw: dict,
    *,
    preferred_type: str,
    initial_dungeons: list[str],
    all_dungeons: bool = False,
) -> tuple[dict | None, str | None, bool]:
    title, title_valid = _clip_ai_text(raw.get('title'), 120, required=True)
    raw_summary = raw.get('summary')
    summary = raw_summary.strip() if isinstance(raw_summary, str) else ''
    if not title or not summary:
        return None, 'AI 草稿缺少标题或摘要', False

    contract_valid = title_valid

    raw_generated_type = raw.get('thread_type')
    generated_type = (
        raw_generated_type.strip()
        if isinstance(raw_generated_type, str)
        else ''
    )
    contract_valid = contract_valid and bool(generated_type)
    thread_type = preferred_type if preferred_type in WORLD_INTEL_TYPES else generated_type
    if thread_type not in WORLD_INTEL_TYPES:
        return None, 'AI 草稿的情报类型无效', False
    if preferred_type in WORLD_INTEL_TYPES and generated_type != preferred_type:
        contract_valid = False
    if raw.get('status') != 'seeded':
        contract_valid = False

    scopes, scopes_valid = _normalize_ai_text_list(
        raw.get('scope_keys'),
        limit=8,
        item_chars=80,
    )
    contract_valid = contract_valid and scopes_valid
    required_scopes = [WORLD_INTEL_ALL_DUNGEONS_SCOPE] if all_dungeons else initial_dungeons
    if any(required_scope not in scopes for required_scope in required_scopes):
        contract_valid = False
    scopes = list(dict.fromkeys(required_scopes + scopes))[:8]
    keywords, keywords_valid = _normalize_ai_text_list(
        raw.get('trigger_keywords'),
        limit=10,
        item_chars=50,
    )
    contract_valid = contract_valid and keywords_valid
    title_keyword = title[:50]
    if title_keyword not in keywords:
        contract_valid = False
        keywords.insert(0, title_keyword)
        keywords = keywords[:10]

    expected_base_dungeon = (
        WORLD_INTEL_ALL_DUNGEONS_SCOPE
        if all_dungeons
        else (initial_dungeons[0] if initial_dungeons else '')
    )
    raw_base_dungeon = raw.get('base_dungeon')
    if (
        not isinstance(raw_base_dungeon, str)
        or raw_base_dungeon.strip() != expected_base_dungeon
    ):
        contract_valid = False

    normalized_weight, _weight_changed = normalize_ascii_integer(
        raw.get('weight'),
        minimum=1,
        maximum=100,
    )
    weight_valid = (
        type(normalized_weight) is int
        and 1 <= normalized_weight <= 100
    )
    weight = normalized_weight if weight_valid else 60
    contract_valid = contract_valid and weight_valid

    next_reveal_hint, next_reveal_valid = _clip_ai_text(
        raw.get('next_reveal_hint'),
        2000,
    )
    power_tier, power_tier_valid = _clip_ai_text(raw.get('power_tier'), 30)
    power_profile, power_profile_valid = _clip_ai_text(
        raw.get('power_profile'),
        4000,
    )
    contract_valid = (
        contract_valid
        and next_reveal_valid
        and power_tier_valid
        and power_profile_valid
    )

    return {
        'title': title,
        'thread_type': thread_type,
        'status': 'seeded',
        'weight': weight,
        'base_dungeon': expected_base_dungeon,
        'scope_keys': scopes,
        'trigger_keywords': keywords,
        'summary': summary,
        'next_reveal_hint': next_reveal_hint,
        'power_tier': power_tier,
        'power_profile': power_profile,
    }, None, contract_valid


def _validate_draft(
    raw: dict,
    *,
    preferred_type: str,
    initial_dungeons: list[str],
    all_dungeons: bool = False,
) -> tuple[dict | None, str | None]:
    draft, error, _ = _validate_draft_with_status(
        raw,
        preferred_type=preferred_type,
        initial_dungeons=initial_dungeons,
        all_dungeons=all_dungeons,
    )
    return draft, error


def generate_world_intel_draft(payload: dict) -> tuple[dict | None, str | None]:
    """根据管理员概述生成一条已校验、未持久化的世界情报草稿。"""
    body = payload if isinstance(payload, dict) else {}
    brief = str(body.get('brief') or '').strip()
    if not brief:
        return None, '请输入世界情报概述'
    if len(brief) > 4000:
        return None, '世界情报概述不能超过 4000 字'

    preferred_type = str(body.get('preferred_type') or '').strip()
    if preferred_type and preferred_type not in WORLD_INTEL_TYPES:
        return None, '期望情报类型无效'
    all_dungeons = body.get('all_dungeons') is True
    initial_dungeons = _normalize_text_list(
        body.get('initial_dungeons'), limit=8, item_chars=80,
    )
    if all_dungeons:
        initial_dungeons = []

    try:
        route = resolve_s1_ai_profile_snapshot('world_intel')
    except AIProfileAssignmentError as exc:
        logger.warning('S1 管理员世界情报生成用途路由不可用: %s', exc)
        return None, '世界情报生成 AI 路由不可用'
    api_key = route.api_key
    api_base = route.api_base
    model = route.model
    timeout = max(30, _coerce_int(
        current_app.config.get('ADVENTURER_S1_WORLD_OUTLINE_AI_TIMEOUT_SECONDS', 600),
        600,
    ))
    user_prompt = _build_user_prompt(
        brief,
        preferred_type,
        initial_dungeons,
        all_dungeons=all_dungeons,
    )
    usage = None
    is_success = False
    request_metadata: dict[str, str] = {}
    request_exception: BaseException | None = None

    def validate_world_intel_response(
        decoded,
    ) -> S1AiResponseContractResult:
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(
                decoded.json_result.error_code or 'invalid_json',
                failure_stage='response_parse',
            )
        draft_candidate, _, contract_valid = _validate_draft_with_status(
            decoded.json_result.data,
            preferred_type=preferred_type,
            initial_dungeons=initial_dungeons,
            all_dungeons=all_dungeons,
        )
        if draft_candidate is not None and contract_valid:
            return S1AiResponseContractResult.accept()
        return S1AiResponseContractResult.reject(
            'world_intel_contract_invalid',
            failure_stage='contract_validation',
        )

    try:
        db_manager.remove()
        response = post_s1_ai_request(
            f'{api_base}/chat/completions',
            feature='world_intel',
            resolved_profile=route,
            request_metadata_out=request_metadata,
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': _SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_prompt},
                ],
                'temperature': 0.5,
            },
            timeout=timeout,
            response_validator=validate_world_intel_response,
        )
        decoded = parse_s1_ai_http_json_response(response)
        usage = decoded.http_result.usage
        raw = decoded.json_result.data
        if not decoded.json_result.ok or raw is None:
            return None, 'AI 未返回有效的世界情报 JSON'
        draft, error, contract_valid = _validate_draft_with_status(
            raw,
            preferred_type=preferred_type,
            initial_dungeons=initial_dungeons,
            all_dungeons=all_dungeons,
        )
        is_success = draft is not None and contract_valid
        return draft, error
    except (KeyError, TypeError, ValueError, requests.RequestException) as exc:
        request_exception = exc
        logger.warning('S1 管理员世界情报生成失败: %s', exc)
        return None, '世界情报生成失败，请稍后重试'
    finally:
        if usage or request_exception is not None:
            record_token_usage(
                call_type='world_intel_generation_ai',
                usage=usage,
                model=request_metadata.get('model') or model,
                attempt_number=1,
                is_success=is_success,
                failure_stage=None if is_success else 'http_or_response',
                validation_error_code=(
                    None
                    if is_success or request_exception is None
                    else f'http_or_response.{type(request_exception).__name__}'
                ),
                record_without_usage=request_exception is not None,
                request_metadata=request_metadata,
                request_exception=request_exception,
            )
