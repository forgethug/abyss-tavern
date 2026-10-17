"""待抽取玩家作为可选路人 NPC 的有界注入、契约校验与结算持久化。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_operation_failure,
)
import json
import random
import re
from collections import defaultdict
from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy import and_, case, exists, func, or_, select, tuple_
from sqlalchemy.orm import aliased, joinedload, load_only, raiseload

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
    AdventurerS1Equipment,
    AdventurerS1ExpeditionLog,
    AdventurerS1ExpeditionLogMember,
    AdventurerS1ExpeditionObserverReview,
    AdventurerS1MarketBuyOffer,
    AdventurerS1MarketBuyOrder,
    AdventurerS1MarketListing,
    AdventurerS1MarketOffer,
    AdventurerS1Reservation,
    AdventurerS1Skill,
    AdventurerS1Trait,
    AdventurerS1WaitingNpcActor,
    AdventurerS1WaitingNpcMaterialization,
    AdventurerS1WaitingNpcRecord,
)
from plugins.adventurer_s1.s1_item_types import S1_ITEM_TYPE_SET
from plugins.adventurer_s1.services.adventurer_s1_settings_service import (
    S1SettingsService,
)
from plugins.adventurer_s1.services.s1_ai_output_normalization import (
    normalize_ascii_integer,
    normalize_exact_boolean,
)


WAITING_NPC_RECORD_KINDS = frozenset({
    'affiliation', 'possession', 'ability', 'strength', 'injury',
    'relationship', 'death', 'other',
})
WAITING_NPC_FACTION_ROLES = frozenset({
    'member', 'agent', 'associate', 'temporary', 'none',
})
_SAFE_DISPLAY_NAME_RE = re.compile(r'[^\u3400-\u9fffa-zA-Z]')


def _positive_config(key: str, default: int, maximum: int) -> int:
    try:
        value = int(current_app.config.get(key, default) or default)
    except (RuntimeError, TypeError, ValueError):
        value = default
    return max(1, min(maximum, value))


def _candidate_limit() -> int:
    return _positive_config('ADVENTURER_S1_WAITING_NPC_CANDIDATE_LIMIT', 4, 20)


def _selection_pool_limit() -> int:
    return max(
        _candidate_limit(),
        _positive_config(
            'ADVENTURER_S1_WAITING_NPC_SELECTION_POOL_LIMIT', 32, 200,
        ),
    )


def _faction_limit() -> int:
    return _positive_config('ADVENTURER_S1_WAITING_NPC_FACTION_LIMIT', 20, 50)


def _lease_minutes() -> int:
    return _positive_config('ADVENTURER_S1_WAITING_NPC_LEASE_MINUTES', 360, 1440)


def _record_limit() -> int:
    return _positive_config('ADVENTURER_S1_WAITING_NPC_RECORD_LIMIT', 4, 20)


def _record_max_chars() -> int:
    return _positive_config('ADVENTURER_S1_WAITING_NPC_RECORD_MAX_CHARS', 180, 1000)


_WAITING_NPC_SENTENCE_ENDINGS = frozenset('。！？!?；;\n')


def _sentence_start(text: str, before_index: int) -> int:
    for index in range(min(before_index, len(text)) - 1, -1, -1):
        if text[index] in _WAITING_NPC_SENTENCE_ENDINGS:
            return index + 1
    return 0


def _sentence_end(text: str, after_index: int) -> int:
    for index in range(max(0, after_index), len(text)):
        if text[index] in _WAITING_NPC_SENTENCE_ENDINGS:
            return index + 1
    return len(text)


def _derive_waiting_npc_excerpt(
    event_text: str,
    *,
    display_name: str | None,
    max_chars: int,
) -> str:
    """从冻结 event 确定性截取有界原文，优先保留角色所在完整句段。"""
    text = event_text.strip()
    if len(text) <= max_chars:
        return text
    anchor = str(display_name or '').strip()
    anchor_index = text.find(anchor) if anchor else -1
    if anchor_index < 0:
        return text[:max_chars].strip()

    sentence_start = _sentence_start(text, anchor_index)
    sentence_end = _sentence_end(text, anchor_index + len(anchor))
    sentence_excerpt = text[sentence_start:sentence_end].strip()
    if sentence_excerpt and len(sentence_excerpt) <= max_chars:
        return sentence_excerpt

    start = max(0, anchor_index - max_chars // 3)
    end = min(len(text), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    return text[start:end].strip()


def extract_verified_waiting_npc_updates_from_observer_raw(
    raw_response: str | None,
    *,
    contract_error: str | None,
    snapshot: dict | None,
    events: list | None,
) -> tuple[list[dict] | None, list[str]]:
    """从整体合同失败的 Observer 原响应中独立投影并复校验 NPC 子合同。

    只有 Observer 明确选择不重生成、且显式提供 `waiting_npc_updates` 时才允许
    提取。其它 override 字段一律不消费；空数组与缺失字段严格区分。
    """
    normalized_contract_error = str(contract_error or '').strip()
    if (
        not normalized_contract_error.startswith('invalid_override_fields:')
        or 'waiting_npc_updates' in normalized_contract_error
    ):
        # 只允许“其它 override 字段自身非法”的完整 JSON 进入局部投影。
        # 截断、空响应、JSON/顶层合同错误及 NPC 字段自身错误一律不得部分落状态。
        return None, ['observer_waiting_npc_partial_contract_error_ineligible']
    if not isinstance(raw_response, str) or not raw_response.strip():
        return None, ['observer_raw_response_missing']
    try:
        parsed = json.loads(raw_response)
    except (TypeError, ValueError):
        return None, ['observer_raw_response_not_json']
    if not isinstance(parsed, dict):
        return None, ['observer_raw_response_not_object']
    needs_regeneration, _ = normalize_exact_boolean(
        parsed.get('needs_regeneration'),
    )
    if needs_regeneration is not False or parsed.get('regeneration_basis') != 'none':
        return None, ['observer_waiting_npc_partial_regeneration_not_closed']
    overrides = parsed.get('override_fields')
    if (
        not isinstance(overrides, dict)
        or 'waiting_npc_updates' not in overrides
    ):
        return None, ['observer_waiting_npc_partial_missing']
    raw_updates = overrides.get('waiting_npc_updates')
    if not isinstance(raw_updates, list):
        return None, [
            'observer_waiting_npc_partial_invalid:'
            'waiting_npc_updates 必须是数组'
        ]
    normalized, errors = validate_waiting_npc_updates_strict(
        raw_updates,
        snapshot=snapshot,
        events=events,
    )
    if errors:
        return None, [
            f'observer_waiting_npc_partial_invalid:{error}'
            for error in errors[:10]
        ]
    return normalized, []


def _profile_limit(kind: str) -> int:
    defaults = {'equipment': 3, 'skill': 2, 'trait': 2}
    return _positive_config(
        f'ADVENTURER_S1_WAITING_NPC_PROFILE_{kind.upper()}_LIMIT',
        defaults[kind],
        12,
    )


def _profile_desc_max_chars() -> int:
    return _positive_config(
        'ADVENTURER_S1_WAITING_NPC_PROFILE_DESC_MAX_CHARS', 160, 600,
    )


def _empty_profile() -> dict:
    return {
        'character_class': None,
        'equipments': [],
        'skills': [],
        'traits': [],
    }


def validate_waiting_npc_profile(
    value,
    *,
    path: str = 'profile',
    allowed_equipment_types: frozenset[str] = S1_ITEM_TYPE_SET,
) -> tuple[dict, list[str]]:
    """验证有界投影；类型范围由服务端调用方指定，默认维持待抽取 NPC 标准类型。"""
    if not isinstance(value, dict):
        return _empty_profile(), [f'{path} 必须是对象']
    errors: list[str] = []
    result = _empty_profile()
    character_class = value.get('character_class')
    if character_class is not None:
        if not isinstance(character_class, str) or not character_class.strip():
            errors.append(f'{path}.character_class 必须是非空字符串或 null')
        elif len(character_class.strip()) > 50:
            errors.append(f'{path}.character_class 过长')
        else:
            result['character_class'] = character_class.strip()

    specs = (
        ('equipments', 'equipment'),
        ('skills', 'skill'),
        ('traits', 'trait'),
    )
    for field, kind in specs:
        rows = value.get(field)
        if not isinstance(rows, list):
            errors.append(f'{path}.{field} 必须是数组')
            continue
        limit = _profile_limit(kind)
        if len(rows) > limit:
            errors.append(f'{path}.{field} 超过上限 {limit}')
            continue
        normalized_rows: list[dict] = []
        seen_names: set[str] = set()
        for index, row in enumerate(rows):
            row_path = f'{path}.{field}[{index}]'
            if not isinstance(row, dict):
                errors.append(f'{row_path} 必须是对象')
                continue
            name = row.get('name')
            desc = row.get('desc')
            if not isinstance(name, str) or not name.strip():
                errors.append(f'{row_path}.name 必须是非空字符串')
                continue
            name = name.strip()
            if len(name) > 100 or name in seen_names:
                errors.append(f'{row_path}.name 过长或重复')
                continue
            seen_names.add(name)
            if not isinstance(desc, str):
                errors.append(f'{row_path}.desc 必须是字符串')
                continue
            desc_max_chars = _profile_desc_max_chars()
            if len(desc.strip()) > desc_max_chars:
                errors.append(f'{row_path}.desc 超过上限 {desc_max_chars}')
                continue
            normalized = {'name': name, 'desc': desc.strip()}
            if kind == 'equipment':
                item_type = row.get('type')
                rarity = row.get('rarity')
                quantity, _ = normalize_ascii_integer(
                    row.get('quantity', 1),
                    minimum=1,
                    maximum=999,
                )
                if not isinstance(item_type, str) or item_type not in allowed_equipment_types:
                    errors.append(f'{row_path}.type 非法')
                    continue
                if rarity not in {'common', 'rare', 'epic', 'legendary'}:
                    errors.append(f'{row_path}.rarity 非法')
                    continue
                if type(quantity) is not int or not 1 <= quantity <= 999:
                    errors.append(f'{row_path}.quantity 必须是 1~999 的整数')
                    continue
                normalized.update({
                    'type': item_type,
                    'rarity': rarity,
                    'quantity': quantity,
                })
            normalized_rows.append(normalized)
        result[field] = normalized_rows
    return result, errors


def _safe_display_name(raw_name: object, reservation_id: int) -> str:
    """显示名只承担叙事展示；稳定身份始终由 actor_id + incarnation 表达。"""
    cleaned = _SAFE_DISPLAY_NAME_RE.sub('', str(raw_name or '').strip())[:30]
    return cleaned or f'旅人{int(reservation_id)}'


def _normalize_faction_keys(raw_keys) -> list[str]:
    if not isinstance(raw_keys, (list, tuple, set)):
        return []
    return list(dict.fromkeys(
        str(value).strip()
        for value in raw_keys
        if isinstance(value, str) and value.strip() and len(value.strip()) <= 120
    ))[:_faction_limit()]


def _render_prompt_block(snapshot: dict) -> str:
    candidates = snapshot.get('candidates') or []
    if not candidates:
        return ''
    allowed_factions = snapshot.get('allowed_faction_keys') or []
    profile_limits = snapshot.get('profile_limits') or {}
    lines = [
        '【待抽取玩家路人候选（可选剧情角色，不是出场任务）】',
        '- 以下是 system 中待抽取玩家 NPC 契约的本场冻结输入；不要求使用任何候选，实际出场者与其它参与主体具有同等叙事资格，只有实际出场者才回传更新。',
        (
            f"- 本场冻结上限：waiting_npc_updates≤{len(candidates)}；"
            f"每人 records≤{int(snapshot.get('record_limit') or _record_limit())}，"
            f"后端派生的每段原文≤{int(snapshot.get('record_max_chars') or _record_max_chars())} 字；"
            f"profile.equipments≤{int(profile_limits.get('equipments') or _profile_limit('equipment'))}，"
            f"skills≤{int(profile_limits.get('skills') or _profile_limit('skill'))}，"
            f"traits≤{int(profile_limits.get('traits') or _profile_limit('trait'))}，"
            f"每项 desc≤{int(profile_limits.get('desc_max_chars') or _profile_desc_max_chars())} 字。"
        ),
    ]
    if allowed_factions:
        lines.append(
            '- 本次允许新归属的 faction_key 仅限：'
            + '、'.join(allowed_factions)
            + '。已有 faction_key 的人物只能按该归属使用，已有 faction_role 也是当前身份；'
            '身份只有在正文实际建立变化时才可修改。没有归属者可保持无势力，'
            '或在正文实际成立时加入上述势力之一。'
        )
    else:
        lines.append('- 本次没有可绑定的势力 key；所有无势力候选都只能保持 faction_key=null、faction_role="none"。')
    for candidate in candidates:
        faction = candidate.get('faction_key') or '无势力'
        lines.append(
            f"- [player_npc_actor_id:{candidate['actor_id']}] "
            f"[incarnation:{candidate['incarnation']}] "
            f"姓名：{candidate['display_name']}；性别：{candidate.get('gender') or '未知'}；"
            f"性格：{candidate.get('personality') or '未定'}；当前归属：{faction}"
            f"；当前势力身份：{candidate.get('faction_role') or 'none'}"
        )
        profile = candidate.get('current_profile') or _empty_profile()
        if any(profile.get(key) for key in ('character_class', 'equipments', 'skills', 'traits')):
            lines.append(
                '  当前有界档案投影（本次未发生变化的条目原样保留）：'
                + json.dumps(profile, ensure_ascii=False, separators=(',', ':'))
            )
        records = candidate.get('records') or []
        if records:
            lines.append('  既往档案原文（只作连续性事实，不是本次必须兑现的清单）：')
            for record in records:
                excerpt = json.dumps(record['excerpt'], ensure_ascii=False)
                lines.append(f"  · [{record['kind']}] {excerpt}")
    return '\n'.join(lines)


def _load_bounded_records_for_lives(
    life_keys: list[tuple[int, int]],
) -> dict[tuple[int, int], list[AdventurerS1WaitingNpcRecord]]:
    """批量取每个人生最近 N 条，并额外保留最近一条势力归属原文。"""
    if not life_keys:
        return {}
    partition = (
        AdventurerS1WaitingNpcRecord.actor_id,
        AdventurerS1WaitingNpcRecord.incarnation,
    )
    recent_order = (
        AdventurerS1WaitingNpcRecord.created_at.desc(),
        AdventurerS1WaitingNpcRecord.id.desc(),
    )
    ranked = (
        db.session.query(
            AdventurerS1WaitingNpcRecord.id.label('record_id'),
            AdventurerS1WaitingNpcRecord.kind.label('kind'),
            func.row_number().over(
                partition_by=partition,
                order_by=recent_order,
            ).label('recent_rank'),
            func.row_number().over(
                partition_by=partition,
                order_by=(
                    case(
                        (AdventurerS1WaitingNpcRecord.kind == 'affiliation', 0),
                        else_=1,
                    ),
                    *recent_order,
                ),
            ).label('affiliation_rank'),
        )
        .filter(tuple_(*partition).in_(life_keys))
        .subquery()
    )
    rows = (
        AdventurerS1WaitingNpcRecord.query
        .join(ranked, ranked.c.record_id == AdventurerS1WaitingNpcRecord.id)
        .filter(or_(
            ranked.c.recent_rank <= _record_limit(),
            (
                (ranked.c.kind == 'affiliation')
                & (ranked.c.affiliation_rank == 1)
            ),
        ))
        .order_by(
            AdventurerS1WaitingNpcRecord.created_at.desc(),
            AdventurerS1WaitingNpcRecord.id.desc(),
        )
        .all()
    )
    grouped: dict[tuple[int, int], list[AdventurerS1WaitingNpcRecord]] = defaultdict(list)
    for row in rows:
        grouped[(int(row.actor_id), int(row.incarnation))].append(row)
    return grouped


def build_waiting_player_npc_injection(
    expedition_log_id: int | None,
    world_story_snapshot: dict | None,
) -> dict:
    """为一场探险冻结一小批候选；同一日志重试始终复用同一快照。"""
    empty = {
        'enabled': False,
        'allowed_faction_keys': [],
        'candidates': [],
        'prompt_block': '',
    }
    if not expedition_log_id:
        return empty
    feature_enabled = bool(S1SettingsService.get('waiting_player_npc_enabled'))

    faction_keys = _normalize_faction_keys(
        (world_story_snapshot or {}).get('faction_keys')
        if isinstance(world_story_snapshot, dict) else []
    )
    now = _portable_utc_now()
    with db_manager.transaction():
        log = (
            AdventurerS1ExpeditionLog.query
            .filter_by(id=int(expedition_log_id))
            .with_for_update()
            .first()
        )
        if not log:
            return empty
        full_log = dict(log.full_log or {}) if isinstance(log.full_log, dict) else {}
        frozen = full_log.get('waiting_player_npc_injection')
        if isinstance(frozen, dict):
            frozen = dict(frozen)
            frozen['prompt_block'] = _render_prompt_block(frozen)
            return frozen
        if not feature_enabled:
            return empty

        lease_log = aliased(AdventurerS1ExpeditionLog)
        actor = AdventurerS1WaitingNpcActor
        lease_is_available = or_(
            actor.id.is_(None),
            actor.active_expedition_log_id.is_(None),
            lease_log.id.is_(None),
            ~lease_log.status.in_(('generating', 'in_progress')),
            and_(
                lease_log.status == 'generating',
                or_(
                    actor.active_lease_expires_at.is_(None),
                    actor.active_lease_expires_at <= now,
                ),
            ),
        )
        faction_is_available = or_(
            actor.id.is_(None),
            actor.faction_key.is_(None),
            actor.faction_key.in_(faction_keys),
        )
        reservations = (
            AdventurerS1Reservation.query
            .outerjoin(
                actor,
                actor.reservation_id == AdventurerS1Reservation.id,
            )
            .outerjoin(
                lease_log,
                lease_log.id == actor.active_expedition_log_id,
            )
            .filter(
                AdventurerS1Reservation.status == 'pending',
                AdventurerS1Reservation.restore_adventurer_id.is_(None),
                or_(
                    actor.id.is_(None),
                    and_(
                        actor.status == 'active',
                        actor.materialized_adventurer_id.is_(None),
                    ),
                ),
                faction_is_available,
                lease_is_available,
            )
            .order_by(
                case(
                    (actor.faction_key.in_(faction_keys), 0),
                    else_=1,
                ),
                func.coalesce(actor.injection_count, 0).asc(),
                case(
                    (actor.last_injected_at.is_(None), 0),
                    else_=1,
                ),
                actor.last_injected_at.asc(),
                AdventurerS1Reservation.id.asc(),
            )
            .limit(_selection_pool_limit())
            .with_for_update(
                of=AdventurerS1Reservation,
                skip_locked=True,
            )
            .all()
        )
        if not reservations:
            snapshot = {**empty, 'enabled': True, 'allowed_faction_keys': faction_keys}
            full_log['waiting_player_npc_injection'] = snapshot
            log.full_log = full_log
            return snapshot

        reservation_ids = [int(row.id) for row in reservations]
        actor_by_reservation = {
            int(actor.reservation_id): actor
            for actor in AdventurerS1WaitingNpcActor.query.filter(
                AdventurerS1WaitingNpcActor.reservation_id.in_(reservation_ids),
            ).with_for_update().all()
        }
        missing_user_ids = {
            int(reservation.user_id)
            for reservation in reservations
            if int(reservation.id) not in actor_by_reservation
        }
        reusable_actors = (
            AdventurerS1WaitingNpcActor.query
            .filter(
                AdventurerS1WaitingNpcActor.user_id.in_(missing_user_ids),
                AdventurerS1WaitingNpcActor.status == 'active',
                AdventurerS1WaitingNpcActor.materialized_adventurer_id.is_(None),
            )
            .order_by(
                AdventurerS1WaitingNpcActor.updated_at.desc(),
                AdventurerS1WaitingNpcActor.id.desc(),
            )
            .with_for_update()
            .all()
            if missing_user_ids else []
        )
        reusable_link_ids = {
            int(actor.reservation_id)
            for actor in reusable_actors
            if actor.reservation_id
        }
        still_pending_link_ids = {
            int(reservation_id)
            for (reservation_id,) in (
                db.session.query(AdventurerS1Reservation.id)
                .filter(
                    AdventurerS1Reservation.id.in_(reusable_link_ids),
                    AdventurerS1Reservation.status == 'pending',
                )
                .all()
                if reusable_link_ids else []
            )
        }
        reusable_by_user: dict[int, list[AdventurerS1WaitingNpcActor]] = defaultdict(list)
        claimed_actor_ids = {int(actor.id) for actor in actor_by_reservation.values()}
        for actor in reusable_actors:
            if (
                int(actor.id) in claimed_actor_ids
                or int(actor.reservation_id or 0) in still_pending_link_ids
            ):
                continue
            reusable_by_user[int(actor.user_id)].append(actor)
        for reservation in reservations:
            actor = actor_by_reservation.get(int(reservation.id))
            safe_name = _safe_display_name(reservation.name, reservation.id)
            if actor is None:
                reusable_rows = reusable_by_user.get(int(reservation.user_id)) or []
                actor = reusable_rows.pop(0) if reusable_rows else None
                if actor is None:
                    actor = AdventurerS1WaitingNpcActor(
                        reservation_id=int(reservation.id),
                        user_id=int(reservation.user_id),
                        display_name=safe_name,
                    )
                    db.session.add(actor)
                else:
                    actor.reservation_id = int(reservation.id)
                    actor.display_name = safe_name
                actor_by_reservation[int(reservation.id)] = actor
            elif actor.status == 'active':
                actor.display_name = safe_name
        db.session.flush()

        leased_log_ids = list({
            int(actor.active_expedition_log_id)
            for actor in actor_by_reservation.values()
            if actor.active_expedition_log_id
        })
        leased_log_statuses = {
            int(log_id): status
            for log_id, status in (
                db.session.query(
                    AdventurerS1ExpeditionLog.id,
                    AdventurerS1ExpeditionLog.status,
                )
                .filter(AdventurerS1ExpeditionLog.id.in_(leased_log_ids))
                .all()
                if leased_log_ids else []
            )
        }
        eligible = []
        for reservation in reservations:
            actor = actor_by_reservation[int(reservation.id)]
            if actor.status != 'active':
                continue
            lease_log_id = int(actor.active_expedition_log_id or 0)
            lease_log_status = leased_log_statuses.get(lease_log_id)
            lease_alive = (
                lease_log_id > 0
                and lease_log_status in {'generating', 'in_progress'}
                and (
                    lease_log_status == 'in_progress'
                    or (
                        actor.active_lease_expires_at is not None
                        and actor.active_lease_expires_at > now
                    )
                )
            )
            if lease_alive:
                continue
            if lease_log_id:
                actor.active_expedition_log_id = None
                actor.active_lease_expires_at = None
            eligible.append((actor, reservation))
        faction_set = set(faction_keys)
        faction_pool = [row for row in eligible if row[0].faction_key in faction_set]
        ambient_pool = [row for row in eligible if not row[0].faction_key]

        rng = random.Random(int(expedition_log_id))
        rng.shuffle(faction_pool)
        rng.shuffle(ambient_pool)
        fairness_key = lambda row: (
            int(row[0].injection_count or 0),
            row[0].last_injected_at or datetime.min,
        )
        faction_pool.sort(key=fairness_key)
        ambient_pool.sort(key=fairness_key)
        limit = _candidate_limit()
        selected = []
        selected_display_names: set[str] = set()
        cross_realm_mirror = full_log.get('cross_realm_mirror_injection')
        mirror_candidate = (
            cross_realm_mirror.get('candidate')
            if isinstance(cross_realm_mirror, dict)
            and cross_realm_mirror.get('enabled') is True
            else None
        )
        if isinstance(mirror_candidate, dict):
            mirror_display_name = str(
                mirror_candidate.get('display_name') or ''
            ).strip()
            if mirror_display_name:
                selected_display_names.add(mirror_display_name.casefold())
        for row in faction_pool + ambient_pool:
            display_name = str(row[0].display_name or '')
            display_name_key = display_name.casefold()
            if display_name_key in selected_display_names:
                continue
            selected.append(row)
            selected_display_names.add(display_name_key)
            if len(selected) >= limit:
                break
        actor_ids = [int(actor.id) for actor, _ in selected]
        records_by_life = _load_bounded_records_for_lives([
            (int(actor.id), int(actor.current_incarnation or 1))
            for actor, _ in selected
        ])

        candidates: list[dict] = []
        for actor, reservation in selected:
            current_profile, current_profile_errors = validate_waiting_npc_profile(
                (
                    actor.current_profile
                    if isinstance(actor.current_profile, dict) and actor.current_profile
                    else _empty_profile()
                ),
                path=f'actor[{int(actor.id)}].current_profile',
            )
            if current_profile_errors:
                current_app.logger.warning(
                    '待抽取玩家 NPC 当前档案越界，注入时仅保留合法有界投影 actor=%s errors=%s',
                    actor.id,
                    current_profile_errors,
                )
            all_life_rows = records_by_life.get(
                (int(actor.id), int(actor.current_incarnation or 1)),
                [],
            )
            life_rows = all_life_rows[:_record_limit()]
            if actor.faction_key and not any(row.kind == 'affiliation' for row in life_rows):
                affiliation_row = next(
                    (row for row in all_life_rows if row.kind == 'affiliation'),
                    None,
                )
                if affiliation_row is not None:
                    if len(life_rows) >= _record_limit():
                        life_rows[-1] = affiliation_row
                    else:
                        life_rows.append(affiliation_row)
            life_rows.reverse()
            candidates.append({
                'actor_id': int(actor.id),
                'incarnation': int(actor.current_incarnation or 1),
                'display_name': actor.display_name,
                'gender': reservation.gender,
                'personality': reservation.personality,
                'faction_key': actor.faction_key,
                'faction_role': actor.faction_role or 'none',
                'current_profile': current_profile,
                'records': [
                    {
                        'kind': row.kind,
                        'excerpt': str(row.excerpt or '')[:_record_max_chars()],
                    }
                    for row in life_rows
                ],
            })
            actor.injection_count = int(actor.injection_count or 0) + 1
            actor.last_injected_at = now
            actor.active_expedition_log_id = int(expedition_log_id)
            actor.active_lease_expires_at = now + timedelta(minutes=_lease_minutes())

        snapshot = {
            'enabled': True,
            'allowed_faction_keys': faction_keys,
            'candidate_limit': limit,
            'record_limit': _record_limit(),
            'record_max_chars': _record_max_chars(),
            'profile_limits': {
                'equipments': _profile_limit('equipment'),
                'skills': _profile_limit('skill'),
                'traits': _profile_limit('trait'),
                'desc_max_chars': _profile_desc_max_chars(),
            },
            'candidates': candidates,
        }
        full_log['waiting_player_npc_injection'] = snapshot
        log.full_log = full_log
        rendered_snapshot = dict(snapshot)
        rendered_snapshot['prompt_block'] = _render_prompt_block(snapshot)
        return rendered_snapshot


def load_waiting_player_npc_injection(expedition_log_id: int | None) -> dict | None:
    """用独立短读事务投影冻结快照，冷日志在归还连接后解压。"""
    if not expedition_log_id:
        return None
    from db_repository.s1_expedition_generation_repository import (
        load_frozen_waiting_npc_injection,
    )

    # 调用点跨越长 AI/Observer 阶段，不继承之前可能已失效的 scoped Session。
    db_manager.remove()
    return load_frozen_waiting_npc_injection(int(expedition_log_id))


def _recover_verified_empty_waiting_npc_quarantines(
    actors: list[AdventurerS1WaitingNpcActor],
    log_statuses: dict[int, str],
) -> int:
    """确定性解除旧版误隔离：Observer 已严格给出空 NPC 更新但其它字段坏掉。"""
    candidate_log_ids = {
        int(actor.active_expedition_log_id)
        for actor in actors
        if actor.status == 'review_pending'
        and actor.active_expedition_log_id
        and log_statuses.get(int(actor.active_expedition_log_id)) == 'completed'
    }
    if not candidate_log_ids:
        return 0
    logs = (
        AdventurerS1ExpeditionLog.query
        .filter(AdventurerS1ExpeditionLog.id.in_(candidate_log_ids))
        .with_for_update()
        .all()
    )
    reviews = (
        AdventurerS1ExpeditionObserverReview.query
        .filter(
            AdventurerS1ExpeditionObserverReview.expedition_log_id.in_(
                candidate_log_ids,
            ),
            AdventurerS1ExpeditionObserverReview.status == 'failed',
            AdventurerS1ExpeditionObserverReview.raw_response.isnot(None),
        )
        .order_by(AdventurerS1ExpeditionObserverReview.id.desc())
        .all()
    )
    reviews_by_log: dict[int, list[AdventurerS1ExpeditionObserverReview]] = (
        defaultdict(list)
    )
    reviews_by_id: dict[int, AdventurerS1ExpeditionObserverReview] = {}
    for review in reviews:
        reviews_by_log[int(review.expedition_log_id)].append(review)
        reviews_by_id[int(review.id)] = review

    recovered_actor_ids: set[int] = set()
    now = _portable_utc_now()
    for log in logs:
        full_log = dict(log.full_log or {}) if isinstance(log.full_log, dict) else {}
        quarantine = full_log.get('waiting_npc_review_quarantine')
        if (
            not isinstance(quarantine, dict)
            or quarantine.get('status') != 'quarantined'
        ):
            continue
        contract_error = str(quarantine.get('contract_error') or '').strip()
        if not contract_error:
            continue
        bound_review_id = quarantine.get('observer_review_id')
        if (
            isinstance(bound_review_id, bool)
            or not isinstance(bound_review_id, int)
            or bound_review_id <= 0
        ):
            bound_review_id = None
        has_binding_requirement = 'review_id_binding_required' in quarantine
        if (
            has_binding_requirement
            and quarantine.get('review_id_binding_required') is not True
        ):
            # 来源标记损坏时也必须保守，不得误落入旧数据兼容分支。
            continue
        review_id_binding_required = has_binding_requirement
        if review_id_binding_required and bound_review_id is None:
            # 新版隔离若审核留档失败，必须保留人工审核；不能降级成旧数据兼容。
            continue
        if bound_review_id is not None:
            bound_review = reviews_by_id.get(bound_review_id)
            candidate_reviews = [bound_review] if bound_review is not None else []
            legacy_unique_match = False
        else:
            # 对没有审核记录主键的旧数据，候选失败记录必须收敛为一条；
            # 多条同类记录一律保留人工审核，避免将重试轮次串线。
            candidate_reviews = reviews_by_log.get(int(log.id), [])
            legacy_unique_match = True
        contract_matching_reviews = [
            review
            for review in candidate_reviews
            if int(review.expedition_log_id) == int(log.id)
            and contract_error in str(review.error_message or '')
        ]
        # 旧兼容必须先证明错误来源记录本身唯一，再验证该唯一原响应；
        # 不能先丢掉非法响应后把剩余一条误称为“唯一”。
        if len(contract_matching_reviews) != 1:
            continue
        matched_review = contract_matching_reviews[0]
        verified_updates, verification_errors = (
            extract_verified_waiting_npc_updates_from_observer_raw(
                matched_review.raw_response,
                contract_error=contract_error,
                snapshot=full_log.get('waiting_player_npc_injection'),
                events=full_log.get('events'),
            )
        )
        if verification_errors or verified_updates != []:
            continue

        repaired_full_log = dict(full_log)
        repaired_full_log['waiting_npc_updates'] = []
        repaired_full_log.pop('waiting_npc_review_quarantine', None)
        repaired_full_log['waiting_npc_review_resolution'] = {
            'status': 'recovered_verified_empty_partial',
            'observer_review_id': int(matched_review.id),
            'legacy_unique_match': legacy_unique_match,
            'original_reason_code': str(
                quarantine.get('reason_code')
                or 'observer_output_contract_invalid'
            ),
            'recovered_at': now.isoformat(),
        }
        repaired_full_log['waiting_npc_persistence'] = (
            persist_completed_waiting_npc_updates(
                int(log.id),
                repaired_full_log,
            )
        )
        log.full_log = repaired_full_log
        recovered_actor_ids.update(
            int(row['actor_id'])
            for row in (
                (full_log.get('waiting_player_npc_injection') or {}).get(
                    'candidates',
                ) or []
            )
            if isinstance(row, dict)
            and isinstance(row.get('actor_id'), int)
            and not isinstance(row.get('actor_id'), bool)
        )
    return len(recovered_actor_ids)


def filter_reservations_with_active_waiting_npc_lease(
    reservations: list[AdventurerS1Reservation],
    *,
    recover_verified_empty_quarantines: bool = False,
) -> tuple[list[AdventurerS1Reservation], int]:
    """批量排除仍在剧情中的 NPC；抽取写路径可顺便修复可证明为空的旧隔离。"""
    if not reservations:
        return [], 0
    reservation_ids = [int(row.id) for row in reservations]
    reservation_ids_by_user: dict[int, set[int]] = defaultdict(set)
    for reservation in reservations:
        reservation_ids_by_user[int(reservation.user_id)].add(int(reservation.id))
    actors = AdventurerS1WaitingNpcActor.query.filter(
        or_(
            AdventurerS1WaitingNpcActor.reservation_id.in_(reservation_ids),
            AdventurerS1WaitingNpcActor.user_id.in_(list(reservation_ids_by_user)),
        ),
        AdventurerS1WaitingNpcActor.status.in_(('active', 'review_pending')),
        AdventurerS1WaitingNpcActor.active_expedition_log_id.isnot(None),
    ).all()
    leased_log_ids = list({
        int(actor.active_expedition_log_id)
        for actor in actors
        if actor.active_expedition_log_id
    })
    log_statuses = {
        int(log_id): status
        for log_id, status in (
            db.session.query(
                AdventurerS1ExpeditionLog.id,
                AdventurerS1ExpeditionLog.status,
            )
            .filter(AdventurerS1ExpeditionLog.id.in_(leased_log_ids))
            .all()
            if leased_log_ids else []
        )
    }
    if (
        recover_verified_empty_quarantines
        and _recover_verified_empty_waiting_npc_quarantines(
            actors,
            log_statuses,
        )
    ):
        # 只读路径可能位于外部 db_manager.transaction() 内（如创建冒险者）；
        # 此时恢复写入应并入外层事务提交，绝不能在此提前 commit 外层未提交改动。
        if db_manager.transaction_depth() == 0:
            ok, err = db_manager.commit()
            if not ok:
                raise RuntimeError(err or '解除待抽取玩家 NPC 旧隔离失败')
        current_app.logger.info(
            'S1 抽取前已解除可验证的空 waiting-NPC 旧隔离',
        )
        # commit 后重新建立精简读取快照，避免访问 expired ORM 对象产生逐行刷新。
        # 位于外层事务内未 commit 时，autoflush 会让重查看到本次恢复写入。
        refreshed_reservations = (
            AdventurerS1Reservation.query
            .filter(
                AdventurerS1Reservation.id.in_(reservation_ids),
                AdventurerS1Reservation.status == 'pending',
            )
            .all()
        )
        return filter_reservations_with_active_waiting_npc_lease(
            refreshed_reservations,
        )
    now = _portable_utc_now()
    blocked_reservation_ids: set[int] = set()
    for actor in actors:
        if actor.status == 'review_pending':
            lease_blocks_draw = True
        else:
            lease_blocks_draw = (
                log_statuses.get(int(actor.active_expedition_log_id or 0))
                == 'in_progress'
                or (
                    log_statuses.get(int(actor.active_expedition_log_id or 0))
                    == 'generating'
                    and actor.active_lease_expires_at is not None
                    and actor.active_lease_expires_at > now
                )
            )
        if not (
            lease_blocks_draw
        ):
            continue
        actor_reservation_id = int(actor.reservation_id or 0)
        if actor_reservation_id in reservation_ids:
            blocked_reservation_ids.add(actor_reservation_id)
        else:
            blocked_reservation_ids.update(
                reservation_ids_by_user.get(int(actor.user_id), set())
            )
    return (
        [row for row in reservations if int(row.id) not in blocked_reservation_ids],
        len(blocked_reservation_ids),
    )


def release_unused_waiting_npc_leases(
    expedition_log_id: int,
    snapshot: dict | None,
    used_actor_ids: set[int],
) -> int:
    """生成定稿后立即释放未出场候选；出场者继续占用到结算。"""
    candidate_ids = {
        int(row['actor_id'])
        for row in ((snapshot or {}).get('candidates') or [])
        if isinstance(row, dict)
        and isinstance(row.get('actor_id'), int)
        and not isinstance(row.get('actor_id'), bool)
    }
    release_ids = candidate_ids - {
        int(actor_id) for actor_id in used_actor_ids
        if isinstance(actor_id, int) and not isinstance(actor_id, bool)
    }
    if not release_ids:
        return 0
    return int(
        AdventurerS1WaitingNpcActor.query
        .filter(
            AdventurerS1WaitingNpcActor.id.in_(release_ids),
            AdventurerS1WaitingNpcActor.active_expedition_log_id
            == int(expedition_log_id),
        )
        .update({
            AdventurerS1WaitingNpcActor.active_expedition_log_id: None,
            AdventurerS1WaitingNpcActor.active_lease_expires_at: None,
        }, synchronize_session=False)
        or 0
    )


def hold_waiting_npc_review_quarantine(
    expedition_log_id: int,
    snapshot: dict | None,
) -> list[dict]:
    """冻结未实体化候选到人工审核完成，禁止未确认档案被再次注入。"""
    actor_refs = [
        {
            'actor_id': int(row['actor_id']),
            'incarnation': int(row['incarnation']),
        }
        for row in ((snapshot or {}).get('candidates') or [])
        if isinstance(row, dict)
        and isinstance(row.get('actor_id'), int)
        and not isinstance(row.get('actor_id'), bool)
        and isinstance(row.get('incarnation'), int)
        and not isinstance(row.get('incarnation'), bool)
    ]
    if not actor_refs:
        return []
    actor_ids = [row['actor_id'] for row in actor_refs]
    actors = (
        AdventurerS1WaitingNpcActor.query
        .filter(AdventurerS1WaitingNpcActor.id.in_(actor_ids))
        .with_for_update()
        .all()
    )
    actor_map = {int(actor.id): actor for actor in actors}
    held_actor_refs: list[dict] = []
    for actor_ref in actor_refs:
        actor = actor_map.get(actor_ref['actor_id'])
        if actor is None:
            raise RuntimeError(
                f'待抽取玩家 NPC #{actor_ref["actor_id"]} 不存在，无法进入审核隔离'
            )
        # 正式抽取权威高于可选 NPC 剧情。抽取事务已冻结继承快照并把 actor
        # 标为 materialized；随后到达的 Observer 隔离只能跳过，不能回写状态。
        if actor.status == 'materialized':
            continue
        if int(actor.current_incarnation or 1) != actor_ref['incarnation']:
            raise RuntimeError(
                f'待抽取玩家 NPC #{actor.id} incarnation 已变化，无法进入审核隔离'
            )
        if int(actor.active_expedition_log_id or 0) != int(expedition_log_id):
            raise RuntimeError(
                f'待抽取玩家 NPC #{actor.id} 已不属于本次探险，无法进入审核隔离'
            )
        if actor.status != 'active':
            raise RuntimeError(
                f'待抽取玩家 NPC #{actor.id} 状态为 {actor.status}，无法进入审核隔离'
            )
        actor.status = 'review_pending'
        actor.active_lease_expires_at = None
        held_actor_refs.append(actor_ref)
    return held_actor_refs


def validate_waiting_npc_updates_strict(
    value,
    *,
    snapshot: dict | None,
    events: list | None,
) -> tuple[list[dict], list[str]]:
    """只接受冻结候选、精确 incarnation 与事件索引；原文摘录由后端派生。"""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['waiting_npc_updates 必须是数组']
    if not isinstance(snapshot, dict) or not snapshot.get('enabled'):
        return ([], [] if not value else ['未启用待抽取玩家 NPC 时不得输出 waiting_npc_updates'])
    event_rows = events if isinstance(events, list) else []
    candidate_map = {
        int(row['actor_id']): row
        for row in (snapshot.get('candidates') or [])
        if isinstance(row, dict) and isinstance(row.get('actor_id'), int)
    }
    allowed_factions = set(_normalize_faction_keys(snapshot.get('allowed_faction_keys')))
    max_records = max(1, int(snapshot.get('record_limit') or _record_limit()))
    max_chars = max(1, int(snapshot.get('record_max_chars') or _record_max_chars()))
    normalized: list[dict] = []
    errors: list[str] = []
    seen_actor_ids: set[int] = set()
    for update_index, raw_update in enumerate(value[:len(candidate_map) + 1]):
        path = f'waiting_npc_updates[{update_index}]'
        if not isinstance(raw_update, dict):
            errors.append(f'{path} 必须是对象')
            continue
        raw_actor_id = raw_update.get('actor_id')
        raw_incarnation = raw_update.get('incarnation')
        actor_id, _ = normalize_ascii_integer(raw_actor_id, minimum=1)
        if type(actor_id) is not int:
            errors.append(f'{path}.actor_id 必须是正整数')
            continue
        candidate = candidate_map.get(actor_id)
        if candidate is None:
            errors.append(f'{path}.actor_id 不在本次冻结候选中')
            continue
        if actor_id in seen_actor_ids:
            errors.append(f'{path}.actor_id 重复')
            continue
        seen_actor_ids.add(actor_id)
        incarnation, _ = normalize_ascii_integer(raw_incarnation, minimum=0)
        if (
            type(incarnation) is not int
            or incarnation != int(candidate.get('incarnation') or 0)
        ):
            errors.append(f'{path}.incarnation 与冻结候选不一致')
            continue
        if 'is_dead' not in raw_update:
            errors.append(f'{path}.is_dead 为必填布尔值')
            continue
        is_dead, _ = normalize_exact_boolean(raw_update.get('is_dead'))
        if type(is_dead) is not bool:
            errors.append(f'{path}.is_dead 必须是布尔值')
            continue
        faction_key = raw_update.get('faction_key')
        if faction_key is not None and (
            not isinstance(faction_key, str) or faction_key not in allowed_factions
        ):
            errors.append(f'{path}.faction_key 不在本次允许势力 key 中')
            continue
        existing_faction = candidate.get('faction_key')
        if existing_faction and faction_key != existing_faction:
            errors.append(f'{path}.faction_key 不得改写已有势力归属')
            continue
        existing_faction_role = candidate.get('faction_role') or 'none'
        faction_role = raw_update.get('faction_role', 'none')
        if faction_role not in WAITING_NPC_FACTION_ROLES:
            errors.append(f'{path}.faction_role 非法')
            continue
        if (faction_key is None) != (faction_role == 'none'):
            errors.append(f'{path}.faction_key 与 faction_role 必须同时表达有/无归属')
            continue
        raw_records = raw_update.get('records')
        if not isinstance(raw_records, list) or not raw_records:
            errors.append(f'{path}.records 必须是非空数组')
            continue
        if len(raw_records) > max_records:
            errors.append(f'{path}.records 超过上限 {max_records}')
            continue
        records: list[dict] = []
        seen_records: set[tuple[str, int]] = set()
        for record_index, raw_record in enumerate(raw_records):
            record_path = f'{path}.records[{record_index}]'
            if not isinstance(raw_record, dict):
                errors.append(f'{record_path} 必须是对象')
                continue
            kind = raw_record.get('kind')
            event_index, _ = normalize_ascii_integer(
                raw_record.get('event_index'), minimum=0,
            )
            if kind not in WAITING_NPC_RECORD_KINDS:
                errors.append(f'{record_path}.kind 非法')
                continue
            if (
                type(event_index) is not int
                or not 0 <= event_index < len(event_rows)
            ):
                errors.append(f'{record_path}.event_index 越界')
                continue
            event_text = event_rows[event_index]
            if not isinstance(event_text, str) or not event_text.strip():
                errors.append(f'{record_path}.event_index 未指向有效 event')
                continue
            excerpt = _derive_waiting_npc_excerpt(
                event_text,
                display_name=candidate.get('display_name'),
                max_chars=max_chars,
            )
            if not excerpt:
                errors.append(f'{record_path}.event_index 未生成有效原文摘录')
                continue
            dedupe_key = (kind, event_index)
            if dedupe_key in seen_records:
                errors.append(f'{record_path} 重复')
                continue
            seen_records.add(dedupe_key)
            records.append({
                'kind': kind,
                'event_index': event_index,
                'excerpt': excerpt,
            })
        if (
            (faction_key and not existing_faction)
            or (existing_faction and faction_role != existing_faction_role)
        ) and not any(row['kind'] == 'affiliation' for row in records):
            errors.append(f'{path} 势力归属或身份变化时必须含 affiliation 原文记录')
        if is_dead and not any(row['kind'] == 'death' for row in records):
            errors.append(f'{path} 标记死亡时必须含 death 原文记录')
        profile, profile_errors = validate_waiting_npc_profile(
            raw_update.get('profile'),
            path=f'{path}.profile',
        )
        errors.extend(profile_errors)
        if records and not profile_errors:
            normalized.append({
                'actor_id': actor_id,
                'incarnation': incarnation,
                'is_dead': is_dead,
                'faction_key': faction_key,
                'faction_role': faction_role,
                'records': records,
                'profile': profile,
            })
    if len(value) > len(candidate_map):
        errors.append('waiting_npc_updates 数量不得超过本次冻结候选数')
    return normalized, errors


def persist_completed_waiting_npc_updates(
    expedition_log_id: int,
    full_log: dict,
) -> dict:
    """在探险完成事务内幂等保存实际出场者的原文档案与当前势力归属。"""
    quarantine = full_log.get('waiting_npc_review_quarantine')
    if (
        isinstance(quarantine, dict)
        and quarantine.get('status') == 'quarantined'
    ):
        return {
            'status': 'quarantined',
            'reason_code': str(
                quarantine.get('reason_code')
                or 'waiting_npc_review_pending'
            ),
            'persisted_actor_ids': [],
            'death_actor_ids': [],
        }
    snapshot = full_log.get('waiting_player_npc_injection')
    updates, errors = validate_waiting_npc_updates_strict(
        full_log.get('waiting_npc_updates'),
        snapshot=snapshot if isinstance(snapshot, dict) else None,
        events=full_log.get('events'),
    )
    if errors:
        raise RuntimeError('待抽取玩家 NPC 结算非法: ' + '；'.join(errors))
    actor_ids = [row['actor_id'] for row in updates]
    frozen_actor_ids = [
        int(row['actor_id'])
        for row in ((snapshot or {}).get('candidates') or [])
        if isinstance(row, dict)
        and isinstance(row.get('actor_id'), int)
        and not isinstance(row.get('actor_id'), bool)
    ]
    locked_actor_ids = list(dict.fromkeys([*actor_ids, *frozen_actor_ids]))
    actors = (
        AdventurerS1WaitingNpcActor.query
        .filter(AdventurerS1WaitingNpcActor.id.in_(locked_actor_ids))
        .with_for_update()
        .all()
    )
    actor_map = {int(actor.id): actor for actor in actors}
    for actor in actors:
        if (
            actor.status == 'review_pending'
            and int(actor.active_expedition_log_id or 0) == int(expedition_log_id)
        ):
            actor.status = 'active'
    existing_lives = (
        {
            (int(actor_id), int(incarnation))
            for actor_id, incarnation in (
                db.session.query(
                    AdventurerS1WaitingNpcRecord.actor_id,
                    AdventurerS1WaitingNpcRecord.incarnation,
                )
                .filter(
                    AdventurerS1WaitingNpcRecord.expedition_log_id
                    == int(expedition_log_id),
                    AdventurerS1WaitingNpcRecord.actor_id.in_(actor_ids),
                )
                .distinct()
                .all()
            )
        }
        if actor_ids else set()
    )
    persisted: list[int] = []
    deaths: list[int] = []
    ignored_materialized_actor_ids: list[int] = []
    now = _portable_utc_now()
    for update in updates:
        key = (update['actor_id'], update['incarnation'])
        if key in existing_lives:
            continue
        actor = actor_map.get(update['actor_id'])
        if not actor:
            raise RuntimeError(f'待抽取玩家 NPC #{update["actor_id"]} 不存在')
        if actor.status == 'materialized':
            # 剧情使用的是冻结 NPC 快照；正式抽取完成后到达的添头结算不得
            # 改写 actor 档案，更不能反向影响已经创建的正式冒险者。
            ignored_materialized_actor_ids.append(int(actor.id))
            continue
        if int(actor.current_incarnation or 1) != update['incarnation']:
            raise RuntimeError(f'待抽取玩家 NPC #{actor.id} incarnation 已变化')
        for record_index, record in enumerate(update['records']):
            db.session.add(AdventurerS1WaitingNpcRecord(
                actor_id=int(actor.id),
                incarnation=update['incarnation'],
                expedition_log_id=int(expedition_log_id),
                event_index=record['event_index'],
                record_index=record_index,
                kind=record['kind'],
                excerpt=record['excerpt'],
            ))
        actor.appearance_count = int(actor.appearance_count or 0) + 1
        actor.last_appeared_at = now
        actor.faction_key = update['faction_key']
        actor.faction_role = (
            update['faction_role'] if update['faction_key'] else None
        )
        actor.current_profile = update['profile']
        persisted.append(int(actor.id))
        if update['is_dead']:
            deaths.append(int(actor.id))
            if actor.status == 'active':
                actor.current_incarnation = int(actor.current_incarnation or 1) + 1
                actor.faction_key = None
                actor.faction_role = None
                actor.current_profile = _empty_profile()
    for actor in actors:
        if int(actor.active_expedition_log_id or 0) == int(expedition_log_id):
            actor.active_expedition_log_id = None
            actor.active_lease_expires_at = None
    result = {
        'persisted_actor_ids': persisted,
        'death_actor_ids': deaths,
    }
    if ignored_materialized_actor_ids:
        result['ignored_materialized_actor_ids'] = ignored_materialized_actor_ids
    return result


def _build_inherited_history_entry(
    actor: AdventurerS1WaitingNpcActor,
    records: list[AdventurerS1WaitingNpcRecord],
) -> str | None:
    excerpts = list(dict.fromkeys(
        str(row.excerpt or '').strip()
        for row in records
        if str(row.excerpt or '').strip()
    ))
    if not excerpts:
        return None
    heading = (
        f'【抽取前经历 · 路人档案 #{int(actor.id)}-'
        f'{int(actor.current_incarnation or 1)}】'
    )
    return heading + '\n' + '\n'.join(f'• {excerpt}' for excerpt in excerpts)


def materialize_waiting_npc_actor_without_inheritance(
    reservation_id: int,
    adventurer_id: int,
) -> None:
    """NPC 来源继承失败时仍封存 actor；正式基础抽取必须继续完成。"""
    actor = (
        AdventurerS1WaitingNpcActor.query
        .filter_by(reservation_id=int(reservation_id))
        .with_for_update()
        .first()
    )
    if actor is None or actor.status == 'materialized':
        return
    actor.status = 'materialized'
    actor.active_expedition_log_id = None
    actor.active_lease_expires_at = None
    actor.materialized_adventurer_id = int(adventurer_id)
    actor.materialized_at = _portable_utc_now()


def apply_waiting_npc_origin_to_adventurer(
    reservation_id: int,
    adventurer: AdventurerS1,
    *,
    existing_equipment_names: set[str] | None = None,
    existing_skill_names: set[str] | None = None,
) -> AdventurerS1WaitingNpcMaterialization | None:
    """在预约抽取事务内，把当前 incarnation 的有界投影精确落到新冒险者。"""
    actor = (
        AdventurerS1WaitingNpcActor.query
        .filter_by(reservation_id=int(reservation_id))
        .with_for_update()
        .first()
    )
    if actor is None and adventurer.owner_id is not None:
        reusable_actors = (
            AdventurerS1WaitingNpcActor.query
            .filter_by(
                user_id=int(adventurer.owner_id),
                status='active',
                materialized_adventurer_id=None,
            )
            .order_by(
                AdventurerS1WaitingNpcActor.updated_at.desc(),
                AdventurerS1WaitingNpcActor.id.desc(),
            )
            .with_for_update()
            .all()
        )
        linked_reservation_ids = {
            int(row.reservation_id) for row in reusable_actors if row.reservation_id
        }
        pending_link_ids = {
            int(linked_id)
            for (linked_id,) in (
                db.session.query(AdventurerS1Reservation.id)
                .filter(
                    AdventurerS1Reservation.id.in_(linked_reservation_ids),
                    AdventurerS1Reservation.status == 'pending',
                    AdventurerS1Reservation.id != int(reservation_id),
                )
                .all()
                if linked_reservation_ids else []
            )
        }
        actor = next(
            (
                row for row in reusable_actors
                if int(row.reservation_id or 0) not in pending_link_ids
            ),
            None,
        )
        if actor is not None:
            actor.reservation_id = int(reservation_id)
    if not actor:
        return None
    if actor.status not in ('active', 'review_pending'):
        raise RuntimeError('待抽取玩家 NPC 已经完成过实体化')
    if actor.active_expedition_log_id:
        # 正式抽取不等待可选 NPC 剧情。当前事务持有 actor 行锁，以最近一次
        # 已完成持久化的档案创建冒险者，并切断旧冻结剧情对正式实体的写权。
        actor.active_expedition_log_id = None
        actor.active_lease_expires_at = None

    raw_profile = actor.current_profile if isinstance(actor.current_profile, dict) else {}
    if raw_profile:
        profile, profile_errors = validate_waiting_npc_profile(
            raw_profile,
            path='stored_waiting_npc_profile',
        )
        if profile_errors:
            raise RuntimeError('待抽取玩家 NPC 档案非法: ' + '；'.join(profile_errors))
    else:
        profile = _empty_profile()

    incarnation = int(actor.current_incarnation or 1)
    records = (
        AdventurerS1WaitingNpcRecord.query
        .filter_by(actor_id=int(actor.id), incarnation=incarnation)
        .order_by(
            AdventurerS1WaitingNpcRecord.created_at.desc(),
            AdventurerS1WaitingNpcRecord.id.desc(),
        )
        .limit(_record_limit())
        .all()
    )
    if actor.faction_key and not any(row.kind == 'affiliation' for row in records):
        affiliation_row = (
            AdventurerS1WaitingNpcRecord.query
            .filter_by(
                actor_id=int(actor.id),
                incarnation=incarnation,
                kind='affiliation',
            )
            .order_by(
                AdventurerS1WaitingNpcRecord.created_at.desc(),
                AdventurerS1WaitingNpcRecord.id.desc(),
            )
            .first()
        )
        if affiliation_row is not None:
            if len(records) >= _record_limit():
                records[-1] = affiliation_row
            else:
                records.append(affiliation_row)
    records.reverse()
    history_entry = _build_inherited_history_entry(actor, records)

    has_profile = bool(
        profile.get('character_class')
        or profile.get('equipments')
        or profile.get('skills')
        or profile.get('traits')
    )
    if not history_entry and not has_profile:
        actor.status = 'materialized'
        actor.active_expedition_log_id = None
        actor.active_lease_expires_at = None
        actor.materialized_adventurer_id = int(adventurer.id)
        actor.materialized_at = _portable_utc_now()
        return None

    equipment_by_name = {
        row.name: row
        for row in AdventurerS1Equipment.query.filter_by(
            adventurer_id=int(adventurer.id),
        ).all()
    }
    equipment_names = set(existing_equipment_names or ()) | set(equipment_by_name)
    skill_names = set(existing_skill_names or ())
    created_equipment_rows: list[AdventurerS1Equipment] = []
    inherited_equipments: list[tuple[AdventurerS1Equipment, int, int]] = []
    created_skills: list[AdventurerS1Skill] = []
    created_traits: list[AdventurerS1Trait] = []
    for row in profile['equipments']:
        if row['name'] in equipment_names:
            existing_equipment = equipment_by_name.get(row['name'])
            if existing_equipment is not None:
                quantity = int(row['quantity'])
                baseline_quantity = int(existing_equipment.quantity or 0)
                existing_equipment.quantity = baseline_quantity + quantity
                inherited_equipments.append(
                    (existing_equipment, quantity, baseline_quantity)
                )
            continue
        equipment_names.add(row['name'])
        quantity = int(row['quantity'])
        equipment = AdventurerS1Equipment(
            adventurer_id=int(adventurer.id),
            name=row['name'],
            type=row['type'],
            rarity=row['rarity'],
            custom_desc=row['desc'] or None,
            quantity=quantity,
        )
        created_equipment_rows.append(equipment)
        inherited_equipments.append((equipment, quantity, 0))
    for row in profile['skills']:
        if row['name'] in skill_names:
            continue
        skill_names.add(row['name'])
        created_skills.append(AdventurerS1Skill(
            adventurer_id=int(adventurer.id),
            name=row['name'],
            description=row['desc'],
        ))
    for row in profile['traits']:
        created_traits.append(AdventurerS1Trait(
            adventurer_id=int(adventurer.id),
            name=row['name'],
            description=row['desc'],
        ))
    db.session.add_all(
        created_equipment_rows + created_skills + created_traits
    )
    db.session.flush()

    inherited_class = profile.get('character_class')
    if inherited_class:
        adventurer.character_class = inherited_class
    if history_entry:
        old_history = str(adventurer.history_story or '').strip()
        adventurer.history_story = (
            f'{old_history}\n\n{history_entry}' if old_history else history_entry
        )

    ledger = AdventurerS1WaitingNpcMaterialization(
        actor_id=int(actor.id),
        incarnation=incarnation,
        adventurer_id=int(adventurer.id),
        profile_snapshot=profile,
        inherited_history_entry=history_entry,
        inherited_character_class=inherited_class,
        equipment_ids=[
            {
                'id': int(row.id),
                'quantity': quantity,
                'baseline_quantity': baseline_quantity,
            }
            for row, quantity, baseline_quantity in inherited_equipments
        ],
        skill_ids=[int(row.id) for row in created_skills],
        trait_ids=[int(row.id) for row in created_traits],
        status='applied',
    )
    db.session.add(ledger)
    actor.status = 'materialized'
    actor.active_expedition_log_id = None
    actor.active_lease_expires_at = None
    actor.materialized_adventurer_id = int(adventurer.id)
    actor.materialized_at = _portable_utc_now()
    return ledger


def _waiting_npc_origin_reset_blocked_reason(
    adventurer: AdventurerS1,
) -> str | None:
    """仅允许尚未探险、尚未接触交易市场的新抽取角色重置来源。"""
    can_dispatch, dispatch_reason = adventurer.can_dispatch()
    if not can_dispatch:
        return f'{dispatch_reason or "冒险者当前忙碌"}，暂时不能另启新生'

    adventurer_id = int(adventurer.id)
    has_expedition = db_manager.query(exists().where(
        AdventurerS1ExpeditionLogMember.adventurer_id == adventurer_id,
    )).scalar()
    if has_expedition:
        return '已有探险经历，不能另启新生'

    has_market_activity = db_manager.query(or_(
        exists().where(
            AdventurerS1MarketListing.seller_adventurer_id == adventurer_id,
        ),
        exists().where(
            AdventurerS1MarketListing.buyer_adventurer_id == adventurer_id,
        ),
        exists().where(
            AdventurerS1MarketOffer.buyer_adventurer_id == adventurer_id,
        ),
        exists().where(
            AdventurerS1MarketBuyOrder.buyer_adventurer_id == adventurer_id,
        ),
        exists().where(
            AdventurerS1MarketBuyOffer.seller_adventurer_id == adventurer_id,
        ),
    )).scalar()
    if has_market_activity:
        return '已有交易市场操作记录，不能另启新生'
    return None


def get_waiting_npc_origin_state(
    adventurer_id: int,
    user_id: int,
) -> tuple[dict | None, str | None]:
    # 状态查询只需要所有权和派遣占位，避免默认 selectin 拉取角色全部资产。
    adventurer = (
        AdventurerS1.query.options(
            load_only(
                AdventurerS1.id, AdventurerS1.owner_id,
                AdventurerS1.status, AdventurerS1.building_instance_id,
            ),
            raiseload('*'),
            joinedload(AdventurerS1.building_instance).options(
                load_only(AdventurerS1BuildingInstance.status),
                joinedload(AdventurerS1BuildingInstance.template).load_only(
                    AdventurerS1BuildingTemplate.handler_key,
                    AdventurerS1BuildingTemplate.code,
                ),
            ),
        )
        .filter_by(id=int(adventurer_id), owner_id=int(user_id))
        .first()
    )
    if not adventurer or int(adventurer.owner_id or 0) != int(user_id):
        return None, '冒险者不存在或不属于你'
    ledger = AdventurerS1WaitingNpcMaterialization.query.filter_by(
        adventurer_id=int(adventurer_id),
    ).first()
    if not ledger:
        return {
            'has_origin': False,
            'can_reset': False,
            'status': None,
        }, None
    blocked_reason = (
        _waiting_npc_origin_reset_blocked_reason(adventurer)
        if ledger.status == 'applied' else None
    )
    profile = ledger.profile_snapshot if isinstance(ledger.profile_snapshot, dict) else {}
    return {
        'has_origin': True,
        'can_reset': ledger.status == 'applied' and blocked_reason is None,
        'status': ledger.status,
        'blocked_reason': blocked_reason,
        'incarnation': int(ledger.incarnation),
        'inherited': {
            'character_class': ledger.inherited_character_class,
            'equipment_count': len(ledger.equipment_ids or []),
            'skill_count': len(ledger.skill_ids or []),
            'trait_count': len(ledger.trait_ids or []),
            'has_history': bool(ledger.inherited_history_entry),
            'profile': profile,
        },
        'reset_at': ledger.reset_at.isoformat() + 'Z' if ledger.reset_at else None,
    }, None


def _remove_exact_history_entry(history: str, inherited_entry: str) -> str:
    blocks = str(history or '').split('\n\n')
    removed = False
    kept: list[str] = []
    for block in blocks:
        if not removed and block == inherited_entry:
            removed = True
            continue
        if block:
            kept.append(block)
    return '\n\n'.join(kept)


def reset_waiting_npc_origin(
    adventurer_id: int,
    user_id: int,
) -> tuple[dict | None, str | None]:
    """只撤销来源账本记录的 NPC 继承项；普通初始项与其他来源保持不动。"""
    result: dict | None = None
    try:
        with db_manager.transaction():
            adventurer = (
                AdventurerS1.query
                .filter_by(id=int(adventurer_id))
                .with_for_update()
                .first()
            )
            if not adventurer or int(adventurer.owner_id or 0) != int(user_id):
                return None, '冒险者不存在或不属于你'
            ledger = (
                AdventurerS1WaitingNpcMaterialization.query
                .filter_by(adventurer_id=int(adventurer_id))
                .with_for_update()
                .first()
            )
            if not ledger:
                return None, '这个冒险者没有抽取前 NPC 经历'
            if ledger.status != 'applied':
                return None, '已经另启新生，无需重复清除'
            blocked_reason = _waiting_npc_origin_reset_blocked_reason(adventurer)
            if blocked_reason:
                return None, blocked_reason

            removed_equipment = 0
            for ref in ledger.equipment_ids or []:
                if not isinstance(ref, dict):
                    continue
                equipment_id = ref.get('id')
                inherited_quantity = ref.get('quantity')
                baseline_quantity = ref.get('baseline_quantity', 0)
                if (
                    isinstance(equipment_id, bool)
                    or not isinstance(equipment_id, int)
                    or isinstance(inherited_quantity, bool)
                    or not isinstance(inherited_quantity, int)
                    or inherited_quantity < 1
                    or isinstance(baseline_quantity, bool)
                    or not isinstance(baseline_quantity, int)
                    or baseline_quantity < 0
                ):
                    continue
                equipment = (
                    AdventurerS1Equipment.query
                    .filter_by(id=equipment_id, adventurer_id=int(adventurer_id))
                    .with_for_update()
                    .first()
                )
                if not equipment:
                    continue
                current_quantity = max(0, int(equipment.quantity or 0))
                removable_quantity = max(0, current_quantity - baseline_quantity)
                removed_quantity = min(removable_quantity, inherited_quantity)
                remaining_quantity = current_quantity - removed_quantity
                if remaining_quantity > 0:
                    equipment.quantity = remaining_quantity
                else:
                    db.session.delete(equipment)
                removed_equipment += removed_quantity

            skill_ids = [
                row for row in (ledger.skill_ids or [])
                if isinstance(row, int) and not isinstance(row, bool)
            ]
            trait_ids = [
                row for row in (ledger.trait_ids or [])
                if isinstance(row, int) and not isinstance(row, bool)
            ]
            removed_skills = (
                AdventurerS1Skill.query
                .filter(
                    AdventurerS1Skill.adventurer_id == int(adventurer_id),
                    AdventurerS1Skill.id.in_(skill_ids),
                )
                .delete(synchronize_session=False)
                if skill_ids else 0
            )
            removed_traits = (
                AdventurerS1Trait.query
                .filter(
                    AdventurerS1Trait.adventurer_id == int(adventurer_id),
                    AdventurerS1Trait.id.in_(trait_ids),
                )
                .delete(synchronize_session=False)
                if trait_ids else 0
            )
            removed_class = False
            if (
                ledger.inherited_character_class
                and adventurer.character_class == ledger.inherited_character_class
            ):
                adventurer.character_class = None
                removed_class = True
            removed_history = False
            if ledger.inherited_history_entry:
                new_history = _remove_exact_history_entry(
                    adventurer.history_story,
                    ledger.inherited_history_entry,
                )
                removed_history = new_history != str(adventurer.history_story or '')
                adventurer.history_story = new_history

            ledger.status = 'reset'
            ledger.reset_at = _portable_utc_now()
            result = {
                'status': 'reset',
                'removed': {
                    'equipment_quantity': int(removed_equipment),
                    'skills': int(removed_skills or 0),
                    'traits': int(removed_traits or 0),
                    'character_class': removed_class,
                    'history': removed_history,
                },
            }
    except Exception as exc:
        return None, player_operation_failure(exc, '另启新生失败')

    from plugins.adventurer_s1.services.adventurer_s1_home_service import (
        invalidate_s1_init_cache,
    )
    from plugins.adventurer_s1.services.adventurer_s1_service import (
        invalidate_adventurer_detail_cache,
    )
    invalidate_adventurer_detail_cache(int(adventurer_id))
    invalidate_s1_init_cache(int(user_id))
    return result, None
