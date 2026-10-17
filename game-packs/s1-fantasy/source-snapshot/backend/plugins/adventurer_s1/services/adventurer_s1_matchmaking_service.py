"""S1 匹配队列：报名 → 按排队时间成团 → 复用现有 Party/探险发车链路。"""
from runtime_core.time import utc_now as _portable_utc_now
import hashlib
from flask import g, has_request_context


def _match_min_size():
    """Read the tavern setting once per HTTP request; ticks read it afresh."""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    cache_key = '_s1_match_min_size'
    if has_request_context() and hasattr(g, cache_key):
        return getattr(g, cache_key)
    value = int(S1SettingsService.get('party_min_size'))
    if has_request_context():
        setattr(g, cache_key, value)
    return value

import random
import secrets
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import joinedload, load_only, noload, selectinload

from db_repository import db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1DormRoom,
    AdventurerS1Follow,
    AdventurerS1FateJourney,
    AdventurerS1MatchInvite,
    AdventurerS1MatchTicket,
    AdventurerS1MatchTicketMember,
    AdventurerS1MatchTicketRejoinCooldown,
    AdventurerS1Party,
    AdventurerS1PartyMember,
)
from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_ACTIVE_DUNGEONS,
    S1_DUNGEON_RANK_MAP,
    check_s1_active_dungeon_select,
    check_s1_join_dungeon_rank_span,
    format_s1_dungeon_level_desc,
    get_s1_active_selectable_ranks,
    get_s1_joinable_ranks,
    get_s1_special_dungeon,
    get_s1_dungeon_rank,
)
from plugins.adventurer_s1.services.adventurer_s1_party_service import (
    _int_cfg,
)
from plugins.adventurer_s1.services.adventurer_s1_state_transition_service import (
    AdventurerS1StateTransitionCoordinator,
)
from plugins.adventurer_s1.services.adventurer_s1_board_version_service import (
    bump_board_version,
    get_board_version,
)
from plugins.adventurer_s1.services.s1_world_location_state_service import (
    S1WorldLocationStateService,
)
from utils.cache_manager import CacheManager
from utils.time_utils import to_utc_iso
from .s1_campaign_task_admission_service import (
    S1CampaignTaskAdmissionService as TaskAdmission,
    notify_task_cancellations_after_commit, task_departure_deadline, timeout_notice,
)

_MATCHMAKING_MY_CACHE_TTL_SECONDS = 60
_MATCHMAKING_PUBLIC_CACHE_TTL_SECONDS = 60
_MATCHMAKING_DUNGEONS_CACHE_TTL_SECONDS = 60
_MATCHMAKING_QUEUE_AHEAD_CACHE_TTL_SECONDS = 60
_MATCHMAKING_INVITES_CACHE_TTL_SECONDS = 60
_DUPLICATE_QUEUED_MEMBER_REMOVED = '检测到重复报名，已自动移出后出现队伍中的重复成员'
_EMPTY_AFTER_DUPLICATE_REMOVAL_ERROR = '重复报名成员已自动移出；报名无剩余成员，已取消'

_my_tickets_cache = CacheManager(
    ttl_seconds=_MATCHMAKING_MY_CACHE_TTL_SECONDS, max_size=500,
)
_my_invites_cache = CacheManager(
    ttl_seconds=_MATCHMAKING_INVITES_CACHE_TTL_SECONDS, max_size=500,
)
_public_parties_cache = CacheManager(
    ttl_seconds=_MATCHMAKING_PUBLIC_CACHE_TTL_SECONDS, max_size=500,
)
_available_dungeons_cache = CacheManager(
    ttl_seconds=_MATCHMAKING_DUNGEONS_CACHE_TTL_SECONDS, max_size=500,
)
_queue_ahead_cache = CacheManager(
    ttl_seconds=_MATCHMAKING_QUEUE_AHEAD_CACHE_TTL_SECONDS, max_size=4,
)
_queued_count_cache = CacheManager(
    ttl_seconds=_MATCHMAKING_PUBLIC_CACHE_TTL_SECONDS, max_size=4,
)


def _invalidate_matchmaking_shared_cache() -> None:
    """全服共享块：公开招募板 / 队列计数 / 排队位次。"""
    _queue_ahead_cache.clear()
    _public_parties_cache.clear()
    _queued_count_cache.clear()
    bump_board_version('matchmaking')


def invalidate_matchmaking_read_cache(
    *,
    ticket_user_ids: Optional[Iterable[int]] = None,
    invite_user_ids: Optional[Iterable[int]] = None,
) -> None:
    """匹配读路径缓存失效；写路径 / scheduler 成团后调用。"""
    _invalidate_matchmaking_shared_cache()
    ticket_ids = list(ticket_user_ids or [])
    invite_ids = list(invite_user_ids or [])
    if not ticket_ids and not invite_ids:
        _my_tickets_cache.clear()
        _my_invites_cache.clear()
        _available_dungeons_cache.clear()
        return
    for uid in set(ticket_ids):
        _my_tickets_cache.delete(uid)
        _available_dungeons_cache.delete(f'board_user:{uid}')
    for uid in set(invite_ids):
        _my_invites_cache.delete(uid)


def _after_matchmaking_mutation(
    *,
    ticket_user_ids: Optional[Iterable[int]] = None,
    invite_user_ids: Optional[Iterable[int]] = None,
    shared_only: bool = False,
) -> None:
    if shared_only:
        _invalidate_matchmaking_shared_cache()
        return
    invalidate_matchmaking_read_cache(
        ticket_user_ids=ticket_user_ids,
        invite_user_ids=invite_user_ids,
    )


_ACTIVE_DUNGEON_NAMES = {d['name'] for d in S1_ACTIVE_DUNGEONS}


def _normalize_dungeon(value: str | None) -> str | None:
    dungeon = str(value or '').strip()
    return dungeon or None


def _validate_preferred_dungeon(dungeon: str | None, *, campaign_dispatch_id=None) -> str | None:
    if not dungeon:
        return None
    if get_s1_special_dungeon(dungeon):
        return '特殊门票副本仍需使用特殊机缘组队入口'
    if dungeon not in _ACTIVE_DUNGEON_NAMES:
        from .s1_campaign_world_service import campaign_world
        if campaign_dispatch_id and campaign_world(dungeon):
            return None
        return '未知或未开放的匹配地图'
    return None


def _closed_dungeon_error(dungeons: Iterable[str], closed_dungeons: set[str]) -> str | None:
    selected = {str(value or '').strip() for value in dungeons if str(value or '').strip()}
    blocked = sorted(selected & closed_dungeons)
    if selected and selected <= closed_dungeons:
        return f'地图「{"、".join(blocked)}」当前没有可正常进入的探索路径，请改选其他地图'
    return None


def _normalize_preferred_dungeons(values: object, legacy_dungeon: str | None = None) -> list[str] | None:
    """规范化单排候选地图；None 表示接受所有可进入的普通地图。"""
    if values is None:
        dungeon = _normalize_dungeon(legacy_dungeon)
        return [dungeon] if dungeon else None
    if not isinstance(values, list):
        raise ValueError('匹配地图列表格式错误')
    normalized: list[str] = []
    for value in values:
        dungeon = _normalize_dungeon(value)
        if not dungeon:
            continue
        if dungeon not in normalized:
            normalized.append(dungeon)
    return normalized or None


def _ticket_preferred_dungeons(ticket: AdventurerS1MatchTicket) -> set[str] | None:
    values = getattr(ticket, 'preferred_dungeons', None)
    if isinstance(values, list) and values:
        return {str(value) for value in values if str(value or '').strip()}
    return {ticket.preferred_dungeon} if ticket.preferred_dungeon else None


def _active_ticket_for_adventurer(adventurer_id: int) -> AdventurerS1MatchTicket | None:
    return (
        AdventurerS1MatchTicket.query
        .join(AdventurerS1MatchTicketMember)
        .filter(
            AdventurerS1MatchTicket.status.in_(['forming', 'queued']),
            AdventurerS1MatchTicketMember.adventurer_id == adventurer_id,
        )
        .first()
    )


def _allowed_ranks_for_adventurer(adv: AdventurerS1) -> set[int]:
    return get_s1_joinable_ranks(adv)


def _allowed_ranks_for_ticket(ticket: AdventurerS1MatchTicket) -> set[int]:
    allowed: set[int] | None = None
    for member in ticket.members:
        adv = member.adventurer
        if not adv:
            return set()
        ranks = _allowed_ranks_for_adventurer(adv)
        allowed = ranks if allowed is None else allowed & ranks
    return allowed or set()


def _ticket_member_count(ticket: AdventurerS1MatchTicket) -> int:
    return len(ticket.members)


def _active_fate_dungeons_by_adventurer_ids(adventurer_ids: Iterable[int]) -> dict[int, set[str]]:
    """批量读取进行中命途的关联地牢，供招募提示与匹配限制共用。"""
    ids = {int(adventurer_id) for adventurer_id in adventurer_ids if adventurer_id}
    if not ids:
        return {}
    rows = (
        AdventurerS1FateJourney.query
        .filter(
            AdventurerS1FateJourney.adventurer_id.in_(ids),
            AdventurerS1FateJourney.status.in_(('pursuing', 'final_ready')),
        )
        .with_entities(
            AdventurerS1FateJourney.adventurer_id,
            AdventurerS1FateJourney.candidate_dungeons,
        )
        .all()
    )
    return {
        int(adventurer_id): {
            str(dungeon).strip()
            for dungeon in (candidate_dungeons or [])
            if str(dungeon or '').strip()
        }
        for adventurer_id, candidate_dungeons in rows
    }


def _ticket_fate_risk_member_names(
    ticket: AdventurerS1MatchTicket,
    fate_dungeons_by_adventurer_id: dict[int, set[str]],
) -> list[str]:
    target_dungeons = _ticket_preferred_dungeons(ticket)
    if not target_dungeons:
        return []
    return [
        member.adventurer.name
        for member in ticket.members
        if member.adventurer
        and target_dungeons & fate_dungeons_by_adventurer_id.get(member.adventurer_id, set())
    ]


def _blocked_fate_dungeons_for_low_level_members(
    tickets: list[AdventurerS1MatchTicket],
    fate_dungeons_by_adventurer_id: dict[int, set[str]],
) -> set[str]:
    """屏蔽低等级成员未确认、或由自动补位新引入的命途关联副本。"""
    min_level = _int_cfg('ADVENTURER_S1_FATE_SOLO_MATCH_MIN_LEVEL', 13)
    all_fate_dungeons = {
        dungeon
        for ticket in tickets
        for member in ticket.members
        for dungeon in fate_dungeons_by_adventurer_id.get(member.adventurer_id, set())
    }
    blocked: set[str] = set()
    for ticket in tickets:
        has_low_level_member = any(
            member.adventurer
            and int(member.adventurer.level or 1) < min_level
            for member in ticket.members
        )
        if not has_low_level_member:
            continue
        if ticket.queue_type == 'solo':
            blocked.update(all_fate_dungeons)
            continue

        own_member_ids = {member.adventurer_id for member in ticket.members}
        external_fate_dungeons = {
            dungeon
            for selected_ticket in tickets
            for member in selected_ticket.members
            if member.adventurer_id not in own_member_ids
            for dungeon in fate_dungeons_by_adventurer_id.get(member.adventurer_id, set())
        }
        blocked.update(external_fate_dungeons)

        own_fate_dungeons = {
            dungeon
            for member in ticket.members
            for dungeon in fate_dungeons_by_adventurer_id.get(member.adventurer_id, set())
        }
        fate_risk = _ticket_fate_risk_state(ticket, fate_dungeons_by_adventurer_id)
        confirmed_own_fate_dungeons = set()
        if fate_risk['risk_member_ids'] and not fate_risk['pending_member_ids']:
            confirmed_own_fate_dungeons = (
                _ticket_preferred_dungeons(ticket) or set()
            ) & own_fate_dungeons
        blocked.update(own_fate_dungeons - confirmed_own_fate_dungeons)
    return blocked


def _ticket_fate_risk_state(
    ticket: AdventurerS1MatchTicket,
    fate_dungeons_by_adventurer_id: dict[int, set[str]],
    additional_adventurers: Iterable[AdventurerS1] | None = None,
) -> dict:
    """返回当前目标下的命途风险及低等级成员确认状态。"""
    target_dungeons = _ticket_preferred_dungeons(ticket)
    adventurers_by_id = {
        member.adventurer_id: member.adventurer
        for member in ticket.members
        if member.adventurer
    }
    for adventurer in additional_adventurers or ():
        adventurers_by_id[adventurer.id] = adventurer
    adventurers = list(adventurers_by_id.values())
    risk_members = [
        adventurer
        for adventurer in adventurers
        if target_dungeons
        and target_dungeons & fate_dungeons_by_adventurer_id.get(adventurer.id, set())
    ]
    if not risk_members:
        return {
            'fingerprint': None,
            'risk_member_ids': [],
            'risk_member_names': [],
            'required_member_ids': [],
            'pending_member_ids': [],
            'pending_member_names': [],
        }
    min_level = _int_cfg('ADVENTURER_S1_FATE_SOLO_MATCH_MIN_LEVEL', 13)
    required_members = [
        adventurer
        for adventurer in adventurers
        if int(adventurer.level or 1) < min_level
    ]
    fingerprint_source = '|'.join([
        ','.join(sorted(target_dungeons or set())),
        ','.join(str(adv.id) for adv in sorted(risk_members, key=lambda row: row.id)),
    ])
    fingerprint = hashlib.sha256(fingerprint_source.encode('utf-8')).hexdigest()[:32]
    acknowledgements = ticket.fate_risk_acknowledgements or {}
    pending_members = [
        adv for adv in required_members
        if acknowledgements.get(str(adv.id)) != fingerprint
    ]
    return {
        'fingerprint': fingerprint,
        'risk_member_ids': [adv.id for adv in risk_members],
        'risk_member_names': [adv.name for adv in risk_members],
        'required_member_ids': [adv.id for adv in required_members],
        'pending_member_ids': [adv.id for adv in pending_members],
        'pending_member_names': [adv.name for adv in pending_members],
    }


def _cancel_pending_invites(
    *,
    ticket_id: int | None = None,
    ticket_ids: list[int] | None = None,
    target_adventurer_id: int | None = None,
    target_adventurer_ids: list[int] | None = None,
    exclude_invite_id: int | None = None,
) -> int:
    """批量取消 pending 邀请（单条 UPDATE，避免循环写库）。"""
    if (
        ticket_id is None
        and not ticket_ids
        and target_adventurer_id is None
        and not target_adventurer_ids
    ):
        return 0
    now = _portable_utc_now()
    q = AdventurerS1MatchInvite.query.filter(AdventurerS1MatchInvite.status == 'pending')
    if ticket_id is not None:
        q = q.filter(AdventurerS1MatchInvite.ticket_id == ticket_id)
    if ticket_ids:
        q = q.filter(AdventurerS1MatchInvite.ticket_id.in_(ticket_ids))
    if target_adventurer_id is not None:
        q = q.filter(AdventurerS1MatchInvite.target_adventurer_id == target_adventurer_id)
    if target_adventurer_ids:
        q = q.filter(AdventurerS1MatchInvite.target_adventurer_id.in_(target_adventurer_ids))
    if exclude_invite_id is not None:
        q = q.filter(AdventurerS1MatchInvite.id != exclude_invite_id)
    return q.update(
        {
            'status': 'cancelled',
            'responded_at': now,
            'updated_at': now,
        },
        synchronize_session=False,
    )


def _preferred_dungeons_compatible(
    current: set[str] | None,
    candidate: set[str] | None,
) -> tuple[bool, set[str] | None]:
    if current is None:
        return True, set(candidate) if candidate is not None else None
    if candidate is None:
        return True, set(current)
    common = current & candidate
    return bool(common), common


def _pick_random_dungeon(
    allowed_ranks: set[int],
    member_levels: list[int] | None = None,
    preferred_dungeons: set[str] | None = None,
    excluded_dungeons: set[str] | None = None,
    *, activity_dungeon: str | None = None,
) -> str | None:
    excluded = excluded_dungeons or set()
    if activity_dungeon:
        from .adventurer_s1_config_service import get_s1_dungeon_rank
        return (activity_dungeon if activity_dungeon in (preferred_dungeons or set())
                and get_s1_dungeon_rank(activity_dungeon) in allowed_ranks
                and activity_dungeon not in excluded else None)
    names = [
        d['name'] for d in S1_ACTIVE_DUNGEONS
        if S1_DUNGEON_RANK_MAP.get(d['name'], 0) in allowed_ranks
        and (preferred_dungeons is None or d['name'] in preferred_dungeons)
        and d['name'] not in excluded
    ]
    return random.choice(names) if names else None


def _member_levels_from_tickets(tickets: list[AdventurerS1MatchTicket]) -> list[int]:
    return [
        int(member.adventurer.level)
        for ticket in tickets
        for member in ticket.members
        if member.adventurer and member.adventurer.level is not None
    ]


def _ticket_members_can_dispatch(ticket: AdventurerS1MatchTicket) -> str | None:
    """校验匹配小队成员能否投入队列。

    队员在 forming 小队中 status 已是 in_party，不能再走 can_dispatch()（仅允许 resting/idle）。
    """
    _BLOCKED = {
        'exploring': '探险中',
        'building': '建造中',
        'upgrading': '升级中',
        'dead': '已死亡',
        'archived': '已封存清退',
    }
    for member in ticket.members:
        adv = member.adventurer
        if not adv:
            return '小队中存在不存在的冒险者'
        if adv.status in _BLOCKED:
            label = _BLOCKED[adv.status]
            return f'{adv.name} 冒险者当前{label}，无法执行新行为'
        if adv.status not in ('resting', 'idle', 'in_party'):
            return f'{adv.name} 冒险者当前{adv.status}，无法执行新行为'
        if adv.building_instance_id:
            return f'{adv.name} 冒险者当前忙碌，无法执行新行为'
        stamina_err = _check_stamina(adv)
        if stamina_err:
            return stamina_err
    return None


def _match_ticket_rejoin_cooldown_minutes() -> int:
    return _int_cfg('ADVENTURER_S1_PARTY_REJOIN_COOLDOWN_MINUTES', 5)


def _check_match_ticket_rejoin_cooldown(adventurer_id: int, ticket: AdventurerS1MatchTicket) -> str | None:
    if getattr(ticket, 'access_mode', 'public') != 'public':
        return None
    cooldown_mins = _match_ticket_rejoin_cooldown_minutes()
    if cooldown_mins <= 0:
        return None
    row = AdventurerS1MatchTicketRejoinCooldown.query.filter_by(
        adventurer_id=adventurer_id,
        ticket_id=ticket.id,
    ).first()
    if not row or row.blocked_until <= _portable_utc_now():
        return None
    remaining = max(1, int((row.blocked_until - _portable_utc_now()).total_seconds() // 60) + 1)
    return f'你刚退出过该公开小队，请 {remaining} 分钟后再加入'


def _record_match_ticket_leave_cooldown(adventurer_id: int, ticket: AdventurerS1MatchTicket) -> None:
    if getattr(ticket, 'access_mode', 'public') != 'public':
        return
    cooldown_mins = _match_ticket_rejoin_cooldown_minutes()
    if cooldown_mins <= 0:
        return
    blocked_until = _portable_utc_now() + timedelta(minutes=cooldown_mins)
    row = AdventurerS1MatchTicketRejoinCooldown.query.filter_by(
        adventurer_id=adventurer_id,
        ticket_id=ticket.id,
    ).first()
    if row:
        row.blocked_until = blocked_until
        row.updated_at = _portable_utc_now()
        return
    db_manager.add(AdventurerS1MatchTicketRejoinCooldown(
        adventurer_id=adventurer_id,
        ticket_id=ticket.id,
        blocked_until=blocked_until,
    ))


def _check_join_dungeon_for_adv(adv: AdventurerS1, preferred_dungeon: str | None, *, campaign_dispatch_id=None) -> str | None:
    """加入已有小队：指定地图最多允许挑战高 1 档；随机地图仍走档位交集。"""
    err = _validate_preferred_dungeon(preferred_dungeon, campaign_dispatch_id=campaign_dispatch_id)
    if err:
        return err
    if not preferred_dungeon:
        return None
    if not campaign_dispatch_id and S1_DUNGEON_RANK_MAP.get(preferred_dungeon) is None:
        return '未知或未开放的匹配地图'
    span_err = check_s1_join_dungeon_rank_span(adv, preferred_dungeon)
    if span_err:
        return f'{adv.name} 与「{preferred_dungeon}」档位差距过大，{span_err}'
    return None


def _ticket_match_error(
    ticket: AdventurerS1MatchTicket,
    *,
    closed_dungeons: set[str] | None = None,
) -> str | None:
    member_count = _ticket_member_count(ticket)
    if member_count <= 0:
        return '小队没有成员，无法进入匹配'
    if member_count > 6:
        return '小队人数超过上限'
    err = _ticket_members_can_dispatch(ticket)
    if err:
        return err
    preferred_dungeons = _ticket_preferred_dungeons(ticket)
    if preferred_dungeons:
        if closed_dungeons is None:
            closed_dungeons = S1WorldLocationStateService.closed_dungeon_names(
                preferred_dungeons,
            )
        world_err = _closed_dungeon_error(preferred_dungeons, closed_dungeons)
        if world_err:
            return world_err
        for member in ticket.members:
            adv = member.adventurer
            if not adv:
                return '小队中存在不存在的冒险者'
            for dungeon in preferred_dungeons:
                if adv.owner_id == ticket.owner_id:
                    err = _check_dungeon_for_adv(adv, dungeon, campaign_dispatch_id=ticket.campaign_dispatch_id)
                else:
                    err = _check_join_dungeon_for_adv(adv, dungeon, campaign_dispatch_id=ticket.campaign_dispatch_id)
                if err:
                    return err
    selected_ranks = _allowed_ranks_for_ticket(ticket)
    if preferred_dungeons:
        selected_ranks &= {get_s1_dungeon_rank(name) for name in preferred_dungeons}
    if not selected_ranks:
        return '小队成员与目标副本档位不兼容，无法进入匹配'
    return None


def _remove_duplicate_queued_members(
    tickets: list[AdventurerS1MatchTicket],
) -> tuple[list[AdventurerS1MatchTicket], bool]:
    """保留最早报名，并从后续票据中移除已经被占用的重复成员。"""
    claimed_adventurer_ids: set[int] = set()
    eligible_tickets: list[AdventurerS1MatchTicket] = []
    changed = False

    for ticket in tickets:
        duplicate_members = [
            member
            for member in ticket.members
            if member.adventurer_id in claimed_adventurer_ids
        ]
        if duplicate_members:
            removed_owner = any(
                member.adventurer and member.adventurer.owner_id == ticket.owner_id
                for member in duplicate_members
            )
            removed_adventurer_ids = {member.adventurer_id for member in duplicate_members}
            for member in duplicate_members:
                ticket.members.remove(member)
            _cancel_pending_invites(
                ticket_id=ticket.id,
                target_adventurer_ids=sorted(removed_adventurer_ids),
            )

            acknowledgements = dict(ticket.fate_risk_acknowledgements or {})
            for adventurer_id in removed_adventurer_ids:
                acknowledgements.pop(str(adventurer_id), None)
            ticket.fate_risk_acknowledgements = acknowledgements or None
            changed = True

            if not ticket.members:
                ticket.status = 'cancelled'
                ticket.match_error = _EMPTY_AFTER_DUPLICATE_REMOVAL_ERROR
                ticket.queued_at = None
                ticket.cancelled_at = _portable_utc_now()
                _cancel_pending_invites(ticket_id=ticket.id)
                continue

            if removed_owner:
                next_owner_id = next(
                    (
                        member.adventurer.owner_id
                        for member in sorted(
                            ticket.members,
                            key=lambda item: (item.created_at, item.id),
                        )
                        if member.adventurer and member.adventurer.owner_id is not None
                    ),
                    None,
                )
                if next_owner_id is None:
                    ticket.status = 'cancelled'
                    ticket.match_error = '重复报名成员已移出；剩余成员无人可接任队长，报名已取消'
                    ticket.queued_at = None
                    ticket.cancelled_at = _portable_utc_now()
                    _cancel_pending_invites(ticket_id=ticket.id)
                    continue
                ticket.owner_id = next_owner_id

            ticket.match_error = _DUPLICATE_QUEUED_MEMBER_REMOVED

        member_ids = {member.adventurer_id for member in ticket.members}
        claimed_adventurer_ids.update(member_ids)
        eligible_tickets.append(ticket)

    return eligible_tickets, changed


def _stop_ticket_for_match_error(ticket: AdventurerS1MatchTicket, error: str) -> None:
    ticket.match_error = error[:300]
    if ticket.queue_type == 'group':
        ticket.status = 'forming'
        ticket.queued_at = None
    else:
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

        ticket.status = 'cancelled'
        ticket.cancelled_at = _portable_utc_now()
        _cancel_pending_invites(ticket_id=ticket.id)
        for member in ticket.members:
            adv = member.adventurer
            if adv and adv.status == 'in_party':
                # 错误恢复需兼容历史残留 assignment，保留 Service 的专属槽位兜底。
                AdventurerS1Service.enter_rest(adv)


def _versioned_user_cache_key(user_id: int, version: str | None) -> str | None:
    """跨 worker 的用户匹配缓存键；无共享版本时禁用本地缓存以保证读到真值。"""
    if version is None:
        return None
    return f'{version}:user:{user_id}'


def _ticket_member_payload(member: AdventurerS1MatchTicketMember) -> dict:
    adv = member.adventurer
    return {
        'adventurer_id': member.adventurer_id,
        'name': adv.name if adv else None,
        'level': adv.level if adv else None,
        'active_title': (
            adv.active_title_config.to_public_dict()
            if adv and adv.active_title_config and adv.active_title_config.is_enabled else None
        ),
    }


def _ticket_public_payload(
    ticket: AdventurerS1MatchTicket | None,
    fate_dungeons_by_adventurer_id: dict[int, set[str]] | None = None,
) -> dict | None:
    """公开招募板：不含 owner_id / matched_* / 队列内部字段。"""
    if not ticket:
        return None
    fate_risk_member_names = _ticket_fate_risk_member_names(
        ticket,
        fate_dungeons_by_adventurer_id or {},
    )
    return {
        'id': ticket.id,
        'invite_code': ticket.invite_code,
        'allow_solo_fill': _ticket_allows_solo_fill(ticket),
        'preferred_dungeon': ticket.preferred_dungeon,
        'preferred_dungeons': sorted(_ticket_preferred_dungeons(ticket) or []),
        'preferred_dungeon_level_desc': format_s1_dungeon_level_desc(ticket.preferred_dungeon),
        'description': ticket.description,
        'campaign_task_title': ticket.campaign_task_title,
        'created_at': to_utc_iso(ticket.created_at) if ticket.created_at else None,
        'member_count': len(ticket.members),
        'members': [_ticket_member_payload(m) for m in ticket.members],
        'has_fate_dungeon_risk': bool(fate_risk_member_names),
        'fate_risk_member_names': fate_risk_member_names,
    }


def _ticket_public_cache_payload(
    ticket: AdventurerS1MatchTicket | None,
    fate_dungeons_by_adventurer_id: dict[int, set[str]] | None = None,
) -> dict | None:
    """公开队伍缓存快照：保留 owner_id 仅供按用户过滤，返回前必须裁剪。"""
    payload = _ticket_public_payload(ticket, fate_dungeons_by_adventurer_id)
    if payload is None:
        return None
    payload['owner_id'] = ticket.owner_id
    return payload


def _strip_public_ticket_internal_fields(payload: dict) -> dict:
    # 共享缓存可能由旧 worker 写入，命中时也按公开招募字段投影。
    return {
        key: payload[key]
        for key in (
            'id', 'invite_code', 'allow_solo_fill', 'preferred_dungeon',
            'preferred_dungeons', 'preferred_dungeon_level_desc', 'description',
            'campaign_task_title', 'created_at', 'member_count', 'members',
            'has_fate_dungeon_risk', 'fate_risk_member_names',
        )
        if key in payload
    }


def _ticket_invite_summary(ticket: AdventurerS1MatchTicket | None) -> dict | None:
    """邀请列表嵌套小队摘要：仅展示用字段。"""
    if not ticket:
        return None
    return {
        'invite_code': ticket.invite_code,
        'preferred_dungeon': ticket.preferred_dungeon,
        'preferred_dungeons': sorted(_ticket_preferred_dungeons(ticket) or []),
        'preferred_dungeon_level_desc': format_s1_dungeon_level_desc(ticket.preferred_dungeon),
        'description': ticket.description,
        'campaign_task_title': ticket.campaign_task_title,
        'queue_type': ticket.queue_type,
        'member_count': len(ticket.members),
    }


def _invite_payload(invite: AdventurerS1MatchInvite) -> dict:
    return {
        'id': invite.id,
        'created_at': to_utc_iso(invite.created_at) if invite.created_at else None,
        'ticket': _ticket_invite_summary(invite.ticket),
    }


def _ticket_payload(
    ticket: AdventurerS1MatchTicket | None,
    *,
    user_id: int | None = None,
    queue_ahead_map: dict[int, int] | None = None,
    fate_dungeons_by_adventurer_id: dict[int, set[str]] | None = None,
) -> dict | None:
    """我的匹配 ticket：前端组队页实际使用的字段白名单。"""
    if not ticket:
        return None
    if fate_dungeons_by_adventurer_id is None:
        fate_dungeons_by_adventurer_id = _active_fate_dungeons_by_adventurer_ids(
            member.adventurer_id for member in ticket.members
        )
    fate_risk = _ticket_fate_risk_state(ticket, fate_dungeons_by_adventurer_id)
    current_user_adventurer_ids = {
        member.adventurer_id
        for member in ticket.members
        if member.adventurer and member.adventurer.owner_id == user_id
    } if user_id is not None else set()
    departure_deadline = task_departure_deadline(ticket)
    payload = {
        'id': ticket.id,
        'status': ticket.status,
        'queue_type': ticket.queue_type,
        'access_mode': ticket.access_mode,
        'allow_solo_fill': _ticket_allows_solo_fill(ticket),
        'invite_code': ticket.invite_code,
        'preferred_dungeon': ticket.preferred_dungeon,
        'preferred_dungeons': sorted(_ticket_preferred_dungeons(ticket) or []),
        'preferred_dungeon_level_desc': format_s1_dungeon_level_desc(ticket.preferred_dungeon),
        'description': ticket.description,
        'campaign_task_title': ticket.campaign_task_title,
        'match_error': ticket.match_error or '',
        'campaign_departure_deadline': to_utc_iso(departure_deadline) if departure_deadline else None,
        'created_at': to_utc_iso(ticket.created_at) if ticket.created_at else None,
        'queued_at': to_utc_iso(ticket.queued_at) if ticket.queued_at else None,
        'member_count': len(ticket.members),
        'members': [_ticket_member_payload(m) for m in ticket.members],
        'has_fate_dungeon_risk': bool(fate_risk['risk_member_ids']),
        'fate_risk_member_names': fate_risk['risk_member_names'],
        'pending_fate_risk_member_names': fate_risk['pending_member_names'],
        'fate_risk_confirmation_required': bool(
            current_user_adventurer_ids & set(fate_risk['pending_member_ids'])
        ),
    }
    if ticket.status == 'matched' and ticket.matched_party:
        payload['matched_party_id'] = ticket.matched_party_id
        payload['matched_party_status'] = ticket.matched_party.status
        payload['matched_party_dungeon'] = ticket.matched_party.target_dungeon
        payload['matched_party_dungeon_level_desc'] = format_s1_dungeon_level_desc(ticket.matched_party.target_dungeon)
        payload['matched_party_departure_time'] = (
            to_utc_iso(ticket.matched_party.departure_time) if ticket.matched_party.departure_time else None
        )
    if user_id is not None:
        payload['is_owner'] = ticket.owner_id == user_id
        payload['can_edit_description'] = bool(
            ticket.owner_id == user_id and ticket.queue_type == 'group'
            and ticket.status in ('forming', 'queued')
            and not ticket.campaign_dispatch_id and not ticket.campaign_task_title
        )
    payload['ready_member_threshold'] = _match_min_size()
    if ticket.status == 'queued':
        ahead = queue_ahead_map.get(ticket.id) if queue_ahead_map is not None else _estimate_queue_parties_ahead(ticket.id)
        payload['queue_parties_ahead'] = ahead or 0
    return payload


def _generate_invite_code() -> str:
    """生成 6 位数字邀请码；只用于普通匹配小队，不暴露顺序自增 id。"""
    for _ in range(20):
        code = f'{secrets.randbelow(1_000_000):06d}'
        exists = AdventurerS1MatchTicket.query.filter_by(invite_code=code).first()
        if not exists:
            return code
    raise RuntimeError('无法生成唯一小队邀请码')


def _resolve_ticket_by_invite_code(invite_code: str | None) -> AdventurerS1MatchTicket | None:
    code = str(invite_code or '').strip()
    if not code:
        return None
    return AdventurerS1MatchTicket.query.filter_by(invite_code=code).first()


def _user_adventurer_ids(user_id: int) -> list[int]:
    return [
        aid for (aid,) in AdventurerS1.query
        .filter(
            AdventurerS1.owner_id == user_id,
            AdventurerS1.is_player_created.is_(True),
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
        )
        .with_entities(AdventurerS1.id)
        .all()
    ]


def _first_user_adventurer(user_id: int) -> AdventurerS1 | None:
    return (
        AdventurerS1.query.filter(
            AdventurerS1.owner_id == user_id,
            AdventurerS1.is_player_created.is_(True),
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
        )
        .order_by(AdventurerS1.id.asc())
        .first()
    )


def _emit_matchmaking_changed(
    action: str,
    *,
    ticket_user_ids: Optional[Iterable[int]] = None,
    invite_user_ids: Optional[Iterable[int]] = None,
    shared_only: bool = False,
    **extra,
) -> None:
    _after_matchmaking_mutation(
        ticket_user_ids=ticket_user_ids,
        invite_user_ids=invite_user_ids,
        shared_only=shared_only,
    )
    try:
        from plugins.adventurer_s1.services.s1_socket_service import s1_socketio as socketio
        socketio.emit('party_changed', {'action': action, **extra}, namespace='/')
    except Exception:
        pass


def _match_ticket_list_load_options():
    """招募板 / 我的 ticket 列表：成员姓名只需主表标量。"""
    from sqlalchemy.orm import defer, noload, selectinload

    return selectinload(AdventurerS1MatchTicket.members).selectinload(AdventurerS1MatchTicketMember.adventurer).options(selectinload(AdventurerS1.active_title_config), noload(AdventurerS1.traits), noload(AdventurerS1.equipment_items), noload(AdventurerS1.skill_items), noload(AdventurerS1.investor_items), noload(AdventurerS1.warehouse_items), noload(AdventurerS1.materials), noload(AdventurerS1.building_instance), noload(AdventurerS1.dorm_room), noload(AdventurerS1.story_threads), defer(AdventurerS1.history_story))


def _ticket_model_has(column_name: str) -> bool:
    return hasattr(AdventurerS1MatchTicket, column_name)


def _ticket_allows_solo_fill(ticket: AdventurerS1MatchTicket) -> bool:
    from .s1_campaign_world_service import campaign_world
    if ticket.campaign_dispatch_id and campaign_world(ticket.preferred_dungeon):
        return False
    return bool(getattr(ticket, 'allow_solo_fill', True))


def _sort_match_tickets(tickets: list[AdventurerS1MatchTicket]) -> list[AdventurerS1MatchTicket]:
    """本轮成团优先处理指定地图报名，再按排队时间。"""
    return sorted(
        tickets,
        key=lambda t: (
            not bool(_ticket_preferred_dungeons(t)),
            t.queued_at or datetime.max,
            t.id,
        ),
    )


def _current_user_adventurer(user_id: int, *, for_dispatch: bool = True) -> tuple[AdventurerS1 | None, str | None]:
    from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

    q = AdventurerS1.query.filter(
        AdventurerS1.owner_id == user_id,
        AdventurerS1.is_player_created.is_(True),
        AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
    ).order_by(AdventurerS1.id.asc())
    adv = q.first()
    if not adv:
        return None, '你当前没有可操作的存活冒险者'
    if for_dispatch:
        can, reason = adv.can_dispatch()
        if not can:
            return None, reason
        if _active_ticket_for_adventurer(adv.id):
            return None, f'{adv.name} 已在匹配队列中'
    return adv, None


def _available_dungeons_for_adventurer(adv: AdventurerS1 | None) -> dict:
    if not adv:
        return {'items': [], 'selectable_ranks': []}

    cache_key = f'{adv.owner_id}:{adv.id}:{adv.level}'
    closed_dungeons = S1WorldLocationStateService.closed_dungeon_names(
        _ACTIVE_DUNGEON_NAMES,
    )
    cached = _available_dungeons_cache.get(cache_key)
    if cached is not None:
        return {
            'selectable_ranks': sorted(get_s1_active_selectable_ranks(adv)),
            'items': [
                item for item in cached.get('items', [])
                if item.get('name') not in closed_dungeons
            ],
        }

    items = []
    selectable_ranks = get_s1_active_selectable_ranks(adv)
    for d in S1_ACTIVE_DUNGEONS:
        rank = S1_DUNGEON_RANK_MAP.get(d['name'], 0)
        if rank not in selectable_ranks:
            continue
        items.append({
            'name': d['name'],
            'level_desc': format_s1_dungeon_level_desc(d['name']),
            'rank': rank,
        })
    payload = {'items': items}
    _available_dungeons_cache.set(cache_key, payload)
    return {
        'selectable_ranks': sorted(selectable_ranks),
        'items': [
            item for item in items
            if item.get('name') not in closed_dungeons
        ],
    }


def _available_dungeons_board_payload(user_id: int, adv_ids: list[int] | None = None) -> dict:
    """board 聚合用：按 user_id 缓存，避免组件缓存全命中时仍查冒险者。"""
    user_key = f'board_user:{user_id}'
    cached = _available_dungeons_cache.get(user_key)
    if cached is not None:
        return cached
    if adv_ids is None:
        adv_ids = _user_adventurer_ids(user_id)
    adv = None
    if adv_ids:
        adv = (
            AdventurerS1.query
            .with_entities(AdventurerS1.id, AdventurerS1.owner_id, AdventurerS1.level)
            .filter(AdventurerS1.id == adv_ids[0])
            .first()
        )
    payload = _available_dungeons_for_adventurer(adv)
    _available_dungeons_cache.set(user_key, payload)
    return payload


def _queued_ticket_count_payload() -> dict:
    """全服 queued 报名及各地图可匹配人数。"""
    cached = _queued_count_cache.get('all')
    if cached is not None:
        return cached

    rows = (
        AdventurerS1MatchTicket.query
        .with_entities(
            AdventurerS1MatchTicket.id,
            AdventurerS1MatchTicket.preferred_dungeon,
            AdventurerS1MatchTicket.preferred_dungeons,
            AdventurerS1MatchTicketMember.adventurer_id,
        )
        .outerjoin(
            AdventurerS1MatchTicketMember,
            AdventurerS1MatchTicketMember.ticket_id == AdventurerS1MatchTicket.id,
        )
        .filter(AdventurerS1MatchTicket.status == 'queued')
        .all()
    )

    queued_ticket_ids = {ticket_id for ticket_id, *_rest in rows}
    candidate_adventurer_ids = {
        adventurer_id for *_ticket, adventurer_id in rows if adventurer_id is not None
    }
    latest_ticket_id_by_adventurer: dict[int, int] = {}
    if candidate_adventurer_ids:
        latest_ticket_id_by_adventurer = dict(
            AdventurerS1MatchTicketMember.query
            .with_entities(
                AdventurerS1MatchTicketMember.adventurer_id,
                func.max(AdventurerS1MatchTicketMember.ticket_id),
            )
            .filter(AdventurerS1MatchTicketMember.adventurer_id.in_(candidate_adventurer_ids))
            .group_by(AdventurerS1MatchTicketMember.adventurer_id)
            .all()
        )

    queued_adventurer_ids: set[int] = set()
    random_adventurer_ids: set[int] = set()
    dungeon_adventurer_ids = {name: set() for name in _ACTIVE_DUNGEON_NAMES}
    ticket_dungeons: dict[int, set[str] | None] = {}
    for ticket_id, preferred_dungeon, preferred_dungeons, adventurer_id in rows:
        if (
            adventurer_id is None
            or latest_ticket_id_by_adventurer.get(adventurer_id) != ticket_id
            or adventurer_id in queued_adventurer_ids
        ):
            continue
        queued_adventurer_ids.add(adventurer_id)
        if ticket_id not in ticket_dungeons:
            ticket_dungeons[ticket_id] = (
                {str(value).strip() for value in preferred_dungeons if str(value or '').strip()}
                if isinstance(preferred_dungeons, list) and preferred_dungeons
                else ({preferred_dungeon} if preferred_dungeon else None)
            )
        preferred = ticket_dungeons[ticket_id]
        if not preferred:
            random_adventurer_ids.add(adventurer_id)
            continue
        for dungeon_name in preferred:
            if dungeon_name in dungeon_adventurer_ids:
                dungeon_adventurer_ids[dungeon_name].add(adventurer_id)

    payload = {
        'queued_count': len(queued_ticket_ids),
        'queued_member_count': len(queued_adventurer_ids),
        'random_member_count': len(random_adventurer_ids),
        'dungeon_member_counts': {
            dungeon['name']: len(dungeon_adventurer_ids[dungeon['name']])
            for dungeon in S1_ACTIVE_DUNGEONS
        },
    }
    _queued_count_cache.set('all', payload)
    return payload


def _public_tickets_payload_for_user(user_id: int) -> dict:
    cached = _public_parties_cache.get('all')
    if cached is None:
        q = AdventurerS1MatchTicket.query.filter(
            AdventurerS1MatchTicket.status == 'forming',
            AdventurerS1MatchTicket.queue_type == 'group',
        )
        if _ticket_model_has('access_mode'):
            q = q.filter(AdventurerS1MatchTicket.access_mode == 'public')
        tickets = q.options(_match_ticket_list_load_options()).order_by(
            AdventurerS1MatchTicket.created_at.asc(),
            AdventurerS1MatchTicket.id.asc(),
        ).limit(100).all()
        fate_dungeons_by_adventurer_id = _active_fate_dungeons_by_adventurer_ids(
            member.adventurer_id
            for ticket in tickets
            for member in ticket.members
        )
        cached = {
            'items': [
                _ticket_public_cache_payload(ticket, fate_dungeons_by_adventurer_id)
                for ticket in tickets
            ],
        }
        _public_parties_cache.set('all', cached)

    items = [
        _strip_public_ticket_internal_fields(item)
        for item in cached.get('items', [])
        if item and item.get('owner_id') != user_id
    ]
    return {'items': items[:50]}


def _description(value: str | None) -> str:
    return str(value or '').strip()[:500]


def _access_mode(value: str | None) -> str:
    return 'invite' if str(value or '').strip() == 'invite' else 'public'


def _check_stamina(adv: AdventurerS1, *, tavern_seated: bool | None = None) -> str | None:
    stamina_min = _int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
    stamina_max = _int_cfg('ADVENTURER_S1_STAMINA_MAX', 100)
    recovery_rate = _int_cfg('ADVENTURER_S1_STAMINA_RECOVERY_PER_MINUTE', 1)
    from plugins.adventurer_s1.services.adventurer_s1_recovery_service import get_live_stamina
    current_stamina = get_live_stamina(
        adv,
        max_stamina=stamina_max,
        recovery_per_minute=recovery_rate,
        tavern_seated=tavern_seated,
    )
    if current_stamina <= stamina_min:
        return f'{adv.name} 精力不足（当前 {current_stamina}，需 > {stamina_min}）'
    return None


def _check_dungeon_for_adv(adv: AdventurerS1, preferred_dungeon: str | None, *, campaign_dispatch_id=None) -> str | None:
    """主动选图：仅允许不高于自身档位的副本；向下可任选。"""
    err = _validate_preferred_dungeon(preferred_dungeon, campaign_dispatch_id=campaign_dispatch_id)
    if err:
        return err
    if not preferred_dungeon:
        return None
    if not campaign_dispatch_id and S1_DUNGEON_RANK_MAP.get(preferred_dungeon) is None:
        return '未知或未开放的匹配地图'
    return check_s1_active_dungeon_select(adv, preferred_dungeon)


def _member_rank_compatible_with_ticket(adv: AdventurerS1, ticket: AdventurerS1MatchTicket) -> bool:
    """加入已有小队：指定地图最多允许挑战高 1 档；随机地图仍走档位交集。"""
    preferred_dungeons = _ticket_preferred_dungeons(ticket)
    if preferred_dungeons:
        return all(_check_join_dungeon_for_adv(adv, dungeon, campaign_dispatch_id=ticket.campaign_dispatch_id) is None for dungeon in preferred_dungeons)
    allowed = _allowed_ranks_for_ticket(ticket) & _allowed_ranks_for_adventurer(adv)
    return bool(allowed)


def _select_min_ready_match_ids(tickets: list[AdventurerS1MatchTicket], anchor_index: int) -> set[int]:
    """预估当前 anchor 是否已具备进本人数：优先补位，最低人数遵循酒馆设置。"""
    anchor = tickets[anchor_index]
    anchor_count = _ticket_member_count(anchor)
    if anchor_count <= 0 or anchor_count > 6:
        return set()

    selected_ids = {anchor.id}
    selected_dungeons = _ticket_preferred_dungeons(anchor)
    selected_ranks = _allowed_ranks_for_ticket(anchor)
    if selected_dungeons:
        selected_ranks &= {get_s1_dungeon_rank(name) for name in selected_dungeons}
    if not selected_ranks:
        return set()

    member_total = anchor_count
    if getattr(anchor, 'access_mode', 'public') == 'public':
        for cand in tickets[anchor_index + 1:]:
            if cand.id in selected_ids or cand.queue_type != 'solo':
                continue
            cand_count = _ticket_member_count(cand)
            if cand_count <= 0 or member_total + cand_count > 6:
                continue
            ok, next_dungeons = _preferred_dungeons_compatible(
                selected_dungeons, _ticket_preferred_dungeons(cand),
            )
            if not ok:
                continue
            cand_ranks = _allowed_ranks_for_ticket(cand)
            next_ranks = selected_ranks & cand_ranks
            if next_dungeons:
                next_ranks &= {get_s1_dungeon_rank(name) for name in next_dungeons}
            if not next_ranks:
                continue
            selected_ids.add(cand.id)
            selected_dungeons = next_dungeons
            selected_ranks = next_ranks
            member_total += cand_count
            if member_total >= 6:
                break

    return selected_ids if member_total >= _match_min_size() else set()


def _estimate_queue_parties_ahead(ticket_id: int) -> int:
    return _estimate_queue_parties_ahead_map([ticket_id]).get(ticket_id, 0)


def _compute_queue_ahead_map_from_tickets(
    remaining: list[AdventurerS1MatchTicket],
    targets: set[int] | None = None,
) -> dict[int, int]:
    """按 FIFO 队列顺序统计各 ticket 前面还有多少个 queued 报名。"""
    if targets is None:
        targets = {ticket.id for ticket in remaining}
    if not targets:
        return {}

    result: dict[int, int] = {}
    for ahead, ticket in enumerate(remaining):
        if ticket.id in targets:
            result[ticket.id] = ahead
            if len(result) >= len(targets):
                break
    fallback = len(remaining)
    for ticket_id in targets:
        result.setdefault(ticket_id, fallback)
    return result


def _get_cached_full_queue_ahead_map() -> dict[int, int]:
    cached = _queue_ahead_cache.get('ahead_map')
    if cached is not None:
        return cached

    rows = (
        AdventurerS1MatchTicket.query
        .filter_by(status='queued')
        .order_by(AdventurerS1MatchTicket.queued_at.asc(), AdventurerS1MatchTicket.id.asc())
        .with_entities(AdventurerS1MatchTicket.id)
        .all()
    )
    result = {ticket_id: idx for idx, (ticket_id,) in enumerate(rows)}
    _queue_ahead_cache.set('ahead_map', result)
    return result


def _estimate_queue_parties_ahead_map(ticket_ids: list[int]) -> dict[int, int]:
    """返回本报名之前还有多少个 queued 报名。"""
    targets = set(ticket_ids)
    if not targets:
        return {}

    full_map = _get_cached_full_queue_ahead_map()
    default_ahead = max(full_map.values()) if full_map else 0
    return {tid: full_map.get(tid, default_ahead) for tid in targets}


class AdventurerS1MatchmakingService:
    """玩家匹配报名与 scheduler 成团。"""

    @staticmethod
    def enqueue(
        user_id: int,
        preferred_dungeon: str | None = None,
        preferred_dungeons: object = None,
        is_admin: bool = False,
    ) -> tuple[dict | None, str | None]:
        """单人匹配：自动使用当前玩家唯一可操作冒险者。"""
        return AdventurerS1MatchmakingService._create_ticket(
            user_id=user_id,
            preferred_dungeon=preferred_dungeon,
            preferred_dungeons=preferred_dungeons,
            queue_type='solo',
            access_mode='public',
            allow_solo_fill=False,
            description='',
            is_admin=is_admin,
        )

    @staticmethod
    def create_party(
        user_id: int,
        preferred_dungeon: str | None = None,
        description: str | None = None,
        access_mode: str | None = None,
        allow_solo_fill: bool | None = None,
        is_admin: bool = False,
        campaign_dispatch_id: int | None = None,
        campaign_dispatch_revision: int | None = None,
    ) -> tuple[dict | None, str | None]:
        """创建普通匹配小队：自动使用当前玩家唯一可操作冒险者作为队长。"""
        return AdventurerS1MatchmakingService._create_ticket(
            user_id=user_id,
            preferred_dungeon=preferred_dungeon,
            preferred_dungeons=None,
            queue_type='group',
            access_mode=_access_mode(access_mode),
            allow_solo_fill=bool(allow_solo_fill),
            description=_description(description),
            is_admin=is_admin,
            campaign_dispatch_id=campaign_dispatch_id,
            campaign_dispatch_revision=campaign_dispatch_revision,
        )

    @staticmethod
    def update_description(ticket_id: int, user_id: int, description: str) -> tuple[dict | None, str | None]:
        """队长只可改自己创建的普通招募队伍说明，系统任务说明保持权威。"""
        if not isinstance(description, str) or len(description.strip()) > 500:
            return None, '小队描述须为不超过 500 字的文字'
        with db_manager.transaction():
            ticket = (
                db_manager.query(AdventurerS1MatchTicket).filter_by(id=ticket_id)
                .populate_existing().with_for_update().first()
            )
            if not ticket or ticket.owner_id != user_id or ticket.queue_type != 'group':
                return None, '只能修改自己创建的小队描述'
            if ticket.status not in ('forming', 'queued'):
                return None, '小队已成团或结束，不能修改描述'
            if ticket.campaign_dispatch_id or ticket.campaign_task_title:
                return None, '共同战役等系统任务的描述不能修改'
            ticket.description = _description(description)
        _emit_matchmaking_changed('description_updated', ticket_id=ticket_id)
        return {'ticket_id': ticket_id, 'description': ticket.description}, None

    @staticmethod
    def _create_ticket(
        user_id: int,
        preferred_dungeon: str | None,
        preferred_dungeons: object,
        queue_type: str,
        access_mode: str,
        allow_solo_fill: bool,
        description: str,
        is_admin: bool = False,
        campaign_dispatch_id: int | None = None,
        campaign_dispatch_revision: int | None = None,
    ) -> tuple[dict | None, str | None]:
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

        preferred_dungeon = _normalize_dungeon(preferred_dungeon)
        try:
            normalized_dungeons = _normalize_preferred_dungeons(preferred_dungeons, preferred_dungeon)
        except ValueError as exc:
            return None, str(exc)
        if queue_type != 'solo':
            normalized_dungeons = [preferred_dungeon] if preferred_dungeon else None
        if normalized_dungeons:
            closed_dungeons = S1WorldLocationStateService.closed_dungeon_names(
                normalized_dungeons,
            )
            world_err = _closed_dungeon_error(normalized_dungeons, closed_dungeons)
            if world_err:
                return None, world_err
            normalized_dungeons = [
                dungeon for dungeon in normalized_dungeons
                if dungeon not in closed_dungeons
            ]
            if preferred_dungeon in closed_dungeons:
                preferred_dungeon = normalized_dungeons[0] if normalized_dungeons else None
        adv, err = _current_user_adventurer(user_id, for_dispatch=True)
        if err:
            return None, err
        for dungeon in normalized_dungeons or []:
            err = _check_dungeon_for_adv(adv, dungeon, campaign_dispatch_id=campaign_dispatch_id)
            if err:
                return None, err
        err = _check_stamina(adv)
        if err:
            return None, err

        with db_manager.transaction():
            locked_adv = (
                AdventurerS1.query
                .populate_existing()
                .filter_by(id=adv.id)
                .with_for_update()
                .first()
            )
            owner_err = AdventurerS1Service.check_owner_playable(locked_adv, user_id, is_admin=is_admin)
            if owner_err:
                return None, owner_err
            can, reason = locked_adv.can_dispatch()
            if not can:
                return None, reason
            if _active_ticket_for_adventurer(locked_adv.id):
                return None, f'{locked_adv.name} 已在匹配队列中'
            err = _check_stamina(locked_adv)
            if err:
                return None, err

            ticket = AdventurerS1MatchTicket(
                owner_id=user_id,
                queue_type=queue_type,
                preferred_dungeon=preferred_dungeon,
                preferred_dungeons=normalized_dungeons,
                status='queued' if queue_type == 'solo' else 'forming',
                queued_at=_portable_utc_now() if queue_type == 'solo' else None,
                match_error='',
            )
            if queue_type == 'group':
                ticket.invite_code = _generate_invite_code()
            if _ticket_model_has('access_mode'):
                ticket.access_mode = access_mode
            if _ticket_model_has('allow_solo_fill'):
                ticket.allow_solo_fill = allow_solo_fill
            if _ticket_model_has('description'):
                ticket.description = description
            if campaign_dispatch_id is not None or campaign_dispatch_revision is not None:
                if (type(campaign_dispatch_id) is not int or campaign_dispatch_id <= 0
                        or type(campaign_dispatch_revision) is not int or campaign_dispatch_revision <= 0):
                    return None, '共同任务身份无效，请刷新后接取'
                from plugins.adventurer_s1.services.s1_campaign_dispatch_service import S1CampaignDispatchService
                err = TaskAdmission.acceptance_error(user_id)
                if err:
                    return None, err
                err = S1CampaignDispatchService.bind_ticket(ticket, campaign_dispatch_id, campaign_dispatch_revision)
                if err:
                    return None, err
                from .s1_campaign_world_service import campaign_world
                if campaign_world(ticket.preferred_dungeon):
                    ticket.allow_solo_fill = False
            db_manager.add(ticket)
            db_manager.flush()

            AdventurerS1StateTransitionCoordinator.leave_rest(locked_adv, 'in_party')
            stamina_min = _int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
            if locked_adv.stamina <= stamina_min:
                AdventurerS1StateTransitionCoordinator.enter_rest(locked_adv)
                return None, f'{locked_adv.name} 精力不足（当前 {locked_adv.stamina}，需 > {stamina_min}）'
            db_manager.add(AdventurerS1MatchTicketMember(ticket_id=ticket.id, adventurer_id=locked_adv.id))

            db_manager.flush()
            ticket_id = ticket.id

        _after_matchmaking_mutation(ticket_user_ids=[user_id])
        return {
            'ticket': _ticket_payload(
                AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, ticket_id),
                user_id=user_id,
            ),
        }, None

    @staticmethod
    def available_dungeons(user_id: int) -> tuple[dict | None, str | None]:
        adv, err = _current_user_adventurer(user_id, for_dispatch=False)
        if err:
            return None, err
        return _available_dungeons_for_adventurer(adv), None

    @staticmethod
    def public_tickets(user_id: int) -> tuple[dict, None]:
        return _public_tickets_payload_for_user(user_id), None

    @staticmethod
    def invite_candidates(ticket_id: int, user_id: int, search: str | None = None) -> tuple[dict | None, str | None]:
        ticket = AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, ticket_id)
        if not ticket:
            return None, '小队不存在'
        if ticket.owner_id != user_id:
            return None, '只有队长可以邀请玩家'
        if ticket.status != 'forming':
            return None, '小队已开始匹配，无法继续邀请'

        followed_ids = {
            aid for (aid,) in AdventurerS1Follow.query
            .filter_by(user_id=user_id)
            .with_entities(AdventurerS1Follow.adventurer_id)
            .all()
        }
        member_ids = {m.adventurer_id for m in ticket.members}
        q = AdventurerS1.query.filter(
            AdventurerS1.is_player_created.is_(True),
            AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            AdventurerS1.owner_id != user_id,
        )
        if search and search.strip():
            q = q.filter(AdventurerS1.name.ilike(f'%{search.strip()}%'))
        rows = q.order_by(AdventurerS1.id.asc()).limit(100).all()

        items = []
        for adv in rows:
            if adv.id in member_ids:
                continue
            can, reason = adv.can_dispatch()
            if not can:
                continue
            if _active_ticket_for_adventurer(adv.id):
                continue
            if _check_join_dungeon_for_adv(adv, ticket.preferred_dungeon, campaign_dispatch_id=ticket.campaign_dispatch_id):
                continue
            if not _member_rank_compatible_with_ticket(adv, ticket):
                continue
            items.append({
                'adventurer_id': adv.id,
                'name': adv.name,
                'level': adv.level,
                'status': adv.status,
                'is_followed': adv.id in followed_ids,
            })
        items.sort(key=lambda row: (not row['is_followed'], row['name']))
        return {'items': items[:30]}, None

    @staticmethod
    def invite_adventurer(ticket_id: int, target_adventurer_id: int, user_id: int) -> tuple[dict | None, str | None]:
        ticket = AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, ticket_id)
        if not ticket:
            return None, '小队不存在'
        if ticket.owner_id != user_id:
            return None, '只有队长可以邀请玩家'
        if ticket.status != 'forming':
            return None, '小队已开始匹配，无法继续邀请'
        if _ticket_member_count(ticket) >= 6:
            return None, '小队已满'
        target = AdventurerS1.query.session.get(AdventurerS1, target_adventurer_id)
        if not target or not target.is_player_created or target.status in AdventurerS1.TAVERN_EXCLUDED_STATUSES:
            return None, '目标冒险者不可邀请'
        if target.owner_id == user_id:
            return None, '不能邀请自己'
        if _active_ticket_for_adventurer(target.id):
            return None, '目标冒险者已在小队或匹配中'
        err = _check_join_dungeon_for_adv(target, ticket.preferred_dungeon, campaign_dispatch_id=ticket.campaign_dispatch_id)
        if err:
            return None, err
        existing = AdventurerS1MatchInvite.query.filter_by(
            ticket_id=ticket.id,
            target_adventurer_id=target.id,
            status='pending',
        ).first()
        if existing:
            return None, '已向该玩家发送过邀请'

        with db_manager.transaction():
            invite = AdventurerS1MatchInvite(
                ticket_id=ticket.id,
                inviter_user_id=user_id,
                target_adventurer_id=target.id,
                status='pending',
            )
            db_manager.add(invite)
            db_manager.flush()
            invite_id = invite.id
        _after_matchmaking_mutation(invite_user_ids=[target.owner_id])
        return {'invite': _invite_payload(AdventurerS1MatchInvite.query.session.get(AdventurerS1MatchInvite, invite_id))}, None

    @staticmethod
    def _my_invites_payload(user_id: int, adv_ids: list[int] | None = None) -> dict:
        if adv_ids is None:
            adv_ids = _user_adventurer_ids(user_id)
        if not adv_ids:
            return {'items': []}
        from sqlalchemy.orm import raiseload, selectinload

        invites = (
            AdventurerS1MatchInvite.query
            .options(
                # 邀请仅投影 ticket 摘要和人数，不读取受邀者/成员资产。
                raiseload('*'),
                selectinload(AdventurerS1MatchInvite.ticket)
                .selectinload(AdventurerS1MatchTicket.members)
                .raiseload('*'),
            )
            .join(
                AdventurerS1MatchTicket,
                AdventurerS1MatchTicket.id == AdventurerS1MatchInvite.ticket_id,
            )
            .filter(
                AdventurerS1MatchInvite.target_adventurer_id.in_(adv_ids),
                AdventurerS1MatchInvite.status == 'pending',
                AdventurerS1MatchTicket.status == 'forming',
                AdventurerS1MatchTicket.queue_type == 'group',
            )
            .order_by(AdventurerS1MatchInvite.created_at.asc())
            .limit(20)
            .all()
        )
        return {'items': [_invite_payload(inv) for inv in invites]}

    @staticmethod
    def my_invites(user_id: int) -> tuple[dict, None]:
        cached = _my_invites_cache.get(user_id)
        if cached is not None:
            return cached, None
        payload = AdventurerS1MatchmakingService._my_invites_payload(user_id)
        _my_invites_cache.set(user_id, payload)
        return payload, None

    @staticmethod
    def accept_invite(invite_id: int, user_id: int, is_admin: bool = False) -> tuple[dict | None, str | None]:
        invite = AdventurerS1MatchInvite.query.session.get(AdventurerS1MatchInvite, invite_id)
        if not invite or invite.status != 'pending':
            return None, '邀请不存在或已处理'
        target = invite.target_adventurer
        if not target:
            return None, '目标冒险者不存在'
        if not is_admin and target.owner_id != user_id:
            return None, '无权处理该邀请'
        data, err = AdventurerS1MatchmakingService.join_public_party(
            invite.ticket_id,
            user_id,
            adventurer_id=target.id,
            is_admin=is_admin,
        )
        if err:
            ticket = AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, invite.ticket_id)
            if ticket and (
                ticket.status != 'forming'
                or _ticket_member_count(ticket) >= 6
            ):
                _cancel_pending_invites(ticket_id=ticket.id)
            return None, err
        with db_manager.transaction():
            locked = (
                AdventurerS1MatchInvite.query
                .populate_existing()
                .filter_by(id=invite_id)
                .with_for_update()
                .first()
            )
            if locked and locked.status == 'pending':
                locked.status = 'accepted'
                locked.responded_at = _portable_utc_now()
            _cancel_pending_invites(
                target_adventurer_id=target.id,
                exclude_invite_id=invite_id,
            )
        return data, None

    @staticmethod
    def decline_invite(invite_id: int, user_id: int, is_admin: bool = False) -> tuple[dict | None, str | None]:
        invite = AdventurerS1MatchInvite.query.session.get(AdventurerS1MatchInvite, invite_id)
        if not invite or invite.status != 'pending':
            return None, '邀请不存在或已处理'
        target = invite.target_adventurer
        if not target:
            return None, '目标冒险者不存在'
        if not is_admin and target.owner_id != user_id:
            return None, '无权处理该邀请'
        with db_manager.transaction():
            invite.status = 'declined'
            invite.responded_at = _portable_utc_now()
        _after_matchmaking_mutation(invite_user_ids=[user_id])
        return {'declined': True, 'invite_id': invite_id}, None

    @staticmethod
    def join_public_party(
        ticket_id: int,
        user_id: int,
        adventurer_id: int | None = None,
        is_admin: bool = False,
        confirm_fate_risk: bool = False,
    ) -> tuple[dict | None, str | None]:
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

        if adventurer_id is not None:
            adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
            if not adv:
                return None, '冒险者不存在'
            owner_err = AdventurerS1Service.check_owner_playable(adv, user_id, is_admin=is_admin)
            if owner_err:
                return None, owner_err
            can, reason = adv.can_dispatch()
            if not can:
                return None, reason
            if _active_ticket_for_adventurer(adv.id):
                return None, f'{adv.name} 已在匹配队列中'
        else:
            adv, err = _current_user_adventurer(user_id, for_dispatch=True)
            if err:
                return None, err
        err = _check_stamina(adv)
        if err:
            return None, err

        with notify_task_cancellations_after_commit() as cancellation_changed, db_manager.transaction():
            # 邀请码/邀请接受路径会在同一 scoped Session 里预读 ticket；拿到数据库
            # 行锁后必须强制刷新，否则另一 worker 已取消/成团的状态仍可能被 identity
            # map 中的 forming 旧对象覆盖，继而向终态 ticket 插入幽灵成员。
            ticket = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(id=ticket_id)
                .with_for_update()
                .first()
            )
            if not ticket or ticket.status != 'forming':
                return None, '小队已不存在、已开始匹配或已成团'
            if ticket.queue_type != 'group':
                return None, '该报名不是小队'
            if TaskAdmission.cancel_expired_locked_ticket(ticket, now=_portable_utc_now()):
                cancellation_changed[0] = True
                return None, timeout_notice()
            if ticket.owner_id == user_id:
                return None, '你已经在该小队中'
            if AdventurerS1MatchTicketMember.query.filter_by(
                ticket_id=ticket.id,
            ).count() >= 6:
                return None, '小队已满'

            locked_adv = (
                AdventurerS1.query
                .populate_existing()
                .filter_by(id=adv.id)
                .with_for_update()
                .first()
            )
            owner_err = AdventurerS1Service.check_owner_playable(locked_adv, user_id, is_admin=is_admin)
            if owner_err:
                return None, owner_err
            can, reason = locked_adv.can_dispatch()
            if not can:
                return None, reason
            if _active_ticket_for_adventurer(locked_adv.id):
                return None, f'{locked_adv.name} 已在匹配队列中'
            err = _check_match_ticket_rejoin_cooldown(locked_adv.id, ticket)
            if err:
                return None, err
            if ticket.campaign_dispatch_id:
                err = TaskAdmission.acceptance_error(user_id)
                if err:
                    return None, err
            err = _check_join_dungeon_for_adv(locked_adv, ticket.preferred_dungeon, campaign_dispatch_id=ticket.campaign_dispatch_id)
            if err:
                return None, err
            if not ticket.preferred_dungeon and not _member_rank_compatible_with_ticket(locked_adv, ticket):
                return None, '你的冒险者与该小队匹配档位不兼容'
            err = _check_stamina(locked_adv)
            if err:
                return None, err

            AdventurerS1StateTransitionCoordinator.leave_rest(locked_adv, 'in_party')
            stamina_min = _int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
            if locked_adv.stamina <= stamina_min:
                AdventurerS1StateTransitionCoordinator.enter_rest(locked_adv)
                return None, f'{locked_adv.name} 精力不足（当前 {locked_adv.stamina}，需 > {stamina_min}）'
            ok, add_error = db_manager.add(AdventurerS1MatchTicketMember(
                ticket_id=ticket.id,
                adventurer_id=locked_adv.id,
            ))
            if not ok:
                raise RuntimeError(f'加入匹配小队失败: {add_error}')
            ticket.match_error = ''
            if confirm_fate_risk:
                fate_dungeons_by_adventurer_id = _active_fate_dungeons_by_adventurer_ids(
                    [member.adventurer_id for member in ticket.members] + [locked_adv.id]
                )
                fate_risk = _ticket_fate_risk_state(
                    ticket,
                    fate_dungeons_by_adventurer_id,
                    additional_adventurers=[locked_adv],
                )
                if locked_adv.id in fate_risk['required_member_ids'] and fate_risk['fingerprint']:
                    acknowledgements = dict(ticket.fate_risk_acknowledgements or {})
                    acknowledgements[str(locked_adv.id)] = fate_risk['fingerprint']
                    ticket.fate_risk_acknowledgements = acknowledgements
            member_count = (
                AdventurerS1MatchTicketMember.query
                .filter_by(ticket_id=ticket.id)
                .count()
            )
            if member_count >= 6:
                _cancel_pending_invites(ticket_id=ticket.id)
            ticket_id = ticket.id
            ticket_owner_id = ticket.owner_id
            # commit expires ORM objects; notification must not reopen a read
            # transaction while cache invalidation / Socket I/O is in progress.
            joined_adventurer_id = locked_adv.id

        _emit_matchmaking_changed(
            'match_ticket_joined',
            ticket_id=ticket_id,
            adventurer_id=joined_adventurer_id,
            ticket_user_ids=[uid for uid in (user_id, ticket_owner_id) if uid],
            invite_user_ids=[user_id],
        )
        return {
            'ticket': _ticket_payload(
                AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, ticket_id),
                user_id=user_id,
            ),
        }, None

    @staticmethod
    def join_by_invite_code(
        invite_code: str,
        user_id: int,
        is_admin: bool = False,
    ) -> tuple[dict | None, str | None]:
        ticket = _resolve_ticket_by_invite_code(invite_code)
        if not ticket:
            return None, '小队邀请码不存在'
        return AdventurerS1MatchmakingService.join_public_party(
            ticket.id,
            user_id,
            is_admin=is_admin,
        )

    @staticmethod
    def leave_ticket(
        ticket_id: int,
        user_id: int,
        is_admin: bool = False,
    ) -> tuple[dict | None, str | None]:
        """非队长成员退出匹配小队；匹配中退队会保留成员并退回等人中。"""
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

        adv_ids = _user_adventurer_ids(user_id)
        if not adv_ids:
            return None, '你当前没有可操作的存活冒险者'

        with notify_task_cancellations_after_commit() as cancellation_changed, db_manager.transaction():
            ticket = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(id=ticket_id)
                .with_for_update()
                .first()
            )
            if not ticket or ticket.status not in ('forming', 'queued'):
                return None, '小队已不存在或已成团'
            if ticket.owner_id == user_id and not is_admin:
                return None, '队长请使用取消小队'

            member_row = (
                AdventurerS1MatchTicketMember.query
                .filter(
                    AdventurerS1MatchTicketMember.ticket_id == ticket.id,
                    AdventurerS1MatchTicketMember.adventurer_id.in_(adv_ids),
                )
                .first()
            )
            if not member_row:
                return None, '你的冒险者不在该小队中'
            if TaskAdmission.cancel_expired_locked_ticket(ticket, now=_portable_utc_now()):
                cancellation_changed[0] = True
                return {'left': True, 'ticket_id': ticket.id, 'cancelled': True}, None

            locked_adv = (
                AdventurerS1.query
                .populate_existing()
                .filter_by(id=member_row.adventurer_id)
                .with_for_update()
                .first()
            )
            if not locked_adv:
                return None, '冒险者不存在'
            owner_err = AdventurerS1Service.check_owner_playable(locked_adv, user_id, is_admin=is_admin)
            if owner_err:
                return None, owner_err

            db_manager.delete_instance(member_row)
            if locked_adv.status == 'in_party':
                AdventurerS1StateTransitionCoordinator.enter_rest(locked_adv)
            _record_match_ticket_leave_cooldown(locked_adv.id, ticket)
            if ticket.status == 'queued':
                ticket.status = 'forming'
                ticket.queued_at = None
                ticket.match_error = f'{locked_adv.name} 已退队，匹配已取消，请队长确认后重新开始匹配'

            _cancel_pending_invites(
                ticket_id=ticket.id,
                target_adventurer_id=locked_adv.id,
            )
            out_id = ticket.id
            adv_id = locked_adv.id
            ticket_owner_id = ticket.owner_id
            leaver_user_id = locked_adv.owner_id

        _emit_matchmaking_changed(
            'match_ticket_left',
            ticket_id=out_id,
            adventurer_id=adv_id,
            ticket_user_ids=[uid for uid in (leaver_user_id, ticket_owner_id) if uid],
        )
        return {
            'left': True,
            'ticket_id': out_id,
            'adventurer_id': adv_id,
        }, None

    @staticmethod
    def cancel(ticket_id: int, user_id: int, is_admin: bool = False) -> tuple[dict | None, str | None]:
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

        stopped_group_matching = False
        ticket_owner_id = None
        with notify_task_cancellations_after_commit() as cancellation_changed, db_manager.transaction():
            locked = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(id=ticket_id)
                .with_for_update()
                .first()
            )
            if not locked:
                return None, '匹配报名不存在'
            if locked.status not in ('forming', 'queued'):
                return None, '该小队已不在可取消状态'
            if not is_admin and locked.owner_id != user_id:
                return None, '无权取消该匹配报名'
            if TaskAdmission.cancel_expired_locked_ticket(locked, now=_portable_utc_now()):
                cancellation_changed[0] = True
                return {'cancelled': True, 'ticket_id': ticket_id}, None
            # 必须依据最终锁读分流。若事务外预读 forming 后另一 worker 已切到 queued，
            # 仍按旧状态走“整队取消”会让本应只是停止匹配的小队瞬间解散。
            if (locked.queue_type == 'group' and locked.status == 'queued'
                    and not locked.campaign_dispatch_id):
                locked.status = 'forming'
                locked.queued_at = None
                locked.match_error = ''
                db_manager.flush()
                stopped_group_matching = True
                ticket_owner_id = locked.owner_id
                out_id = locked.id
            else:
                adv_ids = [
                    row.adventurer_id for row in
                    db_manager.query(AdventurerS1MatchTicketMember)
                    .filter_by(ticket_id=locked.id)
                    .all()
                ]
                affected_user_ids = {locked.owner_id} if locked.owner_id else set()
                if adv_ids:
                    advs = (
                        db_manager.query(AdventurerS1)
                        .populate_existing()
                        .filter(AdventurerS1.id.in_(adv_ids))
                        .with_for_update()
                        .all()
                    )
                    for adv in advs:
                        if adv.owner_id:
                            affected_user_ids.add(adv.owner_id)
                        if adv.status == 'in_party':
                            AdventurerS1StateTransitionCoordinator.enter_rest(adv)
                locked.status = 'cancelled'
                locked.match_error = ''
                locked.queued_at = None
                locked.cancelled_at = _portable_utc_now()
                _cancel_pending_invites(ticket_id=locked.id)

        if stopped_group_matching:
            _after_matchmaking_mutation(
                ticket_user_ids=[ticket_owner_id] if ticket_owner_id else None,
            )
            return {
                'ticket': _ticket_payload(AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, out_id)),
            }, None
        _after_matchmaking_mutation(ticket_user_ids=list(affected_user_ids))
        return {'cancelled': True, 'ticket_id': ticket_id}, None

    @staticmethod
    def start_matching(ticket_id: int, user_id: int, is_admin: bool = False) -> tuple[dict | None, str | None]:
        """队长将已创建小队投入匹配队列。"""
        with notify_task_cancellations_after_commit() as cancellation_changed, db_manager.transaction():
            ticket = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(id=ticket_id)
                .with_for_update()
                .first()
            )
            if not ticket:
                return None, '小队不存在'
            if ticket.status != 'forming':
                return None, '小队已开始匹配或已成团'
            if not is_admin and ticket.owner_id != user_id:
                return None, '只有队长可以开始匹配'
            if TaskAdmission.cancel_expired_locked_ticket(ticket, now=_portable_utc_now()):
                cancellation_changed[0] = True
                return None, timeout_notice()
            member_count = _ticket_member_count(ticket)
            if member_count <= 0:
                return None, '小队没有成员，无法开始匹配'
            if member_count > 6:
                return None, '小队人数超过上限'
            err = _ticket_members_can_dispatch(ticket)
            if err:
                return None, err
            err = _ticket_match_error(ticket)
            if err:
                return None, err
            fate_dungeons_by_adventurer_id = _active_fate_dungeons_by_adventurer_ids(
                member.adventurer_id for member in ticket.members
            )
            fate_risk = _ticket_fate_risk_state(ticket, fate_dungeons_by_adventurer_id)
            if fate_risk['pending_member_names']:
                return None, (
                    '命途风险尚未确认，请等待 '
                    + '、'.join(fate_risk['pending_member_names'])
                    + ' 确认后再开始匹配'
                )
            ticket.status = 'queued'
            ticket.queued_at = _portable_utc_now()
            ticket.match_error = ''
            _cancel_pending_invites(ticket_id=ticket.id)
            db_manager.flush()
            out_id = ticket.id
            ticket_owner_id = ticket.owner_id

        _after_matchmaking_mutation(ticket_user_ids=[ticket_owner_id] if ticket_owner_id else None)
        return {'ticket': _ticket_payload(AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, out_id))}, None

    @staticmethod
    def confirm_fate_risk(
        ticket_id: int,
        user_id: int,
        is_admin: bool = False,
    ) -> tuple[dict | None, str | None]:
        """低等级成员确认当前小队目标对应的命途风险。"""
        with db_manager.transaction():
            ticket = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(id=ticket_id)
                .with_for_update()
                .first()
            )
            if not ticket or ticket.status != 'forming':
                return None, '小队已不存在或已开始匹配'
            fate_dungeons_by_adventurer_id = _active_fate_dungeons_by_adventurer_ids(
                member.adventurer_id for member in ticket.members
            )
            fate_risk = _ticket_fate_risk_state(ticket, fate_dungeons_by_adventurer_id)
            owned_required_ids = {
                member.adventurer_id
                for member in ticket.members
                if member.adventurer
                and member.adventurer.owner_id == user_id
                and member.adventurer_id in fate_risk['required_member_ids']
            }
            if is_admin and not owned_required_ids:
                owned_required_ids = set(fate_risk['required_member_ids'])
            if not fate_risk['fingerprint'] or not owned_required_ids:
                return None, '当前没有需要你确认的命途风险'
            acknowledgements = dict(ticket.fate_risk_acknowledgements or {})
            for adventurer_id in owned_required_ids:
                acknowledgements[str(adventurer_id)] = fate_risk['fingerprint']
            ticket.fate_risk_acknowledgements = acknowledgements
            ticket.match_error = ''
            affected_user_ids = {
                member.adventurer.owner_id
                for member in ticket.members
                if member.adventurer and member.adventurer.owner_id
            }
            db_manager.flush()
            out_id = ticket.id

        _after_matchmaking_mutation(ticket_user_ids=affected_user_ids)
        return {
            'ticket': _ticket_payload(
                AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, out_id),
                user_id=user_id,
            ),
        }, None

    @staticmethod
    def stop_matching(ticket_id: int, user_id: int, is_admin: bool = False) -> tuple[dict | None, str | None]:
        """队长取消匹配，但保留小队成员，回到等人中。"""
        with db_manager.transaction():
            ticket = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(id=ticket_id)
                .with_for_update()
                .first()
            )
            if not ticket:
                return None, '小队不存在'
            if ticket.status != 'queued':
                return None, '小队当前不在匹配中'
            if ticket.queue_type != 'group':
                return None, '单人匹配不能停止为小队'
            if not is_admin and ticket.owner_id != user_id:
                return None, '只有队长可以取消匹配'
            ticket.status = 'forming'
            ticket.queued_at = None
            ticket.match_error = ''
            db_manager.flush()
            out_id = ticket.id
            ticket_owner_id = ticket.owner_id
        _after_matchmaking_mutation(ticket_user_ids=[ticket_owner_id] if ticket_owner_id else None)
        return {'ticket': _ticket_payload(AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, out_id))}, None

    @staticmethod
    def make_public(
        ticket_id: int,
        user_id: int,
        is_admin: bool = False,
    ) -> tuple[dict | None, str | None]:
        """队长将邀请制小队转为公开：展示在招募板，匹配时允许单人补位。"""
        with db_manager.transaction():
            ticket = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(id=ticket_id)
                .with_for_update()
                .first()
            )
            if not ticket:
                return None, '小队不存在'
            if ticket.queue_type != 'group':
                return None, '单人匹配无法设为公开小队'
            if ticket.status not in ('forming', 'queued'):
                return None, '小队已成团或已结束，无法切换'
            if not is_admin and ticket.owner_id != user_id:
                return None, '只有队长可以设为公开'
            if getattr(ticket, 'access_mode', 'public') == 'public':
                return None, '小队已是公开'
            if _ticket_model_has('access_mode'):
                ticket.access_mode = 'public'
            db_manager.flush()
            out_id = ticket.id
            ticket_owner_id = ticket.owner_id

        _after_matchmaking_mutation(ticket_user_ids=[ticket_owner_id] if ticket_owner_id else None)
        return {
            'ticket': _ticket_payload(
                AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, out_id),
                user_id=user_id,
            ),
        }, None

    @staticmethod
    def set_solo_fill(
        ticket_id: int,
        user_id: int,
        allow_solo_fill: bool,
        is_admin: bool = False,
    ) -> tuple[dict | None, str | None]:
        """队长切换是否允许匹配系统用单排报名补位。"""
        with db_manager.transaction():
            ticket = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(id=ticket_id)
                .with_for_update()
                .first()
            )
            if not ticket:
                return None, '小队不存在'
            if ticket.queue_type != 'group':
                return None, '单人匹配无法设置路人补位'
            if ticket.status not in ('forming', 'queued'):
                return None, '小队已成团或已结束，无法切换'
            if not is_admin and ticket.owner_id != user_id:
                return None, '只有队长可以设置路人补位'
            ticket.allow_solo_fill = bool(allow_solo_fill)
            ticket.match_error = ''
            db_manager.flush()
            out_id = ticket.id
            ticket_owner_id = ticket.owner_id

        _after_matchmaking_mutation(ticket_user_ids=[ticket_owner_id] if ticket_owner_id else None)
        return {
            'ticket': _ticket_payload(
                AdventurerS1MatchTicket.query.session.get(AdventurerS1MatchTicket, out_id),
                user_id=user_id,
            ),
        }, None

    @staticmethod
    def my_tickets(user_id: int) -> tuple[dict, None]:
        version = get_board_version('matchmaking')
        cache_key = _versioned_user_cache_key(user_id, version)
        if cache_key is not None:
            cached = _my_tickets_cache.get(cache_key)
            if cached is not None:
                return cached, None

        adv_ids = _user_adventurer_ids(user_id)
        payload = AdventurerS1MatchmakingService._my_tickets_payload(user_id, adv_ids)
        if cache_key is not None:
            _my_tickets_cache.set(cache_key, payload)
        return payload, None

    @staticmethod
    def _my_tickets_payload(user_id: int, adv_ids: list[int]) -> dict:
        active_statuses = ['forming', 'queued']
        active_matched_party_ids = (
            AdventurerS1Party.query
            .filter(AdventurerS1Party.status == 'forming')
            .with_entities(AdventurerS1Party.id)
        )
        active_filter = (
            AdventurerS1MatchTicket.status.in_(active_statuses)
            | (
                (AdventurerS1MatchTicket.status == 'matched')
                & AdventurerS1MatchTicket.matched_party_id.in_(active_matched_party_ids)
            )
        )

        query = AdventurerS1MatchTicket.query.filter(active_filter)
        if adv_ids:
            member_ticket_ids = (
                AdventurerS1MatchTicketMember.query
                .filter(AdventurerS1MatchTicketMember.adventurer_id.in_(adv_ids))
                .with_entities(AdventurerS1MatchTicketMember.ticket_id)
            )
            query = query.filter(or_(
                AdventurerS1MatchTicket.owner_id == user_id,
                AdventurerS1MatchTicket.id.in_(member_ticket_ids),
            ))
        else:
            query = query.filter(AdventurerS1MatchTicket.owner_id == user_id)

        from sqlalchemy.orm import joinedload

        tickets = (
            query
            .options(
                # 匹配结果仅显示出发信息，不能连带加载整队成员及资产。
                joinedload(AdventurerS1MatchTicket.matched_party).load_only(
                    AdventurerS1Party.status,
                    AdventurerS1Party.target_dungeon,
                    AdventurerS1Party.departure_time,
                ).raiseload('*'),
                _match_ticket_list_load_options(),
            )
            .order_by(AdventurerS1MatchTicket.created_at.asc(), AdventurerS1MatchTicket.id.asc())
            .limit(20)
            .all()
        )
        task_state = TaskAdmission.public_state(user_id)
        if not tickets:
            return {'items': [], **task_state}
        queue_ahead_map = _estimate_queue_parties_ahead_map([t.id for t in tickets if t.status == 'queued'])
        fate_dungeons_by_adventurer_id = _active_fate_dungeons_by_adventurer_ids(
            member.adventurer_id
            for ticket in tickets
            for member in ticket.members
        )
        payload = {
            **task_state,
            'items': [
                _ticket_payload(
                    ticket,
                    user_id=user_id,
                    queue_ahead_map=queue_ahead_map,
                    fate_dungeons_by_adventurer_id=fate_dungeons_by_adventurer_id,
                )
                for ticket in tickets
            ],
        }
        return payload

    @staticmethod
    def board(user_id: int, client_version: Optional[str] = None) -> tuple[dict, None]:
        version = get_board_version('matchmaking')
        if version is not None and client_version == version:
            return {'changed': False, 'version': version}, None

        adv_ids = None

        def get_adv_ids() -> list[int]:
            nonlocal adv_ids
            if adv_ids is None:
                adv_ids = _user_adventurer_ids(user_id)
            return adv_ids

        ticket_cache_key = _versioned_user_cache_key(user_id, version)
        cached_tickets = _my_tickets_cache.get(ticket_cache_key) if ticket_cache_key is not None else None
        if cached_tickets is None:
            cached_tickets = AdventurerS1MatchmakingService._my_tickets_payload(user_id, get_adv_ids())
            if ticket_cache_key is not None:
                _my_tickets_cache.set(ticket_cache_key, cached_tickets)

        cached_invites = _my_invites_cache.get(user_id)
        if cached_invites is None:
            cached_invites = AdventurerS1MatchmakingService._my_invites_payload(user_id, get_adv_ids())
            _my_invites_cache.set(user_id, cached_invites)

        payload = {
            'changed': True,
            'version': version,
            'my_tickets': cached_tickets,
            'my_invites': cached_invites,
            'public_parties': _public_tickets_payload_for_user(user_id),
            'available_dungeons': _available_dungeons_board_payload(user_id, adv_ids),
            'queue_summary': _queued_ticket_count_payload(),
        }
        return payload, None

    @staticmethod
    def _dispatch_slot_count() -> int:
        from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import get_daily_expedition_quota_state
        from plugins.adventurer_s1.services.adventurer_s1_expedition_throttle_service import (
            compute_dispatch_allowance,
            minutes_since_last_departure,
        )
        used, quota = get_daily_expedition_quota_state()
        if quota <= 0:
            return 1
        remaining = max(0, quota - used)
        if remaining <= 0:
            return 0
        allowance = compute_dispatch_allowance(used, quota, minutes_since_last_departure())
        # 已到点队伍可能因缺门票等自身校验失败而长期保留 forming；它们不能占用
        # 新组队名额。发车器会按顺序跳过失败队，并在本轮最多成功放行一队。
        return max(0, min(remaining, allowance))

    @staticmethod
    def enqueue_automatic_solo_matches() -> dict:
        """将精力达标且偏好为 auto 的玩家冒险者投入随机地图单人匹配。

        候选与活跃报名先批量读取；真正写入时仍按冒险者逐个短事务加行锁并复查，
        以免和玩家手动报名、建筑占位或偏好切换并发时产生重复报名。
        """
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service
        from plugins.adventurer_s1.services.adventurer_s1_tavern_seat_service import (
            AdventurerS1TavernSeatService,
        )

        candidates = (
            AdventurerS1.query
            .options(
                load_only(
                    AdventurerS1.id,
                    AdventurerS1.name,
                    AdventurerS1.owner_id,
                    AdventurerS1.is_player_created,
                    AdventurerS1.party_preference,
                    AdventurerS1.status,
                    AdventurerS1.stamina,
                    AdventurerS1.stamina_updated_at,
                    AdventurerS1.building_instance_id,
                    AdventurerS1.building_assigned_at,
                    AdventurerS1.dorm_room_id,
                    AdventurerS1.bed_index,
                ),
                noload(AdventurerS1.traits),
                noload(AdventurerS1.equipment_items),
                noload(AdventurerS1.skill_items),
                noload(AdventurerS1.investor_items),
                noload(AdventurerS1.warehouse_items),
                noload(AdventurerS1.materials),
                noload(AdventurerS1.story_threads),
                selectinload(AdventurerS1.dorm_room).options(
                    noload(AdventurerS1DormRoom.occupants),
                    joinedload(AdventurerS1DormRoom.building_instance).options(
                        joinedload(AdventurerS1BuildingInstance.template),
                        noload(AdventurerS1BuildingInstance.helpers),
                    ),
                ),
            )
            .filter(
                AdventurerS1.owner_id.isnot(None),
                AdventurerS1.is_player_created.is_(True),
                AdventurerS1.party_preference == 'auto',
                AdventurerS1.status.in_(AdventurerS1.DISPATCHABLE_STATUSES),
                AdventurerS1.building_instance_id.is_(None),
            )
            .order_by(AdventurerS1.id.asc())
            .all()
        )
        if not candidates:
            return {'queued': 0, 'ticket_ids': []}

        candidate_ids = [adv.id for adv in candidates]
        active_adventurer_ids = {
            adventurer_id
            for (adventurer_id,) in (
                AdventurerS1MatchTicketMember.query
                .join(
                    AdventurerS1MatchTicket,
                    AdventurerS1MatchTicket.id == AdventurerS1MatchTicketMember.ticket_id,
                )
                .filter(
                    AdventurerS1MatchTicketMember.adventurer_id.in_(candidate_ids),
                    AdventurerS1MatchTicket.status.in_(['forming', 'queued']),
                )
                .with_entities(AdventurerS1MatchTicketMember.adventurer_id)
                .all()
            )
        }
        seated_ids = AdventurerS1TavernSeatService.get_seated_adventurer_ids(candidate_ids)
        ready_ids = [
            adv.id for adv in candidates
            if adv.id not in active_adventurer_ids
            and _check_stamina(adv, tavern_seated=adv.id in seated_ids) is None
        ]

        ticket_ids: list[int] = []
        owner_ids: set[int] = set()
        stamina_min = _int_cfg('ADVENTURER_S1_STAMINA_MIN_TO_ENTER', 80)
        for adventurer_id in ready_ids:
            with db_manager.transaction():
                adv = (
                    AdventurerS1.query
                    .populate_existing()
                    .options(
                        selectinload(AdventurerS1.traits),
                        noload(AdventurerS1.equipment_items),
                        noload(AdventurerS1.skill_items),
                        noload(AdventurerS1.investor_items),
                        noload(AdventurerS1.warehouse_items),
                        noload(AdventurerS1.materials),
                        noload(AdventurerS1.story_threads),
                    )
                    .filter_by(id=adventurer_id)
                    .with_for_update()
                    .first()
                )
                if not adv:
                    continue
                if (
                    adv.owner_id is None
                    or not adv.is_player_created
                    or adv.party_preference != 'auto'
                    or adv.status not in AdventurerS1.DISPATCHABLE_STATUSES
                    or adv.building_instance_id is not None
                ):
                    continue
                can_dispatch, _reason = adv.can_dispatch()
                if not can_dispatch or _active_ticket_for_adventurer(adv.id):
                    continue
                if _check_stamina(adv, tavern_seated=adv.id in seated_ids):
                    continue

                AdventurerS1StateTransitionCoordinator.leave_rest(adv, 'in_party')
                if adv.stamina <= stamina_min:
                    AdventurerS1StateTransitionCoordinator.enter_rest(adv)
                    continue

                ticket = AdventurerS1MatchTicket(
                    owner_id=adv.owner_id,
                    queue_type='solo',
                    access_mode='public',
                    allow_solo_fill=False,
                    preferred_dungeon=None,
                    preferred_dungeons=None,
                    description='',
                    status='queued',
                    queued_at=_portable_utc_now(),
                    match_error='',
                )
                db_manager.add(ticket)
                db_manager.flush()
                db_manager.add(AdventurerS1MatchTicketMember(
                    ticket_id=ticket.id,
                    adventurer_id=adv.id,
                ))
                db_manager.flush()
                ticket_ids.append(ticket.id)
                owner_ids.add(adv.owner_id)

        if ticket_ids:
            _after_matchmaking_mutation(ticket_user_ids=owner_ids)
        return {'queued': len(ticket_ids), 'ticket_ids': ticket_ids}

    @staticmethod
    def tick_matchmaking(*, enqueue_automatic: bool = True, dispatch_immediately: bool = True) -> dict:
        slots = AdventurerS1MatchmakingService._dispatch_slot_count()
        if slots <= 0:
            return {'matched': 0, 'party_ids': [], 'slots': 0, 'auto_queued': 0}

        auto_result = (
            AdventurerS1MatchmakingService.enqueue_automatic_solo_matches()
            if enqueue_automatic else {'queued': 0}
        )

        party_ids: list[int] = []
        for _ in range(slots):
            party_id = AdventurerS1MatchmakingService._match_one_party()
            if not party_id:
                break
            party_ids.append(party_id)

        if party_ids:
            # 成团会同时改变每位成员的“我的匹配”和“我的小队”。不能只失效公共
            # 匹配缓存，否则玩家可能继续读到成团前的票据，且看不到新队伍。
            _after_matchmaking_mutation()
            from plugins.adventurer_s1.services.adventurer_s1_tavern_service import (
                invalidate_parties_board_cache,
            )
            invalidate_parties_board_cache()
            from plugins.adventurer_s1.services.adventurer_s1_party_service import (
                is_auto_dispatch_on_full_enabled,
            )
            if not dispatch_immediately:
                # 发车 owner 已持有集群锁，成团后由外层继续发车，避免递归拿锁。
                dispatch_result = None
            elif is_auto_dispatch_on_full_enabled():
                from plugins.adventurer_s1.services.adventurer_s1_expedition_runner import (
                    dispatch_departed_expeditions,
                )
                dispatch_result = dispatch_departed_expeditions()
            else:
                dispatch_result = {'auto_dispatch_disabled': True}
        else:
            dispatch_result = None
        return {
            'matched': len(party_ids),
            'party_ids': party_ids,
            'slots': slots,
            'auto_queued': auto_result['queued'],
            'dispatch': dispatch_result,
        }

    @staticmethod
    def _match_one_party() -> int | None:
        min_size = _match_min_size()
        duplicate_members_changed = False
        matched_party_id: int | None = None
        with notify_task_cancellations_after_commit() as cancellation_changed, db_manager.transaction():
            tickets = (
                AdventurerS1MatchTicket.query
                .populate_existing()
                .filter_by(status='queued')
                .order_by(AdventurerS1MatchTicket.queued_at.asc(), AdventurerS1MatchTicket.id.asc())
                .with_for_update()
                .all()
            )
            if not tickets:
                return None
            now = _portable_utc_now()
            remaining_tickets = []
            for ticket in tickets:
                if TaskAdmission.cancel_expired_locked_ticket(ticket, now=now):
                    cancellation_changed[0] = True
                else:
                    remaining_tickets.append(ticket)
            tickets = remaining_tickets
            closed_dungeons = S1WorldLocationStateService.closed_dungeon_names(
                _ACTIVE_DUNGEON_NAMES,
            )
            tickets, duplicate_members_changed = _remove_duplicate_queued_members(tickets)
            # 即使全部被取消也不提前返回：状态变更需要先提交，再在事务外失效读缓存。
            tickets = _sort_match_tickets(tickets)
            fate_dungeons_by_adventurer_id = _active_fate_dungeons_by_adventurer_ids(
                member.adventurer_id
                for ticket in tickets
                for member in ticket.members
            )

            for anchor_index, anchor in enumerate(tickets):
                if anchor.queue_type == 'group':
                    fate_risk = _ticket_fate_risk_state(anchor, fate_dungeons_by_adventurer_id)
                    if fate_risk['pending_member_names']:
                        _stop_ticket_for_match_error(
                            anchor,
                            '命途风险尚未确认，请等待 '
                            + '、'.join(fate_risk['pending_member_names'])
                            + ' 确认后再开始匹配',
                        )
                        continue
                match_error = _ticket_match_error(
                    anchor,
                    closed_dungeons=closed_dungeons,
                )
                if match_error:
                    _stop_ticket_for_match_error(anchor, match_error)
                    continue

                anchor_count = _ticket_member_count(anchor)
                if anchor_count <= 0 or anchor_count > 6:
                    continue

                selected = [anchor]
                selected_ids = {anchor.id}
                selected_dungeons = _ticket_preferred_dungeons(anchor)
                selected_ranks = _allowed_ranks_for_ticket(anchor)
                if selected_dungeons:
                    selected_ranks &= {get_s1_dungeon_rank(name) for name in selected_dungeons}
                if not selected_ranks:
                    _stop_ticket_for_match_error(anchor, '小队成员与目标副本档位不兼容，无法进入匹配')
                    continue

                member_total = anchor_count
                can_fill_solo = anchor.queue_type == 'solo' or _ticket_allows_solo_fill(anchor)
                if can_fill_solo:
                    for cand in tickets[anchor_index + 1:]:
                        if cand.id in selected_ids:
                            continue
                        # 组队报名保持整体排队，不与另一支组队报名拼团；补位只吸收单人匹配。
                        if cand.queue_type != 'solo':
                            continue
                        cand_error = _ticket_match_error(
                            cand,
                            closed_dungeons=closed_dungeons,
                        )
                        if cand_error:
                            _stop_ticket_for_match_error(cand, cand_error)
                            continue
                        cand_count = _ticket_member_count(cand)
                        if cand_count <= 0 or member_total + cand_count > 6:
                            continue
                        ok, next_dungeons = _preferred_dungeons_compatible(
                            selected_dungeons, _ticket_preferred_dungeons(cand),
                        )
                        if not ok:
                            continue
                        cand_ranks = _allowed_ranks_for_ticket(cand)
                        next_ranks = selected_ranks & cand_ranks
                        if next_dungeons:
                            next_ranks &= {get_s1_dungeon_rank(name) for name in next_dungeons}
                        if not next_ranks:
                            continue
                        next_selected = [*selected, cand]
                        blocked_fate_dungeons = _blocked_fate_dungeons_for_low_level_members(
                            next_selected,
                            fate_dungeons_by_adventurer_id,
                        )
                        has_safe_dungeon = any(
                            S1_DUNGEON_RANK_MAP.get(dungeon['name'], 0) in next_ranks
                            and (next_dungeons is None or dungeon['name'] in next_dungeons)
                            and dungeon['name'] not in blocked_fate_dungeons
                            and dungeon['name'] not in closed_dungeons
                            for dungeon in S1_ACTIVE_DUNGEONS
                        )
                        if not has_safe_dungeon:
                            continue
                        selected.append(cand)
                        selected_ids.add(cand.id)
                        selected_dungeons = next_dungeons
                        selected_ranks = next_ranks
                        member_total += cand_count
                        if member_total >= 6:
                            break

                if member_total < min_size:
                    continue
                target_size = min(member_total, 6)

                blocked_fate_dungeons = _blocked_fate_dungeons_for_low_level_members(
                    selected,
                    fate_dungeons_by_adventurer_id,
                )
                dungeon = _pick_random_dungeon(
                    selected_ranks,
                    _member_levels_from_tickets(selected),
                    selected_dungeons,
                    blocked_fate_dungeons | closed_dungeons,
                    **({'activity_dungeon': anchor.preferred_dungeon}
                       if anchor.campaign_dispatch_id and anchor.preferred_dungeon not in _ACTIVE_DUNGEON_NAMES
                       else {}),
                )
                if not dungeon:
                    _stop_ticket_for_match_error(anchor, '没有可供当前成员共同进入的匹配副本')
                    continue

                from plugins.adventurer_s1.services.adventurer_s1_party_service import (
                    is_auto_dispatch_on_full_enabled,
                )
                party = AdventurerS1Party(
                    name=f"匹配小队_{random.randint(100, 999)}",
                    target_dungeon=dungeon,
                    target_size=target_size,
                    status='forming',
                    departure_time=_portable_utc_now() if is_auto_dispatch_on_full_enabled() else None,
                )
                ok, error = db_manager.add(party)
                if not ok:
                    raise RuntimeError(f'创建匹配小队失败: {error}')

                now = _portable_utc_now()
                matched_ticket_ids: list[int] = []
                party_members: list[AdventurerS1PartyMember] = []
                for ticket in selected:
                    ticket.status = 'matched'
                    ticket.match_error = ''
                    ticket.matched_party_id = party.id
                    ticket.matched_at = now
                    matched_ticket_ids.append(ticket.id)
                    for member in ticket.members:
                        party_members.append(AdventurerS1PartyMember(
                            party_id=party.id,
                            adventurer_id=member.adventurer_id,
                        ))
                ok, error = db_manager.add_all(party_members)
                if not ok:
                    raise RuntimeError(f'创建匹配小队成员失败: {error}')
                _cancel_pending_invites(ticket_ids=matched_ticket_ids)

                matched_party_id = party.id
                break

        if duplicate_members_changed:
            invalidate_matchmaking_read_cache()
        return matched_party_id
