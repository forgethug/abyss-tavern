"""S1 15 级命途抉择与跨副本个人旅程。"""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import hashlib
import json
import logging
import random
import re
import secrets
from datetime import datetime, timedelta
from types import SimpleNamespace

import requests

from plugins.adventurer_s1.services.s1_ai_runtime_config_service import (
    resolve_s1_ai_profile_snapshot,
)
from plugins.adventurer_s1.services.s1_ai_player_name_service import (
    post_s1_ai_request_with_player_aliases as post_s1_ai_request,
)
from plugins.adventurer_s1.services.s1_ai_json_response_service import (
    S1AiResponseContractResult,
    parse_s1_ai_http_json_response,
)
from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1ExpeditionLog,
    AdventurerS1FateEvent,
    AdventurerS1FateJourney,
    AdventurerS1FateResetRequest,
    AdventurerS1StoryThread,
)
from plugins.adventurer_s1.services.adventurer_s1_config_service import S1_ACTIVE_DUNGEONS
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
    normalize_exact_boolean,
)

logger = logging.getLogger(__name__)

FATE_LEVEL = 15
FATE_NPC_NAME = '命途引路人'
_FATE_DEAD_ERROR = '已死亡的冒险者无法开启或继续命途'
_CANONICAL_CHOICE_ROUTE_TYPES = (
    'profession',
    'mastery',
    'legacy',
    'transformation',
    'freedom',
)
_LEGACY_CHOICE_ROUTE_TYPE_ALIASES = {'identity': 'profession'}
_ROUTE_TYPES = frozenset(_CANONICAL_CHOICE_ROUTE_TYPES)
_CHOICE_OPTION_COUNT = 3
_CHOICE_DUNGEON_COUNT = 3
_CHOICE_TEXT_LIMITS = {
    'reflection': 800,
    'title': 120,
    'summary': 500,
    'initial_clue': 600,
}
_FATE_PROGRESS_OUTCOMES = frozenset({
    'no_progress', 'partial', 'advanced', 'final_failed', 'final_success', 'unconfirmed',
})
_LOCATION_NEUTRAL_CLUE_FALLBACK = '本阶段仍有命途证据尚未厘清，可从下方任一可推进地点继续追查。'

_CHOICE_SYSTEM_PROMPT = """你负责为达到成长分水岭的冒险者生成命途抉择。
根据输入的当前职业、性格、技能、特质、装备与历史经历，输出彼此有实质差异、且人物本人愿意追寻的个人愿望。
必须恰好输出 3 个候选选项；不得在 reflection 或其它玩家可见文字中暗示还存在未展示的额外候选道路。
路线决定玩家授权的成果倾向，但不能预订具体职业、技能、特质、装备或保证必定成功。职业路线授权无职业者转职、有职业者进阶、转向或确认原职业；其它路线不得默认改变职业。
候选项必须由输入证据支撑，不得补造输入未建立的人物事实或关系。
每个选项的 initial_clue 只描述下一阶段要查清的问题、证据或人物抉择，并保持地点中立；candidate_dungeons 独立给出 3 个均可推进该阶段的地点，且只能选自输入的 available_dungeons。
玩家可见文字使用自然的游戏内叙事，不得出现 AI、系统字段、概率、后台判断或数据结构。
只输出 JSON 对象，字段与跨字段约束严格遵守 user 提供的 output_contract。"""


def _clip(value, limit: int) -> str:
    text = str(value or '').strip()
    return text if len(text) <= limit else text[:limit].rstrip()


def _normalize_reward_manifest(value: object) -> dict:
    raw = value if isinstance(value, dict) else {}
    def names(field: str) -> list[str]:
        values = raw.get(field)
        if not isinstance(values, list):
            return []
        return [
            _clip(name, 120)
            for name in values
            if isinstance(name, str) and _clip(name, 120)
        ][:24]
    new_class, _ = normalize_exact_boolean(raw.get('new_class', False))
    return {
        'new_class': new_class if type(new_class) is bool else False,
        'new_skills': names('new_skills'),
        'new_traits': names('new_traits'),
        'new_equipments': names('new_equipments'),
    }


def resolve_fate_reward_level_range(
    context: object,
    default_min_level: int,
    default_max_level: int,
) -> tuple[int, int]:
    """命途关键事件与最终试炼统一按后端签发的奖励等级结算。"""
    if not isinstance(context, dict) or context.get('event_type') not in {'key', 'final'}:
        return default_min_level, default_max_level
    try:
        reward_level = int(context.get('reward_level') or context.get('trial_level') or 0)
    except (TypeError, ValueError):
        return default_min_level, default_max_level
    if reward_level <= 0:
        return default_min_level, default_max_level
    return reward_level, reward_level


def normalize_fate_progress(value: object, context: dict | None = None) -> dict | None:
    """宽容规范化命途进展；协议错误只降级为 unconfirmed，不触发整场重试。"""
    if not isinstance(context, dict) and not isinstance(value, dict):
        return None
    raw = value if isinstance(value, dict) else {}
    event_type = str((context or {}).get('event_type') or '').strip()
    outcome = str(raw.get('outcome') or '').strip()
    if outcome not in _FATE_PROGRESS_OUTCOMES:
        outcome = 'unconfirmed'
    allowed_by_event = {
        'ordinary': {'no_progress'},
        'echo': {'no_progress', 'partial'},
        'key': {'no_progress', 'partial', 'advanced'},
        'final': {'final_failed', 'final_success'},
    }
    if event_type and outcome not in allowed_by_event.get(event_type, {'unconfirmed'}):
        outcome = 'unconfirmed'
    raw_focus_id = (context or {}).get('adventurer_id')
    if raw_focus_id is None:
        raw_focus_id = raw.get('focus_adventurer_id')
    focus_id, _ = normalize_ascii_integer(raw_focus_id, minimum=1)
    if type(focus_id) is not int or focus_id <= 0:
        focus_id = None
    raw_evidence = raw.get('evidence_summary')
    evidence = _clip(raw_evidence, 600) if isinstance(raw_evidence, str) else ''
    raw_next_clue = raw.get('next_clue')
    next_clue = _normalize_location_neutral_clue(
        raw_next_clue if isinstance(raw_next_clue, str) else '',
        required=event_type == 'key' and outcome == 'advanced',
    )
    context_has_attempt_id = (
        isinstance(context, dict) and 'final_attempt_id' in context
    )
    raw_attempt_id = (
        context.get('final_attempt_id')
        if context_has_attempt_id
        else raw.get('final_attempt_id')
    )
    final_attempt_id = (
        _clip(raw_attempt_id, 80)
        if context_has_attempt_id or isinstance(raw_attempt_id, str)
        else ''
    )
    return {
        'focus_adventurer_id': focus_id,
        'final_attempt_id': final_attempt_id or None,
        'outcome': outcome,
        'evidence_summary': evidence,
        'next_clue': next_clue,
        'reward_manifest': _normalize_reward_manifest(raw.get('reward_manifest')),
    }


def _json_from_text(text: str) -> dict | None:
    raw = str(text or '').strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.I)
        raw = re.sub(r'\s*```$', '', raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        match = re.search(r'\{.*\}', raw, flags=re.S)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None


def _available_dungeon_names() -> list[str]:
    # 命途沿用 15 级角色的完整主动选图范围；活动池已排除三张未开放的 15+ 终局副本。
    return [str(row.get('name')) for row in S1_ACTIVE_DUNGEONS if row.get('name')]


def _choice_output_contract() -> dict:
    """把 consumer 已执行的命途候选合同完整投影给生成模型。"""
    return {
        'type': 'object',
        'required_fields': ['reflection', 'options'],
        'allow_extra_fields': False,
        'fields': {
            'reflection': {
                'type': 'string',
                'min_chars': 1,
                'max_chars': _CHOICE_TEXT_LIMITS['reflection'],
            },
            'options': {
                'type': 'array',
                'exact_count': _CHOICE_OPTION_COUNT,
                'unique_by': 'route_type',
                'item': {
                    'type': 'object',
                    'required_fields': [
                        'route_type',
                        'title',
                        'summary',
                        'initial_clue',
                        'candidate_dungeons',
                    ],
                    'allow_extra_fields': False,
                    'fields': {
                        'route_type': {
                            'type': 'string',
                            'enum': list(_CANONICAL_CHOICE_ROUTE_TYPES),
                        },
                        'title': {
                            'type': 'string',
                            'min_chars': 1,
                            'max_chars': _CHOICE_TEXT_LIMITS['title'],
                        },
                        'summary': {
                            'type': 'string',
                            'min_chars': 1,
                            'max_chars': _CHOICE_TEXT_LIMITS['summary'],
                        },
                        'initial_clue': {
                            'type': 'string',
                            'min_chars': 1,
                            'max_chars': _CHOICE_TEXT_LIMITS['initial_clue'],
                        },
                        'candidate_dungeons': {
                            'type': 'array',
                            'exact_count': _CHOICE_DUNGEON_COUNT,
                            'unique_items': True,
                            'item': {
                                'type': 'string',
                                'allowed_values_from': 'adventurer.available_dungeons',
                            },
                        },
                    },
                },
            },
        },
        'cross_field_rules': [
            {
                'path': 'options[].initial_clue',
                'must_not_contain_any_from': 'adventurer.available_dungeons',
            },
            {
                'when': {
                    'path': 'adventurer.character_class',
                    'is_empty': True,
                },
                'require': {
                    'path': 'options[].route_type',
                    'contains': 'profession',
                },
            },
        ],
    }


def _normalize_location_neutral_clue(value: object, *, required: bool = False) -> str:
    """线索描述调查目标；地点选择只由 candidate_dungeons 承担。"""
    clue = _clip(value, 600)
    if clue and not any(name in clue for name in _available_dungeon_names()):
        return clue
    return _LOCATION_NEUTRAL_CLUE_FALLBACK if required or clue else ''


def _next_chapter_candidate_dungeons(
    journey_id: int,
    next_chapter_index: int,
    expedition_log_id: int,
    target_dungeon: str,
) -> list[str]:
    """在生成剧情前确定下一章节地点，保证 prompt 与结算使用同一结果。"""
    names = [name for name in _available_dungeon_names() if name != target_dungeon]
    seed = random.Random(f'fate-next:{journey_id}:{next_chapter_index}:{expedition_log_id}')
    return seed.sample(names, min(3, len(names)))


def _new_choice_batch_id() -> str:
    return secrets.token_hex(12)


def _normalize_choices_with_diagnostics(
    data: dict | None,
    adv: AdventurerS1,
    batch_id: str | None = None,
) -> tuple[dict, bool, tuple[str, ...]]:
    batch_id = batch_id or _new_choice_batch_id()
    contract_errors: list[str] = []

    def reject(code: str) -> None:
        if code not in contract_errors:
            contract_errors.append(code)

    if not isinstance(data, dict):
        return (
            {'reflection': '', 'options': []},
            False,
            ('root.type',),
        )

    raw_reflection = data.get('reflection')
    if isinstance(raw_reflection, str):
        reflection = _clip(
            raw_reflection,
            _CHOICE_TEXT_LIMITS['reflection'],
        )
        if not raw_reflection.strip():
            reject('reflection.empty')
        if len(raw_reflection.strip()) > _CHOICE_TEXT_LIMITS['reflection']:
            reject('reflection.max_length')
    else:
        reflection = ''
        reject('reflection.type')

    raw_options = data.get('options')
    if isinstance(raw_options, list):
        option_rows = raw_options
        if len(raw_options) != _CHOICE_OPTION_COUNT:
            reject('options.count')
    else:
        option_rows = []
        reject('options.type')

    available_dungeon_names = _available_dungeon_names()
    available_dungeon_set = set(available_dungeon_names)
    options: list[dict] = []
    used_types: set[str] = set()
    for index, row in enumerate(option_rows):
        if not isinstance(row, dict):
            reject('option.type')
            continue

        raw_route_type_value = row.get('route_type')
        if isinstance(raw_route_type_value, str):
            raw_route_type = raw_route_type_value.strip()
            route_type = _LEGACY_CHOICE_ROUTE_TYPE_ALIASES.get(
                raw_route_type,
                raw_route_type,
            )
        else:
            route_type = ''
            reject('option.route_type.type')
        route_type_allowed = route_type in _ROUTE_TYPES
        route_type_duplicate = route_type_allowed and route_type in used_types
        if isinstance(raw_route_type_value, str) and not route_type_allowed:
            reject('option.route_type.enum')
        if route_type_duplicate:
            reject('option.route_type.duplicate')

        normalized_text: dict[str, str] = {}
        for field in ('title', 'summary', 'initial_clue'):
            raw_value = row.get(field)
            if not isinstance(raw_value, str):
                normalized_text[field] = ''
                reject(f'option.{field}.type')
                continue
            normalized_text[field] = _clip(
                raw_value,
                _CHOICE_TEXT_LIMITS[field],
            )
            if not raw_value.strip():
                reject(f'option.{field}.empty')
            if len(raw_value.strip()) > _CHOICE_TEXT_LIMITS[field]:
                reject(f'option.{field}.max_length')

        title = normalized_text['title']
        summary = normalized_text['summary']
        initial_clue = normalized_text['initial_clue']
        if initial_clue and any(
            name in initial_clue for name in available_dungeon_names
        ):
            reject('option.initial_clue.contains_dungeon')
            initial_clue = ''
        if not initial_clue:
            reject('option.initial_clue.empty')

        candidate_dungeons: list[str] = []
        raw_candidate_dungeons = row.get('candidate_dungeons')
        if not isinstance(raw_candidate_dungeons, list):
            reject('option.candidate_dungeons.type')
            raw_candidate_dungeons = []
        for raw_name in raw_candidate_dungeons:
            if not isinstance(raw_name, str):
                reject('option.candidate_dungeons.item_type')
                continue
            name = raw_name.strip()
            if name not in available_dungeon_set:
                reject('option.candidate_dungeons.unknown')
                continue
            if name in candidate_dungeons:
                reject('option.candidate_dungeons.duplicate')
                continue
            candidate_dungeons.append(name)
        if (
            len(raw_candidate_dungeons) != _CHOICE_DUNGEON_COUNT
            or len(candidate_dungeons) != _CHOICE_DUNGEON_COUNT
        ):
            reject('option.candidate_dungeons.count')

        if (
            not route_type_allowed
            or route_type_duplicate
            or not title
            or not summary
        ):
            continue
        used_types.add(route_type)
        options.append({
            'id': f'route-{batch_id}-{index + 1}', 'route_type': route_type, 'title': title,
            'summary': summary, 'initial_clue': initial_clue,
            'candidate_dungeons': candidate_dungeons[:_CHOICE_DUNGEON_COUNT],
        })
    if len(options) != _CHOICE_OPTION_COUNT:
        reject('options.valid_count')
    if not adv.character_class and not any(row['route_type'] == 'profession' for row in options):
        reject('options.classless_requires_profession')
    final_options = options
    for position, option in enumerate(final_options, start=1):
        option['id'] = f'route-{batch_id}-{position}'
    return {
        'reflection': reflection,
        'options': final_options,
    }, not contract_errors, tuple(contract_errors)


def _normalize_choices_with_status(
    data: dict | None,
    adv: AdventurerS1,
    batch_id: str | None = None,
) -> tuple[dict, bool]:
    normalized, contract_valid, _ = _normalize_choices_with_diagnostics(
        data,
        adv,
        batch_id,
    )
    return normalized, contract_valid


def _profile_asset_description(
    row,
    summaries_by_id: dict[int, str],
    summary_ids,
    *,
    summaries_enabled: bool = False,
    field: str = 'description',
) -> str:
    """仅精确命中内部摘要时替换；未命中时始终保留完整原文。"""
    raw_full = row.get(field) if isinstance(row, dict) else getattr(row, field, None)
    full = str(raw_full or '').strip()
    try:
        raw_id = row.get('id') if isinstance(row, dict) else getattr(row, 'id', 0)
        row_id = int(raw_id or 0)
    except (TypeError, ValueError):
        row_id = 0
    projected = str((summaries_by_id or {}).get(row_id) or '').strip()
    if not summaries_enabled:
        return full or projected
    if projected and row_id in (summary_ids or ()):
        return projected
    return full or projected


def _choice_profile(adv: AdventurerS1, description_ai_context: dict | None = None) -> dict:
    context = description_ai_context if isinstance(description_ai_context, dict) else {}
    skill_summaries = context.get('skill_by_id') or {}
    equipment_summaries = context.get('equipment_by_id') or {}
    trait_summaries = context.get('trait_by_id') or {}
    trait_summary_ids = context.get('trait_summary_ids') or set()
    active_trait_ids = context.get('active_trait_ids')
    skill_summary_ids = context.get('skill_summary_ids') or set()
    equipment_summary_ids = context.get('equipment_summary_ids') or set()
    summaries_enabled = bool(context.get('summaries_enabled'))
    return {
        'name': adv.name,
        'level': int(adv.level or 1),
        'character_class': adv.character_class,
        'personality': adv.personality,
        'history_story': _clip(adv.history_story, 3500),
        'traits': [
            {
                'name': row.name,
                'desc': _profile_asset_description(
                    row,
                    trait_summaries,
                    trait_summary_ids,
                    summaries_enabled=summaries_enabled,
                ),
            }
            for row in adv.traits
            if not row.is_archived
            and (
                not isinstance(active_trait_ids, set)
                or int(getattr(row, 'id', 0) or 0) in active_trait_ids
            )
        ][:16],
        'skills': [
            {
                'name': row.name,
                'desc': _profile_asset_description(
                    row,
                    skill_summaries,
                    skill_summary_ids,
                    summaries_enabled=summaries_enabled,
                ),
            }
            for row in adv.skill_items
        ][:16],
        'equipment': [
            {
                'name': row.name,
                'desc': _profile_asset_description(
                    row,
                    equipment_summaries,
                    equipment_summary_ids,
                    summaries_enabled=summaries_enabled,
                    field='custom_desc',
                ),
            }
            for row in adv.equipment_items
        ][:12],
        'available_dungeons': _available_dungeon_names(),
    }


def _build_final_character_evidence(adv: AdventurerS1, journey: AdventurerS1FateJourney) -> dict:
    """最终试炼人物证据：当前权威状态优先，个人剧情按状态分层。"""
    from plugins.adventurer_s1.services.s1_description_ai_service import (
        build_member_description_ai_context,
    )

    description_ai_context = build_member_description_ai_context([adv])
    trait_summaries = description_ai_context.get('trait_by_id') or {}
    trait_summary_ids = description_ai_context.get('trait_summary_ids') or set()
    active_trait_ids = description_ai_context.get('active_trait_ids') or set()
    skill_summaries = description_ai_context.get('skill_by_id') or {}
    equipment_summaries = description_ai_context.get('equipment_by_id') or {}
    skill_summary_ids = description_ai_context.get('skill_summary_ids') or set()
    equipment_summary_ids = description_ai_context.get('equipment_summary_ids') or set()
    summaries_enabled = bool(description_ai_context.get('summaries_enabled'))
    base_query = AdventurerS1StoryThread.query.filter(
        AdventurerS1StoryThread.adventurer_id == adv.id,
    )
    resolved_threads = (
        base_query.filter(AdventurerS1StoryThread.status == 'resolved')
        .order_by(
            AdventurerS1StoryThread.last_touched_at.desc(),
            AdventurerS1StoryThread.weight.desc(),
        )
        .limit(10)
        .all()
    )
    open_threads = (
        base_query.filter(AdventurerS1StoryThread.status.in_(('active', 'dormant')))
        .order_by(
            AdventurerS1StoryThread.weight.desc(),
            AdventurerS1StoryThread.last_touched_at.desc(),
        )
        .limit(10)
        .all()
    )
    threads = resolved_threads + open_threads

    def thread_rows(statuses: set[str], limit: int) -> list[dict]:
        return [{
            'title': _clip(row.title, 120),
            'summary': _clip(row.summary, 700),
            'status': row.status,
            'tags': [_clip(tag, 60) for tag in (row.tags or [])[:6]],
        } for row in threads if row.status in statuses][:limit]

    def row_value(row, *names):
        for name in names:
            value = row.get(name) if isinstance(row, dict) else getattr(row, name, None)
            if value is not None:
                return value
        return None

    return {
        'current_state': {
            'character_class': adv.character_class,
            'personality': _clip(adv.personality, 500),
            'mental_state': _clip(getattr(adv, 'mental_state', None), 500),
            'traits': [
                {
                    'name': row_value(row, 'name'),
                    'desc': _profile_asset_description(
                        row,
                        trait_summaries,
                        trait_summary_ids,
                        summaries_enabled=summaries_enabled,
                    ),
                }
                for row in adv.traits
                if not bool(row_value(row, 'is_archived'))
                and int(row_value(row, 'id') or 0) in active_trait_ids
            ][:16],
            'skills': [
                {
                    'name': row_value(row, 'name'),
                    'desc': _profile_asset_description(
                        row,
                        skill_summaries,
                        skill_summary_ids,
                        summaries_enabled=summaries_enabled,
                    ),
                }
                for row in adv.skill_items
            ][:16],
            'important_equipment': [
                {
                    'name': row_value(row, 'name'),
                    'desc': _profile_asset_description(
                        row,
                        equipment_summaries,
                        equipment_summary_ids,
                        summaries_enabled=summaries_enabled,
                        field='custom_desc',
                    ),
                }
                for row in adv.equipment_items
            ][:12],
        },
        'resolved_personal_threads': thread_rows({'resolved'}, 10),
        'open_personal_threads': thread_rows({'active', 'dormant'}, 10),
        'chosen_direction': {
            'outcome_preference': 'profession' if journey.route_type == 'identity' else journey.route_type,
            'route_title': journey.route_title,
            'route_summary': journey.route_summary,
            'current_clue': journey.current_clue,
        },
    }


def _call_choice_ai(adv: AdventurerS1) -> dict:
    batch_id = _new_choice_batch_id()
    adv_ref = SimpleNamespace(
        id=int(adv.id),
        name=str(adv.name),
        character_class=adv.character_class,
    )
    route = resolve_s1_ai_profile_snapshot('fate_journey')
    api_key = route.api_key
    api_base = route.api_base
    model = route.model
    from plugins.adventurer_s1.services.s1_description_ai_service import (
        build_member_description_ai_context,
        enqueue_member_description_prewarm,
    )

    # 命途与主剧情一致：摘要只异步预热，当前请求对未命中项使用完整原文。
    queued = enqueue_member_description_prewarm([adv])
    if queued:
        logger.info(
            'S1 命途抉择资产描述压缩已异步入队 adv=%s items=%s',
            adv_ref.id,
            queued,
        )
    description_ai_context = build_member_description_ai_context([adv])
    profile = _choice_profile(adv, description_ai_context)
    # 人物画像完成后立即释放查询 session，禁止跨 AI HTTP 等待持有数据库连接。
    db_manager.remove()
    user_prompt = json.dumps({
        'adventurer': profile,
        'output_contract': _choice_output_contract(),
    }, ensure_ascii=False, separators=(',', ':'))
    usage = None
    is_success = False
    failure_stage = 'http_or_response'
    validation_error_code: str | None = None
    request_metadata: dict[str, object] = {}
    request_exception: BaseException | None = None
    finish_reason: str | None = None
    json_object_mode: bool | None = None
    provider_request_id: str | None = None
    repair_applied = False
    failed_response_excerpt: str | None = None

    def validate_fate_choice_response(
        decoded,
    ) -> S1AiResponseContractResult:
        if not decoded.json_result.ok:
            return S1AiResponseContractResult.reject(
                decoded.json_result.error_code or 'invalid_json',
                failure_stage='response_parse',
            )
        _, contract_valid, contract_errors = (
            _normalize_choices_with_diagnostics(
                decoded.json_result.data,
                adv_ref,
                batch_id,
            )
        )
        if contract_valid:
            return S1AiResponseContractResult.accept()
        primary_error = contract_errors[0] if contract_errors else 'unknown'
        return S1AiResponseContractResult.reject(
            f'invalid_fate_choices.{primary_error}'[:160],
            failure_stage='contract_validation',
        )

    try:
        response = post_s1_ai_request(
            f'{api_base}/chat/completions',
            feature='fate_journey',
            player_names=(adv_ref.name,),
            resolved_profile=route,
            request_metadata_out=request_metadata,
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': _CHOICE_SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_prompt},
                ],
                'temperature': 0.6,
                'response_format': {'type': 'json_object'},
            },
            timeout=int(current_app.config.get('ADVENTURER_S1_FATE_AI_TIMEOUT_SECONDS', 90)),
            response_validator=validate_fate_choice_response,
        )
        decoded = parse_s1_ai_http_json_response(response)
        usage = decoded.http_result.usage
        request_metadata.update(decoded.http_result.runtime_metadata or {})
        json_result = decoded.json_result
        finish_reason = json_result.finish_reason
        repair_applied = json_result.repair_applied is True
        failed_response_excerpt = json_result.raw_excerpt or None
        raw_json_object_mode = request_metadata.get('json_object_mode')
        if type(raw_json_object_mode) is bool:
            json_object_mode = raw_json_object_mode
        elif isinstance(raw_json_object_mode, str):
            json_object_mode = {
                'true': True,
                'false': False,
            }.get(raw_json_object_mode.strip().lower())
        raw_provider_request_id = (
            request_metadata.get('request_id')
            or request_metadata.get('provider_request_id')
        )
        if raw_provider_request_id is not None:
            provider_request_id = str(raw_provider_request_id).strip() or None
        if not json_result.ok:
            failure_stage = 'response_parse'
            validation_error_code = (
                f'response_parse.{json_result.error_code or "invalid_json"}'
            )
            raise ValueError(
                f'命途候选响应不可用: {json_result.error_code}'
            )
        normalized, is_success, contract_errors = _normalize_choices_with_diagnostics(
            json_result.data,
            adv_ref,
            batch_id,
        )
        if not is_success:
            failure_stage = 'contract_validation'
            primary_error = contract_errors[0] if contract_errors else 'unknown'
            validation_error_code = (
                'contract_validation.invalid_fate_choices.'
                f'{primary_error}'
            )
            logger.warning(
                '命途候选合同校验失败 adv=%s errors=%s',
                adv_ref.id,
                ','.join(contract_errors),
            )
            raise ValueError(
                f'命途候选响应不符合结构合同: {primary_error}'
            )
        return normalized
    except Exception as exc:  # noqa: BLE001
        request_exception = exc
        if validation_error_code is None:
            validation_error_code = f'{failure_stage}.{type(exc).__name__}'
        logger.exception('生成命途候选失败 adv=%s，未保存任何候选', adv_ref.id)
        raise
    finally:
        if usage or request_exception is not None:
            from plugins.adventurer_s1.services.s1_token_usage_service import record_token_usage
            record_token_usage(
                call_type='fate_choice_ai', usage=usage,
                model=request_metadata.get('model') or model,
                adventurer_id=adv_ref.id, is_success=is_success,
                failure_stage=None if is_success else failure_stage,
                validation_error_code=None if is_success else validation_error_code,
                finish_reason=finish_reason,
                json_object_mode=json_object_mode,
                provider_request_id=provider_request_id,
                repair_applied=repair_applied,
                failed_response_excerpt=(
                    None if is_success else failed_response_excerpt
                ),
                record_without_usage=request_exception is not None,
                request_metadata=request_metadata,
                request_exception=request_exception,
            )


def _journey_for_update(adventurer_id: int) -> AdventurerS1FateJourney | None:
    return AdventurerS1FateJourney.query.filter_by(adventurer_id=adventurer_id).with_for_update().first()


def _pending_reset_request(
    journey_id: int,
    *,
    for_update: bool = False,
) -> AdventurerS1FateResetRequest | None:
    query = AdventurerS1FateResetRequest.query.filter_by(
        journey_id=journey_id,
        status='pending',
    ).order_by(AdventurerS1FateResetRequest.id.desc())
    if for_update:
        query = query.with_for_update()
    return query.first()


def _choice_generation_is_active(journey: AdventurerS1FateJourney) -> bool:
    if not journey.choice_generation_token or not journey.choice_generation_started_at:
        return False
    timeout_seconds = int(current_app.config.get(
        'ADVENTURER_S1_FATE_CHOICE_GENERATION_LEASE_SECONDS', 300,
    ))
    return journey.choice_generation_started_at >= (
        _portable_utc_now() - timedelta(seconds=max(30, timeout_seconds))
    )


def _clear_choice_generation_lease(
    journey: AdventurerS1FateJourney | None,
    generation_token: str | None,
) -> None:
    if (
        journey is not None
        and generation_token
        and journey.status == 'awaiting_choice'
        and not journey.choice_options
        and journey.choice_generation_token == generation_token
    ):
        journey.choice_generation_token = None
        journey.choice_generation_started_at = None


def _reset_snapshot(journey: AdventurerS1FateJourney) -> dict:
    events = AdventurerS1FateEvent.query.filter_by(journey_id=journey.id).order_by(
        AdventurerS1FateEvent.id.asc(),
    ).all()
    return {
        'journey': journey.to_player_dict(include_events=False),
        'choice_generated_at': (
            journey.choice_generated_at.isoformat() if journey.choice_generated_at else None
        ),
        'events': [{
            'id': row.id,
            'expedition_log_id': row.expedition_log_id,
            'chapter_index': row.chapter_index,
            'dungeon_name': row.dungeon_name,
            'event_type': row.event_type,
            'result_status': row.result_status,
            'title': row.title,
            'summary': row.summary,
            'payload': row.payload,
            'created_at': row.created_at.isoformat() if row.created_at else None,
        } for row in events],
    }


def _clear_journey_for_reset(journey: AdventurerS1FateJourney) -> None:
    journey.status = 'awaiting_choice'
    journey.route_type = None
    journey.route_title = None
    journey.route_summary = None
    journey.reflection = None
    journey.choice_options = []
    journey.current_clue = None
    journey.candidate_dungeons = []
    journey.chapter_index = 0
    journey.key_event_count = 0
    journey.related_expedition_count = 0
    journey.miss_count = 0
    journey.echo_count = 0
    journey.visited_dungeons = []
    journey.breakthrough_completed = False
    journey.final_outcome = None
    journey.final_expedition_log_id = None
    journey.choice_generated_at = None
    journey.choice_generation_token = None
    journey.choice_generation_started_at = None
    journey.chosen_at = None
    journey.completed_at = None


def _player_journey_dict(journey: AdventurerS1FateJourney, *, include_events: bool = False) -> dict:
    """命途面板的展示投影；完整旅程仍供服务端结算与重置快照使用。"""
    snapshot = journey.to_player_dict(include_events=include_events)
    data = {
        key: snapshot[key]
        for key in (
            'status', 'route_title', 'route_summary', 'reflection', 'current_clue',
            'candidate_dungeons', 'key_event_count', 'related_expedition_count',
            'visited_dungeons', 'breakthrough_completed',
        )
    }
    data['choice_options'] = [
        {key: option[key] for key in ('id', 'title', 'summary') if key in option}
        for option in snapshot['choice_options']
        if isinstance(option, dict)
    ]
    if include_events:
        data['events'] = [
            {key: event[key] for key in ('id', 'dungeon_name', 'title', 'summary')}
            for event in snapshot['events']
        ]
    current_clue = str(data.get('current_clue') or '')
    if (
        data.get('status') in {'pursuing', 'final_ready'}
        and (not current_clue or any(name in current_clue for name in _available_dungeon_names()))
    ):
        data['current_clue'] = _LOCATION_NEUTRAL_CLUE_FALLBACK
    return data


class S1FateJourneyService:
    @staticmethod
    def get_player_state(adventurer_id: int, user_id: int) -> tuple[dict | None, str | None]:
        adv = AdventurerS1.query.filter_by(id=adventurer_id).first()
        if not adv or int(adv.owner_id or 0) != int(user_id):
            return None, '只有绑定玩家可以查看该冒险者的命途'
        if adv.status == 'dead':
            return {
                'eligible': False,
                'unavailable_reason': 'dead',
                'required_level': FATE_LEVEL,
                'npc_name': FATE_NPC_NAME,
            }, None
        if int(adv.level or 1) < FATE_LEVEL:
            return {'eligible': False, 'required_level': FATE_LEVEL, 'npc_name': FATE_NPC_NAME}, None
        journey = AdventurerS1FateJourney.query.filter_by(adventurer_id=adv.id).first()
        journey_data = _player_journey_dict(journey, include_events=True) if journey else {
            'status': 'awaiting_choice',
            'route_title': None,
            'route_summary': None,
            'reflection': None,
            'choice_options': [],
            'current_clue': None,
            'candidate_dungeons': [],
            'key_event_count': 0,
            'related_expedition_count': 0,
            'visited_dungeons': [],
            'breakthrough_completed': False,
            'events': [],
        }
        reset_request = _pending_reset_request(journey.id) if journey else None
        return {
            'eligible': True,
            'required_level': FATE_LEVEL,
            'npc_name': FATE_NPC_NAME,
            'key_events_required': int(current_app.config.get('ADVENTURER_S1_FATE_KEY_EVENTS_REQUIRED', 8)),
            'choice_generation_in_progress': bool(
                journey is not None and _choice_generation_is_active(journey)
            ),
            'journey': journey_data,
            'reset_request': reset_request.to_player_dict() if reset_request else None,
        }, None

    @staticmethod
    def enqueue_choice_generation(adventurer_id: int, user_id: int) -> tuple[dict | None, str | None]:
        """校验玩家请求并投递 AI；请求线程不等待模型返回。"""
        from services.async_task_service import AsyncTaskService
        with db_manager.transaction():
            adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
            if not adv or int(adv.owner_id or 0) != int(user_id):
                return None, '只有绑定玩家可以开启命途抉择'
            if adv.status == 'dead':
                return None, _FATE_DEAD_ERROR
            if int(adv.level or 1) < FATE_LEVEL:
                return None, f'达到 {FATE_LEVEL} 级后才能开启命途抉择'
            journey = _journey_for_update(adv.id)
            if journey is None:
                journey = AdventurerS1FateJourney(adventurer_id=adv.id)
                db_manager.add(journey)
                db_manager.flush()
            if journey.status != 'awaiting_choice':
                return None, '命途旅程已经开始，不能重新生成候选'
            if journey.choice_options:
                return {
                    'preparing': False,
                    'journey': _player_journey_dict(journey, include_events=True),
                }, None
            if not _choice_generation_is_active(journey):
                journey.choice_generation_token = secrets.token_hex(24)
                journey.choice_generation_started_at = _portable_utc_now()
            generation_token = str(journey.choice_generation_token or '')
            locked_adventurer_id = int(adv.id)
        payload = {
            'adventurer_id': locked_adventurer_id,
            'user_id': int(user_id),
            'generation_token': generation_token,
        }
        already_running = AsyncTaskService.is_task_enqueued_or_running(
            'ai_s1_fate_choices', payload,
        )
        if not already_running and not AsyncTaskService.submit_task('ai_s1_fate_choices', payload):
            if not AsyncTaskService.is_task_enqueued_or_running('ai_s1_fate_choices', payload):
                return None, '命途暂时没有回应，请稍后再试'
        return {'preparing': True}, None

    @staticmethod
    def generate_choices(
        adventurer_id: int,
        user_id: int,
        generation_token: str | None = None,
    ) -> tuple[dict | None, str | None]:
        with db_manager.transaction():
            adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
            if not adv or int(adv.owner_id or 0) != int(user_id):
                return None, '只有绑定玩家可以开启命途抉择'
            existing = _journey_for_update(adv.id)
            if adv.status == 'dead':
                _clear_choice_generation_lease(existing, generation_token)
                return None, _FATE_DEAD_ERROR
            if int(adv.level or 1) < FATE_LEVEL:
                return None, f'达到 {FATE_LEVEL} 级后才能开启命途抉择'
            if existing is not None:
                if existing.status != 'awaiting_choice':
                    return None, '命途旅程已经开始，不能重新生成候选'
                if existing.choice_options:
                    return _player_journey_dict(existing, include_events=True), None
                if generation_token and existing.choice_generation_token != generation_token:
                    return None, '本次命途生成请求已经失效'
        try:
            choices = _call_choice_ai(adv)
        except Exception:  # noqa: BLE001
            db_manager.rollback()
            if generation_token:
                with db_manager.transaction():
                    journey = _journey_for_update(adventurer_id)
                    _clear_choice_generation_lease(journey, generation_token)
            return None, '命途暂时没有回应，请稍后再试'
        with db_manager.transaction():
            locked_adv = (
                AdventurerS1.query.filter_by(id=adventurer_id)
                .with_for_update()
                .populate_existing()
                .first()
            )
            if not locked_adv or int(locked_adv.owner_id or 0) != int(user_id):
                return None, '只有绑定玩家可以开启命途抉择'
            journey = _journey_for_update(adventurer_id)
            if locked_adv.status == 'dead':
                _clear_choice_generation_lease(journey, generation_token)
                return None, _FATE_DEAD_ERROR
            if int(locked_adv.level or 1) < FATE_LEVEL:
                return None, f'达到 {FATE_LEVEL} 级后才能开启命途抉择'
            if journey is None:
                journey = AdventurerS1FateJourney(adventurer_id=adventurer_id)
                db_manager.add(journey)
            if journey.status != 'awaiting_choice':
                return None, '命途旅程已经开始，不能重新生成候选'
            if journey.choice_options:
                return _player_journey_dict(journey, include_events=True), None
            if generation_token and journey.choice_generation_token != generation_token:
                return None, '本次命途生成请求已经失效'
            journey.reflection = choices['reflection']
            journey.choice_options = choices['options']
            journey.choice_generated_at = _portable_utc_now()
            journey.choice_generation_token = None
            journey.choice_generation_started_at = None
        return _player_journey_dict(journey, include_events=True), None

    @staticmethod
    def choose_route(adventurer_id: int, user_id: int, option_id: str) -> tuple[dict | None, str | None]:
        with db_manager.transaction():
            adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
            if not adv or int(adv.owner_id or 0) != int(user_id):
                return None, '只有绑定玩家可以支持该冒险者的选择'
            if adv.status == 'dead':
                return None, _FATE_DEAD_ERROR
            journey = _journey_for_update(adv.id)
            if journey is None or journey.status != 'awaiting_choice':
                return None, '当前没有待确认的命途抉择'
            option = next((row for row in (journey.choice_options or []) if row.get('id') == option_id), None)
            if not option:
                return None, '命途选项不存在或已经失效'
            journey.route_type = option['route_type']
            journey.route_title = option['title']
            journey.route_summary = option['summary']
            journey.status = 'pursuing'
            journey.chapter_index = 1
            journey.current_clue = option.get('initial_clue') or option['summary']
            journey.candidate_dungeons = option.get('candidate_dungeons') or []
            journey.chosen_at = _portable_utc_now()
        return _player_journey_dict(journey, include_events=True), None

    @staticmethod
    def get_admin_reset_state(adventurer_id: int) -> tuple[dict | None, str | None]:
        adv = AdventurerS1.query.filter_by(id=adventurer_id).first()
        if not adv:
            return None, '冒险者不存在'
        journey = AdventurerS1FateJourney.query.filter_by(adventurer_id=adv.id).first()
        request_row = _pending_reset_request(journey.id) if journey else None
        return {
            'adventurer_id': adv.id,
            'adventurer_name': adv.name,
            'journey': _player_journey_dict(journey, include_events=False) if journey else None,
            'reset_request': request_row.to_admin_dict() if request_row else None,
        }, None

    @staticmethod
    def request_reset(
        adventurer_id: int,
        admin_user_id: int,
        reason: str = '',
    ) -> tuple[dict | None, str | None]:
        reason = _clip(reason, 500)
        with db_manager.transaction():
            adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
            if not adv:
                return None, '冒险者不存在'
            if adv.status == 'dead':
                return None, _FATE_DEAD_ERROR
            if not adv.owner_id:
                return None, '该冒险者没有绑定玩家，无法发送确认'
            journey = _journey_for_update(adv.id)
            if not journey:
                return None, '该冒险者尚未开启命途，无需重置'
            existing = _pending_reset_request(journey.id, for_update=True)
            if existing:
                return existing.to_admin_dict(), None
            if journey.status == 'awaiting_choice' and not journey.choice_options:
                return None, '当前命途已经处于未生成状态，无需重置'
            request_row = AdventurerS1FateResetRequest(
                journey_id=journey.id,
                adventurer_id=adv.id,
                requested_by_user_id=admin_user_id,
                status='pending',
                reason=reason,
                journey_snapshot=_reset_snapshot(journey),
                requested_at=_portable_utc_now(),
            )
            db_manager.add(request_row)
            db_manager.flush()
        return request_row.to_admin_dict(), None

    @staticmethod
    def respond_reset(
        adventurer_id: int,
        user_id: int,
        request_id: int,
        accept: bool,
    ) -> tuple[dict | None, str | None]:
        with db_manager.transaction():
            adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
            if not adv or int(adv.owner_id or 0) != int(user_id):
                return None, '只有绑定玩家可以确认命途重置'
            if adv.status == 'dead':
                return None, _FATE_DEAD_ERROR
            journey = _journey_for_update(adv.id)
            if not journey:
                return None, '命途旅程不存在'
            request_row = AdventurerS1FateResetRequest.query.filter_by(
                id=request_id,
                journey_id=journey.id,
                adventurer_id=adv.id,
            ).with_for_update().first()
            if not request_row or request_row.status != 'pending':
                return None, '命途重置确认不存在或已经处理'
            if not accept:
                request_row.status = 'rejected'
                request_row.responded_at = _portable_utc_now()
                return {
                    'accepted': False,
                    'journey': _player_journey_dict(journey, include_events=True),
                }, None
            if adv.status != 'resting':
                return None, '请先让冒险者退出队伍并回到休息状态，再确认重置命途'
            AdventurerS1FateEvent.query.filter_by(journey_id=journey.id).delete(
                synchronize_session=False,
            )
            _clear_journey_for_reset(journey)
            request_row.status = 'accepted'
            request_row.responded_at = _portable_utc_now()
        return {
            'accepted': True,
            'journey': _player_journey_dict(journey, include_events=True),
        }, None

    @staticmethod
    def build_expedition_prompt_block(
        members: list, target_dungeon: str, expedition_log_id: int | None, name_alias: dict[int, str] | None = None,
        context_out: dict | None = None,
    ) -> str:
        from .adventurer_s1_config_service import is_s1_independent_dungeon
        if is_s1_independent_dungeon(target_dungeon):
            return ''
        if not members or not expedition_log_id:
            return ''
        member_ids = [int(m.id) for m in members]
        journeys = AdventurerS1FateJourney.query.filter(
            AdventurerS1FateJourney.adventurer_id.in_(member_ids),
            AdventurerS1FateJourney.status.in_(('pursuing', 'final_ready')),
        ).all()
        eligible = [j for j in journeys if target_dungeon in (j.candidate_dungeons or [])]
        if not eligible:
            return ''
        # 一场只设一名焦点；等待越久优先级越高，同一日志用 hash 保证重试结果稳定。
        eligible.sort(key=lambda row: (int(row.miss_count or 0), -int(row.id)), reverse=True)
        journey = eligible[0]
        digest = hashlib.sha256(f'{expedition_log_id}:{journey.id}'.encode()).digest()
        roll = int.from_bytes(digest[:4], 'big') / 2**32
        key_base = float(current_app.config.get('ADVENTURER_S1_FATE_KEY_BASE_CHANCE', 0.25))
        key_step = float(current_app.config.get('ADVENTURER_S1_FATE_KEY_MISS_STEP', 0.15))
        key_pity = int(current_app.config.get('ADVENTURER_S1_FATE_KEY_PITY', 5))
        echo_chance = float(current_app.config.get('ADVENTURER_S1_FATE_ECHO_CHANCE', 0.45))
        key_chance = min(1.0, key_base + int(journey.miss_count or 0) * key_step)
        if journey.status == 'final_ready':
            event_type = 'final'
        elif int(journey.miss_count or 0) >= key_pity - 1 or roll < key_chance:
            event_type = 'key'
        elif roll < min(1.0, key_chance + echo_chance):
            event_type = 'echo'
        else:
            event_type = 'ordinary'
        adv = next(m for m in members if int(m.id) == int(journey.adventurer_id))
        ref = (name_alias or {}).get(adv.id) or adv.name
        context = {
            'journey_id': journey.id,
            'adventurer_id': adv.id,
            'event_type': event_type,
            'chapter_index': journey.chapter_index,
            'route_title': journey.route_title,
            'route_summary': journey.route_summary,
            'current_clue': journey.current_clue,
            'target_dungeon': target_dungeon,
            'candidate_dungeons': list(journey.candidate_dungeons or []),
        }
        if event_type in {'key', 'final'}:
            context['trial_level'] = FATE_LEVEL
            context['reward_level'] = FATE_LEVEL
        if event_type == 'key':
            context['next_candidate_dungeons'] = _next_chapter_candidate_dungeons(
                journey.id,
                int(journey.chapter_index or 1) + 1,
                expedition_log_id,
                target_dungeon,
            )
        if event_type == 'final':
            context['final_attempt_id'] = hashlib.sha256(
                f'fate-final:{journey.id}:{adv.id}:{expedition_log_id}'.encode(),
            ).hexdigest()[:32]
            context['character_evidence'] = _build_final_character_evidence(adv, journey)
        if isinstance(context_out, dict):
            context_out.update(context)
        with db_manager.transaction():
            log = AdventurerS1ExpeditionLog.query.filter_by(id=expedition_log_id).with_for_update().first()
            if log:
                full_log = dict(log.full_log or {})
                full_log['fate_journey_context'] = context
                log.full_log = full_log
        if event_type == 'ordinary':
            return ''
        if event_type == 'echo':
            candidate_text = '、'.join(context['candidate_dungeons'])
            return (
                f'【命途线索回响】{ref}正在追寻「{journey.route_title}」。本次只安排一个自然、克制的线索回响，'
                '可以确认、排除或深化方向，但不得宣告章节完成，不得据此发放重大成长。'
                '状态更新 JSON 顶层须附带 fate_progress 对象，字段为 focus_adventurer_id、outcome、evidence_summary、next_clue；'
                '本层 outcome 只能是 no_progress 或 partial。字段缺失不会打回整场，但会按未确认处理。'
                f'当前可推进地点：{candidate_text}，三处均可推进本阶段。next_clue 若非空，只描述新确认的问题、证据或人物抉择并保持地点中立。'
                f'当前线索：{journey.current_clue}'
            )
        if event_type == 'final':
            recent = AdventurerS1FateEvent.query.filter_by(journey_id=journey.id).order_by(
                AdventurerS1FateEvent.id.desc(),
            ).limit(4).all()
            continuity = '；'.join(_clip(row.summary, 160) for row in reversed(recent) if row.summary)
            character_evidence = json.dumps(context['character_evidence'], ensure_ascii=False)
            preference = context['character_evidence']['chosen_direction']['outcome_preference']
            preference_rule = {
                'profession': '玩家选择职业道路：允许无职业者获得职业，也允许现有职业进阶、转向或确认原职业；具体职业必须来自人物证据。',
                'mastery': '玩家选择技艺道路：成果优先深化技能体系，除非人物定位已根本改变，否则不改职业。',
                'transformation': '玩家选择蜕变道路：成果优先体现长期特质变化，职业变化仅在人物定位同步改变时成立。',
                'legacy': '玩家选择传承道路：成果优先体现知识、责任、代表性能力或传承物，不默认改变职业。',
                'freedom': '玩家选择自由道路：不预设成果类别，由人物证据决定。',
            }.get(preference, '按玩家选择的方向决定成果，不默认改变职业。')
            return (
                f'【最终命途试炼】本场以{ref}为唯一命途焦点，收束「{journey.route_title}」的长期旅程。'
                '以角色当前权威状态为基础；已收束个人剧情用于证明角色已经做成的事、选择与代价，活跃或休眠个人剧情用于提供仍在面对的问题。'
                '已解决内容不得重新激活，旧成果不得重复发放；命途事件摘要仅用于连续性校验，不能压过当前状态和个人剧情。'
                f'玩家成果倾向：{preference_rule}'
                '必须据此决定重大成果；成果可以是职业变化，也可以是技艺、特质蜕变、传承物或自由道路，不能默认转职。'
                '这是角色达到当前成长上限后的突破级结算：主要成果必须相对人物命途画像形成显著、可持续且具有个人专属性的长期改变，明显区别于普通副本成长；'
                '配套成果必须让主要成果在角色数据和后续剧情中实际成立，不能只为凑足字段，也不能用奖励数量或普通副本收益代替丰厚度。'
                '若完成试炼，member_updates 必须用现有结构化字段落下一项主要成果和与之相符的配套成果；职业变化仍须同时给身份特质与职业技能。'
                '状态更新 JSON 顶层须附带 fate_progress 对象，字段为 focus_adventurer_id、final_attempt_id、outcome、evidence_summary、next_clue；'
                f'final_attempt_id 必须原样返回为 {context["final_attempt_id"]}；'
                '本层 outcome 只能是 final_failed 或 final_success，并必须与玩家可见正文一致；'
                'outcome 不按 member_updates 的字段数量或组合机械决定。即使 final_failed，正文中正常成立的其它结构化结算仍照常输出，不得为表示失败而缩减。'
                f'人物命途画像：{character_evidence}'
                f'命途事件辅助连续性：{continuity}'
            )
        next_candidate_text = '、'.join(context['next_candidate_dungeons'])
        return (
            f'【命途关键事件】本场以{ref}为唯一命途焦点，围绕「{journey.route_title}」与当前线索形成有实质选择和代价的个人事件。'
            '正文必须给出该阶段是否取得关键进展的清楚证据；不要预设一定转职，也不要仅因这是关键事件就直接发放最终命途奖励。'
            '状态更新 JSON 顶层须附带 fate_progress 对象，字段为 focus_adventurer_id、outcome、evidence_summary、next_clue；'
            '本层 outcome 只能是 no_progress、partial 或 advanced。字段缺失不会打回整场，但不会自动算作完成。'
            f'若 outcome=advanced，next_clue 必须描述下一章节要继续确认的问题、证据或人物抉择并保持地点中立；下一章节可推进地点独立为：{next_candidate_text}，三处均有效。'
            f'当前线索：{journey.current_clue}'
        )

    @staticmethod
    def settle_expedition(members: list, log: AdventurerS1ExpeditionLog, full_log: dict) -> None:
        context = (full_log or {}).get('fate_journey_context')
        if not isinstance(context, dict):
            return
        journey = _journey_for_update(int(context.get('adventurer_id') or 0))
        if not journey or journey.status not in ('pursuing', 'final_ready'):
            return
        existing = AdventurerS1FateEvent.query.filter_by(expedition_log_id=log.id).first()
        if existing:
            return
        event_type = str(context.get('event_type') or 'ordinary')
        summary = _clip(full_log.get('summary'), 1200)
        fate_progress = normalize_fate_progress(full_log.get('fate_progress'), context) or {
            'focus_adventurer_id': journey.adventurer_id,
            'outcome': 'unconfirmed', 'evidence_summary': '', 'next_clue': '',
        }
        member_update = next((
            row for row in (full_log.get('member_updates') or [])
            if isinstance(row, dict) and int(row.get('id') or 0) == int(journey.adventurer_id)
        ), {})
        final_success = (
            event_type == 'final'
            and fate_progress.get('outcome') == 'final_success'
        )
        if event_type == 'key':
            result_status = fate_progress.get('outcome') or 'unconfirmed'
        elif event_type == 'echo':
            result_status = fate_progress.get('outcome') or 'unconfirmed'
        elif event_type == 'final':
            result_status = 'completed' if final_success else 'failed'
        else:
            result_status = 'observed'
        event = AdventurerS1FateEvent(
            journey_id=journey.id,
            expedition_log_id=log.id,
            chapter_index=journey.chapter_index,
            dungeon_name=log.target_dungeon,
            event_type=event_type,
            result_status=result_status,
            title='最终命途试炼' if event_type == 'final' else (
                '命途关键事件' if event_type == 'key' else ('命途线索回响' if event_type == 'echo' else '相关追查')
            ),
            summary=summary,
            payload={'route_title': journey.route_title, 'fate_progress': fate_progress},
        )
        db_manager.add(event)
        journey.related_expedition_count = int(journey.related_expedition_count or 0) + 1
        visited = list(journey.visited_dungeons or [])
        if log.target_dungeon not in visited:
            visited.append(log.target_dungeon)
        journey.visited_dungeons = visited
        if event_type == 'final':
            if final_success:
                journey.status = 'completed'
                journey.breakthrough_completed = True
                journey.final_expedition_log_id = log.id
                manifest = fate_progress.get('reward_manifest') or {}
                final_outcome = {}
                if manifest.get('new_class') and member_update.get('new_class'):
                    final_outcome['new_class'] = member_update['new_class']
                for field in ('new_skills', 'new_traits', 'new_equipments'):
                    names, value = manifest.get(field) or [], member_update.get(field)
                    if names and isinstance(value, dict):
                        final_outcome[field] = {name: value[name] for name in names if name in value}
                    elif names and isinstance(value, list):
                        final_outcome[field] = [row for row in value if (
                            row.get('name') if isinstance(row, dict) else str(row)
                        ) in names]
                journey.final_outcome = final_outcome
                journey.completed_at = _portable_utc_now()
                from plugins.adventurer_s1.services.adventurer_s1_service import (
                    invalidate_adventurer_detail_cache,
                )
                invalidate_adventurer_detail_cache(journey.adventurer_id)
            else:
                journey.miss_count = int(journey.miss_count or 0) + 1
        elif event_type == 'key' and fate_progress.get('outcome') == 'advanced':
            journey.key_event_count = int(journey.key_event_count or 0) + 1
            journey.chapter_index = int(journey.chapter_index or 1) + 1
            journey.miss_count = 0
            journey.current_clue = fate_progress.get('next_clue') or summary or journey.current_clue
            available_names = set(_available_dungeon_names())
            next_candidates = [
                str(name) for name in (context.get('next_candidate_dungeons') or [])
                if str(name) in available_names and str(name) != log.target_dungeon
            ][:3]
            journey.candidate_dungeons = next_candidates or _next_chapter_candidate_dungeons(
                journey.id,
                journey.chapter_index,
                log.id,
                log.target_dungeon,
            )
            final_required = int(current_app.config.get('ADVENTURER_S1_FATE_KEY_EVENTS_REQUIRED', 8))
            if journey.key_event_count >= final_required:
                journey.status = 'final_ready'
            from plugins.adventurer_s1.services.s1_fate_companion_service import (
                record_fate_companion_credits,
            )
            record_fate_companion_credits(
                members,
                expedition_log_id=log.id,
                fate_adventurer_id=journey.adventurer_id,
            )
        elif event_type == 'echo':
            journey.echo_count = int(journey.echo_count or 0) + 1
            journey.miss_count = int(journey.miss_count or 0) + 1
            if fate_progress.get('next_clue'):
                journey.current_clue = fate_progress['next_clue']
        else:
            journey.miss_count = int(journey.miss_count or 0) + 1
