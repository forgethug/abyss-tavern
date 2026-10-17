from runtime_core.time import utc_now as _portable_utc_now
import math
import random
import types
import requests
from datetime import datetime, timedelta
from flask import current_app
from sqlalchemy.orm.attributes import flag_modified
from utils.time_utils import to_utc_iso

__all__ = [
    'ask_deepseek_expedition_result',
    'send_parties_to_expedition',
    '_do_ask_deepseek_expedition_result',
    '_progress_single_exploring_party',
    'progress_expeditions',
    'check_icu_recovery',
    'check_normal_recovery',
    'retry_stuck_generating_logs',
    'auto_game_loop',
]

from db_repository.manager import db_manager
from plugins.adventurer.models import (
    Adventurer,
    ExpeditionLog,
    AdventurerItem,
)
from plugins.adventurer.utils import (
    is_reserved_adventurer_item_name,
    sanitize_adventurer_name,
)

from plugins.adventurer.services.adventurer_config_service import (
    BERSERK_GOLD_MULTIPLIER,
    DUNGEONS,
    _clean_compact_text,
    _clean_persisted_text,
    _coerce_float,
    _coerce_int,
    _get_adventurer_ai_request_timeout,
    calculate_expedition_exp_gain,
    compute_special_event_probability,
    get_adventurer_exp_required,
    get_dungeon_level_range,
    is_berserk_mode_enabled,
)
from plugins.adventurer.services.adventurer_log_service import (
    _calculate_expedition_progress_step,
    _normalize_member_new_traits_payload,
    emit_tavern_precise_updates,
)
from plugins.adventurer.services.adventurer_parser_service import (
    _detect_implausible_extreme_zero_death_result,
    _apply_pre_expedition_equipment_upgrades,
    _apply_pre_expedition_trait_recovery,
    _apply_equipment_damage_updates,
    _apply_member_hp_update,
    _apply_pre_expedition_trait_merges,
    _build_expedition_validation_feedback,
    _normalize_expedition_result_with_current_state,
    _normalize_new_equipment_payload,
    _normalize_stored_to_warehouse_payload,
    _validate_expedition_result,
    parse_deepseek_expedition_response,
)
from plugins.adventurer.services.adventurer_prompt_service import (
    _ask_deepseek_prepare_expedition_context,
    _build_expedition_danger_assessment,
    _build_expedition_ai_system_prompt,
    _build_expedition_ai_user_prompt,
    _build_in_progress_expedition_log_payload,
    _build_expedition_member_runtime_snapshots,
    _build_expedition_member_prompt_inputs,
    _build_pre_expedition_log_events,
    _build_pre_expedition_storage_events,
    _build_special_event_prompt_context,
    _flatten_shopping_events,
    _compose_member_info_for_expedition,
    _extract_cached_prepared_member_context,
    _merge_post_prepare_purchased_items,
    _normalize_cached_shopping_purchased_items,
    _normalize_consumed_equipments_payload,
    _normalize_used_equipments_payload,
    _persist_prepare_stage_checkpoint,
    _run_post_prepare_shopping_stage,
)


_RESERVED_CLOSURE_TARGETS = frozenset({'退休归隐', '酒馆守卫战'})


def _is_reserved_closure_target(target_dungeon: str | None) -> bool:
    return str(target_dungeon or '').strip() in _RESERVED_CLOSURE_TARGETS


def ask_deepseek_expedition_result(
    party,
    members,
    cached_prepared_member_context: dict | None = None,
    purchased_items_by_member: dict[int, list[str]] | None = None
):
    """请求 DeepSeek AI 获取探险结果"""
    from plugins.adventurer.models import AdventurerShopItem
    api_key = current_app.config.get('DEEPSEEK_API_KEY')
    api_base = current_app.config.get('DEEPSEEK_API_BASE')

    member_snapshots = _build_expedition_member_runtime_snapshots(members)
    raw_member_blocks, raw_member_payloads = _build_expedition_member_prompt_inputs(member_snapshots)

    # ── 阶段1：集中执行所有 DB 操作 ──
    log = db_manager.expedition_log.get_latest_by_party(party.id)
    trigger_special_event = False
    is_extreme_crisis = False
    special_event_type = None
    if log:
        full_log = log.full_log or {}
        if full_log.get('has_special_event'):
            trigger_special_event = True
            is_extreme_crisis = full_log.get('is_extreme_crisis', False)
            special_event_type = full_log.get('special_event_type')

    # 在生成本次探险前，把最近完成日志里 AI 返回的新装备同步落库到图鉴。
    logs, _ = db_manager.expedition_log.find_page(page=1, per_page=15, order_by='created_at_desc')
    for log in logs:
        full_log = dict(log.full_log)
        if full_log.get('status') != 'completed':
            continue
        updates = full_log.get('member_updates', [])
        for u in updates:
            if not isinstance(u, dict):
                continue
            new_eqs = _normalize_new_equipment_payload(u.get('new_equipments'))
            if not new_eqs:
                continue
            with db_manager.transaction():
                for eq_name, eq_data in new_eqs.items():
                    if is_reserved_adventurer_item_name(eq_name):
                        continue
                    existing = db_manager.adventurer_item.get_by_name(eq_name)
                    if existing:
                        continue
                    rarity = eq_data.get('rarity', 'common')
                    price_map = {
                        "common": 50000,
                        "rare": 200000,
                        "epic": 1000000,
                        "legendary": 5000000
                    }
                    new_item = AdventurerItem(
                        name=eq_name,
                        type=eq_data.get('type', 'consumable'),
                        description=eq_data.get('desc', ''),
                        rarity=rarity
                    )
                    db_manager.add(new_item)
                    db_manager.flush()
                    new_shop_item = AdventurerShopItem(
                        item_id=new_item.id,
                        price=price_map.get(rarity, 50000),
                        stock=0
                    )
                    db_manager.add(new_shop_item)

    # 在 remove() 前把后续 AI 阶段会用到的列值先抓到局部变量。
    # 原因：前面的 db_manager.transaction() 提交时会把 party 的非主键列置为 expired，
    # 再 session.remove() 让实例 detached，之后访问这些列会触发 lazy refresh 而抛
    # DetachedInstanceError。party.id 是主键，detached 也能用，无须 capture。
    party_name = party.name
    party_target_dungeon = party.target_dungeon

    # 释放连接，后续 AI 调用可能耗时数分钟
    db_manager.remove()

    # ── 阶段2：AI 调用（不持有 DB 连接） ──
    target_dungeon = party_target_dungeon or DUNGEONS[0]['name']

    dungeon_info = next((d for d in DUNGEONS if d['name'] == target_dungeon), DUNGEONS[0])
    if dungeon_info['max_level'] >= 999:
        dungeon_level_desc = f"Lv.{dungeon_info['min_level']}+"
    else:
        dungeon_level_desc = f"Lv.{dungeon_info['min_level']}-{dungeon_info['max_level']}"

    prepared_member_context = (
        cached_prepared_member_context
        if isinstance(cached_prepared_member_context, dict)
        else None
    )
    if prepared_member_context is None:
        # _ask_deepseek_prepare_expedition_context 内部会自行 remove()
        prepared_member_context = _ask_deepseek_prepare_expedition_context(
            api_key=api_key,
            api_base=api_base,
            target_dungeon=target_dungeon,
            dungeon_level_desc=dungeon_level_desc,
            raw_member_payloads=raw_member_payloads,
        )
        if prepared_member_context:
            _persist_prepare_stage_checkpoint(party, members, prepared_member_context)
            db_manager.remove()

    prepared_member_context = _merge_post_prepare_purchased_items(
        prepared_member_context,
        purchased_items_by_member
    )
    member_info = _compose_member_info_for_expedition(
        raw_member_blocks,
        raw_member_payloads,
        prepared_member_context
    )

    _, special_event_mode = _build_special_event_prompt_context(
        trigger_special_event=trigger_special_event,
        is_extreme_crisis=is_extreme_crisis,
        special_event_type=special_event_type
    )
    danger_assessment = _build_expedition_danger_assessment(
        member_snapshots,
        dungeon_min_level=dungeon_info['min_level'],
        dungeon_max_level=dungeon_info['max_level'],
        prepared_member_context=prepared_member_context,
        trigger_special_event=trigger_special_event,
        is_extreme_crisis=is_extreme_crisis,
        special_event_type=special_event_type,
    )

    system_prompt = _build_expedition_ai_system_prompt()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # 确保 AI 调用前不持有 DB 连接
    db_manager.remove()

    max_retries = max(1, _coerce_int(current_app.config.get('ADVENTURER_EXPEDITION_AI_MAX_RETRIES'), 3))
    validation_feedback_limit = max(
        1,
        _coerce_int(current_app.config.get('ADVENTURER_EXPEDITION_AI_VALIDATION_FEEDBACK_LIMIT'), 3)
    )
    strict_consumed_match = bool(current_app.config.get('ADVENTURER_EXPEDITION_STRICT_CONSUMED_MATCH', False))
    zero_death_retry_used = False
    zero_death_retry_event_types = {'abyss', 'stranger', 'unbeatable_horror'}
    request_timeout = _get_adventurer_ai_request_timeout('ADVENTURER_EXPEDITION_AI_TIMEOUT_SECONDS', 600)
    validation_feedback = ''
    for attempt in range(max_retries):
        try:
            user_prompt = _build_expedition_ai_user_prompt(
                target_dungeon=target_dungeon,
                dungeon_level_desc=dungeon_level_desc,
                special_event_mode=special_event_mode,
                danger_assessment=danger_assessment,
                member_info=member_info,
                validation_feedback=validation_feedback,
            )
            data = {
                "model": current_app.config.get('DEEPSEEK_MODEL', 'deepseek-v4-pro'),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.3
            }
            # 探险故事生成涉及大量文本，超时通过配置统一控制，默认 600 秒（10分钟）
            response = requests.post(
                f"{api_base}/chat/completions",
                headers=headers,
                json=data,
                timeout=request_timeout
            )
            response.raise_for_status()
            result_json_str = response.json()['choices'][0]['message']['content'].strip()
            result_data = parse_deepseek_expedition_response(result_json_str)

            if result_data and "summary" in result_data and "events" in result_data and "member_updates" in result_data:
                normalized_preview = _normalize_expedition_result_with_current_state(
                    result_data,
                    member_snapshots,
                    prepared_member_context
                )
                is_valid, validation_errors, validation_warnings = _validate_expedition_result(
                    normalized_preview,
                    member_snapshots,
                    strict_consumed_match=strict_consumed_match,
                )
                if validation_warnings:
                    current_app.logger.warning(
                        "探险结果结构化校验出现 warning: 队伍=%s, warnings=%s",
                        party_name,
                        "; ".join(validation_warnings)
                    )
                if not is_valid:
                    validation_feedback = _build_expedition_validation_feedback(
                        validation_errors,
                        validation_feedback_limit
                    )
                    current_app.logger.warning(
                        "探险结果结构化校验失败，准备重试: 队伍=%s, 错误=%s",
                        party_name,
                        "; ".join(validation_errors)
                    )
                    continue
                if trigger_special_event and special_event_type in zero_death_retry_event_types:
                    should_retry, reason = _detect_implausible_extreme_zero_death_result(
                        normalized_preview,
                        member_snapshots,
                        special_event_type
                    )
                    if should_retry and not zero_death_retry_used:
                        zero_death_retry_used = True
                        current_app.logger.warning(
                            "特殊事件探险结果因零死亡可信度不足而重试一次: 队伍=%s, 类型=%s, 原因=%s",
                            party_name,
                            special_event_type,
                            reason
                        )
                        continue

                result_data = normalized_preview
                if prepared_member_context:
                    result_data['_prepared_member_context'] = prepared_member_context
                current_app.logger.info(f"AI 探险剧本生成成功！(队伍: {party_name})")
                return result_data
            else:
                current_app.logger.warning(f"DeepSeek AI 请求成功，但返回结构无法解析为探险结果")
                validation_feedback = "上次返回内容没有成功解析出合法的 `summary / events / member_updates`，请重新完整输出。"
                continue
        except Exception as e:
            current_app.logger.warning(f"DeepSeek AI 请求失败 (尝试 {attempt+1}/{max_retries}): {str(e)}")
            if 'result_json_str' in locals() and result_json_str:
                current_app.logger.warning(f"AI 原始输出内容: {result_json_str}")
            continue

    current_app.logger.error(f"异步任务 ai_expedition 执行失败: AI 探险剧本生成多次重试均失败 (队伍: {party_name})")
    return None


def send_parties_to_expedition(target_party_id=None):
    """派遣组建好的小队出发探险，并获取结果更新状态"""
    now = _portable_utc_now()
    changed_party_ids = set()
    changed_adventurer_ids = set()
    created_log_ids = set()
    with db_manager.transaction():
        # 查找准备出发的小队
        if target_party_id:
            parties_to_explore = [
                p
                for p in db_manager.adventurer_party.find_all(id=target_party_id, status='forming')
                if not _is_reserved_closure_target(getattr(p, 'target_dungeon', None))
            ]
            for p in parties_to_explore:
                if not p.target_dungeon:
                    # 确保强制出发时也有目标地牢
                    party_members = db_manager.party_member.get_by_party(p.id)
                    adventurer_ids = [pm.adventurer_id for pm in party_members]
                    adventurers = db_manager.adventurer.find_all(id_in=adventurer_ids)
                    if adventurers and len(adventurers) > 0:
                        avg_level = sum(a.level for a in adventurers) / len(adventurers)
                        available_dungeons = [d for d in DUNGEONS if d['min_level'] <= avg_level <= d['max_level']]
                        if available_dungeons:
                            p.target_dungeon = random.choice(available_dungeons)['name']
                        else:
                            p.target_dungeon = DUNGEONS[0]['name']
                    else:
                        p.target_dungeon = DUNGEONS[0]['name']
                p.status = 'exploring'
                changed_party_ids.add(p.id)
                for m in p.members:
                    m.status = 'exploring'
                    changed_adventurer_ids.add(m.id)
            db_manager.flush()
        else:
            # 定时任务：检查是否满员，满员后设置1小时倒计时，倒计时结束才出发
            forming_parties = [
                p
                for p in db_manager.adventurer_party.get_forming()
                if not _is_reserved_closure_target(getattr(p, 'target_dungeon', None))
            ]
            parties_to_explore = []
            for p in forming_parties:
                # 使用 len(p.members) 而不是 count_by_party 避免 N+1 查询，因为 members 使用了 subquery 预加载
                if len(p.members) >= p.target_size:
                    if not p.departure_time:
                        # 应用可能已累积的赞助减免时间
                        reduced_mins = getattr(p, 'sponsor_reduced_minutes', 0)
                        p.departure_time = now + timedelta(minutes=45) - timedelta(minutes=reduced_mins)

                    if now >= p.departure_time:
                        p.status = 'exploring'
                        changed_party_ids.add(p.id)
                        for m in p.members:
                            m.status = 'exploring'
                            changed_adventurer_ids.add(m.id)
                        parties_to_explore.append(p)
            db_manager.flush()
        # AI 请求比较耗时，为避免阻塞主线程/数据库事务太久，
        # 我们先提交状态的变更，这样用户至少能看到小队变成了探险中
        # 并且为了防止 progress_expeditions 认为没有日志是异常而错误解散小队，我们提前生成占位日志
        # 赛季末狂暴模式仅在 _do_party_explore 入口读取一次，避免每个 party 都查 SystemConfig
        berserk_mode = is_berserk_mode_enabled()
        for p in parties_to_explore:
            dungeon_info = next((d for d in DUNGEONS if d['name'] == p.target_dungeon), DUNGEONS[0])
            # 动态计算特殊事件触发概率：常规 min(level*0.03, 0.25)，狂暴 min(0.55+level*0.05, 1.0)
            special_prob = compute_special_event_probability(
                dungeon_info['min_level'],
                berserk_mode=berserk_mode
            )
            has_special_event = random.random() < special_prob
            is_extreme_crisis = False
            special_event_type = None

            if has_special_event:
                is_extreme_crisis = random.random() < 0.10
                # 对于极端危机，40%深渊警告，40%偶遇强者，20%绝对死境大逃亡
                if is_extreme_crisis:
                    rand_val = random.random()
                    if rand_val < 0.4:
                        special_event_type = 'abyss'
                    elif rand_val < 0.8:
                        special_event_type = 'stranger'
                    else:
                        special_event_type = 'unbeatable_horror'

            placeholder_log = ExpeditionLog(
                party_id=p.id,
                party_name=p.name,
                target_dungeon=p.target_dungeon or DUNGEONS[0]['name'],
                result_summary="出发中……",
                full_log={
                    "status": "generating",
                    "events": [],
                    "member_updates": [],
                    "summary": "正在向地牢深处进发...",
                    "has_special_event": has_special_event,
                    "is_extreme_crisis": is_extreme_crisis,
                    "special_event_type": special_event_type,
                    "berserk_mode": berserk_mode,
                    "prepare_status": "pending",
                    "battle_status": "pending"
                }
            )
            db_manager.add(placeholder_log)
            db_manager.flush()
            created_log_ids.add(placeholder_log.id)
        # db_manager.transaction 结束时会自动 commit

    if changed_party_ids or changed_adventurer_ids or created_log_ids:
        emit_tavern_precise_updates(
            adventurer_ids=list(changed_adventurer_ids),
            party_ids=list(changed_party_ids),
            log_ids=list(created_log_ids),
            reason='expedition_departed'
        )

    for party in parties_to_explore:
        # 重新绑定会话中的对象，因为前面 commit 过了
        party = db_manager.adventurer_party.find_one(id=party.id)

        # 提交异步任务生成剧本
        from services.async_task_service import AsyncTaskService
        AsyncTaskService.submit_task('ai_expedition', {
            'party_id': party.id,
            'adventurer_shopping_logs': None,
            'departure_msg': None
        })


def _do_ask_deepseek_expedition_result(party_id, adventurer_shopping_logs=None, departure_msg=None):
    """
    异步执行：向 DeepSeek 请求探险结果并更新日志和状态
    """
    with db_manager.transaction():
        party = db_manager.adventurer_party.find_one(id=party_id)
        if not party:
            return
        if _is_reserved_closure_target(getattr(party, 'target_dungeon', None)):
            current_app.logger.info(
                '跳过普通探险 AI：party_id=%s target_dungeon=%s 属于闭馆保留目标',
                party_id,
                party.target_dungeon,
            )
            return
        members = party.members
        member_snapshots = _build_expedition_member_runtime_snapshots(members)
        _raw_member_blocks, raw_member_payloads = _build_expedition_member_prompt_inputs(member_snapshots)
        party_snapshot = types.SimpleNamespace(
            id=party.id,
            name=party.name,
            target_dungeon=party.target_dungeon,
        )
        log = db_manager.expedition_log.get_latest_by_party(party.id)
        cached_log = dict(log.full_log or {}) if log and isinstance(log.full_log, dict) else {}
        cached_prepared_member_context = _extract_cached_prepared_member_context(cached_log)
        cached_departure_msg = cached_log.get('departure_msg')
        cached_shopping_events = list(cached_log.get('shopping_events') or [])
        cached_purchased_items_by_member = _normalize_cached_shopping_purchased_items(
            cached_log.get('shopping_purchased_items')
        )

    # 释放数据库连接，避免后续长时间操作（AI 调用等）占用连接池
    db_manager.remove()

    prepared_member_context = cached_prepared_member_context
    if prepared_member_context is None:
        target_dungeon = party_snapshot.target_dungeon or DUNGEONS[0]['name']
        dungeon_info = next((d for d in DUNGEONS if d['name'] == target_dungeon), DUNGEONS[0])
        if dungeon_info['max_level'] >= 999:
            dungeon_level_desc = f"Lv.{dungeon_info['min_level']}+"
        else:
            dungeon_level_desc = f"Lv.{dungeon_info['min_level']}-{dungeon_info['max_level']}"
        prepared_member_context = _ask_deepseek_prepare_expedition_context(
            api_key=current_app.config.get('DEEPSEEK_API_KEY'),
            api_base=current_app.config.get('DEEPSEEK_API_BASE'),
            target_dungeon=target_dungeon,
            dungeon_level_desc=dungeon_level_desc,
            raw_member_payloads=raw_member_payloads,
        )
    if prepared_member_context:
        with db_manager.transaction():
            live_party = db_manager.adventurer_party.find_one(id=party_id)
            if not live_party:
                return
            live_members = list(live_party.members or [])
            _apply_pre_expedition_equipment_upgrades(live_members, prepared_member_context)
            _apply_pre_expedition_trait_recovery(live_members, prepared_member_context)
        if cached_prepared_member_context is None:
            live_party = db_manager.adventurer_party.find_one(id=party_id)
            if not live_party:
                return
            _persist_prepare_stage_checkpoint(
                live_party,
                list(live_party.members or []),
                prepared_member_context
            )

    shopping_purchased_items = cached_purchased_items_by_member
    if departure_msg is None and not cached_departure_msg:
        live_party = db_manager.adventurer_party.find_one(id=party_id)
        if not live_party:
            return
        live_members = list(live_party.members or [])
        adventurer_shopping_logs, shopping_purchased_items, departure_msg = _run_post_prepare_shopping_stage(
            live_party,
            live_members,
            prepared_member_context
        )
    else:
        departure_msg = departure_msg if departure_msg is not None else cached_departure_msg
        if not shopping_purchased_items:
            shopping_purchased_items = cached_purchased_items_by_member

    # 释放数据库连接，避免后续 AI 调用占用连接池
    db_manager.remove()

    live_party = db_manager.adventurer_party.find_one(id=party_id)
    if not live_party:
        return
    live_members = list(live_party.members or [])

    result_data = ask_deepseek_expedition_result(
        live_party,
        live_members,
        cached_prepared_member_context=prepared_member_context,
        purchased_items_by_member=shopping_purchased_items
    )
    prepared_member_context = None
    if isinstance(result_data, dict):
        prepared_member_context = result_data.pop('_prepared_member_context', None)

    changed_adventurer_ids = set()
    changed_party_ids = set()
    changed_log_ids = set()
    deleted_log_ids = set()

    if result_data:
        with db_manager.transaction():
            # 重新绑定 members 以防 session 过期
            party = db_manager.adventurer_party.find_one(id=party_id)
            members = party.members
            _apply_pre_expedition_trait_merges(
                members,
                prepared_member_context,
            )
            result_data = _normalize_expedition_result_with_current_state(
                result_data,
                members,
                prepared_member_context
            )

            # 获取刚才生成的占位日志，检查是否触发了特殊事件
            log = db_manager.expedition_log.get_latest_by_party(party.id)
            has_special_event = False
            is_extreme_crisis = False
            if log and log.full_log.get('has_special_event'):
                has_special_event = True
                is_extreme_crisis = log.full_log.get('is_extreme_crisis', False)

            # 记录日志，并为日志增加成员姓名信息
            updates = result_data.get('member_updates', [])
            for u in updates:
                mem = next((m for m in members if str(m.id) == str(u['id'])), None)
                if mem:
                    u['name'] = sanitize_adventurer_name(mem.name, fallback=f"成员{mem.id}")

            log = db_manager.expedition_log.get_latest_by_party(party.id)
            if log and log.full_log.get('status') == 'generating':
                updated_log = _build_in_progress_expedition_log_payload(
                    log.full_log,
                    result_data,
                    members=members,
                    prepared_member_context=prepared_member_context,
                    shopping_events=(
                        _flatten_shopping_events(adventurer_shopping_logs)
                        if adventurer_shopping_logs is not None
                        else None
                    ),
                    departure_msg=departure_msg,
                )
                log.full_log = updated_log
                log.result_summary = "探险进行中..."
                changed_log_ids.add(log.id)
            else:
                # 如果没有找到占位日志（防御性编程）
                updated_log = _build_in_progress_expedition_log_payload(
                    {
                        'has_special_event': has_special_event,
                        'is_extreme_crisis': is_extreme_crisis,
                        'special_event_type': log.full_log.get('special_event_type') if log else None,
                    },
                    result_data,
                    members=members,
                    prepared_member_context=prepared_member_context,
                    shopping_events=(
                        _flatten_shopping_events(adventurer_shopping_logs)
                        if adventurer_shopping_logs is not None
                        else None
                    ),
                    departure_msg=departure_msg,
                )
                log = ExpeditionLog(
                    party_id=party.id,
                    party_name=party.name,
                    target_dungeon=party.target_dungeon or DUNGEONS[0]['name'],
                    result_summary="探险进行中...",
                    full_log=updated_log
                )
                db_manager.add(log)
                db_manager.flush()
                changed_log_ids.add(log.id)
            changed_party_ids.add(party.id)
            changed_adventurer_ids.update(m.id for m in members)
    else:
        # 如果请求失败，解散队伍，恢复空闲，并清理占位日志
        with db_manager.transaction():
            current_app.logger.error(f"小队探险结果获取失败，已解散。")
            log = db_manager.expedition_log.get_latest_by_party(party_id)
            if log and log.full_log.get('status') == 'generating':
                deleted_log_ids.add(log.id)
                db_manager.delete_instance(log)

            # 需要重新查询以附加到当前 session
            party_to_disband = db_manager.adventurer_party.find_one(id=party_id)
            if party_to_disband:
                party_to_disband.status = 'disbanded'
                changed_party_ids.add(party_to_disband.id)
                for m in party_to_disband.members:
                    m.status = 'idle'
                    changed_adventurer_ids.add(m.id)

    emit_tavern_precise_updates(
        adventurer_ids=list(changed_adventurer_ids),
        party_ids=list(changed_party_ids),
        log_ids=list(changed_log_ids),
        deleted_log_ids=list(deleted_log_ids),
        reason='expedition_script_ready' if result_data else 'expedition_failed'
    )


def _progress_single_exploring_party(
    party_id,
    changed_party_ids,
    changed_adventurer_ids,
    changed_log_ids,
):
    """处理单个探险小队的进度推进 / 结算落库。

    调用方负责开事务并 try/except 包裹本调用，单小队失败不应该影响其他小队。
    返回值表示本次处理是否新增了图鉴道具，需要外层失效物品缓存。
    """
    need_invalidate_item_cache = False

    party = db_manager.adventurer_party.find_one(id=party_id)
    if not party:
        return need_invalidate_item_cache

    log = db_manager.expedition_log.get_latest_by_party(party.id)

    if not log or 'status' not in log.full_log or log.full_log['status'] not in ['in_progress', 'generating']:
        # 异常情况，直接解散
        party.status = 'disbanded'
        changed_party_ids.add(party.id)
        for m in party.members:
            m.status = 'idle'
            changed_adventurer_ids.add(m.id)
        return need_invalidate_item_cache

    if log.full_log['status'] == 'generating':
        # AI 还在生成剧本，此时跳过推进，等待下次调度
        return need_invalidate_item_cache

    full_log = dict(log.full_log)
    events = full_log.get('events', [])
    current_index = full_log.get('current_event_index', 0)
    step_size = _calculate_expedition_progress_step(full_log)
    current_index = min(len(events), current_index + step_size) if step_size > 0 else len(events)
    full_log['current_event_index'] = current_index
    full_log['progress_push_count'] = int(full_log.get('progress_push_count', 0) or 0) + 1
    changed_log_ids.add(log.id)

    if current_index >= len(events):
        full_log['status'] = 'completed'
        log.result_summary = full_log.get('summary', '探险结束')

        members = party.members
        normalized_result = _normalize_expedition_result_with_current_state(
            full_log,
            members
        )
        normalized_updates = normalized_result.get('member_updates', [])
        full_log['member_updates'] = normalized_updates
        updates = {u['id']: u for u in normalized_updates if isinstance(u, dict) and 'id' in u}

        # 批量查询道具图鉴，避免N+1
        all_new_eqs = set()
        for u in normalized_updates:
            neq = u.get('new_equipments', {})
            if neq:
                all_new_eqs.update(neq.keys())

        existing_items = {
            item_name
            for item_name, in db_manager.query(AdventurerItem.name)
            .filter(AdventurerItem.name.in_(all_new_eqs))
            .all()
        } if all_new_eqs else set()

        # 赛季末狂暴模式：探险开始时已写入 placeholder_log.berserk_mode，本次结算沿用同一面值
        log_berserk_mode = bool(log.full_log.get('berserk_mode', False))

        for m in members:
            update = updates.get(m.id)
            if update:
                _apply_member_hp_update(m, update)
                changed_adventurer_ids.add(m.id)

                gold_change = int(round(update.get('gold_change', 0)))
                if log_berserk_mode and gold_change > 0:
                    gold_change = int(round(gold_change * BERSERK_GOLD_MULTIPLIER))
                m.gold = max(0, _coerce_float(getattr(m, 'gold', 0), 0.0) + gold_change)

                # 保存本次探险的评分
                score = update.get('score')
                if score is not None:
                    m.latest_score = float(score)

                m.strength += update.get('strength_increase', 0)
                m.agility += update.get('agility_increase', 0)
                m.intelligence += update.get('intelligence_increase', 0)
                m.luck += update.get('luck_increase', 0)

                current_dungeon_min_level, current_dungeon_max_level = get_dungeon_level_range(log.target_dungeon)
                exp_gain = calculate_expedition_exp_gain(
                    adventurer_level=m.level,
                    dungeon_min_level=current_dungeon_min_level,
                    dungeon_max_level=current_dungeon_max_level,
                    score=update.get('score'),
                    has_special_event=log.full_log.get('has_special_event', False),
                    is_extreme_crisis=log.full_log.get('is_extreme_crisis', False),
                    berserk_mode=log_berserk_mode
                )
                current_exp = _coerce_int(getattr(m, 'exp', 0), 0)
                if exp_gain > 0:
                    current_exp += exp_gain
                m.exp = current_exp

                # 允许一次升多级（处理越级挑战获得巨量经验的情况），但等级上限为 15 级
                while m.level < 15 and m.exp >= get_adventurer_exp_required(m.level):
                    m.exp -= get_adventurer_exp_required(m.level)
                    m.level += 1

                    max_hp_inc = random.randint(15, 25)
                    m.max_hp += max_hp_inc
                    if m.status != 'dead':
                        m.hp += max_hp_inc

                    attr_names = ['strength', 'agility', 'intelligence', 'luck']
                    num_attrs = random.randint(1, 3)
                    chosen_attrs = random.sample(attr_names, num_attrs)
                    for attr in chosen_attrs:
                        if attr == 'strength':
                            m.strength += random.randint(2, 4)
                        elif attr == 'agility':
                            m.agility += random.randint(2, 4)
                        elif attr == 'intelligence':
                            m.intelligence += random.randint(2, 4)
                        elif attr == 'luck':
                            m.luck += random.randint(2, 4)

                # 【变化的统一处理】特质：先 removed_traits 再 new_traits，避免同名 remove+add 被误删
                removed_traits = update.get('removed_traits', [])
                if removed_traits:
                    current_traits = list(m.traits or [])
                    modified = False
                    for trait_name in removed_traits:
                        to_remove = None
                        for t in current_traits:
                            if isinstance(t, str) and t == trait_name:
                                to_remove = t
                                break
                            elif isinstance(t, dict) and t.get('name') == trait_name:
                                to_remove = t
                                break
                        if to_remove is not None:
                            current_traits.remove(to_remove)
                            modified = True
                    if modified:
                        m.traits = current_traits
                        flag_modified(m, 'traits')

                new_traits = update.get('new_traits', {})
                if new_traits:
                    # sqlalchemy.ext.mutable.MutableList 需要重新赋值才能触发更新
                    current_traits = list(m.traits or [])
                    from utils.tavern_cache import get_cached_trait_dict
                    trait_dict = get_cached_trait_dict()
                    new_traits_dict = _normalize_member_new_traits_payload(
                        new_traits,
                        trait_dict,
                        member_name=m.name,
                    )

                    for trait_name, trait_desc in new_traits_dict.items():
                        trait_exists = False
                        for t in current_traits:
                            if isinstance(t, str):
                                if t == trait_name:
                                    trait_exists = True
                                    current_traits[current_traits.index(t)] = {"name": trait_name, "desc": trait_desc}
                                    break
                            elif isinstance(t, dict):
                                if t.get('name') == trait_name:
                                    trait_exists = True
                                    t['desc'] = trait_desc
                                    break

                        if not trait_exists:
                            current_traits.append({"name": trait_name, "desc": trait_desc})

                    m.traits = current_traits
                    flag_modified(m, 'traits')

                # 【变化的统一处理】技能：先 removed_skills 再 new_skills
                removed_skills = update.get('removed_skills', [])
                if removed_skills:
                    current_skills = list(m.skills) if m.skills else []
                    modified = False
                    for skill_name in removed_skills:
                        to_remove = None
                        for s in current_skills:
                            if isinstance(s, str) and s == skill_name:
                                to_remove = s
                                break
                            elif isinstance(s, dict) and s.get('name') == skill_name:
                                to_remove = s
                                break
                        if to_remove is not None:
                            current_skills.remove(to_remove)
                            modified = True
                    if modified:
                        m.skills = current_skills
                        flag_modified(m, 'skills')

                new_skills = update.get('new_skills', [])
                if new_skills:
                    current_skills = list(m.skills) if m.skills else []

                    for skill in new_skills:
                        if isinstance(skill, dict) and skill.get('name'):
                            skill_name = skill.get('name')
                            skill_desc = skill.get('desc', '')
                            skill_exists = False
                            for s in current_skills:
                                if isinstance(s, str):
                                    if s == skill_name:
                                        skill_exists = True
                                        current_skills[current_skills.index(s)] = {"name": skill_name, "desc": skill_desc}
                                        break
                                elif isinstance(s, dict):
                                    if s.get('name') == skill_name:
                                        skill_exists = True
                                        s['desc'] = skill_desc
                                        break

                            if not skill_exists:
                                current_skills.append({"name": skill_name, "desc": skill_desc})
                    m.skills = current_skills
                    flag_modified(m, 'skills')

                history_append = update.get('history_story_append')
                if history_append:
                    if m.history_story:
                        m.history_story += "\n\n" + history_append
                    else:
                        m.history_story = history_append

                current_equipment = dict(m.equipment)
                original_equipment = dict(current_equipment)
                used = _normalize_used_equipments_payload(
                    update.get('used_equipments', []),
                    original_equipment
                )
                for used_eq in used:
                    if used_eq not in current_equipment:
                        continue

                    eq_data = current_equipment[used_eq]
                    equipment_type = ''
                    if isinstance(eq_data, dict):
                        equipment_type = str(eq_data.get('type') or '').strip().lower()

                    if isinstance(eq_data, dict) and 'uses_left' in eq_data and eq_data['uses_left'] is not None:
                        new_eq_data = dict(eq_data)
                        remaining_uses = _coerce_int(new_eq_data.get('uses_left'), 0) - 1
                        if remaining_uses <= 0:
                            del current_equipment[used_eq]
                        else:
                            new_eq_data['uses_left'] = remaining_uses
                            current_equipment[used_eq] = new_eq_data
                    elif equipment_type == 'consumable':
                        del current_equipment[used_eq]

                consumed = _normalize_consumed_equipments_payload(
                    update.get('consumed_equipments', []),
                    original_equipment
                )
                for c_eq in consumed:
                    if c_eq in current_equipment:
                        eq_data = current_equipment[c_eq]

                        # 如果是带有剩余次数的消耗品，则扣除次数，直到0才删除
                        if isinstance(eq_data, dict) and 'uses_left' in eq_data and eq_data['uses_left'] is not None:
                            # 这里需要注意，因为 `current_equipment` 是从 JSON 出来的
                            # 修改深层对象后需要重新赋值给当前层，否则可能触发不了 SQLAlchemy 的 JSON 更新
                            new_eq_data = dict(eq_data)
                            remaining_uses = _coerce_int(new_eq_data.get('uses_left'), 0) - 1
                            if remaining_uses <= 0:
                                del current_equipment[c_eq]
                            else:
                                new_eq_data['uses_left'] = remaining_uses
                                current_equipment[c_eq] = new_eq_data
                        else:
                            del current_equipment[c_eq]

                new_equipments = _normalize_new_equipment_payload(update.get('new_equipments'))
                if new_equipments:
                    from plugins.adventurer.models import AdventurerShopItem
                    for eq_name, eq_data in new_equipments.items():
                        if is_reserved_adventurer_item_name(eq_name):
                            continue
                        current_equipment[eq_name] = eq_data

                        # 尝试保存新道具到道具图鉴表
                        if eq_name not in existing_items:
                            existing_items.add(eq_name)
                            # 根据稀有度给探险产出的战利品赋一个基础收购价
                            rarity = eq_data.get('rarity', 'common')
                            price_map = {
                                "common": 50000,
                                "rare": 200000,
                                "epic": 1000000,
                                "legendary": 5000000
                            }
                            # 防止事务中重复创建，再次确认
                            if not db_manager.adventurer_item.get_by_name(eq_name):
                                new_item = AdventurerItem(
                                    name=eq_name,
                                    type=eq_data.get('type', 'consumable'),
                                    description=eq_data.get('desc', ''),
                                    rarity=rarity
                                )
                                db_manager.add(new_item)
                                db_manager.flush()
                                new_shop_item = AdventurerShopItem(
                                    item_id=new_item.id,
                                    price=price_map.get(rarity, 50000),
                                    stock=0  # 探险打出来的属于个人物品，商店库存依然为0
                                )
                                db_manager.add(new_shop_item)
                                need_invalidate_item_cache = True

                current_equipment = _apply_equipment_damage_updates(
                    current_equipment,
                    update.get('equipment_damage_updates')
                )

                stored_to_warehouse = _normalize_stored_to_warehouse_payload(
                    update.get('stored_to_warehouse')
                )
                if stored_to_warehouse:
                    current_warehouse = list(m.warehouse) if m.warehouse else []
                    for warehouse_entry in stored_to_warehouse:
                        w_eq = warehouse_entry.get('name')
                        if not w_eq:
                            continue

                        # 检查装备是否存在且未被锁定
                        if w_eq in current_equipment:
                            eq_data = current_equipment[w_eq]
                            # 锁定的装备不能存入收藏室
                            if isinstance(eq_data, dict) and eq_data.get('locked'):
                                continue

                            if isinstance(eq_data, dict):
                                warehouse_item = {
                                    "name": w_eq,
                                    "desc": eq_data.get('desc', ''),
                                    "type": eq_data.get('type', 'consumable'),
                                    "rarity": eq_data.get('rarity', 'common'),
                                    "uses_left": eq_data.get('uses_left')
                                }
                            else:
                                warehouse_item = {
                                    "name": w_eq,
                                    "desc": "",
                                    "type": "consumable",
                                    "rarity": "common",
                                    "uses_left": None
                                }

                            note = _clean_persisted_text(warehouse_entry.get('note'))
                            if note:
                                warehouse_item['note'] = note

                            current_warehouse.append(warehouse_item)
                            del current_equipment[w_eq]
                    m.warehouse = current_warehouse

                m.equipment = current_equipment
            else:
                # 没有返回更新信息的情况，只更新状态
                if m.hp < m.max_hp:
                    m.status = 'resting'
                    if not m.recovery_time:
                        m.recovery_time = _portable_utc_now() + timedelta(minutes=10)
                else:
                    m.status = 'idle'
                # 探险结束后重置队伍偏好为自动
                m.party_preference = 'auto'
                changed_adventurer_ids.add(m.id)

        party.status = 'disbanded'
        changed_party_ids.add(party.id)
    else:
        pass  # 还在探险中，仅保存进度

    # 必须显式赋值触发 SQLAlchemy JSON 更新
    log.full_log = full_log

    return need_invalidate_item_cache


def progress_expeditions(target_party_id=None):
    """推进正在探险中的小队进度（每5分钟调用一次，或针对特定小队）。

    每个小队的结算放进独立事务并用 try/except 隔离：单个小队的异常只会回滚
    它本次的改动并跳过，不会污染整个 session 把同一轮里其他小队也拖垮。
    """
    from utils.tavern_cache import invalidate_item_cache
    changed_adventurer_ids = set()
    changed_party_ids = set()
    changed_log_ids = set()
    need_invalidate_item_cache = False

    # 先单独查出本轮要处理的小队 ID 列表，避免后续按小队拆事务时 ORM 对象跨 session detached
    with db_manager.transaction():
        if target_party_id:
            exploring_parties = [
                p
                for p in db_manager.adventurer_party.find_all(id=target_party_id, status='exploring')
                if not _is_reserved_closure_target(getattr(p, 'target_dungeon', None))
            ]
        else:
            exploring_parties = [
                p
                for p in db_manager.adventurer_party.get_exploring()
                if not _is_reserved_closure_target(getattr(p, 'target_dungeon', None))
            ]
        party_ids = [p.id for p in exploring_parties]

    for party_id in party_ids:
        try:
            with db_manager.transaction():
                if _progress_single_exploring_party(
                    party_id,
                    changed_party_ids,
                    changed_adventurer_ids,
                    changed_log_ids,
                ):
                    need_invalidate_item_cache = True
        except Exception as e:
            current_app.logger.error(
                "推进小队探险出错 party_id=%s: %s",
                party_id, e, exc_info=True
            )
            # 单小队异常只回滚它本次的事务，循环继续推进其他小队
            continue

    # 失效相应的缓存
    if need_invalidate_item_cache:
        invalidate_item_cache()

    if changed_adventurer_ids or changed_party_ids or changed_log_ids:
        emit_tavern_precise_updates(
            adventurer_ids=list(changed_adventurer_ids),
            party_ids=list(changed_party_ids),
            log_ids=list(changed_log_ids),
            reason='expedition_progressed'
        )


def check_icu_recovery():
    """检查有ICU基金的冒险者并在休息中加速回复血量（每分钟调用）"""
    now = _portable_utc_now()
    changed_adventurer_ids = set()
    with db_manager.transaction():
        # 仅查询存活受伤且拥有足够ICU基金的冒险者，极大地减少数据库锁和内存开销
        adventurers = db_manager.query(Adventurer).filter(
            Adventurer.status.in_(['idle', 'resting', 'in_party']),
            Adventurer.hp < Adventurer.max_hp,
            Adventurer.icu_fund >= 10000
        ).all()

        for adv in adventurers:
            changed = False
            if adv.status != 'in_party':
                adv.status = 'resting'
                changed = True

            warehouse_count = len(adv.warehouse or [])
            recovery_bonus = warehouse_count * 0.1
            tick_recovery = 1.0 + recovery_bonus

            if not adv.recovery_time:
                adv.recovery_time = now + timedelta(minutes=1)
                changed = True
            elif (adv.recovery_time - now).total_seconds() > 60:
                # If previously on 5m timer, adjust to 1m timer from now
                adv.recovery_time = now + timedelta(minutes=1)
                changed = True

            if now >= adv.recovery_time:
                ticks = int((now - adv.recovery_time).total_seconds() / 60) + 1

                current_icu_fund = _coerce_float(getattr(adv, 'icu_fund', 0), 0.0)
                adv.icu_fund = current_icu_fund
                max_affordable_ticks = int(current_icu_fund // 10000)
                actual_ticks = min(ticks, max_affordable_ticks)

                hp_needed = adv.max_hp - adv.hp
                ticks_needed = math.ceil(hp_needed / tick_recovery) if tick_recovery > 0 else actual_ticks
                actual_ticks = min(actual_ticks, ticks_needed)

                if actual_ticks > 0:
                    hp_recovered = actual_ticks * tick_recovery
                    adv.hp = min(adv.max_hp, adv.hp + int(hp_recovered))
                    adv.icu_fund = max(0.0, _coerce_float(getattr(adv, 'icu_fund', 0), 0.0) - (actual_ticks * 10000))
                    changed = True

                if adv.hp >= adv.max_hp:
                    if adv.status == 'resting':
                        adv.status = 'idle'
                    adv.recovery_time = None
                    if adv.party_preference == 'rest' and not adv.rest_start_time:
                        adv.rest_start_time = now
                    changed = True
                else:
                    # If we couldn't afford all ticks, revert to normal timer for the rest
                    if actual_ticks < ticks:
                        adv.recovery_time = now + timedelta(minutes=5)
                    else:
                        adv.recovery_time = adv.recovery_time + timedelta(minutes=1 * ticks)
                    changed = True
            if changed:
                changed_adventurer_ids.add(adv.id)

    if changed_adventurer_ids:
        emit_tavern_precise_updates(adventurer_ids=list(changed_adventurer_ids), reason='icu_recovery')


def check_normal_recovery():
    """检查无ICU基金的冒险者并在休息中回复血量（每5分钟由主循环调用）"""
    now = _portable_utc_now()
    changed_adventurer_ids = set()
    with db_manager.transaction():
        # 1. 修复状态卡住的冒险者（满血但还在休息中）
        stuck_adventurers = db_manager.query(Adventurer).filter(
            Adventurer.status == 'resting',
            Adventurer.hp >= Adventurer.max_hp
        ).all()
        for adv in stuck_adventurers:
            adv.status = 'idle'
            adv.recovery_time = None
            if adv.hp > adv.max_hp:
                adv.hp = adv.max_hp
            changed_adventurer_ids.add(adv.id)

        # 2. 仅处理受伤且无足够ICU基金的冒险者
        adventurers = db_manager.query(Adventurer).filter(
            Adventurer.status.in_(['idle', 'resting', 'in_party']),
            Adventurer.hp < Adventurer.max_hp,
            Adventurer.icu_fund < 10000
        ).all()

        for adv in adventurers:
            changed = False
            if adv.status != 'in_party':
                adv.status = 'resting'
                changed = True
            # 计算收藏室加成：每件物品增加0.1回血速度
            warehouse_count = len(adv.warehouse or [])
            recovery_bonus = warehouse_count * 0.1
            base_recovery = 1.0
            total_recovery = base_recovery + recovery_bonus

            if not adv.recovery_time:
                adv.recovery_time = now + timedelta(minutes=5)
                changed = True

            if now >= adv.recovery_time:
                ticks = int((now - adv.recovery_time).total_seconds() / 300) + 1
                hp_recovered = ticks * total_recovery
                adv.hp = min(adv.max_hp, adv.hp + hp_recovered)
                changed = True

                if adv.hp >= adv.max_hp:
                    if adv.status == 'resting':
                        adv.status = 'idle'
                    adv.recovery_time = None
                    if adv.party_preference == 'rest' and not adv.rest_start_time:
                        adv.rest_start_time = now
                    changed = True
                else:
                    adv.recovery_time = adv.recovery_time + timedelta(minutes=5 * ticks)
                    changed = True
            if changed:
                changed_adventurer_ids.add(adv.id)

    if changed_adventurer_ids:
        emit_tavern_precise_updates(adventurer_ids=list(changed_adventurer_ids), reason='normal_recovery')


def retry_stuck_generating_logs():
    """检测 status='generating' 且超过阈值时间仍未完成的探险日志，自动重新提交 AI 任务。
    与 GM 后台的 /admin/adventurer/logs/<id>/retry-ai 走同一条 AsyncTaskService 路径，
    AsyncTaskService 的 dedupe 锁会自然挡住"任务还在跑"的情况。
    单条日志最多自动重试 ADVENTURER_AUTO_RETRY_GENERATING_MAX_ATTEMPTS 次（默认 3），
    超过后留给 GM 手动 retry。配置项：
    - ADVENTURER_AUTO_RETRY_GENERATING_MINUTES：检测阈值，默认 30 分钟
    - ADVENTURER_AUTO_RETRY_GENERATING_MAX_ATTEMPTS：最大自动重试次数，默认 3
    """
    from services.async_task_service import AsyncTaskService

    threshold_minutes = max(
        1, _coerce_int(current_app.config.get('ADVENTURER_AUTO_RETRY_GENERATING_MINUTES', 30), 30)
    )
    max_attempts = max(
        0, _coerce_int(current_app.config.get('ADVENTURER_AUTO_RETRY_GENERATING_MAX_ATTEMPTS', 3), 3)
    )
    if max_attempts <= 0:
        return 0

    now = _portable_utc_now()
    threshold = now - timedelta(minutes=threshold_minutes)

    # 取卡住时间足够长的日志候选，status='generating' 的过滤放到 Python 侧（full_log 是 JSON）
    candidate_logs = db_manager.query(ExpeditionLog).filter(
        ExpeditionLog.created_at < threshold
    ).all()

    submitted_count = 0
    with db_manager.transaction():
        for log in candidate_logs:
            full_log = log.full_log if isinstance(log.full_log, dict) else {}
            if full_log.get('status') != 'generating':
                continue

            # 过滤掉闭馆相关的探险日志，它们由 closure 专属逻辑管理，不应进入普通探险重试队列
            if full_log.get('closure_mode') in {'retire', 'defend'} or full_log.get('closure_placeholder'):
                continue

            retry_attempts = _coerce_int(full_log.get('auto_retry_count', 0), 0)
            if retry_attempts >= max_attempts:
                continue

            # 距离上一次自动重试至少要超过 threshold_minutes，避免每次主循环都重提
            last_retry_iso = full_log.get('auto_retried_at')
            if isinstance(last_retry_iso, str) and last_retry_iso:
                try:
                    last_dt = datetime.fromisoformat(last_retry_iso.rstrip('Z'))
                    if (now - last_dt).total_seconds() < threshold_minutes * 60:
                        continue
                except ValueError:
                    pass

            submitted = AsyncTaskService.submit_task('ai_expedition', {
                'party_id': log.party_id,
                'adventurer_shopping_logs': None,
                'departure_msg': None,
            })

            if not submitted:
                # dedupe 锁还在（任务正在跑），跳过本轮，下次主循环再看
                continue

            new_full_log = dict(full_log)
            new_full_log['auto_retry_count'] = retry_attempts + 1
            new_full_log['auto_retried_at'] = to_utc_iso(now)
            log.full_log = new_full_log
            submitted_count += 1
            current_app.logger.warning(
                "自动重试卡住的 generating 探险日志: log_id=%s, party_id=%s, attempt=%s/%s",
                log.id, log.party_id, retry_attempts + 1, max_attempts,
            )

    return submitted_count


def auto_game_loop():
    """游戏主循环（可由定时任务调用）"""
    try:
        from plugins.adventurer.services.adventurer_shop_service import initialize_shop_items, restock_basic_consumables, ai_restock_items
        from plugins.adventurer.services.adventurer_party_service import form_parties
        initialize_shop_items()
        restock_basic_consumables()
        ai_restock_items()
        check_normal_recovery()
        form_parties()
        send_parties_to_expedition()
        progress_expeditions()
        retry_stuck_generating_logs()

    except Exception as e:
        current_app.logger.error(f"冒险者游戏循环出错: {str(e)}")
