from runtime_core.time import utc_now as _portable_utc_now
import json
import re
from datetime import datetime, timedelta
from flask import current_app, has_app_context
from sqlalchemy.orm.attributes import flag_modified
from utils.time_utils import to_utc_iso

__all__ = [
    '_EQUIPMENT_RARITY_PRIORITY',
    'EXPEDITION_EVENTS_MIN_COUNT',
    '_get_expedition_events_min_count',
    '_EXPEDITION_EVENT_FIELDNAME_PREFIX_PATTERN',
    '_log_parser_warning',
    '_split_structured_sections',
    '_parse_story_events',
    '_is_valid_expedition_result',
    '_build_expedition_validation_feedback',
    '_build_member_known_equipment_names',
    '_normalize_completion_check_payload',
    '_validate_completion_check',
    '_validate_expedition_result',
    'parse_deepseek_expedition_response',
    '_is_member_update_dead',
    '_build_member_update_evidence_text',
    '_build_expedition_visible_text',
    '_has_equipment_upgrade_signal',
    '_normalize_equipment_damage_stage',
    '_normalize_equipment_damage_updates_payload',
    '_normalize_new_equipment_payload',
    '_normalize_new_skills_payload',
    '_normalize_stored_to_warehouse_payload',
    '_extract_stored_to_warehouse_names',
    '_strip_member_update_new_traits_dirty_names',
    '_normalize_removed_name_list_payload',
    '_normalize_member_update_payload',
    '_normalize_member_updates_with_current_state',
    '_apply_pre_expedition_trait_merges',
    '_merge_upgraded_equipment_desc',
    '_normalize_equipment_rarity_value',
    '_pick_highest_equipment_rarity',
    '_apply_pre_expedition_equipment_upgrades',
    '_apply_pre_expedition_trait_recovery',
    '_merge_pre_expedition_store_items_into_updates',
    '_select_duplicate_new_equipment_owner',
    '_dedupe_duplicate_new_equipments',
    '_find_upgraded_equipment_source_name',
    '_count_keyword_occurrences',
    '_detect_implausible_extreme_zero_death_result',
    '_normalize_expedition_result_with_current_state',
    '_apply_equipment_damage_updates',
    '_apply_member_hp_update',
]

from plugins.adventurer.models import Adventurer
from plugins.adventurer.utils import (
    is_reserved_adventurer_item_name,
    merge_adventurer_text_fragments,
    merge_adventurer_upgraded_equipment_desc,
    normalize_adventurer_trait_entry,
    normalize_adventurer_trait_name_and_desc,
    sanitize_adventurer_name,
    split_adventurer_trait_name_suffix,
    split_name_desc_colon_suffix,
)
from plugins.adventurer.services.adventurer_config_service import (
    EQUIPMENT_DAMAGE_STAGE_INTACT,
    VALID_EQUIPMENT_RARITIES,
    VALID_EQUIPMENT_DAMAGE_STAGES,
    _build_ai_output_preview,
    _clean_compact_text,
    _clean_persisted_text,
    _coerce_float,
    _coerce_int,
    _extract_json_object_from_text,
    _safe_json_loads,
    _strip_markdown_code_fence,
    _to_clean_string_list,
)
from plugins.adventurer.services.adventurer_prompt_service import (
    _build_prepared_context_member_map,
    _normalize_consumed_equipments_payload,
    _normalize_used_equipments_payload,
    _strip_trailing_equipment_note,
)


# ── AI 响应结构解析 ────────────────────────────────────────────────────────────

_EQUIPMENT_RARITY_PRIORITY = {
    'common': 0,
    'rare': 1,
    'epic': 2,
    'legendary': 3,
}

# events 数组最低条数：低于该阈值视为 AI 把剧情压成几句话或把 summary 当 events 写。
# 通过 app.config['EXPEDITION_EVENTS_MIN_COUNT'] 可覆盖，0 表示关闭校验（测试用）。
EXPEDITION_EVENTS_MIN_COUNT = 4


def _get_expedition_events_min_count() -> int:
    """从配置读取 events 最小条数阈值。
    无 Flask 上下文时返回 0（关闭校验，便于单元测试直接调用 `_validate_expedition_result` 校验其它分支）。
    生产环境运行在 request/task context 内，会从 config 读取（默认 EXPEDITION_EVENTS_MIN_COUNT=4）。"""
    if has_app_context():
        try:
            return int(current_app.config.get('EXPEDITION_EVENTS_MIN_COUNT', EXPEDITION_EVENTS_MIN_COUNT))
        except (TypeError, ValueError):
            return EXPEDITION_EVENTS_MIN_COUNT
    return 0


# 拦截 AI 把 JSON/Markdown 字段名（如 'summary: ...' / '**summary**: ...' / '总结: ...'）当作剧情正文写进 events 的情况。
# 兼容英文/中文冒号、可选的 markdown 加粗符号与首部空格。
_EXPEDITION_EVENT_FIELDNAME_PREFIX_PATTERN = re.compile(
    r'^\s*\**\s*(?:summary|events|result_summary|总结|摘要|本次总结|内容总结|剧情总结)\s*\**\s*[:：]',
    re.IGNORECASE,
)


def _strip_expedition_event_field_prefix(event_text: str) -> str:
    """剥离 AI 误写进 events 的 JSON/Markdown 字段名前缀，保留其后剧情正文。"""
    if not isinstance(event_text, str):
        return event_text
    return _EXPEDITION_EVENT_FIELDNAME_PREFIX_PATTERN.sub('', event_text, count=1).strip()


def _sanitize_expedition_story_events(events: object) -> list:
    """修复 events 列表里带字段名前缀的条目；剥离后为空则丢弃。"""
    if not isinstance(events, list):
        return []
    sanitized: list = []
    for event_text in events:
        if not isinstance(event_text, str):
            sanitized.append(event_text)
            continue
        stripped = _strip_expedition_event_field_prefix(event_text)
        if stripped:
            sanitized.append(stripped)
    return sanitized


def _log_parser_warning(message: str, *args) -> None:
    if has_app_context():
        current_app.logger.warning(message, *args)

def _split_structured_sections(text: str, *, require_all_headers: bool = True) -> dict[str, str]:
    """解析带 ### 标题的结构化输出。

    Args:
        text: AI 输出文本
        require_all_headers: 为 True 时三个标题必须齐全才返回，为 False 时允许部分标题缺失。
    """
    headers = ['规则判定', '剧情描述', '状态更新']
    pattern = re.compile(r'^\s*###\s*(规则判定|剧情描述|状态更新)\s*(.*?)$', re.MULTILINE)
    matches = list(pattern.finditer(text or ''))
    sections = {}

    for index, match in enumerate(matches):
        header = match.group(1)
        inline_content = match.group(2).strip()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        if inline_content:
            section_text = inline_content + '\n' + section_text
        sections[header] = section_text

    if require_all_headers:
        return sections if all(header in sections for header in headers) else {}
    return sections


def _parse_story_events(story_text: str) -> list[str]:
    """将剧情描述段落解析为事件列表。"""
    cleaned = _strip_markdown_code_fence(story_text)
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    events = []

    for line in lines:
        normalized = re.sub(r'^\s*(?:\d+[.)、]|[-*•])\s*', '', line).strip()
        if normalized:
            events.append(normalized)

    if len(events) <= 1:
        paragraphs = [part.strip() for part in re.split(r'\n\s*\n', cleaned) if part.strip()]
        if len(paragraphs) > len(events):
            events = paragraphs

    if len(events) <= 1:
        sentences = [s.strip() for s in re.split(r'[。！？]', cleaned) if s.strip()]
        if len(sentences) > len(events):
            events = sentences

    return events


def _is_valid_expedition_result(result_data: dict | None) -> bool:
    """校验探险结果结构是否满足后续处理要求。"""
    if not isinstance(result_data, dict):
        return False

    summary = result_data.get('summary')
    events = result_data.get('events')
    member_updates = result_data.get('member_updates')
    return isinstance(summary, str) and isinstance(events, list) and isinstance(member_updates, list)


def _build_expedition_validation_feedback(errors: list[str], limit: int = 3) -> str:
    """将结构化校验错误压缩成下一轮重试可用的短提示。"""
    compact_errors = [
        _clean_compact_text(error, 160)
        for error in (errors or [])
        if _clean_compact_text(error, 160)
    ]
    if not compact_errors:
        return ''

    feedback_lines = compact_errors[:max(1, _coerce_int(limit, 3))]
    return "上次返回的 JSON 结构仍不合格，请只修正这些字段并重新完整输出：\n" + "\n".join(
        f"- {line}" for line in feedback_lines
    )


def _build_member_known_equipment_names(member: Adventurer | None, update: dict) -> set[str]:
    """收集成员当前已知装备名，兼容原有装备与本次新增物品。"""
    known_names = set((member.equipment or {}).keys()) if member else set()
    known_names.update(_normalize_new_equipment_payload(update.get('new_equipments')).keys())
    return known_names


def _normalize_completion_check_payload(completion_check: object) -> object:
    """清洗 AI 返回的隐藏 completion_check 字段，供校验与内部留档复用。"""
    if not isinstance(completion_check, dict):
        return completion_check

    normalized: dict[str, object] = {}
    for field_name in ('story_finished', 'core_resolved', 'exit_resolved'):
        value = completion_check.get(field_name)
        if isinstance(value, bool):
            normalized[field_name] = value
            continue
        if isinstance(value, str):
            lowered_value = value.strip().lower()
            if lowered_value == 'true':
                normalized[field_name] = True
            elif lowered_value == 'false':
                normalized[field_name] = False

    ending_type = _clean_compact_text(completion_check.get('ending_type'), 40)
    if ending_type:
        normalized['ending_type'] = ending_type

    raw_gain_loss_entries = completion_check.get('member_gain_loss')
    if isinstance(raw_gain_loss_entries, list):
        normalized_entries = []
        for entry in raw_gain_loss_entries:
            if not isinstance(entry, dict):
                normalized_entries.append(entry)
                continue
            normalized_entries.append({
                'id': _coerce_int(entry.get('id'), -1),
                'gained': _to_clean_string_list(entry.get('gained'), 20, 80),
                'lost': _to_clean_string_list(entry.get('lost'), 20, 80),
            })
        normalized['member_gain_loss'] = normalized_entries

    return normalized


def _validate_completion_check(
    completion_check: object,
    expected_member_ids: set[int],
    *,
    require_core_resolved: bool = True,
) -> list[str]:
    """严格校验隐藏 completion_check 字段。

    三个状态位都必须是布尔值；默认全部要求为 True。S1 可通过
    ``require_core_resolved=False`` 表示故事与离场已闭环、核心仍留存。
    ending_type 与逐人成员得失仍始终必填。
    """
    errors: list[str] = []
    if not isinstance(completion_check, dict):
        return ["缺少 completion_check 或 completion_check 不是对象"]

    for field_name in ('story_finished', 'core_resolved', 'exit_resolved'):
        value = completion_check.get(field_name)
        if not isinstance(value, bool):
            errors.append(f"completion_check.{field_name} 缺少或不是布尔值")
        elif value is not True and (field_name != 'core_resolved' or require_core_resolved):
            errors.append(f"completion_check.{field_name} 必须为 true（故事须完整收束）")

    if not _clean_compact_text(completion_check.get('ending_type'), 40):
        errors.append("completion_check 缺少 ending_type")

    member_gain_loss = completion_check.get('member_gain_loss')
    if not isinstance(member_gain_loss, list):
        errors.append("completion_check.member_gain_loss 缺少或不是数组")
        return errors

    seen_member_ids: set[int] = set()
    for index, entry in enumerate(member_gain_loss):
        if not isinstance(entry, dict):
            errors.append(f"completion_check.member_gain_loss[{index}] 不是对象")
            continue

        member_id = _coerce_int(entry.get('id'), -1)
        if member_id < 0:
            errors.append(f"completion_check.member_gain_loss[{index}] 缺少合法 id")
            continue
        if member_id in seen_member_ids:
            errors.append(f"completion_check.member_gain_loss[{index}] 的成员 id={member_id} 重复")
        seen_member_ids.add(member_id)

        if member_id not in expected_member_ids:
            errors.append(f"completion_check.member_gain_loss[{index}] 的成员 id={member_id} 不属于本次出发小队")

        for field_name in ('gained', 'lost'):
            if not isinstance(entry.get(field_name), list):
                errors.append(f"completion_check.member_gain_loss[{index}].{field_name} 不是数组")

    missing_member_ids = expected_member_ids - seen_member_ids
    if missing_member_ids:
        errors.append(
            f"completion_check.member_gain_loss 未覆盖全部小队成员，缺少 id={sorted(missing_member_ids)}"
        )

    return errors


def _validate_expedition_result(
    result_data: dict | None,
    members: list[Adventurer],
    *,
    strict_consumed_match: bool = False,
) -> tuple[bool, list[str], list[str]]:
    """校验探险结果结构，返回 (是否通过, 错误列表, warning 列表)。"""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(result_data, dict):
        return False, ["状态更新不是合法 JSON 对象"], warnings

    summary = result_data.get('summary')
    events = result_data.get('events')
    member_updates = result_data.get('member_updates')
    if not isinstance(summary, str) or not summary.strip():
        errors.append("缺少 summary 或 summary 不是非空字符串")
    if not isinstance(events, list):
        errors.append("缺少 events 或 events 不是数组")
    else:
        # events 数量过少时，AI 多半把整段剧情压成一两条或直接把 summary 当事件写进来；
        # 配置 EXPEDITION_EVENTS_MIN_COUNT=0 可关闭该校验（测试用）。
        min_events = _get_expedition_events_min_count()
        if min_events > 0 and len(events) < min_events:
            errors.append(
                f"events 条数过少（{len(events)} 条），期望至少 {min_events} 条；"
                f"不要把整段剧情压成一两条，也不要把 summary 内容当作 events 写进去"
            )
        # 拒绝以 JSON/Markdown 字段名前缀开头的 event（AI 偷懒直接把 'summary: ...' 当事件写）
        for index, ev in enumerate(events):
            if not isinstance(ev, str):
                continue
            if _EXPEDITION_EVENT_FIELDNAME_PREFIX_PATTERN.match(ev):
                errors.append(
                    f"events[{index}] 以字段名前缀（如 `summary:` / `总结:` / `events:` / `**summary**:`）开头，"
                    f"不允许把 JSON 字段名或 markdown 标题当作剧情正文写进 events"
                )
    if not isinstance(member_updates, list):
        errors.append("缺少 member_updates 或 member_updates 不是数组")
        return False, errors, warnings

    member_id_map = {int(member.id): member for member in (members or [])}
    expected_member_ids = set(member_id_map.keys())
    # completion_check 是给后端做"故事是否真正写完"的隐藏收束校验：缺失/结构非法/布尔位不全为真，
    # 都直接进 errors 触发重试，避免把中断稿、含糊稿、未收束稿当成正常结算落地。
    errors.extend(
        _validate_completion_check(result_data.get('completion_check'), expected_member_ids)
    )
    seen_member_ids = set()
    if len(member_updates) != len(expected_member_ids):
        errors.append(
            f"member_updates 数量不对，期望 {len(expected_member_ids)} 个成员，实际 {len(member_updates)} 个"
        )

    for index, update in enumerate(member_updates):
        if not isinstance(update, dict):
            errors.append(f"member_updates[{index}] 不是对象")
            continue

        member_id = _coerce_int(update.get('id'), -1)
        if member_id < 0:
            errors.append(f"member_updates[{index}] 缺少合法 id")
            continue
        if member_id in seen_member_ids:
            errors.append(f"member_updates[{index}] 的成员 id={member_id} 重复")
        seen_member_ids.add(member_id)

        if member_id not in expected_member_ids:
            errors.append(f"member_updates[{index}] 的成员 id={member_id} 不属于本次出发小队")
            continue

        for field_name in ('remaining_hp', 'score'):
            if update.get(field_name) in (None, ''):
                errors.append(f"member_updates[{index}] 缺少 {field_name}")

        if update.get('remaining_hp') not in (None, ''):
            try:
                int(update.get('remaining_hp'))
            except (TypeError, ValueError):
                errors.append(f"member_updates[{index}].remaining_hp 不是合法整数")

        if update.get('score') not in (None, ''):
            try:
                float(update.get('score'))
            except (TypeError, ValueError):
                errors.append(f"member_updates[{index}].score 不是合法数值")

        new_equipments = update.get('new_equipments') or {}
        if not isinstance(new_equipments, dict):
            errors.append(f"member_updates[{index}].new_equipments 不是对象")
        else:
            for equipment_name, equipment_data in new_equipments.items():
                clean_equipment_name = _clean_compact_text(equipment_name, 120) or f"第{index}位成员的新装备"
                if not isinstance(equipment_data, dict):
                    errors.append(f"{clean_equipment_name} 不是合法对象，缺少 type/desc/rarity")
                    continue

                equipment_type = _clean_compact_text(equipment_data.get('type'), 40)
                equipment_desc = _clean_compact_text(
                    equipment_data.get('desc') or equipment_data.get('description'),
                    240
                )
                equipment_rarity = _clean_compact_text(equipment_data.get('rarity'), 40).lower()
                if not equipment_type:
                    errors.append(f"{clean_equipment_name} 缺少 type")
                if not equipment_desc:
                    errors.append(f"{clean_equipment_name} 缺少 desc")
                if not equipment_rarity:
                    errors.append(f"{clean_equipment_name} 缺少 rarity")
                elif equipment_rarity not in VALID_EQUIPMENT_RARITIES:
                    errors.append(
                        f"{clean_equipment_name} 的 rarity={equipment_rarity} 不合法，只允许 common/rare/epic/legendary"
                    )

        member = member_id_map.get(member_id)
        available_names = _build_member_known_equipment_names(member, update)
        for field_name, normalizer in (
            ('used_equipments', _normalize_used_equipments_payload),
            ('consumed_equipments', _normalize_consumed_equipments_payload),
        ):
            for item_name in normalizer(
                update.get(field_name),
                member.equipment if member else None
            ):
                if item_name in available_names:
                    continue
                if strict_consumed_match:
                    errors.append(f"member_updates[{index}].{field_name} 中的 {item_name} 不在成员原有随身物品里")
                elif available_names:
                    warnings.append(
                        f"member_updates[{index}].{field_name} 中的 {item_name} 未在成员原有随身物品里命中"
                    )

    missing_ids = expected_member_ids - seen_member_ids
    extra_ids = seen_member_ids - expected_member_ids
    if missing_ids:
        errors.append(f"member_updates 未覆盖全部小队成员，缺少 id={sorted(missing_ids)}")
    if extra_ids:
        errors.append(f"member_updates 出现了不属于本次小队的成员 id={sorted(extra_ids)}")

    return len(errors) == 0, errors, warnings


def parse_deepseek_expedition_response(response_text: str) -> dict | None:
    """解析 DeepSeek 探险结果，兼容新分段格式和旧 JSON 格式。"""
    cleaned = _strip_markdown_code_fence(response_text)
    sections = _split_structured_sections(cleaned)

    if sections:
        status_json_str = _extract_json_object_from_text(sections.get('状态更新', ''))
        if status_json_str:
            result_data = _safe_json_loads(status_json_str)
            if isinstance(result_data, dict):
                story_events = _parse_story_events(sections.get('剧情描述', ''))
                if story_events:
                    result_data['events'] = story_events
                if _is_valid_expedition_result(result_data):
                    return result_data

    legacy_json_str = _extract_json_object_from_text(cleaned)
    if not legacy_json_str:
        return None

    result_data = _safe_json_loads(legacy_json_str)
    if _is_valid_expedition_result(result_data):
        return result_data
    return None


# ── 成员结算状态判断 ──────────────────────────────────────────────────────────

def _is_member_update_dead(update: dict) -> bool:
    """判断成员结算是否表示阵亡，兼容 is_dead/status/remaining_hp 多种格式。"""
    if not isinstance(update, dict):
        return False
    if update.get('is_dead', False):
        return True

    status = str(update.get('status') or '').strip().lower()
    if status == 'dead':
        return True

    remaining_hp = update.get('remaining_hp')
    if remaining_hp not in (None, '') and _coerce_int(remaining_hp, 1) <= 0:
        return True

    return False


# ── 证据文本 & 信号检测 ────────────────────────────────────────────────────────

def _build_member_update_evidence_text(
    member_name: str,
    update: dict,
    result_data: dict
) -> str:
    """汇总成员在正文与传记中的证据文本，用于消耗/升级判定。"""
    history_text = _clean_compact_text(update.get('history_story_append'), 600)
    summary_text = _clean_compact_text(result_data.get('summary'), 480)
    relevant_events = []
    for event_text in result_data.get('events') or []:
        event_text = _clean_compact_text(event_text, 600)
        if member_name and member_name in event_text:
            relevant_events.append(event_text)
    return ' '.join([history_text, summary_text, *relevant_events])


def _build_expedition_visible_text(result_data: dict) -> str:
    """拼接玩家可见摘要与事件文本，供一致性校验复用。"""
    summary_text = _clean_compact_text((result_data or {}).get('summary'), 400)
    event_texts = [
        _clean_compact_text(event_text, 400)
        for event_text in ((result_data or {}).get('events') or [])
        if event_text
    ]
    return ' '.join([summary_text, *event_texts])


def _has_equipment_upgrade_signal(*texts: str) -> bool:
    """判断文本里是否出现了明确的装备升级/替换信号。"""
    keywords = ('升级', '强化', '改良', '进阶', '淬炼', '重铸', '改造', '换下', '替换', '取代')
    combined_text = ' '.join(_clean_compact_text(text, 400) for text in texts if text)
    return any(keyword in combined_text for keyword in keywords)


# ── 结算数据规范化 ────────────────────────────────────────────────────────────

def _normalize_equipment_damage_stage(value: object, default: str = '') -> str:
    """统一装备破损阶段，只接受三档中文值。"""
    stage = _clean_compact_text(value, 20)
    if stage in VALID_EQUIPMENT_DAMAGE_STAGES:
        return stage
    return default if default in VALID_EQUIPMENT_DAMAGE_STAGES else ''


def _normalize_equipment_damage_updates_payload(equipment_damage_updates: object) -> list[dict]:
    """统一兼容装备破损结算格式。"""
    raw_updates = []
    if isinstance(equipment_damage_updates, list):
        raw_updates = equipment_damage_updates
    elif isinstance(equipment_damage_updates, dict):
        for equipment_name, damage_data in equipment_damage_updates.items():
            if isinstance(damage_data, dict):
                entry = dict(damage_data)
            else:
                entry = {'damage_stage': damage_data}
            entry['name'] = equipment_name
            raw_updates.append(entry)
    else:
        return []

    normalized_updates = []
    seen_names = set()
    for raw_update in raw_updates:
        if not isinstance(raw_update, dict):
            continue

        equipment_name = _clean_compact_text(
            raw_update.get('name') or raw_update.get('equipment_name'),
            120
        )
        if not equipment_name or equipment_name in seen_names:
            continue

        damage_stage = _normalize_equipment_damage_stage(
            raw_update.get('damage_stage') or raw_update.get('stage')
        )
        if not damage_stage:
            continue

        normalized_update = {
            'name': equipment_name,
            'damage_stage': damage_stage,
        }
        damage_note = _clean_compact_text(
            raw_update.get('damage_note') or raw_update.get('note'),
            240
        )
        if damage_note:
            normalized_update['damage_note'] = damage_note

        normalized_updates.append(normalized_update)
        seen_names.add(equipment_name)

    return normalized_updates


def _normalize_new_equipment_payload(new_equipments: object) -> dict:
    """兼容旧格式的 new_equipments，把字符串描述统一转成对象结构。"""
    if not isinstance(new_equipments, dict):
        return {}

    normalized_equipments = {}
    for equipment_name, equipment_data in new_equipments.items():
        # 默认信任 AI 原样输出，保留 "(防护毒素)"、"(已镶嵌)"、"(3份)" 这种合法变体后缀。
        # 仅当名字超过 AdventurerItem.name VARCHAR(100) 上限会直接撑爆 INSERT 时才剥离尾部括号;
        # 剥离后仍超长再硬截到 100，对齐 DB schema 上限避免整事务回滚。
        compact_name = _clean_compact_text(equipment_name, 200)
        if not compact_name:
            continue
        if len(compact_name) <= 100:
            clean_name = compact_name
        else:
            stripped_name = _strip_trailing_equipment_note(compact_name)
            if len(stripped_name) <= 100:
                clean_name = stripped_name
            else:
                clean_name = _clean_compact_text(stripped_name, 100)
        if not clean_name:
            continue
        # 再额外处理 AI 偶发的 "装备名: 描述" 写法：把冒号后描述折回 desc，避免脏 key 直显前台。
        colon_base, colon_suffix = split_name_desc_colon_suffix(clean_name)
        if colon_base and colon_base != clean_name:
            clean_name = colon_base
        else:
            colon_suffix = ''
        if is_reserved_adventurer_item_name(clean_name):
            _log_parser_warning("探险结算忽略异常 new_equipments 键名: %s", clean_name)
            continue

        if isinstance(equipment_data, dict):
            raw_uses_left = equipment_data.get('uses_left')
            normalized_entry = {
                'desc': _clean_persisted_text(
                    merge_adventurer_text_fragments(
                        colon_suffix,
                        equipment_data.get('desc') or equipment_data.get('description'),
                    )
                ),
                'type': _clean_compact_text(equipment_data.get('type'), 40) or 'consumable',
                'rarity': _clean_compact_text(equipment_data.get('rarity'), 40) or 'common',
                'uses_left': None if raw_uses_left in (None, '') else _coerce_int(raw_uses_left, 0)
            }
            damage_stage = _normalize_equipment_damage_stage(equipment_data.get('damage_stage'))
            damage_note = _clean_persisted_text(equipment_data.get('damage_note'))
            if damage_stage and damage_stage != EQUIPMENT_DAMAGE_STAGE_INTACT:
                normalized_entry['damage_stage'] = damage_stage
                if damage_note:
                    normalized_entry['damage_note'] = damage_note
            if 'evolution_stage' in equipment_data:
                normalized_entry['evolution_stage'] = max(
                    0, _coerce_int(equipment_data.get('evolution_stage'), 0),
                )
            normalized_equipments[clean_name] = normalized_entry
            continue

        normalized_equipments[clean_name] = {
            'desc': _clean_persisted_text(
                merge_adventurer_text_fragments(colon_suffix, equipment_data)
            ),
            'type': 'consumable',
            'rarity': 'common',
            'uses_left': None
        }

    return normalized_equipments


def _normalize_new_skills_payload(
    new_skills: object,
    *,
    split_semantic_name_desc: bool = True,
) -> list[dict]:
    """清洗 AI 返回的 new_skills，统一输出为 {'name': str, 'desc': str} 列表。

    S1 会显式关闭语义拆分，只保留空白与数据库长度边界清洗，并把名称/描述纠正交给
    具备完整剧情上下文的 Observer；旧探险调用保留既有兼容行为。
    同时兼容旧的纯字符串格式（`['技能名']`）和名称映射格式
    （`{'技能名': {'desc': '...'}}`）。
    """
    if isinstance(new_skills, dict):
        skill_entries = []
        for skill_name, skill_data in new_skills.items():
            if isinstance(skill_data, dict):
                entry = dict(skill_data)
                entry.setdefault('name', skill_name)
            else:
                entry = {'name': skill_name, 'desc': skill_data}
            skill_entries.append(entry)
    elif isinstance(new_skills, list):
        skill_entries = new_skills
    else:
        return []

    normalized = []
    for raw_skill in skill_entries:
        if isinstance(raw_skill, dict):
            raw_name = raw_skill.get('name')
            raw_desc = raw_skill.get('desc')
        elif isinstance(raw_skill, str):
            raw_name = raw_skill
            raw_desc = None
        else:
            continue
        if not raw_name:
            continue

        compact_name = _clean_compact_text(raw_name, 200)
        if not compact_name:
            continue
        if split_semantic_name_desc:
            stripped_name = compact_name
            bracket_suffix = ''
            # 短括号后缀可能是合法技能版本；只在名称已经越过 DB 上限时兼容旧脏数据。
            if len(compact_name) > 100:
                stripped_name = _strip_trailing_equipment_note(compact_name)
                if not stripped_name:
                    continue
                bracket_suffix = compact_name[len(stripped_name):].strip()
                if (
                    bracket_suffix
                    and bracket_suffix[0] in '（(['
                    and bracket_suffix[-1] in '）)]'
                ):
                    bracket_suffix = bracket_suffix[1:-1].strip()
            clean_name = _clean_compact_text(stripped_name, 100)
            colon_base, colon_suffix = split_name_desc_colon_suffix(clean_name)
            if colon_base and colon_base != clean_name:
                clean_name = colon_base
                merged_desc = merge_adventurer_text_fragments(colon_suffix, raw_desc)
            else:
                merged_desc = raw_desc
            if bracket_suffix:
                merged_desc = merge_adventurer_text_fragments(
                    bracket_suffix,
                    merged_desc,
                )
        else:
            clean_name = _clean_compact_text(compact_name, 100)
            merged_desc = raw_desc
        if not clean_name:
            continue

        normalized.append({
            'name': clean_name,
            'desc': _clean_persisted_text(merged_desc),
        })
    return normalized


def _normalize_stored_to_warehouse_payload(stored_to_warehouse: object) -> list[dict]:
    """兼容 stored_to_warehouse 的字符串和对象格式，统一转成带 name 的对象列表。"""
    if not isinstance(stored_to_warehouse, list):
        return []

    normalized_entries = []
    seen_names = set()

    for raw_entry in stored_to_warehouse:
        item_name = ''
        item_note = ''

        if isinstance(raw_entry, dict):
            item_name = _clean_compact_text(raw_entry.get('name'), 120)
            item_note = _clean_persisted_text(raw_entry.get('note'))
        else:
            item_name = _clean_compact_text(raw_entry, 120)

        if not item_name or item_name in seen_names:
            continue
        if is_reserved_adventurer_item_name(item_name):
            _log_parser_warning("探险结算忽略异常 stored_to_warehouse 物品名: %s", item_name)
            continue

        seen_names.add(item_name)
        entry = {'name': item_name}
        if item_note:
            entry['note'] = item_note
        normalized_entries.append(entry)

    return normalized_entries


def _extract_stored_to_warehouse_names(stored_to_warehouse: object) -> list[str]:
    """提取 stored_to_warehouse 中的物品名，供去重与比对逻辑使用。"""
    return [entry['name'] for entry in _normalize_stored_to_warehouse_payload(stored_to_warehouse)]


def _strip_member_update_new_traits_dirty_names(new_traits):
    """剥离 AI 在 new_traits key/name 里混进的括号/冒号描述脏写法，保留原形状（dict/list-of-dict/list-of-str）。

    只做基础剥离，不依赖 trait_dict 也不做 desc fallback；最终写库前的完整 normalize 仍由
    expedition_runner 调用 _normalize_member_new_traits_payload 完成。这层的目标是让 full_log
    里展示给前端的 new_traits 也是干净的。
    """
    if not new_traits:
        return new_traits

    def _strip_pair(raw_name, raw_desc):
        clean_name, clean_desc = normalize_adventurer_trait_name_and_desc(
            trait_name=raw_name,
            desc=raw_desc,
            trait_dict=None,
        )
        return clean_name, clean_desc

    if isinstance(new_traits, list):
        cleaned_list = []
        for entry in new_traits:
            if isinstance(entry, dict):
                clean_name, clean_desc = _strip_pair(entry.get('name'), entry.get('desc'))
                if not clean_name:
                    continue
                new_entry = dict(entry)
                new_entry['name'] = clean_name
                if clean_desc:
                    new_entry['desc'] = clean_desc
                cleaned_list.append(new_entry)
            elif isinstance(entry, str):
                clean_name, _clean_desc = _strip_pair(entry, None)
                if clean_name:
                    cleaned_list.append(clean_name)
        return cleaned_list

    if isinstance(new_traits, dict):
        cleaned_dict = {}
        for raw_name, raw_desc in new_traits.items():
            clean_name, clean_desc = _strip_pair(raw_name, raw_desc)
            if not clean_name:
                continue
            cleaned_dict[clean_name] = clean_desc if clean_desc else (raw_desc or '')
        return cleaned_dict

    return new_traits


def _normalize_removed_name_list_payload(removed_list: object) -> list[str]:
    """清洗 AI 返回的 removed_traits / removed_skills 名单，统一为字符串列表。

    主要处理：
    - 类型兼容（list[str] / list[dict{name}]）
    - 剥离 AI 偶发的 `名(说明)` / `名（说明）` / `名: 描述` 尾部脏写法（保守）
    - 去空、去重，长度截断到 200 字
    """
    if not isinstance(removed_list, list):
        return []
    normalized = []
    seen = set()
    for raw in removed_list:
        raw_name = raw.get('name') if isinstance(raw, dict) else raw
        if not isinstance(raw_name, str):
            continue
        compact = _clean_compact_text(raw_name, 200)
        if not compact:
            continue
        # 与 new_traits 同源：拆 `特质名(长描述)` / 未闭合括号 / `名: 描述` 脏写法
        clean_name, _suffix_desc = split_adventurer_trait_name_suffix(compact)
        if not clean_name:
            colon_base, _colon_suffix = split_name_desc_colon_suffix(compact)
            clean_name = colon_base if (colon_base and colon_base != compact) else compact
        clean_name = _clean_compact_text(clean_name, 100)
        if not clean_name or clean_name in seen:
            continue
        seen.add(clean_name)
        normalized.append(clean_name)
    return normalized


# AI 偶发把 member_update 的 JSON 字段名（removed_traits / used_skills 等）误写进 new_traits。
_MEMBER_UPDATE_RESERVED_FIELD_NAMES = frozenset({
    'removed_traits',
    'removed_skills',
    'used_skills',
    'used_equipments',
    'consumed_equipments',
    'stored_to_warehouse',
    'history_story_append',
})
_MEMBER_UPDATE_LIST_HOIST_FIELDS = frozenset({
    'removed_traits',
    'removed_skills',
    'used_skills',
    'used_equipments',
    'consumed_equipments',
    'stored_to_warehouse',
})


def _normalize_simple_name_list(value: object) -> list[str]:
    """把 AI 各种脏写法统一成字符串名单（used_* / removed_* 提升时用）。"""
    if value is None:
        return []
    if isinstance(value, str):
        name = _clean_compact_text(value, 200)
        return [name] if name else []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str):
            name = _clean_compact_text(item, 200)
        elif isinstance(item, dict):
            name = _clean_compact_text(item.get('name'), 200)
        else:
            continue
        if name:
            names.append(name)
    return names


def _merge_hoisted_name_list_field(update: dict, field_name: str, value: object) -> None:
    if field_name in ('removed_traits', 'removed_skills'):
        incoming = _normalize_removed_name_list_payload(_normalize_simple_name_list(value))
    else:
        incoming = _normalize_simple_name_list(value)
    if not incoming:
        return

    existing = update.get(field_name)
    if not existing:
        update[field_name] = incoming
        return
    if isinstance(existing, list):
        merged = list(existing)
        for name in incoming:
            if name not in merged:
                merged.append(name)
        update[field_name] = merged
        return
    if isinstance(existing, str):
        merged = [existing]
        for name in incoming:
            if name not in merged:
                merged.append(name)
        update[field_name] = merged
        return
    update[field_name] = incoming


def _merge_hoisted_history_story_append(update: dict, value: object) -> None:
    if isinstance(value, str):
        text = _clean_compact_text(value, 600)
    elif isinstance(value, list):
        text = _clean_compact_text(' '.join(str(item).strip() for item in value if item), 600)
    else:
        return
    if not text:
        return
    existing = _clean_compact_text(update.get('history_story_append'), 600)
    if not existing:
        update['history_story_append'] = text
    elif text not in existing:
        update['history_story_append'] = f'{existing}\n{text}'


def _hoist_misplaced_member_fields_from_new_traits(update: dict) -> None:
    """把误写进 new_traits 的结算字段名提升回 member_update 顶层。"""
    new_traits = update.get('new_traits')
    if new_traits is None:
        return

    if isinstance(new_traits, dict):
        remaining = {}
        for key, value in new_traits.items():
            field_name = _clean_compact_text(key, 100)
            if field_name in _MEMBER_UPDATE_RESERVED_FIELD_NAMES:
                if field_name in _MEMBER_UPDATE_LIST_HOIST_FIELDS:
                    _merge_hoisted_name_list_field(update, field_name, value)
                elif field_name == 'history_story_append':
                    _merge_hoisted_history_story_append(update, value)
                continue
            remaining[key] = value
        update['new_traits'] = remaining
        return

    if isinstance(new_traits, list):
        # AI 偶发把字段名与取值交替写成纯字符串列表，需在原列表上先配对提升。
        if new_traits and all(isinstance(entry, str) for entry in new_traits):
            consumed_indices: set[int] = set()
            for index, entry in enumerate(new_traits):
                if index in consumed_indices:
                    continue
                field_name = _clean_compact_text(entry, 100)
                if field_name not in _MEMBER_UPDATE_RESERVED_FIELD_NAMES:
                    continue
                if index + 1 >= len(new_traits):
                    consumed_indices.add(index)
                    continue
                next_value = new_traits[index + 1]
                next_name = _clean_compact_text(next_value, 100) if isinstance(next_value, str) else ''
                if isinstance(next_value, str) and next_name not in _MEMBER_UPDATE_RESERVED_FIELD_NAMES:
                    if field_name in _MEMBER_UPDATE_LIST_HOIST_FIELDS:
                        _merge_hoisted_name_list_field(update, field_name, next_value)
                    elif field_name == 'history_story_append':
                        _merge_hoisted_history_story_append(update, next_value)
                    consumed_indices.add(index)
                    consumed_indices.add(index + 1)
                else:
                    consumed_indices.add(index)
            if consumed_indices:
                new_traits = [
                    entry for idx, entry in enumerate(new_traits)
                    if idx not in consumed_indices
                ]

        remaining = []
        for entry in new_traits:
            if isinstance(entry, dict):
                field_name = _clean_compact_text(entry.get('name'), 100)
                if field_name in _MEMBER_UPDATE_RESERVED_FIELD_NAMES:
                    if field_name in _MEMBER_UPDATE_LIST_HOIST_FIELDS:
                        payload = entry.get('desc')
                        if payload in (None, '') and entry.get('value') not in (None, ''):
                            payload = entry.get('value')
                        if payload in (None, '') and entry.get('items') not in (None, ''):
                            payload = entry.get('items')
                        _merge_hoisted_name_list_field(update, field_name, payload)
                    elif field_name == 'history_story_append':
                        _merge_hoisted_history_story_append(update, entry.get('desc'))
                    continue
            elif isinstance(entry, str):
                field_name = _clean_compact_text(entry, 100)
                if field_name in _MEMBER_UPDATE_RESERVED_FIELD_NAMES:
                    continue
            remaining.append(entry)

        update['new_traits'] = remaining


def _swap_misplaced_equipment_and_traits(update: dict) -> None:
    """就地纠正 AI 把装备误放进 new_traits 或把特质误放进 new_equipments 的情况。

    判定规则：
    - new_traits 中带 type（装备类型如 armor/weapon/accessory/consumable/material）且带 rarity 的条目 → 迁到 new_equipments
    - new_equipments 中 type 包含特质关键词（特质/trait/性格/天赋）的条目 → 迁到 new_traits
    - 同名碰撞时保留 new_equipments 中原有条目，丢弃迁移条目
    """
    _EQUIPMENT_TYPE_SIGNALS = {'armor', 'weapon', 'accessory', 'consumable', 'material', '护甲', '武器', '防具', '饰品', '消耗品', '材料'}
    _TRAIT_TYPE_SIGNALS = {'特质', 'trait', '性格', '天赋', '特性'}

    new_traits = update.get('new_traits')
    new_equipments = update.get('new_equipments')

    if new_traits is None and new_equipments is None:
        return

    # ── 从 new_traits 中迁出误放的装备 ──
    traits_to_migrate = []  # (name, equipment_data)
    remaining_traits = None

    if isinstance(new_traits, list) and isinstance(new_equipments, dict):
        remaining = []
        for entry in new_traits:
            if isinstance(entry, dict):
                entry_type = str(entry.get('type', '')).lower().strip()
                entry_rarity = entry.get('rarity')
                if entry_type in _EQUIPMENT_TYPE_SIGNALS and entry_rarity:
                    name = entry.get('name', '')
                    if name:
                        equip_data = {k: v for k, v in entry.items() if k != 'name'}
                        traits_to_migrate.append((name, equip_data))
                        continue
            remaining.append(entry)
        remaining_traits = remaining
    elif isinstance(new_traits, dict):
        remaining = {}
        for name, value in new_traits.items():
            if isinstance(value, dict):
                entry_type = str(value.get('type', '')).lower().strip()
                entry_rarity = value.get('rarity')
                if entry_type in _EQUIPMENT_TYPE_SIGNALS and entry_rarity:
                    traits_to_migrate.append((name, value))
                    continue
            remaining[name] = value
        remaining_traits = remaining

    if remaining_traits is not None:
        update['new_traits'] = remaining_traits

    for name, equip_data in traits_to_migrate:
        if name not in new_equipments:
            new_equipments[name] = equip_data

    # ── 从 new_equipments 中迁出误放的特质 ──
    new_equipments = update.get('new_equipments', {})
    if isinstance(new_equipments, dict):
        traits_from_equips = []
        remaining_equips = {}
        for name, value in new_equipments.items():
            if isinstance(value, dict):
                entry_type = str(value.get('type', '')).lower().strip()
                if entry_type in _TRAIT_TYPE_SIGNALS:
                    trait_entry = {'name': name}
                    if value.get('desc'):
                        trait_entry['desc'] = value['desc']
                    traits_from_equips.append(trait_entry)
                    continue
            remaining_equips[name] = value

        if traits_from_equips:
            update['new_equipments'] = remaining_equips
            current_traits = update.get('new_traits', [])
            if isinstance(current_traits, list):
                current_traits.extend(traits_from_equips)
                update['new_traits'] = current_traits
            elif isinstance(current_traits, dict):
                for trait_entry in traits_from_equips:
                    t_name = trait_entry.get('name', '')
                    if t_name and t_name not in current_traits:
                        current_traits[t_name] = trait_entry.get('desc', '')
                update['new_traits'] = current_traits


def _normalize_member_update_payload(
    update: dict,
    *,
    split_semantic_skill_name_desc: bool = True,
) -> dict:
    """统一兼容成员结算中的旧字段格式。"""
    normalized_update = dict(update or {})

    # ── 结算字段误写进 new_traits 时先提升回顶层，再做装备/特质互迁 ──
    _hoist_misplaced_member_fields_from_new_traits(normalized_update)
    _swap_misplaced_equipment_and_traits(normalized_update)

    normalized_update['gold_change'] = _coerce_float(normalized_update.get('gold_change', 0), 0.0)
    for field_name in (
        'remaining_hp',
        'max_hp_increase',
        'strength_increase',
        'agility_increase',
        'intelligence_increase',
        'luck_increase',
        'exp_gain'
    ):
        if field_name in normalized_update:
            normalized_update[field_name] = _coerce_int(normalized_update.get(field_name), 0)
    normalized_update['new_equipments'] = _normalize_new_equipment_payload(
        normalized_update.get('new_equipments')
    )
    normalized_update['new_skills'] = _normalize_new_skills_payload(
        normalized_update.get('new_skills'),
        split_semantic_name_desc=split_semantic_skill_name_desc,
    )
    normalized_update['new_traits'] = _strip_member_update_new_traits_dirty_names(
        normalized_update.get('new_traits')
    )
    normalized_update['removed_traits'] = _normalize_removed_name_list_payload(
        normalized_update.get('removed_traits')
    )
    normalized_update['removed_skills'] = _normalize_removed_name_list_payload(
        normalized_update.get('removed_skills')
    )
    normalized_update['equipment_damage_updates'] = _normalize_equipment_damage_updates_payload(
        normalized_update.get('equipment_damage_updates')
    )
    normalized_update['consumed_equipments'] = _normalize_consumed_equipments_payload(
        normalized_update.get('consumed_equipments')
    )
    normalized_update['used_equipments'] = _normalize_used_equipments_payload(
        normalized_update.get('used_equipments')
    )
    normalized_update['stored_to_warehouse'] = _normalize_stored_to_warehouse_payload(
        normalized_update.get('stored_to_warehouse')
    )

    return normalized_update


def _normalize_member_updates_with_current_state(member_updates: list, members: list[Adventurer]) -> list[dict]:
    """用成员当前状态纠正 AI 结算，优先信任 remaining_hp，再回推 hp_change 与 status。"""
    member_map = {int(member.id): member for member in members}
    normalized_updates = []

    for raw_update in member_updates or []:
        if not isinstance(raw_update, dict):
            continue

        update = _normalize_member_update_payload(raw_update)
        member_id = _coerce_int(update.get('id'), -1)
        if member_id < 0:
            normalized_updates.append(update)
            continue

        update['id'] = member_id
        member = member_map.get(member_id)
        if member is None:
            normalized_updates.append(update)
            continue

        update['consumed_equipments'] = _normalize_consumed_equipments_payload(
            update.get('consumed_equipments'),
            member.equipment or {}
        )
        update['used_equipments'] = _normalize_used_equipments_payload(
            update.get('used_equipments'),
            member.equipment or {}
        )
        if update['consumed_equipments']:
            migrated_used_names = list(update['used_equipments'])
            for equipment_name in update['consumed_equipments']:
                equipment_data = (member.equipment or {}).get(equipment_name)
                if (
                    isinstance(equipment_data, dict)
                    and equipment_data.get('uses_left') not in (None, '')
                ):
                    migrated_used_names.append(equipment_name)
            update['used_equipments'] = migrated_used_names
        base_hp = max(0, _coerce_int(member.hp, 0))
        base_max_hp = max(1, _coerce_int(member.max_hp, 1))
        max_hp_increase = _coerce_int(update.get('max_hp_increase', 0), 0)
        final_max_hp = max(1, base_max_hp + max_hp_increase)
        if update.get('remaining_hp') not in (None, ''):
            final_hp = _coerce_int(update.get('remaining_hp'), base_hp)
        else:
            final_hp = base_hp

        final_hp = max(0, min(final_max_hp, final_hp))
        is_dead = _is_member_update_dead(update) or final_hp <= 0

        if is_dead:
            final_hp = 0
            update['is_dead'] = True
            update['status'] = 'dead'
        else:
            update['is_dead'] = False
            update['status'] = 'resting' if final_hp < final_max_hp else 'idle'

        update['remaining_hp'] = final_hp
        update['hp_change'] = final_hp - base_hp
        update['max_hp_increase'] = max_hp_increase
        normalized_updates.append(update)

    return normalized_updates


# ── 前置整理应用 ──────────────────────────────────────────────────────────────

def _apply_pre_expedition_trait_merges(
    members: list[Adventurer],
    prepared_context: dict | None,
    existing_traits: set[str] | None = None
) -> bool:
    """把小 AI 判定的旧特质合并结果真正写回冒险者特质，旧特质改为隐藏归档。"""
    prepared_member_map = _build_prepared_context_member_map(prepared_context)
    if not prepared_member_map:
        return False
    member_map = {int(member.id): member for member in members}
    for member_id, prepared_member in prepared_member_map.items():
        trait_merge_plans = prepared_member.get('trait_merge_plans') or []
        if not trait_merge_plans:
            continue

        member = member_map.get(member_id)
        if member is None:
            continue

        current_traits = [normalize_adventurer_trait_entry(trait) for trait in (member.traits or [])]
        modified = False

        for merge_plan in trait_merge_plans:
            if not isinstance(merge_plan, dict):
                continue

            source_traits = [
                str(trait_name).strip()
                for trait_name in (merge_plan.get('source_traits') or [])
                if str(trait_name).strip()
            ]
            merged_trait = merge_plan.get('merged_trait') or {}
            merged_trait_name = _clean_compact_text(merged_trait.get('name'), 60)
            merged_trait_desc = _clean_persisted_text(merged_trait.get('desc'))
            if len(source_traits) < 2 or not merged_trait_name or not merged_trait_desc:
                continue

            source_entries = []
            for entry in current_traits:
                if entry.get('hidden'):
                    continue
                if entry.get('name') in source_traits:
                    source_entries.append(entry)

            if len(source_entries) != len(source_traits):
                continue

            for entry in source_entries:
                entry['hidden'] = True
                entry['merged_into'] = merged_trait_name
                entry['archived_reason'] = 'trait_merge'
                entry['archived_at'] = to_utc_iso(_portable_utc_now())
                modified = True

            visible_merged_entry = None
            for entry in current_traits:
                if not entry.get('hidden') and entry.get('name') == merged_trait_name:
                    visible_merged_entry = entry
                    break

            if visible_merged_entry is None:
                current_traits.append({
                    'name': merged_trait_name,
                    'desc': merged_trait_desc,
                    'hidden': False,
                    'is_merged_trait': True,
                    'merged_from': source_traits,
                    'merge_generated_by': 'pre_expedition_ai',
                })
                modified = True
            else:
                visible_merged_entry['desc'] = merged_trait_desc
                visible_merged_entry['is_merged_trait'] = True
                merged_from = set(visible_merged_entry.get('merged_from') or [])
                merged_from.update(source_traits)
                visible_merged_entry['merged_from'] = list(merged_from)
                modified = True

        if modified:
            member.traits = current_traits

    return False


def _merge_upgraded_equipment_desc(base_desc: object, upgraded_desc: object) -> str:
    """升级装备时保留旧描述核心含义，再补充这次强化后的增强。"""
    return merge_adventurer_upgraded_equipment_desc(base_desc, upgraded_desc)


def _normalize_equipment_rarity_value(raw_rarity: object) -> str:
    """归一化装备稀有度，未知值统一回退到 common。"""
    cleaned_rarity = _clean_compact_text(raw_rarity, 40).lower()
    if cleaned_rarity in _EQUIPMENT_RARITY_PRIORITY:
        return cleaned_rarity
    return 'common'


def _pick_highest_equipment_rarity(*rarities: object) -> str:
    """返回一组装备稀有度中的最高档，用于升级结果保底。"""
    normalized_rarities = [
        _normalize_equipment_rarity_value(rarity)
        for rarity in rarities
    ]
    return max(
        normalized_rarities,
        key=lambda rarity: _EQUIPMENT_RARITY_PRIORITY.get(rarity, 0),
        default='common',
    )


def _apply_pre_expedition_equipment_upgrades(
    members: list[Adventurer],
    prepared_context: dict | None,
) -> bool:
    """把前置整理器决定的装备升级计划真正写回背包。"""
    prepared_member_map = _build_prepared_context_member_map(prepared_context)
    if not prepared_member_map:
        return False

    member_map = {int(member.id): member for member in members}
    any_modified = False
    for member_id, prepared_member in prepared_member_map.items():
        equipment_upgrade_plans = prepared_member.get('equipment_upgrade_plans') or []
        if not equipment_upgrade_plans:
            continue

        member = member_map.get(member_id)
        if member is None:
            continue

        current_equipment = dict(member.equipment or {})
        applied_upgrades = []
        modified = False
        for plan in equipment_upgrade_plans:
            if not isinstance(plan, dict):
                continue

            base_equipment = str(plan.get('base_equipment') or '').strip()
            material_items = [
                str(item_name).strip()
                for item_name in (plan.get('material_items') or [])
                if str(item_name).strip()
            ]
            upgraded_equipment = plan.get('upgraded_equipment') or {}
            if not isinstance(upgraded_equipment, dict):
                continue

            upgraded_name = str(upgraded_equipment.get('name') or '').strip()
            if not base_equipment or not material_items or not upgraded_name:
                continue

            if (
                upgraded_name in current_equipment
                and base_equipment not in current_equipment
                and all(item_name not in current_equipment for item_name in material_items)
            ):
                continue

            if base_equipment not in current_equipment:
                continue
            if any(item_name not in current_equipment for item_name in material_items):
                continue

            base_equipment_data = dict(current_equipment.get(base_equipment) or {})
            material_rarities = [
                dict(current_equipment.get(item_name) or {}).get('rarity')
                for item_name in material_items
            ]
            merged_desc = _merge_upgraded_equipment_desc(
                base_equipment_data.get('desc'),
                upgraded_equipment.get('desc'),
            )
            next_equipment_data = {
                'type': str(
                    upgraded_equipment.get('type')
                    or base_equipment_data.get('type')
                    or 'unknown'
                ).strip(),
                'desc': merged_desc,
                'rarity': _pick_highest_equipment_rarity(
                    upgraded_equipment.get('rarity'),
                    base_equipment_data.get('rarity'),
                    *material_rarities,
                ),
            }
            if 'locked' in base_equipment_data:
                next_equipment_data['locked'] = bool(base_equipment_data.get('locked'))
            if base_equipment_data.get('uses_left') is not None:
                next_equipment_data['uses_left'] = base_equipment_data.get('uses_left')
            if base_equipment_data.get('damage_stage'):
                next_equipment_data['damage_stage'] = base_equipment_data.get('damage_stage')
            if base_equipment_data.get('damage_note'):
                next_equipment_data['damage_note'] = base_equipment_data.get('damage_note')

            current_equipment.pop(base_equipment, None)
            for item_name in material_items:
                current_equipment.pop(item_name, None)
            current_equipment[upgraded_name] = next_equipment_data
            applied_upgrades.append({
                'base_equipment': base_equipment,
                'material_items': material_items,
                'upgraded_equipment': {
                    'name': upgraded_name,
                    'type': next_equipment_data.get('type'),
                    'desc': next_equipment_data.get('desc'),
                    'rarity': next_equipment_data.get('rarity'),
                }
            })
            modified = True

        if modified:
            member.equipment = current_equipment
            prepared_member['applied_equipment_upgrades'] = applied_upgrades
            any_modified = True

    return any_modified


def _apply_pre_expedition_trait_recovery(
    members: list[Adventurer],
    prepared_context: dict | None,
) -> bool:
    """把前置整理器决定的静养修复计划真正写回成员 traits。

    - action='remove': 直接从 traits 列表里移除该名字
    - action='shorten': 把同名 trait 的 desc 更新为 recovered_desc
    - applied_trait_recoveries 字段写回 prepared_member，供日志生成使用
    """
    prepared_member_map = _build_prepared_context_member_map(prepared_context)
    if not prepared_member_map:
        return False

    member_map = {int(member.id): member for member in members}
    any_modified = False
    for member_id, prepared_member in prepared_member_map.items():
        recovery_plans = prepared_member.get('trait_recovery_plans') or []
        if not recovery_plans:
            continue

        member = member_map.get(member_id)
        if member is None:
            continue

        current_traits = list(member.traits or [])
        applied_recoveries = []
        modified = False
        for plan in recovery_plans:
            if not isinstance(plan, dict):
                continue
            source_trait = str(plan.get('source_trait') or '').strip()
            action = str(plan.get('action') or '').strip().lower()
            if not source_trait or action not in ('remove', 'shorten'):
                continue

            target_index = -1
            for index, trait in enumerate(current_traits):
                if isinstance(trait, str) and trait == source_trait:
                    target_index = index
                    break
                if isinstance(trait, dict) and trait.get('name') == source_trait:
                    target_index = index
                    break
            if target_index < 0:
                continue

            if action == 'remove':
                current_traits.pop(target_index)
                applied_recoveries.append({
                    'source_trait': source_trait,
                    'action': 'remove',
                })
                modified = True
            else:
                recovered_desc = str(plan.get('recovered_desc') or '').strip()
                if not recovered_desc:
                    continue
                existing = current_traits[target_index]
                if isinstance(existing, dict):
                    updated = dict(existing)
                    updated['desc'] = recovered_desc
                    current_traits[target_index] = updated
                else:
                    current_traits[target_index] = {'name': source_trait, 'desc': recovered_desc}
                applied_recoveries.append({
                    'source_trait': source_trait,
                    'action': 'shorten',
                    'recovered_desc': recovered_desc,
                })
                modified = True

        if modified:
            member.traits = current_traits
            flag_modified(member, 'traits')
            prepared_member['applied_trait_recoveries'] = applied_recoveries
            any_modified = True

    return any_modified


def _merge_pre_expedition_store_items_into_updates(
    member_updates: list[dict],
    members: list[Adventurer],
    prepared_context: dict | None
) -> list[dict]:
    """把前置整理器决定的出发前存仓结果并入最终 member_updates。"""
    prepared_member_map = _build_prepared_context_member_map(prepared_context)
    if not prepared_member_map:
        return member_updates or []

    updates = [dict(update) for update in (member_updates or []) if isinstance(update, dict)]
    update_map = {}
    for update in updates:
        member_id = _coerce_int(update.get('id'), -1)
        if member_id >= 0:
            update['id'] = member_id
            update_map[member_id] = update

    member_map = {int(member.id): member for member in members}
    for member_id, prepared_member in prepared_member_map.items():
        store_items = list(prepared_member.get('pre_expedition_store_items') or [])
        if not store_items:
            continue

        update = update_map.get(member_id)
        if update is None:
            member = member_map.get(member_id)
            update = {
                'id': member_id,
                'remaining_hp': _coerce_int(getattr(member, 'hp', 0), 0),
                'stored_to_warehouse': [],
            }
            updates.append(update)
            update_map[member_id] = update

        existing_entries = _normalize_stored_to_warehouse_payload(update.get('stored_to_warehouse'))
        existing = [entry['name'] for entry in existing_entries]
        seen = set(existing)
        for item_name in store_items:
            if item_name in seen:
                continue
            existing.append(item_name)
            seen.add(item_name)
        update['stored_to_warehouse'] = existing

    return updates


# ── 战利品去重与升级识别 ───────────────────────────────────────────────────────

def _select_duplicate_new_equipment_owner(
    equipment_name: str,
    candidate_updates: list[dict]
) -> tuple[int | None, str]:
    """同名唯一战利品重复时，保留 AI 原始 JSON 中首个归属。"""
    owner_id = _coerce_int(candidate_updates[0].get('id'), -1) if candidate_updates else None
    return owner_id, f'不读取正文中文描述，按 AI 原始结算顺序保留首个归属: {equipment_name}'


def _dedupe_duplicate_new_equipments(
    result_data: dict,
    member_updates: list[dict],
    members: list[Adventurer]
) -> list[dict]:
    """不再按同名去重 new_equipments，保留 AI 原始归属结果。"""
    return [dict(update) for update in (member_updates or []) if isinstance(update, dict)]


def _find_upgraded_equipment_source_name(
    member: Adventurer,
    update: dict,
    new_equipment_name: str,
    new_equipment_data: dict,
    result_data: dict
) -> str | None:
    """根据正文、传记与新装备描述，识别被升级/替换掉的旧装备名。"""
    if not isinstance(member.equipment, dict) or not member.equipment:
        return None

    member_name = sanitize_adventurer_name(member.name, fallback=f"成员{member.id}")
    history_text = _build_member_update_evidence_text(member_name, update, result_data)
    new_desc = _clean_compact_text((new_equipment_data or {}).get('desc'), 240)
    evidence_text = ' '.join([new_equipment_name, new_desc, history_text])
    if not _has_equipment_upgrade_signal(evidence_text):
        return None

    consumed_names = set(_to_clean_string_list(update.get('consumed_equipments'), 100, 120))
    stored_names = set(_extract_stored_to_warehouse_names(update.get('stored_to_warehouse')))
    new_equipment_names = set((update.get('new_equipments') or {}).keys())
    new_type = str((new_equipment_data or {}).get('type') or '').strip()

    best_candidate_name = None
    best_candidate_score = 0
    for candidate_name, candidate_data in (member.equipment or {}).items():
        if (
            not candidate_name
            or candidate_name in consumed_names
            or candidate_name in stored_names
            or candidate_name in new_equipment_names
        ):
            continue

        candidate_type = ''
        if isinstance(candidate_data, dict):
            candidate_type = str(candidate_data.get('type') or '').strip()

        if new_type and candidate_type and candidate_type != new_type:
            continue

        score = 0
        if candidate_name in evidence_text:
            score += 100
        if new_type and candidate_type and candidate_type == new_type:
            score += 10

        if score > best_candidate_score:
            best_candidate_score = score
            best_candidate_name = candidate_name

    if best_candidate_score <= 0:
        return None
    return best_candidate_name


def _count_keyword_occurrences(text: str, keywords: tuple[str, ...]) -> int:
    """统计一组关键词在文本中的命中次数，用于极端危机结果校验。"""
    compact_text = _clean_compact_text(text, 4000)
    if not compact_text:
        return 0
    return sum(1 for keyword in keywords if keyword and keyword in compact_text)


def _detect_implausible_extreme_zero_death_result(
    result_data: dict,
    members: list[Adventurer],
    special_event_type: str | None
) -> tuple[bool, str | None]:
    """识别三类高危特殊事件里明显不可信的零死亡结果，供重试主 AI。"""
    if special_event_type not in {'abyss', 'stranger', 'unbeatable_horror'}:
        return False, None

    updates = [update for update in (result_data or {}).get('member_updates', []) if isinstance(update, dict)]
    if len(updates) < 4:
        return False, None

    dead_count = sum(1 for update in updates if _is_member_update_dead(update))
    if dead_count > 0:
        return False, None

    visible_text = _build_expedition_visible_text(result_data)
    danger_keywords = (
        '绝对死境', '大逃亡', '逃杀', '近乎灭队', '末日级', '断后', '被吞没', '底牌耗尽',
        '最后五秒', '濒死', '战线崩塌', '局势失控', '拖着残躯', '逃出生天', '重创生还',
        '核心失控', '撤离链断裂', '补给见底'
    )
    survival_keywords = (
        '免死', '替死', '复活', '复生', '时空回溯', '空间折跃', '传送', '保命底牌',
        '代价交换', '奇迹', '回滚', '逆转', '强行救回'
    )
    danger_hits = _count_keyword_occurrences(visible_text, danger_keywords)
    survival_hits = _count_keyword_occurrences(visible_text, survival_keywords)

    member_map = {int(member.id): member for member in members}
    critical_survivor_count = 0
    for update in updates:
        member = member_map.get(_coerce_int(update.get('id'), -1))
        remaining_hp = _coerce_int(update.get('remaining_hp'), getattr(member, 'hp', 0))
        max_hp = max(1, _coerce_int(getattr(member, 'max_hp', 1), 1))
        if remaining_hp <= max(1, int(max_hp * 0.15)):
            critical_survivor_count += 1

    if special_event_type == 'unbeatable_horror':
        if danger_hits >= 3 and critical_survivor_count >= 1 and survival_hits < 3:
            reason = (
                "绝对死境结果出现零死亡，但正文已写出近乎灭队级压迫与多人濒危，"
                "却缺少逐人明确的保命链路。"
            )
            return True, reason
        return False, None

    if special_event_type == 'stranger':
        if danger_hits >= 3 and critical_survivor_count >= 1 and survival_hits < 2:
            reason = (
                "异界存在事件出现零死亡，但正文已写出高位压制、多人濒危或救援链崩盘，"
                "却没有逐人站得住脚的保命链路。"
            )
            return True, reason
        return False, None

    if danger_hits >= 3 and critical_survivor_count >= 1 and survival_hits == 0:
        reason = (
            "深渊异变结果出现零死亡，但正文已写出高烈度崩盘与多人濒危，"
            "却没有明确的逐人保命链路。"
        )
        return True, reason
    return False, None


# ── 整体结算规范化 ────────────────────────────────────────────────────────────

def _normalize_expedition_result_with_current_state(
    result_data: dict,
    members: list[Adventurer],
    prepared_context: dict | None = None
) -> dict:
    """规范化整条探险结算，确保日志中优先保存 remaining_hp。"""
    normalized_result = dict(result_data or {})
    merged_member_updates = _merge_pre_expedition_store_items_into_updates(
        normalized_result.get('member_updates', []),
        members,
        prepared_context
    )
    merged_member_updates = _dedupe_duplicate_new_equipments(
        result_data=normalized_result,
        member_updates=merged_member_updates,
        members=members
    )
    normalized_updates = _normalize_member_updates_with_current_state(
        merged_member_updates,
        members
    )
    normalized_result['member_updates'] = normalized_updates
    if 'completion_check' in normalized_result:
        normalized_result['completion_check'] = _normalize_completion_check_payload(
            normalized_result.get('completion_check')
        )
    return normalized_result


def _apply_equipment_damage_updates(
    current_equipment: dict,
    equipment_damage_updates: object
) -> dict:
    """把装备破损阶段写回当前装备快照。"""
    for damage_update in _normalize_equipment_damage_updates_payload(equipment_damage_updates):
        equipment_name = damage_update.get('name')
        if equipment_name not in current_equipment:
            continue

        existing_equipment_data = current_equipment.get(equipment_name)
        new_equipment_data = (
            dict(existing_equipment_data)
            if isinstance(existing_equipment_data, dict)
            else {}
        )
        damage_stage = damage_update.get('damage_stage')
        if damage_stage == EQUIPMENT_DAMAGE_STAGE_INTACT:
            new_equipment_data.pop('damage_stage', None)
            new_equipment_data.pop('damage_note', None)
        else:
            new_equipment_data['damage_stage'] = damage_stage
            damage_note = _clean_persisted_text(damage_update.get('damage_note'))
            if damage_note:
                new_equipment_data['damage_note'] = damage_note
            else:
                new_equipment_data.pop('damage_note', None)

        current_equipment[equipment_name] = new_equipment_data

    return current_equipment


def _apply_member_hp_update(member: Adventurer, update: dict) -> None:
    """按规范化后的结算结果写回成员 HP、最大 HP 与状态。"""
    max_hp_increase = _coerce_int(update.get('max_hp_increase', 0), 0)
    final_max_hp = max(1, _coerce_int(member.max_hp, 1) + max_hp_increase)
    final_hp = max(0, min(final_max_hp, _coerce_int(update.get('remaining_hp'), member.hp)))
    is_dead = _is_member_update_dead(update) or final_hp <= 0

    member.max_hp = final_max_hp
    member.party_preference = 'auto'
    member.rest_start_time = None

    if is_dead:
        member.hp = 0
        member.status = 'dead'
        member.recovery_time = None
        return

    member.hp = final_hp
    if member.hp < member.max_hp:
        member.status = 'resting'
        member.recovery_time = _portable_utc_now() + timedelta(minutes=10)
    else:
        member.status = 'idle'
        member.recovery_time = None
