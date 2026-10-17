from flask import current_app

__all__ = [
    '_get_expedition_pre_event_count',
    '_calculate_expedition_progress_step',
    '_PUBLIC_LOG_INTERNAL_TOP_LEVEL_FIELDS',
    '_PUBLIC_LOG_INTERNAL_MEMBER_UPDATE_FIELDS',
    '_strip_internal_fields_from_public_log',
    '_replace_member_names_in_public_text',
    '_sanitize_public_log_member_names',
    '_sanitize_public_log_equipment_names',
    '_build_visible_public_log_content',
    'build_public_expedition_log_summary',
    'build_public_expedition_log_detail',
    'list_expedition_logs',
    'get_expedition_log_detail',
    'get_expedition_log_details_batch',
    'build_public_chat_message_payload',
    'emit_tavern_precise_updates',
    '_normalize_member_new_traits_payload',
    'emit_adventurer_update',
]

from db_repository.manager import db_manager
from plugins.adventurer.models import (
    ExpeditionLog,
)
from plugins.adventurer.models import AdventurerChatMessage
from plugins.adventurer.utils import (
    merge_adventurer_text_fragments,
    normalize_adventurer_trait_name_and_desc,
    sanitize_adventurer_name,
    sanitize_equipment_name,
)
from plugins.adventurer.services.adventurer_prompt_service import _build_generating_log_result_summary
from utils.time_utils import to_utc_iso


# ── 日志内容构建 ──────────────────────────────────────────────────────────────

def _get_expedition_pre_event_count(events: list[str]) -> int:
    """获取正式进入地牢前的前置事件数量。"""
    for index, event_text in enumerate(events):
        if '【进入地牢】' in event_text:
            return index + 1
    return 0


def _calculate_expedition_progress_step(full_log: dict) -> int:
    """计算本次推进应放出的事件数量，确保单条日志在限定次数内完成。"""
    events = full_log.get('events', [])
    current_index = int(full_log.get('current_event_index', 0) or 0)
    remaining_events = max(0, len(events) - current_index)
    if remaining_events <= 0:
        return 0

    max_pushes = max(1, int(current_app.config.get('ADVENTURER_LOG_MAX_PROGRESS_PUSHES', 8) or 8))
    progress_push_count = max(0, int(full_log.get('progress_push_count', 0) or 0))
    remaining_pushes = max(1, max_pushes - progress_push_count)
    return max(1, (remaining_events + remaining_pushes - 1) // remaining_pushes)


_PUBLIC_LOG_INTERNAL_TOP_LEVEL_FIELDS = (
    'completion_check',          # AI 隐藏校验字段（故事是否真正写完、member_gain_loss 等）
    'prepared_member_context',   # Prepare AI 内部决策输出（trait_merge_plans / pre_expedition_store_items / risks 等）
    '_prepared_member_context',  # 从 result_data 流过来的同一份数据（防御性命名）
    'auto_retry_count',          # 后台自动重试计数
    'auto_retried_at',           # 后台自动重试时间戳
    'shopping_purchased_items',  # 购物中间数据（events 已经把可见信息写进去了）
    'upgrade_events',            # 升级阶段中间数据（events 已经把可见信息写进去了）
    'storage_events',            # 存仓阶段中间数据（events 已经把可见信息写进去了）
    'departure_msg',             # 出发文案中间数据（events 已经把可见信息写进去了）
)

_PUBLIC_LOG_INTERNAL_MEMBER_UPDATE_FIELDS = (
    'exp_gain',                  # 内部经验值，不展示给前台
)


def _strip_internal_fields_from_public_log(full_log: dict) -> dict:
    """移除前台探险日志不需要暴露的内部结算字段。"""
    public_full_log = dict(full_log or {})
    for field_name in _PUBLIC_LOG_INTERNAL_TOP_LEVEL_FIELDS:
        public_full_log.pop(field_name, None)
    member_updates = public_full_log.get('member_updates')
    if not isinstance(member_updates, list):
        return public_full_log

    sanitized_updates = []
    for update in member_updates:
        if not isinstance(update, dict):
            sanitized_updates.append(update)
            continue
        sanitized_update = dict(update)
        for field_name in _PUBLIC_LOG_INTERNAL_MEMBER_UPDATE_FIELDS:
            sanitized_update.pop(field_name, None)
        sanitized_updates.append(sanitized_update)

    public_full_log['member_updates'] = sanitized_updates
    return public_full_log


def _replace_member_names_in_public_text(text: object, replacement_map: dict[str, str]) -> object:
    """按成员名字映射替换前台文本里的历史脏名字。"""
    if not isinstance(text, str) or not replacement_map:
        return text

    normalized_text = text
    for raw_name, safe_name in sorted(
        replacement_map.items(),
        key=lambda item: len(item[0]),
        reverse=True
    ):
        if raw_name and safe_name and raw_name != safe_name:
            normalized_text = normalized_text.replace(raw_name, safe_name)
    return normalized_text


def _sanitize_public_log_member_names(full_log: dict, result_summary: object) -> tuple[dict, object]:
    """统一清洗前台日志里的成员名，兼容历史坏名字。"""
    public_full_log = dict(full_log or {})
    replacement_map: dict[str, str] = {}

    sanitized_updates = []
    for update in public_full_log.get('member_updates') or []:
        if not isinstance(update, dict):
            sanitized_updates.append(update)
            continue

        sanitized_update = dict(update)
        raw_name = sanitized_update.get('name')
        member_id = sanitized_update.get('id')
        safe_name = sanitize_adventurer_name(raw_name, fallback=f"成员{member_id}")
        if isinstance(raw_name, str) and raw_name and raw_name != safe_name:
            replacement_map[raw_name] = safe_name
        sanitized_update['name'] = safe_name
        sanitized_update['history_story_append'] = _replace_member_names_in_public_text(
            sanitized_update.get('history_story_append'),
            replacement_map
        )
        sanitized_updates.append(sanitized_update)

    public_full_log['member_updates'] = sanitized_updates
    public_full_log['summary'] = _replace_member_names_in_public_text(
        public_full_log.get('summary'),
        replacement_map
    )
    public_full_log['events'] = [
        _replace_member_names_in_public_text(event_text, replacement_map)
        for event_text in (public_full_log.get('events') or [])
    ]
    closure_prepare = public_full_log.get('closure_prepare')
    if isinstance(closure_prepare, dict):
        sanitized_prepare = dict(closure_prepare)
        sanitized_prepare['summary'] = _replace_member_names_in_public_text(
            sanitized_prepare.get('summary'),
            replacement_map
        )
        sanitized_prepare['party_summary'] = _replace_member_names_in_public_text(
            sanitized_prepare.get('party_summary'),
            replacement_map
        )
        sanitized_prepare['events'] = [
            _replace_member_names_in_public_text(event_text, replacement_map)
            for event_text in (sanitized_prepare.get('events') or [])
        ]
        public_full_log['closure_prepare'] = sanitized_prepare
    result_summary = _replace_member_names_in_public_text(result_summary, replacement_map)
    return public_full_log, result_summary


def _sanitize_public_log_equipment_names(public_full_log: dict) -> dict:
    """清洗日志中 member_updates 里的脏装备名（截断括号等）。"""
    member_updates = public_full_log.get('member_updates')
    if not isinstance(member_updates, list):
        return public_full_log
    for update in member_updates:
        if not isinstance(update, dict):
            continue
        for field_name in ('new_equipments',):
            raw_equip = update.get(field_name)
            if not isinstance(raw_equip, dict):
                continue
            cleaned_equip = {}
            for eq_name, eq_data in raw_equip.items():
                clean_name = sanitize_equipment_name(eq_name)
                cleaned_equip[clean_name] = eq_data
            update[field_name] = cleaned_equip
    return public_full_log


def _build_visible_public_log_content(log: ExpeditionLog) -> tuple[dict, str]:
    """构建前台可见的日志正文与结果摘要。"""
    full_log = log.full_log if isinstance(log.full_log, dict) else {}
    status = full_log.get('status')

    visible_closure_meta = {}
    for key in (
        'closure_log_scope',
        'closure_mode',
        'closure_team_index',
        'closure_team_count',
        'closure_battlefront',
        'closure_battlefront_label',
        'closure_log_type',
        'closure_prepare',
    ):
        if key in full_log:
            visible_closure_meta[key] = full_log.get(key)

    if status == 'in_progress':
        idx = full_log.get('current_event_index', 0)
        events = full_log.get('events', [])
        pre_events = _get_expedition_pre_event_count(events)

        actual_idx = max(idx, pre_events)
        visible_log = {
            'status': 'in_progress',
            'events': events[:actual_idx],
            'summary': '探险正在进行中...',
            'member_updates': []
        }
        visible_log.update(visible_closure_meta)
        return visible_log, '探险进行中...'

    if status == 'generating':
        events = full_log.get('events', [])
        result_summary = _build_generating_log_result_summary(full_log)
        visible_log = {
            'status': 'generating',
            'events': events,
            'summary': result_summary,
            'member_updates': []
        }
        visible_log.update(visible_closure_meta)
        return visible_log, result_summary

    log_dict = log.to_dict()
    public_full_log = log_dict.get('full_log', {})
    if not isinstance(public_full_log, dict):
        public_full_log = {}
    public_full_log = _strip_internal_fields_from_public_log(public_full_log)
    public_full_log, result_summary = _sanitize_public_log_member_names(
        public_full_log,
        log_dict.get('result_summary')
    )
    public_full_log = _sanitize_public_log_equipment_names(public_full_log)
    return public_full_log, result_summary


def build_public_expedition_log_summary(log: ExpeditionLog) -> dict:
    """构建前台探险日志列表摘要，避免列表页一次性返回整段日志正文。"""
    visible_full_log, result_summary = _build_visible_public_log_content(log)
    return {
        'id': log.id,
        'party_id': log.party_id,
        'party_name': log.party_name,
        'target_dungeon': log.target_dungeon,
        'result_summary': result_summary,
        'log_status': visible_full_log.get('status', 'completed'),
        'created_at': to_utc_iso(log.created_at)
    }


def build_public_expedition_log_detail(log: ExpeditionLog) -> dict:
    """构建前台探险日志详情。"""
    visible_full_log, result_summary = _build_visible_public_log_content(log)
    return {
        'id': log.id,
        'party_id': log.party_id,
        'party_name': log.party_name,
        'target_dungeon': log.target_dungeon,
        'result_summary': result_summary,
        'log_status': visible_full_log.get('status', 'completed'),
        'full_log': visible_full_log,
        'created_at': to_utc_iso(log.created_at)
    }


def list_expedition_logs(
    page: int = 1,
    per_page: int = 10,
    party_name: str | None = None,
    adventurer_id: int | None = None,
    include_details: bool = False,
) -> tuple[dict | None, str | None]:
    """获取前台探险日志列表，可按小队名或冒险者筛选。"""
    try:
        logs, total = db_manager.expedition_log.list_paginated(
            page=page,
            per_page=per_page,
            party_name=party_name,
            adventurer_id=adventurer_id
        )
        build_log_payload = build_public_expedition_log_detail if include_details else build_public_expedition_log_summary
        return {
            'items': [build_log_payload(log) for log in logs],
            'total': total,
            'page': page,
            'pages': (total + per_page - 1) // per_page
        }, None
    except Exception as e:
        return None, str(e)


def get_expedition_log_detail(log_id: int) -> tuple[dict | None, str | None]:
    """获取单条前台探险日志详情。"""
    try:
        log = db_manager.expedition_log.get_by_id(log_id)
        if not log:
            return None, '日志不存在'
        return build_public_expedition_log_detail(log), None
    except Exception as e:
        return None, str(e)


def get_expedition_log_details_batch(log_ids: list[int]) -> tuple[dict | None, str | None]:
    """批量获取多条前台探险日志详情。"""
    try:
        normalized_ids = []
        seen_ids = set()
        for raw_log_id in log_ids or []:
            try:
                log_id = int(raw_log_id)
            except (TypeError, ValueError):
                continue
            if log_id <= 0 or log_id in seen_ids:
                continue
            seen_ids.add(log_id)
            normalized_ids.append(log_id)

        if not normalized_ids:
            return {'items': [], 'missing_ids': []}, None

        logs = db_manager.expedition_log.find_all(id_in=normalized_ids)
        detail_map = {
            log.id: build_public_expedition_log_detail(log)
            for log in logs
        }
        return {
            'items': [detail_map[log_id] for log_id in normalized_ids if log_id in detail_map],
            'missing_ids': [log_id for log_id in normalized_ids if log_id not in detail_map]
        }, None
    except Exception as e:
        return None, str(e)


def build_public_chat_message_payload(message: AdventurerChatMessage) -> dict:
    """构建前台聊天室消息。"""
    user = db_manager.user.get_by_id(message.user_id) if message.user_id else None
    return {
        'id': message.id,
        'user_id': message.user_id,
        'nickname': (user.nickname or user.username) if user else '未知',
        'active_title': user.active_title if user else None,
        'content': message.content,
        'created_at': to_utc_iso(message.created_at),
    }


# ── Socket 推送 ───────────────────────────────────────────────────────────────

def emit_tavern_precise_updates(
    adventurer_ids: list[int] | None = None,
    party_ids: list[int] | None = None,
    log_ids: list[int] | None = None,
    deleted_log_ids: list[int] | None = None,
    chat_message_ids: list[int] | None = None,
    reason: str | None = None
) -> None:
    """推送深渊酒馆精确实体更新事件。

    冒险者/小队/聊天消息事件只发"变更信号"（ids + reason），前端会去抖刷新；
    探险日志事件携带完整 payload，前端据此做局部 patch，避免重拉整页。
    """
    try:
        from extensions import socketio

        adventurer_ids = sorted(set(adventurer_ids or []))
        party_ids = sorted(set(party_ids or []))
        log_ids = sorted(set(log_ids or []))
        deleted_log_ids = sorted(set(deleted_log_ids or []))
        chat_message_ids = sorted(set(chat_message_ids or []))

        # 数据变更时使列表缓存失效，确保下次请求拿到最新数据
        if adventurer_ids:
            from utils.tavern_cache import invalidate_adventurer_list_cache
            invalidate_adventurer_list_cache()
        if party_ids:
            from utils.tavern_cache import invalidate_party_list_cache
            invalidate_party_list_cache()

        if adventurer_ids:
            socketio.emit('adventurer_changed', {
                'action': 'bulk_updated',
                'reason': reason,
                'ids': adventurer_ids
            }, namespace='/')

        if party_ids:
            socketio.emit('party_changed', {
                'action': 'bulk_updated',
                'reason': reason,
                'ids': party_ids
            }, namespace='/')

        if log_ids:
            logs = db_manager.expedition_log.find_all(id_in=log_ids)
            found_log_ids = set()
            ordered_log_details = []
            log_detail_map = {}
            for log in logs:
                found_log_ids.add(log.id)
                log_detail_map[log.id] = build_public_expedition_log_detail(log)
            for log_id in log_ids:
                if log_id in log_detail_map:
                    ordered_log_details.append(log_detail_map[log_id])
            missing_log_ids = set(log_ids) - found_log_ids
            deleted_log_ids = sorted(set(deleted_log_ids).union(missing_log_ids))
            if len(ordered_log_details) == 1 and not deleted_log_ids:
                socketio.emit('expedition_log_changed', {
                    'action': 'updated',
                    'reason': reason,
                    'data': ordered_log_details[0]
                }, namespace='/')
            elif ordered_log_details or deleted_log_ids:
                # 单条 full_log 可能有 50KB+，多条一次性广播会导致单帧 payload 过大。
                # 按 chunk 拆开顺序广播：删除信号只在第一帧携带，避免重复处理。
                BATCH_CHUNK_SIZE = 5
                if not ordered_log_details:
                    socketio.emit('expedition_logs_batch_changed', {
                        'action': 'batch_updated',
                        'reason': reason,
                        'data': [],
                        'deleted_ids': deleted_log_ids
                    }, namespace='/')
                else:
                    for offset in range(0, len(ordered_log_details), BATCH_CHUNK_SIZE):
                        chunk = ordered_log_details[offset:offset + BATCH_CHUNK_SIZE]
                        socketio.emit('expedition_logs_batch_changed', {
                            'action': 'batch_updated',
                            'reason': reason,
                            'data': chunk,
                            'deleted_ids': deleted_log_ids if offset == 0 else [],
                        }, namespace='/')

        elif deleted_log_ids:
            socketio.emit('expedition_logs_batch_changed', {
                'action': 'batch_updated',
                'reason': reason,
                'data': [],
                'deleted_ids': deleted_log_ids
            }, namespace='/')

        if chat_message_ids:
            socketio.emit('adventurer_chat_message', {
                'action': 'bulk_created',
                'reason': reason,
                'ids': chat_message_ids
            }, namespace='/')
    except Exception as e:
        current_app.logger.warning(f'Failed to emit precise tavern updates: {str(e)}')


def _normalize_member_new_traits_payload(
    new_traits,
    trait_dict: dict | None,
    *,
    member_name: str | None = None
) -> dict[str, str]:
    """把 AI 返回的新特质统一整理成 {name: desc}，避免继续写入神秘占位描述。"""
    if not new_traits:
        return {}

    if isinstance(new_traits, list):
        if new_traits and isinstance(new_traits[0], dict):
            raw_traits = {
                str(t.get('name') or '').strip(): t.get('desc')
                for t in new_traits
                if str(t.get('name') or '').strip()
            }
        else:
            raw_traits = {
                str(trait_name or '').strip(): None
                for trait_name in new_traits
                if str(trait_name or '').strip()
            }
    else:
        raw_traits = {
            str(trait_name or '').strip(): trait_desc
            for trait_name, trait_desc in dict(new_traits or {}).items()
            if str(trait_name or '').strip()
        }

    from plugins.adventurer.services.adventurer_parser_service import (
        _MEMBER_UPDATE_RESERVED_FIELD_NAMES,
    )

    rejected_trait_names = {
        candidate
        for candidate in {
            str(member_name or '').strip(),
            sanitize_adventurer_name(member_name, fallback='').strip(),
        }
        if candidate
    }

    normalized_traits: dict[str, str] = {}
    for trait_name, trait_desc in raw_traits.items():
        normalized_trait_name, normalized_trait_desc = normalize_adventurer_trait_name_and_desc(
            trait_name=trait_name,
            desc=trait_desc,
            trait_dict=trait_dict,
        )
        if normalized_trait_name in _MEMBER_UPDATE_RESERVED_FIELD_NAMES:
            current_app.logger.warning(
                "探险结算忽略误写入 new_traits 的结算字段名: 成员=%s, field=%s",
                sanitize_adventurer_name(member_name, fallback='未知冒险者') if member_name else '未知冒险者',
                normalized_trait_name,
            )
            continue
        if normalized_trait_name in rejected_trait_names:
            current_app.logger.warning(
                "探险结算忽略异常 new_traits 键名: 成员=%s, trait_name=%s",
                sanitize_adventurer_name(member_name, fallback='未知冒险者') if member_name else '未知冒险者',
                normalized_trait_name,
            )
            continue

        normalized_traits[normalized_trait_name] = normalize_adventurer_trait_name_and_desc(
            trait_name=normalized_trait_name,
            desc=merge_adventurer_text_fragments(
                normalized_traits.get(normalized_trait_name),
                normalized_trait_desc,
            ),
            trait_dict=trait_dict,
        )[1]

    return normalized_traits


def emit_adventurer_update() -> None:
    """兼容旧调用，转为精确事件发包器。"""
    emit_tavern_precise_updates(reason='legacy_adventurer_update')
